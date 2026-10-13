# Install the agent skill

Use Python 3.9 or newer, bash and jq. Install and sign in to the vendor CLIs you intend to use, then copy the example overlay into `~/.config/orchestrator/access-overlay.json`. Keep only your configured lanes verified. Run `python3 scripts/fleetctl.py doctor` to identify missing prerequisites.

The skill needs its references and harness readers beside `SKILL.md`, plus the repository scripts. For a checkout at `~/crossfeed-orchestrator`:

```bash
mkdir -p ~/.claude/skills/crossfeed-orchestrator
ln -s ~/crossfeed-orchestrator/skill/SKILL.md ~/.claude/skills/crossfeed-orchestrator/SKILL.md
ln -s ~/crossfeed-orchestrator/skill/references ~/.claude/skills/crossfeed-orchestrator/references
ln -s ~/crossfeed-orchestrator/skill/readers ~/.claude/skills/crossfeed-orchestrator/readers
ln -s ~/crossfeed-orchestrator/scripts ~/.claude/skills/crossfeed-orchestrator/scripts
ln -s ~/crossfeed-orchestrator/docs ~/.claude/skills/crossfeed-orchestrator/docs
```

For another harness, make the same layout in its skills directory and use the corresponding file in `skill/readers/`. Agents can also read `skill/SKILL.md` directly from the checkout. Commands run from the checkout root or the installed skill directory.

## Upgrading an existing installation

Keep one installation and point harness links at it. Preserve optional `LIVE-RULES.md` and other operator-owned files byte-for-byte. They hold private policy and stay outside this repository. Do not replace unknown local files or remove legacy links without inspecting them.

Stage changes under `~/.local/state/orchestrator-install/`, verify reference and script paths, run `bash tests/run-all.sh` with synthetic configuration, and compare source hashes before replacement. Keep the recoverable old tree there too, outside every harness's skills directory, so neither staging nor backups register as duplicate skills. Read back installed hashes and links afterward. `doctor` checks adapter/version/identity drift; it does not prove installed skill source parity.

Quota source behavior is documented in [Quota refresh](QUOTA-REFRESH.md). The optional Claude Code model-switch hook is `scripts/hooks/model-switch-guard.py`, registered as a `PreToolUse` hook for `Bash|Agent|Task`.

### Install a reviewed merge into an existing real directory

The integrating chat owns this procedure. Run it after the branch is reviewed and merged, from the clean merged checkout. Do not install from a worker worktree. Pause new dispatches and wait for active workers to finish, since wrappers read sibling scripts during a run. Check the live skill's writer claim and ensure no other installer is active. These steps update code only: they do not change the access overlay, credentials, spend levels or switches, and they make no provider call.

Set these paths to the merged checkout and the existing installation. `LIVE_ROOT` must be a real directory. A symlink installation already follows its target checkout and needs that target reviewed instead.

```bash
export SOURCE_ROOT="$HOME/Second Brain/2_Code/orchestrator"
export LIVE_ROOT="$HOME/.claude/skills/orchestrator"
git -C "$SOURCE_ROOT" status --short
git -C "$SOURCE_ROOT" log -3 --oneline
test -d "$LIVE_ROOT" && test ! -L "$LIVE_ROOT"
export INSTALL_STATE_ROOT="$HOME/.local/state/orchestrator-install"
mkdir -p "$INSTALL_STATE_ROOT"
export STAGE_ROOT="$(mktemp -d "$INSTALL_STATE_ROOT/orchestrator.stage.XXXXXX")"
export BACKUP_ROOT="$INSTALL_STATE_ROOT/orchestrator.before-$(date +%Y%m%d-%H%M%S)-$$"
cp -a "$LIVE_ROOT/." "$STAGE_ROOT/"
```

Compare incoming managed files with the live versions before replacement. The previous `INSTALL-SOURCE.json` identifies managed files and their installed hashes. A first upgrade, a locally changed managed file, or an incoming path that collides with an unknown operator file stops staging. For files you have inspected and confirmed are managed code, write a JSON file containing a list of relative installed paths, for example `["scripts/fleetctl.py"]`, and set `APPROVED_REPLACEMENTS` to that file's absolute path, such as `$INSTALL_STATE_ROOT/approved-replacements.json`. The variable contains a path, not inline JSON. Preserve private collisions and resolve their paths before installing; never put them on this list. The copy below uses tracked files only, so local run artifacts never travel from the checkout. It flattens `skill/references` and `skill/readers` into the installation, preserves private `SKILL.md`, and records the complete original tree. Any managed symlink stops staging for inspection.

