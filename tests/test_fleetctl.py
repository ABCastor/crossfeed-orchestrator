import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from datetime import timedelta
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fleetctl.py"
if not MODULE_PATH.exists():
    MODULE_PATH = Path(__file__).with_name("fleetctl.py")
SPEC = importlib.util.spec_from_file_location("fleetctl", MODULE_PATH)
assert SPEC and SPEC.loader
fleetctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleetctl)


def lane(
    lane_id,
    model,
    modes=("read-only",),
    inputs=("text",),
    tier="strong",
    cap=1,
    admission="active",
):
    return {
        "lane_id": lane_id,
        "model_key": model,
        "harness": "opencode",
        "provider": "opencode-go",
        "selector": f"opencode-go/{model}",
        "access_status": "verified",
        "admission_status": admission,
        "allowed_modes": list(modes),
        "quota_pool": "opencode-go",
        "quality_tier": tier,
        "capabilities": {"input": list(inputs)},
        "max_parallel": cap,
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
        "routing": {
            "roles": {
                "default": {
                    "quality_first": ["k3", "k27", "flash"],
                    "conserve": ["k27", "flash"],
                    "critical": ["flash"],
                },
                "implementation": {
                    "quality_first": ["k27", "flash"],
                    "conserve": ["k27", "flash"],
                    "critical": ["flash"],
                },
                "media": {
                    "quality_first": ["k3", "mimo"],
                    "conserve": ["mimo", "k3"],
                    "critical": ["mimo"],
                },
            }
        },
        "lanes": [
            lane("k3", "kimi-k3", inputs=("text", "image", "video"), tier="frontier"),
            lane("k27", "kimi-k2.7-code", modes=("read-only", "write")),
            lane("flash", "deepseek-v4-flash", modes=("read-only", "write"), cap=4),
            lane("mimo", "mimo-v2.5", inputs=("text", "image", "audio", "video"), cap=2),
            lane("rejected", "bad", admission="rejected"),
        ],
    }


def metered_overlay(daily_cap=1.0):
    return {
        "schema_version": 3,
        "quota_pools": {"gemini-metered": {"daily_usd_cap": daily_cap}},
        "routing": {"roles": {}},
        "lanes": [
            {
                "lane_id": "gemini-metered-flash-lite",
                "model_key": "gemini-3.1-flash-lite",
                "harness": "opencode",
                "provider": "google",
                "selector": "google/gemini-3.1-flash-lite",
                "access_status": "verified",
                "admission_status": "active",
                "allowed_modes": ["read-only"],
                "quota_pool": "gemini-metered",
                "quality_tier": "metered",
                "capabilities": {"input": ["text", "image"]},
                "max_parallel": 1,
            }
        ],
    }


def snapshot(used, age_seconds=0):
    observed = fleetctl.utc_now() - timedelta(seconds=age_seconds)
    return {
        "source": "opencode-console-dashboard",
        "observed_at": fleetctl.iso(observed),
        "precision_percentage_points": 1,
        "windows": {
            "rolling_5h": {
                "used_percent": used,
                "reset_at": fleetctl.iso(fleetctl.utc_now() + timedelta(hours=2)),
            },
            "weekly": {
                "used_percent": min(used, 99),
                "reset_at": fleetctl.iso(fleetctl.utc_now() + timedelta(days=2)),
            },
            "monthly": {
                "used_percent": min(used, 99),
                "reset_at": fleetctl.iso(fleetctl.utc_now() + timedelta(days=20)),
            },
        },
    }


class FleetRouterTests(unittest.TestCase):
    def test_unknown_is_intelligence_first(self):
        selected = fleetctl.choose_lane(roster(), {}, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "k3")
        self.assertEqual(selected["routing"]["pool_state"], "UNKNOWN")

    def test_abundant_is_intelligence_first(self):
        runtime = {"quota_snapshots": {"opencode-go": snapshot(14)}}
        selected = fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "k3")
        self.assertEqual(selected["routing"]["pool_state"], "ABUNDANT")

    def test_does_not_step_down_before_conserve(self):
        runtime = {"quota_snapshots": {"opencode-go": snapshot(60)}}
        selected = fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "k3")
        self.assertEqual(selected["routing"]["pool_state"], "HEALTHY")

    def test_conserve_steps_down_one_band(self):
        runtime = {"quota_snapshots": {"opencode-go": snapshot(80)}}
        selected = fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "k27")
        self.assertEqual(selected["routing"]["pool_state"], "CONSERVE")

    def test_critical_uses_cheapest_admitted_floor(self):
        runtime = {"quota_snapshots": {"opencode-go": snapshot(95)}}
        selected = fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "flash")

    def test_role_routes_to_task_specific_model(self):
        selected = fleetctl.choose_lane(roster(), {}, "implementation", "write", "text")
        self.assertEqual(selected["lane_id"], "k27")

    def test_capability_is_a_hard_gate(self):
        selected = fleetctl.choose_lane(roster(), {}, "media", "read-only", "audio")
        self.assertEqual(selected["lane_id"], "mimo")

    def test_stale_snapshot_returns_unknown_and_keeps_smart_default(self):
        runtime = {"quota_snapshots": {"opencode-go": snapshot(99, age_seconds=3700)}}
        selected = fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "k3")
        self.assertEqual(selected["routing"]["pool_state"], "UNKNOWN")

    def test_explicit_quota_error_blocks_pool(self):
        runtime = {
            "pool_circuits": {
                "opencode-go": {
                    "until": fleetctl.iso(fleetctl.utc_now() + timedelta(minutes=10)),
                    "limit_name": "weekly",
                }
            }
        }
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")

    def test_stale_trusted_100_percent_stays_exhausted_until_reset(self):
        runtime = {"quota_snapshots": {"opencode-go": snapshot(100, age_seconds=7300)}}
        state, evidence = fleetctl.current_pool_state(runtime, "opencode-go")
        self.assertEqual(state, "EXHAUSTED")
        self.assertEqual(evidence["confidence"], "stale")
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")

    def _lease(self, lane_id, pid=None):
        return {
            "token": f"t-{lane_id}-{pid or os.getpid()}",
            "lane_id": lane_id,
            "pool": "opencode-go",
            "pid": pid or os.getpid(),
            "created_at": fleetctl.iso(),
            "expires_at": fleetctl.iso(fleetctl.utc_now() + timedelta(minutes=10)),
        }

    def test_busy_top_lane_steps_down_instead_of_failing(self):
        """A lane at capacity must not block the task: route to the next candidate.

        Regression: choose_lane ignored leases, so it kept
        returning a full lane and acquire_lease then hard-failed the run.
        """
        runtime = {
            "quota_snapshots": {"opencode-go": snapshot(14)},
            "leases": [self._lease("k3")],
        }
        selected = fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "k27")

    def test_free_slot_on_a_multi_slot_lane_still_routes_there(self):
        """Capacity awareness must not step down while a slot is genuinely free."""
        runtime = {
            "quota_snapshots": {"opencode-go": snapshot(14)},
            "leases": [self._lease("mimo")],
        }
        selected = fleetctl.choose_lane(roster(), runtime, "media", "read-only", "audio")
        self.assertEqual(selected["lane_id"], "mimo")

    def test_dead_lease_holder_frees_the_lane(self):
        """A lease whose process is gone must not keep a lane reserved."""
        runtime = {
            "quota_snapshots": {"opencode-go": snapshot(14)},
            "leases": [self._lease("k3", pid=2_000_000_000)],
        }
        selected = fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "k3")

    def test_all_candidates_busy_raises_with_capacity_reason(self):
        """When every eligible lane is full, say so instead of picking a full one."""
        runtime = {
            "quota_snapshots": {"opencode-go": snapshot(14)},
            "leases": [self._lease("k3"), self._lease("k27")]
            + [dict(self._lease("flash"), token=f"f{i}") for i in range(4)],
        }
        with self.assertRaises(fleetctl.FleetError) as caught:
            fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")
        self.assertIn("at capacity", str(caught.exception))

    def test_trusted_100_percent_snapshot_exhausts_pool(self):
        runtime = {"quota_snapshots": {"opencode-go": snapshot(100)}}
        state, evidence = fleetctl.current_pool_state(runtime, "opencode-go")
        self.assertEqual(state, "EXHAUSTED")
        self.assertEqual(evidence["bottleneck_used_percent"], 100)
        self.assertIn("rolling_5h", evidence["limit_names"])
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")


class FleetTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_leases_enforce_frontier_cap(self):
        token = fleetctl.acquire_lease(self.root, roster(), "k3", 60)
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_lease(self.root, roster(), "k3", 60)
        fleetctl.release_lease(self.root, token)
        self.assertTrue(fleetctl.acquire_lease(self.root, roster(), "k3", 60))

    def test_metered_pool_daily_cap_refuses_when_reached(self):
        overlay = metered_overlay(daily_cap=1.0)
        ledger = self.root / "runs.jsonl"
        now = fleetctl.iso(fleetctl.utc_now())
        ledger.write_text(
            json.dumps(
                {"quota_pool": "gemini-metered", "ended_at": now, "cost": {"estimated_usd": 0.40}}
            )
            + "\n",
            encoding="utf-8",
        )
        token = fleetctl.acquire_lease(self.root, overlay, "gemini-metered-flash-lite", 60)
        self.assertTrue(token)
        fleetctl.release_lease(self.root, token)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"quota_pool": "gemini-metered", "ended_at": now, "cost": {"estimated_usd": 0.75}}
                )
                + "\n"
            )
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_lease(self.root, overlay, "gemini-metered-flash-lite", 60)

    def test_metered_cap_counts_pre_schema_lines_by_lane(self):
        overlay = metered_overlay(daily_cap=1.0)
        ledger = self.root / "runs.jsonl"
        now = fleetctl.iso(fleetctl.utc_now())
        ledger.write_text(
            json.dumps(
                {"lane_id": "gemini-metered-flash-lite", "ended_at": now, "cost": {"estimated_usd": 1.5}}
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_lease(self.root, overlay, "gemini-metered-flash-lite", 60)

    def test_no_cap_pool_is_never_spend_blocked(self):
        # opencode-go has no daily_usd_cap; a large ledger must not block it
        ledger = self.root / "runs.jsonl"
        now = fleetctl.iso(fleetctl.utc_now())
        ledger.write_text(
            json.dumps(
                {"quota_pool": "opencode-go", "ended_at": now, "cost": {"estimated_usd": 999.0}}
            )
            + "\n",
            encoding="utf-8",
        )
        token = fleetctl.acquire_lease(self.root, roster(), "flash", 60)
        self.assertTrue(token)

    def test_exhausted_snapshot_blocks_explicit_lane_lease(self):
        fleetctl.atomic_json(
            self.root / "runtime.json",
            {"quota_snapshots": {"opencode-go": snapshot(100)}},
        )
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_lease(self.root, roster(), "flash", 60)

    def test_record_keeps_cost_semantics_honest(self):
        events = self.root / "events.jsonl"
        events.write_text(
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "s1",
                    "part": {
                        "reason": "stop",
                        "tokens": {
                            "total": 10,
                            "input": 4,
                            "output": 2,
                            "reasoning": 1,
                            "cache": {"read": 3, "write": 0},
                        },
                        "cost": 0.25,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = fleetctl.record_run(self.root, roster(), "k3", events, None, 0, None)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tokens"]["reasoning"], 1)
        self.assertEqual(result["context_profile"], "unspecified")
        self.assertEqual(result["agent_profile"], "unspecified")
        self.assertFalse(result["cost"]["authoritative_for_go_quota"])
        self.assertNotIn("prompt", result)

        research = fleetctl.record_run(
            self.root,
            roster(),
            "k3",
            events,
            None,
            0,
            None,
            "lean",
            "fleet-research",
        )
        self.assertEqual(research["agent_profile"], "fleet-research")
        self.assertEqual(research["context_profile"], "lean")
        ledger = fleetctl.run_ledger_usage(self.root)
        rolling = ledger["windows"]["rolling_5h"]
        self.assertEqual(rolling["fleet-research"]["runs"], 1)
        self.assertEqual(rolling["fleet-research"]["tokens"]["total"], 10)
        self.assertEqual(rolling["unspecified"]["runs"], 1)

    def test_quota_record_opens_pool_circuit(self):
        stderr = self.root / "stderr"
        stderr.write_text('GoUsageLimitError limitName="weekly" retry-after: 120', encoding="utf-8")
        result = fleetctl.record_run(self.root, roster(), "k3", None, stderr, 1, None)
        self.assertEqual(result["error"]["type"], "quota")
        runtime = json.loads((self.root / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["pool_circuits"]["opencode-go"]["limit_name"], "weekly")

    def test_local_database_reports_tokens_but_never_percent(self):
        db = self.root / "opencode.db"
        connection = sqlite3.connect(db)
        connection.executescript(
            """
            CREATE TABLE message (id text, data text);
            CREATE TABLE part (message_id text, time_created integer, data text);
            """
        )
        now = int(fleetctl.time.time() * 1000)
        connection.execute(
            "INSERT INTO message VALUES (?, ?)",
            ("m", json.dumps({"providerID": "opencode-go"})),
        )
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?)",
            (
                "m",
                now,
                json.dumps(
                    {
                        "type": "step-finish",
                        "cost": 0.5,
                        "tokens": {
                            "total": 12,
                            "input": 5,
                            "output": 2,
                            "reasoning": 1,
                            "cache": {"read": 4, "write": 0},
                        },
                    }
                ),
            ),
        )
        connection.commit()
        connection.close()
        observed = fleetctl.local_observed(db, now)
        self.assertEqual(observed["windows"]["rolling_5h"]["tokens"]["reasoning"], 1)
        self.assertFalse(observed["authoritative_for_go_quota"])
        self.assertNotIn("remaining_percent", json.dumps(observed))


class FleetStateCleanupTests(unittest.TestCase):
    def runtime_with_pid(self, pid=...):
        expiry = fleetctl.iso(fleetctl.utc_now() + timedelta(hours=1))
        lease = {"expires_at": expiry}
        claim = {"expires_at": expiry}
        if pid is not ...:
            lease["pid"] = pid
            claim["pid"] = pid
        return {"leases": [lease], "path_claims": [claim]}

    def clean(self, runtime):
        fleetctl.clean_leases(runtime)
        fleetctl.clean_path_claims(runtime)

    def test_dead_pid_is_reaped(self):
        # `true` is /usr/bin/true on Debian but /bin/true on Alpine, so resolve it.
        child = subprocess.Popen([shutil.which("true") or "/bin/true"])
        child.wait()
        runtime = self.runtime_with_pid(child.pid)
        self.clean(runtime)
        self.assertEqual(runtime["leases"], [])
        self.assertEqual(runtime["path_claims"], [])

    def test_live_pid_is_kept(self):
        runtime = self.runtime_with_pid(os.getpid())
        self.clean(runtime)
        self.assertEqual(len(runtime["leases"]), 1)
        self.assertEqual(len(runtime["path_claims"]), 1)

    def test_absent_pid_is_kept(self):
        runtime = self.runtime_with_pid()
        self.clean(runtime)
        self.assertEqual(len(runtime["leases"]), 1)
        self.assertEqual(len(runtime["path_claims"]), 1)


# A stand-in for the real `codexbar` binary. Emits fresh timestamps so the
# freshness guard reads "direct", and the codex branch prepends a non-JSON
# notify line to exercise noise-robust parsing.
FAKE_CODEXBAR = """#!/usr/bin/env python3
import sys, json, datetime as dt
argv = sys.argv
p = argv[argv.index("--provider") + 1]
now = dt.datetime.now(dt.timezone.utc)
def iso(d): return d.isoformat().replace("+00:00", "Z")
if p == "claude":
    print(json.dumps([{"provider": "claude", "source": "claude", "usage": {
        "primary": {"usedPercent": 19, "resetsAt": iso(now + dt.timedelta(hours=5))},
        "secondary": {"usedPercent": 77, "resetsAt": iso(now + dt.timedelta(days=3))},
        "tertiary": None,
        "extraRateWindows": [{"window": {"usedPercent": 30, "resetsAt": iso(now + dt.timedelta(days=3))}, "id": "claude-weekly-scoped-fable", "title": "Fable only"}],
        "updatedAt": iso(now)}}]))
elif p == "codex":
    print("[codex notify] remoteControl/status/changed")
    print(json.dumps([{"provider": "codex", "source": "oauth", "usage": {
        "primary": None,
        "secondary": {"usedPercent": 4, "resetsAt": iso(now + dt.timedelta(days=6))},
        "tertiary": None,
        "extraRateWindows": [{"window": {"usedPercent": 0, "resetsAt": iso(now + dt.timedelta(days=6))}, "id": "codex-spark-weekly"}],
        "updatedAt": iso(now)}}]))
elif p == "opencodego":
    print(json.dumps([{"provider": "opencodego", "source": "web", "usage": {
        "primary": {"usedPercent": 5, "resetsAt": iso(now + dt.timedelta(hours=5))},
        "secondary": {"usedPercent": 17, "resetsAt": iso(now + dt.timedelta(days=6))},
        "tertiary": {"usedPercent": 8, "resetsAt": iso(now + dt.timedelta(days=29))},
        "updatedAt": iso(now)}}]))
else:
    print(json.dumps([{"source": "auto", "provider": p, "error": {"code": 1, "message": "Not logged in", "kind": "provider"}}]))
"""


class CodexbarReaderTests(unittest.TestCase):
    def _fake_codexbar(self, tmp):
        script = Path(tmp) / "fake_codexbar"
        script.write_text(FAKE_CODEXBAR, encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_parse_codexbar_clean_array(self):
        obj = fleetctl._parse_codexbar('[{"provider":"claude","usage":{}}]')
        self.assertEqual(obj["provider"], "claude")

    def test_parse_codexbar_skips_leading_noise(self):
        out = '[codex notify] status/changed\n[{"provider":"codex","usage":{}}]'
        self.assertEqual(fleetctl._parse_codexbar(out)["provider"], "codex")

    def test_parse_codexbar_returns_none_on_garbage(self):
        self.assertIsNone(fleetctl._parse_codexbar("not json at all"))

    def test_codexbar_observed_maps_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = fleetctl.codexbar_observed("claude", binary=self._fake_codexbar(tmp))
            self.assertTrue(obs["available"])
            self.assertEqual(obs["source"], "codexbar")
            self.assertEqual(obs["windows"]["secondary"]["used_percent"], 77)
            self.assertIn("claude-weekly-scoped-fable", obs["windows"])
            self.assertNotIn("tertiary", obs["windows"])  # null slot dropped

    def test_codexbar_observed_handles_leading_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = fleetctl.codexbar_observed("codex", binary=self._fake_codexbar(tmp))
            self.assertTrue(obs["available"])
            self.assertEqual(obs["windows"]["secondary"]["used_percent"], 4)
            self.assertNotIn("primary", obs["windows"])  # null primary dropped

    def test_codexbar_observed_unavailable_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs = fleetctl.codexbar_observed("gemini", binary=self._fake_codexbar(tmp))
            self.assertFalse(obs["available"])
            self.assertIn("Not logged in", obs["reason"])

    def test_codexbar_snapshot_aliases_opencodego_to_pool(self):
        # CodexBar's "opencodego" lane must feed the fleet's "opencode-go" pool,
        # replacing the retired quota-sync.sh console scrape.
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_codexbar(tmp)
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            orig = fleetctl.CODEXBAR_BIN
            fleetctl.CODEXBAR_BIN = fake
            try:
                args = argparse.Namespace(provider="opencodego", timeout=5, json=True)
                results = fleetctl.codexbar_snapshot_command(args, state_dir)
            finally:
                fleetctl.CODEXBAR_BIN = orig
            runtime = fleetctl.load_json(state_dir / "runtime.json", {})
            snaps = runtime.get("quota_snapshots", {})
            self.assertIn("opencode-go", snaps)           # aliased to the pool key
            self.assertNotIn("opencodego", snaps)         # not the raw provider name
            self.assertEqual(snaps["opencode-go"]["source"], "codexbar")
            self.assertIn("opencode-go", results)
            self.assertNotEqual(results["opencode-go"]["state"], "UNKNOWN")  # windows computed

    def test_codexbar_observed_binary_missing(self):
        obs = fleetctl.codexbar_observed("claude", binary="/no/such/codexbar")
        self.assertFalse(obs["available"])
        self.assertIn("not found", obs["reason"])

    def test_codexbar_snapshot_command_writes_and_bands(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = self._fake_codexbar(tmp)
            state_dir = Path(tmp) / "state"
            original = fleetctl.CODEXBAR_BIN
            fleetctl.CODEXBAR_BIN = fake
            try:
                args = argparse.Namespace(provider="claude,codex,gemini", timeout=10, json=True)
                results = fleetctl.codexbar_snapshot_command(args, state_dir)
            finally:
                fleetctl.CODEXBAR_BIN = original
            self.assertEqual(results["claude"]["band"], "conserve")
            self.assertEqual(results["codex"]["band"], "quality_first")
            self.assertFalse(results["gemini"]["available"])
            runtime = json.loads((state_dir / "runtime.json").read_text())
            self.assertEqual(runtime["quota_snapshots"]["claude"]["source"], "codexbar")
            self.assertNotIn("opencode-go", runtime["quota_snapshots"])  # untouched


# A codexbar stand-in that records every invocation, so a test can assert the
# refresh did NOT shell out. @@AGE@@ backdates the reported updatedAt, which is
# how the "never clobber a newer stored snapshot" race is exercised.
COUNTING_CODEXBAR = """#!/usr/bin/env python3
import sys, json, datetime as dt
with open("@@CALLS@@", "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
if "@@FAIL@@":
    print(json.dumps([{"provider": "x", "error": {"code": 1, "message": "Not logged in"}}]))
    raise SystemExit(0)
now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=int("@@AGE@@"))
def iso(d): return d.isoformat().replace("+00:00", "Z")
print(json.dumps([{"provider": "opencodego", "usage": {
    "primary": {"usedPercent": 12, "resetsAt": iso(now + dt.timedelta(hours=5))},
    "updatedAt": iso(now)}}]))
"""


class SnapshotAutoRefreshTests(unittest.TestCase):
    """A stale snapshot is not neutral: past an hour current_pool_state calls the
    pool UNKNOWN and effective_cap clamps every frontier lane to one slot. These
    cover the refresh that removes the manual step, and the guards that keep it
    from becoming a new failure mode of its own."""

    def setUp(self):
        # test_roster_and_fanout sets this process-wide to keep its subprocess
        # tests off the live CodexBar. These tests drive the refresh directly, so
        # they own the variable rather than inheriting whatever imported first.
        self._saved_optout = os.environ.pop("FLEET_NO_AUTO_REFRESH", None)
        self._saved_bin = fleetctl.CODEXBAR_BIN

    def tearDown(self):
        os.environ.pop("FLEET_NO_AUTO_REFRESH", None)
        if self._saved_optout is not None:
            os.environ["FLEET_NO_AUTO_REFRESH"] = self._saved_optout
        fleetctl.CODEXBAR_BIN = self._saved_bin

    def _codexbar(self, tmp, fail=False, age=0):
        calls = Path(tmp) / "calls.log"
        script = Path(tmp) / "counting_codexbar"
        script.write_text(
            COUNTING_CODEXBAR.replace("@@CALLS@@", str(calls))
            .replace("@@FAIL@@", "1" if fail else "")
            .replace("@@AGE@@", str(age)),
            encoding="utf-8",
        )
        script.chmod(0o755)
        fleetctl.CODEXBAR_BIN = str(script)
        return calls

    def _calls(self, calls):
        return len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0

    def _seed(self, state_dir, age_s, used=40, pool="opencode-go", **extra):
        """Merge a snapshot of a given age into the state dir, so several pools
        can be seeded independently."""
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "runtime.json"
        runtime = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        now = fleetctl.utc_now()
        runtime.setdefault("quota_snapshots", {})[pool] = {
            "source": "codexbar",
            "observed_at": fleetctl.iso(now - timedelta(seconds=age_s)),
            "windows": {
                "primary": {
                    "used_percent": used,
                    "reset_at": fleetctl.iso(now + timedelta(hours=5)),
                }
            },
        }
        runtime.update(extra)
        state_path.write_text(json.dumps(runtime), encoding="utf-8")

    def _test_roster(self):
        return roster()

    def _stored(self, state_dir, pool="opencode-go"):
        runtime = json.loads((state_dir / "runtime.json").read_text(encoding="utf-8"))
        return runtime["quota_snapshots"][pool]

    def test_stale_snapshot_is_refreshed_before_it_can_degrade_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._seed(state_dir, age_s=7200)  # past the 3600s UNKNOWN cliff
            before = json.loads((state_dir / "runtime.json").read_text())
            self.assertEqual(
                fleetctl.current_pool_state(before, "opencode-go")[0], "UNKNOWN"
            )
            calls = self._codexbar(tmp)
            outcomes = fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=self._test_roster())
            self.assertEqual(outcomes["opencode-go"], "refreshed")
            self.assertEqual(self._calls(calls), 1)
            after = json.loads((state_dir / "runtime.json").read_text())
            self.assertEqual(fleetctl.current_pool_state(after, "opencode-go")[0], "ABUNDANT")

    def test_fresh_snapshot_is_not_refetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._seed(state_dir, age_s=60)
            calls = self._codexbar(tmp)
            self.assertEqual(fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=self._test_roster()), {})
            self.assertEqual(self._calls(calls), 0)

    def test_unreachable_codexbar_leaves_the_stored_snapshot_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._seed(state_dir, age_s=7200)
            before = self._stored(state_dir)
            fleetctl.CODEXBAR_BIN = "/no/such/codexbar"
            outcomes = fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=self._test_roster())
            self.assertIn("unavailable", outcomes["opencode-go"])
            self.assertEqual(self._stored(state_dir), before)  # honest fallback, not a wipe

    def test_cooldown_stops_a_dead_codexbar_costing_every_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._seed(state_dir, age_s=7200)
            calls = self._codexbar(tmp, fail=True)
            fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=self._test_roster())
            fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=self._test_roster())
            fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=self._test_roster())
            self.assertEqual(self._calls(calls), 1)  # cooldown holds after the failure

    def test_opt_out_env_skips_the_refresh_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._seed(state_dir, age_s=7200)
            calls = self._codexbar(tmp)
            os.environ["FLEET_NO_AUTO_REFRESH"] = "1"
            self.assertEqual(fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=self._test_roster()), {})
            self.assertEqual(self._calls(calls), 0)

    def test_refresh_never_clobbers_a_newer_stored_snapshot(self):
        # The fetch happens outside the lock, so a parallel writer (or a manual
        # `snapshot` override) can land first. Older evidence must not win.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._seed(state_dir, age_s=1800)
            before = self._stored(state_dir)
            self._codexbar(tmp, age=3600)  # CodexBar reports an OLDER observation
            outcomes = fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=self._test_roster())
            self.assertEqual(outcomes["opencode-go"], "kept newer stored snapshot")
            self.assertEqual(self._stored(state_dir), before)

    def test_refresh_leaves_an_explicit_quota_circuit_closed(self):
        # A 429-opened circuit stays authoritative until a human runs
        # codexbar-snapshot; auto-reopening would just hit the same limit again.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            until = fleetctl.iso(fleetctl.utc_now() + timedelta(hours=2))
            self._seed(
                state_dir,
                age_s=7200,
                pool_circuits={"opencode-go": {"until": until, "limit_name": "primary"}},
            )
            self._codexbar(tmp)
            fleetctl.refresh_stale_pools(state_dir, ["opencode-go"], roster=self._test_roster())
            after = json.loads((state_dir / "runtime.json").read_text())
            self.assertIn("opencode-go", after["pool_circuits"])
            self.assertEqual(fleetctl.current_pool_state(after, "opencode-go")[0], "EXHAUSTED")

    def test_metered_web_pools_refresh_less_often_than_the_routing_pool(self):
        # claude/codex/antigravity cost a real dashboard fetch and the claude.ai
        # usage endpoint rate-limits, so they hold a longer TTL. Routing never
        # reads them, so the slower cadence costs nothing that matters.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._seed(state_dir, age_s=1200, pool="claude")
            self._seed(state_dir, age_s=1200)  # opencode-go, same age
            runtime = json.loads((state_dir / "runtime.json").read_text(encoding="utf-8"))
            sources = fleetctl.quota_sources(self._test_roster())
            self.assertTrue(fleetctl.snapshot_needs_refresh(runtime, "opencode-go", sources=sources))
            self.assertFalse(fleetctl.snapshot_needs_refresh(runtime, "claude", sources=sources))
            self.assertLess(
                fleetctl.pool_refresh_ttl("opencode-go", sources=sources), fleetctl.pool_refresh_ttl("claude", sources=sources)
            )
            # Both TTLs stay inside the 3600s window past which a pool reads UNKNOWN.
            self.assertLess(fleetctl.pool_refresh_ttl("claude", sources=sources), 3600)

    def test_metered_pool_is_never_given_a_percentage_snapshot(self):
        # gemini-metered is a paid per-token key limited by a daily USD cap, not a
        # percentage window. Attaching a CodexBar snapshot would assert a quota
        # that does not exist, so it must stay out of the refresh set entirely.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            calls = self._codexbar(tmp)
            self.assertEqual(fleetctl.refresh_stale_pools(state_dir, ["gemini-metered"], roster=self._test_roster()), {})
            self.assertEqual(self._calls(calls), 0)
            self.assertNotIn("gemini-metered", fleetctl.quota_sources(self._test_roster()))

    def test_copilot_pool_is_served_by_codexbar(self):
        # The Student plan bills as an individual plan, which is the single
        # copilot account CodexBar sees; the pool is no longer permanently UNKNOWN.
        self.assertEqual(
            fleetctl.quota_sources(self._test_roster())["github-copilot-student"]["provider"],
            "copilot",
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            calls = self._codexbar(tmp)
            outcomes = fleetctl.refresh_stale_pools(state_dir, ["github-copilot-student"], roster=self._test_roster())
            self.assertEqual(outcomes["github-copilot-student"], "refreshed")
            self.assertEqual(self._calls(calls), 1)
            runtime = json.loads((state_dir / "runtime.json").read_text(encoding="utf-8"))
            self.assertIn("github-copilot-student", runtime["quota_snapshots"])
            self.assertNotIn("copilot", runtime["quota_snapshots"])  # pool key, not provider

    def test_route_uses_refreshed_quota_not_the_stale_band(self):
        # End-to-end through main(): the stale snapshot says 95% (CRITICAL, so
        # routing floors to the cheapest lane), CodexBar says 12%. Routing must
        # follow the live number.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            overlay = root / "overlay.json"
            overlay.write_text(json.dumps(roster()), encoding="utf-8")
            self._seed(state_dir, age_s=1800, used=95)
            calls = self._codexbar(tmp)
            base = [
                str(MODULE_PATH), "--overlay", str(overlay), "--state-dir", str(state_dir), "route",
            ]
            env = {**os.environ, "CODEXBAR_BIN": fleetctl.CODEXBAR_BIN}
            env.pop("FLEET_NO_AUTO_REFRESH", None)
            stale = subprocess.run([*base, "--no-refresh"], capture_output=True, text=True, env=env)
            self.assertEqual(stale.returncode, 0, stale.stderr)
            self.assertEqual(stale.stdout.strip(), "flash")  # CRITICAL floor
            self.assertEqual(self._calls(calls), 0)  # opt-out really opted out
            live = subprocess.run(base, capture_output=True, text=True, env=env)
            self.assertEqual(live.returncode, 0, live.stderr)
            self.assertEqual(live.stdout.strip(), "k3")  # quality_first top lane
            self.assertEqual(self._calls(calls), 1)

    def _multi_window_codexbar(self, tmp):
        calls = Path(tmp) / "calls.log"
        script = Path(tmp) / "multi_codexbar"
        code = f"""#!/usr/bin/env python3
import sys, json, datetime as dt
with open({json.dumps(str(calls))}, "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
now = dt.datetime.now(dt.timezone.utc)
def iso(d): return d.isoformat().replace("+00:00", "Z")
print(json.dumps([{{
    "provider": "antigravity",
    "usage": {{
        "primary": {{"usedPercent": 10, "resetsAt": iso(now + dt.timedelta(hours=5))}},
        "secondary": {{"usedPercent": 20, "resetsAt": iso(now + dt.timedelta(days=2))}},
        "extraRateWindows": [
            {{"window": {{"usedPercent": 30, "resetsAt": iso(now + dt.timedelta(hours=5))}}, "id": "antigravity-quota-summary-gemini-5h"}}
        ],
        "updatedAt": iso(now)
    }}
}}]))
"""
        script.write_text(code, encoding="utf-8")
        script.chmod(0o755)
        fleetctl.CODEXBAR_BIN = str(script)
        return calls

    def test_declared_window_subset_is_only_thing_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            self._multi_window_codexbar(tmp)
            roster = {
                "schema_version": 3,
                "quota_pools": {
                    "antigravity-gemini": {
                        "quota_refresh": {
                            "oracle": "codexbar",
                            "provider": "antigravity",
                            "ttl_s": 1800,
                            "windows": ["antigravity-quota-summary-gemini-5h"],
                        }
                    }
                },
            }
            outcomes = fleetctl.refresh_stale_pools(state_dir, ["antigravity-gemini"], roster=roster)
            self.assertEqual(outcomes["antigravity-gemini"], "refreshed")
            stored = self._stored(state_dir, pool="antigravity-gemini")
            self.assertEqual(list(stored["windows"].keys()), ["antigravity-quota-summary-gemini-5h"])

    def test_absent_windows_key_behaves_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            self._multi_window_codexbar(tmp)
            roster = {
                "schema_version": 3,
                "quota_pools": {
                    "antigravity-gemini": {
                        "quota_refresh": {
                            "oracle": "codexbar",
                            "provider": "antigravity",
                            "ttl_s": 1800,
                        }
                    }
                },
            }
            outcomes = fleetctl.refresh_stale_pools(state_dir, ["antigravity-gemini"], roster=roster)
            self.assertEqual(outcomes["antigravity-gemini"], "refreshed")
            stored = self._stored(state_dir, pool="antigravity-gemini")
            self.assertEqual(
                set(stored["windows"].keys()),
                {"primary", "secondary", "antigravity-quota-summary-gemini-5h"},
            )

    def test_window_subset_matching_nothing_keeps_existing_snapshot_and_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._seed(state_dir, age_s=7200, used=40, pool="antigravity-gemini")
            before = self._stored(state_dir, pool="antigravity-gemini")
            self._multi_window_codexbar(tmp)
            roster = {
                "schema_version": 3,
                "quota_pools": {
                    "antigravity-gemini": {
                        "quota_refresh": {
                            "oracle": "codexbar",
                            "provider": "antigravity",
                            "ttl_s": 1800,
                            "windows": ["nonexistent-window"],
                        }
                    }
                },
            }
            outcomes = fleetctl.refresh_stale_pools(state_dir, ["antigravity-gemini"], roster=roster)
            self.assertTrue(outcomes["antigravity-gemini"].startswith("unavailable"))
            self.assertEqual(self._stored(state_dir, pool="antigravity-gemini"), before)

    def test_shared_provider_fetches_once_and_filters_per_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            calls = self._multi_window_codexbar(tmp)
            roster = {
                "schema_version": 3,
                "quota_pools": {
                    "antigravity-gemini": {
                        "quota_refresh": {
                            "oracle": "codexbar",
                            "provider": "antigravity",
                            "ttl_s": 1800,
                            "windows": ["antigravity-quota-summary-gemini-5h"],
                        }
                    },
                    "antigravity-3p": {
                        "quota_refresh": {
                            "oracle": "codexbar",
                            "provider": "antigravity",
                            "ttl_s": 1800,
                            "windows": ["primary", "secondary"],
                        }
                    },
                },
            }
            outcomes = fleetctl.refresh_stale_pools(
                state_dir, ["antigravity-gemini", "antigravity-3p"], roster=roster
            )
            self.assertEqual(outcomes["antigravity-gemini"], "refreshed")
            self.assertEqual(outcomes["antigravity-3p"], "refreshed")
            self.assertEqual(self._calls(calls), 1)
            stored_gemini = self._stored(state_dir, pool="antigravity-gemini")
            self.assertEqual(list(stored_gemini["windows"].keys()), ["antigravity-quota-summary-gemini-5h"])
            stored_3p = self._stored(state_dir, pool="antigravity-3p")
            self.assertEqual(set(stored_3p["windows"].keys()), {"primary", "secondary"})


def cross_pool_roster():
    """The Go lanes plus one lane spending a DIFFERENT budget, as the Antigravity
    lanes do. The second-pool lane is listed last in quality_first and also carries
    the tighter bands, because a pool with headroom is what you want when the
    primary one is under pressure."""
    r = roster()
    r["lanes"].append(
        {
            "lane_id": "agy-gemini",
            "model_key": "gemini-3.6-flash",
            "harness": "agy",
            "provider": "antigravity",
            "selector": "gemini-3.6-flash-high",
            "access_status": "verified",
            "admission_status": "active",
            "allowed_modes": ["read-only", "write"],
            "quota_pool": "antigravity-gemini",
            "quality_tier": "strong",
            "capabilities": {"input": ["text"]},
            "max_parallel": 1,
        }
    )
    # Mirrors the real placement: ranked by quality in quality_first, but FIRST in
    # the tighter bands, because those exist to spare whichever budget is spent.
    d = r["routing"]["roles"]["default"]
    d["quality_first"] = [*d["quality_first"], "agy-gemini"]
    d["conserve"] = ["agy-gemini", *d["conserve"]]
    d["critical"] = ["agy-gemini", *d["critical"]]
    return r


class CrossPoolRoutingTests(unittest.TestCase):
    """Roles may list lanes from different quota pools. Each candidate has to be
    judged against its own budget, or the fleet throws away paid-for headroom at
    the exact moment it needs it."""

    def _pool(self, name, used, age_s=0):
        now = fleetctl.utc_now()
        return {
            name: {
                "source": "codexbar",
                "observed_at": fleetctl.iso(now - timedelta(seconds=age_s)),
                "windows": {
                    "w": {
                        "used_percent": used,
                        "reset_at": fleetctl.iso(now + timedelta(hours=5)),
                    }
                },
            }
        }

    def test_exhausted_primary_pool_still_routes_to_the_other_pool(self):
        # Before: choose_lane raised outright and the other pool was unreachable.
        runtime = {"quota_snapshots": {**self._pool("opencode-go", 100),
                                       **self._pool("antigravity-gemini", 3)}}
        selected = fleetctl.choose_lane(
            cross_pool_roster(), runtime, "default", "read-only", "text"
        )
        self.assertEqual(selected["lane_id"], "agy-gemini")
        self.assertEqual(selected["routing"]["pool"], "antigravity-gemini")
        self.assertEqual(selected["routing"]["pool_state"], "ABUNDANT")

    def test_harness_filter_keeps_a_wrapper_to_lanes_it_can_run(self):
        # Regression: once a role listed lanes from two harnesses, the OpenCode
        # wrapper was handed an agy lane and died with "not an OpenCode lane" at
        # exactly the moment routing stepped down under pressure.
        runtime = {"quota_snapshots": {**self._pool("opencode-go", 97),
                                       **self._pool("antigravity-gemini", 3)}}
        roster_ = cross_pool_roster()
        unfiltered = fleetctl.choose_lane(roster_, runtime, "default", "read-only", "text")
        self.assertEqual(unfiltered["lane_id"], "agy-gemini")  # best available overall
        pinned = fleetctl.choose_lane(
            roster_, runtime, "default", "read-only", "text", harness="opencode"
        )
        self.assertEqual(pinned["harness"], "opencode")
        agy_only = fleetctl.choose_lane(
            roster_, runtime, "default", "read-only", "text", harness="agy"
        )
        self.assertEqual(agy_only["lane_id"], "agy-gemini")

    def test_harness_filter_with_no_match_raises_rather_than_substituting(self):
        runtime = {"quota_snapshots": {**self._pool("opencode-go", 10)}}
        with self.assertRaises(fleetctl.FleetError) as caught:
            fleetctl.choose_lane(
                cross_pool_roster(), runtime, "default", "read-only", "text", harness="nosuch"
            )
        self.assertIn("harness", str(caught.exception))

    def test_candidate_whose_own_pool_is_exhausted_is_skipped(self):
        # Routing must never return a lane acquire_lease would immediately refuse.
        runtime = {"quota_snapshots": {**self._pool("opencode-go", 100),
                                       **self._pool("antigravity-gemini", 100)}}
        with self.assertRaises(fleetctl.FleetError) as caught:
            fleetctl.choose_lane(cross_pool_roster(), runtime, "default", "read-only", "text")
        self.assertIn("exhausted", str(caught.exception))

    def test_lane_capacity_uses_its_own_pool_not_the_primary(self):
        # The primary pool being tight must not clamp a lane funded elsewhere.
        roster_ = cross_pool_roster()
        for lane in roster_["lanes"]:
            if lane["lane_id"] == "agy-gemini":
                lane["max_parallel"] = 2
                lane["quality_tier"] = "frontier"
        runtime = {"quota_snapshots": {**self._pool("opencode-go", 95),
                                       **self._pool("antigravity-gemini", 3)}}
        lane = [x for x in roster_["lanes"] if x["lane_id"] == "agy-gemini"][0]
        primary_state, _ = fleetctl.current_pool_state(runtime, "opencode-go")
        own_state, _ = fleetctl.current_pool_state(runtime, "antigravity-gemini")
        self.assertEqual(primary_state, "CRITICAL")
        self.assertEqual(own_state, "ABUNDANT")
        # Judged by the primary pool a frontier lane clamps to 1; by its own, 2.
        self.assertEqual(fleetctl.effective_cap(lane, primary_state), 1)
        self.assertEqual(fleetctl.effective_cap(lane, own_state), 2)
        self.assertEqual(fleetctl.lane_free_slots(runtime, lane, own_state), 2)


class QuotaSourceRegistryTests(unittest.TestCase):
    """Adding a provider or a pool used to mean editing constants in this file. The
    overlay now declares it, so the engine carries no account-specific pool names
    and a new source is one JSON block."""

    def setUp(self):
        self._saved_optout = os.environ.pop("FLEET_NO_AUTO_REFRESH", None)
        self._saved_bin = fleetctl.CODEXBAR_BIN

    def tearDown(self):
        os.environ.pop("FLEET_NO_AUTO_REFRESH", None)
        if self._saved_optout is not None:
            os.environ["FLEET_NO_AUTO_REFRESH"] = self._saved_optout
        fleetctl.CODEXBAR_BIN = self._saved_bin

    def _codexbar(self, tmp):
        calls = Path(tmp) / "calls.log"
        script = Path(tmp) / "counting_codexbar"
        script.write_text(
            COUNTING_CODEXBAR.replace("@@CALLS@@", str(calls))
            .replace("@@FAIL@@", "")
            .replace("@@AGE@@", "0"),
            encoding="utf-8",
        )
        script.chmod(0o755)
        fleetctl.CODEXBAR_BIN = str(script)
        return calls

    def test_absent_overlay_returns_empty_dict(self):
        self.assertEqual(fleetctl.quota_sources(None), {})
        self.assertEqual(fleetctl.quota_sources({}), {})

    def test_overlay_can_add_a_pool_with_no_code_change(self):
        overlay = {
            "quota_pools": {
                "acme-pool": {"quota_refresh": {"oracle": "codexbar", "provider": "acmecloud", "ttl_s": 900}}
            }
        }
        sources = fleetctl.quota_sources(overlay)
        self.assertEqual(sources["acme-pool"]["provider"], "acmecloud")
        self.assertEqual(fleetctl.pool_refresh_ttl("acme-pool", sources=sources), 900)
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            calls = self._codexbar(tmp)
            outcomes = fleetctl.refresh_stale_pools(state_dir, ["acme-pool"], roster=overlay)
            self.assertEqual(outcomes["acme-pool"], "refreshed")
            self.assertIn("--provider acmecloud", calls.read_text(encoding="utf-8"))

    def test_overlay_null_suppresses_a_builtin_source(self):
        overlay = {"quota_pools": {"claude": {"quota_refresh": None}}}
        sources = fleetctl.quota_sources(overlay)
        self.assertNotIn("claude", sources)  # the declaration removes it
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            calls = self._codexbar(tmp)
            self.assertEqual(
                fleetctl.refresh_stale_pools(state_dir, ["claude"], roster=overlay), {}
            )
            self.assertFalse(calls.exists())

    def test_overlay_ttl_overrides_the_default(self):
        overlay = {
            "quota_pools": {
                "opencode-go": {"quota_refresh": {"oracle": "codexbar", "provider": "opencodego", "ttl_s": 30}}
            }
        }
        sources = fleetctl.quota_sources(overlay)
        now = fleetctl.utc_now()
        runtime = {
            "quota_snapshots": {
                "opencode-go": {
                    "observed_at": fleetctl.iso(now - timedelta(seconds=60)),
                    "windows": {},
                }
            }
        }
        # 60s old: fresh under default, stale under the declared 30s.
        self.assertFalse(fleetctl.snapshot_needs_refresh(runtime, "opencode-go"))
        self.assertTrue(
            fleetctl.snapshot_needs_refresh(runtime, "opencode-go", sources=sources)
        )

    def test_malformed_declaration_falls_back_instead_of_crashing(self):
        for bad in ({"quota_refresh": "yes"}, {"quota_refresh": {"ttl_s": 5}}):
            sources = fleetctl.quota_sources({"quota_pools": {"opencode-go": bad}})
            self.assertNotIn("opencode-go", sources)

    def test_shipped_overlay_declares_every_pool_it_owns(self):
        # Guards drift between the live overlay and the engine defaults: if a
        # declaration is dropped, the pool silently reverts to a code constant.
        # The overlay shipped in THIS repo, not whatever the current machine happens to
        # have in its own config: the assertion is about the config we ship, and reading the
        # live path made the test pass or fail on one user's private setup.
        overlay_path = Path(__file__).resolve().parents[1] / "examples" / "access-overlay.example.json"
        if not overlay_path.exists():
            self.skipTest("no shipped overlay")
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        pools = overlay.get("quota_pools") or {}
        for pool in pools:
            self.assertIn(
                "quota_refresh", pools[pool], f"{pool} has no declared quota source"
            )
        sources = fleetctl.quota_sources(overlay)
        self.assertEqual(sources["opencode-go"]["oracle"], "codexbar")
        self.assertEqual(sources["opencode-go"]["provider"], "opencodego")
        self.assertEqual(sources["github-copilot-student"]["provider"], "copilot")
        self.assertNotIn("gemini-metered", sources)


def copilot_overlay():
    return {
        "schema_version": 3,
        "quota_pools": {"github-copilot-student": {"monthly_ai_credits": 200}},
        "routing": {"roles": {}},
        "lanes": [
            {
                "lane_id": "github-copilot-student-auto",
                "model_key": "github-copilot-auto",
                "harness": "copilot",
                "provider": "github-copilot",
                "selector": "auto",
                "access_status": "verified",
                "admission_status": "active",
                "allowed_modes": ["read-only"],
                "quota_pool": "github-copilot-student",
                "quality_tier": "observer",
                "capabilities": {"input": ["text"]},
                "max_parallel": 1,
            }
        ],
    }


class CopilotPoolAdmissionTests(unittest.TestCase):
    """With no snapshot the pool read UNKNOWN, so a fully-consumed Copilot
    allowance still admitted work and the run only failed at the provider. A real
    CodexBar snapshot makes the pool refuse it the same way every other pool does."""

    def test_exhausted_copilot_allowance_now_refuses_the_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            now = fleetctl.utc_now()
            runtime = {
                "quota_snapshots": {
                    "github-copilot-student": {
                        "source": "codexbar",
                        "observed_at": fleetctl.iso(now),
                        "windows": {
                            "primary": {
                                "used_percent": 100,
                                "reset_at": fleetctl.iso(now + timedelta(days=7)),
                            }
                        },
                    }
                }
            }
            (state_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
            with self.assertRaises(fleetctl.FleetError) as caught:
                fleetctl.acquire_lease(
                    state_dir, copilot_overlay(), "github-copilot-student-auto", 60
                )
            self.assertIn("exhausted", str(caught.exception))

    def test_without_a_snapshot_the_same_lane_was_admitted(self):
        # Pins the gap this closed: absent evidence used to read as permission.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            runtime = json.loads(
                (state_dir / "runtime.json").read_text(encoding="utf-8")
            ) if (state_dir / "runtime.json").exists() else {}
            self.assertEqual(
                fleetctl.current_pool_state(runtime, "github-copilot-student")[0], "UNKNOWN"
            )
            token = fleetctl.acquire_lease(
                state_dir, copilot_overlay(), "github-copilot-student-auto", 60
            )
            self.assertTrue(token)


class MeteredPoolReportingTests(unittest.TestCase):
    """A metered pool has no percentage to report, so `usage` reported nothing at
    all about it: you could not see you were near the daily cap until a run was
    refused. These cover the cap/spend/remaining surface that replaced that."""

    def _ledger(self, state_dir, *costs):
        state_dir.mkdir(parents=True, exist_ok=True)
        now = fleetctl.utc_now()
        lines = [
            json.dumps(
                {
                    "lane_id": "gemini-metered-flash-lite",
                    "quota_pool": "gemini-metered",
                    "ended_at": fleetctl.iso(now),
                    "cost": {"estimated_usd": cost},
                }
            )
            for cost in costs
        ]
        (state_dir / "runs.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_reports_cap_spend_and_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._ledger(state_dir, 0.25, 0.10)
            metered = fleetctl.metered_pool_usage(state_dir, metered_overlay(daily_cap=1.0))
            info = metered["gemini-metered"]
            self.assertEqual(info["daily_usd_cap"], 1.0)
            self.assertAlmostEqual(info["estimated_spent_usd_today"], 0.35)
            self.assertAlmostEqual(info["estimated_remaining_usd"], 0.65)
            self.assertEqual(info["admission"], "open")
            self.assertFalse(info["has_percentage_quota"])

    def test_admission_flips_to_refused_at_the_cap(self):
        # Must agree with acquire_lease, which refuses once spend reaches the cap.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self._ledger(state_dir, 0.9, 0.2)
            overlay = metered_overlay(daily_cap=1.0)
            info = fleetctl.metered_pool_usage(state_dir, overlay)["gemini-metered"]
            self.assertEqual(info["admission"], "refused")
            self.assertEqual(info["estimated_remaining_usd"], 0.0)  # never negative
            with self.assertRaises(fleetctl.FleetError):
                fleetctl.acquire_lease(state_dir, overlay, "gemini-metered-flash-lite", 60)

    def test_pool_without_a_cap_is_not_reported_as_metered(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            self.assertEqual(fleetctl.metered_pool_usage(state_dir, roster()), {})

    def test_missing_roster_degrades_quietly(self):
        # `usage` predates needing an overlay; a broken one must not take it down.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            self.assertEqual(fleetctl.metered_pool_usage(state_dir, None), {})

    def test_usage_json_carries_the_metered_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            overlay = root / "overlay.json"
            overlay.write_text(json.dumps(metered_overlay(daily_cap=1.0)), encoding="utf-8")
            self._ledger(state_dir, 0.5)
            result = subprocess.run(
                [
                    str(MODULE_PATH), "--overlay", str(overlay), "--state-dir", str(state_dir),
                    "--db", str(root / "missing.db"), "usage", "--json", "--no-refresh",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "FLEET_NO_AUTO_REFRESH": "1"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertAlmostEqual(
                payload["metered_pools"]["gemini-metered"]["estimated_spent_usd_today"], 0.5
            )


class PathClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = tempfile.TemporaryDirectory()
        self.a = str(Path(self.store.name) / "repo-a")
        self.b = str(Path(self.store.name) / "repo-b")
        Path(self.a).mkdir()
        Path(self.b).mkdir()

    def tearDown(self):
        self.tmp.cleanup()
        self.store.cleanup()

    def test_two_concurrent_writers_conflict_not_clobber(self):
        fleetctl.acquire_path_claim(self.root, [self.a], "campaign-1", 60)
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_path_claim(self.root, [self.a], "campaign-2", 60)

    def test_nested_path_overlap_conflicts_both_directions(self):
        nested = str(Path(self.a) / "sub" / "store")
        fleetctl.acquire_path_claim(self.root, [self.a], "campaign-1", 60)
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_path_claim(self.root, [nested], "campaign-2", 60)
        fleetctl.release_path_claim(
            self.root, fleetctl.list_path_claims(self.root)[0]["token"]
        )
        fleetctl.acquire_path_claim(self.root, [nested], "campaign-2", 60)
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_path_claim(self.root, [self.a], "campaign-3", 60)

    def test_same_owner_still_conflicts(self):
        fleetctl.acquire_path_claim(self.root, [self.a], "campaign-1", 60)
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_path_claim(self.root, [self.a], "campaign-1", 60)

    def test_disjoint_paths_coexist(self):
        fleetctl.acquire_path_claim(self.root, [self.a], "campaign-1", 60)
        fleetctl.acquire_path_claim(self.root, [self.b], "campaign-2", 60)
        self.assertEqual(len(fleetctl.list_path_claims(self.root)), 2)

    def test_release_frees_the_path(self):
        token = fleetctl.acquire_path_claim(self.root, [self.a], "campaign-1", 60)
        fleetctl.release_path_claim(self.root, token)
        self.assertTrue(fleetctl.acquire_path_claim(self.root, [self.a], "campaign-2", 60))

    def test_expired_claim_is_reaped(self):
        fleetctl.acquire_path_claim(self.root, [self.a], "campaign-1", 0)
        self.assertTrue(fleetctl.acquire_path_claim(self.root, [self.a], "campaign-2", 60))

    def test_empty_paths_rejected(self):
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.acquire_path_claim(self.root, [], "campaign-1", 60)


class OraclePluggableTests(unittest.TestCase):
    def setUp(self):
        self._orig_optout = os.environ.get("FLEET_NO_AUTO_REFRESH")
        os.environ["FLEET_NO_AUTO_REFRESH"] = "1"

    def tearDown(self):
        if self._orig_optout is None:
            os.environ.pop("FLEET_NO_AUTO_REFRESH", None)
        else:
            os.environ["FLEET_NO_AUTO_REFRESH"] = self._orig_optout

    def test_command_adapter_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_script = Path(tmp) / "cmd.py"
            cmd_script.write_text(
                "import sys, json\n"
                "if '--fail' in sys.argv:\n"
                "    sys.exit(1)\n"
                "print(json.dumps({'available': True, 'windows': {'w1': {'used_percent': 50, 'reset_at': '2026-07-26T12:00:00Z'}}}))\n"
            )
            cfg_success = {"oracle": "command", "command": [sys.executable, str(cmd_script)]}
            res_success = fleetctl.oracle_command(cfg_success, timeout=5)
            self.assertTrue(res_success["available"])
            self.assertEqual(res_success["windows"]["w1"]["used_percent"], 50)

            cfg_fail = {"oracle": "command", "command": [sys.executable, str(cmd_script), "--fail"]}
            res_fail = fleetctl.oracle_command(cfg_fail, timeout=5)
            self.assertFalse(res_fail["available"])
            self.assertIn("exited with code 1", res_fail["reason"])

    def test_file_adapter_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            f_path = Path(tmp) / "quota.json"
            f_path.write_text(
                json.dumps(
                    {
                        "available": True,
                        "windows": {"w1": {"used_percent": 30, "reset_at": "2026-07-26T12:00:00Z"}},
                    }
                )
            )
            res_success = fleetctl.oracle_file({"oracle": "file", "path": str(f_path)})
            self.assertTrue(res_success["available"])
            self.assertEqual(res_success["windows"]["w1"]["used_percent"], 30)

            res_missing = fleetctl.oracle_file({"oracle": "file", "path": str(Path(tmp) / "no_file.json")})
            self.assertFalse(res_missing["available"])
            self.assertIn("not found", res_missing["reason"])

    def test_http_adapter_success_and_failure(self):
        success_data = json.dumps(
            {
                "available": True,
                "windows": {"w1": {"used_percent": 15, "reset_at": "2026-07-26T12:00:00Z"}},
            }
        ).encode("utf-8")

        class MockResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return success_data

        def mock_urlopen(req, timeout=5):
            if "fail" in req.full_url:
                raise urllib.error.URLError("connection refused")
            if req.headers.get("Authorization") != "Bearer secret123":
                raise urllib.error.URLError("unauthorized")
            return MockResp()

        os.environ["TEST_TOKEN_ENV"] = "secret123"
        try:
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                res_success = fleetctl.oracle_http(
                    {"oracle": "http", "url": "https://example.test/ok", "token_env": "TEST_TOKEN_ENV"}
                )
                self.assertTrue(res_success["available"])
                self.assertEqual(res_success["windows"]["w1"]["used_percent"], 15)

                res_fail = fleetctl.oracle_http({"oracle": "http", "url": "https://example.test/fail"})
                self.assertFalse(res_fail["available"])
                self.assertIn("HTTP GET failed", res_fail["reason"])
        finally:
            os.environ.pop("TEST_TOKEN_ENV", None)

    def test_http_oracle_refuses_non_web_schemes(self):
        """urlopen also speaks file:// and ftp://, so an unguarded url would turn this
        adapter into an arbitrary local-file reader for anyone who can supply a config."""
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret.json"
            secret.write_text(
                json.dumps(
                    {"available": True, "windows": {"w": {"used_percent": 1, "reset_at": "2026-07-26T12:00:00Z"}}}
                ),
                encoding="utf-8",
            )
            urls = (f"file://{secret}", "ftp://example.test/usage.json")
        for url in urls:
            result = fleetctl.oracle_http({"oracle": "http", "url": url})
            self.assertFalse(result["available"], f"{url} must be refused")
            self.assertIn("unsupported url scheme", result["reason"])

    def test_http_oracle_refuses_to_send_a_token_over_plaintext(self):
        """A bearer token on a plain-http url would cross the wire in clear text, and
        urllib replays headers across redirects. Refuse rather than leak it."""
        os.environ["TEST_TOKEN_ENV"] = "secret123"
        try:
            with patch("urllib.request.urlopen") as opener:
                result = fleetctl.oracle_http(
                    {"oracle": "http", "url": "http://example.test/usage", "token_env": "TEST_TOKEN_ENV"}
                )
            self.assertFalse(result["available"])
            self.assertIn("not https", result["reason"])
            opener.assert_not_called()
        finally:
            os.environ.pop("TEST_TOKEN_ENV", None)

    def test_oracle_key_honoured(self):
        overlay = {
            "quota_pools": {
                "custom-pool": {
                    "quota_refresh": {
                        "oracle": "file",
                        "path": "/tmp/fake.json",
                        "ttl_s": 900,
                    }
                }
            }
        }
        sources = fleetctl.quota_sources(overlay)
        self.assertIn("custom-pool", sources)
        self.assertEqual(sources["custom-pool"]["oracle"], "file")
        self.assertEqual(sources["custom-pool"]["path"], "/tmp/fake.json")

    def test_unknown_oracle_skipped(self):
        overlay = {
            "quota_pools": {
                "bad-pool": {
                    "quota_refresh": {
                        "oracle": "bogus_oracle_name",
                    }
                }
            }
        }
        sources = fleetctl.quota_sources(overlay)
        self.assertNotIn("bad-pool", sources)

    def test_quota_sources_empty_with_no_overlay(self):
        self.assertEqual(fleetctl.quota_sources(None), {})
        self.assertEqual(fleetctl.quota_sources({}), {})

    def test_dedupe_fires_once_for_shared_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter_file = Path(tmp) / "counter.txt"
            counter_file.write_text("0")
            cmd_script = Path(tmp) / "shared_cmd.py"
            cmd_script.write_text(
                "import sys, json, pathlib\n"
                f"c = pathlib.Path({json.dumps(str(counter_file))})\n"
                "val = int(c.read_text()) + 1\n"
                "c.write_text(str(val))\n"
                "print(json.dumps({'available': True, 'windows': {'w1': {'used_percent': 10, 'reset_at': '2026-07-26T12:00:00Z'}}}))\n"
            )
            overlay = {
                "quota_pools": {
                    "pool-a": {
                        "quota_refresh": {
                            "oracle": "command",
                            "command": [sys.executable, str(cmd_script)],
                            "ttl_s": 600,
                        }
                    },
                    "pool-b": {
                        "quota_refresh": {
                            "oracle": "command",
                            "command": [sys.executable, str(cmd_script)],
                            "ttl_s": 600,
                        }
                    },
                }
            }
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            os.environ.pop("FLEET_NO_AUTO_REFRESH", None)
            try:
                outcomes = fleetctl.refresh_stale_pools(state_dir, ["pool-a", "pool-b"], roster=overlay)
                self.assertEqual(outcomes["pool-a"], "refreshed")
                self.assertEqual(outcomes["pool-b"], "refreshed")
                self.assertEqual(counter_file.read_text(), "1")
            finally:
                os.environ["FLEET_NO_AUTO_REFRESH"] = "1"

    def test_non_codexbar_pool_appears_in_show_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            runtime = {
                "quota_snapshots": {
                    "custom-pool": {
                        "available": True,
                        "source": "custom_script",
                        # Relative to now, not fixed: hardcoded stamps aged past
                        # the one-hour staleness threshold and the test rotted to
                        # UNKNOWN on its own, two days after it was written.
                        "observed_at": fleetctl.iso(),
                        "windows": {
                            "w1": {
                                "used_percent": 25,
                                "reset_at": fleetctl.iso(
                                    fleetctl.utc_now() + timedelta(hours=5)
                                ),
                                "window_minutes": 300,
                            }
                        },
                    }
                }
            }
            (state_dir / "runtime.json").write_text(json.dumps(runtime))
            db_path = Path(tmp) / "telemetry.db"
            db_path.touch()

            import io
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                fleetctl.show_usage(state_dir, db_path, as_json=True)
            out_json = json.loads(buf.getvalue())
            self.assertIn("quota_pools", out_json)
            self.assertIn("custom-pool", out_json["quota_pools"])
            self.assertEqual(out_json["quota_pools"]["custom-pool"]["source"], "custom_script")

            buf_text = io.StringIO()
            with patch("sys.stdout", buf_text):
                fleetctl.show_usage(state_dir, db_path, as_json=False)
            text_val = buf_text.getvalue()
            self.assertIn("custom-pool (custom_script): ABUNDANT", text_val)


def _runtime_with_window(used_percent, seconds_to_reset, window_minutes=300, pool="claude"):
    """A one-window snapshot observed now, resetting in `seconds_to_reset`."""
    return {
        "quota_snapshots": {
            pool: {
                "available": True,
                "source": "codexbar",
                "observed_at": fleetctl.iso(),
                "windows": {
                    "secondary": {
                        "used_percent": used_percent,
                        "reset_at": fleetctl.iso(
                            fleetctl.utc_now() + timedelta(seconds=seconds_to_reset)
                        ),
                        "window_minutes": window_minutes,
                    }
                },
            }
        }
    }


class QuotaPolicyTests(unittest.TestCase):
    """The clock, the three policies, and the guarantee that measurement is never gated."""

    def setUp(self):
        for key in ("FLEET_QUOTA_POLICY", "FLEET_IGNORE_QUOTA"):
            os.environ.pop(key, None)
        fleetctl.resolve_quota_policy(refresh=True)

    def tearDown(self):
        for key in ("FLEET_QUOTA_POLICY", "FLEET_IGNORE_QUOTA"):
            os.environ.pop(key, None)
        fleetctl._POLICY_CACHE = None

    def _policy(self, value):
        os.environ["FLEET_QUOTA_POLICY"] = value

    # --- the surplus test, both directions ---------------------------------

    def test_surplus_when_the_burn_rate_cannot_exhaust_the_window(self):
        """Barely-touched pool, 1h left on a 5h window: it will expire unused.
        The old elapsed-window proxy throttled this, which was wrong."""
        verdict = fleetctl.window_surplus(
            {"used_percent": 20, "seconds_to_reset": 3600, "window_minutes": 300}
        )
        self.assertTrue(verdict["surplus"])
        self.assertEqual(verdict["basis"], "average_burn")
        self.assertLess(verdict["projected_used_percent_at_reset"], 100)

    def test_no_surplus_when_the_same_clock_carries_a_burn_that_exhausts(self):
        """The other direction: same 1h left, but burning fast enough to run out.
        Percentage-of-window would have called this expiring and spent it down."""
        verdict = fleetctl.window_surplus(
            {"used_percent": 85, "seconds_to_reset": 3600, "window_minutes": 300}
        )
        self.assertFalse(verdict["surplus"])
        self.assertGreater(verdict["projected_used_percent_at_reset"], 100)

    def test_observed_pace_wins_over_derived_average(self):
        """The oracle's own projection is the only recent-weighted signal, so it
        must override the whole-window average that would say the opposite."""
        window = {"used_percent": 20, "seconds_to_reset": 3600, "window_minutes": 300}
        self.assertTrue(fleetctl.window_surplus(window)["surplus"])
        window["will_last_to_reset"] = False  # oracle: a burst will exhaust it
        verdict = fleetctl.window_surplus(window)
        self.assertFalse(verdict["surplus"])
        self.assertEqual(verdict["basis"], "observed_pace")

    def test_eta_beyond_the_reset_is_surplus(self):
        verdict = fleetctl.window_surplus(
            {"used_percent": 90, "seconds_to_reset": 600, "eta_seconds": 4000}
        )
        self.assertTrue(verdict["surplus"])
        self.assertEqual(verdict["basis"], "observed_pace")

    def test_elapsed_window_fallback_only_when_no_rate_is_knowable(self):
        """No window length and no pace: fall back to the crude proxy."""
        verdict = fleetctl.window_surplus({"used_percent": 90, "seconds_to_reset": 600})
        self.assertEqual(verdict["basis"], "elapsed_window")
        self.assertTrue(verdict["surplus"])
        far = fleetctl.window_surplus({"used_percent": 90, "seconds_to_reset": 90000})
        self.assertFalse(far["surplus"])

    def test_horizon_is_a_tenth_of_the_window_capped_at_an_hour(self):
        # 5h window -> 30 min; weekly window -> capped at 60 min, not 16.8h.
        self.assertEqual(fleetctl.spend_down_horizon_s({"window_minutes": 300}), 1800)
        self.assertEqual(fleetctl.spend_down_horizon_s({"window_minutes": 10080}), 3600)

    def test_horizon_falls_back_when_the_snapshot_predates_window_minutes(self):
        self.assertEqual(fleetctl.spend_down_horizon_s({}), fleetctl.SPEND_DOWN_MAX_S)

    # --- unknown fails open -------------------------------------------------

    def test_unknown_quota_does_not_throttle_frontier_lanes(self):
        """A stranger with no oracle must not get a silently throttled router.
        Absent measurement is not evidence of exhaustion."""
        frontier = {"max_parallel": 3, "quality_tier": "frontier"}
        for value in ("clock_aware", "strict"):
            self._policy(value)
            self.assertEqual(fleetctl.effective_cap(frontier, "UNKNOWN"), 3)
        self.assertEqual(fleetctl.task_band("UNKNOWN"), "quality_first")

    def test_exhausted_still_fails_closed(self):
        """The counterpart: EXHAUSTED is a measurement, not a gap."""
        self._policy("clock_aware")
        frontier = {"max_parallel": 3, "quality_tier": "frontier"}
        self.assertEqual(fleetctl.task_band("EXHAUSTED"), "critical")
        self.assertEqual(fleetctl.effective_cap(frontier, "CRITICAL"), 1)

    # --- the finding this whole change rests on ----------------------------

    def test_reset_clock_reaches_the_pool_report(self):
        """The regression that made the pool look clockless: reset_at was parsed,
        stored and carried, then dropped by show_usage's hand-picked field list."""
        runtime = _runtime_with_window(92, 20 * 60)
        _, evidence = fleetctl.current_pool_state(runtime, "claude")
        self.assertIn("reset_at", evidence["windows"]["secondary"])
        self.assertGreater(evidence["windows"]["secondary"]["seconds_to_reset"], 0)

    # --- clock_aware -------------------------------------------------------

    def test_expiring_window_stops_gating_under_clock_aware(self):
        """92% with 20 minutes left on a 5h window: spend it, do not ration it."""
        self._policy("clock_aware")
        runtime = _runtime_with_window(92, 20 * 60)
        state, evidence = fleetctl.current_pool_state(runtime, "claude")
        self.assertEqual(state, "CRITICAL")  # measurement stays honest
        self.assertEqual(evidence["spend_down"], ["secondary"])
        self.assertEqual(fleetctl.task_band(state, evidence), "quality_first")

    def test_same_percentage_far_from_reset_still_throttles(self):
        """The control: 92% with three days left is real scarcity."""
        self._policy("clock_aware")
        runtime = _runtime_with_window(92, 3 * 86400, window_minutes=10080)
        state, evidence = fleetctl.current_pool_state(runtime, "claude")
        self.assertEqual(evidence["spend_down"], [])
        self.assertEqual(fleetctl.task_band(state, evidence), "critical")

    def test_a_surviving_window_still_binds_while_another_expires(self):
        """An expiring window must not unlock a pool a second window still limits."""
        self._policy("clock_aware")
        runtime = _runtime_with_window(95, 10 * 60)
        runtime["quota_snapshots"]["claude"]["windows"]["weekly"] = {
            "used_percent": 80,
            "reset_at": fleetctl.iso(fleetctl.utc_now() + timedelta(days=4)),
            "window_minutes": 10080,
        }
        state, evidence = fleetctl.current_pool_state(runtime, "claude")
        self.assertEqual(evidence["spend_down"], ["secondary"])
        self.assertEqual(evidence["binding_used_percent"], 80)
        self.assertEqual(fleetctl.task_band(state, evidence), "conserve")

    def test_frontier_cap_opens_for_an_expiring_window(self):
        self._policy("clock_aware")
        runtime = _runtime_with_window(92, 20 * 60)
        state, evidence = fleetctl.current_pool_state(runtime, "claude")
        frontier = {"max_parallel": 3, "quality_tier": "frontier"}
        self.assertEqual(fleetctl.effective_cap(frontier, state, evidence), 3)

    # --- strict reproduces today's behaviour -------------------------------

    def test_strict_ignores_the_clock_entirely(self):
        """`strict` must be the pre-change router, bit for bit."""
        self._policy("strict")
        runtime = _runtime_with_window(92, 20 * 60)
        state, evidence = fleetctl.current_pool_state(runtime, "claude")
        self.assertEqual(fleetctl.task_band(state, evidence), "critical")
        frontier = {"max_parallel": 3, "quality_tier": "frontier"}
        self.assertEqual(fleetctl.effective_cap(frontier, state, evidence), 1)

    # --- off keeps working, including under its original name --------------

    def test_off_disables_gating(self):
        self._policy("off")
        runtime = _runtime_with_window(92, 3 * 86400, window_minutes=10080)
        state, evidence = fleetctl.current_pool_state(runtime, "claude")
        self.assertEqual(fleetctl.task_band(state, evidence), "quality_first")
        frontier = {"max_parallel": 3, "quality_tier": "frontier"}
        self.assertEqual(fleetctl.effective_cap(frontier, state, evidence), 3)

    def test_legacy_ignore_quota_env_still_means_off(self):
        os.environ["FLEET_IGNORE_QUOTA"] = "1"
        self.assertEqual(fleetctl.resolve_quota_policy()[0], "off")
        self.assertTrue(fleetctl.quota_gating_disabled())

    # --- states that must never soften -------------------------------------

    def test_exhausted_and_unknown_never_soften(self):
        """A spent window is not use-it-or-lose-it, and no data is no clock."""
        self._policy("clock_aware")
        evidence = {"spend_down": ["secondary"], "routing_state": "ABUNDANT"}
        self.assertEqual(fleetctl.gating_state("EXHAUSTED", evidence), "EXHAUSTED")
        self.assertEqual(fleetctl.gating_state("UNKNOWN", evidence), "UNKNOWN")

    def test_full_window_is_exhausted_not_spend_down(self):
        self._policy("clock_aware")
        runtime = _runtime_with_window(100, 15 * 60)
        state, _ = fleetctl.current_pool_state(runtime, "claude")
        self.assertEqual(state, "EXHAUSTED")

    # --- precedence and reporting ------------------------------------------

    def test_env_beats_overlay_beats_default(self):
        overlay = {"routing": {"quota_policy": "strict"}}
        self.assertEqual(
            fleetctl.resolve_quota_policy(overlay),
            ("strict", "overlay:routing.quota_policy"),
        )
        self._policy("off")
        self.assertEqual(
            fleetctl.resolve_quota_policy(overlay), ("off", "env:FLEET_QUOTA_POLICY")
        )
        os.environ.pop("FLEET_QUOTA_POLICY")
        self.assertEqual(fleetctl.resolve_quota_policy({}), ("clock_aware", "default"))

    def test_measurement_is_never_gated_by_policy(self):
        """The load-bearing guarantee: every policy reports the same real numbers."""
        runtime = _runtime_with_window(92, 20 * 60)
        seen = set()
        for value in fleetctl.QUOTA_POLICIES:
            self._policy(value)
            _, evidence = fleetctl.current_pool_state(runtime, "claude")
            seen.add(evidence["bottleneck_used_percent"])
        self.assertEqual(seen, {92})

    def test_usage_json_reports_windows_and_active_policy(self):
        self._policy("clock_aware")
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "runtime.json").write_text(
                json.dumps(_runtime_with_window(92, 20 * 60))
            )
            db_path = Path(tmp) / "telemetry.db"
            db_path.touch()
            import io

            buf = io.StringIO()
            with patch("sys.stdout", buf):
                fleetctl.show_usage(state_dir, db_path, as_json=True)
            out = json.loads(buf.getvalue())
        self.assertEqual(out["quota_policy"]["policy"], "clock_aware")
        pool = out["quota_pools"]["claude"]
        self.assertIn("reset_at", pool["windows"]["secondary"])
        self.assertEqual(pool["bottleneck_used_percent"], 92)
        self.assertEqual(pool["spend_down"], ["secondary"])
        self.assertEqual(pool["band"], "quality_first")


class EffortTests(unittest.TestCase):
    def entry(self, level='high', harness='opencode'):
        return {'levels': {harness: ['low', 'medium', 'high', 'xhigh', 'max']},
                'default': level, 'knee': level, 'by_role': {'review': 'xhigh'},
                'evidence': {'status': 'measured', 'source': 'test curve',
                             'read_on': fleetctl.utc_now().date().isoformat(),
                             'index_version': '4.3.2',
                             'curve': {level: {'intelligence_index': 40}}},
                'recheck': 'Index bump or 30 days'}

    def overlay(self):
        r = roster()
        r['effort'] = {l['model_key']: self.entry() for l in r['lanes']}
        return r

    def test_route_returns_role_effort_and_keeps_roster_immutable(self):
        r = self.overlay()
        selected = fleetctl.choose_lane(r, {}, 'review', 'read-only', 'text')
        self.assertEqual(selected['effort'], 'xhigh')
        self.assertIn('/review', selected['effort_reason'])
        self.assertNotIn('effort', r['lanes'][0])

    def test_missing_data_is_visible_in_route_and_doctor(self):
        r = self.overlay()
        del r['effort']['kimi-k3']  # Sabotage: table entry removed.
        self.assertIsNone(fleetctl.choose_lane(r, {}, 'default', 'read-only', 'text')['effort'])
        with tempfile.TemporaryDirectory() as d:
            issues = fleetctl.effort_problems(r, Path(d))
        self.assertTrue(any('kimi-k3: missing effort' in i for i in issues), issues)

    def test_supported_variant_and_explicit_override(self):
        r = self.overlay()
        self.assertEqual(fleetctl.resolve_effort(r, 'opencode-go/kimi-k3', 'review')['effort'], 'xhigh')
        self.assertEqual(fleetctl.resolve_effort(r, 'k3', 'review', explicit='low')['effort'], 'low')

    def test_invalid_variant_has_loud_provider_exception(self):
        r = self.overlay()
        r['effort']['kimi-k3']['default'] = 'unsupported'
        result = fleetctl.resolve_effort(r, 'k3')
        self.assertIsNone(result['effort'])
        self.assertIn('unsupported, provider-default', result['reason'])

    def test_invalid_direct_effort_is_refused(self):
        r = {'effort': {'gpt-test': self.entry(harness='codex')}}
        with self.assertRaisesRegex(fleetctl.FleetError, 'unsupported effort'):
            fleetctl.resolve_effort(r, 'gpt-test', harness='codex', explicit='unsupported')

    def test_model_without_variants_has_explicit_exception(self):
        r = self.overlay()
        e = r['effort']['kimi-k3']; e.update(levels={'opencode': []}, default='provider-default', knee='provider-default', by_role={})
        result = fleetctl.resolve_effort(r, 'k3')
        self.assertIsNone(result['effort'])
        self.assertIn('provider-default, no level control exposed', result['reason'])

    def test_quota_band_preserves_role_and_changes_level(self):
        r = self.overlay()
        r['effort']['kimi-k2.7-code']['by_band'] = {'conserve': {'default': 'medium'}}
        runtime = {'quota_snapshots': {'opencode-go': snapshot(85)}}
        with patch.dict(os.environ, {'FLEET_QUOTA_POLICY': 'strict'}):
            selected = fleetctl.choose_lane(r, runtime, 'default', 'read-only', 'text')
        self.assertEqual(selected['routing']['band'], 'conserve')
        self.assertEqual(selected['effort'], 'medium')

    def test_evidence_checks_can_each_fail(self):
        with tempfile.TemporaryDirectory() as d:
            for field, value, needle in [('read_on', '2000-01-01', 'stale effort'),
                                         ('read_on', 'not-a-date', 'invalid effort'),
                                         ('source', '', 'needs source'),
                                         ('index_version', '', 'missing index_version'),
                                         ('status', 'pretend', 'measured/unmeasured')]:
                with self.subTest(field=field, value=value):
                    r = self.overlay(); r['effort']['kimi-k3']['evidence'][field] = value
                    self.assertTrue(any(needle in i for i in fleetctl.effort_problems(r, Path(d))))
            for field, value, needle in [('levels', {}, 'missing supported'),
                                         ('default', 'unsupported', 'unsupported effort'),
                                         ('knee', 'unsupported', 'unsupported effort'),
                                         ('by_role', {'review': 'unsupported'}, 'unsupported effort'),
                                         ('by_band', {'conserve': {'review': 'unsupported'}}, 'unsupported effort'),
                                         ('by_band', [], 'invalid effort role/band'),
                                         ('evidence', [], 'invalid effort evidence'),
                                         ('refuse_roles', [], 'invalid effort refusal'),
                                         ('levels', {'opencode': 'high'}, 'missing supported'),
                                         ('recheck', '', 'recheck trigger')]:
                with self.subTest(field=field):
                    r = self.overlay(); r['effort']['kimi-k3'][field] = value
                    self.assertTrue(any(needle in i for i in fleetctl.effort_problems(r, Path(d))))

    def test_fresh_catalog_version_and_knee_drift_checks_can_fail(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / 'market').mkdir()
            catalog = {'fetched_at': fleetctl.iso(), 'artificial_analysis': {
                'kimi-k3': {'index_version': 'next', 'levels': {'high': {'intelligence_index': 43}}}}}
            fleetctl.atomic_json(root / 'market' / 'market-catalog.json', catalog)
            issues = fleetctl.effort_problems(self.overlay(), root)
            self.assertTrue(any('index version changed' in i for i in issues), issues)
            self.assertTrue(any('more than 2 points' in i for i in issues), issues)
            catalog['artificial_analysis']['kimi-k3']['levels']['high'] = {'ambiguous': True}
            fleetctl.atomic_json(root / 'market' / 'market-catalog.json', catalog)
            self.assertTrue(any('snapshot ambiguous' in i for i in fleetctl.effort_problems(self.overlay(), root)))
            catalog['fetched_at'] = fleetctl.iso(fleetctl.utc_now() - timedelta(days=31))
            fleetctl.atomic_json(root / 'market' / 'market-catalog.json', catalog)
            self.assertEqual(fleetctl.effort_problems(self.overlay(), root), [])

    def test_doctor_and_brief_surface_effort_sabotage(self):
        import io
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); r = self.overlay(); del r['effort']['kimi-k3']
            overlay = root / 'overlay.json'; overlay.write_text(json.dumps(r))
            out = io.StringIO()
            with patch('sys.stdout', out):
                self.assertEqual(fleetctl.doctor_command(overlay, root), 1)
            self.assertIn('kimi-k3: missing effort', out.getvalue())
            self.assertIn('Effort evidence needs recheck:', fleetctl.render_brief(fleetctl.fleet_overview(r, {}, root), verbose=True))
            run = subprocess.run([sys.executable, str(MODULE_PATH), '--overlay', str(overlay),
                                  '--state-dir', d, 'effort-check'], capture_output=True, text=True)
            self.assertEqual(run.returncode, 1)
            self.assertIn('kimi-k3: missing effort', run.stderr)

    def test_direct_aliases_and_shipped_gpt_seats(self):
        path = Path(__file__).resolve().parent / 'fixtures' / 'access-overlay.test.json'
        r = json.loads(path.read_text())
        for model, role, want in [('gpt-6.1-sol', 'default', 'medium'), ('gpt-6.1-sol', 'builder', 'high'),
                                  ('gpt-6.1-sol', 'owned-dispatch', 'xhigh'), ('gpt-6-astra', 'reviewer', 'xhigh'),
                                  ('gpt-6.1-sol', 'failed-unit', 'xhigh'),
                                  ('gpt-6-astra', 'audit-retry', 'max'), ('gpt-6-luna', 'explorer', 'medium'),
                                  ('gpt-6-luna', 'probe', 'low'), ('opus', 'default', 'high'), ('sonnet', 'probe', 'low')]:
            with self.subTest(model=model, role=role):
                harness = 'claude' if model in ('opus', 'sonnet') else 'codex'
                self.assertEqual(fleetctl.resolve_effort(r, model, role, harness)['effort'], want)
        self.assertEqual(fleetctl.resolve_effort(r, 'gpt-6.1-sol', 'builder', 'codex', band='conserve')['effort'], 'medium')
        self.assertEqual(fleetctl.resolve_effort(r, 'gpt-6.1-sol', 'builder', 'codex', explicit='xhigh', band='conserve')['effort'], 'xhigh')
        with self.assertRaisesRegex(fleetctl.FleetError, 'Astra'):
            fleetctl.resolve_effort(r, 'gpt-6.1-sol', 'audit-retry', 'codex')
        with self.assertRaisesRegex(fleetctl.FleetError, 'CRITICAL'):
            fleetctl.resolve_effort(r, 'gpt-6.1-sol', 'builder', 'codex', band='critical')
        self.assertEqual(fleetctl.resolve_effort(r, 'gpt-6.1-sol', 'builder', 'codex', explicit='high', band='critical')['effort'], 'high')

    def test_cli_effort_agrees_with_route_band_and_manual_level(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); r = self.overlay()
            for entry in r['effort'].values():
                entry['by_band'] = {'conserve': {'review': 'medium'}}
            overlay = root / 'overlay.json'; overlay.write_text(json.dumps(r))
            env = dict(os.environ, ACCESS_OVERLAY=str(overlay), FLEET_STATE_DIR=d,
                       FLEET_NO_AUTO_REFRESH='1', FLEET_QUOTA_POLICY='strict')
            def run(*args):
                return subprocess.run([sys.executable, str(MODULE_PATH), *args], env=env, capture_output=True, text=True, check=True)
            for runtime in ({'quota_snapshots': {'opencode-go': snapshot(85)}},
                            {'switches': {'opencode-go': 'low'}}):
                fleetctl.atomic_json(root / 'runtime.json', runtime)
                selected = json.loads(run('route', '--role', 'review', '--json').stdout)
                effort = json.loads(run('effort', selected['selector'], 'review', '--json').stdout)
                self.assertEqual(selected['effort'], 'medium')
                self.assertEqual(effort['effort'], selected['effort'])

    def test_critical_builder_stop_survives_direct_standins(self):
        overlay = Path(__file__).resolve().parent / 'fixtures' / 'access-overlay.test.json'
        live_roster = json.loads(overlay.read_text())
        wrapper = MODULE_PATH.with_name('codex-agent.sh')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = dict(os.environ, ACCESS_OVERLAY=str(overlay), FLEET_STATE_DIR=d,
                       FLEET_NO_AUTO_REFRESH='1', FLEET_QUOTA_POLICY='strict')
            for model in ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna'):
                runtime = {'quota_snapshots': {'codex': snapshot(95)}}
                fleetctl.set_model_choice(runtime, live_roster, 'codex', model)
                fleetctl.atomic_json(root / 'runtime.json', runtime)
                for role in ('builder', 'hard-builder', 'failed-builder'):
                    with self.subTest(model=model, role=role):
                        args = [str(wrapper), '--dry-run', '--model', 'gpt-6.1-sol', '--role', role,
                                '--prompt', 'synthetic task', '--dir', d]
                        run = subprocess.run(args, env=env, capture_output=True, text=True)
                        self.assertEqual(run.returncode, 2, run.stderr)
                        self.assertIn('CRITICAL', run.stderr)
                        self.assertEqual(run.stdout, '')
                        run = subprocess.run([*args, '--reasoning', 'high'], env=env, capture_output=True, text=True)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        self.assertIn(model, run.stdout)
                        self.assertIn('model_reasoning_effort=', run.stdout)

    def test_gemini_route_selector_agrees_with_resolved_level(self):
        path = Path(__file__).resolve().parent / 'fixtures' / 'access-overlay.test.json'
        r = json.loads(path.read_text())
        selected = fleetctl.choose_lane(r, {}, 'default', 'read-only', 'text', harness='agy')
        self.assertEqual(selected['effort'], 'medium')
        self.assertTrue(selected['selector'].endswith('-medium'), selected)
        explicit = fleetctl.resolve_effort(r, 'gemini-3.8-flash-low', 'default', 'agy', explicit='low')
        self.assertEqual((explicit['model_key'], explicit['effort']), ('gemini-3.8-flash', 'low'))

    def test_effort_cli_and_roster_entry_point(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); overlay = root / 'overlay.json'; overlay.write_text(json.dumps(self.overlay()))
            env = dict(os.environ, ACCESS_OVERLAY=str(overlay), FLEET_STATE_DIR=d, FLEET_NO_AUTO_REFRESH='1')
            run = subprocess.run([str(MODULE_PATH.with_name('roster.sh')), 'effort', 'k3', 'review'], env=env, capture_output=True, text=True)
            self.assertEqual((run.returncode, run.stdout.strip()), (0, 'xhigh'), run.stderr)
            run = subprocess.run([sys.executable, str(MODULE_PATH), 'effort', 'missing', '--harness', 'codex'], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertIn('missing effort data', run.stderr)

    def test_market_preserves_levels_and_snapshot_rows(self):
        import io
        path = MODULE_PATH.with_name('market-refresh.py')
        spec = importlib.util.spec_from_file_location('market_refresh', path)
        market = importlib.util.module_from_spec(spec); spec.loader.exec_module(market)
        def row(slug, index):
            return {'slug': slug, 'name': slug, 'evaluations': {'artificial_analysis_intelligence_index': index,
                    'terminal_bench_4': 0}, 'median_time_to_first_answer_token': 3,
                    'pricing': {'price_1m_input_tokens': 2}}
        payload = {'index_version': '4.3.2', 'data': [row('model-low', 40), row('model-high', 50), row('model-high-older', 48)]}
        with patch.dict(os.environ, {'AA_API_KEY': 'test-not-a-secret'}), patch.object(market.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
            result = market.fetch_artificial_analysis({'model': ['model']})
        self.assertEqual(set(result['model']['levels']), {'low', 'high'})
        self.assertEqual(len(result['model']['variants']), 3)  # No strongest-only collapse, including duplicate levels.
        self.assertEqual(result['model']['levels']['low']['ttfa_s'], 3)
        self.assertEqual(result['model']['index_version'], '4.3.2')
        self.assertEqual(result['model']['levels']['low']['price_1m_input'], 2)
        self.assertEqual(result['model']['levels']['low']['terminal_bench_4'], 0)
        self.assertTrue(result['model']['levels']['high']['ambiguous'])
        self.assertEqual(len(result['model']['levels']['high']['variants']), 2)
        payload['data'] = [row('model-non-thinking', 30)]
        with patch.dict(os.environ, {'AA_API_KEY': 'test-not-a-secret'}), patch.object(market.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
            result = market.fetch_artificial_analysis({'model': ['model']})
        self.assertEqual(set(result['model']['levels']), {'none'})


class HandSwitchTests(unittest.TestCase):
    """The operator's hand switch: off beats an abundant gauge, on beats an exhausted one."""

    def test_off_blocks_an_abundant_pool(self):
        runtime = {"quota_snapshots": {"opencode-go": snapshot(10)}, "switches": {"opencode-go": "off"}}
        state, evidence = fleetctl.current_pool_state(runtime, "opencode-go")
        self.assertEqual((state, evidence["source"]), ("EXHAUSTED", "switched-off"))
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")

    def test_on_beats_an_exhausted_pool(self):
        runtime = {"quota_snapshots": {"opencode-go": snapshot(100)}, "switches": {"opencode-go": "on"}}
        selected = fleetctl.choose_lane(roster(), runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], "k3")

    def test_cli_sets_and_clears_the_switch(self):
        with tempfile.TemporaryDirectory() as d:
            # The overlay is passed, not inherited: this test used to pass only when another
            # module had exported ACCESS_OVERLAY first, and failed when run on its own.
            overlay = Path(d) / "overlay.json"
            overlay.write_text(json.dumps(roster()), encoding="utf-8")
            run = lambda *a: subprocess.run(
                [sys.executable, str(MODULE_PATH), "--overlay", str(overlay), "--state-dir", d, "switch", *a],
                capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(run("codex", "off"), "off")
            self.assertEqual(run("codex"), "off")
            self.assertEqual(run("codex", "auto"), "auto")

    def test_no_quota_monitor_still_routes(self):
        """CodexBar gone: no snapshots at all. Routing must still pick a lane at full quality."""
        selected = fleetctl.choose_lane(roster(), {"quota_snapshots": {}}, "implementation", "write", "text")
        self.assertEqual(selected["routing"]["pool_state"], "UNKNOWN")
        self.assertTrue(selected["lane_id"])


if __name__ == "__main__":
    unittest.main()
