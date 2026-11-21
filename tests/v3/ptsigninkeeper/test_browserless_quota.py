"""Account queries must not create browsers, disclose tokens, or invent balances."""
import json
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.plugins.ptsigninkeeper import PTSignInKeeper, browserless_quota as quota


class BrowserlessQuotaTests(TestCase):
    def setUp(self):
        quota._CACHE.clear()
        self.payload = {'token': 'private-fixture-token', 'browserless_url': 'https://production-sfo.browserless.io'}

    def response(self, summary=None, status=200, errors=None):
        summary = summary if summary is not None else {
            'plan': 'Free', 'currentPeriodEnd': 1797667301,
            # Real account ledger counters can differ by one; do not recalculate.
            'cloudUnits': {'unitsLeft': 962, 'used': 37, 'available': 1000}}
        return Mock(status_code=status, json=Mock(return_value={'data': {'accountSummary': summary}, 'errors': errors}))

    def test_query_uses_fixed_official_url_and_body_without_browser_work(self):
        response = self.response()
        with patch.object(quota.requests, 'post', return_value=response) as post:
            result = quota.lookup_quota(self.payload)
        self.assertEqual((result['remaining'], result['used'], result['limit']), (962, 37, 1000))
        self.assertEqual(result['scope'], 'account')
        self.assertEqual(post.call_args.args, (quota.ACCOUNT_URL,))
        options = post.call_args.kwargs
        self.assertEqual(options['json']['variables'], {'apiToken': self.payload['token']})
        self.assertNotIn(self.payload['token'], options['json']['query'])
        self.assertFalse(options['allow_redirects'])
        self.assertEqual(options['timeout'], (5, 20))
        self.assertNotIn('apiKeys', options['json']['query'])
        self.assertNotIn(self.payload['token'], json.dumps(result))
        response.close.assert_called_once()

    def test_self_hosted_and_lookalike_domains_never_receive_or_forward_tokens(self):
        with patch.object(quota.requests, 'post') as post:
            for url in ('http://localhost:3000', 'https://browserless.io.attacker.test',
                        'https://notbrowserless.io', 'https://browserless.io@attacker.test',
                        'https://user@production-sfo.browserless.io', 'https://browserless.io:444',
                        'http://production-sfo.browserless.io', 123):
                with self.subTest(url=url):
                    result = quota.lookup_quota({**self.payload, 'browserless_url': url})
                    self.assertEqual(result['status'], 'unsupported')
            post.assert_not_called()

    def test_invalid_inputs_do_not_make_requests(self):
        with patch.object(quota.requests, 'post') as post:
            for payload in (None, [], {}, {'token': 42}, {'token': ' '},
                            {'token': 'bad\nvalue'}, {'token': 'x' * 4097}):
                self.assertEqual(quota.lookup_quota(payload)['status'], 'error')
            post.assert_not_called()

    def test_cache_is_short_lived_token_specific_and_contains_no_plaintext_token(self):
        with patch.object(quota.requests, 'post', side_effect=lambda *a, **kw: self.response()) as post, \
             patch.object(quota.time, 'monotonic', return_value=100) as clock:
            first = quota.lookup_quota(self.payload)
            first['remaining'] = -999
            cached = quota.lookup_quota(self.payload)
            self.assertTrue(cached['cached'])
            self.assertEqual(cached['remaining'], 962)
            self.assertEqual(post.call_count, 1)
            quota.lookup_quota({**self.payload, 'token': 'second-token'})
            self.assertEqual(post.call_count, 2)
            clock.return_value = 161
            self.assertFalse(quota.lookup_quota(self.payload)['cached'])
            self.assertEqual(post.call_count, 3)
        self.assertNotIn(self.payload['token'], repr(quota._CACHE))

    def test_zero_negative_and_unknown_balances_are_distinct(self):
        for value in (0, -2, None, False, 'unknown', float('inf')):
            with self.subTest(value=value), patch.object(quota.requests, 'post', return_value=self.response({
                    'plan': 'Free', 'cloudUnits': {'unitsLeft': value}})):
                quota._CACHE.clear()
                result = quota.lookup_quota(self.payload)
                self.assertEqual(result['remaining'], value if type(value) is int else None)
                self.assertIsNone(result['used'])
                self.assertIsNone(result['limit'])
                self.assertIsNone(result['period_end'])

    def test_non_unit_plan_and_unknown_optional_fields_remain_explicit(self):
        with patch.object(quota.requests, 'post', return_value=self.response({'plan': 'Enterprise'})):
            result = quota.lookup_quota(self.payload)
        self.assertFalse(result['unit_based'])
        self.assertIsNone(result['remaining'])

    def test_remote_errors_and_exceptions_never_echo_credentials_and_are_not_cached(self):
        secret = self.payload['token']
        for response in (self.response(errors=[{'message': secret}]),
                         Mock(status_code=200, json=Mock(side_effect=ValueError(secret))),
                         self.response(status=401), self.response(status=429), self.response(status=302)):
            with patch.object(quota.requests, 'post', return_value=response):
                result = quota.lookup_quota(self.payload)
                self.assertEqual(result['status'], 'error')
                self.assertNotIn(secret, json.dumps(result))
                self.assertFalse(quota._CACHE)
        for error in (requests.Timeout(secret), requests.ConnectionError(secret)):
            with patch.object(quota.requests, 'post', side_effect=error):
                result = quota.lookup_quota(self.payload)
                self.assertEqual(result['status'], 'error')
                self.assertNotIn(secret, json.dumps(result))

    def test_malformed_or_partial_graphql_result_is_not_a_zero_balance(self):
        for body in ([], {}, {'data': []}, {'data': {'accountSummary': None}},
                     {'data': {'accountSummary': {}}}, {'data': {'accountSummary': 'invalid'}}):
            with patch.object(quota.requests, 'post', return_value=Mock(status_code=200, json=lambda: body)):
                self.assertEqual(quota.lookup_quota(self.payload)['status'], 'error')
                self.assertFalse(quota._CACHE)

    def test_only_selected_fields_leave_backend(self):
        with patch.object(quota.requests, 'post', return_value=self.response({
                'plan': self.payload['token'], 'accountId': 'private-id',
                'apiKeys': [{'token': self.payload['token']}], 'cloudUnits': {}})):
            text = json.dumps(quota.lookup_quota(self.payload))
        self.assertNotIn(self.payload['token'], text)
        self.assertNotIn('private-id', text)
        self.assertNotIn('apiKeys', text)

    def test_ui_contract_is_authenticated_post_body_and_does_not_save_config(self):
        host = object.__new__(PTSignInKeeper)
        host.update_config = Mock()
        route = next(item for item in host._ui_routes() if item['path'] == '/ui/browserless-quota')
        self.assertEqual(route['auth'], 'bear')
        self.assertEqual(route['methods'], ['POST'])
        app = FastAPI()
        app.add_api_route(route['path'], route['endpoint'], methods=route['methods'])
        schema = app.openapi()['paths'][route['path']]['post']
        self.assertIn('requestBody', schema)
        self.assertNotIn('parameters', schema)
        with TestClient(app) as client, patch.object(quota.requests, 'post', return_value=self.response()):
            response = client.post(route['path'], json=self.payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['remaining'], 962)
            self.assertEqual(client.get(route['path']).status_code, 405)
        host.update_config.assert_not_called()
