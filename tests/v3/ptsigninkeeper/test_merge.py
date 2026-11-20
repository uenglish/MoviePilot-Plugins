"""Integration regression tests using the actual V3 host, with in-memory storage/network fakes."""
from copy import deepcopy
from collections import Counter
from datetime import datetime
from threading import Lock
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from app.plugins.ptsigninkeeper import PTSignInKeeper
from app.plugins.ptsigninkeeper.result import SiteResult, has_signin_evidence
from app.plugins.ptsigninkeeper.integration import (
    SignInBridge, merge_config, migrate_history, reward_text, row_result, selection_union,
)
from app.plugins.ptsigninkeeper.dashboard import collect_rows, render_dashboard

NS = 'app.plugins.ptsigninkeeper'


class MemoryHost:
    """Plugin data contract backed by dictionaries; no production credentials or database."""
    def __init__(self, sources=None):
        self.data = {}
        self.sources = sources or {}

    def get_data(self, key=None, plugin_id=None):
        data = self.sources.get(plugin_id, {}) if plugin_id else self.data
        if key is None:
            return [SimpleNamespace(key=k, value=deepcopy(v)) for k, v in data.items()]
        return deepcopy(data.get(key))

    def save_data(self, key, value):
        self.data[key] = deepcopy(value)

    def del_data(self, key):
        self.data.pop(key, None)


def plugin_fixture():
    """Exercise the real plugin class while replacing only external host effects."""
    plugin = object.__new__(PTSignInKeeper)
    memory = MemoryHost()
    for name in ('get_data', 'save_data', 'del_data'):
        setattr(plugin, name, getattr(memory, name))
    plugin.get_config = Mock(return_value={})
    plugin.update_config = Mock()
    plugin.post_message = Mock()
    plugin._PTSignInKeeper__custom_sites = Mock(return_value=[])
    plugin.eventmanager = SimpleNamespace(send_event=Mock())
    plugin._enabled = True
    plugin._sign_sites = ['all']
    plugin._login_sites = ['all']
    plugin._notify = True
    plugin._rich_notify = False
    plugin._queue_cnt = 2
    plugin._clean = False
    plugin._retry_keyword = ''
    plugin._auto_cf = 0
    plugin._cron = '1 0 * * *'
    plugin._onlyonce = False
    plugin._extra_config = {'retry_count': 0, 'browserless_token': 'test-token', 'consolidation_version': 1}
    plugin._run_lock = Lock()
    plugin._bridge = SimpleNamespace(stopped=False, stop=Mock())
    plugin._short_retry_attempt = 0
    plugin._generation = 1
    return plugin, memory


class ConfigurationTests(unittest.TestCase):
    def test_merge_preserves_enhanced_and_adds_pt_options(self):
        current = {'enabled': True, 'cron': '1 0 * * *', 'sign_sites': [1], 'login_sites': [2]}
        ordinary = {'enabled': True, 'sign_sites': [3], 'onlyonce': True, 'rich_notify': False}
        pt = {'enabled': True, 'site_ids': ['all'], 'login_site_ids': [4],
              'cron': '6 0 * * *', 'browserless_token': 'test-secret', 'run_once': True}
        value = merge_config(current, ordinary, pt)
        self.assertEqual(value['cron'], '1 0 * * *')
        self.assertEqual(value['sign_sites'], ['all'])
        self.assertEqual(value['login_sites'], [2, 4])
        self.assertEqual(value['browserless_token'], 'test-secret')
        self.assertFalse(value['onlyonce'])
        self.assertFalse(value['rich_notify'])
        self.assertEqual(current['sign_sites'], [1])
        value['sign_sites'] = [8]
        self.assertEqual(merge_config(value, ordinary, pt)['sign_sites'], [8])

    def test_disabled_sources_do_not_add_sites(self):
        value = merge_config({'sign_sites': [1]}, {'enabled': False, 'sign_sites': ['all']},
                             {'enabled': False, 'site_ids': ['all']})
        self.assertEqual(value['sign_sites'], [1])

    def test_selection_handles_mixed_ids(self):
        self.assertEqual(selection_union('["12", 2]', ['custom', 12, True]), [12, 2, 'custom'])
        self.assertEqual(selection_union([1], 'ALL'), ['all'])

    def test_config_save_keeps_rule_credentials(self):
        plugin, _ = plugin_fixture()
        plugin._PTSignInKeeper__update_config()
        self.assertEqual(plugin.update_config.call_args.args[0]['browserless_token'], 'test-token')


