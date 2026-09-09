"""CS-F (audit 2026-09-08): market_circuit cross-process heartbeat.

The market_circuit verdict Counter is incremented in the trading-loop process
while Prometheus scrapes the web process, so the loop rewrites a whole-file
heartbeat state the web side reads. These tests cover:

  A. state helper — write/read contract, RMW cumulative counts, corruption
     reset, future-version/non-dict/missing → None, never-raises, atomic file;
  B. metrics — _refresh exports the heartbeat gauges; a missing file exports
     explicit sentinels (ts=0, age=0, state=4 for mode=unknown) so the
     ``== 0`` HeartbeatAbsent rule fires, and a scrape never errors;
  C. evaluate integration — every verdict path rewrites the heartbeat;
  D. prometheusrule — the three new alerts + fixed DataMissing expr pass the
     contract gate (every hermes_ token registered, _total counters etc.);
  E. dashboard — risk-status payload gains a market_circuit block with
     available False on a missing file and never 500s.
"""

import json
import time
from pathlib import Path

import pytest

from hermes_trader import dashboard, metrics
from hermes_trader.agents import market_circuit as mc
from hermes_trader.agents import market_circuit_state as mcs


# ══════════════════════════════════════════════════════════════════════════
# A. state helper
# ══════════════════════════════════════════════════════════════════════════
def _verdict(action="clear", **over):
    v = {"tripped": False, "trigger": None, "reasons": [], "details": {},
         "data_ok": True, "mode": "shadow", "action": action}
    v.update(over)
    return v


def test_state_file_env_pin_present():
    """Both processes resolve the same shared path via the env pin
    (compose environment + Dockerfile ENV for k8s)."""
    import os
    assert os.environ["HERMES_MARKET_CIRCUIT_STATE_FILE"]


def test_record_then_read_roundtrip(tmp_path):
    path = str(tmp_path / "hb.state")
    mcs.record_evaluation(_verdict("clear"), mode="shadow",
                          verdict_label="no_trip", path=path)
    st = mcs.read_state(path)
    assert st is not None
    assert st["version"] == 1
    assert st["mode"] == "shadow"
    assert st["action"] == "clear"
    assert st["data_ok"] is True
    assert st["tripped"] is False
    assert st["state"] == mcs.STATE_CLEAR
    assert st["counts"]["shadow"]["no_trip"] == 1
    assert time.time() - st["ts"] < 5.0


def test_counts_accumulate_rmw_single_writer(tmp_path):
    path = str(tmp_path / "hb.state")
    mcs.record_evaluation(_verdict("clear"), mode="shadow",
                          verdict_label="no_trip", path=path)
    mcs.record_evaluation(_verdict("clear"), mode="shadow",
                          verdict_label="no_trip", path=path)
    mcs.record_evaluation(_verdict("data_missing", data_ok=False),
                          mode="shadow", verdict_label="data_missing", path=path)
    mcs.record_evaluation(_verdict("would_trip", tripped=True),
                          mode="shadow", verdict_label="trip", path=path)
    st = mcs.read_state(path)
    assert st["counts"]["shadow"] == {"no_trip": 2, "data_missing": 1, "trip": 1}


def test_state_mapping_per_action(tmp_path):
    path = str(tmp_path / "hb.state")
    cases = [
        ("clear", False, mcs.STATE_CLEAR),
        ("off", False, mcs.STATE_OFF),
        ("data_missing", False, mcs.STATE_DATA_MISSING),
        ("error", False, mcs.STATE_ERROR),
        ("would_trip", True, mcs.STATE_TRIPPED),
        ("halt_already_armed", True, mcs.STATE_TRIPPED),
        ("halt_armed", True, mcs.STATE_TRIPPED),
    ]
    for action, tripped, expected in cases:
        mcs.record_evaluation(_verdict(action, tripped=tripped), mode="shadow",
                              verdict_label="no_trip", path=path)
        assert mcs.read_state(path)["state"] == expected, action


def test_read_missing_returns_none(tmp_path):
    assert mcs.read_state(str(tmp_path / "nope.state")) is None


def test_read_corrupt_json_returns_none(tmp_path):
    path = tmp_path / "hb.state"
    path.write_text("{not json", encoding="utf-8")
    assert mcs.read_state(str(path)) is None


