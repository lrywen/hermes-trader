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
        return float(cfg_get("execution.taker_fee_pct", 0.025))
    except Exception:
        return 0.025


def _maker_fee_pct() -> float:
    """Per-fill maker fee in PERCENT. Prefer the shadow_book override; fall back
    to the live execution maker fee, then to HL's standard 0.01%."""
    c = _shadow_cfg()
    try:
        if c.get("maker_fee_pct") is not None:
            return float(c["maker_fee_pct"])
    except (TypeError, ValueError):
        pass
    try:
        from hermes_trader.agents.config_store import cfg_get
        return float(cfg_get("execution.maker_fee_pct", 0.01))
    except Exception:
        return 0.01


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


def _maker_candle_lookback() -> int:
    """1m candles fetched per mark (covers the resting TTL plus the post bar)."""
    return _maker_ttl_bars() + 5


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
        import dataclasses

        from hermes_trader.agents.config_store import read_agent_config
        from hermes_trader.agents.dsl_exit import RetraceTier
        from hermes_trader.agents.executor import resolve_regime_clocks, select_exit_params
        dsl = read_agent_config().get("dsl_exit", {}) or {}
        _prot, _retrace, _tiers_raw, _ml_pct, _ml_roe, _label = \
            select_exit_params(dsl, regime or "neutral")
        _tiers = [RetraceTier(**t) for t in _tiers_raw] if _tiers_raw else None
        _clocks = resolve_regime_clocks(dsl, regime or "neutral")
        from hermes_trader.agents.dsl_exit import ExitPolicy as _EP
        return dataclasses.replace(
            base,
            max_loss_pct=float(_ml_pct),
            max_loss_roe_pct=float(_ml_roe),
            protect_pct=float(_prot),
            retrace_threshold=float(_retrace),
            hard_timeout_minutes=float(_clocks["hard_timeout_minutes"]),
            stale_flat_timeout_minutes=float(_clocks["stale_flat_timeout_minutes"]),
            phase2_tiers=_tiers if _tiers else _EP().phase2_tiers,
        )
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

    width_pct = min(max(entry_atr_pct*mult, floor), ceiling), mirroring
    executor._place_backup_sl / _resolve_sl_width_config. Returns None when
    inputs are unusable. Slip-widening is omitted (it needs live
    avg_exit_slip_bps the paper book cannot observe); second-order.
    """
    try:
        if entry_px <= 0:
            return None
        from hermes_trader.agents.config_store import cfg_get
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
        atr_pct = float(entry_atr_pct or 0.0)
        width = min(max(atr_pct * mult, floor), ceiling)
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
                    entry_regime: str = "", analysis_id: str = "") -> Optional[dict[str, Any]]:
        """Feed one gated decision to BOTH paper accounts.

        Returns the taker open fill record (or None if skipped) for backward
        compatibility; the maker_shadow account additionally receives a resting
        limit order (best-effort)."""
        if not _enabled():
            return None
        if not coin or side not in ("long", "short") or entry_px <= 0 or size_usd <= 0:
            return None
        self.reload_if_changed()
        with self._lock:
            taker_fill = self._open_taker(
                coin=coin, side=side, entry_px=entry_px, size_usd=size_usd,
                leverage=leverage, entry_atr_pct=entry_atr_pct,
                entry_regime=entry_regime, analysis_id=analysis_id)
            if _maker_enabled():
                try:
                    self._post_maker_order(
                        coin=coin, side=side, post_mid=entry_px, size_usd=size_usd,
                        leverage=leverage, entry_atr_pct=entry_atr_pct,
                        entry_regime=entry_regime, analysis_id=analysis_id)
                except Exception as e:
                    logger.warning(f"[shadow_book] maker post failed {coin}: {e}")
            return taker_fill

    def _open_taker(self, *, coin: str, side: str, entry_px: float, size_usd: float,
                    leverage: int, entry_atr_pct: float, entry_regime: str,
                    analysis_id: str) -> Optional[dict[str, Any]]:
        """Immediate mid fill into the taker account."""
        acc = self._account("taker")
        if self._find(acc["positions"], coin, side) is not None:
            logger.debug(f"[shadow_book] skip taker open {coin} {side}: already open")
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

        size_coin = size_usd / entry_px
        pid = uuid.uuid4().hex[:12]
        opened_at = _now_ms()
        acc["positions"].append({
            "id": pid, "coin": coin, "side": side,
            "entry_px": float(entry_px), "size_usd": float(size_usd),
            "size_coin": float(size_coin), "leverage": lev,
            "entry_atr_pct": float(entry_atr_pct or 0.0),
            "entry_regime": entry_regime or "",
            "analysis_id": analysis_id or "",
            "peak_px": float(entry_px), "opened_at": opened_at,
            "mark_px": float(entry_px),
            "unrealized_pct": 0.0, "unrealized_roe_pct": 0.0,
            "unrealized_pnl_usd": 0.0,
        })
        try:
            from hermes_trader.agents.dsl_exit import DSLTracker
            self._trackers[("taker", self._key(coin, side))] = DSLTracker(
                coin, side, float(entry_px), opened_at / 1000.0,
                policy=_build_policy(entry_regime), leverage=lev,
                entry_atr_pct=float(entry_atr_pct or 0.0),
                entry_regime=entry_regime or "")
        except Exception as e:
            logger.warning(f"[shadow_book] taker tracker build failed {coin}: {e}")

        fill = {
            "type": "open", "id": uuid.uuid4().hex[:12],
            "position_id": pid, "ts": opened_at,
            "coin": coin, "side": side, "qty": float(size_coin),
            "price": float(entry_px), "notional_usd": float(size_usd),
            "leverage": lev, "fee_usd": 0.0, "fill_model": "taker",
            "analysis_id": analysis_id or "",
        }
        acc["fills"].append(fill)
        if len(acc["fills"]) > _MAX_FILLS:
            del acc["fills"][: len(acc["fills"]) - _MAX_FILLS]

        snap = self._account_metrics("taker")
        self._append_equity("taker", opened_at, snap, force=True)
        self._save(force=True)
        logger.info(
            f"[shadow_book] taker OPEN {side} {coin} qty={size_coin:g} @ {entry_px:g} "
            f"notional=${size_usd:.2f} lev={lev}x (paper)")
        return fill

    def _post_maker_order(self, *, coin: str, side: str, post_mid: float,
                          size_usd: float, leverage: int, entry_atr_pct: float,
                          entry_regime: str, analysis_id: str) -> None:
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
            "posted_at": _now_ms(),
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
            "leverage": lev, "fee_usd": 0.0, "fill_model": "maker_shadow",
            "analysis_id": order.get("analysis_id", ""),
            # maker fill-quality / adverse-selection metrics (may be None)
            "resting_bars": selection.get("resting_bars"),
            "maker_edge_bps": selection.get("maker_edge_bps"),
            "post_fill_mid_drift_bps": selection.get("post_fill_mid_drift_bps"),
        }
        acc["fills"].append(fill)
        logger.info(
            f"[shadow_book] maker FILLED {side} {coin} qty={size_coin:g} @ {fill_px:g} "
            f"notional=${size_usd:.2f} lev={lev}x (paper)")
        return fill

    def _cancel_maker_order(self, order: dict[str, Any], at_ms: int) -> None:
        """Drop a TTL-expired maker order and append an audit cancel fill."""
        acc = self._account("maker_shadow")
        acc["fills"].append({
            "type": "cancel", "id": uuid.uuid4().hex[:12],
            "ts": at_ms, "coin": order["coin"], "side": order["side"],
            "limit_px": float(order["limit_px"]),
            "fill_model": "maker_shadow",
            "analysis_id": order.get("analysis_id", ""),
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
        from hermes_trader.execution.maker_shadow import ShadowMakerOrder, simulate_shadow_order

        acc = self._account("maker_shadow")
        if not acc["pending_orders"]:
            return
        lookback = _maker_candle_lookback()
        ttl = _maker_ttl_bars()
        for order in list(acc["pending_orders"]):
            coin = order["coin"]
            side = order["side"]
            try:
                bars = fetch_hl_candles(coin, "1m", lookback, opportunistic=True)
            except Exception as e:
                logger.debug(f"[shadow_book] maker candles miss {coin}: {e}")
                continue
            # Need at least the post bar + one forward bar to judge a touch.
            if len(bars) < 2:
                continue
            sim = ShadowMakerOrder(
                coin=coin,
                is_buy=(side == "long"),
                size=float(order["size_usd"]),
                posted_bar_idx=0,
                limit_px=float(order["limit_px"]),
                post_mid_px=float(order["post_mid_px"]),
                ttl_bars=min(ttl, len(bars) - 1),
            )
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
                    })
            elif verdict.canceled:
                # TTL of resting time elapsed without a touch.
                age_min = (_now_ms() - int(order["posted_at"])) / 60000.0
                if age_min >= ttl:
                    acc["pending_orders"] = [
                        o for o in acc["pending_orders"] if o["id"] != order["id"]]
                    self._cancel_maker_order(order, _now_ms())

    def _close_position(self, acct: str, pos: dict[str, Any], exit_px: float,
                        reason: str, hold_min: float = 0.0,
                        mfe_pct: float = 0.0) -> dict[str, Any]:
        """Book a virtual close + realized PnL. Fee uses the account's口径."""
        acc = self._account(acct)
        coin = pos["coin"]
        side = pos["side"]
        entry_px = float(pos["entry_px"])
        notional = float(pos["size_usd"])
        lev = max(1, int(pos.get("leverage", 1)))

        fee_pct = _maker_fee_pct() if acct == "maker_shadow" else _taker_fee_pct()
        if side == "long":
            spot_pct = (exit_px - entry_px) / entry_px * 100.0
        else:
            spot_pct = (entry_px - exit_px) / entry_px * 100.0
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
            "price": float(exit_px), "notional_usd": notional,
            "leverage": lev, "fee_usd": round(fee_usd, 6),
            "spot_pct": round(spot_pct, 4),
            "realized_pnl_pct": round(roe_pct, 4),   # leveraged ROE %
            "gross_pnl_usd": round(gross_pnl, 6),
            "realized_pnl_usd": round(net_pnl, 6),
            "reason": reason or "",
            "hold_minutes": round(hold_min, 2),
            "mfe_pct": round(mfe_pct, 4),
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
                        fill_px = float(mark)
                        if reason.startswith("max_loss"):
                            trig = _backup_sl_trigger_px(
                                coin=coin, side=side, entry_px=float(pos["entry_px"]),
                                entry_atr_pct=float(pos.get("entry_atr_pct", 0.0) or 0.0))
                            if trig is not None:
                                gapped = (mark < trig) if side == "long" else (mark > trig)
                                if gapped:
                                    fill_px = float(trig)
                        closed.append(self._close_position(
                            acct, pos, fill_px, reason or "dsl_exit",
                            hold_min=hold_min, mfe_pct=mfe))
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
                fill = self._close_position("taker", p, px, reason, hold_min=hold)
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
        Edge/drift values of None (no forward bar) are excluded from means."""
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
        return {
            "fills": n_fill,
            "cancels": n_cancel,
            "fill_rate_pct": round(100.0 * n_fill / decided, 2) if decided else 0.0,
            "avg_maker_edge_bps": round(sum(edges) / len(edges), 3) if edges else None,
            "avg_post_fill_drift_bps": round(sum(drifts) / len(drifts), 3) if drifts else None,
            "avg_resting_bars": round(sum(rests) / len(rests), 2) if rests else None,
        }


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
