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
        "require_macro_uptrend": True,
    }


def test_r12_c1_debate_gate_analyst3_default_registered():
    assert CANONICAL_DEFAULTS["debate_gate"]["analyst3_default"] is False


def test_r12_c1_aligned_min_conf_registered_enabled():
    """Audit 2026-09-04 P0-5: 原 None 默认使顺势放宽静默关闭；现默认 0.60
    （低于 min_ai_confidence=0.62），顺势入场获得真实更低门槛。显式设 null
    仍可关闭（risk_gates 对 None 有 is-not-None 守卫）。"""
    assert CANONICAL_DEFAULTS["aligned_min_conf"] == 0.60


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
    ("runner_entry_gate.pullback_long.require_macro_uptrend", True),
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
    # P0-5 (audit 2026-09-04): production had aligned_min_conf=null which
    # silently disabled the with-trend confidence relaxation with no warning.
    # Canonical now pins 0.60 (below min_ai_confidence=0.62, LONG-side
    # relaxation live by default). An explicit null in an operator config
    # still disables the feature: risk_gates guards with
    # `config.get("aligned_min_conf")` + `is not None`, and cfg_get returns a
    # present-but-null config value rather than falling through to canonical.
    assert cfg_get("aligned_min_conf", config={}) == 0.60
    assert cfg_get("aligned_min_conf", config={"aligned_min_conf": None}) is None


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
    """全量 merged 视图落盘 → 重读 round-trip。

    P0-5 后 aligned_min_conf 的 canonical 值为 0.60（不再是 None），正常
    round-trip 保持 0.60；显式 null 是 _deep_merge 的删除标记，落盘的
    null 重读后从 merged 视图中消失（键不存在 → risk_gates
    `config.get(...) is None` 判定特性关闭，这是有意的 opt-out 路径）；
    read_agent_config 末尾的回填守卫仅重生物料 canonical 默认就是 None
    的键（当前无此键，守卫面向未来保留），非 None 键不受影响。"""
    import json
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    monkeypatch.setattr(config_store, "_BACKUP_PATH", str(cfg_file) + ".bak")

    full_view = read_agent_config()          # 纯 canonical 视图
    assert full_view["aligned_min_conf"] == 0.60
    write_agent_config(dict(full_view))       # 模拟 dashboard 全量落盘
    assert json.loads(cfg_file.read_text())["aligned_min_conf"] == 0.60

    reloaded = read_agent_config()
    assert reloaded["aligned_min_conf"] == 0.60  # 数值键 round-trip 不变
    assert reloaded["sl_floor_pct"] == 1.2       # 非 None 键不受影响

    # 显式 null opt-out：落盘 null 经 _deep_merge 当删除标记，重读后键消失。
    view = dict(full_view)
    view["aligned_min_conf"] = None
    write_agent_config(view)
    opt_out = read_agent_config()
    assert "aligned_min_conf" not in opt_out     # 删除标记生效 → 特性关闭
    assert opt_out["sl_floor_pct"] == 1.2        # 其余键完好

    # 回填守卫：canonical 默认 None 的键在 deep-merge 删除后仍被重生物料
    # （当前无生产 None 键，用合成键锁定该防御行为）。
    monkeypatch.setitem(config_store.CANONICAL_DEFAULTS, "_r12c1_sentinel_none", None)
    try:
        backfilled = read_agent_config()
        assert backfilled["_r12c1_sentinel_none"] is None
    finally:
        config_store.CANONICAL_DEFAULTS.pop("_r12c1_sentinel_none", None)


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


# ── roadmap §1/§2 (2026-09-04, #12): gray-release decay / regime blocks ─────
# confidence_decay / signal_age_decay / atr_regime_calibration shipped as
# env-gated shadow features with hardcoded defaults at the call sites. #12
# registers the three blocks in CANONICAL_DEFAULTS + config_schema so they are
# tunable via config/env, visible in dashboard dumps, and covered by the R7
# drift sentinel. Defaults MUST equal the module constants byte-for-byte and
# mode defaults to "off" (production flips to shadow via HERMES_*_MODE env).

