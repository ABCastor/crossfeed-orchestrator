"""BR-PLUGIN-001..003: the three promises a plugin tile makes to the operator.

A plugin is arbitrary code a stranger wrote. These are the rules that keep it from becoming a
credential leak, a self-installing update channel, or a way to break the menu.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_fleetctl import fleetctl

plugins = fleetctl.plugin_tiles
ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "plugins" / "hello-tile"


class PluginBusiness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.plugins_root = self.root / "plugins"
        self.registry_file = self.root / "plugins.json"
        env = {"FLEET_PLUGINS_DIR": str(self.plugins_root), "FLEET_PLUGINS_FILE": str(self.registry_file)}
        old = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
                                 for key, value in old.items()])

    def write_plugin(self, identifier="demo", script="print('{\"rows\": []}')", tile=None):
        folder = self.plugins_root / identifier
        folder.mkdir(parents=True)
        tile = {"id": "one", "title": "One", "command": ["python3", "tile.py"], **(tile or {})}
        (folder / "crossfeed-plugin.json").write_text(json.dumps(
            {"schema": 1, "id": identifier, "name": identifier.title(), "version": "0.1.0", "tiles": [tile]}))
        (folder / "tile.py").write_text(script)
        registry = plugins.load_registry(self.registry_file)
        registry[identifier] = {"source": str(folder), "commit": "", "enabled": False}
        plugins.save_registry(registry, self.registry_file)
        return folder

    # BR-PLUGIN-001 -----------------------------------------------------------------------------
    def test_a_plugin_never_sees_a_provider_credential_or_a_fleet_variable(self):
        """A plugin's command runs with an environment built from nothing, not filtered from ours."""
        folder = self.write_plugin(script="""
import json, os
print(json.dumps({"rows": [{"kind": "text", "text": ",".join(sorted(os.environ))}]}))
""")
        secrets = {"ANTHROPIC_API_KEY": "sk-ant-placeholder", "OPENAI_API_KEY": "sk-openai-placeholder",
                   "GH_TOKEN": "ghp-placeholder", "FLEET_STATE_DIR": "/tmp/x", "FLEET_PANEL_PREFS": "/tmp/y",
                   "AWS_SECRET_ACCESS_KEY": "placeholder"}
        os.environ.update(secrets)
        self.addCleanup(lambda: [os.environ.pop(key, None) for key in secrets])
        tile = plugins.read_manifest(folder)["tiles"][0]
        result = plugins.run_tile("demo", folder, tile, self.state, use_cache=False)
        # macOS adds its own text-encoding hint to every process it starts; it is not ours and holds nothing.
        seen = set(result["doc"]["rows"][0]["text"].split(",")) - {"__CF_USER_TEXT_ENCODING"}
        # Control: the child really does get an environment, so an empty set cannot pass this test.
        self.assertEqual(seen, {"CROSSFEED_PLUGIN_STATE", "HOME", "LANG", "PATH"})
        for name in list(secrets) + ["FLEET_PLUGINS_DIR", "FLEET_PLUGINS_FILE"]:
            self.assertNotIn(name, seen)
            self.assertFalse(any(name.endswith(suffix) for suffix in ("_TOKEN", "_KEY")) and name in seen)

    # BR-PLUGIN-002 -----------------------------------------------------------------------------
    def test_nothing_installs_updates_or_enables_without_the_operators_yes(self):
        """`add` and `update` print what they would do; only `--yes` changes the disk; glance runs enabled tiles only."""
        plan = plugins.install(str(EXAMPLE), apply=False, root=self.plugins_root, registry_file=self.registry_file)
        self.assertFalse(plan["installed"])
        self.assertFalse((self.plugins_root / "hello-tile").exists())
        self.assertEqual(plugins.load_registry(self.registry_file), {})
        # With nothing installed, the glance has no plugin tile at all.
        self.assertEqual(plugins.glance(self.state, now=time.time()), [])
        # Control: the same call with the operator's yes installs and enables it.
        installed = plugins.install(str(EXAMPLE), apply=True, root=self.plugins_root, registry_file=self.registry_file)
        self.assertTrue(installed["enabled"])
        self.assertTrue((self.plugins_root / "hello-tile").is_dir())
        self.assertEqual(len(plugins.glance(self.state, now=time.time())), 2)  # its two tiles
        # Switching it off again stops the tiles from being read, and nothing switches it back on.
        plugins.set_enabled("hello-tile", False, registry_file=self.registry_file, root=self.plugins_root)
        self.assertEqual(plugins.glance(self.state, now=time.time() + 100000), [])

    @unittest.skipUnless(shutil.which("git"), "git is needed to exercise a pinned update")
    def test_update_shows_the_new_commit_before_it_moves_and_nothing_updates_itself(self):
        origin = self.root / "origin"
        origin.mkdir()
        (origin / "crossfeed-plugin.json").write_text(json.dumps(
            {"schema": 1, "id": "gitp", "name": "Gitp", "version": "0.1.0",
             "tiles": [{"id": "one", "command": ["python3", "tile.py"]}]}))
        (origin / "tile.py").write_text("print('{\"rows\": []}')")
        git = lambda *args: subprocess.run(["git", "-C", str(origin), *args], check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "test")
        git("add", ".")
        git("commit", "-qm", "first")
        clone = self.plugins_root / "gitp"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
        plugins.save_registry({"gitp": {"source": str(origin), "commit": "", "enabled": True}}, self.registry_file)
        head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

        (origin / "tile.py").write_text("print('{\"rows\": [{\"kind\": \"text\", \"text\": \"new\"}]}')")
        git("add", ".")
        git("commit", "-qm", "second")
        # Nothing fetches on a timer: until `update` is asked, the installed commit is the old one.
        self.assertEqual(subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                                        capture_output=True, text=True).stdout.strip(), head)
        report = plugins.update("gitp", apply=False, root=self.plugins_root, registry_file=self.registry_file)
        self.assertFalse(report["updated"])
        self.assertNotEqual(report["from"], report["to"])
        self.assertEqual(subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                                        capture_output=True, text=True).stdout.strip(), head)
        self.assertTrue(plugins.update("gitp", apply=True, root=self.plugins_root,
                                       registry_file=self.registry_file)["updated"])

    # BR-PLUGIN-003 -----------------------------------------------------------------------------
    def test_invalid_output_is_one_error_tile_and_never_crashes_the_glance(self):
        """A stranger's garbage becomes an error tile beside the tiles that still work."""
        bad = self.write_plugin(identifier="bad", script="print('this is not a tile')")
        good = self.write_plugin(identifier="good")
        plugins.set_enabled("bad", True, registry_file=self.registry_file, root=self.plugins_root)
        plugins.set_enabled("good", True, registry_file=self.registry_file, root=self.plugins_root)
        rows = {row["tile"]: row for row in plugins.glance(self.state, now=time.time())}
        self.assertEqual(rows["bad/one"]["doc"]["status"], "error")
        self.assertIn("Plugin output invalid", rows["bad/one"]["doc"]["rows"][0]["text"])
        self.assertEqual(rows["good/one"]["doc"]["status"], "ok")  # control: the same glance rendered it
        self.assertIsNone(rows["good/one"]["error"])
        # A tile document over the limits is the same story, not a half-drawn tile.
        (bad / "tile.py").write_text("import json\nprint(json.dumps({'rows': [{'kind': 'text', 'text': 'x'}] * 9}))")
        plugins.run_tile("bad", bad, plugins.read_manifest(bad)["tiles"][0], self.state, use_cache=False)
        again = {row["tile"]: row for row in plugins.glance(self.state, now=time.time() + 100000)}
        self.assertEqual(again["bad/one"]["doc"]["rows"][0]["text"].count("at most 8 rows"), 1)

    def test_a_plugin_that_would_outlive_the_glance_budget_is_reported_not_waited_for(self):
        folder = self.write_plugin(script="import time\ntime.sleep(10)\nprint('{}')", tile={"timeout_seconds": 30})
        plugins.set_enabled("demo", True, registry_file=self.registry_file, root=self.plugins_root)
        started = time.monotonic()
        rows = plugins.glance(self.state, now=time.time(), budget_s=0.2)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual([row["error"] for row in rows], ["Plugin timed out"])
        self.assertFalse((folder / "tile.py").read_text().startswith("import time\nprint"))  # the plugin is untouched


if __name__ == "__main__":
    unittest.main()
