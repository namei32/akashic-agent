"""Verify native publishing with real systemd, Gateway, installer and temporary data."""
from __future__ import annotations

import asyncio
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, cast
from uuid import uuid4
from websockets.asyncio.client import connect as web_connect

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "sdk/python/src")]

from akashic_sdk import AsyncAkashic
from agent.plugins.bundles import set_plugin_choice
from plugins.gateway.cli import find_endpoint, read_token
from scripts.akashic_release.manifest import read_json, write_json
from scripts.akashic_release.wsl import Host, sha256
from scripts.build_plugin_distribution import build
from scripts.install_plugin_distribution import ensure_bundle, extract_core
from scripts.verify_self_deploy_runtime import configure_model, start_model_server


RECEIPT_FAILURE_SESSION = "programmatic:wsl-stop-receipt"
RECEIPT_FAILURE_INPUT = "Submit the fixture release with a stop receipt write failure."
AFTER_RECEIPT_FAILURE = "Confirm that this instance accepts new work."
MIGRATION = "20990101_01_wsl_rollback_probe"


def run(*argv: str, cwd: Path | None = None, timeout: float = 300) -> str:
    return subprocess.check_output(argv, cwd=cwd, text=True, timeout=timeout).strip()


def commit(repo: Path, message: str) -> str:
    run("git", "add", ".", cwd=repo)
    run("git", "-c", "user.name=Scenario", "-c", "user.email=scenario@example.invalid",
        "-c", "commit.gpgSign=false", "commit", "-qm", message, cwd=repo)
    return run("git", "rev-parse", "HEAD", cwd=repo)


def releases(folder: Path, wheels: Path) -> list[tuple[str, Path, Path]]:
    repo = folder / "source"
    run("git", "clone", "--shared", "--quiet", str(ROOT), str(repo))
    run("git", "checkout", "--quiet", "--detach", run("git", "-C", str(ROOT), "rev-parse", "HEAD"), cwd=repo)
    for name in ("scripts/akashic_release/cli.py", "scripts/akashic_release/self_deploy.py",
                 "scripts/akashic_release/wsl.py", "scripts/build_plugin_distribution.py",
                 "scripts/install_plugin_distribution.py", "scripts/release_boot_probe.py",
                 "plugins/delivery/api.py", "plugins/delivery/contract.py", "plugins/gateway/runtime_stop.py",
                 "plugins/message_push/plugin.py", "plugins/message_push/release_report.py",
                 "plugins/standard_tools/skills/deploy-akashic/SKILL.md"):
        shutil.copyfile(ROOT / name, repo / name)
    contract = repo / "plugins/jobs/contract.py"
    original_contract = contract.read_text()
    contract.write_text(original_contract + "\nRELEASE_PROBE_API = 1\n")
    commits = [commit(repo, "Isolated bootstrap")]
    jobs = repo / "plugins/jobs/plugin.py"
    text = jobs.read_text()
    for version in ("9.0.2", "9.0.3"):
        jobs.write_text(re.sub(r'^version = .+$', f'version = "{version}"', text, flags=re.M))
        commits.append(commit(repo, f"Jobs {version}"))
    contract.write_text(original_contract)
    commits.append(commit(repo, "Remove the interface consumed by the retained external plugin"))
    contract.write_text(original_contract + "\nRELEASE_PROBE_API = 1\n")
    (repo / "migrations/core" / f"{MIGRATION}.py").write_text(
        'from yoyo import step\nsteps = [step("CREATE TABLE wsl_rollback_probe (value INTEGER)"),'
        ' step("INSERT INTO wsl_rollback_probe VALUES (79)")]\n')
    commits.append(commit(repo, "Add a migration before candidate startup"))

    def package(sha: str) -> tuple[str, Path, Path]:
        destination = folder / sha
        dist = destination / "distribution"
        destination.mkdir()
        build(repo, sha, dist)
        shutil.copytree(wheels, dist / "wheels")
        return sha, dist, extract_core(dist, destination / "core")

    with ThreadPoolExecutor(max_workers=3) as executor:
        return list(executor.map(package, commits))


