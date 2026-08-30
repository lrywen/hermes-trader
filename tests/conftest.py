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
