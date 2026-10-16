"""Private, offline model cards shared by the console and decision-model CLI.

Reads selector evidence, roster prices and quota state on every request. Publisher
scores remain runtime data. Cards inform judgment, they never authorize a route.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

try:
    from . import selector
    from .run_identity import FAMILY_BY_ROLE, canonical_model, evidence_index
except ImportError:
    import selector
    from run_identity import FAMILY_BY_ROLE, canonical_model, evidence_index


def _when(value):
    """An ISO timestamp as an aware UTC datetime, or None when it is not one."""
    try:
        moment = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _availability(roster, runtime, fleet, pool, key, now):
    """Admission plus the fleet's authoritative pool state, evaluated once per route."""
    off = fleet.pool_level(runtime, pool) == 'off' or not fleet.pool_switches(roster, runtime, pool).get(key, False)
    state, evidence = fleet.current_pool_state(runtime, pool, now, roster=roster, model_key=key)
    until = _when(evidence.get('until'))
    blocked = None
    if not off and state == 'EXHAUSTED':
        blocked = {'reason': evidence.get('source'), 'until': _iso(until) if until else None,
                   'reset_known': until is not None}
    return {'quota_state': state, 'quota_reset_at': evidence.get('until'),
            'availability': {'switched_off': off, 'blocked': blocked}}


def _owner_prior(roster, model, measured):
    """Families the owner expects this model to lead in, from the roles its provisional lanes were admitted for.

    A prior is a starting judgement, not a test: it is read only for families with no measurement behind them.
    """
    by_role = roster.get('policy', {}).get('selector', {}).get('families_by_role', {})
    families = set()
    for lane in roster.get('lanes', []):
        if (lane.get('evidence_confidence') == 'provisional' and lane.get('admission_status') == 'active'
                and canonical_model(lane.get('model_key'), roster) == model):
            families.update(by_role.get(role) or FAMILY_BY_ROLE.get(role) for role in lane.get('roles') or [])
    return sorted(family for family in families if family and family not in measured)


