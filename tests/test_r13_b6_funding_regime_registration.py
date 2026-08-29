"""R13-B6: funding regime 子系统常量注册测试（hyperfeed.py）。

修复前，hyperfeed 的 funding 拥挤 regime 分类器（risk_gates funding 拥挤
闸门的热路径数据源）全部标定都是模块/函数内字面量，对 config 不可见：

  1. **孪生 TTL drift (修了一半)** — market_regime 的代理 regime 缓存
     ``REGIME_TTL_S=300`` 在 R13-B5 已接线为 ``regime_classifier.ttl_sec``；
     hyperfeed 的 funding regime 缓存 ``_FUNDING_REGIME_TTL_S=300`` 是同一
     语义的"5 分钟市场状态缓存"，却仍是硬编码——两个 300 一个可调一个不可调。
  2. **未登记 (纯硬编码)** — funding 拥挤阈值 ±0.0001、OI 地板
     1e7(crypto)/1e6(HIP-3 equity/commodity)、类别多空计数优势 margin=5，
     全部内联在 ``_compute_funding_regime()`` 里，运维不可调、dashboard
     不可见；hyperfeed.py 全文件零 cfg_get / 零 config_store import。

修复方式（零行为变化）：

  * CANONICAL_DEFAULTS 新增嵌套块 ``funding_regime``（5 键：ttl_sec=300、
    crowded_funding_threshold=0.0001、oi_floor_crypto=1e7、
    oi_floor_other=1e6、class_dominance_margin=5）。
  * hyperfeed 新增公共 helper ``funding_regime_params()``（per-leaf
    cfg_get，每个键可独立 env override；整体 try/except + 阈值/OI floor
    guard >0、margin/ttl guard >=0，任何坏 config 回退模块字面量，风控
    热路径绝不崩）；缓存 TTL 与 ``_compute_funding_regime`` 的全部标定
    改走 helper。
  * 模块常量 ``_FUNDING_REGIME_TTL_S`` 保留为 fallback 与外部符号。

零行为变化约束：canonical 默认值严格等于旧硬编码字面量；未设 env/config
时 market_get_funding_regime() / _compute_funding_regime() 输出与改前
完全一致。
"""

import json

from hermes_trader.agents import config_store, hyperfeed
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)
from hermes_trader.agents.config_schema import _ConfigPatch
from hermes_trader.agents.hyperfeed import (
    _FUNDING_REGIME_TTL_S,
    _FUNDING_REGIME_DEFAULTS,
    funding_regime_params,
)


# ── helpers ───────────────────────────────────────────────────────────

def _mk_market(coin, funding, oi=5e7, vol=1e8):
    return {"coin": coin, "funding": funding, "openInterest": oi,
            "dayNtlVlm": vol}


def _patch_universe(monkeypatch, universe):
    """get_universe is called with include_hip3=True inside the classifier."""
    monkeypatch.setattr(hyperfeed, "get_universe",
                        lambda *, include_hip3=False, **k: universe)


def _reset_cache(monkeypatch):
    monkeypatch.setattr(hyperfeed, "_funding_regime_cache", None)


# ── 1. canonical 登记断言 ──────────────────────────────────────────────

def test_r13_b6_funding_regime_block_registered():
    """funding_regime 嵌套块必须登记在 CANONICAL_DEFAULTS。"""
    assert "funding_regime" in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS["funding_regime"], dict)


def test_r13_b6_funding_regime_block_has_exactly_5_keys():
    """sentinel：未来加/删字段都必须在测试里显式改。"""
    block = CANONICAL_DEFAULTS["funding_regime"]
    assert set(block.keys()) == {
        "ttl_sec",
        "crowded_funding_threshold",
        "oi_floor_crypto",
        "oi_floor_other",
        "class_dominance_margin",
    }
    assert len(block) == 5


def test_r13_b6_defaults_match_historical_literals():
    """5 键默认值严格等于 hyperfeed 旧字面量（零行为变化）。"""
    block = CANONICAL_DEFAULTS["funding_regime"]
    assert block["ttl_sec"] == 300
    assert block["crowded_funding_threshold"] == 0.0001
    assert block["oi_floor_crypto"] == 10000000.0
    assert block["oi_floor_other"] == 1000000.0
    assert block["class_dominance_margin"] == 5


