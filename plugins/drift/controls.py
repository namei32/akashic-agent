"""Operator API for future initiative admission; existing proposals are untouched."""

from pydantic import BaseModel
from agent.plugin_composition import Context
from core.common.file_io import run_file_io
from plugins.gateway.contract import RpcMethod
from .schema import ControlRequest, EmptyRequest
from .store import DriftStore
from .initiative import Initiative


async def mount(ctx: Context, store: DriftStore, initiative: Initiative) -> None:
    async def status(_params: BaseModel) -> dict[str, object]:
        return await run_file_io(store.initiative_status)

    async def control(params: BaseModel, paused: bool) -> dict[str, object]:
        request = ControlRequest.model_validate(params.model_dump())
        receipt = await initiative.write(
            lambda: store.set_initiative_paused(
                paused, request.request_id, request.expected_version
            )
        )
        return {
            **await run_file_io(store.initiative_status),
            "control_receipt": receipt,
        }

    async def pause(params: BaseModel) -> dict[str, object]:
        return await control(params, True)

    async def resume(params: BaseModel) -> dict[str, object]:
        return await control(params, False)

    for name, model, handler in (
        ("status", EmptyRequest, status),
        ("pause", ControlRequest, pause),
        ("resume", ControlRequest, resume),
    ):
        await ctx.provide(
            RpcMethod.key("drift/initiative/" + name),
            RpcMethod(model, ctx.entrypoint(handler)),
        )
