"""Admission control: bounded concurrency, a bounded FIFO queue, and fast rejection.

Why this exists
---------------
An LLM engine has a fixed amount of KV-cache and compute. If every arriving request is
handed straight to it, tail latency collapses for *everyone* under load -- the classic
unbounded-queue failure mode where clients time out on work the server is still doing.

The policy here is deliberately simple and predictable:

* at most ``max_concurrent`` requests execute at once;
* at most ``max_queue_size`` further requests may wait, FIFO;
* arrivals beyond that are rejected immediately with 429 + ``Retry-After``
  (shed load early, while the rejection is still cheap);
* a request that waits longer than ``queue_timeout_s`` is rejected with 503 rather
  than being started on a deadline the client has already given up on.

Rejecting fast is a feature: a client that gets a 429 in 2 ms can retry elsewhere,
whereas one that gets a 30 s timeout has burned capacity for nothing.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .errors import AdmissionError, QueueFullError, QueueTimeoutError, ShuttingDownError


Clock = Callable[[], float]


class FairSemaphore:
    """A FIFO counting semaphore.

    ``asyncio.Semaphore`` makes no ordering guarantee, which lets a late arrival barge
    ahead of a request that has already been waiting -- unbounded tail latency under
    sustained load. This variant hands permits out strictly in arrival order and is
    careful to never lose a permit when a waiter is cancelled or times out.
    """

    def __init__(self, value: int) -> None:
        if value < 1:
            raise ValueError("semaphore value must be >= 1")
        self._value = value
        self._initial = value
        self._waiters: deque[asyncio.Future] = deque()

    @property
    def value(self) -> int:
        return self._value

    @property
    def waiters(self) -> int:
        return len(self._waiters)

    @property
    def capacity(self) -> int:
        return self._initial

    async def acquire(self, timeout: float | None = None) -> None:
        """Take a permit, waiting in arrival order if none is free.

        Raises :class:`asyncio.TimeoutError` if ``timeout`` elapses first. Cancellation
        and timeout are both handled on the way out: a permit granted in the same tick
        the waiter gave up is handed straight back rather than leaked.
        """
        # Barge only when nobody is queued, otherwise FIFO is violated.
        if self._value > 0 and not self._waiters:
            self._value -= 1
            return

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters.append(fut)
        try:
            if timeout is None:
                await fut
            else:
                await asyncio.wait_for(fut, timeout)
        except BaseException:
            self._discard(fut)
            # If the permit was granted in the same tick we gave up, hand it back
            # instead of leaking it.
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                self.release()
            raise

    def release(self) -> None:
        """Return a permit and hand it to the longest-waiting caller, if any."""
        self._value += 1
        self._wake()

    def _wake(self) -> None:
        while self._value > 0 and self._waiters:
            fut = self._waiters.popleft()
            if fut.done():  # cancelled or already failed while queued
                continue
            self._value -= 1
            fut.set_result(True)

    def _discard(self, fut: asyncio.Future) -> None:
        try:
            self._waiters.remove(fut)
        except ValueError:
            pass

    def fail_all(self, exc_factory: Callable[[], BaseException]) -> int:
        """Reject every queued waiter (used when draining for shutdown)."""
        count = 0
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_exception(exc_factory())
                count += 1
        return count


@dataclass
class AdmissionStats:
    admitted: int = 0
    completed: int = 0
    rejected_queue_full: int = 0
    rejected_timeout: int = 0
    rejected_shutdown: int = 0
    peak_queue_depth: int = 0
    peak_in_flight: int = 0
    total_queue_wait_s: float = 0.0

    @property
    def rejected(self) -> int:
        return self.rejected_queue_full + self.rejected_timeout + self.rejected_shutdown

    def as_dict(self) -> dict[str, float]:
        """Flatten the counters, adding the two derived values worth reporting."""
        data = {k: v for k, v in self.__dict__.items()}
        data["rejected"] = self.rejected
        data["mean_queue_wait_s"] = (
            self.total_queue_wait_s / self.admitted if self.admitted else 0.0
        )
        return data


@dataclass
class Lease:
    """A granted execution slot. Releasing twice is a no-op."""

    request_id: str
    queue_wait_s: float
    _controller: "AdmissionController" = field(repr=False)
    _released: bool = field(default=False, repr=False)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._controller._release()

    @property
    def released(self) -> bool:
        return self._released


class AdmissionController:
    """Gatekeeper in front of the inference engine."""

    def __init__(
        self,
        max_concurrent: int,
        max_queue_size: int,
        queue_timeout_s: float,
        *,
        clock: Clock = time.perf_counter,
        on_change: Callable[[int, int], None] | None = None,
    ) -> None:
        self._sem = FairSemaphore(max_concurrent)
        self._max_queue_size = max_queue_size
        self._queue_timeout_s = queue_timeout_s
        self._clock = clock
        self._on_change = on_change
        self._in_flight = 0
        self._queued = 0
        self._closed = False
        self._idle = asyncio.Event()
        self._idle.set()
        self.stats = AdmissionStats()

    # -- introspection ------------------------------------------------------
    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def queue_depth(self) -> int:
        return self._queued

    @property
    def capacity(self) -> int:
        return self._sem.capacity

    @property
    def max_queue_size(self) -> int:
        return self._max_queue_size

    @property
    def closed(self) -> bool:
        return self._closed

    def snapshot(self) -> dict[str, float]:
        """Current occupancy plus cumulative counters, for ``/admin/stats`` and logs."""
        return {
            "in_flight": self._in_flight,
            "queue_depth": self._queued,
            "max_concurrency": self.capacity,
            "max_queue_size": self._max_queue_size,
            "accepting": not self._closed,
            **self.stats.as_dict(),
        }

    # -- admission ----------------------------------------------------------
    async def acquire(self, request_id: str) -> Lease:
        """Reserve an execution slot, or raise an :class:`AdmissionError`."""
        if self._closed:
            self.stats.rejected_shutdown += 1
            raise ShuttingDownError("server is shutting down and not accepting new work")

        # Reject *before* queueing when the queue is already at its bound.
        if self._sem.value == 0 and self._queued >= self._max_queue_size:
            self.stats.rejected_queue_full += 1
            raise QueueFullError(
                f"server at capacity: {self._in_flight} running, "
                f"{self._queued} queued (max {self._max_queue_size})"
            )

        started = self._clock()
        self._queued += 1
        self.stats.peak_queue_depth = max(self.stats.peak_queue_depth, self._queued)
        self._notify()
        try:
            await self._sem.acquire(timeout=self._queue_timeout_s)
        except asyncio.TimeoutError as exc:
            self.stats.rejected_timeout += 1
            raise QueueTimeoutError(
                f"timed out after {self._queue_timeout_s:g}s waiting for an execution slot"
            ) from exc
        except AdmissionError:
            self.stats.rejected_shutdown += 1
            raise
        finally:
            self._queued -= 1
            self._notify()

        wait = self._clock() - started
        self._in_flight += 1
        self._idle.clear()
        self.stats.admitted += 1
        self.stats.total_queue_wait_s += wait
        self.stats.peak_in_flight = max(self.stats.peak_in_flight, self._in_flight)
        self._notify()
        return Lease(request_id=request_id, queue_wait_s=wait, _controller=self)

    @asynccontextmanager
    async def slot(self, request_id: str) -> AsyncIterator[Lease]:
        """Context-manager form, for non-streaming call sites."""
        lease = await self.acquire(request_id)
        try:
            yield lease
        finally:
            lease.release()

    def _release(self) -> None:
        self._in_flight -= 1
        self.stats.completed += 1
        self._sem.release()
        if self._in_flight == 0:
            self._idle.set()
        self._notify()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(self._in_flight, self._queued)

    # -- lifecycle ----------------------------------------------------------
    def stop_accepting(self) -> int:
        """Refuse new work immediately and reject anything already queued.

        Split out from :meth:`close` so a pre-stop hook can flip readiness *before*
        SIGTERM arrives: the load balancer stops routing while in-flight generations
        keep streaming to the clients already connected.
        """
        if self._closed:
            return 0
        self._closed = True
        rejected = self._sem.fail_all(
            lambda: ShuttingDownError("server is shutting down and not accepting new work")
        )
        self.stats.rejected_shutdown += rejected
        return rejected

    async def wait_for_idle(self, timeout: float) -> bool:
        """Block until nothing is executing, or ``timeout`` elapses."""
        if self._in_flight == 0:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self, drain_timeout: float = 30.0) -> bool:
        """Stop admitting, reject anything queued, and wait for in-flight work.

        Returns True if the server drained cleanly within ``drain_timeout``.
        """
        self.stop_accepting()
        if self._in_flight == 0:
            return True
        return await self.wait_for_idle(drain_timeout)
