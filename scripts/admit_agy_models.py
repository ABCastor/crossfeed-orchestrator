#!/usr/bin/env python3
"""Prepare the AGY r11 roster migration; never writes the source overlay.

The output is a unified patch for review and application with patch(1).
"""
import argparse
import copy
import difflib
import json
from pathlib import Path

LEVELS = ['low', 'medium', 'high']
MODELS = {'gemini-4-argon': ('antigravity-argon', 'google'),
          'claude-opus-5-5': ('antigravity-3p', 'anthropic'),
          'claude-sonnet-5-5': ('antigravity-3p', 'anthropic')}
SUPERSEDED = {'gemini-3.1-pro': 'gemini-4-argon', 'gemini-3.6-flash': 'gemini-3.8-flash',
              'gemini-3.7-flash': 'gemini-3.8-flash',
              'claude-opus-4-6-thinking': 'claude-opus-5-5', 'claude-sonnet-4-6': 'claude-sonnet-5-5'}


def migrate(source, *, example=False):
    roster = copy.deepcopy(source)
    lanes = roster['lanes']
    templates = {pool: next(l for l in lanes if l.get('harness') == 'agy' and l['quota_pool'] == pool)
                 for pool in ('antigravity-gemini', 'antigravity-3p')}
    if 'antigravity-argon' not in roster['quota_pools']:
        try:
            from .agy_quota_pools import migrate as split_pools
        except ImportError:
            from agy_quota_pools import migrate as split_pools
        roster = split_pools(roster, seed_refusals=False)
        lanes = roster['lanes']
    templates['antigravity-argon'] = templates['antigravity-gemini']
    cards = roster.setdefault('model_cards', {})
    for lane in lanes:
        if lane.get('harness') == 'agy' and lane['model_key'] in SUPERSEDED:
            lane.update(admission_status='retired', roles=[])
            cards.setdefault(lane['model_key'], {}).update(
                status='older', superseded_by=SUPERSEDED[lane['model_key']], superseded_on='2026-10-16')
    for model, (pool, lineage) in MODELS.items():
        card = cards.setdefault(model, {})
        card.update(name={'gemini-4-argon': 'Gemini 4 Argon', 'claude-opus-5-5': 'Opus 5.5',
                          'claude-sonnet-5-5': 'Sonnet 5.5'}[model], status='current', lineage=lineage)
        # Do not replace native Claude controls, refusals, defaults or measured evidence.
        effort = roster.setdefault('effort', {}).setdefault(model, {})
        effort.setdefault('levels', {})['agy'] = LEVELS.copy()
        effort.setdefault('harness_overrides', {})['agy'] = {
            'default': 'high', 'knee': 'high', 'by_role': {},
            'by_band': {'critical': {r: 'medium' for r in ('design', 'visual', 'frontend-visual', 'writing')}},
            'evidence': {'status': 'unmeasured', 'read_on': '2026-10-16',
                         'source': 'agy 1.4.0 models, caller-verified catalogue'}}
        if not effort.get('default'):
            effort.update(default='high', knee='high', by_role={},
                          evidence={'status': 'unmeasured', 'read_on': '2026-10-16',
                                    'source': 'agy 1.4.0 models, caller-verified catalogue',
                                    'note': 'High is provisional, not a measured knee.'},
                          recheck='New AGY catalogue, transport failure, or local fit trials.')
        for level in LEVELS:
            lane_id = 'antigravity-' + model + '-' + level
            if any(l['lane_id'] == lane_id for l in lanes):
                continue
            lane = copy.deepcopy(templates[pool])
            lane.update(lane_id=lane_id, model_key=model, selector=model+'-'+level,
                        quota_pool=pool, access_status='verified', admission_status='active',
                        allowed_modes=['read-only', 'write'], roles=['design', 'visual', 'frontend-visual', 'writing']
                        if model == 'gemini-4-argon' else ['review', 'hard-reasoning'],
                        quality_tier='frontier', max_parallel=1,
                        verified_at=None if example else '2026-10-16',
                        evidence_confidence='example, not deployment evidence' if example else 'provisional',
                        notes='AGY 1.4.0 catalogue controls. Caller canaries: Argon high and Opus high returned OK; other levels and Sonnet are catalogue-only. No long-write or quality trial claimed.')
            lanes.append(lane)
    # Keep native Claude defaults independent of AGY's narrower selector controls.
    selector = roster.setdefault('policy', {}).setdefault('selector', {})
    selector.setdefault('mode_by_role', {}).update({r: 'write' for r in ('design', 'visual', 'frontend-visual', 'writing')})
    selector.setdefault('families_by_role', {}).update({'design': 'visual', 'visual': 'visual', 'writing': 'writing'})
    roles = roster.setdefault('routing', {}).setdefault('roles', {})
    for role in ('design', 'visual', 'frontend-visual', 'writing'):
        rule = roles.setdefault(role, {})
        for band in ('quality_first', 'conserve', 'critical'):
            lane_id = 'antigravity-gemini-4-argon-' + ('medium' if band == 'critical' else 'high')
            rule[band] = [lane_id] + [x for x in rule.get(band, []) if not x.startswith('antigravity-gemini-4-argon-')]
    for rule in roles.values():
        for band in ('quality_first', 'conserve', 'critical'):
            rule[band] = [{'antigravity-3p-opus': 'antigravity-claude-opus-5-5-high',
                           'antigravity-3p-sonnet': 'antigravity-claude-sonnet-5-5-high'}.get(x,x)
                          for x in rule.get(band, [])]
    for profile in roster.get('swarm_profiles', {}).values():
        for band in profile.get('bands', {}).values():
            for worker in band.get('workers', []):
                if worker.get('lane_id') == 'antigravity-3p-opus':
                    worker['lane_id'] = 'antigravity-claude-opus-5-5-high'
                elif worker.get('lane_id') == 'antigravity-3p-sonnet':
                    worker['lane_id'] = 'antigravity-claude-sonnet-5-5-high'
    return roster


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overlay', type=Path, required=True)
    parser.add_argument('--patch', type=Path, required=True)
    args = parser.parse_args()
    if args.overlay.resolve() == args.patch.resolve():
        parser.error('--patch must differ from --overlay')
    before = args.overlay.read_text()
    after = json.dumps(migrate(json.loads(before)), indent=2, ensure_ascii=False) + '\n'
    args.patch.write_text(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                           fromfile='access-overlay.json', tofile='access-overlay.json')))
    print(args.patch)


if __name__ == '__main__':
    main()
