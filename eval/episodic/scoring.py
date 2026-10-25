from __future__ import annotations

import json
import math
from pathlib import Path

from .schema import Case, Suite, digest, file_digest
from .datasets import PINNED_EP_SCORING, PINNED_INMIND
from .support import functions


def epbench_functions(suite: Suite):
    root = Path(suite.protocol["epbench_source"]) / "epbench/src/evaluation"
    for filename in ("prompts.py", "scoring_answers.py"):
        if file_digest(root / filename) != PINNED_EP_SCORING["evaluation/" + filename]:
            raise ValueError("Official EpBench scoring source identity changed")
    prompts = functions(root / "prompts.py", ("generate_episodic_memory_prompt",))
    metrics = functions(
        root / "scoring_answers.py",
        ("judge_prompt_func", "f1_score_func", "remove_duplicates", "generate_metric"),
    )
    return {**prompts, **metrics}


def inmind_prompt(case: Case, filename: str) -> str:
    path = Path(case.truth["prompt_root"]) / filename
    if file_digest(path) != PINNED_INMIND["evaluation/prompts/" + filename]:
        raise ValueError("Official InMind prompt identity changed")
    return path.read_text().rstrip()


def answer_requests(suite: Suite, retrieval: list[dict], settings):
    cases = {c.id: c for c in suite.cases}
    rows = {(r["case_id"], r["arm"]): r for r in retrieval}
    identities = list(dict.fromkeys(r["case_id"] for r in retrieval))
    ep = (
        epbench_functions(suite)
        if any(cases[i].dataset == "epbench" for i in identities)
        else None
    )
    for repeat in range(settings.repeats):
        for index, identity in enumerate(identities):
            case = cases[identity]
            arms = (
                ["hybrid", "full"] if (index + repeat) % 2 == 0 else ["full", "hybrid"]
            )
            if case.dataset == "inmind" and case.category == "query":
                arms.append("oracle")
            for arm in arms:
                evidence = rows[(identity, "full" if arm == "oracle" else arm)]
                error, context = evidence["error"], evidence.get("context", "")
                if arm == "oracle":
                    error = None
                    context = f"User: {case.truth['user_message']}\nAssistant: {case.truth['assistant_message']}"
                    if len(context) > settings.budget_chars:
                        raise ValueError(
                            "Budget cannot fit the complete target-memory control"
                        )
                if case.dataset == "inmind":
                    system, user = (
                        inmind_prompt(case, "answer_system.txt").replace(
                            "{context}", context
                        ),
                        case.question,
                    )
                else:
                    assert ep is not None
                    system = "You are participating in an episodic memory test."
                    user = ep["generate_episodic_memory_prompt"](context, case.question)
                yield {
                    "case_id": identity,
                    "dataset": case.dataset,
                    "cluster_id": case.cluster_id,
                    "category": case.category,
                    "arm": arm,
                    "repeat": repeat,
                    "metric": "answer",
                    "system": system,
                    "user": user,
                    "context": context,
                    "input_error": error,
                }


def judge_requests(suite: Suite, answers: list[dict]):
    cases = {c.id: c for c in suite.cases}
    ep = (
        epbench_functions(suite)
        if any(r["dataset"] == "epbench" for r in answers)
        else None
    )
    for row in answers:
        case = cases[row["case_id"]]
        base = {
            key: row[key]
            for key in (
                "case_id",
                "dataset",
                "cluster_id",
                "category",
                "arm",
                "repeat",
                "context",
            )
        }
        if case.dataset == "epbench":
            assert ep is not None
            yield {
                **base,
                "metric": "epbench",
                "system": "You are an expert in memory tests.",
                "user": ep["judge_prompt_func"](
                    case.truth["retrieval_type"],
                    case.truth["answers"],
                    row.get("text", ""),
                    case.truth["long_answer"],
                ),
                "input_error": row["error"],
            }
            continue
        metrics = ["naive"] if case.category == "naive" else ["application"]
        if case.category == "query" and row["repeat"] == 0 and row["arm"] != "oracle":
            metrics.insert(0, "target-recall")
        for metric in metrics:
            payload = f"user_message: {case.truth['user_message']}\ncontext: {row['context']}\nquery: {case.question}"
            if metric != "naive":
                payload += f"\nexplanation: {case.truth['explanation']}"
            if metric != "target-recall":
                payload += f"\nanswer: {row.get('text', '')}"
            yield {
                **base,
                "metric": metric,
                "system": inmind_prompt(case, f"judge_{metric.replace('-', '_')}.txt"),
                "user": payload,
                "input_error": (
                    row["input_error"] if metric == "target-recall" else row["error"]
                ),
            }


