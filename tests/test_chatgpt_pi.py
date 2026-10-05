"""Generated Pi relay admission, coding tools and evidence against a local fixture."""
import copy
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import evidence
import fleetctl
import selector


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer fixture-key':
            self.send_error(401)
            return
        rows = [{'id': 'chatgpt:fixture-' + level, 'object': 'model', 'saved': True,
                 'row': 'Latest', 'level': position, 'replicas': 2}
                for position, level in ((1, 'medium'), (2, 'high'), (3, 'xhigh'), (4, 'pro'))]
        rows.append({'id': 'chatgpt:older-high', 'object': 'model', 'saved': True, 'row': 'Older', 'level': 2})
        payload = {'object': 'list', 'data': rows} if self.path == '/v1/models' else {
            'workers': [{'label': row['id'].removeprefix('chatgpt:'), 'contact': 'never',
                         'quota_blocked': self.server.blocked} for row in rows],
            'extension_enabled': True, 'wakes': [], 'wake_limits': {
                'attempts': 100, 'daily_cap': 0, 'hourly_attempts': 0, 'hourly_cap': 12, 'cooldown_until': 0}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PiRelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        self.server.blocked = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.overlay = self.root / 'overlay.json'
        self.roster = json.loads((ROOT / 'examples/access-overlay.example.json').read_text())
        gateway = self.roster['chatgpt_gateway']
        gateway['pi_coding'] = True
        template = gateway['lane_template']
        template['transport']['api_base'] = f'http://127.0.0.1:{self.server.server_port}/v1'
        (self.root / 'key').write_text('fixture-key')
        template['auth']['key_file'] = str(self.root / 'key')
        self.roster['policy']['selector'] = {'allowed_pools_by_role': {'default': ['codex', 'chatgpt-work']},
                                           'explore': 0, 'lambda_unknown': 0}
        self.roster['quota_pools']['chatgpt-work']['plan'] = {'limit': 'none-known'}
        self.save()
        self.environment = patch.dict(os.environ, ACCESS_OVERLAY=str(self.overlay), FLEET_STATE_DIR=str(self.state),
                                      FLEET_NO_AUTO_REFRESH='1', FLEET_QUOTA_POLICY='clock_aware',
                                      FLEET_SELECTION_FILE='')
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def save(self):
        self.overlay.write_text(json.dumps(self.roster))

    def expanded(self):
        return fleetctl.read_overlay(self.overlay, self.state)

    def relay_lanes(self, roster=None):
        return [lane for lane in (roster or self.expanded())['lanes'] if lane.get('chatgpt_pi')]

    def measured(self):
        records = []
        for model, level, passes, trials, latency in (
                ('gpt-6.1-sol', 'high', 12, 12, 232.3),
                ('chatgpt:fixture-high', 'provider-default', 9, 11, 340.8),
                ('chatgpt:fixture-xhigh', 'provider-default', 9, 10, 402.1)):
            for index in range(trials):
                record = {'run_id': model + ':' + str(index), 'model_key': model, 'effort': level,
                          'family': 'coding-agent', 'completion_time_s': latency,
                          'outcome': {'mechanically_verified': True, 'passed': index < passes}}
                if model.startswith('chatgpt:'):
                    record['evidence_flags'] = ['coverage_incomplete', 'calibration_unmeasured',
                                                'model_identity_unconfirmed', 'synthetic_coding_only',
                                                'model_harness_specific']
                records.append(record)
        self.state.mkdir(exist_ok=True)
        (self.state / 'runs.jsonl').write_text(''.join(json.dumps(record) + '\n' for record in records))
        return evidence.write_evidence(self.state, self.expanded(), {})

    def test_opt_in_generates_current_high_and_xhigh_coding_routes_only(self):
        roster = self.expanded()
        lanes = self.relay_lanes(roster)
        self.assertEqual({lane['worker_level'] for lane in lanes}, {'high', 'xhigh'})
        for lane in lanes:
            self.assertEqual(lane['harness'], 'pi')
            self.assertEqual(lane['max_parallel'], 2)
            self.assertEqual(lane['quota_pool'], 'chatgpt-work')
            self.assertIn('write', lane['allowed_modes'])
            self.assertIn('implementation', lane['roles'])
            self.assertNotIn('review', lane['roles'])
            self.assertEqual(roster['effort'][lane['model_key']]['levels']['pi'], ['provider-default'])
        self.roster['chatgpt_gateway']['pi_coding'] = False
        self.save()
        self.assertEqual(self.relay_lanes(), [])

    def test_real_generated_options_keep_default_then_fall_back_under_pressure(self):
        self.measured()
        roster = self.expanded()
        allow = 'codex:gpt-6.1-sol:high,chatgpt-work:chatgpt:fixture-high:*,chatgpt-work:chatgpt:fixture-xhigh:*'
        def select(runtime, stakes='normal'):
            return selector.select_option(roster, runtime, self.state, 'implementation',
                                          stakes=stakes, allow=allow, fleet=fleetctl)
        self.assertEqual(select({})['choice']['model_key'], 'gpt-6.1-sol')
        runtime = {'quota_snapshots': {'codex': {'observed_at': fleetctl.iso(), 'windows': {'weekly': {
            'used_percent': 60, 'window_minutes': 10080,
            'reset_at': fleetctl.iso(fleetctl.utc_now() + dt.timedelta(hours=24)),
            'projected_used_percent_at_reset': 130}}}}}
        result = select(runtime)
        choice = result['choice']
        self.assertEqual(choice['model_key'], 'chatgpt:fixture-xhigh')
        self.assertEqual((choice['harness'], choice['level'], choice['worker_level']), ('pi', 'provider-default', 'xhigh'))
        self.assertIn('--mode', choice['command_argv'])
        self.assertIn('rw', choice['command_argv'])
        self.assertIsNone(choice['cost']['estimated_usd'])
        self.assertEqual(choice['cost']['basis'], 'no_known_limit')
        self.assertTrue({'coverage_incomplete', 'calibration_unmeasured', 'model_identity_unconfirmed'} <= set(choice['flags']))
        self.assertEqual(select(runtime, stakes='high')['choice']['model_key'], 'gpt-6.1-sol')
        runtime['model_preferences'] = {'chatgpt:fixture-xhigh': 'off', 'chatgpt:fixture-high': 'off'}
        self.assertEqual(select(runtime)['choice']['model_key'], 'gpt-6.1-sol')

    def test_pi_measurements_do_not_calibrate_native_chat_or_other_families(self):
        rows = {(row['model_key'], row['level']): row for row in self.measured()['rows']}
        for level in ('high', 'xhigh'):
            model = 'chatgpt:fixture-' + level
            self.assertEqual(rows[model, level]['own']['coding-agent']['trials'], 0)
            self.assertTrue(rows[model, level]['q']['coding-agent']['unknown'])
            self.assertGreater(rows[model, 'provider-default']['own']['coding-agent']['trials'], 0)
            self.assertTrue(rows[model, 'provider-default']['q']['review']['unknown'])
        self.server.blocked = True
        options, _ = selector.enumerate_options(self.expanded(), {}, 'implementation', fleet=fleetctl)
        self.assertFalse(any(option['harness'] == 'pi' for option in options))

    def test_coding_relay_refuses_review_in_selector_and_direct_runner(self):
        self.measured()
        options, rejected = selector.enumerate_options(self.expanded(), {}, 'review', fleet=fleetctl)
        self.assertFalse(any(option['harness'] == 'pi' for option in options))
        self.assertTrue(any('role not declared' in reason and ':pi' in reason for reason in rejected))
        run = subprocess.run([str(ROOT / 'scripts/pi-agent.sh'), 'run', '--lane', 'chatgpt:fixture-high:pi',
                              '--prompt', 'fixture review', '--dir', str(self.root), '--mode', 'ro',
                              '--effort-role', 'review'], capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertIn('role not declared', run.stderr)
        self.assertEqual(run.stdout, '')

    def test_generated_relay_runs_existing_pi_supervisor_with_local_write_tools(self):
        binary_dir = self.root / 'bin'
        binary_dir.mkdir()
        binary = binary_dir / 'pi'
        binary.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
agent = pathlib.Path(os.environ['PI_CODING_AGENT_DIR'])
config = json.loads((agent/'models.json').read_text())['providers']['crossfeed-chat']
assert config['apiKey'] == '${CROSSFEED_PROVIDER_KEY}'
assert config['models'][0]['id'] == 'chatgpt:fixture-high'
assert config['models'][0]['compat'] == {'sendSessionAffinityHeaders': True, 'sessionAffinityFormat': 'openai'}
assert '--tools' not in sys.argv
assert sys.argv[sys.argv.index('--thinking')+1] == 'off'
assert not (agent/'auth.json').exists()
sys.stdin.read()
pathlib.Path('tool-write.txt').write_text('local tools admitted')
message = {'role':'assistant','provider':'crossfeed-chat','model':'chatgpt:fixture-high','stopReason':'stop',
           'content':[{'type':'text','text':'PONG'}], 'usage':{'input':10,'output':3,'totalTokens':13,'cost':{'total':99}}}
print(json.dumps({'type':'message_end','message':message}))
print(json.dumps({'type':'agent_end','messages':[message]}))
''')
        binary.chmod(0o755)
        last = self.root / 'answer.txt'
        environment = dict(os.environ, PATH=str(binary_dir) + os.pathsep + os.environ['PATH'])
        run = subprocess.run([str(ROOT / 'scripts/pi-agent.sh'), 'run', '--lane', 'chatgpt:fixture-high:pi',
                              '--prompt', 'fixture task', '--dir', str(self.root), '--mode', 'rw',
                              '--effort-role', 'implementation', '--last', str(last)],
                             env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, 'PONG\n')
        self.assertEqual((self.root / 'tool-write.txt').read_text(), 'local tools admitted')
        receipt = json.loads(Path(str(last) + '.crossfeed.json').read_text())
        self.assertEqual(receipt['quota_pool'], 'chatgpt-work')
        self.assertEqual(receipt['effort'], 'provider-default')
        self.assertEqual(receipt['worker_level'], 'high')
        self.assertTrue(receipt['provider_identity_unconfirmed'])
        self.assertIsNone(receipt['actual_model'])
        self.assertEqual(receipt['identity_source'], 'unconfirmed')
        self.assertEqual(receipt['provider_reported_selector'], 'crossfeed-chat/chatgpt:fixture-high')
        self.assertIn('underlying model unconfirmed', run.stderr)
        self.assertNotIn('(provider reported)', run.stderr)
        self.assertNotIn('cost', receipt)
        self.assertNotIn('fixture-key', run.stdout + run.stderr + last.read_text())
        runtime = json.loads((self.state / 'runtime.json').read_text())
        self.assertEqual(runtime['leases'], [])


if __name__ == '__main__':
    unittest.main()
