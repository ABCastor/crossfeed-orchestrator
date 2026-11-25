"""The routing dial, subscription controls and archives at the dispatch boundary."""
import argparse
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.test_selector import ROOT, fleetctl, quality_row, selector
import chatgpt_catalog

MODEL = "gpt-6.1-sol"
OTHER = "claude-opus-5-5"
NOW = dt.datetime(2026, 11, 25, tzinfo=dt.timezone.utc)


class Round6PolicyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.overlay = self.state / 'overlay.json'
        self.roster = json.loads((ROOT / 'tests/fixtures/access-overlay.test.json').read_text())
        self.roster['policy']['selector'] = {'allowed_pools_by_role': {'default': ['codex', 'claude']}}
        for pool in ('codex', 'claude'):
            self.roster['quota_pools'][pool]['value_capacity'] = {'weekly_usd_equivalent': 50, 'source': 'test plan'}
        self.overlay.write_text(json.dumps(self.roster))
        (self.state / 'evidence').mkdir()
        rows = [dict(quality_row(key, pass_rate=.8), own_cost={pool: {'percent_per_task': share}})
                for pool, key, share in (('codex', MODEL, 1), ('claude', OTHER, 2))]
        (self.state / 'evidence/levels.json').write_text(json.dumps({'rows': rows}))
        for patch in (mock.patch.object(fleetctl, 'utc_now', return_value=NOW),
                      mock.patch.dict(os.environ, {'FLEET_QUOTA_POLICY': 'clock_aware', 'FLEET_IGNORE_QUOTA': '0'})):
            patch.start()
            self.addCleanup(patch.stop)

    def select(self, runtime, allow=None, persist=False):
        return selector.select_option(self.roster, runtime, self.state, 'review', fleet=fleetctl,
                                      no_explore=True, persist_receipts=persist, allow=allow)

    def fleet(self, *args):
        return subprocess.run(['python3', str(ROOT / 'scripts/fleetctl.py'), '--overlay', str(self.overlay),
                               '--state-dir', str(self.state), *args], capture_output=True, text=True)

    # BR-SELECT-007
    def test_max_is_the_label_for_forced_not_a_level(self):
        self.assertEqual(fleetctl.LEVELS, ('off', 'low', 'normal', 'high', 'forced'))
        self.assertEqual(fleetctl.LEVEL_LABELS['forced'], 'Max')
        self.assertIn('ignores quota estimates', fleetctl.LEVEL_MEANING['forced'])
        self.assertNotEqual(self.fleet('level', 'codex', 'max').returncode, 0)
        self.assertEqual(self.fleet('level', 'codex', 'forced').stdout.strip(), 'forced')

    # BR-SWITCH-002
    def test_subscription_dial_is_atomic_audited_and_independent(self):
        pools = ('antigravity-gemini', 'antigravity-3p', 'antigravity-argon')
        self.roster['quota_pools']['antigravity-argon'] = {'plan': {'billing': 'subscription', 'name': 'Google AI Pro'}}
        self.overlay.write_text(json.dumps(self.roster))
        runtime = {'model_toggles': {'antigravity-gemini': ['gemini-fixture']}}
        fleetctl.atomic_json(self.state / 'runtime.json', runtime)
        rejected = self.fleet('level', 'antigravity', 'forced')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(fleetctl.load_json(self.state / 'runtime.json'), runtime)
        changed = self.fleet('level', 'antigravity', 'forced', '--owner', '--reason', 'test instruction')
        self.assertEqual((changed.returncode, changed.stdout.strip()), (0, 'forced'), changed.stderr)
        actual = fleetctl.load_json(self.state / 'runtime.json')
        self.assertEqual(actual['model_toggles'], runtime['model_toggles'])
        self.assertEqual({fleetctl.pool_level(actual, pool) for pool in pools}, {'forced'})
        record = fleetctl.control_record(actual, 'subscription:antigravity')
        self.assertEqual((record['actor'], record['reason'], record['value']), ('owner', 'test instruction', 'forced'))
        self.assertEqual(record['before'], dict.fromkeys(pools, 'normal'))
        self.assertEqual(self.fleet('level', pools[0], 'low', '--owner').returncode, 0)
        self.assertEqual(self.fleet('level', 'antigravity').stdout.strip(), 'mixed')

    # BR-SELECT-009
    def test_efficient_effort_uses_success_per_expected_dollar(self):
        rows = [dict(quality_row(MODEL, level, pass_rate=accuracy),
                     own_cost={"codex": {"percent_per_task": percent}})
                for level, accuracy, percent in (("high", .15, .01), ("xhigh", .85, .02))]
        (self.state / 'evidence/levels.json').write_text(json.dumps({'rows': rows}))
        result = self.select({}, f'codex:{MODEL}:high,codex:{MODEL}:xhigh')
        low, high = sorted(result['top3'], key=lambda option: option['quota_cost_usd'])
        self.assertEqual(low['level'], 'high')
        self.assertGreater(high['p_success'] / high['expected_cost'], low['p_success'] / low['expected_cost'])
        self.assertEqual(result['choice']['efficient_effort']['effort'], 'xhigh')
        menu = selector.compare_options(self.roster, {}, self.state, 'review', fleet=fleetctl)
        self.assertEqual(next(row for row in menu if row['model_key'] == MODEL)['efficient_effort']['effort'], 'xhigh')

    # BR-SELECT-010
    def test_chat_uses_exact_effort_benchmark_and_keeps_route_identity(self):
        chat = 'chatgpt:latest-high'
        pool = 'chatgpt-work'
        self.roster['policy']['selector']['allowed_pools_by_role']['default'] = [pool]
        self.roster['quota_pools'][pool] = {'plan': {'name': 'Included Chat', 'limit': 'none-known'}}
        self.roster['lanes'].append({'lane_id': chat, 'model_key': chat, 'selector': chat,
                                    'harness': 'chatgpt-chat', 'provider': 'chatgpt', 'quota_pool': pool,
                                    'admission_status': 'active', 'access_status': 'verified', 'roles': ['review'],
                                    'allowed_modes': ['read-only'], 'max_parallel': 2, 'worker_level': 'high',
                                    'worker_label': 'fixture', 'benchmark_identity': 'gpt-6-sol',
                                    'benchmark_effort': 'high'})
        self.roster['effort'][chat] = {'levels': {'chatgpt-chat': ['high']}, 'default': 'high'}
        self.roster['benchmark_priors'] = {'gpt-6-sol': {'high': {
            'slug': 'gpt-6-sol-high', 'release_date': '2026-09-22', 'read_on': '2026-11-25',
            'evaluations': {'terminal_bench_4': .70}, 'pricing': {'in': 2, 'out': 10}}}}
        self.assertEqual(chatgpt_catalog.benchmark_cell('GPT-6', 'high'), ('gpt-6-sol', 'high'))
        self.assertEqual(chatgpt_catalog.benchmark_cell('GPT-6', 'pro'), ('gpt-6-astra', 'pro'))
        self.assertIsNone(chatgpt_catalog.benchmark_cell('Latest', 'high'))
        self.assertIsNone(chatgpt_catalog.benchmark_cell('GPT-6.2', 'pro'))
        item = self.select({}, f'{pool}:{chat}:high')['choice']
        self.assertEqual((item['model_key'], item['model_identity']), (chat, chat))
        self.assertEqual(item['prior_source']['benchmark_effort'], 'high')
        self.assertFalse(item['q']['unknown'])
        self.assertEqual(item['quota_cost_usd'], 0)
        self.assertGreater(item['api_cost_usd'], 0)
        del self.roster['benchmark_priors']
        self.assertTrue(self.select({}, f'{pool}:{chat}:high')['choice']['q']['unknown'])

    def dry_dispatch(self, selection):
        args = argparse.Namespace(dir=self.state, last=None, dry_run=True, overlay=self.overlay)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            rc = fleetctl.dispatch_selection(selection, args, self.state, 'fixture', roster=self.roster)
        return rc, output.getvalue()

    # BR-MODEL-001
    def test_archived_model_is_never_selected_or_dispatched(self):
        spec = f'codex:{MODEL}:high'
        old = self.select({}, spec, persist=True)
        self.assertEqual(self.fleet('model-archive', 'codex', MODEL, '--owner', '--reason', 'fixture').returncode, 0)
        runtime = fleetctl.load_json(self.state / 'runtime.json')
        with self.assertRaisesRegex(fleetctl.FleetError, 'model archived'):
            self.select(runtime, spec)
        self.assertEqual(self.dry_dispatch(old), (5, ''))
        self.assertEqual(fleetctl.start_verdict(self.roster, runtime, MODEL, pool='codex')['why'], 'archived')
        self.assertNotIn(MODEL, {item['model_key'] for item in selector.compare_options(
            self.roster, runtime, self.state, 'review', fleet=fleetctl) if item['pool'] == 'codex'})
        overview = fleetctl.fleet_overview(self.roster, runtime, self.state)
        pool = next(row for row in overview['pools'] if row['pool'] == 'codex')
        self.assertNotIn(MODEL, [row['model'] for row in pool['options']])
        row = next(row for row in pool['archived'] if row['model'] == MODEL)
        self.assertEqual((row['archived'], row['set_by'], row['archive_reason']), (True, 'operator', 'fixture'))
        self.assertTrue(row['set_at'])
        self.assertNotEqual(self.fleet('model-archive', 'codex', MODEL, '--restore').returncode, 0)
        self.assertEqual(self.fleet('model-archive', 'codex', MODEL, '--restore', '--owner').returncode, 0)
        self.assertEqual(self.select(fleetctl.load_json(self.state / 'runtime.json'), spec)['choice']['model_key'], MODEL)

    # BR-MODEL-001: control confirms this exact route is observable at the dispatch boundary.
    def test_non_archived_model_is_selected_and_dispatches(self):
        selection = self.select({}, f'codex:{MODEL}:high', persist=True)
        rc, out = self.dry_dispatch(selection)
        self.assertEqual(rc, 0)
        self.assertIn(MODEL, json.loads(out)['command_argv'])


if __name__ == '__main__':
    unittest.main()
