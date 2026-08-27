"""Persistent agent memory — a disk-backed singleton loaded from .agent-memory.json.

The JSON file is a per-component CACHE. The authoritative append-only record is
``~/.hermes-trading/events.jsonl`` (shared with HTA). On ``load()`` the memory is
hydrated from events.jsonl when present (P2-10); the JSON cache is used as a
fast-path fallback. Every ``flush()`` is now guarded by an ``fcntl.flock``
exclusive lock (4.5.1) so a second process (dashboard, MCP, backtest) can no
longer truncate or race the live trading loop's memory file.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

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


def _memory_limits() -> Dict[str, int]:
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
        self._perceptions: List[Dict[str, Any]] = []
        self._analyses: List[Dict[str, Any]] = []
        self._trades: List[Dict[str, Any]] = []
        self._closes: List[Dict[str, Any]] = []  # realized exits (the trade-outcome store)
        # Entry context keyed by "COIN_side" — entry time + the signal snapshot at
        # entry, so the matching close can carry it for the forward signal backtest.
        self._entry_ctx: Dict[str, Dict[str, Any]] = {}
        self._cooldowns: Dict[str, int] = {}
        self._equity: float = 0
        self._daily_pnl: float = 0
        self._peak_daily_pnl: float = 0  # high-water mark of daily_pnl (intraday, resets at UTC roll)
        self._start_of_day_equity: float = 0
        self._day_start_ts: int = 0
        self._open_positions: List[Dict[str, Any]] = []
        # ── Tiered circuit-breaker state (sizing/risk-overhaul 2026-08-26) ──
        # coin -> epoch-ms until which new entries on that coin are blocked
        # (single-coin per-trade loss > threshold). Global halt is a single
        # expiry for the whole book (daily cumulative loss > equity threshold).
        self._coin_circuit: Dict[str, int] = {}
        self._global_halt_until_ms: int = 0
        # Per-coin consecutive losing-close count (resets on a win or day roll).
        self._consecutive_losses: Dict[str, int] = {}
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
        self._slip_series: Dict[str, Deque[Tuple[float, float]]] = {}
        self._day_realized_usd: Dict[str, float] = {}
        self._day_stats_start_ts: int = 0
        self._initialized = False

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
            trades: List[Dict[str, Any]] = []
            closes: List[Dict[str, Any]] = []
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
                with open(MEMORY_FILE, "r") as f:
                    data = json.load(f)

                self._perceptions = (data.get("perceptions") or [])[:limits["perceptions"]]
                self._analyses = (data.get("analyses") or [])[:limits["analyses"]]
                self._trades = (data.get("trades") or [])[:limits["trades"]]
                self._closes = (data.get("closes") or [])[:limits["closes"]]
                self._entry_ctx = data.get("entryCtx") or {}

                # Rebuild cooldowns
                self._cooldowns.clear()
                now = int(time.time() * 1000)
                for c in (data.get("cooldowns") or []):
                    if c.get("expires", 0) > now:
                        self._cooldowns[c["coin"]] = c["expires"]

                self._equity = data.get("equity", 0)
                self._daily_pnl = data.get("dailyPnl", 0)
                self._start_of_day_equity = data.get("startOfDayEquity", 0)
                self._day_start_ts = data.get("dayStartTs", 0)
                self._open_positions = data.get("openPositions", [])

                # Tiered circuit-breaker state (best-effort restore; a stale/
                # expired entry is harmless — the remaining-minutes accessor
                # purges it).
                self._coin_circuit = {
                    str(k): int(v) for k, v in (data.get("coinCircuit") or {}).items()
                    if int(v) > now
                }
                self._global_halt_until_ms = int(data.get("globalHaltUntilMs", 0) or 0)
                if self._global_halt_until_ms < now:
                    self._global_halt_until_ms = 0
                self._consecutive_losses = {
                    str(k): int(v) for k, v in (data.get("consecutiveLosses") or {}).items()
                }

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
            if (time.monotonic() - self._last_flush_ts) < FLUSH_THROTTLE_S:
                return
        # P3-1: time the actual write path only — gated skips are not flushes.
        _t0 = time.monotonic()
        _flush_ok = False
        lock_fd = None
        # Build the snapshot UNDER the in-process lock (P0-C): the dict/list
        # contents are read here while other threads may be appending to them,
        # and json.dump iterates the same objects during the write below.
        # Holding the lock across snapshot + write also makes
        # mutate-then-flush atomic, so a concurrent mutator can't land between
        # the snapshot and the replace (lost update). flock still serializes
        # cross-process writers (dashboard/MCP/backtest); it does not protect
        # against in-process threads racing the snapshot.
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
                "startOfDayEquity": self._start_of_day_equity,
                "dayStartTs": self._day_start_ts,
                "openPositions": list(self._open_positions),
                "coinCircuit": dict(self._coin_circuit),
                "globalHaltUntilMs": int(self._global_halt_until_ms or 0),
                "consecutiveLosses": dict(self._consecutive_losses),
            }
            try:
                # Cross-process exclusive lock (4.5.1) so a concurrent
                # dashboard/MCP process can't interleave a tmp+replace with the
                # trading loop. flock is auto-released by the kernel on crash.
                lock_fd = os.open(MEMORY_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                tmp = MEMORY_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, MEMORY_FILE)
                # Persisted successfully: clear the dirty gate and arm the
                # throttle window (P1-6). On failure we leave _dirty set so a
                # later flush retries the lost write.
                self._dirty = False
                self._last_flush_ts = time.monotonic()
                _flush_ok = True
            except Exception as e:
                logger.error(f"[memory] save failed: {e}")
            finally:
                if lock_fd is not None:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        os.close(lock_fd)
                    except OSError:
                        pass
                # P3-1: flush latency + terminal failure count (retries
                # exhausted = state left dirty for the next tick). Best-effort.
                try:
                    from hermes_trader import metrics

                    metrics.MEMORY_FLUSH_DURATION.labels(
                        force=str(force).lower(),
                        outcome="ok" if _flush_ok else "failed",
                    ).observe(max(0.0, time.monotonic() - _t0))
                    if not _flush_ok:
                        metrics.MEMORY_FLUSH_ERRORS.inc()
                except Exception:  # noqa: BLE001 — metrics must never mask I/O errors
                    pass

    # ── Write operations ────────────────────────────────────────────────────

    def record_perception(self, p: Dict[str, Any]) -> None:
        with self._lock:
            self._perceptions.append(p)
            if len(self._perceptions) > _memory_limits()["perceptions"]:
                self._perceptions.pop(0)
            self._dirty = True

    def record_analysis(self, a: Dict[str, Any]) -> None:
        with self._lock:
            self._analyses.append(a)
            if len(self._analyses) > _memory_limits()["analyses"]:
                self._analyses.pop(0)
            self._dirty = True

    def record_trade(self, t: Dict[str, Any]) -> None:
        with self._lock:
            self._trades.append(t)
            if len(self._trades) > _memory_limits()["trades"]:
                self._trades.pop(0)
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

    def record_entry_context(self, coin: str, side: str, ctx: Dict[str, Any]) -> None:
        """Stash entry time + signal snapshot for an opening position, so its close
        can carry it into the outcome store (forward signal backtest)."""
        with self._lock:
            self._entry_ctx[f"{coin}_{side}"] = ctx
            self._dirty = True
        self.flush()

    def pop_entry_context(self, coin: str, side: str) -> Dict[str, Any]:
        """Retrieve + clear the entry context for a closing position (or {})."""
        with self._lock:
            ctx = self._entry_ctx.pop(f"{coin}_{side}", {})
            if ctx:
                self._dirty = True
        if ctx:
            self.flush()
        return ctx

    def record_close(self, c: Dict[str, Any]) -> None:
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
            evicted: Optional[Dict[str, Any]] = None
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
        _IMPLAUSIBLE_PCT = 0.25
        _CRASH_DOWN_PCT = float(os.environ.get("HERMES_EQUITY_CRASH_DOWN_PCT", "0.40"))
        if (prev_eq > 0 and current_equity > 0
                and (now_s - prev_ts) < 180):
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
                if streak < 2:
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
            if self._day_start_ts < today_utc or self._start_of_day_equity == 0:
                # Re-baseline at day roll or after a memory reset. If there were
                # already contributions today (e.g. operator transferred USDC
                # spot→perp before starting the bot), the baseline must exclude
                # them so the first PnL reading doesn't show -contributions as a
                # loss: daily_pnl = equity_now - baseline - contributions = 0.
                self._start_of_day_equity = current_equity - net_contributions
                self._day_start_ts = today_utc
                self._daily_pnl = 0
                self._peak_daily_pnl = 0  # reset high-water mark at the UTC day roll
                # A new trading day clears the consecutive-loss streak and any
                # lingering per-coin circuit (global halt intentionally survives
                # until its own wall-clock expiry).
                self._consecutive_losses = {}
            else:
                self._daily_pnl = current_equity - self._start_of_day_equity - net_contributions
            # Track the day's peak PnL so a give-back breaker can lock in green days.
            self._peak_daily_pnl = max(self._peak_daily_pnl, self._daily_pnl)
            self._equity = current_equity
            # P1-6: every accepted equity tick mutates dailyPnl/peak/equity;
            # coalesced onto the trading loop's periodic (throttled) flush.
            self._dirty = True

    def peak_daily_pnl(self) -> float:
        """Intraday high-water mark of daily PnL (resets at UTC midnight)."""
        return self._peak_daily_pnl

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
        self.flush()

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
        self.flush()

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
        self.flush()

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

    def circuit_snapshot(self) -> Dict[str, Any]:
        """Read-only (non-mutating) view of the tiered breaker state for metrics.

        Unlike the ``*_remaining_min`` accessors this never purges expired
        entries — a /metrics scrape must not mutate trading state — so expired
        coins are simply excluded from the armed count by comparing timestamps.
        Returns ``{"armed_coins": int, "global_halt": bool}``.
        """
        now_ms = int(time.time() * 1000)
        with self._lock:
            armed = sum(1 for exp in self._coin_circuit.values() if int(exp or 0) > now_ms)
            halted = int(self._global_halt_until_ms or 0) > now_ms
        return {"armed_coins": armed, "global_halt": bool(halted)}

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
    def _close_ts_s(c: Dict[str, Any]) -> Optional[float]:
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

    def _accumulate_close_nolock(self, c: Dict[str, Any],
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

    def _evict_close_stats_nolock(self, c: Dict[str, Any]) -> None:
        """Detach stats for a close row evicted from the bounded _closes list
        (call under lock). O(1): deque entries share append order with
        _closes, so the evicted close — the oldest for its coin — sits at the
        head of that coin's deque when it carried adverse slip."""
        coin = c.get("coin")
        if not coin:
            return
        ts_s = self._close_ts_s(c)
        dq = self._slip_series.get(coin)
        if dq:
            head_ts, head_v = dq[0]
            slip = c.get("exit_slip_bps")
            try:
                v = float(slip) if slip is not None else 0.0
            except (TypeError, ValueError):
                v = 0.0
            if head_ts == ts_s and abs(head_v - v) < 1e-9:
                dq.popleft()
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
        day_totals: Dict[str, float] = {}
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
                day_totals[coin] = day_totals.get(coin, 0.0) + float(pnl)
            except (TypeError, ValueError):
                continue
        self._day_realized_usd = day_totals
        self._day_stats_start_ts = day_start

    def avg_exit_slip_bps(self, coin: str, days: float = 30.0,
                          min_samples: int = 3) -> float:
        """Mean adverse exit slippage in bps over the last `days` for `coin`.
        Returns 0.0 when there are fewer than `min_samples` qualifying closes
        (insufficient history → do not widen on noise). P1-6: scans only the
        coin's bounded adverse-slip deque (≤ the configured closes limit,
        already coin/adverse-filtered) instead of the full _closes list."""
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

    def update_open_positions(self, pos: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._open_positions = list(pos)
            self._dirty = True

    def open_position_coins(self) -> Set[str]:
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

    def get_recent_perceptions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return self._perceptions[-limit:]

    def get_recent_analyses(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return self._analyses[-limit:]

    def get_recent_trades(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return self._trades[-limit:]

    def latest_trade_ts_by_coin(self, limit: int = 20) -> Dict[str, int]:
        """Map each coin to its NEWEST executed_at within the last `limit`
        trades. Backs the loop's pre-research cooldown — must be the newest,
        not the oldest, or a coin traded twice in the window keeps paying for
        redundant LLM research while it's still inside its cooldown."""
        out: Dict[str, int] = {}
        for t in self.get_recent_trades(limit):  # chronological → newest wins
            if t.get("coin") and t.get("executed_at"):
                out[t["coin"]] = t["executed_at"]
        return out

    def get_all_trades(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._trades)

    def get_all_analyses(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._analyses)

    def get_analysis_by_id(self, id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            analyses = list(self._analyses)
        for a in analyses:
            if a["id"] == id:
                return a
        return None

    def get_win_rate(self) -> Dict[str, float]:
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

    def get_payoff_stats(self, limit: int = 200) -> Dict[str, float]:
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

    def last_close_for(self, coin: str) -> Optional[Dict[str, Any]]:
        """Most recent realized close for `coin` (for momentum re-entry: the
        stop-out price to compare against). None if never closed."""
        with self._lock:
            closes = list(self._closes)
        for c in reversed(closes):
            if c.get("coin") == coin:
                return c
        return None

    def get_closes(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            return self._closes[-limit:]

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_day_start_ts(self) -> int:
        """UTC-midnight unix-seconds timestamp for the in-progress trading day."""
        return self._day_start_ts

    def get_start_of_day_equity(self) -> float:
        """Equity baseline at the current UTC day start (daily loss % denominator)."""
        return self._start_of_day_equity

    def get_full_state(self) -> Dict[str, Any]:
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
