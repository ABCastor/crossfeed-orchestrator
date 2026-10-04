"""Seeded implementation contracts with deterministic, offline reference programs."""
import copy
import json
import textwrap

from bench.families.common import coding_fixture

TEMPLATE_IDS = ('tokenizer', 'ttl-lru', 'graph-plan', 'json-diff')
TIERS = ('easy', 'medium', 'hard', 'expert')

TOKENIZER = r'''
import re

MODE = __MODE__

def tokenize(source):
    tokens, stack = [], []
    i, n = 0, len(source)
    def fail(offset, code):
        return {'tokens': tokens, 'error': {'offset': offset, 'code': code}}
    def emit(kind, start, end):
        tokens.append([kind, source[start:end], start, end])
    operators = ['==', '!=', '<=', '>=', '->', '&&', '||', '**', '//'] if MODE >= 1 else []
    while i < n:
        if source[i] in ' \t\r\n':
            i += 1
            continue
        start = i
        if MODE >= 2 and source.startswith('/*', i):
            depth = 1
            i += 2
            while i < n and depth:
                if source.startswith('/*', i):
                    depth += 1
                    i += 2
                elif source.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                return fail(start, 'unterminated-comment')
            continue
        if MODE >= 2 and source[i] == '#':
            end = source.find('\n', i)
            i = n if end < 0 else end + 1
            continue
        if MODE >= 1 and source[i] in "'\"":
            quote = source[i]
            i += 1
            while i < n and source[i] != quote:
                if source[i] in '\r\n':
                    return fail(start, 'unterminated-string')
                if source[i] == '\\':
                    i += 1
                    if i == n or source[i] in '\r\n':
                        return fail(start, 'unterminated-string')
                i += 1
            if i == n:
                return fail(start, 'unterminated-string')
            i += 1
            emit('STRING', start, i)
            continue
        match = re.match(r'[A-Za-z_][A-Za-z0-9_]*', source[i:])
        if match:
            i += len(match.group())
            emit('IDENT', start, i)
            continue
        if source[i] in '0123456789':
            if MODE == 3:
                # Consume the maximal number-shaped run, including exponent signs.
                i += 1
                while i < n:
                    ch = source[i]
                    if ch.isascii() and (ch.isalnum() or ch in '_.'):
                        i += 1
                    elif ch in '+-' and source[i-1] in 'eE':
                        i += 1
                    else:
                        break
                literal = source[start:i]
                digit = r'[0-9](?:_?[0-9])*'
                decimal = digit + r'(?:\.' + digit + r')?(?:[eE][+-]?' + digit + r')?'
                bases = r'0[xX][0-9a-fA-F](?:_?[0-9a-fA-F])*|0[bB][01](?:_?[01])*|0[oO][0-7](?:_?[0-7])*'
                if not re.fullmatch('(?:' + bases + '|' + decimal + ')', literal):
                    return fail(start, 'invalid-number')
            else:
                pattern = r'[0-9]+(?:\.[0-9]+)?' if MODE >= 1 else r'[0-9]+'
                i += len(re.match(pattern, source[i:]).group())
            emit('NUMBER', start, i)
            continue
        operator = next((op for op in operators if source.startswith(op, i)), None)
        if operator:
            i += len(operator)
            emit('OP', start, i)
            continue
        if source[i] in '()+-*/=,:[]{}.;<>!':
            ch = source[i]
            if MODE == 3:
                if ch in '([{':
                    stack.append((ch, i))
                elif ch in ')]}':
                    if not stack or '([{'.index(stack[-1][0]) != ')]}'.index(ch):
                        return fail(i, 'mismatched-delimiter')
                    stack.pop()
            i += 1
            emit('OP', start, i)
            continue
        return fail(i, 'unexpected-character')
    if MODE == 3 and stack:
        return fail(stack[-1][1], 'unclosed-delimiter')
    return {'tokens': tokens, 'error': None}
'''

