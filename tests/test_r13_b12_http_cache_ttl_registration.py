"""R13-B12: HTTP 边缘缓存 TTL + DSL save 退避指数 canonical 登记测试。

修复前，HTTP 边缘缓存的五个旋钮散落在三个模块、且部分停留在 import-time
env 读取或裸字面量：

  * dashboard_routes/public.py——三个 public JSON 轮询端点的 TTL 在 import
    时经 ``os.environ.get("HERMES_SUMMARY_TTL_S", "2.0")`` /
    HERMES_EQUITY_CURVE_TTL_S("30.0") / HERMES_CLOSED_TRADES_TTL_S("10.0")
    读取成模块常量，请求闭包直接引用（改配置要重启、不在 dashboard dump
    里、validate_config 不校验）；
  * server.py——per-coin research verdict 缓存 TTL 在 import 时经
    ``os.environ.get("HERMES_RESEARCH_HTTP_CACHE_S", "30")`` 读取；
  * dashboard.py——TTL 缓存 singleflight 等待者超时 ``_TTL_LOAD_WAIT_S =
    60.0`` 是纯裸字面量，连 env 通道都没有；
  * agents/dsl_exit.py——save 重试退避的指数因子是 ``3 ** attempt`` 里的
    裸字面量 ``3``（dsl_state_io 块 B1 已登记 base/attempts 等 5 叶，唯独
    这个增长因子漏网）。

修复后（零行为变化）：
  * 新增 canonical 块 ``http_cache``（5 叶），默认值逐字镜像旧字面量
    （2.0 / 30.0 / 10.0 / 30.0 / 60.0）；
  * dashboard.py 新增 ``_http_cache_params()`` helper（_HTTP_CACHE_DEFAULTS
    字面量表 + _HTTP_CACHE_SPEC）：逐叶解析链 = legacy env
    （HERMES_SUMMARY_TTL_S / HERMES_EQUITY_CURVE_TTL_S /
    HERMES_CLOSED_TRADES_TTL_S / HERMES_RESEARCH_HTTP_CACHE_S，**最高优先**）
    → cfg_get("http_cache.<leaf>")（HERMES_CFG_HTTP_CACHE__* env +
    agent-config + CANONICAL_DEFAULTS）→ 字面量；float coerce 且须过 >= 0
    guard（TTL=0 合法，表示不写缓存，沿用旧语义）；任何失败整块回退字面量
    独立拷贝，请求渲染绝不抛错；
  * public.py 三个路由在 worker 线程闭包内经 helper 解析 TTL；
    _SUMMARY_TTL_S / _EQUITY_CURVE_TTL_S / _CLOSED_TRADES_TTL_S 模块常量
    保留为 fallback 符号（test_dashboard_config_api 钉死默认值）；
  * server.py ``_research_cached`` 仅在 miss 路径（HIT 早返回、热路径不碰
    config）经 dashboard._http_cache_params() 解析 research_cache_ttl_s；
    _RESEARCH_CACHE_TTL_S 模块常量保留为 fallback 符号；
  * dashboard.py ``_ttl_cached`` 的 waiter 超时仅在并发 singleflight miss
    分支（罕见路径）经 helper 解析 ttl_load_wait_s；缓存命中热路径零 config
    读取；_TTL_LOAD_WAIT_S 常量保留为 fallback 符号；
  * dsl_exit.py 新增 ``_SAVE_BACKOFF_FACTOR``（cfg_get
    dsl_state_io.save_backoff_factor，默认 3，B1 import-time 风格，无 legacy
    env 通道），``3 ** attempt`` 改用该符号；dsl_state_io 块扩成 6 叶。

明确排除：HERMES_PORT（部署端口）、_WEB_DIST / _NO_CACHE_HEADERS（静态资源
/ 安全头）、DSL_STATE_FILE（文件路径）、equity-curve Query 边界
（ge=60/le=2_592_000，API 形状）、_POSTMORTEM_DIR（路径）。
"""

import inspect
import json
import os
import time

import pytest

