"""Cash visibility does not authorize spend or make display-only quotes routing inputs."""
import json
import os
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


def replay(roster, state, display_scale):
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
                     'price_1m': {'input': i * display_scale, 'output': 2 * i * display_scale}}
                    for i, (model, level) in enumerate(sorted(identities), 1)]
            (state / 'evidence').mkdir()
            (state / 'evidence/levels.json').write_text(json.dumps({'rows': rows}))
        for role in ROLES:
            for stakes in STAKES:
                for allow in (None, ALLOW):
                    try:
                        choice = selector.select_option(roster, {}, state, role, stakes=stakes,
                                                        allow=allow, fleet=fleetctl, no_explore=True,
                                                        persist_receipts=False)['choice']
                        assert choice['funding'] != 'metered'
                        result = key(choice)
                    except fleetctl.FleetError:
                        result = 'refused'
                    outcomes[f'{role}/{stakes}/{"restricted" if allow else "unrestricted"}/'
                             f'{"measured" if measured else "unmeasured"}'] = result
    return outcomes


class CashParityTests(unittest.TestCase):
    # BR-CASH-001
    def test_display_quote_does_not_change_unfunded_routing_grid(self):
        historical_grid = json.loads(EXPECTED.read_text())
        roster = json.loads(FIXTURE.read_text())
        with patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as tmp:
            ordinary, inflated = Path(tmp) / 'ordinary', Path(tmp) / 'inflated'
            ordinary.mkdir()
            inflated.mkdir()
            actual = replay(roster, ordinary, display_scale=1)
            control = replay(roster, inflated, display_scale=100)
        self.assertEqual(len(actual), 96)
        self.assertEqual(actual.keys(), historical_grid.keys())
        self.assertEqual(actual, control)
