import asyncio
import json
import unittest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from core.safety import set_order_dry_run_for_tests
from core.booking_service import BookingService, BookingSource, BookingTarget
from core.sports import DEFAULT_TENNIS_COURTS, courts_from_map
from engine import Engine
from models.auth import ProfileSnapshot
from tests.test_booking_service import engine_stub
from utils import CaptchaParseError, Court, get_vid_once, parse_rotate_captcha, _CaptchaSolver


_MIN_CAPTCHA = {
    "backgroundImage": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
    "templateImage": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
}


class ParseRotateCaptchaTests(unittest.TestCase):
    def test_dict_passthrough(self):
        out = parse_rotate_captcha(_MIN_CAPTCHA)
        self.assertEqual(out, _MIN_CAPTCHA)

    def test_json_string_parsed(self):
        raw = json.dumps(_MIN_CAPTCHA)
        out = parse_rotate_captcha(raw)
        self.assertEqual(out["backgroundImage"], _MIN_CAPTCHA["backgroundImage"])

    def test_illegal_string_raises(self):
        with self.assertRaises(CaptchaParseError):
            parse_rotate_captcha("not-json")

    def test_missing_fields_raises(self):
        with self.assertRaises(CaptchaParseError):
            parse_rotate_captcha({"backgroundImage": "x"})


class GetVidOnceCaptchaShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_timing_separates_pacing_from_response_without_payload(self):
        async def post(url, *, json, timeout, extensions):
            timing = extensions["query_timing"]
            timing["sent_at"] = timing["started_at"] + .2
            timing["response_at"] = timing["sent_at"] + .4
            payload = ({"id": "private-id", "captcha": _MIN_CAPTCHA}
                       if "generate" in url else {"success": True, "data": "private-vid"})
            return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

        with patch("utils.get_client", return_value=MagicMock(post=AsyncMock(side_effect=post))), \
                patch("utils._CaptchaSolver._recognize", return_value=0), \
                patch("utils._CaptchaSolver._make_track_sync", return_value=[]):
            _, timing = await get_vid_once()
        for stage in ("gen", "chk"):
            self.assertEqual(timing[stage + "_queue_ms"], 200)
            self.assertEqual(timing[stage + "_request_ms"], 400)
        self.assertNotIn("private", str(timing))

    async def test_nested_bad_captcha_reports_shape_not_body(self):
        response = httpx.Response(200, json={"id": "secret-id", "captcha": "secret-text"},
                                  request=httpx.Request("POST", "https://example.test/generate"))
        client = MagicMock(post=AsyncMock(return_value=response))
        with patch("utils.get_client", return_value=client), patch("core.audit.record") as audit:
            with self.assertRaises(CaptchaParseError):
                await _CaptchaSolver()._generate()
        fields = audit.call_args.kwargs
        self.assertEqual(fields["failure_stage"], "captcha_payload")
        self.assertEqual(fields["captcha_type"], "str")
        self.assertEqual(fields["captcha_length"], 11)
        self.assertEqual(fields["json_error"], "JSONDecodeError")
        self.assertNotIn("secret", str(audit.call_args))

    async def test_non_json_generate_records_metadata_without_response_body(self):
        response = httpx.Response(200, text='Bad Gateway',
                                  request=httpx.Request('POST', 'https://example.test/generate'))
        client = MagicMock(post=AsyncMock(return_value=response))
        with patch('utils.get_client', return_value=client), patch('core.audit.record') as audit:
            with self.assertRaises(CaptchaParseError):
                await _CaptchaSolver()._generate()
        audit.assert_called_once()
        self.assertEqual(audit.call_args.args[0], 'vid.generate_failure')
        self.assertEqual(audit.call_args.kwargs['http_status'], 200)
        self.assertEqual(audit.call_args.kwargs['response_bytes'], 11)
        self.assertNotIn('Bad Gateway', str(audit.call_args))

    async def test_generate_string_captcha_does_not_type_error(self):
        client = MagicMock()
        gen_resp = MagicMock()
        gen_resp.raise_for_status = MagicMock()
        gen_resp.json.return_value = {
            "id": "cid-1",
            "captcha": json.dumps(_MIN_CAPTCHA),
        }
        chk_resp = MagicMock()
        chk_resp.raise_for_status = MagicMock()
        chk_resp.json.return_value = {"success": True, "data": "vid-abcdef12"}
        client.post = AsyncMock(side_effect=[gen_resp, chk_resp])

        with patch("utils.get_client", return_value=client), patch(
            "utils._CaptchaSolver._recognize", return_value=42,
        ):
            vid, timing = await get_vid_once(timeout=2.0)
        self.assertEqual(vid, "vid-abcdef12")
        self.assertIn("total_ms", timing)

    async def test_illegal_captcha_propagates_parse_error(self):
        client = MagicMock()
        gen_resp = MagicMock()
        gen_resp.raise_for_status = MagicMock()
        gen_resp.json.return_value = {"id": "cid-2", "captcha": "{{bad"}
        client.post = AsyncMock(return_value=gen_resp)

        with patch("utils.get_client", return_value=client):
            with self.assertRaises(CaptchaParseError):
                await get_vid_once(timeout=2.0)


class BookingVidGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        set_order_dry_run_for_tests(False)

    async def test_vid_parse_failure_skips_save_order(self):
        e = engine_stub()
        svc = BookingService(e)
        target = BookingTarget(e.court_by_no[1], "2030-06-01", "19:00", "20:00")
        with patch("core.booking_service.get_order_once", AsyncMock(return_value={
            "ok": True, "avail": {"19:00": "1", "19:30": "1"}, "blocks": [],
        })), patch(
            "core.booking_service.get_vid_once",
            AsyncMock(side_effect=CaptchaParseError("captcha JSON 无效")),
        ), patch("core.booking_service.save_order_once", AsyncMock()) as save_mock:
            out = await svc.place_order(target, source=BookingSource.MANUAL)
        save_mock.assert_not_called()
        self.assertFalse(out.ok)
        self.assertIn("captcha", out.msg.lower())


class TennisValidationProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tennis_rush_context_uses_tennis_green_ground(self):
        tennis_courts = courts_from_map(DEFAULT_TENNIS_COURTS)
        e = Engine.__new__(Engine)
        e._validation_sport_id = "tennis"
        e._matrix_caches = {}
        e.offset_days = 1
        e.courts = [Court(1, "badminton-g1", "羽1")]
        e.court_by_no = {c.no: c for c in e.courts}
        e.courts_by_sport = {"tennis": tennis_courts}
        bucket = e._matrix_bucket("tennis")
        bucket["current"] = {
            "date": "2030-06-05",
            "courts": [{"court": 1, "ok": False}, {"court": 3, "ok": True}],
        }
        snap = type("Snap", (), {"token": "tok", "profile": ProfileSnapshot(userid="demo")})()
        with patch("engine.get_order_once", new_callable=AsyncMock) as query:
            query.return_value = {"ok": True, "auth_fail": False, "avail": {}, "blocks": []}
            await e._validate_auth_snapshot(snap)
        court, date, *_ = query.await_args.args
        self.assertEqual(date, "2030-06-05")
        self.assertEqual(court.id, DEFAULT_TENNIS_COURTS[3])


if __name__ == "__main__":
    unittest.main()
