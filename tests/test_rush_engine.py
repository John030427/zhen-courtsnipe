import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.rush_engine import RushEngine
from core.safety import set_order_dry_run_for_tests

from models.auth import TokenState
from models.rush import CHINA, RushPlan, RushWindow
from utils import Court

CHINA_TZ = timezone(timedelta(hours=8))


class RushPlanTests(unittest.TestCase):
    def test_past_release_rejected(self):
        now = datetime(2030, 6, 1, 21, 0, tzinfo=CHINA_TZ)
        with self.assertRaises(ValueError):
            RushPlan.from_config(
                {"auto": {"refresh_at": "20:00:00", "slot_start": "19:00", "slot_end": "20:00"}},
                target_date="2030-06-01",
                now=now,
            )

    def test_release_absolute_iso(self):
        now = datetime(2030, 6, 1, 12, 0, tzinfo=CHINA_TZ)
        plan = RushPlan.from_config(
            {
                "rush": {
                    "release_at": "2030-06-01T20:00:00+08:00",
                    "slot_start": "19:00",
                    "slot_end": "20:00",
                },
            },
            target_date="2030-06-01",
            now=now,
        )
        self.assertEqual(plan.release_at.hour, 20)



    def test_time_only_uses_today_not_target_date(self):
        """Tonight 20:00 can book day-after-tomorrow courts."""
        now = datetime(2030, 6, 1, 12, 0, tzinfo=CHINA_TZ)
        plan = RushPlan.from_config(
            {"auto": {"refresh_at": "20:00:06", "slot_start": "16:00", "slot_end": "18:00"}},
            target_date="2030-06-03",
            now=now,
        )
        self.assertEqual(plan.date, "2030-06-03")
        self.assertEqual(plan.release_at.date().isoformat(), "2030-06-01")
        self.assertEqual(plan.release_at.hour, 20)
        self.assertEqual(plan.release_at.minute, 0)
        self.assertEqual(plan.release_at.second, 6)


class RushEngineClockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        set_order_dry_run_for_tests(True)
        self.t0 = datetime(2030, 6, 1, 19, 50, 0, tzinfo=CHINA_TZ)
        self.mono = 0.0

    async def asyncTearDown(self):
        set_order_dry_run_for_tests(True)

    async def test_default_preparation_runs_at_1958(self):
        self.e = self.engine_stub()
        prepared_at = []
        async def prepare(release):
            prepared_at.append(self.clock())
            return True
        self.e.auth_keeper = SimpleNamespace(prepare_rush=AsyncMock(side_effect=prepare), enabled=True)
        self.e._sync_from_token_manager = MagicMock()
        release = self.t0.replace(hour=20, minute=0)
        plan = RushPlan(date='2030-06-03', release_at=release,
                        windows=[RushWindow(start='19:00', end='20:00')], scan_window_sec=3)
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        with patch('core.rush_engine.get_order_once', AsyncMock(return_value={'ok': True})):
            await rush.start(plan)
            await rush.task
        self.assertEqual(prepared_at, [release - timedelta(seconds=120)])
        self.e.auth_keeper.prepare_rush.assert_awaited_once_with(release.isoformat())

    async def test_preparation_failure_stops_before_queries(self):
        self.e = self.engine_stub()
        self.e.auth_keeper = SimpleNamespace(prepare_rush=AsyncMock(return_value=False),
                                            enabled=True, message='new token unavailable')
        self.e._sync_from_token_manager = MagicMock()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        with patch('core.rush_engine.get_order_once', AsyncMock()) as query:
            await rush.start(self.short_plan())
            await rush.task
        self.assertEqual(rush.reason, 'auth_refresh_failed')
        query.assert_not_awaited()
        self.e.booking.place_order.assert_not_awaited()

    async def test_long_round_has_no_extra_interval_sleep(self):
        self.e = self.engine_stub()
        times = []
        async def query(*args, **kwargs):
            times.append(self.clock())
            await self.tick_sleep(4)
            return {'ok': True}
        plan = self.short_plan().model_copy(update={'scan_window_sec': 9})
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        with patch('core.rush_engine.get_order_once', AsyncMock(side_effect=query)):
            await rush.start(plan)
            await rush.task
        self.assertEqual(len(times), 3)
        self.assertEqual((times[1] - times[0]).total_seconds(), 4)

    async def test_short_round_retains_minimum_interval(self):
        self.e = self.engine_stub()
        times = []
        async def query(*args, **kwargs):
            times.append(self.clock())
            await self.tick_sleep(1)
            return {'ok': True}
        plan = self.short_plan().model_copy(update={'scan_window_sec': 7})
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        with patch('core.rush_engine.get_order_once', AsyncMock(side_effect=query)):
            await rush.start(plan)
            await rush.task
        self.assertEqual((times[1] - times[0]).total_seconds(), 3)

    def clock(self):
        return self.t0

    async def tick_sleep(self, sec: float):
        self.t0 += timedelta(seconds=sec)
        self.mono += sec

    def engine_stub(self):
        court = Court(1, "g1", "1号场")
        booking = SimpleNamespace(
            preflight_windows=lambda *_: {'ok': True, 'message': 'fixture clear', 'windows': []},
            new_run_id=lambda p: "rush-1",
            place_order=AsyncMock(
                return_value=SimpleNamespace(
                    success=False, dry_run=True, uncertain=False,
                    blocked=False, msg="dry", kind="dry_run", raw={},
                    diagnostics={},
                ),
            ),
            publish_result=AsyncMock(),
        )
        stub = SimpleNamespace(
            token="t",
            user_id="u",
            customer_id="c",
            customer_name="n",
            customer_tel="tel",
            config_data={},
            courts=[court],
            court_by_no={1: court},
            vid_running=False,
            tokens=SimpleNamespace(is_degraded_for_orders=lambda: False),
            fetch_token=AsyncMock(return_value={"ok": True}),
            start_vid_pool=AsyncMock(side_effect=self._mark_vid_async),
            stop_vid_pool=AsyncMock(side_effect=self._unmark_vid_async),
            take_vid=AsyncMock(return_value="vid"),
            booking=booking,
            bus=SimpleNamespace(publish=AsyncMock()),
            watcher=SimpleNamespace(state={"running": False}),
            _validation_sport_id=None,
        )
        stub.courts_for_sport = lambda _sid: list(stub.courts)
        return stub

    async def _mark_vid_async(self):
        self.e.vid_running = True

    async def _unmark_vid_async(self):
        self.e.vid_running = False

    async def test_dry_run_skips_vid_pool(self):
        self.e = self.engine_stub()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        plan = RushPlan(
            date="2030-06-01",
            release_at=datetime(2030, 6, 1, 20, 0, 0, tzinfo=CHINA_TZ),
            windows=[RushWindow(start="19:00", end="20:00")],
            token_refresh_lead_sec=2,
            vid_warmup_sec=3,
            scan_window_sec=12,
        )
        free = {
            "ok": True,
            "avail": {"19:00": "1", "19:30": "1"},
            "blocks": [
                {"time": "19:00", "endTime": "19:30", "status": "1"},
                {"time": "19:30", "endTime": "20:00", "status": "1"},
            ],
        }
        with patch("core.rush_engine.get_order_once", AsyncMock(return_value=free)):
            await rush.start(plan)
            for _ in range(200):
                await asyncio.sleep(0)
                if rush.phase in ("success", "exhausted", "degraded", "ordering"):
                    break
            await rush.stop()
        self.e.start_vid_pool.assert_not_called()
        self.e.take_vid.assert_not_called()

    async def test_scan_timeout_follows_answer_times_within_bounds(self):
        self.e = self.engine_stub()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        self.assertEqual(rush._scan_timeout(), 4.0)
        rush._scan_request_ms.extend([400, 380, 420])
        self.assertEqual(rush._scan_timeout(), 2.0)
        rush._scan_request_ms.extend([1250] * 8)
        self.assertAlmostEqual(rush._scan_timeout(), 3.75)
        rush._scan_request_ms.extend([2500] * 8)
        self.assertEqual(rush._scan_timeout(), 4.0)

    async def test_clock_offset_is_reported_not_applied(self):
        self.e = self.engine_stub()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        with patch("core.rush_engine.server_clock_offset", return_value=None):
            self.assertNotIn("clock_offset_ms", rush.state_dict()["diagnostics"])
        with patch("core.rush_engine.server_clock_offset", return_value=(0.8, 0.3)):
            self.assertEqual({k: rush.state_dict()["diagnostics"][k] for k in ("clock_offset_ms", "clock_uncertainty_ms")},
                             {"clock_offset_ms": 800, "clock_uncertainty_ms": 300})

    async def test_scan_passes_the_adaptive_timeout(self):
        self.e = self.engine_stub()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        plan = RushPlan(
            date="2030-06-01",
            release_at=datetime(2030, 6, 1, 20, 0, 0, tzinfo=CHINA_TZ),
            windows=[RushWindow(start="19:00", end="20:00")],
            token_refresh_lead_sec=2,
            vid_warmup_sec=3,
            scan_window_sec=12,
        )
        busy = {"ok": True, "avail": {"19:00": "0", "19:30": "0"}, "blocks": [],
                "timing": {"request_ms": 400}}
        with patch("core.rush_engine.get_order_once", AsyncMock(return_value=busy)) as query:
            await rush.start(plan)
            for _ in range(400):
                await asyncio.sleep(0)
                if rush.phase in ("success", "exhausted", "degraded"):
                    break
            await rush.stop()
        timeouts = [call.kwargs["timeout"] for call in query.await_args_list]
        self.assertGreater(len(timeouts), 3)
        self.assertEqual(timeouts[:3], [4.0, 4.0, 4.0])
        self.assertTrue(all(t == 2.0 for t in timeouts[3:]))
        self.assertEqual(rush.diagnostics["last_queries"][-1]["scan_timeout_ms"], 2000)

    async def test_warmup_starts_pool_only_before_release(self):
        set_order_dry_run_for_tests(False)
        self.e = self.engine_stub()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        plan = RushPlan(
            date="2030-06-01",
            release_at=datetime(2030, 6, 1, 20, 0, 0, tzinfo=CHINA_TZ),
            windows=[RushWindow(start="19:00", end="20:00")],
            token_refresh_lead_sec=5,
            vid_warmup_sec=3,
            scan_window_sec=12,
        )
        free = {
            "ok": True,
            "avail": {"19:00": "1", "19:30": "1"},
            "blocks": [
                {"time": "19:00", "endTime": "19:30", "status": "1"},
                {"time": "19:30", "endTime": "20:00", "status": "1"},
            ],
        }
        with patch("core.rush_engine.get_order_once", AsyncMock(return_value=free)):
            await rush.start(plan)
            for _ in range(200):
                await asyncio.sleep(0)
                if rush.phase in ("success", "exhausted", "degraded"):
                    break
            await rush.stop()
        self.e.start_vid_pool.assert_awaited()
        self.e.stop_vid_pool.assert_awaited()
        self.assertFalse(self.e.booking.place_order.await_count > 0 and self.e.vid_running)

    async def test_start_rejects_past_release(self):
        self.e = self.engine_stub()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        plan = RushPlan(
            date="2030-06-01",
            release_at=datetime(2030, 6, 1, 19, 0, 0, tzinfo=CHINA_TZ),
            windows=[RushWindow(start="18:00", end="19:00")],
        )
        r = await rush.start(plan)
        self.assertFalse(r["ok"])

    async def test_longer_scan_catches_late_availability_without_shortening_target(self):
        async def simulate(scan_window):
            self.t0 = datetime(2030, 6, 1, 19, 50, tzinfo=CHINA_TZ)
            self.e = self.engine_stub()
            rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
            release = self.t0 + timedelta(seconds=2)
            plan = RushPlan(date='2030-06-03', release_at=release,
                windows=[RushWindow(start='16:00', end='18:00')],
                token_refresh_lead_sec=0, scan_window_sec=scan_window)
            async def query(*args, **_):
                available = self.t0 >= release + timedelta(seconds=20)
                avail = {t: '1' if available or t != '17:30' else '0'
                         for t in ('16:00','16:30','17:00','17:30')}
                blocks = [{'time': t, 'endTime': end, 'status': avail[t]}
                          for t,end in zip(avail, ('16:30','17:00','17:30','18:00'))]
                return {'ok': True, 'avail': avail, 'blocks': blocks}
            with patch('core.rush_engine.get_order_once', query), patch('core.rush_engine.record') as audit:
                await rush.start(plan)
                await rush.task
            return rush, self.e.booking.place_order, audit

        short, order, _ = await simulate(15)
        order.assert_not_called()
        self.assertEqual(short.phase, 'exhausted')
        long, order, audit = await simulate(60)
        order.assert_awaited_once()
        target = order.await_args.args[0]
        self.assertEqual((target.start, target.end), ('16:00', '18:00'))
        self.assertGreater(long.diagnostics['rounds'], 1)
        self.assertEqual(long.diagnostics['matches'], 1)
        rows = [c.kwargs for c in audit.call_args_list if c.args[0] == 'rush.query_result']
        self.assertEqual(rows[0]['target_windows'][0]['reason'], 'unavailable_slots')
        self.assertEqual(rows[-1]['target_windows'][0]['reason'], 'match')
        self.assertEqual(len(rows[-1]['target_windows'][0]['slots']), 4)

    async def test_success_then_continued_scan_is_not_reported_as_no_booking(self):
        self.e = self.engine_stub()
        self.e.booking.place_order.return_value.success = True
        self.e.booking.place_order.return_value.dry_run = False
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        plan = RushPlan(date='2030-06-03', release_at=self.t0+timedelta(seconds=2),
            windows=[RushWindow(start='16:00', end='17:00'), RushWindow(start='18:00', end='18:30')],
            token_refresh_lead_sec=0, scan_window_sec=6, stop_after_success=False)
        free = {'ok': True, 'avail': {'16:00':'1','16:30':'1','18:00':'1'},
                'blocks': [{'time':'16:00','endTime':'16:30','status':'1'},
                           {'time':'16:30','endTime':'17:00','status':'1'}]}
        unavailable = {'ok': True, 'avail': {}, 'blocks': []}
        replies = [free]
        async def query(*args, **_):
            return replies.pop() if replies else unavailable
        with patch('core.rush_engine.get_order_once', query):
            await rush.start(plan)
            await rush.task
        self.e.booking.place_order.assert_awaited_once()
        self.assertEqual(rush.phase, 'success')
        self.assertNotIn('未预约', rush.msg)

    async def test_auth_failure_records_school_reply_and_stops_ordering(self):
        self.e = self.engine_stub()
        self.e.tokens.mark_expired = MagicMock()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        plan = RushPlan(date='2030-06-03', release_at=self.t0 + timedelta(seconds=2),
            windows=[RushWindow(start='19:00', end='20:00')], token_refresh_lead_sec=1,
            vid_warmup_sec=0, scan_window_sec=3)
        result = {'ok':False, 'auth_fail':True, 'error_kind':'auth',
                  'school_msg':'token expired', 'error':'token expired', 'code':'401', 'http_status':200}
        with patch('core.rush_engine.get_order_once', AsyncMock(return_value=result)), \
                patch('core.rush_engine.record') as audit:
            await rush.start(plan)
            await rush.task
        self.assertEqual(rush.phase, 'degraded')
        self.e.tokens.mark_expired.assert_called_once()
        self.e.booking.place_order.assert_not_called()
        entries = [c for c in audit.call_args_list if c.args[0]=='rush.query_failure']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kwargs['school_msg'], 'token expired')
        self.assertEqual(entries[0].kwargs['code'], '401')
        self.assertTrue(entries[0].kwargs['auth_fail'])

    async def test_token_refresh_query_error_does_not_degrade(self):
        self.e = self.engine_stub()
        self.e.config_data = {"connection": {"mode": "page"}}
        validation = {
            "ok": False,
            "auth_fail": False,
            "error_kind": "query",
            "school_msg": "系统异常，请重新加载",
            "code": "400",
            "message": "学校查询失败：系统异常",
        }
        self.e.tokens = SimpleNamespace(
            validate_current=AsyncMock(return_value=False),
            is_degraded_for_orders=lambda: False,
            state=TokenState.VALID,
            public_view=lambda: {"validation": validation, "message": validation["message"]},
            mark_expired=MagicMock(),
        )
        self.e._sync_from_token_manager = MagicMock()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        plan = RushPlan(
            date="2030-06-03",
            release_at=self.t0 + timedelta(seconds=4),
            windows=[RushWindow(start="19:00", end="20:00")],
            token_refresh_lead_sec=1,
            vid_warmup_sec=0,
            scan_window_sec=3,
        )
        with patch("core.rush_engine.get_order_once", AsyncMock(return_value={"ok": False, "msg": "empty"})):
            await rush.start(plan)
            await rush.task
        self.assertNotEqual(rush.phase, "degraded")
        self.e.tokens.mark_expired.assert_not_called()

    async def test_token_refresh_auth_fail_degrades(self):
        self.e = self.engine_stub()
        self.e.config_data = {"connection": {"mode": "page"}}
        validation = {
            "ok": False,
            "auth_fail": True,
            "error_kind": "auth",
            "school_msg": "鉴权失败2",
            "code": "401",
            "message": "学校鉴权失败：鉴权失败2",
        }
        self.e.tokens = SimpleNamespace(
            validate_current=AsyncMock(return_value=False),
            is_degraded_for_orders=lambda: True,
            state=TokenState.EXPIRED,
            public_view=lambda: {"validation": validation, "message": validation["message"]},
            mark_expired=MagicMock(),
        )
        self.e._sync_from_token_manager = MagicMock()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        plan = RushPlan(
            date="2030-06-03",
            release_at=self.t0 + timedelta(seconds=2),
            windows=[RushWindow(start="19:00", end="20:00")],
            token_refresh_lead_sec=1,
            vid_warmup_sec=0,
            scan_window_sec=3,
        )
        with patch("core.rush_engine.get_order_once", AsyncMock(return_value={"ok": False, "msg": "empty"})):
            await rush.start(plan)
            await rush.task
        self.assertEqual(rush.phase, "degraded")
        self.assertEqual(rush.reason, "auth_expired")

    def free_reply(self):
        return {'ok': True, 'avail': {'19:00': '1', '19:30': '1'},
                'blocks': [{'time': '19:00', 'endTime': '19:30', 'status': '1'},
                           {'time': '19:30', 'endTime': '20:00', 'status': '1'}]}

    def short_plan(self):
        return RushPlan(date='2030-06-03', release_at=self.t0 + timedelta(seconds=2),
            windows=[RushWindow(start='19:00', end='20:00')], token_refresh_lead_sec=0,
            vid_warmup_sec=0, scan_window_sec=3)

    async def test_dry_run_success_reports_simulated_outcome_not_booking(self):
        self.e = self.engine_stub()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        with patch('core.rush_engine.get_order_once', AsyncMock(return_value=self.free_reply())):
            await rush.start(self.short_plan())
            await rush.task
        state = rush.state_dict()
        self.assertEqual(state['phase'], 'success')
        self.assertEqual(state['outcome'], 'simulated')
        self.assertIsNone(state['reason'])
        stopped = [c for c in self.e.bus.publish.await_args_list if c.args[0] == 'rush.stopped']
        self.assertEqual(stopped[-1].args[1]['outcome'], 'simulated')

    async def test_real_success_reports_booked_outcome(self):
        set_order_dry_run_for_tests(False)
        self.e = self.engine_stub()
        self.e.booking.place_order.return_value.success = True
        self.e.booking.place_order.return_value.dry_run = False
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        with patch('core.rush_engine.get_order_once', AsyncMock(return_value=self.free_reply())):
            await rush.start(self.short_plan())
            await rush.task
        self.assertEqual(rush.state_dict()['outcome'], 'booked')

    async def test_uncertain_order_reports_uncertain_outcome_and_reason(self):
        self.e = self.engine_stub()
        self.e.booking.place_order.return_value.dry_run = False
        self.e.booking.place_order.return_value.uncertain = True
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        with patch('core.rush_engine.get_order_once', AsyncMock(return_value=self.free_reply())):
            await rush.start(self.short_plan())
            await rush.task
        state = rush.state_dict()
        self.assertEqual((state['phase'], state['reason'], state['outcome']),
                         ('degraded', 'result_uncertain', 'uncertain'))

    async def test_no_free_court_ends_with_scan_window_reason(self):
        self.e = self.engine_stub()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        busy = {'ok': True, 'avail': {'19:00': '0', '19:30': '0'},
                'blocks': [{'time': '19:00', 'endTime': '19:30', 'status': '0'},
                           {'time': '19:30', 'endTime': '20:00', 'status': '0'}]}
        with patch('core.rush_engine.get_order_once', AsyncMock(return_value=busy)):
            await rush.start(self.short_plan())
            await rush.task
        state = rush.state_dict()
        self.assertEqual((state['phase'], state['reason'], state['outcome']),
                         ('exhausted', 'scan_window_ended', 'none'))
        self.e.booking.place_order.assert_not_called()

    async def test_scan_auth_failure_reason_and_manual_stop_reason(self):
        self.e = self.engine_stub()
        self.e.tokens.mark_expired = MagicMock()
        rush = RushEngine(self.e, clock_now=self.clock, sleep=self.tick_sleep)
        expired = {'ok': False, 'auth_fail': True, 'error_kind': 'auth', 'error': 'expired'}
        with patch('core.rush_engine.get_order_once', AsyncMock(return_value=expired)):
            await rush.start(self.short_plan())
            await rush.task
        self.assertEqual((rush.reason, rush.outcome()), ('auth_expired', 'none'))

        # Real sleeps with a frozen clock keep the run waiting until stop() cancels it.
        stopped = RushEngine(self.e, clock_now=self.clock, sleep=lambda _sec: asyncio.sleep(0.01))
        plan = RushPlan(date='2030-06-03', release_at=self.t0 + timedelta(hours=1),
            windows=[RushWindow(start='19:00', end='20:00')])
        with patch('core.rush_engine.get_order_once', AsyncMock(return_value=self.free_reply())):
            await stopped.start(plan)
            self.assertIsNone(stopped.reason)
            await stopped.stop()
        self.assertEqual((stopped.phase, stopped.reason, stopped.outcome()), ('stopped', 'stopped', 'none'))

    def test_unknown_stop_reason_is_rejected(self):
        rush = RushEngine(SimpleNamespace(token='', user_id=''))
        with self.assertRaises(ValueError):
            rush._set_phase('degraded', 'x', reason='typo')

class RushStartStagedMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        set_order_dry_run_for_tests(True)

    async def asyncTearDown(self):
        set_order_dry_run_for_tests(True)

    async def test_start_message_is_staged_not_planned_only(self):
        now0 = datetime(2026, 10, 11, 23, 44, 36, tzinfo=CHINA_TZ)
        release = datetime(2026, 10, 11, 23, 45, 6, tzinfo=CHINA_TZ)
        clock = {"t": now0}

        def now():
            return clock["t"]

        async def sleep(sec):
            clock["t"] = clock["t"] + timedelta(seconds=float(sec))
            await asyncio.sleep(0)

        court = Court(1, "g1", "1")
        booking = SimpleNamespace(
            new_run_id=lambda p: "rush-msg",
            preflight_windows=lambda *_: {'ok': True, 'message': 'fixture clear', 'windows': []},
            place_order=AsyncMock(
                return_value=SimpleNamespace(
                    success=False, dry_run=True, uncertain=False,
                    blocked=False, msg="dry", kind="dry_run", raw={},
                ),
            ),
            publish_result=AsyncMock(),
        )
        tokens = SimpleNamespace(
            validate_current=AsyncMock(return_value=True),
            is_degraded_for_orders=lambda: False,
        )
        engine = SimpleNamespace(
            token="t",
            user_id="u",
            customer_id="c",
            customer_name="n",
            customer_tel="tel",
            config_data={"connection": {"mode": "page"}},
            courts=[court],
            court_by_no={1: court},
            vid_running=False,
            tokens=tokens,
            fetch_token=AsyncMock(return_value={"ok": True}),
            start_vid_pool=AsyncMock(),
            stop_vid_pool=AsyncMock(),
            take_vid=AsyncMock(return_value="vid"),
            booking=booking,
            bus=SimpleNamespace(publish=AsyncMock()),
            watcher=SimpleNamespace(state={"running": False}),
            _sync_from_token_manager=MagicMock(),
            _validation_sport_id=None,
        )
        engine.courts_for_sport = lambda _sid: list(engine.courts)
        rush = RushEngine(engine, clock_now=now, sleep=sleep)
        plan = RushPlan(
            date="2026-10-13",
            release_at=release,
            windows=[RushWindow(start="08:00", end="09:00")],
            token_refresh_lead_sec=120,
            vid_warmup_sec=0,
            scan_window_sec=3,
            scan_interval_near_sec=1,
            scan_interval_far_sec=15,
            near_release_sec=5,
        )
        with patch("core.rush_engine.get_order_once", new=AsyncMock(return_value={"ok": False, "msg": "empty"})):
            try:
                out = await rush.start(plan)
                self.assertTrue(out.get("ok"), out)
                self.assertNotIn("已计划", rush.msg or "")
                self.assertTrue(
                    ("放号" in (rush.msg or ""))
                    or ("复验" in (rush.msg or ""))
                    or ("Token" in (rush.msg or "")),
                    rush.msg,
                )
                # Advance clock past release; allow runner to scan/exhaust
                clock["t"] = release + timedelta(milliseconds=50)
                for _ in range(100):
                    if not rush.running:
                        break
                    await sleep(0.05)
                self.assertNotIn("已计划", rush.msg or "")
                self.assertIn(rush.phase, ("exhausted", "success", "degraded", "scanning", "stopped"))
            finally:
                if rush.task and not rush.task.done():
                    rush.task.cancel()
                    try:
                        await rush.task
                    except BaseException:
                        pass



if __name__ == "__main__":
    unittest.main()
