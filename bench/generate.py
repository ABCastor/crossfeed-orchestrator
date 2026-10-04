#!/usr/bin/env python3
"""Generate seeded, offline benchmark fixtures and executable references."""
import argparse
import json
from importlib import import_module
import random
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAMILIES = ('fix', 'implement', 'review', 'repo-qa', 'reasoning', 'extraction')
DIFFICULTIES = ('easy', 'medium', 'hard', 'expert')


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def put(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')


def generate(seed, out, per_family=8):
    if type(seed) is not int or type(per_family) is not int or per_family < 1:
        raise ValueError('seed must be an integer and per_family must be positive')
    out = Path(out)
    if out.is_symlink():
        raise ValueError('output must not be a symlink')
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for family in FAMILIES:
        module = import_module('bench.families.' + family.replace('-', '_'))
        for index in range(per_family):
            difficulty = DIFFICULTIES[index % len(DIFFICULTIES)]
            template_index = (index // len(DIFFICULTIES) + index % len(DIFFICULTIES)) % len(module.TEMPLATE_IDS)
            # Task-local streams preserve existing fixtures when counts grow.
            rng = random.Random('%d:%s:%d' % (seed, family, index))
            fixture = module.make(rng, template_index, difficulty)
            prompt, workspace, check, reference, reply = fixture
            task = out / ('%s-%d' % (family,index+1))
            if task.exists():
                raise ValueError('task already exists: %s; use a fresh output directory' % task)
            task.mkdir()
            (task/'PROMPT.md').write_text(prompt,encoding='utf-8')
            dump(task/'meta.json', {'family':family,'difficulty':difficulty,'tier':difficulty,
                                  'template_id':module.TEMPLATE_IDS[template_index],'seed':seed})
            dump(task/'check.json',check)
            (task/'workspace').mkdir()
            (task/'reference'/'workspace').mkdir(parents=True)
            put(task/'workspace',workspace)
            put(task/'reference'/'workspace',reference)
            (task/'reference'/'reply.txt').write_text(reply,encoding='utf-8')
            paths.append(task)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed',type=int,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--per-family',type=int,default=8)
    args = parser.parse_args()
    try:
        tasks = generate(args.seed,args.out,args.per_family)
    except (ValueError,OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({'tasks':len(tasks),'seed':args.seed,'out':str(args.out)}))


if __name__ == '__main__':
    main()
