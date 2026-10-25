"""Prepare a fresh chronological attempt while retaining all previous evidence."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import shutil

from .data import read, write
from .host import credentials


def prepare_recovery(previous: Path, destination: Path, live_workspace: Path) -> dict:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Recovery requires a new evaluation root")
    destination.mkdir(parents=True, exist_ok=True)
    protocol = read(previous / "evaluation.json")
    shutil.copytree(previous / "data", destination / "data")
    credentials(destination, live_workspace)
    for case in protocol["cases"]:
        old = previous / "cases" / case["question_id"]
        new = destination / "cases" / case["question_id"]
        new.mkdir(parents=True)
        for name in ("history.json", "qa.json"):
            shutil.copy2(old / name, new / name)
    recovery = {"previous": str(previous), "at": datetime.now(UTC).isoformat(),
                "policy": "Fresh memories and model calls. Cross-instance model bindings prohibit blindly copying model receipts.",
                "cases": len(protocol["cases"])}
    protocol["recovery"] = {"previous": str(previous), "chronological_per_turn": True,
                            "explicit_retries_per_source": 3, "fresh_model_calls": True}
    write(destination / "evaluation.json", protocol)
    write(destination / "recovery.json", recovery)
    return recovery
