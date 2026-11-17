"""Read-only overview projection: no school requests or booking actions."""
import ast
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from core.rush_engine import RushEngine
from models.rush import CHINA
from tests.test_booking_service import engine_stub


class OverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load the pure projection without creating engine.py's service singleton.
        tree = ast.parse((Path(__file__).parents[1] / "engine.py").read_text(encoding="utf-8"))
        engine = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Engine")
        method = next(n for n in engine.body if isinstance(n, ast.FunctionDef) and n.name == "_rush_brief")
        method.decorator_list = []
        scope = {"datetime": datetime, "Any": Any}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "engine.py", "exec"), scope)
        cls.brief = staticmethod(scope["_rush_brief"])

    def slot(self):
        slot = RushEngine(engine_stub(), clock_now=lambda: datetime(2030, 6, 1, 19, 59, tzinfo=CHINA))
        slot.plan = {"date": "2030-06-03", "release_at": "2030-06-01T20:00:00+08:00",
                     "windows": [{"start": "20:00", "end": "22:00"}],
                     "court_priority": [7, 6, 5], "court_count": 7}
        slot.run_id = "fixture-rush"
        return slot

    def test_waiting_plan_countdown_and_read_only(self):
        slot = self.slot()
        slot.running, slot.phase = True, "waiting"
        view = self.brief(slot)
        self.assertEqual(view["release_in_sec"], 60)
        self.assertEqual(view["windows"], [["20:00", "22:00"]])
        self.assertEqual(view["courts"], 7)
        self.assertEqual(view["first_courts"], [7, 6, 5])
        self.assertTrue(slot.running)
        self.assertEqual(set(view) & {"token", "userid", "identity", "token_preview"}, set())

    def test_unknown_result_is_not_reported_as_success(self):
        slot = self.slot()
        slot.phase, slot.reason = "degraded", "result_uncertain"
        slot.applied = [{"court": 7, "ok": False, "uncertain": True, "msg": "non-json"}]
        slot._step("order", "订单结果未确认，停止下单", 7)
        slot._mark_finished()
        view = self.brief(slot)
        self.assertEqual(view["outcome"], "uncertain")
        self.assertEqual(view["open_courts"], [7])
        self.assertEqual(view["booked_courts"], [])
        self.assertEqual(view["finished_on"], "2030-06-01")
        self.assertEqual(len(view["timeline"]), 1)

    def test_replay_is_bounded_and_resets_between_runs(self):
        slot = self.slot()
        for _ in range(120):
            slot._step("scan", "查询失败", 7)
        self.assertLessEqual(len(slot.timeline), 80)
        slot._reset_diagnostics()
        self.assertEqual(slot.timeline, [])
