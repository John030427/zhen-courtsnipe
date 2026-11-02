import unittest
from unittest.mock import AsyncMock, patch

import httpx
import utils
from core.safety import set_order_dry_run_for_tests


class OrderDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        set_order_dry_run_for_tests(True)

    async def reply(self, status, payload):
        set_order_dry_run_for_tests(False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload)),
            event_hooks={"request": [utils._pace_request]}) as client:
            with patch("utils.get_client", return_value=client), \
                 patch("utils.wait_http_slot", AsyncMock()), \
                 patch("core.audit.record") as audit:
                out = await utils.save_order_once(
                    vid="secret-vid-value", court=utils.Court(1, "ground", "court"),
                    date="2030-06-01", start="19:00", end="20:00", token="secret-token-value",
                    user_id="fixture-user", customer_id="fixture-customer", customer_name="fixture-name",
                    customer_tel="13812345678", gym_id="fixture-gym", gym_name="gym",
                    _booking_internal=True, _before_send=lambda: None,
                    _audit_context={"run_id": "fixture-run", "sport_id": "badminton"},
                )
        return out, audit.call_args_list

    async def test_http_500_even_success_flag_is_uncertain(self):
        out, _ = await self.reply(500, {"success": True, "msg": "system error"})
        self.assertTrue(out["uncertain"])
        self.assertFalse(out["success"])

    async def test_explicit_duration_rejection_is_terminal_not_uncertain(self):
        msg = "无法预约，当天该场地类型预约总时长超过上限，(剩余可预约时长120分钟),请调整预约时长"
        out, _ = await self.reply(500, {"success": False, "msg": msg, "code": 500})
        self.assertFalse(out["uncertain"])
        self.assertFalse(out["success"])
        self.assertTrue(out["terminal_rejection"])
        self.assertEqual(out["error_kind"], "business")

    async def test_duration_words_do_not_override_conflicting_or_unknown_response(self):
        msg = "无法预约，当天该场地类型预约总时长超过上限，(剩余可预约时长120分钟),请调整预约时长"
        for payload in ({"success": True, "msg": msg}, {"msg": msg},
                        {"success": False, "msg": msg, "uncertain": True},
                        {"success": False, "msg": "系统异常，预约时长超过上限"}):
            with self.subTest(payload=payload):
                out, _ = await self.reply(500, payload)
                self.assertTrue(out["uncertain"])
                self.assertFalse(out["terminal_rejection"])

    async def test_generic_business_system_error_is_not_retried_as_occupied(self):
        out, _ = await self.reply(200, {"success": False, "msg": "系统异常."})
        self.assertTrue(out["uncertain"])
        self.assertNotEqual(utils.classify_order_reply(out)[0], "busy")

    async def test_auth_failure_is_explicit(self):
        out, _ = await self.reply(401, {"msg": "login expired"})
        self.assertTrue(out["auth_fail"])
        self.assertFalse(out["uncertain"])

    async def test_redacted_messages_and_context(self):
        out, calls = await self.reply(200, {"success": False,
            "msg": "secret-token-value secret-vid-value fixture-name 13812345678",
            "data": {"token": "secret-token-value"}})
        for secret in ("secret-token-value", "secret-vid-value", "fixture-name", "13812345678"):
            self.assertNotIn(secret, str(out))
        self.assertNotIn("data", out)
        result = next(c for c in calls if c.args[0] == "order.result")
        self.assertEqual(result.kwargs["gymId"], "fixture-gym")
        self.assertEqual(result.kwargs["run_id"], "fixture-run")
        self.assertEqual(result.kwargs["http_status"], 200)
        self.assertTrue(result.kwargs["sent"])
