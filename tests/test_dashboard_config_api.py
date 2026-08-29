"""Unit tests for the dashboard config-management API (b2).

Covers:
  * GET  /api/dashboard/config           — public read
  * GET  /api/dashboard/config/schema     — key metadata
  * POST /api/dashboard/config            — operator-gated partial update
  * POST /api/dashboard/config            — 401 without token
  * POST /api/dashboard/config            — 422 on type/range validation
  * POST /api/dashboard/config            — 400 on empty/non-dict body
  * GET  /api/dashboard/config/backup     — operator-gated
  * POST /api/dashboard/config/rollback   — operator-gated round-trip
  * GET  /api/dashboard/config/history    — operator-gated audit events
  * GET  /config                          — SPA shell (200) or redirect (no dist)
"""

import os
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_trader.agents import config_store
from hermes_trader.agents.config_store import (
    read_agent_config,
    write_agent_config,
    backup_config,
)
from hermes_trader.dashboard import register_routes


_OP_TOKEN = "test-op-secret-123"


@pytest.fixture()
def client(monkeypatch):
    """Build a fresh FastAPI app with dashboard routes and a known operator token."""
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    app = FastAPI()
    register_routes(app)
    # Seed a known baseline config so tests have deterministic starting state.
    cfg = read_agent_config()
    cfg["mode"] = "OFF"
    cfg["leverage"] = 10
    cfg["max_concurrent"] = 2
    cfg["min_ai_confidence"] = 0.7
    write_agent_config(cfg, backup=False)
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


# ── read endpoints ──────────────────────────────────────────────────────────

def test_get_config_returns_full_dict(client):
    # D-FCFG-3: an UNAUTHENTICATED GET now returns only the non-sensitive
    # whitelist (mode, leverage, toggles, coarse limits) — nested blocks
    # (dsl_exit/atr_risk_sizing/signal_enforcement), allow/block lists and any
    # secret-bearing key are operator-only.
    r = client.get("/api/dashboard/config")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert "mode" in data
    assert "leverage" in data
    # Sensitive / nested material must NOT be on the anonymous response.
    assert "dsl_exit" not in data
    assert "atr_risk_sizing" not in data
    assert "signal_enforcement" not in data
    assert "coin_allowlist" not in data
    assert "coin_blocklist" not in data


def test_get_config_schema_returns_types(client):
    r = client.get("/api/dashboard/config/schema")
    assert r.status_code == 200
    schema = r.json()
    assert schema["leverage"]["type"] == "int"
    assert schema["min_ai_confidence"]["type"] == "float"
    assert schema["mode"]["type"] == "str"
    assert schema["enable_crypto"]["type"] == "bool"
    assert schema["coin_allowlist"]["type"] == "list"
    assert schema["dsl_exit"]["type"] == "object"


def test_config_page_served_by_spa(client):
    # The inline /config page was removed (legacy HTML deletion); the path is
    # now handled by the history-mode SPA catch-all: the index.html shell when
    # /app/web-dist exists (200), otherwise a redirect to the SPA root (302).
    r = client.get("/config", follow_redirects=False)
    assert r.status_code in (200, 302)
    if r.status_code == 302:
        # No built SPA in the test env: catch-all bounces to /, which in
        # turn redirects to /web/.
        assert r.headers["location"] in ("/", "/web/")
    else:
        assert "<!doctype html" in r.text.lower()
    # The old inline page's marker must be gone.
    assert "cfg-grid" not in r.text


# ── POST /api/dashboard/config ──────────────────────────────────────────────

def test_post_config_requires_token(client):
    r = client.post("/api/dashboard/config", json={"updates": {"leverage": 5}})
    assert r.status_code == 401


def test_post_config_with_token_updates_value(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 5}},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["applied"]["leverage"] == 5
    # Verify persistence
    cfg = read_agent_config()
    assert cfg["leverage"] == 5


