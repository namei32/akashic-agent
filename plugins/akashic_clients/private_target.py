"""Prove only the native single-owner Web channel's exact existing target."""
from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager

from agent.plugin_composition import Context
from core.common.file_io import run_file_io
from plugins.ledger.contract import ContentPart, Input, MESSAGE_CATALOG, MessageCatalog


def _matches(catalog: MessageCatalog, recipient: str, session_id: str) -> bool:
    if (not isinstance(recipient, str) or not recipient or recipient.strip() != recipient
            or len(recipient) > 248 or any(ord(char) < 32 for char in recipient)
            or session_id != "akashic:" + recipient or not catalog.exists(session_id)):
        return False
    reader = catalog.reader(session_id)
    with reader.read_snapshot():
        if reader.deleted or reader.attributes.visibility != "listed":
            return False
        page = reader.read_page(limit=1)
        if not page.messages:
            return False
        first = page.messages[0]
        if first.author != "user" or first.source != "conversation" or not isinstance(first.body, Input):
            return False
        return any(
            isinstance(part, ContentPart) and part.kind == "channel.origin"
            and isinstance(part.value, Mapping)
            and part.value.get("channel") == "akashic"
            and part.value.get("chat_id") == recipient
            and part.value.get("sender") == "web"
            for part in first.body.parts
        )


class PrivateTarget:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    @asynccontextmanager
    async def guard(self, recipient: str, session_id: str) -> AsyncGenerator[bool]:
        # The provider cannot unload while the consumer performs its short commit.
        async with self.ctx.runtime_scope():
            catalog = self.ctx.require(MESSAGE_CATALOG)
            yield await run_file_io(lambda: _matches(catalog, recipient, session_id))
