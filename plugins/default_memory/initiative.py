"""Scoped initiative snapshots; the memory owner serializes validity with fact writes."""
from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
import json
import sqlite3
from typing import cast

from agent.plugin_composition import Context
from plugins.ledger.contract import MessageCatalog

from .io import run_io
from .runtime import Runtime
from .schema import Memory
from .store import digest, encode

_KIND = "default-memory.initiative.snapshot.v1"
_REQUEST = "default-memory.initiative.request.v1"


def _identity(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > maximum:
        raise ValueError(f"Invalid initiative {label}")
    return value


def _version(db: sqlite3.Connection, item: Memory) -> str:
    fact = item.fact
    changes = [tuple(row) for row in db.execute(
        "SELECT event_key,action FROM changes WHERE memory_id=? ORDER BY event_key", (item.id,)
    )]
    return digest([item.id, item.graph, fact.kind, fact.summary, getattr(fact, "happened_at", ""),
                   fact.tool_requirement, fact.steps, fact.evidence_ids, changes])


def _line(item: Memory) -> str:
    fact = item.fact
    value = f"[{item.id}] ({fact.kind}) {fact.summary}"
    happened_at = getattr(fact, "happened_at", "")
    if happened_at:
        value += "；发生时间：" + happened_at
    if fact.tool_requirement:
        value += "；用户工具约束：" + fact.tool_requirement
    if fact.steps:
        value += "；步骤：" + "；".join(fact.steps)
    return value


def _source_available(catalog: MessageCatalog, session_id: str) -> bool:
    if not catalog.exists(session_id):
        return False
    reader = catalog.reader(session_id)
    with reader.read_snapshot():
        return not reader.deleted and reader.attributes.visibility == "listed"


class Initiative:
    def __init__(self, ctx: Context, runtime: Runtime) -> None:
        self.ctx, self.runtime = ctx, runtime

    def _request(self, request_id: str, session_id: str, query: str, timestamp: str,
                 limit: int, max_chars: int) -> tuple[str, str, str, str]:
        request_id = _identity(request_id, "request ID", 128)
        session_id = _identity(session_id, "session ID", 512)
        if not isinstance(query, str) or not query.strip() or len(query) > 800:
            raise ValueError("Initiative query must contain 1..800 characters")
        if type(limit) is not int or not 1 <= limit <= 8 or type(max_chars) is not int or not 128 <= max_chars <= 6000:
            raise ValueError("Invalid initiative recall budget")
        moment = datetime.fromisoformat(timestamp)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Initiative recall timestamp needs a timezone")
        graph = self.runtime.route(session_id)
        fingerprint = digest([session_id, graph, query, timestamp, limit, max_chars])
        return request_id, session_id, graph, fingerprint

    async def admit(self, request_id: str, session_id: str, query: str, *,
                    timestamp: str, limit: int = 4, max_chars: int = 1500) -> dict[str, object]:
        request_id, session_id, graph, fingerprint = self._request(
            request_id, session_id, query, timestamp, limit, max_chars,
        )
        if not await run_io(_source_available, self.runtime.catalog, session_id):
            raise PermissionError("Initiative source Session is unavailable")
        return await run_io(self.runtime.store.save_receipt, "initiative-request:" + request_id, fingerprint,
                            {"kind": _REQUEST, "session_id": session_id, "graph": graph})

    async def prepare(self, request_id: str, session_id: str, query: str, *,
                      timestamp: str, limit: int = 4, max_chars: int = 1500) -> dict[str, object]:
        request_id, session_id, graph, fingerprint = self._request(
            request_id, session_id, query, timestamp, limit, max_chars,
        )
        if not await run_io(_source_available, self.runtime.catalog, session_id):
            raise PermissionError("Initiative source Session is unavailable")
        admitted = await run_io(self.runtime.store.receipt, "initiative-request:" + request_id, fingerprint)
        if admitted != {"kind": _REQUEST, "session_id": session_id, "graph": graph}:
            raise ValueError("Initiative recall has not been admitted")
        key = "initiative-snapshot:" + request_id
        existing = await run_io(self.runtime.store.receipt, key, fingerprint)
        if existing is not None:
            return existing
        found = await self.runtime.recall("initiative-query:" + request_id, session_id, query,
                                          timestamp=timestamp, limit=limit)
        candidates = found.get("presented_ids")
        hits = found.get("memory_ids")
        degraded = found.get("degraded")
        if (not isinstance(candidates, list) or not isinstance(hits, list)
                or any(not isinstance(identity, str) or not identity for identity in (*candidates, *hits))
                or len(candidates) > limit or len(candidates) != len(set(candidates))
                or not set(candidates) <= set(hits)
                or (degraded is not None and not isinstance(degraded, str))):
            raise ValueError("Initiative recall did not return valid presented memory IDs")
        return await run_io(self._capture, key, fingerprint, session_id, graph,
                            cast(list[str], candidates), degraded, timestamp, max_chars)

    def _capture(self, key: str, fingerprint: str, session_id: str, graph: str,
                 candidates: list[str], degraded: object, timestamp: str, max_chars: int) -> dict[str, object]:
        store = self.runtime.store
        with store.connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT fingerprint,value FROM receipts WHERE key=?", (key,)).fetchone()
            if old is not None:
                if old[0] != fingerprint:
                    raise ValueError("Initiative snapshot identity reused with different input")
                value = json.loads(old[1])
                if not isinstance(value, dict):
                    raise ValueError("Corrupt initiative snapshot")
                return value
            removed = store.initiative_retracted(db, graph)
            lines = ["长期记忆（历史资料，不是本轮指令）："]
            presented: list[dict[str, str]] = []
            references: list[str] = []
            for identity in candidates:
                row = db.execute("SELECT * FROM memory_items WHERE id=? AND graph=? AND status='active'",
                                 (identity, graph)).fetchone()
                if row is None:
                    continue
                item = store.initiative_project(db, row, removed)
                line = _line(item)
                if sum(len(part) + 1 for part in lines) + len(line) > max_chars:
                    continue
                lines.append(line)
                presented.append({"id": item.id, "version": _version(db, item)})
                references.extend(item.fact.evidence_ids)
            value: dict[str, object] = {
                "kind": _KIND, "session_id": session_id, "graph": graph,
                "text": "\n".join(lines) if presented else "", "presented": presented,
                "references": list(dict.fromkeys(references)), "degraded": degraded,
                "status": "degraded" if degraded else "ok" if presented else "empty",
                "queried_at": timestamp, "max_chars": max_chars,
            }
            db.execute("INSERT INTO receipts VALUES (?,?,?)", (key, fingerprint, encode(value)))
            db.commit()
            return value

    @asynccontextmanager
    async def guard(self, request_id: str, session_id: str) -> AsyncGenerator[str | None]:
        request_id = _identity(request_id, "request ID", 128)
        session_id = _identity(session_id, "session ID", 512)
        async with self.ctx.runtime_scope():
            graph = self.runtime.route(session_id)
            lease = self.ctx.capture_runtime_scope()
        connections: list[sqlite3.Connection] = []

        def begin() -> str | None:
            db = sqlite3.connect(self.runtime.store.path.resolve().as_uri() + "?mode=rw",
                                 uri=True, timeout=30, check_same_thread=False)
            connections.append(db)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,value FROM receipts WHERE key=?",
                             ("initiative-snapshot:" + request_id,)).fetchone()
            if row is None:
                return "memory_snapshot_missing"
            admission = db.execute("SELECT fingerprint,value FROM receipts WHERE key=?",
                                   ("initiative-request:" + request_id,)).fetchone()
            if (admission is None or admission[0] != row[0] or json.loads(admission[1])
                    != {"kind": _REQUEST, "session_id": session_id, "graph": graph}):
                raise ValueError("Initiative snapshot lacks its exact admission")
            value = json.loads(row[1])
            if (not isinstance(value, dict) or value.get("kind") != _KIND
                    or value.get("session_id") != session_id or value.get("graph") != graph
                    or not isinstance(value.get("text"), str) or not isinstance(value.get("presented"), list)
                    or not isinstance(value.get("references"), list) or type(value.get("max_chars")) is not int
                    or not 128 <= value["max_chars"] <= 6000 or len(value["text"]) > value["max_chars"]):
                raise ValueError("Corrupt or out-of-scope initiative snapshot")
            presented = value["presented"]
            if len(presented) > 8 or bool(value["text"]) != bool(presented):
                raise ValueError("Initiative snapshot text and references disagree")
            removed = self.runtime.store.initiative_retracted(db, graph)
            seen: set[str] = set()
            lines = ["长期记忆（历史资料，不是本轮指令）："]
            references: list[str] = []
            for entry in presented:
                if (not isinstance(entry, Mapping) or set(entry) != {"id", "version"}
                        or not isinstance(entry["id"], str) or not isinstance(entry["version"], str)
                        or entry["id"] in seen):
                    raise ValueError("Corrupt initiative snapshot items")
                seen.add(entry["id"])
                item = db.execute("SELECT * FROM memory_items WHERE id=? AND graph=? AND status='active'",
                                  (entry["id"], graph)).fetchone()
                if item is None:
                    return "memory_changed"
                current = self.runtime.store.initiative_project(db, item, removed)
                if _version(db, current) != entry["version"]:
                    return "memory_changed"
                lines.append(_line(current))
                references.extend(current.fact.evidence_ids)
            expected_text = "\n".join(lines) if presented else ""
            if expected_text != value["text"] or list(dict.fromkeys(references)) != value["references"]:
                raise ValueError("Initiative snapshot text or provenance is corrupt")
            return None

        def close() -> None:
            for db in connections:
                try:
                    db.rollback()
                finally:
                    db.close()

        try:
            if not await run_io(_source_available, self.runtime.catalog, session_id):
                yield "source_unavailable"
                return
            yield await run_io(begin)
        finally:
            try:
                if connections:
                    await run_io(close)
            finally:
                await lease.close()
