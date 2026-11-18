"""Fault injection into real shell cleanup bodies, with no native model executable."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class CleanupContracts(unittest.TestCase):
    def test_failed_removals_preserve_all_wrapper_statuses(self):
        with tempfile.TemporaryDirectory() as temp:
            for wrapper in ('agy', 'codex', 'claude', 'copilot', 'opencode', 'openrouter'):
                source = (SCRIPTS / f'{wrapper}-agent.sh').read_text()
                body = re.search(r'^(?:_cleanup|cleanup)\(\) \{\n.*?^\}', source, re.M | re.S)[0]
                function = body.split('(')[0]
                for code in (0, 42, 124, 125, 143):
                    with self.subTest(wrapper=wrapper, code=code):
                        script = '''set -e
crossfeed_finish() { return 0; }
_stand_in_notice() { return 0; }
_stop_stderr_stream() { return 0; }
rm() { return 19; }
rmdir() { return 19; }
CROSSFEED_STARTED=0; CROSSFEED_RUN_ID=fixture; CROSSFEED_IDENTITY=''
AGY_SNAPSHOT=snapshot; ptyfile=pty; KILL_STATE=kill; PTY_CHILD_STATE=state
_tmp_events=events; _tmp_runout=run; _tmp_last=last; RUN_LAST=last
RUNERR_PIPE=pipe; RUNERR_PIPE_DIR=dir; RUNERR=err; RUNOUT=out
copilot_home=''; oc_data_home=''; search_dir=''; WATCHDOG_PID=''; lease_token=''
tmp_events=events; stderr_file=err; answer_file=answer; record_error=record
HERE=.; EVENTS=events; PROGRESS=progress
''' + body + f'\ntrap {function} EXIT\nexit {code}\n'
                        result = subprocess.run(['/bin/bash', '-c', script], cwd=temp,
                                                env={**os.environ, 'HOME': temp, 'CODEX_HOME': temp},
                                                capture_output=True, text=True)
                        self.assertEqual(result.returncode, code, result.stderr)

    def test_identity_scratch_removal_failure_is_not_receipt_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            script = f'''set -e
source "{SCRIPTS / 'run-identity.sh'}"
unlink() {{ return 19; }}
CROSSFEED_STARTED=0
CROSSFEED_IDENTITY=private-scratch
crossfeed_finish 42
'''
            result = subprocess.run(['/bin/bash', '-c', script], cwd=temp,
                                    env={**os.environ, 'HOME': temp}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrappers_do_not_echo_captured_model_text_to_stderr(self):
        # Execute the actual native-failure reporting branches with fake private output.
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            output.write_text('PRIVATE PROMPT AND ANSWER\n')
            for wrapper, start, end in (
                ('agy', 'if [ "$rc" -ne 0 ]; then', '\n# In --print mode'),
                ('codex', 'if [ "$rc" -ne 0 ]; then', '\n#'),
            ):
                source = (SCRIPTS / f'{wrapper}-agent.sh').read_text()
                branch = source[source.index(start):]
                branch = branch[:branch.index('\nfi') + 3]
                result = subprocess.run(['/bin/bash', '-c',
                    f'rc=42; ptyfile="{output}"; RUNOUT="{output}"; _clean() {{ cat "$1"; }};\n' + branch],
                    env={**os.environ, 'HOME': temp}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 42)
                self.assertNotIn('PRIVATE PROMPT AND ANSWER', result.stderr)
                self.assertIn('42', result.stderr)

    def test_opencode_signal_before_first_background_job_preserves_signal_status(self):
        source = (SCRIPTS / 'opencode-agent.sh').read_text()
        handler = re.search(r'^_handle_signal\(\) \{\n.*?^\}', source, re.M | re.S)[0]
        result = subprocess.run(['/bin/bash', '-uc',
                                "CHILD_PID=''; CHILD_PGID=''; WATCHDOG_PID='';\n" + handler +
                                '\n_handle_signal TERM 143\nif [ -n "$PENDING_SIGNAL_CODE" ]; then exit "$PENDING_SIGNAL_CODE"; fi'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 143, result.stderr)
