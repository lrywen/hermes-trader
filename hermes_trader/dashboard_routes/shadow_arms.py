"""SHADOW risk-arm grading-center routes (Audit 2026-09-07, M1).

Surfaces the nightly shadow-arm grader (scripts/shadow_grade.py) over the
dashboard API so the portal can render a per-arm grading/recommendation view:

  * GET  /api/dashboard/shadow-arms/grades        — latest arm verdicts + stats
  * POST /api/dashboard/shadow-arms/refresh       — force a regrade (operator write)
  * GET  /api/dashboard/shadow-arms/grade-history — nightly verdict trend snapshots

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
import logging
import os
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
    ) -> JSONResponse:
        """Nightly verdict snapshots (oldest first) for the trend view."""
        try:
            sg = _load_shadow_grade()
        except Exception as e:
            raise HTTPException(503, f"shadow-arm grader unavailable: {e}")
        since_ms = time.time() * 1000.0 - days * 86400.0 * 1000.0

        def _read() -> list[dict]:
            return sg.read_history(since_ms=since_ms, limit=limit)

        snaps = await asyncio.to_thread(_read)
        return JSONResponse({"snapshots": snaps, "count": len(snaps), "days": days})