def test_r13_b6_module_ttl_constant_preserved():
    """_FUNDING_REGIME_TTL_S 仍是模块级符号且 == 300（外部引用契约）。"""
    assert _FUNDING_REGIME_TTL_S == 300


def test_r13_b6_module_defaults_align_with_canonical():
    """hyperfeed._FUNDING_REGIME_DEFAULTS（fallback 字面量）必须与 canonical
    逐键一致——任何一边漂移就是 silent regression。"""
    d = _FUNDING_REGIME_DEFAULTS
    block = CANONICAL_DEFAULTS["funding_regime"]
    assert d["ttl_sec"] == float(block["ttl_sec"]) == 300.0
    assert d["crowded_funding_threshold"] == block["crowded_funding_threshold"]
    assert d["oi_floor_crypto"] == block["oi_floor_crypto"]
    assert d["oi_floor_other"] == block["oi_floor_other"]
    assert d["class_dominance_margin"] == float(block["class_dominance_margin"])


# ── 2. cfg_get 解析：点路径 + 空 config 回退 canonical ────────────────

def test_r13_b6_cfg_get_dotted_paths_empty_config():
    """空 config 下所有 dotted 路径回退 canonical 默认。"""
    assert cfg_get("funding_regime.ttl_sec", config={}) == 300
    assert cfg_get("funding_regime.crowded_funding_threshold", config={}) == 0.0001
    assert cfg_get("funding_regime.oi_floor_crypto", config={}) == 10000000.0
    assert cfg_get("funding_regime.oi_floor_other", config={}) == 1000000.0
    assert cfg_get("funding_regime.class_dominance_margin", config={}) == 5


def test_r13_b6_cfg_get_block_returns_dict():
    block = cfg_get("funding_regime", config={})
    assert isinstance(block, dict)
    assert block["ttl_sec"] == 300
    assert block["class_dominance_margin"] == 5


# ── 3. env 覆盖（per-leaf canonical env 路由） ────────────────────────

def test_r13_b6_env_override_threshold(monkeypatch):
    """单个阈值可经 HERMES_CFG_FUNDING_REGIME__CROWDED_FUNDING_THRESHOLD
    独立覆盖。"""
    assert cfg_get("funding_regime.crowded_funding_threshold", config={}) == 0.0001
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__CROWDED_FUNDING_THRESHOLD", "0.0005")
    assert cfg_get("funding_regime.crowded_funding_threshold", config={}) == 0.0005
    # 其余键不受影响
    assert cfg_get("funding_regime.ttl_sec", config={}) == 300


