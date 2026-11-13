"""Explicit Windows Codex commands, separate from incoming Stop notifications."""
from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from io import BytesIO
import json
from pathlib import PureWindowsPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from PIL import Image

from agent.plugin_composition import Context
from core.common.file_io import run_file_io
from plugins.jobs.contract import JOB_NOTICES, JobOutcome, JobWatch
from plugins.ledger.contract import (ARTIFACT_READ, MESSAGE_CATALOG, OWNER_STATE, AttachmentKind, AttachmentRef,
                                    ContentPart, Input, OwnerTransaction, json_value)
from plugins.tools.contract import TOOLS, CallSource, Result
from .client import Client, CodexError

api_version = 3
name = "codex_control"
version = "1.0.0"
desc = "指挥 Windows 原生 Codex CLI：新建或选择任务、发送文字和图片、读结果、调整或中断"
inject = (TOOLS, OWNER_STATE, ARTIFACT_READ, MESSAGE_CATALOG)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = "ws://127.0.0.1:45873"


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["create", "list", "select", "read", "send", "steer", "interrupt", "approve"]
    cwd: str = Field(default="", max_length=4096)
    thread_id: str = Field(default="", max_length=256)
    text: str = Field(default="", max_length=100_000)
    image_ids: list[str] = Field(default_factory=list, max_length=4)
    turn_id: str = Field(default="", max_length=256)
    request_id: str = Field(default="", max_length=256)
    decision: Literal["accept", "decline", "cancel"] | None = None
    search: str = Field(default="", max_length=256)
    limit: int = Field(default=20, ge=1, le=30)


def result(value: object, *, error: bool = False) -> Result:
    return Result("error" if error else "success", (ContentPart("text", json.dumps(json_value(value), ensure_ascii=False, sort_keys=True)),))


def validate_image(data: bytes, media_type: str) -> None:
    try:
        with Image.open(BytesIO(data)) as image:
            types = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
            if types.get(image.format or "") != media_type or image.width * image.height > 50_000_000:
                raise ValueError("image type or dimensions rejected")
            image.verify()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as error:
        raise ValueError("图片损坏、格式不符或超过 5000 万像素；尚未发送给 Codex") from error


