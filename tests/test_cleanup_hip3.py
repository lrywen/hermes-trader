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


def _isolate_dsl_state(monkeypatch, tmp_path):
    """Point DSL persistence at a tmp file and clear the in-memory + load latches."""
    from hermes_trader.agents import dsl_exit
    state_file = tmp_path / "dsl.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state_file))
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    return dsl_exit, state_file


def test_resolve_user_address(monkeypatch):
    from hermes_trader.client.hl_client import resolve_user_address
    monkeypatch.setenv("HYPERLIQUID_MASTER_ADDRESS", "0xMASTER")
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0xWALLET")
    assert resolve_user_address() == "0xMASTER"
    monkeypatch.delenv("HYPERLIQUID_MASTER_ADDRESS")
    assert resolve_user_address() == "0xWALLET"


def test_fetch_account_state_aggregates_hip3_dexes(monkeypatch):
    """include_hip3=True sums equity across main + per-dex clearinghouses,
    concatenates positions, and prefixes bare HIP-3 coins with the dex name."""
    from hermes_trader.client import hl_client

    def _fake_http_post(path, payload, **kwargs):
        kind = payload.get("type")
        if kind == "clearinghouseState" and "dex" not in payload:
            return {
                "marginSummary": {"accountValue": "1000", "totalNtlPos": "500", "totalMarginUsed": "100"},
                "withdrawable": "800",
                "assetPositions": [
                    {"position": {"coin": "BTC", "szi": "0.1", "entryPx": "60000"}},
                ],
            }
        if kind == "clearinghouseState" and payload.get("dex") == "xyz":
            return {
                "marginSummary": {"accountValue": "250", "totalNtlPos": "300"},
                # Bare coin name — code should prefix to "xyz:MU"
                "assetPositions": [
                    {"position": {"coin": "MU", "szi": "5", "entryPx": "100"}},
                ],
            }
        if kind == "clearinghouseState" and payload.get("dex") == "vntl":
            return {
                "marginSummary": {"accountValue": "50", "totalNtlPos": "0"},
                "assetPositions": [],
            }
        if kind == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "total": "10"}]}
        return None

    monkeypatch.setattr(hl_client, "_http_post", _fake_http_post)
    monkeypatch.setattr("hermes_trader.client.universe.list_hip3_dexes", lambda: ["xyz", "vntl"])
    # CANONICAL_DEFAULTS has hip3_dex_allowlist=["xyz"]; clear it so both
    # mocked dexes are aggregated (the test exercises cross-dex summation).
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config",
        lambda: {"hip3_dex_allowlist": [], "hip3_dex_blocklist": []})

    state = hl_client.fetch_account_state("0xUSER", include_hip3=True)
    # Aggregated equity = main 1000 + xyz 250 + vntl 50 = 1300
    assert state["equity"] == 1300.0
    # Aggregated notional = main 500 + xyz 300 + vntl 0 = 800
    assert state["total_ntl"] == 800.0
    # `available` = main free initial margin (equity 1000 - margin used 100);
    # stays main-only so executor sizing doesn't bleed in cross-dex idle USDC
    assert state["available"] == 900.0
    # Per-dex breakdown exposed for the dashboard
    assert state["dex_equity"] == {"": 1000.0, "xyz": 250.0, "vntl": 50.0}
    # Positions: main BTC + HIP-3 MU, with bare MU prefixed to xyz:MU
    coins = [p["position"]["coin"] for p in state["asset_positions"]]
    assert coins == ["BTC", "xyz:MU"]


def test_fetch_account_state_main_only_default(monkeypatch):
    """Default include_hip3=False keeps the behavior the executor relies on
    for trade sizing — equity must reflect only the main clearinghouse so
    free-margin calculations don't bleed in idle HIP-3 USDC."""
    from hermes_trader.client import hl_client

    def _fake_http_post(path, payload, **kwargs):
        if payload.get("type") == "clearinghouseState":
            assert "dex" not in payload  # must NOT query HIP-3 dexes
            return {
                "marginSummary": {"accountValue": "1000", "totalNtlPos": "500", "totalMarginUsed": "0"},
                "withdrawable": "1000",
                "assetPositions": [],
            }
        if payload.get("type") == "spotClearinghouseState":
            return {"balances": []}
        return None

    monkeypatch.setattr(hl_client, "_http_post", _fake_http_post)
    state = hl_client.fetch_account_state("0xUSER")
    assert state["equity"] == 1000.0
    assert state["available"] == 1000.0


