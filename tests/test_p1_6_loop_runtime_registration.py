"""P1-6: trading_loop 启动/运行时旋钮 canonical 登记测试。

修复前，``scripts/trading_loop.py`` 的十八个 ``HERMES_*`` 旋钮全部在模块
加载时内联 ``os.environ.get("HERMES_*", <字面量>)`` 读取（loop 日志路径、
surge 通知阈值、看门狗超时、exit checkpoint 节流、meta 预热上限、universe
刷新 TTL、启动 grace、基础扫描节拍、P0-1 动态节拍四件套、P0-2 fill-wake、
P0-3 ws_status 开关 + hold/fresh 窗口、P0-4 research 并行开关 + 池宽）。
它们既不进 CANONICAL_DEFAULTS（dashboard dump 看不见、validate_config 不
校验），也没有 agent-config / HERMES_CFG_ 通道。

修复后（零行为变化）：
  * 新增 canonical 块 ``loop_runtime``（18 个 snake_case leaf），默认值逐字
    镜像旧字面量（int/float 类型也逐字）；
  * 新增可导入 helper ``hermes_trader/loop_runtime.py`` 的
    ``loop_runtime_params(*, config=None)``——逐叶解析链 = legacy HERMES_*
    env（**最高优先**，compose / k8s-configmap 旋钮必须继续生效）→
    cfg_get("loop_runtime.<leaf>")（HERMES_CFG_LOOP_RUNTIME__* env +
    agent-config + CANONICAL_DEFAULTS）→ 字面量；空串视为未设；coerce 失败
    整块回退字面量，启动路径绝不抛错；
  * resolver 刻意放在可导入包模块（trading_loop.py 模块级有 sleep / WS 启动
    / while True 副作用，不可导入），镜像 realtime_feed / scan_budget_params
    的"逻辑外置可测"先例。

与 scan_budget 不同，这十八叶**没有 range guard**：旧内联读取本就无任何
范围校验，0 / 负值是合法的"禁用"值（universe_refresh_s=0、startup_grace=0、
watchdog<=0 都有明确语义）。因此 guard 测试只覆盖 coerce 失败回退。
"""

import json
from pathlib import Path

from hermes_trader.agents import config_store
from hermes_trader.agents.config_schema import _ConfigPatch, validate_config_updates
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get, read_agent_config
from hermes_trader.loop_runtime import (
    _LEGACY_ENV_SPEC,
    LOOP_RUNTIME_DEFAULTS,
    _canonical_matches_literals,
    loop_runtime_params,
)

BLOCK = "loop_runtime"
LEAVES = (
    "loop_log_path",
    "surge_min_score",
    "watchdog_timeout_s",
    "exit_checkpoint_min_interval_s",
    "meta_prewarm_timeout_s",
    "universe_refresh_s",
    "startup_grace_s",
    "scan_interval",
    "scan_dynamic",
    "scan_fresh_s",
    "scan_interval_fast",
    "scan_interval_slow",
    "ws_fill_wake",
    "ws_status_event",
    "ws_status_hold_s",
    "ws_status_fresh_s",
    "research_parallel",
    "research_parallel_workers",
)
LEGACY_ENVS = tuple(spec[0] for spec in _LEGACY_ENV_SPEC.values())

TRADING_LOOP_SRC = (
    Path(__file__).resolve().parents[1] / "scripts" / "trading_loop.py"
).read_text(encoding="utf-8")


def _clear_legacy_envs(monkeypatch):
    for var in LEGACY_ENVS:
        monkeypatch.delenv(var, raising=False)


# ── canonical 登记：块存在、18 叶、与 resolver 字面量表逐字一致 ────────────

def test_p1_6_block_registered():
    """loop_runtime 嵌套块必须在 CANONICAL_DEFAULTS 中且恰为 18 叶。"""
    assert BLOCK in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS[BLOCK], dict)
    assert len(CANONICAL_DEFAULTS[BLOCK]) == 18
    assert set(CANONICAL_DEFAULTS[BLOCK]) == set(LEAVES)


def test_p1_6_canonical_defaults_mirror_literals():
    """canonical 块必须逐字（含 int/float 类型）等于 resolver 字面量表。"""
    assert _canonical_matches_literals()
    assert CANONICAL_DEFAULTS[BLOCK] == LOOP_RUNTIME_DEFAULTS


