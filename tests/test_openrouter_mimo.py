"""Business-boundary tests for the owner-approved paid MiMo route, using real HTTP/wrapper."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
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
from tests.cash_fixtures import ACCOUNT, configure, envelope, seed, seed_reasoning


class MimoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mimo tests ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = json.loads((ROOT/'tests/fixtures/access-overlay.test.json').read_text())
        self.overlay = self.root/'.config/orchestrator/access-overlay.json'
        self.overlay.parent.mkdir(parents=True)
        self.ledger = self.root/'.local/state/orchestrator/cash-budget.json'
        seed(self.ledger)
        self.posts = []
        self.post_paths = []
        self.get_paths = []
        self.redirect_catalog = False
        self.redirect_post = False
        self.response_model = None
        self.models = ['xiaomi/mimo-v2.6-flash', 'xiaomi/mimo-v2.6-pro', 'openrouter/free', 'fixture/other-text']
        self.price = '0.001'
        self.cost = 0.001
        self.status = 200
        self.stall = False
        self.catalog_barrier = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.get_paths.append(self.path)
                if owner.redirect_catalog and self.path.endswith("/models"):
                    self.send_response(302)
                    self.send_header("Location", "/unapproved")
                    self.end_headers()
                    return
                if owner.catalog_barrier:
                    owner.catalog_barrier.wait(timeout=10)
                entries = []
                for model in owner.models:
                    entries.append({'id': model, 'architecture': {'output_modalities': ['text']},
                                    'pricing': {'prompt': owner.price, 'completion': owner.price},
                                    'context_length': 100, 'top_provider': {'max_completion_tokens': 100}})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'data': entries}).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.posts.append(body)
                owner.post_paths.append(self.path)
                if owner.redirect_post:
                    self.send_response(302)
                    self.send_header("Location", "/unapproved")
                    self.end_headers()
                    return
                self.send_response(owner.status)
                self.end_headers()
                if owner.stall:
                    threading.Event().wait(3)
                    return
                if owner.status != 200:
                    self.wfile.write(b'{"error":"synthetic failure"}')
                    return
                self.wfile.write(('data: '+json.dumps({'model': owner.response_model or body['model'], 'choices': [{'delta': {'content':'PONG'}}]})+'\n\n').encode())
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
        base = f'http://127.0.0.1:{self.server.server_port}'
        configure(self.roster, base + '/v1', automatic=True)
        seed_reasoning(self.root/'selection', "mimo-v2.6-flash", "mimo-v2.6-pro")
        seed_reasoning(self.root/'different-state', "fixture-other")
        self.overlay.write_text(json.dumps(self.roster))
        self.overlay.chmod(0o600)
        self.env = dict(os.environ, HOME=str(self.root), ACCESS_OVERLAY=str(self.overlay),
                        FLEET_STATE_DIR=str(self.root/'state'), OPENROUTER_API_BASE='', FLEET_DATA_CLASS='public',
                        OP_SERVICE_ACCOUNT_TOKEN='', OP_SERVICE_ACCOUNT_TOKEN_FILE=str(self.root/'no-token'),
                        PATH=str(bindir)+os.pathsep+os.environ['PATH'], FLEET_QUOTA_POLICY='off', FLEET_IGNORE_QUOTA='1')

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_wrapper(self, *args, lane='openrouter-mimo-v2.6-flash', env=None):
        return subprocess.run([str(ROOT/'scripts/openrouter-agent.sh'), '--lane', lane,
                               '--prompt', 'Return PONG', '--last', str(self.root/'answer'), *args],
                              env={**self.env, **(env or {})}, text=True, capture_output=True, timeout=20)

    def budget(self):
        return json.loads(self.ledger.read_text())['accounts'][ACCOUNT]['calls']

    def isolated_budget(self, name, **limits):
        path = self.root / name / 'cash-budget.json'
        seed(path)
        return Budget(ACCOUNT, envelope(**limits), path=path)

    def add_second_provider(self):
        template = next(lane for lane in self.roster["lanes"] if lane["lane_id"] == "openrouter-mimo-v2.6-flash")
        lane = copy.deepcopy(template)
        lane.pop("paid_mimo")
        lane.update(lane_id="fixture-other", model_key="fixture-other", provider="fixture",
                    selector="fixture/other-text", quota_pool="fixture-billing")
        lane["paid_route"].update(
            model=lane["selector"], provider="fixture-other-upstream",
            endpoint=f"http://127.0.0.1:{self.server.server_port}/other/v1")
        self.roster["quota_pools"]["fixture-billing"] = {"funding": "metered"}
        self.roster["effort"]["fixture-other"] = copy.deepcopy(self.roster["effort"][template["model_key"]])
        self.roster["lanes"].append(lane)
        self.roster["cash_accounts"][ACCOUNT].update(per_call_usd=".25", daily_usd=".25")
        self.overlay.write_text(json.dumps(self.roster))

    # BR-CASH-002
    def test_two_provider_processes_cannot_race_past_last_headroom(self):
        self.add_second_provider()
        self.catalog_barrier = threading.Barrier(2)
        self.cost = .15
        with ThreadPoolExecutor(max_workers=2) as workers:
            mimo = workers.submit(self.run_wrapper, "--paid-mimo")
            other = workers.submit(self.run_wrapper, "--paid", "--role", "hard-reasoning", lane="fixture-other",
                                   env={"FLEET_STATE_DIR": str(self.root / "alternate")})
            results = [job.result() for job in (mimo, other)]
        self.assertEqual(sorted(result.returncode for result in results), [0, 5],
                         [result.stderr for result in results])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.budget()), 1)

    # BR-CASH-002
    def test_second_provider_overlay_only_and_alternate_dispatch_state_share_cash(self):
        self.add_second_provider()
        self.cost = .15
        result = subprocess.run([sys.executable, str(ROOT/'scripts/fleetctl.py'),
                                 '--overlay', str(self.overlay), '--state-dir', str(self.root/'different-state'),
                                 'dispatch', '--role', 'hard-reasoning', '--no-refresh',
                                 '--allow', 'fixture-billing:fixture-other:*', '--prompt', 'Return PONG',
                                 '--dir', str(self.root), '--last', str(self.root/'third-party-answer')],
                                env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.post_paths, ["/other/v1/chat/completions"])
        self.assertEqual(self.posts[0]["provider"]["only"], ["fixture-other-upstream"])
        self.assertEqual(self.posts[0]["model"], "fixture/other-text")
        receipt = json.loads((self.root/'third-party-answer.crossfeed.json').read_text())
        self.assertEqual(receipt["cash"]["reported_usd"], .15)
        self.assertIn(receipt["cash"]["reservation_id"], self.budget())
        blocked = self.run_wrapper("--paid-mimo")
        self.assertEqual(blocked.returncode, 5, blocked.stderr)
        self.assertIn("cap", blocked.stderr)
        self.assertEqual(len(self.posts), 1)

    # BR-CASH-002
    def test_per_call_limit_and_duplicate_settlement(self):
        budget = self.isolated_budget("single", per_call_usd=".25")
        with self.assertRaisesRegex(BudgetError, "per-call"):
            budget.reserve("fixture/other-text", ".251")
        token = budget.reserve("fixture/other-text", ".25")
        budget.settle(token, ".2")
        with self.assertRaisesRegex(BudgetError, "outstanding"):
            budget.settle(token, "0")
        self.assertEqual(budget.snapshot()["settled_usd"], .2)
        self.assertEqual(budget.snapshot()["held_usd"], 0)
        self.assertEqual(budget.snapshot()["available_usd"], 4.8)

    # BR-CASH-003
    def test_direct_private_prompt_refuses_before_even_fetching_catalog(self):
        result = self.run_wrapper("--paid-mimo", env={"FLEET_DATA_CLASS": "private"})
        self.assertEqual(self.posts, [], result.stderr)
        self.assertEqual(self.get_paths, [])
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("private", result.stderr)

    # BR-CASH-003
    def test_dispatch_private_prompt_refuses_without_outbound_http(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/fleetctl.py'), '--overlay', str(self.overlay),
                                 '--state-dir', str(self.root/'private-selection'), 'dispatch', '--no-refresh',
                                 '--role', 'hard-reasoning', '--data-class', 'private',
                                 '--allow', 'openrouter-mimo:mimo-v2.6-flash:*', '--prompt', 'Return PONG',
                                 '--dir', str(self.root)], env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(self.posts, [], result.stderr)
        self.assertEqual(self.get_paths, [])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private", result.stderr)

    # BR-CASH-003
    def test_public_label_does_not_authorize_substituted_bytes(self):
        result = self.run_wrapper("--paid-mimo", "--prompt", "Unapproved content",
                                  env={"FLEET_DATA_CLASS": "public"})
        self.assertEqual(self.posts, [], result.stderr)
        self.assertEqual(self.get_paths, [])
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("approved", result.stderr)

    # BR-CASH-003
    def test_paid_http_redirects_do_not_reach_replacement_endpoint(self):
        self.redirect_catalog = True
        result = self.run_wrapper("--paid-mimo", env={"FLEET_DATA_CLASS": "public"})
        self.assertEqual(self.get_paths, ["/v1/models"], result.stderr)
        self.assertEqual(self.posts, [])
        self.assertNotEqual(result.returncode, 0)
        self.get_paths.clear()
        self.redirect_catalog, self.redirect_post = False, True
        result = self.run_wrapper("--paid-mimo", env={"FLEET_DATA_CLASS": "public"})
        self.assertEqual(self.get_paths, ["/v1/models"], result.stderr)
        self.assertEqual(self.post_paths, ["/v1/chat/completions"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(next(iter(self.budget().values()))["cost_usd"])

    # BR-CASH-003
    def test_legacy_endpoint_override_cannot_send_paid_prompt(self):
        result = self.run_wrapper("--paid-mimo", env={
            "OPENROUTER_API_BASE": f"http://127.0.0.1:{self.server.server_port}/unapproved",
            "FLEET_DATA_CLASS": "public",
        })
        self.assertEqual(self.posts, [], result.stderr)
        self.assertEqual(self.get_paths, [])
        self.assertIn("endpoint", result.stderr)

    # BR-CASH-003
    def test_unexpected_served_model_leaves_an_unsettled_hold(self):
        self.response_model = "fixture/other-text"
        result = self.run_wrapper("--paid-mimo", env={"FLEET_DATA_CLASS": "public"})
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertEqual(len(self.posts), 1)
        self.assertFalse((self.root/"answer").exists())
        self.assertIsNone(next(iter(self.budget().values()))["cost_usd"])

    # BR-CASH-004
    def test_revoked_automatic_selection_is_rechecked_at_send(self):
        self.roster["cash_accounts"][ACCOUNT]["automatic"] = False
        self.overlay.write_text(json.dumps(self.roster))
        stale = self.run_wrapper("--paid-mimo", env={"CROSSFEED_DISPATCH_ID": "stale"})
        self.assertEqual(stale.returncode, 5, stale.stderr)
        self.assertEqual(self.get_paths, [])
        self.assertEqual(self.posts, [])
        self.assertIn("no longer grants automatic", stale.stderr)
        control = self.run_wrapper("--paid-mimo")
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertEqual(len(self.posts), 1)

    # BR-CASH-002
    def test_explicit_paid_flag_does_not_authorize_spend_without_an_envelope(self):
        self.roster.pop("cash_accounts", None)
        self.overlay.write_text(json.dumps(self.roster))
        result = self.run_wrapper('--paid-mimo')
        self.assertEqual(self.posts, [])
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("envelope", result.stderr)

    # BR-CASH-002
    def test_missing_ledger_refuses_even_with_an_envelope(self):
        budget_path = self.root / '.local/state/orchestrator/cash-budget.json'
        budget_path.unlink(missing_ok=True)
        result = self.run_wrapper('--paid-mimo')
        self.assertEqual(self.posts, [])
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("ledger", result.stderr)
        self.assertFalse(budget_path.exists())

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
        self.assertEqual(self.posts[0]["messages"], [{"role": "user", "content": "Return PONG"}])
        self.assertNotIn("tools", self.posts[0])
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
        self.assertEqual(self.budget(), {})

    # BR-MIMO-002
    def test_pro_and_flash_share_one_cap(self):
        self.roster['cash_accounts'][ACCOUNT].update(per_call_usd="0.25", daily_usd="0.25")
        self.overlay.write_text(json.dumps(self.roster))
        self.cost = .15
        first = self.run_wrapper('--paid-mimo')
        second = self.run_wrapper('--paid-mimo', lane='openrouter-mimo-v2.6-pro')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 5, second.stderr)
        self.assertEqual(len(self.posts), 1)

    # BR-MIMO-002
    def test_missing_cost_keeps_reservation_and_refuses_followup(self):
        self.roster['cash_accounts'][ACCOUNT].update(per_call_usd="0.25", daily_usd="0.25")
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
        budget = self.isolated_budget("atomic", per_call_usd="1", daily_usd="1")
        def reserve(model):
            try:
                Budget(ACCOUNT, budget.settings, path=budget.path).reserve(model, Decimal('.6'))
                return True
            except BudgetError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ['xiaomi/mimo-v2.6-pro', 'xiaomi/mimo-v2.6-flash']))
        self.assertEqual(sum(results), 1)

    # BR-MIMO-002
    def test_bad_costs_and_corrupt_ledger_fail_closed(self):
        budget = self.isolated_budget('strict')
        token = budget.reserve('xiaomi/mimo-v2.6-flash', Decimal('.2'))
        for cost in (None, 'NaN', '-1', True):
            with self.subTest(cost=cost), self.assertRaises(BudgetError):
                budget.settle(token, cost)
        budget.path.write_text('{broken')
        with self.assertRaises(BudgetError):
            budget.reserve('xiaomi/mimo-v2.6-pro', Decimal('.2'))

    # BR-MIMO-002
    def test_billing_overrun_blocks_new_calls(self):
        budget = self.isolated_budget('overrun')
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
        budget = self.isolated_budget('days', per_call_usd="1", daily_usd="1")
        known = budget.reserve('xiaomi/mimo-v2.6-flash', '.6')
        budget.settle(known, '.6')
        ledger = json.loads(budget.path.read_text())
        ledger['accounts'][ACCOUNT]['calls'][known]['day'] = '2000-01-01'
        budget.path.write_text(json.dumps(ledger))
        pending = budget.reserve('xiaomi/mimo-v2.6-pro', '.6')
        ledger = json.loads(budget.path.read_text())
        ledger['accounts'][ACCOUNT]['calls'][pending]['day'] = '2000-01-01'
        budget.path.write_text(json.dumps(ledger))
        with self.assertRaises(BudgetError):
            budget.reserve('xiaomi/mimo-v2.6-flash', '.6')
        budget.settle(pending, '.3')
        today = json.loads(budget.path.read_text())['accounts'][ACCOUNT]['calls'][pending]['day']
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
