"""Admission control is the load-bearing piece; test it directly."""

from __future__ import annotations

import asyncio
from llmserve.admission import AdmissionController, FairSemaphore
from llmserve.errors import QueueFullError, QueueTimeoutError, ShuttingDownError

import pytest


async def test_runs_up_to_capacity_then_queues():
    ac = AdmissionController(max_concurrent=2, max_queue_size=4, queue_timeout_s=5)
    a = await ac.acquire("a")
    b = await ac.acquire("b")
    assert ac.in_flight == 2

    waiter = asyncio.create_task(ac.acquire("c"))
    await asyncio.sleep(0)
    assert ac.queue_depth == 1
    assert not waiter.done()

    a.release()
    lease = await waiter
    assert ac.in_flight == 2
    assert lease.queue_wait_s >= 0
    b.release()
    lease.release()
    assert ac.in_flight == 0


async def test_rejects_when_queue_is_full():
    ac = AdmissionController(max_concurrent=1, max_queue_size=1, queue_timeout_s=5)
    held = await ac.acquire("held")
    queued = asyncio.create_task(ac.acquire("queued"))
    await asyncio.sleep(0)

    with pytest.raises(QueueFullError) as excinfo:
        await ac.acquire("overflow")
    assert excinfo.value.http_status == 429
    assert excinfo.value.retry_after is not None
    assert ac.stats.rejected_queue_full == 1

    held.release()
    (await queued).release()


async def test_rejection_is_fast():
    """Shedding must be cheap -- that is the whole point of shedding."""
    ac = AdmissionController(max_concurrent=1, max_queue_size=0, queue_timeout_s=5)
    held = await ac.acquire("held")
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(QueueFullError):
        await ac.acquire("nope")
    assert loop.time() - started < 0.01
    held.release()


async def test_queue_timeout_rejects_and_frees_the_slot():
    ac = AdmissionController(max_concurrent=1, max_queue_size=4, queue_timeout_s=0.05)
    held = await ac.acquire("held")
    with pytest.raises(QueueTimeoutError):
        await ac.acquire("slow")
    assert ac.queue_depth == 0
    assert ac.stats.rejected_timeout == 1
    held.release()
    # The timed-out waiter must not have consumed the permit.
    lease = await asyncio.wait_for(ac.acquire("next"), timeout=0.5)
    lease.release()


async def test_queue_is_fifo():
    ac = AdmissionController(max_concurrent=1, max_queue_size=8, queue_timeout_s=5)
    held = await ac.acquire("held")
    order: list[str] = []

    async def contender(name: str):
        lease = await ac.acquire(name)
        order.append(name)
        lease.release()

    tasks = []
    for name in ("first", "second", "third"):
        tasks.append(asyncio.create_task(contender(name)))
        await asyncio.sleep(0.01)  # establish arrival order

    held.release()
    await asyncio.gather(*tasks)
    assert order == ["first", "second", "third"]


async def test_cancelled_waiter_does_not_leak_a_permit():
    ac = AdmissionController(max_concurrent=1, max_queue_size=4, queue_timeout_s=5)
    held = await ac.acquire("held")
    waiter = asyncio.create_task(ac.acquire("cancel-me"))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    held.release()
    assert ac.queue_depth == 0
    lease = await asyncio.wait_for(ac.acquire("after"), timeout=0.5)
    assert ac.in_flight == 1
    lease.release()


async def test_double_release_is_a_noop():
    ac = AdmissionController(max_concurrent=2, max_queue_size=0, queue_timeout_s=1)
    lease = await ac.acquire("x")
    lease.release()
    lease.release()
    assert ac.in_flight == 0
    assert ac.stats.completed == 1


async def test_close_rejects_queued_and_waits_for_in_flight():
    ac = AdmissionController(max_concurrent=1, max_queue_size=4, queue_timeout_s=5)
    held = await ac.acquire("held")
    queued = asyncio.create_task(ac.acquire("queued"))
    await asyncio.sleep(0.01)

    closing = asyncio.create_task(ac.close(drain_timeout=2))
    with pytest.raises(ShuttingDownError):
        await queued
    with pytest.raises(ShuttingDownError):
        await ac.acquire("late")

    held.release()
    assert await closing is True


async def test_close_reports_timeout_when_work_does_not_finish():
    ac = AdmissionController(max_concurrent=1, max_queue_size=0, queue_timeout_s=5)
    lease = await ac.acquire("stuck")
    assert await ac.close(drain_timeout=0.05) is False
    lease.release()


async def test_snapshot_reports_policy_and_counters():
    ac = AdmissionController(max_concurrent=3, max_queue_size=7, queue_timeout_s=1)
    lease = await ac.acquire("x")
    snap = ac.snapshot()
    assert snap["max_concurrency"] == 3
    assert snap["max_queue_size"] == 7
    assert snap["in_flight"] == 1
    assert snap["accepting"] is True
    lease.release()


async def test_fair_semaphore_hands_permits_back_on_timeout():
    sem = FairSemaphore(1)
    await sem.acquire()
    with pytest.raises(asyncio.TimeoutError):
        await sem.acquire(timeout=0.02)
    assert sem.waiters == 0
    sem.release()
    await asyncio.wait_for(sem.acquire(timeout=0.5), timeout=1)
    assert sem.value == 0


async def test_stop_accepting_is_non_blocking():
    """The pre-stop hook must return immediately, even with work in flight."""
    ac = AdmissionController(max_concurrent=1, max_queue_size=4, queue_timeout_s=5)
    lease = await ac.acquire("running")
    queued = asyncio.create_task(ac.acquire("queued"))
    await asyncio.sleep(0.01)

    assert ac.stop_accepting() == 1
    assert ac.closed is True
    assert ac.in_flight == 1                      # in-flight work is untouched
    with pytest.raises(ShuttingDownError):
        await queued
    assert ac.stop_accepting() == 0               # idempotent

    assert await ac.wait_for_idle(timeout=0.05) is False
    lease.release()
    assert await ac.wait_for_idle(timeout=0.5) is True
