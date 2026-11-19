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
    return re.sub(r'-(low|medium|high|xhigh|max)$', '', selector.split('/', 1)[-1])


def score_key(key):
    # Promotional suffix is a serving route, not a different set of weights.
    return re.sub(r'[^a-z0-9]', '', re.sub(r'-free$', '', key).lower())


def cli_listing(command):
    """Run a catalog command: (cleaned output lines, None) or (None, plain-words reason)."""
    env = {k: v for k, v in os.environ.items() if k not in {
        'COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN', 'OPENCODE_CONFIG',
        'OPENCODE_CONFIG_CONTENT', 'OPENCODE_CONFIG_DIR'}}
    env.update(OPENCODE_DISABLE_EXTERNAL_SKILLS='1', OPENCODE_DISABLE_CLAUDE_CODE_PROMPT='1')
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
    except FileNotFoundError:
        # A scheduler can omit the operator's PATH entries; report the missing executable.
        return None, f'{command[0]} not found on PATH'
    except subprocess.TimeoutExpired:
        return None, f'{command[0]} timed out after 60 s'
    except OSError:
        return None, f'{command[0]} could not be started'
    if run.returncode:
        return None, f"'{' '.join(command)}' exited with code {run.returncode}"
    lines = [re.sub(r'\x1b\[[0-9;]*m', '', line).strip() for line in run.stdout.splitlines()]
    return [line for line in lines if line], None


def catalog_rows(command, *, prefix=None, listing=None):
    """(rows, None) or (None, reason). `listing` reuses an earlier cli_listing result."""
    lines, why = listing if listing is not None else cli_listing(command)
    if lines is None:
        return None, why
    rows = []
    # Fail the whole snapshot on diagnostics/truncation rather than retiring routes
    # against a partially parsed table. AGY's human table is handled separately.
    for line in lines:
        if prefix:
            # OpenCode v2 lists every connected provider in one call (it rejects a
            # provider argument); keep this provider's rows, fail on anything else.
            if not re.fullmatch(r'[\w.-]+/[\w.:/-]+', line):
                return None, f"'{' '.join(command)}' printed a line that is not a model id"
            if line.startswith(prefix + '/'):
                rows.append({'selector': line, 'model_key': line.split('/', 1)[1]})
        else:
            # AGY lists one selector per row, optionally with a display name.
            match = re.match(r'^([a-z][\w.]*-[\w.-]+)(?:\s+.*)?$', line)
            if not match:
                return None, f"'{' '.join(command)}' printed a line that is not a model id"
            rows.append({'selector': match[1], 'model_key': model_key(match[1])})
    # A provider missing from a successful listing is unknown, never empty:
    # an expired login must not retire every route of that provider.
    if not rows:
        return None, f"'{' '.join(command)}' listed no {prefix + ' ' if prefix else ''}models"
    return rows, None


def cli_catalog(command, *, prefix=None):
    return catalog_rows(command, prefix=prefix)[0]


# Codex is a direct pool: its models are model cards run by codex-agent.sh, and
# fleetctl.pool_is_direct() turns every card route off once any lane sits in pool
# `codex`. Codex catalog candidates therefore live in their own candidate-only pool.
CODEX_CATALOG_POOL = 'codex-catalog'
CODEX_STALE_DAYS = 14


def codex_cache_path():
    explicit = os.environ.get('CODEX_MODELS_CACHE')
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.environ.get('CODEX_HOME') or '~/.codex').expanduser() / 'models_cache.json'


def _cache_time(value):
    # Codex writes RFC 3339 with up to nine fraction digits; Python 3.9 reads six.
    text = re.sub(r'(\.\d{6})\d+', r'\1', str(value or '')).replace('Z', '+00:00')
    stamp = dt.datetime.fromisoformat(text)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def codex_known_models(roster):
    """Models the roster already routes through the Codex CLI (cards, evidence, lanes)."""
    cards = roster.get('model_cards') if isinstance(roster.get('model_cards'), dict) else {}
    known = {key for key, card in cards.items() if isinstance(card, dict) and card.get('pool') == 'codex'}
    evidence = roster.get('model_evidence') if isinstance(roster.get('model_evidence'), dict) else {}
    known |= {key for key, item in evidence.items() if isinstance(item, dict)
              and (item.get('quota_pool') or ('codex' if re.match(r'gpt-\d', key) else None)) == 'codex'}
    known |= {l.get('model_key') for l in roster.get('lanes', [])
              if l.get('harness') == 'codex' and l.get('quota_pool') != CODEX_CATALOG_POOL}
    return known


