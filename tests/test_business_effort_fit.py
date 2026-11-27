"""BR-SELECT-011: the thinking level fits the job's difficulty."""
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl  # noqa: E402
import rating  # noqa: E402
import rating_explore  # noqa: E402
import selector  # noqa: E402
from tests.test_selector import quality_row  # noqa: E402

LEVELS = ("medium", "high", "max")
RATES = {"medium": .45, "high": .62, "max": .70}      # terminal-bench pass rates per effort
DEBIT = {"medium": .01, "high": .14, "max": .4}       # quota debit per task per effort
ORDER = {level: n for n, level in enumerate(LEVELS)}
MODEL = "gpt-6.1-sol"


class EffortFit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="effort fit ")
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"FLEET_QUOTA_POLICY": "clock_aware", "FLEET_IGNORE_QUOTA": "0"}).start()
        mock.patch.object(fleetctl, "_POLICY_CACHE", ("clock_aware", "test")).start()
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": ["codex"]}}
        self.roster["effort"][MODEL]["levels"]["codex"] = list(LEVELS)
        self.roster["quota_pools"]["codex"]["value_capacity"] = {
            "weekly_usd_equivalent": 46, "source": "synthetic equal-window debit"}
        rows = [dict(quality_row(MODEL, level, .8, pass_rate=RATES[level]),
                     own_cost={"codex": {"percent_per_task": DEBIT[level]}}) for level in LEVELS]
        directory = self.state / "evidence"
        directory.mkdir()
        (directory / "levels.json").write_text(json.dumps({"schema": "fleet-evidence-levels/v1", "rows": rows}))
        mock.patch.object(rating, "COMPETENT_RERUN_PRICE", .23).start()

    def select(self, role="review", **kwargs):
        kwargs.setdefault("allow", f"codex:{MODEL}:*")
        return selector.select_option(self.roster, {}, self.state, role, fleet=fleetctl,
                                      persist_receipts=False, no_explore=True, **kwargs)

    def level(self, difficulty, stakes):
        return self.select(difficulty=difficulty, rating_stakes=stakes)["choice"]["level"]

    # BR-SELECT-011
    def test_same_model_easy_never_thinks_harder_than_ordinary_than_hard(self):
        for stakes in ("routine", "important", "critical"):
            easy, ordinary, hard = (ORDER[self.level(d, stakes)] for d in ("easy", "ordinary", "hard"))
            self.assertLessEqual(easy, ordinary, stakes)
            self.assertLessEqual(ordinary, hard, stakes)
        # The fixture separates all three: the hard job needs the top effort, the others do not.
        self.assertEqual(self.level("hard", "critical"), "max")
        self.assertLess(ORDER[self.level("ordinary", "critical")], ORDER["max"])
        self.assertNotIn("max", {o["level"] for o in self.select(difficulty="easy", rating_stakes="critical")["top3"]})
        self.assertLess(ORDER[self.level("easy", "routine")], ORDER[self.level("hard", "critical")])

    # BR-SELECT-011
    def test_hard_job_keeps_the_benchmark_curve_and_every_effort(self):
        result = self.select(difficulty="hard", rating_stakes="critical", collect_eligible=True)
        for item in result["eligible_options"]:
            self.assertAlmostEqual(item["p_success_prior"], rating.success_prior(RATES[item["level"]], "hard"))
        self.assertEqual({o["level"] for o in result["top3"]}, set(LEVELS))
        with mock.patch.object(rating, "BENCHMARK_TASKS", 10 ** 12):
            self.assertEqual(self.level("hard", "critical"), result["choice"]["level"])

    # BR-SELECT-011
    def test_ordinary_effort_gain_is_the_derived_share_of_the_benchmark_gain(self):
        result = self.select(difficulty="ordinary", collect_eligible=True)
        p = {o["level"]: o["p_success_prior"] for o in result["eligible_options"]}
        logit = lambda x: rating._logit(x)
        for level in ("high", "max"):
            gain = logit(p[level]) - logit(p["medium"])
            benchmark = logit(RATES[level]) - logit(RATES["medium"])
            self.assertAlmostEqual(gain, rating.EFFORT_LIFT_SCALE["ordinary"] * benchmark)
        self.assertAlmostEqual(logit(p["medium"]), logit(RATES["medium"]) - rating.JOB_OFFSET["ordinary"])

    # BR-SELECT-011
    def test_lift_scale_is_derived_from_the_cached_evidence(self):
        # Artificial Analysis levels.json, lowest to max effort: Intelligence Index vs Terminal-Bench.
        index = {"opus": (.348, .482, .518, .574, .591), "sonnet": (.256, .308, .415, .499, .545)}
        terminal = {"opus": (.313, .525, .566, .596, .658), "sonnet": (.207, .298, .439, .571, .639)}
        ratios = [rating.effort_lift_ratio(index[m], terminal[m]) for m in index]
        for ratio in ratios:
            self.assertAlmostEqual(ratio, rating.EFFORT_LIFT_SCALE["ordinary"], delta=.04)
        self.assertEqual(rating.EFFORT_LIFT_SCALE["hard"], 1.0)
        self.assertLess(rating.EFFORT_LIFT_SCALE["easy"], rating.EFFORT_LIFT_SCALE["ordinary"])
        for rate in (.2, .5, .8):
            self.assertAlmostEqual(rating.effort_prior(rate, .1, "hard"), rating.success_prior(rate, "hard"))
            ladder = [rating.effort_prior(rate, .1, d) for d in ("easy", "ordinary", "hard")]
            self.assertEqual(ladder, sorted(ladder, reverse=True))

    # BR-SELECT-011
    def test_role_names_the_difficulty_and_the_caller_overrides_it(self):
        def difficulty(role, **kwargs):
            return self.select(role, **kwargs)["calibration_difficulty"]
        for role in ("lookup", "summarise", "format", "extract", "triage", "translate"):
            self.assertEqual(difficulty(role), "easy", role)
        for role in ("implementation", "review", "writing", "design", "explain"):
            self.assertEqual(difficulty(role), "ordinary", role)
        for role in ("hard-reasoning", "debug"):
            self.assertEqual(difficulty(role), "hard", role)
        self.assertEqual(difficulty("lookup", difficulty="hard"), "hard")
        self.assertEqual(difficulty("some-new-role"), "ordinary")
        self.roster["policy"]["selector"]["difficulty_by_role"] = {"review": "hard"}
        self.assertEqual(difficulty("review"), "hard")

    # BR-SELECT-011
    def test_receipt_says_how_close_the_effort_is_to_the_models_best(self):
        result = self.select(difficulty="ordinary", rating_stakes="routine")
        fit = result["choice"]["effort_fit"]
        self.assertEqual(result["effort_fit"], fit)
        self.assertRegex(fit["line"], r"^ordinary job: \w+ reaches \d+% of this model's best success at \d+% of its top effort's cost$")
        self.assertLessEqual(fit["reaches_best_pct"], 100)
        self.assertLess(fit["cost_vs_top_pct"], 100)

    # BR-SELECT-011
    def test_checked_outcomes_count_in_their_own_difficulty_bucket(self):
        pick = {"pool": "codex", "model_identity": MODEL, "level": "high"}
        rows = []
        for run, difficulty in (("easy-run", "easy"), ("hard-run", "hard"), ("old-run", None)):
            live = dict(pick) if difficulty is None else dict(pick, calibration_difficulty=difficulty)
            rows += [dict(schema=rating_explore.rating_log.DISPATCH, run_id=run, task_id=run, live_pick=live),
                     dict(schema=rating_explore.rating_log.ACCEPTANCE, run_id=run, task_id=run,
                          verdict="fail", check="independent test")]
        (self.state / rating_explore.rating_log.LEDGER).write_text(
            "".join(json.dumps(row) + "\n" for row in rows))
        key = rating_explore.route_key(pick)
        failures = lambda d: rating_explore.checked_counts(self.state, d).get((key, "fail"), 0)
        self.assertEqual((failures("easy"), failures("ordinary"), failures("hard")), (2, 1, 2))
        self.assertEqual(failures(None), 3)
        # A recorded failure lowers that bucket's success and no other recorded bucket's.
        hard = {o["level"]: o["p_success"] for o in self.select(difficulty="hard", collect_eligible=True)["eligible_options"]}
        ordinary = {o["level"]: o["p_success"] for o in self.select(difficulty="ordinary", collect_eligible=True)["eligible_options"]}
        self.assertLess(hard["high"], rating.success_prior(RATES["high"], "hard"))
        self.assertLess(ordinary["high"], self.select(difficulty="ordinary", collect_eligible=True)["eligible_options"][1]["p_success_prior"])

    # BR-SELECT-011
    def test_thompson_exploration_still_samples_an_ordinary_job(self):
        result = self.select(difficulty="ordinary", rating_stakes="critical")
        options = [o for o in result["top3"] if o["expected_cost"] is not None]
        self.assertGreaterEqual(len(options), 2)
        self.assertEqual(result["calibration_difficulty"], "ordinary")
        for item in options:  # the Beta posterior starts from the difficulty-adjusted prior
            self.assertEqual(item["posterior"], rating_explore.posterior(item["p_success_prior"]))
        sampled = set()
        # Pool state and funding gate real exploration; this test is about the sampler's inputs.
        mock.patch.object(rating_explore, "exploration_safe", lambda item: True).start()
        for seed in range(80):
            choice, p, cost = rating_explore.sample_choice(options, result["failure_price"], random.Random(seed))
            if p is not None:
                sampled.add(choice["level"])
                self.assertAlmostEqual(cost, rating.expected_cost(choice["estimated_price"], p, result["failure_price"]))
        self.assertGreaterEqual(len(sampled), 2)


if __name__ == "__main__":
    unittest.main()
