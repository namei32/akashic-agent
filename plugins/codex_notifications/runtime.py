"""事件去重归本插件；消息和客户端提醒沿既有 Ledger / Delivery 协议提交。"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from agent.plugin_composition import Context
from core.common.file_io import run_file_io
from plugins.content.contract import CONTENT
from plugins.delivery.contract import DELIVERY_GUARDED_START, DELIVERY_SENDERS
from plugins.ledger.contract import (
    BINDINGS, MESSAGE_CATALOG, MESSAGE_WRITERS, OWNER_STATE,
    ContentPart, Output, OwnerTransaction, SESSION_ADMISSION, SESSION_ADMIN, SessionAttributes,
)

from .events import Event


class Notifications:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.lock = asyncio.Lock()

    def target(self) -> dict[str, str]:
        address = uuid5(NAMESPACE_URL, "akashic:codex-notifications:" + self.ctx.runtime.plugin_id).hex
        return {"session_id": "akashic:" + address, "channel": "akashic", "recipient": address}

    async def status(self) -> dict[str, object]:
        async with self.ctx.runtime_scope():
            target = self.target()
            catalog = self.ctx.require(MESSAGE_CATALOG)
            def read():
                exists = catalog.exists(target["session_id"])
                reader = catalog.reader(target["session_id"]) if exists else None
                return {"schema_version": 1, **target, "exists": exists,
                        "deleted": reader.deleted if reader else False,
                        "title": reader.title if reader else "Codex 动态"}
            return await run_file_io(read)

    async def submit(self, event: Event) -> dict[str, object]:
        async with self.ctx.runtime_scope(), self.lock:
            ctx, target = self.ctx, self.target()
            message_id = "codex-notice:" + hashlib.sha256(
                (ctx.runtime.plugin_id + ":" + event.identity).encode()).hexdigest()
            state = ctx.require(OWNER_STATE).open(ctx)
            payload = event.model_dump(mode="json")

            def claim(transaction: OwnerTransaction) -> dict[str, str]:
                key = "event:" + event.identity
                previous = transaction.read(key)
                if previous is not None:
                    if previous.value["event"] != payload:
                        raise ValueError("同一 Codex 回合事件不能更换正文")
                    return dict(cast(Mapping[str, str], previous.value["target"]))
                transaction.save(key, {"event": payload, "target": target}, expected_version=None)
                return target

            target = await state.transact_async(claim)
            catalog = ctx.require(MESSAGE_CATALOG)
            if await run_file_io(lambda: catalog.exists(target["session_id"])):
                if await run_file_io(lambda: catalog.reader(target["session_id"]).deleted):
                    raise ValueError("Codex 动态会话已删除，不能自动恢复或改投其他会话")
            else:
                await ctx.require(SESSION_ADMISSION).ensure_async(ctx, target["session_id"], SessionAttributes())
            await ctx.require(SESSION_ADMIN).set_title_if_unset(target["session_id"], "Codex 动态")
            reader = catalog.reader(target["session_id"])
            delivery = ctx.require(DELIVERY_GUARDED_START).open(ctx)
            selection = delivery.selection(message_id)
            if selection is None:
                sink = {"name": "akashic", "address": target["recipient"],
                        "binding_id": ctx.require(DELIVERY_SENDERS).bind("akashic", ctx.require(BINDINGS))}
                writer = ctx.require(MESSAGE_WRITERS).bind(ctx, author="assistant",
                    source="codex_notifications", body_types=(Output,),
                    content={"text": ctx.require(CONTENT).check_text})(reader.session_id)
                try:
                    _, selection = await delivery.publish_async(writer, message_id,
                        Output((ContentPart("text", event.text()),), "complete"), (sink,))
                finally:
                    writer.expire()
            if selection.session_id != target["session_id"] or selection.sinks != ("akashic",):
                raise ValueError("Codex 通知的原投递目标不一致")
            if delivery.destination(message_id, "akashic").address != target["recipient"]:
                raise ValueError("Codex 通知的原发送地址不一致")
            receipt = await delivery.send(message_id, "akashic")
            message = await run_file_io(lambda: reader.get(message_id))
            if message is None:
                raise RuntimeError("Codex 通知缺少已提交消息")
            # 对 Akashic 的逻辑投递以 Session 提交为准；即时提醒失败单独报告。
            return {"schema_version": 1, "event_id": event.identity, "status": "committed",
                    "session_id": target["session_id"], "message_id": message_id,
                    "seq": message.seq, "notification_status": receipt.status,
                    "notification_error": receipt.error}
