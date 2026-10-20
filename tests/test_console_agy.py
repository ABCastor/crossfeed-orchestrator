"""One subscription on screen, independent allowances in the fleet and snapshot."""
import copy
import json
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_console import console, fleetctl

ROOT = Path(__file__).resolve().parents[1]
NOW = fleetctl.parse_iso('2026-10-20T10:00:00Z')


class Page(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.stack, self.nodes = [], []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        node = dict(tag=tag, attrs=dict(attrs), parents=self.stack[:])
        self.nodes.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]['tag'] == tag:
                del self.stack[i:]
                break

    def ident(self, ident):
        return next(n for n in self.nodes if n['attrs'].get('id') == ident)


class AntigravityPresentationTests(unittest.TestCase):
    def setUp(self):
        self.roster = json.loads((ROOT / 'examples/access-overlay.example.json').read_text())
        self.roster.pop('provider_sources', None)
        self.roster.pop('chatgpt_gateway', None)
        self.runtime = {'pool_circuits': {}, 'quota_snapshots': {}, 'switches': {}}
        for index, (key, label) in enumerate(console.AGY_ALLOWANCES.items()):
            pool = self.roster['quota_pools'][key]
            pool['plan'] = {'name': 'Google AI Pro (second account, promo)', 'billing': 'subscription'}
            until = '2026-10-26T09:00:00Z' if index == 0 else '2026-10-23T09:00:00Z'
            self.runtime['pool_circuits'][key] = {'state': 'EXHAUSTED', 'until': until,
                'opened_at': '2026-10-20T09:00:00Z', 'limit_name': 'individual quota',
                'source': 'synthetic-agy-error'}
            windows = (pool.get('quota_refresh') or {}).get('windows', [])
            self.runtime['quota_snapshots'][key] = {'source': 'synthetic', 'observed_at': fleetctl.iso(NOW),
                'windows': {window: {'used_percent': 3 + index, 'window_minutes': 300 if window.endswith('5h') else 10080,
                            'reset_at': until} for window in windows}}
            self.runtime['switches'][key] = ['normal', 'low', 'high'][index]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.object(fleetctl, 'utc_now', return_value=NOW):
            self.overview = fleetctl.fleet_overview(self.roster, self.runtime, Path(self.tmp.name), NOW)
        self.overview['_brief_context'] = {'roster': self.roster, 'runtime': self.runtime}

    def test_one_top_level_subscription_with_default_closed_allowances(self):
        before = copy.deepcopy(self.overview)
        markup = console.render_page(self.overview, 'test-token')
        self.assertIn('id="provider-antigravity"', markup)
        page = Page(markup)
        group = page.ident('provider-antigravity')
        self.assertEqual(json.loads(group['attrs']['data-pool-members']), list(console.AGY_ALLOWANCES))
        self.assertIn('Antigravity (Google AI Pro)', markup)
        self.assertEqual(markup.count('<p class="plan">Google AI Pro (second account, promo)'), 1)
        details = page.ident('antigravity-allowances')
        self.assertNotIn('open', details['attrs'])
        for key in console.AGY_ALLOWANCES:
            pool = page.ident('pool-' + key)
            self.assertIn(group, pool['parents'])
            self.assertIn(details, pool['parents'])
            controls = [n for n in page.nodes if n['tag'] == 'input' and n['attrs'].get('name') == 'pool'
                        and n['attrs'].get('value') == key]
            self.assertTrue(controls)
            self.assertTrue(all(pool in n['parents'] for n in controls))
        self.assertEqual(self.overview, before)

    def test_each_allowance_keeps_windows_refusal_reset_and_level(self):
        payload = console.snapshot_payload(self.overview)
        for key in console.AGY_ALLOWANCES:
            pool = next(p for p in self.overview['pools'] if p['pool'] == key)
            shown = next(p for p in payload['pools'] if p['pool'] == key)
            self.assertEqual(shown['level'], self.runtime['switches'][key])
            self.assertIn('EXHAUSTED', shown['gauge'])
            self.assertIn(pool['until'], shown['gauge'])
            if key != 'antigravity-argon':
                self.assertIn('Weekly', shown['gauge'])
                self.assertIn('5-hour', shown['gauge'])
                self.assertIn(str(3 + list(console.AGY_ALLOWANCES).index(key)) + '%', shown['gauge'])
            else:
                self.assertNotIn('%', shown['gauge'])  # Argon's refusal is not a measured percentage.
                self.assertIn('Usage percentage is not reported', shown['gauge'])

    def test_summary_prioritizes_refusals_over_low_gauges_and_refreshes(self):
        expected = 'Gemini used up until 26 Oct · Claude/GPT used up until 23 Oct · Argon used up until 23 Oct'
        self.assertEqual(console.snapshot_payload(self.overview).get('agy_summary'), expected)
        self.assertIn(expected, console.render_page(self.overview, 'test-token'))
        gemini = next(p for p in self.overview['pools'] if p['pool'] == 'antigravity-gemini')
        gemini.update(state='NORMAL', until=None)
        gemini['limits'] = [{'used_percent': 27}, {'used_percent': 61}]
        gemini['quota'] = None
        self.assertTrue(console._agy_summary(self.overview['pools']).startswith('Gemini 61% used ·'))
        gemini.update(limits=[], quota=None)
        self.assertTrue(console._agy_summary(self.overview['pools']).startswith('Gemini no reading ·'))

    def test_old_gauges_are_dated_and_expired_windows_stay_out(self):
        snapshot = self.runtime['quota_snapshots']['antigravity-gemini']
        snapshot['observed_at'] = '2026-10-10T10:00:00Z'
        snapshot['windows']['antigravity-quota-summary-gemini-5h']['reset_at'] = '2026-10-19T10:00:00Z'
        before = copy.deepcopy(self.overview)
        pool = next(p for p in self.overview['pools'] if p['pool'] == 'antigravity-gemini')
        gauge = console._provider_gauge(pool, self.overview)
        self.assertIn('Weekly', gauge)
        self.assertNotIn('5-hour', gauge)
        self.assertIn('Last gauge reading 10d 0h ago; provider refusal still applies.', gauge)
        self.assertIn('EXHAUSTED', gauge)
        self.assertNotIn('spare, use it', gauge)
        self.assertEqual(self.overview, before)

    def test_partial_group_and_unrelated_google_pool_stay_separate(self):
        self.overview['pools'] = [p for p in self.overview['pools'] if p['pool'] != 'antigravity-argon']
        self.overview.pop('console_order', None)
        markup = console.render_page(self.overview, 'test-token')
        self.assertIn('id="provider-antigravity"', markup)
        page = Page(markup)
        self.assertEqual(len(json.loads(page.ident('provider-antigravity')['attrs']['data-pool-members'])), 2)
        self.assertNotIn('Argon used up', console.snapshot_payload(self.overview)['agy_summary'])
        paid = page.ident('pool-gemini-metered')
        self.assertNotIn(page.ident('provider-antigravity'), paid['parents'])


if __name__ == '__main__':
    unittest.main()