def external(folder: Path) -> list[tuple[str, Path]]:
    source = folder / "external"
    source.mkdir()
    run("git", "init", "--quiet", "--initial-branch=main", cwd=source)
    result = []
    for version in ("1.0.0", "2.0.0"):
        (source / "plugin.py").write_text(
            f'from plugins.jobs.contract import RELEASE_PROBE_API\napi_version = 3\nname = "release_probe"\nversion = "{version}"\n'
            'async def apply(ctx):\n'
            '    marker = ctx.runtime.data_dir / "fail-stop-once"\n'
            '    def setup():\n'
            '        def cleanup():\n'
            '            if marker.exists():\n'
            '                marker.unlink()\n'
            '                raise RuntimeError("isolated plugin cleanup failure")\n'
            '        return cleanup\n'
            '    await ctx.effect(setup, label="release probe cleanup")\n'
        )
        sha = commit(source, version)
        bundle = folder / f"probe-{version}.bundle"
        run("git", "bundle", "create", str(bundle), "HEAD", "main", cwd=source)
        result.append((sha, bundle))
    return result


async def connect(host: Host) -> AsyncAkashic:
    endpoint = find_endpoint(host.workspace)
    return await AsyncAkashic.connect(endpoint, workspace_token=read_token(host.workspace, endpoint))


async def wait_job(path: Path, *, status: str, require_error: bool = False) -> dict[str, Any]:
    async with asyncio.timeout(150):
        while True:
            if path.exists():
                record = read_json(path)
                if record["status"] == status and (not require_error or record.get("error")):
                    return record
                if record["status"] == "failed_preflight" or record.get("error") and status == "active":
                    raise RuntimeError(record.get("error", record["status"]))
            await asyncio.sleep(0.2)


async def submit(host: Host, folder: Path, subcommand: list[str], session: str,
                 *, status: str = "active") -> tuple[Path, dict[str, Any]]:
    (folder / "next.json").write_text(json.dumps(subcommand))
    async with await connect(host) as client:
        await client.request("programmatic/session/admit", {"session_id": session, "persist_memory": False})
        async with await client.session_follow(session) as feed:
            await client.request("programmatic/message/send", {"session_id": session, "message_id": session + ":input",
                                                               "text": "Use deploy-akashic and finish normally."})
            async with asyncio.timeout(45):
                async for _ in feed.events():
                    page = await client.message_read(session, limit=100)
                    if any(item["body"].get("kind") == "output" and item["body"].get("finish") == "complete"
                           for item in page["items"]):
                        break
            record = (host.root / "run/self-deploy").glob("*.json")
            matches = [path for path in record if read_json(path)["request"]["stop"]["session_id"] == session]
            assert len(matches) == 1
            path = matches[0]
            return path, await wait_job(path, status=status, require_error=status == "selected_not_started")


async def plugins(host: Host) -> dict[str, Any]:
    async with await connect(host) as client:
        status = cast(dict[str, Any], await client.request("plugin/status", {}))
        return {row["plugin_id"]: row for row in status["plugins"] if row.get("state") == "active"}


def callback_fixture(body: dict[str, Any], submit_command: str) -> dict[str, Any] | None:
    if not body.get("tools") or not any("wsl-callback-submit" in str(row.get("content")) for row in body["messages"]):
        return None
    history = body["messages"]
    calls = {call["id"] for row in history for call in row.get("tool_calls", [])}
    callback = any("通知回执 job:" in str(row.get("content")) for row in history)
    if "fixture-callback-submit" in calls or ("fixture-start-build" in calls and not callback):
        return {"role": "assistant", "content": "本回合结束。"}
    command = submit_command if callback else "/usr/bin/sleep 2"
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "fixture-callback-submit" if callback else "fixture-start-build", "type": "function",
        "function": {"name": "shell", "arguments": json.dumps({"command": command, "description": "release fixture",
                    "login": False, "run_in_background": not callback, "yield_time_ms": 50 if not callback else 10000})}}]}


def request_for_session(host: Host, session: str) -> Path | None:
    paths = [path for path in (host.root / "run/self-deploy").glob("*.json")
             if read_json(path)["request"]["stop"]["session_id"] == session]
    assert len(paths) <= 1
    return paths[0] if paths else None


