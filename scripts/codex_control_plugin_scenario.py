"""Formal plugin installation, real Ledger attachments, bindings and remote effects."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CALLER = '''from agent.plugin_composition import ServiceKey
from plugins.tools.contract import TOOLS, ALL_TOOLS
from plugins.ledger.contract import BINDINGS, MESSAGE_WRITERS, MESSAGE_CATALOG, ARTIFACT_IMPORT, ARTIFACT_READ
from plugins.content.contract import CONTENT
api_version=3
name="control_scenario"
version="1.0.0"
inject=(TOOLS,ALL_TOOLS,BINDINGS,MESSAGE_WRITERS,MESSAGE_CATALOG,ARTIFACT_IMPORT,ARTIFACT_READ,CONTENT)
async def apply(ctx):
    async def run(callback): return await callback(ctx)
    await ctx.provide(ServiceKey("scenario.codex-control"),ctx.entrypoint(run))
'''


async def exercise(folder: Path, endpoint: str, thread_id: str, png: bytes, observer: Any) -> dict[str, bool]:
    from agent.plugin_composition import ServiceKey
    from agent.plugin_composition.config_input import save_config
    from agent.plugins.bundles import set_plugin_choice
    from agent.plugins.install import install_git_plugin
    from agent.plugins.manager import PluginManager
    from agent.plugins.selection import PluginSelection
    from agent.plugins.static_manifest import load_static_plugin_manifest
    from plugins.content.contract import CONTENT
    from plugins.ledger.contract import (ARTIFACT_IMPORT, ARTIFACT_READ, BINDINGS, MESSAGE_CATALOG, MESSAGE_WRITERS,
        AttachmentKind, CallRef, ContentPart, Input, Output, ToolCall, ToolResult)
    from plugins.tools.contract import ALL_TOOLS, TOOLS, CallSource

    folder.mkdir()
    workspace, home, sources = folder / "workspace", folder / "home", folder / "sources"
    workspace.mkdir()
    home.mkdir()
    PluginSelection(workspace).initialize()
    before_env = {key: os.environ.get(key) for key in ("HOME", "AKASHIC_PLUGIN_HOME", "AKASHIC_PLUGIN_DISTRIBUTION", "AKASHIC_EXTRA_PLUGIN_DIRS")}
    os.environ.update(HOME=str(home), AKASHIC_PLUGIN_HOME=str(home), AKASHIC_PLUGIN_DISTRIBUTION="", AKASHIC_EXTRA_PLUGIN_DIRS="")
    shutil.copytree(ROOT / "plugins", sources, ignore=shutil.ignore_patterns("__pycache__", "codex_control"))
    for source in sources.iterdir():
        if (source / "plugin.py").is_file():
            name = load_static_plugin_manifest(source).name
            set_plugin_choice(workspace, name, enabled=name in {"ledger", "content", "tools"})
    caller = sources / "control_scenario"
    caller.mkdir()
    (caller / "plugin.py").write_text(CALLER)
    provider = folder / "provider"
    shutil.copytree(ROOT / "plugins/codex_control", provider, ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["git", "init", "-q", "--initial-branch=scenario", str(provider)], check=True)
    subprocess.run(["git", "-C", str(provider), "add", "."], check=True)
    subprocess.run(["git", "-C", str(provider), "-c", "user.name=Scenario", "-c", "user.email=scenario@example.invalid",
                    "commit", "-qm", "isolated Codex control source"], check=True)
    installed = install_git_plugin(workspace=workspace, source=str(provider), marketplace="scenario", plugins_home=home)
    lost_commands = 0
    async def proxy(front: Any) -> None:
        nonlocal lost_commands
        drop_id: object = None
        async with connect(endpoint, proxy=None, max_size=16 * 1024 * 1024) as back:
            async def upstream() -> None:
                nonlocal drop_id, lost_commands
                async for raw in front:
                    message = json.loads(raw)
                    if message.get("method") == "turn/start" and "PLUGIN_LOST_ACK" in json.dumps(message.get("params")):
                        drop_id = message["id"]
                        lost_commands += 1
                    await back.send(raw)
            async def downstream() -> None:
                async for raw in back:
                    message = json.loads(raw)
                    if "method" not in message and drop_id is not None and message.get("id") == drop_id:
                        await front.close()
                        return
                    await front.send(raw)
            tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    relay = await serve(proxy, "127.0.0.1", 0, max_size=16 * 1024 * 1024)
    relay_endpoint = f"ws://127.0.0.1:{next(iter(relay.sockets)).getsockname()[1]}"
    save_config(installed.data_path, {"endpoint": relay_endpoint})
    image = folder / "fixture.png"
    image.write_bytes(png)
    saved: dict[str, Any] = {}

    async def use(ctx: Any) -> dict[str, bool]:
        catalog, bindings = ctx.require(TOOLS), ctx.require(BINDINGS)
        ref = ctx.require(ALL_TOOLS)().select("codex_control")
        binding = await catalog.bind(ref, bindings)
        imported = await ctx.require(ARTIFACT_IMPORT).import_source(str(image), AttachmentKind.IMAGE)
        content, writers = ctx.require(CONTENT), ctx.require(MESSAGE_WRITERS)
        session = "scenario:codex-control"
        reader = ctx.require(MESSAGE_CATALOG).reader(session)
        writer = writers.bind(ctx, author="user", source="conversation", body_types=(Input,),
            content={"text": content.check_text, "artifact_ref": content.check_artifact})(session)
        original = await writer.append_async("original", Input((ContentPart("text", "指挥 Windows Codex"),
                                                                ContentPart("artifact_ref", imported.artifact_id))))
        writer.expire()
        sequence = 0

        async def call(arguments: dict[str, Any], *, repeat: bool = False) -> tuple[Any, Any]:
            nonlocal sequence
            sequence += 1
            def check(item: Any) -> None:
                bindings.describe(item.binding_id, TOOLS)
            writer = writers.bind(ctx, author="assistant", source="conversation", body_types=(Output,),
                                  content={}, check_call=check)(session)
            message = await writer.append_async(f"call-{sequence}", Output((ToolCall(binding, arguments),), "continue"))
            writer.expire()
            snapshot = ctx.require(MESSAGE_CATALOG).reader(session).snapshot()
            assert snapshot[-1].message_id == message.message_id
            source = CallSource(CallRef(message.message_id, 0), snapshot)
            async with bindings.open(binding, TOOLS) as (current, metadata):
                async with current.open(metadata) as tool:
                    prepared = await tool.prepare(arguments, source)
                    if isinstance(prepared, str):
                        return prepared, None
                    result = await tool.invoke(source.effect_key, prepared)
                    if repeat:
                        again = await tool.invoke(source.effect_key, prepared)
                        assert again == result, "The same effect was not returned from its receipt"
                        saved.setdefault("receipts", []).append((source.effect_key, result))
                    writer = writers.bind(ctx, author="tool", source="conversation", body_types=(ToolResult,),
                                          content={"text": content.check_text})(session, call_ref=source.call_ref)
                    await writer.append_async(f"result-{sequence}", ToolResult(source.call_ref, result.outcome, result.parts))
                    writer.expire()
                    return json.loads(result.parts[0].value), result

        listed, _ = await call({"action": "list"})
        assert any(row["id"] == thread_id for row in listed["threads"]), listed
        selected, _ = await call({"action": "select", "thread_id": thread_id})
        assert selected["status"] == "selected", selected
        rejected, _ = await call({"action": "send", "text": "must not be sent", "image_ids": ["not-in-this-conversation"]})
        assert isinstance(rejected, str) and "当前" in rejected
        before = await observer.call("thread/turns/list", {"threadId": thread_id, "limit": 100})
        bad_image = folder / "damaged.png"
        bad_image.write_bytes(b"not a PNG image")
        damaged = await ctx.require(ARTIFACT_IMPORT).import_source(str(bad_image), AttachmentKind.IMAGE)
        writer = writers.bind(ctx, author="user", source="conversation", body_types=(Input,),
                              content={"artifact_ref": content.check_artifact})(session)
        await writer.append_async("damaged-input", Input((ContentPart("artifact_ref", damaged.artifact_id),)))
        writer.expire()
        damaged_result, _ = await call({"action": "send", "text": "must not be sent", "image_ids": [damaged.artifact_id]})
        if isinstance(damaged_result, str):
            assert "图片" in damaged_result, damaged_result
        else:
            assert damaged_result["status"] == "rejected", damaged_result
        still = await observer.call("thread/turns/list", {"threadId": thread_id, "limit": 100})
        assert len(still["data"]) == len(before["data"])
        accepted, _ = await call({"action": "send", "text": "PLUGIN_LAYER_TEXT 原图验证",
                                 "image_ids": [imported.artifact_id]}, repeat=True)
        assert accepted["status"] == "accepted" and accepted["thread_id"] == thread_id, accepted
        ended = await asyncio.wait_for(observer.wait_turn(thread_id, accepted["turn_id"]), 30)
        assert ended["status"] == "completed", ended
        after = await observer.call("thread/turns/list", {"threadId": thread_id, "limit": 100})
        assert len(after["data"]) == len(before["data"]) + 1, "Duplicate remote turn was created"
        read, _ = await call({"action": "read"})
        assert any("CODEX_CONTROL_OK" in turn["text"] for turn in read["turns"]), read
        lost, _ = await call({"action": "send", "text": "PLUGIN_LOST_ACK"}, repeat=True)
        assert lost["status"] == "unknown", lost
        for _ in range(100):
            turns = await observer.call("thread/turns/list", {"threadId": thread_id, "limit": 100})
            if len(turns["data"]) == len(after["data"]) + 1 and all(turn["status"] != "inProgress" for turn in turns["data"]):
                break
            await asyncio.sleep(0.05)
        assert len(turns["data"]) == len(after["data"]) + 1 and lost_commands == 1
        fresh = ctx.require(MESSAGE_CATALOG).reader(session)
        assert fresh.get(original.message_id) == original
        assert fresh.attachments(original.message_id) == (imported,)
        lease = await ctx.require(ARTIFACT_READ).acquire(imported)
        try:
            assert await lease.read_bytes(max_bytes=1024 * 1024) == png
        finally:
            await lease.aclose()
        cwd = (await observer.call("thread/read", {"threadId": thread_id}))["thread"]["cwd"]
        created, _ = await call({"action": "create", "cwd": cwd}, repeat=True)
        new_thread = created["thread_id"]
        assert created["status"] == "created" and new_thread != thread_id, created
        sent, _ = await call({"action": "send", "text": "PLUGIN_CREATED_CLI_TASK"})
        assert sent["thread_id"] == new_thread, sent
        finished = {"data": []}
        for _ in range(100):
            try:
                finished = await observer.call("thread/turns/list", {"threadId": new_thread, "limit": 3})
            except RuntimeError as error:
                if "list_turns is not supported yet" not in str(error) and "no rollout found" not in str(error):
                    raise
            if finished["data"] and finished["data"][0]["status"] == "completed":
                break
            await asyncio.sleep(0.1)
        assert finished["data"] and finished["data"][0]["status"] == "completed", finished
        listed, _ = await call({"action": "list"})
        assert any(row["id"] == new_thread for row in listed["threads"]), listed
        saved.update(binding=binding, session=session, original=original)
        return {"formal_plugin_install": True, "bound_tool_send": True, "selected_thread_preserved": True,
                "real_ledger_image": True, "unattached_image_rejected": True, "duplicate_effect_not_resent": True,
                "independent_observer_received_completion": True, "existing_input_and_attachment_unchanged": True,
                "create_select_send_cli_task": True,
                "lost_ack_is_unknown_not_resent": True, "damaged_image_rejected_before_send": True}

    manager = PluginManager([sources], workspace=workspace, installed_cache_root=home / "cache")
    try:
        await manager.load_all()
        assert manager.live_root is not None
        checks = await manager.live_root.context.require(ServiceKey("scenario.codex-control"))(use)
        await manager.terminate_all()
        manager = PluginManager([sources], workspace=workspace, installed_cache_root=home / "cache")
        await manager.load_all()
        assert manager.live_root is not None
        async def restored(ctx: Any) -> None:
            bindings = ctx.require(BINDINGS)
            async with bindings.open(saved["binding"], TOOLS) as (current, metadata):
                async with current.open(metadata) as tool:
                    for key, result in saved["receipts"]:
                        assert await tool.query(key) == result
        await manager.live_root.context.require(ServiceKey("scenario.codex-control"))(restored)
        checks["receipt_survives_plugin_restart"] = True
        return checks
    finally:
        await manager.terminate_all()
        relay.close()
        await relay.wait_closed()
        for key, value in before_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
