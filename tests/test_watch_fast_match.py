"""Offline checks: early booking must preserve time/court priority and guards."""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.watch_engine import WatchEngine
from models.plans import CHINA, Target, WatchPlan


class FastMatchTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, *, auto=True, first_start="19:00", dates=None, targets=None, rejection=False):
        now = datetime(2030, 6, 1, 12, tzinfo=CHINA)
        mono = 0

        async def sleep(seconds):
            nonlocal mono
            mono += seconds

        with patch("models.plans.now", return_value=now):
            plan = WatchPlan(dates=dates or ["2030-06-02"], courts=[3, 2, 1],
                             targets=targets or [Target(start="19:00", end="22:00")],
                             auto_reserve=auto, duration_minutes=1, poll_interval=60)
        courts = [SimpleNamespace(no=n, id=str(n)) for n in plan.courts]
        query = AsyncMock()

        async def answer(court, *_):
            start = first_start if court.no == 3 else "19:00"
            h = int(start[:2])
            return {"ok": True, "blocks": [
                {"time": f"{h}:00", "endTime": f"{h}:30", "status": "1"},
                {"time": f"{h}:30", "endTime": f"{h+1}:00", "status": "1"}]}

        query.side_effect = answer
        booking = SimpleNamespace(place_order=AsyncMock(return_value=SimpleNamespace(
            uncertain=False, success=not rejection, raw={"terminal_rejection": rejection},
            msg="时长超限")), publish_result=AsyncMock())
        engine = SimpleNamespace(token="fixture", user_id="fixture", booking=booking,
                                 courts_for_sport=lambda _: courts,
                                 bus=SimpleNamespace(publish=AsyncMock()))
        watch = WatchEngine(engine, clock_now=lambda: now, clock_mono=lambda: mono, sleep=sleep)
        with patch("core.watch_engine.get_order_once", query), \
                patch("core.watch_engine.dry_run_orders_enabled", return_value=False):
            await watch.run(plan, "offline-fixture")
        self.assertNotEqual(watch.state["phase"], "error")
        return watch, query, booking

    async def test_best_time_books_first_priority_court_without_scanning_rest(self):
        watch, query, booking = await self.run_case()
        self.assertEqual(query.await_count, 1)
        self.assertTrue(watch.state["scan_short_circuited"])
        self.assertEqual(booking.place_order.await_args.args[0].court.no, 3)
        booking.publish_result.assert_awaited_once()

    async def test_later_slot_does_not_hide_better_time_on_next_court(self):
        _, query, booking = await self.run_case(first_start="20:00")
        self.assertEqual(query.await_count, 2)
        target = booking.place_order.await_args.args[0]
        self.assertEqual((target.court.no, target.start), (2, "19:00"))

    async def test_notify_only_still_scans_all_courts(self):
        watch, query, booking = await self.run_case(auto=False)
        self.assertEqual(query.await_count, 3)
        self.assertFalse(watch.state["scan_short_circuited"])
        booking.place_order.assert_not_awaited()

    async def test_terminal_rejection_stops_without_retry(self):
        watch, query, booking = await self.run_case(rejection=True)
        self.assertEqual(watch.state["phase"], "exhausted")
        self.assertIn("修改预约时长", watch.state["message"])
        query.assert_awaited_once()
        booking.place_order.assert_awaited_once()

    async def test_multiple_dates_or_targets_keep_full_scan(self):
        for settings, count in [({"dates": ["2030-06-02", "2030-06-03"]}, 6),
                                ({"targets": [Target(start="19:00", end="20:00"),
                                              Target(start="20:00", end="21:00")]}, 3)]:
            with self.subTest(settings=settings):
                watch, query, _ = await self.run_case(**settings)
                self.assertEqual(query.await_count, count)
                self.assertFalse(watch.state["scan_short_circuited"])
