"""Post-run, answer-blind diagnostics; these do not replace official QA scores."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .data import canonical_hash, read, write
from .host import Host, MEMORY_TABLES
from .evaluate import complete, official_prompt


async def verdict(host: Host, prompt: str, key: str) -> dict:
    for attempt in range(3):
        value = await complete(host, prompt, f"{key}:{attempt}", judge=True)
        label = value["text"].strip().lower().strip('."\n ')
        if label in {"yes", "no"}:
            return {**value, "yes": label == "yes"}
    raise RuntimeError("Diagnostic judge returned no valid binary verdict")


async def diagnose(base: Path, identity: str) -> dict:
    folder = base / "cases" / identity
    destination = folder / "diagnostics.json"
    if destination.exists():
        return read(destination)
    if not (folder / "result.json").exists() or read(folder / "result.json")["status"] != "complete":
        raise ValueError("Only completed frozen cases can be diagnosed")
    host = Host(base, folder)
    try:
        await host.boot()
        frozen = canonical_hash(host.workspace / "memory/default-v2.db", MEMORY_TABLES)
        qa = read(folder / "qa.json")
        saved = read(folder / "default_memory.json")
        with sqlite3.connect(f"file:{host.workspace / 'memory/default-v2.db'}?mode=ro", uri=True) as db:
            records = db.execute("SELECT id,kind,fact,updated_at FROM memory_items WHERE status='active' ORDER BY id").fetchall()
        lines = []
        for mid, kind, raw, date in records:
            fact = json.loads(raw)
            lines.append(f"[{mid}] ({kind}, {date}) {fact['summary']}" +
                         ("; steps: " + "; ".join(fact["steps"]) if fact.get("steps") else ""))
        result = {"question_id": identity, "diagnostic_only": True,
                  "model_qa_correct": saved["judge"]["correct"]}
        if not identity.endswith("_abs"):
            for name, text in [("retained", "\n".join(lines)), ("presented", saved["context"]["context"])]:
                prompt = ("Determine whether the historical material contains the personal evidence needed "
                          "to support the reference answer to this question. You may combine facts and compute "
                          "time intervals. Do not invent missing facts or assume that the reference is present. "
                          "For a multi-part factual answer, all required facts must be supported. For a preference "
                          "rubric, relevant personal facts supporting a valid personalized answer suffice. "
                          "You do not see the system's generated answer. Answer yes or no only.\n\n"
                          f"Question date: {qa['question_date']}\nQuestion type: {qa['question_type']}\n"
                          f"Question: {qa['question']}\nReference answer/rubric: {qa['answer']}\n"
                          f"Historical material:\n{text or '(empty)'}")
                result[name] = await verdict(host, prompt, f"lme-diagnostic:{identity}:{name}")
            stored, shown, correct = result["retained"]["yes"], result["presented"]["yes"], result["model_qa_correct"]
            result["classification"] = (
                "diagnostic_disagreement" if shown and not stored else
                "supported_correct" if correct and shown else "correct_without_sufficient_visible_evidence" if correct
                else "not_retained_as_active_memory" if not stored else "not_presented" if not shown else "answer_use_error")
        else:
            result["classification"] = "abstention_correct" if result["model_qa_correct"] else "abstention_error"
        # An independent no-memory control checks how much the reader can answer
        # without this particular memory; it never writes a chat or learning input.
        prompt = ("Historical memory/history:\n(No relevant evidence supplied.)\n\nCurrent date: "
                  + qa["question_date"] + "\nQuestion: " + qa["question"])
        result["no_memory_answer"] = await complete(host, prompt, f"lme-diagnostic:{identity}:no-memory-answer")
        grading = official_prompt(base)(qa["question_type"], qa["question"], qa["answer"],
                    result["no_memory_answer"]["text"], abstention=identity.endswith("_abs"))
        result["no_memory_judge"] = await verdict(host, grading, f"lme-diagnostic:{identity}:no-memory-judge")
        if canonical_hash(host.workspace / "memory/default-v2.db", MEMORY_TABLES) != frozen:
            raise RuntimeError("Post-run diagnostic changed memory")
        result["memory_unchanged"] = True
        result["human_reviewed"] = False
        write(destination, result)
        return result
    finally:
        await host.close()
