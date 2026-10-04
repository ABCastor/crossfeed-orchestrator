"""Saved labels and wakes use fake gateway data, Gaddi, and logical time."""
import argparse
import copy
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import console
import fleetctl
import chatgpt_catalog as catalog
import chatgpt_transport as transport
import chatgpt_workers as workers
import selector

FAKE_GADDI = r'''#!/usr/bin/env python3
import json,pathlib,re,sys,time
p=pathlib.Path(__file__).with_name('browser.json')
d=json.loads(p.read_text()); args=sys.argv[2:]; d['calls'].append(args)
action=args[0]; result={}
stall=d.get('stall',{})
stalled=(action==stall.get('action') and stall.get('argument') in args[2:] and stall.get('remaining',0)>0)
if stalled: stall['remaining']-=1
def fail():
 p.write_text(json.dumps(d)); print(json.dumps({'ok':False,'error':{'code':stall.get('code'),'message':'Chrome did not respond'}})); sys.exit(1)
if stalled and not stall.get('after'): fail()
if action=='click' and d.get('click_error'):
 p.write_text(json.dumps(d)); print(json.dumps({'ok':False,'error':{'message':'Chrome did not respond'}})); sys.exit(1)
if action=='open':
 if d.get('open_error'):
  p.write_text(json.dumps(d)); print(json.dumps({'ok':False,'error':{'message':d['open_error']}})); sys.exit(1)
 d['observed']['url']=args[1]; d['sent']=False; d['opens']=d.get('opens',0)+1; result={'id': '42'}
elif action=='eval':
 script=args[2]
 if d.get('rate_limit') and (not d.get('rate_after_send') or d.get('sent')) and 'crossfeedRateLimited' in script:
  result={'value':{'crossfeedRateLimited':True}}
 elif "fetch('/api/auth/session')" in script:
  chat_id=json.loads(re.search(r"conversation/' \+ (.+),",script).group(1))
  d.setdefault('archived',[]).append(chat_id)
  result={'value':d.get('archive_status',200)}
 elif 'location.origin + location.pathname' in script and 'return {pill:' not in script:
  d['location_reads']=d.get('location_reads',0)+1
  if d['location_reads']<=d.get('location_errors',0):
   p.write_text(json.dumps(d)); print(json.dumps({'ok':False,'error':{'message':'evaluation failed'}})); sys.exit(1)
  result={'value':'https://chatgpt.com/' if d['location_reads']<=d.get('location_home_reads',0) else d['observed']['url']}
 elif 'return {row:' in script:
  target=json.loads(re.search(r'const target = (.+);',script).group(1)); d['desired_row']=target['row']
  pos=d.get('picker_position',1); name=['Instant','Medium','High','Extra High','Pro'][pos]
  result={'value':d.get('selection',{'row':d.get('picker_row',target['row']),'position':pos,'minimum':'0','maximum':'4','status':name+', '+str(pos+1)+' of 5.','targetVisible':d.get('target_visible',True)}) if d.get('menu') else None}
 elif 'return {pill:' in script:
  d['pill_reads']=d.get('pill_reads',0)+1
  if d.get('pill_errors')==-1 or d['pill_reads']<=d.get('pill_errors',0):
   p.write_text(json.dumps(d)); print(json.dumps({'ok':False,'error':{'message':'Chrome did not respond'}})); sys.exit(1)
  sequence=d.get('observations',[])
  result={'value':sequence.pop(0) if sequence else d['observed']}
 elif 'return {ready:' in script:
  ready=not d.get('wait_for_show') or d.get('shown',False)
  sequence=d.get('ready_sequence',[])
  if sequence: ready=sequence.pop(0)
  d.setdefault('ready_reads',[]).append({'ready':ready,'at':time.monotonic()})
  result={'value':{'ready':ready,'picker':ready,'composer':ready}}
 elif 'return {text:' in script:
  result={'value':{'text':d.get('composer',''),'chip':d.get('chip',False)}}
 elif "startsWith('crossfeed')" in script:
  d['mention_reads']=d.get('mention_reads',0)+1
  result={'value':d.get('mention_popup',False) and d['mention_reads']>d.get('mention_hidden_reads',0)}
 elif "=== 'Always allow'" in script:
  d['allow_checks']=d.get('allow_checks',0)+1
  if d.get('allow_read_error') and d['allow_checks']==1:
   d['recent']=True;d['polling']=True
   p.write_text(json.dumps(d)); print(json.dumps({'ok':False,'error':{'code':d.get('allow_read_code'),'message':d['allow_read_error']}})); sys.exit(1)
  result={'value':d.get('allow',False) and d['allow_checks']>=d.get('allow_after_checks',1)}
 else: result={'value':True}
elif action=='press' and args[-1] in ('ArrowLeft','ArrowRight'):
 d['picker_position']=d.get('picker_position',1)+(1 if args[-1]=='ArrowRight' else -1)
 d['observed']['pill']=['Instant','Medium','High','Extra High','Pro'][d['picker_position']]
elif action=='press' and args[-1]=='ArrowDown': d['menu']=True
elif action=='press' and args[-1]=='Escape': d['menu']=False
elif action=='click' and 'data-model-picker-view-toggle' in args[-1]: d['target_visible']=True
elif action=='click' and '=\"row\"' in args[-1]: d['picker_row']=d['desired_row'];d['menu']=False
elif action=='click' and '=\"chat\"' in args[-1]: d['observed']['chat']=True
elif action=='type' and args[3]=='@crossfeed': d['composer']='' if d.get('popup_only') else args[3];d['mention_popup']=True
elif action=='click' and '=\"mention\"' in args[-1]: d['chip']=True;d['composer']='crossfeed';d['mention_popup']=False
elif action=='type' and args[-1]=='append':
 d['label']=args[3].split(' wake up as ',1)[1].split('.',1)[0];d['composer']=d.get('composer','')+args[3]
elif action=='press' and args[-1]=='Enter':
 d['sent']=True; d['observed']['url']='https://chatgpt.com/c/'+d.get('new_id','fresh-worker'+('-'+str(d['opens']) if d['opens']>1 else ''))
 if not d.get('allow'): d['recent']=True; d['polling']=True; d.setdefault('active_labels',[]).append(d['label'])
 if d.get('enter_error'):
  p.write_text(json.dumps(d)); print(json.dumps({'ok':False})); sys.exit(1)
elif action=='click' and '="allow"' in args[-1]: d['recent']=True;d['polling']=True;d['allow']=False
elif action=='show': d['shown']=True
elif action=='close':
 if d.get('close_error'):
  p.write_text(json.dumps(d)); print(json.dumps({'ok':False})); sys.exit(1)
 result=d.get('close_result',{'closed':[42],'failed':[]})
if stalled: fail()
p.write_text(json.dumps(d)); print(json.dumps({'result':result}))
'''


