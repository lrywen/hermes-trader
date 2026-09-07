"""Audit-ledger routes (Audit 2026-09-07, M4).

Surfaces the tamper-evident event-log hash chain and the nightly fill
reconciliation status over the dashboard API so the portal can render the
F5 ledger-integrity card (Audit page) and the F6 reconcile-status card
(Operator page):

  * GET /api/dashboard/ledger/verify      — replay the SHA-256 hash chain
  * GET /api/dashboard/ledger/events      — recent events (type filter + limit)
  * GET /api/dashboard/reconcile/status   — latest nightly reconcile report

Posture: all three endpoints are READ-only and anonymous-safe at the trader
boundary (counts, verdicts, chain status — no secrets), matching the shadow
paper-ledger endpoints. Fine-grained RBAC (ledger: admin:audit, reconcile:
operator:mode) is enforced one hop up in the portal BFF _PATH_RULES; these
routes never mutate anything. The reconcile status file is written best-effort
by the nightly cron path (scripts/reconcile_fills.py) — this module only
reads it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from hermes_trader import event_log

logger = __import__("logging").getLogger("hermes-dashboard")

# Status file written by scripts/reconcile_fills.py (best-effort, cron path).
RECONCILE_STATUS_FILE = os.environ.get(
    "HERMES_RECONCILE_STATUS_FILE", "/data/reconcile_status.json"
)


def _read_reconcile_status() -> dict[str, Any]:
    """Synchronous status-file read (run via asyncio.to_thread).

    Raises FileNotFoundError when the cron has never written a status (the
    endpoint maps that to 404 so the UI can show a "never run" placeholder),
    and ValueError when the file exists but is unparseable (mapped to 503)."""
    with open(RECONCILE_STATUS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def register_audit_routes(app: FastAPI) -> None:
    """Mount the hash-chain + reconcile-status read routes."""

    @app.get("/api/dashboard/ledger/verify")
    async def ledger_verify() -> JSONResponse:
        """Replay the events.jsonl SHA-256 hash chain across the active log
        and rotated backups. verify_chain is best-effort and never raises; a
        broken/tampered chain is reported via ok=false + errors[]."""
        result = await asyncio.to_thread(event_log.verify_chain)
        result["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return JSONResponse(result)

    @app.get("/api/dashboard/ledger/events")
    async def ledger_events(
        event_type: str | None = Query(None, alias="event_type"),
        limit: int = Query(100, ge=1, le=500),
    ) -> JSONResponse:
        """Recent ledger events, oldest-first. event_type filters by event
        name; limit keeps the newest N (query_events scans active + rotated
        files and returns ascending order — slicing is done at the route
        layer because the store has no limit parameter). The internal _dt
        parse-helper field is stripped before serialization."""
        events = await asyncio.to_thread(
            event_log.query_events, event_type=event_type
        )
        total = len(events)
        trimmed = events[-limit:]
        for rec in trimmed:
            rec.pop("_dt", None)
        return JSONResponse(
            {"events": trimmed, "count": len(trimmed),
             "total_scanned": total, "limited": total > len(trimmed)}
        )

    @app.get("/api/dashboard/reconcile/status")
    async def reconcile_status() -> JSONResponse:
        """Latest nightly fill-reconciliation report written by the cron path.
        404 when reconciliation has never run (no status file) — the portal
        renders that as an explicit 'never run' state rather than an error."""
        try:
            payload = await asyncio.to_thread(_read_reconcile_status)
        except FileNotFoundError:
            raise HTTPException(404, "reconciliation has never run")
        except (json.JSONDecodeError, OSError) as e:
            raise HTTPException(503, f"reconcile status unreadable: {e}")
        return JSONResponse(payload)
