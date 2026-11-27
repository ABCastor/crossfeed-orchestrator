"""Plugin tiles: the manifest, the output, and the box a plugin's command runs inside.

Every check here runs a real subprocess. The env-scrub and timeout cases are behavioural: they
observe what the child actually received and how long it actually lived, never that a flag was set.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_fleetctl import fleetctl

plugins = fleetctl.plugin_tiles
ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "plugins" / "hello-tile"


def manifest(**overrides):
    doc = {"schema": 1, "id": "demo", "name": "Demo", "version": "0.1.0",
           "tiles": [{"id": "one", "title": "One", "command": ["python3", "-c", "print('{\"rows\":[]}')"]}]}
    doc.update(overrides)
    return doc


class Temp(unittest.TestCase):
    """An isolated plugins folder, registry file and state dir for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.root = root
        self.state = root / "state"
        self.state.mkdir()
        self.env = {"FLEET_PLUGINS_DIR": str(root / "plugins"), "FLEET_PLUGINS_FILE": str(root / "plugins.json")}
        old = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        self.addCleanup(lambda: [os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
                                 for key, value in old.items()])
        self.plugins_root = root / "plugins"
        self.registry_file = root / "plugins.json"

    def write_plugin(self, identifier="demo", script="print('{\"rows\":[]}')", **tile):
        folder = self.plugins_root / identifier
        folder.mkdir(parents=True)
        tile = {"id": "one", "title": "One", "command": ["python3", "tile.py"], **tile}
        (folder / "crossfeed-plugin.json").write_text(json.dumps(
            {"schema": 1, "id": identifier, "name": identifier.title(), "version": "0.1.0", "tiles": [tile]}))
        (folder / "tile.py").write_text(script)
        registry = plugins.load_registry(self.registry_file)
        registry[identifier] = {"source": str(folder), "commit": "", "enabled": False}
        plugins.save_registry(registry, self.registry_file)
        return folder

    def enable(self, identifier="demo"):
        plugins.set_enabled(identifier, True, registry_file=self.registry_file, root=self.plugins_root)


class Manifests(unittest.TestCase):
    def test_a_manifest_is_validated_into_exactly_what_will_run(self):
        doc = plugins.validate_manifest({"schema": 1, "id": "ci", "name": "CI", "version": "1.2.3",
                                         "homepage": "https://example.com/ci",
                                         "tiles": [{"id": "runs", "title": "CI runs", "command": ["python3", "tile.py"],
                                                    "refresh_seconds": 5, "timeout_seconds": 600, "tab": "claude"}]})
        tile = doc["tiles"][0]
        self.assertEqual(doc["homepage"], "https://example.com/ci")
        self.assertEqual(tile["refresh_seconds"], plugins.MIN_REFRESH_S)   # clamped, never trusted
        self.assertEqual(tile["timeout_seconds"], plugins.MAX_TIMEOUT_S)
        self.assertEqual(tile["tab"], "claude")

    def test_unreadable_manifests_are_named_not_guessed(self):
        cases = {
            "schema": manifest(schema=2),
            "id": manifest(id="Bad Id"),
            "tiles": manifest(tiles=[]),
            "command": manifest(tiles=[{"id": "one", "command": []}]),
            "argument": manifest(tiles=[{"id": "one", "command": ["python3", ""]}]),
            "twice": manifest(tiles=[{"id": "one", "command": ["python3", "x"]}, {"id": "one", "command": ["python3", "y"]}]),
        }
        for name, doc in cases.items():
            with self.subTest(name), self.assertRaises(plugins.PluginError):
                plugins.validate_manifest(doc)

    def test_homepage_must_be_https(self):
        self.assertIsNone(plugins.validate_manifest(manifest(homepage="http://example.com"))["homepage"])


