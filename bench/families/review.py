"""Review-only patches: one functional regression among benign changes."""
import ast
import difflib
import json
import textwrap

TEMPLATE_IDS = ('ranked_feed', 'civil_time', 'memo_coherency', 'retry_scheduler')
TIERS = ('easy', 'medium', 'hard', 'expert')


def _source(code):
    return textwrap.dedent(code).lstrip()


def _negated(expression):
    """Equivalent inverse guards make benign patches require semantic reading."""
    node = ast.parse(expression, mode='eval').body
    if isinstance(node, ast.BoolOp):
        joiner = ' or ' if isinstance(node.op, ast.And) else ' and '
        return '('+joiner.join(_negated(ast.unparse(child)) for child in node.values)+')'
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        inverse = {ast.Eq: '!=', ast.NotEq: '==', ast.Lt: '>=', ast.LtE: '>',
                   ast.Gt: '<=', ast.GtE: '<', ast.In: 'not in', ast.NotIn: 'in',
                   ast.Is: 'is not', ast.IsNot: 'is'}
        op = inverse.get(type(node.ops[0]))
        if op is not None:
            return '('+ast.unparse(node.left)+' '+op+' '+ast.unparse(node.comparators[0])+')'
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return '('+ast.unparse(node.operand)+')'
    return 'not ('+expression+')'


def _fixture(rng, contract, fixed, bug_file, good_line, bad_line, function, cases, expected, tier):
    if fixed[bug_file].splitlines().count(good_line) != 1:
        raise ValueError('repair line must identify exactly one source line')
    new = dict(fixed)
    new[bug_file] = fixed[bug_file].replace(good_line, bad_line)
    build = rng.randrange(10000, 1000000)
    old, workspace = {}, {}
    for name, source in fixed.items():
        legacy = []
        for line in source.splitlines():
            indent = line[:len(line)-len(line.lstrip())]
            stripped = line.lstrip()
            if stripped.startswith('return ') and stripped != 'return None':
                expression = stripped[7:]
                parsed = ast.parse(expression, mode='eval').body
                if isinstance(parsed, (ast.Compare, ast.BoolOp)):
                    expression = 'not ('+_negated(expression)+')'
                legacy.extend([indent+'_result = '+expression, indent+'return _result'])
            elif stripped.startswith('if ') and stripped.endswith(':'):
                legacy.append(indent+'if not ('+_negated(stripped[3:-1])+'):')
            elif stripped.startswith('for ') and stripped.endswith(':') and ' in ' in stripped:
                target, expression = stripped[4:-1].split(' in ', 1)
                legacy.append(indent+'for '+target+' in iter('+expression+'):')
            else:
                legacy.append(line)
        old[name] = '# Previous reviewed implementation.\n'+'\n'.join(legacy)+'\n'
        # Seeded harmless observability change shifts source locations.
        header = '# Refactor build %d; remove legacy temporary variables and simplify control flow.\n' % build
        header += '# Review production semantics against CONTRACT.md.\n' * rng.randrange(1, 5)
        current = header+new[name]
        workspace['old/'+name] = old[name]
        workspace['new/'+name] = current
    patch = ''.join(''.join(difflib.unified_diff(workspace['old/'+name].splitlines(True), workspace['new/'+name].splitlines(True), fromfile='old/'+name, tofile='new/'+name)) for name in fixed)
    workspace['PATCH.diff'] = patch
    workspace['CONTRACT.md'] = contract+'\n'
    workspace['scenarios.json'] = json.dumps({'function': function, 'argument_lists': cases[:3]}, indent=2)+'\n'
    workspace['review_notes.txt'] = ('The patch also simplifies return temporaries, equivalent inverse guards and loop iteration, plus headers. These refactors preserve semantics.\n'
                                    'The previous sources are provided to distinguish a regression from legacy behavior.\n')
    target = 'new/'+bug_file
    line = workspace[target].splitlines().index(bad_line)+1
    check = {'kind': 'review', 'bug_file': target, 'bug_line': line,
             'validation': {'fixed_line': good_line, 'buggy_line': bad_line,
                            'function': function, 'cases': cases, 'expected': expected}}
    prompt = ('Review PATCH.diff and the old/ and new/ source trees against CONTRACT.md. scenarios.json supplies sample argument lists, not expected outputs. Exactly ONE newly introduced functional defect exists; other executable refactors preserve semantics.\n'
              'Do not modify files. Report the defective NEW-file line, not an old-file or diff line. Your final line must be a JSON object with bug_file (path such as new/service.py) and bug_line (one-based integer).\n\n'+contract+'\n')
    return prompt, workspace, check, dict(workspace), json.dumps({'bug_file': target, 'bug_line': line})+'\n'


