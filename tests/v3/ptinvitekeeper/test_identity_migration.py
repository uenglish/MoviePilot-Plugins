"""独立插件迁移的幂等性、数据保护和一次性操作回归。"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from app.plugins.ptinvitekeeper.identity_migration import migrate_identity, MARKER


class Host:
    """隔离插件数据和配置，不访问宿主数据库。"""
    def __init__(self, root):
        self.root = Path(root)
        self.config = {}
        self.legacy_config = {'enabled': True, 'onlyonce': True, 'clean': True, 'sign_sites': ['all']}
        self.values = {}
        self.legacy_values = {'签到-2026-11-18': {'results': {'1': {'success': True}}}}

    def get_config(self, plugin_id=None):
        return deepcopy(self.legacy_config if plugin_id else self.config)

    def update_config(self, config):
        self.config = deepcopy(config)

    def get_data(self, key=None, plugin_id=None):
        source = self.legacy_values if plugin_id else self.values
        if key is None:
            return [SimpleNamespace(key=k, value=v) for k, v in source.items()]
        return deepcopy(source.get(key))

    def save_data(self, key, value):
        self.values[key] = deepcopy(value)

    def get_data_path(self, plugin_id=None):
        path = self.root / ('old' if plugin_id else 'new')
        path.mkdir(exist_ok=True)
        return path


class IdentityTests(unittest.TestCase):
    """覆盖重复启动、显式配置、损坏缓存和空历史。"""
    def test_copy_once_retains_source_and_disarms_inherited_actions(self):
        with TemporaryDirectory() as root:
            host = Host(root)
            original = deepcopy(host.legacy_values)
            out = migrate_identity(host, {}, 'Legacy')
            self.assertTrue(out['enabled'])
            self.assertFalse(out['onlyonce'])
            self.assertFalse(out['clean'])
            self.assertEqual(original, host.legacy_values)
            host.values['签到-2026-11-18'] = {'new': True}
            migrate_identity(host, host.config, 'Legacy')
            self.assertEqual(host.values['签到-2026-11-18'], {'new': True})

    def test_explicit_configuration_and_zero_records_win(self):
        with TemporaryDirectory() as root:
            host = Host(root)
            host.values['签到-2026-11-18'] = {}
            out = migrate_identity(host, {'enabled': False, 'onlyonce': True, 'sign_sites': []}, 'Legacy')
            self.assertFalse(out['enabled'])
            self.assertTrue(out['onlyonce'])
            self.assertEqual(out['sign_sites'], [])
            self.assertEqual(host.values['签到-2026-11-18'], {})

    def test_invalid_cache_is_not_marked_and_can_be_retried(self):
        with TemporaryDirectory() as root:
            host = Host(root)
            cache = host.get_data_path('Legacy') / 'site_data.json'
            cache.write_text('bad json')
            with self.assertRaises(ValueError):
                migrate_identity(host, {}, 'Legacy')
            self.assertNotIn(MARKER, host.values)
            self.assertFalse((host.get_data_path() / 'site_data.json').exists())
            cache.write_text('{"站点": {"count": 0}}')
            migrate_identity(host, {}, 'Legacy')
            self.assertEqual((host.get_data_path() / 'site_data.json').read_text(), cache.read_text())
            self.assertIn(MARKER, host.values)
