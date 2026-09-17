"""P1-4 Phase 2.2 (plan a) — D-family accessor effective-value snapshot.

Read-only observability extension: every D-family dual-track accessor
(legacy HERMES_* env -> canonical cfg_get -> inline literal) projects its
REAL effective value plus a per-leaf source label into the startup
effective-config snapshot under ``accessor_effective``.

No env is removed and no resolution semantics change: the legacy env stays
the highest-priority operator emergency escape hatch; the snapshot only
makes the active value (and why it is active) visible.

Source vocabulary: ``env`` (legacy HERMES_* escape hatch), ``cfg_env``
(generic HERMES_CFG_*), ``file`` (mounted agent-config JSON), ``default``
(canonical/inline literal). Two families (hl_client_io / hl_rate_limit /
dsl_state_io) are resolved at IMPORT time with ``config={}``, so the
mounted config file is structurally invisible to their hot path — their
leaves can only be env/cfg_env/default, and the projected values are the
import-time module constants the running code actually consumes.
"""

from __future__ import annotations

import json

import pytest

from hermes_trader.agents import config_store, dsl_exit, executor, memory, research
from hermes_trader import dashboard
from hermes_trader.client import rate_limit


# ── fixtures ───────────────────────────────────────────────────────────────

_SECTION_LEAF_COUNTS = {
    "research_llm": 12,
    "research_fetch": 9,
    "hl_client_io": 15,
    "hl_rate_limit": 7,
    "http_cache": 5,
    "memory_quality": 7,
    "dashboard_equity": 4,
    "dsl_state_io": 6,
    "executor": 5,
}

# Every legacy env var touched by the nine families (sweep for deterministic
# source labeling; the HERMES_CFG_* counterparts are swept per test).
_ALL_LEGACY_ENVS = (
    "OPENROUTER_MODEL", "OPENROUTER_BASE_URL",
    "HERMES_RESEARCH_FALLBACK_TIMEOUT_S", "HERMES_RESEARCH_POOL_WORKERS",
    "HERMES_RESEARCH_SIGNALS_TIMEOUT_S", "HERMES_RESEARCH_FETCH_TIMEOUT_S",
    "HERMES_RESEARCH_FETCH_TIMEOUT_CANDLES", "HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING",
    "HERMES_RESEARCH_FETCH_TIMEOUT_NEWS", "HERMES_RESEARCH_FETCH_TIMEOUT_SIGNALS",
    "HERMES_HL_SDK_TIMEOUT_S", "HERMES_DEFAULT_LEVERAGE", "HERMES_MAX_SLIPPAGE_PCT",
    "HERMES_MAX_SLIPPAGE_CLOSE_PCT", "HERMES_META_TTL_S", "HERMES_ATR_TTL_S",
    "HERMES_CANDLE_CACHE_TTL_S", "HERMES_CANDLE_CACHE_MAX",
    "HERMES_FUNDING_CACHE_TTL_S", "HERMES_WS_MAX_STALE_SECONDS",
    "HERMES_WS_HEARTBEAT_S", "HERMES_WS_SEQ_MAX_BACKWARD",
    "HERMES_WS_MAX_TICK_JUMP_FRAC",
    "HERMES_HL_RATE_REFILL_PER_SEC", "HERMES_HL_RATE_CAPACITY",
    "HERMES_HL_RATE_MAX_WAIT_S", "HERMES_HL_429_RETRIES",
    "HERMES_HL_RATE_OPPORTUNISTIC_WAIT_S", "HERMES_HL_RATE_SHARED",
    "HERMES_HL_RATE_PER_ENDPOINT_GATE",
    "HERMES_SUMMARY_TTL_S", "HERMES_EQUITY_CURVE_TTL_S",
    "HERMES_CLOSED_TRADES_TTL_S", "HERMES_RESEARCH_HTTP_CACHE_S",
    "HERMES_EQUITY_CRASH_DOWN_PCT", "HERMES_MEMORY_FLUSH_THROTTLE_S",
    "HERMES_EQUITY_DIP_RATIO", "HERMES_EQUITY_DIP_WINDOW",
    "HERMES_CLOSED_TRADES_DEDUP_MS",
    "HERMES_DSL_SAVE_INTERVAL_SEC", "HERMES_DSL_FORCE_LOAD_TTL_S",
    "HERMES_DSL_POLICY_CACHE_TTL_S", "HERMES_DSL_SAVE_MAX_ATTEMPTS",
    "HERMES_DSL_SAVE_BACKOFF_BASE_SEC",
    "HERMES_MAX_ATR_PCT", "HERMES_MAX_SPREAD_PCT",
    "HERMES_SPREAD_GATE_FAIL_OPEN",
    "HERMES_LIQ_BUFFER_USD", "HERMES_TAKER_FEE_PCT",
)

