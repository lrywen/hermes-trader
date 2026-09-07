"""E6 xs_reversal: oversold-bounce LONG shadow probe (Audit 2026-09-07, M2).

Long-term SHADOW arm that records what an excess-drawdown mean-reversion LONG
would do on a trade CANDIDATE, without ever touching the live decision. The
M1 offline backtest (archive/scripts/backtest_xs_reversal.py) falsified the
original chop/neutral regime gate: the canonical regime_strength_score is
DIRECTION-AGNOSTIC and ~90% of extreme 3-day drawdowns score TREND regardless
of trend direction. The validated edge lives in DOWNTREND oversold bounces
(trend_bear + RSI[15,35): n=49, 72h WR 61.2% / +2.25%, 168h WR 59.2% /
+5.02%), so the live candidate gate is:

  1. xs trigger    : ext_pct = (close / max(high, last 72 1h bars) - 1) * 100
                     ranks in the bottom (100-top_pct)% tail of the coin's OWN
                     rolling 90-day (2160-bar) drawdown distribution
                     (top_pct=85; self-normalizing, no fixed threshold).
  2. awake         : at least awake_min_frac (0.67 => 5 of 7) of the last
                     awake_bars (7) bars are "active" — volume above the 20d
                     median OR bar range above the 20d median (filters
                     dead-cat grinding declines).
  3. direction gate: EMA8 < EMA21 (established downtrend; the M1 edge cell).
  4. RSI confirm   : rsi_floor (15) <= RSI(14) < rsi_long (35) — oversold but
                     not a free-falling knife; the floor is a non-binding
                     guardrail in the M1 sample ([10,15) had zero signals).

A record is written for every xs+awake TRIGGER regardless of gates 3-4, with
the full snapshot (regime label/score, EMA relation, RSI, is_candidate) so M4
reconcile can re-group every cell forward — the scan never hard-filters on
the regime label. `is_candidate=True` means gates 3-4 also pass (the cell M1
validated).

Hot-path safety (same lesson as shadow_signals): evaluation needs ~2300 1h
candles (a heavy fetch), so it MUST run OFF the execute path — call
`run_xs_reversal_async()`, which snapshots the inputs and does all the
fetching on a daemon thread. The fetch is TTL-cached (90s) + inflight-
coalesced in hl_client, so concurrent/back-to-back dispatches never amplify
the HTTP path. mode=off performs ZERO network access and starts no thread.

mode: off | shadow | enforce. In M2 enforce behaves EXACTLY like shadow
(record-only) — no order is ever placed or blocked from this module; the log
line is tagged "enforce not implemented, recording only".
"""

from __future__ import annotations

import bisect
import logging
import math
import os
import statistics
import threading
import time
from typing import Any, Optional

from hermes_trader.agents.config_store import cfg_get

logger = logging.getLogger(__name__)

# Mode resolution mirrors risk_gates._gate_mode (off/shadow/enforce; env
# override lets production flip the probe without a config rewrite; an
# unparsable value falls back to off — an unknown mode never arms anything).
_GATE_MODES = ("off", "shadow", "enforce")
_MODE_ENV = "HERMES_XS_REVERSAL_MODE"
_PATH_ENV = "HERMES_XS_REVERSAL_SHADOW_FILE"
_DEFAULT_LOG_NAME = "xs_reversal_shadow.jsonl"
_STREAM = "xs_reversal"

# Fixed geometry of the signal (byte-for-byte the M1 backtest windows).
LOOKBACK_BARS = 72          # 3d rolling highest-high window (lookback_d=3)
PCTILE_WIN = 2160           # 90d rolling drawdown distribution
MEDIAN_WIN = 480            # 20d medians for the awake activity filter
REGIME_WIN = 300            # trailing window for the regime snapshot label
# Need the percentile window + the lookback window before any value is
# meaningful; awake/RSI need far less, so this is the binding warm-up floor.
MIN_BARS = PCTILE_WIN + LOOKBACK_BARS
# Fetch a little headroom beyond the minimum so dropping the still-forming
# last bar never leaves us short of the warm-up floor.
FETCH_COUNT = PCTILE_WIN + LOOKBACK_BARS + 5
CANDLE_INTERVAL = "1h"


