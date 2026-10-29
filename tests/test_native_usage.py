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
        got = native.observe_antigravity({}, run=lambda b, t: ("ok", fixture("agy-not-usage.json")))
        self.assertEqual((got["available"], got["kind"]), (False, "malformed"))

    def test_run_failures_stay_unknown(self):
        for kind in ("no-tool", "unauthorized", "failed"):
            got = native.observe_antigravity({}, run=lambda b, t: (kind, "why"))
            self.assertEqual((got["available"], got["kind"]), (False, kind))

    def test_old_cli_is_refused_before_any_prompt(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            log = Path(folder) / "calls"
            agy = Path(folder) / "agy"
            agy.write_text(f'#!/bin/sh\necho "$*" >> {log}\n[ "$1" = --version ] && echo 1.1.9\nexit 0\n')
            agy.chmod(0o700)
            got = native.observe_antigravity({"binary": str(agy)})
            self.assertEqual(got["kind"], "no-tool")
            self.assertEqual(log.read_text().split("\n")[:-1], ["--version"])

    def test_cli_runs_in_a_private_folder_with_browser_openers_stubbed(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            log = Path(folder) / "env"
            agy = Path(folder) / "agy"
            body = fixture("agy-usage.json").replace("'", "")
            agy.write_text(f"""#!/bin/sh
if [ "$1" = --version ]; then echo 1.4.10; exit 0; fi
{{ echo "args=$*"; echo "cwd=$PWD"; echo "browser=$BROWSER"; echo "update=$AGY_CLI_DISABLE_AUTO_UPDATE"; open https://example.invalid; echo "open=$?"; }} > {log}
cat <<'JSON'
{body}
JSON
""")
            agy.chmod(0o700)
            got = native.observe_antigravity({"binary": str(agy)})
            self.assertTrue(got["available"], got)
            seen = dict(line.split("=", 1) for line in log.read_text().splitlines())
            self.assertEqual(seen["args"].split()[:4], ["-p", "/usage", "--output-format", "json"])
            self.assertIn("crossfeed-agy-usage-", seen["cwd"])
            self.assertTrue(seen["browser"].endswith("/open"))
            self.assertEqual(seen["update"], "true")
            self.assertEqual(seen["open"], "1")          # the stub, not the system browser
            self.assertFalse(Path(seen["cwd"]).exists())  # the private folder is gone


class CopilotTests(unittest.TestCase):
    def test_premium_allowance_with_monthly_reset(self):
        snap = native.parse_copilot(fixture("copilot-user.json"), NOW)
        self.assertEqual(snap["windows"], {"premium_monthly": {
            "used_percent": 1, "window_minutes": 31 * 1440, "label": "Premium", "reset_at": "2026-11-01T00:00:00Z"}})
        self.assertEqual(snap["plan"], "Copilot Individual")

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


class FleetWiringTests(unittest.TestCase):
    """The readers plug into the existing refresh: registry, one fetch per provider, honest failure."""

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

    def test_reader_time_limit_is_its_own_not_the_five_second_default(self):
        self.assertEqual(native.reader_timeout("antigravity", {}, fleetctl.REFRESH_TIMEOUT_S), 45)
        self.assertEqual(native.reader_timeout("codex", {"timeout_s": 7}, 5), 7)


if __name__ == "__main__":
    unittest.main()
