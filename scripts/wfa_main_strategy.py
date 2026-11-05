#!/usr/bin/env python3
"""P1.5 — hook walk-forward analysis onto the MAIN strategy's realized PnL.

Why this exists: ``scripts/shadow_grade.py:r5_robustness`` already computes WFA /
block-bootstrap / DSR / OOS-negative-fold-ratio, but only for *experimental gate
arms* (its input is per-trade counterfactual ``pnl_pct``). Nothing WFA-graded the
main strategy's own PnL, so "WFA 通过" could not be claimed for the book that
actually trades.

The main strategy's only sufficient PnL history is the taker SHADOW ledger
(``.shadow-book.json`` → ``accounts.taker.fills``; live ``.agent-memory.json``
holds ~1 close). This script therefore:

* aggregates taker **close** fills into an ordered per-UTC-day bps series
  (``Σ realized_pnl_usd / base_equity × 1e4``, base = wallet − Σrealized, i.e.
  the equity before the first booked close — a constant denominator, so the
  series is a return-on-starting-equity, not a compounding series);
* runs the single-source statistics from ``hermes_trader.validation``;
* pins the config ``era_id`` + era-tracked strategy parameters alongside the
  result, so a WFA record is only valid for the parameter set it ran under.

Pure computation over a state dict; read-only. Fail-closed: fewer than
``MIN_DAYS`` UTC days → ``None`` (never guessed from a short sample).

Usage (inside the container):
    python3 scripts/wfa_main_strategy.py
    python3 scripts/wfa_main_strategy.py --account maker_shadow --out /tmp/wfa.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from hermes_trader.validation.trial_ledger import (
    TrialLedger,
    TrialLedgerError,
    deflated_sharpe_from_ledger,
)

DAY_MS = 86_400_000

# Same acceptance constants shadow_grade uses for R5, kept in one family so the
# main-strategy WFA and the arm R5 block cannot silently diverge.
MIN_DAYS = 20
WFA_TRAIN_DAYS = 12
WFA_TEST_DAYS = 4
DSR_ACCEPT_P = 0.95
WFR_MIN = 0.5

DEFAULT_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
DEFAULT_SHADOW_BOOK = os.environ.get(
    "HERMES_SHADOW_BOOK_FILE",
    os.path.join(DEFAULT_DATA_DIR, ".shadow-book.json"),
)
DEFAULT_CONFIG_FILE = os.environ.get(
    "HERMES_AGENT_CONFIG_FILE",
    os.path.join(DEFAULT_DATA_DIR, ".agent-config.json"),
)


def _utc_day(day: int) -> str:
    return datetime.fromtimestamp(day * 86_400, timezone.utc).strftime("%Y-%m-%d")


def daily_realized_bps(state: Any, account: str = "taker") -> dict[str, Any]:
    """Per-UTC-day return bps of one account's booked closes (pure).

    ``base_equity = wallet_balance - Σ realized_pnl_usd`` is the equity the
    account held before its first booked close (any reset offset is thereby
    removed). Only fills with a parseable ``ts`` and ``realized_pnl_usd`` take
    part in both numerator and base, so the two sides can never drift.

    Returns ``{"ok": bool, "series": [...], "days": [...], ...}``; ``ok`` is
    False (with ``reason``) when the account is missing or base equity is
    non-positive — callers must treat that as "cannot grade", not as zero edge.
    """
    accounts = state.get("accounts") if isinstance(state, dict) else None
    acc = accounts.get(account) if isinstance(accounts, dict) else None
    if not isinstance(acc, dict):
        return {"ok": False, "reason": f"account {account!r} missing",
                "series": [], "days": []}

    fills = [f for f in (acc.get("fills") or []) if isinstance(f, dict)]
    closes = [f for f in fills if f.get("type") == "close"]
    by_day: dict[int, float] = {}
    used = 0
    dropped_ts = 0
    dropped_pnl = 0
    for f in closes:
        try:
            ts = int(f.get("ts"))
        except (TypeError, ValueError):
            dropped_ts += 1
            continue
        pnl = f.get("realized_pnl_usd")
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            dropped_pnl += 1
            continue
        if ts <= 0:
            dropped_ts += 1
            continue
        day = ts // DAY_MS
        by_day[day] = by_day.get(day, 0.0) + pnl
        used += 1

    total_net = sum(by_day.values())
    wallet = float(acc.get("wallet_balance") or 0.0)
    base = wallet - total_net
    out: dict[str, Any] = {
        "ok": False,
        "account": account,
        "series": [],
        "days": [],
        "closes_used": used,
        "closes_dropped_missing_ts": dropped_ts,
        "closes_dropped_missing_pnl": dropped_pnl,
        "wallet_balance": round(wallet, 6),
        "sum_realized_net_usd": round(total_net, 6),
        "base_equity": round(base, 6),
        "start_day": None,
        "end_day": None,
    }
    if base <= 0:
        out["reason"] = "base_equity_non_positive"
        return out
    days = sorted(by_day)
    out.update({
        "ok": True,
        "series": [by_day[d] / base * 1e4 for d in days],
        "days": days,
        "start_day": _utc_day(days[0]) if days else None,
        "end_day": _utc_day(days[-1]) if days else None,
    })
    return out


def main_strategy_wfa(
        series: list[float],
        train_size: int = WFA_TRAIN_DAYS,
        test_size: int = WFA_TEST_DAYS,
        n_trials: Optional[int] = None,
        *,
        as_of=None,
        ledger_path=None,
        ledger: Optional[TrialLedger] = None) -> Optional[dict[str, Any]]:
    """WFA + bootstrap CI + ledger-backed DSR on daily bps (pure).

    ``n_trials`` remains only for positional call compatibility and is ignored.
    Missing ledger coverage or an unknown ``as_of`` produces a structured
    fail-closed result, never a default trial count. Fewer than ``MIN_DAYS``
    days still returns ``None`` for insufficient history.
    """
    if len(series) < MIN_DAYS:
        return None
    from hermes_trader.validation import (
        block_bootstrap_ci,
        probability_of_backtest_overfitting,
        sharpe,
        walk_forward_analysis,
    )

    wfa = walk_forward_analysis(series, train_size=train_size, test_size=test_size)
    lo, hi = block_bootstrap_ci(series)
    sr = sharpe(series)
    dsr = None
    ledger_n_trials = None
    evidence = None
    dsr_error = None
    try:
        evidence = deflated_sharpe_from_ledger(
            observed_sharpe=sr,
            n_obs=len(series),
            arm="main",
            paradigm="wfa_main_strategy",
            as_of=as_of,
            ledger_path=ledger_path,
            ledger=ledger,
        )
        dsr = evidence["dsr_p"]
        ledger_n_trials = evidence["n_trials"]
    except TrialLedgerError as exc:
        dsr_error = exc.as_dict()
    # H3 (2026-11-04): this is the fraction of WFA OOS folds with negative mean
    # return, NOT the CSCV probability of backtest overfitting. A single strategy
    # has no config-comparison matrix, so a true CSCV PBO cannot be computed;
    # emitting it under the name "pbo" overstated what it means. Renamed to
    # oos_negative_fold_ratio; true CSCV is a separate future work item.
    oos_negative_fold_ratio = probability_of_backtest_overfitting(
        [max(v, 0.0) for v in wfa.oos_mean_bps],
        list(wfa.oos_mean_bps))
    mean_bps = statistics.mean(series)
    mean_oos_bps = statistics.mean(wfa.oos_mean_bps)
    dsr_statistical_passes = dsr is not None and dsr >= DSR_ACCEPT_P
    dsr_promotion_eligible = bool(evidence and evidence["promotion_eligible"])
    checks = {
        "wfr_gt_min": wfa.wfr > WFR_MIN,
        "oos_mean_bps_gt_0": mean_oos_bps > 0,
        "dsr_p_ge_accept": dsr_statistical_passes and dsr_promotion_eligible,
        "bb_ci_lo_gt_0": lo > 0,
    }
    verdict = "pass" if all(checks.values()) else (
        "fail_closed" if dsr_error is not None else "fail")
    return {
        "days": len(series),
        "as_of": evidence["as_of"] if evidence else as_of,
        "mean_bps": round(mean_bps, 2),
        "sharpe": round(sr, 3),
        "bb_ci_lo_bps": round(lo, 2),
        "bb_ci_hi_bps": round(hi, 2),
        "wfa_windows": wfa.n_windows,
        "oos_mean_bps": round(mean_oos_bps, 2),
        "oos_win_frac": round(wfa.oos_win_frac, 3),
        "wfr": round(wfa.wfr, 3),
        "wfe": round(wfa.wfe, 3),
        "dsr_p": round(dsr, 4) if dsr is not None else None,
        "n_trials": ledger_n_trials,
        "trial_evidence": evidence,
        "dsr_error": dsr_error,
        "dsr_statistical_passes": dsr_statistical_passes,
        "dsr_promotion_eligible": dsr_promotion_eligible,
        "oos_negative_fold_ratio": round(oos_negative_fold_ratio, 3),
        "checks": checks,
        "verdict": verdict,
    }


def config_pin(config_file: str) -> dict[str, Any]:
    """``era_id`` + era-tracked strategy parameters for the effective config.

    Best-effort: a missing/unreadable config yields ``era_id=None`` (recorded,
    never raised) so an offline WFA run cannot be blocked by config I/O.
    """
    if not os.path.exists(config_file):
        return {"file": config_file, "era_id": None, "tracked": {},
                "reason": "config_file_missing"}
    prev = os.environ.get("HERMES_AGENT_CONFIG_FILE")
    os.environ["HERMES_AGENT_CONFIG_FILE"] = config_file
    try:
        from hermes_trader.agents.config_store import (
            compute_config_era,
            read_agent_config,
        )
        effective = read_agent_config()
        era = compute_config_era(effective)
        return {
            "file": config_file,
            "era_id": era.get("era_id"),
            "tracked": era.get("tracked") or {},
        }
    except Exception as e:  # pragma: no cover - offline observability only
        return {"file": config_file, "era_id": None, "tracked": {},
                "reason": str(e)[:200]}
    finally:
        if prev is None:
            os.environ.pop("HERMES_AGENT_CONFIG_FILE", None)
        else:
            os.environ["HERMES_AGENT_CONFIG_FILE"] = prev


def evaluate(
        state: Any,
        account: str = "taker",
        config_file: str = DEFAULT_CONFIG_FILE,
        train_size: int = WFA_TRAIN_DAYS,
        test_size: int = WFA_TEST_DAYS,
        n_trials: Optional[int] = None,
        *,
        as_of=None,
        ledger_path=None,
        ledger: Optional[TrialLedger] = None) -> dict[str, Any]:
    """Full report: realized PnL, WFA statistics and ledger-backed DSR."""
    led = daily_realized_bps(state, account=account)
    if as_of is None and led.get("end_day"):
        as_of = f"{led['end_day']}T23:59:59Z"
    report: dict[str, Any] = {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of,
        "account": account,
        "params": {
            "train_days": train_size,
            "test_days": test_size,
            "min_days": MIN_DAYS,
            "wfr_min": WFR_MIN,
            "dsr_accept_p": DSR_ACCEPT_P,
        },
        "config": config_pin(config_file),
        "ledger": {k: v for k, v in led.items() if k not in ("series", "days")},
        "daily_bps": led["series"],
        "wfa": None,
        "verdict": "insufficient_history",
    }
    if not led["ok"]:
        report["verdict"] = "cannot_grade"
        return report
    stats = main_strategy_wfa(
        led["series"], train_size=train_size, test_size=test_size,
        as_of=as_of, ledger_path=ledger_path, ledger=ledger)
    if stats is None:
        return report
    report["wfa"] = stats
    report["verdict"] = stats["verdict"]
    return report


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shadow-book", default=DEFAULT_SHADOW_BOOK)
    ap.add_argument("--config-file", default=DEFAULT_CONFIG_FILE)
    ap.add_argument("--account", default="taker")
    ap.add_argument("--train", type=int, default=WFA_TRAIN_DAYS)
    ap.add_argument("--test", type=int, default=WFA_TEST_DAYS)
    ap.add_argument("--as-of", default=None,
                    help="DSR 账本截止时刻；默认取最后收益日 UTC 日终")
    ap.add_argument("--trial-ledger", default=None,
                    help="DSR 试验账本 JSONL（默认读包内账本或环境变量）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 unless verdict == pass")
    args = ap.parse_args(argv)

    state = _read_json(args.shadow_book)
    report = evaluate(state or {}, account=args.account,
                      config_file=args.config_file,
                      train_size=args.train, test_size=args.test,
                      as_of=args.as_of, ledger_path=args.trial_ledger)
    blob = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(blob + "\n")
    print(blob)
    if args.strict and report["verdict"] != "pass":
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
