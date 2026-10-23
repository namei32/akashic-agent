"""通过已有本机 Gateway 接收 Codex 事件；不新增监听端口或调用模型。"""
from typing import cast

from pydantic import BaseModel

from agent.plugin_composition import Context
from plugins.content.contract import CONTENT
from plugins.delivery.contract import DELIVERY_GUARDED_START, DELIVERY_SENDERS
from plugins.gateway.contract import RpcMethod
from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, MESSAGE_WRITERS, OWNER_STATE, SESSION_ADMISSION, SESSION_ADMIN

from .events import Empty, Event
from .runtime import Notifications

api_version = 3
name = "codex_notifications"
version = "1.0.0"
desc = "Codex 本轮结束后，将结果摘要写入独立的 Codex 动态会话"
inject = (SESSION_ADMISSION, SESSION_ADMIN, CONTENT, BINDINGS, MESSAGE_CATALOG, MESSAGE_WRITERS,
          OWNER_STATE, DELIVERY_GUARDED_START, DELIVERY_SENDERS)


async def apply(ctx: Context) -> None:
    runtime = Notifications(ctx)

    async def submit(params: BaseModel) -> object:
        return await runtime.submit(cast(Event, params))

    async def status(_params: BaseModel) -> object:
        return await runtime.status()

    await ctx.provide(RpcMethod.key("codex/notifications/submit"), RpcMethod(Event, submit))
    await ctx.provide(RpcMethod.key("codex/notifications/status"), RpcMethod(Empty, status))
