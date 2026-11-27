"""One machine reads every sign-in; another adopts its readings, with age, and falls back to its own readers."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_fleetctl import fleetctl

mac = fleetctl.mac_readings
native = fleetctl.native_usage
NOW = dt.datetime(2026, 11, 27, 9, 0, tzinfo=dt.timezone.utc)
URL = "https://mac.example.ts.net:8444/crossfeed/readings.json"


def snap(minutes_ago, used=40, **extra):
    stamp = (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"available": True, "observed_at": stamp, "source": "claude-oauth-usage",
            "windows": {"weekly": {"used_percent": used, "reset_at": "2026-11-30T00:00:00Z"}}, **extra}


def payload(**pools):
    return json.dumps({"schema": 1, "generated_at": "x", "snapshots": pools}).encode()


class ExportTests(unittest.TestCase):
    def test_export_keeps_usable_numbers_and_drops_everything_else(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            runtime = {"quota_snapshots": {
                "claude": snap(5, secret_field="x", plan="Max"),
                "down": {"available": False, "observed_at": "2026-11-27T08:00:00Z"},
                "leaky": snap(5, plan="owner@example.com"),
                "keyed": snap(5, per_model_error="Bearer abcdefghijklmnop")},
                "leases": [{"pool": "claude"}], "pins": 1}
            (state / "runtime.json").write_text(json.dumps(runtime))
            out = mac.export(state, NOW)
            self.assertEqual(list(out["snapshots"]), ["claude"])
            self.assertNotIn("secret_field", out["snapshots"]["claude"])
            self.assertEqual(out["snapshots"]["claude"]["plan"], "Max")
            self.assertEqual(set(out), {"schema", "generated_at", "snapshots"})

    def test_export_without_runtime_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mac.export(Path(d), NOW)["snapshots"], {})


class PullTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name)
        self.addCleanup(self.dir.cleanup)

    def configure(self):
        (self.state / mac.CONFIG).write_text(json.dumps({"url": URL}))

    def test_nothing_configured_means_nothing_happens(self):
        fetch = mock.Mock()
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("FLEET_READINGS_URL", None)
            self.assertIsNone(mac.pull(self.state, min_interval_s=0, fetch=fetch))
        fetch.assert_not_called()

    def test_plain_http_is_refused(self):
        (self.state / mac.CONFIG).write_text(json.dumps({"url": "http://mac.example/readings.json"}))
        self.assertIsNone(mac.source_url(self.state))
        (self.state / mac.CONFIG).write_text(json.dumps({"url": "http://127.0.0.1:9/readings.json"}))
        self.assertIsNotNone(mac.source_url(self.state))

    def test_pull_keeps_a_copy_with_its_fetch_time_and_serves_it_when_the_mac_sleeps(self):
        self.configure()
        got = mac.pull(self.state, min_interval_s=0, fetch=lambda url, t: payload(claude=snap(2)), now=NOW)
        self.assertEqual(got["fetched_at"], "2026-11-27T09:00:00Z")
        self.assertEqual(list(got["snapshots"]), ["claude"])
        asleep = mock.Mock(side_effect=OSError("timed out"))
        again = mac.pull(self.state, min_interval_s=0, fetch=asleep, now=NOW + dt.timedelta(hours=3))
        self.assertEqual(again["fetched_at"], "2026-11-27T09:00:00Z")  # the old fetch time, honestly
        self.assertEqual(again["snapshots"], got["snapshots"])

    def test_odd_answers_keep_the_last_copy_and_asleep_with_no_copy_is_none(self):
        self.configure()
        self.assertIsNone(mac.pull(self.state, min_interval_s=0, fetch=mock.Mock(side_effect=OSError("down"))))
        for body in (b"not json", b'{"schema":2,"snapshots":{}}', b'[]', b"x" * (mac.MAX_BYTES + 1)):
            self.assertIsNone(mac.pull(self.state, min_interval_s=0, fetch=lambda u, t, b=body: b))
        self.assertFalse((self.state / mac.CACHE).exists())

    def test_a_minute_passes_between_attempts_so_a_sleeping_mac_costs_one_timeout(self):
        self.configure()
        mac.pull(self.state, fetch=lambda u, t: payload(claude=snap(2)), now=NOW)
        fetch = mock.Mock(side_effect=OSError("asleep"))
        got = mac.pull(self.state, fetch=fetch)
        fetch.assert_not_called()
        self.assertEqual(list(got["snapshots"]), ["claude"])

    def test_a_secret_shaped_snapshot_is_dropped_on_arrival(self):
        self.configure()
        got = mac.pull(self.state, min_interval_s=0, fetch=lambda u, t: payload(
            ok=snap(1), bad=snap(1, plan="someone@example.com")), now=NOW)
        self.assertEqual(list(got["snapshots"]), ["ok"])


class AdoptTests(unittest.TestCase):
    kept = {"fetched_at": "2026-11-27T09:00:00Z", "snapshots": {"claude": snap(2, used=44)}}

    def test_newer_remote_wins_and_clears_our_failure(self):
        runtime = {"quota_snapshots": {"claude": snap(90, used=10)},
                   "quota_refresh_errors": {"claude": {"reason": "No usable Claude Code subscription sign-in"}}}
        self.assertEqual(mac.adopt(runtime, self.kept), ["claude"])
        row = runtime["quota_snapshots"]["claude"]
        self.assertEqual((row["windows"]["weekly"]["used_percent"], row["origin"], row["origin_fetched_at"]),
                         (44, "mac", "2026-11-27T09:00:00Z"))
        self.assertNotIn("claude", runtime["quota_refresh_errors"])

    def test_our_own_newer_reading_is_kept(self):
        runtime = {"quota_snapshots": {"claude": snap(1, used=10)}}
        self.assertEqual(mac.adopt(runtime, self.kept), [])
        self.assertNotIn("origin", runtime["quota_snapshots"]["claude"])

    def test_only_pools_this_machine_declares(self):
        runtime = {}
        self.assertEqual(mac.adopt(runtime, self.kept, {"codex"}), [])
        self.assertEqual(mac.adopt(runtime, self.kept, {"claude"}), ["claude"])


class RefreshTests(unittest.TestCase):
    """The wiring through refresh_stale_pools: Mac first, own reader only where the Mac's reading has aged out."""
    roster = {"quota_pools": {"claude": {"quota_refresh": {"oracle": "codexbar", "provider": "claude", "ttl_s": 900}}}}

    def run_refresh(self, local, remote, fetch_ok=True):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / mac.CONFIG).write_text(json.dumps({"url": URL}))
            fleetctl.atomic_json(state / "runtime.json", {"quota_snapshots": {}})
            fetch = (lambda u, t: payload(claude=remote)) if fetch_ok else mock.Mock(side_effect=OSError("asleep"))
            reader = mock.Mock(return_value=local)
            real_now = fleetctl.utc_now
            with mock.patch.object(mac, "_http_get", fetch), \
                 mock.patch.object(fleetctl, "utc_now", return_value=NOW), \
                 mock.patch.dict(fleetctl.ORACLE_REGISTRY, {"codexbar": reader}):
                fleetctl.refresh_stale_pools(state, ["claude"], roster=self.roster)
            runtime = fleetctl.load_json(state / "runtime.json")
            return runtime, reader

    def test_fresh_mac_reading_is_adopted_and_our_reader_stays_idle(self):
        runtime, reader = self.run_refresh({"available": False, "reason": "no sign-in"}, snap(3, used=44))
        reader.assert_not_called()
        self.assertEqual(runtime["quota_snapshots"]["claude"]["origin"], "mac")
        self.assertNotIn("claude", runtime.get("quota_refresh_errors", {}))

    def test_aged_mac_reading_falls_back_to_our_reader_and_stays_marked_stale(self):
        runtime, reader = self.run_refresh({"available": False, "reason": "no sign-in"}, snap(600, used=44))
        reader.assert_called_once()
        row = runtime["quota_snapshots"]["claude"]
        self.assertEqual(row["origin"], "mac")  # last good reading kept, not blanked
        self.assertIn("claude", runtime["quota_refresh_errors"])
        shown = native.display_observation(row, runtime["quota_refresh_errors"]["claude"], NOW, 900)
        self.assertTrue(shown["reading_stale"])
        self.assertEqual((shown["reading_origin"], shown["reading_age_s"]), ("mac", 600 * 60))

    def test_asleep_mac_leaves_our_reader_in_charge(self):
        local = {"available": True, "observed_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "claude-oauth-usage",
                 "windows": {"weekly": {"used_percent": 7, "reset_at": "2026-11-30T00:00:00Z"}}}
        runtime, reader = self.run_refresh(local, None, fetch_ok=False)
        reader.assert_called_once()
        self.assertNotIn("origin", runtime["quota_snapshots"]["claude"])

    def test_display_marks_origin(self):
        row = snap(3, origin="mac", origin_fetched_at="2026-11-27T09:00:00Z")
        shown = native.display_observation(row, None, NOW, 900)
        self.assertEqual((shown["reading_origin"], shown["reading_origin_fetched_at"], shown["reading_stale"]),
                         ("mac", "2026-11-27T09:00:00Z", False))
        self.assertEqual(native.display_observation(snap(3), None, NOW, 900)["reading_origin"], "local")


