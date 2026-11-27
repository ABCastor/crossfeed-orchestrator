"""BR-SELECT-012, BR-GLANCE-001: Max means the pool's smartest model, and the glance shows token use."""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_fleetctl import fleetctl
from tests.test_selector import ROOT, quality_row, selector

NOW = dt.datetime(2026, 11, 27, 10, 0, tzinfo=dt.timezone.utc)
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


class MaxModel(unittest.TestCase):
    """BR-SELECT-012: the operator, 27 Nov: "if I set Claude to max, it will use the smartest model for everything"."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="max model ")
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"FLEET_QUOTA_POLICY": "clock_aware", "FLEET_IGNORE_QUOTA": "0"}).start()
        mock.patch.object(fleetctl, "_POLICY_CACHE", ("clock_aware", "test")).start()
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": ["claude"]}}
        self.roster["quota_pools"]["claude"]["value_capacity"] = {
            "weekly_usd_equivalent": 100, "source": "test plan"}
        self.roster["effort"][OPUS] = {"levels": {"claude": ["low", "high"]}, "default": "high"}
        self.roster["effort"][SONNET] = {"levels": {"claude": ["low", "high"]}, "default": "high"}
        (self.state / "evidence").mkdir()
        # Opus succeeds a little more and costs twenty times the quota debit: Normal takes the
        # cheaper Sonnet for an easy lookup, Max takes Opus without looking at either price.
        rows = [dict(quality_row(OPUS, level, pass_rate=rate), own_cost={"claude": {"percent_per_task": debit}})
                for level, rate, debit in (("low", .70, 4), ("high", .80, 20))]
        rows += [dict(quality_row(SONNET, level, pass_rate=rate), own_cost={"claude": {"percent_per_task": debit}})
                 for level, rate, debit in (("low", .68, .2), ("high", .78, 1))]
        (self.state / "evidence" / "levels.json").write_text(json.dumps({"rows": rows}))

    def select(self, level="normal", role="lookup", **kwargs):
        runtime = {"switches": {"claude": level}}
        return selector.select_option(self.roster, runtime, self.state, role, fleet=fleetctl,
                                      persist_receipts=False, no_explore=kwargs.pop("no_explore", True),
                                      collect_eligible=True, **kwargs)

    # BR-SELECT-012
    def test_max_runs_the_pools_smartest_model_where_normal_takes_the_cheaper_one(self):
        normal = self.select("normal")
        self.assertEqual((normal["choice"]["pool"], normal["choice"]["model_key"]), ("claude", SONNET))
        forced = self.select("forced")
        self.assertEqual((forced["choice"]["pool"], forced["choice"]["model_key"]), ("claude", OPUS))
        # The smarter model is the dearer one under Normal's expected cost, which is why it lost there.
        self.assertGreater(forced["choice"]["p_success"], normal["choice"]["p_success"])
        # Thinking effort still follows the job: every effort the roster gives the model stays
        # selectable, and an easy lookup takes the lower one.
        levels = {o["level"] for o in forced["eligible_options"] if o["pool"] == "claude"}
        self.assertEqual(levels, {"low", "high"})
        self.assertEqual(self.select("forced")["choice"]["level"], "low")
        self.assertIn("is at Max, which runs claude-opus-5-5 for every job", " ".join(forced["rejected"]))

    # BR-SELECT-012
    def test_other_pools_and_the_effort_pick_are_untouched(self):
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": ["claude", "codex"]}}
        self.roster["effort"]["gpt-6.1-sol"]["levels"]["codex"] = ["low", "high"]
        rows = [dict(quality_row(OPUS, "high", pass_rate=.80), own_cost={"claude": {"percent_per_task": 20}}),
                dict(quality_row(SONNET, "high", pass_rate=.78), own_cost={"claude": {"percent_per_task": 1}}),
                dict(quality_row("gpt-6.1-sol", "high", pass_rate=.9), own_cost={"codex": {"percent_per_task": 2}}),
                dict(quality_row("gpt-6.1-sol", "low", pass_rate=.85), own_cost={"codex": {"percent_per_task": .5}})]
        (self.state / "evidence" / "levels.json").write_text(json.dumps({"rows": rows}))
        forced = self.select("forced")
        codex = {o["level"] for o in forced["eligible_options"] if o["pool"] == "codex"}
        self.assertGreater(len(codex), 1, "another pool still ranks its own efforts")
        self.assertIn("claude-sonnet-5-5/low: claude is at Max, which runs claude-opus-5-5 for every job",
                      forced["rejected"])
        self.assertEqual(forced["choice"]["pool"], "claude")
        self.assertEqual(forced["choice"]["model_key"], OPUS)

    # BR-SELECT-012
    def test_a_forced_pool_model_still_obeys_role_refusals(self):
        self.roster["effort"][OPUS]["refuse_roles"] = {"lookup": "Opus is not spent on lookups by roster policy"}
        forced = self.select("forced")
        self.assertEqual(forced["choice"]["model_key"], SONNET)
        self.assertNotIn(OPUS, " ".join(o["model_key"] for o in forced["eligible_options"]))

    # BR-SELECT-012
    def test_a_forced_pool_model_still_obeys_the_data_class(self):
        # Lane-routed so each model carries its own policy: Opus is the public-only lane here.
        lane = {"quota_pool": "claude", "harness": "claude", "roles": ["lookup"], "access_status": "verified",
                "admission_status": "active", "allowed_modes": ["read-only", "write"], "allowed_pools": ["claude"]}
        self.roster["lanes"] += [{"lane_id": "claude-opus-private", "model_key": OPUS, "model": OPUS,
                                  "data_policy": "code-ok", **lane},
                                 {"lane_id": "claude-sonnet-any", "model_key": SONNET, "model": SONNET, **lane}]
        public = self.select("forced", role="implementation", data_class="code")
        self.assertEqual(public["choice"]["model_key"], OPUS)
        private = self.select("forced", role="implementation", data_class="private")
        self.assertNotEqual(private["choice"]["model_key"], OPUS)
        self.assertIn("code-ok lane refuses private material", " ".join(private["rejected"]))

    # BR-SELECT-012
    def test_exploration_never_samples_a_lesser_model_of_a_forced_pool(self):
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": ["claude"]},
                                             "exploration_cap": 1.0}
        for seed in range(6):
            chosen = self.select("forced", explore_seed=seed, no_explore=False)
            self.assertEqual(chosen["choice"]["model_key"], OPUS, seed)

    # BR-SELECT-012
    def test_equal_success_goes_to_the_newer_model(self):
        rows = [dict(quality_row(OPUS, "high", pass_rate=.80), own_cost={"claude": {"percent_per_task": 20}}),
                dict(quality_row(SONNET, "high", pass_rate=.80), own_cost={"claude": {"percent_per_task": 1}})]
        (self.state / "evidence" / "levels.json").write_text(json.dumps({"rows": rows}))
        forced = self.select("forced")
        # The fixture's card order puts Opus 5.5 first; the newer of two equal models wins.
        self.assertEqual(forced["choice"]["model_key"], OPUS)
        self.roster["model_cards"][OPUS]["order"] = 9
        forced = self.select("forced")
        self.assertEqual(forced["choice"]["model_key"], SONNET)


def runtime_with(tokens=None, cost=None, resets=None):
    snapshot = {"source": "claude-oauth-usage", "observed_at": fleetctl.iso(NOW), "available": True,
                "windows": {"weekly": {"used_percent": 26, "window_minutes": 10080,
                                       "reset_at": fleetctl.iso(NOW + dt.timedelta(hours=96)),
                                       "label": "Weekly"}}}
    if resets:
        snapshot["resets"] = resets
    if tokens:
        snapshot["tokens"] = tokens
    return {"quota_snapshots": {"claude": snapshot}}


class GlanceTokens(unittest.TestCase):
    """BR-GLANCE-001: the operator, 27 Nov: "how many tokens did you use? ... a very tiny, super elegant graph"."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="glance tokens ")
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.addCleanup(mock.patch.stopall)
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.roster["quota_pools"]["claude"]["quota_refresh"] = {"oracle": "claude", "local_usage_cost": True}

    def cost_cache(self, rows):
        daily = [{"day": day, "tokens": tokens, "usd": 1.0} for day, tokens in rows]
        path = self.state / "claude-cli-usage-cost.json"
        path.write_text(json.dumps({"observed_at": fleetctl.iso(NOW), "daily": daily, "last_7d_usd": 1.0}))
        path.chmod(0o600)   # the reader only trusts an owner-only cache

    def glance(self, runtime):
        with mock.patch.object(fleetctl, "utc_now", return_value=NOW):
            overview = fleetctl.fleet_overview(self.roster, runtime, self.state, NOW)
            return fleetctl.glance_payload(self.state / "overlay.json", self.roster, overview, "claude", self.state)

    # BR-GLANCE-001
    def test_the_glance_carries_today_and_a_fourteen_day_series(self):
        days = [(f"2026-11-{n:02d}", n * 100) for n in range(14, 28)]
        self.cost_cache(days)
        claude = next(row for row in self.glance(runtime_with())["pools"] if row["pool"] == "claude")
        self.assertEqual(claude["tokens"], {"today": 2700, "days": [n * 100 for n in range(14, 28)]})
        codex = next(row for row in self.glance(runtime_with())["pools"] if row["pool"] == "codex")
        self.assertNotIn("tokens", codex)   # no usage-cost cache for that pool: nothing is invented

    # BR-GLANCE-001
    def test_a_stale_series_has_no_tokens_today_and_no_cache_means_no_block(self):
        self.cost_cache([("2026-11-20", 100), ("2026-11-21", 200)])
        claude = next(row for row in self.glance(runtime_with())["pools"] if row["pool"] == "claude")
        self.assertEqual(claude["tokens"], {"today": 0, "days": [100, 200]})
        (self.state / "claude-cli-usage-cost.json").unlink()
        claude = next(row for row in self.glance(runtime_with())["pools"] if row["pool"] == "claude")
        self.assertNotIn("tokens", claude)

    # BR-GLANCE-001
    def test_a_reading_shares_the_resets_and_the_tokens_it_shows(self):
        tokens = {"today": 5, "days": [1, 2, 3, 4, 5]}
        resets = [{"label": "One full limit reset", "left": 1, "total": 1, "clears": ["weekly"],
                   "expires_at": "2026-12-10T22:00:00Z", "usable_now": True, "next": True, "key": "weekly",
                   "kind": "claude_grant"}]
        runtime = runtime_with(tokens, resets=resets)
        for row in runtime["quota_snapshots"].values():
            row["origin"] = "mac"
        (self.state / "runtime.json").write_text(json.dumps(runtime))
        shared = fleetctl.mac_readings.export(self.state, NOW, roster=self.roster)
        self.assertEqual(shared["snapshots"]["claude"]["resets"], resets)
        self.assertEqual(shared["snapshots"]["claude"]["tokens"], tokens)
        adopted = fleetctl.mac_readings.adopt({"quota_snapshots": {}}, {"snapshots": shared["snapshots"],
                                                                      "fetched_at": "2026-11-27T10:00:00Z"})
        self.assertEqual(adopted, ["claude"])
        claude = next(row for row in self.glance(runtime)["pools"] if row["pool"] == "claude")
        self.assertEqual(claude["limit_resets"], resets)
        self.assertEqual(claude["tokens"], tokens)


