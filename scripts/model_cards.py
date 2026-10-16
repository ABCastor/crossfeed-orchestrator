"""Private, offline model cards shared by the console and decision-model CLI.

Reads selector evidence, roster prices and quota state on every request. Publisher
scores remain runtime data. Cards inform judgment, they never authorize a route.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

try:
    from . import selector
    from .run_identity import FAMILY_BY_ROLE, canonical_model, evidence_index
except ImportError:
    import selector
    from run_identity import FAMILY_BY_ROLE, canonical_model, evidence_index

# A refusal that names no reset holds the lane for this long, the same hold record_run gives a quota error.
DEFAULT_QUOTA_HOLD_S = 1800
# Where AGY writes the "Resets in 164h32m20s" line its wrapper only reports as exit 3.
AGY_LOG_HOME = Path('~/.gemini/antigravity-cli').expanduser()
_AGY_QUOTA = re.compile(r'^E(\d\d)(\d\d) (\d\d):(\d\d):(\d\d)\.\d+ .*Individual quota reached.*?'
                        r'Resets in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?')


def _when(value):
    """An ISO timestamp as an aware UTC datetime, or None when it is not one."""
    try:
        moment = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _ledger_lines(path):
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            yield from stream
    except OSError:
        return


def agy_quota_reset(ended_at):
    """When AGY's own log says an "Individual quota reached" refusal at `ended_at` resets, or None.

    The wrapper only reports exit 3 for it; the reset ("Resets in 164h32m20s") is in AGY's log, on a line stamped
    in local time. Only a line within ten minutes of the refused run counts, so an older refusal is never read as
    this one.
    """
    ended = _when(ended_at)
    if not ended:
        return None
    best = None
    logs = [AGY_LOG_HOME / 'cli.log', *(AGY_LOG_HOME / 'log').glob('*.log')]
    for log in logs:
        try:
            # New logs must never hide a multi-day refusal in an older session log.
            if log.stat().st_mtime < ended.timestamp() - 600:
                continue
        except OSError:
            continue
        for line in _ledger_lines(log):
            hit = _AGY_QUOTA.match(line)
            if not hit:
                continue
            month, day, hour, minute, second, hours, minutes, seconds = (int(g) if g else 0 for g in hit.groups())
            if not (hours or minutes or seconds):
                continue
            for year in (ended.year - 1, ended.year, ended.year + 1):
                try:
                    stamped = dt.datetime(year, month, day, hour, minute, second).astimezone()   # local, as AGY logs it
                except ValueError:
                    continue
                gap = abs((stamped - ended).total_seconds())
                if gap <= 600 and (best is None or gap < best[0]):
                    best = (gap, stamped + dt.timedelta(hours=hours, minutes=minutes, seconds=seconds))
    return best[1] if best else None


def quota_refusals(state_dir, roster, *, now=None):
    """{(pool, model): {'seen_at', 'until', 'reset_known'}}: the models a provider has refused for quota and not since served.

    Stream the run ledger, retaining the latest quota event per model. A refusal is a run whose error is a quota error, or an AGY run that
    exited 3 (the wrapper's code for "quota reached"). A later successful run of the same model on the same pool
    clears it, and so does its reset passing. Quota here is per model: Gemini 4 Argon can be spent while the rest of
    the Gemini pool still has room, so nothing is inferred about the pool.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    latest = {}
    for line in _ledger_lines(Path(state_dir) / 'runs.jsonl'):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or not record.get('quota_pool'):
            continue
        key = record.get('model_identity') or record.get('selected_model') or record.get('model')
        ended = _when(record.get('ended_at'))
        if not key or not ended:
            continue
        key = (record['quota_pool'], canonical_model(key, roster))
        error = record.get('error') if isinstance(record.get('error'), dict) else {}
        refused = error.get('type') == 'quota' or (record.get('harness') == 'agy' and record.get('returncode') == 3)
        if record.get('status') != 'ok' and not refused:
            continue   # a crash or a timeout says nothing about quota either way
        if key in latest and latest[key]['ended'] > ended:
            continue
        latest[key] = {'ended': ended, 'refused': refused, 'record': record, 'error': error}
    result = {}
    for key, event in latest.items():
        if not event['refused']:
            continue
        logged = agy_quota_reset(event['record'].get('ended_at')) if event['record'].get('harness') == 'agy' else None
        retry = event['error'].get('retry_after_seconds')
        declared = (event['ended'] + dt.timedelta(seconds=retry)
                    if isinstance(retry, (int, float)) and not isinstance(retry, bool) and retry >= 0 else None)
        known = logged is not None or declared is not None
        until = max(filter(None, (logged, declared)), default=None)
        until = until or event['ended'] + dt.timedelta(seconds=DEFAULT_QUOTA_HOLD_S)
        if until > now:
            result[key] = {'seen_at': _iso(event['ended']), 'until': _iso(until), 'reset_known': known}
    return result


def _availability(roster, runtime, fleet, pool, key, model, refusals, now):
    """Whether this provider will run this model now: switched off by the owner, blocked until a reset, or open."""
    off = fleet.pool_level(runtime, pool) == 'off' or not fleet.pool_switches(roster, runtime, pool).get(key, False)
    blocked = None
    if not off:
        state, evidence = fleet.current_pool_state(runtime, pool, now, roster=roster, model_key=key)
        until = _when(evidence.get('until'))
        if state == 'EXHAUSTED' and until and until > now:
            blocked = {'reason': 'pool-circuit' if evidence.get('source') == 'explicit-quota-error' else 'pool-limit',
                       'until': _iso(until), 'reset_known': True}
        refusal = refusals.get((pool, model))
        if refusal and (not blocked or refusal['until'] > blocked['until']):
            blocked = {'reason': 'model-quota', 'until': refusal['until'], 'reset_known': refusal['reset_known']}
    return {'switched_off': off, 'blocked': blocked}


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


def build_cards(roster, runtime, evidence, *, fleet, refusals=None, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    refusals = refusals or {}
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
            sources.append({'pool': pool, 'harness': harness, 'model_key': key,
                            'price_1m': priced.get('price_1m', {}), 'cost': cost,
                            'quota_price': binding['lambda'], 'binding_window': binding['binding_window'],
                            'quota_projection_unknown': binding['projection_unknown'],
                            'latency': latency, 'flags': flags,
                            'availability': _availability(roster, runtime, fleet, pool, key, model, refusals, now)})
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
        outside = [{key: source[key] for key in
                   ('source', 'metric', 'value', 'weight', 'read_on', 'mapping', 'families') if key in source}
                   for source in row.get('sources', [])]
        card = named.get(model) or next((c for key, c in named.items() if canonical_model(key, roster) == model), {})
        cards.append({'model': model, 'level': level, 'family_strengths': strengths,
                      'name': card.get('name'), 'status': card.get('status'),
                      'owner_prior': _owner_prior(roster, model, set(quality) | {f for f, c in own.items() if c.get('trials')}),
                      'quality': quality, 'unmeasured_families': sorted(selector.FAMILIES - set(quality)), 'outside': outside, 'own': {f: c for f, c in own.items() if c.get('trials')},
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
                       fleet.load_json(state_dir / 'evidence/levels.json', {}) or {}, fleet=fleet,
                       refusals=quota_refusals(state_dir, roster))
