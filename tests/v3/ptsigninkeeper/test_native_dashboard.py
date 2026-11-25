"""Homepage JSON keeps the existing daily evidence and works in real hosts."""
from copy import deepcopy
from datetime import date
import json
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.plugins.ptsigninkeeper import PTSignInKeeper, native_dashboard, ui
from test_ui import Host, site


def texts(nodes):
    return '\n'.join(str(node.get('text', '')) + '\n' + texts(node.get('content', []))
                     for node in nodes)


def metrics(nodes):
    return {node['content'][1]['text']: node['content'][0]['text']
            for node in nodes[0]['content']}


class NativeDashboardTests(unittest.TestCase):
    def setUp(self):
        self.sites = [site(1), site(2), site(3, active=False)]
        self.oper = patch.object(ui, 'SiteOper', return_value=SimpleNamespace(
            list_order_by_pri=lambda: self.sites))
        self.day = patch.object(ui, '_today', return_value=date(2026, 11, 16))
        self.oper.start()
        self.day.start()
        self.addCleanup(self.oper.stop)
        self.addCleanup(self.day.stop)

    def test_keys_and_empty_selection_never_start_work(self):
        host = Host()
        host._sign_sites = host._login_sites = []
        self.assertEqual(PTSignInKeeper.get_dashboard_meta(host),
                         [{'key': 'ptsigninkeeper_dashboard', 'name': 'PT 签到管家'}])
        self.assertIsNone(PTSignInKeeper.get_dashboard(host, 'unknown'))
        host.get_data.assert_not_called()
        cols, attrs, nodes = PTSignInKeeper.get_dashboard(host, 'ptsigninkeeper_dashboard')
        self.assertEqual(cols, {'cols': 12, 'md': 6})
        self.assertEqual(attrs['refresh'], 300)
        self.assertEqual(nodes[0]['component'], 'VAlert')
        self.assertIn('选择签到或登录站点', texts(nodes))
        # A read-only host deliberately supplies no runner, scheduler or sender.
        self.assertEqual(host.get_data.call_count, 2)

    def test_total_is_global_but_preview_is_bounded_and_disabled_sites_are_omitted(self):
        self.sites = [site(i) for i in range(1, 123)]
        self.sites[-1].is_active = False
        host = Host({str(i): {'success': True, 'bonus': 0} for i in range(1, 123)})
        nodes = native_dashboard.render(host)
        self.assertEqual(metrics(nodes)['签到成功'], '121')
        self.assertIn('121 个站点 · 展示 3 站', texts(nodes))
        self.assertNotIn('站点122', texts(nodes))
        self.assertEqual(sum(node['component'] == 'VDivider' for node in nodes), 3)
        self.assertLess(len(json.dumps(nodes, ensure_ascii=False)), 6500)

    def test_login_only_success_is_not_a_signin_or_reward(self):
        host = Host(logins={'2': {'success': True}})
        host._sign_sites, host._login_sites = [], [2]
        nodes = native_dashboard.render(host)
        self.assertEqual(metrics(nodes)['签到成功'], '0')
        self.assertEqual(metrics(nodes)['登录成功'], '1')
        self.assertIn('签到：未选择 · 登录：登录成功', texts(nodes))
        self.assertNotIn('奖励：', texts(nodes))

    def test_zero_unknown_and_site_currency_remain_distinct(self):
        self.sites = [site(i) for i in range(1, 4)]
        host = Host({'1': {'success': True, 'bonus': 0},
                     '2': {'success': True},
                     '3': {'success': True, 'bonus': '8 魔力'}})
        output = texts(native_dashboard.render(host))
        self.assertIn('奖励：0', output)
        self.assertIn('奖励：未获取', output)
        self.assertIn('奖励：8 魔力', output)
        self.assertNotIn('总奖励', output)

    def test_u2_waiting_and_unconfirmed_receipts_are_not_success(self):
        self.sites[0].url = 'https://u2.dmhy.org/'
        for receipt in ({'success': False, 'unconfirmed': True, 'message': '待开放：09:00 后核验'},
                        {'success': True, 'bonus': 100}):
            with self.subTest(receipt=receipt):
                host = Host({'1': receipt})
                host._sign_sites, host._login_sites = [1], []
                nodes = native_dashboard.render(host)
                self.assertEqual(metrics(nodes)['签到成功'], '0')
                self.assertEqual(metrics(nodes)['待完成'], '1')
                self.assertNotIn('奖励：100', texts(nodes))
                self.assertTrue('待开放' in texts(nodes) or '结果未确认' in texts(nodes))

    def test_no_signin_capability_keeps_independent_login_evidence(self):
        self.sites[0].url = 'https://kp.m-team.cc/'
        host = Host({'1': {'success': True, 'bonus': 20, 'message': '账号校验通过'}})
        host._sign_sites, host._login_sites = [1], [1]
        nodes = native_dashboard.render(host)
        self.assertEqual(metrics(nodes)['签到成功'], '0')
        self.assertEqual(metrics(nodes)['登录成功'], '1')
        self.assertIn('无签到功能', texts(nodes))
        self.assertNotIn('奖励：20', texts(nodes))

    def test_running_card_is_read_only_and_does_not_include_raw_errors_or_credentials(self):
        host = Host({'1': {'success': False, 'message': 'private-error-token',
                           'cookie': 'private-cookie', 'username': 'private-user'}})
        original = deepcopy(host.data)
        host._run_lock = Lock()
        with host._run_lock:
            nodes = native_dashboard.render(host)
        self.assertIn('任务执行中', texts(nodes))
        self.assertNotIn('private-', json.dumps(nodes))
        self.assertEqual(host.data, original)
        self.assertEqual(host.get_data.call_count, 2)
        allowed = {'div', 'strong', 'VChip', 'VSpacer', 'VDivider', 'VAlert'}
        def check(children):
            for node in children:
                self.assertIn(node['component'], allowed)
                self.assertNotIn('html', node)
                self.assertFalse(any(key.lower().startswith('on') for key in node.get('props', {})))
                check(node.get('content', []))
        check(nodes)

    def test_real_host_response_contains_json_while_retaining_vue_pages(self):
        plugin = object.__new__(PTSignInKeeper)
        plugin.__dict__.update(Host({'1': {'success': True, 'bonus': '12 魔力'}}).__dict__)
        instances = {'PTSignInKeeper': plugin}
        try:
            from app.runtime.extensions.plugin.projection import PluginProjection
        except ModuleNotFoundError:
            from app.core.plugin import PluginManager
            response = PluginManager.get_plugin_dashboard(SimpleNamespace(running_plugins=instances),
                'PTSignInKeeper', 'ptsigninkeeper_dashboard', user_agent='MoviePilot for iOS/2.0.2')
        else:
            response = PluginProjection(instances).dashboard(
                'PTSignInKeeper', 'ptsigninkeeper_dashboard', user_agent='MoviePilot for iOS/2.0.2')
        result = json.loads(response.model_dump_json())
        self.assertEqual(result['render_mode'], 'vue')
        self.assertTrue(result['elements'])
        self.assertIn('奖励：12 魔力', texts(result['elements']))
        self.assertEqual(plugin.get_render_mode(), ('vue', 'dist/assets'))
