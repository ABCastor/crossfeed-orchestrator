"""usage_history: readings over time for the console charts; display only, never read by routing."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from scripts import usage_history as history

T0 = 1_800_000_000


def reading(used, observed, reset=T0 + 86400, minutes=10080, name="weekly"):
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"observed_at": iso(observed), "windows": {name: {"label": "Weekly", "used_percent": used,
                                                              "reset_at": iso(reset), "window_minutes": minutes}}}


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_records_a_window_and_reads_it_back_oldest_first(self):
        self.assertEqual(history.record(self.dir, "claude", reading(10, T0)), 1)
        self.assertEqual(history.record(self.dir, "claude", reading(14, T0 + 900)), 1)
        data = history.series(self.dir, T0 + 1000)
        window = data["claude"]["weekly"]
        self.assertEqual(window["points"], [[T0, 10], [T0 + 900, 14]])
        self.assertEqual((window["minutes"], window["label"], window["resets"]), (10080, "Weekly", [T0 + 86400]))
        self.assertEqual(stat.S_IMODE((self.dir / history.NAME).stat().st_mode), 0o600)

    def test_an_unchanged_window_is_not_repeated_until_the_heartbeat(self):
        history.record(self.dir, "codex", reading(5, T0))
        self.assertEqual(history.record(self.dir, "codex", reading(5, T0 + 600)), 0)
        self.assertEqual(history.record(self.dir, "codex", reading(5, T0 + history.HEARTBEAT_S + 1)), 1)
        self.assertEqual(len(history.series(self.dir, T0 + 4000)["codex"]["weekly"]["points"]), 2)

    def test_a_reset_is_kept_even_when_the_percentage_is_the_same(self):
        history.record(self.dir, "codex", reading(0, T0, reset=T0 + 100))
        self.assertEqual(history.record(self.dir, "codex", reading(0, T0 + 200, reset=T0 + 700)), 1)

    def test_old_samples_and_bad_values_are_ignored_and_nothing_raises(self):
        history.record(self.dir, "claude", reading(10, T0 - 40 * 86400))
        self.assertEqual(history.record(self.dir, "claude", reading(140, T0)), 0)        # out of range
        self.assertEqual(history.record(self.dir, "claude", {"windows": "bad"}), 0)
        self.assertEqual(history.record(self.dir, "claude", {}), 0)
        (self.dir / history.NAME).write_bytes((self.dir / history.NAME).read_bytes() + b"not json\n" + b"x" * 600 + b"\n")
        self.assertEqual(history.series(self.dir, T0, days=30), {})                     # the 40-day-old row is out
        self.assertEqual(len(history.series(self.dir, T0, days=60)["claude"]["weekly"]["points"]), 1)

    def test_compaction_drops_samples_older_than_the_retention_and_keeps_the_rest(self):
        path = self.dir / history.NAME
        old = "".join(json.dumps({"t": T0 - 90 * 86400 + n, "p": "p", "w": "w", "u": 1}) + "\n" for n in range(10000))
        path.write_text(old)
        os.chmod(path, 0o600)
        self.assertGreater(path.stat().st_size, history.COMPACT_BYTES)
        history.record(self.dir, "claude", reading(10, T0), now=T0)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row["p"] for row in rows], ["claude"])
        self.assertEqual(history.record(self.dir, "claude", reading(11, T0 + 900), now=T0 + 900), 1)
        self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_the_log_is_not_followed_through_a_symlink(self):
        target = self.dir / "elsewhere"
        target.write_text("")
        (self.dir / history.NAME).symlink_to(target)
        self.assertEqual(history.record(self.dir, "claude", reading(10, T0)), 0)
        self.assertEqual(target.read_text(), "")


class Wiring(unittest.TestCase):
    def test_a_refresh_that_stores_a_reading_also_logs_it_and_a_failed_one_does_not(self):
        from unittest import mock
        from tests.test_console import console
        fleetctl = console.fleetctl
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            roster = {"quota_pools": {"test": {"quota_refresh": {"oracle": "codexbar", "provider": "test"}}}}
            fleetctl.atomic_json(state / "runtime.json", {})
            ok = {"available": True, "observed_at": fleetctl.iso(), "source": "fixture",
                  "windows": {"weekly": {"label": "Weekly", "used_percent": 33, "window_minutes": 10080,
                                         "reset_at": fleetctl.iso(fleetctl.utc_now() + fleetctl.dt.timedelta(days=3))}}}
            with mock.patch.dict(fleetctl.ORACLE_REGISTRY, {"codexbar": mock.Mock(return_value=ok)}):
                self.assertEqual(fleetctl.refresh_stale_pools(state, ["test"], roster=roster, force=True), {"test": "refreshed"})
            rows = [json.loads(line) for line in (state / history.NAME).read_text().splitlines()]
            self.assertEqual([(r["p"], r["w"], r["u"]) for r in rows], [("test", "weekly", 33)])
            with mock.patch.dict(fleetctl.ORACLE_REGISTRY, {"codexbar": mock.Mock(return_value={"available": False, "reason": "x"})}):
                fleetctl.refresh_stale_pools(state, ["test"], roster=roster, force=True)
            self.assertEqual(len((state / history.NAME).read_text().splitlines()), 1)

    def test_glance_keeps_cost_totals_but_not_the_hourly_buckets(self):
        from tests.test_console import console, base_overlay
        fleetctl = console.fleetctl
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            roster = base_overlay()
            overview = fleetctl.fleet_overview(roster, {}, state)
            pool = overview["pools"][0]
            pool["usage_cost"] = {"today_usd": 1.0, "last_7d_usd": 2.0, "last_30d_usd": 3.0, "hourly": [[1, 2, 3.0]],
                                  "daily": [], "by_model": []}
            payload = fleetctl.glance_payload(state / "overlay.json", roster, overview, None)
            row = next(r for r in payload["pools"] if r["pool"] == pool["pool"])
            self.assertEqual(row["usage_cost"]["last_7d_usd"], 2.0)
            self.assertNotIn("hourly", row["usage_cost"])


if __name__ == "__main__":
    unittest.main()
