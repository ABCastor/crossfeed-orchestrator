"""Expected failure cost ranks exact efforts without changing admission."""
import unittest
from unittest import mock

from tests import test_selector as fixtures

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
        mock.patch.object(selector.rating_log, 'typical_price', return_value=.230769).start()

    def pick(self, **kwargs):
        return self.select(self.runtime, role='hard-reasoning', allow=f'claude:{self.opus}:*',
                           persist_receipts=False, **kwargs)

    def test_hard_role_ranks_each_admitted_effort(self):
        result = self.pick()
        self.assertEqual(result['rating_stakes'], 'important')
        self.assertTrue(result['hard_reasoning_preference']['floor_exception'])
        self.assertEqual([o['level'] for o in result['top3']], ['xhigh', 'high', 'low'])
        self.assertAlmostEqual(result['choice']['expected_cost'], .250 + (1-.809) * 5 * .230769)
        self.assertEqual(self.pick(rating_stakes='routine')['choice']['level'], 'low')

    def test_absolute_best_is_only_a_stakes_alias(self):
        alias = self.pick(value_mode='absolute-best')
        explicit = self.pick(rating_stakes='critical')
        self.assertEqual(alias['rating_stakes'], 'critical')
        self.assertEqual(alias['choice'], explicit['choice'])
        self.assertEqual(self.pick(value_mode='absolute-best', rating_stakes='routine')['choice']['level'], 'low')

    def test_unknown_price_is_not_implicitly_free(self):
        self.prices['xhigh'] = None
        result = self.pick()
        self.assertEqual(result['choice']['level'], 'high')
        self.assertIsNone(result['top3'][-1]['expected_cost'])


if __name__ == '__main__':
    unittest.main()