def test_post_config_multiple_keys(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 8, "max_concurrent": 4, "mode": "LIVE"}},
        headers=_auth(),
    )
    assert r.status_code == 200
    cfg = read_agent_config()
    assert cfg["leverage"] == 8
    assert cfg["max_concurrent"] == 4
    assert cfg["mode"] == "LIVE"


def test_post_config_empty_updates_400(client):
    r = client.post("/api/dashboard/config", json={"updates": {}}, headers=_auth())
    assert r.status_code == 400


def test_post_config_non_dict_updates_400(client):
    r = client.post("/api/dashboard/config", json={"updates": [1, 2]}, headers=_auth())
    assert r.status_code == 400


def test_post_config_unknown_key_422(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"nonexistent_key_xyz": 1}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_post_config_type_mismatch_422(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": "not-an-int"}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_post_config_range_validation_leverage(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 999}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_post_config_range_validation_confidence(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"min_ai_confidence": 1.5}},
        headers=_auth(),
    )
    assert r.status_code == 422


def test_post_config_bool_rejects_non_bool(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"enable_crypto": "yes"}},
        headers=_auth(),
    )
    assert r.status_code == 422


# ── F19: type + range coverage for every numeric CANONICAL_DEFAULTS key ──────

@pytest.mark.parametrize("key,bad_value", [
    ("tp_scale_fraction", 1.5),
    ("tp_scale_fraction", -0.1),
    ("crowded_with_min_conf", 1.2),
    ("min_available_margin_pct", -0.05),
    ("against_funding_min_conf", 2.0),
    ("against_funding_min_score", 150.0),
    ("chop_burst_min_score", -1.0),
    ("strong_trend_threshold", 1.5),
    ("trend_threshold", -0.2),
    ("neutral_threshold", 9.0),
    ("whale_size_multiplier", -1.0),
    ("force_execute_composite", 150),
    ("research_cooldown_min", -5),
    ("held_research_interval_min", -1),
    ("ta_sidestep_min_slow_burn_count", -9),
    ("force_execute_slow_burn_count", -2),
    # wrong types must be rejected too
    ("whale_size_multiplier", "big"),
    ("research_cooldown_min", 3.5),
    ("force_execute_composite", True),
])
def test_f19_newly_covered_numeric_keys_reject_invalid(key, bad_value):
    from hermes_trader.dashboard import _validate_config_updates
    errors = _validate_config_updates({key: bad_value})
    assert any(key in e for e in errors), f"{key}={bad_value!r} not rejected: {errors}"


def test_f19_newly_covered_keys_accept_canonical_defaults():
    """Every F19-covered key's CANONICAL_DEFAULTS value must pass validation —
    the default seed config must never be self-rejecting."""
    from hermes_trader.dashboard import _validate_config_updates
    covered = [
        "tp_scale_fraction", "crowded_with_min_conf", "min_available_margin_pct",
        "research_cooldown_min", "held_research_interval_min",
        "force_execute_composite", "ta_sidestep_min_slow_burn_count",
        "force_execute_slow_burn_count", "whale_size_multiplier",
        "chop_burst_min_score", "against_funding_min_conf",
        "against_funding_min_score", "strong_trend_threshold",
        "trend_threshold", "neutral_threshold",
    ]
    updates = {k: config_store.CANONICAL_DEFAULTS[k] for k in covered}
    assert _validate_config_updates(updates) == []


# ── F26: tunable cache/SSE constants carry sane defaults ────────────────────

def test_f26_tunable_constants_have_expected_defaults():
    """The env-overridable ops constants exist with the prior hard-coded
    defaults so behaviour is unchanged when no env is set."""
    from hermes_trader import dashboard
    from hermes_trader.dashboard_routes import public
    assert dashboard._POSITIONS_CACHE_TTL_S == 5.0
    assert dashboard._SSE_REPLAY_LINES == 500
    assert dashboard._SSE_HEARTBEAT_S == 15.0
    assert dashboard._EQUITY_DIP_RATIO == 0.7
    assert dashboard._EQUITY_DIP_WINDOW == 15
    assert public._SUMMARY_TTL_S == 2.0
    assert public._EQUITY_CURVE_TTL_S == 30.0
    assert public._CLOSED_TRADES_TTL_S == 10.0


