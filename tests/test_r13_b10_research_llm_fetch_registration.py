"""R13-B10: research 模块 LLM 调用 / 并发预取旋钮 canonical 登记测试。

修复前，research.py 的二十个旋钮散落在热路径里：

  * LLM 调用（_call_openrouter / _debate_direct）——gateway 模型名与 base URL
    经 ``os.environ.get("OPENROUTER_MODEL" / "OPENROUTER_BASE_URL", ...)`` 读取
    （OPENROUTER_API_KEY 是密钥，刻意保持裸 env 不登记），其余九个全是局部
    字面量：temperature 0.1、max_tokens 500、debate max_tokens 350、读超时
    60s、connect 超时 5s、429/5xx 重试 2 次、退避基数 1.0s / 上限 15s、
    finish_reason=length 续写 2 轮；
  * 并发 / 预取（_get_pool / _http / _signals_block / _parallel_prefetch）——
    线程池宽度经 HERMES_RESEARCH_POOL_WORKERS 读取（import-time bootstrap
    16），httpx 连接池上限是 8/16 字面量，signals 块 future 超时经
    HERMES_RESEARCH_SIGNALS_TIMEOUT_S（40）读取，预取天花板经
    HERMES_RESEARCH_FETCH_TIMEOUT_S（45）与四个 per-source
    HERMES_RESEARCH_FETCH_TIMEOUT_{CANDLES,FUNDING,NEWS,SIGNALS}
    （15/8/10/12）读取。

它们既不进 CANONICAL_DEFAULTS（dashboard dump 看不见、validate_config
不校验），也没有 agent-config / HERMES_CFG_ 通道。

修复后（零行为变化）：
  * 新增 canonical 块 ``research_llm``（11 叶）与 ``research_fetch``
    （9 叶），默认值逐字镜像旧字面量；
  * research.py 新增 ``research_llm_params()`` / ``research_fetch_params()``
    helper（共享 ``_research_params``）：逐叶解析链 = legacy env
    （OPENROUTER_MODEL / OPENROUTER_BASE_URL / HERMES_RESEARCH_*，**最高优先**，
    tests/test_research_prefetch.py L36 直设 HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING
    必须继续生效）→ cfg_get("research_llm|research_fetch.<leaf>")
    （HERMES_CFG_* env + agent-config + CANONICAL_DEFAULTS）→ 字面量；
    字符串叶 str() 透传（空串跳过），int/float coerce 且须过最小 guard；
    任何失败整块回退字面量独立拷贝，热路径绝不抛错；
  * _get_pool / _http 单例工厂在构造时各读一次 pool_workers / 连接池上限
    （单例只建一次，config 改动下次进程生效）；_call_openrouter 的
    timeout/max_tokens 形参改 Optional[None]，None 时从 helper 解析
    （call_ai 调用方行为不变；debate 显式传参不受影响）。

明确排除：OPENROUTER_API_KEY（密钥裸 env）、debate timeout 派生 clamp/系数
（_debate_per_call_timeout / _debate_synth_timeout 内部公式）、K 线根数 /
prompt 字符截断（prompt 形状）。
"""

import inspect
import json
import os

import hermes_trader.agents.research as research_mod
from hermes_trader.agents import config_store
from hermes_trader.agents.config_schema import _ConfigPatch, validate_config_updates
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get, read_agent_config
from hermes_trader.agents.research import (
    _RESEARCH_FETCH_DEFAULTS,
    _RESEARCH_FETCH_SPEC,
    _RESEARCH_LLM_DEFAULTS,
    _RESEARCH_LLM_SPEC,
    research_fetch_params,
    research_llm_params,
)

LLM_BLOCK = "research_llm"
FETCH_BLOCK = "research_fetch"
LLM_LEAVES = (
    "model", "base_url", "temperature", "max_tokens", "debate_max_tokens",
    "timeout_sec", "connect_timeout_sec", "retries", "backoff_base_sec",
    "backoff_cap_sec", "continuations",
)
FETCH_LEAVES = (
    "pool_workers", "max_connections", "max_keepalive_connections",
    "signals_timeout_sec", "fetch_timeout_default_sec",
    "fetch_timeout_candles_sec", "fetch_timeout_funding_sec",
    "fetch_timeout_news_sec", "fetch_timeout_signals_sec",
)

