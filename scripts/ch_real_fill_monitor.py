#!/usr/bin/env python3
"""Monitor REAL shadow fills sliced by the perception channel that admitted them.

This is the evidence path that can actually justify changing a trading gate.
The historical forward-return buckets (ch_bucket_forward_returns) and the
event-time replay (ch_event_replay) both used public candles / a bar-level
simulation; neither is a realized fill. The P0-A instrumentation (deployed
2026-10-20) pins ``fired_names`` / ``composite_score`` / ``ai_confidence`` /
``trigger_score`` onto every OPEN fill after that date. A close fill carries the
same ``position_id`` plus the DSL ``reason`` and realized leveraged ROE, so each
*closed labeled round trip* is one real, cost-inclusive observation tagged with
the mutually-exclusive surface channel.

This script is read-only and network-free:
  - load the shadow book (HERMES_SHADOW_BOOK_FILE, default /data/.shadow-book
    inside the container; override for a local copy),
  - inner-join labeled opens to their close on position_id per account,
  - classify the channel with the SAME OR precedence as production
    (burst>trend>pattern>mover>gate), reusing ch_signal_extract.classify,
  - bucket realized ROE / win by channel, composite band and DSL exit reason,
  - report sample-size progress vs the ~130 closed fills/bucket needed (paired
    analysis) to detect a ~2pp mean-ROE move at the power used by the replay.

Fills before instrumentation carry no fired_names and are reported only as an
unlabeled legacy total (never silently bucketed into a channel). Open positions
without a close are excluded (right-censored) and counted separately.

Env:
  HERMES_SHADOW_BOOK_FILE  path to .shadow-book.json (default /data/.json)
  CH_MONITOR_MIN_N         per-bucket sample target for the progress bar (130)
  CH_MONITOR_JSON          optional path to also dump the rows as JSON
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ch_signal_extract as csx  # noqa: E402

DEFAULT_BOOK = os.environ.get(
    "HERMES_SHADOW_BOOK_FILE", "/data/.shadow-book.json"
)
MIN_N = int(os.environ.get("CH_MONITOR_MIN_N", "130"))
# Round-trips are still accumulating; do not let a single old book inflate the
# cap the way the 2000-fill audit cap bounds history. Labeled fills only exist
# post-2026-10-20 anyway.


def load_book(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def closed_labeled_round_trips(book: dict) -> list[dict]:
    """Inner-join labeled OPEN fills to their CLOSE on position_id per account.

    Returns one row per closed labeled round trip::
      {account, position_id, coin, side, channel, band, composite, fired,
       roe_pct(close realized leveraged ROE), win, reason, hold_minutes,
       opened_ts, closed_ts, entry_px, exit_px}
    Opens that never closed are skipped (right-censored) and reported by the
    caller via :func:`census`.
    """
    rows: list[dict] = []
    for account, acc in book.get("accounts", {}).items():
        fills = acc.get("fills", [])
        closes_by_pid: dict[str, dict] = {}
        for f in fills:
            if f.get("type") == "close":
                # One close per position_id; keep the first if ever duplicated.
                closes_by_pid.setdefault(f.get("position_id"), f)
        for f in fills:
            if f.get("type") != "open":
                continue
            fired = f.get("fired_names")
            if not fired:
                continue  # pre-instrumentation fill: excluded, not guessed
            close = closes_by_pid.get(f.get("position_id"))
            if close is None:
                continue  # still open -> right-censored
            composite = f.get("composite_score")
            try:
                comp_f = float(composite) if composite is not None else None
            except (TypeError, ValueError):
                comp_f = None
            roe = float(close.get("realized_pnl_pct", 0.0))
            rows.append({
                "account": account,
                "position_id": f.get("position_id"),
                "coin": f.get("coin"),
                "side": f.get("side"),
                "channel": csx.classify(set(fired), comp_f if comp_f is not None else csx.GATE - 1),
                "band": csx.composite_band(comp_f),
                "composite": comp_f,
                "fired": list(fired),
                "roe_pct": roe,
                "win": roe > 0.0,
                "reason": close.get("reason", "") or "",
                "hold_minutes": float(close.get("hold_minutes", 0.0) or 0.0),
                "opened_ts": f.get("ts"),
                "closed_ts": close.get("ts"),
                "entry_px": f.get("price"),
                "exit_px": close.get("price"),
            })
    return rows


def census(book: dict) -> dict:
    """Count opens/closes/labeled/closed-labeled/censored per account."""
    out: dict[str, dict] = {}
    for account, acc in book.get("accounts", {}).items():
        fills = acc.get("fills", [])
        opens = [f for f in fills if f.get("type") == "open"]
        closes = [f for f in fills if f.get("type") == "close"]
        labeled_pids = {f.get("position_id") for f in opens if f.get("fired_names")}
        closed_pids = {f.get("position_id") for f in closes}
        out[account] = {
            "opens": len(opens),
            "closes": len(closes),
            "labeled_opens": len(labeled_pids),
            "closed_labeled": len(labeled_pids & closed_pids),
            "open_labeled_censored": len(labeled_pids - closed_pids),
        }
    return out


def _stats(vals: list[float]) -> dict:
    n = len(vals)
    if n == 0:
        return {"n": 0}
    s = sorted(vals)
    mean = sum(vals) / n
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0
    return {
        "n": n,
        "mean": round(mean, 3),
        "median": round(median, 3),
        "win_pct": round(100.0 * sum(1 for v in vals if v > 0.0) / n, 1),
    }


def bucket(rows: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        groups[str(r.get(key))].append(r["roe_pct"])
    return {k: _stats(v) for k, v in sorted(groups.items())}


def reason_family(reason: str) -> str:
    """Collapse the verbose DSL reason strings to the P0-A bimodal families."""
    r = reason or ""
    for fam in ("max_loss", "floor_breach", "hard_timeout",
                "stale_flat_timeout", "ignite_timeout", "time_scratch"):
        if r.startswith(fam):
            return fam
    return r.split(" ", 1)[0] if r else "unknown"


def format_table(title: str, buckets: dict[str, dict]) -> list[str]:
    lines = [f"  {title}", f"    {'bucket':<16} {'n':>4} {'mean%':>9} "
                           f"{'median%':>9} {'win%':>6} {'to_n' + str(MIN_N):>7}"]
    if not buckets:
        lines.append("    (none)")
        return lines
    for name, st in buckets.items():
        n = st["n"]
        if n:
            lines.append(
                f"    {name:<16} {n:>4} {st['mean']:>9.3f} {st['median']:>9.3f} "
                f"{st['win_pct']:>6.1f} {max(0, MIN_N - n):>7}")
        else:
            lines.append(f"    {name:<16} {n:>4} {'-':>9} {'-':>9} {'-':>6} "
                         f"{MIN_N:>7}")
    return lines


def main() -> int:
    path = DEFAULT_BOOK
    book = load_book(path)

    print("=" * 84)
    print("REAL shadow fills by admission channel (P0-A labeled round trips)")
    print("=" * 84)
    print(f"book: {path}")
    cen = census(book)
    for account, c in cen.items():
        print(f"  [{account}] opens={c['opens']} closes={c['closes']} "
              f"labeled_opens={c['labeled_opens']} "
              f"closed_labeled={c['closed_labeled']} "
              f"open_labeled(right-censored)={c['open_labeled_censored']}")
    unlabeled_closes = sum(
        c["closes"] - c["closed_labeled"] for c in cen.values())
    print(f"  legacy unlabeled closes (excluded from channel buckets): "
          f"{unlabeled_closes}")
    print(f"  per-bucket sample target (CH_MONITOR_MIN_N): {MIN_N}")
    print()

    rows = closed_labeled_round_trips(book)
    if not rows:
        print("No closed labeled round trips yet — instrumentation went live "
              "2026-10-20; keep SHADOW running and re-run.")
        return 0

    print(f"closed labeled round trips (both accounts): {len(rows)}")
    print()

    for account in sorted({r["account"] for r in rows}):
        sub = [r for r in rows if r["account"] == account]
        print(f"-- account: {account}  (n={len(sub)}) --")
        for line in format_table("by channel", bucket(sub, "channel")):
            print(line)
        for line in format_table("by composite band", bucket(sub, "band")):
            print(line)
        reason_rows = [{**r, "reason_family": reason_family(r["reason"])} for r in sub]
        for line in format_table("by DSL exit reason",
                                 bucket(reason_rows, "reason_family")):
            print(line)
        print()

    # channel x coin-clustered read: one mean ROE per (channel, coin) so a coin
    # that fires many times cannot fake n; mirrors the replay clustering.
    print("coin-clustered channel read (one mean ROE per coin, both accounts "
          "kept separate):")
    for account in sorted({r["account"] for r in rows}):
        per: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for r in rows:
            if r["account"] != account:
                continue
            per[r["channel"]][r["coin"]].append(r["roe_pct"])
        lines = [f"  [{account}] {'channel':<12} {'coins':>6} {'coins>0':>8} "
                 f"{'mean_of_coin_means%':>20}"]
        for ch, coins in sorted(per.items()):
            coin_means = [sum(v) / len(v) for v in coins.values()]
            pos = sum(1 for m in coin_means if m > 0)
            lines.append(
                f"  {'':<0}{ch:<12} {len(coins):>6} {pos:>8} "
                f"{sum(coin_means) / len(coin_means):>20.3f}")
        print("\n".join(lines))
    print()

    need = {ch: max(0, MIN_N - st["n"])
            for ch, st in bucket([r for r in rows if r["account"] == "taker"],
                                  "channel").items() if st.get("n")}
    print("progress (taker): " + (", ".join(f"{ch} need {n} more"
          for ch, n in sorted(need.items())) if need else "no taker rows"))

    out_json = os.environ.get("CH_MONITOR_JSON")
    if out_json:
        with open(out_json, "w") as f:
            json.dump({"census": cen, "rows": rows}, f, indent=2)
        print(f"\nwrote rows -> {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
