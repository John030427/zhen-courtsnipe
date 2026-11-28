"""Experiment from 11-28: a Rush does not order with a VID made before the school released.
From 11-13 to 11-27 every order with such a VID got 「系统繁忙」, every one with a VID made after
release went through. Offline: nothing reaches the school."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.booking_service import BookingService, BookingSource, BookingTarget, ScanSnapshot, vid_issued_after
from core.order_guard import VidLease
from core.order_journal import OrderJournal
from core.rush_engine import RushEngine
from core.safety import set_order_dry_run_for_tests
from models.rush import CHINA, RushPlan, RushWindow
from tests.test_booking_service import engine_stub
from tests.test_rush_scan_reuse import free_result


class OrderVidTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.e = engine_stub()
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(tmp.name) / "orders.json")
        self.target = BookingTarget(self.e.court_by_no[1], "2030-06-03", "19:00", "20:00")
        identity = tuple(getattr(self.e, k) for k in ("token", "user_id", "customer_id", "customer_name", "customer_tel"))
        self.snapshot = ScanSnapshot.capture(free_result(self.target.court, self.target.date), identity, 1)
        self.fresh = VidLease("made-after", time.monotonic())
        self.service._generate_vid = AsyncMock(return_value=(self.fresh, None, None, False))
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)

    async def order(self, pooled, cutoff, *, claim=False):
        if claim:
            self.e.claim_vid = lambda **_: pooled
        token = vid_issued_after.set(cutoff)
        try:
            with patch("core.booking_service.save_order_once", AsyncMock(return_value=dict(success=True, sent=True))) as save, \
                    patch("core.booking_service.audit") as audit:
                out = await self.service.place_order(self.target, source=BookingSource.AUTO, scan_snapshot=self.snapshot,
                                                     take_vid=AsyncMock(return_value=pooled))
        finally:
            vid_issued_after.reset(token)
        self.assertTrue(out.success)
        skipped = [c for c in audit.call_args_list if c.args[0] == "booking.vid_before_release_skipped"]
        return save.await_args.kwargs["vid"], skipped

    async def check_made_before_is_left(self, claim):
        now = time.monotonic()
        vid, skipped = await self.order(VidLease("made-before", now - 3), cutoff=now, claim=claim)
        self.assertEqual(vid, "made-after")
        self.service._generate_vid.assert_awaited_once()
        self.assertEqual(len(skipped), 1)
        self.assertNotIn("made-before", repr(skipped[0]))     # the VID itself is never logged

    async def test_pooled_vid_made_before_release_is_left_and_a_new_one_is_made(self):
        await self.check_made_before_is_left(claim=False)

    async def test_vid_claimed_ahead_but_made_before_release_is_left_too(self):
        await self.check_made_before_is_left(claim=True)

    async def test_vid_made_after_release_is_used_straight_away(self):
        now = time.monotonic()
        vid, skipped = await self.order(VidLease("pooled-after", now), cutoff=now - 1)
        self.assertEqual((vid, skipped), ("pooled-after", []))
        self.service._generate_vid.assert_not_awaited()

    async def test_without_a_cutoff_nothing_changes(self):
        # watches and manual orders set no cut-off
        vid, skipped = await self.order(VidLease("old", time.monotonic() - 3), cutoff=None)
        self.assertEqual((vid, skipped), ("old", []))


class RushCutoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_rush_sets_the_cutoff_at_release_for_its_orders(self):
        now = datetime(2030, 6, 1, 19, 59, 58, tzinfo=CHINA)

        async def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)
        e = engine_stub()
        e.booking = BookingService(e)
        e.booking._journal = OrderJournal(Path(tmp.name) / "orders.json")
        e.booking._generate_vid = AsyncMock(side_effect=lambda *a, **k: (VidLease("fresh", time.monotonic()), None, None, False))
        e.courts_for_sport = lambda _: list(e.court_by_no.values())
        e.vid_running, e.start_vid_pool, e.stop_vid_pool = False, AsyncMock(return_value={"ok": True}), AsyncMock()
        e.fetch_token = AsyncMock(return_value={"ok": True})
        e.take_vid = AsyncMock(return_value=None)
        seen = []

        async def save(**kwargs):
            kwargs["_before_send"]()
            seen.append(vid_issued_after.get())
            return dict(success=True, sent=True, http_status=200)

        rush = RushEngine(e, clock_now=lambda: now, sleep=sleep)
        plan = RushPlan(date="2030-06-03", release_at=now + timedelta(seconds=2), scan_window_sec=5,
                        windows=[RushWindow(start="19:00", end="20:00")], court_priority=[1])
        before = time.monotonic()
        with patch("core.rush_engine.get_order_once", AsyncMock(side_effect=lambda c, d, *_a, **_k: free_result(c, d))), \
                patch("core.booking_service.save_order_once", save):
            self.assertTrue((await rush.start(plan))["ok"])
            await rush.task
        self.assertEqual(rush.outcome(), "booked")
        self.assertGreaterEqual(seen[0], before + 1.0)          # release (reached now) plus the extra second
        self.assertLessEqual(seen[0], time.monotonic() + 1.0)
        self.assertIsNone(vid_issued_after.get())               # nothing leaks outside the order


if __name__ == "__main__":
    unittest.main()
