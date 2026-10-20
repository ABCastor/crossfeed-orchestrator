"""Daily catalog reconciliation. No generation calls, credentials or new service.

A successful catalog is serving evidence, never operational admission. Failed
catalogs are unknown, not empty. All mutations share one validated transaction.
"""
from __future__ import annotations

import copy
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess

try:
    from . import patch_opencode_go_roles as patcher
except ImportError:
    import patch_opencode_go_roles as patcher

CHEAP_ROLES = ('default', 'repo-map', 'research-scout')
DEFAULT_BOUNDS = {'aa_floor': 40, 'helper_usd_cap': .01, 'max_roles_per_day': 2}


def number(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 else None)


def model_key(selector):
    return re.sub(r'-(low|medium|high|max)$', '', selector.split('/', 1)[-1])


def score_key(key):
    # Promotional suffix is a serving route, not a different set of weights.
    return re.sub(r'[^a-z0-9]', '', re.sub(r'-free$', '', key).lower())


def cli_catalog(command, *, prefix=None):
    env = {k: v for k, v in os.environ.items() if k not in {
        'COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN', 'OPENCODE_CONFIG',
        'OPENCODE_CONFIG_CONTENT', 'OPENCODE_CONFIG_DIR'}}
    env.update(OPENCODE_DISABLE_EXTERNAL_SKILLS='1', OPENCODE_DISABLE_CLAUDE_CODE_PROMPT='1')
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode:
        return None
    rows = []
    # Fail the whole snapshot on diagnostics/truncation rather than retiring routes
    # against a partially parsed table. AGY's human table is handled separately.
    for line in run.stdout.splitlines():
        line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        if not line:
            continue
        if prefix:
            if not re.fullmatch(re.escape(prefix) + r'/[\w.:-]+', line):
                return None
            rows.append({'selector': line, 'model_key': line.split('/', 1)[1]})
        else:
            # AGY lists one selector per row, optionally with a display name.
            match = re.match(r'^([a-z][\w.]*-[\w.-]+)(?:\s+.*)?$', line)
            if not match:
                return None
            rows.append({'selector': match[1], 'model_key': model_key(match[1])})
    return rows or None


def discover(roster):
    """Probe installed serving catalogs. Never infer native Copilot Auto's weights."""
    catalogs = []
    lanes = roster.get('lanes', [])
    for provider in sorted({l['provider'] for l in lanes if l.get('harness') == 'opencode'} | (
            {'opencode'} if any(l.get('provider') == 'opencode-go' for l in lanes) else set())):
        rows = cli_catalog(['opencode', 'models', provider], prefix=provider)
        # Zen's paid catalog is not a Go entitlement. Only free promotions enter
        # a separate zero-cost pool; disappearing promotions retire that pool only.
        if provider == 'opencode':
            pool = 'opencode-free'
        else:
            pools = {l['quota_pool'] for l in lanes if l.get('harness') == 'opencode'
                     and l.get('provider') == provider}
            if len(pools) != 1:
                continue
            pool = pools.pop()
        catalogs.append({'harness': 'opencode', 'provider': provider, 'pool': pool,
                         'models': rows, 'source': f'opencode models {provider}', 'free_only': provider == 'opencode',
                         'known_promos': [l['selector'] for l in lanes if l.get('quota_pool') == 'opencode-free']})
    if any(l.get('harness') == 'agy' for l in lanes):
        rows = cli_catalog(['agy', 'models'])
        # AGY models span different funding pools: only reconcile known selectors
        # or a new effort variant of a known model. Unknown pool stays a candidate
        # in the report rather than borrowing somebody else's allowance.
        pools = {l['quota_pool'] for l in lanes if l.get('harness') == 'agy' and l.get('quota_pool') != 'antigravity-catalog'}
        unknown = [r for r in rows or [] if not any(l.get('harness') == 'agy'
                   and l.get('quota_pool') != 'antigravity-catalog'
                   and l.get('model_key') == r['model_key'] for l in lanes)]
        for pool in sorted(pools):
            keys = {l['model_key'] for l in lanes if l.get('harness') == 'agy' and l['quota_pool'] == pool}
            catalogs.append({'harness': 'agy', 'provider': 'antigravity', 'pool': pool,
                             'models': [r for r in rows if r['model_key'] in keys] if rows is not None else None,
                             'source': 'agy models', 'unmapped': unknown})
        catalogs.append({'harness': 'agy', 'provider': 'antigravity', 'pool': 'antigravity-catalog',
                         'models': unknown if rows is not None else None,
                         'source': 'agy models (funding pool requires admission)'})
    if any(l.get('harness') == 'copilot' for l in lanes):
        # Native CLI currently exposes Auto only in this adapter. OpenCode's
        # github-copilot catalog is a different route and cannot grant admission.
        catalogs.append({'harness': 'copilot', 'provider': 'github-copilot',
                         'pool': 'github-copilot-student',
                         'models': cli_catalog(['opencode', 'models', 'github-copilot'], prefix='github-copilot'),
                         'dynamic': True,
                         'source': 'opencode models github-copilot; native Auto identity/entitlement unconfirmed'})
    # Reuse the gateway's own validated discovery, including provider instances.
    try:
        from . import chatgpt_transport as chat
    except ImportError:
        import chatgpt_transport as chat
    gateways = [roster['chatgpt_gateway']] if roster.get('chatgpt_gateway') else []
    for source in roster.get('provider_sources', {}).values() if isinstance(roster.get('provider_sources'), dict) else []:
        if source.get('kind') == 'crossfeed-chat' and source.get('gateway'):
            gateways.append(source['gateway'])
    for gateway in gateways:
        template = gateway['lane_template']
        try:
            base, key = chat.settings(template)
            rows = chat.catalog_models(chat.request(base, key, '/models', timeout=5), gateway.get('current_rows') or ['Latest'])
            models = [{'selector': selector, 'model_key': selector} for selector in rows]
        except (chat.Rejected, OSError, ValueError, KeyError):
            models = None
        catalogs.append({'harness': 'chatgpt-chat', 'provider': template['provider'],
                         'pool': template.get('quota_pool', 'chatgpt-work'), 'models': models,
                         'source': 'gateway /v1/models; saved labels, underlying model unconfirmed',
                         'dynamic': True})
    return catalogs