_ALL_CFG_ENVS = (
    "HERMES_CFG_HTTP_CACHE__SUMMARY_TTL_S",
    "HERMES_CFG_RESEARCH_FETCH__MAX_CONNECTIONS",
)


@pytest.fixture
def no_config(monkeypatch, tmp_path):
    """Point the config layer at a missing file so no file value exists."""
    missing = tmp_path / "no-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(missing))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(missing) + ".lock")
    config_store._invalidate_raw_cache()
    for name in _ALL_LEGACY_ENVS + _ALL_CFG_ENVS:
        monkeypatch.delenv(name, raising=False)
    return missing


def _view(monkeypatch):
    return config_store.build_effective_config_snapshot()["accessor_effective"]


def _assert_leaf_shape(section: dict) -> None:
    for leaf, entry in section.items():
        assert set(entry) == {"value", "source"}, leaf
        assert entry["source"] in ("env", "cfg_env", "file", "default"), leaf


# ── section presence / shape ───────────────────────────────────────────────

def test_all_d_family_sections_present_with_full_leaf_coverage(no_config):
    view = config_store.build_effective_config_snapshot()["accessor_effective"]
    for section, count in _SECTION_LEAF_COUNTS.items():
        assert section in view, section
        assert "error" not in view[section], view[section]
        assert len(view[section]) == count, (section, len(view[section]))
        _assert_leaf_shape(view[section])
    # Pre-Phase-2.2 sections keep their old flat shape untouched.
    assert "scan_dynamic" in view["loop_runtime"]
    assert "atr_regime_calibration.mode" in view["gray_modes"]


# ── projected values equal what the running code actually consumes ─────────

def test_per_call_families_project_real_accessor_values(no_config):
    view = config_store.build_effective_config_snapshot()["accessor_effective"]
    assert {k: v["value"] for k, v in view["research_llm"].items()} \
        == research.research_llm_params()
    assert {k: v["value"] for k, v in view["research_fetch"].items()} \
        == research.research_fetch_params()
    assert {k: v["value"] for k, v in view["http_cache"].items()} \
        == dashboard._http_cache_params()
    assert {k: v["value"] for k, v in view["memory_quality"].items()} \
        == memory._memory_quality_params()
    assert {k: v["value"] for k, v in view["dashboard_equity"].items()} \
        == dashboard._dashboard_equity_params()


