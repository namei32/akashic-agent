from __future__ import annotations

import ast
from collections import OrderedDict
import json
import importlib
from pathlib import Path
import random
import re
from typing import Literal

from .schema import Case, Corpus, Evidence, Suite, Unit, clock, file_digest, jsonl

INMIND_REVISION = "5a2ab2686d3d5b832575f0288d41ecc93eab358b"
EPBENCH_REVISION = "892b22af097d4389d4f1b9cd47b5c51fdacd9bef"
PINNED_INMIND = {
    "benchmark/dataset/inmind.jsonl": "4e27c56047d1f7c3eaf21ad5b6662f638f81a434034546be08b85e09876675a4",
    "evaluation/background/lme_s_background.jsonl": "aef51e8a4e606a3ac5ceafdc64d875a7f1f41a051dafd68dce45f9c90d626da1",
    "evaluation/background/manifest.json": "14d30226edf82c823b7438bd3943847c1127503204d637ef074f04d815898125",
    "evaluation/prompts/answer_system.txt": "43b42e53b64c754478b8b17e0ceda6ed2476367e3133766239c97c07ed8eba54",
    "evaluation/prompts/judge_naive.txt": "3fa007ac801223bc3fe070e1c511cfb44e03839280843342ce0900a18e8a0cca",
    "evaluation/prompts/judge_target_recall.txt": "af55e9eacb13adfc5163cb137509194b3fd8bfdfbade869d0c6afecb44158f1d",
    "evaluation/prompts/judge_application.txt": "d45cc35770befb8b8526454a48b745fdd07e067234b1d98262177a9445b5f64c",
}
PINNED_EP_SCORING = {
    "evaluation/prompts.py": "7515bbd911d2475066c8300fda5f45758f9bdc80f3a74bf1b3e05986fea8f322",
    "evaluation/scoring_answers.py": "def2002e20d99849d0a0a068f4e34c8e2197a4b5db9c8ce355512bdf47e6f15b",
}


def source(files: dict[str, str], path: Path) -> None:
    files[str(path.resolve())] = file_digest(path)


