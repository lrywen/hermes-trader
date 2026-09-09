"""CS-D (2026-09-08) tests: cash-flow-invariant drawdown basis.

The drawdown gate's peak/trail used to record RAW equity, which conflates
trading PnL with EXTERNAL cash flow (spot↔perp transfers, deposits,
withdrawals). Production evidence: a perp→spot transfer on 2026-09-01 read as
a −58.9% crash against an inflated peak and latched the gate for 111
consecutive blocks with ZERO trading loss. CS-D stores each trail sample as
(ts, raw_equity, cum_flow) and re-bases every sample onto the CURRENT
cumulative-flow basis before taking the window max, so:

  * a pure transfer slides peak and equity together → dd% unchanged (~0);
  * a genuine trading loss still produces the real dollar drawdown gap;
  * pre-CS-D bare (ts, equity) rows are migrated exactly once (persisted flag).

These tests pin both the memory-level rebase maths and the gate-level
decision (pass on transfer, fail-closed on a real loss).
"""
import json
import time

# ── helpers (local copies; keep this file self-contained) ────────────────

def _ctx(**kw):
    from hermes_trader.agents.risk_gates import GateContext
    base = dict(confidence=0.9, current_positions=[], trade_notional_usd=50,
                daily_pnl=0, market_volume_24h_usd=1e8, coin="ETH",
                trade_side="long", has_binary_news_risk=False, equity=1000.0,
                total_open_notional=0)
    base.update(kw)
    return GateContext(**base)


def _isolated_memory(monkeypatch, tmp_path):
    import hermes_trader.event_log as event_log
    from hermes_trader.agents import memory as memory_mod
    mem_path = str(tmp_path / ".agent-memory.json")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", mem_path)
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", mem_path + ".lock")
    events_path = str(tmp_path / "events.jsonl")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", events_path)
    monkeypatch.setattr(event_log, "EVENTS_FILE", events_path)
    m = memory_mod.AgentMemory()
    m.load()
    monkeypatch.setattr(memory_mod, "memory", m)
    # Modules that did `from ...memory import memory` at import time bound the
    # singleton as THEIR module attribute — patch those call sites too so
    # executor/risk_gates see the same isolated instance.
    import hermes_trader.agents.executor as executor_mod
    monkeypatch.setattr(executor_mod, "memory", m, raising=False)
    return m, mem_path


def _patch_drawdown_cfg(monkeypatch, *, window_days, cooldown_hours):
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


def _seed_trail(m, samples):
    """Append (age_seconds_ago, equity, cum_flow) samples directly, bypassing
    the 600s min-spacing collapse (production cadence is one tick/~10 min)."""
    now = time.time()
    for age_s, eq, flow in samples:
        m._equity_trail.append((now - float(age_s), float(eq), float(flow)))


# ── memory-level rebase maths ────────────────────────────────────────────

def test_transfer_out_leaves_zero_drawdown(monkeypatch, tmp_path):
    """$30 perp→spot (equity 100 → 70, cum_flow 0 → −30) with no trading loss:
    the re-based peak slides to 70 → 0% drawdown. Pre-CS-D this read as a 30%
    crash and froze the gate (the 2026-09-01 −58.9% / 111-block latch)."""
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m._equity = 70.0
    m._peak_equity = 100.0
    m._peak_equity_basis_flow = 0.0
    m._contrib_today = -30.0
    _seed_trail(m, [(3600, 100.0, 0.0), (60, 70.0, -30.0)])
    peak = m.rolling_peak_equity(14.0)
    assert abs(peak - 70.0) < 1e-6, f"transfer-out must slide peak to 70, got {peak}"
    st = m.drawdown_freeze_status(15.0, 14.0, 24.0)
    assert st["frozen"] is False and st["dd_pct"] == 0.0


