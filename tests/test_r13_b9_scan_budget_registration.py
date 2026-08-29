"""R13-B9: perception scan 预算 / 节拍旋钮 canonical 登记测试。

修复前，``scan_once`` 的十二个预算旋钮散落在热路径里：九个经
``os.environ.get("HERMES_*", <字面量>)`` 内联读取（HERMES_MAX_MARKETS /
_HIP3 / _MOVERS / HERMES_MOVERS_VOL_FLOOR_USD / HERMES_HIP3_MOVERS_FLOOR_USD /
HERMES_UNIVERSE_SWEEP / HERMES_BATCH_SIZE / HERMES_BATCH_SLEEP /
HERMES_PERCEPTION_CACHE_MAX），另有三个纯硬编码——线程池宽度兜底 32、
movers 阈值 ``>= 1.0``（1.0%）、``future.result(timeout=60)``。它们既不进
CANONICAL_DEFAULTS（dashboard dump 看不见、validate_config 不校验），也没有
agent-config / HERMES_CFG_ 通道。

修复后（零行为变化）：
  * 新增 canonical 块 ``scan_budget``（12 个 snake_case leaf），默认值逐字
    镜像旧字面量；
  * perception.py 新增 ``scan_budget_params(*, config=None)`` helper：逐叶
    解析链 = legacy HERMES_* env（**最高优先**，MCP server 写
    HERMES_MAX_MARKETS、tests/test_cleanup.py 与 tests/test_online.py 直设
    这些变量，必须继续生效）→ cfg_get("scan_budget.<leaf>")
    （HERMES_CFG_SCAN_BUDGET__* env + agent-config + CANONICAL_DEFAULTS）→
    字面量；coerce/guard 失败回退字面量，热路径绝不抛错；
  * scan_once 浅合并后一次性解析 budget，五个旧 env 读取点、movers 1.0%、
    sweep_n、batch_size/sleep、future timeout 全部改读 budget；
  * 模块级 ``_candle_cache`` 保留 legacy env bootstrap，scan 启动时把
    ``_max_size`` 热同步到 budget["cache_max"]（_Cache.set 每次写入读
    _max_size，无需重建缓存）。

guard 规则：预算槽位（max_markets*）/ 成交量地板 / universe_sweep /
batch_sleep 下限为 **0**——0 是合法的"保留/禁用"值（test_cleanup 显式设
HERMES_MAX_MARKETS_MOVERS=0、HERMES_UNIVERSE_SWEEP=0）；cache_max /
batch_size / parallel_workers / future_timeout_sec 下限 1；movers_min_pct
下限 0.0001（须为正百分比）。
"""

import inspect
import json

import hermes_trader.agents.perception as perception_mod
from hermes_trader.agents import config_store
from hermes_trader.agents.config_schema import _ConfigPatch, validate_config_updates
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get, read_agent_config
from hermes_trader.agents.perception import (
    _SCAN_BUDGET_DEFAULTS,
    _SCAN_BUDGET_SPEC,
    scan_budget_params,
)

BLOCK = "scan_budget"
LEAVES = (
    "cache_max",
    "max_markets",
    "max_markets_hip3",
    "max_markets_movers",
    "movers_vol_floor_usd",
    "hip3_movers_floor_usd",
    "universe_sweep",
    "batch_size",
    "batch_sleep_sec",
    "parallel_workers",
    "movers_min_pct",
    "future_timeout_sec",
)


# ── canonical 登记：块存在、12 叶、与 perception 字面量表逐字一致 ──────────

def test_r13_b9_scan_budget_block_registered():
    """scan_budget 嵌套块必须在 CANONICAL_DEFAULTS 中且恰为 12 叶。"""
    assert BLOCK in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS[BLOCK], dict)
    assert len(CANONICAL_DEFAULTS[BLOCK]) == 12
    assert set(CANONICAL_DEFAULTS[BLOCK]) == set(LEAVES)


def test_r13_b9_canonical_defaults_mirror_perception_literals():
    """canonical 块必须逐字等于热路径回退用的 _SCAN_BUDGET_DEFAULTS。"""
    assert CANONICAL_DEFAULTS[BLOCK] == _SCAN_BUDGET_DEFAULTS


