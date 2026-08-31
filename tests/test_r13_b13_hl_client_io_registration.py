"""R13-B13: Hyperliquid 客户端层数值旋钮 canonical 登记测试。

修复前，client 层（rate_limit / hl_client / ws_client / exchange）的 19 个
数值旋钮散落在四个模块里，全部是 ``os.environ.get(..., 字面量)``：不在
dashboard config dump 里、validate_config 不校验、.agent-config.json 无法
覆盖，其中 per-endpoint gate 开关还是 call-time env 读（import 后切换立即
生效，test_rate_limit_per_endpoint 依赖此契约）。

修复后（零行为变化）：
  * 新增两个 canonical 块——``hl_client_io``（12 叶：SDK timeout、默认杠杆、
    滑点上限、meta/ATR/candle/funding 缓存 TTL、candle 缓存容量、WS staleness
    / heartbeat / seq 回退窗口）与 ``hl_rate_limit``（7 叶：令牌桶 refill /
    capacity / 最大等待 / 429 重试 / opportunistic 等待 / shared 开关 /
    per-endpoint gate 开关，后两叶为 bool）；
  * rate_limit.py（client 导入图最底层叶子，config_store 零 hermes_trader
    导入）托管字面量表 *_DEFAULTS、映射表 *_SPEC（leaf → (legacy env,
    kind "i"/"f"/"b", min guard)）与泛型 helper ``_resolve_hl_block``：
    逐叶解析链 = legacy env（非空串，**最高优先**）→
    cfg_get("block.leaf")（HERMES_CFG_* canonical env + agent-config +
    CANONICAL_DEFAULTS）→ 字面量；按 kind coerce（bool 识别
    1/true/yes/on，跳过数值 guard），数值叶须 >= min（TTL/滑点/timeout/
    retries 类 min=0，0 合法=禁缓存/禁逃生舱/不重试；capacity/cache_max/
    staleness/seq/leverage 类 min=1）；任何异常整块回退字面量独立拷贝；
  * 18 叶在 import 时快照为模块级 ``_HL_CLIENT_IO`` / ``_HL_RATE_LIMIT``
    （旧 env 通道本就要求 boot 前设置，import-time 语义零变化）；
  * **唯一例外**：``rate_per_endpoint_gate`` 保留 call-time env 读——
    ``_per_endpoint_gate_enabled()``：env 非空→env bool；未设/空串→
    回退 import-time 快照。test_gate_disabled_by_env 依赖 import 后
    monkeypatch 立即生效；
  * 消费点全部改读快照符号，旧模块常量名（_SDK_TIMEOUT / HL_LEVERAGE /
    _CANDLE_CACHE_TTL_S / _WS_HEARTBEAT_S …）保留为 fallback 符号。

排重：顶层 ``leverage``（交易配置杠杆，默认 10）与
``hl_client_io.default_leverage``（cross-margin fallback，默认 5）语义与
默认值均不同，永不合并。

排除项：钱包/密钥 env、testnet/backtest 部署开关、
HERMES_HL_RATE_STATE_FILE 路径、MIN_ORDER_USD / post-retry / WS 重连 /
coalesce wait / funding max_size 等纯裸字面量不在本批登记。
"""

import inspect
import json
import os

import pytest

from hermes_trader.agents import config_store
from hermes_trader.agents.config_schema import _ConfigPatch, validate_config_updates
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get, read_agent_config
from hermes_trader.client import exchange as exchange_mod
from hermes_trader.client import hl_client as hl_client_mod
from hermes_trader.client import rate_limit as rl
from hermes_trader.client import ws_client as ws_mod

IO_BLOCK = "hl_client_io"
RL_BLOCK = "hl_rate_limit"

IO_LEAVES = (
    "sdk_timeout_s", "default_leverage", "max_slippage_pct",
    "max_slippage_close_pct", "meta_ttl_s", "atr_ttl_s",
    "candle_cache_ttl_s", "candle_cache_max", "funding_cache_ttl_s",
    "ws_max_stale_s", "ws_heartbeat_s", "ws_seq_max_backward",
    # M-11 (supplemental audit 2026-08-30): per-coin single-tick jump filter.
    "ws_max_tick_jump_frac",
)
RL_LEAVES = (
    "rate_refill_per_sec", "rate_capacity", "rate_max_wait_s",
    "rate_429_retries", "rate_opportunistic_wait_s",
    "rate_shared", "rate_per_endpoint_gate",
)
BOOL_LEAVES = ("rate_shared", "rate_per_endpoint_gate")

