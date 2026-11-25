"""Fixture tests of the optional signed-in Antigravity quota-only endpoint."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tests.test_fleetctl import fleetctl

native = fleetctl.native_usage
NOW = dt.datetime(2026, 11, 25, 10, 0, tzinfo=dt.timezone.utc)


class AntigravityOAuthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / 'operator-session.json'
        self.config = {'oracle': 'antigravity', 'oauth_session_file': str(self.file), 'reuse_running': True}

    def store(self, expiry):
        self.file.write_text(json.dumps({'token': {'access_token': 'fixture-only', 'expiry': expiry.isoformat()}}))
        self.file.chmod(0o600)

    def test_one_selected_login_returns_three_separate_quota_families(self):
        self.store(NOW + dt.timedelta(minutes=10))
        star = {'bucketId': 'gemini-5h', 'window': '5h', 'remainingFraction': 1,
                'resetTime': (NOW + dt.timedelta(hours=5)).isoformat()}
        def bucket(id, fraction):
            return {'bucketId': id, 'window': 'weekly', 'remainingFraction': fraction,
                    'resetTime': (NOW + dt.timedelta(days=2)).isoformat()}
        first = {'buckets': [star, bucket('gemini-weekly', .92), bucket('gemini-unrelated-weekly', .75)]}
        second = {'groups': [{'buckets': [bucket('gemini-weekly', .75), bucket('3p-weekly', .82),
                                         bucket('gemini-argon-wtu-weekly', .5)]}]}
        with patch.object(native, '_now', return_value=NOW), patch.object(native.subprocess, 'run') as spawn, \
                patch.object(native, '_agy_oauth_request', side_effect=(first, second)) as rpc:
            result = native.observe_antigravity(self.config)
        spawn.assert_not_called()
        self.assertEqual([call.args[0] for call in rpc.call_args_list], list(native.AGY_OAUTH_HOSTS))
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ['fixture-only', 'fixture-only'])
        prefix = 'antigravity-quota-summary-'
        self.assertEqual({k.removeprefix(prefix): row['used_percent'] for k, row in result['windows'].items()},
                         {'gemini-weekly': 8, '3p-weekly': 18, 'argon-weekly': 50})
        self.assertIn(prefix + 'gemini-5h', result['idle_windows'])
        self.assertNotIn('fixture-only', json.dumps(result))
        other = {**self.config, 'oauth_session_file': str(self.file.with_name('second.json'))}
        self.assertNotEqual(fleetctl._source_identity(self.config), fleetctl._source_identity(other))

    def test_expired_missing_or_unprivate_login_never_calls_provider_or_falls_back(self):
        with patch.object(native, '_now', return_value=NOW), patch.object(native, '_agy_oauth_request') as rpc, \
                patch.object(native, '_observe_running_antigravity') as running:
            self.assertEqual(native.observe_antigravity(self.config)['kind'], 'no-auth')
            self.store(NOW + dt.timedelta(seconds=20))
            self.assertEqual(native.observe_antigravity(self.config)['kind'], 'expired')
            self.file.chmod(0o644)
            self.assertEqual(native.observe_antigravity(self.config)['kind'], 'no-auth')
        rpc.assert_not_called()
        running.assert_not_called()

    def test_https_summary_post_has_no_redirect_cookie_or_refresh_fallback(self):
        connection = MagicMock()
        reply = connection.getresponse.return_value.__enter__.return_value
        reply.status = 302
        host = native.AGY_OAUTH_HOSTS[0]
        with patch.object(native.http.client, 'HTTPSConnection', return_value=connection) as open_tls:
            self.assertIsNone(native._agy_oauth_request(host, 'fixture-only', 2))
        open_tls.assert_called_once_with(host, timeout=2)
        self.assertEqual(connection.request.call_args.args[:3], ('POST', native.AGY_OAUTH_QUOTA, b'{}'))
        self.assertEqual(connection.request.call_args.args[3]['Authorization'], 'Bearer fixture-only')
        self.assertNotIn('Cookie', connection.request.call_args.args[3])
        connection.request.assert_called_once()
        reply.read.assert_not_called()
        connection.close.assert_called_once()
