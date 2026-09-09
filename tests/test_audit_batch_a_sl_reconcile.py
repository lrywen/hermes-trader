"""Batch-A (P1-1 + P2-2) SL oid reconciliation regressions.

Covers the cancel+replace / lost-response family:

  * exchange.find_sl_trigger_in_open_orders — the four reconcile states
    (replacement / old_alive / absent / lookup_failed), SL-side classification
    using the real mainnet openOrders shape (trigger in `limitPx`, side "A"/"B",
    reduceOnly bool), size tolerance, and hint tie-break that never GATES.
  * exchange.find_open_order_by_cloid — found / confirmed-absent /
    lookup-failed / no-cloid, so backup-SL retries never blind-mint a new
    cloid (duplicate) and never double-arm.
  * executor.sync_exchange_sl — a modify response_unknown:
      - replacement  -> tracker re-claims the NEW oid AND the throttle stamp IS
                        written (next identical tighten is throttled);
      - old_alive    -> old oid kept, throttle NOT stamped (retry allowed);
      - absent       -> oid kept, suspect flagged for backfill, not re-armed;
      - lookup_failed-> old oid kept, throttle NOT stamped.
    Plus the original defect regression: a plain successful modify stamps the
    throttle, but a modify whose response is lost must NOT poison it.
  * dsl_exit.backfill_brackets_from_exchange — a suspect key drives held-oid
    reconciliation: replacement claimed, confirmed-loss nulls + alerts, and an
    ambiguous (None) snapshot defers rather than nulling.
"""
from __future__ import annotations

import pytest

from hermes_trader.client import exchange
from hermes_trader.agents import dsl_exit, executor


# ── fixtures / helpers ────────────────────────────────────────────────────

def _open(oid, coin, side, sz, px, *, reduce_only=True, cloid=None,
          px_key="limitPx"):
    """Build one real-shaped openOrders row (market tpsl: trigger in limitPx)."""
    row = {"coin": coin, "oid": int(oid), "side": side, "sz": str(sz),
           "reduceOnly": bool(reduce_only), px_key: str(px)}
    if cloid is not None:
        row["cloid"] = cloid
    return row


@pytest.fixture
def snap(monkeypatch):
    """Patch the openOrders snapshot with a settable list/None."""
    box = {"orders": []}
    monkeypatch.setattr(exchange, "_fetch_open_orders_snapshot",
                        lambda user=None: box["orders"])
    return box


@pytest.fixture
def mover_env(monkeypatch):
    """A registered long in Phase 2 with a resting SL, and all live gates
    stubbed so sync_exchange_sl reaches the modify call deterministically."""
    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()
    dsl_exit._held_oids_verified = False
    executor._sl_move_state.pop("TESTETH", None)

    policy = dsl_exit.ExitPolicy(
        max_loss_pct=2.5, protect_pct=1.5, retrace_threshold=0.30,
        phase2_tiers=[dsl_exit.RetraceTier(1.5, 0.30)],
        hard_timeout_minutes=1_000_000.0, stale_flat_timeout_minutes=0.0,
        breakeven_trigger_pct=0.0, atr_stop_enabled=False,
        consecutive_breaches_required=1, noise_band_enabled=False)
    tracker = dsl_exit.register_position(
        "TESTETH", "long", 100.0, policy=policy, leverage=1)
    dsl_exit.set_bracket("TESTETH", "long", sl_oid=12345, sl_px=97.0,
                         sl_size=1.0)

    # Deterministic gates.
    monkeypatch.setattr(executor, "_live_abs_szi", lambda coin: 1.0)
    monkeypatch.setattr(executor, "_resolve_min_order_usd", lambda: 10.0)
    monkeypatch.setattr(executor, "time", type("C", (), {"time": lambda s: 1000.0})())
    monkeypatch.setattr("hermes_trader.notify.send_text", lambda *a, **k: None)
    monkeypatch.setattr("hermes_trader.notify.send_card", lambda *a, **k: None)

    # cfg_get returns 0 for every key; the buffer/min-move guards fall back to
    # module defaults via `cfg_get(key, default)` — patch the handful that must
    # NOT be 0 with a small resolver.
    defaults = {
        "sl_buffer_bps": executor._SL_BUFFER_BPS,
        "sl_move.min_bps": executor._SL_MOVE_MIN_BPS,
        "sl_move.min_interval_sec": executor._SL_MOVE_MIN_INTERVAL_SEC,
        "sl_limit_band_pct": 0.0,
    }

    def _cfg(key, *a, **k):
        return defaults.get(key, 0)

    monkeypatch.setattr(executor, "cfg_get", _cfg)

    yield tracker

    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()
    dsl_exit._held_oids_verified = False
    executor._sl_move_state.pop("TESTETH", None)


