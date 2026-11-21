"""Ordinary HTTP fallback and strict receipts, using host imports and fake I/O only."""
import unittest
from unittest.mock import Mock, patch

from app.plugins.ptsigninkeeper.integration import SignInBridge
from app.plugins.ptsigninkeeper.ptengine import core
from app.plugins.ptsigninkeeper.sites.audiences import Audiences
from app.plugins.ptsigninkeeper.sites.tangpt import TangPT
from app.plugins.ptsigninkeeper.sites.nexusphp_attendance import NexusPhpAttendance


SITE = {'id': 1, 'name': '躺平', 'url': 'https://tangpt.top/', 'cookie': 'test=only'}
SIGNED = ('<a href="logout.php">退出</a><aside>魔力值：99,999</aside>'
          '<h2>签到成功</h2><div>本次签到获得 <b>25.5</b> 个魔力值</div>')


class LocalReceiptTests(unittest.TestCase):
    def test_missing_entry_is_temporary_and_does_not_imply_login_or_completion(self):
        with patch.object(NexusPhpAttendance, 'request_html', return_value=(404, 'Not Found')):
            success, message = NexusPhpAttendance.ordinary_receipt(SITE)
            self.assertFalse(success)
            self.assertIn('签到入口暂不可用', message)
            self.assertIn('后续任务会重新检查', message)
            success, message = NexusPhpAttendance.ordinary_receipt(SITE, action='登录')
            self.assertFalse(success)
            self.assertNotIn('签到入口', message)

    def test_hidden_text_instructions_and_cumulative_days_are_not_success(self):
        for html in (
            '<script>签到成功</script>', '<div hidden>已经签到</div>',
            '<template>本次签到获得 999 魔力</template>',
            '<div>签到成功即可获得魔力</div><button>签到</button>',
            '<div>签到成功即可获得魔力</div>',
            '<div>已连续签到 123 天</div>', '<div>未检测到签到成功</div>',
            '<div aria-hidden="true">已签到</div>',
        ):
            with self.subTest(html=html):
                self.assertFalse(NexusPhpAttendance.is_signed(html))
                self.assertEqual(NexusPhpAttendance.signed_message(html), '')

    def test_visible_reward_reaches_bridge_without_profile_balance(self):
        html = SIGNED + '<script>获得 888 魔力值</script>'
        with patch.object(NexusPhpAttendance, 'request_html', return_value=(200, html)):
            result = SignInBridge._legacy_result(SITE, '签到', TangPT)
        self.assertTrue(result.success)
        self.assertEqual(result.bonus, '25.5 魔力值')
        self.assertNotIn('99,999', result.message)
        self.assertNotIn('888', result.message)

    def test_balance_only_success_has_no_invented_reward(self):
        html = '<h2>签到成功</h2><aside>魔力值：99,999</aside>'
        with patch.object(NexusPhpAttendance, 'request_html', return_value=(200, html)):
            result = SignInBridge._legacy_result(SITE, '签到', Audiences)
        self.assertTrue(result.success)
        self.assertEqual(result.bonus, '')

    def test_already_signed_does_not_resubmit(self):
        html = '<a href="logout.php">退出</a><p>您今天已经签到过了，请勿重复刷新。</p>'
        with patch.object(NexusPhpAttendance, 'request_html', return_value=(200, html)) as http:
            result = TangPT().signin(SITE)
        self.assertEqual(result, (True, '今日已签到'))
        http.assert_called_once()

    def test_human_verification_stops_after_one_ordinary_request(self):
        with patch.object(NexusPhpAttendance, 'request_html', return_value=(200,
                          '<div class="cf-turnstile">人机验证中</div>')) as http:
            success, message = TangPT().signin(SITE)
        self.assertFalse(success)
        self.assertIn('人工验证', message)
        http.assert_called_once()

    def test_tangpt_matches_only_its_host(self):
        self.assertTrue(TangPT.match('https://tangpt.top/'))
        self.assertTrue(TangPT.match('https://www.tangpt.top/'))
        self.assertFalse(TangPT.match('https://tangpt.top.evil.test/'))
        self.assertFalse(TangPT.match('https://other.test/tangpt.top'))


class LocalFallbackTests(unittest.TestCase):
    def bridge(self, marker=True):
        bridge = SignInBridge({'site_rules': '', 'browserless_url': '', 'read_activity': False})
        bridge._enrich = Mock(side_effect=lambda info: info)
        bridge._browser = Mock(return_value=None)
        bridge.engine._sign_one = Mock(return_value={
            'site': '躺平', 'status': 'failed', 'engine': 'browserless',
            'message': 'Browserless 额度不足', 'browserless_not_started': marker})
        return bridge

    def test_confirmed_unstarted_preflight_falls_back_once_and_keeps_reward(self):
        bridge = self.bridge()
        with patch.object(NexusPhpAttendance, 'request_html', return_value=(200, SIGNED)) as http:
            result = bridge.perform(SITE, '签到', Mock())
        self.assertTrue(result.success)
        self.assertEqual(result.engine, '站点 HTTP')
        self.assertEqual(result.bonus, '25.5 魔力值')
        http.assert_called_once()
        bridge.engine._sign_one.assert_called_once()

    def test_message_alone_and_possible_submission_never_trigger_fallback(self):
        for marker in (False, None, 'true'):
            with self.subTest(marker=marker):
                bridge = self.bridge(marker)
                with patch.object(bridge, '_adapter', side_effect=AssertionError('must not rerun')):
                    result = bridge.perform(SITE, '签到', Mock())
                self.assertFalse(result.success)

    def test_custom_rules_preserve_explicit_route(self):
        bridge = self.bridge()
        bridge.rules = {'tangpt.top': {'engine': 'browserless'}}
        with patch.object(bridge, '_adapter', side_effect=AssertionError('custom rule changed')):
            self.assertFalse(bridge.perform(SITE, '签到', Mock()).success)

    def test_unapproved_adapter_cannot_be_used_as_fallback(self):
        bridge = self.bridge()
        bridge.route = Mock(return_value=('pt', None))
        unapproved = Mock()
        del unapproved.browserless_fallback_safe
        bridge._adapter = Mock(return_value=('sites', unapproved))
        self.assertFalse(bridge.perform(SITE, '签到', Mock()).success)
        unapproved.assert_not_called()

    def test_runtime_preserves_unstarted_evidence_only_for_direct_preflight(self):
        bridge = SignInBridge({'site_rules': '', 'browserless_url': '', 'read_activity': False,
                              'browserless_sites': [SITE['id']]})
        signer = Mock()
        signer.sign.return_value = core.SignResult('failed', 'Browserless 额度不足', not_started=True)
        with patch('app.plugins.ptsigninkeeper.ptengine.runtime.run_site_module', return_value=None):
            row = bridge.engine._sign_one(SITE, {}, signer)
        self.assertTrue(row['browserless_not_started'])
        signer.sign.return_value = core.SignResult('failed', 'Browserless 额度不足', not_started=False)
        with patch('app.plugins.ptsigninkeeper.ptengine.runtime.run_site_module', return_value=None):
            row = bridge.engine._sign_one(SITE, {}, signer)
        self.assertFalse(row['browserless_not_started'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