CACHE = r'''
import copy

MODE = __MODE__

def trace_cache(capacity, events):
    cache, order, outputs = {}, [], []
    limit = capacity
    def used():
        return sum(item['weight'] for item in cache.values())
    def remove(key):
        if key in cache:
            del cache[key]
            order.remove(key)
    def apply(event, now):
        nonlocal limit
        op = event['op']
        if op == 'batch':
            old_cache, old_order, old_limit = copy.deepcopy(cache), list(order), limit
            values = []
            for child in event['events']:
                ok, value = apply(child, now)
                if not ok:
                    cache.clear()
                    cache.update(old_cache)
                    order[:] = old_order
                    limit = old_limit
                    return False, {'accepted': False, 'values': []}
                values.append(value)
            return True, {'accepted': True, 'values': values}
        if op in ('get', 'peek'):
            key = event['key']
            if key not in cache:
                return True, None
            if op == 'get':
                order.remove(key)
                order.append(key)
                if MODE == 3 and cache[key]['sliding'] and cache[key]['ttl'] is not None:
                    cache[key]['expires'] = now + cache[key]['ttl']
            return True, copy.deepcopy(cache[key]['value'])
        if op == 'remove':
            exists = event['key'] in cache
            remove(event['key'])
            return True, exists
        if op == 'resize':
            limit = event['capacity']
            while used() > limit:
                remove(order[0])
            return True, True
        key = event['key']
        weight = event.get('weight', 1) if MODE >= 2 else 1
        # Reject before removing the old value or evicting another entry.
        if weight > limit:
            return False, False
        remove(key)
        ttl = event.get('ttl') if MODE >= 1 else None
        if ttl is not None and ttl == 0:
            return True, True
        while used() + weight > limit:
            remove(order[0])
        cache[key] = {'value': copy.deepcopy(event['value']), 'weight': weight,
                      'expires': None if ttl is None else now + ttl,
                      'ttl': ttl, 'sliding': event.get('sliding', False) if MODE == 3 else False}
        order.append(key)
        return True, True
    for event in events:
        now = event.get('at', 0)
        for key in list(order):
            expires = cache[key]['expires']
            if expires is not None and expires <= now:
                remove(key)
        _, value = apply(event, now)
        outputs.append({'result': value, 'keys': list(order), 'used': used()})
    return outputs
'''

GRAPH = r'''
from collections import deque
import heapq

MODE = __MODE__

def plan_graph(nodes, edges):
    nodes = sorted(nodes)
    adjacent = {node: set() for node in nodes}
    for a, b in edges:
        adjacent[a].add(b)
    indegree = {node: 0 for node in nodes}
    for targets in adjacent.values():
        for target in targets:
            indegree[target] += 1
    ready = [node for node in nodes if not indegree[node]]
    heapq.heapify(ready)
    order = []
    degree = dict(indegree)
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for child in sorted(adjacent[node]):
            degree[child] -= 1
            if degree[child] == 0:
                heapq.heappush(ready, child)
    answer = {'order': order, 'acyclic': len(order) == len(nodes)}
    if MODE == 0:
        return answer
    state, path, cycle = {}, [], []
    def visit(node):
        nonlocal cycle
        state[node] = 1
        path.append(node)
        for child in sorted(adjacent[node]):
            if state.get(child, 0) == 0:
                if visit(child):
                    return True
            elif state[child] == 1:
                cycle = path[path.index(child):] + [child]
                return True
        path.pop()
        state[node] = 2
        return False
    for node in nodes:
        if not state.get(node) and visit(node):
            break
    answer['cycle'] = cycle
    if MODE == 1:
        return answer
    def reach(start, omit=None):
        seen, todo = set(), [start]
        while todo:
            node = todo.pop()
            for child in adjacent[node]:
                if (node, child) == omit:
                    continue
                if child not in seen:
                    seen.add(child)
                    todo.append(child)
        return seen
    reachable = {node: reach(node) for node in nodes}
    components, assigned = [], set()
    for node in nodes:
        if node not in assigned:
            component = sorted(n for n in nodes if n == node or
                               (n in reachable[node] and node in reachable[n]))
            assigned.update(component)
            if len(component) > 1 or node in adjacent[node]:
                components.append(component)
    answer['cyclic_components'] = sorted(components)
    degree = dict(indegree)
    frontier = sorted(node for node in nodes if not degree[node])
    layers = []
    while frontier:
        layers.append(frontier)
        following = []
        for node in frontier:
            for child in sorted(adjacent[node]):
                degree[child] -= 1
                if degree[child] == 0:
                    following.append(child)
        frontier = sorted(following)
    answer['layers'] = layers
    if MODE == 2:
        return answer
    candidates = []
    for start in nodes:
        # BFS with sorted neighbors discovers the lexicographically first
        # shortest path from each start to each vertex.
        queue = deque([[start]])
        seen = {start}
        while queue:
            chain = queue.popleft()
            for child in sorted(adjacent[chain[-1]]):
                if child == start:
                    body = chain
                    smallest = min(range(len(body)), key=lambda k: body[k])
                    rotated = body[smallest:] + body[:smallest]
                    candidates.append(rotated + [rotated[0]])
                elif child not in seen:
                    seen.add(child)
                    queue.append(chain + [child])
    answer['cycle'] = min(candidates, key=lambda c: (len(c), c)) if candidates else []
    answer['reduction'] = (sorted([a, b] for a in nodes for b in adjacent[a]
                                  if b not in reach(a, (a, b))) if answer['acyclic'] else [])
    return answer
'''

