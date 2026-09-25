"""T-26：破位确认的 bar/tick 口径对齐。

收口前回测把 ``consecutive_breaches_required`` 解释为"连续 N 根 K 线"，实盘
却是"连续 N 次秒级轮询"（exit_checkpoint_min_interval_s=5s）—— 5m 图上差
10 分钟，且偏差方向乐观（系统性少计止损，见参数审查 PRM-07）。

本测试在同一组确定性 5m bar 上对照两种口径：
  * bar 口径（默认，保留供粗粒度回测）：req=2 把破位延迟数根 K 线；
  * tick 口径（T-26）：bar 内按 5s 子采样多次驱动生产 check()，req=2 表示
    连续 2 次秒级轮询，价格一旦在某根 bar 破 floor，于【同一根 bar】内被
    2 个 tick 确认，不再拖延到后面的 K 线。

并断言：
  * 非法 confirm_mode 直接抛错（不静默退回）；
  * tick 间隔可配置，且默认 bar 口径与旧实现逐位一致（不破坏历史回测）。
"""
from __future__ import annotations

import pytest

from hermes_trader.agents.dsl_exit import ExitPolicy, RetraceTier
from hermes_trader.backtest.exit_dsl import BAR_MS_5M, DslBarExit
from hermes_trader.models.types import Candle

BAR_MS = BAR_MS_5M


def _bar(i: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(t=i * BAR_MS, o=o, h=h, l=l, c=c, v=1.0)


def _policy(req: int) -> ExitPolicy:
    return ExitPolicy(
        protect_pct=1.0, retrace_threshold=0.5,
        phase2_tiers=[RetraceTier(0.0, 0.5)],
        max_loss_pct=5.0, max_loss_roe_pct=50.0,
        consecutive_breaches_required=req,
        breach_confirm_sec=0.0, hard_stop_confirm_sec=0.0)


# bar0：high=106 建立 peak（floor→103），low=99.5 不破 max_loss。
# bar1：low=101 破 floor=103 → 此 bar 起算破位。
# bar2..：low 持续在 floor 之下。
def _series() -> list[Candle]:
    return [
        _bar(0, 100.0, 106.0, 99.5, 105.5),
        _bar(1, 105.5, 105.6, 101.0, 101.2),
        _bar(2, 101.2, 101.3, 100.8, 100.9),
        _bar(3, 100.9, 101.0, 100.5, 100.6),
        _bar(4, 100.6, 100.7, 100.0, 100.1),
    ]


def _first_exit(req: int, mode: str, **kw) -> tuple[int, str]:
    ex = DslBarExit(side="long", entry_px=100.0, entry_time_ms=0,
                    policy=_policy(req), leverage=1, coin="T",
                    entry_atr_pct=1.0, bar_ms=BAR_MS,
                    confirm_mode=mode, **kw)
    for i, b in enumerate(_series()):
        ev = ex.on_bar(b, i)
        if ev is not None:
            return i, ev.reason.value if hasattr(ev.reason, "value") else str(ev.reason)
    raise AssertionError("never exited")


def test_bar_mode_delays_breach_by_bars() -> None:
    # 旧 bar 口径复现 PRM-07：req=1 在 bar1，req>=2 被连续破位计数拖到更
    # 后面的 bar（bar3）—— 破位确认按 K 线而非秒计。
    assert _first_exit(1, "bar")[0] == 1
    assert _first_exit(2, "bar")[0] == 3
    assert _first_exit(3, "bar")[0] == 3


def test_tick_mode_confirms_within_breach_bar() -> None:
    # T-26：无论 req=1/2/3，破位都在首个破 floor 的 bar（bar1）内由子 bar
    # tick 确认，不再被拖到 bar4。
    for req in (1, 2, 3):
        bar_idx, reason = _first_exit(req, "tick")
        assert bar_idx == 1, f"req={req} exited at {bar_idx}, expected 1"
        assert reason == "floor_breach"


def test_tick_mode_consec_count_is_sub_bar_polls() -> None:
    # req=2 时 consec 在 bar1 内累计到 2（两个 5s tick），出场延迟以 tick
    # 而非 bar 表达。
    ex = DslBarExit(side="long", entry_px=100.0, entry_time_ms=0,
                    policy=_policy(2), leverage=1, coin="T",
                    entry_atr_pct=1.0, bar_ms=BAR_MS,
                    confirm_mode="tick", tick_confirm_s=5.0)
    bars = _series()
    assert ex.on_bar(bars[0], 0) is None
    assert ex._tr.consecutive_breaches == 0
    ev = ex.on_bar(bars[1], 1)
    assert ev is not None
    assert ex._tr.consecutive_breaches == 2


def test_invalid_confirm_mode_raises() -> None:
    with pytest.raises(ValueError, match="confirm_mode"):
        DslBarExit(side="long", entry_px=100.0, entry_time_ms=0,
                   policy=_policy(1), confirm_mode="bogus")


def test_tick_interval_is_configurable() -> None:
    # 更粗的 tick 间隔（150s）→ 一根 300s bar 内只有 2 个 tick；req=2 仍可在
    # bar1 内由这 2 个 tick 确认。
    bar_idx, _ = _first_exit(2, "tick", tick_confirm_s=150.0)
    assert bar_idx == 1


def test_default_mode_is_tick() -> None:
    # 不传 confirm_mode：生产默认 tick，req=2 在首个破 floor 的 bar1 内由
    # 两个 5s tick 确认（PRM-07 收口后的默认口径）。
    ex = DslBarExit(side="long", entry_px=100.0, entry_time_ms=0,
                    policy=_policy(2), leverage=1, coin="T",
                    entry_atr_pct=1.0, bar_ms=BAR_MS)
    for i, b in enumerate(_series()):
        if ex.on_bar(b, i) is not None:
            assert i == 1
            return
    raise AssertionError("never exited")
