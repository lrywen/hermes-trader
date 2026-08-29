"""R12-C1: 隐式配置字段登记测试。

修复前，下列字段被生产代码实际依赖（cfg_get / read_agent_config().get()
读取），但既不在 CANONICAL_DEFAULTS 也不在 .agent-config.json 中——运维
无法通过配置调整、env 覆盖或 dashboard dump 审计，阈值散落在各调用点的
硬编码 default 里。R12-C1 把它们以"默认值 = 原硬编码值"登记进
CANONICAL_DEFAULTS，行为零变化，但变为可配置 / 可 env 覆盖 / 可审计。

覆盖字段：
  * circuit_breaker.{single_coin_loss_pct, single_coin_halt_min,
    daily_loss_pct, daily_halt_min}（executor 分层熔断器，cfg_get 读取）
  * sl_ceiling_pct / sl_floor_pct（executor 备份止损 clamp）
  * tp_atr_mult（server 手动下单 bracket 止盈）
  * conviction_tiers（executor legacy conviction sizing 阶梯）
  * atr_risk_sizing.coin_overrides（per-coin SL floor 覆盖）
  * dsl_exit.noise_band.{enabled,atr_mult} / consecutive_breaches_required
    / breach_confirm_sec（DSL 退出策略）
  * runner_entry_gate.pullback_long.*（回调做多旁路闸，整块）
  * debate_gate.analyst3_default（共识闸第三分析师默认）
  * aligned_min_conf（顺势降置信门槛，None=关闭）
"""

import pytest

from hermes_trader.agents import config_store
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
    write_agent_config,
)


# ── canonical 登记：默认值必须严格等于原调用点硬编码值 ──────────────────────

def test_r12_c1_circuit_breaker_registered_with_hardcoded_defaults():
    """分层熔断器 4 键：executor.py cfg_get(..., default=) 的原值。"""
    cb = CANONICAL_DEFAULTS["circuit_breaker"]
    assert cb["single_coin_loss_pct"] == 3.0
    assert cb["single_coin_halt_min"] == 60.0
    assert cb["daily_loss_pct"] == 5.0
    assert cb["daily_halt_min"] == 120.0


def test_r12_c1_sl_and_tp_scalars_registered():
    """sl_ceiling_pct=3.0 / sl_floor_pct=1.2（executor 模块常量）、
    tp_atr_mult=1.0（server.py 手动 bracket 默认）。"""
    assert CANONICAL_DEFAULTS["sl_ceiling_pct"] == 3.0
    assert CANONICAL_DEFAULTS["sl_floor_pct"] == 1.2
    assert CANONICAL_DEFAULTS["tp_atr_mult"] == 1.0


def test_r12_c1_conviction_tiers_registered():
    """conviction_tiers 默认 = executor._DEFAULT_CONVICTION_TIERS。"""
    from hermes_trader.agents.executor import _DEFAULT_CONVICTION_TIERS
    tiers = CANONICAL_DEFAULTS["conviction_tiers"]
    assert [tuple(t) for t in tiers] == list(_DEFAULT_CONVICTION_TIERS)


def test_r12_c1_atr_risk_sizing_coin_overrides_registered():
    assert CANONICAL_DEFAULTS["atr_risk_sizing"]["coin_overrides"] == {}


def test_r12_c1_dsl_exit_fields_registered():
    dsl = CANONICAL_DEFAULTS["dsl_exit"]
    assert dsl["noise_band"] == {"enabled": False, "atr_mult": 1.0}
    assert dsl["consecutive_breaches_required"] == 1
    # A-F5 (deep audit 2026-08-28): DSL floor breach needs a 3-5s time gate;
    # default is 4.0s (0.0 previously meant the gate was disabled).
    assert dsl["breach_confirm_sec"] == 4.0


def test_r12_c1_pullback_long_block_registered():
    pb = CANONICAL_DEFAULTS["runner_entry_gate"]["pullback_long"]
    assert pb == {
        "enabled": False,
        "min_composite": 20.0,
        "max_rsi": 70.0,
        "max_extension_atr": 2.0,
        "min_slow_burn": 1,
        "shadow_mode": False,
    }


def test_r12_c1_debate_gate_analyst3_default_registered():
    assert CANONICAL_DEFAULTS["debate_gate"]["analyst3_default"] is False


