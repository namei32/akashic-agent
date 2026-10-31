"""Exercise initiative receipts against real v1 memory, tools, and a restarted plugin."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import tempfile

from default_memory_scenario import run, snapshot


async def extended(host, append, workspace: Path, requests: dict) -> dict[str, object]:
    from agent.plugins.manager import PluginManager
    from plugins.default_memory.contract import DEFAULT_MEMORY, INITIATIVE_MEMORY
    from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, SESSION_ADMIN, CallRef
    from plugins.tools.contract import ALL_TOOLS, TOOLS, CallSource

    root = host.live_root
    assert root is not None
    memory = root.context.require(DEFAULT_MEMORY)
    initiative = root.context.require(INITIATIVE_MEMORY)
    session = "scenario:initiative"
    await append(session, "initiative-user", "I prefer coffee without sugar.")
    assert not (await memory.consume())["failures"]
    options = {"timestamp": datetime.now(UTC).isoformat(), "limit": 4, "max_chars": 1500}
    await initiative.admit("first", session, "coffee", **options)
    first = await initiative.prepare("first", session, "coffee", **options)
    assert "coffee without sugar" in str(first["text"]) and "secret coffee" not in str(first["text"])
    assert len(str(first["text"])) <= 1500 and "initiative-user" in first["references"]
    with sqlite3.connect(workspace / "memory/default-v2.db") as db:
        identity = db.execute("SELECT id FROM memory_items WHERE fact LIKE ?", ("%coffee without sugar%",)).fetchone()[0]
    assert identity in [entry["id"] for entry in first["presented"]]
    try:
        await initiative.admit("first", session, "different query", **options)
    except ValueError:
        pass
    else:
        raise AssertionError("A request ID accepted different inputs")
    try:
        await initiative.prepare("missing", session, "coffee", **options)
    except ValueError:
        pass
    else:
        raise AssertionError("Unadmitted recall started")
    try:
        await initiative.admit("hidden", "scenario:hidden", "coffee", **options)
    except PermissionError:
        pass
    else:
        raise AssertionError("Recall-disabled scope was accepted")
    await initiative.admit("private", "scenario:private", "coffee", **options)
    private = await initiative.prepare("private", "scenario:private", "coffee", **options)
    assert "secret coffee" in str(private["text"]) and "without sugar" not in str(private["text"])
    admin = root.context.require(SESSION_ADMIN)
    await admin.set_deleted("scenario:private", deleted=True)
    async with initiative.guard("private", "scenario:private") as reason:
        assert reason == "source_unavailable"
    await admin.set_deleted("scenario:private", deleted=False)

    await append(session, "forget-initiative", "Forget my coffee preference.", finish=False)
    before = snapshot(workspace)
    tools, bindings = root.context.require(TOOLS), root.context.require(BINDINGS)
    ref = next(ref for ref in root.context.require(ALL_TOOLS)().refs if ref.name == "forget_memory")
    binding = await tools.bind(ref, bindings)
    messages = root.context.require(MESSAGE_CATALOG).reader(session).snapshot()
    source = CallSource(CallRef("initiative-forget-call", 0), messages)
    async with tools.open(bindings.describe(binding, TOOLS)) as bound:
        prepared = await bound.prepare({"memory_ids": [identity], "reason": "User asked to forget"}, source)
        assert isinstance(prepared, dict)
        async with initiative.guard("first", session) as reason:
            assert reason is None
            pending = asyncio.create_task(bound.invoke(source.effect_key, prepared))
            await asyncio.sleep(0.2)
            assert not pending.done(), "Forgetting crossed an active snapshot guard"
        assert (await asyncio.wait_for(pending, 10)).outcome == "success"
    async with initiative.guard("first", session) as reason:
        assert reason == "memory_changed"
    assert await initiative.prepare("first", session, "coffee", **options) == first
    assert snapshot(workspace) == before

    async with initiative.guard("private", "scenario:private") as reason:
        assert reason is None
        stopping = asyncio.create_task(host.terminate_all())
        await asyncio.sleep(0.2)
        assert not stopping.done(), "The provider unloaded while a snapshot guard was active"
    await asyncio.wait_for(stopping, 10)
    reopened = PluginManager([workspace.parent / "sources"], workspace=workspace,
                             installed_cache_root=workspace.parent / "home/cache")
    try:
        await reopened.load_all()
        assert reopened.live_root is not None
        restored = reopened.live_root.context.require(INITIATIVE_MEMORY)
        assert await restored.prepare("first", session, "coffee", **options) == first
        async with restored.guard("first", session) as reason:
            assert reason == "memory_changed"
        async with restored.guard("private", "scenario:private") as reason:
            assert reason is None
        assert snapshot(workspace) == before
    finally:
        await reopened.terminate_all()
    return {"initiative_scope_and_budget": True, "admission_idempotency": True,
            "forget_serialized": True, "stale_replay_rejected": True,
            "provider_drain_and_restart": True}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="memory-initiative-") as directory:
        print(json.dumps(asyncio.run(run(Path(directory), extension=extended)), ensure_ascii=False))