_LEGACY_ENVS = (
    "HERMES_HL_SDK_TIMEOUT_S", "HERMES_DEFAULT_LEVERAGE",
    "HERMES_MAX_SLIPPAGE_PCT", "HERMES_MAX_SLIPPAGE_CLOSE_PCT",
    "HERMES_META_TTL_S", "HERMES_ATR_TTL_S",
    "HERMES_CANDLE_CACHE_TTL_S", "HERMES_CANDLE_CACHE_MAX",
    "HERMES_FUNDING_CACHE_TTL_S",
    "HERMES_WS_MAX_STALE_SECONDS", "HERMES_WS_HEARTBEAT_S",
    "HERMES_WS_SEQ_MAX_BACKWARD", "HERMES_WS_MAX_TICK_JUMP_FRAC",
    "HERMES_HL_RATE_REFILL_PER_SEC", "HERMES_HL_RATE_CAPACITY",
    "HERMES_HL_RATE_MAX_WAIT_S", "HERMES_HL_429_RETRIES",
    "HERMES_HL_RATE_OPPORTUNISTIC_WAIT_S", "HERMES_HL_RATE_SHARED",
    "HERMES_HL_RATE_PER_ENDPOINT_GATE",
)


def _clear_b13_env(monkeypatch):
    """清空 19 个 legacy env + HERMES_CFG_HL_CLIENT_IO__* /
    HERMES_CFG_HL_RATE_LIMIT__* canonical env。"""
    for v in _LEGACY_ENVS:
        monkeypatch.delenv(v, raising=False)
    for k in list(os.environ):
        if k.startswith("HERMES_CFG_HL_CLIENT_IO__") or \
           k.startswith("HERMES_CFG_HL_RATE_LIMIT__"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    _clear_b13_env(monkeypatch)
    yield


# ── canonical 登记：两块存在、叶数 / 类型 / 字面量镜像 ────────────────────

def test_r13_b13_blocks_registered():
    assert IO_BLOCK in CANONICAL_DEFAULTS
    assert RL_BLOCK in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS[IO_BLOCK], dict)
    assert isinstance(CANONICAL_DEFAULTS[RL_BLOCK], dict)
    assert len(CANONICAL_DEFAULTS[IO_BLOCK]) == 13
    assert len(CANONICAL_DEFAULTS[RL_BLOCK]) == 7
    assert set(CANONICAL_DEFAULTS[IO_BLOCK]) == set(IO_LEAVES)
    assert set(CANONICAL_DEFAULTS[RL_BLOCK]) == set(RL_LEAVES)


def test_r13_b13_defaults_mirror_helper_literals():
    assert CANONICAL_DEFAULTS[IO_BLOCK] == rl._HL_CLIENT_IO_DEFAULTS
    assert CANONICAL_DEFAULTS[RL_BLOCK] == rl._HL_RATE_LIMIT_DEFAULTS


def test_r13_b13_io_leaf_values_sentinel():
    """逐叶 sentinel：锁死 12 个客户端 I/O 默认值（零行为变化基线）。"""
    b = CANONICAL_DEFAULTS[IO_BLOCK]
    assert b["sdk_timeout_s"] == 30.0
    assert b["default_leverage"] == 5
    assert b["max_slippage_pct"] == 1.5
    assert b["max_slippage_close_pct"] == 5.0
    assert b["meta_ttl_s"] == 3600.0
    assert b["atr_ttl_s"] == 60.0
    assert b["candle_cache_ttl_s"] == 90.0
    assert b["candle_cache_max"] == 512
    assert b["funding_cache_ttl_s"] == 300.0
    assert b["ws_max_stale_s"] == 30
    assert b["ws_heartbeat_s"] == 10.0
    assert b["ws_seq_max_backward"] == 1024


def test_r13_b13_rl_leaf_values_sentinel():
    """逐叶 sentinel：锁死 7 个限流默认值（零行为变化基线）。"""
    b = CANONICAL_DEFAULTS[RL_BLOCK]
    assert b["rate_refill_per_sec"] == 20.0
    assert b["rate_capacity"] == 600
    assert b["rate_max_wait_s"] == 30.0
    assert b["rate_429_retries"] == 2
    assert b["rate_opportunistic_wait_s"] == 2.0
    assert b["rate_shared"] is True
    assert b["rate_per_endpoint_gate"] is True


