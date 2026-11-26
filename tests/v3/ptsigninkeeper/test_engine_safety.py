"""Network-free regressions for the embedded PT response classifier."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

try:
    from app.plugins.ptsigninkeeper.ptengine import core
except ModuleNotFoundError:
    path = Path(__file__).resolve().parents[3] / 'plugins.v3/ptsigninkeeper/ptengine/core.py'
    spec = importlib.util.spec_from_file_location('ptsigninkeeper_test_core', path)
    core = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = core
    spec.loader.exec_module(core)


class ReceiptTests(unittest.TestCase):
    login_form = ('<form action="takelogin.php"><input name="username"><input type="password" name="password">'
                  '<label>两步验证码 <input name="2fa"></label><input name="imagestring">'
                  '<a href="recover.php">丢失了两步验证码？</a><button>登录</button></form>')

    def test_dolby_login_form_and_independent_otp_require_different_actions(self):
        otp = '<a href="/logout.php">退出</a><form>请输入两步验证码<input name="otp"></form>'
        for judge in (core.classify, core.judge_http):
            for page, expected in [(self.login_form, '重新登录'), (otp, '二级验证')]:
                with self.subTest(judge=judge.__name__, expected=expected):
                    result = judge(page + '<p>签到成功</p>', {})
                    self.assertTrue(result.auth_required)
                    self.assertEqual(result.status, 'failed')
                    self.assertIn(expected, result.message)
                    self.assertEqual(result.challenge, '')

    def test_account_security_navigation_and_hidden_form_do_not_mask_receipt(self):
        page = ('<a href="2fa.php">两步验证</a><a href="/help">如何完成异地登录安全验证</a>'
                '<a href="/logout.php">退出</a><template>' + self.login_form + '</template><p>今日已签到</p>')
        for judge in (core.classify, core.judge_http):
            self.assertEqual(judge(page, {}).status, 'already')

    def test_login_page_stops_http_fallback_and_browser_clicks(self):
        site = {'url': 'https://www.uhddolby.com/', 'cookie': 'sid=fixture'}
        rule = core.resolve_rule(site, {})
        session = Mock(headers={})
        session.get.return_value = Mock(status_code=200, text=self.login_form)
        with patch.object(core, 'new_http_session', return_value=session):
            self.assertTrue(core.http_sign_auto(site, rule).auth_required)
        session.get.assert_called_once()
        session.post.assert_not_called()
        page = Mock()
        page.content.return_value = self.login_form
        with patch.object(core, 'playwright_available', return_value=True), \
             patch.object(core, '_run_browser_callback', side_effect=lambda cb, **kwargs: cb(page)):
            self.assertTrue(core.playwright_sign(site, rule).auth_required)
        page.click.assert_not_called()

    def test_possible_and_hidden_success_are_not_receipts(self):
        for markup in (
            '<script>if (ok) { alert("签到成功"); }</script>',
            '<template><div>签到成功</div></template>',
            '<div hidden>签到成功</div>',
            '<div aria-hidden="true">签到成功</div>',
            '<div style="display: none">签到成功</div>',
            '<input type="hidden" value="已签到">',
            '<!-- 签到成功 -->',
        ):
            for judge in (core.judge_http, core.classify):
                with self.subTest(markup=markup, judge=judge.__name__):
                    self.assertEqual(judge(markup + '<button>签到</button>', {}).status, 'failed')

    def test_negative_statement_wins_over_success_word(self):
        for text in ('未检测到签到成功', '没有签到成功', '签到失败', '尚未签到', '今日未签到'):
            for judge in (core.judge_http, core.classify):
                self.assertEqual(judge('<div>' + text + '</div>', {}).status, 'failed')

    def test_unsubmitted_form_instruction_is_not_success(self):
        for control in ('<button>签到</button>', '<input type="submit" value="立即签到">'):
            for judge in (core.judge_http, core.classify):
                self.assertEqual(judge('<p>签到成功即可获得魔力</p>' + control, {}).status, 'failed')

    def test_explicit_api_failure_wins_over_message(self):
        for state in (False, 0, 'failed', 'error'):
            payload = json.dumps({'state': state, 'message': '没有签到成功，获得 10 魔力'}, ensure_ascii=False)
            for judge in (core.judge_http, core.classify):
                self.assertEqual(judge(payload, {}).status, 'failed')

    def test_explicit_api_success_and_already_still_work(self):
        for state, expected in ((True, 'success'), ('success', 'success'), ('already', 'already')):
            payload = json.dumps({'state': state, 'message': '获得 25 魔力'}, ensure_ascii=False)
            for judge in (core.judge_http, core.classify):
                result = judge(payload, {})
                self.assertEqual(result.status, expected)
                self.assertIn('25 魔力', result.message)

    def test_numeric_zero_status_uses_receipt_not_boolean_semantics(self):
        # Some APIs use 0 as their success code. It is not JSON false.
        payload = json.dumps({'status': 0, 'message': '签到成功，获得 10 魔力'}, ensure_ascii=False)
        self.assertEqual(core.judge_http(payload, {}).status, 'success')

    def test_visible_success_and_disabled_state_remain_valid(self):
        self.assertEqual(core.judge_http('<div>签到成功，获得 10 魔力</div>', {}).status, 'success')
        self.assertEqual(core.judge_http('<input disabled type="button" value="已经打卡">',
                                        {'already_keywords': ['已经打卡']}).status, 'already')

    def test_missing_button_on_error_page_does_not_prove_success(self):
        rule = {'absent_keywords': ['每日签到'], 'absent_status': 'success'}
        for judge in (core.judge_http, core.classify):
            self.assertEqual(judge('<div>Service unavailable</div>', rule).status, 'failed')
            self.assertEqual(judge('<a href="logout.php">退出</a><div>用户首页</div>', rule).status, 'success')

    def test_click_without_confirmation_is_not_success(self):
        rule = {'success_on_click': True}
        self.assertIsNone(core.judge_after_click(rule, True, '<button>签到</button>'))
        self.assertEqual(core.judge_after_click(rule, True, '<div>签到成功</div>').status, 'success')

    def test_profile_balance_is_not_earned_reward(self):
        for balance in ('魔力值：1,000.00', '总魔力 100 点魔力值'):
            html = '<aside>' + balance + '</aside><main>签到成功</main>'
            self.assertEqual(core.extract_bonus(html, {}), '')

    def test_hidden_reward_does_not_override_receipt(self):
        html = '<template>获得 999 魔力</template><main>签到成功，获得 10 魔力</main>'
        self.assertEqual(core.extract_bonus(html, {}), '获得 10 魔力')

    def test_pt_reward_units_are_kept(self):
        for quantity in ('20 憨豆', '30 金贝', '100 做种积分', '1,234.5 点魔力值', '0 魔力'):
            self.assertEqual(core.extract_bonus('签到成功，获得 ' + quantity, {}), '获得 ' + quantity)
        self.assertEqual(core.extract_bonus('签到成功，魔力值：50', {}), '魔力值：50')
        self.assertEqual(core.extract_bonus('鲸币 [使用]: 1000 (签到已得64)', {}), '64 鲸币')

    def test_custom_reward_rule_remains_available(self):
        self.assertEqual(core.extract_bonus('<input hidden name="reward" value="42">',
                                           {'bonus': r'name="reward" value="(\d+)"'}), '42')

    def test_visible_markup_keeps_tail_and_relative_time(self):
        self.assertEqual(core.plain_text('<div>已签到<template>失败</template>，刚刚 &lt; 1 分前</div>'),
                         '已签到，刚刚 < 1 分前')


if __name__ == '__main__':
    unittest.main(verbosity=2)
