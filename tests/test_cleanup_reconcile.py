"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import json
import math
import pathlib
import sys

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


def _load_reconcile_module():
    import importlib.util
    import sys as _sys
    if "reconcile_le_under_test" not in _sys.modules:
        spec = importlib.util.spec_from_file_location(
            "reconcile_le_under_test",
            str(pathlib.Path(__file__).resolve().parents[1]
                / "scripts" / "reconcile_ta_late_entry_shadow.py"))
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["reconcile_le_under_test"] = mod
        spec.loader.exec_module(mod)
    return _sys.modules["reconcile_le_under_test"]


def _mk_candle(t, o, h, l, c, v):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _recon_candles(prices, bar_ms=4 * 3600_000, t0=0):
    return [_mk_candle(t0 + i * bar_ms, p, p + 0.1, p - 0.1, p, 1000.0)
            for i, p in enumerate(prices)]


def _recon_serve(cands):
    """Adapt hand-built candle objects to the raw candleSnapshot payload
    served by the stubbed _http_post (absolute open times preserved)."""
    rows = [{"t": int(c.t), "o": str(c.o), "h": str(c.h), "l": str(c.l),
             "c": str(c.c), "v": str(c.v)} for c in cands]
    return lambda path, payload, *a, **k: rows


def test_reconcile_score_hold_semantics():
    """hold_bars=2 exits at B1.close (2nd bar to close after the signal,
    ~8h); scores are side-aware, fee-netted, and MAE tracks adverse closes."""
    rc = _load_reconcile_module()
    # Signal fires while B0 (index 4) is forming; B1 is index 5.
    prices = [100, 101, 102, 103, 104, 105, 106, 107]
    cs = _recon_candles(prices)
    fee = rc.ROUND_TRIP_FEE_BPS / 10000.0
    exit_px, outcome, mae = rc._score("long", entry_px=104.0, b1_idx=5,
                                      candles=cs, hold_bars=2, fee_pct=fee)
    # hold_bars=2 -> exit_idx = 5 - 2 + 2 = 5 -> B1.close = 105
    assert exit_px == 105.0
    assert outcome == "win"  # +0.96% gross beats 0.05% fees
    # MAE: closes from B0 (104) to exit (105): 104 is flat, no adverse close.
    assert mae == 0.0
    # hold_bars=3 -> exit_idx=6 -> B2.close = 106.
    exit3, _, _ = rc._score("long", 104.0, 5, cs, 3, fee)
    assert exit3 == 106.0
    # Short: price rises against the position → loss + negative MAE.
    exit_s, outcome_s, mae_s = rc._score("short", entry_px=104.0, b1_idx=5,
                                         candles=cs, hold_bars=2, fee_pct=fee)
    assert exit_s == 105.0 and outcome_s == "loss"
    assert mae_s < 0.0  # adverse excursion for a short as price climbs


def test_reconcile_score_mae_tracks_adverse_dip():
    """An adverse dip between entry and exit is captured as negative MAE."""
    rc = _load_reconcile_module()
    # B0=104 (entry), then a dip to 101 on B1, recovery to 108 on B2.
    prices = [100, 100, 100, 100, 104, 101, 108, 109]
    cs = _recon_candles(prices)
    fee = rc.ROUND_TRIP_FEE_BPS / 10000.0
    _, _, mae = rc._score("long", entry_px=104.0, b1_idx=5,
                          candles=cs, hold_bars=3, fee_pct=fee)
    # worst close = 101 at B1: (101-104)/104 = -2.88%
    assert mae < -2.8