def test_r13_b13_leaf_types():
    b_io = CANONICAL_DEFAULTS[IO_BLOCK]
    b_rl = CANONICAL_DEFAULTS[RL_BLOCK]
    # int 叶
    for leaf in ("default_leverage", "candle_cache_max", "ws_max_stale_s",
                 "ws_seq_max_backward"):
        assert isinstance(b_io[leaf], int) and not isinstance(b_io[leaf], bool), leaf
    for leaf in ("rate_capacity", "rate_429_retries"):
        assert isinstance(b_rl[leaf], int) and not isinstance(b_rl[leaf], bool), leaf
    # float 叶
    for leaf in ("sdk_timeout_s", "max_slippage_pct", "max_slippage_close_pct",
                 "meta_ttl_s", "atr_ttl_s", "candle_cache_ttl_s",
                 "funding_cache_ttl_s", "ws_heartbeat_s"):
        assert isinstance(b_io[leaf], float), leaf
    for leaf in ("rate_refill_per_sec", "rate_max_wait_s",
                 "rate_opportunistic_wait_s"):
        assert isinstance(b_rl[leaf], float), leaf
    # bool 叶
    for leaf in BOOL_LEAVES:
        assert b_rl[leaf] is True, leaf


def test_r13_b13_default_leverage_distinct_from_top_level():
    """排重 sentinel：hl_client_io.default_leverage(5, cross-margin fallback)
    与顶层 leverage(10, 交易配置) 是两个独立旋钮，永不合并。"""
    top = cfg_get("leverage", config={})
    assert CANONICAL_DEFAULTS[IO_BLOCK]["default_leverage"] == 5
    assert top != 5


# ── cfg_get 点路径 / 整块：空 config 回退 canonical ───────────────────────

def test_r13_b13_cfg_get_all_io_leaves():
    for leaf in IO_LEAVES:
        assert cfg_get(f"{IO_BLOCK}.{leaf}", config={}) == \
            rl._HL_CLIENT_IO_DEFAULTS[leaf]


def test_r13_b13_cfg_get_all_rl_leaves():
    for leaf in RL_LEAVES:
        assert cfg_get(f"{RL_BLOCK}.{leaf}", config={}) == \
            rl._HL_RATE_LIMIT_DEFAULTS[leaf]


def test_r13_b13_cfg_get_full_blocks():
    b_io = cfg_get(IO_BLOCK, config={})
    b_rl = cfg_get(RL_BLOCK, config={})
    assert isinstance(b_io, dict) and len(b_io) == 13
    assert isinstance(b_rl, dict) and len(b_rl) == 7
    assert b_io["sdk_timeout_s"] == 30.0 and b_io["ws_seq_max_backward"] == 1024
    assert b_rl["rate_capacity"] == 600 and b_rl["rate_shared"] is True


# ── HERMES_CFG_ canonical env 通道（int/float/bool coerce）────────────────

