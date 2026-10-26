"""Technical indicators (ema, sma, atr, rsi, adx) computed over OHLCV candles."""

from __future__ import annotations

import math

from hermes_trader.models.types import Candle


def _coerce_float(value: object) -> float:
    """Normalize an OHLCV cell to float.

    Raw exchange/HL JSON can arrive with numeric fields as strings (e.g.
    ``"179.55"``); feeding those straight into arithmetic raises
    ``TypeError: unsupported operand type(s) for -: 'str' and 'str'`` deep
    inside an indicator. A missing key / ``None`` / unparseable value all
    degrade to NaN so the existing is-finite / fail-closed guards reject the
    bar rather than silently treating garbage as a real price. Returning 0 for
    a missing OHLC field was worse: it fabricated a -100% spike on the next
    return computation (full review 2026-10-25 residual hole).
    """
    if value is None:
        return float("nan")
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")


_MISSING = object()


def candle_val(c: Candle | dict[str, float], key: str) -> float:
    """Read an OHLCV field from a Candle or a plain dict (always a float).

    A missing key/attribute returns NaN (fail-closed), never 0.
    """
    if isinstance(c, dict):
        if key not in c:
            return float("nan")
        return _coerce_float(c.get(key))
    val = getattr(c, key, _MISSING)
    if val is _MISSING:
        return float("nan")
    return _coerce_float(val)


def _check_period(period: int) -> None:
    # B10: every lookback indicator divides/iterates by period; a non-positive
    # period previously returned silent garbage (atr(-1) ~ -8.6e17, adx(-1)
    # ~ -4e20). Reject it explicitly at the boundary.
    if not isinstance(period, int) or period < 1:
        raise ValueError(f"period must be a positive int, got {period!r}")