def inmind(root: Path, files: dict[str, str], *, seed: int = 20261007):
    for name, expected in PINNED_INMIND.items():
        if file_digest(root / name) != expected:
            raise ValueError(f"InMind source differs from pinned release: {name}")
    tasks_path = root / "benchmark/dataset/inmind.jsonl"
    background_path = root / "evaluation/background/lme_s_background.jsonl"
    manifest_path = root / "evaluation/background/manifest.json"
    for path in (
        tasks_path,
        background_path,
        manifest_path,
        root / "benchmark/dataset/schema.json",
        *(root / "evaluation/prompts").glob("*.txt"),
    ):
        source(files, path)
    tasks, background = jsonl(tasks_path), jsonl(background_path)
    manifest = json.loads(manifest_path.read_text())
    if file_digest(background_path) != manifest["sha256"]:
        raise ValueError("InMind background checksum mismatch")
    if len(tasks) != 125 or len({t["task_id"] for t in tasks}) != 125:
        raise ValueError("Expected the frozen 125-task InMind release")
    sessions: OrderedDict[str, list[dict]] = OrderedDict()
    previous = None
    for row in background:
        sid = row["id"].rsplit("-turn-", 1)[0]
        if sid != previous and sid in sessions:
            raise ValueError("Non-contiguous InMind session")
        sessions.setdefault(sid, []).append(row)
        previous = sid
    if (
        len(background) != 486
        or len(sessions) != 47
        or manifest["injection_session_index"] != 8
    ):
        raise ValueError("InMind middle-injection protocol changed")
    grouped: dict[str, list[int]] = {}
    for task in tasks:
        grouped.setdefault(task["domain"], []).append(task["task_id"])
    rng = random.Random(seed)
    for values in grouped.values():
        rng.shuffle(values)
    # Whole tasks are split; the two queries never cross a split.
    dev = {
        value
        for values in grouped.values()
        for value in values[: max(1, len(values) // 5)]
    }
    corpora, cases = [], []
    for task in tasks:
        identity = f"inmind:{task['task_id']}"
        target_user, target_assistant = (
            f"inmind-task-{task['task_id']}-turn-{i}" for i in (0, 1)
        )
        units, order = [], 0
        for index, (sid, rows) in enumerate(sessions.items()):
            visible = list(rows)
            if index == 8:
                visible += [
                    dict(id=target_user, role="user", content=task["user_message"]),
                    dict(
                        id=target_assistant,
                        role="assistant",
                        content=task["assistant_message"],
                    ),
                ]
            pending: list[Evidence] = []
            for row in visible:
                if row["role"] not in {"user", "assistant"} or not isinstance(
                    row["content"], str
                ):
                    raise ValueError("Unexpected InMind message role or text")
                pending.append(
                    Evidence(
                        id=row["id"],
                        session_id=sid,
                        speaker=row["role"],
                        role=row["role"],
                        at=clock(order),
                        text=row["content"],
                    )
                )
                order += 1
                if row["role"] == "assistant":
                    units.append(Unit(id=pending[0].id, messages=tuple(pending)))
                    pending = []
            if pending:
                units.append(Unit(id=pending[0].id, messages=tuple(pending)))
        corpus = Corpus(
            id=identity, adapter="inmind-middle-injection-v1", units=tuple(units)
        )
        flat = [m for u in corpus.units for m in u.messages]
        target_index = next(i for i, m in enumerate(flat) if m.id == target_user)
        if sum(m.role == "user" for m in flat[:target_index]) != 41:
            raise ValueError("Target was not appended at the end of the ninth session")
        if len({m.session_id for m in flat[target_index + 2 :]}) != 38:
            raise ValueError("Target must precede 38 complete background sessions")
        corpora.append(corpus)
        for kind, query in (("naive", task["naive_query"]), ("query", task["query"])):
            cases.append(
                Case(
                    id=identity + ":" + kind,
                    dataset="inmind",
                    corpus_id=identity,
                    cluster_id=identity,
                    split="dev" if task["task_id"] in dev else "holdout",
                    category=kind,
                    question=query,
                    cutoff=clock(order),
                    gold_ids=(target_user,),
                    truth={
                        "task_id": task["task_id"],
                        "domain": task["domain"],
                        "user_message": task["user_message"],
                        "assistant_message": task["assistant_message"],
                        "explanation": task["explanation"],
                        "prompt_root": str((root / "evaluation/prompts").resolve()),
                    },
                )
            )
    return corpora, cases


def epbench(book: Path, files: dict[str, str], *, split: Literal["dev", "holdout"]):
    """Read released chapters/Parquet directly; never regenerate a narrative."""
    parquet = importlib.import_module("pyarrow.parquet")

    paths = [
        book / name
        for name in ("book.json", "df_book_groundtruth.parquet", "df_qa.parquet")
    ]
    for path in paths:
        source(files, path)
    text = json.loads(paths[0].read_text())
    if not isinstance(text, str):
        raise ValueError("Released EpBench book must be a JSON string")
    headings = list(re.finditer(r"(?m)^Chapter (\d+)\n", text))
    if not headings or headings[0].start() != 0:
        raise ValueError("Unrecognized original EpBench chapter boundaries")
    chapters = [
        (
            int(match[1]),
            text[
                match.start() : (
                    headings[i + 1].start() if i + 1 < len(headings) else len(text)
                )
            ],
        )
        for i, match in enumerate(headings)
    ]
    truth_rows = parquet.read_table(paths[1]).to_pylist()
    if [n for n, _ in chapters] != list(range(1, len(chapters) + 1)) or [
        n for n, _ in chapters
    ] != [row["chapter"] for row in truth_rows]:
        raise ValueError("Book chapter order differs from released ground truth")
    identity = "epbench:" + file_digest(paths[0])[:16]
    units = tuple(
        Unit(
            id=f"{identity}:chapter:{n}",
            messages=(
                Evidence(
                    id=f"{identity}:chapter:{n}",
                    session_id=identity,
                    speaker="narrator",
                    role="observation",
                    at=clock(i),
                    text=content,
                ),
            ),
        )
        for i, (n, content) in enumerate(chapters)
    )
    corpus = Corpus(id=identity, adapter="epbench-original-chapter-v1", units=units)
    cases = []
    for index, row in enumerate(parquet.read_table(paths[2]).to_pylist()):
        refs = tuple(
            f"{identity}:chapter:{int(n)}" for n in row["correct_answer_chapters"]
        )
        answers = list(row["correct_answer"])
        # Full-event questions use the corresponding original chapter as the long reference.
        detailed = ast.literal_eval(row["correct_answer_detailed"])
        long_answer = None
        if row["retrieval_type"] == "Full event details" and answers:
            if len(detailed) != 1:
                raise ValueError(
                    "Full-event reference must resolve to one original chapter"
                )
            chapter = int(next(iter(detailed)))
            long_answer = units[chapter - 1].messages[0].text
        cases.append(
            Case(
                id=f"{identity}:qa:{index}",
                dataset="epbench",
                corpus_id=identity,
                cluster_id=identity,
                split=split,
                category=row["get"],
                question=row["question"],
                cutoff=clock(len(units)),
                gold_ids=refs,
                truth={
                    "answers": answers,
                    "long_answer": long_answer,
                    "retrieval_type": row["retrieval_type"],
                    "get": row["get"],
                    "cue": row["cue"],
                    "q_idx": row["q_idx"],
                    "matching_events": row["n_chapters_correct_answer"],
                    "bin": row["bins_items_correct_answer"],
                    "book": str(book.resolve()),
                },
            )
        )
    return [corpus], cases


def prepare(
    *,
    inmind_root: Path | None,
    dev_books: list[Path],
    holdout_books: list[Path],
    epbench_source: Path | None,
    seed: int,
) -> Suite:
    files: dict[str, str] = {}
    corpora, cases = [], []
    if inmind_root:
        histories, queries = inmind(inmind_root, files, seed=seed)
        corpora += histories
        cases += queries
    if dev_books or holdout_books:
        if epbench_source is None:
            raise ValueError("EpBench scoring source is required")
        for name in ("evaluation/prompts.py", "evaluation/scoring_answers.py"):
            path = epbench_source / "epbench/src" / name
            if file_digest(path) != PINNED_EP_SCORING[name]:
                raise ValueError(
                    f"EpBench scoring source differs from pinned release: {name}"
                )
            source(files, path)
        groups: list[tuple[Literal["dev", "holdout"], list[Path]]] = [
            ("dev", dev_books),
            ("holdout", holdout_books),
        ]
        for split, books in groups:
            for book in books:
                histories, queries = epbench(book, files, split=split)
                corpora += histories
                cases += queries
    if not cases:
        raise ValueError("Provide at least one official dataset")
    return Suite(
        source_files=files,
        corpora=tuple(corpora),
        cases=tuple(cases),
        protocol={
            "inmind_revision": INMIND_REVISION,
            "epbench_revision": EPBENCH_REVISION,
            "epbench_source": str(epbench_source.resolve()) if epbench_source else None,
            "seed": seed,
            "clock": "synthetic acceptance order; original dates remain in source text",
            "scope": "MemoryCycle component adaptation with a fixed reader; no production Agent lifecycle claim",
            "epbench_overlap": "Short/long books can share events; splits do not imply unseen users or worlds",
            "inmind_background": "All tasks share the prescribed background; task-level intervals are conditional on it",
        },
    )