def test_r13_b6_env_override_ttl_and_margin(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__TTL_SEC", "60")
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__CLASS_DOMINANCE_MARGIN", "2")
    assert cfg_get("funding_regime.ttl_sec", config={}) == 60
    assert cfg_get("funding_regime.class_dominance_margin", config={}) == 2


def test_r13_b6_env_override_oi_floors(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__OI_FLOOR_CRYPTO", "50000000")
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__OI_FLOOR_OTHER", "500000")
    assert cfg_get("funding_regime.oi_floor_crypto", config={}) == 50000000
    assert cfg_get("funding_regime.oi_floor_other", config={}) == 500000


# ── 4. config dict 部分覆盖：deep merge ───────────────────────────────

def test_r13_b6_config_dict_partial_overlay():
    """config 含子集覆盖时，未列出的键仍走 canonical。"""
    cfg = {"funding_regime": {"ttl_sec": 120, "class_dominance_margin": 3}}
    assert cfg_get("funding_regime.ttl_sec", config=cfg) == 120
    assert cfg_get("funding_regime.class_dominance_margin", config=cfg) == 3
    assert cfg_get("funding_regime.crowded_funding_threshold", config=cfg) == 0.0001
    assert cfg_get("funding_regime.oi_floor_crypto", config=cfg) == 10000000.0


# ── 5. read_agent_config 完整可见 + 深合并 ───────────────────────────

def test_r13_b6_read_agent_config_exposes_block(monkeypatch, tmp_path):
    """read_agent_config() 返回值包含 funding_regime 全部 5 键。"""
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    fr = cfg["funding_regime"]
    assert fr["ttl_sec"] == 300
    assert fr["crowded_funding_threshold"] == 0.0001
    assert fr["oi_floor_crypto"] == 10000000.0
    assert fr["oi_floor_other"] == 1000000.0
    assert fr["class_dominance_margin"] == 5


def test_r13_b6_read_agent_config_deep_merges_overlay(monkeypatch, tmp_path):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "funding_regime": {"ttl_sec": 900, "oi_floor_other": 2000000.0},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    assert cfg["funding_regime"]["ttl_sec"] == 900
    assert cfg["funding_regime"]["oi_floor_other"] == 2000000.0
    # 未覆盖键仍为 canonical
    assert cfg["funding_regime"]["crowded_funding_threshold"] == 0.0001
    assert cfg["funding_regime"]["class_dominance_margin"] == 5


# ── 6. schema 接受 + drift sentinel ──────────────────────────────────

def test_r13_b6_schema_declares_funding_regime():
    """_ConfigPatch 必须把 funding_regime 声明为字段（dashboard 可写）。"""
    from typing import Any
    fields = _ConfigPatch.model_fields
    assert "funding_regime" in fields
    assert fields["funding_regime"].annotation == dict[str, Any]


def test_r13_b6_validate_config_updates_accepts_block():
    """含 funding_regime 块的 patch 通过 strict_keys 校验。"""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "funding_regime": {
            "ttl_sec": 120,
            "crowded_funding_threshold": 0.0002,
            "oi_floor_crypto": 2e7,
            "oi_floor_other": 2e6,
            "class_dominance_margin": 3,
        },
    })
    assert errors == []


# ── 7. funding_regime_params() helper：默认 / 覆盖 / guard ────────────

def test_r13_b6_params_defaults_equal_literals():
    """无 env/config 时 helper 返回的全部值 == 模块字面量（零行为变化）。"""
    p = funding_regime_params(config={})
    assert p["ttl_sec"] == 300.0
    assert p["crowded_funding_threshold"] == 0.0001
    assert p["oi_floor_crypto"] == 1e7
    assert p["oi_floor_other"] == 1e6
    assert p["class_dominance_margin"] == 5.0


def test_r13_b6_params_env_override_flows_through(monkeypatch):
    """helper 是 cfg-driven：env 改标定后返回值随之变化。"""
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__TTL_SEC", "45")
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__CROWDED_FUNDING_THRESHOLD", "0.0003")
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__CLASS_DOMINANCE_MARGIN", "2")
    p = funding_regime_params(config={})
    assert p["ttl_sec"] == 45.0
    assert p["crowded_funding_threshold"] == 0.0003
    assert p["class_dominance_margin"] == 2.0
    # 未覆盖的不动
    assert p["oi_floor_crypto"] == 1e7


def test_r13_b6_params_config_override_flows_through():
    cfg = {"funding_regime": {"oi_floor_crypto": 4e7, "oi_floor_other": 4e6}}
    p = funding_regime_params(config=cfg)
    assert p["oi_floor_crypto"] == 4e7
    assert p["oi_floor_other"] == 4e6


def test_r13_b6_params_guard_nonpositive_threshold_falls_back():
    """funding 阈值 <=0 时回退字面量（0 阈值会把全市场判成拥挤）。"""
    cfg = {"funding_regime": {"crowded_funding_threshold": 0}}
    p = funding_regime_params(config=cfg)
    assert p["crowded_funding_threshold"] == 0.0001
    cfg = {"funding_regime": {"crowded_funding_threshold": -0.001}}
    p = funding_regime_params(config=cfg)
    assert p["crowded_funding_threshold"] == 0.0001


def test_r13_b6_params_guard_nonpositive_oi_floors_fall_back():
    """OI floor <=0 时回退字面量。"""
    cfg = {"funding_regime": {"oi_floor_crypto": 0, "oi_floor_other": -1.0}}
    p = funding_regime_params(config=cfg)
    assert p["oi_floor_crypto"] == 1e7
    assert p["oi_floor_other"] == 1e6


