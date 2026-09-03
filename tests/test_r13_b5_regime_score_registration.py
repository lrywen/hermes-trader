"""R13-B5: regime 评分子系统常量注册测试（market_regime.py + executor.py）。

修复前，5 分量趋势强度评分（与 scripts/backtest_ab_compare._regime_score
字节对齐的回测参考）的全部权重与标定字面量分散在两处、对 config 不可见：

  1. **双份字面量漂移隐患 (最高优)** — market_regime.regime_strength_score()
     与 executor.regime_strength_label()（Plan B 仓位决策）各自内联了一套
     字节级复刻的标定（adx_zero=15 / span=30、atr zero=0.2/span=0.8、
     ema_gap_full=0.5%、price_ext_full=2.0 ATR、obv_flat=0.3）。两处只有
     REGIME_WEIGHTS 是 import 共享的，标定全靠人肉同步——改一处忘另一处
     就是 silent drift（回测/风控闸门 vs Plan B 仓位不一致）。
  2. **未登记 (纯硬编码)** — 5 个权重（0.25/0.225/0.175×3）、EMA8/21、
     指标周期 14、min_candles 50、obv_slope_period 10，以及 regime 分类器
     的缓存 TTL ``REGIME_TTL_S=300``、快 EMA 斜率窗口 ``_SLOPE_LOOKBACK=8``
     （候选 4），全部是模块/函数内字面量，运维不可调、dashboard 不可见。

修复方式（零行为变化）：

  * CANONICAL_DEFAULTS 新增嵌套块 ``regime_score``（17 键：5 权重 +
    7 标定锚点 + 5 周期/阈值）；``regime_classifier`` 块扩
    ``slope_lookback=8`` / ``ttl_sec=300`` 两键。
  * market_regime 新增公共 helper ``regime_score_params()``（per-leaf
    cfg_get，每个键可独立 env override；整体 try/except + 除数/周期 guard，
    任何坏 config 回退模块字面量，评分路径绝不崩）；executor 改为
    import 复用——单一数据源，两份标定从根上不可能漂移。
  * 缓存 TTL / 斜率窗口走 ``_regime_cache_ttl()`` / ``_slope_lookback()``
    热路径重解析；模块常量 REGIME_WEIGHTS / _SLOPE_LOOKBACK / REGIME_TTL_S
    保留为 fallback 与外部符号（research.py 仍 import _obv_slope_sign 默认参）。

零行为变化约束：canonical 默认值严格等于旧硬编码字面量；未设 env/config
时 regime_strength_score() / regime_strength_label() 输出与改前完全一致。
"""

import json

from hermes_trader.agents import config_store, executor, market_regime
from hermes_trader.agents.config_schema import _ConfigPatch
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)
from hermes_trader.agents.market_regime import (
    _SLOPE_LOOKBACK,
    REGIME_TTL_S,
    REGIME_WEIGHTS,
    regime_score_params,
    regime_strength_score,
)
from hermes_trader.models.types import Candle

# ── helpers ───────────────────────────────────────────────────────────

def _mk(t, o, h, l, c, v=1000.0):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _trend_candles(n=60, start=100.0, step=0.5):
    """Steadily rising candles (high ADX, OBV aligned up)."""
    return [_mk(i, start + i * step, start + i * step + 0.8,
                start + i * step - 0.2, start + i * step, 1000.0 + i)
            for i in range(n)]


def _flat_candles(n=60, price=100.0, rng=0.3):
    """Choppy/flat candles oscillating around `price` (low ADX)."""
    out = []
    for i in range(n):
        s = 1.0 if i % 2 == 0 else -1.0
        c = price + s * rng
        out.append(_mk(i, c, c + rng, c - rng, c, 1000.0))
    return out


# ── 1. canonical 登记断言 ──────────────────────────────────────────────

def test_r13_b5_regime_score_block_registered():
    """regime_score 嵌套块必须登记在 CANONICAL_DEFAULTS。"""
    assert "regime_score" in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS["regime_score"], dict)


