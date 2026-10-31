"""Real v4 migration, private-target authorization, concurrency and restart."""
from __future__ import annotations

import asyncio
import importlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.migrations.runner import MigrationRunner
from agent.plugin_composition import CompositionRoot, Context, PluginRuntime
from plugins.akashic_clients.private_target import PrivateTarget
from plugins.channels.contract import private_target_key
from plugins.drift import plugin
from plugins.drift.contract import DRIFT_CHANGED, DRIFT_WAKE_V3
from plugins.gateway.contract import RpcMethod
from plugins.ledger.contract import ContentPart, ContentReferences, Input, MESSAGE_CATALOG
from plugins.ledger.log import MessageLog
from plugins.wake.contract import WAKE_CONTACT, WakeContact

BASE = "e0d1a548ec8bcc7ce725f920a8d3844460aa0130"
MIGRATION = "20261031_01_memory_consent"


def rows(path: Path, name: str) -> list[tuple]:
    with sqlite3.connect(path) as db:
        return db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()


async def run(folder: Path) -> None:
    workspace, core = folder / "workspace", folder / "core"
    old_sources, sources = folder / "old-sources", folder / "sources"
    (core / "migrations/core").mkdir(parents=True)
    (core / "migrations/catalog.toml").write_text("schema_version = 2\nmigrations = []\n")
    archive = subprocess.check_output(["git", "archive", BASE, "plugins/drift"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(folder / "old-tree", filter="data")
    old_sources.mkdir()
    shutil.copytree(folder / "old-tree/plugins/drift", old_sources / "drift")
    old_runner = MigrationRunner(repo_root=core, config_path=folder / "config.toml",
                                 workspace=workspace, plugin_dirs=[old_sources], plugins_home=folder / "home")
    assert old_runner.run().migrations == ("20261028_01_initiative_control", "20261029_01_initiative_target")

    package = folder / "baseline_drift"
    shutil.copytree(old_sources / "drift", package)
    sys.path.insert(0, str(folder))
    legacy = importlib.import_module("baseline_drift.store")
    data = workspace / "plugin-data/drift-builtin"
    path = data / "drift.sqlite3"
    old = legacy.DriftStore(path)
    old.initialize()
    now = datetime.now(UTC)
    target = {"channel": "akashic", "recipient": "owner", "session_id": "akashic:owner"}
    old.propose("old-proposal", "one", {"text": "preserve"}, now)
    old.propose("old-initiative", "one", {"kind": "conversation"}, now, initiative_target=target)
    old.set_initiative_paused(True, "old-pause", 0)
    names = ("proposals", "initiative_control", "initiative_control_receipts", "initiative_proposals")
    before = {name: rows(path, name) for name in names}
    shutil.copytree(ROOT / "plugins/drift", sources / "drift", ignore=shutil.ignore_patterns("__pycache__"))
    runner = MigrationRunner(repo_root=core, config_path=folder / "config.toml",
                             workspace=workspace, plugin_dirs=[sources], plugins_home=folder / "home")
    assert runner.run().migrations == (MIGRATION,)
    assert {name: rows(path, name) for name in names} == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (5,)

    log = MessageLog(workspace / "sessions.db")
    writer = log.writer("akashic:owner", author="user", source="conversation", body_types=(Input,),
                        content={"channel.origin": lambda _: ContentReferences()})
    original = writer.append("original", Input((ContentPart("channel.origin",
        {"channel": "akashic", "chat_id": "owner", "sender": "web"}),)))
    writer.expire()
    saved_messages = log.reader("akashic:owner").snapshot()
    assert original in saved_messages
    current_target = [target]

    async def mount() -> tuple[CompositionRoot, Context, object, list[None]]:
        root = CompositionRoot("memory-consent")
        async def catalog(ctx: Context) -> None:
            await ctx.provide(MESSAGE_CATALOG, log.catalog())
        await root.mount(catalog, name="catalog")
        async def proof(ctx: Context) -> None:
            await ctx.provide(private_target_key("akashic"), PrivateTarget(ctx))
        proof_owner = await root.mount(proof, name="akashic_clients", inject=(MESSAGE_CATALOG,),
            runtime=PluginRuntime("akashic_clients", "scenario", folder, folder, folder, {}))
        async def contact(ctx: Context) -> None:
            def selected() -> WakeContact:
                value = current_target[0]
                return WakeContact(value["channel"], value["recipient"], value["session_id"], "UTC")
            await ctx.provide(WAKE_CONTACT, ctx.entrypoint(selected))
        await root.mount(contact, name="wake-contact")
        await root.mount(plugin.apply, name="drift", runtime=PluginRuntime("drift", "scenario", data, data, data, {}))
        operators: list[Context] = []
        notifications: list[None] = []
        async def operator(ctx: Context) -> None:
            operators.append(ctx)
            await ctx.on(DRIFT_CHANGED, lambda event: notifications.append(event))
        actions = ("status", "resume", "memory-status", "memory-consent")
        await root.mount(operator, name="operator", inject=(DRIFT_WAKE_V3, *(RpcMethod.key(
            "drift/initiative/" + action) for action in actions)))
        return root, operators[0], proof_owner, notifications

    async def rpc(ctx: Context, action: str, **params: object) -> dict[str, object]:
        async with ctx.runtime_scope():
            method = ctx.require(RpcMethod.key("drift/initiative/" + action))
            return cast(dict[str, object], await method.call(method.params.model_validate(params)))

    root, operator, proof_owner, events = await mount()
    try:
        assert await rpc(operator, "memory-status") == {"enabled": False, "target": None, "consent_version": 0}
        bad = {"channel": "akashic", "recipient": "other", "session_id": "akashic:other"}
        try:
            await rpc(operator, "memory-consent", enabled=True, target=bad, request_id="bad", expected_version=0)
        except ValueError:
            pass
        else:
            raise AssertionError("A different target was authorized")
        current_target[0] = {"channel": "telegram", "recipient": "room", "session_id": "telegram:room"}
        try:
            await rpc(operator, "memory-consent", enabled=True, target=current_target[0], request_id="unknown", expected_version=0)
        except ValueError:
            pass
        else:
            raise AssertionError("A channel without private proof was authorized")
        current_target[0] = target
        granted = await rpc(operator, "memory-consent", enabled=True, target=target, request_id="grant", expected_version=0)
        assert granted["enabled"] is True and granted["target"] == target and granted["consent_version"] == 1
        assert await rpc(operator, "memory-consent", enabled=True, target=target, request_id="grant", expected_version=0) == granted
        try:
            await rpc(operator, "memory-consent", enabled=False, request_id="stale", expected_version=0)
        except ValueError:
            pass
        else:
            raise AssertionError("A stale consent version was accepted")
        try:
            await rpc(operator, "memory-consent", enabled=False, request_id="grant", expected_version=0)
        except ValueError:
            pass
        else:
            raise AssertionError("A consent request ID accepted different arguments")
        await rpc(operator, "resume", request_id="resume", expected_version=1)
        ref = {"proposal_id": "old-initiative", "revision": "one", "state_version": 1}
        accepted = {"session_id": "wake:consent", "turn_id": "wake-input:consent"}
        async with operator.runtime_scope():
            wake = operator.require(DRIFT_WAKE_V3)
            assert (await wake.select(ref, accepted, now, target=target))["selected"] is True
        entered, release = asyncio.Event(), asyncio.Event()
        async def hold() -> None:
            async with operator.runtime_scope():
                async with wake.send_start(accepted, target) as reason:
                    assert reason is None
                    entered.set()
                    await release.wait()
        holder = asyncio.create_task(hold())
        await asyncio.wait_for(entered.wait(), 3)
        revoke = asyncio.create_task(rpc(operator, "memory-consent", enabled=False, request_id="revoke", expected_version=1))
        await asyncio.sleep(0.2)
        assert not revoke.done(), "Consent changed while an admitted effect was committing"
        release.set()
        await holder
        assert (await revoke)["enabled"] is False
        await proof_owner.dispose()
        replay = await rpc(operator, "memory-consent", enabled=True, target=target, request_id="grant", expected_version=0)
        assert replay["enabled"] is False and replay["consent_version"] == 2
        assert cast(dict, replay["control_receipt"])["enabled"] is True
        assert events
    finally:
        await root.dispose()

    root, operator, _, _ = await mount()
    try:
        status = await rpc(operator, "memory-status")
        assert status == {"enabled": False, "target": target, "consent_version": 2}
        assert log.reader("akashic:owner").snapshot() == saved_messages
        assert rows(path, "initiative_proposals") == before["initiative_proposals"]
        assert rows(path, "proposals")[0] == before["proposals"][0]
    finally:
        await root.dispose()
        log.close()
    print(json.dumps({"v4_rows_preserved": True, "private_target_required": True,
                      "consent_replay_and_conflicts": True, "guard_serializes_revoke": True,
                      "restart_and_messages_preserved": True}))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="drift-consent-") as temporary:
        asyncio.run(run(Path(temporary)))
