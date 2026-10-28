"""Read-only SHADOW-evidence routes for the new offline-only evidence streams.

Surfaces evidence that previously existed only as /data JSONL consumed by
offline CLIs, so operators can judge the arms in the portal without shell
access. STRICT READ-ONLY: every handler only opens files and aggregates; nothing
here writes config, flips a gate, or places orders.

  * GET /api/dashboard/xs-shadow/panic
      Live xs_reversal panic-gate record-only evidence. Distinguishes the pure
      conditional verdict (panic_gate_pass, computed even while the switch is
      off) from the active switch (panic_gate_active), with atr/rsi/reasons.
      Reads the live xs jsonl PLUS the durable mature-outcome ledger.
  * GET /api/dashboard/xs-shadow/net-outcome
      Live xs economics NET of the 9.26bp round-trip cost (net_outcome.fwd72
      /fwd24/fwd168) from the append-only mature ledger, with a matching GROSS
      view (pnl_pct / forward) so the two populations/units are never confused.
  * GET /api/dashboard/runner-gate/dual-count
      40/45 dual-threshold gate attribution from runner_gate_shadow.jsonl
      (passes40/passes45/admitted), incl. admitted-but-score<45 bypass/AI rows.

Anonymous-safe counts/stats only (no secrets), matching the shadow paper-ledger
posture. Missing files => 200 with zeroed counts + present:false (cold start),
never a 500.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger("hermes-dashboard")

DEFAULT_COST_BPS = 9.26
_LEDGER_NAME = "xs_reversal_outcomes.ledger.jsonl"
_XS_NAME = "xs_reversal_shadow.jsonl"
_RUNNER_NAME = "runner_gate_shadow.jsonl"
_MAX_RECENT = 50


def _data_file(env_name: str, filename: str) -> str:
    """Mirror the writers' /data-vs-HOME resolution (read side)."""
    explicit = os.environ.get(env_name)
    if explicit:
        return explicit
    data_dir = os.environ.get("HERMES_DATA_DIR") or "/data"
    cand = os.path.join(data_dir, filename)
    if os.path.exists(cand):
        return cand
    # config override / legacy home path (xs writer default)
    home_cand = os.path.expanduser(
        os.path.join("~/.hermes-trading", filename))
    return home_cand if os.path.exists(home_cand) else cand


