"""Exercise the real migration, plugin RPCs and SQLite without a live workspace."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.migrations.runner import MigrationRunner
from agent.plugin_composition import CompositionRoot, Context, PluginRuntime
from plugins.drift import plugin
from plugins.gateway.contract import RpcMethod
from plugins.ledger.contract import ContentPart, ContentReferences, Input
from plugins.ledger.log import MessageLog

BASE = "5766f1a5c48dc988ddbc916e8f4f40103274f7b5"
MIGRATION = "20261028_01_initiative_control"


def proposals(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT * FROM proposals ORDER BY proposal_id, revision").fetchall()


async def run(folder: Path) -> dict[str, object]:
    core, sources, workspace = folder / "core", folder / "sources", folder / "workspace"
    (core / "migrations/core").mkdir(parents=True)
    (core / "migrations/catalog.toml").write_text("schema_version = 2\nmigrations = []\n")
    shutil.copytree(ROOT / "plugins/drift", sources / "drift", ignore=shutil.ignore_patterns("__pycache__"))
    data = workspace / "plugin-data/drift-builtin"
    data.mkdir(parents=True)
    path = data / "drift.sqlite3"

    # Create every legacy lifecycle state with the actual baseline owner.
    baseline = folder / "legacy_store.py"
    baseline.write_bytes(subprocess.check_output(["git", "show", BASE + ":plugins/drift/store.py"], cwd=ROOT))
    spec = importlib.util.spec_from_file_location("drift_legacy", baseline)
    assert spec is not None and spec.loader is not None
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    old = legacy.DriftStore(path)
    old.initialize()
    now = datetime.now(UTC)
    for state in ("pending", "selected", "deferred", "await_change", "ready_for_delivery", "settled", "failed"):
        ref = old.propose(state, "one", {"text": "原始提案 " + state}, now, next_due=now + timedelta(hours=1))["ref"]
        if state != "pending":
            selection = old.select(ref, {"session_id": state, "turn_id": state}, now)
            token = selection["selection_token"]
            if state != "selected":
                old.transition(token, "ready_for_delivery" if state == "settled" else "defer" if state == "deferred" else state)
            if state == "settled":
                old.settle_delivery(token, "legacy-message")
    before = proposals(path)
    log = MessageLog(workspace / "sessions.db")
    log.writer("protected", author="user", source="conversation", body_types=(Input,),
               content={"text": lambda _: ContentReferences()}).append(
                   "original-user-message", Input((ContentPart("text", "Keep the full original conversation."),)))
    log.close()
    with sqlite3.connect(workspace / "sessions.db") as db:
        messages = db.execute("SELECT * FROM messages ORDER BY session_key, seq").fetchall()
    runner = MigrationRunner(repo_root=core, config_path=folder / "config.toml",
                             workspace=workspace, plugin_dirs=[sources], plugins_home=folder / "home")
    assert runner.run().migrations == (MIGRATION,)
    assert proposals(path) == before

    async def mount(data_root: Path = data):
        root = CompositionRoot("drift-controls")
        await root.mount(plugin.apply, name="drift", runtime=PluginRuntime(
            "drift", "scenario", data_root, data_root, data_root, {}))
        operators: list[Context] = []
        async def operator(ctx: Context):
            operators.append(ctx)
        await root.mount(operator, name="operator", inject=tuple(
            RpcMethod.key("drift/initiative/" + action) for action in ("status", "pause", "resume")))
        async def rpc(action: str, **params) -> dict[str, object]:
            async with operators[0].runtime_scope():
                method = operators[0].require(RpcMethod.key("drift/initiative/" + action))
                value = await method.call(method.params.model_validate(params))
                assert isinstance(value, dict), value
                return cast(dict[str, object], value)
        return root, rpc

    root, rpc = await mount()
    try:
        initial = await rpc("status")
        assert initial["paused"] is False and initial["control_version"] == 0
        assert initial["proposals"] == {row[3]: 1 for row in before}
        paused = await rpc("pause", request_id="pause-one", expected_version=0)
        assert paused["paused"] and paused["control_version"] == 1
        assert await rpc("pause", request_id="pause-one", expected_version=0) == paused
        resumed = await rpc("resume", request_id="resume-one", expected_version=1)
        assert not resumed["paused"] and resumed["control_version"] == 2
        replay = await rpc("pause", request_id="pause-one", expected_version=0)
        assert not replay["paused"] and replay["control_version"] == 2
        assert replay["control_receipt"] == paused["control_receipt"]
        for action, params in (
            ("resume", {"request_id": "pause-one", "expected_version": 0}),
            ("pause", {"request_id": "stale", "expected_version": 1}),
            ("pause", {"request_id": "invalid", "expected_version": True}),
        ):
            try:
                await rpc(action, **params)
            except ValueError:
                pass
            else:
                raise AssertionError("Conflicting or invalid control request was accepted")
        raced = await asyncio.gather(
            rpc("pause", request_id="race-a", expected_version=2),
            rpc("resume", request_id="race-b", expected_version=2),
            return_exceptions=True)
        assert sum(isinstance(value, ValueError) for value in raced) == 1
        control = await rpc("status")
        assert control["control_version"] == 3
    finally:
        await root.dispose()
    assert runner.run().migrations == ()
    root, rpc = await mount()
    try:
        assert await rpc("status") == control
    finally:
        await root.dispose()
    assert proposals(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM initiative_control_receipts").fetchone() == (3,)

    fresh = folder / "fresh-workspace"
    fresh_runner = MigrationRunner(repo_root=core, config_path=folder / "fresh.toml",
                                   workspace=fresh, plugin_dirs=[sources], plugins_home=folder / "fresh-home")
    assert fresh_runner.run().migrations == (MIGRATION,)
    fresh_data = fresh / "plugin-data/drift-builtin"
    assert not (fresh_data / "drift.sqlite3").exists()
    root, rpc = await mount(fresh_data)
    try:
        assert await rpc("status") == {"paused": False, "control_version": 0, "proposals": {}}
    finally:
        await root.dispose()

    broken = folder / "broken-workspace"
    broken_path = broken / "plugin-data/drift-builtin/drift.sqlite3"
    broken_store = legacy.DriftStore(broken_path)
    broken_store.initialize()
    broken_store.propose("protected", "one", {"text": "Do not rewrite an unknown database"}, now)
    with sqlite3.connect(broken_path) as db:
        db.execute("PRAGMA user_version = 99")
    broken_before = proposals(broken_path)
    broken_runner = MigrationRunner(repo_root=core, config_path=folder / "broken.toml",
                                    workspace=broken, plugin_dirs=[sources], plugins_home=folder / "broken-home")
    try:
        broken_runner.run()
    except RuntimeError as error:
        assert "99" in str(error), error
    else:
        raise AssertionError("Unknown schema version was migrated")
    assert MIGRATION in broken_runner.check()
    assert proposals(broken_path) == broken_before
    with sqlite3.connect(broken_path) as db:
        db.execute("PRAGMA user_version = 2")
        db.execute("ALTER TABLE proposals ADD COLUMN unexpected TEXT")
    broken_before = proposals(broken_path)
    try:
        broken_runner.run()
    except RuntimeError as error:
        assert "schema mismatch" in str(error), error
    else:
        raise AssertionError("Unrecognized proposal schema was migrated")
    assert MIGRATION in broken_runner.check()
    assert proposals(broken_path) == broken_before
    with sqlite3.connect(workspace / "sessions.db") as db:
        assert db.execute("SELECT * FROM messages ORDER BY session_key, seq").fetchall() == messages
    return {"legacy_proposals_preserved": len(before), "control_version": 3,
            "replay_race_restart": "passed", "fresh_and_failed_migration": "passed",
            "messages_preserved": len(messages)}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="drift-controls-") as temporary:
        print(json.dumps(asyncio.run(run(Path(temporary))), ensure_ascii=False, indent=2))