def test_r12_decay_blocks_registered_with_hardcoded_defaults():
    """Canonical defaults equal the executor/perception/sizing literals."""
    from hermes_trader.agents.executor import (
        _ATR_CALIB_MODES,
        _CONFIDENCE_DECAY_DEFAULT_HALFLIFE_S,
        _CONFIDENCE_DECAY_MODES,
    )
    from hermes_trader.agents.perception import (
        _AGE_DECAY_DEFAULT_HALFLIFE_S,
        _AGE_DECAY_MODES,
    )
    from hermes_trader.agents.sizing import ATR_REGIME_DEFAULTS

    cd = CANONICAL_DEFAULTS["confidence_decay"]
    assert cd["mode"] == "off"
    assert cd["halflife_s"] == _CONFIDENCE_DECAY_DEFAULT_HALFLIFE_S == 900.0
    assert cd["shadow_log_path"] == ""
    assert set(_CONFIDENCE_DECAY_MODES) == {"off", "shadow", "enforce"}

    sd = CANONICAL_DEFAULTS["signal_age_decay"]
    assert sd["mode"] == "off"
    assert sd["halflife_s"] == dict(_AGE_DECAY_DEFAULT_HALFLIFE_S)
    assert sd["halflife_s"]["breakout"] == 900.0
    assert sd["halflife_s"]["momentumBurst"] == 0.0
    # onset_ttl_s mirrors the perception.py call-site literal (6h).
    assert sd["onset_ttl_s"] == 21600.0
    assert sd["shadow_log_path"] == ""
    assert set(_AGE_DECAY_MODES) == {"off", "shadow", "enforce"}

    ac = CANONICAL_DEFAULTS["atr_regime_calibration"]
    assert ac["mode"] == "off"
    for k, v in ATR_REGIME_DEFAULTS.items():
        assert ac[k] == v
    assert ac == {
        "mode": "off",
        "low_ratio": 0.6,
        "high_ratio": 1.6,
        "low_mult": 0.85,
        "high_mult": 1.20,
        "min_mult": 0.75,
        "max_mult": 1.35,
        "shadow_log_path": "",
    }
    assert set(_ATR_CALIB_MODES) == {"off", "shadow", "enforce"}


@pytest.mark.parametrize("dotted_key,expected", [
    ("confidence_decay.mode", "off"),
    ("confidence_decay.halflife_s", 900.0),
    ("confidence_decay.shadow_log_path", ""),
    ("signal_age_decay.mode", "off"),
    ("signal_age_decay.halflife_s.breakout", 900.0),
    ("signal_age_decay.halflife_s.trendStrength", 1800.0),
    ("signal_age_decay.halflife_s.trendFlip1h", 7200.0),
    ("signal_age_decay.halflife_s.pctMoveSpike", 0.0),
    ("signal_age_decay.onset_ttl_s", 21600.0),
    ("signal_age_decay.shadow_log_path", ""),
    ("atr_regime_calibration.mode", "off"),
    ("atr_regime_calibration.low_ratio", 0.6),
    ("atr_regime_calibration.high_ratio", 1.6),
    ("atr_regime_calibration.low_mult", 0.85),
    ("atr_regime_calibration.high_mult", 1.20),
    ("atr_regime_calibration.min_mult", 0.75),
    ("atr_regime_calibration.max_mult", 1.35),
    ("atr_regime_calibration.shadow_log_path", ""),
])
def test_r12_cfg_get_resolves_decay_block_defaults(dotted_key, expected):
    assert cfg_get(dotted_key, config={}) == expected


def test_r12_decay_env_override_mode_and_scalar_leaves(monkeypatch):
    """HERMES_CFG_<BLOCK>__<LEAF> overrides land (double-underscore nesting)."""
    monkeypatch.setenv("HERMES_CFG_CONFIDENCE_DECAY__MODE", "shadow")
    monkeypatch.setenv("HERMES_CFG_CONFIDENCE_DECAY__HALFLIFE_S", "1200")
    monkeypatch.setenv("HERMES_CFG_SIGNAL_AGE_DECAY__ONSET_TTL_S", "3600")
    monkeypatch.setenv(
        "HERMES_CFG_SIGNAL_AGE_DECAY__HALFLIFE_S__BREAKOUT", "600")
    monkeypatch.setenv("HERMES_CFG_ATR_REGIME_CALIBRATION__LOW_RATIO", "0.75")
    assert cfg_get("confidence_decay.mode", config={}) == "shadow"
    assert cfg_get("confidence_decay.halflife_s", config={}) == 1200.0
    assert cfg_get("signal_age_decay.onset_ttl_s", config={}) == 3600.0
    assert cfg_get(
        "signal_age_decay.halflife_s.breakout", config={}) == 600.0
    assert cfg_get("atr_regime_calibration.low_ratio", config={}) == 0.75