class FakeClock:
    """Advance logical wake time while subprocess fixtures run without real waits."""
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return time.time()  # Retain the existing wall-clock cooldown fixtures.

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        time.sleep(.01)  # Yield to competing lock holders, never wait the wake delay.


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.ready_times = []
        evaluate = workers.Gaddi.evaluate
        def timed_read(browser, tab, script, **kwargs):
            value = evaluate(browser, tab, script, **kwargs)
            if script == workers.READY:
                self.ready_times.append(browser.clock.monotonic())
            return value
        reader = patch.object(workers.Gaddi, "evaluate", new=timed_read)
        reader.start()
        self.addCleanup(reader.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = json.loads((ROOT / 'examples/access-overlay.example.json').read_text())
        self.lane = self.roster['chatgpt_gateway']['lane_template']
        self.lane['auth']['key_file'] = str(self.root / 'key')
        (self.root / 'key').write_text('fixture-key')
        self.label = 'arbitrary-family-medium'
        self.url = 'https://chatgpt.com/c/saved-worker'
        self.map = {self.label: {'row': 'Latest', 'position': 1, 'url': self.url},
                    '_note': 'Each wake starts a fresh chat; url is the last successful chat, optional before first wake.',
                    'other-pro': {'row': 'Another row', 'position': 4, 'url': 'https://chatgpt.com/c/other'}}
        self.write_state()
        self.gaddi = self.root / 'gaddi'
        self.gaddi.write_text(FAKE_GADDI)
        self.gaddi.chmod(0o700)
        run = subprocess.run
        fixture = compile(FAKE_GADDI.replace('import json,pathlib,re,sys,time',
                                            'import json,pathlib,re,time'), str(self.gaddi), 'exec')
        def fake_run(args, **kwargs):
            if args[0] != str(self.gaddi):
                return run(args, **kwargs)
            self.assertEqual(args[1], '--json')
            output = []
            def exit_fixture(code=0):
                raise SystemExit(code)
            from types import SimpleNamespace
            namespace = {'__file__': str(self.gaddi), 'sys': SimpleNamespace(argv=args, exit=exit_fixture),
                         'print': lambda value: output.append(str(value))}
            code = 0
            try:
                exec(fixture, namespace)
            except SystemExit as exc:
                code = exc.code
            return subprocess.CompletedProcess(args, code, '\n'.join(output), '')
        process = patch.object(workers.subprocess, 'run', side_effect=fake_run)
        process.start()
        self.addCleanup(process.stop)
        self.lane['gaddi_cli'] = str(self.gaddi)
        self.lane['selector'] = 'chatgpt:' + self.label
        self.lane['worker_label'] = self.label
        self.browser = {'calls': [], 'recent': False, 'allow': False,
                        'observed': {'chat': True, 'pill': 'Medium', 'url': self.url}}
        self.write_browser()
        self.worker_request = patch.object(workers, 'request', side_effect=self.gateway)
        self.worker_request.start()
        self.addCleanup(self.worker_request.stop)


    def write_state(self):
        chats = {label: {key: value for key, value in row.items() if key in {'url', 'custom'}}
                 for label, row in self.map.items() if not label.startswith('_') and 'url' in row}
        (self.root / 'wake-state.json').write_text(json.dumps({
            'chats': chats, '_note': self.map.get('_note'), '_archive_pending': [],
            'wakes': [], 'failed': {}, 'cooldown_until': 0}))

    def write_browser(self):
        (self.root / 'browser.json').write_text(json.dumps(self.browser))

    def read_browser(self):
        return json.loads((self.root / 'browser.json').read_text())

    def gateway(self, base, key, path, **kwargs):
        if path == '/models':
            return {'object': 'list', 'data': [
                {'object': 'model', 'id': 'chatgpt:' + label, 'saved': True,
                 'row': row['row'], 'level': row['position'], **({'older': row['older']} if 'older' in row else {})}
                for label, row in self.map.items() if not label.startswith('_')]
                + [{'object': 'model', 'id': 'chatgpt:unmapped-high', 'saved': False}]}
        return {'workers': [{'label': self.label, 'contact': 'recent' if self.read_browser()['recent'] else 'stale',
                             'polling': self.read_browser().get('polling', self.read_browser()['recent']), 'processing_claim': False},
                            {'label': 'other-pro', 'contact': 'recent', 'polling': False, 'processing_claim': False}]}

    def reset_limits(self):
        path = self.root / 'wake-state.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        state.update(wakes=[], failed={}, cooldown_until=0, browser_backoff_until=0)
        path.write_text(json.dumps(state))
        self.browser.update(recent=False, polling=False)

    def wake(self, timeout=10):
        with patch.object(workers, 'request', side_effect=self.gateway):
            workers.wake(self.lane, self.label, timeout=timeout, clock=self.clock)

    def expand(self):
        with patch.object(catalog, 'request', side_effect=self.gateway):
            return catalog.expand(self.roster, self.root / 'state')

    def log_rows(self):
        return [json.loads(line) for line in workers.wake_log_file(self.lane).read_text().splitlines()]

    def test_wake_log_success_and_already_active_call_record_once_each(self):
        self.wake()
        self.wake()
        rows = self.log_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['outcome'] for row in rows], ['ok', 'ok'])
        self.assertEqual(rows[0]['stage'], 'previous conversation archive')
        self.assertEqual(rows[1]['stage'], 'saved worker lookup')
        self.assertEqual(rows[0]['seconds'], 1.5)
        self.assertEqual(set(rows[0]), {'ts', 'label', 'outcome', 'seconds', 'stage', 'message'})
        self.assertEqual(workers.wake_log_file(self.lane).stat().st_mode & 0o777, 0o600)

    def test_wake_log_preserves_failure_stage_after_successful_cleanup(self):
        self.browser['pill_errors'] = -1
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'unresponsive'):
            self.wake(timeout=6)
        row, = self.log_rows()
        self.assertEqual((row['outcome'], row['stage'], row['message']),
                         ('failed', 'Chat mode selection', 'Gaddi unresponsive'))
        self.assertEqual(row['seconds'], 6)

    def test_wake_log_specific_selection_reason_is_retained_safely(self):
        self.browser['selection'] = {'row': 'Wrong row', 'targetVisible': True}
        self.write_browser()
        with self.assertRaises(transport.Rejected):
            self.wake()
        row, = self.log_rows()
        self.assertEqual(row['stage'], 'saved model and level selection')
        self.assertEqual(row['message'], 'saved model row was not confirmed')

    def test_wake_log_cleanup_failure_is_failed_even_after_registration(self):
        self.browser['close_error'] = True
        self.write_browser()
        with self.assertRaises(transport.Rejected):
            self.wake()
        row, = self.log_rows()
        self.assertEqual((row['outcome'], row['stage']), ('failed', 'opened worker tab cleanup'))

    def test_wake_log_admission_refusals_and_override_location(self):
        path = self.root / 'elsewhere' / 'custom-state.json'
        self.lane['wake_state_file'] = str(path)
        self.lane['wake_daily_cap'] = 0
        with self.assertRaisesRegex(transport.Rejected, 'daily cap'):
            self.wake()
        self.assertEqual(workers.wake_log_file(self.lane), path.parent / 'wake-log.jsonl')
        row, = self.log_rows()
        self.assertEqual((row['stage'], row['message']), ('wake admission', 'worker wake daily cap reached (rolling 24 hours)'))
        self.assertEqual(self.read_browser()['calls'], [])

    def test_wake_log_redacts_exception_text_and_invalid_label(self):
        with patch.object(workers, 'load_workers', side_effect=transport.Rejected(6, 'fixture-sensitive-token page text')):
            with self.assertRaises(transport.Rejected):
                workers.wake(self.lane, 'fixture-sensitive-token\npage text', clock=self.clock)
        raw = workers.wake_log_file(self.lane).read_text()
        self.assertNotIn('fixture-sensitive-token', raw)
        self.assertNotIn('page text', raw)
        row, = self.log_rows()
        self.assertEqual((row['label'], row['message']), ('invalid-label', 'wake rejected'))

    def test_wake_log_unexpected_failure_records_stage_before_cleanup(self):
        with patch.object(workers, 'select_level', side_effect=RuntimeError('fixture-sensitive-token')):
            with self.assertRaises(RuntimeError):
                self.wake()
        row, = self.log_rows()
        self.assertEqual((row['stage'], row['message']),
                         ('saved model and level selection', 'unexpected error'))
        self.assertEqual(self.read_browser()['calls'][-1], ['close', '42'])

    def test_wake_log_failure_cannot_mask_original_failure_or_success(self):
        with patch.object(workers, '_log_wake', side_effect=OSError('fixture-sensitive-token')), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            self.wake()
            with self.assertRaisesRegex(transport.Rejected, 'no saved worker'):
                workers.wake(self.lane, 'missing', clock=self.clock)
        self.assertEqual(output.getvalue(), 'wake log: could not record attempt\n' * 2)

    def test_wake_log_retains_last_500_and_concurrent_appends(self):
        for index in range(505):
            workers._log_wake(self.lane, {'index': index})
        rows = self.log_rows()
        self.assertEqual(len(rows), 500)
        self.assertEqual((rows[0]['index'], rows[-1]['index']), (5, 504))
        errors = []
        def append(index):
            try:
                workers._log_wake(self.lane, {'index': index})
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=append, args=(i,)) for i in range(505, 525)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        rows = self.log_rows()
        self.assertEqual(len(rows), 500)
        self.assertEqual({row['index'] for row in rows[-20:]}, set(range(505, 525)))

    def test_doctor_log_counts_rolling_window_and_never_echoes_untrusted_stage(self):
        now = time.time()
        for ts, outcome, stage in ((now - 86401, 'failed', 'wake admission'),
                                   (now - 86400, 'failed', 'wake admission'),
                                   (now - 5, 'ok', 'previous conversation archive'),
                                   (now - 4, 'failed', 'worker registration'),
                                   (now - 3, 'failed', 'worker registration'),
                                   (now - 2, 'failed', 'wake admission'),
                                   (now + 1, 'ok', 'saved worker lookup')):
            workers._log_wake(self.lane, {'ts': ts, 'outcome': outcome, 'stage': stage})
        with patch.object(self.clock, 'time', return_value=now):
            summary = workers.wake_log_status(self.lane, clock=self.clock)
        self.assertIn('4 attempts, 1 successes, 3 failures', summary)
        self.assertIn('wake admission=1, worker registration=2', summary)
        line, _ = catalog.doctor(self.roster['chatgpt_gateway'], {'chatgpt:test': 'sleeping'})
        self.assertIn('wake log (last 24 h, retained)', line)
        workers._log_wake(self.lane, {'ts': now, 'outcome': 'failed', 'stage': 'fixture-sensitive-token'})
        self.assertEqual(workers.wake_log_status(self.lane), 'wake log: unavailable')

    def test_doctor_missing_log_is_empty_and_corrupt_log_is_unavailable(self):
        self.assertIn('0 attempts, 0 successes, 0 failures', workers.wake_log_status(self.lane))
        workers.wake_log_file(self.lane).write_text('not json\n')
        self.assertEqual(workers.wake_log_status(self.lane), 'wake log: unavailable')

    def test_pause_default_clock_preserves_sleep_and_deadline(self):
        with patch.object(workers.time, 'monotonic', return_value=10), \
             patch.object(workers.time, 'sleep') as sleep:
            workers._pause(12)
            sleep.assert_called_once_with(2)
            with self.assertRaisesRegex(transport.Rejected, 'timed out'):
                workers._pause(10)

    def test_saved_settings_come_only_from_api_and_exclude_unsaved_labels(self):
        saved = workers.load_workers(self.lane)
        self.assertEqual(saved[self.label], {'row': 'Latest', 'position': 1, 'level': 'medium'})
        self.assertNotIn('unmapped-high', saved)
        self.assertNotIn('url', saved[self.label])

    def test_wake_state_override_controls_all_volume_guards(self):
        self.assertEqual(workers.wake_state_file(self.lane), self.root / 'wake-state.json')
        alternative = self.root / 'isolated-state.json'
        alternative.write_text(json.dumps({'wakes': [time.time()] * 30}))
        self.lane['wake_state_file'] = str(alternative)
        default = (self.root / 'wake-state.json').read_text()
        with self.assertRaisesRegex(transport.Rejected, 'daily cap'):
            self.wake()
        self.assertEqual(workers.wake_state_file(self.lane), alternative)
        self.assertEqual((self.root / 'wake-state.json').read_text(), default)
        self.assertEqual(self.read_browser()['calls'], [])

    def test_labels_levels_and_sleeping_are_routable_without_model_names_in_code(self):
        roster = self.expand()
        lane = fleetctl.lane_map(roster)['chatgpt:' + self.label]
        self.assertEqual((lane['quota_pool'], lane['max_parallel'], lane['catalog_state']), ('chatgpt-work', 1, 'sleeping'))
        self.assertEqual(roster['model_cards'][lane['lane_id']]['name'], 'Latest / medium')
        self.assertNotIn('chatgpt:unmapped-high', fleetctl.lane_map(roster))
        runtime = {'leases': [], 'quota_snapshots': {}}
        options, _ = selector.enumerate_options(roster, runtime, 'lookup', fleet=fleetctl)
        option = next(o for o in options if o.get('lane_id') == lane['lane_id'])
        self.assertEqual(option['level'], 'medium')
        argv = selector._command(option, 'lookup', self.root / 'selection.json')
        self.assertNotIn('--effort', argv)
        self.assertFalse(any(lane['lane_id'] in issue for issue in fleetctl.effort_problems(roster, self.root)))

    def test_brief_and_console_show_saved_name_and_sleeping(self):
        roster = self.expand()
        overview = fleetctl.fleet_overview(roster, {'leases': []}, self.root / 'state')
        for verbose in (False, True):
            brief = fleetctl.render_brief(overview, verbose)
            self.assertIn('Latest / medium (sleeping)', brief)
        model = next(m for m in overview['models'] if m['model'] == 'chatgpt:' + self.label)
        pool = next(p for p in overview['pools'] if p['pool'] == 'chatgpt-work')
        option = next(o for o in pool['options'] if o['model'] == model['model'])
        self.assertIn('Sleeping; wakes automatically when selected', console._option(model, pool, option))

    def test_fresh_chat_sequence_mention_permission_archive_and_close(self):
        self.browser['allow'] = True
        self.write_browser()
        self.wake()
        calls = self.read_browser()['calls']
        self.assertEqual([c for c in calls if c[0] == 'open'], [['open', workers.HOME, '--group', 'Crossfeed Chat workers']])
        self.assertIn(['type', '42', '[contenteditable=true]', ' wake up as ' + self.label + '. Poll gateway_exchange with worker_label ' + self.label + ' until released.', '--mode', 'append'], calls)
        mention = ['click', '42', '[data-crossfeed-wake="mention"]']
        append = next(c for c in calls if c[0] == 'type' and c[3].startswith(' wake up as '))
        self.assertLess(calls.index(mention), calls.index(append))
        self.assertIn(['click', '42', '[data-crossfeed-wake="allow"]'], calls)
        self.assertEqual(calls[-1], ['close', '42'])
        self.assertTrue(self.read_browser()['recent'])
        picker = next(c for c in calls if c[0] == 'eval' and 'return {row:' in c[-1])
        typed = ['type', '42', '[contenteditable=true]', '@crossfeed']
        enter = ['press', '42', 'Enter']
        archive = next(c for c in calls if c[0] == 'eval' and "fetch('/api/auth/session')" in c[-1])
        self.assertLess(calls.index(picker), calls.index(typed))
        self.assertLess(calls.index(typed), calls.index(mention))
        self.assertLess(calls.index(append), calls.index(enter))
        self.assertLess(calls.index(enter), calls.index(archive))
        self.assertEqual(self.read_browser()['archived'], ['saved-worker'])
        self.assertLessEqual(len(calls), 23)

    def test_redirect_refuses_before_send_and_closes(self):
        self.browser['observations'] = [dict(self.browser['observed'])]
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'new-chat home'):
            self.wake()
        calls = self.read_browser()['calls']
        self.assertFalse(any(c[0] == 'type' for c in calls))
        self.assertEqual(calls[-1], ['close', '42'])

    def test_chat_toggle_clicked_when_work_is_selected(self):
        self.browser['observed']['chat'] = False
        self.write_browser()
        self.wake()
        calls = self.read_browser()['calls']
        click = ['click', '42', '[data-crossfeed-wake="chat"]']
        picker = next(c for c in calls if c[0] == 'eval' and 'return {row:' in c[-1])
        self.assertLess(calls.index(click), calls.index(picker))

    def test_permission_prompt_appearing_after_first_check_is_approved(self):
        self.browser.update(allow=True, allow_after_checks=2)
        self.write_browser()
        self.wake(timeout=10)
        state = self.read_browser()
        self.assertEqual(state['allow_checks'], 2)
        self.assertEqual(sum(c[0] == 'click' and '=\"allow\"' in c[-1] for c in state['calls']), 1)
        self.assertTrue(state['polling'])

    def test_unresponsive_optional_permission_read_keeps_polling_gateway(self):
        self.browser.update(allow=True, allow_read_error='Chrome did not respond within 25 seconds')
        self.write_browser()
        self.wake()
        self.assertTrue(self.read_browser()['polling'])
        self.assertEqual(self.read_browser()['calls'][-1], ['close', '42'])

    def test_transient_chat_mode_reads_retry_then_wake_succeeds(self):
        self.browser['pill_errors'] = 2
        self.write_browser()
        self.wake()
        state = self.read_browser()
        self.assertTrue(state['polling'])
        self.assertEqual(state['pill_reads'], 5)
        self.assertEqual(sum(c[0] == 'open' for c in state['calls']), 1)
        self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['failed'], {})

    def test_persistent_chat_mode_read_fails_at_deadline_with_category(self):
        self.browser['pill_errors'] = -1
        self.write_browser()
        started = self.clock.monotonic()
        with self.assertRaises(transport.Rejected) as caught:
            self.wake(timeout=6)
        self.assertEqual(caught.exception.message, 'Gaddi unresponsive during Chat mode selection')
        elapsed = self.clock.monotonic() - started
        self.assertGreaterEqual(elapsed, 6)
        self.assertLess(elapsed, 8)
        state = self.read_browser()
        self.assertGreater(state['pill_reads'], 1)
        self.assertFalse(any(c[0] == 'type' for c in state['calls']))
        self.assertEqual(state['calls'][-1], ['close', '42'])

    def test_persistent_click_error_stops_after_three_observed_attempts(self):
        self.browser.update(click_error=True, observed={'chat': False, 'pill': 'Medium', 'url': self.url})
        self.write_browser()
        with self.assertRaises(transport.Rejected) as caught:
            self.wake()
        self.assertEqual(caught.exception.message, 'Gaddi unresponsive during Chat mode selection')
        calls = self.read_browser()['calls']
        self.assertEqual(sum(c[0] == 'click' for c in calls), 3)
        self.assertFalse(any(c[0] == 'type' for c in calls))
        self.assertEqual(calls[-1], ['close', '42'])

    def test_stalled_mutations_observe_before_retrying(self):
        wake_text = (' wake up as ' + self.label + '. Poll gateway_exchange with worker_label '
                     + self.label + ' until released.')
        steps = [('click', '[data-crossfeed-wake="chat"]'),
                 ('press', 'ArrowDown'),
                 ('click', '[data-model-picker-view-toggle="true"]'),
                 ('click', '[data-crossfeed-wake="row"]'),
                 ('press', 'ArrowRight'), ('press', 'ArrowLeft'), ('press', 'Escape'),
                 ('type', '@crossfeed'), ('click', '[data-crossfeed-wake="mention"]'),
                 ('type', wake_text)]
        original = copy.deepcopy(self.browser)
        for action, argument in steps:
            for after in (False, True):
                with self.subTest(action=action, argument=argument, applied_before_stall=after):
                    self.browser = copy.deepcopy(original)
                    self.browser.update(picker_row='Other row', target_visible=False,
                                        picker_position=2 if argument == 'ArrowLeft' else 0,
                                        observed={'chat':False, 'pill':'Wrong', 'url':self.url},
                                        stall={'action':action, 'argument':argument, 'remaining':1, 'after':after})
                    self.reset_limits()
                    self.write_state()
                    self.write_browser()
                    self.wake()
                    state = self.read_browser()
                    self.assertTrue(state['polling'])
                    self.assertEqual(state['stall']['remaining'], 0)
                    calls = state['calls']
                    indices = [i for i, c in enumerate(calls) if c[0] == action and argument in c[2:]]
                    expected = (2 if not after else 1)
                    # Selecting a different row also reopens the menu.
                    if argument == 'ArrowDown': expected += 1
                    self.assertEqual(len(indices), expected)
                    if not after:
                        self.assertTrue(any(c[0]=='eval' for c in calls[indices[0]+1:indices[1]]))
                    self.assertEqual(state['composer'], 'crossfeed' + wake_text)
                    self.assertEqual(sum(c[0]=='press' and c[-1]=='Enter' for c in calls), 1)

    def test_enter_stall_never_resends_even_when_it_did_not_apply(self):
        for after in (False, True):
            with self.subTest(applied_before_stall=after):
                self.reset_limits()
                self.browser.update(calls=[], stall={'action':'press', 'argument':'Enter',
                                                    'remaining':1, 'after':after})
                self.write_browser()
                if after:
                    self.wake()
                else:
                    with self.assertRaisesRegex(transport.Rejected, 'orphan archive could not be recorded'):
                        self.wake(timeout=3)
                calls = self.read_browser()['calls']
                self.assertEqual(sum(c[0]=='press' and c[-1]=='Enter' for c in calls), 1)
                if after:
                    self.assertTrue(self.read_browser()['polling'])
                    self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['chats'][self.label]['url'],
                                     'https://chatgpt.com/c/fresh-worker')
                else:
                    self.assertTrue(any(c[0]=='eval' and "=== 'Always allow'" in c[-1] for c in calls))

    def test_lost_mention_typing_reply_accepts_text_or_popup(self):
        for popup_only in (False, True):
            with self.subTest(popup_only=popup_only):
                self.reset_limits()
                self.write_state()
                self.browser.update(calls=[], popup_only=popup_only,
                                    mention_hidden_reads=0 if popup_only else 1,
                                    stall={'action':'type', 'argument':'@crossfeed', 'remaining':1, 'after':True})
                self.write_browser()
                self.wake()
                calls = self.read_browser()['calls']
                self.assertEqual(sum(c[0]=='type' and c[3]=='@crossfeed' for c in calls), 1)
                self.assertTrue(self.read_browser()['polling'])

    @unittest.skipUnless(shutil.which('node'), 'node is required for mention DOM checks')
    def test_mention_accepts_crossfeed_product_capitalization_and_only_visible_popup(self):
        source = r"""
const script = JSON.parse(process.argv[1]);
for (const [text, visible, editor, expected] of [
  ['Crossfeed Chat', true, false, true], ['crossfeed', true, false, true],
  ['CROSSFEED CHAT', true, false, true], ['Crossfeed Chat', false, false, false],
  ['Crossfeed Chat', true, true, false], ['Another connector', true, false, false]
]) {
  const marks = {};
  const button = {textContent:text, getClientRects:()=>visible ? [1] : [],
    closest:()=>editor, setAttribute:(name,value)=>{ marks[name]=value; }};
  global.document = {querySelectorAll:()=>[button]};
  if (eval(script) !== expected) process.exit(1);
  if ((marks['data-crossfeed-wake'] === 'mention') !== expected) process.exit(1);
}
"""
        result = subprocess.run(['node', '-e', source, json.dumps(workers.MENTION)],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'node is required for composer DOM checks')
    def test_composer_observation_distinguishes_raw_mention_from_chip(self):
        source = '''
const script = JSON.parse(process.argv[1]);
for (const [text, chips, expected] of [
  ['@crossfeed', [], false], ['crossfeed', ['crossfeed'], true],
  ['@crossfeed', ['another tool'], false]
]) {
  const e = {innerText:text, getClientRects:()=>[1],
    querySelectorAll:()=>chips.map(text=>({textContent:text}))};
  global.document = {querySelectorAll:()=>[e]};
  const observed = eval(script);
  if (observed.text !== text || observed.chip !== expected) process.exit(1);
}
global.document = {querySelectorAll:()=>[]};
if (eval(script) !== null) process.exit(1);
'''
        result = subprocess.run(['node', '-e', source, json.dumps(workers.COMPOSER)],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_settle_requires_two_ready_reads_before_input(self):
        self.wake()
        state = self.read_browser()
        reads = state['ready_reads']
        self.assertEqual(len(reads), 2)
        self.assertTrue(all(r['ready'] for r in reads))
        self.assertGreaterEqual(self.ready_times[1] - self.ready_times[0], 1.5)
        calls = state['calls']
        ready_indices = [i for i,c in enumerate(calls) if c[0]=='eval' and 'return {ready:' in c[-1]]
        first_input = next(i for i,c in enumerate(calls) if c[0] in ('click','press','type'))
        self.assertLess(ready_indices[-1], first_input)

    def test_settle_restarts_if_readiness_disappears(self):
        self.browser['ready_sequence'] = [True, False, True, True]
        self.write_browser()
        self.wake()
        reads = self.read_browser()['ready_reads']
        self.assertEqual([r['ready'] for r in reads], [True, False, True, True])
        self.assertGreaterEqual(self.ready_times[-1] - self.ready_times[-2], 1.5)

    def test_held_mutation_is_not_retried(self):
        self.browser.update(observed={'chat':False, 'pill':'Medium', 'url':self.url},
                            stall={'action':'click', 'argument':'[data-crossfeed-wake="chat"]',
                                   'remaining':3, 'code':'held'})
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'Gaddi held'):
            self.wake()
        self.assertEqual(sum(c[0]=='click' for c in self.read_browser()['calls']), 1)

    def test_mutation_retry_stays_inside_deadline(self):
        browser = workers.Gaddi(self.lane, time.monotonic()+10)
        calls = []
        def stalled(*args):
            calls.append(args)
            raise transport.Rejected(6, 'Gaddi unresponsive during test')
        def observed():
            browser.deadline = time.monotonic()-1
            return False
        with patch.object(browser, 'call', side_effect=stalled):
            with self.assertRaisesRegex(transport.Rejected, 'timed out'):
                workers._mutate(browser, '42', 'click', 'fixture', confirmed=observed)
        self.assertEqual(len(calls), 1)

    def test_permission_hold_is_not_misclassified_as_optional_debugger_error(self):
        self.browser.update(allow=True, allow_read_code='held', allow_read_error='debugger permission held')
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'Gaddi held'):
            self.wake()
        self.assertEqual(self.read_browser()['calls'][-1], ['close', '42'])

    def test_each_saved_position_restores_a_different_composer_level(self):
        for position, name in enumerate(workers.LEVEL_NAMES):
            with self.subTest(position=position):
                self.map[self.label]['position'] = position
                self.write_state()
                self.browser.update(calls=[], recent=False, observed={'chat': True, 'pill': 'Wrong level', 'url': self.url})
                self.reset_limits()
                self.write_browser()
                self.wake()
                state = self.read_browser()
                self.assertTrue(state['recent'])
                selections = [c for c in state['calls'] if c[0] == 'eval' and 'return {row:' in c[-1]]
                self.assertGreaterEqual(len(selections), 1)
                self.assertIn('"position": ' + str(position), selections[0][-1])
                self.assertEqual(state.get('picker_position', 1), position)

    def test_unconfirmed_selection_refuses_before_send_and_closes(self):
        self.browser['selection'] = {'row': 'Wrong row', 'targetVisible': True}
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'not confirmed'):
            self.wake()
        calls = self.read_browser()['calls']
        self.assertFalse(any(c[0] == 'type' for c in calls))
        self.assertEqual(calls[-1], ['close', '42'])

    def test_recent_worker_needs_no_browser_and_unknown_label_is_refused(self):
        self.browser['recent'] = True
        self.write_browser()
        self.wake()
        self.assertEqual(self.read_browser()['calls'], [])
        with self.assertRaisesRegex(transport.Rejected, 'no saved worker configuration'):
            workers.wake(self.lane, 'unknown-medium', clock=self.clock)

    def test_processing_claim_remains_active_after_poll_contact_goes_stale(self):
        status = {'workers': [{'label': self.label, 'contact': 'stale',
                               'polling': False, 'processing_claim': True}]}
        request = self.gateway
        def respond(base, key, path, **kwargs):
            return status if path == '/gateway/status' else request(base, key, path, **kwargs)
        with patch.object(workers, 'request', side_effect=respond):
            workers.wake(self.lane, self.label, clock=self.clock)
        self.assertEqual(self.read_browser()['calls'], [])

    def test_recent_but_not_polling_worker_is_woken(self):
        self.browser.update(recent=True, polling=False)
        self.write_browser()
        self.wake()
        self.assertTrue(self.read_browser()['polling'])
        self.assertEqual(sum(c[0] == 'open' for c in self.read_browser()['calls']), 1)

    def test_stalled_background_tab_shows_only_its_owned_tab_once(self):
        self.browser['wait_for_show'] = True
        self.write_browser()
        self.wake(timeout=180)
        self.assertGreaterEqual(self.clock.monotonic(), 30)
        self.assertEqual([c for c in self.read_browser()['calls'] if c[0] == 'show'], [['show', '42']])
        self.assertEqual(self.read_browser()['calls'][-1], ['close', '42'])

    def test_cleanup_failure_preserves_original_refusal(self):
        self.browser['close_error'] = True
        self.browser['selection'] = {'row': 'Wrong row', 'targetVisible': True}
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'not confirmed.*cleanup failed'):
            self.wake(timeout=3)

    def test_zero_exit_close_with_failed_tab_is_not_success(self):
        self.browser['close_result'] = {'closed': [], 'failed': [{'tab': 42, 'reason': 'still open'}]}
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'closure was not confirmed'):
            self.wake()

    def test_cleanup_already_missing_owned_tab_is_confirmed_absent(self):
        self.browser['close_result'] = {'closed': [], 'failed': [{'tab': 42, 'reason': 'No tab with id: 42.'}]}
        self.write_browser()
        self.wake()
        self.assertTrue(self.read_browser()['recent'])

    def test_missing_pill_times_out_without_menu_click_or_send(self):
        self.browser['observed']['pill'] = None
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'timed out'):
            self.wake(timeout=.5)
        calls = self.read_browser()['calls']
        self.assertFalse(any(c[0] in ('click', 'type', 'press') for c in calls))
        self.assertEqual(calls[-1], ['close', '42'])

    def test_active_unsaved_label_is_not_a_configured_wake(self):
        del self.map[self.label]
        self.browser.update(recent=True, polling=True)
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'no saved worker configuration'):
            self.wake()
        self.assertEqual(self.read_browser()['calls'], [])

    def test_older_saved_workers_use_existing_console_brief_and_routing_gates(self):
        self.map[self.label]['row'] = 'Previous row'
        self.map['other-pro']['row'] = 'Latest'
        self.write_state()

        roster = self.expand()
        key = 'chatgpt:' + self.label
        runtime = {'leases': [], 'quota_snapshots': {}}
        self.assertTrue(fleetctl.model_is_older(roster, 'chatgpt-work', key))
        self.assertFalse(fleetctl.pool_switches(roster, runtime, 'chatgpt-work')[key])
        options, _ = selector.enumerate_options(roster, runtime, 'lookup', fleet=fleetctl)
        self.assertFalse(any(o.get('lane_id') == key for o in options))
        overview = fleetctl.fleet_overview(roster, runtime, self.root / 'state')
        pool = next(p for p in overview['pools'] if p['pool'] == 'chatgpt-work')
        option = next(o for o in pool['options'] if o['model'] == key)
        self.assertFalse(option['current'])
        self.assertFalse(option['on'])
        model = next(m for m in overview['models'] if m['model'] == key)
        self.assertIn('Older model', console._option(model, pool, option))
        for verbose in (False, True):
            self.assertIn('Older ChatGPT workers: Previous row / medium (sleeping)',
                          fleetctl.render_brief(overview, verbose))
        # A retained older entry follows the existing explicit switch, rather
        # than becoming current merely because its gateway contact is recent.
        roster['model_cards'][key]['older_model_reasons'] = {
            'chatgpt-work': {'job': 'lookup', 'compared_to': 'chatgpt:other-pro', 'advantage': 'faster',
                            'reason': 'fixture latency advantage', 'evidence': 'fixture comparative measurement'}}
        self.assertTrue(fleetctl.older_model_reason(roster, 'chatgpt-work', key))
        self.roster['model_cards'][key] = copy.deepcopy(roster['model_cards'][key])
        self.assertEqual(fleetctl.older_model_reason(self.expand(), 'chatgpt-work', key),
                         fleetctl.older_model_reason(roster, 'chatgpt-work', key))
        fleetctl.set_model_toggle(runtime, roster, 'chatgpt-work', key, False)
        self.assertFalse(fleetctl.pool_switches(roster, runtime, 'chatgpt-work')[key])
        fleetctl.set_model_toggle(runtime, roster, 'chatgpt-work', key, True)
        self.assertTrue(fleetctl.pool_switches(roster, runtime, 'chatgpt-work')[key])

    def test_api_saved_rows_reject_unsafe_label_and_invalid_configuration(self):
        for model in (
            {'id': 'chatgpt:unsafe\nmedium', 'row': 'Picker', 'level': 1},
            {'id': 'chatgpt:safe', 'row': 'Picker', 'level': 5},
            {'id': 'chatgpt:safe', 'row': 'Picker', 'level': True},
            {'id': 'chatgpt:safe', 'row': '', 'level': 1},
            {'id': 'chatgpt:safe', 'row': 'Picker', 'level': 'high'},
        ):
            with self.subTest(model=model), patch.object(workers, 'request', return_value={
                    'object': 'list', 'data': [dict(model, object='model', saved=True)]}):
                with self.assertRaises(transport.Rejected):
                    workers.load_workers(self.lane)

    def test_state_rejects_new_chat_url_and_invalid_saved_url(self):
        for url in ('https://chatgpt.com/', None, self.url + '\n'):
            self.map[self.label]['url'] = url
            self.write_state()
            with self.subTest(url=url), self.assertRaises(transport.Rejected):
                self.wake()

    def test_concurrent_wakes_send_once_after_lock_contact_recheck(self):
        errors = []
        def run():
            try:
                workers.wake(self.lane, self.label, timeout=240, clock=FakeClock())
            except Exception as exc:
                errors.append(str(exc))
        with patch.object(workers, 'request', side_effect=self.gateway):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(c[0] == 'open' for c in self.read_browser()['calls']), 1)

    def test_contact_timeout_closes_tab(self):
        original = self.gateway
        def stale(*args, **kwargs):
            result = original(*args, **kwargs)
            for row in result.get('workers', []): row['contact'] = 'stale'
            return result
        with patch.object(workers, 'request', side_effect=stale):
            with self.assertRaisesRegex(transport.Rejected, 'timed out'):
                workers.wake(self.lane, self.label, timeout=.6, clock=self.clock)
        self.assertEqual(self.read_browser()['calls'][-1], ['close', '42'])

    def test_first_wake_accepts_configuration_without_url(self):
        del self.map[self.label]['url']
        self.write_state()
        self.assertNotIn('url', workers.load_workers(self.lane)[self.label])
        self.wake()
        saved = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(saved['chats'][self.label]['url'], 'https://chatgpt.com/c/fresh-worker')
        self.assertNotIn('archived', self.read_browser())

    def test_url_and_limits_are_written_atomically_preserving_other_keys(self):
        self.map[self.label]['custom'] = {'preserve': True}
        self.write_state()
        replacements = []
        replace = os.replace
        def observe(source, target):
            source, target = Path(source), Path(target)
            self.assertNotEqual(source, target)
            self.assertEqual(source.parent, target.parent)
            json.loads(source.read_text())
            replacements.append(target.name)
            replace(source, target)
        with patch.object(workers.os, 'replace', side_effect=observe):
            self.wake()
        self.assertIn('wake-state.json', replacements)
        saved = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(saved['_note'], self.map['_note'])
        self.assertEqual(saved['chats']['other-pro'], {'url': self.map['other-pro']['url']})
        self.assertEqual(saved['chats'][self.label]['custom'], {'preserve': True})
        self.assertNotIn('row', saved['chats'][self.label])
        self.assertNotIn('level', saved['chats'][self.label])

    def test_archive_failure_is_pending_then_retried_on_next_success(self):
        self.browser['archive_status'] = 503
        self.write_browser()
        self.wake()
        saved = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(saved['_archive_pending'], ['saved-worker'])
        self.browser = self.read_browser()
        self.browser.update(recent=False, polling=False, archive_status=200)
        self.write_browser()
        self.wake()
        saved = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(saved['_archive_pending'], [])
        self.assertEqual(saved['chats'][self.label]['url'], 'https://chatgpt.com/c/fresh-worker-2')
        self.assertEqual(self.read_browser()['archived'], ['saved-worker', 'saved-worker', 'fresh-worker'])

    def test_failed_after_send_preserves_previous_url_and_records_orphan(self):
        self.browser['allow'] = True
        self.browser['allow_after_checks'] = 999
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'timed out'):
            self.wake(timeout=3)
        saved = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(saved['chats'][self.label]['url'], self.url)
        self.assertEqual(saved['_archive_pending'], ['fresh-worker'])
        calls = self.read_browser()['calls']
        with self.assertRaisesRegex(transport.Rejected, 'failure cooldown'):
            self.wake()
        self.assertEqual(self.read_browser()['calls'], calls)
        self.browser = self.read_browser()
        self.browser.update(allow=False)
        self.write_browser()
        self.reset_limits()
        self.wake()
        self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['_archive_pending'], [])
        self.assertIn('fresh-worker', self.read_browser()['archived'])

    def test_registered_chat_location_error_is_retried_and_retained(self):
        self.browser['location_errors'] = 1
        self.write_browser()
        self.wake()
        self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['chats'][self.label]['url'], 'https://chatgpt.com/c/fresh-worker')
        self.assertEqual(self.read_browser()['location_reads'], 2)

    def test_enter_lost_reply_with_delayed_url_registers_without_resend(self):
        self.browser.update(enter_error=True, location_home_reads=2)
        self.write_browser()
        self.wake()
        saved = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(saved['chats'][self.label]['url'], 'https://chatgpt.com/c/fresh-worker')
        self.assertEqual(saved['_archive_pending'], [])
        self.assertEqual(sum(c[0]=='press' and c[-1]=='Enter' for c in self.read_browser()['calls']), 1)
        self.assertEqual(self.read_browser()['location_reads'], 3)
        self.assertEqual(self.read_browser()['calls'][-1], ['close', '42'])

    def test_interruption_after_enter_records_orphan_and_failure_cooldown(self):
        call = workers.Gaddi.call
        def interrupted(browser, *args):
            result = call(browser, *args)
            if args[0] == 'press' and args[-1] == 'Enter':
                raise KeyboardInterrupt()
            return result
        with patch.object(workers.Gaddi, 'call', new=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.wake()
        self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['_archive_pending'], ['fresh-worker'])
        self.assertIn(self.label, json.loads((self.root / 'wake-state.json').read_text())['failed'])
        self.assertEqual(self.read_browser()['calls'][-1], ['close', '42'])

    def test_archive_queue_compaction_write_failure_is_nonfatal(self):
        atomic = workers._atomic_json
        writes = [0]
        def write(path, data):
            if path.name == 'wake-state.json':
                writes[0] += 1
                if writes[0] == 3:
                    raise transport.Rejected(6, 'fixture archive queue write failure')
            atomic(path, data)
        with patch.object(workers, '_atomic_json', side_effect=write):
            self.wake()
        saved = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(saved['chats'][self.label]['url'], 'https://chatgpt.com/c/fresh-worker')
        self.assertEqual(saved['_archive_pending'], ['saved-worker'])
        self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['failed'], {})

    def test_daily_cap_blocks_before_browser_and_expires_after_24_hours(self):
        now = 200000
        (self.root / 'wake-state.json').write_text(json.dumps({'wakes': [now-1]*30}))
        with patch.object(workers.time, 'time', return_value=now):
            with self.assertRaisesRegex(transport.Rejected, 'daily cap'):
                self.wake()
        self.assertEqual(self.read_browser()['calls'], [])
        with patch.object(workers.time, 'time', return_value=now+86400):
            self.wake()
        self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['wakes'], [now+86400])

    def test_custom_daily_cap_counts_failed_attempts(self):
        self.lane['wake_daily_cap'] = 1
        self.browser['selection'] = {'row': 'Wrong row', 'targetVisible': True}
        self.write_browser()
        with self.assertRaises(transport.Rejected):
            self.wake()
        now = time.time()
        state = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(len(state['wakes']), 1)
        self.assertNotIn('refunded', self.log_rows()[0])
        state['failed'][self.label] = now-601
        (self.root / 'wake-state.json').write_text(json.dumps(state))
        calls = self.read_browser()['calls']
        with self.assertRaisesRegex(transport.Rejected, 'daily cap'):
            self.wake()
        self.assertEqual(self.read_browser()['calls'], calls)

    def test_browser_unavailable_refunds_and_backs_off_all_labels_until_expiry(self):
        self.lane['wake_daily_cap'] = 1
        now = 200000
        path = self.root / 'wake-state.json'
        for message in ('chrome bridge not connected', 'Gaddi unreachable', 'connect ECONNREFUSED'):
            with self.subTest(message=message), patch.object(self.clock, 'time', return_value=now):
                self.write_state()
                self.reset_limits()
                self.browser['open_error'] = message
                self.write_browser()
                with self.assertRaisesRegex(transport.Rejected, 'Gaddi unavailable'):
                    self.wake()
                state = json.loads(path.read_text())
                self.assertEqual(state['wakes'], [])
                self.assertEqual(state['failed'], {})
                self.assertEqual(state['browser_backoff_until'], now + 300)
                row = self.log_rows()[-1]
                self.assertEqual((row['outcome'], row['stage'], row['message'], row['refunded']),
                                 ('failed', 'opening new chat', 'Gaddi unavailable', True))
                calls = self.read_browser()['calls']
                with patch.object(self.clock, 'time', return_value=now + 299):
                    with self.assertRaisesRegex(transport.Rejected, 'wakes paused for 5 minutes'):
                        workers.wake(self.lane, 'other-pro', clock=self.clock)
                self.assertEqual(self.read_browser()['calls'], calls)
                self.assertNotIn('refunded', self.log_rows()[-1])
                self.browser.pop('open_error')
                self.write_browser()
                with patch.object(self.clock, 'time', return_value=now + 300):
                    self.wake()
                self.assertEqual(json.loads(path.read_text())['wakes'], [now + 300])

    def test_missing_gaddi_refunds_and_sets_global_backoff(self):
        with patch.object(self.clock, 'time', return_value=200000), \
                patch.object(workers.subprocess, 'run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(transport.Rejected, 'Gaddi unavailable'):
                self.wake()
        state = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(state['wakes'], [])
        self.assertEqual(state['failed'], {})
        self.assertEqual(state['browser_backoff_until'], 200300)
        self.assertTrue(self.log_rows()[-1]['refunded'])

    def test_unreachable_daemon_stderr_refunds_and_sets_global_backoff(self):
        result = subprocess.CompletedProcess([], 1, '',
                'gaddi: daemon not reachable at /private/fixture.sock (ENOENT)\n')
        with patch.object(self.clock, 'time', return_value=200000), \
                patch.object(workers.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(transport.Rejected, 'Gaddi unavailable'):
                self.wake()
        state = json.loads((self.root / 'wake-state.json').read_text())
        self.assertEqual(state['wakes'], [])
        self.assertEqual(state['failed'], {})
        self.assertEqual(state['browser_backoff_until'], 200300)
        self.assertTrue(self.log_rows()[-1]['refunded'])
        self.assertNotIn('fixture.sock', workers.wake_log_file(self.lane).read_text())

    def test_other_failure_before_open_refunds_only_its_reservation(self):
        path = self.root / 'wake-state.json'
        state = json.loads(path.read_text())
        state['wakes'] = [199999]
        path.write_text(json.dumps(state))
        with patch.object(self.clock, 'time', return_value=200000), \
                patch.object(workers.Gaddi, 'call', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                self.wake()
        self.assertEqual(json.loads(path.read_text())['wakes'], [199999])
        self.assertTrue(self.log_rows()[-1]['refunded'])

    def test_failure_cooldown_is_per_label_and_expires(self):
        now = time.time()
        state = {'failed': {self.label: now, 'other-pro': now}}
        (self.root / 'wake-state.json').write_text(json.dumps(state))
        with patch.object(workers.time, 'time', return_value=now+599):
            with self.assertRaisesRegex(transport.Rejected, 'failure cooldown'):
                self.wake()
        self.assertEqual(self.read_browser()['calls'], [])
        with patch.object(workers.time, 'time', return_value=now+600):
            self.wake()
        self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['failed'], {})

    def test_rate_limit_sets_global_cooldown_and_refuses_other_labels(self):
        for text in ('Too many requests', "YOU'VE REACHED the limit", 'rate LIMIT'):
            with self.subTest(text=text):
                self.reset_limits()
                self.browser.update(calls=[], rate_limit=text, rate_after_send=False)
                self.write_browser()
                with self.assertRaisesRegex(transport.Rejected, 'rate-limit cooldown') as caught:
                    self.wake()
                self.assertNotIn(text, str(caught.exception))
                state = json.loads((self.root / 'wake-state.json').read_text())
                self.assertGreater(state['cooldown_until'], time.time()+3590)
                calls = self.read_browser()['calls']
                with patch.object(workers, 'request', side_effect=self.gateway):
                    with self.assertRaisesRegex(transport.Rejected, 'rate-limit cooldown'):
                        workers.wake(self.lane, 'other-pro', clock=self.clock)
                self.assertEqual(self.read_browser()['calls'], calls)
                self.assertEqual(calls[-1], ['close', '42'])

    def test_pro_rate_limit_allows_sleeping_high_to_wake(self):
        self.map[self.label]['position'] = 4
        self.browser.update(rate_limit=True, rate_after_send=False)
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'rate-limit cooldown'):
            self.wake()
        state = json.loads((self.root / 'wake-state.json').read_text())
        self.assertGreater(state['pro_cooldown_until'], time.time() + 3590)
        self.assertEqual(state['cooldown_until'], 0)
        # A different sleeping High label must pass the same persisted guard.
        self.label = 'latest-high'
        self.map[self.label] = {'row': 'Latest', 'position': 2}
        self.lane.update(selector='chatgpt:' + self.label, worker_label=self.label)
        self.browser.update(rate_limit=False, recent=False, polling=False)
        self.write_browser()
        self.wake(timeout=30)
        self.assertTrue(self.read_browser()['sent'])

    def test_rate_limit_after_send_still_records_orphan(self):
        self.browser.update(rate_limit=True, rate_after_send=True, allow=True)
        self.write_browser()
        with self.assertRaisesRegex(transport.Rejected, 'rate-limit cooldown'):
            self.wake()
        self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['_archive_pending'], ['fresh-worker'])

    def test_global_rate_cooldown_expires(self):
        now = 200000
        (self.root / 'wake-state.json').write_text(json.dumps({'cooldown_until': now+3600}))
        with patch.object(workers.time, 'time', return_value=now+3599):
            with self.assertRaisesRegex(transport.Rejected, 'rate-limit cooldown'):
                self.wake()
        self.assertEqual(self.read_browser()['calls'], [])
        with patch.object(workers.time, 'time', return_value=now+3600):
            self.wake()
        self.assertTrue(self.read_browser()['recent'])

    @unittest.skipUnless(shutil.which('node'), 'JavaScript rate fixture requires node')
    def test_real_rate_detection_uses_visible_error_surfaces_only(self):
        cases = [
            ({'body': 'rate limit design in sidebar', 'elements': [{'role': 'navigation', 'text': 'rate limit design'}]}, False),
            ({'title': 'Too MANY requests: private details'}, True),
            ({'elements': [{'role': 'alert', 'text': 'Too many requests: private details'}]}, True),
            ({'elements': [{'role': 'dialog', 'text': "You've reached private details"}]}, True),
            ({'elements': [{'role': 'status', 'text': 'RATE limit private details'}]}, True),
            ({'elements': [{'role': 'alert', 'text': 'all clear'}]}, False),
            ({'elements': [{'role': 'alert', 'text': 'rate limit', 'visible': False}]}, False),
            ({'elements': [{'role': 'dialog', 'text': 'rate limit', 'visibility': 'hidden'}]}, False),
        ]
        for ancestor in ('nav', 'aside', '[role="navigation"]', '[data-testid^="conversation-turn"]', '[data-message-author-role]'):
            cases.append(({'elements': [{'role': 'alert', 'text': 'rate limit design', 'ancestors': [ancestor]}]}, False))
        for fixture, blocked in cases:
            with self.subTest(fixture=fixture):
                browser = workers.Gaddi(self.lane, time.monotonic()+5)
                browser.on_rate_limit = callback = unittest.mock.Mock()
                def evaluate(*args):
                    source = "const fixture = " + json.dumps(fixture) + ";" + r"""
const elements = (fixture.elements || []).map(e => ({
 innerText: e.text, role: e.role,
 closest: selector => (e.ancestors || []).some(a => selector.split(',').map(s => s.trim()).includes(a)),
 getClientRects: () => e.visible === false ? [] : [{}],
 visibility: e.visibility || 'visible'
}));
global.document = {title: fixture.title || '', body: {innerText: fixture.body || ''},
 querySelectorAll: selector => elements.filter(e => selector.includes('[role="' + e.role + '"]'))};
global.getComputedStyle = e => ({visibility: e.visibility});
""" + 'console.log(JSON.stringify(' + args[-1] + '));'
                    result = subprocess.run(['node', '-e', source], capture_output=True, text=True, timeout=3)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return {'value': json.loads(result.stdout)}
                with patch.object(browser, 'call', side_effect=evaluate):
                    if blocked:
                        with self.assertRaisesRegex(transport.Rejected, 'rate-limit cooldown') as caught:
                            browser.evaluate('42', 'true')
                        self.assertNotIn('private details', str(caught.exception))
                        callback.assert_called_once()
                    else:
                        self.assertTrue(browser.evaluate('42', 'true'))
                        callback.assert_not_called()

    def test_wake_guard_reports_bounds_and_cooldown_without_changing_state(self):
        now = time.time()
        path = self.root / 'wake-state.json'
        for cooldown, failed, expected in ((0, {}, 'inactive'), (now+1, {}, 'active'),
                                           (0, {self.label: now-599}, 'active'), (0, {self.label: now-600}, 'inactive')):
            original = json.dumps({'wakes': [now-86400, now-10], 'cooldown_until': cooldown, 'failed': failed})
            path.write_text(original)
            with patch.object(workers.time, 'time', return_value=now):
                self.assertEqual(workers.wake_guard_status(self.lane),
                                 'wake guard: 1/30 wakes in last 24 h, cooldown ' + expected)
            self.assertEqual(path.read_text(), original)
        path.write_text('private invalid fixture')
        self.assertEqual(workers.wake_guard_status(self.lane), 'wake guard: unavailable')

    def test_global_lock_serializes_different_labels(self):
        errors, concurrent, maximum = [], [0], [0]
        call = workers.Gaddi.call
        def counted(browser, *args):
            if args[0] == 'open':
                concurrent[0] += 1
                maximum[0] = max(maximum[0], concurrent[0])
                time.sleep(.1)
            result = call(browser, *args)
            if args[0] == 'close':
                concurrent[0] -= 1
            return result
        def gateway(*args, **kwargs):
            if args[2] == '/models':
                return self.gateway(*args, **kwargs)
            active = self.read_browser().get('active_labels', [])
            return {'workers': [{'label': label, 'contact': 'recent' if label in active else 'stale',
                                  'polling': label in active} for label in (self.label, 'other-pro')]}
        def run(label):
            try:
                workers.wake(self.lane, label, timeout=240, clock=FakeClock())
            except Exception as exc:
                errors.append(str(exc))
        with patch.object(workers, 'request', side_effect=gateway), patch.object(workers.Gaddi, 'call', new=counted):
            threads = [threading.Thread(target=run, args=(label,)) for label in (self.label, 'other-pro')]
            for thread in threads: thread.start()
            for thread in threads: thread.join(12)
        self.assertEqual(errors, [])
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(maximum[0], 1)
        self.assertEqual(sum(c[0] == 'open' for c in self.read_browser()['calls']), 2)
        self.assertEqual(len(json.loads((self.root / 'wake-state.json').read_text())['wakes']), 2)

    def test_wake_lock_wait_uses_request_budget_beyond_240_seconds(self):
        lock = (self.root / '.worker-wake.lock').open('a')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        sleep = self.clock.sleep
        def advance(seconds):
            sleep(seconds)
            if self.clock.monotonic() >= 300:
                fcntl.flock(lock, fcntl.LOCK_UN)
        with patch.object(self.clock, 'sleep', side_effect=advance):
            self.wake(timeout=600)
        self.assertGreaterEqual(self.ready_times[0], 300)
        self.assertTrue(self.read_browser()['polling'])
        self.assertEqual(len(json.loads((self.root / 'wake-state.json').read_text())['wakes']), 1)

    def test_concurrent_labels_wait_past_old_wake_cap_and_open_one_at_a_time(self):
        opened, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        errors, active, maximum = [], [0], [0]
        call = workers.Gaddi.call
        class QueuedClock(FakeClock):
            def sleep(self, seconds):
                super().sleep(seconds)
                if self.now >= 300:
                    release.set()
        queued_clock = QueuedClock()
        def counted(browser, *args):
            if args[0] == 'open':
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
                opened.set()
                if not release.wait(5):
                    raise RuntimeError('second wake never queued')
            result = call(browser, *args)
            if args[0] == 'close':
                active[0] -= 1
            return result
        def gateway(*args, **kwargs):
            if args[2] == '/models':
                return self.gateway(*args, **kwargs)
            labels = self.read_browser().get('active_labels', [])
            return {'workers': [{'label': label, 'contact': 'recent' if label in labels else 'stale',
                                 'polling': label in labels} for label in (self.label, 'other-pro')]}
        def run(label, clock):
            try:
                workers.wake(self.lane, label, timeout=600, clock=clock)
            except Exception as exc:
                errors.append(str(exc))
                release.set()
        with patch.object(workers, 'request', side_effect=gateway), patch.object(workers.Gaddi, 'call', new=counted):
            first = threading.Thread(target=run, args=(self.label, FakeClock()))
            second = threading.Thread(target=run, args=('other-pro', queued_clock))
            first.start()
            self.assertTrue(opened.wait(2))
            second.start()
            for thread in (first, second):
                thread.join(8)
        self.assertTrue(all(not thread.is_alive() for thread in (first, second)))
        self.assertEqual(errors, [])
        self.assertGreaterEqual(queued_clock.now, 300)
        self.assertEqual(maximum[0], 1)
        self.assertEqual(len(json.loads((self.root / 'wake-state.json').read_text())['wakes']), 2)

    def test_wake_lock_timeout_never_spends_cap_or_opens_browser(self):
        with (self.root / '.worker-wake.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(transport.Rejected, 'worker wake timed out'):
                self.wake(timeout=5)
        self.assertEqual(self.clock.monotonic(), 5)
        self.assertEqual(self.read_browser()['calls'], [])
        self.assertEqual(json.loads((self.root / 'wake-state.json').read_text())['wakes'], [])
        self.assertEqual(json.loads(workers.wake_log_file(self.lane).read_text())['stage'], 'wake lock')

    def test_lock_released_at_deadline_does_not_start_wake(self):
        with (self.root / '.worker-wake.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            def advance(seconds):
                self.clock.now += seconds
                fcntl.flock(lock, fcntl.LOCK_UN)
            with patch.object(self.clock, 'sleep', side_effect=advance):
                with self.assertRaisesRegex(transport.Rejected, 'worker wake timed out'):
                    self.wake(timeout=1)
        self.assertEqual(self.read_browser()['calls'], [])

    def test_wake_lock_honours_caller_cancellation(self):
        def check():
            if self.clock.monotonic() >= 3:
                raise transport.Rejected(143, 'external cancellation')
        with (self.root / '.worker-wake.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(transport.Rejected) as error:
                workers.wake(self.lane, self.label, timeout=600, clock=self.clock, check=check)
        self.assertEqual(error.exception.code, 143)
        self.assertEqual(self.clock.monotonic(), 3)
        self.assertEqual(self.read_browser()['calls'], [])

    def test_wake_refusal_propagates_from_transport_health(self):
        self.lane['worker_label'] = self.label
        self.lane['wake_daily_cap'] = 0
        with patch.object(workers, 'request', side_effect=self.gateway):
            with self.assertRaisesRegex(transport.Rejected, 'daily cap') as caught:
                transport.health(self.lane)
        self.assertEqual(caught.exception.code, 6)
        self.assertEqual(self.read_browser()['calls'], [])

    def test_archive_eval_token_never_reaches_trace_or_exception(self):
        sentinel = 'fixture-sensitive-token'
        completed = subprocess.CompletedProcess([], 0, json.dumps({'result': {'value': {'accessToken': sentinel}}}), '')
        browser = workers.Gaddi(self.lane, time.monotonic()+5)
        output = io.StringIO()
        with patch.dict(os.environ, {'CHATGPT_WAKE_TRACE': '1'}), \
             patch.object(workers.subprocess, 'run', return_value=completed), contextlib.redirect_stderr(output):
            workers._retry_archives(browser, '42', self.lane, dict(json.loads((self.root / 'wake-state.json').read_text()), _archive_pending=['failed-chat']))
        self.assertNotIn(sentinel, output.getvalue())
        completed.stdout = json.dumps({'ok': False, 'error': {'message': sentinel}})
        with patch.dict(os.environ, {'CHATGPT_WAKE_TRACE': '1'}), \
             patch.object(workers.subprocess, 'run', return_value=completed), contextlib.redirect_stderr(output):
            with self.assertRaises(transport.Rejected) as caught:
                browser.evaluate('42', workers.ARCHIVE.replace('__ID__', '"failed-chat"'))
        self.assertNotIn(sentinel, str(caught.exception))
        self.assertNotIn(sentinel, output.getvalue())

    @unittest.skipUnless(shutil.which('node'), 'JavaScript archive fixture requires node')
    def test_real_archive_script_returns_only_status_and_keeps_token_in_eval(self):
        script = workers.ARCHIVE.replace('__ID__', '"previous-chat"')
        source = """
const calls = [];
global.fetch = async (url, options) => {
 calls.push(url);
 if (!options) return {ok:true, json:async()=>({accessToken:'fixture-sensitive-token'})};
 if (options.method !== 'PATCH' || options.headers.Authorization !== 'Bearer fixture-sensitive-token' ||
     options.headers['Content-Type'] !== 'application/json' || options.body !== '{"is_archived":true}') throw Error('fixture invalid');
 return {status:204};
};
""" + script + ".then(status => console.log(JSON.stringify({status,calls})));"
        result = subprocess.run(['node', '-e', source], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status':204, 'calls':['/api/auth/session','/backend-api/conversation/previous-chat']})
        self.assertNotIn('fixture-sensitive-token', result.stdout + result.stderr)

    def test_cli_wake_is_available_without_catalog_discovery(self):
        overlay = self.root / 'overlay.json'
        overlay.write_text(json.dumps(self.roster))
        argv = ['fleetctl', '--overlay', str(overlay), '--state-dir', str(self.root / 'state'), 'chatgpt', 'wake', self.label]
        wake = workers.wake
        with patch.object(sys, 'argv', argv), patch.object(workers, 'request', side_effect=self.gateway), \
             patch.object(workers, 'wake', wraps=lambda lane, label: wake(lane, label, clock=self.clock)), \
             patch.object(catalog, 'request', side_effect=AssertionError('must not discover')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(fleetctl.main(), 0)
        self.assertIn('chatgpt:' + self.label + ': recent', output.getvalue())

    def test_connection_refused_is_a_plain_failure(self):
        with patch('socket.create_connection', side_effect=ConnectionRefusedError()):
            with self.assertRaisesRegex(transport.Rejected, 'gateway unavailable'):
                transport.request('http://127.0.0.1:1/v1', 'fixture-key', '/gateway/status')

    def test_work_slider_and_inconsistent_status_refuse_before_send(self):
        for selection in [dict(row='Latest', position=1, minimum='0', maximum='5', status='Light, 2 of 6.'),
                          dict(row='Latest', position=1, minimum='0', maximum='4', status='Extra High, 4 of 5.')]:
            self.browser['selection'] = selection
            self.reset_limits()
            self.write_browser()
            with self.assertRaisesRegex(transport.Rejected, 'Chat power'):
                self.wake()
            self.assertFalse(self.read_browser().get('sent', False))
            self.assertEqual(self.read_browser()['calls'][-1], ['close', '42'])

    @unittest.skipUnless(shutil.which('node'), 'JavaScript DOM fixture requires node')
    def test_picker_without_model_rows_does_not_confirm_the_saved_model(self):
        target = dict(self.map[self.label], row='Latest')
        script = workers.OPEN_PICKER + "; press('ArrowDown');" + workers.PICKER.replace('__WORKER__', json.dumps(target))
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/chatgpt-picker.cjs')],
            input=json.dumps({'script': script, 'noRows': True}), text=True, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)['value']
        self.assertIsNone(value['row'])
        self.assertFalse(value['targetVisible'])

    @unittest.skipUnless(shutil.which('node'), 'JavaScript DOM fixture requires node')
    def test_real_picker_scripts_read_hidden_thumb_and_all_five_statuses(self):
        for position, name in enumerate(workers.LEVEL_NAMES):
            target = dict(self.map[self.label], row='Latest', position=position)
            picker = workers.PICKER.replace('__WORKER__', json.dumps(target))
            source = "(() => {" + workers.OPEN_PICKER + "; press('ArrowDown');" + picker + ";"
            source += "document.querySelector('[data-model-picker-view-toggle=true]').click(); document.querySelector('[data-crossfeed-wake=row]').click();"
            source += workers.OPEN_PICKER + "; press('ArrowDown');" + workers.FOCUS_POWER + ";"
            source += ("press('ArrowRight');" * (position - 1) if position > 1 else "press('ArrowLeft');" * (1 - position))
            source += "const result = " + picker + "; press('Escape'); return result; })()"
            result = subprocess.run(['node', str(ROOT / 'tests/fixtures/chatgpt-picker.cjs')],
                input=json.dumps({'script': source}), text=True, capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(result.stdout)
            self.assertEqual((state['value']['row'], state['value']['position'], state['value']['maximum']), ('Latest', position, '4'))
            self.assertEqual(state['value']['status'], name + ', ' + str(position + 1) + ' of 5.')
            self.assertFalse(state['opened'])

    @unittest.skipUnless(shutil.which('node'), 'JavaScript DOM fixture requires node')
    def test_passive_javascript_reads_visible_pill_and_work_switch(self):
        script = """
const button = (text, attrs = {}, shown = true) => ({innerText: text, textContent: text,
 getClientRects: () => shown ? [1] : [], getAttribute: k => attrs[k] ?? null, hasAttribute: k => k in attrs});
const hiddenPill = button('Pro', {}, false);
const pill = button(' 5.6   Extra High ');
const chat = button('Chat', {'aria-pressed': 'true'});
const work = button('Work', {'aria-pressed': 'false'});
global.document = {
 querySelectorAll: s => s === 'button' ? [chat, work] : s === '[contenteditable=true]' ? [button('')] : [hiddenPill, pill]
};
global.location = {origin: 'https://chatgpt.com', pathname: '/c/saved-worker'};
console.log(JSON.stringify(""" + workers.PILL + "));"
        def evaluate(source):
            result = subprocess.run(['node', '-e', source], text=True, capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertEqual(evaluate(script), {'chat': True, 'pill': '5.6 Extra High', 'url': self.url})
        self.assertFalse(evaluate(script.replace("'aria-pressed': 'false'", "'aria-pressed': 'true'"))['chat'])
        self.assertIsNone(evaluate(script.replace('[chat, work]', '[]'))['chat'])
        self.assertTrue(evaluate(script.replace("button(' 5.6   Extra High ')", "button(' 5.6   Extra High ', {'data-codex-intelligence-trigger': 'true'})"))['chat'])
        hidden_switch = script.replace("const work = button('Work', {'aria-pressed': 'false'});",
                                     "const work = button('Work', {'aria-pressed': 'true'}, false);")
        self.assertIsNone(evaluate(hidden_switch)['chat'])
        ready = script.replace(workers.PILL, workers.READY).replace("global.location", "global.document.readyState = 'complete'; global.document.title = ''; global.location")
        self.assertTrue(evaluate(ready)['ready'])
        self.assertFalse(evaluate(ready.replace('[hiddenPill, pill]', '[hiddenPill]'))['ready'])


if __name__ == '__main__':
    unittest.main()
