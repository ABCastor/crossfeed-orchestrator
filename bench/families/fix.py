"""Seeded repair fixtures with independent behavioral oracles."""
import json
import textwrap

from bench.families.common import coding_fixture

TEMPLATE_IDS = ('pagination', 'interval_merge', 'cache_invalidation', 'recursive_parser')
TIERS = ('easy', 'medium', 'hard', 'expert')


def _source(code):
    return textwrap.dedent(code).lstrip()


def _pack(contract, function, reference, candidate, cases, expected):
    return coding_fixture(contract, function, reference, candidate, cases, expected, 'fix')


def _pagination(rng, tier):
    if tier == 'easy':
        code = _source('''
            def paginate(items, page, size):
                start = page * size
                return items[start:start + size]
        ''')
        cases = [[list(range(rng.randrange(0, 30))), rng.randrange(0, 10), rng.randrange(1, 8)] for _ in range(18)]
        cases += [[[0, 1, 2, 3], 0, 2], [[], 0, 1]]
        expected = [items[p*s:(p+1)*s] for items, p, s in cases]
        return _pack('paginate(items, page, size): page is zero-based and size is positive. Return that slice, including a full final page; beyond the end returns [].', 'paginate', {'solution.py': code}, {'solution.py': code.replace('start + size]', 'start + size - 1]')}, cases, expected)
    if tier == 'medium':
        code = _source('''
            def paginate(rows, page, size, category):
                visible = [row for row in rows if row['category'] == category and not row['deleted']]
                return {'items': visible[page*size:(page+1)*size], 'total': len(visible)}
        ''')
        cases = []
        for _ in range(24):
            rows = [{'id': i, 'category': rng.choice(['a', 'b']), 'deleted': rng.choice([False, False, True])} for i in range(rng.randrange(0, 35))]
            cases.append([rows, rng.randrange(0, 7), rng.randrange(1, 7), rng.choice(['a', 'b', 'missing'])])
        cases.append([[{'id': i, 'category': 'a', 'deleted': i % 2 == 0} for i in range(8)], 0, 2, 'a'])
        expected = []
        for rows, p, s, cat in cases:
            indices = [i for i, row in enumerate(rows) if not row['deleted'] and row['category'] == cat]
            expected.append({'items': [rows[i] for i in indices[p*s:(p+1)*s]], 'total': len(indices)})
        broken = code.replace("visible = [row for row in rows", "visible = [row for row in rows[page*size:(page+1)*size]").replace("visible[page*size:(page+1)*size]", 'visible')
        return _pack('paginate(rows, page, size, category): filter matching category and deleted=False BEFORE slicing the zero-based page; preserve input order. Return {"items": rows on page, "total": count of ALL matching live rows}.', 'paginate', {'solution.py': code}, {'solution.py': broken}, cases, expected)
    prefix = _source('''
        def visible_rows(rows, tenant):
            latest = {}
            for row in rows:
                key = (row['tenant'], row['id'])
                if key not in latest or row['revision'] > latest[key]['revision']:
                    latest[key] = row
            return [row for row in latest.values() if row['tenant'] == tenant and not row['deleted']]
    ''')
    main = _source('''
        def paginate(rows, limit, cursor):
            ordered = sorted(rows, key=lambda row: (row['created'], row['id']))
            eligible = [row for row in ordered if cursor is None or [row['created'], row['id']] > cursor]
            page = eligible[:limit]
            return {'items': page, 'next': [page[-1]['created'], page[-1]['id']] if len(eligible) > limit else None}
    ''')
    function = 'paginate'
    reference = {'solution.py': main}
    candidate = {'solution.py': main.replace("[row['created'], row['id']] > cursor", "row['created'] > cursor[0]")}
    contract = 'paginate(rows, limit, cursor): sort ascending by (created,id), then select rows STRICTLY after cursor [created,id] (or all for None). limit is positive. Return {"items": first limit rows, "next": last returned [created,id] ONLY if another eligible row exists, otherwise null}. IDs are unique.'
    if tier == 'expert':
        main = main.replace('def paginate(rows, limit, cursor):', 'from history import visible_rows\nfrom cursors import eligible_rows\n\ndef paginate(rows, tenant, limit, cursor):').replace("ordered = sorted(rows, key=lambda row: (row['created'], row['id']))", "ordered = sorted(visible_rows(rows, tenant), key=lambda row: (row['created'], row['id']))").replace("eligible = [row for row in ordered if cursor is None or [row['created'], row['id']] > cursor]", 'eligible = eligible_rows(ordered, cursor)')
        cursors = _source('''
            def eligible_rows(rows, cursor):
                return [row for row in rows if cursor is None or [row['created'], row['id']] > cursor]
        ''')
        reference = {'solution.py': main, 'history.py': prefix, 'cursors.py': cursors}
        candidate = {'solution.py': main, 'history.py': prefix.replace("row['revision'] > latest[key]['revision']", "row['revision'] < latest[key]['revision']"), 'cursors.py': cursors.replace(' > cursor', ' >= cursor')}
        contract = 'paginate(rows, tenant, limit, cursor): first choose the highest revision for EACH (tenant,id) across the entire history, then keep this tenant and deleted=False. A tombstone never resurrects an older row. Revisions for a key are distinct, and created may change between revisions. Sort the resulting rows by (created,id); cursor is an exclusive [created,id] boundary, applied after revision resolution. Return {"items": first limit rows, "next": last returned key ONLY if more eligible rows remain, otherwise null}. limit is positive. Return complete original row objects. Resolve defects across history.py and cursors.py as needed.'
    cases = []
    for _ in range(44 if tier == 'expert' else 28):
        rows = []
        for ident in range(rng.randrange(0, 18)):
            for tenant in (['a', 'b'] if tier == 'expert' else ['a']):
                for rev in range(rng.randrange(1, 5) if tier == 'expert' else 1):
                    row = {'id': ident, 'created': rng.randrange(-3, 9)}
                    if tier == 'expert': row.update(tenant=tenant, revision=rev, deleted=rng.choice([False, False, True]))
                    rows.append(row)
        rng.shuffle(rows)
        cursor = None if rng.randrange(3) == 0 else [rng.randrange(-3, 9), rng.randrange(0, 18)]
        cases.append([rows, 'a', rng.randrange(1, 7), cursor] if tier == 'expert' else [rows, rng.randrange(1, 7), cursor])
    simple = [{'id': 0, 'created': 3}, {'id': 1, 'created': 3}, {'id': 2, 'created': 4}]
    if tier == 'expert':
        simple = [dict(row, tenant='a', revision=0, deleted=False) for row in simple]
        simple += [dict(simple[0], revision=1, deleted=True)]
        cases.append([simple, 'a', 1, [3, 0]])
    else: cases.append([simple, 1, [3, 0]])
    expected = []
    for args in cases:
        rows, limit, cursor = (args[0], args[-2], args[-1])
        if tier == 'expert':
            chosen = [r for r in rows if r['tenant'] == args[1] and r['revision'] == max(q['revision'] for q in rows if (q['tenant'], q['id']) == (r['tenant'], r['id'])) and not r['deleted']]
        else: chosen = rows
        chosen = sorted([r for r in chosen if cursor is None or (r['created'], r['id']) > tuple(cursor)], key=lambda r: (r['created'], r['id']))
        page = chosen[:limit]
        expected.append({'items': page, 'next': [page[-1]['created'], page[-1]['id']] if len(chosen) > len(page) else None})
    return _pack(contract, function, reference, candidate, cases, expected)


