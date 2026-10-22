from __future__ import annotations

from agent.plugin_composition import Context, ServiceKey
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from plugins.ui.contract import Configuration
from plugins.conversation.contract import TASK_SESSIONS

from .config import Config
from .runtime import Runtime

SETTINGS = ServiceKey[Configuration]("lol-tracker.settings.v1")
RUNTIME = ServiceKey[Runtime]("lol-tracker.runtime.v1")


class Settings:
    def __init__(self, ctx: Context, runtime: Runtime):
        self.ctx, self.runtime = ctx, runtime

    async def read(self) -> dict[str, object]:
        async with self.ctx.runtime_scope():
            config = Config.model_validate(self.ctx.config)
            return {**self.ctx.require(PLUGIN_CONFIG).read(self.ctx), "enabled": config.enabled,
                    "ready": self.runtime.running, "can_enable": True, "blocked": False,
                    "reason": self.runtime.error,
                    "values": config.model_dump(mode="json", exclude={"enabled"}),
                    "targets": list(await self.ctx.require(TASK_SESSIONS).choices())}

    async def save(self, request_id: str, expected_input: str, values: dict[str, object]) -> dict[str, object]:
        async with self.ctx.runtime_scope():
            if type(values.get("enabled")) is not bool:
                raise ValueError("请选择开启或关闭")
            config = Config.model_validate({**self.ctx.config, **values})
            if config.enabled and config.report_session_id:
                _ = await self.ctx.require(TASK_SESSIONS).resolve(config.report_session_id)
            return await self.ctx.require(PLUGIN_CONFIG).apply(self.ctx, request_id=request_id,
                expected_input=expected_input, config=config.model_dump(mode="json"))

    def receipt(self, request_id: str) -> dict[str, object]:
        return self.ctx.entrypoint(lambda: self.ctx.require(PLUGIN_CONFIG).receipt(self.ctx, request_id))()
