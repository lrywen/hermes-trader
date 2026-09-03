"""P0-2d: pydantic data contracts at the cross-process read boundaries.

Covers hermes_trader.contracts (snapshot + heartbeat parse helpers) and the
wiring in positions_snapshot.read_snapshot / dashboard summary + risk-status
payloads. The invariant under test: malformed data never raises into a read
endpoint — it degrades to None / the same fallback path a missing payload
takes.
"""
from __future__ import annotations

import json

from hermes_trader import contracts, positions_snapshot

# ── parse_snapshot ───────────────────────────────────────────────────────────

def test_parse_snapshot_valid_payload_roundtrip():
    rows = [
        {"type": "oneWay", "position": {"coin": "BTC", "szi": "1.0",
                                        "entryPx": "50000", "unrealizedPnl": "12.5"}},
        {"type": "oneWay", "position": {"coin": "ETH", "szi": "-2.0",
                                        "entryPx": "2500"}},
    ]
    parsed = contracts.parse_snapshot(
        {"version": 1, "saved_at": 1_700_000_000_000, "asset_positions": rows}
    )
    assert parsed is not None
    assert parsed["version"] == 1
    assert parsed["saved_at"] == 1_700_000_000_000
    # Extra exchange fields on the position sub-dict are preserved verbatim.
    btc = parsed["asset_positions"][0]["position"]
    assert btc["coin"] == "BTC"
    assert btc["unrealizedPnl"] == "12.5"


def test_parse_snapshot_legacy_unversioned_payload_defaults():
    parsed = contracts.parse_snapshot(
        {"saved_at": 1, "asset_positions": []}
    )
    assert parsed is not None
    assert parsed["version"] == 0
    assert parsed["asset_positions"] == []


def test_parse_snapshot_non_object_returns_none():
    assert contracts.parse_snapshot(["not", "an", "object"]) is None
    assert contracts.parse_snapshot("junk") is None
    assert contracts.parse_snapshot(None) is None


def test_parse_snapshot_asset_positions_not_a_list_returns_none():
    bad = {"version": 1, "saved_at": 1, "asset_positions": "not-a-list"}
    assert contracts.parse_snapshot(bad) is None


def test_parse_snapshot_malformed_rows_are_skipped_not_fatal():
    rows = [
        {"position": {"coin": "BTC", "szi": "1.0", "entryPx": "50000"}},
        "not-a-dict",
        {"position": "not-a-dict"},
        {"no-position-key": True},
    ]
    parsed = contracts.parse_snapshot(
        {"version": 1, "saved_at": 1, "asset_positions": rows}
    )
    assert parsed is not None
    # Only the well-formed BTC row survives; the three bad rows are skipped.
    assert len(parsed["asset_positions"]) == 1
    assert parsed["asset_positions"][0]["position"]["coin"] == "BTC"


def test_read_snapshot_rejects_mistyped_snapshot_file(tmp_path, monkeypatch):
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps(
        {"version": 1, "saved_at": 10**15, "asset_positions": "junk"}
    ))
    monkeypatch.setattr(positions_snapshot, "SNAPSHOT_FILE", str(snap))
    assert positions_snapshot.read_snapshot() is None


def test_read_snapshot_skips_bad_rows_but_returns_good_ones(tmp_path, monkeypatch):
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({
        "version": 1,
        "saved_at": 10**15,
        "asset_positions": [
            {"position": {"coin": "BTC", "szi": "1.0", "entryPx": "50000"}},
            "not-a-dict",
        ],
    }))
    monkeypatch.setattr(positions_snapshot, "SNAPSHOT_FILE", str(snap))
    out = positions_snapshot.read_snapshot()
    assert out is not None
    assert len(out["asset_positions"]) == 1
    assert out["asset_positions"][0]["position"]["coin"] == "BTC"


# ── parse_heartbeat ──────────────────────────────────────────────────────────

