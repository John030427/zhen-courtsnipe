"""Offline concurrency regression: no school traffic, real temporary journal."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.booking_service import BookingService, BookingSource, BookingTarget
from core.auth_keeper import AuthKeeper
from core.order_journal import OrderJournal
from core.safety import set_order_dry_run_for_tests
from core.request_priority import request_priority, BOOKING_PRIORITY, RUSH_SCAN_PRIORITY
from engine import Engine
from tests.test_booking_service import engine_stub
from utils import Court


FREE = {"ok": True, "blocks": [], "avail": {
    "16:00": "1", "16:30": "1", "17:00": "1", "17:30": "1",
    "20:00": "1", "20:30": "1", "21:00": "1", "21:30": "1"}}


class BookingConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.e = engine_stub(sport_for_ground_id=lambda gid: "tennis" if gid == "tennis" else "badminton")
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(self.tmp.name) / "orders.json")
        self.bad = BookingTarget(Court(10, "badminton", "fixture"), "2030-06-01", "16:00", "18:00")
        self.ten = BookingTarget(Court(6, "tennis", "fixture"), self.bad.date, "20:00", "22:00")
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)
        self.query = patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)).start()
        patch("core.booking_service.get_vid_once", AsyncMock(side_effect=[("first", {}), ("second", {})])).start()
        self.save = patch("core.booking_service.save_order_once", AsyncMock(return_value={
            "success": True, "sent": True})).start()
        patch("core.booking_service.audit").start()
        self.addCleanup(patch.stopall)

    async def book(self, target, **kwargs):
        return await self.service.place_order(target, source=BookingSource.AUTO, **kwargs)

    async def test_auth_refresh_yields_to_activity_without_account_locks(self):
        keeper = AuthKeeper.__new__(AuthKeeper)
        self.e._reserve_inflight = set()
        self.e.booking = self.service
        keeper.engine = self.e
        entered, release = asyncio.Event(), asyncio.Event()
        async def query(*args):
            entered.set()
            await release.wait()
            return FREE
        self.query.side_effect = query
        task = asyncio.create_task(self.book(self.bad))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(keeper._busy())
        finally:
            release.set()
            await asyncio.wait_for(task, 1)
        self.assertFalse(keeper._busy())

    async def test_booking_preparation_outranks_scans_and_restores_context(self):
        seen = []
        async def query(*args):
            seen.append(request_priority.get())
            return FREE
        async def solve(**kwargs):
            seen.append(request_priority.get())
            return "fixture", {}
        self.query.side_effect = query
        previous = request_priority.set(RUSH_SCAN_PRIORITY)
        try:
            with patch("core.booking_service.get_vid_once", AsyncMock(side_effect=solve)):
                self.assertTrue((await self.book(self.ten)).success)
            self.assertEqual(request_priority.get(), RUSH_SCAN_PRIORITY)
        finally:
            request_priority.reset(previous)
        self.assertEqual(seen, [BOOKING_PRIORITY, BOOKING_PRIORITY])

    async def test_two_sports_send_without_waiting_for_other_response(self):
        both_sent = asyncio.Event()
        sent = []

        async def save(**kwargs):
            sent.append(kwargs)
            if len(sent) == 2:
                both_sent.set()
            await asyncio.wait_for(both_sent.wait(), 1)
            self.assertTrue(self.service.has_active_orders)
            return {"success": True, "sent": True}

        self.save.side_effect = save
        outcomes = await asyncio.wait_for(asyncio.gather(
            self.book(self.bad, run_id=self.service.new_run_id()),
            self.book(self.ten, run_id=self.service.new_run_id())), 2)
        self.assertTrue(all(o.success for o in outcomes))
        self.assertEqual({s["vid"] for s in sent}, {"first", "second"})
        self.assertEqual(len(self.service._journal.load()), 2)
        self.assertFalse(self.service.has_active_orders)
        self.assertTrue(all("account_queue_ms" not in o.diagnostics["timing"] for o in outcomes))

    async def test_overlap_race_after_both_initial_checks_sends_once(self):
        both_preparing = asyncio.Event()
        arrivals = 0

        async def query(*args):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_preparing.set()
            await asyncio.wait_for(both_preparing.wait(), 1)
            return FREE

        self.query.side_effect = query
        overlap = BookingTarget(self.ten.court, self.bad.date, "17:00", "18:00")
        outcomes = await asyncio.wait_for(asyncio.gather(self.book(self.bad), self.book(overlap)), 2)
        self.assertEqual(sum(o.success for o in outcomes), 1)
        self.assertEqual(sum(o.blocked for o in outcomes), 1)
        self.assertIn("order_conflict", [o.diagnostics["reason"] for o in outcomes])
        self.save.assert_awaited_once()

    async def test_shared_pool_claim_starts_missing_vid_before_rechecks_finish(self):
        inventory = Engine.__new__(Engine)
        inventory.tuning = self.e.tuning
        inventory._vid_queue = asyncio.Queue(maxsize=3)
        inventory._vid_queue.put_nowait((time.monotonic(), "pooled"))
        self.e.claim_vid = inventory.claim_vid
        self.e.return_vid = inventory.return_vid
        generated = asyncio.Event()

        async def query(*args):
            # Both would stall here if they merely peeked at the same pooled VID.
            await asyncio.wait_for(generated.wait(), 1)
            return FREE

        async def generate(**kwargs):
            generated.set()
            return "generated", {}

        self.query.side_effect = query
        take = AsyncMock(return_value=None)
        with patch("core.booking_service.get_vid_once", AsyncMock(side_effect=generate)) as solve:
            outcomes = await asyncio.wait_for(asyncio.gather(
                self.book(self.bad, take_vid=take), self.book(self.ten, take_vid=take)), 2)
        self.assertTrue(all(o.success for o in outcomes))
        solve.assert_awaited_once()
        take.assert_not_called()
        self.assertEqual({c.kwargs["vid"] for c in self.save.await_args_list}, {"pooled", "generated"})

    async def test_uncertain_in_one_window_does_not_block_other_window(self):
        self.save.side_effect = [
            {"success": False, "uncertain": True, "sent": True},
            {"success": True, "sent": True}]
        first = await self.book(self.bad)
        second = await self.book(self.ten)
        repeated = await self.book(self.bad)
        self.assertTrue(first.uncertain)
        self.assertTrue(second.success)
        self.assertTrue(repeated.blocked)
        self.assertEqual(self.save.await_count, 2)

    async def test_same_run_reentry_rejected_and_cancellation_releases_activity(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def query(*args):
            entered.set()
            await release.wait()
            return FREE

        self.query.side_effect = query
        run_id = self.service.new_run_id()
        task = asyncio.create_task(self.book(self.bad, run_id=run_id))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            rejected = await self.book(self.ten, run_id=run_id)
            self.assertTrue(rejected.blocked)
            self.assertEqual(rejected.diagnostics["reason"], "run_in_flight")
            self.assertTrue(self.service.has_active_orders)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.service.has_active_orders)
        self.assertFalse(self.service._runs[run_id].in_flight)
        self.query.side_effect = None
        self.assertTrue((await self.book(self.bad, run_id=run_id)).success)
        self.assertTrue((await self.book(self.ten, run_id=run_id)).blocked)
        self.save.assert_awaited_once()

    async def test_one_completion_does_not_clear_other_active_order(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def query(court, *args):
            if court.id == "badminton":
                entered.set()
                await release.wait()
            return FREE

        self.query.side_effect = query
        first = asyncio.create_task(self.book(self.bad))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue((await self.book(self.ten)).success)
            self.assertTrue(self.service.has_active_orders)
        finally:
            release.set()
            await asyncio.wait_for(first, 1)
        self.assertFalse(self.service.has_active_orders)
