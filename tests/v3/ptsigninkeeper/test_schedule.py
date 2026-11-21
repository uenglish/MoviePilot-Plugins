"""Actual scheduler dispatch and U2 cycle-aware daily deduplication."""
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from test_merge import plugin_fixture, NS
from app.plugins.ptsigninkeeper import PTSignInKeeper, ui
from app.plugins.ptsigninkeeper.result import SiteResult
from app.plugins.ptsigninkeeper.ptengine.sites import u2


class ScheduleTests(TestCase):
    def setUp(self):
        self.plugin, self.memory = plugin_fixture()
        self.plugin._bridge = None
        self.site = {'id': 1, 'name': '测试站', 'url': 'https://example.invalid/'}
        self.config = {'enabled': True, 'onlyonce': True, 'notify': False, 'cron': '1 9 * * *',
                       'sign_sites': [1], 'login_sites': [], 'retry_time': '', 'retry_count': 0,
                       'consolidation_version': 1}
        for target, value in (
            ('.SiteOper', SimpleNamespace(list_order_by_pri=lambda: [SimpleNamespace(id=1)])),
            ('.SitesHelper', SimpleNamespace(get_indexers=lambda: [self.site]))):
            item = patch(NS + target, return_value=value)
            item.start(); self.addCleanup(item.stop)
        self.addCleanup(self.plugin.stop_service)

    def test_save_dispatches_real_one_shot_job_and_resets_flag(self):
        started = Event()
        def signin(site):
            started.set()
            return SiteResult(site['name'], '签到成功', True, True)
        self.plugin.signin_site = Mock(side_effect=signin)
        self.plugin.init_plugin(self.config)
        self.assertTrue(started.wait(7), 'saved one-shot job was not dispatched')
        self.assertFalse(self.plugin.update_config.call_args.args[0]['onlyonce'])
        self.plugin.signin_site.assert_called_once()

    def test_empty_selection_schedules_nothing_and_explains_why(self):
        with patch(NS + '.logger') as logger:
            self.plugin.init_plugin({**self.config, 'sign_sites': []})
        logger.warning.assert_called_once()
        self.assertIn('未选择签到或登录站点', logger.warning.call_args.args[0])
        self.assertIsNone(self.plugin._scheduler)
        self.assertEqual(self.plugin.get_service(), [])
        self.assertFalse(self.plugin._allow_once)

    def test_saved_string_ids_are_not_silently_discarded(self):
        self.plugin.init_plugin({**self.config, 'sign_sites': ['1'], 'onlyonce': False})
        self.assertEqual(self.plugin._sign_sites, [1])

    def test_daily_0901_uses_configured_timezone_and_clears_old_hour_window(self):
        self.plugin._enabled = True
        self.plugin._cron = '1 9 * * *'
        self.plugin._start_time, self.plugin._end_time = 10, 20
        with patch(NS + '.settings.TZ', 'Asia/Shanghai'):
            trigger = self.plugin.get_service()[0]['trigger']
        now = datetime(2026, 11, 21, 0, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertEqual(trigger.get_next_fire_time(None, now).isoformat(), '2026-11-21T09:01:00+08:00')
        self.assertIsNone(self.plugin._start_time)
        self.assertIsNone(self.plugin._end_time)

    def test_u2_midnight_pending_runs_again_after_opening_and_then_deduplicates(self):
        self.site.update(url=u2.BASE, name='幼儿园')
        self.plugin._bridge = SimpleNamespace(stopped=False, stop=Mock())
        self.plugin._login_sites = []
        self.plugin._extra_config = {'retry_count': 0}
        now = datetime(2026, 11, 21, 0, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        page = '<a href="logout.php">退出</a><p>今天已签到</p><form><input type="date" name="date" value="2026-11-21"></form>'
        ctx = SimpleNamespace(page=Mock(return_value=page), post=Mock())
        def signin(site):
            ok, message = u2.U2().signin(site, ctx)
            return SiteResult(site['name'], message, ok, ok)
        self.plugin.signin_site = Mock(side_effect=signin)
        with patch.object(u2, 'current_time', return_value=now):
            self.plugin.sign_in()
        ctx.page.assert_not_called()
        with patch.object(u2, 'current_time', return_value=now.replace(hour=9, minute=1)):
            self.plugin.sign_in()
            self.plugin.sign_in()
        self.assertEqual(self.plugin.signin_site.call_count, 2)
        ctx.page.assert_called_once()
        ctx.post.assert_not_called()
        records = [v for k, v in self.memory.data.items() if k.startswith('签到-')]
        self.assertTrue(records[0]['results']['1']['success'])
        self.assertEqual(records[0]['results']['1']['signin_day'], '2026-11-21')

    def test_old_u2_success_is_rechecked_without_erasing_login_evidence(self):
        site = {'url': u2.BASE}
        old = {'success': True, 'logged_in': True, 'message': '已签到', 'bonus': '10 UCoin'}
        self.assertFalse(PTSignInKeeper._action_completed(old, '签到', site))
        self.assertTrue(PTSignInKeeper._action_completed(old, '登录', site))
        sign, login = ui._view_records(site, old, {})
        self.assertFalse(sign['success'])
        self.assertTrue(sign['unconfirmed'])
        self.assertTrue(login['success'])
        self.assertTrue(old['success'])
