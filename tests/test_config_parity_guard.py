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
