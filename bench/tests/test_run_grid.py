import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from bench import run_grid, wrapper

RUN_ID = "0cd5dfb7-7a9b-4410-af39-5b4e8d97c762"


def model_receipt(model="fixture", exit_code=0):
    return ("Crossfeed model receipt: requested fixture; ran on %s (provider reported); "
            "selector fixture; exit %s; run %s." % (model, exit_code, RUN_ID))


class ReceiptTests(unittest.TestCase):
    def test_formatting_tails_remove_only_receipt_line(self):
        for tail in ("", "\n", "\r\n", "  \t\n", "\n\n \t\n"):
            with self.subTest(tail=repr(tail)):
                text = '{"answer": 57}\n' + model_receipt() + tail
                reply, identity = run_grid.split_receipt(text)
                remaining_tail = "" if "\n" not in tail else tail[tail.index("\n") + 1:]
                self.assertEqual(reply, '{"answer": 57}\n' + remaining_tail)
                self.assertEqual(identity, {"ran_on": "fixture", "run_id": RUN_ID})

    def test_interior_receipt_prefix_remains(self):
        text = model_receipt("other") + '\n{"answer": 57}\n'
        self.assertEqual(run_grid.split_receipt(text), (text, {}))
        text += model_receipt() + "\n"
        self.assertEqual(run_grid.split_receipt(text)[0], text[:text.rindex(run_grid.RECEIPT_PREFIX)])

    def test_no_receipt_is_unchanged(self):
        for text in ("", " \n\t\n", '{"answer": 57}\r\n \t\n',
                     " " + model_receipt(), "Quoted " + model_receipt(),
                     "Crossfeed model receipt:not the exact prefix"):
            with self.subTest(text=text):
                self.assertEqual(run_grid.split_receipt(text), (text, {}))

    def test_unconfirmed_selected_model_is_not_ran_on(self):
        text = ('{"answer": 57}\nCrossfeed model receipt: requested fixture; '
                'selected other; underlying model unconfirmed; selector fixture; exit 0; run %s.\n' % RUN_ID)
        self.assertEqual(run_grid.split_receipt(text), ('{"answer": 57}\n', {"identity": "unconfirmed", "run_id": RUN_ID}))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crossfeed-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.tasks = self.base / "tasks"
        self.task = self.tasks / "reasoning-1"
        (self.task / "workspace").mkdir(parents=True)
        (self.task / "workspace" / "public.txt").write_text("fixture")
        (self.task / "PROMPT.md").write_text("Compute the answer. End with a JSON line.")
        (self.task / "meta.json").write_text(json.dumps({"family": "reasoning", "difficulty": "easy", "seed": 1}))
        # Use generator's check contract once available; unit measure tests mock CLI.
        (self.task / "check.json").write_text(json.dumps({"kind": "reasoning", "answer": 42}))
        self.out = self.base / "results.jsonl"
        self.config = self.base / "options.json"
        self.option = {"id": "offline", "model": "fixture", "level": "none", "pool": "test",
                       "command": [sys.executable, "-c", "print('reply')"], "timeout_s": 5}
        self.write_options([self.option])

    def write_options(self, options):
        self.config.write_text(json.dumps({"options": options}))

    def cli(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            result = run_grid.main(["--tasks", str(self.tasks), "--options", str(self.config),
                                    "--out", str(self.out)] + list(extra))
        return result, captured.getvalue()

    def reply_worker(self, stdout, final=None, exit_code=0):
        script = "import sys; from pathlib import Path; sys.stdout.write(%r); " % stdout
        if final is not None:
            script += "Path(sys.argv[1]).write_text(%r); " % final
        script += "raise SystemExit(%s)" % exit_code
        self.option["command"] = [sys.executable, "-c", script.replace("{", "{{").replace("}", "}}")]
        if final is not None:
            self.option["command"].append("{reply_file}")

    def test_dry_run_has_no_execution_or_output_mutation(self):
        with mock.patch.object(run_grid, "measure", side_effect=AssertionError("executed")):
            code, output = self.cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.out.exists())
        planned = json.loads(output)
        self.assertEqual(planned["task"], "1:reasoning-1")

    def test_parallel_resume_and_configuration_drift(self):
        option2 = dict(self.option, id="offline2")
        self.write_options([self.option, option2])
        def measured(task, option, results_dir):
            return {"task": task[2], "option": option["id"], "pass": True, "reason": "ok",
                    "task_digest": task[3], "option_digest": run_grid.option_digest(option)}
        with mock.patch.object(run_grid, "measure", side_effect=measured) as measure:
            _, output = self.cli("--parallel", "2")
            self.assertEqual(json.loads(output)["completed"], 2)
            self.cli()
            self.assertEqual(measure.call_count, 2)
        rows = [json.loads(x) for x in self.out.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.option["level"] = "high"
        self.write_options([self.option, option2])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            self.cli()
        self.assertEqual(failure.exception.code, 2)

    def test_task_seed_prevents_resume_collision(self):
        other = self.tasks / "seed2" / "reasoning-1"
        import shutil
        shutil.copytree(self.task, other)
        (other / "meta.json").write_text(json.dumps({"family": "reasoning", "difficulty": "easy", "seed": 2}))
        _, output = self.cli("--dry-run")
        self.assertEqual({json.loads(x)["task"] for x in output.splitlines()}, {"1:reasoning-1", "2:reasoning-1"})

    def test_worker_copy_contains_no_hidden_expectations_or_reference(self):
        (self.task / "reference").mkdir()
        (self.task / "reference" / "secret.txt").write_text("hidden")
        self.option["command"] = [sys.executable, "-c",
            "from pathlib import Path; assert Path('PROMPT.md').exists(); "
            "assert not Path('check.json').exists(); assert not Path('meta.json').exists(); "
            "assert not Path('reference').exists(); print('answer')"]
        with mock.patch.object(run_grid.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"pass": true, "reason": "ok"}', "")):
            row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertTrue(row["pass"], row)
        self.assertEqual(row["exit_code"], 0)
        self.assertNotIn("tokens_in", row)

    def test_nonzero_worker_is_failure_even_if_reply_valid(self):
        self.option["command"] = [sys.executable, "-c", "print('valid'); raise SystemExit(7)"]
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertFalse(row["pass"])
        self.assertEqual(row["exit_code"], 7)

    def test_real_worker_checker_and_resume(self):
        self.option["command"] = [sys.executable, "-c", "print('{\"answer\":42}')"]
        self.option["command"][2] = self.option["command"][2].replace('{"', '{{"').replace('42}', '42}}')
        self.write_options([self.option])
        self.cli()
        row = json.loads(self.out.read_text())
        self.assertTrue(row["pass"], row)
        _, output = self.cli()
        self.assertEqual(json.loads(output), {"completed": 0, "skipped": 1})

    def test_real_answer_57_with_wrapper_receipt_passes(self):
        (self.task / "check.json").write_text(json.dumps({"kind": "reasoning", "answer": 57}))
        self.option["model"] = "deepseek/deepseek-chat"
        receipt = ("Crossfeed model receipt: requested deepseek/deepseek-chat; "
                   "ran on deepseek/deepseek-chat (provider reported); selector deepseek/deepseek-chat; "
                   "exit 0; run %s." % RUN_ID)
        self.reply_worker('{"answer": 57}\n' + receipt + "\n")
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertTrue(row["pass"], row)
        self.assertEqual(row["ran_on"], self.option["model"])
        self.assertEqual(row["run_id"], RUN_ID)
        self.assertNotIn("excluded", row)

    def test_receipt_mismatch_excluded_before_nonzero_worker_check(self):
        self.reply_worker('{"answer": 42}\n' + model_receipt("other", 7) + "\n", exit_code=7)
        with mock.patch.object(run_grid.subprocess, "run") as checker:
            row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertFalse(row["pass"])
        self.assertTrue(row["excluded"])
        self.assertEqual(row["reason"], "receipt model mismatch")
        self.assertEqual(row["ran_on"], "other")
        self.assertEqual(row["run_id"], RUN_ID)
        self.assertEqual(row["exit_code"], 7)
        checker.assert_not_called()

    def test_receipt_identity_retained_on_worker_failure(self):
        self.reply_worker(model_receipt(exit_code=7) + "\n", exit_code=7)
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertFalse(row["pass"])
        self.assertEqual(row["reason"], "worker exited with code 7")
        self.assertEqual(row["ran_on"], "fixture")
        self.assertEqual(row["run_id"], RUN_ID)

    def test_checker_reply_preserves_original_bytes(self):
        for with_receipt in (False, True):
            with self.subTest(with_receipt=with_receipt):
                original = b"literal\xff\r\nanswer\r\n"
                stdout = original + (model_receipt().encode() + b"\r\n" if with_receipt else b"")
                self.option["command"] = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(%r)" % stdout]
                def check(command, **kwargs):
                    self.assertEqual(Path(command[-1]).read_bytes(), stdout)
                    return subprocess.CompletedProcess(command, 0, '{"pass": true, "reason": "ok"}', "")
                with mock.patch.object(run_grid.subprocess, "run", side_effect=check):
                    row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
                self.assertTrue(row["pass"], row)

    def test_final_file_uses_stdout_receipt_and_strips_its_own_receipt(self):
        for final in ('{"answer": 42}\n', '{"answer": 42}\n' + model_receipt() + "\n"):
            with self.subTest(final=final):
                self.reply_worker("Wrapper output\n" + model_receipt() + "\n", final=final)
                row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
                self.assertTrue(row["pass"], row)
                self.assertEqual(row["ran_on"], "fixture")
                self.assertEqual(row["run_id"], RUN_ID)

    def test_final_file_does_not_hide_stdout_receipt_mismatch(self):
        self.reply_worker(model_receipt("other") + "\n", final='{"answer": 42}\n')
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertTrue(row["excluded"])
        self.assertEqual(row["reason"], "receipt model mismatch")
        self.assertEqual(row["ran_on"], "other")
        self.assertEqual(row["run_id"], RUN_ID)

    def test_matching_stdout_does_not_hide_final_receipt_mismatch(self):
        self.reply_worker(model_receipt() + "\n", final='{"answer": 42}\n' + model_receipt("other") + "\n")
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertTrue(row["excluded"])
        self.assertEqual(row["reason"], "receipt model mismatch")
        self.assertEqual(row["ran_on"], "other")
        self.assertEqual(row["run_id"], RUN_ID)

    def test_expert_task_metadata_carried_into_result(self):
        meta = {"family": "reasoning", "difficulty": "expert", "seed": 1,
                "template_id": "schedule", "tier": "expert"}
        (self.task / "meta.json").write_text(json.dumps(meta))
        self.reply_worker('{"answer": 42}\n')
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertTrue(row["pass"], row)
        self.assertEqual(row["difficulty"], "expert")
        self.assertEqual(row["tier"], "expert")
        self.assertEqual(row["template_id"], "schedule")

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_timeout_kills_descendant_that_ignores_term(self):
        sentinel = self.base / "descendant-survived"
        child = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(3); Path(%r).touch()" % str(sentinel)
        script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); time.sleep(30)" % child
        self.option["timeout_s"] = 0.2
        self.option["command"] = [sys.executable, "-c", script]
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertEqual(row["exit_code"], 124)
        import time
        time.sleep(3.1)
        self.assertFalse(sentinel.exists())

    def test_timeout_and_descendant_cleanup(self):
        self.option["timeout_s"] = 0.15
        self.option["command"] = [sys.executable, "-c", "import time; time.sleep(10)"]
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertFalse(row["pass"])
        self.assertEqual(row["exit_code"], 124)
        self.assertLess(row["duration_s"], 4)

    def test_model_stand_in_excluded(self):
        self.option["command"] = [sys.executable, "-c",
            "import sys; print('reply'); print('Crossfeed: this run used other, not fixture (off).', file=sys.stderr)"]
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertTrue(row["excluded"])
        self.assertEqual(row["actual_model"], "other")

    def test_usage_sidecar_and_option_identity(self):
        self.option["command"] = [sys.executable, "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('{\"tokens_in\": 100, \"tokens_out\": 25}'); print('answer')",
            "{usage_file}"]
        # Escape literal JSON braces as command template syntax.
        self.option["command"][2] = self.option["command"][2].replace('{"', '{{"').replace('25}', '25}}')
        with mock.patch.object(run_grid.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"pass": true, "reason": "ok"}', "")):
            row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertEqual(row.get("tokens_in"), 100, row)
        self.assertEqual(row.get("tokens_out"), 25, row)

    def test_template_values_are_literal_arguments(self):
        opt = dict(self.option, command=["worker", "{model}", "{prompt_file}"])
        rendered = run_grid.render_command(opt, {"model": "$(touch stolen)", "prompt_file": "/space here/prompt"})
        self.assertEqual(rendered, ["worker", "$(touch stolen)", "/space here/prompt"])

    def test_telemetry_validates_types_and_aggregates_turns(self):
        events = self.base / "events.jsonl"
        usage = self.base / "usage.json"
        events.write_text('\n'.join(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}) for _ in range(2)))
        self.assertEqual(run_grid.telemetry(usage, events), {"tokens_in": 20, "tokens_out": 6})
        usage.write_text('{"tokens_in": true, "tokens_out": 1}')
        with self.assertRaises(ValueError):
            run_grid.telemetry(usage, events)

    def test_cached_input_counts_use_same_list_price_proxy(self):
        events = self.base / "events.jsonl"
        usage = self.base / "usage.json"
        events.write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 25}}))
        codex = run_grid.telemetry(usage, events)
        events.write_text(json.dumps({"type": "step_finish", "part": {"tokens": {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 800, "write": 100}}}}))
        self.assertEqual(run_grid.telemetry(usage, events), codex)
        events.write_text(json.dumps({"type": "step_finish", "part": {"tokens": {"input": 100, "output": 20, "cache": {"read": True}}}}))
        with self.assertRaises(ValueError):
            run_grid.telemetry(usage, events)

    def test_workspace_root_symlink_rejected(self):
        import shutil
        target = self.base / "hidden"
        target.mkdir()
        (target / "expectations.json").write_text("hidden")
        shutil.rmtree(self.task / "workspace")
        (self.task / "workspace").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            run_grid.discover_tasks(self.tasks)

    def test_malformed_event_is_missing_telemetry_not_grid_abort(self):
        self.option["command"] = [sys.executable, "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('[]'); print('reply')", "{events_file}"]
        with mock.patch.object(run_grid.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"pass": true, "reason": "ok"}', "")):
            row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertTrue(row["pass"], row)
        self.assertIn("telemetry_error", row)
        self.assertNotIn("tokens_in", row)

    def test_environment_changes_resume_provenance_and_dry_run_hides_values(self):
        self.option["command"] = ["$BENCH_TEST_BINARY", "{workdir}"]
        with mock.patch.dict(os.environ, {"BENCH_TEST_BINARY": "/first"}):
            first = run_grid.option_digest(self.option)
            planned = run_grid.render_command(self.option, {"workdir": "fixture"}, True)
        with mock.patch.dict(os.environ, {"BENCH_TEST_BINARY": "/second"}):
            second = run_grid.option_digest(self.option)
        self.assertNotEqual(first, second)
        self.assertEqual(planned[0], "$BENCH_TEST_BINARY")

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_wrapper_cleanup_finishes_for_separate_child_group(self):
        sentinel = self.base / "separate-group-survived"
        child = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(3); Path(%r).touch()" % str(sentinel)
        script = (
            "import os,signal,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-c',%r], start_new_session=True)\n"
            "def stop(*args):\n"
            " time.sleep(0.3)\n"
            " os.killpg(child.pid,signal.SIGKILL)\n"
            " child.wait()\n"
            " sys.exit(0)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            "time.sleep(30)\n" % child)
        self.option["timeout_s"] = 0.2
        self.option["command"] = [sys.executable, "-c", script]
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option)
        self.assertEqual(row["exit_code"], 124)
        import time
        time.sleep(3.1)
        self.assertFalse(sentinel.exists())

    def test_locked_output_refuses_second_writer(self):
        self.out.with_name(self.out.name + ".lock").write_text("another writer")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli()
        self.assertFalse(self.out.exists())


