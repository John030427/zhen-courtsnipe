"""Background VID requests do not take a free request slot while a Rush is busy and the pool
still holds a usable VID: on 11-19 that made each Rush step wait 0.8-0.9 s. Offline, real
pacing (about 3 s of wall time)."""
import asyncio
import time
import unittest
from types import SimpleNamespace

import utils
from core.request_priority import RUSH_SCAN_PRIORITY, request_priority
from engine import Engine


async def rush_step_wait(gate_on: bool) -> float:
    """A Rush query is in flight for 1.2 s while a pool worker waits for a slot; how long does
    the Rush's next request then wait?"""
    utils._next_request = 0.0
    utils._priority_queue = utils._priority_loop = None
    utils.set_background_gate(lambda: gate_on)

    async def worker():
        request_priority.set(utils.BACKGROUND_PRIORITY)
        await utils.wait_http_slot()

    request_priority.set(RUSH_SCAN_PRIORITY)
    await utils.wait_http_slot()           # the query goes out
    pool = asyncio.create_task(worker())   # the pool wants the next slot
    await asyncio.sleep(1.2)               # the query is in flight
    started = time.monotonic()
    await utils.wait_http_slot()           # the order (or next query)
    waited = time.monotonic() - started
    pool.cancel()
    await asyncio.gather(pool, return_exceptions=True)
    return waited


class BackgroundYieldTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        utils.set_background_gate(None)

    async def test_pool_takes_the_slot_without_the_gate(self):
        self.assertGreater(await rush_step_wait(False), 0.6)

    async def test_rush_goes_at_once_with_the_gate(self):
        self.assertLess(await rush_step_wait(True), 0.2)


class GateRuleTests(unittest.TestCase):
    def engine(self, phase, headroom):
        e = Engine.__new__(Engine)
        e.rush = SimpleNamespace(slots={"badminton": SimpleNamespace(running=phase != "idle", phase=phase),
                                         "tennis": SimpleNamespace(running=False, phase="idle")})
        e.vid_headroom = lambda: headroom
        return e

    def test_yields_only_while_a_rush_works_and_a_vid_is_on_hand(self):
        self.assertTrue(self.engine("scanning", 5.0)._vid_pool_should_yield())
        self.assertTrue(self.engine("ordering", 2.5)._vid_pool_should_yield())
        self.assertFalse(self.engine("scanning", 1.0)._vid_pool_should_yield())  # pool needs refilling
        self.assertFalse(self.engine("waiting", 5.0)._vid_pool_should_yield())   # warm-up before release
        self.assertFalse(self.engine("idle", 5.0)._vid_pool_should_yield())


if __name__ == "__main__":
    unittest.main()
