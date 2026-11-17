"""The first Rush query waits for the school's clock, and an order's 「系统繁忙」 does not pause
every request. Offline: the clock and replies are faked."""
import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import utils
from core.rush_engine import CLOCK_WAIT_MAX_SEC, RushEngine
from models.rush import CHINA, RushPlan, RushWindow
from tests.test_booking_service import engine_stub

RELEASE = datetime(2030, 6, 1, 20, 0, tzinfo=CHINA)


class FirstQueryTimeTests(unittest.TestCase):
    def at(self, measured):
        rush = RushEngine(engine_stub())
        plan = RushPlan(date="2030-06-03", release_at=RELEASE, windows=[RushWindow(start="20:00", end="22:00")])
        with patch("core.rush_engine.server_clock_offset", return_value=measured):
            moment, why = rush._first_query_time(plan)
        return round((moment - RELEASE).total_seconds(), 3), why

    def test_school_behind_waits_for_its_clock(self):
        # 11-16: school 0.789 s behind, ±0.107: send when the school reads about 20:00:00.1.
        self.assertEqual(self.at((-0.789, 0.107))[0], 0.996)
        self.assertIn("学校比本机慢 0.789 秒", self.at((-0.789, 0.107))[1])
        self.assertEqual(self.at((-0.105, 0.089))[0], 0.294)

    def test_school_ahead_goes_at_release(self):
        self.assertEqual(self.at((0.738, 0.097))[0], 0.0)

    def test_never_more_than_the_cap_or_unmeasured(self):
        self.assertEqual(self.at((-5.0, 0.5))[0], CLOCK_WAIT_MAX_SEC)
        self.assertEqual(self.at(None)[0], 0.1)
        self.assertIn("没测到", self.at(None)[1])


class BusyBackoffTests(unittest.IsolatedAsyncioTestCase):
    async def reply(self, url, msg):
        request = httpx.Request("POST", url)
        response = httpx.Response(200, json={"success": False, "code": 400, "msg": msg}, request=request)
        utils._next_request = 0.0
        utils._business_busy_count = 0
        with patch("core.audit.record"):
            await utils._check_response(response)
        return utils._next_request

    async def test_order_busy_does_not_pause_everything(self):
        self.assertEqual(await self.reply(utils.URL_SAVE, "系统繁忙，请稍后重试"), 0.0)

    async def test_query_busy_still_pauses(self):
        self.assertGreater(await self.reply(utils.URL_QUERY, "系统繁忙，请稍后重试"), 0.0)

    async def test_order_rate_limit_still_pauses(self):
        self.assertGreater(await self.reply(utils.URL_SAVE, "请求过于频繁"), 0.0)


if __name__ == "__main__":
    unittest.main()