def _intervals(rng, tier):
    if tier in ('easy', 'medium'):
        code = _source('''
            def merge_intervals(intervals, join_touching=True):
                result = []
                for start, end in sorted(intervals):
                    if result and (start <= result[-1][1] if join_touching else start < result[-1][1]):
                        result[-1][1] = max(result[-1][1], end)
                    else:
                        result.append([start, end])
                return result
        ''')
        cases = []
        for _ in range(24):
            pairs = [[rng.randrange(-8, 15), rng.randrange(1, 12)] for _ in range(rng.randrange(0, 18))]
            intervals = [[a, a+b] for a, b in pairs]
            cases.append([intervals] if tier == 'easy' else [intervals, rng.choice([True, False])])
        cases += [[[[0, 10], [2, 3], [10, 12]]]] if tier == 'easy' else [[[[0, 1], [1, 2]], False], [[[0, 10], [2, 3]], True]]
        expected = []
        for args in cases:
            # Connected components of the overlap graph, independently of the sweep implementation.
            intervals = args[0]
            join = args[1] if len(args) > 1 else True
            pending = set(range(len(intervals)))
            components = []
            while pending:
                component = {pending.pop()}
                while True:
                    adjacent = {i for i in pending if any(max(intervals[i][0], intervals[j][0]) <= min(intervals[i][1], intervals[j][1]) if join else max(intervals[i][0], intervals[j][0]) < min(intervals[i][1], intervals[j][1]) for j in component)}
                    if not adjacent: break
                    component.update(adjacent)
                    pending.difference_update(adjacent)
                components.append([min(intervals[i][0] for i in component), max(intervals[i][1] for i in component)])
            expected.append(sorted(components))
        broken = code.replace('max(result[-1][1], end)', 'end') if tier == 'easy' else code.replace('start < result[-1][1]', 'start <= result[-1][1]')
        contract = 'merge_intervals(intervals): return sorted merged CLOSED integer [start,end] intervals, combining overlaps including shared endpoints. Nested intervals must not shorten an earlier interval. start < end.' if tier == 'easy' else 'merge_intervals(intervals, join_touching): intervals are nonempty half-open integer [start,end) ranges. Return sorted union ranges. Strict overlaps always merge; touching endpoints merge ONLY when join_touching is true. Nested and duplicate ranges must be handled. start < end.'
        return _pack(contract, 'merge_intervals', {'solution.py': code}, {'solution.py': broken}, cases, expected)
    events = _source('''
        def boundaries(windows, blackouts):
            return sorted({v for row in windows + blackouts for v in (row['start'], row['end'])})

        def active_at(rows, start):
            return [row for row in rows if row['start'] <= start < row['end']]
    ''')
    coalesce = _source('''
        def append_segment(result, start, end, labels):
            if result and result[-1]['end'] == start and result[-1]['labels'] == labels:
                result[-1]['end'] = end
            else:
                result.append({'start': start, 'end': end, 'labels': labels})
    ''')
    main = _source('''
        def coverage(windows):
            points = sorted({v for row in windows for v in (row['start'], row['end'])})
            result = []
            for start, end in zip(points, points[1:]):
                labels = sorted({row['label'] for row in windows if row['start'] <= start < row['end']})
                if not labels: continue
                if result and result[-1]['end'] == start and result[-1]['labels'] == labels:
                    result[-1]['end'] = end
                else:
                    result.append({'start': start, 'end': end, 'labels': labels})
            return result
    ''')
    reference = {'solution.py': main}
    candidate = {'solution.py': main.replace("sorted({row['label'] for row in windows if row['start'] <= start < row['end']})", "sorted(row['label'] for row in windows if row['start'] <= start <= row['end'])")}
    contract = 'coverage(windows): each window has integer start < end and string label. Treat ranges as half-open. Return maximal nonempty spans as {"start": int, "end": int, "labels": sorted DISTINCT active labels}. Merge adjacent spans exactly when their label sets match. Repeated windows/labels do not multiply coverage. Omit uncovered gaps.'
    if tier == 'expert':
        main = _source('''
            from boundaries import boundaries, active_at
            from segments import append_segment

            def coverage(windows, blackouts):
                result = []
                points = boundaries(windows, blackouts)
                for start, end in zip(points, points[1:]):
                    active = active_at(windows, start)
                    blocked = {row['label'] for row in active_at(blackouts, start)}
                    active = [row for row in active if row['label'] not in blocked]
                    if not active: continue
                    priority = max(row['priority'] for row in active)
                    labels = sorted({row['label'] for row in active if row['priority'] == priority})
                    append_segment(result, start, end, labels)
                return result
        ''')
        reference = {'solution.py': main, 'boundaries.py': events, 'segments.py': coalesce}
        candidate = {'solution.py': main.replace("active = [row for row in active if row['label'] not in blocked]", "active = [row for row in active if row['priority'] == max(r['priority'] for r in active)] if active else []\n        active = [row for row in active if row['label'] not in blocked]"), 'boundaries.py': events.replace("row['start'] <= start < row['end']", "row['start'] <= start <= row['end']"), 'segments.py': coalesce.replace(" and result[-1]['labels'] == labels", '')}
        contract = 'coverage(windows, blackouts): windows are {start,end,label,priority}; blackouts are {start,end,label}. All integer ranges are half-open and start < end. At each point remove EVERY window whose label has an active blackout, THEN select the greatest priority among surviving windows, including all tied distinct labels sorted lexically. Return maximal nonempty {start,end,labels} spans, coalescing only equal label sets at contiguous endpoints; omit gaps. Blackout-only boundaries, negative priorities, duplicate labels at different priorities, ties, and nested ranges matter. Repair the cooperating modules.'
    cases = []
    for _ in range(48 if tier == 'expert' else 32):
        windows = []
        for _ in range(rng.randrange(0, 24)):
            start = rng.randrange(-10, 20)
            row = {'start': start, 'end': start+rng.randrange(1, 10), 'label': rng.choice(['a', 'b', 'c', 'd'])}
            if tier == 'expert': row['priority'] = rng.randrange(-3, 5)
            windows.append(row)
        if tier == 'expert':
            blackouts = []
            for _ in range(rng.randrange(0, 12)):
                start = rng.randrange(-10, 20)
                blackouts.append({'start': start, 'end': start+rng.randrange(1, 8), 'label': rng.choice(['a', 'b', 'c', 'd'])})
            cases.append([windows, blackouts])
        else: cases.append([windows])
    if tier == 'expert':
        cases += [[ [{'start': 0, 'end': 10, 'label': 'a', 'priority': 9}, {'start': 0, 'end': 10, 'label': 'b', 'priority': 1}], [{'start': 2, 'end': 5, 'label': 'a'}] ], [[], [{'start': 0, 'end': 1, 'label': 'a'}]]]
    else: cases.append([[{'start': 0, 'end': 2, 'label': 'a'}, {'start': 2, 'end': 4, 'label': 'b'}, {'start': 0, 'end': 2, 'label': 'a'}]])
    expected = []
    for args in cases:
        windows, blackouts = args[0], args[1] if tier == 'expert' else []
        result = []
        if windows:
            # Integer-cell oracle avoids sharing boundary/sweep code with the reference.
            for point in range(min(w['start'] for w in windows), max(w['end'] for w in windows)):
                active = [w for w in windows if point in range(w['start'], w['end']) and not any(b['label'] == w['label'] and point in range(b['start'], b['end']) for b in blackouts)]
                if tier == 'expert' and active:
                    active = [w for w in active if all(w['priority'] >= q['priority'] for q in active)]
                labels = sorted(set(w['label'] for w in active))
                if not labels: continue
                if result and result[-1]['end'] == point and result[-1]['labels'] == labels: result[-1]['end'] += 1
                else: result.append({'start': point, 'end': point+1, 'labels': labels})
        expected.append(result)
    return _pack(contract, 'coverage', reference, candidate, cases, expected)