class Control:
    idempotent = False  # A transport disconnect must never replay a remote command.

    def __init__(self, ctx: Context, client: Client) -> None:
        self.ctx, self.client = ctx, client
        self.state = ctx.require(OWNER_STATE).open(ctx)

    async def prepare(self, arguments: Mapping[str, object], source: CallSource | None = None) -> Mapping[str, object] | str:
        try:
            args = Arguments.model_validate(json_value(arguments))
        except ValidationError as error:
            return str(error)
        if source is None or not source.messages:
            return "Codex 控制需要当前聊天中的明确工具调用"
        parent = source.messages[-1]
        target = await run_file_io(lambda: self.state.read("target:" + parent.session_id))
        if args.action == "create" and not PureWindowsPath(args.cwd).is_absolute():
            return "新建 CLI 任务需要用户指定的 Windows 绝对项目目录，例如 C:\\Users\\namei\\project"
        if args.action not in {"list", "create"} and not args.thread_id:
            args.thread_id = str(target.value["thread_id"]) if target is not None else ""
        if args.action not in {"list", "create"} and not args.thread_id:
            return "请先新建 CLI 任务，或列出并明确选择 Windows Codex 聊天"
        if args.action in {"send", "steer"} and not args.text.strip() and not args.image_ids:
            return "请提供要发给 Codex 的文字或图片"
        if args.action in {"steer", "interrupt"} and not args.turn_id:
            return "请先读取当前任务，再指定准确的 turn_id"
        if args.action == "approve" and (not args.request_id or args.decision is None):
            return "审批必须指定当前 request_id 和用户明确给出的决定"
        message_ids = tuple(message.message_id for message in source.messages
                            if isinstance(message.body, Input) and message.session_id == parent.session_id)
        reader = self.ctx.require(MESSAGE_CATALOG).reader(parent.session_id)
        attachments = await run_file_io(lambda: reader.attachments_for(message_ids)) if args.image_ids else ()
        refs = {ref.artifact_id: asdict(ref) for ref in attachments if ref.kind == AttachmentKind.IMAGE}
        if set(args.image_ids) - refs.keys():
            return "只能发送当前 Akashic 会话中实际附带的图片，请先上传图片"
        return {"args": args.model_dump(), "images": [refs[key] for key in args.image_ids],
                "parent_session_id": parent.session_id, "parent_source": parent.source, "parent_seq": parent.seq}

    async def query(self, key: str) -> Result | None:
        record = await run_file_io(lambda: self.state.read("command:" + key))
        if record is None:
            return None
        if record.value.get("status") == "finished":
            return result(record.value["result"], error=bool(record.value.get("error")))
        return result({"status": "unknown", "message": "原指令可能已送达；请读取目标聊天状态，不要自动重发"}, error=True)

    async def save(self, key: str, value: Mapping[str, object], *, new: bool = False) -> bool:
        def commit(tx: OwnerTransaction) -> bool:
            previous = tx.read(key)
            if new and previous is not None:
                return False
            tx.save(key, value, expected_version=previous.version if previous else None)
            return True
        return await self.state.transact_async(commit)

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        args = Arguments.model_validate(json_value(arguments["args"]))
        changing = args.action in {"create", "send", "steer", "interrupt", "approve"}
        if changing:
            if not await self.save("command:" + key, {"status": "started", "thread_id": args.thread_id,
                                                       "action": args.action}, new=True):
                return cast(Result, await self.query(key))
        try:
            value = await self.execute(args, arguments)
        except (CodexError, OSError, TimeoutError, ValueError) as error:
            status = "rejected" if isinstance(error, ValueError) else "unknown" if changing else "unavailable"
            value = {"status": status, "message": str(error)}
            if changing:
                await self.save("command:" + key, {"status": "finished", "result": value, "error": True})
            return result(value, error=True)
        if changing:
            await self.save("command:" + key, {"status": "finished", "result": value, "error": False})
        return result(value)

    async def execute(self, args: Arguments, prepared: Mapping[str, object]) -> dict[str, object]:
        await self.client.open()
        if self.client.server_platform != "windows":
            raise CodexError("连接目标不是 Windows 原生 Codex；请核对 CLI 启动入口")
        if args.action == "list":
            page = await self.client.call("thread/list", {"limit": args.limit, "archived": False,
                                                         "sourceKinds": ["cli", "appServer", "vscode", "exec"],
                                                         "sortKey": "updated_at",
                                                         **({"searchTerm": args.search} if args.search else {})})
            fields = ("id", "name", "preview", "cwd", "status", "updatedAt")
            return {"threads": [{k: row.get(k) for k in fields} for row in page.get("data", [])],
                    "next_cursor": page.get("nextCursor")}
        if args.action in {"select", "create"}:
            info = await self.client.call("thread/start", {"cwd": args.cwd}) if args.action == "create" else \
                await self.client.call("thread/read", {"threadId": args.thread_id, "includeTurns": False})
            identity = str(info["thread"]["id"])
            target_key = "target:" + str(prepared["parent_session_id"])
            def select(tx: OwnerTransaction) -> None:
                previous = tx.read(target_key)
                seq = int(str(prepared["parent_seq"]))
                if previous is not None and int(str(previous.value.get("call_seq", -1))) > seq:
                    raise ValueError("已有更新的 Codex 聊天选择，本次旧选择未提交")
                tx.save(target_key, {"thread_id": identity, "call_seq": seq},
                        expected_version=previous.version if previous else None)
            await self.state.transact_async(select)
            return {"status": "created" if args.action == "create" else "selected",
                    "thread_id": identity, "name": info["thread"].get("name"), "cwd": info["thread"].get("cwd")}
        if args.action == "read":
            return await self.client.read_thread(args.thread_id)
        if args.action == "approve":
            request = self.client.approvals.get(args.request_id, {})
            if request.get("params", {}).get("threadId") != args.thread_id:
                raise CodexError("审批不属于选中的 Codex 聊天或已失效")
            assert args.decision is not None
            return await self.client.approve(args.request_id, args.decision)
        if args.action == "interrupt":
            await self.client.call("turn/interrupt", {"threadId": args.thread_id, "turnId": args.turn_id})
            return {"status": "interrupt_requested", "thread_id": args.thread_id, "turn_id": args.turn_id}
        content: list[dict[str, object]] = [{"type": "text", "text": args.text}] if args.text else []
        total = 0
        for raw in cast(list[dict[str, object]], prepared["images"]):
            ref = AttachmentRef(artifact_id=str(raw["artifact_id"]), kind=AttachmentKind.IMAGE,
                filename=cast(str | None, raw["filename"]), media_type=cast(str | None, raw["media_type"]),
                size_bytes=cast(int, raw["size_bytes"]), sha256=str(raw["sha256"]))
            if ref.media_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ValueError("Codex 图片需要明确的 PNG、JPEG、WebP 或 GIF 类型")
            total += ref.size_bytes
            if total > 10 * 1024 * 1024:
                raise ValueError("本次图片总量超过 10 MiB，请减少图片")
            lease = await self.ctx.require(ARTIFACT_READ).acquire(ref)
            try:
                data = await lease.read_bytes(max_bytes=10 * 1024 * 1024)
            finally:
                await lease.aclose()
            await run_file_io(lambda: validate_image(data, ref.media_type or ""))
            encoded = base64.b64encode(data).decode("ascii")
            content.append({"type": "image", "url": f"data:{ref.media_type};base64,{encoded}"})
        if args.action == "steer":
            receipt = await self.client.call("turn/steer", {"threadId": args.thread_id,
                                  "expectedTurnId": args.turn_id, "input": content})
            return {"status": "steered", "thread_id": args.thread_id, "turn_id": receipt.get("turnId")}
        loaded = await self.client.call("thread/loaded/list", {})
        if args.thread_id not in loaded.get("data", []):
            await self.client.call("thread/resume", {"threadId": args.thread_id, "excludeTurns": True})
        receipt = await self.client.call("turn/start", {"threadId": args.thread_id, "input": content})
        turn_id = str(receipt["turn"]["id"])
        notification: dict[str, object] = {}
        try:
            watched = self.watch(prepared, args.thread_id, turn_id)
        except Exception as error:
            watched = False
            notification["notification_error"] = type(error).__name__
        return {"status": "accepted", "thread_id": args.thread_id, "turn_id": turn_id,
                "completion_notification": watched, **notification,
                "message": "指令已接纳，任务尚未完成。"}

    def watch(self, prepared: Mapping[str, object], thread_id: str, turn_id: str) -> bool:
        with self.ctx.borrow(JOB_NOTICES) as notices:
            if notices is None:
                return False
            async def settled() -> JobOutcome:
                try:
                    turn = await self.client.wait_turn(thread_id, turn_id)
                    return JobOutcome("completed" if turn.get("status") == "completed" else "failed",
                                      f"thread_id={thread_id}; turn_id={turn_id}; status={turn.get('status')}")
                except CodexError as error:
                    return JobOutcome("failed", str(error))
            session = str(prepared["parent_session_id"])
            notices.watch(JobWatch(session_id=session,
                source=notices.origin_source(session, str(prepared["parent_source"])),
                job_id="codex-" + turn_id, kind="codex", label="Windows Codex 任务状态",
                collect_hint=f"用 codex_control(action='read', thread_id='{thread_id}') 读取实际结果。",
                settled=settled))
            return True


