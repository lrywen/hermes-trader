#!/usr/bin/env python3
"""xs_reversal universe sweep — independent hourly scan (2026-11-06).

Why this exists: the xs_reversal shadow arm used to be dispatched ONLY from
``executor._dispatch_entry_shadow_probes``, i.e. only when a coin happened to
be an entry candidate in that loop. 2026-11-03..05 the evaluation count was
exactly the daily long-analysis count (42/74/19), so on days with few long
candidates (or pure-short days) the arm was starved: no trigger for 3.3 days
(2026-11-02 14:00Z onward) while the heartbeat still looked fresh — a heartbeat
proves "one dispatch evaluated something", not "the universe was scanned".

This script decouples evaluation from the entry path: once per hour it walks
the scan universe (top-N perps by 24h volume) and calls
``xs_reversal.gather_xs_reversal`` — the SAME MIN_BARS / trigger thresholds /
de-dup / heartbeat code as the event-driven path, so there is no second
implementation and no new record format.

Safety properties:
  * mode=off -> zero network access, exit 0 (same fail-closed semantics as
    ``run_xs_reversal_async``).
  * record-only: appends to the shadow JSONL + rewrites the heartbeat. It never
    places, sizes, blocks or cancels an order.
  * bounded: serial, ``--budget-seconds`` cap; coins left un-evaluated when the
    budget is exhausted are reported, never retried in-process.
  * fetch goes through hl_client's shared rate limiter, so a sweep cannot
    bypass the exchange weight budget.

Usage:
    python3 scripts/xs_reversal_sweep.py
    python3 scripts/xs_reversal_sweep.py --max-coins 20 --budget-seconds 300
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config_store import read_agent_config
from hermes_trader.agents import xs_reversal


def _universe(max_coins: int) -> list[str]:
    """Current scan universe (top-N by 24h volume), same source as perception."""
    from hermes_trader.client.universe import get_universe

    markets = get_universe()
    perps = [m for m in markets if m.get("type") == "perp"
             and not str(m.get("coin", "")).startswith("@")]
    perps.sort(key=lambda m: m.get("dayNtlVlm", 0) or 0, reverse=True)
    return [m["coin"] for m in perps[:max_coins]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="xs_reversal hourly universe sweep")
    ap.add_argument("--max-coins", type=int, default=44,
                    help="cap on the number of universe coins to sweep")
    ap.add_argument("--budget-seconds", type=float, default=540.0,
                    help="wall-clock budget; remaining coins are reported, not retried")
    ap.add_argument("--sleep-s", type=float, default=1.0,
                    help="spacing between coins (lets the shared HL rate bucket refill)")
    args = ap.parse_args(argv)

    config = read_agent_config()
    mode = xs_reversal._resolve_mode(config)
    if mode == "off":
        print("[xs-sweep] mode=off -> no network, nothing to do")
        return 0

    try:
        coins = _universe(max(1, args.max_coins))
    except Exception as e:
        print(f"[xs-sweep] universe fetch failed: {e}")
        return 1
    if not coins:
        print("[xs-sweep] empty universe -> nothing to do")
        return 0

    started = time.time()
    deadline = started + max(1.0, args.budget_seconds)
    evaluated = triggered = failed = 0
    for i, coin in enumerate(coins):
        if time.time() >= deadline:
            print(f"[xs-sweep] budget exhausted after {evaluated} coins; "
                  f"skipped={len(coins) - i}")
            break
        # gather_xs_reversal already swallows network/indicator errors into a
        # debug log + heartbeat; the try/except is a last-resort guard so one
        # bad coin cannot abort the sweep.
        try:
            rec = xs_reversal.gather_xs_reversal(coin, "long", config=config)
        except Exception as e:  # pragma: no cover - defensive
            failed += 1
            print(f"[xs-sweep] {coin} failed: {e}")
        else:
            evaluated += 1
            if rec is not None:
                triggered += 1
                print(f"[xs-sweep] trigger {coin} "
                      f"candidate={rec.get('is_candidate')} "
                      f"ext={rec.get('ext_pct')} pctile={rec.get('ext_percentile')}")
        if args.sleep_s > 0 and i + 1 < len(coins):
            time.sleep(args.sleep_s)

    elapsed = time.time() - started
    print(f"[xs-sweep] mode={mode} coins={len(coins)} evaluated={evaluated} "
          f"triggered={triggered} failed={failed} elapsed={elapsed:.1f}s")
    # Only a total failure is worth a scheduler error line; partial failures are
    # already in the printed summary and in each coin's heartbeat.
    return 1 if evaluated == 0 and failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