def test_import_time_families_project_module_constants(no_config):
    # config={} at import: the snapshot must report the frozen module
    # constants — re-running the helper with a live config would LIE about
    # the hot-path value whenever the mounted file sets these blocks.
    view = config_store.build_effective_config_snapshot()["accessor_effective"]
    assert {k: v["value"] for k, v in view["hl_client_io"].items()} \
        == dict(rate_limit._HL_CLIENT_IO)
    assert {k: v["value"] for k, v in view["hl_rate_limit"].items()} \
        == dict(rate_limit._HL_RATE_LIMIT)
    assert {k: v["value"] for k, v in view["dsl_state_io"].items()} == {
        "save_min_interval_sec": dsl_exit._MIN_SAVE_INTERVAL_SEC,
        "force_load_ttl_s": dsl_exit._FORCE_LOAD_TTL_S,
        "policy_cache_ttl_s": dsl_exit._POLICY_CACHE_TTL_S,
        "save_max_attempts": dsl_exit._SAVE_MAX_ATTEMPTS,
        "save_backoff_base_sec": dsl_exit._SAVE_BACKOFF_BASE_SEC,
        "save_backoff_factor": dsl_exit._SAVE_BACKOFF_FACTOR,
    }


def test_executor_section_projects_real_resolvers(no_config):
    view = config_store.build_effective_config_snapshot()["accessor_effective"]
    ex = view["executor"]
    assert ex["max_atr_pct"]["value"] == executor._resolve_max_atr_pct()
    assert ex["max_spread_pct"]["value"] == executor._resolve_max_spread_pct()
    assert ex["spread_gate_fail_open"]["value"] \
        == executor._resolve_spread_gate_fail_open()
    assert ex["liq_buffer_usd"]["value"] == executor._resolve_liq_buffer_usd()
    assert ex["execution.taker_fee_pct"]["value"] \
        == executor._resolve_hl_taker_fee_pct()


# ── legacy env escape hatch: active value + source=env visible ─────────────

_ENV_CASES = (
    "section,leaf,env_name,raw,expected",
    [
        ("research_llm", "model", "OPENROUTER_MODEL", "snapshot-model-x",
         "snapshot-model-x"),
        ("research_fetch", "pool_workers", "HERMES_RESEARCH_POOL_WORKERS",
         "7", 7),
        # The ONE hl leaf whose production consumer keeps a call-time env
        # read (the other hl/dsl leaves are frozen at import time).
        ("hl_rate_limit", "rate_per_endpoint_gate",
         "HERMES_HL_RATE_PER_ENDPOINT_GATE", "0", False),
        ("http_cache", "summary_ttl_s", "HERMES_SUMMARY_TTL_S", "12.5", 12.5),
        ("memory_quality", "flush_throttle_s",
         "HERMES_MEMORY_FLUSH_THROTTLE_S", "1.25", 1.25),
        ("dashboard_equity", "dip_ratio", "HERMES_EQUITY_DIP_RATIO",
         "0.55", 0.55),
        ("executor", "max_atr_pct", "HERMES_MAX_ATR_PCT", "22.5", 22.5),
        ("executor", "spread_gate_fail_open",
         "HERMES_SPREAD_GATE_FAIL_OPEN", "1", True),
        ("executor", "execution.taker_fee_pct",
         "HERMES_TAKER_FEE_PCT", "0.01", 0.01),
    ],
)


@pytest.mark.parametrize(*_ENV_CASES)
def test_legacy_env_escape_hatch_labeled_env(
        no_config, section, leaf, env_name, raw, expected):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(env_name, raw)
        view = config_store.build_effective_config_snapshot()["accessor_effective"]
    entry = view[section][leaf]
    assert entry["value"] == expected
    assert entry["source"] == "env"


def test_liq_buffer_zero_round_trips_as_env(no_config):
    # 0.0 ("gate disabled") must survive, not collapse to the 10.0 fallback.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HERMES_LIQ_BUFFER_USD", "0")
        view = config_store.build_effective_config_snapshot()["accessor_effective"]
    entry = view["executor"]["liq_buffer_usd"]
    assert entry["value"] == 0.0
    assert entry["source"] == "env"


def test_invalid_numeric_legacy_env_falls_through(no_config):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HERMES_LIQ_BUFFER_USD", "banana")
        view = config_store.build_effective_config_snapshot()["accessor_effective"]
    entry = view["executor"]["liq_buffer_usd"]
    assert entry["value"] == 10.0
    assert entry["source"] == "default"


