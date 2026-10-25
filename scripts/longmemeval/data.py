"""Pinned, answer-blind historical fixtures and deterministic pilot selection."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path


def read(path: Path):
    return json.loads(path.read_text())


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()



def fetch(base: Path) -> dict:
    """Fetch the explicitly pinned public release, without executing upstream code."""
    import urllib.request
    import urllib.error
    import time
    dataset_revision = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
    official_commit = "9e0b455f4ef0e2ab8f2e582289761153549043fc"
    folder = base / "data"
    folder.mkdir(parents=True, exist_ok=True)
    targets = {
        "longmemeval_s_cleaned.json": f"https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/{dataset_revision}/longmemeval_s_cleaned.json",
        "official_evaluate_qa.py": f"https://raw.githubusercontent.com/xiaowu0162/LongMemEval/{official_commit}/src/evaluation/evaluate_qa.py",
        "LongMemEval-LICENSE": f"https://raw.githubusercontent.com/xiaowu0162/LongMemEval/{official_commit}/LICENSE",
    }
    expected = {"longmemeval_s_cleaned.json": "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442",
                "official_evaluate_qa.py": "ecce9c4c79dc89d99534ac17b383a5cbb5b9f0c69ee98adaf0684742e3d95251"}
    for name, url in targets.items():
        target = folder / name
        if not target.exists():
            partial = target.with_suffix(target.suffix + ".partial")
            request = urllib.request.Request(url, headers={"User-Agent": "akashic-longmemeval-evaluation"})
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as out:
                        while block := response.read(1024 * 1024):
                            out.write(block)
                    if name in expected and sha(partial) != expected[name]:
                        raise ValueError(f"Downloaded checksum mismatch: {name}")
                    partial.replace(target)
                    break
                except urllib.error.HTTPError as error:
                    if error.code not in {500, 502, 503, 504} or attempt == 2:
                        raise
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    if attempt == 2:
                        raise
                time.sleep(2 ** attempt)
    dataset = folder / "longmemeval_s_cleaned.json"
    evaluator = folder / "official_evaluate_qa.py"
    for name, checksum in expected.items():
        if sha(folder / name) != checksum:
            raise ValueError(f"Pinned public file checksum mismatch: {name}")
    manifest = {"dataset": "xiaowu0162/longmemeval-cleaned", "revision": dataset_revision,
                "file": dataset.name, "sha256": sha(dataset), "bytes": dataset.stat().st_size,
                "official_commit": official_commit, "evaluator_sha256": sha(evaluator)}
    path = folder / "provenance.json"
    if path.exists() and read(path) != manifest:
        raise ValueError("Existing provenance differs")
    write(path, manifest)
    return manifest


def timestamp(value: str) -> str:
    # The release does not specify a timezone. A uniform UTC interpretation
    # preserves its relative intervals; wall-clock time never drives retrieval.
    return datetime.strptime(value, "%Y/%m/%d (%a) %H:%M").replace(tzinfo=UTC).isoformat()


def prepare(base: Path, dataset: Path, distribution: Path, *, full: bool = False) -> dict:
    source = read(base / "data/provenance.json")
    if sha(dataset) != source["sha256"]:
        raise ValueError("Dataset differs from the pinned provenance")
    rows = read(dataset)
    if len(rows) != 500 or len({r["question_id"] for r in rows}) != 500:
        raise ValueError("Expected 500 distinct official cases")
    seed = "akashic-lme-pilot-v1"
    buckets = defaultdict(list)
    for row in rows:
        key = "abstention" if row["question_id"].endswith("_abs") else row["question_type"]
        buckets[key].append(row)
    selected = []
    for key in sorted(buckets):
        ordered = sorted(buckets[key], key=lambda r: hashlib.sha256((seed + ":" + r["question_id"]).encode()).hexdigest())
        selected.extend(ordered if full else ordered[:2 if key == "abstention" else 3])
    cases = []
    for row in selected:
        identity = row["question_id"]
        if not identity.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Unsafe dataset ID")
        folder = base / "cases" / identity
        sessions, gold_ids = [], []
        for si, (sid, date, turns) in enumerate(zip(
            row["haystack_session_ids"], row["haystack_dates"], row["haystack_sessions"], strict=True
        )):
            stamp = timestamp(date)
            messages = []
            for ti, turn in enumerate(turns):
                if turn["role"] not in {"user", "assistant"} or not isinstance(turn["content"], str):
                    raise ValueError(f"Unsupported original turn in {identity}")
                mid = f"{identity}:s{si}:m{ti}"
                messages.append({"id": mid, "role": turn["role"], "content": turn["content"]})
                if turn.get("has_answer"):
                    gold_ids.append(mid)
            sessions.append({"id": f"lme:{identity}:{si}", "original_id": sid,
                             "timestamp": stamp, "messages": messages})
        history = {"question_id": identity, "sessions": sessions}
        qa = {key: row[key] for key in ("question_id", "question_type", "question", "answer", "answer_session_ids")}
        qa.update(question_date=timestamp(row["question_date"]), evidence_message_ids=gold_ids)
        if (folder / "history.json").exists() and read(folder / "history.json") != history:
            raise ValueError("Existing history differs; use a new evaluation directory")
        write(folder / "history.json", history)
        write(folder / "qa.json", qa)
        cases.append({"question_id": identity, "type": row["question_type"],
                      "abstention": identity.endswith("_abs"),
                      "history_sha256": sha(folder / "history.json"),
                      "qa_sha256": sha(folder / "qa.json"),
                      "sessions": len(sessions),
                      "messages": sum(len(s["messages"]) for s in sessions),
                      "user_messages": sum(m["role"] == "user" for s in sessions for m in s["messages"])})
    protocol = {
        "kind": "akashic-longmemeval", "version": 1, "seed": seed, "full": full,
        "dataset": source, "distribution": str(distribution.resolve()),
        "distribution_sha256": sha(distribution / "distribution.json"),
        "source_commit": read(distribution / "distribution.json")["source_commit"],
        "budget": {"top_k": 8, "max_chars": 6000, "max_tokens": 2048,
                   "tokenizer": "cl100k_base", "max_output_tokens": 2048},
        "retrieval": {"bm25_k1": 1.2, "bm25_b": 0.75, "rrf_k": 60,
                      "chunk_chars": 1000, "chunk_overlap": 150},
        "clock": "Dataset timestamps interpreted uniformly as UTC",
        "answer_reasoning_effort": "low", "judge": "official prompt; configured answer model",
        "arms": ["default_memory", "raw_hybrid", "oracle"],
        "cases": cases,
    }
    marker = base / "evaluation.json"
    if marker.exists() and read(marker) != protocol:
        raise ValueError("Frozen protocol differs; create a new evaluation directory")
    write(marker, protocol)
    return protocol


def seed_history(workspace: Path, history: dict) -> dict:
    """Populate a new, stopped Ledger fixture once, before any learning.

    Public message writers use admission time rather than imported historical
    time. The installed Ledger first creates its own schema. This offline,
    insert-only fixture preserves the dataset's timestamps, roles and text.
    No production database or previously populated fixture may be targeted.
    """
    import sqlite3
    from plugins.ledger.contract import ContentPart, Input, Output, Message, encode_body

    if not (workspace.parent / "history.json").is_file():
        raise ValueError("Not a prepared benchmark case")
    path = workspace / "sessions.db"
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT count(*) FROM messages").fetchone()[0] or db.execute("SELECT count(*) FROM sessions").fetchone()[0]:
            raise ValueError("Historical seeding requires an empty Ledger")
        total = 0
        for session in history["sessions"]:
            stamp = session["timestamp"]
            messages = session["messages"]
            db.execute("INSERT INTO sessions(key,created_at,updated_at,next_seq,attributes) VALUES (?,?,?,?,?)",
                       (session["id"], stamp, stamp, len(messages),
                        '{"learning":"eligible","visibility":"listed"}'))
            for seq, row in enumerate(messages):
                at = datetime.fromisoformat(stamp) + timedelta(microseconds=seq)
                parts = (ContentPart("text", row["content"]),)
                body = Input(parts) if row["role"] == "user" else Output(parts, "complete")
                value = Message(row["id"], session["id"], seq, at, row["role"], "conversation", body)
                db.execute("INSERT INTO messages(id,session_key,seq,ts,author,source,body,metadata) VALUES (?,?,?,?,?,?,?,?)",
                           (value.message_id, value.session_id, seq, at.isoformat(), value.author,
                            value.source, encode_body(body), "{}"))
                total += 1
        db.commit()
    return {"messages": total, "sessions": len(history["sessions"]), "import": "empty-ledger-insert-only"}


def canonical_hash(path: Path, tables: tuple[str, ...]) -> str:
    import sqlite3
    h = hashlib.sha256()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        for table in tables:
            h.update(table.encode())
            for row in db.execute(f'SELECT * FROM "{table}" ORDER BY 1,2'):
                h.update(json.dumps(row, ensure_ascii=False, default=str).encode())
    return h.hexdigest()
