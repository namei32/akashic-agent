"""Publish prepared native releases through the WSL service owner."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from agent.plugins.distribution_sources import is_distribution_owned
from agent.plugins.files import encode_tree, tree_entries
from agent.plugins.reload_journal import check_pending_publication
from agent.plugins.selection import PluginSelection
from bootstrap.workspace_lock import PluginPublicationLock, WorkspaceMaintenanceLock
from plugins.gateway.runtime_stop import StopParams
from scripts.akashic_release.manifest import read_json, release_lock, write_json
from scripts.akashic_release.self_deploy import _request_path, _user_environment, stop_rpc
from scripts.install_plugin_distribution import extract_core, load_deployment_plan, verify_distribution
from scripts.release_boot_probe import failed_plugins
from scripts.akashic_release import wsl_reports

DEFAULT_PROFILE = Path("/etc/akashic/wsl-release.json")
BRIDGE = "/usr/local/libexec/akashic-wsl-service"


def run(*argv: str, timeout: float = 120) -> str:
    return subprocess.check_output(argv, text=True, stdin=subprocess.DEVNULL, timeout=timeout).strip()


def exact(value: str) -> str:
    if not re.fullmatch("[a-f0-9]{40}", value):
        raise ValueError("发行版必须指定完整的 40 位 Git SHA")
    return value


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cgroup(pid: int) -> str:
    return Path(f"/proc/{pid}/cgroup").read_text().strip().removeprefix("0::")


class Host:
    def __init__(self, profile: Path):
        self.profile = profile.resolve(strict=True)
        settings = read_json(self.profile)
        self.root = Path(settings["root"]).resolve(strict=True)
        self.state = Path(settings["state"]).resolve(strict=True)
        self.workspace = self.state / "workspace"
        self.port = int(settings["port"])
        self.unit: str = settings["unit"]
        self.repository = settings.get("repository")
        self.system = self.unit == "akashic.service"
        self.desktop_notifications = settings.get("desktop_notifications", self.system) is True
        if not self.system and not re.fullmatch(r"akashic-wsl-[a-z0-9-]+\.service", self.unit):
            raise ValueError("服务必须是受管的 Akashic WSL unit")
        if self.system and (self.profile.stat().st_uid != 0 or self.profile.stat().st_mode & 0o022):
            raise PermissionError("系统服务要求 root 持有的只读发布配置")
        if not 1 <= self.port <= 65535 or self.root == self.state or self.root.is_relative_to(self.state):
            raise ValueError("发布目录须独立于业务状态，Web 端口须有效")
        self.launch_path = self.root / "launch.json"
        self.lock = self.root / "release.lock"
        self.selection = PluginSelection(self.workspace)

    def record_path(self, request_id: str) -> Path:
        return _request_path(self.root, request_id)

    def service(self) -> dict[str, str]:
        if self.system:
            return json.loads(run(BRIDGE, "status"))["service"]
        return dict(line.split("=", 1) for line in run(
            "systemctl", "--user", "show", self.unit,
            "-p", "Id,ActiveState,SubState,MainPID,InvocationID,ControlGroup,Result,KillMode",
        ).splitlines())

    def control(self, verb: str, invocation: str = "") -> None:
        self.require_independent()
        if verb == "stop":
            status = self.service()
            if status["InvocationID"] != invocation or status["ActiveState"] != "active":
                raise RuntimeError("旧 systemd invocation 已变化")
        if self.system:
            if verb == "start" and self.service()["ActiveState"] == "failed":
                run("/usr/bin/sudo", "-n", "/usr/bin/systemctl", "reset-failed", self.unit)
            run("/usr/bin/sudo", "-n", BRIDGE, verb, *((invocation,) if verb == "stop" else ()), timeout=170)
        else:
            run("systemctl", "--user", verb, self.unit, timeout=170)

    def require_independent(self) -> None:
        group = self.service()["ControlGroup"]
        if group and (cgroup(os.getpid()) + "/").startswith(group + "/"):
            raise RuntimeError("切换必须由独立于 Akashic 的宿主进程执行")

    def ready(self) -> dict[str, Any]:
        status = self.service()
        ready = read_json(self.workspace / ".runtime-ready.json")
        if (status["ActiveState"] != "active" or ready["state"] != "ready"
                or cgroup(ready["pid"]) != status["ControlGroup"]
                or status["KillMode"] not in {"mixed", "control-group"}):
            raise RuntimeError("readiness 不属于当前 systemd 服务")
        return {**ready, "invocation": status["InvocationID"], "root": self.selection.read()}

    def release(self, commit: str) -> dict[str, Any]:
        record = read_json(self.root / "releases" / f"{exact(commit)}.json")
        distribution = Path(record["distribution"])
        if sha256(distribution / "distribution.json") != record["manifest"] or record["commit"] != commit:
            raise RuntimeError("发行版清单发生变化")
        if self.core_digest(Path(record["core"])) != record["coreDigest"]:
            raise RuntimeError("已解包 Core 与登记的发行版不符")
        return record

    @staticmethod
    def core_digest(core: Path) -> str:
        return hashlib.sha256(encode_tree(tree_entries(core))).hexdigest()

    def prepare(self, commit: str) -> dict[str, Any]:
        """Build a fixed checkout using the existing native distribution builder."""
        commit = exact(commit)
        if (self.root / "releases" / f"{commit}.json").exists():
            return self.release(commit)
        if not self.repository:
            raise RuntimeError("WSL 发布配置需要指定构建用 Git 仓库")
        with release_lock(self.root / "build.lock"), tempfile.TemporaryDirectory(dir=self.root) as temp:
            source = Path(temp) / "source"
            run("git", "clone", "--quiet", "--shared", "--no-checkout", self.repository, str(source))
            run("git", "-C", str(source), "checkout", "--quiet", "--detach", commit)
            code = (
                "from pathlib import Path; from scripts.start import Preparation, prepare_source; "
                "import json,sys; "
                "print(json.dumps([str(x) for x in prepare_source(Preparation(Path(sys.argv[2])), Path(sys.argv[1]))]))"
            )
            result = subprocess.check_output(
                [sys.executable, "-B", "-c", code, str(self.root / "build-cache"),
                 str(self.root / f"prepare-{commit}.log")], cwd=source, text=True, timeout=3600,
            )
            core, dist, python = map(Path, json.loads(result.splitlines()[-1]))
            return self.register(argparse.Namespace(core=core, distribution=dist, python=python, adopt=False))

    def register(self, args: argparse.Namespace) -> dict[str, Any]:
        """Register installed artifacts from the existing distribution builder."""
        distribution = args.distribution.resolve(strict=True)
        report = verify_distribution(distribution)
        commit = exact(report["source_commit"])
        core = args.core.resolve(strict=True)
        if not (core / "main.py").is_file():
            raise RuntimeError("缺少已解包的 Core")
        core_digest = self.core_digest(core)
        with tempfile.TemporaryDirectory() as temp:
            reference = extract_core(distribution, Path(temp) / "core", report=report)
            if self.core_digest(reference) != core_digest:
                raise RuntimeError("Core 文件与正式分发归档不符")
        # Keep the venv path, because resolving its bin/python symlink loses the venv.
        python = args.python.expanduser().absolute()
        if not python.is_file():
            raise RuntimeError("缺少发行版解释器")
        record = {"commit": commit, "distribution": str(distribution), "core": str(core),
                  "python": str(python), "manifest": sha256(distribution / "distribution.json"),
                  "coreDigest": core_digest}
        with release_lock(self.lock):
            path = self.root / "releases" / f"{commit}.json"
            if path.exists() and read_json(path) != record:
                raise RuntimeError("同一提交已有不同制品记录")
            write_json(path, record)
            if args.adopt:
                if self.launch_path.exists() or self.ready()["sourceCommit"] != commit:
                    raise RuntimeError("只可首次接管当前运行中的相同发行版")
                write_json(self.launch_path, {"commit": commit})
        return record

    def launch(self) -> None:
        if os.geteuid() == 0:
            raise PermissionError("Core 必须以普通运行用户启动")
        os.umask(0o077)
        startup_lock = os.open(self.state / ".startup.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(startup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(startup_lock, True)  # The Supervisor owns this lock until it exits.
        pointer = read_json(self.launch_path)
        if pointer.get("blocked") or ("root" in pointer and self.selection.read() != pointer["root"]):
            raise RuntimeError("发布尚未结算，使用 WSL resume 或核对回执")
        release = self.release(pointer["commit"])
        core, python = Path(release["core"]), release["python"]
        os.chdir(core)
        env = {**os.environ, "AKASHIC_RUNTIME_COMMIT": release["commit"],
               "AKASHIC_PLUGIN_DISTRIBUTION": release["distribution"],
               "AKASHIC_PLUGIN_HOME": str(self.state / "plugin-home"),
               "AKASHIC_WEB_HOST": "127.0.0.1", "AKASHIC_WEB_PORT": str(self.port),
               "AKASHIC_WEB_ALLOW_NON_LOOPBACK": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        os.execve(python, [python, "-B", str(core / "main.py"), "--config", str(self.state / "config.toml"),
                           "--workspace", str(self.workspace)], env)

    def rpc(self, method: str, params: dict[str, Any], timeout: int = 30) -> Any:
        async def request() -> Any:
            async with asyncio.timeout(timeout):
                return await stop_rpc(self.workspace, method, params)
        return asyncio.run(request())

    def chat_ready(self) -> bool:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.port}/api/shell/state", timeout=3) as response:
            return json.load(response)["chatReady"] is True

    def submit(self, args: argparse.Namespace) -> dict[str, Any]:
        if args.inputs is not None and args.plan is None:
            raise ValueError("--inputs 只能和 --plan 一起使用")
        release = self.release(args.commit)
        context = json.loads(os.environ.get("AKASHIC_CALL_CONTEXT", "{}"))
        request_id = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:32]
        stop = StopParams.model_validate({**context, "request_id": request_id, "timeout_s": args.timeout})
        old = self.ready()
        if old["bootId"] != stop.boot_id or cgroup(os.getpid()) != self.service()["ControlGroup"]:
            raise RuntimeError("只接纳来自当前 WSL Core 的 Shell 调用")
        if read_json(self.launch_path) != {"commit": old["sourceCommit"]}:
            raise RuntimeError("当前启动入口尚未完成上一次发布")
        self.release(old["sourceCommit"])
        path = self.record_path(request_id)
        unit = f"akashic-wsl-deploy-{request_id}.service"
        plan = None if args.plan is None else str(args.plan.resolve(strict=True))
        inputs = None if args.inputs is None else str(args.inputs.resolve(strict=True))
        request = {"commit": release["commit"], "stop": stop.model_dump(), "old": old, "unit": unit,
                   "plan": plan, "plan_digest": sha256(Path(plan)) if plan else None,
                   "inputs": inputs, "backup": args.backup}
        with release_lock(path.with_suffix(".lock")):
            if path.exists():
                record = read_json(path)
                if record["request"] != request:
                    raise RuntimeError("原 Shell 调用已提交不同的部署请求")
                if record["status"] not in {"accepted", "preflight", "active"}:
                    raise RuntimeError(f"原请求处于 {record['status']}，请查询状态")
                return {"requestId": request_id, "status": record["status"], "targetCommit": release["commit"]}
            record = {"request": request, "status": "submitting", "notify": True}
            write_json(path, record)
            try:
                wsl_reports.watch(self, request_id)
                self.rpc("runtime/prepare-stop", {**request["stop"], "arm_only": True})
                subprocess.run([
                    "systemd-run", "--user", "--collect", f"--unit={unit}", "--property=Type=exec",
                    f"--working-directory={Path(__file__).resolve().parents[2]}",
                    f"--setenv=HOME={Path.home()}", sys.executable, "-B", "-m", "scripts.akashic_release.wsl",
                    "--profile", str(self.profile), "worker", request_id,
                ], check=True, env=_user_environment(), stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=30)
            except Exception as error:
                detail = (error.stderr or str(error)).strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
                write_json(path, {**record, "status": "failed", "error": detail})
                raise
            write_json(path, {**record, "status": "accepted"})
        return {"requestId": request_id, "status": "accepted", "targetCommit": release["commit"]}

    def publish(self, commit: str, plan: Path, inputs: Path, *, preflight: bool = False,
                backup: Path | None = None, boot_probe: bool = False) -> dict[str, Any]:
        release = self.release(commit)
        core, dist = Path(release["core"]), Path(release["distribution"])
        result = subprocess.run([release["python"], "-B", str(core / "scripts/install_plugin_distribution.py"),
                     "--distribution", str(dist), "--bundle", str(dist / "bundles/base.toml"),
                     "--workspace", str(self.workspace), "--plugins-home", str(self.state / "plugin-home"),
                     "--config", str(self.state / "config.toml"), "--publish", "--plan", str(plan),
                     "--inputs", str(inputs), *(("--preflight-only",) if preflight else ()),
                     *(("--boot-probe",) if boot_probe else ()),
                     *(("--backup-dir", str(backup)) if backup else ())],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode:
            raise RuntimeError(result.stderr.rstrip().splitlines()[-1] if result.stderr.strip() else "发行版安装器退出失败")
        return json.loads(result.stdout)

    def activate(self, commit: str, root: str, *, previous_boot_id: str | None = None) -> dict[str, Any]:
        if self.selection.read() != root:
            raise RuntimeError("实际 Root 与发布回执不符")
        write_json(self.launch_path, {"commit": commit, "root": root})
        status = self.service()
        if status["ActiveState"] not in {"active", "activating"}:
            self.control("start")
        ready = self.wait_for_activation(commit, root, previous_boot_id=previous_boot_id,
                                         await_restart=status["SubState"] == "auto-restart")
        write_json(self.launch_path, {"commit": commit})
        return ready

    def wait_for_activation(self, commit: str, root: str, *, previous_boot_id: str | None = None,
                            await_restart: bool = False) -> dict[str, Any]:
        deadline = time.monotonic() + 60
        last = "服务未就绪"
        while time.monotonic() < deadline:
            try:
                ready = self.ready()
                if ready["sourceCommit"] != commit or ready["root"] != root:
                    raise RuntimeError("运行的源码或 Root 不符")
                if ready["bootId"] == previous_boot_id:
                    raise RuntimeError("仍是停机前的 boot")
                snapshot = self.rpc("plugin/status", {}, timeout=3)
                if snapshot["selection_ref"] != root:
                    raise RuntimeError("运行中插件组合与发布回执不符")
                failing = failed_plugins(snapshot)
                if failing:
                    raise RuntimeError("选中插件尚未全部激活：" + json.dumps(failing, ensure_ascii=False)[:1250])
                if not self.chat_ready():
                    raise RuntimeError("Web Chat 尚未就绪")
                write_json(self.root / "active.json", ready)
                return ready
            except (OSError, URLError, RuntimeError, TimeoutError) as error:
                last = str(error)
                status = self.service()
                if (status["ActiveState"] in {"inactive", "failed"}
                        or (not await_restart and status["SubState"] == "auto-restart")):
                    break
                time.sleep(0.3)
        raise RuntimeError(f"启动验收未完成：{last}")

    def restore_old_after_stop_failure(self, old: dict[str, Any]) -> dict[str, Any]:
        """Called under the release lock, before any publication or migration."""
        commit, root = old["sourceCommit"], old["root"]
        status = self.service()
        # systemd can clear the invocation when the stopped unit is collected.
        if (status["InvocationID"] not in {"", old["invocation"]}
                or status["ActiveState"] not in {"inactive", "failed"}
                or status["MainPID"] != "0" or status["ControlGroup"]):
            return {"status": "skipped", "error": "无法确认原 systemd invocation 已退出"}
        maintenance = WorkspaceMaintenanceLock(self.workspace)
        maintenance.acquire()
        try:
            if (not isinstance(root, str) or self.selection.read() != root
                    or read_json(self.launch_path) != {"commit": commit}):
                return {"status": "skipped", "error": "启动指针或插件组合与原版本不符"}
        finally:
            # The original launcher must be able to acquire these same locks.
            maintenance.release()
        self.control("start")
        ready = self.wait_for_activation(commit, root, previous_boot_id=old["bootId"])
        return {"status": "restored", "ready": ready}

    def worker(self, request_id: str) -> dict[str, Any]:
        self.require_independent()
        path = self.record_path(request_id)
        with release_lock(self.lock, wait=True), release_lock(path.with_suffix(".lock"), wait=True):
            record = read_json(path)
            if record["status"] != "accepted":
                raise RuntimeError("仅执行已接纳的 WSL 发布")
            request, folder = record["request"], path.with_suffix("")
            stop, old = request["stop"], request["old"]
            record["status"] = "preflight"
            write_json(path, record)
            try:
                if self.ready() != old:
                    raise RuntimeError("原 boot 或 Root 已变化")
                folder.mkdir()
                targets = []
                if request["plan"]:
                    received = folder / "requested.json"
                    shutil.copyfile(request["plan"], received)
                    if sha256(received) != request["plan_digest"]:
                        raise RuntimeError("清单在接单后发生变化")
                    _, expected, targets, adoption = load_deployment_plan(received)
                    if expected != old["root"] or adoption is not None:
                        raise RuntimeError("清单与当前 Root 不符，首次历史归属转换须走离线入口")
                write_json(folder / "plan.json", {"schema_version": 1, "expected_root_ref": old["root"], "targets": targets})
                inputs = Path(request["inputs"] or self.release(request["commit"])["distribution"])
                record["preflight"] = self.publish(request["commit"], folder / "plan.json", inputs, preflight=True, boot_probe=True)
                self.capture_previous(folder, targets)
                write_json(path, record)
                ack = self.rpc("runtime/prepare-stop", stop, timeout=stop["timeout_s"] + 5)
                if ack["state"] != "drained" or ack["bootId"] != old["bootId"] or self.ready() != old:
                    raise RuntimeError("原实例未完成排空，或者发布基线已变化")
                check_pending_publication(self.workspace)
                previous_inputs = {ref: self.selection.read_input(ref) for ref in self.selection.components(old["root"])}
                record["status"] = "stopping"
                write_json(path, record)
            except Exception as error:
                try:
                    self.rpc("runtime/cancel-stop", stop, timeout=5)
                except Exception as cancel_error:
                    record["cancelError"] = str(cancel_error)
                record.update(status="failed_preflight", error=str(error))
                try:
                    write_json(path, record)
                except OSError as receipt_error:
                    error.add_note(f"无法保存失败回执: {receipt_error}")
                raise
            try:
                self.control("stop", old["invocation"])
                try:
                    closed = read_json(self.workspace / "runtime/closed" / f"{old['bootId']}.json")
                except FileNotFoundError as error:
                    raise RuntimeError("旧实例退出后缺少正常关闭回执") from error
                service = self.service()
                if (closed != {"pid": old["pid"], "bootId": old["bootId"], "state": "closed",
                                "rootIdentity": ack["rootIdentity"]}
                        or service["ActiveState"] != "inactive" or service["Result"] != "success"):
                    raise RuntimeError("未取得同一旧实例的关闭回执")
            except Exception as error:
                record.update(status="recovering_old", stop_error=str(error))
                write_json(path, record)
                try:
                    recovery = self.restore_old_after_stop_failure(old)
                except Exception as recovery_error:
                    recovery = {"status": "failed", "error": str(recovery_error)}
                record.update(status="failed_stop", error=record.pop("stop_error"), recovery=recovery)
                write_json(path, record)
                raise
            try:
                write_json(self.launch_path, {"commit": old["sourceCommit"], "blocked": request_id})
                record["status"] = "publishing"
                write_json(path, record)
                record["publication"] = self.publish(request["commit"], folder / "plan.json", inputs,
                                                     backup=folder / "backup" if request["backup"] else None)
                record["status"] = "selected_not_started"
                write_json(path, record)
            except Exception as error:
                record["error"] = str(error)
                write_json(path, record)
                raise
            return self.finish(path, record, previous_inputs=previous_inputs)

    def rollback_failed_start(self, path: Path, record: dict[str, Any],
                              previous_inputs: dict[str, Mapping[str, object]]) -> dict[str, Any]:
        request, preflight, published = record["request"], record["preflight"], record["publication"]
        old, target = request["old"], request["commit"]
        for receipt in (preflight, published):
            if receipt.get("migration_ids") != []:
                raise RuntimeError("无法确认本次发布没有待执行迁移")
            if receipt.get("target_ids") != []:
                raise RuntimeError("无法确认本次发布没有外置插件变更")
            if (receipt.get("old_root_ref") != old["root"]
                    or receipt.get("distribution_source_commit") != target):
                raise RuntimeError("发布回执与本次请求不符")
        if (preflight.get("status") != "preflight_ok" or published.get("status") != "selected_not_started"
                or not preflight.get("plan_sha256") or preflight["plan_sha256"] != published.get("plan_sha256")):
            raise RuntimeError("缺少与预检一致的完整发布回执")
        self.release(old["sourceCommit"])
        with (self.state / ".startup.lock").open("a+b") as startup:
            fcntl.flock(startup.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            root = self.restore_previous_selection(path, record, previous_inputs)
        return self.activate(old["sourceCommit"], root, previous_boot_id=old["bootId"])

    def restore_previous_selection(self, path: Path, record: dict[str, Any],
                                   previous_inputs: dict[str, Mapping[str, object]]) -> str:
        request, published = record["request"], record["publication"]
        old, target = request["old"], request["commit"]
        candidate, components = published["new_root_ref"], tuple(published["ordered_components"])
        status = self.service()
        group = status["ControlGroup"]
        waiting = status["ActiveState"] == "activating" and status["SubState"] == "auto-restart"
        if status["MainPID"] != "0" or not (waiting or status["ActiveState"] in {"inactive", "failed"}):
            raise RuntimeError("无法确认候选实例已退出")
        if group:
            events = Path("/sys/fs/cgroup" + group) / "cgroup.events"
            try:
                if "populated 0" not in events.read_text().splitlines():
                    raise RuntimeError("候选实例的 cgroup 仍有进程")
            except FileNotFoundError:
                pass
        maintenance = WorkspaceMaintenanceLock(self.workspace)
        publication = PluginPublicationLock(self.state / "plugin-home")
        maintenance.acquire()
        try:
            publication.acquire()
            try:
                if (self.selection.read() != candidate or self.selection.components(candidate) != components
                        or (candidate != old["root"] and not self.selection.transition_committed(old["root"], components))
                        or read_json(self.launch_path) != {"commit": target, "root": candidate}):
                    raise RuntimeError("当前插件组合或启动指针不属于本次发布")
                active = read_json(self.root / "active.json")
                if active["sourceCommit"] != old["sourceCommit"] or active["bootId"] != old["bootId"]:
                    raise RuntimeError("最后一次就绪回执已变化")
                external = {ref for ref in components if not is_distribution_owned(self.selection.read_input(ref))}
                if external != {ref for ref, item in previous_inputs.items() if not is_distribution_owned(item)}:
                    raise RuntimeError("发布前后的外置插件输入不一致")
                check_pending_publication(self.workspace)
                restored = tuple(self.selection.prepare(item) for item in previous_inputs.values())
                if restored != tuple(previous_inputs):
                    raise RuntimeError("此前的插件输入身份不符")
                record["status"] = "rolling_back"
                record["startup_error"] = record.pop("error")
                write_json(path, record)
                root = self.selection.commit(restored, expected_ref=candidate) if restored != components else candidate
                record["rollback"] = {"old_root_ref": candidate, "new_root_ref": root,
                                      "ordered_components": list(restored)}
                write_json(path, record)
                write_json(self.launch_path, {"commit": old["sourceCommit"], "root": root})
            finally:
                publication.release()
        finally:
            maintenance.release()
        return root

    def capture_previous(self, folder: Path, targets: list[dict[str, Any]]) -> None:
        previous = []
        selected = read_json(self.workspace / "runtime/plugin-stable.json")["inputs"].values()
        for target in targets:
            if target.get("bundled"):
                continue
            identity = target["plugin_id"]
            saved, = [row for row in selected if row["plugin_id"] == identity]
            code = saved["code"]
            commit = run("git", "-C", code, "rev-parse", "HEAD")
            run("git", "-C", code, "diff", "--quiet", "HEAD", "--")
            name = f"previous-{len(previous)}.bundle"
            run("git", "-C", code, "bundle", "create", str(folder / name), "HEAD")
            previous.append({"plugin_id": identity, "bundle_relative_path": name,
                             "bundle_sha256": sha256(folder / name), "target_commit": commit})
        write_json(folder / "previous.json", {"targets": previous})

    def finish(self, path: Path, record: dict[str, Any], *,
               previous_inputs: dict[str, Mapping[str, object]] | None = None) -> dict[str, Any]:
        publication = record["publication"]
        try:
            record["ready"] = self.activate(record["request"]["commit"], publication["new_root_ref"])
        except Exception as error:
            record["error"] = str(error)
            if previous_inputs is not None:
                try:
                    ready = self.rollback_failed_start(path, record, previous_inputs)
                except Exception as rollback_error:
                    status = "failed" if record["status"] == "rolling_back" else "skipped"
                    record["recovery"] = {"status": status, "error": str(rollback_error)}
                else:
                    record.update(status="rolled_back", ready=ready, recovery={"status": "restored"})
            record["error"] = str(error)
            try:
                write_json(path, record)
            except OSError as receipt_error:
                error.add_note(f"无法保存启动失败回执: {receipt_error}")
            raise
        record["status"] = "active"
        record.pop("error", None)
        write_json(path, record)
        return record

    def recover(self, args: argparse.Namespace) -> dict[str, Any]:
        self.require_independent()
        path = self.record_path(args.request_id)
        with release_lock(self.lock), release_lock(path.with_suffix(".lock")):
            record = read_json(path)
            if args.command == "resume":
                if record["status"] != "selected_not_started":
                    raise RuntimeError("继续启动需要完整发布回执，先核对实际 Root 与迁移账本")
                wsl_reports.preserve(self, args.request_id, record)
                wsl_reports.watch(self, args.request_id)
                return self.finish(path, record)
            if record["status"] not in {"publishing", "selected_not_started", "active", "rolling_back"}:
                raise RuntimeError("当前阶段没有可恢复的发布选择")
            wsl_reports.preserve(self, args.request_id, record)
            wsl_reports.watch(self, args.request_id)
            current, old = self.selection.read(), record["request"]["old"]
            if current is None:
                raise RuntimeError("实例缺少已提交的插件组合")
            previous_receipt = record.get("rollback")
            if previous_receipt and current == previous_receipt["new_root_ref"]:
                root = current
            else:
                expected = record.get("publication", {}).get("new_root_ref", old["root"])
                if current != expected:
                    raise RuntimeError("实际 Root 超出本次发布证据范围")
                status = self.service()
                if status["ActiveState"] == "active":
                    ready = self.ready()
                    if ready["sourceCommit"] != record["request"]["commit"]:
                        raise RuntimeError("当前运行实例不是回退目标")
                    self.control("stop", status["InvocationID"])
                    closed = read_json(self.workspace / "runtime/closed" / f"{ready['bootId']}.json")
                    if (closed["pid"] != ready["pid"] or closed["state"] != "closed"
                            or self.service()["Result"] != "success"):
                        raise RuntimeError("回退前缺少新版正常关闭回执")
                folder = path.with_suffix("")
                plan = folder / "rollback.json"
                previous = read_json(folder / "previous.json")
                write_json(plan, {"schema_version": 1, "expected_root_ref": current, **previous})
                write_json(self.launch_path, {"commit": old["sourceCommit"], "blocked": args.request_id})
                record["status"] = "rolling_back"
                write_json(path, record)
                receipt = self.publish(old["sourceCommit"], plan, folder)
                record["rollback"] = receipt
                write_json(path, record)
                root = receipt["new_root_ref"]
            record.update(status="rolled_back", ready=self.activate(old["sourceCommit"], root))
            record.pop("error", None)
            record.pop("recovery", None)
            write_json(path, record)
            return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(os.environ.get("AKASHIC_WSL_PROFILE", DEFAULT_PROFILE)))
    commands = parser.add_subparsers(dest="command", required=True)
    for verb in ("status", "worker", "resume", "rollback"):
        command = commands.add_parser(verb)
        command.add_argument("request_id")
        if verb == "rollback":
            command.add_argument("--compatible-data", action="store_true", required=True)
    commands.add_parser("report").add_argument("request_id", nargs="?")
    submit = commands.add_parser("submit")
    submit.add_argument("--commit", required=True)
    submit.add_argument("--plan", type=Path)
    submit.add_argument("--inputs", type=Path)
    submit.add_argument("--timeout", default=600, type=int)
    submit.add_argument("--backup", action="store_true")
    install = commands.add_parser("plugin-install")
    install.add_argument("--source", required=True)
    install.add_argument("--ref", required=True)
    install.add_argument("--marketplace", default="local")
    install.add_argument("--sparse", nargs="*", default=[])
    commands.add_parser("plugin-status").add_argument("update_id")
    commands.add_parser("prepare").add_argument("--commit", required=True)
    register = commands.add_parser("register")
    for field in ("core", "distribution", "python"):
        register.add_argument(f"--{field}", type=Path, required=True)
    register.add_argument("--adopt", action="store_true")
    commands.add_parser("launch")
    args = parser.parse_args(argv)
    host = Host(args.profile)
    match args.command:
        case "launch":
            host.launch()
            return 0
        case "register":
            result = host.register(args)
        case "prepare":
            result = host.prepare(args.commit)
        case "submit":
            result = host.submit(args)
        case "plugin-install":
            with release_lock(host.lock):
                if read_json(host.launch_path).keys() != {"commit"}:
                    raise RuntimeError("先完成未结算的发行版切换")
                result = host.rpc("plugin/install", {"source": args.source, "ref": exact(args.ref),
                    "marketplace": args.marketplace, "sparse": args.sparse, "update_id": uuid4().hex}, timeout=120)
        case "plugin-status":
            result = host.rpc("plugin/update", {"update_id": args.update_id})
        case "worker":
            result = host.worker(args.request_id)
        case "report":
            host.require_independent()
            if args.request_id:
                result = wsl_reports.reconcile(host, args.request_id)
                if result.get("chat"):
                    wsl_reports.stop_timer(args.request_id)
            else:
                result = {path.stem: wsl_reports.reconcile(host, path.stem)
                          for path in (host.root / "run/self-deploy").glob("*.json") if read_json(path).get("notify")}
        case "status":
            result = read_json(host.record_path(args.request_id))
            observation = subprocess.run(
                ["systemctl", "--user", "show", result["request"]["unit"], "-p", "LoadState,ActiveState,Result"],
                capture_output=True, text=True, timeout=10, env=_user_environment(),
            )
            result["worker"] = observation.stdout.strip() or observation.stderr.strip()
        case _:
            result = host.recover(args)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
