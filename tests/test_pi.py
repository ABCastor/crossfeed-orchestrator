"""Synthetic Pi subprocess tests, no browser or provider traffic."""
from __future__ import annotations

import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

FAKE_PI = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(os.environ['FAKE_ROOT'])
mode = os.environ.get('FAKE_MODE', 'ok')
assert 'super-secret-test-key' not in ' '.join(sys.argv)
assert os.environ['PI_CODING_AGENT_DIR'] != os.environ.get('FAKE_AMBIENT')
assert not (pathlib.Path(os.environ['PI_CODING_AGENT_DIR']) / 'auth.json').exists()
assert 'ANTHROPIC_OAUTH_TOKEN' not in os.environ
assert 'ANTHROPIC_AUTH_TOKEN' not in os.environ
assert os.environ.get('GEMINI_API_KEY') == 'super-secret-test-key' or os.environ.get('FAKE_PROVIDER') != 'google'
(root/'args.json').write_text(json.dumps(sys.argv[1:]))
(root/'started').write_text(str(os.getpid()))
(root/'prompt').write_text(sys.stdin.read())
def emit(x): print(json.dumps(x), flush=True)
def message(text='  PONG  ', reason='stop', cost=.125, timestamp=10):
 return {'role':'assistant','provider':os.environ.get('FAKE_PROVIDER','google'),'model':'test-model',
         'timestamp': timestamp, 'stopReason':reason, 'content':[{'type':'thinking','thinking':'never final'},{'type':'text','text':text}],
         'usage':{'input':10,'output':3,'cacheRead':2,'cacheWrite':0,'totalTokens':15,
                  'cost':{'input':.1,'output':.025,'cacheRead':0,'cacheWrite':0,'total':cost}}}
if mode in ('stall','descendant','leader-exit'):
 if mode != 'stall':
  subprocess.Popen([sys.executable,'-c',"import pathlib,sys,signal,time; p=pathlib.Path(sys.argv[1]); p.write_text(str(__import__('os').getpid())); signal.signal(signal.SIGTERM,lambda *a:p.with_suffix('.term').write_text('TERM')); time.sleep(60)",str(root/'descendant')])
  for _ in range(100):
   if (root/'descendant').exists(): break
   time.sleep(.01)
 signal.signal(signal.SIGTERM,lambda *a:(root/'leader.term').write_text('TERM'))
 if mode=='leader-exit':
  m=message(); emit({'type':'message_end','message':m}); emit({'type':'agent_end','messages':[m]}); sys.exit(0)
 time.sleep(60)
if mode == 'silent-finish': time.sleep(2.2)
if mode == 'alive':
 for _ in range(8): emit({'type':'message_update','assistantMessageEvent':{'type':'thinking_delta','delta':'thinking'}}); time.sleep(.25)
if mode == 'secret':
 print('super-secret-',file=sys.stderr, end='',flush=True); time.sleep(.05); print('test-key',file=sys.stderr,flush=True)
if mode == 'exit': sys.exit(9)
if mode == 'bad-json': print('not json'); sys.exit(0)
if mode == 'missing': emit({'type':'agent_start'}); sys.exit(0)
m=message('' if mode=='empty' else 'super-secret-test-key' if mode=='secret' else '  PONG  ', 'error' if mode=='error' else 'toolUse' if mode=='tool-only' else 'stop')
if mode == 'tool-turn': emit({'type':'message_end','message':message('tool work','toolUse',.25,9)})
emit({'type':'message_end','message':m})
emit({'type':'agent_end','messages':[m], 'willRetry':mode=='retry'})
'''

FAKE_ROSTER = r'''#!/usr/bin/env python3
import json,os,sys
provider=os.environ.get('FAKE_PROVIDER','google')
args=sys.argv[1:]
if os.environ.get('FAKE_REJECT'): sys.exit(3)
if os.environ.get('FAKE_ROSTER_DEP'): sys.exit(127)
if args[0] in ('lookup','resolve-lane'): print('test-lane')
elif args[0]=='lane-json': print(json.dumps({'harness':os.environ.get('FAKE_HARNESS','pi'),'provider':provider,'selector':provider+'/test-model'}))
elif args[0]=='check-lane': pass
else: sys.exit(2)
'''
FAKE_FLEET = r'''#!/usr/bin/env python3
import json,os,pathlib,sys,time
root=pathlib.Path(os.environ['FAKE_ROOT'])
args=sys.argv[1:]
with (root/'lease.jsonl').open('a') as f: f.write(json.dumps({'command':args[0],'args':args,'at':time.time()})+'\n')
if args[0]=='acquire':
 if os.environ.get('FAKE_LEASE_FAIL'): sys.exit(1)
 print('test-token')
