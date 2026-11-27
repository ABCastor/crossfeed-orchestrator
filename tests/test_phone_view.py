"""The local signed console and the tailnet proxy have separate access boundaries."""
import contextlib
import http.client
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from tests.test_console import base_overlay, console

phone = console.phone_view


@contextlib.contextmanager
def serving(server):
    with server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield
        finally:
            server.shutdown()
            worker.join(timeout=5)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('fixture.invalid', timeout=5)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request(conn, method, path, headers=()):
    try:
        conn.putrequest(method, path)
        conn.putheader('Connection', 'close')
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders()
        reply = conn.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read().decode()
    finally:
        conn.close()


class PhoneTests(unittest.TestCase):
    def test_tailnet_view_needs_one_exact_login_and_never_serves_the_console(self):
        login = 'operator@example.invalid'
        identity = [('Tailscale-User-Login', login)]
        snapshot = Mock(return_value={'pools': [{'name': 'Test <quota>', 'used_percent': 23, 'resets': 'today 14:30',
                                                 'pace': {'words': 'on pace'}, 'measured': True}],
                                      'subscription_spend': {'monthly': [{'currency': 'EUR', 'total': '12.00',
                                                                          'operator': '6.00'}],
                                                             'unpriced': 1, 'unavailable': False}})
        with tempfile.TemporaryDirectory(prefix='cf-') as root:
            path = Path(root) / 'phone.sock'
            with serving(phone.open_server(path, login, snapshot)):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                for invalid in ([], [('Tailscale-User-Login', 'other@example.invalid')], identity * 2,
                                [('Tailscale-User-Login', login.upper())]):
                    self.assertEqual(request(UnixConnection(path), 'GET', '/crossfeed/', invalid)[0], 403)
                for route in ('/snapshot', '/settings', '/login?key=fixture', '/level'):
                    self.assertEqual(request(UnixConnection(path), 'GET', route, identity)[0], 404)
                self.assertEqual(request(UnixConnection(path), 'POST', '/crossfeed', identity)[0], 405)
                snapshot.assert_not_called()
                status, headers, body = request(UnixConnection(path), 'GET', '/crossfeed/', identity)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Cache-Control'], 'private, no-store')
                self.assertIn("form-action 'none'", headers['Content-Security-Policy'])
                for copy in ('77% left', 'Test &lt;quota&gt;', 'today 14:30', 'on pace', 'EUR 12.00', 'EUR 6.00'):
                    self.assertIn(copy, body)
                self.assertNotIn('<form', body)
                self.assertNotIn(login, body)
                self.assertNotIn('Set-Cookie', headers)

    def test_loopback_backend_applies_the_same_identity_rule(self):
        login = 'operator@example.invalid'
        snapshot = Mock(return_value={'pools': [], 'subscription_spend': {'monthly': [], 'unpriced': 0,
                                                                          'unavailable': False}})
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        server = phone.open_loopback_server(port, login, snapshot)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        with serving(server):
            conn = lambda: http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
            self.assertEqual(request(conn(), 'GET', '/crossfeed/', [('Tailscale-User-Login', 'other@example.invalid')])[0], 403)
            self.assertEqual(request(conn(), 'GET', '/settings', [('Tailscale-User-Login', login)])[0], 404)
            self.assertEqual(request(conn(), 'GET', '/crossfeed/', [('Tailscale-User-Login', login)])[0], 200)
        with self.assertRaises(ValueError):
            phone.open_loopback_server(80, login, snapshot)

    def test_operator_login_is_local_private_config(self):
        with tempfile.TemporaryDirectory(prefix='cf-') as root:
            path = Path(root) / 'phone.json'
            path.write_text(json.dumps({'operator_login': 'operator@example.invalid'}))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                phone.operator_login(path)
            path.chmod(0o600)
            self.assertEqual(phone.operator_login(path), 'operator@example.invalid')
            link = Path(root) / 'link.json'
            os.symlink(path, link)
            with self.assertRaises(OSError):
                phone.operator_login(link)
            Path(root).chmod(0o755)
            with self.assertRaises(ValueError):
                phone.open_server(Path(root) / 'phone.sock', 'operator@example.invalid', lambda: {})
            Path(root).chmod(0o700)

    def test_local_phone_route_keeps_the_existing_console_sign_in(self):
        with tempfile.TemporaryDirectory(prefix='cf-') as root:
            state = Path(root)
            overlay = state / 'overlay.json'
            overlay.write_text(json.dumps(base_overlay()))
            app = console.Console(overlay, state, 0)
            with serving(console.bind(app, 0)):
                connect = lambda: http.client.HTTPConnection('127.0.0.1', app.port, timeout=5)
                identity = [('Tailscale-User-Login', 'operator@example.invalid')]
                for path in ('/phone', '/settings', '/snapshot'):
                    self.assertEqual(request(connect(), 'GET', path, identity)[0], 401)
                _, headers, _ = request(connect(), 'GET', '/login?key=' + app.login_key)
                cookie = headers['Set-Cookie'].split(';', 1)[0]
                status, headers, body = request(connect(), 'GET', '/phone', [('Cookie', cookie)])
                self.assertEqual(status, 200)
                self.assertIn('Monthly subscriptions', body)
                self.assertEqual(headers['Content-Security-Policy'], phone.CSP)
                self.assertNotIn('<form', body)
                self.assertNotIn(app.form_token, body)


