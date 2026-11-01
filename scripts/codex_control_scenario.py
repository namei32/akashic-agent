"""Exercise a real Codex app-server with an isolated HOME and local Responses server."""
from __future__ import annotations

import argparse
import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time
import tempfile
from typing import Any
import zlib

ROOT = Path(__file__).resolve().parents[1]
def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def image(rgb: tuple[int, int, int]) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress((b"\0" + bytes(rgb) * 32) * 32)) + png_chunk(b"IEND", b""))


PNG = image((20, 100, 200))


class Responses(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    hold = threading.Event()
    holding = threading.Event()

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        if "SCENARIO_HOLD" in json.dumps(body):
            self.holding.set()
            self.hold.wait(15)
        message = {"id": "msg_fixture", "type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": "CODEX_CONTROL_OK", "annotations": []}]}
        response = {"id": "resp_fixture", "object": "response", "status": "completed", "output": [message],
                    "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}
        events = [("response.created", {"response": {**response, "status": "in_progress", "output": []}}),
                  ("response.output_item.added", {"output_index": 0, "item": {**message, "content": []}}),
                  ("response.output_text.delta", {"item_id": "msg_fixture", "output_index": 0,
                                                 "content_index": 0, "delta": "CODEX_CONTROL_OK"}),
                  ("response.output_item.done", {"output_index": 0, "item": message}),
                  ("response.completed", {"response": response})]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for kind, value in events:
                self.wfile.write(("event: " + kind + "\ndata: " + json.dumps({"type": kind, **value}) + "\n\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def run(cli: str, directory: Path, ws_port: int, *, with_plugin: bool = False) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True)
    home, workspace = directory / "codex-home", directory / "repo"
    home.mkdir()
    workspace.mkdir()
    native = lambda path: subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip() \
        if cli.lower().endswith(".exe") else str(path)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Responses)
    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
    worker.start()
    (home / "config.toml").write_text(
        'model = "gpt-6.1-sol"\nmodel_provider = "akashic_fixture"\napproval_policy = "on-request"\n'
        'sandbox_mode = "read-only"\n[model_providers.akashic_fixture]\nname = "Akashic validation"\n'
        f'base_url = "http://127.0.0.1:{upstream.server_port}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n')
    env = dict(os.environ, CODEX_HOME=native(home))
    if cli.lower().endswith(".exe"):
        env["WSLENV"] = ":".join(filter(None, [env.get("WSLENV", ""), "CODEX_HOME"]))
    spec = importlib.util.spec_from_file_location("scenario_codex_client", ROOT / "plugins/codex_control/client.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.Client(f"ws://127.0.0.1:{ws_port}", timeout=20)
    output = (directory / "app-server.log").open("wb")
    process = subprocess.Popen([cli, "app-server", "--listen", f"ws://127.0.0.1:{ws_port}"],
                               env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output)
    try:
        for _ in range(100):
            try:
                await client.open()
                break
            except (OSError, module.CodexError):
                if process.poll() is not None:
                    raise RuntimeError("Isolated Codex exited; inspect app-server.log")
                await asyncio.sleep(0.1)
        config = await client.call("config/read", {"includeLayers": False})
        assert "akashic_fixture" in json.dumps(config), "CODEX_HOME isolation failed"
        opened = await client.call("thread/start", {"cwd": native(workspace)})
        thread = opened["thread"]["id"]
        text = '中文指令\nquotes: " $() ` literal'
        receipt = await client.call("turn/start", {"threadId": thread, "input": [
            {"type": "text", "text": text},
            {"type": "image", "url": "data:image/png;base64," + base64.b64encode(PNG).decode()}]})
        first = await asyncio.wait_for(client.wait_turn(thread, receipt["turn"]["id"]), timeout=30)
        assert first["status"] == "completed", first
        image_found = False
        for request in Responses.requests:
            for item in request.get("input", []):
                for content in item.get("content", []) if isinstance(item, dict) else []:
                    if content.get("type") == "input_image":
                        image_found = base64.b64decode(content["image_url"].split(",", 1)[1]) == PNG
        if not image_found:
            summary = [{"keys": list(request), "content": [
                {"type": content.get("type"), "keys": list(content),
                 "image_url_prefix": str(content.get("image_url", ""))[:50]}
                for item in request.get("input", []) if isinstance(item, dict)
                for content in item.get("content", []) if isinstance(content, dict)]} for request in Responses.requests]
            print("IMAGE_REQUEST_SHAPE", json.dumps(summary))
        assert image_found, "Original image bytes did not reach the model request"
        read = await client.read_thread(thread)
        assert any("CODEX_CONTROL_OK" in turn["text"] for turn in read["turns"]), read
        await client.call("thread/resume", {"threadId": thread, "excludeTurns": True})
        next_turn = await client.call("turn/start", {"threadId": thread,
                                  "input": [{"type": "text", "text": "SCENARIO_HOLD"}]})
        turn_id = next_turn["turn"]["id"]
        for _ in range(100):
            if Responses.holding.is_set():
                break
            await asyncio.sleep(0.05)
        assert Responses.holding.is_set(), "The controlled long request did not start"
        stale_rejected = False
        try:
            await client.call("turn/interrupt", {"threadId": thread, "turnId": first["id"]})
        except module.CodexError:
            stale_rejected = True
        assert stale_rejected, "A stale turn ID was allowed to interrupt newer work"
        steer = await client.call("turn/steer", {"threadId": thread, "expectedTurnId": turn_id,
                                                "input": [{"type": "text", "text": "调整为只报告状态"}]})
        assert steer["turnId"] == turn_id
        await client.call("turn/interrupt", {"threadId": thread, "turnId": turn_id})
        interrupted = await asyncio.wait_for(client.wait_turn(thread, turn_id), timeout=10)
        assert interrupted["status"] == "interrupted", interrupted
        checks: dict[str, object] = {"native_codex_version": subprocess.check_output([cli, "--version"], text=True).strip(),
                "text_and_original_image": True, "results_readable": True, "same_thread_resume": True,
                "active_turn_steer": True, "interrupt_observed": True, "isolated_home": True,
                "stale_interrupt_rejected": True,
                "thread_id": thread, "directory": str(directory)}
        if with_plugin:
            from codex_control_plugin_scenario import exercise
            Responses.hold.set()
            plugin_png = image((200, 70, 40))
            with tempfile.TemporaryDirectory(prefix="akashic-codex-plugin-") as temporary:
                checks.update(await exercise(Path(temporary) / "fixture", client.url, thread, plugin_png, client))
            payloads = [request for request in Responses.requests if "PLUGIN_LAYER_TEXT" in json.dumps(request)]
            assert any(base64.b64decode(content["image_url"].split(",", 1)[1]) == plugin_png
                       for request in payloads for item in request.get("input", []) if isinstance(item, dict)
                       for content in item.get("content", []) if content.get("type") == "input_image")
            alive = await client.call("thread/read", {"threadId": thread, "includeTurns": False})
            checks["plugin_shutdown_does_not_stop_codex"] = alive["thread"]["id"] == thread
        return checks
    finally:
        Responses.hold.set()
        await client.aclose()
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        output.close()
        await asyncio.to_thread(upstream.shutdown)
        upstream.server_close()
        worker.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-cli", required=True)
    parser.add_argument("--directory", required=True, type=Path, help="New disposable directory; Windows CLI needs a Windows-mounted path")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--with-plugin", action="store_true", help="Also install and invoke the real Akashic plugin")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.codex_cli, args.directory, args.port or port(), with_plugin=args.with_plugin)), ensure_ascii=False))