def codex_catalog(roster, *, now=None):
    """Visible models from the Codex CLI's own cache, minus the ones already routed.

    (rows, None) or (None, reason). Missing, unreadable or stale caches are unknown,
    never empty: a Codex that has not run for a while must not retire anything.
    """
    path = codex_cache_path()
    shown = str(path).replace(str(Path.home()), '~', 1)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None, f'Codex model cache not found at {shown}'
    except (OSError, ValueError, UnicodeError):
        return None, f'Codex model cache at {shown} could not be read'
    if not isinstance(data, dict) or not isinstance(data.get('models'), list):
        return None, f'Codex model cache at {shown} has no model list'
    try:
        fetched = _cache_time(data.get('fetched_at'))
    except (TypeError, ValueError):
        return None, f'Codex model cache at {shown} has no valid fetch time'
    age = ((now or dt.datetime.now(dt.timezone.utc)) - fetched).total_seconds() / 86400
    if age > CODEX_STALE_DAYS:
        return None, (f'Codex model cache is {age:.0f} days old (limit {CODEX_STALE_DAYS}); '
                      'run Codex once to refresh it')
    visible = []
    for model in data['models']:
        if not isinstance(model, dict):
            return None, f'Codex model cache at {shown} has an invalid model entry'
        # Codex's ModelVisibility is list | hide | none; its own picker shows `list` only.
        if model.get('visibility') != 'list':
            continue
        slug = model.get('slug')
        if not isinstance(slug, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,79}', slug):
            return None, f'Codex model cache at {shown} has an invalid model entry'
        name = model.get('display_name')
        visible.append({'selector': slug, 'model_key': slug,
                        'display_name': name if isinstance(name, str) and name else slug})
    if not visible:
        return None, f'Codex model cache at {shown} lists no visible models'
    known = codex_known_models(roster)
    return [row for row in visible if row['model_key'] not in known], None


OPENCODE_LISTING = ['opencode', 'models', '--standalone']


