"""Modern Default Memory: distill facts, retrieve summaries, retain raw history."""
from __future__ import annotations

import asyncio
import fcntl
import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from agent.plugin_composition import Context, RUNTIME_STARTED, RUNTIME_STOPPING
from plugins.akasha.contract import EMBEDDING_MEMORY_PLUGIN, MEMORY_SCOPES
from plugins.commands.contract import COMMANDS, CommandDefinition, CommandInvocation, CommandResult
from plugins.content.contract import CONTENT
from plugins.context.contract import MATERIALS_V4
from plugins.ledger.contract import MESSAGE_CATALOG
from plugins.models.contract import CHAT_MODELS, EMBEDDINGS
from plugins.tools.contract import TOOLS
from plugins.turn_projection.contract import TURN_PROJECTION
from .io import run_io
from .contract import DEFAULT_MEMORY
from .runtime import Runtime
from .store import Store
from .tools import ForgetArgs, MemoryTool, RecallArgs, RememberArgs

api_version = 3
name = "default_memory"
version = "1.0.0"
desc = "提炼结构化长期记忆，使用向量与关键词融合召回"
workspace_files = ("memory/default-v2.db", "memory/default-v2.lock")
inject = (CHAT_MODELS, EMBEDDINGS, MESSAGE_CATALOG, TURN_PROJECTION,
          CONTENT, MEMORY_SCOPES, MATERIALS_V4, TOOLS, COMMANDS)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    mode: Literal["staging", "active"] = "staging"
    auto_learn: bool = False
    extraction_model: str | None = None
    sources: tuple[str, ...] = Field(default=("conversation", "programmatic", "legacy-unattributed"), min_length=1)
    max_chars: int = Field(default=6000, ge=500, le=20000)


