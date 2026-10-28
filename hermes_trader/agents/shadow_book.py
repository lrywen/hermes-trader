"""SHADOW-mode paper-trading ledger (virtual accounts, live-marked).

When the engine runs in mode=SHADOW, a decision that passes EVERY risk gate
normally just returns ``{"executed": False, "reason":
"shadow_mode_would_execute"}`` — nothing is booked, so the dashboard cannot
show what the strategy *would* have done. This module books that decision into
ISOLATED paper accounts and marks them to live mids every loop:

统一成交契约（2026-09-21）—— 同一个「过闸决策」同时喂给两个并列口径账户：

  * ``taker``：决策时按 mid 立即吃单成交（历史既有行为），费用按 taker。
  * ``maker_shadow``：按决策时 mid 挂一张 post-only 限价单（limit 内移一个
    可配 offset），每个 mark 周期拉一次**短缓存 1m K线**，复用
    ``execution.maker_shadow.simulate_shadow_order`` 的保守触及规则判定是否
    被动成交；TTL 内未被触及则撤销（不记账），成交后走与 taker **完全相同**
    的 DSL 退出引擎，费用按 maker。两账户各自独立的 wallet / positions /
    equity_curve，互不污染，dashboard 可直接两曲线对照。

口径声明（引用 maker_shadow 结论时务必一并带上，见 maker_shadow.py）：
SHADOW 触及规则（买单 low≤limit / 卖单 high≥limit）不知道队列位置，系统性
**高估**真实成交率，给的是「乐观成交上界」；能判逆向选择，判不了真实排队/
成交率——后者需小额实盘取样。

No real orders, no real funds: nothing here ever touches the exchange or
``.agent-memory.json``. Persistence follows memory.py / dsl_exit.py: a single
JSON state file guarded by ``fcntl.flock`` + tmp + fsync + ``os.replace`` and
a process-wide ``threading.RLock``. Overridable via ``HERMES_SHADOW_BOOK_FILE``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Optional

from hermes_trader.agents import atomic_io

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHADOW_BOOK_FILE = os.environ.get(
    "HERMES_SHADOW_BOOK_FILE",
    os.path.join(_REPO_ROOT, ".shadow-book.json"),
)
SHADOW_BOOK_LOCK_FILE = SHADOW_BOOK_FILE + ".lock"

_STATE_VERSION = 2
# Canonical account ids. ``taker`` is the default/legacy view; ``maker_shadow``
# is the passive-fill counterfactual.
ACCOUNTS = ("taker", "maker_shadow")
_MAX_FILLS = 2000        # open + close fills combined per account (audit cap)
_MAX_EQUITY_POINTS = 3000
_EQUITY_MIN_INTERVAL_S = 60.0   # throttle between-curve points while a position is open
_SAVE_MIN_INTERVAL_S = 5.0      # throttle mark-time persistence (open/close force-save)


# ---------------------------------------------------------------------------
# config helpers
# ---------------------------------------------------------------------------

def _shadow_cfg() -> dict[str, Any]:
    try:
        from hermes_trader.agents.config_store import cfg_get
        c = cfg_get("shadow_book", None)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _enabled() -> bool:
    return bool(_shadow_cfg().get("enabled", True))


def _maker_enabled() -> bool:
    """Whether the maker_shadow counterfactual account is fed. Default ON so
    the two口径 are collected together; operators can disable to save the
    (cached, opportunistic) candle fetches."""
    return bool(_shadow_cfg().get("maker_shadow_enabled", True))


def _starting_balance() -> float:
    try:
        v = float(_shadow_cfg().get("starting_balance", 10000.0))
        return v if v > 0 else 10000.0
    except (TypeError, ValueError):
        return 10000.0


def _taker_fee_pct() -> float:
    """Per-fill taker fee in PERCENT. Prefer the shadow_book override; fall back
    to the live execution block so paper fees always track live modeling."""
    c = _shadow_cfg()
    try:
        if c.get("taker_fee_pct") is not None:
            return float(c["taker_fee_pct"])
    except (TypeError, ValueError):
        pass
    try:
        from hermes_trader.agents.config_store import cfg_get
        return float(cfg_get("execution.taker_fee_pct", 0.045))
    except Exception:
        return 0.045


def _taker_slippage_pct() -> float:
    """Per-fill taker slippage in PERCENT (R2: honest paper fills).

    Paper taker fills otherwise execute exactly at mid, systematically
    overstating PnL. Defaults to a conservative liquid-market value; set
    ``shadow_book.taker_slippage_pct = 0`` to disable.
    """
    c = _shadow_cfg()
    try:
        if c.get("taker_slippage_pct") is not None:
            return max(0.0, float(c["taker_slippage_pct"]))
    except (TypeError, ValueError):
        pass
    return 0.05


def _apply_slippage(px: float, *, side: str, opening: bool,
                    slip_pct: float) -> float:
    """Move the fill price against the taker by ``slip_pct``.

    Long open / short close -> buy side (pay more). Short open / long close ->
    sell side (receive less).
    """
    if slip_pct <= 0:
        return float(px)
    f = slip_pct / 100.0
    buying = (side == "long") if opening else (side == "short")
    return float(px) * (1.0 + f) if buying else float(px) * (1.0 - f)


def _maker_fee_pct() -> float:
    """Per-fill maker fee in PERCENT. Prefer the shadow_book override; fall back
    to the live execution maker fee, then to HL's tier-0 0.015%."""
    c = _shadow_cfg()
    try:
        if c.get("maker_fee_pct") is not None:
            return float(c["maker_fee_pct"])
    except (TypeError, ValueError):
        pass
    try:
        from hermes_trader.agents.config_store import cfg_get
        return float(cfg_get("execution.maker_fee_pct", 0.015))
    except Exception:
        return 0.015


def _round_trip_fills() -> int:
    c = _shadow_cfg()
    try:
        if c.get("round_trip_fills") is not None:
            return max(1, int(c["round_trip_fills"]))
    except (TypeError, ValueError):
        pass
    try:
        from hermes_trader.agents.config_store import cfg_get
        return max(1, int(cfg_get("execution.round_trip_fills", 2)))
    except Exception:
        return 2


def _maker_limit_offset_bps() -> float:
    """How far INSIDE the spread the resting maker limit is posted (bps from
    the decision mid). A buy posts below mid, a sell above. Default 5 bps."""
    try:
        v = float(_shadow_cfg().get("maker_limit_offset_bps", 5.0))
        return v if v >= 0 else 5.0
    except (TypeError, ValueError):
        return 5.0


def _maker_ttl_bars() -> int:
    """Max 1m bars a maker order rests before it is canceled unfilled."""
    try:
        v = int(_shadow_cfg().get("maker_ttl_bars", 30))
        return max(1, v)
    except (TypeError, ValueError):
        return 30


def _maker_fill_model() -> str:
    """Which maker fill simulator the live shadow book uses.

    Default ``v1`` preserves the audited touch-fill behaviour byte-for-byte;
    ``v2`` selects the conditional-fill + adverse-selection-priced model
    (maker_shadow.simulate_shadow_order_v2, deterministic conservative path).
    Any unrecognised value fails closed to ``v1`` so a typo never silently
    changes booking. v2 remains a SHADOW/replay-only model — never live.
    """
    raw = str(_shadow_cfg().get("maker_fill_model", "v1") or "v1").strip().lower()
    return "v2" if raw in ("v2", "maker_shadow_v2", "conditional") else "v1"


def _maker_v2_params() -> dict[str, float]:
    """v2 conditional-fill parameters (conservative priors; config-overridable
    for offline sweep). Values are clamped to [0,1] by the simulator itself."""
    c = _shadow_cfg()

    def _f(key: str, default: float) -> float:
        try:
            return float(c[key]) if c.get(key) is not None else default
        except (TypeError, ValueError):
            return default

    from hermes_trader.execution.maker_shadow import (
        V2_DEFAULT_ADV_DRIFT_KAPPA,
        V2_DEFAULT_BASE_FILL_PROB,
        V2_DEFAULT_FILL_PROB_DECAY,
    )
    return {
        "base_fill_prob": _f("maker_v2_base_fill_prob", V2_DEFAULT_BASE_FILL_PROB),
        "fill_prob_decay": _f("maker_v2_fill_prob_decay", V2_DEFAULT_FILL_PROB_DECAY),
        "adv_drift_kappa": _f("maker_v2_adv_drift_kappa", V2_DEFAULT_ADV_DRIFT_KAPPA),
    }


def _norm_coin(coin: object) -> str:
    """Normalize a coin symbol for allow-list matching (upper, strip USDT)."""
    return str(coin or "").upper().removesuffix("USDT").strip()


def _maker_v2_coins() -> Optional[set[str]]:
    """Optional coin allow-list for the v2 conditional-fill maker model.

    Returns None when unset/empty => v2 applies to every coin (prior semantics).
    When set (e.g. ["BTC"]), returns the symbol set and only those coins book via
    v2; every other coin keeps the audited v1 touch-fill model. A present but
    malformed/empty-after-normalize allow-list returns an empty set (not None),
    i.e. fail closed to v1 on every coin rather than silently applying v2 to all.
    """
    if "maker_v2_coins" not in _shadow_cfg():
        return None
    raw = _shadow_cfg().get("maker_v2_coins")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple, set)):
        return set()  # malformed type: restrict to nothing (v1 everywhere)
    if len(raw) == 0:
        return None  # explicit empty allow-list == all coins (canonical default)
    coins = {_norm_coin(c) for c in raw if str(c or "").strip()}
    # present, non-empty list that parses to zero valid symbols -> fail closed
    return coins if coins else set()


def _maker_uses_v2(coin: str) -> bool:
    """Coin-scoped model selection: global model must be v2 AND, when an allow
    list is configured, the coin must appear in it. Fails closed to v1."""
    if _maker_fill_model() != "v2":
        return False
    allow = _maker_v2_coins()
    if allow is None:
        return True
    return _norm_coin(coin) in allow