```bash
python3 - <<'PY'
import hashlib, json, os, pathlib, shutil, subprocess
source, live, stage = (pathlib.Path(os.environ[k]) for k in ('SOURCE_ROOT', 'LIVE_ROOT', 'STAGE_ROOT'))
assert stage.resolve().parent == pathlib.Path(os.environ['INSTALL_STATE_ROOT']).resolve(), 'Stage must be in install state'
assert not stage.resolve().is_relative_to(live.parent.resolve()), 'Stage must be outside skills'
subprocess.run(['git', '-C', str(source), 'diff', '--exit-code', 'HEAD'], check=True)
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
def inventory(root):
    return {str(p.relative_to(root)): {'link': os.readlink(p)} if p.is_symlink() else
            {'sha256': digest(p)} if p.is_file() else {'directory': True}
            for p in root.rglob('*')}
original = inventory(live)
assert inventory(stage) == original, 'Live changed during initial copy'
previous = json.loads((live / 'INSTALL-SOURCE.json').read_text()) if (live / 'INSTALL-SOURCE.json').is_file() else {}
approved = json.loads(pathlib.Path(os.environ['APPROVED_REPLACEMENTS']).read_text()) if os.environ.get('APPROVED_REPLACEMENTS') else []
assert isinstance(approved, list) and all(isinstance(p, str) for p in approved)
paths = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z', '--',
    'scripts', 'docs', 'tests', 'bench', 'skill/references', 'skill/readers',
    'examples', 'README.md', 'LICENSE', 'NOTICE', 'BUSINESS-RULES.md']).decode().split('\0')
manifest = {'revision': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD']).decode().strip(),
            'files': {}, 'before': {}, 'private': {}, 'live_tree': original}
for relative in filter(None, paths):
    target = relative.removeprefix('skill/')
    src, dst = source / relative, stage / target
    if src.is_symlink() or dst.is_symlink() or any(p.is_symlink() for p in dst.parents):
        raise SystemExit('Inspect managed symlink before staging: ' + target)
    if (live / target).exists() and target not in approved:
        assert target in previous.get('files', {}), 'Unknown operator collision: ' + target
        assert digest(live / target) == previous['files'][target], 'Locally changed managed file: ' + target
    manifest['before'][target] = digest(live / target)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    manifest['files'][target] = digest(src)
for name in ('SKILL.md', 'LIVE-RULES.md'):
    manifest['private'][name] = digest(live / name)
(stage / 'INSTALL-SOURCE.json').write_text(json.dumps(manifest, indent=2) + '\n')
print('Staged revision', manifest['revision'])
PY
```

Review the stage's private `SKILL.md` and `LIVE-RULES.md` against the approved policy. For the known stale MiMo sentence, replace only `MiMo Pro remains rejected.` with: `MiMo V2.6 Pro and Flash are approved via OpenRouter with explicit opt-in and the existing shared daily cap; no automatic paid fallback. This does not admit other MiMo versions or sources.` Keep the surrounding Gemini cap and Use balance instruction. If either private file has another contradictory restriction, resolve it before installing. Their original hashes stay in the manifest so later live drift is detected. Do not copy the public skill over private policy.

Record the reviewed private files before testing so later changes to them also stop replacement:

```bash
python3 - <<'PY'
import hashlib, json, os, pathlib
stage = pathlib.Path(os.environ['STAGE_ROOT'])
path = stage / 'INSTALL-SOURCE.json'
manifest = json.loads(path.read_text())
manifest['accepted_private'] = {name: hashlib.sha256((stage / name).read_bytes()).hexdigest()
    if (stage / name).is_file() else None for name in manifest['private']}
path.write_text(json.dumps(manifest, indent=2) + '\n')
PY
```

