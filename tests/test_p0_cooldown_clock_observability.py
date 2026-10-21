"""P0 second-batch observability (2026-10-21 redundancy plan).

Locks the two new PURE evaluators:
  - executor._per_coin_cooldown_would_block: front-loaded cooldown
    counterfactual (would_block + reason), read-only, never raises;
  - config_store.exit_clock_reachability_warnings: non-fatal startup check that
    flags unreachable exit clocks (ignite >= stale, hard <= stale).

No trading behaviour change: these only describe what WOULD happen.
"""
from __future__ import annotations

from types import SimpleNamespace

from hermes_trader.agents import executor
from hermes_trader.agents.config_store import exit_clock_reachability_warnings


# ── A2: per-coin cooldown counterfactual ────────────────────────────────────

def _gate_pc(pc, **kw):
    base = {"enabled": True, "per_coin_cooldown": pc}
    base.update(kw)
    return {"runner_entry_gate": base, "mode": "SHADOW"}


def _fake_memory(monkeypatch, *, closes=None, consec=0):
    fm = SimpleNamespace(
        get_closes=lambda limit=200: list(closes or []),
        consecutive_losses=lambda coin: consec,
        last_close_for=lambda coin: None,
    )
    monkeypatch.setattr(executor, "memory", fm)


def test_cooldown_not_configured_is_noop(monkeypatch):
    v = executor._per_coin_cooldown_would_block(
        coin="AAA", side="long", score=10, gate_conf=0.9,
        gate={}, config={"mode": "SHADOW"})
    assert v["configured"] is False
    assert v["would_block"] is False


def test_cooldown_consecutive_losses_blocks(monkeypatch):
    _fake_memory(monkeypatch, closes=[], consec=3)
    pc = {"window_hours": 24, "loss_cooldown_hours": 24,
          "max_consecutive_losses": 2, "repeat_min_composite": 45}
    v = executor._per_coin_cooldown_would_block(
        coin="AAA", side="long", score=80, gate_conf=0.9,
        gate={"per_coin_cooldown": pc}, config={"mode": "SHADOW"})
    assert v["would_block"] is True
    assert "consecutive_losses 3 >= 2" in v["reason"]


def test_cooldown_repeat_low_score_blocks(monkeypatch):
    now = 1_000_000_000_000
    closes = [{"coin": "AAA", "closed_at": now - 60_000, "reason": "floor_breach"}]
    _fake_memory(monkeypatch, closes=closes, consec=0)
    pc = {"window_hours": 24, "max_consecutive_losses": 2,
          "repeat_min_composite": 45}
    v = executor._per_coin_cooldown_would_block(
        coin="AAA", side="long", score=30, gate_conf=0.9,
        gate={"per_coin_cooldown": pc}, config={"mode": "SHADOW"}, now_ms=now)
    assert v["would_block"] is True
    assert "repeat entry" in v["reason"]
    assert v["entries_in_window"] == 1


def test_cooldown_clean_history_does_not_block(monkeypatch):
    _fake_memory(monkeypatch, closes=[], consec=0)
    pc = {"window_hours": 24, "max_consecutive_losses": 2,
          "repeat_min_composite": 45}
    v = executor._per_coin_cooldown_would_block(
        coin="AAA", side="long", score=80, gate_conf=0.9,
        gate={"per_coin_cooldown": pc}, config={"mode": "SHADOW"})
    assert v["would_block"] is False
    assert v["reason"] == ""


def test_cooldown_hemostat_timeout_blocks_in_shadow(monkeypatch):
    now = 1_000_000_000_000
    _fake_memory(monkeypatch, closes=[], consec=0)
    # SHADOW hemostat reads the paper book's last taker close.
    fake_book = SimpleNamespace(
        last_taker_close=lambda coin: {
            "ts": now - 10 * 60_000, "reason": "ignite_timeout (90min no launch)"})
    import hermes_trader.agents.shadow_book as sb
    monkeypatch.setattr(sb, "get_book", lambda: fake_book)
    pc = {"window_hours": 24, "max_consecutive_losses": 2,
          "repeat_min_composite": 45, "timeout_cooldown_min": 30}
    v = executor._per_coin_cooldown_would_block(
        coin="AAA", side="long", score=80, gate_conf=0.9,
        gate={"per_coin_cooldown": pc}, config={"mode": "SHADOW"}, now_ms=now)
    assert v["would_block"] is True
    assert v.get("hemostat_timeout") is True
    assert "ignite_timeout exit 10min ago" in v["reason"]


def test_cooldown_lookup_failure_is_unavailable_not_raise(monkeypatch):
    def _boom(limit=200):
        raise RuntimeError("memory down")
    monkeypatch.setattr(executor, "memory",
                        SimpleNamespace(get_closes=_boom))
    pc = {"window_hours": 24, "max_consecutive_losses": 2}
    v = executor._per_coin_cooldown_would_block(
        coin="AAA", side="long", score=80, gate_conf=0.9,
        gate={"per_coin_cooldown": pc}, config={"mode": "SHADOW"})
    assert v.get("verdict") == "unavailable"
    assert v["would_block"] is False
    assert v["error"] == "RuntimeError"


# ── L5-A2: exit clock reachability ─────────────────────────────────────────

def _clocks_cfg(ignite=90, trend=None, non_trend=None, base_stale=90,
                base_hard=240, enabled=True):
    return {"dsl_exit": {
        "ignite_grace_minutes": ignite,
        "stale_flat_timeout_minutes": base_stale,
        "hard_timeout_minutes": base_hard,
        "regime_aware": {"clocks": {
            "enabled": enabled,
            "trend": trend or {"stale_flat_timeout_minutes": 120,
                               "hard_timeout_minutes": 240},
            "non_trend": non_trend or {"stale_flat_timeout_minutes": 60,
                                       "hard_timeout_minutes": 120},
        }}}}


def test_clock_check_flags_dead_ignite_in_non_trend():
    # Production shape: non_trend stale=60 < ignite=90 -> dead ignite gate.
    warns = exit_clock_reachability_warnings(_clocks_cfg(ignite=90))
    joined = " | ".join(warns)
    assert any("non_trend:" in w and "ignite" in w for w in warns)
    assert "non_trend" in joined
    # trend stale=120 > ignite=90 -> reachable, no trend ignite warning.
    assert not any("reachability: trend:" in w and "ignite" in w for w in warns)


def test_clock_check_clean_config_no_warnings():
    # ignite shorter than every stale; hard longer than every stale.
    cfg = _clocks_cfg(
        ignite=45,
        trend={"stale_flat_timeout_minutes": 120, "hard_timeout_minutes": 240},
        non_trend={"stale_flat_timeout_minutes": 60, "hard_timeout_minutes": 120})
    assert exit_clock_reachability_warnings(cfg) == []


def test_clock_check_flags_hard_not_backstop():
    cfg = _clocks_cfg(
        ignite=30,
        trend={"stale_flat_timeout_minutes": 240, "hard_timeout_minutes": 120},
        non_trend={"stale_flat_timeout_minutes": 60, "hard_timeout_minutes": 120})
    warns = exit_clock_reachability_warnings(cfg)
    assert any("trend" in w and "hard timeout is not a backstop" in w
               for w in warns)


def test_clock_check_disabled_is_silent():
    cfg = _clocks_cfg(enabled=False)
    assert exit_clock_reachability_warnings(cfg) == []