def test_transfer_in_then_real_loss_keeps_real_gap(monkeypatch, tmp_path):
    """A $30 deposit (flow 0 → +30) lifts equity 70 → 100; a subsequent real
    $20 trading loss takes equity to 80. The re-based peak is 100 and the
    dollar gap is 20 (= the genuine loss) — the deposit must neither inflate
    nor blind the gate."""
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m._equity = 80.0
    m._peak_equity = 100.0
    m._peak_equity_basis_flow = 30.0
    m._contrib_today = 30.0
    _seed_trail(m, [(3600, 70.0, 0.0), (1800, 100.0, 30.0), (60, 80.0, 30.0)])
    peak = m.rolling_peak_equity(14.0)
    assert abs(peak - 100.0) < 1e-6, f"peak must re-base to 100, got {peak}"
    dd_pct = (peak - 80.0) / peak * 100.0
    assert abs(dd_pct - 20.0) < 1e-6, f"real $20 loss must show 20%, got {dd_pct}"


def test_all_time_peak_rebased_on_window_empty(monkeypatch, tmp_path):
    """window_days=0 (all-time peak fallback) is also re-based: a $50
    withdrawal after a $100/f+50 peak (equity back to 50, flow 0) must read
    0% drawdown, not a 50% fake crash."""
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m._equity = 50.0
    m._peak_equity = 100.0
    m._peak_equity_basis_flow = 50.0
    m._contrib_today = 0.0
    m._equity_trail.clear()
    peak = m.rolling_peak_equity(0.0)
    assert abs(peak - 50.0) < 1e-6, f"all-time peak must re-base to 50, got {peak}"


# ── gate-level decisions ─────────────────────────────────────────────────

def test_gate_passes_on_transfer_out(monkeypatch, tmp_path):
    """The production latch scenario, end to end through drawdown_gate:
    perp→spot transfer with no trading loss must PASS."""
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m._equity = 70.0
    m._peak_equity = 100.0
    m._peak_equity_basis_flow = 0.0
    m._contrib_today = -30.0
    _seed_trail(m, [(3600, 100.0, 0.0), (60, 70.0, -30.0)])
    _patch_drawdown_cfg(monkeypatch, window_days=14.0, cooldown_hours=24.0)
    r = drawdown_gate(_ctx(equity=70.0), max_drawdown_pct=15.0)
    assert r["pass"] is True, f"transfer-out must not freeze: {r}"


def test_gate_blocks_on_real_loss_after_deposit(monkeypatch, tmp_path):
    """A genuine 20% trading loss must still fail CLOSED even though a deposit
    sits in the flow basis (the re-base must not make the gate blind)."""
    from hermes_trader.agents.risk_gates import drawdown_gate
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m._equity = 80.0
    m._peak_equity = 100.0
    m._peak_equity_basis_flow = 30.0
    m._contrib_today = 30.0
    _seed_trail(m, [(3600, 100.0, 30.0), (60, 80.0, 30.0)])
    _patch_drawdown_cfg(monkeypatch, window_days=14.0, cooldown_hours=24.0)
    r = drawdown_gate(_ctx(equity=80.0), max_drawdown_pct=15.0)
    assert r["pass"] is False, f"real 20% loss must freeze: {r}"


# ── one-shot legacy migration ────────────────────────────────────────────

def test_legacy_trail_migrates_exactly_once(monkeypatch, tmp_path):
    """Pre-CS-D persisted rows are bare (ts, equity) pairs (flow unknown).
    The first accepted tick rigidly tags them onto the current flow basis and
    persists ddBasisMigrated; a reload + further flow must NOT re-tag them."""
    from hermes_trader.agents import memory as memory_mod
    _, mem_path = _isolated_memory(monkeypatch, tmp_path)
    now = time.time()
    # Write a simulated pre-CS-D memory file: 2-tuple trail, no basis fields.
    with open(mem_path, "w") as f:
        json.dump({
            "peakEquity": 100.0,
            "equityTrail": [[now - 3600, 100.0], [now - 1800, 95.0]],
        }, f)
    m = memory_mod.AgentMemory()
    m.load()
    assert m._dd_basis_migrated is False
    assert all(flow is None for _, _, flow in m._equity_trail)
    assert m._peak_equity_basis_flow == 0.0
    # First accepted tick with $30 of net external flow → one-shot migration.
    m.track_daily_pnl(100.0, net_contributions=30.0)
    assert m._dd_basis_migrated is True
    assert all(abs(flow - 30.0) < 1e-9 for _, _, flow in m._equity_trail), \
        "legacy samples must be rigidly tagged to the current cum_flow"
    assert abs(m._peak_equity_basis_flow - 30.0) < 1e-9
    assert abs(m.rolling_peak_equity(14.0) - 100.0) < 1e-6
    m.flush(force=True)
    # Reload: the persisted flag must suppress a second migration.
    m2 = memory_mod.AgentMemory()
    m2.load()
    assert m2._dd_basis_migrated is True
    assert abs(m2._peak_equity_basis_flow - 30.0) < 1e-9
    # Another $30 in (cum_flow 60): old samples keep their stored basis 30.
    m2.track_daily_pnl(100.0, net_contributions=60.0)
    old_flows = [flow for _, _, flow in list(m2._equity_trail)[:-1]]
    assert all(abs(flow - 30.0) < 1e-9 for flow in old_flows), \
        "migration must not re-run on already-annotated samples"


