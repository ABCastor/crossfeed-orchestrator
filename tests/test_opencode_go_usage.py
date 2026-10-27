"""OpenCode Go usage source: the endpoint's answers become a quota snapshot or an explicit unknown.

Fixtures in tests/fixtures/opencode-go-usage are endpoint bodies in the documented shape
(OpenCode's GoUsage schema; the Orca client's recorded cases). No real key or account data.
"""
import datetime as dt
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tests.test_fleetctl import fleetctl, roster

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "opencode-go-usage"
NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone.utc)
# A placeholder: a real key never reaches a fixture.
KEY = "placeholder-go-key-0123456789"
go = fleetctl.opencode_go_usage


def body(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class Response:
    def __init__(self, text, content_type="application/json"):
        self._data = text.encode("utf-8")
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self._data if limit < 0 else self._data[:limit]


def answering(fixture=None, status=200, content_type="application/json", raise_exc=None, seen=None):
    """A urlopen stand-in that records the request and answers with a fixture."""
    def opener(request, timeout=5):
        if seen is not None:
            seen.append(request)
        if raise_exc is not None:
            raise raise_exc
        text = body(fixture)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(text.encode()))
        return Response(text, content_type)
    return opener


def observe(opener, config=None):
    with patch.dict(os.environ, {go.KEY_ENV: KEY}):
        return go.observe(config or {"oracle": "opencode-go"}, timeout=5, opener=opener)


class ParseTests(unittest.TestCase):
    def test_ok_answer_becomes_three_windows_with_resets(self):
        seen = []
        snap = observe(answering("ok.json", seen=seen))
        self.assertTrue(snap["available"])
        self.assertEqual(snap["source"], "opencode-go-api")
        self.assertEqual(seen[0].full_url, go.USAGE_URL)
        self.assertEqual(seen[0].get_method(), "GET")
        self.assertEqual(seen[0].get_header("Authorization"), f"Bearer {KEY}")
        w = snap["windows"]
        self.assertEqual(sorted(w), ["monthly", "rolling_5h", "weekly"])
        self.assertEqual(w["rolling_5h"], {"used_percent": 13, "reset_at": "2026-10-02T15:30:00Z",
                                           "window_minutes": 300, "label": "5-hour"})
        self.assertEqual(w["weekly"]["used_percent"], 41)
        self.assertEqual(w["weekly"]["window_minutes"], 10080)
        # rate-limited is exhausted even when the percent rounds below 100
        self.assertEqual(w["monthly"]["used_percent"], 100)
        self.assertEqual(w["monthly"]["reset_at"], "2026-10-20T09:00:00Z")
        # the shared validator accepts it unchanged: same shape as every other source
        self.assertTrue(fleetctl._validate_snapshot(snap, "x")["available"])

    def test_monthly_meter_is_optional(self):
        payload = json.loads(body("ok.json"))
        del payload["usage"]["monthly"]
        snap = go.parse_usage(json.dumps(payload))
        self.assertEqual(sorted(snap["windows"]), ["rolling_5h", "weekly"])

    def test_unauthorized_is_explicit_never_zero(self):
        snap = observe(answering("unauthorized.json", status=401))
        self.assertEqual((snap["available"], snap["kind"]), (False, "unauthorized"))
        self.assertNotIn("windows", snap)

    def test_no_subscription_is_explicit(self):
        snap = observe(answering("no-subscription.json", status=403))
        self.assertEqual((snap["available"], snap["kind"]), (False, "no-subscription"))
        self.assertNotIn("windows", snap)

    def test_error_name_beats_status_code(self):
        snap = observe(answering("no-subscription.json", status=402))
        self.assertEqual(snap["kind"], "no-subscription")
        snap = observe(answering("unauthorized.json", status=403))
        self.assertEqual(snap["kind"], "unauthorized")

    def test_malformed_answer_is_explicit(self):
        snap = observe(answering("malformed.json"))
        self.assertEqual((snap["available"], snap["kind"]), (False, "malformed"))
        self.assertNotIn("windows", snap)

    def test_sign_in_page_is_unauthorized(self):
        snap = observe(answering("signin.html", content_type="text/html; charset=utf-8"))
        self.assertEqual(snap["kind"], "unauthorized")

    def test_server_error_is_failed(self):
        snap = observe(answering("malformed.json", status=500))
        self.assertEqual(snap["kind"], "failed")

    def test_key_never_leaks_into_any_result(self):
        cases = [
            answering("ok.json"), answering("unauthorized.json", status=401),
            answering("no-subscription.json", status=403), answering("malformed.json"),
            answering(raise_exc=urllib.error.URLError(f"refused with Bearer {KEY}")),
            answering(raise_exc=RuntimeError(KEY)),
        ]
        for opener in cases:
            self.assertNotIn(KEY, json.dumps(observe(opener)))

    def test_missing_key_is_explicit_and_sends_nothing(self):
        seen = []
        with tempfile.TemporaryDirectory() as d, \
                patch.dict(os.environ, {"XDG_DATA_HOME": d}, clear=False):
            os.environ.pop(go.KEY_ENV, None)
            snap = go.observe({"oracle": "opencode-go"}, opener=answering("ok.json", seen=seen))
        self.assertEqual(snap["kind"], "no-key")
        self.assertEqual(seen, [])

    def test_declared_reference_is_used_and_does_not_fall_back(self):
        seen = []
        with patch.dict(os.environ, {"GO_TEST_KEY": KEY, go.KEY_ENV: "other-key"}):
            snap = go.observe({"key": "GO_TEST_KEY"}, opener=answering("ok.json", seen=seen))
        self.assertTrue(snap["available"])
        self.assertEqual(seen[0].get_header("Authorization"), f"Bearer {KEY}")
        with patch.dict(os.environ, {go.KEY_ENV: KEY}):
            os.environ.pop("GO_TEST_KEY_MISSING", None)
            snap = go.observe({"key": "GO_TEST_KEY_MISSING"}, opener=answering("ok.json"))
        self.assertEqual(snap["kind"], "no-key")

    def test_opencode_auth_file_supplies_the_key(self):
        seen = []
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"XDG_DATA_HOME": d}):
            os.environ.pop(go.KEY_ENV, None)
            (Path(d) / "opencode").mkdir()
            (Path(d) / "opencode" / "auth.json").write_text(json.dumps(
                {"opencode-go": {"type": "api", "key": KEY}}))
            snap = go.observe({}, opener=answering("ok.json", seen=seen))
        self.assertTrue(snap["available"])
        self.assertEqual(seen[0].get_header("Authorization"), f"Bearer {KEY}")

    def test_plaintext_url_is_refused_before_reading_a_key(self):
        snap = observe(answering("ok.json"), {"url": "http://example.test/usage"})
        self.assertEqual(snap["kind"], "failed")


