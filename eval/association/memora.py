"""Released weekly dialogue-only adapter; hidden operations and rubrics are evaluation-only."""
from __future__ import annotations
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import random
import re
from typing import Literal

from .schema import Case, Corpus, Evidence, RubricItem, Suite, Unit, digest, file_digest


def day(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def session_refs(value):
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "session_id":
                result.append(str(item))
            else:
                result.extend(session_refs(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(session_refs(item))
    return result


def prepare_memora(data: Path, *, seed=20261003):
    personas = sorted(p.name for p in data.iterdir() if p.is_dir())
    # These two personas were inspected to validate the schema, before model evaluation.
    dev = {"financial_analyst", "academic_researcher"}
    if len(personas) != 10 or not dev <= set(personas):
        raise ValueError("Unexpected official weekly persona set")
    corpora, cases, hashes, audit = [], [], {}, []
    for persona in personas:
        # Evaluation IDs must not reveal the hidden persona profession to the reader.
        identity = "memora:weekly:" + digest(persona)[:12]
        directory = data / persona
        paths = sorted((directory / "conversations").glob("session_*.json"),
                       key=lambda p: int(p.stem.removeprefix("session_")))
        sessions = [json.loads(p.read_text()) for p in paths]
        sessions.sort(key=lambda row: (day(row["date"]), int(row["session_id"])))
        units, known_sessions = [], set()
        for path in paths:
            hashes[str(path.relative_to(data))] = file_digest(path)
        for session in sessions:
            session_id = identity + ":session:" + f"{int(session['session_id']):04d}"
            if session_id in known_sessions:
                raise ValueError("Duplicate Memora session ID")
            known_sessions.add(session_id)
            pending = []
            for index, message in enumerate(session["conversation"]):
                roles: dict[str, Literal["user", "assistant"]] = {"user_agent": "user", "user": "user", "ai_agent": "assistant"}
                role = roles.get(message["speaker"])
                if role is None or not isinstance(message["message"], str):
                    raise ValueError("Unsupported released Memora dialogue")
                pending.append(Evidence(id=f"{session_id}:m{index:04d}", session_id=session_id,
                    source_session_id=session_id, speaker=role, role=role,
                    at=day(session["date"]), text=message["message"]))
                if role == "assistant":
                    units.append(Unit(id=pending[0].id, messages=tuple(pending)))
                    pending = []
            if pending:
                units.append(Unit(id=pending[0].id, messages=tuple(pending)))
        corpus = Corpus(id=identity, adapter="memora-weekly-all-dialogue-date-only-v1", units=tuple(units))
        corpora.append(corpus)
        querypath = next(directory.glob("evaluation_questions_*.json"))
        hashes[str(querypath.relative_to(data))] = file_digest(querypath)
        queries = json.loads(querypath.read_text())
        split = "dev" if persona in dev else "holdout"
        for category, rows in sorted(queries["questions"].items()):
            if len(rows) != 5:
                raise ValueError("Weekly task should contain five questions")
            for row in rows:
                # Source has calendar dates only. Query after all sessions on that date.
                cutoff = day(row["question_date"]) + timedelta(days=1, microseconds=-1)
                visible = {m.session_id for unit in corpus.visible(cutoff).units for m in unit.messages}
                refs = tuple(dict.fromkeys(identity + ":session:" + f"{int(ref):04d}" for ref in session_refs(row["memory_evidence"])))
                unresolved = tuple(ref for ref in refs if ref not in visible)
                rubrics = tuple(RubricItem(id=item["evaluation_question_id"], question=item["evaluation_question"],
                    expected_answer=item["expected_answer"].lower(), kind=item["evaluation_type"])
                    for item in row["evaluation"]["evaluation_questions"])
                cases.append(Case(id=identity + ":" + row["question_id"], dataset="memora", corpus_id=identity,
                    cluster_id=identity, split=split, category=category, question=row["question"], cutoff=cutoff,
                    question_at=cutoff, answer=json.dumps({"memory_evidence": row["memory_evidence"],
                    "forgetting_evidence": row.get("forgetting_evidence", {})}, ensure_ascii=False),
                    gold_ids=refs, gold_level="session" if refs else "none", unresolved_gold_ids=unresolved,
                    rubrics=rubrics, strata=("has_forgetting:" + str(any(x.kind == "forgetting_absence" for x in rubrics)),)))
        audit.append({"persona": persona, "corpus_id": identity, "split": split, "sessions": len(sessions),
                      "messages": sum(len(u.messages) for u in units), "units": len(units),
                      "dialogue_sha256": digest(corpus)})
    if {x["dialogue_sha256"] for x in audit if x["split"] == "dev"} & {x["dialogue_sha256"] for x in audit if x["split"] == "holdout"}:
        raise ValueError("Development and test dialogue content overlaps")
    suite = Suite(sources={"dataset": "geniesinc/Memora weekly", "data_files_sha256": digest(hashes),
        "time_policy": "date-only; stable session order; query at end of question date",
        "memory_input": "all original user/assistant dialogue; no operation_details, share_memory, persona dossier or gold labels"},
        corpora=tuple(corpora), cases=tuple(cases))
    rng = random.Random(seed)
    pilot = []
    dev_clusters = {x["corpus_id"] for x in audit if x["split"] == "dev"}
    for cluster in sorted(dev_clusters):
        for category in ("remembering", "reasoning", "recommending"):
            bucket = sorted((q for q in cases if q.cluster_id == cluster and q.category == category), key=lambda q: q.id)
            rng.shuffle(bucket)
            pilot.extend(bucket[:2])
    if len(pilot) != 12:
        raise ValueError("Memora pilot must span both development personas and three tasks")
    return suite, suite.model_copy(update={"cases": tuple(pilot)}), {
        "source_hashes": hashes, "personas": audit, "development_selection_reason": "Contains personas inspected during schema validation",
        "case_counts": dict(Counter(c.split for c in cases)),
        "rubric_counts": dict(Counter(c.split for c in cases for _ in c.rubrics)),
        "missing_evidence_cases": [c.id for c in cases if c.unresolved_gold_ids],
    }
