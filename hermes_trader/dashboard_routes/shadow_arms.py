"""SHADOW risk-arm grading-center routes (Audit 2026-09-07, M1).

Surfaces the nightly shadow-arm grader (scripts/shadow_grade.py) over the
dashboard API so the portal can render a per-arm grading/recommendation view:

  * GET  /api/dashboard/shadow-arms/grades        — latest arm verdicts + stats
  * POST /api/dashboard/shadow-arms/refresh       — force a regrade (operator write)
  * GET  /api/dashboard/shadow-arms/grade-history — nightly verdict trend snapshots
  * GET  /api/dashboard/shadow-arms/backfill-summary — historical backtest aggregates
  * GET  /api/dashboard/shadow-arms/regen-report  — long-horizon regen replay report
  * POST /api/dashboard/shadow-arms/regen-refresh — trigger a regen replay (operator write)
  * GET  /api/dashboard/shadow-arms/regen-status  — manual regen run state

Posture (INERT red line): this is a READ/REPORT surface. The grader only rates
arms and (via the nightly cron, --push) sends a Feishu advisory card — it never
writes config, never flips a gate's mode, never places orders. A PROMOTE_CANDIDATE
verdict is a recommendation only; promotion to enforce stays a human action via
the authoritative config_store write path. Nothing in these endpoints changes
that posture.

Implementation notes:
  * shadow_grade lives under scripts/ (not a package); it self-installs its own
    directory on sys.path for its shadow_progress dependency. We import it
    lazily by file path via importlib, cached after first load. The whole loader
    is bulletproof: if the grader script is missing/broken the endpoints return
    503 rather than 500, and the trading/dashboard process is unaffected.
  * grades() reads up to ~a dozen shadow JSONL files (some MBs each), so the
    result is TTL-cached for 60s via dashboard._ttl_cached (singleflight —
    concurrent misses only load once). refresh() invalidates the cache.
  * grade-history reads only the slim nightly snapshot JSONL written by the
    cron path (one line per night); it is cheap and read directly.
  * reads are anonymous-safe (verdict + counts — no secrets), matching the
    shadow paper-ledger endpoints; refresh requires the operator write token
    and is audited to the session log.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from hermes_trader import session_log
from hermes_trader.dashboard import _require_operator, _ttl_cached

logger = logging.getLogger("hermes-dashboard")

_DEFAULT_WINDOWS = (24, 72, 168)
_GRADES_TTL_S = 60.0
_GRADES_CACHE_KEY = "shadow_arms_grades"

# ── historical backtest (backfill) evidence surface ──────────────────────────
# Aggregates the offline backfill artifacts (/data/*.backfill.jsonl) produced by
# the scripts/backfill_*.py historical replayers. Same read-only posture as the
# grader: this surface changes nothing, it only reports replayed evidence.
_BACKFILL_TTL_S = 60.0
_BACKFILL_CACHE_KEY = "shadow_arms_backfill_summary"
_BACKFILL_FILES: tuple[tuple[str, str], ...] = (
    ("ta_late_entry", "ta_late_entry_shadow.backfill.jsonl"),
    ("xs_reversal", "xs_reversal_shadow.backfill.jsonl"),
    ("atr_regime_calib", "atr_regime_calib_shadow.backfill.jsonl"),
    ("pullback", "pullback_shadow.backfill.jsonl"),
    ("per_coin_regime", "per_coin_regime_backfill.jsonl"),
    ("daily_extension_cap", "daily_extension_cap_shadow.backfill.jsonl"),
    ("relax_tier", "relax_tier_shadow.backfill.jsonl"),
    ("trend_filter", "trend_filter_shadow.backfill.jsonl"),
)

# Counterfactual probe arms (change/stricter-rule simulations) encode outcome
# "win" as "the arm forgoes profit / hurts" — win = arm-HARMFUL, loss =
# arm-BENEFICIAL (see scripts/reconcile_change_arms_shadow.py, 2026-09-08
# change-arm label fix). Never surface a literal "win rate" for these: their
# pnl win_rate is renamed arm_harmful_rate so no metric name carries opposite
# meanings across arms.
_COUNTERFACTUAL_ARMS = frozenset({
    "atr_regime_calib", "daily_extension_cap", "relax_tier", "trend_filter",
})
_COUNTERFACTUAL_SEMANTICS = "counterfactual: outcome win=arm-harmful loss=arm-beneficial"


def _backfill_dir() -> str:
    return os.environ.get("HERMES_BACKFILL_DIR", "/data")


def _pnl_stats(values, *, counterfactual: bool = False) -> dict | None:
    xs = sorted(v for v in values if isinstance(v, (int, float)))
    if not xs:
        return None
    n = len(xs)
    median = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    # Counterfactual arms: positive pnl = the arm forgoes profit (arm-harmful),
    # so the positive share is reported as arm_harmful_rate, never "win_rate".
    pos_key = "arm_harmful_rate" if counterfactual else "win_rate"
    return {
        "n": n,
        pos_key: round(sum(1 for v in xs if v > 0) / n, 4),
        "avg_pct": round(sum(xs) / n, 4),
        "median_pct": round(median, 4),
        "min_pct": round(xs[0], 4),
        "max_pct": round(xs[-1], 4),
    }


def _arm_extras(arm: str, rows: list[dict]) -> dict:
    extras: dict = {}
    if arm == "xs_reversal":
        extras["candidates"] = sum(1 for r in rows if r.get("is_candidate"))
        fwd: dict = {}
        for label, key in (("24h", "fwd24h_pct"), ("72h", "fwd72h_pct"), ("168h", "fwd168h_pct")):
            fwd[label] = _pnl_stats((r.get("forward") or {}).get(key) for r in rows)
        extras["forward"] = fwd
    elif arm == "atr_regime_calib":
        changed = [r for r in rows if r.get("would_change")]
        extras["would_change"] = len(changed)
        deltas = [
            r["cf_v2_pnl_pct"] - r["cf_v1_pnl_pct"]
            for r in changed
            if isinstance(r.get("cf_v2_pnl_pct"), (int, float))
            and isinstance(r.get("cf_v1_pnl_pct"), (int, float))
        ]
        if deltas:
            extras["calibration_delta"] = {
                "n": len(deltas),
                "avg_pct": round(sum(deltas) / len(deltas), 4),
                "improved": sum(1 for d in deltas if d > 0),
            }
    elif arm == "per_coin_regime":
        would: dict[str, int] = {}
        for r in rows:
            k = str(r.get("would"))
            would[k] = would.get(k, 0) + 1
        extras["would"] = would
    return extras


def _summarize_arm(arm: str, fname: str, data_dir: str) -> dict:
    path = os.path.join(data_dir, fname)
    entry: dict = {"arm": arm, "file": fname, "present": False, "records": 0}
    if not os.path.isfile(path):
        entry["note"] = "no backfill artifact (insufficient live samples)"
        return entry
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # tolerate a torn trailing line; never fail the surface
    entry["present"] = True
    entry["records"] = len(rows)
    entry["mtime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))
    # relax_tier rows are ta_late records carrying rt_*-prefixed grade fields
    # (the bare keys stay None); map the arm onto its actual field names.
    pnl_key = "rt_pnl_pct" if arm == "relax_tier" else "pnl_pct"
    outcome_key = "rt_outcome" if arm == "relax_tier" else "outcome"
    counterfactual = arm in _COUNTERFACTUAL_ARMS
    if counterfactual:
        entry["semantics"] = _COUNTERFACTUAL_SEMANTICS
    outcomes: dict[str, int] = {}
    for r in rows:
        k = r.get(outcome_key)
        if not isinstance(k, str) or not k:
            continue  # ungraded rows: never emit a literal "None" bucket
        outcomes[k] = outcomes.get(k, 0) + 1
    if outcomes:
        entry["outcomes"] = outcomes
    entry["pnl"] = _pnl_stats((r.get(pnl_key) for r in rows), counterfactual=counterfactual)
    by_side: dict = {}
    for side in ("long", "short"):
        st = _pnl_stats(
            (r.get(pnl_key) for r in rows if r.get("side") == side),
            counterfactual=counterfactual,
        )
        if st:
            by_side[side] = st
    if by_side:
        entry["by_side"] = by_side
    extras = _arm_extras(arm, rows)
    if extras:
        entry["extras"] = extras
    return entry


def _backfill_payload() -> dict:
    data_dir = _backfill_dir()
    arms = [_summarize_arm(arm, fname, data_dir) for arm, fname in _BACKFILL_FILES]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data_dir": data_dir,
        "files_present": sum(1 for a in arms if a["present"]),
        "arms": arms,
        "cache_ttl_s": _BACKFILL_TTL_S,
    }


# ── long-horizon signal-regen replay report surface ─────────────────────────
# Surfaces the isolated report written by scripts/regen_param_sweep.py
# (/data/regen_param_sweep_report.json): a 90-180d candidate-universe replay
# with walk-forward 60/20/20 splits, single-axis EV curves, plateau picks and
# a regen-vs-live overlap anchor. Same INERT posture: read/report only — the
# manual trigger below just re-runs the offline script; it never touches
# config, gates or orders.
_REGEN_TTL_S = 60.0
_REGEN_CACHE_KEY = "shadow_arms_regen_report"
_REGEN_SWEEP_TOP_N = 20  # cap the ~150-row ta_late_entry grid on the read surface
_REGEN_RUN_LOG = "/tmp/regen_dashboard_run.log"
_REGEN_KLINE_CACHE = "/tmp/regen_candles_cache.json"

# Manual-run state (singleflight). Polled via the regen-status endpoint.
_REGEN_RUN_STATE: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "days": None,
    "cmd": None,
    "error": None,
}
_REGEN_TASKS: set = set()  # strong refs so GC never reaps a running task


def _regen_report_file() -> str:
    return os.environ.get("HERMES_REGEN_REPORT_FILE", "/data/regen_param_sweep_report.json")


def _scripts_file(fname: str) -> str:
    """Resolve a scripts/ helper by name (scripts/ is not a package; it lives at
    /app/scripts in the image, <repo>/scripts from a checkout)."""
    candidates = [
        os.environ.get("HERMES_SCRIPTS_DIR"),
        "/app/scripts",
        os.path.join(os.path.dirname(__file__), "..", "..", "scripts"),
    ]
    for base in candidates:
        if not base:
            continue
        cand = os.path.join(base, fname)
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    raise FileNotFoundError(f"scripts/{fname} not found")


def _trim_sweep_rows(rows: list, n: int = _REGEN_SWEEP_TOP_N) -> list:
    """Cap the full ta_late_entry grid for the read surface: rank by validation
    avoided-loss-per-block (the arm-benefit metric), requiring a minimum
    blocked sample so tiny cells don't head the table."""

    def _key(r: dict) -> float:
        val = r.get("val") or {}
        blocked = val.get("blocked") or {}
        if (blocked.get("n") or 0) < 30:
            return float("-inf")
        v = val.get("avoided_loss_per_block")
        return float(v) if isinstance(v, (int, float)) else float("-inf")

    return sorted(rows, key=_key, reverse=True)[:n]


