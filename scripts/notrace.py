#!/usr/bin/env python3
"""Read-only Crossfeed leftover census; explicit --purge --yes enables disposal.

Only the FIRST user message proves ownership. Never display prompt text or
subprocess stderr: both can contain private text or credentials. Filesystem and
process inputs are injectable so disposal tests never need the operator's home.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import datetime as dt
import fnmatch
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

# fleetctl and hooks can load the census by file without adding scripts to sys.path.
_ownership_spec = importlib.util.spec_from_file_location('crossfeed_session_cleanup', Path(__file__).with_name('session_cleanup.py'))
_ownership = importlib.util.module_from_spec(_ownership_spec)
_ownership_spec.loader.exec_module(_ownership)
agy_first_user = _ownership.agy_first_user
worker_notice = _ownership.worker_notice

MARKER = 'Crossfeed selected model:'
TASK = '=== CROSSFEED TASK ==='
REASON = 'Stale Crossfeed session with verified ownership'
SURFACES = ('codex', 'agy', 'copilot', 'opencode', 'claude', 'pi', 'transcripts',
            'chatgpt', 'gemini-media', 'openrouter')


class CannotCheck(Exception):
    """Only fixed, non-sensitive diagnostic messages belong in this exception."""


def marked(text):
    return isinstance(text, str) and text.startswith(MARKER) and TASK in text[:2000]


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(part.get('text', '') for part in value
                       if isinstance(part, dict) and part.get('type') in ('text', 'input_text'))
    return ''


@contextmanager
def database(path):
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def present(path):
    # Path.exists() also hides permission errors in recent Python versions.
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False


def glob_checked(root, pattern):
    """Path.glob suppresses unreadable-directory errors; a census must not."""
    parts = pattern.split('/')
    def descend(directory, index):
        try:
            entries = list(directory.iterdir())
        except FileNotFoundError:
            return
        for path in entries:
            if path.is_symlink():
                continue
            part = parts[index]
            if part == '**':
                if path.is_dir():
                    if index + 1 < len(parts) and fnmatch.fnmatchcase(path.name, parts[index + 1]):
                        yield path
                    yield from descend(path, index)
                elif index + 1 < len(parts) and fnmatch.fnmatchcase(path.name, parts[index + 1]):
                    yield path
            elif fnmatch.fnmatchcase(path.name, part):
                if index == len(parts) - 1:
                    yield path
                elif path.is_dir():
                    yield from descend(path, index + 1)
    yield from descend(Path(root), 0)


def injected_codex(text):
    return text.startswith(('# AGENTS.md instructions', '<environment_context>'))


def first_user(path, surface):
    with path.open() as stream:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            message = event.get('message', {})
            if surface == 'claude' and event.get('type') == 'user' and not event.get('isMeta'):
                return text_content(message.get('content', '') if isinstance(message, dict) else message)
            if surface == 'pi' and isinstance(message, dict) and message.get('role') == 'user':
                return text_content(message.get('content'))
            if surface == 'copilot' and event.get('type') == 'user.message':
                return text_content(event.get('data', {}).get('content'))
            if surface == 'codex':
                payload = event.get('payload', {})
                if event.get('type') == 'event_msg' and payload.get('type') == 'user_message':
                    text = text_content(payload.get('message'))
                    if not injected_codex(text):
                        return text
                if event.get('type') == 'response_item' and payload.get('role') == 'user':
                    text = text_content(payload.get('content'))
                    if not injected_codex(text):
                        return text
    return ''


def rollout_metadata(path):
    with path.open() as stream:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get('type') == 'session_meta':
                return event.get('payload', {})
            if event.get('type') in ('response_item', 'event_msg'):
                break
    return {}



def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise CannotCheck('missing or invalid disposal counters')
    return value


def timestamp(value):
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            raise CannotCheck('invalid disposal sweep timestamp') from None
    value = number(value)
    return value / 1000 if value > 100_000_000_000 else value


def chat_health(status, now, older_than):
    """Return counts and violations. Old/partial service schemas never pass."""
    disposal = status.get('chat_disposal')
    if not isinstance(disposal, dict):
        raise CannotCheck('service did not report chat_disposal')
    for key in ('queued', 'oldest_queued_age_ms', 'tracked', 'oldest_tracked_age_ms', 'unproven_dropped'):
        number(disposal.get(key))
    if not isinstance(disposal.get('mode'), str):
        raise CannotCheck('missing disposal mode')
    sweep = disposal.get('last_sweep')
    if not isinstance(sweep, dict):
        raise CannotCheck('missing disposal sweep evidence')
    at = timestamp(sweep.get('at'))
    if type(sweep.get('pass_complete')) is not bool:
        raise CannotCheck('missing disposal pass completion evidence')
    for key in ('backlog', 'errors', 'pass_errors', 'last_pass_errors'):
        number(sweep.get(key))
    limited_until = number(sweep.get('rate_limited_until')) / 1000
    timestamp(sweep.get('pass_started_at'))
    completed_at = timestamp(sweep.get('last_pass_completed_at'))
    matches = {kind: number(sweep.get(f'last_pass_wake_matches_{kind}')) for kind in ('visible', 'archived')}
    deleted = {kind: number(sweep.get(f'last_pass_deleted_{kind}')) for kind in ('visible', 'archived')}
    violations = []
    if disposal['mode'] != 'delete':
        violations.append('disposal mode is not delete')
    if disposal['queued'] and disposal['oldest_queued_age_ms'] > older_than * 1000:
        violations.append('queued chats older than threshold')
    if disposal['unproven_dropped']:
        violations.append('chats dropped without proven deletion')
    if any(matches[k] > deleted[k] for k in matches):
        violations.append('completed sweep left wake chats undeleted')
    if sweep['last_pass_errors']:
        violations.append('completed disposal pass had errors')
    if now - at > 3600 and (limited_until <= now or now - at > 21600):
        violations.append('disposal sweep older than one hour' if limited_until <= now else 'disposal rate-limited for over six hours')
    if not completed_at or now - completed_at > 86400:
        violations.append('no completed full disposal pass within 24 hours')
    if at > now + 60 or completed_at > now + 60:
        raise CannotCheck('disposal timestamps are in the future')
    return max(disposal['queued'], disposal['tracked']), violations



class Census:
    def __init__(self, home, state_dir, codex_home, opencode_db, now, older_than, runner, roster):
        self.home, self.state = Path(home).expanduser(), Path(state_dir).expanduser()
        self.codex_home, self.opencode_db = Path(codex_home).expanduser(), Path(opencode_db).expanduser()
        self.now, self.threshold, self.runner, self.roster = now, older_than, runner, roster
        self._ps = None
        self._open_names = None

    def command(self, argv, timeout=30):
        try:
            return self.runner(list(map(str, argv)), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            raise CannotCheck('process inspection or disposal command could not run') from None

    def processes(self):
        if self._ps is None:
            result = self.command(['ps', '-axo', 'pid,command'])
            if result.returncode:
                raise CannotCheck('process list unavailable')
            self._ps = result.stdout
        return self._ps

    def live_codex_cwds(self, known_cwds=()):
        cwds = set()
        for line in self.processes().splitlines():
            try:
                words = shlex.split(line)
            except ValueError:
                # Truncated/unparseable codex commands cannot establish safety.
                if re.search(r'\bcodex\b.*\bexec\b', line):
                    raise CannotCheck('could not parse a live codex exec command')
                continue
            if len(words) < 3 or Path(words[1]).name != 'codex' or 'exec' not in words[2:]:
                continue
            for cwd in known_cwds:
                # ps displays argv with spaces and may omit shell quoting. Match
                # complete known cwd strings before tokenising the -C option.
                for spelling in (cwd, shlex.quote(cwd), '"' + cwd + '"'):
                    if re.search(r'(?:^|\s)(?:-C|--cd)(?:\s+|=)' + re.escape(spelling) + r'(?:\s|$)', line):
                        cwds.add(str(Path(cwd).resolve()))
            for i, word in enumerate(words[2:], 2):
                if word in ('-C', '--cd') and i + 1 < len(words):
                    cwds.add(str(Path(words[i + 1]).resolve()))
                elif word.startswith('--cd='):
                    cwds.add(str(Path(word[5:]).resolve()))
        return cwds

    def agy_alive(self):
        for line in self.processes().splitlines():
            words = line.split()
            if len(words) > 1 and Path(words[1]).name == 'agy':
                return True
        return False

    def in_use(self, path):
        # One read-only process/open-file snapshot per census, taken lazily for
        # stale candidates. Global lsof avoids traversing thousands of run homes
        # with +D or paying a process-table scan per transcript. It includes cwd
        # and child processes regardless of their executable, without reading envs.
        # Purge invalidates this snapshot before reclassification below.
        if self._open_names is None:
            result = self.command(['lsof', '-nP', '-Fpn0'])
            if result.returncode not in (0, 1) or result.stderr.strip():
                raise CannotCheck('open-file inspection unavailable or incomplete')
            self._open_names = set()
            for record in result.stdout.split('\0'):
                record = record.lstrip('\n')
                if record.startswith('n/'):
                    self._open_names.add(record[1:])
        # Resolve system aliases (e.g. /var -> /private/var on macOS), while
        # caller-side symlink checks reject links inside wrapper-owned roots.
        names = {str(path), str(path.resolve())}
        return any(opened == name or opened.startswith(name + os.sep)
                   for opened in self._open_names for name in names)

    def stale(self, stamp):
        return self.now - float(stamp) > self.threshold

    def safe_path(self, path):
        # A wrapper-owned name does not authorise following a symlink into operator data.
        roots = (self.home, self.state, self.codex_home, self.opencode_db.parent)
        boundaries = [root for root in roots if path.is_relative_to(root)]
        if not boundaries:
            return False
        root = max(boundaries, key=lambda item: len(item.parts))
        cursor = path
        while True:
            if cursor.is_symlink():
                return False
            if cursor == root:
                return True
            cursor = cursor.parent

    def activity(self, path):
        stamp = path.stat().st_mtime
        if path.is_dir():
            # Stream a large home rather than retaining every descendant Path.
            for child in glob_checked(path, '**/*'):
                if self.safe_path(child):
                    stamp = max(stamp, child.stat().st_mtime)
        return stamp

    def file_item(self, row, targets, paths, stamp=None, live=False):
        paths = [p for p in paths if present(p) and self.safe_path(p)]
        if not paths:
            return
        row['crossfeed_total'] += 1
        stamp = max(self.activity(p) for p in paths) if stamp is None else stamp
        if live:
            row['live'] += 1
        elif self.stale(stamp):
            if any(self.in_use(p) for p in paths):
                row['live'] += 1
            else:
                row['leftover'] += 1
                targets.append(paths)

    def codex(self, row, targets):
        db = self.codex_home / 'state_5.sqlite'
        rows = []
        if present(db):
            with database(db) as conn:
                rows = list(conn.execute('SELECT id,source,cwd,first_user_message,updated_at FROM threads'))
        owned = {r['id'] for r in rows if r['source'] == 'exec' and marked(r['first_user_message'])}
        parents = {}
        for r in rows:
            if r['source'] == 'exec':
                if not marked(r['first_user_message']):
                    row['unmarked_hint'] += 1
            else:
                try:
                    parents[r['id']] = json.loads(r['source']).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
                except (ValueError, AttributeError, TypeError):
                    pass  # Non-exec sources are not themselves ownership proof.
        while True:
            children = {child for child, parent in parents.items() if parent in owned} - owned
            if not children:
                break
            owned.update(children)
        # Exclude every marked thread using a live codex exec's -C directory,
        # regardless of age. This deliberately errs on the side of keeping work.
        row['crossfeed_total'] = len(owned)
        live_cwds = self.live_codex_cwds({r['cwd'] for r in rows if r['id'] in owned}) if owned else set()
        live = {r['id'] for r in rows if r['id'] in owned and str(Path(r['cwd']).resolve()) in live_cwds}
        while True:
            children = {child for child, parent in parents.items() if parent in live} - live
            if not children:
                break
            live.update(children)
        for r in rows:
            if r['id'] not in owned:
                continue
            uuid.UUID(r['id'])  # Never pass unvalidated database values as CLI options.
            if r['id'] in live:
                row['live'] += 1
            elif self.stale(r['updated_at']):
                row['leftover'] += 1
                targets.append(r['id'])
        # Orphan rollouts still carry the same first-user ownership proof. CLI
        # disposal is the sole Codex mutator even when the index row is absent.
        indexed = {r['id'] for r in rows}
        for root in (self.codex_home / 'sessions', self.codex_home / 'archived_sessions'):
            for path in glob_checked(root, '**/rollout-*.jsonl'):
                match = re.search(r'([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\.jsonl$', path.name)
                if not match or match[1] in indexed or not self.safe_path(path):
                    continue
                if not marked(first_user(path, 'codex')):
                    continue
                metadata = rollout_metadata(path)
                if metadata.get('source') != 'exec':
                    continue  # Marker alone cannot turn an app session into exec.
                row['crossfeed_total'] += 1
                cwd = metadata.get('cwd')
                if not isinstance(cwd, str):
                    raise CannotCheck('marked orphan rollout has no working directory')
                if str(Path(cwd).resolve()) in self.live_codex_cwds([cwd]):
                    row['live'] += 1
                elif self.stale(path.stat().st_mtime):
                    if self.in_use(path):
                        row['live'] += 1
                    else:
                        row['leftover'] += 1
                        targets.append(match[1])
                indexed.add(match[1])

    def agy(self, row, targets):
        root = self.home / '.gemini/antigravity-cli'
        owned = set()
        alive = self.agy_alive()
        for path in glob_checked(root / 'conversations', '*.db'):
            if not self.safe_path(path):
                continue
            with database(path) as conn:
                strings = agy_first_user(conn)
            if not any(worker_notice(text) for text in strings):
                continue
            owned.add(path.stem)
            paths = [path, Path(str(path) + '-wal'), Path(str(path) + '-shm'),
                     root / 'brain' / path.stem, root / 'annotations' / (path.stem + '.pbtxt'),
                     root / 'implicit' / (path.stem + '.pb')]
            # AGY has no stable per-run process ID in the DB: while any agy is
            # alive, preserve conversations active in the last six hours.
            # -shm is excluded: any read-only SQLite open (this census included) rewrites it.
            stamp = max(self.activity(p) for p in paths
                        if present(p) and self.safe_path(p) and not str(p).endswith('-shm'))
            self.file_item(row, targets, paths, stamp=stamp, live=alive and self.now - stamp < 21600)
        summary = root / 'conversation_summaries.db'
        if present(summary):
            # The running app owns this DB. Copy DB + WAL, never write it in place.
            with tempfile.TemporaryDirectory(prefix='crossfeed-summary-') as temp:
                copy = Path(temp) / summary.name
                shutil.copy2(summary, copy)
                wal = Path(str(summary) + '-wal')
                if present(wal):
                    shutil.copy2(wal, Path(str(copy) + '-wal'))
                with database(copy) as conn:
                    for record in conn.execute('SELECT conversation_id,preview FROM conversation_summaries'):
                        if record['conversation_id'] in owned or marked(record['preview']):
                            row['needs_owner_decision'] += 1

    def transcripts(self, row, targets):
        for pattern in ('dispatches/*/answer.txt', 'dispatches/*/stdout.log', 'runs/*/*.events.jsonl'):
            for path in glob_checked(self.state, pattern):
                if self.safe_path(path):
                    self.file_item(row, targets, [path])

    def session_files(self, row, targets, surface):
        roots = {'claude': self.home / '.claude/projects', 'pi': self.home / '.pi/agent/sessions',
                 'copilot': self.home / '.copilot/session-state'}
        root = roots[surface]
        if surface == 'copilot':
            for path in glob_checked(self.state / 'copilot-homes', 'run.*'):
                if path.is_dir() and self.safe_path(path):
                    self.file_item(row, targets, [path])
            for directory in root.iterdir() if present(root) else ():
                if not directory.is_dir() or not self.safe_path(directory):
                    continue
                # events.jsonl is Copilot's chronological event stream; an operator
                # chat quoting the notice in a later user event is never ours.
                paths = sorted(glob_checked(directory, 'events*.jsonl'))
                if paths and marked(first_user(paths[0], surface)):
                    self.file_item(row, targets, [directory])
            store = self.home / '.copilot/session-store.db'
            if present(store):
                with database(store) as conn:
                    columns = {r['name'] for r in conn.execute('PRAGMA table_info(turns)')}
                    order = 'turn_index' if 'turn_index' in columns else 'rowid'
                    records = conn.execute(f'SELECT session_id,user_message FROM turns ORDER BY {order}')
                    seen = set()
                    for record in records:
                        if record['session_id'] in seen:
                            continue
                        seen.add(record['session_id'])
                        if marked(record['user_message']):
                            row['needs_owner_decision'] += 1
            return
        if not present(root):
            return
        # Fast literal prefilter, including ignored paths. Parse only hits and
        # still inspect FIRST user content rather than trusting a substring hit.
        result = self.command(['rg', '-l', '-0', '-uuu', '-F', MARKER, '--glob', '*.jsonl', str(root)])
        if result.returncode not in (0, 1):
            raise CannotCheck('transcript prefilter could not run')
        for name in result.stdout.split('\0'):
            if name:
                path = Path(name)
                if self.safe_path(path) and marked(first_user(path, surface)):
                    self.file_item(row, targets, [path])

    def opencode(self, row, targets):
        for path in glob_checked(self.state / 'oc-homes', 'run.*'):
            if path.is_dir() and self.safe_path(path):
                self.file_item(row, targets, [path])
        if present(self.opencode_db):
            with database(self.opencode_db) as conn:
                # OpenCode stores roles and text in JSON message/part data. The
                # main DB belongs to the app: report marked sessions, never edit.
                sessions = conn.execute('SELECT id FROM session')
                for session in sessions:
                    message = conn.execute("SELECT id FROM message WHERE session_id=? AND json_extract(data,'$.role')='user' ORDER BY time_created,id LIMIT 1", (session['id'],)).fetchone()
                    if not message:
                        continue
                    parts = conn.execute("SELECT data FROM part WHERE message_id=? ORDER BY time_created,id", (message['id'],))
                    text = ''.join(part.get('text', '') for part in (json.loads(r['data']) for r in parts) if part.get('type') == 'text')
                    if marked(text):
                        row['crossfeed_total'] += 1
                        row['needs_owner_decision'] += 1

    def chatgpt(self, row, targets):
        try:
            from chatgpt_transport import settings, request, Rejected
        except ImportError:
            from scripts.chatgpt_transport import settings, request, Rejected
        lanes = [lane for lane in (self.roster or {}).get('lanes', []) if lane.get('harness') == 'chatgpt-chat']
        template = (self.roster or {}).get('chatgpt_gateway', {}).get('lane_template')
        lane = lanes[0] if lanes else template
        if not lane:
            if isinstance(self.roster, dict) and not lanes and 'chatgpt_gateway' not in self.roster:
                row['status'] = 'n/a (gateway not configured)'
                return
            raise CannotCheck('ChatGPT lane settings unavailable')
        if not lane.get('auth', {}).get('key_file') or lane.get('auth', {}).get('key_ref'):
            raise CannotCheck('ChatGPT census requires the lane key-file setting')
        try:
            base, key = settings(lane)
            status = request(base, key, '/gateway/status')
            checked_at = time.time() if getattr(self, 'live_clock', False) else self.now
            total, violations = chat_health(status, checked_at, self.threshold)
            if status['chat_disposal']['last_sweep']['rate_limited_until'] > checked_at * 1000:
                row['status'] = 'rate-limited'
        except Rejected:
            raise CannotCheck('Crossfeed Chat service or credentials unavailable') from None
        row['crossfeed_total'] = total
        row['violations'] = violations
        row['leftover'] = max(1, total) if violations else 0
        # Service-owned chats are disposed by the service, never by this scanner.

    def scan(self, surface):
        row = dict(crossfeed_total=0, leftover=0, live=0, needs_owner_decision=0,
                   unmarked_hint=0, status='ok')
        targets = []
        try:
            if surface in ('gemini-media', 'openrouter'):
                row['status'] = 'n/a (stateless API)'
            elif surface in ('claude', 'pi', 'copilot'):
                self.session_files(row, targets, surface)
            else:
                getattr(self, surface)(row, targets)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, CannotCheck) as exc:
            row['status'] = 'cannot_check'
            row['error'] = str(exc) if isinstance(exc, CannotCheck) else 'surface data unreadable or unsupported'
            targets = []  # Never partly purge a surface we could not classify.
        return row, targets

    def purge(self, surface, targets):
        # Reclassify immediately before disposal to catch refreshed/live sessions.
        self._ps = None
        self._open_names = None
        fresh, current = self.scan(surface)
        if fresh['status'] == 'cannot_check':
            return 0, len(targets), fresh['error']
        targets = [target for target in targets if target in current]
        if surface == 'codex':
            def delete(identifier):
                try:
                    return self.command(['codex', 'delete', '--force', identifier], timeout=60).returncode == 0
                except CannotCheck:
                    return False
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(delete, targets))
            return sum(results), len(results) - sum(results), None
        purged = failed = 0
        # Batch groups without splitting one conversation's associated files.
        for start in range(0, len(targets), 40):
            batch = targets[start:start + 40]
            files = [str(path) for group in batch for path in group]
            if not files:
                continue
            discard = os.environ.get('DISCARD', str(self.home / '.claude/scripts/discard'))
            try:
                ok = self.command([discard, *files, '--certain', '--reason', REASON], timeout=60).returncode == 0
            except CannotCheck:
                ok = False
            if ok:
                purged += len(batch)
            else:
                failed += len(batch)
        return purged, failed, None


def census(*, home=None, state_dir=None, codex_home=None, opencode_db=None, now=None,
           older_than=900, runner=subprocess.run, surfaces=None, roster=None, purge=False, yes=False):
    """Return counts only; candidates and secret-bearing item text never escape."""
    if not math.isfinite(older_than) or older_than < 0:
        raise ValueError('older-than must be a finite non-negative number')
    home = Path(home or Path.home())
    state_dir = Path(state_dir or os.environ.get('FLEET_STATE_DIR') or
                     Path(os.environ.get('XDG_STATE_HOME', str(home / '.local/state'))) / 'orchestrator')
    scanner = Census(home, state_dir,
                     codex_home or os.environ.get('CODEX_HOME') or home / '.codex',
                     opencode_db or os.environ.get('OPENCODE_DB') or home / '.local/share/opencode/opencode.db',
                     time.time() if now is None else now, older_than, runner, roster)
    # A long purge makes the start time stale; the ChatGPT status check then compares against it.
    scanner.live_clock = now is None
    result = {'older_than_seconds': older_than, 'purge': purge, 'confirmed': bool(purge and yes), 'surfaces': {}}
    exit_code = 0
    for surface in dict.fromkeys(surfaces or SURFACES):
        if surface not in SURFACES:
            raise ValueError('unknown surface')
        row, targets = scanner.scan(surface)
        if purge:
            row.update(purged=0, failed=0, planned=len(targets))
            if yes and targets:
                row['purged'], row['failed'], error = scanner.purge(surface, targets)
                if row['failed'] or error:
                    row['status'] = 'cannot_check'
                    row['error'] = error or 'one or more disposal commands failed'
        result['surfaces'][surface] = row
        if row['status'] == 'cannot_check':
            exit_code = 2
        elif row['leftover'] - row.get('purged', 0) > 0 or row['needs_owner_decision']:
            exit_code = max(exit_code, 1)
    result['exit_code'] = exit_code
    return result


def lines(result, compact=False):
    for name, row in result['surfaces'].items():
        remaining = row['leftover'] - row.get('purged', 0)
        counts = (f'total={row["crossfeed_total"]}, leftover={remaining}, live={row["live"]}, '
                  f'owner_decision={row["needs_owner_decision"]}, unmarked_hint={row["unmarked_hint"]}')
        if row['status'] == 'cannot_check':
            yield f'TRACE CANNOT CHECK {name}: {row["error"]} [{counts}]'
        elif remaining > 0:
            detail = ': ' + '; '.join(row['violations']) if row.get('violations') else ''
            yield (f'TRACE LEFTOVER {name}: {remaining} Crossfeed sessions/artifacts older than '
                   f'{result["older_than_seconds"]:g} s{detail} [{counts}]')
        elif row['needs_owner_decision']:
            yield f'TRACE OWNER DECISION {name}: marked app database rows, never purged [{counts}]'
        else:
            yield f'traces:    {name}: {row["status"]}, {counts}'
        if result['purge']:
            yield (f'  purge {name}: plan={row["planned"]}, purged={row["purged"]}, failed={row["failed"]}'
                   + (' (plan only, add --yes)' if not result['confirmed'] else ''))


def add_arguments(parser):
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--older-than', type=float, default=900, metavar='SECONDS')
    parser.add_argument('--surface', action='extend', nargs='+', choices=SURFACES)
    parser.add_argument('--purge', action='store_true')
    parser.add_argument('--yes', action='store_true')


def command(args, *, state_dir=None, roster=None, opencode_db=None):
    try:
        result = census(state_dir=state_dir, roster=roster, opencode_db=opencode_db,
                        older_than=args.older_than, surfaces=args.surface, purge=args.purge, yes=args.yes)
    except ValueError:
        print('TRACE CANNOT CHECK: older-than must be finite and non-negative')
        return 2
    print(json.dumps(result, indent=2) if args.json else '\n'.join(lines(result)))
    return result['exit_code']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--overlay', type=Path, default=Path(os.environ.get('ACCESS_OVERLAY', '~/.config/orchestrator/access-overlay.json')).expanduser())
    args = parser.parse_args(argv)
    try:
        roster = json.loads(args.overlay.read_text())
    except (OSError, ValueError):
        roster = None
    return command(args, state_dir=args.state_dir, roster=roster)


if __name__ == '__main__':
    raise SystemExit(main())
