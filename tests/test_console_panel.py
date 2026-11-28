"""The panel page, its document route, its preferences write and the one reset claim.

Same door as the rest of the console: signed in, same origin, form token. The reset route is checked
against a fake claim so the test observes the one attempt and the plain answer, not a network call.
"""
import contextlib
import http.client
import io
import json
import os
import re
import stat
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from tests import test_console  # the console loaded once, as the other console suites do

console = test_console.console
fleetctl = console.fleetctl


class PanelServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["FLEET_NO_AUTO_REFRESH"] = "1"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        cls.overlay = cls.state / "overlay.json"
        cls.overlay.write_text(json.dumps(test_console.base_overlay()), encoding="utf-8")
        cls.home = Path(cls.tmp.name) / "home"
        (cls.home / ".config" / "orchestrator").mkdir(parents=True)
        cls._old_prefs = os.environ.get("FLEET_PANEL_PREFS")
        os.environ["FLEET_PANEL_PREFS"] = str(cls.home / ".config" / "orchestrator" / "panel.json")
        cls.app = console.Console(cls.overlay, cls.state, 0)
        cls.server = console.bind(cls.app, 0)
        cls.port = cls.app.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        if cls._old_prefs is None:
            os.environ.pop("FLEET_PANEL_PREFS", None)
        else:
            os.environ["FLEET_PANEL_PREFS"] = cls._old_prefs
        cls.tmp.cleanup()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        data = body.encode() if isinstance(body, str) else body
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        if data:
            conn.send(data)
        response = conn.getresponse()
        body = response.read().decode()
        conn.close()
        return response.status, body, dict(response.getheaders())

    @property
    def signed(self):
        return {"Cookie": f"console_session={self.app.session}"}

    def origin(self):
        return {"Origin": f"http://127.0.0.1:{self.port}"}

    def post_json(self, path, payload):
        return self.request("POST", path, {**self.signed, **self.origin(), "Content-Type": "application/json"},
                            json.dumps(payload))

    def post_form(self, path, fields):
        return self.request("POST", path, {**self.signed, **self.origin(),
                                           "Content-Type": "application/x-www-form-urlencoded"},
                            urllib.parse.urlencode(fields))

    # ---- the page and its document --------------------------------------------------------------
    def test_the_panel_is_a_locked_page_with_no_placeholders_and_no_external_loads(self):
        status, body, headers = self.request("GET", "/panel")
        self.assertEqual(status, 401, "the page needs the console session")
        status, page, headers = self.request("GET", "/panel", self.signed)
        self.assertEqual(status, 200)
        self.assertIn(self.app.form_token, page)              # the token the page's script posts
        self.assertNotIn("{{", page)                          # the template was filled
        self.assertIn("script-src 'self'", headers.get("Content-Security-Policy", ""))
        self.assertNotIn("https://", page)
        self.assertNotIn("fonts.googleapis", page)
        self.assertNotIn("http://", page)
        # Every script the page loads is a panel asset under the console's own static route.
        scripts = re.findall(r'<script src="([^"]+)"', page)
        self.assertTrue(scripts)
        for src in scripts:
            self.assertTrue(src.startswith("/static/panel/"), src)
            path = src.split("?")[0]
            status, _body, _headers = self.request("GET", path, self.signed)
            self.assertEqual(status, 200, path)

    def test_panel_json_is_the_glance_document_plus_the_preferences(self):
        fleetctl.panel_prefs.save({"show": "used"})   # a known starting point, whatever ran before
        status, body, _headers = self.request("GET", "/panel.json", self.signed)
        self.assertEqual(status, 200)
        document = json.loads(body)
        for key in ("schema", "title", "pools", "subscriptions", "subscription_spend", "plugins", "panel"):
            self.assertIn(key, document)
        self.assertEqual(document["panel"]["show"], "used")
        self.assertEqual(document["panel"]["tiles"]["overview"], ["alert", "providers", "subscriptions", "tokens", "links"])
        self.assertIsInstance(document["plugins"], list)

    def test_the_panel_json_needs_the_session(self):
        self.assertEqual(self.request("GET", "/panel.json")[0], 401)

    def test_the_sign_in_link_may_land_on_the_panel_and_nowhere_else(self):
        status, _body, headers = self.request("GET", f"/login?key={self.app.login_key}&next=/panel")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/panel")
        status, _body, headers = self.request("GET", f"/login?key={self.app.login_key}&next=//evil.example")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/")

    # ---- preferences ----------------------------------------------------------------------------
    def test_saving_preferences_writes_the_same_file_fleetctl_reads(self):
        document = {"show": "left", "tiles": {"claude": ["limits", "plugin:hello-tile/bar", "links"]},
                    "hidden_tiles": {"claude": ["resets"]}, "future": 1}
        status, body, _headers = self.post_json("/panel/prefs", {"t": self.app.form_token, "prefs": document})
        self.assertEqual(status, 200, body)
        written = json.loads(body)["prefs"]
        self.assertEqual(written["show"], "left")
        self.assertEqual(written["tiles"]["claude"], ["limits", "plugin:hello-tile/bar", "status", "links"])
        path = Path(os.environ["FLEET_PANEL_PREFS"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["future"], 1)
        self.assertEqual(fleetctl.panel_prefs.load()["show"], "left")

    def test_a_preferences_post_needs_the_token_the_origin_and_a_document(self):
        good = {"t": self.app.form_token, "prefs": {"show": "left"}}
        self.assertEqual(self.post_json("/panel/prefs", {"t": "wrong", "prefs": {"show": "left"}})[0], 400)
        self.assertEqual(self.post_json("/panel/prefs", {"prefs": {"show": "left"}})[0], 400)
        status, _body, _headers = self.request("POST", "/panel/prefs",
                                              {**self.signed, "Content-Type": "application/json"}, json.dumps(good))
        self.assertEqual(status, 403, "a change with no origin evidence is refused")
        self.assertEqual(self.post_json("/panel/prefs", {"t": self.app.form_token, "prefs": []})[0], 400)
        self.assertEqual(self.post_json("/panel/prefs", good)[0], 200)
        big = {"t": self.app.form_token, "prefs": {"note": "x" * 100000}}
        self.assertEqual(self.post_json("/panel/prefs", big)[0], 413)

    def test_one_change_is_applied_to_the_file_as_it_is_now_and_validated(self):
        fleetctl.panel_prefs.save({"show": "left", "budgets": {"openrouter-free": 20}})
        status, body, _headers = self.post_json("/panel/prefs", {"t": self.app.form_token, "change": {"last_tab": "codex"}})
        self.assertEqual(status, 200, body)
        saved = fleetctl.panel_prefs.load()
        self.assertEqual((saved["last_tab"], saved["show"], saved["budgets"]), ("codex", "left", {"openrouter-free": 20}))
        status, body, _headers = self.post_json("/panel/prefs", {"t": self.app.form_token, "change": {"appearance": "sepia", "last_tab": "ghost"}})
        self.assertEqual(status, 200)
        written = json.loads(body)["prefs"]
        self.assertEqual((written["appearance"], written["last_tab"]), ("system", "overview"), "a value outside its choices is the default")
        self.assertEqual(self.post_json("/panel/prefs", {"t": "wrong", "change": {"last_tab": "codex"}})[0], 400)
        fleetctl.panel_prefs.save({"show": "used"})

    def test_the_new_keys_round_trip_through_the_post(self):
        document = {"appearance": "dark", "refresh": {"on_open": False, "every_minutes": 15}, "open_on": "codex",
                    "budgets": {"gemini-metered": 12.5}, "tile_options": {"claude/tokens": {"metric": "cost", "days": 30}},
                    "menubar": {"icon": "none", "text": "number", "show": "claude"}}
        status, body, _headers = self.post_json("/panel/prefs", {"t": self.app.form_token, "prefs": document})
        self.assertEqual(status, 200, body)
        written = json.loads(body)["prefs"]
        for key in ("appearance", "refresh", "open_on", "budgets", "tile_options"):
            self.assertEqual(written[key], document[key], key)
        self.assertEqual(written["menubar"]["icon"], "none")
        self.assertEqual(fleetctl.panel_prefs.load()["menubar"]["show"], "claude")
        fleetctl.panel_prefs.save({"show": "used"})

    # ---- reading the sources again --------------------------------------------------------------------
    def test_the_refresh_button_reads_every_source_through_the_forced_path_and_the_document_says_when(self):
        called = []
        original = self.app.refresh_now
        self.app.refresh_now = lambda: called.append("forced") or {}
        try:
            self.assertEqual(self.post_form("/panel/refresh", {"t": "wrong"})[0], 403)
            self.assertEqual(called, [])
            status, body, _headers = self.post_form("/panel/refresh", {"t": self.app.form_token})
            self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
            self.assertEqual(called, ["forced"])
            self.assertEqual(self.request("POST", "/panel/refresh", self.origin(), "t=x")[0], 401, "signed in only")
        finally:
            self.app.refresh_now = original
        self.app._refreshed_at = 1_800_000_000
        document = json.loads(self.request("GET", "/panel.json", self.signed)[1])
        self.assertEqual(document["refreshed_at"], "2027-01-15T08:00:00Z")
        self.app._refreshed_at = None
        self.assertIsNone(json.loads(self.request("GET", "/panel.json", self.signed)[1])["refreshed_at"])

    # ---- API pools ------------------------------------------------------------------------------------
    def test_panel_json_carries_an_api_block_from_the_cache_with_the_operators_budget_and_reads_no_source(self):
        fleetctl.panel_prefs.save({"budgets": {"gemini-metered": 10}})
        pool = next(p for p in self.app.overview()["pools"] if p["pool"] == "gemini-metered") if any(
            p["pool"] == "gemini-metered" for p in self.app.overview()["pools"]) else None
        if pool is None:
            self.skipTest("the synthetic fleet has no gemini-metered pool")
        fleetctl.api_usage._write_cache(self.state, {"schema": 1, "generated_at": "2026-11-28T10:00:00Z", "admin": {},
                                                     "pools": {"gemini-metered": {"spend": {"month_usd": 3.35}, "source": "local-meter",
                                                                                  "source_label": "Measured by Crossfeed"}}})
        with patch.object(fleetctl.api_usage, "local_meter", side_effect=AssertionError("a render read a source")):
            document = json.loads(self.request("GET", "/panel.json", self.signed)[1])
        row = next(p for p in document["pools"] if p["pool"] == "gemini-metered")
        self.assertEqual(row["api"]["spend"]["month_usd"], 3.35)
        self.assertEqual(row["api"]["budget"], {"monthly_usd": 10, "source": "operator"})
        self.assertEqual(row["api"]["source_label"], "Measured by Crossfeed")
        fleetctl.panel_prefs.save({"show": "used"})

    # ---- the one reset claim ---------------------------------------------------------------------
    def test_use_reset_calls_the_same_function_as_fleetctl_and_answers_plainly(self):
        calls = []

        def fake(argv, state_dir):
            calls.append((argv.pool, argv.key, argv.yes, argv.json, Path(state_dir)))
            print(json.dumps({"ok": True, "pool": "claude", "key": argv.key, "label": "Weekly", "left": 0,
                              "total": 2, "sent": True, "error": None}))

        with patch.object(fleetctl, "use_reset_command", fake):
            status, body, headers = self.post_form("/use-reset", {"t": self.app.form_token, "pool": "claude", "key": "weekly"})
        self.assertEqual(status, 200)
        self.assertEqual(body, "Used: Weekly. Left: 0")
        self.assertIn("text/plain", headers.get("Content-Type", ""))
        self.assertEqual(calls, [("claude", "weekly", True, True, self.state)])   # one attempt, --yes, this state dir

    def test_a_refused_reset_reads_as_an_outcome_not_a_crash(self):
        def fake(argv, state_dir):
            print(json.dumps({"ok": False, "pool": "claude", "key": argv.key, "label": None, "left": None,
                              "sent": False, "error": "no limit reset with the key weekly in the current reading"}))

        with patch.object(fleetctl, "use_reset_command", fake):
            status, body, _headers = self.post_form("/use-reset", {"t": self.app.form_token, "pool": "claude", "key": "weekly"})
        self.assertEqual(status, 409)
        self.assertIn("no limit reset", body)

    def test_only_claude_has_a_reset_and_only_a_key_the_glance_shows(self):
        called = []
        with patch.object(fleetctl, "use_reset_command", lambda argv, state_dir: called.append(argv)):
            self.assertEqual(self.post_form("/use-reset", {"t": self.app.form_token, "pool": "codex", "key": "weekly"})[0], 400)
            self.assertEqual(self.post_form("/use-reset", {"t": self.app.form_token, "pool": "claude",
                                                           "key": "../../etc/passwd"})[0], 409)
        self.assertEqual(called, [])
        self.assertEqual(self.post_form("/use-reset", {"t": "wrong", "pool": "claude", "key": "weekly"})[0], 403)


if __name__ == "__main__":
    unittest.main()