def go_roster():
    r = roster()
    r["quota_pools"]["opencode-go"]["quota_refresh"] = {"oracle": "opencode-go", "ttl_s": 600}
    return r


class RefreshAndBriefTests(unittest.TestCase):
    def refresh(self, state_dir, opener, r):
        with patch.dict(os.environ, {go.KEY_ENV: KEY, "FLEET_NO_AUTO_REFRESH": ""}), \
                patch("urllib.request.urlopen", side_effect=opener):
            return fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=r, force=True)

    def test_declared_source_is_registered(self):
        self.assertIn("opencode-go", fleetctl.quota_sources(go_roster()))

    def test_refresh_records_snapshot_and_brief_shows_numbers(self):
        r = go_roster()
        # The snapshot is stamped with the real clock; the brief is read at NOW, inside the
        # fixture's windows, so every reset is still ahead.
        with tempfile.TemporaryDirectory() as d, patch.object(fleetctl, "utc_now", return_value=NOW):
            outcome = self.refresh(Path(d), answering("ok.json"), r)
            self.assertEqual(outcome, {"opencode-go": "refreshed"})
            runtime = json.loads((Path(d) / "runtime.json").read_text())
            snap = runtime["quota_snapshots"]["opencode-go"]
            self.assertEqual(snap["source"], "opencode-go-api")
            self.assertEqual(snap["windows"]["weekly"]["used_percent"], 41)
            state, evidence = fleetctl.current_pool_state(runtime, "opencode-go", NOW, roster=r)
            self.assertEqual(state, "EXHAUSTED")  # the rate-limited monthly window binds
            data = fleetctl.fleet_overview(r, runtime, Path(d), NOW)
            text = fleetctl.render_brief(data, roster=r, runtime=runtime)
        row = next(line for line in text.splitlines() if line.startswith("opencode-go |"))
        self.assertNotIn("| - | - |", row)
        self.assertIn("100%", row)

    def test_failed_refresh_keeps_previous_reading_and_writes_no_zero(self):
        r = go_roster()
        for fixture, status, kind in [("unauthorized.json", 401, "unauthorized"),
                                      ("no-subscription.json", 403, "no-subscription"),
                                      ("malformed.json", 200, "malformed")]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                outcome = self.refresh(Path(d), answering(fixture, status=status), r)
                self.assertTrue(outcome["opencode-go"].startswith("unavailable:"), outcome)
                runtime = json.loads((Path(d) / "runtime.json").read_text())
                self.assertNotIn("opencode-go", runtime.get("quota_snapshots", {}))
                state, _ = fleetctl.current_pool_state(runtime, "opencode-go", roster=r)
                self.assertEqual(state, "UNKNOWN")

    def test_console_names_the_source(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "console_go_test", Path(fleetctl.__file__).with_name("console.py"))
        console = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(console)
        self.assertEqual(console.SOURCE_WORDS.get("opencode-go-api"), "OpenCode")


if __name__ == "__main__":
    unittest.main()
