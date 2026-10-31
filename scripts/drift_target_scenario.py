"""Exercise target isolation and pause across real Drift plugin generations."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.migrations.runner import MigrationRunner
from agent.plugin_composition import CompositionRoot, Context, PluginRuntime
from plugins.drift import plugin
from plugins.drift.contract import DRIFT_CHANGED, DRIFT_WAKE_V3
from plugins.drift.store import DriftStore
from plugins.gateway.contract import RpcMethod

BASE = "01cd2cf99"


def rows(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


async def run(folder: Path) -> None:
    # Build v3 using the actual previous owner, including an operation receipt.
    package = folder / "baseline_drift"
    for name in ("__init__.py", "store.py", "schema.py", "drift_migrations/__init__.py",
                 "drift_migrations/helpers/__init__.py", "drift_migrations/helpers/initiative_control_v3.py"):
        destination = package / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(subprocess.check_output(
            ["git", "show", f"{BASE}:plugins/drift/{name}"], cwd=ROOT))
    sys.path.insert(0, str(folder))
    legacy = importlib.import_module("baseline_drift.store")
    workspace, sources, core = folder / "workspace", folder / "sources", folder / "core"
    data = workspace / "plugin-data/drift-builtin"
    data.mkdir(parents=True)
    path = data / "drift.sqlite3"
    old = legacy.DriftStore(path)
    old.initialize()
    now = datetime.now(UTC)
    old.propose("legacy", "one", {"text": "retain the original proposal"}, now)
    old.set_initiative_paused(True, "old-pause", 0)
    before = {name: rows(path, name) for name in ("proposals", "initiative_control", "initiative_control_receipts")}
    (core / "migrations/core").mkdir(parents=True)
    (core / "migrations/catalog.toml").write_text("schema_version = 2\nmigrations = []\n")
    shutil.copytree(ROOT / "plugins/drift", sources / "drift", ignore=shutil.ignore_patterns("__pycache__"))
    runner = MigrationRunner(repo_root=core, config_path=folder / "config.toml",
                             workspace=workspace, plugin_dirs=[sources], plugins_home=folder / "home")
    assert runner.run().migrations == (
        "20261028_01_initiative_control", "20261029_01_initiative_target", "20261031_01_memory_consent",
    )
    assert {name: rows(path, name) for name in before} == before

    async def mount() -> tuple[CompositionRoot, Context, list[None]]:
        root = CompositionRoot("drift-target")
        await root.mount(plugin.apply, name="drift", runtime=PluginRuntime(
            "drift", "scenario", data, data, data, {}))
        operators: list[Context] = []
        notifications: list[None] = []
        async def operator(ctx: Context) -> None:
            operators.append(ctx)
            await ctx.on(DRIFT_CHANGED, lambda event: notifications.append(event))
        await root.mount(operator, name="operator", inject=(DRIFT_WAKE_V3, *(RpcMethod.key(
            "drift/initiative/" + action) for action in ("status", "pause", "resume"))))
        return root, operators[0], notifications

    first, a, events_a = await mount()
    second, b, events_b = await mount()
    async def rpc(ctx: Context, action: str, **params: object) -> dict[str, object]:
        async with ctx.runtime_scope():
            method = ctx.require(RpcMethod.key("drift/initiative/" + action))
            result = await method.call(method.params.model_validate(params))
            return cast(dict[str, object], result)
    target = {"channel": "test", "recipient": "owner-a", "session_id": "session-a"}
    other = {"channel": "test", "recipient": "owner-b", "session_id": "session-b"}
    accepted = {"session_id": "wake:internal", "turn_id": "wake-input:one"}
    try:
        assert (await rpc(a, "status"))["paused"] is True
        await rpc(a, "resume", request_id="resume-1", expected_version=1)
        store = DriftStore(path)
        created = store.propose("initiative", "one", {"kind": "conversation"}, now, initiative_target=target)
        ref = cast(dict[str, object], created["ref"])
        async with a.runtime_scope():
            wake = a.require(DRIFT_WAKE_V3)
            visible = await wake.snapshot(now, target=target)
            hidden = await wake.snapshot(now, target=other)
            assert len(cast(tuple, visible["proposals"])) == 2
            assert len(cast(tuple, hidden["proposals"])) == 1  # legacy remains visible
            assert (await wake.select(ref, accepted, now, target=other))["reason"] == "target_mismatch"
        await rpc(a, "pause", request_id="pause-2", expected_version=2)
        async with a.runtime_scope():
            assert (await wake.select(ref, accepted, now, target=target))["reason"] == "paused"
            assert len(cast(tuple, (await wake.snapshot(now, target=target))["proposals"])) == 1
        await rpc(a, "resume", request_id="resume-3", expected_version=3)
        async with a.runtime_scope():
            assert (await wake.select(ref, accepted, now, target=target))["selected"] is True
            async with wake.send_start(accepted, other) as reason:
                assert reason == "target_mismatch"

        entered, release = asyncio.Event(), asyncio.Event()
        async def held_start() -> None:
            async with a.runtime_scope():
                async with wake.send_start(accepted, target) as reason:
                    assert reason is None
                    entered.set()
                    await release.wait()
        holder = asyncio.create_task(held_start())
        await asyncio.wait_for(entered.wait(), 3)
        pause = asyncio.create_task(rpc(b, "pause", request_id="pause-4", expected_version=4))
        await asyncio.sleep(0.1)
        assert not pause.done()  # The durable start precedes the pause commit.
        release.set()
        await holder
        assert (await pause)["paused"] is True
        async with a.runtime_scope():
            async with wake.send_start(accepted, target) as reason:
                assert reason is not None and reason.startswith("paused:drift-selection:")
        await rpc(b, "resume", request_id="resume-5", expected_version=5)
        async with a.runtime_scope():
            async with wake.send_start(accepted, target) as reason:
                assert reason is None
        assert events_a and events_b
        assert rows(path, "proposals")[0] == before["proposals"][0]
    finally:
        await second.dispose()
        await first.dispose()
    print("v3 rows preserved; target isolation, pause admission and cross-generation resume passed")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="drift-target-") as temporary:
        asyncio.run(run(Path(temporary)))