def test_p1_6_individual_leaf_values_sentinel():
    """逐叶 sentinel：锁死十八个默认值，防误改（零行为变化基线）。"""
    b = CANONICAL_DEFAULTS[BLOCK]
    assert b["loop_log_path"] == "/data/trading-loop.log"
    assert b["surge_min_score"] == 40.0
    assert b["watchdog_timeout_s"] == 600
    assert b["exit_checkpoint_min_interval_s"] == 5.0
    assert b["meta_prewarm_timeout_s"] == 3.0
    assert b["universe_refresh_s"] == 1800
    assert b["startup_grace_s"] == 12.0
    assert b["scan_interval"] == 15
    assert b["scan_dynamic"] is False
    assert b["scan_fresh_s"] == 10.0
    assert b["scan_interval_fast"] == 8
    assert b["scan_interval_slow"] == 20
    assert b["ws_fill_wake"] is False
    assert b["ws_status_event"] is False
    assert b["ws_status_hold_s"] == 30.0
    assert b["ws_status_fresh_s"] == 10.0
    assert b["research_parallel"] is True          # 默认开（P1-4 审计结论）
    assert b["research_parallel_workers"] == 4


def test_p1_6_leaf_types_match_literals():
    """类型 sentinel：int 槽位保持 int、float 旋钮保持 float、bool 保持 bool。"""
    b = CANONICAL_DEFAULTS[BLOCK]
    for leaf in ("watchdog_timeout_s", "universe_refresh_s", "scan_interval",
                 "scan_interval_fast", "scan_interval_slow",
                 "research_parallel_workers"):
        assert isinstance(b[leaf], int) and not isinstance(b[leaf], bool), leaf
    for leaf in ("surge_min_score", "exit_checkpoint_min_interval_s",
                 "meta_prewarm_timeout_s", "startup_grace_s", "scan_fresh_s",
                 "ws_status_hold_s", "ws_status_fresh_s"):
        assert isinstance(b[leaf], float), leaf
    for leaf in ("scan_dynamic", "ws_fill_wake", "ws_status_event",
                 "research_parallel"):
        assert isinstance(b[leaf], bool), leaf
    assert isinstance(b["loop_log_path"], str)


# ── cfg_get 点路径 / 整块：空 config 回退 canonical ────────────────────────

def test_p1_6_cfg_get_all_leaves():
    for leaf in LEAVES:
        assert cfg_get(f"{BLOCK}.{leaf}", config={}) == LOOP_RUNTIME_DEFAULTS[leaf]


def test_p1_6_cfg_get_full_block():
    blk = cfg_get(BLOCK, config={})
    assert isinstance(blk, dict) and len(blk) == 18
    assert blk["scan_interval"] == 15
    assert blk["research_parallel"] is True


# ── HERMES_CFG_ canonical env 通道（含 int/float/bool coerce）──────────────

