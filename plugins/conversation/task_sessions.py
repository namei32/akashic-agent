"""任务目标是普通可见会话；不改写消息，不借 Wake 保存路由。"""
from __future__ import annotations

from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

from agent.plugin_composition import Context
from core.common.file_io import run_file_io
from plugins.ledger.contract import MESSAGE_CATALOG, SESSION_ADMISSION, SESSION_ADMIN, SessionAttributes, ContentPart, Control
from plugins.delivery_policy.contract import INPUT_ORIGIN


class UnavailableSession(ValueError):
    """选择失效；与损坏的会话数据、数据库读取错误分开。"""


class SessionTargets:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def allocate(self, key: str) -> Mapping[str, str]:
        # Akashic 的 Web/手机对话共用 akashic 渠道，外部 IM 的房间不能凭空创建。
        address = uuid5(NAMESPACE_URL, 'akashic:task-session:' + key).hex
        return {'session_id': 'akashic:' + address, 'channel': 'akashic', 'recipient': address}

    async def resolve(self, session_id: str) -> Mapping[str, str]:
        async with self.ctx.runtime_scope():
            return await self._resolve(session_id)

    async def _resolve(self, session_id: str) -> Mapping[str, str]:
        catalog, origin = self.ctx.require(MESSAGE_CATALOG), self.ctx.require(INPUT_ORIGIN)
        def read():
            if not catalog.exists(session_id):
                raise UnavailableSession('目标会话不存在，请先建立会话或选择新建')
            reader = catalog.reader(session_id)
            if reader.deleted or reader.attributes.visibility != 'listed':
                raise UnavailableSession('目标会话已删除或不是可见对话')
            if session_id.startswith('akashic:') and session_id[8:]:
                return {'session_id': session_id, 'channel': 'akashic', 'recipient': session_id[8:]}
            route = origin(reader, 'conversation', through_seq=reader.head())
            if route is None:
                raise UnavailableSession('目标会话没有可用的投递来源')
            return {'session_id': session_id, 'channel': route[0], 'recipient': route[1]}
        return await run_file_io(read)

    async def choices(self) -> tuple[Mapping[str, str], ...]:
        async with self.ctx.runtime_scope():
            return await self._choices()

    async def _choices(self) -> tuple[Mapping[str, str], ...]:
        catalog = self.ctx.require(MESSAGE_CATALOG)
        entries = await run_file_io(lambda: catalog.sessions(visibility='listed', limit=200).items)
        targets = []
        for entry in entries:
            try:
                target = await self.resolve(entry.session_id)
            except UnavailableSession:
                continue
            first = entry.first_message
            parts = () if first is None or isinstance(first.body, Control) else first.body.parts
            preview = next((part.value.strip().replace('\n', ' ')[:48] for part in parts
                            if isinstance(part, ContentPart) and part.kind == 'text' and isinstance(part.value, str)), '')
            targets.append({**target, 'label': entry.title or preview or '未命名会话'})
        return tuple(targets)

    async def ensure(self, session_id: str, title: str, *, inherit_from: str | None = None) -> None:
        async with self.ctx.runtime_scope():
            await self._ensure(session_id, title, inherit_from=inherit_from)

    async def _ensure(self, session_id: str, title: str, *, inherit_from: str | None = None) -> None:
        if not session_id.startswith('akashic:') or not session_id[8:]:
            raise ValueError('新建任务会话必须属于 Akashic')
        ctx, catalog = self.ctx, self.ctx.require(MESSAGE_CATALOG)
        existing = await run_file_io(lambda: catalog.exists(session_id))
        if existing:
            _ = await self.resolve(session_id)  # 删除的会话不能被后台任务悄悄复活。
        else:
            attributes = SessionAttributes()
            if inherit_from is not None:
                parent = await run_file_io(lambda: catalog.attributes(inherit_from))
                attributes = SessionAttributes(learning=parent.learning, scope=parent.scope)
            _ = await ctx.require(SESSION_ADMISSION).ensure_async(ctx, session_id, attributes)
        _ = await ctx.require(SESSION_ADMIN).set_title_if_unset(session_id, title)
