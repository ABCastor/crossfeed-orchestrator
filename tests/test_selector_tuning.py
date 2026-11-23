"""High-stakes routing regressions; no provider calls or private evidence."""
import unittest
from unittest import mock
from tests.test_selector import SelectorTests, quality_row
import selector


class TuningTests(SelectorTests):
    def test_allow_namespace_model_keeps_identity_and_wildcard(self):
        self.only(['codex'])
        options, rejected = selector.enumerate_options(self.roster, {}, 'review', fleet=selector._fleet(None))
        option = next(o for o in options if o['pool']=='codex' and o['level']=='high')
        option = dict(option, model_key='chatgpt:latest-high')
        self.evidence([quality_row(option['model_key'])])
        for level in ['high', '*']:
            allowed = 'codex:chatgpt:latest-high:'+level
            with self.subTest(level=level), mock.patch.object(selector, 'enumerate_options', return_value=([option], rejected)):
                result = self.select(allow=allowed)
                self.assertEqual(result['choice']['model_key'], option['model_key'])
                self.assertEqual(result['allow'], [allowed])

    # BR-SELECT-003
    def test_critical_stakes_do_not_hide_extreme_price_pressure(self):
        self.only(['codex'])
        self.roster['quota_pools']['codex']['value_capacity'] = {
            'weekly_usd_equivalent': 46, 'source': 'synthetic weekly estimate'}
        rows = [quality_row('gpt-6.1-sol', mean=.92), quality_row('gpt-6-luna', mean=.75)]
        for row, debit in zip(rows, (10, .01)):
            row['own_cost'] = {'codex': {'percent_per_task': debit}}
        self.evidence(rows)
        runtime = self.snapshot('codex', projected=190, used=50)
        allowed = 'codex:gpt-6.1-sol:high,codex:gpt-6-luna:high'
        with mock.patch.object(selector.rating_log, 'typical_price', return_value=.23):
            result = self.select(runtime, stakes='high', rating_stakes='critical', allow=allowed, no_explore=True)
        cheap, expensive = result['top3']
        self.assertEqual(cheap['model_key'], 'gpt-6-luna')
        self.assertGreater(expensive['p_success'], cheap['p_success'])
        self.assertGreater(expensive['estimated_price'] - cheap['estimated_price'],
                           result['failure_price'] * (expensive['p_success'] - cheap['p_success']))

    def test_hard_evidence_is_not_drowned_by_easy_successes(self):
        self.only(['codex'])
        rows = [quality_row('gpt-6.1-sol', mean=.95), quality_row('gpt-6-luna', mean=.99)]
        rows[0]['q_by_stakes'] = {'high': {'review': {'mean': .9, 'sd': .05, 'n_sources': 1}}}
        rows[1]['q_by_stakes'] = {'high': {'review': {'mean': .7, 'sd': .08, 'n_sources': 1}}}
        self.evidence(rows)
        allowed = 'codex:gpt-6.1-sol:high,codex:gpt-6-luna:high'
        self.assertEqual(self.select(stakes='high', allow=allowed)['choice']['model_key'], 'gpt-6.1-sol')
        self.assertEqual(self.select(stakes='irreversible', allow=allowed)['choice']['model_key'], 'gpt-6.1-sol')
        self.assertEqual(self.select(stakes='normal', allow=allowed)['choice']['model_key'], 'gpt-6-luna')

    def test_no_known_limit_is_unpriced_usage_not_dollars(self):
        cost, flags = selector._task_cost({}, {'pool': 'chatgpt-work'},
            quality_row('chatgpt:latest-high', price=99), {'projection_basis': 'none-known'}, 1)
        self.assertIsNone(cost['estimated_usd'])
        self.assertIsNone(cost['percent'])
        self.assertTrue(cost['unknown'])
        self.assertEqual(cost['penalty_percent'], 0)
        self.assertIn('chat_usage_unpriced', flags)

    def test_no_quota_limit_does_not_erase_paid_api_pricing(self):
        cost, flags = selector._task_cost({}, {'pool': 'paid-api'},
            quality_row('api-model', price=99), {'projection_basis': 'none-known'}, 1)
        self.assertEqual(cost['estimated_usd'], .198)
        self.assertEqual(cost['penalty_percent'], 0)
        self.assertNotIn('chat_usage_unpriced', flags)

    # BR-SELECT-002
    def test_exploration_cap_rejects_invalid_configuration(self):
        self.only(['codex'])
        self.roster['policy']['selector']['exploration_cap'] = float('nan')
        with self.assertRaisesRegex(Exception, 'exploration_cap'):
            self.select(stakes='high')

    # BR-SELECT-003
    def test_high_stakes_records_completion_time_without_using_it_as_price(self):
        self.only(['codex'])
        rows = [quality_row('gpt-6.1-sol', mean=.9, latency=1),
                quality_row('gpt-6-luna', mean=.9, latency=100)]
        rows[0]['latency_by_stakes'] = {'high': 800}
        rows[1]['latency_by_stakes'] = {'high': 10}
        self.evidence(rows)
        allowed = 'codex:gpt-6.1-sol:high,codex:gpt-6-luna:high'
        normal = self.select(stakes='normal', allow=allowed)
        high = self.select(stakes='high', allow=allowed)
        self.assertEqual(normal['choice']['model_key'], high['choice']['model_key'])
        self.assertEqual({i['model_key']: i['latency_s'] for i in high['top3']},
                         {'gpt-6.1-sol': 800, 'gpt-6-luna': 10})


if __name__ == '__main__':
    unittest.main()
