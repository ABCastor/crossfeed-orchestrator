"""Preferences change real routing and lease admission, never model eligibility."""
import copy
import tempfile
import unittest
from pathlib import Path

from tests.test_levels import fleetctl, roster


class ModelPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.roster = roster()
        self.runtime = {}

    def choose(self, role='implementation', **kwargs):
        return fleetctl.choose_lane(self.roster, self.runtime, role, 'read-only', 'text', **kwargs)['lane_id']

    def test_legacy_prefer_and_avoid_are_enabled_without_reordering(self):
        for model, value in [('deepseek-v4-flash', 'prefer'), ('kimi-k2.7-code', 'avoid')]:
            self.runtime = {'model_preferences': {model: value}}
            self.assertEqual(fleetctl.model_preference(self.runtime, model), 'normal')
            self.assertEqual(self.choose(), 'k27')
            self.assertEqual(self.choose('default'), 'k3')

    def test_only_on_and_off_can_be_written(self):
        for value in ('prefer', 'avoid', 'turbo'):
            with self.assertRaises(ValueError):
                fleetctl.set_model_preference(self.runtime, 'kimi-k3', value)
        fleetctl.set_model_preference(self.runtime, 'kimi-k3', 'off')
        fleetctl.set_model_preference(self.runtime, 'kimi-k3', 'normal')
        self.assertEqual(self.runtime['model_preferences'], {})

    def test_off_model_never_routes_even_forced_or_one_shot(self):
        fleetctl.set_model_preference(self.runtime, 'kimi-k3', 'off')
        fleetctl.set_model_preference(self.runtime, 'kimi-k2.7-code', 'off')
        fleetctl.set_pool_level(self.runtime, 'opencode-go', 'forced')
        for one_shot in (True, False):
            self.assertEqual(self.choose('default', one_shot=one_shot), 'flash')
        fleetctl.set_model_preference(self.runtime, 'deepseek-v4-flash', 'off')
        with self.assertRaises(fleetctl.FleetError):
            self.choose()

    def test_off_applies_to_all_lanes_and_direct_lease_acquisition(self):
        duplicate = copy.deepcopy(self.roster['lanes'][1])
        duplicate['lane_id'] = 'another-k27'
        self.roster['lanes'].append(duplicate)
        self.roster['routing']['roles']['implementation']['quality_first'].insert(0, duplicate['lane_id'])
        fleetctl.set_model_preference(self.runtime, 'kimi-k2.7-code', 'off')
        self.assertEqual(self.choose(), 'flash')
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            fleetctl.atomic_json(state / 'runtime.json', self.runtime)
            for lane_id in ('k27', 'another-k27'):
                with self.assertRaisesRegex(fleetctl.FleetError, 'switched off'):
                    fleetctl.acquire_lease(state, self.roster, lane_id, 60)

    def test_preference_cannot_admit_a_retired_model(self):
        self.runtime.setdefault('model_preferences', {})['deepseek-v4-flash'] = 'prefer'
        self.roster['lanes'][2]['admission_status'] = 'retired'
        self.assertEqual(self.choose(), 'k27')

    def test_low_cost_order_beats_preference(self):
        self.roster['lanes'][1]['cost_rank'] = 2
        self.roster['lanes'][2]['cost_rank'] = 1
        fleetctl.set_pool_level(self.runtime, 'opencode-go', 'low')
        self.runtime.setdefault('model_preferences', {})['kimi-k2.7-code'] = 'prefer'
        self.assertEqual(self.choose(), 'flash')

    def test_brief_uses_one_line_and_inventory_includes_unroutable_models(self):
        self.roster['model_evidence'] = {'orphan': {'status': 'unproven'}}
        self.roster['catalogue_only'] = [{'model_key': 'old', 'selector': 'opencode/old', 'reason': 'retired'}]
        fleetctl.set_model_preference(self.runtime, 'kimi-k3', 'off')
        self.runtime.setdefault('model_preferences', {})['deepseek-v4-flash'] = 'prefer'
        with tempfile.TemporaryDirectory() as directory:
            overview = fleetctl.fleet_overview(self.roster, self.runtime, Path(directory))
        self.assertEqual({m['model'] for m in overview['models']}, {'kimi-k3', 'kimi-k2.7-code', 'deepseek-v4-flash', 'orphan', 'old'})
        lines = [line for line in fleetctl.render_brief(overview).splitlines() if line.startswith('off:')]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], 'off: opencode-go/kimi-k3 -> select by role')

    def test_legacy_settings_and_unknown_preferences_preserve_order(self):
        self.runtime = {'switches': {'opencode-go': 'on'}, 'model_preferences': {'kimi-k2.7-code': 'invalid'}}
        self.assertEqual(self.choose(), 'k27')


if __name__ == '__main__':
    unittest.main()
