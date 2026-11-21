"""Read-only projection of committed child activity and parent delivery."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from agent.plugin_composition import CompositionError
from core.common.file_io import run_file_io
from plugins.jobs.contract import JobSnapshot, JobSnapshotStatus, SubagentProgress, SubagentReport
from plugins.ledger.contract import (
    CallRef, ContentPart, Control, MESSAGE_CATALOG, OWNER_STATE,
    Message, MessageCatalog, MessageReader, Output, OwnerStore, ToolCall, ToolResult,
)

from .inputs import DELIVERY, Deliveries
from .request import Request

if TYPE_CHECKING:
    from .runtime import Subagents


def _millis(message: Message) -> int:
    return int(message.recorded_at.timestamp() * 1000)


class SubagentPanel:
    def __init__(self, jobs: Subagents):
        self.jobs = jobs
        self.context = jobs.ctx
        self.listeners: dict[str, set[asyncio.Event]] = {}
        self.report_failures: set[str] = set()
        self.closed = False

    def notify(self, parent_id: str) -> None:
        for listener in self.listeners.get(parent_id, ()):
            listener.set()

    def close(self) -> None:
        self.closed = True
        for parent_id in self.listeners:
            self.notify(parent_id)

    async def follow_jobs(self, session_id: str) -> AsyncGenerator[tuple[JobSnapshot, ...], None]:
        changed = asyncio.Event()
        subscribers = self.listeners.setdefault(session_id, set())
        subscribers.add(changed)
        previous: tuple[JobSnapshot, ...] | None = None
        try:
            while not self.closed:
                changed.clear()
                entered = False
                try:
                    async with self.context.runtime_scope():
                        entered = True
                        catalog = self.context.require(MESSAGE_CATALOG)
                        store = self.context.require(OWNER_STATE).open(self.context)
                        delivery = self.context.require(DELIVERY).open(self.context)
                        report_failures = frozenset(self.report_failures)
                        rows = await run_file_io(lambda: self._snapshot(
                            session_id, catalog, store, delivery, report_failures))
                except CompositionError as error:
                    if entered or error.code != "OWNER_UNAVAILABLE":
                        raise
                    return
                if rows != previous:
                    previous = rows
                    yield rows
                await changed.wait()
        finally:
            subscribers.remove(changed)
            if not subscribers:
                del self.listeners[session_id]

    async def stop_job(self, session_id: str, job_id: str) -> bool:
        async with self.context.runtime_scope():
            return await self.jobs.cancel(job_id.removeprefix("subagent:"), parent_id=session_id)

    def _snapshot(
        self, session_id: str, catalog: MessageCatalog, store: OwnerStore, delivery: Deliveries,
        report_failures: frozenset[str],
    ) -> tuple[JobSnapshot, ...]:
        parent = catalog.reader(session_id)
        pending: list[tuple[Request, MessageReader, Message]] = []
        settled: list[tuple[Request, MessageReader, Message]] = []
        for _, record in store.list():
            request, reader, admitted = self.jobs.read_admitted(record, catalog)
            if request.parent_session_id != session_id:
                continue
            (settled if record.value["settled"] else pending).append((request, reader, admitted))
        recent = sorted(settled, key=lambda row: row[2].recorded_at, reverse=True)[:8]
        return tuple(self._row(request, reader, admitted, self._report(
            parent, request, delivery, request.session_id in report_failures,
        )) for request, reader, admitted in pending + recent)

    @staticmethod
    def _row(
        request: Request, reader: MessageReader, admitted: Message, report: tuple[SubagentReport, str | None],
    ) -> JobSnapshot:
        steps = 0
        status: JobSnapshotStatus = "running"
        finished_at = None
        detail = ""
        tools = {binding: name for name, binding in request.tools.items()}
        calls: dict[CallRef, str] = {}
        actions: list[tuple[int, str]] = []

        for message in reader.snapshot(after_seq=admitted.seq):
            if message.source != "subagent":
                continue
            body = message.body
            at = _millis(message)
            if isinstance(body, Output):
                if any(isinstance(part, ContentPart) and part.kind == "model.facts" for part in body.parts):
                    steps += 1
                for index, part in enumerate(body.parts):
                    if isinstance(part, ToolCall) and part.binding_id in tools:
                        name = tools[part.binding_id]
                        calls[CallRef(message.message_id, index)] = name
                        actions.append((at, f"调用 {name}"))
                if body.finish != "continue":
                    status, finished_at = "completed", at
                    actions.append((at, "生成结果" if body.finish == "complete" else "结束执行"))
            elif isinstance(body, ToolResult) and (name := calls.get(body.call_ref)):
                label = "完成" if body.outcome == "success" else "未成功"
                actions.append((at, f"{name} · {label}"))
            elif isinstance(body, Control) and body.action in {"pause", "failure"}:
                status = "killed" if body.action == "pause" else "failed"
                detail = "用户停止了子任务" if status == "killed" else (body.reason or "子任务执行失败")[:500]
                finished_at = at

        return JobSnapshot(
            request.session_id, "子 agent", request.label, status, detail, _millis(admitted),
            finished_at, status == "running",
            SubagentProgress(steps, tuple(reversed(actions[-3:])), *report),
        )

    @staticmethod
    def _report(
        parent: MessageReader, request: Request, delivery: Deliveries, failed: bool,
    ) -> tuple[SubagentReport, str | None]:
        if request.sink is None:
            return "inline", None
        seq = parent.latest_finished_output_seq(request.session_id, after_seq=-1, through_seq=parent.head())
        pending: SubagentReport = "failed" if failed else "waiting"
        if seq is None:
            return pending, None
        reply, = parent.read(after_seq=seq - 1, through_seq=seq, source=request.session_id, limit=1)
        selection = delivery.selection(reply.message_id)
        sink = request.sink["name"]
        if selection is None:
            return pending, None
        if sink not in selection.sinks:
            return "failed", None
        receipt = delivery.receipt(reply.message_id, sink)
        if receipt is None:
            return pending, None
        return ("delivered", reply.message_id) if receipt.status == "delivered" else ("failed", None)
