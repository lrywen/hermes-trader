"""Audit 2026-09-22：backtest_short_regime 重写为统一内核后的单测。

只测纯逻辑（不联网）：variant 准入判定 + 下跌合成行情能经扫描+内核产出做空交易。
"""
from __future__ import annotations

import pytest

from hermes_trader.backtest import cost as kcost
from hermes_trader.backtest import signals as ksig
from hermes_trader.models.types import Candle
from scripts import backtest_short_regime as bsr

HOUR = 3_600_000


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