def test_p1_6_cfg_env_override_int(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_LOOP_RUNTIME__SCAN_INTERVAL", "42")
    v = cfg_get(f"{BLOCK}.scan_interval", config={})
    assert v == 42 and isinstance(v, int)


def test_p1_6_cfg_env_override_float(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_LOOP_RUNTIME__SURGE_MIN_SCORE", "33.5")
    v = cfg_get(f"{BLOCK}.surge_min_score", config={})
    assert v == 33.5 and isinstance(v, float)


def test_p1_6_cfg_env_override_bool(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_LOOP_RUNTIME__SCAN_DYNAMIC", "true")
    v = cfg_get(f"{BLOCK}.scan_dynamic", config={})
    assert v is True


# ── config dict 部分覆盖：未给叶保留 canonical ─────────────────────────────

def test_p1_6_config_dict_partial_overlay():
    cfg = {BLOCK: {"watchdog_timeout_s": 999, "ws_status_hold_s": 45.0}}
    assert cfg_get(f"{BLOCK}.watchdog_timeout_s", config=cfg) == 999
    assert cfg_get(f"{BLOCK}.ws_status_hold_s", config=cfg) == 45.0
    assert cfg_get(f"{BLOCK}.scan_interval", config=cfg) == 15       # canonical 保留
    assert cfg_get(f"{BLOCK}.research_parallel", config=cfg) is True


# ── read_agent_config 可见 + 深合并（dashboard dump 通道）─────────────────

def test_p1_6_read_agent_config_exposes_block():
    cfg = read_agent_config()
    assert BLOCK in cfg
    assert cfg[BLOCK]["scan_interval"] == 15
    assert cfg[BLOCK]["research_parallel_workers"] == 4


def test_p1_6_read_agent_config_deep_merges_partial(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({BLOCK: {"universe_refresh_s": 60, "scan_interval": 9}}))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg[BLOCK]["universe_refresh_s"] == 60       # 覆盖
    assert cfg[BLOCK]["scan_interval"] == 9            # 覆盖
    assert cfg[BLOCK]["watchdog_timeout_s"] == 600     # canonical 保留
    assert cfg[BLOCK]["research_parallel"] is True


# ── schema：块作为 object 被 strict_keys 接受；_ConfigPatch drift sentinel ─

def test_p1_6_schema_accepts_block():
    errors = validate_config_updates(
        {BLOCK: {"scan_interval": 30, "research_parallel_workers": 8,
                 "ws_status_event": True}},
        strict_keys=True,
    )
    assert not any("unknown key" in e for e in errors), errors
    assert errors == [], errors


def test_p1_6_config_patch_knows_block():
    """drift sentinel：CANONICAL_DEFAULTS 加块后 _ConfigPatch 必须同步声明。"""
    fields = _ConfigPatch.model_fields
    assert BLOCK in fields
    blk = fields[BLOCK].default_factory()
    assert blk["scan_interval"] == 15
    assert blk["research_parallel_workers"] == 4
    assert len(blk) == 18


# ── helper：默认 18 叶 == 字面量、SPEC 映射正确 ───────────────────────────

def test_p1_6_helper_defaults_equal_literals(monkeypatch):
    _clear_legacy_envs(monkeypatch)
    p = loop_runtime_params(config={})
    assert p == LOOP_RUNTIME_DEFAULTS
    assert set(p) == set(LEAVES)


def test_p1_6_spec_maps_eighteen_legacy_envs():
    """18 个 leaf 全部映射到 legacy HERMES_* env（无一纯硬编码）。"""
    assert len(_LEGACY_ENV_SPEC) == 18
    assert _LEGACY_ENV_SPEC["loop_log_path"] == ("HERMES_LOOP_LOG_FILE", "str")
    assert _LEGACY_ENV_SPEC["surge_min_score"] == ("HERMES_SURGE_MIN_SCORE", "float")
    assert _LEGACY_ENV_SPEC["watchdog_timeout_s"] == ("HERMES_WATCHDOG_TIMEOUT_S", "int")
    assert _LEGACY_ENV_SPEC["exit_checkpoint_min_interval_s"] == (
        "HERMES_EXIT_CHECKPOINT_MIN_INTERVAL_S", "float")
    assert _LEGACY_ENV_SPEC["meta_prewarm_timeout_s"] == (
        "HERMES_META_PREWARM_TIMEOUT_S", "float")
    assert _LEGACY_ENV_SPEC["universe_refresh_s"] == ("HERMES_UNIVERSE_REFRESH_S", "int")
    assert _LEGACY_ENV_SPEC["startup_grace_s"] == ("HERMES_STARTUP_GRACE_S", "float")
    assert _LEGACY_ENV_SPEC["scan_interval"] == ("HERMES_SCAN_INTERVAL", "int")
    assert _LEGACY_ENV_SPEC["scan_dynamic"] == ("HERMES_SCAN_DYNAMIC", "bool")
    assert _LEGACY_ENV_SPEC["scan_fresh_s"] == ("HERMES_SCAN_FRESH_S", "float")
    assert _LEGACY_ENV_SPEC["scan_interval_fast"] == ("HERMES_SCAN_INTERVAL_FAST", "int")
    assert _LEGACY_ENV_SPEC["scan_interval_slow"] == ("HERMES_SCAN_INTERVAL_SLOW", "int")
    assert _LEGACY_ENV_SPEC["ws_fill_wake"] == ("HERMES_WS_FILL_WAKE", "bool")
    assert _LEGACY_ENV_SPEC["ws_status_event"] == ("HERMES_WS_STATUS_EVENT", "bool")
    assert _LEGACY_ENV_SPEC["ws_status_hold_s"] == ("HERMES_WS_STATUS_HOLD_S", "float")
    assert _LEGACY_ENV_SPEC["ws_status_fresh_s"] == ("HERMES_WS_STATUS_FRESH_S", "float")
    assert _LEGACY_ENV_SPEC["research_parallel"] == ("HERMES_RESEARCH_PARALLEL", "bool")
    assert _LEGACY_ENV_SPEC["research_parallel_workers"] == (
        "HERMES_RESEARCH_PARALLEL_WORKERS", "int")


# ── legacy env 兼容（硬约束）：仍生效且优先于 canonical 通道 ──────────────

def test_p1_6_legacy_env_scan_interval_flows(monkeypatch):
    _clear_legacy_envs(monkeypatch)
    monkeypatch.setenv("HERMES_SCAN_INTERVAL", "7")
    p = loop_runtime_params(config={})
    assert p["scan_interval"] == 7


def test_p1_6_legacy_env_beats_canonical_env(monkeypatch):
    """legacy HERMES_* 优先级最高：同时设置时压过 HERMES_CFG_*。"""
    _clear_legacy_envs(monkeypatch)
    monkeypatch.setenv("HERMES_SCAN_INTERVAL", "7")
    monkeypatch.setenv("HERMES_CFG_LOOP_RUNTIME__SCAN_INTERVAL", "55")
    p = loop_runtime_params(config={})
    assert p["scan_interval"] == 7


def test_p1_6_legacy_env_empty_string_falls_through_to_canonical(monkeypatch):
    """空串视为未设（旧 int("") 直接报错），应落到 canonical 通道而非崩溃。"""
    _clear_legacy_envs(monkeypatch)
    monkeypatch.setenv("HERMES_WATCHDOG_TIMEOUT_S", "")
    monkeypatch.setenv("HERMES_CFG_LOOP_RUNTIME__WATCHDOG_TIMEOUT_S", "99")
    p = loop_runtime_params(config={})
    assert p["watchdog_timeout_s"] == 99


def test_p1_6_legacy_env_bools_use_historical_truth_set(monkeypatch):
    """bool 叶真值集合逐字沿用旧内联读取：1/true/yes/on 为真，其余为假。"""
    _clear_legacy_envs(monkeypatch)
    monkeypatch.setenv("HERMES_SCAN_DYNAMIC", "yes")
    monkeypatch.setenv("HERMES_WS_FILL_WAKE", "on")
    monkeypatch.setenv("HERMES_WS_STATUS_EVENT", "1")
    monkeypatch.setenv("HERMES_RESEARCH_PARALLEL", "0")   # 显式关并行
    p = loop_runtime_params(config={})
    assert p["scan_dynamic"] is True
    assert p["ws_fill_wake"] is True
    assert p["ws_status_event"] is True
    assert p["research_parallel"] is False


def test_p1_6_legacy_env_log_path_flows(monkeypatch):
    _clear_legacy_envs(monkeypatch)
    monkeypatch.setenv("HERMES_LOOP_LOG_FILE", "/tmp/another-loop.log")
    p = loop_runtime_params(config={})
    assert p["loop_log_path"] == "/tmp/another-loop.log"


# ── canonical env / config dict 经 helper 流向消费方 ──────────────────────

def test_p1_6_helper_canonical_env_flows(monkeypatch):
    """无 legacy env 干扰时，HERMES_CFG_LOOP_RUNTIME__* 必须流向 helper。"""
    _clear_legacy_envs(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_LOOP_RUNTIME__RESEARCH_PARALLEL_WORKERS", "9")
    p = loop_runtime_params(config={})
    assert p["research_parallel_workers"] == 9


def test_p1_6_helper_config_dict_override():
    p = loop_runtime_params(config={BLOCK: {
        "research_parallel_workers": 8,
        "surge_min_score": 2.5,
        "ws_status_event": True,
    }})
    assert p["research_parallel_workers"] == 8
    assert p["surge_min_score"] == 2.5
    assert p["ws_status_event"] is True
    assert p["scan_interval"] == 15          # 其余叶保留字面量
    assert p["research_parallel"] is True


# ── guard：坏值 coerce 失败整块回退字面量，启动路径不崩 ───────────────────

def test_p1_6_guard_garbage_int_returns_full_literals(monkeypatch):
    """int 叶拿到不可解析串：helper 整块回退字面量（不留半坏 dict）。"""
    _clear_legacy_envs(monkeypatch)
    monkeypatch.setenv("HERMES_SCAN_INTERVAL", "garbage")
    p = loop_runtime_params(config={})
    assert p == LOOP_RUNTIME_DEFAULTS


def test_p1_6_guard_bool_rejected_for_int_leaf_falls_back():
    """bool 是 int 子类：int/float 叶拿到真 bool 必须拒绝（防 True 静默变 1），
    整块回退字面量。"""
    p = loop_runtime_params(config={BLOCK: {"scan_interval": True}})
    assert p == LOOP_RUNTIME_DEFAULTS


def test_p1_6_helper_returns_independent_copy():
    """每次返回独立 dict，调用方 mutate 不污染字面量表。"""
    a = loop_runtime_params(config={})
    a["scan_interval"] = 999
    b = loop_runtime_params(config={})
    assert b["scan_interval"] == 15
    assert LOOP_RUNTIME_DEFAULTS["scan_interval"] == 15


# ── trading_loop 接线：源码断言（resolver 注入 + 18 叶消费 + 旧 env 清除）──

def test_p1_6_trading_loop_imports_and_invokes_resolver():
    assert "from hermes_trader.loop_runtime import loop_runtime_params" in TRADING_LOOP_SRC
    assert "_rt = _loop_runtime_params()" in TRADING_LOOP_SRC


def test_p1_6_trading_loop_consumes_all_leaves():
    for leaf in LEAVES:
        assert f'_rt["{leaf}"]' in TRADING_LOOP_SRC, leaf


def test_p1_6_trading_loop_no_longer_reads_legacy_env_directly():
    """十八个内联 os.environ.get 必须全部移出 trading_loop.py（.env.local
    加载用的 os.environ[key]=... 赋值不是 get，保留）。"""
    assert "os.environ.get(" not in TRADING_LOOP_SRC
