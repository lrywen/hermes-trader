"""R13-B2: exchange-SL mover (executor.py) 隐式参数注册测试。

修复前，executor.py L304-311 的 3 个模块级常量控制 Phase 2 trailing
exchange-SL coordination：
  * _SL_MOVE_MIN_INTERVAL_SEC = 30.0 — per-coin throttle on batchModify
  * _SL_MOVE_MIN_BPS         = 15.0 — minimum bps-move to justify
                                      cancel+replace
  * _SL_BUFFER_BPS           = 10.0 — bps behind DSL floor (long: below;
                                      short: above) so DSL fires first

``sl_buffer_bps`` 在 R12-C1 已登记到 CANONICAL_DEFAULTS，且 executor
L2389 已通过 ``cfg_get("sl_buffer_bps", _SL_BUFFER_BPS)`` 接线——是「已
注册 + 已接线」状态。

但 ``_SL_MOVE_MIN_INTERVAL_SEC`` 和 ``_SL_MOVE_MIN_BPS`` 仍是裸模块字
面量，既不进入 CANONICAL_DEFAULTS，hot-path 也不用 cfg_get——是 R13-B2
需要修复的真正隐式字段。修复方式：

  * 在 CANONICAL_DEFAULTS 新增 ``sl_move`` 嵌套块（min_interval_sec=30.0
    / min_bps=15.0，默认值与原 literals 严格一致）
  * _ConfigPatch 声明 ``sl_move`` 字段（drift sentinel）
  * executor.py module symbol 在 import 时通过 ``cfg_get(...)`` 解析为
    canonical 默认（保留向后兼容：verify_dsl_sl_sync.py 等测试脚本读这
    些模块符号仍能看到正确的数）
  * hot-path 比较（L2448, L2455）每次通过 ``cfg_get(...)`` 重新解析，使
    运行时 ``.agent-config.json`` 编辑可在下一 tick 生效，无需重启
"""

import json
import os
import sys
from pathlib import Path

import pytest

from hermes_trader.agents import config_store
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)


# ── canonical 登记：sl_move 块 2 字段默认值与原 literals 严格一致 ──────

def test_r13_b2_sl_move_block_registered():
    assert "sl_move" in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS["sl_move"], dict)


def test_r13_b2_sl_move_defaults_match_historical_literals():
    """默认值严格等于 executor.py 旧硬编码字面量；零行为变化。"""
    block = CANONICAL_DEFAULTS["sl_move"]
    assert block["min_interval_sec"] == 30.0
    assert block["min_bps"] == 15.0


def test_r13_b2_sl_buffer_bps_still_registered():
    """sl_buffer_bps 在 R12-C1 已登记；本 PR 不动它，确保仍然存在。"""
    assert "sl_buffer_bps" in CANONICAL_DEFAULTS
    assert CANONICAL_DEFAULTS["sl_buffer_bps"] == 10.0


# ── cfg_get 解析：嵌套块支持点路径 + 空 config 回退 canonical ──────────

def test_r13_b2_cfg_get_min_interval():
    assert cfg_get("sl_move.min_interval_sec", config={}) == 30.0


def test_r13_b2_cfg_get_min_bps():
    assert cfg_get("sl_move.min_bps", config={}) == 15.0


def test_r13_b2_cfg_get_full_block():
    block = cfg_get("sl_move", config={})
    assert isinstance(block, dict)
    assert block["min_interval_sec"] == 30.0
    assert block["min_bps"] == 15.0


# ── env 覆盖：canonical env 路由（HERMES_CFG_*__*）─────────────────────

def test_r13_b2_env_override_min_interval(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_SL_MOVE__MIN_INTERVAL_SEC", "60.0")
    assert cfg_get("sl_move.min_interval_sec", config={}) == 60.0


def test_r13_b2_env_override_min_bps(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_SL_MOVE__MIN_BPS", "25.0")
    assert cfg_get("sl_move.min_bps", config={}) == 25.0


# ── config dict 覆盖：部分子键深合并 ──────────────────────────────────

def test_r13_b2_config_dict_partial_overlay():
    cfg = {"sl_move": {"min_interval_sec": 60.0}}
    assert cfg_get("sl_move.min_interval_sec", config=cfg) == 60.0
    # 未覆盖子键留 canonical 默认
    assert cfg_get("sl_move.min_bps", config=cfg) == 15.0


# ── read_agent_config 完整可见：dashboard dump 包含新键 ───────────────

def test_r13_b2_read_agent_config_exposes_block():
    cfg = read_agent_config()
    assert "sl_move" in cfg
    assert cfg["sl_move"]["min_interval_sec"] == 30.0
    assert cfg["sl_move"]["min_bps"] == 15.0


def test_r13_b2_read_agent_config_deep_merges_partial_overlay(tmp_path, monkeypatch):
    """磁盘配置只写部分 sl_move 键时，深合并保留其余 canonical 默认。"""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "sl_move": {"min_bps": 25.0},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg["sl_move"]["min_bps"] == 25.0              # 覆盖
    assert cfg["sl_move"]["min_interval_sec"] == 30.0     # canonical


# ── schema 兼容：sl_move 块作为 object 整体被接受 ─────────────────────

def test_r13_b2_schema_accepts_sl_move_block():
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "sl_move": {"min_interval_sec": 60.0, "min_bps": 25.0},
    }, strict_keys=True)
    assert not any("unknown key" in e for e in errors), errors
    assert errors == [], errors


