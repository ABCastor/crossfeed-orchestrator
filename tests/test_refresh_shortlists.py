"""Owner shortlist, archive tombstones and verified-free tariff drift."""
import copy
import unittest

from tests import test_business_model_refresh as fixture


daily = fixture.daily


class RefreshShortlists(unittest.TestCase):
    def setUp(self):
        # Use the existing active promo fixture, but give it an explicit funding claim.
        base = fixture.RefreshTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.roster, self.lane = base._active_free()
        self.lane['funding'] = 'verified-free'
        self.rows = copy.deepcopy(fixture.FIXTURE['after'])
        self.model = next(r for r in self.rows if r['selector'] == self.lane['selector'])

    def reconcile(self):
        return daily.reconcile(self.roster, [fixture.catalog(self.rows)], fixture.market(), today='2026-10-21')

    def test_pinned_rank_is_preserved_when_role_metadata_was_stripped(self):
        lane_id = self.lane['lane_id']
        self.roster.setdefault('policy', {})['model_refresh'] = {'keep_roles': [lane_id]}
        self.lane['roles'] = []
        ranks = self.roster['routing']['roles']['default']['quality_first']
        ranks.append(lane_id)
        # Disqualify for cheap role ranking based on missing quality, not by
        # changing the explicit zero tariff (a safety retirement is different).
        result, changes = daily.reconcile(self.roster, [fixture.catalog(self.rows)],
                                          {'artificial_analysis': {'new-helper': {'intelligence_index': 50},
                                                                  self.lane['model_key'].removesuffix('-free'):
                                                                      {'intelligence_index': 10}}},
                                          today='2026-10-21')
        self.assertIn(lane_id, result['routing']['roles']['default']['quality_first'])
        self.assertTrue(any(s.startswith('default ') for s in changes['held']))

    def test_owner_archived_id_is_not_reopened_by_catalog(self):
        self.lane.update(archived_by_owner='2026-10-20', admission_status='retired', roles=[], retired_by_refresh='2026-10-19')
        result, changes = self.reconcile()
        lane = next(x for x in result['lanes'] if x['lane_id'] == self.lane['lane_id'])
        self.assertEqual(lane['admission_status'], 'retired')
        self.assertNotIn(self.lane['lane_id'], changes['added'])

    def test_explicit_free_admission_is_withdrawn_on_known_nonzero_price(self):
        self.model['price_1m'] = {'input': 0, 'output': .01}
        result, changes = self.reconcile()
        lane = next(x for x in result['lanes'] if x['lane_id'] == self.lane['lane_id'])
        self.assertEqual((lane['admission_status'], lane['roles']), ('retired', []))
        self.assertIn(lane['lane_id'], changes['retired'])
        self.roster = result
        self.model['price_1m'] = {'input': 0, 'output': 0}
        result, _ = self.reconcile()
        self.assertEqual(next(x['admission_status'] for x in result['lanes']
                              if x['lane_id'] == self.lane['lane_id']), 'retired')