DIFF = r'''
import copy

MODE = __MODE__

def json_diff(before, after):
    changes = []
    def equal(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
        if isinstance(a, list):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        return a == b
    def pointer(path, key):
        return path + '/' + str(key).replace('~', '~0').replace('/', '~1')
    def emit(op, path, value=None, source=None):
        item = {'op': op, 'path': path}
        if op in ('add', 'replace'):
            item['value'] = copy.deepcopy(value)
        if op == 'move':
            item['from'] = source
        changes.append(item)
    def keyed(items):
        return all(isinstance(x, dict) and type(x.get('id')) is str for x in items) and len({x['id'] for x in items}) == len(items)
    def block(a, b, path, offset):
        common = min(len(a), len(b))
        for i in range(common):
            walk(a[i], b[i], pointer(path, offset+i))
        for i in range(len(a)-1, len(b)-1, -1):
            emit('remove', pointer(path, offset+i))
        for i in range(len(a), len(b)):
            emit('add', pointer(path, offset+i), b[i])
    def walk(a, b, path):
        if equal(a, b):
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() - b.keys()):
                emit('remove', pointer(path, key))
            for key in sorted(a.keys() & b.keys()):
                walk(a[key], b[key], pointer(path, key))
            for key in sorted(b.keys() - a.keys()):
                emit('add', pointer(path, key), b[key])
        elif isinstance(a, list) and isinstance(b, list) and MODE >= 1:
            if MODE >= 2 and keyed(a) and keyed(b):
                current = copy.deepcopy(a)
                for i, item in enumerate(b):
                    index = next((j for j in range(i, len(current)) if current[j]['id'] == item['id']), None)
                    if index is None:
                        emit('add', pointer(path, i), item)
                        current.insert(i, copy.deepcopy(item))
                    else:
                        if index != i:
                            emit('move', pointer(path, i), source=pointer(path, index))
                            current.insert(i, current.pop(index))
                        walk(current[i], item, pointer(path, i))
                        current[i] = copy.deepcopy(item)
                for i in range(len(current)-1, len(b)-1, -1):
                    emit('remove', pointer(path, i))
            elif MODE == 3:
                n, m = len(a), len(b)
                lengths = [[0]*(m+1) for _ in range(n+1)]
                for i in range(n-1, -1, -1):
                    for j in range(m-1, -1, -1):
                        lengths[i][j] = 1 + lengths[i+1][j+1] if equal(a[i], b[j]) else max(lengths[i+1][j], lengths[i][j+1])
                anchors, i, j = [], 0, 0
                while i < n and j < m:
                    if equal(a[i], b[j]):
                        anchors.append((i, j))
                        i, j = i+1, j+1
                    elif lengths[i+1][j] >= lengths[i][j+1]:
                        i += 1
                    else:
                        j += 1
                previous_i = previous_j = 0
                for i, j in anchors + [(n, m)]:
                    block(a[previous_i:i], b[previous_j:j], path, previous_j)
                    previous_i, previous_j = i+1, j+1
            else:
                block(a, b, path, 0)
        else:
            emit('replace', path, b)
    walk(before, after, '')
    return changes
'''


