"""Native quota readers: each provider's own answer becomes a quota snapshot or an explicit unknown.

Fixtures in tests/fixtures/native-usage are answers in the shapes the providers returned on
29 Oct 2026 (Codex app server 0.165, Anthropic OAuth usage, Antigravity CLI 1.4.10, GitHub
Copilot entitlement), with account ids, logins and balances removed. No real token is used:
every network, keychain and process call is replaced.
"""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tests.test_fleetctl import fleetctl

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "native-usage"
native = fleetctl.native_usage
NOW = dt.datetime(2026, 10, 29, 21, 0, tzinfo=dt.timezone.utc)
TOKEN = "placeholder-token-0123456789"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class Response:
    def __init__(self, text, status=200):
        self._data = text.encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self._data if limit < 0 else self._data[:limit]


def answering(text="", status=200, raise_exc=None, seen=None):
    def opener(request, timeout=5):
        if seen is not None:
            seen.append((request, timeout))
        if raise_exc is not None:
            raise raise_exc
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(text.encode()))
        return Response(text, status)
    return opener


def claude_record(expires_in_s=3600, tier="default_claude_max_20x"):
    return {"claudeAiOauth": {"accessToken": TOKEN, "refreshToken": "never-used",
                              "expiresAt": int((dt.datetime.now(dt.timezone.utc).timestamp() + expires_in_s) * 1000),
                              "subscriptionType": "max", "rateLimitTier": tier}}


class CodexTests(unittest.TestCase):
    def test_weekly_window_is_named_by_length_not_slot(self):
        snap = native.parse_codex(json.loads(fixture("codex-rate-limits.json")), NOW)
        self.assertTrue(snap["available"])
        self.assertEqual(snap["source"], "codex-app-server")
        self.assertEqual(snap["windows"]["weekly"], {"used_percent": 100, "window_minutes": 10080,
                                                     "label": "Weekly", "reset_at": "2026-11-04T06:53:04Z"})
        self.assertNotIn("session", snap["windows"])          # no five-hour window reported: none invented
        self.assertEqual(snap["plan"], "Pro")

    def test_model_bucket_is_scoped_to_its_model(self):
        snap = native.parse_codex(json.loads(fixture("codex-rate-limits.json")), NOW)
        self.assertEqual(snap["windows"]["codex_bengalfox-session"]["label"], "GPT-5.3-Codex-Spark only")
        self.assertEqual(snap["windows"]["codex_bengalfox-weekly"]["used_percent"], 40)

    def test_exchange_failures_stay_unknown(self):
        for kind, reason in [("no-tool", "Codex is not installed (no `codex` command)"),
                             ("unauthorized", "Codex is not signed in"),
                             ("failed", "Codex app server gave no usage within 20s")]:
            got = native.observe_codex({}, exchange=lambda binary, timeout: (kind, reason))
            self.assertEqual((got["available"], got["kind"], got["reason"]), (False, kind, reason))
            self.assertNotIn("windows", got)
        got = native.observe_codex({}, exchange=lambda b, t: ("ok", {"rateLimits": None}))
        self.assertEqual(got["kind"], "malformed")

    def test_real_exchange_with_a_fake_app_server(self):
        """The bounded stdio session against a stand-in server: handshake, answer, child ended."""
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            server = Path(folder) / "codex"
            answer = json.dumps({"id": 2, "result": json.loads(fixture("codex-rate-limits.json"))})
            server.write_text(f"""#!{sys.executable}
import sys, json
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("id") == 1:
        print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
    if msg.get("id") == 2:
        print(json.dumps({{"method": "notice"}}), flush=True)
        print({answer!r}, flush=True)
""")
            server.chmod(0o700)
            got = native.observe_codex({"binary": str(server)})
            self.assertTrue(got["available"], got)
            self.assertEqual(got["windows"]["weekly"]["used_percent"], 100)
            silent = Path(folder) / "silent"
            silent.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
            silent.chmod(0o700)
            got = native.observe_codex({"binary": str(silent), "timeout_s": 1})
            self.assertEqual((got["available"], got["kind"]), (False, "failed"))
            missing = native.observe_codex({"binary": str(Path(folder) / "absent")})
            self.assertEqual(missing["kind"], "no-tool")


