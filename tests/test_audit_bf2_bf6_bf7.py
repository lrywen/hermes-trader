"""Deep-audit (2026-08-28) remediation tests: B-F2, B-F6, B-F7.

Three risk gates whose book-keeping already existed in AgentMemory but which
NOTHING read — they were defined on the risk-control sheet yet never wired:

B-F2: consecutive_loss_gate — block re-entry on a coin after N CONSECUTIVE
      losing closes (memory.record_loss_outcome tracked the streak; no gate
      consumed memory.consecutive_losses()).
B-F6: per_coin_daily_loss_gate — block a coin whose CUMULATIVE realized loss
      today reaches X% of start-of-day equity (memory.coin_daily_realized_
      pnl_pct() existed; no caller).
B-F7: drawdown_gate — block ALL new entries when equity falls more than Y%
      below its high-water mark (memory._peak_equity / the rolling equity
      trail are tracked and persisted as peakEquity / equityTrail; no
      multi-day drawdown control existed). Fixed 2026-09-03: the reference
      peak is a ROLLING window (drawdown_peak_window_days, default 14d) with
      a cooldown re-arm (drawdown_cooldown_hours, default 24h) and a
      one-shot legacy-memory rebase, so a permanently lower balance can no
      longer latch the gate forever; window_days=0 keeps the all-time peak.

Convention (shared with coin_circuit_breaker_gate / global_halt_gate): a
memory state-read failure fails OPEN (the independent breakers still apply);
a threshold <= 0 disables the gate; B-F7 additionally fails OPEN only until a
reference peak exists, then fails CLOSED once a real peak is recorded.
"""
import json
import time

# ── helpers ───────────────────────────────────────────────────────────────

def _ctx(**kw):
    from hermes_trader.agents.risk_gates import GateContext
    base = dict(confidence=0.9, current_positions=[], trade_notional_usd=50,
                daily_pnl=0, market_volume_24h_usd=1e8, coin="ETH",
                trade_side="long", has_binary_news_risk=False, equity=1000.0,
                total_open_notional=0)
    base.update(kw)
    return GateContext(**base)


def _isolated_memory(monkeypatch, tmp_path):
    """AgentMemory pointed at tmp paths (memory + lock + events), hydrated,
    and installed as the module-level singleton the gates import lazily."""
    import hermes_trader.event_log as event_log
    from hermes_trader.agents import memory as memory_mod
    mem_path = str(tmp_path / ".agent-memory.json")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", mem_path)
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", mem_path + ".lock")
    events_path = str(tmp_path / "events.jsonl")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", events_path)
    # record_close emits through event_log's own module-global path.
    monkeypatch.setattr(event_log, "EVENTS_FILE", events_path)
    m = memory_mod.AgentMemory()
    m.load()
    monkeypatch.setattr(memory_mod, "memory", m)
    return m, mem_path


def _boom(*_a, **_k):
    raise RuntimeError("simulated memory read failure")


# Audit 2026-09-03: drawdown_gate now measures against a ROLLING peak
# (circuit_breaker.drawdown_peak_window_days, default 14d) and re-arms after
# drawdown_cooldown_hours (default 24h) instead of latching to the all-time
# peak forever. The original B-F7 cases below encode the all-time-peak
# semantics; pin the window to 0 (rolling_peak_equity(0) falls back to the
# all-time peak — the legacy path) and cooldown to 0 (re-arm only on recovery,
# never on a wall-clock timer) so those cases keep exercising the original
# fail-closed HWM behavior. The new rolling/cooldown/legacy-rebase behavior is
# covered by dedicated cases further down.
def _patch_drawdown_cfg(monkeypatch, *, window_days, cooldown_hours):
    """Force the gate's two drawdown config knobs; every other key delegates
    to the real cfg_get so the rest of eval_all_gates is unaffected."""
    from hermes_trader.agents import risk_gates
    real_cfg_get = risk_gates.cfg_get
    forced = {
        "circuit_breaker.drawdown_peak_window_days": float(window_days),
        "circuit_breaker.drawdown_cooldown_hours": float(cooldown_hours),
    }

    def _cfg(key, default=None, *, config=None):
        if key in forced:
            return forced[key]
        return real_cfg_get(key, default, config=config)

    monkeypatch.setattr(risk_gates, "cfg_get", _cfg)


