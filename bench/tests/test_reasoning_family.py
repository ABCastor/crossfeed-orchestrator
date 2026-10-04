"""Known-answer and independent exhaustive checks for exact reasoning oracles."""
import itertools
import json
from pathlib import Path
import random
import tempfile
import unittest

from bench.check import check_task
from bench.families import reasoning


def public_data(prompt):
    marker = 'Entrants and clues:\n' if 'Entrants and clues:\n' in prompt else 'Data:\n'
    return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]


def brute_orders(labels, clues):
    count = 0
    for order in itertools.permutations(labels):
        positions = {label: i+1 for i, label in enumerate(order)}
        valid = True
        for clue in clues:
            p = [positions[label] for label in clue['labels']]
            kind = clue['kind']
            if kind == 'before':
                holds = p[0] < p[1]
            elif kind == 'gap':
                holds = abs(p[0]-p[1]) == clue['value']
            elif kind == 'not-adjacent':
                holds = abs(p[0]-p[1]) > 1
            elif kind == 'parity':
                holds = p[0] % 2 == clue['value']
            elif kind == 'allowed':
                holds = p[0] in clue['positions']
            elif kind == 'between':
                holds = p[0] < p[1] < p[2] or p[2] < p[1] < p[0]
            elif kind == 'sum':
                holds = sum(p) == clue['value']
            else:
                holds = ((p[0] < p[1]) + (p[2] < p[3])) == 1
            if not holds:
                valid = False
                break
        count += valid
    return count


def brute_score(data):
    best = None
    for mask in range(1 << len(data['items'])):
        selected = [item for i, item in enumerate(data['items']) if mask & (1 << i)]
        ids = {item['id'] for item in selected}
        if len(selected) < data['minimum_items']:
            continue
        if any(not set(item['requires']) <= ids for item in selected):
            continue
        if any(a in ids and b in ids for a, b in data['conflicts']):
            continue
        if any(sum(item['cost'][i] for item in selected) > budget for i, budget in enumerate(data['budgets'])):
            continue
        if any(not lower <= sum(item['group'] == group for item in selected) <= upper
               for group, (lower, upper) in data['groups'].items()):
            continue
        score = sum(item['score'] for item in selected)
        score += sum(delta for a, b, delta in data['synergies'] if a in ids and b in ids)
        best = score if best is None else max(best, score)
    return best


def tick_schedule(capacities, jobs):
    """Enumerate schedules one integer tick at a time, independent of event-time search."""
    index = {job['id']: i for i, job in enumerate(jobs)}
    full = (1 << len(jobs))-1
    states = {(0, ())}
    horizon = sum(job['duration'] for job in jobs) + max(job.get('release', 0) for job in jobs)
    for now in range(horizon+1):
        following = set()
        for done, running in states:
            if done == full:
                return now
            active = {node for node, remaining in running}
            available = [node for node, job in enumerate(jobs)
                         if node not in active and not done & (1 << node)
                         and job.get('release', 0) <= now
                         and all(done & (1 << index[label]) for label in job['after'])]
            for bits in range(1 << len(available)):
                started = [available[i] for i in range(len(available)) if bits & (1 << i)]
                work = list(running) + [(node, jobs[node]['duration']) for node in started]
                if any(sum(jobs[node]['need'][i] for node, remaining in work) > cap
                       for i, cap in enumerate(capacities)):
                    continue
                completed = done
                remaining_work = []
                for node, remaining in work:
                    if remaining == 1:
                        completed |= 1 << node
                    else:
                        remaining_work.append((node, remaining-1))
                following.add((completed, tuple(sorted(remaining_work))))
        states = following
    raise AssertionError('independent schedule search found no solution')


