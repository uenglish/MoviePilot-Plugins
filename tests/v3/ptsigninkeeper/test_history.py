"""Regression coverage for receipts predating the consolidated result schema."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from app.plugins.ptsigninkeeper.integration import migrate_history
from app.plugins.ptsigninkeeper.dashboard import collect_rows, _status


class HistoryHost:
    def __init__(self, data=None, sources=None, names=None, selected=None):
        self.data = deepcopy(data or {})
        self.sources = deepcopy(sources or {})
        self.names = names or {}
        self._sign_sites = selected or []
        self._login_sites = []

    def get_data(self, key=None, plugin_id=None):
        data = self.sources.get(plugin_id, {}) if plugin_id else self.data
        return deepcopy(data.get(key)) if key is not None else [
            SimpleNamespace(key=k, value=deepcopy(v)) for k, v in data.items()]

    def save_data(self, key, value):
        self.data[key] = deepcopy(value)

    def _build_sites_info(self):
        return self.names

    def _resolve_site_ids(self, selected):
        return selected


class HistoricalReceiptsTests(unittest.TestCase):
    now = datetime(2026, 1, 3, 12, 0)

    def test_enhanced_receipts_remain_visible_across_year_and_block_duplicate(self):
        host = HistoryHost(data={
            '1月3日': [{'site': '增强站', 'status': '今日已签到'}],
            '签到-2026-01-03': {'do': [1], 'retry': []},
            '12月31日': [{'site': '增强站', 'status': '签到成功，获得 12 魔力'},
                        {'site': '增强站', 'status': '模拟登录成功'}],
        }, names={1: '增强站'}, selected=[1])
        migrate_history(host, self.now)
        today = host.data['签到-2026-01-03']['results']['1']
        self.assertTrue(today['success'])
        self.assertEqual(host.data['签到-2026-01-03']['retry'], [])
        old = host.data['签到-2025-12-31']['results']['1']
        self.assertEqual(old['bonus'], '12 魔力')
        rows = collect_rows(host, self.now)
        self.assertEqual(len(rows), 1)
        old_row = next(r for r in rows[0]['history'] if r['day'] == '2025-12-31')
        self.assertTrue(old_row['login']['success'])

    def test_explicit_site_id_wins_over_duplicate_names(self):
        host = HistoryHost(data={'1月3日': [
            {'site_id': 2, 'site': '同名站', 'status': '签到成功'},
            {'site': '同名站', 'status': '签到成功，获得 3 魔力'},
        ]}, names={1: '同名站', 2: '同名站'}, selected=[1, 2])
        migrate_history(host, self.now)
        records = host.data['签到-2026-01-03']['results']
        self.assertTrue(records['2']['success'])
        self.assertNotIn('1', records)
        self.assertIn('legacy-name:同名站', records)
        self.assertNotIn('legacy-name:同名站', host.data['签到-2026-01-03']['do'])
        self.assertEqual(len(collect_rows(host, self.now)), 3)

    def test_ordinary_receipts_merge_without_overwriting_enhanced(self):
        host = HistoryHost(data={'签到-2026-01-03': {'results': {'1': {
            'success': True, 'message': '增强版确认成功', 'bonus': '8 魔力'}}}}, sources={
            'AutoSignIn': {'1月3日': [{'site': '站点', 'status': '签到失败，未检测到签到成功'}],
                           '签到-2026-01-03': {'do': [1], 'retry': [1]}}}, names={1: '站点'})
        migrate_history(host, self.now)
        result = host.data['签到-2026-01-03']['results']['1']
        self.assertEqual(result['message'], '增强版确认成功')
        self.assertEqual(result['bonus'], '8 魔力')
        self.assertIn('legacy:AutoSignIn:1月3日', host.data)
        self.assertEqual(host.data['签到-2026-01-03']['retry'], [])

    def test_attempts_without_receipts_are_not_reported_success(self):
        host = HistoryHost(data={'签到-2026-01-03': {'do': [1], 'retry': []}}, names={1: '站点'})
        migrate_history(host, self.now)
        result = host.data['签到-2026-01-03']['results']['1']
        self.assertFalse(result['success'])
        self.assertEqual(_status(result, '签到'), ('结果未确认', 'grey'))

    def test_pt_daily_histories_keep_previous_days_and_rewards(self):
        host = HistoryHost(sources={'PTSiteSignIn': {
            'latest': [{'site': 'PT站点', 'site_id': 3, 'ran_at': '2026-01-03 00:06:00',
                        'status': 'already', 'message': '已签到'}],
            'history_20260101': [{'site': 'PT站点', 'site_id': 3, 'ran_at': '2026-01-01 00:06:00',
                                 'status': 'success', 'message': '签到成功，获得 20 魔力',
                                 'bonus': '获得 20 魔力', 'dual': True, 'login_status': 'login',
                                 'login': '成功（已换用新 Cookie）', 'activity': '刚刚'}],
        }}, names={3: 'PT站点'})
        migrate_history(host, self.now)
        self.assertEqual(host.data['签到-2026-01-01']['results']['3']['bonus'], '20 魔力')
        self.assertTrue(host.data['登录-2026-01-01']['results']['3']['success'])
        self.assertEqual(len(collect_rows(host, self.now)[0]['history']), 2)

    def test_old_marker_repairs_and_repeated_migration_is_idempotent(self):
        host = HistoryHost(data={'consolidation_history_v1': True,
                                 '1月3日': [{'site': '站点', 'status': '签到成功'}]}, names={1: '站点'})
        migrate_history(host, self.now)
        self.assertTrue(host.data['签到-2026-01-03']['results']['1']['success'])
        snapshot = deepcopy(host.data)
        migrate_history(host, self.now)
        self.assertEqual(host.data, snapshot)

    def test_historical_unselected_site_keeps_saved_name(self):
        host = HistoryHost(data={'12月31日': [
            {'site_id': 9, 'site': '已删除站点', 'type': '签到', 'status': '签到成功'}]})
        migrate_history(host, self.now)
        rows = collect_rows(host, self.now)
        self.assertEqual(rows[0]['name'], '已删除站点')
        self.assertEqual(rows[0]['sign'], {})
        self.assertEqual(rows[0]['history'][0]['day'], '2025-12-31')

    def test_zero_reward_and_false_positive_failure_are_preserved(self):
        host = HistoryHost(data={'1月3日': [
            {'site_id': 1, 'site': '零奖励', 'status': '签到成功', 'success': True, 'bonus': '0'},
            {'site_id': 2, 'site': '失败站', 'status': '未检测到签到成功，获得 10 魔力'},
        ]})
        migrate_history(host, self.now)
        rows = {r['id']: r for r in collect_rows(host, self.now)}
        self.assertEqual(rows['1']['bonus'], '0')
        self.assertFalse(rows['2']['sign']['success'])
        self.assertEqual(rows['2']['bonus'], '—')


if __name__ == '__main__':
    unittest.main()