elif args[0]=='release':
 if os.environ.get('FAKE_RELEASE_PAUSE'):
  (root/'release-start').write_text('ready')
  time.sleep(.5)
 if os.environ.get('FAKE_RELEASE_FAIL'): sys.exit(1)
else: sys.exit(2)
'''


class PiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pi-test.')
        self.root = Path(self.tmp.name)
        self.scripts = self.root / 'scripts'
        self.bin = self.root / 'bin'
        self.scripts.mkdir()
        self.bin.mkdir()
        for name in ('pi-agent.sh', 'pi_runner.py', 'run_identity.py'):
            shutil.copy(ROOT / 'scripts' / name, self.scripts / name)
        for file, text in ((self.bin / 'pi', FAKE_PI), (self.scripts / 'roster.sh', FAKE_ROSTER), (self.scripts / 'fleetctl.py', FAKE_FLEET)):
            file.write_text(text)
            file.chmod(0o755)
        self.overlay = self.root / 'overlay.json'
        self.overlay.write_text(json.dumps({'lanes':[{'lane_id':'test-lane','harness':'pi','provider':'google','selector':'google/test-model','model_key':'test-model','quota_pool':'gemini-metered'}]}))
        self.env = os.environ.copy()
        for name in list(self.env):
            if name.endswith(('API_KEY', 'KEY_FILE')) or 'OAUTH' in name:
                self.env.pop(name, None)
        self.env.update(PATH=str(self.bin)+os.pathsep+self.env['PATH'], FAKE_ROOT=str(self.root),
                        GEMINI_API_KEY='super-secret-test-key', ACCESS_OVERLAY=str(self.overlay),
                        FLEET_STATE_DIR=str(self.root/'state'), PI_CODING_AGENT_DIR=str(self.root/'ambient'),
                        FAKE_AMBIENT=str(self.root/'ambient'), ANTHROPIC_OAUTH_TOKEN='ambient-forbidden-login',
                        ANTHROPIC_AUTH_TOKEN='ambient-forbidden-bearer')
        self.wrapper = str(self.scripts/'pi-agent.sh')
        self.last = self.root/'last'
        self.events = self.root/'events.jsonl'

    def tearDown(self):
        self.tmp.cleanup()  # Test scratch only, never user-owned files.

    def run_pi(self, *args, mode='ok', env=None):
        context = dict(self.env, FAKE_MODE=mode)
        context.update(env or {})
        return subprocess.run([self.wrapper, '--lane', 'test-lane', '--prompt', 'task', '--dir', str(self.root),
                               '--events', str(self.events), '--last', str(self.last), *args],
                              env=context, capture_output=True, text=True, timeout=12)

    def ledger(self):
        return [json.loads(line) for line in (self.root/'state'/'runs.jsonl').read_text().splitlines()]

    def lease(self):
        return [json.loads(line) for line in (self.root/'lease.jsonl').read_text().splitlines()]

    def test_final_text_receipt_native_usage_and_isolated_auth(self):
        result = self.run_pi()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'PONG\n')
        self.assertEqual(self.last.read_text(), 'PONG\n')
        self.assertIn('Crossfeed model receipt:', result.stderr)
        receipt = json.loads(Path(str(self.last)+'.crossfeed.json').read_text())
        self.assertEqual(receipt['actual_model'], 'google/test-model')
        self.assertEqual(receipt['quota_pool'], 'gemini-metered')
        self.assertEqual(receipt['tokens']['input'], 10)
        self.assertEqual(receipt['tokens']['total'], 15)
        self.assertEqual(receipt['cost']['estimated_usd'], .125)
        self.assertEqual(len(self.ledger()), 1)
        argv = json.loads((self.root/'args.json').read_text())
        for flag in ('-p','--no-session','--no-extensions','--no-skills','--no-prompt-templates','--no-approve'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--tools')+1], 'read,grep,find,ls')
        self.assertEqual(argv[argv.index('--mode')+1], 'json')
        self.assertIn('Crossfeed selected model:', (self.root/'prompt').read_text())
        self.assertEqual([record['command'] for record in self.lease()], ['acquire','release'])

    def test_prompt_file_model_selection_and_write_mode(self):
        prompt = self.root/'input'
        prompt.write_text('from-file')
        result = subprocess.run([self.wrapper,'run','--model','google/test-model','--prompt-file',str(prompt),'--dir',str(self.root),'--mode','rw'], env=self.env, capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('from-file', (self.root/'prompt').read_text())
        self.assertNotIn('--tools', json.loads((self.root/'args.json').read_text()))

    def test_effort_mapping(self):
        for effort, thinking in (('off','off'),('minimal','minimal'),('low','low'),('medium','medium'),('high','high'),('xhigh','xhigh'),('max','xhigh')):
            with self.subTest(effort=effort):
                result = self.run_pi('--effort', effort)
                self.assertEqual(result.returncode, 0, result.stderr)
                argv=json.loads((self.root/'args.json').read_text())
                self.assertEqual(argv[argv.index('--thinking')+1], thinking)
                self.assertIn('-> thinking '+thinking, result.stderr)

    def test_forbidden_login_providers_and_missing_explicit_keys(self):
        for provider in ('openai-codex','google-gemini-cli','gemini-cli','google-vertex','anthropic'):
            with self.subTest(provider=provider):
                result=self.run_pi(env={'FAKE_PROVIDER':provider})
                self.assertEqual(result.returncode,5,result.stderr)
                self.assertEqual(result.stdout,'')
                self.assertFalse((self.root/'started').exists())
                self.assertFalse((self.root/'lease.jsonl').exists())
        result=self.run_pi(env={'GEMINI_API_KEY':''})
        self.assertEqual(result.returncode,5)

    def test_anthropic_subscription_token_in_key_variable_is_denied(self):
        result=self.run_pi(env={'FAKE_PROVIDER':'anthropic','ANTHROPIC_API_KEY':'sk-ant-oat-forbidden'})
        self.assertEqual(result.returncode,5)
        self.assertNotIn('sk-ant-oat-forbidden',result.stderr)

    def test_api_keys_and_named_client_plan_keys_are_allowed(self):
        for provider, variable in (('anthropic','ANTHROPIC_API_KEY'),('openai','OPENAI_API_KEY'),('zai','ZAI_API_KEY'),('minimax','MINIMAX_API_KEY'),('opencode-go','OPENCODE_API_KEY')):
            with self.subTest(provider=provider):
                result=self.run_pi(env={'FAKE_PROVIDER':provider,variable:'sk-cp-test-key'})
                self.assertEqual(result.returncode,0,result.stderr)

    def test_google_key_file_does_not_leak(self):
        key_file=self.root/'key'
        key_file.write_text('super-secret-test-key\n')
        result=self.run_pi(env={'GEMINI_API_KEY':'','GEMINI_API_KEY_FILE':str(key_file)})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('super-secret-test-key',result.stdout+result.stderr+self.events.read_text())
        self.assertNotIn('super-secret-test-key',(self.root/'args.json').read_text())

    def test_split_stderr_and_event_secret_is_redacted(self):
        result=self.run_pi(mode='secret')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('super-secret-test-key',result.stdout+result.stderr+self.events.read_text()+self.last.read_text())
        self.assertEqual(result.stdout,'[REDACTED]\n')

    def test_frozen_failure_codes_and_no_partial_stdout(self):
        for mode, expected in (('exit',5),('error',5),('bad-json',5),('missing',6),('retry',6),('tool-only',6),('empty',7)):
            with self.subTest(mode=mode):
                result=self.run_pi(mode=mode)
                self.assertEqual(result.returncode,expected,result.stderr)
                self.assertEqual(result.stdout,'')
                self.assertEqual(self.lease()[-1]['command'],'release')

    def test_roster_modality_usage_and_lease_refusals(self):
        for argv,env,expected in (([],{'FAKE_REJECT':'1'},3),([],{'FAKE_HARNESS':'codex'},3),(['--modality','image'],{},3),(['--effort','invalid'],{},2),(['--wall','-1'],{},2),([],{'FAKE_LEASE_FAIL':'1'},4)):
            with self.subTest(argv=argv,env=env):
                result=self.run_pi(*argv,env=env)
                self.assertEqual(result.returncode,expected,result.stderr)
                self.assertEqual(result.stdout,'')
                self.assertFalse((self.root/'started').exists())

    def test_missing_pi_dependency(self):
        (self.bin/'pi').unlink()
        # Hide installed pi, retain shell/python so the launcher still starts.
        for binary in ('bash','python3'):
            (self.bin/binary).symlink_to(shutil.which(binary))
        result=self.run_pi(env={'PATH':str(self.bin)})
        self.assertEqual(result.returncode,127,result.stderr)

    def test_missing_roster_dependency(self):
        result=self.run_pi(env={'FAKE_ROSTER_DEP':'1'})
        self.assertEqual(result.returncode,127,result.stderr)

    def test_default_silence_reports_without_killing(self):
        result = self.run_pi(mode='silent-finish', env={'CROSSFEED_TEST_SILENCE_INTERVAL_S':'1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('silent for ', result.stderr)
        self.assertNotIn('LIMIT FIRED', result.stderr)
        self.assertEqual(result.stdout, 'PONG\n')

    def test_watchdog_idle_and_wall(self):
        for flag,code in (('--idle',125),('--wall',124)):
            with self.subTest(flag=flag):
                result=self.run_pi(flag,'1','--kill-after','0',mode='stall')
                self.assertEqual(result.returncode,code,result.stderr)
                self.assertEqual(result.stdout,'')
                self.assertIn('sending KILL',result.stderr)
                self.assertEqual(self.lease()[-1]['command'],'release')

    def test_live_thinking_events_prevent_false_idle_kill(self):
        result=self.run_pi('--idle','1',mode='alive')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'PONG\n')

    def test_term_kill_descendants_and_lease_outlive_group(self):
        for mode in ('descendant','leader-exit'):
            with self.subTest(mode=mode):
                result=self.run_pi('--wall','1','--idle','0','--kill-after','1',mode=mode)
                self.assertEqual(result.returncode,124,result.stderr)
                self.assertTrue((self.root/'descendant.term').exists())
                self.assertIn('sending KILL',result.stderr)
                pid=int((self.root/'descendant').read_text())
                with self.assertRaises(ProcessLookupError): os.kill(pid,0)
                lease=self.lease()
                self.assertGreaterEqual(lease[-1]['at']-lease[-2]['at'],2)
                self.assertEqual(lease[-2]['args'][-1],'62')

    def test_unbounded_lease_does_not_expire_at_idle_limit(self):
        result=self.run_pi('--idle','1')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.lease()[0]['args'][-1],'2147483647')

    def test_output_write_failure_receipt_records_failure(self):
        self.last.mkdir()
        result=self.run_pi()
        self.assertEqual(result.returncode,5,result.stderr)
        self.assertEqual(result.stdout,'')
        self.assertEqual(self.ledger()[0]['returncode'],5)
        self.assertEqual(self.lease()[-1]['command'],'release')

    def test_banned_provider_refused_before_roster_lookup(self):
        result=subprocess.run([self.wrapper,'--model','openai-codex/test-model','--prompt','task'],
                              env=dict(self.env,FAKE_REJECT='1'),capture_output=True,text=True)
        self.assertEqual(result.returncode,5,result.stderr)
        self.assertIn('forbidden',result.stderr)

    def test_max_retains_canonical_selection_and_native_effort(self):
        selection=self.root/'selection.json'
        selection.write_text(json.dumps({'schema':'fleet-selection/v1','choice':{'model_key':'test-model','level':'max',
                                        'lane_id':'test-lane','pool':'gemini-metered','harness':'pi'}}))
        result=self.run_pi('--effort','max',env={'FLEET_SELECTION_FILE':str(selection)})
        self.assertEqual(result.returncode,0,result.stderr)
        record=self.ledger()[0]
        self.assertEqual(record['effort'],'max')
        self.assertEqual(record['native_effort'],'xhigh')
        self.assertIsNotNone(record['selection'])

    def test_pipe_setup_failure_cleans_up_child_group(self):
        spec=importlib.util.spec_from_file_location('pi_test_runner',ROOT/'scripts'/'pi_runner.py')
        runner=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        args=SimpleNamespace(dir=self.root,prompt='task',kill_after=0,idle=0,wall=0)
        real_popen, real_killpg = subprocess.Popen, os.killpg
        for failure_sig in (None, 0, signal.SIGTERM, signal.SIGKILL):
            with self.subTest(failure_sig=failure_sig):
                children=[]
                def start(*a,**kw):
                    child=real_popen(*a,**kw)
                    children.append(child)
                    # A pipe handshake proves readiness without polling or sleeps.
                    self.assertEqual(child.stdout.readline(),b'ready\n')
                    return child
                def killpg(pid,sig):
                    if failure_sig is not None and sig == failure_sig:
                        real_killpg(pid,signal.SIGKILL)
                        children[0].wait(timeout=5)
                        raise PermissionError(errno.EPERM,'synthetic exited-group denial')
                    # Hold TERM until KILL so no exit race precedes the injected error.
                    if failure_sig == signal.SIGKILL and sig == signal.SIGTERM:
                        return
                    return real_killpg(pid,sig)
                def fail_register(*args, **kwargs):
                    raise OSError('synthetic pipe setup failure')
                broken_poller=SimpleNamespace(register=fail_register,close=lambda:None)
                command=[sys.executable,'-c',"import signal; print('ready',flush=True); signal.pause()"]
                try:
                    with patch.object(runner.subprocess,'Popen',side_effect=start), patch.object(runner.selectors,'DefaultSelector',return_value=broken_poller), patch.object(runner.os,'killpg',side_effect=killpg):
                        with self.assertRaisesRegex(OSError,'synthetic pipe setup failure'):
                            runner.supervise(args,command,self.env,io.StringIO(),'super-secret-test-key',[])
                    self.assertIsNotNone(children[0].returncode)
                    with self.assertRaises(ProcessLookupError): real_killpg(children[0].pid,0)
                finally:
                    for child in children:
                        if child.poll() is None:
                            real_killpg(child.pid,signal.SIGKILL)
                            child.wait(timeout=5)
                        for stream in (child.stdin,child.stdout,child.stderr):
                            stream.close()

    def test_group_signal_only_swallows_missing_or_exited_permission_errors(self):
        spec=importlib.util.spec_from_file_location('pi_test_runner',ROOT/'scripts'/'pi_runner.py')
        runner=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        for sig in (0,signal.SIGTERM,signal.SIGKILL):
            for status in (None,0):
                for code in (errno.ESRCH,errno.EPERM,errno.EINVAL):
                    with self.subTest(sig=sig,status=status,errno=code):
                        def wait(timeout):
                            if status is None:
                                raise subprocess.TimeoutExpired('synthetic child',timeout)
                            return status
                        child=SimpleNamespace(pid=123,poll=lambda:status,wait=wait)
                        error=OSError(code,'synthetic signal error')
                        with patch.object(runner.os,'killpg',side_effect=error):
                            if code == errno.ESRCH or (code == errno.EPERM and status is not None):
                                self.assertFalse(runner.signal_group(child,sig))
                            else:
                                with self.assertRaises(OSError) as raised:
                                    runner.signal_group(child,sig)
                                self.assertIs(raised.exception,error)

    def test_external_interrupt_cleans_group_records_and_releases(self):
        child=subprocess.Popen([self.wrapper,'--lane','test-lane','--prompt','task','--idle','0','--kill-after','0'],
                               env=dict(self.env,FAKE_MODE='stall'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        for _ in range(200):
            if (self.root/'started').exists(): break
            time.sleep(.01)
        self.assertTrue((self.root/'started').exists())
        child.send_signal(signal.SIGINT)
        output,error=child.communicate(timeout=10)
        self.assertEqual(child.returncode,130,error)
        self.assertEqual(output,'')
        self.assertEqual(self.ledger()[0]['returncode'],130)
        self.assertEqual(self.lease()[-1]['command'],'release')
        with self.assertRaises(ProcessLookupError): os.kill(int((self.root/'started').read_text()),0)

    def test_release_failure_preserves_completed_receipt(self):
        result=self.run_pi(env={'FAKE_RELEASE_FAIL':'1'})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'PONG\n')
        self.assertEqual(self.ledger()[0]['returncode'],0)
        self.assertIn('WARNING quota lease release failed',result.stderr)

    def test_signal_during_accounting_preserves_completed_receipt(self):
        child=subprocess.Popen([self.wrapper,'--lane','test-lane','--prompt','task'],
                               env=dict(self.env,FAKE_RELEASE_PAUSE='1'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        for _ in range(200):
            if (self.root/'release-start').exists(): break
            time.sleep(.01)
        self.assertTrue((self.root/'release-start').exists())
        child.send_signal(signal.SIGINT)
        output,error=child.communicate(timeout=10)
        self.assertEqual(child.returncode,0,error)
        self.assertEqual(output,'PONG\n')
        self.assertEqual(self.ledger()[0]['returncode'],0)
        self.assertIn('preserving the recorded completed outcome',error)

    def test_usage_all_tool_turns_and_real_daily_cap_reader(self):
        result=self.run_pi(mode='tool-turn')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.ledger()[0]['cost']['estimated_usd'],.375)
        spec=importlib.util.spec_from_file_location('pi_test_fleet',ROOT/'scripts'/'fleetctl.py')
        fleet=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fleet)
        roster={'lanes':[{'lane_id':'test-lane','quota_pool':'gemini-metered','model_key':'test-model','max_parallel':1}],
                'quota_pools':{'gemini-metered':{'daily_usd_cap':.2}}}
        spent=fleet.pool_spent_since(self.root/'state',roster,'gemini-metered',fleet.local_day_start())
        self.assertEqual(spent,.375)
        with self.assertRaisesRegex(fleet.FleetError,'daily spend cap'):
            fleet.acquire_lease(self.root/'state',roster,'test-lane',60)

    def test_example_lane_uses_capped_metered_pool(self):
        example=json.loads((ROOT/'examples'/'access-overlay.example.json').read_text())
        lanes=[lane for lane in example['lanes'] if lane['harness']=='pi']
        self.assertTrue(lanes)
        self.assertEqual(lanes[0]['quota_pool'],'gemini-metered')
        self.assertEqual(lanes[0]['max_parallel'],1)
        self.assertGreater(example['quota_pools']['gemini-metered']['daily_usd_cap'],0)


if __name__=='__main__':
    unittest.main()