def test_r13_b5_regime_score_block_has_exactly_17_keys():
    """sentinel：未来加/删字段都必须在测试里显式改。"""
    block = CANONICAL_DEFAULTS["regime_score"]
    assert set(block.keys()) == {
        "weight_adx", "weight_atr", "weight_ema_align",
        "weight_price_ext", "weight_obv",
        "adx_zero", "adx_full_span",
        "atr_pct_zero", "atr_pct_full_span",
        "ema_gap_full_pct", "price_ext_full_atr", "obv_flat_score",
        "ema_fast", "ema_slow", "ind_period", "min_candles",
        "obv_slope_period",
    }
    assert len(block) == 17


def test_r13_b5_regime_score_weights_match_historical_literals():
    """5 权重默认值严格等于 REGIME_WEIGHTS 旧字面量；和为 1.0。"""
    block = CANONICAL_DEFAULTS["regime_score"]
    assert block["weight_adx"] == REGIME_WEIGHTS["adx"] == 0.25
    assert block["weight_atr"] == REGIME_WEIGHTS["atr"] == 0.225
    assert block["weight_ema_align"] == REGIME_WEIGHTS["ema_align"] == 0.175
    assert block["weight_price_ext"] == REGIME_WEIGHTS["price_ext"] == 0.175
    assert block["weight_obv"] == REGIME_WEIGHTS["obv"] == 0.175
    total = (block["weight_adx"] + block["weight_atr"] + block["weight_ema_align"]
             + block["weight_price_ext"] + block["weight_obv"])
    assert abs(total - 1.0) < 1e-9


def test_r13_b5_regime_score_calibration_matches_historical_literals():
    """7 个标定锚点默认值严格等于旧内联字面量（零行为变化）。"""
    block = CANONICAL_DEFAULTS["regime_score"]
    assert block["adx_zero"] == 15.0
    assert block["adx_full_span"] == 30.0
    assert block["atr_pct_zero"] == 0.2
    assert block["atr_pct_full_span"] == 0.8
    assert block["ema_gap_full_pct"] == 0.5
    assert block["price_ext_full_atr"] == 2.0
    assert block["obv_flat_score"] == 0.3


def test_r13_b5_regime_score_periods_match_historical_literals():
    """5 个周期/阈值默认值严格等于旧字面量。"""
    block = CANONICAL_DEFAULTS["regime_score"]
    assert block["ema_fast"] == 8
    assert block["ema_slow"] == 21
    assert block["ind_period"] == 14
    assert block["min_candles"] == 50
    assert block["obv_slope_period"] == 10


def test_r13_b5_regime_classifier_new_keys_registered():
    """候选 4：regime_classifier 块扩 slope_lookback / ttl_sec。"""
    block = CANONICAL_DEFAULTS["regime_classifier"]
    assert block["slope_lookback"] == _SLOPE_LOOKBACK == 8
    assert block["ttl_sec"] == REGIME_TTL_S == 300
    # 原有 4 键不受影响
    assert block["fast_ema"] == 20
    assert block["slow_ema"] == 30
    assert block["slope_threshold"] == 0.002
    assert block["chop_adx_max"] == 20.0


def test_r13_b5_module_defaults_align_with_canonical():
    """market_regime._REGIME_SCORE_DEFAULTS（fallback 字面量）必须与
    canonical 逐键一致——任何一边漂移就是 silent regression。"""
    d = market_regime._REGIME_SCORE_DEFAULTS
    block = CANONICAL_DEFAULTS["regime_score"]
    assert d["weights"] == REGIME_WEIGHTS
    for key in ("adx_zero", "adx_full_span", "atr_pct_zero", "atr_pct_full_span",
                "ema_gap_full_pct", "price_ext_full_atr", "obv_flat_score",
                "ema_fast", "ema_slow", "ind_period", "min_candles",
                "obv_slope_period"):
        assert d[key] == block[key], f"fallback literal drift on {key}"


# ── 2. cfg_get 解析：点路径 + 空 config 回退 canonical ────────────────

def test_r13_b5_cfg_get_dotted_paths_empty_config():
    """空 config 下所有 dotted 路径回退 canonical 默认。"""
    assert cfg_get("regime_score.weight_adx", config={}) == 0.25
    assert cfg_get("regime_score.weight_obv", config={}) == 0.175
    assert cfg_get("regime_score.adx_zero", config={}) == 15.0
    assert cfg_get("regime_score.adx_full_span", config={}) == 30.0
    assert cfg_get("regime_score.ema_fast", config={}) == 8
    assert cfg_get("regime_score.obv_slope_period", config={}) == 10
    assert cfg_get("regime_classifier.slope_lookback", config={}) == 8
    assert cfg_get("regime_classifier.ttl_sec", config={}) == 300


