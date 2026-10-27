"""真实 HTTP、SourceSession、ReAct、文件工具与 SQLite 的预算恢复工作流。"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sqlite3
import sys
import tempfile


async def run(folder: Path, limit: int) -> dict:
    import httpx
    from agent.plugin_composition.tasks import Tasks
    from core.net.http import HttpClient
    from plugins.content.plugin import _decode_text, check_text
    from plugins.context.api import check_summary
    from plugins.context.plugin import ContextBuilder
    from plugins.host_execution.contract import FileError
    from plugins.host_execution.files import Files
    from plugins.ledger.contract import ContentPart, Control, Input, Output, ToolResult
    from plugins.ledger.log import MessageLog, SessionAttributes
    from plugins.models.content import render_content
    from plugins.models.contract import BoundModelDescriptor, CapabilitySources, ModelCapabilities, ModelError
    from plugins.models.projection import MessageProjection, check_facts, check_tool_rejection
    from plugins.models.state import _BoundChat
    from plugins.models.store import ModelsStore
    from plugins.openai_compatible.driver import _BoundChat as PhysicalChat, _ConnectionConfig, _ModelConfig
    from plugins.react.plugin import StepLimit, react
    from plugins.sources.session import SourceSession
    from plugins.standard_tools.filesystem import WriteFileTool
    from plugins.tools.execution import MessageReply, Result, ToolExecution
    from plugins.tools.menu import ToolCallDecode
    from scripts.check_model_retry import Credential

    requests, effects, handlers = [], [], set()
    entered, released = asyncio.Event(), asyncio.Event()
    holds, final, reject_next = 2, False, False

    async def serve(reader, writer):
        nonlocal holds, reject_next
        handler = asyncio.current_task()
        handlers.add(handler)
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode()
            size = int(next(line.split(":", 1)[1] for line in header.split("\r\n")
                            if line.lower().startswith("content-length:")))
            requests.append(json.loads(await reader.readexactly(size)))
            if reject_next:
                reject_next = False
                data = json.dumps({"error": {"message": "scenario request rejected", "type": "invalid_request_error"}}).encode()
                writer.write(f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
                await writer.drain()
                return
            if len(effects) == 1 and holds:
                holds -= 1
                entered.set()
                await released.wait()
                return
            number = len(effects) + 1
            call = {"index": 0, "id": f"write-{number}", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({
                    "path": str(folder / f"receipt-{number}.txt"), "content": f"effect-{number}\n"})}}
            message = {"role": "assistant", "content": "recovered" if final else None}
            if not final:
                message["tool_calls"] = [{key: value for key, value in call.items() if key != "index"}]
            data = json.dumps({"id": f"response-{len(requests)}", "choices": [{
                "index": 0, "message": message, "finish_reason": "stop" if final else "tool_calls"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}}).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(handler)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    http = HttpClient(lambda: httpx.AsyncClient(base_url=endpoint, trust_env=False, timeout=5))
    descriptor = BoundModelDescriptor("model", "scenario", 0, "model", "connection", "openai-compatible", "1",
        "scenario", "scenario", "agent", None, ModelCapabilities(context_window=200000), CapabilitySources(), "scenario")
    physical = PhysicalChat(_ConnectionConfig(endpoint, 1, 5, 0, False),
                            Credential(), descriptor, _ModelConfig(None, 16), http)
    operation = WriteFileTool(Files(), folder)
    store = ModelsStore(folder / "models.db", folder / "backups")
    store.initialize()
    model = _BoundChat(descriptor, physical, store, max_attempts=1)
    log, tasks = MessageLog(folder / "sessions.db"), Tasks()
    mutations = []
    def observe(action, table, *_):
        if table == "messages" and action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            mutations.append(action)
        return sqlite3.SQLITE_OK
    log._connection.set_authorizer(observe)
    log.ensure_session("scenario", SessionAttributes())
    log.ensure_session("other", SessionAttributes())
    log.save_binding("file", {"target": str(folder)})

    def writer(session, body, ref=None, source="conversation"):
        return log.writer(session, author="user" if body is Input else "scenario", source=source, body_types=(body,),
            content={"text": check_text, "model.facts": check_facts,
                     "model.tool_rejection": check_tool_rejection, "context.summary": check_summary},
            call_ref=ref, check_call=lambda call: None)

    def source(session="scenario"):
        return SourceSession(reader=log.reader(session), inputs=writer(session, Input),
                             controls=writer(session, Control), tasks=tasks)

    class FileTarget:
        idempotent = False
        async def prepare(self, arguments, source=None):
            if Path(arguments["path"]).parent != folder:
                raise ValueError("文件目标必须在隔离 workspace 内")
            return arguments
        async def query(self, key):
            return None
        async def invoke(self, key, arguments):
            effects.append(key)
            actual = await operation.execute(**dict(arguments))
            return Result("error" if isinstance(actual, FileError) else "success",
                          (ContentPart("text", actual if isinstance(actual, str) else actual.text),))

    @asynccontextmanager
    async def open_tool(binding):
        assert binding == "file"
        yield FileTarget()

    async def authorize(binding, arguments):
        assert binding == "file" and Path(arguments["path"]).parent == folder
        return {"decision": "allowed"}

    class Content:
        def check_metadata(self, metadata):
            assert not metadata
        async def decode(self, text, references):
            return await _decode_text(text, (), references)

    async def program(task, reader, lane):
        execution = ToolExecution(log.owner("tools"), tasks, open_tool, authorize, task_key="tools")
        class Menu:
            schemas = ({"type": "function", "function": {"name": "write_file", "parameters": operation.parameters}},)
            def decode(self, call):
                assert call.name == "write_file"
                return ToolCallDecode("file", call.arguments)
            def name(self, binding):
                return "write_file"
            def parallel(self, binding):
                return False
            def check_start(self, transaction):
                if not task.active:
                    raise asyncio.CancelledError
            async def execute(self, ref, *, commit_after=None):
                reply = MessageReply("result:" + ref.message_id, ref, reader,
                                     writer(reader.session_id, ToolResult, ref), self.check_start)
                try:
                    return await execution.execute_call(reply, commit_after=commit_after)
                finally:
                    reply.writer.expire()
        async def materials(snapshot):
            return {}
        projection = MessageProjection(model, source=lane, check_summary=check_summary,
            render_content=lambda part: render_content(part, artifacts={}),
            tool_name=lambda binding: "write_file", read_call=store.read_call)
        output = writer(reader.session_id, Output)
        task.on_close(output.expire)
        return await react(reader, output, model=model, context=ContextBuilder(), projection=projection,
            materials=materials, content=Content(), tools=Menu(), max_output_tokens=128,
            max_steps=limit, state=log.owner("react"), check_start=Menu().check_start)

    async def budget_failure(task):
        try:
            await asyncio.wait_for(task.join(), 30)
        except RuntimeError as error:
            assert ModelError.matches(error, StepLimit), str(error)
        else:
            raise AssertionError("运行未在配置的预算边界暂停")
        assert log.reader("scenario").snapshot()[-1].body.action == "failure"

    def rows():
        with sqlite3.connect(f"file:{folder / 'sessions.db'}?mode=ro", uri=True) as connection:
            return tuple(connection.execute("SELECT * FROM messages ORDER BY session_key,seq"))

    try:
        conversation = source()
        await conversation.accept("first", Input((ContentPart("text", "write numbered receipts"),)))
        active = await conversation.start(program)
        await asyncio.wait_for(entered.wait(), 10)
        await conversation.pause("pause")
        try:
            await active.join()
        except asyncio.CancelledError:
            pass
        released.set()
        entered, released = asyncio.Event(), asyncio.Event()
        await conversation.resume("resume-pause", "first")
        active = await conversation.start(program)
        await asyncio.wait_for(entered.wait(), 10)
        await conversation.accept("steering", Input((ContentPart("text", "continue the same work"),)))
        try:
            await active.join()
        except asyncio.CancelledError:
            pass
        released.set()
        reject_next = True
        try:
            await asyncio.wait_for((await conversation.start(program)).join(), 10)
        except RuntimeError as error:
            assert not ModelError.matches(error, StepLimit)
        else:
            raise AssertionError("HTTP 400 没有形成来源失败")
        await conversation.accept("after-provider-error", Input((ContentPart("text", "continue after the provider error"),)))
        await budget_failure(await conversation.start(program))
        assert len(effects) == limit, "暂停、恢复、插话或未耗尽时的模型失败重置了预算"

        # 其他来源的输入及显式 resume 都不续额；重新打开真实账本后仍保持耗尽。
        await writer("scenario", Input, source="other-source").append_async(
            "other-source", Input((ContentPart("text", "unrelated source"),)))
        requests_before = len(requests)
        await conversation.resume("resume-exhausted", "after-provider-error")
        await budget_failure(await conversation.start(program))
        assert len(requests) == requests_before
        head = log.reader("scenario").head(source="conversation")
        await conversation.control("legacy-failure", Control("failure", head,
            f"本来源未完成工作已达到 {limit} 个模型输出"), expected_head=head, handle=None)
        protected = rows()
        await tasks.close()
        log.close()
        store.close()
        log, tasks = MessageLog(folder / "sessions.db"), Tasks()
        log._connection.set_authorizer(observe)
        store = ModelsStore(folder / "models.db", folder / "backups")
        store.initialize()
        model = _BoundChat(descriptor, physical, store, max_attempts=1)
        conversation = source()
        assert rows() == protected and not await conversation.needs_reply(log.reader("scenario"), "conversation")

        await conversation.accept("after-exhaustion", Input((ContentPart("text", "continue with a fresh budget"),)))
        await budget_failure(await conversation.start(program))
        assert len(effects) == limit * 2, "耗尽后的新输入没有取得完整的新预算"
        final = True
        await conversation.accept("question", Input((ContentPart("text", "what is the result?"),)))
        await asyncio.wait_for((await conversation.start(program)).join(), 30)
        output = log.reader("scenario").snapshot()[-1].body
        assert isinstance(output, Output) and output.finish == "complete"
        assert len(effects) == limit * 2 and len(set(effects)) == len(effects), "旧文件工具被重复执行"
        assert all((folder / f"receipt-{i}.txt").read_text() == f"effect-{i}\n" for i in range(1, len(effects) + 1))
        assert rows()[:len(protected)] == protected, "既有消息的身份、内容或顺序被改写"
        assert sum(row["role"] == "tool" for row in requests[-1]["messages"]) == limit * 2
        await source("other").accept("independent", Input((ContentPart("text", "independent question"),)))
        await asyncio.wait_for((await source("other").start(program)).join(), 30)
        assert mutations and set(mutations) == {sqlite3.SQLITE_INSERT}, "出现旧消息 UPDATE/DELETE 尝试"
        return {"max_steps": limit, "file_effects": len(effects), "http_requests": len(requests),
                "fresh_budget_after_failure": True, "pause_resume_steering_restart_preserve_budget": True,
                "ordinary_failure_preserves_budget": True, "message_update_delete_attempts": 0,
                "legacy_failure_recovers_without_rewrite": True,
                "original_rows_preserved": len(protected), "old_tools_not_replayed": True,
                "other_session_completed": True}
    finally:
        released.set()
        server.close()
        await server.wait_closed()
        await tasks.close()
        await asyncio.gather(*handlers, return_exceptions=True)
        log.close()
        store.close()
        await operation.aclose()
        await http.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--steps", type=int, default=2)
    args = parser.parse_args()
    if args.steps < 2:
        parser.error("--steps 必须至少为 2，供中途暂停和插话场景使用")
    sys.path.insert(0, str(args.source_root.resolve()))
    with tempfile.TemporaryDirectory(prefix="react-budget-workflow-") as temporary:
        print(json.dumps(asyncio.run(run(Path(temporary), args.steps)), ensure_ascii=False, indent=2))
