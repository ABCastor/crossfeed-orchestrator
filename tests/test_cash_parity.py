"""Pinned pre-cash selector replay; no live roster, model calls or quota refreshes."""
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import fleetctl
import selector

ROLES = ('review', 'implementation', 'hard-reasoning', 'lookup', 'design', 'long-context')
STAKES = ('low', 'normal', 'high', 'irreversible')
ALLOW = 'opencode-go:deepseek-v4-flash:high,opencode-go:glm-5.2:high,codex:gpt-6.1-sol:high'
FIXTURE = ROOT / 'tests/fixtures/access-overlay.test.json'
# Recorded by running replay with d62e54f scripts and roster fixture restored by git show.
EXPECTED = ROOT / 'tests/fixtures/selector-parity-d62e54f.json'


def replay(roster, state):
    assert not roster.get('cash_accounts')
    outcomes = {}
    identities = set()
    def key(option):
        return [option[k] for k in ('pool', 'model_key', 'level')]
    for role in ROLES:
        options, _ = selector.enumerate_options(roster, {}, role, fleet=fleetctl)
        identities.update((o['model_key'], o['level']) for o in options)
    for measured in (False, True):
        if measured:
            families = set(selector.FAMILY_BY_ROLE.values()) | set(roster['policy']['selector'].get('families_by_role', {}).values())
            quality = {family: {'n_sources': 1, 'mean': .8, 'sd': .01} for family in sorted(families)}
            rows = [{'model_key': model, 'level': level, 'q': quality,
                     'tokens_per_task': {'in': 2000, 'out': 1000},
                     'price_1m': {'input': i, 'output': 2 * i}}
                    for i, (model, level) in enumerate(sorted(identities), 1)]
            (state / 'evidence').mkdir()
            (state / 'evidence/levels.json').write_text(json.dumps({'rows': rows}))
        for role in ROLES:
            for stakes in STAKES:
                for allow in (None, ALLOW):
                    random.seed(19)
                    try:
                        choice = selector.select_option(roster, {}, state, role, stakes=stakes,
                                                        allow=allow, fleet=fleetctl)['choice']
                        result = key(choice)
                    except fleetctl.FleetError:
                        result = 'refused'
                    outcomes[f'{role}/{stakes}/{"restricted" if allow else "unrestricted"}/'
                             f'{"measured" if measured else "unmeasured"}'] = result
    return outcomes


class CashParityTests(unittest.TestCase):
    def test_selection_grid_matches_d62e54f(self):
        expected = json.loads(EXPECTED.read_text())
        with patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as tmp:
            actual = replay(json.loads(FIXTURE.read_text()), Path(tmp))
        self.assertEqual(len(expected), 96)
        self.assertEqual(actual.keys(), expected.keys())
        differences = {key: (expected[key], value) for key, value in actual.items() if value != expected[key]}
        self.assertEqual(differences, {}, f'{len(differences)} of 96 outcomes differ from d62e54f')
