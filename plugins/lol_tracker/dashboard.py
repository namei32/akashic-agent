from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from plugins.ui.contract import register_routes

from .settings import RUNTIME, SETTINGS

inject = (SETTINGS, RUNTIME)


class ReportRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    days: int = Field(default=7, ge=1, le=30)


def register(app, context):
    register_routes(app, context, SETTINGS, "/api/dashboard/lol_tracker/config")

    @app.get("/api/dashboard/lol_tracker/status")
    async def status():
        return await context.require(RUNTIME).status()

    @app.get("/api/dashboard/lol_tracker/history")
    async def history(days: int = Query(default=7, ge=1, le=90)):
        now = datetime.now(UTC)
        return await context.require(RUNTIME).facts(now-timedelta(days=days), now, f"近 {days} 天")

    @app.post("/api/dashboard/lol_tracker/report")
    async def report(payload: ReportRequest):
        try:
            return await context.require(RUNTIME).manual(payload.request_id, payload.days)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
