from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from agent.plugin_composition import (
    EMBEDDING_MEMORY_PLUGIN,
    EMBEDDINGS,
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    Context,
)
from agent.plugin_composition.bindings import BINDINGS
from agent.plugin_composition.commands import COMMANDS, CommandDefinition, CommandResult
from agent.plugin_composition.messages import MESSAGE_CATALOG, MESSAGE_EMBEDDINGS
from agent.plugin_contracts.content import CONTENT
from agent.plugin_contracts.context import MATERIALS_V4
from agent.plugin_contracts.proactive import SEMANTIC_INTEREST
from agent.plugin_contracts.tools import TOOLS
from agent.plugin_contracts.turns import TURN_PROJECTION

from .client import HindsightClient
from .config import Config
from .interest import Interest
from .runtime import RecallArguments, RecallTool, Runtime, check_recall
from .sources import PROACTIVE_FEEDBACK_HISTORY
from .store import Journal

api_version = 3
name = "hindsight"
version = "1.0.0"
desc = "Hindsight 记忆、持久反馈消费及 Wake 兼容召回/兴趣服务"
inject = (
    MESSAGE_CATALOG,
    MESSAGE_EMBEDDINGS,
    TURN_PROJECTION,
    CONTENT,
    MATERIALS_V4,
    TOOLS,
    BINDINGS,
    EMBEDDINGS,
)
logger = logging.getLogger(__name__)


async def apply(ctx: Context) -> None:
    config = Config.model_validate(ctx.config)
    if not config.enabled:
        return
    catalog, projection = ctx.require(MESSAGE_CATALOG), ctx.require(TURN_PROJECTION)
    content = ctx.require(CONTENT)
    interest = Interest(
        config,
        catalog,
        projection,
        ctx.require(MESSAGE_EMBEDDINGS),
        ctx.require(EMBEDDINGS),
        content.legacy_post_commit_effect,
    )
    await ctx.provide(EMBEDDING_MEMORY_PLUGIN, object())
    await ctx.provide(SEMANTIC_INTEREST, interest)
    await ctx.effect(lambda: interest.close, label="interest")
    health = await ctx.health("memory", required=False)
    health.degrade("Hindsight 尚未启动")
    client = HindsightClient(config, api_key=os.environ.get(config.api_key_env, ""))
    runtime = Runtime(
        config,
        Journal(
            ctx.data_root
            / (
                "hindsight.sqlite3"
                if config.ledger_namespace == "default"
                else "hindsight-" + config.ledger_namespace + ".sqlite3"
            ),
            config.storage_identity(),
        ),
        client,
        catalog,
        projection,
        content.legacy_post_commit_effect,
        health=health,
    )
    await ctx.effect(lambda: runtime.close, label="hindsight-runtime")

    async def attach_feedback(child: Context) -> None:
        async def pull(cursor: int, count: int):
            from core.common.file_io import run_file_io

            async with child.runtime_scope():
                history = child.require(PROACTIVE_FEEDBACK_HISTORY)
                return await run_file_io(
                    lambda: history.page(after_cursor=cursor, max_items=count)
                )

        protected = child.entrypoint(pull)

        def attach():
            runtime.history = protected
            return lambda: setattr(runtime, "history", None)

        await child.effect(attach, label="feedback-history")

    if config.feedback_enabled:
        await ctx.inject(
            (PROACTIVE_FEEDBACK_HISTORY,), attach_feedback, name="feedback-history"
        )

    async def prepare(snapshot, source):
        return await runtime.prepare(
            snapshot, source, ctx.require(BINDINGS), ctx.runtime.plugin_id
        )

    await ctx.require(MATERIALS_V4).register(
        ctx, name="hindsight", kind="recall", priority=400, prepare=prepare
    )
    await content.register(
        ctx, {"name": "hindsight", "content": {"hindsight.recall": check_recall}}
    )
    tools = ctx.require(TOOLS)
    await tools.declare_group(ctx, description=desc)

    @asynccontextmanager
    async def open_recall(_captured):
        yield RecallTool(runtime)

    await tools.register(
        ctx,
        name="recall_memory",
        description="检索 Hindsight 记忆并返回可核查的原 Message 证据",
        parameters=RecallArguments.model_json_schema(),
        open=open_recall,
        idempotent=True,
        parallel=True,
    )

    watcher: asyncio.Task | None = None

    async def follow():
        while True:
            try:
                async with ctx.runtime_scope():
                    await runtime.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 -- stop incompatible projection, retain independent interest/alert service
                health.degrade("Hindsight 同步失败：" + type(error).__name__)
                logger.warning(
                    "Hindsight synchronization failed (%s)", type(error).__name__
                )
                return
            await asyncio.sleep(config.poll_seconds)

    async def start(_event):
        nonlocal watcher
        async with ctx.runtime_scope():
            await runtime.initialize()
        watcher = await ctx.spawn(follow(), name="hindsight-sync")

    async def stop(_event):
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await runtime.close()

    await ctx.on(RUNTIME_STARTED, start)
    await ctx.on(RUNTIME_STOPPING, stop)

    async def commands(child: Context):
        async def status(_invocation):
            import json

            from core.common.file_io import run_file_io

            runtime._open()
            state = await run_file_io(runtime.journal.status)
            return CommandResult("success", json.dumps(state, ensure_ascii=False))

        async def retry(invocation):
            await runtime.retry(invocation.raw_input.strip())
            return CommandResult(
                "success", "已按原 operation_id 恢复 Hindsight 同步任务"
            )

        registry = child.require(COMMANDS)
        await registry.register(
            child,
            CommandDefinition(
                name="hindsight_status",
                description="查看同步进度与失败任务数",
                handler=status,
                read_only=True,
            ),
        )
        await registry.register(
            child,
            CommandDefinition(
                name="hindsight_retry",
                description="显式恢复原失败同步任务",
                handler=retry,
                recover=retry,
                read_only=False,
                input_hint="job_id",
            ),
        )

    await ctx.inject((COMMANDS,), commands, name="commands")
