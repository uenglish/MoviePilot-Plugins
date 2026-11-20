"""Replay Browserless failures without accessing Browserless or tracker sites."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from app.plugins.ptsigninkeeper.ptengine import core
except ModuleNotFoundError:
    path = Path(__file__).resolve().parents[3] / 'plugins.v3/ptsigninkeeper/ptengine/core.py'
    spec = importlib.util.spec_from_file_location('ptsigninkeeper_browserless_test_core', path)
    core = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = core
    spec.loader.exec_module(core)


class BrowserlessErrorTests(unittest.TestCase):
    def test_failed_navigation_retains_http_status_without_submit(self):
        signer = core.BrowserlessSigner('https://browserless.example.test', 'dummy-token')
        signer._request = Mock(return_value={'goto': {'status': 404}, 'before': {'html': '<h1>Not Found</h1>'}})
        result = signer._sign_once({'url': 'https://audiences.me/', 'cookie': 'session=test'},
                                   {'path': '/attendance.php', 'mode': 'cloudflare'})
        self.assertIn('HTTP 404', result.message)
        signer._request.assert_called_once()

    def request(self, status, body, operation='CheckIn'):
        session = Mock(headers={})
        session.post.return_value = SimpleNamespace(status_code=status, text=body)
        signer = core.BrowserlessSigner('https://browserless.example.test', 'dummy-token')
        with patch.object(core, 'new_http_session', return_value=session):
            result = signer._request({}, operation, 'query CheckIn { version }')
        session.post.assert_called_once()
        session.close.assert_called_once()
        return result

    def test_observed_free_plan_401_is_quota_failure(self):
        result = self.request(401, "You've reached the units usage limit allowed under our free plan, "
                              "please upgrade to a paid plan (requestId: private-request-id)")
        self.assertEqual(result.status, 'failed')
        self.assertIn('免费套餐用量已耗尽', result.message)
        self.assertNotIn('鉴权失败', result.message)
        self.assertNotIn('private-request-id', result.message)
        self.assertTrue(core._needs_token_switch(result))

    def test_invalid_token_keeps_authentication_error(self):
        for status in (401, 403):
            with self.subTest(status=status):
                result = self.request(status, '{"message":"Invalid token"}')
                self.assertEqual(result.status, 'failed')
                self.assertIn(f'鉴权失败（HTTP {status}）', result.message)
                self.assertNotIn('额度不足', result.message)

    def test_explicit_quota_error_can_use_other_status(self):
        result = self.request(403, '{"message":"Quota exceeded"}')
        self.assertIn('当前套餐用量已耗尽', result.message)
        self.assertIn('HTTP 403', result.message)

    def test_rate_limit_without_quota_evidence_keeps_existing_message(self):
        result = self.request(429, 'Too many requests')
        self.assertEqual(result.message, 'Browserless 额度不足或请求过于频繁（HTTP 429）')
        self.assertNotIn('用量已耗尽', result.message)

    def test_successful_response_is_not_reclassified_by_page_text(self):
        result = self.request(200, '{"data":{"html":{"html":"quota exceeded"}}}')
        self.assertEqual(result, {'html': {'html': 'quota exceeded'}, '_errors': []})

    def test_quota_failure_rotates_to_available_token(self):
        quota = self.request(401, 'Reached the units usage limit allowed under our free plan')
        pool = core.BrowserlessPool('https://browserless.example.test', ['token-a', 'token-b'])
        first, second = [signer for _, signer in pool._signers]
        first.sign = Mock(return_value=quota)
        second.sign = Mock(return_value=core.SignResult('already', '今日已签到'))
        result = pool.sign({'url': 'https://site.example.test'}, {})
        self.assertEqual(result.status, 'already')
        self.assertEqual(pool.active_index, 2)
        first.sign.assert_called_once()
        second.sign.assert_called_once()

    def test_only_rejected_initial_preflight_allows_local_fallback(self):
        for operation in ('CheckInPreflight', 'CheckIn', 'CheckInSubmit', 'CheckInVerify'):
            with self.subTest(operation=operation):
                result = self.request(401, 'units usage limit under our free plan', operation)
                self.assertEqual(result.not_started, operation == 'CheckInPreflight')
        result = self.request(503, 'Service unavailable', 'CheckInPreflight')
        self.assertFalse(result.not_started)

    def test_token_rotation_does_not_erase_an_earlier_possible_execution(self):
        pool = core.BrowserlessPool('https://browserless.example.test', ['token-a', 'token-b'])
        first, second = [signer for _, signer in pool._signers]
        first.sign = Mock(return_value=core.SignResult('failed', 'Browserless 额度不足', not_started=False))
        second.sign = Mock(return_value=core.SignResult('failed', 'Browserless 额度不足', not_started=True))
        self.assertFalse(pool.sign({'url': 'https://site.example.test'}, {}).not_started)

    def test_transient_preflight_retry_cannot_claim_no_request_started(self):
        session = Mock(headers={})
        session.post.side_effect = [
            SimpleNamespace(status_code=503, text='upstream interrupted'),
            SimpleNamespace(status_code=401, text='units usage limit under our free plan'),
        ]
        signer = core.BrowserlessSigner('https://browserless.example.test', 'dummy-token')
        with patch.object(core, 'new_http_session', return_value=session), patch.object(core.time, 'sleep'):
            result = signer._request({}, 'CheckInPreflight', 'query', retry_transient=True)
        self.assertEqual(session.post.call_count, 2)
        self.assertFalse(result.not_started)


if __name__ == '__main__':
    unittest.main(verbosity=2)