def _resolve_mode(config: Optional[dict[str, Any]]) -> str:
    """Resolve off/shadow/enforce from the config block then env (fail-closed)."""
    blk = (config or {}).get("xs_reversal") or {}
    mode = str(blk.get("mode", "off") or "off").strip().lower()
    env_mode = str(os.environ.get(_MODE_ENV) or "").strip().lower()
    if env_mode:
        mode = env_mode
    return mode if mode in _GATE_MODES else "off"


def _resolve_path(config: Optional[dict[str, Any]]) -> str:
    """Resolve the shadow JSONL path (config -> env -> ~/.hermes-trading)."""
    blk = (config or {}).get("xs_reversal") or {}
    return str(blk.get("shadow_log_path") or "").strip() or os.environ.get(
        _PATH_ENV,
        os.path.expanduser(f"~/.hermes-trading/{_DEFAULT_LOG_NAME}"),
    )


def _ext_pct_series(closes: list[float], highs: list[float],
                    lookback_bars: int = LOOKBACK_BARS) -> list[float]:
    """Drawdown % from the rolling highest high of the last `lookback_bars`
    inclusive (always <= 0). NaN until the first full window."""
    n = len(closes)
    out = [float("nan")] * n
    for i in range(n):
        lo = i - lookback_bars + 1
        if lo < 0:
            continue
        hh = max(highs[lo:i + 1])
        if hh > 0:
            out[i] = (closes[i] / hh - 1.0) * 100.0
    return out


def _pctile_rank(sorted_win: list[float], v: float) -> float:
    """Fraction of window values <= v (0..1). Lower rank = more extreme
    drawdown because ext_pct is always <= 0 (bisect_right, M1 parity)."""
    if not sorted_win:
        return float("nan")
    return bisect.bisect_right(sorted_win, v) / len(sorted_win)


def _awake_frac(candles: list, vols: list[float], i: int,
                awake_bars: int) -> float:
    """Fraction of the last `awake_bars` bars (ending at i) that are active:
    volume above the trailing MEDIAN_WIN median OR bar range above that
    window's range median. Mirrors the M1 scan exactly."""
    mlo = i - MEDIAN_WIN + 1
    if mlo < 0:
        return float("nan")
    vol_med = statistics.median(vols[mlo:i + 1])
    rng_med = statistics.median(candles[j].h - candles[j].l
                                for j in range(mlo, i + 1))
    active = 0
    for j in range(i - awake_bars + 1, i + 1):
        if vols[j] > vol_med or (candles[j].h - candles[j].l) > rng_med:
            active += 1
    return active / awake_bars


def _regime_label(score: float) -> str:
    """Threshold labels byte-for-byte with backtest_ab_compare._regime_score
    (which delegates to the same canonical scorer)."""
    if score >= 0.70:
        return "STRONG_TREND"
    if score >= 0.55:
        return "TREND"
    if score >= 0.40:
        return "NEUTRAL"
    return "CHOP"