def test_r13_b6_params_guard_negative_ttl_falls_back():
    """TTL <0 时回退字面量（0 合法=永远重算，负数无意义）。"""
    p = funding_regime_params(config={"funding_regime": {"ttl_sec": -10}})
    assert p["ttl_sec"] == 300.0


def test_r13_b6_params_guard_negative_margin_falls_back():
    """优势 margin <0 时回退字面量。"""
    p = funding_regime_params(config={"funding_regime": {"class_dominance_margin": -3}})
    assert p["class_dominance_margin"] == 5.0


def test_r13_b6_params_zero_ttl_is_allowed():
    """ttl_sec=0 是合法运维值（禁用缓存、每次重算），不得被 guard 吞掉。"""
    p = funding_regime_params(config={"funding_regime": {"ttl_sec": 0}})
    assert p["ttl_sec"] == 0.0


def test_r13_b6_params_zero_margin_is_allowed():
    """margin=0 合法（任一多空计数差即判拥挤），不得被 guard 吞掉。"""
    p = funding_regime_params(config={"funding_regime": {"class_dominance_margin": 0}})
    assert p["class_dominance_margin"] == 0.0


def test_r13_b6_params_malformed_config_never_raises():
    """坏 config（字符串塞进数值键 / None / dict 等）绝不冒泡——风控热路径
    永不崩，整体回退模块字面量。"""
    cfg = {"funding_regime": {
        "ttl_sec": "not_a_number",
        "crowded_funding_threshold": None,
        "oi_floor_crypto": {"nested": "bad"},
        "oi_floor_other": [1, 2],
        "class_dominance_margin": "x",
    }}
    p = funding_regime_params(config=cfg)
    assert p["ttl_sec"] == 300.0
    assert p["crowded_funding_threshold"] == 0.0001
    assert p["oi_floor_crypto"] == 1e7
    assert p["oi_floor_other"] == 1e6
    assert p["class_dominance_margin"] == 5.0


def test_r13_b6_params_returns_independent_copy():
    """调用方 mutate 返回 dict 不影响后续调用。"""
    p1 = funding_regime_params(config={})
    p1["crowded_funding_threshold"] = 9.99
    p1["oi_floor_crypto"] = 1.0
    p2 = funding_regime_params(config={})
    assert p2["crowded_funding_threshold"] == 0.0001
    assert p2["oi_floor_crypto"] == 1e7


# ── 8. hot-path 接线：_compute_funding_regime 行为 ────────────────────

def test_r13_b6_compute_default_matches_old_literals(monkeypatch):
    """默认参数下：7 多 0 空（margin 7 > 5）→ LONG_CROWDED，与旧字面量
    路径完全一致（零行为变化）。"""
    universe = [_mk_market(c, 0.0002)
                for c in ("BTC", "ETH", "SOL", "DOGE", "AVAX", "XRP", "LINK")]
    _patch_universe(monkeypatch, universe)
    out = hyperfeed._compute_funding_regime()
    assert out["regimes_by_class"]["crypto"] == "LONG_CROWDED"
    assert out["regime"] == "LONG_CROWDED"


def test_r13_b6_compute_default_margin_boundary_neutral(monkeypatch):
    """默认 margin=5：5 多 0 空（差 5，不满足 >5）→ NEUTRAL。"""
    universe = [_mk_market(c, 0.0002) for c in ("BTC", "ETH", "SOL", "DOGE", "AVAX")]
    _patch_universe(monkeypatch, universe)
    out = hyperfeed._compute_funding_regime()
    assert out["regimes_by_class"]["crypto"] == "NEUTRAL"


