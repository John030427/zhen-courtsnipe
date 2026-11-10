"""Opt-in order at release without a scan: a look at the court a few seconds before the slots
open, then the order itself is the first request at release. Offline: everything is mocked."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.booking_service import BookingService, BookingSource, BookingTarget, ScanSnapshot
from core.order_guard import VidLease
from core.order_journal import OrderJournal
from core.rush_engine import RushEngine
from core.safety import set_order_dry_run_for_tests
from models.rush import CHINA, RushPlan, RushWindow
from tests.test_booking_service import engine_stub
from utils import Court

DATE = "2030-06-03"
RELEASE = datetime(2030, 6, 1, 20, 0, tzinfo=CHINA)
BOOKED = dict(success=True, sent=True, http_status=200)
REFUSED = dict(success=False, sent=True, http_status=200, msg="该时段不可预约")


def reply(court, status, fee=0):
    """19:00–20:00 with one status: 0 not bookable yet, 1 free, 2 held."""
    return dict(ok=True, auth_fail=False, groundId=court.id, startDate=DATE, endDate=DATE,
                avail={"19:00": status, "19:30": status},
                blocks=[dict(time="19:00", endTime="19:30", status=status, fee=fee),
                        dict(time="19:30", endTime="20:00", status=status, fee=fee)])


class OrderFirstTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.e = engine_stub()
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(tmp.name) / "orders.json")
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)

    async def run_rush(self, replies, *, order_first=True, before=None, fee=0):
        """before: per court, the status its target shows ahead of release (default 0)."""
        now = RELEASE - timedelta(seconds=20)

        async def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)

        e = self.e
        e.courts_for_sport = lambda _: [Court(i, f"gid-{i}", f"{i}号场") for i in (1, 2, 3)]
        e.vid_running = False
        e.start_vid_pool, e.stop_vid_pool = AsyncMock(return_value={"ok": True}), AsyncMock()
        e.fetch_token = AsyncMock(return_value={"ok": True})
        e.take_vid = AsyncMock(side_effect=lambda **_: VidLease("fixture-vid", time.monotonic()))
        e.booking = self.service
        rush = RushEngine(e, clock_now=lambda: now, sleep=sleep)
        plan = RushPlan(date=DATE, release_at=RELEASE, scan_window_sec=20, token_refresh_lead_sec=15,
                        windows=[RushWindow(start="19:00", end="20:00")], court_priority=[1, 2, 3],
                        order_first=order_first)
        steps, answers = [], list(replies)

        async def query(court, date, *_a, **_k):
            early = now < RELEASE
            steps.append(("look" if early else "scan", court.no, round((now - RELEASE).total_seconds())))
            return reply(court, (before or {}).get(court.no, "0") if early else "1", fee)

        async def save(**kwargs):
            kwargs["_before_send"]()
            steps.append(("send", kwargs["court"].no, round((now - RELEASE).total_seconds(), 2)))
            return dict(answers.pop(0)) if answers else dict(BOOKED)

        with patch("core.rush_engine.get_order_once", query), \
                patch("core.booking_service.get_order_once", AsyncMock()) as recheck, \
                patch("core.booking_service.save_order_once", save):
            self.assertTrue((await rush.start(plan))["ok"])
            await rush.task
        recheck.assert_not_awaited()
        return rush, steps

    async def test_order_is_the_first_request_at_release(self):
        rush, steps = await self.run_rush([BOOKED])
        self.assertEqual([s[:2] for s in steps], [("look", 1), ("send", 1)])
        self.assertEqual(steps[0][2], -11)            # before the captcha warm-up at -8 s
        self.assertTrue(0 < steps[1][2] <= 1)        # just past release, never before it
        self.assertEqual(rush.outcome(), "booked")
        self.assertIn("没先查询，放号就下单：下单成功", [s["text"] for s in rush.timeline])

    async def test_off_by_default(self):
        rush, steps = await self.run_rush([BOOKED], order_first=False)
        self.assertEqual([s[:2] for s in steps], [("scan", 1), ("send", 1)])

    async def test_a_court_held_before_release_is_passed_over(self):
        rush, steps = await self.run_rush([BOOKED], before={1: "2"})
        self.assertEqual([s[:2] for s in steps], [("look", 1), ("look", 2), ("send", 2)])

    async def test_nothing_usable_seen_means_scan_first(self):
        rush, steps = await self.run_rush([BOOKED], before={1: "2", 2: "2"})
        self.assertEqual([s[:2] for s in steps], [("look", 1), ("look", 2), ("scan", 1), ("send", 1)])

    async def test_refused_straight_order_falls_back_to_scanning(self):
        rush, steps = await self.run_rush([REFUSED, BOOKED])
        self.assertEqual([s[:2] for s in steps], [("look", 1), ("send", 1), ("scan", 1), ("send", 1)])
        self.assertEqual(rush.outcome(), "booked")

    async def test_paid_court_is_never_ordered_straight(self):
        rush, steps = await self.run_rush([BOOKED], fee=30)
        self.assertEqual([s[:2] for s in steps], [("look", 1)])
        self.assertEqual((rush.phase, rush.reason), ("degraded", "order_blocked"))

    async def test_an_old_look_is_not_used(self):
        with patch("core.booking_service.PEEK_MAX_AGE_SEC", -1):
            rush, steps = await self.run_rush([BOOKED])
        self.assertEqual([s[:2] for s in steps], [("look", 1), ("look", 2), ("scan", 1), ("send", 1)])


class BeforeReleaseSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_rush_may_order_from_a_look(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = engine_stub()
            service = BookingService(e)
            service._journal = OrderJournal(Path(tmp) / "orders.json")
            court = e.court_by_no[1]
            target = BookingTarget(court, DATE, "19:00", "20:00")
            identity = (e.token, e.user_id, e.customer_id, e.customer_name, e.customer_tel)
            look = ScanSnapshot.capture(reply(court, "0"), identity, 1)
            self.assertEqual(look.rejection_reason(target, identity, 1), "incomplete")  # not free: no normal reuse
            self.assertIsNone(look.rejection_reason(target, identity, 1, before_release=True))
            set_order_dry_run_for_tests(False)
            self.addCleanup(set_order_dry_run_for_tests, True)
            with patch("core.booking_service.get_order_once", AsyncMock()) as query, \
                    patch("core.booking_service.save_order_once", AsyncMock()) as save:
                out = await service.place_order(target, source=BookingSource.MANUAL, scan_snapshot=look,
                                                before_release=True,
                                                take_vid=AsyncMock(return_value=VidLease("v", time.monotonic())))
            self.assertFalse(out.success)
            self.assertEqual(out.diagnostics["reason"], "no_peek")
            query.assert_not_awaited()
            save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
