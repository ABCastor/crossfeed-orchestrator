"""The workbench's read preview and existing audited writes, on disposable state."""
import datetime as dt
import json
import stat
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
        # The frozen fixture predates the model-scoped Argon pool.
        argon = json.loads((test_console.ROOT / 'examples/access-overlay.example.json').read_text())
        roster['quota_pools']['antigravity-argon'] = argon['quota_pools']['antigravity-argon']
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
        modes = {node['attrs']['value']: node['attrs']['aria-pressed'] for node in page.nodes
                 if node['attrs'].get('name') == 'preview-value'}
        self.assertEqual(modes, {'best-value': 'false', 'absolute-best': 'true'})
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

    def test_default_does_not_repeat_implementation_grade_row(self):
        response, payload = self.get('/?role=default')
        self.assertEqual(response.status, 200)
        self.assertIn(b'<th scope="row">Implementation</th>', payload)
        self.assertNotIn(b'<th scope="row">Default</th>', payload)
        self.assertIn(b'The selector converts grades to a success estimate.', payload)

    def test_legacy_role_floor_never_labels_a_grade(self):
        item = {'pool': 'claude', 'model_key': 'claude-opus-5-5',
                'model_identity': 'claude-opus-5-5', 'level': 'xhigh'}
        q = {'mean': .809, 'n_sources': 1, 'unknown': False}
        scored = dict(item, q=q)
        data = {'role': 'hard-reasoning', 'value': 'best-value', 'pick': scored,
                'evidence': {('claude-opus-5-5', 'xhigh'): {'q': {'reasoning': q}}}}
        policy = {'quality_floors_by_role': {'hard-reasoning': .85}}
        pick = console._comparison_grade(data, item, scored, 'hard-reasoning', policy)
        self.assertIn('0.809', pick)
        self.assertIn('Pick', pick)
        self.assertNotIn('Below floor', pick)
        data['pick'] = None
        unpicked = console._comparison_grade(data, item, None, 'hard-reasoning', policy)
        self.assertIn('0.809', unpicked)
        self.assertNotIn('Pick', unpicked)
        self.assertNotIn('Below floor', unpicked)

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
        roster = json.loads(self.overlay.read_text())
        snapshot = self.app.overview(refresh=False)
        pool = next(p for p in snapshot['pools'] if p['pool'] == self.pool)
        model = next(o for o in pool['options'] if o['model'] == self.model)
        pool['archived'] = [dict(model, archived=True, set_by='agent', set_at=snapshot['generated_at'])]
        pool['options'].remove(model)
        retired = 'gpt-6.1-sol'
        roster['model_cards'][retired]['status'] = 'retired'
        self.overlay.write_text(json.dumps(roster))
        before = self.overlay.read_bytes()
        snapshot['_brief_context']['roster'] = fleet.read_overlay(self.overlay, self.state, discover=False)
        with patch.object(self.app, 'overview', return_value=snapshot):
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
            restore = next(node for node in page.nodes if node['attrs'].get('name') == 'restore')
            self.assertEqual(restore['attrs']['value'], self.model)
            self.assertIn(page.ident('archived'), restore['parents'])
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

    def test_antigravity_is_one_card_with_one_subscription_policy_and_member_switches(self):
        _, payload = self.get()
        self.assertIn(b'<h2>Google AI Pro</h2>', payload)
        page = Page(payload.decode())
        group = page.ident('provider-antigravity')
        rows = page.ident('antigravity-allowances')
        members = json.loads(group['attrs']['data-pool-members'])
        self.assertEqual(members, list(console.AGY_ALLOWANCES))
        self.assertFalse(any(node['tag'] == 'details' for node in rows['parents']))
        slider, = [node for node in page.nodes if group in node['parents'] and node['attrs'].get('role') == 'radiogroup']
        self.assertNotIn(rows, slider['parents'])
        policy = page.ident('policy-antigravity')
        self.assertIn('open', policy['attrs'])
        self.assertIn(policy, slider['parents'])
        self.assertIn(policy, page.ident('actor-antigravity')['parents'])
        stops = [node['attrs'] for node in page.nodes if slider in node['parents'] and node['attrs'].get('role') == 'radio']
        self.assertEqual([(stop['value'], stop['aria-label']) for stop in stops], list(console.LEVEL_WORDS.items()))
        pool_field, = [node for node in page.nodes if slider in node['parents'] and node['attrs'].get('name') == 'pool']
        self.assertEqual(pool_field['attrs']['value'], 'antigravity')
        for key in members:
            row = page.ident('pool-' + key)
            self.assertIn(rows, row['parents'])
            self.assertNotEqual(row['tag'], 'article', 'no nested subscription card')
            pick = page.ident('pick-' + key)
            self.assertIn(row, pick['parents'])
            model_pool, = [node for node in page.nodes if pick in node['parents'] and node['attrs'].get('name') == 'pool']
            self.assertEqual(model_pool['attrs']['value'], key)
        self.assertNotIn(group, page.ident('pool-gemini-metered')['parents'])

    def test_session_and_old_form_survive_relaunch_but_not_another_install(self):
        headers = self.headers()
        token = self.app.form_token
        startup_key = self.app.login_key
        self.relaunch()
        self.assertNotEqual(startup_key, self.app.login_key)
        self.assertEqual(self.request('GET', '/snapshot', headers)[0].status, 200)
        self.assertEqual(self.request('GET', '/snapshot')[0].status, 401)
        self.assertEqual(self.post('/level', {'pool': self.pool, 'level': 'low'}, token=token).status, 303)
        key = self.state / 'console-session.key'
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        other = console.Console(self.overlay, self.state / 'independent', self.port)
        self.assertFalse(other.signed_in(headers))
        self.assertFalse(other.form_ok(token))

    def test_one_model_quotes_raw_price_separately_from_quota(self):
        overview = self.app.overview(refresh=False)
        data = console.workbench_data(overview, self.state, 'implementation', count=1)
        key, = data['keys']
        data['scores'][key].update(quota_cost_usd=.075, api_cost_usd=.012,
                                   quota_basis={'plan': 'Example Go', 'window': 'weekly', 'window_limit': 80,
                                                'share_percent': .1, 'plan_value_usd': 50,
                                                'why': 'The estimated debit comes from published rates.'})
        markup = console._workbench(overview, data)
        self.assertIn('data-count="1"', markup)
        self.assertIn('API cost / job', markup)
        self.assertIn('$0.0120', markup)
        self.assertIn('$0.0750', markup)
        self.assertIn('A typical job uses about 0.1%', markup)
        self.assertIn('Why it leads:', markup)

    def test_benchmark_link_is_local_attributed_safe_and_revisited(self):
        href = 'https://example.com/verified-results'
        fields = {'model': self.model, 'url': href, 'label': 'Independent <board>'}
        self.assertEqual(self.post('/benchmark-link', fields, token='invalid').status, 403)
        self.assertEqual(self.post('/benchmark-link', {**fields, 'url': 'javascript:alert(0)'}).status, 400)
        saved = self.post('/benchmark-link', fields)
        self.assertEqual(saved.status, 303)
        self.assertEqual(saved.getheader('Location'), f'/benchmarks#model-{self.model}')
        self.assertEqual(self.post('/benchmark-link', fields).status, 303)
        links = json.loads(self.runtime.read_text())['console_benchmark_links'][self.model]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['set_by'], 'operator')
        self.relaunch()
        status, page = self.get('/benchmarks')
        self.assertEqual(status.status, 200)
        self.assertIn(b'Independent &lt;board&gt;', page)
        self.assertIn(b'Added by you', page)
        self.assertIn(b'action="/benchmark-link"', page)

    def test_model_families_are_contiguous_by_peak_cost_then_descending_within_family(self):
        options = [{'model': name, 'family': family, 'typical_cost_usd': cost}
                   for name, family, cost in [('C-low', 'C', .1), ('B-high', 'B', .3), ('A', 'A', .4),
                                              ('B-low', 'B', .2), ('C-high', 'C', .5), ('D', 'D', None)]]
        self.assertEqual([o['model'] for o in console._sort_model_entries(options, {})],
                         ['C-high', 'C-low', 'A', 'B-high', 'B-low', 'D'])

    def test_plan_extras_do_not_confuse_zero_cost_with_missing_data(self):
        view = {'pool': 'openrouter-free', 'credits': {'balance': 3, 'unit': 'USD', 'expires_at': None,
                'renews_at': '2026-12-02T00:00:00Z'}, 'usage_cost': {'today_usd': 0, 'last_30d_usd': .25},
                'per_model': [{'model': 'Fixture Model', 'usd_30d': .25, 'requests': 2}]}
        markup = console._plan_extras(view)
        for text in ('3 USD', 'Expires not reported', 'New credits arrive', '$0.0000', '$0.2500',
                     'Status page', 'Fixture Model', '2 requests'):
            self.assertIn(text, markup)

    def test_spend_attribution_follows_the_last_saved_actor_not_the_level(self):
        now = fleet.utc_now()
        runtime = json.loads(self.runtime.read_text())
        with patch.object(fleet, 'utc_now', return_value=now - dt.timedelta(days=1)):
            fleet.set_pool_level(runtime, self.pool, 'forced', actor='agent')
        self.runtime.write_text(json.dumps(runtime))
        _, before = self.get()
        self.assertIn(b'data-level-actor="agent"', before)
        self.assertIn('Set by an agent · yesterday'.encode(), before)

        body = urllib.parse.urlencode({'t': self.app.form_token, 'pool': self.pool, 'level': 'low'})
        res, payload = self.request('POST', '/level', {**self.headers(), 'Accept': 'application/json'}, body)
        self.assertEqual(res.status, 200)
        self.assertIsNone(res.getheader('Location'))
        saved = next(p for p in json.loads(payload)['pools'] if p['pool'] == self.pool)
        self.assertEqual(saved['level'], 'low')
        self.assertEqual(saved['control_actor_kind'], 'owner')
        self.assertRegex(saved['wallet_control_actor'], r'^Set by you · today \d\d:\d\d$')
        on_disk = json.loads(self.runtime.read_text())
        self.assertEqual(fleet.control_record(on_disk, f'level:{self.pool}')['actor'], 'owner')
        self.assertEqual(fleet.pool_level(on_disk, 'antigravity-argon'), 'normal')
        _, after = self.get()
        self.assertIn(b'data-level-actor="owner"', after)
        self.assertIn(saved['wallet_control_actor'].encode(), after)
        self.assertIn(b'Last change not recorded', after)

    def test_refusals_and_scoped_windows_use_the_wallet_rows(self):
        now = fleet.utc_now()
        reset_at = fleet.iso(now + dt.timedelta(days=3))
        limit = {'title': 'Weekly', 'scope': 'Fable only', 'kind': 'window', 'used_percent': 42,
                 'reset_at': reset_at, 'resets': 'Friday'}
        base = {'pool': 'claude', 'state': 'ABUNDANT', 'limits': [limit], 'plan': {}}
        self.assertIn('class="wb-limit"><span>Fable weekly</span>', console._work_gauge(base))
        argon = {**base, 'pool': 'antigravity-argon', 'limits': [{**limit, 'scope': None}]}
        self.assertIn('class="wb-limit"><span>Argon weekly</span>', console._work_gauge(argon))
        refusal = console._work_gauge({**argon, 'limits': [], 'state': 'EXHAUSTED', 'until': reset_at})
        self.assertIn(f'Used up · back <time datetime="{reset_at}">', refusal)
        self.assertNotIn('% left', refusal)


if __name__ == '__main__':
    unittest.main()
