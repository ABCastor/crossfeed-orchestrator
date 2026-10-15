"""Synthetic end-to-end fit import, selector evidence, cards and console contracts."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from bench import to_evidence
from scripts import evidence, fleetctl, model_cards
from tests.test_console import console, unmark

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/benchmarks/results.jsonl'


class BenchmarksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.overlay = {'schema_version': 3, 'lanes': [],
            'quota_pools': {'codex': {}},
            'model_cards': {'fixture-model': {'pool': 'codex', 'price_1m': {'in': 3, 'out': 7}}},
            'effort': {'fixture-model': {'levels': {'codex': ['low', 'high']}, 'default': 'high'}}}
        # Invented publisher values, never real AA data.
        self.catalog = {'fetched_at': '2026-10-14T00:00:00Z', 'artificial_analysis': {
            'fixture-model': {'levels': {'high': {'effort': 'high', 'evaluations': {'terminalbench_v4_0': .41}}}}}}
        fleetctl.atomic_json(self.state / 'market/market-catalog.json', self.catalog)
        self.path = self.state / 'overlay.json'
        fleetctl.atomic_json(self.path, self.overlay)

    def install(self, paths=None):
        return to_evidence.install_evidence(paths or [FIXTURE], self.state, self.overlay, '2026-10-03')

    def levels(self):
        return json.loads((self.state / 'evidence/levels.json').read_text())

    def high(self):
        return next(r for r in self.levels()['rows'] if r['level'] == 'high')

    def test_import_preserves_outside_prior_and_weights_each_trial(self):
        # BR-EVIDENCE-001
        before = evidence.build_evidence(self.overlay, self.catalog, [])['rows']
        before = next(r for r in before if r['level'] == 'high')['q']['coding-agent']
        receipt = self.install()
        self.assertEqual(receipt['imported'], 1)
        row = self.high()
        self.assertEqual(row['own']['coding-agent'], {'passes': 0, 'trials': 1})
        after = row['q']['coding-agent']
        self.assertEqual(after['sources'], before['sources'])
        self.assertEqual(after['prior_strength'], before['prior_strength'])
        # One failed mechanical trial increases beta by exactly one.
        strength = before['prior_strength']
        alpha = max(strength * before['sources'][0]['prior_mean'], 1e-6)
        beta = max(strength * (1 - before['sources'][0]['prior_mean']), 1e-6)
        self.assertAlmostEqual(after['mean'], alpha / (alpha + beta + 1))
        self.assertLess(after['mean'], before['mean'])
        self.assertEqual(row['own_provenance']['coding-agent'][0]['n'], 1)
        self.assertEqual(row['own_provenance']['coding-agent'][0]['date'], '2026-10-03')
        self.assertEqual(row['own_provenance']['coding-agent'][0]['source'], 'bench.fit')
        self.assertEqual(row['latency_s'], 12)
        self.assertEqual(row['source_telemetry']['codex/codex']['trials'], 1)
        low = next(r for r in self.levels()['rows'] if r['level'] == 'low')
        self.assertEqual(low['own']['coding-agent']['trials'], 0)
        self.assertTrue(low['q']['coding-agent']['unknown'])
        self.assertFalse(self.levels()['unavailable']['own'])

    def test_refresh_reimport_and_copies_do_not_lose_or_duplicate_trials(self):
        self.install()
        other = self.state / 'copy.jsonl'
        other.write_bytes(FIXTURE.read_bytes())
        again = self.install([other])
        self.assertEqual((again['imported'], again['already_present']), (0, 1))
        evidence.write_evidence(self.state, self.overlay, self.catalog)
        self.assertEqual(self.high()['own']['coding-agent']['trials'], 1)
        self.assertEqual(len((self.state / 'runs.jsonl').read_text().splitlines()), 1)

    def test_import_retains_ledger_outcomes_and_no_heldout_cost(self):
        own = dict(run_id='existing', model_key='fixture-model', level='high', family='coding-agent',
                   outcome={'mechanically_verified': True, 'passed': True})
        (self.state / 'runs.jsonl').write_text(json.dumps(own) + '\n')
        self.install()
        row = self.high()
        self.assertEqual(row['own']['coding-agent'], {'passes': 1, 'trials': 2})
        self.assertEqual(row['own_cost'], {})
        self.assertEqual({p['source'] for p in row['own_provenance']['coding-agent']}, {'bench.fit', 'runs.jsonl'})

    def test_missing_cached_catalog_cannot_silently_erase_priors(self):
        # BR-EVIDENCE-001
        self.install()
        before = (self.state / 'runs.jsonl').read_bytes()
        for catalog in ({}, {'lmarena': {'rows': [{'model': 'other'}]}}):
            fleetctl.atomic_json(self.state / 'market/market-catalog.json', catalog)
            with self.assertRaisesRegex(ValueError, 'refusing to discard existing priors'):
                self.install()
        self.assertEqual((self.state / 'runs.jsonl').read_bytes(), before)
        self.assertTrue(self.high()['q']['coding-agent']['sources'])

    def test_paired_options_keep_their_own_model_and_level_counts(self):
        first = json.loads(FIXTURE.read_text().splitlines()[0])
        second = dict(first, option='other-high', model='other-model')
        second['pass'] = True
        paired = self.state / 'paired.jsonl'
        paired.write_text(json.dumps(first) + '\n' + json.dumps(second) + '\n')
        self.install([paired])
        cells = {(r['model_key'], r['level']): r for r in self.levels()['rows']}
        self.assertEqual(cells['fixture-model', 'high']['own']['coding-agent'], {'passes': 0, 'trials': 1})
        self.assertEqual(cells['other-model', 'high']['own']['coding-agent'], {'passes': 1, 'trials': 1})

    def test_conflicting_import_fails_before_append(self):
        self.install()
        before = (self.state / 'runs.jsonl').read_bytes()
        row = json.loads(FIXTURE.read_text().splitlines()[0])
        row['pass'] = True
        other = self.state / 'conflict.jsonl'
        other.write_text(json.dumps(row) + '\n')
        with self.assertRaisesRegex(ValueError, 'conflicting imported trial'):
            self.install([other])
        self.assertEqual((self.state / 'runs.jsonl').read_bytes(), before)

    def test_exclusions_and_date_validation(self):
        with self.assertRaisesRegex(ValueError, 'measured-on'):
            to_evidence.import_records([FIXTURE], self.overlay)
        with self.assertRaises(ValueError):
            to_evidence.import_records([FIXTURE], self.overlay, 'not-a-date')
        other = self.state / 'results.jsonl'
        other.write_bytes(FIXTURE.read_bytes())
        (self.state / 'excluded-hook-tasks.txt').write_text('fixture-one\n')
        self.assertEqual(to_evidence.import_records([other], self.overlay), [])

    def test_cards_share_evidence_prices_counts_and_unknowns(self):
        self.install()
        cards = model_cards.read_cards(self.state, self.overlay, fleet=fleetctl)
        high = next(r for r in cards['rows'] if r['level'] == 'high')
        for family, quality in high['quality'].items():
            for key, value in quality.items():
                self.assertEqual(value, self.high()['q'][family][key])
        self.assertEqual(len(high['outside']), len(self.high()['sources']))
        self.assertNotIn('assumption', high['outside'][0])
        self.assertNotIn('sources', high['quality']['coding-agent'])
        self.assertEqual(high['evidence_counts']['trials'], 1)
        self.assertEqual(high['sources'][0]['price_1m'], {'in': 3, 'out': 7})
        self.assertEqual(high['sources'][0]['cost']['estimated_usd'], .00044)
        self.assertEqual(high['sources'][0]['latency']['latency_s'], 12)
        self.assertTrue(high['sources'][0]['quota_projection_unknown'])
        self.assertIn('coding-agent', high['family_strengths'])
        self.assertTrue(cards['private'])
        low = next(r for r in cards['rows'] if r['level'] == 'low')
        self.assertEqual(low['quality'], {})
        self.assertIn('coding-agent', low['unmeasured_families'])

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/fleetctl.py'), '--overlay', str(self.path), *args],
            capture_output=True, text=True, env=dict(os.environ, FLEET_STATE_DIR=str(self.state), FLEET_NO_AUTO_REFRESH='1'))

    def test_real_cli_import_then_export_and_repeat(self):
        imported = self.cli('import-bench', str(FIXTURE), '--measured-on', '2026-10-03')
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(json.loads(imported.stdout)['imported'], 1)
        exported = self.cli('model-cards')
        self.assertEqual(exported.returncode, 0, exported.stderr)
        result = json.loads(exported.stdout)
        self.assertEqual(result, model_cards.read_cards(self.state, self.overlay, fleet=fleetctl))
        self.assertEqual(json.loads(self.cli('import-bench', str(FIXTURE), '--measured-on', '2026-10-03').stdout)['imported'], 0)
        self.assertEqual(self.cli('import-bench', str(FIXTURE)).returncode, 2)

    def test_render_live_content_navigation_empty_and_escaping(self):
        self.install()
        cards = model_cards.read_cards(self.state, self.overlay, fleet=fleetctl)
        page = unmark(console.render_benchmarks(cards))
        for text in ('href="/benchmarks" aria-current="page"', 'href="/"', 'fixture-model',
                     'Our checks', 'n=1', '2026-10-03', 'weight', 'Cost and quota', '/model-cards.json'):
            self.assertIn(text, page)
        self.assertNotIn('href="/" aria-current="page"', page)
        changed = copy.deepcopy(cards)
        changed['rows'][0]['model'] = '<script>bad</script>'
        page = unmark(console.render_benchmarks(changed))
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', page)
        self.assertNotIn('<script>bad</script>', page)
        self.assertIn('No evidence or thinking levels configured', console.render_benchmarks({'rows': []}))

    def request_handler(self, path, signed_in=True):
        # Exercise the real HTTP handler without binding a sandbox-forbidden port.
        app = console.Console(self.path, self.state, 8768)
        headers = f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:8768\r\nConnection: close\r\n'
        if signed_in:
            headers += f'Cookie: {console.COOKIE}={app.session}\r\n'
        class Connection:
            def __init__(self):
                self.sent = bytearray()
            def makefile(self, *args):
                return io.BytesIO((headers + '\r\n').encode())
            def settimeout(self, timeout):
                pass
            def sendall(self, data):
                self.sent.extend(data)
        connection = Connection()
        console.make_handler(app)(connection, ('127.0.0.1', 1), object())
        return bytes(connection.sent).split(b'\r\n\r\n', 1)

    def test_http_routes_read_live_state_and_require_authentication(self):
        self.install()
        for route in ('/benchmarks', '/model-cards.json'):
            headers, body = self.request_handler(route, signed_in=False)
            self.assertIn(b'401', headers)
            self.assertNotIn(b'fixture-model', body)
        headers, body = self.request_handler('/model-cards.json')
        self.assertIn(b'200 OK', headers)
        self.assertIn(b'Cache-Control: no-store', headers)
        self.assertEqual(json.loads(body), model_cards.read_cards(self.state, self.overlay, fleet=fleetctl))
        headers, body = self.request_handler('/benchmarks')
        self.assertIn(b'200 OK', headers)
        self.assertIn(b'fixture-model', body)
        self.assertIn(b'n=1', body)
        # A later observation appears on the next request, without a server restart.
        record = dict(run_id='new-live', model_key='fixture-model', level='high', family='coding-agent',
                      outcome={'mechanically_verified': True, 'passed': True})
        with (self.state / 'runs.jsonl').open('a') as stream:
            stream.write(json.dumps(record) + '\n')
        evidence.write_evidence(self.state, self.overlay, self.catalog)
        _, body = self.request_handler('/model-cards.json')
        high = next(r for r in json.loads(body)['rows'] if r['level'] == 'high')
        self.assertEqual(high['evidence_counts']['trials'], 2)

    def test_legacy_latency_stays_scoped_to_its_recorded_pool(self):
        row = json.loads(FIXTURE.read_text().splitlines()[0])
        row.pop('harness')
        path = self.state / 'legacy.jsonl'
        path.write_text(json.dumps(row) + '\n')
        self.install([path])
        telemetry = self.high()['source_telemetry']
        self.assertEqual(telemetry['codex']['latency_s'], 12)
        self.assertEqual(telemetry['codex']['metric'], 'completion_time_s')
        cards = model_cards.read_cards(self.state, self.overlay, fleet=fleetctl)
        high = next(r for r in cards['rows'] if r['level'] == 'high')
        self.assertEqual(high['sources'][0]['latency'], telemetry['codex'])

    def test_invalid_declared_source_price_stays_unknown(self):
        self.install()
        for bad in (['invalid'], {'in': -1, 'out': True}):
            self.overlay['model_cards']['fixture-model']['price_1m'] = bad
            cards = model_cards.read_cards(self.state, self.overlay, fleet=fleetctl)
            high = next(r for r in cards['rows'] if r['level'] == 'high')
            self.assertEqual(high['sources'][0]['price_1m'], {'in': None, 'out': None})
            self.assertIsNone(high['sources'][0]['cost']['estimated_usd'])
            self.assertIn('unknown in / unknown out', console.render_benchmarks(cards))

    def test_real_overlay_metadata_and_all_configured_rows_render(self):
        from tests.test_console import base_overlay
        roster = base_overlay()
        cards = model_cards.read_cards(self.state, roster, fleet=fleetctl)
        self.assertGreater(len(cards['rows']), 10)
        page = console.render_benchmarks(cards)
        for card in cards['rows']:
            self.assertIn(card['model'], page)

    def test_export_without_state_is_read_only_and_unknown(self):
        empty = self.state / 'missing'
        cards = model_cards.read_cards(empty, self.overlay, fleet=fleetctl)
        self.assertEqual(len(cards['rows']), 2)
        self.assertTrue(all(row['evidence_counts']['trials'] == 0 for row in cards['rows']))
        self.assertFalse(empty.exists())


if __name__ == '__main__':
    unittest.main()