def build_cards(roster, runtime, evidence, *, fleet, now=None):
    now = now or fleet.utc_now()
    named = fleet.model_cards(roster)
    rows = evidence_index(roster, evidence.get('rows', []))
    cells = set(rows)
    for model, effort in roster.get('effort', {}).items():
        levels = effort.get('levels') or {}
        levels = [level for group in levels.values() for level in group] if isinstance(levels, dict) else levels
        cells.update((canonical_model(model, roster), level) for level in levels or [effort.get('default', 'provider-default')])
    routes = list(roster.get('lanes', []))
    for model, card in fleet.model_cards(roster).items():
        if card.get('pool'):
            routes.append(dict(card, model_key=model, quota_pool=card['pool'], harness=card['pool']))
    cards = []
    policy = roster.get('policy', {}).get('selector', {})
    for model, level in sorted(cells):
        row = rows.get((model, level), {})
        families = row.get('q', {})
        strengths = sorted((family for family, q in families.items() if not q.get('unknown', True)),
                           key=lambda family: (-(families[family]['mean'] - families[family]['sd']), family))
        own = row.get('own', {})
        trials = sum(c.get('trials', 0) for c in own.values())
        passes = sum(c.get('passes', 0) for c in own.values())
        sources = []
        seen = set()
        for lane in routes:
            key = lane.get('model_key')
            if canonical_model(key, roster) != model:
                continue
            harness = lane.get('harness')
            exposed = roster.get('effort', {}).get(key, {}).get('levels', {})
            exposed = exposed.get(harness, []) if isinstance(exposed, dict) else exposed
            if exposed and level not in exposed:
                continue
            pool = lane.get('quota_pool')
            identity = (pool, harness, key)
            if not pool or identity in seen:
                continue
            seen.add(identity)
            binding = selector._pool_price(roster, runtime, pool, policy, fleet, model_key=key)
            price = lane.get('price_1m', row.get('price_1m', {}))
            price = price if isinstance(price, dict) else {}
            price = {side: value if (value := selector._number(price.get(side))) is not None and value >= 0 else None
                     for side in ('in', 'out')}
            priced = dict(row, price_1m=price)
            option = dict(pool=pool, harness=harness, model_key=key, level=level, run_as=lane.get('run_as', key))
            cost, flags = selector._task_cost(roster, option, priced, binding, None)
            telemetry = row.get('source_telemetry', {})
            latency = telemetry.get(pool + '/' + str(harness), telemetry.get(pool, {}))
            availability = _availability(roster, runtime, fleet, pool, key, now)
            sources.append({**availability, 'pool': pool, 'harness': harness, 'model_key': key,
                            'price_1m': priced.get('price_1m', {}), 'cost': cost,
                            'quota_price': binding['lambda'], 'binding_window': binding['binding_window'],
                            'quota_projection_unknown': binding['projection_unknown'],
                            'latency': latency, 'flags': flags})
        if strengths:
            verdict = 'Strongest evidence: ' + ', '.join(strengths[:2]) + '.'
            verdict += f' Local checks: {passes}/{trials}; compare the same task and harness.' if trials else ' Outside priors only; local performance unmeasured.'
        else:
            verdict = 'No measured family evidence at this thinking level.'
        # Keep policy text once at the top. Repeating a publisher's assumption
        # and raw source object inside every family wastes decision-model context.
        quality = {family: {key: q[key] for key in
                   ('mean', 'sd', 'unknown', 'prior_strength', 'anchor_metric', 'imputed') if key in q}
                   for family, q in families.items() if not q.get('unknown', True)}
        # Preserve the normalized outside anchor separately from the local-updated estimate.
        outside_quality = {}
        for family, q in families.items():
            if family in quality:
                anchor = next((source for source in q.get('sources', [])
                               if isinstance(source.get('prior_mean'), (int, float))), None)
                if anchor:
                    outside_quality[family] = anchor['prior_mean']
        outside = [{key: source[key] for key in
                   ('source', 'metric', 'value', 'weight', 'read_on', 'mapping', 'families') if key in source}
                   for source in row.get('sources', [])]
        card = named.get(model) or next((c for key, c in named.items() if canonical_model(key, roster) == model), {})
        cards.append({'model': model, 'level': level, 'family_strengths': strengths,
                      'name': card.get('name'), 'status': card.get('status'),
                      'owner_prior': _owner_prior(roster, model, set(quality) | {f for f, c in own.items() if c.get('trials')}),
                      'quality': quality, 'outside_quality': outside_quality, 'unmeasured_families': sorted(selector.FAMILIES - set(quality)), 'outside': outside, 'own': {f: c for f, c in own.items() if c.get('trials')},
                      'own_provenance': row.get('own_provenance', {}),
                      'evidence_counts': {'trials': trials, 'passes': passes,
                                          'outside_metrics': len(row.get('sources', []))},
                      'price_1m': row.get('price_1m', {}), 'own_cost': row.get('own_cost', {}),
                      'latency_s': row.get('latency_s'), 'source_telemetry': row.get('source_telemetry', {}),
                      'sources': sources, 'read_on': row.get('read_on'),
                      'flags': [flag for flag in row.get('flags', []) if not flag.startswith('unknown:')], 'verdict': verdict})
    return {'schema': 'crossfeed-model-cards/v1', 'private': True,
            'read_on': evidence.get('read_on'),
            'caution': 'Heuristic priors, not calibrated success probabilities. Check route admission before dispatch. Runtime publisher scores must not be published.',
            'assumptions': evidence.get('assumptions', {}), 'rows': cards}


def read_cards(state_dir, roster, *, fleet):
    state_dir = Path(state_dir)
    return build_cards(roster, fleet.load_json(state_dir / 'runtime.json', {}) or {},
                       fleet.load_json(state_dir / 'evidence/levels.json', {}) or {}, fleet=fleet)