def _good_heartbeat() -> dict:
    return {
        "event": "loop_heartbeat",
        "ts": 1_700_000_000_000,
        "equity": 100000.0,
        "available": 80000.0,
        "dex_equity": {"main": 50000.0},
        "dex_available": {"main": 40000.0},
        "spot_usdc": 100.0,
        "daily_pnl": -250.0,
        "cum_contrib": 5000.0,
        "open_positions": 3,
        "config": {"mode": "LIVE", "lev": 5, "kill": -1000},
    }


def test_parse_heartbeat_valid_payload_passthrough():
    hb = contracts.parse_heartbeat(_good_heartbeat())
    assert hb is not None
    assert hb["equity"] == 100000.0
    assert hb["cum_contrib"] == 5000.0
    assert hb["open_positions"] == 3
    assert hb["config"]["mode"] == "LIVE"
    assert hb["dex_equity"] == {"main": 50000.0}


def test_parse_heartbeat_legacy_event_gets_defaults():
    # Pre-upgrade heartbeat: no cum_contrib, no spot_usdc, no config.
    legacy = {"event": "loop_heartbeat", "ts": 1, "equity": 10.0,
              "daily_pnl": 0.0, "open_positions": 0}
    hb = contracts.parse_heartbeat(legacy)
    assert hb is not None
    assert hb["cum_contrib"] == 0.0
    assert hb["spot_usdc"] == 0.0
    assert hb["config"] == {}


def test_parse_heartbeat_mistyped_scalar_returns_none():
    bad = _good_heartbeat()
    bad["equity"] = "junk-not-a-number"
    assert contracts.parse_heartbeat(bad) is None


def test_parse_heartbeat_open_positions_must_be_int_coercible():
    bad = _good_heartbeat()
    bad["open_positions"] = "three"
    assert contracts.parse_heartbeat(bad) is None


def test_parse_heartbeat_wrong_event_name_returns_none():
    payload = _good_heartbeat()
    payload["event"] = "scan"
    assert contracts.parse_heartbeat(payload) is None


def test_parse_heartbeat_non_object_returns_none():
    assert contracts.parse_heartbeat(None) is None
    assert contracts.parse_heartbeat("junk") is None


# ── dashboard wiring: malformed heartbeat degrades, never raises ─────────────

def test_dashboard_summary_tolerates_corrupt_heartbeat(monkeypatch):
    import hermes_trader.dashboard as dash

    # A syntactically valid log line whose heartbeat has a mistyped scalar.
    corrupt_hb = _good_heartbeat()
    corrupt_hb["equity"] = "not-a-number"
    monkeypatch.setattr(
        dash, "_read_log_lines",
        lambda: [corrupt_hb, {"event": "scan", "ts": 2, "perceptions": 4}],
    )
    payload = dash._summary_payload()
    # Corrupt heartbeat degrades to "offline / zeroed", endpoint still returns.
    assert payload["status"] == "offline"
    assert payload["equity"] == 0.0
    assert payload["open_positions"] == 0
    assert payload["last_scan_triggers"] == 4


def test_dashboard_summary_accepts_valid_heartbeat(monkeypatch):
    import time as _time

    import hermes_trader.dashboard as dash

    hb = _good_heartbeat()
    hb["ts"] = int(_time.time() * 1000)  # fresh tick → feed not stale
    monkeypatch.setattr(dash, "_read_log_lines", lambda: [hb])
    payload = dash._summary_payload()
    assert payload["status"] in ("scanning", "stale")
    assert abs(payload["equity"] - 100000.0) < 0.01
    assert payload["open_positions"] == 3


def test_dashboard_risk_status_tolerates_corrupt_heartbeat(monkeypatch):
    import hermes_trader.dashboard as dash

    corrupt_hb = _good_heartbeat()
    corrupt_hb["config"] = "not-a-dict"
    monkeypatch.setattr(
        dash, "_read_log_lines",
        lambda: [corrupt_hb],
    )
    payload = dash._risk_status_payload()
    # Feed degrades to offline; no 500, mode/PnL stay at quiet defaults.
    assert payload["feed_status"] == "offline"
    assert payload["mode"] is None