class RoutingTests(unittest.TestCase):
    def bridge(self):
        bridge = SignInBridge({'site_rules': '', 'browserless_url': '', 'read_activity': False})
        bridge._enrich = Mock(side_effect=lambda x: x)
        return bridge

    def test_pt_is_primary_for_covered_sites(self):
        bridge = self.bridge()
        with patch.object(bridge, '_adapter', side_effect=AssertionError('legacy consulted')):
            for url in ('https://u2.dmhy.org/', 'https://hdsky.me/', 'https://hxpt.org/'):
                self.assertEqual(bridge.route({'id': 1, 'url': url})[0], 'pt')
        self.assertEqual(bridge.route({'id': 1, 'url': 'https://piggo.me/'})[0], 'pt')

    def test_one_engine_and_reward(self):
        bridge = self.bridge()
        bridge.route = Mock(return_value=('pt', None))
        bridge._browser = Mock(return_value=None)
        bridge.engine._sign_one = Mock(return_value={'site': '测试站', 'status': 'success',
            'message': '签到成功（获得 25.5 魔力）', 'bonus': '获得 25.5 魔力', 'activity': '刚刚'})
        fallback = Mock(side_effect=AssertionError('extra login'))
        result = bridge.perform({'id': 1, 'name': '测试站', 'url': 'https://example.test/'}, '签到', fallback)
        self.assertTrue(result.success)
        self.assertEqual(result.bonus, '25.5 魔力')
        self.assertEqual(bridge.engine._sign_one.call_count, 1)
        fallback.assert_not_called()

    def test_legacy_adapter_runs_once(self):
        bridge = self.bridge()
        adapter = Mock()
        adapter.return_value.signin.return_value = (True, '签到成功，获得 8 魔力')
        bridge.route = Mock(return_value=('sites', adapter))
        result = bridge.perform({'id': 1, 'name': '测试'}, '签到', Mock())
        self.assertTrue(result.success)
        self.assertEqual(adapter.return_value.signin.call_count, 1)

    def test_failure_cannot_be_success_from_message(self):
        row = {'site': '测试', 'status': 'failed', 'message': '未检测到签到成功，获得 10 魔力', 'bonus': '10'}
        result = row_result(row, '签到')
        self.assertFalse(result.success)
        self.assertEqual(result.bonus, '')

    def test_stopped_bridge_cannot_send(self):
        bridge = self.bridge()
        bridge.stop()
        bridge.route = Mock(side_effect=AssertionError('request after stop'))
        self.assertFalse(bridge.perform({'id': 1, 'name': '测试'}, '签到', Mock()).success)

    def test_all_adapters_import(self):
        bridge = self.bridge()
        bridge._adapter({'url': 'https://unknown.example/'})
        self.assertGreater(len(bridge._adapters), 45)


