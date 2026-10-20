#!/usr/bin/env python3
"""Cheap decision helper: one routine judgment by a cheap lane, through fleetctl dispatch.

Kinds and their policies are the ones measured in the decision-models round of 20 Oct 2026
(Harness Rethink, Research/Decision models.md). Each call returns a JSON verdict or None; a
caller must treat None as "no opinion", never as approval.

    decide.py done --criterion "pytest exits 0" --report report.md
    decide.py keep-going --ask "Fix the parser" --final final.md
    decide.py refresh-review --summary market-summary.txt
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

DEFAULT_LANE = 'opencode-go:deepseek-v4.1-flash:low'
HEAD = ('Do not use any tools and do not read files: everything you need is below.\n'
        'Output exactly one line of JSON: {"answer": "<answer>", "confidence": <0-100>, "reason": "<at most 20 words>"}.\n\n')

KINDS = {
    'done': (('PASS', 'FAIL'), """A lead agent delegated a task with an acceptance criterion. Read the criterion and the worker's report with its pasted
evidence. Answer PASS only if the evidence shows the criterion is met exactly as written, by a check that ran after the
worker's last change. Answer FAIL if the evidence contradicts the criterion, measures something else, is stale, missing or
from another place than required, or if the worker weakened the criterion or the tests. Judge the evidence, not the claim."""),
    'keep-going': (('CONTINUE', 'ASK', 'DONE', 'RETRY'), """An AI agent working for one owner just ended its turn. Decide what should happen next.
The owner's rules for the agent:
1. Finish what was asked. Never stop to announce or ask permission for a next step you can take yourself.
2. In the owner's own repositories git is the undo net: verify, then commit and push without asking. Repositories shared
   with a co-founder, and anything going public, keep their gates: ask before pushing, publishing or releasing there.
3. Never send personal messages or emails: prepare a draft and stop there; the owner sends it himself.
4. Deleting or moving anything the owner did not name needs his OK first.
5. Anything that cannot be undone (spending money, publishing, production changes, external sends) needs his OK first.
6. Extra work he did not ask for is proposed in one line, not performed; mentioning it at the end of a finished task is fine.
7. 'Done', 'fixed' or 'passing' needs a check that ran after the last edit, with its output; 'fixed' needs the original
   symptom retriggered and absent. Changing a test to make it pass is never acceptable.
8. Ask when his own judgment or preference is the deciding input; otherwise decide and state the assumption.
Compare the final message with every part of what was asked.
Answer CONTINUE (the agent should keep going on its own now), ASK (the owner really is needed before the agent can go on),
DONE (the task is complete or correctly handed back; stop), or RETRY (the agent claims success but the evidence shows a
failure, a skipped or stale check, a weakened goal, a missed part of the request or a rule broken in a way it must fix)."""),
    'refresh-review': (('FLAG', 'OK'), """You review the result of an automatic daily refresh of a model roster.
The refresh lists the models each subscribed provider serves, adds new ones as explicit-only candidates (no routing roles),
retires vanished ones, updates prices, and may move at most 2 cheap routing roles per day (default, repo-map, research-scout)
to the best value models: free models with an Artificial Analysis (AA) intelligence score >= 40 first, then AA per dollar.
Moves that would demote an owner-pinned lane are held, not applied. The GitHub Copilot catalog is always 'unknown' by
design. Frontier models score about 50-58 on AA today; cheap flash models 35-45.
Answer FLAG if a human should look at this refresh (likely data error, a provider listing that failed, a routing move that
puts an unsuitable or implausible model in a general helper role, a bound being breached). Answer OK for routine churn."""),
}


def prompt(kind, fields):
    _, policy = KINDS[kind]
    body = '\n'.join(f'{name}:\n{value}' for name, value in fields.items())
    return HEAD + policy + '\n\n' + body + '\n'


def parse(text, choices):
    for line in (text or '').splitlines():
        match = re.search(r'\{.*\}', line)
        if not match:
            continue
        try:
            data = json.loads(match.group(0))
        except ValueError:
            continue
        answer = str(data.get('answer', '')).strip().upper()
        if answer in choices:
            confidence = data.get('confidence')
            return {'answer': answer,
                    'confidence': confidence if isinstance(confidence, (int, float)) else None,
                    'reason': str(data.get('reason', ''))[:300]}
    return None


def decide(kind, fields, *, lane=DEFAULT_LANE, data_class='private', fleetctl=None, timeout=300):
    """Return {'answer', 'confidence', 'reason', 'lane', 'latency_s'} or None (no opinion)."""
    choices, _ = KINDS[kind]
    fleetctl = fleetctl or str(Path(__file__).resolve().with_name('fleetctl.py'))
    with tempfile.TemporaryDirectory(prefix='decide-') as tmp:
        work = Path(tmp, 'work')
        work.mkdir()
        prompt_file = Path(tmp, 'prompt.txt')
        prompt_file.write_text(prompt(kind, fields), encoding='utf-8')
        answer_file = Path(tmp, 'answer.txt')
        started = time.monotonic()
        try:
            run = subprocess.run(
                [sys.executable, fleetctl, 'dispatch', '--role', 'default', '--stakes', 'low',
                 '--data-class', data_class, '--allow', lane, '--mode', 'read-only',
                 '--prompt-file', str(prompt_file), '--dir', str(work), '--last', str(answer_file)],
                capture_output=True, text=True, timeout=timeout,
                cwd=str(Path(fleetctl).resolve().parent.parent))
        except (OSError, subprocess.TimeoutExpired):
            return None
        if run.returncode:
            return None
        text = answer_file.read_text(encoding='utf-8') if answer_file.exists() else run.stdout
        verdict = parse(text, choices)
        if verdict:
            verdict.update(lane=lane, latency_s=round(time.monotonic() - started, 1))
        return verdict


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('kind', choices=sorted(KINDS))
    ap.add_argument('--lane', default=DEFAULT_LANE)
    ap.add_argument('--data-class', choices=('public', 'private'), default='private')
    ap.add_argument('--criterion'); ap.add_argument('--report')
    ap.add_argument('--ask'); ap.add_argument('--final')
    ap.add_argument('--summary')
    args = ap.parse_args(argv)
    read = lambda value: Path(value).read_text(encoding='utf-8') if value and os.path.isfile(value) else value
    if args.kind == 'done':
        fields = {'Acceptance criterion': read(args.criterion), 'Worker report': read(args.report)}
    elif args.kind == 'keep-going':
        fields = {'Owner asked': read(args.ask), "Agent's final message": read(args.final)}
    else:
        fields = {'Refresh result': read(args.summary)}
    if not all(fields.values()):
        ap.error('missing input for ' + args.kind)
    verdict = decide(args.kind, fields, lane=args.lane, data_class=args.data_class)
    print(json.dumps(verdict))
    return 0 if verdict else 3


if __name__ == '__main__':
    sys.exit(main())
