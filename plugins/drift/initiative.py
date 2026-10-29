"""Serialize control changes with the admission of initiative side effects."""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
import sqlite3
from typing import TypeVar

from core.common.file_io import run_file_io
from .store import DriftStore

T = TypeVar("T")


class Initiative:
    def __init__(self, store: DriftStore, notify: Callable[[], None]) -> None:
        self.store, self.notify = store, notify

    async def write(self, call: Callable[[], T]) -> T:
        committed = False

        def run() -> T:
            nonlocal committed
            value = call()
            committed = True
            return value

        try:
            return await run_file_io(run)
        finally:
            # run_file_io waits for a physical commit even if its waiter is cancelled.
            if committed:
                self.notify()

    @asynccontextmanager
    async def start(self, accepted_turn: Mapping[str, object],
                    target: Mapping[str, object]) -> AsyncIterator[str | None]:
        """Hold the SQLite writer lock through the consumer's durable start record."""
        connections: list[sqlite3.Connection] = []

        def begin() -> str | None:
            db = sqlite3.connect(self.store.path.resolve().as_uri() + "?mode=rw",
                                 uri=True, timeout=30, check_same_thread=False)
            connections.append(db)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            return self.store.initiative_send_state(db, accepted_turn, target)

        def close() -> None:
            for db in connections:
                try:
                    db.rollback()
                finally:
                    db.close()

        try:
            state = await run_file_io(begin)
            yield state
        finally:
            if connections:
                await run_file_io(close)
