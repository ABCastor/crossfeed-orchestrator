"""Offline reasoning problems with exact counting and optimization oracles."""
import itertools
import json
from functools import lru_cache

TEMPLATE_IDS = ('lineup-constraints', 'corrected-production', 'resource-scheduling', 'portfolio-selection')
TIERS = ('easy', 'medium', 'hard', 'expert')


def _fixture(prompt, answer):
    prompt += '\nReturn a final JSON line {"answer": INTEGER}, with no other keys.\n'
    return prompt, {}, {'kind': 'reasoning', 'answer': answer}, {}, json.dumps({'answer': answer}) + '\n'


def _count_orders(labels, clues):
    """Count permutations by pruning each clue as soon as its labels are assigned."""
    index = {label: i for i, label in enumerate(labels)}
    requirements = []
    for clue in clues:
        needed = [index[label] for label in clue['labels']]
        requirements.append((clue, needed))
    positions = [0] * len(labels)
    def valid(clue, needed):
        values = [positions[i] for i in needed]
        # Apply one-variable domain constraints even before all variables exist.
        if clue['kind'] == 'parity':
            return not values[0] or values[0] % 2 == clue['value']
        if clue['kind'] == 'allowed':
            return not values[0] or values[0] in clue['positions']
        if not all(values):
            return True
        kind = clue['kind']
        if kind == 'before':
            return values[0] < values[1]
        if kind == 'gap':
            return abs(values[0] - values[1]) == clue['value']
        if kind == 'not-adjacent':
            return abs(values[0] - values[1]) != 1
        if kind == 'between':
            return min(values[0], values[2]) < values[1] < max(values[0], values[2])
        if kind == 'sum':
            return sum(values) == clue['value']
        if kind == 'xor-before':
            return (values[0] < values[1]) != (values[2] < values[3])
        raise ValueError('unknown constraint')
    linked = [[] for _ in labels]
    for clue, needed in requirements:
        for node in needed:
            linked[node].append((clue, needed))
    def search(depth, mask):
        if depth == len(labels):
            return 1
        count = 0
        for node in range(len(labels)):
            if mask & (1 << node):
                continue
            positions[node] = depth + 1
            if all(valid(clue, needed) for clue, needed in linked[node]):
                count += search(depth + 1, mask | (1 << node))
            positions[node] = 0
        return count
    return search(0, 0)


