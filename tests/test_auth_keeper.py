import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.auth_keeper import AuthKeeper
from models.auth import TokenState
from models.auth import ProfileSnapshot
from core.token_manager import TokenManager


class AuthKeeperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        audit = patch('core.auth_keeper.record')
        audit.start()
        self.addCleanup(audit.stop)
        self.e = SimpleNamespace(
            config_data={"connection": {"mode": "page", "auto_recover": True}},
            token="fixture", user_id="fixture", _reserve_inflight=set(),
            booking=SimpleNamespace(_locks={}),
            tokens=SimpleNamespace(validate_current=AsyncMock(return_value=False),
                                   configure=Mock(),
                                   auth_revision=1, token='fixture-old',
                                   state=TokenState.EXPIRED,
                                   is_degraded_for_orders=Mock(return_value=False)),
            _sync_from_token_manager=Mock(),
            clash_capture_state=Mock(return_value={"enabled": True, "listening": True, "captured": True}),
            fetch_token=AsyncMock(return_value={"ok": True}),
        )
        self.keeper = AuthKeeper(self.e)

    async def test_recover_with_verified_capture(self):
        self.assertTrue(await self.keeper.check())
        self.e.fetch_token.assert_awaited_once_with(allow_page_capture=True)
        self.assertEqual(self.keeper.status, "ready")

    async def test_network_error_does_not_refresh(self):
        self.e.tokens.state = TokenState.VALID
        self.assertFalse(await self.keeper.check())
        self.e.fetch_token.assert_not_called()

    async def test_disabled_or_unverified_capture_never_refreshes(self):
        self.e.config_data["connection"]["auto_recover"] = False
        await self.keeper.check()
        self.e.config_data["connection"]["auto_recover"] = True
        self.e.clash_capture_state.return_value["captured"] = False
        await self.keeper.check()
        self.e.fetch_token.assert_not_called()

    async def test_concurrent_checks_share_one_refresh(self):
        async def refresh(**kwargs):
            await asyncio.sleep(.01)
            return {'ok': False}
        self.e.fetch_token.side_effect = refresh
        await asyncio.gather(self.keeper.check(), self.keeper.check())
        self.e.fetch_token.assert_awaited_once()

    async def test_valid_token_does_not_refresh(self):
        self.e.tokens.state = TokenState.VALID
        self.e.tokens.validate_current.return_value = True
        self.assertTrue(await self.keeper.check())
        self.e.fetch_token.assert_not_called()

    async def renew(self, **kwargs):
        self.e.tokens.token = 'fixture-new'
        self.e.tokens.auth_revision += 1
        self.e.tokens.state = TokenState.VALID
        return {'ok': True}

    async def test_pre_release_renews_even_valid_token(self):
        self.e.tokens.state = TokenState.VALID
        self.e.fetch_token.side_effect = self.renew
        self.assertTrue(await self.keeper.prepare_rush('2030-06-01T20:00:00+08:00'))
        self.e.fetch_token.assert_awaited_once_with(allow_page_capture=True)
        self.e.tokens.validate_current.assert_not_awaited()
        self.assertEqual(self.keeper.public_view()['retry_in_sec'], 0)

    async def test_two_sports_share_pre_release_renewal(self):
        self.e.fetch_token.side_effect = self.renew
        release = '2030-06-01T20:00:00+08:00'
        self.assertEqual(await asyncio.gather(
            self.keeper.prepare_rush(release), self.keeper.prepare_rush(release)), [True, True])
        self.e.fetch_token.assert_awaited_once()

    async def test_pre_release_rejects_unchanged_token(self):
        self.e.tokens.state = TokenState.VALID
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.assertEqual(self.keeper.status, 'needs_action')

    async def test_unchanged_token_requires_new_successful_validation(self):
        self.e.fetch_token.return_value = {'ok': False}
        async def validate():
            self.e.tokens.state = TokenState.VALID
            return True
        self.e.tokens.validate_current.side_effect = validate
        self.assertTrue(await self.keeper.prepare_rush('release'))
        self.e.tokens.validate_current.assert_awaited_once()
        self.assertEqual(self.e.tokens.auth_revision, 1)
        self.assertIn('不代表续期', self.keeper.message)

    async def test_two_sports_share_unchanged_token_validation(self):
        async def refresh(**kwargs):
            await asyncio.sleep(.01)
            return {'ok': False}
        async def validate():
            self.e.tokens.state = TokenState.VALID
            return True
        self.e.fetch_token.side_effect = refresh
        self.e.tokens.validate_current.side_effect = validate
        self.assertEqual(await asyncio.gather(
            self.keeper.prepare_rush('release'), self.keeper.prepare_rush('release')), [True, True])
        self.e.fetch_token.assert_awaited_once()
        self.e.tokens.validate_current.assert_awaited_once()

    async def test_failed_or_degraded_validation_never_prepares(self):
        self.e.fetch_token.return_value = {'ok': False}
        self.e.tokens.state = TokenState.VALID
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.assertIsNone(self.keeper._prepared_release)
        self.e.tokens.validate_current.return_value = True
        self.e.tokens.is_degraded_for_orders.return_value = True
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.assertIsNone(self.keeper._prepared_release)

    async def test_no_fallback_query_when_capture_unavailable(self):
        self.e.clash_capture_state.return_value['captured'] = False
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.e.tokens.validate_current.assert_not_awaited()

    async def test_no_fallback_query_if_order_starts_during_refresh(self):
        async def refresh(**kwargs):
            self.e._reserve_inflight.add('fixture-order')
            return {'ok': False}
        self.e.fetch_token.side_effect = refresh
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.e.tokens.validate_current.assert_not_awaited()

    async def test_pre_release_respects_opt_out_and_capture_readiness(self):
        self.e.tokens.state = TokenState.VALID
        self.e.tokens.validate_current.return_value = True
        self.e.config_data['connection']['auto_recover'] = False
        self.assertTrue(await self.keeper.prepare_rush('release'))
        self.e.fetch_token.assert_not_awaited()
        self.e.config_data['connection']['auto_recover'] = True
        self.e.clash_capture_state.return_value['captured'] = False
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.e.fetch_token.assert_not_awaited()

    async def test_pre_release_failure_is_bounded_and_never_prepared(self):
        async def refresh(**kwargs):
            await asyncio.sleep(.01)
            return {'ok': False}
        self.e.fetch_token.side_effect = refresh
        self.assertEqual(await asyncio.gather(
            self.keeper.prepare_rush('release'), self.keeper.prepare_rush('release')), [False, False])
        self.e.fetch_token.assert_awaited_once()
        self.assertIsNone(self.keeper._prepared_release)

    async def test_explicit_retry_after_failure_has_no_cooldown(self):
        self.e.fetch_token.return_value = {'ok': False}
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.e.fetch_token.side_effect = self.renew
        self.assertTrue(await self.keeper.prepare_rush('release'))
        self.assertEqual(self.e.fetch_token.await_count, 2)
        self.assertEqual(self.keeper.public_view()['retry_in_sec'], 0)

    async def test_manual_recovery_after_failed_preparation_needs_no_refresh(self):
        self.e.fetch_token.return_value = {'ok': False}
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.e.tokens.validate_current.assert_awaited_once()
        self.e.tokens.validate_current.reset_mock()
        await self.renew()
        self.e.tokens.snapshot = SimpleNamespace(captured_at=datetime.now(timezone.utc))
        self.assertTrue(await self.keeper.prepare_rush('release'))
        self.e.fetch_token.assert_awaited_once()
        self.e.tokens.validate_current.assert_not_awaited()

    async def test_real_manager_revalidates_after_refresh_timeout(self):
        for valid in (True, False):
            with self.subTest(valid=valid):
                tm = TokenManager()
                tm.hydrate_from_config('fixture-token', ProfileSnapshot(userid='fixture'))
                tm.configure(validate=AsyncMock(return_value={'ok': valid, 'auth_fail': not valid}))
                self.e.tokens = tm
                keeper = AuthKeeper(self.e)
                async def failed_refresh(**kwargs):
                    tm._state = TokenState.FAILED
                    return {'ok': False}
                self.e.fetch_token.side_effect = failed_refresh
                try:
                    self.assertEqual(await keeper.prepare_rush('release'), valid)
                    self.assertEqual(tm.state, TokenState.VALID if valid else TokenState.EXPIRED)
                    self.assertEqual(keeper._prepared_release is not None, valid)
                finally:
                    await tm.close()

    async def test_stale_or_unvalidated_replacement_is_not_reused(self):
        self.e.fetch_token.return_value = {'ok': False}
        self.assertFalse(await self.keeper.prepare_rush('release'))
        await self.renew()
        self.e.tokens.snapshot = SimpleNamespace(captured_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.e.tokens.auth_revision += 1
        self.e.tokens.state = TokenState.UNKNOWN
        self.e.tokens.snapshot.captured_at = datetime.now(timezone.utc)
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.assertEqual(self.e.fetch_token.await_count, 3)

    async def test_pre_release_does_not_refresh_during_order(self):
        self.e._reserve_inflight.add('fixture-order')
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.e.fetch_token.assert_not_awaited()

    async def test_concurrent_recovery_reuses_validated_result(self):
        self.e.fetch_token.side_effect = self.renew
        self.assertEqual(await asyncio.gather(
            self.keeper.check(known_failure_only=True),
            self.keeper.check(known_failure_only=True)), [True, True])
        self.e.fetch_token.assert_awaited_once()
        self.e.tokens.validate_current.assert_not_awaited()

    async def test_shared_preparation_is_not_reused_after_auth_failure(self):
        self.e.fetch_token.side_effect = self.renew
        self.assertTrue(await self.keeper.prepare_rush('release'))
        self.e.tokens.state = TokenState.EXPIRED
        self.e.fetch_token.side_effect = None
        self.e.fetch_token.return_value = {'ok': False}
        self.assertFalse(await self.keeper.prepare_rush('release'))
        self.assertEqual(self.e.fetch_token.await_count, 2)

    async def test_no_refresh_during_order(self):
        lock = asyncio.Lock()
        self.e.booking._locks["fixture"] = lock
        async with lock:
            self.assertFalse(await self.keeper.check())
        self.e.tokens.validate_current.assert_not_called()
        self.e.fetch_token.assert_not_called()

    async def test_refresh_result_requires_valid_auth(self):
        self.e.tokens.is_degraded_for_orders.return_value = True
        self.assertFalse(await self.keeper.check())
        self.assertEqual(self.keeper.status, "needs_action")

    async def test_known_auth_failure_skips_redundant_probe(self):
        self.assertTrue(await self.keeper.check())
        self.e.tokens.validate_current.assert_not_called()

    async def test_failure_event_recovers_without_active_task(self):
        self.keeper.start()
        try:
            notify = self.e.tokens.configure.call_args.kwargs['on_auth_failure']
            notify()
            for _ in range(30):
                if self.e.fetch_token.await_count:
                    break
                await asyncio.sleep(.01)
            self.e.fetch_token.assert_awaited_once_with(allow_page_capture=True)
        finally:
            await self.keeper.close()

    async def test_disabled_failure_event_does_not_refresh(self):
        self.e.config_data['connection']['auto_recover'] = False
        self.keeper.start()
        try:
            self.e.tokens.configure.call_args.kwargs['on_auth_failure']()
            await asyncio.sleep(.02)
            self.e.fetch_token.assert_not_called()
        finally:
            await self.keeper.close()

    async def test_busy_failure_event_waits_until_order_releases(self):
        self.e._reserve_inflight.add('fixture-order')
        self.keeper.start()
        try:
            self.keeper.request_recovery()
            await asyncio.sleep(.02)
            self.e.fetch_token.assert_not_called()
            self.assertEqual(self.keeper.status, 'waiting_order')
            self.e._reserve_inflight.clear()
            for _ in range(130):
                if self.e.fetch_token.await_count:
                    break
                await asyncio.sleep(.01)
            self.e.fetch_token.assert_awaited_once()
        finally:
            await self.keeper.close()

    async def test_validation_event_and_explicit_check_do_not_loop_refresh(self):
        tm = TokenManager()
        tm.hydrate_from_config('fixture-token', ProfileSnapshot(userid='fixture'))
        tm.configure(validate=AsyncMock(return_value={'ok': False, 'auth_fail': True}))
        self.e.tokens = tm
        keeper = AuthKeeper(self.e)
        async def failed_refresh(**kwargs):
            tm._state = TokenState.FAILED
            return {'ok': False}
        self.e.fetch_token.side_effect = failed_refresh
        keeper.start()
        try:
            self.assertFalse(await keeper.check())
            await asyncio.sleep(.03)
            self.e.fetch_token.assert_awaited_once()
            self.assertEqual(keeper.status, 'needs_action')
        finally:
            await keeper.close()
            await tm.close()