def _regen_payload() -> dict:
    """Synchronous loader (run via asyncio.to_thread). Read/report only; a
    missing or half-written report degrades to present:false, never a 500."""
    path = _regen_report_file()
    payload: dict = {
        "present": False,
        "report_file": path,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cache_ttl_s": _REGEN_TTL_S,
    }
    if not os.path.isfile(path):
        payload["note"] = "no regen replay report yet (run regen_param_sweep.py --write or trigger regen-refresh)"
        return payload
    try:
        with open(path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError) as e:
        payload["note"] = f"regen report unreadable: {e}"
        return payload
    payload["present"] = True
    payload["mtime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))
    for key in (
        "generated_at", "days", "hold_bars", "window",
        "n_candidates", "n_candidates_long72", "params_baseline",
        "plateau_picks", "axis_curves", "overlap", "relax_tier",
        "trend_filter_sweep", "daily_ext_cap_sweep",
    ):
        if key in report:
            payload[key] = report[key]
    coins = report.get("coins")
    if isinstance(coins, list):
        payload["n_coins"] = len(coins)
    grid = report.get("ta_late_entry_sweep")
    if isinstance(grid, list):
        payload["ta_late_entry_sweep_rows"] = len(grid)
        payload["ta_late_entry_sweep_top"] = _trim_sweep_rows(grid)
    return payload


def _regen_cmd(days: int, coins: str | None) -> list[str]:
    cmd = [
        sys.executable, _scripts_file("regen_param_sweep.py"),
        "--days", str(days), "--write",
        # Bounded-memory mode for the 1GB container + isolated kline cache so
        # a manual replay never perturbs the live trading caches.
        "--evict-cache", "--cache-file", _REGEN_KLINE_CACHE,
        "--out", _regen_report_file(),
    ]
    if coins:
        cmd += ["--coins", coins]
    return cmd


async def _exec_regen(cmd: list[str]) -> int:
    """Spawn the regen replay subprocess (minutes-long), streaming output to
    the run log; returns the exit code. Seamed out so tests can fake it."""
    with open(_REGEN_RUN_LOG, "ab") as logf:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=logf, stderr=asyncio.subprocess.STDOUT,
        )
        return await proc.wait()


