"""Implementation-family reference, rejection, and independent semantic checks."""
import copy
import json
from pathlib import Path
import random
import tempfile
import unittest

from bench.check import check_task, same_value
from bench.families import implement


def reference_function(index, tier):
    fixture = implement.make(random.Random(1), index, tier)
    namespace = {}
    exec(fixture[3]['solution.py'], namespace)
    return namespace[fixture[2]['function']]


def apply_patch(value, operations):
    """Independent sequential JSON-patch application for generated diff outputs."""
    value = copy.deepcopy(value)
    def parts(pointer):
        return [part.replace('~1', '/').replace('~0', '~') for part in pointer.split('/')[1:]]
    def parent(pointer):
        keys = parts(pointer)
        target = value
        for key in keys[:-1]:
            target = target[int(key)] if isinstance(target, list) else target[key]
        return target, keys[-1]
    for operation in operations:
        op = operation['op']
        if op == 'move':
            source, key = parent(operation['from'])
            item = source.pop(int(key)) if isinstance(source, list) else source.pop(key)
        elif op != 'remove':
            item = copy.deepcopy(operation['value'])
        if operation['path'] == '':
            value = item
            continue
        target, key = parent(operation['path'])
        if isinstance(target, list):
            index = int(key)
            if op == 'remove':
                target.pop(index)
            elif op in ('add', 'move'):
                target.insert(index, item)
            else:
                target[index] = item
        elif op == 'remove':
            del target[key]
        else:
            target[key] = item
    return value


