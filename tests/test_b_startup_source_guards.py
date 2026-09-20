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
        "max_trade_notional_usd": 30.0, "max_daily_loss_usd": -50.0,
        "runner_entry_gate": {},
        "dsl_exit": {"noise_band": {"enabled": True, "atr_mult": 0.8}},
    }


def test_non_data_source_enforces_nothing(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/home/dev/.agent-config.json")
    assert cs.startup_config_integrity_errors({}) == []


def test_b7_full_production_config_passes(monkeypatch):
    monkeypatch.setattr(cs, "CONFIG_PATH", "/data/.agent-config.json")
    monkeypatch.setattr(cs, "_read_raw_config", lambda: _full_prod_raw())
    monkeypatch.setattr(cs.os.path, "exists", lambda p: True)
    assert cs.startup_config_integrity_errors({}) == []


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
