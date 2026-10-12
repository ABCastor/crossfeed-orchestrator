"""Business-boundary tests for the owner-approved paid MiMo route, using real HTTP/wrapper."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import fleetctl
from openrouter_budget import Budget, BudgetError, priced_request


class MimoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mimo tests ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = json.loads((ROOT/'tests/fixtures/access-overlay.test.json').read_text())
        self.overlay = self.root/'roster.json'
        self.overlay.write_text(json.dumps(self.roster))
        self.posts = []
        self.price = '0.001'
        self.cost = 0.001
        self.status = 200
        self.stall = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                entries = []
                for model in ('xiaomi/mimo-v2.6-flash', 'xiaomi/mimo-v2.6-pro', 'openrouter/free'):
                    entries.append({'id': model, 'architecture': {'output_modalities': ['text']},
                                    'pricing': {'prompt': owner.price, 'completion': owner.price},
                                    'context_length': 100, 'top_provider': {'max_completion_tokens': 100}})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'data': entries}).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.posts.append(body)
                self.send_response(owner.status)
                self.end_headers()
                if owner.stall:
                    threading.Event().wait(3)
                    return
                if owner.status != 200:
                    self.wfile.write(b'{"error":"synthetic failure"}')
                    return
                self.wfile.write(('data: '+json.dumps({'model': body['model'], 'choices': [{'delta': {'content':'PONG'}}]})+'\n\n').encode())
                if owner.cost is not None:
                    self.wfile.write(('data: '+json.dumps({'usage': {'cost': owner.cost, 'prompt_tokens':1, 'completion_tokens':1}})+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        bindir = self.root/'bin'
        bindir.mkdir()
        # Synthetic key only, through the real 1Password-reference resolution path.
        op = bindir/'op'
        op.write_text('#!/bin/sh\nprintf "synthetic-test-key"\n')
        op.chmod(0o755)
        self.env = dict(os.environ, ACCESS_OVERLAY=str(self.overlay), FLEET_STATE_DIR=str(self.root/'state'),
                        OPENROUTER_API_BASE=f'http://127.0.0.1:{self.server.server_port}',
                        PATH=str(bindir)+os.pathsep+os.environ['PATH'], FLEET_QUOTA_POLICY='off', FLEET_IGNORE_QUOTA='1')

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_wrapper(self, *args, lane='openrouter-mimo-v2.6-flash'):
        return subprocess.run([str(ROOT/'scripts/openrouter-agent.sh'), '--lane', lane,
                               '--prompt', 'Return PONG', '--last', str(self.root/'answer'), *args],
                              env=self.env, text=True, capture_output=True, timeout=20)

    def budget(self):
        return json.loads((self.root/'state/openrouter-mimo-budget.json').read_text())['calls']

    # BR-MIMO-001: prohibition and observable control share the same endpoint.
    def test_paid_model_without_opt_in_cannot_generate(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(len(self.posts), 0)
        self.assertIn('not free right now', result.stderr)

    # BR-MIMO-001 / BR-MIMO-002: control proves this HTTP probe sees a billed request.
    def test_explicit_paid_route_publishes_and_accounts_cost(self):
        result = self.run_wrapper('--paid-mimo', '--max-tokens', '8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'PONG')
        self.assertEqual(self.posts[0]['max_tokens'], 8)
        self.assertFalse(self.posts[0]['provider']['allow_fallbacks'])
        self.assertEqual(self.posts[0]['provider']['max_price'], {'prompt':1000, 'completion':1000})
        self.assertEqual(next(iter(self.budget().values()))['cost_usd'], '0.001')
        receipt = json.loads((self.root/'answer.crossfeed.json').read_text())
        self.assertEqual(receipt['cost'], {'estimated_usd':0.001, 'source':'openrouter-reported'})
        self.assertEqual(receipt['actual_model'], 'xiaomi/mimo-v2.6-flash')

    # BR-MIMO-001
    def test_paid_opt_in_cannot_authorize_other_models(self):
        result = self.run_wrapper('--paid-mimo', lane='openrouter-free-router')
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn('only permits', result.stderr)
        self.assertEqual(len(self.posts), 0)

    # BR-MIMO-002
    def test_price_jump_is_refused_before_post(self):
        self.price = '1'
        result = self.run_wrapper('--paid-mimo')
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertEqual(len(self.posts), 0)
        self.assertFalse((self.root/'state/openrouter-mimo-budget.json').exists())

    # BR-MIMO-002
    def test_pro_and_flash_share_one_cap(self):
        self.roster['quota_pools']['openrouter-mimo']['daily_usd_cap'] = .25
        self.overlay.write_text(json.dumps(self.roster))
        self.cost = .15
        first = self.run_wrapper('--paid-mimo')
        second = self.run_wrapper('--paid-mimo', lane='openrouter-mimo-v2.6-pro')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 5, second.stderr)
        self.assertEqual(len(self.posts), 1)

    # BR-MIMO-002
    def test_missing_cost_keeps_reservation_and_refuses_followup(self):
        self.roster['quota_pools']['openrouter-mimo']['daily_usd_cap'] = .25
        self.overlay.write_text(json.dumps(self.roster))
        self.cost = None
        first = self.run_wrapper('--paid-mimo')
        second = self.run_wrapper('--paid-mimo', lane='openrouter-mimo-v2.6-pro')
        self.assertEqual(first.returncode, 6, first.stderr)
        self.assertEqual(second.returncode, 5, second.stderr)
        self.assertEqual(len(self.posts), 1)
        self.assertIsNone(next(iter(self.budget().values()))['cost_usd'])
        self.assertFalse((self.root/'answer').exists())

    # BR-MIMO-002
    def test_paid_http_failure_has_no_retry_and_keeps_reservation(self):
        self.status = 503
        result = self.run_wrapper('--paid-mimo')
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertEqual(len(self.posts), 1)
        self.assertIsNone(next(iter(self.budget().values()))['cost_usd'])

    # BR-MIMO-002
    def test_concurrent_reservations_cannot_overspend(self):
        def reserve(model):
            try:
                Budget(self.root/'atomic', '1').reserve(model, Decimal('.6'))
                return True
            except BudgetError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ['xiaomi/mimo-v2.6-pro', 'xiaomi/mimo-v2.6-flash']))
        self.assertEqual(sum(results), 1)

    # BR-MIMO-002
    def test_bad_costs_and_corrupt_ledger_fail_closed(self):
        budget = Budget(self.root/'strict')
        token = budget.reserve('xiaomi/mimo-v2.6-flash', Decimal('.2'))
        for cost in (None, 'NaN', '-1', True):
            with self.subTest(cost=cost), self.assertRaises(BudgetError):
                budget.settle(token, cost)
        budget.path.write_text('{broken')
        with self.assertRaises(BudgetError):
            budget.reserve('xiaomi/mimo-v2.6-pro', Decimal('.2'))

    # BR-MIMO-002
    def test_billing_overrun_blocks_new_calls(self):
        budget = Budget(self.root/'overrun')
        token = budget.reserve('xiaomi/mimo-v2.6-flash', '.1')
        with self.assertRaises(BudgetError):
            budget.settle(token, '.2')
        with self.assertRaises(BudgetError):
            budget.reserve('xiaomi/mimo-v2.6-pro', '.1')

    # BR-MIMO-002
    def test_timeout_keeps_paid_reservation(self):
        self.stall = True
        result = self.run_wrapper('--paid-mimo', '--timeout', '1', '--kill-after', '0')
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertEqual(len(self.posts), 1)
        self.assertIsNone(next(iter(self.budget().values()))['cost_usd'])
        self.assertFalse((self.root/'answer').exists())

    # BR-MIMO-002
    def test_midnight_resets_settled_cost_but_keeps_unknown_reservations(self):
        budget = Budget(self.root/'days', '1')
        known = budget.reserve('xiaomi/mimo-v2.6-flash', '.6')
        budget.settle(known, '.6')
        ledger = json.loads(budget.path.read_text())
        ledger['calls'][known]['day'] = '2000-01-01'
        budget.path.write_text(json.dumps(ledger))
        pending = budget.reserve('xiaomi/mimo-v2.6-pro', '.6')
        ledger = json.loads(budget.path.read_text())
        ledger['calls'][pending]['day'] = '2000-01-01'
        budget.path.write_text(json.dumps(ledger))
        with self.assertRaises(BudgetError):
            budget.reserve('xiaomi/mimo-v2.6-flash', '.6')
        budget.settle(pending, '.3')
        today = json.loads(budget.path.read_text())['calls'][pending]['day']
        self.assertNotEqual(today, '2000-01-01', 'reported spend must count on the settlement day')
        with self.assertRaises(BudgetError):
            budget.reserve('xiaomi/mimo-v2.6-flash', '.8')
        budget.reserve('xiaomi/mimo-v2.6-flash', '.6')

    # BR-MIMO-001
    def test_paid_lane_requires_1password_not_a_key_file(self):
        for lane in self.roster['lanes']:
            if lane.get('paid_mimo'):
                lane['auth'] = {'key_ref':'env:FAKE_KEY'}
        self.overlay.write_text(json.dumps(self.roster))
        result = self.run_wrapper('--paid-mimo')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(len(self.posts), 0)
        self.assertIn('1Password', result.stderr)

    # BR-MIMO-001
    def test_paid_lane_cannot_claim_repository_review(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/fleetctl.py'), '--overlay', str(self.overlay),
                                 '--state-dir', str(self.root/'selection'), 'select', '--role', 'review',
                                 '--allow', 'openrouter-mimo:mimo-v2.6-flash:*', '--no-refresh', '--json'],
                                env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('prompt-contained', result.stderr)
        self.assertEqual(len(self.posts), 0)

    # BR-MIMO-002
    def test_extra_fees_and_missing_prices_are_refused(self):
        entry = {'id':'xiaomi/mimo-v2.6-pro', 'architecture':{'output_modalities':['text']},
                 'context_length':100, 'top_provider':{'max_completion_tokens':100}}
        for prices in ({}, {'prompt':'0.1','completion':'0.1','web_search':'1'},
                       {'prompt':'NaN','completion':'0.1'}, {'prompt':'0.1','completion':'0.1','overrides':[{}]}):
            with self.subTest(prices=prices), self.assertRaises((BudgetError, ValueError)):
                priced_request(dict(entry, pricing=prices), {'model':entry['id']})

    # BR-MIMO-001
    def test_selector_paid_only_with_allow_and_dispatch_uses_opt_in(self):
        # A missing overlay hint must not turn paid MiMo into automatic fallback.
        self.roster['quota_pools']['openrouter-mimo'].pop('explicit_only')
        self.overlay.write_text(json.dumps(self.roster))
        common = [sys.executable, str(ROOT/'scripts/fleetctl.py'), '--overlay', str(self.overlay),
                  '--state-dir', str(self.root/'selection')]
        automatic = subprocess.run(common+['select','--role','hard-reasoning','--no-refresh','--json'],
                                   env=self.env, text=True, capture_output=True)
        self.assertEqual(automatic.returncode, 0, automatic.stderr)
        selection = json.loads(automatic.stdout)
        self.assertNotEqual(selection['choice']['pool'], 'openrouter-mimo')
        self.assertTrue(any('paid pool requires explicit' in x for x in selection['rejected']))
        explicit = subprocess.run(common+['dispatch','--role','hard-reasoning','--no-refresh',
                                          '--allow','openrouter-mimo:mimo-v2.6-flash:*',
                                          '--prompt','Return PONG','--dir',str(self.root),'--last',str(self.root/'dispatch-answer')],
                                  env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual((self.root/'dispatch-answer').read_text().strip(), 'PONG')
        self.assertEqual(self.posts[0]['model'], 'xiaomi/mimo-v2.6-flash')


if __name__ == '__main__':
    unittest.main()
