"""Authenticated HTTP round trips through real selection/dispatch and fake wrappers."""
import copy
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl
import lanes_api
import selector
from tests.test_switch_respected import Sandbox, FAKE_CODEX


WORKER = '''import json, os, pathlib, sys
import fleetctl
args = sys.argv[1:]
assert 'CROSSFEED_API_KEY' not in os.environ
selection = json.loads(pathlib.Path(os.environ['FLEET_SELECTION_FILE']).read_text())
choice = selection['choice']
assert choice['mode'] == 'read-only'
if choice['harness'] == 'codex':
    assert args[args.index('--sandbox') + 1] == 'read-only'
elif choice['harness'] == 'claude':
    assert '--read-only' in args
elif choice['harness'] == 'opencode':
    assert '--read-only' in args and '--write' not in args
state = pathlib.Path(os.environ['FLEET_STATE_DIR'])
prompt = args[args.index('--prompt') + 1]
with (state / 'calls.jsonl').open('a') as f:
    f.write(json.dumps({'choice': choice, 'args': args, 'prompt': prompt, 'cwd': os.getcwd()}) + '\\n')
if (state / 'fail').exists():
    print('private failed partial answer')
    sys.exit(4)
token = fleetctl.acquire_pool_slot(state, choice['pool'], os.getpid(), 60)
last = pathlib.Path(args[args.index('--last') + 1])
record = {'schema': 'crossfeed-model-run/v1', 'returncode': 0,
          'dispatch_id': os.environ['CROSSFEED_DISPATCH_ID'],
          'selected_model': choice['model_key'], 'selector': choice['run_as'],
          'actual_model': None, 'identity_source': 'unconfirmed'}
last.with_name(last.name + '.crossfeed.json').write_text(json.dumps(record))
last.write_text('fake answer é\\n')
print('wrapper stdout differs from final answer')
if token: fleetctl.release_lease(state, token)
'''


