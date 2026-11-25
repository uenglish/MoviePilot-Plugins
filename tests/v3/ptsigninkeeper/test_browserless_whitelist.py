"""Browserless access policy, exercised with fake network clients only."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from app.plugins.ptsigninkeeper.integration import SignInBridge, merge_config
from app.plugins.ptsigninkeeper.ptengine.browserless_policy import browserless_site_ids
from app.plugins.ptsigninkeeper.ptengine.core import SignResult
from app.plugins.ptsigninkeeper.ptengine.runtime import RuleEngine

NS = 'app.plugins.ptsigninkeeper.ptengine.runtime'
SITE = {'id': 1, 'name': '测试站', 'url': 'https://open.cd/', 'cookie': 'test=only'}
HTTP_SITE = dict(SITE, url='https://example.test/')
SUCCESS = SignResult('success', '签到成功')
CHALLENGE = SignResult('failed', 'Cloudflare 验证', challenge='cloudflare')


class WhitelistConfigurationTests(unittest.TestCase):
    def test_normalizes_explicit_ids_without_wildcards_or_mutating_input(self):
        values = ['1', 1, ' 002 ', 'custom-a', ' custom-a ', True, False, None,
                  {}, 1.5, -1, 0, '', 'all', 'ALL', '*']
        original = deepcopy(values)
        self.assertEqual(browserless_site_ids(values), [1, 2, 'custom-a'])
        self.assertEqual(values, original)
        self.assertEqual(browserless_site_ids('["1", "custom-a"]'), [1, 'custom-a'])

    def test_missing_empty_and_invalid_configuration_deny_all(self):
        for value in (None, [], '', 'all', ['all'], ['*'], True, 1, {'1': True}):
            with self.subTest(value=value):
                engine = RuleEngine({'browserless_sites': value})
                self.assertIn('白名单', engine._browserless_block_reason(SITE))
        self.assertIn('白名单', RuleEngine({})._browserless_block_reason(SITE))

    def test_saved_empty_list_never_repopulates_from_legacy_config(self):
        for current in ({}, {'consolidation_version': 1}):
            current.update(browserless_sites=[], future_option={'keep': True})
            saved = merge_config(current, {}, {'browserless_sites': [1]})
            self.assertEqual(saved['browserless_sites'], [])
            self.assertEqual(saved['future_option'], {'keep': True})
            self.assertEqual(merge_config(saved, {}, {})['browserless_sites'], [])

    def test_matching_uses_stable_site_id_and_freezes_each_configuration(self):
        config = {'browserless_sites': ['1', 'custom-a']}
        engine = RuleEngine(config)
        config['browserless_sites'].append(2)
        for site_id in (1, '1', 'custom-a'):
            self.assertEqual(engine._browserless_block_reason(dict(SITE, id=site_id)), '')
        for site_id in (None, True, 2, 'all'):
            self.assertIn('白名单', engine._browserless_block_reason(dict(SITE, id=site_id)))

    def test_bridge_never_builds_or_returns_pool_for_unlisted_site(self):
        config = {'browserless_sites': [1], 'browserless_url': 'https://browserless.example.test'}
        bridge = SignInBridge(config)
        with patch('app.plugins.ptsigninkeeper.integration.BrowserlessPool') as factory:
            self.assertIsNone(bridge._browser(dict(SITE, id=2)))
            factory.assert_not_called()
            pool = bridge._browser(SITE)
            factory.assert_called_once()
            self.assertIsNotNone(pool)
            self.assertIsNone(bridge._browser(dict(SITE, id=2)))
            self.assertEqual(factory.call_count, 1)


class WhitelistRuntimeTests(unittest.TestCase):
    def engine(self, allowed=()):
        engine = RuleEngine({'browserless_sites': list(allowed), 'read_activity': False})
        engine.read_header_reward = Mock(return_value='')
        return engine

    @staticmethod
    def signer():
        signer = Mock()
        signer.sign.return_value = SUCCESS
        signer.sign_api.return_value = SUCCESS
        return signer

    def test_direct_and_explicit_custom_rule_cannot_bypass_whitelist(self):
        engine, signer = self.engine(), self.signer()
        for rules in ({}, {'1': {'engine': 'browserless'}},
                      {'open.cd': {'engine': 'browserless'}}):
            with self.subTest(rules=rules), patch(NS + '.run_site_module', return_value=None):
                row = engine._sign_one(SITE, rules, signer)
            self.assertEqual(row['status'], 'failed')
            self.assertIn('白名单', row['message'])
            self.assertFalse(row['browserless_not_started'])
        signer.sign.assert_not_called()
        signer.sign_api.assert_not_called()

    def test_whitelisted_direct_request_executes_once(self):
        engine, signer = self.engine([1]), self.signer()
        with patch(NS + '.run_site_module', return_value=None):
            row = engine._sign_one(SITE, {}, signer)
        self.assertEqual(row['status'], 'success')
        signer.sign.assert_called_once()

    def test_module_failure_cannot_bypass_whitelist(self):
        engine, signer = self.engine(), self.signer()
        with patch(NS + '.run_site_module', return_value=SignResult('failed', 'OCR 不可用')):
            row = engine._sign_one(SITE, {}, signer)
        self.assertIn('白名单', row['message'])
        signer.sign.assert_not_called()

    def test_http_fallback_is_blocked_before_browser_preparation(self):
        engine, signer = self.engine(), self.signer()
        for outcome in (CHALLENGE, SignResult('failed', '图片验证码', challenge='captcha'),
                        SignResult('failed', '未匹配到签到结果')):
            with self.subTest(reason=outcome.message), \
                 patch(NS + '.run_site_module', return_value=None), \
                 patch(NS + '.http_sign_auto', return_value=outcome), \
                 patch(NS + '.fetch_page') as fetch:
                row = engine._sign_one(HTTP_SITE, {}, signer)
            self.assertEqual(row['engine'], 'http')
            self.assertIn('白名单', row['message'])
            fetch.assert_not_called()
        signer.sign.assert_not_called()

    def test_whitelisted_http_challenge_falls_back_once(self):
        engine, signer = self.engine([1]), self.signer()
        with patch(NS + '.run_site_module', return_value=None), \
             patch(NS + '.http_sign_auto', return_value=CHALLENGE), \
             patch(NS + '.fetch_page', return_value=''):
            row = engine._sign_one(HTTP_SITE, {}, signer)
        self.assertEqual((row['status'], row['engine']), ('success', 'browserless'))
        signer.sign.assert_called_once()

    def test_http_success_is_unaffected_and_whitelist_does_not_force_browser(self):
        for allowed in ([], [1]):
            engine, signer = self.engine(allowed), self.signer()
            with patch(NS + '.run_site_module', return_value=None), \
                 patch(NS + '.http_sign_auto', return_value=SUCCESS) as http:
                row = engine._sign_one(HTTP_SITE, {}, signer)
            self.assertEqual((row['status'], row['engine']), ('success', 'http'))
            http.assert_called_once()
            signer.sign.assert_not_called()

    def test_local_browser_still_runs_but_its_cloud_fallback_is_guarded(self):
        engine, signer = self.engine(), self.signer()
        rule = {'browserless_fallback': True}
        with patch(NS + '.playwright_sign', return_value=SUCCESS) as local:
            result = engine._sign_with_engine(SITE, rule, 'playwright', signer, SITE['name'])
        self.assertEqual(result.status, 'success')
        local.assert_called_once()
        for outcome in (None, SignResult('failed', '本机访问失败')):
            with self.subTest(outcome=outcome), patch(NS + '.playwright_sign', return_value=outcome):
                result = engine._sign_with_engine(SITE, rule, 'playwright', signer, SITE['name'])
            self.assertIn('白名单', result.message)
        signer.sign.assert_not_called()

    def test_dolby_local_fallback_works_even_outside_cloud_whitelist(self):
        engine, signer = self.engine(), self.signer()
        rule = {'auto_fallback': True, 'local_browser_fallback': True}
        outcome = SignResult('failed', '未匹配到签到结果')
        with patch(NS + '.playwright_sign', return_value=SUCCESS):
            result, mode = engine._maybe_fallback(HTTP_SITE, rule, outcome, signer)
        self.assertEqual((result.status, mode), ('success', 'playwright'))
        with patch(NS + '.playwright_sign', return_value=None), patch(NS + '.fetch_page') as fetch:
            result, _ = engine._maybe_fallback(HTTP_SITE, rule, outcome, signer)
        self.assertIn('白名单', result.message)
        signer.sign.assert_not_called()
        fetch.assert_not_called()

    def test_dstudio_and_dolby_read_receipts_without_cloud_browser_configuration(self):
        for host in ('dstudio.me', 'www.uhddolby.com'):
            site = {**HTTP_SITE, 'url': 'https://' + host + '/'}
            engine, signer = self.engine(), self.signer()
            with self.subTest(host=host), patch(NS + '.run_site_module', return_value=None), \
                 patch(NS + '.http_sign_auto', return_value=SUCCESS) as http, \
                 patch.object(engine, 'read_header_reward', return_value=''):
                row = engine._sign_one(site, {}, signer)
            self.assertEqual((row['status'], row['engine']), ('success', 'http'))
            http.assert_called_once()
            signer.sign.assert_not_called()

    def test_api_key_challenge_obeys_whitelist_even_with_existing_pool(self):
        for allowed in ([], [1]):
            engine, signer = self.engine(allowed), self.signer()
            with patch(NS + '.http_sign_auto', return_value=CHALLENGE):
                row = engine._sign_api_key_one(dict(HTTP_SITE, apikey='fixture-only'),
                    {'auth': 'api_key', 'engine': 'http'}, None, None, signer)
            if allowed:
                self.assertEqual((row['status'], row['engine']), ('success', 'browserless'))
                signer.sign_api.assert_called_once()
            else:
                self.assertEqual(row['engine'], 'http')
                self.assertIn('白名单', row['message'])
                signer.sign_api.assert_not_called()
            signer.sign.assert_not_called()

    def test_clear_and_reload_revokes_previous_pool_access(self):
        old, signer = self.engine([1]), self.signer()
        old.stop()
        new = self.engine([])
        for engine in (old, new):
            for api in (False, True):
                self.assertEqual(engine._sign_browserless(SITE, {}, signer, api=api).status, 'failed')
        signer.sign.assert_not_called()
        signer.sign_api.assert_not_called()
