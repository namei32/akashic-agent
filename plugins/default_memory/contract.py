"""Operator controls; the memory store and extraction writer stay private."""
from __future__ import annotations

from typing import Protocol
from agent.plugin_composition import ServiceKey


class MemoryOperations(Protocol):
    async def status(self) -> dict[str, object]: ...
    async def consume(self, *, limit: int | None = None, retry: bool = False) -> dict[str, object]: ...
    async def audit(self) -> dict[str, object]: ...
    async def recall(self, key: str, session_id: str, query: str, *, limit: int = 8,
                     timestamp: str | None = None) -> dict[str, object]: ...


DEFAULT_MEMORY = ServiceKey[MemoryOperations]("default-memory.runtime.v1")
