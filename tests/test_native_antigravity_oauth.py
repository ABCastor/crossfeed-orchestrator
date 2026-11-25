"""Fixture tests of the optional signed-in Antigravity quota-only endpoint."""
import base64
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs
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
        first = {'buckets': [star, bucket('gemini-weekly', .9182), bucket('gemini-unrelated-weekly', .75)]}
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
        self.assertEqual(result["provider_used_percent"][prefix + "gemini-weekly"], 8.18)
        filtered = fleetctl._filtered_observation(result, {"windows": [prefix + "gemini-weekly"]})
        self.assertEqual(filtered["provider_used_percent"], {prefix + "gemini-weekly": 8.18})
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

    def test_acp_exchange_reads_all_families_without_starting_processes_or_writing_credentials(self):
        config = {"oauth_acp_file": str(self.file), "reuse_running": True}
        fields = {"client_id": "fixture-id", "client_secret": "fixture-secret",
                  "refresh_token": "fixture-refresh+token", "project_id": "fixture-project"}
        self.file.write_text(json.dumps(fields))
        self.file.chmod(0o600)
        original = self.file.read_bytes()
        buckets = {"buckets": [
            {"bucketId": bucket, "remainingFraction": .75, "resetTime": "2026-11-25T12:00:00Z"}
            for bucket in ("gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h", "gemini-argon-wtu-weekly")
        ]}
        with patch.object(native, "_now", return_value=NOW), \
                patch.object(native.subprocess, "run") as spawn, \
                patch.object(native, "_agy_google_post", side_effect=[
                    (200, {"access_token": "fixture-grant", "refresh_token": "not-saved"}), (200, buckets), (403, None),
                ]) as request:
            result = native.observe_antigravity(config)
        spawn.assert_not_called()
        token_post, *quota_posts = [call.args for call in request.call_args_list]
        self.assertEqual(token_post[:2], ("oauth2.googleapis.com", "/token"))
        form = parse_qs(token_post[2].decode())
        self.assertEqual(form, {k: [v] for k, v in fields.items() if k != "project_id"}
                         | {"grant_type": ["refresh_token"]})
        self.assertNotIn("Authorization", token_post[3])
        for host, path, body, headers, _ in quota_posts:
            self.assertIn(host, native.AGY_OAUTH_HOSTS)
            self.assertEqual(path, native.AGY_OAUTH_QUOTA)
            self.assertEqual(json.loads(body), {"project": "fixture-project"})
            self.assertEqual(headers["Authorization"], "Bearer fixture-grant")
            self.assertNotIn("Cookie", headers)
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(set(result["windows"]), {
            "antigravity-quota-summary-" + key
            for key in ("gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h", "argon-weekly")
        })
        self.assertNotIn("fixture-", json.dumps(result))
        self.assertNotIn("not-saved", json.dumps(result))
        self.assertNotEqual(fleetctl._source_identity({"oracle": "antigravity", **config}),
                            fleetctl._source_identity(self.config))

    def test_acp_profiles_fail_closed_without_sending_any_other_credential(self):
        config = {"oauth_acp_file": str(self.file), "reuse_running": True}
        self.file.write_text(json.dumps({"client_id": "fixture-id", "client_secret": "fixture-secret",
                                         "refresh_token": "fixture-refresh"}))
        self.file.chmod(0o600)
        with patch.object(native, "_agy_google_post", return_value=(400, None)) as request, \
                patch.object(native, "_observe_running_antigravity") as running:
            self.assertEqual(native.observe_antigravity({**config, **self.config})["kind"], "no-auth")
            self.assertEqual(native.observe_antigravity(config)["kind"], "unauthorized")
            request.assert_called_once()
            request.reset_mock()
            self.file.chmod(0o644)
            self.assertEqual(native.observe_antigravity(config)["kind"], "no-auth")
            request.assert_not_called()
        running.assert_not_called()

    def test_only_asked_for_cli_keychain_entry_is_read_and_never_renewed(self):
        config = {"oracle": "antigravity", "oauth_cli_keychain": True}
        token = json.dumps({"token": {"access_token": "fixture-only",
                                     "expiry": (NOW + dt.timedelta(minutes=12)).isoformat()}}).encode()
        buckets = {"buckets": [{"bucketId": "3p-5h", "remainingFraction": 1}]}
        with patch.object(native.Path, "is_file", return_value=True), \
                patch.object(native, "_now", return_value=NOW), \
                patch.object(native.subprocess, "run") as security, \
                patch.object(native, "_agy_google_post", return_value=(200, buckets)) as rpc:
            security.return_value.returncode = 0
            for raw in (token, b"go-keyring-base64:" + base64.b64encode(token),
                        b"go-keyring-encoded:" + token.hex().encode()):
                security.reset_mock()
                rpc.reset_mock()
                security.return_value.stdout = raw
                result = native.observe_antigravity(config)
                self.assertIn("antigravity-quota-summary-3p-5h", result["idle_windows"])
                security.assert_called_once()
                self.assertEqual(security.call_args.args[0], [
                    "/usr/bin/security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w",
                ])
                self.assertEqual([call.args[0] for call in rpc.call_args_list], list(native.AGY_OAUTH_HOSTS))
            rpc.reset_mock()
            security.return_value.stdout = b"go-keyring-base64:?"
            self.assertEqual(native.observe_antigravity(config)["kind"], "no-auth")
            rpc.assert_not_called()
        self.assertNotEqual(fleetctl._source_identity(config), fleetctl._source_identity(self.config))

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
        self.assertEqual(connection.request.call_args.args[3]["User-Agent"], "antigravity/hub/2.24.0")
        connection.request.assert_called_once()
        reply.read.assert_not_called()
        connection.close.assert_called_once()