async def scripted_reply(host: Host, body: dict[str, Any], command: str) -> dict[str, Any] | None:
    if not body.get("tools"):
        return None
    messages = body["messages"]
    if any(AFTER_RECEIPT_FAILURE in str(message.get("content")) for message in messages if message["role"] == "user"):
        return {"role": "assistant", "content": "Admission restored."}
    if (any(RECEIPT_FAILURE_INPUT in str(message.get("content")) for message in messages if message["role"] == "user")
            and any(message["role"] == "tool" for message in messages)):
        async with asyncio.timeout(90):
            while True:
                path = request_for_session(host, RECEIPT_FAILURE_SESSION)
                if path is not None and read_json(path).get("preflight", {}).get("boot_probe", {}).get("conversation") == "passed":
                    path.parent.chmod(0o500)
                    break
                await asyncio.sleep(0.1)
        return {"role": "assistant", "content": "Ready to stop."}
    return callback_fixture(body, command)


async def verify_receipt_write_failure(host: Host, folder: Path, commit: str, history_session: str) -> None:
    old = host.ready()
    selection = (host.workspace / "runtime/plugin-stable.json").read_bytes()
    receipt_dir = host.root / "run/self-deploy"
    mode = receipt_dir.stat().st_mode & 0o777
    (folder / "next.json").write_text(json.dumps(["submit", "--commit", commit]))
    async with await connect(host) as client:
        history = (await client.message_read(history_session, limit=100))["items"]
        await client.request("programmatic/session/admit", {"session_id": RECEIPT_FAILURE_SESSION, "persist_memory": False})
        async with await client.session_follow(RECEIPT_FAILURE_SESSION) as feed:
            try:
                await client.request("programmatic/message/send", {"session_id": RECEIPT_FAILURE_SESSION,
                                     "message_id": RECEIPT_FAILURE_SESSION + ":input", "text": RECEIPT_FAILURE_INPUT})
                async with asyncio.timeout(115):
                    async for _ in feed.events():
                        page = await client.message_read(RECEIPT_FAILURE_SESSION, limit=100)
                        if any("Ready to stop." in str(item["body"].get("parts")) for item in page["items"]):
                            break
                    path = request_for_session(host, RECEIPT_FAILURE_SESSION)
                    assert path is not None and receipt_dir.stat().st_mode & 0o777 == 0o500
                    record = read_json(path)
                    unit = record["request"]["unit"]
                    while (await asyncio.to_thread(run, "systemctl", "--user", "show", unit, "-p",
                                                    "ActiveState", "--value")).strip() in {"active", "activating"}:
                        await asyncio.sleep(0.1)
                    journal = await asyncio.to_thread(run, "journalctl", "--user", "-u", unit, "--no-pager", "-o", "cat")
                    assert "PermissionError" in journal and "无法保存失败回执" in journal, journal
                    assert read_json(path) == record and record["status"] == "preflight" and "publication" not in record
            finally:
                receipt_dir.chmod(mode)
        assert host.ready() == old and host.chat_ready()
        assert not (host.workspace / "runtime/closed" / f"{old['bootId']}.json").exists()
        assert (host.workspace / "runtime/plugin-stable.json").read_bytes() == selection
        assert read_json(host.launch_path) == {"commit": old["sourceCommit"]}
        assert (await client.message_read(history_session, limit=100))["items"] == history
        new_session = "programmatic:wsl-admission-restored"
        await client.request("programmatic/session/admit", {"session_id": new_session, "persist_memory": False})
        async with await client.session_follow(new_session) as feed:
            await client.request("programmatic/message/send", {"session_id": new_session,
                                 "message_id": new_session + ":input", "text": AFTER_RECEIPT_FAILURE})
            async with asyncio.timeout(20):
                async for _ in feed.events():
                    result = await client.message_read(new_session, limit=100)
                    if any("Admission restored." in str(item["body"].get("parts")) for item in result["items"]):
                        break
    print("STOP RECEIPT FAILURE reopened admission on the same boot and completed fresh chat", flush=True)


async def report_items(host: Host, session: str, request_id: str, expected: str) -> list[dict[str, Any]]:
    async with asyncio.timeout(30):
        while True:
            async with await connect(host) as client:
                page = await client.message_read(session, limit=100)
            matches = [item for item in page["items"] if item["source"] == "release_report"
                       and request_id in str(item["body"]["parts"])]
            if any(expected in str(item["body"]["parts"]) for item in matches):
                return matches
            await asyncio.sleep(0.2)


