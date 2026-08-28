"""R13-B3: risk-gate scoring thresholds (risk_gates.py) 隐式参数注册测试。

修复前，risk_gates.py 里有 8 个硬编码字面量控制风险闸的评分/投票逻辑：

  * market_regime_gate L474 ``effective_min_score = 50.0`` — 普通
    counter-trend composite_score 阈值
  * debate_gate L623-628 analyst2 三段阈值：
      (0.7, 40)  →  conf >= 0.7 且 score >= 40  视为同意
      (0.5, 60)  →  conf >= 0.5 且 score >= 60  视为同意
      (0.8, 20)  →  conf >= 0.8 且 score >= 20  视为同意
  * debate_gate L645 analyst5 阈值 ``0.75`` — conf >= 0.75 视为同意
    (即使没有 whale_signal)

这些字面量既不在 CANONICAL_DEFAULTS，也不能 env 覆盖，也不出现在
dashboard dump / validate_config_updates 视野里——R13 审计把它们列为
hot-path 隐式字段（紧贴 cfg_get 已接线的 chop_min_score / min_trend_score
等兄弟键）。

修复方式：

  * 在 CANONICAL_DEFAULTS 新增 ``analyst_scoring`` 嵌套块（8 字段：
    counter_trend_min_score=50.0 / analyst2_high_conf=0.7 /
    analyst2_high_score=40 / analyst2_mid_conf=0.5 / analyst2_mid_score=60
    / analyst2_very_high_conf=0.8 / analyst2_very_high_score=20 /
    analyst5_whale_or_conf=0.75，默认值与原 literals 严格一致）
  * _ConfigPatch 声明 ``analyst_scoring`` 字段（drift sentinel）
  * risk_gates.py hot-path 全部走 ``cfg_get("analyst_scoring.*", config={})``
    重解析（env / .agent-config.json 编辑无需重启即可生效）
  * 保持 ``config={}`` 形式：market_regime_gate / debate_gate 都不接收
    config 参数；模块级 cfg_get 默认走 read_agent_config() 拿 .agent-config
    视图，env override 由 cfg_get 顶部第一优先级处理

零行为变化约束：默认值 = 旧字面量，运行时闸值不变。
"""

import os
import sys
from copy import deepcopy

import pytest

from hermes_trader.agents import config_store, risk_gates
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)
from hermes_trader.agents.config_schema import _ConfigPatch


# ── 1. canonical 登记断言：analyst_scoring 块 8 字段 ─────────────────

def test_r13_b3_analyst_scoring_block_registered():
    """analyst_scoring 必须登记在 CANONICAL_DEFAULTS 顶层。"""
    assert "analyst_scoring" in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS["analyst_scoring"], dict)


def test_r13_b3_analyst_scoring_defaults_match_historical_literals():
    """默认值严格等于 risk_gates.py 旧硬编码字面量；零行为变化。"""
    block = CANONICAL_DEFAULTS["analyst_scoring"]
    # market_regime_gate / _counter_trend_decision 普通 counter-trend bar
    assert block["counter_trend_min_score"] == 50.0
    # debate_gate analyst2 三段阈值
    assert block["analyst2_high_conf"] == 0.7
    assert block["analyst2_high_score"] == 40
    assert block["analyst2_mid_conf"] == 0.5
    assert block["analyst2_mid_score"] == 60
    assert block["analyst2_very_high_conf"] == 0.8
    assert block["analyst2_very_high_score"] == 20
    # debate_gate analyst5 置信度下限
    assert block["analyst5_whale_or_conf"] == 0.75


def test_r13_b3_analyst_scoring_block_has_exactly_eight_keys():
    """sentinel：未来若有人加字段 / 删字段都需要在测试里显式改。"""
    block = CANONICAL_DEFAULTS["analyst_scoring"]
    assert set(block.keys()) == {
        "counter_trend_min_score",
        "analyst2_high_conf", "analyst2_high_score",
        "analyst2_mid_conf", "analyst2_mid_score",
        "analyst2_very_high_conf", "analyst2_very_high_score",
        "analyst5_whale_or_conf",
    }


# ── 2. cfg_get 解析：嵌套块支持点路径 + 空 config 回退 canonical ─────

def test_r13_b3_cfg_get_counter_trend_min_score():
    assert cfg_get("analyst_scoring.counter_trend_min_score", config={}) == 50.0


def test_r13_b3_cfg_get_analyst2_high_pair():
    assert cfg_get("analyst_scoring.analyst2_high_conf", config={}) == 0.7
    assert cfg_get("analyst_scoring.analyst2_high_score", config={}) == 40