def _feed(rng, tier):
    size = rng.randrange(2, 7)
    if tier == 'easy':
        main = _source('''
            def feed(rows, offset, limit):
                return rows[offset:offset+limit]
        ''')
        good, bad = '    return rows[offset:offset+limit]', '    return rows[offset:offset+limit+1]'
        contract = 'feed(rows, offset, limit) returns up to limit rows starting at nonnegative offset, preserving order. limit > 0. The diagnostic refactor must preserve offset pagination.'
        cases = [[[i for i in range(20)], 1, size], [[], 0, size], [[1, 2, 3], 9, size]]
        expected = [a[o:o+n] for a, o, n in cases]
        return _fixture(rng, contract, {'service.py': main}, 'service.py', good, bad, 'feed', cases, expected, tier)
    main = _source('''
        def feed(rows, cursor, limit):
            rows = sorted(rows, key=lambda r: (-r['score'], r['id']))
            eligible = [r for r in rows if cursor is None or (-r['score'], r['id']) > (-cursor[0], cursor[1])]
            return eligible[:limit]
    ''')
    good = "    eligible = [r for r in rows if cursor is None or (-r['score'], r['id']) > (-cursor[0], cursor[1])]"
    bad = good.replace(' > ', ' >= ')
    contract = 'feed(rows, cursor, limit): rank rows by descending integer score, then ascending string id. cursor is null or [score,id]; select only rows STRICTLY AFTER its rank key, return first limit. Identifiers are unique. Equal-score ties must not repeat the cursor row.'
    rows = [{'id': chr(97+i), 'score': size if i < 4 else size-1} for i in range(7)]
    rng.shuffle(rows)
    cases = [[rows, [size, 'b'], size], [rows, None, size], [rows, [size-1, 'z'], size]]
    expected = [[r for r in sorted(rows, key=lambda r: (-r['score'], r['id'])) if cur is None or (-r['score'], r['id']) > (-cur[0], cur[1])][:n] for rows, cur, n in cases]
    fixed, bug_file = {'service.py': main}, 'service.py'
    if tier in ('hard', 'expert'):
        main = _source('''
            from visibility import visible
            from ranking import rank_key

            def feed(rows, viewer, cursor, limit):
                ranked = sorted((r for r in rows if visible(r, viewer)), key=rank_key)
                eligible = [r for r in ranked if cursor is None or rank_key(r) > (-cursor[0], cursor[1])]
                return eligible[:limit]
        ''')
        visibility = _source('''
            def visible(row, viewer):
                return not row['deleted'] and (row['public'] or viewer in row['readers'])
        ''')
        ranking = _source('''
            def rank_key(row):
                return (-row['score'], row['id'])
        ''')
        fixed = {'service.py': main, 'visibility.py': visibility, 'ranking.py': ranking}
        good = "    return not row['deleted'] and (row['public'] or viewer in row['readers'])"
        bad = "    return not row['deleted'] and row['public'] or viewer in row['readers']"
        bug_file = 'visibility.py'
        contract = contract.replace('feed(rows, cursor, limit)', 'feed(rows, viewer, cursor, limit)')+' Visibility must exclude deleted rows even if the viewer is explicitly listed in readers. A live row is visible if public OR the viewer is in readers; visibility filtering happens before selecting the page.'
        for row in rows: row.update(deleted=False, public=True, readers=[])
        rows[0].update(deleted=True, readers=['v'])
        rows[1].update(public=False, readers=['v'])
        cases = [[rows, 'v', None, size], [rows, 'outsider', None, size], [rows, 'v', [size, 'b'], size]]
        def visible_oracle(row, viewer):
            return row['deleted'] is False and (row['public'] is True or viewer in row['readers'])
        expected = [[r for r in sorted(rows, key=lambda r: (-r['score'], r['id'])) if visible_oracle(r, viewer) and (cur is None or (-r['score'], r['id']) > (-cur[0], cur[1]))][:n] for rows, viewer, cur, n in cases]
        cases.append([[{'id': 'gone', 'score': 999, 'deleted': True, 'public': False, 'readers': ['v']}], 'v', None, size])
        expected.append([])
    if tier == 'expert':
        ranking = _source('''
            def rank_key(row):
                return (-row['snapshot_score'], row['id'])
        ''')
        history = _source('''
            def snapshot_rows(versions, snapshot):
                eligible = [row for row in versions if row['revision'] <= snapshot]
                latest = {}
                for row in eligible:
                    token = (row['tenant'], row['id'])
                    if token not in latest or row['revision'] > latest[token]['revision']:
                        latest[token] = row
                return list(latest.values())
        ''')
        main = _source('''
            from visibility import visible
            from ranking import rank_key
            from history import snapshot_rows

            def feed(versions, tenant, viewer, snapshot, cursor, limit):
                rows = snapshot_rows(versions, snapshot)
                ranked = sorted((r for r in rows if r['tenant'] == tenant and visible(r, viewer)), key=rank_key)
                eligible = [r for r in ranked if cursor is None or rank_key(r) > (-cursor[0], cursor[1])]
                page = eligible[:limit]
                return {'items': page, 'next': [page[-1]['snapshot_score'], page[-1]['id']] if len(eligible) > limit else None}
        ''')
        fixed = {'service.py': main, 'visibility.py': visibility, 'ranking.py': ranking, 'history.py': history}
        good = "        if token not in latest or row['revision'] > latest[token]['revision']:"
        bad = "        if token not in latest or row['revision'] >= latest[token]['revision']:"
        # Equal revisions may be duplicated by mirrors; first-seen record wins, including deletion/ACL payload.
        bug_file = 'history.py'
        contract = 'feed(versions, tenant, viewer, snapshot, cursor, limit): each row is a materialized revision with tenant,id,revision,snapshot_score,current_score,deleted,public,readers. Snapshot is an inclusive revision ceiling. Choose greatest eligible revision per (tenant,id); when mirror duplicates have the SAME revision, retain the FIRST encountered record (canonical source precedes replicas). THEN filter tenant and visibility: deleted is always excluded; otherwise public or viewer in readers. Rank by descending snapshot_score, ascending id; current_score is diagnostic only. Cursor [snapshot_score,id] is exclusive, applied to the snapshot rank. Return {items: first limit eligible rows, next: last returned cursor ONLY if another eligible row remains, else null}. Inputs may be unsorted, IDs may repeat across tenants, newer tombstones can hide older rows, and duplicates can differ in ACL/score. Return original row objects without mutation.'
        cases, expected = [], []
        for _ in range(20):
            versions = []
            for tenant in ('a', 'b'):
                for ident in ('one', 'two', 'three', 'four', 'five'):
                    for revision in range(1, 5):
                        row = {'tenant': tenant, 'id': ident, 'revision': revision, 'snapshot_score': rng.randrange(0, 6), 'current_score': rng.randrange(20, 40), 'deleted': rng.choice([False, False, True]), 'public': rng.choice([True, False]), 'readers': ['v'] if rng.choice([True, False]) else []}
                        versions.append(row)
                        if rng.randrange(2) == 0: versions.append(dict(row, snapshot_score=99, deleted=not row['deleted']))
            snapshot = rng.randrange(1, 5)
            cursor = None if rng.randrange(2) else [rng.randrange(0, 6), rng.choice(['one', 'three'])]
            case = [versions, 'a', 'v', snapshot, cursor, size]
            cases.append(case)
            canonical = {}
            for row in versions:
                token = (row['tenant'], row['id'])
                if row['revision'] <= snapshot and (token not in canonical or canonical[token]['revision'] < row['revision']): canonical[token] = row
            ranked = sorted([r for r in canonical.values() if r['tenant'] == 'a' and visible_oracle(r, 'v') and (cursor is None or (-r['snapshot_score'], r['id']) > (-cursor[0], cursor[1]))], key=lambda r: (-r['snapshot_score'], r['id']))
            page = ranked[:size]
            expected.append({'items': page, 'next': [page[-1]['snapshot_score'], page[-1]['id']] if len(ranked) > size else None})
        canonical = {'tenant': 'a', 'id': 'x', 'revision': 1, 'snapshot_score': size, 'current_score': 100, 'deleted': False, 'public': True, 'readers': []}
        cases.append([[canonical, dict(canonical, deleted=True)], 'a', 'v', 1, None, size])
        expected.append({'items': [canonical], 'next': None})
    return _fixture(rng, contract, fixed, bug_file, good, bad, 'feed', cases, expected, tier)


