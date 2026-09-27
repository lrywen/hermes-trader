"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import math
import pathlib

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def test_positions_snapshot_round_trip(tmp_path, monkeypatch):
    """write_snapshot then read_snapshot returns the same asset_positions."""
    from hermes_trader import positions_snapshot as ps
    monkeypatch.setattr(ps, "SNAPSHOT_FILE", str(tmp_path / "snap.json"))
    rows = [{"position": {"coin": "BTC", "szi": "1.0"}}]
    ps.write_snapshot(rows)
    out = ps.read_snapshot(max_age_s=120.0)
    assert out == {"asset_positions": rows}


def test_positions_snapshot_missing_returns_none(tmp_path, monkeypatch):
    from hermes_trader import positions_snapshot as ps
    monkeypatch.setattr(ps, "SNAPSHOT_FILE", str(tmp_path / "absent.json"))
    assert ps.read_snapshot() is None


def test_positions_snapshot_stale_returns_none(tmp_path, monkeypatch):
    """A snapshot older than max_age_s is treated as absent → caller refetches."""
    import json as _json

    from hermes_trader import positions_snapshot as ps
    f = tmp_path / "snap.json"
    f.write_text(_json.dumps({"saved_at": 0, "asset_positions": [{"x": 1}]}))
    monkeypatch.setattr(ps, "SNAPSHOT_FILE", str(f))
    assert ps.read_snapshot(max_age_s=60.0) is None  # saved_at=epoch 0 → ancient


def test_positions_snapshot_legacy_unversioned_file_still_reads(tmp_path, monkeypatch):
    """P0-2c: a snapshot written before versioning (no ``version`` field) is a
    v0 legacy file with the same layout — it must still be accepted."""
    import json as _json
    import time as _time

    from hermes_trader import positions_snapshot as ps
    f = tmp_path / "snap.json"
    rows = [{"position": {"coin": "BTC", "szi": "1.0"}}]
    f.write_text(_json.dumps({"saved_at": int(_time.time() * 1000),
                              "asset_positions": rows}))
    monkeypatch.setattr(ps, "SNAPSHOT_FILE", str(f))
    out = ps.read_snapshot(max_age_s=120.0)
    assert out == {"asset_positions": rows}


def test_positions_snapshot_future_version_returns_none(tmp_path, monkeypatch):
    """P0-2c: a version newer than this binary is rejected (downgrade guard) →
    caller falls back to a live fetch instead of mis-parsing."""
    import json as _json
    import time as _time

    from hermes_trader import positions_snapshot as ps
    f = tmp_path / "snap.json"
    f.write_text(_json.dumps({"version": 999,
                              "saved_at": int(_time.time() * 1000),
                              "asset_positions": []}))
    monkeypatch.setattr(ps, "SNAPSHOT_FILE", str(f))
    assert ps.read_snapshot(max_age_s=600.0) is None


def test_positions_snapshot_wrong_typed_fields_return_none(tmp_path, monkeypatch):
    """P0-2c: a non-list asset_positions (corrupt/hand-edited file) is ignored
    rather than returned as an unusable value."""
    import json as _json
    import time as _time

    from hermes_trader import positions_snapshot as ps
    f = tmp_path / "snap.json"
    f.write_text(_json.dumps({"version": 1,
                              "saved_at": int(_time.time() * 1000),
                              "asset_positions": "not-a-list"}))
    monkeypatch.setattr(ps, "SNAPSHOT_FILE", str(f))
    assert ps.read_snapshot(max_age_s=600.0) is None


def test_dashboard_positions_prefers_snapshot_no_hl_call(monkeypatch):
    """When a fresh snapshot exists the dashboard transforms it and never calls
    fetch_account_state — this is what removes the cross-process HL load."""
    from hermes_trader import dashboard
    called = {"hl": False}
    def boom(*a, **k):
        called["hl"] = True
        raise AssertionError("fetch_account_state must not be called")
    monkeypatch.setattr(dashboard, "fetch_account_state", boom)
    monkeypatch.setattr(dashboard, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(dashboard.dsl_exit, "load_state", lambda force=False: None)
    monkeypatch.setattr(dashboard.dsl_exit, "_active_positions", {})
    monkeypatch.setattr(dashboard, "read_position_snapshot", lambda max_age_s=120.0: {
        "asset_positions": [
            {"position": {"coin": "BTC", "szi": "2.0", "entryPx": "60000",
                          "positionValue": "122000", "unrealizedPnl": "1000",
                          "marginUsed": "6000", "leverage": {"value": "20"}}},
        ],
    })
    rows = dashboard._positions_payload_uncached()
    assert called["hl"] is False
    assert len(rows) == 1 and rows[0]["coin"] == "BTC" and rows[0]["side"] == "long"


def test_dashboard_positions_falls_back_to_hl_when_no_snapshot(monkeypatch):
    """No snapshot (loop down) → dashboard does a live fetch so it still works."""
    from hermes_trader import dashboard
    monkeypatch.setattr(dashboard, "read_position_snapshot", lambda max_age_s=120.0: None)
    monkeypatch.setattr(dashboard, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(dashboard.dsl_exit, "load_state", lambda force=False: None)
    monkeypatch.setattr(dashboard.dsl_exit, "_active_positions", {})
    fetched = {"n": 0}
    def fake_fetch(user, **kw):
        fetched["n"] += 1
        return {"asset_positions": [
            {"position": {"coin": "ETH", "szi": "-5", "entryPx": "3000",
                          "positionValue": "15000", "unrealizedPnl": "-50",
                          "marginUsed": "3000", "leverage": 5}},
        ]}
    monkeypatch.setattr(dashboard, "fetch_account_state", fake_fetch)
    rows = dashboard._positions_payload_uncached()
    assert fetched["n"] == 1
    assert rows[0]["coin"] == "ETH" and rows[0]["side"] == "short"
