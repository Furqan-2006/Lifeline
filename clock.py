"""Clock abstraction: the only way nodes read time or sleep.

Test setup, for a node environment that is fully deterministic::

    from lifeline.clock import FakeClock
    from lifeline.rng import Rng

    clock = FakeClock(start=0.0)
    rng = Rng(seed=42)
    # pass clock and rng.child("n01") into the node

This file is the only place in ``lifeline/`` that reads real time.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass, field
from typing import Protocol


class Clock(Protocol):
    """Monotonic time in seconds plus an awaitable sleep.

    ``sleep`` is a proposed addition to contract C2.2 (see CONTRACT QUESTIONS).
    """

    def now(self) -> float:
        """Return the current monotonic time in seconds."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the caller for ``seconds`` of this clock's time."""
        ...


class SystemClock:
    """Real monotonic clock, backed by ``time.monotonic`` and ``asyncio.sleep``."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(order=True)
class _Sleeper:
    due: float
    seq: int
    future: asyncio.Future = field(compare=False)


class FakeClock:
    """Deterministic clock that only moves when ``advance`` is called.

    Sleepers wake strictly in due-time order; ties are woken in the order
    they were created. After waking each sleeper the clock yields to the
    event loop several times so that tasks woken by it can register new
    sleeps before time moves on.
    """

    # Number of event-loop turns given to woken tasks after each wake.
    _SETTLE_TURNS = 10

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._heap: list[_Sleeper] = []
        self._counter = itertools.count()

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        if seconds == 0:
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        heapq.heappush(
            self._heap,
            _Sleeper(due=self._now + seconds, seq=next(self._counter), future=future),
        )
        await future

    async def advance(self, dt: float) -> None:
        """Move time forward by ``dt`` seconds, waking due sleepers in order."""
        if dt < 0:
            raise ValueError("cannot move time backward")
        target = self._now + dt
        while self._heap and self._heap[0].due <= target:
            sleeper = heapq.heappop(self._heap)
            self._now = sleeper.due
            if not sleeper.future.done():
                sleeper.future.set_result(None)
            for _ in range(self._SETTLE_TURNS):
                await asyncio.sleep(0)
        self._now = target
        for _ in range(self._SETTLE_TURNS):
            await asyncio.sleep(0)

    def pending(self) -> int:
        """Number of sleepers not yet woken (useful in tests)."""
        return len(self._heap)