class ReasoningFamilyTests(unittest.TestCase):
    def test_all_templates_tiers_seeds_determinism_and_checker(self):
        counts = {'reference': 0, 'null': 0, 'empty': 0}
        with tempfile.TemporaryDirectory(prefix='crossfeed-reasoning-') as temp:
            root = Path(temp)
            workspace = root / 'workspace'
            workspace.mkdir()
            empty = root / 'empty.txt'
            empty.write_text(' \n', encoding='utf-8')
            null = root / 'null.txt'
            null.write_text('null\n', encoding='utf-8')
            for index, template in enumerate(reasoning.TEMPLATE_IDS):
                for tier in reasoning.TIERS:
                    first = reasoning.make(random.Random(1), index, tier)
                    self.assertEqual(first, reasoning.make(random.Random(1), index, tier))
                    second = reasoning.make(random.Random(2), index, tier)
                    self.assertNotEqual(first[0], second[0])
                    for seed, fixture in ((1, first), (2, second)):
                        with self.subTest(template=template, tier=tier, seed=seed):
                            prompt, public, check, reference, reply = fixture
                            self.assertIs(type(check['answer']), int)
                            self.assertEqual((public, reference), ({}, {}))
                            task = root / ('%s-%s-%s' % (seed, template, tier))
                            task.mkdir()
                            (task / 'check.json').write_text(json.dumps(check), encoding='utf-8')
                            reply_file = task / 'reply.txt'
                            reply_file.write_text(reply, encoding='utf-8')
                            self.assertTrue(check_task(task, workspace, reply_file)['pass'])
                            counts['reference'] += 1
                            self.assertFalse(check_task(task, workspace, null)['pass'])
                            counts['null'] += 1
                            self.assertFalse(check_task(task, workspace, empty)['pass'])
                            counts['empty'] += 1
            self.assertEqual(counts, {'reference': 32, 'null': 32, 'empty': 32})

    def test_lineup_known_counts_and_independent_enumeration(self):
        count = reasoning._count_orders
        self.assertEqual(count(['a', 'b', 'c'], []), 6)
        self.assertEqual(count(['a', 'b', 'c'], [{'kind': 'before', 'labels': ['a', 'b']}]), 3)
        self.assertEqual(count(['a', 'b', 'c'], [{'kind': 'between', 'labels': ['a', 'b', 'c']}]), 2)
        self.assertEqual(count(['a', 'b'], [
            {'kind': 'before', 'labels': ['a', 'b']}, {'kind': 'before', 'labels': ['b', 'a']}]), 0)
        labels = list('abcde')
        clues = [
            {'kind': 'xor-before', 'labels': ['a', 'b', 'c', 'd']},
            {'kind': 'parity', 'labels': ['e'], 'value': 1},
            {'kind': 'allowed', 'labels': ['a'], 'positions': [1, 2, 4]},
            {'kind': 'sum', 'labels': ['b', 'c', 'e'], 'value': 9},
            {'kind': 'not-adjacent', 'labels': ['a', 'e']},
            {'kind': 'gap', 'labels': ['b', 'd'], 'value': 2},
        ]
        self.assertEqual(count(labels, clues), brute_orders(labels, clues))
        for seed in (1, 2):
            fixture = reasoning.make(random.Random(seed), 0, 'easy')
            data = public_data(fixture[0])
            self.assertEqual(fixture[2]['answer'], brute_orders(data['entrants'], data['clues']))

    def test_production_known_corrections_units_recovery_rounding(self):
        data = {'ledger': [
            {'action': 'delivery', 'id': 'a', 'row': {'gross': 10, 'tare': 1, 'unit': 'g', 'packages': 2, 'yield': [2, 3], 'status': 'released'}},
            {'action': 'delivery', 'id': 'b', 'row': {'gross': 3000, 'tare': 1, 'unit': 'mg', 'packages': 1, 'yield': [1, 2], 'status': 'released'}},
            {'action': 'replace', 'id': 'a', 'row': {'gross': 5, 'tare': 1, 'unit': 'g', 'packages': 3, 'yield': [2, 3], 'status': 'released'}},
            {'action': 'cancel', 'id': 'b'},
            {'action': 'replace', 'id': 'b', 'row': {'gross': 301, 'tare': 0, 'unit': 'mg', 'packages': 1, 'yield': [2, 3], 'status': 'released'}},
            {'action': 'delivery', 'id': 'ignored', 'row': {'gross': 999, 'tare': 0, 'unit': 'g', 'packages': 9, 'yield': [1, 1], 'status': 'quarantined'}},
        ], 'reserve_mg': 200, 'samples': 2, 'sample_mg': 100, 'dose_mg': 300,
            'caps': 30, 'shells': 40, 'reject': [2, 7], 'reclaim': [2, 3], 'second_reject': [1, 3], 'box_size': 4}
        # 8200 usable - 400 reserved = 7800. First 26, reject 7, recover 1400.
        # Remaining caps limit second run to 4, reject 1. Accept 22, ship 20.
        self.assertEqual(reasoning._production(data), 20)
        data['reclaim'] = None
        self.assertEqual(reasoning._production(data), 16)

    def test_schedule_known_deliberate_idle_and_independent_tick_search(self):
        jobs = [
            {'id': 'a', 'duration': 4, 'need': [1, 0], 'after': [], 'release': 0},
            {'id': 'b', 'duration': 1, 'need': [1, 1], 'after': [], 'release': 1},
            {'id': 'c', 'duration': 8, 'need': [0, 1], 'after': ['b'], 'release': 0},
        ]
        self.assertEqual(reasoning._minimum_makespan([1, 1], jobs), 10)
        self.assertEqual(tick_schedule([1, 1], jobs), 10)
        for seed in range(12):
            rng = random.Random(seed)
            jobs = []
            for i in range(4):
                jobs.append({'id': str(i), 'duration': rng.randrange(1, 4),
                             'need': [rng.randrange(3), rng.randrange(2)],
                             'after': [str(i-1)] if i and rng.random() < 0.4 else [],
                             'release': rng.randrange(3)})
            self.assertEqual(reasoning._minimum_makespan([2, 1], jobs), tick_schedule([2, 1], jobs))

    def test_selection_known_interactions_and_generated_exhaustive_answers(self):
        data = {'items': [
            {'id': 'a', 'cost': [2], 'score': 4, 'group': 'x', 'requires': []},
            {'id': 'b', 'cost': [2], 'score': 8, 'group': 'x', 'requires': ['a']},
            {'id': 'c', 'cost': [3], 'score': 9, 'group': 'y', 'requires': []},
        ], 'budgets': [5], 'groups': {}, 'conflicts': [['b', 'c']],
            'synergies': [['a', 'b', -7], ['a', 'c', 6]], 'minimum_items': 0}
        self.assertEqual(reasoning._maximum_score(data), 19)
        for seed in (1, 2):
            for tier in reasoning.TIERS:
                with self.subTest(seed=seed, tier=tier):
                    fixture = reasoning.make(random.Random(seed), 3, tier)
                    self.assertEqual(fixture[2]['answer'], brute_score(public_data(fixture[0])))


if __name__ == '__main__':
    unittest.main()
