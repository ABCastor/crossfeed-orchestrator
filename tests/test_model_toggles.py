"""Switching models on and off: the engine, the wrappers, the console route and the page.

The requirement: a provider's models are not a single-choice picker; any number of them can be switched on
and off. Every model a provider can run has an on/off switch, and what the switches add up to is the same
everywhere:

    several on : Crossfeed picks the best one that is on for each task
    one on     : every run on that provider uses it
    none on    : the provider is not used

These tests hold each link: the router, a named lease, the direct wrappers, the older single choice
(which must keep meaning "only that one on"), the console route, the words on the page and the brief.
"""

import http.client
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from tests.test_console import ROOT, base_overlay, console

fleetctl = console.fleetctl
SCRIPTS = ROOT / "scripts"


def lane(lane_id, model, pool, modes=("read-only", "write")):
    return {"lane_id": lane_id, "model_key": model, "quota_pool": pool, "harness": "opencode",
            "provider": pool, "selector": f"{pool}/{model}", "access_status": "verified",
            "admission_status": "active", "allowed_modes": list(modes), "roles": [],
            "capabilities": {"input": ["text"]}, "max_parallel": 4}


def routed_roster():
    """Two pools a role can use, one with three models (one read-only), one with a single model."""
    return {
        "schema_version": 3,
        "quota_pools": {"go": {"label": "Go", "plan": {"name": "Go"}}, "other": {"label": "Other", "plan": {"name": "Other"}},
                        "codex": {"label": "Codex", "plan": {"name": "Pro"}}},
        "lanes": [lane("go-flash", "flash", "go"), lane("go-big", "big", "go"),
                  lane("go-reader", "reader", "go", modes=("read-only",)), lane("other-one", "one", "other")],
        "catalogue_only": [],
        "model_evidence": {"gpt-9-sun": {"status": "provisional"}, "gpt-8-old-and-older": {"status": "retired-from-routing"}},
        "model_cards": {"gpt-9-moon": {"pool": "codex", "name": "GPT-9 Moon", "status": "current", "best_for": "Cheap"},
                        "gpt-7-gone": {"pool": "codex", "status": "gone"},
                        "gpt-8-old-and-older": {"pool": "codex", "hidden": True}},
        "routing": {"roles": {"default": {"quality_first": ["go-flash", "other-one", "go-big"]},
                              "review": {"quality_first": ["other-one", "go-flash"]}}},
    }


def claude_roster():
    """A direct pool the way the live roster shapes Claude: four current models, two older, aliases."""
    def card(name, order, status="current", **more):
        return dict({"pool": "claude", "name": name, "status": status, "order": order}, **more)
    return {
        "schema_version": 3,
        "quota_pools": {"claude": {"label": "Claude", "plan": {"name": "Max"}}},
        "lanes": [], "catalogue_only": [], "routing": {"roles": {}},
        "model_cards": {"claude-fable": card("Fable", 1, aliases=["fable"]), "claude-opus": card("Opus", 2, aliases=["opus"]),
                        "claude-sonnet": card("Sonnet", 3, aliases=["sonnet"]), "claude-haiku": card("Haiku", 4, run_as="haiku", aliases=["haiku"]),
                        "claude-old": card("Old", 5, status="older", older_model_reasons={"claude": {
                            "job": "fixture read", "advantage": "faster", "compared_to": "claude-haiku",
                            "reason": "synthetic fixture comparison", "evidence": "test-only"}})},
    }


def off(runtime, roster, pool, *models):
    for model in models:
        fleetctl.set_model_toggle(runtime, roster, pool, model, False)