def _civil(rng, tier):
    shift = rng.choice([30, 45, 60, 90, 120])
    if tier == 'easy':
        source = _source('''
            def convert(local_minute, offset):
                return local_minute - offset
        ''')
        good, bad = '    return local_minute - offset', '    return local_minute + offset'
        cases = [[100, shift], [-100, -shift], [0, shift], [250, 0]]
        expected = [local-offset for local, offset in cases]
        contract = 'convert(local_minute, offset) maps absolute local minute to UTC minute. Offset is local minus UTC, so UTC=local-offset. Values may be negative; do not wrap at midnight.'
        return _fixture(rng, contract, {'service.py': source}, 'service.py', good, bad, 'convert', cases, expected, tier)
    if tier == 'medium':
        source = _source('''
            def convert(day, start, end, offset):
                begin = day*1440 + start - offset
                finish = day*1440 + end - offset
                if end <= start:
                    finish += 1440
                return [begin, finish]
        ''')
        good, bad = '    if end <= start:', '    if end < start:'
        cases = [[2, 120, 120, shift], [2, 1400, 80, shift], [0, 0, 120, -shift], [-2, 1300, 10, shift]]
        expected = [[day*1440+start-offset, day*1440+end-offset+(1440 if end <= start else 0)] for day, start, end, offset in cases]
        contract = 'convert(day,start,end,offset) returns half-open UTC [begin,finish) for a local daily shift. day is an absolute integer day index; start and end are minutes 0-1439. An end <= start is on the NEXT day, so equal endpoints mean a full 24-hour shift. Offset is local minus UTC, in minutes; no wrap of absolute UTC minutes.'
        return _fixture(rng, contract, {'service.py': source}, 'service.py', good, bad, 'convert', cases, expected, tier)
    source = _source('''
        def convert(local, initial_offset, transitions, fold):
            candidates = []
            lower, offset = -10000000, initial_offset
            for upper, next_offset in transitions + [[10000000, initial_offset]]:
                candidate = local - offset
                if lower <= candidate < upper:
                    candidates.append(candidate)
                lower, offset = upper, next_offset
            if not candidates:
                return None
            return min(candidates) if fold == 'earliest' else max(candidates)
    ''')
    good, bad = '        if lower <= candidate < upper:', '        if lower <= candidate <= upper:'
    contract = 'convert(local,initial_offset,transitions,fold): transitions are sorted distinct [utc_minute,new_offset] entries; each offset (local minus UTC) applies starting EXACTLY at that UTC minute, until the next transition. Return the UTC minute mapping to local; a forward-clock gap returns null, a backward-clock fold chooses min UTC for fold="earliest" or max for "latest". Inputs and results lie well inside +/-10000000 minutes. Offsets/transitions can be negative. Never accept a candidate from an era at its exclusive end.'
    cases = [[60, 0, [[60, shift]], 'earliest'], [60+shift, shift, [[60, 0]], 'earliest'], [60, shift, [[60, 0]], 'latest'], [61+shift, shift, [[60, 0]], 'earliest'], [-10, 0, [], 'latest']]
    expected = []
    for local, initial, transitions, fold in cases:
        candidates = set()
        for offset in set([initial]+[event[1] for event in transitions]):
            utc = local-offset
            effective = initial
            for at, new in transitions:
                if utc >= at: effective = new
            if utc+effective == local: candidates.add(utc)
        expected.append((min(candidates) if fold == 'earliest' else max(candidates)) if candidates else None)
    if tier == 'hard': return _fixture(rng, contract, {'service.py': source}, 'service.py', good, bad, 'convert', cases, expected, tier)
    civil = _source('''
        def map_window(start, end, initial_offset, transitions):
            pieces = []
            utc_start, offset = -10000000, initial_offset
            for utc_end, following_offset in transitions + [[10000000, initial_offset]]:
                local_start = utc_start + offset
                local_end = utc_end + offset
                low, high = max(start, local_start), min(end, local_end)
                if low < high:
                    pieces.append([low-offset, high-offset])
                utc_start, offset = utc_end, following_offset
            return pieces
    ''')
    calendars = _source('''
        def local_windows(days, weekly, exceptions):
            windows = []
            for day in days:
                shifts = exceptions.get(str(day), weekly.get(str(day % 7), []))
                for start, end in shifts:
                    windows.append([day*1440+start, day*1440+end+(1440 if end <= start else 0)])
            return windows
    ''')
    intervals = _source('''
        def union(rows):
            result = []
            for start, end in sorted(rows):
                if result and start <= result[-1][1]:
                    result[-1][1] = max(result[-1][1], end)
                else:
                    result.append([start, end])
            return result

        def subtract(rows, exclusions):
            for lo, hi in exclusions:
                remaining = []
                for start, end in rows:
                    if hi <= start or lo >= end:
                        remaining.append([start, end])
                    else:
                        if start < lo: remaining.append([start, lo])
                        if hi < end: remaining.append([hi, end])
                rows = remaining
            return rows
    ''')
    main = _source('''
        from civil import map_window
        from calendars import local_windows
        from intervals import union, subtract

        def schedule(days, weekly, exceptions, initial_offset, transitions, blackouts):
            pieces = []
            for start, end in local_windows(days, weekly, exceptions):
                pieces.extend(map_window(start, end, initial_offset, transitions))
            available = union(subtract(union(pieces), union(blackouts)))
            return {'intervals': available, 'minutes': sum(end-start for start, end in available)}
    ''')
    fixed = {'service.py': main, 'civil.py': civil, 'calendars.py': calendars, 'intervals.py': intervals}
    good, bad = '        local_end = utc_end + offset', '        local_end = utc_end + following_offset'
    contract = 'schedule(days,weekly,exceptions,initial_offset,transitions,blackouts): build all local windows for the supplied unique absolute day indices (day0 Monday). weekly maps weekday strings "0".."6" to shifts [start,end], 0-1439; exceptions maps absolute day strings to REPLACEMENT shifts, [] closes that day. end<=start extends into the next day, including equal endpoints=24h. Map each half-open local interval to ALL its UTC realizations: fall-clock folds include both realizations, spring gaps include neither. transitions sorted [UTC minute,new offset] apply at their exact UTC minute; offset=local-UTC. UNION overlapping or touching UTC windows first, subtract half-open UTC blackouts, and return {intervals: sorted maximal nonempty disjoint ranges, minutes: total union duration}. Blackouts may overlap/nest/extend beyond shifts. A local interval crossing a transition must be clipped against each era using THAT era\'s offset at both ends. Helpers have been refactored across four files; calendar overrides and diagnostic refactors are benign.'
    cases, expected = [], []
    for _ in range(16):
        days = [0, 1, 2, 3]
        weekly = {str(day): [[rng.randrange(0, 400), rng.randrange(500, 1000)], [rng.randrange(1000, 1439), rng.randrange(20, 300)]] for day in days}
        exceptions = {'1': []} if rng.randrange(2) else {'2': [[120, 120]]}
        initial = rng.choice([0, shift])
        transitions = [[200, shift if initial == 0 else 0], [3000, 0 if initial == 0 else shift]]
        blackouts = [[rng.randrange(0, 2000), rng.randrange(2100, 4000)], [4500, 4600]]
        cases.append([days, weekly, exceptions, initial, transitions, blackouts])
        local = []
        for day in days:
            for start, end in exceptions.get(str(day), weekly.get(str(day % 7), [])):
                local.append((day*1440+start, day*1440+end+(1440 if end <= start else 0)))
        # Enumerate UTC cells and round-trip their offsets, independent of era clipping.
        points = []
        for utc in range(-500, 6500):
            offset = initial
            for at, new_offset in transitions:
                if at <= utc: offset = new_offset
            if any(start <= utc+offset < end for start, end in local) and not any(start <= utc < end for start, end in blackouts): points.append(utc)
        merged = []
        for point in points:
            if merged and merged[-1][1] == point: merged[-1][1] += 1
            else: merged.append([point, point+1])
        expected.append({'intervals': merged, 'minutes': len(points)})
    cases.append([[0], {'0': [[0, 1000]]}, {}, 0, [[200, shift]], []])
    expected.append({'intervals': [[0, 1000-shift]], 'minutes': 1000-shift})
    return _fixture(rng, contract, fixed, 'civil.py', good, bad, 'schedule', cases, expected, tier)


