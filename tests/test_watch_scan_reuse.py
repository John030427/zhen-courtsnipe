"""Offline: a watch that just saw a free court orders it from that answer, without asking the
school again, and counts the order it sent, also when the order succeeds."""
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.booking_service import BookingService, ScanSnapshot
from core.order_guard import VidLease
from core.order_journal import OrderJournal
from core.safety import set_order_dry_run_for_tests
from core.watch_engine import WatchEngine
from models.plans import CHINA, Target, WatchPlan
from tests.test_booking_service import engine_stub
from tests.test_rush_scan_reuse import free_result

DATE = "2030-06-02"


class WatchScanReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.e = engine_stub()
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(tmp.name) / "orders.json")
        self.service._generate_vid = AsyncMock(
            side_effect=lambda *a, **k: (VidLease("fixture-vid", time.monotonic()), None, None, False))
        self.e.booking = self.service
        self.e.courts_for_sport = lambda _: list(self.e.court_by_no.values())
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)

    async def run_watch(self, *, reply, scan_age=0.0, change_login=False):
        now = datetime(2030, 6, 1, 12, tzinfo=CHINA)
        skipped = 0.0   # waits between rounds are skipped; the order deadline uses the real clock

        async def sleep(seconds):
            nonlocal skipped
            skipped += seconds

        with patch("models.plans.now", return_value=now):
            plan = WatchPlan(dates=[DATE], courts=[1], targets=[Target(start="19:00", end="20:00")],
                             auto_reserve=True, duration_minutes=5, poll_interval=60, max_attempts=2)

        async def scan(court, date, *_a, **_k):
            if change_login:
                self.e.tokens.auth_revision += 1    # a new login arrives right after the scan
            return free_result(court, date)

        capture = ScanSnapshot.capture

        def aged(*args, **kwargs):
            snap = capture(*args, **kwargs)
            return replace(snap, received_mono=snap.received_mono - scan_age)
        watch = WatchEngine(self.e, clock_now=lambda: now, clock_mono=lambda: time.monotonic() + skipped, sleep=sleep)
        with patch("core.watch_engine.get_order_once", scan), \
                patch("core.booking_service.get_order_once",
                      AsyncMock(side_effect=lambda c, d, *_: free_result(c, d))) as recheck, \
                patch("core.booking_service.save_order_once", AsyncMock(return_value=reply)) as save, \
                patch("core.watch_engine.ScanSnapshot.capture", aged):
            await watch.run(plan, "offline-fixture")
        self.assertNotEqual(watch.state["phase"], "error")
        return watch, recheck, save

    async def test_fresh_scan_is_ordered_without_asking_again(self):
        watch, recheck, save = await self.run_watch(reply=dict(success=True, sent=True, http_status=200))
        recheck.assert_not_awaited()
        save.assert_awaited_once()
        self.assertEqual(watch.state["phase"], "success")
        self.assertEqual(watch.state["orders_sent"], 1)   # was 0 on 11-25 although one order went out

    async def test_scan_older_than_two_seconds_is_checked_again(self):
        _, recheck, save = await self.run_watch(reply=dict(success=True, sent=True, http_status=200), scan_age=3.0)
        recheck.assert_awaited_once()
        save.assert_awaited_once()

    async def test_scan_made_before_a_new_login_is_checked_again(self):
        _, recheck, save = await self.run_watch(reply=dict(success=True, sent=True, http_status=200),
                                                change_login=True)
        recheck.assert_awaited_once()
        save.assert_awaited_once()

    async def test_refused_orders_still_stop_at_the_limit(self):
        watch, _, save = await self.run_watch(
            reply=dict(success=False, sent=True, http_status=200, msg="系统繁忙"))
        self.assertEqual(save.await_count, 2)
        self.assertEqual(watch.state["orders_sent"], 2)
        self.assertEqual(watch.state["phase"], "exhausted")


if __name__ == "__main__":
    unittest.main()
