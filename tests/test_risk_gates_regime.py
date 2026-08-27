"""P3-3: direct unit coverage for the two highest-incident risk paths.

1. ``market_regime_gate`` — the chop/counter-trend verdict logic, in
   particular the P1-4 fix: a LONE momentum burst in chop (classic wick
   fakeout) must NOT bypass the gate without a minimum composite score.
2. ``AgentMemory.track_daily_pnl`` / ``record_loss_outcome`` — UTC day-roll
   re-baselining, peak/give-back high-water mark, net-contribution
   invariance, and the per-coin consecutive-loss streak.

Both groups are pure unit tests: the regime detector / funding lookup are
monkeypatched, and AgentMemory is instantiated directly (never hydrated via
load()), so flush() no-ops and nothing touches disk.
"""

import time

from hermes_trader.agents import risk_gates
from hermes_trader.agents.memory import AgentMemory
from hermes_trader.agents.risk_gates import GateContext, market_regime_gate


# ── helpers ──────────────────────────────────────────────────────────────

def _ctx(**over) -> GateContext:
    """GateContext with neutral defaults; tests override what they need."""
    base = dict(
        confidence=0.3,
        current_positions=[],
        trade_notional_usd=1000.0,
        daily_pnl=0.0,
        market_volume_24h_usd=1_000_000.0,
        coin="BTC",
        trade_side="long",
        has_binary_news_risk=False,
        equity=10_000.0,
        total_open_notional=0.0,
    )
    base.update(over)
    return GateContext(**base)


def _patch_regime(monkeypatch, regime: str, trend_score: float = 0.0) -> None:
    """Free the regime detector and funding overlay to deterministic values."""
    monkeypatch.setattr(
        "hermes_trader.agents.market_regime.detect_regime_with_score",
        lambda coin: (regime, trend_score),
    )
    monkeypatch.setattr(risk_gates, "_funding_regime_for", lambda coin: "NEUTRAL")

    def _fake_cfg(key, default=None, *, config=None):
        return {
            "chop_min_conf": 0.7,
            "chop_min_score": 50.0,
            "chop_burst_min_score": 20.0,
        }.get(key, default)

    monkeypatch.setattr(risk_gates, "cfg_get", _fake_cfg)


# ── market_regime_gate: chop (P1-4 regression core) ──────────────────────

def test_chop_lone_burst_low_score_blocked(monkeypatch):
    # P1-4: momentum burst fired but composite_score 10 < chop_burst_min_score
    # 20 — a lone impulse in chop is a wick fakeout and must be blocked.
    _patch_regime(monkeypatch, "chop")
    ctx = _ctx(confidence=0.3, composite_score=10.0, momentum_burst_fired=True)
    r = market_regime_gate(ctx, counter_regime_min_conf=0.7)
    assert r["pass"] is False
    assert r["via"] == "chop_blocked"


def test_chop_burst_with_score_passes(monkeypatch):
    # Same burst but score 25 >= 20 → genuine impulse, bypass fires.
    _patch_regime(monkeypatch, "chop")
    ctx = _ctx(confidence=0.3, composite_score=25.0, momentum_burst_fired=True)
    r = market_regime_gate(ctx, counter_regime_min_conf=0.7)
    assert r["pass"] is True
    assert r["via"] == "trigger:momentum_burst"


def test_chop_bypass_flag_blocks_even_scored_burst(monkeypatch):
    # block_counter_trend_bypass=True disables the burst bypass entirely.
    _patch_regime(monkeypatch, "chop")
    ctx = _ctx(confidence=0.3, composite_score=25.0, momentum_burst_fired=True)
    r = market_regime_gate(ctx, counter_regime_min_conf=0.7,
                           block_counter_trend_bypass=True)
    assert r["pass"] is False
    assert r["via"] == "chop_blocked"


def test_chop_high_conviction_passes(monkeypatch):
    # Real conviction (conf >= chop_min_conf) clears chop without any trigger.
    _patch_regime(monkeypatch, "chop")
    ctx = _ctx(confidence=0.8, composite_score=0.0)
    r = market_regime_gate(ctx, counter_regime_min_conf=0.7)
    assert r["pass"] is True
    assert r["via"] == "chop_conviction"


def test_chop_slow_burn_alone_does_not_pass(monkeypatch):
    # A lone slow_burn/whale ping fires constantly in chop and must NOT clear
    # it (only momentum_burst is a chop bypass; slow_burn is not consulted).
    _patch_regime(monkeypatch, "chop")
    ctx = _ctx(confidence=0.3, composite_score=10.0, slow_burn_fired=True,
               whale_signal_fired=True)
    r = market_regime_gate(ctx, counter_regime_min_conf=0.7)
    assert r["pass"] is False
    assert r["via"] == "chop_blocked"


