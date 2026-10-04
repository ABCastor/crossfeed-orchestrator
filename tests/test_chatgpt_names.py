"""The shipped gateway has one identity; upstream attribution stays intact."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_PATHS = ('scripts', 'tests', 'docs', 'examples', 'skill', 'README.md',
                 'NOTICE', 'SKILL.md', 'references', 'readers')


def retired_name_failures(root):
    retired = 'pi' + 'link'
    paths = [path for path in PRODUCT_PATHS if (root / path).exists()]
    if not paths:
        return []
    options = ['--hidden', '-g', '!scripts/runs/**', '-g', '!**/__pycache__/**',
               '-g', '!.sabotage-*', '-g', '!*.log']
    listed = subprocess.run(['rg', '--files', *options, '--', *paths],
                            cwd=root, text=True, capture_output=True)
    if listed.returncode not in (0, 1):
        raise RuntimeError(listed.stderr)
    failures = []
    for relative in listed.stdout.splitlines():
        if retired in relative.casefold():
            failures.append(relative + ': retired filename')
    result = subprocess.run(['rg', '-n', '-i', *options, '--', retired, *paths],
                            cwd=root, text=True, capture_output=True)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr)
    for line in result.stdout.splitlines():
        path, number, content = line.split(':', 2)
        if path == 'NOTICE':
            continue
        if path == 'README.md' and 'credit to upstream contributor:' in content.casefold():
            continue
        failures.append(path + ':' + number)
    return failures


class GatewayNamesTests(unittest.TestCase):
    def test_retired_gateway_name_only_appears_in_attribution(self):
        failures = retired_name_failures(ROOT)
        self.assertEqual(failures, [], '\n'.join(failures))

    def test_runtime_files_are_ignored_in_both_layouts(self):
        retired = 'pi' + 'link'
        for layout in ('repo', 'live'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = ['scripts/runs/' + retired + '.txt',
                         'scripts/__pycache__/' + retired + '.pyc',
                         'tests/.sabotage-' + retired + '/source.py',
                         'docs/' + retired + '.log',
                         'backups/' + retired + '.txt',
                         '.scratch/' + retired + '.txt', 'local.txt']
                for path in paths:
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(retired)
                (root / 'NOTICE').write_text(retired)
                (root / 'README.md').write_text('Credit to upstream contributor: ' + retired)
                product_dirs = ['scripts', 'tests', 'docs', 'examples']
                product_dirs += ['skill'] if layout == 'repo' else ['references', 'readers']
                for path in product_dirs:
                    (root / path).mkdir(exist_ok=True)
                (root / 'SKILL.md').write_text('Current gateway identity')
                self.assertEqual(retired_name_failures(root), [])
                for path in [*product_dirs, 'SKILL.md', 'README.md']:
                    with self.subTest(product_path=path):
                        target = root / path
                        if target.is_dir():
                            target = target / (retired + '.txt')
                        original = target.read_text() if target.exists() else None
                        target.write_text(retired)
                        failures = retired_name_failures(root)
                        self.assertIn(str(target.relative_to(root)) + ':1', failures)
                        if target.suffix == '.txt':
                            self.assertIn(str(target.relative_to(root)) + ': retired filename',
                                          failures)
                        target.write_text(original if original is not None else 'Current identity')
                        # A retired filename remains a violation even after its content changes.
                        if original is None:
                            target.rename(target.with_name('current.txt'))


if __name__ == '__main__':
    unittest.main()