def _package(contract, function, source, cases, tier):
    source = textwrap.dedent(source).lstrip().replace('__MODE__', str(TIERS.index(tier)))
    namespace = {}
    exec(source, namespace)
    expected = [namespace[function](*copy.deepcopy(args)) for args in cases]
    # Visible examples provide seeded worker inputs without hidden expected values.
    workspace = {'solution.py': 'def %s(%s):\n    raise NotImplementedError("Implement the contract")\n' %
                 (function, {'tokenize': 'source', 'trace_cache': 'capacity, events',
                             'plan_graph': 'nodes, edges', 'json_diff': 'before, after'}[function]),
                 'examples.json': json.dumps(cases[:3], indent=2, ensure_ascii=False) + '\n'}
    reference = dict(workspace, **{'solution.py': source})
    fixture = coding_fixture(contract, function, reference, workspace, cases, expected, 'implement')
    if function == 'json_diff':
        fixture[2]['output_copies'] = True
    return fixture


def _tokenizer(rng, tier):
    mode = TIERS.index(tier)
    word = 'item_' + str(rng.randrange(10000, 99999))
    number = rng.randrange(10, 999)
    contract = """Implement tokenize(source) in solution.py. Return {'tokens': [...], 'error': null} on success;
otherwise return partial tokens and error {'offset': integer, 'code': string}. Each token is
[kind, exact_source_text, start, end], with zero-based Python string offsets, exclusive end.
Ignore only space, tab, CR and LF. IDENT is ASCII [A-Za-z_][A-Za-z0-9_]*, NUMBER is digits.
Every character in ()+-*/=,:[]{}.;<>! is one OP token. Use maximal identifiers/numbers;
all other characters stop scanning with unexpected-character at that character.
"""
    cases = [[word + ' = ' + str(number) + ' + (3*4)'], ['  ' + word + '_b,17'], [''],
             ['42.5'], ['a@b'], ['é'], ['0007'], ['1true']]
    if mode >= 1:
        contract += """NUMBER now allows a decimal fraction only when a dot is followed by digits (12.5,
but 12. is NUMBER then OP). Before single OP characters match == != <= >= -> && || ** //.
Single or double quoted STRING tokens preserve quotes and escapes without decoding.
A backslash consumes exactly the next non-newline character; matching quotes close strings.
EOF, a CR/LF, or backslash followed by EOF/CR/LF before closure gives unterminated-string
at the opening quote. Strings may contain Unicode. Other tokens are unchanged.
"""
        cases += [[word + '->"hello \\"' + word + '" != 2.50'], ["'it\\'s' == \"雪\""],
                  ['"broken'], ['"line\nnext"'], ['"escape\\'], ['1.2.3 // 4 ** 2 && x || y']]
    if mode >= 2:
        contract += """Outside strings, # starts a comment through LF/EOF; /* starts a block comment
with arbitrary nested /* ... */ pairs. Ignore all comment contents, including newlines.
An unclosed block comment gives unterminated-comment at its outermost opening slash.
Comments take precedence over OP; // remains an operator, not a comment.
"""
        cases += [[word + '/* outer /* inner */ done */+8# tail\nnext'],
                  ['"/* # */" /* ignored */'], ['1 /* a /* b */'], ['# no tokens'],
                  ['a/**/b'], ['a/*x\ny*/b'], ['/* a */ /* b */42']]
    if mode == 3:
        contract += """NUMBER gains decimal exponents and bases 0x/0X hex, 0b/0B binary, 0o/0O octal.
A digit run may have single underscores strictly between valid digits. Decimal forms are
D, D.D, D[eE][+-]?D, or D.D[eE][+-]?D where D is decimal digits with such underscores.
The exponent sign is optional; 1e2 and 1e+2 are both valid.
Base prefixes require one or more digits valid for that base, with the same underscore rule.
Scan a maximal number-shaped run: ASCII alphanumeric characters, underscores and dots,
plus + or - only when immediately preceded by e/E. Validate the ENTIRE run; failures give
invalid-number at its first digit. In particular 1name, 1., 1.2.3, 0x, 0b102 and 1__2 fail.
Outside comments/strings, track opening (, [ and { delimiters. A closing delimiter must
match the top opener, else mismatched-delimiter at that closer WITHOUT emitting it.
At EOF report unclosed-delimiter at the most recent outstanding opener. Errors encountered
while scanning take precedence over the EOF delimiter check. Delimiter tokens are OP.
"""
        cases += [['[' + word + ',0xA_f,0b10_01,0o7_1,12_34.5_6e-2]'],
                  ['(a[)]'], ['(a[1]'], ['0x'], ['1__2'], ['0b102'], ['1e+'], ['1E-2+3'],
                  ['["}", /* ] */ 0XFACE]'], ['([@'], ['1e2-3'], ['0xEe+1']]
        fragments = ['0xA_f', '0b10_01', '1_234.5_6e-2', '"bracket ]"', word]
        cases.append(['[' + ', /* level /* nested */ done */ '.join(rng.choice(fragments) for _ in range(70)) + ']'])
    return _package(contract, 'tokenize', TOKENIZER, cases, tier)


