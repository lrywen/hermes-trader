"""Regression: per-account realized_pnl_usd must not use the GLOBAL baseline.

Bug (found 2026-11-01): _account_metrics computed
``realized_pnl_usd = wallet_balance - state["starting_balance"]`` where
``starting_balance`` is a single GLOBAL value shared by both shadow arms. When
one arm's wallet was edited directly (bypassing deposit()/reset()), the arms'
effective baselines diverge and the global subtraction mis-reports that arm.

Live evidence: maker_shadow wallet=86.874386 with Σclose_realized=-3.125613
(true baseline 90.0) but global starting_balance=100.0, so the old formula
reported -13.1256 instead of -3.1256 — a 3.2x overstatement flowing to the
dashboard. taker (baseline 100.0) was unaffected.

Fix: realized_pnl_usd = Σ(close-fill realized_pnl_usd), correct per-account by
construction and immune to deposit()/reset() baseline shifts.
"""
from __future__ import annotations

from hermes_trader.agents import shadow_book as sb


def _patch_cfg(monkeypatch, **over):
    cfg = {"enabled": True, "maker_shadow_enabled": True,
           "starting_balance": 100.0, "taker_fee_pct": 0.025,
           "maker_fee_pct": 0.01, "maker_limit_offset_bps": 5.0,
           "maker_ttl_bars": 30}
    cfg.update(over)
    monkeypatch.setattr(sb, "_shadow_cfg", lambda: dict(cfg))


def test_realized_pnl_ignores_global_baseline_drift(tmp_path, monkeypatch):
    """A maker arm whose wallet was manually edited (+70, baseline 90 vs global
    100) must report Σclose realized, not wallet minus the global 100."""
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    acc = book._account("maker_shadow")
    # Two closed fills netting -3.125613, and a wallet that was manually set to
    # 86.874386 (= true baseline 90 - 3.125613) while the GLOBAL baseline is 100.
    acc["fills"] = [
        {"type": "close", "realized_pnl_usd": 2.0, "fee_usd": 0.01},
        {"type": "close", "realized_pnl_usd": -5.125613, "fee_usd": 0.01},
        {"type": "open", "realized_pnl_usd": 0.0},  # non-close must be ignored
    ]
    acc["wallet_balance"] = 86.874386
    book.state["starting_balance"] = 100.0

    m = book._account_metrics("maker_shadow")
    # Correct: sum of close fills.
    assert m["realized_pnl_usd"] == -3.1256
    # The old (buggy) formula would have produced this — assert we do NOT.
    assert m["realized_pnl_usd"] != round(86.874386 - 100.0, 4)


def test_realized_pnl_matches_wallet_when_baseline_clean(tmp_path, monkeypatch):
    """When no manual drift, Σclose realized equals wallet - baseline (both arms
    agree), so the fix is a no-op for clean ledgers like taker."""
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    acc = book._account("taker")
    acc["fills"] = [
        {"type": "close", "realized_pnl_usd": 0.241838, "fee_usd": 0.005},
    ]
    # wallet = baseline(100) + realized(0.241838); no drift.
    acc["wallet_balance"] = 100.0 + 0.241838
    book.state["starting_balance"] = 100.0

    m = book._account_metrics("taker")
    assert m["realized_pnl_usd"] == 0.2418
    # Clean ledger: new formula agrees with the old wallet-minus-baseline.
    assert m["realized_pnl_usd"] == round(acc["wallet_balance"] - 100.0, 4)
