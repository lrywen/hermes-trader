"""R13-B1: DSL 状态文件 I/O 5 个隐式 TTL/重试参数注册测试。

修复前，``dsl_exit.py`` 顶部 5 个模块级常量（_MIN_SAVE_INTERVAL_SEC=2.0 /
_FORCE_LOAD_TTL_S=1.0 / _POLICY_CACHE_TTL_S=5.0 / _SAVE_MAX_ATTEMPTS=3 /
_SAVE_BACKOFF_BASE_SEC=0.1）通过 ``os.environ.get(HERMES_DSL_*, <literal>)``
加载，**仅**通过环境变量注入。它们从未进入 ``CANONICAL_DEFAULTS``，导致：
  * MCP server / 运维面板 / ``.agent-config.json`` 都不能观测或覆写
  * ``validate_config_updates`` 的 Pydantic 校验流不包含这些键
  * 一旦部署环境磁盘慢或网络卡顿，运维要调"再重试几次"或"降低落盘频率"
    只能改环境变量，无任何回滚 / 漂移检测

修复后：
  * 5 个常量登记到 ``CANONICAL_DEFAULTS["dsl_state_io"]``（默认值与现有实
    现严格一致，零业务行为变化）
  * ``_ConfigPatch`` 声明 ``dsl_state_io`` 字段（drift sentinel）
  * 5 个 module-load 常量改为 ``cfg_get(...)`` 兑底，**保留** legacy
    ``HERMES_DSL_*`` env 优先级（operator override 向后兼容）
  * canonical env 路由 ``HERMES_CFG_DSL_STATE_IO__*`` 也可工作
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_trader.agents import config_store
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# The five module-load constants are evaluated ONCE at import from env/config.
# Verifying that an env var changes them therefore used to require
# ``importlib.reload(dsl_exit)`` in-process. That reload RE-DEFINES the
# RetraceTier / ExitPolicy / DSLTracker class objects, so any sibling test
# module that did ``from hermes_trader.agents.dsl_exit import RetraceTier``
# before the reload kept a STALE class: ``isinstance(t, RetraceTier)`` then
# returned False for freshly-built tiers even though their repr was identical
# (order-dependent failure of test_p3_regime_exit_common under pytest-xdist).
# These constant-resolution tests instead run in a fresh short-lived SUBPROCESS,
# which re-imports dsl_exit cleanly with the desired env and never disturbs the
# class identity of the pytest worker process.


def _read_const_subprocess(const_name: str, *, extra_env: dict | None = None):
    """Import dsl_exit in a clean subprocess and return its module constant.

    The subprocess inherits conftest's isolated env (state files on tmpfs) and
    any extra_env overrides; HERMES_DSL_* / HERMES_CFG_DSL_STATE_IO__* from the
    host are stripped so each case observes exactly the vars it sets.
    """
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("HERMES_DSL_", "HERMES_CFG_DSL_STATE_IO__")):
            del env[k]
    if extra_env:
        env.update(extra_env)
    env["PYTHONPATH"] = str(REPO_ROOT)
    code = (
        "import json, hermes_trader.agents.dsl_exit as d;"
        f"print(json.dumps(d.{const_name}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())



# ── canonical 登记：dsl_state_io 块 5 字段默认值与现有 literals 一致 ─────

def test_r13_b1_dsl_state_io_block_registered():
    assert "dsl_state_io" in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS["dsl_state_io"], dict)


def test_r13_b1_dsl_state_io_defaults_match_historical_literals():
    """默认值严格等于 dsl_exit.py 旧硬编码字面量；零行为变化。"""
    block = CANONICAL_DEFAULTS["dsl_state_io"]
    assert block["save_min_interval_sec"] == 2.0
    assert block["force_load_ttl_s"] == 1.0
    assert block["policy_cache_ttl_s"] == 5.0
    assert block["save_max_attempts"] == 3
    assert block["save_backoff_base_sec"] == 0.1


# ── cfg_get 解析：嵌套块支持点路径 + 空 config 回退 canonical ─────────────

@pytest.mark.parametrize("dotted_key,expected", [
    ("dsl_state_io.save_min_interval_sec", 2.0),
    ("dsl_state_io.force_load_ttl_s", 1.0),
    ("dsl_state_io.policy_cache_ttl_s", 5.0),
    ("dsl_state_io.save_max_attempts", 3),
    ("dsl_state_io.save_backoff_base_sec", 0.1),
])
def test_r13_b1_cfg_get_resolves_canonical_default(dotted_key, expected):
    assert cfg_get(dotted_key, config={}) == expected


def test_r13_b1_cfg_get_full_block():
    block = cfg_get("dsl_state_io", config={})
    assert isinstance(block, dict)
    assert block["save_min_interval_sec"] == 2.0
    assert block["save_max_attempts"] == 3


# ── env 覆盖：canonical env 路由（HERMES_CFG_*__*）───────────────────────

def test_r13_b1_env_override_save_min_interval(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_DSL_STATE_IO__SAVE_MIN_INTERVAL_SEC", "0.5")
    assert cfg_get("dsl_state_io.save_min_interval_sec", config={}) == 0.5


def test_r13_b1_env_override_save_max_attempts(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_DSL_STATE_IO__SAVE_MAX_ATTEMPTS", "7")
    assert cfg_get("dsl_state_io.save_max_attempts", config={}) == 7


def test_r13_b1_env_override_save_backoff_base(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_DSL_STATE_IO__SAVE_BACKOFF_BASE_SEC", "0.25")
    assert cfg_get("dsl_state_io.save_backoff_base_sec", config={}) == 0.25


# ── config dict 覆盖：部分子键深合并 ───────────────────────────────────

def test_r13_b1_config_dict_partial_overlay():
    cfg = {"dsl_state_io": {"save_max_attempts": 5, "save_backoff_base_sec": 0.5}}
    assert cfg_get("dsl_state_io.save_max_attempts", config=cfg) == 5
    assert cfg_get("dsl_state_io.save_backoff_base_sec", config=cfg) == 0.5
    # 未覆盖子键留 canonical 默认
    assert cfg_get("dsl_state_io.save_min_interval_sec", config=cfg) == 2.0
    assert cfg_get("dsl_state_io.policy_cache_ttl_s", config=cfg) == 5.0


# ── read_agent_config 完整可见：dashboard dump 包含新键 ──────────────────

def test_r13_b1_read_agent_config_exposes_block():
    cfg = read_agent_config()
    assert "dsl_state_io" in cfg
    assert cfg["dsl_state_io"]["save_min_interval_sec"] == 2.0
    assert cfg["dsl_state_io"]["save_max_attempts"] == 3


def test_r13_b1_read_agent_config_deep_merges_partial_overlay(tmp_path, monkeypatch):
    """磁盘配置只写部分 dsl_state_io 键时，深合并保留其余 canonical 默认。"""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "dsl_state_io": {"save_max_attempts": 10, "save_backoff_base_sec": 0.2},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg["dsl_state_io"]["save_max_attempts"] == 10        # 覆盖
    assert cfg["dsl_state_io"]["save_backoff_base_sec"] == 0.2   # 覆盖
    assert cfg["dsl_state_io"]["save_min_interval_sec"] == 2.0    # canonical
    assert cfg["dsl_state_io"]["force_load_ttl_s"] == 1.0        # canonical
    assert cfg["dsl_state_io"]["policy_cache_ttl_s"] == 5.0      # canonical


# ── schema 兼容：dsl_state_io 块作为 object 整体被接受 ──────────────────

def test_r13_b1_schema_accepts_dsl_state_io_block():
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "dsl_state_io": {
            "save_min_interval_sec": 1.0,
            "force_load_ttl_s": 0.5,
            "policy_cache_ttl_s": 3.0,
            "save_max_attempts": 5,
            "save_backoff_base_sec": 0.2,
        },
    }, strict_keys=True)
    assert not any("unknown key" in e for e in errors), errors
    assert errors == [], errors


# ── _ConfigPatch drift sentinel：dsl_state_io 字段被 Pydantic 知晓 ───────

def test_r13_b1_config_patch_knows_dsl_state_io_field():
    from hermes_trader.agents.config_schema import _ConfigPatch
    fields = _ConfigPatch.model_fields
    assert "dsl_state_io" in fields
    block = fields["dsl_state_io"].default_factory()
    assert block["save_min_interval_sec"] == 2.0
    assert block["save_max_attempts"] == 3


# ── dsl_exit.py 接线验证：5 个常量实际从 cfg_get / env 读取 ──────────────
# 见文件顶部说明：这些常量在 import 时求值，env 覆盖测试在隔离子进程里重新
# import，避免 importlib.reload 在 pytest 进程内重定义 RetraceTier 等类。

def test_r13_b1_dsl_exit_min_save_interval_uses_canonical():
    """无 env 时，_MIN_SAVE_INTERVAL_SEC = 2.0（canonical 默认）。"""
    assert _read_const_subprocess("_MIN_SAVE_INTERVAL_SEC") == 2.0


def test_r13_b1_dsl_exit_force_load_ttl_uses_canonical():
    assert _read_const_subprocess("_FORCE_LOAD_TTL_S") == 1.0


def test_r13_b1_dsl_exit_policy_cache_ttl_uses_canonical():
    assert _read_const_subprocess("_POLICY_CACHE_TTL_S") == 5.0


def test_r13_b1_dsl_exit_save_max_attempts_uses_canonical():
    assert _read_const_subprocess("_SAVE_MAX_ATTEMPTS") == 3


def test_r13_b1_dsl_exit_save_backoff_base_uses_canonical():
    assert _read_const_subprocess("_SAVE_BACKOFF_BASE_SEC") == 0.1


# ── 优先级：legacy HERMES_DSL_* env 仍然生效（operator override 保留） ───

def test_r13_b1_legacy_env_save_interval_wins():
    """HERMES_DSL_SAVE_INTERVAL_SEC=10 必须盖掉 canonical 2.0 默认。"""
    assert _read_const_subprocess(
        "_MIN_SAVE_INTERVAL_SEC",
        extra_env={"HERMES_DSL_SAVE_INTERVAL_SEC": "10"}) == 10.0


def test_r13_b1_legacy_env_save_max_attempts_wins():
    assert _read_const_subprocess(
        "_SAVE_MAX_ATTEMPTS",
        extra_env={"HERMES_DSL_SAVE_MAX_ATTEMPTS": "8"}) == 8


def test_r13_b1_legacy_env_save_backoff_base_wins():
    assert _read_const_subprocess(
        "_SAVE_BACKOFF_BASE_SEC",
        extra_env={"HERMES_DSL_SAVE_BACKOFF_BASE_SEC": "0.5"}) == 0.5


def test_r13_b1_legacy_env_force_load_ttl_wins():
    assert _read_const_subprocess(
        "_FORCE_LOAD_TTL_S",
        extra_env={"HERMES_DSL_FORCE_LOAD_TTL_S": "3.0"}) == 3.0


def test_r13_b1_legacy_env_policy_cache_ttl_wins():
    assert _read_const_subprocess(
        "_POLICY_CACHE_TTL_S",
        extra_env={"HERMES_DSL_POLICY_CACHE_TTL_S": "30.0"}) == 30.0


# ── 优先级：canonical env (HERMES_CFG_*) 也可工作（无 legacy env 时） ───

def test_r13_b1_canonical_env_save_min_interval():
    assert _read_const_subprocess(
        "_MIN_SAVE_INTERVAL_SEC",
        extra_env={"HERMES_CFG_DSL_STATE_IO__SAVE_MIN_INTERVAL_SEC": "0.25"}) == 0.25


def test_r13_b1_canonical_env_save_max_attempts():
    assert _read_const_subprocess(
        "_SAVE_MAX_ATTEMPTS",
        extra_env={"HERMES_CFG_DSL_STATE_IO__SAVE_MAX_ATTEMPTS": "6"}) == 6


# ── 优先级：legacy env 盖过 canonical env（向后兼容） ──────────────────

def test_r13_b1_legacy_env_beats_canonical_env():
    """两者都设时，legacy HERMES_DSL_* 必须赢。"""
    assert _read_const_subprocess(
        "_MIN_SAVE_INTERVAL_SEC",
        extra_env={"HERMES_DSL_SAVE_INTERVAL_SEC": "10",
                   "HERMES_CFG_DSL_STATE_IO__SAVE_MIN_INTERVAL_SEC": "0.25"}) == 10.0