# Upper bound on 1m candles fetched to resolve one resting maker order. Guards
# against unbounded fetch for pathologically stale orders; anything older than
# this is handled by the TTL-age cancel fallback in _resolve_pending_maker_orders.
_MAX_MAKER_FETCH_BARS = 600  # 10h of 1m bars
# How many 1m bars after the fill bar we wait for the "next-bar mid" needed to
# compute post-fill adverse-selection drift before giving up (metric stays None).
_MAKER_DRIFT_WAIT_BARS = 3


def _max_positions() -> int:
    """Concurrent-position cap for EACH paper account.

    SHADOW/LIVE PARITY: this MUST equal the global ``max_concurrent`` so the
    paper book admits exactly as many concurrent positions as the live gate
    permits — otherwise SHADOW could book entries the live ``max_concurrent``
    gate blocks (or vice-versa), skewing 1:1 backtest parity. Fall back to the
    live ``max_concurrent`` when the shadow key is unset; log a loud warning on
    any mismatch so a future config drift cannot silently diverge the modes.
    """
    try:
        from hermes_trader.agents.config_store import cfg_get
        live_cap = max(1, int(cfg_get("max_concurrent", 2) or 2))
    except Exception:
        live_cap = 2
    c = _shadow_cfg()
    raw = c.get("max_positions", None)
    if raw is None:
        return live_cap
    try:
        cap = max(1, int(raw))
    except (TypeError, ValueError):
        return live_cap
    if cap != live_cap:
        logger.warning(
            "[shadow_book] DRIFT: shadow_book.max_positions=%d != global "
            "max_concurrent=%d — SHADOW/LIVE position caps differ; backtest "
            "parity compromised. Set shadow_book.max_positions=%d.",
            cap, live_cap, live_cap,
        )
    return cap


def _build_policy(regime: str = ""):
    """Build the SAME DSL ExitPolicy a fresh live entry would get, so paper
    positions exit under identical stops. Lazy import avoids cycles.

    Regime-aware parity: the live executor calls ``select_exit_params`` and
    registers the per-regime max_loss_pct / max_loss_roe_pct / protect /
    retrace / tiers. We start from the full base config policy and overlay the
    regime-aware exit params exactly as the live executor does, so paper/live
    stops match. Fail-open: any resolution error falls back to the base config
    policy (never a crash, never the bare ExitPolicy() default).
    """
    base = None
    try:
        from hermes_trader.agents.dsl_exit import _policy_from_config
        base = _policy_from_config()
    except Exception:
        try:
            from hermes_trader.agents.dsl_exit import ExitPolicy
            base = ExitPolicy()
        except Exception:
            return None
    try:
        from hermes_trader.agents.config_store import read_agent_config
        from hermes_trader.agents.regime_exit_common import overlay_regime_exit
        dsl = read_agent_config().get("dsl_exit", {}) or {}
        policy, _label = overlay_regime_exit(base, dsl, regime or "neutral")
        return policy
    except Exception as e:
        logger.warning(
            f"[shadow_book] regime-aware policy build failed "
            f"(regime={regime!r}); falling back to base config policy: {e}")
        return base


# Live exchange BACKUP stop-loss (the disaster net) defaults. Mirrors executor
# _DEFAULT_SL_*. Parity note: in Phase 1 the exchange trigger rests at this
# width and never moves (sync_exchange_sl only trails once profit reaches
# protect_pct), so it is the worst-case price a live max_loss can fill at on a
# gap that trades THROUGH the DSL floor between two polls.
_BACKUP_SL_ATR_MULT = 1.5
_BACKUP_SL_FLOOR_PCT = 1.0
_BACKUP_SL_CEILING_PCT = 3.0


def _backup_sl_trigger_px(*, coin: str, side: str, entry_px: float,
                          entry_atr_pct: float) -> Optional[float]:
    """Price at which the live exchange backup SL fires on a gap-through.

    宽度统一走备份 SL SSOT ``executor.compute_backup_sl_width_pct``（PRM-01/S4
    收口），不再本地手写 clamp，避免与生产挂单公式漂移。Returns None when
    inputs are unusable. Slip-widening is omitted (it needs live
    avg_exit_slip_bps the paper book cannot observe); second-order.

    atr 不可用（entry_atr_pct<=0）时与生产 executor 挂单守卫一致——**不挂备份
    网**、直接返回 None（生产 L1694 ``if atr > 0 and size_in_coin > 0``）；不得
    退回 SSOT 的 floor 宽度，否则纸簿会高估实盘的 gap-through 保护（NEW_C）。
    """
    try:
        if entry_px <= 0:
            return None
        atr_pct = float(entry_atr_pct or 0.0)
        if atr_pct <= 0.0:
            # 对齐生产：atr 缺失时不挂备份止损单。
            return None
        from hermes_trader.agents.config_store import cfg_get
        from hermes_trader.agents.executor import compute_backup_sl_width_pct
        mult = float(cfg_get("sl_atr_mult",
                             cfg_get("dsl_exit.atr_stop.atr_mult",
                                     _BACKUP_SL_ATR_MULT)))
        floor = float(cfg_get("sl_floor_pct",
                              cfg_get("dsl_exit.atr_stop.floor_pct",
                                      _BACKUP_SL_FLOOR_PCT)))
        ceiling = float(cfg_get("sl_ceiling_pct", _BACKUP_SL_CEILING_PCT))
        coin_floor = cfg_get(
            f"atr_risk_sizing.coin_overrides.{coin}.sl_floor_pct", None)
        if coin_floor is not None:
            floor = float(coin_floor)
        if not (mult > 0 and floor > 0 and ceiling > 0):
            return None
        if floor > ceiling:
            floor = ceiling
        # SSOT 以 atr 价格计；由 entry_atr_pct 反推 atr = atr_pct% × entry_px。
        atr_price = atr_pct / 100.0 * float(entry_px)
        width = compute_backup_sl_width_pct(
            atr=atr_price, entry_px=float(entry_px), sl_atr_mult=mult,
            sl_floor_pct=floor, sl_ceiling_pct=ceiling)
        if side == "long":
            return float(entry_px) * (1.0 - width / 100.0)
        if side == "short":
            return float(entry_px) * (1.0 + width / 100.0)
    except Exception as e:
        logger.warning(f"[shadow_book] backup SL trigger calc failed {coin}: {e}")
    return None


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _reflection_row(acct: str, fill: dict[str, Any]) -> dict[str, Any]:
    """Map a paper close-fill onto the close-row shape reflection expects.

    Paper fills use ``price``/``opened_at`` and carry no entry-time signal
    snapshot; ``entry_time`` (opened_at, epoch seconds) satisfies the reviewer
    gate. ``fill_model`` tags which口径 produced it so reviews stay attributable.
    """
    opened_at = fill.get("opened_at")
    entry_time = None
    if opened_at:
        try:
            entry_time = float(opened_at) / 1000.0
        except (TypeError, ValueError):
            entry_time = None
    return {
        "coin": fill.get("coin"),
        "side": fill.get("side"),
        "leverage": fill.get("leverage"),
        "entry_px": fill.get("entry_px"),
        "exit_px": fill.get("price"),
        "hold_minutes": fill.get("hold_minutes"),
        "realized_pnl_pct": fill.get("realized_pnl_pct"),
        "realized_pnl_usd": fill.get("realized_pnl_usd"),
        "regime_at_entry": fill.get("entry_regime"),
        "closed_at": fill.get("ts"),
        "entry_time": entry_time,
        "trace_id": fill.get("analysis_id"),
        "fill_model": acct,
    }


# P0-A instrumentation (2026-10-19): point-in-time trigger labels pinned on the
# OPEN fill so future real shadow fills can be sliced by *which perception
# trigger fired* -> realized ROE. Historical fills predate this and carry none.
# Kept tiny (a list of names + a few floats); anything malformed is dropped so
# a bad upstream payload can never break bookkeeping.
_SIGNAL_META_NUM_KEYS = ("composite_score", "ai_confidence", "trigger_score")


def _sanitize_signal_meta(meta: Any) -> dict[str, Any]:
    """Coerce an untrusted signal-meta payload into a compact persisted dict.

    Returns {} when there is nothing usable. fired_names is a de-duplicated
    sorted list of non-empty strings; numeric keys are finite floats.
    """
    if not isinstance(meta, dict):
        return {}
    out: dict[str, Any] = {}
    raw_names = meta.get("fired_names")
    if isinstance(raw_names, (list, tuple, set)):
        names = sorted({str(n) for n in raw_names if n})
        if names:
            out["fired_names"] = names
    for k in _SIGNAL_META_NUM_KEYS:
        v = meta.get(k)
        try:
            fv = float(v) if v is not None else None
        except (TypeError, ValueError):
            fv = None
        if fv is not None and fv == fv and fv not in (float("inf"), float("-inf")):
            out[k] = fv
    return out



def _write_atomic(path: str, data: dict[str, Any]) -> bool:
    # Durability contract (tmp-in-dir + fsync file + replace + fsync dir,
    # serialised by an flock on <path>.lock) lives in agents.atomic_io.
    try:
        atomic_io.locked_write_json_atomic(path, data, indent=2, fsync=True)
        return True
    except Exception as e:
        logger.error(f"[shadow_book] save failed: {e}")
        return False


def _read_state(path: str) -> Optional[dict[str, Any]]:
    if not os.path.exists(path):
        return None
    lock_fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[shadow_book] load failed: {e}")
        return None
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _fresh_account() -> dict[str, Any]:
    bal = _starting_balance()
    return {
        "wallet_balance": bal,     # starting + realized PnL (fees deducted on close)
        "positions": [],
        "pending_orders": [],      # maker_shadow-only: resting unfilled limits
        "pending_drift": [],       # maker_shadow-only: filled opens awaiting the
                                   # next 1m bar to backfill post-fill mid drift
        "fills": [],
        "equity_curve": [],
        "closed_count": 0,
    }


