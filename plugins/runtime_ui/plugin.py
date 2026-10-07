from importlib import import_module

from agent.plugin_composition.ui import UI
from agent.plugin_composition.runtime_catalog import RUNTIME_CATALOG, RUNTIME_MCP_DETAIL
from .directory import DIRECTORY, RuntimeDirectory

api_version = 3
name = "runtime-ui"
version = "1.1.0"

inject = (UI, RUNTIME_CATALOG, RUNTIME_MCP_DETAIL)


async def apply(ctx):
    await ctx.provide(DIRECTORY, RuntimeDirectory(ctx))
    await ctx.require(UI).register(
        ctx, web="web_module.js",
        dashboard=lambda: import_module(".dashboard", __package__),
        requires=("shell.pages.v1",),
        provides=(),
    )