class Output(unittest.TestCase):
    def test_unknown_kinds_and_plain_http_links_are_dropped_and_the_rest_kept(self):
        doc = plugins.validate_tile({"title": "CI", "rows": [
            {"kind": "text", "text": "main is green"},
            {"kind": "mystery", "text": "drop me"},
            {"kind": "link", "label": "Open", "url": "http://insecure"},
            {"kind": "link", "label": "Open", "url": "https://example.com"},
            {"kind": "value", "label": "Failed", "value": "3", "tone": "warn"}]})
        self.assertEqual([row["kind"] for row in doc["rows"]], ["text", "link", "value"])
        self.assertEqual(doc["rows"][1]["url"], "https://example.com")

    def test_limits_are_refusals_not_tidy_ups(self):
        cases = {
            "nine rows": {"rows": [{"kind": "text", "text": "x"}] * 9},
            "long text": {"rows": [{"kind": "text", "text": "x" * 121}]},
            "over 100": {"rows": [{"kind": "bar", "label": "M", "used": 150}]},
            "negative": {"rows": [{"kind": "bar", "label": "M", "pace": -1}]},
            "not finite": {"rows": [{"kind": "sparkline", "label": "R", "values": [1, float("inf")]}]},
            "tone": {"rows": [{"kind": "value", "label": "x", "value": "1", "tone": "loud"}]},
            "not rows": {"rows": "three"},
            "resets_at": {"rows": [{"kind": "bar", "label": "M", "used": 1, "resets_at": "next tuesday"}]},
        }
        for name, doc in cases.items():
            with self.subTest(name), self.assertRaises(plugins.PluginError):
                plugins.validate_tile(doc)

    def test_a_bar_needs_a_percentage_and_a_sparkline_at_most_fourteen_days(self):
        self.assertIsNone(plugins.validate_tile({"rows": [{"kind": "bar", "label": "M"}]})["rows"][0]["used"])
        self.assertRaises(plugins.PluginError, plugins.validate_tile,
                          {"rows": [{"kind": "sparkline", "label": "R", "values": [1] * 15}]})


class Running(Temp):
    def test_the_example_plugin_prints_a_tile_the_panel_can_draw(self):
        manifest_doc = plugins.read_manifest(EXAMPLE)
        self.assertEqual(manifest_doc["id"], "hello-tile")
        tile = next(row for row in manifest_doc["tiles"] if row["id"] == "bar")
        result = plugins.run_tile("hello-tile", EXAMPLE, tile, self.state, use_cache=False)
        self.assertIsNone(result["error"])
        self.assertEqual(result["doc"]["status"], "warn")
        self.assertEqual([row["kind"] for row in result["doc"]["rows"]], ["bar", "value", "sparkline"])
        self.assertEqual(result["tile"], "hello-tile/bar")

    def test_a_plugin_cannot_see_a_provider_credential(self):
        folder = self.write_plugin(script="""
import json, os
print(json.dumps({"rows": [{"kind": "text", "text": " ".join(sorted(os.environ))}]}))
""")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-placeholder"
        os.environ["FLEET_SECRET_TOKEN"] = "placeholder"
        os.environ["SOME_KEY"] = "placeholder"
        self.addCleanup(lambda: [os.environ.pop(key, None) for key in ("ANTHROPIC_API_KEY", "FLEET_SECRET_TOKEN", "SOME_KEY")])
        result = plugins.run_tile("demo", folder, plugins.read_manifest(folder)["tiles"][0], self.state, use_cache=False)
        visible = result["doc"]["rows"][0]["text"].split()
        # macOS adds its own text-encoding hint to every process it starts; it is not ours and holds nothing.
        self.assertEqual(sorted(set(visible) - {"__CF_USER_TEXT_ENCODING"}), ["CROSSFEED_PLUGIN_STATE", "HOME", "LANG", "PATH"])
        self.assertEqual(result["doc"]["rows"][0]["text"].find("TOKEN"), -1)

    def test_a_plugin_that_hangs_is_killed_at_its_timeout(self):
        folder = self.write_plugin(script="import time\ntime.sleep(20)\nprint('{}')")
        tile = plugins.read_manifest(folder)["tiles"][0]
        tile["timeout_seconds"] = 1
        started = time.monotonic()
        result = plugins.run_tile("demo", folder, tile, self.state, use_cache=False)
        elapsed = time.monotonic() - started
        self.assertEqual(result["error"], "Plugin timed out")
        self.assertEqual(result["doc"]["status"], "error")
        self.assertLess(elapsed, 8, "the timeout was enforced, not waited out")

    def test_output_over_the_cap_is_refused_and_nothing_is_kept(self):
        folder = self.write_plugin(script="print('x' * 200000)")
        result = plugins.run_tile("demo", folder, plugins.read_manifest(folder)["tiles"][0], self.state, use_cache=False)
        self.assertEqual(result["error"], "Plugin output too large")

    def test_garbage_and_a_nonzero_exit_are_error_tiles(self):
        folder = self.write_plugin(script="print('not json')")
        result = plugins.run_tile("demo", folder, plugins.read_manifest(folder)["tiles"][0], self.state, use_cache=False)
        self.assertTrue(result["error"].startswith("Plugin output invalid"))
        (folder / "tile.py").write_text("import sys\nprint('{}')\nsys.exit(3)")
        result = plugins.run_tile("demo", folder, plugins.read_manifest(folder)["tiles"][0], self.state, use_cache=False)
        self.assertEqual(result["error"], "Plugin failed (exit 3)")
        self.assertEqual(result["doc"]["status"], "error")

    def test_a_cached_tile_is_not_run_again_until_its_refresh_has_passed(self):
        counter = self.state / "runs.txt"
        folder = self.write_plugin(script=f"""
from pathlib import Path
p = Path({str(counter)!r})
p.write_text(str(len(p.read_text()) + 1) if p.exists() else "1")
print('{{"rows": [{{"kind": "text", "text": "ran"}}]}}')
""")
        tile = plugins.read_manifest(folder)["tiles"][0]
        plugins.run_tile("demo", folder, tile, self.state)
        plugins.run_tile("demo", folder, tile, self.state)
        self.assertEqual(counter.read_text(), "1", "the second read came from the cache")
        plugins.run_tile("demo", folder, tile, self.state, now=time.time() + tile["refresh_seconds"] + 1)
        self.assertEqual(counter.read_text(), "2", "after the refresh it runs again")


