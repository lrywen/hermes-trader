"""O-4 (audit 2026-08-31): risk-status viewer card data plane.

Covers the two backend additions that feed the frontend's three new
monitoring cards (risk-status viewer / liquidation-price alert / feed
monitor):

  * ``GET /api/dashboard/risk-status`` — a public, read-only aggregate of
    the tiered breakers (global halt / per-coin circuit) from the flushed
    .agent-memory.json plus mode / daily-PnL-vs-kill / feed liveness from
    the latest loop heartbeat in the session log. Anonymous like
    summary/positions (only non-sensitive operational signals), and must
    never 500 when a backing read fails.
  * position rows now carry ``liq_px`` — HL's ``liquidationPx`` string run
    through the executor's defensive parser, so "0"/None/missing map to
    None (the card renders "—") instead of a false 0.0 liquidation level.

Every test is offline: the memory singleton and session log are
monkeypatched, no HL / network calls are made.
"""

import time

import pytest
from fastapi.testclient import TestClient

from hermes_trader.server import app


def _clear_risk_status_cache() -> None:
    """The endpoint serves through the same 2s TTL cache as summary; drop
    the entry so each test builds a fresh payload instead of seeing a
    previous test's cached body."""
    from hermes_trader import dashboard
    with dashboard._TTL_CACHE_LOCK:
        dashboard._TTL_CACHE.pop("risk-status", None)


def _hb_event(*, ts_ms: int, mode: str = "live", daily_pnl: float = 5.0,
              kill: float = -30.0, open_positions: int = 2) -> dict:
    return {
        "event": "loop_heartbeat",
        "ts": ts_ms,
        "daily_pnl": daily_pnl,
        "open_positions": open_positions,
        "config": {
            "mode": mode, "kill": kill, "frac": 0.1, "lev": 10,
            "max_conc": 3, "notional_cap": 5000, "cool_min": 15,
            "min_conf": 0.6, "crypto": True, "hip3": True,
        },
    }


@pytest.fixture()
def client(monkeypatch):
    _clear_risk_status_cache()
    # Default: breaker state from the real memory singleton is empty/quiet.
    from hermes_trader.agents.memory import memory as mem
    monkeypatch.setattr(mem, "_coin_circuit", {})
    monkeypatch.setattr(mem, "_global_halt_until_ms", 0)
    yield TestClient(app, raise_server_exceptions=False)
    _clear_risk_status_cache()


# ─────────────────────────── memory snapshot ────────────────────────────

def test_risk_status_snapshot_reports_armed_breakers(monkeypatch):
    """risk_status_snapshot is the richer read-only (non-mutating) breaker
    view: armed global halt + remaining minutes, and the per-coin map."""
    from hermes_trader.agents.memory import memory as mem
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(mem, "_global_halt_until_ms", now_ms + 30 * 60_000)
    monkeypatch.setattr(mem, "_coin_circuit", {
        "BTC": now_ms + 15 * 60_000,
        "ETH": now_ms - 5 * 60_000,   # expired → excluded, NOT purged
    })
    snap = mem.risk_status_snapshot()
    assert snap["global_halt"] is True
    assert 29.0 <= snap["global_halt_remaining_min"] <= 30.0
    assert set(snap["coin_circuits"]) == {"BTC"}
    assert 14.0 <= snap["coin_circuits"]["BTC"] <= 15.0
    assert snap["armed_coins"] == 1
    # Read-only: the expired entry must still be present (no purge).
    assert "ETH" in mem._coin_circuit


