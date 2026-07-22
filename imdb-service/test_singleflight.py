"""Tests for async single-flight coordination."""

import asyncio

import pytest

import singleflight


@pytest.mark.asyncio
async def test_same_key_shares_one_task():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "result"

    results = await asyncio.gather(
        *(singleflight.run_singleflight("same", factory) for _ in range(10))
    )

    assert results == ["result"] * 10
    assert calls == 1


@pytest.mark.asyncio
async def test_different_keys_run_concurrently():
    active = 0
    max_active = 0

    async def factory():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "result"

    await asyncio.gather(
        singleflight.run_singleflight("first", factory),
        singleflight.run_singleflight("second", factory),
    )

    assert max_active == 2


@pytest.mark.asyncio
async def test_failed_task_is_removed_for_retry():
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        raise RuntimeError("upstream failed")

    results = await asyncio.gather(
        singleflight.run_singleflight("retry", fail),
        singleflight.run_singleflight("retry", fail),
        return_exceptions=True,
    )
    assert all(isinstance(result, RuntimeError) for result in results)
    assert calls == 1

    async def succeed():
        nonlocal calls
        calls += 1
        return "recovered"

    assert await singleflight.run_singleflight("retry", succeed) == "recovered"
    assert calls == 2


@pytest.mark.asyncio
async def test_global_upstream_concurrency_limit(monkeypatch):
    monkeypatch.setattr(singleflight, "IMDB_UPSTREAM_CONCURRENCY", 2)
    active = 0
    max_active = 0

    async def factory():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1

    await asyncio.gather(
        *(singleflight.run_singleflight(("limited", i), factory) for i in range(6))
    )

    assert max_active == 2
