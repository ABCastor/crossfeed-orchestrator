"""Offline AGY catalogue, routing, effort and presentation agreement."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import admit_agy_models as admission, evidence, fleetctl, model_cards, selector
from tests.test_console import console

ROOT = Path(__file__).resolve().parents[1]


class AgyModelsTests(unittest.TestCase):
    def setUp(self):
        self.roster = json.loads((ROOT/'examples/access-overlay.example.json').read_text())
        # Disable unrelated discovery while preserving all admitted roster metadata.
        self.roster.pop('provider_sources', None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_patch_generator_refuses_to_overwrite_input(self):
        import subprocess, sys
        path = self.state/'overlay.json'
        path.write_text(json.dumps(self.roster))
        before = path.read_bytes()
        run = subprocess.run([sys.executable,str(ROOT/'scripts/admit_agy_models.py'),
                              '--overlay',str(path),'--patch',str(path)],text=True,capture_output=True)
        self.assertEqual(run.returncode,2)
        self.assertIn('--patch must differ',run.stderr)
        self.assertEqual(path.read_bytes(),before)

    def test_catalogue_fixture_matches_declared_list_and_roster_controls(self):
        fixture = json.loads((ROOT/'tests/fixtures/agy-models-1.4.0.json').read_text())
        lane = next(p for p in (ROOT/'skill/references/lane-antigravity.md', ROOT/'references/lane-antigravity.md') if p.is_file())  # repo or installed layout
        declared = lane.read_text().split('<!-- agy-models:start -->')[1].split('<!-- agy-models:end -->')[0]
        ids = set()
        for lane in self.roster['lanes']:
            if lane['harness'] != 'agy' or lane['admission_status'] != 'active':
                continue
            model = lane['model_key']
            self.assertIn(model, declared)
            self.assertIn(lane['selector'],fixture['ids'])
            self.assertTrue(lane['selector'].startswith(model+'-'))
            for level in self.roster['effort'][model]['levels']['agy']:
                ids.add(model+'-'+level)
        self.assertEqual(ids, set(fixture['ids']) - set(fixture['excluded']))
        import re
        self.assertEqual(set(re.findall(r'`([a-z0-9.-]+-(?:low|medium|high))`', declared)), ids)

    def test_migration_is_idempotent_and_preserves_native_controls_and_pool_settings(self):
        before = copy.deepcopy(self.roster)
        after = admission.migrate(before)
        self.assertEqual(after, admission.migrate(after))
        self.assertEqual(after['quota_pools'], before['quota_pools'])
        for model in ('claude-opus-5-5','claude-sonnet-5-5'):
            for key in ('default','knee','by_role','refuse_roles'):
                self.assertEqual(after['effort'][model].get(key), before['effort'][model].get(key))
            self.assertEqual(after['effort'][model]['levels']['claude'], before['effort'][model]['levels']['claude'])
        self.assertEqual(self.roster, before)
        self.assertEqual(before['model_cards']['claude-haiku-5-5']['status'],'current')

    def test_retired_agy_routes_refuse_with_the_named_current_standin(self):
        for model, successor in admission.SUPERSEDED.items():
            verdict = fleetctl.start_verdict(self.roster,{},model,harness='agy')
            self.assertIsNotNone(verdict,model)
            self.assertEqual(verdict['why'],'retired')
            self.assertTrue(verdict['instead'][0].startswith(successor+'-'),verdict)
            # A replacement that the owner switched off is never suggested.
            blocked = fleetctl.start_verdict(self.roster,{'model_preferences':{successor:'off'}},model,harness='agy')
            self.assertFalse(any(x.startswith(successor+'-') for x in blocked['instead']))

    def test_native_and_agy_defaults_are_independent_and_explicit_suffixes_resolve(self):
        for model in admission.MODELS:
            for level in admission.LEVELS:
                r = fleetctl.resolve_effort(self.roster,model+'-'+level,'review','agy',level)
                self.assertEqual((r['model_key'],r['effort']),(model,level))
            self.assertEqual(fleetctl.resolve_effort(self.roster,model,'audit','agy')['effort'],'high')
        self.assertEqual(fleetctl.resolve_effort(self.roster,'claude-sonnet-5-5','review','claude')['effort'],'xhigh')
        with self.assertRaises(fleetctl.FleetError):
            fleetctl.resolve_effort(self.roster,'claude-sonnet-5-5-high','review','agy','max')

    def test_doctor_reads_agy_controls_separately_from_native_claude(self):
        data = copy.deepcopy(self.roster)
        data['lanes'] = [l for l in data['lanes'] if l['model_key'] in admission.MODELS and l['harness']=='agy']
        data['model_cards'] = {}
        problems = fleetctl.effort_problems(data,self.state)
        self.assertFalse(any('unsupported effort' in p for p in problems),problems)

    def test_selector_emits_exact_ids_and_correct_pools_without_readonly_bypass(self):
        opts, _ = selector.enumerate_options(self.roster,{},'design',mode='write',fleet=fleetctl)
        for model,(pool,_) in admission.MODELS.items():
            chosen = [r for r in opts if r['harness']=='agy' and r['model_key']==model]
            self.assertEqual({r['level'] for r in chosen},set(admission.LEVELS))
            for row in chosen:
                self.assertEqual(row['pool'],pool)
                self.assertEqual(row['run_as'],model+'-'+row['level'])
        opts,_ = selector.enumerate_options(self.roster,{},'writing',mode='read-only',fleet=fleetctl)
        self.assertFalse(any(r['harness']=='agy' for r in opts))
        opts,_ = selector.enumerate_options(self.roster,{'model_preferences':{'gemini-4-argon':'off'}},'design',mode='write',fleet=fleetctl)
        self.assertFalse(any(r['model_key']=='gemini-4-argon' for r in opts))
        self.assertTrue(all(l['admission_status']=='retired' for l in self.roster['lanes'] if l.get('harness')=='agy' and l['model_key'] in admission.SUPERSEDED))

    def test_roster_schema_and_brief_through_cli(self):
        import os, subprocess, sys
        path = self.state/'overlay.json'; path.write_text(json.dumps(self.roster))
        env = dict(os.environ, ACCESS_OVERLAY=str(path), FLEET_STATE_DIR=str(self.state), FLEET_NO_AUTO_REFRESH='1')
        checked = subprocess.run(['bash',str(ROOT/'scripts/roster.sh'),'validate'],env=env,text=True,capture_output=True)
        self.assertEqual(checked.returncode,0,checked.stderr)
        overview = fleetctl.fleet_overview(self.roster,{},self.state)
        brief = fleetctl.render_brief(overview,roster=self.roster,runtime={})
        self.assertIn('gemini-4-argon',brief)
        self.assertIn('claude-opus-5-5',brief)
        self.assertNotIn('claude-opus-4-6-thinking',brief.split('off:')[0])

    def test_writing_fit_trial_does_not_train_other_levels_or_visual_quality(self):
        record = {'run_id':'synthetic-writing-fit','model_key':'gemini-4-argon', 'level':'high',
                  'family':'writing','outcome':{'mechanically_verified':True,'passed':True}}
        rows = evidence.build_evidence(self.roster,{},[record])['rows']
        high = next(r for r in rows if r['model_key']=='gemini-4-argon' and r['level']=='high')
        low = next(r for r in rows if r['model_key']=='gemini-4-argon' and r['level']=='low')
        self.assertEqual(high['own']['writing'],{'passes':1,'trials':1})
        self.assertFalse(high['q']['writing']['unknown'])
        self.assertTrue(high['q']['visual']['unknown'])
        self.assertTrue(low['q']['writing']['unknown'])

    def test_cards_and_console_include_new_models_with_unknown_quality(self):
        levels = evidence.build_evidence(self.roster,{},[])
        cards = model_cards.build_cards(self.roster,{},levels,fleet=fleetctl)
        for model in admission.MODELS:
            rows = [r for r in cards['rows'] if r['model']==model and r['level'] in admission.LEVELS]
            self.assertEqual(len(rows),3)
            for row in rows:
                self.assertEqual(row['evidence_counts']['trials'],0)
                self.assertIn('writing',row['unmeasured_families'])
        benchmark_page = console.render_benchmarks(cards)
        for model in admission.MODELS:
            self.assertIn(model,benchmark_page)
        path = self.state/'overlay.json';path.write_text(json.dumps(self.roster))
        fleetctl.atomic_json(self.state/'evidence/levels.json',levels)
        app = console.Console(path,self.state,8768)
        with patch.object(console.fleetctl,'read_overlay',return_value=self.roster):
            page = console.render_page(app.overview(refresh=False),'synthetic-session')
        for name in ('Gemini 4 Argon','Opus 5.5','Sonnet 5.5'):
            self.assertIn(name,page)


from tests.test_switch_respected import Sandbox

class AgyWrapperModelsTests(Sandbox):
    def test_claude_suffix_explicit_effort_and_pool_receipt(self):
        data = json.loads((ROOT/'examples/access-overlay.example.json').read_text())
        data.pop('chatgpt_gateway',None)
        data['swarm_profiles'] = {}
        for rule in data['routing']['roles'].values():
            for band, lanes in rule.items():
                if isinstance(lanes,list):
                    rule[band] = [x for x in lanes if x != 'chatgpt']
        self.write_roster(data)
        argv = self.fake_cli('agy','echo answer\n')
        for args, expected in [(['--model','claude-sonnet-5-5-low'],'low'),
                               (['--model','claude-sonnet-5-5-low','--effort','high'],'high'),
                               (['--lane','antigravity-claude-opus-5-5-medium'],'medium')]:
            run = self.run_script('agy-agent.sh',*args,'--prompt','synthetic task','--dir',str(self.root))
            self.assertEqual(run.returncode,0,run.stderr)
            called = argv.read_text().splitlines()
            model = called[called.index('--model')+1]
            self.assertTrue(model.endswith('-'+expected),model)
            records = [json.loads(x) for x in (self.state/'runs.jsonl').read_text().splitlines()]
            receipt = [r for r in records if r.get('schema')=='crossfeed-model-run/v1'][-1]
            self.assertEqual(receipt['selected_model'],model.rsplit('-',1)[0])
            self.assertEqual(receipt['effort'],expected)
            self.assertIsNone(receipt['actual_model'])
            self.assertEqual(receipt['quota_pool'],'antigravity-3p')


if __name__ == '__main__':
    unittest.main()