def discover(roster):
    """Probe installed serving catalogs. Never infer native Copilot Auto's weights."""
    catalogs = []
    lanes = roster.get('lanes', [])
    listings = {}

    def listing(command):
        # One OpenCode call lists every provider; run it once per refresh.
        if tuple(command) not in listings:
            listings[tuple(command)] = cli_listing(command)
        return listings[tuple(command)]
    for provider in sorted({l['provider'] for l in lanes if l.get('harness') == 'opencode'} | (
            {'opencode'} if any(l.get('provider') == 'opencode-go' for l in lanes) else set())):
        # Use v2's --standalone to list models without the operator's background service.
        rows, why = catalog_rows(OPENCODE_LISTING, prefix=provider, listing=listing(OPENCODE_LISTING))
        # Zen's paid catalog is not a Go entitlement. Only free promotions enter
        # a separate zero-cost pool; disappearing promotions retire that pool only.
        if provider == 'opencode':
            # v2 lists free promotions under opencode-go too; an unconnected Zen
            # with no lanes in its pool has nothing to reconcile, so it is not
            # reported as an unknown catalog every day.
            if rows is None and not any(l.get('quota_pool') == 'opencode-free' for l in lanes):
                continue
            pool = 'opencode-free'
        else:
            pools = {l['quota_pool'] for l in lanes if l.get('harness') == 'opencode'
                     and l.get('provider') == provider}
            if len(pools) != 1:
                continue
            pool = pools.pop()
        catalogs.append({'harness': 'opencode', 'provider': provider, 'pool': pool,
                         'models': rows, 'source': f'opencode models --standalone ({provider})', 'free_only': provider == 'opencode',
                         'reason': why,
                         'known_promos': [l['selector'] for l in lanes if l.get('quota_pool') == 'opencode-free']})
    if any(l.get('harness') == 'agy' for l in lanes):
        rows, why = catalog_rows(['agy', 'models'])
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
                             'source': 'agy models', 'reason': why, 'unmapped': unknown})
        catalogs.append({'harness': 'agy', 'provider': 'antigravity', 'pool': 'antigravity-catalog',
                         'models': unknown if rows is not None else None, 'reason': why,
                         'source': 'agy models (funding pool requires admission)'})
    if any(l.get('harness') == 'copilot' for l in lanes):
        # Native CLI currently exposes Auto only in this adapter. OpenCode's
        # github-copilot catalog is a different route and cannot grant admission.
        lines, _ = listing(OPENCODE_LISTING)
        # It is visibility evidence only and never reconciled, so an OpenCode with
        # no github-copilot provider connected has nothing to observe (like Zen).
        # A failed listing is still unknown.
        if lines is None or any(line.startswith('github-copilot/') for line in lines):
            rows, why = catalog_rows(OPENCODE_LISTING, prefix='github-copilot', listing=listing(OPENCODE_LISTING))
            catalogs.append({'harness': 'copilot', 'provider': 'github-copilot',
                             'pool': 'github-copilot-student', 'models': rows, 'reason': why,
                             'dynamic': True,
                             'source': 'opencode models --standalone (github-copilot); native Auto identity/entitlement unconfirmed'})
    cards = roster.get('model_cards') if isinstance(roster.get('model_cards'), dict) else {}
    if (any(l.get('harness') == 'codex' for l in lanes) or 'codex' in (roster.get('quota_pools') or {})
            or any(isinstance(c, dict) and c.get('pool') == 'codex' for c in cards.values())):
        rows, why = codex_catalog(roster)
        # Subscription routes: an API price for the same model is not this route's price.
        catalogs.append({'harness': 'codex', 'provider': 'openai', 'pool': CODEX_CATALOG_POOL,
                         'models': rows, 'reason': why, 'subscription': True,
                         'source': 'Codex model cache'})
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
                         'reason': None if models is not None else 'the gateway did not answer /v1/models',
                         'source': 'gateway /v1/models; saved labels, underlying model unconfirmed',
                         'dynamic': True})
    return catalogs