import hermes_trader.agents.dsl_exit as dsl_mod
import hermes_trader.dashboard as dashboard_mod
import hermes_trader.server as server_mod
from hermes_trader.dashboard_routes import public as public_mod
from hermes_trader.agents import config_store
from hermes_trader.agents.config_schema import _ConfigPatch, validate_config_updates
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get, read_agent_config
from hermes_trader.dashboard import (
    _HTTP_CACHE_DEFAULTS,
    _HTTP_CACHE_SPEC,
    _http_cache_params,
    _ttl_cached,
)

HC_BLOCK = "http_cache"
DS_BLOCK = "dsl_state_io"
HC_LEAVES = (
    "summary_ttl_s", "equity_curve_ttl_s", "closed_trades_ttl_s",
    "research_cache_ttl_s", "ttl_load_wait_s",
)

# 4 个 legacy env（ttl_load_wait_s 无 legacy 通道）。
_LEGACY_ENVS = (
    "HERMES_SUMMARY_TTL_S",
    "HERMES_EQUITY_CURVE_TTL_S",
    "HERMES_CLOSED_TRADES_TTL_S",
    "HERMES_RESEARCH_HTTP_CACHE_S",
)


def _clear_b12_env(monkeypatch):
    """清空 4 个 legacy env + HERMES_CFG_HTTP_CACHE__* /
    HERMES_CFG_DSL_STATE_IO__SAVE_BACKOFF_FACTOR canonical env。"""
    for v in _LEGACY_ENVS:
        monkeypatch.delenv(v, raising=False)
    for k in list(os.environ):
        if k.startswith("HERMES_CFG_HTTP_CACHE__") or \
           k == "HERMES_CFG_DSL_STATE_IO__SAVE_BACKOFF_FACTOR":
            monkeypatch.delenv(k, raising=False)


# ── canonical 登记：http_cache 块存在、叶数 / 类型 / 字面量镜像 ─────────────

def test_r13_b12_http_cache_block_registered():
    assert HC_BLOCK in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS[HC_BLOCK], dict)
    assert len(CANONICAL_DEFAULTS[HC_BLOCK]) == 5
    assert set(CANONICAL_DEFAULTS[HC_BLOCK]) == set(HC_LEAVES)


def test_r13_b12_http_cache_defaults_mirror_helper_literals():
    assert CANONICAL_DEFAULTS[HC_BLOCK] == _HTTP_CACHE_DEFAULTS


def test_r13_b12_individual_leaf_values_sentinel():
    """逐叶 sentinel：锁死 5 个 HTTP 缓存 TTL 默认值（零行为变化基线）。"""
    b = CANONICAL_DEFAULTS[HC_BLOCK]
    assert b["summary_ttl_s"] == 2.0
    assert b["equity_curve_ttl_s"] == 30.0
    assert b["closed_trades_ttl_s"] == 10.0
    assert b["research_cache_ttl_s"] == 30.0
    assert b["ttl_load_wait_s"] == 60.0


def test_r13_b12_all_leaves_are_float():
    for leaf in HC_LEAVES:
        assert isinstance(CANONICAL_DEFAULTS[HC_BLOCK][leaf], float), leaf


def test_r13_b12_dsl_state_io_gains_backoff_factor():
    """dsl_state_io 块从 B1 的 5 叶扩成 6 叶；新叶 save_backoff_factor=3。"""
    block = CANONICAL_DEFAULTS[DS_BLOCK]
    assert "save_backoff_factor" in block
    assert block["save_backoff_factor"] == 3
    assert isinstance(block["save_backoff_factor"], int)
    # B1 原有 5 叶不被破坏
    assert block["save_min_interval_sec"] == 2.0
    assert block["force_load_ttl_s"] == 1.0
    assert block["policy_cache_ttl_s"] == 5.0
    assert block["save_max_attempts"] == 3
    assert block["save_backoff_base_sec"] == 0.1
    assert len(block) == 6


# ── cfg_get 点路径 / 整块：空 config 回退 canonical ───────────────────────

def test_r13_b12_cfg_get_all_hc_leaves():
    for leaf in HC_LEAVES:
        assert cfg_get(f"{HC_BLOCK}.{leaf}", config={}) == _HTTP_CACHE_DEFAULTS[leaf]


