"""Regression cases for the shared daily scheduler and manual refresh."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from test_merge import plugin_fixture, NS
from app.plugins.ptsigninkeeper.result import SiteResult


class ConsolidatedRunnerTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.memory = plugin_fixture()
        self.site = {'id': 1, 'name': '测试站'}
        helper = patch(NS + '.SiteOper', return_value=SimpleNamespace(
            list_order_by_pri=lambda: [SimpleNamespace(**self.site)]))
        helper.start()
        self.addCleanup(helper.stop)

    def test_failed_checkin_with_valid_session_is_retried(self):
        self.plugin.signin_site = Mock(return_value=SiteResult(
            '测试站', '签到失败：尚未开放', False, True))
        self.plugin.login_site = Mock(side_effect=AssertionError('redundant login'))
        self.plugin.sign_in()
        self.plugin.sign_in()
        self.assertEqual(self.plugin.signin_site.call_count, 2)
        today = datetime.today().strftime('%Y-%m-%d')
        self.assertEqual(self.memory.data['签到-' + today]['retry'], [1])
        self.assertTrue(self.memory.data['登录-' + today]['results']['1']['success'])

    def test_confirmed_login_only_site_is_not_retried(self):
        self.plugin.signin_site = Mock(return_value=SiteResult(
            '测试站', '登录成功，站点无签到功能', False, True, signin_supported=False))
        self.plugin.sign_in()
        self.plugin.sign_in()
        self.assertEqual(self.plugin.signin_site.call_count, 1)
        today = datetime.today().strftime('%Y-%m-%d')
        self.assertEqual(self.memory.data['签到-' + today]['retry'], [])

    def test_clear_today_resets_both_actions(self):
        today = datetime.today().strftime('%Y-%m-%d')
        for action in ('签到', '登录'):
            self.memory.data[action + '-' + today] = {'results': {'1': {
                'success': True, 'logged_in': True, 'message': '旧成功'}}}
        self.plugin._clean = True
        self.plugin.signin_site = Mock(return_value=SiteResult('测试站', '签到失败', False))
        self.plugin.login_site = Mock(return_value=SiteResult('测试站', '登录失败', False))
        self.plugin.sign_in()
        self.assertFalse(self.memory.data['登录-' + today]['results']['1']['success'])
        self.assertFalse(self.plugin._clean)

    def test_signin_failure_without_evidence_checks_login_separately(self):
        self.plugin.signin_site = Mock(return_value=SiteResult('测试站', '验证码未通过', False))
        self.plugin.login_site = Mock(return_value=SiteResult('测试站', '登录成功', True, True))
        self.plugin.sign_in()
        self.plugin.login_site.assert_called_once()
        today = datetime.today().strftime('%Y-%m-%d')
        self.assertFalse(self.memory.data['签到-' + today]['results']['1']['success'])
        self.assertTrue(self.memory.data['登录-' + today]['results']['1']['success'])

    def test_old_generic_entry_error_rechecks_home_without_resubmitting(self):
        self.plugin.signin_site = Mock(return_value=SiteResult('测试站', '打开签到页失败', False))
        self.plugin._read_account_home = Mock(return_value=SiteResult('测试站', '登录成功（首页已验证）', True, True))
        self.plugin.login_site = Mock(side_effect=AssertionError('must use read-only home'))
        self.plugin.sign_in()
        self.plugin._read_account_home.assert_called_once()


    def test_stop_discards_results_and_skips_second_phase(self):
        def stop_during_site(site):
            self.plugin.stop_service()
            return SiteResult('测试站', '签到成功', True, True)
        self.plugin.signin_site = stop_during_site
        self.plugin.login_site = Mock(side_effect=AssertionError('login after stop'))
        self.plugin.sign_in()
        self.assertFalse(any(k.startswith(('签到-', '登录-')) for k in self.memory.data))


if __name__ == '__main__':
    unittest.main()
