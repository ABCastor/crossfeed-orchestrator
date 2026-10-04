"""Model receipts reach the worker, dispatcher, ledger and rendered console, using fake CLIs only."""
import importlib.util
import contextlib
import json
import io
import tempfile
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.test_switch_respected import Sandbox, SCRIPTS, FAKE_CODEX
from tests.test_console import console, fleetctl

SPEC = importlib.util.spec_from_file_location("run_identity", SCRIPTS / "run_identity.py")
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


class IdentityWrapperTests(Sandbox):
    def receipts(self):
        return [json.loads(line) for line in (self.state / "runs.jsonl").read_text().splitlines()
                if json.loads(line).get("schema") == identity.SCHEMA]

    def check_run(self, result, argv, requested, selected, actual, last=None):
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.receipts()[-1]
        self.assertEqual(record["requested_model"], requested)
        self.assertEqual(record["selected_model"], selected)
        self.assertEqual(record["actual_model"], actual)
        self.assertIn(f"Crossfeed selected model: {selected}", argv.read_text())
        self.assertIn("Crossfeed model receipt:", result.stderr)
        self.assertIn(actual or selected, result.stderr)
        self.assertIn(record["run_id"], result.stderr)
        self.assertNotIn("Crossfeed", result.stdout)
        if last:
            self.assertEqual(last.read_text(), result.stdout)
            self.assertNotIn(record["run_id"], last.read_text())
            self.assertEqual(json.loads(Path(str(last) + ".crossfeed.json").read_text()), record)
        overview = fleetctl.fleet_overview(self.roster, {}, self.state)
        page = console.render_page(overview, "test-token")
        self.assertIn("Recent runs", page)
        self.assertIn(actual or selected, page)
        self.assertIn(requested or "automatic selection", page)
        # The ledger never contains the task or the answer.
        self.assertNotIn("private task", (self.state / "runs.jsonl").read_text())

    def test_codex_fallback_reaches_every_side_with_native_thread_identity(self):
        thread = "00000000-0000-0000-0000-000000000001"
        sessions = self.codex_home / "sessions" / "2026" / "10" / "02"
        sessions.mkdir(parents=True)
        (sessions / f"rollout-{thread}.jsonl").write_text(json.dumps(
            {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}) + "\n")
        argv = self.fake_cli("codex", f"echo '{{\"type\":\"thread.started\",\"thread_id\":\"{thread}\"}}'\n" + FAKE_CODEX)
        self.off("codex", "gpt-6-astra")
        last = self.root / "answer"
        result = self.run_script("codex-agent.sh", "--model", "gpt-6-astra", "--prompt", "private task",
                                 "--dir", str(self.root), "--last", str(last), "--idle-timeout", "0")
        self.check_run(result, argv, "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-sol", last)

    def test_claude_stream_tells_the_worker_and_reports_the_provider_model(self):
        argv = self.fake_cli("claude", """echo '{"type":"system","subtype":"init","model":"claude-sonnet-5-5"}'
echo '{"type":"result","is_error":false,"result":"answer"}'
""")
        self.off("claude", "claude-opus-5-5")
        result = self.run_script("claude-agent.sh", "--prompt", "private task", "--dir", str(self.root), "--idle-timeout", "0")
        self.check_run(result, argv, "opus", "claude-sonnet-5-5", "claude-sonnet-5-5")
        self.assertTrue(result.stdout.startswith("answer\n"))
        self.assertNotIn('"type":"system"', result.stdout)

    def test_copilot_auto_reports_the_resolved_model(self):
        argv = self.fake_cli("copilot", """echo '{"type":"session.auto_mode_resolved","data":{"chosenModel":"gpt-6.1-sol"}}'
echo '{"type":"assistant.message","data":{"content":"answer"}}'
echo '{"type":"result","exitCode":0}'
""")
        result = self.run_script("copilot-agent.sh", "--prompt", "private task", "--dir", str(self.root), "--idle-timeout", "0")
        self.check_run(result, argv, "auto", "github-copilot-auto", "gpt-6.1-sol")

    def test_agy_named_model_is_explicitly_unconfirmed_when_no_native_identity_is_exposed(self):
        argv = self.fake_cli("agy", "echo answer\n")
        result = self.run_script("agy-agent.sh", "--model", "gemini-3.6-flash-high", "--prompt", "private task",
                                 "--dir", str(self.root), "--idle-timeout", "0")
        self.check_run(result, argv, "gemini-3.6-flash-high", "gemini-3.6-flash", None)
        self.assertIn("underlying model unconfirmed", result.stderr)

    def test_opencode_lane_and_provider_model_reach_result_and_console(self):
        argv = self.fake_cli("opencode", """echo '{"type":"step_start","part":{"modelID":"kimi-k3","providerID":"opencode-go"}}'
echo '{"type":"text","part":{"text":"answer"}}'
echo '{"type":"step_finish","part":{"reason":"stop"}}'
""")
        auth = self.home / ".local/share/opencode/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"opencode-go":{"type":"api","key":"fake-test-only"}}')
        self.env["AGENT_SYNC_VERIFY"] = "/usr/bin/true"
        result = self.run_script("opencode-agent.sh", "--model-key", "kimi-k3", "--prompt", "private task",
                                 "--dir", str(self.root), "--idle-timeout", "0", "--direct")
        self.check_run(result, argv, "kimi-k3", "kimi-k3", "opencode-go/kimi-k3")
        priced = [json.loads(line) for line in (self.state / "runs.jsonl").read_text().splitlines()
                  if json.loads(line).get("schema") != identity.SCHEMA]
        self.assertEqual(len(priced), 1)
        self.assertEqual(priced[0]["run_id"], self.receipts()[-1]["run_id"])
        self.assertEqual(fleetctl.run_ledger_usage(self.state)["windows"]["rolling_5h"]["direct"]["runs"], 1)

    def test_fanout_summary_reads_native_auto_identity(self):
        self.fake_cli("copilot", '''echo '{"type":"session.auto_mode_resolved","data":{"chosenModel":"gpt-6.1-sol"}}'
echo '{"type":"assistant.message","data":{"content":"answer"}}'
echo '{"type":"result","exitCode":0}'
''')
        tasks, out = self.root / "tasks.jsonl", self.root / "campaign"
        tasks.write_text(json.dumps({"id": "identity", "agent": "copilot", "prompt": "hi", "dir": str(self.root)}) + "\n")
        result = self.run_script("fanout.sh", str(tasks), "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((out / "summary.tsv").read_text().split("\t")[3], "gpt-6.1-sol")
        self.assertEqual((out / "identity.out").read_text(), "answer\n")


    def test_openrouter_wrapper_captures_the_provider_model_from_its_native_stream(self):
        # Intercept urllib in this test's Python processes. No socket or real provider is reachable.
        native = self.root / "native"
        native.mkdir()
        request_file = self.root / "request.json"
        (native / "sitecustomize.py").write_text('''
import io, json, os, urllib.request
from pathlib import Path
def urlopen(request, timeout=None):
    if request.full_url.endswith("/models"):
        return io.BytesIO(json.dumps({"data":[{"id":"openrouter/free", "pricing":{"prompt":"0", "completion":"0"},
            "architecture":{"input_modalities":["text"],"output_modalities":["text"]}}]}).encode())
    data = json.loads(request.data)
    Path(os.environ["NATIVE_REQUEST_FILE"]).write_text(json.dumps(data))
    event = {"model":"vendor/free-answer", "choices":[{"delta":{"content":"answer"}}], "usage":{"cost":0}}
    return io.BytesIO(("data: " + json.dumps(event) + "\\n\\ndata: [DONE]\\n\\n").encode())
urllib.request.urlopen = urlopen
''')
        key = self.root / "synthetic-key"
        key.write_text("fake-test-only")
        self.env.update(PYTHONPATH=str(native), NATIVE_REQUEST_FILE=str(request_file), OPENROUTER_KEY_FILE=str(key))
        last = self.root / "answer"
        result = self.run_script("openrouter-agent.sh", "--model-key", "openrouter-free-router",
                                 "--prompt", "private task", "--last", str(last), "--idle-timeout", "0")
        self.check_run(result, request_file, "openrouter-free-router", "openrouter-free-router", "vendor/free-answer", last)
        self.assertEqual(json.loads(request_file.read_text())["model"], "openrouter/free")

    def test_receipt_persistence_failure_is_not_success(self):
        self.fake_cli("codex", FAKE_CODEX)
        invalid_state = self.root / "not-a-directory"
        invalid_state.write_text("fixture")
        self.env["FLEET_STATE_DIR"] = str(invalid_state)
        result = self.run_script("codex-agent.sh", "--model", "gpt-6.1-sol", "--prompt", "private task", "--idle-timeout", "0")
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertIn("model receipt/ledger failed", result.stderr)


    def test_schema_stdout_is_original_json_and_identity_uses_sidecar(self):
        self.fake_cli("codex", FAKE_CODEX.replace("echo 'final answer'", "echo '{\"ok\":true}'"))
        last, schema = self.root / "answer.json", self.root / "schema.json"
        schema.write_text('{"type":"object","properties":{"ok":{"type":"boolean"}},"additionalProperties":false}')
        result = self.run_script("codex-agent.sh", "--model", "gpt-6.1-sol", "--prompt", "private task",
                                 "--dir", str(self.root), "--last", str(last), "--schema", str(schema), "--idle-timeout", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ok": True})
        self.assertEqual(result.stdout, last.read_text())
        self.assertIn("Crossfeed model receipt:", result.stderr)
        self.assertEqual(json.loads(Path(str(last) + ".crossfeed.json").read_text()), self.receipts()[-1])

    def test_invalid_schema_result_gets_a_failed_receipt(self):
        self.fake_cli("codex", FAKE_CODEX)
        schema = self.root / "schema.json"
        schema.write_text('{}')
        result = self.run_script("codex-agent.sh", "--model", "gpt-6.1-sol", "--prompt", "private task",
                                 "--schema", str(schema), "--idle-timeout", "0")
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("exit 8", result.stderr)
        self.assertEqual(self.receipts()[-1]["returncode"], 8)
        self.assertEqual(self.receipts()[-1]["status"], "error")


    def test_failed_and_empty_runs_get_receipts_but_refusals_and_dry_runs_do_not(self):
        self.fake_cli("codex", "exit 3\n")
        result = self.run_script("codex-agent.sh", "--model", "gpt-6.1-sol", "--prompt", "private task", "--idle-timeout", "0")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.receipts()[-1]["returncode"], 3)
        self.assertIn("exit 3", result.stderr)
        self.assertEqual(result.stdout, "")
        self.fake_cli("codex", "exit 0\n")
        result = self.run_script("codex-agent.sh", "--prompt", "private task", "--idle-timeout", "0")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(self.receipts()[-1]["returncode"], 4)
        before = (self.state / "runs.jsonl").read_bytes()
        result = self.run_script("codex-agent.sh", "--prompt", "hi", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / "runs.jsonl").read_bytes(), before)
        self.off("codex", "gpt-6.1-sol")
        self.off("codex", "gpt-6-astra")
        self.off("codex", "gpt-6-luna")
        result = self.run_script("codex-agent.sh", "--prompt", "hi")
        self.assertEqual(result.returncode, 5)
        self.assertEqual((self.state / "runs.jsonl").read_bytes(), before)


