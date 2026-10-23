#!/usr/bin/env python3
"""Codex Hook 只落盘；独立转发进程经官方 Akashic SDK 发送与核对回执。"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
from pathlib import Path
import sys

from outbox import Outbox


async def drain(box: Outbox, endpoint: str, *, now: bool = False) -> dict:
    from akashic_sdk import AsyncAkashic, RemoteError

    box.bind(endpoint)
    sent = 0
    pending = box.pending(now=now)
    if not pending:
        return {"committed": 0, **box.status()}
    try:
        async with asyncio.timeout(10):
            client = await AsyncAkashic.connect(endpoint)
    except (OSError, ConnectionError, TimeoutError) as error:
        box.fail(pending[0]["id"], f"{type(error).__name__}: {error}")
        return {"committed": 0, **box.status()}
    async with client:
        for row in pending:
            try:
                async with asyncio.timeout(10):
                    receipt = await client.request("codex/notifications/submit", json.loads(row["payload"]))
                if not isinstance(receipt, dict):
                    raise ValueError("Akashic 回执不是对象")
                box.ack(row["id"], receipt)
                sent += 1
            except RemoteError as error:
                box.fail(row["id"], f"RPC {error.code}: {error}", blocked=error.code == -32602)
            except (OSError, ConnectionError, TimeoutError) as error:
                box.fail(row["id"], f"{type(error).__name__}: {error}")
                break
            except ValueError as error:
                box.fail(row["id"], str(error), blocked=True)
    return {"committed": sent, **box.status()}


async def serve(box: Outbox, endpoint: str) -> None:
    while True:
        report = await drain(box, endpoint)
        if report["committed"] or report["errors"]:
            print(json.dumps(report, ensure_ascii=False), flush=True)
        await asyncio.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "drain", "run", "status", "retry"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--now", action="store_true", help="立即重试待发送事件，不改变已提交或受阻事件")
    parser.add_argument("--event-id")
    args = parser.parse_args()
    if args.command in ("drain", "run") and not args.endpoint:
        parser.error("转发需要明确的 --endpoint")
    if args.command == "retry" and not args.event_id:
        parser.error("retry 需要 --event-id")
    box = Outbox(args.state)
    try:
        if args.command == "collect":
            data = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("Codex Hook 事件超过 1 MiB")
            payload = json.loads(data.decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("Codex Hook 输入不是对象")
            box.collect(payload)
            # Stop 的输出必须是 JSON；不要求 Codex 延续、阻断或执行其他工具。
            print("{}")
        elif args.command == "status":
            print(json.dumps(box.status(), ensure_ascii=False))
        elif args.command == "retry":
            box.retry(args.event_id)
            print(json.dumps(box.status(), ensure_ascii=False))
        else:
            with (args.state / "relay.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print(json.dumps({"status": "already_running"}))
                    return 0
                if args.command == "run":
                    asyncio.run(serve(box, args.endpoint))
                else:
                    report = asyncio.run(drain(box, args.endpoint, now=args.now))
                    print(json.dumps(report, ensure_ascii=False))
                    return 1 if report["counts"].get("pending", 0) or report["counts"].get("blocked", 0) else 0
    finally:
        box.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"Codex 通知桥：{error}", file=sys.stderr)
        raise SystemExit(1)
