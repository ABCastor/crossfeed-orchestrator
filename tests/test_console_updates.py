"""Exercise the real HTTP handler without sockets, including its existing security suite.

The socket-based suite remains unchanged and still verifies real loopback binding
where allowed. This transport also works in a sandbox that refuses bind().
"""
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from tests import test_console as base

console = base.console
fleetctl = console.fleetctl


class ConsoleMemoryTests(base.ConsoleServerTests):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {'FLEET_NO_AUTO_REFRESH': '1'})
        cls.env.start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        cls.overlay = cls.state / 'overlay.json'
        cls.overlay.write_text(json.dumps(base.base_overlay()))
        cls.app = console.Console(cls.overlay, cls.state, 8768)
        cls.port = cls.app.port

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        cls.env.stop()

    def request(self, method, path, headers=None, body=None, host=None):
        payload = body.encode() if isinstance(body, str) else body or b''
        lines = [f'{method} {path} HTTP/1.1', f'Host: {host or "127.0.0.1:" + str(self.port)}',
                 'Connection: close', f'Content-Length: {len(payload)}']
        lines.extend(f'{key}: {value}' for key, value in (headers or {}).items())
        incoming = io.BytesIO(('\r\n'.join(lines) + '\r\n\r\n').encode() + payload)
        outgoing = io.BytesIO()

        class Handler(console.make_handler(self.app)):
            def setup(self):
                self.rfile, self.wfile = incoming, outgoing
            def finish(self):
                pass

        Handler(None, ('127.0.0.1', 1111), None)
        class ResponseSocket:
            def makefile(self, *args, **kwargs):
                return io.BytesIO(outgoing.getvalue())
        response = http.client.HTTPResponse(ResponseSocket())
        response.begin()
        return response, response.read()

    def test_binds_to_loopback_only(self):
        # This verifies the bind argument, not permission to bind a real socket.
        server = mock.Mock(server_address=('127.0.0.1', 8768))
        with mock.patch.object(console, '_Server', return_value=server) as constructor:
            console.bind(self.app, 8768)
        self.assertEqual(constructor.call_args.args[0], ('127.0.0.1', 8768))

    def test_ajax_level_acknowledges_persisted_setting_without_redirect(self):
        response, body = self.post_level('codex', 'high', headers={
            'Cookie': self.cookie(), 'Origin': self.origin(), 'Accept': 'application/json'})
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader('Location'))
        self.assertEqual(self.level_of('codex'), 'high')
        self.assertIn('codex', json.loads(body)['brief'])
        self.app.set_level('codex', 'normal')

    def test_preference_post_requires_all_existing_security_checks(self):
        model = base.base_overlay()['lanes'][0]['model_key']
        form = urllib.parse.urlencode({'model': model, 'preference': 'off', 't': self.app.form_token})
        cookie = self.cookie()
        for headers, status in [({'Origin': self.origin()}, 401),
                                ({'Cookie': cookie}, 403),
                                ({'Cookie': cookie, 'Origin': 'http://evil.example'}, 403),
                                ({'Cookie': cookie, 'Origin': self.origin(), 'Sec-Fetch-Site': 'cross-site'}, 403)]:
            response, _ = self.request('POST', '/preference', headers=headers, body=form)
            self.assertEqual(response.status, status)
        headers = {'Cookie': cookie, 'Origin': self.origin()}
        response, _ = self.request('POST', '/preference', headers=headers, body=form.replace(self.app.form_token, 'stale'))
        self.assertEqual(response.status, 403)
        response, _ = self.request('POST', '/preference', headers=headers, body=form, host='evil.example')
        self.assertEqual(response.status, 421)
        response, _ = self.request('POST', '/preference', headers=headers, body=form)
        self.assertEqual((response.status, response.getheader('Location')), (303, '/'))
        runtime = fleetctl.load_json(self.state / 'runtime.json')
        self.assertEqual(fleetctl.model_preference(runtime, model), 'off')
        self.app.set_preference(model, 'normal')

    def test_manual_refresh_post_has_same_origin_session_and_form_guards(self):
        form = urllib.parse.urlencode({'t': self.app.form_token})
        cookie = self.cookie()
        with mock.patch.object(self.app, 'refresh_now', return_value={'codex': 'refreshed'}) as refresh:
            for headers, status in [({'Origin': self.origin()}, 401), ({'Cookie': cookie}, 403),
                                    ({'Cookie': cookie, 'Origin': 'http://evil.example'}, 403),
                                    ({'Cookie': cookie, 'Origin': self.origin(), 'Sec-Fetch-Site': 'cross-site'}, 403)]:
                response, _ = self.request('POST', '/refresh', headers=headers, body=form)
                self.assertEqual(response.status, status)
            headers = {'Cookie': cookie, 'Origin': self.origin(), 'Accept': 'application/json'}
            response, _ = self.request('POST', '/refresh', headers=headers, body='t=stale')
            self.assertEqual(response.status, 403)
            response, _ = self.request('POST', '/refresh', headers=headers, body=form, host='evil.example')
            self.assertEqual(response.status, 421)
            refresh.assert_not_called()
            response, body = self.request('POST', '/refresh', headers=headers, body=form)
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.getheader('Location'))
            payload = json.loads(body)
            self.assertEqual(payload['refresh'], {'codex': 'refreshed'})
            self.assertIn('pools', payload)
            self.assertIn('stamp', payload)
            refresh.assert_called_once()

    def test_unknown_preference_or_model_does_not_write(self):
        before = self.overlay.read_bytes()
        for model, preference in [('nonexistent', 'off'), (base.base_overlay()['lanes'][0]['model_key'], 'turbo')]:
            form = urllib.parse.urlencode({'model': model, 'preference': preference, 't': self.app.form_token})
            response, _ = self.request('POST', '/preference', headers={
                'Cookie': self.cookie(), 'Origin': self.origin()}, body=form)
            self.assertEqual(response.status, 400)
        self.assertEqual(self.overlay.read_bytes(), before)

    def test_snapshot_is_private_and_script_policy_is_local_only(self):
        response, _ = self.request('GET', '/snapshot')
        self.assertEqual(response.status, 401)
        response, body = self.request('GET', '/snapshot', headers={'Cookie': self.cookie()})
        self.assertEqual(response.status, 200)
        self.assertIn('pools', json.loads(body))
        csp = response.getheader('Content-Security-Policy')
        self.assertIn("script-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn('unsafe-inline', csp)
        response, _ = self.request('GET', '/static/console.js')
        self.assertEqual(response.status, 200)


class SnapshotAndRenderTests(unittest.TestCase):
    def test_slow_refresh_does_not_block_page_or_switch_and_is_single_flight(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(3)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            overlay = state / 'overlay.json'
            overlay.write_text(json.dumps(base.base_overlay()))
            app = console.Console(overlay, state, 0)
            with mock.patch.object(fleetctl, 'refresh_stale_pools', side_effect=slow):
                try:
                    start = time.perf_counter()
                    page = console.render_page(app.overview(), 'test-token')
                    self.assertLess(time.perf_counter() - start, .3)
                    self.assertTrue(entered.wait(1))
                    start = time.perf_counter()
                    app.set_level('claude', 'high')
                    self.assertEqual(next(p for p in app.overview()['pools'] if p['pool'] == 'claude')['level'], 'high')
                    self.assertLess(time.perf_counter() - start, .2)
                    self.assertEqual(len(calls), 1)
                    self.assertIn('What agents read', page)
                finally:
                    release.set()
                    with app._refresh_lock:
                        pass

    def test_alphabetic_providers_euro_and_all_model_data(self):
        roster = base.base_overlay()
        roster['quota_pools']['claude']['plan']['currency'] = 'EUR'
        roster['lanes'][0].update(context_window=123456, cost_class='low', evidence_confidence='high')
        roster['model_evidence'] = {roster['lanes'][0]['model_key']: {'note': '<evidence>'}}
        with tempfile.TemporaryDirectory() as directory:
            overview = fleetctl.fleet_overview(roster, {}, Path(directory))
            page = console.render_page(overview, 'test-token')
        labels = [p['label'] for p in overview['pools']]
        self.assertEqual(labels, sorted(labels, key=str.casefold))
        for text in ['€200', '123456', 'Cost class', 'Evidence confidence', '&lt;evidence&gt;',
                     'app on your own screen keeps its own model picker.', 'class="product-mark"', 'class="compact-brand"']:
            self.assertIn(text, page)
        self.assertNotIn('id="models"', page)
        self.assertIn('class="pick"', page)
        for model in overview['models']:   # every model the roster knows is on the page, under its provider
            if model['pools'] and not model['card'].get('hidden'):
                self.assertIn(f'data-model="{model["model"]}"', page)
        self.assertNotIn(' style=', page)


if __name__ == '__main__':
    unittest.main()