def _memo(rng, tier):
    price = rng.randrange(10, 90)
    if tier == 'easy':
        source = _source('''
            def prices(queries):
                cache, result = {}, []
                for query in queries:
                    key = (query['item'], query['factor'])
                    if key not in cache:
                        cache[key] = query['price'] * query['factor']
                    result.append(cache[key])
                return result
        ''')
        good, bad = "        key = (query['item'], query['factor'])", "        key = query['item']"
        queries = [{'item': 'book', 'price': price, 'factor': 1}, {'item': 'book', 'price': price, 'factor': 2}, {'item': 'pen', 'price': 5, 'factor': 1}]
        contract = 'prices(queries) memoizes a pure currency conversion: item prices remain fixed during the call, but integer factor can differ per query. Return price*factor for each query in order. Cache identity must distinguish different factors for the same item.'
        return _fixture(rng, contract, {'service.py': source}, 'service.py', good, bad, 'prices', [[queries]], [[q['price']*q['factor'] for q in queries]], tier)
    source = _source('''
        def prices(events):
            catalog, cache, result = {}, {}, []
            for event in events:
                item = event['item']
                if event['op'] == 'publish':
                    catalog[item] = event['price']
                    for key in list(cache):
                        if key[0] == item:
                            del cache[key]
                else:
                    key = (item, event['factor'])
                    if key not in cache:
                        cache[key] = catalog.get(item) * event['factor'] if item in catalog else None
                    result.append(cache[key])
            return result
    ''')
    good, bad = '                if key[0] == item:', '                if key == item:'
    events = [{'op': 'read', 'item': 'book', 'factor': 1}, {'op': 'publish', 'item': 'book', 'price': price}, {'op': 'read', 'item': 'book', 'factor': 1}, {'op': 'read', 'item': 'book', 'factor': 2}, {'op': 'publish', 'item': 'book', 'price': price+3}, {'op': 'read', 'item': 'book', 'factor': 2}]
    cases, expected = [[events]], [[None, price, price*2, (price+3)*2]]
    contract = 'prices(events): publish sets an item price, read returns current price*factor, or null if never published. Cache includes missing-item (negative) results. A publish must invalidate EVERY factor variant of that item, including a prior null, without invalidating unrelated items. Return reads in order.'
    if tier == 'medium': return _fixture(rng, contract, {'service.py': source}, 'service.py', good, bad, 'prices', cases, expected, tier)
    source = _source('''
        def prices(events):
            catalog, cache, requests, result = {}, {}, {}, []
            for event in events:
                action = event['op']
                if action == 'publish':
                    item = event['item']
                    old = catalog.get(item, (0, None))
                    catalog[item] = (old[0]+1, event['price'])
                    cache = {key: value for key, value in cache.items() if key[0] != item}
                elif action == 'begin':
                    item, factor = event['item'], event['factor']
                    version, price = catalog.get(item, (0, None))
                    requests[event['request']] = (item, factor, version, None if price is None else price*factor)
                elif action == 'finish':
                    item, factor, version, value = requests.pop(event['request'])
                    if version == catalog.get(item, (0, None))[0]:
                        cache[(item, factor)] = value
                else:
                    item, factor = event['item'], event['factor']
                    key = (item, factor)
                    if key not in cache:
                        price = catalog.get(item, (0, None))[1]
                        cache[key] = None if price is None else price*factor
                    result.append(cache[key])
            return result
    ''')
    good, bad = '            if version == catalog.get(item, (0, None))[0]:', '            if version <= catalog.get(item, (0, None))[0]:'
    events = [{'op': 'publish', 'item': 'book', 'price': price}, {'op': 'begin', 'request': 'r', 'item': 'book', 'factor': 2}, {'op': 'publish', 'item': 'book', 'price': price+3}, {'op': 'finish', 'request': 'r'}, {'op': 'read', 'item': 'book', 'factor': 2}]
    cases, expected = [[events]], [[(price+3)*2]]
    contract = 'prices(events): publish replaces item price and increments its revision, invalidating all cached factors. begin captures the current revision and converted price (null for missing) under a unique request id. finish completes a pending request exactly once: publish to cache ONLY if the captured revision is STILL current, otherwise discard stale work. read returns current price*factor or null, populating cache on misses. Return reads in order. Requests can finish out of order, and missing-item requests can race first publication.'
    if tier == 'hard': return _fixture(rng, contract, {'service.py': source}, 'service.py', good, bad, 'prices', cases, expected, tier)
    catalog = _source('''
        class Catalog:
            def __init__(self):
                self.rows = {}
                self.epochs = {}

            def version(self, tenant, item):
                return self.rows.get((tenant, item), (0, None))[0]

            def value(self, tenant, item):
                return self.rows.get((tenant, item), (0, None))[1]

            def epoch(self, tenant):
                return self.epochs.get(tenant, 0)

            def apply(self, event):
                tenant, item = event['tenant'], event['item']
                if event['revision'] > self.version(tenant, item):
                    self.rows[(tenant, item)] = (event['revision'], event.get('price'))

            def invalidate(self, tenant):
                self.epochs[tenant] = self.epoch(tenant) + 1
    ''')
    memo = _source('''
        def stamp(catalog, tenant, item):
            return (catalog.epoch(tenant), catalog.version(tenant, item))

        def current(captured, live):
            return captured[0] == live[0] and captured[1] == live[1]

        def lookup(cache, catalog, key):
            tenant, item, factor = key
            entry = cache.get(key)
            if entry is not None and current(entry[0], stamp(catalog, tenant, item)):
                return entry[1]
            price = catalog.value(tenant, item)
            value = None if price is None else price*factor
            cache[key] = (stamp(catalog, tenant, item), value)
            return value
    ''')
    requests = _source('''
        from memo import stamp, current

        def begin(catalog, event):
            key = (event['tenant'], event['item'], event['factor'])
            price = catalog.value(key[0], key[1])
            return (key, stamp(catalog, key[0], key[1]), None if price is None else price*key[2])

        def finish(catalog, cache, request):
            key, captured, value = request
            if current(captured, stamp(catalog, key[0], key[1])):
                cache[key] = (captured, value)
    ''')
    main = _source('''
        from catalog import Catalog
        from memo import lookup
        from requests import begin, finish

        def prices(events):
            catalog, cache, pending, result = Catalog(), {}, {}, []
            for event in events:
                action = event['op']
                if action in ('publish', 'delete'):
                    catalog.apply(event)
                elif action == 'invalidate':
                    catalog.invalidate(event['tenant'])
                elif action == 'begin':
                    pending[event['request']] = begin(catalog, event)
                elif action == 'finish':
                    finish(catalog, cache, pending.pop(event['request']))
                else:
                    result.append(lookup(cache, catalog, (event['tenant'], event['item'], event['factor'])))
            return result
    ''')
    fixed = {'service.py': main, 'catalog.py': catalog, 'memo.py': memo, 'requests.py': requests}
    good, bad = '    return captured[0] == live[0] and captured[1] == live[1]', '    return captured[0] == live[0] or captured[1] == live[1]'
    contract = 'prices(events): an event stream simulates tenant-scoped catalog reads and asynchronous cache fills. publish {tenant,item,revision,price} and delete {tenant,item,revision} accept only revisions GREATER than the current one (initial revision 0). Delete stores a tombstone price=null while retaining its revision; duplicate/out-of-order events are ignored. invalidate {tenant} increments ONLY that tenant\'s epoch, without changing catalog prices. begin {request,tenant,item,factor} captures the current (epoch,revision) and converted value; finish {request} consumes it once and may cache it only if BOTH epoch and revision still match. Cached entries are likewise usable only when BOTH match; negative cached values obey the same rules. read {tenant,item,factor} returns current price*factor or null for missing/tombstoned items, recording reads in order. Keys distinguish tenant,item,factor; request ids are unique. Null is a legitimate cached result, not proof of absence. Simultaneous revision updates and invalidations, duplicate delivery, cross-tenant item names, and delayed negative fills must remain coherent.'
    cases, expected = [], []
    for _ in range(20):
        events, pending, counter = [], [], 0
        for index in range(60):
            action = rng.choice(['publish', 'publish', 'delete', 'invalidate', 'begin', 'finish', 'read', 'read'])
            tenant, item = rng.choice(['a', 'b']), rng.choice(['book', 'pen'])
            if action == 'finish' and not pending: action = 'read'
            if action == 'finish':
                events.append({'op': 'finish', 'request': pending.pop(rng.randrange(len(pending)))})
                continue
            event = {'op': action, 'tenant': tenant}
            if action != 'invalidate': event['item'] = item
            if action in ('publish', 'delete'):
                event['revision'] = rng.randrange(1, 12)
                if action == 'publish': event['price'] = rng.randrange(0, 100)
            elif action in ('read', 'begin'):
                event['factor'] = rng.randrange(1, 4)
                if action == 'begin':
                    event['request'] = 'r%d' % counter
                    counter += 1
                    pending.append(event['request'])
            events.append(event)
        cases.append([events])
        rows, reads = {}, []
        for event in events:
            if event['op'] in ('publish', 'delete'):
                key = (event['tenant'], event['item'])
                if event['revision'] > rows.get(key, (0, None))[0]: rows[key] = (event['revision'], event.get('price'))
            elif event['op'] == 'read':
                value = rows.get((event['tenant'], event['item']), (0, None))[1]
                reads.append(None if value is None else value*event['factor'])
        expected.append(reads)
    race = [{'op': 'publish', 'tenant': 'a', 'item': 'book', 'revision': 1, 'price': price}, {'op': 'begin', 'tenant': 'a', 'item': 'book', 'factor': 2, 'request': 'r'}, {'op': 'publish', 'tenant': 'a', 'item': 'book', 'revision': 2, 'price': price+3}, {'op': 'finish', 'request': 'r'}, {'op': 'read', 'tenant': 'a', 'item': 'book', 'factor': 2}]
    cases.append([race])
    expected.append([(price+3)*2])
    return _fixture(rng, contract, fixed, 'memo.py', good, bad, 'prices', cases, expected, tier)


