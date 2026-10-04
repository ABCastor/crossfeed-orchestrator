"""Which models are older, which are retired, and every limit a plan has.

Two requirements: Gemini 3.6 and 3.7 Flash are older models and belong in the older models section; and
the console shows every limit it can, not just the five-hour or the weekly one, with its reset, with agents
fully aware of them too.

The fix for the first is a rule, not two rows: a model is older when its provider can run a newer
version of the same line, so the next superseded model moves by itself. These tests hold the rule,
the retired state (a model its vendor withdrew can never be offered or started) and the limits as
the console and `brief` both read them.
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_console import console

fleetctl = console.fleetctl
NOW = dt.datetime(2026, 9, 30, 8, 0, tzinfo=dt.timezone.utc)


def lane(lane_id, model, pool, roles=(), harness="agy"):
    return {"lane_id": lane_id, "model_key": model, "quota_pool": pool, "harness": harness,
            "provider": "antigravity", "selector": f"{model}-high", "access_status": "verified",
            "admission_status": "active", "allowed_modes": ["read-only", "write"], "roles": list(roles),
            "capabilities": {"input": ["text"]}, "max_parallel": 2}


def gemini_roster():
    """A routed provider the way the live roster shapes Antigravity: three Flash versions kept
    admitted (two as fallbacks) beside a Pro, and no card says which is older."""
    return {
        "schema_version": 3,
        "quota_pools": {"gem": {"label": "Antigravity · Gemini", "plan": {"name": "Google AI Pro"}}},
        "lanes": [lane("flash-36", "gemini-3.6-flash", "gem"), lane("flash-37", "gemini-3.7-flash", "gem"),
                  lane("flash-38", "gemini-3.8-flash", "gem", roles=["default"]),
                  lane("pro-31", "gemini-3.1-pro", "gem")],
        "catalogue_only": [], "model_evidence": {}, "model_cards": {},
        "routing": {"roles": {"default": {"quality_first": ["flash-38", "flash-37", "flash-36", "pro-31"]}}},
    }


def codex_roster():
    def card(name, order, status="current", **more):
        return dict({"pool": "codex", "name": name, "status": status, "order": order}, **more)
    return {
        "schema_version": 3,
        "quota_pools": {"codex": {"label": "Codex", "plan": {"name": "Pro"}}},
        "lanes": [], "catalogue_only": [], "routing": {"roles": {}}, "model_evidence": {},
        "model_cards": {"gpt-6-astra": card("GPT-6 Astra", 1), "gpt-6.1-sol": card("GPT-6.1 Sol", 2),
                        "gpt-6-luna": card("GPT-6 Luna", 3),
                        "gpt-5.6-luna": card("GPT-5.6 Luna", 4, status="older"),
                        "gpt-5.3-codex-spark": card("Spark", 7, status="older")},
    }


def overview_of(roster, runtime=None, now=NOW):
    with tempfile.TemporaryDirectory() as directory:
        return fleetctl.fleet_overview(roster, runtime or {}, Path(directory), now)


class OlderRuleTests(unittest.TestCase):
    def test_a_name_gives_its_line_and_its_version(self):
        for key, line in {
            "gemini-3.8-flash": ("gemini-flash", (3, 8)), "gemini-3.1-pro": ("gemini-pro", (3, 1)),
            "gpt-6-sol": ("gpt-sol", (6,)), "gpt-6.1-sol": ("gpt-sol", (6, 1)), "gpt-5.6-sol": ("gpt-sol", (5, 6)),
            "claude-fable-5-1": ("claude-fable", (5, 1)), "claude-fable-5": ("claude-fable", (5,)),
            "claude-opus-4-6-thinking": ("claude-opus-thinking", (4, 6)),
            "deepseek-v4-flash": ("deepseek-v-flash", (4,)), "qwen3.7-max": ("qwen-max", (3, 7)),
            "kimi-k2.7-code": ("kimi-k-code", (2, 7)),
            # an alias that tracks the newest model has no version, so nothing can be newer than it
            "gemini-flash-latest": ("gemini-flash-latest", ()), "claude-haiku": ("claude-haiku", ()),
        }.items():
            self.assertEqual(fleetctl.model_line(key), line, key)
        self.assertEqual(fleetctl.model_line("odd-name-2", {"line": "Gemini-Flash"}), ("gemini-flash", (2,)))

    def test_a_superseded_flash_is_older_on_a_routed_provider_with_no_card_saying_so(self):
        roster = gemini_roster()
        self.assertEqual(fleetctl.current_models(roster, "gem"), ["gemini-3.1-pro", "gemini-3.8-flash"])
        self.assertEqual(fleetctl.superseded_by(roster, "gem", "gemini-3.7-flash"), "gemini-3.8-flash")
        self.assertEqual(fleetctl.superseded_by(roster, "gem", "gemini-3.6-flash"), "gemini-3.8-flash")
        self.assertIsNone(fleetctl.superseded_by(roster, "gem", "gemini-3.1-pro"))   # another line
        pool = overview_of(roster)["pools"][0]
        older = [o["model"] for o in pool["options"] if not o["current"]]
        self.assertEqual(sorted(older), ["gemini-3.6-flash", "gemini-3.7-flash"])
        self.assertTrue(all(o["run_as"] for o in pool["options"]))   # still runnable: each keeps its switch

    def test_the_next_superseded_model_moves_by_itself(self):
        roster = gemini_roster()
        roster["lanes"].append(lane("flash-39", "gemini-3.9-flash", "gem"))
        self.assertEqual(fleetctl.current_models(roster, "gem"), ["gemini-3.1-pro", "gemini-3.9-flash"])
        self.assertTrue(fleetctl.model_is_older(roster, "gem", "gemini-3.8-flash"))
        roster = codex_roster()   # a direct provider: a new card is all it takes, the old card still says "current"
        old_card = roster["model_cards"].pop("gpt-6.1-sol")
        roster["model_cards"]["gpt-6-sol"] = dict(old_card, name="GPT-6 Sol")
        roster["model_cards"]["gpt-6.1-sol"] = {"pool": "codex", "name": "GPT-6.1 Sol", "status": "current", "order": 2}
        self.assertEqual(fleetctl.current_models(roster, "codex"), ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"])
        self.assertEqual(fleetctl.superseded_by(roster, "codex", "gpt-6-sol"), "gpt-6.1-sol")

    def test_the_rosters_own_word_wins_over_the_version_rule(self):
        roster = gemini_roster()
        roster["model_cards"]["gemini-3.6-flash"] = {"superseded_by": "gemini-3.7-flash", "superseded_on": "2026-08-13"}
        self.assertEqual(fleetctl.superseded_by(roster, "gem", "gemini-3.6-flash"), "gemini-3.7-flash")
        roster["model_cards"]["gemini-3.1-pro"] = {"status": "older"}   # no successor, older on the roster's word
        self.assertEqual(fleetctl.current_models(roster, "gem"), ["gemini-3.8-flash"])

    def test_an_older_model_that_is_on_never_stands_in_for_a_model_that_is_off(self):
        roster = gemini_roster()
        roster["routing"]["roles"]["default"]["quality_first"] = ["flash-38"]
        runtime = {}
        fleetctl.set_model_toggle(runtime, roster, "gem", "gemini-3.8-flash", False)
        kept, _ = fleetctl.apply_model_toggles(["flash-38"], roster, runtime)
        self.assertEqual(kept, ["pro-31"])   # the current model that is on, not the two older Flash versions
        pool = overview_of(roster, runtime)["pools"][0]
        self.assertEqual(pool["models"]["state"], "one")   # 1 of 2 current on; older ones are not counted


class RetiredTests(unittest.TestCase):
    def setUp(self):
        self.roster = codex_roster()
        self.roster["model_cards"]["gpt-5.3-codex-spark"]["retired_on"] = "2026-09-14"

    def test_a_retired_model_has_no_switch_and_is_never_run(self):
        self.assertNotIn("gpt-5.3-codex-spark", fleetctl.choosable_models(self.roster, "codex"))
        # nothing is switched off, and a task that asks for it by name still gets a model that exists
        self.assertEqual(fleetctl.model_run_as(self.roster, {}, "codex", "gpt-5.3-codex-spark"), "gpt-6-luna")
        self.assertIsNone(fleetctl.model_run_as(self.roster, {}, "codex", "gpt-6-astra"))
        with self.assertRaises(ValueError):
            fleetctl.set_model_toggle({}, self.roster, "codex", "gpt-5.3-codex-spark", True)
        option = next(o for o in overview_of(self.roster)["pools"][0]["options"] if o["model"] == "gpt-5.3-codex-spark")
        self.assertEqual((option["current"], option["run_as"], option["retired_on"]), (False, None, "2026-09-14"))

    def test_a_retirement_date_still_to_come_changes_nothing(self):
        self.roster["model_cards"]["gpt-5.3-codex-spark"]["retired_on"] = "2999-01-01"
        self.assertIn("gpt-5.3-codex-spark", fleetctl.choosable_models(self.roster, "codex"))
        # Still served does not justify keeping an older model on.
        self.assertEqual(fleetctl.model_run_as(self.roster, {}, "codex", "gpt-5.3-codex-spark"), "gpt-6-luna")

    def test_a_retired_lane_is_neither_routed_nor_leased(self):
        roster = gemini_roster()
        roster["model_cards"]["gemini-3.8-flash"] = {"retired_on": "2026-09-01"}
        chosen = fleetctl.choose_lane(roster, {}, "default", "read-only", "text")
        self.assertEqual(chosen["lane_id"], "flash-37")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(fleetctl.FleetError, "retired"):
                fleetctl.acquire_lease(Path(directory), roster, "flash-38", 60)
        self.assertIn("retired", fleetctl.model_gate(roster, {}, "gemini-3.8-flash-high", harness="agy"))

    def test_the_brief_tells_agents_it_is_retired_and_what_runs_instead(self):
        brief = fleetctl.render_brief(overview_of(self.roster), verbose=True)
        self.assertIn("gpt-5.3-codex-spark retired (gpt-6-luna runs instead)", brief)


def snapshot(windows, idle=None, at=NOW):
    data = {"source": "codexbar", "observed_at": fleetctl.iso(at - dt.timedelta(minutes=2)), "windows": windows}
    if idle:
        data["idle_windows"] = idle
    return data


def window(used, hours, minutes=None, label=None):
    entry = {"used_percent": used, "reset_at": fleetctl.iso(NOW + dt.timedelta(hours=hours))}
    if minutes:
        entry["window_minutes"] = minutes
    if label:
        entry["label"] = label
    return entry


class LimitTests(unittest.TestCase):
    def roster(self):
        return {
            "schema_version": 3, "lanes": [], "catalogue_only": [], "routing": {"roles": {}},
            "quota_pools": {
                "claude": {"label": "Claude", "plan": {"name": "Max"}, "quota_refresh": {"oracle": "codexbar", "provider": "claude"}},
                "go": {"label": "OpenCode Go", "plan": {"name": "Go"}, "quota_refresh": {"oracle": "codexbar", "provider": "opencodego"}},
                "paid": {"label": "Gemini API", "daily_usd_cap": 1.0},
            },
        }

    def runtime(self):
        return {"quota_snapshots": {
            "claude": snapshot({"primary": window(9, 2, 300, "Session"), "secondary": window(80, 150, 10080, "Weekly"),
                                "claude-weekly-scoped-fable": window(0, 150, 10080, "Fable only")}),
            "go": snapshot({"secondary": window(12, 100, 10080, "Weekly"), "tertiary": window(51, 400, 43200, "Monthly"),
                            "renewal": window(0, 400, None, "Renews")},
                           idle={"primary": {"used_percent": 0, "window_minutes": 300, "label": "5-hour"}}),
        }}

    def limits(self):
        pools = {p["pool"]: p for p in overview_of(self.roster(), self.runtime())["pools"]}
        return pools

    def test_every_limit_is_listed_shortest_window_first_with_what_it_counts(self):
        pools = self.limits()
        self.assertEqual([(l["title"], l["scope"], l["used_percent"]) for l in pools["claude"]["limits"]],
                         [("5-hour", None, 9), ("Weekly", None, 80), ("Weekly", "Fable only", 0)])
        self.assertEqual([l["state"] for l in pools["claude"]["limits"]], ["abundant", "conserve", "abundant"])
        # the one the old page showed (the fullest) is still there for routing, beside the rest
        self.assertEqual(pools["claude"]["quota"]["used_percent"], 80)

    def test_a_limit_that_has_not_started_is_shown_and_a_renewal_date_is_not_a_limit(self):
        go = self.limits()["go"]
        self.assertEqual([(l["title"], l["used_percent"], l["reset_at"] is None) for l in go["limits"]],
                         [("5-hour", 0, True), ("Weekly", 12, False), ("Monthly", 51, False)])
        self.assertEqual(go["renews_at"], fleetctl.iso(NOW + dt.timedelta(hours=400)))

    def test_a_paid_pool_shows_its_daily_budget(self):
        paid = self.limits()["paid"]["limits"]
        self.assertEqual([(l["title"], l["kind"], l["cap_usd"], l["resets"]) for l in paid], [("Daily budget", "budget", 1.0, "midnight")])

    def test_names_come_from_the_window_and_the_sources_own_label(self):
        name = fleetctl.limit_name
        self.assertEqual(name("primary", {"window_minutes": 300, "label": "Session"}), ("5-hour", None))
        self.assertEqual(name("secondary", {"window_minutes": 10080}), ("Weekly", None))
        self.assertEqual(name("tertiary", {"window_minutes": 43200, "label": "Monthly"}), ("Monthly", None))
        self.assertEqual(name("x", {"window_minutes": 10080, "label": "Fable only"}), ("Weekly", "Fable only"))
        # a label that only repeats the provider's own name adds nothing
        self.assertEqual(name("g5", {"window_minutes": 300, "label": "Gemini 5-hour"}, "Antigravity · Gemini"), ("5-hour", None))
        self.assertEqual(name("c", {"window_minutes": 10080, "label": "Claude/GPT weekly"}, "Antigravity · Claude and GPT"), ("Weekly", None))
        self.assertEqual(name("primary", {"label": "Premium"}), ("Premium", None))   # no length known: the source's word
        # an extra limit without a label keeps its id rather than a guess
        self.assertEqual(name("codex-base-model-inference", {"window_minutes": 10080}), ("Weekly", "codex-base-model-inference"))

    def test_a_reset_is_said_as_a_person_says_it(self):
        here = NOW.astimezone()
        at = lambda **delta: fleetctl.iso(here + dt.timedelta(**delta))
        self.assertEqual(fleetctl.reset_words(at(hours=1), NOW), f"today {(here + dt.timedelta(hours=1)):%H:%M}")
        tomorrow = (here + dt.timedelta(days=1)).replace(hour=2, minute=0)
        self.assertEqual(fleetctl.reset_words(fleetctl.iso(tomorrow), NOW), "tomorrow 02:00")
        later = (here + dt.timedelta(days=3)).replace(hour=19, minute=16)
        self.assertEqual(fleetctl.reset_words(fleetctl.iso(later), NOW), f"{later:%a} {later.day} {later:%b} 19:16")
        far = here + dt.timedelta(days=17)
        self.assertEqual(fleetctl.reset_words(fleetctl.iso(far), NOW), f"{far:%a} {far.day} {far:%b}")
        # one second before the hour reads as the hour
        edge = (here + dt.timedelta(days=1)).replace(hour=1, minute=59, second=59)
        self.assertEqual(fleetctl.reset_words(fleetctl.iso(edge), NOW), "tomorrow 02:00")

    def test_agents_read_every_limit_and_its_reset(self):
        brief = fleetctl.render_brief(overview_of(self.roster(), self.runtime()), verbose=True)
        lines = brief.splitlines()
        at = lines.index("Limits (used, then when each resets; times are this machine's):")
        claude = next(line for line in lines[at:] if line.startswith("  claude:"))
        self.assertIn("5-hour 9%, resets today", claude)
        self.assertIn("(in 2h 0m)", claude)
        self.assertIn("weekly 80% CONSERVE, resets", claude)
        self.assertIn("weekly Fable only 0%, resets", claude)
        go = next(line for line in lines[at:] if line.startswith("  go:"))
        self.assertIn("5-hour 0%, not started", go)
        self.assertIn("monthly 51%", go)
        self.assertIn("plan renews", go)
        self.assertIn("  paid: daily budget $0.00 of $1.00, resets at midnight", lines)

    def test_codexbar_labels_and_unstarted_windows_are_kept(self):
        payload = [{"provider": "opencodego", "rateWindowLabels": {"primary": "5-hour", "secondary": "Weekly"},
                    "usage": {"primary": {"windowMinutes": 300, "usedPercent": 0},
                              "secondary": {"windowMinutes": 10080, "usedPercent": 11.3, "resetsAt": "2026-10-04T23:59:59Z"},
                              "extraRateWindows": [{"id": "scoped", "title": "Fable only",
                                                    "window": {"windowMinutes": 10080, "usedPercent": 0, "resetsAt": "2026-10-06T13:00:00Z"}},
                                                   {"id": "mail", "title": "someone@example.com",
                                                    "window": {"usedPercent": 1, "resetsAt": "2026-10-06T13:00:00Z"}}]}}]
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "codexbar"
            fake.write_text("#!/bin/sh\ncat <<'JSON'\n" + json.dumps(payload) + "\nJSON\n", encoding="utf-8")
            fake.chmod(0o755)
            seen = fleetctl.codexbar_observed("opencodego", binary=str(fake))
        self.assertEqual(seen["windows"]["secondary"]["label"], "Weekly")
        self.assertEqual(seen["windows"]["scoped"]["label"], "Fable only")
        self.assertNotIn("label", seen["windows"]["mail"])   # an address is never kept
        self.assertEqual(seen["idle_windows"], {"primary": {"used_percent": 0, "window_minutes": 300, "label": "5-hour"}})


if __name__ == "__main__":
    unittest.main()