def test_r13_b13_cfg_env_float_coerce(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_HL_CLIENT_IO__SDK_TIMEOUT_S", "42.5")
    v = cfg_get(f"{IO_BLOCK}.sdk_timeout_s", config={})
    assert v == 42.5 and isinstance(v, float)


def test_r13_b13_cfg_env_int_coerce(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_HL_RATE_LIMIT__RATE_CAPACITY", "321")
    v = cfg_get(f"{RL_BLOCK}.rate_capacity", config={})
    assert v == 321 and isinstance(v, int)


def test_r13_b13_cfg_env_bool_coerce(monkeypatch):
    """bool 叶经 canonical env：false/0/off → False（config_store 原生 coerce）。"""
    monkeypatch.setenv("HERMES_CFG_HL_RATE_LIMIT__RATE_SHARED", "false")
    assert cfg_get(f"{RL_BLOCK}.rate_shared", config={}) is False
    monkeypatch.setenv("HERMES_CFG_HL_RATE_LIMIT__RATE_PER_ENDPOINT_GATE", "0")
    assert cfg_get(f"{RL_BLOCK}.rate_per_endpoint_gate", config={}) is False


# ── config dict 部分覆盖：未给叶保留 canonical ────────────────────────────

def test_r13_b13_config_dict_partial_overlay():
    cfg = {
        IO_BLOCK: {"meta_ttl_s": 7200.0, "ws_heartbeat_s": 3.5},
        RL_BLOCK: {"rate_capacity": 999},
    }
    assert cfg_get(f"{IO_BLOCK}.meta_ttl_s", config=cfg) == 7200.0
    assert cfg_get(f"{IO_BLOCK}.ws_heartbeat_s", config=cfg) == 3.5
    assert cfg_get(f"{IO_BLOCK}.sdk_timeout_s", config=cfg) == 30.0
    assert cfg_get(f"{RL_BLOCK}.rate_capacity", config=cfg) == 999
    assert cfg_get(f"{RL_BLOCK}.rate_refill_per_sec", config=cfg) == 20.0
    assert cfg_get(f"{RL_BLOCK}.rate_shared", config=cfg) is True


# ── read_agent_config 可见 + 深合并（dashboard dump 通道）─────────────────

def test_r13_b13_read_agent_config_exposes_blocks():
    cfg = read_agent_config()
    assert cfg[IO_BLOCK]["sdk_timeout_s"] == 30.0
    assert cfg[IO_BLOCK]["default_leverage"] == 5
    assert cfg[IO_BLOCK]["ws_seq_max_backward"] == 1024
    assert cfg[RL_BLOCK]["rate_capacity"] == 600
    assert cfg[RL_BLOCK]["rate_per_endpoint_gate"] is True


def test_r13_b13_read_agent_config_deep_merges_partial(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        IO_BLOCK: {"sdk_timeout_s": 15.0},
        RL_BLOCK: {"rate_capacity": 800, "rate_shared": False},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg[IO_BLOCK]["sdk_timeout_s"] == 15.0
    assert cfg[IO_BLOCK]["default_leverage"] == 5          # canonical 保留
    assert cfg[RL_BLOCK]["rate_capacity"] == 800
    assert cfg[RL_BLOCK]["rate_shared"] is False
    assert cfg[RL_BLOCK]["rate_refill_per_sec"] == 20.0    # canonical 保留


# ── schema：两块作为 object 被 strict_keys 接受；_ConfigPatch drift ────────

def test_r13_b13_schema_accepts_blocks():
    errors = validate_config_updates(
        {IO_BLOCK: {"sdk_timeout_s": 20.0},
         RL_BLOCK: {"rate_capacity": 700, "rate_shared": False}},
        strict_keys=True,
    )
    assert errors == [], errors


def test_r13_b13_config_patch_knows_blocks():
    """drift sentinel：CANONICAL_DEFAULTS 加块后 _ConfigPatch 必须同步声明。"""
    fields = _ConfigPatch.model_fields
    assert IO_BLOCK in fields
    assert RL_BLOCK in fields
    fb_io = fields[IO_BLOCK].default_factory()
    fb_rl = fields[RL_BLOCK].default_factory()
    assert len(fb_io) == 13 and fb_io["sdk_timeout_s"] == 30.0
    assert len(fb_rl) == 7 and fb_rl["rate_capacity"] == 600
    assert fb_rl["rate_per_endpoint_gate"] is True


# ── helper：默认叶集 == 字面量、签名、SPEC 映射 / kind / guard ────────────

def test_r13_b13_helper_defaults_equal_literals():
    p_io = rl._hl_client_io_params(config={})
    p_rl = rl._hl_rate_limit_params(config={})
    assert set(p_io) == set(IO_LEAVES)
    assert set(p_rl) == set(RL_LEAVES)
    assert p_io == rl._HL_CLIENT_IO_DEFAULTS == CANONICAL_DEFAULTS[IO_BLOCK]
    assert p_rl == rl._HL_RATE_LIMIT_DEFAULTS == CANONICAL_DEFAULTS[RL_BLOCK]


def test_r13_b13_helper_signatures_take_config():
    for fn in (rl._hl_client_io_params, rl._hl_rate_limit_params,
               rl._resolve_hl_block):
        assert "config" in inspect.signature(fn).parameters


def test_r13_b13_spec_maps_all_legacy_envs():
    """19 叶全部映射 legacy env（本批没有纯裸字面量叶）。"""
    for leaf in IO_LEAVES:
        assert rl._HL_CLIENT_IO_SPEC[leaf][0] is not None, leaf
    for leaf in RL_LEAVES:
        assert rl._HL_RATE_LIMIT_SPEC[leaf][0] is not None, leaf
    # 抽查几个关键映射
    assert rl._HL_CLIENT_IO_SPEC["sdk_timeout_s"][0] == "HERMES_HL_SDK_TIMEOUT_S"
    assert rl._HL_CLIENT_IO_SPEC["default_leverage"][0] == "HERMES_DEFAULT_LEVERAGE"
    assert rl._HL_CLIENT_IO_SPEC["ws_max_stale_s"][0] == "HERMES_WS_MAX_STALE_SECONDS"
    assert rl._HL_RATE_LIMIT_SPEC["rate_capacity"][0] == "HERMES_HL_RATE_CAPACITY"
    assert rl._HL_RATE_LIMIT_SPEC["rate_per_endpoint_gate"][0] == \
        "HERMES_HL_RATE_PER_ENDPOINT_GATE"


def test_r13_b13_spec_kinds():
    """kind 分布：bool 2 叶、int 6 叶、其余 float。"""
    for leaf in BOOL_LEAVES:
        assert rl._HL_RATE_LIMIT_SPEC[leaf][1] == "b", leaf
    int_leaves_io = {"default_leverage", "candle_cache_max",
                     "ws_max_stale_s", "ws_seq_max_backward"}
    int_leaves_rl = {"rate_capacity", "rate_429_retries"}
    for leaf in int_leaves_io:
        assert rl._HL_CLIENT_IO_SPEC[leaf][1] == "i", leaf
    for leaf in int_leaves_rl:
        assert rl._HL_RATE_LIMIT_SPEC[leaf][1] == "i", leaf
    for leaf in set(IO_LEAVES) - int_leaves_io:
        assert rl._HL_CLIENT_IO_SPEC[leaf][1] == "f", leaf
    for leaf in set(RL_LEAVES) - int_leaves_rl - set(BOOL_LEAVES):
        assert rl._HL_RATE_LIMIT_SPEC[leaf][1] == "f", leaf


def test_r13_b13_spec_min_guards():
    """TTL/滑点/timeout/retries 类 min=0（0 合法）；capacity/cache_max/
    staleness/seq/leverage 类 min=1；bool 叶 guard 值被跳过不参与比较。"""
    zero_ok_io = {
        "sdk_timeout_s", "max_slippage_pct", "max_slippage_close_pct",
        "meta_ttl_s", "atr_ttl_s", "candle_cache_ttl_s",
        "funding_cache_ttl_s", "ws_heartbeat_s", "ws_max_tick_jump_frac",
    }
    for leaf in zero_ok_io:
        assert rl._HL_CLIENT_IO_SPEC[leaf][2] == 0.0, leaf
    for leaf in ("default_leverage", "candle_cache_max",
                 "ws_max_stale_s", "ws_seq_max_backward"):
        assert rl._HL_CLIENT_IO_SPEC[leaf][2] == 1.0, leaf
    for leaf in ("rate_refill_per_sec", "rate_max_wait_s",
                 "rate_opportunistic_wait_s", "rate_429_retries"):
        assert rl._HL_RATE_LIMIT_SPEC[leaf][2] == 0.0, leaf
    assert rl._HL_RATE_LIMIT_SPEC["rate_capacity"][2] == 1.0


# ── legacy env 兼容（硬约束）：仍生效、优先于 canonical、空串 fallthrough ──

def test_r13_b13_legacy_io_env_flows(monkeypatch):
    monkeypatch.setenv("HERMES_HL_SDK_TIMEOUT_S", "45")
    monkeypatch.setenv("HERMES_WS_HEARTBEAT_S", "12.5")
    p = rl._hl_client_io_params(config={})
    assert p["sdk_timeout_s"] == 45.0
    assert p["ws_heartbeat_s"] == 12.5
    # 未设叶保持字面量
    assert p["meta_ttl_s"] == 3600.0


def test_r13_b13_legacy_rl_env_flows(monkeypatch):
    monkeypatch.setenv("HERMES_HL_RATE_CAPACITY", "777")
    monkeypatch.setenv("HERMES_HL_429_RETRIES", "5")
    p = rl._hl_rate_limit_params(config={})
    assert p["rate_capacity"] == 777
    assert p["rate_429_retries"] == 5


def test_r13_b13_legacy_bool_env_tokens(monkeypatch):
    """bool 叶 legacy env：1/true/yes/on → True；其余（含 0/off/false）→
    False；识别大小写与空白。"""
    for token in ("1", "true", "YES", " On "):
        monkeypatch.setenv("HERMES_HL_RATE_SHARED", token)
        assert rl._hl_rate_limit_params(config={})["rate_shared"] is True, token
    for token in ("0", "off", "false", "garbage"):
        monkeypatch.setenv("HERMES_HL_RATE_SHARED", token)
        assert rl._hl_rate_limit_params(config={})["rate_shared"] is False, token


def test_r13_b13_legacy_env_beats_canonical_env(monkeypatch):
    """legacy env 优先级最高：同时设置时压过 HERMES_CFG_*。"""
    monkeypatch.setenv("HERMES_HL_RATE_CAPACITY", "444")
    monkeypatch.setenv("HERMES_CFG_HL_RATE_LIMIT__RATE_CAPACITY", "555")
    p = rl._hl_rate_limit_params(config={})
    assert p["rate_capacity"] == 444


def test_r13_b13_legacy_env_empty_string_falls_through(monkeypatch):
    """空串视为未设：落到 canonical 通道而非崩溃 / 透传空串。"""
    monkeypatch.setenv("HERMES_HL_SDK_TIMEOUT_S", "")
    monkeypatch.setenv("HERMES_CFG_HL_CLIENT_IO__SDK_TIMEOUT_S", "11.0")
    p = rl._hl_client_io_params(config={})
    assert p["sdk_timeout_s"] == 11.0


def test_r13_b13_helper_config_dict_override():
    p = rl._hl_client_io_params(config={IO_BLOCK: {"meta_ttl_s": 1800.0}})
    assert p["meta_ttl_s"] == 1800.0
    assert p["sdk_timeout_s"] == 30.0
    p2 = rl._hl_rate_limit_params(config={RL_BLOCK: {"rate_max_wait_s": 9.0}})
    assert p2["rate_max_wait_s"] == 9.0
    assert p2["rate_refill_per_sec"] == 20.0


# ── guard：坏值 / 越界回退字面量，热路径不崩；0 在 min=0 叶合法 ───────────

def test_r13_b13_guard_below_min_keeps_literal(monkeypatch):
    """越界（< min）叶保留字面量；同块其他叶正常解析（逐叶 guard）。"""
    monkeypatch.setenv("HERMES_HL_RATE_CAPACITY", "-5")     # min 1
    monkeypatch.setenv("HERMES_HL_RATE_MAX_WAIT_S", "12")   # 合法
    p = rl._hl_rate_limit_params(config={})
    assert p["rate_capacity"] == 600
    assert p["rate_max_wait_s"] == 12.0


def test_r13_b13_guard_garbage_returns_full_literals(monkeypatch):
    """coerce 失败：helper 整块回退字面量（不抛异常、不留半坏 dict）。"""
    monkeypatch.setenv("HERMES_HL_SDK_TIMEOUT_S", "garbage")
    p = rl._hl_client_io_params(config={})
    assert p == rl._HL_CLIENT_IO_DEFAULTS


def test_r13_b13_guard_zero_legal_on_min_zero_leaves(monkeypatch):
    """TTL=0（禁缓存）、close slippage=0（禁平仓逃生舱）、429 retries=0
    （不重试）都是旧 int()/float() 接受的合法值，guard 不得拦截。"""
    monkeypatch.setenv("HERMES_CANDLE_CACHE_TTL_S", "0")
    monkeypatch.setenv("HERMES_FUNDING_CACHE_TTL_S", "0")
    monkeypatch.setenv("HERMES_MAX_SLIPPAGE_CLOSE_PCT", "0")
    p = rl._hl_client_io_params(config={})
    assert p["candle_cache_ttl_s"] == 0.0
    assert p["funding_cache_ttl_s"] == 0.0
    assert p["max_slippage_close_pct"] == 0.0
    monkeypatch.setenv("HERMES_HL_429_RETRIES", "0")
    assert rl._hl_rate_limit_params(config={})["rate_429_retries"] == 0


def test_r13_b13_guard_zero_rejected_on_min_one_leaves(monkeypatch):
    """capacity/cache_max/staleness/seq/leverage 的 0 无意义（旧代码
    int("0") 会生成 0 容量桶 / 0 窗口），guard 回退字面量。"""
    monkeypatch.setenv("HERMES_CANDLE_CACHE_MAX", "0")
    monkeypatch.setenv("HERMES_WS_SEQ_MAX_BACKWARD", "0")
    p = rl._hl_client_io_params(config={})
    assert p["candle_cache_max"] == 512
    assert p["ws_seq_max_backward"] == 1024


def test_r13_b13_helper_returns_independent_copy(monkeypatch):
    """每次返回独立 dict，调用方 mutate 不污染字面量表 / 快照 / 彼此隔离。"""
    a = rl._hl_client_io_params(config={})
    a["sdk_timeout_s"] = 999.0
    b = rl._hl_client_io_params(config={})
    assert b["sdk_timeout_s"] == 30.0
    assert rl._HL_CLIENT_IO_DEFAULTS["sdk_timeout_s"] == 30.0
    assert rl._HL_CLIENT_IO["sdk_timeout_s"] == 30.0


# ── import-time 快照语义：18 叶 boot 时冻结；gate 叶 call-time ────────────

def test_r13_b13_import_time_snapshots_freeze_after_import(monkeypatch):
    """import 后 monkeypatch legacy env：helper（call-time）读到新值，
    但模块级快照 _HL_CLIENT_IO/_HL_RATE_LIMIT 保持 boot 时值——与旧
    "env 须 boot 前设置" 部署语义一致。"""
    monkeypatch.setenv("HERMES_HL_SDK_TIMEOUT_S", "123")
    monkeypatch.setenv("HERMES_HL_RATE_CAPACITY", "321")
    assert rl._hl_client_io_params(config={})["sdk_timeout_s"] == 123.0
    assert rl._hl_rate_limit_params(config={})["rate_capacity"] == 321
    # 快照冻结
    assert rl._HL_CLIENT_IO["sdk_timeout_s"] == 30.0
    assert rl._HL_RATE_LIMIT["rate_capacity"] == 600


def test_r13_b13_snapshot_defaults_equal_canonical():
    """干净 boot 下（测试进程 import 时无 legacy env），快照 == canonical。"""
    assert rl._HL_CLIENT_IO == CANONICAL_DEFAULTS[IO_BLOCK]
    assert rl._HL_RATE_LIMIT == CANONICAL_DEFAULTS[RL_BLOCK]


def test_r13_b13_gate_enabled_helper_call_time_env(monkeypatch):
    """gate 开关保留历史 call-time env 读：import 后切换立即生效。"""
    rl._reset_per_endpoint_gates()
    monkeypatch.delenv("HERMES_HL_RATE_PER_ENDPOINT_GATE", raising=False)
    # env 未设 → 回退 import-time 快照（默认 True）
    assert rl._per_endpoint_gate_enabled() is True
    monkeypatch.setenv("HERMES_HL_RATE_PER_ENDPOINT_GATE", "0")
    assert rl._per_endpoint_gate_enabled() is False
    monkeypatch.setenv("HERMES_HL_RATE_PER_ENDPOINT_GATE", "off")
    assert rl._per_endpoint_gate_enabled() is False
    monkeypatch.setenv("HERMES_HL_RATE_PER_ENDPOINT_GATE", "true")
    assert rl._per_endpoint_gate_enabled() is True
    monkeypatch.setenv("HERMES_HL_RATE_PER_ENDPOINT_GATE", "  ")
    # 空白串 fallthrough → 快照
    assert rl._per_endpoint_gate_enabled() is True


def test_r13_b13_gate_disabled_is_noop(monkeypatch):
    """行为 sentinel：gate=0 时 per_endpoint_gate 不创建锁（no-op），
    复刻 test_gate_disabled_by_env 契约。"""
    rl._reset_per_endpoint_gates()
    monkeypatch.setenv("HERMES_HL_RATE_PER_ENDPOINT_GATE", "0")
    with rl.per_endpoint_gate("candleSnapshot"):
        pass
    assert rl.gate_endpoint_names() == []
    with rl.timed_per_endpoint_gate("metaAndAssetCtxs"):
        pass
    assert rl.gate_endpoint_names() == []


def test_r13_b13_gate_enabled_creates_lock(monkeypatch):
    """gate=1 时已知 endpoint 创建 per-endpoint 锁；unknown/空 endpoint 不建。"""
    rl._reset_per_endpoint_gates()
    monkeypatch.setenv("HERMES_HL_RATE_PER_ENDPOINT_GATE", "1")
    with rl.per_endpoint_gate("candleSnapshot"):
        pass
    with rl.per_endpoint_gate("unknown"):
        pass
    with rl.per_endpoint_gate(""):
        pass
    assert rl.gate_endpoint_names() == ["candleSnapshot"]
    rl._reset_per_endpoint_gates()


# ── 模块属性保留（硬约束）：fallback 符号不删、默认值不变 ──────────────────

def test_r13_b13_rate_limit_symbols_kept():
    assert rl._refill_rate() == 20.0
    assert rl._capacity() == 600
    assert rl.HL_LIMITER.refill_per_sec == 20.0
    assert isinstance(rl._HL_CLIENT_IO, dict)
    assert isinstance(rl._HL_RATE_LIMIT, dict)


def test_r13_b13_hl_client_constants_kept():
    assert hl_client_mod._CANDLE_CACHE_TTL_S == 90.0
    assert hl_client_mod._CANDLE_CACHE_MAX == 512
    assert hl_client_mod._CANDLE_CACHE_DISABLED is False
    assert hl_client_mod._FUNDING_CACHE_TTL_S == 300.0


def test_r13_b13_ws_client_constants_kept():
    assert ws_mod._WS_MAX_STALE_SECONDS == 30
    assert ws_mod._WS_HEARTBEAT_S == 10.0
    assert ws_mod._WS_SEQ_MAX_BACKWARD == 1024


def test_r13_b13_exchange_constants_kept():
    assert exchange_mod._SDK_TIMEOUT == 30.0
    assert exchange_mod.HL_LEVERAGE == 5
    assert exchange_mod._MAX_SLIPPAGE_PCT == 1.5
    assert exchange_mod._MAX_SLIPPAGE_CLOSE_PCT == 5.0
    assert exchange_mod._META_TTL_S == 3600.0
    assert exchange_mod._ATR_TTL_S == 60.0


# ── 热路径接线：源码断言（快照注入 + 旧内联 env 读取清除）─────────────────

def test_r13_b13_rate_limit_source_uses_snapshots():
    assert '_HL_RATE_LIMIT["rate_refill_per_sec"]' in \
        inspect.getsource(rl._refill_rate)
    assert '_HL_RATE_LIMIT["rate_capacity"]' in inspect.getsource(rl._capacity)
    src_build = inspect.getsource(rl._build_limiter)
    assert 'bool(_HL_RATE_LIMIT["rate_shared"])' in src_build
    # state 文件路径保持 env-only 部署旋钮
    assert "HERMES_HL_RATE_STATE_FILE" in src_build


def test_r13_b13_gate_source_uses_call_time_helper():
    assert hasattr(rl, "_per_endpoint_gate_enabled")
    gate_src = inspect.getsource(rl.per_endpoint_gate)
    timed_src = inspect.getsource(rl.timed_per_endpoint_gate)
    assert "_per_endpoint_gate_enabled()" in gate_src
    assert "_per_endpoint_gate_enabled()" in timed_src
    # 旧的内联 env 判定不得残留在两个 gate 里
    assert 'os.environ.get("HERMES_HL_RATE_PER_ENDPOINT_GATE"' not in gate_src
    assert 'os.environ.get("HERMES_HL_RATE_PER_ENDPOINT_GATE"' not in timed_src
    # call-time helper 内部仍直接读 env（这是故意的）
    assert 'os.environ.get("HERMES_HL_RATE_PER_ENDPOINT_GATE")' in \
        inspect.getsource(rl._per_endpoint_gate_enabled)


def test_r13_b13_hl_client_source_wired():
    src = inspect.getsource(hl_client_mod)
    assert '_HL_RATE_LIMIT["rate_max_wait_s"]' in src
    assert '_HL_RATE_LIMIT["rate_429_retries"]' in src
    assert '_HL_RATE_LIMIT["rate_opportunistic_wait_s"]' in src
    assert '_HL_CLIENT_IO["candle_cache_ttl_s"]' in src
    assert '_HL_CLIENT_IO["candle_cache_max"]' in src
    assert '_HL_CLIENT_IO["funding_cache_ttl_s"]' in src
    # 旧内联 env 读取全部清除
    for old in (
        'os.environ.get("HERMES_HL_RATE_MAX_WAIT_S"',
        'os.environ.get("HERMES_HL_429_RETRIES"',
        'os.environ.get("HERMES_HL_RATE_OPPORTUNISTIC_WAIT_S"',
        'os.environ.get("HERMES_CANDLE_CACHE_TTL_S"',
        'os.environ.get("HERMES_CANDLE_CACHE_MAX"',
        'os.environ.get("HERMES_FUNDING_CACHE_TTL_S"',
    ):
        assert old not in src, old


def test_r13_b13_ws_client_source_wired():
    src = inspect.getsource(ws_mod)
    assert '_HL_CLIENT_IO["ws_max_stale_s"]' in src
    assert '_HL_CLIENT_IO["ws_heartbeat_s"]' in src
    assert '_HL_CLIENT_IO["ws_seq_max_backward"]' in src
    for old in (
        'os.environ.get("HERMES_WS_MAX_STALE_SECONDS"',
        'os.environ.get("HERMES_WS_HEARTBEAT_S"',
        'os.environ.get("HERMES_WS_SEQ_MAX_BACKWARD"',
    ):
        assert old not in src, old


def test_r13_b13_exchange_source_wired():
    src = inspect.getsource(exchange_mod)
    assert '_HL_CLIENT_IO["sdk_timeout_s"]' in src
    assert '_HL_CLIENT_IO["default_leverage"]' in src
    assert '_HL_CLIENT_IO["max_slippage_pct"]' in src
    assert '_HL_CLIENT_IO["max_slippage_close_pct"]' in src
    assert '_HL_CLIENT_IO["meta_ttl_s"]' in src
    assert '_HL_CLIENT_IO["atr_ttl_s"]' in src
    for old in (
        'os.environ.get("HERMES_HL_SDK_TIMEOUT_S"',
        'os.environ.get("HERMES_DEFAULT_LEVERAGE"',
        'os.environ.get("HERMES_MAX_SLIPPAGE_PCT"',
        'os.environ.get("HERMES_MAX_SLIPPAGE_CLOSE_PCT"',
        'os.environ.get("HERMES_META_TTL_S"',
        'os.environ.get("HERMES_ATR_TTL_S"',
    ):
        assert old not in src, old
