"""Real isolated plugin host, private Web proof, Wake, model HTTP and delivery."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.plugin_composition import ServiceKey
from agent.plugin_composition.config_input import save_config
from default_memory_scenario import run
from drift_memory_wake_scenario import configure as base_configure, wait_for_tool_clock

TARGET = {"channel": "akashic", "recipient": "destination", "session_id": "akashic:destination"}
POLICY = {"enabled": True, "revision": 1, "excluded": ["akashic:excluded"], "max_sessions": 3, "fixed_session": TARGET["session_id"]}


def configure(sources, workspace):
    base_configure(sources, workspace)
    proof = sources / "scenario_private_target/plugin.py"
    script = proof.read_text()
    start = script.index("    class Proof:")
    proof.write_text(script[:start] + "    from plugins.akashic_clients.private_target import PrivateTarget\n"
                     "    await ctx.provide(private_target_key('akashic'), PrivateTarget(ctx))\n")
    sender = sources / "test_sender/plugin.py"
    sender.write_text(sender.read_text().replace('name="test",', 'name="akashic",'))
    probe = sources / "memory_probe/plugin.py"
    script = probe.read_text().replace("ContentPart\n", "ContentPart, ContentReferences\n")
    script = script.replace('content = {"text": ctx.require(CONTENT).check_text}',
        'content = {"text": ctx.require(CONTENT).check_text, "channel.origin": lambda part: ContentReferences()}')
    script = script.replace('Input((ContentPart("text", message),))',
        'Input((ContentPart("text", message), ContentPart("channel.origin", '
        '{"channel": "akashic", "chat_id": session.removeprefix("akashic:"), "sender": "web"})))')
    script = script.replace('    await ctx.provide(ServiceKey("scenario.append"),', '''    background = writers.bind(ctx, author="wake", source="wake", body_types=(Output,), content=content)
    bot_input = writers.bind(ctx, author="automation", source="conversation", body_types=(Input,), content=content)
    async def noise(session):
        admission.ensure(ctx, session, SessionAttributes())
        for i in range(220):
            background(session).append(session + ":noise:" + str(i), Output((ContentPart("text", "BACKGROUND_SECRET"),), "complete"))
        bot_input(session).append(session + ":bot", Input((ContentPart("text", "AUTOMATION_SECRET"),)))
    await ctx.provide(ServiceKey("scenario.noise"), ctx.entrypoint(noise))
    await ctx.provide(ServiceKey("scenario.append"),''')
    probe.write_text(script)
    save_config(workspace / "plugin-data/wake-builtin", {"enabled": True, "delivery": TARGET,
                "timezone": "UTC", "active_sources": POLICY})


def instrument_boundaries(sources, workspace, provider):
    # Keep real admission, preparation and guards; change only the returned material.
    with (sources / provider / "initiative.py").open("a") as stream:
        stream.write(f"""
from pathlib import Path as _Path
import json as _json
_original_prepare = Initiative.prepare
async def _fixture_prepare(self, *args, **kwargs):
    result = await _original_prepare(self, *args, **kwargs)
    override = _Path({str(workspace / 'fixture-recall.json')!r})
    if override.exists():
        return {{**result, **_json.loads(override.read_text())}}
    return result
Initiative.prepare = _fixture_prepare
""")
    with (sources / "eventmail/plugin.py").open("a") as stream:
        stream.write(f"""
from pathlib import Path as _Path
_original_context = _WakeServices.active_context
async def _observed_context(self, now):
    _Path({str(workspace / 'eventmail-context-read')!r}).touch()
    return await _original_context(self, now)
