"""对话完成、命令与输入来源检查的公共合同。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from agent.plugin_composition import ServiceKey
from plugins.ledger.contract import MessageReader
from agent.plugin_composition.tasks import Task
from plugins.ledger.contract import ContentPart, ContentReferences, Message
from plugins.sources.contract import CompletionProgram


class ConversationCompleteV2(Protocol):
    async def __call__(
        self, session_id: str, program: CompletionProgram,
    ) -> Message: ...


CONVERSATION_COMPLETE_V2 = ServiceKey[ConversationCompleteV2]("conversation.complete.v2")
CONVERSATION_COMMANDS = ServiceKey[
    Callable[[Task, MessageReader, str], Awaitable[Message | None]]
]("conversation.commands.v1")


class OriginCheck(Protocol):
    def __call__(self, part: ContentPart) -> ContentReferences: ...


CHECK_ORIGIN = ServiceKey[OriginCheck]("conversation.check_origin.v1")


class TaskSessions(Protocol):
    """任务与报告的可见会话选择；分配身份不写数据，接纳复用 Ledger。"""

    async def choices(self) -> tuple[Mapping[str, str], ...]: ...
    async def resolve(self, session_id: str) -> Mapping[str, str]: ...
    def allocate(self, key: str) -> Mapping[str, str]: ...
    async def ensure(self, session_id: str, title: str, *, inherit_from: str | None = None) -> None: ...


TASK_SESSIONS = ServiceKey[TaskSessions]("conversation.task-sessions.v1")