def test_post_config_creates_backup(client):
    """A successful POST must back up the previous config before overwriting."""
    before = read_agent_config()["leverage"]
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": before + 1}},
        headers=_auth(),
    )
    assert r.status_code == 200
    bak = backup_config()
    assert bak is not None
    assert bak["leverage"] == before


def test_post_config_audit_log(client):
    """A successful POST must append a config_update event to the session log."""
    from hermes_trader import session_log
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"max_concurrent": 5}},
        headers=_auth(),
    )
    assert r.status_code == 200
    events = session_log.tail(50)
    found = [e for e in events if e.get("event") == "config_update"
             and e.get("updates", {}).get("max_concurrent") == 5]
    assert found, "config_update event not found in session log"


# ── backup / rollback ───────────────────────────────────────────────────────

def test_get_backup_requires_token(client):
    r = client.get("/api/dashboard/config/backup")
    assert r.status_code == 401


def test_get_backup_after_write(client):
    client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 7}},
        headers=_auth(),
    )
    r = client.get("/api/dashboard/config/backup", headers=_auth())
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert data["config"]["leverage"] == 10  # value before the write


def test_rollback_restores_previous(client):
    # Write a change (backup = original leverage=10)
    client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 3}},
        headers=_auth(),
    )
    assert read_agent_config()["leverage"] == 3
    # Roll back
    r = client.post("/api/dashboard/config/rollback", headers=_auth())
    assert r.status_code == 200
    assert read_agent_config()["leverage"] == 10


def test_rollback_requires_token(client):
    r = client.post("/api/dashboard/config/rollback")
    assert r.status_code == 401


# ── history ─────────────────────────────────────────────────────────────────

def test_history_requires_token(client):
    r = client.get("/api/dashboard/config/history")
    assert r.status_code == 401


def test_history_records_updates(client):
    client.post(
        "/api/dashboard/config",
        json={"updates": {"max_concurrent": 6}},
        headers=_auth(),
    )
    r = client.get("/api/dashboard/config/history", headers=_auth())
    assert r.status_code == 200
    history = r.json()["history"]
    assert any(e.get("event") == "config_update" for e in history)


def test_history_records_rollback(client):
    client.post(
        "/api/dashboard/config",
        json={"updates": {"leverage": 4}},
        headers=_auth(),
    )
    client.post("/api/dashboard/config/rollback", headers=_auth())
    r = client.get("/api/dashboard/config/history", headers=_auth())
    assert r.status_code == 200
    events = {e["event"] for e in r.json()["history"]}
    assert "config_rollback" in events


# ── nested object update ────────────────────────────────────────────────────

def test_post_config_nested_object(client):
    """debate_gate is a nested dict — explicit sub-keys replace; canonical-only
    sub-keys are backfilled by the read-path deep merge (R12-C1)."""
    new_gate = {
        "enabled": False,
        "min_agreement": 0.8,
        "min_agree_count": 4,
        "analyst3_default": True,
    }
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"debate_gate": new_gate}},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    gate = read_agent_config()["debate_gate"]
    # All explicitly written keys land verbatim (whole-block replacement).
    assert gate == new_gate
    # A canonical-only sub-key omitted from the payload is backfilled by the
    # read-path deep merge (R12-C1: analyst3_default is now a canonical key).
    r2 = client.post(
        "/api/dashboard/config",
        json={"updates": {"debate_gate": {"enabled": False, "min_agreement": 0.9}}},
        headers=_auth(),
    )
    assert r2.status_code == 200, r2.text
    merged = read_agent_config()["debate_gate"]
    assert merged["min_agreement"] == 0.9  # explicit override lands
    assert merged["analyst3_default"] is False  # canonical default backfilled


def test_post_config_list_value(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"coin_allowlist": ["BTC", "ETH"]}},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert read_agent_config()["coin_allowlist"] == ["BTC", "ETH"]