def _production(data):
    """Interpret corrected deliveries, then integer production/reclaim/packing."""
    rows = {}
    for record in data['ledger']:
        if record['action'] in ('delivery', 'replace'):
            rows[record['id']] = record['row']
        else:
            rows.pop(record['id'], None)
    powder = 0
    for row in rows.values():
        if row['status'] != 'released':
            continue
        multiplier = 1000 if row['unit'] == 'g' else 1
        net = (row['gross'] - row['tare']) * multiplier * row['packages']
        powder += net * row['yield'][0] // row['yield'][1]
    powder -= data['reserve_mg'] + data['samples'] * data['sample_mg']
    initial = min(powder // data['dose_mg'], data['caps'], data['shells'])
    rejects = initial * data['reject'][0] // data['reject'][1]
    accepted = initial - rejects
    if data['reclaim'] is not None:
        reclaim = rejects * data['dose_mg'] * data['reclaim'][0] // data['reclaim'][1]
        remaining = powder - initial * data['dose_mg'] + reclaim
        second = min(remaining // data['dose_mg'], data['caps'] - initial, data['shells'] - initial)
        second_rejects = second * data['second_reject'][0] // data['second_reject'][1]
        accepted += second - second_rejects
    return accepted // data['box_size'] * data['box_size']


def _minimum_makespan(capacities, jobs):
    """Exact event-time state search, including intentionally idle schedules."""
    n = len(jobs)
    index = {job['id']: i for i, job in enumerate(jobs)}
    prerequisites = [sum(1 << index[label] for label in job['after']) for job in jobs]
    full = (1 << n) - 1
    @lru_cache(None)
    def solve(now, done, running):
        if done == full:
            return now
        occupied = [0] * len(capacities)
        active = 0
        for node, finish in running:
            active |= 1 << node
            for resource, amount in enumerate(jobs[node]['need']):
                occupied[resource] += amount
        available = [node for node, job in enumerate(jobs)
                     if not ((done | active) & (1 << node)) and job.get('release', 0) <= now
                     and prerequisites[node] & done == prerequisites[node]]
        # Enumerate feasible start sets at this event; empty means deliberate idle.
        starts = []
        def subsets(at, chosen, usage):
            if at == len(available):
                starts.append(tuple(chosen))
                return
            node = available[at]
            subsets(at + 1, chosen, usage)
            updated = [used + amount for used, amount in zip(usage, jobs[node]['need'])]
            if all(used <= cap for used, cap in zip(updated, capacities)):
                subsets(at + 1, chosen + [node], updated)
        subsets(0, [], occupied)
        best = float('inf')
        for chosen in starts:
            current = list(running) + [(node, now + jobs[node]['duration']) for node in chosen]
            future = [finish for node, finish in current]
            future.extend(job.get('release', 0) for node, job in enumerate(jobs)
                          if not ((done | active) & (1 << node)) and node not in chosen
                          and job.get('release', 0) > now)
            if not future:
                continue
            next_time = min(future)
            completed = done
            remaining = []
            for node, finish in current:
                if finish == next_time:
                    completed |= 1 << node
                else:
                    remaining.append((node, finish))
            answer = solve(next_time, completed, tuple(sorted(remaining)))
            best = min(best, answer)
        return best
    answer = solve(0, 0, ())
    if answer == float('inf'):
        raise ValueError('infeasible schedule')
    return answer


def _maximum_score(data):
    """Budget-pruned exact subset search with backward prerequisites and interactions."""
    items, budgets = data['items'], data['budgets']
    index = {item['id']: i for i, item in enumerate(items)}
    prerequisites = [sum(1 << index[label] for label in item['requires']) for item in items]
    conflicts = [0] * len(items)
    for left, right in data['conflicts']:
        a, b = index[left], index[right]
        conflicts[a] |= 1 << b
        conflicts[b] |= 1 << a
    synergy = {}
    for left, right, value in data['synergies']:
        a, b = sorted((index[left], index[right]))
        synergy[b, a] = value
    bounds = data['groups']
    suffix = [{group: 0 for group in bounds} for _ in range(len(items)+1)]
    for node in range(len(items)-1, -1, -1):
        suffix[node] = dict(suffix[node+1])
        if items[node]['group'] in bounds:
            suffix[node][items[node]['group']] += 1
    best = None
    def search(node, mask, used, counts, score, chosen):
        nonlocal best
        if any(counts[group] + suffix[node][group] < rule[0] for group, rule in bounds.items()):
            return
        if chosen + len(items) - node < data['minimum_items']:
            return
        if node == len(items):
            if chosen >= data['minimum_items']:
                best = score if best is None else max(best, score)
            return
        search(node+1, mask, used, counts, score, chosen)
        item = items[node]
        if prerequisites[node] & mask != prerequisites[node] or conflicts[node] & mask:
            return
        updated = [cost + amount for cost, amount in zip(used, item['cost'])]
        if any(cost > cap for cost, cap in zip(updated, budgets)):
            return
        group = item['group']
        if group in bounds and counts[group] >= bounds[group][1]:
            return
        extra = sum(synergy.get((node, previous), 0) for previous in range(node) if mask & (1 << previous))
        new_counts = dict(counts)
        if group in bounds:
            new_counts[group] += 1
        search(node+1, mask | (1 << node), updated, new_counts, score + item['score'] + extra, chosen+1)
    search(0, 0, [0]*len(budgets), {group: 0 for group in bounds}, 0, 0)
    if best is None:
        raise ValueError('infeasible selection')
    return best


def _lineup(rng, tier):
    mode = TIERS.index(tier)
    n = 7 + mode
    prefix = 'N' + str(rng.randrange(10000, 99999))
    labels = [prefix + chr(65+i) for i in range(n)]
    hidden_order = rng.sample(labels, n)
    position = {label: i+1 for i, label in enumerate(hidden_order)}
    clues = []
    for _ in range(2 + mode):
        left, right = sorted(rng.sample(labels, 2), key=position.get)
        clues.append({'kind': 'before', 'labels': [left, right]})
    left, right = rng.sample(labels, 2)
    clues.append({'kind': 'gap', 'labels': [left, right], 'value': abs(position[left]-position[right])})
    distant = [(a, b) for a, b in itertools.combinations(labels, 2) if abs(position[a]-position[b]) > 1]
    for _ in range(1 + mode):
        left, right = rng.choice(distant)
        clues.append({'kind': 'not-adjacent', 'labels': [left, right]})
    if mode >= 1:
        for label in rng.sample(labels, 2):
            clues.append({'kind': 'parity', 'labels': [label], 'value': position[label] % 2})
        triple = sorted(rng.sample(labels, 3), key=position.get)
        clues.append({'kind': 'between', 'labels': triple})
    if mode >= 2:
        for label in rng.sample(labels, 2):
            choices = sorted({position[label]} | set(rng.sample(range(1, n+1), 3)))
            clues.append({'kind': 'allowed', 'labels': [label], 'positions': choices})
        triple = rng.sample(labels, 3)
        clues.append({'kind': 'sum', 'labels': triple, 'value': sum(position[label] for label in triple)})
    if mode == 3:
        for _ in range(2):
            a, b, c, d = rng.sample(labels, 4)
            if (position[a] < position[b]) == (position[c] < position[d]):
                c, d = d, c
            clues.append({'kind': 'xor-before', 'labels': [a, b, c, d]})
        triple = rng.sample(labels, 3)
        clues.append({'kind': 'sum', 'labels': triple, 'value': sum(position[label] for label in triple)})
    rng.shuffle(clues)
    prompt = """Count every valid arrangement of the named entrants in one straight line. Every entrant
appears exactly once, and positions are numbered 1 through N from left to right. Reversing
an arrangement counts as different unless it is identical. All clues apply simultaneously.
The answer is the exact NUMBER of satisfying arrangements, not an example arrangement.
Clue definitions: before [a,b] means pos(a)<pos(b); gap [a,b] with value k means
abs(pos(a)-pos(b))=k (distance, not the number of people between). not-adjacent means that
distance is not 1. parity value 0 is even and 1 is odd. between [a,b,c] means b is strictly
between a and c, with either orientation allowed. allowed lists permitted positions.
sum means the sum of the listed positions equals value. xor-before [a,b,c,d] means EXACTLY
ONE of pos(a)<pos(b) and pos(c)<pos(d) is true. Duplicate clues impose no extra multiplicity.
Entrants and clues:\n""" + json.dumps({'entrants': labels, 'clues': clues}, indent=2)
    return _fixture(prompt, _count_orders(labels, clues))


def _arithmetic(rng, tier):
    mode = TIERS.index(tier)
    prefix = 'lot_' + str(rng.randrange(10000, 99999)) + '_'
    count = (5, 8, 12, 16)[mode]
    ledger = []
    def new_row():
        unit = rng.choice(['g', 'mg'])
        gross = rng.randrange(15, 90) * (1000 if unit == 'mg' else 1)
        tare = rng.randrange(0, max(1, gross//8))
        return {'gross': gross, 'tare': tare, 'unit': unit, 'packages': rng.randrange(1, 7),
                'yield': rng.choice([[1, 1], [9, 10], [4, 5], [7, 8]]) if mode >= 1 else [1, 1],
                'status': 'released' if rng.random() < 0.8 else 'quarantined'}
    for index in range(count):
        row = new_row()
        if index == 0:
            row['status'] = 'released'
        ledger.append({'action': 'delivery', 'id': prefix + str(index), 'row': row})
    for index in rng.sample(range(count), mode+1):
        row = new_row()
        if index == 0:
            row['status'] = 'released'
        ledger.append({'action': 'replace', 'id': prefix + str(index), 'row': row})
    for index in rng.sample(range(1, count), mode):
        ledger.append({'action': 'cancel', 'id': prefix + str(index)})
    if mode == 3:
        # A correction can reactivate a canceled row, which must be resolved chronologically.
        canceled = ledger[-1]['id']
        row = new_row()
        row['status'] = 'released'
        ledger.append({'action': 'replace', 'id': canceled, 'row': row})
    data = {'ledger': ledger, 'reserve_mg': rng.randrange(100, 500),
            'samples': rng.randrange(3, 12), 'sample_mg': rng.randrange(20, 80),
            'dose_mg': rng.randrange(120, 400), 'caps': 1000000, 'shells': 1000000,
            'reject': [0, 1], 'reclaim': None, 'second_reject': [0, 1], 'box_size': 1,
            'audit_only': {'superseded_estimate_mg': rng.randrange(500000, 900000),
                           'purchase_price_cents': rng.randrange(10000, 99999),
                           'historical_output': rng.randrange(100, 999)}}
    raw = _production(data)
    if mode == 3:
        data['caps'] = raw + max(10, raw//3)
        data['shells'] = raw + max(8, raw//4)
    else:
        data['caps'] = max(0, raw + rng.randrange(-max(1, raw//5), max(2, raw//5)))
        data['shells'] = max(0, raw + rng.randrange(-max(1, raw//6), max(2, raw//6)))
    if mode >= 2:
        data['reject'] = rng.choice([[1, 7], [2, 11], [3, 13]])
        data['box_size'] = rng.choice([8, 12, 16])
    if mode == 3:
        data['reclaim'] = rng.choice([[3, 4], [5, 7], [7, 9]])
        data['second_reject'] = rng.choice([[1, 9], [2, 13]])
    prompt = """Determine the number of accepted capsules shipped by this fictional production line.
Use exact integer arithmetic. All fractions are [numerator,denominator]. Every division
specified as floor rounds down at THAT step; do not combine fractions across steps.
Read the ledger chronologically. delivery installs its whole row under id. replace replaces
the entire row, including status, and reactivates a canceled id. cancel removes that id.
Only each id's final row counts, and only status released contributes powder. For each such
row, gross and tare are PER PACKAGE in unit g or mg; 1 g = 1000 mg. Compute net milligrams
as (gross-tare)*unit_multiplier*packages, then usable = floor(net*yield_n/yield_d), separately
for each final row. Sum usable. Quarantined lots and audit_only fields contribute nothing.
Subtract reserve_mg and samples*sample_mg. This remaining powder is nonnegative and the
reserve/sample powder can never be reused. First-run output = minimum of floor(powder/dose_mg),
caps, shells. Each capsule consumes dose_mg powder, one cap and one shell. Reject exactly
floor(first_output*reject_n/reject_d) capsules, accepting the rest. If reclaim is null stop
production. Otherwise recover floor(rejected*dose_mg*reclaim_n/reclaim_d) mg and add that to
the unused first-run powder. Run ONCE more, with remaining caps and shells (rejected capsules
DO NOT return packaging). Reject floor(second_output*second_reject_n/second_reject_d) in
this second run, accept the rest, and perform NO further recovery. Add both accepted counts.
Ship only full boxes of box_size: answer floor(total_accepted/box_size)*box_size, the number
of CAPSULES shipped, not boxes. Unshipped partial boxes and unused stock are ignored.
Data:\n""" + json.dumps(data, indent=2)
    return _fixture(prompt, _production(data))


def _schedule(rng, tier):
    mode = TIERS.index(tier)
    n = (4, 6, 8, 10)[mode]
    capacities = ([2], [2, 1], [2, 2], [2, 2, 1])[mode]
    prefix = 'job_' + str(rng.randrange(10000, 99999)) + '_'
    jobs = []
    for node in range(n):
        if mode == 3:
            profiles = [[1, 0, 0], [0, 1, 0], [1, 1, 0], [1, 0, 1],
                        [0, 1, 1], [2, 0, 0], [0, 2, 0], [1, 1, 1]]
            need = ([1, 0, 1] if node == 0 else [0, 1, 0] if node == 1
                    else list(rng.choice(profiles)))
        else:
            need = [rng.randrange(cap+1) for cap in capacities]
            if not any(need):
                need[rng.randrange(len(need))] = 1
        previous = [job['id'] for job in jobs]
        after = rng.sample(previous, rng.randrange(min(len(previous), 2)+1)) if previous else []
        if mode == 3 and node < 2:
            after = []
        release = rng.randrange(0, 6) if mode >= 2 else 0
        if mode == 3 and node < 2:
            release = 0
        jobs.append({'id': prefix + str(node), 'duration': rng.randrange(2, 9), 'need': need,
                     'after': after, 'release': release})
    # Worker-facing row order is deliberately unrelated to dependency order.
    shown = rng.sample(jobs, len(jobs))
    prompt = """Find the globally MINIMUM makespan (finish time of the last job) for this resource schedule.
Time starts at integer 0. Each job runs once for its duration, without interruption. It cannot
start before release or before ALL jobs named in after finish. During its whole run it needs
its need vector of resource units simultaneously. Total usage of each resource must never
exceed capacities. Resources are renewable and reusable immediately when a job finishes.
Intervals are [start,finish): jobs may start exactly when another finishes. Jobs can run
concurrently if all capacities permit it, and you may intentionally idle resources. Zero
requirements for a particular resource are valid. All durations are positive integers and
integer start times suffice. No setup times, deadlines, hidden ordering or overnight breaks.
Optimize over all feasible schedules, not a greedy priority heuristic. The answer is the
minimum final finish time, an integer; ties between optimal schedules need no extra choice.
The input row order is irrelevant. Data:\n""" + json.dumps({'capacities': capacities, 'jobs': shown}, indent=2)
    return _fixture(prompt, _minimum_makespan(capacities, jobs))


def _selection(rng, tier):
    mode = TIERS.index(tier)
    n = (8, 10, 12, 16)[mode]
    resources = (1, 2, 3, 3)[mode]
    prefix = 'option_' + str(rng.randrange(10000, 99999)) + '_'
    items = []
    guaranteed = 4 if mode == 3 else 3
    for node in range(n):
        earlier = [item['id'] for item in items]
        requires = rng.sample(earlier, 1) if mode >= 2 and earlier and rng.random() < 0.35 else []
        items.append({'id': prefix + str(node), 'cost': [rng.randrange(2, 13) for _ in range(resources)],
                      'score': rng.randrange(5, 40), 'group': 'ABC'[node % 3], 'requires': requires})
    budgets = [sum(item['cost'][resource] for item in items[:guaranteed]) + rng.randrange(8, 25)
               for resource in range(resources)]
    conflicts = []
    if mode >= 1:
        candidates = [(a, b) for a, b in itertools.combinations(range(n), 2) if b >= guaranteed]
        for a, b in rng.sample(candidates, (3, 5, 9)[mode-1]):
            conflicts.append([items[a]['id'], items[b]['id']])
    groups = {group: [1, rng.randrange(2, 5)] for group in 'ABC'} if mode >= 2 else {}
    if mode == 3:
        groups['A'][1] = max(2, groups['A'][1])
    synergies = []
    if mode == 3:
        for a, b in rng.sample(list(itertools.combinations(range(n), 2)), 12):
            synergies.append([items[a]['id'], items[b]['id'], rng.choice([-19, -11, 8, 17, 26])])
    data = {'items': items, 'budgets': budgets, 'conflicts': conflicts, 'groups': groups,
            'synergies': synergies, 'minimum_items': guaranteed if mode == 3 else 0}
    prompt = """Choose a feasible subset of the listed options to MAXIMIZE its total integer score.
Each option is selected at most once. Sum its cost vectors componentwise; each total must
be <= the corresponding budget. All budgets are inclusive, and unused resources are allowed.
Each selected option contributes score. Selecting an option also requires selecting every id
in its requires list; requirements are not free and consume their own costs. A conflict pair
forbids selecting BOTH, but selecting neither is allowed. For each group in groups, the number
of selected options in that group must be between its [minimum,maximum], inclusive. Groups
omitted from groups have no count restriction. Select at least minimum_items options overall.
A synergy [a,b,delta] adds delta ONCE exactly when both a and b are selected. Negative deltas
are penalties, not conflicts. All pair effects apply simultaneously and are additional to
individual scores. Each conflict/synergy pair is listed once. Dependencies refer only to
options earlier in this input list; ids and lexical order have no objective value.
The answer is the MAXIMUM feasible score, not the chosen ids or a greedy estimate. If several
subsets tie, their common score is still the unique integer answer. Data:\n""" + json.dumps(data, indent=2)
    return _fixture(prompt, _maximum_score(data))


def make(rng, template_index, tier):
    """Generate one exact-answer reasoning fixture."""
    if tier not in TIERS:
        raise ValueError('unknown tier: ' + str(tier))
    if not isinstance(template_index, int) or not 0 <= template_index < len(TEMPLATE_IDS):
        raise ValueError('template index out of range')
    return (_lineup, _arithmetic, _schedule, _selection)[template_index](rng, tier)
