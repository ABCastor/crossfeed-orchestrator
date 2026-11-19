"""Version the scheduled market refresh and its review queue (host-local only)."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path

try:
    import evidence
    import fleetctl as fleet
    import funding
    import selector
except ModuleNotFoundError:
    from scripts import evidence, fleetctl as fleet, funding, selector

ROLES = ('implementation', 'review', 'hard-reasoning', 'debug', 'default', 'research-scout', 'repo-map')
MODES = ('best-value', 'absolute-best')
SCHEMA = 'fleet-market-freshness/v1'


def scope(harness, provider, pool):
    return f'{harness}/{provider}/{pool}'


def catalog_snapshot(catalog):
    scopes = {}
    for source in catalog.get('provider_catalogs', []):
        key = scope(source['harness'], source['provider'], source['pool'])
        models = source.get('models')
        valid = (isinstance(models, list) and all(isinstance(row, dict)
                 and isinstance(row.get('selector'), str) and row['selector'] for row in models)
                 and len({row['selector'] for row in models}) == len(models))
        scopes[key] = ({row['selector']: {k: row[k] for k in ('model_key', 'price_1m', 'free_promo') if k in row}
                        for row in models} if valid else None)
    scopes['market/models.dev/opencode-go'] = ({k: {'cost': v.get('cost')} for k, v in catalog.get('go_models', {}).items()}
                                               if catalog['sources_ok'].get('models_dev') else None)
    return scopes


def diff(before, after):
    return {'added': sorted(after.keys() - before.keys()), 'removed': sorted(before.keys() - after.keys()),
            'changed': sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}


def entitlements(roster, observed):
    result, queue = {}, {}
    registered = set()
    mapped = set()
    for lane in roster.get('lanes', []):
        lane_id, model = lane['lane_id'], lane['selector']
        ctx = scope(lane['harness'], lane['provider'], lane['quota_pool'])
        rows = observed.get(ctx)
        registered.add((ctx, model))
        if lane.get('admission_status') != 'candidate' or lane['model_key'] in roster.get('effort', {}):
            mapped.add((ctx, model))
        result[lane_id] = {'scope': ctx, 'selector': model, 'access': lane.get('access_status'),
                           'admission': lane.get('admission_status'),
                           'funding': funding.classify(roster, lane['quota_pool'], lane),
                           'listed': model in rows if rows is not None else None}
        if lane.get('admission_status') == 'candidate':
            queue['candidate:' + lane_id] = {'kind': 'candidate', 'scope': ctx, 'selector': model,
                                             'reason': 'awaiting entitlement/operational approval'}
        elif not lane.get('archived_by_owner') and (lane.get('retired_by_refresh') or lane.get('free_price_withdrawn_on')):
            reason = 'nonzero tariff' if lane.get('free_price_withdrawn_on') else 'missing from successful provider check'
            queue['retirement:' + lane_id] = {'kind': 'retirement', 'scope': ctx, 'selector': model, 'reason': reason}
    for key, card in roster.get('model_cards', {}).items():
        if not isinstance(card, dict):
            continue  # Legacy rosters also list prose per card.
        pool = card.get('pool')
        if not pool or card.get('hidden') or not fleet.pool_is_direct(roster, pool):
            continue
        result[f'direct:{pool}:{key}'] = {'scope': scope(pool, pool, pool), 'selector': card.get('run_as', key),
            'access': card.get('access_status', 'declared'), 'admission': card.get('admission_status', 'declared'),
            'funding': funding.classify(roster, pool, card), 'listed': None}
    unmapped = []
    for ctx, rows in sorted(observed.items()):
        if rows is None:
            continue
        for model in sorted(rows):
            key = rows[model].get('model_key', model)
            if (ctx, model) in mapped or any(key in roster.get(table, {}) for table in ('effort', 'model_cards')):
                continue
            if ctx == 'market/models.dev/opencode-go' and ('opencode/opencode-go/opencode-go', 'opencode-go/' + key) in registered:
                continue
            entry = {'kind': 'unmapped', 'scope': ctx, 'selector': model,
                     'reason': 'catalog visibility alone is not entitlement or a model mapping'}
            unmapped.append(entry)
            queue[f'unmapped:{ctx}:{model}'] = entry
    return result, queue, unmapped


def grade_coverage(roster, grades):
    admitted = evidence._admitted_cells(roster)
    rows = selector.evidence_index(roster, grades.get('rows', []))
    coverage = {}
    for role in ROLES:
        family = (roster.get('policy', {}).get('selector', {}).get('families_by_role', {}).get(role)
                  or selector.FAMILY_BY_ROLE.get(role, 'coding-agent'))
        graded = 0
        dated_outside = 0
        for cell in admitted:
            row = rows.get(cell, {})
            q = row.get('q_by_stakes', {}).get('high', {}).get(family, row.get('q', {}).get(family, {}))
            if q.get('mean') is not None and not q.get('unknown') and (q.get('n_sources') or row.get('own', {}).get(family, {}).get('trials')):
                graded += 1
                dated_outside += any(s.get('read_on') and family in (s.get('families') or []) for s in row.get('sources', []))
        coverage[role] = {'family': family, 'graded': graded, 'admitted_cells': len(admitted), 'dated_outside': dated_outside}
    return coverage


def value_preview(roster, state_dir):
    runtime = fleet.load_json(state_dir/'runtime.json', {}) or {}
    picks = {}
    for role in ROLES:
        picks[role] = {}
        for mode in MODES:
            try:
                choice = selector.select_option(roster, runtime, state_dir, role, fleet=fleet,
                                                value_mode=mode, persist_receipts=False)
            except fleet.FleetError:
                picks[role][mode] = {'status': 'no-eligible-route'}
                continue
            # Keep the ranked leader, not the random draw from the best-value band.
            first = choice['top3'][0]
            picks[role][mode] = {k: first[k] for k in ('pool', 'model_key', 'level', 'quality', 'score')}
    return picks


def read_previous(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise ValueError('invalid freshness index')
    return data


def publish(market_dir, roster, catalog, grades):
    """Publish an immutable dated revision plus the current index, under a claim."""
    index = market_dir/'freshness-index.json'
    with (market_dir/'freshness.lock').open('a') as claim:
        fcntl.flock(claim, fcntl.LOCK_EX)
        previous = read_previous(index)
        stamp = dt.datetime.fromisoformat(catalog['fetched_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('catalog fetch timestamp must be offset-aware')
        stamp = stamp.astimezone(dt.timezone.utc)
        last_observed = dict(previous.get('last_observed', {}))
        observed = catalog_snapshot(catalog)
        deltas = {}
        for source, rows in observed.items():
            if rows is None:
                deltas[source] = {'status': 'unknown'}
                continue
            if source in last_observed:
                deltas[source] = {'status': 'observed', **diff(last_observed[source], rows)}
            else:
                deltas[source] = {'status': 'baseline', 'count': len(rows)}
            last_observed[source] = rows
        current, queue, unmapped = entitlements(roster, observed)
        previous_ent = previous.get('entitlements', {})
        # Provider visibility is reported separately, never a change in entitlement.
        def without_visible(data):
            return {key: {k: v for k, v in item.items() if k != 'listed'} for key, item in data.items()}
        # Keep unregistered discoveries pending through transient source failure.
        for key, item in previous.get('queue', {}).items():
            if item.get('kind') == 'unmapped' and observed.get(item['scope']) is None:
                queue.setdefault(key, item)
        coverage = grade_coverage(roster, grades) if isinstance(grades, dict) else {}
        alerts = []
        if unmapped:
            alerts.append(f'{len(unmapped)} unmapped model(s): see freshness review queue')
        for role in ('implementation', 'review', 'hard-reasoning', 'debug'):
            now = coverage.get(role, {})
            before = previous.get('coverage', {}).get(role, {})
            if now.get('admitted_cells') and not now['graded']:
                alerts.append(f'{role}: no graded admitted cells')
            elif now.get('graded', 0) < before.get('graded', 0) * .8:
                alerts.append(f'{role}: grade coverage fell from {before["graded"]} to {now.get("graded", 0)} cells')
        picks = value_preview(roster, market_dir.parent) if isinstance(grades, dict) else {}
        inputs = {k: catalog.get(k) for k in ('artificial_analysis', 'lmarena', 'aider_polyglot', 'swebench', 'sources_ok')}
        inputs['terminal_bench_4_read_on'] = evidence.TB4_DATE
        record = {'schema': SCHEMA, 'fetched_at': catalog['fetched_at'], 'provider_catalogs': observed,
                  'provider_diff': deltas, 'entitlements': current,
                  'entitlement_diff': diff(without_visible(previous_ent), without_visible(current)),
                  'queue': queue, 'unmapped': unmapped, 'benchmark_inputs': inputs, 'coverage': coverage,
                  'grade_read_on': grades.get('read_on') if isinstance(grades, dict) else None,
                  'value_leaders': picks, 'alerts': alerts}
        payload = json.dumps(record, sort_keys=True, allow_nan=False).encode()
        name = f'{stamp:%H%M%S.%f}-{hashlib.sha256(payload).hexdigest()[:12]}.json'
        revision = market_dir/'history'/stamp.date().isoformat()/name
        if not revision.exists():
            fleet.atomic_json(revision, record, allow_nan=False)
        if not previous or stamp >= dt.datetime.fromisoformat(previous['fetched_at'].replace('Z', '+00:00')):
            fleet.atomic_json(index, {'schema': SCHEMA, 'fetched_at': catalog['fetched_at'],
                'revision': str(revision.relative_to(market_dir)), 'last_observed': last_observed,
                'entitlements': current, 'queue': queue, 'coverage': coverage}, allow_nan=False)
        return record