def test_trail_triple_roundtrip(monkeypatch, tmp_path):
    """Flow-annotated triples and the peak basis persist and reload verbatim;
    legacy 2-tuple rows parse with flow=None."""
    from hermes_trader.agents import memory as memory_mod
    m, mem_path = _isolated_memory(monkeypatch, tmp_path)
    m._peak_equity = 120.0
    m._peak_equity_basis_flow = 25.0
    m._dd_basis_migrated = True
    m._append_equity_trail_nolock(time.time() - 600, 120.0, 25.0)
    m._append_equity_trail_nolock(time.time(), 110.0, 25.0)
    m.flush(force=True)
    with open(mem_path) as f:
        raw = json.load(f)
    assert all(len(row) == 3 for row in raw["equityTrail"])
    assert abs(raw["peakEquityBasisFlow"] - 25.0) < 1e-9
    assert raw["ddBasisMigrated"] is True
    m2 = memory_mod.AgentMemory()
    m2.load()
    assert abs(m2._peak_equity_basis_flow - 25.0) < 1e-9
    assert len(m2._equity_trail) == 2
    assert all(flow is not None for _, _, flow in m2._equity_trail)
    # Parser still tolerates legacy bare pairs.
    parsed = memory_mod.AgentMemory._parse_equity_trail(
        [[time.time() - 60, 90.0], [time.time(), 91.0, 5.0]])
    assert parsed[0][2] is None and abs(parsed[1][2] - 5.0) < 1e-9


def test_rebase_drawdown_peak_tags_current_basis(monkeypatch, tmp_path):
    """rebase_drawdown_peak seeds the new baseline on the CURRENT flow basis
    (new_peak is raw ctx.equity), so the re-armed peak reads raw on the next
    tick instead of being mis-rebased."""
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    m._contrib_today = -30.0
    m._peak_equity = 100.0
    m._peak_equity_basis_flow = 0.0
    m.rebase_drawdown_peak(70.0, reason="cooldown recovery test")
    assert abs(m._peak_equity - 70.0) < 1e-9
    assert abs(m._peak_equity_basis_flow - (-30.0)) < 1e-9
    ts, eq, flow = m._equity_trail[-1]
    assert abs(eq - 70.0) < 1e-9 and abs(flow - (-30.0)) < 1e-9
    # Re-based onto cur_flow −30: 70 − (−30) + (−30) = 70 → reads raw.
    assert abs(m.rolling_peak_equity(14.0) - 70.0) < 1e-6


# ── CS-D 裁定三: freeze / rebase / roe_halt equity-snapshot audit ─────────

