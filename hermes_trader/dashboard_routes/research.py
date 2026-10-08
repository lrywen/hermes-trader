"""Funding-carry shadow-forward research routes.

Surfaces the forward state produced by ``scripts/funding_carry_forward.py``
(``${HERMES_DATA_DIR}/funding-carry-forward.json``) over the dashboard API so
the portal can render the three-lens (theoretical-neutral / perp-hedge /
bare-directional) forward monitor.

  * GET /api/dashboard/research/funding-carry-forward — signals + rolling summary

Read-only/INERT posture: this endpoint only reads a regenerable research file;
it never writes config, changes a gate, or places orders. The file contains no
secrets. It is operator-gated at the trader boundary (``_require_operator``),
and the portal BFF tightens RBAC to ``dashboard:read``.
"""

from __future__ import annotations

import json
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hermes_trader.dashboard import _require_operator, _ttl_cached

logger = logging.getLogger("hermes-dashboard")

_TTL_S = 10.0
_CACHE_KEY = "research_funding_carry_forward"


def _state_file() -> str:
    return os.environ.get(
        "HERMES_FUNDING_FORWARD_FILE",
        os.path.join(os.environ.get("HERMES_DATA_DIR", "/data"),
                     "funding-carry-forward.json"),
    )


def _payload() -> dict:
    path = _state_file()
    payload: dict = {
        "present": False,
        "file": path,
        "fetched_at": int(time.time() * 1000),
        "summary": {},
        "signals": [],
    }
    if not os.path.isfile(path):
        payload["note"] = "forward state file not present yet"
        return payload
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError) as e:
        payload["note"] = f"forward state unreadable: {e}"
        return payload

    signals = state.get("signals", [])
    payload["present"] = True
    payload["signals"] = signals
    payload["counts"] = {
        "total": len(signals),
        "open": sum(1 for s in signals if s.get("status") == "open"),
        "settled": sum(1 for s in signals if s.get("status") == "settled"),
    }

    # Per-lens rolling aggregates over settled signals.
    summary: dict = {}
    for key in ("theoretical_neutral", "perp_hedge", "bare_directional"):
        xs = [s[key] for s in signals
              if s.get("status") == "settled" and isinstance(s.get(key), (int, float))]
        if xs:
            summary[key] = {
                "n": len(xs),
                "mean": round(sum(xs) / len(xs), 2),
                "win": round(sum(1 for v in xs if v > 0) / len(xs), 4),
            }
    payload["summary"] = summary
    return payload


def register_research_routes(app: FastAPI) -> None:
    @app.get("/api/dashboard/research/funding-carry-forward")
    async def funding_carry_forward(request: Request) -> JSONResponse:
        # Operator-gated read at the trader boundary; portal BFF enforces the
        # finer dashboard:read RBAC before proxying here.
        _require_operator(request, write=False)
        data = _ttl_cached(_CACHE_KEY, _TTL_S, _payload)
        return JSONResponse(data)
