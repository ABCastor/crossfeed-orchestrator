#!/usr/bin/env python3
"""Mine mechanically verified fail-to-pass fixtures from approved read-only repos."""
import argparse
import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_BYTES = 50 * 1024 * 1024
SOURCE_SUFFIXES = {'.py', '.sh', '.bash', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.go', '.rs'}
SECRET_CONTENT = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|(?:AKIA|ASIA)[A-Z0-9]{16}|(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9_-]{32,})')


class Rejected(ValueError):
    pass


def git(repo, *args, input=None):
    return subprocess.check_output(['git', '-C', str(repo), *args], input=input,
                                   stderr=subprocess.PIPE, timeout=30)


def is_test(name):
    path = PurePosixPath(name)
    return (any(p in ('test', 'tests', '__tests__') for p in path.parts[:-1]) or
            bool(re.match(r'^(?:test[_-]|test\.)', path.name)) or
            bool(re.search(r'(?:[_-]test|\.test|\.spec)\.', path.name)))


def safe_path(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or '\\' in name:
        return False
    forbidden = {'.git', '.aws', '.ssh', '.codex', '.claude', 'node_modules', '__pycache__', '.venv', 'venv',
                 'secrets', 'credentials', 'tokens'}
    if any(part.lower() in forbidden for part in p.parts):
        return False
    base = p.name.lower()
    return not (base.startswith('.env') or base in ('.netrc', '.npmrc', '.pypirc', 'auth.json', 'credentials', 'credentials.json', 'id_rsa', 'id_ed25519')
                or base.endswith(('.pem', '.key', '.p12', '.pfx')) or
                re.search(r'(?:^|[._-])(?:secrets?|tokens?|credentials?)(?:[._-]|$)', base))


def tree(repo, commit):
    entries = {}
    for record in git(repo, 'ls-tree', '-rz', '--long', commit).split(b'\0'):
        if not record:
            continue
        info, name = record.split(b'\t', 1)
        mode, kind, oid, size = info.decode().split()
        name = name.decode('utf-8')
        if safe_path(name) and kind == 'blob' and mode in ('100644', '100755'):
            entries[name] = (mode, oid, int(size))
    if sum(v[2] for v in entries.values()) > MAX_BYTES:
        raise Rejected('workspace_over_50mb')
    return entries


def export(repo, entries, root):
    root.mkdir(parents=True, exist_ok=True)
    # Resolve ignore rules before copying payload into the workspace.
    with tempfile.TemporaryDirectory(prefix='history-ignore-') as temp:
        gd, rules = Path(temp) / 'metadata', Path(temp) / 'rules'
        rules.mkdir()
        for name, (_, oid, _) in entries.items():
            if PurePosixPath(name).name == '.gitignore':
                content = git(repo, 'cat-file', 'blob', oid)
                if SECRET_CONTENT.search(content):
                    raise Rejected('secret_content')
                path = rules / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        subprocess.run(['git', 'init', '--bare', '-q', str(gd)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        names = b'\0'.join(n.encode() for n in entries)+b'\0'
        process = subprocess.run(['git', '--git-dir='+str(gd), '--work-tree='+str(rules),
                                  'check-ignore', '--no-index', '-z', '--stdin'], input=names,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        current_ignored = subprocess.run(['git', '-C', str(repo), 'check-ignore', '--no-index', '-z', '--stdin'],
                                        input=names, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if process.returncode not in (0, 1) or current_ignored.returncode not in (0, 1):
            raise Rejected('ignore_check_failed')
        ignored = {v.decode() for v in process.stdout.split(b'\0')+current_ignored.stdout.split(b'\0') if v}
    for name, (mode, oid, _) in entries.items():
        if name in ignored:
            continue
        content = git(repo, 'cat-file', 'blob', oid)
        if SECRET_CONTENT.search(content):
            raise Rejected('secret_content')
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755 if mode == '100755' else 0o644)
    return manifest(root)


def manifest(root, ignored=()):
    result = {}
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        if any(part in ('.git', '__pycache__', '.pytest_cache') for part in path.relative_to(root).parts) or name in ignored:
            continue
        if path.is_symlink():
            result[name] = 'symlink:'+os.readlink(path)
        elif path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def boundary_snapshot(base, workspace, excluded=()):
    """Observe sibling writes inside the isolated cell; this is not an OS sandbox."""
    base, workspace = Path(base).resolve(), Path(workspace).resolve()
    exclusions = {Path(p).resolve() for p in excluded}
    observed = {}
    for path in base.rglob('*'):
        if path == workspace or workspace in path.parents or path.absolute() in exclusions:
            continue
        name = path.relative_to(base).as_posix()
        if path.is_symlink():
            observed[name] = 'symlink:'+os.readlink(path)
        elif path.is_file():
            observed[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            observed[name+'/'] = 'directory'
    return observed


def boundary_unchanged(before, base, workspace, excluded=()):
    return before == boundary_snapshot(base, workspace, excluded)


def test_command(root, touched):
    runnable = [name for name in touched if Path(name).suffix in ('.py', '.sh', '.bash', '.js', '.mjs', '.cjs', '.ts')
                and (root / name).is_file()]
    if not runnable:
        raise Rejected('no_runnable_touched_tests')
    python = [p for p in runnable if p.endswith('.py')]
    node = [p for p in runnable if Path(p).suffix in ('.js', '.mjs', '.cjs', '.ts')]
    bash = [p for p in runnable if Path(p).suffix in ('.sh', '.bash')]
    commands = []
    if python:
        if any(re.search(r'^\s*(?:import pytest|from pytest\b)', (root/p).read_text(), re.M) or
               any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_')
                   for node in ast.parse((root/p).read_text()).body) for p in python):
            commands.append([sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *python])
        else:
            # Load only named files. Discovery preserves unittest imports without running unrelated tests.
            script = ('import sys, unittest\n'
                      'sys.path.insert(0, ".")\n'
                      'loader = unittest.TestLoader()\n'
                      'suite = unittest.TestSuite()\n'
                      'for name in '+repr(python)+':\n'
                      '    from pathlib import Path\n'
                      '    path = Path(name)\n'
                      '    if (path.parent / "__init__.py").exists():\n'
                      '        suite.addTests(loader.loadTestsFromName(name[:-3].replace("/", ".")))\n'
                      '    else:\n'
                      '        suite.addTests(loader.discover(str(path.parent), pattern=path.name))\n'
                      'result = unittest.TextTestRunner(verbosity=1).run(suite)\n'
                      'sys.exit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)\n')
            commands.append([sys.executable, '-B', '-c', script])
    if node:
        commands.append(['node', '--test', *node])
    commands += [['bash', name] for name in bash]
    # A single invocation lets the timeout bound the sum of mixed runners.
    script = ('import subprocess, sys\n'
              'for command in '+repr(commands)+':\n'
              '    code = subprocess.call(command)\n'
              '    if code: sys.exit(code)\n')
    return [sys.executable, '-B', '-c', script]


def run_tests(root, command, timeout):
    root = Path(root)
    with tempfile.TemporaryDirectory(prefix='history-run-') as temp:
        isolated = Path(temp)
        home = isolated / 'home'
        home.mkdir()
        environment = {'PATH':os.environ.get('PATH', '/usr/bin:/bin'), 'HOME':str(home),
                       'TMPDIR':str(isolated), 'LANG':'C.UTF-8', 'PYTHONDONTWRITEBYTECODE':'1',
                       'PYTHONNOUSERSITE':'1', 'CI':'1'}
        before = boundary_snapshot(isolated, root)
        repo_boundary = boundary_snapshot(root.parent, root)
        started = time.monotonic()
        with (isolated/'output').open('w+b') as output:
            proc = subprocess.Popen(command, cwd=root, env=environment, stdout=output,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            duration = time.monotonic()-started
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell()-65536))
            tail = '\n'.join(output.read().decode('utf-8', errors='replace').splitlines()[-60:])
        outside_ok = (boundary_unchanged(before, isolated, root, [isolated/'output']) and
                      boundary_unchanged(repo_boundary, root.parent, root))
        return dict(exit_code=proc.returncode, timed_out=timed_out, duration_s=round(duration,6),
                    tail=tail, outside_ok=outside_ok)


def difficulty(lines, files):
    if lines <= 20 and files <= 1:
        return 'easy'
    if lines <= 80 and files <= 3:
        return 'medium'
    if lines <= 250 and files <= 6:
        return 'hard'
    return 'expert'


def changed_files(repo, parent, commit):
    # Disable rename detection so both sides of renames are represented.
    return [n.decode() for n in git(repo, 'diff', '--no-renames', '--name-only', '-z', parent, commit).split(b'\0') if n]


def make_candidate(repo, commit, destination, timeout=90):
    parent = git(repo, 'rev-parse', commit+'^').decode().strip()
    changed = changed_files(repo, parent, commit)
    tests = [n for n in changed if is_test(n)]
    non_tests = [n for n in changed if not is_test(n)]
    with tempfile.TemporaryDirectory(prefix='history-candidate-') as temp:
        scratch = Path(temp)
        workspace, reference = scratch/'workspace', scratch/'reference'
        parent_tree, fixed_tree = tree(repo, parent), tree(repo, commit)
        combined = dict(parent_tree)
        for name in tests:
            combined.pop(name, None)
            if name in fixed_tree:
                combined[name] = fixed_tree[name]
        if sum(v[2] for v in combined.values()) > MAX_BYTES:
            raise Rejected('workspace_over_50mb')
        original = export(repo, combined, workspace)
        fixed = export(repo, fixed_tree, scratch/'expected')
        if any(n not in original and n not in fixed for n in changed):
            raise Rejected('excluded_changed_file')
        # A filtered path cannot leak into the reference diff.
        if any(not safe_path(n) for n in changed):
            raise Rejected('unsafe_changed_file')
        diff = git(repo, 'diff', '--binary', '--no-renames', parent, commit, '--', *non_tests)
        if SECRET_CONTENT.search(diff):
            raise Rejected('secret_content')
        shutil.copytree(workspace, reference)
        applied = subprocess.run(['git', 'apply', '--no-index', '--binary', '-'], cwd=reference,
                                 input=diff, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if applied.returncode or manifest(reference) != fixed:
            raise Rejected('reference_patch_export_mismatch')
        command = test_command(workspace, tests)
        failing = run_tests(workspace, command, timeout)
        if failing['timed_out'] or failing['duration_s'] >= timeout:
            raise Rejected('parent_timeout')
        if not failing['outside_ok']:
            raise Rejected('parent_outside_write')
        if failing['exit_code'] == 0:
            raise Rejected('parent_tests_green')
        if manifest(workspace) != original:
            raise Rejected('parent_test_side_effect')
        passing = run_tests(reference, command, timeout)
        if passing['timed_out'] or passing['duration_s'] >= timeout:
            raise Rejected('reference_timeout')
        if not passing['outside_ok']:
            raise Rejected('reference_outside_write')
        if passing['exit_code'] != 0:
            raise Rejected('reference_tests_fail')
        if manifest(reference) != fixed:
            raise Rejected('reference_test_side_effect')
        if SECRET_CONTENT.search(diff) or SECRET_CONTENT.search(failing['tail'].encode()):
            raise Rejected('secret_content')
        sizes = git(repo, 'diff', '--numstat', parent, commit, '--', *non_tests).decode().splitlines()
        lines = sum(int(part) for row in sizes for part in row.split('\t')[:2] if part.isdigit())
        tier = difficulty(lines, len(changed))
        test_hashes = {n:v for n,v in original.items() if is_test(n)}
        check = dict(kind='history-fix', test_command=command, timeout_s=timeout,
                     tests=test_hashes, reference_files=non_tests, original_files=original)
        metadata = dict(family='history-fix', difficulty=tier, tier=tier, template_id='git-fail-to-pass',
                        seed=int(commit[:12],16), repo=repo.name, commit=commit, parent=parent,
                        files_touched=changed, lines_changed_non_test=lines, test_command=command)
        destination.mkdir(parents=True)
        shutil.copytree(workspace, destination/'workspace')
        shutil.copytree(reference, destination/'reference/workspace')
        (destination/'reference/reply.txt').write_text('Applied the repair.\n')
        (destination/'reference.patch').write_bytes(diff)
        prompt = ('Make the failing tests pass: '+', '.join(tests)+'. Do not edit test files.\n\n'
                  'Repair the repository in this directory. Keep edits inside it.\n\n'
                  'Failing test output (last 60 lines):\n```text\n'+failing['tail']+'\n```\n')
        (destination/'PROMPT.md').write_text(prompt)
        for name, value in (('meta.json', metadata), ('check.json', check),
                            ('mining.json', dict(parent=failing, reference=passing))):
            (destination/name).write_text(json.dumps(value, indent=2, sort_keys=True)+'\n')
    return destination


def mine(repo, out, since=None, maximum=None, timeout=90):
    repo, out = Path(repo).resolve(), Path(out)
    if out.is_symlink() or out.exists() and any(out.iterdir()):
        raise ValueError('use a fresh, non-symlink output directory')
    out.mkdir(parents=True, exist_ok=True)
    args = ['log', '--no-merges', '--format=%H']
    if since:
        args.append('--since='+since)
    commits = git(repo, *args).decode().splitlines()
    counts, tasks = Counter(), []
    for commit in commits:
        try:
            parent = git(repo, 'rev-parse', commit+'^').decode().strip()
            changed = changed_files(repo, parent, commit)
        except subprocess.SubprocessError:
            continue
        if not any(is_test(n) for n in changed) or not any(not is_test(n) and Path(n).suffix in SOURCE_SUFFIXES for n in changed):
            continue
        if maximum is not None and counts['candidates'] >= maximum:
            break
        counts['candidates'] += 1
        try:
            task = make_candidate(repo, commit, out/('history-fix-'+commit[:12]), timeout)
            tasks.append(task)
            counts['kept'] += 1
        except Rejected as exc:
            counts[str(exc)] += 1
        except (OSError, UnicodeError, SyntaxError, subprocess.SubprocessError) as exc:
            counts['execution_error_'+type(exc).__name__] += 1
        (out/'progress.json').write_text(json.dumps(dict(counts), sort_keys=True)+'\n')
    summary = dict(repo=repo.name, candidates=counts.pop('candidates',0), kept=counts.pop('kept',0),
                   rejected_by_reason=dict(sorted(counts.items())))
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return tasks, summary


def check_history(task, workspace, check):
    from bench.check import result, no_symlinks
    task, workspace = Path(task), Path(workspace)
    no_symlinks(workspace)
    baseline = manifest(task/'workspace')
    tests = {n:v for n,v in baseline.items() if is_test(n)}
    if tests != check['tests'] or baseline != check['original_files']:
        return result(False, 'trusted history manifest changed')
    observed = manifest(workspace, ignored=('PROMPT.md',))
    if {n:v for n,v in observed.items() if is_test(n)} != tests:
        return result(False, 'test files changed')
    changed = sorted(n for n in baseline.keys() | observed.keys() if baseline.get(n) != observed.get(n))
    extra = sorted(set(changed)-set(check['reference_files']))
    with tempfile.TemporaryDirectory(prefix='history-check-') as temp:
        candidate = Path(temp)/'repo'
        shutil.copytree(workspace, candidate, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache'))
        executed = run_tests(candidate, check['test_command'], check['timeout_s'])
        tests_after = {n:v for n,v in manifest(candidate).items() if is_test(n)}
    passed = (executed['exit_code'] == 0 and not executed['timed_out'] and executed['outside_ok'] and tests_after == tests)
    return dict(result(passed, 'touched tests passed' if passed else 'touched tests failed or test/scope integrity changed'),
                scope_extra_files=extra, test_duration_s=executed['duration_s'],
                scope_global_enforced=False, scope_boundary='isolated-cell-parent')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, action='append', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--allow-repo', type=Path, action='append', default=[], help='explicitly approve a read-only input repository; repeat for each input')
    parser.add_argument('--since')
    parser.add_argument('--max', type=int)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--families', choices=('fix',), default='fix')
    args = parser.parse_args(argv)
    if args.timeout <= 0 or not __import__('math').isfinite(args.timeout) or args.max is not None and args.max < 1:
        parser.error('timeout and max must be positive')
    allowed = {path.expanduser().resolve() for path in args.allow_repo}
    repos = [p.expanduser().resolve() for p in args.repo]
    if any(repo not in allowed for repo in repos) or len(set(repos)) != len(repos):
        parser.error('each input needs explicit --allow-repo approval, once each')
    if any(repo == args.out.resolve() or repo in args.out.resolve().parents for repo in repos):
        parser.error('output must be outside the read-only input repos')
    if args.out.is_symlink() or args.out.exists() and any(args.out.iterdir()):
        parser.error('use a fresh output directory')
    print('repo | candidates | kept | rejected by reason', flush=True)
    summaries = []
    for repo in repos:
        _, summary = mine(repo, args.out/repo.name if len(repos)>1 else args.out, args.since, args.max, args.timeout)
        summaries.append(summary)
        reasons = ', '.join('%s=%s'%item for item in summary['rejected_by_reason'].items()) or '-'
        print('%s | %d | %d | %s'%(repo.name,summary['candidates'],summary['kept'],reasons), flush=True)
    if len(repos)>1:
        (args.out/'summary.json').write_text(json.dumps(summaries,indent=2)+'\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
