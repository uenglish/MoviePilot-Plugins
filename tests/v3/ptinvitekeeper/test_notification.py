"""通知范围、失败缓存及成员统计必须与实际刷新结果一致。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.plugins.ptinvitekeeper import PTInviteKeeper
from app.plugins.ptinvitekeeper.notification import build_refresh_notification, send_preview
from app.plugins.ptinvitekeeper.trend import diff_snapshots


def record(quota=2, members=None, **extra):
    return {'last_update': 1000, 'data': {
        'invite_status': {'can_invite': True, 'permanent_count': quota, 'temporary_count': 0},
        'invitees': members or [], **extra}}


class RefreshNotificationTests(unittest.TestCase):
    def test_preview_does_not_refresh_or_change_notification_configuration(self):
        host = SimpleNamespace(data_manager=SimpleNamespace(get_site_data=lambda:{'本站':record()}),
                               post_message=Mock(), _notify=False, refresh_all_sites=Mock())
        self.assertTrue(send_preview(host)['success'])
        host.post_message.assert_called_once()
        host.refresh_all_sites.assert_not_called()
        self.assertFalse(host._notify)
        self.assertIn('本次未访问站点', host.post_message.call_args.kwargs['text'])
        host.data_manager.get_site_data = lambda:{}
        self.assertFalse(send_preview(host)['success'])
        host.post_message.assert_called_once()

    def test_targeted_refresh_excludes_other_sites_and_failed_cache(self):
        records = {'成功站': record(), '失败站': record(90, fetch_failed=True), '范围外': record(800)}
        title, text = build_refresh_notification(records, 1, 1, [{'site_name':'失败站', 'msg':'请求超时'}],
            site_names=['成功站','失败站'], targeted=True)
        self.assertEqual(title, 'PT 邀请管家 · 邀请数据更新')
        self.assertIn('指定站点 2 站', text)
        self.assertIn('已知可用名额 2 个', text)
        self.assertIn('失败站点保留历史缓存，不计入', text)
        self.assertNotIn('范围外', text)
        self.assertNotIn('90 个', text)

    def test_unknown_amount_is_not_zero_and_member_groups_do_not_overlap(self):
        records = {'零名额': record(0, [
            {'username':'member-secret-1','ratio':'0.1','enabled':False},
            {'username':'member-secret-2','ratio':'0.5','enabled':'yes'}]),
            '未知': {'data': {'invite_status': {'can_invite':True}}}}
        _, text = build_refresh_notification(records, 2, 0)
        self.assertIn('已知可用名额 0 个', text)
        self.assertIn('1 站名额未返回，未计入', text)
        self.assertIn('已禁用 1 人 · 低分享率 1 人', text)
        self.assertIn('需关注 2 人', text)
        self.assertNotIn('数据不全 0', text)
        self.assertNotIn('member-secret-', text)

    def test_all_failures_do_not_claim_updated_member_counts(self):
        _, text = build_refresh_notification({'失败站':record(50, fetch_failed=True)}, 0, 1)
        self.assertIn('本次没有读取成功的站点', text)
        self.assertNotIn('已知可用名额', text)
        self.assertNotIn('剩余名额 50', text)

    def test_details_are_bounded_and_secrets_are_removed(self):
        errors = [{'site_name':f'异常{i}', 'msg':'<b>网络异常</b><script>secret-script</script> https://x.test/?token=secret-url\nCookie: secret-cookie\ntoken=secret-token ' + '长' * 500} for i in range(12)]
        _, text = build_refresh_notification({}, 0, 12, errors, site_names=[e['site_name'] for e in errors])
        self.assertNotIn('secret-', text)
        self.assertNotIn('<b>', text)
        self.assertIn('另有 4 项', text)
        self.assertLess(len(text), 2200)

    def test_temporary_quota_change_is_reported_and_outside_changes_are_excluded(self):
        diff = diff_snapshots({'sites':{'本站':{'category':'invitable','temporary':1}}},
                              {'sites':{'本站':{'category':'invitable','temporary':2}}})
        self.assertEqual(len(diff['changed']), 1)
        diff['recovered'] = [{'name':'范围外'}]
        _, text = build_refresh_notification({'本站':record()},1,0,site_names=['本站'],diff=diff)
        self.assertIn('名额或成员变化：本站', text)
        self.assertNotIn('范围外', text)

    def test_rule_blocked_quotas_are_not_counted_as_available(self):
        blocked = {'data': {'invite_status': {'can_invite': False, 'reason': '等级不足',
                                             'permanent_count': 90, 'temporary_count': 0}}}
        _, text = build_refresh_notification({'可邀请': record(2), '等级受限': blocked}, 2, 0)
        self.assertIn('可邀请 1 站 · 已知可用名额 2 个', text)
        self.assertIn('等级不足 1 站', text)
        self.assertNotIn('92', text)
        self.assertNotIn('成员需关注', text)
        self.assertNotIn('低分享率 0', text)

    def test_pending_status_is_separate_from_read_failure_and_temporary_quota_is_visible(self):
        rows = {'可邀请': {'data': {'invite_status': {'can_invite':True, 'permanent_count':2, 'temporary_count':6}}},
                '未确认': {'data': {'invite_status': {'can_invite':False, 'reason':'站点未明确返回状态'}}}}
        _, text = build_refresh_notification(rows, 2, 0)
        self.assertIn('读取成功 2 · 失败 0', text)
        self.assertIn('邀请状态待处理', text)
        self.assertIn('未确认：站点未明确返回状态', text)
        self.assertIn('8 个（含临时 6）', text)
        self.assertNotIn('读取失败', text)

    def test_sender_keeps_channel_and_emits_once_without_network(self):
        plugin = object.__new__(PTInviteKeeper)
        plugin.data_manager = SimpleNamespace(get_site_data=lambda: {'本站':record()})
        plugin.post_message = Mock()
        plugin._send_refresh_notification(1,0,site_names=['本站'],targeted=True)
        plugin.post_message.assert_called_once()
        self.assertEqual(plugin.post_message.call_args.kwargs['title'], 'PT 邀请管家 · 邀请数据更新')
        plugin._send_refresh_notification(0,0)
        plugin.post_message.assert_called_once()


if __name__ == '__main__':
    unittest.main()
