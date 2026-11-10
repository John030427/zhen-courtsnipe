"""Offline regression: scan -> submit -> next court, without duplicate queries."""
import tempfile
import time
import unittest
from dataclasses import replace
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


def free_result(court, date):
    return dict(ok=True, auth_fail=False, groundId=court.id, startDate=date, endDate=date,
                avail={"19:00": "1", "19:30": "1"}, blocks=[
                    dict(time="19:00", endTime="19:30", status="1", fee=0),
                    dict(time="19:30", endTime="20:00", status="1", fee="0")])


class ScanReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.e = engine_stub()
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(tmp.name) / "orders.json")
        self.target = BookingTarget(self.e.court_by_no[1], "2030-06-03", "19:00", "20:00")
        self.identity = tuple(getattr(self.e, k) for k in
                              ("token", "user_id", "customer_id", "customer_name", "customer_tel"))
        self.result = free_result(self.target.court, self.target.date)
        self.snapshot = ScanSnapshot.capture(self.result, self.identity, 1)
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)

    async def order(self, snapshot=None, source=BookingSource.AUTO):
        return await self.service.place_order(self.target, source=source,
            scan_snapshot=snapshot or self.snapshot,
            take_vid=AsyncMock(return_value=VidLease("fixture-vid", time.monotonic())))

    async def test_fresh_complete_scan_posts_without_recheck(self):
        # Capture owns its data; later matrix mutations cannot alter evidence.
        self.result["blocks"][0]["fee"] = 100
        with patch("core.booking_service.get_order_once", AsyncMock()) as query, \
                patch("core.booking_service.save_order_once", AsyncMock(
                    return_value=dict(success=True, sent=True))) as save, \
                patch("core.booking_service.audit") as audit:
            out = await self.order()
        self.assertTrue(out.success)
        query.assert_not_awaited()
        save.assert_awaited_once()
        self.assertEqual(out.diagnostics["recheck_queries"], 0)
        self.assertIn("scan_reuse_ms", out.diagnostics["timing"])
        events = [c.kwargs for c in audit.call_args_list if c.args[0] == "booking.scan_snapshot"]
        self.assertEqual(events[0]["action"], "reuse")
        self.assertGreaterEqual(events[0]["timing"]["scan_age_ms"], 0)
        self.assertNotIn("identity", events[0])
        self.assertNotIn("tok", repr(self.snapshot))

    async def test_invalid_snapshot_falls_back_to_recheck(self):
        cases = [replace(self.snapshot, received_mono=time.monotonic() - 3),
                 replace(self.snapshot, identity=("other",)),
                 replace(self.snapshot, auth_revision=0)]
        for changes in ({"groundId": "other"}, {"startDate": "2030-06-04"},
                        {"endDate": "2030-06-04"}, {"ok": False}, {"auth_fail": True},
                        {"blocks": []}, {"avail": {}},
                        {"blocks": [{**b, "endTime": "20:30"} for b in self.result["blocks"]]}):
            cases.append(replace(self.snapshot, result={**self.result, **changes}))
        set_order_dry_run_for_tests(True)
        for snapshot in cases:
            with self.subTest(snapshot=cases.index(snapshot)), \
                    patch("core.booking_service.get_order_once", AsyncMock(return_value=self.result)) as query:
                out = await self.order(snapshot)
            self.assertTrue(out.dry_run)
            query.assert_awaited_once()

    async def test_reply_without_a_fee_field_is_still_reused(self):
        # The recheck would read the same fee-less reply; the fee gate treats both alike.
        snapshot = replace(self.snapshot, result={**self.result, "blocks": [
            {k: v for k, v in b.items() if k != "fee"} for b in self.result["blocks"]]})
        set_order_dry_run_for_tests(True)
        with patch("core.booking_service.get_order_once", AsyncMock()) as query:
            out = await self.order(snapshot)
        self.assertTrue(out.dry_run)
        query.assert_not_awaited()

    async def test_manual_keeps_recheck_and_dry_run_never_submits(self):
        set_order_dry_run_for_tests(True)
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=self.result)) as query, \
                patch("core.booking_service.save_order_once", AsyncMock()) as save, \
                patch("core.booking_service.get_vid_once", AsyncMock()) as solve:
            out = await self.order(source=BookingSource.MANUAL)
            self.assertTrue(out.dry_run)
            query.assert_awaited_once()
            query.reset_mock()
            out = await self.order()
            self.assertTrue(out.dry_run)
            query.assert_not_awaited()
        save.assert_not_awaited()
        solve.assert_not_awaited()

    async def test_reused_scan_still_blocks_paid_court(self):
        self.snapshot.result["blocks"][0]["fee"] = 10
        with patch("core.booking_service.get_order_once", AsyncMock()) as query, \
                patch("core.booking_service.save_order_once", AsyncMock()) as save:
            out = await self.order()
        self.assertTrue(out.blocked)
        self.assertEqual(out.diagnostics["reason"], "fee_blocked")
        query.assert_not_awaited()
        save.assert_not_awaited()

    async def test_identity_change_during_vid_wait_prevents_send(self):
        async def take_vid():
            self.e.token = "changed"
            return VidLease("fixture-vid", time.monotonic())
        with patch("core.booking_service.get_order_once", AsyncMock()) as query, \
                patch("core.booking_service.save_order_once", AsyncMock()) as save:
            out = await self.service.place_order(self.target, source=BookingSource.AUTO,
                scan_snapshot=self.snapshot, take_vid=take_vid)
        self.assertTrue(out.blocked)
        query.assert_not_awaited()
        save.assert_not_awaited()

    async def test_snapshot_does_not_bypass_deadline_or_uncertain_journal(self):
        with patch("core.booking_service.get_order_once", AsyncMock()) as query, \
                patch("core.booking_service.save_order_once", AsyncMock()) as save:
            out = await self.service.place_order(self.target, source=BookingSource.AUTO,
                scan_snapshot=self.snapshot, deadline_mono=time.monotonic() - 1)
            self.assertTrue(out.blocked)
            self.service._journal.persist_record(dict(account_id=self.e.user_id,
                ground_id=self.target.court.id, date=self.target.date, court=1,
                start="19:00", end="20:00", state="uncertain", time="fixture"))
            out = await self.order()
            self.assertTrue(out.blocked)
        query.assert_not_awaited()
        save.assert_not_awaited()

    async def run_rush(self, reply):
        now = datetime(2030, 6, 1, 19, 59, 58, tzinfo=CHINA)
        async def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)
        self.e.courts_for_sport = lambda _: [Court(i, f"gid-{i}", f"Court {i}") for i in (1, 2)]
        self.e.vid_running = False
        self.e.start_vid_pool = AsyncMock(return_value={"ok": True})
        self.e.stop_vid_pool = AsyncMock()
        self.e.fetch_token = AsyncMock(return_value={"ok": True})
        self.e.take_vid = AsyncMock(side_effect=lambda **_: VidLease("fixture-vid", time.monotonic()))
        self.e.config_data["safety"]["max_order_attempts"] = 2
        self.e.booking = self.service
        rush = RushEngine(self.e, clock_now=lambda: now, sleep=sleep)
        plan = RushPlan(date=self.target.date, release_at=now + timedelta(seconds=2),
                        windows=[RushWindow(start="19:00", end="20:00")], court_priority=[1, 2])
        steps = []
        async def query(court, date, *_args, **_kwargs):
            steps.append(("scan", court.no))
            return free_result(court, date)
        async def save(**kwargs):
            kwargs["_before_send"]()
            steps.append(("send", kwargs["court"].no))
            return reply if kwargs["court"].no == 1 else dict(success=True, sent=True)
        with patch("core.rush_engine.get_order_once", query), \
                patch("core.booking_service.get_order_once", AsyncMock()) as recheck, \
                patch("core.booking_service.save_order_once", save):
            self.assertTrue((await rush.start(plan))["ok"])
            await rush.task
        recheck.assert_not_awaited()
        return rush, steps

    async def test_definite_rejection_moves_to_next_court_no_same_court_retry(self):
        rush, steps = await self.run_rush(dict(success=False, sent=True, msg="busy"))
        self.assertEqual(steps, [("scan", 1), ("send", 1), ("scan", 2), ("send", 2)])
        self.assertEqual(rush.phase, "success")

    async def test_uncertain_order_stops_without_scanning_next_court(self):
        rush, steps = await self.run_rush(dict(success=False, sent=True, uncertain=True))
        self.assertEqual(steps, [("scan", 1), ("send", 1)])
        self.assertEqual(rush.reason, "result_uncertain")
        self.assertEqual(self.service._journal.load()[-1]["state"], "uncertain")