# ── _ConfigPatch drift sentinel：sl_move 字段被 Pydantic 知晓 ──────────

def test_r13_b2_config_patch_knows_sl_move_field():
    from hermes_trader.agents.config_schema import _ConfigPatch
    fields = _ConfigPatch.model_fields
    assert "sl_move" in fields
    block = fields["sl_move"].default_factory()
    assert block["min_interval_sec"] == 30.0
    assert block["min_bps"] == 15.0


# ── executor.py 接线验证：module symbol 仍可见 + hot-path 可重新解析 ──

def test_r13_b2_executor_module_symbols_resolve_to_canonical():
    """executor._SL_MOVE_MIN_INTERVAL_SEC / _SL_MOVE_MIN_BPS 仍可见，
    解析为 canonical 默认（向后兼容 verify_dsl_sl_sync.py 等）。"""
    from hermes_trader.agents import executor
    assert executor._SL_MOVE_MIN_INTERVAL_SEC == 30.0
    assert executor._SL_MOVE_MIN_BPS == 15.0
    assert executor._SL_BUFFER_BPS == 10.0


def test_r13_b2_executor_cfg_get_hot_path_returns_canonical():
    """hot-path 重新解析走 cfg_get；空 config 时回退到 canonical 默认。"""
    from hermes_trader.agents import executor
    # 直接调 cfg_get 与 module symbol 的回退关系
    assert cfg_get("sl_move.min_interval_sec", executor._SL_MOVE_MIN_INTERVAL_SEC) == 30.0
    assert cfg_get("sl_move.min_bps", executor._SL_MOVE_MIN_BPS) == 15.0


# ── hot-path 行为：cfg_get 重解析可在不重启情况下生效 ──────────────────

def test_r13_b2_executor_hot_path_uses_overridden_min_bps(monkeypatch):
    """运行时把 min_bps override 到 100，hot-path cfg_get 必须读到 100。"""
    from hermes_trader.agents import executor
    # 模拟 .agent-config.json 已被运营改成 min_bps=100
    override_cfg = {"sl_move": {"min_bps": 100.0}}
    live = cfg_get("sl_move.min_bps", executor._SL_MOVE_MIN_BPS, config=override_cfg)
    assert live == 100.0
    # 同样的调用，override 撤掉时回到 module symbol
    live_default = cfg_get("sl_move.min_bps", executor._SL_MOVE_MIN_BPS)
    assert live_default == 15.0


def test_r13_b2_executor_hot_path_uses_overridden_min_interval(monkeypatch):
    from hermes_trader.agents import executor
    override_cfg = {"sl_move": {"min_interval_sec": 5.0}}
    live = cfg_get("sl_move.min_interval_sec", executor._SL_MOVE_MIN_INTERVAL_SEC, config=override_cfg)
    assert live == 5.0
    live_default = cfg_get("sl_move.min_interval_sec", executor._SL_MOVE_MIN_INTERVAL_SEC)
    assert live_default == 30.0


# ── 旧 R12-C1 sl_buffer_bps 接线不能回归 ──────────────────────────────

def test_r13_b2_sl_buffer_bps_still_cfg_get_in_executor():
    """R12-C1 已接线 sl_buffer_bps → cfg_get(\"sl_buffer_bps\",
    _SL_BUFFER_BPS)；本 PR 不能改这条路径。"""
    from hermes_trader.agents import executor
    # 模拟 executor._maybe_move_sl_hot_path 内的 cfg_get 调用形式
    live = cfg_get("sl_buffer_bps", executor._SL_BUFFER_BPS)
    assert live == 10.0  # canonical 默认
    override_cfg = {"sl_buffer_bps": 20.0}
    live_override = cfg_get("sl_buffer_bps", executor._SL_BUFFER_BPS, config=override_cfg)
    assert live_override == 20.0


# ── 端到端：与 R13-A1 同模板的「可观测性 / 可审计 / 可覆写」三段断言 ──

def test_r13_b2_sl_move_visible_to_mcp_via_read_agent_config():
    """MCP server / dashboard / validate_config_updates 都能看到 sl_move
    块（与 R13-A1 scan 块同模板）。"""
    cfg = read_agent_config()
    sl = cfg.get("sl_move")
    assert sl is not None
    assert isinstance(sl, dict)
    # 报告值必须包含 min_interval_sec 与 min_bps
    assert "min_interval_sec" in sl
    assert "min_bps" in sl
    # 报告值 = canonical 默认（运营未改 .agent-config.json）
    assert sl["min_interval_sec"] == 30.0
    assert sl["min_bps"] == 15.0


def test_r13_b2_sl_move_audit_visible_with_sentinel():
    """drift sentinel：将来如果有人从 CANONICAL_DEFAULTS 加新键却忘了在
    _ConfigPatch 声明，本断言会捕获 schema 漂移。"""
    from hermes_trader.agents.config_schema import _ConfigPatch
    canonical_keys = set(CANONICAL_DEFAULTS["sl_move"].keys())
    patch_default_keys = set(_ConfigPatch.model_fields["sl_move"].default_factory().keys())
    # 必须严格相等（任何一方有对方没有的 key = drift）
    assert canonical_keys == patch_default_keys, (
        f"sl_move drift: canonical={canonical_keys}, "
        f"schema default={patch_default_keys}"
    )