_RETRY_POLICY = _source('''
    def retryable(code):
        return code == 429 or 500 <= code <= 599

    def due_after(finish, attempt, base_delay, retry_after):
        backoff = finish + base_delay * 2**(attempt-1)
        return max(backoff, retry_after if retry_after is not None else backoff)
''')
_CANCEL = _source('''
    def apply_cancellations(cancellations, index, now, cancelled):
        while index < len(cancellations) and cancellations[index]['time'] <= now:
            cancelled.add(cancellations[index]['id'])
            index += 1
        return index
''')
_RETRY_MAIN = _source('''
    import heapq
    from retry_policy import retryable, due_after
    from cancel import apply_cancellations

    def run(jobs, policy, cancellations):
        by_id = {job['id']: job for job in jobs}
        pending = [(job['release'], job['id'], 1) for job in jobs]
        heapq.heapify(pending)
        running, trace, cancelled, finished = [], [], set(), set()
        cancellations = sorted(cancellations, key=lambda event: (event['time'], event['id']))
        index, now = 0, 0
        while pending or running:
            index = apply_cancellations(cancellations, index, now, cancelled)
            while running and running[0][0] <= now:
                finish, ident, attempt, code, retry_after = heapq.heappop(running)
                job = by_id[ident]
                if ident in cancelled or not retryable(code) or attempt >= policy['max_attempts']:
                    finished.add(ident)
                else:
                    due = due_after(finish, attempt, policy['base_delay'], retry_after)
                    if due < job['deadline']:
                        heapq.heappush(pending, (due, ident, attempt+1))
                    else:
                        finished.add(ident)
            while pending and len(running) < policy['workers'] and pending[0][0] <= now:
                due, ident, attempt = heapq.heappop(pending)
                job = by_id[ident]
                if ident in cancelled or ident in finished or now >= job['deadline']:
                    finished.add(ident)
                    continue
                response = job['responses'][attempt-1]
                finish = now + response['duration']
                trace.append({'id': ident, 'attempt': attempt, 'start': now, 'finish': finish, 'code': response['code']})
                heapq.heappush(running, (finish, ident, attempt, response['code'], response['retry_after']))
            future = [row[0] for row in running]
            if pending and len(running) < policy['workers']:
                future.append(pending[0][0])
            if index < len(cancellations):
                future.append(cancellations[index]['time'])
            future = [time for time in future if time > now]
            if not future:
                break
            now = min(future)
        return trace
''')


