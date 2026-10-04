#!/usr/bin/env python3
"""Generate fresh seeded fixtures and verify references, nulls and coverage offline."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench.check import check_task
from bench.generate import DIFFICULTIES, FAMILIES, generate


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--per-family', type=int, default=32)
    args = parser.parse_args(argv)
    root = args.out
    if root.exists() and any(root.iterdir()):
        parser.error('verification output directory must be empty')
    root.mkdir(parents=True, exist_ok=True)
    empty = root / 'empty.txt'
    empty.write_text('', encoding='utf-8')
    totals = Counter()
    public_digests = {}
    for seed in (1, 2):
        tasks = generate(seed, root / ('seed%d' % seed), args.per_family)
        counts = Counter()
        coverage = {family: Counter() for family in FAMILIES}
        for task in tasks:
            meta = json.loads((task / 'meta.json').read_text())
            coverage[meta['family']][(meta['template_id'], meta['tier'])] += 1
            public = snapshot(task / 'workspace')
            public['PROMPT.md'] = hashlib.sha256((task / 'PROMPT.md').read_bytes()).hexdigest()
            if seed == 2 and public_digests[task.name] == public:
                raise RuntimeError('worker inputs did not vary across seeds: ' + task.name)
            public_digests[task.name] = public
            for reply, wanted, label in ((task / 'reference/reply.txt', True, 'reference_passes'),
                                         (empty, False, 'empty_failures')):
                result = check_task(task, task / 'reference/workspace', reply)
                if result['pass'] is not wanted:
                    raise RuntimeError('%s seed %d: %s' % (task.name, seed, result))
                counts[label] += 1
        for family, cells in coverage.items():
            tiers = Counter()
            for (_, tier), count in cells.items():
                tiers[tier] += count
            if max(tiers.get(t, 0) for t in DIFFICULTIES) - min(tiers.get(t, 0) for t in DIFFICULTIES) > 1:
                raise RuntimeError('unbalanced tiers: ' + family)
            if args.per_family >= 16 and (len(cells) < 16 or min(cells.values()) < 1):
                raise RuntimeError('missing template/tier combinations: ' + family)
        duplicate = root / ('repeat%d' % seed)
        environment = dict(os.environ, PYTHONHASHSEED=str(seed + 100))
        repeated = subprocess.run([sys.executable, str(Path(__file__).with_name('generate.py')),
                                   '--seed', str(seed), '--out', str(duplicate),
                                   '--per-family', str(args.per_family)],
                                  env=environment, capture_output=True, text=True, timeout=300)
        if repeated.returncode:
            raise RuntimeError('generation subprocess failed: ' + repeated.stderr)
        if snapshot(root / ('seed%d' % seed)) != snapshot(duplicate):
            raise RuntimeError('generation is not deterministic for seed %d' % seed)
        totals.update(counts)
        print(json.dumps(dict(seed=seed, tasks=len(tasks), deterministic=True, **counts)), flush=True)
    summary = dict(tasks=sum(totals.values()) // 2, **totals)
    (root / 'verification.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
