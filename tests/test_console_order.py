"""Display ordering and its protected runtime POST, using the real HTTP handler without sockets."""
import copy
import html
from pathlib import Path
import json
import re
import unittest
import urllib.parse

from tests import test_console_updates as transport
from tests.test_console import console, fleetctl, base_overlay


class OrderHTTPTests(transport.ConsoleMemoryTests):
    def order_post(self, order, headers=None, token=None):
        return self.request('POST', '/order', body=urllib.parse.urlencode({
            'order': json.dumps(order), 't': self.app.form_token if token is None else token}),
            headers=headers or {'Cookie': self.cookie(), 'Origin': self.origin(), 'Accept': 'application/json'})

    def test_order_round_trip_is_atomic_and_display_only(self):
        original = self.app.overview(refresh=False)
        old_runtime = fleetctl.load_json(self.state / 'runtime.json', {}) or {}
        old_overlay = self.overlay.read_text()
        order = original['console_order']
        saved = {key: copy.deepcopy(order[key]) for key in ('pools', 'models', 'sort')}
        saved['pools'].reverse()
        saved['sort']['pools'] = 'your'
        for pool in saved['models']:
            saved['models'][pool].reverse()
        response, body = self.order_post(saved)
        self.assertEqual(response.status, 200)
        snapshot = json.loads(body)
        runtime = fleetctl.load_json(self.state / 'runtime.json')
        self.assertEqual(runtime['console_order'], saved)
        runtime.pop('console_order')
        old_runtime.pop('console_order', None)
        runtime.pop('updated_at', None)
        old_runtime.pop('updated_at', None)
        self.assertEqual(runtime, old_runtime)
        self.assertEqual(self.overlay.read_text(), old_overlay)
        relaunched = console.Console(self.overlay, self.state, self.port).overview(refresh=False)
        self.assertEqual(relaunched['console_order']['pools'], saved['pools'])
        self.assertEqual(relaunched['console_order']['models'], saved['models'])
        canonical = fleetctl.fleet_overview(fleetctl.read_overlay(self.overlay),
                                           fleetctl.load_json(self.state / 'runtime.json'), self.state)
        expected = fleetctl.render_brief(canonical, **relaunched['_brief_context'])
        self.assertEqual(snapshot['brief'], expected)
        rendered = console.render_page(relaunched, self.app.form_token)
        shown = re.search(r'<pre id="agent-brief">(.*?)</pre>', rendered, re.S).group(1)
        self.assertEqual(html.unescape(shown), expected)
        self.assertEqual(snapshot['brief_full'], fleetctl.render_brief(canonical, verbose=True))

    def test_order_post_has_existing_security_guards(self):
        view = self.app.overview(refresh=False)['console_order']
        saved = {key: view[key] for key in ('pools', 'models', 'sort')}
        cookie = self.cookie()
        for headers, status in [({'Origin': self.origin()}, 401), ({'Cookie': cookie}, 403),
                                ({'Cookie': cookie, 'Origin': 'http://evil.example'}, 403),
                                ({'Cookie': cookie, 'Origin': self.origin(), 'Sec-Fetch-Site': 'cross-site'}, 403)]:
            response, _ = self.order_post(saved, headers=headers)
            self.assertEqual(response.status, status)
        for token in ('', 'stale'):
            response, _ = self.order_post(saved, token=token)
            self.assertEqual(response.status, 403)
        response, _ = self.request('POST', '/order', host='evil.example',
                                  headers={'Cookie': cookie, 'Origin': self.origin()}, body='t=unused')
        self.assertEqual(response.status, 421)

    def test_malformed_order_returns_400_and_preserves_runtime(self):
        good = {'pools': [], 'models': {}, 'sort': {'pools': 'your', 'models': {}}}
        malformed = [[], {}, dict(good, pools=['x', 'x']), dict(good, pools=[{}]),
                     dict(good, sort={'pools': [], 'models': {}}),
                     dict(good, sort={'pools': 'your', 'models': {'x': {}}}),
                     dict(good, sort={'pools': 'bogus', 'models': {}})]
        before = fleetctl.load_json(self.state / 'runtime.json', {})
        for order in malformed:
            response, _ = self.order_post(order)
            self.assertEqual(response.status, 400, repr(order))
            self.assertEqual(fleetctl.load_json(self.state / 'runtime.json', {}), before)
        response, _ = self.request('POST', '/order',
                                  headers={'Cookie': self.cookie(), 'Origin': self.origin()},
                                  body=urllib.parse.urlencode({'t': self.app.form_token, 'order': '{'}))
        self.assertEqual(response.status, 400)

    def test_large_valid_order_is_accepted(self):
        order = {'pools': [f'future-{i:04d}' for i in range(400)], 'models': {},
                 'sort': {'pools': 'your', 'models': {}}}
        self.assertGreater(len(json.dumps(order)), 4096)
        response, _ = self.order_post(order)
        self.assertEqual(response.status, 200)