def _migrate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bring an on-disk state payload up to ``_STATE_VERSION``.

    v1 was a single FLAT taker account (wallet_balance / positions / fills /
    equity_curve / closed_count at top level). v2 nests those under
    ``accounts["taker"]`` and adds an empty ``accounts["maker_shadow"]``. The
    migration preserves the entire legacy taker book verbatim. An unknown
    FUTURE version is left untouched and warned about. Idempotent.
    """
    if not isinstance(payload, dict):
        raise ValueError("shadow_book payload is not a JSON object")
    raw_version = payload.get("version")
    try:
        version = int(raw_version) if raw_version is not None else 1
    except (TypeError, ValueError):
        logger.warning(
            f"[shadow_book] state file has unparseable version {raw_version!r}; "
            f"treating as v{_STATE_VERSION}"
        )
        version = _STATE_VERSION

    if version > _STATE_VERSION:
        logger.warning(
            f"[shadow_book] state file version {version} is newer than this "
            f"binary (expects v{_STATE_VERSION}); loading without migration — "
            f"a daemon downgrade may be in progress"
        )
        return payload

    if version < 2:
        # Legacy flat taker book → nest under accounts.taker, keep every field.
        legacy = {
            "wallet_balance": float(payload.get(
                "wallet_balance", payload.get("starting_balance", _starting_balance()))),
            "positions": payload.get("positions") if isinstance(payload.get("positions"), list) else [],
            "pending_orders": [],
            "fills": payload.get("fills") if isinstance(payload.get("fills"), list) else [],
            "equity_curve": payload.get("equity_curve") if isinstance(payload.get("equity_curve"), list) else [],
            "closed_count": int(payload.get("closed_count", 0) or 0),
        }
        payload = {
            "created_at": payload.get("created_at", _now_ms()),
            "starting_balance": float(payload.get(
                "starting_balance", _starting_balance())),
            "accounts": {"taker": legacy, "maker_shadow": _fresh_account()},
        }
    payload["version"] = _STATE_VERSION
    return payload


# ---------------------------------------------------------------------------
# the book
# ---------------------------------------------------------------------------

def _tracker_policy_snapshot(tracker: Any) -> dict[str, Any]:
    """Read-only snapshot of the EFFECTIVE exit policy a DSLTracker ran.

    L5 observability (2026-10-21 redundancy plan): the configured regime clocks
    (stale 60/120, hard 120/240) and fixed ignite grace are only knowable from
    the live policy object, so pin the handful of clock/stop knobs onto the
    close fill for regime/clock-split reconciliation. Best-effort; any missing
    attribute degrades to None and this never raises into the close path.
    """
    try:
        pol = getattr(tracker, "policy", None)
        if pol is None:
            return {}
        lev = int(getattr(tracker, "leverage", 1) or 1)
        snap = {
            "leverage": lev,
            "entry_atr_pct": round(float(getattr(tracker, "entry_atr_pct", 0.0) or 0.0), 4),
            "protect_pct": getattr(pol, "protect_pct", None),
            "max_loss_pct": getattr(pol, "max_loss_pct", None),
            "max_loss_roe_pct": getattr(pol, "max_loss_roe_pct", None),
            "hard_timeout_minutes": getattr(pol, "hard_timeout_minutes", None),
            "stale_flat_timeout_minutes": getattr(pol, "stale_flat_timeout_minutes", None),
            "ignite_grace_minutes": getattr(pol, "ignite_grace_minutes", None),
        }
        return snap
    except Exception:
        return {}


class ShadowBook:
    """In-process singleton wrapping the two virtual accounts + DSL trackers."""

    def __init__(self, path: str = SHADOW_BOOK_FILE) -> None:
        self.path = path
        self._lock = threading.RLock()
        # key (account, f"{coin}_{side}") -> DSLTracker (filled positions)
        self._trackers: dict[tuple[str, str], Any] = {}
        self._last_save_ts = 0.0
        self._last_equity_ts = 0.0
        self._last_snapshot: dict[str, Any] = {}
        self._last_mtime = 0.0
        # A4 (2026-10-21 redundancy plan): count would-execute opens that the
        # book silently dropped because an identical position was already open.
        # In-process only (not persisted): the quantity that matters is the live
        # loop's suppression rate since boot; avoiding a persisted mutable
        # counter also sidesteps cross-process save/reload merge races.
        self._would_execute_dropped = 0
        self.state: dict[str, Any] = self._fresh_state()
        self._load()

    def reload_if_changed(self) -> bool:
        """Cross-process hot reload.

        The dashboard runs in a SEPARATE process from the trading loop, so the
        in-memory singleton goes stale the moment the loop writes the file.
        Re-read only when the on-disk mtime advanced past our last load/write;
        a no-op otherwise. In the loop's own process the mtime only advances
        after its OWN save (content == memory), so a reload there is harmless.
        """
        try:
            if not os.path.exists(self.path):
                return False
            mtime = os.path.getmtime(self.path)
            if mtime <= self._last_mtime:
                return False
            with self._lock:
                if mtime <= self._last_mtime:
                    return False
                self._load()
                return True
        except OSError:
            return False

    def count_openings_since(self, coin: str, since_ms: int) -> int:
        """Count this coin's TAKER open fills at/after ``since_ms``.

        reentry_cap must count the openings that actually happened in the mode
        the system runs in. In SHADOW mode entries land here (shadow_open), not
        in the live memory trade log, so a memory-backed count would always read
        0 and the gate could never trigger. This is the shadow-population
        counterpart to ``memory.count_openings_since``.
        """
        self.reload_if_changed()
        with self._lock:
            try:
                fills = list(self._account("taker")["fills"])
            except Exception:
                fills = []
        n = 0
        for f in fills:
            if f.get("type") != "open" or f.get("coin") != coin:
                continue
            try:
                if int(f.get("ts") or 0) >= since_ms:
                    n += 1
            except (TypeError, ValueError):
                continue
        return n

    def last_taker_close(self, coin: str) -> Optional[dict[str, Any]]:
        """Return this coin's most recent TAKER close fill, or None.

        Hemostat (2026-10-11): the SHADOW counterpart of the live
        ``memory.last_close_for`` used by the runner gate's per-coin cooldown.
        Shadow entries/closes never touch live memory, so a cooldown reading
        live memory is blind in SHADOW — this reads the paper ledger the system
        actually populates in shadow mode. Carries the DSL ``reason`` and
        ``realized_pnl_usd`` so a non-profit timeout exit can pace re-entry.
        """
        self.reload_if_changed()
        with self._lock:
            try:
                fills = list(self._account("taker")["fills"])
            except Exception:
                return None
        last: Optional[dict[str, Any]] = None
        for f in fills:
            if f.get("type") != "close" or f.get("coin") != coin:
                continue
            if last is None or int(f.get("ts") or 0) >= int(last.get("ts") or 0):
                last = f
        return last

    def _fresh_state(self) -> dict[str, Any]:
        return {
            "version": _STATE_VERSION,
            "created_at": _now_ms(),
            "starting_balance": _starting_balance(),
            "accounts": {acct: _fresh_account() for acct in ACCOUNTS},
        }

    # -- persistence --------------------------------------------------------

    def _account(self, acct: str) -> dict[str, Any]:
        accts = self.state["accounts"]
        if acct not in accts:
            accts[acct] = _fresh_account()
        return accts[acct]

    def _load(self) -> None:
        data = _read_state(self.path)
        try:
            self._last_mtime = os.path.getmtime(self.path)
        except OSError:
            pass
        if not data:
            return
        try:
            data = _migrate_payload(data)
            start_bal = float(data.get("starting_balance", _starting_balance()))
            accounts: dict[str, Any] = {}
            for acct in ACCOUNTS:
                raw = (data.get("accounts") or {}).get(acct)
                raw = raw if isinstance(raw, dict) else {}
                accounts[acct] = {
                    "wallet_balance": float(raw.get("wallet_balance", start_bal)),
                    "positions": raw.get("positions") if isinstance(raw.get("positions"), list) else [],
                    "pending_orders": raw.get("pending_orders") if isinstance(raw.get("pending_orders"), list) else [],
                    "pending_drift": raw.get("pending_drift") if isinstance(raw.get("pending_drift"), list) else [],
                    "fills": raw.get("fills") if isinstance(raw.get("fills"), list) else [],
                    "equity_curve": raw.get("equity_curve") if isinstance(raw.get("equity_curve"), list) else [],
                    "closed_count": int(raw.get("closed_count", 0) or 0),
                }
            self.state = {
                "version": _STATE_VERSION,
                "created_at": data.get("created_at", _now_ms()),
                "starting_balance": start_bal,
                "accounts": accounts,
            }
        except Exception as e:
            logger.error(f"[shadow_book] corrupt state, starting fresh: {e}")
            self.state = self._fresh_state()
            return
        self._rehydrate_trackers()
        counts = ", ".join(
            f"{acct}:{len(self.state['accounts'][acct]['positions'])} open/"
            f"{self.state['accounts'][acct]['closed_count']} closed"
            for acct in ACCOUNTS)
        logger.info(f"[shadow_book] loaded ({counts})")

    def _rehydrate_trackers(self) -> None:
        """Rebuild a DSLTracker per open FILLED position so peak/floor state
        resumes. Per-row tolerance: a malformed position is evicted with a
        warning instead of aborting and losing ALL trackers."""
        try:
            from hermes_trader.agents.dsl_exit import DSLTracker
            for acct in ACCOUNTS:
                kept: list[dict[str, Any]] = []
                for p in self.state["accounts"][acct]["positions"]:
                    try:
                        coin = p["coin"]
                        side = p["side"]
                        entry_px = float(p["entry_px"])
                        entry_time = float(p.get("opened_at", _now_ms())) / 1000.0
                        policy = _build_policy(p.get("entry_regime", "") or "")
                        t = DSLTracker(
                            coin, side, entry_px, entry_time,
                            policy=policy, leverage=int(p.get("leverage", 1) or 1),
                            entry_atr_pct=float(p.get("entry_atr_pct", 0.0) or 0.0),
                            entry_regime=p.get("entry_regime", "") or "",
                        )
                        if p.get("peak_px"):
                            t.peak_px = float(p["peak_px"])
                        self._trackers[(acct, self._key(coin, side))] = t
                        kept.append(p)
                    except (AttributeError, TypeError, ValueError, KeyError) as e:
                        logger.warning(
                            f"[shadow_book] {acct}: skipping malformed position "
                            f"{p!r}: {e}")
                        continue
                self.state["accounts"][acct]["positions"] = kept
        except Exception as e:
            logger.warning(f"[shadow_book] tracker rehydrate failed (non-fatal): {e}")

    def _save(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_save_ts) < _SAVE_MIN_INTERVAL_S:
            return
        self._last_save_ts = now
        # Persist tracker peak into each position before serializing.
        for acct in ACCOUNTS:
            for p in self.state["accounts"][acct]["positions"]:
                t = self._trackers.get((acct, self._key(p["coin"], p["side"])))
                if t is not None:
                    p["peak_px"] = t.peak_px
        if _write_atomic(self.path, self.state):
            try:
                self._last_mtime = os.path.getmtime(self.path)
            except OSError:
                pass

    @staticmethod
    def _key(coin: str, side: str) -> str:
        return f"{coin}_{side}"

    # -- queries ------------------------------------------------------------

    @staticmethod
    def _find(rows: list[dict[str, Any]], coin: str, side: str) -> Optional[dict[str, Any]]:
        return next(
            (r for r in rows if r["coin"] == coin and r["side"] == side), None)

    def _account_metrics(self, acct: str,
                         mids: Optional[dict[str, float]] = None,
                         fee_pct: Optional[float] = None) -> dict[str, Any]:
        """Compute wallet / margin / equity / unrealized for one account.

        When ``mids`` is supplied, filled positions are marked to those live
        mids and per-position mark fields are refreshed; otherwise the last
        stored mark is used. Resting (unfilled) maker orders hold no margin and
        contribute nothing until filled.
        """
        acc = self._account(acct)
        fee_pct = fee_pct if fee_pct is not None else (
            _maker_fee_pct() if acct == "maker_shadow" else _taker_fee_pct())
        used_margin = 0.0
        unrealized = 0.0
        open_notional = 0.0
        for p in acc["positions"]:
            notional = float(p["size_usd"])
            lev = max(1, int(p.get("leverage", 1)))
            used_margin += notional / lev
            open_notional += notional
            mark = None
            if mids is not None:
                mark = mids.get(p["coin"])
            if mark is not None and mark > 0:
                if p["side"] == "long":
                    upct = (mark - float(p["entry_px"])) / float(p["entry_px"]) * 100.0
                else:
                    upct = (float(p["entry_px"]) - mark) / float(p["entry_px"]) * 100.0
                p["mark_px"] = mark
                p["unrealized_pct"] = upct
                p["unrealized_roe_pct"] = upct * lev
                p["unrealized_pnl_usd"] = notional * upct / 100.0
                p["marked_at"] = _now_ms()
            unrealized += float(p.get("unrealized_pnl_usd", 0.0) or 0.0)
        wallet = float(acc["wallet_balance"])
        equity = wallet + unrealized
        available = equity - used_margin
        return {
            "wallet_balance": round(wallet, 4),
            "equity": round(equity, 4),
            "available": round(available, 4),
            "used_margin": round(used_margin, 4),
            "unrealized_pnl_usd": round(unrealized, 4),
            "open_notional_usd": round(open_notional, 4),
            "realized_pnl_usd": round(wallet - self.state["starting_balance"], 4),
            "total_fees_usd": round(sum(float(f.get("fee_usd", 0.0) or 0.0)
                                        for f in acc["fills"]
                                        if f.get("type") == "close"), 4),
            "open_positions": len(acc["positions"]),
            "resting_orders": len(acc["pending_orders"]),
            "closed_count": acc["closed_count"],
            "fee_pct": fee_pct,
            "round_trip_fills": _round_trip_fills(),
        }

    # -- mutations ----------------------------------------------------------

    def shadow_open(self, *, coin: str, side: str, entry_px: float,
                    size_usd: float, leverage: int, entry_atr_pct: float = 0.0,
                    entry_regime: str = "", analysis_id: str = "",
                    signal_meta: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        """Feed one gated decision to BOTH paper accounts.

        Returns the taker open fill record (or None if skipped) for backward
        compatibility; the maker_shadow account additionally receives a resting
        limit order (best-effort). ``signal_meta`` (P0-A) is a compact
        point-in-time trigger snapshot persisted on the open fill."""
        if not _enabled():
            return None
        if not coin or side not in ("long", "short") or entry_px <= 0 or size_usd <= 0:
            return None
        signal_meta = _sanitize_signal_meta(signal_meta)
        self.reload_if_changed()
        with self._lock:
            taker_fill = self._open_taker(
                coin=coin, side=side, entry_px=entry_px, size_usd=size_usd,
                leverage=leverage, entry_atr_pct=entry_atr_pct,
                entry_regime=entry_regime, analysis_id=analysis_id,
                signal_meta=signal_meta)
            if _maker_enabled():
                try:
                    self._post_maker_order(
                        coin=coin, side=side, post_mid=entry_px, size_usd=size_usd,
                        leverage=leverage, entry_atr_pct=entry_atr_pct,
                        entry_regime=entry_regime, analysis_id=analysis_id,
                        signal_meta=signal_meta)
                except Exception as e:
                    logger.warning(f"[shadow_book] maker post failed {coin}: {e}")
            return taker_fill

    def _open_taker(self, *, coin: str, side: str, entry_px: float, size_usd: float,
                    leverage: int, entry_atr_pct: float, entry_regime: str,
                    analysis_id: str,
                    signal_meta: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        """Immediate mid fill into the taker account."""
        acc = self._account("taker")
        if self._find(acc["positions"], coin, side) is not None:
            # A2 (2026-10-21 redundancy plan): INFO not DEBUG — a gated decision
            # that reached the book but opened no new position is a real
            # admit→fill funnel loss (5 admits → 1 open observed), it must be
            # visible at production log level so 放行数 = 开仓数 + 跳过数.
            # A4: also keep a running would_execute_dropped count for the
            # dashboard / reconciliation without grep-counting free-text logs.
            self._would_execute_dropped += 1
            logger.info(
                f"[shadow_book] skip taker open {coin} {side}: already open "
                f"(would_execute_dropped={self._would_execute_dropped})")
            return None
        if len(acc["positions"]) >= _max_positions():
            logger.info(f"[shadow_book] skip taker open {coin}: max_positions reached")
            return None
        metrics = self._account_metrics("taker")
        lev = max(1, int(leverage))
        if metrics["available"] < size_usd / lev:
            logger.info(
                f"[shadow_book] skip taker open {coin}: need margin {size_usd/lev:.2f} "
                f"> available {metrics['available']:.2f}")
            return None

        # R2: apply taker slippage to the fill (moves price against the taker).
        slip = _taker_slippage_pct()
        fill_px = _apply_slippage(entry_px, side=side, opening=True,
                                  slip_pct=slip)
        size_coin = size_usd / fill_px
        pid = uuid.uuid4().hex[:12]
        opened_at = _now_ms()
        acc["positions"].append({
            "id": pid, "coin": coin, "side": side,
            "entry_px": float(fill_px), "size_usd": float(size_usd),
            "size_coin": float(size_coin), "leverage": lev,
            "entry_atr_pct": float(entry_atr_pct or 0.0),
            "entry_regime": entry_regime or "",
            "analysis_id": analysis_id or "",
            "peak_px": float(fill_px), "opened_at": opened_at,
            "mark_px": float(fill_px),
            "unrealized_pct": 0.0, "unrealized_roe_pct": 0.0,
            "unrealized_pnl_usd": 0.0,
            **(signal_meta or {}),
        })
        try:
            from hermes_trader.agents.dsl_exit import DSLTracker
            self._trackers[("taker", self._key(coin, side))] = DSLTracker(
                coin, side, float(fill_px), opened_at / 1000.0,
                policy=_build_policy(entry_regime), leverage=lev,
                entry_atr_pct=float(entry_atr_pct or 0.0),
                entry_regime=entry_regime or "")
        except Exception as e:
            logger.warning(f"[shadow_book] taker tracker build failed {coin}: {e}")

        fill = {
            "type": "open", "id": uuid.uuid4().hex[:12],
            "position_id": pid, "ts": opened_at,
            "coin": coin, "side": side, "qty": float(size_coin),
            "price": float(fill_px), "mid_px": float(entry_px),
            "slippage_pct": round(slip, 5),
            "notional_usd": float(size_usd),
            "leverage": lev, "fee_usd": 0.0, "fill_model": "taker",
            "analysis_id": analysis_id or "",
            **(signal_meta or {}),
        }
        acc["fills"].append(fill)
        if len(acc["fills"]) > _MAX_FILLS:
            del acc["fills"][: len(acc["fills"]) - _MAX_FILLS]

        snap = self._account_metrics("taker")
        self._append_equity("taker", opened_at, snap, force=True)
        self._save(force=True)
        logger.info(
            f"[shadow_book] taker OPEN {side} {coin} qty={size_coin:g} @ {fill_px:g} "
            f"(mid {entry_px:g}, slip {slip:g}%) "
            f"notional=${size_usd:.2f} lev={lev}x (paper)")
        return fill

    def _post_maker_order(self, *, coin: str, side: str, post_mid: float,
                          size_usd: float, leverage: int, entry_atr_pct: float,
                          entry_regime: str, analysis_id: str,
                          signal_meta: Optional[dict[str, Any]] = None) -> None:
        """Rest a post-only limit (offset inside the spread) in maker_shadow."""
        acc = self._account("maker_shadow")
        # Already filled or already resting for this coin/side → skip.
        if self._find(acc["positions"], coin, side) is not None or \
                self._find(acc["pending_orders"], coin, side) is not None:
            return
        offset_frac = _maker_limit_offset_bps() / 1e4
        if side == "long":
            limit_px = post_mid * (1.0 - offset_frac)
        else:
            limit_px = post_mid * (1.0 + offset_frac)
        acc["pending_orders"].append({
            "id": uuid.uuid4().hex[:12],
            "coin": coin, "side": side,
            "post_mid_px": float(post_mid),
            "limit_px": float(limit_px),
            "size_usd": float(size_usd),
            "leverage": max(1, int(leverage)),
            "entry_atr_pct": float(entry_atr_pct or 0.0),
            "entry_regime": entry_regime or "",
            "analysis_id": analysis_id or "",
            "signal_meta": signal_meta or {},
            "posted_at": _now_ms(),
            # 标记该挂单将由哪个成交模型裁决，撤单审计与之一致（按币 v2 白名单）。
            "fill_model": (
                "maker_shadow_v2" if _maker_uses_v2(coin)
                else "maker_shadow"),
        })

    def _fill_maker_order(self, order: dict[str, Any], fill_px: float,
                          filled_at: int,
                          selection: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Convert a touched maker limit into a filled position + open fill.

        ``selection`` carries the fill-quality metrics computed by the
        simulator (resting bars, maker edge, post-fill mid drift = adverse
        selection). They are pinned on the open fill so the distribution can
        be analysed later; drift may be None when there is no forward bar.
        Caller holds self._lock."""
        selection = selection or {}
        acc = self._account("maker_shadow")
        coin = order["coin"]
        side = order["side"]
        size_usd = float(order["size_usd"])
        lev = max(1, int(order.get("leverage", 1)))
        size_coin = size_usd / fill_px
        pid = uuid.uuid4().hex[:12]
        sig_meta = _sanitize_signal_meta(order.get("signal_meta"))
        acc["positions"].append({
            "id": pid, "coin": coin, "side": side,
            "entry_px": float(fill_px), "size_usd": float(size_usd),
            "size_coin": float(size_coin), "leverage": lev,
            "entry_atr_pct": float(order.get("entry_atr_pct", 0.0) or 0.0),
            "entry_regime": order.get("entry_regime", "") or "",
            "analysis_id": order.get("analysis_id", ""),
            "peak_px": float(fill_px), "opened_at": filled_at,
            "mark_px": float(fill_px),
            "unrealized_pct": 0.0, "unrealized_roe_pct": 0.0,
            "unrealized_pnl_usd": 0.0,
            **sig_meta,
        })
        try:
            from hermes_trader.agents.dsl_exit import DSLTracker
            self._trackers[("maker_shadow", self._key(coin, side))] = DSLTracker(
                coin, side, float(fill_px), filled_at / 1000.0,
                policy=_build_policy(order.get("entry_regime", "")),
                leverage=lev,
                entry_atr_pct=float(order.get("entry_atr_pct", 0.0) or 0.0),
                entry_regime=order.get("entry_regime", "") or "")
        except Exception as e:
            logger.warning(f"[shadow_book] maker tracker build failed {coin}: {e}")

        fill = {
            "type": "open", "id": uuid.uuid4().hex[:12],
            "position_id": pid, "ts": filled_at,
            "coin": coin, "side": side, "qty": float(size_coin),
            "price": float(fill_px), "notional_usd": float(size_usd),
            "leverage": lev, "fee_usd": 0.0,
            # v1 账本沿用 "maker_shadow"；v2 条件成交模型标记 maker_shadow_v2。
            "fill_model": selection.get("fill_model") or "maker_shadow",
            "analysis_id": order.get("analysis_id", ""),
            # maker fill-quality / adverse-selection metrics (may be None)
            "resting_bars": selection.get("resting_bars"),
            "maker_edge_bps": selection.get("maker_edge_bps"),
            "post_fill_mid_drift_bps": selection.get("post_fill_mid_drift_bps"),
            # v2-only conditional-fill audit (None/0 on v1 opens)
            "fill_prob": selection.get("fill_prob"),
            "raw_drift_bps": selection.get("raw_drift_bps"),
            "adv_drift_charge_bps": selection.get("adv_drift_charge_bps"),
            "residual_drift_bps": selection.get("residual_drift_bps"),
            "reject_count": int(selection.get("reject_count") or 0),
            **sig_meta,
        }
        acc["fills"].append(fill)
        # Adverse-selection drift needs the NEXT 1m bar, which does not exist
        # yet when a live cycle fills on the most recent bar. Defer the metric
        # (statistics only) to a later cycle instead of freezing it at None.
        if fill["post_fill_mid_drift_bps"] is None:
            fill_bar_t = selection.get("fill_bar_t")
            try:
                fill_bar_t = int(fill_bar_t) if fill_bar_t is not None else int(filled_at)
            except (TypeError, ValueError):
                fill_bar_t = int(filled_at)
            acc["pending_drift"].append({
                "fill_id": fill["id"],
                "coin": coin,
                "is_buy": side == "long",
                "fill_px": float(fill_px),
                "fill_bar_t": fill_bar_t,
                # Give up after the next few bars; the entry then stays None.
                "deadline_t": fill_bar_t + _MAKER_DRIFT_WAIT_BARS * 60_000,
            })
        logger.info(
            f"[shadow_book] maker FILLED {side} {coin} qty={size_coin:g} @ {fill_px:g} "
            f"notional=${size_usd:.2f} lev={lev}x (paper)")
        return fill

    def _cancel_maker_order(self, order: dict[str, Any], at_ms: int,
                            *, reject_count: int = 0,
                            touched: bool = False) -> None:
        """Drop a TTL-expired maker order and append an audit cancel fill."""
        acc = self._account("maker_shadow")
        acc["fills"].append({
            "type": "cancel", "id": uuid.uuid4().hex[:12],
            "ts": at_ms, "coin": order["coin"], "side": order["side"],
            "limit_px": float(order["limit_px"]),
            "fill_model": order.get("fill_model", "maker_shadow"),
            "analysis_id": order.get("analysis_id", ""),
            # v2 取证：区分"从未触及"与"触及但被队列概率拒绝"，配合零成交告警
            # 防止 v2 接线退化成静默空账本。
            "touched": bool(touched),
            "reject_count": int(reject_count or 0),
        })

    def _resolve_pending_maker_orders(self, mids: dict[str, float]) -> None:
        """For each resting maker order, fetch cached 1m candles and reuse
        ``maker_shadow.simulate_shadow_order`` to decide fill / cancel.

        ``opportunistic=True`` keeps the candle fetch on a short budget and
        never starves the trading path; a missed fetch simply leaves the order
        resting for another cycle. The simulated order is posted at bar idx 0 of
        the fetched window; the window is sized to cover the resting TTL.
        """
        from hermes_trader.client.hl_client import fetch_hl_candles
        from hermes_trader.execution.maker_shadow import (
            ShadowMakerOrder,
            deterministic_shadow_order_v2,
            locate_posted_bar_idx,
            simulate_shadow_order,
        )

        global_v2 = _maker_fill_model() == "v2"
        v2_params = _maker_v2_params() if global_v2 else {}

        acc = self._account("maker_shadow")
        if not acc["pending_orders"]:
            return
        ttl = _maker_ttl_bars()
        now_ms = _now_ms()
        for order in list(acc["pending_orders"]):
            coin = order["coin"]
            side = order["side"]
            posted_at = int(order["posted_at"])
            # Window must span the real post bar through post+TTL. The order
            # has rested <= TTL bars, so (age_bars + TTL + slack) candles always
            # cover it. Do NOT fix to ttl+2: a rest that slipped past one cycle
            # (candle fetch miss / boundary crossing) would age beyond the fixed
            # window, making locate_posted_bar_idx return None forever and the
            # order hang unfilled/uncanceled indefinitely.
            age_bars = max(1, int((now_ms - posted_at) / 60000.0))
            fetch_n = min(age_bars + ttl, _MAX_MAKER_FETCH_BARS)
            try:
                bars = fetch_hl_candles(coin, "1m", fetch_n, opportunistic=True)
            except Exception as e:
                logger.debug(f"[shadow_book] maker candles miss {coin}: {e}")
                # Fallback: an order that aged past TTL must not hang just
                # because the candle fetch failed this cycle.
                if ((now_ms - posted_at) / 60000.0) >= ttl:
                    acc["pending_orders"] = [
                        o for o in acc["pending_orders"]
                        if o["id"] != order["id"]]
                    self._cancel_maker_order(order, now_ms)
                continue
            # Locate the real post bar from the order's timestamp instead of
            # hard-coding idx 0. Until at least one bar has closed after
            # posting, the order simply rests (skip this cycle).
            posted_idx = locate_posted_bar_idx(bars, posted_at)
            if posted_idx is None or posted_idx >= len(bars) - 1:
                # posted bar outside the fetched window (should not happen now
                # that the window scales with age) or still forming: fall back
                # to a straight TTL-age cancel so a stale order can never hang.
                if ((now_ms - posted_at) / 60000.0) >= ttl:
                    acc["pending_orders"] = [
                        o for o in acc["pending_orders"]
                        if o["id"] != order["id"]]
                    self._cancel_maker_order(order, now_ms)
                continue
            sim = ShadowMakerOrder(
                coin=coin,
                is_buy=(side == "long"),
                size=float(order["size_usd"]),
                posted_bar_idx=posted_idx,
                limit_px=float(order["limit_px"]),
                post_mid_px=float(order["post_mid_px"]),
                ttl_bars=min(ttl, len(bars) - 1 - posted_idx),
            )
            use_v2 = global_v2 and _maker_uses_v2(coin)
            if use_v2:
                # 实时影子账本走等概率确定性判定（稳定 hash 抽样，重放可复现，
                # 长期成交率≈base）；带种子蒙特卡洛期望口径仅离线回放使用。
                verdict = deterministic_shadow_order_v2(sim, bars, **v2_params)
            else:
                verdict = simulate_shadow_order(sim, bars)
            if verdict.filled:
                filled_at = int(verdict.fill_ms or _now_ms())
                acc["pending_orders"] = [
                    o for o in acc["pending_orders"] if o["id"] != order["id"]]
                self._fill_maker_order(
                    order, float(verdict.fill_px), filled_at,
                    selection={
                        "resting_bars": verdict.resting_bars,
                        "maker_edge_bps": verdict.maker_edge_bps,
                        "post_fill_mid_drift_bps": verdict.post_fill_mid_drift_bps,
                        "fill_bar_t": verdict.fill_ms,
                        # v2 审计（v1 路径这些键缺省为 None/0，不影响既有聚合）
                        "fill_model": getattr(verdict, "fill_model", "maker_shadow"),
                        "fill_prob": getattr(verdict, "fill_prob", None),
                        "fill_draw": getattr(verdict, "fill_draw", None),
                        "raw_drift_bps": getattr(verdict, "raw_drift_bps", None),
                        "adv_drift_charge_bps": getattr(
                            verdict, "adv_drift_charge_bps", None),
                        "residual_drift_bps": getattr(
                            verdict, "residual_drift_bps", None),
                        "reject_count": getattr(verdict, "reject_count", 0),
                    })
            elif verdict.canceled:
                # TTL of resting time elapsed without a touch.
                age_min = (_now_ms() - posted_at) / 60000.0
                if age_min >= ttl:
                    acc["pending_orders"] = [
                        o for o in acc["pending_orders"] if o["id"] != order["id"]]
                    self._cancel_maker_order(
                        order, _now_ms(),
                        reject_count=getattr(verdict, "reject_count", 0),
                        touched=getattr(verdict, "touch_bar_idx", None) is not None)

    def _resolve_pending_drift(self) -> None:
        """Backfill post-fill mid drift for maker opens that filled before the
        next 1m bar existed. Statistics ONLY: writes ``post_fill_mid_drift_bps``
        onto the existing open fill; never touches fill price, size, or PnL.

        For each pending entry we need the bar starting at ``fill_bar_t + 60s``
        to have CLOSED, then use its (h+l)/2 as the post-fill mid. A fetch miss
        leaves the entry pending for another cycle; past ``deadline_t`` the
        metric is permanently abandoned (stays None) so the queue can't grow.
        Caller holds self._lock. Best-effort, never raises.
        """
        from hermes_trader.client.hl_client import fetch_hl_candles

        acc = self._account("maker_shadow")
        pending = acc.get("pending_drift")
        if not pending:
            return
        now_ms = _now_ms()
        # index open fills by id for the backfill
        fills_by_id = {f.get("id"): f for f in acc["fills"]
                       if f.get("type") == "open"}
        remaining: list[dict[str, Any]] = []
        for pend in list(pending):
            fill_id = pend.get("fill_id")
            target_t = int(pend["fill_bar_t"]) + 60_000
            fill_px = float(pend["fill_px"])
            # Open fill vanished (shouldn't happen) or deadline passed → drop.
            if fill_id not in fills_by_id or now_ms >= int(pend["deadline_t"]):
                continue
            # Need the target bar closed: fetch a small window ending at/after it.
            age_bars = max(2, int((now_ms - int(pend["fill_bar_t"])) / 60_000.0) + 1)
            try:
                bars = fetch_hl_candles(
                    pend["coin"], "1m", min(age_bars, _MAX_MAKER_FETCH_BARS),
                    opportunistic=True)
            except Exception as e:
                logger.debug(f"[shadow_book] drift candles miss {pend['coin']}: {e}")
                remaining.append(pend)
                continue
            post_bar = next((b for b in bars if int(b.t) == target_t), None)
            # Bar not present, or it is the still-forming last bar → wait.
            if post_bar is None or int(post_bar.t) + 60_000 > now_ms:
                remaining.append(pend)
                continue
            post_mid = (float(post_bar.h) + float(post_bar.l)) / 2.0
            if post_mid <= 0 or fill_px <= 0:
                continue  # drop the junk entry rather than looping forever
            if pend.get("is_buy", True):
                drift = (fill_px - post_mid) / fill_px * 1e4
            else:
                drift = (post_mid - fill_px) / fill_px * 1e4
            fills_by_id[fill_id]["post_fill_mid_drift_bps"] = round(float(drift), 4)
        acc["pending_drift"] = remaining

    def _close_position(self, acct: str, pos: dict[str, Any], exit_px: float,
                        reason: str, hold_min: float = 0.0,
                        mfe_pct: float = 0.0,
                        gap_spot_pct: float = 0.0,
                        gap_capped: bool = False,
                        policy_snapshot: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Book a virtual close + realized PnL. Fee uses the account's口径."""
        acc = self._account(acct)
        coin = pos["coin"]
        side = pos["side"]
        entry_px = float(pos["entry_px"])
        notional = float(pos["size_usd"])
        lev = max(1, int(pos.get("leverage", 1)))

        fee_pct = _maker_fee_pct() if acct == "maker_shadow" else _taker_fee_pct()
        # R2: taker paper exits also pay slippage (maker fills are posted).
        close_slip = 0.0 if acct == "maker_shadow" else _taker_slippage_pct()
        fill_exit_px = _apply_slippage(exit_px, side=side, opening=False,
                                       slip_pct=close_slip)
        if side == "long":
            spot_pct = (fill_exit_px - entry_px) / entry_px * 100.0
        else:
            spot_pct = (entry_px - fill_exit_px) / entry_px * 100.0
        gross_pnl = notional * spot_pct / 100.0
        fee_usd = notional * fee_pct / 100.0 * _round_trip_fills()
        net_pnl = gross_pnl - fee_usd
        roe_pct = spot_pct * lev - fee_pct * _round_trip_fills() * lev

        closed_at = _now_ms()
        close_fill = {
            "type": "close", "id": uuid.uuid4().hex[:12],
            "position_id": pos["id"], "ts": closed_at,
            "coin": coin, "side": side,
            "qty": float(pos["size_coin"]), "entry_px": entry_px,
            "price": float(fill_exit_px), "mid_px": float(exit_px),
            "slippage_pct": round(close_slip, 5),
            # Audit 2026-10-16: real gap-through overshoot on max_loss exits.
            # gap_spot_pct>0 means the confirming mark gapped past the exchange
            # backup-SL trigger and the paper fill was capped there; the overshoot
            # is recorded (not folded silently into slippage_pct) for precise
            # tracking. gap_capped=False on a max_loss means ATR was missing at
            # entry, so no live backup-SL existed and any real gap is unknown.
            "gap_spot_pct": round(float(gap_spot_pct or 0.0), 4),
            "gap_capped": bool(gap_capped),
            "notional_usd": notional,
            "leverage": lev, "fee_usd": round(fee_usd, 6),
            "spot_pct": round(spot_pct, 4),
            "realized_pnl_pct": round(roe_pct, 4),   # leveraged ROE %
            "gross_pnl_usd": round(gross_pnl, 6),
            "realized_pnl_usd": round(net_pnl, 6),
            "reason": reason or "",
            "hold_minutes": round(hold_min, 2),
            "mfe_pct": round(mfe_pct, 4),
            # L5 observability (2026-10-21 redundancy plan): pin the EFFECTIVE
            # exit policy the DSL tracker actually ran, so realized exits can be
            # split by regime/clock without guessing from dates. Pure attribution;
            # does not touch PnL or the exit decision.
            "exit_policy": dict(policy_snapshot or {}),
            "fill_model": acct,
            "entry_regime": pos.get("entry_regime", ""),
            "analysis_id": pos.get("analysis_id", ""),
            "opened_at": pos.get("opened_at"),
        }
        acc["fills"].append(close_fill)
        if len(acc["fills"]) > _MAX_FILLS:
            del acc["fills"][: len(acc["fills"]) - _MAX_FILLS]

        acc["wallet_balance"] = float(acc["wallet_balance"]) + net_pnl
        acc["closed_count"] = int(acc["closed_count"]) + 1
        acc["positions"] = [p for p in acc["positions"] if p["id"] != pos["id"]]
        self._trackers.pop((acct, self._key(coin, side)), None)

        snap = self._account_metrics(acct, fee_pct=fee_pct)
        self._append_equity(acct, closed_at, snap, force=True)
        logger.info(
            f"[shadow_book] {acct} CLOSE {side} {coin} @ {exit_px:g} reason={reason} "
            f"spot={spot_pct:+.2f}% roe={roe_pct:+.2f}% pnl=${net_pnl:+.2f} "
            f"fee=${fee_usd:.3f} hold={hold_min:.1f}m")
        # Absorbed from TradingAgents: in SHADOW mode closes settle here rather
        # than executor.py, so schedule the background decision review from this
        # path too — otherwise the learn-from-outcomes loop never runs while in
        # shadow. Off the critical path; INERT and best-effort.
        try:
            from hermes_trader.agents.reflection import maybe_reflect_async

            maybe_reflect_async(_reflection_row(acct, close_fill))
        except Exception as _rf_e:
            logger.warning("[reflection] schedule failed for %s: %s",
                           coin, _rf_e)
        return close_fill

    def mark_to_market(self, mids: dict[str, float],
                       index_prices: Optional[dict[str, float]] = None) -> list[dict[str, Any]]:
        """Mark all FILLED positions to live mids and run DSL exits; resolve
        resting maker orders against cached 1m candles.

        Returns the list of virtual close fills fired this call across BOTH
        accounts (may be empty). Coins absent from ``mids`` are skipped.
        """
        if not mids:
            return []
        self.reload_if_changed()
        closed: list[dict[str, Any]] = []
        with self._lock:
            if _maker_enabled():
                try:
                    self._resolve_pending_maker_orders(mids)
                except Exception as e:
                    logger.warning(f"[shadow_book] maker resolve failed: {e}")
                try:
                    self._resolve_pending_drift()
                except Exception as e:
                    logger.warning(f"[shadow_book] maker drift backfill failed: {e}")
            index_prices = index_prices or {}
            for acct in ACCOUNTS:
                acc = self._account(acct)
                if not acc["positions"]:
                    continue
                for pos in list(acc["positions"]):
                    coin = pos["coin"]
                    side = pos["side"]
                    mark = mids.get(coin)
                    if mark is None or mark <= 0:
                        continue
                    idx = index_prices.get(coin)
                    tracker = self._trackers.get((acct, self._key(coin, side)))
                    exit_now = False
                    reason = ""
                    hold_min = (time.time() - float(pos["opened_at"]) / 1000.0) / 60.0
                    mfe = 0.0
                    if tracker is not None:
                        try:
                            v = tracker.check(float(mark), float(idx) if idx else None)
                            exit_now = bool(getattr(v, "exit", False))
                            reason = str(getattr(v, "reason", "") or "")
                            hold_min = float(getattr(v, "hold_min", hold_min) or hold_min)
                            mfe = float(getattr(v, "mfe_pct", 0.0) or 0.0)
                        except Exception as e:
                            logger.warning(f"[shadow_book] dsl check failed {coin}: {e}")
                    if exit_now:
                        # F6 fill parity: a normal live exit is a software IOC
                        # market fill at the confirming mark. The ONLY divergence
                        # is a gap-through past the exchange backup-SL trigger;
                        # cap the paper fill there too.
                        #
                        # P0-4 (recheck §7.1): 历史 max_loss 出场 PnL 未把 gap-through
                        # overshoot 折入，亏损被低估。但 gap 是否折入 PnL 涉及实盘
                        # backup-SL 的执行语义（限价 vs 市价），F6 契约锁定纸面成交
                        # cap 到 trigger 与实盘 backup-SL 对齐。此处保持 F6 行为：
                        # 成交价仍 cap 到 trigger；gap_spot_pct 单独记录 overshoot
                        # 供 outcome_baseline 的 slippage_analysis 独立核算止损滑点
                        # 成本，避免与 flat slippage_pct 混淆。是否把 gap 折入
                        # 核心 PnL 属实盘执行语义裁决，不由本模块单方面决定。
                        fill_px = float(mark)
                        gap_spot_pct = 0.0
                        gap_capped = False
                        if reason.startswith("max_loss"):
                            trig = _backup_sl_trigger_px(
                                coin=coin, side=side, entry_px=float(pos["entry_px"]),
                                entry_atr_pct=float(pos.get("entry_atr_pct", 0.0) or 0.0))
                            if trig is not None:
                                gapped = (mark < trig) if side == "long" else (mark > trig)
                                if gapped:
                                    fill_px = float(trig)
                                    gap_capped = True
                                    # overshoot = how far mark gapped past the
                                    # backup trigger, in spot % of entry.
                                    gap_spot_pct = abs(mark - trig) / float(pos["entry_px"]) * 100.0
                            else:
                                # ATR missing: no live backup-SL, so the full mark
                                # is used and any real gap is UNKNOWN (uncapped).
                                gap_capped = False
                        closed.append(self._close_position(
                            acct, pos, fill_px, reason or "dsl_exit",
                            hold_min=hold_min, mfe_pct=mfe,
                            gap_spot_pct=gap_spot_pct,
                            gap_capped=gap_capped,
                            policy_snapshot=_tracker_policy_snapshot(tracker)))
                    else:
                        if side == "long":
                            upct = (mark - float(pos["entry_px"])) / float(pos["entry_px"]) * 100.0
                        else:
                            upct = (float(pos["entry_px"]) - mark) / float(pos["entry_px"]) * 100.0
                        pos["mark_px"] = float(mark)
                        pos["unrealized_pct"] = upct
                        pos["unrealized_roe_pct"] = upct * max(1, int(pos.get("leverage", 1)))
                        pos["unrealized_pnl_usd"] = float(pos["size_usd"]) * upct / 100.0
                        pos["marked_at"] = _now_ms()

                snap = self._account_metrics(acct)
                now_mono = time.monotonic()
                if (now_mono - self._last_equity_ts) >= _EQUITY_MIN_INTERVAL_S:
                    self._append_equity(acct, _now_ms(), snap, force=False)
            self._last_equity_ts = time.monotonic()
            self._save(force=False)
        return closed

    def _append_equity(self, acct: str, ts_ms: int, snap: dict[str, Any],
                       force: bool) -> None:
        curve = self._account(acct)["equity_curve"]
        if not force and curve and ts_ms - curve[-1].get("ts", 0) < _EQUITY_MIN_INTERVAL_S * 1000:
            return
        curve.append({
            "ts": ts_ms,
            "equity": snap["equity"],
            "wallet_balance": snap["wallet_balance"],
            "unrealized_pnl_usd": snap["unrealized_pnl_usd"],
            "open_positions": snap["open_positions"],
        })
        if len(curve) > _MAX_EQUITY_POINTS:
            del curve[: len(curve) - _MAX_EQUITY_POINTS]
        self._last_equity_ts = time.monotonic()

    def close_now(self, coin: str, side: Optional[str] = None,
                  exit_px: Optional[float] = None,
                  reason: str = "manual_close") -> Optional[dict[str, Any]]:
        """Force-close a TAKER paper position (operator / API). The maker_shadow
        account exits on its own simulated DSL signals, not via manual close."""
        self.reload_if_changed()
        with self._lock:
            acc = self._account("taker")
            for p in list(acc["positions"]):
                if p["coin"] != coin:
                    continue
                if side and p["side"] != side:
                    continue
                px = float(exit_px) if exit_px else float(p.get("mark_px") or p["entry_px"])
                hold = (time.time() - float(p["opened_at"]) / 1000.0) / 60.0
                _trk = self._trackers.get(("taker", self._key(p["coin"], p["side"])))
                fill = self._close_position(
                    "taker", p, px, reason, hold_min=hold,
                    policy_snapshot=_tracker_policy_snapshot(_trk))
                self._save(force=True)
                return fill
        return None

    def reset(self, starting_balance: Optional[float] = None) -> dict[str, Any]:
        """Wipe BOTH paper accounts back to a fresh bankroll (operator)."""
        with self._lock:
            old_closed = sum(
                self.state["accounts"][a]["closed_count"] for a in ACCOUNTS)
            self._trackers.clear()
            self.state = self._fresh_state()
            if starting_balance is not None and starting_balance > 0:
                self.state["starting_balance"] = float(starting_balance)
                for acct in ACCOUNTS:
                    acc = self.state["accounts"][acct]
                    acc["wallet_balance"] = float(starting_balance)
            self._last_snapshot = {}
            self._save(force=True)
            logger.warning(f"[shadow_book] RESET — wiped {old_closed} closes; "
                           f"fresh bankroll={self.state['starting_balance']:.2f}")
            return {"ok": True, "closed_wiped": old_closed,
                    "starting_balance": self.state["starting_balance"]}

    def deposit(self, amount: float) -> Optional[dict[str, Any]]:
        """Add virtual funds to BOTH accounts WITHOUT touching positions/fills.

        The bankroll baseline and each wallet are raised by ``amount`` so the
        cash is never counted as trading profit; ``available`` grows by the
        deposited amount. Both口径 receive identical notional contributions
        so the comparison stays apples-to-apples.
        """
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return None
        if amt <= 0:
            return None
        self.reload_if_changed()
        with self._lock:
            self.state["starting_balance"] = float(self.state["starting_balance"]) + amt
            taker_snap = None
            for acct in ACCOUNTS:
                acc = self.state["accounts"][acct]
                acc["wallet_balance"] = float(acc["wallet_balance"]) + amt
                snap = self._account_metrics(acct)
                self._append_equity(acct, _now_ms(), snap, force=True)
                if acct == "taker":
                    taker_snap = snap
            self._save(force=True)
            logger.warning(
                f"[shadow_book] DEPOSIT +{amt:.2f} (paper) both accounts; "
                f"taker available={taker_snap['available']:.2f}")
            return {
                "ok": True, "deposited": round(amt, 4),
                "wallet_balance": taker_snap["wallet_balance"],
                "equity": taker_snap["equity"],
                "available": taker_snap["available"],
                "open_positions": taker_snap["open_positions"],
            }

    # -- read views ---------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        """All accounts' metrics + taker positions (the default account view)."""
        self.reload_if_changed()
        with self._lock:
            accounts = {}
            for acct in ACCOUNTS:
                accounts[acct] = self._account_metrics(acct)
            taker = self.state["accounts"]["taker"]
            maker = self.state["accounts"]["maker_shadow"]
            return {
                "enabled": _enabled(),
                "maker_shadow_enabled": _maker_enabled(),
                "starting_balance": self.state["starting_balance"],
                # Flattened taker fields keep the existing payload shape.
                **accounts["taker"],
                "positions": [dict(p) for p in taker["positions"]],
                "accounts": accounts,
                "maker_positions": [dict(p) for p in maker["positions"]],
                "maker_resting_orders": [dict(o) for o in maker["pending_orders"]],
                "last_mark_ts": max(
                    (int(p.get("marked_at", 0) or 0) for p in taker["positions"]),
                    default=0),
                "updated_at": _now_ms(),
            }

    def get_trades(self, limit: int = 200) -> dict[str, Any]:
        """Return taker fills plus a parallel maker_shadow fill list.

        Maker open fills carry the adverse-selection metrics; the two lists
        let the dashboard render a side-by-side feed."""
        self.reload_if_changed()
        with self._lock:
            taker_fills = self.state["accounts"]["taker"]["fills"]
            maker_fills = self.state["accounts"]["maker_shadow"]["fills"]
            t = list(taker_fills)[-limit:]
            m = list(maker_fills)[-limit:]
            t.reverse()
            m.reverse()
            return {
                "trades": t, "total": len(taker_fills),
                "maker_trades": m, "maker_total": len(maker_fills),
            }

    def get_equity_curve(self) -> dict[str, Any]:
        """Taker curve plus a parallel maker_shadow curve for对照."""
        self.reload_if_changed()
        with self._lock:
            taker = self.state["accounts"]["taker"]
            maker = self.state["accounts"]["maker_shadow"]
            return {
                "starting_balance": float(self.state["starting_balance"]),
                "points": list(taker["equity_curve"]),
                "maker_points": list(maker["equity_curve"]),
            }

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            out = self._account_stats("taker")
            maker_stats = self._account_stats("maker_shadow")
            out["maker_shadow"] = maker_stats
            return out

    def _account_stats(self, acct: str) -> dict[str, Any]:
        """Win rate / PnL / hold analytics for one account from its close
        fills. Open/cancel fills are excluded from trade counts."""
        acc = self._account(acct)
        closes = [f for f in acc["fills"] if f.get("type") == "close"]
        n = len(closes)
        wins = [c for c in closes if float(c.get("realized_pnl_usd", 0.0)) > 0]
        losses = [c for c in closes if float(c.get("realized_pnl_usd", 0.0)) <= 0]
        gross_win = sum(float(c.get("realized_pnl_usd", 0.0)) for c in wins)
        gross_loss = sum(float(c.get("realized_pnl_usd", 0.0)) for c in losses)
        total_fee = sum(float(c.get("fee_usd", 0.0) or 0.0) for c in closes)
        total_pnl = sum(float(c.get("realized_pnl_usd", 0.0)) for c in closes)
        holds = [float(c.get("hold_minutes", 0.0) or 0.0) for c in closes]
        best = max((float(c.get("realized_pnl_usd", 0.0)) for c in closes), default=0.0)
        worst = min((float(c.get("realized_pnl_usd", 0.0)) for c in closes), default=0.0)
        start = float(self.state["starting_balance"])
        equity = float(acc["wallet_balance"]) + sum(
            float(p.get("unrealized_pnl_usd", 0.0) or 0.0)
            for p in acc["positions"])
        out = {
            "closed_trades": n,
            "open_positions": len(acc["positions"]),
            "resting_orders": len(acc["pending_orders"]),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / n, 2) if n else 0.0,
            "total_realized_pnl_usd": round(total_pnl, 4),
            "total_fees_usd": round(total_fee, 4),
            "gross_profit_usd": round(gross_win, 4),
            "gross_loss_usd": round(gross_loss, 4),
            "profit_factor": round(gross_win / abs(gross_loss), 2) if gross_loss < 0 else None,
            "avg_win_usd": round(gross_win / len(wins), 4) if wins else 0.0,
            "avg_loss_usd": round(gross_loss / len(losses), 4) if losses else 0.0,
            "best_trade_usd": round(best, 4),
            "worst_trade_usd": round(worst, 4),
            "avg_hold_minutes": round(sum(holds) / n, 2) if n else 0.0,
            "equity_usd": round(equity, 4),
            "total_return_pct": round(100.0 * (equity - start) / start, 4) if start else 0.0,
        }
        if acct == "maker_shadow":
            out["adverse_selection"] = self._adverse_selection_stats(acc)
        return out

    @staticmethod
    def _adverse_selection_stats(acc: dict[str, Any]) -> dict[str, Any]:
        """Aggregate maker fill-quality metrics from open fills + cancel count.

        Pins the maker go/no-go evidence: fill rate (fills vs fills+cuts),
        resting time, captured maker edge, and post-fill adverse drift.
        Edge/drift values of None (no forward bar) are excluded from means.
        v2 opens (fill_model=maker_shadow_v2) additionally report mean queue
        penetration probability and touch-reject count, plus a per-model split
        so v1/v2 can be compared on the same book without cross-contamination.
        """
        opens = [f for f in acc["fills"] if f.get("type") == "open"]
        cancels = [f for f in acc["fills"] if f.get("type") == "cancel"]
        n_fill = len(opens)
        n_cancel = len(cancels)
        edges = [float(f["maker_edge_bps"]) for f in opens
                 if f.get("maker_edge_bps") is not None]
        drifts = [float(f["post_fill_mid_drift_bps"]) for f in opens
                  if f.get("post_fill_mid_drift_bps") is not None]
        rests = [float(f["resting_bars"]) for f in opens
                 if f.get("resting_bars") is not None]
        decided = n_fill + n_cancel

        def _mean(xs: list[float]) -> Optional[float]:
            return round(sum(xs) / len(xs), 3) if xs else None

        v2_opens = [f for f in opens if f.get("fill_model") == "maker_shadow_v2"]
        v2_cancels = [f for f in cancels
                      if f.get("fill_model") == "maker_shadow_v2"]
        v2_probs = [float(f["fill_prob"]) for f in v2_opens
                    if f.get("fill_prob") is not None]
        v2_resid = [float(f["residual_drift_bps"]) for f in v2_opens
                    if f.get("residual_drift_bps") is not None]
        v2_rejects = [int(f.get("reject_count") or 0) for f in v2_opens]
        v2_touched_cancels = sum(
            1 for f in v2_cancels if f.get("touched")
            or int(f.get("reject_count") or 0) > 0)
        v2_decided = len(v2_opens) + len(v2_cancels)
        v2 = None
        if v2_decided:
            # Fail-loud：v2 已裁决足够多订单、且确有挂单被价格触及，却 0 成交，
            # 说明确定性接线退化（历史缺陷：p 恒<1 导致永不成交）。>=20 笔后
            # 置告警，避免冷启动小样本误报。
            zero_fill_with_touches = (
                len(v2_opens) == 0 and v2_touched_cancels >= 20)
            v2 = {
                "fills": len(v2_opens),
                "cancels": len(v2_cancels),
                "touched_cancels": v2_touched_cancels,
                "fill_rate_pct": round(100.0 * len(v2_opens) / v2_decided, 2),
                "zero_fill_warning": zero_fill_with_touches,
                "avg_fill_prob": _mean(v2_probs),
                # 已计入价格后残留的逆选择；kappa=1 时应≈0（S1 Go 判据）。
                "avg_residual_drift_bps": _mean(v2_resid),
                "avg_reject_count": _mean([float(x) for x in v2_rejects]),
                # 被概率拒绝后仍成交的笔数占比（逆选择存活证据）
                "fills_after_reject_pct": round(
                    100.0 * sum(1 for r in v2_rejects if r > 0)
                    / len(v2_opens), 2) if v2_opens else 0.0,
            }
        return {
            "fills": n_fill,
            "cancels": n_cancel,
            "fill_rate_pct": round(100.0 * n_fill / decided, 2) if decided else 0.0,
            "avg_maker_edge_bps": _mean(edges),
            "avg_post_fill_drift_bps": _mean(drifts),
            "avg_resting_bars": _mean(rests),
            "v2": v2,
        }

    def would_execute_dropped(self) -> int:
        """A4: live-loop count of admitted opens dropped as already-open."""
        return int(self._would_execute_dropped)


