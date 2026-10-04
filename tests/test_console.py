"""The console page: what it shows, and the locks on the door.

Each test talks to a real server on 127.0.0.1 through http.client, so the Host,
Origin and cookie checks are exercised exactly as a browser would hit them.
"""

import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("orchestrator_console", ROOT / "scripts" / "console.py")
assert SPEC and SPEC.loader
console = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(console)
fleetctl = console.fleetctl

# The console marks every page for translators as it makes it (console-assets/keep.py). A test of a page's wording or
# markup should not move when a name gains a mark, so the console the tests share reads pages as their words stand, the
# marks taken off. tests/test_console_translate.py loads a console of its own and reads the real thing.
_real_keep_html = console.keep_html


def unmark(page):
    return re.sub(r' translate="no"', "", re.sub(r'<span translate="no">(.*?)</span>', r"\1", page, flags=re.S))


console.keep_html = lambda page: unmark(_real_keep_html(page))


def base_overlay():
    """The frozen synthetic regression fleet, plus one plan.

    The page is tested against real overlay shapes, never a hand-drawn one, and the plan
    is added here so the estimate marking is exercised whichever base is present.
    """
    for candidate in (ROOT / "tests" / "fixtures" / "access-overlay.test.json",
                      ROOT / "examples" / "access-overlay.example.json"):
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            break
    else:
        raise unittest.SkipTest("no overlay to render")
    data["quota_pools"].setdefault("claude", {})["plan"] = {
        "name": "Claude Max 20x", "price": 200, "currency": "USD", "billing": "subscription",
        "allowance": "an unpublished weekly allowance",
    }
    return data


class ConsoleServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["FLEET_NO_AUTO_REFRESH"] = "1"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        cls.overlay = cls.state / "overlay.json"
        cls.overlay.write_text(json.dumps(base_overlay()), encoding="utf-8")
        cls.app = console.Console(cls.overlay, cls.state, 0)
        cls.server = console.bind(cls.app, 0)
        cls.port = cls.app.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    # ---- helpers ------------------------------------------------------------------------
    def request(self, method, path, headers=None, body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        # skip_host lets a test send a hostile Host header, which is the whole point of some tests
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        data = body.encode() if isinstance(body, str) else body
        if data is not None:
            conn.putheader("Content-Type", "application/x-www-form-urlencoded")
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response, payload

    def cookie(self):
        response, _ = self.request("GET", f"/login?key={self.app.login_key}")
        return response.getheader("Set-Cookie").split(";", 1)[0]

    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def post_level(self, pool, level, *, headers=None, token=None, host=None):
        form = urllib.parse.urlencode({"pool": pool, "level": level, "t": token or self.app.form_token})
        return self.request("POST", "/level", headers=headers, body=form, host=host)

    def level_of(self, pool):
        return fleetctl.pool_level(fleetctl.load_json(self.state / "runtime.json", {}) or {}, pool)

    # ---- the page -----------------------------------------------------------------------
    def test_binds_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_signed_in_page_shows_every_pool_with_its_slider(self):
        response, body = self.request("GET", "/", headers={"Cookie": self.cookie()})
        self.assertEqual(response.status, 200)
        page = body.decode()
        self.assertIn(console.PRODUCT_NAME, page)
        pools = fleetctl.fleet_overview(fleetctl.read_overlay(self.overlay), {}, self.state)["pools"]
        self.assertGreaterEqual(len(pools), 5)
        for pool in pools:
            self.assertIn(f'id="pool-{pool["pool"]}"', page)
            self.assertIn(console.html.escape(pool["label"]), page)
        self.assertEqual(page.count('class="lv at-'), len(pools))
        for word in ("Off", "Low", "Normal", "High", "Forced"):
            self.assertIn(f"<span>{word}</span>", page)
        self.assertIn("What agents read", page)
        self.assertIn('<p class="allow">About ', page)   # an estimate, said once in words
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_static_assets_are_served_and_nothing_else(self):
        response, _ = self.request("GET", "/static/console.css")
        self.assertEqual((response.status, response.getheader("Content-Type")), (200, "text/css; charset=utf-8"))
        response, _ = self.request("GET", "/static/fonts/literata.woff2")
        self.assertEqual(response.status, 200)
        for path in ("/static/../fleetctl.py", "/static/chip.svg", "/static/%2e%2e/console.py"):
            response, _ = self.request("GET", path)
            self.assertEqual(response.status, 404, path)

    # ---- sign-in ------------------------------------------------------------------------
    def test_signed_out_page_explains_and_shows_nothing(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 401)
        self.assertNotIn("What agents read", body.decode())

    def test_login_sets_a_strict_http_only_cookie_and_drops_the_key(self):
        response, _ = self.request("GET", f"/login?key={self.app.login_key}")
        self.assertEqual((response.status, response.getheader("Location")), (303, "/"))
        cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn(self.app.login_key, cookie)

    def test_a_wrong_key_signs_nobody_in(self):
        response, _ = self.request("GET", "/login?key=guess")
        self.assertEqual(response.status, 403)
        self.assertIsNone(response.getheader("Set-Cookie"))

    def test_a_forged_cookie_is_refused(self):
        response, _ = self.request("GET", "/", headers={"Cookie": f"{console.COOKIE}=forged"})
        self.assertEqual(response.status, 401)

    def test_the_sign_in_key_never_reaches_a_log(self):
        """BaseHTTPRequestHandler logs every request line, query string included, to stderr."""
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            self.request("GET", f"/login?key={self.app.login_key}")
            self.request("GET", f"/login?key={self.app.login_key}x")
            self.request("GET", f"/nowhere?key={self.app.login_key}")
            self.request("GET", "/", host="evil.example")
        self.assertNotIn(self.app.login_key, captured.getvalue())
        self.assertEqual(captured.getvalue(), "")

    # ---- DNS rebinding ------------------------------------------------------------------
    def test_a_foreign_host_header_is_refused_before_anything_else(self):
        cookie = self.cookie()
        for host in ("evil.example", f"evil.example:{self.port}", f"127.0.0.1:{self.port + 1}", ""):
            response, body = self.request("GET", "/", headers={"Cookie": cookie}, host=host or " ")
            self.assertEqual(response.status, 421, host)
            self.assertNotIn(b"What agents read", body)
        response, _ = self.request("GET", f"/login?key={self.app.login_key}", host="evil.example")
        self.assertEqual(response.status, 421)
        self.assertIsNone(response.getheader("Set-Cookie"))

    def test_localhost_by_name_is_allowed(self):
        response, _ = self.request("GET", "/static/console.css", host=f"localhost:{self.port}")
        self.assertEqual(response.status, 200)

    # ---- cross-site changes -------------------------------------------------------------
    def test_a_change_needs_this_origin(self):
        cookie = self.cookie()
        cases = {
            "no origin at all": {"Cookie": cookie},
            "foreign origin": {"Cookie": cookie, "Origin": "http://evil.example"},
            "foreign referer": {"Cookie": cookie, "Referer": "http://evil.example/page"},
            "cross-site fetch": {"Cookie": cookie, "Origin": self.origin(), "Sec-Fetch-Site": "cross-site"},
            "same-site fetch": {"Cookie": cookie, "Origin": self.origin(), "Sec-Fetch-Site": "same-site"},
            "null origin": {"Cookie": cookie, "Origin": "null"},
        }
        for name, headers in cases.items():
            response, _ = self.post_level("claude", "low", headers=headers)
            self.assertEqual(response.status, 403, name)
        self.assertEqual(self.level_of("claude"), "normal")

    def test_a_change_needs_the_form_token_and_the_cookie(self):
        response, _ = self.post_level("claude", "low", headers={"Cookie": self.cookie(), "Origin": self.origin()},
                                      token="stale")
        self.assertEqual(response.status, 403)
        response, _ = self.post_level("claude", "low", headers={"Origin": self.origin()})
        self.assertEqual(response.status, 401)
        response, _ = self.post_level("claude", "low", headers={"Cookie": self.cookie(), "Origin": self.origin()},
                                      host="evil.example")
        self.assertEqual(response.status, 421)
        self.assertEqual(self.level_of("claude"), "normal")

    def test_the_page_changes_a_level_and_agents_see_it(self):
        headers = {"Cookie": self.cookie(), "Origin": self.origin(), "Sec-Fetch-Site": "same-origin"}
        response, _ = self.post_level("codex", "high", headers=headers)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/#pool-codex"))
        self.assertEqual(self.level_of("codex"), "high")
        brief = fleetctl.render_brief(
            fleetctl.fleet_overview(fleetctl.read_overlay(self.overlay),
                                    fleetctl.load_json(self.state / "runtime.json", {}), self.state))
        self.assertIn("codex | high |", brief)
        # A referer from this page is accepted when a browser sends no Origin.
        response, _ = self.post_level("codex", "normal",
                                      headers={"Cookie": self.cookie(), "Referer": self.origin() + "/"})
        self.assertEqual(response.status, 303)
        self.assertEqual(self.level_of("codex"), "normal")

    def test_unknown_pool_or_level_changes_nothing(self):
        headers = {"Cookie": self.cookie(), "Origin": self.origin()}
        for pool, level in (("nope", "low"), ("claude", "turbo"), ("", "")):
            response, _ = self.post_level(pool, level, headers=headers)
            self.assertEqual(response.status, 400, (pool, level))
        self.assertEqual(self.level_of("claude"), "normal")


class ConsoleRenderTests(unittest.TestCase):
    def test_forced_pool_still_shows_its_measured_quota(self):
        roster = base_overlay()
        now = fleetctl.utc_now()
        runtime = {
            "switches": {"claude": "on"},
            "quota_snapshots": {"claude": {
                "source": "codexbar", "observed_at": fleetctl.iso(now), "plan": "Claude Max 20x",
                "windows": {"secondary": {"used_percent": 41, "window_minutes": 10080,
                                          "reset_at": fleetctl.iso(now + fleetctl.dt.timedelta(days=3))}},
            }},
        }
        with tempfile.TemporaryDirectory() as d:
            page = console.render_page(fleetctl.fleet_overview(roster, runtime, Path(d)), "tok")
        self.assertIn("41%", page)
        self.assertIn("weekly", page)
        self.assertIn('class="lv at-4 is-forced"', page)

    def test_every_dynamic_string_is_escaped(self):
        roster = base_overlay()
        roster["quota_pools"]["claude"]["label"] = "<script>alert(1)</script>"
        roster["quota_pools"]["claude"]["plan"]["allowance"] = '"><img src=x onerror=alert(1)>'
        with tempfile.TemporaryDirectory() as d:
            page = console.render_page(fleetctl.fleet_overview(roster, {}, Path(d)), "tok")
        self.assertNotIn("<script>alert(1)", page)
        self.assertNotIn("<img src=x", page)


if __name__ == "__main__":
    unittest.main()