def _cache(rng, tier):
    mode = TIERS.index(tier)
    keys = ['key_' + str(rng.randrange(10000, 99999)) for _ in range(4)]
    a, b, c, d = keys
    value = rng.randrange(100, 999)
    contract = """Implement trace_cache(capacity, events) in solution.py. Simulate a least-recently-used
cache and return one {'result': ..., 'keys': [...], 'used': integer} for each event.
keys are live keys from least to most recently used; used is the total live weight.
Inputs are valid: capacity is a nonnegative integer, keys are strings, values are JSON.
Initially empty. put has key,value: replace any old entry, make it most recent, and evict
least-recent entries until it fits; unit weight initially. A zero-capacity put is rejected
and MUST leave existing state untouched. put result is true on acceptance, false otherwise.
get has key: return its value or null for a miss; a hit becomes most recent, a miss does
nothing. remove has key: remove it and return true if present, false if missing.
"""
    cases = [[2, [{'op': 'put', 'key': a, 'value': value}, {'op': 'put', 'key': b, 'value': False},
                  {'op': 'get', 'key': a}, {'op': 'put', 'key': c, 'value': {'v': [1, None]}},
                  {'op': 'get', 'key': b}, {'op': 'remove', 'key': a}, {'op': 'remove', 'key': a}]],
             [1, [{'op': 'put', 'key': b, 'value': 0}, {'op': 'put', 'key': b, 'value': None}, {'op': 'get', 'key': b}]],
             [0, [{'op': 'put', 'key': a, 'value': value}, {'op': 'get', 'key': a}]], [3, []]]
    if mode >= 1:
        contract += """Events have integer at timestamps, nondecreasing (equal times allowed). Immediately
BEFORE every top-level event remove ALL entries whose expires <= at. put may contain ttl,
a nonnegative integer; expiry is at+ttl, omitted ttl means no expiry. ttl=0 accepts but
removes an old entry of that key and stores nothing; perform capacity rejection first.
Expired entries never evict live entries or affect recency; expiration gives no extra output.
"""
        cases += [[2, [{'op': 'put', 'at': 0, 'key': a, 'value': value, 'ttl': 4},
                       {'op': 'put', 'at': 1, 'key': b, 'value': 'forever'},
                       {'op': 'get', 'at': 3, 'key': a}, {'op': 'get', 'at': 4, 'key': a},
                       {'op': 'put', 'at': 4, 'key': b, 'value': 'gone', 'ttl': 0},
                       {'op': 'get', 'at': 4, 'key': b}]],
                  [2, [{'op': 'put', 'at': 2, 'key': a, 'value': value, 'ttl': 2},
                       {'op': 'put', 'at': 2, 'key': b, 'value': 7, 'ttl': 2},
                       {'op': 'put', 'at': 4, 'key': c, 'value': 8}]]]
    if mode >= 2:
        contract += """Capacity now counts weight units. put may specify positive integer weight (default 1).
An item with weight > current capacity is rejected atomically: do not remove its old value,
change recency, or evict anything. Otherwise remove the old entry BEFORE calculating required
evictions. peek returns a value without changing recency. resize has capacity (nonnegative),
sets capacity and evicts oldest until used <= capacity; result is true.
"""
        cases += [[5, [{'op': 'put', 'at': 0, 'key': a, 'value': value, 'weight': 3},
                       {'op': 'put', 'at': 0, 'key': b, 'value': False, 'weight': 2},
                       {'op': 'put', 'at': 1, 'key': a, 'value': 999, 'weight': 6},
                       {'op': 'peek', 'at': 1, 'key': a},
                       {'op': 'put', 'at': 1, 'key': a, 'value': value+1, 'weight': 1},
                       {'op': 'resize', 'at': 2, 'capacity': 1},
                       {'op': 'resize', 'at': 3, 'capacity': 0}]],
                  [3, [{'op': 'put', 'at': 0, 'key': a, 'value': 1, 'weight': 2},
                       {'op': 'put', 'at': 0, 'key': b, 'value': 2, 'weight': 1},
                       {'op': 'put', 'at': 1, 'key': c, 'value': 3, 'weight': 3}]]]
    if mode == 3:
        contract += """put may specify sliding:true (default false). A get HIT for a sliding item with finite
ttl resets expires to at+the original ttl; peek does not refresh. batch has events, a list of
non-batch operations with no at field, evaluated in order at the batch timestamp. On ANY
rejected put, roll back EVERY child change, including capacity, eviction, recency and refreshed
expiry. Return {'accepted':false,'values':[]} for failure; otherwise {'accepted':true,'values':
[each child result]}. No extra outputs for children. The pre-batch expiration sweep happens
before the snapshot and is NEVER rolled back. Empty batches succeed. No intra-batch sweep
is needed: ttl=0 stores nothing and all children use the same timestamp.
"""
        cases += [[3, [{'op': 'put', 'at': 0, 'key': a, 'value': value, 'ttl': 5, 'sliding': True},
                       {'op': 'put', 'at': 0, 'key': b, 'value': 2, 'ttl': 2},
                       {'op': 'batch', 'at': 3, 'events': [{'op': 'get', 'key': a},
                           {'op': 'resize', 'capacity': 1}, {'op': 'put', 'key': c, 'value': 3, 'weight': 2}]},
                       {'op': 'get', 'at': 5, 'key': a}]],
                  [3, [{'op': 'put', 'at': 0, 'key': a, 'value': 1, 'ttl': 3, 'sliding': True},
                       {'op': 'batch', 'at': 2, 'events': [{'op': 'get', 'key': a},
                           {'op': 'put', 'key': b, 'value': {'copy': [False, 0]}, 'weight': 2}]},
                       {'op': 'peek', 'at': 4, 'key': a}, {'op': 'get', 'at': 5, 'key': a},
                       {'op': 'batch', 'at': 5, 'events': []}]]]
        events = []
        for at in range(80):
            key = rng.choice(keys)
            if at % 7 == 0:
                events.append({'op': 'batch', 'at': at, 'events': [
                    {'op': 'get', 'key': key}, {'op': 'resize', 'capacity': rng.randrange(1, 8)},
                    {'op': 'put', 'key': d, 'value': {'at': at}, 'weight': rng.randrange(1, 10)}]})
            elif at % 3:
                events.append({'op': 'put', 'at': at, 'key': key, 'value': [at, False],
                               'weight': rng.randrange(1, 5), 'ttl': rng.randrange(0, 12), 'sliding': bool(at % 2)})
            else:
                events.append({'op': rng.choice(['get', 'peek', 'remove']), 'at': at, 'key': key})
        cases.append([6, events])
    if mode >= 1:
        for _, events in cases:
            for event in events:
                event.setdefault('at', 0)
    return _package(contract, 'trace_cache', CACHE, cases, tier)


