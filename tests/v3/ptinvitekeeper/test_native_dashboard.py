"""Native invitation cards expose bounded local summaries, never member identities."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.plugins.ptinvitekeeper import PTInviteKeeper, native_dashboard, ui
from test_ui import fixture, record


def texts(nodes):
    return '\n'.join(str(node.get('text', '')) + '\n' + texts(node.get('content', []))
                     for node in nodes)


def metrics(nodes):
    return {node['content'][1]['text']: node['content'][0]['text']
            for node in nodes[0]['content']}


class NativeDashboardTests(unittest.TestCase):
    def test_registered_key_and_empty_state_only_read_local_data(self):
        host = fixture()
        host.data_manager.get_site_data = Mock(return_value={})
        self.assertEqual(PTInviteKeeper.get_dashboard_meta(host)[0]['key'], 'ptinvitekeeper_dashboard')
        self.assertIsNone(PTInviteKeeper.get_dashboard(host, 'unknown'))
        host.data_manager.get_site_data.assert_not_called()
        cols, attrs, nodes = PTInviteKeeper.get_dashboard(host, 'ptinvitekeeper_dashboard')
        self.assertEqual(cols, {'cols': 12, 'md': 6})
        self.assertEqual(attrs['refresh'], 3600)
        self.assertEqual(nodes[0]['component'], 'VAlert')
        self.assertIn('暂无邀请数据', texts(nodes))
        host.data_manager.get_site_data.assert_called_once_with()

    def test_global_totals_survive_three_site_preview_and_member_privacy(self):
        members = [{'username': 'private-user', 'email': 'private@example.test',
                    'cookie': 'private-cookie', 'ratio': '2'} for _ in range(200)]
        records = {f'Site {i:03}': record(quota=2, members=members if i == 0 else []) for i in range(120)}
        original = deepcopy(records)
        nodes = native_dashboard.render(fixture(records))
        self.assertEqual(metrics(nodes)['站点记录'], '120')
        self.assertEqual(metrics(nodes)['已知持有名额'], '240')
        self.assertIn('成员记录 200', texts(nodes))
        self.assertIn('展示 3 / 120 站', texts(nodes))
        self.assertEqual(sum(node['component'] == 'VDivider' for node in nodes), 3)
        self.assertNotIn('private', json.dumps(nodes))
        self.assertLess(len(json.dumps(nodes, ensure_ascii=False)), 6500)
        self.assertEqual(records, original)

    def test_zero_and_unknown_quota_are_not_interchangeable(self):
        unknown = {'data': {'invite_status': {'can_invite': False, 'reason': '站点服务异常'}}}
        nodes = native_dashboard.render(fixture({'未知': unknown, '零名额': record(quota=0)}))
        output = texts(nodes)
        self.assertIn('剩余名额：未知', output)
        self.assertIn('剩余名额：0', output)
        self.assertEqual(metrics(nodes)['已知持有名额'], '0')

    def test_failed_refresh_displays_previous_values_but_excludes_them_from_totals(self):
        failed = record(quota=99, can_invite=False, reason='private-error',
                        members=[{'username': 'private-user', 'ratio': '0.1'}])
        failed['data'].update(fetch_failed=True, last_success_at=1000, error='private-error')
        nodes = native_dashboard.render(fixture({'失败站': failed, '正常站': record(quota=2)}))
        self.assertEqual(metrics(nodes)['已知持有名额'], '2')
        self.assertEqual(metrics(nodes)['可邀请'], '1')
        self.assertIn('刷新失败', texts(nodes))
        self.assertIn('上次记录名额：99', texts(nodes))
        self.assertIn('成员记录 0', texts(nodes))
        self.assertIn('旧记录不计入汇总', texts(nodes))
        self.assertNotIn('private', json.dumps(nodes))

    def test_stale_and_in_progress_messages_use_saved_record(self):
        host = fixture({'旧站': record(updated=1)})
        host._refreshing = True
        with patch.object(ui.time, 'time', return_value=100000):
            output = texts(native_dashboard.render(host))
        self.assertIn('正在刷新，以下为已保存的结果', output)
        self.assertIn('数据较旧', output)

    def test_update_timestamp_uses_host_timezone_and_handles_missing_dates(self):
        with patch.object(native_dashboard.settings, 'TZ', 'Asia/Shanghai'):
            output = texts(native_dashboard.render(fixture({'站点': record(updated=1000)})))
        self.assertIn('最近成功记录：01-01 08:16', output)
        self.assertIn('尚无成功更新时间', texts(native_dashboard.render(fixture({'站点': record(updated=None)}))))
        self.assertEqual(native_dashboard._updated(float('inf')), '成功更新时间未知')

    def test_real_host_response_contains_json_while_retaining_vue_pages(self):
        plugin = object.__new__(PTInviteKeeper)
        plugin.__dict__.update(vars(fixture({'站点': record(quota=5)})))
        instances = {'PTInviteKeeper': plugin}
        try:
            from app.runtime.extensions.plugin.projection import PluginProjection
        except ModuleNotFoundError:
            from app.core.plugin import PluginManager
            response = PluginManager.get_plugin_dashboard(SimpleNamespace(running_plugins=instances),
                'PTInviteKeeper', 'ptinvitekeeper_dashboard', user_agent='MoviePilot for iOS/2.0.2')
        else:
            response = PluginProjection(instances).dashboard(
                'PTInviteKeeper', 'ptinvitekeeper_dashboard', user_agent='MoviePilot for iOS/2.0.2')
        result = json.loads(response.model_dump_json())
        self.assertEqual(result['render_mode'], 'vue')
        self.assertTrue(result['elements'])
        self.assertIn('剩余名额：5', texts(result['elements']))
        self.assertEqual(plugin.get_render_mode(), ('vue', 'dist/assets'))
