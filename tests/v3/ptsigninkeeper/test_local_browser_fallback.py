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
    def test_unlisted_nexus_sites_read_current_personal_receipt(self):
        for host, amount, unit in [('hhanclub.net', 50, '憨豆'), ('ptsbao.club', 50, '魔力值'),
                                   ('www.hdkyl.in', 170, '魔力值')]:
            site = {**SITE, 'url': f'https://{host}/'}
            html = (f'<aside>连续签到可获得 1000 憨豆</aside><a href="logout.php">退出</a>'
                    f'<p>这是您的第 <b>87</b> 次签到，已连续签到 9 天，本次签到获得 {amount} 个{unit}。</p>')
            bridge = SignInBridge({'browserless_sites': [], 'read_activity': False})
            bridge._enrich = Mock(side_effect=lambda info: info)
            signer = Mock()
            bridge._browser = Mock(return_value=signer)
            with self.subTest(host=host), patch.object(NexusPhpAttendance, '_read_receipt_page',
                    return_value=(200, html)) as get, patch.object(bridge.engine, 'read_header_reward', return_value=''):
                result = bridge.perform(site, '签到', Mock())
                self.assertTrue(result.success, result.message)
                self.assertEqual((result.engine, result.bonus), ('http', f'{amount} {unit}'))
                self.assertEqual(get.call_args.args[0], site['url'] + 'attendance.php')
                get.assert_called_once()
                signer.sign.assert_not_called()

    def test_unlisted_calendar_history_and_challenge_keep_both_failure_reasons(self):
        bridge = SignInBridge({'browserless_sites': [], 'read_activity': False})
        bridge._enrich = Mock(side_effect=lambda info: info)
        site = {**SITE, 'url': 'https://hhanclub.net/'}
        for body, reason in [('<div>昨日已签到 已领取</div>', '当日签到回执'),
                             ('<p>历史回执：这是您的第4次签到，已连续签到2天，本次签到获得5个憨豆。</p>', '回执'),
                             ('<blockquote><p>这是您的第4次签到，已连续签到2天，本次签到获得5个憨豆。</p></blockquote>', '回执'),
                             ('<div>昨天已签到</div><button>立即签到</button>', '按钮'),
                             ('<div class="cf-turnstile"></div>', '人工验证')]:
            with self.subTest(body=body), patch.object(NexusPhpAttendance, '_read_receipt_page',
                    return_value=(200, '<a href="/logout.php">退出</a>' + body)) as get:
                result = bridge.perform(site, '签到', Mock())
                self.assertFalse(result.success)
                self.assertIn('白名单', result.message)
                self.assertIn(reason, result.message)
                get.assert_called_once()
        bridge.rules = {'1': {'engine': 'browserless'}}
        with patch.object(NexusPhpAttendance, '_read_receipt_page') as get:
            result = bridge.perform(site, '签到', Mock())
            self.assertFalse(result.success)
            self.assertIn('白名单', result.message)
            get.assert_not_called()

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
        site = {**SITE, 'name': '皇后', 'url': 'https://open.cd/'}
        bridge = SignInBridge({'site_rules': '', 'browserless_url': '', 'read_activity': False,
                              'browserless_sites': [site['id']]})
        signer = Mock()
        signer.sign.return_value = core.SignResult('failed', 'Browserless 额度不足', not_started=True)
        with patch('app.plugins.ptsigninkeeper.ptengine.runtime.run_site_module', return_value=None):
            row = bridge.engine._sign_one(site, {}, signer)
        self.assertTrue(row['browserless_not_started'])
        signer.sign.return_value = core.SignResult('failed', 'Browserless 额度不足', not_started=False)
        with patch('app.plugins.ptsigninkeeper.ptengine.runtime.run_site_module', return_value=None):
            row = bridge.engine._sign_one(site, {}, signer)
        self.assertFalse(row['browserless_not_started'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
