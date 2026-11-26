"""The Usage and Share pages: cost estimates, charts, who owes what. Synthetic data only."""
import datetime as dt
import json
import re
import time
import unittest
from unittest import mock

from scripts import sharing, usage_page
from tests import test_console as base_tests
console = base_tests.console

NOW = time.time()


def cost(scale=1.0, unpriced=False):
    today = dt.datetime.fromtimestamp(NOW).astimezone().date()
    days = [(today - dt.timedelta(days=29 - n)).isoformat() for n in range(30)]
    daily = [{"day": d, "tokens": 1000 * (n + 1), "usd": round(scale * (n % 5 + 1), 4)} for n, d in enumerate(days)]
    return {"today_usd": daily[-1]["usd"], "last_7d_usd": None if unpriced else 70.0 * scale, "last_30d_usd": 300.0 * scale,
            "today_tokens": 30000, "last_7d_tokens": 90000, "last_30d_tokens": 465000, "daily": daily,
            "hourly": [[int(NOW // 3600) - 3, 10, 1.5]], "age_s": 90, "stale": False,
            "by_model": [{"model": "claude-opus-5-5", "tokens": 300000, "usd": 200.0 * scale, "usd_7d": 50.0, "usd_today": 4.0},
                         {"model": "claude-haiku-5-5", "tokens": 100000, "usd": 100.0 * scale, "usd_7d": 20.0, "usd_today": None}]}


def window(name, title, minutes, used, elapsed, reset="Mon 09:00"):
    reset_at = dt.datetime.fromtimestamp(NOW + 86400, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"name": name, "title": title, "scope": None, "kind": "window", "minutes": minutes, "used_percent": used,
            "reset_at": reset_at, "resets": reset, "state": "x",
            "pace": {"elapsed_percent": elapsed, "status": "deficit" if used > elapsed else "surplus",
                     "words": f"{abs(used - elapsed):g}% {'in deficit' if used > elapsed else 'surplus'}"}}


def overview():
    return {"pools": [
        {"pool": "claude", "label": "Claude", "subscription": "claude", "usage_cost": cost(),
         "limits": [window("weekly", "Weekly", 10080, 60, 40), window("claude-session", "5-hour", 300, 5, 50)]},
        {"pool": "codex", "label": "Codex", "subscription": "codex", "usage_cost": cost(.5, unpriced=True),
         "limits": [window("weekly", "Weekly", 10080, 20, 40)]},
        {"pool": "opencode-go", "label": "Go", "subscription": "opencode-go", "limits": [window("weekly", "Weekly", 10080, 4, 40)]},
    ]}


SERIES = {"claude": {"weekly": {"label": "Weekly", "minutes": 10080,
                                "points": [[NOW - 3 * 86400, 10], [NOW - 86400, 30], [NOW - 3600, 58]], "resets": []}}}


def render(series=None):
    return usage_page.render_usage(overview(), SERIES if series is None else series, NOW,
                                   lambda p: p["label"], lambda m: m.upper(), "Cost logs read 2m ago")


class Pure(unittest.TestCase):
    def test_cost_table_has_today_week_month_per_provider_and_a_total_and_says_the_basis_once(self):
        page = render()
        self.assertEqual(page.count("API list prices"), 1)
        for heading in ("Today", "7 days", "30 days"):
            self.assertIn(f'<th scope="col">{heading}</th>', page)
        self.assertIn("$300.00", page)               # Claude, 30 days
        self.assertIn("$150.00", page)               # Codex, 30 days
        self.assertIn("$450.00", page)               # total
        self.assertIn("≥$70.00", page)               # total of 7 days: Codex has an unpriced model, so a floor
        self.assertIn('class="us-none"', page)       # and its own 7-day cell is a dash, not $0
        self.assertNotIn("Go</th>", page.split("Allowances")[0])  # no cost logs, no cost row

    def test_daily_chart_has_one_bar_per_day_and_provider_keys(self):
        chart = usage_page.daily_chart(usage_page.cost_rows(overview()["pools"], lambda p: p["label"]))
        self.assertEqual(len(re.findall(r"<rect ", chart)), 60)       # 30 days x 2 providers
        self.assertIn("Claude", chart)
        self.assertIn("Codex", chart)
        self.assertEqual(usage_page.daily_chart([{"name": "x", "cost": {"today_usd": 1}}]).count("<svg"), 0)

    def test_by_model_lists_the_largest_first_with_three_windows(self):
        table = usage_page.model_table(usage_page.cost_rows(overview()["pools"], lambda p: p["label"]), lambda m: m.upper())
        self.assertLess(table.index("CLAUDE-OPUS-5-5"), table.index("CLAUDE-HAIKU-5-5"))
        self.assertIn("$200.00", table)
        self.assertIn('class="us-none"', table)       # a model with no list price today shows a dash

    def test_every_window_gets_a_pace_bar_with_its_tick_and_running_ahead_is_marked(self):
        page = usage_page.pace_rows(overview()["pools"], lambda p: p["label"])
        self.assertEqual(page.count('class="us-track"'), 4)
        self.assertIn('<li class="behind">', page)
        self.assertIn('width="60.00"', page)
        self.assertIn('x="40.00"', page)

    def test_history_chart_draws_the_stepped_line_per_subscription_and_never_invents_a_past(self):
        page = usage_page.history_charts(overview()["pools"], lambda g, m: m[0]["label"],
                                         lambda p, l: l["title"], SERIES, NOW)
        self.assertEqual(page.count('<figure class="chart mini"'), 1)      # only Claude has two readings or more
        self.assertIn('class="line s1"', page)
        self.assertNotIn("5-hour", page)                                   # five-hour windows are not charted
        young = usage_page.history_charts(overview()["pools"], lambda g, m: m[0]["label"], lambda p, l: l["title"], {}, NOW)
        self.assertNotIn("<svg", young)
        self.assertIn("fills in as readings arrive", young)

    def test_pages_carry_no_style_attribute_script_or_remote_url(self):
        model = sharing.build({"subscriptions": [{"id": "claude", "name": "Claude Max", "amount": "100", "monthly": "100.00",
                               "currency": "EUR", "operator_share_percent": "50", "other_payer": "<b>Rocco</b>", "pools": ["claude"]}]},
                              overview(), NOW)
        cards = [usage_page.card_html(model, p, sharing.card_lines(model, p["key"]), sharing.card_text(model, p["key"]))
                 for p in model["people"]]
        for page in (render(), usage_page.render_share(model, cards),
                     usage_page.render_print(sharing.card_lines(model, "b-rocco-b"))):
            self.assertNotRegex(page, r"\sstyle=")
            self.assertNotRegex(page, r"<script|https?://")
        shown = usage_page.render_share(model, cards)
        self.assertNotIn("<b>Rocco</b>", shown)                            # a name is text, never markup
        self.assertIn("&lt;b&gt;Rocco&lt;/b&gt;", shown)

    def test_share_page_says_what_cannot_be_known_and_empty_states_are_plain(self):
        empty = usage_page.render_share({"people": [], "unavailable": False}, [])
        self.assertIn("Nothing to share yet", empty)
        self.assertIn("Who used a shared login", empty)
        self.assertIn("docs/SHARING.md", empty)
        self.assertIn("unreadable", usage_page.render_share({"people": [], "unavailable": True}, []))
        self.assertIn("No usage logs are read yet", usage_page.render_usage({"pools": []}, {}, NOW, lambda p: "", lambda m: m, ""))


class Routes(base_tests.ConsoleServerTests):
    """The signed pages on a real loopback server."""

    def test_usage_and_share_need_the_session_and_render_for_it(self):
        for path in ("/usage", "/share", "/share/print?person=me"):
            response, _ = self.request("GET", path)
            self.assertEqual(response.status, 401, path)
        headers = {"Cookie": self.cookie()}
        response, body = self.request("GET", "/usage", headers=headers)
        page = body.decode()
        self.assertEqual(response.status, 200)
        self.assertIn('<a href="/usage" aria-current="page">', page)
        self.assertIn("<h1>Usage</h1>", page)
        response, body = self.request("GET", "/share", headers=headers)
        self.assertEqual(response.status, 200)
        self.assertIn("<h1>Share</h1>", body.decode())
        response, body = self.request("GET", "/share/print?person=nobody", headers=headers)
        self.assertEqual(response.status, 200)
        self.assertIn("No such card", body.decode())
        self.assertIn("/usage", self.request("GET", "/", headers=headers)[1].decode())   # the nav link on every page

    def test_the_content_security_policy_still_forbids_inline_styles_and_scripts(self):
        response, _ = self.request("GET", "/usage", headers={"Cookie": self.cookie()})
        policy = response.getheader("Content-Security-Policy")
        self.assertIn("style-src 'self'", policy)
        self.assertNotIn("unsafe-inline", policy)


for _name in [n for n in dir(base_tests.ConsoleServerTests) if n.startswith("test_")]:
    setattr(Routes, _name, None)        # reuse the server fixture only; the base class's tests run in their own module


if __name__ == "__main__":
    unittest.main()
