from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .calls import run_calls
from .datasets import prepare
from .report import report
from .retrieval import embed, retrieve
from .schema import Suite, file_digest, verify_sources, write_json
from .scoring import answer_requests, judge_requests, rescore
from .support import harness, parent


def parser():
    root = argparse.ArgumentParser(
        description="Frozen EpBench/InMind evaluation; production memory is unchanged"
    )
    commands = root.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--inmind-root", type=Path)
    prep.add_argument("--epbench-source", type=Path)
    prep.add_argument("--dev-book", action="append", type=Path, default=[])
    prep.add_argument("--holdout-book", action="append", type=Path, default=[])
    prep.add_argument("--seed", type=int, default=20261007)
    prep.add_argument("--out", type=Path, required=True)
    for name in ("embed", "retrieve", "answer", "judge", "rescore", "report"):
        command = commands.add_parser(name)
        command.add_argument("--suite", type=Path, required=True)
        command.add_argument("--harness-root", type=Path, required=True)
        command.add_argument("--settings", type=Path)
        command.add_argument("--out", type=Path, required=True)
        if name in {"embed", "retrieve"}:
            command.add_argument("--cache", type=Path, required=True)
            command.add_argument(
                "--split", choices=("dev", "holdout", "all"), required=True
            )
            command.add_argument("--limit", type=int)
        if name in {"answer", "report"}:
            command.add_argument("--retrieval", type=Path, required=True)
        if name == "judge":
            command.add_argument("--answers", type=Path, required=True)
        if name == "rescore":
            command.add_argument("--judgments", type=Path, required=True)
        if name == "report":
            command.add_argument("--judgments", type=Path)
        if name in {"embed", "answer", "judge"}:
            command.add_argument("--provider-root", type=Path)
            command.add_argument("--config", type=Path)
            command.add_argument("--model-workspace", type=Path)
            if name != "embed":
                command.add_argument(
                    "--send",
                    action="store_true",
                    help="Execute frozen requests; otherwise save request payloads only",
                )
    return root


async def remote(args, suite, settings, h):
    sending = args.command == "embed" or args.send
    requests = []
    if args.command == "answer":
        rows = parent(args.retrieval, "retrieval.jsonl", suite, settings)
        requests = list(answer_requests(suite, rows, settings))
        input_hash = file_digest(args.retrieval / "retrieval.jsonl")
    elif args.command == "judge":
        rows = parent(args.answers, "answers.jsonl", suite, settings)
        requests = list(judge_requests(suite, rows))
        input_hash = file_digest(args.answers / "answers.jsonl")
    else:
        input_hash = ""
    if not sending:
        await run_calls(
            suite,
            settings,
            h,
            requests,
            args.out,
            judging=args.command == "judge",
            input_hash=input_hash,
        )
        return
    if any(
        value is None
        for value in (args.provider_root, args.config, args.model_workspace)
    ):
        raise ValueError(
            "Live calls require an isolated provider root, config and model workspace"
        )
    os.environ["AKASHIC_EVAL_RUN_ROOT"] = str(args.provider_root.resolve())
    async with h.client.runtime_client(
        args.config, args.model_workspace, settings
    ) as client:
        if args.command == "embed":
            await embed(
                suite,
                settings,
                h,
                client,
                args.cache,
                args.out,
                split=args.split,
                limit=args.limit,
            )
        else:
            await run_calls(
                suite,
                settings,
                h,
                requests,
                args.out,
                client=client,
                judging=args.command == "judge",
                input_hash=input_hash,
            )


def main():
    args = parser().parse_args()
    if args.command == "prepare":
        suite = prepare(
            inmind_root=args.inmind_root,
            dev_books=args.dev_book,
            holdout_books=args.holdout_book,
            epbench_source=args.epbench_source,
            seed=args.seed,
        )
        write_json(args.out, suite)
        print(
            json.dumps(
                {
                    "corpora": len(suite.corpora),
                    "questions": len(suite.cases),
                    "source_files": len(suite.source_files),
                }
            )
        )
        return
    for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[key] = "1"
    suite = Suite.model_validate_json(args.suite.read_text())
    verify_sources(suite)
    h = harness(args.harness_root.resolve())
    settings = (
        h.schema.Settings()
        if args.settings is None
        else h.schema.Settings.model_validate_json(args.settings.read_text())
    )
    if args.command == "retrieve":
        retrieve(
            suite, settings, h, args.cache, args.out, split=args.split, limit=args.limit
        )
    elif args.command == "report":
        report(suite, settings, args.retrieval, args.out, judgments=args.judgments)
        return
    elif args.command == "rescore":
        rescore(suite, settings, h, args.judgments, args.out)
    else:
        asyncio.run(remote(args, suite, settings, h))
    receipt = json.loads((args.out / "completion.json").read_text())
    print(json.dumps(receipt))
    if receipt.get("failures") or receipt.get("pending"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
