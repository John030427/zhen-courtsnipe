"""Weekly not-bookable slots, learned from the workbench's own queries. Offline."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import httpx
from unittest.mock import patch

import utils
from core.closed_slots import ClosedSlots
from models.rush import CHINA
from utils import Court


def blocks(statuses: dict[str, str]):
    return [{"time": t, "endTime": "", "status": s} for t, s in statuses.items()]


EVENING = ["19:00", "19:30", "20:00", "20:30", "21:00", "21:30"]


class ClosedSlotsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "closed-slots.json"
        self.now = datetime(2026, 11, 22, 20, 30, tzinfo=CHINA)   # Sunday evening
        self.book = ClosedSlots(lambda c: "badminton", path=self.path, now=lambda: self.now)

    def day(self, date, closed=("20:00", "20:30", "21:00", "21:30"), courts=range(1, 11), except_courts=()):
        for n in courts:
            st = {t: ("0" if t in closed and n not in except_courts else "1") for t in EVENING}
            self.book.observe(Court(n, f"g{n}", f"{n}号场"), date, blocks(st))

    def test_only_released_dates_count(self):
        # 11-24 (Tuesday) opens at 20:00 on 11-22: at 19:59 its zeros mean "not released yet".
        self.now = datetime(2026, 11, 22, 19, 59, tzinfo=CHINA)
        self.day("2026-11-24")
        self.assertEqual(self.book.data, {})
        self.now = datetime(2026, 11, 22, 20, 1, tzinfo=CHINA)
        self.day("2026-11-24")
        self.assertIn("2026-11-24", self.book.data["badminton"])

    def test_weekly_ranges_need_every_court_closed(self):
        self.day("2026-11-10")
        self.day("2026-11-17")
        self.day("2026-11-18", except_courts=(1,))          # one court open: not a closed range
        self.assertEqual(self.book.weekly("badminton"), [
            {"weekday": "二", "weekday_no": 1, "start": "20:00", "end": "22:00", "dates": ["2026-11-10", "2026-11-17"]}])

    def test_same_weekday_last_time(self):
        self.day("2026-11-10", closed=("19:00",))
        self.day("2026-11-17")
        note = self.book.same_weekday_before("badminton", "2026-11-24", "20:00", "22:00")
        self.assertEqual((note["date"], note["weekday"], note["all_closed"], note["courts_seen"]),
                         ("2026-11-17", "二", True, 10))
        self.assertIsNone(self.book.same_weekday_before("badminton", "2026-11-25", "20:00", "22:00"))
        self.day("2026-11-18", except_courts=(1, 2))
        part = self.book.same_weekday_before("badminton", "2026-11-25", "20:00", "22:00")
        self.assertEqual((part["all_closed"], part["usable_courts"]), (False, [1, 2]))
        free = self.book.same_weekday_before("badminton", "2026-11-24", "19:00", "20:00")
        self.assertEqual(free["closed_courts"], [])            # 19:00–20:00 was bookable on 11-17

    def test_kept_across_restarts_and_old_days_dropped(self):
        self.day("2026-10-01")
        self.day("2026-11-17")
        self.book.flush()
        again = ClosedSlots(lambda c: "badminton", path=self.path, now=lambda: self.now)
        self.assertEqual(sorted(again.data["badminton"]), ["2026-11-17"])


class QueryHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_successful_query_is_seen(self):
        seen = []
        reply = {"success": True, "data": {"configList": [{"date": "2026-11-24", "timeBlockList": [
            {"time": "20:00", "endTime": "20:30", "status": "0", "customerName": "fixture"}]}]}}

        async def wait():
            return None

        utils.set_query_observer(lambda court, date, b: seen.append((court.no, date, b)))
        self.addCleanup(utils.set_query_observer, None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=reply))) as client:
            with patch("utils.get_client", return_value=client), patch("utils.wait_http_slot", wait), \
                    patch("core.venue_config_probe.log_order_time_config_from_server"):
                await utils.get_order_once(Court(3, "g3", "3号场"), "2026-11-24", "tok", "user")
        self.assertEqual(seen[0][:2], (3, "2026-11-24"))
        self.assertEqual(seen[0][2][0]["status"], "0")
        self.assertNotIn("customerName", seen[0][2][0])


if __name__ == "__main__":
    unittest.main()
