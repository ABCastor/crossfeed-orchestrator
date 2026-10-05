#!/usr/bin/env python3
"""Reject personal home paths and email addresses in distributable text."""
from pathlib import Path
import re
import subprocess
import sys

HOME = re.compile('/' + r'(?:Users|home)/[A-Za-z0-9_][A-Za-z0-9_.-]*(?=/|\b)')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')
# Reserved example domains are intentionally used by credential fixtures.
EXAMPLES = {'example.com', 'example.org', 'example.net'}


def main():
    files = sys.argv[1:] or subprocess.check_output([
        'git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z',
    ]).decode().rstrip('\0').split('\0')
    failures = []
    for name in sorted(set(files)):
        file = Path(name)
        if not file.is_file():
            continue
        data = file.read_bytes()
        if file.name.startswith("OFL-") and file.suffix == ".txt" and b"SIL OPEN FONT LICENSE" in data:
            continue  # Required public font attribution, not operator data.
        try:
            source = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if '\0' in source:
            continue
        for number, line in enumerate(source.splitlines(), 1):
            if HOME.search(line):
                failures.append(f'{name}:{number}: absolute personal home path')
            for match in EMAIL.finditer(line):
                domain = match.group(1).lower()
                if domain not in EXAMPLES and not domain.endswith(('.test', '.invalid', '.example')):
                    failures.append(f'{name}:{number}: non-placeholder email address')
    if failures:
        print('\n'.join(failures))  # Locations only, never the matching values.
        return 1
    print('PASS source privacy: no personal home paths or non-placeholder emails')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
