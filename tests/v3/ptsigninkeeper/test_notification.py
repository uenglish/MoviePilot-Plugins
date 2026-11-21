"""Each visited site belongs to exactly one notification result group."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.plugins.ptsigninkeeper.notification import (build_notification_content, build_notification_text,
    notification_result_changed, notification_title, send_preview)
from app.plugins.ptsigninkeeper.result import SiteResult


class NotificationTests(unittest.TestCase):
    def test_unchanged_retry_result_does_not_trigger_another_notice(self):
        previous = {'success': False, 'message': '请求失败', 'logged_in': False,
                    'bonus': '', 'activity': '01:00', 'engine': 'http',
                    'signin_supported': True}
        current = {'success': False, 'message': '请求失败', 'logged_in': False,
                   'bonus': '', 'activity': '01:10', 'engine': 'browserless',
                   'signin_supported': True}
        self.assertFalse(notification_result_changed(previous, current))
        self.assertTrue(notification_result_changed(previous, {**current, 'logged_in': True}))
        self.assertTrue(notification_result_changed(None, current))

    def test_preview_only_reads_saved_results_and_sends_one_labeled_message(self):
        host = SimpleNamespace(get_data=Mock(return_value={'results':{'1':{'success':True,'message':'签到成功'}}}),
                               _rich_notify=True, post_message=Mock())
        with patch('app.plugins.ptsigninkeeper.ui._metadata', return_value={'1':{'name':'测试站'}}):
            result = send_preview(host)
        self.assertTrue(result['success'])
        host.post_message.assert_called_once()
        payload = host.post_message.call_args.kwargs
        self.assertIn('排版预览', payload['title'])
        self.assertIn('本次未执行签到', payload['text'])
        self.assertEqual(payload['rich_content'].title, payload['title'])
        host.get_data.return_value = {}
        with patch('app.plugins.ptsigninkeeper.ui._metadata', return_value={}):
            self.assertFalse(send_preview(host)['success'])
        host.post_message.assert_called_once()

    def test_new_identity_and_plain_results_match_rich_results(self):
        results = [SiteResult('成功站', '签到成功，获得 0 魔力', True, True, '0 魔力'),
                   SiteResult('保号站', '无签到功能，登录成功', False, True, signin_supported=False),
                   SiteResult('异常站', 'Cookie 已失效', False)]
        args = ('2026-11-20', '签到', results, 3, [3], [1, 2, 3])
        content = build_notification_content(*args)
        text = build_notification_text(*args)
        self.assertEqual(content.title, notification_title('签到'))
        self.assertEqual(content.title, 'PT 签到管家 · 签到结果')
        self.assertIn('登录检测结果', notification_title('登录'))
        self.assertIn('签到成功 1 · 仅登录 1 · 失败 1', text)
        self.assertLess(text.index('异常站'), text.index('成功站'))
        self.assertIn('已返回奖励 1 站', text)
        self.assertEqual(text, '\n\n'.join('\n'.join(part for part in (b.title, b.text) if part)
                                         for b in content.blocks))
        self.assertIn('MoviePilot → PT 签到管家', text)

    def test_external_receipts_are_bounded_and_sanitized(self):
        result = SiteResult('异常站', '<b>请求异常</b><script>secret-script</script> https://x.test/?token=secret-url\ntoken=secret-key ' + '长' * 500, False)
        args = ('2026-11-20', '签到', [result], 1, [1], [1])
        content = build_notification_content(*args)
        rich = '\n'.join((block.title or '') + (block.text or '') for block in content.blocks)
        for text in (rich, build_notification_text(*args)):
            self.assertNotIn('secret-', text)
            self.assertNotIn('<script>', text)
            self.assertNotIn('长' * 161, text)

    def test_signin_failure_with_valid_login_is_counted_once(self):
        results = [
            SiteResult('验证码站', '签到失败：验证码错误', False, True),
            SiteResult('保号站', '无签到功能，登录成功', False, True, signin_supported=False),
            SiteResult('失效站', '登录失败：Cookie 已失效', False),
            SiteResult('成功站', '签到成功，获得 10 魔力', True, True, '10 魔力'),
        ]
        content = build_notification_content('2026-11-16', '签到', results, 4, [1, 3], [1, 2, 3, 4])
        text = '\n'.join((block.title or '') + '\n' + (block.text or '') for block in content.blocks)
        self.assertIn('签到成功 1 · 仅登录 1 · 失败 2', text)
        self.assertIn('签到失败 · 登录有效 · 1', text)
        self.assertIn('无签到功能 · 登录有效 · 1', text)
        self.assertIn('待重试 2 站', text)
        for result in results:
            self.assertEqual(text.count(result.site_name), 1)

    def test_login_results_have_no_signin_failure_group(self):
        results = [SiteResult('登录站', '登录成功', True, True),
                   SiteResult('未登录站', '登录失败', False)]
        content = build_notification_content('2026-11-16', '登录', results, 2, [2], [1, 2])
        text = '\n'.join((block.title or '') + '\n' + (block.text or '') for block in content.blocks)
        self.assertIn('登录成功 1 · 失败 1', text)
        self.assertNotIn('签到失败 · 登录有效', text)

    def test_many_successes_do_not_repeat_receipts_and_zero_is_a_returned_reward(self):
        results = [SiteResult(f'成功站{i}', '重复回执', True, True, 0, '最近动向') for i in range(45)]
        results.append(SiteResult('超时站', '请求超时', False))
        text = build_notification_text('2026-11-20', '签到', results, 46, [], list(range(46)))
        self.assertIn('签到成功 45 · 仅登录 0 · 失败 1', text)
        self.assertIn('已返回奖励 45 站', text)
        self.assertNotIn('重复回执', text)
        self.assertNotIn('最近动向', text)
        self.assertNotIn('成功站44', text)
        self.assertLess(len(text), 350)

    def test_large_failure_groups_are_bounded_with_omission_counts(self):
        results = [SiteResult(f'失败站{i}', f'业务异常{i // 20}', False) for i in range(200)]
        text = build_notification_text('2026-11-20', '签到', results, 200, [], list(range(200)))
        self.assertIn('失败 200', text)
        self.assertIn('等 20 站', text)
        self.assertIn('另有 4 类异常', text)
        self.assertLess(len(text), 1400)

    def test_missing_entry_remains_retryable_and_is_not_permanent_no_signin(self):
        result = SiteResult('临时关闭站', '签到页面 HTTP 404', False, True)
        text = build_notification_text('2026-11-20', '签到', [result], 1, [1], [1])
        self.assertIn('签到入口暂不可用 · 1 站', text)
        self.assertIn('临时关闭站：登录已确认', text)
        self.assertIn('后续任务会重新检查', text)
        self.assertNotIn('无签到功能', text)
        self.assertFalse(result.completed)
        self.assertTrue(result.signin_supported)
        self.assertIn('待重试 1 站', text)

    def test_preview_reuses_separate_login_evidence_without_rewriting_history(self):
        signs = {'results': {'1': {'success':False, 'message':'签到入口 HTTP 404'}}}
        logins = {'results': {'1': {'success':True}}}
        host = SimpleNamespace(get_data=Mock(side_effect=lambda key: signs if key.startswith('签到-') else logins),
                               _rich_notify=False, post_message=Mock())
        with patch('app.plugins.ptsigninkeeper.ui._metadata', return_value={'1': {'name':'本站'}}):
            send_preview(host)
        self.assertIn('本站：登录已确认', host.post_message.call_args.kwargs['text'])
        self.assertNotIn('logged_in', signs['results']['1'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