_CACHE_STORE = _source('''
    from collections import OrderedDict

    class Store:
        def __init__(self, capacity):
            self.capacity = capacity
            self.entries = OrderedDict()

        def purge(self, now, epochs):
            for token, entry in list(self.entries.items()):
                value, expiry, weight, generation = entry
                if (expiry is not None and now >= expiry) or generation != epochs.get(token[0]):
                    del self.entries[token]

        def put(self, token, value, expiry, weight, generation):
            self.entries.pop(token, None)
            if weight > self.capacity:
                return
            self.entries[token] = (value, expiry, weight, generation)
            while sum(entry[2] for entry in self.entries.values()) > self.capacity:
                self.entries.popitem(last=False)

        def get(self, token):
            if token not in self.entries:
                return None
            self.entries.move_to_end(token)
            return self.entries[token][0]
''')
_CACHE_EPOCHS = _source('''
    class Epochs:
        def __init__(self):
            self.values = {}

        def get(self, namespace):
            return self.values.get(namespace, 0)

        def invalidate(self, namespace):
            self.values[namespace] = self.get(namespace) + 1
''')
_CACHE_EXPERT = _source('''
    from store import Store
    from epochs import Epochs

    def simulate_cache(capacity, operations):
        store, epochs, reads = Store(capacity), Epochs(), []
        for op in operations:
            now = op['time']
            store.purge(now, epochs)
            namespace = op['namespace']
            if op['op'] == 'invalidate':
                epochs.invalidate(namespace)
                store.purge(now, epochs)
                continue
            token = (namespace, op['key'])
            if op['op'] == 'put':
                ttl = op['ttl']
                store.put(token, op['value'], None if ttl is None else now + ttl,
                          op['weight'], epochs.get(namespace))
                store.purge(now, epochs)
            elif op['op'] == 'get':
                reads.append(store.get(token))
            elif op['op'] == 'delete':
                store.entries.pop(token, None)
        return {'reads': reads, 'keys': [list(token) for token in store.entries]}
''')


