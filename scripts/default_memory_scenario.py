"""Real plugin composition, ledger, model HTTP driver, SQLite and restart."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROBE = '''
from agent.plugin_composition import ServiceKey
from plugins.ledger.contract import MESSAGE_WRITERS, SESSION_ADMISSION, OWNER_STATE, SessionAttributes, Input, Output, ContentPart
from plugins.content.contract import CONTENT
api_version = 3
name = "memory_probe"
version = "1.0.0"
inject = (MESSAGE_WRITERS, SESSION_ADMISSION, CONTENT)
async def apply(ctx):
    admission = ctx.require(SESSION_ADMISSION)
    await admission.register_dimension(ctx, name="project", check=lambda value: None)
    writers = ctx.require(MESSAGE_WRITERS)
    content = {"text": ctx.require(CONTENT).check_text}
    user = writers.bind(ctx, author="user", source="conversation", body_types=(Input,), content=content)
    assistant = writers.bind(ctx, author="assistant", source="conversation", body_types=(Output,), content=content)
    async def append(session, identity, message, learning="eligible", scope=(), finish=True):
        admission.ensure(ctx, session, SessionAttributes(learning=learning, scope=scope))
        user(session).append(identity, Input((ContentPart("text", message),)))
        if finish:
            assistant(session).append(identity + ":reply", Output((ContentPart("text", "I understand."),), "complete"))
    await ctx.provide(ServiceKey("scenario.append"), ctx.entrypoint(append))
'''


async def run(folder: Path, extension=None) -> dict[str, object]:
    from agent.plugins.manager import PluginManager
    from agent.plugins.selection import PluginSelection
    from agent.plugin_composition import ServiceKey
    from agent.plugin_composition.config_input import save_config
    from plugins.models.contract import MODEL_CATALOG, ModelCapabilities, CapabilitySources
    from plugins.models.settings import MODEL_SETTINGS, AddConnection, AddModel, SetDefaultModel
    from plugins.akasha.contract import SEMANTIC_INTEREST, EMBEDDING_MEMORY_PLUGIN
    from plugins.ledger.contract import MESSAGE_CATALOG, BINDINGS, Output, ToolCall, CallRef
    from plugins.tools.contract import ALL_TOOLS, TOOLS, CallSource
    from plugins.default_memory.contract import DEFAULT_MEMORY

    requests = {"chat": 0, "embedding": 0}
    bad_reply = True

    async def serve(reader, writer):
        nonlocal bad_reply
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode()
            first = headers.split("\r\n")[0]
            size = next((int(line.split(":", 1)[1]) for line in headers.split("\r\n")
                         if line.lower().startswith("content-length:")), 0)
            payload = json.loads(await reader.readexactly(size)) if size else {}
            if "/embeddings" in first:
                requests["embedding"] += 1
                values = payload["input"]
                if isinstance(values, str):
                    values = [values]
                def vector(value):
                    value = value.lower()
                    if "secret" in value:
                        return [0., 1., 0., 0.]
                    if "coffee" in value:
                        return [1., 0., 0., 0.]
                    return [0., 0., 1., 0.]
                result = {"data": [{"index": i, "embedding": vector(value)}
                                   for i, value in enumerate(values)], "usage": {"prompt_tokens": 1, "total_tokens": 1}}
            elif "/chat/completions" in first:
                prompt = payload["messages"][-1]["content"]
                if "原始资料：" in prompt:
                    requests["chat"] += 1
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
            if "/chat/completions" in first and "malformed fixture" in str(payload) and bad_reply:
                result["choices"][0]["message"]["content"] = "not a memory JSON object"
                bad_reply = False
            body = json.dumps(result).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " +
                         str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    endpoint = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
    workspace = folder / "workspace"
    workspace.mkdir()
    PluginSelection(workspace).initialize()
    sources = folder / "sources"
    for name in ("ledger", "commands", "content", "context", "tools", "turn_projection",
                 "models", "openai_compatible", "akasha", "default_memory"):
        shutil.copytree(ROOT / "plugins" / name, sources / name, ignore=shutil.ignore_patterns("__pycache__"))
    (sources / "memory_probe").mkdir()
    (sources / "memory_probe/plugin.py").write_text(PROBE)
    save_config(workspace / "plugin-data/akasha-builtin", {"enabled": True, "memory_enabled": False})
    save_config(workspace / "plugin-data/default_memory-builtin", {"mode": "active", "auto_learn": False})
    os.environ.update(HOME=str(folder / "home"), AKASHIC_PLUGIN_HOME=str(folder / "home"), AKASHIC_PLUGIN_DISTRIBUTION="")
    host = PluginManager([sources], workspace=workspace, installed_cache_root=folder / "home/cache")
    try:
        await host.load_all()
        root = host.live_root
        if root is None:
            raise AssertionError("Missing composition")
        from plugins.gateway.contract import RpcMethod
        catalog = root.context.require(MODEL_CATALOG)
        async def command(operation, **fields):
            method = root.context.require(RpcMethod.key("models/command"))
            payload = method.params.model_validate({"type": operation, "expected_revision": catalog.snapshot().revision, **fields})
            response = await method.call(payload)
            assert response["status"] == 200, response
        await command("add_connection", connection_id="fixture", name="fixture", driver_id="openai-compatible",
                      endpoint=endpoint, auth_identity="fixture", credential={"api_key": "fixture"})
        await command("add_model", model_id="chat-fixture", connection_id="fixture", kind="chat",
                      model="memory-fixture", capabilities={"context_window": 32000, "max_output_tokens": 8000},
                      capability_sources={"context_window": "user", "max_output_tokens": "user"})
        await command("set_default", role="default", model_id="chat-fixture")
        await command("add_model", model_id="embedding-fixture", connection_id="fixture", kind="embedding",
                      model="embedding-fixture", capabilities={"embedding_dimensions": 4, "embedding_normalization": "l2"},
                      capability_sources={"embedding_dimensions": "user", "embedding_normalization": "user"},
                      make_default_embedding=True)
        append = root.context.require(ServiceKey("scenario.append"))
        await append("scenario:global", "coffee", "I always prefer black coffee.")
        await append("scenario:excluded", "excluded", "Never learn this.", learning="excluded")
        await append("scenario:open", "unfinished", "This turn is not finished.", finish=False)
        # Seed operator-owned scope settings in this disposable ledger.
        with sqlite3.connect(workspace / "sessions.db") as db:
            for key, value in (
                ("project=private", {"learn": "isolated"}),
                ("project=off", {"learn": "off"}),
                ("project=hidden", {"learn": "off", "recall": False}),
            ):
                db.execute("INSERT INTO owner_records VALUES (?,?,1,?)",
                           ("plugin:akasha:scope-policy", key, json.dumps(value)))
        await append("scenario:private", "private", "My secret coffee preference is decaf.", scope=(("project", "private"),))
        await append("scenario:off", "off", "Off coffee memory.", scope=(("project", "off"),))
        await append("scenario:hidden", "hidden", "Hidden coffee memory.", scope=(("project", "hidden"),))
        before = snapshot(workspace)
        port = root.context.require(DEFAULT_MEMORY)
        status = await port.consume()
        assert not status["failures"], status
        assert status["learning"] == {"applied": 2}, status
        assert requests["chat"] == 2, requests
        query_method = root.context.require(RpcMethod.key("default-memory/query"))
        query = await query_method.call(query_method.params.model_validate({
            "request_id": "scenario:first", "session_id": "scenario:global", "query": "coffee",
        }))
        assert "black coffee" in query["text"], query
        assert "secret" not in query["text"], query
        private = await port.recall("scenario:private", "scenario:private", "coffee")
        assert "secret coffee" in private["text"] and "black coffee" not in private["text"], private
        try:
            await port.recall("scenario:hidden", "scenario:hidden", "coffee")
        except PermissionError:
            pass
        else:
            raise AssertionError("Recall-disabled project leaked memory")
        after_requests = dict(requests)
        assert await port.recall("operator:scenario:first", "scenario:global", "coffee") == query
        await port.consume()
        assert requests == after_requests, (requests, after_requests)
        assert snapshot(workspace) == before
        assert root.context.require(EMBEDDING_MEMORY_PLUGIN) is not None
        interest = root.context.require(SEMANTIC_INTEREST)
        assert interest.decision() is True and interest.status() is None
        from datetime import datetime, UTC
        scores = await interest.score(["coffee"], cutoff=datetime.now(UTC).isoformat())
        assert scores[0] > 0, scores
        assert not (workspace / "memory/akasha.db").exists()
        with sqlite3.connect(workspace / "sessions-derived.db") as derived:
            assert derived.execute(
                "SELECT count(*) FROM message_embeddings WHERE message_id IN ('off','off:reply','hidden','hidden:reply','excluded','excluded:reply')"
            ).fetchone()[0] == 0
        await append("scenario:retry", "retry", "I prefer tea; malformed fixture.")
        before = snapshot(workspace)
        failed = await port.consume()
        assert len(failed["failures"]) == 1, failed
        assert requests["chat"] == 3, requests
        await host.terminate_all()
        host = PluginManager([sources], workspace=workspace, installed_cache_root=folder / "home/cache")
        await host.load_all()
        root = host.live_root
        port = root.context.require(DEFAULT_MEMORY)
        failed = await port.consume()
        assert len(failed["failures"]) == 1 and requests["chat"] == 3, (failed, requests)
        recovered = await port.consume(retry=True)
        assert not recovered["failures"] and recovered["learning"] == {"applied": 3}, recovered
        assert requests["chat"] == 4, requests
        # A real competing SQLite writer holds the disk operation open.
        # Cancellation must not release the plugin call while that job runs.
        held = sqlite3.connect(workspace / "memory/default-v2.db")
        held.execute("BEGIN IMMEDIATE")
        before_cancel = dict(requests)
        pending = asyncio.create_task(port.recall("scenario:cancel", "scenario:global", "coffee"))
        try:
            await asyncio.sleep(.15)
            pending.cancel()
            await asyncio.sleep(.05)
            assert not pending.done(), "Cancellation released an unfinished disk job"
        finally:
            held.rollback()
            held.close()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Expected cancellation after the disk job settled")
        assert requests == before_cancel
        memory_id = query["memory_ids"][0]
        # Use the registered, bound tool against an actual call source.
        tools = root.context.require(TOOLS)
        tool = next(ref for ref in root.context.require(ALL_TOOLS)().refs if ref.name == "forget_memory")
        binding = await tools.bind(tool, root.context.require(BINDINGS))
        messages = root.context.require(MESSAGE_CATALOG).reader("scenario:global").snapshot()
        source = CallSource(CallRef("scenario-forget-call", 0), messages)
        # ToolCatalog.open consumes the saved binding metadata and its provider.
        async with tools.open(root.context.require(BINDINGS).describe(binding, TOOLS)) as bound:
            prepared = await bound.prepare({"memory_ids": [memory_id], "reason": "user asked to forget"}, source)
            assert isinstance(prepared, dict), prepared
            result = await bound.invoke(source.effect_key, prepared)
            assert result.outcome == "success", result
            assert await bound.query(source.effect_key) == result
        assert snapshot(workspace) == before
        await host.terminate_all()
        host = PluginManager([sources], workspace=workspace, installed_cache_root=folder / "home/cache")
        await host.load_all()
        port = host.live_root.context.require(DEFAULT_MEMORY)
        assert (await port.recall("scenario:after-forget", "scenario:global", "coffee"))["memory_ids"] == []
        assert await port.recall("operator:scenario:first", "scenario:global", "coffee") == query
        await port.consume()
        assert requests["chat"] == 4, requests
        assert snapshot(workspace) == before
        # Explicit procedure memory uses the real registered tool and stores its steps.
        append = host.live_root.context.require(ServiceKey("scenario.append"))
        await append("scenario:explicit", "remember-user",
                     "Remember my reporting procedure: inspect evidence, then write the report.", finish=False)
        before_explicit = snapshot(workspace)
        tools = host.live_root.context.require(TOOLS)
        bindings = host.live_root.context.require(BINDINGS)
        tool = next(ref for ref in host.live_root.context.require(ALL_TOOLS)().refs if ref.name == "remember_memory")
        binding = await tools.bind(tool, bindings)
        messages = host.live_root.context.require(MESSAGE_CATALOG).reader("scenario:explicit").snapshot()
        source = CallSource(CallRef("scenario-remember-call", 0), messages)
        async with tools.open(bindings.describe(binding, TOOLS)) as bound:
            prepared = await bound.prepare({"summary": "Reporting procedure", "kind": "procedure",
                                            "steps": ["Inspect evidence", "Write report"]}, source)
            assert isinstance(prepared, dict), prepared
            result = await bound.invoke(source.effect_key, prepared)
            assert result.outcome == "success"
            assert await bound.query(source.effect_key) == result
            assert await bound.invoke(source.effect_key, prepared) == result
        with sqlite3.connect(workspace / "memory/default-v2.db") as db:
            facts = [json.loads(row[0]) for row in db.execute("SELECT fact FROM memory_items WHERE kind='procedure'")]
            assert len(facts) == 1 and facts[0]["steps"] == ["Inspect evidence", "Write report"], facts
        assert snapshot(workspace) == before_explicit
        extra = {} if extension is None else await extension(host, append, workspace, requests)
        return {"real_plugin_composition": True, "http_driver": True, "eligible_only": True, "project_isolation": True, "wake_new_evidence": True,
                "idempotent_learning": True, "frozen_recall": True, "forget_and_restart": True, "durable_failure_and_explicit_retry": True, "cancellation_drains_sqlite": True, "explicit_procedure_memory": True,
                "original_rows_unchanged": True, "requests": requests, **extra}
    finally:
        await host.terminate_all()
        server.close()
        await server.wait_closed()


def snapshot(workspace: Path):
    with sqlite3.connect(workspace / "sessions.db") as db:
        return db.execute("SELECT * FROM messages ORDER BY session_key,seq").fetchall()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="default-memory-scenario-") as directory:
        print(json.dumps(asyncio.run(run(Path(directory))), ensure_ascii=False))
