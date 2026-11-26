"""Round 7 console: incident badge, billing, model quota and the Add-key door, hidden when absent."""
import io
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import fleetctl
from tests.test_console import console, base_overlay
from tests.test_secret_store import FakeSystem

KEY = "sk-or-v1-consolekeyplaceholder0123"
ORIGIN = {"Origin": "http://127.0.0.1:8768"}


class PlanExtras(unittest.TestCase):
    def render(self, pool, **kwargs):
        return console._plan_extras({"pool": "claude", **pool}, **kwargs)

    def test_nothing_new_is_drawn_when_the_fields_are_absent_or_clear(self):
        plain = self.render({}, empty=False)
        clear = self.render({"incident": {"level": "none", "text": None}}, empty=False)
        self.assertEqual(plain, clear)
        for word in ("incident", "Degraded", "renews", "Funded credits", "management key"):
            self.assertNotIn(word, plain)

    def test_incident_badge_names_the_level_text_and_links_the_status_page(self):
        html = self.render({"incident": {"level": "minor", "text": "Elevated errors <b>x</b>", "affected": ["claude.ai"],
                                         "since": "2026-11-24T00:10:40Z"}, "status_url": "https://status.claude.com/"})
        self.assertIn('class="incident lvl-minor"', html)
        self.assertIn("<b>Degraded</b> · Elevated errors", html)
        self.assertIn("Elevated errors &lt;b&gt;x&lt;/b&gt;", html)
        self.assertEqual(html.count("Status page"), 1)
        # the badge belongs to the provider's card, not to each of its allowance rows
        self.assertNotIn("incident", self.render({"incident": {"level": "major", "text": "x"}}, show_status=False))

    def test_billing_shows_renewal_grants_and_credit_use(self):
        history = [{"date": f"2026-11-{day:02d}", "credits": float(day)} for day in range(1, 27)]
        html = self.render({"billing": {
            "plan": "pro", "period": "monthly", "active_until": "2026-12-04T10:58:38Z", "renews_at": "2026-12-04T10:58:38Z",
            "will_renew": True, "grants": [{"credits": 12048.5, "granted": 62500.0, "expires_at": "2027-01-01T00:00:00Z"}],
            "credits_today": 26.0, "credits_7d": 161.0, "credits_30d": 351.0, "credit_history": history}}, empty=False)
        self.assertIn("Pro · Monthly: renews <b>", html)
        self.assertIn("Funded credits <b>12,048 of 62,500</b> left", html)
        self.assertIn("Credits used:", html)
        self.assertIn("Credit use by day · last 14 days", html)
        self.assertEqual(html.count("<li>"), 14)

    def test_a_subscription_that_ends_says_so(self):
        html = self.render({"billing": {"plan": "plus", "active_until": "2026-12-04T10:58:38Z", "will_renew": False}}, empty=False)
        self.assertIn("ends <b>", html)
        self.assertIn("not renewing", html)

    def test_model_quota_row_names_the_pool_it_shares(self):
        html = self.render({"model_quota": [{"label": "Argon models", "used_percent": 27.6, "reset_at": "2026-11-26T14:13:49Z",
                                              "shared_with": "Gemini 5-hour"}]}, empty=False)
        self.assertIn("Argon models <b>28% used</b>", html)
        self.assertIn("shares Gemini 5-hour", html)

    def test_add_key_form_appears_only_for_the_management_key_gap_and_holds_no_value(self):
        gap = self.render({"per_model_error": "OpenRouter activity requires a management key with Activity read access "
                                              "(add one: fleetctl secret set openrouter-management)"}, empty=False)
        self.assertIn('class="secret-form"', gap)
        self.assertIn('type="password"', gap)
        self.assertNotIn("value=\"sk", gap)
        other = self.render({"per_model_error": "OpenRouter activity returned HTTP 500"}, empty=False)
        self.assertNotIn("secret-form", other)
        self.assertIn("not available", other)
        shown = self.render({"per_model": [{"model": "a/b", "usd_30d": 1.5, "requests": 2}]}, empty=False)
        self.assertNotIn("secret-form", shown)

    def test_local_antigravity_cost_lists_its_models(self):
        html = self.render({"usage_cost": {"today_usd": 8.2, "last_30d_usd": 8.46, "today_tokens": 10, "last_30d_tokens": 20,
                                           "by_model": [{"model": "gemini-4-neon", "usd": 8.4, "tokens": 14569615, "requests": 72}]}})
        self.assertIn("Cost by model · last 30 days", html)
        self.assertIn("14,569,615 tokens", html)


