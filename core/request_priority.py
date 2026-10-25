"""Prioritize pending work without increasing the shared HTTP request budget."""
import asyncio
from contextvars import ContextVar
from itertools import count

request_priority = ContextVar("request_priority", default=5)


class PriorityQueue:
    def __init__(self):
        self._sequence = count()
        self._waiters = []

    def _wake(self):
        for _, _, event in self._waiters:
            event.set()

    async def acquire(self, priority, remaining, claim):
        waiter = (priority, next(self._sequence), asyncio.Event())
        self._waiters.append(waiter)
        self._waiters.sort(key=lambda item: item[:2])
        self._wake()
        try:
            while True:
                delay = remaining() if self._waiters[0] is waiter else None
                if delay is not None and delay <= 0:
                    claim()
                    return
                waiter[2].clear()
                try:
                    await asyncio.wait_for(waiter[2].wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._waiters.remove(waiter)
            self._wake()
