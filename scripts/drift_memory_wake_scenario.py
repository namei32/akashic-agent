"""Real Wake, Default Memory, Ledger and Delivery with a local model and receiver."""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from default_memory_scenario import run
from agent.plugin_composition.config_input import save_config
from tests.support.delivery_sources import sources as delivery_sources

TARGET = {"channel": "test", "recipient": "room", "session_id": "scenario:initiative"}


async def wait_for_tool_clock() -> None:
    # ADR-0100 deliberately treats timestamps before process startup as unknown.
    # Some WSL hosts temporarily move wall time backwards by several seconds;
    # wait past that startup boundary instead of changing recovery semantics.
    from plugins.tools.contract import PROCESS_STARTED_AT
    await asyncio.sleep(5)
    async with asyncio.timeout(15):
        while datetime.now(UTC) < PROCESS_STARTED_AT + timedelta(seconds=5):
            await asyncio.sleep(.05)


PROOF = '''
from contextlib import asynccontextmanager
from functools import partial
from agent.plugin_composition import ServiceKey
from agent.plugin_composition.tasks import TASKS
from plugins.channels.contract import private_target_key
from plugins.context.contract import MATERIALS_V4
from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG
from plugins.standard_tools.shell import ShellOwners, shell_cleanup
api_version = 3
name = "scenario_private_target"
version = "1.0.0"
inject = (MESSAGE_CATALOG, BINDINGS, TASKS, MATERIALS_V4)
async def apply(ctx):
    await ctx.provide(ServiceKey("tools.cleanup.v1"), partial(
        shell_cleanup, ctx, ShellOwners(ctx), ctx.require(TASKS).open(ctx)))
    async def global_prompt(snapshot, source):
        return {"system_prompt": "TRUSTED_GLOBAL_POLICY", "reminders": (
            {"name": "private", "text": "TRUSTED_OWNER_PRIVATE_DATA", "priority": 1},)}
    async def unscoped(snapshot, source):
        return {"reminders": ({"name": "other", "text": "UNSCOPED_CONTEXT_SECRET", "priority": 1},)}
    materials = ctx.require(MATERIALS_V4)
    await materials.register(ctx, name="trusted-global", kind="context", prepare=global_prompt, prompt=True)
    await materials.register(ctx, name="unscoped-data", kind="context", prepare=unscoped)
    class Proof:
        @asynccontextmanager
        async def guard(self, recipient, session_id):
            async with ctx.runtime_scope():
                catalog = ctx.require(MESSAGE_CATALOG)
                lease = ctx.capture_runtime_scope()
            try:
                valid = recipient == "room" and session_id == "scenario:initiative" and catalog.exists(session_id)
                if valid:
                    reader = catalog.reader(session_id)
                    valid = not reader.deleted and reader.attributes.visibility == "listed"
                yield valid
            finally:
                await lease.close()
    await ctx.provide(private_target_key("test"), Proof())
'''


def configure(sources: Path, workspace: Path) -> None:
    for name in ("sources", "conversation", "react", "reply_program", "tool_search",
                 "drift", "wake", "eventmail", "timer", "delivery_policy"):
        shutil.copytree(ROOT / "plugins" / name, sources / name, ignore=shutil.ignore_patterns("__pycache__"))
    delivery_sources(sources)
    (sources / "scenario_private_target").mkdir()
    (sources / "scenario_private_target/plugin.py").write_text(PROOF)
    save_config(workspace / "plugin-data/wake-builtin", {"enabled": True, "delivery": TARGET, "timezone": "UTC"})
    save_config(workspace / "plugin-data/drift-builtin", {"auto_propose": False})
    save_config(workspace / "plugin-data/context-builtin", {
        "prompt_sources": {"trusted-global": "scenario_private_target"}})


