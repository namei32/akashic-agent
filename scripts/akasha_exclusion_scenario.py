"""真实插件切换排除策略：保留原消息，拒绝旧图，正式重建后重启验证。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from default_memory_scenario import run, snapshot


def configure(_sources: Path, workspace: Path) -> None:
    from agent.plugin_composition.config_input import save_config
    save_config(workspace / "plugin-data/akasha-builtin", {
        "memory_enabled": True, "excluded_turn_endings": ["denied:reply", "later-denied:reply"]})
    save_config(workspace / "plugin-data/default_memory-builtin", {"enabled": False})
    save_config(workspace / "plugin-data/context-builtin", {"recall_source": ["akasha", "akasha"]})


async def verify(host, append, workspace: Path, requests) -> dict[str, bool]:
    from agent.plugin_composition import ServiceKey
    from agent.plugin_composition.config_input import save_config
    from agent.plugins.manager import PluginManager
    from plugins.akasha.infrastructure.persistence import load_consumption, logical_state_sha256
    from plugins.commands.contract import COMMANDS
    from plugins.context.contract import MATERIALS_V4
    from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, MESSAGE_EMBEDDINGS
    from plugins.akasha.learning import AKASHA_LEARNING

    graph = workspace / "memory/akasha.db"
    async def prepare(manager):
        root = manager.live_root
        assert root is not None
        async with root.context.require(MATERIALS_V4).bind() as view:
            return await view.prepare(root.context.require(MESSAGE_CATALOG).reader("scenario:query").snapshot(), "conversation")

    async def wait_for(count):
        for _ in range(200):
            state = load_consumption(graph)
            if state is not None and len(state.applied) == count:
                return state
            await asyncio.sleep(.025)
        raise AssertionError(f"learning did not publish {count} turns")

    async def rebuild(manager):
        root = manager.live_root
        assert root is not None
        result = await root.context.require(COMMANDS).freeze().execute(
            "/akasha_reindex confirm", session_key="scenario:query", channel="fixture", chat_id="fixture", sender="operator")
        assert result is not None and result.result.kind == "success", result

    async def restart():
        manager = PluginManager([workspace.parent / "sources"], workspace=workspace,
                                installed_cache_root=workspace.parent / "home/cache")
        await manager.load_all()
        return manager

    # 首次模型配置之后先建立空图，保留原有首次启用切点语义。
    await prepare(host)
    await append("scenario:history", "keep", "I prefer black coffee.")
    await append("scenario:history", "denied", "Private coffee resume details, excluded by operator.")
    await append("scenario:other", "other", "Another coffee memory.")
    state = await wait_for(2)
    assert {entry.ending[1] for entry in state.applied} == {"keep:reply", "other:reply"}
    root = host.live_root
    assert root is not None
    catalog = root.context.require(MESSAGE_CATALOG)
    metadata = root.context.require(BINDINGS).describe(state.applied[0].learning_binding, AKASHA_LEARNING)
    assert tuple(metadata["excluded_turn_endings"]) == ("denied:reply", "later-denied:reply")
    learning = root.context.require(AKASHA_LEARNING)
    vectors = root.context.require(MESSAGE_EMBEDDINGS).bind(learning.text)
    for identity in ("denied", "denied:reply"):
        message = catalog.reader("scenario:history").get(identity)
        assert message is not None
        assert vectors.read(message, model=metadata["embedding_model"], dimension=metadata["dimension"]) is None
    await append("scenario:query", "query", "coffee?", finish=False)
    first = await prepare(host)
    assert "ref=keep" in str(first) and "Private coffee resume" not in str(first)
    rows = {row[0]: row for row in snapshot(workspace)}
    await rebuild(host)
    assert {entry.ending[1] for entry in (await wait_for(2)).applied} == {"keep:reply", "other:reply"}
    await host.terminate_all()

    # 旧图已含后来撤回的整轮。配置改变后不得靠旧查询缓存继续提供该材料。
    save_config(workspace / "plugin-data/akasha-builtin", {
        "memory_enabled": True, "excluded_turn_endings": ["denied:reply", "later-denied:reply", "keep:reply"]})
    manager = await restart()
    try:
        unavailable = await prepare(manager)
        assert "不可用" in str(unavailable) and "ref=keep" not in str(unavailable)
        await rebuild(manager)
        state = await wait_for(1)
        assert state.applied[0].ending[1] == "other:reply"
        root = manager.live_root
        assert root is not None
        append = root.context.require(ServiceKey("scenario.append"))
        await append("scenario:later", "later-denied", "Excluded future coffee turn.")
        await append("scenario:later", "new", "New eligible coffee turn.")
        state = await wait_for(2)
        assert {entry.ending[1] for entry in state.applied} == {"other:reply", "new:reply"}
        before = logical_state_sha256(graph)
    finally:
        await manager.terminate_all()
    manager = await restart()
    try:
        assert logical_state_sha256(graph) == before
        after = {row[0]: row for row in snapshot(workspace)}
        assert all(after[identity] == row for identity, row in rows.items())
        assert all(identity in after for identity in ("denied", "denied:reply", "keep", "later-denied"))
    finally:
        await manager.terminate_all()
    return {"http_embedding": requests["embedding"] > 0, "excluded_before_embedding": True,
            "old_graph_refused": True, "same_consumer_rebuild": True, "exclusion_survives_restart": True,
            "original_messages_preserved": True}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="akashic-exclusion-") as directory:
        print(json.dumps(asyncio.run(run(Path(directory).resolve(), verify, configure=configure, setup_only=True))))
