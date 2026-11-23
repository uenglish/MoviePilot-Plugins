"""上游适配：明确回执、只提交一次、登录只验证。"""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from app.plugins.ptsigninkeeper.ptengine import core
from app.plugins.ptsigninkeeper.ptengine.sites.sunnypt import SunnyPT
from app.plugins.ptsigninkeeper.ptengine.sites.tangpt import TangPT


def response(payload=None, text='', status=200):
    return SimpleNamespace(status_code=status, text=text, json=lambda: payload)


class SunnyTests(TestCase):
    site = {'url': 'https://sunnypt.top/', 'cookie': 'session=private-test'}

    def context(self, states, submitted=None):
        sessions = []
        payloads = [{'data': {'accessToken': 'test.token.value'}}, *states]
        for payload in payloads:
            session = Mock(headers={'Cookie': self.site['cookie']})
            session.get.return_value = response(payload)
            session.post.return_value = response(submitted)
            sessions.append(session)
        # Request order: session, status, POST, optional status readback.
        if submitted is not None:
            post = Mock(headers={'Cookie': self.site['cookie']})
            post.post.return_value = response(submitted)
            sessions.insert(2, post)
        return Mock(timeout=5, proxies=None, _session=Mock(side_effect=sessions)), sessions

    def test_login_validates_without_signing(self):
        ctx, sessions = self.context([{'code': 0, 'data': {'checked_in': False}}])
        self.assertTrue(SunnyPT().login(self.site, ctx)[0])
        for session in sessions:
            session.post.assert_not_called()
            session.close.assert_called_once()

    def test_already_skips_post_and_does_not_invent_reward(self):
        ctx, sessions = self.context([{'code': 0, 'data': {'checked_in': True, 'points': 999}}])
        self.assertEqual(SunnyPT().signin(self.site, ctx), (True, '今日已签到'))
        for session in sessions:
            session.post.assert_not_called()

    def test_post_preserves_zero_and_missing_reward(self):
        for data, expected in [({'points': 0}, '0 魔力值'), ({}, '签到成功'),
                               ({'points': '8.5'}, '8.5 魔力值'), ({'points': True}, '签到成功')]:
            ctx, sessions = self.context([{'code': 0, 'data': {'checked_in': False}}], {'code': 0, 'data': data})
            ok, message = SunnyPT().signin(self.site, ctx)
            self.assertTrue(ok); self.assertIn(expected, message)
            sessions[2].post.assert_called_once()
            self.assertNotIn('Cookie', sessions[1].headers)
            self.assertFalse(sessions[2].post.call_args.kwargs['allow_redirects'])

    def test_duplicate_requires_readback_and_unknown_state_never_posts(self):
        for state in [False, True]:
            ctx, sessions = self.context([{'code': 0, 'data': {'checked_in': False}},
                                         {'code': 0, 'data': {'checked_in': state}}], {'code': 400001})
            self.assertEqual(SunnyPT().signin(self.site, ctx)[0], state)
            sessions[2].post.assert_called_once()
        ctx, sessions = self.context([{'code': 0, 'data': {'checked_in': 'false'}}])
        self.assertFalse(SunnyPT().signin(self.site, ctx)[0])
        for session in sessions:
            session.post.assert_not_called()

    def test_http_errors_and_secrets_not_returned(self):
        ctx, sessions = self.context([])
        sessions[0].get.side_effect = RuntimeError('private-test test.token.value')
        ok, message = SunnyPT().signin(self.site, ctx)
        self.assertFalse(ok); self.assertNotIn('private-test', message)
        self.assertFalse(SunnyPT.match('https://sunnypt.top.example.org/'))

    def test_session_creation_and_cleanup_do_not_leak_or_replace_result(self):
        for adapter in (SunnyPT(), TangPT()):
            ctx = Mock(_session=Mock(side_effect=RuntimeError('private-secret')))
            ok, message = adapter.signin(self.site, ctx)
            self.assertFalse(ok)
            self.assertNotIn('private-secret', message)
        ctx, sessions = self.context([{'code': 0, 'data': {'checked_in': True}}])
        for session in sessions:
            session.close.side_effect = RuntimeError('private-secret')
        self.assertEqual(SunnyPT().signin(self.site, ctx), (True, '今日已签到'))


class TangTests(TestCase):
    site = {'url': 'https://tangpt.top/', 'cookie': 'test=only'}
    form = '<a href="logout.php">退出</a><form action="/attendance.php" method="post"><input type="hidden" name="csrf" value="test-csrf"><input type="submit" name="submit" value="立即签到"></form>'
    signed = '<a href="logout.php">退出</a><h2>签到成功</h2><p>本次签到获得 20 魔力值</p>'

    def context(self, before, after=''):
        session = Mock(headers={})
        session.get.side_effect = [response(text=before), response(text=after)]
        session.post.return_value = response(status=302)
        return Mock(timeout=5, proxies=None, _session=Mock(return_value=session)), session

    def test_form_preserves_hidden_fields_and_reads_back_once(self):
        ctx, session = self.context(self.form, self.signed)
        ok, message = TangPT().signin(self.site, ctx)
        self.assertTrue(ok); self.assertIn('20 魔力值', message)
        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.kwargs['data'], {'csrf': 'test-csrf', 'submit': '立即签到'})
        self.assertEqual(session.get.call_count, 2)

    def test_already_does_not_submit(self):
        ctx, session = self.context('<a href="logout.php">退出</a><p>今日已签到</p>')
        self.assertTrue(TangPT().signin(self.site, ctx)[0]); session.post.assert_not_called()

    def test_uncertain_submit_only_reads_back(self):
        ctx, session = self.context(self.form, self.form)
        session.post.side_effect = TimeoutError('secret')
        ok, message = TangPT().signin(self.site, ctx)
        self.assertFalse(ok); self.assertIn('未确认', message)
        session.post.assert_called_once(); self.assertEqual(session.get.call_count, 2)

    def test_challenge_external_form_and_hidden_form_never_submit(self):
        for page in ['<div class="cf-turnstile">验证</div>',
                     self.form.replace('/attendance.php', 'https://evil.example/attendance.php'),
                     '<template>' + self.form + '</template>']:
            ctx, session = self.context(page)
            self.assertFalse(TangPT().signin(self.site, ctx)[0]); session.post.assert_not_called()

    def test_default_routes_are_http_and_discovered_modules_share_identity(self):
        self.assertEqual(core.resolve_rule(self.site, {})['engine'], 'http')
        modules = core.load_site_modules(refresh=True)
        self.assertIn(TangPT, modules); self.assertIn(SunnyPT, modules)
