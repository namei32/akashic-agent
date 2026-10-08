from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module

from pydantic import BaseModel, Field

from agent.plugin_composition import RUNTIME_STARTED, RUNTIME_STOPPING, Context
from agent.plugin_composition.models import CHAT_MODELS
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from agent.plugin_composition.ui import UI
from agent.plugin_contracts import ContentPart
from agent.plugin_contracts.proactive import EVENTMAIL_ALERT_SOURCE
from agent.plugin_contracts.tools import TOOLS, Result
from core.common.file_io import run_file_io

from .config import Config
from .runtime import Runtime
from .settings import RUNTIME, SETTINGS, Settings
from .store import Store, encoded

api_version = 3
name = "lol_tracker"
version = "1.0.0"
desc = "查询关注玩家的国服英雄联盟战绩，生成每日分析并交由 Wake 推送"
inject = (PLUGIN_CONFIG, TOOLS, CHAT_MODELS)


class Query(BaseModel):
    days: int = Field(default=7, ge=1, le=90, description="查看最近多少天的已采集战绩")


class HistoryTool:
    idempotent = True

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def prepare(self, arguments, source=None):
        return Query.model_validate(arguments).model_dump()

    async def invoke(self, key, arguments):
        now = datetime.now(UTC)
        result = await self.runtime.facts(now-timedelta(days=arguments["days"]), now, "战绩查询")
        result["collector"] = {k: v for k, v in (await self.runtime.status()).items() if k != "reports"}
        return Result("success", (ContentPart("text", encoded(result)),))

    async def query(self, key):
        return None


async def apply(ctx: Context) -> None:
    config = Config.model_validate(ctx.config)
    store = Store(ctx.data_root / "matches.sqlite3")
    await run_file_io(store.initialize)
    runtime = Runtime(ctx, config, store)
    await ctx.provide(RUNTIME, runtime)
    await ctx.provide(SETTINGS, Settings(ctx, runtime))
    catalog = ctx.require(TOOLS)
    await catalog.declare_group(ctx, description=desc)

    @asynccontextmanager
    async def open_history(_state):
        yield HistoryTool(runtime)

    await catalog.register(ctx, name="lol_match_history", description="查询已关注玩家的英雄联盟战绩、分模式统计和采集覆盖情况。可根据返回事实分析；离线或不完整不能当作没有对局。",
                           parameters=Query.model_json_schema(), open=open_history, idempotent=True)

    async def alerts(child: Context):
        source = child.require(EVENTMAIL_ALERT_SOURCE).bind("lol_tracker")
        def attach():
            runtime.report_alert = child.entrypoint(source.report)
            def stop():
                runtime.report_alert = None
                source.close()
            return stop
        await child.effect(attach, label="report-alerts")

    await ctx.inject((EVENTMAIL_ALERT_SOURCE,), alerts, name="push")

    async def ui(child: Context):
        await child.require(UI).register(child, web="web_module.js",
            dashboard=lambda: import_module(".dashboard", __package__), requires=("shell.pages.v1",))

    await ctx.inject((UI, SETTINGS, RUNTIME), ui, name="ui")
    worker = None

    async def start(_event):
        nonlocal worker
        if config.enabled:
            worker = await ctx.spawn(runtime.follow(), name="collect-and-report")
        else:
            await run_file_io(lambda: runtime.bridge_config(False))

    async def stop(_event):
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await run_file_io(lambda: runtime.bridge_config(False))

    await ctx.on(RUNTIME_STARTED, start)
    await ctx.on(RUNTIME_STOPPING, stop)
