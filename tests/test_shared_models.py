"""Shared model evidence, with real admission and isolated synthetic state."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import evidence
import fleetctl
import run_identity
import selector


def identity_card(target='gpt-6-luna'):
    return {'status': 'current', 'identity': {'model_key': target,
            'evidence': 'Synthetic dated provider catalog proving the exact release',
            'verified_on': '2026-10-13'}}


def proof(run, model, pool='codex', passed=True, level='high', **kwargs):
    return {'run_id': run, 'model_key': model, 'effort': level, 'role': 'review',
            'started_at': '2026-10-13T12:00:00Z',
            'pool': pool, 'harness': 'codex' if pool == 'codex' else 'opencode',
            'outcome': {'mechanically_verified': True, 'passed': passed}, **kwargs}


class SharedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.roster = {'model_cards': {'gpt-6-luna': {'status': 'current'},
                                      'go-luna': identity_card()},
                       'effort': {key: {'levels': {'codex': ['high', 'low']}}
                                  for key in ('gpt-6-luna', 'go-luna')},
                       'lanes': [{'model_key': 'go-luna', 'selector': 'opencode-go/luna-exact'}]}

    def build(self, records, catalog=None):
        return evidence.build_evidence(self.roster, catalog or {}, records, '2026-10-13')

    def row(self, result, key='gpt-6-luna', level='high'):
        return next(r for r in result['rows'] if r['model_key'] == key and r['level'] == level)

    def test_two_sources_share_quality_tokens_and_keep_debits_and_latency_separate(self):
        records = [proof('a', 'gpt-6-luna', tokens={'input': 100, 'output': 10}, ttfa_s=1,
                         own_cost={'percent_per_task': 2}),
                   proof('b', 'opencode-go/luna-exact', pool='opencode-go', passed=False,
                         tokens={'input': 300, 'output': 30}, ttfa_s=9,
                         own_cost={'percent_per_task': 7})]
        result = self.build(records)
        row = self.row(result)
        self.assertEqual(row['own']['review'], {'trials': 2, 'passes': 1})
        self.assertEqual(row['tokens_per_task'], {'in': 200, 'out': 20})
        self.assertEqual(row['own_cost']['codex']['percent_per_task'], 2)
        self.assertEqual(row['own_cost']['opencode-go']['percent_per_task'], 7)
        self.assertEqual(row['source_telemetry']['codex/codex']['latency_s'], 1)
        self.assertEqual(row['source_telemetry']['opencode-go/opencode']['latency_s'], 9)
        self.assertFalse(any(r['model_key'] == 'go-luna' for r in result['rows']))
        self.assertTrue(self.row(result, level='low')['q']['review']['unknown'])

    def test_linked_identity_usage_and_repeated_proof_count_once(self):
        records = [proof('one', 'go-luna'), proof('one', 'gpt-6-luna'),
                   {'run_id': 'one', 'tokens': {'input': 40, 'output': 20}},
                   {'run_id': 'one', 'tokens': {'input': 40, 'output': 20}}]
        row = self.row(self.build(records))
        self.assertEqual(row['own']['review']['trials'], 1)
        self.assertEqual(row['tokens_per_task']['in'], 40)

    def test_external_alias_catalog_is_shared_and_duplicate_metrics_are_one_source(self):
        variant = {'slug': 'gpt-6-luna-high', 'effort': 'high', 'release_date': '2026-10-01',
                   'evaluations': {'terminalbench_v4_0': .8}}
        catalog = {'artificial_analysis': {key: {'levels': {'high': variant}}
                   for key in ('go-luna', 'gpt-6-luna')}}
        row = self.row(self.build([], catalog))
        self.assertFalse(row['q']['review']['unknown'])
        self.assertEqual(row['q']['review']['n_sources'], 1)
        self.assertEqual(len(row['q']['review']['sources']), 1)

    def test_unproven_aliases_different_versions_and_routers_stay_separate(self):
        for alias, card in [('no-proof', {'identity': {'model_key': 'gpt-6-luna'}}),
                            ('different-release', identity_card('missing-release')),
                            ('auto', identity_card())]:
            self.roster['model_cards'][alias] = card
        for key in ('no-proof', 'different-release', 'auto', 'vendor/gpt-6-luna', 'gpt-6-luna-20261001'):
            with self.subTest(key=key):
                row = self.row(self.build([proof('one', key)]))
                self.assertEqual(row['own']['review']['trials'], 0)

    def test_ambiguous_selector_does_not_choose_a_card_by_order(self):
        self.roster['lanes'].append({'model_key': 'other-release', 'selector': 'opencode-go/luna-exact'})
        row = self.row(self.build([proof('one', 'opencode-go/luna-exact')]))
        self.assertEqual(row['own']['review']['trials'], 0)

    def test_snapshot_conflict_and_alias_chain_do_not_share(self):
        self.roster['model_cards']['gpt-6-luna']['served_snapshot'] = '2026-10-01'
        self.roster['model_cards']['go-luna']['served_snapshot'] = '2026-10-02'
        row = self.row(self.build([proof('one', 'go-luna')]))
        self.assertEqual(row['own']['review']['trials'], 0)
        self.roster['model_cards']['chain'] = identity_card('go-luna')
        row = self.row(self.build([proof('two', 'chain')]))
        self.assertEqual(row['own']['review']['trials'], 0)

    def test_named_dynamic_and_unconfirmed_relay_routes_do_not_share(self):
        for route in [{'selector': 'auto'}, {'harness': 'chatgpt-chat'},
                      {'chatgpt_pi': True}, {'provider_identity_unconfirmed': True},
                      {'selector': 'openrouter/auto'}, {'selector': 'provider:auto'},
                      {'selector': 'named-router', 'dynamic_model': True}]:
            with self.subTest(route=route):
                self.roster['lanes'][0] = dict(model_key='go-luna', **route)
                result = self.build([proof('one', 'go-luna')])
                self.assertEqual(self.row(result)['own']['review']['trials'], 0)
                self.assertEqual(self.row(result, 'go-luna')['own']['review']['trials'], 1)

    def test_historical_and_unconfirmed_run_provenance_cannot_be_relabelled(self):
        cases = [{'started_at': '2026-09-01T00:00:00Z'}, {'started_at': None},
                 {'provider_identity_unconfirmed': True, 'model_identity': 'gpt-6-luna'}]
        for fields in cases:
            with self.subTest(fields=fields):
                result = self.build([proof('one', 'go-luna', **fields)])
                self.assertEqual(self.row(result)['own']['review']['trials'], 0)
                indexed = run_identity.evidence_index(self.roster, result['rows'])
                self.assertEqual(indexed['gpt-6-luna', 'high']['own']['review']['trials'], 0)
                self.assertEqual(indexed['go-luna', 'high']['own']['review']['trials'], 1)

    def test_recorded_identity_survives_a_later_alias_retarget(self):
        self.roster['model_cards']['other-release'] = {'status': 'current'}
        self.roster['model_cards']['go-luna'] = identity_card('other-release')
        row = self.row(self.build([proof('one', 'go-luna', model_identity='gpt-6-luna')]))
        self.assertEqual(row['own']['review']['trials'], 1)

    def test_conflicting_snapshots_cannot_merge_even_when_both_sets_match(self):
        self.roster['lanes'] = [{'model_key': key, 'served_snapshot': snapshot}
                                for key in ('go-luna', 'gpt-6-luna')
                                for snapshot in ('2026-10-01', '2026-10-02')]
        self.assertEqual(self.row(self.build([proof('one', 'go-luna')]))['own']['review']['trials'], 0)

    def test_provider_drift_does_not_train_the_selected_model(self):
        # A declared different release is backend drift, not an unmatched name.
        self.roster['model_cards']['provider/other-release'] = {'status': 'current'}
        result = self.build([proof('one', 'gpt-6-luna', selected_model='gpt-6-luna',
                                   actual_model='provider/other-release', identity_source='provider')])
        self.assertEqual(self.row(result)['own']['review']['trials'], 0)
        self.assertEqual(self.row(result, 'provider/other-release')['own']['review']['trials'], 1)

    def test_unmatched_provider_name_falls_back_with_provenance(self):
        reported = 'opencode-go/luna-exact-20261001'
        for fields in [{}, {'actual_model_identity': reported}]:
            with self.subTest(fields=fields):
                result = self.build([proof('one', 'go-luna', actual_model=reported,
                                           identity_source='provider', **fields)])
                row = self.row(result)
                self.assertEqual(row['own']['review']['trials'], 1)
                self.assertEqual(row['provider_reported_models'], [reported])
                self.assertIn('model_identity_unconfirmed', row['flags'])
                self.assertFalse(any(r['model_key'] == reported for r in result['rows']))

    def test_registered_snapshots_and_exact_selectors_remain_distinct(self):
        snapshot = 'opencode-go/luna-exact-20261001'
        self.roster['model_cards'][snapshot] = {'status': 'current'}
        result = self.build([proof('one', 'go-luna', actual_model=snapshot, identity_source='provider'),
                             proof('two', 'go-luna', actual_model='opencode-go/luna-exact',
                                   identity_source='provider')])
        self.assertEqual(self.row(result)['own']['review']['trials'], 1)
        self.assertEqual(self.row(result, snapshot)['own']['review']['trials'], 1)

    def test_receipt_preserves_selected_route_and_unconfirmed_provider(self):
        self.roster['lanes'][0].update(lane_id='go-luna', quota_pool='opencode-go', harness='opencode')
        with mock.patch.object(run_identity, 'roster', return_value=self.roster):
            record = run_identity.begin('opencode', 'go-luna', 'opencode-go/luna-exact',
                                        'go-luna', 'review', 'high')
        self.assertEqual(record['selected_model'], 'go-luna')
        self.assertEqual(record['model_identity'], 'gpt-6-luna')
        self.assertIsNone(record['actual_model'])
        self.assertIn('underlying model unconfirmed', run_identity.receipt(dict(record, returncode=0)))

    def test_proven_alias_is_not_receipt_drift_but_another_release_is(self):
        record = {'selected_model': 'gpt-6-luna', 'actual_model': 'opencode-go/luna-exact'}
        self.assertFalse(run_identity.model_identity_drift(record, self.roster))
        record['actual_model'] = 'provider/different-release'
        self.assertTrue(run_identity.model_identity_drift(record, self.roster))

    def test_fit_benchmark_uses_the_same_identity_without_heldout_leakage(self):
        from bench import split, to_evidence
        fit = next('fit-%d' % i for i in range(100) if split.task_split('fit-%d' % i) == 'fit')
        held = next('held-%d' % i for i in range(100) if split.task_split('held-%d' % i) == 'heldout')
        rows = [{'task': fit, 'option': key, 'model': key, 'pool': pool, 'level': 'high',
                 'started_at': '2026-10-13T12:00:00Z',
                 'harness': 'codex' if pool == 'codex' else 'opencode',
                 'family': 'review', 'pass': passed, 'duration_s': 1}
                for key, pool, passed in [('gpt-6-luna', 'codex', True), ('opencode-go/luna-exact', 'opencode-go', False)]]
        rows.append({'task': held, 'model': {}, 'pass': 'poison'})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bench.jsonl'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            result = to_evidence.build_evidence([path], self.build([]), overlay=self.roster)
        self.assertEqual(self.row(result)['own']['review'], {'passes': 1, 'trials': 2})
        self.assertEqual(self.row(result)['source_telemetry']['opencode-go/opencode']['metric'], 'completion_time_s')
        self.assertFalse(any(r['model_key'] == 'go-luna' for r in result['rows']))

    def test_fit_benchmark_preserves_historical_or_unconfirmed_alias(self):
        from bench import split, to_evidence
        fit = next('fit-%d' % i for i in range(100) if split.task_split('fit-%d' % i) == 'fit')
        for fields in [{'started_at': '2026-09-01T00:00:00Z'},
                       {'started_at': '2026-10-13T00:00:00Z', 'provider_identity_unconfirmed': True}]:
            row = dict(task=fit, option='go', model='go-luna', pool='opencode-go',
                       level='high', family='review', **fields, **{'pass': True})
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'bench.jsonl'
                path.write_text(json.dumps(row) + '\n')
                result = to_evidence.build_evidence([path], self.build([]), overlay=self.roster)
            self.assertEqual(self.row(result)['own']['review']['trials'], 0)
            self.assertEqual(self.row(result, 'go-luna')['own']['review']['trials'], 1)


class SharedRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shared model tests ')
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.roster = json.loads((ROOT / 'tests/fixtures/access-overlay.test.json').read_text())
        self.roster['policy']['selector'] = {'allowed_pools_by_role': {'default': ['codex', 'opencode-go']}}
        canonical = 'gpt-6-luna'
        self.roster['model_cards']['go-luna'] = identity_card()
        self.roster['effort']['go-luna'] = copy.deepcopy(self.roster['effort'][canonical])
        self.roster['effort']['go-luna']['levels'] = {'opencode': ['high']}
        lane = copy.deepcopy(next(l for l in self.roster['lanes'] if l['quota_pool'] == 'opencode-go'))
        lane.update(lane_id='go-luna', model_key='go-luna', selector='opencode-go/luna-exact',
                    roles=['review'], allowed_modes=['read-only'], access_status='verified',
                    admission_status='active', price_1m={'in': 0, 'out': 0})
        self.roster['lanes'] = [lane]
        # Only these two routes may enter this experiment.
        self.allow = 'codex:gpt-6-luna:high,opencode-go:go-luna:high'
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, FLEET_QUOTA_POLICY='off', FLEET_IGNORE_QUOTA='1').start()
        self.write_rows([{'model_key': canonical, 'level': 'high',
                         'q': {'review': {'mean': .8, 'sd': .01, 'n_sources': 1}},
                         'tokens_per_task': {'in': 1000, 'out': 1000},
                         'price_1m': {'in': 2, 'out': 4}, 'latency_s': 1}])

    def write_rows(self, rows):
        folder = self.state / 'evidence'
        folder.mkdir(exist_ok=True)
        (folder / 'levels.json').write_text(json.dumps({'rows': rows}))

    def select(self, runtime=None, **kwargs):
        return selector.select_option(self.roster, runtime or {}, self.state, 'review',
                                      allow=self.allow, fleet=fleetctl, **kwargs)

    def test_selector_reads_one_quality_row_and_preserves_source_command(self):
        result = self.select()
        routes = {r['pool']: r for r in result['top3']}
        self.assertEqual(routes['opencode-go']['quality'], routes['codex']['quality'])
        self.assertFalse(routes['opencode-go']['q']['unknown'])
        self.assertEqual(routes['opencode-go']['model_identity'], 'gpt-6-luna')
        self.assertEqual(routes['opencode-go']['model_key'], 'go-luna')
        self.assertIn('go-luna', routes['opencode-go']['command_argv'])
        receipt = json.loads(Path(routes['opencode-go']['selection_file']).read_text())
        self.assertEqual(receipt['choice']['model_identity'], 'gpt-6-luna')

    def test_source_off_switch_and_capacity_choose_other_source(self):
        off = {'model_toggles': {'opencode-go': ['go-luna']}}
        self.assertEqual(self.select(off)['choice']['pool'], 'codex')
        runtime = {}
        fleetctl.set_pool_level(runtime, 'opencode-go', 'off')
        self.assertEqual(self.select(runtime)['choice']['pool'], 'codex')
        lane = self.roster['lanes'][0]
        runtime = {'leases': [{'lane_id': lane['lane_id'], 'expires_at': '2099-01-01T00:00:00Z'}
                              for _ in range(lane['max_parallel'])]}
        self.assertEqual(self.select(runtime)['choice']['pool'], 'codex')

    def test_canonical_global_off_reaches_alias_at_wrapper_admission(self):
        runtime = {'model_preferences': {'gpt-6-luna': 'off'}}
        self.assertFalse(fleetctl.pool_switches(self.roster, runtime, 'opencode-go')['go-luna'])
        self.assertEqual(fleetctl.model_blocked(self.roster, runtime, 'opencode-go', 'go-luna'), 'off')
        with self.assertRaises(fleetctl.FleetError):
            self.select(runtime)
        fleetctl.set_model_toggle(runtime, self.roster, 'codex', 'gpt-6-luna', False)
        self.assertFalse(fleetctl.pool_switches(self.roster, runtime, 'opencode-go')['go-luna'])

    def test_declared_marginal_price_breaks_equal_score_ties(self):
        self.roster['lanes'][0]['price_1m'] = {'in': 0, 'out': 0}
        self.assertEqual(self.select()['choice']['pool'], 'opencode-go')

    def test_conflicting_legacy_alias_caches_remain_unknown(self):
        self.roster['model_cards']['third-luna'] = identity_card()
        self.write_rows([{'model_key': alias, 'level': 'high',
                         'q': {'review': {'mean': mean, 'sd': .01, 'n_sources': 1}}}
                        for alias, mean in [('go-luna', .9), ('third-luna', .1)]])
        self.assertTrue(all(r['q']['unknown'] for r in self.select()['top3']))

    def test_conflicting_dated_alias_caches_choose_newest_without_summing(self):
        self.roster['model_cards']['third-luna'] = identity_card()
        rows = [{'model_key': alias, 'level': 'high', 'read_on': date,
                 'own': {'review': {'trials': trials, 'passes': 1}},
                 'q': {'review': {'mean': mean, 'sd': .01, 'n_sources': 1}}}
                for alias, date, trials, mean in [('go-luna', '2026-10-13', 2, .9),
                                                  ('third-luna', '2026-10-14', 5, .1)]]
        for candidates in [rows, list(reversed(rows))]:
            winner = run_identity.evidence_index(self.roster, candidates)['gpt-6-luna', 'high']
            self.assertEqual(winner['model_key'], 'third-luna')
            self.assertEqual(winner['own']['review']['trials'], 5)
            self.assertEqual(winner['q']['review']['mean'], .1)
            self.assertIn('alias_cache_conflict', winner['flags'])
        # A canonical cache always wins, even when older than the aliases.
        canonical = dict(rows[0], model_key='gpt-6-luna', read_on='2026-10-01')
        self.assertEqual(run_identity.evidence_index(self.roster, rows + [canonical])['gpt-6-luna', 'high'], canonical)

    def test_alias_cache_ties_use_stable_key_without_quality_cherry_picking(self):
        self.roster['model_cards']['third-luna'] = identity_card()
        for fields in [{'read_on': '2026-10-13'}, {'model_identity': 'gpt-6-luna'}]:
            rows = [dict(model_key=alias, level='high', q={'review': {'mean': mean}}, **fields)
                    for alias, mean in [('third-luna', .99), ('go-luna', .1)]]
            for candidates in [rows, list(reversed(rows))]:
                winner = run_identity.evidence_index(self.roster, candidates)['gpt-6-luna', 'high']
                self.assertEqual(winner['model_key'], 'go-luna')
                self.assertEqual(winner['q']['review']['mean'], .1)

    def test_source_prices_and_latency_are_used_for_their_source_only(self):
        result = self.select()
        routes = {r['pool']: r for r in result['top3']}
        self.assertEqual(routes['opencode-go']['cost']['estimated_usd'], 0)
        self.assertEqual(routes['codex']['cost']['estimated_usd'], .006)
        self.write_rows([{'model_key': 'gpt-6-luna', 'level': 'high',
                         'q': {'review': {'mean': .8, 'sd': .01, 'n_sources': 1}}, 'latency_s': 99,
                         'latency_by_stakes': {'high': 0.001},
                         'source_telemetry': {'opencode-go/opencode': {'latency_s': 1},
                                              'codex/codex': {'latency_s': 20}}}])
        self.assertEqual(self.select()['choice']['pool'], 'opencode-go')
        routes = {r['pool']: r for r in self.select(stakes='high')['top3']}
        self.assertEqual(routes['codex']['latency_s'], 20)

    def test_invalid_source_price_stays_unknown_without_using_shared_price(self):
        for price in ['invalid', {'in': -1, 'out': 0}, {'in': 0}]:
            with self.subTest(price=price):
                self.roster['lanes'][0]['price_1m'] = price
                routes = {r['pool']: r for r in self.select()['top3']}
                self.assertIsNone(routes['opencode-go']['cost']['estimated_usd'])
                self.assertEqual(routes['codex']['cost']['estimated_usd'], .006)

    def test_legacy_alias_cache_is_read_without_double_counting(self):
        self.write_rows([{'model_key': 'go-luna', 'level': 'high',
                         'read_on': '2026-10-13',
                         'q': {'review': {'mean': .9, 'sd': .01, 'n_sources': 1}}}])
        result = self.select()
        self.assertTrue(all(r['quality'] == .9 for r in result['top3']))

    def test_old_or_undated_legacy_cache_cannot_train_current_identity(self):
        for date in [None, '2026-09-01']:
            self.write_rows([{'model_key': 'go-luna', 'level': 'high', 'read_on': date,
                             'q': {'review': {'mean': .98, 'sd': .01, 'n_sources': 1}}}])
            self.assertTrue(all(r['q']['unknown'] for r in self.select()['top3']))

    def test_alias_does_not_bypass_paid_explicit_allow_or_cap(self):
        # BR-MIMO-001 / BR-MIMO-002: inspect the real selector admission result.
        paid = copy.deepcopy(json.loads((ROOT / 'tests/fixtures/access-overlay.test.json').read_text()))
        paid['model_cards']['go-flash'] = identity_card('mimo-v2.6-flash')
        paid['effort']['go-flash'] = {'levels': {'opencode': []}, 'default': 'service-chosen'}
        go = copy.deepcopy(self.roster['lanes'][0])
        go.update(lane_id='go-flash', model_key='go-flash', roles=['hard-reasoning'])
        paid['lanes'].append(go)
        options, rejected = selector.enumerate_options(paid, {}, 'hard-reasoning', fleet=fleetctl)
        self.assertTrue(any(r['lane_id'] == 'go-flash' for r in options))
        self.assertFalse(any(r['pool'] == 'openrouter-mimo' for r in options))
        options, _ = selector.enumerate_options(paid, {}, 'hard-reasoning', fleet=fleetctl,
                                                explicit_pools={'openrouter-mimo'})
        self.assertTrue(any(r['pool'] == 'openrouter-mimo' for r in options))
        with mock.patch.object(fleetctl, 'pool_spent_since', return_value=5):
            with self.assertRaisesRegex(fleetctl.FleetError, 'cap'):
                selector.select_option(paid, {}, self.state, 'hard-reasoning', fleet=fleetctl,
                                       allow='openrouter-mimo:mimo-v2.6-flash:*')


if __name__ == '__main__':
    unittest.main()