class ImplementFamilyTests(unittest.TestCase):
    def test_diff_output_aliasing_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='crossfeed-alias-') as temporary:
            root = Path(temporary)
            for tier in implement.TIERS:
                fixture = implement.make(random.Random(1), 3, tier)
                _, _, check, reference, reply = fixture
                task = root / tier
                (task / 'workspace').mkdir(parents=True)
                source = reference['solution.py'].replace("item['value'] = copy.deepcopy(value)",
                                                         "item['value'] = value")
                self.assertNotEqual(source, reference['solution.py'])
                (task / 'workspace/solution.py').write_text(source)
                (task / 'check.json').write_text(json.dumps(check))
                (task / 'reply.txt').write_text(reply)
                with self.subTest(tier=tier):
                    self.assertFalse(check_task(task, task / 'workspace', task / 'reply.txt')['pass'])

    def test_cache_fixtures_obey_timestamp_contract(self):
        for tier in ('medium', 'hard', 'expert'):
            for seed in (1, 2):
                fixture = implement.make(random.Random(seed), 1, tier)
                for _, events in fixture[2]['cases']:
                    timestamps = [event['at'] for event in events]
                    self.assertTrue(all(type(at) is int for at in timestamps))
                    self.assertEqual(timestamps, sorted(timestamps))
                    for event in events:
                        if event['op'] == 'batch':
                            self.assertTrue(all('at' not in child for child in event['events']))

    def test_all_templates_tiers_and_seeds_through_checker(self):
        counts = {'reference': 0, 'stub': 0, 'empty': 0}
        with tempfile.TemporaryDirectory(prefix='crossfeed-implement-') as temp:
            root = Path(temp)
            empty = root / 'empty.txt'
            empty.write_text(' \n', encoding='utf-8')
            for seed in (1, 2):
                for index, template in enumerate(implement.TEMPLATE_IDS):
                    for tier in implement.TIERS:
                        with self.subTest(seed=seed, template=template, tier=tier):
                            prompt, workspace, check, reference, reply = implement.make(random.Random(seed), index, tier)
                            task = root / ('%s-%s-%s' % (seed, template, tier))
                            task.mkdir()
                            (task / 'check.json').write_text(json.dumps(check), encoding='utf-8')
                            for name, mapping in (('workspace', workspace), ('reference', reference)):
                                folder = task / name
                                folder.mkdir()
                                for path, source in mapping.items():
                                    (folder / path).write_text(source, encoding='utf-8')
                            reply_path = task / 'reply.txt'
                            reply_path.write_text(reply, encoding='utf-8')
                            verdict = check_task(task, task / 'reference', reply_path)
                            self.assertTrue(verdict['pass'], verdict)
                            counts['reference'] += 1
                            verdict = check_task(task, task / 'workspace', reply_path)
                            self.assertFalse(verdict['pass'], verdict)
                            counts['stub'] += 1
                            verdict = check_task(task, task / 'reference', empty)
                            self.assertFalse(verdict['pass'], verdict)
                            counts['empty'] += 1
            self.assertEqual(counts, {'reference': 32, 'stub': 32, 'empty': 32})

    def test_determinism_and_seeded_public_inputs(self):
        self.assertEqual(len(set(implement.TEMPLATE_IDS)), 4)
        for index in range(len(implement.TEMPLATE_IDS)):
            for tier in implement.TIERS:
                with self.subTest(index=index, tier=tier):
                    first = implement.make(random.Random(1), index, tier)
                    again = implement.make(random.Random(1), index, tier)
                    second = implement.make(random.Random(2), index, tier)
                    self.assertEqual(first, again)
                    self.assertNotEqual((first[0], first[1]), (second[0], second[1]))
                    self.assertNotIn('test_solution.py', first[1])

    def test_tokenizer_independent_expectations(self):
        tokenize = reference_function(0, 'expert')
        self.assertEqual(tokenize('0xA_f + [1e-2]'), {
            'tokens': [['NUMBER', '0xA_f', 0, 5], ['OP', '+', 6, 7], ['OP', '[', 8, 9],
                       ['NUMBER', '1e-2', 9, 13], ['OP', ']', 13, 14]], 'error': None})
        self.assertEqual(tokenize('/* one /* two */ three */"雪"')['tokens'], [['STRING', '"雪"', 25, 28]])
        self.assertEqual(tokenize('1__2')['error'], {'offset': 0, 'code': 'invalid-number'})
        self.assertEqual(tokenize('([)]')['error'], {'offset': 2, 'code': 'mismatched-delimiter'})
        self.assertEqual(tokenize('([1]')['error'], {'offset': 0, 'code': 'unclosed-delimiter'})
        self.assertEqual(tokenize('"x\\')['error'], {'offset': 0, 'code': 'unterminated-string'})
        self.assertEqual(tokenize('/* x /* y */')['error'], {'offset': 0, 'code': 'unterminated-comment'})

    def test_cache_transaction_expiry_and_capacity_independent_expectations(self):
        cache = reference_function(1, 'expert')
        events = [
            {'op': 'put', 'at': 0, 'key': 'a', 'value': 1, 'ttl': 5, 'sliding': True},
            {'op': 'put', 'at': 0, 'key': 'b', 'value': 2, 'ttl': 2},
            {'op': 'batch', 'at': 3, 'events': [{'op': 'get', 'key': 'a'},
                {'op': 'resize', 'capacity': 0}, {'op': 'put', 'key': 'c', 'value': 3}]},
            {'op': 'get', 'at': 5, 'key': 'a'},
            {'op': 'put', 'at': 6, 'key': 'd', 'value': 4, 'weight': 2},
        ]
        self.assertEqual(cache(2, events), [
            {'result': True, 'keys': ['a'], 'used': 1},
            {'result': True, 'keys': ['a', 'b'], 'used': 2},
            {'result': {'accepted': False, 'values': []}, 'keys': ['a'], 'used': 1},
            {'result': None, 'keys': [], 'used': 0},
            {'result': True, 'keys': ['d'], 'used': 2},
        ])
        self.assertEqual(cache(2, [
            {'op': 'put', 'at': 0, 'key': 'a', 'value': 1},
            {'op': 'put', 'at': 1, 'key': 'a', 'value': 2, 'weight': 3},
            {'op': 'get', 'at': 2, 'key': 'a'},
        ])[-1], {'result': 1, 'keys': ['a'], 'used': 1})

    def test_graph_independent_expectations(self):
        plan = reference_function(2, 'expert')
        self.assertEqual(plan(['d', 'c', 'b', 'a'], [
            ['a', 'b'], ['b', 'c'], ['a', 'c'], ['c', 'd'], ['a', 'd'], ['b', 'd'], ['a', 'b'],
        ]), {'order': ['a', 'b', 'c', 'd'], 'acyclic': True, 'cycle': [],
             'cyclic_components': [], 'layers': [['a'], ['b'], ['c'], ['d']],
             'reduction': [['a', 'b'], ['b', 'c'], ['c', 'd']]})
        self.assertEqual(plan(['d', 'c', 'b', 'a'], [
            ['a', 'b'], ['b', 'c'], ['c', 'a'], ['c', 'd'], ['d', 'c'], ['a', 'd'], ['d', 'a'],
        ]), {'order': [], 'acyclic': False, 'cycle': ['a', 'd', 'a'],
             'cyclic_components': [['a', 'b', 'c', 'd']], 'layers': [], 'reduction': []})
        medium = reference_function(2, 'medium')
        self.assertEqual(medium(['c', 'b', 'a'], [['a', 'b'], ['b', 'c'], ['c', 'a']])['cycle'], ['a', 'b', 'c', 'a'])

    def test_diff_alignment_independent_expectations_and_patch_application(self):
        diff = reference_function(3, 'expert')
        self.assertEqual(diff([1, 2, 1], [1, 1, 2]), [
            {'op': 'remove', 'path': '/1'}, {'op': 'add', 'path': '/2', 'value': 2}])
        self.assertEqual(diff([{'id': 'a', 'x': 1}, {'id': 'b', 'x': 2}],
                              [{'id': 'b', 'x': 3}, {'id': 'a', 'x': 1}]), [
            {'op': 'move', 'from': '/1', 'path': '/0'},
            {'op': 'replace', 'path': '/0/x', 'value': 3}])
        self.assertEqual(diff({'a/~': True}, {'a/~': 1}), [
            {'op': 'replace', 'path': '/a~1~0', 'value': 1}])
        for tier in implement.TIERS:
            fixture = implement.make(random.Random(2), 3, tier)
            for args, operations in zip(fixture[2]['cases'], fixture[2]['expected']):
                before, after = args
                with self.subTest(tier=tier, before=before, after=after):
                    self.assertTrue(same_value(apply_patch(before, operations), after))


if __name__ == '__main__':
    unittest.main()