def enrich(catalogs, models_dev):
    """Join exact provider prices; never use another provider's price as a route price."""
    for catalog in catalogs:
        provider = models_dev.get(catalog['provider']) or {}
        provider = provider if isinstance(provider, dict) else {}
        provider_models = provider.get('models') or {}
        provider_models = provider_models if isinstance(provider_models, dict) else {}
        for row in catalog.get('models') or []:
            key = row['selector'].split('/', 1)[-1]
            info = provider_models.get(key) or {}
            info = info if isinstance(info, dict) else {}
            row['name'] = info.get('name', row['model_key'])
            if isinstance(info.get('cost'), dict):
                row['price_1m'] = {k: info['cost'].get(k) for k in ('input', 'output')}
            free = key.endswith('-free') or bool(re.search(r'\bfree\b', str(info.get('name', '')), re.I))
            if free and ('price_1m' not in row or row['price_1m'] == {'input': 0, 'output': 0}):
                row.update(free_promo=True, price_1m={'input': 0, 'output': 0})
        if catalog.get('free_only') and catalog.get('models') is not None:
            catalog['models'] = [r for r in catalog['models'] if r.get('free_promo') or (
                r['selector'] in catalog.get('known_promos', []) and 'price_1m' not in r)]
    return catalogs


def outside_scores(key, market):
    matches = [value for name, value in market.get('artificial_analysis', {}).items()
               if score_key(name) == score_key(key)]
    if len(matches) != 1:
        return {}
    aa = matches[0]
    return {'aa_intelligence': aa.get('intelligence_index'), 'aa_coding': aa.get('coding_index'),
            'vals': aa.get('vals', (aa.get('evaluations') or {}).get('vals_index')),
            'source': 'Artificial Analysis', 'fetched_at': market.get('fetched_at')}