def _drive_to_phase2(tracker, mark=102.0):
    """Run the DSL check so the floor ratchets into Phase 2, then return the
    mover's expected target SL px."""
    tracker.check(mark)
    floor = tracker._last_floor
    return floor * (1.0 - executor._SL_BUFFER_BPS / 10_000.0)


# ── find_sl_trigger_in_open_orders: four states ───────────────────────────

def test_find_sl_old_alive_when_old_oid_rests(snap):
    snap["orders"] = [_open(12345, "TESTETH", "A", 1.0, 101.3)]
    r = exchange.find_sl_trigger_in_open_orders(
        "TESTETH", is_long=True, entry_px=100.0, expect_size=1.0,
        old_oid=12345, hint_trigger_px=101.3)
    assert r["status"] == "old_alive"
    assert r["order"]["oid"] == 12345


def test_find_sl_replacement_after_cancel_replace(snap):
    # Old oid gone; a fresh SL-side reduce-only sell rests near the hint.
    snap["orders"] = [_open(900002, "TESTETH", "A", 1.0, 101.2986)]
    r = exchange.find_sl_trigger_in_open_orders(
        "TESTETH", is_long=True, entry_px=100.0, expect_size=1.0,
        old_oid=12345, hint_trigger_px=101.3, mark_px=102.0)
    assert r["status"] == "replacement"
    assert r["order"]["oid"] == 900002
    assert abs(r["order"]["trigger_px"] - 101.2986) < 1e-9
    assert r["n_candidates"] == 1


def test_find_sl_absent_when_no_sl_side_order(snap):
    # Only a TP-side (above entry for a long) reduce-only order rests.
    snap["orders"] = [_open(700001, "TESTETH", "A", 1.0, 110.0)]
    r = exchange.find_sl_trigger_in_open_orders(
        "TESTETH", is_long=True, entry_px=100.0, expect_size=1.0,
        old_oid=12345, hint_trigger_px=101.3)
    assert r["status"] == "absent"


def test_find_sl_lookup_failed_when_snapshot_none(snap):
    snap["orders"] = None
    r = exchange.find_sl_trigger_in_open_orders(
        "TESTETH", is_long=True, entry_px=100.0, expect_size=1.0,
        old_oid=12345)
    assert r["status"] == "lookup_failed"


def test_find_sl_size_mismatch_is_not_replacement(snap):
    # SL-side but a wildly different size — must not be claimed as ours.
    snap["orders"] = [_open(900003, "TESTETH", "A", 5.0, 101.3)]
    r = exchange.find_sl_trigger_in_open_orders(
        "TESTETH", is_long=True, entry_px=100.0, expect_size=1.0,
        old_oid=12345, hint_trigger_px=101.3, mark_px=102.0)
    assert r["status"] == "absent"


def test_find_sl_size_within_tolerance_claimed(snap):
    # 1.5% size drift (post-partial / rounding) within the 2% tol is claimed.
    snap["orders"] = [_open(900004, "TESTETH", "A", 1.015, 101.3)]
    r = exchange.find_sl_trigger_in_open_orders(
        "TESTETH", is_long=True, entry_px=100.0, expect_size=1.0,
        old_oid=12345, hint_trigger_px=101.3, mark_px=102.0)
    assert r["status"] == "replacement"


def test_find_sl_hint_only_tie_breaks_never_gates(snap):
    # Two SL candidates; the one FARTHER in px is still claimable, and the
    # closest to hint wins. Neither is excluded by the hint.
    snap["orders"] = [
        _open(900010, "TESTETH", "A", 1.0, 100.9),
        _open(900011, "TESTETH", "A", 1.0, 101.28),
    ]
    r = exchange.find_sl_trigger_in_open_orders(
        "TESTETH", is_long=True, entry_px=100.0, expect_size=1.0,
        old_oid=12345, hint_trigger_px=101.3, mark_px=102.0)
    assert r["status"] == "replacement"
    assert r["order"]["oid"] == 900011
    assert r["n_candidates"] == 2


def test_find_sl_short_trailing_crossed_entry_buy_side(snap):
    # A trailing SHORT stop ratchets DOWN and can cross below entry; against the
    # live mark the buy trigger ABOVE the mark is still the stop side.
    snap["orders"] = [_open(800001, "TESTETH", "B", 1.0, 98.7)]
    r = exchange.find_sl_trigger_in_open_orders(
        "TESTETH", is_long=False, entry_px=100.0, expect_size=1.0,
        old_oid=12345, hint_trigger_px=98.7, mark_px=98.0)
    assert r["status"] == "replacement"
    assert r["order"]["oid"] == 800001


