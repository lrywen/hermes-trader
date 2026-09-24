"""Late-chase gate + move-state machine tests (2026-09-22, BCH postmortem).

Covers the BCH failure: a strong directional move (270→321, +19% in 43 min)
missed at its origin must not be re-entered at the terminal tick merely
because its score rose with price. The move-state machine anchors a move on
the first fresh impulse and locks entry once the price runs beyond the fresh
band; late_chase_gate enforces that and adds a terminal 1h-RSI blowoff check.
"""


from hermes_trader.agents import move_state
from hermes_trader.agents.risk_gates import late_chase_gate
from hermes_trader.models.types import GateContext


def _ctx(**over) -> GateContext:
    base = dict(
        confidence=0.8, current_positions=[], trade_notional_usd=10, daily_pnl=0,
        market_volume_24h_usd=1e8, coin="BCH", trade_side="long",
        has_binary_news_risk=False, equity=20, total_open_notional=0,
        entry_px=321.0, composite_score=60)
    base.update(over)
    return GateContext(**base)


def _path(tmp_path) -> str:
    return str(tmp_path / "move-state.json")


# ── move_state machine ──────────────────────────────────────────────────────

def test_move_anchors_on_first_impulse_and_allows_within_band(tmp_path):
    p = _path(tmp_path)
    move_state.observe(coin="BCH", mid=270.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    allowed, _ = move_state.entry_allowed(
        coin="BCH", side="long", mid=285.0,
        fresh_max_extension_pct=8.0, path=p)
    assert allowed is True


def test_move_locks_beyond_band(tmp_path):
    p = _path(tmp_path)
    # Anchor at 270, then price runs to 321 (+19%).
    move_state.observe(coin="BCH", mid=270.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    move_state.observe(coin="BCH", mid=321.0, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    allowed, why = move_state.entry_allowed(
        coin="BCH", side="long", mid=321.0,
        fresh_max_extension_pct=8.0, path=p)
    assert allowed is False
    assert "missed move" in why


def test_pullback_reanchors_and_allows(tmp_path):
    p = _path(tmp_path)
    move_state.observe(coin="BCH", mid=270.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    move_state.observe(coin="BCH", mid=321.0, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    # Price retraces >50% of the extension (toward ~290).
    move_state.observe(coin="BCH", mid=290.0, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    allowed, _ = move_state.entry_allowed(
        coin="BCH", side="long", mid=290.0,
        fresh_max_extension_pct=8.0, path=p)
    assert allowed is True


def test_no_move_record_allows_calm_coin(tmp_path):
    p = _path(tmp_path)
    allowed, _ = move_state.entry_allowed(
        coin="BCH", side="long", mid=100.0,
        fresh_max_extension_pct=8.0, path=p)
    assert allowed is True


def test_counter_direction_always_allowed(tmp_path):
    p = _path(tmp_path)
    move_state.observe(coin="BCH", mid=270.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    move_state.observe(coin="BCH", mid=321.0, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    # A short against an over-extended up move is not "joining" it.
    allowed, _ = move_state.entry_allowed(
        coin="BCH", side="short", mid=321.0,
        fresh_max_extension_pct=8.0, path=p)
    assert allowed is True


# ── late_chase_gate ─────────────────────────────────────────────────────────

def test_gate_blocks_missed_move(tmp_path, monkeypatch):
    p = _path(tmp_path)
    monkeypatch.setattr(move_state, "STATE_FILE", p)
    move_state.observe(coin="BCH", mid=270.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    move_state.observe(coin="BCH", mid=321.0, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    cfg = {"late_chase": {
        "enabled": True, "fresh_move_band_pct": 8.0,
        "rsi1h_overbought": 999.0, "rsi1h_oversold": 15.0}}
    r = late_chase_gate(_ctx(entry_px=321.0), cfg)
    assert r["pass"] is False
    assert r["via"] == "late_chase_missed_move"


def test_gate_blocks_terminal_rsi(monkeypatch):
    # No move lock; supply a terminal 1h RSI via the internal helper.
    import hermes_trader.agents.risk_gates as rg
    monkeypatch.setattr(rg, "_latest_closed_rsi", lambda coin, interval: 87.0)
    cfg = {"late_chase": {
        "enabled": True, "fresh_move_band_pct": 8.0,
        "rsi1h_overbought": 85.0, "rsi1h_oversold": 15.0}}
    r = late_chase_gate(_ctx(entry_px=321.0), cfg)
    assert r["pass"] is False
    assert "blowoff" in r["reason"]


def test_gate_passes_fresh_entry(monkeypatch):
    import hermes_trader.agents.risk_gates as rg
    monkeypatch.setattr(rg, "_latest_closed_rsi", lambda coin, interval: 60.0)
    cfg = {"late_chase": {
        "enabled": True, "fresh_move_band_pct": 8.0,
        "rsi1h_overbought": 85.0, "rsi1h_oversold": 15.0}}
    # No move-state file for this coin → move leg allows; RSI 60 not terminal.
    r = late_chase_gate(_ctx(coin="FRESHCOIN", entry_px=10.0), cfg)
    assert r["pass"] is True


def test_gate_disabled_passes():
    r = late_chase_gate(_ctx(), {"late_chase": {"enabled": False}})
    assert r["pass"] is True


# ── P2: anchor re-anchor guards ─────────────────────────────────────────────

def test_young_anchor_not_flipped_by_opposite_impulse(tmp_path):
    p = _path(tmp_path)
    move_state.observe(coin="UNI", mid=100.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    # Immediately (well within min_anchor_age_sec) an opposite impulse arrives.
    move_state.observe(coin="UNI", mid=101.0, impulse=True, impulse_dir="down",
                       fresh_max_extension_pct=8.0,
                       min_anchor_age_sec=300.0, path=p)
    rec = move_state.get_move("UNI", path=p)
    # Anchor memory preserved: still the original up move at 100.
    assert rec["dir"] == "up"
    assert rec["anchor"] == 100.0


def test_old_anchor_flipped_by_opposite_impulse(tmp_path):
    p = _path(tmp_path)
    move_state.observe(coin="UNI", mid=100.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    # Age the anchor beyond the minimum, then a genuine opposite impulse.
    move_state.observe(coin="UNI", mid=101.0, impulse=True, impulse_dir="down",
                       fresh_max_extension_pct=8.0,
                       min_anchor_age_sec=0.0, path=p)
    rec = move_state.get_move("UNI", path=p)
    assert rec["dir"] == "down"
    assert rec["anchor"] == 101.0


def test_shallow_move_does_not_reset_lock(tmp_path):
    p = _path(tmp_path)
    move_state.observe(coin="MON", mid=100.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    # Move only reaches +9% (missed), then a shallow wiggle — not a 50% retrace
    # of a >=3% ... here extension is large enough; verify the 50% rule still holds.
    move_state.observe(coin="MON", mid=109.0, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    move_state.observe(coin="MON", mid=108.0, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=8.0,
                       min_move_extension_pct_for_reset=3.0, path=p)
    allowed, _ = move_state.entry_allowed(
        coin="MON", side="long", mid=108.0,
        fresh_max_extension_pct=8.0, path=p)
    assert allowed is False


def test_underdeveloped_move_cannot_reset(tmp_path):
    p = _path(tmp_path)
    move_state.observe(coin="X", mid=100.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    # Price pokes just past band (+9) → missed; extreme recorded 109.
    move_state.observe(coin="X", mid=109.0, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=8.0, path=p)
    # Force the recorded max_ext under the reset threshold by retracing is not
    # possible here; instead verify a missed move whose max_ext < threshold
    # never resets even on full retrace (use a tight band so missed at small ext).
    p2 = _path(tmp_path) + "b"
    move_state.observe(coin="Y", mid=100.0, impulse=True, impulse_dir="up",
                       fresh_max_extension_pct=2.0, path=p2)
    move_state.observe(coin="Y", mid=102.5, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=2.0, path=p2)
    # Retrace fully back to anchor (ext 0) but max_ext 2.5 < threshold 3.
    move_state.observe(coin="Y", mid=100.0, impulse=False, impulse_dir="up",
                       fresh_max_extension_pct=2.0,
                       min_move_extension_pct_for_reset=3.0, path=p2)
    rec = move_state.get_move("Y", path=p2)
    assert rec["missed"] is True


# ── P1 Leg3: real-time forming-bar blowoff ──────────────────────────────────

def test_gate_blocks_realtime_blowoff_long(monkeypatch, tmp_path):
    import hermes_trader.agents.move_state as move_state
    import hermes_trader.agents.risk_gates as rg
    # Pin an isolated, empty move-state so Leg1 cannot lock (the process-wide
    # real state file may contain a live missed-move for the same coin).
    monkeypatch.setattr(move_state, "STATE_FILE", str(tmp_path / ".move.json"))
    # No move-state lock; closed RSI mild; but real-time read is a blowoff.
    monkeypatch.setattr(rg, "_latest_closed_rsi", lambda coin, interval: 59.0)
    monkeypatch.setattr(
        rg, "_realtime_terminal",
        lambda coin, interval, mid: (83.0, 3.6))
    cfg = {"late_chase": {
        "enabled": True, "fresh_move_band_pct": 8.0,
        "rsi1h_overbought": 999.0, "rsi1h_oversold": 1.0,
        "realtime": {"enabled": True, "interval": "5m",
                     "rsi_overbought": 80.0, "rsi_oversold": 20.0,
                     "max_extension_atr": 3.0}}}
    r = late_chase_gate(_ctx(coin="UNI", entry_px=9.92), cfg)
    assert r["pass"] is False
    assert r["via"] == "late_chase_realtime_blowoff"


def test_gate_passes_realtime_healthy(monkeypatch, tmp_path):
    import hermes_trader.agents.move_state as move_state
    import hermes_trader.agents.risk_gates as rg
    monkeypatch.setattr(move_state, "STATE_FILE", str(tmp_path / ".move.json"))
    monkeypatch.setattr(rg, "_latest_closed_rsi", lambda coin, interval: 55.0)
    monkeypatch.setattr(
        rg, "_realtime_terminal",
        lambda coin, interval, mid: (62.0, 1.2))
    cfg = {"late_chase": {
        "enabled": True, "fresh_move_band_pct": 8.0,
        "rsi1h_overbought": 999.0, "rsi1h_oversold": 1.0,
        "realtime": {"enabled": True, "interval": "5m",
                     "rsi_overbought": 80.0, "rsi_oversold": 20.0,
                     "max_extension_atr": 3.0}}}
    r = late_chase_gate(_ctx(coin="ENA", entry_px=10.0), cfg)
    assert r["pass"] is True


def test_gate_blocks_realtime_blowoff_short(monkeypatch, tmp_path):
    import hermes_trader.agents.move_state as move_state
    import hermes_trader.agents.risk_gates as rg
    monkeypatch.setattr(move_state, "STATE_FILE", str(tmp_path / ".move.json"))
    monkeypatch.setattr(rg, "_latest_closed_rsi", lambda coin, interval: 40.0)
    monkeypatch.setattr(
        rg, "_realtime_terminal",
        lambda coin, interval, mid: (16.0, -3.5))
    cfg = {"late_chase": {
        "enabled": True, "fresh_move_band_pct": 8.0,
        "rsi1h_overbought": 1.0, "rsi1h_oversold": 0.0,
        "realtime": {"enabled": True, "interval": "5m",
                     "rsi_overbought": 80.0, "rsi_oversold": 20.0,
                     "max_extension_atr": 3.0}}}
    r = late_chase_gate(_ctx(coin="PUMP", trade_side="short", entry_px=10.0), cfg)
    assert r["pass"] is False
    assert r["via"] == "late_chase_realtime_blowoff"
