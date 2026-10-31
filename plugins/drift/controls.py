"""Operator API for future initiative admission; existing proposals are untouched."""

from pydantic import BaseModel
from agent.plugin_composition import Context
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from core.common.file_io import run_file_io
from plugins.channels.contract import private_target_key
from plugins.gateway.contract import RpcMethod
from plugins.wake.contract import WAKE_CONTACT
from .schema import ControlRequest, EmptyRequest, MemoryConsentRequest, ProducerConfig, ProducerRequest
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

    async def memory_status(_params: BaseModel) -> dict[str, object]:
        return await run_file_io(store.memory_consent)

    async def memory_consent(params: BaseModel) -> dict[str, object]:
        request = MemoryConsentRequest.model_validate(params.model_dump())
        receipt = await run_file_io(lambda: store.memory_consent_receipt(request))
        if receipt is None and request.enabled:
            target = request.target
            assert target is not None
            with ctx.borrow(WAKE_CONTACT) as contact:
                if contact is None or contact().target() != target.model_dump():
                    raise ValueError("请选择当前 Wake 的精确联系目标")
                with ctx.borrow(private_target_key(target.channel)) as verifier:
                    if verifier is None:
                        raise ValueError("该渠道无法证明目标为私人会话")
                    async with verifier.guard(target.recipient, target.session_id) as private:
                        if not private:
                            raise ValueError("该目标不是可用的私人会话")
                        receipt = await initiative.write(lambda: store.set_memory_consent(request))
        elif receipt is None:
            receipt = await initiative.write(lambda: store.set_memory_consent(request))
        return {**await run_file_io(store.memory_consent), "control_receipt": receipt}

    for name, model, handler in (
        ("status", EmptyRequest, status),
        ("pause", ControlRequest, pause),
        ("resume", ControlRequest, resume),
        ("memory-status", EmptyRequest, memory_status),
        ("memory-consent", MemoryConsentRequest, memory_consent),
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