class OrderCriteriaTests(unittest.TestCase):
    def setUp(self):
        self.overview = {'pools': [
            {'pool': 'a', 'label': 'Zulu', 'quota': {'used_percent': 80, 'resets_in_s': 100},
             'limits': [{'resets_in_s': 5}], 'options': [{'model': 'x'}, {'model': 'y'}, {'model': 'z'}]},
            {'pool': 'b', 'label': 'Beta', 'quota': {'used_percent': 20, 'resets_in_s': 30}, 'options': []},
            {'pool': 'c', 'label': 'Alpha', 'quota': None, 'options': []}],
            'models': [{'model': 'x', 'card': {'name': 'Zulu'}}, {'model': 'y', 'card': {'name': 'Alpha'}},
                       {'model': 'z', 'card': {'name': 'Beta'}}]}
        self.saved = {'pools': ['b', 'unknown', 'a'], 'models': {'a': ['y', 'retired', 'x']},
                      'sort': {'pools': 'your', 'models': {'a': 'your'}}}

    def test_new_pools_and_models_append_in_roster_order(self):
        roster = {'quota_pools': {'a': {}, 'b': {}, 'd': {}, 'c': {}}}
        self.overview['pools'].append({'pool': 'd', 'label': 'Delta', 'options': []})
        order = console.display_order(self.overview, self.saved, roster)
        self.assertEqual(order['pools'], ['b', 'a', 'd', 'c'])
        self.assertEqual(order['models']['a'], ['y', 'x', 'z'])

    def test_every_pool_sort_and_unknown_readings(self):
        order = console.display_order(self.overview, self.saved)
        self.assertEqual(order['ranks']['pools'], {'your': ['b', 'a', 'c'], 'quota': ['b', 'a', 'c'],
                                                 'reset': ['a', 'b', 'c'], 'name': ['c', 'b', 'a']})

    def test_metered_budget_reading_is_quota_evidence(self):
        self.overview['pools'][2]['limits'] = [{'kind': 'budget', 'used_percent': 5}]
        self.assertEqual(console.display_order(self.overview)['ranks']['pools']['quota'], ['c', 'b', 'a'])

    def test_every_model_sort_uses_evidence_and_unknowns_last(self):
        evidence = {'rows': [
            {'model_key': 'x', 'q': {'review': {'mean': .6}}, 'price_1m': {'in': 1, 'out': 1}},
            {'model_key': 'y', 'q': {'review': {'mean': .9}}, 'price_1m': {'in': 2, 'out': 2}},
            {'model_key': 'z', 'q': {'review': {'mean': .99, 'unknown': True}}, 'price_1m': {'in': None, 'out': None}}]}
        order = console.display_order(self.overview, self.saved, evidence=evidence)
        self.assertEqual(order['ranks']['models']['a'], {'your': ['y', 'x', 'z'], 'quality': ['y', 'x', 'z'],
                                                       'cheapest': ['x', 'y', 'z'], 'name': ['y', 'z', 'x']})
        fallback = console.display_order(self.overview, self.saved)
        self.assertEqual(fallback['ranks']['models']['a']['quality'], ['x', 'y', 'z'])

class LifecycleOrderTests(unittest.TestCase):
    def test_chat_picker_levels_are_quality_ordered_even_with_sparse_evidence(self):
        keys = ['high', 'instant', 'medium', 'pro', 'xhigh']
        overview = {'pools': [{'pool': 'chat', 'label': 'Chat',
                    'options': [{'model': key, 'current': True} for key in keys]}],
                    'models': [{'model': key, 'lanes': [{'harness': 'chatgpt-chat', 'worker_level': key}]}
                               for key in keys]}
        sparse = {'rows': [{'model_key': 'instant', 'q': {'review': {'mean': .9}}}]}
        order = console.display_order(overview, evidence=sparse)
        expected = ['pro', 'xhigh', 'high', 'medium', 'instant']
        self.assertEqual(order['ranks']['models']['chat']['quality'], expected)
        self.assertEqual(order['sort']['models']['chat'], 'quality')
        self.assertEqual(order['models']['chat'], expected)

    def test_every_provider_keeps_older_section_under_every_sort_and_custom_order(self):
        roster = base_overlay()
        overview = fleetctl.fleet_overview(roster, {}, Path('/tmp'))
        by_key = {m['model']: m for m in overview['models']}
        count = 0
        for criterion in console.MODEL_SORTS:
            saved = {'pools': [], 'models': {}, 'sort': {'pools': 'name', 'models': {}}}
            for pool in overview['pools']:
                saved['models'][pool['pool']] = [o['model'] for o in reversed(pool.get('options', []))]
                saved['sort']['models'][pool['pool']] = criterion
            order = console.display_order(overview, saved)
            for pool in overview['pools']:
                older = [o for o in pool.get('options', []) if not o['current']]
                if not older:
                    continue
                count += 1
                markup = console._picker(pool, by_key, 'test', order)
                section = re.search(r'<details class="older">(.*)</ul></details>', markup, re.S)
                self.assertIsNotNone(section, (pool['pool'], criterion))
                for option in older:
                    self.assertIn(f'data-model="{option["model"]}"', section.group(1))
                    self.assertNotIn(f'data-model="{option["model"]}"', markup[:section.start()])
        self.assertGreater(count, 4)


if __name__ == "__main__":
    unittest.main()
