"""Real timer, plugin composition, Wake, model decision and local-only delivery."""
from __future__ import annotations

import asyncio
import argparse
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.plugin_composition.config_input import save_config
from agent.plugin_composition import ServiceKey
from agent.plugin_composition.runtime_catalog import build_runtime_catalog
from agent.plugins.manager import PluginManager
from plugins.content.plugin import check_text
from plugins.eventmail.contract import EVENTMAIL_ALERT_SOURCE
from plugins.gateway.contract import RpcMethod
from plugins.drift.contract import DRIFT_WAKE_V3
from plugins.drift.store import DriftStore
from plugins.ledger.contract import ContentPart, Input, MESSAGE_CATALOG
from tests.support.delivery_sources import sources as delivery_sources
from tests.test_default_reply import application


async def run(folder: Path) -> None:
    workspace = folder / "workspace"
    target = {"channel": "test", "recipient": "room", "session_id": "test:room"}

    def configure(sources: Path) -> None:
        for name in ("drift", "wake", "eventmail", "timer", "delivery_policy"):
            shutil.copytree(ROOT / "plugins" / name, sources / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        delivery_sources(sources)
        save_config(workspace / "plugin-data/wake-builtin",
                    {"enabled": True, "delivery": target, "timezone": "UTC"})
        save_config(workspace / "plugin-data/drift-builtin",
                    {"auto_propose": False, "interval_seconds": 60, "start_hour": 0, "end_hour": 24})
        provider = sources / "test_provider/plugin.py"
        code = provider.read_text().replace('from contextlib import asynccontextmanager',
                                            'import asyncio\nfrom contextlib import asynccontextmanager').replace(
            "class Driver:\n        max_tool_schemas = None",
            "class Driver:\n        max_tool_schemas = None\n        wake_done = False\n        alert_done = False")
        code = code.replace(
            'if len(calls) == 1:\n                return LLMResponse(None, [ToolCall("provider-call", "write_evidence", {})])',
            '''if "share_content" in str(request.tools) and not self.wake_done:
                self.wake_done = True
                (ctx.data_root / "wake-entered").write_text("started")
                while not (ctx.data_root / "wake-release").exists():
                    await asyncio.sleep(0.05)
                return LLMResponse(None, [ToolCall("provider-call", "share_content", {"message": "最近过得怎么样？", "items": []})])
            if "share_alert" in str(request.tools) and not self.alert_done:
                self.alert_done = True
                return LLMResponse(None, [ToolCall("alert-call", "share_alert", {"message": "测试告警"})])''')
        code = code.replace('await ctx.provide(ServiceKey("fixture.calls"), calls)', '''
    from plugins.models.contract import MODEL_CATALOG
    from plugins.akasha.contract import SEMANTIC_INTEREST
    class Catalog:
        def snapshot(self):
            return SimpleNamespace(role_bindings={"default": "fixture"},
                                   model=lambda _id: SimpleNamespace(availability="available"))
    class Interest:
        def status(self): return None
        def decision(self): return True
        async def score(self, texts, *, cutoff): return tuple(0.5 for _ in texts)
    await ctx.provide(MODEL_CATALOG, Catalog())
    await ctx.provide(SEMANTIC_INTEREST, Interest())
    await ctx.provide(ServiceKey("fixture.calls"), calls)''')
        provider.write_text(code)

    async with application(folder, replying=True, extra_sources=configure) as (log, host), AsyncExitStack() as cleanup:
        root = host.live_root
        assert root is not None
        writer = log.writer(target["session_id"], author="user", source="conversation",
                            body_types=(Input,), content={"text": check_text})
        original = writer.append("scenario-original", Input((ContentPart("text", "最近在学习摄影。"),)))
        writer.expire()

        async def call(name: str, **params: object) -> dict[str, Any]:
            current = host.live_root
            assert current is not None
            method = current.context.require(RpcMethod.key("drift/initiative/" + name))
            value = await method.call(method.params.model_validate(params))
            assert isinstance(value, dict)
            return cast(dict[str, Any], value)

        config = await call("config")
        assert config["producer"]["auto_propose"] is False
        initial_catalog = build_runtime_catalog(root, host._active_generations)
        initial = cast(list[dict[str, Any]], initial_catalog["plugins"])
        assert not any(fiber["name"] == "drift/producer" for item in initial
                       if item["id"] == "drift" for fiber in item["composition"]["fibers"])
        result = await call("configure", request_id="enable-producer", expected_input=config["input_ref"],
                            auto_propose=True, interval_seconds=60, start_hour=0, end_hour=24)
        assert result is not None
        await host.wait_idle()
        enabled = await call("config")
        assert enabled["producer"]["auto_propose"] is True
        current = host.live_root
        assert current is not None
        catalog = build_runtime_catalog(current, host._active_generations)
        relevant = [item for item in cast(list[dict[str, Any]], catalog["plugins"])
                    if item["id"] in {"drift", "wake", "timer"}]
        fibers = {fiber["name"]: fiber for item in relevant for fiber in item["composition"]["fibers"]}
        assert fibers["drift/producer"]["state"] == "active", fibers["drift/producer"]
        assert fibers["wake/function"]["state"] == "active", fibers["wake/function"]
        before = tuple(log.reader(target["session_id"]).snapshot())
        assert original in before
        sent = workspace / "plugin-data/test_sender-builtin/sent.jsonl"
        database = workspace / "plugin-data/drift-builtin/drift.sqlite3"
        model_data = workspace / "plugin-data/test_provider-builtin"
        async with asyncio.timeout(85):
            while not (model_data / "wake-entered").exists():
                await asyncio.sleep(0.2)
        await call("pause", request_id="pause-during-model", expected_version=0)
        (model_data / "wake-release").touch()
        attempts = workspace / "plugin-data/wake-builtin/wake.sqlite3"
        async with asyncio.timeout(10):
            while True:
                with sqlite3.connect(attempts) as db:
                    outcome = db.execute("SELECT outcome FROM wake_attempts WHERE owner='drift'").fetchone()
                if outcome is not None and outcome[0] == "deferred":
                    break
                await asyncio.sleep(0.1)
        assert not sent.exists() or not sent.read_text().strip()
        alert = current.context.require(EVENTMAIL_ALERT_SOURCE).bind("scenario")
        try:
            await alert.report(event_id="alert", payload={"text": "需要注意的测试事件"}, observed_at=datetime.now(UTC))
        finally:
            alert.close()
        async with asyncio.timeout(10):
            while not sent.exists() or len(sent.read_text().splitlines()) < 1:
                await asyncio.sleep(0.1)
        await host.terminate_all()
        host = PluginManager([folder / "plugins"], workspace=workspace, installed_cache_root=folder / "home/cache")
        cleanup.push_async_callback(host.terminate_all)
        await host.load_all()
        restored_root = host.live_root
        assert restored_root is not None
        log = cast(Any, restored_root.context.require(MESSAGE_CATALOG))._log
        calls = restored_root.context.require(ServiceKey("fixture.calls"))
        assert calls == []
        await call("resume", request_id="resume-after-alert", expected_version=1)
        async with asyncio.timeout(10):
            while len(sent.read_text().splitlines()) < 2:
                await asyncio.sleep(0.1)
        entries = [json.loads(line) for line in sent.read_text().splitlines()]
        assert len(entries) == 2 and all(entry[1] == target["recipient"] for entry in entries)
        assert len({entry[2] for entry in entries}) == 2
        assert calls == []  # Resume used the saved model decision, not a second call.
        delivered = [log.reader(target["session_id"]).get(entry[2]) for entry in entries]
        assert all(message is not None and message.source == "wake" for message in delivered)
        assert "测试告警" in str(delivered[0].body) and "最近过得怎么样？" in str(delivered[1].body)
        async with asyncio.timeout(10):
            while True:
                with sqlite3.connect(database) as db:
                    proposal = db.execute("SELECT status, payload_json FROM proposals WHERE proposal_id LIKE 'initiative:%'").fetchone()
                if proposal is not None and proposal[0] == "settled":
                    break
                await asyncio.sleep(0.1)
        assert datetime.fromisoformat(json.loads(proposal[1])["valid_until"]) > datetime.now(UTC)
        # A captured opportunity that expires before execution cannot be sent later.
        store = DriftStore(database)
        now = datetime.now(UTC)
        stale_due = now - timedelta(seconds=10)
        stale = store.propose("scenario:expired", "1", {"kind": "conversation",
            "valid_until": (now - timedelta(seconds=5)).isoformat()}, stale_due, initiative_target=target)
        current = host.live_root
        assert current is not None
        drift = current.context.require(DRIFT_WAKE_V3)
        snapshot = await drift.snapshot(now, target=target)
        proposals = cast(tuple[dict[str, Any], ...], snapshot["proposals"])
        assert not any(item["ref"]["proposal_id"] == "scenario:expired" for item in proposals)
        accepted = {"session_id": "wake:expired", "turn_id": "wake-input:expired"}
        ref = cast(dict[str, object], stale["ref"])
        assert (await drift.select(ref, accepted, now, target=target))["reason"] == "expired"
        selected = await drift.select(ref, accepted, stale_due, target=target)
        assert selected["selected"] is True
        token = cast(str, selected["selection_token"])
        assert (await drift.transition(token, "ready_for_delivery"))["changed"] is True
        async with drift.send_start(accepted, target) as reason:
            assert reason == "expired:" + token
        assert (await drift.transition(token, "invalidated"))["changed"] is True
        after = tuple(log.reader(target["session_id"]).snapshot())
        assert all(message in after for message in before)
        with sqlite3.connect(workspace / "sessions.db") as db:
            attributes = [json.loads(row[0]) for row in db.execute(
                "SELECT attributes FROM sessions WHERE key LIKE 'wake:%'")]
        assert attributes and all(value == {"learning": "excluded", "visibility": "internal"} for value in attributes)
        print(json.dumps({"explicit_opt_in": True, "real_timer_seconds": 60, "wake_settled": True,
                          "pause_blocks_send": True, "alert_while_paused": True,
                          "restart_resume_original_send": True, "expired_opportunity_blocked": True,
                          "local_delivery_count": len(entries),
                          "original_messages_preserved": True}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is not None:
        args.output.mkdir(parents=True, exist_ok=False)
        asyncio.run(run(args.output))
    else:
        with tempfile.TemporaryDirectory(prefix="drift-producer-") as temporary:
            asyncio.run(run(Path(temporary)))