def test_r13_b6_compute_threshold_env_takes_effect(monkeypatch):
    """hot-path 接线验证：收紧 funding 阈值到 0.0005 后，funding=0.0002
    的市场不再计入拥挤。"""
    # 7 个 funding=0.0002 的市场：默认阈值 0.0001 下全 counted → LONG_CROWDED
    universe = [_mk_market(c, 0.0002)
                for c in ("BTC", "ETH", "SOL", "DOGE", "AVAX", "XRP", "LINK")]
    _patch_universe(monkeypatch, universe)
    assert hyperfeed._compute_funding_regime()["regimes_by_class"]["crypto"] == "LONG_CROWDED"
    # 阈值提高到 0.0005 → 0.0002 不超阈值 → 0 多 0 空 → NEUTRAL
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__CROWDED_FUNDING_THRESHOLD", "0.0005")
    assert hyperfeed._compute_funding_regime()["regimes_by_class"]["crypto"] == "NEUTRAL"


def test_r13_b6_compute_margin_env_takes_effect(monkeypatch):
    """hot-path 接线验证：margin 降到 2 后，3 多 0 空即 LONG_CROWDED
    （默认 margin=5 下 3 多是 NEUTRAL）。"""
    universe = [_mk_market(c, 0.0002) for c in ("BTC", "ETH", "SOL")]
    _patch_universe(monkeypatch, universe)
    assert hyperfeed._compute_funding_regime()["regimes_by_class"]["crypto"] == "NEUTRAL"
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__CLASS_DOMINANCE_MARGIN", "2")
    assert hyperfeed._compute_funding_regime()["regimes_by_class"]["crypto"] == "LONG_CROWDED"


def test_r13_b6_compute_oi_floor_crypto_env_takes_effect(monkeypatch):
    """hot-path 接线验证：crypto OI floor 提到 8e7 后，OI=5e7 的多头市场
    被地板过滤 → NEUTRAL。"""
    universe = [_mk_market(c, 0.0002, oi=5e7)
                for c in ("BTC", "ETH", "SOL", "DOGE", "AVAX", "XRP", "LINK")]
    _patch_universe(monkeypatch, universe)
    assert hyperfeed._compute_funding_regime()["regimes_by_class"]["crypto"] == "LONG_CROWDED"
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__OI_FLOOR_CRYPTO", "8e7")
    assert hyperfeed._compute_funding_regime()["regimes_by_class"]["crypto"] == "NEUTRAL"


def test_r13_b6_compute_oi_floor_other_class_scaling(monkeypatch):
    """HIP-3 equity/commodity 市场走 oi_floor_other（1e6）：OI=5e6 的
    namespaced 市场在默认地板上计入；floor 提到 8e6 后被过滤。

    用 7 个未登记的 xyz: 假名——classify_asset 对 namespaced 且不在
    commodity/equity/native 白名单的 bare ticker 一律归 equity，保证 7 个
    多头落在同一类（margin 7 > 5）。
    """
    universe = [_mk_market(c, 0.0002, oi=5e6)
                for c in ("xyz:ZZA", "xyz:ZZB", "xyz:ZZC", "xyz:ZZD",
                          "xyz:ZZE", "xyz:ZZF", "xyz:ZZG")]
    _patch_universe(monkeypatch, universe)
    out = hyperfeed._compute_funding_regime()
    assert out["regimes_by_class"]["equity"] == "LONG_CROWDED"
    # crypto 类无市场 → NEUTRAL，不受影响
    assert out["regimes_by_class"]["crypto"] == "NEUTRAL"
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__OI_FLOOR_OTHER", "8e6")
    out2 = hyperfeed._compute_funding_regime()
    assert out2["regimes_by_class"]["equity"] == "NEUTRAL"


def test_r13_b6_compute_short_crowded_symmetric(monkeypatch):
    """负 funding 对称：7 个 funding=-0.0002 → SHORT_CROWDED。"""
    universe = [_mk_market(c, -0.0002)
                for c in ("BTC", "ETH", "SOL", "DOGE", "AVAX", "XRP", "LINK")]
    _patch_universe(monkeypatch, universe)
    out = hyperfeed._compute_funding_regime()
    assert out["regimes_by_class"]["crypto"] == "SHORT_CROWDED"
    assert out["regime"] == "SHORT_CROWDED"


# ── 9. 缓存 TTL 接线 ──────────────────────────────────────────────────