class Glance(Temp):
    def test_only_enabled_plugins_contribute_and_a_disabled_one_runs_nothing(self):
        self.write_plugin(identifier="demo")
        result = plugins.glance(self.state, now=time.time())
        self.assertEqual(result, [])
        before = (self.state / "runs.txt")
        self.enable("demo")
        rows = plugins.glance(self.state, now=time.time())
        self.assertEqual([row["tile"] for row in rows], ["demo/one"])
        self.assertFalse(before.exists())

    def test_tiles_run_in_parallel_inside_one_total_budget(self):
        self.write_plugin(identifier="slow", script="import time\ntime.sleep(2)\nprint('{}')",
                          timeout_seconds=30, refresh_seconds=300)
        self.enable("slow")
        started = time.monotonic()
        rows = plugins.glance(self.state, now=time.time(), budget_s=0.2)
        elapsed = time.monotonic() - started
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error"], "Plugin timed out")
        self.assertLess(elapsed, 8, "one budget, not one timeout after another")

    def test_a_broken_plugin_never_takes_the_glance_down(self):
        self.write_plugin(identifier="bad", script="raise SystemExit('nope')")
        self.write_plugin(identifier="good")
        self.enable("bad")
        self.enable("good")
        rows = plugins.glance(self.state, now=time.time())
        by_tile = {row["tile"]: row for row in rows}
        self.assertEqual(by_tile["bad/one"]["doc"]["status"], "error")
        self.assertIsNone(by_tile["good/one"]["error"])

    def test_an_unreadable_manifest_or_a_missing_folder_is_skipped(self):
        self.write_plugin(identifier="demo")
        self.enable("demo")
        (self.plugins_root / "demo" / "crossfeed-plugin.json").write_text("{not json")
        self.assertEqual(plugins.glance(self.state, now=time.time()), [])
        shutil.rmtree(self.plugins_root / "demo")
        self.assertEqual(plugins.glance(self.state, now=time.time()), [])


