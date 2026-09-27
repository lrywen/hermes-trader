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


def test_dsl_max_loss_exit_populates_coin(monkeypatch, tmp_path):
    from hermes_trader.agents.executor import monitor_exits
    dsl_exit, _ = _isolate_dsl_state(monkeypatch, tmp_path)
    # H-5 (supplemental audit 2026-08-30): the hard stop now wick-guards — a
    # breach must persist hard_stop_confirm_sec (default 1.0s) before firing,
    # so a single synchronous tick no longer exits. Pin the window to 0 here
    # to exercise the immediate-stop verdict/coin plumbing the test asserts.
    _immediate_stop = dsl_exit.ExitPolicy(hard_stop_confirm_sec=0.0)
    dsl_exit.register_position("ETH", "long", 100.0, policy=_immediate_stop)
    # H-5 wick guard also asks the index (oracle) to confirm the stop. Feed a
    # test-world index consistent with the synthetic mid (no network) and stub
    # the same fetch for the monitor_exits path, which fetches it internally.
    _idx = {"ETH": 96.0}
    monkeypatch.setattr(dsl_exit, "get_index_prices", lambda coins: _idx)
    verdicts = dsl_exit.check_all_positions({"ETH": 96.0}, index_prices=_idx)  # 4% loss > cap
    assert len(verdicts) == 1 and verdicts[0].exit is True
    assert verdicts[0].coin == "ETH"          # field the cleanup added
    exits = monitor_exits({"ETH": 96.0})
    assert exits and exits[0]["coin"] == "ETH"


def test_dsl_no_exit_when_flat(monkeypatch, tmp_path):
    dsl_exit, _ = _isolate_dsl_state(monkeypatch, tmp_path)
    dsl_exit.register_position("SOL", "long", 100.0)
    assert dsl_exit.check_all_positions({"SOL": 100.5}) == []


def test_dsl_persistence_roundtrip(monkeypatch, tmp_path):
    """register_position writes state; load_state on a fresh registry restores it."""
    dsl_exit, state_file = _isolate_dsl_state(monkeypatch, tmp_path)

    t = dsl_exit.register_position("ETH", "long", 100.0)
    t.peak_px = 105.0
    t._last_floor = 102.5
    dsl_exit._save_state()
    assert state_file.exists()

    # Simulate a process restart.
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    dsl_exit.load_state()

    assert "ETH_long" in dsl_exit._active_positions
    restored = dsl_exit._active_positions["ETH_long"]
    assert restored.entry_px == 100.0
    assert restored.peak_px == 105.0
    assert restored._last_floor == 102.5
    dsl_exit._active_positions.clear()


def test_dsl_deregister_position(monkeypatch, tmp_path):
    dsl_exit, _ = _isolate_dsl_state(monkeypatch, tmp_path)
    dsl_exit.register_position("BTC", "long", 50_000.0)
    assert dsl_exit.deregister_position("BTC", "long") is True
    assert "BTC_long" not in dsl_exit._active_positions
    assert dsl_exit.deregister_position("BTC", "long") is False  # idempotent


def test_dsl_rehydrate_from_exchange(monkeypatch, tmp_path):
    """rehydrate synthesizes a tracker for an existing exchange position and drops
    trackers whose coin is no longer open."""
    dsl_exit, _ = _isolate_dsl_state(monkeypatch, tmp_path)

    # Pre-existing tracker for a coin that is NOT in the exchange position list
    # should be dropped.
    dsl_exit.register_position("OLD", "long", 1.0)

    asset_positions = [
        {"position": {"coin": "ETH", "szi": "0.5", "entryPx": "3000"}},
        {"position": {"coin": "SOL", "szi": "-10", "entryPx": "150"}},
        {"position": {"coin": "ZERO", "szi": "0", "entryPx": "1"}},  # ignored
    ]
    dsl_exit.rehydrate_from_exchange(asset_positions)

    keys = set(dsl_exit._active_positions)
    assert "ETH_long" in keys
    assert "SOL_short" in keys
    assert "OLD_long" not in keys
    assert "ZERO_long" not in keys and "ZERO_short" not in keys
    assert dsl_exit._active_positions["ETH_long"].entry_px == 3000.0
    assert dsl_exit._active_positions["SOL_short"].entry_px == 150.0
    dsl_exit._active_positions.clear()


def test_dsl_close_helper_deregisters(monkeypatch, tmp_path):
    """close_position_market deregisters the tracker on a successful close."""
    from hermes_trader.agents import dsl_exit, executor
    dsl_exit, _ = _isolate_dsl_state(monkeypatch, tmp_path)
    dsl_exit.register_position("ETH", "long", 100.0)

    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: {
        "asset_positions": [{"position": {"coin": "ETH", "szi": "0.5", "entryPx": "100"}}],
    })
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 99.0)
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda is_buy, size, mid_price, coin, **kw: {"ok": True, "order_id": "x1"})

    res = executor.close_position_market("ETH")
    assert res["ok"] is True
    assert res["side"] == "long"
    assert "ETH_long" not in dsl_exit._active_positions
