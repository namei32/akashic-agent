from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path

import numpy as np

from .runner import checked_retrieval, jsonl
from .schema import Settings, Suite, digest, file_digest, write_json
from .scoring import checked_answers


def paired_interval(rows: list[dict], metric: str, *, seed=20260923, draws=2000):
    """Mean repeats within question, then resample entire conversation clusters."""
    values = defaultdict(lambda: defaultdict(list))
    clusters = {}
    for row in rows:
        if row.get(metric) is not None:
            values[row["case_id"]][row["arm"]].append(float(row[metric]))
            clusters[row["case_id"]] = row["cluster_id"]
    pairs = {identity: float(np.mean(arms["full"]) - np.mean(arms["hybrid"]))
             for identity, arms in values.items() if arms.get("hybrid") and arms.get("full")}
    grouped = defaultdict(list)
    for identity, difference in pairs.items():
        grouped[clusters[identity]].append(difference)
    result = {"paired_cases": len(pairs), "clusters": len(grouped),
              "full_minus_hybrid": None if not pairs else float(np.mean(list(pairs.values()))), "ci95": None}
    if len(grouped) >= 2:
        groups = [grouped[key] for key in sorted(grouped)]
        rng = np.random.default_rng(seed)
        sampled = [float(np.mean([v for i in rng.integers(len(groups), size=len(groups)) for v in groups[i]]))
                   for _ in range(draws)]
        result["ci95"] = [float(v) for v in np.quantile(sampled, [.025, .975])]
    return result


def stats(values):
    values = [float(v) for v in values if v is not None]
    return {"n": len(values), "mean": None if not values else float(np.mean(values)),
            "p50": None if not values else float(np.median(values)),
            "p95": None if not values else float(np.quantile(values, .95))}


def grouped_report(rows: list[dict], metric: str, seed: int):
    result = {}
    for dataset in sorted({row["dataset"] for row in rows}):
        selected = [row for row in rows if row["dataset"] == dataset]
        for category in ["all", *sorted({row["category"] for row in selected})]:
            subset = selected if category == "all" else [row for row in selected if row["category"] == category]
            averages = {}
            for arm in ("hybrid", "full"):
                by_case = defaultdict(list)
                for row in subset:
                    if row["arm"] == arm and row.get(metric) is not None:
                        by_case[row["case_id"]].append(float(row[metric]))
                averages[arm] = stats([np.mean(v) for v in by_case.values()])
            result[f"{dataset}/{category}"] = {**averages, "paired": paired_interval(subset, metric, seed=seed)}
    return result