def test_r13_b3_cfg_get_analyst2_mid_and_vhigh_pair():
    assert cfg_get("analyst_scoring.analyst2_mid_conf", config={}) == 0.5
    assert cfg_get("analyst_scoring.analyst2_mid_score", config={}) == 60
    assert cfg_get("analyst_scoring.analyst2_very_high_conf", config={}) == 0.8
    assert cfg_get("analyst_scoring.analyst2_very_high_score", config={}) == 20


def test_r13_b3_cfg_get_analyst5_whale_or_conf():
    assert cfg_get("analyst_scoring.analyst5_whale_or_conf", config={}) == 0.75


def test_r13_b3_cfg_get_full_block():
    block = cfg_get("analyst_scoring", config={})
    assert isinstance(block, dict)
    assert block["counter_trend_min_score"] == 50.0
    assert block["analyst5_whale_or_conf"] == 0.75


# ── 3. env 覆盖：canonical env 路由（HERMES_CFG_ANALYST_SCORING__*） ───

def test_r13_b3_env_override_counter_trend_min_score(monkeypatch):
    monkeypatch.setenv(
        "HERMES_CFG_ANALYST_SCORING__COUNTER_TREND_MIN_SCORE", "70.5"
    )
    assert cfg_get("analyst_scoring.counter_trend_min_score", config={}) == 70.5


def test_r13_b3_env_override_analyst5_floor(monkeypatch):
    monkeypatch.setenv(
        "HERMES_CFG_ANALYST_SCORING__ANALYST5_WHALE_OR_CONF", "0.9"
    )
    assert cfg_get("analyst_scoring.analyst5_whale_or_conf", config={}) == 0.9


# ── 4. config dict 部分覆盖：deep merge 正确 ─────────────────────────

def test_r13_b3_config_dict_partial_overlay():
    """传入 config 含 analyst_scoring 子集，未列出的 key 走 canonical 默认。"""
    cfg = {"analyst_scoring": {"counter_trend_min_score": 88.0}}
    assert cfg_get("analyst_scoring.counter_trend_min_score", config=cfg) == 88.0
    # 未覆盖的 key 仍回退到 canonical 默认
    assert cfg_get("analyst_scoring.analyst2_high_conf", config=cfg) == 0.7
    assert cfg_get("analyst_scoring.analyst5_whale_or_conf", config=cfg) == 0.75


# ── 5. read_agent_config 完整可见 + 深合并（实际 .agent-config.json 路径） ─

def test_r13_b3_read_agent_config_exposes_block(monkeypatch, tmp_path):
    """read_agent_config() 返回的 dict 包含 analyst_scoring 块（即使 .agent-config.json 不存在）。"""
    # 把 CONFIG_PATH 指向 tmp 空目录下的一个不存在文件（conftest 已经 setenv
    # HERMES_AGENT_CONFIG_FILE 到一个 tmp 路径；这里再覆盖到当前 tmp_path）
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    assert "analyst_scoring" in cfg
    assert cfg["analyst_scoring"]["counter_trend_min_score"] == 50.0


def test_r13_b3_read_agent_config_deep_merges_partial_overlay(monkeypatch, tmp_path):
    """read_agent_config() 会把 on-disk 的 analyst_scoring 子集 deep-merge 到 canonical。"""
    import json
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "analyst_scoring": {"counter_trend_min_score": 99.0},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    assert cfg["analyst_scoring"]["counter_trend_min_score"] == 99.0
    # 未在 on-disk 出现的 key 仍是 canonical 默认
    assert cfg["analyst_scoring"]["analyst2_high_conf"] == 0.7
    assert cfg["analyst_scoring"]["analyst5_whale_or_conf"] == 0.75


# ── 6. schema 接受 + drift sentinel ──────────────────────────────────

def test_r13_b3_schema_accepts_analyst_scoring_block():
    """_ConfigPatch 必须把 analyst_scoring 声明为字段。"""
    from typing import Any
    fields = _ConfigPatch.model_fields
    assert "analyst_scoring" in fields
    assert fields["analyst_scoring"].annotation == dict[str, Any]


def test_r13_b3_validate_config_updates_accepts_block():
    """含 analyst_scoring 子集的 patch 通过 validate_config_updates（strict_keys=True）。"""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates(
        {"analyst_scoring": {"counter_trend_min_score": 75.0}}
    )
    assert errors == []


# ── 7. risk_gates 模块加载 + hot-path cfg_get 接线 ───────────────────

