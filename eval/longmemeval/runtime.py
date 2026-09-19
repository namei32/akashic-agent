"""Isolated evaluation adapter for the production MessageLog and programmatic APIs."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from unittest.mock import patch
from uuid import uuid4
from typing import TYPE_CHECKING, cast

from agent.plugin_composition import CHAT_MODELS, EMBEDDINGS, BoundModelDescriptor
from agent.plugins.snapshot import lease_runtime_snapshot
if TYPE_CHECKING:
    from bootstrap.tools import CoreRuntime



def parse_date(value: str, *, fallback: datetime | None = None) -> datetime:
    if not value:
        return fallback or datetime.now(UTC)
    text = re.sub(r"\s*\([^)]*\)", "", value.strip()).replace("/", "-")
    result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


@contextmanager
def replay_clock(stamp: datetime):
    """Replay source time at the log write boundary in a serial, isolated evaluator."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp.astimezone(tz) if tz is not None else stamp.replace(tzinfo=None)
    with patch("session.log.datetime", Clock):
        yield


def experiment_identity(config: Path, template: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parents[1].rglob("*.py")):
        digest.update(str(path.relative_to(Path(__file__).parents[1])).encode())
        digest.update(path.read_bytes())
    digest.update(config.read_bytes())
    for path in sorted((template / "plugin-data").glob("*/config.input.json")):
        digest.update(path.read_bytes())
    digest.update((template / "memory/VEDA.md").read_bytes())
    with sqlite3.connect((template / "model-registry.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        for table in ("model_registry_meta", "model_definitions", "embedding_models", "model_role_bindings"):
            digest.update(repr(db.execute(f"SELECT * FROM {table}").fetchall()).encode())
    home = Path(os.environ["AKASHIC_PLUGIN_HOME"])
    digest.update((home / "manifest.toml").read_bytes())
    for path in sorted((home / "cache").glob("*/*/.pointers.json")):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def prepare_experiment(args) -> None:
    if args.workers != 1:
        raise ValueError("Use --workers 1; concurrent replay needs separate processes and plugin homes")
    template = Path(os.environ.get("AKASHIC_EVAL_TEMPLATE", "")).expanduser().resolve()
    if not (template / "model-registry.sqlite3").is_file():
        raise ValueError("Set AKASHIC_EVAL_TEMPLATE to a prepared isolated template")
    if not os.environ.get("AKASHIC_PLUGIN_HOME"):
        raise ValueError("An isolated AKASHIC_PLUGIN_HOME is required")
    workspace = args.workspace.expanduser().resolve()
    if workspace == template or (workspace / "sessions.db").exists():
        raise ValueError("--workspace must be an experiment root, not a live or template workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / "experiment.json"
    identity = experiment_identity(args.config, template)
    data_digest = hashlib.sha256(identity.encode())
    data_digest.update(json.dumps({"timeout": args.timeout}, sort_keys=True).encode())
    for name in ("data", "questions", "contexts"):
        path = getattr(args, name, None)
        if path is not None:
            with path.open("rb") as data:
                for chunk in iter(lambda: data.read(1024 * 1024), b""):
                    data_digest.update(chunk)
    identity = data_digest.hexdigest()
    if marker.exists() and json.loads(marker.read_text())["identity"] != identity:
        raise ValueError("Experiment inputs changed; use a new --workspace instead of stale cached results")
    if not marker.exists():
        if any(workspace.iterdir()):
            raise ValueError("Existing directory has no experiment identity; choose an empty --workspace")
        marker.write_text(json.dumps({"identity": identity, "template": str(template)}, indent=2) + "\n")


def archive_partial_workspace(workspace: Path) -> None:
    if workspace.exists():
        if not (workspace / ".benchmark-case.json").is_file():
            raise ValueError("Refusing to replace an unmarked workspace")
        workspace.rename(workspace.with_name(workspace.name + ".partial-" + uuid4().hex))
    workspace.mkdir(parents=True, exist_ok=True)


@dataclass
class BenchmarkRuntime:
    core: CoreRuntime
    workspace: Path
    agent_model: BoundModelDescriptor
    started: bool = False

    async def start(self) -> None:
        if not self.started:
            await self.core.plugin_manager.start_runtime()
            self.started = True


def _seed_model_registry(source: Path, target: Path) -> None:
    if target.exists():
        return
    if not source.is_file():
        raise RuntimeError(f"Benchmark model registry does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as current:
        with closing(sqlite3.connect(target)) as seeded:
            current.backup(seeded)


async def create_runtime(config_path: Path, workspace: Path, *, model_registry_source: Path | None = None) -> BenchmarkRuntime:
    from agent.config import load_config
    from agent.plugins.selection import PluginSelection
    from bootstrap.init_workspace import init_workspace
    from bootstrap.tools import build_core_runtime
    from core.net.http import SharedHttpResources

    template = Path(os.environ.get("AKASHIC_EVAL_TEMPLATE", "")).expanduser().resolve()
    if not (template / "model-registry.sqlite3").is_file() or not os.environ.get("AKASHIC_PLUGIN_HOME"):
        raise RuntimeError("An isolated template and AKASHIC_PLUGIN_HOME are required")
    workspace = workspace.expanduser().resolve()
    if workspace == template:
        raise ValueError("The template cannot be used as a case workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / ".benchmark-case.json"
    if not marker.exists():
        if any(path.name not in {"trace.log"} for path in workspace.iterdir()):
            raise ValueError("Refusing a nonempty workspace without a benchmark marker")
        marker.write_text(json.dumps({"version": 2, "template": str(template)}) + "\n")
    init_workspace(config_path=config_path, workspace=workspace, force=False)
    selection = PluginSelection(workspace)
    if not selection.path.exists():selection.initialize()
    for source in (template / "plugin-data").glob("*/config.input.json"):
        target = workspace / "plugin-data" / source.parent.name / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():shutil.copyfile(source, target)
    memory = workspace / "memory"
    memory.mkdir(exist_ok=True)
    for name in ("VEDA.md", "SELF.md", "MEMORY.md"):
        target = memory / name
        if not target.exists():shutil.copyfile(template / "memory" / name, target)
    registry = model_registry_source if model_registry_source and model_registry_source.is_file() else template / "model-registry.sqlite3"
    _seed_model_registry(registry, workspace / "model-registry.sqlite3")
    http = SharedHttpResources()
    core = build_core_runtime(load_config(config_path, workspace=workspace), workspace, http)
    async def switch_endpoints(old_commands, new_commands):
        # Same contract as AppRuntime: installed tools resolve their exact scope;
        # the in-process benchmark exposes no separate channel command registry.
        return None
    core.plugin_manager.bind_endpoint_switcher(switch_endpoints)
    try:
        await core.start()
        async with lease_runtime_snapshot(core.plugin_manager.snapshot_store) as snapshot:
            if snapshot.composition_root is None:
                raise RuntimeError("Installed plugin composition is unavailable")
            context = snapshot.composition_root.context
            context.require(EMBEDDINGS).describe()
            async with context.require(CHAT_MODELS).execution() as execution:
                descriptor = execution.chat("agent").descriptor
    except BaseException:
        await core.stop()
        await http.aclose()
        raise
    return BenchmarkRuntime(core, workspace, descriptor)


async def ask(rt: BenchmarkRuntime, *, question: str, question_date: str, session_prefix: str, timeout_s: float):
    from plugins.programmatic.control import PROGRAMMATIC, AdmitParams, SendParams, ResultParams, PauseParams
    from session.message import ContentPart, Output, ToolCall, ToolResult
    from agent.plugin_composition.bindings import BINDINGS
    from agent.plugin_composition import ServiceKey
    from agent.plugin_contracts import json_value
    session_id = f"programmatic:eval:{session_prefix}:{uuid4().hex}"
    input_id = uuid4().hex
    await rt.start()
    request_time = parse_date(question_date)
    state_path = rt.workspace / "ingest_state.json"
    if not question_date and state_path.exists():
        last = json.loads(state_path.read_text()).get("replay_end")
        if last:request_time = parse_date(last) + timedelta(seconds=1)
    async with AsyncExitStack() as scope:
        # Only the source-neutral semantic clock is replayed. Auth expiry,
        # transport timeouts, metrics and the OS wall clock remain real.
        scope.enter_context(patch("agent.plugin_contracts.timekit.utcnow", lambda: request_time))
        snapshot = await scope.enter_async_context(lease_runtime_snapshot(rt.core.plugin_manager.snapshot_store))
        if snapshot.composition_root is None:
            raise RuntimeError("Installed plugin composition is unavailable")
        context = snapshot.composition_root.context
        api = context.require(PROGRAMMATIC)
        await api.call("programmatic/session/admit", AdmitParams(session_id=session_id, persist_memory=False))
        with replay_clock(request_time):
            await api.call("programmatic/message/send", SendParams(session_id=session_id, message_id=input_id, text=question))
        try:
            async with asyncio.timeout(timeout_s):
                async for _ in rt.core.message_log.reader(session_id).follow():
                    result = await api.call("programmatic/message/result", ResultParams(session_id=session_id, input_id=input_id))
                    if result["status"] != "open":break
        except (TimeoutError, asyncio.CancelledError):
            await api.call("programmatic/message/pause", PauseParams(session_id=session_id, message_id=uuid4().hex))
            raise
        if result["status"] not in {"complete", "quiet"}:
            raise RuntimeError(f"Agent ended with {result['status']}: {result.get('reason', '')}")
        reader = rt.core.message_log.reader(session_id)
        ending_id = result["ending_message_id"]
        if not isinstance(ending_id, str):
            raise RuntimeError("Completed output has no stable message ID")
        ending = reader.get(ending_id)
        if ending is None or not isinstance(ending.body, Output):
            raise RuntimeError("A completed result must refer to a persisted Output")
        answer = ''.join(part.value for part in ending.body.parts
                         if isinstance(part, ContentPart) and part.kind == "text" and isinstance(part.value, str))
        bindings = context.require(BINDINGS)
        trace = []
        messages = reader.snapshot()
        for message in messages:
            if not isinstance(message.body, Output):continue
            calls = []
            for index, part in enumerate(message.body.parts):
                if not isinstance(part, ToolCall):continue
                tool = cast(Mapping[str, object], bindings.describe(part.binding_id, ServiceKey("tools.v1"))["tool"])
                observed = next((item.body for item in messages if isinstance(item.body, ToolResult)
                    and item.body.call_ref.message_id == message.message_id and item.body.call_ref.part_index == index), None)
                calls.append({"name":tool["name"], "arguments":json_value(part.arguments),
                              "result":str(observed.parts) if observed else None})
            if calls:trace.append({"calls":calls})
        return answer, trace, session_id


def format_model_trace(rt: BenchmarkRuntime) -> str:
    d = rt.agent_model
    return f"agent_model={d.model}\ndriver={d.driver_id}\nmodel_revision={d.model_revision}\nplugin_snapshot={d.plugin_snapshot_id}\n"


async def close_runtime(rt: BenchmarkRuntime) -> None:
    try:
        await rt.core.stop()
    finally:
        await rt.core.http_resources.aclose()
