"""Spend levels (off, low, normal, high, forced), the brief, and plan awareness.

Self-contained on purpose: its fixtures live here, so the file can be dropped into any
install of the engine without depending on another test module or a shipped overlay.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fleetctl.py"
SPEC = importlib.util.spec_from_file_location("fleetctl", MODULE_PATH)
assert SPEC and SPEC.loader
fleetctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleetctl)


def lane(lane_id, model, modes=("read-only",), inputs=("text",), tier="strong", cap=1):
    return {
        "lane_id": lane_id, "model_key": model, "harness": "opencode", "provider": "opencode-go",
        "selector": f"opencode-go/{model}", "access_status": "verified", "admission_status": "active",
        "allowed_modes": list(modes), "quota_pool": "opencode-go", "quality_tier": tier,
        "capabilities": {"input": list(inputs)}, "max_parallel": cap,
    }


def roster():
    return {
        "schema_version": 3,
        "quota_pools": {
            "opencode-go": {"quota_refresh": {"oracle": "codexbar", "provider": "opencodego", "ttl_s": 600}},
            "claude": {"quota_refresh": {"oracle": "codexbar", "provider": "claude", "ttl_s": 1800}},
            "codex": {"quota_refresh": {"oracle": "codexbar", "provider": "codex", "ttl_s": 1800}},
            "github-copilot-student": {"quota_refresh": {"oracle": "codexbar", "provider": "copilot", "ttl_s": 1800}},
        },
        "routing": {"roles": {
            "default": {"quality_first": ["k3", "k27", "flash"], "conserve": ["k27", "flash"], "critical": ["flash"]},
            "implementation": {"quality_first": ["k27", "flash"], "conserve": ["k27", "flash"], "critical": ["flash"]},
        }},
        "lanes": [
            lane("k3", "kimi-k3", inputs=("text", "image", "video"), tier="frontier"),
            lane("k27", "kimi-k2.7-code", modes=("read-only", "write")),
            lane("flash", "deepseek-v4-flash", modes=("read-only", "write"), cap=4),
        ],
    }


def snapshot(used):
    now = fleetctl.utc_now()
    return {
        "source": "opencode-console-dashboard",
        "observed_at": fleetctl.iso(now),
        "precision_percentage_points": 1,
        "windows": {
            "rolling_5h": {"used_percent": used, "reset_at": fleetctl.iso(now + timedelta(hours=2))},
            "weekly": {"used_percent": min(used, 99), "reset_at": fleetctl.iso(now + timedelta(days=2))},
            "monthly": {"used_percent": min(used, 99), "reset_at": fleetctl.iso(now + timedelta(days=20))},
        },
    }

def tiered_roster():
    """The fixture roster with real cost spread: flash is the cheap lane, k3 the big one."""
    data = roster()
    for entry in data["lanes"]:
        if entry["lane_id"] == "flash":
            entry["quality_tier"] = "standard"
    return data


def at_level(runtime, pool, level):
    fleetctl.set_pool_level(runtime, pool, level)
    return runtime


class SpendLevelTests(unittest.TestCase):
    """Off · Low · Normal · High · Forced, one per pool, and what the router does with each."""

    def test_normal_is_todays_behaviour(self):
        for used in (14, 60, 80, 95):
            plain = {"quota_snapshots": {"opencode-go": snapshot(used)}}
            normal = at_level({"quota_snapshots": {"opencode-go": snapshot(used)}}, "opencode-go", "normal")
            self.assertNotIn("opencode-go", normal.get("switches", {}))
            self.assertEqual(
                fleetctl.choose_lane(tiered_roster(), plain, "default", "read-only", "text")["lane_id"],
                fleetctl.choose_lane(tiered_roster(), normal, "default", "read-only", "text")["lane_id"],
            )

    def test_low_picks_the_cheapest_capable_lane(self):
        runtime = at_level({"quota_snapshots": {"opencode-go": snapshot(14)}}, "opencode-go", "low")
        selected = fleetctl.choose_lane(tiered_roster(), runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "flash")
        self.assertEqual(selected["routing"]["level"], "low")
        # Capability still gates: a write task on low gets the cheapest lane that can write.
        write = fleetctl.choose_lane(tiered_roster(), runtime, "implementation", "write", "text")
        self.assertEqual(write["lane_id"], "flash")

    def test_low_allows_the_big_model_as_a_one_shot(self):
        runtime = at_level({"quota_snapshots": {"opencode-go": snapshot(14)}}, "opencode-go", "low")
        selected = fleetctl.choose_lane(tiered_roster(), runtime, "default", "read-only", "text", one_shot=True)
        self.assertEqual(selected["lane_id"], "k3")
        big = fleetctl.lane_map(tiered_roster())["k3"]
        big["max_parallel"] = 3
        self.assertEqual(fleetctl.effective_cap(big, "ABUNDANT", None, "low"), 1)

    def test_low_is_one_call_at_a_time_across_the_pool(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            with fleetctl.locked_runtime(state) as runtime:
                fleetctl.set_pool_level(runtime, "opencode-go", "low")
            fleetctl.acquire_lease(state, tiered_roster(), "flash", 600)
            with self.assertRaisesRegex(fleetctl.FleetError, "one call at a time"):
                fleetctl.acquire_lease(state, tiered_roster(), "k27", 600)
            runtime = fleetctl.load_json(state / "runtime.json", {})
            k27 = fleetctl.lane_map(tiered_roster())["k27"]
            self.assertEqual(fleetctl.lane_free_slots(runtime, k27, "ABUNDANT"), 0)

    def test_high_keeps_strong_models_through_conserve(self):
        conserve = {"quota_snapshots": {"opencode-go": snapshot(80)}}
        self.assertEqual(
            fleetctl.choose_lane(tiered_roster(), conserve, "default", "read-only", "text")["lane_id"], "k27")
        high = at_level({"quota_snapshots": {"opencode-go": snapshot(80)}}, "opencode-go", "high")
        self.assertEqual(
            fleetctl.choose_lane(tiered_roster(), high, "default", "read-only", "text")["lane_id"], "k3")
        frontier = dict(fleetctl.lane_map(tiered_roster())["k3"], max_parallel=3)
        self.assertEqual(fleetctl.effective_cap(frontier, "CONSERVE", None, "normal"), 1)
        self.assertEqual(fleetctl.effective_cap(frontier, "CONSERVE", None, "high"), 3)
        # "Within quota": a critical pool clamps the frontier lane even at high.
        self.assertEqual(fleetctl.effective_cap(frontier, "CRITICAL", None, "high"), 1)
        # High never exceeds what the provider tolerates.
        self.assertEqual(fleetctl.effective_cap(frontier, "ABUNDANT", None, "high"), 3)

    def test_forced_routes_through_an_exhausted_gauge(self):
        exhausted = {"quota_snapshots": {"opencode-go": snapshot(100)}}
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.choose_lane(tiered_roster(), exhausted, "default", "read-only", "text")
        forced = at_level({"quota_snapshots": {"opencode-go": snapshot(100)}}, "opencode-go", "forced")
        self.assertEqual(
            fleetctl.choose_lane(tiered_roster(), forced, "default", "read-only", "text")["lane_id"], "k3")

    def test_off_never_routes_and_never_leases(self):
        runtime = at_level({"quota_snapshots": {"opencode-go": snapshot(5)}}, "opencode-go", "off")
        with self.assertRaisesRegex(fleetctl.FleetError, "switched off"):
            fleetctl.choose_lane(tiered_roster(), runtime, "default", "read-only", "text")
        with tempfile.TemporaryDirectory() as d:
            with fleetctl.locked_runtime(Path(d)) as stored:
                fleetctl.set_pool_level(stored, "opencode-go", "off")
            with self.assertRaisesRegex(fleetctl.FleetError, "switched off by hand"):
                fleetctl.acquire_lease(Path(d), tiered_roster(), "flash", 600)

    def test_a_low_pool_yields_nothing_to_other_pools_rank(self):
        """Reordering stays inside a low pool's own slots: another pool's lane keeps its place."""
        data = tiered_roster()
        other = lane("agy", "gemini-flash", tier="standard")
        other["quota_pool"] = "antigravity-gemini"
        data["lanes"].append(other)
        data["routing"]["roles"]["default"]["quality_first"] = ["k3", "agy", "k27", "flash"]
        runtime = at_level({}, "opencode-go", "low")
        ordered = fleetctl.order_for_levels(
            data["routing"]["roles"]["default"]["quality_first"], fleetctl.lane_map(data), runtime)
        self.assertEqual(ordered, ["flash", "agy", "k27", "k3"])


class LevelCompatibilityTests(unittest.TestCase):
    """Existing runtime files and the three-way switch keep their meaning."""

    def test_stored_values_read_as_levels(self):
        self.assertEqual(fleetctl.pool_level({"switches": {"codex": "on"}}, "codex"), "forced")
        self.assertEqual(fleetctl.pool_level({"switches": {"codex": "off"}}, "codex"), "off")
        self.assertEqual(fleetctl.pool_level({"switches": {}}, "codex"), "normal")
        self.assertEqual(fleetctl.pool_level({}, "codex"), "normal")
        self.assertEqual(fleetctl.pool_level({"switches": []}, "codex"), "normal")  # malformed file
        self.assertEqual(fleetctl.pool_level({"switches": {"codex": "sideways"}}, "codex"), "normal")

    def test_levels_are_stored_in_the_legacy_vocabulary(self):
        runtime = {}
        for level, stored in (("forced", "on"), ("off", "off"), ("low", "low"), ("high", "high")):
            fleetctl.set_pool_level(runtime, "codex", level)
            self.assertEqual(runtime["switches"]["codex"], stored)
        fleetctl.set_pool_level(runtime, "codex", "normal")
        self.assertNotIn("codex", runtime["switches"])
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.set_pool_level(runtime, "codex", "turbo")

    def test_legacy_forced_on_still_reads_abundant(self):
        state, evidence = fleetctl.current_pool_state({"switches": {"codex": "on"}}, "codex")
        self.assertEqual((state, evidence["source"]), ("ABUNDANT", "forced-on"))


class LevelCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.overlay = self.dir / "overlay.json"
        data = tiered_roster()
        # Keep this healthy synthetic roster healthy; missing-data sabotage has
        # separate tests. The legacy verbose brief keeps the full plan details.
        data["effort"] = {row["model_key"]: {
            "levels": {row["harness"]: []}, "default": "provider-default",
            "knee": "provider-default", "by_role": {},
            "evidence": {"source": "synthetic no-control model", "status": "unmeasured",
                         "read_on": fleetctl.utc_now().date().isoformat()}, "recheck": "fixture changes",
        } for row in data["lanes"]}
        data["quota_pools"]["opencode-go"]["plan"] = {
            "name": "Go", "price": 10, "currency": "USD", "billing": "subscription",
            "allowance": "$60 of model value a month",
        }
        data["quota_pools"]["gemini-metered"] = {"daily_usd_cap": 1.0}
        self.overlay.write_text(json.dumps(data), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, check=True):
        env = dict(os.environ, FLEET_NO_AUTO_REFRESH="1")
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), "--overlay", str(self.overlay), "--state-dir", str(self.dir), *args],
            capture_output=True, text=True, check=check, env=env,
        )

    def test_level_sets_shows_and_lists(self):
        self.assertEqual(self.run_cli("level", "opencode-go", "low").stdout.strip(), "low")
        self.assertEqual(self.run_cli("level", "opencode-go").stdout.strip(), "low")
        listing = self.run_cli("level").stdout
        self.assertIn("opencode-go: low", listing)
        self.assertIn("codex: normal", listing)

    def test_level_and_switch_agree(self):
        """The wrappers read `switch <pool>` and refuse on "off"; that contract must hold."""
        self.run_cli("switch", "codex", "on")
        self.assertEqual(self.run_cli("level", "codex").stdout.strip(), "forced")
        self.run_cli("level", "codex", "low")
        self.assertEqual(self.run_cli("switch", "codex").stdout.strip(), "auto")
        self.run_cli("level", "codex", "off")
        self.assertEqual(self.run_cli("switch", "codex").stdout.strip(), "off")
        self.run_cli("switch", "codex", "auto")
        self.assertEqual(self.run_cli("level", "codex").stdout.strip(), "normal")

    def test_level_refuses_an_unknown_pool_or_value(self):
        result = self.run_cli("level", "nope", "low", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown pool nope", result.stderr)
        self.assertNotEqual(self.run_cli("level", "codex", "turbo", check=False).returncode, 0)

    def test_a_query_never_writes_the_runtime_file(self):
        self.run_cli("level")
        self.run_cli("level", "codex")
        self.assertFalse((self.dir / "runtime.json").exists())

    def test_verbose_brief_keeps_all_details_without_codexbar(self):
        for pool, level in (("opencode-go", "low"), ("codex", "high"), ("claude", "forced"),
                            ("github-copilot-student", "off")):
            self.run_cli("level", pool, level)
        text = self.run_cli("brief", "--verbose").stdout.strip().splitlines()
        # A header, one line per level in use, then the limits: a heading and one line per plan
        # that has a limit to show (with no quota readings, only the paid pool's daily budget),
        # and the additive selector command in the footer.
        self.assertEqual(len(text), 9)
        self.assertEqual([line.split(" ")[0] for line in text[1:6]], ["FORCED", "HIGH", "NORMAL", "LOW", "OFF"])
        self.assertTrue(text[6].startswith("Limits ("))
        self.assertEqual(text[7], "  gemini-metered: daily budget $0.00 of $1.00, resets at midnight")
        self.assertEqual(text[8], "Pick with: fleetctl.py select --role R")
        self.assertIn("no quota readings", text[0])
        body = "\n".join(text)
        for word in ("FORCED", "HIGH", "NORMAL", "LOW", "OFF"):
            self.assertIn(word, body)
        self.assertIn("opencode-go", [line for line in text if line.startswith("LOW")][0])
        self.assertIn("Go $10/mo ≈$60 of model value a month", body)
        self.assertIn("gemini-metered $0.00/$1.00 today paid per token", body)

    def test_compact_brief_has_one_pool_row_without_codexbar(self):
        levels = {"opencode-go": "low", "codex": "high", "claude": "forced",
                  "github-copilot-student": "off", "gemini-metered": "normal"}
        for pool, level in levels.items():
            self.run_cli("level", pool, level)
        text = self.run_cli("brief", "--no-refresh").stdout.strip().splitlines()
        self.assertEqual(len(text), len(levels) + 3)
        self.assertEqual(text[0], "pool | level | binding window | used | resets | price | models on")
        rows = [line.split(" | ") for line in text[1:-2]]
        self.assertTrue(all(len(row) == 7 for row in rows))
        self.assertEqual({row[0]: row[1] for row in rows}, levels)
        self.assertTrue(all(row[5] == "~0.50" for row in rows))
        self.assertEqual(text[-2], "off: none")
        self.assertEqual(text[-1], "pick: fleetctl.py select --role R [--stakes S]")

    def test_brief_json_carries_plan_and_measured_quota(self):
        runtime = at_level({"quota_snapshots": {"opencode-go": snapshot(30)}}, "opencode-go", "forced")
        (self.dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
        data = json.loads(self.run_cli("brief", "--json").stdout)
        pools = {p["pool"]: p for p in data["pools"]}
        go = pools["opencode-go"]
        self.assertEqual(go["level"], "forced")
        # Forced ignores the gauge for routing, but the page and the brief still show it.
        self.assertEqual(go["quota"]["used_percent"], 30)
        self.assertEqual(go["plan"]["price"], 10)
        self.assertEqual(go["plan"]["billing"], "subscription")
        self.assertEqual(pools["gemini-metered"]["plan"]["billing"], "per-token")

    def test_route_accepts_one_shot(self):
        self.run_cli("level", "opencode-go", "low")
        self.assertEqual(self.run_cli("route", "--no-refresh").stdout.strip(), "flash")
        self.assertEqual(self.run_cli("route", "--no-refresh", "--one-shot").stdout.strip(), "k3")


class PlanAwarenessTests(unittest.TestCase):
    def test_codexbar_plan_label_never_keeps_an_email(self):
        payload = {"usage": {"loginMethod": "Claude Max 20x", "accountEmail": "someone@example.com",
                             "identity": {"accountEmail": "someone@example.com"}}}
        self.assertEqual(fleetctl.codexbar_plan_label(payload), "Claude Max 20x")
        self.assertEqual(
            fleetctl.codexbar_plan_label({"openaiDashboard": {"accountPlan": "Pro 20x"}, "usage": {"loginMethod": "pro"}}),
            "Pro 20x")
        self.assertIsNone(fleetctl.codexbar_plan_label({"usage": {"loginMethod": "someone@example.com"}}))
        self.assertIsNone(fleetctl.codexbar_plan_label({}))

    def test_plan_falls_back_to_the_oracle_label_and_never_guesses_a_price(self):
        plan = fleetctl.pool_plan({}, {"plan": "Claude Max 20x"})
        self.assertEqual((plan["name"], plan["name_source"], plan["price"]), ("Claude Max 20x", "oracle", None))
        self.assertEqual(fleetctl.pool_plan({"daily_usd_cap": 1}, None)["billing"], "per-token")
        self.assertIsNone(fleetctl.format_price(plan))
        self.assertEqual(fleetctl.format_price({"price": 20, "currency": "EUR", "billing": "subscription"}), "€20")



class OverviewWordingTests(unittest.TestCase):
    def test_window_names_only_when_they_mean_a_length(self):
        self.assertEqual(fleetctl.window_label("secondary", {"window_minutes": 10080}), "7d")
        self.assertEqual(fleetctl.window_label("primary", {"window_minutes": 300}), "5h")
        self.assertEqual(fleetctl.window_label("weekly", {}), "7d")
        self.assertEqual(fleetctl.window_label("primary", {}), "")

    def test_a_stale_reading_is_called_stale_not_missing(self):
        old = snapshot(40)
        old["observed_at"] = fleetctl.iso(fleetctl.utc_now() - timedelta(hours=2))
        with tempfile.TemporaryDirectory() as d:
            overview = fleetctl.fleet_overview(roster(), {"quota_snapshots": {"claude": old}}, Path(d))
        claude = {p["pool"]: p for p in overview["pools"]}["claude"]
        self.assertIsNone(claude["quota"])
        self.assertGreaterEqual(claude["stale_age_s"], 7200)
        self.assertIn("claude quota stale (2h", fleetctl.render_brief(overview, verbose=True))


ROOT = MODULE_PATH.parents[1]


def run_fleet(state, *args, env_extra=None):
    env = dict(os.environ, FLEET_NO_AUTO_REFRESH="1", **(env_extra or {}))
    return subprocess.run([sys.executable, str(MODULE_PATH), "--state-dir", str(state), *args],
                          capture_output=True, text=True, env=env)


class ForcedRespectsCircuitsTests(unittest.TestCase):
    """Forced overrides the gauges, never a failure the provider itself reported."""

    def test_an_open_circuit_beats_forced(self):
        future = fleetctl.iso(fleetctl.utc_now() + timedelta(hours=1))
        runtime = {"switches": {"opencode-go": "on"},
                   "quota_snapshots": {"opencode-go": snapshot(100)},
                   "pool_circuits": {"opencode-go": {"until": future, "limit_name": "5 hour"}}}
        state, evidence = fleetctl.current_pool_state(runtime, "opencode-go")
        self.assertEqual((state, evidence["source"]), ("EXHAUSTED", "explicit-quota-error"))
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")

    def test_forced_returns_once_the_circuit_has_passed(self):
        past = fleetctl.iso(fleetctl.utc_now() - timedelta(minutes=1))
        runtime = {"switches": {"opencode-go": "on"},
                   "quota_snapshots": {"opencode-go": snapshot(100)},
                   "pool_circuits": {"opencode-go": {"until": past}}}
        self.assertEqual(fleetctl.current_pool_state(runtime, "opencode-go")[0], "ABUNDANT")
        self.assertEqual(fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")["lane_id"], "k3")


class PoolSlotTests(unittest.TestCase):
    """Low reaches the wrappers that take no lane lease: claude, codex and copilot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.holders = []

    def tearDown(self):
        for proc in self.holders:
            proc.kill()
            proc.wait()
        self.tmp.cleanup()

    def holder(self):
        """A live process to hold a slot, standing in for a running wrapper."""
        proc = subprocess.Popen(["sleep", "60"])
        self.holders.append(proc)
        return proc

    def set_low(self, pool):
        with fleetctl.locked_runtime(self.state) as runtime:
            fleetctl.set_pool_level(runtime, pool, "low")

    def test_not_low_is_a_no_op_that_writes_nothing(self):
        result = run_fleet(self.state, "pool-slot", "codex", "--pid", str(os.getpid()))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse((self.state / "runtime.json").exists())

    def test_two_concurrent_runs_cannot_both_start(self):
        self.set_low("codex")
        a, b = self.holder(), self.holder()
        env = dict(os.environ, FLEET_NO_AUTO_REFRESH="1")
        racers = [subprocess.Popen([sys.executable, str(MODULE_PATH), "--state-dir", str(self.state),
                                    "pool-slot", "codex", "--pid", str(p.pid), "--wait", "0"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                  for p in (a, b)]
        codes = sorted(r.wait() for r in racers)
        for r in racers:
            r.stdout.close(); r.stderr.close()
        self.assertEqual(codes, [0, 5])

    def test_the_second_run_waits_then_refuses_and_the_slot_dies_with_its_holder(self):
        self.set_low("codex")
        first = self.holder()
        self.assertEqual(run_fleet(self.state, "pool-slot", "codex", "--pid", str(first.pid)).returncode, 0)
        second = run_fleet(self.state, "pool-slot", "codex", "--pid", str(self.holder().pid), "--wait", "1")
        self.assertEqual(second.returncode, 5)
        self.assertIn("waiting up to 1s", second.stderr)
        self.assertIn("one run at a time", second.stderr)
        self.assertEqual(second.stdout, "")
        first.kill(); first.wait()
        third = run_fleet(self.state, "pool-slot", "codex", "--pid", str(self.holder().pid), "--wait", "0")
        self.assertEqual(third.returncode, 0)

    def test_each_direct_wrapper_honours_low(self):
        bindir = self.state / "bin"
        bindir.mkdir()
        (bindir / "python3").symlink_to(sys.executable)
        overlay = self.state / "overlay.json"
        overlay.write_text(json.dumps(roster()), encoding="utf-8")
        # No claude, codex or copilot on this PATH: a run the gate lets through stops at the
        # binary check (127) and never reaches a real provider.
        env = dict(os.environ, PATH=f"{bindir}:/usr/bin:/bin", FLEET_STATE_DIR=str(self.state),
                   ACCESS_OVERLAY=str(overlay), FLEET_NO_AUTO_REFRESH="1", FLEET_POOL_WAIT_S="1")
        for wrapper, pool in (("codex", "codex"), ("claude", "claude"), ("copilot", "github-copilot-student")):
            with self.subTest(wrapper=wrapper):
                self.set_low(pool)
                holder = self.holder()
                self.assertEqual(run_fleet(self.state, "pool-slot", pool, "--pid", str(holder.pid)).returncode, 0)
                script = ROOT / "scripts" / f"{wrapper}-agent.sh"
                blocked = subprocess.run(["bash", str(script), "--prompt", "hi"], capture_output=True,
                                         text=True, env=env, timeout=60)
                self.assertEqual(blocked.returncode, 5, blocked.stderr)
                self.assertIn("one run at a time", blocked.stderr)
                holder.kill(); holder.wait()
                free = subprocess.run(["bash", str(script), "--prompt", "hi"], capture_output=True,
                                      text=True, env=env, timeout=60)
                self.assertEqual(free.returncode, 127, free.stderr)
                self.assertNotIn("one run at a time", free.stderr)


if __name__ == "__main__":
    unittest.main()