async def verify_rollback_chat(host: Host) -> str:
    session = "akashic:" + uuid4().hex
    secret = host.workspace / "web-auth/secret"
    headers = {"Authorization": "Bearer " + secret.read_text().strip()} if secret.exists() else {}
    async with web_connect(f"ws://127.0.0.1:{host.port}/ws", additional_headers=headers, proxy=None) as web:
        await web.send(json.dumps({"type": "message.send", "request_id": uuid4().hex, "session_id": session,
                                   "text": AFTER_RECEIPT_FAILURE}))
        async with asyncio.timeout(25):
            while json.loads(await web.recv()).get("type") != "message.accepted":
                pass
            async with await connect(host) as client:
                while True:
                    page = await client.message_read(session, limit=30)
                    if any(row["body"].get("finish") == "complete"
                           and "Admission restored." in str(row["body"]["parts"]) for row in page["items"]):
                        return session
                    await asyncio.sleep(0.2)


async def verify_automatic_rollback(host: Host, folder: Path, candidate: str, migration: str,
                                    fail_candidate: Path, cli: list[str], history_session: str) -> None:
    old = host.ready()
    old_components = host.selection.components(old["root"])
    old_inputs = {ref: dict(host.selection.read_input(ref)) for ref in old_components}
    async with await connect(host) as client:
        history = (await client.message_read(history_session, limit=100))["items"]
    write_json(fail_candidate, {"commit": candidate})
    auto_session = "programmatic:wsl-auto-rollback"
    job, result = await submit(host, folder, ["submit", "--commit", candidate], auto_session, status="rolled_back")
    assert result["error"] and result["recovery"]["status"] == "restored"
    ready = host.ready()
    assert ready["sourceCommit"] == old["sourceCommit"] and ready["bootId"] != old["bootId"]
    assert ready["root"] == result["rollback"]["new_root_ref"] and ready == result["ready"], (ready, result["ready"])
    assert host.selection.components(ready["root"]) == old_components
    assert {ref: dict(host.selection.read_input(ref)) for ref in old_components} == old_inputs
    reports = await report_items(host, auto_session, job.stem, "发布失败，旧版已恢复")
    assert len(reports) == 1 and result["error"] in str(reports[0]["body"]["parts"])
    await verify_rollback_chat(host)
    async with await connect(host) as client:
        assert (await client.message_read(history_session, limit=100))["items"] == history
    print("AUTO ROLLBACK restored ordered inputs and completed a new Web chat", flush=True)

    write_json(fail_candidate, {"commit": candidate, "change_root": True})
    job, result = await submit(host, folder, ["submit", "--commit", candidate],
                               "programmatic:wsl-root-mismatch", status="selected_not_started")
    published = result["publication"]["new_root_ref"]
    replacement = read_json(folder / "fixture-root.json")["root"]
    assert replacement != published and host.selection.read() == replacement
    assert "本次发布" in result["recovery"]["error"] and result["recovery"]["status"] == "skipped"
    assert "rollback" not in result and read_json(host.launch_path) == {"commit": candidate, "root": published}
    assert read_json(host.root / "active.json") == ready
    print("ROOT MISMATCH preserved the newer selection and failed launch", flush=True)
    fail_candidate.unlink()
    write_json(host.launch_path, {"commit": candidate, "root": replacement})
    await asyncio.to_thread(host.activate, candidate, replacement)

    write_json(fail_candidate, {"commit": migration})
    job, result = await submit(host, folder, ["submit", "--commit", migration],
                               "programmatic:wsl-migration", status="selected_not_started")
    assert result["preflight"]["migration_ids"] == result["publication"]["migration_ids"] == [MIGRATION]
    assert result["recovery"]["status"] == "skipped" and "迁移" in result["recovery"]["error"]
    assert "rollback" not in result
    published = result["publication"]["new_root_ref"]
    assert host.selection.read() == published and read_json(host.launch_path) == {"commit": migration, "root": published}
    with sqlite3.connect(f"file:{host.workspace / 'migrations.sqlite3'}?mode=ro", uri=True) as ledger:
        assert ledger.execute("SELECT value FROM wsl_rollback_probe").fetchall() == [(79,)]
    print("MIGRATION preserved the migrated ledger and failed candidate", flush=True)
    fail_candidate.unlink()
    await asyncio.to_thread(run, *cli, "resume", job.stem)


