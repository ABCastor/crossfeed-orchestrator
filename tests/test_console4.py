"""Admission and funding, not model version numbers, define the control page."""
import tempfile
import unittest
from pathlib import Path

from tests.test_console import console

fleetctl = console.fleetctl


def roster_fixture():
    def lane(model, pool, harness, provider, **extra):
        return dict(lane_id=model, model_key=model, quota_pool=pool, harness=harness,
                    provider=provider, selector=model, admission_status='active',
                    access_status='verified', allowed_modes=['read-only'], roles=[], **extra)

    return {
        'quota_pools': {
            'codex': {'label': 'Codex (ChatGPT)', 'plan': {'name': 'ChatGPT Pro'}},
            'antigravity-gemini': {'label': 'Antigravity · Gemini', 'plan': {'name': 'AI Pro'}},
            'antigravity-3p': {'label': 'Antigravity · Claude and GPT', 'plan': {'name': 'AI Pro'}},
            'antigravity': {},
        },
        'lanes': [
            lane('gpt-6.1-sol', 'codex', 'codex', 'openai'),
            lane('gpt-6-astra', 'codex', 'codex', 'openai'),
            lane('gpt-6-luna', 'codex', 'codex', 'openai'),
            lane('gemini-3.8-flash', 'antigravity-gemini', 'agy', 'antigravity'),
            lane('gemini-3.6-flash', 'antigravity-gemini', 'agy', 'antigravity'),
            lane('gemini-3.1-pro', 'antigravity-gemini', 'agy', 'antigravity'),
            lane('claude-opus-4-6-thinking', 'antigravity-3p', 'agy', 'antigravity'),
        ],
        'catalogue_only': [
            {'model_key': 'gpt-oss-120b', 'selector': 'agy/gpt-oss-120b-medium'},
            {'model_key': 'gemini-3.5-flash', 'selector': 'agy/gemini-3.5-flash-high'},
        ],
        'model_evidence': {
            'gpt-5.6-luna': {'status': 'fallback-only'},
            'gpt-5.6-sol-and-terra': {'status': 'retired-from-routing'},
        },
        'routing': {'roles': {
            'implementation': {'quality_first': ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna']},
            'repo-map': {'quality_first': ['gpt-6-luna', 'gpt-6.1-sol']},
            'review': {'quality_first': ['gemini-3.8-flash', 'claude-opus-4-6-thinking']},
        }},
    }


def picker_html(overview, pool):
    """The rendered model picker of one provider, as the page shows it."""
    by_key = {m['model']: m for m in overview['models']}
    return console._picker(next(p for p in overview['pools'] if p['pool'] == pool), by_key, 'fixture')


class CurrentArchiveTests(unittest.TestCase):
    def overview(self, roster=None, runtime=None):
        with tempfile.TemporaryDirectory() as directory:
            return fleetctl.fleet_overview(roster or roster_fixture(), runtime or {}, Path(directory))

    def test_codex_admitted_models_current_and_evidence_only_archived(self):
        overview = self.overview()
        models = {m['model']: m for m in overview['models']}
        for name in ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna'):
            self.assertEqual(models[name]['current_pools'], ['codex'])
        for name in ('gpt-5.6-luna', 'gpt-5.6-sol-and-terra'):
            self.assertEqual(models[name]['pools'], ['codex'])
            self.assertEqual(models[name]['current_pools'], [])
        rendered = picker_html(overview, 'codex')
        current, archive = rendered.split('<details class="older">')
        # Best rank across ALL roles, with name breaking the rank-1 tie.
        self.assertLess(current.index('gpt-6-astra'), current.index('gpt-6-luna'))
        self.assertLess(current.index('gpt-6-luna'), current.index('gpt-6.1-sol'))
        self.assertIn('gpt-5.6-luna', archive)

    def test_real_overlay_evidence_shape_cannot_admit_codex_models(self):
        roster = roster_fixture()
        for lane in roster['lanes'][:3]:
            roster['model_evidence'][lane['model_key']] = {
                'status': 'provisional', 'runs': 2, 'harness': 'codex exec (ChatGPT Pro login)',
                'note': 'Used daily; this prose is not a routing admission.',
            }
        roster['lanes'] = roster['lanes'][3:]
        codex = [m for m in self.overview(roster)['models'] if 'codex' in m['pools']]
        self.assertEqual(len(codex), 5)
        self.assertTrue(all(not m['current_pools'] for m in codex))

    def test_gemini_pro_and_older_flash_remain_current_without_ranking(self):
        models = {m['model']: m for m in self.overview()['models']}
        for name in ('gemini-3.1-pro', 'gemini-3.6-flash', 'gemini-3.8-flash'):
            self.assertEqual(models[name]['current_pools'], ['antigravity-gemini'])

    def test_catalogue_models_inherit_actual_harness_pools_without_bare_provider(self):
        overview = self.overview()
        models = {m['model']: m for m in overview['models']}
        for name, pool in [('gpt-oss-120b', 'antigravity-3p'),
                           ('gemini-3.5-flash', 'antigravity-gemini')]:
            self.assertEqual(models[name]['pools'], [pool])
            self.assertEqual(models[name]['current_pools'], [])
            rendered = picker_html(overview, pool)
            self.assertIn(f'data-model="{name}"', rendered.split('<details class="older">')[1])
        page = console.render_page(overview, 'fixture')
        self.assertNotIn('No provider plan recorded', page)
        self.assertNotIn('class="pool catalogue"', page)
        self.assertEqual(page.count('class="pool lvl-'), 3)
        for name in models:   # each model appears exactly once on the page, choosable or not
            self.assertEqual(page.count(f'data-model="{name}"'), 1)

    def test_missing_pool_uses_matching_sibling_and_not_other_harness(self):
        roster = roster_fixture()
        pro = next(l for l in roster['lanes'] if l['model_key'] == 'gemini-3.1-pro')
        del pro['quota_pool']
        roster['quota_pools']['gemini-metered'] = {'plan': {'name': 'Gemini API'}}
        roster['lanes'].insert(0, dict(pro, lane_id='paid-pro', quota_pool='gemini-metered',
                                       harness='opencode', provider='google', admission_status='retired'))
        model = next(m for m in self.overview(roster)['models'] if m['model'] == 'gemini-3.1-pro')
        self.assertEqual(set(model['pools']), {'gemini-metered', 'antigravity-gemini'})
        self.assertEqual(model['current_pools'], ['antigravity-gemini'])

    def test_neither_preferences_nor_catalogue_fields_override_admission(self):
        roster = roster_fixture()
        roster['lanes'][0]['admission_status'] = 'retired'
        roster['lanes'][1]['access_status'] = 'unverified'
        roster['catalogue_only'].append(dict(roster['lanes'][2], model_key='catalogue-only'))
        models = {m['model']: m for m in self.overview(roster, {
            'model_preferences': {'gpt-6.1-sol': 'normal', 'gpt-6-luna': 'off'},
        })['models']}
        for name in ('gpt-6.1-sol', 'gpt-6-astra', 'catalogue-only'):
            self.assertEqual(models[name]['current_pools'], [])
        self.assertEqual(models['gpt-6-luna']['current_pools'], ['codex'])

    def test_harness_only_fallback_is_archived_under_existing_plan(self):
        roster = roster_fixture()
        roster['lanes'].append(dict(roster['lanes'][0], lane_id='unplaced', model_key='unknown-model',
                                    quota_pool=None, provider='unknown-provider'))
        model = next(m for m in self.overview(roster)['models'] if m['model'] == 'unknown-model')
        self.assertEqual(model['pools'], ['codex'])
        self.assertEqual(model['current_pools'], [])


if __name__ == '__main__':
    unittest.main()


class DirectPoolModelsAreChoosable(unittest.TestCase):
    """A model with no routing lane still gets its switch: the wrapper passes it to the vendor's CLI.

    These once showed without a control ("the provider's own app picks its model"). A model
    the wrapper can run can be switched on or off, so the page offers exactly that.
    """

    def test_evidence_only_models_render_with_a_switch(self):
        roster = roster_fixture()
        roster['lanes'] = roster['lanes'][3:]
        roster['model_evidence']['gpt-6.1-sol'] = {'status': 'provisional', 'note': 'x'}
        with tempfile.TemporaryDirectory() as directory:
            overview = fleetctl.fleet_overview(roster, {}, Path(directory))
        html_out = picker_html(overview, 'codex')
        self.assertIn('name="switch" value="gpt-6.1-sol=off"', html_out)
        self.assertIn('role="switch" aria-checked="true"', html_out)
        self.assertNotIn('own app picks its model', html_out)