class ShareTests(unittest.TestCase):
    MODEL = {'unavailable': False, 'as_of': 1_800_000_000.0, 'plans': [
        {'id': 'claude', 'name': 'Claude <Max>', 'currency': 'EUR', 'monthly': '100.00', 'operator_percent': '50',
         'other_name': 'Sam', 'use': {'percent': 26, 'total_usd': 16.9, 'measured_usd': 9.8, 'rest_usd': 7.1,
                                        'measured_share': 58.0, 'basis': 'in the last 7 days', 'exact': True,
                                        'stale': False, 'overshoot': False, 'reason': None}}],
        'people': [{'key': 'me', 'name': 'Me', 'self': True, 'owed': {'EUR': '50.00'},
                    'plans': [{'plan': 'claude', 'percent': '50', 'monthly': '50.00'}]},
                   {'key': 'sam', 'name': 'Sam', 'self': False, 'owed': {'EUR': '50.00'},
                    'plans': [{'plan': 'claude', 'percent': '50', 'monthly': '50.00'}]}]}

    def test_share_cards_are_served_to_the_one_login_with_a_pinned_script_and_no_other_script(self):
        login = 'operator@example.invalid'
        identity = [('Tailscale-User-Login', login)]
        snapshot, share = Mock(return_value={'pools': [], 'subscription_spend': {'monthly': [], 'unpriced': 0, 'unavailable': False}}), Mock(return_value=self.MODEL)
        with tempfile.TemporaryDirectory(prefix='cf-') as root:
            path = Path(root) / 'phone.sock'
            with serving(phone.open_server(path, login, snapshot, share)):
                for route in ('/share', '/crossfeed/share', '/crossfeed/share/'):
                    self.assertEqual(request(UnixConnection(path), 'GET', route, [])[0], 403, route)
                share.assert_not_called()
                status, headers, body = request(UnixConnection(path), 'GET', '/crossfeed/share', identity)
                self.assertEqual(status, 200)
                self.assertIn(f"script-src 'sha256-{phone.SCRIPT_HASH}'", headers['Content-Security-Policy'])
                self.assertIn(f"style-src 'sha256-{phone.CSS_HASH}'", headers['Content-Security-Policy'])
                self.assertEqual(body.count('<script>'), 1)
                self.assertIn(f'<script>{phone.SHARE_SCRIPT}</script>', body)
                self.assertIn('Sam', body)
                self.assertIn('½ of €100.00 a month · yours €50.00', body)
                self.assertIn('Claude &lt;Max&gt;', body)
                self.assertNotIn('<Max>', body)
                self.assertIn('data-share="card-sam"', body)
                self.assertNotIn("script-src", request(UnixConnection(path), 'GET', '/crossfeed/', identity)[1]['Content-Security-Policy'])

    def test_without_a_share_source_the_route_does_not_exist_and_the_main_page_links_only_when_there_are_prices(self):
        login = 'operator@example.invalid'
        identity = [('Tailscale-User-Login', login)]
        spend = {'monthly': [{'currency': 'EUR', 'total': '12.00', 'operator': '6.00'}], 'unpriced': 0, 'unavailable': False}
        snapshot = Mock(return_value={'pools': [], 'subscription_spend': spend})
        with tempfile.TemporaryDirectory(prefix='cf-') as root:
            path = Path(root) / 'phone.sock'
            with serving(phone.open_server(path, login, snapshot)):
                self.assertEqual(request(UnixConnection(path), 'GET', '/crossfeed/share', identity)[0], 404)
                self.assertIn('href="share"', request(UnixConnection(path), 'GET', '/crossfeed/', identity)[2])
        empty = phone.render({'pools': [], 'subscription_spend': {'monthly': [], 'unpriced': 1, 'unavailable': False}})
        self.assertNotIn('href="share"', empty)

    def test_a_failing_share_read_is_a_plain_503_without_the_reason(self):
        login = 'operator@example.invalid'
        share = Mock(side_effect=RuntimeError('/Users/someone/private/path'))
        with tempfile.TemporaryDirectory(prefix='cf-') as root:
            path = Path(root) / 'phone.sock'
            with serving(phone.open_server(path, login, Mock(), share)):
                status, _, body = request(UnixConnection(path), 'GET', '/share', [('Tailscale-User-Login', login)])
                self.assertEqual(status, 503)
                self.assertNotIn('private', body)
