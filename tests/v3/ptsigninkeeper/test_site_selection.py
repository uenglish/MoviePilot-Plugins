"""Configured accounts must reach the runner even without host indexer rules."""
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.plugins.ptsigninkeeper.result import SiteResult
from test_merge import NS, plugin_fixture


class ConfiguredSiteTests(TestCase):
    def setUp(self):
        self.plugin, self.memory = plugin_fixture()
        self.plugin._notify = False
        self.plugin._login_sites = []
        self.sites = [
            {'id': 32, 'name': '高清杜比', 'domain': 'uhddolby.com',
             'url': 'https://www.uhddolby.com/', 'is_active': True, 'public': 0,
             'cookie': 'session=fixture-cookie', 'ua': 'fixture-agent', 'proxy': 1,
             'render': 0, 'timeout': 17, 'apikey': 'fixture-key', 'token': 'fixture-token',
             'note': {'account': 'fixture-account'}},
            {'id': 49, 'name': 'Depth Studio', 'url': 'https://dstudio.me/',
             'is_active': True, 'public': 0},
        ]
        oper = patch(NS + '.SiteOper', return_value=SimpleNamespace(
            list_order_by_pri=lambda: [SimpleNamespace(**site) for site in self.sites]))
        oper.start(); self.addCleanup(oper.stop)
        # The host cannot identify the new domain. Its search catalog must not
        # decide which configured accounts can run independent sign-in rules.
        helper = patch(NS + '.SitesHelper', return_value=SimpleNamespace(
            get_indexers=Mock(return_value=[]), get_indexer=Mock(return_value=None)))
        helper.start(); self.addCleanup(helper.stop)
        self.plugin.signin_site = Mock(side_effect=lambda site: SiteResult(
            site['name'], '今日已签到，获得 164 鲸币', True, True, '164 鲸币', engine='http'))

    def results(self):
        day = datetime.today().strftime('%Y-%m-%d')
        return (self.memory.data.get('签到-' + day) or {}).get('results', {})

    def test_selected_new_domain_runs_with_original_account_and_connection_options(self):
        self.plugin._sign_sites = ['32']
        self.plugin.sign_in()
        self.plugin.signin_site.assert_called_once_with(self.sites[0])
        self.assertTrue(self.results()['32']['success'])
        self.assertNotIn('49', self.results())

    def test_all_includes_unrecognized_domain_and_daily_results_deduplicate(self):
        self.plugin.sign_in()
        self.plugin.sign_in()
        self.assertEqual(self.plugin.signin_site.call_count, 2)
        self.assertEqual(set(self.results()), {'32', '49'})

    def test_disabled_and_public_sites_never_run_even_when_explicitly_selected(self):
        self.sites[0]['is_active'] = False
        self.sites[1]['public'] = 1
        for selected in ([32, 49], ['all']):
            self.plugin._sign_sites = selected
            self.plugin.sign_in()
        self.plugin.signin_site.assert_not_called()
        self.assertEqual(self.results(), {})

    def test_custom_sites_preserve_ids_and_cannot_replace_database_accounts(self):
        self.plugin._PTSignInKeeper__custom_sites.return_value = [
            {'id': '32', 'name': '重复账号', 'url': 'https://other.invalid/'},
            {'id': 'custom-a', 'name': '自定义站', 'url': 'https://custom.invalid/'},
            {'id': 'custom-a', 'name': '重复自定义站', 'url': 'https://duplicate.invalid/'},
            {'id': 'custom-off', 'name': '已停用', 'is_active': 'false'},
            {'id': 'custom-public', 'name': '公开站点', 'public': '1'},
        ]
        self.plugin.sign_in()
        calls = {str(call.args[0]['id']): call.args[0] for call in self.plugin.signin_site.call_args_list}
        self.assertEqual(set(calls), {'32', '49', 'custom-a'})
        self.assertEqual(calls['32'], self.sites[0])
        self.assertEqual(calls['custom-a']['url'], 'https://custom.invalid/')
        self.assertEqual(self.plugin.signin_site.call_count, 3)

    def test_domain_api_uses_same_accounts_and_daily_deduplication(self):
        with patch(NS + '.settings.API_TOKEN', 'fixture-host-key'):
            denied = self.plugin.signin_by_domain('https://www.uhddolby.com/', 'wrong')
            self.assertFalse(denied.success)
            self.plugin.signin_site.assert_not_called()
            for url in ('https://www.uhddolby.com/', 'https://uhddolby.com/attendance.php', 'uhddolby.com'):
                result = self.plugin.signin_by_domain(url, 'fixture-host-key')
                self.assertTrue(result.success)
            lookalike = self.plugin.signin_by_domain('https://uhddolby.com.example.invalid/', 'fixture-host-key')
            self.assertFalse(lookalike.success)
        self.plugin.signin_site.assert_called_once()

    def test_domain_api_does_not_reenable_disabled_account(self):
        self.sites[0]['is_active'] = False
        with patch(NS + '.settings.API_TOKEN', 'fixture-host-key'):
            result = self.plugin.signin_by_domain('https://www.uhddolby.com/', 'fixture-host-key')
        self.assertFalse(result.success)
        self.plugin.signin_site.assert_not_called()

    def test_dispatch_log_identifies_site_without_credentials(self):
        self.plugin._sign_sites = [32]
        with patch(NS + '.logger') as logger:
            self.plugin.sign_in()
        lines = '\n'.join(str(call.args[0]) for call in logger.info.call_args_list)
        self.assertIn('开始站点签到：高清杜比（ID：32）', lines)
        for key in ('cookie', 'apikey', 'token', 'ua'):
            self.assertNotIn(self.sites[0][key], lines)
