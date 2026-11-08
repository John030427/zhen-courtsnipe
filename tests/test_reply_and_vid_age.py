"""What a 5xx / non-JSON order reply looked like, the official page's captcha-to-order interval,
and the per-sport Rush summary the overview card reads. Offline, nothing is sent."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

import mitm_addon
from engine import Engine
from utils import describe_reply

CHINA_TZ = timezone(timedelta(hours=8))


class DescribeReplyTests(unittest.TestCase):
    def test_gateway_page(self):
        r = httpx.Response(502, headers={"server": "nginx/1.20", "content-type": "text/html; charset=utf-8"},
                           text="<html><head><title>502 Bad Gateway</title></head><body>...</body></html>")
        self.assertEqual(describe_reply(r), "HTTP 502 · nginx/1.20 · text/html · 502 Bad Gateway")

    def test_page_without_title_and_secrets_removed(self):
        r = httpx.Response(500, headers={"content-type": "text/plain"},
                           text="error for fixture-token-123 and 13800000000\n\n trace")
        hint = describe_reply(r, "fixture-token-123", "13800000000")
        self.assertNotIn("fixture-token-123", hint)
        self.assertNotIn("13800000000", hint)
        self.assertTrue(hint.startswith("HTTP 500 · text/plain · error for"))

    def test_long_body_is_cut(self):
        r = httpx.Response(500, text="y" * 10000)
        hint = describe_reply(r)
        self.assertEqual(hint.count("y"), 80)  # the words part is capped; status and type stay
        self.assertLess(len(hint), 140)


def flow(path, status, reply, *, start=0.0, end=0.0, token="fixture-token"):
    response = SimpleNamespace(status_code=status, timestamp_end=end,
                               json=lambda: reply if not isinstance(reply, Exception) else (_ for _ in ()).throw(reply))
    return SimpleNamespace(request=SimpleNamespace(path=path, timestamp_start=start, query={"token": token}),
                           response=response)


class VidAgeTests(unittest.TestCase):
    def setUp(self):
        mitm_addon._captcha_passed_at = None

    def test_interval_from_captcha_pass_to_order(self):
        mitm_addon.note_captcha_check(flow(mitm_addon.CAPTCHA_CHECK_API, 200, {"success": True}, end=100.0))
        line = mitm_addon.vid_age_line(flow(mitm_addon.SAVE_ORDER_API, 200, {"success": True, "msg": "预约成功"},
                                            start=131.4))
        self.assertIn("验证码通过后 31.4 秒提交订单", line)
        self.assertIn("HTTP 200 成功 预约成功", line)
        self.assertIsNone(mitm_addon.vid_age_line(flow(mitm_addon.SAVE_ORDER_API, 200, {}, start=140)))  # one per pass

    def test_failed_captcha_does_not_count_and_token_is_not_printed(self):
        mitm_addon.note_captcha_check(flow(mitm_addon.CAPTCHA_CHECK_API, 200, {"success": False}, end=100.0))
        self.assertIsNone(mitm_addon.vid_age_line(flow(mitm_addon.SAVE_ORDER_API, 200, {}, start=101)))
        mitm_addon.note_captcha_check(flow(mitm_addon.CAPTCHA_CHECK_API, 200, {"success": True}, end=100.0))
        line = mitm_addon.vid_age_line(flow(mitm_addon.SAVE_ORDER_API, 500, {"msg": "验证码失效 fixture-token"},
                                            start=109.0))
        self.assertIn("9.0 秒", line)
        self.assertIn("未成功", line)
        self.assertNotIn("fixture-token", line)

    def test_non_json_order_reply(self):
        mitm_addon.note_captcha_check(flow(mitm_addon.CAPTCHA_CHECK_API, 200, {"success": True}, end=10.0))
        line = mitm_addon.vid_age_line(flow(mitm_addon.SAVE_ORDER_API, 502, ValueError("html"), start=12.0))
        self.assertIn("HTTP 502 未成功 non-json", line)


class RushBriefTests(unittest.TestCase):
    def slot(self, **kw):
        now = datetime(2030, 6, 1, 19, 57, 29, tzinfo=CHINA_TZ)
        base = dict(running=False, phase="idle", reason=None, msg="", applied=[], plan={}, _now=lambda: now,
                    outcome=lambda: None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_waiting_plan(self):
        plan = {"date": "2030-06-03", "release_at": "2030-06-01T20:00:00+08:00",
                "windows": [{"start": "16:00", "end": "18:00"}], "court_priority": [1, 2, 3]}
        brief = Engine._rush_brief(self.slot(running=True, phase="waiting", plan=plan))
        self.assertEqual(brief["release_in_sec"], 151)
        self.assertEqual(brief["windows"], [["16:00", "18:00"]])
        self.assertEqual(brief["courts"], 3)
        self.assertIsNone(brief["last_order"])

    def test_last_order_carries_reply_hint_only(self):
        applied = [{"court": 7, "ok": False, "uncertain": True, "msg": "non-json", "reply_hint": "HTTP 502 · nginx",
                    "diagnostics": {"timing": {}}}]
        brief = Engine._rush_brief(self.slot(phase="degraded", applied=applied, outcome=lambda: "uncertain"))
        self.assertEqual(brief["last_order"], {"court": 7, "ok": False, "uncertain": True, "msg": "non-json",
                                               "reply_hint": "HTTP 502 · nginx"})
        self.assertIsNone(brief["release_in_sec"])


if __name__ == "__main__":
    unittest.main()
