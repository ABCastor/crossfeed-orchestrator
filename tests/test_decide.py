"""Decision helper: prompt, dispatch contract, parsing, and the shadow refresh review."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from scripts import decide


class DecideTests(unittest.TestCase):
    def test_parse_accepts_only_listed_answers(self):
        choices = ('PASS', 'FAIL')
        self.assertEqual(decide.parse('noise\n{"answer": "fail", "confidence": 88, "reason": "stale"}', choices),
                         {'answer': 'FAIL', 'confidence': 88, 'reason': 'stale'})
        self.assertIsNone(decide.parse('{"answer": "MAYBE", "confidence": 50}', choices))
        self.assertIsNone(decide.parse('PASS', choices))
        self.assertIsNone(decide.parse('', choices))

    def test_prompt_carries_policy_and_fields(self):
        text = decide.prompt('done', {'Acceptance criterion': 'pytest exits 0', 'Worker report': '40 passed'})
        self.assertIn('Judge the evidence, not the claim', text)
        self.assertIn('pytest exits 0', text)
        self.assertIn('40 passed', text)
        self.assertIn('Compare the final message with every part of what was asked',
                      decide.prompt('keep-going', {'Owner asked': 'x', "Agent's final message": 'y'}))

    def test_dispatch_contract_and_verdict(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen['cmd'] = cmd
            Path(cmd[cmd.index('--last') + 1]).write_text('{"answer": "RETRY", "confidence": 91, "reason": "1 failed"}\n')
            return subprocess.CompletedProcess(cmd, 0, '', '')

        with patch.object(decide.subprocess, 'run', side_effect=fake_run):
            verdict = decide.decide('keep-going', {'Owner asked': 'a', "Agent's final message": 'b'},
                                    lane='opencode-go:deepseek-v4.1-flash:low', data_class='private')
        cmd = seen['cmd']
        self.assertEqual(verdict['answer'], 'RETRY')
        self.assertEqual(verdict['lane'], 'opencode-go:deepseek-v4.1-flash:low')
        for flag, value in (('--allow', 'opencode-go:deepseek-v4.1-flash:low'), ('--mode', 'read-only'),
                            ('--data-class', 'private'), ('--stakes', 'low'), ('--role', 'default')):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn('dispatch', cmd)
        self.assertNotIn('--owner', cmd)

    def test_failures_are_no_opinion(self):
        with patch.object(decide.subprocess, 'run', return_value=subprocess.CompletedProcess([], 5, '', 'refused')):
            self.assertIsNone(decide.decide('done', {'a': 'b'}))
        with patch.object(decide.subprocess, 'run', side_effect=subprocess.TimeoutExpired('x', 1)):
            self.assertIsNone(decide.decide('done', {'a': 'b'}))

        def garbage(cmd, **kwargs):
            Path(cmd[cmd.index('--last') + 1]).write_text('I think it passes.')
            return subprocess.CompletedProcess(cmd, 0, '', '')

        with patch.object(decide.subprocess, 'run', side_effect=garbage):
            self.assertIsNone(decide.decide('done', {'a': 'b'}))


if __name__ == '__main__':
    unittest.main()
