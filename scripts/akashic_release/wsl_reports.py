"""Reconcile host release receipts independently of Core and of the release worker."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

from scripts.akashic_release.manifest import read_json, release_lock, write_json
from scripts.akashic_release.self_deploy import _user_environment

if TYPE_CHECKING:
    from scripts.akashic_release.wsl import Host


def unit_name(request_id: str) -> str:
    return f"akashic-wsl-report-{request_id}"


def watch(host: Host, request_id: str) -> None:
    if unit_state(unit_name(request_id) + ".timer") in {"active", "activating"}:
        return
    subprocess.run([
        "systemd-run", "--user", "--collect", "--on-active=1s", "--on-unit-inactive=5s",
        "--timer-property=AccuracySec=1s", "--property=TimeoutStartSec=30s",
        f"--unit={unit_name(request_id)}", f"--working-directory={Path(__file__).resolve().parents[2]}",
        f"--setenv=HOME={Path.home()}", sys.executable, "-B", "-m", "scripts.akashic_release.wsl",
        "--profile", str(host.profile), "report", request_id,
    ], env=_user_environment(), check=True, capture_output=True, text=True, timeout=20)


def unit_state(unit: str) -> str:
    result = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ActiveState", "--value"],
                            env=_user_environment(), capture_output=True, text=True, timeout=8)
    if result.returncode and not result.stdout.strip():
        raise RuntimeError("无法读取用户 systemd 状态")
    return result.stdout.strip()


def reconcile(host: Host, request_id: str) -> dict[str, Any]:
    path = host.record_path(request_id)
    with release_lock(host.root / "notices" / f"{request_id}.lock", wait=True):
        record = read_json(path)
        request = record["request"]
        phase, error = record["status"], record.get("error")
        if phase not in {"active", "rolled_back", "failed", "failed_preflight", "failed_stop"} and not error:
            if time.time() - path.stat().st_mtime < 35 or unit_state(request["unit"]) in {"active", "activating", "deactivating"}:
                return {"status": "waiting", "phase": phase}
            error = "宿主 worker 已退出，缺少完整发布回执"
            if original_error := record.get("stop_error") or record.get("startup_error"):
                error = f"{original_error}。{error}"
        folder = host.root / "notices" / request_id
        enqueue(host, request_id, record, folder, error)
        deliveries = sorted(folder.glob("*.json"), key=lambda file: file.stat().st_mtime_ns)
        results = [deliver(host, request_id, request["stop"], file) for file in deliveries]
        return {"chat": all(item["chat"] for item in results), "reports": results}


def enqueue(host: Host, request_id: str, record: dict[str, Any], folder: Path, error: str | None) -> None:
    request, phase = record["request"], record["status"]
    detail = str(error or "").strip()[:1200]
    identity = hashlib.sha256(f"{request_id}\n{phase}\n{detail}".encode()).hexdigest()
    receipt = folder / f"{identity}.json"
    if receipt.exists():
        return
    try:
        running = host.ready()["sourceCommit"]
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        running = "未知"
    selected = record.get("rollback" if phase == "rolled_back" else "publication", {}).get("new_root_ref")
    recovery = record.get("recovery", {})
    if phase == "active" and not error:
        title = "Akashic 发布成功"
    elif phase == "rolled_back" and not error:
        title = "Akashic 已回退"
    elif phase in {"failed_stop", "rolled_back"} and recovery.get("status") == "restored":
        title = "Akashic 发布失败，旧版已恢复"
    else:
        title = "Akashic 发布失败"
    if not host.system:
        title = "[隔离验收] " + title
    text = (f"{title}\n任务 {request_id}，阶段 {phase}。\n"
            f"目标 {request['commit'][:12]}，运行版本 {running[:12]}。")
    if selected:
        text += f"\n已提交插件组合 {selected[:12]}。"
    if detail:
        text += f"\n原因：{detail}"
    if recovery_error := recovery.get("error"):
        text += f"\n未能自动恢复旧版：{str(recovery_error)[:400]}"
    write_json(receipt, {"title": title, "text": text, "chat": False})


def preserve(host: Host, request_id: str, record: dict[str, Any]) -> None:
    """Keep the failed attempt even if recovery wins the race with the timer."""
    if error := record.get("error"):
        with release_lock(host.root / "notices" / f"{request_id}.lock", wait=True):
            enqueue(host, request_id, record, host.root / "notices" / request_id, error)


def deliver(host: Host, request_id: str, stop: dict[str, Any], receipt: Path) -> dict[str, Any]:
    delivery = read_json(receipt)
    notification = host.root / "desktop-notifications" / receipt.name
    if delivery.get("desktop") == "queued" and notification.with_suffix(".ack").exists():
        delivery["desktop"] = "shown"
        write_json(receipt, delivery)
    if not delivery["chat"]:
        try:
            if not host.chat_ready():
                raise RuntimeError("Web Chat 尚未就绪")
            status = host.rpc("plugin/status", {}, timeout=4)
            if not any(row["plugin_id"].split("@", 1)[0] == "reply" and row["state"] == "active"
                       for row in status["plugins"]):
                raise RuntimeError("聊天回复组件不可用")
            call = {k: stop[k] for k in ("boot_id", "session_id", "call_message_id", "call_part_index")}
            ack = host.rpc("runtime/release-report", {**call, "request_id": request_id, "event_id": receipt.stem,
                                                      "text": delivery["text"]}, timeout=6)
            if ack["status"] != "committed" or ack["session_id"] != call["session_id"]:
                raise RuntimeError("原会话未确认发布报告")
            delivery.update(chat=True, message_id=ack["message_id"])
        except Exception as delivery_error:
            delivery["chat_error"] = str(delivery_error)[:300]
            if host.desktop_notifications:
                if not notification.exists():
                    write_json(notification, {"id": receipt.stem, "title": delivery["title"], "text": delivery["text"]})
                delivery["desktop"] = "shown" if notification.with_suffix(".ack").exists() else "queued"
        write_json(receipt, delivery)
    return delivery


def stop_timer(request_id: str) -> None:
    name = unit_name(request_id) + ".timer"
    if unit_state(name) in {"active", "activating"}:
        subprocess.run(["systemctl", "--user", "stop", name], env=_user_environment(), check=True, timeout=10)
