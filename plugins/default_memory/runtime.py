"""Memory operations use public capabilities and a private fact database."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from dataclasses import replace
from plugins.akasha.contract import MemoryScopes
from plugins.content.contract import Content
from plugins.ledger.contract import Input, Message, MessageCatalog
from plugins.models.contract import ChatModels, Embeddings, ModelError
from plugins.turn_projection.contract import TurnProjection
from .learning import collect, extract, check, text
from .retrieval import normalized, render, retrieve
from .schema import Draft, Source
from .store import Store, digest


class Runtime:
    def __init__(self, store: Store, catalog: MessageCatalog, projection: TurnProjection,
                 content: Content, scopes: MemoryScopes, models: ChatModels, embeddings: Embeddings,
                 *, sources: tuple[str, ...], model_id: str | None = None, max_chars: int = 6000):
        self.store = store
        self.catalog = catalog
        self.projection = projection
        self.content = content
        self.scopes = scopes
        self.models = models
        self.embeddings = embeddings
        self.sources = sources
        self.model_id = model_id
        self.max_chars = max_chars
        self.learning_lock = asyncio.Lock()
        self.recall_lock = asyncio.Lock()
        self.errors: dict[str, str] = {}
        self.last_total = 0
        self.paused = False

    async def vectors(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        descriptor = self.embeddings.describe()
        await asyncio.to_thread(self.store.space, descriptor.identity, descriptor.dimensions, initialize=True)
        async with self.embeddings.bind(model_id=descriptor.model_id) as model:
            if model.descriptor.identity != descriptor.identity:
                raise ValueError("Embedding provider changed while opening")
            result = await model.embed(texts)
        if len(result.vectors) != len(texts):
            raise ValueError("Embedding result count mismatch")
        return tuple(normalized(v, descriptor.dimensions) for v in result.vectors)

    async def sources_snapshot(self) -> tuple[Source, ...]:
        heads = self.catalog.snapshot_heads()
        # Scope calls own a protected provider entry and stay on the host loop.
        routes = {session: self.scopes.route(session) for session in heads}
        class Routes:
            def route(self, session_id: str):
                return routes[session_id]
        return await asyncio.to_thread(collect, self.catalog, self.projection, self.content,
                                       Routes(), self.sources, heads)

    async def learn(self, source: Source) -> tuple[str, ...]:
        descriptor = self.embeddings.describe()
        await asyncio.to_thread(self.store.space, descriptor.identity, descriptor.dimensions, initialize=True)
        draft = await asyncio.to_thread(self.store.draft, source)
        if draft is None:
            items = await asyncio.to_thread(self.store.items, source.graph)
            # Only scoped memories enter the extraction prompt.
            cues = "\n".join(row["text"] for row in json.loads(source.text) if row["role"] == "user")
            cue_vector = (await self.vectors([cues]))[0] if items else None
            candidates = tuple(hit.memory for hit in retrieve(items, cues, cue_vector, datetime.fromisoformat(source.timestamp), limit=12))
            epoch = await asyncio.to_thread(self.store.retry_epoch)
            draft, model = await extract(source, candidates, self.models, model_id=self.model_id, retry_epoch=epoch)
            draft = await asyncio.to_thread(self.store.stage, source, draft, model)
        vectors = await self.vectors([fact.summary for fact in draft.facts]) if draft.facts else ()
        return await asyncio.to_thread(self.store.apply, source, draft, vectors)

    async def consume(self, *, limit: int | None = None, retry: bool = False) -> dict[str, object]:
        async with self.learning_lock:
            if retry:
                await asyncio.to_thread(self.store.retry_epoch, advance=True)
            self.errors = await asyncio.to_thread(self.store.failures)
            if self.paused:
                return await self.status()
            sources = await self.sources_snapshot()
            self.last_total = len(sources)
            learned = await asyncio.to_thread(self.store.learned)
            applied = 0
            for source in sources:
                if self.paused or (limit is not None and applied >= limit):
                    break
                previous = learned.get(source.key)
                if previous is not None and previous[0] != source.digest:
                    raise ValueError("Learned source prefix changed; operator verification required")
                if previous is not None and previous[1] == "applied":
                    continue
                if source.key in self.errors:
                    continue
                try:
                    await self.learn(source)
                    applied += 1
                except asyncio.CancelledError:
                    raise
                except (ValueError, RuntimeError, TimeoutError) as error:
                    # Expose failures and retain the draft/cursor. Automatic events
                    # do not repeatedly charge for the same invalid model response.
                    self.errors[source.key] = str(error)
                    await asyncio.to_thread(self.store.note_failure, source.key, str(error))
            return await self.status()

    async def audit(self) -> dict[str, object]:
        sources = await self.sources_snapshot()
        self.last_total = len(sources)
        by_session: dict[str, int] = {}
        for source in sources:
            by_session[source.session_id] = by_session.get(source.session_id, 0) + 1
        return {"eligible_turns": len(sources), "sessions": by_session,
                "source_digests": {source.key: source.digest for source in sources}}

    async def status(self) -> dict[str, object]:
        value = await asyncio.to_thread(self.store.status)
        return {**value, "eligible_turns": self.last_total, "paused": self.paused,
                "failures": dict(self.errors)}

    def route(self, session_id: str, *, write: bool = False) -> str:
        route = self.scopes.route(session_id)
        selected = route.write if write else route.read
        if selected is None:
            raise PermissionError("该会话的记忆学习或召回已关闭")
        return selected

    async def recall(self, key: str, session_id: str, query: str, *, limit: int = 8,
                     timestamp: str | None = None) -> dict[str, object]:
        graph = self.route(session_id)
        fingerprint = digest([session_id, graph, query, limit])
        async with self.recall_lock:
            old = await asyncio.to_thread(self.store.receipt, key, fingerprint)
            if old is not None:
                return old
            if not self.store.path.exists():
                return {"text": "", "references": [], "status": "Default Memory 尚未初始化"}
            vector = None
            degraded = None
            try:
                vector = (await self.vectors([query]))[0]
            except (RuntimeError, TimeoutError) as error:
                if not ModelError.matches(error):
                    raise
                degraded = str(error)
            items = await asyncio.to_thread(self.store.items, graph)
            now = datetime.fromisoformat(timestamp) if timestamp else datetime.now(UTC)
            hits = retrieve(items, query, vector, now, limit=limit)
            rendered, presented = render(hits, max_chars=self.max_chars)
            result: dict[str, object] = {
                "text": rendered,
                "references": list(dict.fromkeys(ref for hit in presented for ref in hit.memory.fact.evidence_ids)),
                "memory_ids": [hit.memory.id for hit in hits],
                "scores": [{"id": hit.memory.id, "score": hit.score, "rrf": hit.fusion, "lane": hit.lane} for hit in hits],
                "degraded": degraded,
            }
            return await asyncio.to_thread(self.store.save_receipt, key, fingerprint, result)

    async def prepare(self, snapshot: tuple[Message, ...], source: str) -> Mapping[str, object]:
        turns = self.projection.project(snapshot, source)
        if not turns or turns[-1].status != "open":
            return {}
        by_id = {message.message_id: message for message in snapshot}
        inputs = [by_id[key] for key in turns[-1].message_ids
                  if isinstance(by_id[key].body, Input) and self.content.is_user_input(by_id[key])]
        if not inputs or not text(inputs[-1]).strip():
            return {}
        newest = inputs[-1]
        if self.scopes.route(newest.session_id).read is None:
            return {}
        result = await self.recall("context:" + newest.message_id, newest.session_id, text(newest),
                                   timestamp=newest.recorded_at.isoformat())
        reminders = []
        if result["text"]:
            reminders.append({"name": "memory:" + newest.message_id, "text": result["text"], "priority": 400})
        if result.get("degraded"):
            reminders.append({"name": "status", "text": "Default Memory 向量通道不可用，本次使用关键词召回。", "priority": 300})
        references = result["references"]
        if not isinstance(references, (list, tuple)) or any(not isinstance(key, str) for key in references):
            raise ValueError("Corrupt recall references")
        return {"reminders": tuple(reminders),
                "references": tuple({"ref": key, "resolved_ref": key} for key in references)}
