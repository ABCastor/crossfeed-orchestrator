"""The menu-bar glance reads the same overview as the console and brief, never its own numbers."""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_fleetctl import fleetctl

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 29, 21, 0, tzinfo=dt.timezone.utc)


def load_menubar():
    import importlib.util
    spec = importlib.util.spec_from_file_location("menubar_under_test", ROOT / "scripts" / "menubar.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


menubar = load_menubar()


def window(used, reset_hours, minutes, label=None):
    entry = {"used_percent": used, "reset_at": fleetctl.iso(NOW + dt.timedelta(hours=reset_hours)),
             "window_minutes": minutes}
    if label:
        entry["label"] = label
    return entry


def runtime():
    observed = fleetctl.iso(NOW - dt.timedelta(minutes=3))
    return {
        "quota_snapshots": {
            "claude": {"source": "claude-oauth-usage", "observed_at": observed, "windows": {
                "session": window(4, 4, 300, "Session"), "weekly": window(26, 110, 10080, "Weekly"),
                "claude-weekly-scoped-fable": window(0, 110, 10080, "Fable only")}},
            "codex": {"source": "codex-app-server", "observed_at": observed,
                      "windows": {"weekly": window(100, 130, 10080, "Weekly")}},
            "antigravity-gemini": {"source": "antigravity-cli", "observed_at": observed, "windows": {
                "antigravity-quota-summary-gemini-5h": window(0, 5, 300, "Gemini 5-hour"),
                "antigravity-quota-summary-gemini-weekly": window(1, 100, 10080, "Gemini weekly")}},
            "antigravity-3p": {"source": "antigravity-cli", "observed_at": observed, "windows": {
                "antigravity-quota-summary-3p-weekly": window(85, 100, 10080, "Claude/GPT weekly")}},
            "antigravity-argon": {"source": "antigravity-cli", "observed_at": observed, "windows": {
                "antigravity-quota-summary-argon-weekly": window(100, 12, 10080, "Argon weekly")}},
        },
        "quota_refresh_errors": {"github-copilot-student": {
            "attempted_at": observed, "reason": "the GitHub CLI is not signed in (`gh auth login`)"}},
    }


class GlanceTests(unittest.TestCase):
    def setUp(self):
        self.roster = json.loads((ROOT / "examples" / "access-overlay.example.json").read_text())
        self.roster.pop("provider_sources", None)
        self.roster.pop("chatgpt_gateway", None)
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        with patch.object(fleetctl, "utc_now", return_value=NOW):
            self.overview = fleetctl.fleet_overview(self.roster, runtime(), Path(self.tmp.name), NOW)

    def test_title_names_lead_then_hot_then_full(self):
        self.assertEqual(menubar.title(self.overview, "claude"),
                         "Claude 26% · AGY Claude/GPT 85% · full: Argon, Codex")

    def test_title_without_lead_or_pressure(self):
        quiet = {"pools": [p for p in self.overview["pools"] if p["pool"] == "antigravity-gemini"]}
        self.assertEqual(menubar.title(quiet, None), "Limits OK")
        self.assertEqual(menubar.title(quiet, "antigravity-gemini"), "Gemini 1%")

    def test_dropdown_lists_every_limit_with_its_reset(self):
        out = menubar.swiftbar(self.overview, "claude", "/plugins/limits.5m.sh")
        lines = out.splitlines()
        self.assertEqual(lines[0], menubar.title(self.overview, "claude"))
        self.assertEqual(lines[1], "---")
        self.assertTrue(lines[2].startswith("Claude  26% · resets "))   # the lead first
        self.assertIn("--Weekly (Fable only) 0% · resets", out)
        self.assertRegex(out, r"Codex \(ChatGPT\)  full · resets .* \| color=#d70015")
        self.assertRegex(out, r"Antigravity · Argon  full · resets .*color=#d70015")
        self.assertIn("Antigravity · Gemini  1% · resets", out)
        self.assertIn("Read the limits again now | bash=/plugins/limits.5m.sh param1=--force terminal=false refresh=true", out)
        # Every content line keeps its text whole: a '|' in a reason would cut the line.
        self.assertNotIn("|", out.splitlines()[0])

    def test_failed_gauge_is_said_not_hidden(self):
        out = menubar.swiftbar(self.overview, "claude")
        self.assertIn("GitHub Copilot", out)
        self.assertIn("Copilot: not read now (the GitHub CLI is not signed in (`gh auth login`))", out)

    def test_glance_command_reads_state_without_refreshing(self):
        state = Path(self.tmp.name) / "state"
        state.mkdir()
        (state / "runtime.json").write_text(json.dumps(runtime()))
        overlay = Path(self.tmp.name) / "overlay.json"
        overlay.write_text(json.dumps(self.roster))
        env = dict(os.environ, ACCESS_OVERLAY=str(overlay), FLEET_STATE_DIR=str(state), FLEET_NO_AUTO_REFRESH="1")
        for fmt in ("text", "swiftbar", "json"):
            done = subprocess.run([sys.executable, str(ROOT / "scripts" / "fleetctl.py"), "glance",
                                   "--lead", "claude", "--format", fmt],
                                  capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("Claude", done.stdout)
        data = json.loads(done.stdout)
        self.assertIn("Claude", data["title"])
        self.assertEqual(data["pools"][0]["pool"], "claude")


if __name__ == "__main__":
    unittest.main()