def test_r12_c1_aligned_min_conf_registered_as_none():
    """None = 功能关闭（risk_gates 对 None 有显式 is-not-None 守卫）。"""
    assert CANONICAL_DEFAULTS["aligned_min_conf"] is None


# ── cfg_get 解析：空 config 时返回 canonical 默认 ───────────────────────────

@pytest.mark.parametrize("dotted_key,expected", [
    ("circuit_breaker.single_coin_loss_pct", 3.0),
    ("circuit_breaker.single_coin_halt_min", 60.0),
    ("circuit_breaker.daily_loss_pct", 5.0),
    ("circuit_breaker.daily_halt_min", 120.0),
    ("sl_ceiling_pct", 3.0),
    ("sl_floor_pct", 1.2),
    ("tp_atr_mult", 1.0),
    ("atr_risk_sizing.coin_overrides", {}),
    ("dsl_exit.noise_band.enabled", False),
    ("dsl_exit.noise_band.atr_mult", 1.0),
    ("dsl_exit.consecutive_breaches_required", 1),
    ("dsl_exit.breach_confirm_sec", 4.0),
    ("runner_entry_gate.pullback_long.enabled", False),
    ("runner_entry_gate.pullback_long.min_composite", 20.0),
    ("runner_entry_gate.pullback_long.max_rsi", 70.0),
    ("runner_entry_gate.pullback_long.max_extension_atr", 2.0),
    ("runner_entry_gate.pullback_long.min_slow_burn", 1),
    ("runner_entry_gate.pullback_long.shadow_mode", False),
    ("debate_gate.analyst3_default", False),
])
def test_r12_c1_cfg_get_resolves_canonical_default(dotted_key, expected):
    assert cfg_get(dotted_key, config={}) == expected


def test_r12_c1_cfg_get_conviction_tiers_shape():
    tiers = cfg_get("conviction_tiers", config={})
    assert tiers == [[0.80, 1.5], [0.65, 1.0], [0.0, 0.7]]
    # executor._parse_conviction_tiers 接受 list-of-lists（t[0]/t[1] 索引）
    from hermes_trader.agents.executor import _parse_conviction_tiers
    parsed = _parse_conviction_tiers(tiers)
    assert parsed[0] == (0.80, 1.5)
    assert parsed[-1] == (0.0, 0.7)


def test_r12_c1_cfg_get_aligned_min_conf_none_disables_feature():
    # risk_gates 用 `config.get("aligned_min_conf")` + `is not None` 守卫
    assert cfg_get("aligned_min_conf", config={}) is None


# ── env 覆盖：新登记键支持 HERMES_CFG_ 覆盖（含嵌套双下划线）────────────────

