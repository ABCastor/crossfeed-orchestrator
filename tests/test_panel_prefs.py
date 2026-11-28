"""The panel preferences file both platforms read: defaults, validation, one atomic 600 write.

The Mac menu and the Linux web panel must agree about what a legal document is, so every rule here
is exercised through the same module both call, and once through `fleetctl panel`.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_fleetctl import fleetctl

panel_prefs = fleetctl.panel_prefs
ROOT = Path(__file__).resolve().parents[1]


class Defaults(unittest.TestCase):
    def test_a_missing_file_is_the_defaults_with_every_tab_filled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "panel.json"
            doc = panel_prefs.load(path)
            self.assertFalse(path.exists())  # reading never writes
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["show"], "used")
        self.assertEqual(doc["tabs"][0], "overview")
        self.assertEqual(doc["tabs"][-1], "other")
        self.assertEqual(doc["tiles"]["overview"], ["alert", "providers", "subscriptions", "tokens", "links"])
        self.assertEqual(doc["tiles"]["claude"], ["limits", "resets", "credits", "tokens", "policy", "status", "links"])
        self.assertEqual(doc["hidden_tabs"], [])
        self.assertEqual(doc["hidden_tiles"], {})

    def test_absent_tab_ids_are_appended_and_overview_and_other_keep_the_edges(self):
        doc = panel_prefs.normalize({"tabs": ["other", "codex", "overview"]})
        self.assertEqual(doc["tabs"][:3], ["overview", "codex", "claude"])
        self.assertEqual(doc["tabs"][-1], "other")


class Validation(unittest.TestCase):
    def test_an_unknown_tile_and_an_unreadable_show_fall_back_to_something_drawable(self):
        doc = panel_prefs.normalize({"show": "sideways", "tiles": {"claude": ["limits", "nope", "plugin:ci/runs", "plugin:Bad/x"]}})
        self.assertEqual(doc["show"], "used")
        self.assertEqual(doc["tiles"]["claude"], ["limits", "plugin:ci/runs", "status"])

    def test_hidden_tiles_keep_the_tabs_own_tiles_and_visible_tiles_reads_both_lists(self):
        doc = panel_prefs.normalize({"tiles": {"claude": ["limits", "resets", "links"]},
                                     "hidden_tiles": {"claude": ["resets", "not-a-tile"]}})
        self.assertEqual(doc["hidden_tiles"]["claude"], ["resets"])
        self.assertEqual(panel_prefs.visible_tiles(doc, "claude"), ["limits", "status", "links"])

    def test_a_document_that_is_not_an_object_is_refused_not_half_applied(self):
        for bad in ([], "x", 3, None):
            with self.assertRaises(panel_prefs.PrefsError):
                panel_prefs.normalize(bad)

    def test_unknown_keys_survive_a_round_trip(self):
        doc = panel_prefs.normalize({"show": "left", "future_field": {"a": 1}, "schema": 1})
        self.assertEqual(doc["future_field"], {"a": 1})
        self.assertEqual(doc["show"], "left")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "panel.json"
            panel_prefs.save(doc, path)
            self.assertEqual(json.loads(path.read_text())["future_field"], {"a": 1})

    def test_hidden_tabs_drop_ids_the_order_does_not_have_and_never_hide_overview(self):
        doc = panel_prefs.normalize({"hidden_tabs": ["other", "overview", "notreal"]})
        self.assertEqual(doc["hidden_tabs"], ["other"])


class RoundTwelveKeys(unittest.TestCase):
    def test_defaults_for_every_new_key(self):
        doc = panel_prefs.normalize({})
        self.assertEqual(doc["appearance"], "system")
        self.assertEqual(doc["refresh"], {"on_open": True, "every_minutes": 5})
        self.assertEqual(doc["open_on"], "last")
        self.assertEqual(doc["last_tab"], "overview")
        self.assertEqual(doc["budgets"], {})
        self.assertEqual(doc["tile_options"], {})
        self.assertEqual(doc["menubar"]["icon"], "provider")
        self.assertEqual(doc["menubar"]["text"], "window")

    def test_api_pools_are_default_tabs_with_their_own_tiles_and_other_stays_last(self):
        doc = panel_prefs.normalize({})
        self.assertEqual(doc["tabs"][-4:], ["openrouter-free", "gemini-metered", "chatgpt-work", "other"])
        self.assertEqual(doc["tiles"]["gemini-metered"],
                         ["limits", "spend", "balance", "models", "activity", "status", "links"])
        self.assertEqual(doc["tiles"]["chatgpt-work"], ["limits", "activity", "models", "status", "links"])   # a relay reports no money
        self.assertEqual(doc["tiles"]["other"], ["other"])

    def test_a_tab_saved_before_the_api_tabs_existed_gains_them_before_other(self):
        doc = panel_prefs.normalize({"tabs": ["overview", "codex", "claude", "other"]})
        self.assertEqual(doc["tabs"][:3], ["overview", "codex", "claude"])
        self.assertIn("openrouter-free", doc["tabs"])
        self.assertEqual(doc["tabs"][-1], "other")

    def test_the_status_tile_joins_a_provider_list_saved_before_it_existed_but_never_overview_or_other(self):
        doc = panel_prefs.normalize({"tiles": {"codex": ["limits", "links"], "other": ["other"], "overview": ["alert", "links"]}})
        self.assertEqual(doc["tiles"]["codex"], ["limits", "status", "links"])
        self.assertEqual(doc["tiles"]["other"], ["other"])
        self.assertEqual(doc["tiles"]["overview"], ["alert", "links"])   # Status is pinned on Overview only when the operator adds it

    def test_choices_outside_their_vocabulary_fall_back_to_the_defaults(self):
        doc = panel_prefs.normalize({"appearance": "sepia", "open_on": "nowhere", "last_tab": "Bad Id",
                                     "refresh": {"on_open": "yes", "every_minutes": 5000, "keep": 1},
                                     "menubar": {"icon": "tanks", "text": "loud", "extra": "cost", "future": 1}})
        self.assertEqual(doc["appearance"], "system")
        self.assertEqual(doc["open_on"], "last")
        self.assertEqual(doc["last_tab"], "overview")
        self.assertEqual(doc["refresh"], {"on_open": True, "every_minutes": 5, "keep": 1})   # unknown nested keys survive
        self.assertEqual((doc["menubar"]["icon"], doc["menubar"]["text"], doc["menubar"]["extra"]), ("provider", "window", "cost"))
        self.assertEqual(doc["menubar"]["future"], 1)

    def test_good_choices_survive_and_open_on_may_name_any_listed_tab(self):
        doc = panel_prefs.normalize({"appearance": "dark", "open_on": "codex", "last_tab": "claude",
                                     "refresh": {"on_open": False, "every_minutes": 0},
                                     "menubar": {"icon": "none", "show": "claude", "window": "Week", "count": "left"}})
        self.assertEqual((doc["appearance"], doc["open_on"], doc["last_tab"]), ("dark", "codex", "claude"))
        self.assertEqual(doc["refresh"], {"on_open": False, "every_minutes": 0})
        self.assertEqual((doc["menubar"]["icon"], doc["menubar"]["show"], doc["menubar"]["window"], doc["menubar"]["count"]),
                         ("none", "claude", "Week", "left"))

    def test_last_tab_and_open_on_never_name_a_hidden_or_unknown_tab(self):
        doc = panel_prefs.normalize({"hidden_tabs": ["codex"], "last_tab": "codex", "open_on": "ghost"})
        self.assertEqual(doc["last_tab"], "overview")
        self.assertEqual(doc["open_on"], "last")

    def test_budgets_are_positive_numbers_per_pool_and_nothing_else(self):
        doc = panel_prefs.normalize({"budgets": {"openrouter-free": 20, "gemini-metered": 7.5, "chatgpt-work": 9,
                                                 "x": -1, "claude": "20", "Bad Id": 5, "codex": True}})
        self.assertEqual(doc["budgets"], {"openrouter-free": 20, "gemini-metered": 7.5})   # Crossfeed Chat reports no money: no budget

    def test_the_refresh_interval_is_any_whole_number_of_minutes_up_to_a_day(self):
        for minutes, kept in ((0, 0), (1, 1), (7, 7), (1440, 1440), (1441, 5), (-1, 5), (2.5, 5), ("5", 5), (True, 5)):
            self.assertEqual(panel_prefs.normalize({"refresh": {"every_minutes": minutes}})["refresh"]["every_minutes"], kept, minutes)

    def test_tile_options_keep_only_a_known_metric_and_range(self):
        doc = panel_prefs.normalize({"tile_options": {
            "claude/tokens": {"metric": "cost", "days": 30}, "claude/links": {"metric": "bogus", "days": 9},
            "codex/tokens": {"days": 7}, "nowhere": {"metric": "cost"}, "plugin/x": "no"}})
        self.assertEqual(doc["tile_options"], {"claude/tokens": {"metric": "cost", "days": 30}, "codex/tokens": {"days": 7}})

    def test_apply_change_merges_budgets_and_options_and_sets_the_last_tab_alone(self):
        start = panel_prefs.normalize({"budgets": {"openrouter-free": 20}, "show": "left"})
        moved = panel_prefs.apply_change(start, {"budgets": {"gemini-metered": 5}, "last_tab": "claude", "appearance": "light"})
        self.assertEqual(moved["budgets"], {"openrouter-free": 20, "gemini-metered": 5})
        self.assertEqual((moved["last_tab"], moved["appearance"], moved["show"]), ("claude", "light", "left"))


class Writing(unittest.TestCase):
    def test_save_is_atomic_mode_600_and_reads_back_the_same_document(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "panel.json"
            written = panel_prefs.save({"show": "left", "tiles": {"codex": ["links", "limits"]}}, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), written)
            self.assertEqual(panel_prefs.load(path)["tiles"]["codex"], ["status", "links", "limits"])
            # A second write leaves no temporary file behind and replaces, never appends.
            panel_prefs.save({"show": "used"}, path)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["panel.json"])
            self.assertEqual(json.loads(path.read_text())["show"], "used")

    def test_apply_change_moves_one_thing_and_leaves_the_rest_alone(self):
        start = panel_prefs.normalize({"show": "used", "tiles": {"claude": ["limits", "links"]}})
        moved = panel_prefs.apply_change(start, {"tiles": {"claude": ["links", "limits"]}})
        self.assertEqual(moved["tiles"]["claude"], ["status", "links", "limits"])
        self.assertEqual(moved["tiles"]["codex"], start["tiles"]["codex"])
        self.assertEqual(moved["show"], "used")

    def test_the_environment_names_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "custom.json"
            old = os.environ.get("FLEET_PANEL_PREFS")
            os.environ["FLEET_PANEL_PREFS"] = str(target)
            try:
                self.assertEqual(panel_prefs.path(), target)
            finally:
                if old is None:
                    os.environ.pop("FLEET_PANEL_PREFS", None)
                else:
                    os.environ["FLEET_PANEL_PREFS"] = old


class FleetctlPanel(unittest.TestCase):
    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "fleetctl.py"), *args],
                              capture_output=True, text=True, timeout=120, env=env)

    def test_panel_get_prints_defaults_and_set_writes_what_it_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, "FLEET_PANEL_PREFS": str(Path(folder) / "panel.json"),
                   "FLEET_STATE_DIR": str(Path(folder) / "state")}
            got = self.run_cli("panel", "get", env=env)
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(json.loads(got.stdout)["show"], "used")
            document = {"show": "left", "hidden_tiles": {"claude": ["credits"]}}
            wrote = self.run_cli("panel", "set", "--json", json.dumps(document), env=env)
            self.assertEqual(wrote.returncode, 0, wrote.stderr)
            self.assertEqual(json.loads(wrote.stdout)["show"], "left")
            self.assertEqual(stat.S_IMODE((Path(folder) / "panel.json").stat().st_mode), 0o600)
            self.assertEqual(json.loads(self.run_cli("panel", "get", env=env).stdout)["hidden_tiles"]["claude"], ["credits"])

    def test_panel_set_refuses_a_document_that_is_not_a_document(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, "FLEET_PANEL_PREFS": str(Path(folder) / "panel.json")}
            for bad in ('["a"]', "not json", '"x"'):
                result = self.run_cli("panel", "set", "--json", bad, env=env)
                self.assertNotEqual(result.returncode, 0, bad)
            self.assertFalse((Path(folder) / "panel.json").exists())


if __name__ == "__main__":
    unittest.main()