def test_risk_status_snapshot_quiet_defaults(monkeypatch):
    from hermes_trader.agents.memory import memory as mem
    monkeypatch.setattr(mem, "_coin_circuit", {})
    monkeypatch.setattr(mem, "_global_halt_until_ms", 0)
    # Audit 2026-09-03: the snapshot refreshes breaker state from the shared
    # tmp memory file first; stub it so the drawdown fields below are
    # deterministic instead of reloaded from another test's flushed state.
    monkeypatch.setattr(mem, "refresh_risk_state_from_disk", lambda: None)
    monkeypatch.setattr(mem, "_peak_equity", 0.0)
    monkeypatch.setattr(mem, "_equity", 0.0)
    mem._equity_trail.clear()
    monkeypatch.setattr(mem, "_dd_frozen_since_ms", 0)
    monkeypatch.setattr(mem, "_dd_last_baseline_ms", 0)
    snap = mem.risk_status_snapshot()
    assert snap == {
        "global_halt": False,
        "global_halt_remaining_min": 0.0,
        "coin_circuits": {},
        "armed_coins": 0,
        # Audit 2026-09-03: B-F7 drawdown breaker card (quiet defaults).
        "drawdown": {
            "frozen": False,
            "dd_pct": 0.0,
            "threshold_pct": 15.0,
            "peak_equity": 0.0,
            "all_time_peak_equity": 0.0,
            "equity": 0.0,
            "window_days": 14.0,
            "frozen_since_ms": 0,
            "frozen_for_min": 0.0,
            "cooldown_hours": 24.0,
            "cooldown_remaining_min": 0.0,
            "last_baseline_ms": 0,
            "trail_samples": 0,
        },
    }


# ─────────────────────────── payload builder ────────────────────────────

def test_payload_live_heartbeat_merges_memory_and_feed(monkeypatch):
    """A fresh heartbeat + armed coin circuit → live feed, mode/pnl/kill
    passthrough, breaker map from memory."""
    from hermes_trader import dashboard
    from hermes_trader.agents.memory import memory as mem
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(mem, "_coin_circuit", {"xyz:NVDA": now_ms + 12 * 60_000})
    monkeypatch.setattr(mem, "_global_halt_until_ms", 0)
    monkeypatch.setattr(
        dashboard, "_read_log_lines",
        lambda: [_hb_event(ts_ms=now_ms - 3000, daily_pnl=12.5)],
    )
    out = dashboard._risk_status_payload()
    assert out["feed_status"] == "live"
    assert out["feed_age_s"] == 3
    assert out["mode"] == "LIVE"
    assert out["daily_pnl"] == 12.5
    assert out["daily_loss_limit"] == -30.0
    assert out["kill_armed"] is False
    assert out["open_positions"] == 2
    assert out["armed_coins"] == 1
    assert "xyz:NVDA" in out["coin_circuits"]
    assert out["global_halt"] is False


def test_payload_offline_without_heartbeat(monkeypatch):
    from hermes_trader import dashboard
    monkeypatch.setattr(dashboard, "_read_log_lines", list)
    out = dashboard._risk_status_payload()
    assert out["feed_status"] == "offline"
    assert out["feed_age_s"] is None
    assert out["mode"] is None
    assert out["kill_armed"] is False


def test_payload_stale_when_heartbeat_old(monkeypatch):
    from hermes_trader import dashboard
    now_ms = int(time.time() * 1000)
    # Default stale threshold is 180s; a heartbeat 10 minutes old is stale.
    monkeypatch.setattr(
        dashboard, "_read_log_lines",
        lambda: [_hb_event(ts_ms=now_ms - 600_000)],
    )
    out = dashboard._risk_status_payload()
    assert out["feed_status"] == "stale"
    assert out["feed_age_s"] >= 590


def test_payload_kill_armed_when_daily_loss_hits_limit(monkeypatch):
    """daily_pnl is negative on a loss day; kill is the negative USD floor.
    pnl <= floor means the hard kill-switch has tripped."""
    from hermes_trader import dashboard
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(
        dashboard, "_read_log_lines",
        lambda: [_hb_event(ts_ms=now_ms - 1000, daily_pnl=-31.2, kill=-30.0)],
    )
    out = dashboard._risk_status_payload()
    assert out["kill_armed"] is True
    assert out["daily_pnl"] == -31.2
    # Still above the floor → not armed.
    monkeypatch.setattr(
        dashboard, "_read_log_lines",
        lambda: [_hb_event(ts_ms=now_ms - 1000, daily_pnl=-10.0, kill=-30.0)],
    )
    assert dashboard._risk_status_payload()["kill_armed"] is False


