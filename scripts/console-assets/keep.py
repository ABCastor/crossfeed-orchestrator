"""Protect product names, model names, identifiers and amounts from browser translation.
The corresponding browser rules are in keep.js beside this file.

Mark only the protected word, including touching whitespace when needed; keep
sentences translatable. Translators can rewrite model names and trim whitespace.
"""

from __future__ import annotations

import re

# Names that are never translated. Longest first: a longer name wins over the one it starts with.
NAMES = [
    "Crossfeed Orchestrator", "Crossfeed", "Castor", "Chip",
    "Artificial Analysis", "GitHub Copilot", "Copilot Student", "Antigravity", "OpenRouter", "OpenCode Go", "OpenCode", "ChatGPT Plus",
    "ChatGPT", "Copilot", "CodexBar", "Microsoft", "Anthropic", "OpenAI", "Google AI", "Google", "Gmail", "Tailscale", "1Password",
    # A model: its family and every version or size word after it ("Claude Opus 4.6 Thinking", "Gemini 3.1 Pro Preview",
    # "GPT-6.1 Sol", "Kimi K2.7 Code", "Claude Max 20x").
    r"(?:Claude|Gemini|GPT|Kimi|DeepSeek|Codex|Opus|Sonnet|Haiku)(?:[ -](?:[A-Z]?\d[\w.]*|Opus|Sonnet|Haiku|Max|Pro|Flash|Ultra|Mini|Nano|Turbo|Sol|Terra|Luna|Astra|Code|Thinking|Preview|Latest|Instruct))*",
    # The signature line: a pun that exists only in English.
    "we give a dam", "We give a dam",
]

# Units a translator was seen to rewrite ("L/100 pkm" came back "L/100 km"): kept with their number.
_UNIT = r"(?:nm|NM|kg|km|pkm|mi|ft|kt|lb|hPa)(?![A-Za-z])"

_SOURCE = "|".join(
    [
        r"https?://[^\s<>\"']+",  # an address
        r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",  # an email address
        r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|dev|app|aero)\b",  # a domain
        r"\b(?:" + "|".join(n if n.startswith("(?:") else re.escape(n) for n in NAMES) + r")(?![A-Za-z])",
        r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b",  # an identifier: archive_message
        r"\b[a-z][a-z0-9]*(?:-[a-z0-9.]+)*-\d[a-z0-9.-]*\b",  # a model or pool id with its version: claude-sonnet-4-6, antigravity-3p
        r"\b[a-z][a-z0-9-]*/[a-z0-9][a-z0-9.-]*\b",  # a provider and model: codex/gpt-6.1-sol
        r"[$\u20ac\u00a3]\s?\d[\d,.]*",  # an amount of money: $200 came back "200 dollari"
        r"\b[A-Z0-9]{1,2}-[A-Z0-9]{2,5}\b",  # a registration: EI-ABC, G-ABCD
        r"\b(?!(?:UTC|AI)\b)(?=[A-Z0-9₂]*[A-Z])[A-Z0-9₂]{2,}\b",  # a code or capital abbreviation: LIPZ, B738, FR926, MCP (not UTC or AI: a translator writes those right)
        r"\b\d[\d,.]*\s?" + _UNIT,  # a number with its unit: 816,881 nm
    ]
)
_KEEP = re.compile(_SOURCE)

# This product's data, by its markup.
_KEPT_TAGS = {"code", "kbd", "samp", "var", "pre", "dt", "th", "textarea"}
_KEPT_CLASSES = {"brand", "compact-brand", "bn", "bd", "wordmark", "nm", "key-hint"}
_KEPT_INSIDE = {"dd": "more"}  # a value in a model's details: its id, its status, the roster's own notes
# Words a translator renders wrongly when they stand alone ("Home" came back "Casa", "Show" "Spettacolo", "Apply"
# "Fare domanda a"): kept when they are all an element says.
_WHOLE = {"Home", "Show", "Apply"}

_GAP = re.compile(r"^[\s\-–—/·:,.;()→⇄+*|]*$")


def runs(text: str) -> list[list[int]]:
    """The stretches of `text` to keep as [start, end] pairs: each kept word, neighbours joined when only punctuation or
    space lies between them, and every space touching one taken in."""
    found: list[list[int]] = []
    for m in _KEEP.finditer(text):
        # Never inside an HTML entity (&#39;, &amp;).
        if m.start() > 0 and text[m.start() - 1] in "&#":
            continue
        found.append([m.start(), m.end()])
    joined: list[list[int]] = []
    for s, e in found:
        if joined and _GAP.match(text[joined[-1][1] : s]):
            joined[-1][1] = e
        else:
            joined.append([s, e])
    for r in joined:
        while r[0] > 0 and text[r[0] - 1].isspace():
            r[0] -= 1
        while r[1] < len(text) and text[r[1]].isspace():
            r[1] += 1
    out: list[list[int]] = []
    for r in joined:  # spaces taken in may have made two runs touch
        if out and r[0] <= out[-1][1]:
            out[-1][1] = max(out[-1][1], r[1])
        else:
            out.append(r)
    return out