class UsdTotal(unittest.TestCase):
    """BR-SUBS-004: one USD total at ECB reference rates, fetched at most once a day."""

    ROSTER = {"quota_pools": {
        "claude": {"plan": {"billing": "subscription", "name": "Claude Max 20x", "price": 183, "currency": "GBP"}},
        "codex": {"plan": {"billing": "subscription", "name": "ChatGPT Pro", "price": 200, "currency": "USD",
                           "operator_share_percent": 50, "other_payer": "Edo"}},
    }}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="usd total ")
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.path = self.state / "subscriptions.json"   # never created: the overlay prices are enough

    def view(self, fetch, now=NOW):
        return fleetctl.subscriptions.view(self.path, self.ROSTER, {}, state_dir=self.state, now=now, fetch=fetch)

    def rates(self, as_of="2026-11-27"):
        return {"as_of": as_of, "base": "USD", "rates": {"GBP": 0.8, "EUR": 0.9}}

    # BR-SUBS-004
    def test_one_usd_total_converts_each_plan_and_the_operators_share(self):
        view = self.view(lambda: self.rates())
        # £183 / 0.8 = $228.75, $200 exact. A plan with no share set is the operator's alone (BR-SUBS-005).
        self.assertEqual(view["usd"], {"total": "428.75", "operator": "328.75",
                                       "rates_as_of": "2026-11-27", "approximate": True})
        self.assertEqual([(row["currency"], row["monthly"]) for row in view["subscriptions"]],
                         [("GBP", "183.00"), ("USD", "200.00")])
        # Each row carries its own USD figure, so the rows add up to the total.
        self.assertEqual([row.get("operator_usd") for row in view["subscriptions"]], ["228.75", "100.00"])
        split = json.loads(json.dumps(self.ROSTER))
        split["quota_pools"]["claude"]["plan"].update({"operator_share_percent": 50, "other_payer": "Edo"})
        both = fleetctl.subscriptions.view(self.path, split, {}, state_dir=self.state, now=NOW,
                                           fetch=lambda: self.rates())
        self.assertEqual((both["usd"]["total"], both["usd"]["operator"]), ("428.75", "214.38"))

    # BR-SUBS-004
    def test_the_rates_are_fetched_at_most_once_a_day_into_the_state_dir(self):
        calls = []

        def fetch():
            calls.append(1)
            return self.rates()

        self.view(fetch)
        self.view(fetch)
        self.assertEqual(len(calls), 1)
        cache = self.state / "fx-rates.json"
        self.assertEqual(json.loads(cache.read_text())["as_of"], "2026-11-27")
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.view(fetch, now=NOW + dt.timedelta(days=1))
        self.assertEqual(len(calls), 2)

    # BR-SUBS-004
    def test_a_failed_fetch_keeps_the_last_good_copy_and_an_unknown_rate_gives_no_block(self):
        self.view(lambda: self.rates())
        stale = self.view(lambda: None, now=NOW + dt.timedelta(days=2))
        self.assertEqual((stale["usd"]["total"], stale["usd"]["rates_as_of"]), ("428.75", "2026-11-27"))
        empty = self.view(lambda: None, now=NOW)
        self.assertIn("usd", empty)
        (self.state / "fx-rates.json").unlink()
        self.assertNotIn("usd", self.view(lambda: None))
        self.assertNotIn("usd", self.view(lambda: {"as_of": "2026-11-27", "rates": {"EUR": 0.9}}))

    # BR-SUBS-004
    def test_a_page_render_never_reaches_the_network_and_the_refresh_fills_the_cache_once_a_day(self):
        with mock.patch.object(fleetctl.subscriptions.urllib.request, "build_opener") as build:
            self.assertNotIn("usd", self.view(None))
        build.assert_not_called()
        calls = []
        fetch = lambda: (calls.append(1), self.rates())[1]  # noqa: E731 - a two-line seam
        self.assertTrue(fleetctl.subscriptions.refresh_fx(self.state, now=NOW, fetch=fetch))
        self.assertFalse(fleetctl.subscriptions.refresh_fx(self.state, now=NOW, fetch=fetch))
        self.assertEqual(len(calls), 1)
        self.assertIn("usd", self.view(None))

    # BR-SUBS-004
    def test_the_fetch_replaces_the_network_and_a_test_never_reaches_it(self):
        from unittest import mock
        with mock.patch.object(fleetctl.subscriptions.urllib.request, "build_opener") as build:
            view = self.view(lambda: self.rates())
        build.assert_not_called()
        self.assertIn("usd", view)
        fixed = json.dumps({"as_of": "2026-11-27", "rates": {"GBP": 0.8}})
        with mock.patch.dict(os.environ, {fleetctl.subscriptions.FX_ENV: fixed}), \
                mock.patch.object(fleetctl.subscriptions.urllib.request, "build_opener") as build:
            self.assertEqual(fleetctl.subscriptions._fx_fetch(), {"as_of": "2026-11-27", "base": "USD",
                                                                  "rates": {"GBP": 0.8}})
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
