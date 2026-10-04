"""Validate repository answers by resolving only generated public source."""
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from bench.check import check_task
from bench.families import repo_qa

TRACE_BATCH = '''
import importlib
import json
from pathlib import Path

calls = []
def instrument(function):
    def traced(payload):
        outcome = function(payload)
        calls.append((payload['id'], payload['revision'], outcome['accepted'],
                      function.__module__.replace('.', '/') + '.py:' + function.__name__))
        return outcome
    return traced

for path in sorted(Path('handlers').glob('*.py')):
    if path.stem == '__init__':
        continue
    module = importlib.import_module('handlers.' + path.stem)
    for name, function in list(vars(module).items()):
        if callable(function) and function.__module__ == module.__name__:
            setattr(module, name, instrument(function))

import entry
scenario = json.loads(Path('scenario.json').read_text())
outcomes = entry.process_batch()
ident = scenario['target_event']
revision = outcomes[ident]['revision']
assert revision == (3 if any(event['revision'] == 4 and event['id'] == ident for event in scenario['events']) else 1)
successful = [call[3] for call in calls if call[0] == ident and call[1] == revision and call[2]]
assert len(successful) == 1
print(successful[0])
'''


def put(root, files):
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')


class RepoQaFamilyTests(unittest.TestCase):
    def fixtures(self):
        for seed in (1, 2):
            for template in range(4):
                for tier in repo_qa.TIERS:
                    yield seed, template, tier, repo_qa.make(random.Random('%s:%s:%s' % (seed, template, tier)), template, tier)

    def test_references_nulls_and_independent_runtime_resolution(self):
        count = 0
        with tempfile.TemporaryDirectory(prefix='repo-qa-family-') as temporary:
            root = Path(temporary)
            empty = root / 'empty.txt'
            empty.write_text(' \n\t', encoding='utf-8')
            for seed, template, tier, fixture in self.fixtures():
                with self.subTest(seed=seed, template=repo_qa.TEMPLATE_IDS[template], tier=tier):
                    prompt, workspace, check, reference, reply = fixture
                    task = root / ('%s-%s-%s' % (seed, template, tier))
                    task.mkdir()
                    put(task / 'workspace', workspace)
                    put(task / 'reference', reference)
                    (task / 'PROMPT.md').write_text(prompt, encoding='utf-8')
                    (task / 'check.json').write_text(json.dumps(check), encoding='utf-8')
                    reply_path = task / 'reply.txt'
                    reply_path.write_text(reply, encoding='utf-8')
                    self.assertEqual(reference, workspace)
                    self.assertTrue(check_task(task, task / 'reference', reply_path)['pass'])
                    self.assertFalse(check_task(task, task / 'reference', empty)['pass'])
                    self.assertGreaterEqual(len(workspace), 10)
                    self.assertLessEqual(len(workspace), 60)
                    self.assertNotIn(check['answer'], prompt)
                    module, symbol = check['answer'].split(':')
                    self.assertIn(module, workspace)
                    self.assertIn('def ' + symbol + '(', workspace[module])
                    script = (TRACE_BATCH if tier in ('hard', 'expert') else
                              "import entry; fn = entry.resolve(); "
                              "print(fn.__module__.replace('.', '/') + '.py:' + fn.__name__)")
                    resolved = subprocess.run([sys.executable, '-B', '-c', script],
                        cwd=str(task / 'workspace'), text=True, capture_output=True, timeout=10)
                    self.assertEqual(resolved.returncode, 0, resolved.stderr)
                    self.assertEqual(resolved.stdout.strip(), check['answer'])
                    count += 1
        self.assertEqual(count, 32)

    def test_determinism_and_seeded_worker_inputs(self):
        for template in range(4):
            for tier in repo_qa.TIERS:
                with self.subTest(template=template, tier=tier):
                    first = repo_qa.make(random.Random(1), template, tier)
                    repeated = repo_qa.make(random.Random(1), template, tier)
                    other = repo_qa.make(random.Random(2), template, tier)
                    self.assertEqual(first, repeated)
                    self.assertNotEqual(first[1], other[1])
                    self.assertNotEqual(first[2]['answer'], other[2]['answer'])

    def test_tiers_change_active_resolution_semantics(self):
        self.assertEqual(len(repo_qa.TEMPLATE_IDS), 4)
        for template, active_file in enumerate(('settings.py', 'dispatch.py', 'container.py', 'routing.py')):
            fixtures = [repo_qa.make(random.Random(10), template, tier) for tier in repo_qa.TIERS]
            with self.subTest(template=template):
                signatures = [(fixture[1][active_file], tuple(sorted((path, source) for path, source in fixture[1].items()
                                                                     if path.endswith('.json')))) for fixture in fixtures]
                self.assertEqual(len(set(signatures)), 4)
                sizes = [len(fixture[1]) for fixture in fixtures]
                self.assertEqual(sizes, sorted(set(sizes)))

    def test_hard_and_expert_query_invocation_not_returned_callable(self):
        for template in range(4):
            for tier in ('hard', 'expert'):
                with self.subTest(template=template, tier=tier):
                    prompt, workspace, check, reference, reply = repo_qa.make(random.Random(10), template, tier)
                    self.assertIn('final accepted result', prompt)
                    self.assertNotIn('entry.py:resolve()', prompt)
                    self.assertNotIn('def resolve()', workspace['entry.py'])
                    scenario = json.loads(workspace['scenario.json'])
                    target_events = [event for event in scenario['events'] if event['id'] == scenario['target_event']]
                    if tier == 'expert':
                        self.assertEqual([event['revision'] for event in target_events], [1, 3, 2, 4])
                        self.assertTrue(all(attempt['phase'] == 'prepare' for attempt in target_events[-1]['attempts']))
                    self.assertTrue(any(attempt['context'] for event in target_events for attempt in event['attempts']))

    def test_each_expert_template_can_differ_from_default_selection(self):
        with tempfile.TemporaryDirectory(prefix='repo-qa-default-') as temporary:
            root = Path(temporary)
            for template in range(4):
                differences = 0
                for seed in (1, 2):
                    fixture = repo_qa.make(random.Random('%s:%s:expert' % (seed, template)), template, 'expert')
                    directory = root / ('%s-%s' % (template, seed))
                    put(directory, fixture[1])
                    default = subprocess.run([sys.executable, '-B', '-c',
                        "import entry,json; from pathlib import Path; "
                        "fn = entry.select(json.loads(Path('scenario.json').read_text())); "
                        "print(fn.__module__.replace('.', '/') + '.py:' + fn.__name__)"],
                        cwd=str(directory), text=True, capture_output=True, timeout=10)
                    self.assertEqual(default.returncode, 0, default.stderr)
                    differences += default.stdout.strip() != fixture[2]['answer']
                with self.subTest(template=template):
                    self.assertGreater(differences, 0, 'default callable metadata answers the entire expert template')


if __name__ == '__main__':
    unittest.main()
