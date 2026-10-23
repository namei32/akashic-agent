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
                      model_id=config.extraction_model, max_chars=config.max_chars)
    # Operations are owned by this plugin even when invoked by an operator.
    class Port:
        status = staticmethod(ctx.entrypoint(runtime.status))
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
    async def rpc_pause(_params: BaseModel):
        runtime.paused = True
        return await runtime.status()
    async def rpc_resume(_params: BaseModel):
        runtime.paused = False
        return await runtime.status()
    async def rpc_recall(params: BaseModel):
        request = RecallQuery.model_validate(params.model_dump())
        return await runtime.recall("operator:" + request.request_id, request.session_id,
                                    request.query, limit=request.limit)
    for name, model, operation in (
        ("status", Empty, rpc_status), ("audit", Empty, rpc_audit), ("backfill", Backfill, rpc_backfill),
        ("pause", Empty, rpc_pause), ("resume", Empty, rpc_resume),
        ("query", RecallQuery, rpc_recall),
    ):
        await ctx.provide(RpcMethod.key("default-memory/" + name), RpcMethod(model, ctx.entrypoint(operation)))
    health = await ctx.health("learning", required=False)
    watcher: asyncio.Task[None] | None = None
    lock_handle = None

    async def status_command(_invocation: CommandInvocation) -> CommandResult:
        value = await runtime.status()
        return CommandResult("success", json.dumps(value, ensure_ascii=False))

    async def backfill_command(_invocation: CommandInvocation) -> CommandResult:
        runtime.paused = False
        value = await runtime.consume(retry=True)
        return CommandResult("error" if value["failures"] else "success", json.dumps(value, ensure_ascii=False))

    async def pause_command(_invocation: CommandInvocation) -> CommandResult:
        runtime.paused = True
        return CommandResult("success", "已暂停；当前模型调用完成后保存进度，原始对话保持不变。")

    for command, description, handler, readonly in (
        ("default_memory_status", "查看 Default Memory 补学与召回状态", status_command, True),
        ("default_memory_backfill", "补学全部符合学习条件的历史并重试失败项（调用模型）", backfill_command, False),
        ("default_memory_pause", "暂停 Default Memory 学习", pause_command, False),
    ):
        await ctx.require(COMMANDS).register(ctx, CommandDefinition(command, description, handler, read_only=readonly))

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
        async for _heads in ctx.require(MESSAGE_CATALOG).follow():
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
