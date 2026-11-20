from unittest import TestCase
from unittest.mock import Mock

from app.plugins.ptinvitekeeper.sites import _ISiteHandler
from app.plugins.ptinvitekeeper.sites.hxpt import HxptHandler
from app.plugins.ptinvitekeeper.sites.yema import YemaHandler
from app.plugins.ptinvitekeeper.sites.rousipro import RousiProHandler


def ok(data):
    return Mock(status_code=200, json=lambda: {'code': 0, 'data': data})


class AccountInvitations(TestCase):
    def test_hx_current_quota_and_empty_member_page(self):
        http = Mock()
        http.get.side_effect = [ok({'class_id': 2, 'quota_limit': 0, 'quota_used': 0, 'quota_extra': 0}),
                                ok({'items': [], 'total': 0, 'page': 1})]
        result = HxptHandler().parse_invite_page({'url': 'https://hxpt.org/'}, http)
        self.assertFalse(result['invite_status']['can_invite'])
        self.assertIn('LV2', result['invite_status']['reason'])
        self.assertEqual(result['invitees'], [])

    def test_hx_member_failure_does_not_return_empty_success(self):
        http = Mock()
        http.get.side_effect = [ok({'class_id': 3, 'quota_limit': 2, 'quota_used': 1, 'quota_extra': 0}),
                                ok({'items': [], 'total': 1, 'page': 1})]
        self.assertIn('error', HxptHandler().parse_invite_page({'url': 'https://hxpt.org/'}, http))

    def test_rousipro_needs_web_session_and_never_signs_in_to_read_invites(self):
        http = Mock()
        http.get.return_value = Mock(status_code=401)
        self.assertIn('网页登录会话', RousiProHandler().parse_invite_page({'apikey': 'fake'}, http)['error'])
        http.post.assert_not_called()

    def test_hhclub_uses_current_account_panel(self):
        self.assertEqual(_ISiteHandler._user_id_from_html('<a href="userdetails.php?id=99">member</a><div id="user-info-panel"><a href="/invite.php?id=42">invite</a></div>', 'https://site.test/'), '42')

    def test_yema_zero_quota_and_zero_members_are_valid(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=lambda: {'success': True, 'data': {
            'inviteClaimInfo': {'permanentInviteNum': 0, 'tempInviteNum': 0},
            'invitePromotionStats': {'totalInviteUserNum': 0}}})
        session.post.return_value = Mock(status_code=200, json=lambda: {'success': True, 'data': []})
        result = YemaHandler().parse_invite_page({'url': 'https://www.yemapt.org'}, session)
        self.assertNotIn('error', result)
        self.assertFalse(result['invite_status']['can_invite'])
        self.assertEqual(result['invite_status']['temporary_count'], 0)
        self.assertEqual(result['invitees'], [])
