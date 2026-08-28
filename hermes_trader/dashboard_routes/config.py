"""Operator-gated config management routes (F23).

Moved verbatim out of ``dashboard.register_routes``: config write, rolling
backup, manual snapshots, rollback, activity history and key schema. All
routes require the operator token; shared validation / apply helpers are
imported from ``hermes_trader.dashboard``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, get_origin

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from hermes_trader import session_log
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    backup_config,
    create_snapshot,
    list_snapshots,
    read_agent_config,
    restore_backup,
    restore_snapshot,
)
from hermes_trader.agents.config_schema import _ConfigPatch
from hermes_trader.dashboard import (
    _config_apply,
    _require_operator,
    _validate_config_updates,
)

logger = logging.getLogger("hermes-dashboard")


def register_config_routes(app: FastAPI) -> None:
    """Mount the operator-gated config write / backup / rollback routes."""

    # ── config write API (operator-gated) ─────────────────────────────────
    # F25/F27: the whitelist / type / range gate lives in
    # agents/config_schema.py (Pydantic model as the single source of truth),
    # shared with the terminal `set` handler, the CLI and the legacy endpoint.

    @app.post("/api/dashboard/config")
    async def dashboard_config_write(request: Request) -> JSONResponse:
        """Apply a partial config update. Operator token required.

        Body: ``{"updates": {"key": value, ...}}``.
        Validates types/ranges, backs up the current config, writes atomically,
        and appends an audit event to the session log.
        """
        _require_operator(request, write=True)
        # F7: a malformed JSON body must return 422, not a 500.
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "invalid JSON body")
        updates = body.get("updates")
        if not isinstance(updates, dict) or not updates:
            raise HTTPException(400, "updates must be a non-empty object")

        errors = _validate_config_updates(updates)
        if errors:
            raise HTTPException(422, json.dumps({"errors": errors}))

        def _apply() -> dict[str, Any]:
            # F20: _config_apply holds the process-wide config lock across the
            # whole read-modify-write so concurrent updates can't clobber.
            return _config_apply(updates, backup=True)

        result = await asyncio.to_thread(_apply)
        session_log.append({
            "event": "config_update",
            "ts": int(time.time() * 1000),
            "updates": {k: v for k, v in updates.items()},
            "old": result["old"],
            "via": "web",
        })
        logger.info("config update via dashboard: %s", list(updates.keys()))
        return JSONResponse({"ok": True, "applied": result["new"]})

    @app.get("/api/dashboard/config/backup")
    async def dashboard_config_backup(request: Request) -> JSONResponse:
        """Return the last backed-up config (before the last write)."""
        _require_operator(request)
        bak = await asyncio.to_thread(backup_config)
        if bak is None:
            return JSONResponse({"available": False, "config": None})
        return JSONResponse({"available": True, "config": bak})

    @app.post("/api/dashboard/config/backup")
    async def dashboard_config_snapshot(request: Request) -> JSONResponse:
        """Create a named manual snapshot of the current config.

        Unlike the rolling ``.bak`` (overwritten on every write), manual
        snapshots are immutable, timestamped, and retained up to a cap so the
        operator can checkpoint a known-good config before risky edits.
        """
        _require_operator(request, write=True)
        try:
            body = await request.json()
        except Exception:
            body = {}
        reason = "manual"
        if isinstance(body, dict):
            r = body.get("reason")
            if isinstance(r, str) and r.strip():
                reason = r.strip()[:80]
        try:
            snap = await asyncio.to_thread(create_snapshot, reason)
        except OSError as e:
            raise HTTPException(500, f"snapshot failed: {e}")
        session_log.append({
            "event": "config_snapshot",
            "ts": int(time.time() * 1000),
            "snapshot_id": snap["id"],
            "reason": reason,
        })
        logger.info("manual config snapshot created: %s", snap["id"])
        return JSONResponse({"ok": True, "snapshot": snap})

    @app.post("/api/dashboard/config/rollback")
    async def dashboard_config_rollback(request: Request) -> JSONResponse:
        """Restore the config.

        Body is optional. ``{"id": "snap-<ts>"}`` restores a specific manual
        snapshot; with no body (or no id) it restores the rolling ``.bak``
        produced by the last write.
        """
        _require_operator(request, write=True)
        target = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                target = body.get("id")
        except Exception:
            target = None

        if isinstance(target, str) and target.startswith("snap-"):
            try:
                ts = int(target[len("snap-"):])
            except ValueError:
                raise HTTPException(400, "invalid snapshot id")
            ok = await asyncio.to_thread(restore_snapshot, ts)
            if not ok:
                raise HTTPException(404, f"snapshot {target} not found")
            restored_from = target
        else:
            ok = await asyncio.to_thread(restore_backup)
            if not ok:
                raise HTTPException(409, "no backup available to restore")
            restored_from = "backup"
        session_log.append({
            "event": "config_rollback",
            "ts": int(time.time() * 1000),
            "from": restored_from,
        })
        logger.warning("config rolled back via dashboard (from=%s)", restored_from)
        cfg = await asyncio.to_thread(read_agent_config)
        return JSONResponse({"ok": True, "from": restored_from, "config": cfg})

    @app.get("/api/dashboard/config/history")
    async def dashboard_config_history(request: Request) -> JSONResponse:
        """Recent config activity plus available manual snapshots.

        Returns ``{"history": [...], "snapshots": [...]}``. History items are
        config_update / config_rollback / config_snapshot events (newest
        last, capped at 30). Snapshots are manual recovery points (newest
        first) and can be passed as ``id`` to the rollback endpoint.
        """
        _require_operator(request)
        events = await asyncio.to_thread(session_log.tail, 200)
        history = [
            e for e in (events or [])
            if e.get("event") in (
                "config_update", "config_rollback", "config_snapshot",
            )
        ][-30:]
        snapshots = await asyncio.to_thread(list_snapshots)
        return JSONResponse({"history": history, "snapshots": snapshots})

    @app.get("/api/dashboard/config/schema")
    async def dashboard_config_schema() -> JSONResponse:
        """Return key metadata (type + default) for building the edit form."""
        schema: dict[str, Any] = {}
        for key, default in CANONICAL_DEFAULTS.items():
            field = _ConfigPatch.model_fields.get(key)
            annotation = field.annotation if field is not None else type(default)
            if annotation is int:
                type_name = "int"
            elif annotation is float:
                type_name = "float"
            elif annotation is bool:
                type_name = "bool"
            elif annotation is str:
                type_name = "str"
            elif annotation is list or get_origin(annotation) is list or isinstance(default, list):
                type_name = "list"
            elif annotation is dict or get_origin(annotation) is dict or isinstance(default, dict):
                type_name = "object"
            else:
                type_name = "any"
            schema[key] = {"type": type_name, "default": default}
        return JSONResponse(schema)