class LinuxFallbackTests(unittest.TestCase):
    def test_agy_session_comes_from_the_cli_file_when_there_is_no_macos_keychain(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "antigravity-cli"
            folder.mkdir()
            doc = {"token": {"access_token": "placeholder-token", "expiry": "2026-11-27T10:00:00Z"}}
            target = folder / "antigravity-oauth-token"
            target.write_text(json.dumps(doc))
            target.chmod(0o600)
            with mock.patch.dict("os.environ", {"GEMINI_CLI_HOME": d}), \
                 mock.patch.object(native.Path, "is_file", return_value=False):
                self.assertEqual(native._agy_cli_keychain(3), doc)
                target.chmod(0o644)  # not owner-only: refused, not read
                self.assertEqual(native._agy_cli_keychain(3), {})
                target.unlink()
                self.assertEqual(native._agy_cli_keychain(3), {})

    def test_go_key_file_stands_in_only_for_an_op_reference_on_a_machine_without_op(self):
        go = fleetctl.opencode_go_usage
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "opencode"
            key.write_text("placeholder-go-key\n")
            key.chmod(0o600)
            env = {"PI_KEYS_DIR": d}
            with mock.patch.dict("os.environ", env), mock.patch.object(go.shutil, "which", return_value=None):
                self.assertEqual(go.resolve_key({"key": "op://v/i/f"}), ("placeholder-go-key", None))
                key.chmod(0o640)
                self.assertEqual(go.resolve_key({"key": "op://v/i/f"})[0], None)
                key.chmod(0o600)
            with mock.patch.dict("os.environ", env), mock.patch.object(go.shutil, "which", return_value="/usr/bin/op"), \
                 mock.patch.object(go, "_providers_module", side_effect=RuntimeError("op failed")):
                self.assertIn("could not be read", go.resolve_key({"key": "op://v/i/f"})[1]["reason"])


class ServedTests(unittest.TestCase):
    def test_readings_route_is_login_gated_and_secret_free(self):
        from tests.test_phone_view import UnixConnection, request, serving, phone
        login = "operator@example.invalid"
        with tempfile.TemporaryDirectory(prefix="cf-") as root:
            state = Path(root) / "state"
            state.mkdir()
            (state / "runtime.json").write_text(json.dumps({"quota_snapshots": {"claude": snap(1, plan="Max")}}))
            path = Path(root) / "phone.sock"
            path.parent.chmod(0o700)
            with serving(phone.open_server(path, login, mock.Mock(), None, lambda: mac.export(state, NOW))):
                for route in ("/crossfeed/readings.json", "/readings.json"):
                    self.assertEqual(request(UnixConnection(path), "GET", route, [])[0], 403)
                    status, headers, body = request(UnixConnection(path), "GET", route,
                                                    [("Tailscale-User-Login", login)])
                    self.assertEqual((status, headers["Content-Type"]), (200, "application/json"))
                    self.assertEqual(json.loads(body)["snapshots"]["claude"]["plan"], "Max")
                self.assertEqual(request(UnixConnection(path), "POST", "/crossfeed/readings.json",
                                         [("Tailscale-User-Login", login)])[0], 405)


if __name__ == "__main__":
    unittest.main()