def evaluate_xs_reversal(candles: list, *, config: Optional[dict[str, Any]] = None
                         ) -> Optional[dict[str, Any]]:
    """Pure evaluation over the last CLOSED 1h bar. Returns a snapshot record
    when the xs percentile + awake trigger fires, else None.

    The record is built for EVERY trigger (gates 3-4 only set is_candidate)
    so M4 reconcile sees the full forward grid. Returns None on insufficient
    data or any non-finite indicator — callers treat that as "no signal" and
    must never decide from it. `candles` should already have the still-forming
    last bar dropped (see gather_xs_reversal)."""
    if not candles or len(candles) < MIN_BARS:
        return None

    lookback_d = int(cfg_get("xs_reversal.lookback_d", 3, config=config))
    top_pct = float(cfg_get("xs_reversal.top_pct", 85, config=config))
    awake_bars = int(cfg_get("xs_reversal.awake_bars", 7, config=config))
    awake_min_frac = float(cfg_get("xs_reversal.awake_min_frac", 0.67,
                                   config=config))
    rsi_long = float(cfg_get("xs_reversal.rsi_long", 35.0, config=config))
    rsi_floor = float(cfg_get("xs_reversal.rsi_floor", 15.0, config=config))

    # lookback_d only re-derives the lookback window when changed from the
    # default 3; the canonical fixed LOOKBACK_BARS (72) is the 3d geometry.
    lookback_bars = max(1, lookback_d * 24)

    from hermes_trader.indicators import math as ind

    closes = [c.c for c in candles]
    highs = [c.h for c in candles]
    vols = [c.v for c in candles]
    n = len(candles)
    i = n - 1  # decision bar = last closed bar

    ema8_arr = ind.ema(closes, 8)
    ema21_arr = ind.ema(closes, 21)
    rsi_arr = ind.rsi(candles, 14)
    e8, e21, rsi_v = ema8_arr[i], ema21_arr[i], rsi_arr[i]
    if not all(math.isfinite(v) for v in (e8, e21, rsi_v)):
        return None

    # ext_pct at the decision bar needs `lookback_bars` highs; the percentile
    # rank needs PCTILE_WIN prior ext_pct values, each itself requiring
    # `lookback_bars` bars.
    if n < PCTILE_WIN + lookback_bars:
        return None
    ext = _ext_pct_series(closes, highs, lookback_bars)
    if not math.isfinite(ext[i]):
        return None
    plo = i - PCTILE_WIN + 1
    ext_win = [v for v in ext[plo:i + 1] if math.isfinite(v)]
    if len(ext_win) < PCTILE_WIN:
        return None
    pctile = _pctile_rank(sorted(ext_win), ext[i])
    max_tail = (100.0 - top_pct) / 100.0
    if not math.isfinite(pctile) or pctile > max_tail:
        return None

    awake = _awake_frac(candles, vols, i, awake_bars)
    if not math.isfinite(awake) or awake < awake_min_frac:
        return None

    # Direction gate (M1 edge cell) + RSI oversold confirmation. These decide
    # is_candidate only — the snapshot is recorded either way for M4.
    downtrend = e8 < e21
    rsi_ok = rsi_floor <= rsi_v < rsi_long
    is_candidate = bool(downtrend and rsi_ok)

    # Regime snapshot label (canonical direction-agnostic scorer over the
    # trailing REGIME_WIN bars). Recorded for M4 grouping; NEVER a hard gate.
    regime_score = 0.0
    try:
        from hermes_trader.agents.market_regime import regime_strength_score
        regime_score = float(regime_strength_score(candles[max(0, i - REGIME_WIN + 1):i + 1]))
    except Exception as e:  # pragma: no cover - snapshot only, never gates
        logger.debug(f"[xs_reversal] regime score failed: {e}")

    last = candles[i]
    return {
        "timestamp": int(getattr(last, "t", 0) or time.time() * 1000),
        "coin": getattr(last, "coin", "") or "",
        "side": "long",
        "entry_px": float(closes[i]),
        "ext_pct": round(float(ext[i]), 4),
        "ext_percentile": round(float(pctile), 6),
        "awake_frac": round(float(awake), 4),
        "macro_regime": _regime_label(regime_score),
        "regime_score": round(float(regime_score), 4),
        "ema8": round(float(e8), 8),
        "ema21": round(float(e21), 8),
        "ema8_gt_ema21": bool(e8 > e21),
        "rsi14": round(float(rsi_v), 4),
        "rsi_long": rsi_long,
        "rsi_floor": rsi_floor,
        "top_pct": top_pct,
        "is_candidate": is_candidate,
        # outcome fields are backfilled by M4 reconcile; null for now.
        "outcome": None,
        "exit_px": None,
        "pnl_usd": None,
    }