# 两块的全部 legacy env（helper 默认值测试前需清空，防宿主环境污染）。
_LEGACY_ENVS = (
    "OPENROUTER_MODEL", "OPENROUTER_BASE_URL",
    "HERMES_RESEARCH_POOL_WORKERS", "HERMES_RESEARCH_SIGNALS_TIMEOUT_S",
    "HERMES_RESEARCH_FETCH_TIMEOUT_S", "HERMES_RESEARCH_FETCH_TIMEOUT_CANDLES",
    "HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING", "HERMES_RESEARCH_FETCH_TIMEOUT_NEWS",
    "HERMES_RESEARCH_FETCH_TIMEOUT_SIGNALS",
)


def _clear_research_env(monkeypatch):
    """清空两块全部 legacy env + HERMES_CFG_RESEARCH_* canonical env。"""
    for v in _LEGACY_ENVS:
        monkeypatch.delenv(v, raising=False)
    for k in list(os.environ):
        if k.startswith("HERMES_CFG_RESEARCH_LLM__") or \
           k.startswith("HERMES_CFG_RESEARCH_FETCH__"):
            monkeypatch.delenv(k, raising=False)


# ── canonical 登记：两块存在、叶数正确、与 research 字面量表逐字一致 ────────

def test_r13_b10_research_llm_block_registered():
    assert LLM_BLOCK in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS[LLM_BLOCK], dict)
    assert len(CANONICAL_DEFAULTS[LLM_BLOCK]) == 11
    assert set(CANONICAL_DEFAULTS[LLM_BLOCK]) == set(LLM_LEAVES)


def test_r13_b10_research_fetch_block_registered():
    assert FETCH_BLOCK in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS[FETCH_BLOCK], dict)
    assert len(CANONICAL_DEFAULTS[FETCH_BLOCK]) == 9
    assert set(CANONICAL_DEFAULTS[FETCH_BLOCK]) == set(FETCH_LEAVES)


def test_r13_b10_canonical_llm_defaults_mirror_research_literals():
    assert CANONICAL_DEFAULTS[LLM_BLOCK] == _RESEARCH_LLM_DEFAULTS


def test_r13_b10_canonical_fetch_defaults_mirror_research_literals():
    # pool_workers 字面量表引用 import-time bootstrap（_POOL_WORKERS），
    # canonical 登记固定为 16；逐叶比对，pool_workers 单独锁 16。
    cb = CANONICAL_DEFAULTS[FETCH_BLOCK]
    for leaf in FETCH_LEAVES:
        if leaf == "pool_workers":
            assert cb[leaf] == 16
        else:
            assert cb[leaf] == _RESEARCH_FETCH_DEFAULTS[leaf], leaf


def test_r13_b10_individual_llm_leaf_values_sentinel():
    """逐叶 sentinel：锁死 11 个 LLM 默认值（零行为变化基线）。"""
    b = CANONICAL_DEFAULTS[LLM_BLOCK]
    assert b["model"] == "deepseek-v4-flash"
    assert b["base_url"] == "https://openrouter.ai/api/v1"
    assert b["temperature"] == 0.1
    assert b["max_tokens"] == 500
    assert b["debate_max_tokens"] == 350
    assert b["timeout_sec"] == 60.0
    assert b["connect_timeout_sec"] == 5.0
    assert b["retries"] == 2
    assert b["backoff_base_sec"] == 1.0
    assert b["backoff_cap_sec"] == 15.0
    assert b["continuations"] == 2


def test_r13_b10_individual_fetch_leaf_values_sentinel():
    """逐叶 sentinel：锁死 9 个并发/预取默认值。"""
    b = CANONICAL_DEFAULTS[FETCH_BLOCK]
    assert b["pool_workers"] == 16
    assert b["max_connections"] == 16
    assert b["max_keepalive_connections"] == 8
    assert b["signals_timeout_sec"] == 40.0
    assert b["fetch_timeout_default_sec"] == 45.0
    assert b["fetch_timeout_candles_sec"] == 15.0
    assert b["fetch_timeout_funding_sec"] == 8.0
    assert b["fetch_timeout_news_sec"] == 10.0
    assert b["fetch_timeout_signals_sec"] == 12.0