def _read_session_events(monkeypatch, tmp_path):
    """Point session_log at a tmp JSONL and return a reader for the events
    written during the test (the production append path)."""
    import hermes_trader.session_log as session_log
    log_path = str(tmp_path / "session-audit.jsonl")
    monkeypatch.setattr(session_log, "SESSION_LOG_FILE", log_path)
    # The flock sidecar path is derived from SESSION_LOG_FILE at call time.

    def _events():
        out = []
        try:
            with open(log_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except FileNotFoundError:
            return []
        return out

    return _events


def test_freeze_emits_one_snapshot_event_per_episode(monkeypatch, tmp_path):
    """The drawdown gate emits exactly ONE drawdown_frozen event per freeze
    episode (first stamp), carrying equity/peak/dd_pct/threshold/window —
    a freeze must never be just a timestamp with no equity context."""
    from hermes_trader.agents import risk_gates
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    read_events = _read_session_events(monkeypatch, tmp_path)
    _patch_drawdown_cfg(monkeypatch, window_days=14.0, cooldown_hours=24.0)
    # Real 20% trading loss from a 100 peak (no external flow) → gate trips.
    m._equity = 80.0
    m._peak_equity = 100.0
    m._peak_equity_basis_flow = 0.0
    _seed_trail(m, [(3600, 100.0, 0.0), (60, 80.0, 0.0)])
    r1 = risk_gates.drawdown_gate(_ctx(equity=80.0), 15.0)
    r2 = risk_gates.drawdown_gate(_ctx(equity=80.0), 15.0)  # still frozen
    assert r1["pass"] is False and r2["pass"] is False
    frozen = [e for e in read_events() if e.get("event") == "drawdown_frozen"]
    assert len(frozen) == 1, f"exactly one freeze event per episode, got {len(frozen)}"
    ev = frozen[0]
    assert abs(ev["equity"] - 80.0) < 1e-6
    assert abs(ev["peak_equity"] - 100.0) < 1e-6
    assert abs(ev["dd_pct"] - 20.0) < 1e-6
    assert ev["threshold_pct"] == 15.0 and ev["window_days"] == 14.0
    assert ev["frozen_since_ms"] > 0


def test_rebase_emits_archive_event_with_destroyed_trail_span(monkeypatch, tmp_path):
    """rebase_drawdown_peak must not destroy the rolling trail silently: the
    drawdown_peak_rebased event records old/new peaks (current-flow basis) and
    the archived trail span/sample count."""
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    read_events = _read_session_events(monkeypatch, tmp_path)
    m._equity = 80.0
    m._peak_equity = 100.0
    m._peak_equity_basis_flow = 0.0
    _seed_trail(m, [(3600, 100.0, 0.0), (1800, 95.0, 0.0), (60, 80.0, 0.0)])
    m.rebase_drawdown_peak(80.0, reason="cooldown recovery test")
    evs = [e for e in read_events() if e.get("event") == "drawdown_peak_rebased"]
    assert len(evs) == 1
    ev = evs[0]
    assert abs(ev["old_peak_equity"] - 100.0) < 1e-6
    assert abs(ev["new_peak_equity"] - 80.0) < 1e-6
    assert abs(ev["peak_reset_pct"] - 20.0) < 1e-6
    assert ev["archived_trail_samples"] == 3
    assert ev["archived_oldest_ms"] > 0 and ev["archived_newest_ms"] >= ev["archived_oldest_ms"]
    assert "cooldown recovery test" in ev["reason"]


def test_roe_halt_event_carries_equity_snapshot(monkeypatch, tmp_path):
    """A roe_halt event carries the account-level equity_snapshot (equity,
    rolling peak, dd_pct) so the post-mortem sees the book state, not just the
    per-trade ROE. Snapshot failure degrades to an empty dict, never muting
    the halt itself."""
    from hermes_trader.agents import executor
    m, _ = _isolated_memory(monkeypatch, tmp_path)
    read_events = _read_session_events(monkeypatch, tmp_path)
    # Book state: 100 peak, 40 current equity → 60% dd on the rolling basis.
    m._equity = 40.0
    m._peak_equity = 100.0
    m._peak_equity_basis_flow = 0.0
    _seed_trail(m, [(3600, 100.0, 0.0), (60, 40.0, 0.0)])
    captured = []
    fired = executor.maybe_roe_blowup_halt(
        "HYPE", -252.0, source="close", event_log=captured.append)
    assert fired is True
    assert captured and captured[0]["event"] == "roe_halt"
    snap = captured[0].get("equity_snapshot") or {}
    assert abs(snap.get("equity", 0) - 40.0) < 1e-6
    assert abs(snap.get("peak_equity", 0) - 100.0) < 1e-6
    assert abs(snap.get("dd_pct", 0) - 60.0) < 1e-6
    assert snap.get("snapshot_ts_ms", 0) > 0
    # The session-log fallback path (no injected sink) carries it too.
    m._equity = 40.0
    fired2 = executor.maybe_roe_blowup_halt("HYPE", -252.0, source="close")
    assert fired2 is True
    halts = [e for e in read_events() if e.get("event") == "roe_halt"]
    assert halts and (halts[-1].get("equity_snapshot") or {}).get("equity") == 40.0