def _read_jsonl(path: str, limit: Optional[int] = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning("shadow_evidence read failed %s: %s", path, e)
        return []
    return rows[-limit:] if limit else rows


def _day_of(ts_ms: Any) -> str:
    try:
        return _dt.datetime.utcfromtimestamp(float(ts_ms) / 1000.0).strftime(
            "%Y-%m-%d")
    except (TypeError, ValueError):
        return "?"


def _fmean(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def _net_pct(rec: dict[str, Any], horizon: int) -> Optional[float]:
    """Schema-tolerant NET fwd{h} (PERCENT). New reconciler -> net_outcome;
    legacy outcome dict; else gross forward minus the 9.26bp cost."""
    key = f"fwd{horizon}_net_pct"
    net = rec.get("net_outcome")
    if isinstance(net, dict) and net.get(key) is not None:
        return float(net[key])
    fwd = rec.get("forward")
    gross = None
    if isinstance(fwd, dict) and fwd.get(f"fwd{horizon}h_pct") is not None:
        gross = float(fwd[f"fwd{horizon}h_pct"])
    elif rec.get(f"fwd{horizon}h_pct") is not None:
        gross = float(rec[f"fwd{horizon}h_pct"])
    if horizon == 72 and rec.get("pnl_pct") is not None:
        gross = float(rec["pnl_pct"])
    if gross is None:
        return None
    cost = float((net or {}).get("cost_bps_rt", DEFAULT_COST_BPS))
    return round(gross - cost / 100.0, 4)


def _gross_pct(rec: dict[str, Any], horizon: int) -> Optional[float]:
    fwd = rec.get("forward")
    if isinstance(fwd, dict) and fwd.get(f"fwd{horizon}h_pct") is not None:
        return float(fwd[f"fwd{horizon}h_pct"])
    if horizon == 72 and rec.get("pnl_pct") is not None:
        return float(rec["pnl_pct"])
    return None


# --------------------------------------------------------------------------- #
# xs panic gate
# --------------------------------------------------------------------------- #
def _xs_path() -> str:
    # Prefer the configured live path (/data/xs_reversal_shadow.jsonl), then
    # the same env/default chain the xs writer uses.
    cfg = os.environ.get("HERMES_XS_REVERSAL_SHADOW_FILE")
    if cfg and os.path.exists(cfg):
        return cfg
    data = os.path.join(os.environ.get("HERMES_DATA_DIR") or "/data", _XS_NAME)
    if os.path.exists(data):
        return data
    return _data_file("HERMES_XS_REVERSAL_SHADOW_FILE", _XS_NAME)


def _panic_payload(days: int) -> dict[str, Any]:
    live = _read_jsonl(_xs_path())
    ledger_path = os.path.join(os.environ.get("HERMES_DATA_DIR") or "/data",
                               _LEDGER_NAME)
    led = _read_jsonl(ledger_path)
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=days)
    cutoff_ms = cutoff.timestamp() * 1000.0

    def fresh(rows):
        return [r for r in rows if _num(r.get("timestamp")) is not None
                and float(r["timestamp"]) >= cutoff_ms]

    live_f, led_f = fresh(live), fresh(led)
    seen, merged = set(), []
    for r in live_f + led_f:
        k = (r.get("timestamp"), r.get("coin"))
        if k in seen:
            continue
        seen.add(k)
        merged.append(r)

    n = len(merged)
    # active == the on/off switch actually recorded (panic_gate_active); fall
    # back to the older panic_gate_enabled key on pre-8e18a84 rows.
    def active(r):
        v = r.get("panic_gate_active")
        return r.get("panic_gate_enabled") if v is None else v
    n_active_true = sum(1 for r in merged if active(r) is True)
    n_cond_pass = sum(1 for r in merged if r.get("panic_gate_pass") is True)
    # active AND pass = trades the switch would have admitted
    n_active_pass = sum(1 for r in merged
                        if active(r) is True and r.get("panic_gate_pass") is True)
    cand = [r for r in merged if r.get("is_candidate") is True]
    atr = [float(r["atr24_pct"]) for r in merged
           if r.get("atr24_pct") is not None]
    atrp = [float(r["atr_pctile"]) for r in merged
            if r.get("atr_pctile") is not None]
    rsi = [float(r["rsi14"]) for r in merged if r.get("rsi14") is not None]
    reasons: dict[str, int] = {}
    for r in merged:
        for why in (r.get("panic_gate_reasons") or []):
            reasons[str(why)] = reasons.get(str(why), 0) + 1
    recent = sorted(
        ({
            "ts": r.get("timestamp"), "day": _day_of(r.get("timestamp")),
            "coin": r.get("coin"), "ext_pct": r.get("ext_pct"),
            "rsi14": r.get("rsi14"), "atr24_pct": r.get("atr24_pct"),
            "atr_pctile": r.get("atr_pctile"),
            "active": active(r), "pass": r.get("panic_gate_pass"),
            "is_candidate": r.get("is_candidate"),
            "reasons": r.get("panic_gate_reasons") or [],
        } for r in merged),
        key=lambda x: -(x["ts"] or 0))[:_MAX_RECENT]
    return {
        "present": n > 0, "window_days": days, "rows": n,
        "live_rows": len(live_f), "ledger_rows": len(led_f),
        "active_switch_true": n_active_true,
        "conditional_pass": n_cond_pass,
        "active_and_pass": n_active_pass,
        "candidates": len(cand),
        "avg_atr24_pct": _fmean(atr), "avg_atr_pctile": _fmean(atrp),
        "avg_rsi14": _fmean(rsi),
        "reason_counts": dict(sorted(reasons.items(),
                                     key=lambda kv: -kv[1])),
        "recent": recent,
    }


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# xs net outcome (mature ledger only — real forward resolution)
# --------------------------------------------------------------------------- #
def _net_payload(days: int, candidates_only: bool) -> dict[str, Any]:
    ledger_path = os.path.join(os.environ.get("HERMES_DATA_DIR") or "/data",
                               _LEDGER_NAME)
    rows = _read_jsonl(ledger_path)
    cutoff_ms = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).timestamp()         * 1000.0
    rows = [r for r in rows if _num(r.get("timestamp")) is not None
            and float(r["timestamp"]) >= cutoff_ms]
    if candidates_only:
        rows = [r for r in rows if r.get("is_candidate") is True]

    horizons = (24, 72, 168)
    windows = {}
    for h in horizons:
        net = [v for r in rows if (v := _net_pct(r, h)) is not None]
        gross = [v for r in rows if (v := _gross_pct(r, h)) is not None]
        net_bp = [v * 100.0 for v in net]
        gross_bp = [v * 100.0 for v in gross]
        windows[f"fwd{h}h"] = {
            "n_net": len(net),
            "net_mean_bp": _fmean(net_bp),
            "net_win_rate_pct": round(100.0 * sum(1 for v in net if v > 0)
                                      / len(net), 2) if net else None,
            "n_gross": len(gross),
            "gross_mean_bp": _fmean(gross_bp),
            "gross_win_rate_pct": round(100.0 * sum(1 for v in gross if v > 0)
                                        / len(gross), 2) if gross else None,
        }
    cost = DEFAULT_COST_BPS
    for r in rows:
        n0 = r.get("net_outcome")
        if isinstance(n0, dict) and n0.get("cost_bps_rt") is not None:
            cost = float(n0["cost_bps_rt"])
            break
    recent = sorted(
        ({
            "ts": r.get("timestamp"), "day": _day_of(r.get("timestamp")),
            "coin": r.get("coin"), "is_candidate": r.get("is_candidate"),
            "outcome": r.get("outcome"),
            "net72_bp": (lambda v: round(v * 100.0, 2) if v is not None else None)(
                _net_pct(r, 72)),
            "gross72_bp": (lambda v: round(v * 100.0, 2) if v is not None else None)(
                _gross_pct(r, 72)),
            "pass": r.get("panic_gate_pass"), "active": r.get(
                "panic_gate_active", r.get("panic_gate_enabled")),
        } for r in rows),
        key=lambda x: -(x["ts"] or 0))[:_MAX_RECENT]
    return {
        "present": len(rows) > 0, "window_days": days,
        "candidates_only": candidates_only, "rows": len(rows),
        "cost_bps_rt": cost,
        "note": "net = gross forward close-only minus round-trip cost; LIVE live "
                "shadow (real HL forward bars), NOT historical backtest.",
        "windows": windows, "recent": recent,
    }