class APIUnitTests(unittest.TestCase):
    def test_cli_startup_errors_are_clean(self):
        env = dict(os.environ, CROSSFEED_API_KEY="")
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text(secrets.token_urlsafe(32))
            key.chmod(0o600)
            cases = [[], ["--key-file", str(key), "--dir", str(Path(tmp) / "missing")],
                     ["--key-file", str(key), "--port", "65536"]]
            for extra in cases:
                result = subprocess.run([sys.executable, str(ROOT / "scripts/fleetctl.py"), "serve-api", *extra],
                                        capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_key_file_requires_exact_permissions_and_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key"
            key = secrets.token_urlsafe(32)
            path.write_text(key + "\n")
            path.chmod(0o600)
            self.assertTrue(lanes_api.load_key(path) == key)
            path.chmod(0o640)
            with self.assertRaises(fleetctl.FleetError):
                lanes_api.load_key(path)
            path.chmod(0o600)
            with mock.patch.object(lanes_api.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(fleetctl.FleetError):
                    lanes_api.load_key(path)
            link = Path(tmp) / "link"
            link.symlink_to(path)
            with self.assertRaises(fleetctl.FleetError):
                lanes_api.load_key(link)

    def test_key_environment_is_required_and_not_echoed_on_failure(self):
        for value in ("", "space key", "line\nbreak", "é", "a" * 8193):
            with mock.patch.dict(os.environ, {"CROSSFEED_API_KEY": value}):
                with self.assertRaises(fleetctl.FleetError) as error:
                    lanes_api.load_key(None)
                if value:
                    self.assertNotIn(value, str(error.exception))

    def test_text_message_parts_preserve_roles_and_literal_content(self):
        literal = '$(touch forbidden) `command` "quotes"\nsecond line'
        _, prompt, streaming = lanes_api.parse_request({"model": "crossfeed:auto", "messages": [
            {"role": "system", "content": "Reply briefly"},
            {"role": "user", "content": [{"type": "text", "text": literal}]}]})
        messages = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(messages[1]["content"], literal)
        self.assertEqual(messages[0]["role"], "system")
        self.assertFalse(streaming)

    def test_target_filter_supports_gateway_keys_with_colons(self):
        roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        options, _ = selector.enumerate_options(roster, {}, "review", mode="read-only", fleet=fleetctl)
        option = copy.deepcopy(next(o for o in options if o["harness"] == "codex"))
        option["model_key"] = "chatgpt:worker-label"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(selector, "enumerate_options", return_value=([option], [])):
            result = selector.select_option(roster, {}, Path(tmp), "review", mode="read-only",
                                            target=("codex", "chatgpt:worker-label"), fleet=fleetctl)
            self.assertEqual(result["choice"]["model_key"], "chatgpt:worker-label")
            with self.assertRaises(fleetctl.FleetError):
                selector.select_option(roster, {}, Path(tmp), "review", target=("claude", "missing"), fleet=fleetctl)
        self.assertEqual(lanes_api.model_id({"harness": "chatgpt-chat", "model_key": "chatgpt:worker-label"}),
                         "chatgpt:worker-label")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lanes API ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        scripts = self.root / "scripts"
        scripts.mkdir()
        for source in (ROOT / "scripts").glob("*.py"):
            shutil.copyfile(source, scripts / source.name)
        for name in ("codex", "claude", "opencode"):
            wrapper = scripts / (name + "-agent.sh")
            wrapper.write_text("#!" + sys.executable + "\n" + WORKER)
            wrapper.chmod(0o700)
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": ["codex", "claude", "opencode-go"]},
                                             "explore": 0}
        self.overlay = self.root / "overlay.json"
        self.overlay.write_text(json.dumps(self.roster))
        self.state = self.root / "state"
        self.state.mkdir()
        self.key = secrets.token_urlsafe(32)
        env = dict(os.environ, CROSSFEED_API_KEY=self.key, FLEET_NO_AUTO_REFRESH="1", FLEET_QUOTA_POLICY="clock_aware",
                   FLEET_IGNORE_QUOTA="0")
        # Model a standalone server, rather than the test runner's agent harness.
        env = {key: value for key, value in env.items()
               if not key.startswith(("CODEX_", "CLAUDE_CODE_", "PI_")) and key != "CLAUDECODE"}
        self.diagnostics = (self.root / "server.log").open("w+")
        self.addCleanup(self.diagnostics.close)
        self.process = subprocess.Popen([sys.executable, str(scripts / "fleetctl.py"),
                                         "--overlay", str(self.overlay), "--state-dir", str(self.state),
                                         "serve-api", "--port", "0", "--dir", str(self.root)],
                                        env=env, stdout=subprocess.PIPE, stderr=self.diagnostics, text=True)
        self.addCleanup(self.stop)
        line = self.process.stdout.readline()
        self.assertIn("127.0.0.1:", line)
        self.port = int(line.split("127.0.0.1:")[1].split("/")[0])

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=10)
        self.process.stdout.close()

    def request(self, path="/v1/chat/completions", body=None, auth=True, raw=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.key
        headers.update(extra_headers or {})
        try:
            connection.request("POST" if body is not None or raw is not None else "GET", path,
                               body=raw if raw is not None else json.dumps(body) if body is not None else None,
                               headers=headers)
            response = connection.getresponse()
            payload = response.read().decode()
            return response.status, payload, response.getheader("Content-Type")
        finally:
            connection.close()

    def body(self, model="codex:gpt-6.1-sol", **extra):
        return {"model": model, "messages": [{"role": "user", "content": "Explain this in text"}], **extra}

    def calls(self):
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_catalog_uses_read_only_admission_and_auto(self):
        status, payload, _ = self.request("/v1/models")
        self.assertEqual(status, 200)
        models = json.loads(payload)
        ids = {model["id"] for model in models["data"]}
        self.assertEqual(models["object"], "list")
        self.assertIn("crossfeed:auto", ids)
        self.assertIn("codex:gpt-6.1-sol", ids)
        self.assertTrue(any(key.startswith("opencode:") for key in ids))
        self.assertFalse(any(key.startswith("agy:") for key in ids))
        self.assertEqual(self.calls(), [])

    def test_auth_required_on_both_endpoints(self):
        for path, body in (("/v1/models", None), ("/v1/chat/completions", self.body())):
            status, payload, _ = self.request(path, body, auth=False)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(payload)["error"]["code"], "invalid_api_key")
        self.assertEqual(self.calls(), [])

    def test_pinned_completion_has_receipts_and_no_invented_model_or_usage(self):
        status, payload, _ = self.request(body=self.body())
        self.assertEqual(status, 200, payload)
        completion = json.loads(payload)
        self.assertEqual(completion["object"], "chat.completion")
        self.assertEqual(completion["model"], "codex:gpt-6.1-sol")
        self.assertEqual(completion["choices"][0]["message"]["content"], "fake answer é\n")
        self.assertEqual(completion["choices"][0]["finish_reason"], "stop")
        self.assertIsNone(completion["crossfeed"]["identity"]["actual_model"])
        self.assertNotIn("usage", completion)
        receipt = json.loads(Path(completion["crossfeed"]["selection_file"]).read_text())
        self.assertEqual(receipt["choice"]["mode"], "read-only")
        self.assertTrue(all(o["model_key"] == "gpt-6.1-sol" for o in receipt["top3"]))
        self.assertTrue(Path(completion["crossfeed"]["model_receipt"]).is_file())
        self.assertEqual(self.calls()[0]["cwd"], str(self.root))
        self.diagnostics.flush()
        self.diagnostics.seek(0)
        self.assertTrue(self.key not in self.diagnostics.read())

    def test_auto_and_sse_completion(self):
        status, payload, content_type = self.request(body=self.body("crossfeed:auto", stream=True))
        self.assertEqual(status, 200, payload)
        self.assertEqual(content_type, "text/event-stream")
        frames = [line[6:] for line in payload.splitlines() if line.startswith("data: ")]
        self.assertEqual(frames[-1], "[DONE]")
        first, last = map(json.loads, frames[:-1])
        self.assertEqual(first["object"], "chat.completion.chunk")
        self.assertEqual(first["choices"][0]["delta"]["content"], "fake answer é\n")
        self.assertEqual(last["choices"][0]["finish_reason"], "stop")
        self.assertEqual(first["crossfeed"]["requested_model"], "crossfeed:auto")

    def test_bad_requests_do_not_dispatch(self):
        invalid = [self.body(mode="write"), self.body(tools=[]), self.body(temperature=0),
                   self.body(n=2), self.body(stream="false"), self.body(messages=[]),
                   self.body(messages=[{"role": "tool", "content": "result"}]),
                   self.body(messages=[{"role": [], "content": "result"}]),
                   self.body(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]),
                   self.body(messages=[{"role": "user", "content": "   "}]), [], None]
        for body in invalid:
            status, payload, _ = self.request(raw=json.dumps(body))
            self.assertEqual(status, 400, payload)
        self.assertEqual(self.request(raw="{broken")[0], 400)
        # Refuse the oversized declared length before attempting to read its body.
        self.assertEqual(self.request(raw="", extra_headers={"Content-Length": str(lanes_api.MAX_BODY_BYTES + 1)})[0], 400)
        self.assertEqual(self.request("/other")[0], 404)
        self.assertEqual(self.request(body=self.body("missing"))[0], 404)
        self.assertEqual(self.calls(), [])

    def test_live_switch_is_rechecked_for_catalog_and_completion(self):
        with fleetctl.locked_runtime(self.state) as runtime:
            fleetctl.set_model_preference(runtime, "gpt-6.1-sol", "off")
        self.assertEqual(self.request(body=self.body())[0], 404)
        _, payload, _ = self.request("/v1/models")
        self.assertNotIn("codex:gpt-6.1-sol", {m["id"] for m in json.loads(payload)["data"]})
        self.assertEqual(self.calls(), [])

    def test_wrapper_refusal_is_clear_and_does_not_leak_partial_answer(self):
        (self.state / "fail").touch()
        status, payload, _ = self.request(body=self.body())
        self.assertEqual(status, 429, payload)
        self.assertNotIn("private failed partial answer", payload)
        self.assertTrue(all(call["choice"]["model_key"] == "gpt-6.1-sol" for call in self.calls()))

    def test_low_pool_lease_is_released_by_wrapper(self):
        with fleetctl.locked_runtime(self.state) as runtime:
            fleetctl.set_pool_level(runtime, "codex", "low")
        self.assertEqual(self.request(body=self.body())[0], 200)
        self.assertEqual(fleetctl.load_json(self.state / "runtime.json")["leases"], [])

    def test_exact_body_limit_launches_wrapper(self):
        body = self.body(messages=[{"role": "user", "content": ""}])
        body["messages"][0]["content"] = "x" * (lanes_api.MAX_BODY_BYTES - len(json.dumps(body).encode()))
        raw = json.dumps(body)
        self.assertEqual(len(raw.encode()), lanes_api.MAX_BODY_BYTES)
        status, payload, _ = self.request(raw=raw)
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.calls()), 1)


