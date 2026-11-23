import json
import random
import tempfile
import unittest
from pathlib import Path

from scripts import fleetctl, rating, rating_explore as p, rating_log

LEDGER, DISPATCH, ACCEPTANCE = rating_log.LEDGER, rating_log.DISPATCH, rating_log.ACCEPTANCE


def option(model, price=.15, grade=.7, funding='included', used=45, debit=1, state='ABUNDANT'):
    return {'model_identity': model, 'pool': 'test', 'level': 'high', 'funding': funding,
            'estimated_price': price, 'expected_cost': rating.expected_cost(price, grade, 1),
            'cost': {'percent': debit}, 'pool_price': {'state': state, 'window': {'used_percent': used}},
            'posterior': p.posterior(grade)}


class Trial(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write_rows(self, rows):
        (self.root / LEDGER).write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in rows))

    def test_learning_ignores_exit_deduplicates_checked_task_and_distinguishes_route(self):
        a, b = option('m'), option('m', grade=.8)
        b['level'] = 'low'
        def dispatch(run, live):
            return dict(schema=DISPATCH, run_id=run, task_id='t', returncode=0, live_pick=live)
        def checked(run, verdict):
            return dict(schema=ACCEPTANCE, run_id=run, task_id='t', verdict=verdict, check='independent test')
        self.write_rows([dispatch('a', a), dispatch('b', b), dispatch('retry', a),
                         checked('a', 'fail'), checked('a', 'pass'), checked('retry', 'pass')])
        counts = p.checked_counts(self.root)
        self.assertEqual(counts, {(p.route_key(a), 'fail'): 1})
        alpha, beta = p.posterior(.7, failures=1)
        self.assertAlmostEqual(alpha / (alpha + beta), .56)

    def test_cap_is_enforced_after_every_dispatch(self):
        self.assertFalse(p.budget_allows((5, 0), .15))
        self.assertTrue(p.budget_allows((6, 0), .15))
        total, explored = 0, 0
        for total in range(1, 1001):
            explored += p.budget_allows((total - 1, explored), .15)
            self.assertLessEqual(explored / total, .15)
        self.assertEqual(explored, 150)
        self.assertFalse(p.budget_allows((1000, 0), 0))

    def test_counter_survives_long_rows_and_preserves_latest(self):
        self.assertEqual(p.read_totals(self.root), (0, 0))
        self.write_rows([dict(schema=p.TRIAL, dispatches=8, non_greedy_dispatches=1),
                         dict(schema=DISPATCH, payload='x' * 200000),
                         dict(schema=p.TRIAL, dispatches=14, non_greedy_dispatches=2),
                         dict(schema=DISPATCH, payload='x' * 200000)])
        self.assertEqual(p.read_totals(self.root), (14, 2))

    def test_sampler_excludes_disallowed_routes_and_replays_seed(self):
        best = option('good')
        alternatives = [option('kimi-v9'), option('paid', funding='metered'),
                        option('depleted', used=73, debit=3), option('conserve', state='CONSERVE'),
                        option('beyond-price-band', price=.4), option('legal', price=.19)]
        rows = [best, *alternatives]
        def draws():
            rng = random.Random(62)
            return [p.sample_choice(rows, 1, rng)[0]['model_identity'] for _ in range(100)]
        self.assertEqual(draws(), draws())
        self.assertEqual(set(draws()), {'good', 'legal'})


    def test_dispatch_rechecks_cap_and_persists_sample_vs_mean(self):
        mean, sampled = option('good'), option('alternative', price=.19)
        trial = {'cap': .15, 'mean_pick': p.pick_summary(mean), 'sampled_pick': p.pick_summary(sampled)}
        selection = {'rating_sampling': trial, 'exploration': True, 'choice': sampled, 'top3': [mean],
                     'role': 'default', 'rating_stakes': 'routine'}
        outcomes = [fleetctl._book_rating_trial(selection, self.root, f't{i}', f'r{i}')
                    for i in range(8)]
        self.assertEqual(outcomes, [mean] * 6 + [sampled, mean])
        with (self.root / LEDGER).open('rb') as handle:
            rows = list(rating_log.rows(handle))
        self.assertEqual(rows[-1]['routed_pick']['model_identity'], 'good')
        self.assertEqual(rows[-1]['sampled_pick']['model_identity'], 'alternative')
        self.assertFalse(rows[-1]['applied'])
        self.assertEqual(p.read_totals(self.root), (8, 1))

    def test_cli_can_seed_or_disable_sampling(self):
        args = fleetctl.build_parser().parse_args(
            ['select', '--role', 'review', '--no-explore', '--explore-seed', '6'])
        self.assertTrue(args.no_explore)
        self.assertEqual(args.explore_seed, 6)


if __name__ == '__main__':
    unittest.main()
