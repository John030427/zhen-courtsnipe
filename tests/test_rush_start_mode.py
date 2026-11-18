"""A Rush start carries the mode the person confirmed; the workbench refuses a start in the
other mode, and the task keeps the page's id for that start. Offline, no task is run."""
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from core.rush_registry import SportRushRegistry
from core.safety import set_order_dry_run_for_tests
from models.rush import CHINA
from tests.test_booking_service import engine_stub


class StartModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = SportRushRegistry(engine_stub(target_date="2030-06-03"))
        self.registry.start = AsyncMock(side_effect=lambda plan: {"ok": True, "plan": plan})
        self.settings = {"date": "2030-06-03", "slot_start": "20:00", "slot_end": "22:00", "court_priority": [],
                         "request_id": "badminton-x1"}
        self.addCleanup(set_order_dry_run_for_tests, True)

    async def start(self, **extra):
        with patch("core.rush_registry.datetime") as clock:
            clock.now.return_value = datetime(2030, 6, 1, 19, 0, tzinfo=CHINA)
            return await self.registry.start_from_config("badminton", {**self.settings, **extra})

    async def test_real_mode_confirmed_as_test_is_refused(self):
        set_order_dry_run_for_tests(False)
        out = await self.start(confirmed_dry_run=True)
        self.assertFalse(out["ok"])
        self.assertTrue(out["mode_changed"])
        self.assertIn("真实预约", out["msg"])
        self.registry.start.assert_not_awaited()

    async def test_test_mode_confirmed_as_real_is_refused(self):
        set_order_dry_run_for_tests(True)
        out = await self.start(confirmed_dry_run=False)
        self.assertFalse(out["ok"])
        self.assertIn("测试模式", out["msg"])

    async def test_start_without_a_confirmed_mode_is_refused(self):
        set_order_dry_run_for_tests(False)
        for value in (None, "false", 0):
            out = await self.start(confirmed_dry_run=value)
            self.assertFalse(out["ok"])
        self.registry.start.assert_not_awaited()

    async def test_matching_mode_starts_and_keeps_the_request_id(self):
        set_order_dry_run_for_tests(False)
        out = await self.start(confirmed_dry_run=False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["plan"].request_id, "badminton-x1")


if __name__ == "__main__":
    unittest.main()
