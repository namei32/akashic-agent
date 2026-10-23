"""Exercise the installed companion's HTTP and durable-state boundaries."""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil

import httpx
import pytest

from agent.plugins.manager import PluginManager
from core.common.unix_socket import socket_alias_path
from plugins.content.plugin import check_text
from plugins.ledger.contract import ContentPart, Output
from tests.test_default_reply import application


@asynccontextmanager
async def companion_application(tmp_path):
    folder = tmp_path.resolve()
    calls = folder / "model-calls.jsonl"

    def configure(sources):
        shutil.copytree(Path(__file__).parents[1] / "plugins/companion", sources / "companion",
                        ignore=shutil.ignore_patterns("__pycache__"))
        entry = sources / "test_provider/plugin.py"
        source = entry.read_text()
        start, end = source.index("    class Driver:"), source.index("    descriptor =")
        source = source[:start] + '''    class Driver:
        max_tool_schemas = None
        def estimate_context_tokens(self, messages, tools):
            return 10
        async def complete(self, request):
            import asyncio, json
            assert not request.tools
            question = str(request.messages[-1]["content"])
            with Path(CALLS_FILE).open("a") as stream:
                stream.write(json.dumps({"question": question, "system": request.system_prompt}) + "\\n")
            if request.on_delta:
                await request.on_delta({"content_delta": "partial explanation"})
            if question == "slow explanation":
                await asyncio.sleep(30)
            return LLMResponse("explanation: " + question)
'''.replace("CALLS_FILE", repr(str(calls))) + source[end:]
        source = source.replace("    class Target:", "        independent_execution = execution\n    class Target:")
        entry.write_text(source)

    async with application(folder, replying=False, extra_sources=configure) as (log, host):
        log.writer("parent-a", author="assistant", source="fixture", body_types=(Output,),
                   content={"text": check_text}).append(
            "original-message", Output((ContentPart("text", "original parent content"),), "complete"))
        socket = socket_alias_path(folder / "workspace/runtime", "companion.sock")
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket)),
                base_url="http://fixture", timeout=10,
                headers={"x-akashic-csrf": "1", "origin": "http://fixture"}) as client:
            yield log, host, client, calls, socket


@pytest.mark.asyncio
async def test_companion_http_isolates_history_and_deduplicates_requests(tmp_path):
    async with companion_application(tmp_path) as (log, _host, client, calls, _socket):
        original = log.reader("parent-a").snapshot()
        first_visit = await client.get("/api/companion/state", params={"parent": "parent-a"})
        assert first_visit.status_code == 200
        assert first_visit.json()["history"] == []
        assert not calls.exists()

        request = {"request_id": "companion-request-0001", "parent": "parent-a",
                   "question": "explain this", "reference": {
                       "message_id": "original-message", "text": "original parent content"}}
        invalid = {**request, "reference": {"message_id": "missing", "text": "uncommitted"}}
        assert (await client.post("/api/companion/ask", json=invalid)).status_code == 409
        assert not calls.exists()
        assert (await client.post("/api/companion/ask", json=request)).status_code == 200
        events = await client.get("/api/companion/events/" + request["request_id"])
        completed = json.loads(events.text.strip().splitlines()[-1])
        assert completed["status"] == "complete" and completed["text"] == "explanation: explain this"

        assert (await client.post("/api/companion/ask", json=request)).status_code == 200
        assert len(calls.read_text().splitlines()) == 1
        conflict = await client.post("/api/companion/ask", json={**request, "question": "different"})
        assert conflict.status_code == 409
        assert len(calls.read_text().splitlines()) == 1
        own = (await client.get("/api/companion/state", params={"parent": "parent-a"})).json()
        other = (await client.get("/api/companion/state", params={"parent": "parent-b"})).json()
        assert len(own["history"]) == 2 and own["reference"] == request["reference"]
        assert other["history"] == [] and other["reference"] is None
        assert log.reader("parent-a").snapshot() == original

        rejected = await client.post("/api/companion/preferences", json={"explanation": "blocked"},
                                     headers={"origin": "http://other-origin"})
        assert rejected.status_code == 403


@pytest.mark.asyncio
async def test_companion_stop_and_restart_preserve_partial_output(tmp_path):
    async with companion_application(tmp_path) as (log, host, client, calls, socket):
        original = log.reader("parent-a").snapshot()
        saved = await client.post("/api/companion/preferences", json={"explanation": "use an example first"})
        assert saved.status_code == 200
        request = {"request_id": "companion-slow-request-0001", "parent": "parent-a",
                   "question": "slow explanation", "reference": None}
        assert (await client.post("/api/companion/ask", json=request)).status_code == 200
        async with asyncio.timeout(5):
            while True:
                state = (await client.get("/api/companion/state", params={"parent": "parent-a"})).json()
                if state["active"] and state["active"]["text"]:
                    break
                await asyncio.sleep(.01)
        stopped = await client.post("/api/companion/stop/" + request["request_id"], json={})
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "interrupted"
        assert stopped.json()["text"] == "partial explanation"
        again = await client.post("/api/companion/stop/" + request["request_id"], json={})
        assert again.json()["text"] == "partial explanation"
        captured = calls.read_text().splitlines()
        assert len(captured) == 1 and "use an example first" in json.loads(captured[0])["system"]
        assert log.reader("parent-a").snapshot() == original

        await host.terminate_all()
        folder = tmp_path.resolve()
        restored = PluginManager([folder / "plugins"], workspace=folder / "workspace",
                                 installed_cache_root=folder / "home/cache")
        try:
            await restored.load_all()
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket)),
                                        base_url="http://fixture", timeout=10) as fresh:
                state = (await fresh.get("/api/companion/state", params={"parent": "parent-a"})).json()
                assert len(state["history"]) == 2 and state["history"][-1]["partial"]
                assert state["active"]["status"] == "interrupted"
                assert state["preferences"]["explanation"] == "use an example first"
                assert calls.read_text().splitlines() == captured
                rejected = await fresh.post("/api/companion/preferences", json={"explanation": "blocked"})
                assert rejected.status_code == 403
        finally:
            await restored.terminate_all()