class ReceiptTests(unittest.TestCase):
    def test_openrouter_receipt_uses_resolved_native_model_and_escapes_console(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.dict(os.environ, FLEET_STATE_DIR=str(root)):
                path, events, last = root / "identity", root / "events", root / "answer"
                record = identity.begin("openrouter", "openrouter/free", "openrouter/free", "", "")
                path.write_text(json.dumps(record))
                events.write_text('{"type":"crossfeed.provider_model","model":"actual/free-model"}\n')
                last.write_text("answer\n")
                identity.finish(path, events, 0, last, False)
                recorded = json.loads(path.read_text())
                self.assertEqual(recorded["actual_model"], "actual/free-model")
                self.assertEqual(last.read_text(), "answer\n")
                recorded["requested_model"] = "<script>alert(1)</script>"
                page = console._recent_runs({"recent_runs": [recorded]})
                self.assertNotIn("<script>", page)
                self.assertIn("&lt;script&gt;", page)

    def test_aliases_preserve_versions_and_resolve_declared_names(self):
        data = {"model_cards": {"claude-opus-5-5": {"run_as": "opus", "aliases": ["claude-opus-5-5-20260922"]}}}
        data["model_cards"] = {"claude-opus-5": {"status": "older", "aliases": ["opus"]}, **data["model_cards"]}
        self.assertEqual(identity.normalize_model_alias("anthropic/opus", data), "claude-opus-5-5")
        self.assertEqual(identity.normalize_model_alias("claude-opus-5-5-20260922", data), "claude-opus-5-5")
        self.assertEqual(identity.normalize_model_alias("claude-opus-5", data), "claude-opus-5")
        self.assertEqual(identity.normalize_model_alias("opencode-go/kimi-k3", data), "kimi-k3")
        self.assertTrue(identity.model_identity_drift({"selected_model": "claude-opus-5-5", "actual_model": "claude-opus-5"}, data))
        self.assertFalse(identity.model_identity_drift({"selected_model": "github-copilot-auto", "actual_model": "gpt-6.1-sol"}, data))

    def test_native_drift_warns_on_stderr_without_changing_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.dict(os.environ, FLEET_STATE_DIR=str(root)), mock.patch.object(identity, "roster", return_value={}):
                path, events, last = root / "identity", root / "events", root / "answer"
                path.write_text(json.dumps(identity.begin("claude", "opus", "claude-opus-5-5", "", "")))
                events.write_text('{"type":"system","subtype":"init","model":"claude-opus-5"}\n')
                last.write_text("unchanged")
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(identity.finish(path, events, 0, last, False), 0)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(last.read_text(), "unchanged")
                self.assertIn("WARNING: MODEL IDENTITY DRIFT", err.getvalue())
                self.assertIn("Crossfeed model receipt:", err.getvalue())

    def test_alias_match_and_dynamic_resolution_do_not_warn(self):
        data = {"model_cards": {"claude-opus-5-5": {"run_as": "opus"}}}
        for selected, selector, actual in [("claude-opus-5-5", "opus", "anthropic/opus"),
                                          ("openrouter-free-router", "openrouter/free", "vendor/native")]:
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with mock.patch.dict(os.environ, FLEET_STATE_DIR=str(root)), mock.patch.object(identity, "roster", return_value=data):
                    path, events = root / "identity", root / "events"
                    record = identity.begin("openrouter", selected, selector, "", "")
                    record["selected_model"] = selected
                    path.write_text(json.dumps(record))
                    events.write_text(json.dumps({"type": "crossfeed.provider_model", "model": actual}) + "\n")
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        identity.finish(path, events, 0, None, False)
                    self.assertNotIn("WARNING", err.getvalue())

    def test_pi_native_identity_usage_and_terminal_snapshot_are_not_double_counted(self):
        message = {"role": "assistant", "model": "gemini-test", "provider": "google", "timestamp": 1,
                   "content": [{"type": "text", "text": "private answer"}],
                   "usage": {"input": 10, "output": 4, "cacheRead": 2, "cacheWrite": 0,
                             "totalTokens": 16, "cost": {"total": 0.03}}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            events = root / "events"
            events.write_text("\n".join(json.dumps(event) for event in [
                {"type": "message_end", "message": message},
                {"type": "message_end", "message": message},
                {"type": "agent_end", "messages": [message]}]) + "\n")
            self.assertEqual(identity.observed_model("pi", events), "google/gemini-test")
            self.assertEqual(identity.observed_usage("pi", events), {
                "tokens": {"input": 10, "output": 4, "cache_read": 2, "cache_write": 0, "total": 16},
                "cost": {"estimated_usd": 0.03, "source": "pi-native"}})
            with mock.patch.dict(os.environ, FLEET_STATE_DIR=str(root)), mock.patch.object(identity, "roster", return_value={"lanes": [
                    {"lane_id": "pi-google", "harness": "pi", "selector": "google/gemini-test", "model_key": "gemini-test", "quota_pool": "gemini-metered"}]}):
                path = root / "identity"
                path.write_text(json.dumps(identity.begin("pi", "google/gemini-test", "google/gemini-test", "pi-google", "", "high")))
                with contextlib.redirect_stderr(io.StringIO()):
                    identity.finish(path, events, 0, None, False)
                record = json.loads(path.read_text())
                self.assertEqual(record["quota_pool"], "gemini-metered")
                self.assertEqual(record["cost"]["estimated_usd"], 0.03)
                self.assertNotIn("private answer", (root / "runs.jsonl").read_text())
            events.write_text(json.dumps({"type": "agent_end", "messages": [message]}) + "\n")
            self.assertEqual(identity.observed_usage("pi", events)["tokens"]["total"], 16)
            second = {**message, "timestamp": 2}
            events.write_text(json.dumps({"type": "message_end", "message": message}) + "\n" +
                              json.dumps({"type": "agent_end", "messages": [message, second]}) + "\n")
            self.assertEqual(identity.observed_usage("pi", events)["tokens"]["total"], 32)

    def test_pi_prose_is_not_native_identity_and_missing_usage_remains_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.write_text('{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"I am gpt-6.1-sol"}]}}\n')
            self.assertIsNone(identity.observed_model("pi", events))
            self.assertEqual(identity.observed_usage("pi", events), {})
        self.assertEqual(identity.mapped_pi_effort("max"), "xhigh")
        self.assertEqual(identity.mapped_pi_effort("medium"), "medium")
        with self.assertRaises(ValueError):
            identity.mapped_pi_effort("ultra")

    def test_opencode_native_database_joins_only_the_current_session(self):
        import tempfile
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            events, database = root / "events", root / "opencode.db"
            events.write_text('{"type":"step_start","sessionID":"current","part":{}}\n')
            with contextlib.closing(sqlite3.connect(database)) as db:
                db.execute("CREATE TABLE message(session_id TEXT, time_created INTEGER, data TEXT)")
                for session, timestamp, model in [("current", 1, "kimi-k3"), ("other", 2, "wrong-model")]:
                    db.execute("INSERT INTO message VALUES (?, ?, ?)", (session, timestamp,
                               json.dumps({"role": "assistant", "providerID": "opencode-go", "modelID": model})))
                db.commit()
            self.assertEqual(identity.observed_model("opencode", events, database), "opencode-go/kimi-k3")


    def test_a_native_auto_selector_is_not_mistaken_for_an_underlying_model(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            events = Path(folder) / "events"
            events.write_text('{"type":"crossfeed.provider_model","model":"openrouter/free"}\n')
            self.assertIsNone(identity.observed_model("openrouter", events))


    def test_all_wrappers_call_the_same_protocol(self):
        for path in SCRIPTS.glob("*-agent.sh"):
            text = path.read_text()
            if path.name == "chatgpt-agent.sh":
                # Crossfeed Chat imports the same shared protocol in its HTTP runner.
                runner = (SCRIPTS / "chatgpt_runner.py").read_text()
                self.assertIn("import run_identity", runner)
                self.assertIn("run_identity.begin(", runner)
                self.assertIn("run_identity.worker_notice(", runner)
                self.assertIn("run_identity.finish(", runner)
                continue
            if path.name == "pi-agent.sh":
                # Pi's Python runner invokes the same begin/prompt/finish API directly.
                runner = (SCRIPTS / "pi_runner.py").read_text()
                self.assertIn("run_identity", runner)
                self.assertIn('"begin", "--path"', runner)
                self.assertIn('"finish", "--path"', runner)
                continue
            self.assertIn('source "${BASH_SOURCE[0]%/*}/run-identity.sh"', text, path.name)
            self.assertIn("crossfeed_prepare ", text, path.name)
            self.assertIn('crossfeed_finish "$status"', text, path.name)


if __name__ == "__main__":
    unittest.main()
