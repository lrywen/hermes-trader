"""统一敏感配置管理路由（LLM / 交易所 / 飞书）。

所有端点都经 ``_require_operator`` 鉴权；写操作 ``write=True``（走写限速）。
BFF 层进一步用 ``config:read`` / ``config:write`` 做 RBAC。

响应约定
--------
- 成功：``{"ok": true, ...}``，前端弹绿色 toast；
- 业务校验失败：422 + ``{"detail": "<错误，可能多条以；连接>"}``，前端弹红色 toast；
- 未认证：401/429/503，由全局 axios 处理。

密钥字段在响应中一律掩码；编辑时留空表示不修改。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from hermes_trader import session_log
from hermes_trader.agents import secret_store
from hermes_trader.dashboard import _require_operator

logger = logging.getLogger("hermes-dashboard")


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(422, "请求体必须是对象")
    return body


def _audit(action: str, request: Request, details: dict[str, Any] | None = None) -> None:
    """Best-effort 写 session 审计，绝不因审计失败影响主流程。"""
    try:
        user = request.headers.get("X-Portal-User", "")
        session_log.append(
            "secret_config_update",
            {"action": action, "user": user, **(details or {})},
        )
    except Exception:  # pragma: no cover
        pass


def register_secrets_routes(app: FastAPI) -> None:
    """挂载敏感配置 CRUD 路由。"""

    # ── 聚合读取：三区掩码视图 ─────────────────────────────────────────
    @app.get("/api/dashboard/secrets")
    async def get_secrets(request: Request) -> JSONResponse:
        _require_operator(request)
        return JSONResponse(secret_store.masked_view())

    # ── LLM profiles：列表 ────────────────────────────────────────────
    @app.get("/api/dashboard/secrets/llm")
    async def llm_list(request: Request) -> JSONResponse:
        _require_operator(request)
        return JSONResponse(
            {
                "profiles": secret_store.list_llm_profiles(),
                "default_profile_id": secret_store.read_secrets()["llm"].get(
                    "default_profile_id"
                ),
            }
        )

    # ── LLM profiles：新增 ────────────────────────────────────────────
    @app.post("/api/dashboard/secrets/llm")
    async def llm_create(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        body = await _json_body(request)
        data = body.get("profile") if isinstance(body.get("profile"), dict) else body
        pid, errs = secret_store.create_llm_profile(data)
        if errs:
            raise HTTPException(422, "；".join(errs))
        _audit("llm_create", request, {"id": pid})
        return JSONResponse({"ok": True, "id": pid})

    # ── LLM profiles：编辑 ────────────────────────────────────────────
    @app.put("/api/dashboard/secrets/llm/{pid}")
    async def llm_update(pid: str, request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        body = await _json_body(request)
        data = body.get("profile") if isinstance(body.get("profile"), dict) else body
        errs = secret_store.update_llm_profile(pid, data)
        if errs:
            raise HTTPException(422, "；".join(errs))
        _audit("llm_update", request, {"id": pid})
        return JSONResponse({"ok": True})

    # ── LLM profiles：删除 ────────────────────────────────────────────
    @app.delete("/api/dashboard/secrets/llm/{pid}")
    async def llm_delete(pid: str, request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        errs = secret_store.delete_llm_profile(pid)
        if errs:
            raise HTTPException(422, "；".join(errs))
        _audit("llm_delete", request, {"id": pid})
        return JSONResponse({"ok": True})

    # ── LLM profiles：设默认 ──────────────────────────────────────────
    @app.post("/api/dashboard/secrets/llm/{pid}/default")
    async def llm_set_default(pid: str, request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        errs = secret_store.set_default_llm_profile(pid)
        if errs:
            raise HTTPException(422, "；".join(errs))
        _audit("llm_set_default", request, {"id": pid})
        return JSONResponse({"ok": True})

    # ── exchange：编辑（单实例） ──────────────────────────────────────
    @app.put("/api/dashboard/secrets/exchange")
    async def exchange_update(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        body = await _json_body(request)
        data = body.get("config") if isinstance(body.get("config"), dict) else body
        errs = secret_store.update_singleton("exchange", data)
        if errs:
            raise HTTPException(422, "；".join(errs))
        _audit("exchange_update", request)
        return JSONResponse({"ok": True})

    # ── feishu：编辑（单实例） ─────────────────────────────────────────
    @app.put("/api/dashboard/secrets/feishu")
    async def feishu_update(request: Request) -> JSONResponse:
        _require_operator(request, write=True)
        body = await _json_body(request)
        data = body.get("config") if isinstance(body.get("config"), dict) else body
        errs = secret_store.update_singleton("feishu", data)
        if errs:
            raise HTTPException(422, "；".join(errs))
        _audit("feishu_update", request)
        return JSONResponse({"ok": True})
