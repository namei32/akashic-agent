"""模型用量的独立只读界面；调用事实始终由 Models 拥有。"""
from importlib import import_module

from agent.plugin_composition import Context
from plugins.models.contract import MODEL_USAGE_REPORTS
from plugins.ui.contract import UI

api_version = 3
name = "model_usage"
version = "1.0.0"
desc = "查看模型 token 用量、响应速度和每次调用的统计覆盖"
inject = (MODEL_USAGE_REPORTS,)


async def apply(ctx: Context) -> None:
    async def ui(child: Context) -> None:
        await child.require(UI).register(
            child, web="web_module.js", dashboard=lambda: import_module(".dashboard", __package__),
            requires=("shell.settings.v1",),
            contract_digests={"shell.settings.v1": "1f4dd2eaee9118c799590a36745975150c46979a3f62b431f73aa29e949ce189"},
        )

    await ctx.inject((UI, MODEL_USAGE_REPORTS), ui, name="ui")
