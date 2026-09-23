from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path

from .datasets import prepare
from .embeddings import VectorCache, populate
from .report import build_report
from .runner import append, run_answers, run_retrieval, select_cases
from .schema import Settings, digest, read_suite, write_json
from .scoring import OfficialScorers, run_judgments


def parser():
    root = argparse.ArgumentParser(description="Paired Hybrid/Full evaluation; no production runtime changes.")
    commands = root.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Normalize datasets and freeze whole-conversation splits")
    for name in ("lme-dev", "lme-holdout", "locomo"):
        prep.add_argument("--" + name, type=Path)
    prep.add_argument("--seed", type=int, default=20260923)
    prep.add_argument("--time-policy", choices=("strict", "provided-history"), default="strict",
                      help="Strict quarantines gold-after-question cases; provided-history is not causal replay")
    prep.add_argument("--out", type=Path, required=True)
    for command in ("embed", "retrieve", "answer", "judge", "report"):
        cmd = commands.add_parser(command)
        cmd.add_argument("--suite", type=Path, required=True)
        cmd.add_argument("--settings", type=Path, help="Frozen Settings JSON; defaults match the comparison plan")
        if command in {"embed", "retrieve"}:
            cmd.add_argument("--cache", type=Path, required=True)
        if command in {"embed", "answer", "judge"}:
            cmd.add_argument("--config", type=Path, required=True, help="Isolated production model runtime config")
            cmd.add_argument("--model-workspace", type=Path, required=True)
        if command == "embed":
            cmd.add_argument("--receipts", type=Path, required=True)
            cmd.add_argument("--batch-size", type=int, default=32)
        else:
            cmd.add_argument("--out", type=Path, required=True)
        if command in {"answer", "report"}:
            cmd.add_argument("--retrieval", type=Path, required=True)
        if command in {"judge", "report"}:
            cmd.add_argument("--answers", type=Path, required=command == "judge")
        if command in {"retrieve", "embed"}:
            cmd.add_argument("--split", choices=("dev", "holdout"), required=True)
            cmd.add_argument("--limit", type=int, help="Development-only stratified pilot limit")
        if command == "judge":
            cmd.add_argument("--lme-rubric", type=Path)
            cmd.add_argument("--lme-sha256")
            cmd.add_argument("--locomo-scorer", type=Path)
            cmd.add_argument("--locomo-sha256")
            cmd.add_argument("--retry-from", type=Path, help="Rejudge only pending rows from a matching completed run")
        if command == "report":
            cmd.add_argument("--judgments", type=Path)
    smoke = commands.add_parser("smoke", help="Offline synthetic pipeline; no paid model or embedding calls")
    smoke.add_argument("--out", type=Path, required=True)
    return root


async def remote(args, suite, settings):
    from .client import runtime_client
    selected = select_cases(suite, args.split, args.limit) if args.command == "embed" else None
    official = None
    if args.command == "judge":
        official = OfficialScorers(lme=args.lme_rubric, lme_sha256=args.lme_sha256,
                                  locomo=args.locomo_scorer, locomo_sha256=args.locomo_sha256)
    async with runtime_client(args.config, args.model_workspace, settings) as client:
        if args.command == "embed":
            descriptor = client.embedding_descriptor()
            args.receipts.parent.mkdir(parents=True, exist_ok=True)
            with args.receipts.open("x", encoding="utf-8") as stream:
                assert selected is not None
                scoped_suite = suite.model_copy(update={"cases": tuple(selected)})
                append(stream, {"kind": "embedding_identity", "suite_sha256": digest(suite),
                                "case_ids": [case.id for case in selected], **descriptor})
                with closing(VectorCache(args.cache, model=settings.embedding_model, dimensions=settings.dimensions,
                                         space=descriptor["space"], writable=True)) as cache:
                    try:
                        receipts = await populate(scoped_suite, cache, client, batch_size=args.batch_size,
                                                  on_receipt=lambda row: append(stream, row))
                        append(stream, {"kind": "completed", "batches": len(receipts), "cache_sha256": cache.fingerprint()})
                    except BaseException as error:
                        append(stream, {"kind": "failed", "error": f"{type(error).__name__}: {error}"})
                        raise
        elif args.command == "answer":
            await run_answers(suite, args.retrieval, args.out, settings, client)
        else:
            assert official is not None
            await run_judgments(suite, args.answers, args.out, settings, client, official, retry_from=args.retry_from)


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "prepare":
        suite = prepare(lme_dev=args.lme_dev, lme_holdout=args.lme_holdout, locomo_path=args.locomo,
                        seed=args.seed, time_policy=args.time_policy)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.out, suite)
        print(json.dumps({"corpora": len(suite.corpora), "questions": len(suite.cases),
                          "data_invalid": sum(bool(c.data_issues) for c in suite.cases),
                          "evidence_scoring_excluded": sum(bool(c.evidence_label_issues) for c in suite.cases),
                          "evidence_id_normalizations": sum(ref.resolution == "normalized_numeric"
                                                            for c in suite.cases for ref in c.evidence_references)}))
        return
    if args.command == "smoke":
        from .smoke import smoke
        asyncio.run(smoke(args.out))
        return
    suite = read_suite(args.suite)
    settings = Settings() if args.settings is None else Settings.model_validate_json(args.settings.read_text())
    if args.command == "retrieve":
        with closing(VectorCache(args.cache, model=settings.embedding_model, dimensions=settings.dimensions)) as cache:
            run_retrieval(suite, cache, args.out, settings, split=args.split, limit=args.limit)
    elif args.command == "report":
        build_report(suite, args.retrieval, args.out, settings, answers=args.answers, judgments=args.judgments)
    else:
        asyncio.run(remote(args, suite, settings))
    if args.command in {"retrieve", "answer", "judge"}:
        receipt = json.loads((args.out / "completion.json").read_text())
        print(json.dumps(receipt))
        if receipt.get("failures") or receipt.get("pending"):
            raise SystemExit(2)


if __name__ == "__main__":
    main()
