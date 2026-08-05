"""Async single-flight coordination for IMDb upstream requests."""

import asyncio
import os
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

T = TypeVar("T")
IMDB_UPSTREAM_CONCURRENCY = max(1, int(os.getenv("IMDB_UPSTREAM_CONCURRENCY", "8")))

_tasks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Hashable, asyncio.Task[Any]]] = (
    WeakKeyDictionary()
)
_semaphores: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Hashable, asyncio.Semaphore]] = (
    WeakKeyDictionary()
)


async def run_singleflight(key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
    """Run one task per key and share its result among concurrent callers."""
    loop = asyncio.get_running_loop()
    registry = _tasks.setdefault(loop, {})
    task = registry.get(key)
    if task is None:

        async def run_limited() -> T:
            semaphores = _semaphores.setdefault(loop, {})
            semaphore = semaphores.setdefault(
                "imdb-upstream", asyncio.Semaphore(IMDB_UPSTREAM_CONCURRENCY)
            )
            async with semaphore:
                return await factory()

        task = asyncio.create_task(run_limited())
        registry[key] = task

        def cleanup(done: asyncio.Task[Any]) -> None:
            if registry.get(key) is done:
                registry.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(cleanup)

    return await asyncio.shield(task)
