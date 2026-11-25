"""Offline: an existing order only stops an auto-booking watch for the same sport and overlapping
hours, the same rule as at order time. Badminton and tennis can both be watched and booked."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.order_journal import OrderJournal
from core.watch_engine import WatchEngine
from models.plans import CHINA, Target, WatchPlan

GROUNDS = {"bad-9": "badminton", "ten-3": "tennis"}


class ExistingOrderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.journal = OrderJournal(Path(tmp.name) / "watch-orders.json")
        patcher = patch("core.watch_engine.OrderJournal", lambda: self.journal)
        patcher.start()
        self.addCleanup(patcher.stop)
        courts = [SimpleNamespace(no=n, id=f"g{n}") for n in (1, 2)]
        self.engine = SimpleNamespace(
            token="fixture", user_id="fixture-user", vid_running=False, _reserve_inflight=set(),
            auto=SimpleNamespace(is_running=lambda _sid: False, running=False),
            courts_for_sport=lambda _sid: courts, sport_for_ground_id=lambda gid: GROUNDS.get(gid, "badminton"))

    def booked(self, ground_id, start="18:00", end="20:00", state="success", date="2030-06-02"):
        rec = {"account_id": "fixture-user", "ground_id": ground_id, "date": date, "start": start,
               "end": end, "state": state}
        self.assertTrue(self.journal.persist_record(rec))

    def plan(self, sport, start="18:00", end="22:00", auto=True):
        with patch("models.plans.now", return_value=datetime(2030, 6, 1, 12, tzinfo=CHINA)):
            return WatchPlan(sport_id=sport, dates=["2030-06-02"], courts=[1, 2], auto_reserve=auto,
                             targets=[Target(start=start, end=end)])

    def blocks(self, sport, **kw):
        return WatchEngine(self.engine)._existing_order_blocks(self.plan(sport, **kw), sport)

    def test_badminton_booked_does_not_stop_a_tennis_watch_for_the_same_hours(self):
        self.booked("bad-9")
        self.assertFalse(self.blocks("tennis"))

    def test_same_sport_overlapping_hours_is_still_refused(self):
        for state in ("success", "pending", "uncertain"):
            with self.subTest(state=state):
                self.journal._path.unlink(missing_ok=True)
                self.booked("ten-3", state=state)
                self.assertTrue(self.blocks("tennis"))

    def test_same_sport_other_hours_or_other_day_is_allowed(self):
        self.booked("bad-9", start="10:00", end="12:00")
        self.booked("bad-9", date="2030-06-03")
        self.booked("bad-9", state="failed")
        self.assertFalse(self.blocks("badminton"))

    def test_order_without_a_court_still_blocks_every_sport(self):
        self.booked("")
        self.assertTrue(self.blocks("tennis"))

    def test_start_refuses_only_the_watch_that_clashes(self):
        self.booked("bad-9")
        import asyncio
        refused = asyncio.run(WatchEngine(self.engine).start(self.plan("badminton")))
        self.assertFalse(refused["ok"])
        self.assertIn("这个运动", refused["msg"])


if __name__ == "__main__":
    unittest.main()
