#!/usr/bin/env python3
"""B-4c: control-group comparison for SHADOW signal/block arms.

For each arm, split mature records into the SIGNAL-HIT set (the arm's condition
fired) and a same-period NON-HIT baseline (the probe evaluated a decision but the
condition did not fire), matched on macro regime and forward window. Reports the
win-rate difference and mean-forward-return difference with a 95% bootstrap
confidence interval, so a low raw hit-set WR can be judged against what the rest
of the market did over the same period (otherwise "0% WR" is uninterpretable).

Read-only: never writes config or places orders. Reads the live JSONL and its
size-rotated siblings.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
from typing import Any


def _arm_files(path: str) -> list[str]:
    files = {path}
    base = path
    files.update(glob.glob(base + ".[0-9]*"))
    return sorted(files)


def _load(path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in _arm_files(path):
        try:
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except FileNotFoundError:
            pass
    return out


def _fwd72(rec: dict[str, Any]) -> float | None:
    fwd = rec.get("forward")
    if isinstance(fwd, dict) and fwd.get("fwd72h_pct") is not None:
        try:
            return float(fwd["fwd72h_pct"])
        except (TypeError, ValueError):
            return None
    if rec.get("pnl_pct") is not None:
        try:
            return float(rec["pnl_pct"])
        except (TypeError, ValueError):
            return None
    return None


def _hit_flag(arm: str, rec: dict[str, Any]) -> bool | None:
    """True = arm condition fired; False = observed non-hit; None = n/a."""
    if arm == "xs_reversal":
        return bool(rec.get("is_candidate"))
    if arm == "daily_extension_cap":
        return bool(rec.get("ext_would_block"))
    return None


def _wr(vals: list[float]) -> float:
    return sum(1 for v in vals if v > 0) / len(vals) if vals else float("nan")


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else float("nan")


def _boot_ci(hit: list[float], base: list[float], stat, n_boot: int,
             seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(n_boot):
        h = [hit[rng.randrange(len(hit))] for _ in hit]
        b = [base[rng.randrange(len(base))] for _ in base]
        deltas.append(stat(h) - stat(b))
    deltas.sort()
    lo = deltas[int(0.025 * n_boot)]
    hi = deltas[int(0.975 * n_boot)]
    return lo, hi


ARMS = {
    "xs_reversal": "/data/xs_reversal_shadow.jsonl",
    "daily_extension_cap": "/data/daily_extension_cap_shadow.jsonl",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    print("=" * 92)
    print("B-4c 对照组：命中集 vs 同期未命中基线（72h forward，bootstrap 95% CI）")
    print("=" * 92)

    for arm, path in ARMS.items():
        recs = _load(path)
        hit_v: list[float] = []
        base_v: list[float] = []
        hit_reg: dict[str, list[float]] = {}
        base_reg: dict[str, list[float]] = {}
        for r in recs:
            v = _fwd72(r)
            if v is None:
                continue
            flag = _hit_flag(arm, r)
            reg = str(r.get("macro_regime") or "?")
            if flag is True:
                hit_v.append(v)
                hit_reg.setdefault(reg, []).append(v)
            elif flag is False:
                base_v.append(v)
                base_reg.setdefault(reg, []).append(v)

        print(f"\n### {arm}   (mature: hit={len(hit_v)} baseline={len(base_v)})")
        if not hit_v or not base_v:
            print("  命中集或基线为空，无法比较。")
            continue
        wr_h, wr_b = _wr(hit_v), _wr(base_v)
        m_h, m_b = _mean(hit_v), _mean(base_v)
        wr_lo, wr_hi = _boot_ci(hit_v, base_v, _wr, args.boot, args.seed)
        m_lo, m_hi = _boot_ci(hit_v, base_v, _mean, args.boot, args.seed)
        print(f"  胜率   命中 {wr_h:6.1%} | 基线 {wr_b:6.1%} | "
              f"差值 {wr_h - wr_b:+.1%}  95%CI [{wr_lo:+.1%}, {wr_hi:+.1%}]")
        print(f"  均收益 命中 {m_h:+6.2f}% | 基线 {m_b:+6.2f}% | "
              f"差值 {m_h - m_b:+.2f}%  95%CI [{m_lo:+.2f}%, {m_hi:+.2f}%]")
        for reg in sorted(set(hit_reg) | set(base_reg)):
            h = hit_reg.get(reg, [])
            b = base_reg.get(reg, [])
            print(f"    [{reg:14s}] hit n={len(h):2d} WR={_wr(h):5.0%} "
                  f"mean={_mean(h):+6.2f}% | base n={len(b):2d} "
                  f"WR={_wr(b):5.0%} mean={_mean(b):+6.2f}%")
        print("  判读：胜率/均收益差值的 CI 完全 < 0 且不含 0 → 臂有害(删除候选)；"
              "CI 含 0 → 证据不足继续观察。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
