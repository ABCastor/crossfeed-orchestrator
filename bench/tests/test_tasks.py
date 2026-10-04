"""Offline fixture and checker contract tests."""
import collections
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from bench.check import check_task
from bench.generate import FAMILIES, generate
from bench.reference import materialize

ROOT = Path(__file__).resolve().parents[2]


def snapshot(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


class TaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='crossfeed-tests-')
        cls.root = Path(cls.temp.name)
        cls.seed1 = generate(1,cls.root/'seed1')
        cls.seed2 = generate(2,cls.root/'seed2')
        cls.empty = cls.root/'empty.txt'
        cls.empty.write_text(' \n\t',encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def clone(self,name):
        temp = tempfile.TemporaryDirectory(prefix='crossfeed-case-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        task = next(t for t in self.seed1 if t.name == name)
        workspace = root/'workspace'
        shutil.copytree(task/'reference'/'workspace',workspace)
        reply = root/'reply.txt'
        reply.write_text((task/'reference'/'reply.txt').read_text(),encoding='utf-8')
        return task,workspace,reply

    def test_shape_and_difficulty(self):
        self.assertEqual(len(self.seed1),48)
        counts = collections.defaultdict(collections.Counter)
        for task in self.seed1:
            meta = json.loads((task/'meta.json').read_text())
            counts[meta['family']][meta['difficulty']] += 1
            self.assertEqual(meta['seed'],1)
            self.assertEqual(meta['tier'], meta['difficulty'])
            self.assertTrue(meta['template_id'])
            self.assertTrue((task/'PROMPT.md').is_file())
            self.assertTrue((task/'check.json').is_file())
            self.assertTrue((task/'workspace').is_dir())
            self.assertTrue((task/'reference/workspace').is_dir())
            self.assertTrue((task/'reference/reply.txt').is_file())
            self.assertFalse((task/'workspace/check.json').exists())
            if meta['family'] == 'repo-qa':
                files = [p for p in (task/'workspace').rglob('*') if p.is_file()]
                self.assertGreaterEqual(len(files),10)
                self.assertLessEqual(len(files),60)
        self.assertEqual(set(counts),set(FAMILIES))
        for counts_family in counts.values():
            self.assertEqual(counts_family,{'easy':2,'medium':2,'hard':2,'expert':2})

    def test_template_tier_coverage_and_uneven_counts(self):
        for n in (5, 17, 32):
            tasks = generate(31, self.root / ('coverage%d' % n), per_family=n)
            coverage = collections.defaultdict(collections.Counter)
            tiers = collections.defaultdict(collections.Counter)
            for task in tasks:
                meta = json.loads((task / 'meta.json').read_text())
                coverage[meta['family']][(meta['template_id'], meta['tier'])] += 1
                tiers[meta['family']][meta['tier']] += 1
            for family in FAMILIES:
                values = [tiers[family][t] for t in ('easy', 'medium', 'hard', 'expert')]
                self.assertLessEqual(max(values) - min(values), 1)
                if n >= 16:
                    self.assertGreaterEqual(len(coverage[family]), 16)
                    self.assertGreaterEqual(len({template for template, _ in coverage[family]}), 4)
                if n == 32:
                    self.assertEqual(set(coverage[family].values()), {2})

    def test_generation_deterministic_and_seeded(self):
        again = self.root/'again'
        generate(1,again)
        self.assertEqual(snapshot(self.root/'seed1'),snapshot(again))
        self.assertNotEqual(snapshot(self.root/'seed1'),snapshot(self.root/'seed2'))
        for task1,task2 in zip(self.seed1,self.seed2):
            # Metadata-only differences are insufficient: worker inputs must vary.
            def worker_digest(task):
                data = (task/'PROMPT.md').read_bytes()
                for p in sorted((task/'workspace').rglob('*')):
                    if p.is_file():
                        data += p.read_bytes()
                return hashlib.sha256(data).hexdigest()
            self.assertNotEqual(worker_digest(task1),worker_digest(task2),task1.name)

    def test_reference_passes_both_seeds(self):
        for task in self.seed1+self.seed2:
            with self.subTest(seed=task.parent.name,task=task.name):
                self.assertEqual(check_task(task,task/'reference/workspace',task/'reference/reply.txt')['pass'],True)

    def test_empty_reply_fails_even_reference_code(self):
        for task in self.seed1+self.seed2:
            with self.subTest(seed=task.parent.name,task=task.name):
                self.assertEqual(check_task(task,task/'reference/workspace',self.empty)['pass'],False)

    def test_original_bugs_and_implementation_stubs_fail(self):
        for task in self.seed1+self.seed2:
            if task.name.startswith(('fix-','implement-')):
                with self.subTest(task=str(task)):
                    self.assertFalse(check_task(task,task/'workspace',task/'reference/reply.txt')['pass'])

    def test_fix_public_answer_lookup_cannot_pass_hidden_cases(self):
        from bench.families import fix
        import random
        for index, template in enumerate(fix.TEMPLATE_IDS):
            prompt, workspace, check, reference, reply = fix.make(random.Random(1), index, 'expert')
            tree = ast.parse(workspace['test_solution.py'])
            vectors = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                           and isinstance(node.func, ast.Name) and node.func.id == 'zip')
            public = ast.literal_eval(vectors.args[0])
            keys = {json.dumps(args, sort_keys=True) for args in public}
            self.assertTrue(any(json.dumps(args, sort_keys=True) not in keys for args in check['cases']))
            task = self.root / ('lookup-' + template)
            task.mkdir()
            (task / 'workspace').mkdir()
            for name, source in workspace.items():
                (task / 'workspace' / name).write_text(source)
            (task / 'check.json').write_text(json.dumps(check))
            (task / 'reply.txt').write_text(reply)
            lookup = ("import ast, copy, json\nfrom pathlib import Path\n"
                      "tree = ast.parse(Path(__file__).with_name('test_solution.py').read_text())\n"
                      "vectors = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) "
                      "and isinstance(n.func, ast.Name) and n.func.id == 'zip')\n"
                      "known = {json.dumps(a, sort_keys=True): b for a,b in zip("
                      "ast.literal_eval(vectors.args[0]), ast.literal_eval(vectors.args[1]))}\n"
                      "def %s(*args):\n"
                      "    return copy.deepcopy(known.get(json.dumps(list(args), sort_keys=True)))\n") % check['function']
            (task / 'workspace/solution.py').write_text(lookup)
            with self.subTest(template=template):
                self.assertFalse(check_task(task, task / 'workspace', task / 'reply.txt')['pass'])

    def test_fix_test_change_add_delete_rejected(self):
        for change in ('change','add','delete','nested_add','renamed_test'):
            task,workspace,reply = self.clone('fix-1')
            test = workspace/'test_solution.py'
            if change == 'change':
                test.write_text('import unittest\n',encoding='utf-8')
            elif change == 'add':
                (workspace/'test_other.py').write_text('pass\n',encoding='utf-8')
            elif change == 'delete':
                # Test scratch directory is outside user folders.
                test.unlink()
            elif change == 'renamed_test':
                test.rename(workspace/'ignored.py')
            else:
                (workspace/'nested').mkdir()
                (workspace/'nested/test_added.py').write_text('pass\n',encoding='utf-8')
            with self.subTest(change=change):
                self.assertFalse(check_task(task,workspace,reply)['pass'])

    def test_implementation_hidden_tests_leave_workspace_untouched(self):
        task,workspace,reply = self.clone('implement-8')
        before = snapshot(workspace)
        self.assertTrue(check_task(task,workspace,reply)['pass'])
        self.assertEqual(snapshot(workspace),before)
        self.assertFalse(any(p.name.startswith('test') for p in workspace.rglob('*')))

    def test_malformed_reply_and_types(self):
        for family in ('review','repo-qa','reasoning','extraction'):
            task,workspace,reply = self.clone(family+'-1')
            for malformed in ('null','[]','{','{"answer": NaN}','{"answer":1,"answer":2}'):
                reply.write_text(malformed,encoding='utf-8')
                with self.subTest(family=family,reply=malformed):
                    self.assertFalse(check_task(task,workspace,reply)['pass'])
        task,workspace,reply = self.clone('reasoning-1')
        reply.write_text('{"answer":true}',encoding='utf-8')
        self.assertFalse(check_task(task,workspace,reply)['pass'])
        task,workspace,reply = self.clone('extraction-1')
        obj = json.loads(reply.read_text())
        obj['quantity'] = str(obj['quantity'])
        reply.write_text(json.dumps(obj),encoding='utf-8')
        self.assertFalse(check_task(task,workspace,reply)['pass'])

    def test_extraction_prompts_supply_real_json_schema(self):
        primitive_types = {'string':str,'integer':int,'boolean':bool,'null':type(None)}
        for task in self.seed1+self.seed2:
            if not task.name.startswith('extraction-'):
                continue
            prompt = (task/'PROMPT.md').read_text()
            encoded = prompt.split('JSON Schema: ',1)[1]
            schema,end = json.JSONDecoder().raw_decode(encoded)
            expected = json.loads((task/'check.json').read_text())['expected']
            with self.subTest(task=str(task)):
                self.assertEqual(schema['type'],'object')
                self.assertEqual(schema['additionalProperties'],True)
                self.assertEqual(set(schema['required']),set(expected))
                self.assertEqual(len(schema['required']),len(set(schema['required'])))
                self.assertEqual(set(schema['properties']),set(expected))
                for field,value in expected.items():
                    constraint = schema['properties'][field]
                    self.assertIsInstance(constraint,dict)
                    declared_type = constraint['type']
                    if declared_type == 'array':
                        self.assertIs(type(value),list)
                        self.assertEqual(constraint['items'],{'type':'string'})
                        self.assertTrue(all(type(item) is str for item in value))
                    else:
                        self.assertIn(declared_type,primitive_types)
                        self.assertIs(type(value),primitive_types[declared_type])

    def test_extraction_boolean_integer_and_nested_types(self):
        task = next(t for t in self.seed1 if t.name.startswith('extraction-') and
                    {'expedited', 'quantity', 'tags', 'shipping'} <= set(json.loads((t/'check.json').read_text())['expected']))
        task,workspace,reply = self.clone(task.name)
        correct = json.loads(reply.read_text())
        for field,bad in [('expedited',int(correct['expedited'])),('quantity',True),('tags','fragile,priority'),('shipping','null')]:
            changed = dict(correct)
            changed[field] = bad
            reply.write_text(json.dumps(changed),encoding='utf-8')
            with self.subTest(field=field):
                self.assertFalse(check_task(task,workspace,reply)['pass'])
        correct['extra'] = 'allowed'
        reply.write_text(json.dumps(correct),encoding='utf-8')
        self.assertTrue(check_task(task,workspace,reply)['pass'])

    def test_answer_normalization_and_final_line(self):
        task,workspace,reply = self.clone('repo-qa-1')
        obj = json.loads(reply.read_text())
        obj['answer'] = ' '+obj['answer'].upper().replace(':',' : ')+' '
        reply.write_text('Here is the entry.\n'+json.dumps(obj)+'\n\n',encoding='utf-8')
        self.assertTrue(check_task(task,workspace,reply)['pass'])
        reply.write_text(json.dumps(obj)+'\nextra commentary\n',encoding='utf-8')
        verdict = check_task(task,workspace,reply)
        self.assertTrue(verdict['pass'])
        self.assertFalse(verdict['strict_pass'])
        self.assertEqual(verdict['format_issue'], 'extra prose')

    def test_review_line_tolerance_and_path_guards(self):
        task,workspace,reply = self.clone('review-1')
        expected = json.loads(reply.read_text())
        for delta in (-2,-1,0,1,2):
            obj = dict(expected,bug_line=expected['bug_line']+delta)
            reply.write_text(json.dumps(obj),encoding='utf-8')
            with self.subTest(delta=delta):
                self.assertEqual(check_task(task,workspace,reply)['pass'],abs(delta)<=1)
        for file in ('../'+expected['bug_file'],'/'+expected['bug_file'],'lib/../'+expected['bug_file']):
            reply.write_text(json.dumps(dict(expected,bug_file=file)),encoding='utf-8')
            self.assertFalse(check_task(task,workspace,reply)['pass'])
        reply.write_text(json.dumps(dict(expected,bug_line=True)),encoding='utf-8')
        self.assertFalse(check_task(task,workspace,reply)['pass'])

    def test_workspace_and_reply_symlinks_rejected(self):
        task,workspace,reply = self.clone('fix-1')
        (workspace/'escape.py').symlink_to(task/'check.json')
        self.assertFalse(check_task(task,workspace,reply)['pass'])
        task,workspace,reply = self.clone('review-1')
        link = reply.parent/'link.txt'
        link.symlink_to(reply)
        self.assertFalse(check_task(task,workspace,link)['pass'])

    def test_unittest_assertion_monkeypatch_cannot_fake_correct_values(self):
        for family in ('fix','implement'):
            task,workspace,reply = self.clone(family+'-1')
            function = json.loads((task/'check.json').read_text())['function']
            source = ('import unittest\n'
                      'unittest.TestCase.assertTrue = lambda *a, **kw: None\n'
                      'def %s(*args):\n    return None\n') % function
            (workspace/'solution.py').write_text(source,encoding='utf-8')
            output = check_task(task,workspace,reply)
            with self.subTest(family=family):
                self.assertFalse(output['pass'])
                self.assertEqual(output['reason'],'behavioral values or input mutation differ')
                self.assertEqual((workspace/'test_solution.py').read_bytes() if family == 'fix' else b'',
                                 (task/'workspace/test_solution.py').read_bytes() if family == 'fix' else b'')

    def test_independent_behavior_checks_input_mutation(self):
        task,workspace,reply = self.clone('implement-2')
        function = json.loads((task/'check.json').read_text())['function']
        reference = (workspace/'solution.py').read_text().replace('def '+function+'(', 'def original(')
        source = ('import unittest\n'
                  'unittest.TestCase.assertEqual = lambda *a, **kw: None\n') + reference
        source += ('def %s(*args):\n'
                   '    answer = original(*args)\n'
                   '    def mutate(value):\n'
                   '        if isinstance(value, list): value.append(1); return True\n'
                   '        if isinstance(value, dict): value["unexpected"] = 1; return True\n'
                   '        return False\n'
                   '    for value in args:\n'
                   '        if mutate(value): break\n'
                   '    return answer\n') % function
        (workspace/'solution.py').write_text(source,encoding='utf-8')
        output = check_task(task,workspace,reply)
        self.assertFalse(output['pass'])
        self.assertEqual(output['reason'],'behavioral values or input mutation differ')

    def test_early_process_exit_cannot_fake_green_tests(self):
        task,workspace,reply = self.clone('implement-1')
        (workspace/'solution.py').write_text('import os\nos._exit(0)\n',encoding='utf-8')
        self.assertFalse(check_task(task,workspace,reply)['pass'])

    def test_candidate_stdlib_shadow_does_not_replace_trusted_unittest(self):
        task,workspace,reply = self.clone('fix-1')
        shutil.copyfile(task/'workspace/solution.py',workspace/'solution.py')
        (workspace/'unittest.py').write_text('raise RuntimeError("candidate unittest was imported")\n',encoding='utf-8')
        output = check_task(task,workspace,reply)
        self.assertFalse(output['pass'])
        self.assertIn('trusted tests failed',output['reason'])

    def test_descendant_with_inherited_pipes_is_timed_out(self):
        if not hasattr(__import__('os'),'fork'):
            self.skipTest('POSIX process groups required')
        task,workspace,reply = self.clone('implement-1')
        (workspace/'solution.py').write_text('import os, time\nif os.fork() == 0:\n    time.sleep(30)\n    os._exit(0)\n'+(workspace/'solution.py').read_text(),encoding='utf-8')
        with mock.patch('bench.check.CODE_TIMEOUT_SECONDS',0.3):
            output = check_task(task,workspace,reply)
        self.assertFalse(output['pass'])
        self.assertEqual(output['reason'],'tests timed out')

    def test_reference_helper(self):
        task,workspace,reply = self.clone('extraction-1')
        empty = workspace.parent/'empty'
        target_reply = workspace.parent/'copied.txt'
        materialize(task,empty,target_reply)
        self.assertTrue(check_task(task,empty,target_reply)['pass'])
        with self.assertRaises(ValueError):
            materialize(task,empty,target_reply)

    def test_checker_cli_one_json_line_and_exit_zero(self):
        task = self.seed1[0]
        for argv,passed in [([str(task),str(task/'reference/workspace'),str(task/'reference/reply.txt')],True),
                            ([str(task),str(task/'reference/workspace'),str(self.empty)],False),
                            (['missing','missing','missing'],False),([],False)]:
            proc = subprocess.run([sys.executable,str(ROOT/'bench/check.py')]+argv,capture_output=True,text=True)
            with self.subTest(argv=argv):
                self.assertEqual(proc.returncode,0,proc.stderr)
                self.assertEqual(proc.stderr,'')
                self.assertEqual(len(proc.stdout.splitlines()),1)
                data = json.loads(proc.stdout)
                self.assertEqual(set(data),{'pass','reason','strict_pass','format_ok'})
                self.assertEqual(data['strict_pass'], passed)
                self.assertEqual(data['pass'],passed)

    def test_count_option_and_existing_task_rejection(self):
        root = self.root/'larger'
        tasks = generate(9,root,per_family=17)
        self.assertEqual(len(tasks),102)
        with self.assertRaises(ValueError):
            generate(9,root,per_family=17)
        with self.assertRaises(ValueError):
            generate(9,self.root/'invalid',per_family=0)


if __name__ == '__main__':
    unittest.main()
