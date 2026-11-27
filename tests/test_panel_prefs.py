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
        self.assertEqual(doc["tiles"]["claude"], ["limits", "resets", "credits", "tokens", "policy", "links"])
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
        self.assertEqual(doc["tiles"]["claude"], ["limits", "plugin:ci/runs"])

    def test_hidden_tiles_keep_the_tabs_own_tiles_and_visible_tiles_reads_both_lists(self):
        doc = panel_prefs.normalize({"tiles": {"claude": ["limits", "resets", "links"]},
                                     "hidden_tiles": {"claude": ["resets", "not-a-tile"]}})
        self.assertEqual(doc["hidden_tiles"]["claude"], ["resets"])
        self.assertEqual(panel_prefs.visible_tiles(doc, "claude"), ["limits", "links"])

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


class Writing(unittest.TestCase):
    def test_save_is_atomic_mode_600_and_reads_back_the_same_document(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "panel.json"
            written = panel_prefs.save({"show": "left", "tiles": {"codex": ["links", "limits"]}}, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), written)
            self.assertEqual(panel_prefs.load(path)["tiles"]["codex"], ["links", "limits"])
            # A second write leaves no temporary file behind and replaces, never appends.
            panel_prefs.save({"show": "used"}, path)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["panel.json"])
            self.assertEqual(json.loads(path.read_text())["show"], "used")

    def test_apply_change_moves_one_thing_and_leaves_the_rest_alone(self):
        start = panel_prefs.normalize({"show": "used", "tiles": {"claude": ["limits", "links"]}})
        moved = panel_prefs.apply_change(start, {"tiles": {"claude": ["links", "limits"]}})
        self.assertEqual(moved["tiles"]["claude"], ["links", "limits"])
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
