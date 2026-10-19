"""config_parity_guard 单测 + 危险方向零漂移硬断言。

含两类：
  * 纯分类器逻辑（合成数据，始终离线运行）；
  * 对真实生产 live 配置的守卫——live 文件不在仓库内（仅本机/容器存在），
    缺失时 skip；存在时断言危险方向漂移数为 0。这是「修复自噬」的根治门禁。
"""
from __future__ import annotations

import json
import os

import pytest

from hermes_trader.agents import config_parity_guard as g
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS


# ── 分类器逻辑 ─────────────────────────────────────────────────────────────
def test_hi_danger_detects_canonical_looser():
    c = {"max_total_notional_pct": 4.0}
    l = {"max_total_notional_pct": 2.0}
    out = g.find_dangerous_divergences(c, l)
    assert out and out[0]["leaf"] == "max_total_notional_pct"


def test_lo_danger_detects_canonical_looser():
    c = {"min_available_margin_pct": 0.1}
    l = {"min_available_margin_pct": 0.2}
    assert g.find_dangerous_divergences(c, l)


def test_safe_direction_not_flagged():
    # canonical 更严（lo_danger 下 canonical 更大）→ 不算危险
    c = {"min_available_margin_pct": 0.3}
    l = {"min_available_margin_pct": 0.2}
    assert g.find_dangerous_divergences(c, l) == []


def test_armed_bool_detects_rearm():
    c = {"signal_enforcement": {"boost": True}}
    l = {"signal_enforcement": {"boost": False}}
    assert g.find_dangerous_divergences(c, l)


def test_armed_mode_detects_disarmed_filter():
    c = {"trend_filter_200ma": {"mode": "off"}}
    l = {"trend_filter_200ma": {"mode": "enforce"}}
    out = g.find_dangerous_divergences(c, l)
    assert out and out[0]["leaf"] == "trend_filter_200ma.mode"


def test_armed_mode_classifier_detects_weaker_canonical():
    # armed_mode 下保护强度落后即危险（rank off0<shadow1<enforce2）。
    assert g.find_dangerous_divergences(
        {"trend_filter_200ma": {"mode": "shadow"}},
        {"trend_filter_200ma": {"mode": "enforce"}})
    assert g.find_dangerous_divergences(
        {"trend_filter_200ma": {"mode": "off"}},
        {"trend_filter_200ma": {"mode": "shadow"}})


def test_equal_values_no_finding():
    c = {"max_total_notional_pct": 2.0}
    assert g.find_dangerous_divergences(c, c) == []


def test_sanctioned_looser_leaves_are_registered_and_exempt():
    """P0-1: 白名单叶必须已登记（可枚举可见），且 find 不将其计为新漂移。"""
    assert g._SANCTIONED_LOOSER_LEAVES <= set(g.LEAF_DANGER_KIND), (
        "白名单叶必须登记进 LEAF_DANGER_KIND，否则仍是假阴性"
    )
    # 用真实 canonical/live 验证：这 3 个叶 canonical 确实比 live 宽松，
    # 但白名单豁免使其不被报告（它们是已知、有注释的有意漂移）。
    import json as _json
    path = _live_config_path()
    if path is None:
        pytest.skip("live 配置不在本环境")
    with open(path, encoding="utf-8") as f:
        live = _json.load(f)
    findings = g.find_dangerous_divergences(CANONICAL_DEFAULTS, live)
    leaked = sorted(set(g.LEAF_DANGER_KIND) & g._SANCTIONED_LOOSER_LEAVES - {
        x["leaf"] for x in findings})
    # 白名单叶不应出现在 findings 中（它们被豁免）。
    assert not (g._SANCTIONED_LOOSER_LEAVES & {x["leaf"] for x in findings}), leaked


def test_unsanctioned_new_dangerous_divergence_is_reported():
    """P0-1 防复发: 不在白名单的已登记危险叶，若 canonical 宽松于 live 必须报出。"""
    # max_total_notional_pct 已登记为 hi_danger 且不在白名单。
    assert "max_total_notional_pct" not in g._SANCTIONED_LOOSER_LEAVES
    assert g.find_dangerous_divergences(
        {"max_total_notional_pct": 5.0}, {"max_total_notional_pct": 1.0}) == [
        {"leaf": "max_total_notional_pct", "canonical": 5.0, "live": 1.0},
    ]
    # 反向（canonical 更严）不报。
    assert g.find_dangerous_divergences(
        {"max_total_notional_pct": 1.0}, {"max_total_notional_pct": 5.0}) == []


