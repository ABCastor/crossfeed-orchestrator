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
                                           '--verified-model', 'worker-a', '--verified-model', 'worker-b'])
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