# ── cfg_env / file / default source labels ─────────────────────────────────

@pytest.mark.parametrize(
    "section,leaf,cfg_env,raw,expected",
    [
        ("http_cache", "summary_ttl_s",
         "HERMES_CFG_HTTP_CACHE__SUMMARY_TTL_S", "9.25", 9.25),
        ("research_fetch", "max_connections",
         "HERMES_CFG_RESEARCH_FETCH__MAX_CONNECTIONS", "5", 5),
    ],
)
def test_cfg_env_override_labeled_cfg_env(
        no_config, section, leaf, cfg_env, raw, expected):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(cfg_env, raw)
        view = config_store.build_effective_config_snapshot()["accessor_effective"]
    entry = view[section][leaf]
    assert entry["value"] == expected
    assert entry["source"] == "cfg_env"


def test_mounted_file_value_labeled_file(monkeypatch, tmp_path):
    cfg_file = tmp_path / "agent-config.json"
    cfg_file.write_text(json.dumps({
        "memory_quality": {"reconfirm_streak": 4},
        "max_spread_pct": 2.5,
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH",
                        str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    for name in _ALL_LEGACY_ENVS + _ALL_CFG_ENVS:
        monkeypatch.delenv(name, raising=False)
    view = config_store.build_effective_config_snapshot()["accessor_effective"]
    assert view["memory_quality"]["reconfirm_streak"] == {
        "value": 4, "source": "file"}
    assert view["executor"]["max_spread_pct"] == {
        "value": 2.5, "source": "file"}


def test_import_time_families_never_label_file_and_default_to_canonical(
        no_config):
    view = config_store.build_effective_config_snapshot()["accessor_effective"]
    for section in ("hl_client_io", "hl_rate_limit", "dsl_state_io"):
        sources = {leaf: e["source"] for leaf, e in view[section].items()}
        assert set(sources.values()) == {"default"}, (section, sources)
    assert view["hl_client_io"]["sdk_timeout_s"]["value"] == 30.0
    assert view["hl_rate_limit"]["rate_capacity"]["value"] == 600
    assert view["dsl_state_io"]["save_min_interval_sec"]["value"] == 2.0
    assert view["dsl_state_io"]["save_backoff_factor"]["value"] == 3


def test_call_time_families_default_labels_when_nothing_overridden(no_config):
    view = config_store.build_effective_config_snapshot()["accessor_effective"]
    assert view["research_llm"]["model"] == {
        "value": "deepseek-v4-flash", "source": "default"}
    assert view["http_cache"]["summary_ttl_s"] == {
        "value": 2.0, "source": "default"}
    assert view["memory_quality"]["flush_throttle_s"] == {
        "value": 0.2, "source": "default"}
    assert view["dashboard_equity"]["dip_ratio"] == {
        "value": 0.7, "source": "default"}
    assert view["executor"]["max_atr_pct"] == {
        "value": 15.0, "source": "default"}
    assert view["executor"]["liq_buffer_usd"] == {
        "value": 10.0, "source": "default"}
    assert view["executor"]["execution.taker_fee_pct"] == {
        "value": 0.025, "source": "default"}
    assert view["executor"]["spread_gate_fail_open"] == {
        "value": False, "source": "default"}


# ── failure isolation: one broken section never breaks the snapshot ────────

def test_section_import_failure_isolates_to_error_leaf(no_config, monkeypatch):
    # A None entry in sys.modules makes `from hermes_trader.agents import
    # memory` raise ImportError; only that section must degrade.
    monkeypatch.setitem(__import__("sys").modules,
                        "hermes_trader.agents.memory", None)
    snap = config_store.build_effective_config_snapshot()
    view = snap["accessor_effective"]
    assert "error" in view["memory_quality"]
    assert "error" not in view["research_llm"]
    assert "scan_dynamic" in view["loop_runtime"]
