"""Shared contract packaging, independent of task semantics."""
import hashlib
import json


def _test_source(function, cases, expected):
    return ("import copy\nimport unittest\nfrom solution import %s\n"
             "\ndef strict_equal(a, b):\n"
             "    if type(a) is not type(b): return False\n"
             "    if isinstance(b, dict): return a.keys() == b.keys() and all(strict_equal(a[k], v) for k,v in b.items())\n"
             "    if isinstance(b, list): return len(a) == len(b) and all(strict_equal(x,y) for x,y in zip(a,b))\n"
             "    return a == b\n"
             "\nclass ContractTests(unittest.TestCase):\n"
             "    def test_cases(self):\n"
             "        for args, want in zip(%r, %r):\n"
             "            with self.subTest(args=args):\n"
             "                original = copy.deepcopy(args)\n"
             "                self.assertTrue(strict_equal(%s(*args), want), 'result type or value differs')\n"
             "                self.assertEqual(args, original, 'input was mutated')\n") % (function, cases, expected, function)


def coding_fixture(contract, function, reference, candidate, cases, expected, family):
    tests = _test_source(function, cases, expected)
    workspace, reference = dict(candidate), dict(reference)
    check = {'kind': family, 'test_source': tests, 'function': function,
             'cases': cases, 'expected': expected}
    if family == 'fix':
        # Public regression examples must not expose every scored answer.
        examples, answers, seen = [], [], set()
        for args, want in zip(cases, expected):
            key = json.dumps(args, sort_keys=True)
            if key not in seen:
                examples.append(args)
                answers.append(want)
                seen.add(key)
            if len(examples) == 3:
                break
        if not any(json.dumps(args, sort_keys=True) not in seen for args in cases):
            raise ValueError('fix fixture needs distinct hidden scoring inputs')
        public_tests = _test_source(function, examples, answers)
        workspace['test_solution.py'] = reference['test_solution.py'] = public_tests
        check['tests'] = {'test_solution.py': hashlib.sha256(public_tests.encode()).hexdigest()}
    prompt = ('Repair the defect.' if family == 'fix' else 'Implement the specified function.')
    prompt += '\n\n' + contract + '\n\nUse only the Python standard library. Do not mutate inputs. Preserve test files. End with a short nonempty description of your change.\n'
    return prompt, workspace, check, reference, 'Implemented the contract and checked edge cases.\n'
