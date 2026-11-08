"""Rush scan budget: booked courts are looked at less often, a Rush whose targets are not released
steps behind the other sport's scans, and an empty ending says which of the two it was.
Offline: queries are mocked, orders run in test mode."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.request_priority import RUSH_SCAN_PRIORITY, RUSH_YIELD_PRIORITY, request_priority
from core.rush_engine import TAKEN_RECHECK_EVERY, RushEngine, court_view
from core.safety import set_order_dry_run_for_tests
from models.rush import RushPlan, RushWindow
from utils import Court

CHINA_TZ = timezone(timedelta(hours=8))


def window(*statuses, reason="unavailable_slots"):
    return {"reason": reason, "slots": [{"status": s} for s in statuses]}


def reply(statuses):
    """A query answer for 19:00–20:00 with the given 30-minute statuses."""
    times = ["19:00", "19:30"]
    return {"ok": True, "avail": dict(zip(times, statuses)),
            "blocks": [{"time": t, "endTime": e, "status": s}
                       for t, e, s in zip(times, ["19:30", "20:00"], statuses)]}


class CourtViewTests(unittest.TestCase):
    def test_views(self):
        self.assertEqual(court_view([window("1", "1", reason="match")]), "free")
        self.assertEqual(court_view([window("2", "2")]), "taken")
        self.assertEqual(court_view([window("0", "0")]), "closed")
        self.assertEqual(court_view([window("2", "1")]), "mixed")
        self.assertEqual(court_view([window("0", "2")]), "mixed")
        self.assertEqual(court_view([window("missing", "2")]), "mixed")


class RushBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        set_order_dry_run_for_tests(True)
        self.now = datetime(2030, 6, 1, 19, 59, 50, tzinfo=CHINA_TZ)

    async def asyncTearDown(self):
        set_order_dry_run_for_tests(True)

    async def sleep(self, sec):
        self.now += timedelta(seconds=sec)
        await asyncio.sleep(0)

    def engine(self, courts):
        booking = SimpleNamespace(
            preflight_windows=lambda *_: {"ok": True, "message": "fixture clear", "windows": []},
            new_run_id=lambda _: "rush-fixture", place_order=AsyncMock(), publish_result=AsyncMock())
        stub = SimpleNamespace(
            token="fixture-token", user_id="fixture-user", customer_id="c", customer_name="n", customer_tel="t",
            config_data={}, courts=courts, court_by_no={c.no: c for c in courts}, vid_running=False,
            tokens=SimpleNamespace(is_degraded_for_orders=lambda: False),
            fetch_token=AsyncMock(return_value={"ok": True}), start_vid_pool=AsyncMock(), stop_vid_pool=AsyncMock(),
            take_vid=AsyncMock(), booking=booking, bus=SimpleNamespace(publish=AsyncMock()),
            watcher=SimpleNamespace(state={"running": False}), _validation_sport_id=None)
        stub.courts_for_sport = lambda _sid: list(courts)
        return stub

    async def run_rush(self, answers, seconds=30):
        """answers: court no -> status pair. Each query takes one second, as the pacing does."""
        courts = [Court(n, f"g{n}", f"{n}号场") for n in sorted(answers)]
        e = self.engine(courts)
        rush = RushEngine(e, clock_now=lambda: self.now, sleep=self.sleep)
        asked, priorities = [], []

        async def query(court, *_a, **_k):
            asked.append(court.no)
            priorities.append(request_priority.get())
            self.now += timedelta(seconds=1)
            return reply(answers[court.no])

        plan = RushPlan(date="2030-06-03", release_at=datetime(2030, 6, 1, 20, 0, tzinfo=CHINA_TZ),
                        windows=[RushWindow(start="19:00", end="20:00")], token_refresh_lead_sec=2,
                        vid_warmup_sec=1, scan_window_sec=seconds, scan_interval_near_sec=1)
        with patch("core.rush_engine.get_order_once", side_effect=query):
            await rush.start(plan)
            for _ in range(5000):
                await asyncio.sleep(0)
                if not rush.running and rush.phase != "idle":
                    break
        return rush, asked, priorities

    async def test_booked_court_is_rechecked_every_third_round(self):
        rush, asked, _ = await self.run_rush({1: ("2", "2"), 2: ("1", "2")})
        rounds = rush.diagnostics["rounds"]
        self.assertGreaterEqual(rounds, 6)
        self.assertEqual(asked.count(2), rounds)  # a partly free court is checked every round
        self.assertLessEqual(asked.count(1), rounds // TAKEN_RECHECK_EVERY + 1)
        self.assertEqual(rush.diagnostics["skipped_taken"], rounds - asked.count(1))
        self.assertEqual(rush.reason, "scan_window_ended")  # a free cell was seen, the window was too short

    async def test_nothing_ever_free_ends_as_full(self):
        rush, _, _ = await self.run_rush({1: ("2", "2"), 2: ("0", "2")})
        self.assertEqual((rush.phase, rush.reason), ("exhausted", "target_full"))
        self.assertIn("没有出现过空位", rush.msg)

    async def test_not_released_ends_as_not_open_and_yields_its_turn(self):
        rush, asked, priorities = await self.run_rush({1: ("0", "0"), 2: ("0", "0")})
        self.assertEqual((rush.phase, rush.reason), ("exhausted", "target_not_open"))
        self.assertTrue(rush.msg.startswith("目标时段在查到的场地上一直是「不可约」"))
        # The first round runs at the shared scan priority, later ones behind the other sport.
        self.assertEqual(priorities[:2], [RUSH_SCAN_PRIORITY] * 2)
        self.assertTrue(all(p == RUSH_YIELD_PRIORITY for p in priorities[2:]))
        self.assertGreater(len(asked), 4)  # alone, it keeps scanning at full speed

    async def test_yield_ends_when_a_court_opens(self):
        answers = {1: ("0", "0"), 2: ("0", "0")}
        courts = [Court(n, f"g{n}", f"{n}号场") for n in answers]
        rush = RushEngine(self.engine(courts), clock_now=lambda: self.now, sleep=self.sleep)
        token = request_priority.set(RUSH_SCAN_PRIORITY)
        try:
            closed = [window("0", "0")]
            rush._note_court(1, closed, 1, 2)
            rush._note_court(2, closed, 1, 2)
            self.assertEqual(request_priority.get(), RUSH_YIELD_PRIORITY)
            rush._note_court(1, [window("2", "0")], 2, 2)
            self.assertEqual(request_priority.get(), RUSH_SCAN_PRIORITY)
        finally:
            request_priority.reset(token)


if __name__ == "__main__":
    unittest.main()