def reconcile(source, catalogs, market, *, today=None):
    # BR-REFRESH-001, BR-BENCH-001
    roster = copy.deepcopy(source)
    today = today or dt.datetime.now().astimezone().date().isoformat()
    changes = {'added': [], 'retired': [], 'prices': [], 'roles': [], 'unknown': []}
    all_lanes = roster['lanes']
    fresh = set()
    successful = set()
    for catalog in catalogs:
        rows = catalog.get('models')
        label = catalog['source']
        if rows is None:
            changes['unknown'].append(label)
            continue
        # Dynamic gateway lanes already have their own live admission/retirement
        # path. Persisting them here would collide with that expansion.
        if catalog.get('dynamic'):
            continue
        if (not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('selector'), str)
                or not r['selector'] or not isinstance(r.get('model_key'), str) or not r['model_key'] for r in rows)
                or len({r['selector'] for r in rows}) != len(rows)):
            changes['unknown'].append(label + ' (invalid catalog)')
            continue
        scope = [l for l in all_lanes if (l.get('harness'), l.get('provider'), l.get('quota_pool')) ==
                 (catalog['harness'], catalog['provider'], catalog['pool'])]
        by_selector = {l['selector']: l for l in scope}
        seen = {r['selector'] for r in rows}
        for lane in scope:
            if lane['selector'] not in seen and lane.get('admission_status') != 'retired':
                lane.update(admission_status='retired', roles=[], retired_by_refresh=today)
                changes['retired'].append(lane['lane_id'])
        for row in rows:
            lane = by_selector.get(row['selector'])
            if lane is None:
                # New lanes inherit transport limits only, never credentials,
                # operational evidence, effort controls or roles from a sibling.
                lane_id = 'catalog-' + re.sub(r'[^a-zA-Z0-9_.-]', '-', catalog['pool'] + '-' + row['selector'])
                if any(l['lane_id'] == lane_id for l in all_lanes):
                    raise patcher.PatchError('catalog lane_id collision')
                if catalog['pool'] == 'antigravity-catalog':
                    roster.setdefault('quota_pools', {}).setdefault('antigravity-catalog', {'label': 'AGY catalog candidates; funding unassigned'})
                if catalog['pool'] == 'opencode-free':
                    roster.setdefault('quota_pools', {}).setdefault('opencode-free', {'label': 'OpenCode free promotions'})
                lane = {'lane_id': lane_id, 'harness': catalog['harness'], 'provider': catalog['provider'],
                        'quota_pool': catalog['pool'], 'selector': row['selector'], 'model_key': row['model_key'],
                        'access_status': 'unverified', 'admission_status': 'candidate', 'roles': [],
                        'quality_tier': 'unmeasured', 'capabilities': {'input': ['text'], 'output': ['text']},
                        'allowed_modes': ['read-only'], 'max_parallel': 1, 'max_tasks_per_run': 1,
                        'timeout_s': 600, 'retries': 0, 'data_policy': 'public-only',
                        'notes': 'Catalog only. Verify entitlement, controls and small admission check before activation.'}
                all_lanes.append(lane)
                changes['added'].append(lane_id)
            successful.add(lane['lane_id'])
            if lane.get('retired_by_refresh'):
                lane.update(admission_status='candidate', access_status='unverified', roles=[])
                lane.pop('retired_by_refresh')
            if row.get('free_promo'):
                lane['free_promo'] = True
                lane.setdefault('data_policy', 'public-only')
            price = row.get('price_1m')
            if isinstance(price, dict) and all(number(price.get(k)) is not None for k in ('input', 'output')):
                if lane.get('price_1m') != price:
                    lane['price_1m'] = copy.deepcopy(price)
                    changes['prices'].append(lane['lane_id'])
            lane['market_scores'] = outside_scores(lane['model_key'], market)
            lane['catalog_checked_on'] = today
            if isinstance(price, dict) and all(number(price.get(k)) is not None for k in ('input', 'output')):
                fresh.add(lane['lane_id'])
        for row in catalog.get('unmapped', []):
            changes['unknown'].append('unmapped funding pool: ' + row['selector'])
    # Retirement is a safety action, outside the value-churn allowance. Keep
    # references valid; never silently replace a vanished council member.
    retired = set(changes['retired'])
    for bands in roster.get('routing', {}).get('roles', {}).values():
        for band, ranking in bands.items():
            if isinstance(ranking, list):
                bands[band] = [x for x in ranking if x not in retired]
    for profile in roster.get('swarm_profiles', {}).values():
        for band in profile.get('bands', {}).values():
            band['workers'] = [w for w in band.get('workers', []) if w.get('lane_id') not in retired]
    rerank(roster, changes, today, fresh, successful)
    return roster, changes


