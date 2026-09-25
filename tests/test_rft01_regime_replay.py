"""RFT-01：回测内核 regime 层同源回放测试。

验证三件事：
1. 默认（regime_replay=False）口径不变：policy 平铺、regime 仅作信号/常量；
2. regime_replay=True 时，regime 由生产纯函数 classify_candles 在 PIT 窗口
   （只含决策 bar 及之前）判定，无前视；
3. 出场 policy 通过生产同源 select_exit_params / resolve_regime_clocks 选档，
   Trade 上的 entry_regime / exit_label 与所选档一致。
"""
from __future__ import annotations

from hermes_trader.agents.dsl_exit import ExitPolicy
from hermes_trader.backtest import driver
from hermes_trader.backtest import regime as kregime
from hermes_trader.backtest.cost import CostModel
from hermes_trader.backtest.types import Signal
from hermes_trader.models.types import Candle

BAR_MS = 3_600_000  # 1h bars
T0 = 1_700_000_000_000
ZERO_COST = CostModel(round_trip_fee_bps=0.0, entry_slip_bps=0.0,
                      exit_slip_bps=0.0, stop_delay_slip_bps=0.0)


def _policy() -> ExitPolicy:
    return ExitPolicy(
        max_loss_pct=0.8, max_loss_roe_pct=100.0, protect_pct=1.5,
        retrace_threshold=0.15, hard_timeout_minutes=1e9,
        stale_flat_timeout_minutes=0.0,
        hard_stop_confirm_sec=0.0, breach_confirm_sec=0.0,
    )


def _uptrend_bars(n: int = 130) -> list[Candle]:
    # 稳定上行：每根 +0.5%，累计涨幅足以让 EMA20/30 + slope 判 'up'。
    bars = []
    px = 100.0
    for i in range(n):
        o = px
        c = px * 1.005
        bars.append(Candle(t=T0 + i * BAR_MS, o=o, h=c * 1.001,
                           l=o * 0.999, c=c, v=1.0))
        px = c
    return bars


def _flat_bars(n: int = 130) -> list[Candle]:
    bars = []
    for i in range(n):
        bars.append(Candle(t=T0 + i * BAR_MS, o=100.0, h=100.05,
                           l=99.95, c=100.0, v=1.0))
    return bars


def test_pit_classify_uptrend_is_up() -> None:
    bars = _uptrend_bars()
    r = kregime.classify_pit_regime(bars, len(bars) - 2)
    assert r == "up"


def test_pit_classify_does_not_look_ahead() -> None:
    # 前段平铺，决策 bar 之后才暴涨：决策时刻必须判不出 up。
    bars = _flat_bars(110) + _uptrend_bars(40)
    decision = 108
    r = kregime.classify_pit_regime(bars, decision)
    assert r != "up"
    # 但在包含暴涨段的更晚 bar 上能判 up（证明窗口本身有效）。
    assert kregime.classify_pit_regime(bars, len(bars) - 2) == "up"


def test_regime_replay_labels_trade_with_trend_bucket() -> None:
    bars = _uptrend_bars()
    sig = Signal(bar_index=100, side="long")
    dsl = {
        "regime_aware": {
            "enabled": True,
            "trend_ride": {"protect_pct": 2.5, "retrace_threshold": 0.4},
            "max_loss": {
                "trend": {"max_loss_pct": 4.0, "max_loss_roe_pct": 20.0},
                "non_trend": {"max_loss_pct": 0.8, "max_loss_roe_pct": 10},
            },
            "clocks": {
                "enabled": True,
                "trend": {"hard_timeout_minutes": 240.0,
                          "stale_flat_timeout_minutes": 120.0},
                "non_trend": {"hard_timeout_minutes": 120.0,
                              "stale_flat_timeout_minutes": 60.0},
            },
        }
    }
    trades = driver.run(
        bars, [sig], _policy(), coin="TST", leverage=1,
        notional_usd=1000.0, cost=ZERO_COST, bar_ms=BAR_MS,
        regime_replay=True, dsl_config=dsl,
    )
    assert len(trades) == 1
    t = trades[0]
    assert t.entry_regime == "up"
    assert t.exit_label.startswith("trend_ride")


def test_default_off_keeps_flat_policy_and_blank_label() -> None:
    bars = _uptrend_bars()
    sig = Signal(bar_index=100, side="long")
    trades = driver.run(
        bars, [sig], _policy(), coin="TST", leverage=1,
        notional_usd=1000.0, cost=ZERO_COST, bar_ms=BAR_MS,
    )
    assert len(trades) == 1
    # 默认口径：不现判 regime，标签留空。
    assert trades[0].exit_label == ""


def test_replay_signal_carried_regime_wins() -> None:
    # 信号自带真实 regime（replay 记录）时直接采用，不重判。
    bars = _uptrend_bars()
    sig = Signal(bar_index=100, side="long", entry_regime="down")
    trades = driver.run(
        bars, [sig], _policy(), coin="TST", leverage=1,
        notional_usd=1000.0, cost=ZERO_COST, bar_ms=BAR_MS,
        regime_replay=True, dsl_config={"regime_aware": {"enabled": False}},
    )
    assert trades[0].entry_regime == "down"


def test_short_window_classify_returns_neutral() -> None:
    bars = _flat_bars(5)
    assert kregime.classify_pit_regime(bars, 4) in ("neutral", "chop")
