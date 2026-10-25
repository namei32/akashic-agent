"""Continue the explicitly authorized full run after the pilot passes integrity gates."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import fcntl
import os
from pathlib import Path
import shutil

from .data import prepare, read, sha, write
from .evaluate import active_worker, report, run_all


def prepare_full(pilot: Path, full: Path) -> dict:
    initial = read(pilot / "evaluation.json")
    full.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (full / "evaluation.json").exists():
        shutil.copytree(pilot / "data", full / "data", dirs_exist_ok=True)
        (full / "credentials").mkdir(exist_ok=True, mode=0o700)
        shutil.copy2(pilot / "credentials/model-registry.sqlite3",
                     full / "credentials/model-registry.sqlite3")
        protocol = prepare(full, full / "data/longmemeval_s_cleaned.json",
                           Path(initial["distribution"]), full=True)
        selected = read(pilot / "worker-harness.json")
        shutil.copytree(pilot / selected["directory"], full / "harness",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(pilot / selected["manifest"], full / "harness-manifest.json")
        write(full / "worker-harness.json", {"directory": "harness", "manifest": "harness-manifest.json"})
    else:
        protocol = read(full / "evaluation.json")
    for key in ("dataset", "distribution_sha256", "source_commit", "budget", "retrieval", "arms"):
        if protocol[key] != initial[key]:
            raise ValueError(f"Full run differs from the fixed pilot configuration: {key}")
    if not protocol["full"] or len(protocol["cases"]) != 500:
        raise ValueError("Expected the complete 500-case release")
    return protocol


async def run_pipeline(pilot: Path, full: Path, workers: int) -> None:
    lock = (pilot / "pipeline.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state_path = pilot / "pipeline-status.json"
    try:
        protocol = prepare_full(pilot, full)
        write(pilot / "full-run-authorization.json", {
            "user_request": "20 题通过后继续 500 题",
            "pass_definition": "All pilot cases complete; chronological learning and preservation pass. Correct answers are not required for every question.",
            "full_root": str(full), "workers": workers,
        })
        while True:
            summary = report(pilot)
            write(state_path, {"stage": "waiting_for_pilot", "completed": summary["completed"],
                               "total": summary["total"], "full_root": str(full),
                               "at": datetime.now(UTC).isoformat()})
            if summary["completed"] == summary["total"] and not summary["errors"]:
                break
            # Continue waiting while the current controller is active; it may be
            # recovering an identified scorer issue from preserved raw outputs.
            controller = read(pilot / "controller.json")
            process = Path("/proc") / str(controller["pid"])
            alive = False
            try:
                state = (process / "stat").read_text().split(") ", 1)[1].split()[0]
                alive = state != "Z"
            except FileNotFoundError:
                pass
            if not alive:
                write(state_path, {"stage": "needs_attention", "reason": "Pilot controller stopped before all integrity gates passed",
                                   "completed": summary["completed"], "errors": summary["errors"]})
                return
            await asyncio.sleep(20)
        while any(active_worker(pilot, row["question_id"]) for row in read(pilot / "evaluation.json")["cases"]):
            await asyncio.sleep(2)
        inherited = []
        full_rows = {row["question_id"]: row for row in protocol["cases"]}
        for row in read(pilot / "evaluation.json")["cases"]:
            identity = row["question_id"]
            source, target = pilot / "cases" / identity, full / "cases" / identity
            result = read(source / "result.json")
            if not read(source / "chronology.json")["chronological_prefix"]:
                raise ValueError(f"Pilot chronology failed: {identity}")
            if not all(result["preservation"].get(key) is True for key in (
                "history_unchanged", "memory_unchanged_after_queries")):
                raise ValueError(f"Pilot preservation failed: {identity}")
            for name, field in (("history.json", "history_sha256"), ("qa.json", "qa_sha256")):
                if sha(source / name) != full_rows[identity][field]:
                    raise ValueError(f"Pilot input differs from the full dataset: {identity}")
            if not (target / "result.json").exists():
                for name in ("result.json", "default_memory.json", "raw_hybrid.json", "oracle.json",
                             "chronology.json", "preservation.json", "models.json", "memories.json",
                             "source-order.json", "learning.json"):
                    if (source / name).exists():
                        write(target / name, read(source / name))
                write(target / "inherited-source.json", {"source_case": str(source),
                    "workspace_and_credentials_copied": False,
                    "reason": "Reuse immutable completed results; detailed databases remain in the pilot."})
            inherited.append({"question_id": identity, "result_sha256": sha(source / "result.json")})
        write(full / "inherited-pilot-results.json", {
            "source": str(pilot), "cases": inherited,
            "memory_algorithm_tuned_on_pilot": False,
            "note": "Identical inputs/configuration; reuse completed outputs, not cross-instance model request replay.",
        })
        write(full / "controller.json", {"pid": os.getpid(), "kind": "authorized-pipeline", "workers": workers,
                                         "pilot_root": str(pilot), "at": datetime.now(UTC).isoformat()})
        write(state_path, {"stage": "full_running", "pilot_completed": len(inherited), "full_root": str(full),
                           "full_total": 500, "pid": os.getpid(), "at": datetime.now(UTC).isoformat()})
        await run_all(full, workers)
        final = report(full)
        write(state_path, {"stage": "complete" if final["completed"] == 500 and not final["errors"] else "needs_attention",
                           "full_root": str(full), "completed": final["completed"], "total": 500,
                           "at": datetime.now(UTC).isoformat()})
    finally:
        lock.close()
