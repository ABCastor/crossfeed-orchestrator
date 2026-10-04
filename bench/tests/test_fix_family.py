"""Repair-family acceptance against the actual isolated checker."""
import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest

from bench.check import check_task
from bench.families import fix


def write_workspace(root, files):
    root.mkdir(parents=True)
    for name, source in files.items():
        target = root/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding='utf-8')


class FixFamilyTests(unittest.TestCase):
    def test_references_original_defects_and_empty_replies(self):
        counts = {'references': 0, 'original_defects': 0, 'empty_replies': 0}
        with tempfile.TemporaryDirectory(prefix='crossfeed-fix-matrix-') as temp:
            root = Path(temp)
            for seed in (1, 2):
                for template in range(len(fix.TEMPLATE_IDS)):
                    for tier in fix.TIERS:
                        name = '%s-%s-%s' % (seed, fix.TEMPLATE_IDS[template], tier)
                        with self.subTest(fixture=name):
                            fixture = fix.make(random.Random(seed), template, tier)
                            prompt, candidate, check, reference, reply = fixture
                            task = root/name
                            task.mkdir()
                            (task/'check.json').write_text(json.dumps(check), encoding='utf-8')
                            write_workspace(task/'workspace', candidate)
                            write_workspace(task/'reference', reference)
                            answer = task/'reply.txt'
                            answer.write_text(reply, encoding='utf-8')
                            empty = task/'empty.txt'
                            empty.write_text('', encoding='utf-8')
                            self.assertEqual(candidate['test_solution.py'], reference['test_solution.py'])
                            self.assertEqual(check['tests']['test_solution.py'], hashlib.sha256(candidate['test_solution.py'].encode()).hexdigest())
                            observed = check_task(task, task/'reference', answer)
                            self.assertTrue(observed['pass'], observed)
                            counts['references'] += 1
                            observed = check_task(task, task/'workspace', answer)
                            self.assertFalse(observed['pass'], observed)
                            counts['original_defects'] += 1
                            observed = check_task(task, task/'reference', empty)
                            self.assertFalse(observed['pass'], observed)
                            counts['empty_replies'] += 1
        print('Fix matrix: references=%d original_defects_rejected=%d empty_replies_rejected=%d' % tuple(counts.values()))
        self.assertEqual(counts, {'references': 32, 'original_defects': 32, 'empty_replies': 32})

    def test_seeded_determinism_and_expert_structure(self):
        for template in range(len(fix.TEMPLATE_IDS)):
            for tier in fix.TIERS:
                with self.subTest(template=template, tier=tier):
                    one = fix.make(random.Random(1), template, tier)
                    again = fix.make(random.Random(1), template, tier)
                    two = fix.make(random.Random(2), template, tier)
                    self.assertEqual(one, again)
                    self.assertNotEqual(one[1], two[1])
                    if tier == 'expert':
                        self.assertGreaterEqual(len(one[1]), 4)  # Three source modules plus immutable tests.
                        self.assertGreaterEqual(len(one[2]['cases']), 40)

    def test_unknown_tier_rejected(self):
        with self.assertRaises(ValueError):
            fix.make(random.Random(1), 0, 'impossible')
