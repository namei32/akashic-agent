"""Operator controls; the memory store and extraction writer stay private."""
from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from agent.plugin_composition import ServiceKey


class MemoryOperations(Protocol):
    async def status(self) -> dict[str, object]: ...
    async def set_paused(self, paused: bool, *, request_id: str | None = None,
                         expected_version: int | None = None) -> dict[str, object]: ...
    async def retry_failed(self, *, request_id: str, expected_version: int) -> dict[str, object]: ...
    async def consume(self, *, limit: int | None = None, retry: bool = False) -> dict[str, object]: ...
    async def audit(self) -> dict[str, object]: ...
    async def recall(self, key: str, session_id: str, query: str, *, limit: int = 8,
                     timestamp: str | None = None) -> dict[str, object]: ...


DEFAULT_MEMORY = ServiceKey[MemoryOperations]("default-memory.runtime.v1")


class InitiativeMemory(Protocol):
    """Scoped, replayable snapshots and a short current-validity guard."""

    async def admit(self, request_id: str, session_id: str, query: str, *,
                    timestamp: str, limit: int = 4, max_chars: int = 1500) -> dict[str, object]: ...

    async def prepare(self, request_id: str, session_id: str, query: str, *,
                      timestamp: str, limit: int = 4, max_chars: int = 1500) -> dict[str, object]: ...

    def guard(self, request_id: str, session_id: str) -> AbstractAsyncContextManager[str | None]: ...


INITIATIVE_MEMORY = ServiceKey[InitiativeMemory]("default-memory.initiative.v1")
