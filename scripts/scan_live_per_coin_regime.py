#!/usr/bin/env python3
"""线上真候选管线：从 per_coin_regime_shadow.jsonl 里筛出 runner gate 放行的记录并扫描。

探针前移到 executor 的 runner entry gate 之后，影子日志里混进了大量"被 runner gate
拦下、根本不会成交"的候选。这些记录没有真实入场，EV 分析会被污染。detail.runner_block
就是用来区分的：

    runner_block == ""    → 通过 runner gate 的真候选（可用于 EV 分析）
    runner_block == "..." → 被拦下的候选（只能看方向性）
    字段缺失               → 前移改造之前写入的历史行，一律排除

管线：读线上 shadow（含轮转）→ 过滤 → 写中间文件 → (可选) reconcile 打分 → scan 扫描。
中间文件是独立的一份，绝不原地覆盖线上日志（reconcile --write 会整文件重写）。
已打分的 outcome/pnl_pct 会从上一版中间文件继承，避免每次重跑都重新拉 K 线。

用法:
    # 只看过滤统计，不落盘
    python3 scripts/scan_live_per_coin_regime.py --dry-run

    # 全流程：过滤 + 打分 + 扫描
    python3 scripts/scan_live_per_coin_regime.py --reconcile

    # 已经打过分，只重跑扫描
    python3 scripts/scan_live_per_coin_regime.py
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_RECONCILE = os.path.join(_HERE, "reconcile_per_coin_regime_shadow.py")
_SCAN = os.path.join(_HERE, "scan_per_coin_regime_params.py")

DEFAULT_SRC = "/data/per_coin_regime_shadow.jsonl"
DEFAULT_OUT = "/data/per_coin_regime_live.jsonl"
# shadow_log.py 的轮转是 BACKUP_COUNT = 5。
ROTATIONS = 5


def _source_files(base: str) -> list[str]:
    """按时间正序返回存在的日志文件：.5 .4 .3 .2 .1 base。"""
    out = []
    for i in range(ROTATIONS, 0, -1):
        p = f"{base}.{i}"
        if os.path.exists(p) and os.path.getsize(p) > 0:
            out.append(p)
    if os.path.exists(base):
        out.append(base)
    return out


def _key(rec: dict[str, Any]) -> tuple:
    return (rec.get("trace_id") or "", rec.get("timestamp") or "",
            rec.get("coin") or "", rec.get("side") or "")


def _load(paths: list[str]) -> list[dict[str, Any]]:
    """合并读入，按 key 去重（后出现的覆盖先出现的）。"""
    seen: dict[tuple, dict[str, Any]] = {}
    bad = 0
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                seen[_key(rec)] = rec
    if bad:
        print(f"  (skipped {bad} unparsable lines)")
    return list(seen.values())


def _block_class(rec: dict[str, Any]) -> str:
    rb = (rec.get("detail") or {}).get("runner_block")
    if rb is None:
        return "missing"
    return "pass" if str(rb).strip() == "" else "blocked"


def _carry_outcomes(rows: list[dict[str, Any]], prev_path: str) -> int:
    """把上一版中间文件里已打分的结果搬过来，省掉重复的 K 线拉取。"""
    if not os.path.exists(prev_path):
        return 0
    prev: dict[tuple, dict[str, Any]] = {}
    with open(prev_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("outcome") is not None:
                prev[_key(r)] = r
    n = 0
    for r in rows:
        old = prev.get(_key(r))
        if old is None or r.get("outcome") is not None:
            continue
        for fld in ("outcome", "pnl_pct", "exit_px", "pnl_usd"):
            if fld in old:
                r[fld] = old[fld]
        n += 1
    return n


def _counts(rows: list[dict[str, Any]], field: str) -> str:
    c = collections.Counter(str(r.get(field)) for r in rows)
    return "  ".join(f"{k}={v}" for k, v in c.most_common())


def _blocked_reasons(rows: list[dict[str, Any]], top: int = 8) -> None:
    c = collections.Counter()
    for r in rows:
        rb = str((r.get("detail") or {}).get("runner_block") or "")
        if rb:
            # "confidence 0.55 < 0.62" 这类带数字的原因归一到前两个词
            c[" ".join(rb.split()[:3])] += 1
    for reason, n in c.most_common(top):
        print(f"    {n:5d}  {reason}")


def _run(cmd: list[str]) -> int:
    print("\n$ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=DEFAULT_SRC,
                    help=f"线上 shadow 日志（自动带上 .1~.{ROTATIONS} 轮转），默认 {DEFAULT_SRC}")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"过滤后的中间文件，默认 {DEFAULT_OUT}")
    ap.add_argument("--keep", choices=("pass", "blocked", "all"), default="pass",
                    help="保留哪一类记录：pass=runner_block 为空串（默认）")
    ap.add_argument("--reconcile", action="store_true",
                    help="过滤后先调 reconcile_per_coin_regime_shadow.py --write 打分")
    ap.add_argument("--window-hours", type=float, default=24.0,
                    help="传给 reconcile 的成熟窗口，默认 24")
    ap.add_argument("--no-scan", action="store_true", help="只过滤/打分，不跑扫描")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件不跑下游")
    ap.add_argument("--iters", type=int, default=20000, help="传给 scan 的置换次数")
    ap.add_argument("--seed", type=int, default=20260911, help="传给 scan 的随机种子")
    args = ap.parse_args()

    paths = _source_files(args.src)
    if not paths:
        print(f"no shadow log found at {args.src}(.1~.{ROTATIONS})")
        return 1
    print("sources: " + ", ".join(paths))

    rows = _load(paths)
    by_class = collections.Counter(_block_class(r) for r in rows)
    print(f"\ntotal records: {len(rows)}")
    print(f"  runner_block==\"\"  (真候选) : {by_class['pass']}")
    print(f"  runner_block!=\"\"  (被拦)   : {by_class['blocked']}")
    print(f"  字段缺失 (改造前历史行)     : {by_class['missing']}")
    if by_class["blocked"]:
        print("  被拦原因 top:")
        _blocked_reasons(rows)

    if args.keep == "all":
        kept = [r for r in rows if _block_class(r) != "missing"]
    else:
        kept = [r for r in rows if _block_class(r) == args.keep]
    kept.sort(key=lambda r: str(r.get("timestamp") or ""))

    print(f"\nkept ({args.keep}): {len(kept)}")
    if kept:
        print("  would   : " + _counts(kept, "would"))
        print("  side    : " + _counts(kept, "side"))
        print("  aligned : " + _counts(kept, "macro_aligned"))
        scored = sum(1 for r in kept if r.get("pnl_pct") is not None)
        aligned_scored = sum(1 for r in kept
                             if r.get("macro_aligned") and r.get("pnl_pct") is not None)
        print(f"  已打分  : {scored}   其中 macro_aligned（scan 实际吃到的）: {aligned_scored}")

    if args.dry_run:
        print("\n(dry-run; 未写文件、未跑下游)")
        return 0
    if not kept:
        print("\nnothing to write — 线上还没积累到通过 runner gate 的候选")
        return 1

    carried = _carry_outcomes(kept, args.out)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(kept)} rows -> {args.out}"
          + (f"  (继承 {carried} 条已打分结果)" if carried else ""))

    if args.reconcile:
        rc = _run([sys.executable, _RECONCILE, "--file", args.out,
                   "--window-hours", str(args.window_hours), "--write"])
        if rc != 0:
            print(f"reconcile failed (rc={rc})")
            return rc

    if args.no_scan:
        return 0
    return _run([sys.executable, _SCAN, "--file", args.out,
                 "--iters", str(args.iters), "--seed", str(args.seed)])


if __name__ == "__main__":
    sys.exit(main())