def test_find_sl_accepts_triggerpx_field_too(snap):
    snap["orders"] = [_open(900020, "TESTETH", "A", 1.0, 101.3,
                            px_key="triggerPx")]
    r = exchange.find_sl_trigger_in_open_orders(
        "TESTETH", is_long=True, entry_px=100.0, expect_size=1.0,
        old_oid=12345, hint_trigger_px=101.3, mark_px=102.0)
    assert r["status"] == "replacement"
    assert r["order"]["trigger_px"] == 101.3


# ── find_open_order_by_cloid: backup SL claim states ──────────────────────

def test_cloid_found_claims_oid(snap):
    snap["orders"] = [_open(555001, "TESTETH", "A", 1.0, 97.0,
                            cloid="0xabc123")]
    r = exchange.find_open_order_by_cloid("TESTETH", "0xabc123")
    assert r["found"] is True and r["lookup_ok"] is True
    assert r["oid"] == 555001 and r["trigger_px"] == 97.0


def test_cloid_confirmed_absent_allows_new_cloid(snap):
    snap["orders"] = [_open(555001, "OTHER", "A", 1.0, 97.0,
                            cloid="0xabc123")]
    r = exchange.find_open_order_by_cloid("TESTETH", "0xabc123")
    assert r["found"] is False and r["lookup_ok"] is True


def test_cloid_lookup_failed_keeps_old_cloid(snap):
    snap["orders"] = None
    r = exchange.find_open_order_by_cloid("TESTETH", "0xabc123")
    assert r["found"] is False and r["lookup_ok"] is False


def test_cloid_empty_is_lookup_not_ok(snap):
    r = exchange.find_open_order_by_cloid("TESTETH", None)
    assert r["found"] is False and r["lookup_ok"] is False
    assert r.get("reason") == "no_cloid"


# ── sync_exchange_sl: response_unknown four branches + throttle hygiene ───

def test_mover_success_writes_throttle_and_rotates_oid(mover_env, monkeypatch):
    tracker = mover_env
    target = _drive_to_phase2(tracker)
    monkeypatch.setattr(executor, "modify_sl_trigger",
                        lambda **k: {"ok": True, "order_id": 900001,
                                     "status": "resting"})
    executor.sync_exchange_sl({"TESTETH": 102.0})
    assert tracker.sl_oid == 900001
    assert abs(tracker.sl_px - target) < 1e-9
    assert "TESTETH" in executor._sl_move_state


def test_mover_unknown_replacement_reclaims_and_stamps(mover_env, monkeypatch):
    tracker = mover_env
    target = _drive_to_phase2(tracker)
    monkeypatch.setattr(executor, "modify_sl_trigger",
                        lambda **k: {"ok": False,
                                     "error_code": "response_unknown",
                                     "old_oid": 12345})
    monkeypatch.setattr(
        executor, "find_sl_trigger_in_open_orders",
        lambda *a, **k: {"status": "replacement",
                         "order": {"oid": 900077, "trigger_px": target,
                                   "size": 1.0},
                         "n_candidates": 1})
    executor.sync_exchange_sl({"TESTETH": 102.0})
    assert tracker.sl_oid == 900077
    assert abs(tracker.sl_px - target) < 1e-9
    # Throttle IS stamped after a reconciled replacement.
    assert executor._sl_move_state.get("TESTETH") == (1000.0, pytest.approx(target))


def test_mover_unknown_old_alive_keeps_oid_no_throttle(mover_env, monkeypatch):
    tracker = mover_env
    _drive_to_phase2(tracker)
    monkeypatch.setattr(executor, "modify_sl_trigger",
                        lambda **k: {"ok": False,
                                     "error_code": "response_unknown",
                                     "old_oid": 12345})
    monkeypatch.setattr(
        executor, "find_sl_trigger_in_open_orders",
        lambda *a, **k: {"status": "old_alive",
                         "order": {"oid": 12345, "trigger_px": 97.0}})
    executor.sync_exchange_sl({"TESTETH": 102.0})
    assert tracker.sl_oid == 12345
    assert "TESTETH" not in executor._sl_move_state


def test_mover_unknown_absent_flags_suspect_no_rearm(mover_env, monkeypatch):
    tracker = mover_env
    _drive_to_phase2(tracker)
    monkeypatch.setattr(executor, "modify_sl_trigger",
                        lambda **k: {"ok": False,
                                     "error_code": "response_unknown",
                                     "old_oid": 12345})
    monkeypatch.setattr(
        executor, "find_sl_trigger_in_open_orders",
        lambda *a, **k: {"status": "absent"})
    executor.sync_exchange_sl({"TESTETH": 102.0})
    # Oid left untouched (not blindly re-armed from the mover)...
    assert tracker.sl_oid == 12345
    # ...throttle not poisoned...
    assert "TESTETH" not in executor._sl_move_state
    # ...and suspect flagged so the next backfill reconciles it.
    assert "TESTETH_long" in dsl_exit._suspect_sl_keys