def test_r13_b10_leaf_types_match_literals():
    """类型 sentinel：str 槽位保持 str、int 保持 int、float 保持 float。"""
    lb = CANONICAL_DEFAULTS[LLM_BLOCK]
    assert isinstance(lb["model"], str) and isinstance(lb["base_url"], str)
    for leaf in ("max_tokens", "debate_max_tokens", "retries", "continuations"):
        assert isinstance(lb[leaf], int), leaf
    for leaf in ("temperature", "timeout_sec", "connect_timeout_sec",
                 "backoff_base_sec", "backoff_cap_sec"):
        assert isinstance(lb[leaf], float), leaf
    fb = CANONICAL_DEFAULTS[FETCH_BLOCK]
    for leaf in ("pool_workers", "max_connections", "max_keepalive_connections"):
        assert isinstance(fb[leaf], int), leaf
    for leaf in ("signals_timeout_sec", "fetch_timeout_default_sec",
                 "fetch_timeout_candles_sec", "fetch_timeout_funding_sec",
                 "fetch_timeout_news_sec", "fetch_timeout_signals_sec"):
        assert isinstance(fb[leaf], float), leaf


# ── cfg_get 点路径 / 整块：空 config 回退 canonical ────────────────────────

def test_r13_b10_cfg_get_all_llm_leaves():
    for leaf in LLM_LEAVES:
        assert cfg_get(f"{LLM_BLOCK}.{leaf}", config={}) == _RESEARCH_LLM_DEFAULTS[leaf]


def test_r13_b10_cfg_get_all_fetch_leaves():
    for leaf in FETCH_LEAVES:
        assert cfg_get(f"{FETCH_BLOCK}.{leaf}", config={}) == CANONICAL_DEFAULTS[FETCH_BLOCK][leaf]


def test_r13_b10_cfg_get_full_blocks():
    lb = cfg_get(LLM_BLOCK, config={})
    assert isinstance(lb, dict) and len(lb) == 11
    assert lb["model"] == "deepseek-v4-flash"
    assert lb["retries"] == 2
    fb = cfg_get(FETCH_BLOCK, config={})
    assert isinstance(fb, dict) and len(fb) == 9
    assert fb["pool_workers"] == 16
    assert fb["fetch_timeout_funding_sec"] == 8.0


# ── HERMES_CFG_ canonical env 通道（含 str/int/float coerce）───────────────

