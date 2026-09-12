"""Test isolation: redirect agent state files to a throwaway temp dir BEFORE any
hermes module imports, so a test can never read or truncate the live
.agent-memory.json / .agent-config.json (a pytest run wiped live trading state
on 2026-06-15). This runs at conftest import — before test modules are collected,
hence before memory.py / config_store.py freeze their module-level paths.
"""

import atexit
import os
import shutil
import tempfile

_tmp = tempfile.mkdtemp(prefix="hermes-test-state-")
# Clean up the throwaway dir at interpreter exit so each pytest session removes
# its own state instead of leaking a hermes-test-state-* dir in /tmp (253 dirs /
# 16MB accumulated before this). ignore_errors: cleanup must never mask test
# results, and stale file handles on some /tmp mounts can make removal fail.
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
# Force (not setdefault): even if the dev shell exports these, tests must use
# disposable paths.
os.environ["HERMES_AGENT_MEMORY_FILE"] = os.path.join(_tmp, ".agent-memory.json")
os.environ["HERMES_AGENT_CONFIG_FILE"] = os.path.join(_tmp, ".agent-config.json")
os.environ["HERMES_DSL_STATE_FILE"] = os.path.join(_tmp, ".dsl-state.json")
# Redirect the authoritative event feed + operational heartbeat so tests that
# exercise record_trade/record_close or session_log.append never touch the
# live volume's events.jsonl / session-log.jsonl.
os.environ["HERMES_EVENTS_FILE"] = os.path.join(_tmp, "events.jsonl")
os.environ["SESSION_LOG_PATH"] = os.path.join(_tmp, "session-log.jsonl")
# Redirect the ta_late_entry shadow JSONL: gate/prefilter tests that omit
# shadow_log_path would otherwise append to the developer's real
# ~/.hermes-trading/ta_late_entry_shadow.jsonl (late_entry_shadow_path falls
# back to this env var). Tests that need to assert on contents pass an
# explicit shadow_log_path or monkeypatch.setenv to their own tmp_path.
os.environ["HERMES_TA_LATE_ENTRY_SHADOW_FILE"] = os.path.join(
    _tmp, "ta_late_entry_shadow.jsonl")
# Redirect the market_circuit shadow JSONL (roadmap §3) the same way:
# market_circuit.shadow_log_path falls back to this env var, so shadow/enforce
# tests never append to the developer's real market_circuit_shadow.jsonl.
os.environ["HERMES_MARKET_CIRCUIT_SHADOW_FILE"] = os.path.join(
    _tmp, "market_circuit_shadow.jsonl")
# CS-F: redirect the market_circuit cross-process heartbeat state the same way
# (default /data is unwritable on dev hosts and shared in the container).
# Tests asserting on heartbeat contents monkeypatch market_circuit_state.STATE_FILE
# (or pass path=) to their own tmp_path; the env var is read once at import.
os.environ["HERMES_MARKET_CIRCUIT_STATE_FILE"] = os.path.join(
    _tmp, ".market-circuit.state")
# M17：同样把 pullback gate 评估心跳重定向到临时目录（默认 /data 在开发机
# 不可写）。需要断言心跳内容的测试自行传 path=/monkeypatch。
os.environ["HERMES_PULLBACK_GATE_STATE_FILE"] = os.path.join(
    _tmp, ".pullback-gate.state")
# Audit 2026-09-07 (test isolation): the remaining ten shadow arms were NOT
# redirected here, so a gate/shadow test that omitted an explicit path
# appended synthetic rows (e.g. coin "TESTCOIN") to the developer's REAL
# ~/.hermes-trading/<arm>_shadow.jsonl when pytest ran on the host (the
# container mounts that path read-only, so it only bit host runs; the leaked
# files even rotated to .1). Redirect every remaining arm's *_SHADOW_FILE env
# to the throwaway dir the same way. Tests that assert on file contents still
# pass an explicit shadow_log_path or monkeypatch.setenv to their own tmp_path.
for _arm_env, _arm_file in (
    ("HERMES_PULLBACK_SHADOW_FILE", "pullback_shadow.jsonl"),
    ("HERMES_ATR_REGIME_CALIB_SHADOW_FILE", "atr_regime_calib_shadow.jsonl"),
    ("HERMES_SIZING_V2_SHADOW_FILE", "sizing_v2_shadow.jsonl"),
    ("HERMES_CONFIDENCE_DECAY_SHADOW_FILE", "confidence_decay_shadow.jsonl"),
    ("HERMES_SIGNAL_AGE_DECAY_SHADOW_FILE", "signal_age_decay_shadow.jsonl"),
    ("HERMES_TREND_FILTER_SHADOW_FILE", "trend_filter_shadow.jsonl"),
    ("HERMES_DAILY_EXTENSION_CAP_SHADOW_FILE", "daily_extension_cap_shadow.jsonl"),
    ("HERMES_REENTRY_CAP_SHADOW_FILE", "reentry_cap_shadow.jsonl"),
    ("HERMES_XS_REVERSAL_SHADOW_FILE", "xs_reversal_shadow.jsonl"),
    ("HERMES_REGIME_OVERLAY_SHADOW_FILE", "regime_overlay_shadow.jsonl"),
):
    os.environ[_arm_env] = os.path.join(_tmp, _arm_file)
