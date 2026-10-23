"""The Computer bridge must start through its declared Python runtime."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import shutil
import sys
from threading import Thread
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from agent.plugin_composition import Context
from agent.plugins.static_manifest import load_static_plugin_manifest, materialize_command
from plugins.computer import plugin as computer
from plugins.mcp.contract import MCP_SERVERS, McpServerDefinition


@pytest.mark.asyncio
async def test_registered_computer_mcp_uses_owned_python_not_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AKASHIC_PLUGIN_HOME", str(tmp_path / "plugin-home"))
    definitions: list[McpServerDefinition] = []

    async def register(_ctx, definition):
        definitions.append(definition)

    mcp = SimpleNamespace(register=register)
    capabilities = AsyncMock()
    ctx = SimpleNamespace(data_root=tmp_path / "data", provide=AsyncMock(), on=AsyncMock(),
                          require=lambda key: mcp if key == MCP_SERVERS else capabilities)
    await computer.apply(cast(Context, ctx))
    assert len(definitions) == 1
    definition = definitions[0]

    artifact = tmp_path / "computer"
    shutil.copytree(Path(computer.__file__).parent, artifact,
                    ignore=shutil.ignore_patterns("__pycache__"))
    manifest = load_static_plugin_manifest(artifact)
    environment = tmp_path / "environment"
    interpreter = environment / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    interpreter.chmod(0o755)
    command = materialize_command(artifact, manifest.python, definition.command,
                                  definition.cwd, environment_root=environment)

    # The host's generic python3 is deliberately unusable; a shebang bypass must fail.
    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    generic_python = path_bin / "python3"
    generic_python.write_text("#!/bin/sh\necho 'unexpected host python3' >&2\nexit 71\n")
    generic_python.chmod(0o755)

    class DriverStatus(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/driver/status"
            body = json.dumps({"version": 3, "source": True, "ready": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), DriverStatus)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*command, cwd=artifact,
            env={"PATH": str(path_bin), "COMPUTER_URL": f"http://127.0.0.1:{server.server_port}"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        wire = "".join(json.dumps(message) + "\n" for message in messages).encode()
        stdout, stderr = await asyncio.wait_for(process.communicate(wire), 10)
        assert process.returncode == 0, stderr.decode()
        replies = [json.loads(line) for line in stdout.splitlines()]
        assert replies[0]["id"] == 1 and replies[0]["result"]["serverInfo"]["name"] == "akashic-computer"
        assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        server.shutdown()
        server.server_close()
        thread.join()