def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average.

    A non-finite sample makes the EMA NaN while it is the seed/current value,
    but — like ``sma`` — the series RECOVERS once a finite sample reappears
    (M1, three-layer audit 2026-10-26). The previous recursion ``e = x*k +
    e*(1-k)`` kept e NaN forever after one bad bar, so a single missing close
    silently wiped 83% of an ema(20) series. Recovery restarts the recursion at
    the next finite sample using it as the new seed; the gap is not
    back-filled. On all-finite input the fast path below is the original
    recursion and stays bit-identical.
    """
    _check_period(period)
    n = len(values)
    out = [float("nan")] * n
    if not values:
        return out
    k = 2 / (period + 1)

    if all(isinstance(x, (int, float)) and math.isfinite(x) for x in values):
        e = values[0]
        out[0] = e
        for i in range(1, n):
            e = values[i] * k + e * (1 - k)
            out[i] = e
        return out

    # Dirty path: NaN propagates through the recursion, then re-seeds at the
    # next finite value so the tail recovers.
    e = float("nan")
    for i in range(n):
        x = values[i]
        if not isinstance(x, (int, float)) or not math.isfinite(x):
            out[i] = float("nan")
            e = float("nan")
            continue
        if not math.isfinite(e):
            e = x           # (re)seed after a gap / at the start
        else:
            e = x * k + e * (1 - k)
        out[i] = e
    return out


def sma(values: list[float], period: int) -> list[float]:
    """Simple moving average.

    A window containing any non-finite value is NaN (the mean is undefined), but
    NaN no longer POISONS every later window: once the bad sample has slid out
    of the trailing window the SMA recovers. Full-checkup 2026-10-24 (P1-4): the
    previous running-accumulator form did ``acc += NaN`` once and then every
    subsequent ``acc - x`` stayed NaN, so one dirty bar wiped 69% of a
    range_compression bandwidth history (100 -> 31 valid) silently.

    On all-finite input this is bit-identical to the old accumulator path (it
    uses the same incremental add/subtract), so normal-data output is unchanged.
    """
    _check_period(period)
    out = [float("nan")] * len(values)
    # Indexes of non-finite samples; only when the input is dirty do we take the
    # (slightly more expensive) per-window NaN-membership path.
    dirty = [i for i, x in enumerate(values) if not isinstance(x, (int, float)) or not math.isfinite(x)]
    if not dirty:
        acc = 0.0
        for i in range(len(values)):
            acc += values[i]
            if i >= period:
                acc -= values[i - period]
            if i >= period - 1:
                out[i] = acc / period
        return out

    dirty_set = set(dirty)
    for i in range(period - 1, len(values)):
        lo = i - period + 1
        # Window is valid only if none of its `period` samples is dirty. Check
        # membership against the (typically tiny) dirty index set.
        if any(j in dirty_set for j in range(lo, i + 1)):
            continue
        out[i] = sum(values[lo: i + 1]) / period
    return out


def _true_range(candles: list[Candle], i: int) -> float:
    """True range at index i (0.0 for i==0); NaN if any input field is bad."""
    h, l = candle_val(candles[i], "h"), candle_val(candles[i], "l")
    pc = candle_val(candles[i - 1], "c")
    tr = max(h - l, abs(h - pc), abs(l - pc))
    return tr if math.isfinite(tr) else float("nan")


def atr(candles: list[Candle], period: int = 14) -> list[float]:
    """Average true range (Wilder smoothing).

    NaN no longer poisons the whole series (M1, three-layer audit 2026-10-26):
    a bar whose h/l/prior-close is non-finite yields a NaN TR for that bar; the
    smoothed ATR is NaN while fewer than ``period`` consecutive valid TRs
    follow the gap, then re-seeds from their simple mean and resumes Wilder
    smoothing — so the tail recovers instead of staying NaN forever (the old
    ``out[i] = (out[i-1]*(p-1)+tr)/p`` recursion could never un-NaN). The
    all-finite path is the original computation and bit-identical.
    """
    _check_period(period)
    n = len(candles)
    out = [float("nan")] * n
    if n <= period:
        return out

    tr = [float("nan")] * n
    for i in range(1, n):
        tr[i] = _true_range(candles, i)

    if all(math.isfinite(x) for x in tr[1:]):
        acc = sum(tr[1:period + 1])
        out[period] = acc / period
        for i in range(period + 1, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
        return out

    # Dirty path: track the trailing run of consecutive valid TRs; once it
    # reaches `period`, (re)seed from their mean and continue Wilder smoothing
    # until the next NaN resets the run.
    run: list[float] = []
    smoothed = float("nan")
    for i in range(1, n):
        x = tr[i]
        if not math.isfinite(x):
            run = []
            smoothed = float("nan")
            continue
        if not math.isfinite(smoothed):
            run.append(x)
            if len(run) == period:
                smoothed = sum(run) / period
                out[i] = smoothed
        else:
            smoothed = (smoothed * (period - 1) + x) / period
            out[i] = smoothed
    return out


def _rsi_from_closes(closes: list[float], period: int = 14) -> list[float]:
    """Wilder RSI series from close prices (first `period` values are NaN).

    M2 (three-layer audit 2026-10-26): a non-finite close is a missing bar, not
    a zero move. The gain/loss accumulators only consume FINITE price
    differences — an explicit ``isfinite`` check rather than relying on both
    ``d > 0`` and ``d < 0`` being False for NaN (which silently folded the bad
    bar in as "no movement" and nudged RSI toward 50). The initial Wilder seed
    averages over the finite deltas, and one bad bar no longer biases the
    smoothed level; the series recovers on the next finite close (the Wilder
    recursion was never permanently NaN-poisoned). All-finite output is
    bit-identical to the original.
    """
    _check_period(period)
    out = [float("nan")] * len(closes)
    if len(closes) <= period:
        return out

    def _delta(i: int) -> float:
        a, b = closes[i], closes[i - 1]
        d = a - b
        return d if math.isfinite(d) else float("nan")

    g, l = 0.0, 0.0
    n_seed = 0
    for i in range(1, period + 1):
        d = _delta(i)
        if not math.isfinite(d):
            continue
        n_seed += 1
        if d >= 0:
            g += d
        else:
            l -= d

    # Average over the finite deltas actually present in the seed window.
    avg_g = g / n_seed if n_seed else 0.0
    avg_l = l / n_seed if n_seed else 0.0
    # Full-checkup 2026-10-24 (P1-1): ``avg_l == 0`` conflates "no down bars"
    # (genuine RSI 100) with "no movement at all" (avg_g == avg_l == 0), which
    # is mathematically no information, not maximum overbought. A flat tape
    # returning RSI 100 can falsely trip the late_chase 1h-RSI blowoff gate.
    # Flat -> neutral 50; strictly-nonnegative moves with zero losses -> 100.
    if n_seed:
        out[period] = 100 if avg_l == 0 and avg_g > 0 else (50 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))

    for i in range(period + 1, len(closes)):
        d = _delta(i)
        if not math.isfinite(d):
            # Missing bar: hold the previous smoothed level rather than feed a
            # fabricated zero move (which would pull RSI toward 50).
            out[i] = out[i - 1] if math.isfinite(out[i - 1]) else float("nan")
            continue
        # gain = positive move else 0; loss = magnitude of a negative move else 0.
        # The loss term must be a non-negative magnitude — the previous
        # `d if d < 0 else -d` fed negatives in and drove RSI below 0.
        avg_g = (avg_g * (period - 1) + (d if d > 0 else 0)) / period
        avg_l = (avg_l * (period - 1) + (-d if d < 0 else 0)) / period
        out[i] = 100 if avg_l == 0 and avg_g > 0 else (50 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))

    return out


def rsi(candles: list[Candle], period: int = 14) -> list[float]:
    """Relative strength index."""
    closes = [candle_val(c, "c") for c in candles]
    return _rsi_from_closes(closes, period)


def rsi_last(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI of the last close; None when data is insufficient or NaN."""
    if len(closes) <= period:
        return None
    last = _rsi_from_closes(closes, period)[-1]
    return last if math.isfinite(last) else None