class RewardTests(unittest.TestCase):
    def test_actual_quantities_only(self):
        samples = {'签到成功（获得 20 魔力）': '20 魔力', '获得 1,234.50 点魔力值': '1,234.50 魔力值',
                   '0': '0', '获得 30 粒爆米花': '30 爆米花', '签到成功（Cookie 已更新）': '',
                   '连续签到 20 天': '', '已签到': '', '签到成功': ''}
        for text, expected in samples.items():
            self.assertEqual(reward_text(text), expected)

    def test_hidden_success_is_not_evidence(self):
        self.assertFalse(has_signin_evidence('<script>签到成功</script><button>签到</button>'))
        self.assertTrue(has_signin_evidence('<div>签到成功</div>'))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.memory = plugin_fixture()
        self.sites = [{'id': 1, 'name': '同名站'}, {'id': 2, 'name': '同名站'}]
        self.helper = patch(NS + '.SitesHelper', return_value=SimpleNamespace(get_indexers=lambda: self.sites))
        self.helper.start()
        self.addCleanup(self.helper.stop)

    def test_daily_dedup_and_dual_login_reuse(self):
        self.plugin.signin_site = Mock(side_effect=lambda s: SiteResult(s['name'], '签到成功', True, True, '12 魔力', '刚刚', 'http'))
        self.plugin.login_site = Mock(side_effect=AssertionError('duplicate login'))
        self.plugin.sign_in()
        self.plugin.sign_in()
        self.assertEqual(self.plugin.signin_site.call_count, 2)
        self.plugin.login_site.assert_not_called()
        day = datetime.today().strftime('%Y-%m-%d')
        self.assertEqual(self.memory.data['签到-' + day]['results']['1']['bonus'], '12 魔力')
        self.assertTrue(self.memory.data['登录-' + day]['results']['1']['success'])

    def test_missing_checkin_entry_can_have_a_logged_in_homepage(self):
        self.plugin.signin_site = Mock(return_value=SiteResult('site', '签到入口 HTTP 404', False, False))
        self.plugin._read_account_home = Mock(return_value=SiteResult('site', '登录成功（首页已验证）', True, True))
        self.plugin.login_site = Mock(side_effect=AssertionError('must not submit another sign-in'))
        self.plugin.sign_in()
        today = datetime.today().strftime('%Y-%m-%d')
        self.assertFalse(self.memory.data['签到-' + today]['results']['1']['success'])
        self.assertTrue(self.memory.data['登录-' + today]['results']['1']['success'])
        self.assertTrue(self.plugin._read_account_home.called)

    def test_only_failed_site_retries(self):
        self.plugin.signin_site = Mock(side_effect=lambda s: SiteResult(s['name'], '签到成功' if s['id'] == 1 else '签到失败', s['id'] == 1, s['id'] == 1))
        self.plugin.login_site = Mock(return_value=SiteResult("测试", "登录失败", False))
        self.plugin.sign_in()
        self.plugin.sign_in()
        self.assertEqual(Counter(x.args[0]['id'] for x in self.plugin.signin_site.call_args_list), Counter([1, 2, 2]))

    def test_busy_run_is_not_reentered(self):
        self.plugin._run_signin = Mock()
        self.plugin._run_lock.acquire()
        self.plugin.sign_in()
        self.plugin._run_signin.assert_not_called()
        self.plugin._run_lock.release()

    def test_old_generation_cannot_save(self):
        def work(site):
            self.plugin._generation += 1
            return SiteResult(site['name'], '签到成功', True, True)
        self.plugin.signin_site = work
        self.plugin._PTSignInKeeper__do(datetime.today(), '签到', [1])
        self.assertFalse(any(key.startswith('签到-') for key in self.memory.data))


class MigrationAndDisplayTests(unittest.TestCase):
    def test_history_migration_idempotent_and_joined(self):
        today = datetime.today().strftime('%Y-%m-%d')
        host = MemoryHost({'PTSiteSignIn': {'latest': [
            {'site': '站点', 'site_id': '1', 'ran_at': today + ' 00:06:00', 'status': 'success',
             'message': '签到成功（获得 10 魔力）', 'bonus': '获得 10 魔力', 'activity': '刚刚'}]}})
        migrate_history(host)
        snapshot = deepcopy(host.data)
        migrate_history(host)
        self.assertEqual(host.data, snapshot)
        self.assertTrue(host.data['签到-' + today]['results']['1']['success'])
        host._build_sites_info = lambda: {1: '站点'}
        host._resolve_site_ids = lambda ids: ids
        host._sign_sites = [1]
        host._login_sites = [1]
        rows = collect_rows(host)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['bonus'], '10 魔力')
        page = render_dashboard(host)
        text = json.dumps(page, ensure_ascii=False)
        self.assertIn('本次魔力 / 奖励', text)
        self.assertEqual(text.count('站点签到与登录'), 1)

    def test_unknown_reward_is_not_zero(self):
        plugin, memory = plugin_fixture()
        today = datetime.today().strftime('%Y-%m-%d')
        memory.data['签到-' + today] = {'results': {'1': {'success': True, 'logged_in': True, 'message': '签到成功'}}}
        plugin._build_sites_info = lambda: {1: '站点'}
        plugin._resolve_site_ids = lambda ids: [1]
        row = collect_rows(plugin)[0]
        self.assertEqual(row['bonus'], '未获取')
        self.assertTrue(row['login']['success'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
