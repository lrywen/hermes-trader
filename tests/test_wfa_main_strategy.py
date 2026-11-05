"""P1.5 wfa_main_strategy: ledger→daily bps aggregation + WFA wiring."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import wfa_main_strategy as w  # noqa: E402

DAY_MS = 86_400_000
AS_OF = "2026-01-01T00:00:00Z"


def _ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    rec = {
        "ledger_version": 2, "trial_id": "main-prospective",
        "family": "main::wfa_main_strategy",
        "arm": "main", "paradigm": "wfa_main_strategy", "iteration": 1,
        "test_at": "2025-01-02T00:00:00Z", "recorded_at": "2025-01-02T00:00:00Z",
        "preregistration": {
            "status": "preregistered", "registered_at": "2025-01-01T00:00:00Z",
            "prospective": True,
        },
        "source": "preregistered", "status": "active",
        "provenance": {"adr": "docs/adr/test.md"},
    }
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return str(path)


def _state(closes, wallet, account="taker"):
    return {"version": 2, "accounts": {account: {
        "wallet_balance": wallet, "fills": closes}}}


def _close(day, pnl, **kw):
    fill = {"type": "close", "ts": day * DAY_MS + 3_600_000,
            "realized_pnl_usd": pnl}
    fill.update(kw)
    return fill


def test_daily_bps_groups_same_day_and_uses_constant_base():
    # base = wallet - sum(close net) = 100.8 - 0.8 = 100.0
    state = _state([
        _close(100, 0.6),
        _close(100, 0.4),
        _close(101, -0.2),
        {"type": "open", "ts": 101 * DAY_MS, "realized_pnl_usd": 9.9},
    ], wallet=100.8)
    out = w.daily_realized_bps(state)
    assert out["ok"] is True
    assert out["base_equity"] == 100.0
    assert out["closes_used"] == 3
    assert out["days"] == [100, 101]
    assert out["series"][0] == 100.0      # (0.6+0.4)/100 * 1e4
    assert out["series"][1] == -20.0      # -0.2/100 * 1e4
    assert out["sum_realized_net_usd"] == 0.8


def test_daily_bps_drops_unparseable_fills_and_reports_counts():
    state = _state([
        {"type": "close", "realized_pnl_usd": 1.0},          # no ts
        {"type": "close", "ts": 5 * DAY_MS},                 # no pnl
        _close(6, 0.5),
    ], wallet=100.5)
    out = w.daily_realized_bps(state)
    assert out["ok"] is True
    assert out["closes_used"] == 1
    assert out["closes_dropped_missing_ts"] == 1
    assert out["closes_dropped_missing_pnl"] == 1
    assert out["days"] == [6]


def test_daily_bps_fails_closed_on_missing_account_or_bad_base():
    assert w.daily_realized_bps(_state([], 100.0), account="nope")["ok"] is False
    # wallet below the realized sum => base equity <= 0, cannot normalize
    bad = w.daily_realized_bps(_state([_close(1, 5.0)], wallet=1.0))
    assert bad["ok"] is False
    assert bad["reason"] == "base_equity_non_positive"


def test_main_strategy_wfa_insufficient_history_returns_none():
    assert w.main_strategy_wfa([1.0] * (w.MIN_DAYS - 1)) is None


def test_main_strategy_wfa_passes_on_consistent_positive_series(tmp_path):
    series = ([5.0, 3.0, 7.0, 1.0] * 6)[:24]
    stats = w.main_strategy_wfa(series, n_trials=999, as_of=AS_OF,
                                ledger_path=_ledger(tmp_path))
    assert stats is not None
    assert stats["days"] == 24
    assert stats["wfa_windows"] == 3
    assert stats["oos_mean_bps"] == 4.0
    assert stats["wfr"] == 1.0
    assert stats["verdict"] == "pass"
    assert stats["checks"]["bb_ci_lo_gt_0"] is True


def test_main_strategy_wfa_fails_on_negative_series(tmp_path):
    series = [-5.0, -3.0, -7.0, -1.0] * 6
    stats = w.main_strategy_wfa(series, as_of=AS_OF,
                                ledger_path=_ledger(tmp_path))
    assert stats is not None
    assert stats["verdict"] == "fail"
    assert stats["checks"]["oos_mean_bps_gt_0"] is False


def test_evaluate_pins_params_and_config(tmp_path):
    state = _state([_close(20 + i, 1.0 if i % 2 else 2.0) for i in range(24)],
                   wallet=100.0 + 36.0)
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    ledger = _ledger(tmp_path)
    rep = w.evaluate(state, config_file=str(cfg), as_of=AS_OF,
                     ledger_path=ledger)
    assert rep["ledger"]["closes_used"] == 24
    assert rep["params"]["train_days"] == w.WFA_TRAIN_DAYS
    assert "n_trials" not in rep["params"]
    assert rep["wfa"]["n_trials"] == 1
    assert rep["config"]["era_id"] is not None
    assert rep["verdict"] in ("pass", "fail")
    # missing config is recorded, not raised
    rep2 = w.evaluate(state, config_file=str(tmp_path / "absent.json"),
                      as_of=AS_OF, ledger_path=ledger)
    assert rep2["config"]["era_id"] is None
    assert rep2["config"]["reason"] == "config_file_missing"
