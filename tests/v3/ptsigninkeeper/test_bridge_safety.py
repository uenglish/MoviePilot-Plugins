"""Host-backed bridge tests with fake responses and no network requests."""
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from app.plugins.ptsigninkeeper.integration import SignInBridge, reward_text, row_result
from app.plugins.ptsigninkeeper.result import SiteResult


class BridgeSafetyTests(unittest.TestCase):
    def test_homepage_login_reports_transport_cause_without_request_url(self):
        from requests import exceptions
        from app.plugins.ptsigninkeeper import PTSignInKeeper
        site = {'id': 45, 'name': '幼儿园', 'url': 'https://u2.dmhy.org/', 'cookie': 'test=only', 'timeout': 5}
        bridge = self.bridge()
        login = PTSignInKeeper._PTSignInKeeper__login_base
        with patch('app.plugins.ptsigninkeeper.RequestUtils') as request:
            get = request.return_value.get_res
            for error, expected in [(exceptions.ReadTimeout, '超时设置 5 秒'), (exceptions.SSLError, 'TLS'),
                                    (exceptions.ProxyError, '代理'), (exceptions.ConnectionError, '连接失败'),
                                    (None, '未收到 HTTP 响应')]:
                with self.subTest(error=error):
                    get.side_effect = error('private-url') if error else None
                    get.return_value = None
                    result = bridge.perform(site, '登录', login)
                    self.assertFalse(result.logged_in)
                    self.assertIn(expected, result.message)
                    self.assertNotIn('private-url', result.message)
                    self.assertTrue(get.call_args.kwargs['raise_exception'])

    def test_yema_routes_to_current_adapter_and_respects_user_rule(self):
        from app.plugins.ptsigninkeeper.sites.yema import YemaPT
        bridge = self.bridge()
        info = {'id': 51, 'name': '野马', 'url': 'https://www.yemapt.org/', 'apikey': 'test'}
        self.assertEqual(bridge.route(info), ('sites', YemaPT))
        with patch.object(YemaPT, 'signin', return_value=(True, '签到成功，获得 0 积分')) as signin:
            result = bridge.perform(info, '签到', Mock())
        signin.assert_called_once()
        self.assertTrue(result.success)
        self.assertEqual(result.bonus, '0 积分')
        bridge.rules = {'yemapt.org': {'path': '/custom'}}
        self.assertEqual(bridge.route(info), ('pt', None))

    @staticmethod
    def bridge():
        bridge = SignInBridge({'site_rules': '', 'browserless_url': '', 'read_activity': False})
        bridge._enrich = Mock(side_effect=lambda value: value)
        return bridge

    def test_signin_module_without_login_does_not_validate_cookie(self):
        for url in ('https://hdsky.me/', 'https://u2.dmhy.org/', 'https://tjupt.org/'):
            bridge = self.bridge()
            bridge.engine._login_one = Mock(side_effect=AssertionError('unverified cookie accepted'))
            validate = Mock(return_value=(False, '登录失败：Cookie 已过期'))
            result = bridge.perform({'id': 11, 'name': '测试站', 'url': url, 'cookie': 'expired=dummy'},
                                    '登录', validate)
            self.assertFalse(result.logged_in)
            validate.assert_called_once()

    def test_hddolby_key_only_uses_preserved_account_adapter(self):
        bridge = self.bridge()
        route, adapter = bridge.route({'url': 'https://www.uhddolby.com/', 'apikey': 'dummy'})
        self.assertEqual(route, 'sites')
        self.assertEqual(adapter.__name__, 'HDDolby')
        self.assertFalse(adapter.signin_supported)
        self.assertEqual(bridge.route({'url': 'https://www.uhddolby.com/', 'cookie': 'dummy'})[0], 'pt')

    def test_explicit_user_rule_wins_over_legacy_adapter(self):
        bridge = self.bridge()
        bridge.rules = {'uhddolby.com': {'auth': 'api_key', 'path': '/custom'}}
        self.assertEqual(bridge.route({'url': 'https://www.uhddolby.com/', 'apikey': 'dummy'})[0], 'pt')

    def test_failed_signin_with_real_login_still_needs_retry(self):
        result = row_result({'site': '测试', 'status': 'failed', 'message': '验证码错误', 'logged_in': True}, '签到')
        self.assertTrue(result.logged_in)
        self.assertFalse(result.success)
        self.assertFalse(result.completed)
        self.assertTrue(result.signin_supported)

    def test_explicit_no_signin_does_not_claim_signin_success(self):
        result = row_result({'site': '测试', 'status': 'nosign', 'logged_in': True}, '签到')
        self.assertTrue(result.completed)
        self.assertFalse(result.success)
        self.assertFalse(result.signin_supported)
        adapter = type('NoSign', (), {'signin_supported': False,
                                    'signin': lambda self, info: (True, '站点无签到功能，账号校验通过')})
        legacy = SignInBridge._legacy_result({'name': '测试'}, '签到', adapter)
        self.assertTrue(legacy.completed)
        self.assertFalse(legacy.success)
        self.assertFalse(legacy.signin_supported)

    def test_reward_normalization_preserves_amount_and_unit(self):
        for value, expected in ((0, '0'), ('获得 20 憨豆', '20 憨豆'), ('获得 30 金贝', '30 金贝'),
                                ('获得 100 做种积分', '100 做种积分'), ('魔力值：50', '50 魔力值'),
                                ('获得 30 粒爆米花', '30 爆米花')):
            self.assertEqual(reward_text(value), expected)
        self.assertEqual(reward_text(False), '')

    def test_reload_cannot_overlap_inflight_site(self):
        old, new = self.bridge(), self.bridge()
        entered, release = Event(), Event()
        def perform(info, action, adapter):
            entered.set()
            release.wait(5)
            return SiteResult('测试', '签到成功', True, True)
        old.route = Mock(return_value=('sites', object))
        old._legacy_result = perform
        info = {'id': 987654, 'name': '测试', 'url': 'https://example.test/'}
        worker = Thread(target=old.perform, args=(info, '签到', Mock()))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            old.stop()
            new.route = Mock(side_effect=AssertionError('overlapping request'))
            result = new.perform(info, '签到', Mock())
            self.assertFalse(result.success)
            self.assertIn('前次任务仍在执行', result.message)
        finally:
            release.set()
            worker.join(5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
