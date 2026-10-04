import json
from pathlib import Path
import tempfile
import unittest

from bench import check
from bench.tests.test_run_grid import model_receipt


FIXTURE = Path(__file__).parent / 'fixtures/brief4_repo_qa_prose_braces_reply.txt'
ANSWER_KINDS = ('repo-qa', 'reasoning', 'review')


class AnswerLineTests(unittest.TestCase):
    def verdict(self, text, kind='reasoning', answer=42):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'workspace').mkdir()
            data = ({'kind': kind, 'bug_file': 'module.py', 'bug_line': 7}
                    if kind == 'review' else {'kind': kind, 'answer': answer})
            (root / 'check.json').write_text(json.dumps(data))
            (root / 'reply.txt').write_text(text)
            return check.check_task(root, root / 'workspace', root / 'reply.txt')

    def object_line(self, kind, correct=True):
        return json.dumps({'bug_file': 'module.py', 'bug_line': 7 if correct else 30}
                          if kind == 'review' else {'answer': 42 if correct else 1})

    def test_real_reply_with_prose_braces_and_receipt(self):
        reply = FIXTURE.read_text()
        self.assertIn('storage: {alias: shared}', reply)
        self.assertIn('Crossfeed model receipt:', reply)
        verdict = self.verdict(reply, 'repo-qa', 'handlers/elm_66848.py:process_72785')
        self.assertTrue(verdict['pass'], verdict)
        self.assertTrue(verdict['strict_pass'], verdict)
        self.assertTrue(verdict['format_ok'], verdict)

    def test_last_object_line_wins_and_ignores_prose_braces(self):
        for kind in ANSWER_KINDS:
            reply = ('storage: {alias: shared}\n' + self.object_line(kind, False)
                     + '\nAn unfinished brace { and a quoted brace "{.\n'
                     + self.object_line(kind) + '\n \t\n' + model_receipt())
            with self.subTest(kind=kind):
                verdict = self.verdict(reply, kind)
                self.assertTrue(verdict['pass'], verdict)
                self.assertTrue(verdict['strict_pass'], verdict)
                self.assertTrue(verdict['format_ok'], verdict)

    def test_trailing_non_whitespace_fails_format_only(self):
        for kind in ANSWER_KINDS:
            for tail in ('Thanks.', '{invalid object}', '{"answer":NaN}', '[]'):
                with self.subTest(kind=kind, tail=tail):
                    verdict = self.verdict(self.object_line(kind) + '\n' + tail, kind)
                    self.assertTrue(verdict['pass'], verdict)
                    self.assertFalse(verdict['strict_pass'], verdict)
                    self.assertFalse(verdict['format_ok'], verdict)
                    self.assertEqual(verdict['format_issue'], 'extra prose')

    def test_last_wrong_object_overrides_earlier_correct_answer(self):
        for kind in ANSWER_KINDS:
            with self.subTest(kind=kind):
                verdict = self.verdict(self.object_line(kind) + '\n'
                                       + self.object_line(kind, False), kind)
                self.assertFalse(verdict['pass'], verdict)
                self.assertFalse(verdict['strict_pass'], verdict)
                self.assertTrue(verdict['format_ok'], verdict)

    def test_fenced_object_line_passes_content_only(self):
        for kind in ANSWER_KINDS:
            with self.subTest(kind=kind):
                verdict = self.verdict('```json\n' + self.object_line(kind) + '\n```', kind)
                self.assertTrue(verdict['pass'], verdict)
                self.assertFalse(verdict['strict_pass'], verdict)
                self.assertFalse(verdict['format_ok'], verdict)
                self.assertEqual(verdict['format_issue'], 'fenced')

    def test_no_complete_object_line_is_rejected(self):
        for kind in ANSWER_KINDS:
            for reply in ('storage: {alias: shared}', 'Here: ' + self.object_line(kind),
                          '[' + self.object_line(kind) + ']',
                          '{"answer":1,"answer":42}', '{"answer":NaN}'):
                with self.subTest(kind=kind, reply=reply):
                    verdict = self.verdict(reply, kind)
                    self.assertFalse(verdict['pass'], verdict)
                    self.assertFalse(verdict['strict_pass'], verdict)

    def test_extraction_still_rejects_prose_braces_and_conflicting_objects(self):
        for reply in ('storage: {alias: shared}\n{"answer":42}',
                      '{"answer":1}\n{"answer":42}',
                      '```json\n{"answer":42}\n```\n```json\n{"answer":42}\n```'):
            with self.subTest(reply=reply):
                with self.assertRaises(ValueError):
                    check.json_answer(reply, 'extraction')


if __name__ == '__main__':
    unittest.main()
