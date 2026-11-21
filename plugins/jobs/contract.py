"""后台任务完成通知的公共合同；生产方只登记等待入口，投递归 jobs 插件。"""
from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol

from agent.plugin_composition import ServiceKey

JobStatus = Literal["completed", "failed", "timeout", "killed", "unknown"]
JobSnapshotStatus = Literal["running", "stopping", "completed", "failed", "timeout", "killed", "unknown"]
SubagentReport = Literal["inline", "waiting", "delivered", "failed"]


@dataclass(frozen=True, slots=True)
class JobOutcome:
    status: JobStatus
    detail: str


@dataclass(frozen=True, slots=True)
class SubagentProgress:
    steps: int
    actions: tuple[tuple[int, str], ...]
    report: SubagentReport
    report_message_id: str | None = None


# settled 在任务结算时返回结果；返回 None 表示模型已收走结果或任务被显式停止，不再通知。
@dataclass(frozen=True, slots=True)
class JobWatch:
    session_id: str
    source: str
    job_id: str
    kind: str
    label: str
    collect_hint: str
    settled: Callable[[], Awaitable[JobOutcome | None]]
    # 任务实际开始的 epoch 毫秒；生产方先前台运行再转后台时提供，缺席按登记时刻计。
    started_at_ms: int | None = None
    # 生产方提供的停止入口，供用户在界面上终止；返回 False 表示任务已不在运行。缺席时界面不提供停止。
    stop: Callable[[], Awaitable[bool]] | None = None
    # A process handle can be reused after restart. Producers may supply the stable tool-call key.
    effect_key: str = ""
    through_seq: int | None = None

    @property
    def wake_source(self) -> str:
        suffix = "@" + sha256(self.effect_key.encode()).hexdigest() if self.effect_key else ""
        return "job:" + self.job_id + suffix


# 前端只读镜像的一行；时间均为 epoch 毫秒。
@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    kind: str
    label: str
    status: JobSnapshotStatus
    detail: str
    started_at: int
    finished_at: int | None = None
    stoppable: bool = False
    subagent: SubagentProgress | None = None

    def to_wire(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.job_id, "kind": self.kind, "label": self.label, "status": self.status,
            "detail": self.detail, "started_at": self.started_at, "finished_at": self.finished_at,
            "stoppable": self.stoppable,
        }
        if self.subagent is not None:
            result["subagent"] = {
                "steps": self.subagent.steps,
                "actions": [{"at": at, "label": label} for at, label in self.subagent.actions],
                "report": self.subagent.report,
                "report_message_id": self.subagent.report_message_id,
            }
        return result


class JobPanelSource(Protocol):
    def follow_jobs(self, session_id: str) -> AsyncGenerator[tuple[JobSnapshot, ...], None]: ...

    async def stop_job(self, session_id: str, job_id: str) -> bool: ...


class JobNotices(Protocol):
    def watch(self, job: JobWatch) -> None: ...

    def origin_source(self, session_id: str, source: str) -> str:
        """当前唤醒可访问登记任务的原来源；其他会话和来源保持原样。"""
        ...

    async def stop_job(self, session_id: str, job_id: str) -> bool:
        """用户停止本会话的运行中任务；结算会照常通知模型，并注明是用户停止的。"""
        ...

    def follow_jobs(self, session_id: str) -> AsyncGenerator[tuple[JobSnapshot, ...], None]:
        """先产出会话当前全量快照，之后每次变化产出最新全量快照；连续变化会合并。
        提供方 activation 关闭时迭代结束，消费方应视旧快照失效并重新借用服务。"""
        ...


JOB_NOTICES = ServiceKey[JobNotices]("jobs.notices.v1")
JOB_WAITING = ServiceKey[Callable[[str, str], bool]]("jobs.waiting.v1")
