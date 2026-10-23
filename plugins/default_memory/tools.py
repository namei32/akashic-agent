"""Explicit recall and user-directed edits with durable tool receipts."""
from __future__ import annotations

import json
from dataclasses import asdict
from collections.abc import Mapping
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from plugins.ledger.contract import ContentPart, Input, json_value
from plugins.tools.contract import CallSource, Result
from .io import run_io
from .learning import check, text
from .schema import Draft, Fact, Source
from .store import digest
from .runtime import Runtime


class RecallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=16000)
    limit: int = Field(default=8, ge=1, le=40)


class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=2000)
    kind: Literal["profile", "preference", "procedure", "event"] = "preference"


class ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=500)


class MemoryTool:
    idempotent = True

    def __init__(self, runtime: Runtime, action: str):
        self.runtime = runtime
        self.action = action

    async def prepare(self, arguments: Mapping[str, object],
                      source: CallSource | None = None) -> Mapping[str, object] | str:
        try:
            schema = {"recall": RecallArgs, "remember": RememberArgs, "forget": ForgetArgs}[self.action]
            parsed = schema.model_validate(json_value(arguments))
            if source is None or not source.messages:
                return "记忆工具需要真实对话出处"
            first = source.messages[0]
            graph = self.runtime.route(first.session_id, write=self.action != "recall")
            users = [m for m in source.messages if isinstance(m.body, Input)
                     and self.runtime.content.is_user_input(m)]
            if self.action != "recall" and not users:
                return "记忆修改需要真实用户输入"
            newest = users[-1] if users else first
            if self.action != "recall" and self.runtime.content.legacy_post_commit_effect(newest) == "suppress":
                return "该输入不允许沉淀为长期记忆"
            return {"action": self.action, "arguments": parsed.model_dump(mode="json"),
                    "session_id": first.session_id, "graph": graph,
                    "user_id": newest.message_id, "user_text": text(newest),
                    "timestamp": newest.recorded_at.isoformat(),
                    "effect_key": source.effect_key}
        except (ValidationError, PermissionError) as error:
            return str(error)

    def result(self, record: Mapping[str, object]) -> Result:
        value = str(record.get("text", ""))
        if self.action == "recall" and not value:
            value = "没有达到召回阈值的长期记忆。"
        return Result("success", (ContentPart("text", value),))

    async def query(self, key: str) -> Result | None:
        record = await run_io(self.runtime.store.receipt, "tool:" + key)
        return None if record is None else self.result(record)

    async def invoke(self, key: str, arguments: Mapping[str, object]) -> Result:
        raw = json_value(arguments)
        if not isinstance(raw, dict):
            raise ValueError("Prepared memory arguments must be an object")
        data: dict[str, object] = raw
        if data.get("action") != self.action or data.get("effect_key") != key:
            raise ValueError("Memory tool arguments do not belong to this call")
        fingerprint = digest(data)
        identity = "tool:" + key
        previous = await run_io(self.runtime.store.receipt, identity, fingerprint) if self.action != "recall" else None
        if previous is not None:
            return self.result(previous)
        session = str(data["session_id"])
        graph = self.runtime.route(session, write=self.action != "recall")
        if graph != data["graph"]:
            raise ValueError("Memory scope changed after preparation")
        if self.action == "recall":
            args = RecallArgs.model_validate(data["arguments"])
            record = await self.runtime.recall(identity, session, args.query, limit=args.limit)
            return self.result(record)
        async with self.runtime.learning_lock:
            if self.action == "forget":
                args = ForgetArgs.model_validate(data["arguments"])
                record = await run_io(self.runtime.store.forget, identity, fingerprint, graph,
                                                 args.memory_ids, args.reason, [str(data["user_id"])])
                return self.result(record)
            args = RememberArgs.model_validate(data["arguments"])
            fact = Fact(kind=args.kind, summary=args.summary, evidence_ids=(str(data["user_id"]),))
            source = Source(identity, graph, session, str(data["user_id"]), str(data["timestamp"]),
                            (str(data["user_id"]),), (str(data["user_id"]),), fingerprint, "")
            draft = check(Draft(facts=(fact,)), source, ())
            descriptor = self.runtime.embeddings.describe()
            await run_io(self.runtime.store.space, descriptor.identity, descriptor.dimensions, initialize=True)
            draft = await run_io(self.runtime.store.stage, source, draft, "explicit-user-tool")
            vectors = await self.runtime.vectors([value.summary for value in draft.facts])
            ids = await run_io(self.runtime.store.apply, source, draft, vectors)
            record = await run_io(self.runtime.store.save_receipt, identity, fingerprint,
                                             {"text": "已保存长期记忆：" + "、".join(ids), "memory_ids": list(ids)})
            return self.result(record)