def test_fetch_aggregate_contributions_classifies_send_events(monkeypatch):
    """`fetch_aggregate_contributions_since` distinguishes pool-boundary
    transfers (spot↔perp, spot↔HIP-3) from intra-pool transfers (main↔xyz),
    treating only the former as contributions to the aggregated equity."""
    from hermes_trader.client import hl_client

    USER = "0xUSER"
    events = [
        # spot → xyz: $30 into the pool
        {"delta": {"type": "send", "user": USER, "destination": USER,
                   "sourceDex": "spot", "destinationDex": "xyz",
                   "usdcValue": "30.0"}},
        # spot → main: $50 into the pool
        {"delta": {"type": "send", "user": USER, "destination": USER,
                   "sourceDex": "spot", "destinationDex": "",
                   "usdcValue": "50.0"}},
        # main → spot: $20 OUT of the pool
        {"delta": {"type": "send", "user": USER, "destination": USER,
                   "sourceDex": "", "destinationDex": "spot",
                   "usdcValue": "20.0"}},
        # main → xyz: $100 intra-pool — must be NEUTRAL
        {"delta": {"type": "send", "user": USER, "destination": USER,
                   "sourceDex": "", "destinationDex": "xyz",
                   "usdcValue": "100.0"}},
        # xyz → vntl: $40 intra-pool — must be NEUTRAL
        {"delta": {"type": "send", "user": USER, "destination": USER,
                   "sourceDex": "xyz", "destinationDex": "vntl",
                   "usdcValue": "40.0"}},
        # External deposit: $200 into pool
        {"delta": {"type": "deposit", "usdcValue": "200.0"}},
        # External withdrawal: $15 out
        {"delta": {"type": "withdraw", "usdcValue": "15.0"}},
    ]
    monkeypatch.setattr(hl_client, "_http_post", lambda path, payload: events)
    monkeypatch.setattr("hermes_trader.client.universe.list_hip3_dexes",
                        lambda: ["xyz", "vntl", "km"])

    # Net = 30 + 50 - 20 + 0 + 0 + 200 - 15 = 245
    net = hl_client.fetch_aggregate_contributions_since(USER, start_ms=1)
    assert net == 245.0


def test_fetch_aggregate_contributions_skips_when_no_user(monkeypatch):
    """Defensive zero-return when user is empty or start_ms is invalid —
    a missing wallet should never crash the heartbeat."""
    from hermes_trader.client import hl_client
    assert hl_client.fetch_aggregate_contributions_since("", start_ms=1) == 0.0
    assert hl_client.fetch_aggregate_contributions_since("0xUSER", start_ms=0) == 0.0


def test_track_daily_pnl_subtracts_contributions():
    """A $50 spot→perp transfer must not appear as $50 of trading profit."""
    import time

    from hermes_trader.agents.memory import AgentMemory
    m = AgentMemory()
    # Seed start-of-day so the function takes the "established baseline" branch.
    m._start_of_day_equity = 200.0
    m._day_start_ts = int(time.time()) + 1  # in future → won't reset baseline
    # Equity grew $60 since start-of-day, but $50 was a transfer in →
    # only $10 is real trading PnL.
    m.track_daily_pnl(current_equity=260.0, net_contributions=50.0)
    assert m.get_daily_pnl() == 10.0
    # Pure trading gain with no contributions still works.
    m.track_daily_pnl(current_equity=270.0, net_contributions=50.0)
    assert m.get_daily_pnl() == 20.0  # 270 - 200 - 50