def _cache_oracle(capacity, operations, tier):
    # Recency uses numeric timestamps rather than the reference's OrderedDict.
    entries, reads, stamp = {}, [], 0
    for op in operations:
        stamp += 1
        now = op.get('time', 0)
        for token, item in list(entries.items()):
            if item['expires'] is not None and item['expires'] <= now: del entries[token]
        namespace = op.get('namespace', '')
        token = (namespace, op.get('key', ''))
        if op['op'] == 'invalidate':
            entries = {k: v for k, v in entries.items() if k[0] != namespace}
        elif op['op'] == 'put':
            entries.pop(token, None)
            weight = op.get('weight', 1)
            ttl = op.get('ttl')
            if weight <= capacity and (ttl is None or ttl > 0):
                entries[token] = {'value': op['value'], 'expires': None if ttl is None else now+ttl, 'weight': weight, 'stamp': stamp}
                while sum(v['weight'] for v in entries.values()) > capacity:
                    del entries[min(entries, key=lambda k: entries[k]['stamp'])]
        elif op['op'] == 'get':
            reads.append(entries[token]['value'] if token in entries else None)
            if token in entries: entries[token]['stamp'] = stamp
        elif op['op'] == 'delete': entries.pop(token, None)
    if tier in ('easy', 'medium'): return reads
    keys = sorted(entries, key=lambda k: entries[k]['stamp'])
    return {'reads': reads, 'keys': [list(k) for k in keys] if tier == 'expert' else [k[1] for k in keys]}


