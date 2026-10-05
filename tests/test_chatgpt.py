"""Crossfeed Chat contract checks against a loopback fake gateway, never ChatGPT."""
import copy
import contextlib
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl
import chatgpt_runner as runner
import selector
from tests.test_selector import quality_row

KEY = "super-secret-chatgpt-test-key"


class HttpPairSocket(socket.socket):
    def setsockopt(self, level, option, value, *args):
        if level == socket.IPPROTO_TCP and option == socket.TCP_NODELAY:
            return  # TCP packet coalescing has no counterpart on a socket pair.
        return super().setsockopt(level, option, value, *args)


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, payload):
        data = json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        assert self.headers["Authorization"] == "Bearer " + KEY
        if self.path == "/v1/models":
            return self.reply(200, {"object": "list", "data": [{"id": selector, "object": "model", "saved": True, "row": "Latest", "level": getattr(self.server, "catalog_levels", {}).get(selector, 1)}
                | ({'replicas': self.server.catalog_replicas[selector]}
                   if selector in getattr(self.server, 'catalog_replicas', {}) else {})
                for selector in getattr(self.server, "catalog_ids", [])]})
        assert self.path == "/v1/gateway/status"
        leased_state = getattr(self.server, 'pro_pause_when_leased', None)
        if leased_state and leased_state.exists() and json.loads(leased_state.read_text()).get('leases'):
            self.server.quota_blocked = ['chatgpt:latest-pro']
        mode = self.server.mode
        if mode == "auth":
            return self.reply(401, {"error": KEY})
        if mode == "health-429":
            return self.reply(429, {"error": KEY})
        self.reply(200, {'workers': [{'label': selector.removeprefix('chatgpt:'),
            'contact': 'never' if mode == 'sleep' else 'stale' if mode == 'stale-active' else 'recent',
            'polling': mode not in {'sleep', 'stale-active'},
            'quota_blocked': selector in getattr(self.server, 'quota_blocked', []),
            'processing_claim': mode == 'busy'} for selector in self.server.catalog_ids]})

    def do_POST(self):
        assert self.path == "/v1/chat/completions"
        assert self.headers["Authorization"] == "Bearer " + KEY
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.payloads.append(body)
        assert body["model"] in self.server.catalog_ids and body["tool_choice"] == "none"
        assert self.headers["Idempotency-Key"]
        assert self.headers["X-Crossfeed-Session"] == "orchestrator:" + self.headers["Idempotency-Key"]
        if hasattr(self.server, "keys"):
            self.server.keys.append(self.headers["Idempotency-Key"])
        blocked = getattr(self.server, 'blocked_keys', {}).get(self.headers['Idempotency-Key'])
        if blocked is not None:
            blocked.wait(5)
        assert not body["stream"] and "tools" not in body and "reasoning_effort" not in body
        mode = getattr(self.server, 'mode_by_selector', {}).get(body['model'], self.server.mode)
        if mode in {'503', '504'}:
            return self.reply(int(mode), {'error': 'inactive or timed out'})
        if mode == "429":
            return self.reply(429, {"error": KEY})
        if mode == "hang":
            time.sleep(3)
        elif mode == "buffered":
            time.sleep(1.2)
        content = "" if mode == "empty" else KEY if mode == "secret" else "PONG"
        finish = "tool_calls" if mode == "nonterminal" else "stop"
        answer = {"model": "chatgpt", "usage": {"total_tokens": 0},
                  "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}]}
        answer['model'] = 'chatgpt:wrong-label' if mode == 'mismatched-model' else body['model']
        if mode == 'tool-payload':
            answer['choices'][0]['message']['tool_calls'] = [{'id': 'forbidden-call'}]
        if hasattr(self.server, 'picker_receipt'):
            answer['metadata'] = {'picker_receipt': self.server.picker_receipt}
        if mode == 'downgraded':
            answer['metadata'] = {'picker_receipt': {'row': 'Latest', 'level': 1, 'source': 'worker'}}
        if mode == 'rate-text':
            answer['choices'][0]['message']['content'] = "You've reached the Pro quota limit. Try again in 3 days."
        self.reply(200, answer)


class ChatGPTTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chatgpt test ")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.socketpair = os.environ.get("CHATGPT_TEST_SOCKETPAIR") == "1"
        self.pass_fds = ()
        if self.socketpair:
            # Explicit sandbox fixture: real HTTP parsing over connected local
            # sockets, with no bind/listen permission and no TCP coverage claim.
            self.gateway = SimpleNamespace(server_port=3210, mode="ok", payloads=[])
            sockets = []
            def provision():
                batch = []
                for _ in range(32):
                    client, server = socket.socketpair()
                    sockets.append((client, server))
                    batch.append(client.fileno())
                    threading.Thread(target=Gateway, args=(server, ("127.0.0.1", 0), self.gateway), daemon=True).start()
                self.pass_fds = tuple(batch)
                self.fds = iter(batch)
            self.provision = provision
            provision()
            def connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kwargs):
                peer = HttpPairSocket(fileno=os.dup(next(self.fds)))
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    peer.settimeout(timeout)
                return peer
            self.socket_connection = connect
            def close_sockets():
                for pair in sockets:
                    for peer in pair:
                        try:
                            peer.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        peer.close()
            self.addCleanup(close_sockets)
            (self.work / "sitecustomize.py").write_text('''import os,socket,subprocess
fds=iter(map(int,os.environ['CHATGPT_FIXTURE_FDS'].split(',')))
class PairSocket(socket.socket):
 def setsockopt(self,level,option,value,*args):
  if level==socket.IPPROTO_TCP and option==socket.TCP_NODELAY: return
  return super().setsockopt(level,option,value,*args)
def connect(address,timeout=socket._GLOBAL_DEFAULT_TIMEOUT,source_address=None,**kwargs):
 peer=PairSocket(fileno=os.dup(next(fds)))
 if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT: peer.settimeout(timeout)
 return peer
socket.create_connection=connect
# Canonical admission also reads /models in the quota-lease subprocess.
# Give each such child fresh connected sockets without changing production code.
original_popen=subprocess.Popen
def popen(argv,*args,**kwargs):
 if isinstance(argv,list) and any(str(part).endswith('/fleetctl.py') for part in argv):
  owned=[next(fds) for _ in range(2)]
  kwargs['env']=dict(kwargs.get('env') or os.environ,CHATGPT_FIXTURE_FDS=','.join(map(str,owned)))
  kwargs['pass_fds']=tuple(owned)
 return original_popen(argv,*args,**kwargs)
subprocess.Popen=popen
''')
        else:
            self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
            self.gateway.daemon_threads = True
            self.gateway.mode, self.gateway.payloads = "ok", []
            threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
            self.addCleanup(self.gateway.server_close)
            self.addCleanup(self.gateway.shutdown)
        (self.work / "key").write_text(KEY)
        example = json.loads((ROOT / "examples/access-overlay.example.json").read_text())
        self.roster = copy.deepcopy(example)
        self.lane = self.roster['chatgpt_gateway']['lane_template']
        self.lane.update(lane_id='chatgpt:fixture-medium', model_key='chatgpt:fixture-medium',
                         selector='chatgpt:fixture-medium', harness='chatgpt-chat',
                         worker_label='fixture-medium', worker_level='medium',
                         access_status='verified', admission_status='active')
        self.lane['transport']['api_base'] = f'http://127.0.0.1:{self.gateway.server_port}/v1'
        self.lane['auth']['key_file'] = str(self.work / 'key')
        self.gateway.selector = 'chatgpt:fixture-medium'
        self.gateway.catalog_ids = [self.gateway.selector]
        self.gateway.keys = []
        self.roster['lanes'] = []
        self.roster["policy"]["selector"]["allowed_pools_by_role"] = {"default": ["chatgpt-work"]}
        self.overlay = self.work / "overlay.json"
        self.save()
        self.env = dict(os.environ, ACCESS_OVERLAY=str(self.overlay), FLEET_STATE_DIR=str(self.work / "state"),
                        FLEET_QUOTA_AUTO_REFRESH="0", FLEET_QUOTA_POLICY="off", FLEET_SELECTION_FILE="")
        if self.socketpair:
            self.env.update(PYTHONPATH=str(self.work), CHATGPT_FIXTURE_FDS=",".join(map(str, self.pass_fds)))
        (self.work / "prompt").write_text("Say PONG")

    def save(self):
        self.overlay.write_text(json.dumps(self.roster))

    def command(self, *extra):
        if self.socketpair:
            self.provision()
            self.env["CHATGPT_FIXTURE_FDS"] = ",".join(map(str, self.pass_fds))
        return [str(ROOT / "scripts/chatgpt-agent.sh"), "run", "--lane", self.lane["lane_id"],
                "--prompt-file", str(self.work / "prompt"), "--dir", str(self.work),
                "--events", str(self.work / "events"), "--last", str(self.work / "last"), *extra]

    def run_adapter(self, mode="ok", extra=()):
        self.gateway.mode = mode
        result = subprocess.run(self.command(*extra), env=self.env, pass_fds=self.pass_fds,
                                text=True, capture_output=True, timeout=8)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        for name in ("events", "last", "last.crossfeed.json"):
            if (self.work / name).exists():
                self.assertNotIn(KEY, (self.work / name).read_text())
        self.assertEqual(json.loads((self.work / "state/runtime.json").read_text()).get("leases", []), []) if (self.work / "state/runtime.json").exists() else None
        return result

    def test_success_and_unconfirmed_identity_unknown_usage(self):
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "PONG\n")
        record = json.loads((self.work / "last.crossfeed.json").read_text())
        self.assertEqual(record["configured_model"], "chatgpt:fixture-medium")
        self.assertEqual(record["effort"], "medium")
        self.assertIsNone(record["native_effort"])
        self.assertIsNone(record["actual_model"])
        self.assertEqual(record["identity_source"], "unconfirmed")
        self.assertEqual(record["usage_source"], "unavailable")
        self.assertNotIn("tokens", record)
        self.assertIn("underlying model unconfirmed", result.stderr)

    def configure_catalog(self):
        self.gateway.catalog_ids = [self.gateway.selector]
        self.gateway.keys = []
        self.save()

    def test_saved_selector_and_idempotency_key_preserve_request_identity(self):
        self.configure_catalog()
        result = self.run_adapter(extra=('--idempotency-key', 'stable-test-request'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.gateway.payloads[0]['model'], self.gateway.selector)
        self.assertEqual(self.gateway.keys, ['stable-test-request'])
        receipt = json.loads((self.work / 'last.crossfeed.json').read_text())
        self.assertEqual(receipt['configured_model'], self.gateway.selector)
        self.assertEqual(receipt['idempotency_key'], 'stable-test-request')
        self.assertIsNone(receipt['actual_model'])
        self.assertNotIn('observed_picker', receipt)
        first = copy.deepcopy(self.gateway.payloads[0])
        result = self.run_adapter(extra=('--idempotency-key', 'stable-test-request'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.gateway.keys, ['stable-test-request'] * 2)
        self.assertEqual(self.gateway.payloads[1], first)

    def test_response_for_different_saved_label_is_refused(self):
        result = self.run_adapter('mismatched-model')
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertFalse((self.work / 'last').exists())

    def test_tool_payload_is_refused_without_writing_answer(self):
        result = self.run_adapter('tool-payload')
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertFalse((self.work / 'last').exists())

    def test_catalog_default_key_is_persisted_and_missing_id_never_dispatched(self):
        self.configure_catalog()
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((self.work / "last.crossfeed.json").read_text())
        self.assertEqual(self.gateway.keys, [receipt["run_id"]])
        self.gateway.catalog_ids = []
        result = self.run_adapter()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(len(self.gateway.payloads), 1)

    def test_sleeping_saved_worker_posts_without_local_browser_wake(self):
        for mode in ('sleep', 'stale-active'):
            with self.subTest(mode=mode):
                result = self.run_adapter(mode, extra=('--idempotency-key', 'sleeping-fixture'))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'PONG\n')
                self.assertEqual(self.gateway.payloads[-1]['model'], self.gateway.selector)
                self.assertEqual(self.gateway.keys[-1], 'sleeping-fixture')
        receipt = json.loads((self.work / 'last.crossfeed.json').read_text())
        self.assertEqual(receipt['effort'], 'medium')
        self.assertIsNone(receipt['actual_model'])
        self.assertEqual(list(self.work.glob('wake-*')), [])

    def test_gateway_503_and_504_are_clean_runtime_failures(self):
        for mode in ('503', '504'):
            result = self.run_adapter(mode)
            self.assertEqual(result.returncode, 6, result.stderr)
            self.assertIn('gateway HTTP ' + mode, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(list(self.work.glob('wake-*')), [])

    def test_429_lease_and_auth(self):
        for mode, code in [("429", 4), ("health-429", 4), ("auth", 5)]:
            with self.subTest(mode=mode):
                result = self.run_adapter(mode)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_empty_and_missing_terminal(self):
        for mode, code in [("empty", 7), ("nonterminal", 6)]:
            with self.subTest(mode=mode):
                result = self.run_adapter(mode)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads((self.work / "last.crossfeed.json").read_text())["returncode"], code)

    def test_secret_redacted_from_final_and_artifacts(self):
        result = self.run_adapter("secret")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "[REDACTED]\n")

    def test_default_silence_reports_without_killing(self):
        self.env['CROSSFEED_TEST_SILENCE_INTERVAL_S'] = '1'
        result = self.run_adapter('buffered')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('silent for ', result.stderr)
        self.assertNotIn('LIMIT FIRED', result.stderr)

    def test_buffered_default_and_timeouts(self):
        self.assertEqual(self.run_adapter("buffered").returncode, 0)
        for flag, code in [("--wall", 124), ("--idle", 125)]:
            result = self.run_adapter("hang", (flag, "1"))
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual(result.stdout, "")

    def test_mode_effort_and_terms_refusal_before_request(self):
        for extra in [("--mode", "rw"), ("--effort", "xhigh"), ("--modality", "image")]:
            self.assertEqual(self.run_adapter(extra=extra).returncode, 3)
        self.lane["auth"]["terms_class"] = "first-party"
        self.save()
        self.assertEqual(self.run_adapter().returncode, 5)
        self.assertEqual(self.gateway.payloads, [])

    def hold_lease(self):
        self.lane["max_parallel"] = 1
        self.save()
        # A live lease held by this test must block the adapter's acquisition.
        runtime = self.work / "state/runtime.json"
        runtime.parent.mkdir(exist_ok=True)
        runtime.write_text(json.dumps({"leases": [{"lane_id": self.lane["lane_id"], "pool": "chatgpt-work",
            "token": "held", "pid": os.getpid(), "expires_at": fleetctl.iso(fleetctl.utc_now() + fleetctl.dt.timedelta(seconds=60))}]}))
    def start_adapter(self, name, *extra):
        child = subprocess.Popen(self.command('--events', str(self.work / (name + '.events')),
                                 '--last', str(self.work / (name + '.last')),
                                 '--idempotency-key', name, *extra), env=self.env, pass_fds=self.pass_fds,
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def stop():
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=5)
        self.addCleanup(stop)
        return child

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(condition())

    def tickets(self):
        return list((self.work / 'state/chatgpt-queue').glob('*/*.ticket'))

    def test_two_replicas_run_concurrently_and_decrease_limits_waiting_caller(self):
        self.gateway.catalog_replicas = {self.gateway.selector: 2}
        first_release, second_release = threading.Event(), threading.Event()
        self.addCleanup(first_release.set)
        self.addCleanup(second_release.set)
        self.gateway.blocked_keys = {'first': first_release, 'second': second_release}
        first = self.start_adapter('first', '--wall', '10')
        self.wait_for(lambda: self.gateway.keys == ['first'])
        second = self.start_adapter('second', '--wall', '10')
        self.wait_for(lambda: self.gateway.keys == ['first', 'second'])
        runtime = self.work / 'state/runtime.json'
        self.assertEqual(len(json.loads(runtime.read_text())['leases']), 2)
        third = self.start_adapter('third', '--wall', '10')
        self.wait_for(lambda: len(self.tickets()) == 1)
        time.sleep(.2)
        self.assertEqual(self.gateway.keys, ['first', 'second'])
        # A decrease is authoritative for queued callers, without interrupting
        # the two requests that already own leases.
        self.gateway.catalog_replicas[self.gateway.selector] = 1
        first_release.set()
        out, err = first.communicate(timeout=4)
        self.assertEqual((first.returncode, out), (0, 'PONG\n'), err)
        time.sleep(.3)
        self.assertEqual(self.gateway.keys, ['first', 'second'])
        self.assertEqual(len(json.loads(runtime.read_text())['leases']), 1)
        second_release.set()
        for child in (second, third):
            out, err = child.communicate(timeout=5)
            self.assertEqual((child.returncode, out), (0, 'PONG\n'), err)
        self.assertEqual(self.gateway.keys, ['first', 'second', 'third'])
        self.assertEqual(json.loads(runtime.read_text())['leases'], [])
        self.assertEqual(self.tickets(), [])

    def test_waiting_caller_discovers_replica_increase_without_restart(self):
        self.hold_lease()
        caller = self.start_adapter('waiting', '--wall', '6')
        self.wait_for(lambda: len(self.tickets()) == 1)
        time.sleep(.2)
        self.assertEqual(self.gateway.keys, [])
        self.gateway.catalog_replicas = {self.gateway.selector: 2}
        out, err = caller.communicate(timeout=4)
        self.assertEqual((caller.returncode, out), (0, 'PONG\n'), err)
        self.assertEqual(self.gateway.keys, ['waiting'])
        self.assertEqual([lease['token'] for lease in json.loads(
            (self.work / 'state/runtime.json').read_text())['leases']], ['held'])
        fleetctl.release_lease(self.work / 'state', 'held')
        self.assertEqual(self.tickets(), [])

    def test_local_quota_lease_wait_is_bounded(self):
        self.hold_lease()
        started = time.monotonic()
        result = subprocess.run(self.command('--wall', '1'), env=self.env, pass_fds=self.pass_fds,
                                text=True, capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.tickets(), [])
        self.assertEqual(self.gateway.payloads, [])

    def test_concurrent_callers_run_in_arrival_order_and_other_label_is_independent(self):
        self.hold_lease()
        release = threading.Event()
        self.addCleanup(release.set)
        self.gateway.blocked_keys = {'first': release}
        first = self.start_adapter('first', '--wall', '6')
        self.wait_for(lambda: len(self.tickets()) == 1)
        second = self.start_adapter('second', '--wall', '6')
        self.wait_for(lambda: len(self.tickets()) == 2)
        third = self.start_adapter('third', '--wall', '6')
        self.wait_for(lambda: len(self.tickets()) == 3)
        self.gateway.catalog_ids.append('chatgpt:other-medium')
        other = self.start_adapter('other', '--lane', 'chatgpt:other-medium', '--wall', '3')
        out, err = other.communicate(timeout=4)
        self.assertEqual((other.returncode, out), (0, 'PONG\n'), err)
        self.assertEqual(self.gateway.keys, ['other'])
        fleetctl.release_lease(self.work / 'state', 'held')
        self.wait_for(lambda: self.gateway.keys == ['other', 'first'])
        time.sleep(.15)
        self.assertEqual(self.gateway.keys, ['other', 'first'])
        release.set()
        for child in (first, second, third):
            out, err = child.communicate(timeout=6)
            self.assertEqual((child.returncode, out), (0, 'PONG\n'), err)
        self.assertEqual(self.gateway.keys, ['other', 'first', 'second', 'third'])
        self.assertEqual(self.tickets(), [])
        self.assertEqual(json.loads((self.work / 'state/runtime.json').read_text())['leases'], [])

    def test_sigterm_while_queued_removes_place_and_preserves_held_lease(self):
        self.hold_lease()
        first = self.start_adapter('first', '--wall', '6')
        self.wait_for(lambda: len(self.tickets()) == 1)
        cancelled = self.start_adapter('cancelled', '--wall', '6')
        self.wait_for(lambda: len(self.tickets()) == 2)
        with (self.tickets()[0].parent / 'lock').open('a') as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            cancelled.send_signal(signal.SIGTERM)
            out, err = cancelled.communicate(timeout=2)
        self.assertEqual((cancelled.returncode, out), (143, ''), err)
        self.assertEqual(len(self.tickets()), 1)
        self.assertEqual(json.loads((self.work / 'state/runtime.json').read_text())['leases'][0]['token'], 'held')
        fleetctl.release_lease(self.work / 'state', 'held')
        out, err = first.communicate(timeout=5)
        self.assertEqual((first.returncode, out), (0, 'PONG\n'), err)
        self.assertEqual(self.gateway.keys, ['first'])
        self.assertEqual(self.tickets(), [])

    def test_killed_waiter_does_not_block_successors(self):
        self.hold_lease()
        killed = self.start_adapter('killed', '--wall', '6')
        self.wait_for(lambda: len(self.tickets()) == 1)
        killed.kill()
        killed.communicate(timeout=2)
        fleetctl.release_lease(self.work / 'state', 'held')
        successor = self.start_adapter('successor', '--wall', '3')
        out, err = successor.communicate(timeout=4)
        self.assertEqual((successor.returncode, out), (0, 'PONG\n'), err)
        self.assertEqual(self.tickets(), [])

    def test_wait_time_is_not_added_to_http_wall_budget(self):
        self.hold_lease()
        self.gateway.mode = 'hang'
        real_now = time.monotonic
        clock = [0.0]
        context = runner.multiprocessing.get_context('fork')
        original_turn = runner.lane_turn
        original_popen = subprocess.Popen
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}

        @contextlib.contextmanager
        def waited_turn(lane, check):
            with original_turn(lane, check) as release:
                # Consume 600 ms of the production deadline without depending
                # on interpreter/admission speed or a scheduled timer callback.
                clock[0] = .6
                fleetctl.release_lease(self.work / 'state', 'held')
                yield release

        def pipe(*args, **kwargs):
            receiver, sender = context.Pipe(*args, **kwargs)
            def poll(timeout):
                ready = receiver.poll(0)
                # The HTTP-start barrier below establishes an actual POST
                # before the controlled clock consumes its remaining 400 ms.
                clock[0] = round(clock[0] + .1, 1)
                return ready
            return SimpleNamespace(poll=poll, recv=receiver.recv, close=receiver.close), sender

        def process(*args, **kwargs):
            self.assertEqual(options.wall_deadline, 1)
            child = context.Process(*args, **kwargs)
            start = child.start
            def started():
                start()
                until = real_now() + 1
                while not self.gateway.payloads and real_now() < until:
                    time.sleep(.001)
                if len(self.gateway.payloads) != 1:
                    child.terminate()
                    child.join(options.kill_after)
                self.assertEqual(len(self.gateway.payloads), 1, 'real HTTP child must reach the loopback gateway')
                if self.socketpair:
                    # Fork copies the fixture iterator. Reserve the socket the
                    # child used so lease cleanup gets a fresh connection.
                    next(self.fds)
            child.start = started
            return child

        def popen(argv, *args, **kwargs):
            if self.socketpair and isinstance(argv, list) and any(str(part).endswith('/fleetctl.py') for part in argv):
                owned = [next(self.fds) for _ in range(2)]
                kwargs['env'] = dict(os.environ, CHATGPT_FIXTURE_FDS=','.join(map(str, owned)))
                kwargs['pass_fds'] = tuple(owned)
            return original_popen(argv, *args, **kwargs)

        with patch.object(sys, 'argv', self.command('--wall', '1')):
            options = runner.arguments()
        started = real_now()
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, self.env))
                # Keep native multiprocessing/socket poll clocks real.
                stack.enter_context(patch.object(runner, 'time',
                                                SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep)))
                stack.enter_context(patch.object(runner, 'lane_turn', waited_turn))
                stack.enter_context(patch.object(runner.multiprocessing, 'get_context',
                                                return_value=SimpleNamespace(Pipe=pipe, Process=process)))
                stack.enter_context(patch.object(subprocess, 'Popen', popen))
                if self.socketpair:
                    stack.enter_context(patch.object(socket, 'create_connection', self.socket_connection))
                with contextlib.redirect_stderr(io.StringIO()):
                    status = runner.run(options)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        self.assertEqual(status, 124)
        self.assertEqual(len(self.gateway.payloads), 1)
        self.assertEqual(clock[0], 1, 'queue time must leave only 400 ms for HTTP')
        self.assertLess(real_now() - started, 1.5)
        self.assertEqual(self.tickets(), [])
        self.assertEqual(json.loads((self.work / 'state/runtime.json').read_text())['leases'], [])

    def test_stuck_fleet_lock_is_bounded_and_cancellable(self):
        state = self.work / 'state'
        state.mkdir()
        with (state / 'runtime.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            bounded = self.start_adapter('bounded', '--wall', '1')
            out, err = bounded.communicate(timeout=2)
            self.assertEqual((bounded.returncode, out), (124, ''), err)
            cancelled = self.start_adapter('cancelled')
            self.wait_for(lambda: len(self.tickets()) == 1)
            cancelled.send_signal(signal.SIGTERM)
            out, err = cancelled.communicate(timeout=2)
            self.assertEqual((cancelled.returncode, out), (143, ''), err)
        self.assertEqual(self.tickets(), [])
        self.assertEqual(self.gateway.payloads, [])

    def test_wait_progress_is_emitted_every_60_seconds(self):
        now = [0]
        error = io.StringIO()
        @contextlib.contextmanager
        def wait(lane, check):
            for elapsed in (0, 59, 60, 61, 119, 120):
                now[0] = elapsed
                check()
            yield
        args = SimpleNamespace(action='run', wall=0, lane=self.lane['lane_id'])
        with patch.object(runner, 'lane_for', return_value=self.lane), \
             patch.object(runner, 'lane_turn', side_effect=wait), \
             patch.object(runner, 'run_turn', return_value=0), \
             patch.object(runner.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(runner.signal, 'signal'), contextlib.redirect_stderr(error):
            self.assertEqual(runner.run(args), 0)
        self.assertEqual(error.getvalue().splitlines(), ['chatgpt-agent: waiting for lane ' + self.lane['lane_id']] * 2)

    def test_lease_rejects_reparented_caller_without_reserving_slot(self):
        state = self.work / 'lease-state'
        state.mkdir()
        (state / 'runtime.json').write_text(json.dumps({'leases': []}))
        lane = dict(self.lane, quota_pool='fixture')
        roster = dict(lanes=[lane], quota_pools={'fixture': {}}, models={})
        with patch.object(fleetctl.os, 'getppid', return_value=1):
            with self.assertRaisesRegex(fleetctl.FleetError, 'lease caller exited'):
                fleetctl.acquire_lease(state, roster, lane['lane_id'], 60, pid=os.getpid())
        self.assertEqual(json.loads((state / 'runtime.json').read_text())['leases'], [])

    def test_switched_off_quota_is_refused_without_waiting(self):
        state = self.work / 'state'
        state.mkdir()
        (state / 'runtime.json').write_text(json.dumps({'leases': [], 'switches': {'chatgpt-work': 'off'}}))
        result = self.run_adapter(extra=('--wall', '5'))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(self.gateway.payloads, [])
        self.assertEqual(self.tickets(), [])

    def test_cancellation_records_failure_and_releases_lease(self):
        self.gateway.mode = "hang"
        child = subprocess.Popen(self.command(), env=self.env, pass_fds=self.pass_fds,
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.poll() is None and child.terminate())
        deadline = time.monotonic() + 4
        while not self.gateway.payloads and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.gateway.payloads)
        child.send_signal(signal.SIGTERM)
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 143, err)
        self.assertEqual(out, "")
        self.assertEqual(json.loads((self.work / "state/runtime.json").read_text())["leases"], [])
        self.assertEqual(json.loads((self.work / "last.crossfeed.json").read_text())["returncode"], 143)

    def test_cancellation_during_accounting_warns_and_preserves_completed_result(self):
        state = self.work / "state"
        state.mkdir()
        with (state / "runs.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            child = subprocess.Popen(self.command(), env=self.env, pass_fds=self.pass_fds,
                                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 4
            while not (self.work / "last").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            child.send_signal(signal.SIGTERM)
            fcntl.flock(lock, fcntl.LOCK_UN)
            out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(out, "PONG\n")
        self.assertIn("cancellation arrived during accounting", err)
        self.assertEqual(json.loads((self.work / "last.crossfeed.json").read_text())["returncode"], 0)

    def test_selector_high_and_irreversible_preserve_saved_label_for_lazy_wake(self):
        state = self.work / "selector-state"
        (state / "evidence").mkdir(parents=True)
        (state / "evidence/levels.json").write_text(json.dumps({"rows": [quality_row("chatgpt:fixture-medium", level="medium")]}))
        transport = patch("socket.create_connection", self.socket_connection) if self.socketpair else contextlib.nullcontext()
        with patch.dict(os.environ, self.env), patch.object(fleetctl, "_POLICY_CACHE", ("off", "test")), transport:
            for stakes in ["high", "irreversible"]:
                with self.subTest(stakes=stakes):
                    choice = selector.select_option(fleetctl.read_overlay(self.overlay, state), {}, state, "review", stakes=stakes, fleet=fleetctl)["choice"]
                    self.assertEqual(choice["harness"], "chatgpt-chat")
                    self.assertIn("--lane", choice["command_argv"])
                    self.assertNotIn("--effort", choice["command_argv"])
                    self.gateway.mode = "sleep"
                    if self.socketpair:
                        self.provision()
                    sleeping = selector.select_option(fleetctl.read_overlay(self.overlay, state), {}, state,
                                                       "review", stakes=stakes, fleet=fleetctl)['choice']
                    self.assertEqual(sleeping['lane_id'], self.lane['lane_id'])
                    self.assertEqual(sleeping['harness'], 'chatgpt-chat')
                    self.assertEqual(self.gateway.payloads, [])
                    self.gateway.mode = "ok"
                    if self.socketpair:
                        self.provision()


class ChatGPTGuardTests(unittest.TestCase):
    def test_group_escalation_and_idle_guards_detect_sabotage(self):
        with tempfile.TemporaryDirectory(prefix="chatgpt guards ") as directory:
            mirror = Path(directory) / "scripts"
            mirror.mkdir()
            for source in (ROOT / "scripts").iterdir():
                if source.is_file() and source.name != "chatgpt_runner.py":
                    (mirror / source.name).symlink_to(source)
            runner = mirror / "chatgpt_runner.py"
            original = (ROOT / "scripts/chatgpt_runner.py").read_text()
            runner.write_text(original)
            def scan():
                return subprocess.run(["bash", str(ROOT / "scripts/check-dispatch-invariants.sh"),
                                       str(mirror), str(ROOT / "skill/SKILL.md")], capture_output=True,
                                      text=True, timeout=30, env=dict(os.environ,
                                      ACCESS_OVERLAY=str(ROOT / "tests/fixtures/access-overlay.test.json"),
                                      FLEET_STATE_DIR=str(Path(directory) / "state"), FLEET_NO_AUTO_REFRESH="1"))
            self.assertEqual(scan().returncode, 0)
            for old in ("os.killpg(child.pid, signal.SIGKILL)", "args.idle and now - progress >= args.idle"):
                self.assertIn(old, original)
                runner.write_text(original.replace(old, "False"))
                self.assertNotEqual(scan().returncode, 0)
                runner.write_text(original)
                self.assertEqual(scan().returncode, 0)


if __name__ == "__main__":
    unittest.main()