def _graph(rng, tier):
    mode = TIERS.index(tier)
    prefix = 'job_' + str(rng.randrange(10000, 99999)) + '_'
    a, b, c, d, e, f = [prefix + ch for ch in 'abcdef']
    contract = """Implement plan_graph(nodes, edges) in solution.py. nodes is a list of unique string labels;
edges contains [source,target] directed prerequisites, with both endpoints in nodes. Ignore
duplicate edges. Self-loops are valid. Return {'order': [...], 'acyclic': boolean}.
Compute order by Kahn's algorithm: repeatedly take the lexicographically smallest currently
zero-indegree node, emit it and remove its outgoing edges. Return this exact emitted prefix,
even for a cyclic graph; acyclic is true exactly when all nodes were emitted. Input order is
irrelevant. An empty graph is acyclic. All label sorting uses Python string ordering.
"""
    cases = [[[c, a, b], [[a, c], [b, c], [a, c]]],
             [[d, b, c, a], [[a, b], [a, c], [c, d]]], [[], []],
             [[a, b, c], [[a, b], [b, a]]], [[a], [[a, a]]], [[c, a, b], []]]
    if mode >= 1:
        contract += """Also return cycle: [] for no cycle, otherwise a closed directed cycle [v0,...,v0].
For this tier choose the FIRST back edge discovered by recursive depth-first search over
all nodes in sorted order and each node's distinct outgoing neighbors in sorted order.
White/unvisited nodes recurse, gray nodes are on the current DFS stack, black nodes finished.
The witness is the stack suffix beginning at the gray target, followed by that target again.
Search the whole graph, not just the Kahn prefix; stop immediately on the first back edge.
"""
        cases += [[[f, e, d, c, b, a], [[a, b], [b, c], [c, b], [d, e], [e, d], [f, a]]],
                  [[a, b, c], [[a, b], [b, c], [c, a]]]]
    if mode >= 2:
        contract += """Also return cyclic_components and layers. A strongly connected component is a maximal
set of mutually reachable nodes. Include only components of size >1 or singletons with a
self-loop. Sort members of each component, then sort component lists lexicographically.
layers is a round-based Kahn traversal: each round emits ALL zero-indegree nodes in sorted
order, removes their outgoing edges together, then starts the next round. Omit blocked nodes
and empty rounds. layers may flatten to a different order than the heap-based order field.
"""
        cases += [[[a, b, c, d, e, f], [[a, b], [b, a], [b, c], [c, d], [d, c], [e, e], [f, b]]],
                  [[a, b, c, d, e], [[a, b], [b, d], [c, e]]]]
    if mode == 3:
        contract += """REPLACE the DFS cycle selection above: return a directed cycle with the FEWEST edges.
Canonicalize each candidate by rotating its body so its smallest label comes first (keep
the direction), then repeat the first label to close it. Among shortest candidates return
the lexicographically smallest canonical list. A self-loop is [v,v] and beats longer cycles.
Also return reduction: for an acyclic graph, sorted [source,target] edges of its transitive
reduction. Keep an edge iff no other directed path connects its endpoints after that edge
is removed. Deduplicate and sort by source then target. For any cyclic graph reduction is [].
"""
        cases += [[[a, b, c, d, e, f], [[a, b], [b, c], [c, a], [d, e], [e, d], [f, f]]],
                  [[a, b, c, d, e], [[a, b], [b, c], [c, a], [a, d], [d, a], [a, e], [e, a]]],
                  [[a, b, c, d], [[a, b], [a, c], [b, c], [c, d], [a, d], [b, d], [a, b]]]]
        nodes = [prefix + str(i).zfill(2) for i in range(24)]
        edges = [[nodes[i], nodes[j]] for i in range(len(nodes)) for j in range(i+1, len(nodes))
                 if rng.random() < 0.21]
        cases.append([rng.sample(nodes, len(nodes)), rng.sample(edges, len(edges))])
        cyclic = copy.deepcopy(edges) + [[nodes[15], nodes[8]], [nodes[8], nodes[15]],
                                        [nodes[5], nodes[6]], [nodes[6], nodes[5]]]
        cases.append([nodes, cyclic])
    return _package(contract, 'plan_graph', GRAPH, cases, tier)