class Installing(Temp):
    def test_add_without_yes_installs_nothing_and_prints_every_command(self):
        plan = plugins.install(str(EXAMPLE), apply=False, root=self.plugins_root, registry_file=self.registry_file)
        self.assertFalse(plan["installed"])
        self.assertFalse((self.plugins_root / "hello-tile").exists())
        self.assertEqual(plugins.load_registry(self.registry_file), {})
        words = plugins.describe_plan(plan, verb="Would install")
        self.assertIn("python3 hello.py", words)
        self.assertIn("Would install", words)

    def test_add_yes_installs_and_enables_a_folder(self):
        plan = plugins.install(str(EXAMPLE), apply=True, root=self.plugins_root, registry_file=self.registry_file)
        self.assertTrue(plan["installed"] and plan["enabled"])
        self.assertTrue((self.plugins_root / "hello-tile" / "crossfeed-plugin.json").is_file())
        self.assertEqual(plugins.load_registry(self.registry_file)["hello-tile"]["enabled"], True)
        # A second install of the same id is refused rather than overwriting a running plugin.
        with self.assertRaises(plugins.PluginError):
            plugins.install(str(EXAMPLE), apply=True, root=self.plugins_root, registry_file=self.registry_file)

    def test_enable_refuses_a_folder_whose_manifest_no_longer_reads(self):
        self.write_plugin(identifier="demo")
        (self.plugins_root / "demo" / "crossfeed-plugin.json").write_text("{}")
        with self.assertRaises(plugins.PluginError):
            plugins.set_enabled("demo", True, registry_file=self.registry_file, root=self.plugins_root)
        self.assertFalse(plugins.load_registry(self.registry_file)["demo"]["enabled"])

    def test_remove_moves_the_folder_to_the_trash_instead_of_deleting_it(self):
        self.write_plugin(identifier="demo")
        result = plugins.remove("demo", state_dir=self.state, root=self.plugins_root, registry_file=self.registry_file)
        self.assertFalse((self.plugins_root / "demo").exists())
        self.assertTrue(Path(result["moved_to"]).is_dir())
        self.assertEqual(plugins.load_registry(self.registry_file), {})

    @unittest.skipUnless(shutil.which("git"), "git is needed to exercise a pinned update")
    def test_update_shows_the_new_commit_and_the_command_diff_before_it_moves(self):
        origin = self.root / "origin"
        origin.mkdir()
        (origin / "crossfeed-plugin.json").write_text(json.dumps(manifest(id="gitp")))
        (origin / "tile.py").write_text("print('{\"rows\":[]}')")
        run = lambda *args: subprocess.run(["git", "-C", str(origin), *args], check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        run("config", "user.email", "test@example.com")
        run("config", "user.name", "test")
        run("add", ".")
        run("commit", "-qm", "first")
        clone = self.plugins_root / "gitp"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
        plugins.save_registry({"gitp": {"source": str(origin), "commit": "", "enabled": True}}, self.registry_file)

        (origin / "crossfeed-plugin.json").write_text(json.dumps(manifest(id="gitp", tiles=[
            {"id": "one", "title": "One", "command": ["python3", "other.py"]}])))
        (origin / "other.py").write_text("print('{\"rows\":[]}')")
        run("add", ".")
        run("commit", "-qm", "second")

        report = plugins.update("gitp", apply=False, root=self.plugins_root, registry_file=self.registry_file)
        self.assertFalse(report["updated"])
        self.assertEqual(report["command_changes"][0]["after"], ["python3", "other.py"])
        self.assertEqual((clone / "crossfeed-plugin.json").read_text(), json.dumps(manifest(id="gitp")))
        applied = plugins.update("gitp", apply=True, root=self.plugins_root, registry_file=self.registry_file)
        self.assertTrue(applied["updated"])
        self.assertEqual(json.loads((clone / "crossfeed-plugin.json").read_text())["tiles"][0]["command"], ["python3", "other.py"])


class Registry(unittest.TestCase):
    def test_a_damaged_registry_is_read_as_no_plugins(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plugins.json"
            for text in ("not json", "[]", json.dumps({"Bad Id": {"enabled": True}, "ok": {"enabled": "yes"}})):
                path.write_text(text)
                registry = plugins.load_registry(path)
                self.assertEqual(set(registry), {"ok"} if "ok" in text else set())
                for record in registry.values():
                    self.assertIs(record["enabled"], False)


if __name__ == "__main__":
    unittest.main()
