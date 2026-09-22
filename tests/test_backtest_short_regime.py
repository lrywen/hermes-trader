"""Audit 2026-09-22：backtest_short_regime 重写为统一内核后的单测。

只测纯逻辑（不联网）：variant 准入判定 + 下跌合成行情能经扫描+内核产出做空交易。
"""
from __future__ import annotations

import pytest

from hermes_trader.backtest import cost as kcost
from hermes_trader.backtest import signals as ksig
from hermes_trader.backtest.types import ExitReason, Trade
from hermes_trader.models.types import Candle
from scripts import backtest_short_regime as bsr

HOUR = 3_600_000


def _trade(coin: str, entry_h: int, exit_h: int, notional: float,
           pnl_net: float) -> Trade:
    """构造一笔最小 Trade（进出场价不影响组合约束，只看时间/名义/盈亏）。"""
    return Trade(
        coin=coin, side="short", entry_bar=entry_h, exit_bar=exit_h,
        entry_time_ms=entry_h * HOUR, exit_time_ms=exit_h * HOUR,
        entry_ref_px=100.0, entry_fill_px=100.0,
        exit_ref_px=100.0, exit_fill_px=100.0,
        reason=ExitReason.MAX_LOSS,
        notional_usd=notional, fee_usd=0.0,
        pnl_gross_usd=pnl_net, pnl_net_usd=pnl_net,
    )


def _falling_candles(n: int = 160, start: float = 100.0) -> list[Candle]:
    """前 2/3 平稳，后 1/3 持续放量下跌（触发做空信号 + ta_late 短镜放行）。"""
    candles: list[Candle] = []
    pivot = int(n * 0.66)
    base = start
    for i in range(n):
        o = base
        if i >= pivot:
            c = base * 0.985  # -1.5%/bar 加速下跌
            v = 5000.0
        else:
            c = base * (1 + 0.0002)  # 微涨盘整
            v = 1000.0
        h = max(o, c) * 1.0005
        l = min(o, c) * 0.9995
        candles.append(Candle(t=i * HOUR, o=o, h=h, l=l, c=c, v=v))
        base = c
    return candles


def test_variant_admit_filters():
    base = {"bar": 10, "score": 30.0, "own_down": True, "macro": "down"}
    assert bsr._variant_admit("A")(base) is True
    assert bsr._variant_admit("B")(base) is True
    assert bsr._variant_admit("C")(base) is True
    assert bsr._variant_admit("D")(base) is True
    assert bsr._variant_admit("E", 25.0)(base) is True
    assert bsr._variant_admit("E", 40.0)(base) is False  # score 30 < 40

    no_trend = dict(base, own_down=False, macro="neutral")
    assert bsr._variant_admit("B")(no_trend) is False
    assert bsr._variant_admit("C")(no_trend) is False
    assert bsr._variant_admit("D")(no_trend) is False
    # A baseline 仍放行。
    assert bsr._variant_admit("A")(no_trend) is True

    with pytest.raises(ValueError):
        bsr._variant_admit("Z")


def test_scan_candidates_records_inputs_and_kernel_runs(monkeypatch):
    candles = _falling_candles()
    # 宏观序列直接给全 down，使 variant C/D 视为熊市（避免依赖 BTC 拉取）。
    macro_ts = [c.t for c in candles]
    macro_regime = ["down"] * len(candles)

    # 生产 trigger 阈值/权重。
    cfg = ksig.default_heuristic_config(warmup=100)
    cands, stats = bsr._scan_candidates(
        "TEST", candles, None, macro_ts, macro_regime,
        cfg.thresholds, cfg.weights, le_params={},
        warmup=100, sim_ms=HOUR,
    )
    # 下跌段应产出 base short 候选。
    assert cands, "下跌行情未产出做空候选"
    assert all(c["macro"] == "down" for c in cands)

    # 经统一内核跑 variant A：应完成至少一笔做空交易。
    from dataclasses import replace

    from hermes_trader.agents.dsl_exit import _build_policy_from_config
    policy = replace(_build_policy_from_config())
    trades = bsr._run_variant(
        candles, cands, policy, bsr._variant_admit("A"),
        coin="TEST", leverage=1, notional=10.0,
        cost=kcost.CostModel(), sim_ms=HOUR,
    )
    assert trades
    assert all(t.side == "short" for t in trades)


def test_portfolio_constraint_caps_concurrency_and_same_coin():
    # 原回放：equity=100, fraction=0.2 → base_margin=20；notional=20 → leverage=1。
    # 三笔时间重叠（h0-10 / h1-11 / h2-12），max_concurrent=2 → 第三笔被拒。
    trades = [
        _trade("AAA", 0, 10, 20.0, -1.0),
        _trade("BBB", 1, 11, 20.0, -1.0),
        _trade("CCC", 2, 12, 20.0, -1.0),
    ]
    out = bsr._apply_portfolio_constraint(trades, 100.0, 0.2, 2)
    assert [t.coin for t in out] == ["AAA", "BBB"]

    # 同币重叠：第二笔 AAA 在第一笔未平时到达 → 被拒。
    same_coin = [_trade("AAA", 0, 10, 20.0, -1.0),
                 _trade("AAA", 5, 15, 20.0, -1.0)]
    out2 = bsr._apply_portfolio_constraint(same_coin, 100.0, 0.2, 5)
    assert [t.entry_time_ms for t in out2] == [0]


def test_portfolio_constraint_rebases_equity_after_close():
    # AAA h0-10 亏 -10 → 权益 100→90；BBB h10 入场（AAA 刚平）按新权益下单。
    trades = [
        _trade("AAA", 0, 10, 20.0, -10.0),
        _trade("BBB", 10, 20, 20.0, 0.0),
    ]
    out = bsr._apply_portfolio_constraint(trades, 100.0, 0.2, 5)
    assert len(out) == 2
    # AAA 名义保持 20（首笔按初始权益）；BBB 在 90 权益下名义=90*0.2*1=18。
    assert out[0].notional_usd == 20.0
    assert out[1].notional_usd == 18.0
    # 缩放口径：BBB 原 notional=20，新 18 → 缩放比 0.9。
    assert out[1].pnl_net_usd == 0.0


def test_portfolio_constraint_totals_bounded_by_equity():
    # 顺序成交（互不重叠），全部亏损，账户单调下降；名义应随权益递减而非恒定。
    trades = [_trade("AAA", h, h + 5, 20.0, -5.0) for h in range(0, 60, 10)]
    out = bsr._apply_portfolio_constraint(trades, 100.0, 0.2, 5)
    notionals = [t.notional_usd for t in out]
    # 每亏 -5 后下一笔名义 = 上一权益*0.2，应严格递减。
    assert all(a > b for a, b in zip(notionals, notionals[1:]))
    # 全部名义合计远小于"无脑固定20×6=120"，受真实本金约束。
    assert sum(notionals) < 120.0