def test_r13_b5_cfg_get_block_returns_dict():
    block = cfg_get("regime_score", config={})
    assert isinstance(block, dict)
    assert block["weight_adx"] == 0.25
    assert block["min_candles"] == 50


# ── 3. env 覆盖（per-leaf canonical env 路由） ────────────────────────

def test_r13_b5_env_override_single_weight(monkeypatch):
    """单个权重可经 HERMES_CFG_REGIME_SCORE__WEIGHT_ADX 独立覆盖。"""
    assert cfg_get("regime_score.weight_adx", config={}) == 0.25
    monkeypatch.setenv("HERMES_CFG_REGIME_SCORE__WEIGHT_ADX", "0.40")
    assert cfg_get("regime_score.weight_adx", config={}) == 0.40
    # 其余权重不受影响
    assert cfg_get("regime_score.weight_atr", config={}) == 0.225


def test_r13_b5_env_override_calibration_and_periods(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_REGIME_SCORE__ADX_FULL_SPAN", "40.0")
    monkeypatch.setenv("HERMES_CFG_REGIME_SCORE__EMA_FAST", "10")
    monkeypatch.setenv("HERMES_CFG_REGIME_SCORE__MIN_CANDLES", "80")
    assert cfg_get("regime_score.adx_full_span", config={}) == 40.0
    assert cfg_get("regime_score.ema_fast", config={}) == 10
    assert cfg_get("regime_score.min_candles", config={}) == 80


def test_r13_b5_env_override_classifier_ttl_and_lookback(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_REGIME_CLASSIFIER__TTL_SEC", "600")
    monkeypatch.setenv("HERMES_CFG_REGIME_CLASSIFIER__SLOPE_LOOKBACK", "12")
    assert cfg_get("regime_classifier.ttl_sec", config={}) == 600
    assert cfg_get("regime_classifier.slope_lookback", config={}) == 12


# ── 4. config dict 部分覆盖：deep merge ───────────────────────────────

def test_r13_b5_config_dict_partial_overlay():
    """config 含子集覆盖时，未列出的键仍走 canonical。"""
    cfg = {"regime_score": {"weight_adx": 0.5, "obv_flat_score": 0.1}}
    assert cfg_get("regime_score.weight_adx", config=cfg) == 0.5
    assert cfg_get("regime_score.obv_flat_score", config=cfg) == 0.1
    assert cfg_get("regime_score.weight_atr", config=cfg) == 0.225
    assert cfg_get("regime_score.adx_zero", config=cfg) == 15.0


def test_r13_b5_config_dict_classifier_overlay():
    cfg = {"regime_classifier": {"ttl_sec": 120}}
    assert cfg_get("regime_classifier.ttl_sec", config=cfg) == 120
    assert cfg_get("regime_classifier.slope_lookback", config=cfg) == 8


# ── 5. read_agent_config 完整可见 + 深合并 ───────────────────────────

def test_r13_b5_read_agent_config_exposes_blocks(monkeypatch, tmp_path):
    """read_agent_config() 返回值包含 regime_score 全部 17 键 + 新增 2 键。"""
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    rs = cfg["regime_score"]
    assert rs["weight_adx"] == 0.25
    assert rs["obv_flat_score"] == 0.3
    assert rs["ind_period"] == 14
    assert cfg["regime_classifier"]["slope_lookback"] == 8
    assert cfg["regime_classifier"]["ttl_sec"] == 300


def test_r13_b5_read_agent_config_deep_merges_overlay(monkeypatch, tmp_path):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "regime_score": {"weight_adx": 0.33, "adx_zero": 20.0},
        "regime_classifier": {"ttl_sec": 900},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    assert cfg["regime_score"]["weight_adx"] == 0.33
    assert cfg["regime_score"]["adx_zero"] == 20.0
    # 未覆盖键仍为 canonical
    assert cfg["regime_score"]["weight_atr"] == 0.225
    assert cfg["regime_score"]["ema_fast"] == 8
    assert cfg["regime_classifier"]["slope_lookback"] == 8
    assert cfg["regime_classifier"]["ttl_sec"] == 900


# ── 6. schema 接受 + drift sentinel ──────────────────────────────────

def test_r13_b5_schema_declares_regime_score():
    """_ConfigPatch 必须把 regime_score 声明为字段（dashboard 可写）。"""
    from typing import Any
    fields = _ConfigPatch.model_fields
    assert "regime_score" in fields
    assert fields["regime_score"].annotation == dict[str, Any]
    assert "regime_classifier" in fields


def test_r13_b5_validate_config_updates_accepts_blocks():
    """含 R13-B5 块的 patch 通过 strict_keys 校验。"""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "regime_score": {
            "weight_adx": 0.3, "adx_full_span": 40.0, "ema_fast": 10,
        },
        "regime_classifier": {"ttl_sec": 600, "slope_lookback": 12},
    })
    assert errors == []


