"""A real BM25+dense raw-history control with explicit shared context budgets."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
import re
from pathlib import Path
import time

import numpy as np
import tiktoken

from .data import read, write


class Budget:
    def __init__(self, protocol: dict):
        self.max_chars = protocol["max_chars"]
        self.max_tokens = protocol["max_tokens"]
        self.encoding = tiktoken.get_encoding(protocol["tokenizer"])

    def tokens(self, text: str) -> int:
        return len(self.encoding.encode(text, disallowed_special=()))

    def pack(self, entries: list[dict]) -> dict:
        lines, selected = [], []
        for item in entries:
            candidate = "\n\n".join([*lines, item["text"]])
            if len(candidate) > self.max_chars or self.tokens(candidate) > self.max_tokens:
                continue
            lines.append(item["text"])
            selected.append(item)
        text = "\n\n".join(lines)
        return {"context": text, "context_chars": len(text), "context_tokens": self.tokens(text),
                "selected": selected, "omitted_entries": len(entries) - len(selected)}


def chunks(history: dict, protocol: dict) -> list[dict]:
    result = []
    width, overlap = protocol["chunk_chars"], protocol["chunk_overlap"]
    for session in history["sessions"]:
        for message in session["messages"]:
            value = message["content"]
            for offset in range(0, len(value), width - overlap):
                content = value[offset:offset + width]
                result.append({
                    "id": f'{message["id"]}:c{offset}', "message_id": message["id"],
                    "session_id": session["original_id"], "role": message["role"],
                    "text": f'[{message["id"]}] {session["timestamp"]} {message["role"]}:\n{content}',
                })
                if offset + width >= len(value):
                    break
    return result


def terms(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def bm25(documents: list[dict], query: str, k1: float, b: float) -> list[float]:
    counters = [Counter(terms(d["text"])) for d in documents]
    sizes = [sum(c.values()) for c in counters]
    average = sum(sizes) / max(1, len(sizes))
    scores = [0.] * len(documents)
    for term in set(terms(query)):
        df = sum(term in c for c in counters)
        if not df:
            continue
        idf = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
        for i, counts in enumerate(counters):
            tf = counts[term]
            if tf:
                scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * sizes[i] / max(1., average)))
    return scores


async def raw_hybrid(host, history: dict, query: str, budget: Budget) -> dict:
    settings = host.protocol["retrieval"]
    docs = chunks(history, settings)
    descriptor = host.embedding.describe()
    digest = hashlib.sha256(json.dumps([docs, query, descriptor.identity], ensure_ascii=False).encode()).hexdigest()
    cache = host.folder / "raw-vectors.npz"
    metadata = host.folder / "raw-vectors.json"
    begin = time.perf_counter()
    if cache.exists():
        if read(metadata)["digest"] != digest:
            raise ValueError("Raw retrieval vector cache changed")
        with np.load(cache, allow_pickle=False) as saved:
            matrix, vector = saved["matrix"], saved["query"]
    else:
        all_texts = [query, *[d["text"] for d in docs]]
        vector_rows = []
        parts = host.folder / "raw-vector-parts"
        parts.mkdir(exist_ok=True)
        async with host.embedding.bind(model_id=descriptor.model_id) as model:
            for start in range(0, len(all_texts), 10):
                batch = all_texts[start:start + 10]
                part = parts / f"{start:06d}.json"
                if part.exists():
                    saved = read(part)
                    if saved["digest"] != digest or len(saved["vectors"]) != len(batch):
                        raise ValueError("Embedding checkpoint mismatch")
                else:
                    called = time.perf_counter()
                    response = await model.embed(batch)
                    if len(response.vectors) != len(batch):
                        raise ValueError("Embedding batch length mismatch")
                    saved = {"digest": digest, "vectors": response.vectors,
                             "milliseconds": (time.perf_counter() - called) * 1000,
                             "usage": None if response.usage is None else asdict(response.usage)}
                    write(part, saved)
                vector_rows.extend(saved["vectors"])
                write(host.folder / "progress.json", {"phase": "raw_embeddings",
                      "embedded": len(vector_rows), "total": len(all_texts)})
        values = np.asarray(vector_rows, dtype=np.float64)
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        if not np.isfinite(values).all() or (norms <= 0).any():
            raise ValueError("Invalid baseline embeddings")
        values /= norms
        vector, matrix = values[0], values[1:]
        np.savez_compressed(cache, matrix=matrix, query=vector)
        write(metadata, {"digest": digest, "model_id": descriptor.model_id, "dimensions": descriptor.dimensions})
    dense = matrix @ vector
    lexical = bm25(docs, query, settings["bm25_k1"], settings["bm25_b"])
    fused = Counter()
    candidate_limit = max(16, host.protocol["budget"]["top_k"] * 2)
    for lane in (dense, lexical):
        order = sorted(range(len(docs)), key=lambda i: (-lane[i], docs[i]["id"]))
        for rank, index in enumerate(order[:candidate_limit], 1):
            if lane is lexical and lexical[index] <= 0:
                continue
            fused[index] += 1 / (settings["rrf_k"] + rank)
    order = sorted(fused, key=lambda i: (-fused[i], docs[i]["id"]))[:host.protocol["budget"]["top_k"]]
    packed = budget.pack([docs[i] for i in order])
    packed["candidates"] = [{"id": docs[i]["id"], "dense": float(dense[i]), "bm25": lexical[i],
                             "rrf": fused[i]} for i in order]
    packed["milliseconds"] = (time.perf_counter() - begin) * 1000
    packed["chunks"] = len(docs)
    return packed


def oracle(history: dict, qa: dict, protocol: dict, budget: Budget) -> dict:
    evidence = set(qa["evidence_message_ids"])
    answer_sessions = set(qa["answer_session_ids"])
    docs = chunks(history, protocol)
    wanted = [d for d in docs if d["message_id"] in evidence] if evidence else [
        d for d in docs if d["session_id"] in answer_sessions]
    result = budget.pack(wanted)
    result["all_evidence_fits"] = result["omitted_entries"] == 0
    result["selection"] = "annotated evidence turns" if evidence else "annotated evidence sessions"
    return result


def evidence_diagnostic(result: dict, qa: dict) -> dict:
    gold = set(qa["evidence_message_ids"])
    ids = {item.get("message_id") for item in result.get("selected", [])}
    for item in result.get("selected", []):
        ids.update(item.get("evidence_ids", []))
    return {"gold_messages": len(gold), "presented_gold_messages": len(gold & ids),
            "source_recall": len(gold & ids) / len(gold) if gold else None,
            "note": "Source-ID overlap is a provenance diagnostic, not semantic fact coverage."}
