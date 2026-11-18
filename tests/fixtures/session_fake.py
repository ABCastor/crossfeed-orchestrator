"""Persist synthetic CLI sessions; never used outside isolated test HOME/state."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid


def varint(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


AGY_SCHEMA = "CREATE TABLE IF NOT EXISTS steps (idx integer, step_type integer NOT NULL DEFAULT 0, status integer NOT NULL DEFAULT 0, has_subtrajectory numeric NOT NULL DEFAULT false, metadata blob, error_details blob, permissions blob, task_details blob, render_info blob, step_payload blob, step_format integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))"


def conversation(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute(AGY_SCHEMA)
        raw = text.encode()
        db.execute('INSERT INTO steps (idx,step_type,step_payload) VALUES ((SELECT COALESCE(MAX(idx), -1) + 1 FROM steps), 14, ?)', (b'\x08\x0e\x12' + varint(len(raw)) + raw,))


def main():
    lane, *args = sys.argv[1:]
    home = Path.home()
    if lane == 'codex-delete':
        tid = args[0]
        if os.environ.get('FAKE_DELETE_FAIL'):
            return 33
        root = Path(os.environ['CODEX_HOME'])
        # Prove child-first ordering and receipt-before-deletion.
        with contextlib.closing(sqlite3.connect(root / 'state_5.sqlite')) as db, db:
            children = [r for r in db.execute('SELECT id, source FROM threads')
                        if json.loads(r[1]).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id') == tid]
            assert not children, 'parent deleted before its children'
            db.execute('DELETE FROM threads WHERE id=?', (tid,))
        for path in (root / 'sessions').glob(f'*{tid}.jsonl'):
            path.unlink()
        with (root / 'deletes').open('a') as f:
            f.write(tid + '\n')
        return 0
    if lane == 'codex':
        root = Path(os.environ['CODEX_HOME'])
        (root / 'sessions').mkdir(parents=True, exist_ok=True)
        tid, child, grandchild = [str(uuid.uuid4()) for _ in range(3)]
        with contextlib.closing(sqlite3.connect(root / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS threads (id TEXT, source TEXT, first_user_message TEXT)')
            for id_, parent in ((tid, None), (child, tid), (grandchild, child)):
                source = {'subagent': {'thread_spawn': {'parent_thread_id': parent}}} if parent else {}
                db.execute('INSERT INTO threads VALUES (?, ?, ?)', (id_, json.dumps(source), args[0] if args else ""))
                (root / 'sessions' / f'rollout-{id_}.jsonl').write_text(json.dumps(
                    {'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol'}}) + '\n')
        print(json.dumps({'type': 'thread.started', 'thread_id': tid}), flush=True)
    elif lane == 'agy':
        root = home / '.gemini/antigravity-cli'
        tid = str(uuid.uuid4())
        conversation(root / 'conversations' / (tid + '.db'), args[0])
        (root / 'brain' / tid).mkdir(parents=True)
        (root / 'brain' / tid / 'answer.md').write_text('fake answer')
        for folder, suffix in (('annotations', '.pbtxt'), ('implicit', '.pb')):
            (root / folder).mkdir(exist_ok=True)
            (root / folder / (tid + suffix)).write_text('fake')
    elif lane == 'copilot':
        root = Path(os.environ.get('COPILOT_HOME', str(home / '.copilot')))
        (root / 'session-state' / str(uuid.uuid4())).mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(root / 'session-store.db')) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (transcript TEXT)')
            db.execute('INSERT INTO sessions VALUES (?)', ('fake answer',))
        if os.environ.get('FAKE_COPILOT_HOME_FILE'):
            Path(os.environ['FAKE_COPILOT_HOME_FILE']).write_text(str(root))
    elif lane == 'claude':
        if '--no-session-persistence' not in args:
            (home / 'claude-persisted-session').write_text('fake session')
    elif lane == 'opencode':
        root = Path(os.environ['XDG_DATA_HOME'])
        (root / 'fake-session').write_text('fake transcript')
        if os.environ.get('SESSION_FAKE_LOG'):
            with Path(os.environ['SESSION_FAKE_LOG']).open('a') as f:
                f.write(str(root) + '\n')
                if 'opencode-fleet-public-search' in str(Path.cwd()):
                    f.write(str(Path.cwd()) + '\n')
    elif lane == 'assert-clean':
        root = Path(args[0])
        patterns = ['**/rollout-*.jsonl', '**/conversations/*.db', '**/brain/*/answer.md',
                    '**/annotations/*.pbtxt', '**/implicit/*.pb', '**/session-store.db',
                    '**/claude-persisted-session', '**/oc-homes/run.*', '**/copilot-homes/run.*']
        leftovers = [str(p.relative_to(root)) for pattern in patterns for p in root.glob(pattern)]
        registry = root / 'session-paths'
        if registry.exists():
            leftovers += [p for p in registry.read_text().splitlines() if Path(p).exists()]
        for database in root.glob('**/state_5.sqlite'):
            with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                count = db.execute('SELECT COUNT(*) FROM threads').fetchone()[0]
            if count:
                leftovers.append(f'{database.relative_to(root)}: {count} thread rows')
        assert not leftovers, 'persisted sessions: ' + ', '.join(leftovers)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
