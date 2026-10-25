"""Real plugin/HTTP/SQLite scenario for durable learning controls and provider loss."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from default_memory_scenario import ROOT, PROBE, snapshot


async def run(folder: Path) -> dict[str, object]:
    from agent.plugins.install import install_git_plugin
    from agent.plugins.manager import PluginManager
    from agent.plugins.selection import PluginSelection
    from agent.plugin_composition import ServiceKey
    from agent.plugin_composition.config_input import save_config
    from plugins.models.contract import MODEL_CATALOG
    from plugins.ledger.contract import MESSAGE_CATALOG
    from plugins.default_memory.contract import DEFAULT_MEMORY
    from plugins.commands.contract import COMMANDS
    from plugins.context.contract import MATERIALS_V4
    from plugins.akasha.settings import SETTINGS
    from plugins.gateway.contract import RpcMethod

    calls = {"chat": 0, "embedding": 0}
    entered, release = asyncio.Event(), asyncio.Event()

    async def serve(reader, writer):
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode()
            first = headers.split("\r\n")[0]
            size = next((int(line.split(":", 1)[1]) for line in headers.split("\r\n")
                         if line.lower().startswith("content-length:")), 0)
            payload = json.loads(await reader.readexactly(size)) if size else {}
            if "/embeddings" in first:
                calls["embedding"] += 1
                values = payload["input"]
                if isinstance(values, str):
                    values = [values]
                result = {"data": [{"index": i, "embedding": [1., 0., 0., 0.]}
                                   for i, _ in enumerate(values)],
                          "usage": {"prompt_tokens": 1, "total_tokens": 1}}
            elif "/chat/completions" in first:
                prompt = payload["messages"][-1]["content"]
                if "原始资料：" in prompt:
                    calls["chat"] += 1
                    if calls["chat"] == 1:
                        entered.set()
                        await release.wait()
                    rows = json.loads(prompt.split("原始资料：", 1)[1])
                else:
                    rows = []
                facts = [{"kind": "preference", "summary": row["text"], "evidence_ids": [row["message_id"]]}
                         for row in rows if row["role"] == "user"]
                result = {"id": "fixture", "object": "chat.completion", "model": "memory-fixture",
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({"facts": facts, "retract": []})},
                                       "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
            else:
                result = {"data": [{"id": "memory-fixture"}, {"id": "embedding-fixture"}]}
            body = json.dumps(result).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " +
                         str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    endpoint = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
    workspace, sources = folder / "workspace", folder / "sources"
    workspace.mkdir()
    PluginSelection(workspace).initialize()
    for name in ("ledger", "commands", "content", "context", "tools", "turn_projection",
                 "models", "openai_compatible"):
        shutil.copytree(ROOT / "plugins" / name, sources / name, ignore=shutil.ignore_patterns("__pycache__"))
    (sources / "memory_probe").mkdir()
    (sources / "memory_probe/plugin.py").write_text(PROBE)
    os.environ.update(HOME=str(folder / "home"), AKASHIC_PLUGIN_HOME=str(folder / "home"), AKASHIC_PLUGIN_DISTRIBUTION="")
    for name in ("akasha", "default_memory"):
        provider = folder / name
        shutil.copytree(ROOT / "plugins" / name, provider, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["git", "init", "-q", "--initial-branch=source", str(provider)], check=True)
        subprocess.run(["git", "-C", str(provider), "add", "."], check=True)
        subprocess.run(["git", "-C", str(provider), "-c", "user.name=Scenario", "-c",
                        "user.email=scenario@example.invalid", "commit", "-qm", name], check=True)
        install_git_plugin(workspace=workspace, source=str(provider), marketplace="lab", plugins_home=folder / "home")
    save_config(workspace / "plugin-data/akasha-lab", {"enabled": True, "memory_enabled": False})
    save_config(workspace / "plugin-data/default_memory-lab", {"mode": "active", "auto_learn": True})
    save_config(workspace / "plugin-data/context-builtin", {"recall_source": ["default_memory", "default_memory@lab"]})

    def make_host():
        return PluginManager([sources], workspace=workspace, installed_cache_root=folder / "home/cache")

    host = make_host()

    async def rpc(name, **params):
        method = host.live_root.context.require(RpcMethod.key("default-memory/" + name))
        return await method.call(method.params.model_validate(params))

    async def wait_applied(count):
        async with asyncio.timeout(15):
            while True:
                value = await rpc("status")
                assert not value["failures"], value
                if value["learning"] == {"applied": count} and value["current_source"] is None:
                    return value
                await asyncio.sleep(.02)

    async def command(line, identity, *, recover=False):
        return await host.live_root.context.require(COMMANDS).freeze().execute(
            line, session_key="scenario:controls", channel="scenario", chat_id="controls",
            sender="operator", message_id=identity, recover=recover)

    try:
        await host.load_all()
        root = host.live_root
        catalog = root.context.require(MODEL_CATALOG)
        async def model_command(operation, **fields):
            method = root.context.require(RpcMethod.key("models/command"))
            params = method.params.model_validate({"type": operation, "expected_revision": catalog.snapshot().revision, **fields})
            response = await method.call(params)
            assert response["status"] == 200, response
        await model_command("add_connection", connection_id="fixture", name="fixture", driver_id="openai-compatible",
                            endpoint=endpoint, auth_identity="fixture", credential={"api_key": "fixture"})
        await model_command("add_model", model_id="chat-fixture", connection_id="fixture", kind="chat",
                            model="memory-fixture", capabilities={"context_window": 32000, "max_output_tokens": 8000},
                            capability_sources={"context_window": "user", "max_output_tokens": "user"})
        await model_command("set_default", role="default", model_id="chat-fixture")
        await model_command("add_model", model_id="embedding-fixture", connection_id="fixture", kind="embedding",
                            model="embedding-fixture", capabilities={"embedding_dimensions": 4, "embedding_normalization": "l2"},
                            capability_sources={"embedding_dimensions": "user", "embedding_normalization": "user"},
                            make_default_embedding=True)
        append = root.context.require(ServiceKey("scenario.append"))
        await append("scenario:controls", "first", "I always prefer black coffee.")
        await asyncio.wait_for(entered.wait(), timeout=15)
        await append("scenario:controls", "second", "I prefer morning walks.")
        paused = await asyncio.wait_for(rpc("pause", request_id="pause-in-flight", expected_version=0), timeout=5)
        assert paused["paused"] and paused["control_version"] == 1 and paused["current_source"], paused
        assert calls["chat"] == 1, calls
        release.set()
        await wait_applied(1)
        before = snapshot(workspace)
        assert (await rpc("backfill", retry=True))["paused"]
        blocked = await command("/default_memory_backfill", "backfill-paused")
        assert blocked.result.kind == "error", blocked
        assert calls["chat"] == 1, calls

        settings = root.context.require(SETTINGS)
        config = await settings.read()
        assert config["can_disable"] is False and "default_memory@lab" in config["support_dependents"], config
        selection = PluginSelection(workspace).read()
        try:
            await settings.save("disable-support", config["input_ref"], {"enabled": False})
        except ValueError as error:
            assert "项目策略" in str(error), error
        else:
            raise AssertionError("Settings disabled an actively required provider")
        assert PluginSelection(workspace).read() == selection
        assert snapshot(workspace) == before

        await host.terminate_all()
        host = make_host()
        await host.load_all()
        after_restart = await rpc("status")
        assert after_restart["paused"] and after_restart["control_version"] == 1, after_restart
        assert calls["chat"] == 1, calls
        recall = await rpc("query", request_id="paused-recall", session_id="scenario:controls", query="coffee")
        assert "black coffee" in recall["text"], recall
        resumed = await command("/default_memory_resume", "resume-after-restart")
        assert resumed.result.kind == "success", resumed
        state = await wait_applied(2)
        assert not state["paused"] and state["control_version"] == 2, state
        assert calls["chat"] == 2, calls

        # Old delivery and command recovery must acknowledge their receipt, not
        # undo a more recent control intent.
        replay = await rpc("pause", request_id="pause-in-flight", expected_version=0)
        assert replay["control_receipt"]["version"] == 1 and not replay["paused"], replay
        try:
            await rpc("pause", request_id="stale-pause", expected_version=1)
        except ValueError as error:
            assert "刷新" in str(error), error
        else:
            raise AssertionError("Stale control version was accepted")
        try:
            await rpc("resume", request_id="pause-in-flight")
        except ValueError:
            pass
        else:
            raise AssertionError("Reused request identity accepted conflicting content")
        await command("/default_memory_pause", "pause-before-provider-check")
        await command("/default_memory_resume", "resume-after-restart", recover=True)
        assert (await rpc("status"))["paused"]
        assert (await rpc("status"))["control_version"] == 3

        # Core's explicit administrator switch stays available. Normal context
        # binding must explain a missing selected recall provider.
        await host.set_plugin_enabled("akasha@lab", False)
        materials = host.live_root.context.require(MATERIALS_V4)
        try:
            async with materials.bind():
                raise AssertionError("Missing recall silently accepted")
        except ValueError as error:
            assert "所选长期记忆尚未就绪" in str(error), error
        async with materials.bind(exclude_kinds=frozenset({"recall"})):
            pass
        await host.set_plugin_enabled("akasha@lab", True)
        assert (await rpc("status"))["paused"], await rpc("status")
        assert calls["chat"] == 2, calls

        append = host.live_root.context.require(ServiceKey("scenario.append"))
        await append("scenario:controls", "third", "I prefer quiet libraries.")
        before = snapshot(workspace)
        # An actual SQLite lock delays persistence while the acknowledgement is
        # cancelled. Resume must still commit once and wake the backlog worker.
        held = sqlite3.connect(workspace / "memory/default-v2.db")
        held.execute("BEGIN IMMEDIATE")
        pending = asyncio.create_task(rpc("resume", request_id="cancelled-resume", expected_version=3))
        try:
            await asyncio.sleep(.15)
            pending.cancel()
            await asyncio.sleep(.05)
            assert not pending.done(), "Cancellation released an unfinished control write"
        finally:
            held.rollback()
            held.close()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Expected acknowledgement cancellation")
        state = await wait_applied(3)
        assert not state["paused"] and state["control_version"] == 4, state
        assert calls["chat"] == 3, calls
        assert snapshot(workspace) == before

        same_state = await rpc("resume", request_id="reaffirm-running", expected_version=4)
        assert not same_state["paused"] and same_state["control_version"] == 5, same_state
        try:
            await rpc("pause", request_id="delayed-before-reaffirm", expected_version=4)
        except ValueError:
            pass
        else:
            raise AssertionError("A no-op intent did not supersede the older version")

        # Automatic recall uses the real current user Input and Context owner.
        await append("scenario:controls", "recall-input", "What coffee do I prefer?", finish=False)
        before = snapshot(workspace)
        root = host.live_root
        messages = root.context.require(MESSAGE_CATALOG).reader("scenario:controls").snapshot()
        async with root.context.require(MATERIALS_V4).bind() as view:
            prepared = await view.prepare(messages, "conversation")
            embeddings = calls["embedding"]
            repeated = await view.prepare(messages, "conversation")
            assert prepared == repeated and calls["embedding"] == embeddings
        assert "black coffee" in json.dumps(prepared), prepared
        assert snapshot(workspace) == before
        return {"pause_during_model_call": True, "paused_backfill_stays_paused": True,
                "restart_preserves_pause": True, "recall_while_paused": True,
                "resume_wakes_backlog": True, "stale_control_rejected": True,
                "request_and_command_replay": True, "settings_dependency_guard": True,
                "missing_recall_fails_loudly": True, "provider_restore_preserves_pause": True,
                "cancelled_ack_commits_and_wakes": True, "automatic_context_recall": True,
                "original_rows_unchanged": True, "calls": calls}
    finally:
        release.set()
        await host.terminate_all()
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="default-memory-controls-") as directory:
        print(json.dumps(asyncio.run(run(Path(directory))), ensure_ascii=False))
