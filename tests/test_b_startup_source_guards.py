# -*- coding: utf-8 -*-
"""B-7 / B-10：生产权威源（/data/.agent-config.json）启动守卫测试。

这两项守卫是 source-gated：仅当 CONFIG_PATH 指向容器挂载的
/data/.agent-config.json 时才强制；本地/CI（canonical 默认）一律放行，避免
误报。这里通过 monkeypatch CONFIG_PATH 与 _read_raw_config 验证：
  B-7  关键顶键缺失 / 文件非对象 → 报错；完整 142 键口径 → 通过；
  B-10 生产配置显式关 noise_band → 报错；保持开启 → 通过；
  非 /data 源 → 两项都不触发。
"""
from __future__ import annotations

from hermes_trader.agents import config_store as cs


def _full_prod_raw():
    """一份关键键齐全、noise_band 开启的生产原始配置（最小但满足守卫）。"""
    return {
        "mode": "SHADOW", "leverage": 10, "max_concurrent": 2,
        "max_trade_notional_usd": 30.0,
        # max_daily_loss_usd 须等于 config_parity_guard._REVIEWED_SAFE_DIVERGENCES
        # 登记的复核值 (-3)，否则 P0-A 修复后的启动 parity 门禁会把它当作值漂移
        # 报红（recheck-4 P0-A，2026-10-18）。
        "max_daily_loss_usd": -3,
        "runner_entry_gate": {},
        "dsl_exit": {"noise_band": {"enabled": True, "atr_mult": 0.8}},
        # 灰度 enforce 臂：生产基线已 enforce，守卫要求保持。
        # P0-2 (2026-10-16)：_GRAY_ENFORCE_ARMS 现含 5 个 enforce 块，夹具须覆盖全部。
        "market_circuit": {"mode": "enforce"},
        "daily_extension_cap": {"mode": "enforce"},
        "trend_filter_200ma": {"mode": "enforce"},
        "ta_late_entry": {"mode": "enforce"},
        "reasoning_effort_rollout": {"mode": "enforce"},
    }


def test_non_data_source_enforces_nothing(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/home/dev/.agent-config.json")
    assert cs.startup_config_integrity_errors({}) == []


def test_b7_full_production_config_passes(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/data/.agent-config.json")
    monkeypatch.setattr(cs, "_read_raw_config", lambda: _full_prod_raw())
    monkeypatch.setattr(cs.os.path, "exists", lambda p: True)
    assert cs.startup_config_integrity_errors({}) == []


def test_parity_gate_reports_value_drift_on_production(monkeypatch):
    """recheck-4 P0-A 防复发：启动 parity 门禁必须拿到真实结果，异常不得被吞。

    此前 `_production_parity_uncovered_errors` 误用 `_g.CANONICAL_DEFAULTS`
    （config_parity_guard 无此属性）抛 AttributeError 被 except 记 non-fatal 吞掉，
    返回 []，启动拦截 100% 失效。本测试强制生产分支，喂一个值漂移的 live 配置
    （max_daily_loss_usd = -500，登记复核值 -3），必须返回非空且含该叶；喂已复核
    覆盖的配置必须返回空（无假阳性）。
    """
    # 值漂移 → 门禁红
    monkeypatch.setattr(cs, "CONFIG_PATH", "/data/.agent-config.json")
    raw_drift = _full_prod_raw()
    raw_drift["max_daily_loss_usd"] = -500
    monkeypatch.setattr(cs, "_read_raw_config", lambda: raw_drift)
    monkeypatch.setattr(cs.os.path, "exists", lambda p: True)
    errors = cs._production_parity_uncovered_errors()
    assert errors, "P0-A 门禁应报告值漂移，却返回空（异常被吞？）"
    assert any("max_daily_loss_usd" in e for e in errors), errors

    # 已复核覆盖 → 门禁绿（无假阳性）
    monkeypatch.setattr(cs, "_read_raw_config", lambda: _full_prod_raw())
    assert cs._production_parity_uncovered_errors() == []

    # 非 /data 源 → 不检查
    monkeypatch.setattr(cs, "CONFIG_PATH", "/home/dev/.agent-config.json")
    assert cs._production_parity_uncovered_errors() == []


def test_b7_missing_required_key_is_startup_error(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/data/.agent-config.json")
    raw = _full_prod_raw()
    del raw["leverage"]
    monkeypatch.setattr(cs, "_read_raw_config", lambda: raw)
    monkeypatch.setattr(cs.os.path, "exists", lambda p: True)
    errors = cs.startup_config_integrity_errors({})
    assert any("leverage" in e and "required" in e for e in errors), errors


def test_b7_non_object_is_startup_error(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/data/.agent-config.json")
    monkeypatch.setattr(cs, "_read_raw_config", lambda: ["not", "object"])
    monkeypatch.setattr(cs.os.path, "exists", lambda p: True)
    errors = cs.startup_config_integrity_errors({})
    assert any("not a JSON object" in e for e in errors), errors


def test_b10_noise_band_disabled_on_production_is_error(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/data/.agent-config.json")
    raw = _full_prod_raw()
    raw["dsl_exit"]["noise_band"] = {"enabled": False}
    monkeypatch.setattr(cs, "_read_raw_config", lambda: raw)
    monkeypatch.setattr(cs.os.path, "exists", lambda p: True)
    errors = cs.startup_config_integrity_errors({})
    assert any("noise_band" in e for e in errors), errors


def test_b10_noise_band_enabled_on_production_passes(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/data/.agent-config.json")
    monkeypatch.setattr(cs, "_read_raw_config", lambda: _full_prod_raw())
    monkeypatch.setattr(cs.os.path, "exists", lambda p: True)
    errors = cs.startup_config_integrity_errors({})
    assert not any("noise_band" in e for e in errors), errors


def test_enforce_arm_downgrade_is_startup_error(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/data/.agent-config.json")
    raw = _full_prod_raw()
    raw["market_circuit"] = {"mode": "shadow"}
    monkeypatch.setattr(cs, "_read_raw_config", lambda: raw)
    monkeypatch.setattr(cs.os.path, "exists", lambda p: True)
    errors = cs.startup_config_integrity_errors({})
    assert any("market_circuit" in e for e in errors), errors


def test_enforce_arm_missing_block_is_startup_error(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/data/.agent-config.json")
    raw = _full_prod_raw()
    del raw["daily_extension_cap"]
    monkeypatch.setattr(cs, "_read_raw_config", lambda: raw)
    monkeypatch.setattr(cs.os.path, "exists", lambda p: True)
    errors = cs.startup_config_integrity_errors({})
    assert any("daily_extension_cap" in e for e in errors), errors


def test_enforce_arm_non_data_source_enforces_nothing(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/home/dev/.agent-config.json")
    assert cs._production_enforce_arm_errors() == []
