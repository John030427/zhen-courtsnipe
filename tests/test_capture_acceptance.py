"""A capture from Clash → mitmdump must land in the TokenManager, or say why it did not.

Covers a fresh install (template placeholders in config.toml), a real account switch, a
mitmdump restarted under a running workbench, repeats, and the IPC telling a business
rejection apart from success. No school request: validation is mocked.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import httpx

from core import clash_capture
from core.token_manager import TokenManager
from models.auth import ProfileSnapshot
from network import capture_protocol
from network.capture_protocol import parse_capture_payload, post_capture_sync

PATH = "/api/blade-app/qywx/getOrderTimeConfigList"
TEMPLATE = ProfileSnapshot(userid="YOUR_USER_ID", customer_id="YOUR_CUSTOMER_ID", name="YOUR_NAME", tel="YOUR_PHONE")


def event(token="fixture-token-aaaa", userid="fixture-user", source="mitm", **extra):
    payload = capture_protocol.build_capture_payload(token=token, userid=userid, path=PATH, source=source, **extra)
    return parse_capture_payload(payload)


class CaptureAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, token="", profile=TEMPLATE):
        tm = TokenManager()
        self.persisted = Mock()
        self.validate = AsyncMock(return_value={"ok": True, "auth_fail": False})
        tm.configure(persist=self.persisted, validate=self.validate)
        tm.hydrate_from_config(token, profile)
        return tm

    async def test_fresh_install_template_is_not_an_account(self):
        tm = self.manager()
        self.assertEqual(tm.public_view()["message"], "未配置 Token")
        result = await tm.ingest_capture(event())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["mode"], "idle_commit")
        view = tm.public_view()
        self.assertTrue(view["token_set"])
        self.assertEqual(view["userid"], "fixture-user")
        self.assertIsNotNone(tm.last_mitm_capture_at)
        self.assertIsNone(tm.last_capture_reject)
        self.persisted.assert_called_once()
        self.validate.assert_awaited()  # read-only check runs right after the sync
        self.assertTrue(result["validated"])

    async def test_template_name_and_phone_are_not_kept_as_profile(self):
        tm = self.manager()
        await tm.ingest_capture(event())
        profile = tm._snapshot.profile
        self.assertEqual((profile.customer_id, profile.name, profile.tel), ("", "", ""))
        self.assertFalse(tm.public_view()["profile_ready"])

    async def test_another_real_account_is_still_rejected_and_reported(self):
        tm = self.manager(token="fixture-token-old", profile=ProfileSnapshot(userid="fixture-owner"))
        with self.assertLogs("court", level="WARNING") as logs:
            result = await tm.ingest_capture(event(userid="fixture-someone-else"))
        self.assertFalse(result["ok"])
        self.assertIn("账号不匹配", result["msg"])
        reject = tm.last_capture_reject
        self.assertIn("账号不匹配", reject["reason"])
        self.assertEqual(reject["source"], "mitm")
        self.assertTrue(any("capture rejected" in line for line in logs.output))
        self.assertFalse(any("fixture-token" in line for line in logs.output))  # no token in the log
        # The same owner's next capture clears the rejection.
        accepted = await tm.ingest_capture(event(token="fixture-token-new", userid="fixture-owner"))
        self.assertTrue(accepted["ok"], accepted)
        self.assertIsNone(tm.last_capture_reject)

    async def test_mitmdump_restart_under_a_running_workbench(self):
        tm = self.manager()
        # Advance wall time across the simulated process restart. Windows clocks
        # can otherwise return the same tick for both processes in this fast test.
        with patch.object(capture_protocol, "_addon_seq", 0), patch.object(
                capture_protocol.time, "time_ns", return_value=2_000_000_000_000_000_000):
            for _ in range(5):
                self.assertTrue((await tm.ingest_capture(event()))["ok"])
        with patch.object(capture_protocol, "_addon_seq", 0), patch.object(
                capture_protocol.time, "time_ns", return_value=2_000_000_001_000_000_000):
            result = await tm.ingest_capture(event(token="fixture-token-bbbb"))
        self.assertTrue(result["ok"], result)

    async def test_old_or_repeated_sequence_is_stale(self):
        tm = self.manager()
        first = event()
        self.assertTrue((await tm.ingest_capture(first))["ok"])
        again = await tm.ingest_capture(first)
        self.assertEqual(again, {"ok": False, "msg": "stale capture sequence"})
        self.assertEqual(tm.last_capture_reject["reason"], "stale capture sequence")

    async def test_same_token_again_is_a_reachability_ping(self):
        tm = self.manager()
        await tm.ingest_capture(event())
        result = await tm.ingest_capture(event())
        self.assertEqual(result["mode"], "reachable")
        self.assertEqual(self.persisted.call_count, 1)

    def test_sequence_keeps_increasing_within_a_process(self):
        numbers = [capture_protocol.next_addon_sequence() for _ in range(1000)]
        self.assertEqual(numbers, sorted(set(numbers)))
        self.assertLess(numbers[-1], 2 ** 53)  # exact in JSON


class CaptureStateTests(unittest.TestCase):
    def state(self, captured_at, reject):
        return clash_capture.build_clash_capture_state(
            enabled=True, mitm={"listening": True}, last_mitm_capture_at=captured_at,
            host="127.0.0.1", port=8080, reservation_host="reservation.example", last_reject=reject)

    def test_rejected_capture_is_shown_until_a_newer_one_is_accepted(self):
        reject = {"at": "2026-10-24T08:00:00+00:00", "reason": "账号不匹配，已拒绝捕获", "source": "mitm"}
        state = self.state(None, reject)
        self.assertEqual((state["status"], state["label"]), ("rejected", "抓到但未采用"))
        self.assertEqual(state["last_reject_reason"], "账号不匹配，已拒绝捕获")
        later = self.state(datetime(2026, 10, 24, 9, tzinfo=timezone.utc), reject)
        self.assertEqual(later["status"], "captured")
        self.assertIsNone(later["last_reject_reason"])


class IpcReplyTests(unittest.TestCase):
    def post(self, reply):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=reply)

        real_client = httpx.Client
        with patch.object(capture_protocol.httpx, "Client",
                          lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
            result = post_capture_sync({"capture_seq": 1}, url="http://127.0.0.1:1/x", secret="fixture-key")
        return result, calls

    def test_business_rejection_is_not_ok_and_not_retried(self):
        (ok, detail), calls = self.post({"ok": False, "msg": "账号不匹配，已拒绝捕获"})
        self.assertFalse(ok)
        self.assertEqual(detail, "工作台未采用：账号不匹配，已拒绝捕获")
        self.assertEqual(len(calls), 1)

    def test_accepted_is_ok(self):
        (ok, detail), _ = self.post({"ok": True, "mode": "idle_commit"})
        self.assertEqual((ok, detail), (True, ""))

    def test_secret_never_appears_in_the_detail(self):
        (ok, detail), _ = self.post({"ok": False, "msg": "bad fixture-key"})
        self.assertFalse(ok)
        self.assertNotIn("fixture-key", detail)


if __name__ == "__main__":
    unittest.main()
