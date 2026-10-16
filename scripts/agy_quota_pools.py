#!/usr/bin/env python3
"""Prepare and validate an idempotent AGY quota overlay patch; never edit the input."""
import argparse
import copy
import difflib
import json
import os
from pathlib import Path
import subprocess
import tempfile

ARGON_POOL = 'antigravity-argon'
# Caller-verified provider refusals, not percentage estimates.
REFUSALS = {
    ARGON_POOL: {'opened_at': '2026-10-16T12:45:31Z', 'until': '2026-10-23T09:18:00Z',
                 'limit_name': 'individual quota', 'source': 'caller-verified-agy-error'},
    'antigravity-3p': {'opened_at': '2026-10-16T13:03:00Z', 'until': '2026-10-16T14:21:27Z',
                       'limit_name': 'individual quota (5-hour pool)', 'source': 'caller-verified-agy-error'},
}


def migrate(source, *, seed_refusals=True):
    roster = copy.deepcopy(source)
    pools = roster['quota_pools']
    pools.setdefault(ARGON_POOL, {
        'label': 'Antigravity · Argon',
        'plan': {'name': 'Antigravity · Argon individual allowance', 'billing': 'subscription'},
        'quota_refresh': None,
        'quota_source': 'AGY individual quota refusals; the shared Gemini gauge does not measure this allowance.',
    })
    if seed_refusals:
        from datetime import datetime
        for pool, refusal in REFUSALS.items():
            previous = pools[pool].get('quota_error', {})
            if not previous.get('until') or datetime.fromisoformat(previous['until'].replace('Z', '+00:00')) < datetime.fromisoformat(refusal['until'].replace('Z', '+00:00')):
                pools[pool]['quota_error'] = dict(refusal)
    # Carry existing automatic role admission across the split, without adding roles.
    for allowed in roster.get('policy', {}).get('selector', {}).get('allowed_pools_by_role', {}).values():
        if 'antigravity-gemini' in allowed and ARGON_POOL not in allowed:
            allowed.insert(allowed.index('antigravity-gemini') + 1, ARGON_POOL)
    for lane in roster['lanes']:
        if lane.get('harness') == 'agy' and lane.get('model_key') == 'gemini-4-argon':
            lane['quota_pool'] = ARGON_POOL
    return roster


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overlay', type=Path, required=True)
    parser.add_argument('--patch', type=Path, required=True)
    args = parser.parse_args()
    if args.overlay.resolve() == args.patch.resolve():
        parser.error('--patch must differ from --overlay')
    before = args.overlay.read_text()
    result = migrate(json.loads(before))
    after = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    # Resolve both repository and installed skill layouts; validate without remote discovery.
    with tempfile.TemporaryDirectory(prefix='agy-quota-validate-') as directory:
        overlay = Path(directory) / 'overlay.json'
        overlay.write_text(after)
        subprocess.run(['bash', str(Path(__file__).with_name('roster.sh')), 'validate'],
                       env=dict(os.environ, ACCESS_OVERLAY=str(overlay), FLEET_NO_AUTO_REFRESH='1'), check=True)
    args.patch.parent.mkdir(parents=True, exist_ok=True)
    lines = difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                fromfile='access-overlay.json', tofile='access-overlay.json')
    args.patch.write_text(''.join(line if line.endswith('\n') else
                                 line + '\n\\ No newline at end of file\n' for line in lines))
    print(args.patch)


if __name__ == '__main__':
    main()
