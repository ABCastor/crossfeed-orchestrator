"""BR-ROSTER-001: Argon is off every chain; Neon explains public and code material, Opus the rest.

The synthetic class patches the shipped example the same way the operator's overlay was changed.
Set CROSSFEED_ROSTER_OVERLAYS to a path-separated list of overlay files to run the same checks on
real ones (for example the staged primary and recovery copies); that class is skipped otherwise.
"""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import types
import unittest
from unittest import mock

from scripts import fleetctl, selector

ROOT = Path(__file__).resolve().parents[1]
ARGON, NEON, OPUS = "gemini-4-argon", "catalog-antigravity-catalog-gemini-4-neon-high", "antigravity-claude-opus-5-5-high"
ARGON_HIGH = "antigravity-gemini-4-argon-high"
VISUAL = ("design", "visual", "frontend-visual")
BANDS = ("quality_first", "conserve", "critical")


def _clock(day):
    """A stand-in for fleetctl.dt whose `now` is fixed to a day; the real calendar is never consulted."""
    class Now(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(day.year, day.month, day.day, 12)
    return types.SimpleNamespace(**{**vars(dt), "datetime": Now})


def after_round8(roster):
    """The example overlay with the round-8 roster change applied (mirrors the operator's overlay)."""
    r = copy.deepcopy(roster)
    template = next(l for l in r["lanes"] if l["lane_id"] == ARGON_HIGH)
    neon = copy.deepcopy(template)
    neon.update(lane_id=NEON, model_key="gemini-4-neon", selector="gemini-4-neon-high", quota_pool="antigravity-gemini",
                roles=["implementation", "review", "hard-reasoning", "debug", *VISUAL, "writing", "explain"],
                data_policy="code-ok", agy_read_only_copy=True,
                agy_read_only_roles=["review", "hard-reasoning", "debug", "explain"],
                notes="synthetic Neon lane for the round-8 roster tests")
    r["lanes"].append(neon)
    r["model_cards"]["gemini-4-neon"] = {"name": "Gemini 4 Neon", "lineage": "google", "status": "current"}
    r["effort"]["gemini-4-neon"] = {"default": "high", "knee": "high", "levels": {"agy": ["low", "medium", "high", "xhigh"]},
                                    "by_role": {}, "evidence": {"status": "unmeasured", "read_on": "2026-11-26"}}
    for lane in r["lanes"]:
        if lane.get("model_key") == ARGON and lane.get("harness") == "agy":
            lane["roles"] = []
    r["model_cards"][ARGON]["retired_on"] = "2026-12-01"
    roles = r["routing"]["roles"]
    roles["writing"] = {"quality_first": ["chatgpt", OPUS, NEON], "conserve": ["chatgpt", OPUS, NEON],
                        "critical": ["chatgpt", OPUS]}
    for role in VISUAL:
        roles[role] = {band: [NEON] for band in BANDS}
    roles["explain"] = {band: [NEON, OPUS] for band in BANDS}
    policy = r["policy"]["selector"]
    policy["mode_by_role"]["explain"] = "read-only"
    policy["families_by_role"]["explain"] = "reasoning"
    return r


class RosterRules:
    """The checks shared by the synthetic roster and any real overlay under test."""
    roster = None

    def route(self, role, data_class, *, roster=None, runtime=None, mode="read-only"):
        return fleetctl.choose_lane(roster or self.roster, runtime or {}, role, mode, "text", data_class=data_class)

    # BR-ROSTER-001
    def test_argon_is_on_no_chain_and_no_role(self):
        argon_lanes = {l["lane_id"] for l in self.roster["lanes"] if l.get("model_key") == ARGON}
        self.assertIn(ARGON_HIGH, argon_lanes)
        for role, bands in self.roster["routing"]["roles"].items():
            for band, chain in bands.items():
                self.assertFalse(argon_lanes & set(chain), f"{role}/{band}")
        for lane in self.roster["lanes"]:
            if lane.get("model_key") == ARGON:
                self.assertEqual(lane.get("roles"), [], lane["lane_id"])
        # Observable boundary: no role, data class or mode can route or select an Argon lane.
        probed = 0
        for role in self.roster["routing"]["roles"]:
            for data_class in ("public", "code", "private"):
                for mode in ("read-only", "write"):
                    options, _ = selector.enumerate_options(self.roster, {}, role, mode=mode, data_class=data_class,
                                                            fleet=fleetctl)
                    self.assertFalse([o for o in options if o["model_key"] == ARGON], (role, data_class, mode))
                    try:
                        chosen = self.route(role, data_class, mode=mode)
                    except fleetctl.FleetError:
                        continue
                    probed += 1
                    self.assertNotEqual(chosen["model_key"], ARGON, (role, data_class, mode))
        self.assertGreater(probed, 10)

    # BR-ROSTER-001 control: the same probe sees Argon when a chain names it.
    def test_control_a_chain_that_names_argon_routes_it(self):
        roster = copy.deepcopy(self.roster)
        roster["routing"]["roles"]["writing"]["quality_first"] = [ARGON_HIGH]
        self.assertEqual(self.route("writing", "private", roster=roster)["model_key"], ARGON)

    # BR-ROSTER-001
    def test_argon_retires_on_its_dated_card_and_never_runs_afterwards(self):
        self.assertEqual(self.roster["model_cards"][ARGON]["retired_on"], "2026-12-01")
        with mock.patch.object(fleetctl, "dt", _clock(dt.date(2026, 11, 30))):
            self.assertIsNone(fleetctl.retired_on(self.roster, ARGON))
        with mock.patch.object(fleetctl, "dt", _clock(dt.date(2026, 12, 1))):
            self.assertEqual(fleetctl.retired_on(self.roster, ARGON), "2026-12-01")
            # Even a chain that still names the lane cannot start it once the day has come.
            roster = copy.deepcopy(self.roster)
            roster["routing"]["roles"]["writing"]["quality_first"] = [ARGON_HIGH]
            with self.assertRaises(fleetctl.FleetError) as refused:
                self.route("writing", "private", roster=roster)
            self.assertIn("model retired", str(refused.exception))

    # BR-ROSTER-001
    def test_writing_chain_order(self):
        writing = self.roster["routing"]["roles"]["writing"]
        self.assertEqual(writing["quality_first"], ["chatgpt", OPUS, NEON])
        self.assertEqual(writing["conserve"], ["chatgpt", OPUS, NEON])
        self.assertEqual(writing["critical"], ["chatgpt", OPUS])
        # ChatGPT is a gateway seat that is not in this static roster; the next seat is Opus for any material.
        for data_class in ("public", "code", "private"):
            self.assertEqual(self.route("writing", data_class)["model_key"], "claude-opus-5-5")
        # Neon writes only after Opus, and only public or code material.
        no_opus = copy.deepcopy(self.roster)
        next(l for l in no_opus["lanes"] if l["lane_id"] == OPUS)["access_status"] = "unverified"
        self.assertEqual(self.route("writing", "public", roster=no_opus)["lane_id"], NEON)
        self.assertEqual(self.route("writing", "code", roster=no_opus)["lane_id"], NEON)
        with self.assertRaises(fleetctl.FleetError):
            self.route("writing", "private", roster=no_opus)

    # BR-ROSTER-001
    def test_explain_takes_neon_for_public_and_code_and_opus_for_private(self):
        self.assertEqual(self.roster["routing"]["roles"]["explain"]["quality_first"], [NEON, OPUS])
        self.assertEqual(self.route("explain", "public")["lane_id"], NEON)
        self.assertEqual(self.route("explain", "code")["lane_id"], NEON)
        private = self.route("explain", "private")
        self.assertEqual((private["model_key"], private["effort"]), ("claude-opus-5-5", "high"))
        self.assertEqual(self.route("explain", None)["model_key"], "claude-opus-5-5")  # unclassified is private

    # BR-ROSTER-001 control: the probe sees Neon for private material once its policy allows it.
    def test_control_neon_takes_private_material_only_when_its_policy_says_so(self):
        roster = copy.deepcopy(self.roster)
        next(l for l in roster["lanes"] if l["lane_id"] == NEON)["data_policy"] = "private-ok"
        self.assertEqual(self.route("explain", "private", roster=roster)["lane_id"], NEON)

    # BR-ROSTER-001
    def test_visual_roles_use_neon_where_argon_was(self):
        for role in VISUAL:
            for band in BANDS:
                self.assertEqual(self.roster["routing"]["roles"][role][band], [NEON], (role, band))
            self.assertEqual(self.route(role, "code")["lane_id"], NEON)

    # BR-ROSTER-001
    def test_neon_stays_code_ok_and_explain_is_read_only(self):
        neon = next(l for l in self.roster["lanes"] if l["lane_id"] == NEON)
        self.assertEqual(neon["data_policy"], "code-ok")
        self.assertIn("explain", neon["roles"])
        self.assertEqual(self.roster["policy"]["selector"]["mode_by_role"]["explain"], "read-only")
        self.assertEqual(self.roster["policy"]["selector"]["families_by_role"]["explain"], "reasoning")


class SyntheticRosterTests(RosterRules, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        example = json.loads((ROOT / "examples/access-overlay.example.json").read_text())
        example.pop("provider_sources", None)
        cls.roster = after_round8(example)


@unittest.skipUnless(os.environ.get("CROSSFEED_ROSTER_OVERLAYS"), "set CROSSFEED_ROSTER_OVERLAYS to check real overlays")
class RealOverlayTests(unittest.TestCase):
    def test_every_named_overlay_obeys_the_same_rules(self):
        paths = [p for p in os.environ["CROSSFEED_ROSTER_OVERLAYS"].split(os.pathsep) if p]
        self.assertTrue(paths)
        for path in paths:
            roster = json.loads(Path(path).read_text())
            checks = type("Overlay", (RosterRules, unittest.TestCase), {"roster": roster})
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(checks).run(result)
            self.assertEqual((result.errors, result.failures), ([], []), path)
            self.assertGreaterEqual(result.testsRun, 8, path)


if __name__ == "__main__":
    unittest.main()
