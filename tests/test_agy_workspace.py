"""Read-only execution contract against a fake Antigravity executable."""
from pathlib import Path
import subprocess
import sys

from tests.test_switch_respected import Sandbox, SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import fleetctl
import selector


class ReadOnlyAgyTests(Sandbox):
    def setUp(self):
        super().setUp()
        lane = next(lane for lane in self.roster['lanes'] if lane['harness'] == 'agy'
                    and lane['quota_pool'] == 'antigravity-gemini'
                    and lane['admission_status'] == 'active')
        lane.update(agy_read_only_copy=True, data_policy='code-ok')
        self.lane = lane['lane_id']
        self.roster['policy']['selector'] = {'allowed_pools_by_role': {'default': ['antigravity-gemini']}}
        self.write_roster(self.roster)
        self.work = self.root / 'repo with spaces'
        self.work.mkdir()
        self.tracked = self.work / 'canary.txt'
        self.untracked = self.work / 'draft.txt'
        self.tracked.write_text('canary 1\n')
        self.untracked.write_text('untracked 1\n')
        (self.work / '.gitignore').write_text('ignored.txt\n')
        (self.work / 'ignored.txt').write_text('not part of this workspace\n')
        subprocess.run(['git', 'init', '-q', str(self.work)], env=self.env, check=True)
        subprocess.run(['git', '-C', str(self.work), 'add', '--', 'canary.txt', '.gitignore'],
                       env=self.env, check=True)
        self.last = self.root / 'answer.txt'
        self.args = self.fake_cli('agy', '''
set -eu
[ "$(cat canary.txt)" = 'canary 1' ]
[ "$(cat draft.txt)" = 'untracked 1' ]
[ ! -e .git ] && [ ! -e ignored.txt ]
pwd -P > "$FAKE_CWD"
printf 'canary 2\\n' > canary.txt
if [ -n "${FAKE_CHANGE:-}" ]; then printf 'external change\\n' > "$FAKE_CHANGE"; fi
printf 'fake result\\n'
''')
        self.cwd = self.root / 'agy.cwd'
        self.env.update(FAKE_CWD=str(self.cwd), FLEET_DATA_CLASS='code')

    def run_case(self):
        options, _ = selector.enumerate_options(self.roster, {}, 'review', mode='read-only',
                                                data_class='code', fleet=fleetctl)
        option = next(row for row in options if row['lane_id'] == self.lane)
        option['command_argv'] = selector._command(option, 'review', self.root / 'not-set.json', fleet=fleetctl)
        argv = fleetctl.dispatch_argv(option, 'Read project code', self.work, self.last)
        return subprocess.run(argv, env=self.env, cwd=self.root, text=True, capture_output=True, timeout=30)

    def assert_copy_was_granted_and_removed(self):
        argv = self.args.read_text().splitlines()
        cwd = Path(self.cwd.read_text().strip())
        self.assertIn('--sandbox', argv)
        self.assertEqual(argv.count('--add-dir'), 1)
        self.assertEqual(Path(argv[argv.index('--add-dir') + 1]).resolve(), cwd)
        self.assertNotEqual(cwd, self.work.resolve())
        self.assertFalse(cwd.parent.exists())

    def test_fake_writes_only_to_snapshot(self):
        # BR-AGY-001 — confirm a real cwd write takes place without changing the original.
        result = self.run_case()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'fake result\n')
        self.assertEqual(self.last.read_text(), result.stdout)
        self.assertEqual(self.tracked.read_text(), 'canary 1\n')
        self.assertEqual(self.untracked.read_text(), 'untracked 1\n')
        self.assert_copy_was_granted_and_removed()

    def test_original_change_discards_answer(self):
        # BR-AGY-001 — control: each type of persistent original change is detected.
        addition = self.work / 'new.txt'
        for path in (self.tracked, self.untracked, addition):
            with self.subTest(path=path.name):
                before = path.read_text() if path.exists() else None
                self.env['FAKE_CHANGE'] = str(path)
                result = self.run_case()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('original workspace changed during read-only run', result.stderr)
                self.assertEqual(path.read_text(), 'external change\n')
                self.assertEqual(result.stdout, '')
                self.assertFalse(self.last.exists())
                self.assert_copy_was_granted_and_removed()
                if before is not None:
                    path.write_text(before)

    def test_external_symlink_is_refused_before_launch(self):
        # BR-AGY-001 — a link out of the snapshot cannot write through to the source.
        (self.work / 'escape').symlink_to('../outside')
        result = self.run_case()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symlink leaves the snapshot', result.stderr)
        self.assertFalse(self.args.exists())

    def test_answer_destination_cannot_write_to_original(self):
        # BR-AGY-001 — the wrapper itself cannot publish text into the original.
        self.last = self.tracked
        result = self.run_case()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('output files must be outside the original worktree', result.stderr)
        self.assertFalse(self.args.exists())
        self.assertEqual(self.tracked.read_text(), 'canary 1\n')
