"""邀请简报的统计口径、变化来源、缓存边界及真实宿主发送行为。"""
from copy import deepcopy
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.plugins.ptinvitekeeper import PTInviteKeeper
from app.plugins.ptinvitekeeper.data import DataManager
from app.plugins.ptinvitekeeper.module_loader import ModuleLoader
from app.plugins.ptinvitekeeper.notification import (
    build_refresh_content, build_refresh_notification, notification_plain_text, send_preview,
)


def record(quota=2, temporary=0, members=None, status=None, **extra):
    return {'last_update': 1000, 'data': {
        'invite_status': {'can_invite': True, 'permanent_count': quota, 'temporary_count': temporary, **(status or {})},
        'invitees': members or [], **extra}}


def member(name='private-member', ratio='2', enabled=True):
    return {'username': name, 'email': 'private@example.test', 'ratio': ratio, 'enabled': enabled}


def block(content, title):
    return next(item.text for item in content.blocks if (item.title or '').startswith(title))


class RefreshNotificationTests(unittest.TestCase):
    def test_targeted_refresh_excludes_other_sites_and_failed_cache(self):
        records = {'成功站': record(), '失败站': record(90, members=[member()], fetch_failed=True), '范围外': record(800)}
        title, text = build_refresh_notification(records, 1, 1, [{'site_name': '失败站', 'msg': '请求超时'}],
            site_names=['成功站', '失败站', '成功站'], targeted=True)
        self.assertEqual(title, 'PT 邀请管家 · 1 站需关注')
        self.assertIn('指定刷新 2 站', text)
        self.assertIn('现有可用名额 2 个 · 1 站持有', text)
        self.assertIn('成员记录 0 条', text)
        self.assertIn('历史缓存不计入', text)
        self.assertNotIn('范围外', text)
        self.assertNotIn('90 个', text)
        self.assertNotIn('全部站点', text)

    def test_unknown_zero_and_partial_quota_have_distinct_wording(self):
        records = {'零名额': record(0), '未知': record(None, None), '部分': record(None, 3)}
        _, text = build_refresh_notification(records, 3, 0)
        self.assertIn('已知可用名额 3 个 · 1 站持有', text)
        self.assertIn('零名额：现有 0 个（永久 0 · 临时 0）', text)
        self.assertIn('未知：名额未知', text)
        self.assertIn('部分：已知 3 个（永久 未知 · 临时 3）', text)

    def test_remaining_count_is_an_exact_total_without_invented_types(self):
        _, text = build_refresh_notification({'总数站': record(None, None, status={'remaining_count': 7})}, 1, 0)
        self.assertIn('现有可用名额 7 个', text)
        self.assertIn('现有 7 个（类型未返回）', text)
        self.assertNotIn('永久 0', text)

    def test_invalid_amounts_never_reduce_available_total(self):
        _, text = build_refresh_notification({'异常值': record(-10, 2), '小数': record(0.5, None)}, 2, 0)
        self.assertIn('已知可用名额 2 个', text)
        self.assertIn('已知 2 个（永久 未知 · 临时 2）', text)
        self.assertIn('小数：名额未知', text)

    def test_members_are_records_not_deduplicated_people_and_groups_do_not_overlap(self):
        records = {'本站': record(members=[member(ratio='0.1', enabled=False), member('private-2', '0.5'), member('private-3', '-')]),
                   '另一站': record(members=[member()])}
        content = build_refresh_content(records, 2, 0)
        text = notification_plain_text(content)
        self.assertEqual(content.title, 'PT 邀请管家 · 1 站需关注')
        self.assertIn('成员记录 4 条（分站统计，未跨站去重）', text)
        self.assertIn('已禁用 1 人 · 低分享率 1 人', block(content, '成员需关注'))
        self.assertNotIn('分享率待确认', block(content, '成员需关注'))
        self.assertIn('分享率待确认 1 人', block(content, '成员数据待确认'))
        self.assertNotIn('private', text)
        self.assertNotIn('@', text)

    def test_missing_ratio_alone_is_not_a_member_risk_alert(self):
        content = build_refresh_content({'本站': record(members=[member(ratio='-')])}, 1, 0)
        self.assertEqual(content.title, 'PT 邀请管家 · 邀请数据已更新')
        self.assertNotIn('成员需关注', notification_plain_text(content))
        self.assertIn('不据此判为低分享率或账号禁用', block(content, '成员数据待确认'))

    def test_all_failures_do_not_claim_fresh_counts_or_a_quota_drop(self):
        content = build_refresh_content({'失败站': record(50, fetch_failed=True, last_success_at=1000)}, 0, 1,
                                        previous_records={'失败站': record(50)})
        text = notification_plain_text(content)
        self.assertEqual(content.title, 'PT 邀请管家 · 本次读取失败')
        self.assertIn('名额与成员数据未更新', text)
        self.assertIn('上次成功', text)
        self.assertNotIn('可用名额', text)
        self.assertNotIn('成员记录', text)
        self.assertNotIn('→', text.replace('MoviePilot → PT 邀请管家', ''))

    def test_missing_error_details_are_acknowledged(self):
        _, text = build_refresh_notification({}, 0, 2, site_names=['甲', '乙'])
        self.assertIn('2 站未返回失败详情', text)
        self.assertNotIn('可用名额 0', text)

    def test_error_detail_outside_scope_is_not_exposed(self):
        _, text = build_refresh_notification({'本站': record()}, 1, 0,
            [{'site_name': '范围外', 'msg': 'secret-outside'}], site_names=['本站'])
        self.assertNotIn('范围外', text)
        self.assertNotIn('secret-outside', text)
        self.assertNotIn('读取失败 ·', text)

    def test_errors_are_sanitized_limited_and_have_advice(self):
        reason = '<b>读取异常</b><script>secret-script</script> https://x.test/?token=secret-url\nCookie: secret-cookie\n"token":"secret-token" private@example.test ' + '长' * 500
        errors = [{'site_name': f'异常{i}', 'msg': reason} for i in range(12)]
        _, text = build_refresh_notification({}, 0, 12, errors)
        self.assertNotIn('secret-', text)
        self.assertNotIn('<b>', text)
        self.assertNotIn('private@', text)
        self.assertIn('另有 6 站', text)
        self.assertIn('查看插件详情后重试', text)
        self.assertLess(len(text), 1800)

    def test_common_failure_reasons_have_specific_next_steps(self):
        for reason, expected in [('Cookie已失效', '重新登录并同步 Cookie'), ('Cloudflare 验证页', '浏览器服务'),
                                 ('ReadTimeout', '检查网络后重试'), ('HTTP 429', '避免连续刷新'),
                                 ('HTTP 503', '等待站点恢复后重试')]:
            with self.subTest(reason=reason):
                _, text = build_refresh_notification({}, 0, 1, [{'site_name': '本站', 'msg': reason}])
                self.assertIn(expected, text)

    def test_change_groups_status_quota_and_members_once_before_other_details(self):
        before = {'本站': record(2, 1, [member()], status={'can_invite': False, 'reason': '等级不足'})}
        after = {'本站': record(3, 4, [member(), member('private-2')])}
        content = build_refresh_content(after, 1, 0, previous_records=before)
        changes = block(content, '本次变化')
        self.assertEqual(changes.count('• 本站'), 1)
        self.assertIn('等级不足 → 可邀请', changes)
        self.assertIn('永久 2 → 3（+1）', changes)
        self.assertIn('临时 1 → 4（+3）', changes)
        self.assertIn('成员记录 1 → 2（+1）', changes)
        self.assertEqual(content.blocks[1].title, '本次变化 · 1 站')
        self.assertEqual(content.title, 'PT 邀请管家 · 1 站有变化')

    def test_lost_permission_and_decreasing_quota_are_explicit(self):
        content = build_refresh_content({'本站': record(1, status={'can_invite': False, 'reason': '等级不足'})},
            1, 0, previous_records={'本站': record(3)})
        changes = block(content, '本次变化')
        self.assertIn('可邀请 → 等级不足', changes)
        self.assertIn('永久 3 → 1（-2）', changes)
        self.assertIn('现有可用名额 0 个', notification_plain_text(content))

    def test_health_changes_are_visible_even_when_member_count_is_unchanged(self):
        content = build_refresh_content({'本站': record(members=[member(ratio='0.5')])}, 1, 0,
            previous_records={'本站': record(members=[member()])})
        self.assertIn('低分享率 0 → 1（+1）', block(content, '本次变化'))
        self.assertNotIn('成员记录', block(content, '本次变化'))
        self.assertEqual(content.title, 'PT 邀请管家 · 1 站需关注')

    def test_unknown_counts_are_not_reported_as_zero_deltas(self):
        content = build_refresh_content({'本站': record(2, None)}, 1, 0,
            previous_records={'本站': record(None, 1)})
        changes = block(content, '本次变化')
        self.assertIn('永久 未知 → 2', changes)
        self.assertIn('临时 1 → 未知', changes)
        self.assertNotIn('（+', changes)
        self.assertNotIn('（-', changes)

    def test_recovery_does_not_compare_corrupted_failure_state_or_stale_counts(self):
        content = build_refresh_content({'本站': record(4)}, 1, 0,
            previous_records={'本站': record(50, status={'can_invite': False, 'reason': '请求超时'}, fetch_failed=True)})
        changes = block(content, '本次变化')
        self.assertIn('读取恢复；可邀请；现有 4 个', changes)
        self.assertNotIn('50', changes)
        self.assertNotIn('（-46）', changes)
        self.assertNotIn('恢复可邀请', changes)

    def test_first_observation_is_not_reported_as_an_increase(self):
        content = build_refresh_content({'本站': record(4)}, 1, 0, previous_records={})
        self.assertIn('首次记录 1 站', block(content, '本次变化'))
        self.assertNotIn('（+4）', notification_plain_text(content))
        self.assertNotIn('有变化', content.title)

    def test_unchanged_is_limited_to_the_actual_refresh_scope(self):
        content = build_refresh_content({'本站': record(), '范围外': record(4)}, 1, 0,
            site_names=['本站'], previous_records={'本站': record(), '范围外': record(40)})
        self.assertIn('无变化', block(content, '本次变化'))
        self.assertNotIn('范围外', notification_plain_text(content))

    def test_owned_and_exchangeable_quota_are_not_added_together(self):
        content = build_refresh_content({'有名额': record(2), '可兑换': record(0, status={
            'bonus': 10000, 'permanent_invite_price': 5000, 'temporary_invite_price': 2000})}, 2, 0)
        text = notification_plain_text(content)
        self.assertIn('现有可用名额 2 个 · 1 站持有', text)
        self.assertIn('永久 2 个 或 临时 5 个', block(content, '可兑换名额'))
        self.assertIn('不能相加', text)
        self.assertNotIn('可兑换', block(content, '现有可用名额'))

    def test_reported_mteam_exchange_count_is_shown_without_owned_quota(self):
        content = build_refresh_content({'MTeam': record(0, status={
            'reason': '无可用邀请名额，魔力值(180000.5)可购买2个临时邀请'})}, 1, 0)
        self.assertIn('MTeam：临时 2 个', block(content, '可兑换名额'))
        self.assertIn('现有可用名额 0 个 · 0 站持有', notification_plain_text(content))

    def test_restricted_quota_and_exchange_are_not_claimed_as_usable(self):
        content = build_refresh_content({'可邀请': record(2), '等级受限': record(90, status={
            'can_invite': False, 'reason': '等级不足', 'bonus': 10000, 'temporary_invite_price': 1000})}, 2, 0)
        text = notification_plain_text(content)
        self.assertIn('现有可用名额 2 个', text)
        self.assertIn('等级受限：等级不足', block(content, '邀请受限'))
        self.assertNotIn('可兑换名额', text)
        self.assertNotIn('92', text)

    def test_pending_invitation_status_does_not_become_fetch_failure(self):
        content = build_refresh_content({'可邀请': record(2, 6), '未确认': record(status={
            'can_invite': False, 'reason': '站点未明确返回状态'})}, 2, 0)
        text = notification_plain_text(content)
        self.assertIn('已更新 2 站 · 读取失败 0 站', text)
        self.assertIn('未确认：站点未明确返回状态', block(content, '邀请状态待确认'))
        self.assertIn('现有 8 个（永久 2 · 临时 6）', text)
        self.assertNotIn('读取失败 ·', text)

    def test_large_notifications_have_global_budget_and_keep_all_section_counts(self):
        records, previous = {}, {}
        for i in range(80):
            name = f'站点{i}'
            records[name] = record(i, 2, [member(ratio='0.5'), member('private-2', '-')],
                status={'bonus': 100000, 'temporary_invite_price': 1000}, fetch_failed=i < 20)
            previous[name] = record(100, 0)
        content = build_refresh_content(records, 60, 20, previous_records=previous)
        text = notification_plain_text(content)
        self.assertLess(len(text), 4096)
        self.assertIn('本次变化 · 60 站', text)
        self.assertIn('读取失败 · 20 站', text)
        self.assertIn('成员需关注 · 60 站', text)
        self.assertIn('可兑换名额 · 60 站', text)
        self.assertTrue(text.endswith('完整结果与成员详情：MoviePilot → PT 邀请管家'))

    def test_saved_site_permission_messages_offer_actions_without_unusable_exchange_amounts(self):
        content = build_refresh_content({
            '二步验证站': record(status={'can_invite': False, 'reason': '你需要启用二步验证才能使用邀请功能'}),
            '资格未知': record(status={'can_invite': False,
                'reason': '无法发送邀请，请手动查看原因，但您的魔力值(120000)可购买临时邀请(24个,5000魔力/个)'})}, 2, 0)
        pending = block(content, '邀请状态待确认')
        self.assertIn('请在站点安全设置中开启', pending)
        self.assertIn('请先到站点确认邀请权限', pending)
        self.assertNotIn('120000', pending)
        self.assertNotIn('可兑换名额', notification_plain_text(content))

    def test_preview_keeps_cache_age_and_never_claims_a_new_refresh(self):
        records = {'本站': record(), '失败站': record(90, fetch_failed=True)}
        original = deepcopy(records)
        host = SimpleNamespace(data_manager=SimpleNamespace(get_site_data=lambda: records),
            post_message=Mock(), _notify=False, refresh_all_sites=Mock())
        self.assertTrue(send_preview(host)['success'])
        host.post_message.assert_called_once()
        host.refresh_all_sites.assert_not_called()
        self.assertFalse(host._notify)
        self.assertEqual(records, original)
        message = host.post_message.call_args.kwargs
        self.assertIn('本次未访问站点', message['text'])
        self.assertIn('缓存记录时间', message['text'])
        self.assertNotIn('已更新', message['text'])
        self.assertEqual(message['text'], notification_plain_text(message['rich_content']))
        host.data_manager.get_site_data = lambda: {}
        self.assertFalse(send_preview(host)['success'])
        host.post_message.assert_called_once()

    def test_sender_emits_once_with_identical_rich_and_plain_sections(self):
        plugin = object.__new__(PTInviteKeeper)
        plugin.data_manager = SimpleNamespace(get_site_data=lambda: {'本站': record(3)})
        plugin.post_message = Mock()
        plugin._last_diff = {'changed': [{'name': '错误的旧趋势'}]}
        plugin._send_refresh_notification(1, 0, site_names=['本站'], targeted=True,
                                          previous_records={'本站': record(2)})
        plugin.post_message.assert_called_once()
        message = plugin.post_message.call_args.kwargs
        self.assertEqual(message['title'], 'PT 邀请管家 · 1 站有变化')
        self.assertEqual(message['text'], notification_plain_text(message['rich_content']))
        self.assertIn('永久 2 → 3（+1）', message['text'])
        self.assertNotIn('错误的旧趋势', message['text'])
        plugin._send_refresh_notification(0, 0)
        plugin.post_message.assert_called_once()

    def test_real_refresh_path_passes_pre_refresh_data_and_respects_notify_switch(self):
        with TemporaryDirectory() as root:
            plugin = object.__new__(PTInviteKeeper)
            plugin._refresh_lock, plugin._refreshing = Lock(), False
            plugin._all_sites, plugin._notify = True, True
            plugin.sites = SimpleNamespace(get_indexers=lambda: [{'id': 1, 'name': '本站'}, {'id': 2, 'name': '范围外'}])
            plugin.data_manager = DataManager(root)
            plugin.data_manager.save_data({'本站': record(2), '范围外': record(100)})
            plugin._get_site_invite_data = Mock(return_value=record(5)['data'])
            plugin.post_message = Mock()
            with patch.object(ModuleLoader, 'load_site_handlers', return_value=[]):
                self.assertEqual(plugin.refresh_all_sites(['本站']), {'success': 1, 'error': 0})
                message = plugin.post_message.call_args.kwargs
                self.assertIn('永久 2 → 5（+3）', message['text'])
                self.assertNotIn('范围外', message['text'])
                plugin._notify = False
                plugin.refresh_all_sites(['本站'])
            plugin.post_message.assert_called_once()
            self.assertFalse(plugin._refresh_lock.locked())


if __name__ == '__main__':
    unittest.main()
