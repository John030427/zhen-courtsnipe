import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.booking_service import BookingService, BookingSource, BookingTarget
from core.order_journal import OrderJournal
from core.safety import set_order_dry_run_for_tests
from core.sports import DEFAULT_TENNIS_COURTS, DEFAULT_TENNIS_GYM_ID
from utils import Court


def engine_stub(**kw):
    court = Court(1, "gid-1", "1号场")
    tokens = SimpleNamespace(
        is_degraded_for_orders=lambda: False,
        auth_revision=1,
        public_view=lambda: {},
    )
    defaults = {
        "token": "tok",
        "user_id": "user-a",
        "customer_id": "cid",
        "customer_name": "n",
        "customer_tel": "t",
        "gym_id": "gym",
        "gym_name": "gym",
        "sport_for_ground_id": lambda _gid: "badminton",
        "venue_for_sport": lambda _sid: ("gym", "gym"),
        "config_data": {"safety": {"max_order_attempts": 1, "max_orders_per_run": 1}},
        "tuning": SimpleNamespace(vid_timeout=1, vid_max_age=30),
        "tokens": tokens,
        "court_by_no": {1: court},
        "_solve_sem": asyncio.Semaphore(1),
        "bus": SimpleNamespace(publish=AsyncMock()),
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class BookingServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jpath = Path(self.temp.name) / "journal.json"
        set_order_dry_run_for_tests(True)

    def tearDown(self):
        set_order_dry_run_for_tests(True)
        self.temp.cleanup()

    def svc(self, engine):
        s = BookingService(engine)
        s._journal = OrderJournal(self.jpath)
        return s

    async def test_dry_run_never_posts(self):
        e = engine_stub()
        svc = self.svc(e)
        target = BookingTarget(e.court_by_no[1], "2030-06-01", "19:00", "20:00")
        with patch("core.booking_service.get_order_once", AsyncMock(return_value={
            "ok": True, "avail": {"19:00": "1", "19:30": "1"}, "blocks": [],
        })), patch("core.booking_service.get_vid_once", AsyncMock()) as vid_mock, patch(
            "utils.get_client",
        ) as gc:
            client = MagicMock()
            client.post = AsyncMock()
            gc.return_value = client
            out = await svc.place_order(target, source=BookingSource.MANUAL)
        client.post.assert_not_called()
        vid_mock.assert_not_called()
        self.assertTrue(out.dry_run)

    async def test_concurrent_lock_single_flight(self):
        e = engine_stub()
        svc = self.svc(e)
        target = BookingTarget(e.court_by_no[1], "2030-06-01", "19:00", "20:00")
        calls = 0

        async def slow_query(*a, **k):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return {"ok": True, "avail": {"19:00": "1", "19:30": "1"}, "blocks": []}

        with patch("core.booking_service.get_order_once", slow_query), patch(
            "core.booking_service.get_vid_once", AsyncMock(return_value=("vid", {})),
        ):
            t1 = asyncio.create_task(svc.place_order(target, source=BookingSource.MANUAL))
            t2 = asyncio.create_task(svc.place_order(target, source=BookingSource.MANUAL))
            await asyncio.gather(t1, t2)
        self.assertGreaterEqual(calls, 2)

    async def test_account_change_does_not_block_other_account(self):
        journal = OrderJournal(self.jpath)
        journal.persist_record({
            "account_id": "old-user",
            "ground_id": "gid-1",
            "date": "2030-06-01",
            "court": 1,
            "start": "19:00",
            "end": "20:00",
            "state": "success",
            "time": "t",
        })
        e = engine_stub(user_id="new-user")
        svc = self.svc(e)
        target = BookingTarget(e.court_by_no[1], "2030-06-01", "19:00", "20:00")
        with patch("core.booking_service.get_order_once", AsyncMock(return_value={
            "ok": True, "avail": {"19:00": "1", "19:30": "1"}, "blocks": [],
        })), patch("core.booking_service.get_vid_once", AsyncMock(return_value=("vid", {}))):
            out = await svc.place_order(target, source=BookingSource.MANUAL)
        self.assertFalse(out.blocked)

    async def test_restart_journal_blocks_duplicate(self):
        journal = OrderJournal(self.jpath)
        journal.persist_record({
            "account_id": "user-a",
            "ground_id": "gid-1",
            "date": "2030-06-01",
            "court": 1,
            "start": "19:00",
            "end": "20:00",
            "state": "pending",
            "time": "t",
        })
        e = engine_stub()
        svc = self.svc(e)
        target = BookingTarget(e.court_by_no[1], "2030-06-01", "19:00", "20:00")
        out = await svc.place_order(target, source=BookingSource.MANUAL)
        self.assertTrue(out.blocked)

    async def test_overlap_blocks(self):
        journal = OrderJournal(self.jpath)
        journal.persist_record({
            "account_id": "user-a",
            "ground_id": "gid-2",
            "date": "2030-06-01",
            "court": 2,
            "start": "19:30",
            "end": "20:30",
            "state": "uncertain",
            "time": "t",
        })
        e = engine_stub()
        svc = self.svc(e)
        target = BookingTarget(e.court_by_no[1], "2030-06-01", "19:00", "20:00")
        out = await svc.place_order(target, source=BookingSource.MANUAL)
        self.assertTrue(out.blocked)

    async def test_uncertain_persisted(self):
        set_order_dry_run_for_tests(False)
        e = engine_stub()
        svc = self.svc(e)
        target = BookingTarget(e.court_by_no[1], "2030-06-01", "19:00", "20:00")
        with patch("core.booking_service.get_order_once", AsyncMock(return_value={
            "ok": True, "avail": {"19:00": "1", "19:30": "1"}, "blocks": [],
        })), patch("core.booking_service.get_vid_once", AsyncMock(return_value=("vid", {}))), patch(
            "core.booking_service.save_order_once",
            AsyncMock(return_value={"success": False, "uncertain": True, "msg": "timeout"}),
        ):
            out = await svc.place_order(target, source=BookingSource.MANUAL)
        self.assertTrue(out.uncertain)
        records = OrderJournal(self.jpath).load()
        self.assertEqual(records[-1]["state"], "uncertain")

    async def test_terminal_rejection_stops_run_without_blocking_future_corrected_order(self):
        set_order_dry_run_for_tests(False)
        e = engine_stub()
        svc = self.svc(e)
        target = BookingTarget(e.court_by_no[1], "2030-06-01", "19:00", "20:00")
        with patch("core.booking_service.get_order_once", AsyncMock(return_value={
            "ok": True, "avail": {"19:00": "1", "19:30": "1"}, "blocks": [],
        })), patch("core.booking_service.get_vid_once", AsyncMock(return_value=("vid", {}))), patch(
            "core.booking_service.save_order_once", AsyncMock(return_value={
                "success": False, "terminal_rejection": True, "sent": True, "msg": "时长超限",
            }),
        ):
            out = await svc.place_order(target, source=BookingSource.MANUAL)
        self.assertTrue(out.blocked)
        self.assertFalse(out.uncertain)
        self.assertEqual(OrderJournal(self.jpath).load()[-1]["state"], "failed")
        self.assertFalse(svc._journal.find_blockers("user-a", date=target.date,
                                                  start=target.start, end=target.end))

    async def test_tennis_court_uses_tennis_gym_on_save(self):
        set_order_dry_run_for_tests(False)
        tennis_court = Court(6, DEFAULT_TENNIS_COURTS[6], "6号场")
        e = engine_stub(
            gym_id="badminton-gym",
            gym_name="羽毛球馆",
            tennis_gym_id=DEFAULT_TENNIS_GYM_ID,
            tennis_gym_name="欣园网球场",
            courts_by_sport={"tennis": [tennis_court]},
        )
        e.sport_for_ground_id = lambda gid: "tennis" if gid == tennis_court.id else "badminton"
        e.venue_for_sport = lambda sid: (
            (DEFAULT_TENNIS_GYM_ID, "欣园网球场")
            if sid == "tennis"
            else ("badminton-gym", "羽毛球馆")
        )
        svc = self.svc(e)
        target = BookingTarget(tennis_court, "2030-06-01", "19:00", "20:00")
        captured: dict = {}

        async def capture_save(**kwargs):
            captured.update(kwargs)
            return {"success": True, "msg": "ok"}

        with patch("core.booking_service.get_order_once", AsyncMock(return_value={
            "ok": True, "avail": {"19:00": "1", "19:30": "1"}, "blocks": [],
        })), patch("core.booking_service.get_vid_once", AsyncMock(return_value=("vid", {}))), patch(
            "core.booking_service.save_order_once", AsyncMock(side_effect=capture_save),
        ):
            out = await svc.place_order(target, source=BookingSource.MANUAL)
        self.assertTrue(out.success)
        self.assertEqual(captured.get("gym_id"), DEFAULT_TENNIS_GYM_ID)
        self.assertEqual(captured.get("gym_name"), "欣园网球场")
        self.assertNotEqual(captured.get("gym_id"), "badminton-gym")

    async def test_bypass_save_order_without_internal_flag(self):
        set_order_dry_run_for_tests(False)
        court = Court(1, "g", "1号场")
        with patch("utils.get_client") as gc:
            client = MagicMock()
            client.post = AsyncMock()
            gc.return_value = client
            from utils import save_order_once
            r = await save_order_once(
                vid="v", court=court, date="2030-01-01", start="19:00", end="20:00",
                token="t", user_id="u", customer_id="c", customer_name="n",
                customer_tel="tel", gym_id="g", gym_name="gn",
            )
        client.post.assert_not_called()
        self.assertIn("BookingService", r.get("msg", ""))


if __name__ == "__main__":
    unittest.main()
