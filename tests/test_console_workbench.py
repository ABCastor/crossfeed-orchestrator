"""The workbench's read preview and existing audited writes, on disposable state."""
import datetime as dt
import json
import tempfile
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from tests import test_console, test_console_updates
from tests.test_console_agy import Page
from tests.test_selector import quality_row

console = test_console.console
fleet = console.fleetctl


class WorkbenchTests(unittest.TestCase):
    request = test_console_updates.ConsoleMemoryTests.request
    port = 8768
    pool = 'claude'
    model = 'claude-sonnet-5-5'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        roster = test_console.base_overlay()
        roster.pop('chatgpt_gateway', None)
        roster.pop('provider_sources', None)
        roster['model_cards'][self.model]['context_length'] = 123000
        self.overlay = self.state / 'overlay.json'
        self.overlay.write_text(json.dumps(roster), encoding='utf-8')
        self.original_overlay = self.overlay.read_bytes()
        self.app = console.Console(self.overlay, self.state, self.port)
        no_refresh = patch.object(console.Console, 'refresh_in_background', autospec=True)
        no_refresh.start()
        self.addCleanup(no_refresh.stop)
        self.runtime = self.state / 'runtime.json'
        now = fleet.utc_now()
        snap = {'observed_at': fleet.iso(now), 'source': 'test', 'windows': {'weekly': {
            'used_percent': 27, 'window_minutes': 10080, 'reset_at': fleet.iso(now + dt.timedelta(days=1))}}}
        self.runtime.write_text(json.dumps({'quota_snapshots': {self.pool: snap}}))
        directory = self.state / 'evidence'
        directory.mkdir()
        rows = [quality_row(self.model, mean=.89, family='coding-agent'),
                quality_row('gpt-6.1-sol', mean=.81, family='coding-agent')]
        (directory / 'levels.json').write_text(json.dumps({'rows': rows}))

    def headers(self):
        return {'Cookie': f'{console.COOKIE}={self.app.session}', 'Origin': f'http://127.0.0.1:{self.port}'}

    def get(self, path='/'):
        return self.request('GET', path, self.headers())

    def post(self, endpoint, fields, *, token=None):
        body = urllib.parse.urlencode({'t': token if token is not None else self.app.form_token,
                                      'return-role': 'debug', 'return-value': 'absolute-best', **fields})
        return self.request('POST', endpoint, self.headers(), body)[0]

    def switch(self, page, name, *, within=None):
        return next(node['attrs'] for node in page.nodes if node['attrs'].get('role') == 'switch'
                    and node['attrs'].get('name') == name
                    and (within is None or page.ident(within) in node['parents']))

    def page(self, path='/'):
        response, payload = self.get(path)
        self.assertEqual(response.status, 200)
        return Page(payload.decode())

    def relaunch(self):
        self.app = console.Console(self.overlay, self.state, self.port)

    def test_signed_in_preview_compares_real_selector_cells_without_writes(self):
        before = self.runtime.read_bytes()
        view = self.app.overview(refresh=False)
        actual = console.workbench_data(view, self.state, value='absolute-best')
        oracle = console.selector.select_option(view['_brief_context']['roster'], view['_brief_context']['runtime'],
                                                self.state, 'implementation', value_mode='absolute-best',
                                                fleet=types.SimpleNamespace(**vars(fleet)), persist_receipts=False)
        self.assertEqual(actual['pick'], oracle['choice'])
        self.assertNotEqual(actual['pick']['model_key'], actual['runner']['model_key'])
        choices = list(actual['menu'])[:4]
        query = urllib.parse.urlencode([('preview-value', 'absolute-best'),
                                       *[('compare', key) for key in choices]])
        _, payload = self.get('/?' + query)
        page = Page(payload.decode())
        self.assertEqual(self.switch(page, 'preview-value')['aria-checked'], 'true')
        table = next(node for node in page.nodes if node['tag'] == 'table' and node['attrs'].get('class') == 'wb-matrix')
        self.assertEqual(sum(node['tag'] == 'th' and node['attrs'].get('scope') == 'col' and table in node['parents']
                             for node in page.nodes), 5)
        _, details = self.get('/?value=absolute-best&' + urllib.parse.urlencode({'compare': f'{self.pool}:{self.model}:high'}))
        self.assertIn(b'0.890', details)
        self.assertIn(b'123,000 tokens', details)
        self.assertIn(b'73% left', details)
        self.assertEqual(self.runtime.read_bytes(), before)
        self.assertFalse((self.state / 'selections').exists())
        self.assertEqual(self.overlay.read_bytes(), self.original_overlay)

    def test_preview_rejects_invalid_query_and_does_not_write(self):
        before = self.runtime.read_bytes()
        for path in ('/?role=bogus', '/?value=bogus', '/?preview-value=bogus', '/?compare=x' + '&compare=y' * 4):
            self.assertEqual(self.get(path)[0].status, 400, path)
        self.assertEqual(self.request('GET', '/?role=review')[0].status, 401)
        self.assertEqual(self.runtime.read_bytes(), before)
        self.assertEqual(self.overlay.read_bytes(), self.original_overlay)

    def test_model_switch_refusal_off_reload_and_on_under_same_subscription(self):
        ident = console._model_id(self.pool, self.model)
        initial = self.page()
        switch = self.switch(initial, 'switch', within=ident)
        self.assertEqual((switch['aria-checked'], switch['value']), ('true', f'{self.model}=off'))
        self.assertIn(initial.ident('pool-claude'), initial.ident(ident)['parents'])
        before = self.runtime.read_bytes()
        for invalid in ('nonexistent=off', f'{self.model}=maybe'):
            self.assertEqual(self.post('/model', {'pool': self.pool, 'switch': invalid}).status, 400)
        self.assertEqual(self.post('/model', {'pool': self.pool, 'switch': switch['value']}, token='stale').status, 403)
        self.assertEqual(self.runtime.read_bytes(), before)
        response = self.post('/model', {'pool': self.pool, 'switch': switch['value']})
        self.assertEqual(response.status, 303)
        self.assertIn('role=debug&value=absolute-best#pick-claude', response.getheader('Location'))
        self.relaunch()
        offline = self.page(response.getheader('Location').split('#')[0])
        switch = self.switch(offline, 'switch', within=ident)
        self.assertEqual((switch['aria-checked'], switch['value']), ('false', f'{self.model}=on'))
        self.assertIn(offline.ident('pool-claude'), offline.ident(ident)['parents'])
        self.assertEqual(self.post('/model', {'pool': self.pool, 'switch': switch['value']}).status, 303)
        self.relaunch()
        self.assertEqual(self.switch(self.page(), 'switch', within=ident)['aria-checked'], 'true')
        events = json.loads(self.runtime.read_text())['control_history']
        self.assertEqual([(event['key'], event['actor'], event['value']) for event in events],
                         [(f'model:{self.pool}:{self.model}', 'owner', False),
                          (f'model:{self.pool}:{self.model}', 'owner', True)])
        self.assertEqual(self.overlay.read_bytes(), self.original_overlay)

    def test_pool_switch_refusal_off_reload_and_on_at_normal(self):
        switch = self.switch(self.page(), 'level', within='pool-claude')
        self.assertEqual((switch['aria-checked'], switch['value']), ('true', 'off'))
        before = self.runtime.read_bytes()
        for pool, level in (('nonexistent', 'off'), (self.pool, 'bogus')):
            self.assertEqual(self.post('/level', {'pool': pool, 'level': level}).status, 400)
        self.assertEqual(self.post('/level', {'pool': self.pool, 'level': 'off'}, token='stale').status, 403)
        self.assertEqual(self.runtime.read_bytes(), before)
        self.assertEqual(self.post('/level', {'pool': self.pool, 'level': switch['value']}).status, 303)
        self.relaunch()
        switch = self.switch(self.page(), 'level', within='pool-claude')
        self.assertEqual((switch['aria-checked'], switch['value']), ('false', 'normal'))
        self.assertEqual(self.post('/level', {'pool': self.pool, 'level': switch['value']}).status, 303)
        self.relaunch()
        self.assertEqual(self.switch(self.page(), 'level', within='pool-claude')['aria-checked'], 'true')
        events = json.loads(self.runtime.read_text())['control_history']
        self.assertEqual([(event['key'], event['actor'], event['value']) for event in events],
                         [('level:claude', 'owner', 'off'), ('level:claude', 'owner', 'normal')])
        self.assertEqual(self.overlay.read_bytes(), self.original_overlay)

    def test_unwritable_controls_are_disabled_and_have_no_mutation_endpoint(self):
        before = self.runtime.read_bytes()
        page = self.page()
        for name, endpoint in (('explicit-only', '/explicit-only'), ('role-value', '/value-mode')):
            switch = self.switch(page, name)
            self.assertIn('disabled', switch)
            self.assertTrue(page.ident(switch['aria-describedby']))
            self.assertEqual(self.post(endpoint, {name: 'on'}).status, 404)
        self.assertEqual(self.runtime.read_bytes(), before)
        self.assertEqual(self.overlay.read_bytes(), self.original_overlay)

    def test_spend_policy_defaults_open_and_inventory_remains_a_drawer(self):
        page = self.page()
        policies = [node for node in page.nodes if node['tag'] == 'details'
                    and node['attrs'].get('class') == 'wb-advanced']
        self.assertGreater(len(policies), 1)
        self.assertTrue(all('open' in policy['attrs'] for policy in policies))
        self.assertNotIn('open', page.ident('pick-claude')['attrs'])
        compare = page.ident('compare')
        wallet = page.ident('wallet-heading')
        self.assertLess(page.nodes.index(compare), page.nodes.index(wallet))

    def test_archived_and_retired_models_do_not_enter_workbench_inventory_or_menu(self):
        # A direct or active lane may still be present; the archival filter does not mutate the roster.
        home = self.state / 'fixture-home'
        archive = home / '.claude' / 'roster' / 'models' / '_archive'
        archive.mkdir(parents=True)
        (archive / self.model).mkdir()
        roster = json.loads(self.overlay.read_text())
        retired = 'gpt-6.1-sol'
        roster['model_cards'][retired]['status'] = 'retired'
        self.overlay.write_text(json.dumps(roster))
        before = self.overlay.read_bytes()
        with patch.object(console.Path, 'home', return_value=home):
            query = urllib.parse.urlencode({'fragment': 'compare', 'role': 'implementation',
                                            'compare': f'{self.pool}:{self.model}:high'})
            status, body = self.get('/?' + query)
            self.assertEqual(status.status, 200)
            self.assertNotIn(b'<main>', body)
            self.assertIn(b'id="compare"', body)
            self.assertNotIn(self.model.encode(), body)
            self.assertNotIn(retired.encode(), body)
            page = self.page()
            for key in (self.model, retired):
                self.assertFalse(any(node['attrs'].get('data-model') == key for node in page.nodes))
        self.assertEqual(self.overlay.read_bytes(), before)
        self.assertFalse((self.state / 'selections').exists())

    def test_fetch_write_returns_snapshot_without_redirecting(self):
        headers = {**self.headers(), 'Accept': 'application/json'}
        body = urllib.parse.urlencode({'t': self.app.form_token, 'pool': self.pool,
                                       'switch': f'{self.model}=off'})
        response, payload = self.request('POST', '/model', headers, body)
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader('Location'))
        pool = next(p for p in json.loads(payload)['pools'] if p['pool'] == self.pool)
        self.assertNotIn(self.model, pool['on'])
        self.assertIn('wallet_gauge', pool)

    def test_cash_is_route_specific_and_failed_readings_are_not_current_headroom(self):
        option = {'pool': 'example', 'model_key': 'example', 'funding': 'included'}
        score = {'q': {'mean': .8, 'unknown': False}, 'cost': {'percent': .32, 'estimated': True},
                 'cash': {'estimate_usd': .024, 'api_equivalent_usd': .018}}
        pool = {'label': 'Example', 'gauge_error': 'not refreshed'}
        cells = console._compare_cells(option, score, pool, {})
        self.assertEqual(cells, ['0.800', '≈0.32%', '—', '—', 'Example', 'Refresh failed'])
        self.assertEqual(console._compare_cells({**option, 'funding': 'metered'}, score, pool, {})[2], '≈$0.0240')
        self.assertEqual(console._compare_cells(option, None, {'stale_age_s': 200}, {})[-1], 'Stale')

    def test_antigravity_is_one_group_with_visible_separate_allowances(self):
        page = self.page()
        group = page.ident('provider-antigravity')
        details = page.ident('antigravity-allowances')
        self.assertIn('open', details['attrs'])
        for key in json.loads(group['attrs']['data-pool-members']):
            node = page.ident('pool-' + key)
            self.assertIn(group, node['parents'])
            self.assertIn(details, node['parents'])
            self.assertTrue(page.ident('pick-' + key))
        self.assertNotIn(group, page.ident('pool-gemini-metered')['parents'])


if __name__ == '__main__':
    unittest.main()