def test_post_config_list_wrong_type_422(client):
    r = client.post(
        "/api/dashboard/config",
        json={"updates": {"coin_allowlist": "BTC"}},
        headers=_auth(),
    )
    assert r.status_code == 422


# ── manual trading surface (server.py place-order / close-position) ─────────
#
# These endpoints are registered on the SERVER app (server.py calls
# dashboard.register_routes(app)), not on a bare dashboard-only app — so hit
# them through server.app with the operator token env set.

import pytest as _pytest  # noqa: E402


@_pytest.fixture()
def server_client(monkeypatch):
    """TestClient over the real server app with a known operator token."""
    from fastapi.testclient import TestClient
    from hermes_trader import server
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", _OP_TOKEN)
    return TestClient(server.app)


def test_manual_place_order_blocked_in_off_mode(server_client, monkeypatch):
    """Opening a NEW position via the manual endpoint must be refused while
    Mode=OFF (the operator endpoint must not bypass the autonomous OFF gate)."""
    from hermes_trader import server
    monkeypatch.setattr(server, "read_agent_config",
                        lambda: {"mode": "OFF", "leverage": 10})
    r = server_client.post(
        "/api/hl/place-order",
        json={"coin": "BTC", "side": "long", "leverage": 5},
        headers=_auth(),
    )
    assert r.status_code == 409


def test_manual_place_order_rejects_invalid_side(server_client, monkeypatch):
    """An unrecognized side is a 400 (input validation), reached only once the
    OFF gate is cleared."""
    from hermes_trader import server
    monkeypatch.setattr(server, "read_agent_config",
                        lambda: {"mode": "LIVE", "leverage": 10})
    r = server_client.post(
        "/api/hl/place-order",
        json={"coin": "BTC", "side": "sideways", "leverage": 5},
        headers=_auth(),
    )
    assert r.status_code == 400


def test_manual_place_order_rejects_leverage_above_cap(server_client, monkeypatch):
    """Leverage above the 10x canonical norm is clamped, but a non-numeric /
    out-of-band value is rejected with 400 rather than silently applied."""
    from hermes_trader import server
    monkeypatch.setattr(server, "read_agent_config",
                        lambda: {"mode": "LIVE", "leverage": 10})
    r = server_client.post(
        "/api/hl/place-order",
        json={"coin": "BTC", "side": "long", "leverage": "not-a-number"},
        headers=_auth(),
    )
    assert r.status_code == 400


