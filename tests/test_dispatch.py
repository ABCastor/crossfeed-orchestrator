"""Dispatch preserves literal tasks and falls back on launch and runtime failure."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import signal
import time
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl
import selector


WORKER = '''import json, os, pathlib, sys
code, identity, label = int(sys.argv[1]), sys.argv[2], sys.argv[3]
args = sys.argv[4:]
last = pathlib.Path(args[args.index("--last") + 1])
prompt = args[args.index("--prompt") + 1]
with pathlib.Path(os.environ["CALLS"]).open("a") as f:
    f.write(json.dumps({"label": label, "args": args, "prompt": prompt,
                       "cwd": os.getcwd(), "selection": os.environ.get("FLEET_SELECTION_FILE"),
                       "state": os.environ.get("FLEET_STATE_DIR"),
                       "overlay": os.environ.get("ACCESS_OVERLAY"),
                       "dispatch_id": os.environ.get("CROSSFEED_DISPATCH_ID")}) + "\\n")
print("private wrapper diagnostic " + prompt, file=sys.stderr)
if code: print("partial failed answer: " + label)
if identity != "none":
    record = {"schema": "crossfeed-model-run/v1", "actual_model": "provider-model",
              "dispatch_id": os.environ["CROSSFEED_DISPATCH_ID"], "returncode": code}
    if identity == "mismatch": record["dispatch_id"] = "previous-run"
    pathlib.Path(str(last) + ".crossfeed.json").write_text(
        "not json" if identity == "malformed" else json.dumps(record))
if code == 0:
    last.write_text(label + " answer\\n")
    print(label + " answer")
sys.exit(code)
'''


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch test ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "state"
        self.worker = self.root / "worker.py"
        self.worker.write_text(WORKER)
        self.calls = self.root / "calls.jsonl"
        self.args = argparse.Namespace(dir=self.root, last=None, dry_run=False,
                                       overlay=self.root / "overlay.json")
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"CALLS": str(self.calls),
                                    "CROSSFEED_DISPATCH_ID": "inherited-stale-id",
                                    "FLEET_SELECTION_FILE": "inherited-stale-selection"}).start()

    def option(self, label, code=0, identity="valid", harness="claude"):
        receipt = self.root / (label + ".selection.json")
        receipt.write_text(json.dumps({"choice": {"model_key": label}}))
        return {"pool": harness, "harness": harness, "model_key": label, "level": "high",
                "selection_file": str(receipt), "command": "do not execute this display string",
                "command_argv": ["env", "FLEET_SELECTION_FILE=" + str(receipt),
                                 sys.executable, str(self.worker), str(code), identity, label,
                                 "--prompt", "<task>"]}

    def selection(self, options, choice=None):
        return {"choice": choice or options[0], "top3": options}

    def dispatch(self, selection, prompt="worker task"):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = fleetctl.dispatch_selection(selection, self.args, self.state, prompt)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return rc, calls, stderr.getvalue()

    def test_attempt_transcripts_removed_after_stdout_last_api_and_failure(self):
        for mode in ('stdout', 'last', 'api', 'failure', 'cancel'):
            with self.subTest(mode=mode):
                self.args.last = self.root / 'caller-answer' if mode == 'last' else None
                answer = {} if mode == 'api' else None
                code = 42 if mode == 'failure' else 143 if mode == 'cancel' else 0
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = fleetctl.dispatch_selection(self.selection([self.option(mode, code)]),
                                                     self.args, self.state, 'task', response=answer)
                self.assertEqual(rc, code)
                for attempt in (self.state / 'dispatches').iterdir():
                    self.assertFalse((attempt / 'answer.txt').exists())
                    self.assertFalse((attempt / 'stdout.log').exists())
                    self.assertTrue((attempt / 'dispatch.json').is_file())
                    self.assertTrue((attempt / 'answer.txt.crossfeed.json').is_file())
                    self.assertTrue((attempt / 'stderr.log').is_file())
                if mode == 'last':
                    self.assertEqual(self.args.last.read_text(), 'last answer\n')
                if mode == 'api':
                    self.assertEqual(answer['content'], 'api answer\n')

    def test_prompt_is_literal_and_selection_environment_survives(self):
        prompt = 'line 1\\n"quotes" $HOME $(touch forbidden) `touch forbidden` ; --dir <task>'
        selected = self.option("chosen")
        original = list(selected["command_argv"])
        rc, calls, diagnostic = self.dispatch(self.selection([selected]), prompt)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["prompt"], prompt)
        self.assertEqual(calls[0]["cwd"], str(self.root))
        self.assertEqual(calls[0]["selection"], selected["selection_file"])
        self.assertEqual(calls[0]["state"], str(self.state))
        self.assertEqual(calls[0]["overlay"], str(self.args.overlay))
        self.assertNotEqual(calls[0]["dispatch_id"], "inherited-stale-id")
        self.assertEqual(selected["command_argv"], original)
        self.assertNotIn(prompt, diagnostic)
        self.assertFalse((self.root / "forbidden").exists())
        receipt = next(self.state.glob("dispatches/*/dispatch.json"))
        self.assertNotIn(prompt, receipt.read_text())
        self.assertNotIn("actual_model", json.loads(receipt.read_text()))
        self.assertIn("model_receipt=", diagnostic)
        self.assertIn("diagnostics=", diagnostic)
        self.assertNotIn(prompt, receipt.with_name("stderr.log").read_text())
        self.assertIn("stderr_bytes=", receipt.with_name("stderr.log").read_text())

    def test_prelaunch_refusals_retry_ranked_options_once(self):
        options = [self.option("first", 4, "none"), self.option("second", 5, "none"),
                   self.option("third")]
        rc, calls, diagnostic = self.dispatch(self.selection(options))
        self.assertEqual(rc, 0)
        self.assertEqual([call["label"] for call in calls], ["first", "second", "third"])
        self.assertEqual(diagnostic.count(" instead"), 2)
        self.assertIn("claude:first failed (exit 4); using claude:second instead", diagnostic)
        self.assertEqual(len({call["dispatch_id"] for call in calls}), 3)

    def test_exploratory_choice_runs_before_all_top_three(self):
        options = [self.option(str(index), 5, "none") for index in range(3)]
        exploratory = self.option("exploration", 4, "none")
        rc, calls, _ = self.dispatch(self.selection(options, exploratory))
        self.assertEqual(rc, 5)
        self.assertEqual([call["label"] for call in calls], ["exploration", "0", "1", "2"])

    def test_runtime_failures_retry_across_harnesses_despite_receipts(self):
        for harness in ("claude", "codex", "opencode", "copilot", "agy", "openrouter", "chatgpt-chat"):
            for code in (4, 6, 124):
                for identity in ("valid", "malformed", "mismatch"):
                    with self.subTest(harness=harness, code=code, identity=identity):
                        prior = len(self.calls.read_text().splitlines()) if self.calls.exists() else 0
                        rc, calls, diagnostic = self.dispatch(self.selection([
                            self.option("first", code, identity, harness), self.option("unused")]))
                        self.assertEqual(rc, 0)
                        self.assertEqual(len(calls) - prior, 2)
                        self.assertIn("using claude:unused instead", diagnostic)
                        self.assertNotIn("Traceback", diagnostic)

    # BR-CASH-004
    def test_retry_never_enters_or_leaves_a_metered_attempt(self):
        self.args.data_class = "public"
        for first_paid in (False, True):
            with self.subTest(first_paid=first_paid):
                first = self.option(f"first-{first_paid}", 6, "valid")
                second = self.option(f"second-{first_paid}")
                paid = first if first_paid else second
                paid.update(funding="metered", data_class="public")
                rc, calls, diagnostic = self.dispatch(self.selection([first, second]))
                self.assertEqual(rc, 6, diagnostic)
                self.assertEqual(calls[-1]["label"], first["model_key"])
                self.assertNotIn(second["model_key"], [call["label"] for call in calls])

    def test_free_billing_incident_never_retries_another_lane(self):
        first = self.option("charged-free", 8, "valid", "openrouter")
        first.update(funding="verified-free")
        rc, calls, diagnostic = self.dispatch(self.selection([first, self.option("unused")]))
        self.assertEqual(rc, 8, diagnostic)
        self.assertEqual([call["label"] for call in calls], ["charged-free"])

    def test_success_and_cancellation_never_retry(self):
        for code in (0, 129, 130, 143):
            with self.subTest(code=code):
                prior = len(self.calls.read_text().splitlines()) if self.calls.exists() else 0
                rc, calls, _ = self.dispatch(self.selection([
                    self.option("first", code, "none"), self.option("unused")]))
                self.assertEqual(rc, code)
                self.assertEqual(len(calls) - prior, 1)

    def test_stale_caller_sidecar_cannot_suppress_retry(self):
        self.args.last = self.root / "caller answer.txt"
        self.args.last.write_text("old answer")
        sidecar = Path(str(self.args.last) + ".crossfeed.json")
        sidecar.write_text(json.dumps({"dispatch_id": "inherited-stale-id", "returncode": 0}))
        rc, calls, _ = self.dispatch(self.selection([
            self.option("refused", 5, "none"), self.option("launched", 0)]))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.args.last.read_text(), "launched answer\n")
        self.assertEqual(json.loads(sidecar.read_text())["dispatch_id"], calls[1]["dispatch_id"])

    def test_all_refused_preserves_final_native_exit(self):
        rc, calls, _ = self.dispatch(self.selection([
            self.option("a", 5, "none"), self.option("b", 4, "none")]))
        self.assertEqual(rc, 4)
        self.assertEqual(len(calls), 2)

    def test_missing_wrapper_falls_back(self):
        option = self.option("missing")
        option["command_argv"] = [str(self.root / "missing"), "--prompt", "<task>"]
        rc, calls, diagnostic = self.dispatch(self.selection([option, self.option("unused")]))
        self.assertEqual(rc, 0)
        self.assertEqual([call["label"] for call in calls], ["unused"])
        self.assertIn("failed (exit 127); using claude:unused instead", diagnostic)

    def test_dry_run_has_resolved_argv_and_never_launches(self):
        self.args.dry_run = True
        option = self.option("preview")
        stdout = io.StringIO()
        with mock.patch.object(subprocess, "Popen") as launch, contextlib.redirect_stdout(stdout):
            rc, calls, _ = self.dispatch(self.selection([option]), "dry task")
        launch.assert_not_called()
        preview = json.loads(stdout.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertIn("dry task", preview["command_argv"])
        self.assertEqual(preview["selection_file"], option["selection_file"])
        self.assertFalse(Path(preview["receipt"]).exists())
        self.assertFalse(self.state.exists())

    def test_wrapper_argv_flags_for_every_selected_harness(self):
        for harness in selector.WRAPPERS:
            for mode in ("read-only", "write"):
                with self.subTest(harness=harness, mode=mode):
                    level = "service-chosen" if harness in {"copilot", "chatgpt-chat"} else (
                        "provider-default" if harness == "openrouter" else "high")
                    option = {"harness": harness, "level": level, "run_as": "chosen-model",
                              "lane_id": "chosen-lane", "mode": mode}
                    if harness == "chatgpt-chat" and mode == "write":
                        with self.assertRaises(fleetctl.FleetError):
                            selector._command(option, "review", self.root / "selection.json")
                        continue
                    option["command_argv"] = selector._command(option, "review", self.root / "selection.json")
                    argv = fleetctl.dispatch_argv(option, "exact task", self.root, self.root / "answer")
                    self.assertEqual(argv[argv.index("--prompt") + 1], "exact task")
                    self.assertEqual(argv[-2:], ["--last", str(self.root / "answer")])
                    self.assertEqual("--dir" in argv, harness != "openrouter")
                    if harness == "codex":
                        self.assertEqual(argv[argv.index("--sandbox") + 1],
                                         "read-only" if mode == "read-only" else "workspace-write")
                    if harness == "opencode":
                        self.assertIn("--read-only" if mode == "read-only" else "--write", argv)
                    if harness == "chatgpt-chat":
                        self.assertEqual(argv[argv.index("--mode") + 1], "ro")
                        self.assertEqual(argv[argv.index("--lane") + 1], "chosen-lane")
                        self.assertNotIn("--effort", argv)

    def test_bad_placeholder_refuses_before_launch(self):
        for slots in (["--prompt", "already filled"], ["--prompt", "<task>", "--prompt", "<task>"]):
            option = self.option("bad")
            option["command_argv"] = ["unused"] + slots
            with self.assertRaises(fleetctl.FleetError):
                self.dispatch(self.selection([option]))
        self.assertFalse(self.calls.exists())

    def test_cli_prompt_file_and_lineage_reach_selector_without_refresh_on_dry_run(self):
        prompt_file = self.root / "task with spaces.txt"
        prompt_file.write_text("file task\nsecond line")
        option = self.option("preview", harness="agy")
        cli = ["fleetctl", "--overlay", str(self.args.overlay), "--state-dir", str(self.state),
               "dispatch", "--role", "review", "--stakes", "high", "--family", "review",
               "--exclude-lineage", "openai", "--mode", "write", "--prompt-file", str(prompt_file),
               "--dir", str(self.root), "--dry-run", "--allow", "antigravity-gemini:gemini-3.8-flash:*"]
        with mock.patch.object(sys, "argv", cli), mock.patch.object(fleetctl, "read_overlay", return_value={}), \
             mock.patch.object(fleetctl, "select_option", return_value=self.selection([option])) as select, \
             mock.patch.object(fleetctl, "refresh_stale_pools") as refresh, \
             mock.patch.object(subprocess, "Popen") as launch, \
             contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(fleetctl.main(), 0)
        refresh.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(select.call_args.kwargs["exclude_lineage"], "openai")
        self.assertEqual(select.call_args.kwargs["stakes"], "high")
        self.assertEqual(select.call_args.kwargs["allow"], "antigravity-gemini:gemini-3.8-flash:*")
        self.assertEqual(select.call_args.kwargs["mode"], "write")
        self.assertIn(prompt_file.read_text(), json.loads(stdout.getvalue())["command_argv"])

    def test_real_signals_forward_to_worker_group_and_remove_transcripts(self):
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum):
                ready = self.root / f'ready-{signum}'
                received = self.root / f'received-{signum}'
                worker = self.root / f'signal-worker-{signum}.py'
                worker.write_text("import pathlib, signal, sys, time\n"
                    f"def stop(sig, frame):\n pathlib.Path({str(received)!r}).write_text(str(sig)); sys.exit(128+sig)\n"
                    "signal.signal(signal.SIGTERM, stop)\nsignal.signal(signal.SIGHUP, stop)\n"
                    "args=sys.argv; pathlib.Path(args[args.index('--last')+1]).write_text('private answer')\n"
                    "print('private partial',flush=True)\n"
                    f"pathlib.Path({str(ready)!r}).write_text(str(__import__('os').getpid()))\ntime.sleep(30)\n")
                option = self.option(f'signal-{signum}')
                option['command_argv'] = [sys.executable, str(worker), '--prompt', '<task>']
                runner = self.root / f'signal-runner-{signum}.py'
                runner.write_text("import argparse,json,pathlib,sys\n"
                    f"sys.path.insert(0,{str(ROOT / 'scripts')!r})\nimport fleetctl\n"
                    f"args=argparse.Namespace(dir=pathlib.Path({str(self.root)!r}),last=None,dry_run=False,overlay=pathlib.Path({str(self.args.overlay)!r}))\n"
                    f"sys.exit(fleetctl.dispatch_selection(json.loads({json.dumps(self.selection([option]))!r}),args,pathlib.Path({str(self.state)!r}),'task'))\n")
                proc = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        start_new_session=True)
                finished = False
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists())
                    proc.send_signal(signum)
                    proc.communicate(timeout=10)
                    self.assertEqual(proc.returncode, 128 + signum)
                    self.assertEqual(received.read_text(), str(signum))
                    self.assertFalse(list(self.state.glob('dispatches/*/answer.txt')))
                    self.assertFalse(list(self.state.glob('dispatches/*/stdout.log')))
                    finished = True
                finally:
                    if not finished and ready.exists():
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(int(ready.read_text()), signal.SIGKILL)
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.communicate()

    def test_stdout_has_only_worker_bytes(self):
        runner = self.root / "runner.py"
        options = self.selection([self.option("broken", 6), self.option("chosen")])
        runner.write_text("import argparse, json, pathlib, sys\n"
                          f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\nimport fleetctl\n"
                          f"options = json.loads({json.dumps(options)!r})\n"
                          f"args = argparse.Namespace(dir=pathlib.Path({str(self.root)!r}), last=None, "
                          f"dry_run=False, overlay=pathlib.Path({str(self.args.overlay)!r}))\n"
                          f"sys.exit(fleetctl.dispatch_selection(options,args,pathlib.Path({str(self.state)!r}),'task'))\n")
        result = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "chosen answer\n")
        self.assertEqual(len(result.stderr.splitlines()), 3)
        self.assertNotIn("private wrapper diagnostic", result.stderr)
        self.assertNotIn("partial failed answer", result.stdout)

    def test_real_selector_dry_run_resolves_relative_receipts_across_directories(self):
        worker_dir = self.root / "worker directory"
        worker_dir.mkdir()
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts/fleetctl.py"), "--overlay",
            str(ROOT / "tests/fixtures/access-overlay.test.json"), "--state-dir", "relative state",
            "dispatch", "--role", "review", "--exclude-lineage", "openai",
            "--allow", "claude:claude-sonnet-5-5:high",
            "--prompt", "exact task", "--dir", str(worker_dir), "--mode", "read-only", "--dry-run",
        ], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(result.stdout)
        selection = Path(preview["selection_file"])
        self.assertTrue(selection.is_absolute())
        self.assertTrue(selection.is_file())
        receipt = json.loads(selection.read_text())
        self.assertEqual(receipt["allow"], ["claude:claude-sonnet-5-5:high"])
        self.assertEqual(receipt["choice"]["model_key"], "claude-sonnet-5-5")
        self.assertEqual(receipt["choice"]["level"], "high")
        self.assertIn("FLEET_SELECTION_FILE=" + str(selection), preview["command_argv"])
        self.assertEqual(preview["cwd"], str(worker_dir))
        self.assertFalse(Path(preview["receipt"]).exists())


if __name__ == "__main__":
    unittest.main()
