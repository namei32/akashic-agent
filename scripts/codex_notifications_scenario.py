"""真实插件、Gateway/SDK 和消息账本验证 Codex 通知；仅写隔离 workspace。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "sdk/python/src")]
from akashic_sdk import AsyncAkashic, RemoteError
from agent.plugin_composition import ServiceKey
from agent.plugins.manager import PluginManager
from plugins.content.contract import CONTENT
from plugins.ledger.contract import ContentPart, Input, Output, SESSION_ADMIN
from tests.test_default_reply import application

METHOD = "codex/notifications/submit"


def configure(sources: Path) -> None:
    for name in ("delivery", "delivery_policy", "akashic_sender", "gateway", "codex_notifications"):
        shutil.copytree(ROOT / "plugins" / name, sources / name,
                        ignore=shutil.ignore_patterns("__pycache__"))


async def run(directory: Path) -> dict[str, object]:
    directory.mkdir()
    home, workspace = directory / "home", directory / "workspace"
    home.mkdir()
    os.environ.update(HOME=str(home), AKASHIC_PLUGIN_HOME=str(home),
                      AKASHIC_PLUGIN_DISTRIBUTION="", AKASHIC_EXECUTION_MODE="local")
    endpoint = str(workspace / "akashic.sock")
    event = {"schema_version": 1, "bridge_id": "integration-wsl", "session_id": "codex-thread",
             "turn_id": "turn-one", "event": "Stop", "cwd": "/home/example/项目",
             "observed_at": datetime.now(UTC).isoformat(), "summary": "需要你确认下一步；这不代表任务成功。",
             "truncated": False}

    async with application(directory, replying=False, extra_sources=configure) as (log, host):
        root = host.live_root.context
        log.writer("akashic:wake", author="user", source="seed", body_types=(Input,),
                   content={"text": root.require(CONTENT).check_text}).append(
                       "old-message", Input((ContentPart("text", "保留原有 Wake 历史"),)))
        old = log.reader("akashic:wake").snapshot()
        async with await AsyncAkashic.connect(endpoint) as client:
            initial = await client.request("codex/notifications/status", {})
            assert initial["exists"] is False
            first = await client.request(METHOD, event)
            target = first["session_id"]
            assert first["status"] == "committed" and first["notification_status"] == "delivered"
            assert target != "akashic:wake" and log.reader(target).title == "Codex 动态"
            results = await asyncio.gather(*(client.request(METHOD, event) for _ in range(12)))
            assert all(result == first for result in results)
            assert len(log.reader(target).snapshot()) == 1
            message = log.reader(target).get(first["message_id"])
            assert isinstance(message.body, Output) and message.source == "codex_notifications"
            assert "本轮回复已停止" in message.body.parts[0].value
            for bad in ({**event, "summary": "换成另一份结果"}, {**event, "route": "akashic:wake"}):
                try:
                    await client.request(METHOD, bad)
                except RemoteError:
                    pass
                else:
                    raise AssertionError("接收端接受了冲突正文或额外选路字段")
            await root.require(SESSION_ADMIN).set_title(target, "我的 Codex 工作记录")
            second = await client.request(METHOD, {**event, "turn_id": "turn-two"})
            assert second["session_id"] == target and log.reader(target).title == "我的 Codex 工作记录"
            assert log.reader("akashic:wake").snapshot() == old
            assert root.require(ServiceKey("fixture.calls")) == []
            before = log.reader(target).snapshot()
            # 生产者不保存本次 ACK；重启后仍以同一事件重投。
        original_first = first

    restored = PluginManager([directory / "plugins"], workspace=workspace,
                             installed_cache_root=home / "cache")
    try:
        await restored.load_all()
        root = restored.live_root.context
        from plugins.ledger.contract import MESSAGE_CATALOG
        catalog = root.require(MESSAGE_CATALOG)
        async with await AsyncAkashic.connect(endpoint) as client:
            assert await client.request(METHOD, event) == original_first
            assert catalog.reader(target).snapshot() == before
            await root.require(SESSION_ADMIN).set_deleted(target, deleted=True)
            try:
                await client.request(METHOD, {**event, "turn_id": "turn-after-delete"})
            except RemoteError:
                pass
            else:
                raise AssertionError("后台通知复活了删除的会话")
            assert catalog.reader(target).deleted
            await root.require(SESSION_ADMIN).set_deleted(target, deleted=False)
            third = await client.request(METHOD, {**event, "turn_id": "turn-after-delete"})
            assert third["session_id"] == target
            assert catalog.reader(target).snapshot()[:len(before)] == before
            assert catalog.reader("akashic:wake").snapshot() == old
    finally:
        await restored.terminate_all()
    return {"real_gateway_and_sdk": True, "dedicated_session": target, "concurrent_dedupe": True,
            "conflicting_payload_rejected": True, "restart_after_lost_ack": True,
            "manual_title_preserved": True, "deleted_session_not_revived": True,
            "old_messages_preserved": True, "model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.output.resolve()))
    (args.output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