def test_read_non_dict_returns_none(tmp_path):
    path = tmp_path / "hb.state"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert mcs.read_state(str(path)) is None


def test_read_future_version_returns_none(tmp_path):
    path = tmp_path / "hb.state"
    path.write_text(json.dumps({"version": 999, "ts": time.time()}),
                    encoding="utf-8")
    assert mcs.read_state(str(path)) is None


def test_record_resets_counts_on_corrupt_prior_file(tmp_path):
    path = tmp_path / "hb.state"
    path.write_text("garbage", encoding="utf-8")
    mcs.record_evaluation(_verdict("clear"), mode="shadow",
                          verdict_label="no_trip", path=str(path))
    st = mcs.read_state(str(path))
    assert st["counts"] == {"shadow": {"no_trip": 1}}


def test_record_never_raises_on_unwritable_path():
    # /dev/null/... is a directory-impossible path; record must swallow it.
    mcs.record_evaluation(_verdict("clear"), mode="shadow",
                          verdict_label="no_trip",
                          path="/dev/null/cannot/write/here.state")


def test_write_is_non_fsync_compact_json(tmp_path):
    path = tmp_path / "hb.state"
    mcs.record_evaluation(_verdict("clear"), mode="shadow",
                          verdict_label="no_trip", path=str(path))
    raw = path.read_text(encoding="utf-8")
    assert "\n " not in raw and "\n" not in raw.strip()[1:]  # compact single line


# ══════════════════════════════════════════════════════════════════════════
# B. metrics export from the heartbeat file
# ══════════════════════════════════════════════════════════════════════════
def test_render_metrics_contains_new_gauges():
    body = metrics.render_metrics()[0].decode()
    for name in (
        "hermes_market_circuit_last_eval_timestamp_seconds",
        "hermes_market_circuit_eval_age_seconds",
        "hermes_market_circuit_state",
        "hermes_market_circuit_verdicts_cumulative",
    ):
        assert name in body


def test_refresh_reads_heartbeat_values(monkeypatch, tmp_path):
    path = str(tmp_path / "hb.state")
    ts = time.time() - 10.0
    Path(path).write_text(json.dumps({
        "version": 1, "ts": ts, "mode": "shadow", "action": "data_missing",
        "data_ok": False, "tripped": False, "state": mcs.STATE_DATA_MISSING,
        "counts": {"shadow": {"no_trip": 5, "data_missing": 2, "trip": 1}},
    }), encoding="utf-8")
    monkeypatch.setattr(mcs, "STATE_FILE", path)
    metrics.render_metrics()
    assert metrics.MARKET_CIRCUIT_LAST_EVAL_TS._value.get() == pytest.approx(ts)
    age = metrics.MARKET_CIRCUIT_EVAL_AGE._value.get()
    assert 9.5 <= age <= 30.0
    assert metrics.MARKET_CIRCUIT_STATE.labels(mode="shadow")._value.get() == 2.0
    assert metrics.MARKET_CIRCUIT_VERDICTS_CUMULATIVE.labels(
        mode="shadow", verdict="no_trip")._value.get() == 5.0
    assert metrics.MARKET_CIRCUIT_VERDICTS_CUMULATIVE.labels(
        mode="shadow", verdict="trip")._value.get() == 1.0


def test_refresh_missing_file_exports_explicit_sentinels(monkeypatch, tmp_path):
    # First scrape before the loop ever wrote a heartbeat: a plain Gauge
    # always emits a 0.0 sample anyway, so the exporter must make the
    # absence EXPLICIT (ts=0, age=0, state=4 for mode=unknown) for the
    # ``== 0`` HeartbeatAbsent rule.
    missing = str(tmp_path / "absent.state")
    monkeypatch.setattr(mcs, "STATE_FILE", missing)
    body = metrics.render_metrics()[0].decode()  # must not raise
    assert metrics.MARKET_CIRCUIT_LAST_EVAL_TS._value.get() == 0.0
    assert metrics.MARKET_CIRCUIT_EVAL_AGE._value.get() == 0.0
    assert metrics.MARKET_CIRCUIT_STATE.labels(mode="unknown")._value.get() == 4.0
    # Labelled cumulative samples were cleared, not carried from an older mode.
    assert metrics.MARKET_CIRCUIT_VERDICTS_CUMULATIVE._metrics == {}
    assert "hermes_market_circuit_verdicts_cumulative{" not in body