def test_unregistered_leaf_ignored():
    assert g.find_dangerous_divergences({"random_leaf": 9}, {"random_leaf": 1}) == []


# ── recheck 2026-10-18 §5.1: 门禁判据改为「漂移 ⊆ 登记」 ────────────────────
def test_uncovered_divergences_empty_against_live():
    """当前生产 53 处漂移必须全部显式登记归属（危险/白名单/已复核安全）。

    修复前门禁对未登记漂移零感知（有效覆盖 0）；修复后断言「漂移 ⊆ 登记」，
    任何未登记的新漂移都使 uncovered 非空 → 门禁红。
    """
    path = _live_config_path()
    if path is None:
        pytest.skip("live 配置不在本环境")
    with open(path, encoding="utf-8") as f:
        live = json.load(f)
    uncovered = g.uncovered_divergences(CANONICAL_DEFAULTS, live)
    assert uncovered == [], (
        "以下生产漂移既未登记为危险(LEAF_DANGER_KIND)、也未列入白名单/已复核"
        "安全(_REVIEWED_SAFE_DIVERGENCES)，门禁对其零感知："
        + ", ".join(uncovered)
        + "。请显式审查后登记归属，否则无法保证未来 fail-open 漂移被捕获。")


def test_uncovered_divergences_flags_new_unregistered_leaf():
    """防复发: 新增一个既不在危险表、也不在白名单/安全表的漂移 → uncovered 非空。

    fix-verification §11③：_REVIEWED_SAFE_DIVERGENCES 改为绑定复核当时的值，
    叶名已登记但值与登记不一致同样视为「未复核」→ 报出。
    """
    # 用真实已登记叶名 + 与登记一致的值 → covered（uncovered 空）。
    #   _comment 登记 ('', '10 USDC 实盘 · 10倍杠杆 · BTC/ETH增强版 | ...')
    #   mode     登记 ('OFF', 'SHADOW')
    c = {"_comment": "", "mode": "OFF"}
    l = {"_comment": "10 USDC 实盘 · 10倍杠杆 · BTC/ETH增强版 | 选择性开启辅助功能",
         "mode": "SHADOW"}
    assert g.uncovered_divergences(c, l) == []
    # 新增一个未登记的漂移叶 → 必须被报告。
    c2 = {"_comment": "", "mode": "OFF", "brand_new_leaf": 1}
    l2 = {"_comment": "10 USDC 实盘 · 10倍杠杆 · BTC/ETH增强版 | 选择性开启辅助功能",
          "mode": "SHADOW", "brand_new_leaf": 2}
    assert "brand_new_leaf" in g.uncovered_divergences(c2, l2)


def test_uncovered_divergences_flags_value_drift_on_reviewed_leaf():
    """fix-verification §11③ 根治「三代假绿」：已登记为「已复核安全」的叶，
    只要值从复核当时漂移（如 max_daily_loss_usd 从 -3 改成 -500）即当次变红。
    修复前只按叶名登记，值变更完全不可见；绑定 (canonical, live) 后必须报出。
    """
    # 登记值 max_daily_loss_usd = (-2.0, -3)。live 仍为 -3 → covered。
    c = {"max_daily_loss_usd": -2.0}
    l = {"max_daily_loss_usd": -3}
    assert g.uncovered_divergences(c, l) == []
    # live 从 -3 改 -500（更宽松止损/更大的允许亏损）→ 值漂移 → uncovered 报出。
    l2 = {"max_daily_loss_usd": -500}
    assert "max_daily_loss_usd" in g.uncovered_divergences(c, l2)
    # canonical 漂移（repo 默认被改）同样报出。
    c2 = {"max_daily_loss_usd": -10.0}
    assert "max_daily_loss_usd" in g.uncovered_divergences(c2, l)


def test_uncovered_divergences_empty_on_equal_configs():
    c = {"max_total_notional_pct": 2.0, "mode": "SHADOW"}
    assert g.uncovered_divergences(c, c) == []