# ── 7. regime_score_params() helper：默认 / 覆盖 / guard ─────────────

def test_r13_b5_params_defaults_equal_literals():
    """无 env/config 时 helper 返回的全部值 == 模块字面量（零行为变化）。"""
    p = regime_score_params(config={})
    assert p["weights"] == REGIME_WEIGHTS
    assert p["adx_zero"] == 15.0
    assert p["adx_full_span"] == 30.0
    assert p["atr_pct_zero"] == 0.2
    assert p["atr_pct_full_span"] == 0.8
    assert p["ema_gap_full_pct"] == 0.5
    assert p["price_ext_full_atr"] == 2.0
    assert p["obv_flat_score"] == 0.3
    assert p["ema_fast"] == 8
    assert p["ema_slow"] == 21
    assert p["ind_period"] == 14
    assert p["min_candles"] == 50
    assert p["obv_slope_period"] == 10


def test_r13_b5_params_env_override_flows_through(monkeypatch):
    """helper 是 cfg-driven：env 改权重/标定后返回值随之变化。"""
    monkeypatch.setenv("HERMES_CFG_REGIME_SCORE__WEIGHT_ADX", "0.45")
    monkeypatch.setenv("HERMES_CFG_REGIME_SCORE__ADX_ZERO", "18.0")
    monkeypatch.setenv("HERMES_CFG_REGIME_SCORE__EMA_SLOW", "25")
    p = regime_score_params(config={})
    assert p["weights"]["adx"] == 0.45
    assert p["adx_zero"] == 18.0
    assert p["ema_slow"] == 25
    # 未覆盖的不动
    assert p["weights"]["atr"] == 0.225


def test_r13_b5_params_config_override_flows_through():
    cfg = {"regime_score": {"obv_flat_score": 0.15, "min_candles": 70}}
    p = regime_score_params(config=cfg)
    assert p["obv_flat_score"] == 0.15
    assert p["min_candles"] == 70


def test_r13_b5_params_guard_nonpositive_divisors_fall_back():
    """除数类标定（span/full_pct/full_atr）<=0 时回退字面量，杜绝除零。"""
    cfg = {"regime_score": {
        "adx_full_span": 0, "atr_pct_full_span": -1.0,
        "ema_gap_full_pct": 0.0, "price_ext_full_atr": -2.0,
    }}
    p = regime_score_params(config=cfg)
    assert p["adx_full_span"] == 30.0
    assert p["atr_pct_full_span"] == 0.8
    assert p["ema_gap_full_pct"] == 0.5
    assert p["price_ext_full_atr"] == 2.0


def test_r13_b5_params_guard_nonpositive_periods_fall_back():
    """int 周期 <=0 时回退字面量。"""
    cfg = {"regime_score": {
        "ema_fast": 0, "ema_slow": -5, "ind_period": 0,
        "min_candles": -1, "obv_slope_period": 0,
    }}
    p = regime_score_params(config=cfg)
    assert p["ema_fast"] == 8
    assert p["ema_slow"] == 21
    assert p["ind_period"] == 14
    assert p["min_candles"] == 50
    assert p["obv_slope_period"] == 10


