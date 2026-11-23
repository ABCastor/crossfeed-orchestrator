"""BR-SELECT-003: benchmark pass rates seed a weak, checked-outcome prior."""
import math
import unittest

from scripts import rating, rating_explore
from scripts.success_evidence import SuccessEvidence


def terminal(percent):
    return dict(source='terminal_bench', mapping='aa_terminal', metric='terminal_bench_4',
                value=percent, weight=.8, url='https://www.tbench.ai/', prior_mean=.99)


def index(score):
    return dict(source='artificial_analysis', mapping='aa_v5_coding_proxy',
                metric='artificial_analysis_intelligence_index', index_version=5, value=score, weight=.3)


class SuccessEvidenceTests(unittest.TestCase):
    # BR-SELECT-003
    def test_ordinary_estimate_keeps_raw_failure_ratio_not_grade_rank(self):
        data = SuccessEvidence({('small', 'max'): {'sources': [terminal(25)]},
                                ('frontier', 'max'): {'sources': [terminal(60)]}})
        small = rating.success_prior(data.rate('small', 'max', 'coding-agent')[0])
        frontier = rating.success_prior(data.rate('frontier', 'max', 'coding-agent')[0])
        self.assertEqual(small, .25)
        self.assertEqual(frontier, .60)
        self.assertAlmostEqual((1 - small) / (1 - frontier), 1.875)
        hard = rating.success_prior(frontier, 'hard')
        self.assertAlmostEqual(hard / (1 - hard), frontier / (1 - frontier) * math.exp(-.7))
        self.assertGreater(rating.success_prior(frontier, 'easy'), frontier)

    # BR-SELECT-003
    def test_role_prefers_relevant_raw_results_and_marks_terminal_transfer(self):
        lcr = dict(source='artificial_analysis', mapping='aa_lcr', metric='lcr', value=.8, weight=.6)
        data = SuccessEvidence({('model', 'high'): {'sources': [terminal(60), lcr]}})
        self.assertEqual(data.rate('model', 'high', 'coding-agent')[:2], (.60, 'benchmark'))
        self.assertEqual(data.rate('model', 'high', 'research')[:2], (.80, 'benchmark'))
        self.assertEqual(data.rate('model', 'high', 'review')[:2], (.60, 'role_proxy_benchmark_estimate'))
        no_research = SuccessEvidence({('model', 'high'): {'sources': [terminal(60)]}})
        self.assertEqual(no_research.rate('model', 'high', 'research')[:2], (.60, 'cross_family_benchmark_estimate'))

    # BR-SELECT-003
    def test_nearest_same_model_beats_more_distant_and_different_model_donors(self):
        data = SuccessEvidence({('model', 'low'): {'sources': [terminal(20)]},
                                ('model', 'xhigh'): {'sources': [terminal(58)]},
                                ('other', 'high'): {'sources': [terminal(95)]}})
        rate, basis, source = data.rate('model', 'high', 'coding-agent')
        self.assertGreater(rate, .20)
        self.assertLessEqual(rate, .58)
        self.assertEqual(basis, 'sibling_benchmark_estimate')
        self.assertEqual((source['effort'], source['metric']), ('xhigh', 'terminal_bench_4'))
        self.assertEqual(data.rate('model', 'max', 'coding-agent')[0], .58)

    # BR-SELECT-003
    def test_index_fit_targets_pass_rates_and_is_explicitly_not_an_exact_measurement(self):
        rows = {(model, 'high'): {'sources': [index(score), terminal(rate)]}
                for model, score, rate in [('a', 20, 10), ('b', 40, 40), ('c', 60, 70)]}
        rows['d', 'high'] = {'sources': [index(50)]}
        rows['d', 'max'] = {'sources': [index(90)]}
        data = SuccessEvidence(rows)
        rate, basis, evidence = data.rate('d', 'high', 'coding-agent')
        self.assertAlmostEqual(rate, .55)
        self.assertEqual(basis, 'aa_v5_terminal_fit_estimate')
        self.assertEqual(evidence['fit_samples'], 3)
        rate, _, evidence = data.rate('d', 'max', 'coding-agent')
        self.assertAlmostEqual(rate, .70)
        self.assertTrue(evidence['outside_fit_range'])
        self.assertEqual(data.rate('unmeasured', 'low', 'coding-agent')[:2], (.10, 'observed_low_benchmark_estimate'))

    # BR-SELECT-002, BR-SELECT-003
    def test_checked_outcomes_shift_prior_without_becoming_benchmarks(self):
        data = SuccessEvidence({('model', 'high'): {'sources': [terminal(60)]}})
        prior = rating.success_prior(data.rate('model', 'high', 'coding-agent')[0])
        alpha, beta = rating_explore.posterior(prior, passes=1, failures=0)
        self.assertAlmostEqual(alpha / (alpha + beta), .68)
        alpha, beta = rating_explore.posterior(prior, passes=0, failures=1)
        self.assertAlmostEqual(alpha / (alpha + beta), .48)
        self.assertEqual(data.rate('model', 'high', 'coding-agent')[0], .60)


if __name__ == '__main__':
    unittest.main()
