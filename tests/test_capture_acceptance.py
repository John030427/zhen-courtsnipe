"""A capture from Clash → mitmdump must land in the TokenManager, or say why it did not.

Covers a fresh install (template placeholders in config.toml), a real account switch, a
mitmdump restarted under a running workbench, repeats, and the IPC telling a business
rejection apart from success. No school request: validation is mocked.
"""
import asyncio
import logging
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

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
        for _ in range(5):
            self.assertTrue((await tm.ingest_capture(event()))["ok"])
        # A new mitmdump process starts its counter again.
        # A process restart takes time; model that explicitly instead of depending on
        # the Windows wall clock's sub-millisecond resolution in this tight unit test.
        restarted_at = (tm._last_accepted_capture_seq + 1) * 1000
        with patch.object(capture_protocol, "_addon_seq", 0), \
                patch.object(capture_protocol.time, "time_ns", return_value=restarted_at):
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


class FollowAccountTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        tm = TokenManager(refresh_timeout_sec=0.1, max_refresh_attempts=1)
        tm.hydrate_from_config("fixture-token-old", ProfileSnapshot(
            userid="fixture-owner", customer_id="fixture-old-customer",
            name="fixture-old-name", tel="fixture-old-phone"))
        self.busy = False
        self.persisted = Mock()
        self.validate = AsyncMock(return_value={"ok": True})
        tm.configure(persist=self.persisted, validate=self.validate,
                     can_switch_account=lambda: not self.busy)
        self.addAsyncCleanup(tm.close)
        return tm

    async def test_idle_switch_drops_old_profile_and_requires_new_validation(self):
        tm = self.manager()
        await tm.validate_current()
        entered, release = asyncio.Event(), asyncio.Event()

        async def validate(snap):
            entered.set()
            await release.wait()
            return {"ok": False, "auth_fail": True}

        tm.configure(validate=validate)
        task = asyncio.create_task(tm.ingest_capture(event()))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(tm.userid, "fixture-user")
        self.assertEqual(tm.snapshot.profile, ProfileSnapshot(userid="fixture-user"))
        self.assertFalse(tm.public_view()["validated"])
        self.assertEqual(tm.public_view()["validation"], {})
        self.assertIsNone(tm.public_view()["last_success_at"])
        self.assertTrue(tm.is_degraded_for_orders())
        self.persisted.assert_called_once_with(tm.snapshot)
        release.set()
        result = await task
        self.assertTrue(result["account_switched"])
        self.assertFalse(result["validated"])

    async def test_profile_response_with_same_token_is_persisted(self):
        tm = self.manager()
        await tm.ingest_capture(event())
        result = await tm.ingest_capture(event(source="mitm-profile",
            customer_id="fixture-new-customer", name="fixture-new-name", tel="fixture-new-phone"))
        self.assertEqual(result["mode"], "idle_commit")
        self.assertTrue(tm.public_view()["profile_ready"])
        self.assertEqual(self.persisted.call_args.args[0].profile.customer_id, "fixture-new-customer")
        self.assertEqual(self.persisted.call_count, 2)

    async def test_same_token_different_user_still_switches_and_revalidates(self):
        tm = self.manager()
        result = await tm.ingest_capture(event(token="fixture-token-old"))
        self.assertTrue(result["account_switched"])
        self.validate.assert_awaited_once()
        self.assertEqual(tm.snapshot.profile, ProfileSnapshot(userid="fixture-user"))

    async def test_running_task_rejects_other_account_but_accepts_owner_refresh(self):
        tm = self.manager()
        self.busy = True
        rejected = await tm.ingest_capture(event())
        self.assertFalse(rejected["ok"])
        self.assertIn("任务进行中", rejected["msg"])
        self.assertEqual(tm.userid, "fixture-owner")
        self.persisted.assert_not_called()
        accepted = await tm.ingest_capture(event(userid="fixture-owner"))
        self.assertTrue(accepted["ok"])
        self.busy = False
        self.assertEqual(tm.userid, "fixture-owner")  # rejected capture is not queued
        self.assertTrue((await tm.ingest_capture(event()))["account_switched"])

    async def test_guard_is_rechecked_at_commit(self):
        tm = self.manager()
        tm.configure(can_switch_account=Mock(side_effect=[True, False]))
        result = await tm.ingest_capture(event())
        self.assertFalse(result["ok"])
        self.assertEqual(tm.userid, "fixture-owner")
        self.persisted.assert_not_called()

    async def test_missing_userid_cannot_reuse_previous_identity(self):
        tm = self.manager()
        result = await tm.ingest_capture(event(userid=""))
        self.assertFalse(result["ok"])
        self.assertEqual(tm.userid, "fixture-owner")
        self.persisted.assert_not_called()

    async def test_old_capture_cannot_switch_back(self):
        tm = self.manager()
        old = event(userid="fixture-owner")
        await tm.ingest_capture(event())
        self.assertFalse((await tm.ingest_capture(old))["ok"])
        self.assertEqual(tm.userid, "fixture-user")

    async def test_old_recovery_cannot_overwrite_new_account(self):
        tm = self.manager()
        started = asyncio.Event()

        async def trigger():
            started.set()
            return True

        refresh = asyncio.create_task(tm.refresh(trigger))
        await asyncio.wait_for(started.wait(), 1)
        result = await asyncio.wait_for(tm.ingest_capture(event()), 1)
        self.assertTrue(result["account_switched"])
        self.assertTrue(result["validated"])
        self.assertFalse((await refresh)["ok"])
        self.assertEqual(tm.userid, "fixture-user")
        self.assertTrue(tm.public_view()["validated"])

    async def test_same_account_recovery_still_works_during_task(self):
        tm = self.manager()
        self.busy = True

        async def trigger():
            result = await tm.ingest_capture(event(userid="fixture-owner"))
            self.assertEqual(result["mode"], "refresh_pending")
            return True

        result = await tm.refresh(trigger)
        self.assertTrue(result["ok"])
        self.assertEqual(tm.userid, "fixture-owner")
        self.assertTrue(tm.public_view()["validated"])

    async def test_profile_not_lost_while_old_recovery_trigger_is_finishing(self):
        tm = self.manager()
        started, finish_trigger, synced = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def trigger():
            started.set()
            await finish_trigger.wait()
            return False

        async def on_sync(revision):
            synced.set()

        tm.configure(on_sync_segment=on_sync)
        refresh = asyncio.create_task(tm.refresh(trigger))
        await asyncio.wait_for(started.wait(), 1)
        switch = asyncio.create_task(tm.ingest_capture(event()))
        await asyncio.wait_for(synced.wait(), 1)
        synced.clear()
        profile = asyncio.create_task(tm.ingest_capture(event(source="mitm-profile",
            customer_id="fixture-new-customer", name="fixture-new-name", tel="fixture-new-phone")))
        await asyncio.wait_for(synced.wait(), 1)
        finish_trigger.set()
        await asyncio.wait_for(asyncio.gather(refresh, switch, profile), 1)
        self.assertTrue(tm.public_view()["profile_ready"])
        self.assertTrue(tm.public_view()["validated"])
        self.assertEqual(self.persisted.call_args.args[0].profile.name, "fixture-new-name")

    async def test_delayed_old_validation_cannot_change_new_accounts_result(self):
        tm = self.manager()
        started, release = asyncio.Event(), asyncio.Event()

        async def validate(snap):
            if snap.userid == "fixture-owner":
                started.set()
                await release.wait()
                return {"ok": False, "auth_fail": True}
            return {"ok": True}

        tm.configure(validate=validate)
        old = tm.start_validation()
        await asyncio.wait_for(started.wait(), 1)
        await tm.ingest_capture(event())
        release.set()
        await old
        self.assertTrue(tm.public_view()["validated"])
        self.assertEqual(tm.userid, "fixture-user")

    async def test_page_sync_uses_live_task_guard(self):
        tm = self.manager()
        self.busy = True
        result = await tm.apply_page_session(token="fixture-new", userid="fixture-user",
                                             allow_account_switch=True)
        self.assertFalse(result["ok"])
        self.assertEqual(tm.userid, "fixture-owner")


class EngineAccountGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # No server, jobs, or school requests; test only the actual engine wiring.
        with patch.object(logging.getLogger("court"), "handlers", [logging.NullHandler()]):
            from engine import Engine
        self.engine = Engine.__new__(Engine)
        e = self.engine
        e.rush = SimpleNamespace(running=False)
        e.watcher = SimpleNamespace(any_running=Mock(return_value=False))
        e.booking = SimpleNamespace(_locks={})
        e._reserve_inflight = set()
        e.tokens = TokenManager()
        e.ttl_probe = SimpleNamespace(on_sync_segment=AsyncMock(), on_validate_success=AsyncMock())
        e._validate_auth_snapshot = AsyncMock(return_value={"ok": True})
        e.config_store = Mock()
        e.config_data = {}
        e._wire_token_manager()
        e.tokens.hydrate_from_config("fixture-old", ProfileSnapshot(userid="fixture-owner"))
        self.addAsyncCleanup(e.tokens.close)

    async def test_all_busy_states_lock_capture(self):
        e = self.engine
        self.assertTrue(e.can_switch_account())
        for field in ("rush", "watch", "manual", "order_lock"):
            with self.subTest(field=field):
                e.rush.running = field == "rush"
                e.watcher.any_running.return_value = field == "watch"
                e._reserve_inflight = {"fixture-order"} if field == "manual" else set()
                e.booking._locks = {"fixture-owner": Mock(locked=Mock(return_value=field == "order_lock"))}
                self.assertFalse(e.can_switch_account())
                self.assertFalse((await e.tokens.ingest_capture(event()))["ok"])
                self.assertEqual(e.tokens.userid, "fixture-owner")

    async def test_engine_identity_and_config_update_before_validation(self):
        e = self.engine

        async def validate(snap):
            self.assertEqual(e.user_id, snap.userid)
            self.assertEqual(e.token, snap.token)
            self.assertEqual(e.customer_id, "")
            self.assertEqual(e.config_data["user"]["id"], snap.userid)
            return {"ok": True}

        e.tokens.configure(validate=validate)
        self.assertTrue((await e.tokens.ingest_capture(event()))["account_switched"])
        e.config_store.update_sections.assert_called_once()


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
