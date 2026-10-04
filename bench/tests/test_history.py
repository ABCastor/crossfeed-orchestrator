import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from bench.check import check_task
from bench.history import (boundary_snapshot, boundary_unchanged, difficulty,
                           make_candidate, mine, Rejected, safe_path, run_tests, test_command, export, tree)
from bench.run_grid import discover_tasks, measure


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'input'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo/'tests').mkdir()
        (self.repo/'tests/__init__.py').write_text('')
        (self.repo/'solution.py').write_text('def add(a, b):\n    return a - b\n')
        (self.repo/'tests/test_add.py').write_text('import unittest\nfrom solution import add\nclass Add(unittest.TestCase):\n    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n')
        (self.repo/'.gitignore').write_text('ignored.txt\n.env*\n')
        (self.repo/'ignored.txt').write_text('do not export')
        (self.repo/'.env').write_text('PRIVATE=fixture-value')
        self.git('add', '--all')
        self.git('add', '-f', 'ignored.txt', '.env')
        self.git('commit', '-qm', 'Parent')
        (self.repo/'solution.py').write_text('def add(a, b):\n    return a + b\n')
        with (self.repo/'tests/test_add.py').open('a') as out:
            out.write('    def test_sum(self):\n        self.assertEqual(add(1, 2), 3)\n')
        self.git('add', '--all')
        self.git('commit', '-qm', 'Fix with a deliberately informative subject')
        self.commit = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE).decode()

    def task(self):
        return make_candidate(self.repo, self.commit, self.root/'task', timeout=5)

    def test_real_fail_to_pass_and_input_unchanged(self):
        before = self.git('status', '--porcelain')
        task = self.task()
        ref = task/'reference/reply.txt'
        self.assertTrue(check_task(task, task/'reference/workspace', ref)['pass'])
        self.assertFalse(check_task(task, task/'workspace', ref)['pass'])
        empty = self.root/'empty.txt'
        empty.write_text('')
        self.assertFalse(check_task(task, task/'workspace', empty)['pass'])
        self.assertEqual(before, self.git('status', '--porcelain'))
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.commit)
        for root in (task/'workspace', task/'reference/workspace'):
            self.assertFalse((root/'.git').exists())
            self.assertFalse((root/'.env').exists())
            self.assertFalse((root/'ignored.txt').exists())
        prompt = (task/'PROMPT.md').read_text()
        self.assertNotIn('deliberately informative subject', prompt)
        self.assertIn('AssertionError', prompt)
        self.assertLessEqual(len(json.loads((task/'mining.json').read_text())['parent']['tail'].splitlines()), 60)
        self.assertEqual(discover_tasks(task)[0][1]['family'], 'history-fix')
        self.assertIn('return a + b', (task/'reference.patch').read_text())
        self.assertNotIn('test_sum', (task/'reference.patch').read_text())

    def test_test_integrity_and_scope_measurement(self):
        task = self.task()
        candidate = self.root/'candidate'
        shutil.copytree(task/'reference/workspace', candidate)
        (candidate/'extra.py').write_text('anything = 1\n')
        verdict = check_task(task, candidate, task/'reference/reply.txt')
        self.assertTrue(verdict['pass'])
        self.assertEqual(verdict['scope_extra_files'], ['extra.py'])
        (candidate/'tests/test_add.py').write_text('')
        self.assertFalse(check_task(task, candidate, task/'reference/reply.txt')['pass'])

    def test_grid_checks_history_repairs_and_sibling_escape(self):
        import sys
        task = self.task()
        command = 'from pathlib import Path; Path("solution.py").write_text("def add(a, b):\\n    return a + b\\n"); print("Applied")'
        option = dict(id='repair', model='synthetic', level='test', pool='synthetic',
                      command=[sys.executable, '-c', command], timeout_s=5)
        verdict = measure(discover_tasks(task)[0], option, self.root/'cells-good')
        self.assertTrue(verdict['pass'], verdict)
        self.assertEqual(verdict['scope_extra_files'], [])
        self.assertFalse(verdict['scope_global_enforced'])
        option['command'] = [sys.executable, '-c', command+'; Path("../escaped.txt").write_text("bad")']
        verdict = measure(discover_tasks(task)[0], option, self.root/'cells-escape')
        self.assertFalse(verdict['pass'], verdict)
        self.assertEqual(verdict['reason'], 'outside repo edited')

    def test_still_red_reference_is_rejected(self):
        (self.repo/'solution.py').write_text('def add(a, b):\n    return a * b\n')
        self.git('add', 'solution.py')
        self.git('commit', '--amend', '--no-edit', '-q')
        commit = self.git('rev-parse', 'HEAD').strip()
        with self.assertRaisesRegex(Rejected, 'reference_tests_fail'):
            make_candidate(self.repo, commit, self.root/'task', timeout=5)

    def test_parent_green_is_rejected_and_counted(self):
        (self.repo/'tests/test_add.py').write_text('import unittest\nclass Safe(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n')
        self.git('add', '--all')
        self.git('commit', '--amend', '--no-edit', '-q')
        tasks, summary = mine(self.repo, self.root/'mined', maximum=1, timeout=5)
        self.assertEqual(tasks, [])
        self.assertEqual(summary['candidates'], 1)
        self.assertEqual(summary['rejected_by_reason'], {'parent_tests_green': 1})

    def test_boundary_and_safe_files(self):
        workspace = self.root/'worker'
        workspace.mkdir()
        evidence = self.root/'reply.txt'
        before = boundary_snapshot(self.root, workspace, [evidence])
        (workspace/'source').write_text('allowed')
        evidence.write_text('allowed transport')
        self.assertTrue(boundary_unchanged(before, self.root, workspace, [evidence]))
        (self.root/'escaped').write_text('no')
        self.assertFalse(boundary_unchanged(before, self.root, workspace, [evidence]))
        for name in ('.env.production', '.git/config', 'key.pem', '../outside', 'credentials.json'):
            self.assertFalse(safe_path(name))
        self.assertEqual(difficulty(15, 1), 'easy')
        self.assertEqual(difficulty(81, 2), 'hard')
        self.assertEqual(difficulty(15, 7), 'expert')

    def test_unsafe_cli_repo_is_rejected(self):
        p = subprocess.run(['python3', 'bench/history.py', '--repo', str(self.repo), '--out', str(self.root/'out')], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn('explicit --allow-repo approval', p.stderr)

    def test_source_local_excludes_and_credential_names(self):
        (self.repo/'private-state.txt').write_text('private fixture')
        self.git('add', 'private-state.txt')
        self.git('commit', '--amend', '--no-edit', '-q')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        (self.repo/'.git/info').mkdir(exist_ok=True)
        with (self.repo/'.git/info/exclude').open('a') as out:
            out.write('\nprivate-state.txt\n')
        root = self.root/'export'
        export(self.repo, tree(self.repo, self.commit), root)
        self.assertFalse((root/'private-state.txt').exists())
        with self.assertRaisesRegex(Rejected, 'excluded_changed_file'):
            self.task()
        for name in ('.npmrc', '.netrc', '.pypirc', 'secrets/plain.txt', 'tokens/plain.txt', 'credentials/plain.txt'):
            self.assertFalse(safe_path(name), name)

    def test_saved_patch_must_construct_exact_reference(self):
        (self.repo/'marker.txt').write_text('tracked marker')
        (self.repo/'solution.py').write_text('def add(a, b):\n    return a - b\n')
        self.git('add', '--all')
        self.git('commit', '-qm', 'Parent with marker')
        (self.repo/'.gitignore').write_text('marker.txt\n')
        (self.repo/'solution.py').write_text('def add(a, b):\n    return a + b\n')
        with (self.repo/'tests/test_add.py').open('a') as out:
            out.write('    def test_marker(self):\n        from pathlib import Path\n        self.assertFalse(Path("marker.txt").exists())\n')
        self.git('add', '--all')
        self.git('commit', '-qm', 'Ignore transition')
        candidate = self.git('rev-parse', 'HEAD').strip()
        # Current excludes must differ to exercise the historical ignore transition.
        (self.repo/'.gitignore').write_text('')
        self.git('add', '--all')
        self.git('commit', '-qm', 'Later ignore policy')
        with self.assertRaisesRegex(Rejected, 'reference_patch_export_mismatch'):
            make_candidate(self.repo, candidate, self.root/'task', timeout=5)

    def test_timeout_and_sibling_write_are_rejected(self):
        import sys
        root = self.root/'worker'
        root.mkdir()
        outcome = run_tests(root, [sys.executable, '-c', 'import time; time.sleep(5)'], .05)
        self.assertTrue(outcome['timed_out'])
        outcome = run_tests(root, [sys.executable, '-c', 'from pathlib import Path; Path("../escaped").write_text("probe")'], 3)
        self.assertFalse(outcome['outside_ok'])

    def test_pytest_functions_without_import_are_detected(self):
        import importlib.util
        if importlib.util.find_spec('pytest') is None:
            self.skipTest('pytest not installed')
        root = self.root/'pytest-repo'
        root.mkdir()
        (root/'test_plain.py').write_text('def test_plain():\n    assert 1 == 1\n')
        cmd = test_command(root, ['test_plain.py'])
        self.assertEqual(run_tests(root, cmd, 5)['exit_code'], 0)
        (root/'test_plain.py').write_text('def test_plain():\n    assert 1 == 2\n')
        self.assertNotEqual(run_tests(root, cmd, 5)['exit_code'], 0)
