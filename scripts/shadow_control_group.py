#!/usr/bin/env python3
"""B-4c: registry-driven control comparison for eligible shadow arms.

For each eligible arm, split mature records into SIGNAL-HIT and same-period
NON-HIT baseline sets, matched on macro regime and forward window. Win-rate and
mean-forward-return differences retain their bootstrap 95% CIs. Ineligible arms
are explicit ``not_eligible`` and never fabricated as zero samples.
``weight_shadow`` remains as superseded history; candidate selection reuses the
candidate gate's existing ``min_composite`` instead of creating weight variants.

Read-only: never writes config or places orders. Reads live JSONL and rotated
siblings. The arm registry is ``shadow_progress.ARMS``.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
from typing import Any

import shadow_grade as sg
import shadow_progress as sp
from hermes_trader.agents import candidate_gate_shadow as candidate_gate

_CANDIDATE_PATH = os.environ.get(
    "HERMES_CANDIDATE_GATE_SHADOW_FILE",
    os.path.join(sp.WRITABLE_DATA, "candidate_gate_shadow.jsonl"))
_CANDIDATE_MIN_COMPOSITE = float(getattr(
    candidate_gate, "DEFAULT_COMPOSITE_MIN", 40.0))
_SUPPLEMENTAL_ARMS = ("sizing_v2", "candidate_gate_shadow", "spread_observe")
_REGISTERED_ARMS = tuple(sp.ARMS) if isinstance(sp.ARMS, dict) else tuple(
    entry[0] for entry in sp.ARMS)
CONTROL_GROUP = tuple(dict.fromkeys(
    [*_REGISTERED_ARMS, *_SUPPLEMENTAL_ARMS, "weight_shadow"]))


def _arm_files(path: str) -> list[str]:
    files = {path}
    files.update(glob.glob(path + ".[0-9]*"))
    return sorted(files)


def _load(path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in _arm_files(path):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except (FileNotFoundError, OSError):
            pass
    return out


def _fwd72(rec: dict[str, Any]) -> float | None:
    fwd = rec.get("forward")
    if isinstance(fwd, dict) and fwd.get("fwd72h_pct") is not None:
        try:
            return float(fwd["fwd72h_pct"])
        except (TypeError, ValueError):
            return None
    if rec.get("fwd72_return") is not None:
        try:
            return float(rec["fwd72_return"])
        except (TypeError, ValueError):
            return None
    if rec.get("pnl_pct") is not None:
        try:
            return float(rec["pnl_pct"])
        except (TypeError, ValueError):
            return None
    return None


def _hit_flag(arm: str, rec: dict[str, Any]) -> bool | None:
    """Reuse the grader's registered hit semantics, including bespoke arms."""
    if arm == "candidate_gate_shadow":
        if rec.get("shadow") is not True:
            return None
        try:
            band_lo = float(rec.get("band_lo"))
        except (TypeError, ValueError):
            return None
        return True if band_lo == _CANDIDATE_MIN_COMPOSITE else None
    kind = sg.ARM_KIND.get(arm)
    if kind not in ("block", "change", "signal", "rollout"):
        return None
    return sg._hit_field(rec, kind, arm)


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


def _agent_config() -> dict[str, Any]:
    try:
        from hermes_trader.agents.config_store import read_agent_config
        return read_agent_config() or {}
    except Exception:
        return {}


def _arm_specs() -> list[dict[str, Any]]:
    specs = []
    cfg = _agent_config()
    if isinstance(sp.ARMS, dict):
        registered = [(label, values[0], values[1])
                      for label, values in sp.ARMS.items()]
    else:
        registered = []
        for label, blk_name, env_file, default_name, path_key, mode_key in sp.ARMS:
            registered.append((
                label,
                sp._arm_mode(cfg, blk_name, env_file, label=label,
                             mode_key=mode_key),
                sp._arm_path(cfg, blk_name, env_file, default_name,
                             path_key=path_key, label=label),
            ))
    for label, mode, path in registered:
        specs.append({"arm": label, "mode": mode, "path": path,
                      "registered": True})
    known = {spec["arm"] for spec in specs}
    for label in _SUPPLEMENTAL_ARMS:
        if label in known:
            continue
        if label == "candidate_gate_shadow":
            specs.append({"arm": label, "mode": "shadow",
                          "path": _CANDIDATE_PATH, "registered": False})
        elif label == "sizing_v2":
            specs.append({
                "arm": label, "registered": False,
                "mode": sp._arm_mode(
                    cfg, "atr_risk_sizing", "HERMES_SIZING_V2_SHADOW_FILE",
                    mode_key="sizing_v2_mode"),
                "path": sp._arm_path(
                    cfg, "atr_risk_sizing", "HERMES_SIZING_V2_SHADOW_FILE",
                    "sizing_v2_shadow.jsonl",
                    path_key="sizing_v2_shadow_log_path", label=label),
            })
        else:
            specs.append({
                "arm": label, "mode": "observe", "registered": False,
                "path": None,
                "eligibility_block": "observation_only_no_hit_decision",
            })
    specs.append({"arm": "weight_shadow", "mode": "superseded",
                  "path": None, "registered": False})
    return specs


