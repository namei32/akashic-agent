"""Scheduler 发布的只读任务快照合同。"""

from collections.abc import Mapping
from typing import Protocol

from agent.plugin_composition import ServiceKey


class SchedulerReader(Protocol):
    """异步读取调度快照；取消退出前完成实际读取。"""

    async def list_jobs(self) -> tuple[Mapping[str, object], ...]: ...

    async def get_job(self, job_id: str) -> Mapping[str, object] | None: ...


class TaskSessions(Protocol):
    """周期任务的可见对话选择；分配身份不写数据，ensure 只接纳新会话。"""

    async def choices(self) -> tuple[Mapping[str, str], ...]: ...

    async def resolve(self, session_id: str) -> Mapping[str, str]: ...

    def allocate(self, key: str) -> Mapping[str, str]: ...

    async def ensure(self, session_id: str, title: str, *, inherit_from: str | None = None) -> None: ...


SCHEDULER_INSPECTION = ServiceKey[SchedulerReader]("scheduler.inspection.v3")
TASK_SESSIONS = ServiceKey[TaskSessions]("scheduler.task-sessions.v1")
