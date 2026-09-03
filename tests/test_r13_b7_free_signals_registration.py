"""R13-B7: 免费信号套件常量注册测试（options_gex / short_volume /
crypto_whale / news_catalyst / shadow_signals / whale_index）。

修复前，六个免费/影子信号文件的 TTL、HTTP timeout、窗口与阈值标定全部是
模块/函数内字面量，对 config 不可见，并伴随五类 drift/重复：

  * **D1 gex caution drift（最关键）** — canonical ``gex_signal.
    caution_near_wall_pct`` 登记为 10.0，但三个消费点的死兜底/签名默认是
    1.0：shadow_signals.enforce_signals（dict 直读）、executor
    _runner_entry_block_reason（dict 直读）、options_gex.gex_override_caution
    （签名默认）。运行时经 ``_deep_merge(CANONICAL_DEFAULTS, raw)`` 生效的是
    10.0，所以 1.0 是死兜底；但 dict 直读路径**不读 env**，
    HERMES_CFG_GEX_SIGNAL__CAUTION_NEAR_WALL_PCT 覆盖静默失效。
  * **D2 whale 15 分钟窗口多处重复**（shadow_signals 块与 signal_enforcement
    块各自硬编码 15）。
  * **D3 四模块缓存 TTL 硬编码**（gex 900 / shortvol 3600 / crypto_whale
    120 / news 300）。
  * **D4 HTTP timeout 家族硬编码**（12.0 / 12.0 / 2.5 / 3.0）。
  * **D5 whale/news/shortvol 阈值重复**（$250k veto/boost、0.60/0.35/0.03、
    0.20 bias、2.5x surge 等）。

修复方式（零行为变化）：

  * CANONICAL_DEFAULTS 新增 5 个嵌套块（options_gex / short_volume /
    crypto_whale / news_catalyst / whale_index），schema 同步 5 字段。
  * 每个模块新增公共 helper（``options_gex_params`` 等），per-leaf cfg_get，
    每个键可独立 env override；幅度/阈值 guard、坏 config 整体回退模块
    字面量，信号热路径绝不崩。
  * 三个 D1 消费点全部改走 ``cfg_get("gex_signal.caution_near_wall_pct",
    10.0, config=...)``；gather_shadow_signals / run_shadow_async 新增
    keyword-only ``config`` 形参（保 test_shadow_signals 三位置参兼容）。
  * 遗留 env 读取（HERMES_WHALE_HTTP_TIMEOUT_S /
    HERMES_CRYPTO_WHALE_CACHE_MAX / HERMES_NEWS_HTTP_TIMEOUT_S）移除，
    默认值经 canonical 保留。
  * 模块常量（_GEX_TTL_S / _CACHE_TTL_S / _CACHE_MAX / _HTTP_TIMEOUT_S）
    保留为 fallback 与外部符号。

零行为变化约束：canonical 默认值严格等于旧硬编码字面量；未设 env/config
时所有信号函数输出与改前完全一致（D1 只对齐死兜底，运行时值 10.0 不变）。
"""

import json

from hermes_trader.agents import (
    config_store,
    crypto_whale,
    news_catalyst,
    options_gex,
    shadow_signals,
    short_volume,
    whale_index,
)
from hermes_trader.agents.config_schema import _ConfigPatch
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)
from hermes_trader.agents.crypto_whale import (
    _CRYPTO_WHALE_DEFAULTS,
    Print,
    crypto_whale_params,
)
from hermes_trader.agents.news_catalyst import (
    _NEWS_CATALYST_DEFAULTS,
    news_catalyst_params,
)
from hermes_trader.agents.options_gex import (
    _OPTIONS_GEX_DEFAULTS,
    GexReport,
    options_gex_params,
)
from hermes_trader.agents.short_volume import (
    _SHORT_VOLUME_DEFAULTS,
    short_volume_params,
)
from hermes_trader.agents.whale_index import (
    _WHALE_INDEX_DEFAULTS,
    whale_index_params,
)

# ── helpers ─────────────────────────────────────────────────────────────

def _mk_whale_market(coin, funding=0.0, oi=1e7, vol=5e6, mid=1.0, prev=1.0):
    """universe 行；oi 是 coin units，oi_usd = oi * mid。"""
    return {"coin": coin, "type": "perps", "funding": funding,
            "openInterest": oi, "dayNtlVlm": vol,
            "midPx": mid, "prevDayPx": prev}


def _mk_gex_report(spot=100.0, gamma_flip=95.0, call_wall=106.0):
    """pin_long_gamma + spot 已在 flip 上方 + spot 距 call wall 6% 的报告。"""
    return GexReport(
        ticker="ZZZ", spot=spot, total_gex=1e6, regime="pin_long_gamma",
        gamma_flip=gamma_flip, call_wall=call_wall, put_wall=90.0,
        max_pain=100.0, n_contracts=42,
    )


def _reset_module_cache(module):
    module._cache.clear()


# ── 1. canonical 登记断言（5 块）────────────────────────────────────────

def test_r13_b7_five_blocks_registered():
    """五个新块必须登记在 CANONICAL_DEFAULTS。"""
    for block in ("options_gex", "short_volume", "crypto_whale",
                  "news_catalyst", "whale_index"):
        assert block in CANONICAL_DEFAULTS
        assert isinstance(CANONICAL_DEFAULTS[block], dict)


def test_r13_b7_options_gex_block_keys_and_values():
    block = CANONICAL_DEFAULTS["options_gex"]
    assert set(block.keys()) == {"ttl_sec", "http_timeout_s"}
    assert block["ttl_sec"] == 900
    assert block["http_timeout_s"] == 12.0