def _summarize_arm(spec: dict[str, Any], n_boot: int, seed: int) -> dict[str, Any]:
    arm = spec["arm"]
    row: dict[str, Any] = {
        "arm": arm, "path": spec["path"], "mode": spec["mode"],
        "registered": spec["registered"], "status": "ok",
        "eligibility_block": None, "hit_n": None, "baseline_n": None,
        "hit": [], "baseline": [], "regimes": {},
    }
    if arm == "weight_shadow":
        row.update({
            "status": "superseded",
            "eligibility_block": "superseded_by_candidate_gate_shadow",
            "superseded_by": "candidate_gate_shadow",
            "min_composite": _CANDIDATE_MIN_COMPOSITE,
            "generates_candidates": False,
            "history": "retained",
        })
        return row
    if spec.get("eligibility_block"):
        row.update({"status": "not_eligible",
                    "eligibility_block": spec["eligibility_block"]})
        return row
    if spec["mode"] == "off":
        row.update({"status": "not_eligible", "eligibility_block": "mode_off"})
        return row
    kind = "signal" if arm == "candidate_gate_shadow" else sg.ARM_KIND.get(arm)
    if kind not in ("block", "change", "signal", "rollout"):
        row.update({"status": "not_eligible",
                    "eligibility_block": "hit_semantics_unregistered"})
        return row
    if arm == "candidate_gate_shadow":
        row["min_composite"] = _CANDIDATE_MIN_COMPOSITE
        row["skipped_n"] = 0

    hit_v: list[float] = []
    base_v: list[float] = []
    hit_reg: dict[str, list[float]] = {}
    base_reg: dict[str, list[float]] = {}
    for rec in _load(spec["path"]):
        value = _fwd72(rec)
        flag = _hit_flag(arm, rec)
        if arm == "candidate_gate_shadow" and (value is None or flag is not True):
            row["skipped_n"] += 1
            continue
        if value is None:
            continue
        regime = str(rec.get("macro_regime") or "?")
        if flag is True:
            hit_v.append(value)
            hit_reg.setdefault(regime, []).append(value)
        elif flag is False:
            base_v.append(value)
            base_reg.setdefault(regime, []).append(value)
    row["hit_n"] = len(hit_v)
    row["baseline_n"] = len(base_v)
    row["hit"] = hit_v
    row["baseline"] = base_v
    if arm == "candidate_gate_shadow" and not base_v:
        row.update({"status": "no_data", "reason": "baseline_unavailable"})
        return row
    if not hit_v or not base_v:
        row.update({"status": "no_data", "reason": "hit_or_baseline_empty"})
        return row

    wr_h, wr_b = _wr(hit_v), _wr(base_v)
    m_h, m_b = _mean(hit_v), _mean(base_v)
    wr_lo, wr_hi = _boot_ci(hit_v, base_v, _wr, n_boot, seed)
    m_lo, m_hi = _boot_ci(hit_v, base_v, _mean, n_boot, seed)
    row.update({
        "hit_wr": wr_h, "baseline_wr": wr_b,
        "wr_delta": wr_h - wr_b, "wr_ci": [wr_lo, wr_hi],
        "hit_mean": m_h, "baseline_mean": m_b,
        "mean_delta": m_h - m_b, "mean_ci": [m_lo, m_hi],
    })
    for regime in sorted(set(hit_reg) | set(base_reg)):
        h = hit_reg.get(regime, [])
        b = base_reg.get(regime, [])
        row["regimes"][regime] = {
            "hit_n": len(h), "baseline_n": len(b),
            "hit_wr": _wr(h) if h else None,
            "baseline_wr": _wr(b) if b else None,
            "hit_mean": _mean(h) if h else None,
            "baseline_mean": _mean(b) if b else None,
        }
    return row


def evaluate_control_group(n_boot: int = 2000, seed: int = 7) -> dict[str, Any]:
    specs = _arm_specs()
    return {
        "registry_source": "shadow_progress.ARMS",
        "registered_arm_count": len(sp.ARMS),
        "covered_registered_arms": [s["arm"] for s in specs if s["registered"]],
        "arms": [_summarize_arm(spec, n_boot, seed) for spec in specs],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = evaluate_control_group(args.boot, args.seed)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0

    print("=" * 92)
    print("B-4c 对照组：命中集 vs 同期未命中基线（72h forward，bootstrap 95% CI）")
    print("=" * 92)
    for row in report["arms"]:
        print(f"\n### {row['arm']}   status={row['status']}")
        if row["status"] in ("not_eligible", "superseded"):
            print(f"  {row['eligibility_block']}；不伪造统计样本。")
            continue
        print(f"  mature: hit={row['hit_n']} baseline={row['baseline_n']}")
        if row["status"] == "no_data":
            print("  命中集或基线为空，无法比较。")
            continue
        print(f"  胜率   命中 {row['hit_wr']:6.1%} | 基线 {row['baseline_wr']:6.1%} | "
              f"差值 {row['wr_delta']:+.1%}  95%CI "
              f"[{row['wr_ci'][0]:+.1%}, {row['wr_ci'][1]:+.1%}]")
        print(f"  均收益 命中 {row['hit_mean']:+6.2f}% | 基线 "
              f"{row['baseline_mean']:+6.2f}% | 差值 {row['mean_delta']:+.2f}% "
              f"95%CI [{row['mean_ci'][0]:+.2f}%, {row['mean_ci'][1]:+.2f}%]")
        for regime, stats in row["regimes"].items():
            print(f"    [{regime:14s}] hit n={stats['hit_n']:2d} "
                  f"base n={stats['baseline_n']:2d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