def test_r13_b12_cfg_get_full_block():
    b = cfg_get(HC_BLOCK, config={})
    assert isinstance(b, dict) and len(b) == 5
    assert b["summary_ttl_s"] == 2.0 and b["ttl_load_wait_s"] == 60.0


def test_r13_b12_cfg_get_backoff_factor():
    assert cfg_get(f"{DS_BLOCK}.save_backoff_factor", config={}) == 3


# ── HERMES_CFG_ canonical env 通道（float coerce）─────────────────────────

def test_r13_b12_cfg_env_override_float(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_HTTP_CACHE__SUMMARY_TTL_S", "7.5")
    v = cfg_get(f"{HC_BLOCK}.summary_ttl_s", config={})
    assert v == 7.5 and isinstance(v, float)


def test_r13_b12_cfg_env_override_load_wait(monkeypatch):
    # ttl_load_wait_s 没有 legacy env，HERMES_CFG_ 是唯一 env 通道
    monkeypatch.setenv("HERMES_CFG_HTTP_CACHE__TTL_LOAD_WAIT_S", "90")
    v = cfg_get(f"{HC_BLOCK}.ttl_load_wait_s", config={})
    assert v == 90.0 and isinstance(v, float)


def test_r13_b12_cfg_env_override_backoff_factor(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_DSL_STATE_IO__SAVE_BACKOFF_FACTOR", "4")
    v = cfg_get(f"{DS_BLOCK}.save_backoff_factor", config={})
    assert v == 4 and isinstance(v, int)


# ── config dict 部分覆盖：未给叶保留 canonical ────────────────────────────

def test_r13_b12_config_dict_partial_overlay():
    cfg = {HC_BLOCK: {"summary_ttl_s": 5.0, "ttl_load_wait_s": 120.0}}
    assert cfg_get(f"{HC_BLOCK}.summary_ttl_s", config=cfg) == 5.0
    assert cfg_get(f"{HC_BLOCK}.ttl_load_wait_s", config=cfg) == 120.0
    assert cfg_get(f"{HC_BLOCK}.equity_curve_ttl_s", config=cfg) == 30.0
    assert cfg_get(f"{HC_BLOCK}.research_cache_ttl_s", config=cfg) == 30.0


# ── read_agent_config 可见 + 深合并（dashboard dump 通道）─────────────────

def test_r13_b12_read_agent_config_exposes_blocks():
    cfg = read_agent_config()
    assert HC_BLOCK in cfg
    assert cfg[HC_BLOCK]["summary_ttl_s"] == 2.0
    assert cfg[HC_BLOCK]["research_cache_ttl_s"] == 30.0
    assert cfg[HC_BLOCK]["ttl_load_wait_s"] == 60.0
    assert cfg[DS_BLOCK]["save_backoff_factor"] == 3


def test_r13_b12_read_agent_config_deep_merges_partial(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        HC_BLOCK: {"equity_curve_ttl_s": 45.0},
        DS_BLOCK: {"save_backoff_factor": 5},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg[HC_BLOCK]["equity_curve_ttl_s"] == 45.0
    assert cfg[HC_BLOCK]["summary_ttl_s"] == 2.0          # canonical 保留
    assert cfg[DS_BLOCK]["save_backoff_factor"] == 5
    assert cfg[DS_BLOCK]["save_max_attempts"] == 3        # B1 canonical 保留


# ── schema：http_cache 作为 object 被 strict_keys 接受；_ConfigPatch drift ─

def test_r13_b12_schema_accepts_block():
    errors = validate_config_updates(
        {HC_BLOCK: {"summary_ttl_s": 3.0, "ttl_load_wait_s": 75.0}},
        strict_keys=True,
    )
    assert errors == [], errors


def test_r13_b12_config_patch_knows_block():
    """drift sentinel：CANONICAL_DEFAULTS 加块后 _ConfigPatch 必须同步声明。"""
    fields = _ConfigPatch.model_fields
    assert HC_BLOCK in fields
    fb = fields[HC_BLOCK].default_factory()
    assert len(fb) == 5
    assert fb["summary_ttl_s"] == 2.0 and fb["ttl_load_wait_s"] == 60.0


# ── helper：默认叶集 == 字面量、SPEC 映射 / kind / guard 正确 ─────────────

def test_r13_b12_helper_defaults_equal_literals(monkeypatch):
    _clear_b12_env(monkeypatch)
    p = _http_cache_params(config={})
    assert set(p) == set(HC_LEAVES)
    assert p == _HTTP_CACHE_DEFAULTS
    assert p == CANONICAL_DEFAULTS[HC_BLOCK]


def test_r13_b12_helper_signature_takes_config():
    assert "config" in inspect.signature(_http_cache_params).parameters


def test_r13_b12_spec_maps_legacy_envs():
    """4 叶映射 legacy env；ttl_load_wait_s 的 legacy=None（纯裸字面量）。"""
    assert _HTTP_CACHE_SPEC["summary_ttl_s"][0] == "HERMES_SUMMARY_TTL_S"
    assert _HTTP_CACHE_SPEC["equity_curve_ttl_s"][0] == "HERMES_EQUITY_CURVE_TTL_S"
    assert _HTTP_CACHE_SPEC["closed_trades_ttl_s"][0] == "HERMES_CLOSED_TRADES_TTL_S"
    assert _HTTP_CACHE_SPEC["research_cache_ttl_s"][0] == "HERMES_RESEARCH_HTTP_CACHE_S"
    assert _HTTP_CACHE_SPEC["ttl_load_wait_s"][0] is None


def test_r13_b12_spec_kinds_and_guards():
    """全部 float kind；下限 0.0（TTL=0 合法=不写缓存）。"""
    for leaf in HC_LEAVES:
        env_name, kind, min_v = _HTTP_CACHE_SPEC[leaf]
        assert kind == "f", leaf
        assert min_v == 0.0, leaf


# ── legacy env 兼容（硬约束）：仍生效且优先于 canonical 通道 ──────────────

def test_r13_b12_legacy_summary_ttl_flows(monkeypatch):
    _clear_b12_env(monkeypatch)
    monkeypatch.setenv("HERMES_SUMMARY_TTL_S", "4.25")
    p = _http_cache_params(config={})
    assert p["summary_ttl_s"] == 4.25


def test_r13_b12_legacy_research_ttl_flows(monkeypatch):
    _clear_b12_env(monkeypatch)
    monkeypatch.setenv("HERMES_RESEARCH_HTTP_CACHE_S", "12")
    p = _http_cache_params(config={})
    assert p["research_cache_ttl_s"] == 12.0


def test_r13_b12_legacy_env_beats_canonical_env(monkeypatch):
    """legacy env 优先级最高：同时设置时压过 HERMES_CFG_*。"""
    _clear_b12_env(monkeypatch)
    monkeypatch.setenv("HERMES_SUMMARY_TTL_S", "4.0")
    monkeypatch.setenv("HERMES_CFG_HTTP_CACHE__SUMMARY_TTL_S", "9.0")
    p = _http_cache_params(config={})
    assert p["summary_ttl_s"] == 4.0


def test_r13_b12_legacy_env_empty_string_falls_through(monkeypatch):
    """空串视为未设：落到 canonical 通道而非崩溃 / 透传空串。"""
    _clear_b12_env(monkeypatch)
    monkeypatch.setenv("HERMES_SUMMARY_TTL_S", "")
    monkeypatch.setenv("HERMES_CFG_HTTP_CACHE__SUMMARY_TTL_S", "11.0")
    p = _http_cache_params(config={})
    assert p["summary_ttl_s"] == 11.0


# ── canonical env / config dict 经 helper 流向消费方 ──────────────────────

def test_r13_b12_helper_canonical_env_flows(monkeypatch):
    _clear_b12_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_HTTP_CACHE__EQUITY_CURVE_TTL_S", "60.0")
    monkeypatch.setenv("HERMES_CFG_HTTP_CACHE__TTL_LOAD_WAIT_S", "150.0")
    p = _http_cache_params(config={})
    assert p["equity_curve_ttl_s"] == 60.0
    assert p["ttl_load_wait_s"] == 150.0


def test_r13_b12_helper_config_dict_override():
    p = _http_cache_params(config={HC_BLOCK: {
        "closed_trades_ttl_s": 22.0, "ttl_load_wait_s": 88.0,
    }})
    assert p["closed_trades_ttl_s"] == 22.0
    assert p["ttl_load_wait_s"] == 88.0
    assert p["summary_ttl_s"] == 2.0
    assert p["equity_curve_ttl_s"] == 30.0


# ── guard：坏值 / 越界回退字面量，热路径不崩 ──────────────────────────────

def test_r13_b12_guard_negative_ttl_falls_back(monkeypatch):
    _clear_b12_env(monkeypatch)
    monkeypatch.setenv("HERMES_SUMMARY_TTL_S", "-9")
    p = _http_cache_params(config={})
    assert p["summary_ttl_s"] == 2.0


def test_r13_b12_guard_garbage_returns_full_literals(monkeypatch):
    """coerce 失败：helper 整块回退字面量（不抛异常、不留半坏 dict）。"""
    _clear_b12_env(monkeypatch)
    monkeypatch.setenv("HERMES_RESEARCH_HTTP_CACHE_S", "garbage")
    p = _http_cache_params(config={})
    assert p == _HTTP_CACHE_DEFAULTS


def test_r13_b12_guard_zero_ttl_is_legal(monkeypatch):
    """TTL=0 合法（旧 import-time float() 也接受 0）：表示不写缓存。"""
    _clear_b12_env(monkeypatch)
    monkeypatch.setenv("HERMES_SUMMARY_TTL_S", "0")
    p = _http_cache_params(config={})
    assert p["summary_ttl_s"] == 0.0


def test_r13_b12_helper_returns_independent_copy(monkeypatch):
    """每次返回独立 dict，调用方 mutate 不污染字面量表 / 彼此隔离。"""
    _clear_b12_env(monkeypatch)
    a = _http_cache_params(config={})
    a["summary_ttl_s"] = 999.0
    b = _http_cache_params(config={})
    assert b["summary_ttl_s"] == 2.0
    assert _HTTP_CACHE_DEFAULTS["summary_ttl_s"] == 2.0


# ── 模块属性保留（硬约束）：fallback 符号不删、默认值不变 ──────────────────

def test_r13_b12_public_module_constants_kept():
    """test_dashboard_config_api L243-245 钉死：模块属性必须保留且值不变。"""
    assert public_mod._SUMMARY_TTL_S == 2.0
    assert public_mod._EQUITY_CURVE_TTL_S == 30.0
    assert public_mod._CLOSED_TRADES_TTL_S == 10.0


def test_r13_b12_server_module_constant_kept():
    assert server_mod._RESEARCH_CACHE_TTL_S == 30.0


def test_r13_b12_dashboard_wait_constant_kept():
    assert dashboard_mod._TTL_LOAD_WAIT_S == 60.0


def test_r13_b12_dsl_backoff_factor_symbol():
    assert dsl_mod._SAVE_BACKOFF_FACTOR == 3
    assert isinstance(dsl_mod._SAVE_BACKOFF_FACTOR, int)


# ── 热路径接线：源码断言（helper 注入 + 消费点 + 旧内联引用清除）──────────

def test_r13_b12_modules_expose_helper():
    assert hasattr(dashboard_mod, "_http_cache_params")
    assert dashboard_mod._http_cache_params is _http_cache_params
    assert hasattr(server_mod.dashboard, "_http_cache_params")
    # public.py 从 dashboard 导入 helper
    assert hasattr(public_mod, "_http_cache_params")


def test_r13_b12_public_routes_resolve_ttl_via_helper():
    """三个路由的 worker 闭包经 _http_cache_params() 取 TTL，不再直接引用
    模块常量（常量本身保留为 fallback 符号）。"""
    src = inspect.getsource(public_mod.register_public_routes)
    assert '_http_cache_params()["summary_ttl_s"]' in src
    assert '_http_cache_params()["equity_curve_ttl_s"]' in src
    assert '_http_cache_params()["closed_trades_ttl_s"]' in src
    # 旧的直接模块常量传参不得残留在 to_thread 调用里
    assert "_ttl_cached, \"summary\", _SUMMARY_TTL_S" not in src
    assert "_EQUITY_CURVE_TTL_S,\n" not in src
    assert "_CLOSED_TRADES_TTL_S,\n" not in src


def test_r13_b12_server_research_resolves_ttl_via_helper():
    src = inspect.getsource(server_mod._research_cached)
    assert 'dashboard._http_cache_params().get("research_cache_ttl_s"' in src
    # miss 路径改用局部 ttl_s；旧的 _RESEARCH_CACHE_TTL_S 消费点不得残留
    assert "if ttl_s > 0:" in src
    assert "time.time() + ttl_s" in src
    assert "if _RESEARCH_CACHE_TTL_S > 0:" not in src
    assert "time.time() + _RESEARCH_CACHE_TTL_S" not in src


def test_r13_b12_ttl_cached_resolves_wait_via_helper():
    src = inspect.getsource(_ttl_cached)
    assert '_http_cache_params().get("ttl_load_wait_s"' in src
    assert "ev.wait(timeout=wait_s)" in src
    assert "ev2.wait(timeout=wait_s)" in src
    # 旧的裸常量消费点不得残留（常量定义本身保留）
    assert "ev.wait(timeout=_TTL_LOAD_WAIT_S)" not in src


def test_r13_b12_dsl_save_uses_backoff_factor_symbol():
    src = inspect.getsource(dsl_mod._save_state)
    assert "_SAVE_BACKOFF_FACTOR ** attempt" in src
    assert "3 ** attempt" not in src
    # 模块常量经 cfg_get 登记
    assert 'cfg_get("dsl_state_io.save_backoff_factor"' in inspect.getsource(dsl_mod)


# ── 行为 sentinel：TTL 缓存语义零变化（默认 TTL 下命中 / 过期 / TTL=0）─────

def test_r13_b12_ttl_cached_hit_skips_loader(monkeypatch):
    """默认链路：同一 key 在 TTL 内第二次调用不重跑 loader（缓存命中）。"""
    _clear_b12_env(monkeypatch)
    dashboard_mod._TTL_CACHE.clear()
    dashboard_mod._TTL_INFLIGHT.clear()
    calls = {"n": 0}

    def _loader():
        calls["n"] += 1
        return {"v": calls["n"]}

    r1 = _ttl_cached("b12-hit", 100.0, _loader)
    r2 = _ttl_cached("b12-hit", 100.0, _loader)
    assert r1 == r2 == {"v": 1}
    assert calls["n"] == 1


def test_r13_b12_ttl_cached_expiry_reruns_loader(monkeypatch):
    """TTL 到期后重跑 loader（默认 TTL 行为不变）。"""
    _clear_b12_env(monkeypatch)
    dashboard_mod._TTL_CACHE.clear()
    dashboard_mod._TTL_INFLIGHT.clear()
    counter = {"n": 0}

    def _loader():
        counter["n"] += 1
        return counter["n"]

    assert _ttl_cached("b12-exp", 0.0, _loader) == 1   # ttl=0 → 不命中
    time.sleep(0.01)
    assert _ttl_cached("b12-exp", 0.0, _loader) == 2   # 再 miss


def test_r13_b12_helper_drives_ttl_zero_disables_cache(monkeypatch):
    """端到端：helper 解析出 TTL=0（legacy env）时，_ttl_cached 每次 miss
    （缓存写了也立即过期）——证明 helper 值真的流进 TTL 位、语义零变化。"""
    _clear_b12_env(monkeypatch)
    monkeypatch.setenv("HERMES_CLOSED_TRADES_TTL_S", "0")
    dashboard_mod._TTL_CACHE.clear()
    dashboard_mod._TTL_INFLIGHT.clear()
    ttl = _http_cache_params(config={})["closed_trades_ttl_s"]
    assert ttl == 0.0
    counter = {"n": 0}

    def _loader():
        counter["n"] += 1
        return counter["n"]

    _ttl_cached("b12-zero", ttl, _loader)
    _ttl_cached("b12-zero", ttl, _loader)
    assert counter["n"] == 2
