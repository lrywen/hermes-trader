# -*- coding: utf-8 -*-
"""B-guard：回测内核 guard 的杠杆一致性 + 逐币成本表齐全度守卫测试。

钉死两件生产可比性约束：
  * assert_leverage_allowed —— 回测杠杆必须等于生产 LIVE_LEVERAGE(10)，
    否则保证金/爆仓/ROE 止损口径不可比；
  * assert_cost_table_complete / check_cost_table_coverage —— 回测币池必须被
    per_coin_half_spread_bps.json 完整覆盖，缺币即硬拒绝（防止静默回退
    0.31bps 低估成本）。
另校验随包成本表确实覆盖生产 81 币研究池（防止数据文件自身回退）。
"""
from __future__ import annotations

import json

import pytest

from hermes_trader.backtest import guard

# ── leverage ───────────────────────────────────────────────────────────────

def test_leverage_live_value_accepted():
    guard.assert_leverage_allowed(guard.LIVE_LEVERAGE)
    assert guard.LIVE_LEVERAGE == 10


@pytest.mark.parametrize("bad", [1, 3, 5, 20, 50, 0, -10])
def test_leverage_divergent_rejected(bad):
    with pytest.raises(ValueError, match="leverage"):
        guard.assert_leverage_allowed(bad)


@pytest.mark.parametrize("bad", [10.0, "10", True, False, None])
def test_leverage_invalid_type_rejected(bad):
    with pytest.raises(ValueError, match="leverage"):
        guard.assert_leverage_allowed(bad)


# ── cost table coverage ────────────────────────────────────────────────────

def test_cost_coverage_empty_when_all_covered():
    coins = ["BTC", "ETH", "SOL"]
    assert guard.check_cost_table_coverage(coins, ["ETH", "BTC", "SOL"]) == []


def test_cost_coverage_case_insensitive():
    assert guard.check_cost_table_coverage(["btc", "Eth"], ["BTC", "ETH"]) == []


def test_cost_coverage_lists_missing_coins():
    missing = guard.check_cost_table_coverage(
        ["BTC", "GHOST", "SOL", "SHADY"], ["BTC", "SOL"])
    assert set(missing) == {"GHOST", "SHADY"}


def test_cost_table_complete_raises_on_missing():
    with pytest.raises(ValueError, match="cost table missing"):
        guard.assert_cost_table_complete(["BTC", "GHOST"], ["BTC"])


def test_cost_table_complete_passes_when_fully_covered():
    guard.assert_cost_table_complete(["a", "b"], ["A", "B", "C"])


def test_shipped_cost_table_covers_live_research_pool():
    """随包半价差表必须覆盖生产研究池（/tmp/p6b1_recon.json 的 81 币）。

    这是守卫自身的数据完整性回归：若成本表缺币，按 B-3 的逻辑生产回测就该
    在调度前被拦下。/tmp 研究池文件缺失时跳过（非 CI 资产）。
    """
    p = ("hermes_trader/data/per_coin_half_spread_bps.json")
    data = json.loads(open(p, encoding="utf-8").read())
    covered = list((data.get("half_spread_bps") or {}).keys())
    try:
        pool = json.load(open("/tmp/p6b1_recon.json", encoding="utf-8"))
        coins = pool["bt_ready_pool"]
    except (OSError, KeyError):
        pytest.skip("生产研究池 /tmp/p6b1_recon.json 不可用（非 CI 资产）")
    missing = guard.check_cost_table_coverage(coins, covered)
    assert missing == [], f"成本表未覆盖研究池币: {missing}"