def _cache(rng, tier):
    code = _source('''
        def simulate_cache(operations):
            entries, reads = {}, []
            for op in operations:
                if op['op'] == 'put': entries[op['key']] = op['value']
                elif op['op'] == 'delete': entries.pop(op['key'], None)
                else: reads.append(entries.get(op['key']))
            return reads
    ''')
    contract = 'simulate_cache(operations): process put {key,value}, get {key}, delete {key} operations in order. Return a list of get results, null for misses. A put replaces a prior value; delete of a missing key is harmless. Keys are strings and values are non-null JSON values.'
    if tier == 'medium':
        code = _source('''
            def simulate_cache(operations):
                entries, reads = {}, []
                for op in operations:
                    now = op['time']
                    entries = {k: v for k, v in entries.items() if v[1] is None or now < v[1]}
                    if op['op'] == 'put':
                        ttl = op['ttl']
                        entries[op['key']] = (op['value'], None if ttl is None else now+ttl)
                    elif op['op'] == 'delete': entries.pop(op['key'], None)
                    else:
                        item = entries.get(op['key'])
                        reads.append(item[0] if item is not None and (item[1] is None or now < item[1]) else None)
                return reads
        ''')
        contract += ' Every operation has nondecreasing integer time. A put has ttl=null for no expiry, or nonnegative integer TTL. A value expires at time >= put.time+ttl, including ttl=0. Replacing resets expiry, and expired entries never return old values.'
    reference, candidate = {'solution.py': code}, {'solution.py': code.replace("entries[op['key']] = op['value']", "entries.setdefault(op['key'], op['value'])")}
    if tier == 'medium': candidate = {'solution.py': code.replace('now < v[1]', 'now <= v[1]').replace('now < item[1]', 'now <= item[1]')}
    if tier in ('hard', 'expert'):
        reference = {'solution.py': _CACHE_EXPERT, 'store.py': _CACHE_STORE, 'epochs.py': _CACHE_EPOCHS}
        candidate = {'solution.py': _CACHE_EXPERT, 'store.py': _CACHE_STORE.replace('self.entries.move_to_end(token)', '# BUG: hits do not refresh recency'), 'epochs.py': _CACHE_EPOCHS}
        contract = 'simulate_cache(capacity, operations): simulate a cache in chronological (nondecreasing integer time) operation order. Before EACH operation purge all expired entries. put carries key, non-null JSON value, ttl (null or nonnegative integer); expiry is inclusive at time+ttl. delete removes a key; get returns its value or null and moves ONLY hits to most-recently-used. A put replaces the old entry, resets TTL and moves it to most-recently-used. Evict least-recently-used live entries until within capacity. Return {"reads": results of gets, "keys": surviving keys from least to most recently used}. capacity >= 0.'
        if tier == 'hard':
            main = _CACHE_EXPERT.replace("namespace = op['namespace']", "namespace = ''").replace("op['weight'], epochs.get(namespace)", '1, epochs.get(namespace)').replace("[list(token) for token in store.entries]", '[token[1] for token in store.entries]')
            # Hard presents one file; expert requires finding cross-module invariants.
            main = main.replace('from store import Store\nfrom epochs import Epochs\n', _CACHE_STORE+'\n'+_CACHE_EPOCHS+'\n')
            reference = {'solution.py': main}
            candidate = {'solution.py': main.replace('self.entries.move_to_end(token)', '# BUG: hits do not refresh recency')}
            contract += ' Each entry costs one unit. An entry with ttl=0 must not survive or evict another live entry. If capacity=0 no entry is stored.'
            # A zero-TTL insert must not evict live entries before its immediate expiry.
            reference['solution.py'] = main.replace('store.put(token,', "if ttl == 0:\n                store.entries.pop(token, None)\n                continue\n            store.put(token,")
            candidate['solution.py'] = reference['solution.py'].replace('self.entries.move_to_end(token)', '# BUG: hits do not refresh recency')
        else:
            contract += ' Expert: every operation has namespace; identity is (namespace,key). put also carries positive integer weight, and capacity limits TOTAL weight. invalidate {namespace,time} removes all entries in ONLY that namespace; future puts work normally. An oversized replacement removes its previous value but is not admitted and does not evict other entries. A ttl=0 replacement similarly removes its previous value without eviction. Return keys as [namespace,key] pairs. Purge expiration and invalidated generations before capacity decisions, even for misses/deletes/invalidates. Repair all cooperating modules.'
            reference['solution.py'] = _CACHE_EXPERT.replace('store.put(token,', "if ttl == 0:\n                store.entries.pop(token, None)\n                continue\n            store.put(token,")
            candidate = {'solution.py': reference['solution.py'], 'store.py': _CACHE_STORE.replace('self.entries.pop(token, None)\n        if weight', 'if weight').replace('now >= expiry', 'now > expiry'), 'epochs.py': _CACHE_EPOCHS.replace('return self.values.get(namespace, 0)', 'return sum(self.values.values())')}
    cases = []
    for _ in range(40 if tier == 'expert' else 26):
        capacity = rng.randrange(0, 9)
        operations, now = [], 0
        for index in range(rng.randrange(15, 90 if tier == 'expert' else 45)):
            now += rng.randrange(0, 3)
            action = rng.choices(['put', 'get', 'delete', 'invalidate'] if tier == 'expert' else ['put', 'get', 'delete'], weights=[5, 6, 2, 2] if tier == 'expert' else [5, 6, 2])[0]
            op = {'op': action, 'key': rng.choice(['a', 'b', 'c', 'd', 'e'])}
            if tier != 'easy': op['time'] = now
            if tier == 'expert': op['namespace'] = rng.choice(['red', 'blue', 'green'])
            if action == 'put':
                op['value'] = rng.choice([index, str(index), False, [index], {'v': index}])
                if tier != 'easy': op['ttl'] = rng.choice([None, 0, 1, 3, 7, 20])
                if tier == 'expert': op['weight'] = rng.randrange(1, 10)
            if action == 'invalidate': del op['key']
            operations.append(op)
        cases.append([capacity, operations] if tier in ('hard', 'expert') else [operations])
    if tier == 'easy':
        cases.append([[{'op': 'put', 'key': 'a', 'value': 1}, {'op': 'put', 'key': 'a', 'value': 2}, {'op': 'get', 'key': 'a'}]])
    elif tier == 'medium':
        cases.append([[{'op': 'put', 'key': 'a', 'value': 1, 'time': 0, 'ttl': 2}, {'op': 'get', 'key': 'a', 'time': 2}]])
    else:
        trace = [{'op': 'put', 'key': 'a', 'value': 1, 'time': 0, 'ttl': None}, {'op': 'put', 'key': 'b', 'value': 2, 'time': 0, 'ttl': None}, {'op': 'get', 'key': 'a', 'time': 0}, {'op': 'put', 'key': 'c', 'value': 3, 'time': 0, 'ttl': None}, {'op': 'get', 'key': 'b', 'time': 0}]
        if tier == 'expert':
            for op in trace:
                op['namespace'] = 'red'
                if op['op'] == 'put': op['weight'] = 1
            trace += [{'op': 'invalidate', 'namespace': 'blue', 'time': 0}, {'op': 'get', 'namespace': 'red', 'key': 'a', 'time': 0}]
        cases.append([2, trace])
    expected = [_cache_oracle(args[0] if len(args) == 2 else 10**9, args[-1], tier) for args in cases]
    return _pack(contract, 'simulate_cache', reference, candidate, cases, expected)


