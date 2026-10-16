"""The family header: the console wears the same header as the other products in the family ("v2.8 header").

A header that shrank over one range while a second name faded in over another read as scattered, a held name that
shrank onto the line read as sitting too high, and a sticky header left the screen when the page bounced past its
end. So: the header is fixed to the screen; every part rides up with the page on one range, 1:1, linear, then holds
with the name at its own size; the front page's name starts as large as its row allows and lands on the line; on a
phone the lens and the toggle stand beside the name on its baseline; the tab you are on is marked in the studio's
olive, the owner's selected knot and person's line. Motion was recorded side by side with a sibling product; these checks hold
the rules and the arithmetic that produce it.

The owner's 16 October request changed four things these checks encoded: the nav has two tabs, Providers and
Benchmarks; the nav and the name never touch; the line is terracotta then teal in equal halves with the olive dot
centred; the tab icon has no plate. A check that one of those contradicted is adapted where it stands, and says why.
"""

import math
import re
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from tests.test_console import base_overlay, console

fleetctl = console.fleetctl
CSS = (console.ASSETS / "console.css").read_text(encoding="utf-8")
JS = (console.ASSETS / "console.js").read_text(encoding="utf-8")
PY = (console.ASSETS.parent / "console.py").read_text(encoding="utf-8")
V28 = CSS[CSS.index("/* v2.8 header */"):]

# The Benchmarks tab in bold at 14px, the fallback the CSS uses before console.js measures it (Providers is 63.2px in
# the same measure). The nav's gap is 17.6px.
BENCHMARKS_BOLD = 80
TABS_BESIDE = 63.2 + 17.6 + BENCHMARKS_BOLD   # Providers, the gap, Benchmarks: the nav as it sits beside the name


