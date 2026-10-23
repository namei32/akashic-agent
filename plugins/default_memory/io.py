"""Keep a plugin's writer lease until an in-flight SQLite job has settled."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import ParamSpec, TypeVar

from core.common.file_io import run_file_io

P = ParamSpec("P")
T = TypeVar("T")


async def run_io(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    return await run_file_io(partial(operation, *args, **kwargs))