_WakeServices.active_context = _observed_context
""")


async def scenario(provider="default_memory"):
    entered, release = asyncio.Event(), asyncio.Event()
    prompts = []

    async def chat(payload):
        if "share_content" not in json.dumps(payload.get("tools", [])):
            return None
        prompts.append(json.dumps(payload, ensure_ascii=False))
        entered.set()
        await release.wait()
        return {"id": "fixture", "object": "chat.completion", "model": "memory-fixture",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "share", "type": "function", "function": {
                    "name": "share_content", "arguments": json.dumps({"message": "自然续聊。", "items": []})}}]},
                "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}

    async def exercise(host, append, workspace, requests):
        from plugins.drift.contract import DRIFT_CHANGED
        from plugins.drift.store import DriftStore
        from plugins.gateway.contract import RpcMethod
        from plugins.ledger.contract import MESSAGE_CATALOG, SESSION_ADMIN
        from plugins.wake.request import read_request
        root = host.live_root
        assert root is not None
        catalog = root.context.require(MESSAGE_CATALOG)
        await append(TARGET["session_id"], "target-user", "Destination only.")
        for day in range(3):
            await append("akashic:active", "active-" + str(day), "I prefer coffee without sugar.")
        await append("akashic:third", "third-user", "A third conversation.")
        for i in range(12):
            await append("akashic:burst", "burst-" + str(i), "ONE_DAY_BURST")
        # Historical timestamps are fixture construction in this disposable DB.
        with sqlite3.connect(workspace / "sessions.db") as db:
            for day in range(3):
                db.execute("UPDATE messages SET ts=? WHERE id IN (?,?)",
                    ((datetime.now(UTC) - timedelta(days=day)).isoformat(),
                     "active-" + str(day), "active-" + str(day) + ":reply"))
        await root.context.require(ServiceKey("scenario.noise"))("akashic:active")
        await append("akashic:excluded", "excluded-user", "EXCLUDED_SECRET")
        await append("akashic:project", "project-user", "PROJECT_SECRET", scope=(("project", "private"),))
        # More than the old tail cap of background rows must not hide the user.
        before = {sid: catalog.reader(sid).snapshot() for sid in catalog.snapshot_heads(visibility="listed")}
        store = DriftStore(workspace / "plugin-data/drift-builtin/drift.sqlite3")
        sent = workspace / "plugin-data/test_sender-builtin/sent.jsonl"
        database = workspace / "plugin-data/drift-builtin/drift.sqlite3"

        def notifications():
            return [json.loads(line) for line in sent.read_text().splitlines()
                    if json.loads(line)[2].startswith("wake-notification:")]

        async def rpc(name, **fields):
            method = root.context.require(RpcMethod.key("drift/initiative/" + name))
            return await method.call(method.params.model_validate(fields))

        async def wait_state(identity, expected):
            async with asyncio.timeout(25):
                while True:
                    with sqlite3.connect(database) as db:
                        state = db.execute("SELECT status FROM proposals WHERE proposal_id=?", (identity,)).fetchone()
                    if state == (expected,):
                        return
                    await asyncio.sleep(.03)

        async def submit(identity, *, memory=False):
            entered.clear()
            release.clear()
            now = datetime.now(UTC)
            payload: dict[str, object] = {"kind": "conversation", "valid_until": (now + timedelta(minutes=5)).isoformat()}
            if memory:
                payload.update(memory_assisted=True, consent_version=1)
            result = store.propose(identity, "1", payload, now, initiative_target=TARGET)
            assert result["inserted"] is True
            root.context.emit(DRIFT_CHANGED, None)

        async def propose(identity, *, memory=False):
            await submit(identity, memory=memory)
            await asyncio.wait_for(entered.wait(), 25)
            prompt = prompts[-1]
            assert "coffee without sugar" in prompt
            assert all(secret not in prompt for secret in ("EXCLUDED_SECRET", "PROJECT_SECRET", "BACKGROUND_SECRET",
                "AUTOMATION_SECRET", "TRUSTED_OWNER_PRIVATE_DATA", "UNSCOPED_CONTEXT_SECRET"))
            assert "TRUSTED_GLOBAL_POLICY" in prompt
            for sid in catalog.snapshot_heads(prefix="wake:", visibility="internal"):
                request = read_request(catalog.reader(sid).snapshot())
                history = json.loads(request.history)
                assert sum(len(json.dumps(row, ensure_ascii=False)) for row in history["recent_conversation"]) <= 6000
            return prompt

        await wait_for_tool_clock()
        await propose("ordinary")
        release.set()
        await wait_state("ordinary", "settled")
        assert len(notifications()) == 1, sent.read_text()
        # Exact original records, identities and order survive new target messages.
        for sid, rows in before.items():
            after = catalog.reader(sid).snapshot()
            assert tuple(m for m in after if m.message_id in {r.message_id for r in rows}) == rows
        first = [read_request(catalog.reader(sid).snapshot()) for sid in catalog.snapshot_heads(prefix="wake:")]
        assert first[-1].source_evidence[0].session_id == "akashic:active"
        assert first[-1].target.session_id == TARGET["session_id"]
        ids = [e.session_id for e in first[-1].source_evidence]
        assert len(ids) == 4 and set(ids) == {"akashic:active", "akashic:burst", "akashic:third", TARGET["session_id"]}, ids
        first_ids = first[-1].source_evidence[0].message_ids
        # Same real evidence is not counted twice when a second opportunity runs.
        await rpc("memory-consent", enabled=True, target=TARGET, request_id="grant", expected_version=0)
        marker = workspace / "eventmail-context-read"
        fixture = workspace / "fixture-recall.json"
        fixture.write_text(json.dumps({"text": "", "references": [], "status": "empty"}))
        await propose("empty-recall", memory=True)
        release.set()
        await wait_state("empty-recall", "settled")
        assert len(notifications()) == 2
        for identity, refs in (("outside", ["active-0", "excluded-user"]), ("unattributed", [])):
            fixture.write_text(json.dumps({"text": "RECALL_CANARY", "references": refs, "status": "ok"}))
            count = len(prompts)
            target_rows = catalog.reader(TARGET["session_id"]).snapshot()
            await submit(identity, memory=True)
            release.set()
            async with asyncio.timeout(25):
                while not entered.is_set():
                    with sqlite3.connect(database) as db:
                        if db.execute("SELECT status FROM proposals WHERE proposal_id=?", (identity,)).fetchone() == ("invalidated",):
                            break
                    await asyncio.sleep(.03)
            assert len(prompts) == count, f"{identity}: recall reached model; EventMail read={marker.exists()}"
            assert len(notifications()) == 2 and catalog.reader(TARGET["session_id"]).snapshot() == target_rows
        fixture.unlink()
        await propose("memory", memory=True)
        latest = [read_request(catalog.reader(sid).snapshot()) for sid in catalog.snapshot_heads(prefix="wake:")]
        assert any(r.memory_source_session == "akashic:active" and r.source_evidence[0].message_ids == first_ids for r in latest)
        # Delete the selected source while model I/O is pending: no publication/send.
        await root.context.require(SESSION_ADMIN).set_deleted("akashic:active", deleted=True)
        release.set()
        await wait_state("memory", "invalidated")
        assert len(notifications()) == 2, sent.read_text()
        assert len(catalog.reader(TARGET["session_id"]).snapshot()) == len(before[TARGET["session_id"]]) + 2
        await root.context.require(SESSION_ADMIN).set_deleted("akashic:active", deleted=False)
        await propose("policy-revoked")
        assert not marker.exists(), "auto capture called EventMail.active_context"
        # Apply a real owner configuration generation while model I/O is pending.
        current = host.read_config_input("wake")
        await host.apply_config_input("wake", "revoke-auto-sources", current["input_ref"],
            {"enabled": True, "delivery": TARGET, "timezone": "UTC", "memory_provider": provider,
             "active_sources": {"enabled": False, "revision": 2}})
        release.set()
        async with asyncio.timeout(30):
            while host.read_config_update("wake", "revoke-auto-sources")["state"] != "active":
                await asyncio.sleep(.03)
        await wait_state("policy-revoked", "invalidated")
        assert len(notifications()) == 2, sent.read_text()
        # Observe the notification count at load_all return, not completion of async recovery.
        await host.terminate_all()
        from agent.plugins.manager import PluginManager
        restarted = PluginManager([workspace.parent / "sources"], workspace=workspace,
                                  installed_cache_root=workspace.parent / "home/cache")
        try:
            await restarted.load_all()
            assert len(notifications()) == 2, sent.read_text()
        finally:
            await restarted.terminate_all()
        return {"ordinary_auto_source_delivered": True, "memory_uses_selected_source": True,
            "restart_load_return_no_additional_send": True, "empty_recall_allowed": True,
            "unattributed_and_outside_blocked_before_model": True, "auto_eventmail_context_unread": True, "multi_day_activity_beats_one_day_burst": True,
            "config_revoke_during_model_blocks_publication": True,
            "background_tail_does_not_hide_user": True, "unscoped_material_excluded": True,
            "excluded_and_project_sources_hidden": True, "deletion_before_publish_blocks_send": True,
            "original_messages_preserved": True, "repeated_capture_same_evidence": True}

    def configure_provider(sources, workspace):
        configure(sources, workspace)
        instrument_boundaries(sources, workspace, provider)
        if provider == "akasha":
            save_config(workspace / "plugin-data/akasha-builtin", {"memory_enabled": True})
            save_config(workspace / "plugin-data/default_memory-builtin", {"enabled": False})
            save_config(workspace / "plugin-data/wake-builtin", {"enabled": True, "delivery": TARGET,
                "timezone": "UTC", "active_sources": POLICY, "memory_provider": provider})
    with tempfile.TemporaryDirectory(prefix="active-sources-") as directory:
        return await run(Path(directory), extension=exercise, configure=configure_provider, chat_handler=chat, setup_only=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("akasha", "default_memory"), default="akasha")
    print(json.dumps(asyncio.run(scenario(parser.parse_args().provider)), ensure_ascii=False))
