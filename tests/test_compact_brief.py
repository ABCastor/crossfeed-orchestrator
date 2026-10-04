"""Compact/verbose brief contracts with isolated selector context and CLI state."""
import contextlib
import copy
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_fleetctl import fleetctl, roster
from tests.test_selector import claude_binding_fixture

NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone.utc)
HEADER = 'pool | level | binding window | used | resets | price | models on'


def overview():
    return {
        'product': 'Crossfeed', 'generated_at': fleetctl.iso(NOW),
        'quota_readings': {'sources': ['fixture'], 'newest_age_s': 60, 'measured_pools': 1},
        'pools': [
            {'pool': 'codex', 'label': 'Codex', 'level': 'high', 'state': 'HEALTHY',
             'quota': {'used_percent': 40, 'window': '7d', 'resets_in_s': 7200},
             'plan': {'name': 'Pro', 'price': 200, 'currency': 'USD', 'billing': 'subscription'},
             'limits': [{'name': 'weekly', 'title': 'Weekly', 'kind': 'window', 'scope': None,
                         'used_percent': 40, 'state': 'healthy', 'reset_at': '2026-10-02T14:00:00Z',
                         'resets': 'today 16:00', 'resets_in_s': 7200}],
             'direct': True,
             'models': {'state': 'one', 'on': ['gpt-6.1-sol'], 'off': ['gpt-6-astra'],
                        'stand_ins': {'gpt-6-astra': 'gpt-6.1-sol'}, 'retired': {}},
             'pins': [{'action': 'held', 'pool': 'codex', 'file': 'builder.toml',
                       'value': 'gpt-6.1-sol', 'original': 'gpt-6-astra'}]},
            {'pool': 'claude', 'label': 'Claude', 'level': 'normal', 'state': 'UNKNOWN',
             'plan': {}, 'models': {'state': 'all', 'on': ['claude-sonnet'], 'off': []}},
        ],
        'effort_problems': ['fixture evidence expired'],
    }


