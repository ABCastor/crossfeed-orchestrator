"""Invented data only: job boundaries, live admission, and progressive disclosure."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from html.parser import HTMLParser

from scripts import benchmark_page as page, model_cards, fleetctl
from tests.test_console import console


def row(model='fixture-a', family='coding-agent', score=.8, own=True, pool='codex', **kw):
    data = dict(model=model, level='high', quality={family: dict(mean=score, sd=.05)},
        own={family: dict(passes=8, trials=10)} if own else {},
        outside=[] if own else [dict(metric='fixture_ranking', families=[family], value=.8, weight=1)],
        sources=[dict(pool=pool, harness=pool, price_1m={'out': 1}, cost={'percent': .02}, availability={})],
        flags=['latency_completion_measured'], latency_s=20, own_provenance={}, owner_prior=[])
    data.update(kw)
    return data


def picks(rows, job):
    return next(rs for key, _, _, rs in page.select_jobs(rows) if key == job)


class VisibleText(HTMLParser):
    """Read the default main view, excluding collapsed details and scripts."""
    def __init__(self):
        super().__init__(); self.closed = 0; self.summary = 0; self.script = 0; self.text = []
    def handle_starttag(self, tag, attrs):
        if tag == 'details': self.closed += 1
        if tag == 'summary': self.summary += 1
        if tag == 'script': self.script += 1
    def handle_endtag(self, tag):
        if tag == 'details': self.closed -= 1
        if tag == 'summary': self.summary -= 1
        if tag == 'script': self.script -= 1
    def handle_data(self, data):
        if not self.script and (not self.closed or self.summary == self.closed): self.text.append(data)


class JobTests(unittest.TestCase):
    def test_coding_does_not_become_design_evidence(self):
        self.assertEqual(picks([row()], 'design'), [])
        r = row(family='visual', own=False)
        r['own'] = {'coding-agent': {'passes': 64, 'trials': 64}}
        self.assertEqual(page.confidence(r, ('visual',))[0], 'outside')
        self.assertNotIn('64', page.why(r, ('visual',)))
        self.assertIn('No test of ours yet for this job', page.why(r, ('visual',)))

    def test_real_writing_family_prior_is_labelled_and_usable(self):
        prior = row('new-model', quality={}, own={}, outside=[], owner_prior=['writing'], pool='antigravity-gemini')
        measured = row(family='writing')
        result = picks([prior, measured], 'writing')
        self.assertEqual([r['model'] for r in result], ['fixture-a', 'new-model'])
        self.assertEqual(page.confidence(prior, ('writing',))[0], 'guess')
        self.assertIn('owner’s prior', page.why(prior, ('writing',)))

    def test_family_local_results_precede_stronger_outside_estimate(self):
        local = row(score=.3)
        foreign = row('foreign', own=False, score=.99)
        self.assertEqual(picks([foreign, local], 'coding')[0]['model'], 'fixture-a')

    def test_exhaustion_promotes_working_runner_up(self):
        blocked = row(score=.95)
        blocked['sources'][0]['availability'] = {'blocked': {'until': '2026-10-23T00:00:00Z', 'reset_known': True}}
        backup = row('backup', score=.7)
        self.assertEqual([r['model'] for r in picks([blocked, backup], 'coding')], ['backup'])
        self.assertIn('Not available until 2026-10-23', page.render_body({'rows': [blocked, backup]}))

    def test_alternate_source_preserves_availability_and_live_cost(self):
        r = row()
        r['sources'][0]['availability'] = {'blocked': {'until': '2099-01-01', 'reset_known': True}}
        r['sources'].append(dict(pool='opencode-go', cost={'percent': .003}, availability={}))
        self.assertEqual(len(picks([r], 'coding')), 1)
        self.assertIn('OpenCode Go', page.cost_feel(r))
        self.assertNotIn('Codex', page.cost_feel(r))

    def test_switched_off_route_cannot_recommend(self):
        r = row(); r['sources'][0]['availability'] = {'switched_off': True}
        self.assertEqual(picks([r], 'coding'), [])
        html = page.render_body({'rows': [r]})
        self.assertNotIn('fixture-a', html.split('class="unmeasured-box"')[0])

    def test_distinct_models_and_cross_pool_backup(self):
        a, b, c = row(score=.9), row('same-pool', score=.8), row('backup', score=.78, pool='claude')
        self.assertEqual([r['model'] for r in picks([a, dict(a, level='low'), b, c], 'coding')], ['fixture-a', 'backup'])

    def test_review_prefers_distinct_lineage(self):
        rows = [row('claude-a', 'review', .9), row('claude-b', 'review', .85), row('gpt-a', 'review', .7)]
        self.assertEqual([r['model'] for r in picks(rows, 'review')], ['claude-a', 'gpt-a'])

    def test_research_speed_and_slow_near_equivalent_setting(self):
        a = row(family='research', latency_s=200)
        self.assertEqual(picks([a], 'research'), [])
        slow = row(level='max', latency_s=220, score=.82)
        quick = row(level='high', latency_s=50, score=.8)
        self.assertEqual(picks([slow, quick], 'coding')[0]['level'], 'high')

    def test_fast_measured_speed_precedes_placeholder(self):
        measured = row(latency_s=30)
        estimated = row('estimated', latency_s=1, flags=['latency_estimated'])
        self.assertEqual([r['model'] for r in picks([estimated, measured], 'fast')], ['fixture-a'])

    def test_unmeasured_collapsed_all_settings_preserved_and_main_plain(self):
        r = row(level='xhigh')
        unknown = row('unknown-model', quality={}, own={}, outside=[], flags=['heuristic_priors'])
        cards = {'rows': [r, dict(r, level='max'), unknown], 'read_on': '2026-10-16'}
        rendered = console.render_benchmarks(cards)
        parser = VisibleText(); parser.feed(rendered)
        visible = ' '.join(parser.text)
        for raw in ['coding-agent', 'xhigh', 'quota_price', 'family_strengths', 'heuristic_priors', 'provider-default']:
            self.assertNotIn(raw, visible)
        self.assertNotIn('Unknown Model', visible)
        self.assertIn('Not measured yet', visible)
        self.assertNotIn('class="unmeasured-box" open', rendered)
        self.assertEqual(rendered.count('class="model-disclosure"'), 2)
        self.assertIn('Extra-deep thinking', rendered)
        self.assertIn('Maximum thinking', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertIn('/static/console.js', rendered)

    def test_model_summary_cost_aggregates_sources_across_settings(self):
        public = row('ranking-model', own=False, sources=[], level='unspecified:ranking-model')
        active = row('ranking-model', quality={}, own={}, outside=[])
        html = page.render_body({'rows': [public, active]})
        summary = html.split('class="model-line">')[1].split('</summary>')[0]
        self.assertIn('ChatGPT / Codex', summary)
        self.assertNotIn('No active allowance', summary)
        active['sources'][0]['availability'] = {'blocked': {'until': '2026-10-23T00:00:00Z', 'reset_known': True}}
        html = page.render_body({'rows': [public, active]})
        summary = html.split('class="model-line">')[1].split('</summary>')[0]
        self.assertIn('Not available until', summary)
        self.assertNotIn('Switched off', summary)

    def test_details_preserve_other_settings_evidence(self):
        a, b = row(), row(level='low', own=False)
        a['own_provenance'] = {'coding-agent': [dict(source='fixture-local', n=10, date='2026-10-02')]}
        html = page.render_body({'rows': [a, b]})
        for text in ['fixture-local', '2026-10-02', 'Fixture ranking', 'weight 1']:
            self.assertIn(text, html)


class AvailabilityTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)
    def test_pool_exhaustion_and_switches_are_read_live(self):
        roster = {'quota_pools': {'codex': {}}, 'model_cards': {'fixture-a': {'pool': 'codex'}}}
        runtime = {'pool_circuits': {'codex': {'until': '2026-10-17T00:00:00Z'}}}
        check = lambda: model_cards._availability(roster, runtime, fleetctl, 'codex', 'fixture-a', 'fixture-a', {}, self.NOW)
        self.assertIsNotNone(check()['blocked'])
        runtime['switches'] = {'codex': 'off'}
        self.assertTrue(check()['switched_off'])
        runtime['switches'] = {}; runtime['pool_circuits'] = {}
        self.assertIsNone(check()['blocked'])

    def test_rejected_unverified_and_retired_routes_cannot_recommend(self):
        roster = {'quota_pools': {'fixture-pool': {}},
            'effort': {'rejected-fixture': {'levels': {'opencode': ['high']}}},
            'lanes': [dict(model_key='rejected-fixture', harness='opencode', quota_pool='fixture-pool',
                admission_status='rejected', access_status='verified')]}
        evidence = {'rows': [dict(model='rejected-fixture', model_key='rejected-fixture', level='high',
            q={'coding-agent': dict(mean=.9, sd=.05, unknown=False)},
            own={'coding-agent': dict(passes=9, trials=10)})]}
        for status, access in [('rejected', 'verified'), ('active', 'unverified')]:
            roster['lanes'][0].update(admission_status=status, access_status=access)
            cards = model_cards.build_cards(roster, {}, evidence, fleet=fleetctl)
            self.assertEqual(picks(cards['rows'], 'coding'), [])
        roster['lanes'] = []
        roster['quota_pools'] = {'codex': {}}
        roster['effort']['rejected-fixture']['levels'] = {'codex': ['high']}
        roster['model_cards'] = {'rejected-fixture': {'pool': 'codex', 'retired_on': '2020-01-01'}}
        cards = model_cards.build_cards(roster, {}, evidence, fleet=fleetctl)
        self.assertEqual(picks(cards['rows'], 'coding'), [])

    def test_refusal_reset_success_recovery_and_stale_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runs.jsonl'
            refusal = dict(model='fixture-a', harness='agy', quota_pool='antigravity-gemini',
                ended_at='2026-10-15T23:50:00Z', status='error', returncode=3)
            with patch.object(model_cards, 'agy_quota_reset', return_value=self.NOW + dt.timedelta(hours=164)):
                path.write_text(json.dumps(refusal) + '\n')
                result = model_cards.quota_refusals(directory, {}, now=self.NOW)
                self.assertTrue(result[('antigravity-gemini', 'fixture-a')]['reset_known'])
                # Other failures cannot clear the refusal; a subsequent successful run can.
                failure = dict(refusal, ended_at='2026-10-16T00:00:01Z', returncode=1)
                path.write_text(json.dumps(refusal) + '\n' + json.dumps(failure) + '\n')
                self.assertTrue(model_cards.quota_refusals(directory, {}, now=self.NOW))
                success = dict(failure, status='ok', returncode=0)
                path.write_text(json.dumps(refusal) + '\n' + json.dumps(success) + '\n')
                self.assertFalse(model_cards.quota_refusals(directory, {}, now=self.NOW))
            path.write_text(json.dumps(refusal) + '\n')
            with patch.object(model_cards, 'agy_quota_reset', return_value=self.NOW - dt.timedelta(seconds=1)):
                self.assertFalse(model_cards.quota_refusals(directory, {}, now=self.NOW))

    def test_agy_log_duration_is_not_replaced_by_half_hour_default(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            stamp = self.NOW.astimezone().strftime('E%m%d %H:%M:%S')
            (home / 'cli.log').write_text(stamp + '.000 12 Individual quota reached. Resets in 164h32m20s\n')
            with patch.object(model_cards, 'AGY_LOG_HOME', home):
                reset = model_cards.agy_quota_reset('2026-10-16T00:00:00Z')
            self.assertEqual(reset, self.NOW + dt.timedelta(hours=164, minutes=32, seconds=20))

    def test_busy_logs_and_ledger_cannot_hide_unexpired_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'log').mkdir()
            stamp = self.NOW.astimezone().strftime('E%m%d %H:%M:%S')
            (home / 'log/old.log').write_text(stamp + '.000 12 Individual quota reached. Resets in 164h32m20s\n')
            for n in range(12):
                (home / f'log/new-{n}.log').write_text('No quota event\n')
            refusal = dict(model='fixture-a', harness='agy', quota_pool='antigravity-gemini',
                ended_at='2026-10-16T00:00:00Z', status='error', returncode=3,
                error={'retry_after_seconds': 1800})
            (home / 'runs.jsonl').write_text(json.dumps(refusal) + '\n' + ('{"padding":"' + 'x' * 10000 + '"}\n') * 120)
            with patch.object(model_cards, 'AGY_LOG_HOME', home):
                result = model_cards.quota_refusals(home, {}, now=self.NOW + dt.timedelta(hours=2))
            self.assertIn(('antigravity-gemini', 'fixture-a'), result)
            self.assertEqual(result[('antigravity-gemini', 'fixture-a')]['until'], '2026-10-22T20:32:20Z')

    def test_provisional_prior_comes_from_roster_not_model_name(self):
        roster = {'lanes': [dict(model_key='new-fixture', evidence_confidence='provisional',
            admission_status='active', roles=['design', 'writing'])]}
        self.assertEqual(model_cards._owner_prior(roster, 'new-fixture', set()), ['visual', 'writing'])
        self.assertEqual(model_cards._owner_prior(roster, 'new-fixture', {'visual'}), ['writing'])