def test_manual_close_hip3_coin_name_not_upper_corrupted(server_client, monkeypatch):
    """HIP-3 coin names (xyz:MU, vntl:*) carry a ':' and must NOT be .upper()'d
    as a whole. A manual close in Mode=OFF is allowed (flatten is risk
    reduction) and passes the coin through verbatim to close_position_market."""
    from hermes_trader import server

    captured = {}

    def _fake_close(coin):
        captured["coin"] = coin
        return {"ok": True, "coin": coin, "side": "long"}

    monkeypatch.setattr(server, "close_position_market", _fake_close)
    # Mode stays OFF — close (flatten) must still go through.
    monkeypatch.setattr(server, "read_agent_config",
                        lambda: {"mode": "OFF", "leverage": 10})
    r = server_client.post(
        "/api/hl/close-position",
        json={"coin": "xyz:MU"},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert captured["coin"] == "xyz:MU"  # not "XYZ:MU"


# ── F11: authenticated operator write rate limit ───────────────────────────

def test_operator_write_rate_limit_returns_429(client, monkeypatch):
    """F11: a valid token does not grant unlimited writes. Past the per-IP
    sliding-window cap, state-changing requests get 429 + Retry-After; reads
    are never limited."""
    from hermes_trader import dashboard
    monkeypatch.setattr(dashboard, "_WRITE_RATE_MAX", 3)
    monkeypatch.setattr(dashboard, "_WRITE_RATE_WINDOW_S", 60.0)
    dashboard._write_hits.clear()
    # First 3 authenticated writes within the window succeed.
    for _ in range(3):
        r = client.post("/api/dashboard/operator/mode",
                        json={"mode": "OFF"}, headers=_auth())
        assert r.status_code == 200, r.text
    # 4th write trips the cap.
    r = client.post("/api/dashboard/operator/mode",
                    json={"mode": "OFF"}, headers=_auth())
    assert r.status_code == 429
    assert r.headers.get("retry-after")
    # Unauthenticated writes are still 401, not 429-masked.
    r_bad = client.post("/api/dashboard/operator/mode", json={"mode": "OFF"})
    assert r_bad.status_code == 401
    # Reads (GET) are NOT rate-limited.
    for _ in range(10):
        rr = client.get("/api/dashboard/operator/config", headers=_auth())
        assert rr.status_code == 200


def test_operator_write_rate_disabled_when_max_zero(client, monkeypatch):
    """HERMES_OP_WRITE_RATE_MAX=0 turns the limiter off (escape hatch)."""
    from hermes_trader import dashboard
    monkeypatch.setattr(dashboard, "_WRITE_RATE_MAX", 0)
    dashboard._write_hits.clear()
    for _ in range(6):
        r = client.post("/api/dashboard/operator/mode",
                        json={"mode": "OFF"}, headers=_auth())
        assert r.status_code == 200, r.text


# ── F22: operator manual-close audit trail ─────────────────────────────────

def test_operator_mode_shadow_accepted_and_persisted(client):
    """SHADOW is a first-class mode: the operator endpoint accepts it and the
    canonical value "SHADOW" is persisted (full pipeline, no real orders)."""
    r = client.post("/api/dashboard/operator/mode",
                    json={"mode": "shadow"}, headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "SHADOW"
    assert read_agent_config()["mode"] == "SHADOW"


def test_operator_mode_paper_rejected_400(client):
    """PAPER must NOT be accepted: the executor only shadows on the literal
    "SHADOW" and skips only on "OFF", so an unknown value like "PAPER" would
    be treated as LIVE and trade real funds. Reject it at the boundary."""
    r = client.post("/api/dashboard/operator/mode",
                    json={"mode": "PAPER"}, headers=_auth())
    assert r.status_code == 400, r.text
    assert read_agent_config().get("mode") != "PAPER"


def test_operator_close_writes_audit_event(client, monkeypatch, tmp_path):
    """F22: a web operator close must persist an operator_action event to the
    session log AND fork it into the authoritative events.jsonl."""
    from hermes_trader import session_log, event_log
    sess = tmp_path / "session.jsonl"
    ev = tmp_path / "events.jsonl"
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", str(sess))
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev))
    monkeypatch.setattr(
        "hermes_trader.agents.executor.close_position_market",
        lambda coin: {"ok": True, "coin": coin, "side": "long",
                      "fill_px": 50000.0, "realized_pnl_pct": 1.2,
                      "leverage": 10},
    )
    r = client.post("/api/dashboard/operator/close",
                    json={"coin": "btc"}, headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    sess_rows = [json.loads(l) for l in sess.read_text().splitlines() if l.strip()]
    op = [e for e in sess_rows if e.get("event") == "operator_action"]
    assert op, "operator_action event missing from session log"
    rec = op[-1]
    assert rec["action"] == "close"
    assert rec["coin"] == "BTC"          # bare ticker normalized to upper
    assert rec["via"] == "web"
    assert rec["result"]["ok"] is True
    assert rec["result"]["fill_px"] == 50000.0

    # Forked into events.jsonl (authoritative feed).
    assert ev.exists()
    ev_rows = [json.loads(l) for l in ev.read_text().splitlines() if l.strip()]
    assert any(e.get("event") == "operator_action"
               and e.get("payload", {}).get("action") == "close"
               for e in ev_rows)


# ── F20: POST /api/agent/config RMW must be serialized ─────────────────────

def test_agent_config_endpoint_concurrent_merge(client):
    """F20: POST /api/agent/config used to do an unlocked read → merge →
    write, so concurrent requests each merging their own key lost all but
    the last writer's change. Under update_agent_config's flock the merges
    serialize and every key survives. D-FCFG-4: the payload keys must be
    canonical (unknown keys are now 422'd)."""
    import threading
    from fastapi.testclient import TestClient
    from hermes_trader import server

    # Each thread writes a DISTINCT canonical scalar key — under correct
    # flock serialization every key must survive.
    thread_keys = {
        0: {"leverage": 5},
        1: {"max_concurrent": 4},
        2: {"min_ai_confidence": 0.6},
        3: {"tp_scale_fraction": 0.75},
        4: {"crowded_with_min_conf": 0.65},
    }

    errors = []

    def _merge_key(i: int) -> None:
        # Each thread gets its own TestClient (portal threads are not shared
        # safely); the endpoint itself is the system under test.
        c = TestClient(server.app)
        r = c.post(
            "/api/agent/config",
            json=thread_keys[i],
            headers=_auth(),
        )
        if r.status_code != 200:
            errors.append((i, r.status_code, r.text))

    threads = [threading.Thread(target=_merge_key, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    final = read_agent_config()
    assert final["leverage"] == 5
    assert final["max_concurrent"] == 4
    assert final["min_ai_confidence"] == 0.6
    assert final["tp_scale_fraction"] == 0.75
    assert final["crowded_with_min_conf"] == 0.65


# ── D-FCFG-3: two-tier config endpoint (deep audit 2026-08-28) ─────────────

def test_anonymous_config_is_whitelist_only(client):
    """D-FCFG-3: anonymous GET /api/dashboard/config returns posture/toggles
    but never nested strategy blocks or allow/block lists."""
    r = client.get("/api/dashboard/config")
    assert r.status_code == 200
    data = r.json()
    # Public posture fields are present.
    assert data.get("mode") == "OFF"
    assert "enable_crypto" in data
    assert "leverage" in data
    assert "min_ai_confidence" in data
    # Everything sensitive is absent.
    for sensitive in ("dsl_exit", "atr_risk_sizing", "signal_enforcement",
                      "coin_allowlist", "coin_blocklist",
                      "hip3_dex_allowlist", "hip3_dex_blocklist"):
        assert sensitive not in data, f"{sensitive} leaked to anonymous config"


def test_operator_config_public_endpoint_returns_full_redacted(client):
    """D-FCFG-3: with a valid operator token, the PUBLIC config endpoint
    returns the full document with secret fields redacted by _redact."""
    cfg = read_agent_config()
    cfg["openrouter_api_key"] = "sk-secret-value-1234567890"
    cfg["mode"] = "OFF"
    write_agent_config(cfg, backup=False)
    r = client.get("/api/dashboard/config", headers=_auth())
    assert r.status_code == 200
    data = r.json()
    # Full document — nested blocks are present for the operator.
    assert "dsl_exit" in data
    assert "atr_risk_sizing" in data
    # Secret scrubbed.
    assert data.get("openrouter_api_key") == "<redacted>"


def test_operator_config_endpoint_bad_token_401_no_lockout(client):
    """D-FCFG-3: presenting a bad token on the config endpoint is 401;
    presenting NO token never counts as a failed login (anonymous viewers must
    not trip the brute-force lockout)."""
    # Anonymous request: 200 (whitelist), not a counted failure.
    r0 = client.get("/api/dashboard/config")
    assert r0.status_code == 200
    # Six anonymous requests in a row must NOT lock the IP out (limit is 5).
    for _ in range(6):
        assert client.get("/api/dashboard/config").status_code == 200
    # A wrong token IS a failure: 401 + challenge.
    r_bad = client.get("/api/dashboard/config",
                       headers={"Authorization": "Bearer wrong-token"})
    assert r_bad.status_code == 401
    assert "www-authenticate" in r_bad.headers


# ── D-FCFG-3: feed ticket minting ──────────────────────────────────────────

def test_feed_ticket_requires_token(client):
    r = client.post("/api/dashboard/operator/feed-ticket")
    assert r.status_code == 401


def test_feed_ticket_minted_for_operator(client):
    r = client.post("/api/dashboard/operator/feed-ticket", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("ticket"), str)
    assert len(body["ticket"]) >= 32
    assert body["expires_in_s"] > 0


# ── D-FCFG-3: feed history / stream authorization ──────────────────────────

_SAMPLE_EVENTS = [
    {"event": "loop_heartbeat", "ts": 1, "equity": 100.0, "daily_pnl": 1.0,
     "config": {"mode": "LIVE", "crypto": True, "hip3": True,
                "secret_dsl_exit_ladder": {"max_loss_pct": 0.4}}},
    {"event": "scan", "ts": 2, "coin": "BTC", "triggers": 2},
    {"event": "near_miss", "ts": 3, "coin": "ETH", "reason": "confidence 0.6 < 0.7"},
    {"event": "error", "ts": 4, "message": "something failed"},
    {"event": "research", "ts": 5, "coin": "BTC", "verdict": "long",
     "score": 0.91, "perceptions": {"trend": "up"}},
    {"event": "execute", "ts": 6, "coin": "BTC", "side": "long",
     "size": 1000.0, "leverage": 10, "entry_px": 50000.0},
    {"event": "place_order", "ts": 7, "coin": "BTC", "side": "long",
     "notional": 800.0},
    {"event": "close_position", "ts": 8, "coin": "ETH", "pnl_usd": -2.5},
    {"event": "config_update", "ts": 9, "updates": {"leverage": 5}},
    {"event": "operator_action", "ts": 10, "action": "close", "coin": "BTC"},
    {"event": "dsl_exit", "ts": 11, "coin": "SOL", "floor_px": 123.4},
]


def _patch_feed_log(monkeypatch, events):
    """Make session_log.tail() return our sample events (both the name
    imported into public routes and the module, so feed_history sees them)."""
    from hermes_trader import session_log
    from hermes_trader.dashboard_routes import public as public_routes
    monkeypatch.setattr(public_routes.session_log, "tail",
                        lambda n=10: list(events[-n:]), raising=False)
    monkeypatch.setattr(session_log, "tail",
                        lambda n=10: list(events[-n:]), raising=False)


def test_feed_history_anonymous_is_whitelisted(client, monkeypatch):
    """D-FCFG-3: anonymous /api/feed/history only returns whitelisted event
    types, and even those have nested/sensitive fields stripped."""
    _patch_feed_log(monkeypatch, _SAMPLE_EVENTS)
    r = client.get("/api/feed/history?limit=500")
    assert r.status_code == 200
    evts = r.json()["events"]
    types = {e["event"] for e in evts}
    # Public event types survive.
    assert {"loop_heartbeat", "scan", "near_miss", "error"} <= types
    # Sensitive event types are dropped wholesale.
    assert types.isdisjoint({
        "research", "execute", "place_order", "close_position",
        "config_update", "operator_action", "dsl_exit",
    })
    # Heartbeat's full config block is projected to posture only.
    hb = next(e for e in evts if e["event"] == "loop_heartbeat")
    assert set(hb["config"].keys()) == {"mode", "crypto", "hip3"}
    assert "secret_dsl_exit_ladder" not in hb["config"]
    # Equity scalars remain (already public via the summary endpoint).
    assert hb["equity"] == 100.0


def test_feed_history_operator_header_gets_full(client, monkeypatch):
    _patch_feed_log(monkeypatch, _SAMPLE_EVENTS)
    r = client.get("/api/feed/history?limit=500", headers=_auth())
    assert r.status_code == 200
    types = {e["event"] for e in r.json()["events"]}
    assert {"execute", "research", "place_order", "config_update",
            "operator_action", "dsl_exit"} <= types


def test_feed_history_with_ticket_gets_full(client, monkeypatch):
    _patch_feed_log(monkeypatch, _SAMPLE_EVENTS)
    tr = client.post("/api/dashboard/operator/feed-ticket", headers=_auth())
    ticket = tr.json()["ticket"]
    r = client.get(f"/api/feed/history?limit=500&ticket={ticket}")
    assert r.status_code == 200
    types = {e["event"] for e in r.json()["events"]}
    assert "execute" in types and "config_update" in types


def test_feed_history_bad_ticket_falls_back_to_public(client, monkeypatch):
    _patch_feed_log(monkeypatch, _SAMPLE_EVENTS)
    r = client.get("/api/feed/history?limit=500&ticket=not-a-real-ticket")
    assert r.status_code == 200
    types = {e["event"] for e in r.json()["events"]}
    assert "execute" not in types
    assert "scan" in types


def test_feed_ticket_expires(monkeypatch):
    """D-FCFG-3: a ticket past its TTL is rejected (gracefully → public)."""
    from hermes_trader import dashboard
    monkeypatch.setattr(dashboard, "_FEED_TICKET_TTL_S", 0.01)
    ticket, _ = dashboard._issue_feed_ticket()
    assert dashboard._feed_ticket_valid(ticket)
    import time as _time
    _time.sleep(0.05)
    assert not dashboard._feed_ticket_valid(ticket)


def test_public_feed_filter_unit():
    """D-FCFG-3: direct unit coverage of the projection."""
    from hermes_trader.dashboard import _public_feed_filter
    # Non-whitelisted event → None (dropped entirely).
    assert _public_feed_filter({"event": "execute", "coin": "BTC"}) is None
    assert _public_feed_filter({"event": "config_update"}) is None
    # scan keeps scalars, drops nested dicts.
    out = _public_feed_filter({
        "event": "scan", "ts": 1, "triggers": 2,
        "nested": {"secret": "x"}, "items": [1, 2, 3],
    })
    assert out["triggers"] == 2
    assert "nested" not in out and "items" not in out
    # heartbeat config is narrowed.
    hb = _public_feed_filter({
        "event": "loop_heartbeat", "ts": 1, "equity": 5.0,
        "config": {"mode": "LIVE", "crypto": True, "hip3": False,
                   "dsl_exit": {"max_loss_pct": 0.4}},
    })
    assert hb["config"] == {"mode": "LIVE", "crypto": True, "hip3": False}
    assert hb["equity"] == 5.0


def test_feed_stream_anonymous_projects(client, monkeypatch):
    """D-FCFG-3: the SSE generator with public_only=True never emits a
    sensitive event type (drives _tail_log_sse directly so the infinite poll
    loop is bounded)."""
    import asyncio
    from hermes_trader.dashboard import _tail_log_sse

    async def _collect():
        gen = _tail_log_sse(public_only=True)
        chunks = []
        # Replay buffer is emitted before the first sleep; grab the first
        # chunk then close the generator (the live loop would block forever).
        async for chunk in gen:
            chunks.append(chunk)
            if len(chunks) >= 3:
                break
        await gen.aclose()
        return chunks

    _patch_feed_log(monkeypatch, _SAMPLE_EVENTS)
    # session_log.tail is awaited inside the generator via asyncio.to_thread;
    # patch the module-level symbol the dashboard module uses.
    from hermes_trader import dashboard, session_log
    monkeypatch.setattr(dashboard.session_log, "tail",
                        lambda n=10: list(_SAMPLE_EVENTS[-n:]), raising=False)
    monkeypatch.setattr(session_log, "tail",
                        lambda n=10: list(_SAMPLE_EVENTS[-n:]), raising=False)
    chunks = asyncio.new_event_loop().run_until_complete(_collect())
    text = "".join(chunks)
    for bad in ("place_order", "close_position", "config_update",
                "operator_action", '"execute"', '"research"', "dsl_exit"):
        assert bad not in text, f"sensitive token {bad!r} leaked to public SSE"
    assert "loop_heartbeat" in text or "scan" in text
