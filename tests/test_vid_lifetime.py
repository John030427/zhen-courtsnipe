"""A solved captcha is kept and reused; how old it may be is learned from what the school did
with real orders. Offline: saveOrder and the captcha solve are mocked."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.booking_service import BookingService, BookingSource, BookingTarget
from core.order_guard import VidLease
from core.order_journal import OrderJournal
from core.safety import set_order_dry_run_for_tests
from core.vid_lifetime import UNPROVEN_TRIES, VidLifetime
from engine import Engine
from tests.test_booking_service import engine_stub

FRESH, HOLD = 8.0, 90.0


class VidLifetimeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "vid-lifetime.json"
        self.life = VidLifetime(self.path)

    def note(self, age, **kw):
        base = dict(success=False, uncertain=False, message="该时段已被预约", fresh=FRESH)
        return self.life.note(age, **{**base, **kw})

    def test_kept_until_evidence_says_otherwise(self):
        self.assertEqual(self.life.limit(FRESH, HOLD), HOLD)
        self.assertEqual(self.life.limit(FRESH, 0), FRESH)  # holding switched off

    def test_success_proves_an_age_and_survives_a_restart(self):
        self.assertEqual(self.note(20, success=True, message="操作成功"), "proven")
        again = VidLifetime(self.path)
        self.assertEqual((again.proven, again.tries), (20, 0))

    def test_refusal_naming_the_captcha_caps_the_limit_for_good(self):
        self.assertEqual(self.note(40, message="验证码已失效，请重新验证"), "refused")
        self.assertEqual(self.life.limit(FRESH, HOLD), 32)
        self.note(30, message="验证码错误")
        self.assertEqual(VidLifetime(self.path).limit(FRESH, HOLD), 24)
        self.note(9, message="验证码错误")
        self.assertEqual(self.life.limit(FRESH, HOLD), FRESH)  # never below the fresh age

    def test_unclear_refusals_are_tried_only_a_few_times(self):
        self.note(15, success=True)
        for _ in range(UNPROVEN_TRIES):
            self.assertEqual(self.life.limit(FRESH, HOLD), HOLD)
            self.assertEqual(self.note(30), "unclear")
        self.assertEqual(self.life.limit(FRESH, HOLD), 15)  # back to what an order proved
        self.assertEqual(self.note(12), "")                 # within the proven age: nothing new
        self.note(50, success=True)                         # a new proof reopens the trying
        self.assertEqual(self.life.limit(FRESH, HOLD), HOLD)

    def test_fresh_or_unknown_results_teach_nothing(self):
        self.assertEqual(self.note(5, message="验证码错误"), "")
        self.assertEqual(self.note(30, uncertain=True), "")
        self.assertFalse(self.path.exists())


class KeptPoolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.e = Engine.__new__(Engine)
        self.e._vid_queue = asyncio.Queue(maxsize=3)
        self.e.tuning = SimpleNamespace(vid_max_age=FRESH, vid_hold_sec=HOLD, vid_pool_size=3)
        self.e.vid_lifetime = VidLifetime(Path(tmp.name) / "vid-lifetime.json")
        self.addCleanup(patch.stopall)
        patch("core.audit.record").start()

    def put(self, *ages):
        now = time.monotonic()
        for age in ages:
            self.e._vid_queue.put_nowait((now - age, f"vid-{age}"))

    def test_older_vid_stays_and_the_freshest_goes_first(self):
        self.put(30, 3)
        self.assertEqual(self.e.vid_count(), 2)
        self.assertEqual(self.e.claim_vid().value, "vid-3")
        self.assertEqual(self.e.claim_vid().value, "vid-30")

    def test_full_pool_means_no_more_solving(self):
        self.put(30, 20)
        self.assertFalse(self.e._vid_pool_full())
        self.put(5)
        self.assertTrue(self.e._vid_pool_full())
        self.e.tuning.vid_hold_sec = 0  # only fresh VIDs count: the pool keeps turning over
        self.assertFalse(self.e._vid_pool_full())

    def test_refused_age_shrinks_the_pool(self):
        self.put(30, 3)
        self.e.vid_lifetime.note(12, success=False, uncertain=False, message="验证码已过期", fresh=FRESH)
        self.assertAlmostEqual(self.e.vid_age_limit(), 9.6)
        self.assertEqual(self.e.vid_count(), 1)


class KeptVidOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.life = VidLifetime(Path(tmp.name) / "vid-lifetime.json")
        self.e = engine_stub(tuning=SimpleNamespace(vid_timeout=1, vid_max_age=FRESH), vid_lifetime=self.life)
        self.e.vid_age_limit = lambda: self.life.limit(FRESH, HOLD)
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(tmp.name) / "orders.json")
        self.target = BookingTarget(self.e.court_by_no[1], "2030-06-03", "19:00", "20:00")
        self.free = dict(ok=True, avail={"19:00": "1", "19:30": "1"}, blocks=[
            dict(time="19:00", endTime="19:30", status="1", fee=0),
            dict(time="19:30", endTime="20:00", status="1", fee=0)])
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)

    async def order(self, replies):
        sent = []

        async def save(**kwargs):
            kwargs["_before_send"]()
            sent.append(kwargs["vid"])
            return dict(replies.pop(0))

        kept = AsyncMock(return_value=VidLease("kept-vid", time.monotonic() - 20))
        with patch("core.booking_service.get_order_once", AsyncMock(return_value=self.free)), \
                patch("core.booking_service.save_order_once", save), \
                patch("core.booking_service.get_vid_once", AsyncMock(return_value=("new-vid", {}))):
            out = await self.service.place_order(self.target, source=BookingSource.AUTO, take_vid=kept)
        return out, sent

    async def test_kept_vid_that_works_is_proven(self):
        out, sent = await self.order([dict(success=True, sent=True)])
        self.assertTrue(out.success)
        self.assertEqual(sent, ["kept-vid"])
        self.assertGreaterEqual(self.life.proven, 20)
        self.assertGreaterEqual(out.diagnostics["vid_age_ms"], 20000)

    async def test_school_refuses_the_kept_vid_then_a_new_one_is_sent_once(self):
        out, sent = await self.order([dict(success=False, sent=True, msg="验证码已失效"),
                                      dict(success=True, sent=True)])
        self.assertTrue(out.success)
        self.assertEqual(sent, ["kept-vid", "new-vid"])
        self.assertGreaterEqual(self.life.refused, 20)
        self.assertLess(self.e.vid_age_limit(), 20)  # the pool will not hand out one that old again

    async def test_other_refusal_is_not_retried(self):
        out, sent = await self.order([dict(success=False, sent=True, msg="该时段已被预约")])
        self.assertFalse(out.success)
        self.assertEqual(sent, ["kept-vid"])
        self.assertEqual((self.life.tries, self.life.refused), (1, None))


if __name__ == "__main__":
    unittest.main()
