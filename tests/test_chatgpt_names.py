"""The shipped gateway has one identity; upstream attribution stays intact."""
from pathlib import Path
import os
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_PATHS = ('scripts', 'tests', 'docs', 'examples', 'skill', 'README.md',
                 'NOTICE', 'SKILL.md', 'references', 'readers')


def retired_name_failures(root):
    retired = 'pi' + 'link'

    def ignored(path):
        parts = path.relative_to(root).parts
        return (parts[:2] == ('scripts', 'runs') or
                any(part == '__pycache__' or part.startswith('.sabotage-') or
                    part.endswith('.log') for part in parts))

    def walk_error(error):
        raise error

    paths = []
    for name in PRODUCT_PATHS:
        product = root / name
        if product.is_file():
            paths.append(product)
        elif product.is_dir():
            for directory, directories, files in os.walk(product, onerror=walk_error):
                directory = Path(directory)
                directories[:] = [name for name in directories
                                  if not ignored(directory / name)]
                paths.extend(directory / name for name in files
                             if not ignored(directory / name))
    paths.sort()
    failures = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if retired in relative.casefold():
            failures.append(relative + ': retired filename')
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if relative == 'NOTICE':
            continue
        content = path.read_bytes()
        if content.startswith((b'\xff\xfe', b'\xfe\xff')):
            text = content.decode('utf-16', errors='replace')
        elif b'\0' in content:
            # Bundled fonts and other binaries are not text sources.
            continue
        else:
            text = content.decode('utf-8', errors='replace')
        for number, line in enumerate(text.splitlines(), 1):
            if retired not in line.casefold():
                continue
            if relative == 'README.md' and 'credit to upstream contributor:' in line.casefold():
                continue
            failures.append(relative + ':' + str(number))
    return failures


class GatewayNamesTests(unittest.TestCase):
    def test_retired_gateway_name_only_appears_in_attribution(self):
        failures = retired_name_failures(ROOT)
        self.assertEqual(failures, [], '\n'.join(failures))

    def test_nested_hidden_source_names_and_content_are_checked(self):
        retired = 'pi' + 'link'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / 'docs' / '.hidden' / 'nested'
            nested.mkdir(parents=True)
            (nested / 'current.py').write_text('Current identity\n' + retired.upper())
            (nested / 'encoded.txt').write_text(retired, encoding='utf-16')
            (nested / (retired.upper() + '.txt')).write_text('Current identity')
            self.assertCountEqual(retired_name_failures(root), [
                'docs/.hidden/nested/current.py:2',
                'docs/.hidden/nested/encoded.txt:1',
                'docs/.hidden/nested/' + retired.upper() + '.txt: retired filename',
            ])

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
