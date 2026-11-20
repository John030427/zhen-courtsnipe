"""Timed start: checks the capture proxy and the login at the chosen time, then starts each Rush;
pending starts survive a restart. Offline: the engine is a stub, nothing reaches the school."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.rush_schedule import RushSchedule
from models.rush import CHINA

PLAN = {"sport_id": "badminton", "date": "2030-06-03", "slot_start": "20:00", "slot_end": "22:00",
        "release_at": "20:00:00", "court_priority": [9, 8], "confirmed_dry_run": False, "request_id": "badminton-a1"}


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "rush-schedule.json"
        self.now = datetime(2030, 6, 1, 18, 0, tzinfo=CHINA)
        self.mitm = {"port": 8080, "port_listening": False, "proc_alive": False, "port_conflict": False}
        self.engine = SimpleNamespace(
            mitm_capture_enabled=lambda: True, mitm_state=lambda: dict(self.mitm),
            restart_mitm=AsyncMock(return_value=True),
            auth_keeper=SimpleNamespace(check=AsyncMock(return_value=True)),
            rush=SimpleNamespace(start_from_config=AsyncMock(return_value={"ok": True})))

    def schedule(self):
        async def sleep(sec):
            self.now += timedelta(seconds=sec)
            await asyncio.sleep(0)
        return RushSchedule(self.engine, path=self.path, now=lambda: self.now, sleep=sleep)

    async def run_until_done(self, sched, item_id):
        for _ in range(2000):
            await asyncio.sleep(0)
            if sched.items[item_id]["status"] not in ("pending", "preparing"):
                return sched.items[item_id]
        self.fail("timed start never finished")

    async def test_at_the_time_it_restarts_the_proxy_checks_login_then_starts(self):
        sched = self.schedule()
        out = sched.add("19:55", [PLAN, {**PLAN, "sport_id": "tennis", "request_id": "tennis-b2"}])
        self.assertTrue(out["ok"])
        self.engine.rush.start_from_config.assert_not_awaited()     # nothing happens before the time
        item = await self.run_until_done(sched, out["item"]["id"])
        self.assertGreaterEqual(self.now, datetime(2030, 6, 1, 19, 55, tzinfo=CHINA))
        self.engine.restart_mitm.assert_awaited_once()
        self.engine.auth_keeper.check.assert_awaited_once()
        calls = self.engine.rush.start_from_config.await_args_list
        self.assertEqual([c.kwargs["sport_id"] for c in calls], ["badminton", "tennis"])
        self.assertEqual(calls[0].kwargs["settings"]["court_priority"], [9, 8])
        self.assertFalse(calls[0].kwargs["settings"]["confirmed_dry_run"])
        self.assertEqual(item["status"], "started")
        texts = [s["text"] for s in item["steps"]]
        self.assertIn("抓取代理端口 8080 没在监听，已重启", texts)
        self.assertIn("登录有效", texts)
        self.assertIn("羽毛球抢场已开始", texts)

    async def test_healthy_proxy_is_left_alone_and_a_refused_start_is_reported(self):
        self.mitm.update(port_listening=True, proc_alive=True)
        self.engine.rush.start_from_config = AsyncMock(return_value={"ok": False, "msg": "工作台现在是测试模式"})
        sched = self.schedule()
        item = await self.run_until_done(sched, sched.add("19:55", [PLAN])["item"]["id"])
        self.engine.restart_mitm.assert_not_awaited()
        self.assertEqual(item["status"], "failed")
        self.assertIn("羽毛球没有开始：工作台现在是测试模式", [s["text"] for s in item["steps"]])

    async def test_another_program_on_the_port_is_not_touched(self):
        self.mitm.update(port_listening=True, port_conflict=True)
        sched = self.schedule()
        item = await self.run_until_done(sched, sched.add("19:55", [PLAN])["item"]["id"])
        self.engine.restart_mitm.assert_not_awaited()
        self.assertEqual(item["status"], "started")

    async def test_bad_requests_are_refused(self):
        sched = self.schedule()
        self.assertIn("已经过了", sched.add("17:00", [PLAN])["msg"])
        self.assertIn("早于放号时间", sched.add("20:05", [PLAN])["msg"])
        self.assertIn("预约模式", sched.add("19:55", [{**PLAN, "confirmed_dry_run": None}])["msg"])
        self.assertIn("时间格式", sched.add("7点55", [PLAN])["msg"])
        self.assertTrue(sched.add("19:55", [PLAN])["ok"])
        self.assertIn("已经设了定时开始", sched.add("19:50", [PLAN])["msg"])
        await sched.close()

    async def test_cancel_and_restart_survival(self):
        sched = self.schedule()
        keep = sched.add("19:55", [PLAN])["item"]
        drop = sched.add("19:56", [{**PLAN, "sport_id": "tennis"}])["item"]
        self.assertTrue(sched.cancel(drop["id"]))
        await sched.close()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual({i["id"]: i["status"] for i in saved}, {keep["id"]: "pending", drop["id"]: "cancelled"})
        again = self.schedule()          # the workbench restarted before 19:55
        again.load()
        self.assertEqual(again.pending_sports(), {"badminton"})
        item = await self.run_until_done(again, keep["id"])
        self.assertEqual(item["status"], "started")

    async def test_a_start_missed_while_the_workbench_was_off_says_so(self):
        sched = self.schedule()
        keep = sched.add("19:55", [PLAN])["item"]
        await sched.close()
        self.now = datetime(2030, 6, 1, 20, 10, tzinfo=CHINA)
        later = self.schedule()
        later.load()
        self.assertEqual(later.items[keep["id"]]["status"], "missed")
        self.engine.rush.start_from_config.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
