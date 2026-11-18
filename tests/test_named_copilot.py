"""Account-specific Copilot selection through the real wrapper, with no provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
S = ROOT / 'scripts'


class NamedCopilotTests(unittest.TestCase):
    def test_operator_must_explicitly_declare_funding_and_metered_is_not_selectable(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            command = [sys.executable, S/'configure-copilot-models.py', '--roster',
                       ROOT/'examples/access-overlay.example.json', '--pool', 'github-copilot-company',
                       '--max-credits', '3', '--verified-model', 'worker-a']
            missing = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(missing.returncode, 2)
            self.assertIn('--funding', missing.stderr)
            invalid = subprocess.run(command + ['--funding', 'unknown'], capture_output=True, text=True, timeout=30)
            self.assertEqual(invalid.returncode, 2)
            overlay = w / 'roster.json'
            overlay.write_bytes(subprocess.check_output(command + ['--funding', 'metered'], timeout=30))
            roster = json.loads(overlay.read_bytes())
            self.assertEqual(roster['quota_pools']['github-copilot-company']['funding'], 'metered')
            select = [sys.executable, S/'fleetctl.py', '--overlay', overlay, '--state-dir', w/'state',
                      'select', '--no-refresh', '--role', 'review',
                      '--allow', 'github-copilot-company:github-copilot-company-worker-a:*']
            refused = subprocess.run(select, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('access or admission not active/verified', refused.stderr)
            for mode in ('auto', 'worker-a'):
                fake = w / 'copilot'
                marker = w / 'launched'
                fake.write_text('#!/bin/sh\ntouch "$LAUNCH_MARKER"\n')
                fake.chmod(0o755)
                env = dict(os.environ, HOME=str(w), ACCESS_OVERLAY=str(overlay), FLEET_STATE_DIR=str(w/'state'),
                           PATH=f'{w}:{os.environ["PATH"]}', LAUNCH_MARKER=str(marker))
                direct = subprocess.run([S/'copilot-agent.sh', '--model', mode, '--prompt', 'synthetic'],
                                        env=env, cwd=w, text=True, capture_output=True, timeout=30)
                self.assertNotEqual(direct.returncode, 0, direct.stderr)
                self.assertFalse(marker.exists())

    def test_account_selects_two_named_workers_but_student_cannot(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            stub = w / 'copilot'
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_FILE"\n' +
                            "printf '%s\\n' '{\"type\":\"assistant.message\",\"data\":{\"content\":\"ok\"}}' '{\"type\":\"result\",\"exitCode\":0}'\n")
            stub.chmod(0o755)
            roster = w / 'roster.json'
            example = ROOT / 'examples/access-overlay.example.json'
            raw = subprocess.check_output([sys.executable, S/'configure-copilot-models.py', '--roster', example,
                                           '--pool', 'github-copilot-company', '--max-credits', '3',
                                           '--funding', 'included', '--verified-model', 'worker-a', '--verified-model', 'worker-b'])
            roster.write_bytes(raw)
            env = dict(os.environ, PATH=f'{w}:{os.environ["PATH"]}', FLEET_STATE_DIR=str(w/'state'),
                       ACCESS_OVERLAY=str(roster), FLEET_NO_AUTO_REFRESH='1', ARGV_FILE=str(w/'args'))
            def run(*args):
                return subprocess.run(args, cwd=w, env=env, capture_output=True, text=True, timeout=30)
            for model in ('worker-a', 'worker-b'):
                proc = run(sys.executable, S/'fleetctl.py', 'select', '--role', 'review', '--allow',
                           f'github-copilot-company:github-copilot-company-{model}:*', '--no-refresh', '--json')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                choice = json.loads(proc.stdout)['choice']
                self.assertEqual(choice['pool'], 'github-copilot-company')
                self.assertEqual(choice['cash']['funding'], 'included')
                cmd = ['fixture' if arg == '<task>' else arg for arg in choice['command_argv']]
                proc = run(*cmd)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                argv = (w/'args').read_text().splitlines()
                self.assertEqual(argv[argv.index('--model')+1], model)
                self.assertEqual(argv[argv.index('--max-ai-credits')+1], '3')
                self.assertIn('--deny-tool=write', argv)
            (w/'args').unlink()
            for mode in ('unverified', 'student'):
                config = json.loads(raw)
                if mode == 'student': config = json.loads(example.read_text())
                else: config['lanes'][-1]['access_status'] = 'unknown'
                roster.write_text(json.dumps(config))
                proc = run(S/'copilot-agent.sh', '--model', 'worker-b', '--prompt', 'fixture')
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse((w/'args').exists(), 'must not contact provider')


if __name__ == '__main__':
    unittest.main()