# ══════════════════════════════════════════════════════════════════════════
# C. evaluate integration — every verdict path writes a heartbeat
# ══════════════════════════════════════════════════════════════════════════
def _candle(h, c):
    from types import SimpleNamespace
    return SimpleNamespace(h=float(h), c=float(c))


def _crash_fetcher(*_a, **_k):
    return [_candle(100, 100), _candle(100, 99), _candle(98, 97)]


def _calm_fetcher(*_a, **_k):
    return [_candle(100, 100), _candle(100.1, 100), _candle(100.1, 99.9)]


def _failing_fetcher(*_a, **_k):
    raise RuntimeError("candle API down")


def _passthrough(raw, interval):
    return raw, False


class _FakeMem:
    def __init__(self, remaining_min=0.0):
        self.remaining_min = remaining_min
        self.arm_calls = 0

    def global_halt_remaining_min(self):
        return self.remaining_min

    def set_global_halt(self, until_ms):
        self.arm_calls += 1


def _cfg(tmp_path, **over):
    cfg = {"mode": "shadow", "halt_minutes": 30.0, "cooldown_minutes": 60.0,
           "shadow_log_path": str(tmp_path / "mc_shadow.jsonl")}
    cfg.update(over)
    return cfg


@pytest.mark.parametrize("cfg_mode,expected_label,expected_state", [
    ("off", "off", mcs.STATE_OFF),
])
def test_evaluate_off_writes_heartbeat(tmp_path, monkeypatch, cfg_mode,
                                       expected_label, expected_state):
    path = str(tmp_path / "hb.state")
    monkeypatch.setattr(mcs, "STATE_FILE", path)
    v = mc.evaluate({"mode": cfg_mode}, mem=_FakeMem(),
                    candle_fetcher=_calm_fetcher, closed_filter=_passthrough,
                    stop_events=[])
    assert v["action"] == "off"
    st = mcs.read_state(path)
    assert st["state"] == expected_state
    assert st["counts"]["off"][expected_label] == 1


def test_evaluate_clear_and_data_missing_and_trip_heartbeats(tmp_path, monkeypatch):
    path = str(tmp_path / "hb.state")
    monkeypatch.setattr(mcs, "STATE_FILE", path)

    mc.evaluate(_cfg(tmp_path), mem=_FakeMem(),
                candle_fetcher=_calm_fetcher, closed_filter=_passthrough,
                stop_events=[])
    st = mcs.read_state(path)
    assert st["state"] == mcs.STATE_CLEAR
    assert st["counts"]["shadow"]["no_trip"] == 1

    # data_ok needs BOTH index and stop-cluster data unavailable. Passing
    # stop_events=[] (even via the module window fallback) counts as valid
    # "no stops" data, so stub the fallback feed to return None.
    monkeypatch.setattr(mc._stop_window, "events", lambda: None)
    mc.evaluate(_cfg(tmp_path), mem=_FakeMem(),
                candle_fetcher=_failing_fetcher, closed_filter=_passthrough,
                stop_events=None)
    st = mcs.read_state(path)
    assert st["state"] == mcs.STATE_DATA_MISSING
    assert st["counts"]["shadow"]["data_missing"] == 1

    mc.evaluate(_cfg(tmp_path), mem=_FakeMem(),
                candle_fetcher=_crash_fetcher, closed_filter=_passthrough,
                stop_events=[])
    st = mcs.read_state(path)
    assert st["state"] == mcs.STATE_TRIPPED
    assert st["counts"]["shadow"]["trip"] == 1


def test_evaluate_enforce_trip_heartbeat(tmp_path, monkeypatch):
    path = str(tmp_path / "hb.state")
    monkeypatch.setattr(mcs, "STATE_FILE", path)
    v = mc.evaluate(_cfg(tmp_path, mode="enforce"), mem=_FakeMem(),
                    candle_fetcher=_crash_fetcher, closed_filter=_passthrough,
                    notifier=lambda **k: None, event_log=lambda e: None,
                    stop_events=[])
    assert v["action"] == "halt_armed"
    st = mcs.read_state(path)
    assert st["state"] == mcs.STATE_TRIPPED
    assert st["counts"]["enforce"]["trip"] == 1


