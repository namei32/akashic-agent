from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import random
from statistics import mean

from .schema import Suite, digest, file_digest, write_json
from .support import parent


def report(
    suite: Suite, settings, retrieval: Path, output: Path, *, judgments: Path | None
):
    rows = parent(retrieval, "retrieval.jsonl", suite, settings)
    cases = {c.id: c for c in suite.cases}
    evidence = defaultdict(list)
    for row in rows:
        evidence[(row["dataset"], row["arm"])].append(row)
    summary = {
        "suite_sha256": digest(suite),
        "settings": settings.model_dump(mode="json"),
        "retrieval_sha256": file_digest(retrieval / "retrieval.jsonl"),
        "protocol": suite.protocol,
        "retrieval": [],
        "quality": [],
        "quality_final_ready": False,
    }
    for (dataset, arm), items in sorted(evidence.items()):
        valid = [
            r["evidence_complete"]
            for r in items
            if r.get("evidence_complete") is not None
        ]
        summary["retrieval"].append(
            {
                "dataset": dataset,
                "arm": arm,
                "questions": len(items),
                "failures": sum(bool(r["error"]) for r in items),
                "complete_evidence": mean(valid) if valid else None,
                "mean_context_chars": mean(r.get("context_chars", 0) for r in items),
                "mean_query_ms": (
                    mean(
                        r["retrieval_ms"]
                        for r in items
                        if r.get("retrieval_ms") is not None
                    )
                    if any(r.get("retrieval_ms") is not None for r in items)
                    else None
                ),
            }
        )
    if judgments:
        scored = parent(judgments, "judgments.jsonl", suite, settings)
        groups = defaultdict(list)
        for row in scored:
            for category in dict.fromkeys(("all", cases[row["case_id"]].category)):
                groups[(row["dataset"], category, row["metric"])].append(row)
        receipt = json.loads((judgments / "completion.json").read_text())
        summary["quality_final_ready"] = (
            receipt["pending"] == 0 and settings.embedding_model != "offline-fixture"
        )
        summary["judgments_sha256"] = file_digest(judgments / "judgments.jsonl")
        for (dataset, category, metric), items in sorted(groups.items()):
            by_case = defaultdict(list)
            for row in items:
                if row["score"] is not None:
                    by_case[(row["case_id"], row["arm"])].append(row["score"])
            values = {key: mean(scores) for key, scores in by_case.items()}
            point = {
                arm: mean(v for (identity, a), v in values.items() if a == arm)
                for arm in ("hybrid", "full", "oracle")
                if any(a == arm for _, a in values)
            }
            pairs = [
                identity
                for identity, arm in values
                if arm == "hybrid" and (identity, "full") in values
            ]
            clusters = defaultdict(list)
            for identity in pairs:
                clusters[cases[identity].cluster_id].append(
                    values[(identity, "full")] - values[(identity, "hybrid")]
                )
            interval = None
            if len(clusters) > 1:
                rng = random.Random(settings.seed)
                keys = sorted(clusters)
                samples = sorted(
                    mean(
                        v
                        for key in rng.choices(keys, k=len(keys))
                        for v in clusters[key]
                    )
                    for _ in range(2000)
                )
                interval = [samples[49], samples[1949]]
            summary["quality"].append(
                {
                    "dataset": dataset,
                    "category": category,
                    "metric": metric,
                    "means": point,
                    "paired_questions": len(pairs),
                    "clusters": len(clusters),
                    "delta": (
                        mean(v for values_ in clusters.values() for v in values_)
                        if pairs
                        else None
                    ),
                    "paired_95_interval": interval,
                    "pending": sum(r["score"] is None for r in items),
                    "execution_failures": sum(bool(r["error"]) for r in items),
                }
            )
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "summary.json", summary)
    lines = [
        "# EpBench / InMind 评测",
        "",
        "这是当前记忆组件与固定阅读器的对照，未修改生产学习算法。",
        "EpBench 使用章节观察适配；InMind 按第九会话插入目标后再摄入 38 个完整会话。",
        "接纳时钟只表示摄入顺序，原始日期保留在原文中；不据此证明自然遗忘。",
        "",
        "| 数据 | 组别 | 问题数 | 失败 | 全部必要证据覆盖 | 平均证据字符 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    if settings.embedding_model == "offline-fixture":
        lines.insert(
            2, "本次使用结构验证用向量，不测语义检索质量；这些数字只用于检查接线。"
        )
    for row in summary["retrieval"]:
        lines.append(
            f"| {row['dataset']} | {row['arm']} | {row['questions']} | {row['failures']} | {row['complete_evidence']} | {row['mean_context_chars']:.1f} |"
        )
    lines += [
        "",
        (
            "回答评分已就绪。"
            if summary["quality_final_ready"]
            else "回答质量尚未就绪；纯检索和请求构造不代表回答能力。"
        ),
        "",
        "EpBench 答案评分使用冻结官方 generate_metric 的 lenient F1；按题型分别报告。",
        "Chronological ordering score 未执行，F1 不证明时间顺序；未形成官方完整榜单成绩。",
        "InMind 的 oracle 是目标记忆可见的诊断对照，单独报告；target-recall 裁判不读取回答。",
        "短/长 EpBench 可能共享事件；InMind 任务共享固定背景。区间按故事/任务聚类，条件于这些语料，未作多重比较校正。",
    ]
    if summary["quality"]:
        lines += [
            "",
            "| 数据/分组 | 指标 | H | F | Oracle | F−H | 95% 区间 |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
        for row in summary["quality"]:
            p = row["means"]
            lines.append(
                f"| {row['dataset']}/{row['category']} | {row['metric']} | {p.get('hybrid')} | {p.get('full')} | {p.get('oracle')} | {row['delta']} | {row['paired_95_interval']} |"
            )
    (output / "report.md").write_text("\n".join(lines) + "\n")
