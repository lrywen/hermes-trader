"""Regression: ADA/DOT 2026-09-08 incident batch (audit 2026-09-10).

Three distinct defects were reconstructed from the live evidence:

1. BRACKET EXCEPTION VOIDED THE EXECUTE DECISION
   A filled position's post-fill bracket (backup SL / TP scale-out) was
   placed with no exception boundary. A single raised error (the historical
   ``place_hl_trigger_order() got an unexpected keyword argument 'cloid'``
   TypeError) propagated out of ``_place_post_fill_brackets`` and therefore
   out of the whole execute path AFTER the fill was already registered. The
   trading loop caught it only at the per-coin boundary, so:
     * the exchange held a live position,
     * NO server-side SL/TP had been armed,
     * events.jsonl had NO execute row (the decision record was never
       written) — the fill was invisible in the authoritative feed.
   Fix: each bracket leg is contained independently; the execute result
   always returns executed=True for a real fill and carries ``sl_missing``
   / ``bracket_error`` so the failure is loud AND auditable.

2. error EVENTS NEVER FORKED INTO events.jsonl
   The loop writes failures to session-log; fork_from_session only mirrored a
   whitelist that omitted ``error``. The post-fill bracket failure therefore
   existed solely in session-log (and its rotated archives). Fix: ``error``
   joins _FORKABLE_EVENTS.

3. PEAK_PXLOSS ACROSS RESTART (ADA mfe 1.14% vs true 1.40%)
   A favorable peak advance never requested a state save ("peak rebuilds"),
   so a restart rehydrated a stale peak; once price had rolled over the peak
   could never rebuild and the MFE was permanently understated. Fix: a peak
   advance now requests the same coalesced (throttled) save as a floor move.
"""
from __future__ import annotations

import json
import types

import pytest

from hermes_trader.agents import dsl_exit, executor
from hermes_trader import event_log


# ── 1. bracket exceptions are contained and surfaced ────────────────────

def _bracket_config():
    return {
        "sl_atr_mult": 1.2,
        "sl_floor_pct": 1.2,
        "sl_ceiling_pct": 3.0,
        "tp_scale_fraction": 0.0,  # keep the TP leg inert; SL leg is the SUT
    }


def test_backup_sl_exception_is_contained_and_flagged(monkeypatch):
    """A raising backup-SL placer must not escape the bracket builder.

    It must instead yield sl_missing=True and a non-null bracket_error so the
    fill stays recorded as executed and the unprotected state is explicit.
    """
    def _boom(*a, **k):
        raise TypeError("place_hl_trigger_order() got an unexpected keyword argument 'cloid'")

    monkeypatch.setattr(executor, "_place_backup_sl", _boom)
    monkeypatch.setattr(executor, "_place_tp_scale_out", lambda *a, **k: None)
    # silence the risk alert side-channel
    monkeypatch.setattr(
        "hermes_trader.notify.send_text", lambda *a, **k: None, raising=False)

    out = executor._place_post_fill_brackets(
        config=_bracket_config(), coin="ADA", is_buy=True, trade_side="long",
        atr=0.003, entry_px=0.23, size_in_coin=130.0,
        stop_px=0.22, tp_px=0.24)

    assert out["sl_missing"] is True
    assert out["bracket_error"] is not None
    assert "backup_sl" in out["bracket_error"]
    assert "cloid" in out["bracket_error"]


def test_tp_exception_does_not_mask_sl_result(monkeypatch):
    """A TP-leg failure must not clobber a successfully armed SL."""
    monkeypatch.setattr(executor, "_place_backup_sl", lambda *a, **k: False)

    def _tp_boom(*a, **k):
        raise RuntimeError("tp venue 500")

    monkeypatch.setattr(executor, "_place_tp_scale_out", _tp_boom)

    out = executor._place_post_fill_brackets(
        config=_bracket_config(), coin="DOT", is_buy=True, trade_side="long",
        atr=0.02, entry_px=1.17, size_in_coin=25.0,
        stop_px=1.14, tp_px=1.21)

    assert out["sl_missing"] is False
    assert out["bracket_error"] is not None
    assert "tp_scale" in out["bracket_error"]


def test_bracket_result_carries_error_key_by_default(monkeypatch):
    """Happy path still exposes the key (None) so callers can read it blindly."""
    monkeypatch.setattr(executor, "_place_backup_sl", lambda *a, **k: False)
    monkeypatch.setattr(executor, "_place_tp_scale_out", lambda *a, **k: None)

    out = executor._place_post_fill_brackets(
        config=_bracket_config(), coin="BTC", is_buy=True, trade_side="long",
        atr=100.0, entry_px=60000.0, size_in_coin=0.001,
        stop_px=59000.0, tp_px=61000.0)

    assert out["sl_missing"] is False
    assert out["bracket_error"] is None


# ── 2. error events fork into the authoritative feed ─────────────────────

def test_error_event_is_forkable():
    assert "error" in event_log._FORKABLE_EVENTS