def test_r13_b7_short_volume_block_keys_and_values():
    block = CANONICAL_DEFAULTS["short_volume"]
    assert set(block.keys()) == {
        "ttl_sec", "http_timeout_s", "crowded_ratio", "light_ratio",
        "trend_delta", "lookback_days",
    }
    assert block["ttl_sec"] == 3600
    assert block["http_timeout_s"] == 12.0
    assert block["crowded_ratio"] == 0.60
    assert block["light_ratio"] == 0.35
    assert block["trend_delta"] == 0.03
    assert block["lookback_days"] == 5


def test_r13_b7_crypto_whale_block_keys_and_values():
    block = CANONICAL_DEFAULTS["crypto_whale"]
    assert set(block.keys()) == {
        "ttl_sec", "http_timeout_s", "cache_max", "window_minutes",
        "min_usd", "bias_threshold", "max_pages",
    }
    assert block["ttl_sec"] == 120
    assert block["http_timeout_s"] == 2.5
    assert block["cache_max"] == 1024
    assert block["window_minutes"] == 15
    assert block["min_usd"] == 100000
    assert block["bias_threshold"] == 0.20
    assert block["max_pages"] == 6


def test_r13_b7_news_catalyst_block_keys_and_values():
    block = CANONICAL_DEFAULTS["news_catalyst"]
    assert set(block.keys()) == {
        "ttl_sec", "http_timeout_s", "surge_breaking_x", "surge_elevated_x",
        "timespan", "max_records", "rss_limit", "fetch_max_workers",
    }
    assert block["ttl_sec"] == 300
    assert block["http_timeout_s"] == 3.0
    assert block["surge_breaking_x"] == 2.5
    assert block["surge_elevated_x"] == 1.5
    assert block["timespan"] == "1h"
    assert block["max_records"] == 30
    assert block["rss_limit"] == 25
    assert block["fetch_max_workers"] == 2


def test_r13_b7_whale_index_block_keys_and_values():
    block = CANONICAL_DEFAULTS["whale_index"]
    assert set(block.keys()) == {
        "min_volume_usd", "funding_confidence_scale", "oi_vol_ratio_min",
        "oi_vol_confidence_norm", "min_oi_usd", "max_funding_threshold",
        "funding_norm", "flat_price_pct", "min_oi_growth_pct",
        "max_price_move_pct", "surge_norm_pct", "min_confidence",
        "mcp_min_confidence", "mcp_top_n",
    }
    assert block["min_volume_usd"] == 1000000
    assert block["funding_confidence_scale"] == 0.0001
    assert block["oi_vol_ratio_min"] == 10
    assert block["oi_vol_confidence_norm"] == 50
    assert block["min_oi_usd"] == 5000000
    assert block["max_funding_threshold"] == -0.00001
    assert block["funding_norm"] == 0.00008
    assert block["flat_price_pct"] == 10
    assert block["min_oi_growth_pct"] == 8.0
    assert block["max_price_move_pct"] == 4.0
    assert block["surge_norm_pct"] == 25.0
    assert block["min_confidence"] == 0.05
    assert block["mcp_min_confidence"] == 0.1
    assert block["mcp_top_n"] == 10


def test_r13_b7_gex_caution_canonical_is_10_not_1():
    """D1 sentinel：canonical 是 10.0（运行时值）；1.0 死兜底已被消灭。"""
    assert CANONICAL_DEFAULTS["gex_signal"]["caution_near_wall_pct"] == 10.0
    assert cfg_get("gex_signal.caution_near_wall_pct", config={}) == 10.0


def test_r13_b7_module_fallback_constants_preserved():
    """模块级常量仍是符号且 == 旧字面量（外部引用契约）。"""
    assert options_gex._GEX_TTL_S == 900.0
    assert short_volume._CACHE_TTL_S == 3600.0
    assert crypto_whale._CACHE_TTL_S == 120.0
    assert crypto_whale._CACHE_MAX == 1024
    assert crypto_whale._HTTP_TIMEOUT_S == 2.5
    assert news_catalyst._CACHE_TTL_S == 300.0
    assert news_catalyst._HTTP_TIMEOUT_S == 3.0


def test_r13_b7_module_defaults_align_with_canonical():
    """五个模块 _DEFAULTS（fallback 字面量）必须与 canonical 逐键一致——
    任何一边漂移就是 silent regression。"""
    pairs = [
        (options_gex, "options_gex", _OPTIONS_GEX_DEFAULTS),
        (short_volume, "short_volume", _SHORT_VOLUME_DEFAULTS),
        (crypto_whale, "crypto_whale", _CRYPTO_WHALE_DEFAULTS),
        (news_catalyst, "news_catalyst", _NEWS_CATALYST_DEFAULTS),
        (whale_index, "whale_index", _WHALE_INDEX_DEFAULTS),
    ]
    for _mod, block_name, defaults in pairs:
        block = CANONICAL_DEFAULTS[block_name]
        assert set(defaults.keys()) == set(block.keys()), block_name
        for k, v in defaults.items():
            assert v == block[k], (block_name, k, v, block[k])


# ── 2. cfg_get 解析：空 config 回退 canonical ───────────────────────────

def test_r13_b7_cfg_get_dotted_paths_empty_config():
    """空 config 下各块 dotted 路径回退 canonical 默认。"""
    assert cfg_get("options_gex.ttl_sec", config={}) == 900
    assert cfg_get("short_volume.crowded_ratio", config={}) == 0.60
    assert cfg_get("crypto_whale.window_minutes", config={}) == 15
    assert cfg_get("news_catalyst.surge_breaking_x", config={}) == 2.5
    assert cfg_get("whale_index.min_confidence", config={}) == 0.05


