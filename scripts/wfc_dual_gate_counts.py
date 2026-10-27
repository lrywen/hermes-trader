#!/usr/bin/env python3
"""WF-C：runner 末端 min_composite 40 vs 45 双门 record-only 计数（只读）。

executor._record_runner_gate_verdict 现在在每条 runner-gate 裁决上额外落盘
`passes40` / `passes45`（不改任何生产门）。本脚本聚合 runner_gate_shadow.jsonl，
回答 ch_weight_calibration 留下的问题：现 45 门相对 40 门，多挡了多少、挡掉的是
什么（admitted/blocked、各 rule、score 分布），为是否提"40 vs 45"ADR 攒证据。

纯离线、只读；找不到文件时给出明确空结果而非报错。

用法：
  python scripts/wfc_dual_gate_counts.py
  python scripts/wfc_dual_gate_counts.py --file /data/runner_gate_shadow.jsonl --days 28
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
from collections import Counter
from typing import Optional


def _find_default() -> str:
    for p in (
        os.path.expanduser("~/.hermes-trading/runner_gate_shadow.jsonl"),
        "/data/runner_gate_shadow.jsonl",
    ):
        if os.path.exists(p):
            return p
    return os.path.expanduser("~/.hermes-trading/runner_gate_shadow.jsonl")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=None)
    ap.add_argument("--days", type=float, default=None,
                    help="只统计最近 N 天（默认全部）")
    args = ap.parse_args(argv)
    path = args.file or _find_default()

    if not os.path.exists(path):
        print(json.dumps({"file": path, "rows": 0,
                          "note": "no runner_gate_shadow.jsonl yet; counters are "
                                  "record-only and populate once the live/shadow "
                                  "loop emits verdicts"}, ensure_ascii=False, indent=2))
        return 0

    cutoff = None
    if args.days:
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=args.days)).timestamp() * 1000

    n = p40 = p45 = 0
    c40_only_admitted = c45_blocked_but40 = 0
    admit40 = admit45 = 0
    blocked_rules_40only = Counter()
    scores = []
    by_side = Counter()
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff is not None and float(r.get("ts") or 0) < cutoff:
                continue
            if r.get("passes40") is None:
                continue  # 旧账本无新字段
            n += 1
            by_side[r.get("side", "?")] += 1
            s = r.get("composite_score")
            if isinstance(s, (int, float)):
                scores.append(float(s))
            if r["passes40"]:
                p40 += 1
                if r.get("admitted"):
                    admit40 += 1
            if r["passes45"]:
                p45 += 1
                if r.get("admitted"):
                    admit45 += 1
            # 关键反事实：过 40 但不过 45
            if r["passes40"] and not r["passes45"]:
                c45_blocked_but40 += 1
                blocked_rules_40only[r.get("rule", "") or "(admitted)"] += 1
                if r.get("admitted"):
                    c40_only_admitted += 1

    out = {
        "file": path,
        "window_days": args.days,
        "verdicts_with_dual_counter": n,
        "by_side": dict(by_side),
        "passes40": p40,
        "passes45": p45,
        "passes40_but_not45": c45_blocked_but40,
        "of_which_actually_admitted": c40_only_admitted,
        "admitted_among_passes40": admit40,
        "admitted_among_passes45": admit45,
        "score_median": round(statistics.median(scores), 2) if scores else None,
        "rules_blocking_40only": dict(blocked_rules_40only.most_common(12)),
        "note": ("40-vs-45 is RECORD-ONLY; do not change min_composite until an "
                 "AI-gated A/B over enough verdicts shows the [40,45) band stays "
                 "positive end-to-end"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