def test_r13_b10_cfg_env_override_llm_int(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__RETRIES", "7")
    v = cfg_get(f"{LLM_BLOCK}.retries", config={})
    assert v == 7 and isinstance(v, int)


def test_r13_b10_cfg_env_override_llm_float(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__TEMPERATURE", "0.42")
    v = cfg_get(f"{LLM_BLOCK}.temperature", config={})
    assert v == 0.42 and isinstance(v, float)


def test_r13_b10_cfg_env_override_llm_string(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__MODEL", "some-other-model")
    v = cfg_get(f"{LLM_BLOCK}.model", config={})
    assert v == "some-other-model" and isinstance(v, str)


def test_r13_b10_cfg_env_override_fetch_workers(monkeypatch):
    # max_connections 没有 legacy env，canonical env 是唯一 env 通道
    monkeypatch.setenv("HERMES_CFG_RESEARCH_FETCH__MAX_CONNECTIONS", "32")
    assert cfg_get(f"{FETCH_BLOCK}.max_connections", config={}) == 32


def test_r13_b10_cfg_env_override_fetch_float(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_RESEARCH_FETCH__FETCH_TIMEOUT_NEWS_SEC", "22.5")
    v = cfg_get(f"{FETCH_BLOCK}.fetch_timeout_news_sec", config={})
    assert v == 22.5 and isinstance(v, float)


# ── config dict 部分覆盖：未给叶保留 canonical ─────────────────────────────

def test_r13_b10_config_dict_partial_overlay():
    cfg = {
        LLM_BLOCK: {"max_tokens": 1024, "temperature": 0.3},
        FETCH_BLOCK: {"pool_workers": 4, "fetch_timeout_news_sec": 9.0},
    }
    assert cfg_get(f"{LLM_BLOCK}.max_tokens", config=cfg) == 1024
    assert cfg_get(f"{LLM_BLOCK}.temperature", config=cfg) == 0.3
    assert cfg_get(f"{LLM_BLOCK}.model", config=cfg) == "deepseek-v4-flash"
    assert cfg_get(f"{LLM_BLOCK}.retries", config=cfg) == 2
    assert cfg_get(f"{FETCH_BLOCK}.pool_workers", config=cfg) == 4
    assert cfg_get(f"{FETCH_BLOCK}.fetch_timeout_news_sec", config=cfg) == 9.0
    assert cfg_get(f"{FETCH_BLOCK}.max_connections", config=cfg) == 16
    assert cfg_get(f"{FETCH_BLOCK}.signals_timeout_sec", config=cfg) == 40.0


# ── read_agent_config 可见 + 深合并（dashboard dump 通道）─────────────────

def test_r13_b10_read_agent_config_exposes_blocks():
    cfg = read_agent_config()
    assert LLM_BLOCK in cfg and FETCH_BLOCK in cfg
    assert cfg[LLM_BLOCK]["model"] == "deepseek-v4-flash"
    assert cfg[LLM_BLOCK]["debate_max_tokens"] == 350
    assert cfg[FETCH_BLOCK]["pool_workers"] == 16
    assert cfg[FETCH_BLOCK]["fetch_timeout_funding_sec"] == 8.0


def test_r13_b10_read_agent_config_deep_merges_partial(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        LLM_BLOCK: {"retries": 5, "model": "m"},
        FETCH_BLOCK: {"signals_timeout_sec": 33.0},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg[LLM_BLOCK]["retries"] == 5
    assert cfg[LLM_BLOCK]["model"] == "m"
    assert cfg[LLM_BLOCK]["temperature"] == 0.1            # canonical 保留
    assert cfg[FETCH_BLOCK]["signals_timeout_sec"] == 33.0
    assert cfg[FETCH_BLOCK]["pool_workers"] == 16         # canonical 保留


# ── schema：两块作为 object 被 strict_keys 接受；_ConfigPatch drift sentinel ─

def test_r13_b10_schema_accepts_both_blocks():
    errors = validate_config_updates(
        {
            LLM_BLOCK: {"max_tokens": 800, "retries": 3, "model": "x"},
            FETCH_BLOCK: {"pool_workers": 8, "max_connections": 24},
        },
        strict_keys=True,
    )
    assert not any("unknown key" in e for e in errors), errors
    assert errors == [], errors


def test_r13_b10_config_patch_knows_both_blocks():
    """drift sentinel：CANONICAL_DEFAULTS 加块后 _ConfigPatch 必须同步声明。"""
    fields = _ConfigPatch.model_fields
    assert LLM_BLOCK in fields and FETCH_BLOCK in fields
    lb = fields[LLM_BLOCK].default_factory()
    assert len(lb) == 11 and lb["model"] == "deepseek-v4-flash" and lb["retries"] == 2
    fb = fields[FETCH_BLOCK].default_factory()
    assert len(fb) == 9 and fb["pool_workers"] == 16 and fb["max_keepalive_connections"] == 8


# ── helper：默认叶集 == 字面量、SPEC 映射 / kind / guard 正确 ─────────────

def test_r13_b10_helper_defaults_equal_literals(monkeypatch):
    _clear_research_env(monkeypatch)
    lp = research_llm_params(config={})
    fp = research_fetch_params(config={})
    assert set(lp) == set(LLM_LEAVES)
    assert set(fp) == set(FETCH_LEAVES)
    assert lp == _RESEARCH_LLM_DEFAULTS
    for leaf in FETCH_LEAVES:
        assert fp[leaf] == CANONICAL_DEFAULTS[FETCH_BLOCK][leaf], leaf


def test_r13_b10_spec_maps_legacy_envs():
    """LLM 块 2 个字符串叶映射 OPENROUTER_*；FETCH 块 7 叶映射
    HERMES_RESEARCH_*；其余纯硬编码叶 legacy=None。"""
    assert _RESEARCH_LLM_SPEC["model"][0] == "OPENROUTER_MODEL"
    assert _RESEARCH_LLM_SPEC["base_url"][0] == "OPENROUTER_BASE_URL"
    assert _RESEARCH_FETCH_SPEC["pool_workers"][0] == "HERMES_RESEARCH_POOL_WORKERS"
    assert _RESEARCH_FETCH_SPEC["signals_timeout_sec"][0] == "HERMES_RESEARCH_SIGNALS_TIMEOUT_S"
    assert _RESEARCH_FETCH_SPEC["fetch_timeout_default_sec"][0] == "HERMES_RESEARCH_FETCH_TIMEOUT_S"
    assert _RESEARCH_FETCH_SPEC["fetch_timeout_candles_sec"][0] == "HERMES_RESEARCH_FETCH_TIMEOUT_CANDLES"
    assert _RESEARCH_FETCH_SPEC["fetch_timeout_funding_sec"][0] == "HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING"
    assert _RESEARCH_FETCH_SPEC["fetch_timeout_news_sec"][0] == "HERMES_RESEARCH_FETCH_TIMEOUT_NEWS"
    assert _RESEARCH_FETCH_SPEC["fetch_timeout_signals_sec"][0] == "HERMES_RESEARCH_FETCH_TIMEOUT_SIGNALS"
    for leaf in ("temperature", "max_tokens", "debate_max_tokens", "timeout_sec",
                 "connect_timeout_sec", "retries", "backoff_base_sec",
                 "backoff_cap_sec", "continuations"):
        assert _RESEARCH_LLM_SPEC[leaf][0] is None, leaf
    for leaf in ("max_connections", "max_keepalive_connections"):
        assert _RESEARCH_FETCH_SPEC[leaf][0] is None, leaf


def test_r13_b10_spec_kinds_and_guards():
    """kind 列：model/base_url 为 s；token/重试/续写/池/连接为 i；余为 f。
    guard 列：retries/continuations/backoff 允许 0；token/池/超时须 >= 下限。"""
    for leaf in ("model", "base_url"):
        assert _RESEARCH_LLM_SPEC[leaf][1] == "s", leaf
    for leaf in ("max_tokens", "debate_max_tokens", "retries", "continuations"):
        assert _RESEARCH_LLM_SPEC[leaf][1] == "i", leaf
    for leaf in ("temperature", "timeout_sec", "connect_timeout_sec",
                 "backoff_base_sec", "backoff_cap_sec"):
        assert _RESEARCH_LLM_SPEC[leaf][1] == "f", leaf
    assert _RESEARCH_LLM_SPEC["max_tokens"][2] == 1
    assert _RESEARCH_LLM_SPEC["retries"][2] == 0
    assert _RESEARCH_LLM_SPEC["timeout_sec"][2] == 0.1
    for leaf in ("pool_workers", "max_connections", "max_keepalive_connections"):
        assert _RESEARCH_FETCH_SPEC[leaf][1] == "i", leaf
    for leaf in FETCH_LEAVES[3:]:
        assert _RESEARCH_FETCH_SPEC[leaf][1] == "f", leaf
    assert _RESEARCH_FETCH_SPEC["pool_workers"][2] == 1
    assert _RESEARCH_FETCH_SPEC["signals_timeout_sec"][2] == 0.1


# ── legacy env 兼容（硬约束）：仍生效且优先于 canonical 通道 ──────────────

def test_r13_b10_legacy_openrouter_model_flows(monkeypatch):
    """运维 gateway 路由：OPENROUTER_MODEL 必须继续流向 helper。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
    p = research_llm_params(config={})
    assert p["model"] == "anthropic/claude-3.5-sonnet"


def test_r13_b10_legacy_openrouter_base_url_flows(monkeypatch):
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://gateway.example.com/v1")
    p = research_llm_params(config={})
    assert p["base_url"] == "https://gateway.example.com/v1"


def test_r13_b10_legacy_env_beats_canonical_env(monkeypatch):
    """legacy env 优先级最高：同时设置时压过 HERMES_CFG_*。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_MODEL", "legacy-model")
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__MODEL", "canonical-model")
    monkeypatch.setenv("HERMES_RESEARCH_POOL_WORKERS", "7")
    monkeypatch.setenv("HERMES_CFG_RESEARCH_FETCH__POOL_WORKERS", "55")
    lp = research_llm_params(config={})
    fp = research_fetch_params(config={})
    assert lp["model"] == "legacy-model"
    assert fp["pool_workers"] == 7


def test_r13_b10_legacy_env_empty_string_falls_through(monkeypatch):
    """空串视为未设：落到 canonical 通道而非崩溃 / 透传空串。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_MODEL", "")
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__RETRIES", "13")
    monkeypatch.setenv("HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING", "")
    p = research_llm_params(config={})
    f = research_fetch_params(config={})
    assert p["model"] == "deepseek-v4-flash"
    assert p["retries"] == 13
    assert f["fetch_timeout_funding_sec"] == 8.0


def test_r13_b10_legacy_prefetch_funding_timeout_compat(monkeypatch):
    """tests/test_research_prefetch.py L36 硬约束：设
    HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING=1 让 funding future 1s 快速超时。
    B10 接线后该 env 必须原样生效。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING", "1")
    f = research_fetch_params(config={})
    assert f["fetch_timeout_funding_sec"] == 1.0
    # 其余 per-source 叶不受影响
    assert f["fetch_timeout_candles_sec"] == 15.0
    assert f["fetch_timeout_news_sec"] == 10.0


def test_r13_b10_legacy_signals_and_pool_workers_flow(monkeypatch):
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_RESEARCH_SIGNALS_TIMEOUT_S", "25")
    monkeypatch.setenv("HERMES_RESEARCH_POOL_WORKERS", "11")
    f = research_fetch_params(config={})
    assert f["signals_timeout_sec"] == 25.0
    assert f["pool_workers"] == 11


def test_r13_b10_legacy_fetch_timeout_default_and_sources_flow(monkeypatch):
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_RESEARCH_FETCH_TIMEOUT_S", "30")
    monkeypatch.setenv("HERMES_RESEARCH_FETCH_TIMEOUT_CANDLES", "9")
    monkeypatch.setenv("HERMES_RESEARCH_FETCH_TIMEOUT_NEWS", "4")
    monkeypatch.setenv("HERMES_RESEARCH_FETCH_TIMEOUT_SIGNALS", "6")
    f = research_fetch_params(config={})
    assert f["fetch_timeout_default_sec"] == 30.0
    assert f["fetch_timeout_candles_sec"] == 9.0
    assert f["fetch_timeout_news_sec"] == 4.0
    assert f["fetch_timeout_signals_sec"] == 6.0


# ── canonical env / config dict 经 helper 流向消费方 ──────────────────────

def test_r13_b10_helper_canonical_env_flows(monkeypatch):
    """无 legacy env 干扰时，HERMES_CFG_RESEARCH_* 必须流向 helper。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__MAX_TOKENS", "900")
    monkeypatch.setenv("HERMES_CFG_RESEARCH_FETCH__MAX_KEEPALIVE_CONNECTIONS", "4")
    lp = research_llm_params(config={})
    fp = research_fetch_params(config={})
    assert lp["max_tokens"] == 900
    assert fp["max_keepalive_connections"] == 4


def test_r13_b10_helper_config_dict_override():
    lp = research_llm_params(config={LLM_BLOCK: {"continuations": 4, "backoff_cap_sec": 30.0}})
    fp = research_fetch_params(config={FETCH_BLOCK: {"max_connections": 20, "fetch_timeout_candles_sec": 18.0}})
    assert lp["continuations"] == 4
    assert lp["backoff_cap_sec"] == 30.0
    assert lp["model"] == "deepseek-v4-flash"          # 其余叶保留字面量
    assert fp["max_connections"] == 20
    assert fp["fetch_timeout_candles_sec"] == 18.0
    assert fp["pool_workers"] == 16


# ── guard：坏值 / 越界回退字面量，热路径不崩 ──────────────────────────────

def test_r13_b10_guard_pool_workers_zero_falls_back(monkeypatch):
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_RESEARCH_POOL_WORKERS", "0")
    f = research_fetch_params(config={})
    assert f["pool_workers"] == 16                     # 0 会让 ThreadPoolExecutor 报错


def test_r13_b10_guard_max_tokens_negative_falls_back(monkeypatch):
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__MAX_TOKENS", "-5")
    p = research_llm_params(config={})
    assert p["max_tokens"] == 500


def test_r13_b10_guard_timeout_garbage_returns_full_literals(monkeypatch):
    """coerce 失败：helper 整块回退字面量（不抛异常、不留半坏 dict）。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__TIMEOUT_SEC", "garbage")
    p = research_llm_params(config={})
    assert p == _RESEARCH_LLM_DEFAULTS


def test_r13_b10_guard_zero_retries_and_continuations_are_legal(monkeypatch):
    """retries / continuations 下限 0：0 是合法的"不重试 / 不续写"值。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__RETRIES", "0")
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__CONTINUATIONS", "0")
    p = research_llm_params(config={})
    assert p["retries"] == 0
    assert p["continuations"] == 0


def test_r13_b10_guard_zero_temperature_and_backoff_base_are_legal(monkeypatch):
    """temperature / backoff_base 下限 0.0：0 合法（确定性采样 / 无退避基数）。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__TEMPERATURE", "0")
    monkeypatch.setenv("HERMES_CFG_RESEARCH_LLM__BACKOFF_BASE_SEC", "0")
    p = research_llm_params(config={})
    assert p["temperature"] == 0.0
    assert p["backoff_base_sec"] == 0.0


def test_r13_b10_guard_negative_fetch_timeout_falls_back(monkeypatch):
    """fetch 超时下限 0.1：负值回退字面量（旧代码 per-source env 设 0 会得到
    timeout=0 立即超时；新代码 guard 回退，更安全）。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING", "-3")
    f = research_fetch_params(config={})
    assert f["fetch_timeout_funding_sec"] == 8.0


def test_r13_b10_helper_returns_independent_copy(monkeypatch):
    """每次返回独立 dict，调用方 mutate 不污染字面量表 / 彼此隔离。"""
    _clear_research_env(monkeypatch)
    a = research_llm_params(config={})
    a["max_tokens"] = 999
    a["model"] = "mutated"
    b = research_llm_params(config={})
    assert b["max_tokens"] == 500
    assert b["model"] == "deepseek-v4-flash"
    assert _RESEARCH_LLM_DEFAULTS["max_tokens"] == 500
    fa = research_fetch_params(config={})
    fa["pool_workers"] = 999
    fb = research_fetch_params(config={})
    assert fb["pool_workers"] == 16


# ── 热路径接线：源码断言（helper 注入 + 消费点 + 旧常量/字面量清除）────────

def test_r13_b10_module_exposes_helpers():
    assert hasattr(research_mod, "research_llm_params")
    assert hasattr(research_mod, "research_fetch_params")
    assert research_mod.research_llm_params is research_llm_params
    assert research_mod.research_fetch_params is research_fetch_params
    # import-time bootstrap 保留为 fallback 符号
    assert hasattr(research_mod, "_POOL_WORKERS")


def test_r13_b10_get_pool_resolves_workers_via_helper():
    src = inspect.getsource(research_mod._get_pool)
    assert 'workers = int(research_fetch_params()["pool_workers"])' in src
    assert "max_workers=workers" in src
    assert "max_workers=_POOL_WORKERS" not in src


def test_r13_b10_http_client_resolves_limits_via_helper():
    src = inspect.getsource(research_mod._http)
    assert "fp = research_fetch_params()" in src
    assert 'int(fp["max_keepalive_connections"])' in src
    assert 'int(fp["max_connections"])' in src
    assert "max_keepalive_connections=8" not in src
    assert "max_connections=16" not in src


def test_r13_b10_signals_block_resolves_timeout_via_helper():
    src = inspect.getsource(research_mod._signals_block)
    assert '_sig_timeout = float(research_fetch_params()["signals_timeout_sec"])' in src
    assert 'os.environ.get("HERMES_RESEARCH_SIGNALS_TIMEOUT_S"' not in src


def test_r13_b10_call_openrouter_resolves_llm_knobs_via_helper():
    src = inspect.getsource(research_mod._call_openrouter)
    assert "lp = research_llm_params()" in src
    assert 'model = str(lp["model"])' in src
    assert 'base_url = str(lp["base_url"])' in src
    assert 'timeout = float(lp["timeout_sec"])' in src
    assert 'max_tokens = int(lp["max_tokens"])' in src
    assert 'temperature = float(lp["temperature"])' in src
    assert 'connect_timeout = float(lp["connect_timeout_sec"])' in src
    assert 'max_429_retries = int(lp["retries"])' in src
    assert 'backoff_base_s = float(lp["backoff_base_sec"])' in src
    assert 'backoff_cap_s = float(lp["backoff_cap_sec"])' in src
    assert 'max_length_continuations = int(lp["continuations"])' in src
    # 旧内联 env / 字面量不得残留在函数体内
    assert 'os.environ.get("OPENROUTER_MODEL"' not in src
    assert 'os.environ.get("OPENROUTER_BASE_URL"' not in src
    assert '"temperature": 0.1' not in src
    assert "connect=5.0" not in src
    # 密钥保持裸 env（刻意不进 canonical）
    assert 'os.environ.get("OPENROUTER_API_KEY"' in src


def test_r13_b10_module_no_longer_defines_old_local_constants():
    """四个旧局部常量名必须从模块彻底消失（改名自 lp[] 解析）。"""
    src = inspect.getsource(research_mod)
    assert "_MAX_429_RETRIES" not in src
    assert "_BACKOFF_BASE_S" not in src
    assert "_BACKOFF_CAP_S" not in src
    assert "_MAX_LENGTH_CONTINUATIONS" not in src


def test_r13_b10_debate_direct_resolves_token_budget_via_helper():
    src = inspect.getsource(research_mod._debate_direct)
    assert 'debate_tokens = int(research_llm_params()["debate_max_tokens"])' in src
    assert "max_tokens=debate_tokens" in src
    assert "max_tokens=350" not in src


def test_r13_b10_parallel_prefetch_resolves_ceilings_via_helper():
    src = inspect.getsource(research_mod._parallel_prefetch)
    assert "fp = research_fetch_params()" in src
    assert '_default_timeout = float(fp["fetch_timeout_default_sec"])' in src
    assert '"fetch_timeout_candles_sec"' in src
    assert '"fetch_timeout_funding_sec"' in src
    assert '"fetch_timeout_news_sec"' in src
    assert '"fetch_timeout_signals_sec"' in src
    # 旧的五个内联 env 读取不得残留
    for var in (
        "HERMES_RESEARCH_FETCH_TIMEOUT_S",
        "HERMES_RESEARCH_FETCH_TIMEOUT_CANDLES",
        "HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING",
        "HERMES_RESEARCH_FETCH_TIMEOUT_NEWS",
        "HERMES_RESEARCH_FETCH_TIMEOUT_SIGNALS",
    ):
        assert f'os.environ.get("{var}"' not in src, var


# ── 单例工厂：canonical env 在构造时生效（懒加载，只建一次）──────────────

def test_r13_b10_get_pool_constructs_with_canonical_workers(monkeypatch):
    """池单例首次构造时读 research_fetch.pool_workers；重置 _POOL 后
    canonical env HERMES_RESEARCH_POOL_WORKERS 驱动宽度。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setenv("HERMES_RESEARCH_POOL_WORKERS", "9")
    monkeypatch.setattr(research_mod, "_POOL", None)
    try:
        pool = research_mod._get_pool()
        assert pool._max_workers == 9
    finally:
        pool.shutdown(wait=False)
        monkeypatch.setattr(research_mod, "_POOL", None)


def test_r13_b10_http_client_constructs_with_canonical_limits(monkeypatch):
    """httpx 客户端单例首次构造时经 helper 读连接池上限（不开业务请求，仅建
    client）：构造不抛错、单例生效；上限消费点由源码断言覆盖。"""
    _clear_research_env(monkeypatch)
    monkeypatch.setattr(research_mod, "_HTTP", None)
    try:
        client = research_mod._http()
        assert client is research_mod._HTTP       # 单例缓存生效
        assert not client.is_closed
    finally:
        if research_mod._HTTP is not None:
            research_mod._HTTP.close()
        monkeypatch.setattr(research_mod, "_HTTP", None)
