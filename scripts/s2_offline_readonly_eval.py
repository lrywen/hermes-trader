#!/usr/bin/env python3
"""Offline, read-only S2-1/2/3/4 evaluation.

Reads session-log candidates and frozen native Hyperliquid candles.  It never
fills absent flow/liquidity with zero and never starts an S3 arm.  The only
output is the JSON path explicitly supplied by the caller (use /tmp).
"""
from __future__ import annotations

import argparse
import bisect
import copy
import gzip
import importlib.util
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

APP_ROOT = Path(os.environ.get("HERMES_APP_ROOT", "/app" if Path("/app/hermes_trader").exists() else str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "scripts"))

from hermes_trader.agents.config import trigger_thresholds_params, trigger_weights_params
from hermes_trader.agents.market_regime import classify_candles
from hermes_trader.indicators.triggers import build_hits, composite_score, lower_highs_1h
from hermes_trader.models.types import Candle

_spec = importlib.util.spec_from_file_location("s0_reconcile_readonly", APP_ROOT / "scripts" / "reconcile_decision_shadow.py")
if _spec is None or _spec.loader is None:
    raise ImportError("S0 reconcile_decision_shadow.py is required")
s0 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s0)

OLD_SIX = ("pctMoveSpike", "volumeSpike", "breakout", "trendStrength", "higherLows1h", "lowerHighs1h")
FORWARD_BARS = 48  # S0 reconcile 4h horizon; entry at next 5m open.
S0_HORIZON = "4h"
BAR_5M = 300_000
COSTS_BP = (3.0, 9.26)
PREREG = {
    "paired_n_min": 1000,
    "coin_days_min": 30,
    "pair_coverage_min": 0.90,
    "agreement_wilson95_lower_min": 0.95,
    "breakout_live_fired_min": 30,
    "breakout_precision_min": 0.90,
    "breakout_recall_min": 0.90,
    "direction_gross_median_min_exclusive": 0.0,
    "direction_gross_block_ci95_lower_min_exclusive": 9.26,
    "s0_horizon": S0_HORIZON,
    "bootstrap_n": 2000,
    "seed": 20261104,
    # S3 relaxed gate (user-approved 2026-11-05): SHORT gross median > -20bp
    # and CI upper > 0 is sufficient to start SHADOW S3; strict pass/fail
    # gates above remain reported but no longer block S3 shadow-only entry.
    "s3_relaxed_short_median_min_exclusive": -20.0,
    "s3_relaxed_short_ci95_upper_min_exclusive": 0.0,
    "s3_gate_version": "relaxed_v1_2026_11_05",
}


def finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def side_sign(side: str) -> int:
    # Exact S0 reconcile convention (short=-1, all other sides=+1).
    return -1 if str(side).lower() == "short" else 1


def directional_bp(entry: float, exit: float, side: str) -> float:
    if not finite(entry) or not finite(exit) or entry <= 0:
        raise ValueError("entry/exit must be finite and entry must be positive")
    return side_sign(side) * (float(exit) - float(entry)) / float(entry) * 10_000.0


def apply_cost_once(gross_bp: float, cost_bp: float) -> float:
    return float(gross_bp) - float(cost_bp)


