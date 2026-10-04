"""The console made obvious at a glance.

Requirements: the refresh control is just a refresh icon; the page is obvious, nice and easy to use; Gemini
3.6 and 3.7 Flash sit in the older models section; clicking a model opens a page about it; an older model that
is still better at something says so in its details, when that is true; every limit the plan has is shown,
with its reset; a control at the top right of what agents read copies it. These tests hold each of those on
the page the console renders, and that a click on a switch reaches the files that name a model, as the
command line does.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from tests.test_console import console
from tests import test_limits_and_older as older
from tests.test_limits_and_older import codex_roster, gemini_roster, overview_of

fleetctl = console.fleetctl
FIXTURE_SOURCE = "https://example.com/synthetic-comparison"


def page_of(roster, runtime=None):
    return console.render_page(overview_of(roster, runtime), "token")


def section(page, pool):
    start = page.index(f'id="pool-{pool}"')
    end = page.find("<article", start + 1)
    return page[start:end if end > 0 else len(page)]


def older_fold(block):
    start = block.index('<details class="older">')
    return block[start:]


class LimitsOnThePage(unittest.TestCase):
    def setUp(self):
        # the limits fixture of test_limits_and_older, read without running its tests a second time here
        self.roster, self.runtime = older.LimitTests.roster(None), older.LimitTests.runtime(None)

    def test_every_limit_has_a_row_with_its_reset_in_plain_words(self):
        overview = overview_of(self.roster, self.runtime)
        page = console.render_page(overview, "token")
        for pool in overview["pools"]:
            block = section(page, pool["pool"])
            self.assertEqual(block.count('<li class="lim'), len(pool["limits"]), pool["pool"])
            for limit in pool["limits"]:
                if limit["kind"] == "window" and limit["reset_at"]:
                    self.assertIn(f'resets {limit["resets"]}', block)
        claude = section(page, "claude")
        self.assertIn('<span class="ln">Weekly<small>Fable only</small></span>', claude)   # a per-model limit says what it counts
        self.assertIn('class="lim hot"', claude)                                            # 80% used: nearly spent
        go = section(page, "go")
        self.assertIn("not started yet", go)                                                # a 5-hour window nothing has used yet
        self.assertIn("Plan renews", go)                                                    # a renewal date is a date, not a limit
        self.assertIn("resets at midnight", section(page, "paid"))                         # a paid plan's daily budget

    def test_a_note_in_capitals_is_not_shouted_and_acronyms_stay(self):
        self.assertEqual(console._first_sentence("EXISTS TO KILL A PAID LANE, NOT TO WIN ROUTES: it shares it with GPT and AA"),
                         "Exists to kill a paid lane, not to win routes: it shares it with GPT and AA")

    def test_a_limit_sent_without_a_label_loses_its_id_words(self):
        self.assertEqual(console._scope_words("claude-weekly-scoped-fable", "claude"), "Fable")
        self.assertEqual(console._scope_words("Fable only", "claude"), "Fable only")
        self.assertEqual(console._scope_words("gpt-reserve", "codex"), "GPT Reserve")
        self.assertIsNone(console._scope_words("claude-weekly", "claude"))

    def test_the_refresh_is_an_icon_with_a_name_and_the_old_words_are_gone(self):
        page = page_of(self.roster, self.runtime)
        refresh = page[page.index('<form class="quota-refresh"'):page.index("</form>", page.index('<form class="quota-refresh"'))]
        self.assertIn('aria-label="Read the limits again now"', refresh)
        self.assertIn('<svg class="ico"', refresh)
        self.assertNotIn("Refresh quota", page)
        self.assertIn("Limits read", page)


class ModelFacts(unittest.TestCase):
    # Fictional comparison values test escaping, citation rendering and older-model details.
    # These are not benchmark observations about the named models.
    def roster(self):
        roster = codex_roster()
        cards = roster["model_cards"]
        cards["gpt-6.1-sol"] = {"pool": "codex", "name": "GPT-6.1 Sol", "status": "current", "order": 2,
                                "page_url": "https://artificialanalysis.ai/models/gpt-6-1-sol",
                                "access": "not yet proven on this login (2026-09-30)"}
        cards["gpt-6-sol"] = {"pool": "codex", "name": "GPT-6 Sol", "status": "current", "order": 3}
        cards["gpt-6-sol"].update(page_url="https://artificialanalysis.ai/models/gpt-6-sol", superseded_on="2026-09-29",
                                  better_than_successor_at=[{"capability": "Synthetic fixture measure", "this": "fixture A",
                                                             "successor": "fixture B", "margin": "small", "source": FIXTURE_SOURCE,
                                                             "read": "2026-09-30"}],
                                  cross_check="Synthetic cross-check")
        cards["gpt-6-astra"]["page_url"] = "javascript:alert(1)"   # never a link
        return roster

    def test_a_models_name_opens_a_page_about_it(self):
        codex = section(page_of(self.roster()), "codex")
        self.assertIn('<a class="nm" href="https://artificialanalysis.ai/models/gpt-6-1-sol" target="_blank" '
                      'rel="noopener noreferrer"', codex)
        self.assertNotIn("javascript:", codex)
        self.assertIn('<span class="nm">GPT-6 Astra</span>', codex)                       # no safe page: plain name

    def test_an_older_model_says_what_replaced_it_and_where_it_still_leads(self):
        codex = section(page_of(self.roster()), "codex")
        fold = older_fold(codex)
        self.assertIn('data-model="gpt-6-sol"', fold)                                     # the newer Sol made it older, by rule
        self.assertIn("Replaced by GPT-6.1 Sol on 29 September; still ahead on 1 measure, see Details", fold)
        self.assertIn('<p class="lh">Still better than GPT-6.1 Sol at</p>', fold)
        self.assertIn('<span class="cap">Synthetic fixture measure</span>', fold)
        self.assertIn('<b class="num">fixture A</b> vs <span class="num">fixture B</span> · small lead', fold)
        self.assertIn(f'<a href="{FIXTURE_SOURCE}" target="_blank" rel="noopener noreferrer">example.com</a> (read 30 September)', fold)
        self.assertIn("Checked elsewhere: Synthetic cross-check", fold)
        self.assertIn("<b>Access</b> not yet proven on this login", codex)

    def test_no_facts_means_nothing_shown(self):
        roster = codex_roster()
        codex = section(page_of(roster), "codex")
        self.assertNotIn('class="leads"', codex)
        self.assertNotIn('<a class="nm"', codex)

    def test_superseded_flash_versions_sit_under_older_with_no_card_saying_so(self):
        block = section(page_of(gemini_roster()), "gem")
        fold = older_fold(block)
        current = block[:block.index('<details class="older">')]
        for key in ("gemini-3.6-flash", "gemini-3.7-flash"):
            self.assertIn(f'data-model="{key}"', fold)
            self.assertNotIn(f'data-model="{key}"', current)
        self.assertIn('data-model="gemini-3.8-flash"', current)
        self.assertIn('data-model="gemini-3.1-pro"', current)                            # another line stays current
        self.assertIn("Replaced by Gemini 3.8 Flash", fold)

    def test_a_withdrawn_model_has_no_switch(self):
        roster = codex_roster()
        roster["model_cards"]["gpt-5.3-codex-spark"]["retired_on"] = "2026-09-14"
        codex = section(page_of(roster), "codex")
        spark = codex[codex.index('data-model="gpt-5.3-codex-spark"'):]
        spark = spark[:spark.index("</li>")]
        self.assertIn('<span class="na">Withdrawn</span>', spark)
        self.assertNotIn('class="sw"', spark)
        self.assertIn("Withdrawn by its maker on 14 September", spark)


class CopyAndSlider(unittest.TestCase):
    def test_what_agents_read_has_a_copy_control_at_its_top_right(self):
        page = page_of(codex_roster())
        agents = page[page.index('<section class="agents"'):]
        self.assertLess(agents.index('<button class="copy" type="button" aria-label="Copy what agents read">'),
                         agents.index('<pre id="agent-brief">'))                              # before the text: floated top right
        self.assertIn('<span class="cw">Copy</span>', agents)

    def test_the_slider_is_a_labelled_radio_group_in_plain_words(self):
        codex = section(page_of(codex_roster()), "codex")
        self.assertIn('role="radiogroup" aria-label="How much of Codex to use"', codex)
        self.assertEqual(codex.count('role="radio"'), 5)
        self.assertIn(console.LEVEL_PLAIN["normal"], codex)
        self.assertNotIn("routing follows the quota gauges", codex)                        # the agents' words stay in the brief
        self.assertNotIn(" style=", codex)


class ClicksReachEveryPath(unittest.TestCase):
    """A click on the page does what the command line does, pins included."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.seat = self.dir / "builder.toml"
        self.seat.write_text('name = "builder"\nmodel = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n', encoding="utf-8")
        self.watched = self.dir / "config.toml"
        self.watched.write_text('model = "gpt-6-astra"\n[profiles.x]\nmodel = "gpt-6.1-sol"\n', encoding="utf-8")
        roster = codex_roster()
        roster["quota_pools"]["codex"]["model_pins"] = [
            {"path": str(self.seat), "key": "model", "format": "toml", "manage": True, "what": "Codex agent seats"},
            {"path": str(self.watched), "key": "model", "format": "toml", "manage": False, "what": "the Codex app's default"}]
        self.roster = roster
        self.overlay = self.dir / "overlay.json"
        self.overlay.write_text(json.dumps(roster), encoding="utf-8")
        self.state = self.dir / "state"
        self.state.mkdir()
        self.app = console.Console(self.overlay, self.state, 0)

    def page(self):
        return console.render_page(self.app.overview(refresh=False), "token")

    def test_switching_a_model_off_on_the_page_moves_the_seats_that_name_it_and_back(self):
        self.app.set_toggle("codex", "gpt-6-astra", False)
        self.assertIn('model = "gpt-6.1-sol"', self.seat.read_text())                        # the seat now names what runs
        self.assertEqual(self.watched.read_text().splitlines()[0], 'model = "gpt-6-astra"')   # an app's own file is never written
        codex = section(self.page(), "codex")
        self.assertIn("builder.toml now runs GPT-6.1 Sol while GPT-6 Astra is off", codex)
        self.assertIn("still names GPT-6 Astra, which is switched off. Crossfeed does not edit this file", codex)
        self.app.set_toggle("codex", "gpt-6-astra", True)
        self.assertIn('model = "gpt-6-astra"', self.seat.read_text())                      # its own model back
        self.assertNotIn('class="pins"', section(self.page(), "codex"))

    def test_a_seat_moved_off_a_withdrawn_model_says_so_and_promises_nothing_back(self):
        self.seat.write_text('model = "gpt-5.3-codex-spark"\n', encoding="utf-8")
        self.roster["model_cards"]["gpt-5.3-codex-spark"]["retired_on"] = "2026-09-14"
        self.overlay.write_text(json.dumps(self.roster), encoding="utf-8")
        self.app.set_toggle("codex", "gpt-6-luna", True)                                  # any click syncs the pins
        self.assertNotIn("gpt-5.3-codex-spark", self.seat.read_text())
        codex = section(self.page(), "codex")
        self.assertIn("was withdrawn by its maker.", codex)
        self.assertNotIn("Spark is off, and gets it back", codex)

    def test_switch_all_on_is_the_current_list_and_leaves_an_older_model_as_set(self):
        self.app.set_toggle("codex", "gpt-6.1-sol", False)
        self.app.set_toggle("codex", "gpt-5.6-luna", False)
        self.app.set_choice("codex", "auto")
        runtime = fleetctl.load_json(self.state / "runtime.json", {})
        switches = fleetctl.pool_switches(self.roster, runtime, "codex")
        self.assertTrue(all(switches[key] for key in fleetctl.current_models(self.roster, "codex")))
        self.assertFalse(switches["gpt-5.6-luna"])


if __name__ == "__main__":
    unittest.main()
