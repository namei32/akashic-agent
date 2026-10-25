"""Run the frozen LongMemEval pilot through the installed memory plugin."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from longmemeval.data import fetch, prepare, read
from longmemeval.host import Host, credentials


async def probe(base: Path, identity: str) -> None:
    folder = base / "cases" / identity
    host = Host(base, folder)
    try:
        await host.initialize()
        status = await host.port.consume(limit=1)
        print(json.dumps({"question_id": identity, "status": status, "usage": host.usage()}, ensure_ascii=False))
    finally:
        await host.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "prepare", "recover", "probe", "worker", "run", "report", "diagnose", "pipeline"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--distribution", type=Path)
    parser.add_argument("--credential-workspace", type=Path)
    parser.add_argument("--case")
    parser.add_argument("--previous-root", type=Path)
    parser.add_argument("--full-root", type=Path)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.command == "fetch":
        print(json.dumps(fetch(args.root), indent=2))
    elif args.command == "prepare":
        if args.distribution is None or args.credential_workspace is None:
            parser.error("prepare requires --distribution and --credential-workspace")
        protocol = prepare(args.root, args.root / "data/longmemeval_s_cleaned.json", args.distribution, full=args.full)
        credentials(args.root, args.credential_workspace)
        print(json.dumps({"cases": len(protocol["cases"]), "user_messages": sum(c["user_messages"] for c in protocol["cases"])}))
    elif args.command == "recover":
        if args.previous_root is None or args.credential_workspace is None:
            parser.error("recover requires --previous-root and --credential-workspace")
        from longmemeval.recovery import prepare_recovery
        print(json.dumps(prepare_recovery(args.previous_root, args.root, args.credential_workspace), indent=2))
    elif args.command == "pipeline":
        if args.full_root is None:
            parser.error("pipeline requires --full-root")
        from longmemeval.pipeline import run_pipeline
        asyncio.run(run_pipeline(args.root, args.full_root, args.workers))
    elif args.command == "diagnose":
        if not args.case:
            parser.error("diagnose requires --case")
        from longmemeval.diagnostics import diagnose
        result = asyncio.run(diagnose(args.root, args.case))
        print(json.dumps({"question_id": args.case, "classification": result["classification"]}))
    elif args.command == "probe":
        identity = args.case or read(args.root / "evaluation.json")["cases"][0]["question_id"]
        asyncio.run(probe(args.root, identity))
    else:
        from longmemeval.evaluate import worker, run_all, report
        if args.command == "worker":
            if not args.case:
                parser.error("worker requires --case")
            asyncio.run(worker(args.root, args.case))
        elif args.command == "run":
            asyncio.run(run_all(args.root, args.workers))
        else:
            print(json.dumps(report(args.root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