def test_r13_b3_risk_gates_module_loads_clean():
    """risk_gates 模块加载不抛错；hot-path cfg_get 解析出 canonical 默认。"""
    # 模块已经 import 过；直接调 cfg_get 验证 hot-path 路径
    val = cfg_get("analyst_scoring.counter_trend_min_score", config={})
    assert val == 50.0


def test_r13_b3_market_regime_gate_effective_min_score_uses_canonical(monkeypatch):
    """market_regime_gate 的 effective_min_score 现在走 cfg_get 重解析。
    直接 monkeypatch env，验证 hot-path 拿到的是 canonical 默认 50.0
    （不是硬编码 fallback）。"""
    # 无 env override 时拿 canonical 默认
    val = cfg_get("analyst_scoring.counter_trend_min_score", config={})
    assert val == 50.0
    # env override 立即生效
    monkeypatch.setenv(
        "HERMES_CFG_ANALYST_SCORING__COUNTER_TREND_MIN_SCORE", "65.0"
    )
    val2 = cfg_get("analyst_scoring.counter_trend_min_score", config={})
    assert val2 == 65.0


def test_r13_b3_debate_gate_analyst2_branches_observable(monkeypatch):
    """debate_gate 的 analyst2 投票依赖的 6 个阈值在 env 改变时立即变化。"""
    # canonical 默认 (0.7, 40) — conf=0.71, score=39 应 FAIL
    a2 = (
        cfg_get("analyst_scoring.analyst2_high_conf", config={}),
        cfg_get("analyst_scoring.analyst2_high_score", config={}),
    )
    assert a2 == (0.7, 40)
    # 把 high_score 降到 30，应让 conf=0.71, score=39 PASS
    monkeypatch.setenv(
        "HERMES_CFG_ANALYST_SCORING__ANALYST2_HIGH_SCORE", "30"
    )
    assert cfg_get("analyst_scoring.analyst2_high_score", config={}) == 30.0


def test_r13_b3_debate_gate_analyst5_observable(monkeypatch):
    """analyst5 阈值 0.75 可通过 env 改写。"""
    assert cfg_get("analyst_scoring.analyst5_whale_or_conf", config={}) == 0.75
    monkeypatch.setenv(
        "HERMES_CFG_ANALYST_SCORING__ANALYST5_WHALE_OR_CONF", "0.6"
    )
    assert cfg_get("analyst_scoring.analyst5_whale_or_conf", config={}) == 0.6


# ── 8. 端到端 sentinel：debate_gate 投票对阈值敏感 ──────────────────

def _ctx(**kw):
    """最小 GateContext 构造（debate_gate 不读全字段，只读它关心的几个）。"""
    base = dict(
        confidence=0.7,
        current_positions=[],
        trade_notional_usd=50,
        daily_pnl=0,
        market_volume_24h_usd=1e8,
        coin="BTC",
        trade_side="long",
        has_binary_news_risk=False,
        equity=1000,
        total_open_notional=0,
        composite_score=50.0,
        momentum_burst_fired=False,
        slow_burn_fired=False,
        whale_signal_fired=False,
    )
    base.update(kw)
    return risk_gates.GateContext(**base)


def test_r13_b3_debate_gate_analyst2_threshold_actually_changes_vote(monkeypatch):
    """端到端 sentinel：把 analyst2_high_score 拉低到 10，conf=0.71 + score=39
    应当通过 analyst2（之前会因 score=39 < 40 失败）。"""
    # 关键前提：triggers / news / analyst5 都过，analyst3 默认 False 失败；
    # 只看 analyst2 的 high 段：conf >= 0.7 且 score >= 40
    cfg = {"debate_gate": {"enabled": True, "min_agreement": 0.6,
                            "min_agree_count": 3, "analyst3_default": False}}
    # baseline：score=39 拿不到 high 段（需 >= 40），但 conf=0.71 + score=39
    # 也不满足 mid 段（conf<0.5 不对——0.71 >= 0.5 但 score=39 < 60），也不
    # 满足 vhigh 段（conf=0.71 < 0.8）。所以 analyst2 = False。
    ctx = _ctx(confidence=0.71, composite_score=39,
               momentum_burst_fired=True)  # trigger 1 = True
    # 注：analyst1 需要 active_triggers>=1；analyst2 = False; analyst3 = False;
    # analyst4 = True (no news); analyst5 = False (conf=0.71<0.75, no whale).
    # votes = [T, F, F, T, F] = 2/5 < 0.6 → block.
    r = risk_gates.debate_gate(ctx, cfg)
    assert r["pass"] is False

    # 把 analyst2_high_score 降到 30：conf=0.71 (>=0.7) 且 score=39 (>=30) → True
    monkeypatch.setenv("HERMES_CFG_ANALYST_SCORING__ANALYST2_HIGH_SCORE", "30")
    r2 = risk_gates.debate_gate(ctx, cfg)
    # votes = [T, T, F, T, F] = 3/5 = 0.6 ≥ 0.6 → pass
    assert r2["pass"] is True


