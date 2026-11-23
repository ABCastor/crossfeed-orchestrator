"""Exercise the opt-in Haiku policy through real selection and its receipts."""
import copy
import datetime as dt
import json
from pathlib import Path
import unittest

from tests import test_selector as fixture
import fleetctl

ROOT = fixture.ROOT
quality_row = fixture.quality_row


class JudgmentPolicyTests(unittest.TestCase):
    evidence = fixture.SelectorTests.evidence
    select = fixture.SelectorTests.select

    # BR-SELECT-001
    def setUp(self):
        fixture.SelectorTests.setUp(self)
        fixture.SelectorTests.only(self, ['claude'])
        self.roster['policy']['selector']['exploration_cap'] = 0
        self.haiku = 'claude-haiku-5-5'
        self.roster['model_cards'][self.haiku] = {
            'pool': 'claude', 'status': 'current', 'aliases': ['haiku']}
        self.roster['effort'][self.haiku] = copy.deepcopy(
            self.roster['effort']['claude-sonnet-5-5'])
        self.roster['effort'][self.haiku].pop('refuse_roles', None)
        fixture.SelectorTests.one_level(self, 'claude')
        self.fragment = json.loads((ROOT / 'examples/haiku-judgment-policy.json').read_text())
        self.rows = [quality_row(self.haiku, mean=.99),
                     quality_row('claude-sonnet-5-5', mean=.6)]
        self.evidence(self.rows)

    def apply_policy(self):
        for model, patch in self.fragment['effort'].items():
            self.roster['effort'][model].setdefault('refuse_roles', {}).update(patch['refuse_roles'])

    # BR-SELECT-001: control, the real selector can choose Haiku without policy.
    def test_missing_policy_reproduces_haiku_review(self):
        result = self.select()
        self.assertEqual(result['choice']['model_key'], self.haiku)
        receipt = json.loads(Path(result['selection_file']).read_text())
        self.assertEqual(receipt['choice']['model_key'], self.haiku)

    # BR-SELECT-001
    def test_judgment_roles_exclude_haiku_at_every_stakes_and_in_receipts(self):
        self.apply_policy()
        roles = ('review', 'reviewer', 'audit', 'second-opinion', 'audit-retry',
                 'hard-reasoning', 'judgment')
        for patch in self.fragment['effort'].values():
            self.assertEqual(set(patch['refuse_roles']), set(roles))
        for role in roles:
            family = 'reasoning' if role in ('hard-reasoning', 'judgment') else 'review'
            self.evidence([quality_row(self.haiku, mean=.99, family=family),
                           quality_row('claude-sonnet-5-5', mean=.6, family=family)])
            for stakes in ('low', 'normal', 'high', 'irreversible'):
                with self.subTest(role=role, stakes=stakes):
                    result = self.select(role=role, stakes=stakes)
                    self.assertEqual(result['choice']['model_key'], 'claude-sonnet-5-5')
                    self.assertTrue(any('Haiku is never used for judgment' in r for r in result['rejected']))
                    for option in result['top3']:
                        self.assertNotEqual(option['model_key'], self.haiku)
                        receipt = json.loads(Path(option['selection_file']).read_text())
                        self.assertNotEqual(receipt['choice']['model_key'], self.haiku)

    # BR-SELECT-001
    def test_allow_and_target_cannot_readmit_haiku_for_review(self):
        self.apply_policy()
        with self.assertRaisesRegex(fleetctl.FleetError, 'Haiku is never used for judgment'):
            self.select(allow=f'claude:{self.haiku}:*')
        with self.assertRaisesRegex(fleetctl.FleetError, 'no eligible selector option'):
            self.select(target=('claude', self.haiku))

    # BR-SELECT-001: control, cheap read roles still admit Haiku.
    def test_nonjudgment_reads_keep_haiku_available(self):
        self.apply_policy()
        for role, family in [('probe', 'repo-qa'), ('classification', 'extraction'),
                             ('filter', 'extraction'), ('research-scout', 'research')]:
            with self.subTest(role=role):
                self.evidence([quality_row(self.haiku, mean=.99, family=family),
                               quality_row('claude-sonnet-5-5', mean=.6, family=family)])
                self.assertEqual(self.select(role=role)['choice']['model_key'], self.haiku)

    # BR-SELECT-001
    def test_legacy_haiku_is_refused_but_can_still_probe(self):
        legacy = 'claude-haiku'
        self.roster['model_cards'][legacy] = {'pool': 'claude', 'status': 'current'}
        self.apply_policy()
        with self.assertRaisesRegex(fleetctl.FleetError, 'Haiku is never used for judgment'):
            self.select(allow=f'claude:{legacy}:*')
        self.evidence([quality_row(legacy, level='provider-default', family='repo-qa')])
        self.assertEqual(self.select(role='probe', allow=f'claude:{legacy}:*')['choice']['model_key'], legacy)

    # BR-SELECT-001
    def test_checked_exploration_cannot_choose_refused_model(self):
        self.roster['policy']['selector']['exploration_cap'] = 1
        self.roster['quota_pools']['claude']['value_capacity'] = {
            'weekly_usd_equivalent': 46, 'source': 'synthetic measured quota'}
        models = (self.haiku, 'claude-sonnet-5-5', 'claude-opus-5-5')
        self.evidence([dict(quality_row(model), own_cost={'claude': {'percent_per_task': .1}})
                       for model in models])
        now = fleetctl.utc_now()
        runtime = {'quota_snapshots': {'claude': {'observed_at': fleetctl.iso(now), 'windows': {'weekly': {
            'used_percent': 10, 'window_minutes': 10080, 'projected_used_percent_at_reset': 20,
            'reset_at': fleetctl.iso(now + dt.timedelta(days=2))}}}}}
        allow = ','.join(f'claude:{model}:high' for model in models)
        def picks():
            return [self.select(runtime, stakes='low', allow=allow, explore_seed=seed) for seed in range(20)]
        self.assertTrue(any(result['rating_sampling']['sampled_pick']['model_key'] == self.haiku
                            for result in picks()))  # control: the sampler can pick Haiku without the rule.
        self.apply_policy()
        results = picks()
        self.assertTrue(any(result['exploration'] for result in results))
        for result in results:
            self.assertNotEqual(result['choice']['model_key'], self.haiku)
            self.assertTrue(all(o['model_key'] != self.haiku for o in result['top3']))


if __name__ == '__main__':
    unittest.main()