def test_fork_from_session_persists_error(tmp_path, monkeypatch):
    ev_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(event_log, "EVENTS_FILE", str(ev_file))
    # anchor/lock sidecars resolve next to the events file automatically.
    ok = event_log.fork_from_session({
        "event": "error",
        "ts": 1788881480807,
        "coin": "ADA",
        "error": "TypeError: place_hl_trigger_order() got an unexpected "
                 "keyword argument 'cloid'",
    })
    assert ok is True
    lines = ev_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "error"
    assert rec["timestamp"] == "2026-09-08T15:31:20Z"
    assert rec["payload"]["coin"] == "ADA"
    assert "cloid" in rec["payload"]["error"]


# ── 7 (P0 regression): cloid contract across executor → exchange ────────

def test_backup_sl_cloid_accepted_by_trigger_signature(monkeypatch):
    """The exact 2026-09-08 TypeError must be structurally impossible.

    executor._place_backup_sl calls place_hl_trigger_order(cloid=...); the
    client signature must accept that kwarg end-to-end. A drift where the
    client dropped the parameter used to raise TypeError and voided the whole
    post-fill bracket (ADA/DOT). Assert the client signature accepts cloid and
    the executor actually forwards one.
    """
    import inspect

    from hermes_trader.client import exchange

    sig = inspect.signature(exchange.place_hl_trigger_order)
    assert "cloid" in sig.parameters, "client must accept cloid kwarg"

    captured = {}

    def _fake_trigger(is_long_position, size, trigger_px, kind,
                      coin="BTC", limit_band_pct=None, cloid=None):
        captured["cloid"] = cloid
        captured["kind"] = kind
        return {"ok": True, "order_id": 999}

    monkeypatch.setattr(executor, "place_hl_trigger_order", _fake_trigger)
    monkeypatch.setattr(executor, "set_bracket", lambda *a, **k: None)

    class _Mem:
        def avg_exit_slip_bps(self, coin, days=30.0):
            return 0.0

    missing = executor._place_backup_sl(
        atr=0.003, entry_px=0.23, sl_atr_mult=1.2, sl_floor_pct=1.2,
        sl_ceiling_pct=3.0, size_in_coin=130.0, is_buy=True, coin="ADA",
        trade_side="long", memory=_Mem(), sl_limit_band_pct=0.0)

    assert missing is False
    assert captured["cloid"] is not None
    assert captured["kind"] == "sl"


# ── 3. peak advance survives a restart (coalesced save) ──────────────────

def _isolate_dsl(monkeypatch, tmp_path):
    state_file = tmp_path / "dsl.json"
    monkeypatch.setattr(dsl_exit, "DSL_STATE_FILE", str(state_file))
    monkeypatch.setattr(dsl_exit, "DSL_STATE_LOCK_FILE", str(state_file) + ".lock")
    # no cross-test throttle bleed
    monkeypatch.setattr(dsl_exit, "_LAST_SAVE_TS", 0.0)
    monkeypatch.setattr(dsl_exit, "_SAVE_DIRTY", False)
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    return str(state_file)


def test_peak_advance_is_persisted_before_restart(monkeypatch, tmp_path):
    """The exact ADA sequence: peak to 1.40%, then a fresh process rehydrates.

    Post-fix the favorable peak is on disk (throttled save), so rehydrate in a
    second registry sees the true peak and the exit MFE is not understated.
    """
    state_file = _isolate_dsl(monkeypatch, tmp_path)

    pol = dsl_exit.ExitPolicy()
    t = dsl_exit.register_position("ADA", "long", entry_px=0.22882, policy=pol)
    # favorable tick: +1.40% — no exit, just a peak advance
    v = t.check(0.232025)
    assert v.exit is False
    assert t.peak_px == pytest.approx(0.232025)
    # force any coalesced (throttled) write to land deterministically
    dsl_exit._save_state()

    # Simulate process restart: new registry, rehydrate from disk.
    dsl_exit._active_positions.clear()
    dsl_exit._loaded_from_disk = False
    dsl_exit.load_state()

    reborn = dsl_exit._active_positions["ADA_long"]
    assert reborn.peak_px == pytest.approx(0.232025), \
        "favorable peak must survive restart (was: stale lower peak)"
    assert reborn._peak_profit_pct() == pytest.approx(1.400669, abs=1e-3)


def test_peak_advance_requests_coalesced_save_not_force(monkeypatch, tmp_path):
    """Peak saves must go through the throttle (force=False), never a force write."""
    _isolate_dsl(monkeypatch, tmp_path)
    calls = []

    def _fake_request_save(force=False):
        calls.append(force)

    monkeypatch.setattr(dsl_exit, "_request_save", _fake_request_save)
    t = dsl_exit.register_position("ZEC", "long", entry_px=1252.4,
                                   policy=dsl_exit.ExitPolicy())
    # favorable advance: the tick-opening deferred flush (False) plus exactly
    # ONE additional request attributable to the peak advance, also throttled.
    calls.clear()
    t.check(1260.0)
    assert calls.count(False) >= 1 and not any(calls), calls
    peak_requests = calls.count(False)

    # a tick that changes neither peak nor floor adds no peak-save request
    # (only the same opening deferred-flush request at most)
    calls.clear()
    t.check(1259.0)
    assert calls.count(False) <= peak_requests
    assert not any(calls)
