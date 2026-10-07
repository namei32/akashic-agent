"""Expose this Web module's read-only routes in its exact request owner."""
from collections.abc import Mapping

from fastapi import FastAPI, HTTPException, Query

from agent.plugin_composition import DashboardContext
from .directory import DIRECTORY

inject = (DIRECTORY,)


def register(app: FastAPI, context: DashboardContext) -> None:
    @app.get("/api/dashboard/runtime/{section}")
    async def read(
        section: str,
        document_id: str | None = Query(None, max_length=1024),
        job_id: str | None = Query(None, max_length=1024),
        owner_id: str | None = Query(None, max_length=256),
        name: str | None = Query(None, max_length=256),
    ) -> dict[str, object]:
        fields = {"document": {"document_id": document_id}, "job": {"job_id": job_id},
                  "mcp": {"owner_id": owner_id, "name": name}}
        values: dict[str, object] = dict(fields.get(section, {}))
        if any(value is None for value in values.values()):
            raise HTTPException(400, "缺少目录项目标识")
        try:
            payload = await context.require(DIRECTORY).read(section, values)
        except KeyError as error:
            raise HTTPException(404, "未知运行目录") from error
        unavailable = payload.get("unavailable")
        if isinstance(unavailable, Mapping):
            code = str(unavailable.get("code", ""))
            raise HTTPException(404 if code.endswith("_not_found") else 409,
                                str(unavailable.get("message", "目录检查服务不可用")))
        return payload