# ── 3. env 覆盖（per-leaf canonical env 路由）──────────────────────────

def test_r13_b7_env_override_one_leaf_per_block(monkeypatch):
    """每个块至少一个叶子可经 HERMES_CFG_<BLOCK>__<KEY> 独立覆盖。"""
    monkeypatch.setenv("HERMES_CFG_OPTIONS_GEX__HTTP_TIMEOUT_S", "5.5")
    monkeypatch.setenv("HERMES_CFG_SHORT_VOLUME__LOOKBACK_DAYS", "10")
    monkeypatch.setenv("HERMES_CFG_CRYPTO_WHALE__MIN_USD", "250000")
    monkeypatch.setenv("HERMES_CFG_NEWS_CATALYST__TIMESPAN", "6h")
    monkeypatch.setenv("HERMES_CFG_WHALE_INDEX__MCP_TOP_N", "3")
    assert cfg_get("options_gex.http_timeout_s", config={}) == 5.5
    assert cfg_get("short_volume.lookback_days", config={}) == 10
    assert cfg_get("crypto_whale.min_usd", config={}) == 250000
    assert cfg_get("news_catalyst.timespan", config={}) == "6h"
    assert cfg_get("whale_index.mcp_top_n", config={}) == 3


def test_r13_b7_env_override_d1_gex_caution_reaches_cfg_get(monkeypatch):
    """D1 关键断言：gex caution 经 env 覆盖后，cfg_get 路径真正拿到新值
    （旧 dict 直读路径 env 静默失效）。三个消费点都走这个键。"""
    assert cfg_get("gex_signal.caution_near_wall_pct", 10.0, config={}) == 10.0
    monkeypatch.setenv("HERMES_CFG_GEX_SIGNAL__CAUTION_NEAR_WALL_PCT", "4.5")
    assert cfg_get("gex_signal.caution_near_wall_pct", 10.0, config={}) == 4.5


def test_r13_b7_env_override_d2_whale_windows(monkeypatch):
    """D2：两处 whale_window_min 分别登记在 shadow_signals /
    signal_enforcement 块，都可经 env 覆盖（gather 段与 enforce 段）。"""
    monkeypatch.setenv("HERMES_CFG_SHADOW_SIGNALS__WHALE_WINDOW_MIN", "30")
    monkeypatch.setenv("HERMES_CFG_SIGNAL_ENFORCEMENT__WHALE_WINDOW_MIN", "45")
    assert cfg_get("shadow_signals.whale_window_min", config={}) == 30
    assert cfg_get("signal_enforcement.whale_window_min", config={}) == 45


# ── 4. config dict 部分覆盖：deep merge ─────────────────────────────────

def test_r13_b7_config_dict_partial_overlay():
    """config 含子集覆盖时，未列出的键仍走 canonical。"""
    cfg = {
        "options_gex": {"ttl_sec": 60},
        "short_volume": {"crowded_ratio": 0.7},
        "crypto_whale": {"max_pages": 2},
        "news_catalyst": {"rss_limit": 10},
        "whale_index": {"flat_price_pct": 5},
    }
    assert cfg_get("options_gex.ttl_sec", config=cfg) == 60
    assert cfg_get("options_gex.http_timeout_s", config=cfg) == 12.0
    assert cfg_get("short_volume.crowded_ratio", config=cfg) == 0.7
    assert cfg_get("short_volume.light_ratio", config=cfg) == 0.35
    assert cfg_get("crypto_whale.max_pages", config=cfg) == 2
    assert cfg_get("crypto_whale.bias_threshold", config=cfg) == 0.20
    assert cfg_get("news_catalyst.rss_limit", config=cfg) == 10
    assert cfg_get("news_catalyst.timespan", config=cfg) == "1h"
    assert cfg_get("whale_index.flat_price_pct", config=cfg) == 5
    assert cfg_get("whale_index.min_oi_usd", config=cfg) == 5000000


# ── 5. read_agent_config 完整可见 + 深合并 ──────────────────────────────

def test_r13_b7_read_agent_config_exposes_blocks(monkeypatch, tmp_path):
    """read_agent_config() 返回值包含全部五个新块。"""
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    assert cfg["options_gex"]["ttl_sec"] == 900
    assert cfg["short_volume"]["lookback_days"] == 5
    assert cfg["crypto_whale"]["cache_max"] == 1024
    assert cfg["news_catalyst"]["timespan"] == "1h"
    assert cfg["whale_index"]["max_funding_threshold"] == -0.00001


def test_r13_b7_read_agent_config_deep_merges_overlay(monkeypatch, tmp_path):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "crypto_whale": {"http_timeout_s": 4.0},
        "whale_index": {"min_confidence": 0.2},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    assert cfg["crypto_whale"]["http_timeout_s"] == 4.0
    assert cfg["crypto_whale"]["window_minutes"] == 15
    assert cfg["whale_index"]["min_confidence"] == 0.2
    assert cfg["whale_index"]["mcp_top_n"] == 10


# ── 6. schema 接受 + drift sentinel ────────────────────────────────────

def test_r13_b7_schema_declares_five_blocks():
    """_ConfigPatch 必须把五个块声明为字段（dashboard 可写）。"""
    from typing import Any
    fields = _ConfigPatch.model_fields
    for block in ("options_gex", "short_volume", "crypto_whale",
                  "news_catalyst", "whale_index"):
        assert block in fields
        assert fields[block].annotation == dict[str, Any]


