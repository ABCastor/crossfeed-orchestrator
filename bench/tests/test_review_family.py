"""Review locations must identify an executable, behavior-changing regression."""
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from bench.check import check_task, same_value
from bench.families import review


def write_files(root, files):
    root.mkdir(parents=True)
    for name, source in files.items():
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')


def behavior(root, function, cases):
    script = ('import copy,json,sys\n'
              'sys.path.insert(0,sys.argv[1])\n'
              'import service\n'
              'cases=json.loads(sys.stdin.read())\n'
              'original=copy.deepcopy(cases)\n'
              'values=[getattr(service,sys.argv[2])(*args) for args in cases]\n'
              'print(json.dumps({"values":values,"unchanged":cases==original}))\n')
    proc = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(root), function], input=json.dumps(cases), text=True, capture_output=True, timeout=10)
    if proc.returncode:
        raise AssertionError('runtime exit %d: %s' % (proc.returncode, proc.stderr))
    return json.loads(proc.stdout)


class ReviewFamilyTests(unittest.TestCase):
    def test_review_matrix_and_regression_behavior(self):
        counts = {'references': 0, 'empty': 0, 'regressions': 0, 'repairs': 0, 'baselines': 0}
        with tempfile.TemporaryDirectory(prefix='crossfeed-review-matrix-') as temp:
            root = Path(temp)
            for seed in (1, 2):
                for index, template in enumerate(review.TEMPLATE_IDS):
                    for tier in review.TIERS:
                        name = '%s-%s-%s' % (seed, template, tier)
                        with self.subTest(fixture=name):
                            prompt, workspace, check, reference, reply = review.make(random.Random(seed), index, tier)
                            task = root/name
                            task.mkdir()
                            write_files(task/'workspace', workspace)
                            write_files(task/'reference', reference)
                            (task/'check.json').write_text(json.dumps(check), encoding='utf-8')
                            answer, empty = task/'answer.txt', task/'empty.txt'
                            answer.write_text(reply, encoding='utf-8')
                            empty.write_text('', encoding='utf-8')
                            self.assertTrue(check_task(task, task/'reference', answer)['pass'])
                            counts['references'] += 1
                            self.assertFalse(check_task(task, task/'reference', empty)['pass'])
                            counts['empty'] += 1
                            probe = check['validation']
                            buggy = workspace[check['bug_file']].splitlines()
                            self.assertEqual(buggy[check['bug_line']-1], probe['buggy_line'])
                            fixed = list(buggy)
                            fixed[check['bug_line']-1] = probe['fixed_line']
                            self.assertEqual([i for i, (a, b) in enumerate(zip(buggy, fixed)) if a != b], [check['bug_line']-1])
                            self.assertFalse(probe['buggy_line'].lstrip().startswith('#'))
                            if tier in ('hard', 'expert'):
                                executable_additions = [line for line in workspace['PATCH.diff'].splitlines() if line.startswith('+') and not line.startswith('+++') and line[1:].strip() and not line[1:].lstrip().startswith('#')]
                                self.assertGreaterEqual(len(executable_additions), 3)
                            if tier == 'expert':
                                self.assertGreaterEqual(len([key for key in workspace if key.startswith('new/') and key.endswith('.py')]), 3)
                            observed = behavior(task/'workspace/new', probe['function'], probe['cases'])
                            self.assertTrue(observed['unchanged'])
                            self.assertFalse(same_value(observed['values'], probe['expected']))
                            counts['regressions'] += 1
                            fixed_files = dict(workspace)
                            fixed_files[check['bug_file']] = '\n'.join(fixed)+'\n'
                            write_files(task/'corrected', fixed_files)
                            observed = behavior(task/'corrected/new', probe['function'], probe['cases'])
                            self.assertTrue(observed['unchanged'])
                            self.assertTrue(same_value(observed['values'], probe['expected']), (observed['values'], probe['expected']))
                            counts['repairs'] += 1
                            observed = behavior(task/'workspace/old', probe['function'], probe['cases'])
                            self.assertTrue(observed['unchanged'])
                            self.assertTrue(same_value(observed['values'], probe['expected']), (observed['values'], probe['expected']))
                            counts['baselines'] += 1
        print('Review matrix: references=%d empty_replies_rejected=%d regressions_proven=%d one_line_repairs=%d old_baselines=%d' % tuple(counts.values()))
        self.assertEqual(set(counts.values()), {32})

    def test_determinism_and_seeded_scenarios(self):
        for index in range(len(review.TEMPLATE_IDS)):
            for tier in review.TIERS:
                with self.subTest(template=index, tier=tier):
                    one = review.make(random.Random(1), index, tier)
                    again = review.make(random.Random(1), index, tier)
                    two = review.make(random.Random(2), index, tier)
                    self.assertEqual(one, again)
                    self.assertNotEqual(one[1]['scenarios.json'], two[1]['scenarios.json'])

    def test_unknown_tier_rejected(self):
        with self.assertRaises(ValueError):
            review.make(random.Random(1), 0, 'impossible')
