"""在隔离的 V3 宿主中验证后宫合并；全部站点请求使用替身。"""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.plugins.ptinvitekeeper import PTInviteKeeper
from app.plugins.ptinvitekeeper import categories, trend
from app.plugins.ptinvitekeeper.data import DataManager
from app.plugins.ptinvitekeeper.migration import merge_config, merge_data
from app.plugins.ptinvitekeeper.module_loader import ModuleLoader
from app.plugins.ptinvitekeeper.parsing import extract_invite_reason, sanitize_invitees
from app.plugins.ptinvitekeeper.site_access import classify, COOKIE_EXPIRED_REASON, detect_schema
from app.sdk.plugin import _PluginBase


class ConfigTests(unittest.TestCase):
    def test_preserves_enhanced_preferences_and_unions_active_legacy_sites(self):
        current = {"enabled": True, "cron": "0 8 * * *", "site_ids": [1], "page_filter": "A", "no_browser_sites": ["B"]}
        legacy = {"enabled": True, "cron": "0 9 * * *", "site_ids": ["2", 1], "onlyonce": True}
        value = merge_config(current, legacy)
        self.assertEqual(value["site_ids"], [1, 2])
        self.assertEqual(value["cron"], "0 8 * * *")
        self.assertEqual(value["no_browser_sites"], ["B"])
        self.assertNotIn("onlyonce", value)
        self.assertEqual(current["site_ids"], [1])
        value["site_ids"] = [4]
        self.assertEqual(merge_config(value, legacy)["site_ids"], [4])

    def test_disabled_legacy_does_not_expand_sites(self):
        self.assertEqual(merge_config({"site_ids": [1]}, {"enabled": False, "site_ids": [2]})["site_ids"], [1])

    def test_empty_legacy_selection_means_all_and_onlyonce_is_not_replayed(self):
        self.assertEqual(merge_config({"site_ids": [1]}, {"enabled": True, "site_ids": []})["site_ids"], ["all"])
        self.assertFalse(merge_config({}, {"enabled": True, "onlyonce": True})["onlyonce"])

    def test_host_contract_is_not_overridden_by_http_handlers(self):
        self.assertIs(PTInviteKeeper.get_config, _PluginBase.get_config)
        self.assertIs(PTInviteKeeper.update_config, _PluginBase.update_config)
        plugin = object.__new__(PTInviteKeeper)
        routes = {row["path"]: row["endpoint"].__name__ for row in plugin.get_api()}
        self.assertEqual(routes["/config"], "api_get_config")
        self.assertEqual(routes["/update_config"], "api_update_config")


class DataTests(unittest.TestCase):
    def test_newest_site_wins_ties_keep_enhanced_and_originals_are_archived(self):
        with TemporaryDirectory() as root:
            target, old = Path(root) / "enhanced", Path(root) / "ordinary"
            target.mkdir(); old.mkdir()
            enhanced = {"newer": {"last_update": 20, "data": {"custom": True}}, "tie": {"last_update": 10, "data": {"enhanced": True}}}
            legacy = {"newer": {"last_update": 10}, "tie": {"last_update": 10}, "added": {"last_update": 30}}
            for folder, data, history in [(target, enhanced, [{"time": 2, "sites": {}}]), (old, legacy, [{"time": 1, "sites": {}}, {"time": 2, "sites": {}}])]:
                (folder / "site_data.json").write_text(json.dumps(data))
                (folder / "history.json").write_text(json.dumps(history))
            merge_data(target, old)
            actual = json.loads((target / "site_data.json").read_text())
            self.assertEqual(actual["newer"], enhanced["newer"])
            self.assertEqual(actual["tie"], enhanced["tie"])
            self.assertEqual(actual["added"], legacy["added"])
            self.assertEqual([row["time"] for row in json.loads((target / "history.json").read_text())], [1, 2])
            self.assertEqual(json.loads((target / "legacy-nexusinvitee-v3/ordinary-site_data.json").read_text()), legacy)
            self.assertEqual(json.loads((target / "legacy-nexusinvitee-v3/enhanced-site_data.json").read_text()), enhanced)
            snapshot = (target / "site_data.json").read_bytes()
            (old / "site_data.json").write_text('{}')
            merge_data(target, old)
            self.assertEqual((target / "site_data.json").read_bytes(), snapshot)

    def test_newer_ordinary_record_is_used(self):
        with TemporaryDirectory() as root:
            target, old = Path(root) / "enhanced", Path(root) / "ordinary"
            target.mkdir(); old.mkdir()
            (target / "site_data.json").write_text('{"site":{"last_update":1}}')
            (old / "site_data.json").write_text('{"site":{"last_update":2}}')
            merge_data(target, old)
            self.assertEqual(json.loads((target / "site_data.json").read_text())["site"]["last_update"], 2)

    def test_invalid_source_does_not_overwrite_target_or_mark_complete(self):
        with TemporaryDirectory() as root:
            target, old = Path(root) / "enhanced", Path(root) / "ordinary"
            target.mkdir(); old.mkdir()
            (target / "site_data.json").write_text('{"site": {}}')
            (old / "site_data.json").write_text('broken')
            with self.assertRaises(ValueError):
                merge_data(target, old)
            self.assertEqual((target / "site_data.json").read_text(), '{"site": {}}')
            self.assertFalse((target / "consolidation-v3.json").exists())


