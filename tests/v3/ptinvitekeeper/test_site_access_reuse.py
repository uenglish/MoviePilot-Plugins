"""Account links from a verified homepage avoid a redundant failure-prone request."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from app.plugins.ptinvitekeeper.sites import _ISiteHandler


class UserIdReuseTests(TestCase):
    def test_verified_homepage_uses_own_invite_link_without_network(self):
        session = Mock()
        html = '<a href="userdetails.php?id=99">forum member</a><div id="info_block"><a href="invite.php?id=7&amp;type=new">邀请</a></div>'
        self.assertEqual(_ISiteHandler._get_user_id(session, "https://site.test/", html), "7")
        session.get.assert_not_called()

    def test_idless_authenticated_layout_remains_supported(self):
        session = Mock()
        self.assertEqual(_ISiteHandler._get_user_id(session, "https://site.test/", '<a href="logout.php">退出</a>'), "")
        session.get.assert_not_called()

    def test_uninformative_homepage_falls_back_to_control_panel(self):
        response = Mock(text='<div id="info_block"><a href="userdetails.php">profile</a><a href="userdetails.php?id=17">me</a></div>')
        session = Mock(get=Mock(return_value=response))
        self.assertEqual(_ISiteHandler._get_user_id(session, "https://site.test/", '<h1>Home</h1>'), "17")
        session.get.assert_called_once_with("https://site.test/usercp.php", timeout=(5, 15))

    def test_login_form_does_not_count_as_authenticated(self):
        self.assertIsNone(_ISiteHandler._user_id_from_html('<form action="takelogin.php"><input name="password"></form>'))

    def test_other_accounts_and_external_links_are_not_owner_evidence(self):
        html = '<a href="userdetails.php?id=99">member</a><div id="info_block"><a href="https://other.test/invite.php?id=9">other</a></div>'
        self.assertIsNone(_ISiteHandler._user_id_from_html(html, 'https://site.test/'))
