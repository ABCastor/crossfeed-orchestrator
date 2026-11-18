#!/usr/bin/env python3
"""Delete only this run's native sessions, after run_identity has captured its receipt."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

THREAD_ID = re.compile(r'[a-f0-9-]{36}\Z')


def worker_notice(text: str, run_id: str | None = None) -> bool:
    if not isinstance(text, str) or not text.startswith('Crossfeed selected model:'):
        return False
    header, delimiter, _ = text.partition('=== CROSSFEED TASK ===')
    return bool(delimiter) and (run_id is None or bool(run_id) and f'Crossfeed run {run_id}.' in header)


def _protobuf_fields(payload):
    """Yield (field, wire, value) for one protobuf message; malformed input raises ValueError."""
    pos = 0
    def integer():
        nonlocal pos
        result = 0
        for shift in range(0, 70, 7):
            if pos >= len(payload):
                raise ValueError('truncated AGY user-input protobuf')
            byte = payload[pos]
            pos += 1
            result |= (byte & 127) << shift
            if byte < 128:
                return result
        raise ValueError('invalid AGY user-input protobuf varint')
    while pos < len(payload):
        tag = integer()
        wire = tag & 7
        if wire == 0:
            integer()
        elif wire in (1, 5):
            pos += 8 if wire == 1 else 4
        elif wire == 2:
            size = integer()
            end = pos + size
            if end > len(payload):
                raise ValueError('truncated AGY user-input protobuf string')
            yield tag >> 3, wire, payload[pos:end]
            pos = end
        else:
            raise ValueError('unsupported AGY user-input protobuf wire type')
        if pos > len(payload):
            raise ValueError('truncated AGY user-input protobuf field')


def protobuf_strings(payload):
    """Decode wire fields, not arbitrary marker bytes inside another field.

    The user text of an AGY step_type 14 payload is a field-2 string, either at top level
    or nested under field 19. Only those two paths count; malformed payloads fail closed.
    """
    for field, _wire, value in _protobuf_fields(payload):
        if field == 2:
            yield value.decode('utf-8')
        elif field == 19:
            for inner_field, _inner_wire, inner in _protobuf_fields(value):
                if inner_field == 2:
                    yield inner.decode('utf-8')


def agy_first_user(db) -> list[str]:
    row = db.execute('SELECT step_payload FROM steps WHERE step_type = 14 ORDER BY idx LIMIT 1').fetchone()
    return list(protobuf_strings(row[0])) if row and row[0] is not None else []


def codex_cleanup(home: Path, events: Path, run_id: str) -> None:
    roots = []
    if not events.is_file():
        return
    for line in events.read_text(errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'thread.started':
            tid = event.get('thread_id')
            if isinstance(tid, str) and THREAD_ID.fullmatch(tid):
                roots.append(tid)
    if not roots:
        return
    children: dict[str, list[str]] = {}
    verified = set()
    errors = []
    database = home / 'state_5.sqlite'
    if database.exists():
        try:
            with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                for tid, source, first in db.execute('SELECT id, source, first_user_message FROM threads'):
                    if run_id and worker_notice(first, run_id):
                        verified.add(tid)
                    try:
                        parent = json.loads(source).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if isinstance(tid, str) and THREAD_ID.fullmatch(tid) and isinstance(parent, str):
                        children.setdefault(parent, []).append(tid)
        except sqlite3.Error:
            errors.append('cannot read Codex sub-agent thread database')
    for root in roots:
        if root not in verified:
            print('codex-agent: SESSION CLEANUP SKIPPED: unverified thread', file=sys.stderr)
    roots = [root for root in roots if root in verified]
    visited = set()
    def delete(tid):
        if tid in visited:
            return
        visited.add(tid)
        for child in children.get(tid, []):
            delete(child)
        try:
            # Do not echo native CLI output: it is not a metadata-only contract.
            result = subprocess.run(['codex', 'delete', '--force', tid],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            if result.returncode:
                errors.append(f'codex delete exited {result.returncode}')
        except (OSError, subprocess.TimeoutExpired):
            errors.append('codex delete could not complete')
    for root in roots:
        delete(root)
    if errors:
        raise RuntimeError('; '.join(dict.fromkeys(errors)))


def agy_snapshot(root: Path) -> dict[str, list[int]]:
    result = {}
    for path in (root / 'conversations').glob('*.db'):
        stat = path.stat()
        # WAL writes can change the first user step without changing the DB itself.
        signature = [stat.st_mtime_ns, stat.st_size]
        wal = Path(str(path) + '-wal')
        try:
            wal_stat = wal.stat()
            signature += [wal_stat.st_mtime_ns, wal_stat.st_size]
        except FileNotFoundError:
            signature += [0, 0]
        result[path.name] = signature
    return result


def agy_cleanup(root: Path, before: dict, run_id: str) -> None:
    errors = []
    for name, signature in agy_snapshot(root).items():
        if before.get(name) == signature:
            continue
        path = root / 'conversations' / name
        try:
            with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                strings = agy_first_user(db)
            if not run_id or not any(worker_notice(text, run_id) for text in strings):
                continue
            # Only a decoded notice in the FIRST user step authorises disposal.
            tid = path.stem
            for suffix in ('', '-wal', '-shm'):
                Path(str(path) + suffix).unlink(missing_ok=True)
            brain = root / 'brain' / tid
            if brain.exists():
                shutil.rmtree(brain)
            (root / 'annotations' / (tid + '.pbtxt')).unlink(missing_ok=True)
            (root / 'implicit' / (tid + '.pb')).unlink(missing_ok=True)
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            errors.append(f'{name}: {type(exc).__name__}')
    if errors:
        raise RuntimeError('; '.join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('codex', 'agy-snapshot', 'agy'))
    parser.add_argument('--events', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--run-id')
    args = parser.parse_args()
    root = Path.home() / '.gemini/antigravity-cli'
    try:
        if args.action == 'codex':
            codex_cleanup(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser(), args.events, args.run_id)
        elif args.action == 'agy-snapshot':
            args.snapshot.write_text(json.dumps(agy_snapshot(root)))
        else:
            agy_cleanup(root, json.loads(args.snapshot.read_text()), args.run_id)
    except (OSError, ValueError, RuntimeError) as exc:
        # The shell owns status preservation. Emit exactly one loud metadata-only diagnostic.
        wrapper = 'codex-agent' if args.action == 'codex' else 'agy-agent'
        print(f'{wrapper}: SESSION CLEANUP FAILED: {str(exc).replace(chr(10), " ")}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
