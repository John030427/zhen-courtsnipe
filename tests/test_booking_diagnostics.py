import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.booking_service import BookingService, BookingSource, BookingTarget
from core.order_guard import VidLease
from core.order_journal import OrderJournal
from core.safety import set_order_dry_run_for_tests
from core.rush_engine import RushEngine
from engine import Engine
from models.rush import CHINA, RushPlan, RushWindow
from tests.test_booking_service import engine_stub
from utils import CaptchaParseError, Court


FREE = {"ok": True, "avail": {"19:00": "1", "19:30": "1"}, "blocks": []}


class BookingDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = patch("core.booking_service.audit").start()
        self.addCleanup(patch.stopall)
        self.e = engine_stub()
        self.e.tokens.mark_expired = lambda _: None
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(self.tmp.name) / "orders.json")
        self.target = BookingTarget(self.e.court_by_no[1], "2030-06-03", "19:00", "20:00")
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)

    async def order(self, **kwargs):
        return await self.service.place_order(
            self.target, source=BookingSource.AUTO, run_id="fixture-rush", **kwargs)

    async def test_recheck_timeout_is_not_misreported_as_no_free_court(self):
        with patch("core.booking_service.get_order_once", AsyncMock(return_value={
            "ok": False, "error_kind": "network", "error": "ReadTimeout"
        })), patch("core.booking_service.get_vid_once", AsyncMock()) as vid, \
                patch("core.booking_service.save_order_once", AsyncMock()) as save:
            out = await self.order()
        self.assertEqual(out.diagnostics["reason"], "recheck_network")
        self.assertEqual(out.diagnostics["recheck_errors"], 1)
        self.assertFalse(out.diagnostics["sent"])
        vid.assert_not_called()
        save.assert_not_called()
        self.assertIn("recheck_ms", out.diagnostics["timing"])
        event = self.audit.call_args
        self.assertEqual(event.args[0], "booking.outcome")
        self.assertEqual(event.kwargs["run_id"], "fixture-rush")
        self.assertNotIn("token", event.kwargs)

    async def test_vid_failure_is_measured_without_leaking_exception_content(self):
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)), \
                patch("core.booking_service.get_vid_once", AsyncMock(
                    side_effect=CaptchaParseError("private-payload"))), \
                patch("core.booking_service.save_order_once", AsyncMock()) as save:
            out = await self.order()
        self.assertEqual(out.diagnostics["reason"], "vid_unavailable")
        self.assertEqual(out.diagnostics["vid_errors"], 1)
        self.assertIn("vid_generate_ms", out.diagnostics["timing"])
        self.assertIn("vid_semaphore_ms", out.diagnostics["timing"])
        self.assertNotIn("private-payload", str(self.audit.call_args_list))
        save.assert_not_called()

    async def test_success_keeps_stage_times_and_actual_send_flag(self):
        lease = VidLease("private-vid", time.monotonic())
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)), \
                patch("core.booking_service.get_vid_once", AsyncMock()) as solve, \
                patch("core.booking_service.save_order_once", AsyncMock(
                    return_value={"success": True, "sent": True})):
            out = await self.order(take_vid=AsyncMock(return_value=lease))
        self.assertTrue(out.success)
        self.assertEqual(out.diagnostics["order_sends"], 1)
        self.assertTrue(out.diagnostics["sent"])
        self.assertEqual(out.diagnostics["reason"], "success")
        for stage in ("guards_ms", "recheck_ms", "vid_pool_ms", "submit_ms"):
            self.assertGreaterEqual(out.diagnostics["timing"][stage], 0)
        solve.assert_not_called()
        self.assertNotIn("private-vid", str(self.audit.call_args_list))

    async def test_cancelled_recheck_is_audited_and_cancellation_propagates(self):
        with patch("core.booking_service.get_order_once", AsyncMock(
                side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.order()
        self.assertEqual(self.audit.call_args.kwargs["reason"], "cancelled")
        self.assertEqual(self.audit.call_args.kwargs["stage"], "recheck")
        self.assertFalse(self.audit.call_args.kwargs["sent"])

    async def test_expired_deadline_after_solver_queue_does_not_generate(self):
        self.e._solve_sem = asyncio.Semaphore(0)
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)), \
                patch("core.booking_service.get_vid_once", AsyncMock()) as solve:
            task = asyncio.create_task(self.order(deadline_mono=time.monotonic() + .01))
            await asyncio.sleep(.03)
            self.e._solve_sem.release()
            out = await task
        self.assertTrue(out.blocked)
        solve.assert_not_called()

    async def test_recheck_auth_failure_marks_only_current_auth(self):
        mark = self.e.tokens.mark_expired = Mock()
        with patch("core.booking_service.get_order_once", AsyncMock(
                return_value={"ok": False, "auth_fail": True})):
            out = await self.order()
        mark.assert_called_once()
        self.assertTrue(out.blocked)
        self.assertEqual(out.diagnostics["reason"], "recheck_auth")

    async def test_old_recheck_auth_failure_does_not_expire_new_credentials(self):
        mark = self.e.tokens.mark_expired = Mock()
        async def query(*_):
            self.e.tokens.auth_revision += 1
            return {"ok": False, "auth_fail": True}
        with patch("core.booking_service.get_order_once", query):
            out = await self.order()
        mark.assert_not_called()
        self.assertTrue(out.blocked)

    async def test_rush_summary_includes_recheck_and_vid_failures(self):
        now = datetime(2030, 6, 1, 19, 59, 58, tzinfo=CHINA)
        async def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)
        self.e.courts_for_sport = lambda _: [Court(i, f"ground-{i}", f"Court {i}") for i in (1, 4, 7)]
        self.e.vid_running = False
        self.e.start_vid_pool = AsyncMock(return_value={"ok": True})
        self.e.stop_vid_pool = AsyncMock()
        self.e.fetch_token = AsyncMock(return_value={"ok": True})
        self.e.take_vid = AsyncMock(return_value=None)
        self.e.booking = self.service
        rush = RushEngine(self.e, clock_now=lambda: now, sleep=sleep)
        plan = RushPlan(date=self.target.date, release_at=now + timedelta(seconds=2),
                        windows=[RushWindow(start="19:00", end="20:00")])
        scan = {**FREE, "blocks": [
            {"time": "19:00", "endTime": "19:30", "status": "1"},
            {"time": "19:30", "endTime": "20:00", "status": "1"}]}
        with patch("core.rush_engine.get_order_once", AsyncMock(return_value=scan)), \
                patch("core.rush_engine.record") as audit, \
                patch("core.booking_service.get_order_once", AsyncMock(side_effect=[
                    {"ok": False, "error_kind": "network"}, FREE, FREE])), \
                patch("core.booking_service.get_vid_once", AsyncMock(side_effect=[
                    CaptchaParseError("fixture"), CaptchaParseError("fixture"), ("fixture-vid", {})])) as solve, \
                patch("core.booking_service.save_order_once", AsyncMock(
                    return_value={"success": True, "sent": True})) as save:
            self.assertTrue((await rush.start(plan))["ok"])
            await rush.task
        self.assertEqual(solve.await_count, 3)  # the second court got one captcha retry
        self.assertEqual(rush.phase, "success")
        save.assert_awaited_once()
        self.assertEqual(rush.diagnostics["query_errors"], 0)
        self.assertEqual(rush.diagnostics["recheck_errors"], 1)
        self.assertEqual(rush.diagnostics["vid_errors"], 1)
        self.assertEqual(rush.diagnostics["order_sends"], 1)
        self.assertEqual(rush.diagnostics["not_sent"], 2)
        summary = [c.kwargs for c in audit.call_args_list if c.args[0] == "rush.summary"][-1]
        self.assertEqual(summary["recheck_queries"], 3)
        self.assertEqual([a["diagnostics"]["reason"] for a in rush.applied],
                         ["recheck_network", "vid_unavailable", "success"])


