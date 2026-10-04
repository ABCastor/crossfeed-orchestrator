import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from bench import cost, selector_policy, split, to_evidence


def task_for(part):
    return next('21:folder-%d' % i for i in range(100) if split.task_split('folder-%d' % i) == part)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.results = self.root / 'results.jsonl'
        self.fit, self.heldout = task_for('fit'), task_for('heldout')
        self.row = dict(task=self.fit, option='sol-high', pool='codex', model='gpt-6.1-sol',
                        level='high', family='implement', tier='hard', pass_=True, duration_s=10,
                        tokens_in=20, tokens_out=5)
        self.row['pass'] = self.row.pop('pass_')
        self.levels = {'assumptions': {}, 'rows': [{'model_key': 'gpt-6.1-sol', 'level': 'high',
                         'own': {'coding-agent': {'passes': 99, 'trials': 100}},
                         'own_cost': {'codex': {'percent_per_task': 100}},
                         'q': {'coding-agent': {'mean': .99, 'sd': .01, 'sources': [], 'prior_strength': 0}}}]}

    def write(self, rows):
        self.results.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def test_split_hashes_folder_not_seed_or_path(self):
        folder = 'gen-fix-1'
        expected = 'heldout' if int(hashlib.sha256(folder.encode()).hexdigest(), 16) % 2 else 'fit'
        self.assertEqual(split.task_split('21:' + folder), expected)
        self.assertEqual(split.task_split('999:' + folder), expected)
        self.assertEqual(split.task_split(folder), expected)
        with self.assertRaises(ValueError):
            split.task_split('a/b')

    def test_heldout_payload_never_accessed(self):
        # Poisoned payload is not a valid measurement. Fit ingestion must not
        # inspect even its missing option, family, pass or identity fields.
        self.write([self.row, {'task': self.heldout, 'pass': 'POISON', 'model': {}}])
        result = to_evidence.build_evidence([self.results], self.levels)
        row = result['rows'][0]
        self.assertEqual(row['own']['coding-agent'], {'passes': 1, 'trials': 1})
        self.assertEqual(result['bench']['fit_cells'], 1)
        self.assertEqual(row['q']['coding-agent']['mean'], 2 / 3)
        self.assertEqual(row['own_cost'], {})
        self.assertEqual(self.levels['rows'][0]['q']['coding-agent']['mean'], .99)

    def test_changing_valid_heldout_outcomes_does_not_change_evidence(self):
        held = dict(self.row, task=self.heldout)
        self.write([self.row, held])
        before = to_evidence.build_evidence([self.results], self.levels)
        held.update({'pass': False, 'tokens_in': 999999, 'duration_s': 9999, 'family': 'review'})
        self.write([self.row, held])
        self.assertEqual(to_evidence.build_evidence([self.results], self.levels), before)

    def test_external_prior_is_rebuilt_not_old_posterior(self):
        q = {'mean': .999, 'sd': .001, 'sources': [{'prior_mean': .75}], 'prior_strength': 4}
        result = to_evidence.posterior(q, {'passes': 0, 'trials': 2}, {})
        self.assertEqual(result['mean'], .5)
        self.assertFalse(result['unknown'])

    def test_costs_require_fit_provenance_and_keep_exact_pool(self):
        self.write([self.row])
        doc = {'schema': 'crossfeed-quota-cost/v1', 'split': 'fit', 'options': [
            {'option': 'sol-high', 'pool': 'codex', 'percent_binding_window_per_task': .25, 'unmeasured_tasks': 0}]}
        result = to_evidence.build_evidence([self.results], self.levels, doc)
        self.assertEqual(result['rows'][0]['own_cost']['codex']['percent_per_task'], .25)
        doc['split'] = 'heldout'
        with self.assertRaises(ValueError):
            to_evidence.build_evidence([self.results], self.levels, doc)

    def test_family_merge_duplicates_and_identity(self):
        self.write([self.row, self.row, dict(self.row, task=self.fit + '-other', family='history-fix')])
        # Choose a second verified fit folder rather than depend on its hash.
        second = next('21:extra%d' % i for i in range(100) if split.task_split('extra%d' % i) == 'fit')
        self.write([self.row, self.row, dict(self.row, task=second, family='history-fix')])
        result = to_evidence.build_evidence([self.results], self.levels)
        self.assertEqual(result['rows'][0]['own']['coding-agent']['trials'], 2)
        self.assertEqual(to_evidence.model_key('opencode-go/deepseek-v4.1-flash', 'high'), 'deepseek-v4.1-flash')
        self.assertEqual(to_evidence.model_key('gemini-3.8-flash-high', 'high'), 'gemini-3.8-flash')

    def test_live_replay_contract_and_unmeasured_choice(self):
        held = dict(self.row, task=self.heldout, family='review', tier='expert')
        self.write([self.row, held])
        template = self.root / 'template'
        (template / 'evidence').mkdir(parents=True)
        (template / 'evidence/levels.json').write_text(json.dumps(self.levels))
        (template / 'runtime.json').write_text(json.dumps({'schema': 'fixture', 'quota_snapshots': {}}))
        script = self.root / 'fake.py'
        script.write_text('fixture')
        template_before = (template / 'runtime.json').read_bytes()
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            state = Path(kwargs['env']['FLEET_STATE_DIR'])
            self.assertNotEqual(state, template)
            self.assertEqual(json.loads((state / 'evidence/levels.json').read_text())['bench']['fit_cells'], 1)
            self.assertIn('--no-refresh', command)
            self.assertEqual(command[command.index('--role') + 1], 'review')
            self.assertEqual(command[command.index('--stakes') + 1], 'high')
            return subprocess.CompletedProcess(command, 0, json.dumps({'choice': choice, 'top3': []}), '')
        choice = {'pool': 'codex', 'model_key': 'gpt-6.1-sol', 'level': 'high'}
        result = selector_policy.run_policy([self.results], template, 'now', script=script, runner=runner)
        self.assertEqual(result['mapping'][self.heldout], 'sol-high')
        choice['level'] = 'low'
        result = selector_policy.run_policy([self.results], template, 'codex-tight', script=script, runner=runner)
        self.assertIsNone(result['mapping'][self.heldout])
        self.assertEqual(result['status'][self.heldout]['status'], 'unmeasured_choice')
        self.assertEqual((template / 'runtime.json').read_bytes(), template_before)
        self.assertEqual(len(calls), 2)

    def test_frozen_hook_exclusion_applies_to_fit_and_replay(self):
        (self.root / 'excluded-hook-tasks.txt').write_text(split.task_folder(self.fit) + '\n' + split.task_folder(self.heldout) + '\n')
        self.write([self.row, dict(self.row, task=self.heldout, family='review')])
        result = to_evidence.build_evidence([self.results], self.levels)
        self.assertEqual(result['bench']['fit_cells'], 0)
        self.assertEqual(result['rows'][0]['own']['coding-agent']['trials'], 0)
        self.assertEqual(list(split.iter_split_rows([self.results], 'heldout')), [])
        self.assertEqual(result['bench']['excluded_task_folders'], sorted([split.task_folder(self.fit), split.task_folder(self.heldout)]))

    def test_excluded_attempt_makes_batch_cost_unknown(self):
        from bench.tests.test_brief4_cost import fleet_usage
        snapshots = []
        for when, used, captured in (("before", 30, "2026-10-02T10:00:00+00:00"),
                                     ("after", 34, "2026-10-02T10:02:00+00:00")):
            path = self.root / (when + ".json")
            path.write_text(json.dumps({"status": "ok", "captured_at": captured,
                                        "data": fleet_usage("codex", used, captured)}))
            snapshots.append(str(path))
        second = next('21:extra%d' % i for i in range(100) if split.task_split('extra%d' % i) == 'fit')
        row = dict(self.row, pool_usage={"before": snapshots[0], "after": snapshots[1]})
        self.write([row, dict(row, task=second, excluded=True)])
        result = to_evidence.build_evidence([self.results], self.levels)
        self.assertEqual(result['rows'][0]['own']['coding-agent']['trials'], 1)
        self.assertEqual(result['rows'][0]['own_cost'], {})
        self.assertEqual(result['bench']['fit_attempted_cells'], 2)

    def test_recorded_identity_mismatch_is_unmeasured(self):
        held = dict(self.row, task=self.heldout, model='wrong-model', level='low')
        self.write([self.row, held])
        options = self.root / 'options.json'
        options.write_text(json.dumps({'options': [{'id': 'sol-high', 'pool': 'codex',
                                     'model': 'gpt-6.1-sol', 'level': 'high'}]}))
        template = self.root / 'template'
        (template / 'evidence').mkdir(parents=True)
        (template / 'evidence/levels.json').write_text(json.dumps(self.levels))
        (template / 'runtime.json').write_text('{}')
        script = self.root / 'fake.py'
        script.write_text('fixture')
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, json.dumps({'choice': {
                'pool': 'codex', 'model_key': 'gpt-6.1-sol', 'level': 'high'}}), '')
        result = selector_policy.run_policy([self.results], template, 'now', [options], script=script, runner=runner)
        self.assertIsNone(result['mapping'][self.heldout])
        self.assertEqual(result['status'][self.heldout]['status'], 'unmeasured_choice')

    def test_scenarios_copy_and_replace_only_target_pool(self):
        from datetime import datetime, timezone
        original = {'quota_snapshots': {'claude': {'windows': {}}, 'codex': {'windows': {}}},
                    'pool_circuits': {'codex': {'until': 'future'}}, 'switches': {'codex': 'forced'}}
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        result = selector_policy.scenario_runtime(original, 'codex-tight', now)
        self.assertEqual(result['quota_snapshots']['codex']['windows']['secondary']['used_percent'], 95)
        self.assertEqual(result['quota_snapshots']['claude'], original['quota_snapshots']['claude'])
        self.assertEqual(original['switches']['codex'], 'forced')
        self.assertEqual(result['switches']['codex'], 'normal')