# ---------------------------------------------------------------------------
# module-level singleton + thin public API (safe to call from executor/loop)
# ---------------------------------------------------------------------------

_book: Optional[ShadowBook] = None
_book_lock = threading.Lock()


def get_book() -> ShadowBook:
    global _book
    if _book is None:
        with _book_lock:
            if _book is None:
                _book = ShadowBook()
    return _book


def shadow_open(**kwargs: Any) -> Optional[dict[str, Any]]:
    try:
        return get_book().shadow_open(**kwargs)
    except Exception as e:
        logger.error(f"[shadow_book] shadow_open failed: {e}")
        return None


def mark_to_market(mids: dict[str, float],
                   index_prices: Optional[dict[str,float]] = None) -> list[dict[str, Any]]:
    try:
        return get_book().mark_to_market(mids, index_prices)
    except Exception as e:
        logger.error(f"[shadow_book] mark_to_market failed: {e}")
        return []


def get_account() -> dict[str, Any]:
    return get_book().get_account()


def get_trades(limit: int = 200) -> dict[str, Any]:
    return get_book().get_trades(limit=limit)


def get_equity_curve() -> dict[str, Any]:
    return get_book().get_equity_curve()


def get_stats() -> dict[str, Any]:
    return get_book().get_stats()


def reset(starting_balance: Optional[float] = None) -> dict[str, Any]:
    return get_book().reset(starting_balance=starting_balance)


def deposit(amount: float) -> Optional[dict[str, Any]]:
    return get_book().deposit(amount)


def close_now(coin: str, side: Optional[str] = None,
              exit_px: Optional[float] = None, reason: str = "manual_close") -> Optional[dict[str, Any]]:
    try:
        return get_book().close_now(coin, side=side, exit_px=exit_px, reason=reason)
    except Exception as e:
        logger.error(f"[shadow_book] close_now failed: {e}")
        return None
