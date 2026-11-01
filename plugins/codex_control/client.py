"""One connection to the Windows native Codex CLI app-server."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake


class CodexError(RuntimeError):
    """A rejected request or an unavailable connection; never implies rollback."""


class Client:
    def __init__(self, url: str, *, timeout: float = 30) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} \
                or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Codex 控制地址必须是本机 loopback WebSocket 地址")
        self.url, self.timeout = url, timeout
        self.socket: ClientConnection | None = None
        self.reader: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.serial = 0
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.completed: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self.changed = asyncio.Condition()
        self.closed = False
        self.server_platform: str | None = None
        self.logger = logging.Logger("akashic.codex_control.transport", level=logging.WARNING)

    async def open(self) -> None:
        async with self.lock:
            if self.closed:
                raise CodexError("Codex 控制连接已关闭")
            if self.socket is not None:
                return
            try:
                self.socket = await connect(self.url, open_timeout=5, max_size=16 * 1024 * 1024,
                                            max_queue=16, proxy=None, logger=self.logger)
            except (OSError, TimeoutError, InvalidHandshake) as error:
                raise CodexError("无法连接 Windows Codex，请检查原生 CLI 控制后端是否正在运行") from error
            self.reader = asyncio.create_task(self._read(), name="codex-control-reader")
            try:
                identity = await self._request("initialize", {"clientInfo": {"name": "akashic_control", "version": "1.0.0"},
                                                              "capabilities": {}})
                self.server_platform = identity.get("platformFamily")
                await self.socket.send(json.dumps({"method": "initialized", "params": {}}))
            except BaseException:
                await self._disconnect()
                raise

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self.open()
        return await self._request(method, params)

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        socket = self.socket
        if socket is None:
            raise CodexError("Codex 控制连接已断开")
        if len(self.pending) >= 8:
            raise CodexError("Codex 控制请求过多，请等待已提交请求结算")
        self.serial += 1
        identity = self.serial
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            async with asyncio.timeout(self.timeout):
                await socket.send(json.dumps({"id": identity, "method": method, "params": params}, ensure_ascii=False))
                return await future
        except (ConnectionClosed, TimeoutError) as error:
            raise CodexError("等待 Codex 回执失败，发送结果未知；请读取目标聊天，不要自动重发") from error
        finally:
            self.pending.pop(identity, None)

    async def _read(self) -> None:
        socket = self.socket
        assert socket is not None
        try:
            async for raw in socket:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise CodexError("Codex 返回了无效协议消息")
                identity = message.get("id")
                if "method" in message:
                    await self._event(socket, message)
                elif isinstance(identity, int) and (future := self.pending.get(identity)) is not None and not future.done():
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(CodexError(str(error.get("message", "Codex 拒绝请求"))[:2000]))
                    elif isinstance(message.get("result"), dict):
                        future.set_result(message["result"])
                    else:
                        future.set_exception(CodexError("Codex 返回了无效请求结果"))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Prompt, tool arguments, and image data must not become diagnostics.
            failure = type(error).__name__
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CodexError(f"Codex 控制协议中断（{failure}），发送结果需重新核对"))
        finally:
            await socket.close()
            if self.socket is socket:
                self.socket = None
                self.approvals.clear()
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(CodexError("Codex 连接中断，已发送请求的结果未知；不要自动重发"))
                async with self.changed:
                    self.changed.notify_all()

    async def _event(self, socket: ClientConnection, message: dict[str, Any]) -> None:
        method, params = message["method"], message.get("params", {})
        if "id" in message:
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"} \
                    and len(self.approvals) < 16:
                self.approvals[str(message["id"])] = message
            else:
                await socket.send(json.dumps({"id": message["id"], "error": {
                    "code": -32601, "message": "Akashic cannot answer this interaction; use the desktop client"}}))
        elif method == "turn/completed":
            turn = params.get("turn", {})
            key = (params.get("threadId", ""), turn.get("id", ""))
            self.completed[key] = {"id": key[1], "status": turn.get("status"), "error": turn.get("error")}
            while len(self.completed) > 128:
                self.completed.popitem(last=False)
            async with self.changed:
                self.changed.notify_all()

    async def approve(self, identity: str, decision: str) -> dict[str, Any]:
        if decision not in {"accept", "decline", "cancel"}:
            raise ValueError("无效的审批决定")
        request = self.approvals.get(identity)
        if request is None or self.socket is None:
            raise CodexError("该审批已失效，请重新读取当前待审批项")
        await self.socket.send(json.dumps({"id": request["id"], "result": {"decision": decision}}))
        self.approvals.pop(identity, None)
        return {"request_id": identity, "status": "response_sent", "decision": decision}

    async def wait_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        async with self.changed:
            await self.changed.wait_for(lambda: (thread_id, turn_id) in self.completed or self.socket is None)
            if (thread_id, turn_id) not in self.completed:
                raise CodexError("连接已断开；远端任务可能仍在运行，请重新读取其状态")
            return dict(self.completed[(thread_id, turn_id)])

    async def read_thread(self, identity: str) -> dict[str, Any]:
        metadata = await self.call("thread/read", {"threadId": identity, "includeTurns": False})
        turns = await self.call("thread/turns/list", {"threadId": identity, "limit": 3,
                                                    "sortDirection": "desc", "itemsView": "full"})
        thread = metadata.get("thread", {})
        result: dict[str, Any] = {"thread": {k: thread.get(k) for k in ("id", "name", "cwd", "status")},
                                  "turns": [], "approvals": []}
        text_budget, diff_budget, command_budget = 24000, 12000, 4000
        for turn in turns.get("data", []):
            messages = [item.get("text", "") for item in turn.get("items", []) if item.get("type") == "agentMessage"]
            text = "\n".join(messages)
            all_commands = [item for item in turn.get("items", []) if item.get("type") == "commandExecution"]
            commands = [{k: item.get(k) for k in ("id", "command", "cwd", "status", "exitCode")}
                        for item in all_commands[-10:]]
            for command in commands:
                raw = str(command.get("command", ""))
                command["command"] = raw[:command_budget]
                command["truncated"] = len(raw) > command_budget
                command_budget -= len(command["command"])
            all_changes = [change for item in turn.get("items", []) if item.get("type") == "fileChange"
                           for change in item.get("changes", [])]
            changes = all_changes[-20:]
            diffs = []
            for item in changes:
                raw = str(item.get("diff", ""))
                excerpt = raw[:min(8000, diff_budget)]
                diff_budget -= len(excerpt)
                diffs.append({"path": item.get("path"), "kind": item.get("kind"), "diff": excerpt,
                              "truncated": len(raw) > len(excerpt)})
            excerpt = text[:min(16000, text_budget)]
            text_budget -= len(excerpt)
            result["turns"].append({"id": turn.get("id"), "status": turn.get("status"),
                                    "text": excerpt, "truncated": len(text) > len(excerpt), "error": turn.get("error"),
                                    "commands": commands, "changes": diffs,
                                    "commands_truncated": len(all_commands) > len(commands) or any(item["truncated"] for item in commands),
                                    "changes_truncated": len(all_changes) > len(changes) or any(item["truncated"] for item in diffs)})
        for key, request in self.approvals.items():
            params = request.get("params", {})
            if params.get("threadId") == identity:
                result["approvals"].append({"request_id": key, "method": request["method"],
                    **{field: params[field] for field in ("command", "cwd", "reason", "itemId", "grantRoot") if field in params}})
        return result

    async def _disconnect(self) -> None:
        socket, self.socket = self.socket, None
        if socket is not None:
            await socket.close()
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        self.approvals.clear()
        for future in self.pending.values():
            if not future.done():
                future.set_exception(CodexError("Codex 控制连接已关闭，远端任务状态需重新查询"))
        async with self.changed:
            self.changed.notify_all()

    async def aclose(self) -> None:
        self.closed = True
        async with self.lock:
            await self._disconnect()
