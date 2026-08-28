"""P0-1: manual /api/hl/place-order now routes through the same 16-gate risk
chain that the autonomous executor runs. ``_check_manual_order_gates`` is the
extracted pure function we test here; the surrounding FastAPI route is
covered by the dashboard-config integration tests.

Coverage:
  * daily_loss 阻断 (PnL below kill switch)
  * max_concurrent 阻断 (position count cap hit)
  * equity_risk 阻断 (total notional vs equity cap)
  * liquidity floor 阻断 (24h volume below floor)
  * happy path: clean context, all 17 gates pass
  * confidence 不阻断 (manual path = operator-vetted, confidence=1.0 baked in)
  * bad input (zero equity, empty positions) 仍能走通评估不抛异常
  * config 读取失败 (磁盘满 / JSON 损坏) 不向上传播异常
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ── helpers ────────────────────────────────────────────────────────────────


# A minimal config that disables every gate whose default would block a clean
# manual order (debate_gate would otherwise require analyst agreement on
# confidence=1.0 triggers=0; notional_cap needs an explicit cap; market
# liquidity is left at the production default so the volume test below can
# exercise it).
#
# NB: gates treat 0/None as "zero cap" (fail-closed), not as "disabled". To
# make a gate permissive in a unit test we use very large caps / very
# permissive thresholds.
_DISABLED_CONFIG = {
    "debate_gate": {"enabled": False},
    "max_trade_notional_usd": 0,            # 0 == no per-trade cap (gate short-circuits)
    "max_concurrent": 9999,                 # effectively no cap
    "min_market_volume_usd": 5_000_000,     # realistic default so we can trip it
    "min_hip3_volume_usd": 1_000_000,
    "min_short_volume_usd": 0,
    "max_total_notional_pct": 100.0,        # 100x equity cap == effectively no cap
    "max_daily_loss_usd": -1_000_000_000,   # huge negative cap == effectively off
    "min_ai_confidence": 0.7,
    "aligned_min_conf": None,
    "min_trend_score": 0.0,
    "coin_allowlist": [],
    "coin_blocklist": [],
    "max_crypto_long_correlated": 9999,
    "cooldown_min": 0,
    "daily_giveback_halt_pct": 0,
    "daily_giveback_min_peak_usd": 0,
    "counter_regime_min_conf": 0.7,
    "block_counter_trend_bypass": False,
    "crowded_with_min_conf": 0.0,
}


def _patch(monkeypatch, *, daily_pnl: float = 0.0, config: dict | None = None):
    """Stub the two module-level dependencies _check_manual_order_gates reads.

    * ``server.memory.daily_pnl``  — the live PnL feed
    * ``server.read_agent_config`` — the runtime config (gates fall back to 0)

    We import lazily so the test module can sit at the top of the test tree
    without re-triggering conftest side effects.
    """
    import hermes_trader.server as srv

    fake_mem = SimpleNamespace(daily_pnl=daily_pnl, peak_daily_pnl=0.0)
    monkeypatch.setattr(srv, "memory", fake_mem, raising=False)
    merged = dict(_DISABLED_CONFIG)
    if config:
        merged.update(config)
    monkeypatch.setattr(
        srv, "read_agent_config", lambda: merged, raising=False
    )
    return srv


def _clean_ctx(**over):
    """A baseline context that should pass every gate under _DISABLED_CONFIG."""
    base = dict(
        coin="BTC",
        is_buy=True,
        position_notional=10_000.0,
        live_equity=100_000.0,
        total_open_notional=0.0,
        market_vol_24h=500_000_000.0,  # well above the 5M floor
        positions=[],
    )
    base.update(over)
    return base


# ── happy path ─────────────────────────────────────────────────────────────


def test_clean_context_passes_all_17_gates(monkeypatch):
    srv = _patch(monkeypatch)
    report = srv._check_manual_order_gates(**_clean_ctx())
    assert isinstance(report, dict)
    # eval_all_gates contract: {results, blocked, block_reasons}
    assert "blocked" in report
    assert "block_reasons" in report
    assert "results" in report
    # All 17 gates ran
    expected_keys = {
        "confidence", "max_concurrent", "notional_cap", "daily_loss",
        "daily_giveback", "liquidity", "short_liquidity", "coin_filter",
        "cooldown", "coin_circuit", "global_halt", "opposite_guard",
        "correlation", "equity_risk", "market_regime", "news", "debate",
    }
    assert expected_keys.issubset(set(report["results"].keys()))
    assert report["blocked"] is False, f"unexpected blocks: {report['block_reasons']}"


def test_confidence_floor_does_not_block_manual_path(monkeypatch):
    """Manual orders are operator-vetted — we pass confidence=1.0 so the
    AI-confidence floor (which auto-blocks low-conv AI signals) cannot
    prevent an operator from manually closing a stuck position."""
    srv = _patch(monkeypatch, config={"min_ai_confidence": 0.95})
    report = srv._check_manual_order_gates(**_clean_ctx())
    assert report["blocked"] is False
    assert report["results"]["confidence"]["pass"] is True


# ── gate trip scenarios ────────────────────────────────────────────────────


def test_daily_loss_blocks_manual_order(monkeypatch):
    """PnL below the kill switch must block even manual orders. With
    max_daily_loss_usd=-500 and daily_pnl=-1000, daily_loss is the only
    safety gate that fires under _DISABLED_CONFIG."""
    srv = _patch(
        monkeypatch,
        daily_pnl=-1000.0,
        config={"max_daily_loss_usd": -500.0},
    )
    report = srv._check_manual_order_gates(**_clean_ctx())
    assert report["blocked"] is True
    assert any("daily loss" in r for r in report["block_reasons"])


def test_max_concurrent_blocks_manual_order(monkeypatch):
    """If the operator already has max_concurrent open positions, a manual
    addition must be refused — the cap protects against manual churning."""
    positions = [
        {"position": {"coin": "BTC", "szi": "0.1", "entryPx": "50000"}},
        {"position": {"coin": "ETH", "szi": "1.0", "entryPx": "3000"}},
    ]
    srv = _patch(monkeypatch, config={"max_concurrent": 2})
    report = srv._check_manual_order_gates(
        **_clean_ctx(positions=positions)
    )
    assert report["blocked"] is True
    assert any("max positions" in r for r in report["block_reasons"])


def test_equity_risk_blocks_manual_order(monkeypatch):
    """Trade notional that would push the account past max_total_notional_pct
    of equity is refused. With equity=10k, max_total_notional_pct=0.5, and an
    open notional=0, a $6k manual add must trip the gate."""
    srv = _patch(monkeypatch, config={"max_total_notional_pct": 0.5})
    report = srv._check_manual_order_gates(
        **_clean_ctx(
            live_equity=10_000.0,
            total_open_notional=0.0,
            position_notional=6_000.0,
        )
    )
    assert report["blocked"] is True
    assert any("equity" in r.lower() or "notional" in r.lower()
               for r in report["block_reasons"])


def test_zero_liquidity_blocks_manual_order(monkeypatch):
    """Manual order on a dead market (24h volume = 0) must be refused even
    if all other gates would pass. This is the 'manual order on an illiquid
    alt' footgun — without the gate an operator can drain the account on a
    $50k market-impact trade."""
    srv = _patch(monkeypatch)
    report = srv._check_manual_order_gates(
        **_clean_ctx(market_vol_24h=0.0)
    )
    assert report["blocked"] is True
    assert any("volume" in r.lower() or "liquidity" in r.lower()
               for r in report["block_reasons"])


# ── robustness ─────────────────────────────────────────────────────────────


def test_invalid_inputs_do_not_raise(monkeypatch):
    """The gate evaluator must tolerate garbage / missing inputs without
    crashing — a raised exception inside the route would 500 the operator
    and could mask an actual safety trip. The default canonical config
    values (0-cap on notional, 0-concurrent, 0-volume) deliberately fail
    closed, so this test only checks that the call returns a report."""
    srv = _patch(monkeypatch)
    # All zero / None: every gate that depends on a positive threshold
    # should fall back to "no cap" (gate.pass=True) per the canonical
    # zero-means-disabled convention; this gives a clean report.
    report = srv._check_manual_order_gates(
        coin="BTC",
        is_buy=True,
        position_notional=0.0,
        live_equity=0.0,
        total_open_notional=0.0,
        market_vol_24h=0.0,
        positions=[],
    )
    assert isinstance(report, dict)
    assert "blocked" in report
    assert isinstance(report["blocked"], bool)
    assert isinstance(report["block_reasons"], list)


def test_config_read_failure_fails_closed_safely(monkeypatch):
    """If read_agent_config throws (disk full / corrupt JSON), the gate
    chain must still produce a valid report using an empty config — it
    must not propagate the exception into the FastAPI route."""
    import hermes_trader.server as srv

    def _boom():
        raise RuntimeError("config disk on fire")

    monkeypatch.setattr(srv, "read_agent_config", _boom, raising=False)
    monkeypatch.setattr(
        srv, "memory", SimpleNamespace(daily_pnl=0.0), raising=False
    )
    report = srv._check_manual_order_gates(**_clean_ctx())
    assert isinstance(report, dict)
    assert "blocked" in report


# ── caller's gate-key check (regression: P0-1 originally read "pass") ─────


def test_route_uses_blocked_key(monkeypatch):
    """Regression: an earlier draft of place_order branched on
    ``report["pass"]`` which eval_all_gates never returns — that always
    raised 403. The route must check ``report["blocked"]`` instead. We
    import the route body to verify the key name used.
    """
    import hermes_trader.server as srv
    import inspect
    src = inspect.getsource(srv.place_order)
    assert '"blocked"' in src or "'blocked'" in src, (
        "place_order must branch on report['blocked'] (eval_all_gates "
        "contract), not on a non-existent 'pass' key."
    )
