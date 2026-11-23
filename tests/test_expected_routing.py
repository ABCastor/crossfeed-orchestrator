"""Expected failure cost ranks exact efforts without changing admission."""
import json
import unittest
from unittest import mock

from tests import test_selector as fixtures
import rating_log

selector = fixtures.selector
fleet = fixtures.fleetctl


class ExpectedRouting(unittest.TestCase):
    evidence = fixtures.SelectorTests.evidence
    select = fixtures.SelectorTests.select

    def setUp(self):
        fixtures.SelectorTests.setUp(self)
        self.roster['policy']['selector'] = {
            'prefer_opus_hard_reasoning': True,
            'quality_floors_by_role': {'hard-reasoning': .85},
        }
        self.opus = 'claude-opus-5-5'
        self.roster['effort'][self.opus]['levels']['claude'] = ['low', 'high', 'xhigh']
        self.prices = {'low': .093, 'high': .170, 'xhigh': .250}
        self.runtime = fixtures.claude_binding_fixture(fleet.utc_now())
        self.evidence([fixtures.quality_row(self.opus, level, grade, family='reasoning')
                       for level, grade in [('low', .383), ('high', .596), ('xhigh', .809)]])
        mock.patch.object(selector, 'routing_price', lambda item: self.prices[item['level']]).start()
        mock.patch.object(selector.rating, 'COMPETENT_RERUN_PRICE', .230769).start()

    def pick(self, **kwargs):
        return self.select(self.runtime, role='hard-reasoning', allow=f'claude:{self.opus}:*',
                           persist_receipts=False, **kwargs)

    def test_hard_role_ranks_each_admitted_effort(self):
        result = self.pick()
        self.assertEqual(result['rating_stakes'], 'important')
        self.assertTrue(result['hard_reasoning_preference']['floor_exception'])
        self.assertEqual({o['level'] for o in result['top3']}, {'xhigh', 'high', 'low'})
        critical = self.pick(rating_stakes='critical')
        self.assertEqual(critical['choice']['level'], 'xhigh')
        self.assertIn(result['choice']['level'], ('high', 'xhigh', 'max'))
        p = selector.rating.success_prior(.809, 'hard')
        self.assertAlmostEqual(critical['choice']['expected_cost'], .250 + (1-p) * 20 * .230769)
        self.assertEqual(self.pick(rating_stakes='routine')['choice']['level'], 'low')

    def test_absolute_best_is_only_a_stakes_alias(self):
        alias = self.pick(value_mode='absolute-best')
        explicit = self.pick(rating_stakes='critical')
        self.assertEqual(alias['rating_stakes'], 'critical')
        self.assertEqual(alias['choice'], explicit['choice'])
        self.assertEqual(self.pick(value_mode='absolute-best', rating_stakes='routine')['choice']['level'], 'low')

    def test_unknown_price_is_not_implicitly_free(self):
        self.prices['xhigh'] = None
        result = self.pick(rating_stakes='critical')
        self.assertEqual(result['choice']['level'], 'high')
        self.assertIsNone(result['top3'][-1]['expected_cost'])


class HardEffortStability(unittest.TestCase):
    evidence = fixtures.SelectorTests.evidence
    select = fixtures.SelectorTests.select

    def setUp(self):
        fixtures.SelectorTests.setUp(self)
        opus = 'claude-opus-5-5'
        self.roster['policy']['selector'] = {
            'prefer_opus_hard_reasoning': True,
            'quality_floors_by_role': {'hard-reasoning': .85},
        }
        self.roster['quota_pools']['claude']['value_capacity'] = {
            'weekly_usd_equivalent': 200 * 12 / 52, 'source': 'synthetic reference subscription',
        }
        levels = [('low', .383, .20), ('medium', .532, .30), ('high', .596, .37),
                  ('xhigh', .809, .54), ('max', .830, .82)]
        self.roster['effort'][opus]['levels']['claude'] = [level for level, _, _ in levels]
        self.evidence([dict(fixtures.quality_row(opus, level, grade, family='reasoning'),
                            own_cost={'claude': {'percent_per_task': debit}})
                       for level, grade, debit in levels])
        self.args = dict(role='hard-reasoning', allow=f'claude:{opus}:*', rating_stakes='important',
                         no_explore=True, persist_receipts=False, collect_eligible=True)

    def test_admitted_opus_does_not_drop_below_high_at_important_stakes(self):
        runtime = fixtures.claude_binding_fixture(fleet.utc_now())
        window = runtime['quota_snapshots']['claude']['windows']['secondary']
        prices = []
        for used in range(20, 86, 5):
            with self.subTest(weekly_used=used):
                window.update(used_percent=used, projected_used_percent_at_reset=used + 10)
                result = self.select(runtime, **self.args)
                self.assertEqual(result['hard_reasoning_preference']['route'], 'opus')
                self.assertIn(result['choice']['level'], ('high', 'xhigh', 'max'))
                self.assertEqual(result['typical_price_basis'], 'competent_rerun_reference')
                self.assertAlmostEqual(result['failure_price'], 5 * .005 * (200 * 12 / 52))
                low = next(item for item in result['eligible_options'] if item['level'] == 'low')
                self.assertIn(low['cost']['basis'], ('measured', 'relative_proxy'))
                prices.append(low['estimated_price'])
        self.assertGreater(prices[-1], prices[0])

    def test_rerun_price_does_not_learn_from_cheap_first_attempts_or_menu(self):
        runtime = fixtures.claude_binding_fixture(fleet.utc_now())
        before = self.select(runtime, **self.args)
        cheap = {'schema': rating_log.DISPATCH, 'attempt_index': 0,
                 'live_pick': {'pricing_basis': selector.rating.LIVE_PRICE_BASIS, 'estimated_price': .0126}}
        (self.state / rating_log.LEDGER).write_text((json.dumps(cheap) + "\n") * 2)
        after = self.select(runtime, **self.args)
        narrowed = self.select(runtime, **dict(self.args, allow=self.args['allow'].replace(':*', ':xhigh')))
        self.assertEqual(before['failure_price'], after['failure_price'])
        self.assertEqual(before['failure_price'], narrowed['failure_price'])
        self.assertEqual(before['choice'], after['choice'])

    def test_hard_grade_gap_preserves_the_neutral_anchor(self):
        prior = selector.rating.success_prior
        self.assertAlmostEqual(prior(.5, 'ordinary'), .825)
        self.assertEqual(prior(None, 'hard'), prior(.5, 'hard'))
        self.assertGreater(prior(.809, 'hard') - prior(.383, 'hard'), .25)
        self.assertGreater(prior(.809, 'hard') - prior(.383, 'hard'), prior(.809) - prior(.383))


if __name__ == '__main__':
    unittest.main()