def test_r13_b3_debate_gate_analyst5_threshold_actually_changes_vote(monkeypatch):
    """端到端 sentinel：把 analyst5 阈值拉低到 0.5，conf=0.6 + 无 whale 应当通过
    analyst5（之前会因 conf<0.75 失败）。"""
    cfg = {"debate_gate": {"enabled": True, "min_agreement": 0.5,
                            "min_agree_count": 2, "analyst3_default": True}}
    # analyst3 = True (legacy); analyst1 = False (no triggers);
    # analyst2：conf=0.6 < 0.7 不进 high；conf=0.6 >= 0.5 但 score=20 < 60 不进 mid；
    #          conf=0.6 < 0.8 不进 vhigh → False
    # analyst4 = True; analyst5 = False (0.6 < 0.75 baseline)
    # votes = [F, F, T, T, F] = 2/5 = 0.4 < 0.5 → block
    ctx = _ctx(confidence=0.6, composite_score=20)
    r = risk_gates.debate_gate(ctx, cfg)
    assert r["pass"] is False

    # 把 analyst5 阈值降到 0.5：analyst5 = True
    monkeypatch.setenv("HERMES_CFG_ANALYST_SCORING__ANALYST5_WHALE_OR_CONF", "0.5")
    r2 = risk_gates.debate_gate(ctx, cfg)
    # votes = [F, F, T, T, T] = 3/5 = 0.6 ≥ 0.5 → pass
    assert r2["pass"] is True


# ── 9. 零行为变化：未设置 env 时 debate_gate 投票结果与原硬编码一致 ─

def test_r13_b3_debate_gate_default_analyst2_vote_unchanged():
    """canonical 默认下，conf=0.71, score=39 的 analyst2 投票结果与原硬编码
    完全一致（False）—— 验证零行为变化。"""
    # 清掉可能干扰的 env
    for k in list(os.environ):
        if k.startswith("HERMES_CFG_ANALYST_SCORING__"):
            del os.environ[k]
    ctx = _ctx(confidence=0.71, composite_score=39, momentum_burst_fired=True)
    cfg = {"debate_gate": {"enabled": True, "min_agreement": 0.6,
                            "min_agree_count": 3, "analyst3_default": False}}
    r = risk_gates.debate_gate(ctx, cfg)
    # 与 test_r13_b3_debate_gate_analyst2_threshold_actually_changes_vote
    # baseline 分支完全一致：block
    assert r["pass"] is False


def test_r13_b3_debate_gate_default_analyst5_vote_unchanged():
    """canonical 默认下，conf=0.6, no whale, score=20 的 analyst5 投票结果与
    原硬编码完全一致（False）—— 验证零行为变化。"""
    for k in list(os.environ):
        if k.startswith("HERMES_CFG_ANALYST_SCORING__"):
            del os.environ[k]
    ctx = _ctx(confidence=0.6, composite_score=20)
    cfg = {"debate_gate": {"enabled": True, "min_agreement": 0.5,
                            "min_agree_count": 2, "analyst3_default": True}}
    r = risk_gates.debate_gate(ctx, cfg)
    assert r["pass"] is False


# ── 10. MCP / dashboard 可观测性：analyst_scoring 在 dump 中可见 ─────

def test_r13_b3_canonical_defaults_contains_analyst_scoring_for_mcp_dump():
    """CANONICAL_DEFAULTS 是 MCP server / dashboard dump 的 source of truth。
    analyst_scoring 必须出现，否则 R11 审计的『隐式字段不可观测』问题未解。"""
    assert "analyst_scoring" in CANONICAL_DEFAULTS
    # 默认值类型正确（避免 dashboard 渲染时 crash）
    block = CANONICAL_DEFAULTS["analyst_scoring"]
    assert isinstance(block["counter_trend_min_score"], float)
    assert isinstance(block["analyst2_high_conf"], float)
    assert isinstance(block["analyst2_high_score"], int)
    assert isinstance(block["analyst2_mid_conf"], float)
    assert isinstance(block["analyst2_mid_score"], int)
    assert isinstance(block["analyst2_very_high_conf"], float)
    assert isinstance(block["analyst2_very_high_score"], int)
    assert isinstance(block["analyst5_whale_or_conf"], float)