# ── 真实生产配置守卫 ────────────────────────────────────────────────────────
def _live_config_path() -> str | None:
    candidates = [
        os.environ.get("HERMES_LIVE_CONFIG_PATH"),
        "/home/ldy/hermes-deploy/.agent-config.json",
        "/data/.agent-config.json",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def test_no_dangerous_failopen_divergence_against_live():
    path = _live_config_path()
    if path is None:
        pytest.skip("live 配置文件不在本环境（仅本机/容器做零漂移守卫）")
    with open(path, encoding="utf-8") as f:
        live = json.load(f)
    findings = g.find_dangerous_divergences(CANONICAL_DEFAULTS, live)
    assert findings == [], (
        "canonical 在以下叶子比 live 宽松，配置丢键深合并会放宽保护："
        + "; ".join(f"{x['leaf']} (canon={x['canonical']!r}, live={x['live']!r})"
                    for x in findings))


def test_risk_per_trade_live_delta_is_safe_direction():
    # 覆盖缺口（P4）：测试历史只钉 canonical 0.02，未断言 live 0.026。登记为
    # 已知安全方向——live 风险比例更大，丢键回落到 canonical 只会收紧，不放宽。
    path = _live_config_path()
    if path is None:
        pytest.skip("live 配置文件不在本环境")
    with open(path, encoding="utf-8") as f:
        live = json.load(f)
    canon = CANONICAL_DEFAULTS["atr_risk_sizing"]["risk_per_trade_pct"]
    assert canon <= live["atr_risk_sizing"]["risk_per_trade_pct"]


def test_uncovered_divergences_flags_live_looser_on_danger_leaf():
    """recheck-4 P0-B 防复发：危险叶「live 比 canonical 宽松」（保护被当场放宽）
    必须被 uncovered 报出。修复前危险叶在 uncovered 里整叶 continue 跳过，而
    find_dangerous_divergences 只查「canonical 宽松」，数值限制被 live 放宽
    （如 max_total_notional_pct 2→100）在两侧都不可见。
    """
    # hi_danger：live 值更高 = 单笔名义仓位更大 = 更宽松 → 必须报
    c = {"max_total_notional_pct": 2.0}
    l = {"max_total_notional_pct": 100.0}
    assert "max_total_notional_pct" in g.uncovered_divergences(c, l)
    # 反向（canonical 宽松）仍由 find_dangerous_divergences 管，uncovered 不重复
    assert g.uncovered_divergences({"max_total_notional_pct": 2.0},
                                   {"max_total_notional_pct": 1.0}) == []


def test_uncovered_divergences_flags_live_only_new_leaf():
    """recheck-4 P0-C 防复发：只存在于 live 的新叶（canonical 无此键）必须被报出。
    修复前 diff 只遍历 canonical 的键，live-only 新叶永远进不了判据。
    """
    c = {"max_total_notional_pct": 2.0}
    l = {"max_total_notional_pct": 2.0,
         "totally_new_risk_key": {"max_loss_pct": 99}}
    u = g.uncovered_divergences(c, l)
    assert "totally_new_risk_key.max_loss_pct" in u, u
    # canonical-only 叶（live 未覆盖，沿用 canonical 默认）不构成「live 新增」不报。
    # hard_stop 只在 canonical，live 未设置 → 沿用 canonical 默认 → 不算 live 新增。
    assert g.uncovered_divergences({"max_total_notional_pct": 2.0, "hard_stop": 1.0},
                                   {"max_total_notional_pct": 2.0}) == []


def test_tuple_vs_list_equivalent_is_not_a_divergence():
    """P0-2 (2026-10-19): canonical 用 tuple、JSON live 用 list 表达同一序列时
    （如 dsl_exit.ignite_periods），仅是容器类型不同、语义相同，不得误报漂移。"""
    c = {"dsl_exit": {"ignite_periods": ("5m", "15m", "1h")}}
    l = {"dsl_exit": {"ignite_periods": ["5m", "15m", "1h"]}}
    assert g.uncovered_divergences(c, l) == []
    assert g.find_dangerous_divergences(c, l) == []


def test_real_sequence_value_change_is_still_flagged():
    """序列归一化不得掩盖真实的值漂移：元素不同仍须报出。"""
    c = {"dsl_exit": {"ignite_periods": ("5m", "15m", "1h")}}
    l = {"dsl_exit": {"ignite_periods": ["15m", "1h"]}}
    assert "dsl_exit.ignite_periods" in g.uncovered_divergences(c, l)