def test_r12_decay_config_dict_override_partial():
    """Operator overlay wins; untouched leaves stay canonical."""
    cfg = {
        "confidence_decay": {"mode": "enforce", "halflife_s": 300.0},
        "atr_regime_calibration": {"high_mult": 1.5},
    }
    assert cfg_get("confidence_decay.mode", config=cfg) == "enforce"
    assert cfg_get("confidence_decay.halflife_s", config=cfg) == 300.0
    assert cfg_get("confidence_decay.shadow_log_path", config=cfg) == ""
    assert cfg_get("atr_regime_calibration.high_mult", config=cfg) == 1.5
    assert cfg_get("atr_regime_calibration.low_ratio", config=cfg) == 0.6


def test_r12_decay_blocks_exposed_in_full_config_view():
    """read_agent_config() pure-canonical view exposes all three blocks."""
    cfg = read_agent_config()
    assert cfg["confidence_decay"]["mode"] == "off"
    assert cfg["signal_age_decay"]["halflife_s"]["momentumBurst"] == 0.0
    assert cfg["signal_age_decay"]["onset_ttl_s"] == 21600.0
    assert cfg["atr_regime_calibration"]["max_mult"] == 1.35


def test_r12_decay_blocks_deep_merge_partial_overlay(tmp_path, monkeypatch):
    """A disk overlay with one leaf keeps the other canonical leaves."""
    import json
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "signal_age_decay": {"halflife_s": {"breakout": 120.0}},
        "atr_regime_calibration": {"mode": "shadow"},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg["signal_age_decay"]["halflife_s"]["breakout"] == 120.0
    # Overridden leaf's siblings survive from canonical.
    assert cfg["signal_age_decay"]["halflife_s"]["trendStrength"] == 1800.0
    assert cfg["signal_age_decay"]["mode"] == "off"
    assert cfg["atr_regime_calibration"]["mode"] == "shadow"
    assert cfg["atr_regime_calibration"]["low_ratio"] == 0.6


def test_r12_decay_blocks_known_to_schema_strict_keys():
    """strict_keys=True accepts the blocks and validates nested leaves."""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "confidence_decay": {"mode": "shadow", "halflife_s": 600.0},
        "signal_age_decay": {
            "mode": "enforce",
            "halflife_s": {"breakout": 300.0, "momentumBurst": 0.0},
            "onset_ttl_s": 7200.0,
        },
        "atr_regime_calibration": {
            "mode": "shadow", "low_ratio": 0.5, "max_mult": 1.5,
        },
    }, strict_keys=True)
    assert errors == [], errors


def test_r12_decay_schema_rejects_unknown_and_bad_leaves():
    """Nested spec catches unknown leaves and bad enum/type values."""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "confidence_decay": {"mode": "bogus", "bogus_leaf": 1},
        "signal_age_decay": {"halflife_s": {"not_a_trigger": 100.0}},
        "atr_regime_calibration": {"low_ratio": "not-a-number"},
    }, strict_keys=True)
    joined = " | ".join(errors)
    assert "confidence_decay.mode" in joined
    assert "confidence_decay.bogus_leaf: unknown key" in joined
    assert "signal_age_decay.halflife_s.not_a_trigger: unknown key" in joined
    assert "atr_regime_calibration.low_ratio" in joined


def test_r12_decay_runtime_readers_pick_up_config_block():
    """The actual runtime parsers resolve mode/leaves from the config block."""
    from hermes_trader.agents.executor import (
        _atr_calib_config,
        _confidence_decay_config,
    )
    from hermes_trader.agents.perception import _age_decay_config, _age_decay_halflives_ms

    cd = _confidence_decay_config({"confidence_decay": {"mode": "shadow", "halflife_s": 300.0}})
    assert cd["mode"] == "shadow"
    assert cd["halflife_s"] == 300.0

    ac = _atr_calib_config({"atr_regime_calibration": {"mode": "enforce"}})
    assert ac["mode"] == "enforce"
    # An invalid mode in the block safely falls back to off.
    assert _atr_calib_config({"atr_regime_calibration": {"mode": "bogus"}})["mode"] == "off"

    sd = _age_decay_config({"signal_age_decay": {"mode": "shadow"}})
    assert sd["mode"] == "shadow"
    hl = _age_decay_halflives_ms(sd["block"])
    assert hl["breakout"] == 900.0 * 1000.0      # canonical default
    sd2 = _age_decay_config({"signal_age_decay": {"halflife_s": {"breakout": 60.0}}})
    hl2 = _age_decay_halflives_ms(sd2["block"])
    assert hl2["breakout"] == 60.0 * 1000.0      # overlay leaf