class ClaudeTests(unittest.TestCase):
    def test_limits_become_session_weekly_and_scoped_weekly(self):
        snap = native.parse_claude(fixture("claude-usage.json"), "Claude Max 20x", NOW)
        self.assertEqual(snap["windows"]["session"]["used_percent"], 5)
        self.assertEqual(snap["windows"]["session"]["window_minutes"], 300)
        self.assertEqual(snap["windows"]["weekly"]["used_percent"], 26)
        self.assertEqual(snap["windows"]["weekly"]["reset_at"], "2026-11-03T13:00:00Z")
        self.assertEqual(snap["windows"]["claude-weekly-scoped-fable"]["label"], "Fable only")
        self.assertEqual(snap["plan"], "Claude Max 20x")

    def test_older_answer_without_limits_list(self):
        snap = native.parse_claude(fixture("claude-usage-legacy.json"))
        self.assertEqual({k: v["used_percent"] for k, v in snap["windows"].items()}, {"session": 48, "weekly": 3})

    def test_legacy_scoped_limits_do_not_replace_general_windows(self):
        expiry = "2026-11-03T13:00:00Z"
        body = {"seven_day": {"utilization": 9, "resets_at": expiry},
                "seven_day_sonnet": {"utilization": 100, "resets_at": expiry},
                "seven_day_opus": {"utilization": 12, "resets_at": expiry},
                "seven_day_cowork": {"utilization": 0}}
        snap = native.parse_claude(json.dumps(body), observed_at=NOW)
        self.assertEqual({k: v["used_percent"] for k, v in snap["windows"].items()},
                         {"weekly": 9, "claude-weekly-scoped-sonnet": 100, "claude-weekly-scoped-opus": 12})
        self.assertEqual(snap["idle_windows"]["claude-weekly-scoped-cowork"]["used_percent"], 0)
        self.assertFalse(fleetctl.quota_window_applies({}, "claude", "claude-weekly-scoped-sonnet",
                                                      snap["windows"]["claude-weekly-scoped-sonnet"], "claude-opus-5-5"))

    def test_reader_ignores_bridge_token_config_and_non_subscription_records(self):
        api_login = claude_record()
        api_login["claudeAiOauth"].update(subscriptionType="console", accessToken="not-a-subscription")
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "bridge-only",
                                     "ANTHROPIC_API_KEY": "api-only", "CLAUDE_CONFIG_DIR": "/bridge"}), \
                patch.object(native.Path, "home", return_value=Path("/operator")), \
                patch.object(native.shutil, "which", return_value="/usr/bin/security"), \
                patch.object(native.subprocess, "run") as keychain, \
                patch.object(native, "_owner_file", return_value=json.dumps(claude_record()).encode()) as read:
            keychain.return_value.returncode = 0
            keychain.return_value.stdout = json.dumps(api_login)
            token, plan, failure = native.claude_token()
        read.assert_called_once_with(Path("/operator/.claude/.credentials.json"))
        self.assertEqual((token, plan, failure), (TOKEN, "Claude Max 20x", None))
        self.assertEqual(native.claude_token([api_login])[2]["kind"], "no-key")

    def test_no_http_redirect_or_proxy_for_subscription_token(self):
        with patch.object(native.urllib.request, "build_opener") as build:
            build.return_value.open = answering(status=302)
            result = native.observe_claude({}, records=[claude_record()])
        proxy, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))
        self.assertEqual(result["kind"], "failed")

    def test_scoped_window_applies_only_to_its_model(self):
        roster = {"quota_pools": {"claude": {"label": "Claude"}},
                  "model_cards": {"claude-fable-5-1": {}, "claude-opus-5-5": {}}}
        snap = native.parse_claude(fixture("claude-usage.json"))
        window = snap["windows"]["claude-weekly-scoped-fable"]
        self.assertFalse(fleetctl.quota_window_applies(roster, "claude", "claude-weekly-scoped-fable", window))
        self.assertTrue(fleetctl.quota_window_applies(roster, "claude", "claude-weekly-scoped-fable", window, "claude-fable-5-1"))
        self.assertTrue(fleetctl.quota_window_applies(roster, "claude", "weekly", snap["windows"]["weekly"]))

    def test_reads_with_the_unexpired_sign_in_and_sends_it_only_in_the_header(self):
        seen = []
        got = native.observe_claude({}, opener=answering(fixture("claude-usage.json"), seen=seen),
                                    records=[claude_record(-60), claude_record(3600)])
        self.assertTrue(got["available"])
        request, timeout = seen[0]
        self.assertEqual(request.full_url, native.CLAUDE_USAGE_URL)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("Anthropic-beta"), "oauth-2025-04-20")
        self.assertGreaterEqual(timeout, 10)
        self.assertNotIn(TOKEN, json.dumps(got))

    def test_expired_or_missing_sign_in_is_reported_never_renewed(self):
        def opener(*a, **kw):
            raise AssertionError("no request without a valid sign-in")
        got = native.observe_claude({}, opener=opener, records=[claude_record(-60)])
        self.assertEqual((got["available"], got["kind"]), (False, "expired"))
        got = native.observe_claude({}, opener=opener, records=[])
        self.assertEqual(got["kind"], "no-key")

    def test_http_failures_are_distinct(self):
        for status, kind in [(401, "unauthorized"), (403, "unauthorized"), (429, "rate-limited"), (500, "failed")]:
            got = native.observe_claude({}, opener=answering('{"error":{"message":"x"}}', status=status),
                                        records=[claude_record()])
            self.assertEqual((got["available"], got["kind"]), (False, kind))
        got = native.observe_claude({}, opener=answering(raise_exc=urllib.error.URLError(f"secret {TOKEN}")),
                                    records=[claude_record()])
        self.assertEqual(got["kind"], "failed")
        self.assertNotIn(TOKEN, got["reason"])
        got = native.observe_claude({}, opener=answering("<html>sign in</html>"), records=[claude_record()])
        self.assertEqual(got["kind"], "malformed")


