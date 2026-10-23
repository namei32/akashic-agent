"""真实插件安装、Models 账本和 HTTP 查询；只使用一次性合成调用记录。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def seed(workspace: Path):
    from plugins.models.contract import BoundModelDescriptor, CapabilitySources, LLMResponse, ModelCapabilities, ModelUsage
    from plugins.models.store import ModelsStore

    store = ModelsStore(workspace / "model-registry.sqlite3", workspace / "backups")
    store.initialize()
    binding = BoundModelDescriptor("scenario", "snapshot", 1, "sample", "local", "scenario", "1",
        "PRIVATE-IDENTITY", "sample-chat", "default", None, ModelCapabilities(), CapabilitySources(), "digest")

    def start(key: str, descriptor=binding):
        return store.resume_call(descriptor, "PRIVATE-DIGEST", request_key=key, owner_id="scenario", max_attempts=3)

    def finish(identity: str, usage: ModelUsage | None):
        store.finish_call(identity, usage=usage, failure=None, duration_ms=2000, first_token_ms=500,
                          response=LLMResponse("PRIVATE-OUTPUT"))

    exact = ModelUsage(input_tokens=100, output_tokens=20, cached_input_tokens=80,
                       reasoning_output_tokens=5, covered_request_count=1, coverage="exact")
    try:
        for index in range(52):
            usage = exact if index % 2 == 0 else ModelUsage(input_tokens=100, output_tokens=20,
                covered_request_count=1, coverage="exact")
            identity = start(f"call:{index}")
            finish(identity, usage)
            # 制作历史账本夹具：已结算行分布在过去一天，便于检查时间筛选与趋势。
            with sqlite3.connect(store.path) as connection:
                stamp = (datetime.now(UTC) - timedelta(minutes=index * 20)).strftime("%Y-%m-%d %H:%M:%S")
                connection.execute("UPDATE model_calls SET started_at=? WHERE id=?", (stamp, identity))
        finish(start("partial"), ModelUsage(input_tokens=30, coverage="partial"))
        finish(start("unknown"), None)
        failed = start("retry")
        store.finish_call(failed, usage=None, failure="TransportError: PRIVATE-ERROR", duration_ms=1000,
                          next_attempt_at=0, send_evidence="unknown")
        finish(start("retry"), exact)
        start("unsettled")
    finally:
        store.close()
    return binding


def snapshot(path: Path) -> str:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return "\n".join(connection.iterdump())


async def run(directory: Path, port: int | None) -> dict[str, bool]:
    import httpx
    from agent.plugins.bundles import set_plugin_choice
    from agent.plugins.install import install_git_plugin
    from agent.plugins.manager import PluginManager
    from agent.plugins.selection import PluginSelection
    from agent.plugins.static_manifest import load_static_plugin_manifest
    from plugins.ui.contract import WEB_UI

    workspace, home, sources = directory / "workspace", directory / "home", directory / "sources"
    workspace.mkdir(); home.mkdir()
    os.environ.update(HOME=str(home), AKASHIC_PLUGIN_HOME=str(home), AKASHIC_PLUGIN_DISTRIBUTION="",
                      AKASHIC_EXTRA_PLUGIN_DIRS="", AKASHIC_EXECUTION_MODE="local")
    PluginSelection(workspace).initialize()
    seed(workspace)
    shutil.copytree(ROOT / "plugins", sources, ignore=shutil.ignore_patterns("__pycache__"))
    if port is not None:
        shutil.copytree(ROOT / "static/dashboard", sources / "ui/static/dashboard", dirs_exist_ok=True)
    for path in sources.iterdir():
        if (path / "plugin.py").is_file():
            name = load_static_plugin_manifest(path).name
            set_plugin_choice(workspace, name, enabled=name in {"models", "ui", "shell-ui"})
    provider = directory / "model_usage"
    shutil.copytree(ROOT / "plugins/model_usage", provider, ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["git", "init", "-q", "--initial-branch=source", str(provider)], check=True)
    subprocess.run(["git", "-C", str(provider), "add", "."], check=True)
    subprocess.run(["git", "-C", str(provider), "-c", "user.name=Scenario", "-c", "user.email=scenario@example.invalid",
                    "commit", "-qm", "isolated model usage source"], check=True)
    install_git_plugin(workspace=workspace, source=str(provider), marketplace="lab", plugins_home=home)
    manager = PluginManager([sources], workspace=workspace, installed_cache_root=home / "cache")
    try:
        await manager.load_all()
        root = manager.live_root
        assert root is not None
        endpoint = next(item for item in root.endpoints() if item.name == "dashboard")
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=endpoint.address), base_url="http://local") as web:
            if port is not None:
                import uvicorn
                app = FastAPI()
                @app.get("/__scenario/theme/{theme}")
                async def theme_preview(theme: str):
                    if theme not in {"light", "dark", "reset"}:
                        raise HTTPException(404)
                    reply = RedirectResponse("/dashboard#model-usage")
                    if theme == "reset":
                        reply.delete_cookie("akashic_theme", path="/dashboard")
                    else:
                        reply.set_cookie("akashic_theme", theme, path="/dashboard", max_age=600)
                    return reply
                @app.get("/api/chat/web-ui/bootstrap")
                async def bootstrap():
                    return Response(await root.context.require(WEB_UI).bootstrap(), media_type="application/json")
                @app.get("/api/chat/web-ui/state")
                async def state():
                    return await root.context.require(WEB_UI).state()
                @app.api_route("/{path:path}", methods=["GET", "POST"])
                async def forward(request: Request, path: str):
                    reply = await web.request(request.method, "/" + path, params=request.query_params, content=await request.body(),
                        headers={key: value for key, value in request.headers.items() if key != "host"})
                    return Response(reply.content, status_code=reply.status_code,
                        headers={key: value for key, value in reply.headers.items() if key not in {"content-length", "content-encoding"}})
                await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).serve()
                return {"browser_fixture_closed": True}
            now = datetime.now(UTC) + timedelta(seconds=60)
            params = {"start": (now - timedelta(days=1)).isoformat(), "end": now.isoformat()}
            catalog = json.loads(await root.context.require(WEB_UI).bootstrap())
            module = next(item for item in catalog["modules"] if item["pluginId"] == "model_usage@lab")
            headers = {"x-akashic-web-snapshot": catalog["snapshotId"], "x-akashic-web-catalog": catalog["catalogId"],
                       "x-akashic-web-module": module["pluginId"], "x-akashic-web-generation": module["generationId"]}
            before = snapshot(workspace / "model-registry.sqlite3")

            async def get(**extra):
                reply = await web.get("/api/dashboard/model-usage", params={**params, **extra}, headers=headers)
                assert reply.status_code == 200, reply.text
                assert "PRIVATE-" not in reply.text
                return reply.json()

            report = await get()
            totals = report["summary"]
            assert totals["calls"] == 57 and totals["success"] == 55 and totals["errors"] == 1 and totals["unsettled"] == 1, totals
            assert totals["input_tokens"] == 5330 and totals["output_tokens"] == 1060, totals
            assert totals["input_tokens_known_calls"] == 54 and totals["output_tokens_known_calls"] == 53
            assert totals["cache_write_input_tokens"] is None and totals["cache_write_input_tokens_known_calls"] == 0
            assert totals["cached_input_tokens"] == 2160 and totals["cache_hit_rate"] == .8
            assert totals["retries"] == 1 and totals["tokens_per_second"] == 10
            assert len(report["trend"]) > 10
            second = await get(cursor=report["next_cursor"])
            assert len(report["calls"]) == 50 and len(second["calls"]) == 7 and second["next_cursor"] is None
            assert second["summary"] == totals
            assert len({row["id"] for row in report["calls"] + second["calls"]}) == 57
            assert (await get(model="missing"))["summary"]["calls"] == 0
            assert (await get(model="sample-chat"))["summary"] == totals
            assert (await get(start=(now - timedelta(minutes=5)).isoformat()))["summary"]["calls"] < 57
            for invalid in ({"cursor": "!bad!"}, {"start": now.isoformat()}, {"start": (now-timedelta(days=32)).isoformat()}, {"start": "2026-01-01T00:00:00"}):
                assert (await web.get("/api/dashboard/model-usage", params={**params, **invalid}, headers=headers)).status_code == 422
            assert (await web.get("/api/dashboard/model-usage", params=params, headers={"sec-fetch-site": "same-origin"})).status_code == 403
            assert snapshot(workspace / "model-registry.sqlite3") == before
            assert not (workspace / "sessions.db").exists()
            # 翻页期间有新调用落在同一时间范围，也不能改变首次查询的行集合。
            with sqlite3.connect(workspace / "model-registry.sqlite3") as writer:
                writer.execute("INSERT INTO model_calls (id,binding_json,request_digest,state) "
                    "SELECT 'scenario-late',binding_json,'late','started' FROM model_calls LIMIT 1")
            before = snapshot(workspace / "model-registry.sqlite3")
            # 不同 end 绕过短缓存，同时沿用第一页的行上界。
            newer_end = (now + timedelta(seconds=1)).isoformat()
            assert (await get(end=newer_end, cursor=report["next_cursor"]))["summary"]["calls"] == 57
            assert (await get(end=newer_end))["summary"]["calls"] == 58
            assert snapshot(workspace / "model-registry.sqlite3") == before
        set_plugin_choice(workspace, "model_usage@lab", enabled=False)
        await manager.reconcile_disabled_and_drain("model_usage@lab")
        catalog = json.loads(await root.context.require(WEB_UI).bootstrap())
        assert all(item["pluginId"] != "model_usage@lab" for item in catalog["modules"])
        assert snapshot(workspace / "model-registry.sqlite3") == before
        return {"installed_plugin_http": True, "known_and_unknown_fields": True, "retry_and_speed": True,
                "pagination_and_filtering": True, "concurrent_append_boundary": True,
                "no_private_payload": True, "read_only_database": True, "disable_keeps_history": True}
    finally:
        await manager.terminate_all()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="akashic-model-usage-") as temporary:
        print(json.dumps(asyncio.run(run(Path(temporary), args.port))))