def build_report(suite: Suite, retrieval: Path, output: Path, settings: Settings, *,
                 answers: Path | None = None, judgments: Path | None = None):
    parent, indexed = checked_retrieval(suite, retrieval, settings)
    cases = {case.id: case for case in suite.cases}
    retrieval_rows = []
    for row in indexed.values():
        case = cases[row["case_id"]]
        if case.data_issues:
            continue
        metrics = row.get("metrics", {})
        scorable = case.evidence_scorable
        retrieval_rows.append({**row, "recall": metrics.get("recall", 0.0) if scorable else None,
                               "complete": metrics.get("complete", 0.0) if scorable else None,
                               "evidence_level": case.gold_level})
    result = {"version": 1, "suite_sha256": digest(suite), "case_count": len(parent["case_ids"]),
              "eligible_case_count": sum(not cases[i].data_issues for i in parent["case_ids"]),
              "data_invalid_cases": {i: list(cases[i].data_issues) for i in parent["case_ids"] if cases[i].data_issues},
              "settings": settings.model_dump(mode="json"), "retrieval_failures": sum(bool(r["error"]) for r in retrieval_rows),
              "retrieval": {}, "latency_scope": "successful queries, cached vectors; failures reported separately"}
    result["evidence_metric_eligible_case_count"] = sum(
        cases[i].evidence_scorable and not cases[i].data_issues for i in parent["case_ids"])
    result["evidence_scoring_exclusions"] = {
        i: {"issues": list(cases[i].evidence_label_issues), "gold_ids": list(cases[i].gold_ids),
            "references": [ref.model_dump(mode="json") for ref in cases[i].evidence_references]}
        for i in parent["case_ids"] if cases[i].evidence_label_issues}
    result["evidence_id_normalizations"] = {
        i: [ref.model_dump(mode="json") for ref in cases[i].evidence_references if ref.resolution == "normalized_numeric"]
        for i in parent["case_ids"] if any(ref.resolution == "normalized_numeric" for ref in cases[i].evidence_references)}
    # Do not silently average message-label and session-label recall into one score.
    for level in ("message", "session"):
        rows = [r for r in retrieval_rows if r["evidence_level"] == level]
        result["retrieval"][level] = {"recall": grouped_report(rows, "recall", settings.seed),
                                     "complete": grouped_report(rows, "complete", settings.seed)}
    result["retrieval_latency_ms"] = {
        arm: stats(r["retrieval_ms"] for r in indexed.values() if r["arm"] == arm and not r["error"])
        for arm in ("hybrid", "full")}
    result["paired_evidence"] = [
        {"case_id": identity,
         "gained": sorted(set(indexed[(identity, "full")].get("metrics", {}).get("gold_hit_ids", [])) -
                          set(indexed[(identity, "hybrid")].get("metrics", {}).get("gold_hit_ids", []))),
         "lost": sorted(set(indexed[(identity, "hybrid")].get("metrics", {}).get("gold_hit_ids", [])) -
                        set(indexed[(identity, "full")].get("metrics", {}).get("gold_hit_ids", [])))}
        for identity in parent["case_ids"] if not cases[identity].data_issues and cases[identity].evidence_scorable]
    if answers is not None:
        answer_manifest, answer_rows = checked_answers(suite, answers, settings)
        if answer_manifest["retrieval_sha256"] != file_digest(retrieval / "retrieval.jsonl"):
            raise ValueError("Answers do not belong to this retrieval run")
        result["answer_failures"] = sum(bool(row["error"]) for row in answer_rows if not cases[row["case_id"]].data_issues)
        result["model_latency_ms"] = {arm: stats(r["model_ms"] for r in answer_rows if r["arm"] == arm and not r["error"])
                                      for arm in ("hybrid", "full")}
        result["tokens"] = {arm: {field: stats(
            (r.get("usage") or {}).get(field) for r in answer_rows if r["arm"] == arm
        ) for field in ("input_tokens", "cached_input_tokens", "output_tokens")} for arm in ("hybrid", "full")}
    if judgments is not None:
        if answers is None:
            raise ValueError("Judgments require their answer artifact")
        manifest = json.loads((judgments / "manifest.json").read_text())
        completion = json.loads((judgments / "completion.json").read_text())
        if manifest["suite_sha256"] != digest(suite) or manifest["answers_sha256"] != file_digest(answers / "answers.jsonl"):
            raise ValueError("Judgments belong to other answers")
        if manifest["settings"] != settings.model_dump(mode="json"):
            raise ValueError("Judgment settings differ")
        if completion["judgments_sha256"] != file_digest(judgments / "judgments.jsonl"):
            raise ValueError("Judgments changed")
        scored = jsonl(judgments / "judgments.jsonl")
        keys = {(r["case_id"], r["arm"], r["repeat"]) for r in scored}
        expected = {(r["case_id"], r["arm"], r["repeat"]) for r in answer_rows}
        if len(keys) != len(scored) or keys != expected:
            raise ValueError("Missing or duplicate judgments")
        scored = [r for r in scored if not cases[r["case_id"]].data_issues]
        pending = sum(r["status"] == "pending" for r in scored)
        result["pending_judgments"] = pending
        result["quality_final_ready"] = pending == 0 and bool(scored)
        # Missing judgments contribute a conservative zero; final claims remain blocked.
        semantic = [{**r, "semantic_correct": (0.0 if r["status"] == "pending" else None)
                    if r["semantic_correct"] is None else float(r["semantic_correct"])} for r in scored]
        result["semantic_accuracy"] = grouped_report([r for r in semantic if r["dataset"] not in {"personamem-v2", "memora"}
            and r.get("semantic_correct") is not None], "semantic_correct", settings.seed)
        persona_rows = [r for r in semantic if r["dataset"] == "personamem-v2"]
        result["official_personamem_mcq_accuracy"] = grouped_report(persona_rows, "semantic_correct", settings.seed)
        result["personamem_overlapping_strata"] = {
            label: grouped_report([r for r in persona_rows if label in cases[r["case_id"]].strata],
                                  "semantic_correct", settings.seed).get("personamem-v2/all")
            for label in sorted({s for r in persona_rows for s in cases[r["case_id"]].strata})}
        result["official_locomo_f1"] = grouped_report([r for r in scored if r["dataset"] == "locomo"], "official_f1", settings.seed)
        if any(r.get("scoring_mode") == "official_python_f1" for r in scored):
            result["refusal_accuracy"] = grouped_report([r for r in semantic if cases[r["case_id"]].unanswerable], "semantic_correct", settings.seed)
            result["semantic_accuracy"] = grouped_report([r for r in semantic if r["dataset"] == "longmemeval"], "semantic_correct", settings.seed)
        memora_rows = [r for r in scored if r["dataset"] == "memora"]
        result["memora_fama"] = grouped_report(memora_rows, "fama", settings.seed)
        result["memora_memory_presence"] = grouped_report(memora_rows, "memory_presence_accuracy", settings.seed)
        result["memora_forgetting_absence"] = grouped_report(memora_rows, "forgetting_absence_accuracy", settings.seed)
        result["memora_forgetting_requirement_violation"] = grouped_report(memora_rows, "forgetting_requirement_violation", settings.seed)
        if memora_rows:
            result["memora_scoring_scope"] = "official rubric and FAMA formula; fixed single Sol judge per criterion; quoted-data guard; not official three-judge leaderboard"
            result["memora_cases_with_forgetting"] = len({r["case_id"] for r in memora_rows if any(c.kind == "forgetting_absence" for c in cases[r["case_id"]].rubrics)})
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "paired-summary.json", result)
    lines = ["# Hybrid / Full paired evaluation", "",
             f"Frozen questions: {len(parent['case_ids'])}; eligible: {result['eligible_case_count']}; "
             f"data quarantines: {len(result['data_invalid_cases'])}; retrieval failures: {result['retrieval_failures']}.",
             "", "Intervals resample whole history clusters after averaging repeats within questions.",
             "Different datasets and evidence levels are reported separately. No best-of-N selection.",
             "All numbers describe this run; no automatic claim of graph causality or non-inferiority.", ""]
    lines += [f"Evidence-metric eligible questions: {result['evidence_metric_eligible_case_count']}; "
              f"unresolved annotation exclusions: {len(result['evidence_scoring_exclusions'])}; "
              f"questions with normalized evidence IDs: {len(result['evidence_id_normalizations'])}.", ""]
    if result["evidence_scoring_exclusions"]:
        lines += ["Unresolved annotations exclude the entire question from evidence metrics, not from answer-quality scoring. "
                  "Original references and the full labelled denominator are retained in paired-summary.json.", ""]
        for identity, issue in result["evidence_scoring_exclusions"].items():
            unresolved = [ref["source_id"] for ref in issue["references"] if ref["resolved_id"] is None]
            lines.append(f"- Evidence metrics excluded: {identity}; unresolved IDs: {', '.join(unresolved)}.")
        lines.append("")
    sections = {f"Recall ({level})": data["recall"] for level, data in result["retrieval"].items()}
    sections.update({name: result[name] for name in ("semantic_accuracy", "refusal_accuracy", "official_locomo_f1", "official_personamem_mcq_accuracy", "memora_fama", "memora_memory_presence", "memora_forgetting_absence") if name in result})
    for title, data in sections.items():
        lines += [f"## {title}", "", "| Dataset/category | Hybrid | Full | Difference | 95% interval | Cases/clusters |",
                  "|---|---:|---:|---:|---|---|"]
        for label, item in data.items():
            def number(value):
                return "N/A" if value is None else f"{value:.4f}"
            paired = item["paired"]
            lines.append(f"| {label} | {number(item['hybrid']['mean'])} | {number(item['full']['mean'])} | "
                         f"{number(paired['full_minus_hybrid'])} | {paired['ci95']} | "
                         f"{paired['paired_cases']}/{paired['clusters']} |")
        lines.append("")
    if result.get("pending_judgments"):
        lines.append("Judgments are pending: this is not a final quality report.")
    if result["data_invalid_cases"]:
        lines.append("Data-invalid questions are listed separately and excluded from algorithm quality; this is a filtered subset.")
    if not result["eligible_case_count"]:
        lines.append("No eligible questions: this run cannot support a quality claim.")
    if judgments is None:
        lines.append("Answer quality has NOT RUN in this report.")
    lines += ["", "Latency and token coverage (including unknown usage) are in paired-summary.json.",
              "Token differences are not monetary savings; first-build and cached-query costs are distinct."]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result