async def rejected_callback(host: Host, folder: Path, target: str) -> None:
    session = "akashic:" + uuid4().hex
    (folder / "next.json").write_text(json.dumps(["submit", "--commit", target]))
    secret = host.workspace / "web-auth/secret"
    headers = {"Authorization": "Bearer " + secret.read_text().strip()} if secret.exists() else {}
    async with web_connect(f"ws://127.0.0.1:{host.port}/ws", additional_headers=headers, proxy=None) as web:
        await web.send(json.dumps({"type": "message.send", "request_id": "wsl-callback-submit", "session_id": session,
                                   "text": "wsl-callback-submit"}))
        async with asyncio.timeout(35):
            while True:
                matches = [path for path in (host.root / "run/self-deploy").glob("*.json")
                           if read_json(path)["request"]["stop"]["session_id"] == session]
                if matches:
                    path, = matches
                    state = read_json(path)
                    if state["status"] == "failed":
                        break
                await asyncio.sleep(0.2)
        assert "job:shell-" in state["error"] and "最终 Output provider" in state["error"], state
        assert len(await report_items(host, session, path.stem, "最终 Output provider")) == 1
    print("CALLBACK rejected before worker, reported to the original Web session", flush=True)


async def desktop_notice(host: Host, request_id: str) -> None:
    queue = host.root / "desktop-notifications"
    async with asyncio.timeout(20):
        while True:
            matches = [p for p in queue.glob("*.json") if request_id in read_json(p)["text"]]
            if matches:
                notice, = matches
                break
            await asyncio.sleep(0.2)
    if host.desktop_notifications:
        script = ROOT / "integrations/wsl_release_notifications/Watch-ReleaseNotifications.ps1"
        script_path = run("wslpath", "-w", str(script))
        queue_path = run("wslpath", "-w", str(queue))
        command = f"& ([ScriptBlock]::Create((Get-Content -LiteralPath '{script_path}' -Raw))) -Queue '{queue_path}' -Once"
        await asyncio.to_thread(run, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command, timeout=75)
        assert notice.with_suffix(".ack").is_file(), "Windows did not confirm displaying the isolated release notification"
        print("DESKTOP Windows confirmed displaying the release failure", flush=True)


