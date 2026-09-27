"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import math
import pathlib

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def _mk_candle(t, o, h, l, c, v):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _flat_candles(n, price=100.0, vol=1000.0, rng=0.5):
    """Choppy/flat candles oscillating around `price` (produces low ADX)."""
    out = []
    for i in range(n):
        s = 1.0 if (i % 2 == 0) else -1.0
        c = price + s * (i % 3) * 0.1
        out.append(_mk_candle(i, c, c + rng, c - rng, c, vol))
    return out


def _load_bt_module():
    import importlib.util
    import sys as _sys
    if "bt_under_test" not in _sys.modules:
        spec = importlib.util.spec_from_file_location(
            "bt_under_test", str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backtest.py"))
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["bt_under_test"] = mod
        spec.loader.exec_module(mod)
    return _sys.modules["bt_under_test"]


def _kernel_sim(coin, base, sim_ms, cfg, *, candles_4h=None, candles_15m=None,
                late_entry_params=None):
    """Reproduce scripts/backtest.py's thin per-coin orchestration on the
    unified P4 kernel: heuristic signals → the live late-entry veto (same pure
    ``late_entry_check`` on only CLOSED higher-TF bars) → ``driver.run``."""
    from hermes_trader.agents.dsl_exit import _build_policy_from_config
    from hermes_trader.agents.ta_filter import late_entry_check
    from hermes_trader.backtest import driver as kdriver
    from hermes_trader.backtest import signals as ksig
    from hermes_trader.backtest.cost import CostModel

    bt = _load_bt_module()
    hcfg = ksig.HeuristicConfig(
        thresholds=cfg["thresholds"], weights=cfg["weights"], warmup=100)
    signals = ksig.heuristic_signals(base, hcfg, bar_ms=sim_ms)

    vetoes: list = []
    kept: list = []
    le_cfg = dict(late_entry_params or {})
    t4 = [c.t for c in candles_4h] if candles_4h else []
    t15 = [c.t for c in candles_15m] if candles_15m else []
    for sig in signals:
        if le_cfg and candles_4h is not None:
            decision_ms = base[sig.bar_index].t + sim_ms
            w4 = bt._closed_slice(candles_4h, t4, decision_ms, 4 * 3600_000)
            w15 = bt._closed_slice(candles_15m, t15, decision_ms, 15 * 60_000)
            le = late_entry_check(w4, w15, sig.side, le_cfg)
            if le.get("block"):
                vetoes.append({"coin": coin, "side": sig.side,
                               "bar": sig.bar_index, "reason": le.get("reason", "")})
                continue
        kept.append(sig)

    zero = CostModel(0.0, 0.0, 0.0, 0.0)
    trades = kdriver.run(
        base, kept, _build_policy_from_config(), coin=coin, leverage=5,
        notional_usd=50.0, cost=zero, bar_ms=sim_ms)
    return trades, vetoes


def _trend_candles(n, start=100.0, step=0.5, vol=1000.0):
    """Steadily rising candles (produces high ADX)."""
    out = []
    for i in range(n):
        c = start + i * step
        out.append(_mk_candle(i, c, c + 0.8, c - 0.2, c, vol + i))
    return out


def test_backtest_closed_slice_uses_only_closed_higher_tf_bars():
    """Higher-TF bars are visible only once CLOSED at the decision instant —
    an in-progress 4h bar would leak the future (look-ahead)."""
    import importlib.util
    import sys as _sys
    if "bt_under_test" not in _sys.modules:
        spec = importlib.util.spec_from_file_location(
            "bt_under_test", str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backtest.py"))
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["bt_under_test"] = mod
        spec.loader.exec_module(mod)
    bt = _sys.modules["bt_under_test"]
    H = 3600_000
    ts = [i * 4 * H for i in range(5)]
    series = [f"c{i}" for i in range(5)]
    # decision at 12h: bars opening 0h/4h/8h (closing 4h/8h/12h) are closed.
    assert bt._closed_slice(series, ts, 12 * H, 4 * H) == ["c0", "c1", "c2"]
    # 1ms before: the 8h bar (closes exactly 12h) is still in progress.
    assert bt._closed_slice(series, ts, 12 * H - 1, 4 * H) == ["c0", "c1"]
    # Nothing closed yet → None (caller treats as data missing, not as signal).
    assert bt._closed_slice(series, ts, 3 * H, 4 * H) is None
    assert bt._closed_slice([], [], 12 * H, 4 * H) is None


def test_backtest_simulate_enforces_late_entry_veto():
    """The thin backtest orchestration blocks late entries via the SAME pure
    function and records vetoes; without higher-TF data the run is unaffected."""
    H = 3600_000
    # 1h base series on the SAME ms time axis as the 4h/15m series (bar open
    # t = i*1h); long flat stretch then a parabolic final ramp that makes
    # every trigger fire while 4h is deeply overbought.
    base = [_mk_candle(i * H, c.o, c.h, c.l, c.c, c.v)
            for i, c in enumerate(_flat_candles(320))]
    for i in range(260, 320):
        base[i] = _mk_candle(i * H, 100 + (i - 260) * 0.8, 100 + (i - 259) * 0.8,
                             100 + (i - 260) * 0.8 - 0.2,
                             100 + (i - 260) * 0.8, 5000)
    bull4h = _trend_candles(120, start=100.0, step=0.6)
    bull4h = [_mk_candle(c.t * 4 * H, c.o, c.h, c.l, c.c, c.v) for c in bull4h]
    hot15 = [_mk_candle(c.t * 15 * 60_000, c.o, c.h, c.l, c.c, c.v)
             for c in _trend_candles(400, start=100.0, step=0.6)]
    cfg = {"_interval": "1h",
           "thresholds": {"sigmaThreshold": 2.0, "bbLength": 20, "bbStdDev": 2.0,
                          "adxPeriod": 14, "breakoutLookback": 20,
                          "breakoutMinRvol": 1.5, "breakoutRvolWindow": 20,
                          "breakoutAtrScoreMult": 3.0,
                          "momentumLookback": 10, "momentumPct": 2.0},
           "weights": {}}
    params = {"mode": "shadow", "rsi_ob": 75, "rsi_os": 25, "ext_ob": 2.5,
              "ext_os": -2.5, "rsi_ob_relaxed": 82, "rsi_os_relaxed": 18,
              "ext_ob_relaxed": 3.5, "ext_os_relaxed": -3.5,
              "adx_trend_threshold": 35, "mtf_enabled": True}
    trades_gated, vetoes = _kernel_sim(
        "TEST", base, H, cfg, candles_4h=bull4h, candles_15m=hot15,
        late_entry_params=dict(params))
    trades_plain, _vetoes_plain = _kernel_sim("TEST", base, H, cfg)
    # Gate vetoed at least as many entries as it removed from the trade list.
    assert len(vetoes) > 0
    assert len(trades_gated) <= len(trades_plain)
    assert all(v["reason"].startswith("late ") for v in vetoes)
    assert all(v["coin"] == "TEST" and v["side"] == "long" for v in vetoes)


def test_oos_split_index_holds_tail_out_after_warmup():
    """The split sits oos_frac of the way through the TRADEABLE window
    [warmup, n_bars); the warmup indicator prefix is always in-sample."""
    from hermes_trader.backtest import stats as kstats
    # 100 warmup + 1000 tradeable bars, 30% OOS -> split at 100 + 700 = 800.
    assert kstats.oos_split_index(1100, 100, 0.3) == 800
    # 0% OOS -> split at the end (nothing is out-of-sample).
    assert kstats.oos_split_index(1100, 100, 0.0) == 1100
    # Degenerate: fewer bars than warmup -> all in-sample (split == n_bars).
    assert kstats.oos_split_index(50, 100, 0.3) == 50


def test_oos_split_tags_trades_by_entry_bar():
    """The kernel classifies trades OOS only when their ENTRY bar is at or
    after the split bar (``split_trades``); the fixed warmup prefix is never
    OOS. A trade decided pre-split that exits after it stays in-sample."""
    from hermes_trader.backtest import stats as kstats
    H = 3600_000
    # Flat stretch, then ramp → crash → ramp → crash so positions actually
    # CLOSE on both sides of the split. A single steady ramp never retraces to
    # the trailing floor and the 180-bar hard timeout lands past the data end,
    # so its one open position is never recorded as a trade.
    base = [_mk_candle(i * H, c.o, c.h, c.l, c.c, c.v)
            for i, c in enumerate(_flat_candles(420))]

    def _seg(lo, hi, start_px, step, vol=5000):
        # Replace bars [lo, hi) with a steady ramp; OHLC keeps h >= o,c >= l.
        for k, i in enumerate(range(lo, hi)):
            px = start_px + k * step
            if step >= 0:
                base[i] = _mk_candle(i * H, px - step, px + 0.8,
                                     px - step - 0.2, px, vol)
            else:
                base[i] = _mk_candle(i * H, px - step, px - step + 0.2,
                                     px - 0.8, px, vol)

    # split_bar=324 cuts through the second ramp.
    _seg(260, 301, 100.0, 0.7)    # ramp-up: IS long trails out in the crash
    _seg(301, 317, 126.4, -1.6)   # crash:   IS short trails out in recovery
    _seg(317, 357, 103.1, 0.7)    # ramp-up: OOS long trails out in the crash
    _seg(357, 397, 128.8, -1.6)   # crash:   OOS short stops on flat recovery
    cfg = {"_interval": "1h",
           "thresholds": {"sigmaThreshold": 2.0, "bbLength": 20, "bbStdDev": 2.0,
                          "adxPeriod": 14, "breakoutLookback": 20,
                          "breakoutMinRvol": 1.5, "breakoutRvolWindow": 20,
                          "breakoutAtrScoreMult": 3.0,
                          "momentumLookback": 10, "momentumPct": 2.0},
           "weights": {}}
    split_bar = kstats.oos_split_index(len(base), 100, 0.3)  # 100 + 0.7*320 = 324
    trades, _vetoes = _kernel_sim("OOS", base, H, cfg)
    assert trades, "fixture should fire entries"
    is_trades, oos_trades = kstats.split_trades(trades, split_bar)
    # Every trade's classification matches the documented entry-bar rule.
    assert all(t.entry_bar < split_bar for t in is_trades)
    assert all(t.entry_bar >= split_bar for t in oos_trades)
    assert len(is_trades) + len(oos_trades) == len(trades)
    # Closed trades must exist on BOTH sides of the split.
    assert is_trades and oos_trades
    # Split sits in the tradeable region and the warmup prefix is always IS.
    assert split_bar == 324
    assert all(t.entry_bar >= 100 for t in trades)


def test_split_metrics_reports_sharpe_and_drawdown():
    """``trade_stats`` computes win rate, expectancy, Sharpe and max drawdown;
    an empty segment degrades to an all-zero Stats (n == 0)."""
    from hermes_trader.backtest import stats as kstats
    from hermes_trader.backtest.types import ExitReason, Trade

    H = 3600_000

    def _tr(pnl, entry, exit_):
        return Trade(
            coin="X", side="long", entry_bar=entry, exit_bar=exit_,
            entry_time_ms=entry * H, exit_time_ms=exit_ * H,
            entry_ref_px=100.0, entry_fill_px=100.0,
            exit_ref_px=100.0, exit_fill_px=100.0,
            reason=ExitReason.FLOOR_BREACH, notional_usd=100.0, fee_usd=0.0,
            pnl_gross_usd=pnl, pnl_net_usd=pnl)

    # Equity path: +10 (peak 10), -4 (dd 4), +6 (peak 16), -10 (dd 10) -> MDD 10.
    seg = [_tr(10, 0, 1), _tr(-4, 2, 3), _tr(6, 4, 5), _tr(-10, 6, 7)]
    m = kstats.trade_stats(seg, equity=100.0)
    assert m.n == 4
    assert m.wins == 2
    assert abs(m.pnl_net_usd - 2.0) < 1e-9
    assert abs(m.expectancy_usd - 0.5) < 1e-9
    assert abs(m.max_dd_usd - 10.0) < 1e-9
    assert m.sharpe != 0.0  # non-zero dispersion -> finite Sharpe
    assert kstats.trade_stats([], equity=100.0).n == 0