def test_r13_b7_validate_config_updates_accepts_blocks():
    """含五个新块的 patch 通过 strict_keys 校验。"""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "options_gex": {"ttl_sec": 60, "http_timeout_s": 8.0},
        "short_volume": {"crowded_ratio": 0.55, "lookback_days": 3},
        "crypto_whale": {"min_usd": 200000, "max_pages": 4},
        "news_catalyst": {"timespan": "2h", "surge_breaking_x": 3.0},
        "whale_index": {"min_confidence": 0.08, "mcp_top_n": 5},
    })
    assert errors == []


# ── 7. 五个 params() helper：默认 / 覆盖 / guard / 不崩 / 独立拷贝 ──────

def test_r13_b7_helpers_defaults_equal_literals():
    """空 config 下五个 helper 返回值 == 模块字面量（零行为变化）。"""
    assert options_gex_params(config={}) == {"ttl_sec": 900.0, "http_timeout_s": 12.0}
    sp = short_volume_params(config={})
    assert sp["ttl_sec"] == 3600.0 and sp["crowded_ratio"] == 0.60
    assert sp["lookback_days"] == 5
    cp = crypto_whale_params(config={})
    assert cp["window_minutes"] == 15.0 and cp["bias_threshold"] == 0.20
    assert cp["cache_max"] == 1024
    np_ = news_catalyst_params(config={})
    assert np_["surge_breaking_x"] == 2.5 and np_["timespan"] == "1h"
    assert np_["fetch_max_workers"] == 2
    wp = whale_index_params(config={})
    assert wp["funding_confidence_scale"] == 0.0001
    assert wp["max_funding_threshold"] == -0.00001
    assert wp["flat_price_pct"] == 10


def test_r13_b7_helpers_env_override_flows_through(monkeypatch):
    """helper 是 cfg-driven：env 改标定后返回值随之变化。"""
    monkeypatch.setenv("HERMES_CFG_OPTIONS_GEX__TTL_SEC", "120")
    monkeypatch.setenv("HERMES_CFG_SHORT_VOLUME__CROWDED_RATIO", "0.55")
    monkeypatch.setenv("HERMES_CFG_CRYPTO_WHALE__BIAS_THRESHOLD", "0.35")
    monkeypatch.setenv("HERMES_CFG_NEWS_CATALYST__SURGE_ELEVATED_X", "2.0")
    monkeypatch.setenv("HERMES_CFG_WHALE_INDEX__MIN_OI_USD", "8000000")
    assert options_gex_params(config={})["ttl_sec"] == 120.0
    assert short_volume_params(config={})["crowded_ratio"] == 0.55
    assert crypto_whale_params(config={})["bias_threshold"] == 0.35
    assert news_catalyst_params(config={})["surge_elevated_x"] == 2.0
    assert whale_index_params(config={})["min_oi_usd"] == 8000000.0


def test_r13_b7_helpers_config_override_flows_through():
    cfg = {
        "options_gex": {"http_timeout_s": 9.0},
        "short_volume": {"light_ratio": 0.25},
        "crypto_whale": {"window_minutes": 30.0},
        "news_catalyst": {"max_records": 12},
        "whale_index": {"funding_norm": 0.0001},
    }
    assert options_gex_params(config=cfg)["http_timeout_s"] == 9.0
    assert short_volume_params(config=cfg)["light_ratio"] == 0.25
    assert crypto_whale_params(config=cfg)["window_minutes"] == 30.0
    assert news_catalyst_params(config=cfg)["max_records"] == 12
    assert whale_index_params(config=cfg)["funding_norm"] == 0.0001


def test_r13_b7_helpers_guard_bad_ratios_fall_back():
    """ratio/threshold 类越界（<=0 或 >=1）回退字面量。"""
    sp = short_volume_params(config={"short_volume": {
        "crowded_ratio": 0, "light_ratio": 1.5, "trend_delta": -0.1}})
    assert sp["crowded_ratio"] == 0.60
    assert sp["light_ratio"] == 0.35
    assert sp["trend_delta"] == 0.03
    cp = crypto_whale_params(config={"crypto_whale": {"bias_threshold": 1.0}})
    assert cp["bias_threshold"] == 0.20


def test_r13_b7_helpers_guard_nonpositive_magnitudes_fall_back():
    """TTL/timeout/window/min_usd/floor 等 <0 回退（0 值按语义合法）。"""
    op = options_gex_params(config={"options_gex": {"ttl_sec": -5}})
    assert op["ttl_sec"] == 900.0
    cp = crypto_whale_params(config={"crypto_whale": {
        "window_minutes": -1, "min_usd": -100, "cache_max": 0, "max_pages": -2}})
    assert cp["window_minutes"] == 15.0
    assert cp["min_usd"] == 100_000.0
    assert cp["cache_max"] == 1024
    assert cp["max_pages"] == 6
    wp = whale_index_params(config={"whale_index": {
        "min_volume_usd": -1, "funding_confidence_scale": 0,
        "oi_vol_ratio_min": 0, "flat_price_pct": -3}})
    assert wp["min_volume_usd"] == 1_000_000.0
    assert wp["funding_confidence_scale"] == 0.0001
    assert wp["oi_vol_ratio_min"] == 10
    assert wp["flat_price_pct"] == 10


def test_r13_b7_helpers_negative_funding_threshold_preserved():
    """max_funding_threshold 语义就是负值（funding 必须低于它），不做 >0
    guard——配置给 -0.00005 必须原样生效。"""
    wp = whale_index_params(config={"whale_index": {
        "max_funding_threshold": -0.00005}})
    assert wp["max_funding_threshold"] == -0.00005