def enrich(catalogs, models_dev):
    """Join exact provider prices; never use another provider's price as a route price."""
    for catalog in catalogs:
        if catalog.get('subscription'):
            continue
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
    changes = {'added': [], 'retired': [], 'prices': [], 'roles': [], 'unknown': [], 'held': [], 'unfunded': []}
    all_lanes = roster['lanes']
    fresh = set()
    successful = set()
    for catalog in catalogs:
        rows = catalog.get('models')
        label = catalog['source']
        if rows is None:
            entry = label + (': ' + catalog['reason'] if catalog.get('reason') else '')
            if entry not in changes['unknown']:
                changes['unknown'].append(entry)
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
                if catalog['pool'] == CODEX_CATALOG_POOL:
                    roster.setdefault('quota_pools', {}).setdefault(CODEX_CATALOG_POOL, {
                        'label': 'Codex catalog candidates; admit as a Codex model card to route'})
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
            if lane.get('archived_by_owner'):
                # Retained tombstones match catalog identities without reintroducing
                # operator-archived routes into the admission queue.
                continue
            if lane.get('retired_by_refresh'):
                lane.update(admission_status='candidate', access_status='unverified', roles=[])
                lane.pop('retired_by_refresh')
            # Repair earlier split keys only on unfunded AGY candidates. Never
            # change the identity or funding of an admitted lane from discovery.
            if (catalog['pool'] == 'antigravity-catalog' and catalog['harness'] == 'agy'
                    and lane.get('admission_status') == 'candidate' and lane.get('access_status') == 'unverified'
                    and not lane.get('roles') and lane['model_key'] == row['selector']
                    and row['selector'].endswith('-xhigh') and row['model_key'] == row['selector'].removesuffix('-xhigh')):
                lane['model_key'] = row['model_key']
            if row.get('free_promo'):
                lane['free_promo'] = True
                lane.setdefault('data_policy', 'public-only')
            price = row.get('price_1m')
            exact_price = isinstance(price, dict) and all(number(price.get(k)) is not None for k in ('input', 'output'))
            if (lane.get('funding') == 'verified-free' and lane.get('admission_status') == 'active'
                    and exact_price and any(price[k] != 0 for k in ('input', 'output'))):
                # A known tariff change invalidates the admission. Do not move
                # the lane to a billed pool or re-activate it on a later refresh.
                lane.update(admission_status='retired', access_status='unverified', roles=[],
                            free_promo=False, free_price_withdrawn_on=today)
                changes['retired'].append(lane['lane_id'])
            if exact_price:
                if lane.get('price_1m') != price:
                    lane['price_1m'] = copy.deepcopy(price)
                    changes['prices'].append(lane['lane_id'])
            lane['market_scores'] = outside_scores(lane['model_key'], market)
            lane['catalog_checked_on'] = today
            if exact_price:
                fresh.add(lane['lane_id'])
        # A listed model with no funding pool is a standing admission question,
        # not a failed check; report it once, separately.
        for row in catalog.get('unmapped', []):
            if row['selector'] not in changes['unfunded']:
                changes['unfunded'].append(row['selector'])
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
    # Lanes the operator pins to their cheap roles: a move that would demote one
    # is held for review instead of applied (BR-REFRESH-002).
    keep = bounds.get('keep_roles', [])
    if not isinstance(keep, list) or not all(isinstance(x, str) for x in keep):
        raise patcher.PatchError('model_refresh keep_roles must be a list of lane ids')
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
        # Preserve the shortlist as well as lane metadata. If a prior run
        # stripped the role label but the owner still ranks the lane in a band,
        # the ranking is enough evidence that automatic removal is not allowed.
        shortlisted = {lane_id for entries in bands.values() if isinstance(entries, list) for lane_id in entries}
        demoted = sorted(l['lane_id'] for l in managed if l['lane_id'] in keep
                         and (role in l.get('roles', []) or l['lane_id'] in shortlisted)
                         and l['lane_id'] not in selected)
        if demoted:
            changes['held'].append(f"{role} (would demote {', '.join(demoted)})")
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
    text = ('Daily roster: ' + ', '.join(f'{len(changes[k])} {k}' for k in
            ('added', 'retired', 'prices', 'roles', 'unknown')) + '.')
    if changes.get('held'):
        text += ' Held for review: ' + '; '.join(changes['held']) + '.'
    if changes.get('unfunded'):
        text += ' AGY models awaiting a funding pool: ' + ', '.join(changes['unfunded']) + '.'
    return text


def unknown_checks(catalogs):
    """{check label: reason} for every catalog check that returned no usable listing."""
    found = {}
    for catalog in catalogs:
        if catalog.get('models') is None:
            found.setdefault(catalog['source'], catalog.get('reason') or 'listing failed')
    return found


def record_unknown_streak(path, unknown):
    """Count consecutive runs each check stayed unknown, in a one-line state file."""
    path = Path(path)
    try:
        previous = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    previous = previous if isinstance(previous, dict) else {}
    counts = {}
    for label in sorted(unknown):
        before = previous.get(label)
        counts[label] = (before if type(before) is int and before > 0 else 0) + 1
    patcher._atomic_write(path, (json.dumps(counts, sort_keys=True) + '\n').encode())
    return counts


def loud_line(unknown, counts, *, threshold=2):
    """The report's first line when a check stayed unknown on consecutive runs, else None."""
    failing = [f'{label} ({unknown[label]}), {counts[label]} runs in a row'
               for label in sorted(unknown) if counts.get(label, 0) >= threshold]
    if not failing:
        return None
    return ('CATALOG CHECK FAILING: ' + '; '.join(failing)
            + '. New models from these catalogs are not being discovered until this is fixed.')
