"""Memory status and operator controls through the memory owner's public port."""
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import DashboardContext
from .contract import DEFAULT_MEMORY

inject = (DEFAULT_MEMORY,)


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["pause", "resume", "retry"]
    request_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=0)


def register(app: FastAPI, context: DashboardContext) -> None:
    @app.get("/api/dashboard/default-memory")
    async def read() -> JSONResponse:
        return JSONResponse(await context.require(DEFAULT_MEMORY).status(), headers={"Cache-Control": "no-store"})

    @app.post("/api/dashboard/default-memory/control")
    async def control(request: Control) -> dict[str, object]:
        memory = context.require(DEFAULT_MEMORY)
        try:
            if request.action == "retry":
                receipt = await memory.retry_failed(request_id=request.request_id, expected_version=request.expected_version)
            else:
                receipt = await memory.set_paused(request.action == "pause", request_id=request.request_id,
                                                  expected_version=request.expected_version)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {**await memory.status(), "control_receipt": receipt}