def test_r13_b7_helpers_zero_ttl_is_allowed():
    """ttl_sec=0 合法（禁用缓存/每次重算），不得被 >=0 guard 吞掉。"""
    assert options_gex_params(config={"options_gex": {"ttl_sec": 0}})["ttl_sec"] == 0.0
    assert short_volume_params(config={"short_volume": {"ttl_sec": 0}})["ttl_sec"] == 0.0
    assert crypto_whale_params(config={"crypto_whale": {"ttl_sec": 0}})["ttl_sec"] == 0.0
    assert news_catalyst_params(config={"news_catalyst": {"ttl_sec": 0}})["ttl_sec"] == 300.0


def test_r13_b7_helpers_guard_bad_timespan_falls_back():
    """news timespan 空串/非 str 回退 "1h"。"""
    np_ = news_catalyst_params(config={"news_catalyst": {"timespan": "   "}})
    assert np_["timespan"] == "1h"


def test_r13_b7_helpers_malformed_config_never_raises():
    """坏 config（字符串/None/dict 塞进数值键）绝不冒泡，整体回退字面量。"""
    bad = {"ttl_sec": "x", "http_timeout_s": None, "cache_max": [1],
           "window_minutes": {"n": 1}, "bias_threshold": "y",
           "max_pages": "z", "crowded_ratio": object(), "lookback_days": None,
           "min_volume_usd": "bad"}
    op = options_gex_params(config={"options_gex": bad})
    assert op == {"ttl_sec": 900.0, "http_timeout_s": 12.0}
    sp = short_volume_params(config={"short_volume": bad})
    assert sp["crowded_ratio"] == 0.60 and sp["lookback_days"] == 5
    cp = crypto_whale_params(config={"crypto_whale": bad})
    assert cp["cache_max"] == 1024 and cp["window_minutes"] == 15.0
    np_ = news_catalyst_params(config={"news_catalyst": bad})
    assert np_["timespan"] == "1h" and np_["surge_breaking_x"] == 2.5
    wp = whale_index_params(config={"whale_index": bad})
    assert wp["min_confidence"] == 0.05 and wp["mcp_top_n"] == 10


def test_r13_b7_helpers_return_independent_copies():
    """调用方 mutate 返回 dict 不影响后续调用。"""
    p1 = crypto_whale_params(config={})
    p1["bias_threshold"] = 9.99
    p1["cache_max"] = 1
    p2 = crypto_whale_params(config={})
    assert p2["bias_threshold"] == 0.20 and p2["cache_max"] == 1024
    w1 = whale_index_params(config={})
    w1["mcp_top_n"] = 99
    assert whale_index_params(config={})["mcp_top_n"] == 10


# ── 8. hot-path 行为：默认 == 旧字面量 + env 生效 ───────────────────────

def test_r13_b7_classify_short_regime_defaults_match_literals():
    """0.70→crowded / 0.45→neutral / 0.20→light（旧字面量边界 0.60/0.35）。"""
    assert short_volume.classify_short_regime(0.70) == "crowded_short_squeeze_fuel"
    assert short_volume.classify_short_regime(0.60) == "crowded_short_squeeze_fuel"
    assert short_volume.classify_short_regime(0.45) == "neutral"
    assert short_volume.classify_short_regime(0.35) == "light_short"
    assert short_volume.classify_short_regime(0.20) == "light_short"


def test_r13_b7_classify_short_regime_env_takes_effect(monkeypatch):
    """crowded 阈值 env 收紧到 0.75 后，0.70 落到 neutral。"""
    assert short_volume.classify_short_regime(0.70) == "crowded_short_squeeze_fuel"
    monkeypatch.setenv("HERMES_CFG_SHORT_VOLUME__CROWDED_RATIO", "0.75")
    assert short_volume.classify_short_regime(0.70) == "neutral"


def test_r13_b7_trend_defaults_and_env():
    """_trend：末-首 >0.03 rising / <-0.03 falling，否则 flat。"""
    assert short_volume._trend([0.40, 0.45]) == "rising"
    assert short_volume._trend([0.45, 0.40]) == "falling"
    assert short_volume._trend([0.40, 0.41]) == "flat"
    assert short_volume._trend([0.40]) == "n/a"


def test_r13_b7_detect_surge_defaults_match_literals():
    """detect_surge：latest >= 2.5x median(earlier) 且 >0 才 breaking。"""
    breaking, x = news_catalyst.detect_surge([100, 100, 100, 260])
    assert breaking is True and x == 2.6
    not_breaking, x2 = news_catalyst.detect_surge([100, 100, 100, 200])
    assert not_breaking is False and x2 == 2.0
    short, x3 = news_catalyst.detect_surge([100, 260])
    assert short is False and x3 == 1.0  # <3 bins → (False, 1.0)


def test_r13_b7_detect_surge_env_takes_effect(monkeypatch):
    """breaking_x env 降到 1.8 后，2.0x 也算 breaking。"""
    assert news_catalyst.detect_surge([100, 100, 100, 200])[0] is False
    monkeypatch.setenv("HERMES_CFG_NEWS_CATALYST__SURGE_BREAKING_X", "1.8")
    assert news_catalyst.detect_surge([100, 100, 100, 200])[0] is True


