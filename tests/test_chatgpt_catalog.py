"""Saved-label discovery closes admission without current gateway evidence."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import chatgpt_catalog as catalog
import chatgpt_transport as transport
import fleetctl
import chatgpt_runner as runner
import selector
from types import SimpleNamespace


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = json.loads((ROOT / 'examples/access-overlay.example.json').read_text())
        self.gateway = self.roster['chatgpt_gateway']
        self.template = self.gateway['lane_template']
        self.key = 'private-catalog-fixture-key'
        (self.root / 'key').write_text(self.key)
        self.template['auth']['key_file'] = str(self.root / 'key')
        self.models = {'object': 'list', 'data': [
            {'object': 'model', 'id': 'chatgpt:saved-medium', 'saved': True, 'row': 'Latest', 'level': 1},
            {'object': 'model', 'id': 'chatgpt:previous-pro', 'saved': True, 'row': 'Previous row', 'level': 4},
            {'object': 'model', 'id': 'chatgpt:unsaved-label', 'saved': False}]}
        self.status = {'workers': [
            {'label': 'saved-medium', 'contact': 'never', 'polling': False, 'processing_claim': False},
            {'label': 'previous-pro', 'contact': 'recent', 'polling': True, 'processing_claim': False}]}
        self.overlay = self.root / 'overlay.json'
        self.overlay.write_text(json.dumps(self.roster))

    def gateway_request(self, base, key, path, **kwargs):
        return self.status if path == '/gateway/status' else self.models

    def expand(self, persist=True):
        with patch.object(catalog, 'request', side_effect=self.gateway_request):
            return catalog.expand(self.roster, self.root / 'state', persist=persist)

    def generated(self, roster):
        return {lane['lane_id']: lane for lane in roster['lanes'] if lane.get('gateway_service')}

    def test_saved_labels_only_with_exact_selectors_and_model_levels(self):
        roster = self.expand()
        lanes = self.generated(roster)
        self.assertEqual(set(lanes), {'chatgpt:saved-medium', 'chatgpt:previous-pro'})
        for key, lane in lanes.items():
            self.assertEqual((lane['selector'], lane['harness'], lane['quota_pool'], lane['max_parallel']),
                             (key, 'chatgpt-chat', 'chatgpt-work', 1))
            self.assertEqual(lane['admission_status'], 'active')
        self.assertEqual(lanes['chatgpt:saved-medium']['worker_level'], 'medium')
        self.assertEqual(lanes['chatgpt:previous-pro']['worker_level'], 'pro')
        self.assertEqual(roster['model_cards']['chatgpt:saved-medium']['name'], 'Latest / medium')
        self.assertEqual(roster['model_cards']['chatgpt:previous-pro']['status'], 'older')

    def test_never_contacted_saved_label_is_admitted_as_sleeping(self):
        lane = self.generated(self.expand())['chatgpt:saved-medium']
        self.assertEqual((lane['access_status'], lane['admission_status'], lane['catalog_state']),
                         ('verified', 'active', 'sleeping'))
        self.assertIsNotNone(lane['verified_at'])

    def test_media_worker_has_its_own_name_in_agent_brief(self):
        self.models['data'].append({'object': 'model', 'id': 'chatgpt:media-unattended',
                                   'saved': True, 'row': 'Latest', 'level': 1})
        roster = self.expand()
        self.assertEqual(roster['model_cards']['chatgpt:media-unattended']['name'],
                         'ChatGPT media · Images and video · Unattended')
        brief = fleetctl.render_brief(fleetctl.fleet_overview(roster, {}, self.root))
        self.assertIn('ChatGPT media · Images and video · Unattended (sleeping)', brief)
        self.assertEqual(brief.count('Latest / medium (sleeping)'), 1)

    def test_replica_capacity_refreshes_from_live_catalog_over_cached_counts(self):
        for count in (3, 2, 1):
            self.models['data'][0]['replicas'] = count
            lanes = self.generated(self.expand())
            self.assertEqual(lanes['chatgpt:saved-medium']['max_parallel'], count)
            self.assertEqual(lanes['chatgpt:previous-pro']['max_parallel'], 1)
        del self.models['data'][0]['replicas']
        self.assertEqual(self.generated(self.expand())['chatgpt:saved-medium']['max_parallel'], 1)

    def test_invalid_replica_counts_close_cached_admission(self):
        self.models['data'][0]['replicas'] = 2
        self.expand()
        for count in (0, -1, True, False, 1.5, 2.0, '2', None, [], {}):
            with self.subTest(replicas=count):
                self.models['data'][0]['replicas'] = count
                result = self.expand(persist=False)
                self.assertEqual(result['chatgpt_catalog']['error_code'], 6)
                self.assertTrue(all(lane['admission_status'] == 'rejected'
                                    for lane in self.generated(result).values()))

    def test_runner_rejects_invalid_declared_capacity(self):
        lane = self.generated(self.expand())['chatgpt:saved-medium']
        args = SimpleNamespace(lane=lane['lane_id'], mode='ro', modality='text', effort=None)
        for count in (0, -1, True, 2.0, '2', None):
            with self.subTest(capacity=count), patch.object(runner.run_identity, 'roster',
                    return_value={'lanes': [dict(lane, max_parallel=count)]}), \
                    patch.object(runner.run_identity, 'state_dir', return_value=self.root / 'state'):
                with self.assertRaisesRegex(transport.Rejected, 'positive worker capacity'):
                    runner.lane_for(args)

    def test_status_changes_display_without_changing_admission(self):
        for contact, expected in [('recent', 'probed-ok'), ('stale', 'sleeping'), ('never', 'sleeping')]:
            self.status['workers'][0]['contact'] = contact
            with self.subTest(contact=contact):
                lane = self.generated(self.expand())['chatgpt:saved-medium']
                self.assertEqual((lane['admission_status'], lane['catalog_state']), ('active', expected))
        self.status['workers'] = []
        self.assertEqual(self.generated(self.expand())['chatgpt:saved-medium']['catalog_state'], 'sleeping')

    def test_removed_label_cannot_inherit_cached_admission_or_ranking(self):
        self.expand()
        self.models['data'] = self.models['data'][1:]
        result = self.expand()
        gone = self.generated(result)['chatgpt:saved-medium']
        self.assertEqual((gone['selector'], gone['admission_status'], gone['catalog_state']),
                         ('chatgpt:saved-medium', 'rejected', 'unavailable'))
        self.assertNotIn(gone['lane_id'], result['routing']['roles']['default']['quality_first'])

    def test_contacted_but_unsaved_label_cannot_inherit_cached_admission(self):
        self.expand()
        self.models['data'][0] = {'id': 'chatgpt:saved-medium', 'object': 'model', 'saved': False}
        self.assertEqual(self.generated(self.expand())['chatgpt:saved-medium']['admission_status'], 'rejected')

    def test_models_or_status_failure_closes_all_cached_lanes(self):
        self.expand()
        for failed_path in ['/models', '/gateway/status']:
            def respond(base, key, path, **kwargs):
                if path == failed_path:
                    raise transport.Rejected(6, 'gateway unavailable')
                return self.gateway_request(base, key, path, **kwargs)
            with self.subTest(path=failed_path), patch.object(catalog, 'request', side_effect=respond):
                result = catalog.expand(self.roster, self.root / 'state')
                self.assertTrue(result['chatgpt_catalog']['error'])
                self.assertTrue(all(lane['admission_status'] == 'rejected' for lane in self.generated(result).values()))

    def test_invalid_models_and_duplicate_status_close_admission(self):
        self.expand()
        invalid_models = [[], {}, {'object': 'list', 'data': None},
                          {'object': 'list', 'data': [self.models['data'][0]] * 2},
                          {'object': 'list', 'data': [dict(self.models['data'][0], level=True)]},
                          {'object': 'list', 'data': [dict(self.models['data'][0], row='')]},
                          {'object': 'list', 'data': [dict(self.models['data'][0], id='chatgpt:bad\nlabel')]}]
        for payload in invalid_models:
            with self.subTest(payload=payload), patch.object(catalog, 'request', side_effect=lambda base, key, path, **kw:
                    self.status if path == '/gateway/status' else payload):
                result = catalog.expand(self.roster, self.root / 'state')
                self.assertTrue(result['chatgpt_catalog']['error'])
                self.assertTrue(all(row['admission_status'] == 'rejected' for row in self.generated(result).values()))
        self.status['workers'] *= 2
        self.assertTrue(self.expand()['chatgpt_catalog']['error'])

    def test_unsaved_duplicate_selector_is_rejected(self):
        unsaved = {'object': 'model', 'id': 'chatgpt:unsaved', 'saved': False}
        with self.assertRaises(transport.Rejected):
            transport.catalog_models({'object': 'list', 'data': [unsaved, unsaved]})

    def test_invalid_configuration_preserves_ordinary_lanes_and_closes_saved_lanes(self):
        expanded = self.expand()
        ordinary = [lane for lane in expanded['lanes'] if not lane.get('gateway_service')]
        for gateway in [None, {}, [], 'invalid', dict(self.gateway, lane_template=None), dict(self.gateway, service_id=None)]:
            roster = copy.deepcopy(expanded)
            roster['chatgpt_gateway'] = gateway
            with self.subTest(gateway=gateway):
                result = catalog.expand(roster, self.root / 'state')
                self.assertEqual([lane for lane in result['lanes'] if not lane.get('gateway_service')], ordinary)
                self.assertTrue(result['chatgpt_catalog']['error'])
                self.assertTrue(all(lane['admission_status'] == 'rejected' for lane in self.generated(result).values()))
                with self.assertRaisesRegex(fleetctl.FleetError, 'gateway admission closed'):
                    fleetctl.acquire_lease(self.root / 'state', result, 'chatgpt:saved-medium', 60)

    def test_removed_gateway_cannot_admit_expanded_overlay(self):
        expanded = self.expand()
        del expanded['chatgpt_gateway']
        result = catalog.expand(expanded, self.root / 'state')
        self.assertTrue(result['chatgpt_catalog']['error'])
        self.assertTrue(all(lane['admission_status'] == 'rejected' for lane in self.generated(result).values()))

    def test_catalog_reads_api_without_local_configuration_map(self):
        with patch.object(catalog, 'request', side_effect=self.gateway_request) as request:
            result = catalog.expand(self.roster, self.root / 'state')
        self.assertEqual(sorted(call.args[2] for call in request.call_args_list), ['/gateway/status', '/models'])
        self.assertEqual(len(self.generated(result)), 2)
        self.assertEqual(list(self.root.glob('*workers*')), [])

    def test_corrupt_cache_preserves_other_lanes(self):
        state = self.root / 'state'
        state.mkdir()
        for contents in ['invalid-json', '[]', '{"models":[]}']:
            (state / 'chatgpt-catalog.json').write_text(contents)
            with self.subTest(contents=contents):
                result = self.expand()
                self.assertEqual(result['lanes'], self.roster['lanes'])
                self.assertTrue(result['chatgpt_catalog']['error'])

    def test_discovery_rechecks_live_settings_and_only_explicit_sync_persists(self):
        with patch.object(catalog, 'request', side_effect=self.gateway_request):
            self.assertIn('chatgpt:saved-medium', self.generated(fleetctl.read_overlay(self.overlay, self.root / 'state')))
            self.assertFalse((self.root / 'state/chatgpt-catalog.json').exists())
            argv = ['fleetctl', '--overlay', str(self.overlay), '--state-dir', str(self.root / 'state'), 'chatgpt', 'sync']
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(fleetctl.main(), 0)
            self.assertIn('chatgpt:saved-medium', output.getvalue())
            self.assertTrue((self.root / 'state/chatgpt-catalog.json').exists())
            self.models['data'] = self.models['data'][1:]
            result = fleetctl.read_overlay(self.overlay, self.root / 'state')
        self.assertEqual(self.generated(result)['chatgpt:saved-medium']['admission_status'], 'rejected')

    def test_doctor_reports_gateway_wake_status_without_external_cli(self):
        self.status.update(extension_enabled=True, wakes=[],
                           wake_limits={'attempts': 11, 'daily_cap': 60, 'cooldown_until': 0})
        result = self.expand()
        with patch.object(catalog, 'subprocess', create=True) as process:
            line, failed = catalog.doctor(self.gateway, result['chatgpt_catalog']['states'],
                                          result['chatgpt_catalog']['wake'])
            process.run.assert_not_called()
        self.assertFalse(failed)
        self.assertIn('chatgpt:saved-medium=sleeping', line)
        self.assertIn('Crossfeed Chat wake: extension paired, 11/60 attempts', line)
        self.assertNotIn(self.key, line)

    def test_doctor_flags_failed_wakes_caps_cooldowns_and_unpaired_extension(self):
        status = dict(extension_enabled=True, wakes=[],
                      wake_limits={'attempts': 1, 'daily_cap': 60, 'cooldown_until': 0})
        for change in ({'extension_enabled': False},
                       {'wakes': [{'state': 'failed', 'error': self.key}]},
                       {'wakes': [{'state': 'expired'}]},
                       {'wake_limits': {'attempts': 60, 'daily_cap': 60, 'cooldown_until': 0}},
                       {'wake_limits': {'attempts': 1, 'daily_cap': 60, 'cooldown_until': (time.time() + 60) * 1000}},
                       {'wake_limits': None}):
            with self.subTest(change=change):
                wake = catalog.gateway_wake_status(dict(status, **change))
                line, failed = catalog.doctor(self.gateway, {'chatgpt:saved-medium': 'sleeping'}, wake)
                self.assertTrue(failed)
                self.assertNotIn(self.key, line)
                self.assertIn('owner action: check Crossfeed Chat', line)
        self.assertTrue(catalog.doctor(self.gateway, {'chatgpt:saved-medium': 'sleeping'})[1])

    def test_optional_daily_cap_through_overlay_catalog_lease_and_doctor(self):
        for cap in (0, None):
            with self.subTest(daily_cap=cap):
                limits = {'attempts': 100, 'hourly_attempts': 11, 'hourly_cap': 12, 'cooldown_until': 0}
                if cap is None:
                    self.template.pop('wake_daily_cap', None)
                else:
                    self.template['wake_daily_cap'] = cap
                    limits['daily_cap'] = cap
                self.status.update(extension_enabled=True, wakes=[], wake_limits=limits)
                self.overlay.write_text(json.dumps(self.roster))
                with patch.object(catalog, 'request', side_effect=self.gateway_request):
                    result = fleetctl.read_overlay(self.overlay, self.root / 'state')
                lane = self.generated(result)['chatgpt:saved-medium']
                self.assertEqual(lane.get('wake_daily_cap'), cap)
                self.assertEqual(lane['admission_status'], 'active')
                token = fleetctl.acquire_lease(self.root / 'state', result, lane['lane_id'], 60)
                fleetctl.release_lease(self.root / 'state', token)
                line, failed = catalog.doctor(self.gateway, result['chatgpt_catalog']['states'],
                                              result['chatgpt_catalog']['wake'])
                self.assertFalse(failed)
                self.assertIn('100 attempts today, no daily cap', line)
                self.assertIn('11/12 attempts in last hour', line)
                self.assertIn('owner action: none', line)
                self.assertNotIn(self.key, line)

    def test_hourly_cap_still_blocks_when_daily_cap_is_disabled(self):
        for daily in ({'daily_cap': 0}, {}):
            for hourly in ({'hourly_attempts': 12, 'hourly_cap': 12},
                           {'hourly_attempts': 0, 'hourly_cap': 0}):
                with self.subTest(daily=daily, hourly=hourly):
                    status = dict(extension_enabled=True, wakes=[], wake_limits={
                        'attempts': 100, 'cooldown_until': 0, **daily, **hourly})
                    line, failed = catalog.gateway_wake_status(status)
                    self.assertTrue(failed)
                    self.assertIn('no daily cap', line)
                    self.assertIn('attempts in last hour', line)

    def test_malformed_daily_and_hourly_limits_are_unavailable(self):
        limits = {'attempts': 1, 'daily_cap': 0, 'hourly_attempts': 1, 'hourly_cap': 12,
                  'cooldown_until': 0}
        for field in ('daily_cap', 'hourly_attempts', 'hourly_cap'):
            for value in (None, True, -1, 1.5, '12'):
                with self.subTest(field=field, value=value):
                    status = dict(extension_enabled=True, wakes=[], wake_limits=dict(limits, **{field: value}))
                    self.assertEqual(catalog.gateway_wake_status(status), ('Crossfeed Chat wake: unavailable', True))
        for field in ('hourly_attempts', 'hourly_cap'):
            partial = dict(limits)
            del partial[field]
            self.assertEqual(catalog.gateway_wake_status(dict(extension_enabled=True, wakes=[], wake_limits=partial)),
                             ('Crossfeed Chat wake: unavailable', True))

    def test_plain_runner_admission_error_does_not_dispatch(self):
        self.roster['chatgpt_gateway'] = None
        self.overlay.write_text(json.dumps(self.roster))
        with patch.dict(os.environ, ACCESS_OVERLAY=str(self.overlay), FLEET_STATE_DIR=str(self.root / 'state')):
            with self.assertRaises(transport.Rejected) as caught:
                runner.lane_for(SimpleNamespace(lane='chatgpt:saved-medium'))
        self.assertEqual(caught.exception.code, 6)
        self.assertIn('gateway configuration', caught.exception.message)

    def test_gateway_settings_reject_hosts_the_service_does_not_accept(self):
        for base in ['http://localhost:4319/v1', 'http://[::1]:4319/v1',
                     'http://127.0.0.1:4319/v1?redirect=1', 'https://127.0.0.1:4319/v1',
                     'http://user@127.0.0.1:4319/v1']:
            lane = copy.deepcopy(self.template)
            lane['transport']['api_base'] = base
            with self.subTest(base=base), self.assertRaises(transport.Rejected):
                transport.settings(lane)

    def test_selector_preserves_opaque_labels_without_decoding(self):
        for value in ['chatgpt:saved-medium', 'chatgpt:Future_5', 'chatgpt:Family @ pro', 'chatgpt:%6Catest', 'chatgpt:λ']:
            self.assertEqual(transport.canonical_selector(value), value.removeprefix('chatgpt:'))
        for value in ['chatgpt:', 'chatgpt:bad\nlabel', 'chatgpt: label', 'chatgpt:' + 'λ' * 65, 'chatgpt:saved\n']:
            with self.subTest(value=value), self.assertRaises(transport.Rejected):
                transport.canonical_selector(value)
        self.assertIsNone(transport.canonical_selector('other:saved-medium'))


if __name__ == '__main__':
    unittest.main()