class CaptchaOverlapTests(unittest.IsolatedAsyncioTestCase):
    """The captcha may be solved while the recheck is in flight, never at extra concurrency."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = patch("core.booking_service.audit").start()
        self.addCleanup(patch.stopall)
        self.returned = []
        self.headroom = 0.0
        self.e = engine_stub(vid_headroom=lambda: self.headroom, return_vid=self.returned.append)
        self.e.tokens.mark_expired = lambda _: None
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(self.tmp.name) / "orders.json")
        self.target = BookingTarget(self.e.court_by_no[1], "2030-06-03", "19:00", "20:00")
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)

    async def order(self):
        return await self.service.place_order(self.target, source=BookingSource.AUTO, run_id="fixture-rush",
                                              take_vid=AsyncMock(return_value=None))

    async def test_captcha_starts_while_the_recheck_is_in_flight(self):
        order = []
        recheck_done = asyncio.Event()
        async def recheck(*_):
            order.append("recheck sent")
            await asyncio.sleep(0.05)
            recheck_done.set()
            return FREE
        async def solve(**_):
            order.append("captcha started" + (" after recheck" if recheck_done.is_set() else ""))
            return "fixture-vid", {}
        with patch("core.booking_service.get_order_once", recheck), \
                patch("core.booking_service.get_vid_once", solve), \
                patch("core.booking_service.save_order_once", AsyncMock(
                    return_value={"success": True, "sent": True})) as save:
            out = await self.order()
        self.assertTrue(out.success)
        self.assertEqual(order, ["recheck sent", "captcha started"])  # not "after recheck"
        self.assertEqual(save.await_args.kwargs["vid"], "fixture-vid")
        self.assertIn("vid_wait_ms", out.diagnostics["timing"])
        self.assertEqual(self.returned, [])

    async def test_failed_recheck_gives_the_captcha_back_to_the_pool(self):
        async def recheck(*args):
            await asyncio.sleep(0)  # allow the completed solve to be returned
            return {"ok": False, "error_kind": "network"}
        with patch("core.booking_service.get_order_once", recheck), \
                patch("core.booking_service.get_vid_once", AsyncMock(return_value=("fixture-vid", {}))), \
                patch("core.booking_service.save_order_once", AsyncMock()) as save:
            out = await self.order()
            await asyncio.sleep(0)  # let the background captcha finish
            await asyncio.sleep(0)
        self.assertEqual(out.diagnostics["reason"], "recheck_network")
        save.assert_not_called()
        self.assertEqual([lease.value for lease in self.returned], ["fixture-vid"])

    async def test_failed_recheck_cancels_unfinished_solve_before_returning(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def solve(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def recheck(*args):
            await asyncio.wait_for(started.wait(), 1)
            return {"ok": False, "error_kind": "network"}
        with patch("core.booking_service.get_order_once", recheck), \
                patch("core.booking_service.get_vid_once", solve), \
                patch("core.booking_service.save_order_once", AsyncMock()) as save:
            out = await asyncio.wait_for(self.order(), 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(out.diagnostics["reason"], "recheck_network")
        self.assertEqual(self.returned, [])
        self.assertFalse(self.service.has_active_orders)
        self.assertFalse(self.e._solve_sem.locked())
        save.assert_not_called()

    async def test_fresh_pool_means_no_early_captcha(self):
        self.headroom = 7.0
        lease = VidLease("pooled-vid", time.monotonic())
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)), \
                patch("core.booking_service.get_vid_once", AsyncMock()) as solve, \
                patch("core.booking_service.save_order_once", AsyncMock(
                    return_value={"success": True, "sent": True})) as save:
            out = await self.service.place_order(self.target, source=BookingSource.AUTO, run_id="fixture-rush",
                                                 take_vid=AsyncMock(return_value=lease))
        self.assertTrue(out.success)
        solve.assert_not_called()
        self.assertEqual(save.await_args.kwargs["vid"], "pooled-vid")

    async def test_reserved_pool_lease_is_used_without_another_take_or_solve(self):
        lease = VidLease("claimed-fixture", time.monotonic())
        self.e.claim_vid = Mock(return_value=lease)
        take = AsyncMock(return_value=None)
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)), \
                patch("core.booking_service.get_vid_once", AsyncMock()) as solve, \
                patch("core.booking_service.save_order_once", AsyncMock(
                    return_value={"success": True, "sent": True})) as save:
            out = await self.service.place_order(self.target, source=BookingSource.AUTO, take_vid=take)
        self.assertTrue(out.success)
        take.assert_not_called()
        solve.assert_not_called()
        self.assertEqual(save.await_args.kwargs["vid"], lease.value)
        self.assertEqual(self.returned, [])

    async def test_failed_recheck_returns_reserved_lease_with_original_age(self):
        lease = VidLease("claimed-fixture", time.monotonic() - 2)
        self.e.claim_vid = Mock(return_value=lease)
        with patch("core.booking_service.get_order_once", AsyncMock(return_value={"ok": False})), \
                patch("core.booking_service.get_vid_once", AsyncMock()) as solve, \
                patch("core.booking_service.save_order_once", AsyncMock()) as save:
            await self.order()
            await asyncio.sleep(0)
        self.assertEqual(self.returned, [lease])
        solve.assert_not_called()
        save.assert_not_called()

    async def test_reserved_vid_expiring_during_recheck_gets_bounded_replacement(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                # Claim was valid before recheck; emulate age crossing the limit during it.
                self.e.claim_vid = Mock(return_value=VidLease("expired-fixture", time.monotonic() - 60))
                self.service._journal = OrderJournal(Path(self.tmp.name) / f"expired-{fails}.json")
                with patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)), \
                        patch("core.booking_service.get_vid_once", AsyncMock(
                            side_effect=CaptchaParseError("fixture") if fails else None,
                            return_value=("replacement-fixture", {}))) as solve, \
                        patch("core.booking_service.save_order_once", AsyncMock(
                            return_value={"success": True, "sent": True})) as save:
                    out = await self.order()
                if fails:
                    self.assertEqual(solve.await_count, 2)
                    self.assertEqual(out.diagnostics["reason"], "vid_unavailable")
                    save.assert_not_called()
                else:
                    self.assertTrue(out.success)
                    solve.assert_awaited_once()
                    self.assertEqual(save.await_args.kwargs["vid"], "replacement-fixture")

    async def test_dry_run_never_solves_a_captcha(self):
        set_order_dry_run_for_tests(True)
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)), \
                patch("core.booking_service.get_vid_once", AsyncMock()) as solve:
            out = await self.order()
        self.assertTrue(out.dry_run)
        solve.assert_not_called()

    async def test_one_captcha_retry_then_success(self):
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)), \
                patch("core.booking_service.get_vid_once", AsyncMock(side_effect=[
                    CaptchaParseError("fixture"), ("fixture-vid", {})])) as solve, \
                patch("core.booking_service.save_order_once", AsyncMock(
                    return_value={"success": True, "sent": True})):
            out = await self.order()
        self.assertTrue(out.success)
        self.assertEqual(solve.await_count, 2)
        attempts = [c.kwargs.get("vid_attempt") for c in self.audit.call_args_list if c.args[0] == "booking.vid"]
        self.assertEqual(attempts, [1, 2])

    async def test_no_retry_when_the_deadline_is_too_close(self):
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=FREE)), \
                patch("core.booking_service.get_vid_once", AsyncMock(
                    side_effect=CaptchaParseError("fixture"))) as solve, \
                patch("core.booking_service.save_order_once", AsyncMock()) as save:
            out = await self.service.place_order(self.target, source=BookingSource.AUTO, run_id="fixture-rush",
                                                 take_vid=AsyncMock(return_value=None),
                                                 deadline_mono=time.monotonic() + 1.5)
        self.assertEqual(out.diagnostics["reason"], "vid_unavailable")
        self.assertEqual(solve.await_count, 1)
        save.assert_not_called()


class VidInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.e = Engine.__new__(Engine)
        self.e._vid_queue = asyncio.Queue(maxsize=3)
        self.e.tuning = SimpleNamespace(vid_max_age=8)
        self.audit = patch("core.audit.record").start()
        self.addCleanup(patch.stopall)

    async def test_take_freshest_preserves_timestamp_and_other_valid_lease(self):
        now = time.monotonic()
        for item in ((now - 7, "older"), (now - 1, "newer"), (now - 30, "expired")):
            self.e._vid_queue.put_nowait(item)
        lease = await self.e.take_vid(timeout=.05)
        self.assertEqual(lease.value, "newer")
        self.assertEqual(lease.issued_mono, now - 1)
        self.assertEqual(self.e.vid_count(), 1)
        self.assertEqual((await self.e.take_vid(timeout=.05)).value, "older")

    async def test_claim_reserves_freshest_once_and_keeps_short_lived_inventory(self):
        now = time.monotonic()
        self.e._vid_queue.put_nowait((now - 7, "older"))
        self.e._vid_queue.put_nowait((now - 1, "fresh"))
        claimed = self.e.claim_vid(min_remaining=3)
        self.assertEqual((claimed.value, claimed.issued_mono), ("fresh", now - 1))
        self.assertIsNone(self.e.claim_vid(min_remaining=3))
        self.assertEqual(self.e.vid_count(), 1)
        self.assertEqual((await self.e.take_vid(timeout=.05)).value, "older")

    async def test_headroom_and_returned_lease_keep_the_original_age(self):
        self.assertEqual(self.e.vid_headroom(), 0.0)
        issued = time.monotonic() - 3
        self.e.return_vid(VidLease("unused", issued))
        self.assertAlmostEqual(self.e.vid_headroom(), 5.0, delta=0.2)
        self.e.return_vid(VidLease("too-old", time.monotonic() - 9))
        self.assertEqual(self.e.vid_count(), 1)
        lease = await self.e.take_vid(timeout=.05)
        self.assertEqual((lease.value, lease.issued_mono), ("unused", issued))

    async def test_expired_inventory_is_not_displayed_as_ready(self):
        self.e._vid_queue.put_nowait((time.monotonic() - 30, "expired-secret"))
        self.assertEqual(self.e.vid_count(), 0)
        self.assertNotIn("expired-secret", str(self.audit.call_args_list))
        self.assertIsNone(await self.e.take_vid(timeout=.001))

    async def test_empty_pool_waits_for_new_item_with_original_age(self):
        task = asyncio.create_task(self.e.take_vid(timeout=.1))
        await asyncio.sleep(0)
        issued = time.monotonic()
        self.e._vid_queue.put_nowait((issued, "fresh"))
        lease = await task
        self.assertEqual(lease.issued_mono, issued)
        self.assertEqual(lease.value, "fresh")