class CompactBriefTests(unittest.TestCase):
    def test_reported_claude_weekly_binding(self):
        r = roster()
        runtime = claude_binding_fixture(NOW)
        runtime['quota_snapshots']['claude']['windows']['primary']['will_last_to_reset'] = True
        with tempfile.TemporaryDirectory() as d, patch.object(fleetctl, 'utc_now', return_value=NOW):
            data = fleetctl.fleet_overview(r, runtime, Path(d), NOW)
            text = fleetctl.render_brief(data, roster=r, runtime=runtime)
        self.assertIn('claude | normal | weekly | 72% | 3d 15h | 5.93 |', text)

    def test_antigravity_measurements_survive_spend_levels(self):
        r = roster()
        for pool, suffix in [('antigravity-gemini', 'gemini'), ('antigravity-3p', '3p')]:
            r['quota_pools'][pool] = {'label': pool}
            for level in ['normal', 'forced', 'off', 'high']:
                name = f'antigravity-quota-summary-{suffix}-weekly'
                runtime = {'switches': {pool: level}, 'quota_snapshots': {pool: {
                    'observed_at': fleetctl.iso(NOW), 'windows': {name: {
                        'used_percent': 72, 'window_minutes': 10080,
                        'reset_at': fleetctl.iso(NOW + dt.timedelta(hours=87)),
                    }}}}}
                with self.subTest(pool=pool, level=level), tempfile.TemporaryDirectory() as d, \
                     patch.object(fleetctl, 'utc_now', return_value=NOW):
                    data = fleetctl.fleet_overview(r, runtime, Path(d), NOW)
                    text = fleetctl.render_brief(data, roster=r, runtime=runtime)
                self.assertIn(f'{pool} | {level} | weekly | 72% | 3d 15h | 5.93 |', text)

    def test_compact_golden_and_no_overview_mutation(self):
        data = overview()
        original = copy.deepcopy(data)
        self.assertEqual(fleetctl.render_brief(data), '\n'.join([
            HEADER,
            'codex | high | weekly | 40% | 2h 0m | - | gpt-6.1-sol',
            'claude | normal | - | - | - | - | claude-sonnet',
            'off: codex/gpt-6-astra -> gpt-6.1-sol',
            'pick: fleetctl.py select --role R [--stakes S]',
        ]))
        self.assertEqual(data, original)

    def test_verbose_contains_every_legacy_line(self):
        stamp = NOW.astimezone().strftime('%H:%M')
        expected = [
            f'Crossfeed brief {stamp} · quota from fixture, newest 1m old · ≈ is an estimate',
            'HIGH (strong models, full parallel until critical): codex 40%/7d Pro $200/mo',
            'NORMAL (follow the quota gauges): claude',
            "Limits (used, then when each resets; times are this machine's):",
            '  codex: weekly 40%, resets today 16:00 (in 2h 0m)',
            'Switched off: codex gpt-6-astra (gpt-6.1-sol runs instead)',
            'Never name a switched-off model in a command or a report: name the one that runs.',
            'Seats moved with the switches: codex builder.toml runs gpt-6.1-sol (was gpt-6-astra)',
            'Effort evidence needs recheck: fixture evidence expired',
            'Pick with: fleetctl.py select --role R',
        ]
        self.assertEqual(fleetctl.render_brief(overview(), verbose=True).splitlines(), expected)

    def test_selector_price_and_projection_binding_match_selector(self):
        r = roster()
        r['policy'] = {'selector': {'target': 80, 'lambda0': 2}}
        runtime = {'quota_snapshots': {'claude': {
            'source': 'fixture', 'observed_at': fleetctl.iso(NOW),
            'windows': {
                '5h': {'used_percent': 70, 'reset_at': fleetctl.iso(NOW + dt.timedelta(hours=1)),
                       'window_minutes': 300, 'projected_used_percent_at_reset': 80},
                'weekly': {'used_percent': 40, 'reset_at': fleetctl.iso(NOW + dt.timedelta(days=1)),
                           'window_minutes': 10080, 'projected_used_percent_at_reset': 95},
            }}}}
        with tempfile.TemporaryDirectory() as d, patch.object(fleetctl, 'utc_now', return_value=NOW):
            data = fleetctl.fleet_overview(r, runtime, Path(d), NOW)
            self.assertEqual(next(p for p in data['pools'] if p['pool'] == 'claude')['quota']['used_percent'], 70)
            text = fleetctl.render_brief(data, roster=r, runtime=runtime)
        self.assertIn('claude | normal | weekly | 40% | 1d 0h | 1.50 |', text)
        self.assertNotIn('$', text)

    def test_unknown_projection_uses_selector_fallback_and_off_models_are_explicit(self):
        r = roster()
        r['policy'] = {'selector': {'lambda_unknown': .75}}
        with tempfile.TemporaryDirectory() as d:
            data = fleetctl.fleet_overview(r, {}, Path(d), NOW)
            text = fleetctl.render_brief(data, roster=r, runtime={})
        self.assertIn('claude | normal | - | - | - | ~0.75 |', text)
        data = overview()
        data['pools'][0]['models']['retired'] = {'old': '2026-01-01'}
        self.assertIn('codex/old (retired) -> refused', fleetctl.render_brief(data))
        data['pools'][0]['direct'] = False
        data['pools'][0]['models']['stand_ins'] = {}
        self.assertIn('codex/gpt-6-astra -> select by role', fleetctl.render_brief(data))

    def test_cli_compact_verbose_and_json_unchanged(self):
        data = overview()
        for flags in ([], ['--verbose'], ['--json'], ['--json', '--verbose']):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as d:
                args = ['fleetctl.py', '--state-dir', d, 'brief', '--no-refresh', *flags]
                output = io.StringIO()
                with patch.object(sys, 'argv', args), patch.object(fleetctl, 'read_overlay', return_value=roster()), \
                     patch.object(fleetctl, 'fleet_overview', return_value=data), contextlib.redirect_stdout(output):
                    self.assertEqual(fleetctl.main(), 0)
                if '--json' in flags:
                    self.assertEqual(json.loads(output.getvalue()), data)
                elif '--verbose' in flags:
                    self.assertEqual(output.getvalue().rstrip('\n'), fleetctl.render_brief(data, verbose=True))
                else:
                    self.assertTrue(output.getvalue().startswith(HEADER + '\n'))
                    self.assertIn('pick: fleetctl.py select --role R [--stakes S]', output.getvalue())

    def test_internal_binding_labels_and_rounded_prices(self):
        cases = [
            ('primary', {'window_minutes': 300}, '5h'),
            ('secondary', {'window_minutes': 10080}, 'weekly'),
            ('antigravity-quota-summary-3p-weekly', {}, 'weekly'),
            ('antigravity-quota-summary-gemini-5h', {}, '5h'),
            ('secondary', {'label': 'Monthly'}, 'monthly'),
            ('primary', {'window_minutes': 43200}, 'monthly'),
            ('daily_usd_cap', {}, 'daily $'),
            ('primary', {}, '-'),
        ]
        for name, fields, label in cases:
            with self.subTest(name=name, fields=fields):
                r = roster()
                r['policy'] = {'selector': {'lambda0': 6.05631, 'target': 80}}
                window = dict(fields, used_percent=40, reset_at=fleetctl.iso(NOW + dt.timedelta(hours=1)),
                              projected_used_percent_at_reset=100)
                runtime = {'quota_snapshots': {'claude': {'observed_at': fleetctl.iso(NOW),
                                                        'windows': {name: window}}}}
                with tempfile.TemporaryDirectory() as d, patch.object(fleetctl, 'utc_now', return_value=NOW):
                    data = fleetctl.fleet_overview(r, runtime, Path(d), NOW)
                    text = fleetctl.render_brief(data, roster=r, runtime=runtime)
                self.assertIn(f'claude | normal | {label} | 40% | 1h 0m | 6.06 |', text)

    def test_current_models_only_with_older_count_and_no_mutation(self):
        data = overview()
        models = data['pools'][0]['models']
        models['current'] = ['gpt-6.1-sol']
        models['on'] += ['gpt-6-sol', 'gpt-5.6-luna']
        original = copy.deepcopy(data)
        self.assertIn(' | gpt-6.1-sol (+2 older)\n', fleetctl.render_brief(data))
        models['current'] = []
        self.assertIn(' | - (+3 older)\n', fleetctl.render_brief(data))
        models['current'] = original['pools'][0]['models']['current']
        self.assertEqual(data, original)

    def test_default_unknown_price_and_daily_budget_label(self):
        r = roster()
        with tempfile.TemporaryDirectory() as d:
            data = fleetctl.fleet_overview(r, {}, Path(d), NOW)
            self.assertIn(' | ~0.50 |', fleetctl.render_brief(data, roster=r, runtime={}))
        data = overview()
        data['pools'][1]['metered'] = {'daily_usd_cap': 10}
        self.assertIn('claude | normal | daily $ |', fleetctl.render_brief(data))


if __name__ == '__main__':
    unittest.main()
