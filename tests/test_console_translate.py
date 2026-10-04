"""A translator (Chrome's, Safari's, Google's page translator) must never change what is not prose.

On 2 October 2026 Google's translator turned the console's model names into "Claude Sonetto 4.6" and "Soluzione GPT-6.1",
"Gemini" into "Gemelli", "Antigravity" into "Antigravità", a model id into "pensiero di Claude Opus 4-6" and "$200" into
"200 dollari". console-assets/keep.py marks what must stay as written with translate="no" on every page as it is made
(console-assets/keep.js does it for what the page's script adds). These tests read the page the way a translator does
(what stands outside translate="no") and fail when a model name, id, amount or the signature line is in it, when a page
loses lang="en" or its kept head, when a font is served without the header Google's translated copy needs, or when the
browser's rules drift from the server's. Everything here is the shipped example overlay.

The other console tests read pages with the marks taken off (tests/test_console.py); this file loads a console of its own
and reads the real thing.
"""

import http.client
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FLEET_NO_AUTO_REFRESH", "1")


def load_console():
    spec = importlib.util.spec_from_file_location("orchestrator_console_marked", ROOT / "scripts" / "console.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


console = load_console()
keep = console.console_keep
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
RAW = {"script", "style", "svg", "template", "textarea", "noscript"}


class Translatable(HTMLParser):
    """The text a translator would rewrite: everything outside translate="no", scripts, styles, svg and the head. Written
    here on its own, not with keep.py: a check that used the marking's own code would pass whatever the marking missed."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open, self.no, self.raw, self.text = [], 0, 0, []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        kept = dict(attrs).get("translate") == "no"
        self.open.append((tag, kept))
        self.no += kept
        self.raw += tag in RAW

    def handle_endtag(self, tag):
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                for t, k in self.open[i:]:
                    self.no -= k
                    self.raw -= t in RAW
                del self.open[i:]
                return

    def handle_data(self, data):
        if not self.no and not self.raw and data.strip():
            self.text.append(data)


def translatable(page):
    parser = Translatable()
    parser.feed(re.sub(r"<!--.*?-->", "", page, flags=re.S))
    return "\n".join(parser.text)


# What may not stand in translatable text, whatever page it is on (the rules, as a list).
NEVER = [
    ("the product's name", re.compile(r"Castor|Crossfeed")),
    ("the signature line", re.compile(r"we give a dam", re.I)),
    ("a model name", re.compile(r"\b(?:Claude|Gemini|GPT|Kimi|DeepSeek|Codex|Opus|Sonnet|Haiku|Antigravity|ChatGPT|OpenCode|OpenRouter|Copilot)\b")),
    ("a code or capital abbreviation (UTC and AI are written right by a translator)", re.compile(r"\b(?!(?:UTC|AI)\b)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,}\b")),
    ("an identifier", re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b|\b[a-z][a-z0-9]*(?:-[a-z0-9.]+)*-\d[a-z0-9.-]*\b")),
    ("an amount of money", re.compile(r"[$€£]\s?\d")),
    ("an email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
]


def leaks(page, also=()):
    text = translatable(page)
    found = []
    for what, pattern in NEVER:
        m = pattern.search(text)
        if m:
            found.append(f'{what}: "{m.group(0)}" in {text[max(0, m.start() - 30):m.end() + 40]!r}')
    found += [f'seeded text "{s}"' for s in also if s in text]
    return found


class TheMarking(unittest.TestCase):
    """The rules, case by case: each is a word the live page was seen to lose."""

    def test_the_name_keeps_the_space_beside_it_inside_its_mark_and_only_the_name_is_marked(self):
        out = keep.keep_html("<h2>Why Castor</h2><p>Made by Castor, a studio.</p>")
        self.assertIn('Why<span translate="no"> Castor</span>', out)   # a translator trims what it rewrites
        self.assertNotIn("<h2 translate", out)                       # never the whole heading

    def test_model_names_providers_ids_and_amounts_are_kept_whole(self):
        out = keep.keep_html("<p>usually Claude Opus 4.6 Thinking on Antigravity · Gemini, or GPT-6.1 Sol, id claude-sonnet-4-6, $200 a month</p>")
        for kept in ("Claude Opus 4.6 Thinking", "Antigravity", "Gemini", "GPT-6.1 Sol", "claude-sonnet-4-6", "$200"):
            self.assertRegex(out, rf'<span translate="no">[^<]*{re.escape(kept)}', kept)
        self.assertEqual(leaks(out), [])
        self.assertIn("usually", translatable(out))
        self.assertIn("a month", translatable(out))

    def test_the_signature_line_is_kept_whole(self):
        self.assertEqual(keep.keep_html('<span class="qt">we give a dam</span>'), '<span class="qt" translate="no">we give a dam</span>')

    def test_an_element_that_is_only_a_kept_word_is_marked_itself_with_no_new_element(self):
        self.assertEqual(keep.keep_html('<span class="nm">Sonnet 5.5</span>'), '<span class="nm" translate="no">Sonnet 5.5</span>')
        self.assertEqual(keep.keep_html("<code>archive</code>"), '<code translate="no">archive</code>')

    def test_a_models_details_stay_as_written_but_its_labels_and_sentences_follow_the_reader(self):
        out = keep.keep_html('<details class="more"><dl><dt>Model id</dt><dd>claude-opus-4-6-thinking</dd><dt>Roles</dt><dd>review</dd></dl></details><p>Nothing to show.</p>')
        self.assertIn('<dt translate="no">Model id</dt>', out)
        self.assertIn('<dd translate="no">review</dd>', out)
        self.assertIn("<p>Nothing to show.</p>", out)

    def test_the_space_beside_a_kept_element_goes_into_a_mark_of_its_own(self):
        out = keep.keep_html('<p><b>Spends</b> in <span class="nm">Sonnet 5.5</span></p>')
        self.assertRegex(out, r'in<span translate="no"> </span><span class="nm" translate="no">Sonnet 5\.5</span>')

    def test_the_head_is_kept_scripts_and_svg_are_untouched_and_marking_twice_changes_nothing(self):
        page = '<!doctype html><html lang="en"><head><title>Providers · Crossfeed Orchestrator</title></head><body><svg><text>GPT</text></svg><script>var a="GPT"</script><p>GPT</p></body></html>'
        out = keep.keep_html(page)
        self.assertIn('<head translate="no"><title>Providers · Crossfeed Orchestrator</title></head>', out)
        self.assertIn('<svg><text>GPT</text></svg><script>var a="GPT"</script>', out)
        self.assertEqual(keep.keep_html(out), out)

    def test_marking_the_page_costs_milliseconds(self):
        import time
        page = "<!doctype html><html lang=\"en\"><head><title>x</title></head><body>" + ('<div class="opt"><span class="nm">Claude Sonnet 4.6</span><span class="bf">Also picked for reviews and builds.</span><dd>claude-sonnet-4-6</dd></div>' * 400) + "</body></html>"
        keep.keep_html(page)
        t0 = time.perf_counter()
        for _ in range(10):
            keep.keep_html(page)
        each = (time.perf_counter() - t0) / 10 * 1000
        print(f"keep_html on {len(page) // 1000} KB: {each:.1f} ms")
        self.assertLess(each, 100)


class ThePages(unittest.TestCase):
    """The console as it is sent, with the shipped example overlay."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name)
        overlay = json.loads((ROOT / "examples" / "access-overlay.example.json").read_text(encoding="utf-8"))
        overlay["quota_pools"].setdefault("claude", {})["plan"] = {"name": "Claude Max 20x", "price": 200, "currency": "USD", "billing": "subscription", "allowance": "an unpublished weekly allowance"}
        cls.overlay = cls.state / "overlay.json"
        cls.overlay.write_text(json.dumps(overlay), encoding="utf-8")
        cls.app = console.Console(cls.overlay, cls.state, 0)
        cls.server = console.bind(cls.app, 0)
        cls.port = cls.app.port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{self.port}")
        if cookie:
            conn.putheader("Cookie", cookie)
        conn.endheaders()
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def cookie(self):
        response, _ = self.get(f"/login?key={self.app.login_key}")
        return response.getheader("Set-Cookie").split(";", 1)[0]

    def test_the_providers_page_keeps_model_names_ids_amounts_and_the_signature_to_itself(self):
        response, body = self.get("/", self.cookie())
        page = body.decode()
        self.assertEqual(response.status, 200)
        self.assertEqual(leaks(page, ("Claude Opus 4.6 Thinking", "GPT-6.1 Sol", "Gemini 3.1 Pro", "claude-opus-4-6-thinking", "$200")), [])
        self.assertIn('<html lang="en"', page)           # a translator is told the source language, so it does not have to guess
        self.assertIn('<head translate="no"', page)      # the tab keeps the name
        self.assertIn('src="/static/keep.js?v=', page)   # and what the page's script adds is marked in the browser

    def test_the_page_a_refusal_makes_is_marked_the_same(self):
        response, body = self.get("/")
        self.assertEqual(response.status, 401)
        page = body.decode()
        self.assertEqual(leaks(page), [])
        self.assertIn('<html lang="en"', page)
        self.assertIn('<head translate="no"', page)

    def test_the_marks_are_really_on_in_a_page_the_console_sends(self):
        _, body = self.get("/", self.cookie())
        page = body.decode()
        self.assertRegex(page, r'<b class="wordmark" translate="no">Castor</b>')
        self.assertIn('<span translate="no">we give a dam</span>', page)
        self.assertRegex(page, r'<span class="nm" translate="no">[^<]+</span>')


class TheFonts(unittest.TestCase):
    """Google shows a translated page from its own address, so a typeface served without this header is dropped there and
    the page falls back to a wider face (seen on the studio's site, round 58)."""

    @classmethod
    def setUpClass(cls):
        ThePages.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        ThePages.tearDownClass.__func__(cls)

    get = ThePages.get

    def test_every_font_is_served_with_access_control_allow_origin_star(self):
        fonts = sorted(p.name for p in (console.ASSETS / "fonts").glob("*.woff2"))
        self.assertGreaterEqual(len(fonts), 3)
        for name in fonts:
            response, _ = self.get(f"/static/fonts/{name}")
            self.assertEqual(response.status, 200, name)
            self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*", name)

    def test_every_font_the_stylesheet_names_is_one_that_is_served_that_way(self):
        css = (console.ASSETS / "console.css").read_text(encoding="utf-8")
        named = re.findall(r"url\(\s*[\"']?(fonts/[^)\"']+\.woff2)", css)   # relative to /static/console.css
        self.assertTrue(named)
        for name in named:
            url = "/static/" + name
            response, _ = self.get(url)
            self.assertEqual(response.status, 200, url)
            self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*", url)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TheBrowsersRulesAgreeWithTheServers(unittest.TestCase):
    """keep.js (what the page's script adds) and keep.py (the page as sent) are one set of rules written twice: the same
    text must give the same stretches in both."""

    SAMPLES = [
        "Antigravity · Claude and GPT",
        "Crossfeed picks the best for each task, usually Claude Opus 4.6 Thinking",
        "Shares ONE quota window with antigravity-3p-opus",
        "id claude-opus-4-6-thinking and codex/gpt-6.1-sol",
        "Claude Max 20x, $200 a month, about 816,881 nm",
        "write to pilot@example.test or https://example.test/a now",
        "we give a dam, by Castor",
        "Used even when its limits look low",
        "it&#39;s 5",
    ]

    def test_the_same_text_gives_the_same_stretches(self):
        script = "const k=require(process.argv[1]);const s=JSON.parse(process.argv[2]);console.log(JSON.stringify(s.map(t=>k.runs(t))))"
        out = subprocess.run(["node", "-e", script, str(console.ASSETS / "keep.js"), json.dumps(self.SAMPLES)], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [keep.runs(t) for t in self.SAMPLES])


if __name__ == "__main__":
    unittest.main()