class AntigravityTests(unittest.TestCase):
    def test_every_family_keeps_its_own_windows(self):
        # An hour into the 5-hour windows, so every bucket has a running clock.
        snap = native.parse_antigravity(fixture("agy-usage.json"), NOW + dt.timedelta(hours=1))
        used = {k: v["used_percent"] for k, v in snap["windows"].items()}
        self.assertEqual(used, {
            "antigravity-quota-summary-gemini-weekly": 1, "antigravity-quota-summary-gemini-5h": 0,
            "antigravity-quota-summary-3p-weekly": 51, "antigravity-quota-summary-3p-5h": 0,
            "antigravity-quota-summary-argon-weekly": 100})
        self.assertNotIn("idle_windows", snap)
        self.assertEqual(snap["windows"]["antigravity-quota-summary-3p-weekly"]["label"], "Claude/GPT weekly")
        self.assertEqual(snap["windows"]["antigravity-quota-summary-argon-weekly"]["label"], "Argon weekly")
        self.assertEqual(snap["windows"]["antigravity-quota-summary-gemini-5h"]["window_minutes"], 300)

    def test_untouched_five_hour_window_has_not_started(self):
        # Read at the moment the fixture was taken: an unused 5-hour bucket resets exactly 5 h later.
        taken = dt.datetime(2026, 10, 29, 21, 7, 7, tzinfo=dt.timezone.utc)
        snap = native.parse_antigravity(fixture("agy-usage.json"), taken)
        self.assertNotIn("antigravity-quota-summary-gemini-5h", snap["windows"])
        self.assertEqual(snap["idle_windows"]["antigravity-quota-summary-gemini-5h"],
                         {"used_percent": 0, "window_minutes": 300, "label": "Gemini 5-hour"})
        self.assertIn("antigravity-quota-summary-gemini-weekly", snap["windows"])

    def test_a_model_answer_is_never_read_as_usage(self):
        self.assertIsNone(native.parse_antigravity(fixture("agy-not-usage.json")))
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "export.json"
            report.write_text(fixture("agy-not-usage.json"))
            got = native.observe_antigravity({"path": str(report)})
            self.assertEqual((got["available"], got["kind"]), (False, "malformed"))

    def test_rejects_future_mtime_oversize_and_special_files(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "export.json"
            report.write_text(fixture("agy-usage.json"))
            future = native._now().timestamp() + 120
            os.utime(report, (future, future))
            cfg = {"path": str(report)}
            self.assertIn("capture time", native.observe_antigravity(cfg)["reason"])
            report.write_bytes(b" " * (native.MAX_BODY_BYTES + 1))
            self.assertEqual(native.observe_antigravity(cfg)["kind"], "malformed")
            fifo = Path(folder) / "not-a-report"
            os.mkfifo(fifo)
            self.assertIn("not a regular file", native.observe_antigravity({"path": str(fifo)})["reason"])
            alias = Path(folder) / "alias.json"
            alias.symlink_to(report)
            self.assertFalse(native.observe_antigravity({"path": str(alias)})["available"])


class CopilotTests(unittest.TestCase):
    def test_premium_allowance_with_monthly_reset(self):
        snap = native.parse_copilot(fixture("copilot-user.json"), NOW)
        self.assertEqual(snap["windows"], {"premium_monthly": {
            "used_percent": 1, "window_minutes": 31 * 1440, "label": "Premium", "reset_at": "2026-11-01T00:00:00Z"}})
        self.assertEqual(snap["plan"], "Copilot Individual")

    def test_raw_percent_is_visible_without_changing_the_routing_precision(self):
        payload = json.loads(fixture("copilot-user.json"))
        payload["quota_snapshots"]["premium_interactions"]["percent_remaining"] = 99.8
        snap = native.parse_copilot(json.dumps(payload), NOW)
        self.assertEqual(snap["windows"]["premium_monthly"]["used_percent"], 0)
        self.assertEqual(snap["provider_used_percent"]["premium_monthly"], .2)

    def test_unlimited_is_not_a_window(self):
        self.assertIsNone(native.parse_copilot(fixture("copilot-unlimited.json")))

    def test_sends_github_token_and_classifies_failures(self):
        seen = []
        got = native.observe_copilot({}, opener=answering(fixture("copilot-user.json"), seen=seen), token=TOKEN)
        self.assertTrue(got["available"])
        self.assertEqual(seen[0][0].get_header("Authorization"), f"token {TOKEN}")
        for status, kind in [(401, "unauthorized"), (404, "no-subscription"), (502, "failed")]:
            got = native.observe_copilot({}, opener=answering("{}", status=status), token=TOKEN)
            self.assertEqual(got["kind"], kind)

    def test_signed_out_github_cli(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            gh = Path(folder) / "gh"
            gh.write_text("#!/bin/sh\necho 'not logged in' >&2\nexit 1\n")
            gh.chmod(0o700)
            got = native.observe_copilot({"gh": str(gh)}, opener=answering(fixture("copilot-user.json")))
            self.assertEqual((got["available"], got["kind"]), (False, "no-key"))


class CodexBarPortTests(unittest.TestCase):
    def test_codex_oauth_matches_recorded_weekly_and_does_not_spawn(self):
        auth = json.dumps({"tokens": {"access_token": TOKEN, "account_id": "test-account"}}).encode()
        def usage(request, timeout):
            self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/wham/usage")
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
            self.assertEqual(request.get_header("Chatgpt-account-id"), "test-account")
            return Response(fixture("codex-wham-usage.json"))
        with patch.object(native, "_owner_file", return_value=auth), \
                patch.object(native.subprocess, "Popen") as spawn:
            snap = native.observe_codex_oauth({}, opener=usage)
        spawn.assert_not_called()
        self.assertEqual(snap["plan"], "Pro")
        self.assertEqual(snap["windows"], {"weekly": {"used_percent": 66, "window_minutes": 10080,
                                                      "reset_at": "2026-11-22T10:39:25Z", "label": "Weekly"}})
        self.assertNotIn(TOKEN, json.dumps(snap))

    def test_credit_balance_is_read_but_not_used_as_a_window(self):
        payload = {"credits": {"balance": "1234.5", "unlimited": False}}
        snap = native.parse_codex_oauth(payload, NOW)
        self.assertEqual(snap["account_usage"], {"credits_balance": 1234.5})
        self.assertEqual(snap["windows"], {})
        payload["credits"]["unlimited"] = True
        self.assertIsNone(native.parse_codex_oauth(payload, NOW))

    def test_codex_oauth_preserves_scoped_signal_and_ignores_invalid_slots(self):
        answer = json.loads(fixture("codex-wham-usage.json"))
        answer["rate_limit"]["secondary_window"] = {"used_percent": 0, "reset_at": 0, "limit_window_seconds": 18000}
        answer["additional_rate_limits"] = [42, {"limit_name": "Spark", "metered_feature": "codex-spark",
            "rate_limit": {"primary_window": {"used_percent": 100, "reset_at": 1795343965,
                                              "limit_window_seconds": 18000}}}]
        windows = native.parse_codex_oauth(answer)["windows"]
        self.assertEqual(set(windows), {"weekly", "codex-spark-session"})
        self.assertEqual(windows["codex-spark-session"]["label"], "Spark only")
        self.assertEqual(windows["codex-spark-session"]["used_percent"], 100)

    def test_antigravity_local_response_keeps_three_families_and_ignores_disabled(self):
        payload = json.loads(fixture("agy-local-quota.json"))
        summary = native.parse_antigravity_local(payload, NOW)
        key = "antigravity-quota-summary-"
        self.assertEqual({k: v["used_percent"] for k, v in summary["windows"].items()},
                         {key + "gemini-5h": 9, key + "gemini-weekly": 18,
                          key + "3p-weekly": 36, key + "argon-weekly": 3})
        self.assertEqual(summary["windows"][key + "gemini-weekly"]["window_minutes"], 10080)
        self.assertEqual(summary["windows"][key + "argon-weekly"]["reset_at"], "2026-06-20T00:39:55Z")
        self.assertEqual(summary["idle_windows"][key + "3p-5h"]["used_percent"], 0)
        payload["code"] = "unauthenticated"
        self.assertIsNone(native.parse_antigravity_local(payload))

    def test_antigravity_only_calls_verified_same_uid_loopback_listener(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(Path, "home", return_value=Path(folder)):
            agy = (Path(folder) / ".local/bin/agy").resolve()
            uid = os.getuid()
            calls = []
            def run(argv, **kwargs):
                calls.append(argv[0])
                if argv[:2] == ["/bin/ps", "-axww"]:
                    text = f"7 {uid + 1} {agy}\n8 {uid} {agy}\n9 {uid} /tmp/agy\n"
                elif argv[0] == "/bin/ps":
                    text = f"{agy} --csrf_token=fixture-csrf\n"
                else:
                    self.assertEqual(argv[2:5], ["-a", "-p", "8"])
                    text = "p8\nn*:3450\nn127.0.0.1:3451\nn192.168.1.2:3452\n"
                return type("Result", (), {"stdout": text})()
            with patch.object(native.subprocess, "run", side_effect=run), patch.object(
                    native, "_agy_local_request", return_value=json.loads(fixture("agy-local-quota.json"))) as rpc:
                snap = native.observe_antigravity({"reuse_running": True, "binary": "/tmp/forbidden"})
            self.assertEqual(calls, ["/bin/ps", "/bin/ps", "/usr/sbin/lsof"])
            self.assertEqual(rpc.call_args.args[:2], (3451, "fixture-csrf"))
            self.assertEqual(snap["source"], "antigravity-local")
            self.assertNotIn("fixture-csrf", json.dumps(snap))

    def test_legacy_widget_flag_never_opens_codexbar(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(native.os, "open", wraps=os.open) as opened:
            report = Path(tmp) / "missing.json"
            snap = native.observe_antigravity({"codexbar_widget": True, "path": str(report)})
        self.assertFalse(snap["available"])
        self.assertEqual([call.args[0] for call in opened.call_args_list], [report])

    def test_display_only_argon_is_not_a_routing_signal_or_a_reset_clock(self):
        payload = {"groups": [{"buckets": [{"id": "gemini-argon-wtu-weekly", "remainingFraction": .999}]}]}
        captured = NOW
        pool = "antigravity-argon"
        config = {"oracle": "antigravity", "windows": ["antigravity-quota-summary-argon-weekly"]}
        roster = {"quota_pools": {pool: {"quota_refresh": config}}, "lanes": []}
        runtime = {"quota_snapshots": {}}
        observed = fleetctl._filtered_observation(native.parse_antigravity_local(payload, captured), config)
        self.assertEqual(fleetctl._commit_gauge(runtime, pool, observed, fleetctl.iso(captured),
                                                recover_circuit=False), "refreshed")
        self.assertEqual(fleetctl.current_pool_state(runtime, pool, captured, roster=roster)[0], "UNKNOWN")
        with tempfile.TemporaryDirectory() as root:
            overview = fleetctl.fleet_overview(roster, runtime, Path(root), captured)
        limits = overview["pools"][0]["limits"]
        self.assertTrue(limits[0]["reset_unknown"])
        self.assertIn("0% (display only)", fleetctl.render_brief(overview))
        old = {"observed_at": "2026-11-19T15:00:00Z", "windows": {
            config["windows"][0]: {"used_percent": 100, "reset_at": fleetctl.iso(captured + dt.timedelta(days=1))}}}
        runtime["quota_snapshots"][pool] = old
        with patch.object(fleetctl, "utc_now", return_value=captured):
            fleetctl._commit_gauge(runtime, pool, observed, fleetctl.iso(captured), recover_circuit=False)
        self.assertEqual(runtime["quota_snapshots"][pool], old)

    def test_codexbar_cannot_be_a_backdoor_to_launch_antigravity(self):
        with patch.object(fleetctl.subprocess, "run") as spawn:
            for provider in ("agy", "all", "antigravity", "claude,antigravity"):
                self.assertFalse(fleetctl.codexbar_observed(provider)["available"])
        spawn.assert_not_called()


class OpenRouterAccountTests(unittest.TestCase):
    def test_existing_key_reads_account_credits_and_key_usage_without_a_quota_window(self):
        calls = []
        def usage(request, timeout):
            calls.append(request.full_url)
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
            if request.full_url.endswith("/credits"):
                return Response('{"data":{"total_credits":30,"total_usage":26.25,"name":"redact"}}')
            return Response('{"data":{"usage_daily":0,"usage_weekly":0.125,"limit_remaining":3,"label":"redact"}}')
        with patch.dict(os.environ, {"OPENROUTER_KEY_FILE": "/existing/key"}), \
                patch.object(native, "_owner_file", return_value=TOKEN.encode()) as read:
            snap = native.observe_openrouter({}, opener=usage)
        read.assert_called_once_with(Path("/existing/key"))
        self.assertEqual(calls, [native.OPENROUTER_BASE + suffix for suffix in ("credits", "key")])
        self.assertEqual(snap["windows"], {})
        self.assertEqual(snap["account_usage"], {"account_credits_usd": 30, "account_usage_usd": 26.25,
                                                 "account_balance_usd": 3.75, "key_usage_daily_usd": 0,
                                                 "key_usage_weekly_usd": .125, "key_limit_remaining_usd": 3})
        self.assertNotIn(TOKEN, json.dumps(snap))
        self.assertNotIn("redact", json.dumps(snap))
        self.assertEqual(fleetctl.current_pool_state({"quota_snapshots": {"openrouter-free": snap}},
                                                    "openrouter-free")[0], "UNKNOWN")

    def test_account_only_oracle_refreshes_without_using_quota_for_routing(self):
        pool = "openrouter-free"
        roster = {"quota_pools": {pool: {"plan": {"limit": "none-known"},
                                         "quota_refresh": {"oracle": "openrouter", "ttl_s": 1800}}}}
        snap = {**native._snapshot(native.OPENROUTER_SOURCE, {}, {}), "account_usage": {"account_balance_usd": 1.5}}
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(native.OBSERVERS, {"openrouter": lambda *_a, **_k: snap}):
            path = Path(temp)
            self.assertEqual(fleetctl.refresh_stale_pools(path, [pool], roster=roster, force=True), {pool: "refreshed"})
            runtime = json.loads((path / "runtime.json").read_text())
            self.assertEqual(runtime["quota_snapshots"][pool]["account_usage"], snap["account_usage"])
            self.assertEqual(fleetctl.current_pool_state(runtime, pool, roster=roster)[0], "UNKNOWN")

    def test_an_env_token_is_never_used_as_a_fallback(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": TOKEN}), \
                patch.object(native, "_owner_file", side_effect=OSError), \
                patch.object(native, "_http_get") as fetch:
            self.assertEqual(native.observe_openrouter({})["kind"], "no-key")
        fetch.assert_not_called()


class FleetWiringTests(unittest.TestCase):
    """The readers plug into the existing refresh: registry, one fetch per provider, honest failure."""

    def test_claude_codexbar_source_is_independent_of_expired_cli_credentials(self):
        # CodexBar's web answer can contain a synthetic five-hour slot, not a measured zero.
        payload = [{"provider": "claude", "source": "web", "usage": {
            "updatedAt": fleetctl.iso(), "accountEmail": "private@example.invalid",
            "loginMethod": "Claude Max 20x",
            "primary": {"usedPercent": 0, "windowMinutes": 300, "isSyntheticPlaceholder": True},
            "secondary": {"usedPercent": 86, "windowMinutes": 10080,
                          "resetsAt": fleetctl.iso(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))},
            "extraRateWindows": [{"id": "synthetic-extra", "title": "Placeholder",
                                  "window": {"usedPercent": 0, "isSyntheticPlaceholder": True}}]}}]
        roster = {"quota_pools": {"claude": {
            "quota_refresh": {"oracle": "codexbar", "provider": "claude", "ttl_s": 300}}}}
        response = type("Result", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""})()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(native, "_claude_credential_records", return_value=[claude_record(-60)]) as credentials, \
                patch.object(fleetctl.subprocess, "run", return_value=response) as run:
            state = Path(folder)
            self.assertEqual(fleetctl.refresh_stale_pools(state, ["claude"], roster=roster, force=True),
                             {"claude": "refreshed"})
            runtime = json.loads((state / "runtime.json").read_text())
            snap = runtime["quota_snapshots"]["claude"]
            self.assertEqual(snap["source"], "codexbar")
            self.assertEqual(snap["windows"]["secondary"]["used_percent"], 86)
            self.assertEqual(snap["plan"], "Claude Max 20x")
            self.assertNotIn("idle_windows", snap)
            self.assertNotIn("private@example.invalid", json.dumps(runtime))
            self.assertNotIn("claude", runtime["quota_refresh_errors"])
            credentials.assert_not_called()
            self.assertEqual(run.call_args.args[0][1:],
                             ["usage", "--provider", "claude", "--format", "json"])

    def setUp(self):
        self.roster = json.loads((ROOT / "examples" / "access-overlay.example.json").read_text())
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self._optout = os.environ.pop("FLEET_NO_AUTO_REFRESH", None)
        self.addCleanup(lambda: self._optout is not None and os.environ.__setitem__("FLEET_NO_AUTO_REFRESH", self._optout))

    def test_registry_names_every_native_reader(self):
        for name in ("codex", "claude", "antigravity", "copilot", "opencode-go"):
            self.assertIn(name, fleetctl.ORACLE_REGISTRY)

    def test_one_antigravity_report_feeds_three_pools_without_codexbar(self):
        # The clock stands at the fixture's capture (29 Oct ~22:00Z, its commit time), before its
        # resets, which on the real clock have all passed.
        captured = dt.datetime(2026, 10, 29, 22, 0, tzinfo=dt.timezone.utc)
        for module, name in ((native, "_now"), (fleetctl, "utc_now")):
            frozen = patch.object(module, name, lambda: captured)
            frozen.start()
            self.addCleanup(frozen.stop)
        calls = []
        snapshot = native.parse_antigravity(fixture("agy-usage.json"))

        def agy(config, timeout=5):
            calls.append(config.get("windows"))
            return snapshot
        pools = ["antigravity-gemini", "antigravity-3p", "antigravity-argon"]
        with patch.dict(fleetctl.ORACLE_REGISTRY, {"antigravity": agy}), \
                patch.object(fleetctl, "CODEXBAR_BIN", str(self.state / "no-codexbar")):
            outcomes = fleetctl.refresh_stale_pools(self.state, pools, roster=self.roster)
        self.assertEqual(len(calls), 1)
        self.assertEqual(outcomes, {pool: "refreshed" for pool in pools})
        runtime = json.loads((self.state / "runtime.json").read_text())
        stored = {pool: sorted(runtime["quota_snapshots"][pool]["windows"]) for pool in pools}
        self.assertEqual(stored["antigravity-argon"], ["antigravity-quota-summary-argon-weekly"])
        self.assertEqual(stored["antigravity-gemini"], ["antigravity-quota-summary-gemini-5h",
                                                        "antigravity-quota-summary-gemini-weekly"])
        now = fleetctl.parse_iso(runtime["quota_snapshots"]["antigravity-3p"]["observed_at"])
        states = {pool: fleetctl.current_pool_state(runtime, pool, now, roster=self.roster)[0] for pool in pools}
        # Argon exhausted is not Antigravity exhausted.
        self.assertEqual(states["antigravity-argon"], "EXHAUSTED")
        self.assertNotEqual(states["antigravity-gemini"], "EXHAUSTED")
        self.assertNotEqual(states["antigravity-3p"], "EXHAUSTED")

    def test_failed_native_reader_is_a_gauge_error_not_zero(self):
        failure = native.unavailable("claude-oauth-usage", "expired", "Claude Code's sign-in has expired")
        with patch.dict(fleetctl.ORACLE_REGISTRY, {"claude": lambda config, timeout=5: failure}):
            outcomes = fleetctl.refresh_stale_pools(self.state, ["claude"], roster=self.roster)
        self.assertTrue(outcomes["claude"].startswith("unavailable"))
        runtime = json.loads((self.state / "runtime.json").read_text())
        self.assertNotIn("claude", runtime.get("quota_snapshots", {}))
        state, evidence = fleetctl.current_pool_state(runtime, "claude", roster=self.roster)
        self.assertEqual(state, "GAUGE_ERROR")
        self.assertIn("expired", evidence["gauge_error"])

    # BR-QUOTA-001
    def test_antigravity_refresh_never_starts_cli_and_keeps_expired_evidence_stale(self):
        report = self.state / "manual-usage.json"
        marker = self.state / "unexpected-launch"
        tool = self.state / "fake-quota-cli"
        tool.write_text(f'#!/bin/sh\ntouch "{marker}"\n[ "$1" = --version ] && echo 1.5.12\nexit 0\n')
        tool.chmod(0o700)
        pool = "antigravity-gemini"
        self.roster["quota_pools"][pool]["quota_refresh"].update(path=str(report), binary=str(tool))
        captured = dt.datetime(2026, 10, 29, 22, 0, tzinfo=dt.timezone.utc)
        now = captured
        with patch.dict(os.environ, {"AGY_BIN": str(tool)}), \
                patch.object(native, "_now", lambda: now), patch.object(fleetctl, "utc_now", lambda: now):
            def refresh(force=False):
                return fleetctl.refresh_stale_pools(self.state, [pool], roster=self.roster, force=force)

            def runtime():
                return json.loads((self.state / "runtime.json").read_text())

            self.assertIn("No Antigravity usage export", refresh()[pool])
            self.assertEqual(fleetctl.current_pool_state(runtime(), pool, now, roster=self.roster)[0], "GAUGE_ERROR")
            report.write_text(fixture("agy-usage.json"))
            os.utime(report, (captured.timestamp(), captured.timestamp()))
            self.assertEqual(refresh(force=True)[pool], "refreshed")  # successful observation control
            stamp = runtime()["quota_snapshots"][pool]["observed_at"]
            self.assertEqual(fleetctl.parse_iso(stamp), captured)
            now += dt.timedelta(minutes=12)
            self.assertEqual(refresh(force=True)[pool], "kept newer stored snapshot")
            now += dt.timedelta(minutes=4)
            self.assertEqual(fleetctl.current_pool_state(runtime(), pool, now, roster=self.roster)[1]["confidence"], "stale")
            self.assertIn("older than 15 minutes", refresh(force=True)[pool])
            _, evidence = fleetctl.current_pool_state(runtime(), pool, now, roster=self.roster)
            self.assertEqual((evidence["confidence"], evidence["observed_at"]), ("stale", stamp))
            self.assertFalse(marker.exists())

    def test_reader_time_limit_is_its_own_not_the_five_second_default(self):
        self.assertEqual(native.reader_timeout("codex", {}, fleetctl.REFRESH_TIMEOUT_S), 20)
        self.assertEqual(native.reader_timeout("codex", {"timeout_s": 7}, 5), 7)


if __name__ == "__main__":
    unittest.main()
