from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.plugin_composition import Context
from plugins.models.contract import CHAT_MODELS, ModelError, ModelRequest
from core.common.file_io import run_file_io

from .analysis import ANALYSIS_PROMPT, metrics, render
from .config import Config
from .store import Store, encoded


class Runtime:
    def __init__(self, ctx: Context, config: Config, store: Store):
        self.ctx, self.config, self.store = ctx, config, store
        self.report_alert: Callable[..., Awaitable[Mapping[str, object]]] | None = None
        self.running = False
        self.error = ""
        self.lock = asyncio.Lock()

    def bridge_config(self, enabled: bool):
        if not self.config.bridge_directory:
            return
        root = Path(self.config.bridge_directory)
        root.mkdir(parents=True, exist_ok=True)
        target = root / "watchlist.json"
        temporary = target.with_suffix(".pending")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(encoded({"enabled": enabled, "players": [p.model_dump() for p in self.config.players]}))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    async def ingest(self):
        def read():
            path = Path(self.config.bridge_directory) / "snapshot.json"
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("战绩采集文件过大")
            return json.loads(path.read_text(encoding="utf-8-sig"))
        await run_file_io(lambda: self.store.ingest(read()))

    async def status(self) -> dict:
        snapshot = await run_file_io(self.store.latest)
        collector_status = snapshot.get("status", "waiting") if snapshot else "waiting"
        if snapshot and datetime.now(UTC) - datetime.fromisoformat(snapshot["observed_at"]) > timedelta(minutes=15):
            collector_status = "stale"
        return {"running": self.running, "error": self.error, "push_available": self.report_alert is not None,
                "observed_at": snapshot.get("observed_at") if snapshot else None,
                "collector_status": collector_status,
                "collector_error": snapshot.get("error") if snapshot else None,
                "players": [{k: p.get(k) for k in ("riot_id", "platform", "status", "error")}
                            for p in snapshot.get("players", [])] if snapshot else [],
                "reports": await run_file_io(self.store.reports)}

    async def facts(self, start: datetime, end: datetime, title: str) -> dict:
        snapshot = await run_file_io(self.store.latest)
        source_players = snapshot.get("players", []) if snapshot else []
        observed = datetime.fromisoformat(snapshot["observed_at"]) if snapshot else None
        players = []
        for watch in self.config.players:
            source = next((p for p in source_players if p["platform"] == watch.platform
                           and p["riot_id"].casefold() == watch.riot_id.casefold()), None)
            rows = await run_file_io(partial(self.store.matches, watch.key, start, end))
            note = "数据可能不完整：客户端离线或尚未完成采集"
            if source and source.get("status") == "ok" and observed:
                oldest = source.get("oldest_started_at")
                covered = source.get("history_exhausted") or (oldest and datetime.fromisoformat(oldest) <= start)
                if observed >= end and covered:
                    note = "已覆盖统计区间"
                else:
                    note = "数据可能不完整：最近一次采集尚未覆盖整个统计区间"
            players.append({"riot_id": watch.riot_id, "server": "艾欧尼亚" if watch.platform == "HN1" else watch.platform,
                            "observed_at": observed.astimezone(ZoneInfo(self.config.timezone)).isoformat() if observed else None,
                            "coverage_note": note, "summary": metrics(rows)})
        return {"title": title, "start": start.isoformat(), "end": end.isoformat(), "players": players}

    async def make_report(self, identity: str, start: datetime, end: datetime, title: str) -> dict:
        async with self.lock:
            previous = await run_file_io(lambda: self.store.report(identity))
            if previous:
                return previous
            facts = await self.facts(start, end, title)
            claimed = await run_file_io(lambda: self.store.claim(identity, facts))
            if not claimed:
                existing = await run_file_io(lambda: self.store.report(identity))
                if existing is None:
                    raise RuntimeError("报告提交身份丢失")
                return existing
            body, error = render(facts), None
            if self.config.analysis_enabled and any(p["summary"]["modes"] for p in facts["players"]):
                try:
                    async with asyncio.timeout(180):
                        async with self.ctx.require(CHAT_MODELS).independent_execution() as execution:
                            response = await execution.chat("fast").complete(ModelRequest(
                                messages=[{"role": "user", "content": encoded(facts)}],
                                system_prompt=ANALYSIS_PROMPT, max_output_tokens=1800))
                    if not response.content or response.tool_calls:
                        raise ValueError("分析模型未返回有效正文")
                    body += "\n\n赛后分析\n" + response.content.strip()
                except (RuntimeError, ValueError, TimeoutError) as exc:
                    if isinstance(exc, RuntimeError) and ModelError.read(exc) is None:
                        raise
                    error = type(exc).__name__
                    body += "\n\nAI 分析暂不可用，以上保留可核验的战绩数据。"
            await run_file_io(lambda: self.store.finish(identity, body, error))
            saved = await run_file_io(lambda: self.store.report(identity))
            if saved is None:
                raise RuntimeError("报告未持久保存")
            return saved

    async def publish_pending(self):
        publish = self.report_alert
        if publish is None:
            return
        for report in await run_file_io(self.store.pending):
            if report["body"] is None:
                # A previous model request may have completed remotely. Do not silently bill it again.
                facts = json.loads(report["facts"])
                await run_file_io(partial(self.store.finish, report["id"], render(facts) +
                    "\n\n上次 AI 分析被中断，结果未确认；可手动生成新的分析。", "analysis_interrupted"))
                report = await run_file_io(partial(self.store.report, report["id"]))
                if report is None:
                    raise RuntimeError("待发送报告丢失")
            receipt = await publish(event_id=report["id"],
                payload={"title": "英雄联盟战绩汇总", "text": report["body"],
                         "report_id": report["id"], "source": "lol_tracker", "category": "subscribed_daily_report"},
                observed_at=datetime.fromisoformat(report["created_at"]))
            await run_file_io(partial(self.store.published, report["id"], dict(receipt)))

    async def tick(self, now: datetime):
        try:
            await self.ingest()
            self.error = ""
        except (OSError, ValueError) as exc:
            self.error = f"无法更新采集数据：{type(exc).__name__}"
        due, start, end = self.config.daily_period(now)
        started = await run_file_io(self.store.started_at)
        if started <= due <= now:
            await self.make_report("daily:" + due.date().isoformat(), start, end,
                                   start.strftime("%Y-%m-%d") if self.config.window == "previous_day" else "过去 24 小时")
        async with self.lock:
            await self.publish_pending()

    async def follow(self):
        self.running = True
        try:
            await run_file_io(lambda: self.bridge_config(True))
            while True:
                try:
                    async with self.ctx.runtime_scope():
                        await self.tick(datetime.now(UTC))
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    self.error = str(exc)[:300] or type(exc).__name__
                await asyncio.sleep(60)
        finally:
            self.running = False

    async def manual(self, request_id: str, days: int) -> dict:
        if not self.running:
            raise ValueError("请先开启英雄联盟战绩插件")
        try:
            await self.ingest()
        except (OSError, ValueError):
            self.error = "本次使用缓存；采集数据未能更新"
        end = datetime.now(ZoneInfo(self.config.timezone))
        # Include only completed history already observable at capture time.
        latest = await run_file_io(self.store.latest)
        if latest and latest.get("status") == "ok":
            end = min(end, datetime.fromisoformat(latest["observed_at"]).astimezone(end.tzinfo))
        report = await self.make_report("manual:" + request_id, end-timedelta(days=days), end, f"近 {days} 天")
        async with self.lock:
            await self.publish_pending()
        return {"report_id": report["id"], "status": "queued", "body": report["body"]}