def test_track_daily_pnl_day_roll_rebases_with_todays_contributions():
    """Regression (supplemental audit 2026-09-02): on the FIRST tick after a UTC
    midnight roll the baseline must re-seed using ONLY today's contributions.

    The phantom-$30-loss bug: a perp->spot transfer made YESTERDAY (yesterday's
    net_contributions=-30) was re-queried over a stale day_start window on the
    first tick today, seeding startOfDayEquity = equity - (-30) = equity+30,
    after which every tick read daily_pnl = equity - (equity+30) - 0 = -30 —
    falsely tripping the hard daily-loss kill switch. With the heartbeat fixed
    to query contributions from TODAY's boundary, a pre-SOD transfer returns 0
    today, so the roll re-baselines at the true equity and daily_pnl reads 0."""
    from datetime import datetime, timezone

    from hermes_trader.agents.memory import AgentMemory

    today_utc = int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    m = AgentMemory()
    # Yesterday's already-baseline book: $50.9 equity at yesterday's start.
    m._start_of_day_equity = 50.9
    m._day_start_ts = today_utc - 86400  # yesterday → day-roll triggers
    # Overnight a perp->spot $30 transfer happened; equity now 20.9. Today's
    # contribution window (post-fix) correctly excludes yesterday's move → 0.
    m.track_daily_pnl(current_equity=20.9, net_contributions=0.0)
    # Baseline reseeds at true equity; no phantom loss carried over.
    assert m._start_of_day_equity == 20.9
    assert m.get_daily_pnl() == 0.0
    # A later tick today with equity flat and no transfers still reads ~0,
    # proving the ghost -$30 never re-appears.
    m.track_daily_pnl(current_equity=20.9, net_contributions=0.0)
    assert m.get_daily_pnl() == 0.0


def test_rehydrate_preserves_trackers_for_unqueried_dexes(monkeypatch, tmp_path):
    """A timeout on the `xyz` HIP-3 dex used to drop every xyz tracker as
    stale and reset peak/floor/phase-2 state on the next cycle. Now the
    rehydrator scopes its stale check to dexes that were actually queried,
    so a transient HL outage leaves DSL state intact."""
    dsl_exit, _ = _isolate_dsl_state(monkeypatch, tmp_path)
    # Register 3 trackers: main BTC, xyz:MU (HIP-3), vntl:NVDA (HIP-3).
    dsl_exit.register_position("BTC", "long", 60000.0)
    dsl_exit.register_position("xyz:MU", "long", 920.0)
    dsl_exit.register_position("vntl:NVDA", "long", 500.0)
    # Bump phase-2 state on the xyz tracker, then persist to disk so it
    # survives the load_state() call inside rehydrate_from_exchange.
    t = dsl_exit._active_positions["xyz:MU_long"]
    t.peak_px = 950.0
    t._last_floor = 935.0
    dsl_exit._save_state()

    # Simulate one cycle where main returned BTC but xyz dex timed out.
    # queried_dexes excludes "xyz" → xyz:MU tracker must be preserved.
    # vntl was queried successfully and returned NVDA → that one stays too.
    positions = [
        {"position": {"coin": "BTC", "szi": "0.1", "entryPx": "60000"}},
        {"position": {"coin": "vntl:NVDA", "szi": "1", "entryPx": "500"}},
    ]
    dsl_exit.rehydrate_from_exchange(positions, queried_dexes={"", "vntl"})

    # xyz:MU was NOT in queried_dexes → preserved with phase-2 state intact.
    assert "xyz:MU_long" in dsl_exit._active_positions, "xyz tracker wrongly dropped"
    assert dsl_exit._active_positions["xyz:MU_long"].peak_px == 950.0
    assert dsl_exit._active_positions["xyz:MU_long"]._last_floor == 935.0

    # And the legacy behavior still works: pass queried_dexes=None and any
    # missing position gets dropped exactly like before.
    dsl_exit.rehydrate_from_exchange(positions, queried_dexes=None)
    assert "xyz:MU_long" not in dsl_exit._active_positions
