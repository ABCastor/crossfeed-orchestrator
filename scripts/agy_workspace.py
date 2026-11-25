"""Build a disposable AGY snapshot and fingerprint the original worktree.

This is not a filesystem sandbox. It removes working-copy write-through paths
and detects persistent changes to the original; it cannot undo external writes.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(root), *args], env={**env, 'GIT_OPTIONAL_LOCKS': '0'},
                          capture_output=True, check=check)


def worktree(target: Path) -> tuple[Path, bool]:
    # Detect the actual .git directory or linked-worktree pointer, not ambient Git env.
    for parent in (target, *target.parents):
        if os.path.lexists(parent / '.git'):
            root = git(target, 'rev-parse', '--show-toplevel').stdout.rstrip(b'\n')
            return Path(os.fsdecode(root)).resolve(strict=True), True
    return target, False


def paths(root: Path, repo: bool) -> list[Path]:
    if repo:
        raw = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').stdout
        entries = {Path(os.fsdecode(name)) for name in raw.split(b'\0') if name}
        # Never follow a replaced tracked directory (a symlink) back into the source.
        return sorted(entries | {p for path in entries for p in path.parents if p != Path('.')})
    return sorted(p.relative_to(root) for p in root.rglob('*'))


def record(root: Path, repo: bool, target: Path, copy: Path | None = None) -> str:
    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)

    add(os.fsencode(root))
    add(os.fsencode(target))
    if repo:
        add(git(root, 'ls-files', '-z', '--stage').stdout)
        add(git(root, 'symbolic-ref', '-q', 'HEAD', check=False).stdout)
        add(git(root, 'rev-parse', '-q', '--verify', 'HEAD', check=False).stdout)
    blocked: set[Path] = set()
    for relative in paths(root, repo):
        if '.git' in relative.parts or any(p in blocked for p in relative.parents):
            continue
        source = root / relative
        add(os.fsencode(relative))
        try:
            info = source.lstat()
        except (FileNotFoundError, NotADirectoryError):
            add(b'missing')
            continue
        mode = info.st_mode
        add(str(mode).encode())
        dest = (copy / source.relative_to(target)
                if copy is not None and source != target and target in source.parents else None)
        if stat.S_ISDIR(mode):
            # A Git submodule is another worktree and isn't covered by the outer index.
            if os.path.lexists(source / '.git'):
                raise ValueError(f'nested Git worktree is unsupported: {relative}')
            if dest is not None:
                dest.mkdir(parents=True, exist_ok=True)
            continue
        blocked.add(relative)
        if stat.S_ISLNK(mode):
            link = os.readlink(source)
            add(os.fsencode(link))
            if dest is not None:
                if os.path.isabs(link):
                    raise ValueError(f'absolute symlink is unsupported: {relative}')
                dest.symlink_to(link)
        elif stat.S_ISREG(mode):
            content = hashlib.sha256()
            with source.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b''):
                    content.update(chunk)
            add(content.digest())
            if dest is not None:
                shutil.copyfile(source, dest, follow_symlinks=False)
                dest.chmod(stat.S_IMODE(mode) & 0o777)
        else:
            raise ValueError(f'unsupported file type: {relative}')
    return digest.hexdigest()


def inside(path: Path, root: Path) -> bool:
    # samefile also handles a case-insensitive filesystem's alternate spellings.
    return any(p.exists() and p.samefile(root) for p in (path, *path.parents))


def check_outputs(root: Path, outputs: list[str]) -> None:
    for output in filter(None, outputs):
        path = Path(output).resolve()
        if inside(path, root) or (path.exists() and path.stat().st_nlink > 1):
            raise ValueError('read-only output files must be outside the original worktree and not hardlinked')


# BR-AGY-001
def main() -> None:
    action, original, scratch, *args = sys.argv[1:]
    if action == 'clean':
        shutil.rmtree(scratch)
        return
    target = Path(original).resolve(strict=True)
    root, repo = worktree(target)
    copy = Path(scratch).resolve(strict=True) / 'workspace'
    if inside(copy, root):
        raise ValueError('read-only scratch must be outside the original worktree')
    if action == 'prepare':
        check_outputs(root, args)
        copy.mkdir()
        before = record(root, repo, target, copy)
        for path in copy.rglob('*'):
            if path.is_symlink():
                resolved = path.resolve()
                if resolved != copy and copy not in resolved.parents:
                    raise ValueError(f'symlink leaves the snapshot: {path.relative_to(copy)}')
        if record(root, repo, target) != before:
            raise ValueError('original workspace changed while preparing its snapshot')
        print(before)
    elif action == 'verify':
        before, *outputs = args
        if record(root, repo, target) != before:
            raise ValueError('original workspace changed during read-only run; result discarded')
        check_outputs(root, outputs)
    else:
        raise ValueError('unknown workspace action')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit(f'agy-agent: READ-ONLY WORKSPACE CHECK FAILED: {exc}')