def test_r13_b9_individual_leaf_values_sentinel():
    """逐叶 sentinel：锁死十二个默认值，防误改（零行为变化基线）。"""
    b = CANONICAL_DEFAULTS[BLOCK]
    assert b["cache_max"] == 512
    assert b["max_markets"] == 60
    assert b["max_markets_hip3"] == 25
    assert b["max_markets_movers"] == 10
    assert b["movers_vol_floor_usd"] == 300_000.0
    assert b["hip3_movers_floor_usd"] == 50_000.0
    assert b["universe_sweep"] == 0
    assert b["batch_size"] == 20
    assert b["batch_sleep_sec"] == 0.3
    assert b["parallel_workers"] == 32
    assert b["movers_min_pct"] == 1.0
    assert b["future_timeout_sec"] == 60


def test_r13_b9_leaf_types_match_literals():
    """类型 sentinel：int 槽位保持 int、float 旋钮保持 float。"""
    b = CANONICAL_DEFAULTS[BLOCK]
    for leaf in ("cache_max", "max_markets", "max_markets_hip3",
                 "max_markets_movers", "universe_sweep", "batch_size",
                 "parallel_workers", "future_timeout_sec"):
        assert isinstance(b[leaf], int), leaf
    for leaf in ("movers_vol_floor_usd", "hip3_movers_floor_usd",
                 "batch_sleep_sec", "movers_min_pct"):
        assert isinstance(b[leaf], float), leaf


# ── cfg_get 点路径 / 整块：空 config 回退 canonical ────────────────────────

def test_r13_b9_cfg_get_all_leaves():
    for leaf in LEAVES:
        assert cfg_get(f"{BLOCK}.{leaf}", config={}) == _SCAN_BUDGET_DEFAULTS[leaf]


def test_r13_b9_cfg_get_full_block():
    blk = cfg_get(BLOCK, config={})
    assert isinstance(blk, dict) and len(blk) == 12
    assert blk["max_markets"] == 60
    assert blk["movers_min_pct"] == 1.0


# ── HERMES_CFG_ canonical env 通道（含 int/float coerce）──────────────────

