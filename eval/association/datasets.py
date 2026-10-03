"""Source-faithful adapters. Gold labels are never part of Corpus."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import random
import re
from typing import Literal, cast

from .schema import Case, Corpus, Evidence, EvidenceReference, Suite, Unit, file_digest

LOCOMO_CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def timestamp(value: str) -> datetime:
    text = re.sub(r"\s*\([^)]*\)", "", value.strip()).replace("/", "-")
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        result = None
        for fmt in ("%I:%M %p on %d %B, %Y", "%I:%M %p on %d %B %Y", "%d %B %Y"):
            try:
                result = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass
        if result is None:
            raise ValueError(f"Unsupported explicit dataset timestamp: {value!r}")
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def longmemeval(path: Path, split: str, *, time_policy: str = "strict") -> tuple[list[Corpus], list[Case]]:
    if split not in {"dev", "holdout"}:
        raise ValueError("Unknown dataset split")
    if time_policy not in {"strict", "provided-history"}:
        raise ValueError("Unknown history visibility policy")
    corpora, cases = [], []
    for row in json.loads(path.read_text(encoding="utf-8")):
        identity = f"longmemeval:{row['question_id']}"
        sessions, dates, source_ids = row["haystack_sessions"], row["haystack_dates"], row["haystack_session_ids"]
        if not (len(sessions) == len(dates) == len(source_ids)):
            raise ValueError("LongMemEval session/date/identity lengths differ")
        units, gold = [], []
        for i, raw in enumerate(sessions):
            at = timestamp(dates[i])
            # Official histories can repeat a source session ID. Preserve each occurrence,
            # while keeping the original label identity for session-level evaluation.
            session = f"{identity}:session:{i}"
            source_session = f"{identity}:source:{source_ids[i]}"
            messages = []
            for j, item in enumerate(raw):
                if item["role"] not in {"user", "assistant"} or not isinstance(item["content"], str):
                    raise ValueError("Unsupported LongMemEval history message")
                message = Evidence(id=f"{session}:{j}", session_id=session, source_session_id=source_session, speaker=item["role"],
                                   role=item["role"], at=at, text=item["content"])
                messages.append(message)
                if item.get("has_answer"):
                    gold.append(message.id)
            # Group consecutive user inputs with their completed assistant response.
            pending = []
            for message in messages:
                pending.append(message)
                if message.role == "assistant":
                    units.append(Unit(id=pending[0].id, messages=tuple(pending)))
                    pending = []
            if pending:
                units.append(Unit(id=pending[0].id, messages=tuple(pending)))
        units.sort(key=lambda unit: max(m.at for m in unit.messages))
        corpus = Corpus(id=identity, adapter="lme-source-turns-v1", units=tuple(units))
        absent = str(row["question_id"]).endswith("_abs")
        level = "message" if gold else "session"
        if not gold:
            gold = [f"{identity}:source:{s}" for s in row.get("answer_session_ids", [])]
        if absent or not gold:
            gold, level = [], "none"
        question_at = timestamp(row["question_date"])
        cutoff = question_at
        if time_policy == "provided-history" and units:
            cutoff = max(question_at, max(m.at for u in units for m in u.messages) + timedelta(microseconds=1))
        messages = [m for u in corpus.visible(cutoff).units for m in u.messages]
        known = {m.id if level == "message" else (m.source_session_id or m.session_id) for m in messages}
        all_known = {m.id if level == "message" else (m.source_session_id or m.session_id)
                     for u in corpus.units for m in u.messages}
        if not set(gold) <= all_known:
            raise ValueError(f"Gold labels cannot be mapped to source history: {identity}")
        issues = ("gold_after_question_time",) if not set(gold) <= known else ()
        corpora.append(corpus)
        cases.append(Case(id=identity, dataset="longmemeval", corpus_id=identity, cluster_id=identity,
                          split=cast(Literal["dev", "holdout"], split), category="abstention" if absent else row["question_type"],
                          question=row["question"], cutoff=cutoff, question_at=question_at,
                          answer=str(row["answer"]), unanswerable=absent,
                          gold_ids=tuple(dict.fromkeys(gold)), gold_level=level, data_issues=issues))
    return corpora, cases


def locomo_evidence_references(identity: str, labels: list[str], known_ids: set[str]) -> tuple[EvidenceReference, ...]:
    """Normalize numeric zero-padding only when it resolves to one real source message."""
    def numeric_key(value: str):
        match = re.fullmatch(r"D([0-9]+):([0-9]+)", value)
        return None if match is None else (int(match[1]), int(match[2]))

    numeric: dict[tuple[int, int], list[str]] = {}
    for value in sorted(known_ids):
        key = numeric_key(value)
        if key is not None:
            numeric.setdefault(key, []).append(value)
    references = []
    for label in labels:
        if not isinstance(label, str):
            raise ValueError("LoCoMo evidence IDs must be strings")
        source = f"{identity}:{label}"
        if label in known_ids:
            references.append(EvidenceReference(source_id=source, resolved_id=source, resolution="exact"))
            continue
        key = numeric_key(label)
        matches = [] if key is None else numeric.get(key, [])
        if len(matches) == 1:
            references.append(EvidenceReference(source_id=source, resolved_id=f"{identity}:{matches[0]}",
                                                resolution="normalized_numeric"))
        else:
            references.append(EvidenceReference(source_id=source, resolved_id=None,
                                                resolution="ambiguous" if matches else "missing"))
    return tuple(references)


def locomo(path: Path, *, seed: int = 20260923, dev_conversations: int = 2,
           targets: dict[str, int] | None = None) -> tuple[list[Corpus], list[Case]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    identities = sorted(str(row["sample_id"]) for row in rows)
    if len(set(identities)) != len(identities) or not 0 < dev_conversations < len(rows):
        raise ValueError("LoCoMo requires unique conversations and nonempty dev/holdout groups")
    rng = random.Random(seed)
    rng.shuffle(identities)
    dev = set(identities[:dev_conversations])
    corpora, cases = [], []
    for row in rows:
        identity = f"locomo:{row['sample_id']}"
        conversation = row["conversation"]
        sessions = sorted((k for k in conversation if re.fullmatch(r"session_\d+", k)),
                          key=lambda key: int(key.split("_")[1]))
        units = []
        known_ids = set()
        for session in sessions:
            at = timestamp(conversation[f"{session}_date_time"])
            for item in conversation[session]:
                known_ids.add(item["dia_id"])
                # Each observed utterance is one event, not a fabricated user/assistant pair.
                evidence = Evidence(id=f"{identity}:{item['dia_id']}", session_id=f"{identity}:{session}",
                                    speaker=item["speaker"], role="observation", at=at, text=item["text"])
                units.append(Unit(id=evidence.id, messages=(evidence,)))
        units.sort(key=lambda unit: unit.messages[0].at)
        if not units:
            raise ValueError("LoCoMo conversation is empty")
        corpora.append(Corpus(id=identity, adapter="locomo-speaker-observations-v1", units=tuple(units)))
        cutoff = max(m.at for u in units for m in u.messages) + timedelta(microseconds=1)
        for index, question in enumerate(row["qa"]):
            category = LOCOMO_CATEGORIES[int(question["category"])]
            if category == "open-domain":
                continue
            absent = category == "adversarial"
            references = locomo_evidence_references(identity, question.get("evidence", []), known_ids) if not absent else ()
            # Keep unresolved original IDs. Dropping them would silently shrink the recall denominator.
            gold = tuple(ref.resolved_id or ref.source_id for ref in references)
            cases.append(Case(id=f"{identity}:q{index}", dataset="locomo", corpus_id=identity,
                              cluster_id=identity, split="dev" if str(row["sample_id"]) in dev else "holdout",
                              category=category, official_category=int(question["category"]),
                              question=question["question"], cutoff=cutoff,
                              answer=str(question.get("answer", "")), unanswerable=absent,
                              gold_ids=gold, gold_level="message" if gold else "none",
                              evidence_references=references))
    # Round-robin across whole conversations; never sample on model outcomes.
    targets = targets if targets is not None else {"multi-hop": 60, "single-hop": 20, "temporal": 20, "adversarial": 20}
    selected = [case for case in cases if case.split == "dev"]
    for category, count in sorted(targets.items()):
        if count < 0:
            raise ValueError("Sample counts cannot be negative")
        buckets = {}
        for case in cases:
            if case.split == "holdout" and case.category == category:
                buckets.setdefault(case.cluster_id, []).append(case)
        for bucket in buckets.values():
            rng.shuffle(bucket)
        groups = sorted(buckets)
        rng.shuffle(groups)
        for _ in range(count):
            groups = [group for group in groups if buckets[group]]
            if not groups:
                break
            group = groups.pop(0)
            selected.append(buckets[group].pop())
            groups.append(group)
    return corpora, sorted(selected, key=lambda case: case.id)


def prepare(*, lme_dev: Path | None = None, lme_holdout: Path | None = None,
            locomo_path: Path | None = None, seed: int = 20260923, time_policy: str = "strict") -> Suite:
    corpora, cases, sources = [], [], {"adapter_version": "1", "split_seed": str(seed),
                                     "longmemeval_time_policy": time_policy}
    for split, path in (("dev", lme_dev), ("holdout", lme_holdout)):
        if path is not None:
            cs, qs = longmemeval(path, split, time_policy=time_policy)
            corpora.extend(cs)
            cases.extend(qs)
            sources[f"longmemeval_{split}_sha256"] = file_digest(path)
    if locomo_path is not None:
        cs, qs = locomo(locomo_path, seed=seed)
        corpora.extend(cs)
        cases.extend(qs)
        sources["locomo_sha256"] = file_digest(locomo_path)
        sources["locomo_evidence_policy"] = "numeric-id-audit-v1;unresolved-excluded-from-evidence-metrics-only"
    if not cases:
        raise ValueError("No evaluation cases supplied")
    return Suite(sources=sources, corpora=tuple(corpora), cases=tuple(cases))