def test_evaluate_non_dict_cfg_heartbeat_and_safe(tmp_path, monkeypatch):
    path = str(tmp_path / "hb.state")
    monkeypatch.setattr(mcs, "STATE_FILE", path)
    v = mc.evaluate("not-a-config", mem=_FakeMem())
    assert v["action"] == "error" and v["data_ok"] is False
    st = mcs.read_state(path)
    assert st["state"] == mcs.STATE_ERROR
    assert st["counts"]["off"]["data_missing"] == 1


def test_evaluate_heartbeat_failure_never_breaks_evaluate(tmp_path, monkeypatch):
    """If the heartbeat write raises, evaluate's verdict/side effects must be
    unchanged (observability must not perturb the trading hot path)."""
    def _boom(*_a, **_k):
        raise RuntimeError("state disk down")

    monkeypatch.setattr(mcs, "record_evaluation", _boom)
    v = mc.evaluate(_cfg(tmp_path, mode="enforce"), mem=_FakeMem(),
                    candle_fetcher=_crash_fetcher, closed_filter=_passthrough,
                    notifier=lambda **k: None, event_log=lambda e: None,
                    stop_events=[])
    assert v["action"] == "halt_armed"


# ══════════════════════════════════════════════════════════════════════════
# D. prometheusrule contract for the new/changed alerts
# ══════════════════════════════════════════════════════════════════════════
def test_prom_rule_new_market_circuit_alerts():
    """End-to-end contract gate (registration + naming + for/severity)."""
    import yaml
    rule_path = Path(__file__).resolve().parents[1] / "k8s" / "prometheusrule.yaml"
    doc = yaml.safe_load(rule_path.read_text(encoding="utf-8"))
    rules = {r["alert"]: r for group in doc["spec"]["groups"]
             for r in group["rules"] if "alert" in r}
    assert "HermesMarketCircuitEvalStale" in rules
    assert "HermesMarketCircuitEvalDead" in rules
    assert "HermesMarketCircuitHeartbeatAbsent" in rules
    dm = rules["HermesMarketCircuitDataMissing"]
    # The blind Counter increase() must be gone; reads cross-process state.
    assert "hermes_market_circuit_verdicts_total" not in dm["expr"]
    assert "hermes_market_circuit_state" in dm["expr"]
    assert rules["HermesMarketCircuitEvalDead"]["labels"]["severity"] == "critical"


# ══════════════════════════════════════════════════════════════════════════
# E. dashboard risk-status payload
# ══════════════════════════════════════════════════════════════════════════
def _risk_blind_without_heartbeat(monkeypatch, tmp_path):
    """Baseline risk_blind driven solely by the other gates (memory read /
    blind-gate events), which is independent of the heartbeat file."""
    monkeypatch.setattr(mcs, "STATE_FILE", str(tmp_path / "absent.state"))
    return dashboard._risk_status_payload()["risk_blind"]


def test_dashboard_payload_has_market_circuit_block(monkeypatch, tmp_path):
    baseline_blind = _risk_blind_without_heartbeat(monkeypatch, tmp_path)
    path = str(tmp_path / "hb.state")
    monkeypatch.setattr(mcs, "STATE_FILE", path)
    mcs.record_evaluation(_verdict("data_missing", data_ok=False),
                          mode="shadow", verdict_label="data_missing", path=path)
    out = dashboard._risk_status_payload()
    block = out["market_circuit"]
    assert block["available"] is True
    assert block["mode"] == "shadow"
    assert block["state"] == mcs.STATE_DATA_MISSING
    assert block["state_label"] == "data_missing"
    assert block["age_s"] is not None
    assert block["counts"]["shadow"]["data_missing"] == 1
    # A present heartbeat never flips the trading-gate blindness flag.
    assert out.get("risk_blind") == baseline_blind


def test_dashboard_payload_missing_heartbeat_degrades_quietly(monkeypatch, tmp_path):
    baseline_blind = _risk_blind_without_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setattr(mcs, "STATE_FILE", str(tmp_path / "absent.state"))
    out = dashboard._risk_status_payload()
    assert out["market_circuit"] == {"available": False}
    # Observability gap never flips the trading-gate blindness flag.
    assert out.get("risk_blind") == baseline_blind