def verdict(suite: Suite, case: Case, metric: str, text: str) -> dict:
    value = json.loads(text)
    if metric != "epbench":
        if (
            type(value.get("score")) is not int
            or value["score"] not in (0, 1)
            or not isinstance(value.get("reason"), str)
        ):
            raise ValueError("Expected an official binary InMind verdict")
        return {"score": value["score"], "reason": value["reason"]}
    expected = case.truth["answers"]
    matches = value["matching_score"]
    if not isinstance(value["identified_items_in_AI_answer"], list) or not all(
        isinstance(x, str) for x in value["identified_items_in_AI_answer"]
    ):
        raise ValueError("EpBench predicted items must be a string list")
    if not isinstance(matches, list) or len(matches) != len(expected):
        raise ValueError("EpBench verdict has a different reference item count")
    for reference, match in zip(expected, matches, strict=True):
        if not isinstance(match, dict) or list(match) != [reference]:
            raise ValueError("EpBench verdict changed reference identity or order")
        raw_score = match[reference]
        # The official generator uses float(value), including JSON numeric strings.
        if type(raw_score) not in (int, float, str):
            raise ValueError("EpBench match score must be numeric")
        score = float(raw_score)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("EpBench match score is outside [0,1]")
    metrics = epbench_functions(suite)["generate_metric"](expected, value)
    score = metrics["f1_score_lenient"]
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError(
            "Official EpBench formula produced an invalid score; keep verdict pending"
        )
    return {"score": score, "official_metrics": metrics}


def request_id(row: dict) -> str:
    return digest([row["case_id"], row["arm"], row["repeat"], row["metric"]])


def rescore(suite: Suite, settings, h, source: Path, output: Path):
    """Recompute all saved judge responses without changing or regenerating any."""
    from .schema import canonical, write_json
    from .support import begin, parent, source_identity

    rows = parent(source, "judgments.jsonl", suite, settings)
    begin(
        output,
        {
            "kind": "judge-rescore",
            "suite_sha256": digest(suite),
            "settings": settings.model_dump(mode="json"),
            "source": source_identity(h),
            "raw_judgments_sha256": file_digest(source / "judgments.jsonl"),
            "model_attempts": 0,
        },
    )
    cases = {c.id: c for c in suite.cases}
    failures = pending = 0
    with (output / "judgments.jsonl").open("x") as stream:
        for old in rows:
            row = {**old, "previous_error": old["error"], "error": None, "score": None}
            try:
                if row["input_error"]:
                    row["error"], row["score"] = row["input_error"], 0
                elif not row["text"]:
                    row["error"] = old["error"] or "Missing raw judge response"
                else:
                    row.update(
                        verdict(
                            suite, cases[row["case_id"]], row["metric"], row["text"]
                        )
                    )
            except Exception as error:
                row["error"] = f"{type(error).__name__}: {error}"
            failures += bool(row["error"])
            pending += row["score"] is None
            stream.write(canonical(row) + "\n")
    write_json(
        output / "completion.json",
        {
            "rows": len(rows),
            "failures": failures,
            "pending": pending,
            "model_attempts": 0,
            "sha256": file_digest(output / "judgments.jsonl"),
        },
    )