def block(source, at):
    """A block's text from `at` to the `}` matching the first `{` after it."""
    depth = 0
    for i in range(source.index("{", at), len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            return source[at:i + 1]
    raise AssertionError("unclosed block")


HELD = block(V28, V28.index("@supports (animation-timeline:scroll()){"))


def rule(source, selector):
    """The declarations of the first rule whose selector is exactly `selector`."""
    match = re.search(r"(^|[\s{}])" + re.escape(selector) + r"\{", source)
    assert match, f"no rule for {selector}"
    body = block(source, match.start(0) + len(match.group(1)))
    inner = re.sub(r"/\*.*?\*/", "", body[body.index("{") + 1:-1], flags=re.S)
    pairs = (d.split(":", 1) for d in re.split(r";(?![^(]*\))", inner) if d.strip())
    return {key.strip(): value.strip() for key, value in pairs}


def num(expr, env):
    """A number out of calc() arithmetic, with var() values substituted and min/max/tan/atan2 evaluated."""
    text = expr
    for _ in range(8):
        text = re.sub(r"var\((--[\w-]+)\)", lambda m: f"({env[m.group(1)]})", text)
    text = text.replace("calc(", "(")
    text = re.sub(r"(\d*\.?\d+)vw", lambda m: f"({m.group(1)}*{env['vw']}/100)", text)
    text = re.sub(r"(\d*\.?\d+)rem", r"(\1*16)", text)
    text = re.sub(r"(\d*\.?\d+)px", r"\1", text)
    assert re.fullmatch(r"[\d\s.+\-*/(),a-z0-9]+", text), text
    return eval(text, {"__builtins__": {}}, {"min": min, "max": max, "tan": math.tan, "atan2": math.atan2})


ROOT = rule(V28, ":root")
PHONE_NM = re.search(r"@media \(max-width:560px\)\{:root\{--nm:([^}]+)\}\}", V28).group(1)


def tokens(width, front, scrollbar=0):
    """What a screen `width` px wide computes, on the front page or not, with or without an always-on scrollbar."""
    env = {"vw": width, "--line-w": 2, "--logo": num(ROOT["--logo"], {"vw": width}), "--sb": scrollbar}
    env["--nm"] = num(PHONE_NM, env) if width <= 560 else 30
    for key in ("--cap", "--air", "--air-held", "--h0", "--hE"):
        env[key] = num(ROOT[key], env)
    if front:
        front_rule = rule(HELD, ":root.front")
        room = front_rule["--hroom"]
        for bound in (1000, 760, 680):   # narrower screens: the tabs leave the row, then the gutter, then the toggle's room
            if width <= bound:
                room = re.search(rf"@media \(max-width:{bound}px\)\{{:root\.front\{{--hroom:([^}}]+)\}}\}}", HELD).group(1)
        env["--hroom"] = num(room, env)
        for key in ("--hbw", "--h0", "--hE"):
            env[key] = num(front_rule[key], env)
    for key in ("--travel", "--mast-h", "--mast-stuck"):
        env[key] = num(ROOT[key], env)
    return env


def brand_width(env):
    """The brand's width at scale 1: the mark (its height times the drawing's 48.28:25.5), the gap, the name."""
    nm = env["--nm"]
    return (10 + .73 * nm) * 1.0119 * env["--logo"] * 48.28 / 25.5 + .6 * nm + 10.3656 * nm


def render(front=True):
    with tempfile.TemporaryDirectory() as directory:
        overview = fleetctl.fleet_overview(base_overlay(), {}, Path(directory))
    return console.render_page(overview, "tok") if front else console.message_page("Signed out", "Open the link.")


class FamilyHeaderTests(unittest.TestCase):
    def test_the_selected_knot_ships_without_the_try_looks_scaffolding(self):
        self.assertTrue(render().startswith('<!doctype html><html lang="en" class="front">'))
        self.assertTrue(render(front=False).startswith('<!doctype html><html lang="en">'))
        for gone in ("LOOK_TAB", "LOOK_LINE", "LOOK_HERO", "LOOK_LOGO", "LOOK_NAME"):
            self.assertNotIn(gone, PY)
        for gone in ("data-look-tab", "data-look-line", "data-look-hero", "data-look-logo", "data-look-name", "--hero", "--name-em"):
            self.assertNotIn(gone, CSS)
        self.assertIn('<i class="here" aria-hidden="true"></i>', render())
        self.assertIn('<span class="tl" data-t="Providers">Providers</span>', render())

    def test_the_olive_knot_is_centred_on_the_persons_line(self):
        # Adapted (16 Oct): the olive dot sits where the two halves of the line meet, not on the selected tab, and
        # terracotta no longer runs from the gutter to a tab, so the halves are one equal gradient.
        self.assertIn("--between:#667D2F", CSS)
        knot = rule(V28, ".mast .here")
        self.assertEqual(knot["background"], "var(--between)")
        self.assertEqual((knot["width"], knot["height"], knot["border-radius"]), ("8px", "8px", "50%"))
        self.assertEqual(knot["box-shadow"], "0 0 0 2px var(--paper)")
        self.assertEqual((knot["left"], knot["top"]), ("calc(50% - 4px)", "calc(100% + var(--line-w) / 2 - 4px)"))
        self.assertNotIn(".nav a[aria-current]::after", CSS)
        self.assertNotIn("anchor-name", CSS)
        self.assertNotIn(".mast .here{display:none}", V28)     # the dot stands on a phone too
        self.assertEqual((ROOT["--line-l"], ROOT["--line-r"]), ("var(--human)", "var(--machine)"))

    def test_tab_words_reserve_their_bold_width_and_read_in_full_ink(self):
        self.assertIn("--tab:light-dark(#3B3731,#C2BDB4)", CSS)
        self.assertEqual(rule(CSS, ".nav")["font-size"], "14px")
        self.assertEqual(rule(CSS, ".nav")["font-weight"], "400")
        self.assertEqual(rule(CSS, ".nav a")["color"], "var(--tab)")
        self.assertEqual(rule(CSS, ".nav a[aria-current]")["color"], "var(--ink)")
        self.assertEqual(rule(CSS, ".nav a[aria-current] .tl")["font-weight"], "700")
        copy = rule(CSS, ".nav .tl::after")
        self.assertEqual((copy["content"], copy["visibility"], copy["height"], copy["font-weight"]),
                         ('attr(data-t) / ""', "hidden", "0", "700"))

    # EXPECTED TO CHANGE (16 Oct): "olive dot centred" puts one dot where the halves meet and nothing travels, so no
    # knot is left to stretch between tabs or to share a clock with the line. Kept as written; the lead decides.
    @unittest.expectedFailure
    def test_the_knot_and_line_share_one_clock_and_reduced_motion_has_no_transition(self):
        motion = block(V28, V28.index("@media (prefers-reduced-motion:no-preference)"))
        self.assertIn("@view-transition{navigation:auto}", motion)
        self.assertIn("--t-move:450ms", CSS)
        self.assertIn("::view-transition-group(mast-current),::view-transition-group(mast-here){"
                      "animation-duration:var(--t-move);animation-timing-function:var(--ease)}", motion)
        self.assertEqual(rule(motion, "::view-transition-image-pair(mast-current)")["animation"],
                         "tab-stretch var(--t-move) linear both")
        frames = re.findall(r"(\d+)%\{scale:([\d.]+) ([\d.]+)", block(V28, V28.index("@keyframes tab-stretch")))
        self.assertIn(("48", "4", ".62"), frames)
        self.assertEqual(frames[-1], ("100", "1", "1"))
        self.assertEqual(rule(motion, "html.knot-arrives::view-transition-image-pair(mast-current)")["animation"], "none")

    def test_the_header_is_fixed_so_a_bounce_past_the_end_never_takes_it_away(self):
        mast = rule(HELD, ".mast")
        self.assertEqual((mast["position"], mast["top"], mast["left"], mast["right"]), ("fixed", "0", "0", "0"))
        self.assertEqual(mast["height"], "var(--mast-h)")
        self.assertEqual(mast["align-content"], "flex-end")          # an untrimmed name overflows up, never under the line
        self.assertEqual(rule(HELD, "main")["padding-top"], "var(--mast-h)")   # the page keeps the header's room
        # Its paper runs edge to edge; the line and its contents keep to the column, exactly the page's.
        self.assertEqual((mast["padding-left"], mast["padding-right"]), ("var(--gut)", "var(--gut)"))
        self.assertEqual(ROOT["--gut"], "max(1.25rem,calc((100% - 59.5rem) / 2))")
        self.assertIn(".wrap{max-width:62rem;margin:0 auto;padding:0 1.25rem 4rem}", CSS)   # 62rem less its padding
        self.assertIn("@media (max-width:760px){:root{--gut:1rem}}", V28)
        self.assertIn(".wrap{padding:0 1rem 3.5rem}", CSS)                                  # the phone's column
        self.assertEqual(rule(HELD, ".mast::after")["background-size"], "calc(100% - 2 * var(--gut)) var(--line-w),auto")
        paper = rule(V28, ".mast::after")
        self.assertEqual(paper["inset"], "0 0 calc(-1 * var(--line-w))")
        self.assertIn("linear-gradient(90deg,var(--line-l) 0 50%,var(--line-r) 50%)", paper["background"])
        self.assertEqual((ROOT["--line-l"], ROOT["--line-r"]), ("var(--human)", "var(--machine)"))
        # The box itself never moves; nothing sticky or tucked is left.
        for key in ("transform", "translate", "scale", "animation"):
            self.assertNotIn(key, mast)
        for gone in ("position:sticky", "--tuck", "--held-scale", "bar-held", "bar-name", "--bar-h"):
            self.assertNotIn(gone, CSS + JS + PY)

    def test_every_part_rides_up_with_the_page_on_one_range_one_to_one_then_holds(self):
        riders = re.search(r"\n  ([^{\n]+)\{animation:mast-ride linear forwards;animation-timeline:scroll\(root\);"
                           r"animation-range:0 var\(--travel\)\}", HELD).group(1)
        self.assertEqual(riders.split(","), [".mast .brand", ".mast .nav", ".mast .bar-tools", ".mast .here", ".mast::after", ".find-panel", ".settings-panel"])
        # One keyframe, one range, nothing else moves: no part may start later, last longer or run on its own timeline
        # (the old header eased the name over one range and faded a second name in over another: scattered).
        self.assertEqual(HELD.count("animation:"), 1)
        self.assertEqual(re.findall(r"animation-range:([^;}]+)", CSS), ["0 var(--travel)"])
        self.assertEqual(re.findall(r"[;{]\s*animation-timeline:([^;}]+)", CSS), ["scroll(root)"])
        self.assertNotIn("animation-timing-function", HELD)
        ride = re.search(r"@keyframes mast-ride\{to\{translate:0 ([^;]+);scale:1\}\}", V28)
        self.assertIsNotNone(ride)                                    # `to` only: WebKit fills no backwards
        for width in (1440, 1100, 760, 402, 390, 360):
            for front in (False, True):
                env = tokens(width, front)
                self.assertAlmostEqual(-num(ride.group(1), env), env["--travel"])   # the ride is as long as the range
                # held, what stays is the rest header less its ride: 14px of air, the capitals, 10px, the line
                self.assertAlmostEqual(env["--mast-h"] + 2 - env["--travel"], env["--mast-stuck"])
                self.assertAlmostEqual(env["--mast-stuck"], 14 + env["--cap"] + 12)
        self.assertEqual(tokens(1440, False)["--mast-stuck"], 47)       # the family's held height, to the pixel
        self.assertEqual(tokens(1440, False)["--travel"], 14)           # an inner page rides 14px, then holds
        self.assertEqual(tokens(1440, False)["--mast-h"], 59)
        self.assertIn("html{scroll-padding-top:calc(var(--mast-stuck) + 1rem)}", HELD)
        self.assertEqual(CSS.count("scroll-margin-top:calc(var(--mast-stuck) + 1rem)"), 2)   # providers and models

    def test_held_the_name_keeps_its_size_so_it_stands_as_near_the_line_as_at_rest(self):
        self.assertEqual(ROOT["--h0"], "1")                            # off the front page nothing is scaled
        self.assertEqual(ROOT["--air"], "28px")
        self.assertEqual(ROOT["--air-held"], "14px")
        self.assertIn("scale:1}}", V28[V28.index("@keyframes mast-ride"):])
        self.assertIn(".brand{display:flex;align-items:flex-end;margin-bottom:-10px;gap:.6em;font-size:var(--nm);", CSS)

    def test_the_front_pages_name_starts_as_large_as_its_row_allows_and_lands_on_the_line(self):
        self.assertEqual(rule(HELD, ".mast .brand")["scale"], "var(--h0)")
        self.assertIn(".brand{", CSS)
        self.assertIn("transform-origin:0 100%}", CSS[CSS.index(".brand{display:flex"):][:200])   # the mark's foot on the line
        for width, scrollbar in [(w, s) for w in (1920, 1440, 1100, 1000, 900, 761, 700, 681, 600, 560, 430, 402, 390,
                                                  375, 360, 320) for s in (0, 15)]:
            env = tokens(width, True, scrollbar)
            h0 = env["--h0"]
            self.assertTrue(1 <= h0 <= 2, (width, h0))
            self.assertAlmostEqual(env["--hbw"], brand_width(env), delta=.01)
            # It fits beside what shares its row, in the real column (an always-on scrollbar takes 15px of 100vw). Above
            # 1000px the row holds both tabs, the lens, toggle and gear; from 681px to 1000px the tabs have a row above
            # the name, so the row holds the lens, toggle and gear; on a phone the same with 12px clear.
            column = min(width - scrollbar - (32 if width <= 760 else 40), 952)
            if width <= 680:
                beside = 12 + 3 * 24.4
            elif width <= 1000:
                beside = 24 + 3 * 24.4
            else:
                beside = 24 + TABS_BESIDE + 17.6 + 3 * 24.4
            self.assertLessEqual(brand_width(env) * h0 + beside, column + .01, (width, scrollbar))
            # the room above grows with its tallest part, the mark, and the ride with it
            self.assertAlmostEqual(env["--hE"], (10 + .73 * env["--nm"]) * 1.0119 * env["--logo"] * (h0 - 1))
            self.assertAlmostEqual(env["--travel"], 14 + env["--hE"])
        # Adding the gear consumes one control width, so the name maximises the
        # remaining room rather than retaining the former two-control scale.
        for scrollbar in (0, 15):
            env = tokens(1440, True, scrollbar)
            self.assertAlmostEqual(env["--h0"], min(2, env["--hroom"] / brand_width(env)), delta=.0001)
        self.assertIn("@media (pointer:fine){:root{--sb:15px}}", V28)
        self.assertEqual(ROOT["--sb"], "0px")                           # a phone's scrollbar floats over the page
        self.assertIn("@media (prefers-reduced-motion:reduce){:root.front{--h0:1}}", HELD)
        self.assertIn("@property --hroom{syntax:'<length>';inherits:true;initial-value:0px}", V28)
        self.assertIn("@property --hbw{syntax:'<length>';inherits:true;initial-value:1px}", V28)
        self.assertIn("--h0:min(2,max(1,tan(atan2(var(--hroom),var(--hbw)))))", HELD)

    def test_on_a_phone_the_lens_and_the_toggle_stand_beside_the_name_never_in_a_row_of_their_own(self):
        # Adapted (16 Oct): the nav has two tabs, so on a phone they take a row above the name. The lens and the toggle
        # still stand beside the name on its baseline, and nothing else shares that row.
        self.assertIn("@media (max-width:1000px){", V28)
        self.assertIn(".mast .nav{margin-left:0;order:-1;flex-basis:100%}", V28)
        self.assertIn(".mast .bar-tools{margin-left:auto}", V28)
        self.assertNotIn(".mast .nav{display:none}", V28)
        for width, scrollbar in [(w, s) for w in (560, 430, 402, 390, 375, 360, 320) for s in (0, 15)]:
            env = tokens(width, False, scrollbar)
            self.assertLessEqual(env["--nm"], 24)
            column = width - scrollbar - 32
            # the name, 12px of air, the lens, toggle and gear (73.2px): the row holds them, at the name's held size
            self.assertLessEqual(brand_width(env) + 12 + 3 * 24.4, column + 1e-9, width)
        self.assertAlmostEqual(tokens(402, False)["--nm"], (402 - 32 - 86 - 21.46) / 12.533)   # three controls keep a 402px row within its column
        # All three approved 17px controls share colour and baseline geometry.
        tools = rule(CSS, ".bar-tools")
        self.assertEqual(tools["--tool-icon-size"], "17px")
        self.assertEqual(tools["--tool-color"], "light-dark(var(--human),var(--machine))")
        self.assertEqual(tools["color"], "var(--tool-color)")
        self.assertEqual(tools["margin"],
                         "-10px 0 calc(var(--tool-icon-size) * 6.15 / 24 - 22px) 1.1rem")
        self.assertEqual(tools["align-self"], "flex-end")
        self.assertIn("@media (max-width:680px){.mast .bar-tools{translate:0 calc(10px * (1 - var(--h0)))}}", HELD)
        self.assertNotIn("bar-name", render())

    def test_reduced_motion_stops_every_animation_but_the_ride_which_is_the_pages_own_movement(self):
        kill = re.search(r"@media\(prefers-reduced-motion:reduce\)\{([^{]+)\{animation:none!important", CSS).group(1)
        self.assertEqual(kill, ":not(.mast>.brand,.mast>.nav,.mast>.bar-tools,.mast>.here,.find-panel,.settings-panel),"
                               ":not(.mast)::before,:not(.mast)::after")

    def test_the_search_panel_opens_under_the_line_at_the_columns_end_and_rides_with_it(self):
        self.assertEqual(rule(CSS, ".find-panel,.settings-panel")["top"], "calc(100% + var(--line-w))")
        panel = rule(HELD, ".find-panel,.settings-panel")
        self.assertEqual((panel["right"], panel["width"]), ("var(--gut)", "min(25rem,100% - 2 * var(--gut))"))

    def test_one_block_at_the_end_of_the_file(self):
        self.assertEqual(CSS.count("/* v2.8 header */"), 1)
        self.assertNotIn("/* v2.", V28[len("/* v2.8 header */"):])     # the last block in the file
        self.assertNotIn("/* v2.7 family header */", CSS)
        stripped = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        self.assertEqual(stripped.count("{"), stripped.count("}"))   # an open block silently scopes all after it

    # ---- checks added in the 16 October pass --------------------------------------------------------------------

    def test_both_tabs_remain_reachable_on_every_page(self):
        for settings, benchmarks in [(False, False), (True, False), (False, True)]:
            markup = console._masthead(settings=settings, benchmarks=benchmarks)
            self.assertIn('href="/benchmarks"', markup)
            self.assertIn('data-t="Providers"', markup)
            self.assertIn('data-t="Benchmarks"', markup)
        self.assertNotRegex(CSS, r'\.mast \.nav\{[^}]*display:none')

    def test_narrow_header_uses_intrinsic_height_and_its_own_tab_row(self):
        narrow = CSS[CSS.index('.mast{position:relative;height:auto;'):]
        self.assertIn('.mast{position:relative;height:auto;padding-left:0;padding-right:0;row-gap:calc(24px + var(--hE))}', narrow)
        self.assertIn('.mast .nav{margin-left:0;order:-1;flex-basis:100%}', narrow)
        self.assertIn('main{padding-top:0}', narrow)
        self.assertIn('.find-panel,.settings-panel{right:0;width:min(25rem,100%)}', narrow)

    def test_desktop_keeps_the_shared_scroll_ride_and_panel_alignment(self):
        self.assertIn('position:fixed;top:0;left:0;right:0;', CSS)
        self.assertIn('animation:mast-ride linear forwards;animation-timeline:scroll(root)', CSS)
        self.assertIn('@keyframes mast-ride{to{translate:0 calc(-1 * var(--travel));scale:1}}', CSS)
        self.assertIn('right:var(--gut);width:min(25rem,100% - 2 * var(--gut))', CSS)

    def test_equal_halves_share_one_painted_layer(self):
        self.assertIn('linear-gradient(90deg,var(--line-l) 0 50%,var(--line-r) 50%)', CSS)
        self.assertIn('--line-l:var(--human);--line-r:var(--machine)', CSS)
        self.assertNotIn('anchor(--here', CSS)
        self.assertNotIn('.mast:has(> .here)::after', CSS)
        self.assertNotIn('view-transition-name:mast-here', CSS)
        self.assertNotIn('view-transition-name:mast-current', CSS)

    def test_olive_dot_is_centred_and_rides_with_the_rule(self):
        dot = re.search(r'\.mast \.here\{([^}]+)\}', CSS).group(1)
        for declaration in ['left:calc(50% - 4px)', 'width:8px', 'height:8px',
                            'background:var(--between)', 'box-shadow:0 0 0 2px var(--paper)',
                            'top:calc(100% + var(--line-w) / 2 - 4px)']:
            self.assertIn(declaration, dot)
        self.assertIn('.mast .here,.mast::after', CSS)

    def test_tab_selection_keeps_accessible_and_visible_indication(self):
        self.assertIn('aria-current="page"', console._masthead())
        self.assertIn('text-decoration-color:var(--human);text-underline-offset:6px', CSS)
        self.assertIn('content:attr(data-t) / ""', CSS)
        self.assertIn('.nav a[aria-current] .tl{font-weight:700}', CSS)

    def test_scale_remeasures_real_layout_after_fonts_and_resize(self):
        for source in ['nav.offsetWidth', 'tools.offsetWidth', 'brand.offsetWidth + 1',
                       'document.fonts?.ready.then(measureHeader)', 'new window.ResizeObserver(measureHeader)',
                       "window.addEventListener('resize', measureHeader)", 'window.innerWidth <= 1000',
                       "document.fonts?.addEventListener?.('loadingdone', measureHeader)"]:
            self.assertIn(source, JS)
        self.assertNotIn("setProperty('--h0'", JS)                    # the scale stays in the CSS arithmetic
        self.assertNotIn('179px', CSS)                                # the single-tab reserve

    def test_reduced_motion_uses_final_size_and_no_navigation_animation(self):
        self.assertIn('@media (prefers-reduced-motion:reduce){:root.front{--h0:1}}', CSS)
        self.assertIn('@media (prefers-reduced-motion:no-preference){\n  @view-transition', CSS)

    def test_inline_tab_icon_has_no_plate_and_adapts_ink(self):
        icon = ET.fromstring(urllib.parse.unquote(console.TAB_ICON.split(',', 1)[1]))
        self.assertEqual(icon.attrib['viewBox'], '0 0 16 16')
        for rect in icon.findall('{*}rect'):
            self.assertFalse(rect.get('width') == '16' and rect.get('height') == '16')
        style = icon.find('{*}style').text
        self.assertIn('prefers-color-scheme:light', style)
        self.assertIn('--i:#14130F', style)
        self.assertIn('--i:#E9E6DF', style)

    def test_stylesheet_stays_balanced(self):
        stripped = re.sub(r'/\*.*?\*/', '', CSS, flags=re.S)
        self.assertEqual(stripped.count('{'), stripped.count('}'))


if __name__ == "__main__":
    unittest.main()
