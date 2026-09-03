"""R13-B8: trigger weights / thresholds 配置登记测试 + D6 drift 修复测试。

修复前，``TRIGGER_CONFIG["weights"]``（12 键）与 ``TRIGGER_CONFIG["thresholds"]``
（16 键）是 ``hermes_trader.agents.config`` 模块级 dict：perception.py 经
``config["weights"]`` / ``config["thresholds"]`` 直读，既不走 cfg_get 也不走
read_agent_config，完全没有 env / dashboard / set 通道——运维既无法运行时调参，
也无法在 dashboard dump 中看到实际生效值。

同时存在一处隐性 drift（D6）：``trendMomentumPct`` 的 canonical 字面量是 5.0
（config.py，注释记载 3.0 曾导致 22 triggers/scan、~4.5x AI 成本、longs 泛滥），
但 perception.py L298/300 的 dict.get 兜底与 triggers.py
uptrend_momentum/downtrend_momentum 的签名默认都是 **3.0**。运行时经 get_config()
恒为 5.0（死兜底），但 thresholds 一旦缺键，3.0 会静默复活。

修复后：
  * 两块登记进 CANONICAL_DEFAULTS（``trigger_weights`` /
    ``trigger_thresholds``，leaf 用 snake_case 惯例），默认值与 TRIGGER_CONFIG
    严格一致（零行为变化）；
  * config.py 新增 ``trigger_weights_params`` / ``trigger_thresholds_params``
    helper：逐叶 cfg_get + guard，把 snake_case canonical leaf 映射回消费方
    （composite_score 按 trigger 名索引、_scan_single_market 驼峰访问）所需的
    camelCase 运行时键；
  * perception.scan_once 浅合并后注入两块 helper 结果，env 热路径生效；
  * D6 四处 3.0 死兜底全部对齐 5.0（perception L298/300、triggers L397/419）。

覆盖断言：
  * 两块 canonical 登记 + 默认值逐键 == TRIGGER_CONFIG（经 keymap 映射）
  * 逐叶 sentinel（12 weights / 16 thresholds），含 6 个故意为 0 的权重
  * cfg_get 点路径、env 覆盖（HERMES_CFG_TRIGGER_*__*）、config dict 深合并、
    read_agent_config 可见、schema validate、_ConfigPatch drift sentinel
  * helper：默认/camelCase 键集/env 与 dict 覆盖/guard（负权重、<=0 浮点、<1 int、
    坏字符串）/返回独立副本
  * D6：triggers 签名默认 5.0；~+3.7% 走势在默认（5.0）下不 fire、显式 3.0 才
    fire；perception/triggers 源码不再残留 3.0 死兜底；scan_once 已接线 helper
"""

import inspect
import json

from hermes_trader.agents import config as config_mod
from hermes_trader.agents import config_store
from hermes_trader.agents.config import (
    _TRIGGER_THRESHOLDS_KEYMAP,
    _TRIGGER_WEIGHTS_KEYMAP,
    TRIGGER_CONFIG,
    trigger_thresholds_params,
    trigger_weights_params,
)
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)

# ── canonical 登记：两块存在且为 dict ──────────────────────────────────────

def test_r13_b8_trigger_weights_block_registered():
    """trigger_weights 嵌套块必须在 CANONICAL_DEFAULTS 中。"""
    assert "trigger_weights" in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS["trigger_weights"], dict)
    assert len(CANONICAL_DEFAULTS["trigger_weights"]) == 12


def test_r13_b8_trigger_thresholds_block_registered():
    """trigger_thresholds 嵌套块必须在 CANONICAL_DEFAULTS 中。"""
    assert "trigger_thresholds" in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS["trigger_thresholds"], dict)
    assert len(CANONICAL_DEFAULTS["trigger_thresholds"]) == 16


# ── 默认值严格镜像 TRIGGER_CONFIG（经 snake→camel keymap）──────────────────

