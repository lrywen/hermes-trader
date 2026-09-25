"""ARP-02 B4：backtest_majors_surge baseline 臂委托统一内核的逐位一致性。

收口前 baseline 出场走脚本自建的 ``_simulate_trade``（DSL 阶梯的复刻）；收口
后 baseline 走 ``_simulate_baseline_kernel``（``DslBarExit`` 驱动生产
``DSLTracker``，policy 经 ``_policy_from_dsl_dict`` 同源构造）。本测试在同一组
确定性 bar、同一份平铺 dsl_exit 配置块上对照两条路径，断言出场 bar / 原因 /
成交价格逐位一致 —— 证明委托内核是纯替换、不改变 baseline 的历史测量结论。

其余实验臂（fade / pullback / filt / dsl_t* 等）仍走 ``_simulate_trade``，不属
本测试范围。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from hermes_trader.models.types import Candle

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

BAR_MS = 300_000
T0 = 1_700_000_000_000

# 与 live .agent-config.json 的 dsl_exit 块同形（平铺 baseline 口径）。
DSL_BLOCK = {
    "max_loss_pct": 1.0,
    "max_loss_roe_pct": 100.0,
    "protect_pct": 1.5,
    "retrace_threshold": 0.15,
    "hard_timeout_minutes": 600.0,
    "stale_flat_timeout_minutes": 240.0,
    "breakeven_trigger_pct": 2.5,
    "breakeven_lock_pct": 0.3,
    "phase2_tiers": [
        {"pct_above_entry": 2, "retrace_threshold": 0.35},
        {"pct_above_entry": 6, "retrace_threshold": 0.30},
        {"pct_above_entry": 12, "retrace_threshold": 0.20},
        {"pct_above_entry": 20, "retrace_threshold": 0.15},
    ],
}


@pytest.fixture(scope="module")
def surge() -> Any:
    mod_name = "bms_b4"
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPTS / "backtest_majors_surge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bars(rows: List[Tuple[float, float, float, float]]) -> List[Candle]:
    return [Candle(t=T0 + i * BAR_MS, o=o, h=h, l=l, c=c, v=1.0)
            for i, (o, h, l, c) in enumerate(rows)]


def _run_old(surge: Any, allbars: List[Candle]) -> Tuple[str, int, float]:
    dsl = surge.DslParams.from_config(DSL_BLOCK)
    cand = surge.Candidate(bar_idx=0, side="long", arm="baseline", score=60.0,
                           fired=[], meta={})
    tr = surge._simulate_trade(cand, allbars, 0, dsl, 10_000.0,
                               0.0, 0.0, 0.0, "TEST")
    assert tr is not None
    return tr.exit_reason, tr.hold_bars, round(tr.exit_px, 8)


def _run_kernel(surge: Any, allbars: List[Candle]) -> Tuple[str, int, float]:
    cand = surge.Candidate(bar_idx=0, side="long", arm="baseline", score=60.0,
                           fired=[], meta={})
    tr = surge._simulate_baseline_kernel(cand, allbars, 0, DSL_BLOCK, 10_000.0,
                                         0.0, 0.0, "TEST")
    assert tr is not None
    return tr.exit_reason, tr.hold_bars, round(tr.exit_px, 8)


# (名称, 信号bar + 后续bar)。信号在 idx0 收盘，成交在 idx1 开盘(=100)。
SCENARIOS = [
    # 下跌触发 max_loss：idx2 low 98.5 < stop 99。
    ("max_loss", [
        (100, 100, 100, 100),
        (100, 100.2, 99.5, 99.6),
        (99.6, 99.7, 98.5, 98.6),
    ]),
    # 冲高后回撤触发 floor：idx2 冲 103(peak)，floor=100+3*0.65=101.95；
    # idx3 low 101.0 < floor。
    ("floor_after_runup", [
        (100, 100, 100, 100),
        (100, 100.6, 99.9, 100.5),
        (100.5, 103.0, 100.4, 102.8),
        (102.8, 102.9, 101.0, 101.2),
    ]),
    # 数据末端仍持仓 → end_of_data 按末收盘。
    ("end_of_data", [
        (100, 100, 100, 100),
        (100, 100.4, 99.8, 100.2),
        (100.2, 100.5, 99.9, 100.3),
    ]),
    # 浅亏但未触止损、持续平淡到 stale_flat(240min=48根，peak<protect)。
    ("stale_flat", [(100, 100, 100, 100)] +
     [(100.0, 100.3, 99.9, 100.0) for _ in range(50)]),
]


@pytest.mark.parametrize("name,rows", SCENARIOS,
                         ids=[s[0] for s in SCENARIOS])
def test_baseline_kernel_matches_legacy(surge: Any, name: str,
                                        rows: list) -> None:
    allbars = _bars(rows)
    old = _run_old(surge, allbars)
    new = _run_kernel(surge, allbars)
    assert new == old, f"{name}: kernel={new} legacy={old}"


def test_baseline_kernel_skips_last_bar_signal(surge: Any) -> None:
    # 末根出信号无法在后续 bar 成交：两条路径都应返回 None。
    allbars = _bars([(100, 100, 100, 100)])
    cand = surge.Candidate(bar_idx=0, side="long", arm="baseline", score=60.0,
                           fired=[], meta={})
    assert surge._simulate_baseline_kernel(cand, allbars, 0, DSL_BLOCK,
                                           10_000.0, 0.0, 0.0, "TEST") is None