_PARSER_LEXER = _source(r'''
    import json
    import re

    class ParseError(ValueError):
        pass

    def tokenize(text, comments=False):
        tokens, pos = [], 0
        while pos < len(text):
            char = text[pos]
            if char in ' \t\r\n':
                pos += 1
                continue
            if comments and text.startswith('//', pos):
                end = text.find('\n', pos+2)
                pos = len(text) if end == -1 else end+1
                continue
            if comments and text.startswith('/*', pos):
                end = text.find('*/', pos+2)
                if end == -1: raise ParseError('syntax')
                pos = end+2
                continue
            if char in '[]{},:':
                tokens.append((char, char))
                pos += 1
                continue
            if char == '"':
                try:
                    value, consumed = json.JSONDecoder().raw_decode(text[pos:])
                except (ValueError, json.JSONDecodeError):
                    raise ParseError('syntax')
                if not isinstance(value, str): raise ParseError('syntax')
                tokens.append(('value', value))
                pos += consumed
                continue
            number = re.match(r'-?(?:0|[1-9][0-9]*)', text[pos:])
            if number:
                tokens.append(('value', int(number.group())))
                pos += len(number.group())
                continue
            for spelling, value in (('true', True), ('false', False), ('null', None)):
                if text.startswith(spelling, pos):
                    tokens.append(('value', value))
                    pos += len(spelling)
                    break
            else:
                raise ParseError('syntax')
        return tokens
''')
_PARSER_CORE = _source('''
    from lexer import ParseError

    def parse(tokens, trailing=False):
        pos = 0

        def take(kind):
            nonlocal pos
            if pos >= len(tokens) or tokens[pos][0] != kind: raise ParseError('syntax')
            item = tokens[pos][1]
            pos += 1
            return item

        def value():
            nonlocal pos
            if pos >= len(tokens): raise ParseError('syntax')
            kind = tokens[pos][0]
            if kind == 'value': return take('value')
            if kind == '[':
                take('[')
                items = []
                if pos < len(tokens) and tokens[pos][0] == ']':
                    take(']')
                    return items
                while True:
                    items.append(value())
                    if pos < len(tokens) and tokens[pos][0] == ']':
                        take(']')
                        return items
                    take(',')
                    if trailing and pos < len(tokens) and tokens[pos][0] == ']':
                        take(']')
                        return items
            if kind == '{':
                take('{')
                obj = {}
                if pos < len(tokens) and tokens[pos][0] == '}':
                    take('}')
                    return obj
                while True:
                    key = take('value')
                    if type(key) is not str: raise ParseError('syntax')
                    take(':')
                    child = value()
                    if key in obj: raise ParseError('duplicate')
                    obj[key] = child
                    if pos < len(tokens) and tokens[pos][0] == '}':
                        take('}')
                        return obj
                    take(',')
                    if trailing and pos < len(tokens) and tokens[pos][0] == '}':
                        take('}')
                        return obj
            raise ParseError('syntax')

        result = value()
        if pos != len(tokens): raise ParseError('syntax')
        return result
''')
_PARSER_REFS = _source('''
    import re
    from lexer import ParseError

    def resolve(root):
        def lookup(pointer):
            if type(pointer) is not str: raise ParseError('reference')
            if pointer == '': return root
            if not pointer.startswith('/'): raise ParseError('reference')
            current = root
            for raw in pointer[1:].split('/'):
                if re.search(r'~(?:[^01]|$)', raw): raise ParseError('reference')
                part = raw.replace('~1', '/').replace('~0', '~')
                if type(current) is dict and part in current:
                    current = current[part]
                elif type(current) is list and re.fullmatch(r'0|[1-9][0-9]*', part) and int(part) < len(current):
                    current = current[int(part)]
                else: raise ParseError('reference')
            return current

        def visit(node, active):
            if type(node) is dict:
                if set(node) == {'$ref'}:
                    pointer = node['$ref']
                    if type(pointer) is not str or pointer in active: raise ParseError('reference')
                    return visit(lookup(pointer), active | {pointer})
                return {key: visit(value, active) for key, value in node.items()}
            if type(node) is list: return [visit(value, active) for value in node]
            return node
        return visit(root, set())
''')
_PARSER_MAIN = _source('''
    from lexer import tokenize, ParseError
    from syntax import parse

    def parse_document(text):
        try:
            return {'ok': True, 'value': parse(tokenize(text))}
        except ParseError as error:
            return {'ok': False, 'error': str(error)}
''')


