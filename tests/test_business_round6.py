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

    def runtime(self, level, *, used=91, hours=96, eta=None):
        def window(used, forecast):
            return {'observed_at': fleetctl.iso(NOW), 'windows': {'weekly': {
                'reset_at': fleetctl.iso(NOW + dt.timedelta(hours=hours)), 'used_percent': used,
                'window_minutes': 10080, 'projected_used_percent_at_reset': forecast,
                **({'eta_seconds': eta} if eta is not None else {}),
            }}}
        runtime = {'quota_snapshots': {'codex': window(used, 1000), 'claude': window(1, 2)}}
        fleetctl.set_pool_level(runtime, 'codex', level)
        return runtime

    def select(self, runtime, allow=None, persist=False):
        return selector.select_option(self.roster, runtime, self.state, 'review', fleet=fleetctl,
                                      no_explore=True, persist_receipts=persist, allow=allow)

    def fleet(self, *args):
        return subprocess.run(['python3', str(ROOT / 'scripts/fleetctl.py'), '--overlay', str(self.overlay),
                               '--state-dir', str(self.state), *args], capture_output=True, text=True)

    # BR-SELECT-008
    def test_max_dial_order_and_real_limits(self):
        self.assertEqual(fleetctl.LEVELS, ('off', 'low', 'normal', 'high', 'max', 'forced'))
        lane = {'max_parallel': 4, 'quality_tier': 'frontier'}
        observed = []
        for level in fleetctl.LEVELS:
            runtime = self.runtime(level, used=91)
            state, evidence = fleetctl.current_pool_state(runtime, 'codex', roster=self.roster)
            band = fleetctl.level_band(level, fleetctl.task_band(state, evidence))
            observed.append((state != 'EXHAUSTED', fleetctl.effective_cap(lane, state, evidence, level),
                             band == 'quality_first'))
        self.assertEqual(observed, sorted(observed))
        self.assertGreater(observed[4], observed[3])
        for used in (100,):
            with self.assertRaisesRegex(fleetctl.FleetError, 'pool exhausted'):
                self.select(self.runtime('max', used=used), f'codex:{MODEL}:high')
        rounded = self.runtime('max', used=100)
        rounded['quota_snapshots']['codex']['provider_used_percent'] = {'weekly': 99.9}
        self.assertEqual(self.select(rounded, f'codex:{MODEL}:high')['choice']['model_key'], MODEL)

    # BR-SELECT-008
    def test_max_yields_to_equal_quality_cheaper_quota(self):
        allow = f'codex:{MODEL}:high,claude:{OTHER}:high'
        def choice(level):
            return self.select(self.runtime(level), allow)['choice']
        self.assertEqual(choice('max')['model_key'], OTHER)
        self.assertEqual(choice('forced')['model_key'], MODEL)
        max_cost = self.select(self.runtime('max'), f'codex:{MODEL}:high')['choice']['quota_cost_usd']
        normal_cost = self.select(self.runtime('normal'), f'codex:{MODEL}:high')['choice']['quota_cost_usd']
        neutral = choice('forced')['quota_cost_usd']
        self.assertGreater(max_cost, 3 * choice('max')['quota_cost_usd'])
        self.assertAlmostEqual(max_cost, neutral + .1 * (normal_cost - neutral))
        self.assertEqual(choice('max')['quality'], choice('forced')['quality'])

    # BR-SELECT-008
    def test_max_reserves_only_in_last_stretch(self):
        self.assertIsNone(fleetctl.lead_pressure(self.runtime('max', hours=3, eta=120), 'codex', self.roster))
        self.assertIsNotNone(fleetctl.lead_pressure(self.runtime('max', hours=.5, eta=120), 'codex', self.roster))
        self.assertIsNone(fleetctl.lead_pressure(self.runtime('max', hours=.5, eta=3600), 'codex', self.roster))
        self.assertIsNone(fleetctl.lead_pressure(self.runtime('forced', hours=.5, eta=120), 'codex', self.roster))

    # BR-SWITCH-002
    def test_subscription_dial_is_atomic_audited_and_independent(self):
        pools = ('antigravity-gemini', 'antigravity-3p', 'antigravity-argon')
        self.roster['quota_pools']['antigravity-argon'] = {'plan': {'billing': 'subscription', 'name': 'Google AI Pro'}}
        self.overlay.write_text(json.dumps(self.roster))
        runtime = {'model_toggles': {'antigravity-gemini': ['gemini-fixture']}}
        fleetctl.atomic_json(self.state / 'runtime.json', runtime)
        rejected = self.fleet('level', 'antigravity', 'max')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(fleetctl.load_json(self.state / 'runtime.json'), runtime)
        changed = self.fleet('level', 'antigravity', 'max', '--owner', '--reason', 'test instruction')
        self.assertEqual((changed.returncode, changed.stdout.strip()), (0, 'max'), changed.stderr)
        actual = fleetctl.load_json(self.state / 'runtime.json')
        self.assertEqual(actual['model_toggles'], runtime['model_toggles'])
        self.assertEqual({fleetctl.pool_level(actual, pool) for pool in pools}, {'max'})
        record = fleetctl.control_record(actual, 'subscription:antigravity')
        self.assertEqual((record['actor'], record['reason'], record['value']), ('owner', 'test instruction', 'max'))
        self.assertEqual(record['before'], dict.fromkeys(pools, 'normal'))
        self.assertEqual(self.fleet('level', pools[0], 'low', '--owner').returncode, 0)
        self.assertEqual(self.fleet('level', 'antigravity').stdout.strip(), 'mixed')

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