class RealWrapperTests(Sandbox):
    def test_live_stand_in_reports_wrapper_selected_model_and_ledger(self):
        self.fake_cli("codex", FAKE_CODEX)
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": ["codex"]}}
        self.write_roster(self.roster)
        api = lanes_api.LanesAPI(self.overlay, self.state, self.root, "review")
        dispatch = fleetctl.dispatch_selection

        def switch_then_dispatch(*args, **kwargs):
            with fleetctl.locked_runtime(self.state) as runtime:
                fleetctl.set_model_preference(runtime, "gpt-6-astra", "off")
            return dispatch(*args, **kwargs)

        with mock.patch.dict(os.environ, self.env), mock.patch.object(fleetctl, "dispatch_selection", side_effect=switch_then_dispatch):
            completion = api.complete("codex:gpt-6-astra", "Read-only text task")
        self.assertEqual(completion["model"], "codex:gpt-6.1-sol")
        self.assertEqual(completion["crossfeed"]["requested_model"], "codex:gpt-6-astra")
        self.assertEqual(completion["crossfeed"]["identity"]["selected_model"], "gpt-6.1-sol")
        self.assertIsNone(completion["crossfeed"]["identity"]["actual_model"])
        ledger = [json.loads(line) for line in (self.state / "runs.jsonl").read_text().splitlines()]
        self.assertTrue(any(record.get("selected_model") == "gpt-6.1-sol" for record in ledger))


if __name__ == "__main__":
    unittest.main()
