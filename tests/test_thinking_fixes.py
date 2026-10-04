"""Unit B regressions: real dispatch code, private runtime, no model calls."""
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from tests.test_fleetctl import fleetctl

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
OVERLAY = ROOT / 'tests' / 'fixtures' / 'access-overlay.test.json'


class ThinkingFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = json.loads(OVERLAY.read_text())
        self.env = dict(os.environ, ACCESS_OVERLAY=str(OVERLAY),
                        FLEET_STATE_DIR=str(self.root / 'state'),
                        CODEX_HOME=str(self.root / 'codex'), FLEET_NO_AUTO_REFRESH='1')
        Path(self.env['CODEX_HOME']).mkdir()
        (Path(self.env['CODEX_HOME']) / 'config.toml').write_text('model = "gpt-6.1-sol"\n')

    def run_script(self, script, *args):
        return subprocess.run([str(SCRIPTS / script), *args], env=self.env,
                              text=True, capture_output=True)

    def codex(self, *args):
        return self.run_script('codex-agent.sh', '--dry-run', '--dir', str(self.root),
                               '--prompt', 'synthetic task', *args)

    def test_codex_config_model_precedes_standin_and_effort(self):
        for value in ('model=gpt-6-luna', 'model = "gpt-6-luna"', "model = 'gpt-6-luna'"):
            with self.subTest(value=value):
                run = self.codex('-c', value)
                self.assertEqual(run.returncode, 0, run.stderr)
                argv = shlex.split(run.stdout)
                self.assertEqual(argv[argv.index('-m') + 1], 'gpt-6-luna')
                self.assertIn('roster gpt-6-luna/default', run.stderr)
        run = self.codex('-c', 'model=gpt-6-astra', '-c', 'model=gpt-6-luna')
        self.assertEqual(shlex.split(run.stdout)[shlex.split(run.stdout).index('-m') + 1], 'gpt-6-luna')
        run = self.codex('-c', 'model=gpt-6-luna', '--model', 'gpt-6.1-sol')
        self.assertEqual(shlex.split(run.stdout)[shlex.split(run.stdout).index('-m') + 1], 'gpt-6.1-sol')

    def test_google_supported_variants_are_pinned(self):
        for key, levels in [('gemini-flash-latest', ['low', 'medium', 'high']),
                            ('gemini-flash-lite-latest', ['minimal', 'low', 'medium', 'high']),
                            ('gemini-3.1-pro-preview', ['low', 'medium', 'high'])]:
            with self.subTest(model=key):
                entry = self.roster['effort'][key]
                self.assertEqual(entry['levels']['opencode'], levels)
                self.assertIn(entry['default'], levels)
                self.assertEqual(entry['evidence']['status'], 'unmeasured')
                for level in levels:
                    self.assertEqual(fleetctl.resolve_effort(self.roster, key, harness='opencode',
                                                            explicit=level)['effort'], level)

    def test_tool_source_directory_disables_live_mcp_after_caller_config(self):
        tool = self.root / 'browser-tool'
        nested = tool / 'src'
        nested.mkdir(parents=True)
        self.roster['policy']['codex_mcp_denials'] = {'browser_tool': [str(tool)]}
        overlay = self.root / 'overlay.json'
        overlay.write_text(json.dumps(self.roster))
        self.env['ACCESS_OVERLAY'] = str(overlay)
        for directory in (tool, nested):
            run = self.run_script('codex-agent.sh', '--dry-run', '--dir', str(directory),
                                  '--prompt', 'synthetic', '-c', 'mcp_servers.browser_tool.enabled=true')
            self.assertEqual(run.returncode, 0, run.stderr)
            argv = shlex.split(run.stdout)
            flags = [v for v in argv if v.startswith('mcp_servers.browser_tool.enabled=')]
            self.assertEqual(flags, ['mcp_servers.browser_tool.enabled=true', 'mcp_servers.browser_tool.enabled=false'])
            self.assertIn('live MCP tool browser_tool disabled', run.stderr)
        run = self.codex()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn('mcp_servers.browser_tool.enabled=false', run.stdout)
        self.assertNotIn('live MCP tool', run.stderr)

    def test_invalid_tool_denial_config_refuses_launch(self):
        for rules in ({'bad.name': [str(self.root)]}, {'browser_tool': ['relative/path']}, []):
            self.roster['policy']['codex_mcp_denials'] = rules
            overlay = self.root / 'overlay.json'
            overlay.write_text(json.dumps(self.roster))
            self.env['ACCESS_OVERLAY'] = str(overlay)
            run = self.codex()
            self.assertEqual(run.returncode, 2, run.stderr)
            self.assertEqual(run.stdout, '')

    def test_unsupported_explicit_variant_is_refused_even_without_controls(self):
        for key in ('kimi-k3', 'kimi-k2.7-code'):
            with self.subTest(model=key):
                with self.assertRaisesRegex(fleetctl.FleetError, 'unsupported effort.*supported:'):
                    fleetctl.resolve_effort(self.roster, key, harness='opencode', explicit='unsupported')
                run = self.run_script('fleetctl.py', 'effort', key, '--harness', 'opencode', '--level', 'unsupported')
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertIn('unsupported effort', run.stderr)
                self.assertEqual(run.stdout, '')

    def test_codex_fanout_write_defaults_to_builder_and_forwards_it(self):
        # Only the worker is stubbed. Preflight, task normalization and launch stay real.
        farm = self.root / 'scripts'
        farm.mkdir()
        for path in SCRIPTS.iterdir():
            if path.name != 'codex-agent.sh':
                (farm / path.name).symlink_to(path)
        argv_file = self.root / 'argv'
        stub = farm / 'codex-agent.sh'
        stub.write_text('#!/bin/bash\nset -eu\n'
                        f'printf "%s\\n" "$@" > {shlex.quote(str(argv_file))}\n'
                        'while [ $# -gt 0 ]; do\n'
                        'case "$1" in --last) printf "built\\n" > "$2"; shift 2;; *) shift;; esac\n'
                        'done\n')
        stub.chmod(0o755)
        task_file = self.root / 'tasks.jsonl'
        task = {'id': 'build', 'agent': 'codex', 'mode': 'write',
                'dir': str(self.root), 'prompt': 'synthetic task'}
        task_file.write_text(json.dumps(task) + '\n')
        out = self.root / 'out'
        run = subprocess.run([str(farm / 'fanout.sh'), str(task_file), '--out', str(out)],
                             env=self.env, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        manifest = json.loads((out / 'manifest.jsonl').read_text())
        self.assertEqual(manifest['role'], 'builder')
        argv = argv_file.read_text().splitlines()
        self.assertEqual(argv[argv.index('--role') + 1], 'builder')
        for mode, role, expected in [('read-only', None, 'default'), ('write', 'implementation', 'implementation')]:
            task.update(mode=mode)
            if role:
                task['role'] = role
            task_file.write_text(json.dumps(task) + '\n')
            run = self.run_script('fanout.sh', str(task_file), '--out', str(self.root / f'out-{mode}'), '--dry-run')
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['role'], expected)

    def test_sol_implementation_seat_and_quota_steps(self):
        for role, band, expected in [('implementation', 'quality_first', 'xhigh'),
                                     ('implementation', 'conserve', 'xhigh'),
                                     ('implementation', 'critical', 'high'),
                                     ('builder', 'quality_first', 'high'),
                                     ('builder', 'conserve', 'medium')]:
            self.assertEqual(fleetctl.resolve_effort(self.roster, 'gpt-6.1-sol', role,
                                                   'codex', band=band)['effort'], expected)
        with self.assertRaisesRegex(fleetctl.FleetError, 'CRITICAL'):
            fleetctl.resolve_effort(self.roster, 'gpt-6.1-sol', 'builder', 'codex', band='critical')

    def test_standin_rechecks_explicit_level_and_clamps_sol_to_xhigh(self):
        runtime = {}
        fleetctl.set_model_choice(runtime, self.roster, 'codex', 'gpt-6.1-sol')
        fleetctl.atomic_json(Path(self.env['FLEET_STATE_DIR']) / 'runtime.json', runtime)
        for config in (['--reasoning', 'max'], ['-c', 'model_reasoning_effort="max"'],
                       ['--reasoning', 'ultra']):
            run = self.codex('-c', 'model=gpt-6-astra', *config)
            self.assertEqual(run.returncode, 0, run.stderr)
            argv = shlex.split(run.stdout)
            self.assertEqual(argv[argv.index('-m') + 1], 'gpt-6.1-sol')
            self.assertEqual([v for v in argv if v.startswith('model_reasoning_effort=')][-1],
                             'model_reasoning_effort="xhigh"')
            self.assertIn('clamped', run.stderr)
            self.assertIn('gpt-6.1-sol', run.stderr)
        run = self.codex('--model', 'gpt-6-astra', '--reasoning', 'low')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn('clamped', run.stderr)

    def test_standin_clamps_to_replacement_supported_levels(self):
        self.roster['effort']['gpt-6-luna']['levels']['codex'] = ['low', 'medium', 'high']
        resolved = fleetctl.resolve_effort(self.roster, 'gpt-6-luna', harness='codex',
                                          explicit='xhigh', stand_in=True)
        self.assertEqual(resolved['effort'], 'high')
        self.assertIn('clamped xhigh to high', resolved['reason'])

    def test_roster_doctor_reports_effort_failure_and_continues_picker_checks(self):
        r = self.roster
        del r['effort']['kimi-k3']
        overlay = self.root / 'overlay.json'
        overlay.write_text(json.dumps(r))
        self.env['ACCESS_OVERLAY'] = str(overlay)
        bindir = self.root / 'bin'
        bindir.mkdir()
        marker = self.root / 'providers'
        picker = bindir / 'opencode'
        selectors = {p: [l['selector'] for l in r['lanes'] if l['provider'] == p and l['harness'] == 'opencode']
                     for p in {l['provider'] for l in r['lanes'] if l['harness'] == 'opencode'}}
        picker.write_text('#!/usr/bin/env python3\nimport sys\n'
                          f'from pathlib import Path\nwith Path({str(marker)!r}).open("a") as f: f.write(sys.argv[2] + "\\n")\n'
                          f'print("\\n".join({selectors!r}[sys.argv[2]]))\n')
        picker.chmod(0o755)
        self.env['PATH'] = str(bindir) + os.pathsep + self.env['PATH']
        run = self.run_script('roster.sh', 'doctor')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('kimi-k3: missing effort', run.stderr)
        self.assertTrue(marker.exists(), run.stderr)
        self.assertEqual(set(marker.read_text().splitlines()), set(selectors))
        self.assertIn('doctor FAIL', run.stderr)
        self.assertNotIn('doctor PASS', run.stdout)

    def test_doctor_and_brief_warn_seven_days_before_shared_evidence_deadline(self):
        read_on = dt.date(2026, 10, 2)
        for entry in self.roster['effort'].values():
            entry['evidence']['read_on'] = read_on.isoformat()
        for age, should_warn in [(22, False), (23, True), (30, True)]:
            now = dt.datetime.combine(read_on + dt.timedelta(days=age), dt.time(), dt.timezone.utc)
            issues = fleetctl.effort_problems(self.roster, self.root, now)
            warnings = [i for i in issues if 'recheck due 2026-11-01' in i]
            self.assertEqual(bool(warnings), should_warn, issues)
            brief = fleetctl.render_brief(fleetctl.fleet_overview(self.roster, {}, self.root, now), verbose=True)
            self.assertEqual('recheck due 2026-11-01' in brief, should_warn, brief)
        now += dt.timedelta(days=1)
        self.assertTrue(any('stale effort' in i for i in fleetctl.effort_problems(self.roster, self.root, now)))

    def test_opus_review_and_reviewer_agree_at_high(self):
        for role in ('review', 'reviewer'):
            self.assertEqual(fleetctl.resolve_effort(self.roster, 'claude-opus-5-5', role, 'claude')['effort'], 'high')
        self.assertEqual(fleetctl.resolve_effort(self.roster, 'claude-opus-5-5', 'audit', 'claude')['effort'], 'xhigh')

    def test_openrouter_announces_service_chosen_effort(self):
        # Stop at missing auth, before any HTTP. The notice must not require a
        # successful network call, and must leave the deliverable channel empty.
        farm = self.root / 'scripts'
        farm.mkdir()
        (farm / 'openrouter-agent.sh').symlink_to(SCRIPTS / 'openrouter-agent.sh')
        for helper in ('run-identity.sh', 'run_identity.py'):
            (farm / helper).symlink_to(SCRIPTS / helper)
        roster = farm / 'roster.sh'
        roster.write_text('#!/bin/bash\ncase "$1" in\n'
                          'lane-json) echo \'{"harness":"openrouter","selector":"synthetic/free"}\';;\n'
                          'check-lane) exit 0;; *) exit 2;; esac\n')
        roster.chmod(0o755)
        self.env['OPENROUTER_KEY_FILE'] = str(self.root / 'missing-key')
        run = subprocess.run([str(farm / 'openrouter-agent.sh'), '--lane', 'fake', '--prompt', 'synthetic'],
                             env=self.env, text=True, capture_output=True)
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertEqual(run.stderr.count('effort service-chosen: the service chooses the level'), 1)
        self.assertEqual(run.stdout, '')


if __name__ == '__main__':
    unittest.main()
