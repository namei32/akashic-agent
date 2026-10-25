#!/usr/bin/env python3
"""安装 Windows Stop Hook 与 WSL 用户服务；保留其他 Hook，不修改 notify。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import tempfile
from uuid import uuid4

MARKER = "Akashic：保存 Codex 动态"
UNIT = "akashic-codex-notifications.service"
ROOT = Path(__file__).resolve().parents[2]


def write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"拒绝替换符号链接：{path}")
    old = path.read_bytes() if path.exists() else None
    if old == body:
        return
    if old is not None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + ".before-akashic-" + stamp)
        with backup.open("xb") as file:
            file.write(old)
        backup.chmod(0o600)
    descriptor, temporary = tempfile.mkstemp(prefix=".akashic-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(body)
            file.flush()
            os.fsync(file.fileno())
        current = path.read_bytes() if path.exists() else None
        if current != old:
            raise ValueError(f"配置在准备期间被其他程序修改：{path}")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def artifact(destination: Path) -> tuple[Path, str]:
    sources = {name: (ROOT / "integrations/codex_notifications" / name).read_bytes()
               for name in ("bridge.py", "outbox.py")}
    sdk = ROOT / "sdk/python"
    for path in [sdk / "pyproject.toml", *sorted((sdk / "src").rglob("*.py"))]:
        sources["sdk/" + str(path.relative_to(sdk))] = path.read_bytes()
    identity = hashlib.sha256(b"".join(name.encode() + b"\0" + sources[name]
                                     for name in sorted(sources))).hexdigest()
    target = destination / identity
    if target.is_symlink():
        raise ValueError("桥接制品目录不能是符号链接")
    target.mkdir(parents=True, exist_ok=True)
    for name, data in sources.items():
        path = target / name
        if path.is_symlink():
            raise ValueError(f"制品文件不能是符号链接：{path}")
        if path.exists() and path.read_bytes() != data:
            raise ValueError(f"已保存的桥接制品被修改：{path}")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    python = target / "env/bin/python"
    ready = target / "ready.json"
    if not ready.exists():
        subprocess.run([sys.executable, "-m", "venv", str(target / "env")], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--no-deps",
                        "--disable-pip-version-check", str(target / "sdk")], check=True)
        ready.write_text(json.dumps({"sha256": identity}))
    subprocess.run([str(python), "-c", "from akashic_sdk import AsyncAkashic"], check=True)
    return target, identity


def hooks(previous: dict, command: str, windows: str) -> dict:
    # 相同 marker 只认本工具创建的 bridge.py collect 命令；保留全部其他定义。
    result = json.loads(json.dumps(previous))
    events = result.setdefault("hooks", {})
    if not isinstance(events, dict) or not isinstance(events.get("Stop", []), list):
        raise ValueError("现有 Hook 配置的 hooks/Stop 格式不兼容")
    retained = []
    for group in events.get("Stop", []):
        if not isinstance(group, dict):
            raise ValueError("现有 Stop Hook group 不是对象")
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            raise ValueError("现有 Stop Hook handlers 不是数组")
        for handler in handlers:
            if not isinstance(handler, dict):
                raise ValueError("现有 Stop Hook handler 不是对象")
            if handler.get("statusMessage") == MARKER:
                if "bridge.py" not in handler.get("command", "") or " collect " not in handler["command"]:
                    raise ValueError("已有同名 Hook 不属于本通知桥，请先核对")
        owned = any(handler.get("statusMessage") == MARKER for handler in handlers)
        if owned:
            group["hooks"] = [handler for handler in handlers if handler.get("statusMessage") != MARKER]
        if not owned or group.get("hooks") or set(group) != {"hooks"}:
            retained.append(group)
    events["Stop"] = retained
    events["Stop"].append({"hooks": [{"type": "command", "command": command,
        "commandWindows": windows, "timeout": 30, "statusMessage": MARKER}]})
    return result


def install_plugin(endpoint: str, destination: Path) -> None:
    """复用正式制品构建器和现有安装 RPC；只更新通知插件。"""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "sdk/python/src"))
    from akashic_sdk import AsyncAkashic
    from scripts.build_plugin_distribution import _bundle_plugin, git
    if git(ROOT, "status", "--porcelain", "--", "plugins/codex_notifications").strip():
        raise ValueError("通知插件源码尚未提交，拒绝安装不明确的版本")
    commit = git(ROOT, "rev-parse", "HEAD").decode().strip()
    output = destination / "plugins" / commit
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / "codex_notifications.bundle"
    manifest = output / "bundle.json"
    if not manifest.exists():
        stamp = git(ROOT, "show", "-s", "--format=%cI", commit).decode().strip()
        record = _bundle_plugin(ROOT, commit, stamp, "plugins/codex_notifications", output, set())
        write(manifest, json.dumps(record).encode())
    record = json.loads(manifest.read_text())
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != record["sha256"]:
        raise ValueError("已准备的插件包摘要不匹配")
    operation_path = output / "operation.json"
    existing_operation = operation_path.exists()
    if existing_operation:
        saved_operation = json.loads(operation_path.read_text())
        if not isinstance(saved_operation, dict) or not isinstance(saved_operation.get("update_id"), str):
            raise ValueError("已保存的插件安装身份无效")
        operation: dict[str, object] = {"update_id": saved_operation["update_id"]}
    else:
        operation = {"update_id": "codex-notifications-" + uuid4().hex}
        write(operation_path, json.dumps(operation).encode())
    async def activate():
        async with asyncio.timeout(90):
            async with await AsyncAkashic.connect(endpoint) as client:
                if not existing_operation:
                    await client.request("plugin/install", {**operation, "source": str(bundle),
                        "marketplace": "local", "ref": "", "sparse": []})
                while True:
                    status = await client.request("plugin/update", operation)
                    if not isinstance(status, dict):
                        raise RuntimeError("插件安装回执不是对象")
                    if status["state"] == "active":
                        return
                    if status["state"] in ("failed", "rejected", "cancelled"):
                        raise RuntimeError(f"通知插件未激活，安装操作 {operation['update_id']}：{status}")
                    await asyncio.sleep(1)
    try:
        asyncio.run(activate())
    except TimeoutError:
        raise TimeoutError(f"插件安装尚未确认完成：{operation['update_id']}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--windows-codex-home", type=Path, required=True)
    parser.add_argument("--distro", required=True)
    parser.add_argument("--wsl-user", default=getpass.getuser())
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/akashic-codex")
    parser.add_argument("--install-root", type=Path, default=Path.home() / ".local/share/akashic-codex")
    parser.add_argument("--systemd-dir", type=Path, default=Path.home() / ".config/systemd/user")
    parser.add_argument("--install-plugin", action="store_true", help="通过正式安装接口安装当前提交的通知插件")
    parser.add_argument("--no-start", action="store_true", help="只准备配置，供隔离验收或手动启动")
    args = parser.parse_args()
    if "microsoft" not in platform.release().lower():
        parser.error("此安装器只能在目标 WSL 内运行")
    if not re.fullmatch(r"[A-Za-z0-9_. -]+", args.distro) or not re.fullmatch(r"[A-Za-z0-9_-]+", args.wsl_user):
        parser.error("WSL 发行版或用户名称无效")
    for name in ("workspace", "windows_codex_home", "state", "install_root", "systemd_dir"):
        path = getattr(args, name).expanduser().resolve()
        if any(char in str(path) for char in '\n\r"%`$'):
            parser.error("安装路径不能含换行、引号或命令展开字符")
        setattr(args, name, path)
    if not args.windows_codex_home.is_dir():
        parser.error("Windows Codex 配置目录不存在，请先启动桌面 Codex")
    if not args.no_start and args.systemd_dir != Path.home() / ".config/systemd/user":
        parser.error("自定义 systemd 目录只能配合 --no-start")
    endpoint = str(args.workspace / "akashic.sock")
    if args.install_plugin:
        install_plugin(endpoint, args.install_root)
    target, identity = artifact(args.install_root)
    python = str(target / "env/bin/python")
    probe = subprocess.run([python, "-c",
        "import json,sys; from akashic_sdk import Akashic; "
        "c=Akashic.connect(sys.argv[1]); "
        "print(json.dumps(c.request('codex/notifications/status',{}))); c.close()", endpoint],
        capture_output=True, text=True, timeout=15)
    if probe.returncode:
        raise RuntimeError("Akashic 通知插件尚未就绪，未修改 Hook 或启动转发服务：" + probe.stderr[-1000:])
    status = json.loads(probe.stdout)
    if status.get("schema_version") != 1 or status.get("deleted"):
        raise ValueError("通知插件协议不匹配或 Codex 动态会话已删除")
    sys.path.insert(0, str(target))
    from outbox import Outbox
    box = Outbox(args.state)
    try:
        box.bind(endpoint)
    finally:
        box.close()
    arguments = [python, str(target / "bridge.py"), "collect", "--state", str(args.state)]
    windows = "wsl.exe " + " ".join('"' + value + '"' for value in
        ["--distribution", args.distro, "--user", args.wsl_user, "--exec", *arguments])
    hook_path = args.windows_codex_home / "hooks.json"
    previous = json.loads(hook_path.read_text(encoding="utf-8-sig")) if hook_path.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError("现有 hooks.json 不是对象")
    updated = hooks(previous, shlex.join(arguments), windows)
    unit_path = args.systemd_dir / UNIT
    if unit_path.exists() and "Managed by Akashic Codex bridge" not in unit_path.read_text():
        raise ValueError("同名 systemd 服务不属于此通知桥")
    def systemd_argument(value: str) -> str:
        return json.dumps(value.replace("%", "%%").replace("$", "$$"), ensure_ascii=False)
    service_args = [python, str(target / "bridge.py"), "run", "--state", str(args.state), "--endpoint", endpoint]
    unit = ("# Managed by Akashic Codex bridge\n[Unit]\nDescription=Akashic Codex notifications\n"
            "[Service]\nType=simple\nUMask=0077\nEnvironment=PYTHONUNBUFFERED=1\nExecStart="
            + " ".join(map(systemd_argument, service_args))
            + "\nRestart=on-failure\nRestartSec=5\n[Install]\nWantedBy=default.target\n")
    write(unit_path, unit.encode())
    write(hook_path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode())
    details = {"artifact": str(target), "sha256": identity, "endpoint": endpoint,
               "state": str(args.state), "hooks_file": str(hook_path), "unit": str(unit_path),
               "session_id": status["session_id"], "hook_trust": "review_in_codex_required"}
    write(args.state / "installation.json", (json.dumps(details, ensure_ascii=False, indent=2) + "\n").encode())
    if not args.no_start:
        if args.systemd_dir != Path.home() / ".config/systemd/user":
            raise ValueError("自定义 systemd 目录只能配合 --no-start")
        env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
        for action in (["daemon-reload"], ["enable", UNIT], ["restart", UNIT], ["is-active", "--quiet", UNIT]):
            subprocess.run(["systemctl", "--user", *action], env=env, check=True)
    print(json.dumps(details, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"安装未完成：{error}", file=sys.stderr)
        raise SystemExit(1)