def rerank(roster, changes, today, fresh, successful):
    # BR-REFRESH-001
    bounds = {**DEFAULT_BOUNDS, **roster.get('policy', {}).get('model_refresh', {})}
    floor, cap, limit = bounds['aa_floor'], bounds['helper_usd_cap'], bounds['max_roles_per_day']
    if (number(floor) is None or number(cap) is None or type(limit) is not int or not 0 <= limit <= 2
            or floor < DEFAULT_BOUNDS['aa_floor'] or cap > DEFAULT_BOUNDS['helper_usd_cap']):
        raise patcher.PatchError('model_refresh bounds can only tighten the fixed defaults')
    counter = roster.get('model_refresh_day', {})
    used = counter.get('roles_changed', 0) if counter.get('date') == today else 0
    if type(used) is not int or not 0 <= used <= 2:
        raise patcher.PatchError('invalid daily role counter')
    # Only Go and zero-cost promotions are allowed automatic changes. Discovery
    # of other subscriptions still updates catalog metadata and retirement.
    managed = [l for l in roster['lanes'] if l['lane_id'] in successful and l['lane_id'] in fresh
               and number(l.get('market_scores', {}).get('aa_intelligence')) is not None
               and l.get('harness') == 'opencode'
               and l.get('quota_pool') in {'opencode-go', 'opencode-free'}]
    ranked = []
    for lane in managed:
        quality = number(lane.get('market_scores', {}).get('aa_intelligence'))
        cost = patcher.helper_call_cost(lane.get('price_1m', {}))
        if (lane['lane_id'] not in fresh or lane.get('access_status') != 'verified' or lane.get('admission_status') != 'active'
                or quality is None or quality < floor or cost is None or cost > cap):
            continue
        ranked.append((0 if cost == 0 else 1, -(quality if cost == 0 else quality / cost), lane['lane_id']))
    selected = [row[2] for row in sorted(ranked)]
    ids = {l['lane_id'] for l in managed}
    anchors = {l['lane_id'] for l in roster['lanes'] if l['lane_id'] in successful
               and l.get('harness') == 'opencode'
               and l.get('quota_pool') in {'opencode-go', 'opencode-free'}}
    role_rules = roster.get('routing', {}).get('roles', {})
    # Do not erase operational routes on a missing AA feed or unknown prices.
    # A known price/quality disqualification must remove its old roles even
    # when there is no winner. Unassessed routes remain unchanged.
    if not managed:
        return
    for role in CHEAP_ROLES:
        if used >= limit:
            break
        if role not in role_rules:
            continue
        bands = role_rules[role]
        candidate = copy.deepcopy(bands)
        for band, entries in candidate.items():
            if not isinstance(entries, list):
                continue
            positions = [i for i, x in enumerate(entries) if x in anchors]
            insert = sum(x not in ids for x in entries[:positions[0]]) if positions else len(entries)
            retained = [x for x in entries if x not in ids]
            retained[insert:insert] = selected
            candidate[band] = retained
        lane_delta = any((role in l.get('roles', [])) != (l['lane_id'] in selected) for l in managed)
        if candidate == bands and not lane_delta:
            continue
        role_rules[role] = candidate
        for lane in managed:
            roles = [r for r in lane.get('roles', []) if r != role]
            if lane['lane_id'] in selected:
                roles.append(role)
            lane['roles'] = roles
            lane['value_routing_managed'] = True
        used += 1
        changes['roles'].append(role)
    roster['model_refresh_day'] = {'date': today, 'roles_changed': used}


# BR-REFRESH-001
def apply_refresh(overlay, catalogs, market, *, today=None):
    """One lock/backup/validated atomic replace; counter lives in the same write."""
    overlay = Path(overlay)
    if overlay.is_symlink():
        raise patcher.PatchError('refusing symlink overlay')
    with overlay.with_name(overlay.name + '.refresh.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = overlay.read_bytes()
        candidate, changes = reconcile(json.loads(original), catalogs, market, today=today)
        if candidate == json.loads(original):
            return changes
        updated = patcher.dump(candidate)
        patcher.validate_overlay(updated, label='daily refresh candidate')
        backup = patcher._backup_path(overlay)
        shutil.copy2(overlay, backup)
        os.chmod(backup, 0o600)
        if backup.read_bytes() != original or overlay.read_bytes() != original:
            raise patcher.PatchError('overlay changed while preparing refresh')
        try:
            patcher._atomic_write(overlay, updated.encode())
            patcher.validate_overlay(overlay.read_text(), label='daily refresh applied overlay')
        except BaseException:
            patcher._atomic_write(overlay, original)
            raise
        return changes


def summary(changes):
    return ('Daily roster: ' + ', '.join(f'{len(changes[k])} {k}' for k in
            ('added', 'retired', 'prices', 'roles', 'unknown')) + '.')
