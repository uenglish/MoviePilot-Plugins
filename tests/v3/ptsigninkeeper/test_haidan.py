"""Exercise the home button, one AJAX request and cookie rotation over real local HTTP."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest import TestCase
from unittest.mock import Mock, patch

from app.plugins.ptsigninkeeper.ptengine import core, haidan
from app.plugins.ptsigninkeeper.ptengine.runtime import RuleEngine


class HaiDanTests(TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server = self.server
                server.paths.append(self.path)
                if self.path == '/signin.php':
                    server.entries += 1
                    ok = (self.headers.get('Cookie') == 'sid=rotated' and
                          self.headers.get('X-Requested-With') == 'XMLHttpRequest')
                    if ok and server.mode in {'rotate', 'drop'}:
                        server.signed = True
                    if server.mode == 'drop':
                        self.close_connection = True
                        return
                    self.send_response(302 if ok and server.mode == 'rotate' else 403)
                    self.send_header('Location', '/index.php')
                    self.end_headers()
                    return
                if server.mode == 'login':
                    self.send_response(302)
                    self.send_header('Location', '/login.php')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Set-Cookie', 'sid=rotated; Domain=127.0.0.1; Path=/')
                self.end_headers()
                label = '已经打卡' if server.signed else '签到'
                self.wfile.write(('<a href="/logout.php">退出</a><script>\'<input value="已经打卡">\'</script>'
                                  f'<input type="button" value="{label}">').encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.paths, server.entries, server.signed, server.mode = [], 0, False, 'rotate'
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.03}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server
        self.site = {'id': 1, 'url': f'http://127.0.0.1:{server.server_port}/', 'cookie': 'sid=old'}
        self.rule = core.resolve_rule({'url': 'https://www.haidan.cc/'}, {})

    def test_rotated_or_interrupted_entry_is_confirmed_from_home(self):
        for mode in ('rotate', 'drop'):
            self.server.mode, self.server.signed = mode, False
            self.server.paths.clear()
            result = haidan.sign_in(self.site, self.rule, 2, None)
            self.assertEqual(result.status, 'success', result.message)
            self.assertEqual(self.server.paths, ['/index.php', '/signin.php', '/index.php'])

    def test_existing_receipt_or_expired_session_never_opens_entry(self):
        for mode, expected in [('rotate', 'already'), ('login', 'failed')]:
            self.server.mode, self.server.signed = mode, True
            result = haidan.sign_in(self.site, self.rule, 2, None)
            self.assertEqual(result.status, expected)
            self.assertEqual(result.auth_required, mode == 'login')
        self.assertEqual(self.server.entries, 0)

    def test_entry_status_is_kept_when_home_does_not_confirm(self):
        self.server.mode = 'denied'
        result = haidan.sign_in(self.site, self.rule, 2, None)
        self.assertEqual(result.status, 'failed')
        self.assertIn('入口返回 HTTP 403', result.message)
        self.assertIn('未显示当日打卡回执', result.message)
        self.assertEqual(self.server.entries, 1)

    def test_builtin_alias_and_explicit_custom_routes(self):
        engine = RuleEngine({'browserless_sites': [], 'read_activity': False})
        site = dict(self.site, url='https://www.haidan.video/')
        failure = core.SignResult('failed', '首页未显示当日打卡回执')
        signer = Mock()
        with patch.object(haidan, 'sign_in', return_value=failure) as builtin, \
             patch('app.plugins.ptsigninkeeper.ptengine.runtime.http_sign_auto', return_value=failure) as custom:
            self.assertEqual(engine._sign_one(site, {}, signer)['message'], failure.message)
            self.assertEqual(engine._sign_one(site, {'1': {'engine': 'http', 'path': '/custom'}}, signer)['status'], 'failed')
            builtin.assert_called_once()
            custom.assert_called_once()
            self.assertEqual(custom.call_args.args[1]['path'], '/custom')
            signer.sign.assert_not_called()