class RuntimeTests(unittest.TestCase):
    def test_concurrent_refresh_cannot_release_owner_state(self):
        plugin = object.__new__(PTInviteKeeper)
        plugin._refresh_lock = Lock()
        plugin._refreshing = True
        plugin._refresh_selected_sites = Mock(side_effect=AssertionError("concurrent request"))
        plugin._refresh_lock.acquire()
        result = plugin.refresh_all_sites()
        self.assertEqual(result["message"], "刷新已在进行中")
        self.assertTrue(plugin._refreshing)
        self.assertTrue(plugin._refresh_lock.locked())

    def test_empty_explicit_scope_makes_no_site_requests(self):
        plugin = object.__new__(PTInviteKeeper)
        plugin._refresh_lock = Lock()
        plugin._refreshing = False
        plugin._all_sites = True
        plugin.sites = SimpleNamespace(get_indexers=lambda: [{"id": 1, "name": "test"}])
        plugin._get_site_invite_data = Mock(side_effect=AssertionError("unexpected request"))
        with patch.object(ModuleLoader, "load_site_handlers", return_value=[]):
            result = plugin.refresh_all_sites([])
        self.assertEqual(result["success"], 0)
        self.assertFalse(plugin._refresh_lock.locked())

    def test_init_uses_v3_configuration_and_keeps_migration_marker(self):
        with TemporaryDirectory() as root:
            plugin = object.__new__(PTInviteKeeper)
            plugin._refresh_lock = Lock()
            plugin._extra_config = {}
            configs = {"nexusinvitee": {"enabled": True, "site_ids": [2], "onlyonce": True}}
            saved = []
            plugin._plugininstance = SimpleNamespace(get_config_data=lambda key: deepcopy(configs.get(key)), save_config_data=lambda **kw: saved.append(kw))
            plugin.plugindata = SimpleNamespace(get_data=lambda plugin_id, key: None if key else [], save=lambda *args: None)
            plugin.systemconfig = SimpleNamespace(get=lambda key: None)
            plugin.get_data_path = lambda plugin_id=None: Path(root) / (plugin_id or "PTInviteKeeper")
            plugin._PTInviteKeeper__reload_modules = Mock()
            with patch("app.plugins.ptinvitekeeper.SitesHelper", return_value=SimpleNamespace(get_indexers=lambda: [])), patch("app.plugins.ptinvitekeeper.SiteOper"), patch.object(ModuleLoader, "load_site_handlers", return_value=[]):
                plugin.init_plugin({"enabled": False, "site_ids": [1], "page_filter": "test"})
            self.assertFalse(plugin._enabled)
            self.assertEqual(plugin._nexus_sites, [1, 2])
            self.assertEqual(saved[-1]["config_data"]["consolidation_version"], 1)
            self.assertFalse(plugin._onlyonce)

    def test_all_ordinary_and_enhanced_handlers_are_available(self):
        names = {handler.__name__ for handler in ModuleLoader.load_site_handlers()}
        self.assertTrue({"NexusPhpHandler", "ButterflyHandler", "GazelleHandler", "TtgHandler", "Unit3dHandler", "HdkylinHandler", "MTeamHandler"}.issubset(names))
        self.assertIn("HxptHandler", names)
        self.assertEqual(len(names), 12)


class ParserPreservationTests(unittest.TestCase):
    def test_member_cleanup_and_enhanced_reason_extraction(self):
        rows = sanitize_invitees([{"username": "用户名"}, {"username": "member", "ratio": "10 GiB", "uploaded": "10 GiB", "downloaded": "5 GiB"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ratio"], "2.000")
        self.assertEqual(extract_invite_reason('<input disabled value="邀請數量不足">'), "邀請數量不足")

    def test_access_status_does_not_misreport_logged_in_page(self):
        self.assertIsNone(classify(SimpleNamespace(status_code=200, url="https://example.test/login.php", text='<a href="logout.php">退出</a>')))
        self.assertEqual(classify(SimpleNamespace(status_code=200, url="https://example.test/login.php", text='<input name="password">')), COOKIE_EXPIRED_REASON)
        self.assertEqual(detect_schema('', 'https://yemapt.org'), 'yema')

    def test_category_and_trend_remain_available(self):
        record = {"data": {"invite_status": {"can_invite": False, "reason": "等级不足"}, "invitees": []}, "last_update": 1}
        self.assertEqual(categories.classify(record)["key"], "level")
        snapshot = trend.build_snapshot({"test": record}, categories.classify, 1)
        self.assertEqual(snapshot["sites"]["test"]["category"], "level")


if __name__ == '__main__':
    unittest.main(verbosity=2)