def _edges(text: str, before: bool, after: bool) -> list[list[int]]:
    """`runs` plus the spaces at either end of `text` when a kept element stands beside it: a translator trims the text it
    rewrites, so "<b>Archive</b> in <code>x</code>" came back "Archive inx" until the space went into a mark of its own."""
    r = runs(text)
    lead = len(text) - len(text.lstrip()) if before else 0
    trail = len(text) - len(text.rstrip()) if after else 0
    if lead:
        r.insert(0, [0, lead])
    if trail and lead != len(text):
        r.append([len(text) - trail, len(text)])
    out: list[list[int]] = []
    for x in sorted(r):
        if out and x[0] <= out[-1][1]:
            out[-1][1] = max(out[-1][1], x[1])
        else:
            out.append(list(x))
    return out


def _all_kept(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t in _WHOLE:
        return True
    r = runs(text)
    return len(r) == 1 and not text[: r[0][0]].strip() and not text[r[0][1] :].strip()


_PIECE = re.compile(r"<!--.*?-->|<(script|style|svg|template|textarea|title|noscript)\b.*?</\1\s*>|</?[A-Za-z][^>]*>", re.S)
_TAG = re.compile(r"^<(/?)([A-Za-z][\w-]*)([^>]*)>$", re.S)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_CLASS = re.compile(r'\bclass="([^"]*)"')
_NO = re.compile(r'\btranslate\s*=\s*"?no')
_END = re.compile(r"\s*/?>$")


def _add_no(tag: str) -> str:
    return _END.sub(lambda m: ' translate="no"' + m.group(0), tag, count=1)


def _classes(attrs: str) -> list[str]:
    m = _CLASS.search(attrs) if "class=" in attrs else None
    return m.group(1).split() if m else []


def _is_kept(name: str, cls: list[str], within: list[dict]) -> bool:
    if name in _KEPT_TAGS or any(c in _KEPT_CLASSES for c in cls):
        return True
    want = _KEPT_INSIDE.get(name)
    return bool(want) and any(want in w["cls"] for w in within)


def keep_html(html: str) -> str:
    """The page with everything a translator must leave alone marked: the head (the tab keeps the name), codes, names, ids
    and the signature line. Idempotent: a page already marked comes back unchanged."""
    out: list[str] = []
    stack: list[dict] = []  # the elements open around here: name, cls, no
    skip = 0
    at = 0
    prev_name = ""  # the opening tag just written, so an element holding only a kept word can be marked itself
    prev_at = -1
    after = False  # the last thing written closed a kept element: the space after it needs a mark of its own

    def opens_kept(piece: str | None, end: int) -> bool:
        """Does the element this tag opens end up kept: by its markup, or because it holds only a kept word?"""
        t = _TAG.match(piece or "")
        if not t or t.group(1):
            return False
        name, attrs = t.group(2).lower(), t.group(3)
        if _NO.search(attrs) or ("translate=" not in attrs and _is_kept(name, _classes(attrs), stack)):
            return True
        if name in _VOID or attrs.endswith("/"):
            return False
        lt = html.find("<", end)
        return lt > end and html.startswith(f"</{name}", lt) and _all_kept(html[end:lt])

    def flush(text: str, nxt: str | None, nxt_end: int = 0) -> None:
        nonlocal skip
        if not text or skip or not text.strip():
            out.append(text)
            return
        if prev_name and prev_at == len(out) - 1 and nxt and nxt.startswith(f"</{prev_name}") and _all_kept(text):
            out[prev_at] = _add_no(out[prev_at])  # the element holds only this: mark the element, change no text
            stack[-1]["no"] = True  # and it counts as kept for the spaces beside it
            skip += 1
            out.append(text)
            return
        from_ = 0
        # Only a space can be lost, so the look ahead is made only when the text ends in one.
        for s, e in _edges(text, after, text[-1].isspace() and opens_kept(nxt, nxt_end)):
            if s > from_:
                out.append(text[from_:s])
            out.append(f'<span translate="no">{text[s:e]}</span>')
            from_ = e
        out.append(text[from_:] if from_ else text)

    for m in _PIECE.finditer(html):
        flush(html[at : m.start()], m.group(0), m.end())
        after = False
        at = m.end()
        piece = m.group(0)
        t = None if m.group(1) or piece.startswith("<!--") else _TAG.match(piece)
        if not t:
            out.append(piece)
            prev_name = ""
            continue
        close, tag_name, attrs = t.groups()
        name = tag_name.lower()
        if close:
            i = len(stack) - 1
            while i >= 0 and stack[i]["name"] != name:
                i -= 1
            if i >= 0:
                after = stack[i]["no"]
                skip -= sum(1 for j in range(i, len(stack)) if stack[j]["no"])
                del stack[i:]
            prev_name = ""
        else:
            cls = _classes(attrs)
            no = bool(_NO.search(attrs))
            if "translate=" not in attrs and (name == "head" or _is_kept(name, cls, stack)):
                piece = _add_no(piece)
                no = True
            if name not in _VOID and not attrs.endswith("/"):
                stack.append({"name": name, "cls": cls, "no": no})
                if no:
                    skip += 1
                prev_name = name
            else:
                prev_name = ""
        out.append(piece)
        prev_at = len(out) - 1
    flush(html[at:], None)
    return "".join(out)
