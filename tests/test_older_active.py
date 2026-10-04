"""Fictional successor leads test retention and visible older-model switches."""
import copy
import unittest

from tests.test_console import fleetctl
from tests.test_console5 import older_fold, page_of, section
from tests.test_limits_and_older import gemini_roster


SOURCE = "https://example.com/fixtures/model-comparison"


def recorded_flash_roster():
    """Seven fictional rows exercise comparison rendering, never provider observations."""
    roster = gemini_roster()
    measures = [(f"Synthetic measure {n}", f"fixture {n} A", f"fixture {n} B",
                 "small" if n % 2 else "clear") for n in range(1, 8)]
    roster["model_cards"]["gemini-3.7-flash"] = {
        "name": "Gemini 3.7 Flash", "superseded_by": "gemini-3.8-flash",
        "better_than_successor_at": [
            dict(capability=capability, this=this, successor=successor, margin=margin,
                 source=SOURCE, read="2026-09-30")
            for capability, this, successor, margin in measures],
    }
    return roster


class RecordedOlderLeads(unittest.TestCase):
    def test_each_legacy_console_comparison_is_a_valid_retention_reason(self):
        roster = recorded_flash_roster()
        card = roster["model_cards"]["gemini-3.7-flash"]
        for comparison in list(card["better_than_successor_at"]):
            with self.subTest(capability=comparison["capability"]):
                card["better_than_successor_at"] = [comparison]
                reason = fleetctl.older_model_reason(roster, "gem", "gemini-3.7-flash")
                self.assertIn(comparison["capability"], reason)
                self.assertIn(SOURCE, reason)
                self.assertTrue(fleetctl.pool_switches(roster, {}, "gem")["gemini-3.7-flash"])

    def test_malformed_and_unsubstantiated_comparisons_do_not_qualify(self):
        roster = recorded_flash_roster()
        comparison = roster["model_cards"]["gemini-3.7-flash"]["better_than_successor_at"][0]
        for field, bad in [("source", ""), ("source", {}), ("source", []),
                           ("capability", []), ("this", {}), ("this", True),
                           ("successor", []), ("this", float("nan")),
                           ("this", float("inf")), ("margin", "tie")]:
            with self.subTest(field=field, value=bad):
                broken = dict(comparison, **{field: bad})
                roster["model_cards"]["gemini-3.7-flash"]["better_than_successor_at"] = [broken]
                self.assertIsNone(fleetctl.older_model_reason(roster, "gem", "gemini-3.7-flash"))
        for shape in (42, "unsupported", {"source": SOURCE}):
            roster["model_cards"]["gemini-3.7-flash"]["better_than_successor_at"] = shape
            self.assertIsNone(fleetctl.older_model_reason(roster, "gem", "gemini-3.7-flash"))

    def test_finite_numeric_comparisons_remain_supported(self):
        roster = recorded_flash_roster()
        card = roster["model_cards"]["gemini-3.7-flash"]
        card["better_than_successor_at"] = [dict(card["better_than_successor_at"][0], this=8, successor=6.0)]
        self.assertIn("8 vs 6.0", fleetctl.older_model_reason(roster, "gem", "gemini-3.7-flash"))

    def test_recorded_lead_over_an_older_comparator_is_not_a_current_lead(self):
        roster = recorded_flash_roster()
        roster["model_cards"]["gemini-3.6-flash"] = {
            "superseded_by": "gemini-3.7-flash",
            "better_than_successor_at": copy.deepcopy(
                roster["model_cards"]["gemini-3.7-flash"]["better_than_successor_at"]),
        }
        self.assertIsNone(fleetctl.older_model_reason(roster, "gem", "gemini-3.6-flash"))
        self.assertFalse(fleetctl.pool_switches(roster, {}, "gem")["gemini-3.6-flash"])

    def test_older_on_is_visibly_active_and_off_preserves_its_visible_copy(self):
        roster = recorded_flash_roster()
        on_fold = older_fold(section(page_of(roster), "gem"))
        self.assertIn("still ahead on 7 measures, see Details", on_fold)
        self.assertIn('<span class="older-active">active</span>', on_fold)
        self.assertIn('<span class="older-active-note">older, still used where it leads</span>', on_fold)
        runtime = {"model_toggles": {"gem": ["gemini-3.7-flash"]}}
        off_fold = older_fold(section(page_of(roster, runtime), "gem"))
        self.assertIn("still ahead on 7 measures, see Details", off_fold)
        self.assertIn('<span class="older-active" hidden>active</span>', off_fold)
        self.assertIn('<span class="older-active-note" hidden>older, still used where it leads</span>', off_fold)
        self.assertNotIn('<span class="older-active">', off_fold)
        self.assertFalse(fleetctl.pool_switches(roster, runtime, "gem")["gemini-3.7-flash"])
        # Older rows without a qualifying reason retain their existing disabled OFF control.
        blocked = off_fold[off_fold.index('data-model="gemini-3.6-flash"'):].split("</li>", 1)[0]
        self.assertNotIn('class="older-active', blocked)
        self.assertIn('disabled name="switch"', blocked)

    def test_active_indicator_and_note_remain_visible_in_collapsed_older_summary(self):
        roster = recorded_flash_roster()
        fold = older_fold(section(page_of(roster), "gem"))
        self.assertTrue(fold.startswith('<details class="older"><summary>'))  # collapsed initially
        summary = fold.split('</summary>', 1)[0]
        self.assertIn('<span class="older-active older-active-summary">active</span>', summary)
        self.assertIn('<span class="older-active-note older-active-summary-note">'
                      'older, still used where it leads</span>', summary)
        runtime = {"model_toggles": {"gem": ["gemini-3.7-flash"]}}
        off_summary = older_fold(section(page_of(roster, runtime), "gem")).split('</summary>', 1)[0]
        self.assertIn('<span class="older-active older-active-summary" hidden>active</span>', off_summary)
        self.assertIn('<span class="older-active-note older-active-summary-note" hidden>', off_summary)
        self.assertNotIn('<span class="older-active older-active-summary">', off_summary)


if __name__ == "__main__":
    unittest.main()
