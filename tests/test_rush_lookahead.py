"""While an order waits for the school, the next court is looked at; if the order is refused and
that court is free, it is ordered at once. Only one order is ever on its way. Offline."""
import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.booking_service import BookingService
from core.order_guard import VidLease
from core.order_journal import OrderJournal
from core.rush_engine import RushEngine
from core.safety import set_order_dry_run_for_tests
from models.rush import CHINA, RushPlan, RushWindow
from tests.test_booking_service import engine_stub
from utils import Court, order_sent_hook

DATE = "2030-06-03"
BUSY = dict(success=False, sent=True, http_status=200, msg="系统繁忙，请稍后重试")
BOOKED = dict(success=True, sent=True, http_status=200)
UNKNOWN = dict(success=False, sent=True, uncertain=True, msg="网络异常，请核对官方订单")


def reply(court, free=True):
    s = "1" if free else "2"
    return dict(ok=True, auth_fail=False, groundId=court.id, startDate=DATE, endDate=DATE,
                avail={"19:00": s, "19:30": s},
                blocks=[dict(time="19:00", endTime="19:30", status=s, fee=0),
                        dict(time="19:30", endTime="20:00", status=s, fee=0)])


class LookaheadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.e = engine_stub()
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(tmp.name) / "orders.json")
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)

    async def run_rush(self, answers, free=(10, 9, 8), slot_wait=0):
        """Courts 10, 9, 8 in that order. answers: saveOrder replies in order; each reply comes
        back only after the next query (if any) has been made, as a slow school would."""
        now = datetime(2030, 6, 1, 19, 59, 58, tzinfo=CHINA)

        async def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)

        e = self.e
        e.courts_for_sport = lambda _: [Court(i, f"gid-{i}", f"{i}号场") for i in (8, 9, 10)]
        e.vid_running = False
        e.start_vid_pool, e.stop_vid_pool = AsyncMock(return_value={"ok": True}), AsyncMock()
        e.fetch_token = AsyncMock(return_value={"ok": True})
        e.take_vid = AsyncMock(side_effect=lambda **_: VidLease("fixture-vid", time.monotonic()))
        e.config_data["safety"]["max_orders_per_run"] = 3
        e.booking = self.service
        rush = RushEngine(e, clock_now=lambda: now, sleep=sleep)
        plan = RushPlan(date=DATE, release_at=now + timedelta(seconds=2), scan_window_sec=20,
                        windows=[RushWindow(start="19:00", end="20:00")], court_priority=[10, 9, 8])
        steps, answers = [], list(answers)
        queried = asyncio.Event()

        async def query(court, date, *_a, **_k):
            steps.append(("scan", court.no))
            queried.set()
            return reply(court, court.no in free)

        async def save(**kwargs):
            kwargs["_before_send"]()
            for _ in range(slot_wait):        # waiting for the request slot, as a real order may
                await asyncio.sleep(0)
            steps.append(("send", kwargs["court"].no))
            if order_sent_hook.get():
                order_sent_hook.get()()       # what the real sender does once the order has left
            queried.clear()
            try:
                await asyncio.wait_for(queried.wait(), 0.2)   # the school answers after the next query
            except asyncio.TimeoutError:
                pass
            steps.append(("reply", kwargs["court"].no))
            return dict(answers.pop(0)) if answers else dict(BOOKED)

        with patch("core.rush_engine.get_order_once", query), \
                patch("core.booking_service.get_order_once", AsyncMock()) as recheck, \
                patch("core.booking_service.save_order_once", save):
            self.assertTrue((await rush.start(plan))["ok"])
            await rush.task
        recheck.assert_not_awaited()   # the court seen while waiting is ordered from that look
        return rush, steps

    async def test_refused_first_order_goes_straight_to_the_court_seen_meanwhile(self):
        rush, steps = await self.run_rush([BUSY, BOOKED])
        self.assertEqual(steps, [("scan", 10), ("send", 10), ("scan", 9), ("reply", 10),
                                 ("send", 9), ("scan", 8), ("reply", 9)])
        self.assertEqual(rush.outcome(), "booked")
        self.assertEqual([a["court"] for a in rush.applied], [10, 9])

    async def test_look_ahead_waits_until_the_order_has_left(self):
        # 11-27: the look-ahead query went first and the order waited a second for its slot
        rush, steps = await self.run_rush([BUSY, BOOKED], slot_wait=20)
        self.assertEqual(steps[:3], [("scan", 10), ("send", 10), ("scan", 9)])
        self.assertEqual(rush.outcome(), "booked")

    async def test_first_order_booked_means_no_second_order(self):
        rush, steps = await self.run_rush([BOOKED])
        self.assertEqual(steps, [("scan", 10), ("send", 10), ("scan", 9), ("reply", 10)])
        self.assertEqual(rush.outcome(), "booked")

    async def test_unknown_result_stops_even_with_a_free_court_seen(self):
        rush, steps = await self.run_rush([UNKNOWN])
        self.assertEqual(steps, [("scan", 10), ("send", 10), ("scan", 9), ("reply", 10)])
        self.assertEqual((rush.phase, rush.reason), ("degraded", "result_uncertain"))

    async def test_court_seen_taken_is_skipped_and_the_scan_goes_on(self):
        rush, steps = await self.run_rush([BUSY, BOOKED], free=(10, 8))
        self.assertEqual(steps[:5], [("scan", 10), ("send", 10), ("scan", 9), ("reply", 10), ("scan", 8)])
        self.assertEqual(steps[5], ("send", 8))
        self.assertEqual(steps.count(("scan", 9)), 1)   # not asked again in the same round
        self.assertEqual(rush.outcome(), "booked")


if __name__ == "__main__":
    unittest.main()