Run the staged suite through the established durable launcher. Use a new job name on every attempt. The fixtures isolate runtime state; do not point tests at the live overlay or state directory. Use `/tmp` for disposable test fixtures: paths with spaces break an existing consumer-test parser, fixtures inside a checkout inherit its Git worktree identity and trigger the write-overlap guard, and the literal `/private/tmp` path trips an existing payload-leak test. Keep persistent outputs and caches inside the stage or on an external disk. Read the complete log and require exit zero before continuing.

```bash
export INSTALL_JOB="crossfeed-install-$(date +%Y%m%d-%H%M%S)-$$"
TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 "$HOME/.claude/scripts/longrun.sh" start "$INSTALL_JOB" -- \
  /bin/bash -c 'cd "$1" && exec caffeinate -dimsu bash tests/run-all.sh' _ "$STAGE_ROOT"
"$HOME/.claude/scripts/longrun.sh" wait "$INSTALL_JOB"
```

With dispatches still paused, verify source parity and absence of live drift, then retain the old tree and replace it. These are two directory renames with a short path gap, so there must be no active worker or installer. The old tree remains the rollback copy. The manifest intentionally excludes the private skill text from public-source parity.

```bash
python3 - <<'PY'
import hashlib, json, os, pathlib
live, stage = (pathlib.Path(os.environ[k]) for k in ('LIVE_ROOT', 'STAGE_ROOT'))
state = pathlib.Path(os.environ['INSTALL_STATE_ROOT']).resolve()
backup = pathlib.Path(os.environ['BACKUP_ROOT'])
assert stage.resolve().parent == state and backup.resolve().parent == state, 'Install artifacts must be in install state'
assert not state.is_relative_to(live.parent.resolve()), 'Install state must be outside skills'
assert live.stat().st_dev == stage.stat().st_dev == state.stat().st_dev, 'Renames require one filesystem'
manifest = json.loads((stage / 'INSTALL-SOURCE.json').read_text())
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
def inventory(root):
    return {str(p.relative_to(root)): {'link': os.readlink(p)} if p.is_symlink() else
            {'sha256': digest(p)} if p.is_file() else {'directory': True}
            for p in root.rglob('*')}
assert inventory(live) == manifest['live_tree'], 'Live tree changed since staging'
staged = inventory(stage)
for name, expected in manifest['live_tree'].items():
    if name not in manifest['files'] and name not in {*manifest['private'], 'INSTALL-SOURCE.json'}:
        assert staged.get(name) == expected, 'Preserved operator file changed: ' + name
for name, expected in manifest['files'].items():
    assert digest(stage / name) == expected, 'Staged source drift: ' + name
for name, expected in manifest['accepted_private'].items():
    assert digest(stage / name) == expected, 'Reviewed private policy drift: ' + name
for name, expected in dict(manifest['before'], **manifest['private']).items():
    assert digest(live / name) == expected, 'Live changed since staging: ' + name
assert not backup.exists()
live.rename(backup)
try:
    stage.rename(live)
except BaseException:
    backup.rename(live)
    raise
for name, expected in manifest['files'].items():
    assert digest(live / name) == expected, 'Installed source drift: ' + name
for name, expected in manifest['accepted_private'].items():
    assert digest(live / name) == expected, 'Installed private policy drift: ' + name
print('Installed revision', manifest['revision'], 'rollback:', backup)
PY
```

Read back the installed private policy, references, readers and any harness links. Record the installed revision, staged suite result, source-hash readback and retained backup in the orchestrator context home, then resume dispatches. On failed acceptance, pause dispatches again and restore the named backup with the same environment:

```bash
python3 - <<'PY'
import os, pathlib, uuid
live, backup, state = (pathlib.Path(os.environ[k]) for k in ('LIVE_ROOT', 'BACKUP_ROOT', 'INSTALL_STATE_ROOT'))
assert backup.is_dir() and not backup.is_symlink()
assert backup.resolve().parent == state.resolve() and not state.resolve().is_relative_to(live.parent.resolve())
aside = state / ('orchestrator.failed-' + uuid.uuid4().hex)
live.rename(aside)
try:
    backup.rename(live)
except BaseException:
    aside.rename(live)
    raise
print('Restored', live, 'retained failed tree:', aside)
PY
```

Do not delete either tree during this procedure. Rebuilding evidence or admitting newly verified matching sources is a separate step through the existing market refresh and overlay workflow, not an installer side effect.
