"""在 WSL 隔离目录实际安装通知桥，验证 Hook 合并、不可变制品及幂等更新。"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import pwd
import shutil
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "sdk/python/src")]
from codex_notifications_scenario import configure
from tests.test_default_reply import application


async def run(directory: Path) -> dict:
    directory.mkdir()
    home, windows = directory / "home", directory / "windows-codex"
    home.mkdir(); windows.mkdir()
    os.environ.update(HOME=str(home), AKASHIC_PLUGIN_HOME=str(home), AKASHIC_PLUGIN_DISTRIBUTION="",
                      AKASHIC_EXECUTION_MODE="local")
    original = {"description": "keep existing hooks", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "/bin/true", "statusMessage": "original"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "/bin/true"}]}]}}
    hook_file = windows / "hooks.json"
    hook_file.write_text(json.dumps(original))
    notify = b'notify = ["existing-notifier"]\n'
    (windows / "config.toml").write_bytes(notify)
    state, artifacts, units = directory / "state", directory / "artifacts", directory / "units"
    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu-24.04")
    wsl_user = pwd.getpwuid(os.getuid()).pw_name

    async def install(workspace):
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(ROOT / "integrations/codex_notifications/install_wsl.py"), "--workspace", str(workspace),
            "--windows-codex-home", str(windows), "--distro", distro, "--wsl-user", wsl_user,
            "--state", str(state), "--install-root", str(artifacts), "--systemd-dir", str(units), "--no-start", "--install-plugin",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), 90)
        return process.returncode, out.decode(), err.decode()

    def without_receiver(sources):
        configure(sources)
        shutil.rmtree(sources / "codex_notifications")

    async with application(directory, replying=False, extra_sources=without_receiver):
        code, out, err = await install(directory / "workspace")
        assert code == 0, (out, err)
        details = json.loads(out.splitlines()[-1])
        updated = json.loads(hook_file.read_text())
        assert updated["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]
        assert updated["hooks"]["Stop"][0] == original["hooks"]["Stop"][0]
        added = updated["hooks"]["Stop"][1]["hooks"][0]
        assert 'wsl.exe' in added["commandWindows"] and distro in added["commandWindows"]
        assert added["type"] == "command" and ' collect ' in added["command"]
        assert (windows / "config.toml").read_bytes() == notify
        before_hook, before_unit = hook_file.read_bytes(), Path(details["unit"]).read_bytes()
        assert len(list(windows.glob('hooks.json.before-akashic-*'))) == 1
        assert (await install(directory / "workspace"))[0] == 0
        assert hook_file.read_bytes() == before_hook
        assert len(list(windows.glob('hooks.json.before-akashic-*'))) == 1
        # 通过真正的 Windows PowerShell 执行生成的 commandWindows；stdin 经过 wsl.exe。
        command = "$ProgressPreference='SilentlyContinue'; " + added["commandWindows"]
        encoded = base64.b64encode(command.encode("utf-16-le")).decode()
        process = await asyncio.create_subprocess_exec(
            "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        hook_event = {"hook_event_name": "Stop", "session_id": "native-windows",
            "turn_id": "setup-check", "cwd": str(ROOT), "last_assistant_message": "原生 Windows 命令验证"}
        output, errors = await asyncio.wait_for(process.communicate(json.dumps(hook_event).encode()), 30)
        assert process.returncode == 0 and output.strip() == b"{}", (output, errors)
        python = str(Path(details["artifact"]) / "env/bin/python")
        process = await asyncio.create_subprocess_exec(python, str(Path(details["artifact"]) / "bridge.py"),
            "drain", "--state", str(state), "--endpoint", str(directory / "workspace/akashic.sock"), "--now",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        output, errors = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0 and json.loads(output)["committed"] == 1, (output, errors)
        assert (await install(directory / "missing-runtime"))[0] == 1
        assert hook_file.read_bytes() == before_hook and Path(details["unit"]).read_bytes() == before_unit
        saved_bridge = Path(details["artifact"]) / "bridge.py"
        saved_bridge.write_text(saved_bridge.read_text() + '\n# scenario tampering\n')
        assert (await install(directory / "workspace"))[0] == 1
        assert saved_bridge.read_text().endswith('# scenario tampering\n')
        assert hook_file.read_bytes() == before_hook
    return {"real_wsl_install": True, "native_windows_hook_command": True, "plugin_install_and_repeat": True, "existing_hooks_and_notify_preserved": True,
            "windows_command_generated": True, "idempotent_install": True,
            "unavailable_receiver_does_not_change_hooks": True, "changed_artifact_rejected": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.output.resolve()))
    (args.output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
