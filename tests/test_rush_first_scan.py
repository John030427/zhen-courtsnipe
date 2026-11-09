import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.request_priority import request_priority, RUSH_SCAN_PRIORITY
from core.rush_engine import RushEngine
from core.rush_registry import SportRushRegistry
from core.safety import set_order_dry_run_for_tests
from models.rush import CHINA, RushPlan, RushWindow
from tests.test_booking_service import engine_stub
from utils import Court


class FirstScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_first_scan_gets_preference_and_context_is_restored(self):
        set_order_dry_run_for_tests(True)
        self.addCleanup(set_order_dry_run_for_tests, True)
        now = datetime(2030, 6, 1, 19, 59, 58, tzinfo=CHINA)
        async def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)
        e = engine_stub()
        e.courts_for_sport = lambda _: [Court(i, f"fixture-{i}", f"Court {i}") for i in (7, 6)]
        e.fetch_token = AsyncMock(return_value={"ok": True})
        e.vid_running = False
        e.booking = SimpleNamespace(
            preflight_windows=lambda *_: {"ok": True, "message": "fixture clear", "windows": []},
            new_run_id=lambda *_: "fixture-rush", place_order=AsyncMock())
        plan = RushPlan(date="2030-06-03", sport_id="tennis", release_at=now + timedelta(seconds=1),
                        windows=[RushWindow(start="20:00", end="22:00")],
                        scan_window_sec=3, prefer_first_scan=True)
        rush = RushEngine(e, clock_now=lambda: now, sleep=sleep)
        seen = []
        async def query(*args, **kwargs):
            seen.append(request_priority.get())
            return {"ok": True, "blocks": [], "avail": {}}
        original = request_priority.get()
        with patch("core.rush_engine.get_order_once", query):
            self.assertTrue((await rush.start(plan))["ok"])
            await rush.task
        self.assertTrue(rush.plan["prefer_first_scan"])
        self.assertEqual(seen[0], 1)
        self.assertGreaterEqual(len(seen), 2)
        self.assertTrue(all(p == RUSH_SCAN_PRIORITY for p in seen[1:]))
        self.assertEqual(request_priority.get(), original)
        e.booking.place_order.assert_not_awaited()

    async def test_start_option_is_not_written_to_configuration(self):
        e = engine_stub(target_date="2030-06-03")
        registry = SportRushRegistry(e)
        registry.start = AsyncMock(return_value={"ok": True})
        with patch("core.rush_registry.datetime") as clock:
            clock.now.return_value = datetime(2030, 6, 1, 19, 0, tzinfo=CHINA)
            result = await registry.start_from_config("tennis", {"prefer_first_scan": True})
        self.assertTrue(result["ok"])
        self.assertTrue(registry.start.await_args.args[0].prefer_first_scan)
        self.assertNotIn("rush", e.config_data)