def _retry_oracle(jobs, policy, cancellations):
    queue = [(job['release'], job['id'], 1) for job in jobs]
    by_id = {job['id']: job for job in jobs}
    running, cancelled, finished, trace = [], set(), set(), []
    horizon = max([job['deadline'] for job in jobs]+[0])+max([response['duration'] for job in jobs for response in job['responses']]+[1])+1
    for now in range(horizon):
        cancelled.update(event['id'] for event in cancellations if event['time'] == now)
        completed = sorted((item for item in running if item['finish'] == now), key=lambda item: (item['finish'], item['id'], item['attempt']))
        running = [item for item in running if item['finish'] != now]
        for item in completed:
            ident, attempt = item['id'], item['attempt']
            retry = item['code'] in [429]+list(range(500, 600))
            if ident in cancelled or not retry or attempt == policy['max_attempts']:
                finished.add(ident)
            else:
                delay = policy['base_delay']*(2**(attempt-1))
                response = by_id[ident]['responses'][attempt-1]
                due = now+delay
                if response['retry_after'] is not None and response['retry_after'] > due: due = response['retry_after']
                if due < by_id[ident]['deadline']: queue.append((due, ident, attempt+1))
                else: finished.add(ident)
        queue.sort()
        while queue and queue[0][0] <= now and len(running) < policy['workers']:
            _, ident, attempt = queue.pop(0)
            job = by_id[ident]
            if ident in cancelled or ident in finished or now >= job['deadline']:
                finished.add(ident)
                continue
            response = job['responses'][attempt-1]
            item = {'id': ident, 'attempt': attempt, 'start': now, 'finish': now+response['duration'], 'code': response['code']}
            trace.append(item)
            running.append(item)
    return trace


