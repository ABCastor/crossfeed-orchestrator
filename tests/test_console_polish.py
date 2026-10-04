"""Console provider selection and minutes-long research seats, with fake services only."""
import copy
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import providers
from tests import test_chatgpt, test_console

ROOT = Path(__file__).resolve().parents[1]


class Headings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.flex_children = []
        self.titles = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.stack and self.stack[-1] == 'h2':
            self.flex_children.append((tag, attrs.get('class')))
        if attrs.get('class') == 'pool-title':
            self.in_title = True
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == 'h2':
            self.in_title = False
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        if self.in_title:
            self.titles.append(data)


class ConsolePolishTests(unittest.TestCase):
    def test_translation_spans_stay_inside_one_heading_child(self):
        for label in ['ChatGPT chats via Crossfeed Chat', 'Antigravity · Claude and GPT', 'Antigravity · Gemini', 'Codex (ChatGPT)']:
            page = test_console.console.keep_html('<h2>' + test_console.console._handle(label)
                                                 + test_console.console._pool_heading(label) + '</h2>')
            parsed = Headings()
            parsed.feed(page)
            self.assertEqual(parsed.flex_children, [('button', 'order-handle'), ('span', 'pool-title')], label)
            expected = label.replace(' · ', ' ')
            self.assertEqual(''.join(parsed.titles), expected)
        self.assertEqual(test_console.console.fleetctl.pool_label('chatgpt-work', {'label': 'ChatGPT Chat via Crossfeed Chat'}),
                         'ChatGPT chats via Crossfeed Chat')

    def test_picker_covers_every_supported_kind_and_explicit_model_choice(self):
        page = test_console.console.provider_setup([], 'token')
        for kind in providers.KINDS:
            self.assertIn(f'<option value="{kind}">', page)
        self.assertIn('OpenRouter, paid or free', page)
        self.assertIn('provider-model-list', page)
        self.assertIn('name="models" rows="3" required', page)

    def test_openrouter_paid_model_uses_budgeted_pi_not_free_only_wrapper(self):
        catalog = {'data': [{'id': 'vendor/paid', 'architecture': {'output_modalities': ['text']}, 'pricing': {'prompt': '0.001', 'completion': '0.002'}},
                            {'id': 'vendor/free:free', 'architecture': {'output_modalities': ['text']}, 'pricing': {'prompt': '0', 'completion': '0'}},
                            {'id': 'vendor/image', 'architecture': {'output_modalities': ['image']}, 'pricing': {'prompt': '0', 'completion': '0'}}]}
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(catalog).encode())
        with tempfile.TemporaryDirectory() as tmp, patch.object(providers.urllib.request, 'build_opener', return_value=opener):
            path = Path(tmp) / 'overlay.json'
            path.write_text(json.dumps(test_console.base_overlay()))
            result = providers.probe('openrouter', 'https://openrouter.ai/api/v1', key='synthetic-key')
            self.assertEqual(result['models'], ['vendor/paid', 'vendor/free:free'])
            providers.add(path, id='router', kind='openrouter', base_url='https://openrouter.ai/api/v1',
                          key='synthetic-key', models='vendor/paid', daily_cap='2')
            roster = json.loads(path.read_text())
            lane = next(l for l in roster['lanes'] if l.get('provider_source') == 'router')
            self.assertEqual(lane['harness'], 'pi')
            self.assertEqual(lane['selector'], 'router/vendor/paid')
            self.assertEqual(lane['transport']['api_base'], 'https://openrouter.ai/api/v1')
            self.assertEqual(lane['access_status'], 'unverified')
            self.assertEqual(roster['quota_pools']['router']['daily_usd_cap'], 2)
            self.assertNotIn('vendor/free:free', roster['provider_sources']['router']['models'])
            # Execute through a fake Pi, then prove its estimated spend reaches the next lease gate.
            lane['access_status'] = 'verified'
            roster['quota_pools']['router']['daily_usd_cap'] = .01
            path.write_text(json.dumps(roster))
            binary_dir = Path(tmp) / 'bin'
            binary_dir.mkdir()
            binary = binary_dir / 'pi'
            binary.write_text("""#!/usr/bin/env python3
import json,os,pathlib,sys
config=json.loads((pathlib.Path(os.environ['PI_CODING_AGENT_DIR'])/'models.json').read_text())['providers']['router']
assert config['models'][0]['id']=='vendor/paid'
assert config['models'][0]['cost']['input']==1000
sys.stdin.read()
m={'role':'assistant','provider':'router','model':'vendor/paid','stopReason':'stop','content':[{'type':'text','text':'PONG'}],
   'usage':{'input':10,'output':3,'totalTokens':13,'cost':{'total':0}}}
print(json.dumps({'type':'message_end','message':m}))
print(json.dumps({'type':'agent_end','messages':[m]}))
""")
            binary.chmod(0o755)
            state = Path(tmp) / 'state'
            env = {**os.environ, 'ACCESS_OVERLAY': str(path), 'FLEET_STATE_DIR': str(state),
                   'FLEET_NO_AUTO_REFRESH': '1', 'PATH': str(binary_dir) + os.pathsep + os.environ['PATH']}
            answer = Path(tmp) / 'answer'
            run = subprocess.run([str(ROOT/'scripts/pi-agent.sh'), 'run', '--lane', lane['lane_id'], '--prompt', 'Inspect.',
                                  '--dir', tmp, '--last', str(answer)], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)
            receipt = json.loads(Path(str(answer)+'.crossfeed.json').read_text())
            self.assertAlmostEqual(receipt['cost']['estimated_usd'], .016)
            lease = subprocess.run([str(ROOT/'scripts/fleetctl.py'), 'acquire', '--lane', lane['lane_id']],
                                   env=env, capture_output=True, text=True)
            self.assertNotEqual(lease.returncode, 0)
            self.assertIn('daily spend cap', lease.stderr)

            gate = subprocess.run([str(ROOT / 'scripts/roster.sh'), 'validate'],
                                  env={**os.environ, 'ACCESS_OVERLAY': str(path)}, capture_output=True, text=True)
            self.assertEqual(gate.returncode, 0, gate.stderr)

    def test_openrouter_keeps_each_models_price_and_omits_unknown_prices(self):
        catalog = {'data': [
            {'id': 'a/paid', 'architecture': {'output_modalities': ['text']}, 'pricing': {'prompt': '0.001', 'completion': '0.002'}},
            {'id': 'b/paid', 'architecture': {'output_modalities': ['text']}, 'pricing': {'prompt': '0.003', 'completion': '0.004'}},
            {'id': 'unknown/paid', 'architecture': {'output_modalities': ['text']}, 'pricing': {'prompt': '-1', 'completion': '-1'}}]}
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(catalog).encode())
        with tempfile.TemporaryDirectory() as tmp, patch.object(providers.urllib.request, 'build_opener', return_value=opener):
            path = Path(tmp) / 'overlay.json'
            path.write_text(json.dumps(test_console.base_overlay()))
            result = providers.probe('openrouter', 'https://openrouter.ai/api/v1', key='synthetic-key')
            self.assertEqual(result['models'], ['a/paid', 'b/paid'])
            providers.add(path, id='router', kind='openrouter', base_url='https://openrouter.ai/api/v1',
                          key='synthetic-key', models='a/paid,b/paid', daily_cap='1')
            lanes = [l for l in json.loads(path.read_text())['lanes'] if l.get('provider_source') == 'router']
            self.assertEqual([l['api_pricing']['input'] for l in lanes], [1000, 3000])

    def test_cli_connection_check_does_not_require_model_ids(self):
        with patch.object(providers.subprocess, 'run', return_value=Mock(returncode=0)):
            result = providers.probe('codex')
            self.assertEqual(result['models'], [])
            self.assertIn('CLI found', result['message'])


class ResearchSeatTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_chatgpt.ChatGPTTests(methodName='test_success_and_unconfirmed_identity_unknown_usage')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.work = self.fixture.work
        self.fixture.gateway.selector = 'chatgpt:latest-pro'
        self.fixture.gateway.catalog_ids = ['chatgpt:latest-pro']
        self.fixture.save()

    def fanout(self, task, *, dry=True, name='out'):
        path = self.work / (name + '.jsonl')
        path.write_text(json.dumps({'id': 'pro', 'agent': 'chatgpt-chat', 'model': 'chatgpt:latest-pro',
                                   'dir': str(self.work), 'mode': 'read-only', 'role': 'hard-reasoning', **task}) + '\n')
        args = [str(ROOT / 'scripts/fanout.sh'), str(path), '--out', str(self.work / name)]
        if dry:
            args.append('--dry-run')
        return subprocess.run(args, env=self.fixture.env, capture_output=True, text=True, timeout=15)

    def test_fanout_executes_buffered_chat_with_no_implicit_timeout(self):
        self.fixture.gateway.mode = 'buffered'
        result = self.fanout({'prompt': 'Reason from these source excerpts.'}, dry=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.work / 'out/manifest.jsonl').read_text())
        self.assertEqual(manifest['timeout'], 0)
        self.assertEqual((self.work / 'out/pro.out').read_text(), 'PONG\n')
        receipt = json.loads((self.work / 'out/pro.out.crossfeed.json').read_text())
        self.assertEqual(receipt['selected_model'], 'chatgpt:latest-pro')
        self.assertIsNone(receipt['actual_model'])

    def test_fanout_rejects_write_attachments_and_thinking_override(self):
        for i, change in enumerate([{'mode': 'write'}, {'modality': 'image'}, {'effort': 'high'}, {'role': 'builder'}]):
            result = self.fanout({'prompt': 'Inspect.', **change}, name=f'refuse-{i}')
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertFalse((self.work / f'refuse-{i}/summary.tsv').exists())

    def test_validator_uses_saved_label_contract_for_added_gateways(self):
        roster = json.loads((ROOT / 'examples/access-overlay.example.json').read_text())
        roster['provider_sources'] = {'chat': {'kind': 'crossfeed-chat'}}
        roster.pop('chatgpt_gateway')
        # Static route families use the gateway slot, so clear these unrelated rankings here.
        roster['routing']['roles'] = {}
        roster['swarm_profiles'] = {'research': {'bands': {
            band: {'parallel': 1, 'workers': [{'id': 'pro', 'selector': 'chatgpt:Latest Pro α', 'angle': 'Inspect.'}]}
            for band in ['quality_first', 'unknown', 'conserve', 'critical']}}}
        self.fixture.overlay.write_text(json.dumps(roster))
        result = subprocess.run([str(ROOT/'scripts/roster.sh'), 'validate'], env=self.fixture.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        roster['swarm_profiles']['research']['bands']['unknown']['workers'][0]['selector'] = 'chatgpt: bad '
        self.fixture.overlay.write_text(json.dumps(roster))
        result = subprocess.run([str(ROOT/'scripts/roster.sh'), 'validate'], env=self.fixture.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 3, result.stderr)

    def test_research_profile_reserves_pro_in_every_band_and_can_exclude_openai(self):
        example = json.loads((ROOT / 'examples/access-overlay.example.json').read_text())
        profile = example['swarm_profiles']['research']
        for band in profile['bands'].values():
            pro = next(w for w in band['workers'] if w.get('selector') == 'chatgpt:latest-pro')
            self.assertEqual(pro['timeout'], 0)
        # Keep one real fake-gateway seat and one static admitted critic; no external worker runs.
        pro = profile['bands']['unknown']['workers'][0]
        critic = example['lanes'][0]
        critic.update(access_status='verified', admission_status='active', allowed_modes=['read-only'])
        self.fixture.roster['lanes'] = [critic]
        self.fixture.roster['swarm_profiles'] = {'research': {'bands': {
            band: {'parallel': 2, 'workers': [copy.deepcopy(pro), {'id': 'critic', 'lane_id': critic['lane_id'], 'angle': 'Challenge.'}]}
            for band in ['quality_first', 'unknown', 'conserve', 'critical']}}}
        self.fixture.save()
        args = [str(ROOT / 'scripts/swarm.sh'), 'research', '--prompt', 'Review excerpts.', '--dir', str(self.work),
                '--dry-run', '--out', str(self.work / 'swarm')]
        result = subprocess.run(args, env=self.fixture.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('chatgpt:latest-pro', result.stdout)
        args[-1] = str(self.work / 'swarm-excluded')
        result = subprocess.run(args + ['--exclude-lineage', 'openai'], env=self.fixture.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('chatgpt:latest-pro', result.stdout)
        self.assertIn('excluded lead lineage', result.stderr)
        self.assertIn('PANEL INCOMPLETE', result.stderr)


if __name__ == '__main__':
    unittest.main()
