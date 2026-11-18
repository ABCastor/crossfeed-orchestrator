#!/usr/bin/env python3
"""Prepare a roster for one signed-in Copilot account. Never write the live roster."""
import argparse
import copy
import datetime
import json
import re
from pathlib import Path


def configure(roster, pool, models, cap, *, funding):
    if funding not in ("included", "metered"):
        raise ValueError("declare included or metered funding")
    if not re.fullmatch(r'github-copilot-[a-z0-9-]+', pool) or pool in roster['quota_pools']:
        raise ValueError('choose a new github-copilot- account pool')
    if cap < 1 or not models or len(set(models)) != len(models):
        raise ValueError('provide distinct verified model IDs and a positive per-run credit cap')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', m) or m.lower() == 'auto' for m in models):
        raise ValueError('invalid named model ID')
    out = copy.deepcopy(roster)
    lanes = out['lanes']
    auto = next(l for l in lanes if l['lane_id'] == 'github-copilot-student-auto')
    if auto['quota_pool'] != 'github-copilot-student':
        raise ValueError('this roster already has a configured Copilot login; review it rather than adding another account')
    if any(l['harness'] == 'copilot' and l['selector'] in models for l in lanes):
        raise ValueError('Copilot model selector already configured')
    out['quota_pools'][pool] = {'label': 'GitHub Copilot (named account)', 'model_selection': 'named',
                               'funding': funding,
                               'quota_source': 'Configure from the signed-in company account; unknown until observed.'}
    allowed = out.get('policy', {}).get('selector', {}).get('allowed_pools_by_role', {})
    review = allowed.get('review', allowed.get('default'))
    if review is not None:
        allowed['review'] = list(dict.fromkeys([*review, pool]))
    auto.update(quota_pool=pool, soft_session_credit_cap=cap)
    if funding == 'metered':
        auto['admission_status'] = 'candidate'  # No bounded Copilot transport exists.
    for model in models:
        key = f'{pool}-{model}'
        if key in out['effort'] or any(l['lane_id'] == key for l in lanes):
            raise ValueError('model key or lane ID collision')
        lanes.append({**auto, 'lane_id': key, 'model_key': key, 'selector': model,
                      'quota_pool': pool, 'allowed_modes': ['read-only'], 'roles': ['review'],
                      'soft_session_credit_cap': cap, 'access_status': 'verified',
                      'admission_status': 'active' if funding == 'included' else 'candidate',
                      'verified_at': datetime.date.today().isoformat(),
                      'notes': 'Named model verified by operator using the official Copilot CLI on this account.',
                      'evidence_confidence': 'operator-attested access; performance unmeasured'})
        out['effort'][key] = {'levels': {'copilot': []}, 'default': 'service-chosen', 'by_role': {},
                              'evidence': {'status': 'unmeasured', 'source': 'No effort flag passed.'},
                              'recheck': 'Recheck after a login, plan, model or CLI change.'}
    return out


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--roster', type=Path, required=True)
    p.add_argument('--pool', required=True)
    p.add_argument('--verified-model', action='append', required=True, help='exact ID already tested on this login')
    p.add_argument('--max-credits', type=int, required=True, help='account-approved hard cap per call')
    p.add_argument('--funding', required=True, choices=('included', 'metered'),
                   help='verified billing on this login; metered remains a candidate')
    args = p.parse_args()
    try:
        result = configure(json.loads(args.roster.read_text()), args.pool, args.verified_model, args.max_credits, funding=args.funding)
    except (KeyError, ValueError, StopIteration) as exc:
        p.error(f'cannot configure account: {exc}')
    print(json.dumps(result, indent=2))
