"""Contracts: provider membership, model admission and manual refresh."""
import copy
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from tests.test_console import console, base_overlay

fleetctl = console.fleetctl


class ProviderModelsTests(unittest.TestCase):
    def overview(self, roster):
        with tempfile.TemporaryDirectory() as directory:
            return fleetctl.fleet_overview(roster, {}, Path(directory))

    def test_current_requires_verified_active_lane_regardless_of_rank_or_version(self):
        roster = base_overlay()
        prototype = roster['lanes'][0]
        pool = prototype['quota_pool']
        variants = [('family-2.9', 'active', 'verified'), ('family-2.10', 'active', 'verified'),
                    ('family-3.0', 'retired', 'verified'), ('family-4.0', 'active', 'unverified')]
        roster['lanes'] = [dict(prototype, lane_id=key, model_key=key,
                                admission_status=admission, access_status=access)
                           for key, admission, access in variants]
        roster['routing']['roles'] = {'default': {'quality_first': [v[0] for v in variants]}}
        roster['lanes'].append(dict(prototype, lane_id='unranked', model_key='unranked-7'))
        roster['catalogue_only'] = [{'model_key': 'family-8', 'selector': f'{pool}/family-8'}]
        models = {m['model']: m for m in self.overview(roster)['models']}
        for key in ['family-2.9', 'family-2.10', 'unranked-7']:
            self.assertEqual(models[key]['current_pools'], [pool], key)
        for key in ['family-3.0', 'family-4.0', 'family-8']:
            self.assertEqual(models[key]['current_pools'], [], key)
        self.assertEqual(models['family-8']['pools'], [pool])

    def test_same_model_belongs_to_each_funding_provider_and_has_unique_targets(self):
        roster = base_overlay()
        lane = copy.deepcopy(roster['lanes'][0])
        lane.update(lane_id='second-copy', quota_pool='claude')
        roster['lanes'].append(lane)
        roster['routing']['roles']['default']['quality_first'].append(lane['lane_id'])
        overview = self.overview(roster)
        model = next(m for m in overview['models'] if m['model'] == lane['model_key'])
        self.assertEqual(set(model['pools']), {roster['lanes'][0]['quota_pool'], 'claude'})
        page = console.render_page(overview, 'fixture')
        for pool in model['pools']:
            self.assertEqual(page.count(f'id="{console._model_id(pool, model["model"])}"'), 1)
        self.assertEqual(page.count(f'data-model="{model["model"]}"'), 2)

    def test_card_pool_merges_with_existing_model_and_preserves_older_family_member(self):
        from tests.test_model_toggles import lane

        for source in ('lane', 'evidence'):
            with self.subTest(source=source):
                roster = {
                    'quota_pools': {'codex': {'label': 'Codex', 'plan': {'name': 'Pro'}},
                                    'go': {'label': 'Go', 'plan': {'name': 'Go'}}},
                    'lanes': [lane('go-luna', 'gpt-6-luna', 'go')] if source == 'lane' else [],
                    'model_evidence': {'gpt-6-luna': {'quota_pool': 'go', 'status': 'provisional'}},
                    'model_cards': {
                        'gpt-6-luna': {'pool': 'codex', 'name': 'GPT-6 Luna', 'status': 'current'},
                        'gpt-5.6-luna': {'pool': 'codex', 'name': 'GPT-5.6 Luna', 'status': 'older',
                            'older_model_reasons': {'codex': {
                                'job': 'fixture task', 'advantage': 'faster',
                                'compared_to': 'gpt-6-luna', 'reason': 'synthetic comparison',
                                'evidence': 'test-only'}}},
                    },
                }
                overview = self.overview(roster)
                model = next(m for m in overview['models'] if m['model'] == 'gpt-6-luna')
                self.assertEqual(set(model['pools']), {'go', 'codex'})
                self.assertEqual(set(model['providers']), {'Go', 'Codex'})
                # Display membership must not invent routing admission on either provider.
                self.assertEqual(model['current_pools'], ['go'] if source == 'lane' else [])
                codex = next(p for p in overview['pools'] if p['pool'] == 'codex')
                self.assertEqual(set(codex['models']['on']), {'gpt-6-luna', 'gpt-5.6-luna'})
                options = {o['model']: o for o in codex['options']}
                self.assertEqual(set(options), set(codex['models']['on']))
                self.assertTrue(options['gpt-6-luna']['current'])
                self.assertFalse(options['gpt-5.6-luna']['current'])
                self.assertEqual(options['gpt-5.6-luna']['superseded_by'], 'gpt-6-luna')
                for sort in console.MODEL_SORTS:
                    overview['console_order'] = console.display_order(overview, {
                        'pools': [], 'models': {},
                        'sort': {'pools': 'name', 'models': {'codex': sort}}})
                    page = console.render_page(overview, 'fixture')
                    block = re.search(r'id="pool-codex".*?</article>', page, re.S).group()
                    self.assertEqual(set(re.findall(r'data-model="([^"]+)"', block)),
                                     set(codex['models']['on']), sort)
                    if sort == 'your':
                        current, older = block.split('<details class="older">')
                        self.assertIn('data-model="gpt-6-luna"', current)
                        self.assertIn('data-model="gpt-5.6-luna"', older)
                    for pool in model['pools']:
                        self.assertEqual(page.count(f'id="{console._model_id(pool, model["model"])}"'), 1)

    def test_render_has_named_header_search_per_provider_picker_and_older_models(self):
        overview = self.overview(base_overlay())
        page = console.render_page(overview, 'fixture')
        self.assertIn('<title>Providers · Crossfeed Orchestrator</title>', page)
        self.assertIn('aria-label="Search everything  /"', page)
        self.assertIn('</nav><span class="bar-tools">', page)
        self.assertIn('action="/refresh"', page)
        self.assertIn('class="older"', page)
        self.assertNotIn('id="models"', page)
        self.assertIn('data-sort="pools"', page)
        self.assertIn('data-sort="models"', page)
        self.assertNotIn('<select name="model"', page)
        self.assertNotIn('>Prefer<', page)
        self.assertNotIn('>Avoid<', page)
        for pool in overview['pools']:
            self.assertIn(f'id="pick-{pool["pool"]}"', page)
        self.assertIn('role="switch"', page)          # every model a provider can run has a switch
        self.assertIn('<span class="now all">', page)
        self.assertNotIn('aria-pressed="true" data-name', page)   # the single-choice picker is gone

    def test_evidence_only_gpt_stays_archived_inside_codex(self):
        roster = base_overlay()
        roster['model_evidence']['gpt-6-example'] = {'status': 'provisional'}
        model = next(m for m in self.overview(roster)['models'] if m['model'] == 'gpt-6-example')
        self.assertEqual(model['pools'], ['codex'])
        self.assertEqual(model['current_pools'], [])

    def test_catalogue_model_in_dormant_pool_is_not_lost(self):
        roster = base_overlay()
        roster['quota_pools']['dormant'] = {}
        roster['catalogue_only'].append({'model_key': 'dormant-1', 'selector': 'dormant/old'})
        overview = self.overview(roster)
        self.assertNotIn('dormant', [p['pool'] for p in overview['pools']])
        self.assertIn('data-model="dormant-1"', console.render_page(overview, 'fixture'))

    def test_snapshot_updates_all_model_switches_with_saved_state(self):
        roster = base_overlay()
        runtime = {}
        fleetctl.set_model_toggle(runtime, roster, 'opencode-go', 'deepseek-v4-flash', False)
        runtime.setdefault('model_preferences', {})['deepseek-v4-pro'] = 'avoid'   # not a state: still on
        with tempfile.TemporaryDirectory() as directory:
            overview = fleetctl.fleet_overview(roster, runtime, Path(directory))
        pools = {p['pool']: p for p in console.snapshot_payload(overview)['pools']}
        self.assertNotIn('deepseek-v4-flash', pools['opencode-go']['on'])
        self.assertIn('deepseek-v4-pro', pools['opencode-go']['on'])
        self.assertEqual(pools['opencode-go']['state'], 'some')
        self.assertEqual(pools['codex']['state'], 'all')

    def test_logo_has_one_turning_group_with_stationary_disc_and_shared_keyline(self):
        root = ET.fromstring((console.ASSETS / 'crossfeed.svg').read_text())
        logo = root.find('./svg[@class="castor"]')
        self.assertEqual(logo.get('viewBox'), '0 0 24 24')
        self.assertEqual(logo.get('overflow'), 'visible')
        self.assertIsNone(logo.find('mask'))
        self.assertEqual(len(logo.findall('./g[@class="hm-h"]')), 1)
        keyline, fill = logo.find('./g[@class="hm-h"]')
        self.assertEqual(keyline.get('d'), fill.get('d'))
        self.assertEqual(keyline.get('stroke'), 'var(--paper)')
        self.assertEqual(keyline.get('stroke-width'), '0.8')
        self.assertEqual(keyline.get('stroke-linejoin'), 'round')
        self.assertEqual(fill.get('fill'), '#C4552A')
        self.assertEqual(logo.find('./path').get('fill'), '#006770')
        structure = [e.get('stroke-width') for e in root.iter() if e.get('stroke') in ('var(--i)', 'var(--h)')]
        self.assertEqual(set(structure), {'1.185'})