def _parse_oracle(text):
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output: raise ValueError('duplicate')
            output[key] = value
        return output
    def bad_number(value):
        raise ValueError('syntax')
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_float=bad_number, parse_constant=bad_number)
        return {'ok': True, 'value': value}
    except ValueError as error:
        return {'ok': False, 'error': 'duplicate' if str(error) == 'duplicate' else 'syntax'}


def _parser(rng, tier):
    reference = {'lexer.py': _PARSER_LEXER, 'syntax.py': _PARSER_CORE, 'solution.py': _PARSER_MAIN}
    contract = 'parse_document(text): parse a JSON document using integer numbers only (no floats/exponents/NaN/Infinity). Return {"ok": true, "value": parsed JSON value} or {"ok": false, "error": "syntax"}. Consume the entire input, permit only JSON whitespace, and reject malformed syntax. Inputs at this tier are flat arrays of nonnegative integers.'
    texts = []
    if tier == 'easy':
        for _ in range(24): texts.append(json.dumps([rng.randrange(0, 1000) for _ in range(rng.randrange(0, 20))]))
        texts += ['[]', '[1]', '[1,2,3]', '[1,]', '[1 2]', '[1] garbage', '']
        candidate = dict(reference, **{'syntax.py': _PARSER_CORE.replace('return items', 'return items[:-1]')})
    else:
        def tree(depth):
            if depth == 0 or rng.randrange(3) == 0:
                return rng.randrange(-1000, 1001) if tier == 'medium' else rng.choice([rng.randrange(-1000, 1001), None, True, False, 'a"b\\c\n', 'url://host/a', 'snow \u2603'])
            if tier == 'medium' or rng.randrange(2): return [tree(depth-1) for _ in range(rng.randrange(0, 5))]
            return {key: tree(depth-1) for key in rng.sample(['a', 'b', 'a/b', '~', '', 'quote"'], rng.randrange(0, 7))}
        for _ in range(30 if tier == 'medium' else 42): texts.append(json.dumps(tree(4 if tier == 'medium' else 6), ensure_ascii=rng.choice([True, False])))
        texts += ['[[-1],2,[]]', '[[[]]]', '[1,]', '[[]', '[1]false', '[01]', '[--1]', '']
        candidate = dict(reference, **{'lexer.py': _PARSER_LEXER.replace('int(number.group())', 'abs(int(number.group()))')})
        contract = contract.replace('Inputs at this tier are flat arrays of nonnegative integers.', 'Inputs at this tier are recursively nested arrays and signed integers, with arbitrary JSON whitespace.')
        if tier in ('hard', 'expert'):
            contract = contract.replace('Inputs at this tier are recursively nested arrays and signed integers, with arbitrary JSON whitespace.', 'Support recursively nested objects, arrays, signed integers, strings with JSON escapes and Unicode, true, false, and null. Duplicate object keys (including equivalent escaped spellings) produce error "duplicate". Keys must be strings.')
            texts += ['{"a":1,"a":2}', '{"a":1,"\\u0061":2}', '{"nested":{"a":0,"a":1}}', '{"x":"\\u2603\\n\\t\\\\\\\""}', '{"x":true,"y":false,"z":null}', '{"a":1,}', '[1.0]', '[1e2]', '[NaN]', '{1:2}', '"unterminated', '[true false]', '{"a":"raw\nline"}']
            candidate = dict(reference, **{'syntax.py': _PARSER_CORE.replace("if key in obj: raise ParseError('duplicate')", '# BUG: duplicate keys silently overwrite')})
    cases = [[text] for text in texts]
    expected = [_parse_oracle(text) for text in texts]
    if tier == 'expert':
        reference['references.py'] = _PARSER_REFS
        reference['solution.py'] = _PARSER_MAIN.replace('from syntax import parse', 'from syntax import parse\nfrom references import resolve').replace('parse(tokenize(text))', 'resolve(parse(tokenize(text, comments=True), trailing=True))')
        candidate = dict(reference)
        candidate['lexer.py'] = _PARSER_LEXER.replace('tokens, pos = [], 0', "text = re.sub(r'//[^\\n]*|/\\*.*?\\*/', '', text, flags=re.S)\n    tokens, pos = [], 0")
        candidate['references.py'] = _PARSER_REFS.replace("raw.replace('~1', '/').replace('~0', '~')", "raw.replace('~0', '~').replace('~1', '/')").replace("re.fullmatch(r'0|[1-9][0-9]*', part)", 'part.isdigit()')
        contract += ' Expert: permit // line comments and /* block comments */ outside strings, plus one trailing comma in nonempty arrays/objects. Comments cannot split tokens. An object with EXACTLY one key "$ref" is replaced by the resolved root JSON Pointer target. Pointer "" means root; otherwise starts with /; decode ~1 to / and ~0 to ~ (in that order), reject other ~ escapes. List indices must be canonical nonnegative decimals (0 or no leading zero). Resolve chains recursively, including targets containing references. Missing/invalid pointers and any reference cycle produce error "reference". Ordinary objects with additional keys keep their $ref field. Parsing/duplicate errors occur before resolution. Root reference cycles, escaped pointer components, cross-branch aliases, and independent repeated references matter. Repair lexer.py, syntax.py, references.py and/or solution.py; never alter tests.'
        # Nonexpert JSON-only cases have the same outcomes, except trailing commas now allowed.
        expected = [_parse_oracle(text) for text in texts]
        for i, text in enumerate(texts):
            if text in ('[1,]', '{"a":1,}'): expected[i] = {'ok': True, 'value': [1] if text.startswith('[') else {'a': 1}}
        extras = [
            ('/* intro */ {"a":[1,2,], "b":true,} // end', {'ok': True, 'value': {'a': [1, 2], 'b': True}}),
            ('{"url":"https://host/x/*not comment*/","v":1}', {'ok': True, 'value': {'url': 'https://host/x/*not comment*/', 'v': 1}}),
            ('[1/* gap */,2,// gap\n3,]', {'ok': True, 'value': [1, 2, 3]}),
            ('[1/*split*/2]', {'ok': False, 'error': 'syntax'}),
            ('[1] /* open', {'ok': False, 'error': 'syntax'}),
            ('[,]', {'ok': False, 'error': 'syntax'}),
            ('{"a":1,"a":2,"b":{"$ref":"/missing"}}', {'ok': False, 'error': 'duplicate'}),
            ('{"x":{"$ref":"/x"}}', {'ok': False, 'error': 'reference'}),
            ('{"x":{"$ref":"/y"},"y":{"$ref":"/x"}}', {'ok': False, 'error': 'reference'}),
            ('{"$ref":""}', {'ok': False, 'error': 'reference'}),
            ('{"x":[1],"y":{"$ref":"/x/00"}}', {'ok': False, 'error': 'reference'}),
            ('{"x":[1],"y":{"$ref":"/x/-1"}}', {'ok': False, 'error': 'reference'}),
            ('{"x":1,"y":{"$ref":"/x/no"}}', {'ok': False, 'error': 'reference'}),
            ('{"y":{"$ref":1}}', {'ok': False, 'error': 'reference'}),
            ('{"y":{"$ref":"/missing"}}', {'ok': False, 'error': 'reference'}),
            ('{"y":{"$ref":"/bad~2key"}}', {'ok': False, 'error': 'reference'}),
            ('{"a":1,"b":{"$ref":"a"}}', {'ok': False, 'error': 'reference'}),
            ('{"$ref":"/missing","other":1}', {'ok': True, 'value': {'$ref': '/missing', 'other': 1}}),
            ('{"~1":8,"/":9,"x":{"$ref":"/~01"}}', {'ok': True, 'value': {'~1': 8, '/': 9, 'x': 8}}),
            ('{"":false,"alias":{"$ref":"/"}}', {'ok': True, 'value': {'': False, 'alias': False}}),
        ]
        for _ in range(24):
            data = [rng.randrange(-100, 101), {'a/b': rng.choice([None, False, 'http://host']), '~key': rng.randrange(100)}]
            doc = {'data': data, 'first': {'$ref': '/data/0'}, 'escaped': {'$ref': '/data/1/a~1b'}, 'chain': {'$ref': '/first'}, 'nested': [{'again': {'$ref': '/data/1/~0key'}}]}
            want = {'data': data, 'first': data[0], 'escaped': data[1]['a/b'], 'chain': data[0], 'nested': [{'again': data[1]['~key']}]}
            extras.append(('/* payload */ '+json.dumps(doc)+' // stop', {'ok': True, 'value': want}))
        for text, want in extras:
            cases.append([text])
            expected.append(want)
    if tier in ('easy', 'medium', 'hard'):
        # One-file lower tiers retain the same real recursive implementation.
        for files in (reference, candidate):
            files['solution.py'] = files['lexer.py']+'\n'+files['syntax.py'].replace('from lexer import ParseError\n', '')+'\n'+files['solution.py'].replace('from lexer import tokenize, ParseError\nfrom syntax import parse\n', '')
            del files['lexer.py'], files['syntax.py']
    return _pack(contract, 'parse_document', reference, candidate, cases, expected)


def make(rng, template_index, tier):
    """Return a deterministic repair fixture for the requested template and tier."""
    if tier not in TIERS:
        raise ValueError('unknown fix tier: %s' % tier)
    builders = (_pagination, _intervals, _cache, _parser)
    return builders[template_index % len(builders)](rng, tier)