def test_r13_b9_cfg_env_override_int(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_SCAN_BUDGET__MAX_MARKETS", "42")
    v = cfg_get(f"{BLOCK}.max_markets", config={})
    assert v == 42 and isinstance(v, int)


def test_r13_b9_cfg_env_override_float(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_SCAN_BUDGET__MOVERS_MIN_PCT", "2.75")
    v = cfg_get(f"{BLOCK}.movers_min_pct", config={})
    assert v == 2.75 and isinstance(v, float)


def test_r13_b9_cfg_env_override_workers(monkeypatch):
    # parallel_workers 没有 legacy env，canonical env 是唯一 env 通道
    monkeypatch.setenv("HERMES_CFG_SCAN_BUDGET__PARALLEL_WORKERS", "16")
    assert cfg_get(f"{BLOCK}.parallel_workers", config={}) == 16


# ── config dict 部分覆盖：未给叶保留 canonical ─────────────────────────────

def test_r13_b9_config_dict_partial_overlay():
    cfg = {BLOCK: {"batch_size": 7, "future_timeout_sec": 90}}
    assert cfg_get(f"{BLOCK}.batch_size", config=cfg) == 7
    assert cfg_get(f"{BLOCK}.future_timeout_sec", config=cfg) == 90
    assert cfg_get(f"{BLOCK}.max_markets", config=cfg) == 60      # canonical 保留
    assert cfg_get(f"{BLOCK}.batch_sleep_sec", config=cfg) == 0.3


# ── read_agent_config 可见 + 深合并（dashboard dump 通道）─────────────────

def test_r13_b9_read_agent_config_exposes_block():
    cfg = read_agent_config()
    assert BLOCK in cfg
    assert cfg[BLOCK]["max_markets"] == 60
    assert cfg[BLOCK]["cache_max"] == 512


def test_r13_b9_read_agent_config_deep_merges_partial(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({BLOCK: {"universe_sweep": 5, "batch_size": 11}}))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg[BLOCK]["universe_sweep"] == 5            # 覆盖
    assert cfg[BLOCK]["batch_size"] == 11               # 覆盖
    assert cfg[BLOCK]["max_markets"] == 60              # canonical 保留
    assert cfg[BLOCK]["movers_min_pct"] == 1.0


# ── schema：块作为 object 被 strict_keys 接受；_ConfigPatch drift sentinel ─

def test_r13_b9_schema_accepts_block():
    errors = validate_config_updates(
        {BLOCK: {"max_markets": 80, "parallel_workers": 24, "movers_min_pct": 1.5}},
        strict_keys=True,
    )
    assert not any("unknown key" in e for e in errors), errors
    assert errors == [], errors


def test_r13_b9_config_patch_knows_scan_budget():
    """drift sentinel：CANONICAL_DEFAULTS 加块后 _ConfigPatch 必须同步声明。"""
    fields = _ConfigPatch.model_fields
    assert BLOCK in fields
    blk = fields[BLOCK].default_factory()
    assert blk["max_markets"] == 60
    assert blk["future_timeout_sec"] == 60
    assert len(blk) == 12


# ── helper：默认 12 叶 == 字面量、SPEC 映射正确 ───────────────────────────

def test_r13_b9_helper_defaults_equal_literals(monkeypatch):
    for v in (
        "HERMES_MAX_MARKETS", "HERMES_MAX_MARKETS_HIP3", "HERMES_MAX_MARKETS_MOVERS",
        "HERMES_MOVERS_VOL_FLOOR_USD", "HERMES_HIP3_MOVERS_FLOOR_USD",
        "HERMES_UNIVERSE_SWEEP", "HERMES_BATCH_SIZE", "HERMES_BATCH_SLEEP",
        "HERMES_PERCEPTION_CACHE_MAX",
    ):
        monkeypatch.delenv(v, raising=False)
    p = scan_budget_params(config={})
    assert p == _SCAN_BUDGET_DEFAULTS
    assert set(p) == set(LEAVES)


def test_r13_b9_spec_maps_nine_legacy_envs():
    """9 个 leaf 映射到 legacy HERMES_* env；3 个纯硬编码 leaf 无 legacy env。"""
    assert _SCAN_BUDGET_SPEC["max_markets"][0] == "HERMES_MAX_MARKETS"
    assert _SCAN_BUDGET_SPEC["max_markets_hip3"][0] == "HERMES_MAX_MARKETS_HIP3"
    assert _SCAN_BUDGET_SPEC["max_markets_movers"][0] == "HERMES_MAX_MARKETS_MOVERS"
    assert _SCAN_BUDGET_SPEC["movers_vol_floor_usd"][0] == "HERMES_MOVERS_VOL_FLOOR_USD"
    assert _SCAN_BUDGET_SPEC["hip3_movers_floor_usd"][0] == "HERMES_HIP3_MOVERS_FLOOR_USD"
    assert _SCAN_BUDGET_SPEC["universe_sweep"][0] == "HERMES_UNIVERSE_SWEEP"
    assert _SCAN_BUDGET_SPEC["batch_size"][0] == "HERMES_BATCH_SIZE"
    assert _SCAN_BUDGET_SPEC["batch_sleep_sec"][0] == "HERMES_BATCH_SLEEP"
    assert _SCAN_BUDGET_SPEC["cache_max"][0] == "HERMES_PERCEPTION_CACHE_MAX"
    for leaf in ("parallel_workers", "movers_min_pct", "future_timeout_sec"):
        assert _SCAN_BUDGET_SPEC[leaf][0] is None, leaf


# ── legacy env 兼容（硬约束）：仍生效且优先于 canonical 通道 ──────────────

def test_r13_b9_legacy_env_max_markets_flows(monkeypatch):
    """MCP server (scripts/hermes-mcp-server.py L936) 写 HERMES_MAX_MARKETS
    驱动 perception——helper 必须继续读到。"""
    monkeypatch.setenv("HERMES_MAX_MARKETS", "7")
    p = scan_budget_params(config={})
    assert p["max_markets"] == 7


def test_r13_b9_legacy_env_beats_canonical_env(monkeypatch):
    """legacy HERMES_* 优先级最高：同时设置时压过 HERMES_CFG_*。"""
    monkeypatch.setenv("HERMES_MAX_MARKETS", "7")
    monkeypatch.setenv("HERMES_CFG_SCAN_BUDGET__MAX_MARKETS", "55")
    p = scan_budget_params(config={})
    assert p["max_markets"] == 7


def test_r13_b9_legacy_env_empty_string_falls_through_to_canonical(monkeypatch):
    """空串视为未设（os.environ.get 拿到 "" 的旧行为是 int("") 直接报错），
    应落到 canonical 通道而非崩溃。"""
    monkeypatch.setenv("HERMES_MAX_MARKETS", "")
    monkeypatch.setenv("HERMES_CFG_SCAN_BUDGET__BATCH_SIZE", "13")
    p = scan_budget_params(config={})
    assert p["max_markets"] == 60
    assert p["batch_size"] == 13


def test_r13_b9_legacy_env_full_test_cleanup_mix(monkeypatch):
    """复现 tests/test_cleanup.py 的 env 组合（10/3/0/0 + 地板），helper 必须
    原样解析——旧测试不允许回归。"""
    monkeypatch.setenv("HERMES_MAX_MARKETS", "10")
    monkeypatch.setenv("HERMES_MAX_MARKETS_HIP3", "3")
    monkeypatch.setenv("HERMES_MAX_MARKETS_MOVERS", "0")
    monkeypatch.setenv("HERMES_UNIVERSE_SWEEP", "0")
    p = scan_budget_params(config={})
    assert p["max_markets"] == 10
    assert p["max_markets_hip3"] == 3
    assert p["max_markets_movers"] == 0
    assert p["universe_sweep"] == 0


def test_r13_b9_legacy_env_vol_floor_override(monkeypatch):
    """test_cleanup L1556 设 HERMES_MOVERS_VOL_FLOOR_USD=1000000。"""
    monkeypatch.setenv("HERMES_MOVERS_VOL_FLOOR_USD", "1000000")
    p = scan_budget_params(config={})
    assert p["movers_vol_floor_usd"] == 1_000_000.0
    assert p["hip3_movers_floor_usd"] == 50_000.0  # 另一叶不受影响


def test_r13_b9_legacy_env_cache_max_flows(monkeypatch):
    monkeypatch.setenv("HERMES_PERCEPTION_CACHE_MAX", "256")
    p = scan_budget_params(config={})
    assert p["cache_max"] == 256


# ── canonical env / config dict 经 helper 流向消费方 ──────────────────────

def test_r13_b9_helper_canonical_env_for_legacy_leaf(monkeypatch):
    """无 legacy env 干扰时，HERMES_CFG_SCAN_BUDGET__* 必须流向 helper。"""
    monkeypatch.delenv("HERMES_BATCH_SIZE", raising=False)
    monkeypatch.setenv("HERMES_CFG_SCAN_BUDGET__BATCH_SIZE", "9")
    p = scan_budget_params(config={})
    assert p["batch_size"] == 9


def test_r13_b9_helper_config_dict_override():
    p = scan_budget_params(config={BLOCK: {
        "parallel_workers": 8,
        "movers_min_pct": 2.5,
        "future_timeout_sec": 120,
    }})
    assert p["parallel_workers"] == 8
    assert p["movers_min_pct"] == 2.5
    assert p["future_timeout_sec"] == 120
    assert p["max_markets"] == 60  # 其余叶保留字面量


# ── guard：坏值 / 越界回退字面量，热路径不崩 ──────────────────────────────

def test_r13_b9_guard_negative_budget_slot_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_MAX_MARKETS", "-5")
    p = scan_budget_params(config={})
    assert p["max_markets"] == 60  # 负预算非法 → 字面量


def test_r13_b9_guard_zero_slots_are_legal(monkeypatch):
    """0 是合法的"保留/禁用"值：movers=0 / sweep=0 / hip3=0 都必须生效
    （test_cleanup 依赖）。"""
    monkeypatch.setenv("HERMES_MAX_MARKETS_MOVERS", "0")
    monkeypatch.setenv("HERMES_UNIVERSE_SWEEP", "0")
    monkeypatch.setenv("HERMES_MAX_MARKETS_HIP3", "0")
    p = scan_budget_params(config={})
    assert p["max_markets_movers"] == 0
    assert p["universe_sweep"] == 0
    assert p["max_markets_hip3"] == 0


def test_r13_b9_guard_batch_size_zero_falls_back(monkeypatch):
    """batch_size 下限 1（0 会让 range 步长为 0 直接 ValueError）。"""
    monkeypatch.setenv("HERMES_BATCH_SIZE", "0")
    p = scan_budget_params(config={})
    assert p["batch_size"] == 20


def test_r13_b9_guard_cache_max_below_one_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_PERCEPTION_CACHE_MAX", "0")
    p = scan_budget_params(config={})
    assert p["cache_max"] == 512


def test_r13_b9_guard_workers_below_one_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_SCAN_BUDGET__PARALLEL_WORKERS", "0")
    p = scan_budget_params(config={})
    assert p["parallel_workers"] == 32


def test_r13_b9_guard_movers_min_pct_must_be_positive(monkeypatch):
    """movers_min_pct 下限 0.0001：0 / 负值会让全部市场都算 movers，回退 1.0。"""
    monkeypatch.setenv("HERMES_CFG_SCAN_BUDGET__MOVERS_MIN_PCT", "0")
    p = scan_budget_params(config={})
    assert p["movers_min_pct"] == 1.0


def test_r13_b9_guard_negative_sleep_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_BATCH_SLEEP", "-1")
    p = scan_budget_params(config={})
    assert p["batch_sleep_sec"] == 0.3


def test_r13_b9_guard_garbage_string_returns_full_literals(monkeypatch):
    """coerce 失败：helper 整块回退字面量（不抛异常、不留半坏 dict）。"""
    monkeypatch.setenv("HERMES_BATCH_SIZE", "garbage")
    p = scan_budget_params(config={})
    assert p == _SCAN_BUDGET_DEFAULTS


def test_r13_b9_helper_returns_independent_copy():
    """每次返回独立 dict，调用方 mutate 不污染字面量表。"""
    a = scan_budget_params(config={})
    a["max_markets"] = 999
    b = scan_budget_params(config={})
    assert b["max_markets"] == 60
    assert _SCAN_BUDGET_DEFAULTS["max_markets"] == 60


# ── scan_once 接线：源码断言（helper 注入 + 12 叶消费点 + 旧 env 清除）─────

def test_r13_b9_scan_once_resolves_budget_via_helper():
    src = inspect.getsource(perception_mod.scan_once)
    assert "budget = scan_budget_params(config=_cfg)" in src
    # 线程池宽度：显式 call arg > canonical（默认 32）
    assert "workers = parallel_workers or int(budget[\"parallel_workers\"])" in src
    # 缓存上限热同步（_Cache.set 每次写入读 _max_size）
    assert "_candle_cache._max_size = int(budget[\"cache_max\"])" in src


def test_r13_b9_scan_once_consumes_all_budget_leaves():
    src = inspect.getsource(perception_mod.scan_once)
    assert 'int(budget["max_markets"])' in src
    assert 'int(budget["max_markets_hip3"])' in src
    assert 'int(budget["max_markets_movers"])' in src
    assert 'float(budget["movers_vol_floor_usd"])' in src
    assert 'float(budget["hip3_movers_floor_usd"])' in src
    assert 'budget["movers_min_pct"]' in src
    assert 'int(budget["universe_sweep"])' in src
    assert 'int(budget["batch_size"])' in src
    assert 'float(budget["batch_sleep_sec"])' in src
    assert 'timeout=int(budget["future_timeout_sec"])' in src


def test_r13_b9_scan_once_no_longer_reads_legacy_env_directly():
    """九个 legacy env 的内联 os.environ.get 必须全部移出 scan_once
    （改由 helper 统一解析）；模块级 cache bootstrap 保留在 scan_once 外。"""
    src = inspect.getsource(perception_mod.scan_once)
    for var in (
        "HERMES_MAX_MARKETS", "HERMES_MAX_MARKETS_HIP3", "HERMES_MAX_MARKETS_MOVERS",
        "HERMES_MOVERS_VOL_FLOOR_USD", "HERMES_HIP3_MOVERS_FLOOR_USD",
        "HERMES_UNIVERSE_SWEEP", "HERMES_BATCH_SIZE", "HERMES_BATCH_SLEEP",
        "HERMES_PERCEPTION_CACHE_MAX",
    ):
        assert f'os.environ.get("{var}"' not in src, var
    # 旧硬编码 movers 1.0% 阈值与 timeout=60 也不得残留
    assert ">= 1.0" not in src
    assert "timeout=60" not in src


def test_r13_b9_module_keeps_cache_bootstrap_and_helper():
    """模块级 _candle_cache bootstrap 保留（import 即有缓存），helper 可导入。"""
    assert hasattr(perception_mod, "scan_budget_params")
    assert perception_mod.scan_budget_params is scan_budget_params
    assert hasattr(perception_mod, "_candle_cache")
    assert hasattr(perception_mod, "_CANDLE_CACHE_MAX")
    mod_src = inspect.getsource(perception_mod)
    assert 'os.environ.get("HERMES_PERCEPTION_CACHE_MAX", "512")' in mod_src
    # workers 的防御默认仍在（canonical 解析前的兜底）
    assert "workers = parallel_workers or 32" in mod_src