def test_payload_global_halt_surfaces(monkeypatch):
    from hermes_trader import dashboard
    from hermes_trader.agents.memory import memory as mem
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(mem, "_global_halt_until_ms", now_ms + 45 * 60_000)
    monkeypatch.setattr(
        dashboard, "_read_log_lines",
        lambda: [_hb_event(ts_ms=now_ms - 2000)],
    )
    out = dashboard._risk_status_payload()
    assert out["global_halt"] is True
    assert 44.0 <= out["global_halt_remaining_min"] <= 45.0


def test_payload_degrades_when_backing_reads_raise(monkeypatch):
    """A broken memory file or unreadable session log must never turn the
    card into a 500 — the endpoint still returns the quiet defaults."""
    from hermes_trader import dashboard
    from hermes_trader.agents import memory as memory_mod

    def _boom(*a, **k):
        raise RuntimeError("simulated memory corruption")

    monkeypatch.setattr(memory_mod, "memory", _boom)
    monkeypatch.setattr(dashboard, "_read_log_lines", _boom)
    out = dashboard._risk_status_payload()
    assert out["feed_status"] == "offline"
    assert out["global_halt"] is False
    assert out["coin_circuits"] == {}
    assert out["armed_coins"] == 0
    assert "ts" in out


# ─────────────────────────── HTTP endpoint ──────────────────────────────

def test_endpoint_anonymous_200_and_shape(client, monkeypatch):
    """risk-status is public read-only like summary/positions: no operator
    token, no internal-peer requirement, and returns the full field set."""
    from hermes_trader import dashboard
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(
        dashboard, "_read_log_lines",
        lambda: [_hb_event(ts_ms=now_ms - 1500, mode="paper")],
    )
    _clear_risk_status_cache()
    resp = client.get("/api/dashboard/risk-status")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "global_halt", "global_halt_remaining_min", "coin_circuits",
        "armed_coins", "mode", "daily_pnl", "daily_loss_limit",
        "kill_armed", "open_positions", "feed_status", "feed_age_s", "ts",
    ):
        assert key in body
    assert body["mode"] == "PAPER"
    assert body["feed_status"] == "live"


def test_endpoint_survives_backend_failure(client, monkeypatch):
    """Even with every backing read raising, the HTTP layer returns 200
    with degraded defaults — the dashboard card must show 'offline', not
    an error boundary."""
    from hermes_trader import dashboard
    from hermes_trader.agents import memory as memory_mod

    def _boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(memory_mod, "memory", _boom)
    monkeypatch.setattr(dashboard, "_read_log_lines", _boom)
    _clear_risk_status_cache()
    resp = client.get("/api/dashboard/risk-status")
    assert resp.status_code == 200
    assert resp.json()["feed_status"] == "offline"


# ─────────────────────────── liq_px passthrough ─────────────────────────

def _position(coin: str, szi: str, liq_px) -> dict:
    return {"position": {
        "coin": coin, "szi": szi, "entryPx": "100",
        "positionValue": "1000", "unrealizedPnl": "10",
        "marginUsed": "200", "leverage": {"value": "5"},
        **({"liquidationPx": liq_px} if liq_px is not None else {}),
    }}


def test_position_rows_carry_parsed_liq_px(monkeypatch):
    """liquidationPx comes through as a float; "0" (fully margined / tiny
    position) and a missing field map to None, never 0.0."""
    from hermes_trader import dashboard
    monkeypatch.setattr(dashboard.dsl_exit, "load_state", lambda force=False: None)
    monkeypatch.setattr(dashboard.dsl_exit, "tracker_view", lambda coin, side: None)
    state = {"asset_positions": [
        _position("BTC", "1.0", "93500.5"),
        _position("ETH", "2.0", "0"),        # fully margined → None
        _position("SOL", "-3.0", None),      # field absent → None
    ]}
    rows = dashboard._rows_from_state(state)
    by_coin = {r["coin"]: r for r in rows}
    assert by_coin["BTC"]["liq_px"] == pytest.approx(93500.5)
    assert by_coin["ETH"]["liq_px"] is None
    assert by_coin["SOL"]["liq_px"] is None
