"""真实 Ledger 与材料 owner 组成请求材料，验证召回可见范围和摘要失败边界。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.plugin_composition import CompositionRoot, Context, PluginRuntime
from plugins.content.plugin import check_text
from plugins.context.api import decode_material
from plugins.context.materials import ContextMaterials
from plugins.ledger.contract import ContentPart, Input, Message, Output, SessionAttributes
from plugins.ledger.log import MessageLog


async def scenario(workspace: Path) -> dict[str, object]:
    log = MessageLog(workspace / "sessions.db")
    session = "scenario:visible-recall"
    log.ensure_session(session, SessionAttributes())
    for index, body in enumerate((
        Input((ContentPart("text", "old outside selected window"),)),
        Output((ContentPart("text", "old answer"),), "complete"),
        Input((ContentPart("text", "covered user"),)),
        Output((ContentPart("text", "covered answer"),), "complete"),
        Input((ContentPart("text", "current question"),)),
    )):
        log.writer(session, author="user" if isinstance(body, Input) else "assistant",
                   source="conversation", body_types=(type(body),), content={"text": check_text}).append(
                       f"message-{index}", body)
    snapshot = log.reader(session).snapshot()
    with sqlite3.connect(workspace / "sessions.db") as db:
        before = db.execute("SELECT * FROM messages ORDER BY seq").fetchall()
    root = CompositionRoot("visible-recall")
    services: list[ContextMaterials] = []
    observed: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    state = {"summary": True, "fail": False}

    async def setup(ctx: Context) -> None:
        services.append(ContextMaterials(ctx, prompt_sources={},
                        summary_source=("summary", "summary"), recall_source=("recall", "recall")))

    async def summary_owner(ctx: Context) -> None:
        async def prepare(messages: tuple[Message, ...], source: str):
            if state["fail"]:
                raise OSError("summary unavailable")
            if not state["summary"]:
                return {}
            return {"summary": {"reference": "scenario-summary", "content": "summarized history",
                                "source_message_ids": tuple(message.message_id for message in messages[2:4])}}
        await services[0].register(ctx, name="summary", kind="context", prepare=prepare)

    async def recall_owner(ctx: Context) -> None:
        async def prepare(_messages: tuple[Message, ...], _source: str):
            raise AssertionError("visible callback was bypassed")
        async def visible(messages: tuple[Message, ...], source: str, shown: tuple[Message, ...]):
            observed.append((tuple(message.message_id for message in messages),
                             tuple(message.message_id for message in shown)))
            return {"reminders": ({"name": "recall", "text": "index with source references", "priority": 300},)}
        await services[0].register(ctx, name="recall", kind="recall", prepare=prepare,
                                   prepare_visible=visible)

    try:
        for name, apply in (("materials", setup), ("summary", summary_owner), ("recall", recall_owner)):
            await root.mount(apply, name=name,
                             runtime=PluginRuntime(name, "scenario", workspace, workspace, workspace, {}))
        async with services[0].bind() as view:
            material = decode_material(await view.prepare(snapshot, "conversation"))
            assert observed[-1] == (("message-0", "message-1", "message-2", "message-3", "message-4"),
                                    ("message-4",))
            assert material.summary is not None
            assert [item.text for item in material.reminders] == ["index with source references"]
            state["summary"] = False
            await view.prepare(snapshot, "conversation")
            assert observed[-1][0] == observed[-1][1]
            state["fail"] = True
            try:
                await view.prepare(snapshot, "conversation")
            except OSError as error:
                assert str(error) == "summary unavailable"
            else:
                raise AssertionError("summary failure was hidden")
            assert len(observed) == 2
        with sqlite3.connect(workspace / "sessions.db") as db:
            assert db.execute("SELECT * FROM messages ORDER BY seq").fetchall() == before
        return {"full_log_retained": True, "visible_tail_matches_summary": True,
                "no_summary_keeps_full_visibility": True, "failed_summary_does_not_query": True,
                "source_rows_unchanged": True}
    finally:
        await root.dispose()
        log.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="akashic-visible-recall-") as directory:
        print(json.dumps(asyncio.run(scenario(Path(directory)))))