def test_r13_b5_params_malformed_config_never_raises():
    """坏 config（字符串塞进数值键 / None 等）绝不冒泡——评分路径永不崩。"""
    cfg = {"regime_score": {
        "weight_adx": "not_a_number", "adx_zero": None,
        "ema_fast": {"nested": "bad"}, "min_candles": [1, 2],
    }}
    # 整体回退模块字面量
    p = regime_score_params(config=cfg)
    assert p["weights"]["adx"] == 0.25
    assert p["adx_zero"] == 15.0
    assert p["ema_fast"] == 8
    assert p["min_candles"] == 50


def test_r13_b5_params_returns_independent_copy():
    """调用方 mutate 返回 dict 不影响后续调用（深拷贝隔离）。"""
    p1 = regime_score_params(config={})
    p1["weights"]["adx"] = 9.99
    p1["adx_zero"] = 99.0
    p2 = regime_score_params(config={})
    assert p2["weights"]["adx"] == 0.25
    assert p2["adx_zero"] == 15.0


# ── 8. TTL / lookback helpers ─────────────────────────────────────────

def test_r13_b5_cache_ttl_default_and_override(monkeypatch):
    assert market_regime._regime_cache_ttl(config={}) == 300
    monkeypatch.setenv("HERMES_CFG_REGIME_CLASSIFIER__TTL_SEC", "120")
    assert market_regime._regime_cache_ttl(config={}) == 120


def test_r13_b5_cache_ttl_negative_falls_back():
    assert market_regime._regime_cache_ttl(
        config={"regime_classifier": {"ttl_sec": -5}}) == 300


def test_r13_b5_slope_lookback_default_and_override(monkeypatch):
    assert market_regime._slope_lookback(config={}) == 8
    monkeypatch.setenv("HERMES_CFG_REGIME_CLASSIFIER__SLOPE_LOOKBACK", "16")
    assert market_regime._slope_lookback(config={}) == 16


def test_r13_b5_slope_lookback_nonpositive_falls_back():
    assert market_regime._slope_lookback(
        config={"regime_classifier": {"slope_lookback": 0}}) == 8


# ── 9. hot-path 接线：regime_strength_score 行为 ──────────────────────

def test_r13_b5_strength_score_insufficient_candles_returns_zero():
    """不足 min_candles(50) 根 → 0.0（与原逻辑一致）。"""
    assert regime_strength_score([]) == 0.0
    assert regime_strength_score(_trend_candles(49)) == 0.0


def test_r13_b5_strength_score_trending_gt_flat():
    """强趋势 candles 评分显著高于盘整 candles（sanity，标定未坏）。"""
    trend = regime_strength_score(_trend_candles(80))
    flat = regime_strength_score(_flat_candles(80))
    assert 0.0 <= flat < trend <= 1.0
    assert trend > 0.5  # 稳步上涨 80 根应给出中高强度分


def test_r13_b5_strength_score_min_candles_env_takes_effect(monkeypatch):
    """hot-path 接线验证：提高 min_candles 后，原本可评分的 60 根 → 0.0。"""
    candles = _trend_candles(60)
    assert regime_strength_score(candles) > 0.0
    monkeypatch.setenv("HERMES_CFG_REGIME_SCORE__MIN_CANDLES", "100")
    assert regime_strength_score(candles) == 0.0


# ── 10. 单一数据源：executor 与 market_regime 同源不漂移 ──────────────

def test_r13_b5_executor_imports_shared_params_helper():
    """executor 必须 import 共享 helper（而不是再内联一套标定）。"""
    assert hasattr(executor, "regime_score_params")
    assert executor.regime_score_params is market_regime.regime_score_params