def gather_xs_reversal(coin: str, side: str = "long", *,
                       config: Optional[dict[str, Any]] = None
                       ) -> Optional[dict[str, Any]]:
    """Fetch 1h candles for `coin` and evaluate the xs_reversal probe.

    Returns the snapshot record (and appends it to the shadow JSONL) when the
    trigger fires, else None. ONLY the LONG side is meaningful (oversold
    bounce); other sides return None without fetching. Network/indicator/
    write failures are swallowed to a debug log — this arm must never affect
    the trade path."""
    if (side or "long").lower() != "long":
        return None
    try:
        from hermes_trader.client.hl_client import fetch_hl_candles
        candles = fetch_hl_candles(coin, CANDLE_INTERVAL, FETCH_COUNT)
        if not candles:
            return None
        # Drop the still-forming last bar so the trigger evaluates the last
        # CLOSED bar (perception._drop_forming_bar parity). Inlined here to
        # avoid a cross-module import for a two-line check; _last_bar_closed
        # semantics: a bar is closed once now >= bar_open + interval.
        last = candles[-1]
        bar_open_ms = float(getattr(last, "t", 0) or 0.0)
        if bar_open_ms > 0 and time.time() * 1000.0 < bar_open_ms + 3600.0 * 1000.0:
            candles = candles[:-1]
        rec = evaluate_xs_reversal(candles, config=config)
        if rec is None:
            return None
        rec["coin"] = coin
        path = _resolve_path(config)
        try:
            from hermes_trader.shadow_log import append_jsonl
            append_jsonl(path, rec, stream=_STREAM)
        except Exception as e:  # pragma: no cover - writer never raises
            logger.debug(f"[xs_reversal] shadow write failed ({coin}): {e}")
        return rec
    except Exception as e:
        logger.debug(f"[xs_reversal] gather failed ({coin}): {e}")
        return None


def run_xs_reversal_async(coin: str, side: str = "long", *,
                          config: Optional[dict[str, Any]] = None) -> None:
    """Fire-and-forget shadow evaluate+log on a daemon thread. NEVER blocks
    the caller (the execute path). mode=off returns immediately with no
    thread and no network; shadow/enforce both only RECORD (enforce is not
    implemented in M2 — the log line says so)."""
    mode = _resolve_mode(config)
    if mode == "off":
        return
    if (side or "long").lower() != "long":
        return

    def _worker() -> None:
        try:
            rec = gather_xs_reversal(coin, side, config=config)
            if rec:
                if mode == "enforce":
                    # Audit 2026-09-07 (E6): enforce is intentionally a
                    # record-only no-op in M2 — a half-implemented enforce
                    # that blocks/allows orders is forbidden. Tag the line so
                    # M4 can tell the posture apart in the logs.
                    logger.info(
                        f"[xs_reversal] {coin} ENFORCE not implemented, "
                        f"recording only: candidate={rec['is_candidate']} "
                        f"ext={rec['ext_pct']}% pct={rec['ext_percentile']} "
                        f"rsi={rec['rsi14']} regime={rec['macro_regime']}")
                else:
                    logger.info(
                        f"[xs_reversal] {coin} shadow: candidate={rec['is_candidate']} "
                        f"ext={rec['ext_pct']}% pct={rec['ext_percentile']} "
                        f"awake={rec['awake_frac']} rsi={rec['rsi14']} "
                        f"ema8<ema21={not rec['ema8_gt_ema21']} "
                        f"regime={rec['macro_regime']}")
        except Exception as e:  # pragma: no cover
            logger.debug(f"[xs_reversal] {coin} failed: {e}")

    threading.Thread(target=_worker, name=f"xs-reversal-{coin}",
                     daemon=True).start()