def _retry(rng, tier):
    base = rng.randrange(1, 7)
    if tier == 'easy':
        source = _source('''
            def run(codes, max_attempts):
                used = []
                for code in codes:
                    if len(used) >= max_attempts:
                        break
                    used.append(code)
                    if code != 429 and not 500 <= code <= 599:
                        break
                return used
        ''')
        good, bad = '        if len(used) >= max_attempts:', '        if len(used) > max_attempts:'
        cases = [[[500]*10, base], [[500, 200, 500], 5], [[429, 503, 403], 5]]
        expected = [[500]*base, [500, 200], [429, 503, 403]]
        contract = 'run(codes,max_attempts) returns status codes for performed attempts. max_attempts includes the FIRST attempt, is positive, and is a strict cap. Retry only 429 and 500-599; stop immediately after any other code, including success and permanent failures. Supplied codes are potential responses in attempt order.'
        return _fixture(rng, contract, {'service.py': source}, 'service.py', good, bad, 'run', cases, expected, tier)
    if tier == 'medium':
        source = _source('''
            def run(finish, attempt, base_delay, retry_after):
                backoff = finish + base_delay * 2**(attempt-1)
                return max(backoff, retry_after if retry_after is not None else backoff)
        ''')
        good = '    return max(backoff, retry_after if retry_after is not None else backoff)'
        bad = good.replace('max(', 'min(')
        cases = [[10, 1, base, 100], [10, 3, base, 0], [10, 2, base, None], [0, 1, base, 0]]
        expected = [max(finish+delay*2**(attempt-1), retry_after if retry_after is not None else -1) for finish, attempt, delay, retry_after in cases]
        contract = 'run(finish,attempt,base_delay,retry_after) gives the earliest next attempt start. attempt is the positive number of the attempt just finished. Exponential backoff is finish + base_delay*2**(attempt-1); retry_after is null or an ABSOLUTE not-before timestamp. Honor BOTH constraints: neither a past Retry-After nor a shorter backoff may relax the other.'
        return _fixture(rng, contract, {'service.py': source}, 'service.py', good, bad, 'run', cases, expected, tier)
    source = _source('''
        def run(responses, max_attempts, base_delay, deadline):
            trace, start = [], 0
            for attempt, response in enumerate(responses, 1):
                if attempt > max_attempts or start >= deadline:
                    break
                finish = start+response['duration']
                trace.append({'attempt': attempt, 'start': start, 'finish': finish, 'code': response['code']})
                if response['code'] != 429 and not 500 <= response['code'] <= 599:
                    break
                backoff = finish + base_delay * 2**(attempt-1)
                start = max(backoff, response['retry_after'] if response['retry_after'] is not None else backoff)
            return trace
    ''')
    good = '        backoff = finish + base_delay * 2**(attempt-1)'
    bad = '        backoff = start + base_delay * 2**(attempt-1)'
    contract = 'run(responses,max_attempts,base_delay,deadline): first attempt starts at time 0 if strictly before deadline. Each response has code,duration (positive),retry_after (null or absolute timestamp). Record {attempt,start,finish,code}; finish=start+duration. Retry only 429/500-599, max_attempts includes initial request. Next start is max(previous FINISH + base_delay*2**(attempt-1), Retry-After). Do not start at or after deadline; an already started attempt may finish beyond it. Retry-After in the past does not remove backoff. Return chronological trace.'
    responses = [{'code': 500, 'duration': base+5, 'retry_after': None}, {'code': 429, 'duration': 3, 'retry_after': base+20}, {'code': 200, 'duration': 2, 'retry_after': None}]
    cases = [[responses, 3, base, 100], [responses, 5, base, base+5], [responses, 3, base, 0]]
    expected = []
    for responses, max_attempts, delay, deadline in cases:
        trace, next_start = [], 0
        for i, response in enumerate(responses):
            if i == max_attempts or next_start >= deadline: break
            finish = next_start+response['duration']
            trace.append({'attempt': i+1, 'start': next_start, 'finish': finish, 'code': response['code']})
            if response['code'] not in [429]+list(range(500, 600)): break
            next_start = finish+delay*2**i
            if response['retry_after'] is not None: next_start = max(next_start, response['retry_after'])
        expected.append(trace)
    if tier == 'hard': return _fixture(rng, contract, {'service.py': source}, 'service.py', good, bad, 'run', cases, expected, tier)
    contract = 'run(jobs,policy,cancellations) simulates a worker pool. Jobs have unique string id, nonnegative release, positive deadline, and enough responses {code,duration>=1,retry_after:null or absolute int} for policy.max_attempts. policy has workers>=1, max_attempts>=1 and base_delay>=1. Initially every job is due at release, attempt1. Pending jobs dispatch by (due,id,attempt), at actual current time when a slot is available, only strictly before that job\'s deadline. Running attempts occupy slots until finish, even if cancelled; already-started responses always appear in trace. At EACH timestamp process cancellations FIRST, completions SECOND, dispatch THIRD. Cancelled jobs never start another attempt, including cancellation at exact release/retry/slot-release time. Completions retry only 429/500-599, up to max_attempts; next due=max(finish+base_delay*2**(attempt-1),retry_after). Ignore Retry-After on terminal results. Completion at/after deadline is recorded but cannot lead to a new dispatch. Discard due jobs whose actual start would reach deadline. Return trace of starts {id,attempt,start,finish,code} in dispatch order. Simultaneous events, slot contention, cancellation races, finite deadlines, and backoff must all obey these invariants. All other refactors are benign.'
    fixed = {'service.py': _RETRY_MAIN, 'retry_policy.py': _RETRY_POLICY, 'cancel.py': _CANCEL}
    good = "    while index < len(cancellations) and cancellations[index]['time'] <= now:"
    bad = "    while index < len(cancellations) and cancellations[index]['time'] < now:"
    cases, expected = [], []
    for _ in range(20):
        jobs = []
        for i in range(9):
            jobs.append({'id': chr(97+i), 'release': rng.randrange(0, 20), 'deadline': rng.randrange(30, 100), 'responses': [{'code': rng.choice([200, 400, 429, 500, 503]), 'duration': rng.randrange(1, 12), 'retry_after': rng.choice([None, rng.randrange(0, 80)])} for _ in range(5)]})
        policy = {'workers': rng.randrange(1, 4), 'max_attempts': rng.randrange(1, 6), 'base_delay': base}
        cancellations = [{'id': job['id'], 'time': rng.choice([job['release'], rng.randrange(0, 50)])} for job in rng.sample(jobs, 4)]
        cases.append([jobs, policy, cancellations])
        expected.append(_retry_oracle(jobs, policy, cancellations))
    job = {'id': 'a', 'release': base, 'deadline': 100, 'responses': [{'code': 200, 'duration': 1, 'retry_after': None}]}
    cases.append([[job], {'workers': 1, 'max_attempts': 1, 'base_delay': base}, [{'id': 'a', 'time': base}]])
    expected.append([])
    return _fixture(rng, contract, fixed, 'cancel.py', good, bad, 'run', cases, expected, tier)


def make(rng, template_index, tier):
    if tier not in TIERS:
        raise ValueError('unknown review tier: %s' % tier)
    return (_feed, _civil, _memo, _retry)[template_index % len(TEMPLATE_IDS)](rng, tier)