async def _regen_run(cmd: list[str], days: int) -> None:
    from hermes_trader.dashboard import _TTL_CACHE
    try:
        rc = await _exec_regen(cmd)
        _REGEN_RUN_STATE["exit_code"] = rc
        if rc == 0:
            # Fresh report on disk: drop the TTL-cached read so the next GET
            # regen-report reflects the new replay immediately.
            _TTL_CACHE.pop(_REGEN_CACHE_KEY, None)
    except Exception as e:  # never let a spawn failure wedge the run state
        logger.warning("regen replay run failed: %s", e)
        _REGEN_RUN_STATE["error"] = str(e)
    finally:
        _REGEN_RUN_STATE["running"] = False
        _REGEN_RUN_STATE["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        session_log.append({
            "event": "shadow_arms_regen_refresh_done",
            "ts": int(time.time() * 1000),
            "days": days,
            "exit_code": _REGEN_RUN_STATE.get("exit_code"),
            "error": _REGEN_RUN_STATE.get("error"),
            "via": "web",
        })

_shadow_grade_mod = None
_shadow_grade_load_failed: Exception | None = None


def _load_shadow_grade():
    """Lazily load scripts/shadow_grade.py by path (scripts/ is not a package).

    Cached after the first successful load; a load failure is also cached and
    re-raised so callers can surface a stable 503 instead of 500."""
    global _shadow_grade_mod, _shadow_grade_load_failed
    if _shadow_grade_mod is not None:
        return _shadow_grade_mod
    if _shadow_grade_load_failed is not None:
        raise _shadow_grade_load_failed
    try:
        # /app/scripts in the image (Dockerfile COPY scripts/), <repo>/scripts
        # when running from a checkout; fall back relative to this file.
        candidates = [
            os.environ.get("HERMES_SCRIPTS_DIR"),
            "/app/scripts",
            os.path.join(os.path.dirname(__file__), "..", "..", "scripts"),
        ]
        path = None
        for base in candidates:
            if not base:
                continue
            cand = os.path.join(base, "shadow_grade.py")
            if os.path.isfile(cand):
                path = os.path.abspath(cand)
                break
        if path is None:
            raise FileNotFoundError("scripts/shadow_grade.py not found")
        scripts_dir = os.path.dirname(path)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        spec = importlib.util.spec_from_file_location("hermes_shadow_grade", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load spec for {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _shadow_grade_mod = mod
        return mod
    except Exception as e:  # bulletproof: never let a missing grader crash the API
        _shadow_grade_load_failed = e
        logger.warning("shadow_grade grader unavailable: %s", e)
        raise


def _grades_payload(windows: list[int]) -> dict:
    """Synchronous loader (run via asyncio.to_thread). Collects grades and stamps
    the cache/window provenance for the portal."""
    sg = _load_shadow_grade()
    report = sg.collect_grades(list(windows))
    report["windows_req"] = list(windows)
    report["cache_ttl_s"] = _GRADES_TTL_S
    return report


def _parse_windows(raw: str | None) -> list[int]:
    if not raw:
        return list(_DEFAULT_WINDOWS)
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            w = int(part)
        except ValueError:
            raise HTTPException(422, f"invalid window {part!r}; use comma-separated hours")
        if w <= 0 or w > 24 * 90:
            raise HTTPException(422, f"window {w} out of range (1..2160 hours)")
        out.append(w)
    return out or list(_DEFAULT_WINDOWS)


def register_shadow_arms_routes(app: FastAPI) -> None:
    """Mount the shadow-arm grading-center read + operator-refresh routes."""

    @app.get("/api/dashboard/shadow-arms/grades")
    async def shadow_arms_grades(windows: str | None = Query(None)) -> JSONResponse:
        """Latest per-arm verdicts/window stats. 60s TTL cache; anonymous-safe."""
        wins = _parse_windows(windows)
        try:
            payload = await asyncio.to_thread(
                _ttl_cached, _GRADES_CACHE_KEY, _GRADES_TTL_S,
                lambda: _grades_payload(wins),
            )
        except Exception as e:
            raise HTTPException(503, f"shadow-arm grader unavailable: {e}")
        return JSONResponse(payload)

    @app.post("/api/dashboard/shadow-arms/refresh")
    async def shadow_arms_refresh(request: Request) -> JSONResponse:
        """Force an immediate regrade (operator write), bypassing the 60s cache.

        Does NOT write a nightly history snapshot (only the cron path does, so
        manual operator refreshes cannot fabricate trend history). Audited."""
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        wins = _parse_windows(body.get("windows") if isinstance(body, dict) else None)
        try:
            payload = await asyncio.to_thread(_grades_payload, wins)
        except Exception as e:
            raise HTTPException(503, f"shadow-arm grader unavailable: {e}")
        # Warm/replace the default-window cached value so the next grades read
        # for the standard view is instant; custom windows just refresh too.
        from hermes_trader.dashboard import _TTL_CACHE
        _TTL_CACHE[_GRADES_CACHE_KEY] = (time.time(), payload)
        counts: dict[str, int] = {}
        for a in payload.get("arms", []):
            counts[a.get("verdict", "?")] = counts.get(a.get("verdict", "?"), 0) + 1
        session_log.append({
            "event": "shadow_arms_refresh",
            "ts": int(time.time() * 1000),
            "windows": wins,
            "counts": counts,
            "via": "web",
        })
        logger.info("shadow-arm grades refreshed via dashboard: %s", counts)
        return JSONResponse({"ok": True, **payload})

    @app.get("/api/dashboard/shadow-arms/grade-history")
    async def shadow_arms_grade_history(
        days: int = Query(30, ge=1, le=400),
        limit: int = Query(400, ge=1, le=400),
        source: str | None = Query(
            None, pattern="^(cron|manual)$",
            description="M9：按快照来源过滤；趋势视图传 cron 以排除手工重评污染",
        ),
    ) -> JSONResponse:
        """Nightly verdict snapshots (oldest first) for the trend view."""
        try:
            # Off the event loop: _load_shadow_grade exec_module's the grader
            # source; blocking import work must not freeze the loop.
            sg = await asyncio.to_thread(_load_shadow_grade)
        except Exception as e:
            raise HTTPException(503, f"shadow-arm grader unavailable: {e}")
        since_ms = time.time() * 1000.0 - days * 86400.0 * 1000.0

        def _read() -> list[dict]:
            return sg.read_history(since_ms=since_ms, limit=limit, source=source)

        snaps = await asyncio.to_thread(_read)
        return JSONResponse({"snapshots": snaps, "count": len(snaps), "days": days})

    @app.get("/api/dashboard/shadow-arms/backfill-summary")
    async def shadow_arms_backfill_summary() -> JSONResponse:
        """Historical backtest (backfill) aggregates per arm. 60s TTL cache;
        anonymous-safe (counts/stats only), matching the grades read posture."""
        try:
            payload = await asyncio.to_thread(
                _ttl_cached, _BACKFILL_CACHE_KEY, _BACKFILL_TTL_S, _backfill_payload,
            )
        except Exception as e:
            raise HTTPException(503, f"backfill summary unavailable: {e}")
        return JSONResponse(payload)

    @app.get("/api/dashboard/shadow-arms/regen-report")
    async def shadow_arms_regen_report() -> JSONResponse:
        """Long-horizon signal-regen replay report (walk-forward 60/20/20,
        axis curves, plateau picks, regen-vs-live overlap anchor). 60s TTL
        cache: a re-run rewrites the JSON file and is surfaced once the cache
        lapses — or immediately after a dashboard-triggered run, which drops
        the cache on success. Anonymous-safe (counts/stats only)."""
        try:
            payload = await asyncio.to_thread(
                _ttl_cached, _REGEN_CACHE_KEY, _REGEN_TTL_S, _regen_payload,
            )
        except Exception as e:
            raise HTTPException(503, f"regen replay report unavailable: {e}")
        return JSONResponse(payload)

    @app.get("/api/dashboard/shadow-arms/regen-status")
    async def shadow_arms_regen_status() -> JSONResponse:
        """Manual regen replay run state (polled by the portal while running)."""
        return JSONResponse(dict(_REGEN_RUN_STATE))

    @app.post("/api/dashboard/shadow-arms/regen-refresh")
    async def shadow_arms_regen_refresh(request: Request) -> JSONResponse:
        """Trigger a manual long-horizon regen replay (operator write).

        Runs scripts/regen_param_sweep.py --write in the background (takes
        minutes); the report endpoint picks the new file up as soon as the
        run finishes. Singleflight: a second trigger while running gets 409.
        Audited to the session log (start + completion)."""
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(422, "invalid JSON body")
        days_raw = body.get("days", 120)
        try:
            days = int(days_raw)
        except (TypeError, ValueError):
            raise HTTPException(422, f"invalid days {days_raw!r}; use an integer 30..365")
        if days < 30 or days > 365:
            raise HTTPException(422, f"days {days} out of range (30..365)")
        coins = body.get("coins")
        if coins is not None:
            coins = str(coins).strip()
            if coins and not re.fullmatch(r"[A-Za-z0-9_,.\-/]{1,300}", coins):
                raise HTTPException(422, "invalid coins; use comma-separated symbols")
            if not coins:
                coins = None
        if _REGEN_RUN_STATE.get("running"):
            raise HTTPException(
                409, "a regen replay is already running; poll regen-status",
            )
        try:
            cmd = _regen_cmd(days, coins)
        except FileNotFoundError as e:
            raise HTTPException(503, str(e))
        _REGEN_RUN_STATE.update({
            "running": True,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "finished_at": None,
            "exit_code": None,
            "days": days,
            "cmd": " ".join(cmd),
            "error": None,
        })
        task = asyncio.create_task(_regen_run(cmd, days))
        _REGEN_TASKS.add(task)
        task.add_done_callback(_REGEN_TASKS.discard)
        session_log.append({
            "event": "shadow_arms_regen_refresh",
            "ts": int(time.time() * 1000),
            "days": days,
            "coins": coins,
            "via": "web",
        })
        logger.info("regen replay triggered via dashboard: days=%s coins=%s", days, coins)
        return JSONResponse({"ok": True, **dict(_REGEN_RUN_STATE)})