async def apply(ctx: Context) -> None:
    config = Config.model_validate(ctx.config)
    client = Client(config.endpoint)
    await ctx.effect(lambda: client.aclose, label="codex-control.connection")
    control = Control(ctx, client)

    @asynccontextmanager
    async def open_tool(_state: Mapping[str, object]) -> AsyncIterator[Control]:
        yield control

    tools = ctx.require(TOOLS)
    await tools.declare_group(ctx, always_on=True, description=desc)
    await tools.register(ctx, name="codex_control", parameters=Arguments.model_json_schema(), open=open_tool,
        description=("通过 Windows 原生 Codex CLI 后端执行用户指令。不要绕过本工具另开 codex-delegate 任务。"
            "create 在用户指定的 Windows 绝对 cwd 新建并选中任务；list 搜索聊天，select 固定当前会话目标，read 读取状态和结果。"
            "独立 CLI 不能操纵另一个桌面后端的活动任务，不得同时续接桌面正在执行的聊天。"
            "只有用户明确要求时才 send 发新指令，或 steer 调整正在执行的准确 turn_id，interrupt 请求停止。"
            "图片用当前会话已上传的 image_ids，传输原始图片。approve 只用于用户明确同意、拒绝或取消的当前 request_id；"
            "不得自行审批、改变权限或改用其他聊天。accepted 只是接纳；结果未知时读取状态，禁止自动重发。"),
        idempotent=False, parallel=False)