def test_r13_b8_trigger_weights_defaults_mirror_trigger_config():
    """canonical weights 逐叶 == TRIGGER_CONFIG['weights'] 对应驼峰键。"""
    block = CANONICAL_DEFAULTS["trigger_weights"]
    for leaf, camel in _TRIGGER_WEIGHTS_KEYMAP.items():
        assert leaf in block, f"missing canonical leaf: {leaf}"
        assert block[leaf] == TRIGGER_CONFIG["weights"][camel], (
            f"trigger_weights.{leaf}={block[leaf]!r} != "
            f"TRIGGER_CONFIG weights.{camel}={TRIGGER_CONFIG['weights'][camel]!r}"
        )


def test_r13_b8_trigger_thresholds_defaults_mirror_trigger_config():
    """canonical thresholds 逐叶 == TRIGGER_CONFIG['thresholds'] 对应驼峰键。"""
    block = CANONICAL_DEFAULTS["trigger_thresholds"]
    for leaf, (camel, _is_int) in _TRIGGER_THRESHOLDS_KEYMAP.items():
        assert leaf in block, f"missing canonical leaf: {leaf}"
        assert block[leaf] == TRIGGER_CONFIG["thresholds"][camel], (
            f"trigger_thresholds.{leaf}={block[leaf]!r} != "
            f"TRIGGER_CONFIG thresholds.{camel}="
            f"{TRIGGER_CONFIG['thresholds'][camel]!r}"
        )


# ── 逐叶 sentinel：锁死每个默认值，防误改 ─────────────────────────────────

def test_r13_b8_trigger_weights_individual_values():
    w = CANONICAL_DEFAULTS["trigger_weights"]
    assert w["trend_strength"] == 0.55
    assert w["pct_move_spike"] == 0.40
    assert w["breakout"] == 0.30
    assert w["volume_spike"] == 0.25
    assert w["momentum_burst"] == 0.20
    assert w["volume_buildup_1h"] == 0.15


def test_r13_b8_six_weights_intentionally_zero():
    """六个权重故意为 0（净负 lift / surfacing-only），canonical 必须保持 0。"""
    w = CANONICAL_DEFAULTS["trigger_weights"]
    for leaf in ("higher_lows_1h", "trend_flip_1h", "range_compression",
                 "uptrend_momentum", "downtrend_momentum", "daily_mover"):
        assert w[leaf] == 0.0, f"{leaf} 应为故意 0 权重，实际 {w[leaf]!r}"


def test_r13_b8_trigger_thresholds_individual_values():
    t = CANONICAL_DEFAULTS["trigger_thresholds"]
    assert t["sigma_threshold"] == 2.0
    assert t["trend_momentum_lookback"] == 72
    assert t["trend_momentum_pct"] == 5.0  # D6 canonical：不是 3.0
    assert t["breakout_lookback"] == 48
    assert t["breakout_min_rvol"] == 1.5
    assert t["breakout_rvol_window"] == 20
    assert t["breakout_atr_score_mult"] == 3.0
    assert t["breakout_confirm_bars"] == 2
    assert t["bb_length"] == 20
    assert t["bb_std_dev"] == 2
    assert t["adx_period"] == 14
    assert t["momentum_lookback"] == 2
    assert t["momentum_pct"] == 4.0
    assert t["vol_buildup_ratio"] == 2.5
    assert t["trend_flip_bars"] == 3
    assert t["higher_lows_required"] == 4


def test_r13_b8_trigger_config_itself_unchanged():
    """TRIGGER_CONFIG 字面量保持原样（回测脚本直读的外部符号）。"""
    assert TRIGGER_CONFIG["thresholds"]["trendMomentumPct"] == 5.0
    assert TRIGGER_CONFIG["weights"]["trendStrength"] == 0.55
    assert len(TRIGGER_CONFIG["weights"]) == 12
    assert len(TRIGGER_CONFIG["thresholds"]) == 16


# ── cfg_get 点路径：空 config 回退 canonical，覆盖全部 28 叶 ───────────────

def test_r13_b8_cfg_get_all_weights_leaves():
    for leaf, camel in _TRIGGER_WEIGHTS_KEYMAP.items():
        assert cfg_get(f"trigger_weights.{leaf}", config={}) == \
            TRIGGER_CONFIG["weights"][camel]


