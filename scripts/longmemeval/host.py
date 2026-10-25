"""Isolated installed-plugin lifecycle; live state is only read for credentials."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import sqlite3
import sys

from .data import canonical_hash, read, seed_history, sha, write

CHOSEN = {"ledger", "content", "commands", "context", "tools", "turn_projection",
          "models", "openai-compatible", "codex", "host_execution", "akasha", "default_memory"}
HISTORY_TABLES = ("messages", "sessions", "attachments", "message_attachments", "owner_records")
MEMORY_TABLES = ("memory_items", "learning", "origins", "changes", "metadata")


def credentials(base: Path, live_workspace: Path) -> None:
    """Copy only configuration into a private template; never copy chat history."""
    target = base / "credentials/model-registry.sqlite3"
    if target.exists():
        return
    if base.resolve().is_relative_to(live_workspace.resolve()):
        raise ValueError("Evaluation must be outside the production workspace")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{live_workspace / 'model-registry.sqlite3'}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
    os.chmod(target, 0o600)
    with sqlite3.connect(target) as db:
        db.execute("DELETE FROM model_calls")
        db.commit()
        db.execute("VACUUM")


class Host:
    def __init__(self, base: Path, folder: Path):
        self.base, self.folder = base.resolve(), folder.resolve()
        self.protocol = read(base / "evaluation.json")
        if self.protocol["kind"] != "akashic-longmemeval" or not self.folder.is_relative_to(self.base / "cases"):
            raise ValueError("Not an isolated evaluation case")
        self.workspace, self.home = self.folder / "workspace", self.folder / "home"
        self.distribution = Path(self.protocol["distribution"])
        if sha(self.distribution / "distribution.json") != self.protocol["distribution_sha256"]:
            raise ValueError("Installed distribution changed")
        os.environ.update(HOME=str(self.home), AKASHIC_PLUGIN_HOME=str(self.home),
            AKASHIC_WORKSPACE=str(self.workspace), AKASHIC_PLUGIN_DISTRIBUTION=str(self.distribution),
            AKASHIC_EXTRA_PLUGIN_DIRS="", AKASHIC_EXECUTION_MODE="local", AKASHIC_WORKLOAD_SOCKET="")
        sys.path.insert(0, str(self.distribution.parent / "core"))
        self.manager = None

    async def boot(self) -> None:
        from agent.plugins.manager import PluginManager
        from agent.plugins.selection import PluginSelection
        from agent.plugins.distribution_sources import distribution_sources
        from agent.plugins.bundles import set_plugin_choice
        from agent.plugin_composition.config_input import save_config

        self.workspace.mkdir(parents=True, exist_ok=True)
        registry = self.workspace / "model-registry.sqlite3"
        if not registry.exists():
            shutil.copy2(self.base / "credentials/model-registry.sqlite3", registry)
            os.chmod(registry, 0o600)
        if not (self.workspace / "runtime/plugin-stable.json").exists():
            PluginSelection(self.workspace).initialize()
            for row in read(self.distribution / "distribution.json")["plugins"]:
                set_plugin_choice(self.workspace, row["name"] + "@release",
                                  enabled=row["name"] in CHOSEN, distribution=self.distribution)
            save_config(self.workspace / "plugin-data/akasha-release", {"enabled": True, "memory_enabled": False})
            save_config(self.workspace / "plugin-data/default_memory-release",
                        {"mode": "active", "auto_learn": False, "sources": ["conversation"], "max_chars": 6000})
            save_config(self.workspace / "plugin-data/context-release",
                        {"prompt_sources": {}, "recall_source": ["default_memory", "default_memory@release"]})
        self.manager = PluginManager([], workspace=self.workspace, installed_cache_root=self.home / "cache",
                                     distribution=distribution_sources(self.workspace, self.home, self.distribution))
        await self.manager.load_all()
        await self.manager.reconcile_changed()
        write(self.folder / "runtime-status.json", self.manager.plugin_status())
        from plugins.default_memory.contract import DEFAULT_MEMORY
        from plugins.models.contract import CHAT_MODELS, EMBEDDINGS, MODEL_CATALOG
        self.port = self.manager.live_root.context.require(DEFAULT_MEMORY)
        self.chat = self.manager.live_root.context.require(CHAT_MODELS)
        self.embedding = self.manager.live_root.context.require(EMBEDDINGS)
        snapshot = self.manager.live_root.context.require(MODEL_CATALOG).snapshot()
        self.model_id = snapshot.role_bindings["default"]
        descriptor = self.embedding.describe()
        write(self.folder / "models.json", {
            "answer_model_id": self.model_id, "embedding_model_id": descriptor.model_id,
            "embedding_space": descriptor.identity, "embedding_dimensions": descriptor.dimensions,
            "judge_protocol": "official LongMemEval prompts with the same configured answer model",
            "official_judge_model_reproduced": False,
        })

    async def close(self) -> None:
        if self.manager is not None:
            await self.manager.terminate_all()
            self.manager = None

    async def initialize(self) -> None:
        await self.boot()
        if not (self.folder / "seeded.json").exists():
            await self.close()
            result = seed_history(self.workspace, read(self.folder / "history.json"))
            write(self.folder / "seeded.json", result)
            await self.boot()

    async def learn(self) -> dict:
        import time
        before = canonical_hash(self.workspace / "sessions.db", HISTORY_TABLES)
        audit = await self.port.audit()
        source_digests = audit["source_digests"]
        if not isinstance(source_digests, dict):
            raise TypeError("Source audit must contain an ordered digest map")
        expected_order = list(source_digests)
        write(self.folder / "source-order.json", expected_order)
        retry_counts = read(self.folder / "learning-retries.json") if (self.folder / "learning-retries.json").exists() else {}
        while True:
            status = await self.port.status()
            write(self.folder / "progress.json", {"phase": "learning", "status": status})
            failures = status["failures"]
            if failures:
                if not isinstance(failures, dict):
                    raise TypeError("Failure status must be a mapping")
                if any(retry_counts.get(key, 0) >= 3 for key in failures):
                    break
                # Models owns durable backoff. Respect its recorded deadline
                # before requesting an explicit retry through the memory port.
                with sqlite3.connect(f"file:{self.workspace / 'model-registry.sqlite3'}?mode=ro", uri=True) as db:
                    row = db.execute("SELECT max(next_attempt_at) FROM model_calls WHERE state='error'").fetchone()
                due = row[0] if row and row[0] is not None else time.time()
                wait = max(5., float(due) - time.time() + 2.)
                write(self.folder / "progress.json", {"phase": "learning_backoff", "seconds": wait, "status": status})
                await asyncio.sleep(wait)
                version = status["control_version"]
                if type(version) is not int:
                    raise TypeError("Learning control version must be an integer")
                for key in failures:
                    retry_counts[key] = retry_counts.get(key, 0) + 1
                write(self.folder / "learning-retries.json", retry_counts)
                await self.port.retry_failed(request_id=f"benchmark-retry-version-{version}", expected_version=version)
                while True:
                    status = await self.port.status()
                    write(self.folder / "progress.json", {"phase": "retrying", "status": status})
                    if not status["retry_pending"] and status["current_source"] is None:
                        break
                    await asyncio.sleep(1)
                # Never advance to a later source while a prior source is failed.
                continue
            progress = status["progress"]
            if isinstance(progress, dict) and progress["pending"] == 0:
                break
            await self.port.consume(limit=1)
        write(self.folder / "learning.json", status)
        with sqlite3.connect(f"file:{self.workspace / 'memory/default-v2.db'}?mode=ro", uri=True) as db:
            learned_order = [row[0] for row in db.execute("SELECT source_key FROM learning WHERE state='applied' ORDER BY rowid")]
        chronological = learned_order == expected_order[:len(learned_order)]
        write(self.folder / "chronology.json", {"chronological_prefix": chronological, "applied": len(learned_order), "eligible": len(expected_order)})
        if not chronological:
            raise RuntimeError("A later source was learned before an earlier source")
        if canonical_hash(self.workspace / "sessions.db", HISTORY_TABLES) != before:
            raise RuntimeError("Learning changed the imported historical records")
        write(self.folder / "preservation.json", {"history_unchanged": True, "history_sha256": before,
                                                  "production_opened_for_writes": False})
        return status

    def usage(self) -> dict:
        import json
        from collections import Counter
        with sqlite3.connect(f"file:{self.workspace / 'model-registry.sqlite3'}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT state,usage_json,duration_ms,id FROM model_calls").fetchall()
        inherited = set(read(self.folder / "reused-call-ids.json")) if (self.folder / "reused-call-ids.json").exists() else set()
        totals, states = Counter(), Counter()
        known = 0
        for state, raw, duration, identity in rows:
            states[state] += 1
            if raw:
                value = json.loads(raw)
                known += 1
                for key, count in value.items():
                    if isinstance(count, int) and not isinstance(count, bool):
                        totals[key] += count
        return {"calls": len(rows), "call_scope": "persisted chat calls only",
                "embedding_usage_complete": False,
                "embedding_note": "Raw-RAG embedding usage is in raw-vector-parts; learning/recall embeddings are not persisted by the shipped provider.",
                "states": dict(states), "calls_with_usage": known,
                "carried_call_receipts": len(inherited),
                "new_call_records": sum(row[3] not in inherited for row in rows),
                "usage_fields": dict(totals), "summed_call_duration_ms": sum(r[2] or 0 for r in rows)}