async def apply(ctx: Context) -> None:
    config = Config.model_validate(ctx.config)
    if not config.enabled:
        return
    store = Store(ctx.workspace_file("memory/default-v2.db"))
    runtime = Runtime(store, ctx.require(MESSAGE_CATALOG), ctx.require(TURN_PROJECTION),
                      ctx.require(CONTENT), ctx.require(MEMORY_SCOPES), ctx.require(CHAT_MODELS),
                      ctx.require(EMBEDDINGS), sources=config.sources,
                      model_id=config.extraction_model, max_chars=config.max_chars, auto_learn=config.auto_learn)
    # Operations are owned by this plugin even when invoked by an operator.
    class Port:
        status = staticmethod(ctx.entrypoint(runtime.status))
        set_paused = staticmethod(ctx.entrypoint(runtime.set_paused))
        consume = staticmethod(ctx.entrypoint(runtime.consume))
        audit = staticmethod(ctx.entrypoint(runtime.audit))
        recall = staticmethod(ctx.entrypoint(runtime.recall))
    await ctx.provide(DEFAULT_MEMORY, Port())

    from plugins.gateway.contract import RpcMethod
    class Empty(BaseModel):
        model_config = ConfigDict(extra="forbid")
    class Backfill(Empty):
        limit: int | None = Field(default=None, ge=1)
        retry: bool = False
    class Control(Empty):
        request_id: str | None = Field(default=None, min_length=1, max_length=128)
        expected_version: int | None = Field(default=None, ge=0, strict=True)
    class RecallQuery(Empty):
        request_id: str = Field(min_length=1, max_length=128)
        session_id: str = Field(min_length=1)
        query: str = Field(min_length=1, max_length=16000)
        limit: int = Field(default=8, ge=1, le=40)
    async def rpc_status(_params: BaseModel):
        return await runtime.status()
    async def rpc_audit(_params: BaseModel):
        return await runtime.audit()
    async def rpc_backfill(params: BaseModel):
        request = Backfill.model_validate(params.model_dump())
        return await runtime.consume(limit=request.limit, retry=request.retry)
    async def rpc_control(params: BaseModel, paused: bool):
        request = Control.model_validate(params.model_dump())
        receipt = await runtime.set_paused(paused, request_id=request.request_id,
                                           expected_version=request.expected_version)
        return {**await runtime.status(), "control_receipt": receipt}
    async def rpc_pause(params: BaseModel):
        return await rpc_control(params, True)
    async def rpc_resume(params: BaseModel):
        return await rpc_control(params, False)
    async def rpc_recall(params: BaseModel):
        request = RecallQuery.model_validate(params.model_dump())
        return await runtime.recall("operator:" + request.request_id, request.session_id,
                                    request.query, limit=request.limit)
    for name, model, operation in (
        ("status", Empty, rpc_status), ("audit", Empty, rpc_audit), ("backfill", Backfill, rpc_backfill),
        ("pause", Control, rpc_pause), ("resume", Control, rpc_resume),
        ("query", RecallQuery, rpc_recall),
    ):
        await ctx.provide(RpcMethod.key("default-memory/" + name), RpcMethod(model, ctx.entrypoint(operation)))
    health = await ctx.health("learning", required=False)
    watcher: asyncio.Task[None] | None = None
    lock_handle = None

    async def status_command(_invocation: CommandInvocation) -> CommandResult:
        value = await runtime.status()
        return CommandResult("success", json.dumps(value, ensure_ascii=False))

    async def backfill_command(invocation: CommandInvocation) -> CommandResult:
        option = invocation.raw_input.strip()
        if option not in {"", "retry"}:
            return CommandResult("error", "用法：/default_memory_backfill [retry]")
        value = await runtime.consume(retry=option == "retry")
        if value["paused"]:
            return CommandResult("error", "学习仍处于暂停状态；请先 /default_memory_resume，再执行补学。")
        return CommandResult("error" if value["failures"] else "success", json.dumps(value, ensure_ascii=False))

    async def control_command(invocation: CommandInvocation, paused: bool) -> CommandResult:
        request_id = ("command:" + invocation.name + ":" + invocation.message_id
                      if invocation.message_id else None)
        receipt = await runtime.set_paused(paused, request_id=request_id)
        current = await runtime.status()
        label = "暂停" if paused else "恢复"
        state = "暂停" if current["paused"] else "运行"
        return CommandResult("success", f"{label}请求已执行（回执版本 {receipt['version']}）；"
                             f"当前学习状态：{state}（版本 {current['control_version']}）。"
                             + ("已开始的轮次完成后停止，重启后保持暂停；已有记忆仍可召回。"
                                if current["paused"] else
                                "自动学习开启时会继续处理积压轮次；/default_memory_status 查看状态。"))

    async def pause_command(invocation: CommandInvocation) -> CommandResult:
        return await control_command(invocation, True)

    async def resume_command(invocation: CommandInvocation) -> CommandResult:
        return await control_command(invocation, False)

    for command, description, handler, readonly in (
        ("default_memory_status", "查看 Default Memory 学习、暂停与召回状态", status_command, True),
        ("default_memory_backfill", "补学符合条件的历史；retry 显式重试失败项", backfill_command, False),
        ("default_memory_pause", "持久暂停学习，保持已有记忆召回", pause_command, False),
        ("default_memory_resume", "恢复学习并处理积压轮次", resume_command, False),
    ):
        await ctx.require(COMMANDS).register(ctx, CommandDefinition(
            command, description, handler, read_only=readonly,
            recover=handler if command in {"default_memory_pause", "default_memory_resume"} else None,
            input_hint="[retry]" if command == "default_memory_backfill" else None,
        ))

    if config.mode == "active":
        await ctx.provide(EMBEDDING_MEMORY_PLUGIN, object())
        await ctx.require(MATERIALS_V4).register(ctx, name="default_memory", kind="recall",
                                               prepare=runtime.prepare, priority=400)
        tools = ctx.require(TOOLS)
        await tools.declare_group(ctx, description=desc)
        for action, schema, description in (
            ("recall", RecallArgs, "查询提炼后的长期记忆，返回稳定记忆 ID"),
            ("remember", RememberArgs, "仅在用户明确要求记住时保存长期事实"),
            ("forget", ForgetArgs, "将用户明确撤回的记忆 ID 标记失效，保留原始对话"),
        ):
            @asynccontextmanager
            async def open_tool(_captured: Mapping[str, object], action: str = action) -> AsyncGenerator[MemoryTool]:
                yield MemoryTool(runtime, action)
            await tools.register(ctx, name=action + "_memory", description=description,
                                 parameters=schema.model_json_schema(), open=open_tool, idempotent=True,
                                 parallel=action == "recall")

    async def follow() -> None:
        async def notifications() -> None:
            async for _heads in ctx.require(MESSAGE_CATALOG).follow():
                runtime.changed.set()

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(notifications(), name="default-memory-catalog")
            while True:
                await runtime.changed.wait()
                runtime.changed.clear()
                async with ctx.runtime_scope():
                    result = await runtime.consume()
                    if result["failures"]:
                        health.degrade(f"Default Memory 有 {len(runtime.errors)} 个待重试轮次；/default_memory_status 查看")
                    else:
                        health.recover()

    async def start(_event: object) -> None:
        nonlocal lock_handle, watcher
        path = ctx.workspace_file("memory/default-v2.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = path.open("a+")
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await run_io(store.initialize)
            runtime.errors = await run_io(store.failures)
        except BaseException:
            lock_handle.close()
            lock_handle = None
            raise
        if config.auto_learn:
            watcher = await ctx.spawn(follow(), name="default-memory-learning")

    async def stop(_event: object = None) -> None:
        nonlocal lock_handle
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
        if lock_handle is not None:
            lock_handle.close()
            lock_handle = None

    await ctx.on(RUNTIME_STARTED, start)
    await ctx.on(RUNTIME_STOPPING, stop)
    await ctx.effect(lambda: stop, label="default-memory-store")