def _diff(rng, tier):
    mode = TIERS.index(tier)
    key = 'field_' + str(rng.randrange(10000, 99999))
    value = rng.randrange(100, 999)
    contract = """Implement json_diff(before, after) in solution.py. Values are finite JSON: objects have
string keys, arrays, strings, booleans, null, integers and floats. Return an ordered list
of patch operations transforming before into after. Each is {'op': 'remove', 'path': P},
{'op': 'add'/'replace', 'path': P, 'value': V}; later tiers also permit move.
P is a JSON Pointer: root is '', append '/' + key/index at each level. Escape ~ as ~0 then
/ as ~1 in object keys; indices are decimal without leading zeros. Output values must be
copies. Equality is recursive and TYPE-SENSITIVE (true != 1, 1 != 1.0); object key order does
not affect equality. If equal emit nothing. If both are objects: first remove old-only keys
in sorted order, then recursively diff shared keys in sorted order, then add new-only keys
in sorted order. Otherwise emit one replace at this path. Arrays are atomic for this tier.
Operations apply sequentially, and their ordering is part of the contract.
"""
    cases = [[{key: value, 'old': None}, {key: value+1, 'new': False}],
             [{'a/b': {'~x': [1, True]}, '': 1}, {'a/b': {'~x': [1, False]}, '': 1.0}],
             [None, {key: []}], [True, 1], [1, 1.0], [[1, 2], [2, 1]],
             [{key: {'z': None, 'a': [False, 0]}}, {key: {'a': [False, 0], 'z': None}}], [[], []]]
    if mode >= 1:
        contract += """When both values are arrays, override the atomic rule: recursively diff their common
indices in ascending order, remove surplus old elements from highest index down, then append
surplus new elements in ascending index order. Do not replace the array itself.
"""
        cases += [[[value, {'x': False}, 7, 8], [value, {'x': 0}]],
                  [[1], [1, {key: '~/'}, False]], [[], [True, 1]], [[1, 1.0], [1.0, 1]],
                  [[{'nested': [1, 2]}], [{'nested': [0, 1, 2]}]]]
    if mode >= 2:
        contract += """Use a keyed-array rule BEFORE the positional rule when BOTH arrays consist entirely of
objects with unique string id fields (empty arrays qualify). Work on a copy of the old array.
For each new element in ascending desired index i: find its id in the current suffix starting
at i. If absent, add the whole new object at i. If present at j != i, emit
{'op':'move','from': pointer_to_j,'path': pointer_to_i}, remove at j then insert at i.
Next recursively diff the now-current object at i against the new object, then regard it as
updated. Finally remove all leftover trailing elements from highest index down. id matching
uses exact string equality; duplicate ids or non-string/missing ids use the ordinary array rule.
"""
        cases += [[[{'id': 'a', key: value}, {'id': 'b', 'x': 1}, {'id': 'c', 'x': 3}],
                   [{'id': 'c', 'x': 4}, {'id': 'd', key: False}, {'id': 'a', key: value}]],
                  [[{'id': 'same', 'x': 1}, {'id': 'same', 'x': 2}], [{'id': 'same', 'x': 2}]],
                  [[{'id': 1}, {'id': '2'}], [{'id': '2'}, {'id': 1}]],
                  [[], [{'id': 'new', 'x': []}]], [[{'id': 'old'}], []]]
    if mode == 3:
        contract += """For arrays that do NOT qualify as keyed, replace the positional rule with deterministic
longest-common-subsequence (LCS) alignment, using the type-sensitive deep equality above.
Build suffix LCS lengths. Scan old/new from the start: equal elements become anchors; otherwise
skip the old element when skipping old and skipping new have equal LCS length, else skip the
side giving larger length. Process gaps between anchors (and the final trailing gap) in order.
For each gap pair old/new unmatched elements positionally and recursively diff common positions;
remove surplus old elements from highest index down, then add surplus new elements in ascending
order. Paths use the CURRENT array indices after previous gap edits, i.e. the new-side start
index of each gap. Anchors produce no operation. This exact alignment and patch order, rather
than any arbitrary minimal patch, is required. Keyed arrays still take precedence.
"""
        cases += [[[1, 2, 1], [1, 1, 2]], [[False, 0, 1, 1.0], [0, False, 1.0, 1]],
                  [[1, {'x': 2}, 3, {'y': [1, 2]}, 5], [0, 1, {'x': 4}, 3, {'y': [2]}, 5, 6]],
                  [[1, 2, 3, 4], [2, 3]], [[1, 3], [0, 1, 2, 3, 4]],
                  [[{'id': 'x', 'items': [1, 2, 3]}, {'id': 'y'}],
                   [{'id': 'y'}, {'id': 'x', 'items': [0, 1, 3]}]]]
        old = [rng.choice([None, False, 0, 1, 'x', {key: value}, [1, False]]) for _ in range(45)]
        new = copy.deepcopy(old)
        for _ in range(15):
            index = rng.randrange(len(new))
            if rng.random() < 0.5:
                new.pop(index)
            else:
                new.insert(index, {key: rng.randrange(100)})
        cases.append([{'~root/items': old}, {'~root/items': new}])
    return _package(contract, 'json_diff', DIFF, cases, tier)


def make(rng, template_index, tier):
    """Return a coding_fixture tuple for one selected template and difficulty tier."""
    if tier not in TIERS:
        raise ValueError('unknown tier: ' + str(tier))
    if not isinstance(template_index, int) or not 0 <= template_index < len(TEMPLATE_IDS):
        raise ValueError('template index out of range')
    return (_tokenizer, _cache, _graph, _diff)[template_index](rng, tier)
