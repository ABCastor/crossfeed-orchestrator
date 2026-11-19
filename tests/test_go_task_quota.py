import json
import tempfile
import unittest
from pathlib import Path

from scripts import evidence, fleetctl, selector


class GoTaskQuotaTests(unittest.TestCase):
    def cost(self, model, requests=None, plan="OpenCode Go", window="rolling_5h"):
        roster = {"quota_pools": {"opencode-go": {"plan": {"name": plan}}}}
        opt = dict(pool="opencode-go", model_key=model, run_as=model, level="high")
        row = {"own_cost": {"opencode-go": {"requests_per_task": requests}}} if requests else {}
        return selector._task_cost(roster, opt, row, {"binding_window": window}, None)[0]

    def test_model_and_plan_weights(self):
        flash = self.cost("deepseek-v4.1-flash")
        kimi = self.cost("kimi-k3.1-pro")
        self.assertAlmostEqual(kimi["percent"] / flash["percent"], 43300 / 147)
        self.assertEqual(kimi["request_count_basis"], "unmeasured_one_request")
        weekly = self.cost("kimi-k3.1-pro", requests=8, plan="OpenCode Go Plus", window="weekly")
        self.assertAlmostEqual(weekly["percent"], 8 * 100 / (588 * 2.5))
        self.assertEqual(weekly["request_count_basis"], "observed_median")
        self.assertTrue(self.cost("kimi-k3.1-pro", plan="Other Plan")["unknown"])

    def test_native_steps_count_tool_turns_without_double_counting_and_train_one_task(self):
        def step(kind, id_, reason=None):
            return {"type": kind, "sessionID": "synthetic", "part": {"id": id_, "reason": reason}}

        model = "kimi-k3.1-pro"
        events = [step("step_start", "a"), step("step_finish", "b", "tool-calls"),
                  step("step_start", "c"), step("step_finish", "d", "tool-calls"),
                  step("step_start", "e"), step("step_start", "e"),
                  {"type": "text", "part": {"text": "final"}}]
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "events"
            p.write_text("\n".join(map(json.dumps, events)))
            lane = {"lane_id": "go", "quota_pool": "opencode-go", "provider": "opencode-go", "model_key": model}
            overlay = {"lanes": [lane]}
            receipt = fleetctl.record_run(Path(root) / "state", overlay, "go", p, None, 0, None, effort="high")
            self.assertTrue(evidence._number(receipt["own_cost"]["opencode-go"]["requests_per_task"]) == 3)
            result = evidence.build_evidence(overlay, {}, [receipt, dict(receipt)])
            row = next(row for row in result["rows"] if row["model_key"] == model)
            self.assertEqual(row["own_cost"]["opencode-go"], {"requests_per_task": 3, "trials": 1})
            self.assertAlmostEqual(self.cost(model, 3)["percent"], 300 / 147)

    def test_v2_end_only_counts_each_model_turn(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "events"
            p.write_text('\n'.join(json.dumps({"type": "session.step.ended", "data": {"finish": finish}})
                                   for finish in ("tool-calls", "tool-calls", "stop")))
            self.assertEqual(fleetctl.parse_events(p)["observed_model_requests"], 3)
