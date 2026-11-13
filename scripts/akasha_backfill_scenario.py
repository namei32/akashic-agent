"""真实历史先于模型配置：普通重建保留跳过语义，显式补缺完整重放。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from default_memory_scenario import run, snapshot


def configure(sources: Path, workspace: Path) -> None:
    from agent.plugin_composition.config_input import save_config
    save_config(workspace / "plugin-data/akasha-builtin", {"memory_enabled": True})
    save_config(workspace / "plugin-data/default_memory-builtin", {"enabled": False})
    save_config(workspace / "plugin-data/context-builtin", {"recall_source": ["akasha", "akasha"]})
    probe = sources / "memory_probe/plugin.py"
    text = probe.read_text()
    registration = '    await ctx.provide(ServiceKey("scenario.append"), ctx.entrypoint(append))'
    assert registration in text
    probe.write_text(text.replace(registration,
        '    await append("scenario:old", "old-a", "Old coffee preference.")\n'
        '    await append("scenario:old", "old-b", "Another old coffee preference.")\n' + registration))


async def verify(host, append, workspace: Path, requests) -> dict[str, bool]:
    from plugins.akasha.infrastructure.persistence import load_consumption, logical_state_sha256
    from plugins.commands.contract import COMMANDS
    from plugins.context.contract import MATERIALS_V4
    root = host.live_root
    assert root is not None
    async with root.context.require(MATERIALS_V4).bind() as view:
        await view.prepare((), "conversation")
    await append("scenario:new", "new", "Current coffee preference.")
    graph = workspace / "memory/akasha.db"
    async with asyncio.timeout(10):
        while True:
            state = load_consumption(graph)
            if state is not None and len(state.applied) == 1:
                break
            await asyncio.sleep(.025)
    with sqlite3.connect(workspace / "sessions-derived.db") as db:
        old_vectors = db.execute("SELECT * FROM message_embeddings ORDER BY message_id,model").fetchall()
    assert len(old_vectors) == 2
    rows = snapshot(workspace)
    async def reindex(option):
        result = await root.context.require(COMMANDS).freeze().execute(
            "/akasha_reindex " + option, session_key="scenario:new", channel="fixture", chat_id="fixture", sender="operator")
        assert result is not None and result.result.kind == "success", result
        return json.loads(result.result.text.removeprefix("Akasha 重建完成："))
    ordinary = await reindex("confirm")
    state = load_consumption(graph)
    assert state is not None and len(state.applied) == 1 and len(state.skipped) == 2
    assert {item.reason for item in state.skipped} == {"missing-embedding"}
    assert ordinary["embedded_messages"] == 0
    filled = await reindex("confirm fill_missing")
    state = load_consumption(graph)
    assert state is not None and len(state.applied) == 3 and not state.skipped
    assert {item.ending[1] for item in state.applied} == {"old-a:reply", "old-b:reply", "new:reply"}
    assert filled["embedded_messages"] == 4
    with sqlite3.connect(workspace / "sessions-derived.db") as db:
        vectors = db.execute("SELECT * FROM message_embeddings ORDER BY message_id,model").fetchall()
    assert len(vectors) == 6 and all(row in vectors for row in old_vectors)
    assert snapshot(workspace) == rows
    before, calls = logical_state_sha256(graph), requests["embedding"]
    repeated = await reindex("confirm fill_missing")
    assert repeated["embedded_messages"] == 0 and requests["embedding"] == calls
    assert logical_state_sha256(graph) == before and snapshot(workspace) == rows
    await host.terminate_all()
    from agent.plugin_composition.config_input import save_config
    from agent.plugins.manager import PluginManager
    save_config(workspace / "plugin-data/akasha-builtin", {
        "memory_enabled": True, "replay_fill_missing_embeddings": True})
    request = workspace / "memory/.akasha-replay-request.json"
    request.write_text(json.dumps({"schema_version": 1, "reason": "scenario-explicit-full-replay"}))
    restarted = PluginManager([workspace.parent / "sources"], workspace=workspace,
                              installed_cache_root=workspace.parent / "home/cache")
    try:
        await restarted.load_all()
        current = restarted.live_root
        assert current is not None
        async with current.context.require(MATERIALS_V4).bind() as view:
            await view.prepare((), "conversation")
        await asyncio.sleep(.1)
        receipt = json.loads((workspace / "memory/.akasha-replay-status.json").read_text())
        assert receipt["phase"] == "completed" and not request.exists()
        assert json.loads(receipt["detail"])["turns"] == 3
        assert requests["embedding"] == calls and snapshot(workspace) == rows
    finally:
        await restarted.terminate_all()
    return {"historical_messages_precede_model": True, "ordinary_rebuild_skips_missing": True,
            "explicit_backfill_learns_all": True, "fixed_vectors_preserved": True,
            "repeat_has_no_model_calls": True, "source_rows_unchanged": True,
            "startup_replay_receipt_persists": True}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="akashic-backfill-") as directory:
        print(json.dumps(asyncio.run(run(Path(directory).resolve(), verify, configure=configure, setup_only=True))))