def adx(candles: list[Candle], period: int = 14) -> list[float]:
    """Average directional index.

    NaN recovery (M1, three-layer audit 2026-10-26): one bar with a bad
    h/l/c used to make every Wilder-smoothed state (TR/+DM/-DM, then DX, then
    ADX) NaN forever, wiping ~92% of the series. Each smoothing stage now
    re-seeds from `period` consecutive valid inputs after a gap, so the tail
    recovers. The all-finite path is the original recursion and bit-identical.
    """
    _check_period(period)
    n = len(candles)
    out = [float("nan")] * n
    if n <= period * 2:
        return out

    tr = [float("nan")] * n
    p_dm = [float("nan")] * n
    m_dm = [float("nan")] * n

    for i in range(1, n):
        h, l = candle_val(candles[i], "h"), candle_val(candles[i], "l")
        pc = candle_val(candles[i - 1], "c")
        ph, pl = candle_val(candles[i - 1], "h"), candle_val(candles[i - 1], "l")

        trv = max(h - l, abs(h - pc), abs(l - pc))
        up = h - ph
        dn = pl - l
        # All five inputs must be finite; otherwise this bar and the directional
        # moves that reference it cannot be defined.
        if not all(math.isfinite(v) for v in (trv, up, dn, h, l, pc, ph, pl)):
            continue
        tr[i] = trv
        p_dm[i] = up if (up > dn and up > 0) else 0.0
        m_dm[i] = dn if (dn > up and dn > 0) else 0.0

    def _dx_at(tr_s: float, p_s: float, m_s: float) -> float:
        pdi = 0 if tr_s == 0 else 100 * p_s / tr_s
        mdi = 0 if tr_s == 0 else 100 * m_s / tr_s
        total = pdi + mdi
        return 0.0 if total == 0 else 100 * abs(pdi - mdi) / total

    dx = [float("nan")] * n

    valid = [i for i in range(1, n) if math.isfinite(tr[i])]
    if len(valid) == n - 1:
        # All-finite fast path: original computation, bit-identical.
        tr_s = sum(tr[1:period + 1])
        p_s = sum(p_dm[1:period + 1])
        m_s = sum(m_dm[1:period + 1])
        dx[period] = _dx_at(tr_s, p_s, m_s)
        for i in range(period + 1, n):
            tr_s = tr_s - tr_s / period + tr[i]
            p_s = p_s - p_s / period + p_dm[i]
            m_s = m_s - m_s / period + m_dm[i]
            dx[i] = _dx_at(tr_s, p_s, m_s)

        adx_s = sum(dx[period:period * 2])
        out[period * 2 - 1] = adx_s / period
        for i in range(period * 2, n):
            out[i] = (out[i - 1] * (period - 1) + dx[i]) / period
        return out

    # Dirty path: Wilder-smooth the three components over runs of `period`
    # consecutive valid bars, re-seeding each run from a simple sum.
    run_idx: list[int] = []
    tr_s = p_s = m_s = float("nan")
    for i in range(1, n):
        if not math.isfinite(tr[i]):
            run_idx = []
            tr_s = p_s = m_s = float("nan")
            continue
        run_idx.append(i)
        if len(run_idx) <= period:
            # (Re)build the seed window from raw values.
            w = run_idx[-period:]
            tr_s = sum(tr[j] for j in w)
            p_s = sum(p_dm[j] for j in w)
            m_s = sum(m_dm[j] for j in w)
            if len(run_idx) >= period:
                dx[i] = _dx_at(tr_s, p_s, m_s)
        else:
            tr_s = tr_s - tr_s / period + tr[i]
            p_s = p_s - p_s / period + p_dm[i]
            m_s = m_s - m_s / period + m_dm[i]
            dx[i] = _dx_at(tr_s, p_s, m_s)

    # Smooth DX into ADX, re-seeding from `period` consecutive valid DX.
    dx_run: list[float] = []
    adx_prev = float("nan")
    for i in range(period, n):
        if not math.isfinite(dx[i]):
            dx_run = []
            adx_prev = float("nan")
            continue
        dx_run.append(dx[i])
        if not math.isfinite(adx_prev):
            if len(dx_run) == period:
                adx_prev = sum(dx_run) / period
                out[i] = adx_prev
        else:
            adx_prev = (adx_prev * (period - 1) + dx[i]) / period
            out[i] = adx_prev

    return out


def obv(candles: list[Candle]) -> list[float]:
    """On-balance volume: cumulative volume signed by close direction.

    Rises when volume accumulates on up-closes, falls on down-closes. The
    slope/level confirms whether a price move is backed by real participation
    (OBV confirms price) or is a weak/ divergent push.
    """
    out = [0.0] * len(candles)
    running = 0.0
    prev_close: float | None = None
    for i, c in enumerate(candles):
        close = candle_val(c, "c")
        vol = candle_val(c, "v")
        # M3 (three-layer audit 2026-10-26): a non-finite volume must NOT poison
        # the cumulative sum (``running += nan`` stayed NaN forever), and a
        # non-finite close must not become the comparison baseline (a NaN
        # prev_close silently dropped the next two bars). Hold the running total
        # across an unusable bar (volume treated as 0 contribution, the prior
        # close retained) instead of fabricating a signed NaN move.
        if not math.isfinite(close):
            out[i] = running
            continue
        if prev_close is not None:
            signed_vol = vol if math.isfinite(vol) else 0.0
            if close > prev_close:
                running += signed_vol
            elif close < prev_close:
                running -= signed_vol
        out[i] = running
        prev_close = close
    return out