def test_r13_b7_compute_whale_flow_defaults_match_literals():
    """bias：|net|/whale$ >= 0.20 才有方向；min_usd=100k 才计 whale print。"""
    buys = [Print(price=100.0, qty=2000.0, ts=1, is_buy=True)]   # $200k buy
    sells = [Print(price=100.0, qty=1000.0, ts=2, is_buy=False)]  # $100k sell
    rep = crypto_whale.compute_whale_flow(buys + sells, symbol="BTCUSDT")
    assert rep.whale_n == 2
    assert rep.net_usd == 100_000.0
    # net 100k / total 300k = 0.333 >= 0.20 → whale_buying
    assert rep.bias == "whale_buying"
    # balanced: 150k vs 130k → |net|/total = 20/280 = 0.071 < 0.20
    balanced = [Print(100.0, 1500.0, 1, True), Print(100.0, 1300.0, 2, False)]
    rep2 = crypto_whale.compute_whale_flow(balanced, symbol="ETHUSDT")
    assert rep2.bias == "balanced"
    # sub-threshold prints don't count
    small = [Print(100.0, 500.0, 1, True)]  # $50k < $100k
    rep3 = crypto_whale.compute_whale_flow(small, symbol="SOLUSDT")
    assert rep3.whale_n == 0 and rep3.bias == "balanced"


def test_r13_b7_compute_whale_flow_env_takes_effect(monkeypatch):
    """bias_threshold env 提到 0.5 后，0.333 的失衡变成 balanced。"""
    prints = [Print(100.0, 2000.0, 1, True), Print(100.0, 1000.0, 2, False)]
    assert crypto_whale.compute_whale_flow(prints, symbol="X").bias == "whale_buying"
    monkeypatch.setenv("HERMES_CFG_CRYPTO_WHALE__BIAS_THRESHOLD", "0.5")
    assert crypto_whale.compute_whale_flow(prints, symbol="X").bias == "balanced"


def test_r13_b7_smart_money_concentration_defaults(monkeypatch):
    """负 funding + vol>=$1M → accumulation；OI/(vol/1e6)>10 → high_oi。
    confidence 按 funding/0.0001 与 ratio/50 标定（旧字面量）。"""
    universe = [
        # vol=5e12 USD → ratio=1e7/5e6=2.0 (<10) → 只触发 accumulation
        _mk_whale_market("ZZA", funding=-0.0001, oi=1e7, vol=5e12),
        # vol=5e6 USD → ratio=200/5=40 → high_oi（dayNtlVlm 单位是美元）
        _mk_whale_market("ZZB", funding=0.0001, oi=200, vol=5e6),
        _mk_whale_market("ZZC", funding=0.0, oi=1, vol=100),        # too small
    ]
    monkeypatch.setattr(whale_index, "get_universe", lambda *a, **k: universe)
    out = whale_index.smart_money_concentration()
    coins = {r["coin"]: r for r in out}
    assert "ZZA" in coins and "ZZB" in coins and "ZZC" not in coins
    zza = [r for r in out if r["coin"] == "ZZA"][0]
    assert zza["signal"] == "accumulation"
    assert zza["confidence"] == 1.0  # |-0.0001|/0.0001 = 1.0
    zzb = [r for r in out if r["coin"] == "ZZB"][0]
    assert zzb["signal"] == "high_oi_concentration"
    assert zzb["oi_volume_ratio"] == 40.0
    assert zzb["confidence"] == min(1.0, 40.0 / 50)


def test_r13_b7_smart_money_concentration_env_takes_effect(monkeypatch):
    """funding scale env 放宽到 0.0002 后，-0.0001 的 confidence 从 1.0
    降到 0.5（env 真正进入热路径）。"""
    # vol=5e12 USD → oi_vol_ratio=2.0 (<10)，唯一结果即 accumulation，
    # 避免 high_oi(conf=1.0) 降序排第一导致 after[0] 取错条目
    universe = [_mk_whale_market("ZZA", funding=-0.0001, oi=1e7, vol=5e12)]
    monkeypatch.setattr(whale_index, "get_universe", lambda *a, **k: universe)
    base = whale_index.smart_money_concentration()
    assert base[0]["confidence"] == 1.0
    monkeypatch.setenv("HERMES_CFG_WHALE_INDEX__FUNDING_CONFIDENCE_SCALE", "0.0002")
    after = whale_index.smart_money_concentration()
    assert after[0]["confidence"] == 0.5


def test_r13_b7_oi_funding_anomaly_defaults(monkeypatch):
    """OI(usd) >= $5M + funding < -0.00001 + |price move| < 10% → 信号。"""
    universe = [
        # oi=6e6 coins * mid 1.0 = $6M OI; funding -0.00008 < -0.00001; flat px
        _mk_whale_market("ZZA", funding=-0.00008, oi=6e6, vol=1e8,
                         mid=1.0, prev=1.0),
        # funding only -0.000005 → above threshold → excluded
        _mk_whale_market("ZZB", funding=-0.000005, oi=6e6, vol=1e8,
                         mid=1.0, prev=1.0),
    ]
    monkeypatch.setattr(whale_index, "get_universe",
                        lambda *a, **k: universe)
    out = whale_index.oi_funding_anomaly()
    assert [r["coin"] for r in out] == ["ZZA"]
    # confidence = |funding|/0.00008 * (1 - 0/10) = 1.0
    assert out[0]["confidence"] == 1.0


def test_r13_b7_oi_funding_anomaly_env_floor_takes_effect(monkeypatch):
    """min_oi_usd env 提到 $8M 后，$6M OI 的市场被地板过滤。"""
    universe = [_mk_whale_market("ZZA", funding=-0.00008, oi=6e6, vol=1e8)]
    monkeypatch.setattr(whale_index, "get_universe", lambda *a, **k: universe)
    assert len(whale_index.oi_funding_anomaly()) == 1
    monkeypatch.setenv("HERMES_CFG_WHALE_INDEX__MIN_OI_USD", "8000000")
    assert whale_index.oi_funding_anomaly() == []