class ManualRefreshTests(unittest.TestCase):
    def test_force_bypasses_fresh_cache_cooldown_and_auto_disable_but_retains_failed_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            roster = {'quota_pools': {'test': {'quota_refresh': {'oracle': 'codexbar', 'provider': 'test'}}}}
            old = {'available': True, 'observed_at': fleetctl.iso(), 'windows': {'primary': {'used_percent': 12}}}
            fleetctl.atomic_json(state / 'runtime.json', {'quota_snapshots': {'test': old},
                'quota_refresh_attempts': {'test': fleetctl.iso()}})
            with mock.patch.dict('os.environ', {'FLEET_NO_AUTO_REFRESH': '1'}), mock.patch.dict(
                    fleetctl.ORACLE_REGISTRY, {'codexbar': mock.Mock(return_value={'available': False, 'reason': 'offline'})}):
                result = fleetctl.refresh_stale_pools(state, ['test'], roster=roster, force=True)
                self.assertEqual(result, {'test': 'unavailable: offline'})
                self.assertEqual(fleetctl.load_json(state / 'runtime.json')['quota_snapshots']['test'], old)
                adapter = fleetctl.ORACLE_REGISTRY['codexbar']
                adapter.assert_called_once()
                new = dict(old, observed_at=fleetctl.iso(fleetctl.utc_now() + fleetctl.dt.timedelta(seconds=1)))
                adapter.return_value = new
                self.assertEqual(fleetctl.refresh_stale_pools(state, ['test'], roster=roster, force=True), {'test': 'refreshed'})
                self.assertEqual(fleetctl.load_json(state / 'runtime.json')['quota_snapshots']['test'], new)

    def test_console_manual_refresh_uses_force_under_shared_refresh_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            app = console.Console(Path(directory) / 'overlay', Path(directory), 8768)
            with mock.patch.object(fleetctl, 'read_overlay', return_value=base_overlay()), mock.patch.object(
                    fleetctl, 'refresh_stale_pools', return_value={'codex': 'refreshed'}) as refresh:
                self.assertEqual(app.refresh_now(), {'codex': 'refreshed'})
                self.assertTrue(refresh.call_args.kwargs['force'])
                self.assertFalse(app._refresh_lock.locked())
                self.assertGreater(app._last_refresh, 0)


if __name__ == '__main__':
    unittest.main()
