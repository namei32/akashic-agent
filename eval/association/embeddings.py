"""Content-addressed, model-bound vector cache; no answers or graph state."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3

import numpy as np

from .schema import canonical, digest


class VectorCache:
    def __init__(self, path: Path, *, model: str, dimensions: int, space: str | None = None,
                 writable: bool = False):
        self.path, self.model, self.dimensions = path, model, dimensions
        if writable:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.is_file():
            raise ValueError("Missing vector cache; run embedding preparation first")
        self.db = sqlite3.connect(path if writable else path.resolve().as_uri() + "?mode=ro", uri=not writable)
        try:
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables and writable and space:
                with self.db:
                    self.db.execute("CREATE TABLE association_meta (value TEXT NOT NULL)")
                    self.db.execute("CREATE TABLE vectors (key TEXT PRIMARY KEY, text_hash TEXT NOT NULL, data BLOB NOT NULL)")
                    self.db.execute("INSERT INTO association_meta VALUES (?)",
                                    (canonical({"version": 1, "model": model, "dimensions": dimensions, "space": space}),))
            elif "association_meta" not in tables:
                raise ValueError("Refusing a database that is not an association vector cache")
            metadata = json.loads(self.db.execute("SELECT value FROM association_meta").fetchone()[0])
            if (metadata["version"], metadata["model"], metadata["dimensions"]) != (1, model, dimensions):
                raise ValueError("Embedding model or dimensions mismatch")
            if space is not None and metadata["space"] != space:
                raise ValueError("Embedding space identity changed")
            self.space = metadata["space"]
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def get(self, text: str) -> np.ndarray:
        key = digest(text)
        row = self.db.execute("SELECT text_hash,data FROM vectors WHERE key=?", (key,)).fetchone()
        if row is None:
            raise KeyError(f"Missing embedding {key}; no live fallback during retrieval")
        if row[0] != key:
            raise ValueError("Embedding content identity mismatch")
        vector = np.frombuffer(row[1], dtype="<f4").copy()
        if len(vector) != self.dimensions or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
            raise ValueError("Invalid cached vector")
        if not np.isclose(np.linalg.norm(vector), 1, atol=1e-5):
            raise ValueError("Cache vector is not unit-normalized")
        return vector

    def put(self, text: str, values):
        vector = np.asarray(values, dtype=np.float32)
        if vector.ndim != 1 or len(vector) != self.dimensions or not np.isfinite(vector).all():
            raise ValueError("Embedding shape or values invalid")
        norm = np.linalg.norm(vector)
        if norm == 0:
            raise ValueError("Zero embedding")
        vector = (vector / norm).astype("<f4")
        key = digest(text)
        old = self.db.execute("SELECT data FROM vectors WHERE key=?", (key,)).fetchone()
        if old is not None and old[0] != vector.tobytes():
            raise ValueError("Refusing to overwrite a frozen vector")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO vectors VALUES (?,?,?)", (key, key, vector.tobytes()))

    def fingerprint(self) -> str:
        # Logical hash is stable across SQLite page layouts.
        result = hashlib.sha256(canonical({"model": self.model, "dimensions": self.dimensions,
                                          "space": self.space}).encode())
        for key, content_hash, blob in self.db.execute("SELECT key,text_hash,data FROM vectors ORDER BY key"):
            result.update(canonical([key, content_hash]).encode())
            result.update(blob)
        return result.hexdigest()


async def populate(suite, cache: VectorCache, client, *, batch_size: int = 32, on_receipt=None):
    if batch_size < 1:
        raise ValueError("Invalid embedding batch size")
    corpora = {c.id: c for c in suite.corpora}
    eligible = [q for q in suite.cases if not q.data_issues]
    texts = dict.fromkeys(text for q in eligible for u in corpora[q.corpus_id].visible(q.cutoff).units
                         for text in (u.input_text, u.output_text) if text)
    texts.update(dict.fromkeys(q.question for q in eligible))
    missing = []
    for text in texts:
        try:
            cache.get(text)
        except KeyError:
            missing.append(text)
    receipts = []
    for start in range(0, len(missing), batch_size):
        batch = missing[start:start + batch_size]
        result = await client.embed(batch)
        if (result["model"], result["space"], result["dimensions"]) != (cache.model, cache.space, cache.dimensions):
            raise ValueError("Provider embedding identity mismatch")
        if len(result["vectors"]) != len(batch):
            raise ValueError("Provider returned a different batch length")
        for text, vector in zip(batch, result["vectors"], strict=True):
            cache.put(text, vector)
        receipt = {"text_hashes": [digest(text) for text in batch], "usage": result.get("usage")}
        receipts.append(receipt)
        if on_receipt is not None:
            on_receipt(receipt)
    return receipts
