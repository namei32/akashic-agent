"""真实插件、HTTP 向量驱动与 SQLite 验证索引、窗口去重、范围读取和重启。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from default_memory_scenario import run, snapshot


EXTRA = '''
from plugins.context.contract import MATERIALS_V4
from plugins.ledger.contract import BINDINGS, ToolCall
from plugins.tools.contract import TOOLS
original_apply = apply
inject = (*inject, MATERIALS_V4, OWNER_STATE, BINDINGS)
async def apply(ctx):
    await original_apply(ctx)
    store = ctx.require(OWNER_STATE).open(ctx)
    async def summary(messages, source):
        record = store.read("summary")
        return {} if record is None else {"summary": record.value}
    async def set_summary(identities):
        store.transact(lambda tx: tx.save("summary", {
            "reference": "scenario-summary", "content": "Earlier query summarized",
            "source_message_ids": identities}, expected_version=None))
    bindings = ctx.require(BINDINGS)
    def check_call(call):
        bindings.describe(call.binding_id, TOOLS)
    writer = ctx.require(MESSAGE_WRITERS).bind(ctx, author="assistant", source="conversation",
        body_types=(Output,), content={"text": ctx.require(CONTENT).check_text}, check_call=check_call)
    async def call(session, identity, binding, arguments):
        writer(session).append(identity, Output((ToolCall(binding, arguments),), "continue"))
    await ctx.require(MATERIALS_V4).register(ctx, name="scenario_summary", kind="context", prepare=summary)
    await ctx.provide(ServiceKey("scenario.set-summary"), ctx.entrypoint(set_summary))
    await ctx.provide(ServiceKey("scenario.call"), ctx.entrypoint(call))
'''


def configure(sources: Path, workspace: Path) -> None:
    from agent.plugin_composition.config_input import save_config
    save_config(workspace / "plugin-data/akasha-builtin", {"enabled": True, "memory_enabled": True})
    save_config(workspace / "plugin-data/default_memory-builtin", {"enabled": False})
    save_config(workspace / "plugin-data/context-builtin", {
        "summary_source": ["scenario_summary", "memory_probe"], "recall_source": ["akasha", "akasha"]})
    with (sources / "memory_probe/plugin.py").open("a") as stream:
        stream.write(EXTRA)


async def verify(host, append, workspace: Path, requests) -> dict[str, object]:
    from agent.plugin_composition import ServiceKey
    from agent.plugins.manager import PluginManager
    from plugins.akasha.infrastructure.persistence import load_consumption, logical_state_sha256
    from plugins.context.contract import MATERIALS_V4
    from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, CallRef
    from plugins.tools.contract import ALL_TOOLS, TOOLS, CallSource

    root = host.live_root
    assert root is not None
    catalog = root.context.require(MESSAGE_CATALOG)
    # 模型在运行时配置；先建立空图，再写首轮经历，避免首次启用切点包含已有消息。
    async with root.context.require(MATERIALS_V4).bind() as view:
        await view.prepare((), "conversation")
    with sqlite3.connect(workspace / "sessions.db") as db:
        db.execute("INSERT INTO owner_records VALUES (?,?,1,?)", (
            "plugin:akasha:scope-policy", "project=private", json.dumps({"learn": "isolated", "recall": True})))
    text = "I always prefer black coffee. " + "Long details. " * 30 + "FULL_TEXT_END"
    await append("scenario:global", "coffee", text)
    await append("scenario:private", "private", "My secret preference is decaf.", scope=(("project", "private"),))
    await append("scenario:excluded", "excluded", "Never learn this.", learning="excluded")
    graph = workspace / "memory/akasha.db"
    for _ in range(200):
        state = load_consumption(graph)
        if state is not None and len(state.applied) == 1:
            break
        await asyncio.sleep(.025)
    else:
        raise AssertionError("global learning did not publish")
    before_graph = logical_state_sha256(graph)
    query_session = "scenario:query"
    async def prepare():
        async with root.context.require(MATERIALS_V4).bind() as view:
            return await view.prepare(catalog.reader(query_session).snapshot(), "conversation")
    await append(query_session, "query-first", "coffee preference?", finish=False)
    first = await prepare()
    rendered = json.dumps(first)
    assert "ref=coffee" in rendered and "FULL_TEXT_END" not in rendered
    assert "My secret preference" not in rendered
    assert await prepare() == first
    await append(query_session, "query-second", "coffee preference?", finish=False)
    second = await prepare()
    assert "ref=coffee" not in json.dumps(second)
    await root.context.require(ServiceKey("scenario.set-summary"))(["query-first", "query-second"])
    await append(query_session, "query-third", "coffee preference?", finish=False)
    third = await prepare()
    assert "ref=coffee" in json.dumps(third), "summarized recall stayed incorrectly excluded"

    tools, bindings = root.context.require(TOOLS), root.context.require(BINDINGS)
    call = root.context.require(ServiceKey("scenario.call"))
    async def invoke(name, arguments, identity):
        ref = next(item for item in root.context.require(ALL_TOOLS)().refs if item.name == name)
        binding = await tools.bind(ref, bindings)
        await call(query_session, identity, binding, arguments)
        source = CallSource(CallRef(identity, 0), catalog.reader(query_session).snapshot())
        async with tools.open(bindings.describe(binding, TOOLS)) as tool:
            prepared = await tool.prepare(arguments, source)
            assert isinstance(prepared, dict), prepared
            return await tool.invoke(source.effect_key, prepared)
    fetched = await invoke("fetch_messages", {"message_ids": ["coffee", "private", "missing"]}, "fetch-call")
    fetched_text = "\n".join(str(part.value) for part in fetched.parts if part.kind == "text")
    assert "FULL_TEXT_END" in fetched_text and "My secret preference" not in fetched_text
    assert "不存在" in fetched_text
    recalled = await invoke("recall_memory", {"query": "coffee", "limit": 5}, "recall-call")
    assert "ref=coffee" in str(recalled.parts) and "FULL_TEXT_END" not in str(recalled.parts)
    assert logical_state_sha256(graph) == before_graph
    rows = {row[0]: row for row in snapshot(workspace)}
    assert "excluded" in rows and "private" in rows
    await host.terminate_all()
    restarted = PluginManager([workspace.parent / "sources"], workspace=workspace,
                              installed_cache_root=workspace.parent / "home/cache")
    try:
        await restarted.load_all()
        current = restarted.live_root
        assert current is not None
        async with current.context.require(MATERIALS_V4).bind() as view:
            restored = await view.prepare(current.context.require(MESSAGE_CATALOG).reader(query_session).snapshot(), "conversation")
        assert restored == third
        after = {row[0]: row for row in snapshot(workspace)}
        assert all(after[identity] == row for identity, row in rows.items())
        assert logical_state_sha256(graph) == before_graph
    finally:
        await restarted.terminate_all()
    return {"http_embedding": requests["embedding"] > 0, "compact_index": True,
            "same_input_reused": True, "window_dedup": True, "summary_restores_recall": True,
            "full_text_fetch": True, "project_scope_enforced": True, "query_is_read_only": True,
            "restart_reuses_material": True, "source_rows_unchanged": True}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="akashic-index-scenario-") as directory:
        print(json.dumps(asyncio.run(run(Path(directory).resolve(), verify, configure=configure, setup_only=True))))
