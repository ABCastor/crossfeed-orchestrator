import json
from pathlib import Path
import tempfile
import unittest

from bench import check, run_grid
from bench.tests import test_run_grid
from bench.tests.test_run_grid import model_receipt, RUN_ID


class FormatTests(unittest.TestCase):
    def verdict(self, text, kind='reasoning'):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'workspace').mkdir()
            data = {'kind': kind, 'answer': 42} if kind != 'extraction' else {'kind': kind, 'expected': {'answer': 42}}
            (root/'check.json').write_text(json.dumps(data))
            (root/'reply.txt').write_text(text)
            return check.check_task(root, root/'workspace', root/'reply.txt')

    def test_fenced_content_passes_but_strict_fails(self):
        for kind in ('extraction', 'reasoning', 'repo-qa'):
            with self.subTest(kind=kind):
                verdict = self.verdict('```json\n{"answer":42}\n```\n' + model_receipt(), kind)
                self.assertTrue(verdict['pass'], verdict)
                self.assertFalse(verdict['strict_pass'])
                self.assertFalse(verdict['format_ok'])
                self.assertEqual(verdict['format_issue'], 'fenced')

    def test_extra_prose_and_strict_final_line(self):
        for text, kind, strict in (
                ('Here: {"answer":42} Thanks.', 'extraction', False),
                ('Here:\n{"answer":42}', 'reasoning', True),
                ('Here:\n{"answer":42}', 'extraction', False),
                ('{"answer":42}', 'extraction', True)):
            verdict = self.verdict(text, kind)
            self.assertTrue(verdict['pass'], verdict)
            self.assertEqual(verdict['strict_pass'], strict)
            self.assertEqual(verdict['format_ok'], strict)
            if not strict:
                self.assertEqual(verdict['format_issue'], 'extra prose')

    def test_ambiguity_and_invalid_json_cannot_be_rescued(self):
        for text in ('{"answer":1}\n{"answer":42}',
                     '```json\n{"answer":42}\n```\n```json\n{"answer":42}\n```',
                     '{"answer":1,"answer":42}',
                     '{"outer":{"answer":42},oops}',
                     '[{"answer":42}]', '{"answer":NaN}'):
            # Extraction still requires one unambiguous object. Answer tasks
            # select the last complete object line under the Brief 4 contract.
            verdict = self.verdict(text, 'extraction')
            self.assertFalse(verdict['pass'], (text, verdict))
            self.assertFalse(verdict['strict_pass'])

    def test_wrong_fenced_content_fails_both(self):
        verdict = self.verdict('```json\n{"answer":1}\n```')
        self.assertFalse(verdict['pass'])
        self.assertFalse(verdict['strict_pass'])
        self.assertFalse(verdict['format_ok'])


class EvidenceTests(unittest.TestCase):
    setUp = test_run_grid.RunnerTests.setUp
    write_options = test_run_grid.RunnerTests.write_options
    cli = test_run_grid.RunnerTests.cli
    reply_worker = test_run_grid.RunnerTests.reply_worker

    def test_failure_retains_only_output_evidence(self):
        self.reply_worker('{"answer":42}\n' + model_receipt(), exit_code=7)
        self.option['command'][2] = "import sys; sys.stderr.write('x'*5000); " + self.option['command'][2]
        self.write_options([self.option])
        self.cli()
        row = json.loads(self.out.read_text())
        cell = Path(row['cell_dir'])
        self.assertEqual(cell.parent, self.out.parent.resolve()/'cells')
        self.assertEqual({p.name for p in cell.iterdir()}, {'reply.txt', 'stderr.tail', 'receipt.json'})
        self.assertIn('{"answer":42}', (cell/'reply.txt').read_text())
        self.assertEqual((cell/'stderr.tail').read_bytes(), b'x'*4096)
        self.assertEqual(json.loads((cell/'receipt.json').read_text())['stdout']['run_id'], RUN_ID)
        self.assertFalse(row['pass'])

    def test_unconfirmed_cell_kept_and_prefix_normalized(self):
        receipts = [
            'Crossfeed model receipt: requested fixture; selected other; underlying model unconfirmed; selector fixture; exit 0; run %s.' % RUN_ID,
            model_receipt('opencode-go/fixture')]
        for receipt in receipts:
            self.reply_worker('{"answer":42}\n' + receipt)
            row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option, self.base)
            self.assertTrue(row['pass'], row)
            self.assertNotIn('excluded', row)
            if 'unconfirmed' in receipt:
                self.assertEqual(row['identity'], 'unconfirmed')
                self.assertNotIn('ran_on', row)

    def test_checker_splits_only_one_receipt(self):
        self.reply_worker('{"answer":42}\n' + model_receipt() + '\n' + model_receipt())
        row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option, self.base)
        self.assertTrue(row['pass'], row)
        self.assertFalse(row['strict_pass'])
        self.assertEqual(row['format_issue'], 'extra prose')

    def test_timeout_and_launch_error_keep_files(self):
        for command in (['missing-crossfeed-test-command'], ['python3', '-c', 'import time; time.sleep(30)']):
            self.option.update(command=command, timeout_s=.1, termination_grace_s=0)
            row = run_grid.measure(run_grid.discover_tasks(self.tasks)[0], self.option, self.base)
            cell = Path(row['cell_dir'])
            self.assertFalse(row['pass'])
            self.assertTrue(all((cell/name).is_file() for name in ('reply.txt', 'stderr.tail', 'receipt.json')))
