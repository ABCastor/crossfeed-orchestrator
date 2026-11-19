"""AGY effort selectors retain the exact CLI ID and share one model identity."""
import json
from pathlib import Path
import unittest

from scripts import daily_model_refresh as daily, fleetctl, selector


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/access-overlay.example.json'


class AgyXhighTests(unittest.TestCase):
    def setUp(self):
        self.roster = json.loads(EXAMPLE.read_text())
        key = 'gemini-fixture'
        self.key = key
        template = next(lane for lane in self.roster['lanes']
                        if lane['harness'] == 'agy' and lane['quota_pool'] == 'antigravity-gemini')
        self.roster['lanes'] = [dict(template, lane_id=f'{key}-{level}', model_key=key,
                                     selector=f'{key}-{level}', roles=['implementation'],
                                     access_status='verified', admission_status='active')
                                for level in ('xhigh', 'high')]
        self.roster['model_cards'][key] = {'status': 'current'}
        self.roster['effort'][key] = {'levels': {'agy': ['high', 'xhigh']},
                                      'default': 'high', 'knee': 'high', 'by_role': {}}
        policy = self.roster['policy'].get('selector', {})
        policy.pop('allowed_pools_by_role', None)

    def test_catalog_keeps_selector_and_joins_efforts_under_base_key(self):
        ids = [f'{self.key}-{level}' for level in ('low', 'medium', 'high', 'xhigh')]
        rows, error = daily.catalog_rows(['fixture'], listing=(ids, None))
        self.assertIsNone(error)
        self.assertEqual(rows, [dict(selector=name, model_key=self.key) for name in ids])

    def test_refresh_repairs_an_existing_unfunded_split_candidate(self):
        pool = 'antigravity-catalog'
        self.roster['quota_pools'][pool] = {'label': 'Unassigned'}
        only = self.roster['lanes'][0]
        exact = only['selector']
        only.update(quota_pool=pool, model_key=exact, access_status='unverified',
                    admission_status='candidate', roles=[])
        self.roster['lanes'] = [only]
        result, changes = daily.reconcile(self.roster, [dict(
            harness='agy', provider=only['provider'], pool=pool, source='fixture',
            models=[dict(selector=exact, model_key=self.key)])], {})
        lane = result['lanes'][0]
        self.assertEqual((lane['model_key'], lane['selector']), (self.key, exact))
        self.assertEqual((lane['quota_pool'], lane['admission_status'], lane['access_status'], lane['roles']),
                         (pool, 'candidate', 'unverified', []))
        self.assertEqual(changes['added'], [])

    def test_explicit_xhigh_can_resolve_the_same_family_without_a_separate_lane(self):
        self.roster['lanes'] = self.roster['lanes'][1:]
        choice = fleetctl.resolve_effort(self.roster, self.key + '-xhigh', harness='agy', explicit='xhigh')
        self.assertEqual((choice['model_key'], choice['effort']), (self.key, 'xhigh'))

    def test_scored_options_rewrite_xhigh_to_the_effort_actually_advertised(self):
        options, _ = selector.enumerate_options(self.roster, {}, 'implementation', mode='write', fleet=fleetctl)
        ids = {row['level']: row['run_as'] for row in options if row['model_key'] == self.key}
        self.assertEqual(ids, {'high': self.key + '-high', 'xhigh': self.key + '-xhigh'})

    def test_scored_effort_role_allowlist_does_not_relax_read_only_or_affect_explicit_effort(self):
        self.roster['effort'][self.key]['scored_levels_by_role'] = {
            'implementation': ['high'], 'hard-reasoning': ['high', 'xhigh']}
        cases = [('implementation', 'write', {'high'}),
                 ('hard-reasoning', 'write', {'high', 'xhigh'}),
                 ('hard-reasoning', 'read-only', set()), ('default', 'write', set())]
        for role, mode, expected in cases:
            with self.subTest(role=role, mode=mode):
                options, _ = selector.enumerate_options(self.roster, {}, role, mode=mode, fleet=fleetctl)
                self.assertEqual({row['level'] for row in options if row['model_key'] == self.key}, expected)
        self.assertEqual(fleetctl.resolve_effort(self.roster, self.key, 'implementation', 'agy', 'xhigh')['effort'], 'xhigh')
        self.roster['effort'][self.key]['scored_levels_by_role']['implementation'] = ['invalid']
        options, rejected = selector.enumerate_options(self.roster, {}, 'implementation', mode='write', fleet=fleetctl)
        self.assertFalse(any(row['model_key'] == self.key for row in options))
        self.assertTrue(any('no permitted scored effort' in row for row in rejected))

    def test_legacy_route_remaps_xhigh_lane_to_default_high(self):
        self.roster['routing']['roles']['implementation'] = {
            band: [self.key + '-xhigh'] for band in ('quality_first', 'conserve', 'critical')}
        choice = fleetctl.choose_lane(self.roster, {}, 'implementation', 'write', 'text', harness='agy')
        self.assertEqual(choice['selector'], self.key + '-high')


if __name__ == '__main__':
    unittest.main()
