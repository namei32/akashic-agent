"""Deliver to separate sessions and verify the real scoped history survives restart."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.plugin_composition import ServiceKey
from agent.plugins.manager import PluginManager
from plugins.content.plugin import check_text
from plugins.delivery.contract import DELIVERY_READ, DELIVERY_SCOPED_READ
from plugins.ledger.contract import ContentPart, Input, MESSAGE_CATALOG
from tests.support.delivery_sources import sources as delivery_sources
from tests.test_default_reply import application


def configure(sources: Path) -> None:
    delivery_sources(sources)
    driver = sources / "scoped_history_driver"
    driver.mkdir()
    (driver / "plugin.py").write_text('''
from agent.plugin_composition import ServiceKey
from plugins.content.contract import CONTENT
from plugins.delivery.contract import DELIVERY_GUARDED_START, DELIVERY_SENDERS
from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, MESSAGE_WRITERS, ContentPart, Output
api_version = 3
name = "scoped_history_driver"
version = "1.0.0"
inject = (CONTENT, DELIVERY_GUARDED_START, DELIVERY_SENDERS, BINDINGS, MESSAGE_CATALOG, MESSAGE_WRITERS)
async def apply(ctx):
    async def deliver(session, identity):
        writer = ctx.require(MESSAGE_WRITERS).bind(ctx, author="wake", source="wake", body_types=(Output,),
            content={"text": ctx.require(CONTENT).check_text})(session)
        try:
            message = await writer.append_async(identity, Output((ContentPart("text", identity),), "complete"))
        finally:
            writer.expire()
        binding = ctx.require(DELIVERY_SENDERS).bind("test", ctx.require(BINDINGS))
        delivery = ctx.require(DELIVERY_GUARDED_START).open(ctx)
        sink = {"name": "test", "address": "local-receiver", "binding_id": binding}
        await delivery.prepare_async(ctx.require(MESSAGE_CATALOG).reader(session), message, (sink,))
        return (await delivery.send(identity, "test")).status
    await ctx.provide(ServiceKey("scenario.deliver"), ctx.entrypoint(deliver))
''')


async def run(folder: Path) -> None:
    async with application(folder, replying=False, extra_sources=configure) as (log, host):
        root = host.live_root
        assert root is not None
        send = root.context.require(ServiceKey("scenario.deliver"))

        # Two real Session streams and a real local-only sender share Delivery's owner state.
        for session in ("test:private-a", "test:private-b"):
            writer = log.writer(session, author="user", source="conversation", body_types=(Input,),
                                content={"text": check_text})
            writer.append(session + ":original", Input((ContentPart("text", "Original message"),)))
            writer.expire()
        before = {session: tuple(log.reader(session).snapshot())
                  for session in ("test:private-a", "test:private-b")}

        async def deliver(session: str, identity: str) -> None:
            assert await send(session, identity) == "delivered"

        await deliver("test:private-a", "a-first")
        for index in range(5):
            await deliver("test:private-b", f"b-{index}")
        start, stop = datetime.now(UTC) - timedelta(minutes=5), datetime.now(UTC) + timedelta(minutes=5)
        scoped = root.context.require(DELIVERY_SCOPED_READ)
        global_history = root.context.require(DELIVERY_READ)
        selected = scoped.recent_for_session("test:private-a", since=start, until=stop, limit=1)
        assert [row.message.message_id for row in selected] == ["a-first"]
        assert all(row.message.session_id == "test:private-b" for row in
                   global_history.recent(since=start, until=stop, limit=3))
        assert not scoped.recent_for_session(
            "test:private-a", since=start, until=stop, limit=1, excluded_sources=frozenset({"wake"}),
        )
        await deliver("test:private-a", "a-second")
        expected = ["a-second", "a-first"]
        assert [row.message.message_id for row in scoped.recent_for_session(
            "test:private-a", since=start, until=stop, limit=2)] == expected
        await host.terminate_all()
        host = PluginManager([folder / "plugins"], workspace=folder / "workspace",
                             installed_cache_root=folder / "home/cache")
        try:
            await host.load_all()
            restored = host.live_root
            assert restored is not None
            scoped = restored.context.require(DELIVERY_SCOPED_READ)
            assert [row.message.message_id for row in scoped.recent_for_session(
                "test:private-a", since=start, until=stop, limit=2)] == expected
            catalog = cast(Any, restored.context.require(MESSAGE_CATALOG))
            for session, originals in before.items():
                assert tuple(catalog.reader(session).snapshot())[:len(originals)] == originals
        finally:
            await host.terminate_all()
    print(json.dumps({"real_deliveries": 7, "filtered_before_limit": True,
                      "legacy_read_unchanged": True, "restart_and_messages_preserved": True}))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="delivery-scoped-history-") as temporary:
        asyncio.run(run(Path(temporary)))
