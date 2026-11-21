"""Notification semantics: one round, one site, real rewards and actionable failures."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from app.plugins.ptsigninkeeper.notification import (
    build_notification_content, build_notification_text, build_run_notification,
    notification_plain_text, notification_result_changed, notification_batch,
    queue_notification, send_preview)
from app.plugins.ptsigninkeeper.result import SiteResult


class NotificationTests(unittest.TestCase):
    def test_unchanged_retry_result_does_not_trigger_another_notice(self):
        previous = {'success': False, 'message': '请求失败', 'logged_in': False,
                    'bonus': '', 'activity': '01:00', 'engine': 'http', 'signin_supported': True}
        current = {**previous, 'activity': '01:10', 'engine': 'browserless'}
        self.assertFalse(notification_result_changed(previous, current))
        self.assertTrue(notification_result_changed(previous, {**current, 'logged_in': True}))
        self.assertTrue(notification_result_changed(None, current))

    def test_plain_and_rich_share_structure_and_zero_reward_is_visible(self):
        results = [SiteResult('成功站', '已签到', True, True, '0 魔力'),
                   SiteResult('保号站', '无签到功能', False, True, signin_supported=False),
                   SiteResult('异常站', 'Cookie 已失效', False)]
        args = ('2026-11-21', '签到', results, 3, [3], [1, 2, 3])
        content = build_notification_content(*args)
        text = build_notification_text(*args)
        self.assertEqual(content.title, 'PT 签到管家 · 需要关注')
        self.assertEqual(text, notification_plain_text(content))
        self.assertIn('签到完成 1 · 仅登录 1', text)
        self.assertIn('需处理 1 · 等待 0 · 登录有效 2', text)
        self.assertIn('成功站 · 0 魔力', text)
        self.assertIn('检查站点管理中的凭据', text)
        self.assertLess(text.index('异常站'), text.index('成功站'))
        self.assertIn('已签到回执不代表本轮新增收益', text)
        for result in results:
            self.assertEqual(text.count(result.site_name), 1)

    def test_waiting_u2_is_not_a_failure_or_success(self):
        result = SiteResult('幼儿园', '待开放：北京时间 09:00 后检查', False, True)
        content = build_notification_content('2026-11-21', '签到', [result], 1, [45], [45])
        text = notification_plain_text(content)
        self.assertIn('需处理 0 · 等待 1', text)
        self.assertEqual(content.title, 'PT 签到管家 · 等待后续处理')
        self.assertNotIn('需要处理', text)
        self.assertIn('本轮未完成 1 站', text)

    def test_signin_and_login_merge_without_double_counting(self):
        sign = SiteResult('验证码站', '验证码错误', False, False)
        login = SiteResult('验证码站', '登录成功', True, True)
        content = build_run_notification('2026-11-21', [('签到', [sign], [1]), ('登录', [login], [1])], 1, 1)
        text = notification_plain_text(content)
        self.assertIn('本轮处理 1 / 已选 1 站', text)
        self.assertIn('需处理 1 · 等待 0 · 登录有效 1', text)
        self.assertIn('1 站登录有效', text)
        self.assertEqual(text.count('验证码站'), 1)

    def test_missing_entry_is_failure_with_login_evidence(self):
        result = SiteResult('临时关闭站', '签到页面 HTTP 404', False, True)
        text = build_notification_text('2026-11-21', '签到', [result], 1, [1], [1])
        self.assertIn('签到入口暂不可用 · 1 站', text)
        self.assertIn('1 站登录有效', text)
        self.assertNotIn('无签到功能', text)
        self.assertIn('本轮未完成 1 站', text)

    def test_large_reports_are_bounded_without_repeating_raw_receipts(self):
        results = [SiteResult(f'成功站{i}', '重复回执', True, True, 0, '最近动向') for i in range(45)]
        text = build_notification_text('2026-11-21', '签到', results, 45, [], list(range(45)))
        self.assertIn('签到完成 45', text)
        self.assertIn('成功站0 · 0', text)
        self.assertIn('另有 39 站返回奖励', text)
        self.assertNotIn('重复回执', text)
        self.assertNotIn('最近动向', text)
        self.assertLess(len(text), 600)
        results = [SiteResult(f'失败站{i}', f'业务异常{i // 20}', False) for i in range(200)]
        text = build_notification_text('2026-11-21', '签到', results, 200, [], list(range(200)))
        self.assertIn('需处理 200', text)
        self.assertIn('等 20 站', text)
        self.assertIn('另有 5 类问题', text)
        self.assertLess(len(text), 1800)

    def test_external_receipts_are_sanitized_and_unknown_reward_is_not_zero(self):
        result = SiteResult('异常站', '<b>请求异常</b><script>secret-script</script> https://x.test/?token=secret-url\ntoken=secret-key ' + '长' * 500, False)
        text = build_notification_text('2026-11-21', '签到', [result], 1, [1], [1])
        self.assertNotIn('secret-', text)
        self.assertNotIn('<script>', text)
        self.assertNotIn('长' * 100, text)
        text = build_notification_text('2026-11-21', '签到', [SiteResult('测试站', '已签到', True, True)], 1, [], [1])
        self.assertIn('未返回奖励数额', text)
        self.assertNotIn('· 0', text)

    def test_one_round_sends_once_and_unchanged_retry_sends_nothing(self):
        host = SimpleNamespace(_notify=True, _rich_notify=True, _generation=1, post_message=Mock())
        sign = SiteResult('测试站', '已签到', True, True, '3 魔力')
        login = SiteResult('测试站', '登录成功', True, True)
        with notification_batch(host, '2026-11-21'):
            queue_notification(host, '2026-11-21', '签到', [sign], [1], [1], [], True)
            queue_notification(host, '2026-11-21', '登录', [login], [1], [1], [], True)
            host.post_message.assert_not_called()
        host.post_message.assert_called_once()
        payload = host.post_message.call_args.kwargs
        self.assertIn('本轮处理 1 / 已选 1 站', payload['text'])
        self.assertEqual(payload['text'], notification_plain_text(payload['rich_content']))
        with notification_batch(host, '2026-11-21'):
            queue_notification(host, '2026-11-21', '签到', [sign], [1], [1], [], False)
        host.post_message.assert_called_once()

    def test_reload_or_exception_discards_incomplete_notification(self):
        host = SimpleNamespace(_notify=True, _rich_notify=False, _generation=1, post_message=Mock())
        with notification_batch(host, '2026-11-21'):
            queue_notification(host, '2026-11-21', '签到', [SiteResult('测试', '失败', False)], [1], [1], [1], True)
            host._generation += 1
        host.post_message.assert_not_called()
        with self.assertRaises(RuntimeError), notification_batch(host, '2026-11-21'):
            queue_notification(host, '2026-11-21', '签到', [SiteResult('测试', '失败', False)], [1], [1], [1], True)
            raise RuntimeError('cancel')
        host.post_message.assert_not_called()

    def test_preview_uses_saved_results_and_separate_login_evidence(self):
        signs = {'results': {'1': {'success': False, 'message': '签到入口 HTTP 404'}}}
        logins = {'results': {'1': {'success': True}}}
        host = SimpleNamespace(get_data=Mock(side_effect=lambda key: signs if key.startswith('签到-') else logins),
                               _rich_notify=True, post_message=Mock())
        with patch('app.plugins.ptsigninkeeper.ui._metadata', return_value={'1': {'name': '本站'}}):
            self.assertTrue(send_preview(host)['success'])
        payload = host.post_message.call_args.kwargs
        self.assertIn('本次未执行签到', payload['text'])
        self.assertIn('1 站登录有效', payload['text'])
        self.assertEqual(payload['title'], payload['rich_content'].title)
        self.assertNotIn('logged_in', signs['results']['1'])
        host.post_message.assert_called_once()
