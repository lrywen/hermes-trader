"""Batch S4 guards — Q7 drift tiers, Q9 swallowed-error metric, Q14 config
provenance CLI, Q15 configurable funding-fetch bounds.

Behavior defaults are unchanged; these pin the new safety/observability hooks.
"""

from __future__ import annotations

import json

import pytest

from hermes_trader.agents import executor, config_store, research
from hermes_trader import metrics


# ── Q9: swallowed-error counter exists and the three business call sites use it

def test_swallowed_metric_defined():
    assert hasattr(metrics, "SWALLOWED_ERRORS")


@pytest.mark.parametrize("label", [
    "sl_coin_floor_override",
    "regime_detection",
    "config_env_drift_audit",
])
def test_executor_silent_branches_are_instrumented(label):
    import inspect
    src = inspect.getsource(executor)
    assert f'labels(func="{label}")' in src


# ── Q7: drift tiers + critical global halt

def test_drift_tier_constants():
    assert executor._STOP_DRIFT_WARN_PCT == 5.0
    assert executor._STOP_DRIFT_CRITICAL_PCT == 25.0
    assert executor._STOP_DRIFT_CRITICAL_HALT_MIN > 0


def test_drift_register_block_halts_on_critical(monkeypatch):
    """The post-fill drift block must call memory.set_global_halt when dev
    exceeds the critical tier (source-level contract; the block is deep in the
    fill-registration path)."""
    import inspect
    src = inspect.getsource(executor._register_filled_position)
    assert "_STOP_DRIFT_CRITICAL_PCT" in src
    assert "set_global_halt" in src
    assert "STOP DRIFT CRITICAL" in src
    # The warn-only constant must no longer be a hard-coded 5.0 gate.
    assert "_dev_pct > 5.0" not in src


# ── Q14: provenance resolution + CLI

def test_provenance_default(monkeypatch):
    monkeypatch.delenv("HERMES_CFG_DSL_EXIT__PROTECT_PCT", raising=False)
    # Force the file layer to miss by pointing at a nonexistent config file via
    # _read_raw_config returning None.
    monkeypatch.setattr(config_store, "_read_raw_config", lambda: None)
    value, source = config_store._resolve_provenance("dsl_exit.protect_pct")
    assert source == "default"
    assert value == config_store._lookup_default("dsl_exit.protect_pct")


def test_provenance_cfg_env_wins(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_DSL_EXIT__PROTECT_PCT", "0.42")
    value, source = config_store._resolve_provenance("dsl_exit.protect_pct")
    assert source == "cfg_env"
    assert value == 0.42


def test_explain_cli_outputs_source(tmp_path, monkeypatch):
    # Point CONFIG_PATH at an isolated file containing one override.
    cfg = tmp_path / "agent-config.json"
    cfg.write_text(json.dumps({"dsl_exit": {"protect_pct": 1.25}}))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", f"{cfg}.lock")
    rc = config_store._config_cli(["x", "explain", "dsl_exit.protect_pct"])
    assert rc == 0


def test_dump_effective_cli_is_json(monkeypatch, capsys):
    monkeypatch.setattr(config_store, "_read_raw_config", lambda: None)
    rc = config_store._config_cli(["x", "--dump-effective"])
    assert rc == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert len(doc) > 100
    assert all(v["source"] == "default" for v in doc.values())


def test_cli_bad_usage_returns_2():
    assert config_store._config_cli(["x"]) == 2


# ── Q15: funding fetch bounds are config-driven with unchanged defaults

def test_funding_default_constants_unchanged():
    assert research._FUNDING_ATTEMPT_TIMEOUT_S == 3.5
    assert research._FUNDING_TOTAL_CAP_S == 6.0
    assert research._FUNDING_MAX_ATTEMPTS == 2


def test_funding_fetch_reads_config_bounds(monkeypatch):
    """When configured, the fetch loop uses the configured attempt count; with a
    permanently-pending future it attempts exactly max_attempts and then returns
    a degraded marker (never raises, never stalls past the total cap)."""
    calls = {"n": 0}

    class _Never:
        def result(self, timeout=None):
            raise research.FuturesTimeoutError()

        def cancel(self):
            return True

    def _fake_submit(fn, coin, start_time):
        calls["n"] += 1
        return _Never()

    # Tiny real timeouts so the loop runs all 3 attempts well under the cap.
    monkeypatch.setattr(research._funding_pool, "submit", _fake_submit)
    monkeypatch.setattr(research, "cfg_get", lambda k, d=None: {
        "funding_lookback_hours": 24,
        "funding_fetch.attempt_timeout_s": 0.01,
        "funding_fetch.total_cap_s": 5.0,
        "funding_fetch.max_attempts": 3,
    }.get(k, d))
    marker = research._fetch_funding_rate("ETH")
    assert calls["n"] == 3
    assert isinstance(marker, str)