async def scenario(memory_source: Path | None) -> dict[str, object]:
    entered, release = asyncio.Event(), asyncio.Event()
    model_requests: list[dict] = []
    decision = ["share"]

    async def chat(payload):
        if "share_content" not in json.dumps(payload.get("tools", []), ensure_ascii=False):
            return None
        model_requests.append(payload)
        entered.set()
        await release.wait()
        if decision[0] == "failure":
            return 400, {"error": {"message": "content_filter: fixture rejection"}}
        if decision[0] == "invalid":
            return {"id": "wake-fixture", "object": "chat.completion", "model": "memory-fixture",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "No decision"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        name = "share_content" if decision[0] == "share" else "skip_content"
        arguments = ({"message": "最近咖啡喝得怎么样？", "items": []} if decision[0] == "share"
                     else {"reason": "现在没有合适的话题"})
        return {"id": "wake-fixture", "object": "chat.completion", "model": "memory-fixture",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "wake-share", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(arguments)}}]},
                    "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}

    async def exercise(host, append, workspace: Path, requests: dict) -> dict[str, object]:
        from plugins.default_memory.contract import DEFAULT_MEMORY
        from plugins.drift.contract import DRIFT_CHANGED
        from plugins.drift.store import DriftStore
        from plugins.eventmail.contract import EVENTMAIL_CONTEXT_SOURCE
        from plugins.gateway.contract import RpcMethod
        from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, CallRef
        from plugins.tools.contract import ALL_TOOLS, TOOLS, CallSource

        root = host.live_root
        assert root is not None
        memory = root.context.require(DEFAULT_MEMORY)
        await append(TARGET["session_id"], "coffee-user", "I prefer coffee without sugar.")
        await append("scenario:unrelated", "private-other", "Unrelated secret watermelon.", learning="excluded")
        assert not (await memory.consume())["failures"]
        with sqlite3.connect(workspace / "memory/default-v2.db") as db:
            identity = db.execute("SELECT id FROM memory_items WHERE fact LIKE ?", ("%coffee without sugar%",)).fetchone()[0]

        async def rpc(name: str, **params):
            method = root.context.require(RpcMethod.key("drift/initiative/" + name))
            return await method.call(method.params.model_validate(params))
        from plugins.wake.contract import WAKE_CONTACT
        from agent.plugin_composition.runtime_catalog import build_runtime_catalog
        catalog_before = build_runtime_catalog(root, host._active_generations)
        wake_state = [item for item in catalog_before["plugins"] if item["id"] in {"wake", "reply", "reply_program"}]
        assert root.context.get(WAKE_CONTACT) is not None, wake_state
        granted = await rpc("memory-consent", enabled=True, target=TARGET, request_id="grant", expected_version=0)
        assert granted["consent_version"] == 1
        database = workspace / "plugin-data/drift-builtin/drift.sqlite3"
        store = DriftStore(database)
        sent = workspace / "plugin-data/test_sender-builtin/sent.jsonl"

        async def propose(identity: str, version: int):
            entered.clear()
            release.clear()
            now = datetime.now(UTC)
            result = store.propose(identity, "1", {"kind": "conversation", "memory_assisted": True,
                "consent_version": version, "valid_until": (now + timedelta(minutes=5)).isoformat()},
                now, initiative_target=TARGET)
            assert result["inserted"] is True, result
            root.context.emit(DRIFT_CHANGED, None)
            await asyncio.wait_for(entered.wait(), 20)
            prompt = json.dumps(model_requests[-1], ensure_ascii=False)
            assert "Unrelated secret watermelon" not in prompt
            assert "TRUSTED_GLOBAL_POLICY" in prompt
            assert "TRUSTED_OWNER_PRIVATE_DATA" not in prompt and "UNSCOPED_CONTEXT_SECRET" not in prompt
            return prompt

        async def wait_state(identity: str, expected: str):
            async with asyncio.timeout(15):
                while True:
                    with sqlite3.connect(database) as db:
                        row = db.execute("SELECT status FROM proposals WHERE proposal_id=?", (identity,)).fetchone()
                    if row == (expected,):
                        return
                    await asyncio.sleep(.05)

        catalog = root.context.require(MESSAGE_CATALOG)
        before = catalog.reader(TARGET["session_id"]).snapshot()
        unrelated = catalog.reader("scenario:unrelated").snapshot()
        await wait_for_tool_clock()
        prompt = await propose("revoke-during-model", 1)
        assert "coffee without sugar" in prompt
        await rpc("memory-consent", enabled=False, request_id="revoke", expected_version=1)
        release.set()
        await wait_state("revoke-during-model", "invalidated")
        assert catalog.reader(TARGET["session_id"]).snapshot() == before
        assert not sent.exists() or not sent.read_text().strip()

        await rpc("memory-consent", enabled=True, target=TARGET, request_id="grant-again", expected_version=2)
        await propose("forget-during-model", 3)
        await append(TARGET["session_id"], "forget-user", "Forget my coffee preference.", finish=False)
        after_forget_request = catalog.reader(TARGET["session_id"]).snapshot()
        tools, bindings = root.context.require(TOOLS), root.context.require(BINDINGS)
        ref = next(ref for ref in root.context.require(ALL_TOOLS)().refs if ref.name == "forget_memory")
        binding = await tools.bind(ref, bindings)
        messages = root.context.require(MESSAGE_CATALOG).reader(TARGET["session_id"]).snapshot()
        source = CallSource(CallRef("wake-forget", 0), messages)
        async with tools.open(bindings.describe(binding, TOOLS)) as bound:
            prepared = await bound.prepare({"memory_ids": [identity], "reason": "User asked to forget"}, source)
            assert (await bound.invoke(source.effect_key, prepared)).outcome == "success"
        release.set()
        await wait_state("forget-during-model", "invalidated")
        assert catalog.reader(TARGET["session_id"]).snapshot() == after_forget_request
        assert not sent.exists() or not sent.read_text().strip()

        before_delivery = catalog.reader(TARGET["session_id"]).snapshot()
        await propose("fresh-opportunity", 3)
        with sqlite3.connect(workspace / "memory/default-v2.db") as db:
            fresh = json.loads(db.execute("SELECT value FROM receipts WHERE key LIKE ? ORDER BY rowid DESC LIMIT 1",
                                          ("initiative-snapshot:%",)).fetchone()[0])
        assert identity not in [item["id"] for item in fresh["presented"]]
        await rpc("pause", request_id="pause-during-model", expected_version=0)
        release.set()
        await wait_state("fresh-opportunity", "ready_for_delivery")
        assert not sent.exists() or not sent.read_text().strip()
        count_before_resume = len(model_requests)
        await rpc("resume", request_id="resume-after-model", expected_version=1)
        await wait_state("fresh-opportunity", "settled")
        assert len(model_requests) == count_before_resume
        assert sent.exists() and len(sent.read_text().splitlines()) == 1
        after = catalog.reader(TARGET["session_id"]).snapshot()
        assert all(row in after for row in before_delivery)
        assert catalog.reader("scenario:unrelated").snapshot() == unrelated
        decision[0] = "skip"
        await propose("skip-opportunity", 3)
        release.set()
        await wait_state("skip-opportunity", "await_change")
        assert catalog.reader(TARGET["session_id"]).snapshot() == after
        assert len(sent.read_text().splitlines()) == 1
        decision[0] = "invalid"
        await propose("invalid-decision", 3)
        release.set()
        await wait_state("invalid-decision", "await_change")
        attempts = workspace / "plugin-data/wake-builtin/wake.sqlite3"
        async with asyncio.timeout(10):
            while True:
                with sqlite3.connect(attempts) as db:
                    invalid = db.execute("SELECT a.outcome FROM wake_attempts a JOIN wake_runs r ON a.attempt_id=r.run_id "
                                         "WHERE r.decision_detail=? AND a.outcome!='checking'",
                                         ("Drift 没有提交唯一有效决定",)).fetchone()
                if invalid:
                    break
                await asyncio.sleep(.05)
        assert invalid == ("deferred",), invalid
        assert catalog.reader(TARGET["session_id"]).snapshot() == after
        assert len(sent.read_text().splitlines()) == 1
        decision[0] = "failure"
        await propose("model-failure", 3)
        release.set()
        await wait_state("model-failure", "invalidated")
        async with asyncio.timeout(10):
            while True:
                with sqlite3.connect(attempts) as db:
                    failed = db.execute("SELECT a.outcome FROM wake_attempts a JOIN wake_runs r ON a.attempt_id=r.run_id "
                                        "WHERE r.decision_detail=? AND a.outcome='failed'",
                                        ("Drift 没有提交唯一有效决定",)).fetchone()
                if failed:
                    break
                await asyncio.sleep(.05)
        assert failed == ("failed",), failed
        assert catalog.reader(TARGET["session_id"]).snapshot() == after
        assert len(sent.read_text().splitlines()) == 1
        # An active EventMail context has no scope proof; it must defer this
        # opportunity instead of entering the model or pretending to skip.
        event_source = root.context.require(EVENTMAIL_CONTEXT_SOURCE).bind("scenario")
        try:
            now = datetime.now(UTC)
            await event_source.report(event_id="unscoped", payload={"text": "UNSCOPED_EVENT_SECRET"},
                                      observed_at=now, expires_at=now + timedelta(minutes=5))
            result = store.propose("unscoped-context", "1", {"kind": "conversation", "memory_assisted": True,
                "consent_version": 3, "valid_until": (now + timedelta(minutes=5)).isoformat()},
                now, initiative_target=TARGET)
            assert result["inserted"] is True
            root.context.emit(DRIFT_CHANGED, None)
            await wait_state("unscoped-context", "invalidated")
        finally:
            event_source.close()
        assert len(model_requests) == count_before_resume + 3
        assert catalog.reader(TARGET["session_id"]).snapshot() == after
        with sqlite3.connect(workspace / "plugin-data/wake-builtin/wake.sqlite3") as db:
            reasons = db.execute("SELECT decision,decision_detail FROM wake_runs WHERE decision_detail LIKE ?",
                                 ("%unscoped_context%",)).fetchall()
        assert reasons == [("defer", "主动记忆材料不可用：unscoped_context")], reasons
        with sqlite3.connect(workspace / "sessions.db") as db:
            admissions = db.execute("SELECT count(*) FROM owner_records WHERE owner=? AND key LIKE ?",
                                    ("plugin:wake", "memory-recall:%")).fetchone()[0]
        assert admissions == 6, admissions
        return {"scoped_prompt": True, "revoked_and_forgotten_do_not_publish": True,
                "pause_resume_reuses_decision": True, "fresh_opportunity_delivered_once": True,
                "skip_does_not_publish": True, "invalid_decision_is_deferred": True,
                "nonretryable_model_failure_reported": True, "unscoped_event_defers": True,
                "durable_recall_admissions": admissions,
                "original_rows_preserved": True}

    with tempfile.TemporaryDirectory(prefix="dmw-") as directory:
        return await run(Path(directory), extension=exercise, configure=configure, chat_handler=chat,
                         memory_source=memory_source, setup_only=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-source", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(scenario(arguments.memory_source)), ensure_ascii=False))