def test_r12_c1_env_override_circuit_breaker(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_CIRCUIT_BREAKER__DAILY_LOSS_PCT", "8.0")
    assert cfg_get("circuit_breaker.daily_loss_pct", config={}) == 8.0


def test_r12_c1_env_override_sl_floor(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_SL_FLOOR_PCT", "1.5")
    assert cfg_get("sl_floor_pct", config={}) == 1.5


def test_r12_c1_env_override_pullback_long_enabled(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_RUNNER_ENTRY_GATE__PULLBACK_LONG__ENABLED", "true")
    assert cfg_get("runner_entry_gate.pullback_long.enabled", config={}) is True


def test_r12_c1_env_override_analyst3_default(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_DEBATE_GATE__ANALYST3_DEFAULT", "true")
    assert cfg_get("debate_gate.analyst3_default", config={}) is True


# ── config dict 覆盖：运维显式配置优先于 canonical ──────────────────────────

def test_r12_c1_config_dict_overrides_circuit_breaker():
    cfg = {"circuit_breaker": {"single_coin_loss_pct": 2.0, "daily_halt_min": 30.0}}
    assert cfg_get("circuit_breaker.single_coin_loss_pct", config=cfg) == 2.0
    assert cfg_get("circuit_breaker.single_coin_halt_min", config=cfg) == 60.0
    assert cfg_get("circuit_breaker.daily_loss_pct", config=cfg) == 5.0
    assert cfg_get("circuit_breaker.daily_halt_min", config=cfg) == 30.0


def test_r12_c1_config_dict_overrides_pullback_long_partial():
    """裸 config dict（未经 read_agent_config 深合并）只覆盖显式给出的子键；
    未覆盖子键的 canonical 回填由 read_agent_config 深合并保证（见下条）。"""
    cfg = {"runner_entry_gate": {"pullback_long": {"enabled": True, "max_rsi": 65.0}}}
    pb = cfg_get("runner_entry_gate.pullback_long", config=cfg)
    assert pb["enabled"] is True
    assert pb["max_rsi"] == 65.0


def test_r12_c1_read_agent_config_exposes_all_new_fields():
    """conftest 把 CONFIG_PATH 指向不存在的临时文件 → 纯 canonical 视图。
    新登记键必须全部出现在 merged config（dashboard dump / 审计所见）。"""
    cfg = read_agent_config()
    assert cfg["circuit_breaker"]["daily_loss_pct"] == 5.0
    assert cfg["sl_ceiling_pct"] == 3.0
    assert cfg["sl_floor_pct"] == 1.2
    assert cfg["tp_atr_mult"] == 1.0
    assert cfg["conviction_tiers"] == [[0.80, 1.5], [0.65, 1.0], [0.0, 0.7]]
    assert cfg["atr_risk_sizing"]["coin_overrides"] == {}
    assert cfg["dsl_exit"]["noise_band"]["enabled"] is False
    assert cfg["dsl_exit"]["consecutive_breaches_required"] == 1
    assert cfg["dsl_exit"]["breach_confirm_sec"] == 4.0
    assert cfg["runner_entry_gate"]["pullback_long"]["shadow_mode"] is False
    assert cfg["debate_gate"]["analyst3_default"] is False
    assert "aligned_min_conf" in cfg


def test_r12_c1_read_agent_config_deep_merges_partial_overlay(tmp_path, monkeypatch):
    """磁盘配置只写部分新键时，深合并保留其余 canonical 默认。"""
    import json
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "circuit_breaker": {"daily_loss_pct": 7.5},
        "runner_entry_gate": {"pullback_long": {"enabled": True}},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg["circuit_breaker"]["daily_loss_pct"] == 7.5
    assert cfg["circuit_breaker"]["single_coin_loss_pct"] == 3.0  # canonical 保留
    assert cfg["runner_entry_gate"]["pullback_long"]["enabled"] is True
    assert cfg["runner_entry_gate"]["pullback_long"]["max_rsi"] == 70.0
    # 既有键不受影响
    assert cfg["runner_entry_gate"]["allow_shorts"] is False


def test_r12_c1_none_default_key_survives_full_view_round_trip(tmp_path, monkeypatch):
    """写路径把全量 merged 视图落盘（含 aligned_min_conf: null）后再读，
    null 虽被 _deep_merge 当作删除标记，但回填守卫保证该键仍可见
    （行为零变化：值依旧是 None，审计/dump 可见性不丢失）。"""
    import json
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(cfg_file) + ".bak")

    full_view = read_agent_config()          # 纯 canonical 视图，含 None 键
    assert full_view["aligned_min_conf"] is None
    write_agent_config(dict(full_view))       # 模拟 dashboard 全量落盘
    assert json.loads(cfg_file.read_text())["aligned_min_conf"] is None

    reloaded = read_agent_config()
    assert "aligned_min_conf" in reloaded     # 回填守卫生效
    assert reloaded["aligned_min_conf"] is None
    assert reloaded["sl_floor_pct"] == 1.2   # 非 None 键不受影响


# ── schema 兼容：新键不被 validate_config_updates 拒绝 ──────────────────────

def test_r12_c1_new_top_level_keys_are_known_to_schema():
    """strict_keys 模式下，新登记的顶层键不再是 unknown key。"""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "sl_ceiling_pct": 2.5,
        "sl_floor_pct": 1.0,
        "tp_atr_mult": 1.5,
        "aligned_min_conf": None,
        "conviction_tiers": [[0.9, 2.0]],
        "circuit_breaker": {"daily_loss_pct": 6.0},
    }, strict_keys=True)
    assert not any("unknown key" in e for e in errors), errors


def test_r12_c1_nested_blocks_accepted_as_objects():
    """嵌套块作为 object 整体接受（schema 不对嵌套 dict 深校验）。"""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "dsl_exit": {"noise_band": {"enabled": True, "atr_mult": 0.8}},
        "runner_entry_gate": {"pullback_long": {"enabled": True}},
        "debate_gate": {"analyst3_default": True},
        "atr_risk_sizing": {"coin_overrides": {"HYPE": {"sl_floor_pct": 1.5}}},
    }, strict_keys=True)
    assert errors == [], errors
