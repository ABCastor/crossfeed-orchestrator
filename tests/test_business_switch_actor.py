"""BR-SWITCH-001: real control boundaries in an isolated fleet."""
import importlib.util
import json
import subprocess
import unittest
from unittest import mock

from tests.test_switch_respected import Sandbox, HOOK, SCRIPTS
from tests.test_console import console

spec = importlib.util.spec_from_file_location('switch_guard_actor', HOOK)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class SwitchActorTests(Sandbox):
    # BR-SWITCH-001
    def runtime(self):
        return json.loads((self.state / 'runtime.json').read_text())

    def denied(self, *args):
        before = (self.state / 'runtime.json').read_bytes()
        result = self.fleet(*args, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('ask the owner', result.stderr)
        self.assertEqual((self.state / 'runtime.json').read_bytes(), before)
        return result

    def bash(self, command):
        result = subprocess.run(['python3', str(HOOK)], env=self.env, text=True,
            input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}}),
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['hookSpecificOutput']['permissionDecisionReason'] if result.stdout.strip() else None

    def test_owner_off_survives_agent_off_and_all_enable_forms(self):
        # BR-SWITCH-001: the persisted state is the prohibition checkpoint.
        self.fleet('model-toggle', 'codex', 'gpt-6-astra', 'off', '--owner', '--reason', 'save it')
        self.off('codex', 'gpt-6-astra')
        self.denied('model-toggle', 'codex', 'gpt-6-astra', 'on')
        self.denied('model-choice', 'codex', 'auto')
        self.denied('model-choice', 'codex', 'gpt-6-astra')
        state = self.runtime()
        self.assertEqual(state['control_records']['model:codex:gpt-6-astra']['actor'], 'agent')
        self.assertEqual(state['control_limits']['model:codex:gpt-6-astra']['reason'], 'save it')
        self.fleet('model-toggle', 'codex', 'gpt-6-astra', 'on', '--owner')
        self.assertNotIn('gpt-6-astra', self.runtime()['model_toggles'].get('codex', []))

    def test_agent_off_can_be_reenabled_and_history_is_preserved(self):
        # BR-SWITCH-001: permitted control proves enabling is observable.
        self.off('codex', 'gpt-6-astra')
        before = self.runtime()['control_history']
        self.fleet('model-toggle', 'codex', 'gpt-6-astra', 'on', '--reason', 'capacity')
        state = self.runtime()
        self.assertNotIn('gpt-6-astra', state['model_toggles'].get('codex', []))
        self.assertEqual(state['control_history'][:len(before)], before)
        record = state['control_records']['model:codex:gpt-6-astra']
        self.assertEqual((record['actor'], record['reason']), ('agent', 'capacity'))
        self.assertIn('T', record['when'])

    def test_levels_protect_owner_ceiling_without_blocking_tightening(self):
        # BR-SWITCH-001
        self.fleet('level', 'codex', 'low', '--owner')
        self.denied('level', 'codex', 'high')
        self.denied('switch', 'codex', 'auto')
        self.denied('profile', 'max-quality', '--because', 'codex')
        self.fleet('level', 'codex', 'off')
        self.denied('level', 'codex', 'normal')
        self.fleet('level', 'codex', 'low')  # still within the owner's ceiling
        self.fleet('level', 'codex', 'high', '--owner')
        self.fleet('level', 'codex', 'off')
        self.fleet('level', 'codex', 'normal')
        self.assertNotIn('codex', self.runtime()['switches'])

    def test_guard_redirects_and_explicit_state_cannot_bypass_owner(self):
        # BR-SWITCH-001
        self.fleet('level', 'opencode-go', 'off', '--owner')
        self.fleet('model-toggle', 'codex', 'gpt-6-astra', 'off', '--owner')
        commands = ['python3 scripts/fleetctl.py level opencode-go low',
                    'fleetctl.py model-toggle codex gpt-6-astra on',
                    'fleetctl.py model-choice codex auto']
        for command in commands:
            for redirect in ['', ' 2>&1 | tail -2', ' >file', ' 2>/dev/null',
                             ' > "file with spaces" 2>&1 | tail -1', ' &>/dev/null']:
                with self.subTest(command=command, redirect=redirect):
                    reason = self.bash(command + redirect)
                    self.assertIn('the owner switched this off on ', reason)
                    self.assertIn('ask the owner', reason)
        explicit = f'FLEET_STATE_DIR={self.state} fleetctl.py --overlay {self.overlay} level opencode-go high 2>&1 | tail -1'
        self.assertIn('ask the owner', self.bash(explicit))
        self.assertIsNone(self.bash('fleetctl.py level opencode-go off 2>&1 | tail -1'))
        self.off('codex', 'gpt-6.1-sol')
        self.assertIsNone(self.bash('fleetctl.py model-toggle codex gpt-6.1-sol on 2>&1 | tail -1'))

    def test_redirects_before_and_between_arguments_and_reason_flags(self):
        # BR-SWITCH-001
        self.fleet('level', 'codex', 'off', '--owner')
        for command in [
            '2>/dev/null fleetctl.py level codex high',
            'true;2>&1 fleetctl.py level codex high',
            'fleetctl.py 2>/dev/null level codex high',
            'fleetctl.py level codex high --reason "capacity 2>&1" 2>&1 | tail -1',
            'fleetctl.py level codex high &>>file',
            'fleetctl.py level codex high 2>& 1',
        ]:
            with self.subTest(command=command):
                self.assertIn('ask the owner', self.bash(command))

    def test_console_records_owner_and_renders_actor_with_time(self):
        # BR-SWITCH-001
        app = console.Console(self.overlay, self.state, 0)
        app.set_level('codex', 'low')
        app.set_toggle('codex', 'gpt-6-astra', False)
        self.off('codex', 'gpt-6.1-sol')
        state = self.runtime()
        self.assertEqual(state['control_records']['level:codex']['actor'], 'owner')
        self.assertEqual(state['control_records']['model:codex:gpt-6-astra']['actor'], 'owner')
        with mock.patch.dict('os.environ', self.env, clear=True):
            overview = app.overview(refresh=False)
        page = console.render_page(overview, app.form_token)
        self.assertRegex(page, r'level-actor">you, \d+ \w+ \d\d:\d\d')
        self.assertRegex(page, r'model-actor">agent, \d+ \w+ \d\d:\d\d')
        payload = console.snapshot_payload(overview)
        codex = next(p for p in payload['pools'] if p['pool'] == 'codex')
        self.assertTrue(codex['control_actor'].startswith('you, '))
        self.assertTrue(codex['model_actors']['gpt-6.1-sol'].startswith('agent, '))
        self.denied('level', 'codex', 'high')
        app.set_choice('codex', 'auto')
        self.assertEqual(self.runtime()['control_records']['model:codex:gpt-6-astra']['actor'], 'owner')

    def test_redirect_parser_preserves_quoted_text_and_pipeline_commands(self):
        self.assertEqual(guard.segments('fleetctl.py level codex low 2>&1 | tail -2'),
                         [['fleetctl.py', 'level', 'codex', 'low'], ['tail', '-2']])
        self.assertEqual(guard.segments('echo "2>&1"; fleetctl.py level codex high>file'),
                         [['echo', '2>&1'], ['fleetctl.py', 'level', 'codex', 'high']])

    def pin_fixture(self):
        pin = self.root / 'seat.toml'
        pin.write_text('model = "gpt-6-astra"\n')
        roster = self.roster
        roster['quota_pools']['codex']['model_pins'] = [
            {'path': str(pin), 'key': 'model', 'format': 'toml', 'manage': True}]
        self.write_roster(roster)
        return pin

    def test_pin_undo_cannot_restore_owner_off_and_records_actor(self):
        # BR-SWITCH-001
        pin = self.pin_fixture()
        self.fleet('model-toggle', 'codex', 'gpt-6-astra', 'off', '--owner', '--reason', 'owner pin follows')
        self.assertNotIn('gpt-6-astra', pin.read_text())
        before = pin.read_bytes()
        pin_records = [r for k, r in self.runtime()['control_records'].items() if k.startswith('pin:')]
        self.assertEqual(pin_records[0]['actor'], 'owner')
        self.assertEqual(pin_records[0]['reason'], 'owner pin follows')
        self.denied('pins', 'undo')
        self.assertEqual(pin.read_bytes(), before)
        self.assertIn('ask the owner', self.bash('fleetctl.py pins undo 2>&1 | tail -1'))
        self.fleet('pins', 'undo', '--owner')
        self.assertIn('gpt-6-astra', pin.read_text())

    def test_agent_pin_undo_is_allowed_under_owner_low_level(self):
        # BR-SWITCH-001: permitted control for the same pin restoration boundary.
        pin = self.pin_fixture()
        self.fleet('level', 'codex', 'low', '--owner')
        self.off('codex', 'gpt-6-astra')
        self.fleet('pins', 'undo', '--reason', 'restore agent choice')
        self.assertIn('gpt-6-astra', pin.read_text())
        records = [r for k, r in self.runtime()['control_records'].items() if k.startswith('pin:')]
        self.assertEqual(records[0]['actor'], 'agent')
        self.assertEqual(records[0]['reason'], 'restore agent choice')

    def test_legacy_console_preference_also_protects_owner_off(self):
        # BR-SWITCH-001
        app = console.Console(self.overlay, self.state, 0)
        app.set_preference('gpt-6-astra', 'off')
        self.denied('model-toggle', 'codex', 'gpt-6-astra', 'on')
        self.assertEqual(self.runtime()['model_preferences']['gpt-6-astra'], 'off')
        app.set_toggle('codex', 'gpt-6-astra', True)
        self.assertNotIn('gpt-6-astra', self.runtime()['model_toggles'].get('codex', []))

    def test_profile_reason_does_not_replace_explicit_pool_authorization(self):
        # BR-SWITCH-001
        self.fleet('level', 'codex', 'off')
        self.fleet('profile', 'max-quality', '--because', 'codex', '--reason', 'extra capacity')
        record = self.runtime()['control_records']['level:codex']
        self.assertEqual((record['actor'], record['value'], record['reason']),
                         ('agent', 'high', 'extra capacity'))
        self.fleet('level', 'codex', 'off', '--owner')
        self.denied('profile', 'max-quality', '--because', 'codex', '--reason', 'extra capacity')

