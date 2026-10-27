"""Codex catalog discovery and loud catalog failures (daily refresh).

Fake Codex cache fixtures and fake CLIs only; the real ~/.codex is never read.
"""
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import daily_model_refresh as daily, fleetctl, selector
from scripts import patch_opencode_go_roles as patcher
from tests.test_opencode_value import value_fixture

ROOT = Path(__file__).resolve().parents[1]
CACHE = json.loads((ROOT / 'tests/fixtures/codex-models-cache.json').read_text())
SPEC = importlib.util.spec_from_file_location('codex_catalog_market', ROOT / 'scripts/market-refresh.py')
market_refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(market_refresh)
FETCHED = dt.datetime(2026, 10, 27, 8, 25, tzinfo=dt.timezone.utc)


def codex_roster():
    roster = value_fixture()
    roster.setdefault('model_cards', {})['gpt-known'] = {'pool': 'codex', 'name': 'GPT Known', 'status': 'current'}
    return roster


class CodexCatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.cache = self.tmp / 'models_cache.json'
        self.write_cache()
        self.env = patch.dict(os.environ, {'CODEX_MODELS_CACHE': str(self.cache)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.overlay = self.tmp / 'overlay.json'
        self.overlay.write_text(patcher.dump(codex_roster()))

    def write_cache(self, **changes):
        data = copy.deepcopy(CACHE)
        data.update(changes)
        self.cache.write_text(json.dumps(data))

    def codex(self, roster, now=FETCHED):
        catalogs = [{'harness': 'codex', 'provider': 'openai', 'pool': daily.CODEX_CATALOG_POOL,
                     'source': 'Codex model cache', 'subscription': True}]
        catalogs[0]['models'], catalogs[0]['reason'] = daily.codex_catalog(roster, now=now)
        return catalogs

    def test_discover_adds_a_codex_catalog_for_a_codex_pool_without_running_other_clis(self):
        roster = codex_roster()
        roster['lanes'] = []
        with patch.object(daily.subprocess, 'run', side_effect=AssertionError('no CLI expected')):
            catalogs = daily.discover(roster)
        codex = [c for c in catalogs if c['harness'] == 'codex']
        self.assertEqual(len(codex), 1)
        self.assertEqual((codex[0]['pool'], codex[0]['provider']), ('codex-catalog', 'openai'))

    def test_new_visible_model_becomes_an_unverified_candidate_without_breaking_card_routes(self):
        roster = codex_roster()
        rows, why = daily.codex_catalog(roster, now=FETCHED + dt.timedelta(days=3))
        self.assertIsNone(why)
        # Known card filtered; hide and none never enter.
        self.assertEqual(rows, [{'selector': 'gpt-new', 'model_key': 'gpt-new', 'display_name': 'GPT New'}])
        before = fleetctl.choosable_models(roster, 'codex')
        changes = daily.apply_refresh(self.overlay, self.codex(roster), {}, today='2026-10-27')
        result = json.loads(self.overlay.read_text())
        lane = next(l for l in result['lanes'] if l['model_key'] == 'gpt-new')
        self.assertEqual(changes['added'], [lane['lane_id']])
        self.assertEqual((lane['harness'], lane['quota_pool'], lane['selector']), ('codex', 'codex-catalog', 'gpt-new'))
        self.assertEqual((lane['admission_status'], lane['access_status'], lane['roles']), ('candidate', 'unverified', []))
        self.assertNotIn('price_1m', lane)
        self.assertIn('codex-catalog', result['quota_pools'])
        self.assertFalse(any(l['model_key'] in {'gpt-hidden', 'gpt-unlisted'} for l in result['lanes']))
        # A lane in pool `codex` would switch the direct card routes off.
        self.assertTrue(fleetctl.pool_is_direct(result, 'codex'))
        self.assertEqual(fleetctl.choosable_models(result, 'codex'), before)
        options, rejected = selector.enumerate_options(result, {}, 'default', fleet=fleetctl)
        self.assertNotIn('gpt-new', [o.get('model_key') for o in options])
        patcher.validate_overlay(self.overlay.read_text(), label='codex candidate readback')

    def test_known_model_from_evidence_or_card_is_not_a_candidate(self):
        roster = codex_roster()
        roster['model_evidence']['gpt-new'] = {'status': 'measured', 'quota_pool': 'codex'}
        self.assertEqual(daily.codex_catalog(roster, now=FETCHED)[0], [])

    def test_stale_or_missing_cache_is_unknown_and_retires_nothing(self):
        daily.apply_refresh(self.overlay, self.codex(codex_roster()), {}, today='2026-10-27')
        start = json.loads(self.overlay.read_text())
        late = FETCHED + dt.timedelta(days=15)
        stale = self.codex(start, now=late)
        self.assertIsNone(stale[0]['models'])
        self.assertIn('15 days old', stale[0]['reason'])
        self.cache.unlink()
        missing = self.codex(start, now=FETCHED)
        self.assertIsNone(missing[0]['models'])
        self.assertIn('not found', missing[0]['reason'])
        for catalogs in (stale, missing):
            result, changes = daily.reconcile(start, catalogs, {}, today='2026-11-11')
            self.assertEqual(changes['retired'], [])
            self.assertEqual(result['lanes'], start['lanes'])
            self.assertTrue(changes['unknown'][0].startswith('Codex model cache: '))
        # Only a fresh listing without the model retires its candidate.
        self.write_cache(models=[m for m in CACHE['models'] if m['slug'] != 'gpt-new'])
        result, changes = daily.reconcile(start, self.codex(start), {}, today='2026-10-28')
        self.assertEqual(changes['retired'], ['catalog-codex-catalog-gpt-new'])

    def test_unreadable_cache_shapes_are_unknown(self):
        for data in ('not json', json.dumps({'models': []}), json.dumps({**CACHE, 'fetched_at': '-262143-01-01T00:00:00Z'}),
                     json.dumps({**CACHE, 'models': [m for m in CACHE['models'] if m['visibility'] != 'list']})):
            with self.subTest(data=data[:40]):
                self.cache.write_text(data)
                rows, why = daily.codex_catalog(codex_roster(), now=FETCHED)
                self.assertIsNone(rows)
                self.assertTrue(why)

    def test_codex_route_price_is_not_joined_from_api_prices(self):
        catalogs = self.codex(codex_roster())
        daily.enrich(catalogs, {'openai': {'models': {'gpt-new': {'cost': {'input': 5, 'output': 20}}}}})
        self.assertNotIn('price_1m', catalogs[0]['models'][0])


class LoudFailureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.overlay = self.tmp / 'overlay.json'
        self.overlay.write_text(patcher.dump(value_fixture()))

    def test_missing_binary_is_named(self):
        empty = self.tmp / 'empty-bin'
        empty.mkdir()
        roster = {'lanes': [{'harness': 'opencode', 'provider': 'opencode-go', 'quota_pool': 'opencode-go'},
                            {'harness': 'agy', 'provider': 'antigravity', 'quota_pool': 'antigravity-3p', 'model_key': 'x'}]}
        with patch.dict(os.environ, PATH=str(empty)):
            unknown = daily.unknown_checks(daily.discover(roster))
        self.assertEqual(unknown['opencode models --standalone (opencode-go)'], 'opencode not found on PATH')
        self.assertEqual(unknown['agy models'], 'agy not found on PATH')

    def test_copilot_listing_is_unknown_only_when_the_listing_fails(self):
        roster = {'lanes': [{'harness': 'copilot', 'provider': 'github-copilot'}]}
        ok = subprocess.CompletedProcess([], 0, 'google/gemini-x\nopencode-go/beam\n', '')
        with patch.object(daily.subprocess, 'run', return_value=ok):
            self.assertEqual(daily.discover(roster), [])
        with patch.object(daily.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
            self.assertEqual(len(daily.unknown_checks(daily.discover(roster))), 1)

    def test_unfunded_agy_model_is_reported_once_and_is_not_an_unknown_check(self):
        rows = [{'selector': 'gpt-oss-120b-medium', 'model_key': 'gpt-oss-120b'}]
        catalogs = [{'harness': 'agy', 'provider': 'antigravity', 'pool': pool, 'models': [], 'source': 'agy models',
                     'unmapped': rows} for pool in ('antigravity-3p', 'antigravity-gemini')]
        _, changes = daily.reconcile(value_fixture(), catalogs, {}, today='2026-10-27')
        self.assertEqual((changes['unknown'], changes['unfunded']), ([], ['gpt-oss-120b-medium']))
        self.assertEqual(daily.unknown_checks(catalogs), {})

    def run_main(self, catalogs):
        with patch.object(market_refresh, 'OVERLAY', self.overlay), patch.object(market_refresh, 'STATE_DIR', self.tmp / 'market'), \
                patch.object(market_refresh.daily, 'discover', return_value=copy.deepcopy(catalogs)), \
                patch.object(market_refresh, 'fetch', return_value='{}'), \
                patch.object(market_refresh, 'fetch_artificial_analysis', return_value={}), \
                patch.object(market_refresh, 'write_evidence'), patch.object(market_refresh, 'load_lmarena', return_value={}):
            code = market_refresh.main()
        return code, (self.tmp / 'market/market-report.md').read_text().splitlines()

    def test_two_consecutive_unknown_runs_lead_the_report_and_fail_the_job(self):
        down = [{'harness': 'opencode', 'provider': 'opencode-go', 'pool': 'opencode-go', 'models': None,
                 'source': 'opencode models --standalone (opencode-go)', 'reason': 'opencode not found on PATH'}]
        listed = [{'selector': l['selector'], 'model_key': l['model_key']} for l in value_fixture()['lanes']
                  if l.get('provider') == 'opencode-go' and l.get('harness') == 'opencode']
        up = [dict(down[0], models=listed, reason=None)]
        code, report = self.run_main(down)
        self.assertEqual((code, report[0]), (0, '# Market report'))
        self.assertIn('opencode not found on PATH', '\n'.join(report))
        state = (self.tmp / 'market/catalog-unknown-streak.json').read_text()
        self.assertEqual(state.count('\n'), 1)
        code, report = self.run_main(down)
        self.assertNotEqual(code, 0)
        self.assertTrue(report[0].startswith('CATALOG CHECK FAILING: opencode models --standalone (opencode-go) '
                                             '(opencode not found on PATH), 2 runs in a row'), report[0])
        # A successful night resets the count.
        code, report = self.run_main(up)
        self.assertEqual((code, report[0]), (0, '# Market report'))
        code, report = self.run_main(down)
        self.assertEqual((code, report[0]), (0, '# Market report'))

    def test_codex_candidate_is_named_in_the_report(self):
        catalogs = [{'harness': 'codex', 'provider': 'openai', 'pool': 'codex-catalog', 'source': 'Codex model cache',
                     'subscription': True, 'reason': None,
                     'models': [{'selector': 'gpt-new', 'model_key': 'gpt-new', 'display_name': 'GPT New'}]}]
        code, report = self.run_main(catalogs)
        self.assertEqual(code, 0)
        self.assertIn('LISTED by the Codex CLI but NOT routed (unverified candidates, pool codex-catalog): GPT New (gpt-new)', report)
        self.assertIn('Daily roster: 1 added', '\n'.join(report))


if __name__ == '__main__':
    unittest.main()