def _legacy_drawdown_cfg(monkeypatch):
    """All-time peak + no timer re-arm (the original pre-fix B-F7 semantics)."""
    _patch_drawdown_cfg(monkeypatch, window_days=0.0, cooldown_hours=0.0)


# ── B-F2: consecutive-loss gate ───────────────────────────────────────────

def test_bf2_blocks_at_the_limit(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import consecutive_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.record_loss_outcome("ETH", -1.0)
    m.record_loss_outcome("ETH", -0.5)
    m.record_loss_outcome("ETH", -2.0)  # streak = 3
    assert m.consecutive_losses("ETH") == 3
    r = consecutive_loss_gate(_ctx(coin="ETH"), limit=3)
    assert r["pass"] is False
    assert "consecutive-loss" in r["reason"] and "ETH" in r["reason"]


def test_bf2_passes_below_the_limit(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import consecutive_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.record_loss_outcome("ETH", -1.0)
    m.record_loss_outcome("ETH", -0.5)  # streak = 2 < limit 3
    assert consecutive_loss_gate(_ctx(coin="ETH"), limit=3)["pass"] is True


def test_bf2_streak_is_per_coin(monkeypatch, tmp_path):
    """A losing streak on SOL must not block ETH entries."""
    from hermes_trader.agents.risk_gates import consecutive_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    for _ in range(5):
        m.record_loss_outcome("SOL", -1.0)
    assert consecutive_loss_gate(_ctx(coin="ETH"), limit=3)["pass"] is True
    assert consecutive_loss_gate(_ctx(coin="SOL"), limit=3)["pass"] is False


def test_bf2_winning_close_resets_streak(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import consecutive_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.record_loss_outcome("ETH", -1.0)
    m.record_loss_outcome("ETH", -1.0)
    m.record_loss_outcome("ETH", -1.0)
    assert consecutive_loss_gate(_ctx(coin="ETH"), limit=3)["pass"] is False
    m.record_loss_outcome("ETH", 0.8)  # a winner clears the streak
    assert m.consecutive_losses("ETH") == 0
    assert consecutive_loss_gate(_ctx(coin="ETH"), limit=3)["pass"] is True


def test_bf2_day_roll_clears_streak(monkeypatch, tmp_path):
    """track_daily_pnl resets _consecutive_losses at the UTC day roll."""
    from hermes_trader.agents.risk_gates import consecutive_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    for _ in range(3):
        m.record_loss_outcome("ETH", -1.0)
    assert consecutive_loss_gate(_ctx(coin="ETH"), limit=3)["pass"] is False
    # Simulate a UTC roll: baseline from a prior day → track re-baselines and
    # clears the streak.
    m._day_start_ts = 0
    m.track_daily_pnl(1000.0)
    assert m.consecutive_losses("ETH") == 0
    assert consecutive_loss_gate(_ctx(coin="ETH"), limit=3)["pass"] is True


def test_bf2_disabled_when_limit_non_positive(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import consecutive_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    for _ in range(10):
        m.record_loss_outcome("ETH", -1.0)
    assert consecutive_loss_gate(_ctx(coin="ETH"), limit=0)["pass"] is True
    assert consecutive_loss_gate(_ctx(coin="ETH"), limit=-1)["pass"] is True


def test_bf2_read_failure_fails_open(monkeypatch, tmp_path):
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import consecutive_loss_gate
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_mod.memory, "consecutive_losses", _boom)
    assert consecutive_loss_gate(_ctx(coin="ETH"), limit=3)["pass"] is True


# ── B-F6: per-coin cumulative daily-loss gate ─────────────────────────────

def _record_close(m, coin, usd):
    """A realized close carrying realized_pnl_usd (closed_at = now)."""
    m.record_close({
        "coin": coin, "side": "long", "entry_px": 100.0, "exit_px": 99.0,
        "spot_pct": -1.0, "realized_pnl_pct": -1.0,
        "realized_pnl_usd": usd, "leverage": 2.0,
        "closed_at": int(time.time() * 1000),
    })


def test_bf6_blocks_when_cumulative_daily_loss_reaches_cap(monkeypatch, tmp_path):
    """Five -0.8%-of-SOD stops on the same name (-4.0%) trip a 3% cap even
    though NO single stop hit the per-trade 3% breaker."""
    from hermes_trader.agents.risk_gates import per_coin_daily_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.track_daily_pnl(1000.0)  # SOD baseline = $1000
    for _ in range(5):
        _record_close(m, "ETH", -8.0)  # -0.8% each → -4.0% cumulative
    pct = m.coin_daily_realized_pnl_pct("ETH", m.get_start_of_day_equity())
    assert -4.1 < pct < -3.9
    r = per_coin_daily_loss_gate(_ctx(coin="ETH"), max_loss_pct=3.0)
    assert r["pass"] is False
    assert "daily loss" in r["reason"] and "ETH" in r["reason"]


def test_bf6_passes_under_the_cap(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import per_coin_daily_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.track_daily_pnl(1000.0)
    _record_close(m, "ETH", -20.0)  # -2.0% < 3% cap
    assert per_coin_daily_loss_gate(_ctx(coin="ETH"), max_loss_pct=3.0)["pass"] is True


def test_bf6_profitable_day_passes(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import per_coin_daily_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.track_daily_pnl(1000.0)
    _record_close(m, "ETH", 50.0)  # +5% realized
    assert per_coin_daily_loss_gate(_ctx(coin="ETH"), max_loss_pct=3.0)["pass"] is True


def test_bf6_loss_is_per_coin(monkeypatch, tmp_path):
    """SOL's daily loss must not block ETH."""
    from hermes_trader.agents.risk_gates import per_coin_daily_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.track_daily_pnl(1000.0)
    for _ in range(5):
        _record_close(m, "SOL", -8.0)  # -4.0% on SOL
    assert per_coin_daily_loss_gate(_ctx(coin="ETH"), max_loss_pct=3.0)["pass"] is True
    assert per_coin_daily_loss_gate(_ctx(coin="SOL"), max_loss_pct=3.0)["pass"] is False


def test_bf6_no_baseline_equity_passes(monkeypatch, tmp_path):
    """Pre-first-tick: SOD equity is 0 → nothing to measure → pass."""
    from hermes_trader.agents.risk_gates import per_coin_daily_loss_gate
    _isolated_memory(monkeypatch, tmp_path)  # no track_daily_pnl call
    assert per_coin_daily_loss_gate(_ctx(coin="ETH"), max_loss_pct=3.0)["pass"] is True


def test_bf6_disabled_when_threshold_non_positive(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import per_coin_daily_loss_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.track_daily_pnl(1000.0)
    for _ in range(5):
        _record_close(m, "ETH", -8.0)
    assert per_coin_daily_loss_gate(_ctx(coin="ETH"), max_loss_pct=0.0)["pass"] is True


def test_bf6_read_failure_fails_open(monkeypatch, tmp_path):
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import per_coin_daily_loss_gate
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_mod.memory, "get_start_of_day_equity", _boom)
    assert per_coin_daily_loss_gate(_ctx(coin="ETH"), max_loss_pct=3.0)["pass"] is True


# ── B-F7: account-wide drawdown gate (+ peak tracking in memory) ──────────

def test_bf7_blocks_when_drawdown_exceeds_cap(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    _legacy_drawdown_cfg(monkeypatch)  # Audit 2026-09-03: all-time-peak semantics
    m.track_daily_pnl(1000.0)   # peak seeded at $1000
    m.track_daily_pnl(840.0)    # -16% from peak
    assert m.peak_equity() == 1000.0
    r = drawdown_gate(_ctx(equity=840.0), max_drawdown_pct=15.0)
    assert r["pass"] is False
    assert "drawdown" in r["reason"]


def test_bf7_passes_under_the_cap(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.track_daily_pnl(1000.0)
    m.track_daily_pnl(900.0)    # -10% < 15% cap
    assert drawdown_gate(_ctx(equity=900.0), max_drawdown_pct=15.0)["pass"] is True


def test_bf7_new_high_resets_the_baseline(monkeypatch, tmp_path):
    """A recovery to a new high-water mark relieves the halt (the gate measures
    peak→current, so at the peak drawdown is 0%)."""
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    _legacy_drawdown_cfg(monkeypatch)  # Audit 2026-09-03: all-time-peak semantics
    m.track_daily_pnl(1000.0)
    m.track_daily_pnl(800.0)    # -20% → would halt at 15%
    assert drawdown_gate(_ctx(equity=800.0), max_drawdown_pct=15.0)["pass"] is False
    # Recover in steps (>25% single-tick swings are rejected as partial-dex
    # blips by the implausible-read filter, exactly as the -50% crash path is).
    m.track_daily_pnl(900.0)
    m.track_daily_pnl(1100.0)   # new all-time high
    m.track_daily_pnl(1050.0)   # only -4.5% off the new peak
    assert m.peak_equity() == 1100.0
    assert drawdown_gate(_ctx(equity=1050.0), max_drawdown_pct=15.0)["pass"] is True


def test_bf7_peak_survives_day_roll(monkeypatch, tmp_path):
    """The HWM is ALL-TIME (does NOT reset at UTC roll) — a slow multi-day
    grind that trips no single-day limit must still trip drawdown."""
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    _legacy_drawdown_cfg(monkeypatch)  # Audit 2026-09-03: all-time-peak semantics
    m.track_daily_pnl(1000.0)   # day 1 peak
    m._day_start_ts = 0         # force UTC re-baseline on next tick
    m.track_daily_pnl(990.0)    # day 2 start
    m._day_start_ts = 0
    m.track_daily_pnl(850.0)    # day 3: -15% from the all-time peak
    assert m.peak_equity() == 1000.0
    assert drawdown_gate(_ctx(equity=850.0), max_drawdown_pct=15.0)["pass"] is False


def test_bf7_no_peak_yet_passes(monkeypatch, tmp_path):
    """Before the first accepted equity tick there is no reference peak →
    fail OPEN (nothing to measure)."""
    from hermes_trader.agents.risk_gates import drawdown_gate
    _isolated_memory(monkeypatch, tmp_path)
    assert drawdown_gate(_ctx(equity=1000.0), max_drawdown_pct=15.0)["pass"] is True


def test_bf7_zero_ctx_equity_passes(monkeypatch, tmp_path):
    """ctx.equity <= 0 (missing live equity in the proposal) → no measurement."""
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.track_daily_pnl(1000.0)
    assert drawdown_gate(_ctx(equity=0.0), max_drawdown_pct=15.0)["pass"] is True


def test_bf7_disabled_when_threshold_non_positive(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m.track_daily_pnl(1000.0)
    m.track_daily_pnl(100.0)    # -90%
    assert drawdown_gate(_ctx(equity=100.0), max_drawdown_pct=0.0)["pass"] is True
    assert drawdown_gate(_ctx(equity=100.0), max_drawdown_pct=-5.0)["pass"] is True


def test_bf7_read_failure_fails_open(monkeypatch, tmp_path):
    from hermes_trader.agents import memory as memory_mod
    from hermes_trader.agents.risk_gates import drawdown_gate
    _isolated_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(memory_mod.memory, "peak_equity", _boom)
    assert drawdown_gate(_ctx(equity=800.0), max_drawdown_pct=15.0)["pass"] is True


def test_bf7_peak_not_inflated_by_rejected_partial_dex_blip(monkeypatch, tmp_path):
    """The implausible-read filter runs BEFORE the peak update: a one-tick
    partial-dex spike (+50%) must not become the HWM (which would otherwise
    make every subsequent healthy reading look like a fake drawdown)."""
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    _legacy_drawdown_cfg(monkeypatch)  # Audit 2026-09-03: all-time-peak semantics
    m.track_daily_pnl(1000.0)
    m.track_daily_pnl(1500.0)   # +50% in one tick → rejected as implausible
    assert m.peak_equity() == 1000.0
    # and the rejected tick must not fake-trip drawdown either: equity $950 is
    # only -5% off the true $1000 peak
    from hermes_trader.agents.risk_gates import drawdown_gate
    assert drawdown_gate(_ctx(equity=950.0), max_drawdown_pct=15.0)["pass"] is True


def test_bf7_peak_equity_persists_across_restart(monkeypatch, tmp_path):
    """peakEquity is written to the snapshot and rehydrated by a fresh
    AgentMemory (durability: a restart must not forget the HWM)."""
    from hermes_trader.agents import memory as memory_mod
    m, path = _isolated_memory(monkeypatch, tmp_path)
    m.track_daily_pnl(1234.0)
    m.flush(force=True)
    with open(path) as f:
        assert json.load(f)["peakEquity"] == 1234.0
    m2 = memory_mod.AgentMemory()
    m2.load()
    assert m2.peak_equity() == 1234.0


# ── B-F7 rolling-peak recovery (fix Audit 2026-09-03) ───────────────────

def test_bf7_rolling_window_ages_out_old_peak(monkeypatch, tmp_path):
    """A peak OLDER than the rolling window no longer counts: a permanently
    lower balance stops looking like a fresh drawdown once the high ages out
    of the window (the production latched-forever bug)."""
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    now_s = time.time()
    # Seed the trail directly (production cadence is one sample ~per 600s, so
    # same-second ticks collapse; aged timestamps need explicit samples):
    # a 20d-old $1000 peak and a current $840 level (-16% from the old peak).
    m._equity = 840.0
    m._peak_equity = 1000.0  # all-time HWM stays $1000
    m._equity_trail.clear()
    m._equity_trail.append((now_s - 20 * 86400.0, 1000.0))
    m._equity_trail.append((now_s - 100.0, 840.0))
    # 14d window: the $1000 peak aged out → the in-window peak is $840 → 0%
    # drawdown → pass even though the all-time HWM is still $1000.
    _patch_drawdown_cfg(monkeypatch, window_days=14.0, cooldown_hours=24.0)
    assert drawdown_gate(_ctx(equity=840.0), max_drawdown_pct=15.0)["pass"] is True
    assert m.peak_equity() == 1000.0  # all-time HWM untouched
    # A 30d window still covers the old peak → the same state must block.
    _patch_drawdown_cfg(monkeypatch, window_days=30.0, cooldown_hours=24.0)
    assert drawdown_gate(_ctx(equity=840.0), max_drawdown_pct=15.0)["pass"] is False


def test_bf7_cooldown_rebases_after_frozen_window(monkeypatch, tmp_path):
    """A freeze held continuously for drawdown_cooldown_hours re-baselines the
    peak to current equity and lets entries resume — bounded recovery so a
    permanent equity drop can never latch the gate past the cooldown."""
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    # Audit 2026-09-03: seed the rolling trail directly with spaced,
    # timestamped samples. Same-second track_daily_pnl() ticks collapse into
    # one sample via the 600s min-spacing rule, which would hide the peak.
    now_s = time.time()
    m._peak_equity = 1000.0
    m._equity_trail.clear()
    m._equity_trail.append((now_s - 3600.0, 1000.0))  # in-window peak, >600s old
    m._equity_trail.append((now_s, 800.0))            # -20% → trips the 15% cap
    _patch_drawdown_cfg(monkeypatch, window_days=14.0, cooldown_hours=24.0)
    r = drawdown_gate(_ctx(equity=800.0), max_drawdown_pct=15.0)
    assert r["pass"] is False
    assert m._dd_frozen_since_ms > 0
    # Backdate the freeze stamp to just past the 24h cooldown.
    m._dd_frozen_since_ms = int(time.time() * 1000) - 25 * 3600 * 1000
    # Cooldown recovery: gate re-arms at $800 and passes.
    r = drawdown_gate(_ctx(equity=800.0), max_drawdown_pct=15.0)
    assert r["pass"] is True
    assert m.peak_equity() == 800.0       # baseline re-armed to current equity
    assert len(m._equity_trail) == 1      # trail reseeded at the new baseline
    assert m._dd_frozen_since_ms == 0     # freeze episode cleared
    # And a subsequent healthy evaluation stays open.
    assert drawdown_gate(_ctx(equity=810.0), max_drawdown_pct=15.0)["pass"] is True


def test_bf7_legacy_memory_oneshot_rebase(monkeypatch, tmp_path):
    """Pre-upgrade memory has an all-time peak but NO rolling trail (the
    persisted equityTrail did not exist). On the FIRST gate evaluation after
    the upgrade an over-threshold drawdown against that legacy peak is
    re-baselined once immediately, lifting the latched deadlock on deploy
    instead of waiting a full cooldown/window."""
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    # Simulate a legacy store: all-time HWM $1000, equity now $800 (-20%),
    # and an empty rolling trail (feature did not exist pre-upgrade).
    m._peak_equity = 1000.0
    m._equity = 800.0
    m._equity_trail.clear()
    assert len(m._equity_trail) == 0
    _patch_drawdown_cfg(monkeypatch, window_days=14.0, cooldown_hours=24.0)
    # First evaluation: the latched legacy drawdown is rebased once → pass.
    assert drawdown_gate(_ctx(equity=800.0), max_drawdown_pct=15.0)["pass"] is True
    assert m.peak_equity() == 800.0
    assert len(m._equity_trail) == 1  # reseeded at the rebased baseline
    # Once the rolling trail exists (post-upgrade steady state), the one-shot
    # legacy path is skipped even for an over-threshold drawdown: a genuine
    # fresh drawdown must still fail closed and must NOT be rebased.
    m2, _ = _isolated_memory(monkeypatch, tmp_path)
    m2._peak_equity = 1000.0
    m2._equity_trail.clear()
    m2._equity_trail.append((time.time() - 60.0, 1000.0))  # fresh in-window peak
    assert len(m2._equity_trail) == 1
    assert drawdown_gate(_ctx(equity=800.0), max_drawdown_pct=15.0)["pass"] is False
    assert m2.peak_equity() == 1000.0  # fresh drawdown fails closed, no rebase


# ── eval_all_gates wiring ────────────────────────────────────────────────

# A permissive config so ONLY the three new gates can block the proposal.
_WIRING_CONFIG = {
    "debate_gate": {"enabled": False},
    "max_trade_notional_usd": 0,
    "max_concurrent": 9999,
    "min_market_volume_usd": 0,
    "min_hip3_volume_usd": 0,
    "min_short_volume_usd": 0,
    "max_total_notional_pct": 100_000.0,
    "max_daily_loss_usd": -1_000_000_000,
    "min_ai_confidence": 0.0,
    "aligned_min_conf": None,
    "min_trend_score": 0.0,
    "coin_allowlist": [],
    "coin_blocklist": [],
    "max_crypto_long_correlated": 9999,
    "cooldown_min": 0,
    "daily_giveback_halt_pct": 0,
    "daily_giveback_min_peak_usd": 0,
    "counter_regime_min_conf": 0.0,
    "block_counter_trend_bypass": False,
    "crowded_with_min_conf": 0.0,
    "news_blackout": {"enabled": False},
    # The three new gates — wired through the circuit_breaker block:
    "circuit_breaker": {
        "consecutive_loss_limit": 3,
        "coin_daily_loss_pct": 5.0,
        "max_drawdown_pct": 15.0,
    },
}


def test_bf_wiring_new_gate_keys_present_and_pass_when_clean(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import eval_all_gates
    _isolated_memory(monkeypatch, tmp_path)  # fresh memory: no streaks/loss/peak
    report = eval_all_gates(_ctx(coin="ETH", equity=1000.0), dict(_WIRING_CONFIG))
    for key in ("consecutive_loss", "coin_daily_loss", "drawdown"):
        assert key in report["results"], f"{key} not wired into eval_all_gates"
        assert report["results"][key]["pass"] is True
    assert report["blocked"] is False, report["block_reasons"]


def test_bf_wiring_consecutive_loss_blocks_through_eval(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import eval_all_gates
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    for _ in range(3):
        m.record_loss_outcome("ETH", -1.0)
    report = eval_all_gates(_ctx(coin="ETH", equity=1000.0), dict(_WIRING_CONFIG))
    assert report["results"]["consecutive_loss"]["pass"] is False
    assert report["blocked"] is True
    assert any("consecutive-loss" in r for r in report["block_reasons"])


def test_bf_wiring_drawdown_blocks_through_eval(monkeypatch, tmp_path):
    from hermes_trader.agents.risk_gates import eval_all_gates
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    _legacy_drawdown_cfg(monkeypatch)  # Audit 2026-09-03: all-time-peak semantics
    m.track_daily_pnl(1000.0)
    m.track_daily_pnl(800.0)  # -20% > 15%
    report = eval_all_gates(_ctx(coin="ETH", equity=800.0), dict(_WIRING_CONFIG))
    assert report["results"]["drawdown"]["pass"] is False
    assert report["blocked"] is True
    assert any("drawdown" in r for r in report["block_reasons"])


def test_bf_wiring_thresholds_zero_disable_gates(monkeypatch, tmp_path):
    """circuit_breaker thresholds <= 0 disable the gates even when the state
    would otherwise block."""
    from hermes_trader.agents.risk_gates import eval_all_gates
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    for _ in range(5):
        m.record_loss_outcome("ETH", -1.0)
    m.track_daily_pnl(1000.0)
    m.track_daily_pnl(100.0)  # -90%
    cfg = dict(_WIRING_CONFIG)
    cfg["circuit_breaker"] = {
        "consecutive_loss_limit": 0,
        "coin_daily_loss_pct": 0.0,
        "max_drawdown_pct": 0.0,
    }
    report = eval_all_gates(_ctx(coin="ETH", equity=100.0), cfg)
    assert report["results"]["consecutive_loss"]["pass"] is True
    assert report["results"]["coin_daily_loss"]["pass"] is True
    assert report["results"]["drawdown"]["pass"] is True


def test_bf_wiring_canonical_defaults_are_sane():
    """The three thresholds must ship enabled with conservative defaults."""
    from hermes_trader.agents.config_store import cfg_get
    assert int(cfg_get("circuit_breaker.consecutive_loss_limit",
                       config={"circuit_breaker": {}})) == 3
    assert float(cfg_get("circuit_breaker.coin_daily_loss_pct",
                         config={"circuit_breaker": {}})) == 5.0
    assert float(cfg_get("circuit_breaker.max_drawdown_pct",
                         config={"circuit_breaker": {}})) == 15.0
