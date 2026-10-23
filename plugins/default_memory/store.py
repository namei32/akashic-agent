"""Private SQLite facts, immutable drafts and transactional replay receipts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from collections.abc import Iterator, Mapping, Sequence
from .schema import Draft, Fact, Memory, Source
from .retrieval import cosine, normalized

_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE memory_items (
 id TEXT PRIMARY KEY, graph TEXT NOT NULL, kind TEXT NOT NULL,
 content_hash TEXT NOT NULL, fact TEXT NOT NULL, embedding TEXT NOT NULL,
 reinforcement INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('active','superseded')),
 source_key TEXT NOT NULL
);
CREATE INDEX memory_graph ON memory_items(graph,status);
CREATE TABLE learning (
 source_key TEXT PRIMARY KEY, digest TEXT NOT NULL, source TEXT NOT NULL,
 draft TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL,
 result TEXT
);
CREATE TABLE origins (
 source_key TEXT NOT NULL, item_index INTEGER NOT NULL, memory_id TEXT NOT NULL,
 evidence_ids TEXT NOT NULL, PRIMARY KEY(source_key,item_index)
);
CREATE TABLE changes (
 event_key TEXT PRIMARY KEY, memory_id TEXT NOT NULL, action TEXT NOT NULL,
 reason TEXT NOT NULL, evidence TEXT NOT NULL
);
CREATE TABLE receipts (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, value TEXT NOT NULL);
"""


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Store:
    """No handle to sessions.db, attachments, Markdown or Akasha graphs."""

    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(create=True) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not names:
                db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
                db.execute("INSERT INTO metadata VALUES ('schema','1')")
                db.commit()
            version = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
            if version is None or version[0] != "1":
                raise ValueError("Unsupported Default Memory schema")

    @contextmanager
    def connection(self, *, create: bool = False, write: bool = False) -> Iterator[sqlite3.Connection]:
        mode = "rwc" if create else "rw" if write else "ro"
        db = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if create:
                db.execute("PRAGMA journal_mode=WAL")
            if create or write:
                db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    def space(self, identity: str, dimension: int, *, initialize: bool = False) -> None:
        encoded = encode([identity, dimension])
        with self.connection(write=initialize) as db:
            if initialize:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('embedding_space',?)", (encoded,))
            row = db.execute("SELECT value FROM metadata WHERE key='embedding_space'").fetchone()
            if row is not None and row[0] != encoded:
                raise ValueError("Default Memory embedding space changed; explicit migration required")
            if initialize:
                db.commit()

    def dimension(self) -> int | None:
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='embedding_space'").fetchone()
            return None if row is None else int(json.loads(row[0])[1])

    @staticmethod
    def decode(row: sqlite3.Row) -> Memory:
        return Memory(row["id"], row["graph"], Fact.model_validate_json(row["fact"]),
                      tuple(json.loads(row["embedding"])), row["reinforcement"],
                      row["created_at"], row["updated_at"], row["status"], row["source_key"])

    def items(self, graph: str) -> tuple[Memory, ...]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM memory_items WHERE graph=? AND status='active' ORDER BY id", (graph,))
            found = tuple(self.decode(row) for row in rows)
        dimension = self.dimension()
        if found and dimension is None:
            raise ValueError("Stored memories have no embedding space")
        for item in found:
            normalized(item.vector, dimension or 0)
        return found

    def learned(self) -> dict[str, tuple[str, str]]:
        if not self.path.exists():
            return {}
        with self.connection() as db:
            return {r[0]: (r[1], r[2]) for r in db.execute("SELECT source_key,digest,state FROM learning")}

    def draft(self, source: Source) -> Draft | None:
        with self.connection() as db:
            row = db.execute("SELECT digest,draft FROM learning WHERE source_key=?", (source.key,)).fetchone()
        if row is None:
            return None
        if row[0] != source.digest:
            raise ValueError("Source changed after memory extraction")
        return Draft.model_validate_json(row[1])

    def stage(self, source: Source, draft: Draft, model: str) -> Draft:
        with self.connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,draft FROM learning WHERE source_key=?", (source.key,)).fetchone()
            if row is not None:
                if row[0] != source.digest:
                    raise ValueError("Source changed after memory extraction")
                return Draft.model_validate_json(row[1])
            db.execute("INSERT INTO learning VALUES (?,?,?,?,?,'draft',NULL)",
                       (source.key, source.digest, encode({**asdict(source), "text": ""}), draft.model_dump_json(), model))
            db.commit()
        return draft

    def apply(self, source: Source, draft: Draft, vectors: Sequence[Sequence[float]]) -> tuple[str, ...]:
        """Facts, provenance, retractions and completion commit together."""
        dimension = self.dimension()
        if dimension is None:
            raise ValueError("Embedding space must be fixed before learning")
        if len(vectors) != len(draft.facts):
            raise ValueError("Fact embedding count mismatch")
        vectors = tuple(normalized(v, dimension) for v in vectors)
        with self.connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT * FROM learning WHERE source_key=?", (source.key,)).fetchone()
            if saved is None or saved["digest"] != source.digest or saved["draft"] != draft.model_dump_json():
                raise ValueError("Learning must apply the durable draft from its exact source")
            if saved["state"] == "applied":
                return tuple(json.loads(saved["result"]))
            for index, action in enumerate(draft.retract):
                row = db.execute("SELECT * FROM memory_items WHERE id=? AND graph=?",
                                 (action.memory_id, source.graph)).fetchone()
                if row is None or row["source_key"] == source.key:
                    raise ValueError("Invalid retraction target")
                db.execute("UPDATE memory_items SET status='superseded' WHERE id=?", (action.memory_id,))
                db.execute("INSERT INTO changes VALUES (?,?,?,?,?)",
                           (source.key + ":retract:" + str(index), action.memory_id, "supersede",
                            action.reason, encode(action.evidence_ids)))
            result: list[str] = []
            for index, (fact, vector) in enumerate(zip(draft.facts, vectors, strict=True)):
                content_hash = digest([fact.kind, fact.summary])
                existing = db.execute(
                    "SELECT * FROM memory_items WHERE graph=? AND kind=? AND content_hash=? AND status='active' ORDER BY id LIMIT 1",
                    (source.graph, fact.kind, content_hash)).fetchone()
                if existing is None and fact.kind == "event":
                    for row in db.execute("SELECT * FROM memory_items WHERE graph=? AND kind='event' AND status='active' ORDER BY id", (source.graph,)):
                        old = self.decode(row)
                        age = (datetime.fromisoformat(source.timestamp) - datetime.fromisoformat(old.updated_at)).total_seconds()
                        if 0 <= age <= 7 * 86400 and cosine(vector, old.vector) >= .92:
                            existing = row
                            break
                if existing is not None:
                    identity = existing["id"]
                    db.execute("UPDATE memory_items SET reinforcement=reinforcement+1,updated_at=max(updated_at,?) WHERE id=?",
                               (source.timestamp, identity))
                else:
                    identity = "dm_" + digest([source.key, index])[:24]
                    db.execute("INSERT INTO memory_items VALUES (?,?,?,?,?,?,1,?,?,'active',?)",
                               (identity, source.graph, fact.kind, content_hash, fact.model_dump_json(),
                                encode(vector), source.timestamp, source.timestamp, source.key))
                db.execute("INSERT INTO origins VALUES (?,?,?,?)",
                           (source.key, index, identity, encode(fact.evidence_ids)))
                result.append(identity)
            db.execute("UPDATE learning SET state='applied',result=? WHERE source_key=?",
                       (encode(result), source.key))
            db.commit()
            return tuple(result)

    def receipt(self, key: str, fingerprint: str | None = None) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        with self.connection() as db:
            row = db.execute("SELECT fingerprint,value FROM receipts WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if fingerprint is not None and row[0] != fingerprint:
            raise ValueError("Receipt key reused for different arguments")
        value = json.loads(row[1])
        if not isinstance(value, dict):
            raise ValueError("Corrupt memory receipt")
        return value

    def save_receipt(self, key: str, fingerprint: str, value: Mapping[str, object]) -> dict[str, object]:
        with self.connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,value FROM receipts WHERE key=?", (key,)).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise ValueError("Receipt key reused for different arguments")
                return json.loads(row[1])
            db.execute("INSERT INTO receipts VALUES (?,?,?)", (key, fingerprint, encode(dict(value))))
            db.commit()
        return dict(value)

    def forget(self, key: str, fingerprint: str, graph: str, ids: Sequence[str], reason: str,
               evidence: Sequence[str]) -> dict[str, object]:
        with self.connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT fingerprint,value FROM receipts WHERE key=?", (key,)).fetchone()
            if saved:
                if saved[0] != fingerprint:
                    raise ValueError("Conflicting forget replay")
                return json.loads(saved[1])
            for index, identity in enumerate(ids):
                row = db.execute("SELECT graph FROM memory_items WHERE id=?", (identity,)).fetchone()
                if row is None or row[0] != graph:
                    raise ValueError("Memory target does not belong to this scope")
                db.execute("UPDATE memory_items SET status='superseded' WHERE id=?", (identity,))
                db.execute("INSERT INTO changes VALUES (?,?,?,?,?)",
                           (key + ":" + str(index), identity, "forget", reason, encode(evidence)))
            result: dict[str, object] = {"text": "已将指定记忆标为失效；原始对话仍保留。", "memory_ids": list(ids)}
            db.execute("INSERT INTO receipts VALUES (?,?,?)", (key, fingerprint, encode(result)))
            db.commit()
            return result

    def failures(self) -> dict[str, str]:
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='failures'").fetchone()
            return {} if row is None else json.loads(row[0])

    def note_failure(self, key: str, reason: str) -> None:
        with self.connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM metadata WHERE key='failures'").fetchone()
            failures = {} if row is None else json.loads(row[0])
            failures[key] = reason
            db.execute("INSERT INTO metadata VALUES ('failures',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encode(failures),))
            db.commit()

    def retry_epoch(self, *, advance: bool = False) -> int:
        with self.connection(write=advance) as db:
            if advance:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM metadata WHERE key='retry_epoch'").fetchone()
            epoch = 0 if row is None else int(row[0])
            if advance:
                epoch += 1
                db.execute("INSERT INTO metadata VALUES ('retry_epoch',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(epoch),))
                db.execute("INSERT INTO metadata VALUES ('failures','{}') ON CONFLICT(key) DO UPDATE SET value='{}'")
                db.commit()
            return epoch

    def status(self) -> dict[str, object]:
        if not self.path.exists():
            return {"initialized": False}
        with self.connection() as db:
            return {
                "initialized": True,
                "learning": dict(db.execute("SELECT state,count(*) FROM learning GROUP BY state")),
                "memories": dict(db.execute("SELECT status,count(*) FROM memory_items GROUP BY status")),
                "kinds": dict(db.execute("SELECT kind,count(*) FROM memory_items WHERE status='active' GROUP BY kind")),
                "receipts": db.execute("SELECT count(*) FROM receipts").fetchone()[0],
            }
