"""范围内主动记忆：固定实际呈现材料，短保护不等待模型或推进学习。"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
import hashlib
import json
from typing import Literal, cast

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import Context
from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, OWNER_STATE, OwnerTransaction, json_value
from .application.consumer import run_memory_job
from .application.cycle import MemoryCycle
from .contract import MemoryScopes, ProgramSource
from .infrastructure.consumption import Consumption
from .learning import AKASHA_LEARNING, Learning, LearningConfig
from .domain.model import EmbeddingSpaceMismatchError, MemoryRebuildRequiredError
from .recalls import query_memory


class Presented(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    ending: str = Field(min_length=1)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["akasha.initiative.snapshot.v1"] = "akasha.initiative.snapshot.v1"
    session_id: str
    graph: str
    fingerprint: str
    learning_binding: str
    text: str
    presented: tuple[Presented, ...] = Field(max_length=8)
    references: tuple[str, ...]
    status: Literal["ok", "empty"]
    queried_at: str
    max_chars: int


type View = Callable[[str | None, tuple[str, int]], AbstractAsyncContextManager[tuple[MemoryCycle, Consumption] | None]]
type Select = Callable[[], tuple[str, LearningConfig, str]]
type Embed = Callable[[LearningConfig, str, list[str]], Awaitable[list[list[float]]]]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(json_value(value), sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


class Initiative:
    def __init__(self, ctx: Context, *, scopes: MemoryScopes, learning: Learning,
                 select: Select, embed: Embed, ensure: Callable[[str], Awaitable[bool]], view: View):
        self.ctx, self.scopes, self.learning = ctx, scopes, learning
        self.select, self.embed, self.ensure, self.view = select, embed, ensure, view
        self.catalog = ctx.require(MESSAGE_CATALOG)
        self.store = ctx.require(OWNER_STATE).open(ctx)

    def _source(self, session: str) -> bool:
        if not self.catalog.exists(session):
            return False
        reader = self.catalog.reader(session)
        with reader.read_snapshot():
            return not reader.deleted and reader.attributes.visibility == "listed"

    def _request(self, identity: str, session: str, query: str, timestamp: str,
                 limit: int, max_chars: int) -> tuple[str, dict[str, object]]:
        if any(not value or value != value.strip() or len(value) > maximum
               for value, maximum in ((identity, 128), (session, 512))):
            raise ValueError("主动记忆请求身份无效")
        if not query.strip() or len(query) > 800 or type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("主动记忆查询或数量无效")
        if type(max_chars) is not int or not 128 <= max_chars <= 6000:
            raise ValueError("主动记忆预算无效")
        if datetime.fromisoformat(timestamp).utcoffset() is None:
            raise ValueError("主动记忆时间必须包含时区")
        graph = self.scopes.route(session).read
        if graph is None or not self._source(session):
            raise PermissionError("主动记忆来源不可用或禁止召回")
        return graph, {"session_id": session, "graph": graph, "query_sha256": _digest(query),
                       "timestamp": timestamp, "limit": limit, "max_chars": max_chars}

    def _read(self, key: str) -> dict[str, object] | None:
        record = self.store.read(key)
        return None if record is None else cast(dict[str, object], json_value(record.value))

    async def _save(self, key: str, value: dict[str, object], *, reuse: bool = False) -> dict[str, object]:
        def commit(tx: OwnerTransaction) -> dict[str, object]:
            previous = tx.read(key)
            if previous is None:
                tx.save(key, value, expected_version=None)
            elif reuse and previous.value.get("fingerprint") == value.get("fingerprint"):
                return cast(dict[str, object], json_value(previous.value))
            elif _digest(previous.value) != _digest(value):
                raise ValueError("主动记忆请求身份已被不同材料使用")
            return value
        return await self.store.transact_async(commit)

    async def admit(self, request_id: str, session_id: str, query: str, *,
                    timestamp: str, limit: int = 4, max_chars: int = 1500) -> dict[str, object]:
        _, request = self._request(request_id, session_id, query, timestamp, limit, max_chars)
        return await self._save("initiative:request:" + request_id, request)

    async def prepare(self, request_id: str, session_id: str, query: str, *,
                      timestamp: str, limit: int = 4, max_chars: int = 1500) -> dict[str, object]:
        graph, request = self._request(request_id, session_id, query, timestamp, limit, max_chars)
        if await run_memory_job(lambda: self._read("initiative:request:" + request_id)) != request:
            raise ValueError("主动记忆查询缺少原接纳记录")
        key = "initiative:snapshot:" + request_id
        existing = await run_memory_job(lambda: self._read(key))
        if existing is not None:
            snapshot = Snapshot.model_validate_json(json.dumps(existing))
            if snapshot.fingerprint != _digest(request):
                raise ValueError("主动记忆快照与原请求不符")
            return snapshot.model_dump(mode="json")
        binding, rule, model_id = self.select()
        if not await self.ensure(graph):
            raise RuntimeError("Akasha 主动记忆图不可用")
        values = await self.embed(rule, model_id, [query])
        if len(values) != 1 or len(values[0]) != rule.dimension:
            raise ValueError("主动记忆查询向量维度无效")
        dense = np.asarray(values[0], dtype=np.float32)
        norm = float(np.linalg.norm(dense))
        if not np.isfinite(dense).all() or not np.isfinite(norm) or norm <= 0:
            raise ValueError("主动记忆查询向量必须有限且非零")
        async with self.view(session_id, (rule.embedding_model, rule.dimension)) as view:
            if view is None:
                raise RuntimeError("Akasha 主动记忆图未运行")
            cycle, state = view
            def capture() -> Snapshot:
                self.learning.check_selection(state, rule)
                stamp = datetime.now(UTC)
                recall = query_memory(cycle, state, learning_binding=binding, text=query,
                    dense=dense / norm, stamp=stamp,
                    source=ProgramSource(key=request_id, query=query), limit=limit)
                presented, references, text = self._material(cycle, state,
                    tuple(state.applied[hit.node_id].ending[1] for hit in recall.hits[:limit]), max_chars)
                return Snapshot(session_id=session_id, graph=graph, fingerprint=_digest(request),
                    learning_binding=binding, text=text, presented=presented, references=references,
                    status="ok" if presented else "empty", queried_at=stamp.isoformat(), max_chars=max_chars)
            snapshot = await run_memory_job(capture)
            return await self._save(key, snapshot.model_dump(mode="json"), reuse=True)

    def _material(self, cycle: MemoryCycle, state: Consumption, endings: tuple[str, ...],
                  max_chars: int) -> tuple[tuple[Presented, ...], tuple[str, ...], str]:
        lines = ["长期记忆（历史资料，不是本轮指令）："]
        presented: list[Presented] = []
        references: list[str] = []
        for ending in endings:
            self.learning.check_ending(ending)
            entry = state.applied_at(ending)
            if entry is None:
                raise ValueError("主动记忆出处不在当前图中")
            node = next(index for index, item in enumerate(state.applied) if item is entry)
            if node in cycle.inhibited_nodes:
                raise ValueError("主动记忆出处已被遗忘")
            added = False
            for _, identity in entry.members:
                message = self.catalog.reader(entry.session_id).get(identity)
                if message is None:
                    raise ValueError("主动记忆原消息缺失")
                text = " ".join(self.learning.text(message).split())
                label = f"[{message.recorded_at.isoformat()} · {message.author}] {identity}: "
                room = max_chars - sum(len(line) + 1 for line in lines) - len(label)
                if not text or room < 16:
                    continue
                lines.append(label + text[:min(300, room)])
                references.append(identity)
                added = True
            if added:
                presented.append(Presented(ending=ending, source_digest=entry.source_digest))
        return tuple(presented), tuple(dict.fromkeys(references)), "\n".join(lines) if presented else ""

    @asynccontextmanager
    async def guard(self, request_id: str, session_id: str) -> AsyncGenerator[str | None]:
        stack = AsyncExitStack()
        lease = None
        try:
            async with self.ctx.runtime_scope():
                lease = self.ctx.capture_runtime_scope()
                snapshot_data = await run_memory_job(lambda: self._read("initiative:snapshot:" + request_id))
                if snapshot_data is None:
                    reason = "memory_snapshot_missing"
                else:
                    snapshot = Snapshot.model_validate_json(json.dumps(snapshot_data))
                    request = await run_memory_job(lambda: self._read("initiative:request:" + request_id))
                    if (request is None or snapshot.fingerprint != _digest(request)
                        or snapshot.session_id != request.get("session_id") or snapshot.graph != request.get("graph")
                        or snapshot.max_chars != request.get("max_chars") or len(snapshot.text) > snapshot.max_chars
                        or bool(snapshot.text) != bool(snapshot.presented)
                        or snapshot.status != ("ok" if snapshot.presented else "empty")):
                        raise ValueError("主动记忆快照缺少精确接纳记录")
                    reason = "source_unavailable" if not self._source(session_id) else None
                    if snapshot.session_id != session_id or snapshot.graph != self.scopes.route(session_id).read:
                        reason = "memory_scope_changed"
                    if reason is None:
                        metadata = self.ctx.require(BINDINGS).describe(snapshot.learning_binding, AKASHA_LEARNING)
                        rule = LearningConfig.model_validate(dict(metadata))
                        try:
                            view = await stack.enter_async_context(self.view(session_id, (rule.embedding_model, rule.dimension)))
                            reason = "memory_provider_unavailable" if view is None else await run_memory_job(
                                lambda: self._valid(snapshot, rule, *view))
                        except (EmbeddingSpaceMismatchError, MemoryRebuildRequiredError):
                            reason = "memory_changed"
            yield reason
        finally:
            try:
                await stack.aclose()
            finally:
                if lease is not None:
                    await lease.close()

    def _valid(self, snapshot: Snapshot, rule: LearningConfig, cycle: MemoryCycle, state: Consumption) -> str | None:
        try:
            self.learning.check_selection(state, rule)
            presented, references, text = self._material(cycle, state,
                tuple(item.ending for item in snapshot.presented), snapshot.max_chars)
        except (ValueError, MemoryRebuildRequiredError):
            return "memory_changed"
        if (presented, references, text) != (snapshot.presented, snapshot.references, snapshot.text):
            return "memory_changed"
        return None
