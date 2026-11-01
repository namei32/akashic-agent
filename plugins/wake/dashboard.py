"""Expose Wake's durable attempts and Message-backed flow evidence."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Literal, cast

from fastapi import FastAPI, HTTPException, Query
from pydantic import ValidationError

from agent.plugin_composition import DashboardContext

from .plugin import WAKE_DASHBOARD
from .runtime import DashboardView


from plugins.ui.contract import UI
from .settings import SETTINGS, InitiativeUnavailable, Settings

inject = (UI, SETTINGS, WAKE_DASHBOARD,)


def _view(context: DashboardContext) -> DashboardView:
    view = context.require(WAKE_DASHBOARD)()
    if view is None:
        raise RuntimeError("Wake runtime 尚未启动")
    return view


def _latest_initiative(view: DashboardView) -> dict[str, object] | None:
    attempt = next((item for item in view.list_attempts(50) if item.get("owner") == "drift"), None)
    if attempt is None:
        return None
    identity = attempt.get("attempt_id")
    latest = {key: attempt.get(key) for key in ("outcome", "fired_at", "completed_at")}
    latest.update(memory_status=None, delivery_status=None, reason=None)
    if not isinstance(identity, str):
        return latest
    run = next((item for item in view.list_runs(50) if item.get("run_id") == identity), None)
    detail = run.get("decision_detail") if run else None
    if isinstance(detail, str):
        if detail == "主动机会、授权或记忆材料已失效":
            latest["reason"] = "invalidated"
        elif detail.startswith("主动记忆材料不可用："):
            code = detail.removeprefix("主动记忆材料不可用：")
            if code in {"no_user_input", "unscoped_context", "source_unavailable", "scoped_history_unavailable"}:
                latest["reason"] = code
    try:
        flow = view.flow(identity)
    except RuntimeError:
        latest["delivery_status"] = "unavailable"
        return latest
    if flow is None:
        return latest
    delivery = flow.get("delivery")
    if isinstance(delivery, Mapping) and isinstance(delivery.get("status"), str):
        latest["delivery_status"] = delivery["status"]
    messages = flow.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, Mapping) or not isinstance(message.get("text"), str):
                continue
            try:
                phase = json.loads(message["text"])
            except ValueError:
                continue
            data = phase.get("data") if isinstance(phase, dict) and phase.get("stage") == "drift" else None
            memory = data.get("memory") if isinstance(data, dict) else None
            status = memory.get("status") if isinstance(memory, dict) else None
            if isinstance(status, str) and status in {"ok", "empty", "degraded"}:
                latest["memory_status"] = status
    return latest


def register(app: FastAPI, context: DashboardContext) -> None:
    """Register read-only routes owned by the live Wake runtime."""
    context.require(UI).register_configuration(app, context, SETTINGS, "/api/dashboard/wake/config")

    async def initiative(action: str, params: Mapping[str, object]) -> dict[str, object]:
        settings = cast(Settings, context.require(SETTINGS))
        try:
            return await settings.initiative(action, params)
        except ValidationError as error:
            raise HTTPException(422, error.errors(include_input=False, include_url=False, include_context=False)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except InitiativeUnavailable as error:
            raise HTTPException(503, str(error)) from error

    @app.get("/api/dashboard/wake/initiative")
    async def initiative_status() -> dict[str, object]:
        settings = cast(Settings, context.require(SETTINGS))
        wake = await settings.read()
        return {"control": await initiative("status", {}), "consent": await initiative("memory-status", {}),
                "config": await initiative("config", {}), "wake": wake,
                "memory_ready": await settings.memory_initiative_ready(), "latest": _latest_initiative(_view(context))}

    @app.post("/api/dashboard/wake/initiative/{action}")
    async def initiative_control(action: Literal["pause", "resume", "memory-consent", "configure"],
                                 params: dict[str, object]) -> dict[str, object]:
        return await initiative(action, params)

    @app.get("/api/dashboard/wake/initiative/config-receipts/{request_id}")
    async def initiative_receipt(request_id: str) -> dict[str, object]:
        try:
            return await initiative("config-receipt", {"request_id": request_id})
        except KeyError as error:
            raise HTTPException(404, "配置回执不存在") from error

    @app.get("/api/dashboard/wake/attempts")
    async def list_attempts(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, object]:
        view = _view(context)
        rows = view.list_attempts(page_size, offset=(page - 1) * page_size)
        return {"items": rows, "total": view.count_attempts(), "page": page, "page_size": page_size}

    @app.get("/api/dashboard/wake/attempts/{attempt_id}")
    async def get_attempt(attempt_id: str) -> Mapping[str, object]:
        item = _view(context).get_attempt(attempt_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Wake 定时检查不存在")
        return item

    @app.get("/api/dashboard/wake/runs")
    async def list_runs(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, object]:
        view = _view(context)
        rows = view.list_runs(page_size, offset=(page - 1) * page_size)
        return {"items": rows, "total": view.count_runs(), "page": page, "page_size": page_size}

    @app.get("/api/dashboard/wake/runs/{run_id}")
    async def get_run(run_id: str) -> Mapping[str, object]:
        item = _view(context).get_run(run_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Wake 判断记录不存在")
        return item