def test_r13_b6_cache_ttl_default_300(monkeypatch):
    """默认 TTL=300：缓存后 299s 内不重算（compute 不被再次调用）。"""
    _reset_cache(monkeypatch)
    calls = {"n": 0}

    def _fake_compute():
        calls["n"] += 1
        return {"regime": "NEUTRAL", "regimes_by_class": {}, "assets": []}

    monkeypatch.setattr(hyperfeed, "_compute_funding_regime", _fake_compute)
    t0 = 1000.0
    monkeypatch.setattr(hyperfeed.time, "time", lambda: t0)
    hyperfeed.market_get_funding_regime()
    hyperfeed.market_get_funding_regime()  # cached
    assert calls["n"] == 1
    # 299s later — still fresh (< 300)
    monkeypatch.setattr(hyperfeed.time, "time", lambda: t0 + 299)
    hyperfeed.market_get_funding_regime()
    assert calls["n"] == 1
    # 301s later — stale, recompute
    monkeypatch.setattr(hyperfeed.time, "time", lambda: t0 + 301)
    hyperfeed.market_get_funding_regime()
    assert calls["n"] == 2


def test_r13_b6_cache_ttl_env_takes_effect(monkeypatch):
    """TTL env=60：61s 后即重算（默认 300 下仍新鲜）。"""
    _reset_cache(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_FUNDING_REGIME__TTL_SEC", "60")
    calls = {"n": 0}

    def _fake_compute():
        calls["n"] += 1
        return {"regime": "NEUTRAL", "regimes_by_class": {}, "assets": []}

    monkeypatch.setattr(hyperfeed, "_compute_funding_regime", _fake_compute)
    t0 = 2000.0
    monkeypatch.setattr(hyperfeed.time, "time", lambda: t0)
    hyperfeed.market_get_funding_regime()
    assert calls["n"] == 1
    monkeypatch.setattr(hyperfeed.time, "time", lambda: t0 + 61)
    hyperfeed.market_get_funding_regime()
    assert calls["n"] == 2


# ── 10. 无循环导入 + dashboard 可观测性 ───────────────────────────────

def test_r13_b6_hyperfeed_imports_cfg_get_cleanly():
    """hyperfeed import cfg_get 不产生循环依赖（模块已正常加载）。"""
    assert hasattr(hyperfeed, "cfg_get")
    assert hyperfeed.cfg_get is cfg_get


def test_r13_b6_canonical_visible_for_dashboard_dump():
    """CANONICAL_DEFAULTS 是 dashboard / MCP dump 的 source of truth——
    R13-B6 新登记块必须出现且类型正确（避免渲染 crash）。"""
    block = CANONICAL_DEFAULTS["funding_regime"]
    assert isinstance(block["ttl_sec"], int)
    assert isinstance(block["crowded_funding_threshold"], float)
    assert isinstance(block["oi_floor_crypto"], float)
    assert isinstance(block["oi_floor_other"], float)
    assert isinstance(block["class_dominance_margin"], int)


# ── 11. 零行为变化：默认参数下输出确定性 ──────────────────────────────

def test_r13_b6_zero_behavior_change_deterministic(monkeypatch):
    """同一 universe 两次计算完全一致（确定性），且默认 params == 旧字面量。"""
    universe = [
        _mk_market("BTC", 0.0003, oi=8e7),
        _mk_market("ETH", 0.0002, oi=6e7),
        _mk_market("SOL", -0.0002, oi=3e7),
        _mk_market("DOGE", 0.00005, oi=2e7),   # below funding bar → NEUTRAL
        _mk_market("XRP", 0.0002, oi=500.0),    # below OI floor → NEUTRAL
    ]
    _patch_universe(monkeypatch, universe)
    out1 = hyperfeed._compute_funding_regime()
    out2 = hyperfeed._compute_funding_regime()
    assert out1 == out2
    # 3 counted longs (BTC/ETH + ...), 1 short: BTC/ETH long, SOL short,
    # DOGE/XRP neutral → long=2, short=1 → margin 1, not > 5 → NEUTRAL
    assert out1["regimes_by_class"]["crypto"] == "NEUTRAL"
    # assets sorted by funding rate desc
    rates = [a["funding_rate"] for a in out1["assets"]]
    assert rates == sorted(rates, reverse=True)
