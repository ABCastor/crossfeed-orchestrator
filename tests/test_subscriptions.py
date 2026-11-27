"""Private ledger, signed write path and the tray's public JSON contract."""
import http.client
import json
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from tests.test_console import console

ledger = console.fleetctl.subscriptions
ROOT = Path(__file__).resolve().parents[1]


def plan(name):
    return {"plan": {"name": name, "billing": "subscription", "monthly_usd": 999}}


ROSTER = {"schema_version": 3, "quota_pools": {
    "claude": {**plan("Example Claude"), "quota_refresh": {"oracle": "claude"},
               "value_capacity": {"weekly_usd_equivalent": 40, "source": "fixture"}},
    "opencode-go": plan("Example Go"), "antigravity-gemini": plan("Example AGY"),
    "antigravity-3p": plan("Example AGY siblings"), "antigravity-argon": plan("Example AGY Argon"),
    "openrouter-free": {"plan": {"billing": "free"}},
}, "lanes": []}


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.overlay = self.root / 'overlay.json'
        self.overlay.write_text(json.dumps(ROSTER))
        self.path = ledger.path_for(self.overlay)

    def test_save_prefills_unknowns_and_keeps_currencies_shares_and_pools_distinct(self):
        original = self.overlay.read_bytes()
        self.assertEqual(ledger.view(self.path, ROSTER, {})['unpriced'], 3)
        self.assertFalse(self.path.exists(), 'read-only views do not write')
        ledger.save(self.path, ROSTER, 'claude', {'amount': '120', 'currency': 'eur', 'period': 'yearly',
                                                   'operator_share_percent': '40', 'other_payer': 'Other payer',
                                                   'renewal': '2028-02-29'})
        ledger.save(self.path, ROSTER, 'antigravity', {'amount': '20', 'currency': 'USD', 'period': 'monthly'})
        data = ledger.view(self.path, ROSTER, {})
        self.assertEqual(data['monthly'], [{'currency': 'EUR', 'operator': '4.00', 'total': '10.00'},
                                          {'currency': 'USD', 'operator': None, 'total': '20.00'}])
        self.assertEqual((data['unpriced'], data['unsplit']), (1, 1))
        rows = {row['id']: row for row in data['subscriptions']}
        self.assertEqual(rows['claude']['other_share_percent'], '60')
        self.assertEqual(rows['claude']['renewal'], '2028-02-29')
        self.assertIsNone(rows['opencode-go']['amount'])
        self.assertEqual(set(rows['antigravity']['pools']),
                         {'antigravity-gemini', 'antigravity-3p', 'antigravity-argon'})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.overlay.read_bytes(), original)

    def test_invalid_edits_do_not_overwrite_valid_data_and_permissive_configs_are_refused(self):
        ledger.save(self.path, ROSTER, 'claude', {'amount': '0', 'currency': 'EUR', 'period': 'monthly'})
        self.assertEqual(ledger.view(self.path, ROSTER, {})['monthly'][0]['total'], '0.00')
        original = self.path.read_bytes()
        for bad in ({'amount': '1e2'}, {'amount': '5'}, {'operator_share_percent': '101'},
                    {'renewal': '2027-02-29'}, {'currency': '<x>'}):
            with self.assertRaises(ValueError):
                ledger.save(self.path, ROSTER, 'claude', bad)
            self.assertEqual(self.path.read_bytes(), original)
        self.path.chmod(0o644)
        self.assertTrue(ledger.view(self.path, ROSTER, {})['unavailable'])
        with self.assertRaises(ValueError):
            ledger.save(self.path, ROSTER, 'claude', {})

    def test_value_requires_a_direct_unscoped_weekly_window(self):
        limit = {'kind': 'window', 'minutes': 10080, 'used_percent': 25, 'reset_at': '2030-01-01T00:00:00Z'}
        pool = {'pool': 'claude', 'quota': {'confidence': 'direct'}, 'limits': [limit]}
        value = lambda: ledger.view(self.path, ROSTER, {'pools': [pool]})['subscriptions'][1]['pool_value']
        self.assertEqual(value()['claude']['used_usd_equivalent'], '10.00')
        limit['scope'] = 'narrow-model'
        self.assertEqual(value(), {})
        limit['scope'] = None
        pool['gauge_error'] = 'not fresh'
        self.assertEqual(value(), {})

    def test_glance_json_schema_and_privacy(self):
        ledger.save(self.path, ROSTER, 'claude', {'amount': '12.30', 'currency': 'EUR', 'period': 'monthly',
                                                   'operator_share_percent': '50', 'other_payer': 'fixture-person'})
        cmd = [sys.executable, str(ROOT / 'scripts/fleetctl.py'), '--overlay', str(self.overlay),
               '--state-dir', str(self.root), 'glance', '--no-refresh']
        raw = subprocess.check_output([*cmd, '--json'], text=True)
        self.assertEqual(json.loads(raw), json.loads(subprocess.check_output([*cmd, '--format', 'json'])))
        self.assertNotIn('fixture-person', raw)
        body = json.loads(raw)
        self.assertEqual(set(body), {'schema', 'title', 'pools', 'readings', 'reading_origin', 'subscription_spend', 'subscriptions'})
        self.assertNotIn('fixture-person', json.dumps(body['subscriptions']))
        self.assertEqual(body['schema'], 1)
        self.assertIn('openrouter-free', {p['pool'] for p in body['pools']})
        pool = next(row for row in body['pools'] if row['pool'] == 'claude')
        self.assertTrue(pool['measured'])
        self.assertIsNone(pool['used_percent'])
        self.assertIn('limits', pool)
        self.assertEqual(body['subscription_spend'], {
            'monthly': [{'currency': 'EUR', 'operator': '6.15', 'total': '12.30'}],
            'unpriced': 2, 'unsplit': 0, 'unavailable': False})

    def test_console_write_requires_session_origin_and_form_token(self):
        app = console.Console(self.overlay, self.root, 0)
        with console.bind(app, 0) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def request(method, path, body=None, **headers):
                    with_connection = http.client.HTTPConnection('127.0.0.1', app.port, timeout=5)
                    try:
                        with_connection.request(method, path, body, headers)
                        reply = with_connection.getresponse()
                        return reply.status, dict(reply.getheaders()), reply.read()
                    finally:
                        with_connection.close()

                route = '/settings/subscriptions'
                origin = f'http://127.0.0.1:{app.port}'
                _, headers, _ = request('GET', '/login?key=' + app.login_key)
                cookie = headers['Set-Cookie'].split(';', 1)[0]
                fields = {'id': 'claude', 'amount': '10', 'period': 'monthly', 'currency': 'EUR'}
                self.assertEqual(request('POST', route, urlencode(fields), Origin=origin)[0], 401)
                self.assertEqual(request('POST', route, urlencode(fields), Cookie=cookie, Origin=origin)[0], 403)
                self.assertFalse(self.path.exists())
                fields['t'] = app.form_token
                self.assertEqual(request('POST', route, urlencode(fields), Cookie=cookie)[0], 403)
                status, response_headers, _ = request('POST', route, urlencode(fields), Cookie=cookie, Origin=origin)
                self.assertEqual(status, 303)
                self.assertEqual(response_headers['Location'], '/settings#subscriptions')
                self.assertEqual(ledger.view(self.path, ROSTER, {})['monthly'][0]['total'], '10.00')
            finally:
                server.shutdown()
                worker.join(timeout=5)