class RouterToggleTests(unittest.TestCase):
    def setUp(self):
        self.roster = routed_roster()
        self.runtime = {}

    def choose(self, role="default", mode="write"):
        return fleetctl.choose_lane(self.roster, self.runtime, role, mode, "text")["lane_id"]

    def test_nothing_off_changes_nothing(self):
        self.assertEqual(self.choose(), "go-flash")
        self.assertEqual(fleetctl.pool_switches(self.roster, self.runtime, "go"), {"flash": True, "big": True, "reader": True})
        with tempfile.TemporaryDirectory() as directory:
            brief = fleetctl.render_brief(fleetctl.fleet_overview(self.roster, self.runtime, Path(directory)), verbose=True)
        self.assertNotIn("Models:", brief)

    def test_several_on_the_router_picks_the_best_one_that_is_on(self):
        fleetctl.set_model_toggle(self.runtime, self.roster, "go", "flash", False)
        self.assertEqual(self.choose(), "other-one")          # the job's next favourite
        fleetctl.set_pool_level(self.runtime, "other", "off")
        self.assertEqual(self.choose(), "go-big")             # flash is off, other is off: big is what is left

    def test_one_on_keeps_its_own_rank_when_the_job_lists_it(self):
        fleetctl.set_model_toggle(self.runtime, self.roster, "go", "flash", False)
        fleetctl.set_model_toggle(self.runtime, self.roster, "go", "reader", False)
        self.assertEqual(self.choose(), "other-one")           # big is listed third, so it stays third
        fleetctl.set_pool_level(self.runtime, "other", "off")
        self.assertEqual(self.choose(), "go-big")

    def test_one_on_stands_in_when_the_job_ranks_only_models_that_are_off(self):
        for model in ("flash", "reader"):
            fleetctl.set_model_toggle(self.runtime, self.roster, "go", model, False)
        # review ranks go's flash only. Flash is off, so big, the one model left on, stands in at flash's slot
        self.assertEqual(self.choose("review"), "other-one")
        fleetctl.set_pool_level(self.runtime, "other", "off")
        self.assertEqual(self.choose("review"), "go-big")

    def test_none_on_leaves_the_provider_out_and_the_others_run(self):
        for model in ("flash", "big", "reader"):
            fleetctl.set_model_toggle(self.runtime, self.roster, "go", model, False)
        self.assertEqual(fleetctl.models_state(fleetctl.pool_switches(self.roster, self.runtime, "go")), "none")
        self.assertEqual(self.choose(), "other-one")
        self.assertEqual(self.choose("review"), "other-one")
        fleetctl.set_pool_level(self.runtime, "other", "off")
        with self.assertRaisesRegex(fleetctl.FleetError, "go-flash: model switched off"):
            self.choose()

    def test_gates_still_judge_the_models_that_are_on(self):
        for model in ("flash", "big"):
            fleetctl.set_model_toggle(self.runtime, self.roster, "go", model, False)
        # reader, the one model left on, stands in for go's first slot but is read-only: a read task can
        # run it, a write task cannot, and no other go model stands in
        self.assertEqual(self.choose(mode="read-only"), "go-reader")
        self.assertEqual(self.choose(mode="write"), "other-one")
        fleetctl.set_pool_level(self.runtime, "other", "off")
        with self.assertRaisesRegex(fleetctl.FleetError, "go-reader: mode write"):
            self.choose(mode="write")

    def test_a_named_lane_of_a_model_that_is_off_is_refused_at_acquire(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with fleetctl.locked_runtime(state) as runtime:
                fleetctl.set_model_toggle(runtime, self.roster, "go", "flash", False)
            with self.assertRaisesRegex(fleetctl.FleetError, "model flash is switched off in the .* console"):
                fleetctl.acquire_lease(state, self.roster, "go-flash", 60)
            fleetctl.release_lease(state, fleetctl.acquire_lease(state, self.roster, "go-big", 60))
            fleetctl.acquire_lease(state, self.roster, "other-one", 60)   # other pools untouched

    def test_the_switches_are_kept_per_provider_and_an_empty_list_is_not_kept(self):
        fleetctl.set_model_toggle(self.runtime, self.roster, "go", "flash", False)
        fleetctl.set_model_toggle(self.runtime, self.roster, "go", "big", False)
        self.assertEqual(self.runtime["model_toggles"], {"go": ["big", "flash"]})
        fleetctl.set_model_toggle(self.runtime, self.roster, "go", "big", True)
        fleetctl.set_model_toggle(self.runtime, self.roster, "go", "flash", True)
        self.assertEqual(self.runtime["model_toggles"], {})

    def test_the_same_model_funded_by_two_providers_is_switched_on_each_alone(self):
        self.roster["lanes"].append(lane("other-flash", "flash", "other"))
        self.roster["routing"]["roles"]["default"]["quality_first"].insert(0, "other-flash")
        self.assertEqual(self.choose(), "other-flash")
        fleetctl.set_model_toggle(self.runtime, self.roster, "other", "flash", False)
        self.assertEqual(fleetctl.pool_switches(self.roster, self.runtime, "go")["flash"], True)
        self.assertEqual(fleetctl.pool_switches(self.roster, self.runtime, "other")["flash"], False)
        self.assertEqual(self.choose(), "go-flash")            # go still runs it; other no longer does

    def test_an_older_per_model_off_is_settled_onto_every_provider_that_runs_it(self):
        self.roster["lanes"].append(lane("other-big", "big", "other"))
        runtime = {"model_preferences": {"big": "off"}}
        self.assertEqual(fleetctl.pool_switches(self.roster, runtime, "other")["big"], False)   # it still reads
        fleetctl.set_model_toggle(runtime, self.roster, "go", "flash", False)
        self.assertEqual(runtime["model_preferences"], {})
        self.assertEqual(runtime["model_toggles"], {"go": ["big", "flash"], "other": ["big"]})
        fleetctl.set_model_toggle(runtime, self.roster, "go", "big", True)     # now free of the other provider
        self.assertEqual(fleetctl.pool_switches(self.roster, runtime, "go")["big"], True)
        self.assertEqual(fleetctl.pool_switches(self.roster, runtime, "other")["big"], False)

    def test_toggle_validates(self):
        for pool, model in (("go", "one"), ("go", "nope"), ("nowhere", "big"), ("codex", "gpt-7-gone")):
            with self.assertRaises(ValueError):
                fleetctl.set_model_toggle(self.runtime, self.roster, pool, model, False)
        self.assertEqual(self.runtime, {})

    def test_older_runtime_files_still_read(self):
        runtime = {"switches": {"go": "on"}, "model_preferences": {"big": "prefer", "reader": "off"}}
        self.assertEqual(fleetctl.pool_switches(self.roster, runtime, "go"), {"flash": True, "big": True, "reader": False})
        self.assertEqual(fleetctl.choose_lane(self.roster, runtime, "default", "write", "text")["lane_id"], "go-flash")
        self.assertIsNone(fleetctl.model_choice({"model_choices": {"go": ""}}, "go"))
        self.assertIsNone(fleetctl.model_choice({"model_choices": {"go": 7}}, "go"))


class OlderSingleChoiceTests(unittest.TestCase):
    """The morning's picker stored {pool: model}. That file must keep meaning "only that model on"."""

    def setUp(self):
        self.roster = routed_roster()
        self.runtime = {"model_choices": {"go": "big"}}

    def test_a_stored_choice_reads_as_one_on(self):
        self.assertEqual(fleetctl.pool_switches(self.roster, self.runtime, "go"), {"flash": False, "big": True, "reader": False})
        self.assertEqual(fleetctl.choose_lane(self.roster, self.runtime, "default", "write", "text")["lane_id"], "other-one")
        fleetctl.set_pool_level(self.runtime, "other", "off")
        self.assertEqual(fleetctl.choose_lane(self.roster, self.runtime, "default", "write", "text")["lane_id"], "go-big")
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            fleetctl.atomic_json(state / "runtime.json", self.runtime)
            with self.assertRaisesRegex(fleetctl.FleetError, "switched off"):
                fleetctl.acquire_lease(state, self.roster, "go-flash", 60)

    def test_the_first_switch_touched_settles_it_into_switches(self):
        fleetctl.set_model_toggle(self.runtime, self.roster, "go", "reader", True)
        self.assertNotIn("go", self.runtime["model_choices"])
        self.assertEqual(fleetctl.pool_switches(self.roster, self.runtime, "go"), {"flash": False, "big": True, "reader": True})
        self.assertEqual(self.runtime["model_toggles"], {"go": ["flash"]})

    def test_a_choice_no_longer_offered_changes_nothing_and_is_said(self):
        self.roster["lanes"][1]["admission_status"] = "retired"
        self.assertIsNone(fleetctl.effective_model_choice(self.roster, self.runtime, "go"))
        self.assertEqual(fleetctl.choose_lane(self.roster, self.runtime, "default", "write", "text")["lane_id"], "go-flash")
        with tempfile.TemporaryDirectory() as directory:
            overview = fleetctl.fleet_overview(self.roster, self.runtime, Path(directory))
        go = next(p for p in overview["pools"] if p["pool"] == "go")
        self.assertEqual((go["models"]["state"], go["models"]["unavailable"]), ("all", "big"))
        self.assertIn("go chose big, no longer offered: all on", fleetctl.render_brief(overview, verbose=True))

    def test_set_model_choice_is_only_this_one_and_auto_switches_all_on(self):
        runtime = {}
        fleetctl.set_model_choice(runtime, self.roster, "go", "big")
        self.assertEqual(fleetctl.pool_switches(self.roster, runtime, "go"), {"flash": False, "big": True, "reader": False})
        for pool, model in (("go", "one"), ("go", "nope"), ("nowhere", "big"), ("codex", "gpt-7-gone")):
            with self.assertRaises(ValueError):
                fleetctl.set_model_choice(runtime, self.roster, pool, model)
        fleetctl.set_model_choice(self.runtime, self.roster, "go", "auto")
        self.assertNotIn("go", self.runtime["model_choices"])
        self.assertTrue(all(fleetctl.pool_switches(self.roster, self.runtime, "go").values()))


class DirectPoolTests(unittest.TestCase):
    def setUp(self):
        self.roster = claude_roster()
        self.runtime = {}

    def run_as(self, requested=None):
        return fleetctl.model_run_as(self.roster, self.runtime, "claude", requested)

    def test_nothing_off_the_task_keeps_its_own_model(self):
        for requested in (None, "opus", "claude-haiku", "a-model-nobody-listed"):
            self.assertIsNone(self.run_as(requested))

    def test_a_model_that_is_on_runs_as_asked_by_key_id_or_alias(self):
        off(self.runtime, self.roster, "claude", "claude-fable")
        for requested in ("opus", "claude-opus", "haiku", "claude-old"):
            self.assertIsNone(self.run_as(requested))

    def test_the_nearest_model_that_is_on_stands_in_cheaper_side_first(self):
        off(self.runtime, self.roster, "claude", "claude-opus", "claude-fable")
        self.assertEqual(self.run_as("fable"), "claude-sonnet")     # opus and fable are off; sonnet is nearest
        self.assertEqual(self.run_as(None), "claude-sonnet")        # the wrapper's own default is opus
        self.runtime = {}
        off(self.runtime, self.roster, "claude", "claude-sonnet")
        self.assertEqual(self.run_as("sonnet"), "haiku")             # opus and haiku are equally near: the cheaper one
        self.runtime = {}
        off(self.runtime, self.roster, "claude", "claude-haiku", "claude-old")
        self.assertEqual(self.run_as("haiku"), "claude-sonnet")     # nothing cheaper is left: the next one up

    def test_a_name_the_roster_does_not_list_stands_in_for_the_default(self):
        off(self.runtime, self.roster, "claude", "claude-opus")
        self.assertEqual(self.run_as("a-model-nobody-listed"), "claude-sonnet")

    def test_none_on_refuses(self):
        off(self.runtime, self.roster, "claude", *self.roster["model_cards"])
        with self.assertRaisesRegex(fleetctl.NoModelOn, "no model is switched on for claude"):
            self.run_as("opus")

    def test_an_older_model_runs_when_named_but_never_stands_in(self):
        current = ("claude-fable", "claude-opus", "claude-sonnet", "claude-haiku")
        off(self.runtime, self.roster, "claude", *current)
        self.assertIsNone(self.run_as("claude-old"))               # named, and on: it runs
        for requested in ("opus", None, "a-model-nobody-listed"):
            with self.assertRaises(fleetctl.NoModelOn):            # not asked for by name: the provider is off
                self.run_as(requested)
        with tempfile.TemporaryDirectory() as directory:
            claude = fleetctl.fleet_overview(self.roster, self.runtime, Path(directory))["pools"][0]
        self.assertEqual((claude["models"]["state"], claude["models"]["on"]), ("none", ["claude-old"]))
        self.assertEqual(claude["models"]["current"], list(current))
        self.runtime = {}
        off(self.runtime, self.roster, "claude", "claude-old")     # an older one off changes nothing for the rest
        self.assertIsNone(self.run_as("opus"))
        with tempfile.TemporaryDirectory() as directory:
            claude = fleetctl.fleet_overview(self.roster, self.runtime, Path(directory))["pools"][0]
        self.assertEqual(claude["models"]["state"], "all")

    def test_the_usual_model_follows_the_switches(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(fleetctl.fleet_overview(self.roster, self.runtime, Path(directory))["pools"][0]["usual"], "opus")
            off(self.runtime, self.roster, "claude", "claude-opus")
            self.assertEqual(fleetctl.fleet_overview(self.roster, self.runtime, Path(directory))["pools"][0]["usual"], "claude-sonnet")

    def test_the_overview_and_the_brief_say_what_the_switches_add_up_to(self):
        roster = routed_roster()
        states = []
        for down in ((), ("flash",), ("flash", "big"), ("flash", "big", "reader")):
            runtime = {}
            off(runtime, roster, "go", *down)
            with tempfile.TemporaryDirectory() as directory:
                overview = fleetctl.fleet_overview(roster, runtime, Path(directory))
            go = next(p for p in overview["pools"] if p["pool"] == "go")
            states.append(go["models"]["state"])
            self.assertEqual([o["on"] for o in go["options"] if o["run_as"]], [m not in down for m in ("flash", "big", "reader")])
        self.assertEqual(states, ["all", "some", "one", "none"])
        runtime = {}
        off(runtime, roster, "go", "flash", "reader")
        with tempfile.TemporaryDirectory() as directory:
            brief = fleetctl.render_brief(fleetctl.fleet_overview(roster, runtime, Path(directory)), verbose=True)
        self.assertIn("Switched off: go flash, reader (the router skips them)", brief)
        self.assertIn("Never name a switched-off model", brief)
        off(runtime, roster, "go", "big")
        with tempfile.TemporaryDirectory() as directory:
            brief = fleetctl.render_brief(fleetctl.fleet_overview(roster, runtime, Path(directory)), verbose=True)
        self.assertIn("Switched off: go every model (provider off)", brief)
        runtime = {}
        off(runtime, self.roster, "claude", "claude-opus")
        with tempfile.TemporaryDirectory() as directory:
            brief = fleetctl.render_brief(fleetctl.fleet_overview(self.roster, runtime, Path(directory)), verbose=True)
        # a direct pool names what is off AND what a run that asks for it gets instead
        self.assertIn("Switched off: claude claude-opus (claude-sonnet runs instead)", brief)


def write_fake(bin_dir: Path, name: str, args_file: Path) -> None:
    fake = bin_dir / name
    fake.write_text("#!/bin/bash\n"
                    f"printf '%s\\n' \"$@\" > {args_file}\n"
                    "last=''; while [ $# -gt 0 ]; do case \"$1\" in -o) last=\"$2\"; shift 2;; *) shift;; esac; done\n"
                    "[ -n \"$last\" ] && echo done > \"$last\"\n"
                    "echo '{\"result\":\"done\"}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


class WrapperDispatchTests(unittest.TestCase):
    """The switches reach the CLI: dry runs print the flag, and a real launch passes it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.overlay = root / "overlay.json"
        self.overlay.write_text(json.dumps(base_overlay()))
        self.bin = root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, FLEET_STATE_DIR=str(self.state), ACCESS_OVERLAY=str(self.overlay),
                        FLEET_NO_AUTO_REFRESH="1", PATH=f"{self.bin}:{os.environ['PATH']}")

    def tearDown(self):
        self.tmp.cleanup()

    def fleet(self, *args):
        return subprocess.run(["python3", str(SCRIPTS / "fleetctl.py"), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def wrapper(self, name, *args):
        return subprocess.run(["bash", str(SCRIPTS / name), "--prompt", "say hi", "--dir", self.tmp.name, *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_cli_switches_models_and_prints_the_state(self):
        self.assertEqual(self.fleet("model-toggle", "codex", "gpt-6.1-sol").stdout.strip(), "on")
        self.assertEqual(self.fleet("model-toggle", "codex", "gpt-6.1-sol", "off").stdout.strip(), "off")
        self.assertEqual(self.fleet("model-toggle", "codex", "gpt-6-astra").stdout.strip(), "on")
        refused = self.fleet("model-toggle", "codex", "not-a-model", "off")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("models on codex", refused.stderr)
        self.assertEqual(self.fleet("model-toggle", "codex", "gpt-6-astra", "off").stdout.strip(), "off")
        self.assertEqual(self.fleet("model-toggle", "codex", "gpt-6-sol", "off").stdout.strip(), "off")  # older Sol is selectable too
        self.assertEqual(self.fleet("model-choice", "codex").stdout.strip(), "gpt-6-luna")   # one left on

    def test_model_choice_still_means_only_this_one(self):
        self.assertEqual(self.fleet("model-choice", "codex").stdout, "")
        result = self.fleet("model-choice", "codex", "gpt-6.1-sol")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "gpt-6.1-sol"))
        self.assertEqual(self.fleet("model-toggle", "codex", "gpt-6-astra").stdout.strip(), "off")
        refused = self.fleet("model-choice", "codex", "not-a-model")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("choosable on codex", refused.stderr)
        self.assertEqual(self.fleet("model-choice", "codex", "auto").stdout, "")
        self.assertEqual(self.fleet("model-toggle", "codex", "gpt-6-astra").stdout.strip(), "on")

    def test_model_run_names_the_stand_in_and_exits_5_when_none_is_on(self):
        self.assertEqual(self.fleet("model-run", "codex", "gpt-6-luna").stdout, "")
        self.fleet("model-toggle", "codex", "gpt-6-luna", "off")
        self.assertEqual(self.fleet("model-run", "codex", "gpt-6-luna").stdout.strip(), "gpt-6.1-sol")
        self.assertEqual(self.fleet("model-run", "codex", "gpt-6-astra").stdout, "")
        for model in ("gpt-6-astra", "gpt-6.1-sol"):
            self.fleet("model-toggle", "codex", model, "off")
        none = self.fleet("model-run", "codex", "gpt-6-luna")
        self.assertEqual((none.returncode, none.stdout), (5, ""))
        self.assertIn("no model is switched on for codex", none.stderr)

    def test_codex_dry_run_runs_the_nearest_model_that_is_on(self):
        before = self.wrapper("codex-agent.sh", "--model", "gpt-6-astra", "--dry-run")
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertRegex(before.stdout, r"^codex exec .* -m gpt-6-astra .*<prompt>$")
        self.fleet("model-toggle", "codex", "gpt-6-astra", "off")
        after = self.wrapper("codex-agent.sh", "--model", "gpt-6-astra", "--dry-run")
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertIn(" -m gpt-6.1-sol ", after.stdout)
        self.assertNotIn("gpt-6-astra", after.stdout)
        self.assertIn("model gpt-6.1-sol, the nearest one switched on in the Crossfeed console (gpt-6-astra is off)", after.stderr)
        self.assertIn(" -m gpt-6-luna ", self.wrapper("codex-agent.sh", "--model", "gpt-6-luna", "--dry-run").stdout)

    def test_a_dry_run_takes_no_slot_and_never_waits(self):
        self.fleet("level", "codex", "low")
        holder = subprocess.Popen(["sleep", "30"])
        try:
            self.assertEqual(self.fleet("pool-slot", "codex", "--pid", str(holder.pid)).returncode, 0)
            busy = subprocess.run(["python3", str(SCRIPTS / "fleetctl.py"), "pool-slot", "codex", "--pid", "1", "--wait", "0"],
                                  env=self.env, capture_output=True, text=True, timeout=60)
            self.assertEqual(busy.returncode, 5)              # a real run would queue behind the holder
            dry = self.wrapper("codex-agent.sh", "--dry-run")  # a dry run does not
            self.assertEqual(dry.returncode, 0, dry.stderr)
            leases = (fleetctl.load_json(self.state / "runtime.json", {}) or {}).get("leases", [])
            self.assertEqual(len(leases), 1)
        finally:
            holder.kill()
            holder.wait()

    def test_codex_real_launch_passes_the_stand_in_and_none_on_is_refused(self):
        args_file = Path(self.tmp.name) / "codex-args"
        write_fake(self.bin, "codex", args_file)
        self.fleet("model-toggle", "codex", "gpt-6-astra", "off")
        result = self.wrapper("codex-agent.sh", "--model", "gpt-6-astra", "--idle-timeout", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = args_file.read_text().splitlines()
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
        for model in ("gpt-6.1-sol", "gpt-6-luna"):
            self.fleet("model-toggle", "codex", model, "off")
        args_file.unlink()
        refused = self.wrapper("codex-agent.sh", "--idle-timeout", "0")
        self.assertEqual(refused.returncode, 5)
        self.assertIn("no model is switched on for codex in the Crossfeed console", refused.stderr)
        self.assertFalse(args_file.exists())                  # nothing was started

    def test_claude_default_stands_in_when_opus_is_off_and_none_on_is_refused(self):
        dry = self.wrapper("claude-agent.sh", "--dry-run")
        self.assertIn(" --model opus ", dry.stdout)
        self.fleet("model-toggle", "claude", "claude-opus-5-5", "off")
        dry = self.wrapper("claude-agent.sh", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(" --model claude-sonnet-5-5 ", dry.stdout)
        self.assertIn("the nearest one switched on in the Crossfeed console (opus is off)", dry.stderr)
        self.assertIn(" --model sonnet ", self.wrapper("claude-agent.sh", "--model", "sonnet", "--dry-run").stdout)
        args_file = Path(self.tmp.name) / "claude-args"
        write_fake(self.bin, "claude", args_file)
        result = self.wrapper("claude-agent.sh", "--idle-timeout", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = args_file.read_text().splitlines()
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        for model in ("claude-sonnet-5-5", "claude-sonnet-5"):
            self.fleet("model-toggle", "claude", model, "off")
        self.assertEqual(self.wrapper("claude-agent.sh", "--dry-run").returncode, 5)

    def test_claude_wrapper_default_matches_the_engine(self):
        source = (SCRIPTS / "claude-agent.sh").read_text()
        self.assertIn(f'MODEL="{fleetctl.CLAUDE_WRAPPER_DEFAULT}"', source)


class ConsoleToggleRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["FLEET_NO_AUTO_REFRESH"] = "1"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        cls.overlay = cls.state / "overlay.json"
        cls.overlay.write_text(json.dumps(base_overlay()), encoding="utf-8")
        cls.app = console.Console(cls.overlay, cls.state, 0)
        cls.server = console.bind(cls.app, 0)
        import threading
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        (self.state / "runtime.json").unlink(missing_ok=True)
        self.app.set_toggle("codex", "gpt-6-sol", False)  # fixture: only the three current models start on

    def request(self, method, path, headers=None, body=None, host=None, conn=None):
        own = conn is None
        conn = conn or http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.app.port}")
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        data = body.encode() if isinstance(body, str) else body
        if data is not None:
            conn.putheader("Content-Type", "application/x-www-form-urlencoded")
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        response = conn.getresponse()
        payload = response.read()
        if own:
            conn.close()
        return response, payload

    def cookie(self):
        response, _ = self.request("GET", f"/login?key={self.app.login_key}")
        return response.getheader("Set-Cookie").split(";", 1)[0]

    def origin(self):
        return f"http://127.0.0.1:{self.app.port}"

    def on(self, pool):
        roster = fleetctl.read_overlay(self.overlay)
        runtime = fleetctl.load_json(self.state / "runtime.json", {}) or {}
        return [model for model, value in fleetctl.pool_switches(roster, runtime, pool).items() if value]

    def form(self, pool, token=None, **fields):
        return urllib.parse.urlencode({"pool": pool, "t": token or self.app.form_token, **fields})

    def test_model_post_keeps_every_existing_lock(self):
        cookie = self.cookie()
        body = self.form("codex", switch="gpt-6.1-sol=off")
        for headers, status_code, host in [({"Origin": self.origin()}, 401, None), ({"Cookie": cookie}, 403, None),
                                           ({"Cookie": cookie, "Origin": "http://evil.example"}, 403, None),
                                           ({"Cookie": cookie, "Origin": self.origin(), "Sec-Fetch-Site": "cross-site"}, 403, None),
                                           ({"Cookie": cookie, "Origin": self.origin()}, 421, "evil.example")]:
            response, _ = self.request("POST", "/model", headers=headers, body=body, host=host)
            self.assertEqual(response.status, status_code)
        response, _ = self.request("POST", "/model", headers={"Cookie": cookie, "Origin": self.origin()},
                                   body=self.form("codex", token="stale", switch="gpt-6.1-sol=off"))
        self.assertEqual(response.status, 403)
        self.assertEqual(len(self.on("codex")), 3)

    def test_a_switch_saves_answers_json_and_redirects_without_script(self):
        headers = {"Cookie": self.cookie(), "Origin": self.origin(), "Accept": "application/json"}
        response, body = self.request("POST", "/model", headers=headers, body=self.form("codex", switch="gpt-6.1-sol=off"))
        self.assertEqual(response.status, 200)
        codex = next(p for p in json.loads(body)["pools"] if p["pool"] == "codex")
        self.assertEqual((codex["on"], codex["state"], codex["label"]), (["gpt-6-astra", "gpt-6-luna"], "some", "2 of 3 on"))
        # Older models without a recorded reason are off too, so codex need not be the first pool on the line.
        self.assertIn("codex/gpt-6-sol -> gpt-6-luna", json.loads(body)["brief"])
        self.assertIn("codex/gpt-6.1-sol -> gpt-6-luna", json.loads(body)["brief"])
        response, body = self.request("POST", "/model", headers=headers, body=self.form("codex", switch="gpt-6-astra=off"))
        codex = next(p for p in json.loads(body)["pools"] if p["pool"] == "codex")
        self.assertEqual((codex["on"], codex["state"], codex["label"]), (["gpt-6-luna"], "one", "Only GPT-6 Luna on"))
        headers.pop("Accept")
        response, _ = self.request("POST", "/model", headers=headers, body=self.form("codex", switch="gpt-6.1-sol=on"))
        self.assertEqual((response.status, response.getheader("Location")), (303, "/#pool-codex"))
        self.assertEqual(self.on("codex"), ["gpt-6.1-sol", "gpt-6-luna"])

    def test_none_on_and_switch_all_on(self):
        headers = {"Cookie": self.cookie(), "Origin": self.origin(), "Accept": "application/json"}
        for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"):
            response, body = self.request("POST", "/model", headers=headers, body=self.form("codex", switch=f"{model}=off"))
        codex = next(p for p in json.loads(body)["pools"] if p["pool"] == "codex")
        self.assertEqual((codex["on"], codex["state"], codex["label"]), ([], "none", "None on"))
        self.assertIn("Codex (ChatGPT) is off until you switch one on", codex["note"])
        response, body = self.request("POST", "/model", headers=headers, body=self.form("codex", model="auto"))
        codex = next(p for p in json.loads(body)["pools"] if p["pool"] == "codex")
        self.assertEqual((codex["state"], codex["label"]), ("all", "All enabled"))

    def test_the_older_model_field_still_means_only_this_one(self):
        headers = {"Cookie": self.cookie(), "Origin": self.origin()}
        response, _ = self.request("POST", "/model", headers=headers, body=self.form("codex", model="gpt-6-luna"))
        self.assertEqual(response.status, 303)
        self.assertEqual(self.on("codex"), ["gpt-6-luna"])

    def test_unknown_pool_model_or_state_changes_nothing(self):
        headers = {"Cookie": self.cookie(), "Origin": self.origin()}
        for pool, fields in (("codex", {"switch": "gpt-0-imaginary=off"}), ("nope", {"switch": "gpt-6.1-sol=off"}),
                             ("codex", {"switch": "gpt-6.1-sol=maybe"}), ("codex", {"switch": "gpt-6.1-sol"}),
                             ("codex", {"model": ""}), ("opencode-go", {"switch": "gpt-6.1-sol=off"})):
            response, _ = self.request("POST", "/model", headers=headers, body=self.form(pool, **fields))
            self.assertEqual(response.status, 400, (pool, fields))
        self.assertEqual(len(self.on("codex")), 3)

    def test_one_connection_carries_page_assets_and_clicks(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
        cookie = self.cookie()
        page, _ = self.request("GET", "/", headers={"Cookie": cookie}, conn=conn)
        self.assertEqual((page.status, page.version, page.getheader("Cache-Control")), (200, 11, "no-store"))
        css, _ = self.request("GET", "/static/console.css", conn=conn)
        self.assertEqual(css.getheader("Cache-Control"), "private, max-age=31536000, immutable")
        font, _ = self.request("GET", "/static/fonts/literata.woff2", conn=conn)
        self.assertEqual(font.getheader("Cache-Control"), "private, max-age=604800")
        self.assertNotIn("no-store", font.getheader("Cache-Control"))
        conn.close()


class PickerRenderTests(unittest.TestCase):
    def render(self, runtime=None, roster=None):
        roster = roster or base_overlay()
        with tempfile.TemporaryDirectory() as directory:
            overview = fleetctl.fleet_overview(roster, runtime or {}, Path(directory))
        return overview, console.render_page(overview, "tok")

    def picker(self, page, pool):
        start = page.index(f'id="pick-{pool}"')
        return page[start:page.index("</form>", start)]

    def test_every_model_provider_has_a_switch_in_words_and_all_on_is_the_default(self):
        overview, page = self.render()
        for pool in overview["pools"]:
            self.assertIn(f'id="pick-{pool["pool"]}"', page)
        codex = self.picker(page, "codex")
        self.assertEqual(codex.count('role="switch"'), 4)  # three current models plus older Sol
        self.assertEqual(codex.count('aria-checked="true"'), 3)  # unjustified older Sol defaults off
        older = codex.split('<details class="older">')[1]
        self.assertIn('data-model="gpt-6-sol"', older)
        self.assertEqual(older.count('role="switch"'), 1)
        for name in ("GPT-6 Astra", "GPT-6.1 Sol", "GPT-6 Luna"):
            self.assertIn(f'data-name="{name}"', codex)
            self.assertIn(f'aria-label="Use {name}"', codex)
        self.assertIn('<span class="st">On</span>', codex)
        self.assertIn('<b class="v">All enabled</b>', codex)
        self.assertRegex(codex, r'class="all-on"[^>]*hidden>Switch all on')
        self.assertNotIn("Crossfeed decides</span>", codex)

    def test_the_line_says_what_the_switches_add_up_to(self):
        cases = {
            (): ("all", "All enabled"),
            ("gpt-6.1-sol",): ("some", "2 of 3 on"),
            ("gpt-6.1-sol", "gpt-6-luna"): ("one", "Only GPT-6 Astra on"),
            ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"): ("none", "None on"),
        }
        for down, (state, label) in cases.items():
            runtime = {}
            off(runtime, base_overlay(), "codex", *down)
            _, page = self.render(runtime)
            codex = self.picker(page, "codex")
            self.assertIn(f'<span class="now {state}"><i class="mark" aria-hidden="true"></i><b class="v">{label}</b>', codex, down)
            self.assertEqual(codex.count('aria-checked="true"'), 3 - len(down))  # unjustified older Sol stays off
            self.assertEqual('class="all-on" type="submit" name="model" value="auto" hidden' in codex.replace("  ", " "), not down)

    def test_a_switch_sends_the_state_it_would_set(self):
        runtime = {}
        off(runtime, base_overlay(), "codex", "gpt-6.1-sol")
        _, page = self.render(runtime)
        codex = self.picker(page, "codex")
        self.assertIn('name="switch" value="gpt-6.1-sol=on"', codex)
        self.assertIn('name="switch" value="gpt-6-astra=off"', codex)
        self.assertIn('<li class="opt off"', codex)
        self.assertIn('<span class="st">Off</span>', codex)

    def test_older_models_fold_keep_their_switches_and_say_how_many_are_on(self):
        _, page = self.render()
        claude = page[page.index('id="pick-claude"'):]
        self.assertIn('<details class="older"><summary>Older models <span class="count">1 · 0 on</span>', claude)
        older = claude[claude.index('<details class="older">'):claude.index("</details>", claude.index('<details class="older">'))]
        self.assertIn('data-name="Sonnet 5"', older)
        runtime = {}
        off(runtime, base_overlay(), "claude", "claude-sonnet-5")
        _, page = self.render(runtime)
        self.assertIn('<span class="count">1 · 0 on</span>', page)
        claude = self.picker(page, "claude")
        self.assertIn('<b class="v">All enabled</b>', claude)          # an older model is never counted...
        self.assertRegex(claude, r'class="all-on"[^>]*hidden')             # ...and "Switch all on", which is the current list, has nothing to do
        self.assertIn("An older model runs only when a task asks for it by name", claude)

    def test_a_model_the_router_cannot_run_shows_no_switch(self):
        roster = base_overlay()
        roster["lanes"][0]["admission_status"] = "retired"
        key = roster["lanes"][0]["model_key"]
        _, page = self.render(roster=roster)
        row = page[page.index(f'data-model="{key}"'):]
        row = row[:row.index("</li>")]
        self.assertNotIn('role="switch"', row)
        self.assertIn('<span class="na">Unavailable</span>', row)

    def test_details_are_three_plain_facts_at_most_with_more_behind(self):
        _, page = self.render()
        for block in re.findall(r'<ul class="bullets">(.*?)</ul>', page):
            self.assertLessEqual(block.count("<li>"), 2)   # the third fact is the line under the name
        self.assertIn('<details class="more"><summary>More<span class="vh"> about GPT-6 Astra</span></summary>', page)
        self.assertIn('<summary>Details<span class="vh"> about GPT-6.1 Sol</span></summary>', page)

    def test_an_older_single_choice_shows_as_one_on(self):
        _, page = self.render({"model_choices": {"codex": "gpt-6-luna"}})
        codex = self.picker(page, "codex")
        self.assertIn('<b class="v">Only GPT-6 Luna on</b>', codex)
        self.assertIn('name="switch" value="gpt-6-luna=off"', codex)
        self.assertIn('name="switch" value="gpt-6-astra=on"', codex)

    def test_header_holds_search_shortcuts_and_line_is_the_column(self):
        _, page = self.render()
        self.assertIn('aria-keyshortcuts="/ Meta+K Control+K"', page)
        self.assertIn('title="Search everything  /"', page)
        self.assertIn('<kbd class="key-hint" aria-hidden="true">⌘K</kbd>', page)
        self.assertIn('<header class="mast" data-line>', page)
        css = (console.ASSETS / "console.css").read_text()
        mast = re.search(r"\n\.mast\{position:relative;[^}]*\}", css).group(0)
        self.assertIn("margin:0;", mast)             # no bleed past the column
        self.assertIn("padding:28px 0 10px", mast)
        # held, it is fixed to the screen and its contents keep to the column (test_family_header has the rest)
        self.assertIn(".mast{position:fixed;top:0;left:0;right:0;", css)
        self.assertIn("padding-left:var(--gut);padding-right:var(--gut)", css)

    def test_the_switch_is_a_real_target_and_its_state_is_in_words(self):
        css = (console.ASSETS / "console.css").read_text()
        block = css[css.index("/* Model switches */"):]
        sw = re.search(r"\n\.sw\{([^}]*)\}", block).group(1)
        self.assertIn("min-height:44px", sw)
        self.assertIn("min-width:44px", sw)
        self.assertEqual(css.count("/* Model switches */"), 1)     # one block, at the end of the file
        self.assertNotIn(".choice", css)                           # the single-choice picker's rules are gone

    def test_every_string_is_escaped(self):
        roster = base_overlay()
        roster["model_cards"]["gpt-6.1-sol"]["name"] = "<img src=x onerror=alert(1)>"
        roster["model_cards"]["gpt-6.1-sol"]["best_for"] = '"><script>alert(1)</script>'
        _, page = self.render(roster=roster)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<script>alert(1)", page)


if __name__ == "__main__":
    unittest.main()
