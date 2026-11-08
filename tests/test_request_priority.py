import asyncio
import time
import unittest

from core.request_priority import PriorityQueue, BOOKING_PRIORITY, RUSH_SCAN_PRIORITY


class RequestPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_then_booking_then_scan_then_background(self):
        queue = PriorityQueue()
        next_at = time.monotonic() + 0.03
        calls = []
        async def run(priority):
            def claim():
                nonlocal next_at
                calls.append((priority, time.monotonic()))
                next_at = time.monotonic() + 0.01
            await queue.acquire(priority, lambda: next_at - time.monotonic(), claim)
        await asyncio.gather(*(run(p) for p in [5, 2, 1, 0]))
        self.assertEqual([p for p, _ in calls], [0, 1, 2, 5])
        self.assertTrue(all(b[1]-a[1] >= 0.009 for a,b in zip(calls, calls[1:])))

    async def test_continuing_scan_cannot_jump_a_waiting_other_sport(self):
        queue = PriorityQueue()
        next_at = time.monotonic() + .02
        calls = []

        async def run(name, priority=RUSH_SCAN_PRIORITY):
            def claim():
                nonlocal next_at
                calls.append(name)
                next_at = time.monotonic() + .005
            await queue.acquire(priority, lambda: next_at-time.monotonic(), claim)

        async def badminton():
            for i in range(5):
                await run(f"bad-{i}")

        await asyncio.gather(badminton(), run("tennis"), run("recheck", BOOKING_PRIORITY))
        self.assertEqual(calls[:3], ["recheck", "bad-0", "tennis"])
        self.assertEqual(calls[3:], [f"bad-{i}" for i in range(1, 5)])

    async def test_cancelled_waiter_does_not_block_queue(self):
        queue = PriorityQueue()
        first = asyncio.create_task(queue.acquire(0, lambda: 10, lambda: None))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        calls = []
        await asyncio.wait_for(queue.acquire(5, lambda: 0, lambda: calls.append(True)), timeout=0.1)
        self.assertEqual(calls, [True])
        self.assertFalse(queue._waiters)