def test_reconcile_main_dry_run_scores_mature_vetoes(monkeypatch, tmp_path, capsys):
    """main(): mature blocked rows (both layers) get counterfactual outcomes
    in dry-run (no writeback); immature/non-blocked rows are skipped. Dry-run
    works on freshly-deserialized dicts, so scoring is asserted via the
    stdout summary, not the test's in-memory objects."""
    rc = _load_reconcile_module()
    from datetime import datetime, timedelta, timezone
    log = tmp_path / "shadow.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(hours=20)
           ).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        {"timestamp": old, "coin": "AAA", "side": "long", "blocked": True,
         "layer": "gate", "entry_px": 104.0, "trade_notional_usd": 100.0},
        {"timestamp": old, "coin": "AAA", "side": "long", "blocked": True,
         "layer": "prefilter"},  # no entry_px → B0.close fallback
        {"timestamp": recent, "coin": "AAA", "side": "long", "blocked": True,
         "layer": "gate", "entry_px": 104.0},  # too young → skipped
        {"timestamp": old, "coin": "AAA", "side": "long", "blocked": False,
         "layer": "gate"},  # not a veto → skipped
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    # Signal lands while the 4th bar (index 4, open=T0+16h) is forming;
    # candle at index 5 opens ~20h, after the 20h-old signal... build prices
    # so B0.close=104, B1.close=105.
    after_ts = datetime.fromisoformat(old.replace("Z", "+00:00"))
    sig_ms = int(after_ts.timestamp() * 1000)
    BAR = 4 * 3600_000
    bucket = sig_ms // BAR * BAR
    prices = [100, 101, 102, 103, 104, 105, 106, 107]
    candles = [_mk_candle(bucket - 4 * BAR + i * BAR, p, p + 0.1, p - 0.1, p, 1000.0)
               for i, p in enumerate(prices)]
    monkeypatch.setattr(rc, "_http_post", _recon_serve(candles))
    monkeypatch.setattr(sys, "argv",
                        ["reconcile", "--file", str(log), "--window-hours", "8"])
    assert rc.main() == 0
    reread = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    # Dry-run: file untouched → outcomes still None (only 2 of 4 rows mature).
    assert all(r.get("outcome") is None for r in reread)
    # Scoring is visible in the stdout summary: 2 mature vetoes (gate +
    # prefilter), hold_bars=2 exits at B1.close=105; the 1h-old veto and the
    # blocked:False row are excluded.
    out = capsys.readouterr().out
    assert "mature (>= 8h): 2" in out
    assert "2 mature vetoes (2x4h hold)" in out
    assert "layer=gate" in out and "layer=prefilter" in out
    assert "exit=105.0000" in out
    assert "dry-run" in out


def test_reconcile_main_write_persists_outcomes(monkeypatch, tmp_path):
    """--write flips outcome/exit_px/pnl_pct back into the JSONL; a veto
    younger than the maturity window is left pending."""
    rc = _load_reconcile_module()
    from datetime import datetime, timedelta, timezone
    log = tmp_path / "shadow.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(hours=20)
           ).strftime("%Y-%m-%dT%H:%M:%SZ")
    young = (datetime.now(timezone.utc) - timedelta(minutes=30)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [
        {"timestamp": old, "coin": "BBB", "side": "long", "blocked": True,
         "layer": "gate", "entry_px": 104.0},
        {"timestamp": young, "coin": "BBB", "side": "long", "blocked": True,
         "layer": "gate", "entry_px": 104.0},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    after_ts = datetime.fromisoformat(old.replace("Z", "+00:00"))
    sig_ms = int(after_ts.timestamp() * 1000)
    BAR = 4 * 3600_000
    bucket = sig_ms // BAR * BAR
    prices = [100, 101, 102, 103, 104, 105, 106, 107]
    candles = [_mk_candle(bucket - 4 * BAR + i * BAR, p, p + 0.1, p - 0.1, p, 1000.0)
               for i, p in enumerate(prices)]
    monkeypatch.setattr(rc, "_http_post", _recon_serve(candles))
    monkeypatch.setattr(sys, "argv",
                        ["reconcile", "--file", str(log), "--write"])
    assert rc.main() == 0
    out = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert out[0]["outcome"] in ("win", "loss")
    assert out[0]["exit_px"] == 105.0 and out[0]["hold_bars"] == 2
    assert out[1].get("outcome") is None  # immature record untouched
