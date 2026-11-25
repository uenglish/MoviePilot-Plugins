"""Exercise the actual V2 RequestUtils against a local HTTP server."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from unittest.mock import patch

from app.plugins.ptsigninkeeper.ptengine.core import http_sign_auto
from app.plugins.ptsigninkeeper.ptengine.http import new_http_session


class V2HttpSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Set-Cookie', 'session=redirected; Path=/')
                    self.send_header('Location', '/form')
                    self.end_headers()
                    return
                if self.path == '/domain-redirect':
                    self.send_response(302)
                    self.send_header('Set-Cookie', 'auth=redirected; Domain=127.0.0.1; Path=/')
                    self.send_header('Location', '/raw')
                    self.end_headers()
                    return
                if self.path == '/attendance.php':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Set-Cookie', 'csrf=form-session; Path=/')
                    self.end_headers()
                    self.wfile.write(('<form method="post" action="/attendance.php">'
                                      '<input type="hidden" name="csrf" value="form-session">'
                                      '<input type="submit" value="立即签到"></form>').encode())
                    return
                self.send_response(200)
                if self.path == '/form':
                    self.send_header('Set-Cookie', 'csrf=form-session; Path=/')
                elif self.path == '/rotate':
                    self.send_header('Set-Cookie', 'auth=rotated; Path=/')
                    self.send_header('Set-Cookie', 'scoped=private; Path=/private')
                elif self.path == '/expire':
                    self.send_header('Set-Cookie', 'auth=; Max-Age=0; Path=/')
                elif self.path == '/domain-rotate':
                    self.send_header('Set-Cookie', 'auth=rotated; Domain=127.0.0.1; Path=/')
                elif self.path == '/domain-expire':
                    self.send_header('Set-Cookie', 'auth=; Max-Age=0; Domain=127.0.0.1; Path=/')
                elif self.path == '/domain-scoped':
                    self.send_header('Set-Cookie', 'auth=private; Domain=127.0.0.1; Path=/private')
                elif self.path == '/unrelated-domain':
                    self.send_header('Set-Cookie', 'auth=bad; Domain=other.test; Path=/')
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                if self.path == '/raw':
                    self.wfile.write(self.headers.get('Cookie', '').encode())
                    return
                cookie = SimpleCookie(self.headers.get('Cookie', ''))
                self.wfile.write(json.dumps({k: v.value for k, v in cookie.items()}).encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.server.posts.append((self.path, body))
                if self.path == '/drop':
                    self.close_connection = True
                    return
                cookie = SimpleCookie(self.headers.get('Cookie', ''))
                if self.path == '/attendance.php':
                    ok = cookie.get('csrf') and cookie['csrf'].value == 'form-session' and b'csrf=form-session' in body
                    self.send_response(200 if ok else 403)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(('签到成功，本次签到获得 12 魔力值' if ok else '会话校验失败').encode())
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({k: v.value for k, v in cookie.items()}).encode())

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.posts = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.session = new_http_session()
        self.session.headers['Cookie'] = 'auth=original'
        self.server.posts.clear()
        self.addCleanup(self.session.close)

    def test_redirect_and_form_cookies_survive_until_post(self):
        self.session.get(self.url + '/redirect', timeout=3)
        response = self.session.post(self.url + '/submit', data={}, timeout=3)
        self.assertEqual(response.json(), {'auth': 'original', 'session': 'redirected', 'csrf': 'form-session'})

    def test_rotation_expiry_and_cookie_path_are_respected(self):
        self.session.get(self.url + '/rotate', timeout=3)
        self.assertEqual(self.session.get(self.url + '/echo', timeout=3).json(), {'auth': 'rotated'})
        self.assertEqual(self.session.get(self.url + '/private/echo', timeout=3).json(),
                         {'auth': 'rotated', 'scoped': 'private'})
        self.session.get(self.url + '/expire', timeout=3)
        self.assertEqual(self.session.get(self.url + '/echo', timeout=3).json(), {})

    def test_initial_cookie_is_not_reinjected_on_another_host(self):
        self.session.get(self.url + '/echo', timeout=3)
        other_host = self.url.replace('127.0.0.1', 'localhost')
        self.assertEqual(self.session.get(other_host + '/echo', timeout=3).json(), {})

    def test_explicit_domain_cookie_replaces_bootstrap_without_duplicates(self):
        self.session.get(self.url + '/domain-rotate', timeout=3)
        self.assertEqual(self.session.get(self.url + '/raw', timeout=3).text, 'auth=rotated')

    def test_explicit_domain_cookie_expiry_removes_bootstrap(self):
        self.session.get(self.url + '/domain-expire', timeout=3)
        self.assertEqual(self.session.get(self.url + '/raw', timeout=3).text, '')

    def test_redirect_already_uses_rotated_domain_cookie(self):
        self.assertEqual(self.session.get(self.url + '/domain-redirect', timeout=3).text, 'auth=redirected')

    def test_narrower_path_or_unrelated_domain_does_not_replace_root_cookie(self):
        for path in ('/domain-scoped', '/unrelated-domain'):
            self.session.get(self.url + path, timeout=3)
            self.assertEqual(self.session.get(self.url + '/raw', timeout=3).text, 'auth=original')

    def test_changed_configured_cookie_replaces_initial_auth(self):
        self.session.get(self.url + '/rotate', timeout=3)
        self.session.get(self.url + '/domain-rotate', timeout=3)
        self.session.headers['Cookie'] = 'auth=updated'
        self.assertEqual(self.session.get(self.url + '/echo', timeout=3).json(), {'auth': 'updated'})

    def test_unknown_post_result_is_not_retried_by_v2_transport(self):
        with self.assertRaises(Exception):
            self.session.post(self.url + '/drop', data={'sign': 'once'}, timeout=3)
        self.assertEqual(len(self.server.posts), 1)

    def test_real_v2_form_submission_keeps_csrf_session(self):
        result = http_sign_auto({'url': self.url, 'cookie': 'auth=original'},
                                {'method': 'GET', 'path': '/attendance.php', 'submit_text': '立即签到'}, timeout=3)
        self.assertEqual(result.status, 'success', result.message)
        self.assertEqual(len(self.server.posts), 1)

    def test_close_releases_the_supplied_session(self):
        with patch.object(self.session._transport, 'close') as close:
            self.session.close()
        close.assert_called_once()
