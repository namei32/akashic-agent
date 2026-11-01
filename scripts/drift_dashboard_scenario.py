"""Use the installed dashboard HTTP boundary for versioned Drift controls."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from default_memory_scenario import run
from drift_memory_wake_scenario import TARGET, configure as configure_wake


def configure(sources: Path, workspace: Path) -> None:
    configure_wake(sources, workspace)
    for name in ("ui", "shell_ui"):
        shutil.copytree(ROOT / "plugins" / name, sources / name, ignore=shutil.ignore_patterns("__pycache__"))


async def headers(host):
    from plugins.ui.contract import WEB_UI
    current = host.live_root
    assert current is not None
    data = json.loads(await current.context.require(WEB_UI).bootstrap())
    module = next(item for item in data["modules"] if item["pluginId"].split("@")[0] == "wake")
    return {"x-akashic-web-snapshot": data["snapshotId"], "x-akashic-web-catalog": data["catalogId"],
            "x-akashic-web-module": module["pluginId"], "x-akashic-web-generation": module["generationId"]}


async def exercise(host, append, workspace: Path, requests: dict) -> dict[str, object]:
    import httpx
    from plugins.ledger.contract import MESSAGE_CATALOG

    await append(TARGET["session_id"], "dashboard-original", "I enjoy landscape photography.")
    catalog = host.live_root.context.require(MESSAGE_CATALOG)
    before = catalog.reader(TARGET["session_id"]).snapshot()
    endpoint = next(value for value in host.live_root.endpoints() if value.name == "dashboard")

    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=endpoint.address), base_url="http://local") as web:
        path = "/api/dashboard/wake/initiative"
        assert (await web.get(path)).status_code == 403
        async def request(method, suffix="", body=None, expected=200):
            response = await web.request(method, path + suffix, json=body, headers=await headers(host))
            assert response.status_code == expected, response.text
            return response.json()

        initial = await request("GET")
        assert initial["consent"]["enabled"] is False and initial["control"]["paused"] is False
        await request("POST", "/memory-consent", {"enabled": True, "target": {**TARGET, "recipient": "other"},
                      "request_id": "incorrect", "expected_version": 0}, expected=409)
        grant = {"enabled": True, "target": TARGET, "request_id": "grant", "expected_version": 0}
        accepted = await request("POST", "/memory-consent", grant)
        assert accepted["enabled"] and accepted["target"] == TARGET and accepted["consent_version"] == 1
        assert (await request("POST", "/memory-consent", grant))["control_receipt"]["version"] == 1
        await request("POST", "/memory-consent", {"enabled": False, "request_id": "stale", "expected_version": 0}, expected=409)
        await request("POST", "/pause", {"request_id": "pause", "expected_version": 0})
        assert (await request("GET"))["control"]["paused"] is True
        await request("POST", "/resume", {"request_id": "resume", "expected_version": 1})
        producer = initial["config"]["producer"]
        config = {**producer, "auto_propose": False, "memory_assisted": True, "interval_seconds": 60,
                  "request_id": "configure", "expected_input": initial["config"]["input_ref"]}
        receipt = await request("POST", "/configure", config)
        assert receipt["request_id"] == "configure"
        async with asyncio.timeout(20):
            while True:
                latest = await request("GET", "/config-receipts/configure")
                if latest["state"] in {"active", "failed"}:
                    break
                await asyncio.sleep(.05)
        assert latest["state"] == "active", latest
        current = await request("GET")
        assert current["config"]["producer"]["memory_assisted"] is True
        assert current["config"]["producer"]["auto_propose"] is False
        await request("POST", "/memory-consent", {"enabled": False, "request_id": "revoke", "expected_version": 1})
        assert (await request("GET"))["consent"]["enabled"] is False
    assert catalog.reader(TARGET["session_id"]).snapshot() == before
    return {"dashboard_requires_web_identity": True, "target_checked": True, "version_and_replay": True,
            "pause_resume": True, "config_receipt": True, "revoke": True, "original_messages_preserved": True}


def configure_without_drift(sources: Path, workspace: Path) -> None:
    configure(sources, workspace)
    shutil.rmtree(sources / "drift")


async def missing_drift(host, append, workspace: Path, requests: dict) -> dict[str, object]:
    import httpx
    endpoint = next(value for value in host.live_root.endpoints() if value.name == "dashboard")
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=endpoint.address), base_url="http://local") as web:
        identity = await headers(host)
        unavailable = await web.get("/api/dashboard/wake/initiative", headers=identity)
        assert unavailable.status_code == 503, unavailable.text
        config = await web.get("/api/dashboard/wake/config", headers=identity)
        assert config.status_code == 200, config.text
    return {"missing_drift_keeps_wake_settings": True}


async def scenario(folder: Path) -> dict[str, object]:
    present, absent = folder / "present", folder / "absent"
    present.mkdir()
    absent.mkdir()
    results = await run(present, extension=exercise, configure=configure, setup_only=True)
    return {**results, **await run(absent, extension=missing_drift, configure=configure_without_drift, setup_only=True)}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="dd-") as directory:
        print(json.dumps(asyncio.run(scenario(Path(directory))), ensure_ascii=False))
