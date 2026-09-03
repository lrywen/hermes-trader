"""SHADOW-mode paper-trading ledger (virtual account, live-marked).

When the engine runs in mode=SHADOW, a decision that passes EVERY risk gate
normally just returns ``{"executed": False, "reason":
"shadow_mode_would_execute"}`` — nothing is booked, so the dashboard cannot
show what the strategy *would* have done. This module adds an isolated paper
account:

  * ``shadow_open(...)`` books a VIRTUAL fill at the decision-time mid, locking
    notional / leverage / ATR exactly like the live sizing did.
  * ``mark_to_market(mids)`` runs each virtual position through the SAME
    ``DSLTracker`` exit engine the live engine uses (constructed from the live
    ``dsl_exit`` config block), marking to live mids every loop. When a stop /
    target / timeout fires it books a virtual close with realized PnL.
  * Fees mirror the live bookkeeping (``execution.taker_fee_pct`` x
    ``round_trip_fills``, modeled on close). No real orders, no real funds:
    nothing here ever touches the exchange or ``.agent-memory.json``.

Persistence follows the memory.py / dsl_exit.py pattern: a single JSON state
file guarded by ``fcntl.flock(LOCK_EX)`` + tmp + fsync + ``os.replace``, with a
process-wide ``threading.RLock`` for in-thread serialization. Overridable via
``HERMES_SHADOW_BOOK_FILE`` (default on the repo root, mounted volume-friendly).
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

_STATE_VERSION = 1
_MAX_FILLS = 2000        # open + close fills combined (audit record; capped)
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


def _max_positions() -> int:
    try:
        return max(1, int(_shadow_cfg().get("max_positions", 10)))
    except (TypeError, ValueError):
        return 10


def _build_policy():
    """Build the SAME DSL ExitPolicy a fresh live entry would get, so paper
    positions exit under identical stops. Lazy import avoids cycles."""
    try:
        from hermes_trader.agents.dsl_exit import _policy_from_config
        return _policy_from_config()
    except Exception:
        try:
            from hermes_trader.agents.dsl_exit import ExitPolicy
            return ExitPolicy()
        except Exception:
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


def _migrate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bring an on-disk state payload up to ``_STATE_VERSION`` (mirrors the
    version/migration discipline in dsl_exit.py).

    Each migration is a pure structural transform that adds fields the newer
    schema needs with safe defaults. An unknown FUTURE version is left
    untouched and warned about so a downgraded daemon never rewrites a newer
    file. Idempotent: re-running on an already-current payload is a no-op.
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

    # Future migrations chain here:
    # if version < 2:
    #     payload = _migrate_v1_to_v2(payload); version = 2
    payload["version"] = _STATE_VERSION
    return payload


# ---------------------------------------------------------------------------
# the book
# ---------------------------------------------------------------------------

class ShadowBook:
    """In-process singleton wrapping the virtual account state + DSL trackers."""

    def __init__(self, path: str = SHADOW_BOOK_FILE) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._trackers: dict[str, Any] = {}   # key f"{coin}_{side}" -> DSLTracker
        self._last_save_ts = 0.0
        self._last_equity_ts = 0.0
        self._last_snapshot: dict[str, Any] = {}
        self._last_mtime = 0.0
        self.state: dict[str, Any] = self._fresh_state()
        self._load()

    def reload_if_changed(self) -> bool:
        """Cross-process hot reload.

        The dashboard runs in a SEPARATE process from the trading loop (same as
        dsl_exit state), so the in-memory singleton goes stale the moment the
        loop writes the file. Re-read only when the on-disk mtime advanced past
        our last load/write; a no-op otherwise. In the loop's own process the
        file mtime only advances after its OWN save (content == memory), so a
        reload there is harmless and never loses unsaved mark updates.
        """
        try:
            if not os.path.exists(self.path):
                return False
            mtime = os.path.getmtime(self.path)
            if mtime <= self._last_mtime:
                return False
            with self._lock:
                # Re-check under lock to avoid a double reload racing a save.
                if mtime <= self._last_mtime:
                    return False
                self._load()
                return True
        except OSError:
            return False

    def _fresh_state(self) -> dict[str, Any]:
        bal = _starting_balance()
        return {
            "version": _STATE_VERSION,
            "created_at": _now_ms(),
            "starting_balance": bal,
            "wallet_balance": bal,     # starting + realized PnL (fees deducted on close)
            "positions": [],
            "fills": [],
            "equity_curve": [],
            "closed_count": 0,
        }

    # -- persistence --------------------------------------------------------

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
            # P0-2c: isinstance guards — a wrong-typed field (corrupt or
            # hand-edited file) degrades that field to empty instead of
            # aborting the whole load and losing the entire virtual book.
            def _list_field(key):
                val = data.get(key)
                return val if isinstance(val, list) else []
            self.state = {
                "version": _STATE_VERSION,
                "created_at": data.get("created_at", _now_ms()),
                "starting_balance": float(data.get("starting_balance", _starting_balance())),
                "wallet_balance": float(data.get("wallet_balance", data.get("starting_balance", _starting_balance()))),
                "positions": _list_field("positions"),
                "fills": _list_field("fills"),
                "equity_curve": _list_field("equity_curve"),
                "closed_count": int(data.get("closed_count", 0) or 0),
            }
        except Exception as e:
            logger.error(f"[shadow_book] corrupt state, starting fresh: {e}")
            self.state = self._fresh_state()
            return
        # Rehydrate a DSLTracker per open position so peak/floor state resumes.
        # P0-2c: per-row tolerance — a single malformed position row (missing
        # coin/side/entry_px, non-numeric value) is evicted with a warning
        # instead of aborting the loop and losing ALL trackers.
        valid_positions: list[dict[str, Any]] = []
        try:
            from hermes_trader.agents.dsl_exit import DSLTracker
            policy = _build_policy()
            for p in self.state["positions"]:
                try:
                    coin = p["coin"]
                    side = p["side"]
                    entry_px = float(p["entry_px"])
                    entry_time = float(p.get("opened_at", _now_ms())) / 1000.0
                    t = DSLTracker(
                        coin, side, entry_px, entry_time,
                        policy=policy, leverage=int(p.get("leverage", 1) or 1),
                        entry_atr_pct=float(p.get("entry_atr_pct", 0.0) or 0.0),
                        entry_regime=p.get("entry_regime", "") or "",
                    )
                    if p.get("peak_px"):
                        t.peak_px = float(p["peak_px"])
                    self._trackers[self._key(coin, side)] = t
                    valid_positions.append(p)
                except (AttributeError, TypeError, ValueError, KeyError) as e:
                    logger.warning(
                        f"[shadow_book] skipping malformed position row {p!r}: {e}"
                    )
                    continue
            self.state["positions"] = valid_positions
        except Exception as e:
            # Import/policy-build failure: keep the rows as-is; trackers are
            # rebuilt lazily by open_position() on the trading path.
            logger.warning(f"[shadow_book] tracker rehydrate failed (non-fatal): {e}")
        logger.info(
            f"[shadow_book] loaded: {len(self.state['positions'])} open, "
            f"{self.state['closed_count']} closed, wallet={self.state['wallet_balance']:.2f}")

    def _save(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_save_ts) < _SAVE_MIN_INTERVAL_S:
            return
        self._last_save_ts = now
        # Persist tracker peak into each position before serializing.
        for p in self.state["positions"]:
            t = self._trackers.get(self._key(p["coin"], p["side"]))
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

    def _find_position(self, coin: str, side: str) -> Optional[dict[str, Any]]:
        return next(
            (p for p in self.state["positions"]
             if p["coin"] == coin and p["side"] == side),
            None,
        )

    def _account_metrics(self, mids: Optional[dict[str, float]] = None) -> dict[str, Any]:
        """Compute wallet / margin / equity / unrealized from live state.

        When ``mids`` is supplied, unrealized is marked to those live mids and
        per-position mark fields are refreshed; otherwise the last mark stored
        on each position is used.
        """
        fee_pct = _taker_fee_pct()
        fills_n = _round_trip_fills()
        used_margin = 0.0
        unrealized = 0.0
        open_notional = 0.0
        for p in self.state["positions"]:
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
        wallet = float(self.state["wallet_balance"])
        equity = wallet + unrealized
        available = equity - used_margin
        return {
            "starting_balance": float(self.state["starting_balance"]),
            "wallet_balance": round(wallet, 4),
            "equity": round(equity, 4),
            "available": round(available, 4),
            "used_margin": round(used_margin, 4),
            "unrealized_pnl_usd": round(unrealized, 4),
            "open_notional_usd": round(open_notional, 4),
            "realized_pnl_usd": round(wallet - float(self.state["starting_balance"]), 4),
            "total_fees_usd": round(sum(float(f.get("fee_usd", 0.0) or 0.0)
                                        for f in self.state["fills"]
                                        if f.get("type") == "close"), 4),
            "open_positions": len(self.state["positions"]),
            "closed_count": self.state["closed_count"],
            "taker_fee_pct": fee_pct,
            "round_trip_fills": fills_n,
        }

    # -- mutations ----------------------------------------------------------

    def shadow_open(self, *, coin: str, side: str, entry_px: float,
                    size_usd: float, leverage: int, entry_atr_pct: float = 0.0,
                    entry_regime: str = "", analysis_id: str = "") -> Optional[dict[str, Any]]:
        """Book a virtual fill. Returns the open fill record, or None if skipped."""
        if not _enabled():
            return None
        if not coin or side not in ("long", "short") or entry_px <= 0 or size_usd <= 0:
            return None
        with self._lock:
            if self._find_position(coin, side) is not None:
                logger.debug(f"[shadow_book] skip open {coin} {side}: already open")
                return None
            if len(self.state["positions"]) >= _max_positions():
                logger.info(f"[shadow_book] skip open {coin}: max_positions reached")
                return None
            metrics = self._account_metrics()
            lev = max(1, int(leverage))
            margin_need = size_usd / lev
            if metrics["available"] < margin_need:
                logger.info(
                    f"[shadow_book] skip open {coin}: need margin {margin_need:.2f} "
                    f"> available {metrics['available']:.2f}")
                return None

            size_coin = size_usd / entry_px
            pid = uuid.uuid4().hex[:12]
            opened_at = _now_ms()
            pos = {
                "id": pid,
                "coin": coin,
                "side": side,
                "entry_px": float(entry_px),
                "size_usd": float(size_usd),
                "size_coin": float(size_coin),
                "leverage": lev,
                "entry_atr_pct": float(entry_atr_pct or 0.0),
                "entry_regime": entry_regime or "",
                "analysis_id": analysis_id or "",
                "peak_px": float(entry_px),
                "opened_at": opened_at,
                "mark_px": float(entry_px),
                "unrealized_pct": 0.0,
                "unrealized_roe_pct": 0.0,
                "unrealized_pnl_usd": 0.0,
            }
            self.state["positions"].append(pos)

            # Build an isolated DSL tracker (not registered in the live global
            # registry, so live DSL state is never touched by paper positions).
            try:
                from hermes_trader.agents.dsl_exit import DSLTracker
                self._trackers[self._key(coin, side)] = DSLTracker(
                    coin, side, float(entry_px), opened_at / 1000.0,
                    policy=_build_policy(), leverage=lev,
                    entry_atr_pct=float(entry_atr_pct or 0.0),
                    entry_regime=entry_regime or "",
                )
            except Exception as e:
                logger.warning(f"[shadow_book] tracker build failed for {coin}: {e}")

            fill = {
                "type": "open",
                "id": uuid.uuid4().hex[:12],
                "position_id": pid,
                "ts": opened_at,
                "coin": coin,
                "side": side,
                "qty": float(size_coin),
                "price": float(entry_px),
                "notional_usd": float(size_usd),
                "leverage": lev,
                "fee_usd": 0.0,
                "analysis_id": analysis_id or "",
            }
            self.state["fills"].append(fill)
            if len(self.state["fills"]) > _MAX_FILLS:
                del self.state["fills"][: len(self.state["fills"]) - _MAX_FILLS]

            snap = self._account_metrics()
            self._append_equity(opened_at, snap, force=True)
            self._last_snapshot = snap
            self._save(force=True)
            logger.info(
                f"[shadow_book] OPEN {side} {coin} qty={size_coin:g} @ {entry_px:g} "
                f"notional=${size_usd:.2f} lev={lev}x (paper)")
            return fill

    def _close_position(self, pos: dict[str, Any], exit_px: float,
                        reason: str, hold_min: float = 0.0,
                        mfe_pct: float = 0.0) -> dict[str, Any]:
        """Book a virtual close + realized PnL. Caller holds self._lock."""
        coin = pos["coin"]
        side = pos["side"]
        entry_px = float(pos["entry_px"])
        notional = float(pos["size_usd"])
        lev = max(1, int(pos.get("leverage", 1)))

        if side == "long":
            spot_pct = (exit_px - entry_px) / entry_px * 100.0
        else:
            spot_pct = (entry_px - exit_px) / entry_px * 100.0
        gross_pnl = notional * spot_pct / 100.0
        fee_usd = notional * _taker_fee_pct() / 100.0 * _round_trip_fills()
        net_pnl = gross_pnl - fee_usd
        roe_pct = spot_pct * lev - _taker_fee_pct() * _round_trip_fills() * lev

        closed_at = _now_ms()
        close_fill = {
            "type": "close",
            "id": uuid.uuid4().hex[:12],
            "position_id": pos["id"],
            "ts": closed_at,
            "coin": coin,
            "side": side,
            "qty": float(pos["size_coin"]),
            "entry_px": entry_px,
            "price": float(exit_px),
            "notional_usd": notional,
            "leverage": lev,
            "fee_usd": round(fee_usd, 6),
            "spot_pct": round(spot_pct, 4),
            "realized_pnl_pct": round(roe_pct, 4),   # leveraged ROE %
            "gross_pnl_usd": round(gross_pnl, 6),
            "realized_pnl_usd": round(net_pnl, 6),
            "reason": reason or "",
            "hold_minutes": round(hold_min, 2),
            "mfe_pct": round(mfe_pct, 4),
            "entry_regime": pos.get("entry_regime", ""),
            "analysis_id": pos.get("analysis_id", ""),
            "opened_at": pos.get("opened_at"),
        }
        self.state["fills"].append(close_fill)
        if len(self.state["fills"]) > _MAX_FILLS:
            del self.state["fills"][: len(self.state["fills"]) - _MAX_FILLS]

        self.state["wallet_balance"] = float(self.state["wallet_balance"]) + net_pnl
        self.state["closed_count"] = int(self.state["closed_count"]) + 1
        self.state["positions"] = [
            p for p in self.state["positions"] if p["id"] != pos["id"]
        ]
        self._trackers.pop(self._key(coin, side), None)

        snap = self._account_metrics()
        self._append_equity(closed_at, snap, force=True)
        self._last_snapshot = snap
        logger.info(
            f"[shadow_book] CLOSE {side} {coin} @ {exit_px:g} reason={reason} "
            f"spot={spot_pct:+.2f}% roe={roe_pct:+.2f}% pnl=${net_pnl:+.2f} "
            f"fee=${fee_usd:.3f} hold={hold_min:.1f}m")
        return close_fill

    def mark_to_market(self, mids: dict[str, float],
                       index_prices: Optional[dict[str, float]] = None) -> list[dict[str, Any]]:
        """Mark every open paper position to live mids; run DSL exits.

        Returns the list of virtual close fills fired this call (may be empty).
        Positions whose coin is absent from ``mids`` are skipped this pass.
        """
        if not mids:
            return []
        closed: list[dict[str, Any]] = []
        with self._lock:
            if not self.state["positions"]:
                # Still refresh equity snapshot cheaply (no-op when flat).
                return []
            index_prices = index_prices or {}
            # Iterate over a snapshot of positions (list mutates on close).
            for pos in list(self.state["positions"]):
                coin = pos["coin"]
                side = pos["side"]
                mark = mids.get(coin)
                if mark is None or mark <= 0:
                    continue
                idx = index_prices.get(coin)
                tracker = self._trackers.get(self._key(coin, side))
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
                    closed.append(self._close_position(
                        pos, float(mark), reason or "dsl_exit",
                        hold_min=hold_min, mfe_pct=mfe))
                else:
                    # Refresh stored mark/unrealized for dashboard reads.
                    if side == "long":
                        upct = (mark - float(pos["entry_px"])) / float(pos["entry_px"]) * 100.0
                    else:
                        upct = (float(pos["entry_px"]) - mark) / float(pos["entry_px"]) * 100.0
                    pos["mark_px"] = float(mark)
                    pos["unrealized_pct"] = upct
                    pos["unrealized_roe_pct"] = upct * max(1, int(pos.get("leverage", 1)))
                    pos["unrealized_pnl_usd"] = float(pos["size_usd"]) * upct / 100.0
                    pos["marked_at"] = _now_ms()

            snap = self._account_metrics()
            self._last_snapshot = snap
            now_mono = time.monotonic()
            if (now_mono - self._last_equity_ts) >= _EQUITY_MIN_INTERVAL_S:
                self._append_equity(_now_ms(), snap, force=False)
                self._last_equity_ts = now_mono
            self._save(force=False)
        return closed

    def _append_equity(self, ts_ms: int, snap: dict[str, Any], force: bool) -> None:
        curve = self.state["equity_curve"]
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
                  exit_px: Optional[float] = None, reason: str = "manual_close") -> Optional[dict[str, Any]]:
        """Force-close a paper position (operator / manual). Used by the API."""
        with self._lock:
            for p in list(self.state["positions"]):
                if p["coin"] != coin:
                    continue
                if side and p["side"] != side:
                    continue
                px = float(exit_px) if exit_px else float(p.get("mark_px") or p["entry_px"])
                hold = (time.time() - float(p["opened_at"]) / 1000.0) / 60.0
                fill = self._close_position(p, px, reason, hold_min=hold)
                self._save(force=True)
                return fill
        return None

    def reset(self, starting_balance: Optional[float] = None) -> dict[str, Any]:
        """Wipe the paper account back to a fresh bankroll (operator action)."""
        with self._lock:
            old_closed = self.state.get("closed_count", 0)
            if starting_balance is not None and starting_balance > 0:
                # Persist the new default into the live config block so restarts
                # honor it; callers (API) handle the config write themselves.
                pass
            self._trackers.clear()
            self.state = self._fresh_state()
            if starting_balance is not None and starting_balance > 0:
                self.state["starting_balance"] = float(starting_balance)
                self.state["wallet_balance"] = float(starting_balance)
            self._last_snapshot = self._account_metrics()
            self._append_equity(_now_ms(), self._last_snapshot, force=True)
            self._save(force=True)
            logger.warning(f"[shadow_book] RESET — wiped {old_closed} closes; "
                           f"fresh bankroll={self.state['starting_balance']:.2f}")
            return {"ok": True, "closed_wiped": old_closed,
                    "starting_balance": self.state["starting_balance"]}

    # -- read views ---------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            snap = self._account_metrics()
            return {
                **snap,
                "enabled": _enabled(),
                "positions": [dict(p) for p in self.state["positions"]],
                "last_mark_ts": max(
                    (int(p.get("marked_at", 0) or 0) for p in self.state["positions"]),
                    default=0,
                ),
                "updated_at": _now_ms(),
            }

    def get_trades(self, limit: int = 200) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            fills = list(self.state["fills"])[-limit:]
            fills.reverse()
            return {"trades": fills, "total": len(self.state["fills"])}

    def get_equity_curve(self) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            return {"points": list(self.state["equity_curve"]),
                    "starting_balance": float(self.state["starting_balance"])}

    def get_stats(self) -> dict[str, Any]:
        self.reload_if_changed()
        with self._lock:
            closes = [f for f in self.state["fills"] if f.get("type") == "close"]
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
            equity = float(self.state["wallet_balance"]) + sum(
                float(p.get("unrealized_pnl_usd", 0.0) or 0.0)
                for p in self.state["positions"])
            return {
                "closed_trades": n,
                "open_positions": len(self.state["positions"]),
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
                "starting_balance": start,
                "wallet_balance": round(float(self.state["wallet_balance"]), 4),
                "equity_usd": round(equity, 4),
                "total_return_pct": round(100.0 * (equity - start) / start, 4) if start else 0.0,
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
                   index_prices: Optional[dict[str, float]] = None) -> list[dict[str, Any]]:
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


def close_now(coin: str, side: Optional[str] = None,
              exit_px: Optional[float] = None, reason: str = "manual_close") -> Optional[dict[str, Any]]:
    try:
        return get_book().close_now(coin, side=side, exit_px=exit_px, reason=reason)
    except Exception as e:
        logger.error(f"[shadow_book] close_now failed: {e}")
        return None