async def scenario(folder: Path, compiled: list[tuple[str, Path, Path]], probe: list[tuple[str, Path]], *,
                   desktop: bool, auto_rollback_only: bool = False) -> None:
    state, root, home = folder / "state", folder / "releases", folder / "home"
    for path in (state, root, home):
        path.mkdir()
    os.environ["HOME"] = str(home)
    unit = f"akashic-wsl-{uuid4().hex[:12]}.service"
    profile = folder / "host.json"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    write_json(profile, {"root": str(root), "state": str(state), "unit": unit, "port": port,
                         "desktop_notifications": desktop})
    host = Host(profile)
    (folder / "shell.py").write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        f"args=json.loads(Path({str(folder / 'next.json')!r}).read_text())\n"
        f"os.execv(sys.executable,[sys.executable,'-B',{str(ROOT / 'scripts/akashic_release/cli.py')!r},"
        f"'wsl','--profile',{str(profile)!r},*args])\n"
    )
    pythonpath = f"{ROOT}:{ROOT / 'sdk/python/src'}"
    # Volatile, uniquely named unit. Failure injection applies only to this instance.
    unit_root = Path(f"/run/user/{os.getuid()}/systemd/user")
    unit_root.mkdir(parents=True, exist_ok=True)
    unit_file = unit_root / unit
    hold = folder / "hold-start"
    fail_candidate = folder / "fail-candidate.json"
    start_check = folder / "check-start.py"
    start_check.write_text(f"""from pathlib import Path
from scripts.akashic_release.manifest import read_json, write_json
from scripts.akashic_release.wsl import Host
from bootstrap.workspace_lock import PluginPublicationLock, WorkspaceMaintenanceLock

fault = Path({str(fail_candidate)!r})
if fault.exists():
    host = Host(Path({str(profile)!r}))
    mode = read_json(fault)
    launch = read_json(host.launch_path)
    if launch['commit'] == mode['commit']:
        if mode.get('change_root'):
            maintenance = WorkspaceMaintenanceLock(host.workspace)
            publication = PluginPublicationLock(host.state / 'plugin-home')
            maintenance.acquire()
            publication.acquire()
            root = launch['root']
            replacement = host.selection.commit(host.selection.components(root), expected_ref=root)
            write_json(fault.with_name('fixture-root.json'), {{'root': replacement}})
            publication.release()
            maintenance.release()
        raise SystemExit('Isolated candidate startup failure')
""")
    unit_text = f"""[Unit]
Description=Isolated Akashic native deployment test
[Service]
Type=exec
KillMode=mixed
Restart=always
RestartSec=15
WorkingDirectory={ROOT}
Environment=HOME={home}
Environment=PYTHONPATH={pythonpath}
ExecStartPre=/usr/bin/test ! -e {hold}
ExecStartPre={sys.executable} -B {start_check}
ExecStart={sys.executable} -B -m scripts.akashic_release.wsl --profile {profile} launch
"""
    base, base_dist, base_core = compiled[0]
    run(sys.executable, "-B", str(base_core / "main.py"), "init", "--config", str(state / "config.toml"),
        "--workspace", str(host.workspace))
    ensure_bundle(base_dist, base_dist / "bundles/base.toml", workspace=host.workspace, plugins_home=state / "plugin-home",
                  config_path=state / "config.toml", receipt_path=host.workspace / "runtime/distribution-install.json")
    for _, dist, core in compiled:
        run(sys.executable, "-B", str(ROOT / "scripts/akashic_release/cli.py"), "wsl", "--profile", str(profile),
            "register", "--core", str(core), "--distribution", str(dist), "--python", sys.executable)
    # Computer needs a separate workload controller. This scenario uses real Web/API and Shell.
    if any(row["name"] == "computer" for row in read_json(base_dist / "distribution.json")["plugins"]):
        set_plugin_choice(host.workspace, "computer@release", enabled=False)
        host.publish(base, _plan(host, folder / "initial.json", []), base_dist)
    write_json(host.launch_path, {"commit": base})
    cli = [sys.executable, "-B", str(ROOT / "scripts/akashic_release/cli.py"), "wsl", "--profile", str(profile)]
    command = f"{sys.executable} -B {folder / 'shell.py'}"
    server, model_port = await start_model_server(folder, command=command,
                                                 scripted_reply=lambda body: scripted_reply(host, body, command))
    try:
        with unit_file.open("x") as stream:
            stream.write(unit_text)
        run("systemctl", "--user", "daemon-reload")
        initial = host.selection.read()
        assert initial is not None
        first = await asyncio.to_thread(host.activate, base, initial)
        async with await connect(host) as client:
            await configure_model(client, model_port)
        sha, bundle = probe[0]
        installed = json.loads(await asyncio.to_thread(run, *cli, "plugin-install", "--source", str(bundle), "--ref", sha))
        async with asyncio.timeout(30):
            while True:
                hot = json.loads(await asyncio.to_thread(run, *cli, "plugin-status", installed["update_id"]))
                if hot["state"] == "active" and hot["active_input_ref"] == hot["input_ref"]:
                    break
                await asyncio.sleep(0.2)
        assert host.ready()["bootId"] == first["bootId"]
        hot_input = (await plugins(host))["release_probe@local"]["input_ref"]
        print("HOT active without restarting", flush=True)

        if auto_rollback_only:
            history_session = await verify_rollback_chat(host)
            await verify_automatic_rollback(host, folder, compiled[2][0], compiled[4][0],
                                            fail_candidate, cli, history_session)
            return

        builtin, _, _ = compiled[1]
        await rejected_callback(host, folder, builtin)
        assert host.ready() == first | {"root": host.selection.read()}
        incompatible, _, _ = compiled[3]
        old = host.ready()
        bad_session = "programmatic:wsl-incompatible"
        bad_job, bad = await submit(host, folder, ["submit", "--commit", incompatible], bad_session, status="failed_preflight")
        assert "RELEASE_PROBE_API" in bad["error"] and "release_probe@local" in bad["error"], bad
        assert host.ready() == old and "publication" not in bad
        assert len(await report_items(host, bad_session, bad_job.stem, "RELEASE_PROBE_API")) == 1
        print("COMPATIBILITY detected the retained plugin failure before stopping, notified original session", flush=True)
        wrong = folder / "stale-plan.json"
        old = host.ready()
        write_json(wrong, {"schema_version": 1, "expected_root_ref": "0" * 64, "targets": []})
        await submit(host, folder, ["submit", "--commit", builtin, "--plan", str(wrong)],
                     "programmatic:wsl-stale", status="failed_preflight")
        assert host.ready() == old
        print("PREFLIGHT refused a stale Root without restarting", flush=True)

        await verify_receipt_write_failure(host, folder, builtin, bad_session)

        builtin_session = "programmatic:wsl-builtin"
        builtin_job, before = await submit(host, folder, ["submit", "--commit", builtin], builtin_session)
        assert before["preflight"]["boot_probe"]["conversation"] == "passed"
        assert len(await report_items(host, builtin_session, builtin_job.stem, "发布成功")) == 1
        snapshot = await plugins(host)
        assert snapshot["jobs@release"]["version"] == "9.0.2"
        assert snapshot["release_probe@local"]["input_ref"] == hot_input
        assert before["ready"]["bootId"] != first["bootId"]
        async with await connect(host) as client:
            history = (await client.message_read(builtin_session, limit=100))["items"]
        assert any(item["id"] == builtin_session + ":input" for item in history)
        print("BUILTIN active and external input preserved", flush=True)

        mixed, _, _ = compiled[2]
        sha, bundle = probe[1]
        target = {"plugin_id": "release_probe@local", "bundle_relative_path": bundle.name,
                  "bundle_sha256": sha256(bundle), "target_commit": sha}
        plan = _plan(host, folder / "mixed.json", [target])
        hold.touch()
        job, failed = await submit(host, folder, ["submit", "--commit", mixed, "--plan", str(plan), "--inputs", str(folder)],
                                   "programmatic:wsl-mixed", status="selected_not_started")
        assert host.selection.read() == failed["publication"]["new_root_ref"]
        assert failed["recovery"]["status"] == "skipped" and "外置" in failed["recovery"]["error"]
        assert "rollback" not in failed and (await asyncio.to_thread(host.service))["SubState"] == "auto-restart"
        assert read_json(host.launch_path) == {"commit": mixed, "root": failed["publication"]["new_root_ref"]}
        print("EXTERNAL CHANGE preserved the published candidate for manual recovery", flush=True)
        if desktop:
            await desktop_notice(host, job.stem)
        # Recreate the durable launch state immediately after publication and before activation.
        write_json(host.launch_path, {"commit": builtin, "blocked": job.stem})
        hold.unlink()
        await asyncio.to_thread(run, *cli, "resume", job.stem)
        assert len(await report_items(host, "programmatic:wsl-mixed", job.stem, "发布失败")) >= 1
        print("REPORT replayed the original failure after chat recovered", flush=True)
        after = await plugins(host)
        assert after["jobs@release"]["version"] == "9.0.3" and after["release_probe@local"]["version"] == "2.0.0"
        assert host.selection.read() == failed["publication"]["new_root_ref"]
        print("MIXED failed startup resumed without republishing", flush=True)

        await asyncio.to_thread(run, *cli, "rollback", job.stem, "--compatible-data")
        restored = await plugins(host)
        assert restored["jobs@release"]["version"] == "9.0.2" and restored["release_probe@local"]["version"] == "1.0.0"
        assert host.ready()["sourceCommit"] == builtin
        async with await connect(host) as client:
            original = await client.message_read(builtin_session, limit=100)
            assert original["items"] == history
        print("ROLLBACK restored both inputs and retained the complete original conversation", flush=True)

        _plan(host, plan, [target])
        hold.touch()
        job, _ = await submit(host, folder, ["submit", "--commit", mixed, "--plan", str(plan), "--inputs", str(folder)],
                              "programmatic:wsl-rollback", status="selected_not_started")
        hold.unlink()
        await asyncio.to_thread(run, *cli, "rollback", job.stem, "--compatible-data")
        await report_items(host, "programmatic:wsl-rollback", job.stem, "已回退")
        restored = await plugins(host)
        assert restored["jobs@release"]["version"] == "9.0.2" and restored["release_probe@local"]["version"] == "1.0.0"
        assert host.ready()["sourceCommit"] == builtin
        requests = json.loads((folder / "requests.json").read_text())
        assert any("deploy-akashic" in str(message.get("content")) and "原生 WSL" in str(message.get("content"))
                   for request in requests for message in request["messages"] if message["role"] in {"system", "developer"})
        print("ROLLBACK recovered directly from a failed startup", flush=True)

        await verify_automatic_rollback(host, folder, mixed, compiled[4][0], fail_candidate, cli, bad_session)

        # A real cleanup failure can leave the plugin journal pending. Run this case last.
        async with await connect(host) as client:
            earlier = (await client.message_read(bad_session, limit=100))["items"]
        old = host.ready()
        marker = host.workspace / "plugin-data/release_probe-local/fail-stop-once"
        marker.touch(exist_ok=False)
        failed_stop_session = "programmatic:wsl-stop-failure"
        stop_job, failed_stop = await submit(host, folder, ["submit", "--commit", mixed],
                                             failed_stop_session, status="failed_stop")
        assert not marker.exists() and "关闭回执" in failed_stop["error"]
        assert not (host.workspace / "runtime/closed" / f"{old['bootId']}.json").exists()
        assert "publication" not in failed_stop and "rollback" not in failed_stop
        assert failed_stop["recovery"]["status"] == "restored", failed_stop
        recovered = host.ready()
        assert failed_stop["recovery"]["ready"] == recovered
        assert recovered["bootId"] != old["bootId"] and recovered["invocation"] != old["invocation"]
        assert recovered["sourceCommit"] == old["sourceCommit"] and recovered["root"] == old["root"]
        assert read_json(host.launch_path) == {"commit": old["sourceCommit"]}
        assert read_json(host.root / "active.json") == recovered
        assert len(await report_items(host, failed_stop_session, stop_job.stem, "发布失败，旧版已恢复")) == 1
        async with await connect(host) as client:
            assert (await client.message_read(bad_session, limit=100))["items"] == earlier
        print("STOP FAILURE restored the old release without publication and reported it to the same session", flush=True)

        retry_job, retry = await submit(host, folder, ["submit", "--commit", mixed],
                                         "programmatic:wsl-stop-failure-retry", status="failed_preflight")
        assert "尚未结算" in retry["error"] and "publication" not in retry
        assert host.ready() == recovered
        assert len(await report_items(host, "programmatic:wsl-stop-failure-retry", retry_job.stem, "尚未结算")) == 1
        print("STOP RETRY refused another shutdown while the cleanup journal was pending", flush=True)
    finally:
        await server.cleanup()
        for path in (host.root / "run/self-deploy").glob("*.json"):
            subprocess.run(["systemctl", "--user", "stop", f"akashic-wsl-report-{path.stem}.timer"],
                           capture_output=True, check=False, timeout=10)
        if unit_file.exists():
            try:
                run("systemctl", "--user", "stop", unit)
            finally:
                unit_file.unlink()
                run("systemctl", "--user", "daemon-reload")


def _plan(host: Host, path: Path, targets: list[dict[str, Any]]) -> Path:
    write_json(path, {"schema_version": 1, "expected_root_ref": host.selection.read(), "targets": targets})
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", type=Path)
    parser.add_argument("--reuse", type=Path, help="Reuse built fixture releases in an earlier evidence directory")
    parser.add_argument("--desktop", action="store_true", help="Show an actual failure notification on this Windows desktop")
    parser.add_argument("--auto-rollback-only", action="store_true")
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="ak-wsl-"))
    print("EVIDENCE", folder, flush=True)
    if args.reuse:
        fixture = args.reuse.resolve(strict=True)
        commits = run("git", "-C", str(fixture / "source"), "log", "-5", "--reverse", "--format=%H").splitlines()
        compiled = [(sha, fixture / sha / "distribution", fixture / sha / "core") for sha in commits]
    else:
        compiled = releases(folder, args.wheels.resolve(strict=True))
    asyncio.run(scenario(folder, compiled, external(folder), desktop=args.desktop,
                         auto_rollback_only=args.auto_rollback_only))
