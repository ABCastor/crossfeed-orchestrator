"""Execute the documented installer against isolated, provider-free trees."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BLOCKS = re.findall(r"python3 - <<'PY'\n(.*?)\nPY", (ROOT / 'docs/SKILL-INSTALL.md').read_text(), re.S)


class InstallDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='orch-install-', dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.live = self.root / 'skills/orchestrator'
        self.state = self.root / 'state/orchestrator-install'
        self.stage = self.state / 'orchestrator.stage.test'
        self.backup = self.state / 'orchestrator.before-test'
        for folder in (self.source, self.live, self.state):
            folder.mkdir(parents=True)
        for name, text in {'docs/SELECTOR.md': 'Reviewed managed documentation\n',
                           'skill/references/test.md': 'Reference\n',
                           'skill/readers/test.md': 'Reader\n',
                           'bench/to_evidence.py': 'print("fixture")\n'}.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        def git(*args):
            subprocess.run(['git', '-C', str(self.source), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        git('init', '-q')
        git('add', 'docs', 'skill', 'bench')
        git('-c', 'user.name=Anneo22', '-c', 'user.email=159916923+Anneo22@users.noreply.github.com',
            'commit', '-qm', 'Installer fixture\n\nAssisted-by: Codex')
        (self.live / 'SKILL.md').write_text('Private skill\n')
        (self.live / 'LIVE-RULES.md').write_text('Private rules\n')
        (self.live / 'operator.txt').write_text('Preserved local file\n')
        (self.live / 'docs').mkdir()
        (self.live / 'docs/SELECTOR.md').write_text('Old reviewed code\n')
        shutil.copytree(self.live, self.stage)
        self.approved = self.state / 'approved.json'
        self.approved.write_text(json.dumps(['docs/SELECTOR.md']))
        self.env = patch.dict(os.environ, SOURCE_ROOT=str(self.source), LIVE_ROOT=str(self.live),
                              STAGE_ROOT=str(self.stage), BACKUP_ROOT=str(self.backup),
                              INSTALL_STATE_ROOT=str(self.state), APPROVED_REPLACEMENTS=str(self.approved))
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_block(self, index):
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(BLOCKS[index], 'SKILL-INSTALL.md', 'exec'), {})

    def test_json_file_approval_copy_swap_and_rollback(self):
        original = {str(p.relative_to(self.live)): p.read_bytes() for p in self.live.rglob('*') if p.is_file()}
        self.run_block(0)
        self.assertEqual((self.stage / 'docs/SELECTOR.md').read_text(), 'Reviewed managed documentation\n')
        self.assertTrue((self.stage / 'references/test.md').is_file())
        self.assertTrue((self.stage / 'readers/test.md').is_file())
        (self.stage / 'SKILL.md').write_text('Reviewed private skill\n')
        self.run_block(1)
        self.run_block(2)
        self.assertTrue(self.backup.is_dir())
        self.assertEqual(self.backup.parent, self.state)
        self.assertEqual((self.live / 'operator.txt').read_bytes(), original['operator.txt'])
        self.run_block(3)
        restored = {str(p.relative_to(self.live)): p.read_bytes() for p in self.live.rglob('*') if p.is_file()}
        self.assertEqual(restored, original)
        self.assertEqual(len(list(self.state.glob('orchestrator.failed-*'))), 1)
        self.assertEqual([p.name for p in self.live.parent.iterdir()], ['orchestrator'])

    def test_inline_json_and_unreviewed_collisions_refuse_staging(self):
        with patch.dict(os.environ, APPROVED_REPLACEMENTS='["docs/SELECTOR.md"]'):
            with self.assertRaises(FileNotFoundError):
                self.run_block(0)
        with patch.dict(os.environ, APPROVED_REPLACEMENTS=''):
            with self.assertRaisesRegex(AssertionError, 'Unknown operator collision'):
                self.run_block(0)

    def test_stage_and_backup_cannot_register_as_duplicate_skills(self):
        misplaced = self.live.parent / 'orchestrator.stage.test'
        shutil.copytree(self.live, misplaced)
        with patch.dict(os.environ, STAGE_ROOT=str(misplaced), INSTALL_STATE_ROOT=str(self.live.parent)):
            with self.assertRaisesRegex(AssertionError, 'outside skills'):
                self.run_block(0)
        self.run_block(0)
        self.run_block(1)
        with patch.dict(os.environ, BACKUP_ROOT=str(self.live.parent / 'orchestrator.before-test')):
            with self.assertRaisesRegex(AssertionError, 'in install state'):
                self.run_block(2)
        self.assertTrue(self.live.is_dir())

    def test_live_and_staged_drift_stop_replacement(self):
        self.run_block(0)
        self.run_block(1)
        for path, message in [(self.live / 'operator.txt', 'Live tree changed'),
                              (self.stage / 'docs/SELECTOR.md', 'Staged source drift'),
                              (self.stage / 'SKILL.md', 'Reviewed private policy drift')]:
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_text('Unreviewed drift\n')
                with self.assertRaisesRegex(AssertionError, message):
                    self.run_block(2)
                path.write_bytes(original)
                self.assertFalse(self.backup.exists())


if __name__ == '__main__':
    unittest.main()