# ── market_regime_gate: aligned / neutral / counter-trend ────────────────

def test_aligned_with_trend_passes(monkeypatch):
    _patch_regime(monkeypatch, "up", trend_score=1.0)
    ctx = _ctx(trade_side="long", confidence=0.9)
    r = market_regime_gate(ctx)
    assert r["pass"] is True
    assert r["via"] == "aligned"


def test_neutral_regime_free_passes(monkeypatch):
    # Neutral tape free-passes even a low-conviction trade (no trend to fight).
    _patch_regime(monkeypatch, "neutral")
    ctx = _ctx(trade_side="long", confidence=0.1)
    r = market_regime_gate(ctx)
    assert r["pass"] is True
    assert r["via"] == "neutral"


def test_counter_trend_blocked_when_bypass_disabled(monkeypatch):
    # Up tape + short, low conf/score, no own-coin signal, bypass blocked.
    _patch_regime(monkeypatch, "up", trend_score=1.0)
    ctx = _ctx(trade_side="short", confidence=0.3, composite_score=10.0)
    r = market_regime_gate(ctx, counter_regime_min_conf=0.7,
                           block_counter_trend_bypass=True)
    assert r["pass"] is False
    assert r["via"] == "blocked_bypass"


def test_counter_trend_burst_passes_when_bypass_allowed(monkeypatch):
    # Symmetric case: with the bypass left at its default (False), a strong
    # own-coin momentum burst overrides the slow macro regime call.
    _patch_regime(monkeypatch, "up", trend_score=1.0)
    ctx = _ctx(trade_side="short", confidence=0.3, composite_score=10.0,
               momentum_burst_fired=True)
    r = market_regime_gate(ctx, counter_regime_min_conf=0.7,
                           block_counter_trend_bypass=False)
    assert r["pass"] is True
    assert r["via"] == "trigger:momentum_burst"


# ── AgentMemory.track_daily_pnl / record_loss_outcome ────────────────────

def _fresh_memory() -> AgentMemory:
    # Direct instantiation: _initialized stays False, so flush() is a safe
    # no-op and no .agent-memory.json is touched.
    return AgentMemory()


def test_daily_pnl_baseline_peak_and_giveback():
    m = _fresh_memory()
    m.track_daily_pnl(1000.0)          # establishes SOD baseline
    assert m._start_of_day_equity == 1000.0
    assert m._daily_pnl == 0.0

    m.track_daily_pnl(1050.0)          # +50
    assert m._daily_pnl == 50.0
    assert m.peak_daily_pnl() == 50.0

    m.track_daily_pnl(1030.0)          # gave back 20 — peak must hold
    assert m._daily_pnl == 30.0
    assert m.peak_daily_pnl() == 50.0


def test_net_contributions_excluded_from_pnl():
    # A $50 deposit must not look like trading profit: daily PnL is invariant
    # to net_contributions on both the baseline tick and subsequent ticks.
    m = _fresh_memory()
    m.track_daily_pnl(1000.0, net_contributions=50.0)
    assert m._start_of_day_equity == 950.0
    assert m._daily_pnl == 0.0

    m.track_daily_pnl(1000.0, net_contributions=50.0)
    assert m._daily_pnl == 0.0        # 1000 - 950 - 50

    m.track_daily_pnl(1060.0, net_contributions=50.0)
    assert m._daily_pnl == 60.0       # 1060 - 950 - 50


def test_day_roll_resets_baseline_peak_and_streak():
    m = _fresh_memory()
    m.track_daily_pnl(1000.0)
    m.record_loss_outcome("BTC", -1.0)
    m.record_loss_outcome("BTC", -0.5)
    assert m.consecutive_losses("BTC") == 2

    # Force the UTC day-roll condition: day-start timestamp in the past.
    m._day_start_ts = int(time.time()) - 2 * 86400
    m.track_daily_pnl(1100.0)

    assert m.consecutive_losses("BTC") == 0   # streak cleared at roll
    assert m._daily_pnl == 0.0                # re-baselined
    assert m.peak_daily_pnl() == 0.0
    assert m._start_of_day_equity == 1100.0


def test_consecutive_loss_streak_counts_and_resets_on_win():
    m = _fresh_memory()
    m.track_daily_pnl(1000.0)  # establish today's baseline (no roll mid-test)
    m.record_loss_outcome("ETH", -1.0)
    m.record_loss_outcome("ETH", -0.2)
    assert m.consecutive_losses("ETH") == 2
    assert m.consecutive_losses("DOGE") == 0  # untouched coin

    m.record_loss_outcome("ETH", 0.5)         # a win clears the streak
    assert m.consecutive_losses("ETH") == 0
