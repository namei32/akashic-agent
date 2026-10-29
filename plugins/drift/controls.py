"""Operator API for future initiative admission; existing proposals are untouched."""

from pydantic import BaseModel
from agent.plugin_composition import Context
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from core.common.file_io import run_file_io
from plugins.gateway.contract import RpcMethod
from .schema import ControlRequest, EmptyRequest, ProducerConfig, ProducerRequest
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


async def mount_configuration(ctx: Context) -> None:
    """Use the normal configuration owner for an explicit, versioned opt-in."""
    async def read(_params: BaseModel) -> dict[str, object]:
        return {**ctx.require(PLUGIN_CONFIG).read(ctx),
                "producer": ProducerConfig.model_validate(ctx.config).model_dump()}

    async def configure(params: BaseModel) -> dict[str, object]:
        request = ProducerRequest.model_validate(params.model_dump())
        config = request.model_dump(exclude={"request_id", "expected_input"})
        return await ctx.require(PLUGIN_CONFIG).apply(
            ctx, request_id=request.request_id, expected_input=request.expected_input,
            config={**ctx.config, **config})

    for name, model, handler in (("config", EmptyRequest, read),
                                 ("configure", ProducerRequest, configure)):
        await ctx.provide(RpcMethod.key("drift/initiative/" + name),
                          RpcMethod(model, ctx.entrypoint(handler)))
