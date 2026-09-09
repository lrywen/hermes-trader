"""Persistent agent memory — a disk-backed singleton loaded from .agent-memory.json.

The JSON file is a per-component CACHE. The authoritative append-only record is
``~/.hermes-trading/events.jsonl`` (shared with HTA). On ``load()`` the memory is
hydrated from events.jsonl when present (P2-10); the JSON cache is used as a
fast-path fallback. Every ``flush()`` is now guarded by an ``fcntl.flock``
exclusive lock (4.5.1) so a second process (dashboard, MCP, backtest) can no
longer truncate or race the live trading loop's memory file.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections import deque
from typing import Any, Deque, Optional

from hermes_trader.agents import atomic_io

logger = logging.getLogger(__name__)


# Audit 2026-09-06 (E5, P2): state-file resilience for .agent-memory.json.
# A corrupt cache used to be logged and silently replaced by an empty memory
# (events.jsonl replay still backfills trades, but intraday fields/PnL trail
# were lost). The corrupt file is now quarantined (.corrupt-<ts>), a risk
# card + metric fired, and a single-generation .bak (last-known-good) tried
# first. Best-effort throughout: failures here must never break the hot path.
def _quarantine_corrupt_memory(path: str) -> None:
    """Move a corrupt memory file aside to ``<path>.corrupt-<ms>``. Best-effort.

    Audit 2026-09-06 (E5, P2). Also bumps MEMORY_CORRUPT_ISOLATIONS and sends
    a risk-category Feishu card, each individually guarded.
    """
    quar = None
    try:
        quar = f"{path}.corrupt-{int(time.time() * 1000)}"
        shutil.move(path, quar)
        logger.error(f"[memory] corrupt file quarantined: {path} -> {quar}")
    except OSError as qe:
        logger.error(f"[memory] failed to quarantine corrupt file {path}: {qe}")
    try:
        from hermes_trader.metrics import MEMORY_CORRUPT_ISOLATIONS

        MEMORY_CORRUPT_ISOLATIONS.inc()
    except Exception:
        pass
    try:
        from hermes_trader import notify

        notify.send_card(
            "Agent 记忆文件损坏已隔离",
            fields={
                "文件": path,
                "隔离副本": str(quar),
                "后续动作": "已尝试回退 .bak，并回放 events.jsonl 兜底重建",
            },
            category="risk",
            level="danger",
            dedup_key="agent-memory-corrupt",
        )
    except Exception:
        pass


def _read_memory_candidate(path: str) -> Optional[dict[str, Any]]:
    """Read+json.load one memory-file candidate; quarantine + return None on
    failure (FileNotFoundError → None silently). Best-effort, never raises.

    Audit 2026-09-06 (E5, P2).
    """
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON is not an object")
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"[memory] candidate unreadable ({path}): {e}")
        _quarantine_corrupt_memory(path)
        return None


def _rotate_memory_bak_pre() -> None:
    """Copy the live memory file to .bak before it is overwritten. Best-effort.

    Audit 2026-09-06 (E5, P2). The caller already holds the cross-process
    flock via atomic_io.locked_write_json_atomic's lock convention; the copy
    is done before that helper replaces the live file.
    """
    try:
        if os.path.exists(MEMORY_FILE):
            shutil.copy2(MEMORY_FILE, MEMORY_FILE + ".bak")
    except OSError as e:
        logger.warning(f"[memory] pre-write .bak rotation failed: {e}")


def _seed_memory_bak_post() -> None:
    """Seed .bak from the freshly written live file if no .bak exists yet
    (first-ever flush has no predecessor). Best-effort, never raises.

    Audit 2026-09-06 (E5, P2).
    """
    try:
        if not os.path.exists(MEMORY_FILE + ".bak") and os.path.exists(MEMORY_FILE):
            shutil.copy2(MEMORY_FILE, MEMORY_FILE + ".bak")
    except OSError as e:
        logger.warning(f"[memory] post-write .bak seed failed: {e}")

# P1-6: flush() is invoked on every record_trade/record_close/cooldown/circuit
# mutation — dozens of times per scan cycle, each doing a full json.dump +
# tmp+replace of the whole memory file. Throttle non-critical flushes to one
# per this many seconds; critical writes (realized closes, startup rebuild)
# pass force=True to bypass both the dirty gate and the throttle.
FLUSH_THROTTLE_S = float(os.environ.get("HERMES_MEMORY_FLUSH_THROTTLE_S", "0.2"))

# Anchored to the repo root (mirrors config_store.py), not os.getcwd() — so the
# MCP server and the trading loop always share one .agent-memory.json regardless
# of which directory each was launched from.
# Override with HERMES_AGENT_MEMORY_FILE when deploying behind a mounted volume.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MEMORY_FILE = os.environ.get(
    "HERMES_AGENT_MEMORY_FILE",
    os.path.join(_REPO_ROOT, ".agent-memory.json"),
)
# Cross-process exclusive lock guarding flush() (4.5.1). Held only for the
# duration of the atomic tmp+replace; never held across long operations.
MEMORY_LOCK_FILE = MEMORY_FILE + ".lock"

# Authoritative append-only event log shared with HTA (P2-10).
_EVENTS_FILE = os.environ.get(
    "HERMES_EVENTS_FILE",
    os.path.expanduser("~/.hermes-trading/events.jsonl"),
)

# P2-3: fallback defaults; the live limits come from config
# (``memory_limits.*`` in .agent-config.json) via _memory_limits() below.
MAX_PERCEPTIONS = 500
MAX_ANALYSES = 200
MAX_TRADES = 100
MAX_CLOSES = 500  # realized trade outcomes — backs win-rate / payoff / risk-of-ruin / Phase-3 stats
# P1-6: each coin's incremental exit-slip deque is capped (at the configured
# closes limit) so it cannot grow unboundedly; the read-time close-time window
# filters older entries anyway.

# R9/P3-4: age-based retention (days). The count caps above bound the list
# length, but without an age cutoff a low-traffic deployment keeps months-old
# perceptions/analyses in the agent's working context forever. 0 disables age
# eviction for that list (trades are an audit record — count-capped only).
MAX_AGE_DAYS_DEFAULT = {"perceptions": 30.0, "analyses": 30.0, "trades": 0.0}
# Candidate timestamp keys (ms epoch) per list, in priority order. Records with
# no usable timestamp are kept (never silently evict what we can't date).
_AGE_TS_KEYS = {
    # 2026-09-02：perception 记录的时间戳字段是 fired_at（perception.py 写入
    # int(time.time()*1000)），此前候选键里没有它 → 所有 perception 都被当成
    # “无法定龄”而保留，30 天 TTL 形同虚设。fired_at 前置作为首选键。
    "perceptions": ("fired_at", "ts", "created_at", "timestamp"),
    "analyses": ("created_at", "ts", "timestamp"),
    "trades": ("executed_at", "created_at", "ts", "timestamp"),
}


def _memory_max_age_days() -> dict[str, float]:
    """Configured max age in days for the time-bounded memory lists.

    R9/P3-4: reads ``memory_limits.max_age_days.<list>`` via cfg_get with the
    ``MAX_AGE_DAYS_DEFAULT`` fallbacks. A missing/invalid/negative value falls
    back to the default; 0 means "do not evict by age" for that list.
    """
    from hermes_trader.agents.config_store import cfg_get
    cfg = cfg_get("memory_limits.max_age_days", None)
    if not isinstance(cfg, dict):
        return dict(MAX_AGE_DAYS_DEFAULT)
    out: dict[str, float] = {}
    for key, fallback in MAX_AGE_DAYS_DEFAULT.items():
        try:
            v = float(cfg.get(key, fallback))
            out[key] = v if v >= 0 else fallback
        except (TypeError, ValueError):
            out[key] = fallback
    return out


def _evict_aged(records: list[dict[str, Any]], list_key: str,
                cutoff_ms: float) -> list[dict[str, Any]]:
    """Drop records older than ``cutoff_ms`` (ms epoch); keep undatable ones.

    Returns a new list so callers can reassign under the lock.
    """
    keys = _AGE_TS_KEYS.get(list_key, ())
    kept: list[dict[str, Any]] = []
    for rec in records:
        ts_ms = None
        for k in keys:
            v = rec.get(k)
            if v:
                try:
                    ts_ms = float(v)
                    break
                except (TypeError, ValueError):
                    continue
        # No usable timestamp → keep (matches the closes window convention of
        # never aging out rows we can't date).
        if ts_ms is None or ts_ms <= 0 or ts_ms >= cutoff_ms:
            kept.append(rec)
    return kept


def _memory_limits() -> dict[str, int]:
    """Configured retention limits for the in-process memory lists.

    P2-3: these were hardcoded module constants. Read through cfg_get with
    the constants above as fallbacks so a missing/invalid config never
    shrinks memory below a sane bound.
    """
    from hermes_trader.agents.config_store import cfg_get
    def _limit(sub_key: str, fallback: int) -> int:
        try:
            v = int(cfg_get(f"memory_limits.{sub_key}", fallback))
            return v if v > 0 else fallback
        except (TypeError, ValueError):
            return fallback
    return {
        "perceptions": _limit("max_perceptions", MAX_PERCEPTIONS),
        "analyses": _limit("max_analyses", MAX_ANALYSES),
        "trades": _limit("max_trades", MAX_TRADES),
        "closes": _limit("max_closes", MAX_CLOSES),
    }


# R13-B11: equity data-quality gate tunables. The literals below mirror the
# values previously hardcoded in track_daily_pnl (_IMPLAUSIBLE_PCT = 0.25, the
# `< 180` window, `streak < 2`), the avg_exit_slip_bps signature defaults
# (days=30.0, min_samples=3) and the FLUSH_THROTTLE_S module constant (0.2);
# they stay as the final fallback layer. CRASH_DOWN_PCT_DEFAULT /
# FLUSH_THROTTLE_S also carry their legacy env-var defaults (kept as the
# top-priority channel by _memory_quality_params for operator/test compat).
_MEMORY_QUALITY_DEFAULTS: dict[str, Any] = {
    "implausible_pct": 0.25,
    "crash_down_pct": 0.40,
    "filter_window_sec": 180,
    "reconfirm_streak": 2,
    "slip_window_days": 30.0,
    "slip_min_samples": 3,
    "flush_throttle_s": 0.2,
}
# CS-G (sizing-v2 short side + cost cap) cold-start conservative fallbacks.
# When a per-side series has too few samples, the reader degrades to the
# shared coin series / a global same-side mean, and only then to these
# literals — never to ZERO (zero cost widening would size the position as if
# fills and carry were free). 2.0 bps/side matches the existing offline PF
# reports' no-measurement adverse-slip assumption (_DEFAULT_SLIPPAGE_BPS);
# 8h is the conservative expected perp carry horizon before a same-side
# measured mean-hold exists.
_DEFAULT_COLD_EXIT_SLIP_BPS = 2.0
_DEFAULT_EXPECTED_HOLD_HOURS = 8.0
# leaf -> (legacy env var or None, kind "i"/"f", minimum guard).
_MEMORY_QUALITY_SPEC: dict[str, tuple[Optional[str], str, float]] = {
    "implausible_pct": (None, "f", 0.0),
    "crash_down_pct": ("HERMES_EQUITY_CRASH_DOWN_PCT", "f", 0.0),
    "filter_window_sec": (None, "i", 0),
    "reconfirm_streak": (None, "i", 1),
    "slip_window_days": (None, "f", 0.0),
    "slip_min_samples": (None, "i", 1),
    "flush_throttle_s": ("HERMES_MEMORY_FLUSH_THROTTLE_S", "f", 0.0),
}


def _memory_quality_params() -> dict[str, Any]:
    """Resolve the seven memory equity quality-gate knobs (independent copy).

    R13-B11: per leaf, a non-empty legacy env var wins
    (HERMES_EQUITY_CRASH_DOWN_PCT / HERMES_MEMORY_FLUSH_THROTTLE_S — operator
    /test compat), then cfg_get covers HERMES_CFG_MEMORY_QUALITY__<LEAF> env
    and the agent-config dict, then the inline literal. Ints/floats are
    coerced and must clear the minimum guard. Any failure returns a fresh
    literal copy so the equity hot path never raises.
    """
    from hermes_trader.agents.config_store import cfg_get
    p = dict(_MEMORY_QUALITY_DEFAULTS)
    try:
        for leaf, (legacy_env, kind, min_v) in _MEMORY_QUALITY_SPEC.items():
            raw: Any = None
            if legacy_env is not None:
                raw = os.environ.get(legacy_env)
            if raw is None or raw == "":
                raw = cfg_get(f"memory_quality.{leaf}")
            if raw is None:
                continue
            v: Any = int(raw) if kind == "i" else float(raw)
            if v >= min_v:
                p[leaf] = v
    except Exception as e:
        logger.debug(f"[memory] memory_quality params read failed, using literals: {e}")
        return dict(_MEMORY_QUALITY_DEFAULTS)
    return p


class AgentMemory:
    """Singleton — persistent in-memory state + disk persistence."""

    _instance: Optional["AgentMemory"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        # In-process lock guarding every mutation + the flush snapshot
        # (P0-B/P0-C). flock below only serializes cross-process writers;
        # within one process the research thread pool, dashboard handlers and
        # the trading loop all touch this singleton, so the state mutations
        # and the snapshot taken for json.dump must be atomic — otherwise a
        # concurrent mutation can either land after the snapshot (lost update)
        # or mutate a dict while json.dump iterates it (RuntimeError).
        self._lock = threading.RLock()
        self._perceptions: list[dict[str, Any]] = []
        self._analyses: list[dict[str, Any]] = []
        self._trades: list[dict[str, Any]] = []
        self._closes: list[dict[str, Any]] = []  # realized exits (the trade-outcome store)
        # Entry context keyed by "COIN_side" — entry time + the signal snapshot at
        # entry, so the matching close can carry it for the forward signal backtest.
        self._entry_ctx: dict[str, dict[str, Any]] = {}
        self._cooldowns: dict[str, int] = {}
        self._equity: float = 0
        self._daily_pnl: float = 0
        self._peak_daily_pnl: float = 0  # high-water mark of daily_pnl (intraday, resets at UTC roll)
        # (supplemental audit 2026-09-02) High-water mark of REALIZED daily
        # PnL only (locked-in closes), excluding unrealized float. The give-back
        # breaker arms off this so a transient open-position paper spike can no
        # longer latch the gate and block fresh entries for the rest of the UTC
        # day. Rebuilt from the persisted _closes ledger on day-roll/restart, so
        # it is restart-safe (unlike _peak_daily_pnl, which is mark-to-market).
        self._peak_daily_realized_pnl: float = 0
        # B-F7 (deep audit 2026-08-28): all-time high-water mark of equity,
        # backing the account max-drawdown gate. Unlike _peak_daily_pnl this
        # does NOT reset at UTC roll — a drawdown is measured from the highest
        # equity the account has ever reached. Updated on every accepted equity
        # tick in track_daily_pnl (after the implausible-read filter).
        self._peak_equity: float = 0
        # CS-D: the cumulative net external flow basis that _peak_equity was
        # recorded on. The all-time peak is re-based onto the current flow
        # basis at read time (_peak_equity + cur_flow − _peak_equity_basis_flow)
        # so money added AFTER the high-water tick doesn't understate dd%.
        self._peak_equity_basis_flow: float = 0.0
        # Drawdown-gate recovery (fix 2026-09-03): the all-time peak above
        # never resets, so after a permanent equity drop (realized loss,
        # withdrawal, balance rebase) the drawdown gate latched FOREVER with
        # no recovery path (observed: peak $50.9 vs equity $20.9 → −58.9%,
        # 111 consecutive blocks). The gate now measures against a ROLLING
        # peak over a configurable window instead of the all-time high.
        #   _equity_trail: deque of (epoch_s, raw_equity, cum_flow), one
        #     sample ~per scan tick (age-pruned); rolling_peak_equity(window_days)
        #     re-bases each sample onto the current flow basis, takes the max
        #     over samples newer than window_days, and falls back to the
        #     flow-rebased all-time peak when no windowed samples exist. CS-D
        #     (2026-09-08): cum_flow is cumulative net external flow as of the
        #     tick; legacy (ts, equity) pairs parse as flow=None → one-shot rebase.
        self._equity_trail: Deque[tuple[float, float, float]] = deque()
        # Drawdown freeze bookkeeping (epoch ms): when the gate freezes it
        # stamps _dd_frozen_since_ms; once the freeze has lasted the
        # configured cooldown, the baseline re-arms to current equity (the
        # same event the rolling peak would produce organically as old highs
        # age out, but bounded to ~cooldown hours instead of a full window).
        self._dd_frozen_since_ms: int = 0
        self._dd_last_baseline_ms: int = 0
        # CS-D (2026-09-08): one-shot flag — becomes True once the persisted
        # equity trail has been migrated from bare (ts, equity) tuples onto the
        # flow-annotated (ts, equity, cum_flow) basis. Persisted so the rebase
        # runs exactly once across the upgrade, not once per process start.
        self._dd_basis_migrated: bool = False
        self._start_of_day_equity: float = 0
        self._day_start_ts: int = 0
        # P0-1 (v3): cumulative net EXTERNAL capital flow into the tradeable
        # equity pool (deposit − withdrawal, incl. cross-pool transfers in),
        # used solely to render a cash-flow-neutral equity curve / return on
        # the dashboard — a deposit must not show up as trading profit. Risk
        # gates and position sizing keep reading raw equity and are untouched.
        #   _contrib_folded_total: flow across fully-elapsed UTC days;
        #   _contrib_today: today's running total (re-fetched from the ledger
        #     each tick, folded into the total at the UTC roll).
        self._contrib_folded_total: float = 0.0
        self._contrib_today: float = 0.0
        self._open_positions: list[dict[str, Any]] = []
        # ── Tiered circuit-breaker state (sizing/risk-overhaul 2026-08-26) ──
        # coin -> epoch-ms until which new entries on that coin are blocked
        # (single-coin per-trade loss > threshold). Global halt is a single
        # expiry for the whole book (daily cumulative loss > equity threshold).
        self._coin_circuit: dict[str, int] = {}
        self._global_halt_until_ms: int = 0
        # Per-coin consecutive losing-close count (resets on a win or day roll).
        self._consecutive_losses: dict[str, int] = {}
        # P1-6: dirty flag + flush throttle. Mutations set _dirty; flush()
        # skips a clean store and coalesces bursts of mutations within
        # FLUSH_THROTTLE_S into one json.dump+replace. force=True (realized
        # closes, startup rebuild) bypasses both gates.
        self._dirty: bool = False
        self._last_flush_ts: float = 0.0
        # P1-6: incremental O(1) close statistics, replacing per-call O(n)
        # scans of up to MAX_CLOSES rows on the hot research/execution path.
        # coin -> deque of (closed_at_epoch_s, adverse_slip_bps>0) for the
        # running exit-slip mean; coin -> today's realized USD sum for the
        # per-coin daily loss breaker. Both are rebuilt once after hydration
        # (load/replay) and updated incrementally in record_close.
        self._close_stats_built: bool = False
        self._slip_series: dict[str, Deque[tuple[float, float]]] = {}
        # O-8 (supplemental audit 2026-08-30): coin -> deque of
        # (closed_at_epoch_s, realized_round_trip_fee_bps>0). Calibrates the
        # backtests' hardcoded 5-bps round-trip fee against ACTUAL exchange
        # fees (fee_usd / notional_usd from closes marked fee_actual=True —
        # i.e. real exchange fill fees, not the in-process modeled estimate).
        self._fee_series: dict[str, Deque[tuple[float, float]]] = {}
        # CS-G (sizing-v2 short side): direction-differentiated views rebuilt
        # from the same close rows (which carry `side`, `exit_slip_bps` and
        # `hold_minutes`). Key (coin, side) -> deque of (closed_at_s, value).
        # _slip_side tracks adverse exit slip per side (shorts and longs do
        # not share fill quality under stress); _hold_side tracks realized
        # holding time in HOURS per side to size the expected funding carry.
        self._slip_side: dict[tuple[str, str], Deque[tuple[float, float]]] = {}
        self._hold_side: dict[tuple[str, str], Deque[tuple[float, float]]] = {}
        self._day_realized_usd: dict[str, float] = {}
        self._day_stats_start_ts: int = 0
        self._initialized = False
        # Cross-process freshness (fix 2026-09-03): the API server and the
        # trading loop are SEPARATE processes, each with its own memory
        # singleton. The loop writes the drawdown/circuit state to disk; the
        # server's in-memory copy is a snapshot from its own startup and never
        # refreshes (load() is _initialized-guarded), so the risk dashboard
        # showed a STALE freeze long after the loop had released it. The
        # read-only refresher below re-reads the risk fields when the file's
        # mtime advances. It never sets _dirty nor flushes, so a read-only
        # server scrape can never overwrite the loop's authoritative file.
        self._risk_file_mtime: float = 0.0

    @classmethod
    def get_instance(cls) -> "AgentMemory":
        # Double-checked locking: the trading loop, research workers and
        # dashboard handlers can all race the first call (P0-B).
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── Persistence ─────────────────────────────────────────────────────────

    def _rebuild_from_events(self) -> bool:
        """Rebuild trade/close history from the shared events.jsonl (P2-10).

        The per-component JSON is a cache; events.jsonl is the source of truth.
        On startup we replay ``order``/``close``/``risk`` events to repopulate
        ``_trades`` and ``_closes`` so a wiped/corrupt JSON cache never loses
        realized outcomes. Returns True if at least one event was replayed.
        """
        if not os.path.exists(_EVENTS_FILE):
            return False
        try:
            trades: list[dict[str, Any]] = []
            closes: list[dict[str, Any]] = []
            with open(_EVENTS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ev = rec.get("event")
                    payload = rec.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    # Stamp trace_id/timestamp onto the rebuilt record so the
                    # in-memory shape stays self-describing.
                    payload = dict(payload)
                    payload.setdefault("trace_id", rec.get("trace_id", ""))
                    payload.setdefault("event_ts", rec.get("timestamp", ""))
                    if ev == "order":
                        trades.append(payload)
                    elif ev == "close":
                        closes.append(payload)
            _limits = _memory_limits()
            if trades:
                self._trades = trades[-_limits["trades"]:]
            if closes:
                self._closes = closes[-_limits["closes"]:]
            if trades or closes:
                # P1-6: _closes was replaced wholesale — force the
                # incremental stats to lazily rebuild on first use / flush.
                self._close_stats_built = False
                logger.info(
                    f"[memory] rebuilt from events.jsonl: "
                    f"{len(trades)} orders, {len(closes)} closes"
                )
                return True
        except Exception as e:
            logger.warning(f"[memory] rebuild from events.jsonl failed: {e}")
        return False

    def load(self) -> None:
        """Load state from disk.

        Hydration order: the JSON cache restores all live/intraday fields;
        then ``events.jsonl`` is replayed to guarantee trades/closes are never
        lost even if the JSON cache was wiped (P2-10).
        """
        # Hold the in-process lock for the whole hydration: two threads racing
        # the first load() must not interleave field assignments (P0-B/P0-C).
        # RLock is reentrant so the flush() below is safe.
        with self._lock:
            if self._initialized:
                return
            rebuilt = False
            limits = _memory_limits()  # P2-3: config-driven retention caps
            try:
                # Audit 2026-09-06 (E5, P2): recovery chain live -> .bak.
                # A corrupt cache is quarantined/alerted/counted inside
                # _read_memory_candidate; the last-known-good .bak is then
                # tried. events.jsonl replay below remains the authoritative
                # backfill regardless of which candidate hydrated.
                data = _read_memory_candidate(MEMORY_FILE)
                if data is None:
                    data = _read_memory_candidate(MEMORY_FILE + ".bak")
                if data is None:
                    raise FileNotFoundError(MEMORY_FILE)

                # P0-2a: isinstance guards — a single top-level field of the
                # wrong type (corrupt/hand-edited file) must not abort the whole
                # hydration; drop that field and keep everything else.
                def _list_field(key):
                    val = data.get(key)
                    return val if isinstance(val, list) else []
                self._perceptions = _list_field("perceptions")[:limits["perceptions"]]
                self._analyses = _list_field("analyses")[:limits["analyses"]]
                self._trades = _list_field("trades")[:limits["trades"]]
                self._closes = _list_field("closes")[:limits["closes"]]
                _entry_ctx = data.get("entryCtx")
                self._entry_ctx = _entry_ctx if isinstance(_entry_ctx, dict) else {}

                # Rebuild cooldowns. P0-2a: per-row tolerance — one malformed
                # entry (missing coin, non-numeric expires, non-dict row) is
                # skipped instead of aborting the load and losing ALL history.
                self._cooldowns.clear()
                now = int(time.time() * 1000)
                _cooldown_rows = data.get("cooldowns")
                if isinstance(_cooldown_rows, list):
                    for c in _cooldown_rows:
                        try:
                            coin = c.get("coin")
                            exp = int(c.get("expires", 0) or 0)
                            if coin and exp > now:
                                self._cooldowns[str(coin)] = exp
                        except (AttributeError, TypeError, ValueError):
                            continue

                # P0-2a: coerce scalar fields; a non-numeric value in one field
                # degrades that field to its zero default instead of aborting the
                # whole hydration (the first accepted tick re-seeds equity/pnl).
                def _num(key, cast, default):
                    try:
                        return cast(data.get(key, default) or default)
                    except (TypeError, ValueError):
                        return default
                self._equity = _num("equity", float, 0.0)
                self._daily_pnl = _num("dailyPnl", float, 0.0)
                # P2-1: restore intraday PnL peak (absent in old files → 0.0;
                # the UTC day-roll check on the next tick re-baselines it if the
                # persisted day_start_ts is from a prior day).
                self._peak_daily_pnl = _num("peakDailyPnl", float, 0.0)
                self._start_of_day_equity = _num("startOfDayEquity", float, 0.0)
                self._day_start_ts = _num("dayStartTs", int, 0)
                # P0-1: restore cumulative external-flow state (absent in files
                # written before this feature → 0.0, seeds from the ledger on the
                # first accepted ticks). Coerce defensively; these feed only the
                # dashboard curve, never the risk path.
                try:
                    self._contrib_folded_total = float(data.get("cumContribFolded", 0.0) or 0.0)
                except (TypeError, ValueError):
                    self._contrib_folded_total = 0.0
                try:
                    self._contrib_today = float(data.get("cumContribToday", 0.0) or 0.0)
                except (TypeError, ValueError):
                    self._contrib_today = 0.0
                # B-F7: all-time equity high-water mark (absent in old files →
                # 0.0, re-seeds itself on the first accepted equity tick).
                try:
                    self._peak_equity = float(data.get("peakEquity", 0) or 0)
                except (TypeError, ValueError):
                    self._peak_equity = 0.0
                # CS-D: flow basis the persisted peak was recorded on (absent
                # in pre-CS-D files → 0.0; the one-shot legacy rebase re-tags
                # it onto the current basis on the first accepted tick).
                try:
                    self._peak_equity_basis_flow = float(
                        data.get("peakEquityBasisFlow", 0.0) or 0.0)
                except (TypeError, ValueError):
                    self._peak_equity_basis_flow = 0.0
                # Drawdown recovery state (absent in old files → empty trail /
                # zero stamps; the trail seeds itself from the next accepted
                # equity tick, and the first gate pass after upgrade re-baselines
                # a pre-existing latched freeze immediately rather than after a
                # full cooldown window).
                trail = self._parse_equity_trail(data.get("equityTrail"))
                self._equity_trail = deque(trail[-4320:])  # ≤ ~30d at 10-min cadence
                self._dd_frozen_since_ms = _num("ddFrozenSinceMs", int, 0)
                self._dd_last_baseline_ms = _num("ddLastBaselineMs", int, 0)
                # CS-D: a persisted flag means the trail is already on the
                # flow-annotated basis; absent (pre-CS-D file) → False so the
                # first accepted tick runs the one-shot legacy rebase.
                self._dd_basis_migrated = bool(data.get("ddBasisMigrated", False))
                _open_positions = data.get("openPositions", [])
                self._open_positions = _open_positions if isinstance(_open_positions, list) else []

                # Tiered circuit-breaker state (best-effort restore; a stale/
                # expired entry is harmless — the remaining-minutes accessor
                # purges it). P0-2a: per-entry tolerance so one bad value can't
                # abort the load.
                self._coin_circuit = {}
                _coin_circuit = data.get("coinCircuit")
                if isinstance(_coin_circuit, dict):
                    for k, v in _coin_circuit.items():
                        try:
                            iv = int(v)
                            if iv > now:
                                self._coin_circuit[str(k)] = iv
                        except (TypeError, ValueError):
                            continue
                self._global_halt_until_ms = _num("globalHaltUntilMs", int, 0)
                if self._global_halt_until_ms < now:
                    self._global_halt_until_ms = 0
                self._consecutive_losses = {}
                _consec = data.get("consecutiveLosses")
                if isinstance(_consec, dict):
                    for k, v in _consec.items():
                        try:
                            self._consecutive_losses[str(k)] = int(v)
                        except (TypeError, ValueError):
                            continue

                logger.info(
                    f"[memory] loaded {len(self._perceptions)} perceptions, "
                    f"{len(self._analyses)} analyses, {len(self._trades)} trades from {MEMORY_FILE}"
                )
            except FileNotFoundError:
                logger.info("[memory] no existing memory file found, starting fresh")
            except Exception as e:
                logger.error(f"[memory] load failed: {e}")

            # Source of truth: replay events.jsonl to backfill trades/closes.
            rebuilt = self._rebuild_from_events()

            # P1-6: build the incremental stats once over the hydrated
            # _closes (JSON cache + events replay merged), regardless of
            # whether a rebuild happened — even a JSON-only hydration needs
            # the structures. force=True persists the merged view back to
            # the JSON cache immediately (critical startup path).
            self._rebuild_close_stats_nolock()
            self._dirty = True
            self._initialized = True
            if rebuilt:
                # Persist the merged view back to the JSON cache.
                try:
                    self.flush(force=True)
                except Exception:
                    pass

    def refresh_risk_state_from_disk(self) -> None:
        """Read-only cross-process refresh of risk/drawdown fields.

        The API server and trading loop run as separate processes with
        independent memory singletons. The loop owns trading state and writes
        it to MEMORY_FILE; the server's copy is only hydrated at startup. This
        re-reads the live risk fields (equity, rolling peak trail, drawdown
        freeze stamps, circuit breakers) when the file's mtime has advanced,
        so the dashboard reflects the loop's CURRENT gate state instead of a
        startup snapshot.

        Strictly read-only on the write side: it never sets ``_dirty`` and
        never flushes, so a server scrape cannot clobber the loop's
        authoritative file. Cheap (one stat per call; a json read only when
        the mtime changed); safe to call on every poll.
        """
        try:
            mtime = os.path.getmtime(MEMORY_FILE)
        except OSError:
            return
        if mtime <= self._risk_file_mtime:
            return
        try:
            with open(MEMORY_FILE, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        now_ms = int(time.time() * 1000)
        with self._lock:
            # Equity + rolling peak trail (drawdown gate inputs).
            try:
                self._equity = float(data.get("equity", 0) or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                self._peak_equity = float(data.get("peakEquity", 0) or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                self._peak_equity_basis_flow = float(
                    data.get("peakEquityBasisFlow", 0.0) or 0.0)
            except (TypeError, ValueError):
                self._peak_equity_basis_flow = 0.0
            trail = self._parse_equity_trail(data.get("equityTrail"))
            self._equity_trail = deque(trail[-4320:])
            self._dd_frozen_since_ms = int(data.get("ddFrozenSinceMs", 0) or 0)
            self._dd_last_baseline_ms = int(data.get("ddLastBaselineMs", 0) or 0)
            self._dd_basis_migrated = bool(data.get("ddBasisMigrated", False))
            # Circuit breakers (same cross-process staleness issue).
            self._global_halt_until_ms = int(data.get("globalHaltUntilMs", 0) or 0)
            if self._global_halt_until_ms < now_ms:
                self._global_halt_until_ms = 0
            self._coin_circuit = {
                str(k): int(v) for k, v in (data.get("coinCircuit") or {}).items()
                if int(v) > now_ms
            }
        # Record the mtime only after a successful read so a torn/transient
        # read is retried on the next poll.
        self._risk_file_mtime = mtime

    def _write_atomic(self, data: dict[str, Any]) -> bool:
        """Persist ``data`` to MEMORY_FILE atomically with cross-process lock +
        fsync. Returns True on success.

        Split out of ``flush()`` so the in-process snapshot lock is not held
        across the (potentially slow) disk write and so the cross-process
        flock is always released even on a JSON/IO error.

        R11-B1:
          * Cross-process ``flock`` is acquired before any tmp-file work and
            released in a top-level ``finally`` (so an exception in
            ``json.dump`` cannot leak the kernel lock until process death).
          * The tmp file is ``fsync``'d before ``os.replace`` so a power
            loss between dump and rename cannot leave a zero-length
            ``.agent-memory.json``.
          * The directory entry is also ``fsync``'d so the rename is
            durable on the journal.
        """
        # R11-B1 guarantees (flock before tmp work, fsync before replace,
        # fsync dir after replace, lock released even on error) now live in
        # agents.atomic_io.locked_write_json_atomic; MEMORY_LOCK_FILE is
        # MEMORY_FILE + ".lock", matching the helper's lock convention.
        try:
            # Audit 2026-09-06 (E5, P2): preserve the current live file as the
            # single-generation .bak before it is replaced (best-effort).
            _rotate_memory_bak_pre()
            atomic_io.locked_write_json_atomic(MEMORY_FILE, data, indent=2, fsync=True)
            # First-ever flush has no predecessor to rotate — seed .bak from
            # the fresh live file so a recovery copy always exists.
            _seed_memory_bak_post()
            return True
        except Exception as e:
            logger.error(f"[memory] save failed: {e}")
            return False

    def _observe_flush_metric(self, t0: float, force: bool, ok: bool) -> None:
        """Best-effort: record flush latency / failure counter.

        P3-1: latency only counts the actual write path, not gated skips.
        Failures increment MEMORY_FLUSH_ERRORS so a flapping disk can be
        alerted on (R11-F1). R11-B1: isolated so it never masks I/O errors
        raised by the write itself.
        """
        try:
            from hermes_trader import metrics
            metrics.MEMORY_FLUSH_DURATION.labels(
                force=str(force).lower(),
                outcome="ok" if ok else "failed",
            ).observe(max(0.0, time.monotonic() - t0))
            if not ok:
                metrics.MEMORY_FLUSH_ERRORS.inc()
        except Exception:
            pass

    def flush(self, force: bool = False) -> None:
        """Save current state to disk.

        GUARD: never flush from an un-hydrated singleton. A process that imports
        memory but didn't call load() (a test, the dashboard server, an MCP tool)
        has empty in-memory state; flushing it would TRUNCATE the live
        .agent-memory.json over good data (observed 2026-06-15: a pytest run wiped
        92 trades + the day's SOD baseline, forcing a SOD re-baseline on restart).
        Only the loaded owner (the trading loop) may persist.

        P1-6: non-forced flushes are cheap when nothing changed (``_dirty``) and
        coalesced within FLUSH_THROTTLE_S so a burst of cooldown/circuit
        mutations no longer triggers one full json.dump+replace each. Critical
        writes — realized closes and the post-hydration rebuild — pass
        ``force=True`` to bypass both gates and persist immediately.

        R11-B1: the in-process snapshot lock is released before disk I/O so
        a slow disk doesn't stall the trading loop; cross-process flock +
        fsync live in ``_write_atomic`` and are ALWAYS released in finally.
        """
        if not self._initialized:
            logger.debug("[memory] flush skipped — singleton not hydrated (load() not called)")
            return
        # Gate BEFORE taking the write path: clean store or a burst inside the
        # throttle window costs nothing. _dirty/_last_flush_ts are only ever
        # mutated under self._lock, so this unlocked read is advisory at worst.
        if not force:
            if not self._dirty:
                return
            # R13-B11: throttle now resolves through memory_quality.flush_throttle_s
            # (legacy HERMES_MEMORY_FLUSH_THROTTLE_S env still wins); FLUSH_THROTTLE_S
            # remains as the helper's literal fallback symbol.
            if (time.monotonic() - self._last_flush_ts) < _memory_quality_params()["flush_throttle_s"]:
                return
        # P3-1: time the actual write path only — gated skips are not flushes.
        _t0 = time.monotonic()
        # Build the snapshot UNDER the in-process lock (P0-C): the dict/list
        # contents are read here while other threads may be appending to them,
        # and json.dump iterates the same objects during the write below.
        # Holding the lock across snapshot + write also makes
        # mutate-then-flush atomic, so a concurrent mutator can't land between
        # the snapshot and the replace (lost update). flock still serializes
        # cross-process writers (dashboard/MCP/backtest); it does not protect
        # against in-process threads racing the snapshot.
        # R11-B1: keep the snapshot lock tight — release BEFORE the (potentially
        # slow) disk write. _write_atomic operates on the immutable dict we
        # built here and uses its own cross-process lock.
        with self._lock:
            if not force and not self._dirty:
                # Re-check under the lock: a racing flush may have persisted it.
                return
            data = {
                "perceptions": list(self._perceptions),
                "analyses": list(self._analyses),
                "trades": list(self._trades),
                "closes": list(self._closes),
                "entryCtx": dict(self._entry_ctx),
                "cooldowns": [{"coin": coin, "expires": exp} for coin, exp in self._cooldowns.items()],
                "equity": self._equity,
                "dailyPnl": self._daily_pnl,
                # P2-1: persist the intraday PnL high-water mark so the
                # give-back breaker survives a restart within the same UTC day.
                "peakDailyPnl": self._peak_daily_pnl,
                "startOfDayEquity": self._start_of_day_equity,
                "dayStartTs": self._day_start_ts,
                # P0-1: cumulative external capital flow (display-only).
                "cumContribFolded": self._contrib_folded_total,
                "cumContribToday": self._contrib_today,
                "peakEquity": self._peak_equity,  # B-F7 all-time equity HWM
                # CS-D: the flow basis _peak_equity was recorded on; re-based at
                # read time (peak + cur_flow − basis) so deposits/transfers
                # cannot inflate the peak or fake a drawdown.
                "peakEquityBasisFlow": self._peak_equity_basis_flow,
                # CS-D: trail rows are (ts, raw_equity, cum_flow) triples; the
                # third element is absent only on pre-CS-D files (parsed as
                # None → one-shot legacy rebase on the first accepted tick).
                "equityTrail": [[ts, eq, flow]
                                for ts, eq, flow in self._equity_trail],
                "ddFrozenSinceMs": int(self._dd_frozen_since_ms or 0),
                "ddLastBaselineMs": int(self._dd_last_baseline_ms or 0),
                # CS-D: one-shot legacy-trail migration flag (survives restarts
                # so the rigid rebase runs exactly once across the upgrade).
                "ddBasisMigrated": bool(self._dd_basis_migrated),
                "openPositions": list(self._open_positions),
                "coinCircuit": dict(self._coin_circuit),
                "globalHaltUntilMs": int(self._global_halt_until_ms or 0),
                "consecutiveLosses": dict(self._consecutive_losses),
            }
            # Clear the dirty gate NOW so a concurrent mutator can re-dirty
            # for the next flush window without us having to re-enter the
            # lock. _write_atomic is best-effort: on failure the caller will
            # leave _dirty set on the next mutation.
            self._dirty = False
        # Disk write — outside the in-process lock (R11-B1).
        ok = self._write_atomic(data)
        if ok:
            self._last_flush_ts = time.monotonic()
        self._observe_flush_metric(_t0, force, ok)

    # ── Write operations ────────────────────────────────────────────────────

    def _retention_sweep_nolock(self) -> None:
        """R9/P3-4: apply age eviction + count cap to perceptions/analyses/trades.

        Called under ``self._lock`` from the record_* writes. Count caps use a
        cheap length check; the age sweep only runs for lists with a non-zero
        max_age_days and rebuilds the list in place.
        """
        limits = _memory_limits()
        ages = _memory_max_age_days()
        now_ms = time.time() * 1000.0
        for list_key, cap_key in (("perceptions", "perceptions"),
                                  ("analyses", "analyses"),
                                  ("trades", "trades")):
            records = getattr(self, f"_{list_key}")
            max_age = ages.get(list_key, 0.0)
            if max_age and max_age > 0:
                cutoff_ms = now_ms - max_age * 86400.0 * 1000.0
                records = _evict_aged(records, list_key, cutoff_ms)
                setattr(self, f"_{list_key}", records)
            cap = limits[cap_key]
            if len(records) > cap:
                del records[:len(records) - cap]

    def record_perception(self, p: dict[str, Any]) -> None:
        with self._lock:
            self._perceptions.append(p)
            self._retention_sweep_nolock()
            self._dirty = True

    def record_analysis(self, a: dict[str, Any]) -> None:
        with self._lock:
            self._analyses.append(a)
            self._retention_sweep_nolock()
            self._dirty = True

    def record_trade(self, t: dict[str, Any]) -> None:
        with self._lock:
            self._trades.append(t)
            self._retention_sweep_nolock()
            self._dirty = True
        # Authoritative event feed: emit an "order" event so rebuild can
        # reconstruct trades on a fresh/corrupt JSON cache (PURR record-loss
        # fix 2026-08-22). Best-effort, never blocks trading.
        try:
            from hermes_trader import event_log
            ok = event_log.append("order", payload=t,
                                  trace_id=str(t.get("trace_id") or
                                               t.get("analysis_id") or ""))
            if not ok:
                # P1-5: the authoritative order event was NOT durably written.
                # event_log.append already logged a warning at the I/O layer;
                # escalate to error so a dead audit feed triggers an alert
                # instead of silently losing the trade's reconstruct record.
                logger.error("[memory] event_log.append('order') returned False "
                             "for coin=%s — audit feed may be down",
                             t.get("coin"))
        except Exception as e:
            logger.error("[memory] event_log.append('order') raised %s: %s "
                         "(coin=%s) — trade not recorded in events.jsonl",
                         type(e).__name__, e, t.get("coin"))
        self.flush()

    def record_entry_context(self, coin: str, side: str, ctx: dict[str, Any]) -> None:
        """Stash entry time + signal snapshot for an opening position, so its close
        can carry it into the outcome store (forward signal backtest)."""
        with self._lock:
            self._entry_ctx[f"{coin}_{side}"] = ctx
            self._dirty = True
        self.flush()

    def pop_entry_context(self, coin: str, side: str) -> dict[str, Any]:
        """Retrieve + clear the entry context for a closing position (or {})."""
        with self._lock:
            ctx = self._entry_ctx.pop(f"{coin}_{side}", {})
            if ctx:
                self._dirty = True
        if ctx:
            self.flush()
        return ctx

    def record_close(self, c: dict[str, Any]) -> None:
        """Append a realized exit to the outcome store and persist.

        This is THE source of realized PnL — previously outcomes only existed in
        log text (trades[].pnl was never populated), so win-rate / payoff / RoR /
        Phase-3 stats had nothing to read. Called from close_position_market so a
        single chokepoint covers DSL, AI-close, and kill-switch exits.
        Expected keys: coin, side, entry_px, exit_px, spot_pct, realized_pnl_pct
        (leveraged, net fees), realized_pnl_usd (net USD), leverage, closed_at.
        """
        with self._lock:
            # P1-6: keep the incremental stats current under the same lock as
            # the list mutation (lazy (re)build after hydration, one-time day-
            # roll rescan, O(1) fold + bounded-list eviction).
            self._ensure_close_stats_nolock()
            self._recheck_day_stats_nolock()
            self._closes.append(c)
            evicted: Optional[dict[str, Any]] = None
            if len(self._closes) > _memory_limits()["closes"]:
                evicted = self._closes.pop(0)
            self._accumulate_close_nolock(c)
            if evicted is not None:
                self._evict_close_stats_nolock(evicted)
            self._dirty = True
        # Authoritative event feed: emit a "close" event so rebuild can
        # reconstruct realized outcomes on a fresh/corrupt JSON cache (PURR
        # record-loss fix 2026-08-22). Best-effort, never blocks trading.
        try:
            from hermes_trader import event_log
            ok = event_log.append("close", payload=c,
                                  trace_id=str(c.get("trace_id") or ""))
            if not ok:
                # P1-5: same escalation as record_trade — a lost close event
                # breaks realized-PnL rebuild and post-trade reconciliation.
                logger.error("[memory] event_log.append('close') returned False "
                             "for coin=%s — audit feed may be down",
                             c.get("coin"))
        except Exception as e:
            logger.error("[memory] event_log.append('close') raised %s: %s "
                         "(coin=%s) — close not recorded in events.jsonl",
                         type(e).__name__, e, c.get("coin"))
        # Realized close = critical: bypass the dirty/throttle gates and
        # persist immediately (P1-6).
        self.flush(force=True)

    def update_equity(self, eq: float) -> None:
        with self._lock:
            self._equity = eq
            self._dirty = True

    def track_daily_pnl(self, current_equity: float, net_contributions: float = 0.0) -> None:
        """Reset baseline at UTC midnight so dailyPnl reflects today's gains.

        `net_contributions` is the cumulative USDC flow into the tradeable
        equity pool since `_day_start_ts` (positive = money came in,
        negative = money left). Subtracting it makes daily PnL invariant
        to deposits, withdrawals, and spot↔perp transfers — otherwise a
        $50 spot→perp transfer looks like $50 of trading profit. Callers
        that don't have a ledger source should pass 0 (degrades to the
        old behavior).
        """
        from datetime import datetime, timezone
        # ── Partial-dex degraded-read filter ────────────────────────────
        # A flaky per-dex query can drop a whole clearinghouse from the
        # aggregate (observed 2026-06-12 08:06: aggregate momentarily read
        # xyz-only $59.7 vs true $98.7 → dailyPnl printed −$39 and tripped
        # the daily-loss gate; had it landed in the heartbeat instead, the
        # HARD kill-switch would have flattened the whole book on fiction).
        # A real >25% equity move inside 3 minutes is impossible at ~2x
        # gross book without liquidation, so reject fast spikes and keep
        # the prior reading; a SUSTAINED move re-asserts itself after 180s
        # and is then accepted (genuine crash detection delayed ≤3min).
        now_s = time.time()
        prev_eq = getattr(self, "_last_eq_reading", 0.0)
        prev_ts = getattr(self, "_last_eq_reading_ts", 0.0)
        # H10: the original filter blind-rejected ANY >25% equity move within
        # 180s, which masked real flash crashes — the daily-loss kill-switch
        # would see a stale, optimistic reading for up to 3 minutes while the
        # book was actually blowing up. Two corrections keep the false-positive
        # protection for flaky per-dex reads while letting genuine crashes
        # through promptly:
        #   1. A large DOWN move beyond a crash threshold is accepted
        #      immediately (a >40% equity drop at this gross leverage is not a
        #      transient partial-dex artifact — fail-OPEN on real risk).
        #   2. Otherwise, instead of a time-based 180s blackout, require the
        #      implausible reading to RE-CONFIRM once before accepting. A
        #      one-tick partial-dex blip stays rejected; a sustained move
        #      (even a slower crash) is accepted on the very next tick.
        # R13-B11: the four filter knobs resolve through the memory_quality
        # block (legacy HERMES_EQUITY_CRASH_DOWN_PCT env still wins); the
        # literals live in _MEMORY_QUALITY_DEFAULTS as the final fallback.
        _q = _memory_quality_params()
        _IMPLAUSIBLE_PCT = _q["implausible_pct"]
        _CRASH_DOWN_PCT = _q["crash_down_pct"]
        _FILTER_WINDOW_S = _q["filter_window_sec"]
        _RECONFIRM_STREAK = _q["reconfirm_streak"]
        if (prev_eq > 0 and current_equity > 0
                and (now_s - prev_ts) < _FILTER_WINDOW_S):
            move_frac = (current_equity - prev_eq) / prev_eq
            if move_frac <= -_CRASH_DOWN_PCT:
                logger.critical(
                    f"[memory] EQUITY CRASH ${prev_eq:.2f} -> ${current_equity:.2f} "
                    f"({move_frac*100:.1f}%) in {now_s - prev_ts:.0f}s — accepting "
                    f"immediately (exceeds crash threshold; kill-switch MUST see this)"
                )
                # fall through to accept the reading
            elif abs(move_frac) > _IMPLAUSIBLE_PCT:
                streak = getattr(self, "_eq_implausible_streak", 0) + 1
                self._eq_implausible_streak = streak
                if streak < _RECONFIRM_STREAK:
                    logger.error(
                        f"[memory] IMPLAUSIBLE equity swing ${prev_eq:.2f} -> "
                        f"${current_equity:.2f} ({move_frac*100:+.1f}%) in "
                        f"{now_s - prev_ts:.0f}s — suspected partial-dex degraded "
                        f"read; IGNORING this tick (will accept if it re-confirms). "
                        f"[streak={streak}]"
                    )
                    return
                logger.warning(
                    f"[memory] implausible equity move ${prev_eq:.2f} -> "
                    f"${current_equity:.2f} re-confirmed across {streak} ticks — "
                    f"accepting as a sustained move (no longer treating as blip)."
                )
                # fall through to accept
        # Reset streak whenever we accept a reading. The day-roll baseline and
        # PnL writes below are serialized with flush snapshots (P0-C).
        with self._lock:
            self._eq_implausible_streak = 0
            self._last_eq_reading = current_equity
            self._last_eq_reading_ts = now_s

            today_utc = int(datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp())
            # P0-1: a genuine UTC day roll (a prior recorded day ended) folds
            # yesterday's running external-flow total into the cumulative
            # folded total so the dashboard's cash-flow-neutral curve stays
            # correct across midnight. Display-only — risk gates read raw
            # equity and never touch these fields. The `_start_of_day_equity
            # == 0` reset case is intentionally NOT folded: on a fresh/restore
            # load folded_total already holds the persisted value and today's
            # running total is 0, so there is nothing to fold.
            day_rolled = bool(self._day_start_ts) and self._day_start_ts < today_utc
            if day_rolled or self._start_of_day_equity == 0:
                if day_rolled:
                    self._contrib_folded_total += self._contrib_today
                # Re-baseline at day roll or after a memory reset. If there were
                # already contributions today (e.g. operator transferred USDC
                # spot→perp before starting the bot), the baseline must exclude
                # them so the first PnL reading doesn't show -contributions as a
                # loss: daily_pnl = equity_now - baseline - contributions = 0.
                self._start_of_day_equity = current_equity - net_contributions
                self._day_start_ts = today_utc
                self._daily_pnl = 0
                self._peak_daily_pnl = 0  # reset high-water mark at the UTC day roll
                self._peak_daily_realized_pnl = 0  # (supplemental audit 2026-09-02) reset realized peak too
                # A new trading day clears the consecutive-loss streak and any
                # lingering per-coin circuit (global halt intentionally survives
                # until its own wall-clock expiry).
                self._consecutive_losses = {}
            else:
                self._daily_pnl = current_equity - self._start_of_day_equity - net_contributions
            # P0-1: today's net external flow is re-fetched from the ledger each
            # tick (authoritative since-SOD window); folded_total covers prior
            # UTC days. Their sum is the cumulative flow the dashboard subtracts
            # to render a cash-flow-neutral curve.
            self._contrib_today = float(net_contributions or 0.0)
            # Track the day's peak PnL so a give-back breaker can lock in green days.
            self._peak_daily_pnl = max(self._peak_daily_pnl, self._daily_pnl)
            # B-F7: all-time equity high-water mark for the drawdown gate. The
            # implausible-read filter above already ran, so the values reaching
            # here are accepted (sustained) readings — a one-tick partial-dex
            # blip can neither inflate the peak nor fake a crash below it.
            #
            # CS-D (2026-09-08) contribution-invariant drawdown basis: raw
            # equity conflates trading PnL with EXTERNAL cash flow (spot↔perp
            # transfers, deposits, withdrawals). A $30 spot→perp transfer used
            # to inflate the peak (dd% understated, gate too loose); a
            # perp→spot transfer used to fake a crash (gate froze with zero
            # trading loss — the −58.9% / 111-block latch of 2026-09-01). The
            # peak/trail are now stored on a cash-flow-normalised basis: each
            # sample carries the cumulative net external flow as of that tick,
            # and rolling_peak_equity() re-bases every sample onto TODAY's flow
            # before taking the max, so transfers slide both peak and equity
            # together while a genuine trading loss still shows a real %.
            cum_flow = float(self._contrib_folded_total + self._contrib_today)
            # One-shot legacy migration: samples persisted before CS-D are bare
            # (ts, equity) tuples with no flow annotation — they sit on the
            # flow basis of their day, unknown now. Rebase the whole trail and
            # the all-time peak onto the CURRENT flow basis exactly once, then
            # every new sample is natively annotated. Runs even when the trail
            # is empty: a pre-CS-D file can still carry a bare all-time peak
            # whose basis is unknown, and that peak must be tagged to the
            # current basis too. Harmless when cum_flow is ~0 (fresh / no
            # transfers): the rigid shift is a no-op and the log notes it.
            if not self._dd_basis_migrated:
                self._rebase_legacy_trail_to_flow_basis_nolock(cum_flow)
            self._dd_basis_migrated = True
            # All-time peak on a NAMED flow basis: _peak_equity is raw equity as
            # of the high-water tick and _peak_equity_basis_flow is the cum_flow
            # that tick sat on. Re-based to today's basis at read time so a
            # deposit since the peak cannot understate the drawdown.
            cand_peak = float(current_equity)
            if cand_peak >= self._peak_equity:
                self._peak_equity = cand_peak
                self._peak_equity_basis_flow = cum_flow
            self._equity = current_equity
            # Drawdown-gate rolling peak (fix 2026-09-03): append the accepted
            # equity tick, then age-prune the trail. Samples are appended at
            # most one per ~600s so the deque stays tiny (≤ ~4,320 for 30d)
            # while still giving the rolling-window peak daily resolution.
            # CS-D: tuples are (ts, raw_equity, cum_flow) so the peak can be
            # re-normalised onto the current cash-flow basis at read time.
            self._append_equity_trail_nolock(now_s, current_equity, cum_flow)
            # P1-6: every accepted equity tick mutates dailyPnl/peak/equity;
            # coalesced onto the trading loop's periodic (throttled) flush.
            self._dirty = True

    def peak_daily_pnl(self) -> float:
        """Intraday high-water mark of daily PnL (resets at UTC midnight)."""
        return self._peak_daily_pnl

    def cumulative_external_contributions(self) -> float:
        """P0-1: cumulative net EXTERNAL capital flow into the tradeable pool
        since the first observed ledger (deposit − withdrawal, incl. cross-pool
        transfers in). Display-only input for the dashboard's cash-flow-neutral
        equity curve — ``equity_adjusted = equity − cumulative_flow`` so a
        deposit does not read as trading profit. Risk gates / sizing MUST NOT
        use this; they read raw equity.
        """
        with self._lock:
            return self._contrib_folded_total + self._contrib_today

    def daily_realized_pnl(self) -> float:
        """(supplemental audit 2026-09-02) Today's total REALIZED PnL (USD),
        summed across coins from the _closes ledger — excludes unrealized
        float. Recomputed on demand off the per-coin running total (which is
        rebuilt from persisted closes on a day-roll/restart), so it is
        restart-safe."""
        with self._lock:
            self._ensure_close_stats_nolock()
            self._recheck_day_stats_nolock()
            return float(sum(self._day_realized_usd.values()))

    def peak_daily_realized_pnl(self) -> float:
        """(supplemental audit 2026-09-02) Intraday high-water mark of REALIZED
        daily PnL only. Updated on demand whenever gates read it: the give-back
        breaker arms off this rather than the mark-to-market peak, so a paper
        float spike that never gets locked in can't latch the gate."""
        with self._lock:
            self._ensure_close_stats_nolock()
            self._recheck_day_stats_nolock()
            realized = float(sum(self._day_realized_usd.values()))
            if realized > self._peak_daily_realized_pnl:
                self._peak_daily_realized_pnl = realized
                self._dirty = True
            return float(self._peak_daily_realized_pnl)

    def peak_equity(self) -> float:
        """All-time high-water mark of equity (B-F7 drawdown gate baseline).

        Updated only on accepted equity ticks; 0.0 until the first tick, in
        which case the drawdown gate treats the book as having no reference
        peak and passes (fail-open on missing reference, fail-closed once a
        real peak exists)."""
        with self._lock:
            return float(self._peak_equity)

    # ── Drawdown-gate recovery (fix 2026-09-03) ────────────────────────────
    # The drawdown gate previously measured against the ALL-TIME peak, which
    # never recovers: after a permanent equity drop (realized loss, withdraw,
    # balance rebase) dd stays forever above threshold and the gate latched
    # with zero new entries and no recovery path. The gate now measures
    # against a rolling peak over a configurable window, re-baselines after a
    # cooldown, and exposes its freeze state to the dashboard.

    _EQUITY_TRAIL_MIN_SPACING_S = 600.0   # ≤ one sample per ~10 scan minutes
    _EQUITY_TRAIL_MAX_AGE_S = 45 * 86400  # retain ~45 days (window is ≤ this)

    @staticmethod
    def _parse_equity_trail(rows: Any) -> list[tuple[float, float, float]]:
        """Parse the persisted equity trail into (ts_s, raw_equity, cum_flow)
        triples. CS-D (2026-09-08): rows written before the contribution-
        invariant basis are bare (ts, equity) pairs; their flow is unknown, so
        cum_flow is left as None and the one-shot legacy rebase annotates them
        on the first accepted tick. Rows written by CS-D+ carry cum_flow as the
        third element."""
        out: list[tuple[float, float, float]] = []
        if isinstance(rows, list):
            for row in rows:
                try:
                    ts_s = float(row[0])
                    eq = float(row[1])
                    if ts_s <= 0 or eq <= 0:
                        continue
                    flow = float(row[2]) if len(row) >= 3 and row[2] is not None else None
                    out.append((ts_s, eq, flow))
                except (TypeError, ValueError, IndexError):
                    continue
        out.sort(key=lambda r: r[0])
        return out

    def _rebase_legacy_trail_to_flow_basis_nolock(self, cur_cum_flow: float) -> None:
        """One-shot CS-D migration (call under lock): rewrite every bare legacy
        (ts, equity, flow=None) sample onto the CURRENT cumulative-flow basis.

        Legacy samples were recorded on whatever flow basis held at their time;
        that per-sample flow is unknown, so we attribute ALL external flow seen
        so far (``cur_cum_flow``) to the period before the migration tick and
        shift each legacy sample by the SAME constant. This slides the entire
        pre-migration trail rigidly onto today's basis: the post-migration peak
        vs current-equity gap then reflects only TRADING PnL (transfers cancel),
        while within the legacy stretch the rigid shift preserves every dd%
        (peak and equity move together). New samples after this point are
        natively flow-annotated, so the basis only tightens from here."""
        rebased = deque()
        for ts_s, eq, flow in self._equity_trail:
            f = float(cur_cum_flow) if flow is None else float(flow)
            rebased.append((ts_s, float(eq), f))
        self._equity_trail = rebased
        # The all-time peak is a bare raw-equity number on an unknown flow
        # basis. Like the trail, rigidly re-base it onto the current basis:
        # record the raw peak value but tag its flow basis as cur_cum_flow, so
        # the read-time re-base (peak + cur − basis) reads it raw on the very
        # next tick (cur == basis) and stays consistent with the re-based trail.
        self._peak_equity_basis_flow = float(cur_cum_flow)
        logger.warning(
            "[memory] CS-D one-shot drawdown-basis migration: %d legacy trail "
            "samples re-based onto current cumulative-flow basis (cum_flow=%.2f)",
            len(rebased), float(cur_cum_flow))

    def _append_equity_trail_nolock(self, now_s: float, equity: float,
                                    cum_flow: float = 0.0) -> None:
        """Append an accepted equity tick to the rolling trail (call under lock).

        Samples land at most once per _EQUITY_TRAIL_MIN_SPACING_S; a fresh
        accepted tick simply refreshes the latest value, so the window's peak
        always reflects the newest reading. Age-prunes from the left (trail is
        time-ordered) and caps length so the persisted file stays small. CS-D:
        each sample is (ts, raw_equity, cum_flow_at_tick)."""
        triple = (now_s, float(equity), float(cum_flow or 0.0))
        if self._equity_trail and (now_s - self._equity_trail[-1][0]) < self._EQUITY_TRAIL_MIN_SPACING_S:
            self._equity_trail[-1] = triple
        else:
            self._equity_trail.append(triple)
        cutoff = now_s - self._EQUITY_TRAIL_MAX_AGE_S
        while self._equity_trail and self._equity_trail[0][0] < cutoff:
            self._equity_trail.popleft()
        while len(self._equity_trail) > 4320:
            self._equity_trail.popleft()

    @staticmethod
    def _rebased_peak_nolock(trail: "Deque[tuple[float, float, float]]",
                             cutoff: float, cur_cum_flow: float) -> float:
        """CS-D: highest equity over the window, re-based onto TODAY's cumulative
        -flow basis so external transfers cannot inflate/fake the peak.

        Sample i on flow basis f_i with raw equity e_i represents trading
        equity ``e_i − f_i``; to compare like-for-like with the CURRENT book
        (whose basis is cur_cum_flow) we re-base it to ``e_i − f_i + cur_flow``.
        A transfer (Δ in BOTH equity and cum_flow) slides peak and current
        equity together → dd% unchanged by cash movement; only a genuine trading
        loss moves the gap. Samples with unknown flow (pre-migration, already
        rigidly re-based at migration) carry f == cur_cum_flow and are read
        raw."""
        best = 0.0
        for ts_s, eq, flow in trail:
            if ts_s < cutoff:
                continue
            f = float(flow) if flow is not None else float(cur_cum_flow)
            rebased = float(eq) - f + float(cur_cum_flow)
            if rebased > best:
                best = rebased
        return float(best)

    def _current_cum_flow_nolock(self) -> float:
        return float(self._contrib_folded_total + self._contrib_today)

    def rolling_peak_equity(self, window_days: float) -> float:
        """High-water mark of equity over the trailing ``window_days`` days, on
        the CS-D cash-flow-normalised basis (transfers cannot inflate the peak
        or fake a drawdown — see _rebased_peak_nolock).

        Falls back to the all-time peak when the trail is empty/has no samples
        inside the window (cold start / pre-upgrade memory) so the gate never
        silently disarms. window_days <= 0 means "use the all-time peak"
        (legacy behavior)."""
        with self._lock:
            cur_flow = self._current_cum_flow_nolock()
            if window_days <= 0:
                # Re-base the all-time peak onto today's basis as well so a
                # net inflow since the peak doesn't understate the drawdown.
                return float(max(0.0, self._peak_equity + cur_flow
                                 - self._peak_equity_basis_flow))
            cutoff = time.time() - float(window_days) * 86400.0
            peak = self._rebased_peak_nolock(self._equity_trail, cutoff, cur_flow)
            if peak > 0:
                return peak
            # Empty window → fall back to the all-time peak on today's basis.
            return float(max(0.0, self._peak_equity + cur_flow
                             - self._peak_equity_basis_flow))

    def mark_drawdown_frozen(self) -> tuple[int, bool]:
        """Stamp the drawdown-freeze start on first block (call when gate
        trips). Returns ``(epoch_ms_freeze_start, newly_frozen)`` where
        ``newly_frozen`` is True only on the FIRST stamp of this freeze
        episode (cleared on recovery) — callers emit the freeze audit event
        exactly once per episode. Idempotent: a repeat call just reports the
        existing stamp with ``newly_frozen=False``."""
        newly = False
        with self._lock:
            if not self._dd_frozen_since_ms:
                self._dd_frozen_since_ms = int(time.time() * 1000)
                self._dirty = True
                newly = True
            since = int(self._dd_frozen_since_ms)
        # Risk-blocking state: persist immediately so a restart cannot erase a
        # freeze stamp (mirrors set_loss_cooldown's force-flush policy).
        self.flush(force=True)
        return since, newly

    def clear_drawdown_freeze(self) -> None:
        """Clear the drawdown freeze bookkeeping on recovery (gate passing)."""
        with self._lock:
            if self._dd_frozen_since_ms:
                self._dd_frozen_since_ms = 0
                self._dirty = True

    def rebase_drawdown_peak(self, new_peak: float, reason: str = "") -> None:
        """Re-arm the drawdown baseline to ``new_peak`` after cooldown / a
        permanent drop, releasing a latched freeze. Also clears the freeze
        stamp AND the rolling trail (reseeded at the new baseline) so the gate
        starts measuring forward from here instead of re-tripping on stale
        highs still inside the window — the next genuine drawdown starts a
        fresh freeze episode.

        CS-D audit: the cleared trail is NOT destroyed silently — its
        span/sample count plus both peak values (re-based onto the current
        flow basis) are recorded as a ``drawdown_peak_rebased`` session event
        so a post-mortem can see what the baseline was reset from."""
        with self._lock:
            old = float(self._peak_equity)
            old_basis = float(self._peak_equity_basis_flow)
            cur_flow = self._current_cum_flow_nolock()
            self._peak_equity = float(new_peak)
            # CS-D: new_peak is raw CURRENT equity (ctx.equity) passed by the
            # gate, so it sits on the current cum_flow basis — tag it as such
            # or the read-time re-base would misread the new baseline.
            self._peak_equity_basis_flow = float(cur_flow)
            self._dd_frozen_since_ms = 0
            self._dd_last_baseline_ms = int(time.time() * 1000)
            # Archive the trail ABOUT to be destroyed (span + sample count;
            # values stay in the events.jsonl audit stream, not in memory).
            archived_samples = len(self._equity_trail)
            archived_oldest_ms = int(self._equity_trail[0][0] * 1000) if self._equity_trail else 0
            archived_newest_ms = int(self._equity_trail[-1][0] * 1000) if self._equity_trail else 0
            # Reset the rolling trail to the accepted new baseline: any older
            # (higher) samples would otherwise keep rolling_peak_equity above
            # threshold and re-freeze on the very next gate pass. CS-D: seed a
            # flow-annotated triple on the current basis.
            self._equity_trail.clear()
            self._equity_trail.append(
                (time.time(), float(new_peak), float(cur_flow)))
            self._dirty = True
            old_rebased = float(max(0.0, old + cur_flow - old_basis))
        self.flush(force=True)
        logger.warning(
            "[risk] drawdown baseline re-armed: peak $%.2f -> $%.2f (%s)",
            old_rebased, float(new_peak), reason or "cooldown recovery")
        # CS-D: audit event for the baseline reset (best-effort; never blocks
        # the gate path). All dollar figures are on the CURRENT flow basis.
        try:
            from hermes_trader import session_log
            session_log.append({
                "event": "drawdown_peak_rebased",
                "ts": int(time.time() * 1000),
                "reason": str(reason or "cooldown recovery")[:200],
                "old_peak_equity": round(old_rebased, 4),
                "new_peak_equity": round(float(new_peak), 4),
                "peak_reset_pct": round(
                    (old_rebased - float(new_peak)) / old_rebased * 100.0, 2)
                    if old_rebased > 0 else 0.0,
                "basis_cum_flow": round(float(cur_flow), 4),
                "archived_trail_samples": int(archived_samples),
                "archived_oldest_ms": archived_oldest_ms,
                "archived_newest_ms": archived_newest_ms,
            })
        except Exception as _le:
            logger.warning("[risk] drawdown rebase audit event failed: %s", _le)

    def drawdown_freeze_status(self, max_drawdown_pct: float,
                               window_days: float, cooldown_hours: float) -> dict[str, Any]:
        """Read-only live drawdown-freeze status for the dashboard card.

        Computes the gate decision against CURRENT memory (same rolling peak
        the gate enforces) so the UI sees the freeze the instant it trips,
        independent of trade attempts. Read-only: never stamps/clears/baselines.
        """
        with self._lock:
            equity = float(self._equity or 0.0)
            cur_flow = self._current_cum_flow_nolock()
            # CS-D: every peak figure must be on the SAME current-flow basis as
            # raw current equity, or a deposit/transfer would move dd% on the
            # dashboard (the gate itself uses rolling_peak_equity(), so mirror
            # that basis here rather than reading bare stored peaks).
            at_peak = float(max(
                0.0, self._peak_equity + cur_flow - self._peak_equity_basis_flow))
            cutoff = time.time() - float(window_days) * 86400.0 if window_days > 0 else 0.0
            if window_days > 0:
                peak = self._rebased_peak_nolock(
                    self._equity_trail, cutoff, cur_flow)
                if peak <= 0:
                    peak = at_peak
            else:
                peak = at_peak
            frozen_since = int(self._dd_frozen_since_ms or 0)
            last_baseline = int(self._dd_last_baseline_ms or 0)
            trail_len = len(self._equity_trail)
        now_ms = int(time.time() * 1000)
        dd_pct = ((peak - equity) / peak * 100.0) if peak > 0 and equity > 0 else 0.0
        frozen = bool(peak > 0 and equity > 0 and dd_pct >= float(max_drawdown_pct))
        cooldown_remaining_min = 0.0
        frozen_for_min = 0.0
        if frozen_since:
            frozen_for_min = max(0.0, (now_ms - frozen_since) / 60_000)
            if cooldown_hours > 0:
                cooldown_remaining_min = max(
                    0.0, float(cooldown_hours) * 60.0 - frozen_for_min)
        return {
            "frozen": frozen,
            "dd_pct": round(dd_pct, 2),
            "threshold_pct": float(max_drawdown_pct),
            "peak_equity": round(peak, 4),
            "all_time_peak_equity": round(at_peak, 4),
            "equity": round(equity, 4),
            "window_days": float(window_days),
            "frozen_since_ms": frozen_since if frozen else 0,
            "frozen_for_min": round(frozen_for_min, 1),
            "cooldown_hours": float(cooldown_hours),
            "cooldown_remaining_min": round(cooldown_remaining_min, 1),
            "last_baseline_ms": last_baseline,
            "trail_samples": trail_len,
        }

    def last_equity_reading(self) -> tuple[float, float]:
        """Most recent ACCEPTED equity reading and its epoch-seconds timestamp.

        Returns ``(0.0, 0.0)`` before the first tick. The heartbeat uses this as
        the previous-tick baseline for its phantom-crash sanity check (a reading
        is only comparable against the last value the quality gate accepted)."""
        with self._lock:
            return float(getattr(self, "_last_eq_reading", 0.0) or 0.0), \
                float(getattr(self, "_last_eq_reading_ts", 0.0) or 0.0)

    # ── Loss cooldown (anti-revenge re-entry) ───────────────────────────────
    # Backed by the persisted `_cooldowns` dict (coin -> expires_ms), which was
    # serialized but never written/read until 2026-06-11 — wired up after TON
    # was churned 3x in one day (-1.4%, -0.9%, -6.5% ROE): the AI re-bought the
    # same falling name as soon as the standard 60min cooldown expired.

    def set_loss_cooldown(self, coin: str, until_ms: int) -> None:
        """Block re-entry on `coin` until `until_ms` (epoch ms)."""
        with self._lock:
            self._cooldowns[coin] = int(until_ms)
            self._dirty = True
        # B-M12 (deep audit 2026-08-28): risk-blocking state must hit disk
        # immediately — a crash inside the flush-throttle window would lose the
        # cooldown and let the anti-revenge block vanish on restart.
        self.flush(force=True)

    def loss_cooldown_remaining_min(self, coin: str) -> float:
        """Minutes left on `coin`'s loss cooldown (0 when expired/absent)."""
        with self._lock:
            exp = self._cooldowns.get(coin)
            if not exp:
                return 0.0
            remaining = (int(exp) - int(time.time() * 1000)) / 60_000
            if remaining <= 0:
                self._cooldowns.pop(coin, None)
                self._dirty = True
                return 0.0
            return remaining

    # ── Tiered circuit breakers (sizing/risk-overhaul 2026-08-26) ──────────
    # Two independent halt levels above the legacy per-close loss cooldown:
    #   1. single-coin: a per-trade realized loss beyond a spot-% threshold
    #      blocks re-entry on that one coin for a short window.
    #   2. global: a daily cumulative loss beyond an equity-% threshold halts
    #      ALL new entries for a longer window.
    # Setters persist; remaining-min accessors lazily purge expired entries.

    def set_coin_circuit(self, coin: str, until_ms: int) -> None:
        with self._lock:
            self._coin_circuit[coin] = int(until_ms)
            self._dirty = True
        # B-M12: force flush — a per-coin breaker must survive a crash within
        # the throttle window, otherwise the coin can be re-opened on restart.
        self.flush(force=True)

    def coin_circuit_remaining_min(self, coin: str) -> float:
        with self._lock:
            exp = self._coin_circuit.get(coin)
            if not exp:
                return 0.0
            remaining = (int(exp) - int(time.time() * 1000)) / 60_000
            if remaining <= 0:
                self._coin_circuit.pop(coin, None)
                self._dirty = True
                return 0.0
            return remaining

    def set_global_halt(self, until_ms: int) -> None:
        with self._lock:
            self._global_halt_until_ms = int(until_ms)
            self._dirty = True
        # B-M12: force flush — the global halt blocks ALL entries; losing it to
        # a throttled-write crash would resume trading through a daily-loss halt.
        self.flush(force=True)

    def global_halt_remaining_min(self) -> float:
        with self._lock:
            exp = int(self._global_halt_until_ms or 0)
            if not exp:
                return 0.0
            remaining = (exp - int(time.time() * 1000)) / 60_000
            if remaining <= 0:
                self._global_halt_until_ms = 0
                self._dirty = True
                return 0.0
            return remaining

    def circuit_snapshot(self) -> dict[str, Any]:
        """Read-only (non-mutating) view of the tiered breaker state for metrics.

        Unlike the ``*_remaining_min`` accessors this never purges expired
        entries — a /metrics scrape must not mutate trading state — so expired
        coins are simply excluded from the armed count by comparing timestamps.
        Returns ``{"armed_coins": int, "global_halt": bool}``.
        """
        # Cross-process freshness (fix 2026-09-03): the API server is a
        # separate process from the trading loop; pull the loop's latest
        # risk state from disk (read-only) before reporting.
        self.refresh_risk_state_from_disk()
        now_ms = int(time.time() * 1000)
        with self._lock:
            armed = sum(1 for exp in self._coin_circuit.values() if int(exp or 0) > now_ms)
            halted = int(self._global_halt_until_ms or 0) > now_ms
        return {"armed_coins": armed, "global_halt": bool(halted)}

    def risk_status_snapshot(self) -> dict[str, Any]:
        """O-4 (audit 2026-08-31): richer read-only breaker view for the
        dashboard's risk-status card. Like ``circuit_snapshot`` this never
        mutates (no purge) — expired entries are excluded by timestamp, not
        deleted — so a read-only scrape can never alter trading state.

        Returns ``global_halt`` armed flag + remaining minutes, and the map of
        currently-armed per-coin circuits with their remaining minutes, so the
        UI can show WHICH coins are gated and for how long (not just a count).
        """
        # Cross-process freshness (fix 2026-09-03): the API server is a
        # separate process from the trading loop; pull the loop's latest
        # risk state (equity trail, drawdown freeze, halts) from disk
        # read-only before reporting so the dashboard never shows a stale
        # startup snapshot.
        self.refresh_risk_state_from_disk()
        now_ms = int(time.time() * 1000)
        with self._lock:
            g_exp = int(self._global_halt_until_ms or 0)
            global_halt = g_exp > now_ms
            global_remaining_min = max(0.0, (g_exp - now_ms) / 60_000) if global_halt else 0.0
            coin_circuits = {
                str(coin): round(max(0.0, (int(exp) - now_ms) / 60_000), 1)
                for coin, exp in self._coin_circuit.items()
                if int(exp or 0) > now_ms
            }
        out = {
            "global_halt": global_halt,
            "global_halt_remaining_min": round(global_remaining_min, 1),
            "coin_circuits": coin_circuits,
            "armed_coins": len(coin_circuits),
        }
        # Drawdown freeze (fix 2026-09-03): live gate state for the dashboard
        # freeze banner — evaluated read-only against current memory. Config is
        # read lazily inside to keep this layer free of a config_store import.
        try:
            from hermes_trader.agents.config_store import cfg_get
            max_dd = float(cfg_get("circuit_breaker.max_drawdown_pct") or 0.0)
            window = float(cfg_get("circuit_breaker.drawdown_peak_window_days", 14.0) or 0.0)
            cooldown = float(cfg_get("circuit_breaker.drawdown_cooldown_hours", 24.0) or 0.0)
            out["drawdown"] = self.drawdown_freeze_status(max_dd, window, cooldown)
        except Exception:
            out["drawdown"] = None
        return out

    def record_loss_outcome(self, coin: str, realized_pnl_pct: float) -> None:
        """Update the per-coin consecutive-loss streak from a realized close.
        A loss increments (the breaker gate decides whether the count trips);
        any non-loss resets the streak to zero. Called from the close chokepoint
        alongside the legacy loss cooldown."""
        with self._lock:
            if realized_pnl_pct < 0:
                self._consecutive_losses[coin] = int(self._consecutive_losses.get(coin, 0)) + 1
            else:
                self._consecutive_losses[coin] = 0
            self._dirty = True
        self.flush()

    def consecutive_losses(self, coin: str) -> int:
        with self._lock:
            return int(self._consecutive_losses.get(coin, 0))

    # ── Slippage aggregation (dynamic stop compensation) ───────────────────
    # Close rows already capture exit_slip_bps (positive = adverse fill vs
    # mid). We aggregate the recent mean per coin so the sizer and backup SL
    # can widen stops by the observed adverse slip, offsetting gap-through
    # overruns (PURR #6 root cause: backup stop too tight + slip).
    #
    # P1-6: both hot-path aggregations were O(n) scans of up to MAX_CLOSES
    # rows on every research/execution call. They now fold incrementally
    # (O(1) per record_close) over:
    #   _slip_series[coin]  — deque of (closed_at_s, adverse_slip_bps)
    #   _day_realized_usd[coin] — today's realized-USD total
    # The deque already holds only this coin's adverse slips and is bounded
    # at the configured closes limit; the days-window filter is a list comprehension
    # over that small set on the read snapshot. closed_at is NOT monotonic
    # (event replay/restores append out of order), so the window cannot be
    # a deque left-pop — a newer head entry would stop eviction early and
    # keep stale rows. Rebuilt once from _closes after hydration.

    @staticmethod
    def _close_ts_s(c: dict[str, Any]) -> Optional[float]:
        """Normalize a close row's closed_at to epoch seconds (0.0 when
        absent/unparseable). closed_at may arrive as epoch s or ms."""
        ts = c.get("closed_at")
        if not ts:
            return 0.0
        try:
            v = float(ts)
        except (TypeError, ValueError):
            return 0.0
        if v > 1e12:
            v = v / 1000.0
        return v

    def _rebuild_close_stats_nolock(self) -> None:
        """Recompute the incremental stats from _closes (call under lock).

        Runs once after hydration, and whenever _closes was wholesale
        replaced (_rebuild_from_events). Idempotent: fold order is the
        _closes append order."""
        self._slip_series = {}
        self._fee_series = {}
        self._slip_side = {}
        self._hold_side = {}
        self._day_realized_usd = {}
        day_start = self._day_start_ts
        for c in self._closes:
            self._accumulate_close_nolock(c, day_start=day_start)
        self._day_stats_start_ts = day_start
        self._close_stats_built = True

    def _ensure_close_stats_nolock(self) -> None:
        """Lazily build the incremental stats on first use after hydration."""
        if not self._close_stats_built:
            self._rebuild_close_stats_nolock()

    def _accumulate_close_nolock(self, c: dict[str, Any],
                                 day_start: Optional[int] = None) -> None:
        """Fold one close row into the slip deque and the per-coin
        realized-USD daily totals (call under lock)."""
        coin = c.get("coin")
        if not coin:
            return
        ts_s = self._close_ts_s(c)
        slip = c.get("exit_slip_bps")
        if slip is not None:
            try:
                v = float(slip)
            except (TypeError, ValueError):
                v = 0.0
            # Only adverse (positive) slip widens a stop; favorable fills are
            # not something to budget protection for. Closes without
            # exit_slip_bps never entered the old scan either.
            if v > 0:
                dq = self._slip_series.get(coin)
                if dq is None:
                    dq = deque()
                    self._slip_series[coin] = dq
                dq.append((ts_s, v))
                if len(dq) > _memory_limits()["closes"]:
                    dq.popleft()
        # O-8: fold the REALIZED round-trip fee (bps of notional) into the
        # fee deque, but only for closes carrying actual exchange fees
        # (fee_actual=True). In-process DSL/AI closes model fee_usd as
        # 2.5bps×2×notional, so averaging them would just echo the constant
        # the backtest is trying to calibrate — circular. The external
        # stop/backfill paths read the fill's real `fee`; those rows get
        # marked. Backfill rows with no usable notional (entry_px=0) carry
        # fee_actual=False so they never bias the series low.
        if c.get("fee_actual"):
            try:
                fee_usd = float(c.get("fee_usd") or 0.0)
                notional = float(c.get("notional_usd") or 0.0)
            except (TypeError, ValueError):
                fee_usd = notional = 0.0
            if fee_usd > 0 and notional > 0:
                fee_bps = fee_usd / notional * 1e4
                fdq = self._fee_series.get(coin)
                if fdq is None:
                    fdq = deque()
                    self._fee_series[coin] = fdq
                fdq.append((ts_s, fee_bps))
                if len(fdq) > _memory_limits()["closes"]:
                    fdq.popleft()
        # CS-G: direction-differentiated views. Only well-formed long/short
        # rows contribute (normalized to lowercase so legacy disk rows written
        # with "LONG"/"SHORT" still fold in); adverse exit slip stays >0-only
        # (consistent with the shared _slip_series), and hold time must be
        # positive.
        _side = str(c.get("side") or "").lower()
        if _side in ("long", "short"):
            _cap = _memory_limits()["closes"]
            if slip is not None:
                try:
                    _sv = float(slip)
                except (TypeError, ValueError):
                    _sv = 0.0
                if _sv > 0:
                    sdq = self._slip_side.get((coin, _side))
                    if sdq is None:
                        sdq = deque()
                        self._slip_side[(coin, _side)] = sdq
                    sdq.append((ts_s, _sv))
                    if len(sdq) > _cap:
                        sdq.popleft()
            _hold_min = c.get("hold_minutes")
            if _hold_min is not None:
                try:
                    _hv = float(_hold_min) / 60.0
                except (TypeError, ValueError):
                    _hv = 0.0
                if _hv > 0:
                    hdq = self._hold_side.get((coin, _side))
                    if hdq is None:
                        hdq = deque()
                        self._hold_side[(coin, _side)] = hdq
                    hdq.append((ts_s, _hv))
                    if len(hdq) > _cap:
                        hdq.popleft()
        if day_start is None:
            day_start = self._day_start_ts
        # Mirror the old scan: rows without a usable closed_at (0/None) were
        # skipped from the daily total entirely.
        if ts_s and ts_s >= float(day_start):
            pnl = c.get("realized_pnl_usd")
            if pnl is not None:
                try:
                    self._day_realized_usd[coin] = (
                        self._day_realized_usd.get(coin, 0.0) + float(pnl)
                    )
                except (TypeError, ValueError):
                    pass
                # (supplemental audit 2026-09-02) Fold the banked profit into the
                # realized high-water mark AT CLOSE TIME. Sampling the peak only
                # when the gate runs could miss a spike that two gate evaluations
                # straddle (a profit taken then partially given back before the
                # next order attempt). Updating here — the single point every
                # realized total flows through, including _rebuild on restart —
                # captures the true peak and stays restart-safe.
                try:
                    _day_total = float(sum(self._day_realized_usd.values()))
                    if _day_total > self._peak_daily_realized_pnl:
                        self._peak_daily_realized_pnl = _day_total
                        self._dirty = True
                except (TypeError, ValueError):
                    pass

    def _evict_close_stats_nolock(self, c: dict[str, Any]) -> None:
        """Detach stats for a close row evicted from the bounded _closes list
        (call under lock). O(1): deque entries share append order with
        _closes, so the evicted close — the oldest for its coin — sits at the
        head of that coin's deque when it carried adverse slip."""
        coin = c.get("coin")
        if not coin:
            return
        ts_s = self._close_ts_s(c)
        slip = c.get("exit_slip_bps")
        dq = self._slip_series.get(coin)
        if dq:
            head_ts, head_v = dq[0]
            try:
                v = float(slip) if slip is not None else 0.0
            except (TypeError, ValueError):
                v = 0.0
            if head_ts == ts_s and abs(head_v - v) < 1e-9:
                dq.popleft()
        # O-8: drop the fee deque head when the evicted close was its source
        # (same coin + closed_at). Fee entries share the _closes append order.
        fdq = self._fee_series.get(coin)
        if fdq and c.get("fee_actual"):
            head_ts, _head_fb = fdq[0]
            try:
                _fee = float(c.get("fee_usd") or 0.0)
                _not = float(c.get("notional_usd") or 0.0)
                _fb = (_fee / _not * 1e4) if (_fee > 0 and _not > 0) else 0.0
            except (TypeError, ValueError):
                _fb = 0.0
            if head_ts == ts_s and abs(_head_fb - _fb) < 1e-6:
                fdq.popleft()
        # CS-G: detach the per-side slip/hold heads the evicted close fed.
        _eside = str(c.get("side") or "").lower()
        if _eside in ("long", "short"):
            sdq = self._slip_side.get((coin, _eside))
            if sdq:
                _h_ts, _h_v = sdq[0]
                try:
                    _sv = float(slip) if slip is not None else 0.0
                except (TypeError, ValueError):
                    _sv = 0.0
                if _h_ts == ts_s and abs(_h_v - _sv) < 1e-9 and _sv > 0:
                    sdq.popleft()
            hdq = self._hold_side.get((coin, _eside))
            if hdq:
                _h_ts, _h_h = hdq[0]
                try:
                    _hv = float(c.get("hold_minutes")) / 60.0
                except (TypeError, ValueError):
                    _hv = 0.0
                if _h_ts == ts_s and abs(_h_h - _hv) < 1e-9 and _hv > 0:
                    hdq.popleft()
        if ts_s and ts_s >= float(self._day_stats_start_ts):
            pnl = c.get("realized_pnl_usd")
            if pnl is not None:
                try:
                    self._day_realized_usd[coin] = (
                        self._day_realized_usd.get(coin, 0.0) - float(pnl)
                    )
                except (TypeError, ValueError):
                    pass

    def _recheck_day_stats_nolock(self) -> None:
        """Rescan _closes for the current UTC day if the baseline rolled
        after the stats were built (rare path: once per day, O(n))."""
        if not self._close_stats_built:
            return
        if self._day_stats_start_ts == self._day_start_ts:
            return
        day_start = self._day_start_ts
        day_totals: dict[str, float] = {}
        # (supplemental audit 2026-09-02) Rebuild the realized high-water mark
        # on roll as well. track_daily_pnl() resets _peak_daily_realized_pnl at
        # the UTC roll, but a lazy accessor (daily_realized_pnl() /
        # peak_daily_realized_pnl()) can be the FIRST call after midnight —
        # e.g. a manual order before the first heartbeat tick. Without this the
        # stale yesterday peak would still arm the give-back gate and block the
        # new day's first entries. Replaying today's closes in append order and
        # tracking the running max reproduces the exact peak that the
        # _accumulate_close_nolock updates would have produced.
        running_total = 0.0
        rebuilt_peak = 0.0
        for c in self._closes:
            ts_s = self._close_ts_s(c)
            if not ts_s or ts_s < float(day_start):
                continue
            coin = c.get("coin")
            if not coin:
                continue
            pnl = c.get("realized_pnl_usd")
            if pnl is None:
                continue
            try:
                v = float(pnl)
                day_totals[coin] = day_totals.get(coin, 0.0) + v
                running_total += v
                if running_total > rebuilt_peak:
                    rebuilt_peak = running_total
            except (TypeError, ValueError):
                continue
        self._day_realized_usd = day_totals
        self._peak_daily_realized_pnl = float(rebuilt_peak)
        self._day_stats_start_ts = day_start

    def avg_exit_slip_bps(self, coin: str, days: Optional[float] = None,
                          min_samples: Optional[int] = None) -> float:
        """Mean adverse exit slippage in bps over the last `days` for `coin`.
        Returns 0.0 when there are fewer than `min_samples` qualifying closes
        (insufficient history → do not widen on noise). P1-6: scans only the
        coin's bounded adverse-slip deque (≤ the configured closes limit,
        already coin/adverse-filtered) instead of the full _closes list.

        R13-B11: when not passed explicitly, the lookback window and minimum
        sample bar resolve from the memory_quality block
        (slip_window_days=30.0 / slip_min_samples=3 literals as fallback)."""
        _q = _memory_quality_params()
        if days is None:
            days = _q["slip_window_days"]
        if min_samples is None:
            min_samples = _q["slip_min_samples"]
        cutoff = time.time() - days * 86400.0
        with self._lock:
            self._ensure_close_stats_nolock()
            dq = self._slip_series.get(coin)
            # Snapshot under the lock; filter OUTSIDE it. closed_at is NOT
            # monotonic (event replay / restores can append out of order), so
            # the window can't be a deque left-pop — a newer entry at the head
            # would stop the loop early and keep stale entries. The per-coin
            # deque is already filtered to this coin, adverse-only and capped
            # at the configured closes limit, so this stays far cheaper than
            # the old full-_closes scan. Rows without a usable closed_at (ts==0.0)
            # were kept by the old scan as well (never aged out).
            samples = [v for ts, v in (dq or ()) if not ts or ts >= cutoff]
        if len(samples) < min_samples:
            return 0.0
        return sum(samples) / len(samples)

    def avg_round_trip_fee_bps(self, coin: str, days: Optional[float] = None,
                               min_samples: Optional[int] = None) -> float:
        """Mean REALIZED round-trip fee in bps of notional over the last
        `days` for `coin`, derived from close rows that carried actual
        exchange fill fees (fee_actual=True, fee_usd/notional_usd).

        O-8 (supplemental audit 2026-08-30): the backtests hardcode a 5-bps
        round-trip taker fee; this lets them calibrate that constant against
        the venue's real charges. Returns 0.0 when there are fewer than
        `min_samples` qualifying closes (insufficient history → keep the
        backtest's conservative default rather than calibrate on noise).
        Reuses the slip window/sample params (slip_window_days=30.0 /
        slip_min_samples=3) — same rationale, no extra config surface."""
        _q = _memory_quality_params()
        if days is None:
            days = _q["slip_window_days"]
        if min_samples is None:
            min_samples = _q["slip_min_samples"]
        cutoff = time.time() - days * 86400.0
        with self._lock:
            self._ensure_close_stats_nolock()
            dq = self._fee_series.get(coin)
            samples = [v for ts, v in (dq or ()) if not ts or ts >= cutoff]
        if len(samples) < min_samples:
            return 0.0
        return sum(samples) / len(samples)

    @staticmethod
    def _window_mean(dq: Optional[Deque[tuple[float, float]]], cutoff: float,
                     min_samples: int) -> Optional[float]:
        """Mean of a (ts, value) deque inside the window with >= min_samples,
        else None. Rows with no usable ts (0.0) never age out (same rule as
        avg_exit_slip_bps)."""
        if not dq:
            return None
        samples = [v for ts, v in dq if not ts or ts >= cutoff]
        if len(samples) < min_samples:
            return None
        return sum(samples) / len(samples)

    def avg_exit_slip_bps_side(self, coin: str, side: str,
                               days: Optional[float] = None,
                               min_samples: Optional[int] = None,
                               default_bps: float = _DEFAULT_COLD_EXIT_SLIP_BPS
                               ) -> tuple[float, str]:
        """CS-G: direction-differentiated adverse exit slip in bps.

        Degradation chain (never to zero — a missing-data zero would size the
        position as if exits were free): (1) this coin+side mean, (2) the
        shared coin mean via avg_exit_slip_bps (its own insufficient-history
        result is NOT trusted here), (3) a global same-side mean across coins,
        (4) the conservative `default_bps` literal. Returns (bps, source)."""
        _side = str(side or "long").lower()
        if _side not in ("long", "short"):
            _side = "long"
        _q = _memory_quality_params()
        if days is None:
            days = _q["slip_window_days"]
        if min_samples is None:
            min_samples = _q["slip_min_samples"]
        cutoff = time.time() - days * 86400.0
        with self._lock:
            self._ensure_close_stats_nolock()
            m = self._window_mean(self._slip_side.get((coin, _side)),
                                  cutoff, min_samples)
            if m is not None:
                return m, "coin_side"
            m = self._window_mean(self._slip_series.get(coin), cutoff, min_samples)
            if m is not None:
                return m, "coin_shared"
            pooled: list[float] = []
            for (c2, s2), dq in self._slip_side.items():
                if s2 == _side and c2 != coin:
                    pooled.extend(v for ts, v in dq if not ts or ts >= cutoff)
        if len(pooled) >= min_samples:
            return sum(pooled) / len(pooled), "global_side"
        return float(default_bps), "default"

    def avg_hold_hours_side(self, coin: str, side: str,
                            days: Optional[float] = None,
                            min_samples: Optional[int] = None,
                            default_hours: float = _DEFAULT_EXPECTED_HOLD_HOURS
                            ) -> tuple[float, str]:
        """CS-G: mean realized holding time in HOURS for `coin`+`side`, used
        to size the expected funding-carry horizon. Degradation chain:
        coin+side mean → global same-side mean → conservative default."""
        _side = str(side or "long").lower()
        if _side not in ("long", "short"):
            _side = "long"
        _q = _memory_quality_params()
        if days is None:
            days = _q["slip_window_days"]
        if min_samples is None:
            min_samples = _q["slip_min_samples"]
        cutoff = time.time() - days * 86400.0
        with self._lock:
            self._ensure_close_stats_nolock()
            m = self._window_mean(self._hold_side.get((coin, _side)),
                                  cutoff, min_samples)
            if m is not None:
                return m, "coin_side"
            pooled: list[float] = []
            for (c2, s2), dq in self._hold_side.items():
                if s2 == _side and c2 != coin:
                    pooled.extend(v for ts, v in dq if not ts or ts >= cutoff)
        if len(pooled) >= min_samples:
            return sum(pooled) / len(pooled), "global_side"
        return float(default_hours), "default"

    def coin_daily_realized_pnl_pct(self, coin: str,
                                    start_of_day_equity: float) -> float:
        """Sum of today's realized PnL (USD) for `coin`, expressed as a % of
        start-of-day equity. Backs the per-coin daily loss breaker. Uses
        closed_at >= the current UTC day start as recorded by track_daily_pnl.
        P1-6: O(1) read off the incremental per-coin running total (a UTC day
        roll triggers a one-time rescan in _recheck_day_stats_nolock)."""
        if start_of_day_equity <= 0:
            return 0.0
        with self._lock:
            self._ensure_close_stats_nolock()
            self._recheck_day_stats_nolock()
            total = self._day_realized_usd.get(coin, 0.0)
        return total / start_of_day_equity * 100.0

    def update_open_positions(self, pos: list[dict[str, Any]]) -> None:
        with self._lock:
            self._open_positions = list(pos)
            self._dirty = True

    def open_position_coins(self) -> set[str]:
        """Set of coins with a live (non-zero) open position. The loop exempts
        these from the pre-research cooldown so the AI can still issue a CLOSE
        on something we already hold — AI-driven exits must never be starved by
        the re-entry cooldown."""
        with self._lock:
            positions = list(self._open_positions)
        coins = set()
        for p in positions:
            if not isinstance(p, dict):
                continue
            pos = p.get("position", p)
            coin = pos.get("coin")
            try:
                if coin and float(pos.get("szi", 0) or 0) != 0:
                    coins.add(coin)
            except (TypeError, ValueError):
                continue
        return coins

    # ── Read operations ─────────────────────────────────────────────────────

    # Readers take a shallow snapshot under the lock so a concurrent append/
    # trim can never mutate the list while the caller iterates it (P0-C).

    def get_recent_perceptions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return self._perceptions[-limit:]

    def get_recent_analyses(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return self._analyses[-limit:]

    def get_recent_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return self._trades[-limit:]

    def latest_trade_ts_by_coin(self, limit: int = 20) -> dict[str, int]:
        """Map each coin to its NEWEST executed_at within the last `limit`
        trades. Backs the loop's pre-research cooldown — must be the newest,
        not the oldest, or a coin traded twice in the window keeps paying for
        redundant LLM research while it's still inside its cooldown."""
        out: dict[str, int] = {}
        for t in self.get_recent_trades(limit):  # chronological → newest wins
            if t.get("coin") and t.get("executed_at"):
                out[t["coin"]] = t["executed_at"]
        return out

    def get_all_trades(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._trades)

    def count_openings_since(self, coin: str, since_ms: int) -> int:
        # Audit 2026-09-06 (D3), ported from Pathia reentry_cap: count this
        # coin's OPENING fills (record_trade only ever records entries; closes
        # live in _closes) executed at/after ``since_ms``. Backs the per-coin
        # rolling reentry cap; a pure scan over the trade log so it shares the
        # existing retention/sweep and needs no extra state to rebuild.
        with self._lock:
            trades = list(self._trades)
        n = 0
        for t in trades:
            if t.get("coin") != coin:
                continue
            try:
                ts = int(t.get("executed_at") or 0)
            except (TypeError, ValueError):
                continue
            if ts >= since_ms:
                n += 1
        return n

    def get_all_analyses(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._analyses)

    def get_analysis_by_id(self, id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            analyses = list(self._analyses)
        for a in analyses:
            if a["id"] == id:
                return a
        return None

    def get_win_rate(self) -> dict[str, float]:
        # Prefer the realized outcome store; fall back to the legacy (never-
        # populated) trades[].pnl shape for backward compat.
        with self._lock:
            closes = list(self._closes)
            trades = list(self._trades)
        if closes:
            wins = sum(1 for c in closes if (c.get("realized_pnl_pct") or 0) > 0)
            total = len(closes)
            return {"wins": wins, "total": total, "rate": wins / total if total else 0}
        closed = [t for t in trades if t.get("exitPx") is not None and t.get("pnl") is not None]
        wins = sum(1 for t in closed if (t.get("pnl") or 0) > 0)
        total = len(closed)
        return {"wins": wins, "total": total, "rate": wins / total if total > 0 else 0}

    def get_payoff_stats(self, limit: int = 200) -> dict[str, float]:
        """Realized win-rate + payoff ratio (avg win / avg loss) from the outcome
        store — the inputs to risk-of-ruin and the Phase-3 report. Uses leveraged
        realized_pnl_pct (net fees). Returns zeros when there are no closes yet."""
        with self._lock:
            rows = self._closes[-limit:]
        wins = [float(c.get("realized_pnl_pct") or 0) for c in rows if (c.get("realized_pnl_pct") or 0) > 0]
        losses = [abs(float(c.get("realized_pnl_pct") or 0)) for c in rows if (c.get("realized_pnl_pct") or 0) <= 0]
        n = len(rows)
        win_rate = len(wins) / n if n else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        payoff = (avg_win / avg_loss) if avg_loss > 0 else 0.0
        return {
            "n": n, "win_rate": win_rate, "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss, "payoff_ratio": payoff,
        }

    def last_close_for(self, coin: str) -> Optional[dict[str, Any]]:
        """Most recent realized close for `coin` (for momentum re-entry: the
        stop-out price to compare against). None if never closed."""
        with self._lock:
            closes = list(self._closes)
        for c in reversed(closes):
            if c.get("coin") == coin:
                return c
        return None

    def get_closes(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return self._closes[-limit:]

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_start_of_day_equity(self) -> float:
        """Equity baseline at the current UTC day start (daily loss % denominator)."""
        return self._start_of_day_equity

    def get_full_state(self) -> dict[str, Any]:
        with self._lock:
            open_positions = list(self._open_positions)
            equity = self._equity
            daily_pnl = self._daily_pnl
            start_of_day_equity = self._start_of_day_equity
        return {
            "recent_perceptions": self.get_recent_perceptions(),
            "recent_analyses": self.get_recent_analyses(),
            "recent_trades": self.get_recent_trades(),
            "win_rate": self.get_win_rate(),
            "equity": equity,
            "daily_pnl": daily_pnl,
            "start_of_day_equity": start_of_day_equity,
            "open_positions": open_positions,
        }


# Module-level singleton.
memory = AgentMemory.get_instance()
