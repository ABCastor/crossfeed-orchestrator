"""Catalog fixtures exercise the actual overlay writer and routing boundaries."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import daily_model_refresh as daily, fleetctl, selector
from scripts import patch_opencode_go_roles as patcher
from tests.test_opencode_value import value_fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/daily-model-catalogs.json').read_text())
SPEC = importlib.util.spec_from_file_location('daily_market_test', ROOT / 'scripts/market-refresh.py')
market_refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(market_refresh)


def catalog(rows, **kwargs):
    return {'harness': 'opencode', 'provider': 'opencode-go', 'pool': 'opencode-go',
            'models': copy.deepcopy(rows), 'source': 'fixture', **kwargs}


def market():
    return {'artificial_analysis': copy.deepcopy(FIXTURE['scores']), 'fetched_at': '2026-10-20T00:00:00Z'}


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.roster = value_fixture()
        self.overlay = self.tmp / 'overlay.json'
        self.overlay.write_text(patcher.dump(self.roster))

    def apply(self, rows, **kwargs):
        return daily.apply_refresh(self.overlay, [catalog(rows)], market(), today='2026-10-20', **kwargs)

    def test_add_retire_price_and_backup_through_real_writer(self):
        # BR-REFRESH-001
        rows = FIXTURE['after']
        before = self.overlay.read_bytes()
        changes = self.apply(rows)
        result = json.loads(self.overlay.read_text())
        self.assertEqual(len(changes['added']), 2)
        new = next(l for l in result['lanes'] if l['model_key'] == 'new-helper')
        self.assertEqual((new['admission_status'], new['access_status'], new['roles']), ('candidate', 'unverified', []))
        self.assertEqual(new['price_1m'], {'input': .2, 'output': .8})
        self.assertEqual(new['market_scores']['aa_coding'], 55)
        paid = next(l for l in result['lanes'] if l['model_key'] == 'mimo-v2.6-flash' and l['provider'] == 'opencode-go')
        self.assertEqual(paid['price_1m']['input'], .1)
        self.assertEqual(paid['market_scores']['vals'], 60)
        absent = next(l for l in result['lanes'] if l['model_key'] == 'qwen3.8-flash')
        self.assertEqual(absent['admission_status'], 'retired')
        backups = list(self.tmp.glob('overlay.json.bak.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        patcher.validate_overlay(self.overlay.read_text(), label='test readback')

    def test_failed_or_malformed_catalog_cannot_retire_or_change_roles(self):
        # BR-REFRESH-001, prohibition + successful retirement control above
        for rows in (None, [{'selector': 'truncated'}], [{'selector': 'x', 'model_key': 'x'}] * 2):
            with self.subTest(rows=rows):
                result, changes = daily.reconcile(self.roster, [catalog(rows)], market())
                self.assertEqual(result, self.roster)
                self.assertEqual(changes['retired'], [])
                self.assertTrue(changes['unknown'])

    def test_successful_empty_catalog_retires_all_scoped_routes(self):
        result, changes = daily.reconcile(self.roster, [catalog([])], market())
        self.assertTrue(changes['retired'])
        self.assertTrue(all(l['admission_status'] == 'retired' for l in result['lanes'] if l['provider'] == 'opencode-go'))

    def test_free_promo_candidate_does_not_borrow_operational_admission(self):
        # BR-BENCH-001, BR-DATA-001
        self.apply(FIXTURE['after'])
        result = json.loads(self.overlay.read_text())
        free = next(l for l in result['lanes'] if l['model_key'] == 'beam-free')
        self.assertEqual((free['price_1m'], free['data_policy'], free['roles']), ({'input': 0, 'output': 0}, 'public-only', []))
        self.assertNotIn('beam-free', result.get('effort', {}))
        self.assertFalse(free['lane_id'] in result['routing']['roles']['default']['quality_first'])

    def _active_free(self):
        r, _ = daily.reconcile(self.roster, [catalog(FIXTURE['after'])], market(), today='2026-10-19')
        free = next(l for l in r['lanes'] if l['model_key'] == 'beam-free')
        free.update(access_status='verified', admission_status='active')
        r['effort']['beam-free'] = copy.deepcopy(r['effort']['mimo-v2.6-flash'])
        self.overlay.write_text(patcher.dump(r))
        return r, free

    def test_free_first_daily_cap_survives_repeated_runs_and_day_rollover(self):
        # BR-REFRESH-001
        r, free = self._active_free()
        first = self.apply(FIXTURE['after'])
        result = json.loads(self.overlay.read_text())
        self.assertEqual(len(first['roles']), 2)
        for role in first['roles']:
            ranked_go = [x for x in result['routing']['roles'][role]['quality_first']
                         if any(l['lane_id'] == x and l.get('harness') == 'opencode' for l in result['lanes'])]
            self.assertEqual(ranked_go[0], free['lane_id'])
        second = self.apply(FIXTURE['after'])
        self.assertEqual(second['roles'], [])
        self.assertEqual(json.loads(self.overlay.read_text())['model_refresh_day']['roles_changed'], 2)
        _, changes = daily.reconcile(result, [catalog(FIXTURE['after'])], market(), today='2026-10-21')
        self.assertEqual(changes['roles'], ['research-scout'])
        # Other providers, switches, frontier roles, controls remain untouched.
        for role in ('review', 'implementation', 'hard-reasoning'):
            self.assertEqual(result['routing']['roles'][role], r['routing']['roles'][role])
        self.assertEqual(result['effort'], r['effort'])

    def test_quality_floor_and_unknown_price_do_not_win(self):
        # BR-REFRESH-001
        r, free = self._active_free()
        m = market()
        m['artificial_analysis']['beam']['intelligence_index'] = 39
        result, changes = daily.reconcile(r, [catalog(FIXTURE['after'])], m, today='2026-10-21')
        for role in changes['roles']:
            self.assertNotIn(free['lane_id'], result['routing']['roles'][role]['quality_first'])
        self.assertIsNone(patcher.helper_call_cost({'input': None, 'output': 0}))
        self.assertAlmostEqual(patcher.helper_call_cost({'input': 1, 'output': 1}), .01)
        rows = [{k: v for k, v in row.items() if k != 'price_1m'} for row in FIXTURE['after']]
        result, changes = daily.reconcile(r, [catalog(rows)], market(), today='2026-10-21')
        self.assertEqual(changes['roles'], [])

    def test_disqualified_price_removes_old_roles_even_without_a_winner(self):
        r, free = self._active_free()
        r, _ = daily.reconcile(r, [catalog(FIXTURE['after'])], market(), today='2026-10-20')
        rows = copy.deepcopy(FIXTURE['after'])
        for row in rows:
            if 'price_1m' in row:
                row['price_1m'] = {'input': 100, 'output': 100}
        result, changes = daily.reconcile(r, [catalog(rows)], market(), today='2026-10-21')
        self.assertEqual(len(changes['roles']), 2)
        for role in changes['roles']:
            self.assertNotIn(free['lane_id'], result['routing']['roles'][role]['quality_first'])
        # Unknown scores are a separate state and cannot authorize removal.
        unknown, changes = daily.reconcile(r, [catalog(rows)], {}, today='2026-10-21')
        self.assertEqual(changes['roles'], [])
        self.assertEqual(unknown['routing'], r['routing'])

    def test_bounds_cannot_expand_or_reset_same_day(self):
        # BR-REFRESH-001
        for settings in ({'max_roles_per_day': 3}, {'aa_floor': 0}, {'helper_usd_cap': 1}):
            r = copy.deepcopy(self.roster)
            r.setdefault('policy', {})['model_refresh'] = settings
            with self.assertRaises(patcher.PatchError):
                daily.reconcile(r, [catalog(FIXTURE['after'])], market())

    def test_invalid_candidate_never_writes_and_post_write_failure_rolls_back(self):
        # BR-REFRESH-001
        original = self.overlay.read_bytes()
        with patch.object(patcher, 'validate_overlay', side_effect=patcher.PatchError('bad candidate')):
            with self.assertRaises(patcher.PatchError):
                self.apply(FIXTURE['after'])
        self.assertEqual(self.overlay.read_bytes(), original)
        with patch.object(patcher, 'validate_overlay', side_effect=[None, patcher.PatchError('bad applied')]):
            with self.assertRaises(patcher.PatchError):
                self.apply(FIXTURE['after'])
        self.assertEqual(self.overlay.read_bytes(), original)

    def test_policy_refuses_private_routing_selector_and_explicit_acquisition(self):
        # BR-DATA-001 prohibition. All three boundaries observe an actual refusal.
        r, free = self._active_free()
        r, _ = daily.reconcile(r, [catalog(FIXTURE['after'])], market(), today='2026-10-20')
        options, rejected = selector.enumerate_options(r, {}, 'default', fleet=fleetctl)
        self.assertNotIn(free['lane_id'], [o.get('lane_id') for o in options])
        self.assertTrue(any('public-only' in why for why in rejected))
        chosen = fleetctl.choose_lane(r, {}, 'default', 'read-only', 'text', harness='opencode')
        self.assertNotEqual(chosen['lane_id'], free['lane_id'])
        with patch.dict(os.environ, {'FLEET_DATA_CLASS': 'private'}):
            with self.assertRaisesRegex(fleetctl.FleetError, 'public-only'):
                fleetctl.acquire_lease(self.tmp / 'state', r, free['lane_id'], 10)
        self.assertFalse((self.tmp / 'state/runtime.json').exists())

    def test_public_control_and_per_model_owner_override(self):
        # BR-DATA-001 observable positive controls for both permitted paths.
        r, free = self._active_free()
        r, _ = daily.reconcile(r, [catalog(FIXTURE['after'])], market(), today='2026-10-20')
        chosen = fleetctl.choose_lane(r, {}, 'default', 'read-only', 'text', harness='opencode', data_class='public')
        self.assertEqual(chosen['lane_id'], free['lane_id'])
        options, _ = selector.enumerate_options(r, {}, 'default', fleet=fleetctl, data_class='public')
        option = next(o for o in options if o.get('lane_id') == free['lane_id'])
        argv = selector._command(option, 'default', self.tmp / 'receipt', fleet=fleetctl)
        self.assertIn('--direct', argv)
        self.assertIn('FLEET_DATA_CLASS=public', argv)
        with patch.dict(os.environ, {'FLEET_DATA_CLASS': 'public'}):
            token = fleetctl.acquire_lease(self.tmp / 'state', r, free['lane_id'], 10)
            self.assertTrue(token)
        lane = next(l for l in r['lanes'] if l['lane_id'] == free['lane_id'])
        lane['data_policy'] = 'private-ok'
        r, _ = daily.reconcile(r, [catalog(FIXTURE['after'])], market(), today='2026-10-21')
        lane = next(l for l in r['lanes'] if l['lane_id'] == free['lane_id'])
        self.assertEqual(lane['data_policy'], 'private-ok')
        self.assertIsNone(fleetctl.data_policy_refusal(lane, 'private'))

    def test_promotional_prices_join_exact_provider_and_scores_match_base(self):
        cats = [catalog([{'selector': 'opencode-go/beam-free', 'model_key': 'beam-free'}])]
        daily.enrich(cats, {'opencode': {'models': {'beam-free': {'cost': {'input': 8, 'output': 9}}}}})
        row = cats[0]['models'][0]
        self.assertEqual(row['price_1m'], {'input': 0, 'output': 0})
        self.assertTrue(row['free_promo'])
        self.assertEqual(daily.outside_scores('beam-free', market())['aa_intelligence'], 44)

    def test_cli_failed_or_diagnostic_output_is_unknown(self):
        for stdout, returncode in [('opencode-go/x\nWARNING truncated\n', 0), ('', 1), ('', 0)]:
            with patch.object(daily.subprocess, 'run', return_value=subprocess.CompletedProcess([], returncode, stdout, '')):
                self.assertIsNone(daily.cli_catalog(['fixture'], prefix='opencode-go'))
        with patch.object(daily.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'opencode-go/beam-free\n', '')):
            self.assertEqual(daily.cli_catalog(['fixture'], prefix='opencode-go')[0]['model_key'], 'beam-free')

    def test_discovery_and_market_listing_use_v2_standalone(self):
        # BR-REFRESH-001: provider listing is injectable through PATH, with no server.
        bin_dir = self.tmp / 'catalog-bin'
        bin_dir.mkdir()
        executable = bin_dir / 'opencode'
        executable.write_text('#!' + sys.executable + '\n' +
            "import sys\n" +
            "assert sys.argv[1:3] == ['models', '--standalone'], sys.argv\n" +
            "assert len(sys.argv) == 4, sys.argv\n" +
            "assert sys.argv[3] in ('opencode-go', 'opencode', 'github-copilot'), sys.argv\n" +
            "print(sys.argv[3] + '/fixture-model')\n")
        executable.chmod(0o755)
        roster = {'lanes': [
            {'harness': 'opencode', 'provider': 'opencode-go', 'quota_pool': 'opencode-go'},
            {'harness': 'copilot', 'provider': 'github-copilot'}]}
        with patch.dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH']):
            catalogs = daily.discover(roster)
            entitled = market_refresh.entitled_models()
        self.assertEqual({c['provider']: c['models'] for c in catalogs}, {
            provider: [{'selector': provider + '/fixture-model', 'model_key': 'fixture-model'}]
            for provider in ('opencode-go', 'opencode', 'github-copilot')})
        self.assertEqual(entitled, {'opencode-go/fixture-model'})

    def test_failed_go_catalog_preserves_go_order_when_promos_succeed(self):
        r, free = self._active_free()
        lane = next(l for l in r['lanes'] if l['lane_id'] == free['lane_id'])
        lane.update(provider='opencode', quota_pool='opencode-free', selector='opencode/beam-free')
        promo = catalog([{'selector': 'opencode/beam-free', 'model_key': 'beam-free',
                          'free_promo': True, 'price_1m': {'input': 0, 'output': 0}}],
                        provider='opencode', pool='opencode-free')
        result, _ = daily.reconcile(r, [catalog(None), promo], market(), today='2026-10-20')
        go_ids = {l['lane_id'] for l in r['lanes'] if l.get('quota_pool') == 'opencode-go'}
        for role, bands in result['routing']['roles'].items():
            for band, ranking in bands.items():
                self.assertEqual([x for x in ranking if x in go_ids],
                                 [x for x in r['routing']['roles'][role][band] if x in go_ids])

    def test_promo_name_and_price_are_read_after_provider_discovery(self):
        cats = [catalog([{'selector': 'opencode/promo', 'model_key': 'promo'},
                         {'selector': 'opencode/paid', 'model_key': 'paid'}],
                        provider='opencode', pool='opencode-free', free_only=True)]
        daily.enrich(cats, {'opencode': {'models': {'promo': {'name': 'Promo (Free)'},
                        'paid': {'name': 'Paid', 'cost': {'input': 1, 'output': 2}}}}})
        self.assertEqual([r['model_key'] for r in cats[0]['models']], ['promo'])
        self.assertEqual(cats[0]['models'][0]['price_1m'], {'input': 0, 'output': 0})

    def test_reappearing_route_requires_admission_again(self):
        r, _ = daily.reconcile(self.roster, [catalog([])], market())
        r, _ = daily.reconcile(r, [catalog(FIXTURE['after'])], market())
        lane = next(l for l in r['lanes'] if l.get('provider') == 'opencode-go' and l['model_key'] == 'mimo-v2.6-flash')
        self.assertEqual((lane['access_status'], lane['admission_status'], lane['roles']), ('unverified', 'candidate', []))

    def test_public_wrapper_uses_empty_working_root_and_private_is_refused(self):
        # BR-DATA-001: real wrapper, roster and lease paths; only provider is fake.
        r, free = self._active_free()
        r.pop('chatgpt_gateway', None)
        r.pop('provider_sources', None)
        self.overlay.write_text(patcher.dump(r))
        home = self.tmp / 'home'
        auth = home / '.local/share/opencode/auth.json'
        auth.parent.mkdir(parents=True)
        auth.write_text('{}')
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        executable = bin_dir / 'opencode'
        observed = self.tmp / 'observed.json'
        executable.write_text('#!' + sys.executable + '\n' +
            "import json,os,sys\n" +
            "from pathlib import Path\n" +
            "Path(os.environ['R19_OBSERVED']).write_text(json.dumps({'args':sys.argv[1:],'cwd':os.getcwd()}))\n" +
            "print(json.dumps({'type':'text','part':{'text':'PUBLIC_OK'}}))\n" +
            "print(json.dumps({'type':'step_finish','part':{'reason':'stop'}}))\n")
        executable.chmod(0o755)
        env = dict(os.environ, HOME=str(home), ACCESS_OVERLAY=str(self.overlay),
                   FLEET_STATE_DIR=str(self.tmp / 'state'), FLEET_NO_AUTO_REFRESH='1',
                   PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], R19_OBSERVED=str(observed))
        argv = ['bash', str(ROOT / 'scripts/opencode-agent.sh'), '--lane', free['lane_id'],
                '--direct', '--prompt', 'public fixture text', '--dir', str(self.tmp), '--timeout', '10']
        run = subprocess.run(argv, env=dict(env, FLEET_DATA_CLASS='private'), text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('public-only', run.stderr)
        self.assertFalse(observed.exists())
        run = subprocess.run(argv, env=dict(env, FLEET_DATA_CLASS='public'), text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), 'PUBLIC_OK')
        observation = json.loads(observed.read_text())
        provider_args = observation['args']
        root = observation['cwd']
        self.assertIn('--standalone', provider_args)
        self.assertNotIn('--dir', provider_args)
        self.assertNotIn('--variant', provider_args)
        self.assertNotEqual(root, str(self.tmp))
        self.assertEqual(provider_args[provider_args.index('--agent') + 1], 'direct')

    def test_code_class_ordering_across_lane_policies(self):
        # BR-DATA-001: public < code < private; each policy accepts up to its ceiling.
        from scripts import data_policy
        policies = {'public-only': {'public'}, 'code-ok': {'public', 'code'}, 'private-ok': {'public', 'code', 'private'}}
        for policy, accepted in policies.items():
            lane = {'lane_id': 'x', 'data_policy': policy}
            for material in ('public', 'code', 'private'):
                reason = data_policy.refusal(lane, material)
                if material in accepted:
                    self.assertIsNone(reason, (policy, material))
                else:
                    self.assertIn(policy, reason)
                    self.assertIn(material, reason)
        self.assertIsNotNone(data_policy.refusal({'data_policy': 'code-ok'}, 'private'))
        with self.assertRaises(ValueError):
            data_policy.classification('notes')
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('FLEET_DATA_CLASS', None)
            self.assertEqual(data_policy.classification(), 'private')
        # Existing defaults unchanged: promo lanes public-only, ordinary lanes private-ok.
        self.assertIn('public-only', data_policy.refusal({'free_promo': True}, 'private'))
        self.assertIn('public-only', data_policy.refusal({'selector': 'x-free'}, 'code'))
        self.assertIsNone(data_policy.refusal({}, 'private'))
        self.assertEqual(data_policy.refusal({'data_policy': 'bogus'}, 'public'), 'invalid lane data_policy')

    def test_code_ok_lane_routes_code_but_not_private_and_public_only_refuses_code(self):
        # BR-DATA-001: selector, choose_lane and explicit acquisition observe the new class.
        r, free = self._active_free()
        r, _ = daily.reconcile(r, [catalog(FIXTURE['after'])], market(), today='2026-10-20')
        lane = next(l for l in r['lanes'] if l['lane_id'] == free['lane_id'])
        lid = free['lane_id']
        # public-only (the promo default) refuses code material.
        options, rejected = selector.enumerate_options(r, {}, 'default', fleet=fleetctl, data_class='code')
        self.assertNotIn(lid, [o.get('lane_id') for o in options])
        self.assertTrue(any('public-only' in why and 'code' in why for why in rejected))
        with patch.dict(os.environ, {'FLEET_DATA_CLASS': 'code'}):
            with self.assertRaisesRegex(fleetctl.FleetError, 'public-only'):
                fleetctl.acquire_lease(self.tmp / 'state0', r, lid, 10)
        # owner moves the lane to code-ok.
        lane['data_policy'] = 'code-ok'
        chosen = fleetctl.choose_lane(r, {}, 'default', 'read-only', 'text', harness='opencode', data_class='code')
        self.assertEqual(chosen['lane_id'], lid)
        options, _ = selector.enumerate_options(r, {}, 'default', fleet=fleetctl, data_class='code')
        option = next(o for o in options if o.get('lane_id') == lid)
        argv = selector._command(option, 'default', self.tmp / 'receipt', fleet=fleetctl)
        self.assertIn('FLEET_DATA_CLASS=code', argv)
        self.assertNotIn('--direct', argv)  # code-ok lanes may read the project workspace
        with patch.dict(os.environ, {'FLEET_DATA_CLASS': 'code'}):
            self.assertTrue(fleetctl.acquire_lease(self.tmp / 'state1', r, lid, 10))
        # private material is still refused by code-ok, at selection and at acquisition.
        options, rejected = selector.enumerate_options(r, {}, 'default', fleet=fleetctl, data_class='private')
        self.assertNotIn(lid, [o.get('lane_id') for o in options])
        self.assertTrue(any('code-ok' in why and 'private' in why for why in rejected))
        self.assertNotEqual(fleetctl.choose_lane(r, {}, 'default', 'read-only', 'text', harness='opencode')['lane_id'], lid)
        with patch.dict(os.environ, {'FLEET_DATA_CLASS': 'private'}):
            with self.assertRaisesRegex(fleetctl.FleetError, 'code-ok'):
                fleetctl.acquire_lease(self.tmp / 'state2', r, lid, 10)
        # public material still reaches it, with the tool-less path unchanged only for public-only lanes.
        options, _ = selector.enumerate_options(r, {}, 'default', fleet=fleetctl, data_class='public')
        self.assertIn(lid, [o.get('lane_id') for o in options])

    def test_wrapper_code_ok_lane_runs_without_direct_and_refuses_private(self):
        # BR-DATA-001: real wrapper and roster validation accept the code-ok value.
        r, free = self._active_free()
        r.pop('chatgpt_gateway', None)
        r.pop('provider_sources', None)
        lane = next(l for l in r['lanes'] if l['lane_id'] == free['lane_id'])
        lane['data_policy'] = 'code-ok'
        self.overlay.write_text(patcher.dump(r))
        home = self.tmp / 'home'
        auth = home / '.local/share/opencode/auth.json'
        auth.parent.mkdir(parents=True)
        auth.write_text('{}')
        profile = home / '.config/opencode/fleet-worker'
        profile.mkdir(parents=True)
        (profile / 'opencode.jsonc').write_text('{}\n')
        (profile / 'AGENTS.md').write_text('# fake lean context\n')
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        executable = bin_dir / 'opencode'
        observed = self.tmp / 'observed.json'
        executable.write_text('#!' + sys.executable + '\n' +
            "import json,os,sys\n" +
            "from pathlib import Path\n" +
            "Path(os.environ['R19_OBSERVED']).write_text(json.dumps({'args':sys.argv[1:],'cwd':os.getcwd()}))\n" +
            "print(json.dumps({'type':'text','part':{'text':'CODE_OK'}}))\n" +
            "print(json.dumps({'type':'step_finish','part':{'reason':'stop'}}))\n")
        executable.chmod(0o755)
        env = dict(os.environ, HOME=str(home), ACCESS_OVERLAY=str(self.overlay),
                   FLEET_STATE_DIR=str(self.tmp / 'state'), FLEET_NO_AUTO_REFRESH='1',
                   PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], R19_OBSERVED=str(observed))
        argv = ['bash', str(ROOT / 'scripts/opencode-agent.sh'), '--lane', free['lane_id'],
                '--prompt', 'code fixture text', '--dir', str(self.tmp), '--timeout', '10']
        run = subprocess.run(argv, env=dict(env, FLEET_DATA_CLASS='private'), text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('code-ok', run.stderr)
        self.assertFalse(observed.exists())
        run = subprocess.run(argv, env=dict(env, FLEET_DATA_CLASS='code'), text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), 'CODE_OK')
        # The same lane as public-only still demands --direct and refuses code material.
        lane['data_policy'] = 'public-only'
        self.overlay.write_text(patcher.dump(r))
        observed.unlink()
        run = subprocess.run(argv, env=dict(env, FLEET_DATA_CLASS='code'), text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('public-only', run.stderr)
        self.assertFalse(observed.exists())

    def test_market_main_calls_writer_after_fetch_and_reports_summary(self):
        # BR-REFRESH-001
        rows = [catalog(FIXTURE['after'])]
        with patch.object(market_refresh, 'OVERLAY', self.overlay), patch.object(market_refresh, 'STATE_DIR', self.tmp / 'market'), \
                patch.object(daily, 'discover', return_value=rows), \
                patch.object(market_refresh, 'fetch', return_value='{}'), \
                patch.object(market_refresh, 'fetch_artificial_analysis', return_value=market()['artificial_analysis']), \
                patch.object(market_refresh, 'write_evidence'), patch.object(market_refresh, 'load_lmarena', return_value={}):
            self.assertEqual(market_refresh.main(), 0)
        report = (self.tmp / 'market/market-report.md').read_text()
        self.assertIn('Daily roster: 2 added', report)
        persisted = json.loads(self.overlay.read_text())
        self.assertTrue(any(l['model_key'] == 'new-helper' for l in persisted['lanes']))


if __name__ == '__main__':
    unittest.main()
