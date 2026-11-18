"""No-trace contracts. Every root and subprocess is isolated from the real home."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from tests.fixtures.session_fake import AGY_SCHEMA, conversation

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
MARK = 'Crossfeed selected model: fixture\n=== CROSSFEED TASK ===\nprivate fixture'


def db(path, sql, rows=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        for statement in sql.split(';'):
            if statement.strip():
                conn.execute(statement)
        for query, values in rows:
            conn.executemany(query, values)
        conn.commit()


def varint(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


class NoTrace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.state = self.home / 'state'
        self.now = time.time()
        self.calls = []
        self.ps = ''
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'CODEX_HOME': str(self.home / '.codex'),
                                         'FLEET_STATE_DIR': str(self.state), 'DISCARD': str(self.home / 'discard')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.n = importlib.import_module('notrace')

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == 'rg':
            return subprocess.run(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, self.ps if argv[0] == 'ps' else '', '')

    def census(self, surfaces, **kwargs):
        return self.n.census(home=self.home, state_dir=self.state, now=self.now,
                             runner=self.runner, surfaces=surfaces, **kwargs)

    def age(self, path, old=True):
        stamp = self.now - (7200 if old else 10)
        os.utime(path, (stamp, stamp))
        return path

    def transcript(self, path, rows, old=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return self.age(path, old)

    def codex(self):
        ids = [f'00000000-0000-4000-8000-{i:012d}' for i in range(1, 8)]
        rows = [
            (ids[0], 'exec', '/old', MARK, self.now - 7200),
            (ids[1], 'exec', '/owner', 'owner asks\n' + MARK, self.now - 7200),
            (ids[2], json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': ids[0]}}}), '/child', 'child', self.now - 7200),
            (ids[3], 'exec', '/fresh', MARK, self.now - 10),
            (ids[4], 'exec', '/live dir', MARK, self.now - 7200),
            (ids[5], 'exec', '/owner', 'no marker', self.now - 7200),
            (ids[6], json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': ids[1]}}}), '/owner-child', MARK, self.now - 7200),
        ]
        db(self.home / '.codex/state_5.sqlite',
           'CREATE TABLE threads(id TEXT, source TEXT, cwd TEXT, first_user_message TEXT, updated_at REAL, archived INT DEFAULT 0, rollout_path TEXT)',
           [('INSERT INTO threads(id,source,cwd,first_user_message,updated_at) VALUES(?,?,?,?,?)', rows)])
        self.ps = '99 codex exec -C "/live dir" --json\n100 codex app-server\n'
        return ids

    def test_codex_exact_counts_plan_and_purge(self):
        ids = self.codex()
        result = self.census(['codex'])
        row = result['surfaces']['codex']
        self.assertEqual({k: row[k] for k in ('crossfeed_total', 'leftover', 'live', 'unmarked_hint')},
                         dict(crossfeed_total=4, leftover=2, live=1, unmarked_hint=2))
        self.assertEqual(result['exit_code'], 1)
        self.census(['codex'], purge=True)
        self.assertFalse(any(c[0] == 'codex' for c in self.calls))
        result = self.census(['codex'], purge=True, yes=True)
        self.assertEqual(result['surfaces']['codex']['purged'], 2)
        self.assertEqual(result['exit_code'], 0)
        deletes = [c for c in self.calls if c[0] == 'codex']
        self.assertCountEqual(deletes, [['codex', 'delete', '--force', ids[0]], ['codex', 'delete', '--force', ids[2]]])

    def test_codex_orphan_rollouts_need_exec_proof_and_respect_live_cwd(self):
        owned = []
        for i, (source, cwd, first) in enumerate([('exec', '/old', MARK), ('app', '/owner', MARK),
                                                 ('exec', '/live dir', MARK), ('exec', '/owner', 'owner')], 20):
            identifier = f'00000000-0000-4000-8000-{i:012d}'
            path = self.home / f'.codex/archived_sessions/rollout-2026-{identifier}.jsonl'
            self.transcript(path, [{'type': 'session_meta', 'payload': {'source': source, 'cwd': cwd}},
                                   {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': first}},
                                   {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': MARK}}])
            if source == 'exec' and cwd == '/old':
                owned.append(identifier)
        self.ps = '42 /opt/bin/codex exec -C /live dir --json'
        result = self.census(['codex'], purge=True, yes=True)
        row = result['surfaces']['codex']
        self.assertEqual((row['crossfeed_total'], row['leftover'], row['live'], row['purged']), (2, 1, 1, 1))
        self.assertEqual([c[3] for c in self.calls if c[0] == 'codex'], owned)

    def test_codex_live_root_protects_all_descendants(self):
        ids = self.codex()
        with contextlib.closing(sqlite3.connect(self.home / '.codex/state_5.sqlite')) as conn, conn:
            conn.execute('UPDATE threads SET source=? WHERE id=?',
                         (json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': ids[4]}}}), ids[2]))
            conn.execute('INSERT INTO threads(id,source,cwd,first_user_message,updated_at) VALUES(?,?,?,?,?)',
                         ('00000000-0000-4000-8000-000000000099',
                          json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': ids[2]}}}),
                          '/grandchild', 'child', self.now - 7200))
        row = self.census(['codex'], purge=True, yes=True)['surfaces']['codex']
        self.assertEqual(row['live'], 3)
        self.assertEqual([c[-1] for c in self.calls if c[0] == 'codex'], [ids[0]])

    def test_codex_rollout_skips_injected_user_context(self):
        for i, injected in enumerate(('# AGENTS.md instructions\nprivate rules', '<environment_context>cwd</environment_context>')):
            path = self.home / f'.codex/sessions/rollout-00000000-0000-4000-8000-{i:012d}.jsonl'
            self.transcript(path, [{'type': 'session_meta', 'payload': {'source': 'exec', 'cwd': '/old'}},
                                   {'type': 'response_item', 'payload': {'role': 'user', 'content': injected}},
                                   {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': MARK}}])
        self.assertEqual(self.census(['codex'])['surfaces']['codex']['leftover'], 2)

    def test_copilot_private_homes_count_purge_and_protect_live(self):
        for name, old in (('old', True), ('fresh', False), ('busy', True)):
            path = self.state / f'copilot-homes/run.{name}'
            path.mkdir(parents=True)
            self.age(path, old)
        def busy(argv, **kwargs):
            if argv[0] == 'lsof':
                return subprocess.CompletedProcess(argv, 0, 'n' + str(self.state / 'copilot-homes/run.busy/lock') + '\n', '')
            return self.runner(argv, **kwargs)
        row = self.n.census(home=self.home, state_dir=self.state, now=self.now, runner=busy,
                            surfaces=['copilot'], purge=True, yes=True)['surfaces']['copilot']
        self.assertEqual((row['crossfeed_total'], row['leftover'], row['live'], row['purged']), (3, 1, 1, 1))

    def test_agy_group_age_includes_all_artifacts(self):
        root = self.home / '.gemini/antigravity-cli'
        for name in ('brain', 'annotations', 'implicit', 'shm'):
            path = root / f'conversations/{name}.db'
            conversation(path, MARK)
            self.age(path)
            extra = {'brain': root / 'brain/brain/subdir/answer',
                     'annotations': root / 'annotations/annotations.pbtxt',
                     'implicit': root / 'implicit/implicit.pb',
                     'shm': Path(str(path) + '-shm')}[name]
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.touch()
            if name == 'brain':
                self.age(extra.parent)
                self.age(extra.parent.parent)
        row = self.census(['agy'], purge=False)['surfaces']['agy']
        self.assertEqual(row['crossfeed_total'], 4)
        # A read-only SQLite open can touch -shm;
        # so a fresh -shm is no activity evidence. The three real artifacts keep their groups.
        self.assertEqual(row['leftover'], 1)

    def test_agy_summary_prefix_alone_is_not_proof(self):
        root = self.home / '.gemini/antigravity-cli'
        db(root / 'conversation_summaries.db', 'CREATE TABLE conversation_summaries(conversation_id TEXT, preview TEXT)',
           [('INSERT INTO conversation_summaries VALUES(?,?)', [('owner', 'Crossfeed selected model: discussed'), ('worker', MARK)])])
        self.assertEqual(self.census(['agy'])['surfaces']['agy']['needs_owner_decision'], 1)

    def test_transcripts_first_user_only_and_owner_stores(self):
        owned = []
        for surface, base in [('claude', '.claude/projects/p'), ('pi', '.pi/agent/sessions/p'),
                              ('copilot', '.copilot/session-state')]:
            def row(text):
                if surface == 'claude':
                    return {'type': 'user', 'message': {'role': 'user', 'content': text}}
                if surface == 'pi':
                    return {'type': 'message', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}}
                return {'type': 'user.message', 'data': {'content': text}}
            prefix = self.home / base
            for name, messages, old in [('marked', [MARK], True), ('fresh', [MARK], False),
                                         ('owner', ['owner', MARK], True), ('plain', ['none'], True)]:
                path = prefix / (name + '.jsonl' if surface != 'copilot' else name + '/events.jsonl')
                self.transcript(path, [row(m) for m in messages], old)
                if surface == 'copilot':
                    self.age(path.parent, old)
                if name == 'marked':
                    owned.append(path.parent if surface == 'copilot' else path)
        db(self.home / '.copilot/session-store.db',
           'CREATE TABLE sessions(id TEXT); CREATE TABLE turns(session_id TEXT, user_message TEXT, turn_index INT)',
           [('INSERT INTO turns VALUES(?,?,?)', [('marked', MARK, 0), ('owner', 'owner', 0), ('owner', MARK, 1)])])
        result = self.census(['claude', 'pi', 'copilot'], purge=True, yes=True)
        for surface in ['claude', 'pi', 'copilot']:
            row = result['surfaces'][surface]
            self.assertEqual((row['crossfeed_total'], row['leftover'], row['purged']), (2, 1, 1))
        self.assertEqual(result['surfaces']['copilot']['needs_owner_decision'], 1)
        self.assertEqual(result['exit_code'], 1)
        deleted = [str(p) for c in self.calls if c[0] == str(self.home / 'discard') for p in c[1:] if str(p).startswith(str(self.home))]
        self.assertCountEqual(deleted, list(map(str, owned)))
        for call in self.calls:
            if call[0] == str(self.home / 'discard'):
                self.assertIn('--certain', call)
        self.assertTrue((self.home / '.copilot/session-store.db').exists())

    def test_agy_protobuf_and_summary_never_deleted(self):
        root = self.home / '.gemini/antigravity-cli'
        for name, text in [('marked', MARK), ('owner', 'owner'), ('fresh', MARK)]:
            payload = text.encode()
            db(root / f'conversations/{name}.db', AGY_SCHEMA,
               [('INSERT INTO steps(idx,step_type,step_payload) VALUES(?,?,?)', [(0, 14, b'\x08\x0e\x12' + varint(len(payload)) + payload),
                                                    (1, 14, b'\x12' + varint(len(MARK)) + MARK.encode())])])
            self.age(root / f'conversations/{name}.db', name != 'fresh')
        for relative in ['brain/marked', 'annotations', 'implicit']:
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / 'annotations/marked.pbtxt').touch()
        (root / 'implicit/marked.pb').touch()
        for path in [root / 'brain/marked', root / 'annotations/marked.pbtxt', root / 'implicit/marked.pb']:
            self.age(path)
        db(root / 'conversation_summaries.db', 'CREATE TABLE conversation_summaries(conversation_id TEXT, preview TEXT)',
           [('INSERT INTO conversation_summaries VALUES(?,?)', [('marked', 'summary'), ('owner', 'owner')])])
        result = self.census(['agy'], purge=True, yes=True)
        row = result['surfaces']['agy']
        self.assertEqual((row['crossfeed_total'], row['leftover'], row['purged'], row['needs_owner_decision']), (2, 1, 1, 1))
        self.assertFalse(any('conversation_summaries' in str(c) or 'owner.db' in str(c) for c in self.calls))
        self.ps = '8 agy --prompt work'
        self.assertEqual(self.census(['agy'])['surfaces']['agy']['live'], 2)

    def test_wrapper_owned_paths_and_open_files(self):
        old = self.state / 'oc-homes/run.old'
        old.mkdir(parents=True)
        fresh = self.state / 'oc-homes/run.fresh'
        fresh.mkdir()
        self.age(old)
        self.age(fresh, False)
        active_child = self.state / 'oc-homes/run.active-child'
        active_child.mkdir()
        (active_child / 'new.db').touch()
        self.age(active_child)  # Parent age alone must not discard recent child data.
        for filename in ['answer.txt', 'stdout.log', 'stderr.log', 'dispatch.json', 'receipt.crossfeed.json']:
            path = self.state / 'dispatches/d' / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            self.age(path)
        for filename in ['one.events.jsonl', 'manifest.jsonl', 'one.out']:
            path = self.state / 'runs/r' / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            self.age(path)
        result = self.census(['opencode', 'transcripts'], purge=True, yes=True)
        self.assertEqual(result['surfaces']['opencode']['leftover'], 1)
        self.assertEqual(result['surfaces']['opencode']['crossfeed_total'], 3)
        self.assertEqual(result['surfaces']['transcripts']['leftover'], 3)
        self.assertFalse(any('stderr.log' in str(c) or 'manifest.jsonl' in str(c) or 'dispatch.json' in str(c) for c in self.calls))
        def busy(argv, **kwargs):
            if argv[0] == 'lsof':
                return subprocess.CompletedProcess(argv, 0, 'n' + str(old / 'lock') + '\n', '')
            return self.runner(argv, **kwargs)
        result = self.n.census(home=self.home, state_dir=self.state, surfaces=['opencode'], now=self.now, runner=busy)
        self.assertEqual(result['surfaces']['opencode']['live'], 1)
        self.assertEqual(result['exit_code'], 0)

    def test_open_file_inspection_is_bounded_across_stale_artifacts(self):
        for i in range(4):
            path = self.state / f'dispatches/d{i}/answer.txt'
            path.parent.mkdir(parents=True)
            path.touch()
            self.age(path)
        result = self.census(['transcripts'])
        self.assertEqual(result['surfaces']['transcripts']['leftover'], 4)
        self.assertLessEqual(len([c for c in self.calls if c[0] == 'lsof']), 1)
        def incomplete(argv, **kwargs):
            if argv[0] == 'lsof':
                return subprocess.CompletedProcess(argv, 1, '', 'inspection incomplete')
            return self.runner(argv, **kwargs)
        result = self.n.census(home=self.home, state_dir=self.state, surfaces=['transcripts'],
                              now=self.now, runner=incomplete, purge=True, yes=True)
        self.assertEqual(result['exit_code'], 2)
        self.assertEqual(result['surfaces']['transcripts']['purged'], 0)
        self.assertFalse(any(c[0] == str(self.home / 'discard') for c in self.calls))

    def test_cannot_check_and_failed_purge(self):
        path = self.home / '.codex/state_5.sqlite'
        path.parent.mkdir()
        path.write_text('broken')
        self.assertEqual(self.census(['codex'])['exit_code'], 2)
        path.unlink()
        self.codex()
        def fail(argv, **kwargs):
            if argv[0] == 'codex':
                return subprocess.CompletedProcess(argv, 1, '', 'secret should never escape')
            return self.runner(argv, **kwargs)
        result = self.n.census(home=self.home, state_dir=self.state, surfaces=['codex'], now=self.now,
                              runner=fail, purge=True, yes=True)
        self.assertEqual(result['exit_code'], 2)
        self.assertEqual(result['surfaces']['codex']['failed'], 2)
        self.assertNotIn('secret', json.dumps(result))

    def test_census_can_be_loaded_by_file_without_script_path(self):
        script = ("import importlib.util; "
                  f"spec=importlib.util.spec_from_file_location('isolated_notrace', {str(SCRIPTS / 'notrace.py')!r}); "
                  "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
                  "assert module.worker_notice('Crossfeed selected model: fixture\\n=== CROSSFEED TASK ===')")
        result = subprocess.run([sys.executable, '-I', '-c', script], cwd=self.home,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_boundary_and_empty_exit_zero(self):
        fleet = SCRIPTS / 'fleetctl.py'
        args = [sys.executable, str(fleet), 'traces', '--surface', 'codex', '--json']
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['surfaces']['codex']['leftover'], 0)
        self.codex()
        # Fake ps and codex/discard executables: even a broken CLI cannot touch real state.
        bindir = self.home / 'bin'
        bindir.mkdir()
        log = self.home / 'calls'
        for executable in ['ps', 'codex', 'discard']:
            path = bindir / executable
            path.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/calls"\n')
            path.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'], DISCARD=str(bindir / 'discard'))
        result = subprocess.run(args, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1, result.stderr)
        aged = subprocess.run(args + ['--older-than', '10000'], capture_output=True, text=True, env=env)
        self.assertEqual(aged.returncode, 0, aged.stderr)
        invalid = subprocess.run(args + ['--older-than', 'nan'], capture_output=True, text=True, env=env)
        self.assertEqual(invalid.returncode, 2)
        multiple = subprocess.run(args + ['--surface', 'pi'], capture_output=True, text=True, env=env)
        self.assertEqual(set(json.loads(multiple.stdout)['surfaces']), {'codex', 'pi'})
        result = subprocess.run(args + ['--purge'], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('delete --force', log.read_text())
        result = subprocess.run(args + ['--purge', '--yes'], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().count('delete --force'), 3)

    def test_doctor_fails_for_leftovers_and_incomplete_census(self):
        fleet = importlib.import_module('fleetctl')
        overlay = self.home / 'overlay.json'
        overlay.write_text('{}')
        for code in (0, 1, 2):
            result = self.census(['codex'])
            result['exit_code'] = code
            row = result['surfaces']['codex']
            if code == 1:
                row['leftover'] = 1
            elif code == 2:
                row.update(status='cannot_check', error='fixture unavailable')
            output = io.StringIO()
            with patch.object(fleet, 'read_overlay', return_value={'lanes': [], 'quota_pools': {}}), \
                 patch.object(fleet, 'effort_problems', return_value=[]), \
                 patch.object(fleet, 'cli_version_problems', return_value=[]), \
                 patch.object(fleet, 'opencode_whitelist_problems', return_value=[]), \
                 patch.object(fleet, 'identity_drift_problems', return_value=[]), \
                 patch.object(fleet, 'refresh_stale_pools'), \
                 patch.object(fleet, 'traces_module', return_value=self.n), \
                 patch.object(self.n, 'census', return_value=result), contextlib.redirect_stdout(output):
                self.assertEqual(fleet.doctor_command(overlay, self.state), int(code != 0))
            self.assertIn('no trace:', output.getvalue())
            self.assertIn('codex:', output.getvalue())

    def test_marker_boundaries_meta_and_wrong_protobuf_field(self):
        self.assertTrue(self.n.marked(MARK))
        self.assertFalse(self.n.marked('owner quoted ' + MARK))
        self.assertFalse(self.n.marked('Crossfeed selected model:' + 'x' * 2000 + '\n=== CROSSFEED TASK ==='))
        path = self.home / '.claude/projects/p/meta.jsonl'
        self.transcript(path, [{'type': 'user', 'isMeta': True, 'message': {'content': 'meta'}},
                               {'type': 'user', 'message': {'content': MARK}}])
        self.assertEqual(self.census(['claude'])['surfaces']['claude']['leftover'], 1)
        # A marker in another protobuf string field does not identify a chat.
        text = MARK.encode()
        root = self.home / '.gemini/antigravity-cli/conversations'
        db(root / 'owner.db', AGY_SCHEMA,
           [('INSERT INTO steps(idx,step_type,step_payload) VALUES(?,?,?)', [(0, 14, b'\x1a' + varint(len(text)) + text)])])
        self.age(root / 'owner.db')
        self.assertEqual(self.census(['agy'])['surfaces']['agy']['crossfeed_total'], 0)

    def test_opencode_main_db_is_counted_but_never_mutated(self):
        path = self.home / '.local/share/opencode/opencode.db'
        db(path, 'CREATE TABLE session(id TEXT); CREATE TABLE message(id TEXT,session_id TEXT,time_created INT,data TEXT); CREATE TABLE part(id TEXT,message_id TEXT,time_created INT,data TEXT)', [
            ('INSERT INTO session VALUES(?)', [('marked',), ('owner',)]),
            ('INSERT INTO message VALUES(?,?,?,?)', [('m', 'marked', 1, '{"role":"user"}'),
                                                   ('o', 'owner', 1, '{"role":"user"}'),
                                                   ('o2', 'owner', 2, '{"role":"user"}')]),
            ('INSERT INTO part VALUES(?,?,?,?)', [('p', 'm', 1, json.dumps({'type': 'text', 'text': MARK})),
                                                 ('p2', 'o', 1, '{"type":"text","text":"owner"}'),
                                                 ('p3', 'o2', 2, json.dumps({'type': 'text', 'text': MARK}))])])
        before = path.read_bytes()
        result = self.census(['opencode'], purge=True, yes=True)
        row = result['surfaces']['opencode']
        self.assertEqual((row['crossfeed_total'], row['needs_owner_decision'], row['purged']), (1, 1, 0))
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(any(c[0] in ('codex', str(self.home / 'discard')) for c in self.calls))

    def test_purge_rechecks_activity_and_ignores_symlink(self):
        directory = self.state / 'oc-homes/run.owned'
        directory.mkdir(parents=True)
        self.age(directory)
        owner = self.home / 'owner'
        owner.mkdir()
        self.age(owner)
        (directory.parent / 'run.link').symlink_to(owner, target_is_directory=True)
        calls = 0
        def race(argv, **kwargs):
            nonlocal calls
            if argv[0] == 'lsof':
                calls += 1
                if calls == 1:
                    os.utime(directory, (self.now, self.now))
            return self.runner(argv, **kwargs)
        result = self.n.census(home=self.home, state_dir=self.state, surfaces=['opencode'],
                              now=self.now, runner=race, purge=True, yes=True)
        self.assertEqual(result['surfaces']['opencode']['crossfeed_total'], 1)
        self.assertEqual(result['surfaces']['opencode']['purged'], 0)
        self.assertFalse(any(c[0] == str(self.home / 'discard') for c in self.calls))

    def test_chat_health_matrix_without_sockets(self):
        clean = {'mode': 'delete', 'queued': 0, 'oldest_queued_age_ms': 0,
                 'tracked': 0, 'oldest_tracked_age_ms': 0, 'unproven_dropped': 0,
                 'last_sweep': {'at': self.now, 'pass_complete': True, 'backlog': 0,
                                'errors': 0, 'pass_errors': 0, 'rate_limited_until': 0,
                                'pass_started_at': self.now, 'last_pass_completed_at': self.now, 'last_pass_errors': 0,
                                'last_pass_wake_matches_visible': 0, 'last_pass_wake_matches_archived': 0,
                                'last_pass_deleted_visible': 0, 'last_pass_deleted_archived': 0}}
        lane = {'harness': 'chatgpt-chat', 'effort': {'shape': 'none'},
                'transport': {'api_base': 'http://127.0.0.1:4319/v1'},
                'auth': {'key_file': str(self.home / 'key'), 'kind': 'subscription-relay',
                         'terms_class': 'owner-accepted',
                         'owner_acceptance': {'quote': 'fixture', 'date': '2026-11-04'}}}
        (self.home / 'key').write_text('fixture-key')
        import chatgpt_transport
        def check(disposal, expected):
            with patch.object(chatgpt_transport, 'request', return_value={'chat_disposal': disposal}) as request:
                result = self.census(['chatgpt'], roster={'lanes': [lane]})
            self.assertEqual(result['exit_code'], expected, result)
            self.assertEqual(request.call_args.args[2], '/gateway/status')
        import copy
        check(clean, 0)
        for changes in [dict(mode='archive'), dict(queued=1, oldest_queued_age_ms=901000),
                        dict(unproven_dropped=1)]:
            changed = copy.deepcopy(clean)
            changed.update(changes)
            check(changed, 1)
        for sweep_changes in [dict(at=self.now - 3601), dict(last_pass_errors=1),
                              dict(last_pass_wake_matches_visible=1),
                              dict(last_pass_wake_matches_archived=1)]:
            changed = copy.deepcopy(clean)
            changed['last_sweep'].update(sweep_changes)
            check(changed, 1)
        changed = copy.deepcopy(clean)
        changed['last_sweep']['last_pass_completed_at'] = None
        check(changed, 2)
        changed['last_sweep']['last_pass_completed_at'] = self.now - 86401
        check(changed, 1)
        changed = copy.deepcopy(clean)
        changed['last_sweep'].update(at=self.now - 3601, rate_limited_until=(self.now + 60) * 1000)
        check(changed, 0)
        changed['last_sweep']['at'] = self.now - 21601
        check(changed, 1)
        for key in clean['last_sweep']:
            changed = copy.deepcopy(clean)
            del changed['last_sweep'][key]
            check(changed, 2)
        for key in clean:
            changed = copy.deepcopy(clean)
            del changed[key]
            check(changed, 2)
        with patch.object(chatgpt_transport, 'request', side_effect=chatgpt_transport.Rejected(6, 'fixture unavailable')):
            self.assertEqual(self.census(['chatgpt'], roster={'lanes': [lane]})['exit_code'], 2)
        # The status check compares service timestamps to its own clock, not the purge start.
        fresh = copy.deepcopy(clean)
        fresh['last_sweep'].update(at=self.now + 1800, pass_started_at=self.now + 1800,
                                   last_pass_completed_at=self.now + 1800)
        clock = iter([self.now] + [self.now + 1800] * 50)
        with patch.object(chatgpt_transport, 'request', return_value={'chat_disposal': fresh}), \
                patch.object(self.n.time, 'time', side_effect=lambda: next(clock)):
            result = self.n.census(home=self.home, state_dir=self.state, runner=self.runner,
                                   surfaces=['chatgpt'], roster={'lanes': [lane]})
        self.assertEqual(result['exit_code'], 0, result)

    def test_unconfigured_gateway_is_not_applicable_but_broken_settings_fail(self):
        row = self.census(['chatgpt'], roster={'lanes': []})
        self.assertEqual(row['exit_code'], 0, row)
        self.assertEqual(row['surfaces']['chatgpt']['status'], 'n/a (gateway not configured)')
        for roster in (None, {'chatgpt_gateway': {}}, {'lanes': [{'harness': 'chatgpt-chat'}]}):
            with self.subTest(roster=roster):
                self.assertEqual(self.census(['chatgpt'], roster=roster)['exit_code'], 2)

    def test_chat_status_contract(self):
        status = {'chat_disposal': {'mode': 'delete', 'queued': 0, 'oldest_queued_age_ms': 0,
                  'tracked': 0, 'oldest_tracked_age_ms': 0, 'unproven_dropped': 0,
                  'last_sweep': {'at': self.now, 'pass_complete': True, 'backlog': 0,
                                 'errors': 0, 'pass_errors': 0, 'rate_limited_until': 0,
                                 'pass_started_at': self.now, 'last_pass_completed_at': self.now, 'last_pass_errors': 0,
                                 'last_pass_wake_matches_visible': 0, 'last_pass_wake_matches_archived': 0,
                                 'last_pass_deleted_visible': 0, 'last_pass_deleted_archived': 0}}}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner):
                if inner.path not in ('/status', '/v1/gateway/status'):
                    inner.send_error(404)
                    return
                inner.send_response(200)
                inner.end_headers()
                inner.wfile.write(json.dumps(status).encode())
            def log_message(*args):
                pass
        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except PermissionError as exc:
            self.skipTest(f'loopback socket binding denied: {exc}')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        key = self.home / 'key'
        key.write_text('fixture-key')
        lane = {'harness': 'chatgpt-chat', 'effort': {'shape': 'none'},
                'transport': {'api_base': f'http://127.0.0.1:{server.server_port}/v1'},
                'auth': {'key_file': str(key), 'kind': 'subscription-relay', 'terms_class': 'owner-accepted',
                         'owner_acceptance': {'quote': 'fixture', 'date': '2026-11-04'}}}
        roster = {'lanes': [lane]}
        def code():
            return self.census(['chatgpt'], roster=roster)['exit_code']
        self.assertEqual(code(), 0)
        disposal = status['chat_disposal']
        disposal.update(queued=1, oldest_queued_age_ms=901000)
        self.assertEqual(code(), 1)
        disposal.update(queued=0, oldest_queued_age_ms=0)
        disposal['last_sweep']['last_pass_wake_matches_archived'] = 1
        self.assertEqual(code(), 1)
        disposal['last_sweep']['last_pass_wake_matches_archived'] = 0
        disposal['last_sweep']['at'] = self.now - 3601
        self.assertEqual(code(), 1)
        disposal['last_sweep']['at'] = self.now
        disposal['last_sweep']['last_pass_completed_at'] = None
        self.assertEqual(code(), 2)  # no evidence of a completed pass
        disposal['last_sweep']['last_pass_completed_at'] = self.now - 86401
        self.assertEqual(code(), 1)
        del disposal['oldest_tracked_age_ms']
        self.assertEqual(code(), 2)
        status.clear()
        self.assertEqual(code(), 2)
        lane['transport']['api_base'] = 'http://127.0.0.1:1/v1'
        self.assertEqual(code(), 2)


if __name__ == '__main__':
    unittest.main()