# --------------------------------------------------------------------------- #
# runner gate 40/45 dual count
# --------------------------------------------------------------------------- #
def _dual_payload(days: int) -> dict[str, Any]:
    rows = _read_jsonl(_data_file("HERMES_RUNNER_GATE_SHADOW_FILE",
                                  _RUNNER_NAME))
    cutoff_ms = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).timestamp()         * 1000.0
    rows = [r for r in rows if _num(r.get("ts")) is not None
            and float(r["ts"]) >= cutoff_ms]
    n = len(rows)
    p40 = [r for r in rows if r.get("passes40") is True]
    p45 = [r for r in rows if r.get("passes45") is True]
    admitted = [r for r in rows if r.get("admitted") is True]
    p40_not45 = [r for r in p40 if r.get("passes45") is not True]
    # admitted despite composite < the 45 hard floor -> bypass/AI path
    admitted_low = [r for r in admitted
                    if _num(r.get("composite_score")) is not None
                    and float(r["composite_score"]) < 45.0]
    rule_counts: dict[str, int] = {}
    for r in rows:
        rule = str(r.get("rule") or "?")
        key = rule.split("(")[0].strip()
        rule_counts[key] = rule_counts.get(key, 0) + 1
    recent = sorted(
        ({
            "ts": r.get("ts"), "day": _day_of(r.get("ts")),
            "coin": r.get("coin"), "side": r.get("side"),
            "composite_score": r.get("composite_score"),
            "passes40": r.get("passes40"), "passes45": r.get("passes45"),
            "admitted": r.get("admitted"), "rule": r.get("rule"),
        } for r in rows),
        key=lambda x: -(x["ts"] or 0))[:_MAX_RECENT]
    return {
        "present": n > 0, "window_days": days, "rows": n,
        "passes40": len(p40), "passes45": len(p45),
        "passes40_not45": len(p40_not45), "admitted": len(admitted),
        "admitted_score_below45": len(admitted_low),
        "rule_counts": dict(sorted(rule_counts.items(),
                                   key=lambda kv: -kv[1])[:12]),
        "recent": recent,
    }


def register_shadow_evidence_routes(app: FastAPI) -> None:
    @app.get("/api/dashboard/xs-shadow/panic")
    async def xs_shadow_panic(days: int = Query(90, ge=1, le=400)) -> JSONResponse:
        return JSONResponse(await _to_thread(_panic_payload, days))

    @app.get("/api/dashboard/xs-shadow/net-outcome")
    async def xs_shadow_net(days: int = Query(120, ge=1, le=600),
                            candidates_only: bool = Query(True)) -> JSONResponse:
        return JSONResponse(await _to_thread(_net_payload, days,
                                             candidates_only))

    @app.get("/api/dashboard/runner-gate/dual-count")
    async def runner_dual_count(days: int = Query(30, ge=1, le=400)) -> JSONResponse:
        return JSONResponse(await _to_thread(_dual_payload, days))


async def _to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)
