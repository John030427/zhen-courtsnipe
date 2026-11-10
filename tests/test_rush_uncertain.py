"""An order with no usable reply (500, gateway page, timeout): the Rush asks the school who holds
the slot, then books on, stops, or reports a booking — instead of stopping blind.
Offline: queries and saveOrder are mocked."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import utils
from core.booking_service import BookingService
from core.order_guard import VidLease
from core.order_journal import OrderJournal, lock_key
from core.rush_engine import RushEngine
from core.safety import set_order_dry_run_for_tests
from models.rush import CHINA, RushPlan, RushWindow
from tests.test_booking_service import engine_stub
from utils import Court, slot_holder, slot_is_mine

ME = ("user-a", "cid", "n", "t")  # user id, customer id, name, phone of engine_stub
DATE = "2030-06-03"


def court_reply(court, state="free"):
    """What a query shows for 19:00–20:00: free, mine, other, or hidden (booked, no booker shown)."""
    mine = {"free": None, "mine": True, "other": False, "hidden": None}[state]
    status = "1" if state == "free" else "2"
    return dict(ok=True, auth_fail=False, groundId=court.id, startDate=DATE, endDate=DATE,
                avail={"19:00": status, "19:30": status},
                blocks=[dict(time="19:00", endTime="19:30", status=status, fee=0, mine=mine),
                        dict(time="19:30", endTime="20:00", status=status, fee=0, mine=mine)])


HTTP_500 = dict(success=False, sent=True, uncertain=True, http_status=500, msg="系统异常")
TIMEOUT = dict(success=False, sent=True, uncertain=True, msg="网络异常，请核对官方订单")
BOOKED = dict(success=True, sent=True, http_status=200)


class SlotOwnerTests(unittest.TestCase):
    def test_who_booked(self):
        self.assertTrue(slot_is_mine({"customerName": "n", "customerTel": "t"}, ME))
        self.assertTrue(slot_is_mine({"customerName": "n", "customerTel": "138****0000"}, ME))
        self.assertFalse(slot_is_mine({"customerName": "n", "customerTel": "other"}, ME))
        self.assertFalse(slot_is_mine({"customerName": "someone", "customerCode": "user-a"}, ME))
        self.assertTrue(slot_is_mine({"customerCode": "user-a"}, ME))
        self.assertTrue(slot_is_mine({"customerName": "张*", "customerCode": "cid"}, ME))
        # Nothing readable about the booker: not a "someone else".
        for blk in ({}, {"customerName": "张*"}, {"customerCode": "x-1"}, {"customerTel": "t"}):
            self.assertIsNone(slot_is_mine(blk, ME))
        self.assertIsNone(slot_is_mine({"customerName": "n"}, None))

    def test_who_holds_the_window(self):
        court = Court(1, "gid-1", "1号场")
        for state, verdict in (("free", "free"), ("mine", "mine"), ("other", "other"), ("hidden", "unknown")):
            self.assertEqual(slot_holder(court_reply(court, state), "19:00", "20:00"), verdict)
        half = court_reply(court, "mine")
        half["blocks"][1].update(status="1", mine=None)
        self.assertEqual(slot_holder(half, "19:00", "20:00"), "unknown")
        half["blocks"][1].update(status="2", mine=False)
        self.assertEqual(slot_holder(half, "19:00", "20:00"), "other")
        self.assertEqual(slot_holder({"ok": False}, "19:00", "20:00"), "unknown")
        self.assertEqual(slot_holder(court_reply(court), "19:00", "21:00"), "unknown")  # a slot is missing


class QueryOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_says_mine_without_keeping_the_booker(self):
        booker = {"customerName": "fixture-booker", "customerCode": "fixture-code", "customerTel": "13800000000"}
        reply = {"success": True, "data": {"configList": [{"date": DATE, "timeBlockList": [
            {"time": "19:00", "endTime": "19:30", "status": "2", **booker},
            {"time": "19:30", "endTime": "20:00", "status": "2", "customerName": "n", "customerTel": "t"},
            {"time": "20:00", "endTime": "20:30", "status": "1"}]}]}}

        async def wait():
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=reply))) as client:
            with patch("utils.get_client", return_value=client), patch("utils.wait_http_slot", wait), \
                    patch("core.venue_config_probe.log_order_time_config_from_server"):
                plain = await utils.get_order_once(Court(1, "gid-1", "1号场"), DATE, "tok", "user-a")
                asked = await utils.get_order_once(Court(1, "gid-1", "1号场"), DATE, "tok", "user-a", me=ME)
        self.assertNotIn("mine", plain["blocks"][0])
        self.assertEqual([b["mine"] for b in asked["blocks"]], [False, True, None])
        for value in booker.values():
            self.assertNotIn(value, str(asked))


class JournalGateTests(unittest.TestCase):
    def test_only_the_named_uncertain_record_is_let_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = OrderJournal(Path(tmp) / "orders.json")
            base = dict(account_id="user-a", date=DATE, start="19:00", end="20:00", time="fixture")
            journal.persist_record({**base, "ground_id": "gid-1", "court": 1, "state": "uncertain"})
            journal.persist_record({**base, "ground_id": "gid-2", "court": 2, "state": "success"})
            ask = dict(date=DATE, start="19:00", end="20:00", ground_id="gid-3")
            self.assertEqual(len(journal.find_blockers("user-a", **ask)), 2)
            let = frozenset({lock_key("gid-1", DATE, "19:00", "20:00"), lock_key("gid-2", DATE, "19:00", "20:00")})
            left = journal.find_blockers("user-a", **ask, ignore_uncertain=let)
            self.assertEqual([r["ground_id"] for r in left], ["gid-2"])  # a booking still blocks

            self.assertTrue(journal.settle_uncertain("user-a", "gid-1", DATE, "19:00", "20:00",
                                                     state="failed", verdict="other"))
            settled = next(r for r in journal.load() if r["ground_id"] == "gid-1")
            self.assertEqual((settled["state"], settled["settled_by"], settled["verdict"]),
                             ("failed", "requery", "other"))
            self.assertFalse(journal.settle_uncertain("user-a", "gid-2", DATE, "19:00", "20:00",
                                                      state="failed", verdict="other"))  # not uncertain


class RushUncertainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.e = engine_stub()
        self.service = BookingService(self.e)
        self.service._journal = OrderJournal(Path(tmp.name) / "orders.json")
        set_order_dry_run_for_tests(False)
        self.addCleanup(set_order_dry_run_for_tests, True)

    def journal(self):
        return {r["court"]: r["state"] for r in self.service._journal.load()}

    async def run_rush(self, replies, checks, *, extra=1, courts=(1, 2, 3)):
        """replies: saveOrder answers in the order sent. checks: per court, what each check query
        shows in turn (the last one repeats); a court not listed cannot be queried."""
        now = datetime(2030, 6, 1, 19, 59, 58, tzinfo=CHINA)

        async def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)

        e = self.e
        e.courts_for_sport = lambda _: [Court(i, f"gid-{i}", f"{i}号场") for i in courts]
        e.vid_running = False
        e.start_vid_pool, e.stop_vid_pool = AsyncMock(return_value={"ok": True}), AsyncMock()
        e.fetch_token = AsyncMock(return_value={"ok": True})
        e.take_vid = AsyncMock(side_effect=lambda **_: VidLease("fixture-vid", time.monotonic()))
        e.config_data["safety"]["max_orders_per_run"] = 3
        e.booking = self.service
        rush = RushEngine(e, clock_now=lambda: now, sleep=sleep)
        plan = RushPlan(date=DATE, release_at=now + timedelta(seconds=2), scan_window_sec=20,
                        windows=[RushWindow(start="19:00", end="20:00")], court_priority=list(courts),
                        uncertain_extra_orders=extra)
        steps, answers, seen = [], list(replies), {k: list(v) for k, v in checks.items()}

        async def scan(court, date, *_a, **_k):
            steps.append(("scan", court.no))
            return court_reply(court)

        async def check(court, date, *_a, me=None, **_k):
            steps.append(("check", court.no))
            self.assertEqual(me, ME)
            states = seen.get(court.no)
            if not states:
                return dict(ok=False, error_kind="network", error="fixture")
            return court_reply(court, states.pop(0) if len(states) > 1 else states[0])

        async def save(**kwargs):
            kwargs["_before_send"]()
            steps.append(("send", kwargs["court"].no))
            return dict(answers.pop(0)) if answers else dict(BOOKED)

        with patch("core.rush_engine.get_order_once", scan), \
                patch("core.booking_service.get_order_once", check), \
                patch("core.booking_service.save_order_once", save):
            self.assertTrue((await rush.start(plan))["ok"])
            await rush.task
        return rush, steps

    async def test_error_reply_and_court_still_free_orders_it_again(self):
        rush, steps = await self.run_rush([HTTP_500, BOOKED], {1: ["free"]})
        self.assertEqual(steps, [("scan", 1), ("send", 1), ("check", 1), ("send", 1)])
        self.assertEqual((rush.phase, rush.outcome()), ("success", "booked"))
        self.assertEqual(self.journal(), {1: "success"})
        self.assertEqual([a["uncertain"] for a in rush.applied], [False, False])

    async def test_only_one_more_order_on_the_same_court(self):
        rush, steps = await self.run_rush([HTTP_500, HTTP_500, BOOKED], {1: ["free"]})
        self.assertEqual(steps[:7], [("scan", 1), ("send", 1), ("check", 1), ("send", 1), ("check", 1),
                                     ("scan", 2), ("send", 2)])
        self.assertEqual(self.journal(), {1: "failed", 2: "success"})

    async def test_timeout_but_the_school_shows_it_as_mine(self):
        rush, steps = await self.run_rush([TIMEOUT], {1: ["mine"]})
        self.assertEqual(steps, [("scan", 1), ("send", 1), ("check", 1)])
        self.assertEqual((rush.phase, rush.outcome(), rush.reason), ("success", "booked", None))
        self.assertIn("1 号场", rush.msg)
        self.assertEqual(self.journal(), {1: "success"})
        self.assertEqual(rush.applied[0]["verified"], "mine")

    async def test_someone_else_got_it_moves_to_the_next_court(self):
        rush, steps = await self.run_rush([TIMEOUT, BOOKED], {1: ["other"]})
        self.assertEqual(steps, [("scan", 1), ("send", 1), ("check", 1), ("scan", 2), ("send", 2)])
        self.assertEqual(rush.outcome(), "booked")
        records = {r["court"]: r for r in self.service._journal.load()}
        self.assertEqual((records[1]["state"], records[1]["settled_by"]), ("failed", "requery"))
        self.assertEqual(records[2]["state"], "success")

    async def test_cannot_tell_twice_waits_then_stops_for_a_manual_check(self):
        rush, steps = await self.run_rush([TIMEOUT, TIMEOUT, BOOKED], {})
        # One more order after the first unknown; after the second, a late check of both, then stop.
        self.assertEqual(steps, [("scan", 1), ("send", 1), ("check", 1), ("scan", 2), ("send", 2), ("check", 2),
                                 ("check", 1), ("check", 2)])
        self.assertEqual((rush.phase, rush.reason, rush.outcome()), ("degraded", "result_uncertain", "uncertain"))
        self.assertIn("1、2 号场", rush.msg)
        self.assertEqual(self.journal(), {1: "uncertain", 2: "uncertain"})  # left for the person to check

    async def test_zero_extra_orders_stops_at_the_first_unknown(self):
        rush, steps = await self.run_rush([TIMEOUT, BOOKED], {}, extra=0)
        self.assertEqual(steps, [("scan", 1), ("send", 1), ("check", 1), ("check", 1)])
        self.assertEqual(rush.reason, "result_uncertain")

    async def test_unknown_orders_that_did_not_land_let_the_rush_go_on(self):
        # Timeouts: a court still free right away proves nothing, but still free 8 s later does.
        rush, steps = await self.run_rush([TIMEOUT, TIMEOUT, BOOKED], {1: ["free"], 2: ["free"]})
        self.assertEqual(steps, [("scan", 1), ("send", 1), ("check", 1), ("scan", 2), ("send", 2), ("check", 2),
                                 ("check", 1), ("check", 2), ("scan", 3), ("send", 3)])
        self.assertEqual((rush.phase, rush.outcome()), ("success", "booked"))
        self.assertEqual(self.journal(), {1: "failed", 2: "failed", 3: "success"})

    async def test_late_check_finds_the_first_order_landed_too(self):
        rush, steps = await self.run_rush([TIMEOUT, BOOKED], {1: ["free", "mine"]})
        self.assertEqual(steps, [("scan", 1), ("send", 1), ("check", 1), ("scan", 2), ("send", 2), ("check", 1)])
        self.assertEqual((rush.phase, rush.outcome()), ("success", "booked"))
        self.assertIn("不止一个场（1、2 号）", rush.msg)
        self.assertEqual(self.journal(), {1: "success", 2: "success"})

    async def test_replay_reads_in_order(self):
        rush, _ = await self.run_rush([TIMEOUT, BOOKED], {1: ["other"]})
        kinds = [(s["kind"], s["court"]) for s in rush.timeline]
        self.assertEqual(kinds, [("scan", 1), ("order", 1), ("verify", 1), ("scan", 2), ("order", 2), ("end", None)])
        self.assertEqual(rush.timeline[2]["text"], "核对：被别人约走了")
        self.assertTrue(all(isinstance(s["t"], float) for s in rush.timeline))
        self.assertEqual(rush.state_dict()["timeline"], rush.timeline)


if __name__ == "__main__":
    unittest.main()
