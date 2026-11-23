import io
import json
import tempfile
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path

from scripts import rating
from scripts import rating_replay as replay


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.before = replay.HOLDOUT - timedelta(days=1)
        self.offer = rating.Offer(rating.Route("exact-model", "medium", "pool"), 2)
        self.task = rating.Task("coding", "ordinary", "routine")

    def event(self, id_, moment):
        return replay.Event(id_, moment, self.task, self.offer, (self.offer,), 0)

    def test_quotes_require_independent_quota_and_cash_inputs(self):
        cost = {"percent": 10, "estimated_usd": 0.01}
        capacity = {"weekly_usd_equivalent": 40}
        pool = {"lambda": 1, "window_minutes": 10080}
        quote = {"cost": cost, "capacity": capacity, "pool_price": pool}
        self.assertIsNone(replay.price(quote))
        included = {**quote, "cash": {"funding": "included", "estimate_usd": 9}}
        self.assertEqual(replay.price(included), 8)
        self.assertIsNone(replay.price({**included, "capacity": {}}))
        cash = {"funding": "metered", "estimate_usd": 2}
        self.assertEqual(replay.price({**quote, "cash": cash}), 10)
        self.assertEqual(replay.price({"cash": cash}), 2)
        self.assertIsNone(replay.price({"cash": {"funding": "metered", "api_equivalent_usd": 2}}))

    def test_no_lookahead_same_time_sharing_or_held_out_updates(self):
        e1 = self.event("fit-1", self.before)
        e2 = self.event("fit-2", self.before)
        e3 = self.event("held-1", replay.HOLDOUT)
        e4 = self.event("held-2", replay.HOLDOUT + timedelta(hours=1))
        rows = list(replay.forecast([e4, e3, e2, e1], {
            e1.id: (self.before + timedelta(seconds=1), True),
            e2.id: (replay.HOLDOUT, False),
            e3.id: (replay.HOLDOUT + timedelta(seconds=1), False),
        }))
        self.assertEqual([f.current_p for f in rows[:2]], [0.5, 0.5])
        self.assertTrue(all(f.shadow is None for f in rows[:2]))
        self.assertGreater(rows[2].current_p, 0.5)
        self.assertEqual(rows[2].current_p, rows[3].current_p)
        self.assertEqual([f.checked for f in rows], [True, False, False, None])
        self.assertEqual(rows[2].shadow.price, 2)

    def test_check_join_requires_unique_hashed_proof_for_actual_route(self):
        one = self.event("one", self.before)
        two = self.event("two", self.before)
        three = self.event("three", self.before)
        worker = {"schema": "crossfeed-model-run/v1", "returncode": 0,
                  "actual_model_identity": self.offer.route.model, "effort": self.offer.route.effort,
                  "quota_pool": self.offer.route.subscription}
        proof = {"schema": "afk-attempt/v1", "result": "verified",
                 "ended_at": replay.HOLDOUT.isoformat(), "proof": {"returncode": 0, "output_sha256": "fixture"}}
        inputs = [
            {**worker, "dispatch_id": one.id, "afk_attempt_id": "accepted"},
            {**worker, "dispatch_id": two.id},
            {**worker, "dispatch_id": three.id, "afk_attempt_id": "wrong-model", "actual_model_identity": "other"},
            {**proof, "attempt_id": "accepted"}, {**proof, "attempt_id": "wrong-model"},
            {"evidence_source": "bench.fit", "outcome": {"passed": True}, "dispatch_id": two.id},
        ]
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "runs.jsonl"
            ledger.write_text("\n".join(map(json.dumps, inputs)) + "\n{")
            counts = Counter()
            self.assertEqual(replay.checked_outcomes(ledger, [one, two, three], counts),
                             {one.id: (replay.HOLDOUT, True)})
            self.assertEqual(counts["benchmark fit rows"], 1)
            self.assertEqual(counts["unreadable ledger rows"], 1)
            with ledger.open("a") as handle:
                handle.write('\n' + json.dumps({**worker, "dispatch_id": two.id, "afk_attempt_id": "accepted"}))
            self.assertEqual(replay.checked_outcomes(ledger, [one, two, three], Counter()), {})

    def test_receipt_path_is_contained_and_details_include_abstentions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selections = root / "selections"
            selections.mkdir()
            attempt = root / "attempt"
            attempt.mkdir()
            selection = selections / "selection.json"
            receipt = attempt / "dispatch.json"
            item = {"model_key": "requested", "model_identity": "exact-model", "pool": "pool", "level": "medium"}
            selection.write_text(json.dumps({"choice": item, "top3": [item], "family": "coding-agent", "stakes": "low"}))
            args = {"dispatch_id": "attempt", "started_at": self.before.isoformat(), "returncode": 0}
            receipt.write_text(json.dumps({**args, "selection_file": str(selection)}))
            e = replay.event(receipt, selections)
            self.assertEqual(e.current.route, self.offer.route)
            self.assertEqual(len(e.offers), 1)
            self.assertEqual(e.task, self.task)
            rows = list(replay.forecast([e], {}))
            out = io.StringIO()
            replay.details(rows, out)
            self.assertEqual(len(out.getvalue().splitlines()), 2)
            self.assertIn("no-prior-typical-price", out.getvalue())
            receipt.write_text(json.dumps({**args, "selection_file": str(root / "runs.jsonl")}))
            self.assertIsNone(replay.event(receipt, selections).current)

    def test_calibration_does_not_treat_zero_exit_as_checked_success(self):
        rows = list(replay.forecast([self.event("fit", self.before)], {}))
        self.assertEqual(replay.calibration(rows, lambda f: f.checked), "n=0")
        self.assertIn("predicted 50.0%, observed 100.0%, Brier 0.250", replay.calibration(rows, replay.exit_ok))


if __name__ == "__main__":
    unittest.main()
