"""Run-local cleanup: bounded ownership, read-only inspection, child-first native deletion."""
import contextlib
import importlib.util
import json
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from tests.fixtures.session_fake import conversation

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class CleanupTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('session_cleanup', SCRIPTS / 'session_cleanup.py')
        self.cleanup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cleanup)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_agy_matches_first_user_marker_only_and_keeps_summaries(self):
        root = self.root
        marker = 'Crossfeed selected model: fixture\nCrossfeed run unique-run.\n=== CROSSFEED TASK ===\ntask'
        conversation(root / 'conversations/old.db', marker)
        before = self.cleanup.agy_snapshot(root)
        conversation(root / 'conversations/mine.db', marker)
        conversation(root / 'conversations/other.db', 'Crossfeed run other-run.')
        conversation(root / 'conversations/late.db', 'unrelated first user')
        conversation(root / 'conversations/late.db', marker)
        # Modified old DBs must also be inspected, even when their ids existed at snapshot.
        conversation(root / 'conversations/old.db', 'new step')
        for id_ in ('mine', 'old'):
            (root / 'brain' / id_).mkdir(parents=True)
            (root / 'brain' / id_ / 'answer').write_text('transcript')
            for folder, ext in (('annotations', '.pbtxt'), ('implicit', '.pb')):
                (root / folder).mkdir(exist_ok=True)
                (root / folder / (id_ + ext)).touch()
            for suffix in ('-wal', '-shm'):
                (root / 'conversations' / (id_ + '.db' + suffix)).touch()
        summary = root / 'conversation_summaries.db'
        summary.write_bytes(b'keep summaries exactly')
        self.cleanup.agy_cleanup(root, before, 'unique-run')
        for id_ in ('mine', 'old'):
            self.assertFalse((root / 'conversations' / (id_ + '.db')).exists())
            self.assertFalse((root / 'brain' / id_).exists())
            self.assertFalse((root / 'annotations' / (id_ + '.pbtxt')).exists())
            self.assertFalse((root / 'implicit' / (id_ + '.pb')).exists())
            self.assertFalse((root / 'conversations' / (id_ + '.db-wal')).exists())
            self.assertFalse((root / 'conversations' / (id_ + '.db-shm')).exists())
        self.assertTrue((root / 'conversations/other.db').exists())
        self.assertTrue((root / 'conversations/late.db').exists())
        self.assertEqual(summary.read_bytes(), b'keep summaries exactly')

    def test_codex_child_first_and_unrelated_threads_untouched(self):
        ids = [f'00000000-0000-0000-0000-{i:012d}' for i in range(1, 5)]
        parent, child, grandchild, unrelated = ids
        with contextlib.closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT, source TEXT, first_user_message TEXT)')
            for id_, ancestor in ((parent, None), (child, parent), (grandchild, child), (unrelated, None)):
                db.execute('INSERT INTO threads VALUES (?, ?, ?)', (id_, json.dumps(
                    {'subagent': {'thread_spawn': {'parent_thread_id': ancestor}}}),
                     'Crossfeed selected model: fixture\nCrossfeed run mine.\n=== CROSSFEED TASK ===' if id_ == parent else 'child task'))
        events = self.root / 'events'
        events.write_text('bad json\n' + json.dumps({'type': 'thread.started', 'thread_id': parent}) + '\n')
        calls = []
        def delete(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0)
        with mock.patch.object(self.cleanup.subprocess, 'run', side_effect=delete):
            self.cleanup.codex_cleanup(self.root, events, 'mine')
        self.assertEqual(calls, [['codex', 'delete', '--force', id_] for id_ in (grandchild, child, parent)])

    def test_codex_stale_shared_events_cannot_delete_unverified_threads(self):
        tid = '00000000-0000-0000-0000-000000000001'
        events = self.root / 'events'
        events.write_text(json.dumps({'type': 'thread.started', 'thread_id': tid}))
        for first in ('owner', 'Crossfeed selected model: fixture\nCrossfeed run other.\n=== CROSSFEED TASK ===',
                      'Crossfeed selected model: fixture\n=== CROSSFEED TASK ===\nCrossfeed run mine.'):
            with self.subTest(first=first):
                with contextlib.closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
                    db.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT, source TEXT, first_user_message TEXT)')
                    db.execute('DELETE FROM threads')
                    db.execute('INSERT INTO threads VALUES (?, ?, ?)', (tid, '{}', first))
                stderr = io.StringIO()
                with mock.patch.object(self.cleanup.subprocess, 'run', return_value=mock.Mock(returncode=0)) as delete, contextlib.redirect_stderr(stderr):
                    self.cleanup.codex_cleanup(self.root, events, 'mine')
                delete.assert_not_called()
                self.assertIn('SESSION CLEANUP SKIPPED: unverified thread', stderr.getvalue())

    def test_agy_owner_quote_of_run_marker_is_not_ownership(self):
        text = 'Owner conversation, quoting Crossfeed run unique-run. later'
        conversation(self.root / 'conversations/owner.db', text)
        self.cleanup.agy_cleanup(self.root, {}, 'unique-run')
        self.assertTrue((self.root / 'conversations/owner.db').exists())

    def test_agy_marker_must_precede_task_delimiter(self):
        text = 'Crossfeed selected model: fixture\n=== CROSSFEED TASK ===\nCrossfeed run unique-run.'
        conversation(self.root / 'conversations/owner.db', text)
        self.cleanup.agy_cleanup(self.root, {}, 'unique-run')
        self.assertTrue((self.root / 'conversations/owner.db').exists())

    def test_missing_run_id_never_authorizes_run_local_deletion(self):
        tid = '00000000-0000-0000-0000-000000000001'
        notice = 'Crossfeed selected model: fixture\nCrossfeed run mine.\n=== CROSSFEED TASK ==='
        events = self.root / 'events'
        events.write_text(json.dumps({'type': 'thread.started', 'thread_id': tid}))
        with contextlib.closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads(id TEXT, source TEXT, first_user_message TEXT)')
            db.execute('INSERT INTO threads VALUES (?, ?, ?)', (tid, 'exec', notice))
        conversation(self.root / 'conversations/mine.db', notice)
        for run_id in (None, ''):
            with self.subTest(run_id=run_id):
                with mock.patch.object(self.cleanup.subprocess, 'run', return_value=mock.Mock(returncode=0)) as delete:
                    self.cleanup.codex_cleanup(self.root, events, run_id)
                delete.assert_not_called()
                self.cleanup.agy_cleanup(self.root, {}, run_id)
                self.assertTrue((self.root / 'conversations/mine.db').exists())

    def test_agy_native_user_field_varint_and_lowest_idx(self):
        notice = 'Crossfeed selected model: ' + 'x' * 200 + '\nCrossfeed run mine.\n=== CROSSFEED TASK ==='
        path = self.root / 'conversations/mine.db'
        conversation(path, notice)
        conversation(path, 'owner later')
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute('INSERT INTO steps(idx,step_type,step_payload) VALUES(-1, 1, ?)', (b'not a user payload',))
        self.cleanup.agy_cleanup(self.root, {}, 'mine')
        self.assertFalse(path.exists())

    def test_agy_native_nested_layout_field_19_then_2(self):
        # The native step_type 14 payload starts with
        # scalar/metadata fields and carries the user text at field 19 -> field 2, not at top level.
        from tests.fixtures.session_fake import varint
        notice = b'Crossfeed selected model: fixture\nCrossfeed run mine.\n=== CROSSFEED TASK ===\ntask'
        inner = b'\x12' + varint(len(notice)) + notice
        meta = b'\x0a\x0c\x08\xa5\xcf\x81\xd6\x06\x10\xa8\xb3\x8d\xd6\x03'
        payload = b'\x08\x0e\x20\x03\x2a' + varint(len(meta)) + meta + b'\x9a\x01' + varint(len(inner)) + inner
        path = self.root / 'conversations/mine.db'
        conversation(path, 'placeholder')
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute('DELETE FROM steps')
            db.execute('INSERT INTO steps(idx,step_type,step_payload) VALUES(0, 14, ?)', (payload,))
        self.cleanup.agy_cleanup(self.root, {}, 'mine')
        self.assertFalse(path.exists())
        # The same notice nested under any other field is not the user text: never ownership.
        other = b'\x08\x0e\xa2\x01' + varint(len(inner)) + inner
        path2 = self.root / 'conversations/owner.db'
        conversation(path2, 'placeholder')
        with contextlib.closing(sqlite3.connect(path2)) as db, db:
            db.execute('DELETE FROM steps')
            db.execute('INSERT INTO steps(idx,step_type,step_payload) VALUES(0, 14, ?)', (other,))
        self.cleanup.agy_cleanup(self.root, {}, 'mine')
        self.assertTrue(path2.exists())

    def test_agy_wrong_field_and_malformed_suffix_fail_closed(self):
        from tests.fixtures.session_fake import varint
        notice = b'Crossfeed selected model: fixture\nCrossfeed run mine.\n=== CROSSFEED TASK ==='
        for payload in (b'\x1a' + varint(len(notice)) + notice,
                        b'\x12' + varint(len(notice)) + notice + b'\x12\x80'):
            path = self.root / 'conversations/owner.db'
            conversation(path, 'owner')
            with contextlib.closing(sqlite3.connect(path)) as db, db:
                db.execute('DELETE FROM steps')
                db.execute('INSERT INTO steps(idx,step_type,step_payload) VALUES(0, 14, ?)', (payload,))
            try:
                self.cleanup.agy_cleanup(self.root, {}, 'mine')
            except RuntimeError:
                pass  # malformed payload is a diagnostic failure, never deletion
            self.assertTrue(path.exists())

    def test_no_thread_emitted_never_runs_delete_or_opens_database(self):
        events = self.root / 'events'
        events.write_text('{"type":"turn.failed"}\n')
        with mock.patch.object(self.cleanup.subprocess, 'run') as command:
            self.cleanup.codex_cleanup(self.root, events, 'mine')
        command.assert_not_called()

    def test_delete_failure_attempts_other_threads_and_reports_failure(self):
        events = self.root / 'events'
        events.write_text(json.dumps({'type': 'thread.started', 'thread_id': '00000000-0000-0000-0000-000000000001'}))
        with contextlib.closing(sqlite3.connect(self.root / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads (id TEXT, source TEXT, first_user_message TEXT)')
            db.execute('INSERT INTO threads VALUES (?, ?, ?)', ('00000000-0000-0000-0000-000000000001', '{}',
                       'Crossfeed selected model: fixture\nCrossfeed run mine.\n=== CROSSFEED TASK ==='))
        with mock.patch.object(self.cleanup.subprocess, 'run', return_value=mock.Mock(returncode=17)):
            with self.assertRaisesRegex(RuntimeError, 'delete'):
                self.cleanup.codex_cleanup(self.root, events, 'mine')


if __name__ == '__main__':
    unittest.main()