def test_r13_b5_executor_label_and_score_share_calibration():
    """同一组指标输入下，executor.regime_strength_label 与
    market_regime.regime_strength_score 用同一套标定 → 标签与分数一致。

    构造一个强趋势 snapshot：分数 >= strong_trend_threshold(0.70) 时
    label 必须是 STRONG_TREND。两处若标定漂移，这里会对不上。
    """
    # 先用真实 candles 算出一组强趋势指标值
    candles = _trend_candles(80, start=100.0, step=0.8)
    from hermes_trader.indicators.math import adx as _adx
    from hermes_trader.indicators.math import atr as _atr
    from hermes_trader.indicators.math import ema
    closes = [c.c for c in candles]
    e8 = ema(closes, 8)[-1]
    e21 = ema(closes, 21)[-1]
    atr_v = next(v for v in reversed(_atr(candles, 14)) if v == v)
    adx_v = next(v for v in reversed(_adx(candles, 14)) if v == v)
    close = closes[-1]
    analysis = {
        "ema8_1h": e8, "ema21_1h": e21, "atr1h": atr_v,
        "adx1h": adx_v, "close1h": close, "obv_slope_1h": 1,
    }
    score = regime_strength_score(candles)
    label = executor.regime_strength_label(analysis)
    assert score >= cfg_get("strong_trend_threshold")
    assert label == "STRONG_TREND"


def test_r13_b5_executor_label_thin_snapshot_returns_empty():
    """1h snapshot 缺字段 / close  falsy → ""（Plan B 不适用），行为不变。"""
    assert executor.regime_strength_label({}) == ""
    assert executor.regime_strength_label(
        {"ema8_1h": 1, "ema21_1h": 1, "atr1h": 1, "adx1h": 1,
         "close1h": 0}) == ""


def test_r13_b5_executor_label_chop_when_all_components_zero():
    """ADX/ATR/EMA gap/延伸全为 0（价格紧贴 EMA、无波动）→ CHOP。"""
    analysis = {
        "ema8_1h": 100.0, "ema21_1h": 100.0,  # e8 == e21 → gap 0；bullish=False
        "atr1h": 0.0, "adx1h": 0.0,           # ADX 0 → adx_c=0；atr_v=0 → atr_c=0
        "close1h": 100.0, "obv_slope_1h": 0,  # flat OBV
    }
    # e8==e21 → bullish=False；obv_dir=0 → obv_c=obv_flat_score(0.3)
    # score = 0.175*0.3 = 0.0525 < neutral_threshold(0.40) → CHOP
    assert executor.regime_strength_label(analysis) == "CHOP"


# ── 11. 无循环导入 + dashboard 可观测性 ───────────────────────────────

def test_r13_b5_market_regime_imports_cfg_get_cleanly():
    """market_regime import cfg_get 不产生循环依赖（模块已正常加载）。"""
    assert hasattr(market_regime, "cfg_get")
    assert market_regime.cfg_get is cfg_get


def test_r13_b5_regime_weights_still_exported():
    """REGIME_WEIGHTS 仍是模块级公开符号（research/backtest 外部引用契约）。"""
    assert isinstance(REGIME_WEIGHTS, dict)
    assert set(REGIME_WEIGHTS.keys()) == {
        "adx", "atr", "ema_align", "price_ext", "obv"}


def test_r13_b5_canonical_visible_for_dashboard_dump():
    """CANONICAL_DEFAULTS 是 dashboard / MCP dump 的 source of truth——
    R13-B5 新登记块必须出现且类型正确（避免渲染 crash）。"""
    assert "regime_score" in CANONICAL_DEFAULTS
    block = CANONICAL_DEFAULTS["regime_score"]
    assert isinstance(block["weight_adx"], float)
    assert isinstance(block["adx_zero"], float)
    assert isinstance(block["ema_fast"], int)
    assert isinstance(block["min_candles"], int)
    assert isinstance(CANONICAL_DEFAULTS["regime_classifier"]["ttl_sec"], int)
    assert isinstance(CANONICAL_DEFAULTS["regime_classifier"]["slope_lookback"], int)


# ── 12. 零行为变化：默认参数下评分与改前字面量路径一致 ───────────────

def test_r13_b5_zero_behavior_change_default_score_reproducible():
    """同一组 trend candles 两次评分完全一致（确定性），且落在与旧硬编码
    相同的数值区间——默认 params == 旧字面量，评分路径零行为变化。"""
    candles = _trend_candles(80, start=100.0, step=0.6)
    s1 = regime_strength_score(candles)
    s2 = regime_strength_score(candles)
    assert s1 == s2
    # 稳步上涨 80 根：ADX 高、OBV 向上对齐、EMA 多头排列 → 中高分
    assert s1 >= cfg_get("trend_threshold")
    flat = regime_strength_score(_flat_candles(80))
    assert flat < s1