def test_mover_unknown_lookup_failed_keeps_oid_no_throttle(mover_env, monkeypatch):
    tracker = mover_env
    _drive_to_phase2(tracker)
    monkeypatch.setattr(executor, "modify_sl_trigger",
                        lambda **k: {"ok": False,
                                     "error_code": "response_unknown",
                                     "old_oid": 12345})
    monkeypatch.setattr(
        executor, "find_sl_trigger_in_open_orders",
        lambda *a, **k: {"status": "lookup_failed"})
    executor.sync_exchange_sl({"TESTETH": 102.0})
    assert tracker.sl_oid == 12345
    assert "TESTETH" not in executor._sl_move_state
    assert "TESTETH_long" not in dsl_exit._suspect_sl_keys


# ── backfill: suspect-driven held-oid reconciliation ─────────────────────

def test_backfill_suspect_claims_replacement(monkeypatch):
    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()
    dsl_exit._held_oids_verified = False
    policy = dsl_exit.ExitPolicy(max_loss_pct=2.5, protect_pct=1.5)
    tracker = dsl_exit.register_position(
        "TESTETH", "long", 100.0, policy=policy, leverage=1)
    dsl_exit.set_bracket("TESTETH", "long", sl_oid=12345, sl_px=97.0,
                         sl_size=1.0)
    dsl_exit.mark_sl_oid_suspect("TESTETH", "long")
    # Old oid absent; a cancel+replace new SL rests.
    monkeypatch.setattr(
        dsl_exit, "_fetch_open_orders",
        lambda user: [_open(900099, "TESTETH", "A", 1.0, 101.3)])
    monkeypatch.setattr(dsl_exit, "_save_state", lambda: None)
    # Trailing SL ratcheted ABOVE entry; only the live-mark boundary classifies
    # 101.3 as a stop (entry-only would read it as a TP and fail to claim it).
    n = dsl_exit.backfill_brackets_from_exchange(
        "0xuser", marks={"TESTETH": 102.0})
    assert n >= 1
    assert tracker.sl_oid == 900099
    assert "TESTETH_long" not in dsl_exit._suspect_sl_keys
    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()


def test_backfill_suspect_confirmed_loss_nulls_and_alerts(monkeypatch):
    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()
    dsl_exit._held_oids_verified = False
    alerts = []
    monkeypatch.setattr("hermes_trader.notify.send_text",
                        lambda *a, **k: alerts.append(1))
    policy = dsl_exit.ExitPolicy(max_loss_pct=2.5, protect_pct=1.5)
    tracker = dsl_exit.register_position(
        "TESTETH", "long", 100.0, policy=policy, leverage=1)
    dsl_exit.set_bracket("TESTETH", "long", sl_oid=12345, sl_px=97.0,
                         sl_size=1.0)
    dsl_exit.mark_sl_oid_suspect("TESTETH", "long")
    # Authoritative EMPTY snapshot — genuinely no orders.
    monkeypatch.setattr(dsl_exit, "_fetch_open_orders", lambda user: [])
    monkeypatch.setattr(dsl_exit, "_save_state", lambda: None)
    n = dsl_exit.backfill_brackets_from_exchange("0xuser")
    assert n >= 1
    assert tracker.sl_oid is None
    assert alerts
    assert "TESTETH_long" not in dsl_exit._suspect_sl_keys
    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()


def test_backfill_ambiguous_snapshot_defers_without_nulling(monkeypatch):
    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()
    dsl_exit._held_oids_verified = False
    policy = dsl_exit.ExitPolicy(max_loss_pct=2.5, protect_pct=1.5)
    tracker = dsl_exit.register_position(
        "TESTETH", "long", 100.0, policy=policy, leverage=1)
    dsl_exit.set_bracket("TESTETH", "long", sl_oid=12345, sl_px=97.0,
                         sl_size=1.0)
    dsl_exit.mark_sl_oid_suspect("TESTETH", "long")
    # Lookup FAILED (None) — must defer and NEVER null.
    monkeypatch.setattr(dsl_exit, "_fetch_open_orders", lambda user: None)
    n = dsl_exit.backfill_brackets_from_exchange("0xuser")
    assert n == 0
    assert tracker.sl_oid == 12345
    # Still suspect so the next cycle retries.
    assert "TESTETH_long" in dsl_exit._suspect_sl_keys
    dsl_exit._active_positions.clear()
    dsl_exit._suspect_sl_keys.clear()