class SecretDoor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.path, self.state = root / "overlay.json", root / "state"
        self.path.write_text(json.dumps(base_overlay()))
        self.system = FakeSystem(keychain={})
        # The console loads its own copy of fleetctl and so of secret_store; patch every copy, and make a
        # real subprocess call from any of them fail loudly instead of touching the machine's Keychain.
        copies = {id(m): m for m in (fleetctl.secret_store, console.fleetctl.secret_store)}.values()
        for module in copies:
            store = lambda module=module: module.__dict__["_RealStore"](
                env={}, runner=self.system, platform="darwin", which=self.system.which,
                directory=root / "config", op_env=lambda: None)
            module._RealStore = module.Store
            self.addCleanup(lambda module=module: module.__dict__.pop("_RealStore", None))
            for patcher in (patch.object(module, "Store", side_effect=store),
                            patch.object(module, "_run", side_effect=AssertionError("real process call"))):
                patcher.start()
                self.addCleanup(patcher.stop)

    def post(self, fields, headers, token=None):
        app = console.Console(self.path, self.state, 8768)
        self.app = app
        handler = object.__new__(console.make_handler(app))
        form = urllib.parse.urlencode({"t": app.form_token if token is None else token, **fields}).encode()
        handler.headers = {"Host": "127.0.0.1:8768", "Content-Length": str(len(form)), **headers}
        handler.rfile, handler.path, handler.command = io.BytesIO(form), "/secret", "POST"
        handler._send = Mock()
        with patch.object(console.Console, "refresh_now", return_value={}):
            handler.do_POST()
        return handler._send.call_args.args

    def signed(self):
        app = console.Console(self.path, self.state, 8768)
        return {**ORIGIN, "Cookie": f"{console.COOKIE}={app.session}", "Accept": "application/json"}

    def test_the_door_enforces_origin_sign_in_and_form_token(self):
        fields = {"name": "openrouter-management", "value": KEY}
        valid = self.signed()
        self.assertEqual(self.post(fields, {**valid, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post(fields, ORIGIN)[0], 401)
        self.assertEqual(self.post(fields, valid, token="bad")[0], 403)
        self.assertEqual(self.system.keychain, {})

    def test_save_goes_to_the_store_and_the_value_never_comes_back(self):
        status, payload, *_ = self.post({"name": "openrouter-management", "value": KEY}, self.signed())
        self.assertEqual(status, 200)
        self.assertEqual(self.system.keychain["openrouter-management"], KEY)
        self.assertNotIn(KEY, payload.decode())
        self.assertIn("Keychain", json.loads(payload)["message"])
        page = console.render_page(self.app.overview(refresh=False), self.app.form_token)
        self.assertNotIn(KEY, page)

    def test_bad_values_and_unknown_names_are_refused_without_echo(self):
        for fields in ({"name": "openrouter-management", "value": "short"},
                       {"name": "openrouter-management", "value": "has a space in it 123456"},
                       {"name": "other-secret", "value": KEY}):
            status, payload, *_ = self.post(fields, self.signed())
            self.assertEqual(status, 400)
            self.assertNotIn(KEY, payload.decode())
            self.assertNotIn(fields["value"], payload.decode())
        self.assertEqual(self.system.keychain, {})


if __name__ == "__main__":
    unittest.main()
