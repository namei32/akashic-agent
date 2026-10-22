"""在隔离插件栈验证日报分流、原发送回执恢复及用户追问；不连接游戏或外部账号。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.plugin_composition.config_input import save_config
from agent.plugins.manager import PluginManager
from plugins.channels.contract import CHANNEL_INPUT_V2, ChannelInboundMessage
from plugins.content.plugin import check_text
from plugins.ledger.contract import ContentPart, Output
from plugins.lol_tracker.settings import RUNTIME, SETTINGS
from plugins.conversation.contract import TASK_SESSIONS
from tests.support.delivery_sources import sources as delivery_sources
from tests.test_default_reply import application


async def run(root: Path):
    home, workspace, collector = root / 'home', root / 'workspace', root / 'collector'
    home.mkdir(); collector.mkdir()
    os.environ.update(HOME=str(home), AKASHIC_PLUGIN_HOME=str(home), AKASHIC_PLUGIN_DISTRIBUTION='')
    stamp = datetime.now(UTC)
    (collector / 'snapshot.json').write_text(json.dumps({
        'schema_version': 1, 'status': 'offline', 'observed_at': stamp.isoformat(), 'players': []}))
    config = {'enabled': True, 'players': [{'riot_id': 'scenario#0000', 'platform': 'HN1'}],
              'bridge_directory': str(collector), 'analysis_enabled': False, 'daily_time': '23:59'}

    def configure(sources):
        for name in ('delivery_policy', 'eventmail', 'lol_tracker'):
            shutil.copytree(ROOT / 'plugins' / name, sources / name, ignore=shutil.ignore_patterns('__pycache__'))
        delivery_sources(sources)
        sender = sources / 'test_sender/plugin.py'
        code = sender.read_text().replace('name="test", idempotent=True, open=open', 'name="akashic", idempotent=True, open=open')
        code = code.replace('async def send(self, key, address, message):', '''async def send(self, key, address, message):
            if (ctx.data_root / "reject").exists():
                return SendResult(status="rejected", error="scenario receiver offline")''')
        sender.write_text(code)
        save_config(workspace / 'plugin-data/lol_tracker-builtin', config)

    def sent():
        path = workspace / 'plugin-data/test_sender-builtin/sent.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def messages():
        with sqlite3.connect(workspace / 'sessions.db') as db:
            return set(db.execute('SELECT * FROM messages').fetchall())

    async with application(root, replying=True, extra_sources=configure) as (log, host):
        context = host._active_generations['lol_tracker'].fiber.context
        log.writer('akashic:wake', author='assistant', source='seed', body_types=(Output,),
            content={'text': check_text}).append('old-wake-message', Output((ContentPart('text', 'old wake history'),), 'complete'))
        old_wake = log.reader('akashic:wake').snapshot()
        # HTTP/UI 等外部消费者没有 LoL 的 OwnerCall；公开入口自己取得生命周期范围。
        public_runtime = host.live_root.context.require(RUNTIME)
        assert (await public_runtime.status())['running']
        first = await public_runtime.manual('first', 1)
        async with context.runtime_scope():
            runtime = context.require(RUNTIME)
            assert first['status'] == 'delivered' and first['session_id'] != 'akashic:wake'
            target = first['session_id']
            second = await runtime.manual('second', 1)
            assert second['session_id'] == target
            assert log.reader(target).title == '英雄联盟战绩'
            assert log.reader('akashic:wake').snapshot() == old_wake
            assert len([m for m in log.reader(target).snapshot() if m.source == 'lol_tracker']) == 2
            settings = context.require(SETTINGS)
            configuration = await settings.read()
            assert any(item['session_id'] == target for item in configuration['targets'])
            try:
                await settings.save('invalid-target', configuration['input_ref'],
                    {'enabled': True, 'report_session_id': 'akashic:missing'})
            except ValueError:
                pass
            else:
                raise AssertionError('Settings accepted a nonexistent destination')
            assert (await settings.read())['input_ref'] == configuration['input_ref']

            # 用户在报告会话里通过真实渠道接纳追问，普通对话仍可继续。
            await host.live_root.context.require(CHANNEL_INPUT_V2)(target, 'followup', ChannelInboundMessage(
                'akashic', 'user', target[8:], '解释这份报告', datetime.now(UTC), {}))
            async with asyncio.timeout(30):
                while not any(isinstance(m.body, Output) and m.body.finish == 'complete' and m.source == 'conversation'
                              for m in log.reader(target).snapshot()):
                    await asyncio.sleep(.02)

            # 旧 EventMail 已接纳但生产者丢失回执；补回执不得改走新会话。
            facts = {'title': 'legacy', 'start': stamp.isoformat(), 'end': stamp.isoformat(), 'players': []}
            runtime.store.claim('legacy', facts)
            runtime.store.finish('legacy', 'legacy report')
            legacy = runtime.store.report('legacy')
            await runtime.report_alert(event_id='legacy', payload={'title': '英雄联盟战绩汇总', 'text': legacy['body'],
                'report_id': 'legacy', 'source': 'lol_tracker', 'category': 'subscribed_daily_report'},
                observed_at=datetime.fromisoformat(legacy['created_at']))
            async with runtime.lock:
                await runtime.publish_pending()
            assert runtime.store.report('legacy')['published'] == 1
            with sqlite3.connect(workspace / 'plugin-data/eventmail-builtin/eventmail.sqlite3') as db:
                assert db.execute('SELECT COUNT(*) FROM mail_envelopes').fetchone()[0] == 1

            # 外部接收端明确拒绝时不记成功；用户重复同一手动请求可重试原消息。
            reject = workspace / 'plugin-data/test_sender-builtin/reject'
            reject.touch()
            try:
                await runtime.manual('rejected', 1)
            except ValueError as error:
                assert 'offline' in str(error)
            else:
                raise AssertionError('Rejected delivery was reported as successful')
            assert runtime.store.report('manual:rejected')['published'] == 0
            reject.unlink()
            assert (await runtime.manual('rejected', 1))['status'] == 'delivered'

            # 发送成功、日报尚未保存 ACK 时重启；同时修改未来报告的目标。
            report = await runtime.make_report('manual:lost-ack', stamp - timedelta(days=1), stamp, 'recovery')
            receipt = await runtime.delivery.send(report)
            assert receipt['status'] == 'delivered' and runtime.store.report(report['id'])['published'] == 0
            recovery_message = receipt['message_id']
            sessions = context.require(TASK_SESSIONS)
            other = sessions.allocate('scenario-alternative')
            await sessions.ensure(other['session_id'], '其他战绩会话')
            before = messages()
            config['report_session_id'] = other['session_id']

    save_config(workspace / 'plugin-data/lol_tracker-builtin', config)
    restored = PluginManager([root / 'plugins'], workspace=workspace, installed_cache_root=home / 'cache')
    try:
        await restored.load_all()
        context = restored._active_generations['lol_tracker'].fiber.context
        async with context.runtime_scope():
            runtime = context.require(RUNTIME)
            async with asyncio.timeout(30):
                while not runtime.store.report('manual:lost-ack')['published']:
                    await asyncio.sleep(.02)
            assert runtime.store.destination('manual:lost-ack')['session_id'] == target
            assert sum(row[2] == recovery_message for row in sent()) == 1
            assert (await runtime.manual('after-move', 1))['session_id'] == other['session_id']
            assert before.issubset(messages())
            assert len({row[0] for row in sent()}) == len(sent())
            settings = context.require(SETTINGS)
            configuration = await settings.read()
            await settings.save('restore-default-target', configuration['input_ref'],
                {'enabled': True, 'report_session_id': ''})
        await restored.wait_idle()
        context = restored._active_generations['lol_tracker'].fiber.context
        async with context.runtime_scope():
            runtime = context.require(RUNTIME)
            assert runtime.config.report_session_id == ''
            assert (await runtime.manual('after-settings-save', 1))['session_id'] == target
    finally:
        await restored.terminate_all()
    return {'dedicated_session': target, 'repeat_uses_same_session': True, 'followup': True,
            'legacy_handoff': True, 'rejected_not_success': True, 'restart_no_duplicate': True,
            'pending_keeps_original_target': True, 'new_reports_use_selected_target': True,
            'old_messages_preserved': True, 'settings_validated_and_applied': True, 'external_sends': len(sent())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    result = asyncio.run(run(directory))
    (directory / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