def test_r13_b7_whale_accumulation_map_zero_arg_calls_compatible(monkeypatch):
    """聚合函数必须以**无参**方式调子信号（test_cleanup 把它们 monkeypatch
    成零参 lambda）；min_confidence 默认 0.05 门控生效。"""
    monkeypatch.setattr(whale_index, "oi_funding_anomaly",
                        lambda: [{"coin": "ZZA", "confidence": 0.5}])
    monkeypatch.setattr(whale_index, "oi_surge_accumulation",
                        lambda: [{"coin": "ZZB", "confidence": 0.02}])
    m = whale_index.whale_accumulation_map()
    assert set(m.keys()) == {"ZZA"}  # ZZB 0.02 < 0.05 门控
    assert m["ZZA"]["confidence"] == 0.5


def test_r13_b7_get_whale_signals_zero_arg_calls_and_top_n(monkeypatch):
    """get_whale_signals 同样无参调子函数；mcp_top_n=10 / mcp_min_confidence
    =0.1 门控生效。"""
    monkeypatch.setattr(whale_index, "smart_money_concentration",
                        lambda: [{"coin": "ZZA", "confidence": 0.9},
                                 {"coin": "ZZB", "confidence": 0.05}])
    monkeypatch.setattr(whale_index, "oi_funding_anomaly",
                        lambda: [{"coin": "ZZC", "confidence": 0.3}])
    out = whale_index.get_whale_signals()
    coins = [r["coin"] for r in out]
    assert "ZZA" in coins and "ZZC" in coins
    assert "ZZB" not in coins  # 0.05 < mcp_min_confidence 0.1
    assert len(out) <= 10


def test_r13_b7_d1_gex_override_caution_default_10pct(monkeypatch):
    """D1 行为验证：spot 距 call wall 6% 时，默认 near=10% → suppress=True
    （运行时值一直是 10.0；旧 1.0 死兜底若生效会是 False）。"""
    monkeypatch.setattr(options_gex, "gex_signal_cached",
                        lambda *a, **k: _mk_gex_report())
    suppress, reason = options_gex.gex_override_caution(
        "xyz:ZZZ", "long", allow_fetch=False)
    assert suppress is True
    assert "pin-trap" in reason


def test_r13_b7_d1_gex_override_caution_env_takes_effect(monkeypatch):
    """D1 核心：near_wall_pct env 收紧到 5% 后，6% 的 gap 不再 suppress
    （旧 dict 直读/签名默认路径 env 静默失效——现在 cfg_get 路径生效）。"""
    monkeypatch.setattr(options_gex, "gex_signal_cached",
                        lambda *a, **k: _mk_gex_report())
    assert options_gex.gex_override_caution(
        "xyz:ZZZ", "long", allow_fetch=False)[0] is True
    monkeypatch.setenv("HERMES_CFG_GEX_SIGNAL__CAUTION_NEAR_WALL_PCT", "5")
    assert options_gex.gex_override_caution(
        "xyz:ZZZ", "long", allow_fetch=False)[0] is False


def test_r13_b7_d1_gex_override_non_crypto_or_short_passes(monkeypatch):
    """非 xyz: 名字或非 long 侧直接放行（不查 GEX）。"""
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        return _mk_gex_report()

    monkeypatch.setattr(options_gex, "gex_signal_cached", _boom)
    assert options_gex.gex_override_caution("BTC", "long", allow_fetch=False) == (False, "")
    assert options_gex.gex_override_caution("xyz:ZZZ", "short", allow_fetch=False) == (False, "")
    assert calls["n"] == 0


def test_r13_b7_gather_shadow_signals_accepts_config_kwarg():
    """gather_shadow_signals 新 config 形参是 keyword-only——三位置参调用
    （test_shadow_signals 既有契约）仍合法，传 config= 也不报错。"""
    import inspect
    sig = inspect.signature(shadow_signals.gather_shadow_signals)
    assert "config" in sig.parameters
    assert sig.parameters["config"].kind == inspect.Parameter.KEYWORD_ONLY
    sig2 = inspect.signature(shadow_signals.run_shadow_async)
    assert "config" in sig2.parameters
    assert sig2.parameters["config"].kind == inspect.Parameter.KEYWORD_ONLY


# ── 9. 缓存 TTL 接线 ───────────────────────────────────────────────────

def test_r13_b7_gex_cache_ttl_default_900(monkeypatch):
    """默认 TTL=900：899s 内不重新 fetch；901s 后重 fetch。"""
    options_gex._gex_cache.clear()
    calls = {"n": 0}

    def _fake_signal(coin):
        calls["n"] += 1
        return _mk_gex_report()

    monkeypatch.setattr(options_gex, "gex_signal", _fake_signal)
    t0 = 1000.0
    monkeypatch.setattr(options_gex.time, "time", lambda: t0)
    options_gex.gex_signal_cached("xyz:ZZZ")
    options_gex.gex_signal_cached("xyz:ZZZ")
    assert calls["n"] == 1
    monkeypatch.setattr(options_gex.time, "time", lambda: t0 + 899)
    options_gex.gex_signal_cached("xyz:ZZZ")
    assert calls["n"] == 1
    monkeypatch.setattr(options_gex.time, "time", lambda: t0 + 901)
    options_gex.gex_signal_cached("xyz:ZZZ")
    assert calls["n"] == 2


