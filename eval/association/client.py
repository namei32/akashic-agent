"""Use a frozen provider subprocess while evaluating the current memory engine."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from uuid import uuid4
from .schema import Settings, canonical


class RemoteModelError(RuntimeError):
    pass


class RemoteProtocolError(RuntimeError):
    pass


class RuntimeClient:
    def __init__(self, process, settings, ready):
        self.process, self.settings, self.ready = process, settings, ready
        self.poisoned = False
        self.lock = asyncio.Lock()

    def embedding_descriptor(self):
        descriptor = self.ready["embedding"]
        if (descriptor["model"], descriptor["dimensions"]) != (self.settings.embedding_model, self.settings.dimensions):
            raise ValueError("Configured embedding provider differs from frozen settings")
        return descriptor

    async def _call(self, operation, **payload):
        if self.poisoned:
            raise RemoteProtocolError("Provider channel requires restart after interruption")
        identity = str(uuid4())
        async with self.lock:
            try:
                self.process.stdin.write((canonical({"id": identity, "operation": operation, **payload}) + "\n").encode())
                await self.process.stdin.drain()
                line = await self.process.stdout.readline()
                if not line:
                    raise RemoteProtocolError("Provider process exited; see private provider log")
                response = json.loads(line)
                if response.get("id") != identity:
                    raise RemoteProtocolError("Provider response identity mismatch")
            except BaseException:
                self.poisoned = True
                raise
        if response.get("error"):
            raise RemoteModelError(response["error"])
        return response["result"]

    async def embed(self, texts):
        return await self._call("embed", texts=texts)

    async def complete(self, system, user, output_tokens):
        result = await self._call("complete", system=system, user=user, output_tokens=output_tokens)
        if (result.get("model"), result.get("effort")) != (self.settings.model, self.settings.effort):
            raise ValueError("Model identity changed across provider transport")
        if result.get("tool_calls"):
            raise ValueError("Fixed evaluator proposed tools; none were executed")
        return result


@asynccontextmanager
async def runtime_client(config: Path, workspace: Path, settings: Settings):
    root = Path(os.environ["AKASHIC_EVAL_RUN_ROOT"])
    python = (root / "python-path.txt").read_text().strip()
    log = root / "logs" / (workspace.name + "-provider.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as stderr:
        process = await asyncio.create_subprocess_exec(python, str(root / "provider_server.py"), str(config), str(workspace), canonical(settings),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr, limit=8 * 1024 * 1024)
        assert process.stdin is not None and process.stdout is not None
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 90)
            if not line:
                raise RemoteProtocolError("Provider failed to initialize; see private provider log")
            ready = json.loads(line)
            if ready.get("kind") != "ready":
                raise RemoteProtocolError("Provider initialization protocol mismatch")
            yield RuntimeClient(process, settings, ready)
        finally:
            if process.returncode is None:
                try:
                    process.stdin.write((canonical({"id": "close", "operation": "close"}) + "\n").encode())
                    await process.stdin.drain()
                    await asyncio.wait_for(process.wait(), 15)
                except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 10)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
