"""Run-correlated provider refusals override gauges, isolate pools and expire."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import agy_quota_pools as migration, evidence, fleetctl, model_cards, selector
from tests.test_console import console
from tests.test_switch_respected import Sandbox

ROOT = Path(__file__).resolve().parents[1]
ARGON_ERROR = (ROOT/'tests/fixtures/agy-individual-quota.txt').read_text()
CLAUDE_ERROR = 'RESOURCE_EXHAUSTED (429): Individual quota reached ... Resets in 1h18m27s'
NOW = fleetctl.parse_iso('2026-10-16T13:03:00Z')


class QuotaPoolTests(unittest.TestCase):
    def setUp(self):
        self.roster = json.loads((ROOT/'examples/access-overlay.example.json').read_text())
        self.roster.pop('provider_sources', None)
        self.roster.pop('chatgpt_gateway', None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.argon = next(l['lane_id'] for l in self.roster['lanes'] if l['model_key']=='gemini-4-argon')
        self.claude = next(l['lane_id'] for l in self.roster['lanes'] if l['model_key']=='claude-opus-5-5' and l['harness']=='agy')

    def record(self, lane, message):
        with patch.object(fleetctl, 'utc_now', return_value=NOW):
            fleetctl.record_quota_error(self.state, self.roster, lane, message)
        return json.loads((self.state/'runtime.json').read_text())

    def models(self, runtime, now=NOW, **kwargs):
        with patch.object(fleetctl, 'utc_now', return_value=now):
            options, _ = selector.enumerate_options(self.roster, runtime, 'design', mode='write', fleet=fleetctl, **kwargs)
        return {r['model_key'] for r in options if r['harness']=='agy'}

    def test_exact_duration_and_variants(self):
        for text, seconds in [(ARGON_ERROR,592340),(CLAUDE_ERROR,4707),
                              ('Individual quota reached Resets in 2h',7200),
                              ('Individual quota reached Resets in 9m3s',543),
                              ('Individual quota reached Resets in 1h 18m 27s.',4707)]:
            self.assertEqual(fleetctl.quota_refusal(text,agy=True)['retry_after_seconds'],seconds)
        self.assertEqual(fleetctl.iso(fleetctl.parse_iso('2026-10-16T12:45:31Z') + dt.timedelta(seconds=fleetctl.quota_refusal(ARGON_ERROR,agy=True)['retry_after_seconds'])), '2026-10-23T09:17:51Z')
        self.assertIsNone(fleetctl.quota_refusal('ordinary error',agy=True))
        self.assertIsNone(fleetctl.quota_refusal('The quota feature is implemented.',agy=True))
        self.assertIsNone(fleetctl.quota_refusal('AGY QUOTA EXHAUSTED (429) - empty output; marker in /tmp/agy-quota/agy-home/cli.log',agy=True))
        self.assertEqual(fleetctl.quota_refusal('HTTP 429 retry-after: 42')['retry_after_seconds'],42)

    def test_argon_blocks_only_argon_and_recovers_at_exact_reset(self):
        control = self.models({})
        self.assertTrue({'gemini-4-argon','gemini-3.8-flash','claude-opus-5-5'} <= control)
        runtime = self.record(self.argon, ARGON_ERROR)
        until = runtime['pool_circuits'][migration.ARGON_POOL]['until']
        self.assertEqual(until,'2026-10-23T09:35:20Z')
        self.assertNotIn('gemini-4-argon',self.models(runtime))
        self.assertTrue({'gemini-3.8-flash','claude-opus-5-5'} <= self.models(runtime))
        reset = fleetctl.parse_iso(until)
        self.assertNotIn('gemini-4-argon',self.models(runtime, reset-dt.timedelta(seconds=1)))
        self.assertIn('gemini-4-argon',self.models(runtime, reset))
        runtime['switches'] = {migration.ARGON_POOL:'on'}
        self.assertEqual(fleetctl.current_pool_state(runtime,migration.ARGON_POOL,NOW,roster=self.roster)[0],'EXHAUSTED')

    def test_claude_low_gauge_cannot_clear_refusal_and_recovery(self):
        runtime = self.record(self.claude, CLAUDE_ERROR)
        runtime['quota_snapshots'] = {'antigravity-3p':{'observed_at':fleetctl.iso(NOW),
            'source':'synthetic-3-percent','windows':{'rolling_5h':{'used_percent':3,
            'reset_at':'2026-10-16T18:03:00Z','window_minutes':300}}}}
        state, info = fleetctl.current_pool_state(runtime,'antigravity-3p',NOW,roster=self.roster)
        self.assertEqual((state,info['until']),('EXHAUSTED','2026-10-16T14:21:27Z'))
        self.assertNotIn('claude-opus-5-5',self.models(runtime))
        self.assertTrue({'gemini-4-argon','gemini-3.8-flash'} <= self.models(runtime))
        self.assertIn('claude-opus-5-5',self.models(runtime,fleetctl.parse_iso(info['until'])))
        # A generic subsequent cooldown cannot shorten a known reset.
        self.record(self.claude,'HTTP 429 retry-after: 1')
        self.assertEqual(json.loads((self.state/'runtime.json').read_text())['pool_circuits']['antigravity-3p']['until'],info['until'])

    def test_paid_argon_is_never_automatic_fallback(self):
        lane = copy.deepcopy(next(l for l in self.roster['lanes'] if l['lane_id']==self.argon))
        lane.update(lane_id='paid-argon',harness='opencode',quota_pool='gemini-metered',selector='google/gemini-4-argon')
        self.roster['effort']['gemini-4-argon']['levels']['opencode']=['high']
        self.roster['lanes'].append(lane)
        self.roster['quota_pools']['gemini-metered']['explicit_only']=True
        runtime = self.record(self.argon,ARGON_ERROR)
        with patch.object(fleetctl,'utc_now',return_value=NOW):
            options,_ = selector.enumerate_options(self.roster,runtime,'design',mode='write',fleet=fleetctl)
            self.assertFalse(any(r['model_key']=='gemini-4-argon' for r in options))
            explicit,_ = selector.enumerate_options(self.roster,runtime,'design',mode='write',fleet=fleetctl,explicit_pools={'gemini-metered'})
            self.assertTrue(any(r['pool']=='gemini-metered' and r['model_key']=='gemini-4-argon' for r in explicit))

    def test_manual_gauge_refresh_preserves_agy_refusal(self):
        from argparse import Namespace
        import contextlib
        import io
        self.record(self.claude,CLAUDE_ERROR)
        window='antigravity-quota-summary-3p-5h'
        snapshot={'available':True,'source':'synthetic','observed_at':fleetctl.iso(NOW),
                  'windows':{window:{'used_percent':3,'reset_at':'2026-10-16T18:03:00Z','window_minutes':300}}}
        with patch.dict(fleetctl.ORACLE_REGISTRY,{'codexbar':lambda *a,**kw: snapshot}), contextlib.redirect_stdout(io.StringIO()), patch.object(fleetctl,'utc_now',return_value=NOW):
            fleetctl.codexbar_snapshot_command(Namespace(provider='antigravity-3p',timeout=1,json=True),self.state,self.roster)
        runtime=json.loads((self.state/'runtime.json').read_text())
        self.assertEqual(runtime['pool_circuits']['antigravity-3p']['until'],'2026-10-16T14:21:27Z')
        self.assertEqual(fleetctl.current_pool_state(runtime,'antigravity-3p',NOW,roster=self.roster)[0],'EXHAUSTED')

    def test_record_telemetry_uses_same_parser(self):
        error = self.state/'stderr'; error.write_text(ARGON_ERROR)
        with patch.object(fleetctl,'utc_now',return_value=NOW):
            record = fleetctl.record_run(self.state,self.roster,self.argon,None,error,3,None)
        self.assertEqual(record['error']['retry_after_seconds'],592340)
        self.assertIn(migration.ARGON_POOL,json.loads((self.state/'runtime.json').read_text())['pool_circuits'])

    def test_migration_preserves_source_controls_and_does_not_seed_examples(self):
        before = copy.deepcopy(self.roster)
        for l in before['lanes']:
            if l['model_key']=='gemini-4-argon' and l['harness']=='agy':
                l['quota_pool']='antigravity-gemini'
        before['quota_pools'].pop(migration.ARGON_POOL)
        after = migration.migrate(before)
        self.assertEqual(after,migration.migrate(after))
        self.assertNotIn(migration.ARGON_POOL,before['quota_pools'])
        self.assertEqual(after['quota_pools']['antigravity-gemini'],before['quota_pools']['antigravity-gemini'])
        self.assertEqual(after['effort'],before['effort'])
        with patch.object(fleetctl,'utc_now',return_value=NOW):
            self.assertEqual(fleetctl.current_pool_state({},migration.ARGON_POOL,roster=after)[0],'EXHAUSTED')
        at_reset = fleetctl.parse_iso('2026-10-23T09:18:00Z')
        self.assertNotEqual(fleetctl.current_pool_state({},migration.ARGON_POOL,at_reset,roster=after)[0],'EXHAUSTED')
        self.assertNotIn('quota_error',self.roster['quota_pools'][migration.ARGON_POOL])

    def test_brief_providers_benchmarks_show_independent_reset(self):
        runtime = self.record(self.argon,ARGON_ERROR)
        self.record(self.claude,CLAUDE_ERROR)
        runtime = json.loads((self.state/'runtime.json').read_text())
        with patch.object(fleetctl,'utc_now',return_value=NOW):
            overview = fleetctl.fleet_overview(self.roster,runtime,self.state,NOW)
            brief = fleetctl.render_brief(overview,roster=self.roster,runtime=runtime)
            verbose = fleetctl.render_brief(overview,verbose=True)
            cards = model_cards.build_cards(self.roster,runtime,evidence.build_evidence(self.roster,runtime,[]),fleet=fleetctl)
            page = console.render_page(overview,'synthetic-session')
            benchmarks = console.render_benchmarks(cards)
        for pool, reset in [(migration.ARGON_POOL,'2026-10-23T09:35:20Z'),('antigravity-3p','2026-10-16T14:21:27Z')]:
            self.assertIn(pool,brief)
            self.assertIn(reset,brief)
            self.assertIn(pool,verbose)
            self.assertIn(pool,page)
            entry = next(p for p in overview['pools'] if p['pool']==pool)
            self.assertEqual(entry['until'],reset)
            self.assertEqual(entry['limits'][0]['reset_at'],reset)
            self.assertIn(reset,benchmarks)
            self.assertIn('EXHAUSTED',benchmarks)

    def test_patch_cli_validates_applies_and_is_idempotent(self):
        source = json.loads((ROOT/'examples/access-overlay.example.json').read_text())
        path = self.state/'access-overlay.json'; path.write_text(json.dumps(source))
        before = path.read_bytes()
        output = self.state/'live.patch'
        command = [sys.executable,str(ROOT/'scripts/agy_quota_pools.py'),'--overlay',str(path),'--patch',str(output)]
        result = subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(path.read_bytes(),before)
        applied = subprocess.run(['patch','--batch',str(path),str(output)],capture_output=True,text=True)
        self.assertEqual(applied.returncode,0,applied.stderr)
        self.assertEqual(json.loads(path.read_text()),migration.migrate(source))
        self.assertEqual(subprocess.run(command,capture_output=True).returncode,0)
        self.assertEqual(output.read_text(),'')
        bad = subprocess.run(command[:-1]+[str(path)],capture_output=True,text=True)
        self.assertEqual(bad.returncode,2)


class WrapperQuotaTests(Sandbox):
    def setUp(self):
        super().setUp()
        data = json.loads((ROOT/'examples/access-overlay.example.json').read_text())
        data.pop('chatgpt_gateway',None)
        data.pop('provider_sources',None)
        data['swarm_profiles']={}
        for rule in data['routing']['roles'].values():
            for band, lanes in rule.items():
                if isinstance(lanes,list): rule[band]=[x for x in lanes if x!='chatgpt']
        self.write_roster(data)

    def test_wrapper_persists_full_error_and_blocks_next_call_for_both_pools(self):
        for model,message,pool in [('gemini-4-argon-high',ARGON_ERROR,migration.ARGON_POOL),
                                   ('claude-opus-5-5-high',CLAUDE_ERROR,'antigravity-3p')]:
            with self.subTest(model=model):
                args = self.fake_cli('agy',"echo '"+message.strip()+"'\nexit 3\n")
                run = self.run_script('agy-agent.sh','--model',model,'--prompt','synthetic quota task','--dir',str(self.root))
                self.assertEqual(run.returncode,3,run.stderr)
                runtime = json.loads((self.state/'runtime.json').read_text())
                seconds = 592340 if pool==migration.ARGON_POOL else 4707
                circuit = runtime['pool_circuits'][pool]
                self.assertEqual((fleetctl.parse_iso(circuit['until'])-fleetctl.parse_iso(circuit['opened_at'])).total_seconds(),seconds)
                called = args.stat().st_mtime_ns
                blocked = self.run_script('agy-agent.sh','--model',model,'--prompt','synthetic quota task','--dir',str(self.root))
                self.assertEqual(blocked.returncode,5,blocked.stderr)
                self.assertEqual(called,args.stat().st_mtime_ns)

    def test_exit_zero_refusal_blocks_but_stale_global_log_does_not_open_circuit(self):
        self.fake_cli('agy',"echo '"+CLAUDE_ERROR+"'\n")
        run = self.run_script('agy-agent.sh','--model','claude-sonnet-5-5-high','--prompt','synthetic','--dir',str(self.root))
        self.assertEqual(run.returncode,3,run.stderr)
        log = self.home/'.gemini/antigravity-cli/cli.log'
        log.parent.mkdir(parents=True);log.write_text(ARGON_ERROR)
        self.fake_cli('agy','exit 0\n')
        run = self.run_script('agy-agent.sh','--model','gemini-4-argon-high','--prompt','synthetic','--dir',str(self.root))
        self.assertEqual(run.returncode,3,run.stderr)
        runtime=json.loads((self.state/'runtime.json').read_text())
        self.assertNotIn(migration.ARGON_POOL,runtime['pool_circuits'])


if __name__ == '__main__': unittest.main()