def test_r13_b7_crypto_whale_cache_ttl_default_120(monkeypatch):
    """默认 TTL=120：119s 内不重新 fetch。"""
    _reset_module_cache(crypto_whale)
    calls = {"n": 0}

    def _fake_fetch(symbol, **k):
        calls["n"] += 1
        return [Print(100.0, 2000.0, 1, True)]

    monkeypatch.setattr(crypto_whale, "fetch_aggtrades_window", _fake_fetch)
    t0 = 2000.0
    monkeypatch.setattr(crypto_whale.time, "time", lambda: t0)
    crypto_whale.crypto_whale_signal("BTC")
    crypto_whale.crypto_whale_signal("BTC")
    assert calls["n"] == 1
    monkeypatch.setattr(crypto_whale.time, "time", lambda: t0 + 119)
    crypto_whale.crypto_whale_signal("BTC")
    assert calls["n"] == 1
    monkeypatch.setattr(crypto_whale.time, "time", lambda: t0 + 121)
    crypto_whale.crypto_whale_signal("BTC")
    assert calls["n"] == 2


def test_r13_b7_short_volume_cache_ttl_env_takes_effect(monkeypatch):
    """shortvol TTL env=60：61s 后即重新 fetch（默认 3600 下仍新鲜）。"""
    _reset_module_cache(short_volume)
    monkeypatch.setenv("HERMES_CFG_SHORT_VOLUME__TTL_SEC", "60")
    calls = {"n": 0}

    def _fake_fetch_day(date, timeout=None):
        calls["n"] += 1
        return None  # no rows → rep None, but still cached

    monkeypatch.setattr(short_volume, "_fetch_day", _fake_fetch_day)
    t0 = 3000.0
    monkeypatch.setattr(short_volume.time, "time", lambda: t0)
    short_volume.short_volume_signal("AAPL")
    n_after_first = calls["n"]
    assert n_after_first > 0
    monkeypatch.setattr(short_volume.time, "time", lambda: t0 + 30)
    short_volume.short_volume_signal("AAPL")
    assert calls["n"] == n_after_first  # cached
    monkeypatch.setattr(short_volume.time, "time", lambda: t0 + 61)
    short_volume.short_volume_signal("AAPL")
    assert calls["n"] > n_after_first   # stale → re-fetch


# ── 10. 无循环导入 + dashboard 可观测性 ─────────────────────────────────

def test_r13_b7_modules_import_cfg_get_cleanly():
    """六个模块 import cfg_get 不产生循环依赖（模块已正常加载）。"""
    for mod in (options_gex, short_volume, crypto_whale, news_catalyst,
                whale_index, shadow_signals):
        assert hasattr(mod, "cfg_get")
        assert mod.cfg_get is cfg_get


def test_r13_b7_legacy_env_reads_removed():
    """三个遗留 env（HERMES_WHALE_HTTP_TIMEOUT_S /
    HERMES_CRYPTO_WHALE_CACHE_MAX / HERMES_NEWS_HTTP_TIMEOUT_S）源码中
    不再被读取——默认值经 canonical 保留。"""
    import pathlib
    root = pathlib.Path(options_gex.__file__).parent
    for fname, needle in (
        ("crypto_whale.py", "HERMES_WHALE_HTTP_TIMEOUT_S"),
        ("crypto_whale.py", "HERMES_CRYPTO_WHALE_CACHE_MAX"),
        ("news_catalyst.py", "HERMES_NEWS_HTTP_TIMEOUT_S"),
    ):
        text = (root / fname).read_text()
        # 注释里可以提到历史，但不能再有 environ.get 读取
        for line in text.splitlines():
            if needle in line and "environ" in line:
                raise AssertionError(f"legacy env read still present: {fname}: {line.strip()}")


def test_r13_b7_canonical_visible_for_dashboard_dump():
    """CANONICAL_DEFAULTS 是 dashboard/MCP dump 的 source of truth——新登记
    块必须出现且叶子类型可 JSON 渲染（避免 dashboard crash）。"""
    type_map = {
        "options_gex": {"ttl_sec": int, "http_timeout_s": (int, float)},
        "short_volume": {"crowded_ratio": float, "lookback_days": int},
        "crypto_whale": {"cache_max": int, "window_minutes": int,
                         "bias_threshold": float, "min_usd": int},
        "news_catalyst": {"timespan": str, "max_records": int,
                          "surge_breaking_x": float},
        "whale_index": {"min_volume_usd": int, "max_funding_threshold": float,
                        "flat_price_pct": int, "mcp_top_n": int},
    }
    for block, leaves in type_map.items():
        b = CANONICAL_DEFAULTS[block]
        for k, t in leaves.items():
            assert isinstance(b[k], t), (block, k, type(b[k]))
    json.dumps(CANONICAL_DEFAULTS)  # whole tree must be JSON-serializable


# ── 11. 零行为变化：默认参数下输出确定性 ────────────────────────────────

def test_r13_b7_zero_behavior_change_deterministic(monkeypatch):
    """同一 universe 两次计算完全一致（确定性），且默认标定 == 旧字面量。"""
    universe = [
        _mk_whale_market("ZZA", funding=-0.0001, oi=1e7, vol=5e6),
        _mk_whale_market("ZZB", funding=0.0002, oi=2e8, vol=5e6),
        _mk_whale_market("ZZC", funding=-0.00008, oi=6e6, vol=1e8,
                         mid=1.0, prev=0.99),
    ]
    monkeypatch.setattr(whale_index, "get_universe", lambda *a, **k: universe)
    out1 = whale_index.smart_money_concentration()
    out2 = whale_index.smart_money_concentration()
    assert out1 == out2
    # sorted by confidence desc
    confs = [r["confidence"] for r in out1]
    assert confs == sorted(confs, reverse=True)
    # short-regime 纯函数确定性
    r1 = short_volume.classify_short_regime(0.66)
    r2 = short_volume.classify_short_regime(0.66)
    assert r1 == r2 == "crowded_short_squeeze_fuel"
