"""已生成日报直接写入固定会话；真实发送状态仍由 Delivery 拥有。"""
from __future__ import annotations

import hashlib
from agent.plugin_composition import Context
from plugins.content.contract import CONTENT
from plugins.delivery.contract import DELIVERY_GUARDED_START, DELIVERY_SENDERS
from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, MESSAGE_WRITERS, ContentPart, Output
from plugins.conversation.contract import TASK_SESSIONS
from core.common.file_io import run_file_io
from .store import Store


class ReportDelivery:
    def __init__(self, ctx: Context, store: Store):
        self.ctx, self.store = ctx, store

    def default_target(self):
        return self.ctx.require(TASK_SESSIONS).allocate('lol-reports:' + self.ctx.runtime.plugin_id)

    async def target(self, session_id: str):
        sessions = self.ctx.require(TASK_SESSIONS)
        if session_id:
            return dict(await sessions.resolve(session_id))
        target = self.default_target()
        await sessions.ensure(target['session_id'], '英雄联盟战绩')
        return dict(target)

    async def send(self, report: dict, *, retry: bool = False) -> dict:
        ctx = self.ctx
        target = await run_file_io(lambda: self.store.destination(report['id']))
        if target is None or not isinstance(report['body'], str):
            raise ValueError('日报缺少固定目标或正文')
        identity = hashlib.sha256((ctx.runtime.plugin_id + '\n' + report['id']).encode()).hexdigest()
        message_id = 'lol-report:' + identity
        result = {'kind': 'session-delivery-v1', 'session_id': target['session_id'], 'message_id': message_id}
        catalog = ctx.require(MESSAGE_CATALOG)
        reader = catalog.reader(target['session_id'])
        available = await run_file_io(lambda: catalog.exists(reader.session_id) and not reader.deleted)
        if not available:
            return {**result, 'status': 'failed', 'error': '日报目标会话已删除或不存在'}
        delivery = ctx.require(DELIVERY_GUARDED_START).open(ctx)
        selection = delivery.selection(message_id)
        if selection is None:
            sink = {'name': target['channel'], 'address': target['recipient'],
                    'binding_id': ctx.require(DELIVERY_SENDERS).bind(target['channel'], ctx.require(BINDINGS))}
            writer = ctx.require(MESSAGE_WRITERS).bind(ctx, author='assistant', source='lol_tracker',
                body_types=(Output,), content={'text': ctx.require(CONTENT).check_text})(reader.session_id)
            try:
                _, selection = await delivery.publish_async(writer, message_id,
                    Output((ContentPart('text', report['body']),), 'complete'), (sink,))
            finally:
                writer.expire()
        if selection.session_id != reader.session_id or selection.sinks != (target['channel'],):
            raise ValueError('日报原投递选择与固定目标不一致')
        sink = selection.sinks[0]
        if delivery.destination(message_id, sink).address != target['recipient']:
            raise ValueError('日报原发送地址与固定目标不一致')
        receipt = await (delivery.retry(message_id, sink) if retry else delivery.send(message_id, sink))
        return {**result, 'status': receipt.status, 'error': receipt.error,
                'provider_ids': list(receipt.provider_ids)}
