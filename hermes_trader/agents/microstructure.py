"""Microstructure accumulators for launch-point detection (shadow phase).

The confirmation-based triggers (breakout / momentumBurst) only fire AFTER a
move has printed closed bars, so entries structurally arrive ~10 minutes late.
This module captures LEADING / synchronous evidence — aggressive trade flow and
order-book imbalance — that turns at, or just before, the launch bar.

Two rolling, in-memory accumulators per coin:

* ``CVD``  — cumulative volume delta from public trade prints. Each print is
  signed by aggressor side (buyer-aggressor +, seller-aggressor −). What feeds
  a launch is one-sided *taking*, which diverges from price minutes before a
  breakout and separates real demand from paint-the-tape markup.
* ``Book imbalance`` — top-of-book bid/ask size imbalance from L2 snapshots.
  A bid-stacked, ask-thin book predicts the easy direction of travel.

Both are bounded rolling windows (time + count), thread-safe (the WS callback
is a background thread), best-effort, and never raise into the trading path.
They produce a normalized aggression/imbalance read consumed by the breakout
flow-confirm path and the trading/risk layer.

Shadow-only: nothing here places orders. Fail-open on insufficient data.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Optional

logger = logging.getLogger(__name__)

# Rolling window for trade prints: keep enough history to compare recent
# aggression against a baseline, but bounded so a long session can't leak.
_WINDOW_SEC = 300.0
_MAX_PRINTS = 4000
# Baseline vs burst window split within the rolling window.
_BURST_SEC = 60.0


class _CoinFlow:
    __slots__ = ("prints", "cvds", "last_imbalance", "last_imbalance_ts")

    def __init__(self) -> None:
        # (ts, signed_size)
        self.prints: Deque[tuple[float, float]] = deque()
        # cumulative signed volume aligned 1:1 with prints (for windowed CVD)
        self.cvds: Deque[float] = deque()
        self.last_imbalance: Optional[float] = None
        self.last_imbalance_ts: float = 0.0


class Microstructure:
    """Thread-safe rolling CVD + book-imbalance accumulators."""

    def __init__(self, window_sec: float = _WINDOW_SEC,
                 max_prints: int = _MAX_PRINTS) -> None:
        self._window_sec = float(window_sec)
        self._max_prints = int(max_prints)
        self._lock = threading.Lock()
        self._coins: dict[str, _CoinFlow] = {}

    # ── ingestion (WS callback thread) ────────────────────────────────────
    def _flow(self, coin: str) -> _CoinFlow:
        f = self._coins.get(coin)
        if f is None:
            f = _CoinFlow()
            self._coins[coin] = f
        return f

    def add_trade(self, *, coin: str, ts: float, size: float,
                  buyer_aggressor: bool) -> None:
        """Add one public trade print. Best-effort; never raises."""
        try:
            if size <= 0:
                return
            signed = float(size) * (1.0 if buyer_aggressor else -1.0)
            with self._lock:
                f = self._flow(coin)
                prev = f.cvds[-1] if f.cvds else 0.0
                f.prints.append((float(ts), signed))
                f.cvds.append(prev + signed)
                self._prune_locked(f, float(ts))
        except Exception:  # pragma: no cover - defensive
            logger.debug("[micro] add_trade failed", exc_info=True)

    def set_book_imbalance(self, *, coin: str, ts: float,
                           imbalance: float) -> None:
        """Set the latest top-of-book imbalance in [-1, 1] (bid − ask)."""
        try:
            with self._lock:
                f = self._flow(coin)
                f.last_imbalance = max(-1.0, min(1.0, float(imbalance)))
                f.last_imbalance_ts = float(ts)
        except Exception:  # pragma: no cover - defensive
            logger.debug("[micro] set_book_imbalance failed", exc_info=True)

    def _prune_locked(self, f: _CoinFlow, now: float) -> None:
        cutoff = now - self._window_sec
        while f.prints and (f.prints[0][0] < cutoff
                            or len(f.prints) > self._max_prints):
            f.prints.popleft()
            f.cvds.popleft()

    # ── reads (main thread) ───────────────────────────────────────────────
    def aggression(self, coin: str, *, now: Optional[float] = None
                   ) -> Optional[float]:
        """Normalized directional aggression in [-1, 1] over the burst window.

        ``net_burst_volume / gross_burst_volume`` — 1.0 = every print in the
        last ``_BURST_SEC`` was buyer-aggressive, −1.0 all seller. None when
        there is no meaningful flow, so callers fail open.
        """
        now = float(now if now is not None else time.time())
        with self._lock:
            f = self._coins.get(coin)
            if not f or not f.prints:
                return None
            net = 0.0
            gross = 0.0
            for ts, signed in f.prints:
                if ts >= now - _BURST_SEC:
                    net += signed
                    gross += abs(signed)
            if gross <= 0:
                return None
            return max(-1.0, min(1.0, net / gross))

    def cvd_divergence(self, coin: str, *, now: Optional[float] = None
                       ) -> Optional[float]:
        """Signed CVD change over the burst window, gross-normalized [-1,1].

        Equivalent to windowed aggression but derived from cumulative deltas;
        kept explicit so callers can read "flow turning" magnitude directly.
        """
        return self.aggression(coin, now=now)

    def book_imbalance(self, coin: str, *, now: Optional[float] = None,
                       max_age_sec: float = 10.0) -> Optional[float]:
        """Latest book imbalance if fresh, else None (fail open)."""
        now = float(now if now is not None else time.time())
        with self._lock:
            f = self._coins.get(coin)
            if not f or f.last_imbalance is None:
                return None
            if now - f.last_imbalance_ts > float(max_age_sec):
                return None
            return f.last_imbalance

    def reset(self, coin: Optional[str] = None) -> None:
        with self._lock:
            if coin is None:
                self._coins.clear()
            else:
                self._coins.pop(coin, None)


# ── process-wide singleton + helpers ────────────────────────────────────────

_instance: Optional[Microstructure] = None
_instance_lock = threading.Lock()


def get_microstructure() -> Microstructure:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Microstructure()
        return _instance


def imbalance_from_sides(bids: list[Any], asks: list[Any],
                         *, depth: int = 10) -> float:
    """Top-N size imbalance in [-1, 1]: ``(bid_sz − ask_sz)/(bid_sz+ask_sz)``.

    Accepts SDK L2 levels ``[{'px':..,'sz':..}, ...]`` or ``(px, sz)`` tuples,
    or any object with ``.sz``. Size is not price-weighted (top-N depth).
    """
    def _sz(level: Any) -> float:
        if isinstance(level, dict):
            return float(level.get("sz", 0.0) or 0.0)
        if hasattr(level, "sz"):
            return float(getattr(level, "sz") or 0.0)
        try:
            return float(level[1])
        except Exception:
            return 0.0

    bid_sz = sum(_sz(x) for x in list(bids or [])[:depth])
    ask_sz = sum(_sz(x) for x in list(asks or [])[:depth])
    tot = bid_sz + ask_sz
    if tot <= 0:
        return 0.0
    return max(-1.0, min(1.0, (bid_sz - ask_sz) / tot))


# ── launch scoring: combine leading flow with volatility compression ────────

def compression_extreme(candles: list[Any], lookback: int = 48) -> Optional[float]:
    """Range-bandwidth percentile (0–100): how squeezed price is NOW.

    Low percentile (<~10) = historically compressed, the setup before an
    expansion. Direction-agnostic — flow (CVD) supplies direction. Best-effort.

    Bandwidth is the mean bar range ``(high−low)/mid`` over ``lookback`` bars.
    It uses the true intrabar range rather than close-to-close dispersion: a
    tight coiling tape can hold a constant close while its range collapses,
    which a close-based measure would miss entirely.
    """
    try:
        def _v(c: Any, k: str) -> float:
            return c.get(k) if isinstance(c, dict) else getattr(c, k)

        if len(candles) < lookback + 1:
            return None
        bandwidths: list[float] = []
        for i in range(lookback, len(candles) + 1):
            window = candles[i - lookback:i]
            rng = sum(_v(c, "h") - _v(c, "l") for c in window) / lookback
            mid = sum(_v(c, "c") for c in window) / lookback
            if mid > 0:
                bandwidths.append(rng / mid)
        if len(bandwidths) < 20:
            return None
        cur = bandwidths[-1]
        rank = sum(1 for b in bandwidths[:-1] if b <= cur)
        return 100.0 * rank / (len(bandwidths) - 1)
    except Exception:
        return None


def near_key_level(candles: list[Any], side: str, *,
                   pivot_lookback: int = 120,
                   tolerance_atr: float = 1.0) -> Optional[bool]:
    """Whether a launch is confluent with a higher-timeframe key level.

    For a long, price should be breaking OUT from just above a major swing
    LOW (the launch base sits on HTF support); for a short, from below a major
    swing HIGH (HTF resistance). We use the most extreme pivot over
    ``pivot_lookback`` bars as the key level and require the latest close to
    be within ``tolerance_atr`` of it on the correct side. Returns None on
    insufficient data so the caller fails open.
    """
    try:
        from hermes_trader.indicators.math import atr as atr_arr
        if len(candles) < 30:
            return None
        a = atr_arr(candles, 14)[-1]
        if not (a == a) or a <= 0:
            return None
        window = candles[-pivot_lookback:]
        def _lvl(c: Any, k: str) -> float:
            return c.get(k) if isinstance(c, dict) else getattr(c, k)
        swing_low = min(_lvl(c, "l") for c in window)
        swing_high = max(_lvl(c, "h") for c in window)
        last = _lvl(candles[-1], "c")
        band = float(tolerance_atr) * a
        if side == "long":
            # Base on support: latest close is above the swing low but near it.
            return last >= swing_low and (last - swing_low) <= band
        else:
            # Base under resistance: latest close below swing high but near it.
            return last <= swing_high and (swing_high - last) <= band
    except Exception:
        return None