def test_r13_b8_cfg_get_all_thresholds_leaves():
    for leaf, (camel, _is_int) in _TRIGGER_THRESHOLDS_KEYMAP.items():
        assert cfg_get(f"trigger_thresholds.{leaf}", config={}) == \
            TRIGGER_CONFIG["thresholds"][camel]


def test_r13_b8_cfg_get_full_blocks():
    w = cfg_get("trigger_weights", config={})
    t = cfg_get("trigger_thresholds", config={})
    assert isinstance(w, dict) and len(w) == 12
    assert isinstance(t, dict) and len(t) == 16
    assert t["trend_momentum_pct"] == 5.0


# ── env 覆盖：HERMES_CFG_<BLOCK>__<LEAF>，下划线保留 ──────────────────────

def test_r13_b8_env_override_trend_momentum_pct(monkeypatch):
    # D6 键的 canonical env 路由：snake leaf 直接 upper，下划线不插/不删
    monkeypatch.setenv("HERMES_CFG_TRIGGER_THRESHOLDS__TREND_MOMENTUM_PCT", "7.5")
    assert cfg_get("trigger_thresholds.trend_momentum_pct", config={}) == 7.5


def test_r13_b8_env_override_weight_trend_strength(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_TRIGGER_WEIGHTS__TREND_STRENGTH", "0.99")
    assert cfg_get("trigger_weights.trend_strength", config={}) == 0.99


def test_r13_b8_env_override_int_leaf_coerces_to_int(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_TRIGGER_THRESHOLDS__BB_LENGTH", "25")
    v = cfg_get("trigger_thresholds.bb_length", config={})
    assert v == 25
    assert isinstance(v, int)


# ── config dict 部分覆盖：未给叶保留 canonical ─────────────────────────────

def test_r13_b8_config_dict_partial_overlay():
    cfg = {
        "trigger_weights": {"trend_strength": 0.70},
        "trigger_thresholds": {"trend_momentum_pct": 6.0, "bb_length": 30},
    }
    assert cfg_get("trigger_weights.trend_strength", config=cfg) == 0.70
    assert cfg_get("trigger_weights.pct_move_spike", config=cfg) == 0.40  # canonical 保留
    assert cfg_get("trigger_thresholds.trend_momentum_pct", config=cfg) == 6.0
    assert cfg_get("trigger_thresholds.bb_length", config=cfg) == 30
    assert cfg_get("trigger_thresholds.sigma_threshold", config=cfg) == 2.0


# ── read_agent_config 可见：dashboard dump 包含两块 ────────────────────────

def test_r13_b8_read_agent_config_exposes_blocks():
    cfg = read_agent_config()
    assert "trigger_weights" in cfg
    assert "trigger_thresholds" in cfg
    assert cfg["trigger_weights"]["trend_strength"] == 0.55
    assert cfg["trigger_thresholds"]["trend_momentum_pct"] == 5.0


def test_r13_b8_read_agent_config_deep_merges_partial_overlay(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "trigger_weights": {"breakout": 0.65},
        "trigger_thresholds": {"momentum_pct": 5.5},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg["trigger_weights"]["breakout"] == 0.65          # 覆盖
    assert cfg["trigger_weights"]["trend_strength"] == 0.55   # canonical 保留
    assert cfg["trigger_thresholds"]["momentum_pct"] == 5.5   # 覆盖
    assert cfg["trigger_thresholds"]["trend_momentum_pct"] == 5.0


# ── schema：两块作为 object 被 strict_keys 接受 ────────────────────────────

def test_r13_b8_blocks_accepted_by_schema():
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "trigger_weights": {"trend_strength": 0.60, "daily_mover": 0.10},
        "trigger_thresholds": {"trend_momentum_pct": 6.5, "bb_length": 25},
    }, strict_keys=True)
    assert not any("unknown key" in e for e in errors), errors
    assert errors == [], errors


def test_r13_b8_config_patch_knows_both_fields():
    """drift sentinel：CANONICAL_DEFAULTS 加块后 _ConfigPatch 必须同步声明。"""
    from hermes_trader.agents.config_schema import _ConfigPatch
    fields = _ConfigPatch.model_fields
    assert "trigger_weights" in fields
    assert "trigger_thresholds" in fields
    w = fields["trigger_weights"].default_factory()
    t = fields["trigger_thresholds"].default_factory()
    assert w["trend_strength"] == 0.55
    assert t["trend_momentum_pct"] == 5.0


# ── helper：默认值 / camelCase 键集 / 与 TRIGGER_CONFIG 严格相等 ───────────

def test_r13_b8_weights_helper_defaults_equal_trigger_config():
    """helper 返回驼峰键，且默认值逐键 == TRIGGER_CONFIG['weights']。"""
    w = trigger_weights_params(config={})
    assert w == TRIGGER_CONFIG["weights"]
    assert set(w) == set(_TRIGGER_WEIGHTS_KEYMAP.values())


def test_r13_b8_thresholds_helper_defaults_equal_trigger_config():
    t = trigger_thresholds_params(config={})
    assert t == TRIGGER_CONFIG["thresholds"]
    assert set(t) == {camel for camel, _ in _TRIGGER_THRESHOLDS_KEYMAP.values()}


def test_r13_b8_weights_helper_env_override_flows_to_camel_key(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_TRIGGER_WEIGHTS__BREAKOUT", "0.88")
    w = trigger_weights_params(config={})
    assert w["breakout"] == 0.88
    assert w["trendStrength"] == 0.55  # 其余不动


def test_r13_b8_thresholds_helper_env_override_flows_to_camel_key(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_TRIGGER_THRESHOLDS__MOMENTUM_PCT", "6.25")
    t = trigger_thresholds_params(config={})
    assert t["momentumPct"] == 6.25
    assert t["trendMomentumPct"] == 5.0


def test_r13_b8_helper_config_dict_override():
    cfg = {"trigger_thresholds": {"adx_period": 21}}
    t = trigger_thresholds_params(config=cfg)
    assert t["adxPeriod"] == 21
    assert t["bbLength"] == 20  # canonical 保留


# ── helper guard：坏值回退字面量，热路径不崩 ──────────────────────────────

def test_r13_b8_weights_guard_negative_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_TRIGGER_WEIGHTS__TREND_STRENGTH", "-0.5")
    w = trigger_weights_params(config={})
    assert w["trendStrength"] == 0.55  # 负权重非法 → 字面量


def test_r13_b8_weights_guard_zero_is_legal(monkeypatch):
    """权重 0 合法（六个零权重是有意的）；把正权重覆成 0 必须生效。"""
    monkeypatch.setenv("HERMES_CFG_TRIGGER_WEIGHTS__TREND_STRENGTH", "0")
    w = trigger_weights_params(config={})
    assert w["trendStrength"] == 0.0


def test_r13_b8_thresholds_guard_non_positive_float_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_TRIGGER_THRESHOLDS__SIGMA_THRESHOLD", "0")
    t = trigger_thresholds_params(config={})
    assert t["sigmaThreshold"] == 2.0  # 浮点阈值须 >0


def test_r13_b8_thresholds_guard_int_below_one_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_TRIGGER_THRESHOLDS__ADX_PERIOD", "0")
    t = trigger_thresholds_params(config={})
    assert t["adxPeriod"] == 14  # int 阈值须 >=1


def test_r13_b8_helper_guard_garbage_string_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_TRIGGER_WEIGHTS__VOLUME_SPIKE", "notanumber")
    w = trigger_weights_params(config={})
    assert w["volumeSpike"] == 0.25  # coerce 失败 → 整块字面量
    assert w == TRIGGER_CONFIG["weights"]


def test_r13_b8_helper_returns_independent_copy():
    """helper 每次返回独立 dict，调用方 mutate 不污染 TRIGGER_CONFIG 字面量。"""
    w1 = trigger_weights_params(config={})
    w1["trendStrength"] = 999.0
    t1 = trigger_thresholds_params(config={})
    t1["bbLength"] = 999
    w2 = trigger_weights_params(config={})
    t2 = trigger_thresholds_params(config={})
    assert w2["trendStrength"] == 0.55
    assert t2["bbLength"] == 20
    assert TRIGGER_CONFIG["weights"]["trendStrength"] == 0.55
    assert TRIGGER_CONFIG["thresholds"]["bbLength"] == 20


# ── D6 drift 修复：签名默认 + 行为 + 源码死兜底清除 + scan_once 接线 ───────

def test_r13_b8_d6_trigger_signature_defaults_are_five():
    """uptrend/downtrend_momentum 的 pct_threshold 签名默认必须是 5.0。"""
    from hermes_trader.indicators.triggers import downtrend_momentum, uptrend_momentum
    assert inspect.signature(uptrend_momentum).parameters["pct_threshold"].default == 5.0
    assert inspect.signature(downtrend_momentum).parameters["pct_threshold"].default == 5.0


def test_r13_b8_d6_default_threshold_rejects_3_7pct_move():
    """~+3.7% / ~-3.7% 的持续走势：旧默认 3.0 会 fire（泛滥源头），新默认
    5.0 不 fire；显式传 3.0 仍 fire（证明不是数据问题，是阈值问题）。"""
    from hermes_trader.indicators.triggers import downtrend_momentum, uptrend_momentum
    from hermes_trader.models.types import Candle

    up = [Candle(t=i, o=100, h=101, l=99, c=100.0 * (1.0005 ** i), v=10)
          for i in range(80)]    # 1.0005**72 ≈ +3.67%
    down = [Candle(t=i, o=100, h=101, l=99, c=100.0 * (0.9995 ** i), v=10)
            for i in range(80)]  # ≈ -3.67%

    # 默认签名（5.0）：3.7% 不达 5%，不 surface
    assert uptrend_momentum(up)["fired"] is False
    assert downtrend_momentum(down)["fired"] is False
    # 显式 3.0：3.7% 达标，会 fire（旧行为，复现泛滥条件）
    assert uptrend_momentum(up, 72, 3.0)["fired"] is True
    assert downtrend_momentum(down, 72, 3.0)["fired"] is True
    # 显式 5.0：确定不 fire
    assert uptrend_momentum(up, 72, 5.0)["fired"] is False
    assert downtrend_momentum(down, 72, 5.0)["fired"] is False


def test_r13_b8_d6_no_three_point_zero_dead_fallback_in_sources():
    """perception / triggers 源码不得再残留 trendMomentumPct/pct_threshold
    的 3.0 死兜底。"""
    perc_src = inspect.getsource(config_mod.perception) if hasattr(config_mod, "perception") else None
    if perc_src is None:
        import hermes_trader.agents.perception as perception_mod
        perc_src = inspect.getsource(perception_mod)
    import hermes_trader.indicators.triggers as triggers_mod
    trig_src = inspect.getsource(triggers_mod)

    assert 'trendMomentumPct", 3.0' not in perc_src, "perception 仍有 3.0 死兜底"
    assert 'trendMomentumPct", 5.0' in perc_src, "perception 应对齐 5.0"
    assert "pct_threshold: float = 3.0" not in trig_src, "triggers 签名默认仍是 3.0"
    assert "pct_threshold: float = 5.0" in trig_src


def test_r13_b8_scan_once_injects_canonical_blocks():
    """scan_once 必须在浅合并后注入两块 helper 结果（env 热路径生效点）。"""
    import hermes_trader.agents.perception as perception_mod
    src = inspect.getsource(perception_mod.scan_once)
    assert "trigger_weights_params(config=_cfg)" in src
    assert "trigger_thresholds_params(config=_cfg)" in src
    assert 'scan_cfg["weights"]' in src
    assert 'scan_cfg["thresholds"]' in src


def test_r13_b8_perception_module_imports_helpers():
    """perception 顶层 import 必须包含两个 helper（注入点依赖）。"""
    import hermes_trader.agents.perception as perception_mod
    assert hasattr(perception_mod, "trigger_weights_params")
    assert hasattr(perception_mod, "trigger_thresholds_params")
    assert perception_mod.trigger_weights_params is config_mod.trigger_weights_params
    assert perception_mod.trigger_thresholds_params is config_mod.trigger_thresholds_params
