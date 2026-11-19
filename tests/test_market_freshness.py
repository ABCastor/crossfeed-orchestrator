"""Versioned daily diffs, failed-source baselines, review queue and replay isolation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import test_selector as fixture
from scripts import market_freshness as fresh


def feed(day, models):
    return {'fetched_at': f'2026-11-{day}T03:40:00Z', 'sources_ok': {'models_dev': False},
            'provider_catalogs': [{'harness': 'opencode', 'provider': 'opencode-go', 'pool': 'opencode-go',
                'models': [{'selector': 'opencode-go/' + m, 'model_key': m} for m in models]
                          if models is not None else None}],
            'artificial_analysis': {'synthetic': {'read_on': '2026-11-18'}}}


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.market = Path(tmp.name)/'market'
        self.market.mkdir()
        self.scope = 'opencode/opencode-go/opencode-go'

    def test_failed_and_duplicate_provider_rows_never_replace_the_baseline(self):
        r = {'lanes': [], 'model_cards': {}}
        first = fresh.publish(self.market, r, feed(17, ['first']), None)
        self.assertEqual(first['provider_diff'][self.scope], {'status': 'baseline', 'count': 1})
        self.assertIn('unmapped model', first['alerts'][0])
        failed = fresh.publish(self.market, r, feed(18, None), None)
        self.assertEqual(failed['provider_diff'][self.scope], {'status': 'unknown'})
        self.assertEqual(first['queue'], failed['queue'])
        dup = fresh.publish(self.market, r, feed(18, ['first', 'first']), None)
        self.assertEqual(dup['provider_diff'][self.scope], {'status': 'unknown'})
        second = fresh.publish(self.market, r, feed(19, ['second']), None)
        self.assertEqual(second['provider_diff'][self.scope], {'status': 'observed',
            'added': ['opencode-go/second'], 'removed': ['opencode-go/first'], 'changed': []})
        index = json.loads((self.market/'freshness-index.json').read_text())
        self.assertEqual(json.loads((self.market/index['revision']).read_text()), second)
        self.assertGreaterEqual(len(list((self.market/'history').glob('*/*.json'))), 3)
        self.assertEqual(second['benchmark_inputs']['artificial_analysis']['synthetic']['read_on'], '2026-11-18')

    def test_visibility_is_not_admission_and_retirements_are_queued(self):
        lane = {'lane_id': 'demo', 'harness': 'opencode', 'provider': 'opencode-go', 'quota_pool': 'opencode-go',
                'selector': 'opencode-go/example', 'model_key': 'example', 'admission_status': 'candidate',
                'access_status': 'unverified'}
        r = {'lanes': [lane], 'model_cards': {}}
        first = fresh.publish(self.market, r, feed(17, ['example']), None)
        self.assertEqual(first['entitlements']['demo']['admission'], 'candidate')
        self.assertIn('candidate:demo', first['queue'])
        failed = fresh.publish(self.market, r, feed(18, None), None)
        self.assertEqual(failed['entitlement_diff']['changed'], [])
        lane.update(admission_status='retired', retired_by_refresh='2026-11-19')
        report = fresh.publish(self.market, r, feed(19, []), None)
        self.assertIn('retirement:demo', report['queue'])
        self.assertEqual(report['entitlement_diff']['changed'], ['demo'])
        self.assertNotIn('candidate:demo', report['queue'])

    def test_version_holds_coverage_and_ranks_both_modes_without_receipts(self):
        roster = json.loads((fixture.ROOT/'tests/fixtures/access-overlay.test.json').read_text())
        row = fixture.quality_row('gpt-6.1-sol', mean=.8)
        row['sources'] = [{'read_on': '2026-11-18', 'families': ['review']}]
        grades = {'rows': [row], 'read_on': '2026-11-19'}
        cache = self.market.parent/'evidence'
        cache.mkdir()
        (cache/'levels.json').write_text(json.dumps(grades))
        with mock.patch.object(fresh.fleet.chatgpt_pro, 'refresh', side_effect=AssertionError('not a preview')):
            result = fresh.publish(self.market, roster, feed(19, []), grades)
        self.assertEqual(result['coverage']['review']['dated_outside'], 1)
        self.assertEqual(result['value_leaders']['review']['absolute-best']['model_key'], 'gpt-6.1-sol')
        self.assertIn('best-value', result['value_leaders']['review'])
        self.assertFalse((self.market.parent/'selections').exists())
