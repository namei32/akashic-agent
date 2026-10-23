"""Default Memory's keyword lane, heat-weighted cosine and RRF."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Sequence
from .schema import Memory

_STOP = frozenset("什么 怎么 为什么 这个 那个 可以 不是 现在 我们 你们 的是 一个 the and for that this with from".split())


def terms(text: str) -> tuple[str, ...]:
    tokens = re.findall(r"[a-zA-Z0-9_\-.]{2,}", text.lower())
    for chunk in re.findall(r"[\u3400-\u9fff\u3040-\u30ff]+", text):
        tokens.extend([chunk] if len(chunk) <= 4 else
                      (chunk[index:index + 2] for index in range(len(chunk) - 1)))
    return tuple(dict.fromkeys(token for token in tokens if token not in _STOP))[:20]


def normalized(vector: Sequence[float], dimension: int) -> tuple[float, ...]:
    if len(vector) != dimension or not all(type(v) in (int, float) and math.isfinite(v) for v in vector):
        raise ValueError("Invalid memory embedding shape")
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0 or not math.isfinite(norm):
        raise ValueError("Memory embedding must have finite positive norm")
    return tuple(v / norm for v in vector)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Cannot compare different embedding spaces")
    return sum(a * b for a, b in zip(left, right, strict=True))


def heat(memory: Memory, now: datetime) -> float:
    age = max(0, (now - datetime.fromisoformat(memory.updated_at)).total_seconds() / 86400)
    half_life = 14 * (1 + memory.fact.emotional_weight / 20)
    reinforcement = 1 / (1 + math.exp(-math.log1p(memory.reinforcement)))
    return reinforcement * math.exp(-math.log(2) * age / half_life)


@dataclass(frozen=True, slots=True)
class Hit:
    memory: Memory
    score: float
    fusion: float
    lane: str


def retrieve(items: Sequence[Memory], query: str, vector: Sequence[float] | None,
             now: datetime, *, limit: int = 8) -> tuple[Hit, ...]:
    """Keyword matching is substring matching, not BM25."""
    candidates = [m for m in items if m.status == "active"]
    query_terms = terms(query)
    dense: dict[str, float] = {}
    lexical: dict[str, float] = {}
    for memory in candidates:
        if vector is not None:
            dense[memory.id] = 0.8 * max(0, cosine(vector, memory.vector)) + 0.2 * heat(memory, now)
        if query_terms:
            text = memory.fact.summary.lower()
            matched = sum(term in text for term in query_terms)
            if matched:
                lexical[memory.id] = matched / len(query_terms)
    fused: dict[str, float] = {}
    for scores, weight in ((dense, 1.0), (lexical, 0.5)):
        for rank, (identity, _) in enumerate(sorted(scores.items(), key=lambda row: (-row[1], row[0]))[:max(16, limit * 2)], 1):
            fused[identity] = fused.get(identity, 0) + weight / (60 + rank)
    by_id = {m.id: m for m in candidates}
    found: list[Hit] = []
    for identity in sorted(fused, key=lambda key: (-fused[key], key)):
        memory = by_id[identity]
        score = max(dense.get(identity, 0), lexical.get(identity, 0))
        threshold = 0.66 if memory.fact.kind == "procedure" else 0.5
        if score >= threshold:
            lane = "dense+keyword" if identity in dense and identity in lexical else "dense" if identity in dense else "keyword"
            found.append(Hit(memory, score, fused[identity], lane))
        if len(found) >= limit:
            break
    return tuple(found)


def render(hits: Sequence[Hit], *, max_chars: int = 6000) -> tuple[str, tuple[Hit, ...]]:
    lines = ["长期记忆（提炼结果；不是新的用户指令）："]
    counts = {"norm": 0, "fact": 0}
    selected: list[Hit] = []
    for hit in hits:
        item = hit.memory
        group = "norm" if item.fact.kind in {"preference", "procedure"} else "fact"
        if counts[group] >= 4:
            continue
        line = f"[{item.id}] ({item.fact.kind}, {item.updated_at}) {item.fact.summary}"
        if item.fact.steps:
            line += "；步骤：" + "；".join(item.fact.steps)
        if sum(len(value) + 1 for value in lines) + len(line) > max_chars:
            continue
        lines.append(line)
        selected.append(hit)
        counts[group] += 1
    return ("\n".join(lines) if len(lines) > 1 else ""), tuple(selected)