class WrapperTests(unittest.TestCase):
    def test_wrapper_flags_and_prompt_argument_without_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            prompt = Path(temporary) / "prompt"
            prompt.write_text('literal $(touch nope) "quotes"')
            for harness, effort in (("codex", "--reasoning"), ("claude", "--effort"),
                                    ("agy", "--effort"), ("opencode", "--variant")):
                with self.subTest(harness=harness), mock.patch.object(wrapper.os, "execv") as call:
                    wrapper.main([harness, "--scripts", "/wrappers", "--prompt-file", str(prompt),
                                  "--dir", temporary, "--model", "example", "--level", "high"])
                    command = call.call_args.args[1]
                    self.assertIn(effort, command)
                    self.assertEqual(command[0], "/wrappers/%s-agent.sh" % harness)
                    if harness == "agy":
                        self.assertEqual(command[command.index("--prompt") + 1], prompt.read_text())

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_real_adapter_replacement_allows_child_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "escaped-child-survived"
            child = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(3); Path(%r).touch()" % str(sentinel)
            fake = root / "agy-agent.sh"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import os,signal,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c',%r], start_new_session=True)\n"
                "def stop(*args):\n"
                " time.sleep(0.3)\n"
                " os.killpg(child.pid,signal.SIGKILL)\n"
                " child.wait()\n"
                " sys.exit(0)\n"
                "signal.signal(signal.SIGTERM,stop)\n"
                "time.sleep(30)\n" % child)
            fake.chmod(0o755)
            prompt = root / "PROMPT.md"
            prompt.write_text("fixture prompt")
            command = [sys.executable, str(Path(wrapper.__file__).resolve()), "agy", "--scripts", str(root),
                       "--prompt-file", str(prompt), "--dir", str(root), "--model", "fixture", "--level", "high"]
            code = run_grid.execute(command, root, root / "reply", root / "errors", timeout=.2)
            self.assertEqual(code, 124)
            import time
            time.sleep(3.1)
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
