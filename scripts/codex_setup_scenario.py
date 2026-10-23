"""在 WSL 隔离目录实际安装通知桥，验证 Hook 合并、不可变制品及幂等更新。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
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

    async def install(workspace):
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(ROOT / "integrations/codex_notifications/install_wsl.py"), "--workspace", str(workspace),
            "--windows-codex-home", str(windows), "--distro", "Ubuntu-24.04", "--wsl-user", "namei",
            "--state", str(state), "--install-root", str(artifacts), "--systemd-dir", str(units), "--no-start",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), 90)
        return process.returncode, out.decode(), err.decode()

    async with application(directory, replying=False, extra_sources=configure):
        code, out, err = await install(directory / "workspace")
        assert code == 0, (out, err)
        details = json.loads(out.splitlines()[-1])
        updated = json.loads(hook_file.read_text())
        assert updated["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]
        assert updated["hooks"]["Stop"][0] == original["hooks"]["Stop"][0]
        added = updated["hooks"]["Stop"][1]["hooks"][0]
        assert 'wsl.exe' in added["commandWindows"] and 'Ubuntu-24.04' in added["commandWindows"]
        assert added["type"] == "command" and ' collect ' in added["command"]
        assert (windows / "config.toml").read_bytes() == notify
        before_hook, before_unit = hook_file.read_bytes(), Path(details["unit"]).read_bytes()
        assert len(list(windows.glob('hooks.json.before-akashic-*'))) == 1
        assert (await install(directory / "workspace"))[0] == 0
        assert hook_file.read_bytes() == before_hook
        assert len(list(windows.glob('hooks.json.before-akashic-*'))) == 1
        assert (await install(directory / "missing-runtime"))[0] == 1
        assert hook_file.read_bytes() == before_hook and Path(details["unit"]).read_bytes() == before_unit
        saved_bridge = Path(details["artifact"]) / "bridge.py"
        saved_bridge.write_text(saved_bridge.read_text() + '\n# scenario tampering\n')
        assert (await install(directory / "workspace"))[0] == 1
        assert saved_bridge.read_text().endswith('# scenario tampering\n')
        assert hook_file.read_bytes() == before_hook
    return {"real_wsl_install": True, "existing_hooks_and_notify_preserved": True,
            "windows_command_generated": True, "idempotent_install": True,
            "unavailable_receiver_does_not_change_hooks": True, "changed_artifact_rejected": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.output.resolve()))
    (args.output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
