"""Windows Hook 形状的 stdin 经真实 WSL CLI、Unix socket 和 Akashic 插件写入日志。"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "sdk/python/src")]
from codex_notifications_scenario import configure
from tests.test_default_reply import application

BRIDGE = ROOT / "integrations/codex_notifications/bridge.py"


async def run(directory: Path) -> dict:
    directory.mkdir()
    state, home, workspace = directory / "outbox", directory / "home", directory / "workspace"
    home.mkdir()
    os.environ.update(HOME=str(home), AKASHIC_PLUGIN_HOME=str(home), AKASHIC_PLUGIN_DISTRIBUTION="",
                      AKASHIC_EXECUTION_MODE="local")
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "sdk/python/src")}
    endpoint = str(directory / "relay.sock")
    event = {"hook_event_name": "Stop", "session_id": "windows-codex", "turn_id": "one",
             "cwd": "\\\\wsl.localhost\\Ubuntu-24.04\\home\\example\\项目",
             "last_assistant_message": "本轮已有结果，等待用户确认。",
             "transcript_path": "/must-not-be-read", "input_messages": ["do-not-forward-sentinel"]}

    async def command(action, *, payload=None, extra=()):
        process = await asyncio.create_subprocess_exec(sys.executable, str(BRIDGE), action,
            "--state", str(state), *extra, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment)
        out, err = await asyncio.wait_for(process.communicate(
            json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None), 20)
        return process.returncode, out.decode(), err.decode()

    collected = await asyncio.gather(*(command("collect", payload=event) for _ in range(12)))
    assert all(code == 0 and out.strip() == "{}" for code, out, _ in collected), collected
    code, out, _ = await command("status")
    assert json.loads(out)["counts"] == {"pending": 1}
    code, out, _ = await command("drain", extra=("--endpoint", endpoint, "--now"))
    assert code == 1 and json.loads(out)["counts"] == {"pending": 1}
    drop_ack = True
    tasks = set()

    async def proxy(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        upstream_reader, upstream_writer = await asyncio.open_unix_connection(workspace / "akashic.sock")
        submit_ids = set()

        async def forward():
            while line := await reader.readline():
                frame = json.loads(line)
                if frame.get("method") == "codex/notifications/submit":
                    submit_ids.add(frame["id"])
                upstream_writer.write(line)
                await upstream_writer.drain()

        async def backward():
            while line := await upstream_reader.readline():
                frame = json.loads(line)
                if drop_ack and frame.get("id") in submit_ids:
                    return
                writer.write(line)
                await writer.drain()

        workers = [asyncio.create_task(forward()), asyncio.create_task(backward())]
        try:
            await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for stream in (writer, upstream_writer):
                stream.close()
                with suppress(ConnectionError):
                    await stream.wait_closed()
            tasks.discard(task)

    async with application(directory, replying=False, extra_sources=configure) as (log, host):
        server = await asyncio.start_unix_server(proxy, endpoint)
        try:
            code, out, err = await command("drain", extra=("--endpoint", endpoint, "--now"))
            assert code == 1, (out, err)
            targets = [entry.session_id for entry in log.catalog().sessions(visibility="listed").items]
            assert len(targets) == 1
            target = targets[0]
            before = log.reader(target).snapshot()
            assert len(before) == 1 and "do-not-forward-sentinel" not in str(before)
            drop_ack = False
            code, out, err = await command("drain", extra=("--endpoint", endpoint, "--now"))
            assert code == 0 and json.loads(out)["counts"] == {"committed": 1}, (out, err)
            assert log.reader(target).snapshot() == before
            long_event = {**event, "turn_id": "two", "last_assistant_message": "摘要" * 4000}
            assert (await command("collect", payload=long_event))[0] == 0
            assert (await command("drain", extra=("--endpoint", endpoint, "--now")))[0] == 0
            text = log.reader(target).snapshot()[-1].body.parts[0].value
            assert "仅保留前 6000 字符" in text and len(text) < 6400
            assert (await command("collect", payload={**event, "last_assistant_message": "变更正文"}))[0] == 1
            assert (await command("drain", extra=("--endpoint", str(workspace / "akashic.sock"))))[0] == 1
            assert len(log.reader(target).snapshot()) == 2
        finally:
            server.close()
            await server.wait_closed()
            if tasks:
                await asyncio.gather(*tasks)
    return {"concurrent_first_hooks": True, "offline_retained": True, "lost_rpc_ack_replayed_once": True,
            "summary_bounded": True, "transcript_and_inputs_not_forwarded": True,
            "conflict_preserves_original": True, "destination_binding_fixed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.output.resolve()))
    (args.output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
