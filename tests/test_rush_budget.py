"""Rush scan budget: booked courts are looked at less often, a Rush whose targets are not released
steps behind the other sport's scans, and an empty ending says which of the two it was.
Offline: queries are mocked, orders run in test mode."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.request_priority import RUSH_SCAN_PRIORITY, RUSH_YIELD_PRIORITY, request_priority
from core.rush_engine import TAKEN_RECHECK_EVERY, TAKEN_TRUST_AFTER_SEC, RushEngine, court_view
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
        """answers: court no -> status pair, or a function of the round. Each query takes one
        second, as the pacing does."""
        courts = [Court(n, f"g{n}", f"{n}号场") for n in sorted(answers)]
        e = self.engine(courts)
        rush = RushEngine(e, clock_now=lambda: self.now, sleep=self.sleep)
        asked, priorities = [], []
        self.rounds_asked = {n: [] for n in answers}
        current = {"round": 0}

        async def published(topic, payload, **_):
            if topic == "rush.scan":  # sent right before each court is queried
                current["round"] = payload["round"]
                self.rounds_asked[payload["court"]].append(payload["round"])

        e.bus.publish = AsyncMock(side_effect=published)

        async def query(court, *_a, **_k):
            asked.append(court.no)
            priorities.append(request_priority.get())
            round_no = current["round"]
            self.now += timedelta(seconds=1)
            answer = answers[court.no]
            return reply(answer(round_no) if callable(answer) else answer)

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
        self.assertGreaterEqual(rounds, 8)
        self.assertEqual(asked.count(2), rounds)  # a partly free court is checked every round
        seen = self.rounds_asked[1]
        # Every round until the answers are trusted (5 s after release), then every third round.
        trusted = next(r for r in seen if r >= 3)
        self.assertEqual(seen, list(range(1, trusted + 1)) + list(range(trusted + TAKEN_RECHECK_EVERY, rounds + 1,
                                                                          TAKEN_RECHECK_EVERY)))
        self.assertEqual(rush.diagnostics["skipped_taken"], rounds - len(seen))
        self.assertEqual(rush.reason, "scan_window_ended")  # a free cell was seen, the window was too short

    async def test_a_booked_court_that_opens_again_is_back_every_round(self):
        rush, _, _ = await self.run_rush({1: lambda r: ("2", "2") if r < 6 else ("2", "0"), 2: ("1", "2")})
        seen = self.rounds_asked[1]
        after = [r for r in seen if r > 6]
        self.assertTrue(after)  # rechecked, saw a change, and is checked every round again
        self.assertEqual(after, list(range(after[0], rush.diagnostics["rounds"] + 1)))

    async def test_booked_right_at_release_is_not_trusted(self):
        rush = RushEngine(self.engine([Court(1, "g1", "1号场")]), clock_now=lambda: self.now, sleep=self.sleep)
        rush._note_court(1, [window("2", "2")], 1, 1, settled=False)
        self.assertEqual(rush._court_views[1][0], "mixed")
        rush._note_court(1, [window("2", "2")], 2, 1, settled=True)
        self.assertEqual(rush._court_views[1][0], "taken")
        self.assertEqual(TAKEN_TRUST_AFTER_SEC, 5)

    async def test_missing_slots_do_not_read_as_full(self):
        rush = RushEngine(self.engine([Court(1, "g1", "1号场")]), clock_now=lambda: self.now, sleep=self.sleep)
        rush._note_court(1, [window("2", "missing")], 1, 1)
        self.assertEqual(rush._no_booking_reason()[0], "scan_window_ended")
        rush._seen_status = {"0", "2"}
        self.assertEqual(rush._no_booking_reason()[0], "target_full")

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
        closed = [window("0", "0")]
        rush._note_court(1, closed, 1, 2)
        self.assertFalse(rush._yielding)  # not every court seen yet
        rush._note_court(2, closed, 1, 2)
        self.assertTrue(rush._yielding)
        rush._note_court(1, [window("2", "0")], 2, 2)
        self.assertFalse(rush._yielding)


if __name__ == "__main__":
    unittest.main()