def wilson_interval(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = successes / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def volume_tier(volume: Any) -> str:
    # Reuse S0's current static benchmark tier baseline; missing is not micro.
    if not finite(volume) or float(volume) < 0:
        return "unknown"
    return s0.map_bench_tier(float(volume)) or "unknown"


def liquidity_stratum(volume: Any) -> str:
    if not finite(volume) or float(volume) < 0:
        return "unknown"
    v = float(volume)
    if v >= 50_000_000:
        return "gte_50m"
    if v >= 5_000_000:
        return "5m_to_50m"
    return "lt_5m"


def flow_state(record: dict[str, Any], live_breakout_hit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the original flow fields; never coerce missing raw to zero."""
    available = record.get("flow_data_available", None)
    raw = record.get("flow_confirm_raw", None)
    live_flow_fired = (live_breakout_hit or {}).get("flow_confirmed", None)
    if available is False and raw is None:
        if live_flow_fired is True:
            return {"status": "missing", "raw": None, "reason": "contradiction_unavailable_but_flow_confirmed"}
        return {"status": "ordinary", "raw": None, "reason": "available_false_both_sides_none"}
    if available is True:
        if not finite(raw) or not -1.0 <= float(raw) <= 1.0:
            return {"status": "missing", "raw": None, "reason": "available_true_raw_missing_or_invalid"}
        raw = float(raw)
        side = str(record.get("side", "")).lower()
        directional_raw = -raw if side == "short" else raw
        if live_flow_fired is True and directional_raw < 0.7:
            return {"status": "missing", "raw": None, "reason": "contradiction_directional_flow_confirmed_below_0_7"}
        return {"status": "full_stack", "raw": raw, "reason": "original_finite_raw"}
    if available is False and raw is not None:
        return {"status": "missing", "raw": None, "reason": "contradiction_unavailable_with_raw"}
    return {"status": "missing", "raw": None, "reason": "availability_or_raw_fields_absent"}


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return float(numerator) / float(denominator) if denominator else None


def strict_fidelity_verdict(m: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "paired_n": {"value": m.get("paired_n", 0), "threshold": PREREG["paired_n_min"], "pass": m.get("paired_n", 0) >= PREREG["paired_n_min"]},
        "coin_days": {"value": m.get("coin_days", 0), "threshold": PREREG["coin_days_min"], "pass": m.get("coin_days", 0) >= PREREG["coin_days_min"]},
        "pair_coverage": {"value": m.get("pair_coverage"), "threshold": PREREG["pair_coverage_min"], "pass": m.get("pair_coverage") is not None and m["pair_coverage"] >= PREREG["pair_coverage_min"]},
        "agreement_wilson95_lower": {"value": m.get("agreement_wilson95_lower"), "threshold": PREREG["agreement_wilson95_lower_min"], "pass": m.get("agreement_wilson95_lower") is not None and m["agreement_wilson95_lower"] >= PREREG["agreement_wilson95_lower_min"]},
        "breakout_live_fired_n": {"value": m.get("breakout_live_fired_n", 0), "threshold": PREREG["breakout_live_fired_min"], "pass": m.get("breakout_live_fired_n", 0) >= PREREG["breakout_live_fired_min"]},
        "breakout_precision": {"value": m.get("breakout_precision"), "threshold": PREREG["breakout_precision_min"], "pass": m.get("breakout_precision") is not None and m["breakout_precision"] >= PREREG["breakout_precision_min"]},
        "breakout_recall": {"value": m.get("breakout_recall"), "threshold": PREREG["breakout_recall_min"], "pass": m.get("breakout_recall") is not None and m["breakout_recall"] >= PREREG["breakout_recall_min"]},
    }
    insufficient = {"paired_n", "coin_days", "pair_coverage", "breakout_live_fired_n"}
    hard_failed = [k for k, v in checks.items() if not v["pass"] and k not in insufficient]
    insufficient_failed = [k for k, v in checks.items() if not v["pass"] and k in insufficient]
    # Null precision/recall with too few live positives is insufficient data,
    # not evidence of a measured precision/recall failure.
    if m.get("breakout_live_fired_n", 0) < PREREG["breakout_live_fired_min"]:
        hard_failed = [k for k in hard_failed if k not in {"breakout_precision", "breakout_recall"}]
        for k in ("breakout_precision", "breakout_recall"):
            if not checks[k]["pass"] and k not in insufficient_failed:
                insufficient_failed.append(k)
    status = "pass" if all(v["pass"] for v in checks.values()) else ("inconclusive" if insufficient_failed else "fail")
    return {"status": status, "checks": checks, "failed_checks": [k for k, v in checks.items() if not v["pass"]], "hard_failures": hard_failed, "insufficient_data_checks": insufficient_failed}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    den = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    return sum(x * y for x, y in zip(dx, dy)) / den if den > 0 else None


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and xs[order[j]] == xs[order[i]]:
            j += 1
        rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            out[order[k]] = rank
        i = j
    return out


def hit_ic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n_fired = sum(bool(r["fired"]) for r in rows)
    n_unfired = len(rows) - n_fired
    base = {"n": len(rows), "n_fired": n_fired, "n_unfired": n_unfired, "min_n": 30}
    if len(rows) < 30 or not n_fired or not n_unfired:
        return {**base, "status": "insufficient", "pearson_ic": None, "spearman_ic": None, "fired_minus_unfired_bp": None}
    xs = [1.0 if r["fired"] else 0.0 for r in rows]
    ys = [float(r["gross_bp"]) for r in rows]
    fired = [y for y, x in zip(ys, xs) if x]
    unfired = [y for y, x in zip(ys, xs) if not x]
    return {**base, "status": "estimated", "pearson_ic": _pearson(xs, ys), "spearman_ic": _pearson(_ranks(xs), _ranks(ys)), "fired_minus_unfired_bp": statistics.fmean(fired) - statistics.fmean(unfired)}


def summarize_returns(rows: list[dict[str, Any]], seed: int = PREREG["seed"]) -> dict[str, Any]:
    valid = [r for r in rows if finite(r.get("gross_bp"))]
    gross = [float(r["gross_bp"]) for r in valid]
    out: dict[str, Any] = {"n": len(gross), "slippage_bps": None, "slippage_status": "unknown"}
    if not gross:
        return {**out, "coin_days": 0, "gross_median_bp": None, "gross_mean_bp": None, "gross_block_ci95_bp": [None, None], "net_3_mean_bp": None, "net_9_26_mean_bp": None, "net_9_26_ci95_bp": [None, None], "direction_gate": "inconclusive"}
    coin_days = len({(str(r.get("coin") or ""), str(r["day"])) for r in valid if r.get("coin")})
    days = [datetime.strptime(str(r["day"]), "%Y-%m-%d").date().toordinal() for r in valid]
    # Call the current S0 day-block estimator with decimal returns.  S0 returns
    # percent; convert percent to bp exactly once (x100).
    boot = s0.block_ci([x / 10_000.0 for x in gross], days, hz=S0_HORIZON, B=PREREG["bootstrap_n"], seed=seed)
    raw_ci = boot.get("ci95_pct")
    ci = [None, None] if not raw_ci or raw_ci[0] is None or boot.get("blocks", 0) < 2 else [float(raw_ci[0]) * 100.0, float(raw_ci[1]) * 100.0]
    gross_median = statistics.median(gross)
    gross_mean = statistics.fmean(gross)
    out.update({
        "coin_days": coin_days,
        "gross_median_bp": gross_median,
        "gross_mean_bp": gross_mean,
        "gross_block_mean_bp": None if boot["mean_pct"] is None else float(boot["mean_pct"]) * 100.0,
        "gross_block_ci95_bp": ci,
        "block_bootstrap": {"B": PREREG["bootstrap_n"], "blocks": boot["blocks"], "degenerate": boot.get("degenerate", False) or boot.get("blocks", 0) < 2, "method": "S0 reconcile block_ci, horizon_blocks, median-of-day-block-means"},
        "unique_days": len(set(days)),
        "net_3_mean_bp": apply_cost_once(gross_mean, 3.0),
        "net_9_26_mean_bp": apply_cost_once(gross_mean, 9.26),
        "net_3_median_bp": apply_cost_once(gross_median, 3.0),
        "net_9_26_median_bp": apply_cost_once(gross_median, 9.26),
        "net_9_26_ci95_bp": [None if ci[0] is None else apply_cost_once(ci[0], 9.26), None if ci[1] is None else apply_cost_once(ci[1], 9.26)],
    })
    if ci[0] is None:
        out["direction_gate"] = "inconclusive"
    else:
        positive = gross_median > 0 and ci[0] > 9.26
        out["direction_gate"] = "pass" if positive else "fail"
    return out


class NativeCandles:
    def __init__(self, paths: list[Path]):
        self.maps: list[tuple[str, dict[str, Any]]] = []
        self._series: dict[tuple[str, str], list[Candle]] = {}
        self._volume_24h: dict[tuple[str, int], float | None] = {}
        self.selected_sources: Counter[str] = Counter()
        self.invalid_bars: Counter[str] = Counter()
        for path in paths:
            if not path.is_file():
                continue
            with path.open("r") as f:
                payload = json.load(f)
            if isinstance(payload.get("series"), dict):
                payload = payload["series"]
            self.maps.append((path.name, payload))

    def series(self, coin: str, interval: str) -> list[Candle]:
        key = (coin, interval)
        if key in self._series:
            return self._series[key]
        raw_key = f"{coin}|{interval}"
        selected: dict[int, Candle] = {}
        for label, mapping in self.maps:
            raw = mapping.get(raw_key)
            if not raw:
                continue
            contributed = False
            for ts, v in raw.items():
                try:
                    if isinstance(v, dict):
                        vals = [float(v[k]) for k in ("o", "h", "l", "c", "v")]
                    elif isinstance(v, (list, tuple)) and len(v) >= 5:
                        vals = [float(x) for x in v[:5]]
                    else:
                        raise ValueError("unsupported OHLCV shape")
                    t = int(ts)
                    if not all(math.isfinite(x) for x in vals) or vals[0] <= 0 or vals[3] <= 0 or vals[4] < 0 or vals[1] < max(vals[0], vals[3]) or vals[2] > min(vals[0], vals[3]) or vals[1] < vals[2]:
                        raise ValueError("invalid OHLCV")
                    if t not in selected:
                        selected[t] = Candle(t=t, o=vals[0], h=vals[1], l=vals[2], c=vals[3], v=vals[4])
                        contributed = True
                except (KeyError, TypeError, ValueError):
                    self.invalid_bars[raw_key] += 1
            if contributed:
                self.selected_sources[label] += 1
        bars = [selected[t] for t in sorted(selected)]
        self._series[key] = bars
        return bars

    def volume_24h_usd(self, coin: str, scored_open: int) -> float | None:
        """Rolling-24h close x base-volume over 288 contiguous closed 5m bars."""
        key = (coin, int(scored_open))
        if key in self._volume_24h:
            return self._volume_24h[key]
        series = self.series(coin, "5m")
        end = bisect.bisect_right([c.t for c in series], int(scored_open))
        value: float | None = None
        if end >= 288 and series[end - 1].t == int(scored_open):
            window = series[end - 288:end]
            if all(c.t == int(scored_open) - (287 - i) * BAR_5M for i, c in enumerate(window)):
                notionals = [float(c.c) * float(c.v) for c in window]
                if all(finite(v) for v in notionals):
                    value = float(sum(notionals))
        self._volume_24h[key] = value
        return value


def _history(series: list[Candle], end_close_ms: int, interval_ms: int, count: int, exact_last_open: int | None = None) -> list[Candle]:
    if not series:
        return []
    ts = [c.t for c in series]
    last_open = exact_last_open if exact_last_open is not None else end_close_ms - interval_ms
    idx = bisect.bisect_right(ts, last_open) - 1
    if idx < 0 or (exact_last_open is not None and (idx < 0 or ts[idx] != exact_last_open)):
        return []
    out = series[max(0, idx - count + 1):idx + 1]
    # Trim to the latest contiguous raw-bar segment; never synthesize gaps.
    cut = 0
    for i in range(len(out) - 1, 0, -1):
        if out[i].t - out[i - 1].t != interval_ms:
            cut = i
            break
    return out[cut:]


def aggregate_15m(candles_5m: list[Candle], scored_close_ms: int) -> list[Candle]:
    groups: dict[int, list[Candle]] = defaultdict(list)
    for c in candles_5m:
        groups[c.t // 900_000 * 900_000].append(c)
    out = []
    for start in sorted(groups):
        if start + 900_000 > scored_close_ms:
            continue
        bars = sorted(groups[start], key=lambda c: c.t)
        if [c.t for c in bars] != [start, start + 300_000, start + 600_000]:
            continue
        out.append(Candle(t=start, o=bars[0].o, h=max(c.h for c in bars), l=min(c.l for c in bars), c=bars[-1].c, v=sum(c.v for c in bars)))
    return _history(out, scored_close_ms, 900_000, 100)


def iter_jsonl(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths, key=lambda p: p.name):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", errors="ignore") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(row, dict):
                        yield row
        except (OSError, EOFError):
            continue


def scored_bar_open(record: dict[str, Any]) -> int | None:
    """Convert decision evidence to the last fully closed native 5m bar open."""
    if finite(record.get("bar_close_ms")) and float(record["bar_close_ms"]) >= BAR_5M:
        return int(record["bar_close_ms"]) - BAR_5M
    if finite(record.get("ts")) and float(record["ts"]) >= BAR_5M:
        return int(record["ts"]) // BAR_5M * BAR_5M - BAR_5M
    return None


def _candidate_side(record: dict[str, Any]) -> str | None:
    event = record.get("event")
    if event == "research" and str(record.get("verdict", "")).upper() in {"LONG", "SHORT"}:
        return str(record["verdict"]).lower()
    if event == "execute" and str(record.get("side", "")).lower() in {"long", "short"}:
        return str(record["side"]).lower()
    if event == "ta_skip" and record.get("signal") == "LONG_ONLY_SKIP_SHORT":
        return "short"
    return None


def load_session_candidates(data_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Actual side-specific decisions only; directionless near-miss is audit-only."""
    paths = [p for p in data_dir.glob("session-log.jsonl*") if p.is_file()]
    unique: dict[tuple[str, str, int], dict[str, Any]] = {}
    stats: Counter[str] = Counter()
    for r in iter_jsonl(paths):
        if r.get("event") == "near_miss":
            stats["near_miss_records_disclosed_not_used"] += 1
            continue
        side = _candidate_side(r)
        if side is None:
            continue
        source = str(r.get("event"))
        stats[f"raw_{source}_{side}"] += 1
        coin = r.get("coin")
        anchor = scored_bar_open(r)
        if not isinstance(coin, str) or not coin or anchor is None:
            stats[f"unalignable_{side}"] += 1
            stats[f"unalignable_{source}_{side}"] += 1
            continue
        key = (coin, side, anchor)
        if key in unique:
            stats[f"duplicate_{side}"] += 1
            stats[f"duplicate_{source}_{side}"] += 1
            old = unique[key]
            old["source_events"] = sorted(set(old["source_events"]) | {source})
            old["source_record_n"] += 1
            old["last_ts"] = max(old.get("last_ts") or 0, int(r["ts"]) if finite(r.get("ts")) else 0)
            continue
        candidate = dict(r)
        candidate.update({
            "coin": coin,
            "side": side,
            "entry_anchor_ts": anchor,
            "source_events": [source],
            "source_record_n": 1,
            "last_ts": int(r["ts"]) if finite(r.get("ts")) else None,
        })
        unique[key] = candidate
    out = {"long": [], "short": []}
    for (coin, side, anchor), candidate in unique.items():
        stats[f"unique_{side}"] += 1
        out[side].append(candidate)
    for side in out:
        out[side].sort(key=lambda r: (r["entry_anchor_ts"], r["coin"]))
    stats["source_files"] = len(paths)
    stats["unique_total"] = len(unique)
    return out, dict(stats)


def load_runner_gate_candidates(data_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Dedup runner-gate decisions while retaining schema gaps in coverage."""
    paths = [p for p in data_dir.glob("runner_gate_shadow.jsonl*") if p.is_file()]
    unique: dict[tuple[str, str, int], dict[str, Any]] = {}
    stats: Counter[str] = Counter()
    for r in iter_jsonl(paths):
        side = str(r.get("side", "")).lower()
        if side not in {"long", "short"}:
            stats["ignored_without_explicit_side"] += 1
            continue
        stats[f"raw_{side}"] += 1
        has_hits = isinstance(r.get("trigger_hits"), list)
        stats[f"raw_{side}_{'with' if has_hits else 'without'}_trigger_hits"] += 1
        coin = r.get("coin")
        anchor = scored_bar_open(r)
        if not isinstance(coin, str) or not coin or anchor is None:
            stats[f"unalignable_{side}"] += 1
            continue
        key = (coin, side, anchor)
        candidate = dict(r)
        candidate.update({"coin": coin, "side": side, "entry_anchor_ts": anchor, "dedup_conflict": False, "source_record_n": 1})
        if key not in unique:
            unique[key] = candidate
            continue
        stats[f"duplicate_{side}"] += 1
        old = unique[key]
        old["source_record_n"] += 1
        old_hits = _live_hits(old)
        new_hits = _live_hits(candidate)
        if old_hits and new_hits and {k: v.get("fired") for k, v in old_hits.items()} != {k: v.get("fired") for k, v in new_hits.items()}:
            old["dedup_conflict"] = True
            stats[f"conflicting_duplicates_{side}"] += 1
        if not old_hits and new_hits:
            candidate["source_record_n"] = old["source_record_n"]
            candidate["dedup_conflict"] = old["dedup_conflict"]
            unique[key] = candidate
    out = {"long": [], "short": []}
    for (coin, side, anchor), candidate in unique.items():
        stats[f"unique_{side}"] += 1
        if not _live_hits(candidate):
            stats[f"unique_{side}_without_trigger_hits"] += 1
        out[side].append(candidate)
    for side in out:
        out[side].sort(key=lambda r: (r["entry_anchor_ts"], r["coin"]))
    stats["source_files"] = len(paths)
    stats["unique_total"] = len(unique)
    return out, dict(stats)


def _live_hits(candidate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = candidate.get("trigger_hits") if isinstance(candidate.get("trigger_hits"), list) else candidate.get("hits")
    return {h.get("name"): h for h in raw or [] if isinstance(h, dict) and isinstance(h.get("name"), str) and isinstance(h.get("fired"), bool)}


def evaluate_candidate_fidelity(candidate: dict[str, Any], replay_hits: list[dict[str, Any]] | None, canonical_names: list[str], k_invalid_reason: str | None = None) -> list[dict[str, Any]]:
    live = _live_hits(candidate)
    replay = {h["name"]: h for h in replay_hits or []}
    flow = flow_state(candidate, live.get("breakout"))
    rows = []
    for name in canonical_names:
        l = live.get(name)
        r = replay.get(name)
        reason = None
        pairable = True
        if k_invalid_reason or r is None:
            pairable, reason = False, k_invalid_reason or "k_replay_missing"
        elif l is None:
            pairable, reason = False, "live_key_missing"
        elif name == "dailyMover":
            pairable, reason = False, "daily_mover_external_prevDayPx_dayNtlVlm_absent"
        elif name == "breakout" and flow["status"] == "missing":
            pairable, reason = False, flow["reason"]
        agree = bool(l["fired"] == r["fired"]) if pairable else None
        rows.append({"name": name, "pairable": pairable, "agree": agree, "missing_reason": reason, "live_fired": l.get("fired") if l else None, "replay_fired": r.get("fired") if r else None})
    return rows


def _empty_fidelity() -> dict[str, Any]:
    return {"candidate_n": 0, "paired_candidate_n": 0, "total_pairs": 0, "paired_n": 0, "agree_n": 0, "per_trigger": defaultdict(Counter), "missing_reasons": Counter(), "flow_states": Counter(), "pairable_coin_days": set(), "breakout_tp": 0, "breakout_fp": 0, "breakout_fn": 0, "composite_abs_error": []}


def _fidelity_json(acc: dict[str, Any]) -> dict[str, Any]:
    paired, agree = acc["paired_n"], acc["agree_n"]
    lo, hi = wilson_interval(agree, paired)
    tp, fp, fn = acc["breakout_tp"], acc["breakout_fp"], acc["breakout_fn"]
    per = {}
    for name, c in sorted(acc["per_trigger"].items()):
        wlo, whi = wilson_interval(c["agree"], c["paired"])
        per[name] = {"total": c["total"], "paired": c["paired"], "agree": c["agree"], "agreement": _ratio(c["agree"], c["paired"]), "wilson95": [wlo, whi]}
    errors = acc["composite_abs_error"]
    m = {
        "candidate_n": acc["candidate_n"],
        "paired_candidate_n": acc["paired_candidate_n"],
        "total_candidate_trigger_pairs": acc["total_pairs"],
        "paired_n": paired,
        "pair_coverage": _ratio(paired, acc["total_pairs"]),
        "agree_n": agree,
        "agreement": _ratio(agree, paired),
        "agreement_wilson95": [lo, hi],
        "agreement_wilson95_lower": lo,
        "coin_days": len(acc["pairable_coin_days"]),
        "breakout_live_fired_n": tp + fn,
        "breakout_tp": tp, "breakout_fp": fp, "breakout_fn": fn,
        "breakout_precision": _ratio(tp, tp + fp),
        "breakout_recall": _ratio(tp, tp + fn),
        "per_trigger": per,
        "missing_reasons": dict(acc["missing_reasons"]),
        "flow_states": dict(acc["flow_states"]),
        "composite_score_k_only": {"n": len(errors), "mean_abs_error": statistics.fmean(errors) if errors else None, "note": "descriptive only; absent flow is not zero and dailyMover external context is unavailable"},
    }
    m["strict_gate"] = strict_fidelity_verdict(m)
    return m


def _forward(cache: NativeCandles, coin: str, anchor: int, side: str) -> dict[str, Any] | None:
    series = cache.series(coin, "5m")
    ts = [c.t for c in series]
    idx = bisect.bisect_left(ts, anchor)
    if idx >= len(ts) or ts[idx] != anchor or idx + FORWARD_BARS >= len(series):
        return None
    path = series[idx:idx + FORWARD_BARS + 1]
    if any(path[i].t - path[i - 1].t != BAR_5M for i in range(1, len(path))):
        return None
    entry, exit = path[1].o, path[-1].c
    return {"gross_bp": directional_bp(entry, exit, side), "entry_ts": path[1].t, "exit_ts": path[-1].t, "entry": entry, "exit": exit}


def _regime(cache: NativeCandles, coin: str, scored_close: int) -> str:
    proxy = coin if ":" in coin or coin.upper() in {"PAXG", "XAU", "XAG"} else "BTC"
    bars = _history(cache.series(proxy, "4h"), scored_close, 14_400_000, 60)
    if len(bars) < 30:
        return "unknown"
    return classify_candles(bars)


def _volume_liquidity(rows: list[dict[str, Any]], seed: int = PREREG["seed"]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[liquidity_stratum(r.get("volume_24h_usd"))].append(r)
    tiers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        tiers[volume_tier(r.get("volume_24h_usd"))].append(r)
    return {
        "evaluated_separately": True,
        "main_direction_not_filtered_by_50m": True,
        "volume_missing_is_not_zero": True,
        "strata": {name: summarize_returns(groups.get(name, []), seed + i) for i, name in enumerate(("lt_5m", "5m_to_50m", "gte_50m", "unknown"))},
        "s0_tier_baselines": {name: {"n": len(tiers.get(name, [])), "tier": name} for name in ("micro", "mid", "mega", "unknown")},
        "tier_definition": {"mega_min_usd": s0.TIER_MEGA_MIN_USD, "mid_min_usd": s0.TIER_MID_MIN_USD, "source": "S0 reconcile map_bench_tier"},
    }


def joint_distribution(records: list[dict[str, Any]]) -> dict[str, Any]:
    out = Counter()
    for r in records:
        side = str(r.get("side", "")).lower()
        if side not in {"long", "short"} or not finite(r.get("raw")) or not isinstance(r.get("breakout_fired"), bool) or not isinstance(r.get("out_of_range"), bool):
            out["missing_not_imputed"] += 1
            continue
        directional_raw = side_sign(side) * float(r["raw"])
        fired, strong, cross = r["breakout_fired"], directional_raw >= 0.7, r["out_of_range"]
        out["real_intersection_n"] += 1
        out[f"fired_{int(fired)}_directionalraw07_{int(strong)}_out_{int(cross)}"] += 1
        if fired:
            out["breakout_fired_n"] += 1
            if strong:
                out["fired_and_directionalraw07_n"] += 1
                if cross:
                    out["fired_and_directionalraw07_and_out_n"] += 1
    return dict(out)


def trigger_correlations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pairwise live old-six co-firing correlation on the same real-side rows."""
    out: dict[str, Any] = {}
    for i, left in enumerate(OLD_SIX):
        for right in OLD_SIX[i + 1:]:
            paired = [r for r in rows if isinstance(r.get("hits", {}).get(left), bool) and isinstance(r.get("hits", {}).get(right), bool)]
            xs = [float(r["hits"][left]) for r in paired]
            ys = [float(r["hits"][right]) for r in paired]
            out[f"{left}|{right}"] = {
                "n": len(paired),
                "status": "estimated" if len(paired) >= 30 and len(set(xs)) == 2 and len(set(ys)) == 2 else "insufficient",
                "pearson_phi": _pearson(xs, ys) if len(paired) >= 30 else None,
            }
    return out


def direction_verdict(summary: dict[str, Any], fidelity: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        "n": {"value": summary.get("n", 0), "threshold": PREREG["paired_n_min"], "pass": summary.get("n", 0) >= PREREG["paired_n_min"]},
        "coin_days": {"value": summary.get("coin_days", 0), "threshold": PREREG["coin_days_min"], "pass": summary.get("coin_days", 0) >= PREREG["coin_days_min"]},
    }
    evidence_status = "pass" if all(v["pass"] for v in evidence.values()) else "inconclusive"
    dg = summary.get("direction_gate", "inconclusive")
    fg = fidelity.get("strict_gate", {}).get("status", "inconclusive")
    # Evidence insufficiency always dominates a hard failure: an under-covered
    # replay cannot be promoted to a directional fail/pass conclusion.
    if evidence_status == "inconclusive" or dg == "inconclusive" or fg == "inconclusive":
        verdict = "inconclusive"
    elif dg == "fail" or fg == "fail":
        verdict = "fail"
    else:
        verdict = "pass"
    # S3 relaxed gate (user-approved 2026-11-05): for SHORT, gross median > -20bp
    # and CI upper > 0 is sufficient for shadow-only S3. LONG still requires the
    # strict gate. Fidelity must not be a hard fail (inconclusive is acceptable
    # for shadow data collection — the purpose of S3 is precisely to collect it).
    ci = summary.get("gross_block_ci95_bp") or [None, None]
    gross_median = summary.get("gross_median_bp")
    relaxed_short_pass = (
        finite(gross_median)
        and gross_median > PREREG["s3_relaxed_short_median_min_exclusive"]
        and ci[1] is not None
        and ci[1] > PREREG["s3_relaxed_short_ci95_upper_min_exclusive"]
    )
    fg_hard_fail = fg == "fail"
    s3_relaxed_allowed = relaxed_short_pass and not fg_hard_fail and evidence_status == "pass"
    return {
        "verdict": verdict,
        "return_evidence_status": evidence_status,
        "return_evidence_checks": evidence,
        "direction_gate": dg,
        "fidelity_gate": fg,
        "pass_requires_all_three": ["return_n>=1000 and independent coin_days>=30", "gross_median_bp>0 and gross_block_ci95_lower_bp>9.26 (equivalent net@9.26 CI lower>0)", "strict_fidelity_gate=pass"],
        "insufficient_data_is_not_fail": True,
        "s3_relaxed_gate": {
            "version": PREREG["s3_gate_version"],
            "applicable_side": "short",
            "gross_median_bp": gross_median,
            "median_threshold": PREREG["s3_relaxed_short_median_min_exclusive"],
            "ci95_upper_bp": ci[1],
            "ci95_upper_threshold": PREREG["s3_relaxed_short_ci95_upper_min_exclusive"],
            "relaxed_short_pass": relaxed_short_pass,
            "fidelity_hard_fail": fg_hard_fail,
            "s3_relaxed_allowed": s3_relaxed_allowed,
        },
        "s3_start_allowed": s3_relaxed_allowed,
    }


def evaluate(data_dir: Path, native_cache_paths: Iterable[str | Path] = ()) -> dict[str, Any]:
    supplemental_caches = [Path(p).resolve() for p in native_cache_paths]
    cache = NativeCandles([
        data_dir / "ch_hist_candles.json",
        data_dir / ".historical-candles.json",
        *supplemental_caches,
    ])
    session, session_stats = load_session_candidates(data_dir)
    runner, runner_stats = load_runner_gate_candidates(data_dir)
    with (data_dir / ".agent-config.json").open() as f:
        config = json.load(f)
    default_thresholds = trigger_thresholds_params(config=config)
    weights = trigger_weights_params(config=config)
    default_flow_min = float((config.get("launch_capture") or {}).get("flow_confirm_min", 0.7))
    default_cp = config.get("candlestick_patterns") or {}
    default_mc = config.get("momentum_continuation") or {}
    canonical_hits, _ = build_hits([], default_thresholds, weights, candles_1h=[], candles_15m=[], candlestick_patterns=default_cp, momentum_continuation=default_mc)
    canonical_names = [h["name"] for h in canonical_hits]

    coverage: Counter[str] = Counter()
    returns: dict[str, list[dict[str, Any]]] = {"long": [], "short": []}
    return_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}

    # S2-1/S2-2: actual direction-specific session decisions only. A near-miss
    # is never cloned into a LONG and a SHORT counterfactual pair.
    for side in ("long", "short"):
        for c in session[side]:
            coin, anchor = c["coin"], c["entry_anchor_ts"]
            coverage[f"{side}_session_candidates"] += 1
            fwd = _forward(cache, coin, anchor, side)
            if fwd is None:
                coverage[f"{side}_forward_missing"] += 1
                continue
            day = datetime.fromtimestamp(anchor / 1000, timezone.utc).date().isoformat()
            volume = cache.volume_24h_usd(coin, anchor)
            coverage[f"{side}_volume_{'known' if volume is not None else 'unknown'}"] += 1
            row = {
                **fwd,
                "coin": coin,
                "side": side,
                "anchor": anchor,
                "day": day,
                "volume_24h_usd": volume,
                "volume_tier": volume_tier(volume),
                "regime": _regime(cache, coin, anchor + BAR_5M),
                "selection": "actual_session_direction",
                "source_events": c["source_events"],
            }
            returns[side].append(row)
            return_by_key[(coin, side, anchor)] = row
            coverage[f"{side}_forward_complete"] += 1

    fidelity_acc = {side: _empty_fidelity() for side in ("long", "short")}
    flow_records: dict[str, list[dict[str, Any]]] = {"long": [], "short": []}
    flow_returns: dict[str, dict[str, list[dict[str, Any]]]] = {"long": defaultdict(list), "short": defaultdict(list)}
    flow_transitions = {side: Counter() for side in ("long", "short")}
    flow_coverage = {side: Counter() for side in ("long", "short")}
    ic_rows = {side: {name: [] for name in OLD_SIX} for side in ("long", "short")}
    ic_wide: dict[str, list[dict[str, Any]]] = {"long": [], "short": []}
    aligned_symbols = {side: set() for side in ("long", "short")}

    # S2 fidelity/S2-3/S2-4: runner-gate supplies the actual side and live hit
    # reference. Legacy rows without hits stay in every coverage denominator.
    for side in ("long", "short"):
        acc = fidelity_acc[side]
        for c in runner[side]:
            coin, anchor = c["coin"], c["entry_anchor_ts"]
            day = datetime.fromtimestamp(anchor / 1000, timezone.utc).date().isoformat()
            scored_close = anchor + BAR_5M
            acc["candidate_n"] += 1
            flow_coverage[side]["candidates"] += 1
            c5_all = _history(cache.series(coin, "5m"), scored_close, BAR_5M, 400, exact_last_open=anchor)
            c5 = c5_all[-100:]
            c1 = _history(cache.series(coin, "1h"), scored_close, 3_600_000, 100)
            c15 = aggregate_15m(c5_all, scored_close)
            k_invalid = None
            if c.get("dedup_conflict"):
                k_invalid = "dedup_conflict_live_reference"
            elif not c5_all or c5_all[-1].t != anchor:
                k_invalid = "missing_exact_5m_bar"
            elif len(c5_all) < 300 or len(c5) < 100 or len(c1) < 100 or len(c15) < 100:
                k_invalid = "insufficient_contiguous_5m_1h_or_derived15m_history"
            if k_invalid:
                flow_coverage[side][k_invalid] += 1
            else:
                aligned_symbols[side].add(coin)
                flow_coverage[side]["exact_bar_aligned"] += 1

            snapshot = c.get("thresholds") if isinstance(c.get("thresholds"), dict) else {}
            thresholds = {**default_thresholds, **snapshot}
            flow_coverage[side]["thresholds_record_snapshot" if snapshot else "thresholds_current_default"] += 1
            recorded_flow_min = c.get("flow_confirm_min", snapshot.get("flow_confirm_min", snapshot.get("flowConfirmMin")))
            flow_min = float(recorded_flow_min) if finite(recorded_flow_min) else default_flow_min
            cp = c.get("candlestick_patterns") if isinstance(c.get("candlestick_patterns"), dict) else default_cp
            mc = c.get("momentum_continuation") if isinstance(c.get("momentum_continuation"), dict) else default_mc
            live = _live_hits(c)
            flow = flow_state(c, live.get("breakout"))
            flow_coverage[side][f"flow_{flow['status']}"] += 1
            replay_k_only = None
            replay_full = None
            score_weights = weights
            if not k_invalid:
                try:
                    replay_k_only, score_weights = build_hits(c5, thresholds, weights, candles_1h=c1, candles_15m=c15, flow_confirm=None, flow_confirm_min=flow_min, momentum_continuation=mc, candlestick_patterns=cp)
                    if flow["status"] == "missing":
                        replay_full = replay_k_only
                    else:
                        replay_full, _ = build_hits(c5, thresholds, weights, candles_1h=c1, candles_15m=c15, flow_confirm=flow["raw"], flow_confirm_min=flow_min, momentum_continuation=mc, candlestick_patterns=cp)
                except Exception as exc:
                    k_invalid = f"production_pure_function_error:{type(exc).__name__}"
                    replay_k_only = None
                    replay_full = None
                    flow_coverage[side][k_invalid] += 1

            live_score = c.get("score", c.get("composite"))
            if not k_invalid and finite(live_score):
                replay_score = composite_score(replay_k_only or [], score_weights)
                acc["composite_abs_error"].append(abs(float(live_score) - replay_score))

            pairs = evaluate_candidate_fidelity(c, replay_full if not k_invalid else None, canonical_names, k_invalid)
            acc["total_pairs"] += len(pairs)
            candidate_pairable = False
            for p in pairs:
                name = p["name"]
                pc = acc["per_trigger"][name]
                pc["total"] += 1
                if not p["pairable"]:
                    acc["missing_reasons"][p["missing_reason"]] += 1
                    continue
                candidate_pairable = True
                acc["paired_n"] += 1
                pc["paired"] += 1
                if p["agree"]:
                    acc["agree_n"] += 1
                    pc["agree"] += 1
                if name == "breakout":
                    if p["live_fired"] and p["replay_fired"]:
                        acc["breakout_tp"] += 1
                    elif p["replay_fired"]:
                        acc["breakout_fp"] += 1
                    elif p["live_fired"]:
                        acc["breakout_fn"] += 1
            if candidate_pairable:
                acc["paired_candidate_n"] += 1
                acc["pairable_coin_days"].add((coin, day))

            fwd = None if c.get("dedup_conflict") else _forward(cache, coin, anchor, side)
            volume = cache.volume_24h_usd(coin, anchor)
            out_of_range = None
            lookback = int(thresholds.get("breakoutLookback", 20))
            if len(c5) > lookback:
                prior = c5[-lookback - 1:-1]
                out_of_range = bool(c5[-1].c > max(x.h for x in prior)) if side == "long" else bool(c5[-1].c < min(x.l for x in prior))
            full_breakout = next((h for h in replay_full or [] if h.get("name") == "breakout"), None)
            k_breakout = next((h for h in replay_k_only or [] if h.get("name") == "breakout"), None)
            flow_records[side].append({
                "side": side,
                "breakout_fired": full_breakout.get("fired") if full_breakout and flow["status"] != "missing" else None,
                "raw": flow["raw"],
                "out_of_range": out_of_range,
            })
            if full_breakout and k_breakout and flow["status"] != "missing":
                flow_transitions[side][f"k_only_{int(bool(k_breakout['fired']))}->actual_flow_{int(bool(full_breakout['fired']))}"] += 1
            if fwd is not None:
                flow_row = {**fwd, "coin": coin, "side": side, "anchor": anchor, "day": day, "volume_24h_usd": volume, "regime": _regime(cache, coin, scored_close)}
                flow_returns[side][flow["status"]].append(flow_row)
                flow_coverage[side]["forward_complete"] += 1
            else:
                flow_coverage[side]["forward_missing"] += 1

            # Per-hit IC requires both real live hits and the same-side session
            # return at the identical coin/anchor. Runner-only or session-only
            # rows are disclosed by the join counts, never synthetically paired.
            session_row = return_by_key.get((coin, side, anchor))
            if session_row is None or c.get("dedup_conflict"):
                flow_coverage[side]["ic_join_missing"] += 1
                continue
            flow_coverage[side]["ic_join_matched"] += 1
            hits = {name: live[name]["fired"] for name in OLD_SIX if name in live and isinstance(live[name].get("fired"), bool)}
            ic_wide[side].append({"coin": coin, "day": day, "regime": session_row["regime"], "hits": hits})
            for name, fired in hits.items():
                ic_rows[side][name].append({"fired": fired, "gross_bp": session_row["gross_bp"], "coin": coin, "day": day, "regime": session_row["regime"]})

    fidelity_by_side = {side: _fidelity_json(fidelity_acc[side]) for side in ("long", "short")}
    sides: dict[str, Any] = {}
    verdicts: dict[str, Any] = {}
    per_hit_ic: dict[str, Any] = {}
    flow_by_side: dict[str, Any] = {}
    for i, side in enumerate(("long", "short")):
        summary = summarize_returns(returns[side], PREREG["seed"] + i)
        summary["liquidity_50m"] = _volume_liquidity(returns[side], PREREG["seed"] + 10 + i)
        summary["by_btc_4h_regime"] = {
            regime: summarize_returns([r for r in returns[side] if r["regime"] == regime], PREREG["seed"] + 20 + i)
            for regime in sorted({r["regime"] for r in returns[side]})
        }
        sides[side] = summary
        verdicts[side] = direction_verdict(summary, fidelity_by_side[side])
        regime_rows: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {name: [] for name in OLD_SIX})
        for name in OLD_SIX:
            for row in ic_rows[side][name]:
                regime_rows[row["regime"]][name].append(row)
        per_hit_ic[side] = {
            "overall": {name: hit_ic(ic_rows[side][name]) for name in OLD_SIX},
            "by_btc_4h_regime": {regime: {name: hit_ic(rows[name]) for name in OLD_SIX} for regime, rows in sorted(regime_rows.items())},
            "trigger_correlations": trigger_correlations(ic_wide[side]),
            "side_regime_cell_counts": dict(Counter(r["regime"] for r in ic_wide[side])),
        }
        flow_by_side[side] = {
            "coverage": dict(flow_coverage[side]),
            "joint_distribution": joint_distribution(flow_records[side]),
            "k_only_to_actual_flow_breakout_transitions": dict(flow_transitions[side]),
            "returns_by_flow_state": {state: summarize_returns(rows, PREREG["seed"] + 30 + i + j) for j, (state, rows) in enumerate(sorted(flow_returns[side].items()))},
        }

    edge_status = "pass" if all(v["verdict"] == "pass" for v in verdicts.values()) else ("inconclusive" if any(v["verdict"] == "inconclusive" for v in verdicts.values()) else "fail")
    result = {
        "schema_version": "s2_offline_readonly_v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_read_only_no_network_s3_relaxed_gate",
        "preregistration": copy.deepcopy(PREREG),
        "cost_model": {"units": "unlevered_bp", "entry": "next_5m_open", "exit": "close_after_48_5m_bars_4h", "gross_sign": "S0 reconcile convention: long=+1 short=-1", "net_3": "gross-3 once", "net_9_26": "gross-9.26 once", "slippage_bps": None, "slippage_status": "unknown", "tier_basis": "PIT close_x_base_volume rolling 288 native 5m bars; S0 map_bench_tier; no leverage"},
        "coverage": {
            "session_candidates": session_stats,
            "runner_gate_candidates": runner_stats,
            "evaluation": dict(coverage),
            "cache_selected_series_by_file": dict(cache.selected_sources),
            "supplemental_native_caches": [str(p) for p in supplemental_caches],
            "invalid_cache_bars": dict(cache.invalid_bars),
            "candidate_definition": "returns: unique actual session (coin,side,scored_bar_open); fidelity/flow: unique runner-gate (coin,side,scored_bar_open); near-miss is audit-only",
            "native_bar_rule": "bar_close_ms-5m, else decision ts floored to previous closed 5m; exact 5m anchor; closed native 1h; natural 15m triplets only; BTC 4h regime",
            "volume_rule": "HL_5m_close_x_base_volume_rolling_288 ending at scored bar; contiguous bars required; missing remains unknown",
            "runner_aligned_symbols": {side: len(aligned_symbols[side]) for side in ("long", "short")},
        },
        "fidelity_by_side": fidelity_by_side,
        "direction_verdict": verdicts,
        "edge_verdict": edge_status,
        "long": sides["long"],
        "short": sides["short"],
        "flow": {
            "rule": "available=true + finite raw => full stack; available=false + raw=None => ordinary; invalid/contradiction => missing, never zero; short directional raw=-raw",
            "k_only": {"reported_separately": True, "not_strict_full_stack": True, "composite_score": {side: fidelity_by_side[side]["composite_score_k_only"] for side in ("long", "short")}},
            "s2_3_runner_gate": {"by_side": flow_by_side, "selection_bias": "actual runner-gate side with native PIT bars and original flow state; no near-miss or universe-level causal inference"},
        },
        "per_hit_ic": {
            "definition": "old-six live fired(0/1) vs same-key, same-side session 4h gross bp; n>=30 and both classes; correlations use the same joined rows",
            "join_key": "coin x side x scored_bar_open",
            "sides": per_hit_ic,
            "new_14": {"status": "not_evaluated", "reason": "no valid historical fired_names contract for a new 14-item arm"},
            "confounding": ["side and regime cells are reported before interpretation and are descriptive, not causal", "coin/day clustering and trigger co-firing violate independent-observation assumptions", "regime is a BTC 4h PIT proxy for crypto", "no multiplicity adjustment; old-six IC is exploratory", "missing lowerHighs1h live keys remain missing, never false"],
        },
        "limitations": ["record thresholds are preferred when present; otherwise current config is a disclosed fallback, not proof of historical live equivalence", "dailyMover needs external prevDayPx/dayNtlVlm and remains unpairable rather than fabricated false", "missing live hits, flow, native bars, forward paths and volume remain missing and reduce coverage", "session decisions are not fills; net@3/net@9.26 are cost scenarios and slippage is unknown", "insufficient return or fidelity evidence is inconclusive, not a directional fail", "s3_start_allowed uses the user-approved relaxed SHORT gate (median>-20bp, CI upper>0); LONG still requires strict pass"],
        "s3_start_allowed": verdicts["short"]["s3_start_allowed"],
        "s3_gate": {
            "version": PREREG["s3_gate_version"],
            "short_relaxed": verdicts["short"].get("s3_relaxed_gate", {}),
            "long_strict": {
                "verdict": verdicts["long"]["verdict"],
                "s3_start_allowed": verdicts["long"]["verdict"] == "pass",
                "note": "LONG still requires strict gate: median>0, CI lower>9.26bp, fidelity=pass",
            },
        },
    }
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="/data", type=Path)
    p.add_argument("--native-cache", action="append", default=[], type=Path,
                   help="optional read-only native candle snapshot; may be repeated")
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    output = args.output.resolve()
    if output.parent != Path("/tmp"):
        p.error("--output must be a file directly under /tmp")
    result = evaluate(args.data_dir, args.native_cache)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    os.replace(tmp, output)
    print(json.dumps({"output": str(output), "fidelity": {side: result["fidelity_by_side"][side]["strict_gate"]["status"] for side in ("long", "short")}, "direction_verdict": result["direction_verdict"], "edge_verdict": result["edge_verdict"], "s3_start_allowed": result["s3_start_allowed"], "s3_gate": result["s3_gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
