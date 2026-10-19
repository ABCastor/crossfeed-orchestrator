"""The owner-approved local benchmark policy has one documented implementation home."""
from pathlib import Path
import unittest

from scripts import benchmark_page


class BenchmarkPolicyTests(unittest.TestCase):
    def test_admission_rule_is_documented_and_referenced(self):
        # BR-BENCH-001
        root = Path(__file__).resolve().parents[1]
        ledger = (root / 'BUSINESS-RULES.md').read_text()
        rule = ledger.split('## BR-BENCH-001', 1)[1].split('\n## ', 1)[0].lower()
        for requirement in (
            'do not start new broad local benchmark campaigns',
            'one small admission check',
            'a handful of tasks on cheap lanes',
            'keep a receipt',
            'only when the owner asks',
        ):
            self.assertIn(requirement, rule)

        implementation = (root / 'scripts' / 'benchmark_page.py').read_text()
        self.assertIn('# BR-BENCH-001', implementation)
        self.assertIn('BR-BENCH-001', ledger)
        rendered = benchmark_page.render_body({'rows': []})
        self.assertIn('newly admitted models get one small admission check', rendered)
        self.assertIn('Our measured results stay visible and labelled as ours', rendered)


if __name__ == '__main__':
    unittest.main()
