"""The console: one local page that shows every provider pool and sets how much of it to spend.

It is a door, not a view: a click here changes where agents route. So it is locked like one.

- It binds 127.0.0.1 and nothing else. There is no option to widen that.
- Each launch mints its own secret. The sign-in link printed at start carries it once; the
  server trades it for an HttpOnly, SameSite=Strict cookie and redirects, so the secret leaves
  the address bar. Stopping the console voids both.
- Every request must name this server in its Host header (127.0.0.1:PORT or localhost:PORT).
  That defeats DNS rebinding, where a hostile page points its own hostname at 127.0.0.1.
- Every change is a POST that must come from this origin (Origin, else Referer, and a
  same-origin Sec-Fetch-Site when the browser sends one) and carry this launch's form token.
  That defeats a cross-site form post even if a browser were to attach the cookie.
- Responses forbid framing, inline scripts, off-origin loads and caching.

Standard library only. The page renders from `fleetctl.fleet_overview`, the same structure
`fleetctl.py brief` prints, so what the operator sees and what agents read cannot drift.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import http.server
import importlib.util
import json
import math
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "console-assets"
COOKIE = "console_session"

_spec = importlib.util.spec_from_file_location("fleetctl", HERE / "fleetctl.py")
assert _spec and _spec.loader
fleetctl = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("fleetctl", fleetctl)
_spec.loader.exec_module(fleetctl)

# What a translator must never change (model names, ids, amounts, the signature): marked on every page as it is made. The
# rules, written once for every Castor product, are in console-assets/keep.py, and in keep.js for what the page's script adds.
_keep_spec = importlib.util.spec_from_file_location("console_keep", ASSETS / "keep.py")
assert _keep_spec and _keep_spec.loader
console_keep = importlib.util.module_from_spec(_keep_spec)
sys.modules.setdefault("console_keep", console_keep)
_keep_spec.loader.exec_module(console_keep)
keep_html = console_keep.keep_html

try:
    import providers
except ImportError:
    from scripts import providers

PRODUCT_NAME = fleetctl.PRODUCT_NAME
LEVELS = fleetctl.LEVELS
LEVEL_WORDS = {"off": "Off", "low": "Low", "normal": "Normal", "high": "High", "forced": "Ignore estimates"}
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".js": "text/javascript; charset=utf-8"}
# The page is never stored; its assets are. The stylesheet and script are asked for by a content
# hash (?v=...), so a year is safe; the fonts never change under one name. Sending no-store with
# them would make each visit fetch ~390 KB again.
STATIC_CACHE = {".css": "private, max-age=31536000, immutable", ".js": "private, max-age=31536000, immutable",
                ".woff2": "private, max-age=604800"}
CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class Console:
    """Everything one launch knows: where the config lives, and its three secrets."""

    def __init__(self, overlay_path: Path, state_dir: Path, port: int) -> None:
        self.overlay_path = overlay_path
        self.state_dir = state_dir
        self.port = port
        self.login_key = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.form_token = secrets.token_urlsafe(24)
        self._refresh_lock = threading.Lock()
        self._last_refresh = float("-inf")

    def refresh_in_background(self, roster: dict[str, Any]) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            return
        if time.monotonic() - self._last_refresh < 30:
            self._refresh_lock.release()
            return
        self._last_refresh = time.monotonic()

        def refresh():
            try:
                fleetctl.refresh_stale_pools(self.state_dir, fleetctl.quota_sources(roster), roster=roster)
            finally:
                self._refresh_lock.release()

        threading.Thread(target=refresh, daemon=True, name="console-quota").start()

    # ---- the checks, one place each ---------------------------------------------------
    def local_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def local_origins(self) -> set[str]:
        return {f"http://{host}" for host in self.local_hosts()}

    def host_ok(self, headers: Any) -> bool:
        return (headers.get("Host") or "").strip().lower() in self.local_hosts()

    def origin_ok(self, headers: Any) -> bool:
        """A change must provably come from this page. No evidence of origin is a refusal."""
        fetch_site = headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site not in {"same-origin"}:
            return False
        origin = headers.get("Origin")
        if origin:
            return origin in self.local_origins()
        referer = headers.get("Referer") or ""
        return any(referer.startswith(prefix + "/") for prefix in self.local_origins())

    def signed_in(self, headers: Any) -> bool:
        cookies = {}
        for part in (headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                cookies[name] = value
        return hmac.compare_digest(cookies.get(COOKIE, ""), self.session)

    def form_ok(self, token: str) -> bool:
        return hmac.compare_digest(token or "", self.form_token)

    # ---- the fleet ------------------------------------------------------------------------
    def overview(self, refresh: bool = True) -> dict[str, Any]:
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        if refresh:
            self.refresh_in_background(roster)
        runtime = fleetctl.load_json(self.state_dir / "runtime.json", {}) or {}
        overview = fleetctl.fleet_overview(roster, runtime, self.state_dir)
        overview['_brief_context'] = {'roster': roster, 'runtime': runtime}
        overview['provider_sources'] = providers.list_sources(roster)
        evidence = fleetctl.load_json(self.state_dir / 'evidence' / 'levels.json', {}) or {}
        overview['console_order'] = display_order(overview, runtime.get('console_order'), roster, evidence)
        return overview

    def set_order(self, order: Any) -> None:
        validate_order(order)
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            runtime['console_order'] = order

    def refresh_now(self) -> dict[str, str]:
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        with self._refresh_lock:
            outcomes = fleetctl.refresh_stale_pools(
                self.state_dir, fleetctl.quota_sources(roster), roster=roster, force=True)
            self._last_refresh = time.monotonic()
        return outcomes

    def set_level(self, pool: str, level: str) -> None:
        pools = set(fleetctl.read_overlay(self.overlay_path, self.state_dir).get("quota_pools", {}))
        if pool not in pools or level not in LEVELS:
            raise ValueError("unknown pool or level")
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            fleetctl.set_pool_level(runtime, pool, level)

    def set_toggle(self, pool: str, model: str, on: bool) -> None:
        """Switch one model of a provider on or off."""
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            fleetctl.set_model_toggle(runtime, roster, pool, model, on)
        fleetctl.pins_after_switch(roster, self.state_dir)   # files that name a model follow the switch, as on the CLI

    def set_choice(self, pool: str, model: str) -> None:
        """Run only one model on a provider (a model key), or switch every model on ("auto")."""
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            if model == "auto":
                # "Switch all on" is the list you see: the current models. An older model stays as you left it;
                # it never stands in for another, and runs only when a task names it.
                for key in fleetctl.current_models(roster, pool):
                    fleetctl.set_model_toggle(runtime, roster, pool, key, True)
            else:
                fleetctl.set_model_choice(runtime, roster, pool, model)
        fleetctl.pins_after_switch(roster, self.state_dir)

    def set_preference(self, model: str, preference: str) -> None:
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        if model not in {item["model"] for item in fleetctl.model_overview(roster, {})}:
            raise ValueError("unknown model")
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            fleetctl.set_model_preference(runtime, model, preference)


# ---- rendering ------------------------------------------------------------------------------
POOL_SORTS = {'your': 'Your order', 'quota': 'Quota left', 'reset': 'Resets soonest', 'name': 'Name'}
MODEL_SORTS = {'your': 'Your order', 'quality': 'Quality', 'cheapest': 'Cheapest', 'name': 'Name'}


def validate_order(order: Any) -> None:
    def ids(value: Any) -> bool:
        return (isinstance(value, list) and all(isinstance(key, str) and 0 < len(key) <= 200 for key in value)
                and len(value) == len(set(value)))
    if not isinstance(order, dict) or set(order) != {'pools', 'models', 'sort'}:
        raise ValueError('invalid display order')
    models, sorts = order['models'], order['sort']
    if (not ids(order['pools']) or not isinstance(models, dict)
            or not all(isinstance(key, str) and ids(value) for key, value in models.items())
            or not isinstance(sorts, dict) or set(sorts) != {'pools', 'models'}
            or not isinstance(sorts['pools'], str) or sorts['pools'] not in POOL_SORTS or not isinstance(sorts['models'], dict)
            or not all(isinstance(key, str) and isinstance(value, str) and value in MODEL_SORTS for key, value in sorts['models'].items())):
        raise ValueError('invalid display order')


def _append_order(saved: list[str], current: list[str]) -> list[str]:
    return [key for key in saved if key in current] + [key for key in current if key not in saved]


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def display_order(overview: dict[str, Any], saved: Any = None, roster: dict[str, Any] | None = None,
                  evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Console-only presentation; never change the roster or selector input."""
    saved = saved or {'pools': [], 'models': {}, 'sort': {'pools': 'name', 'models': {}}}
    try:
        validate_order(saved)
    except (ValueError, TypeError):
        saved = {'pools': [], 'models': {}, 'sort': {'pools': 'name', 'models': {}}}
    by_pool = {pool['pool']: pool for pool in overview['pools']}
    ids = list(by_pool)
    if roster:
        ids = _append_order(list(roster.get('quota_pools', {})), ids)
    pools = _append_order(saved['pools'], ids)
    def quota(key):
        value = _number((by_pool[key].get('quota') or {}).get('used_percent'))
        if value is None:
            readings = [_number(limit.get('used_percent')) for limit in by_pool[key].get('limits') or []]
            readings = [reading for reading in readings if reading is not None]
            value = max(readings) if readings else None
        return (value is None, value if value is not None else 0)
    def reset(key):
        values = [_number(limit.get('resets_in_s')) for limit in by_pool[key].get('limits') or []]
        values = [value for value in values if value is not None]
        fallback = _number((by_pool[key].get('quota') or {}).get('resets_in_s'))
        value = min(values) if values else fallback
        return (value is None, value if value is not None else 0)
    ranks = {'pools': {'your': pools, 'quota': sorted(ids, key=quota), 'reset': sorted(ids, key=reset),
                       'name': sorted(ids, key=lambda key: by_pool[key]['label'].casefold())}, 'models': {}}
    by_model = {model['model']: model for model in overview['models']}
    rows = (evidence or {}).get('rows') or []
    models = {}
    for pool, item in by_pool.items():
        current = [option['model'] for option in item.get('options') or []]
        models[pool] = _append_order(saved['models'].get(pool, []), current)
        qualities, prices = {}, {}
        for key in current:
            measurements = [row for row in rows if (row.get('model_key') or row.get('model')) == key]
            # Highest measured effort row, with equal weight per measured task family.
            scores = [[_number(q.get('mean')) for q in (row.get('q') or {}).values()
                       if isinstance(q, dict) and not q.get('unknown')] for row in measurements]
            scores = [[value for value in score if value is not None] for score in scores]
            quality = [sum(score) / len(score) for score in scores if score]
            qualities[key] = max(quality) if quality else None
            costs = []
            for row in measurements:
                price = row.get('price_1m') or {}
                inp, out = _number(price.get('in')), _number(price.get('out'))
                if inp is not None and out is not None and inp >= 0 and out >= 0:
                    costs.append((3 * inp + out) / 4)
            prices[key] = min(costs) if costs else None
        def measured(key, values, descending=False):
            value = values[key]
            return value is None, (-value if descending else value) if value is not None else 0
        ranks['models'][pool] = {'your': models[pool],
            'quality': sorted(current, key=lambda key: measured(key, qualities, True)),
            'cheapest': sorted(current, key=lambda key: measured(key, prices)),
            'name': sorted(current, key=lambda key: _name(by_model.get(key), key).casefold())}
    sorts = {'pools': saved['sort']['pools'],
             'models': {pool: saved['sort']['models'].get(pool, 'your') for pool in by_pool}}
    flat = {pool: sorts['models'][pool] != 'your' or models[pool] != [o['model'] for o in item.get('options') or []]
            for pool, item in by_pool.items()}
    return {'pools': pools, 'models': models, 'sort': sorts, 'ranks': ranks, 'flat': flat}


def _brief(overview: dict[str, Any], verbose: bool = False) -> str:
    return fleetctl.render_brief(overview, verbose=verbose, **overview.get('_brief_context', {}))


HANDLE = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5h.01M16 5h.01M8 12h.01M16 12h.01M8 19h.01M16 19h.01"/></svg>'


def _handle(label: str) -> str:
    return (f'<button class="order-handle" type="button" aria-label="Reorder {html.escape(label)}" '
            f'title="Drag, or use Arrow Up/Down then Enter to save or Escape to cancel">{HANDLE}</button>')


def _sort_control(kind: str, selected: str, pool: str = '') -> str:
    choices = POOL_SORTS if kind == 'pools' else MODEL_SORTS
    options = ''.join(f'<option value="{key}"{" selected" if key == selected else ""}>{label}</option>'
                      for key, label in choices.items())
    return (f'<label class="sort-control">Sort <select data-sort="{kind}" data-pool="{html.escape(pool)}" '
            f'aria-label="Sort {"providers" if kind == "pools" else "models for " + html.escape(pool)}">{options}</select></label>')


def _asset_version(name: str) -> str:
    try:
        return hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


# The product drawing, footer and theme toggle sit beside the stylesheet and are read once at import.
FOOTER = (ASSETS / "footer.html").read_text(encoding="utf-8")
MARK = (ASSETS / "crossfeed.svg").read_text(encoding="utf-8")
TAB_ICON = "data:image/svg+xml," + urllib.parse.quote((ASSETS / "tab-icon.svg").read_text(encoding="utf-8"), safe="")
THEME_TOGGLE = (ASSETS / "theme-toggle.html").read_text(encoding="utf-8")


LENS = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-linecap="round">'
    '<circle cx="10.6" cy="10.6" r="5.4" stroke-width="1.5"/>'
    '<path d="M14.6 14.6 19.2 19.2" stroke-width="1.6"/></g></svg>'
)
CHEVRON = '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 9.5 5 5 5-5"/></svg>'
# Drawn in the lens's stroke, so every icon on the page is one family.
OUT = ('<svg class="out" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6.5h8.5V15M17.2 6.8 7 17"/></svg>')
REFRESH = ('<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M18.6 13.2a6.8 6.8 0 1 1-1.9-6.1"/>'
           '<path d="M17.4 3.6v3.9h-3.9"/></svg>')
COPY = ('<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><g class="i-copy"><rect x="8.6" y="8.6" width="10.4" '
        'height="10.4" rx="2.2"/><path d="M15.4 8.6V7a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v6.4a2 2 0 0 0 2 2h1.6"/></g>'
        '<path class="i-done" d="m6 12.4 4 4 8-8.6"/></svg>')
# The three typefaces the first screen is set in: asked for with the page, not after the CSS.
PRELOAD_FONTS = ("commissioner.woff2", "literata.woff2", "crossfeedreadserif.woff2")

# "Crossfeed" is the name, "Orchestrator" what it is: two spans, set in two voices (weight and colour).
_BN, _, _BD = PRODUCT_NAME.partition(" ")
BRAND_WORDS = f'<span class="bn">{html.escape(_BN)}</span> <span class="bd">{html.escape(_BD)}</span>'


def _masthead() -> str:
    """The Castor product header: everything stands on one line that is exactly the column.

    It is fixed to the screen and every part rides up with the page, 1:1, until the air above the
    name is halved; then it holds, the name at its own size (console.css, "v2.8 header").
    """
    name = html.escape(PRODUCT_NAME)
    return (
        '<header class="mast" data-line><i class="here" aria-hidden="true"></i><span class="brand">'
        f'<a class="product-mark" href="/" aria-label="{name} console">{MARK}</a>'
        f'<a class="compact-brand" href="/">{BRAND_WORDS}<span class="star" aria-hidden="true">*</span></a></span>'
        '<nav class="nav" aria-label="Main"><a href="/" aria-current="page"><span class="tl" data-t="Providers">Providers</span></a></nav>'
        '<span class="bar-tools"><span class="find">'
        '<button class="find-lens" type="button" aria-label="Search everything  /" title="Search everything  /" '
        'aria-keyshortcuts="/ Meta+K Control+K" aria-expanded="false" aria-controls="find">'
        f'{LENS}</button></span>{THEME_TOGGLE}</span>'
        '<div class="find-panel" id="find" hidden><label for="global-search-input">Search everything</label>'
        '<span class="find-field"><input id="global-search-input" type="search" autocomplete="off" '
        'placeholder="Find a provider or model" aria-controls="search-results" aria-keyshortcuts="Meta+K Control+K">'
        '<kbd class="key-hint" aria-hidden="true">⌘K</kbd></span>'
        '<p class="search-status" role="status"></p><ul id="search-results"></ul></div></header>'
    )


def shell(title: str, body: str, front: bool = False) -> str:
    """A page. `front` is the console's front page, where the name starts as large as its row allows."""
    fonts = "".join(f'<link rel="preload" href="/static/fonts/{font}" as="font" type="font/woff2" crossorigin>'
                    for font in PRELOAD_FONTS)
    front_class = ' class="front"' if front else ""
    return keep_html(
        f'<!doctype html><html lang="en"{front_class}><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{html.escape(title)} · {html.escape(PRODUCT_NAME)}</title>{fonts}"
        f'<link rel="icon" type="image/svg+xml" href="{TAB_ICON}">'  # inline: no favicon.ico request
        f'<link rel="stylesheet" href="/static/console.css?v={_asset_version("console.css")}">'
        f'<script src="/static/console.js?v={_asset_version("console.js")}" defer></script>'
        f'<script src="/static/keep.js?v={_asset_version("keep.js")}" defer></script></head>'
        f'<body><div class="wrap">{_masthead()}<main>{body}</main>{FOOTER}</div></body></html>'
    )


def _ago(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 90:
        return "just now"
    return f"{fleetctl.format_duration(seconds)} ago"


def _meter(used: int, hot: bool) -> str:
    """A thin bar. Its width is an attribute, not a style: the page's policy allows no inline styles."""
    used = max(0, min(100, int(used)))
    return (f'<svg class="bar{" hot" if hot else ""}" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true">'
            f'<rect class="track" width="100" height="4" rx="2"/><rect class="fill" width="{used}" height="4" rx="2"/></svg>')


# ---- the limits -----------------------------------------------------------------------------------
# The console shows every limit the quota source can report, not just the five-hour or the weekly one, each in a
# very minimal way with its reset. One short row per limit the quota source reports (fleetctl.pool_limits, the
# list `brief` gives agents too): what it counts, a thin bar, how much is used, and when it resets in plain
# words. A limit that is nearly spent turns terracotta; one that will reset with allowance to spare says so
# in olive.
_ID_NOISE = {"weekly", "week", "monthly", "month", "daily", "day", "5h", "scoped", "quota", "summary", "limit", "window"}


def _scope_words(scope: str | None, pool: str) -> str | None:
    """A limit's scope as a person reads it. The source's own label ("Fable only") stands as written; a bare id
    it sent without one ("claude-weekly-scoped-fable") loses the provider's name and the length words, which
    the row already says, and keeps the rest ("Fable")."""
    if not scope or not re.fullmatch(r"[a-z0-9._-]+", scope):
        return scope
    parts = [part for part in scope.split("-") if part not in _ID_NOISE and part not in pool.split("-")]
    return _pretty("-".join(parts)) if parts else None


def _limit_row(limit: dict[str, Any], pool: str = "") -> str:
    limit = {**limit, "scope": _scope_words(limit.get("scope"), pool)}
    title = html.escape(limit["title"])
    scope = f'<small>{html.escape(limit["scope"])}</small>' if limit.get("scope") else ""
    if limit["kind"] == "budget":
        spent, cap = limit["spent_usd"], limit["cap_usd"]
        full = limit["state"] == "full"
        value = f'<b class="num">${spent:.2f}</b><span class="of"> of ${cap:.2f}</span>'
        when = "spent, back at midnight" if full else "real money, resets at midnight"
        label = f'{title}: ${spent:.2f} of ${cap:.2f}, {when}'
        return (f'<li class="lim{" full" if full else ""}" aria-label="{html.escape(label)}"><span class="ln">{title}</span>'
                f'{_meter(limit["used_percent"], full)}<span class="pc">{value}</span>'
                f'<span class="rs" aria-hidden="true">{when}</span></li>')
    used = int(limit["used_percent"])
    hot = used >= 75
    if limit["reset_at"] is None:
        when = "not started yet"
    else:
        when = f'resets {html.escape(limit["resets"])}'
    use = limit.get("spend_down")
    if use:
        when += ' <span class="use">· spare, use it</span>'
    classes = "lim" + (" hot" if hot else "") + (" full" if used >= 100 else "")
    text = f'{limit["title"]}{", " + limit["scope"] if limit.get("scope") else ""}: {used}% used, ' + (
        "not started yet" if limit["reset_at"] is None else f'resets {limit["resets"]}') + (
        ", with allowance to spare" if use else "")
    return (f'<li class="{classes}" aria-label="{html.escape(text)}"><span class="ln">{title}{scope}</span>'
            f'{_meter(used, hot)}<span class="pc"><b class="num">{used}%</b></span>'
            f'<span class="rs" aria-hidden="true">{when}</span></li>')


def _gauge(pool: dict[str, Any]) -> str:
    """Every limit of the pool's plan, one row each, or why there is none to show."""
    state = pool["state"]
    limits = pool.get("limits") or []
    exhausted = ""
    if state == "EXHAUSTED" and pool.get("until"):
        until = fleetctl.reset_words(pool["until"])
        exhausted = f'<p class="q hot"><b>Used up</b>, back {html.escape(until)}</p>'
        if not limits:
            return exhausted
    if limits:
        renews = ""
        if pool.get("renews_at"):
            renews = f'<p class="q renews">Plan renews {html.escape(fleetctl.reset_words(pool["renews_at"]))}</p>'
        return f'{exhausted}<ul class="limits" aria-label="Limits">{"".join(_limit_row(l, pool["pool"]) for l in limits)}</ul>{renews}'
    if pool["plan"].get("limit") == "none-known":
        words = 'Chat usage: no published cap' if pool.get('pro_usage') else 'no known limit'
        return f'<p class="q quiet">{words}</p>'
    if pool.get("stale_age_s") is not None:
        age = html.escape(fleetctl.format_duration(pool["stale_age_s"]))
        return (f'<p class="q quiet">The last reading is {age} old, too old to trust, so '
                "Crossfeed treats this plan as full until it reads it again.</p>")
    if pool.get("measured"):
        return '<p class="q quiet">No reading yet, so Crossfeed treats this plan as full.</p>'
    return '<p class="q quiet">This plan reports no limits Crossfeed can read.</p>'


def _pro_usage(pool: dict[str, Any]) -> str:
    meter = pool.get("pro_usage")
    if not meter:
        return ""
    if meter.get("unavailable"):
        words = fleetctl.chatgpt_pro.text(meter)
        return f'<p class="q quiet">{html.escape(words)}</p>'
    words = f"Pro-thinking uses: ~{meter['used']} of {meter['allowance']} in last 7 days (local estimate)"
    detail = (f"{meter['requests']} answered requests + {meter['wakes']} observed wakes. "
              "Crossfeed use only; your own chats are not counted. "
              "The allowance is configured locally. Pro fallback: Extra High, then High.")
    return (f'<p class="q {"hot" if meter["spent"] else "quiet"}">{html.escape(words)}</p>'
            f'<details class="pro-note"><summary>How Pro is estimated</summary><p class="q">{html.escape(detail)}</p></details>')


def _plan_lines(pool: dict[str, Any]) -> str:
    plan = pool["plan"]
    words: list[str] = []
    if plan.get("name") and plan["name"] != pool["label"]:
        words.append(html.escape(plan["name"]))
    price = fleetctl.format_price(plan)
    billing = plan.get("billing")
    if price == "free":
        words.append("free")
    elif price:
        words.append(f'<span class="num">{html.escape(price)}</span> a month' if billing == "subscription"
                     else f'<span class="num">{html.escape(price)}</span>')
    if billing and billing != "free" and price != "free":
        words.append(fleetctl.BILLING_WORDS[billing])
    line = " · ".join(words) if words else '<span class="quiet">Plan not set</span>'
    out = f'<p class="plan">{line}</p>'
    if plan.get("allowance"):
        # An estimate, said once in words: "About 200 AI credits a month" (the brief's agents read "≈").
        allowance = re.sub(r"^(≈\s*|about\s+|around\s+|roughly\s+)", "", str(plan["allowance"]).strip(), flags=re.I)
        out += f'<p class="allow">About {html.escape(allowance)}</p>'
    return out


# What each spend level means, said to a person (fleetctl.LEVEL_MEANING stays the agents' shorter line).
LEVEL_PLAIN = {
    "off": "Never used: every run that asks for it is refused.",
    "low": "Used sparingly: one task at a time, cheapest model first, the big model only for a single answer.",
    "normal": "Used as its limits allow: freely while there is room, less as they run low.",
    "high": "Used freely: strong models and several tasks at once, until its limits are nearly spent.",
    "forced": "Uses this provider despite quota estimates. Local budgets and actual refusals still apply.",
}


def _level_meaning(level: str) -> str:
    return LEVEL_PLAIN[level] + " Ignore estimates still obeys actual limits."


def _slider(pool: dict[str, Any], form_token: str) -> str:
    current = pool["level"]
    at = LEVELS.index(current)
    label = html.escape(pool["label"])
    stops = []
    for level in LEVELS:
        word = LEVEL_WORDS[level]
        meaning = html.escape(_level_meaning(level))
        if level == current:
            stops.append(
                f'<button type="submit" name="level" value="{level}" class="stop on {level}" role="radio" aria-checked="true" '
                f'aria-label="{word}" title="{meaning}"><i></i><span>{word}</span></button>'
            )
        else:
            stops.append(
                f'<button type="submit" name="level" value="{level}" class="stop {level}" role="radio" aria-checked="false" '
                f'aria-label="{word}" title="{meaning}"><i></i><span>{word}</span></button>'
            )
    pool_id = html.escape(pool["pool"])
    return (
        '<p class="lvh" aria-hidden="true">How agents use this plan</p>'
        f'<form class="lv at-{at} is-{current}" method="post" action="/level" role="radiogroup" '
        f'aria-label="How much of {label} to use" aria-describedby="means-{pool_id}">'
        f'<input type="hidden" name="pool" value="{pool_id}"><input type="hidden" name="t" value="{form_token}">'
        f'<span class="rail" aria-hidden="true"><span class="fill"></span></span>{"".join(stops)}</form>'
        f'<p class="means" id="means-{pool_id}">{html.escape(_level_meaning(current))}</p>'
    )


def _display_value(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{key.replace('_', ' ')}: {_display_value(item)}" for key, item in value.items())
    if isinstance(value, list):
        return ", ".join(_display_value(item) for item in value)
    return str(value)


def _model_id(provider: str, model: str) -> str:
    return "model-" + hashlib.sha256(f"{provider}:{model}".encode()).hexdigest()[:16]


# ---- the model switches -------------------------------------------------------------------------
# One list per provider: the line says what the switches add up to (all on, some, one, none), the
# current models are listed with one line each on what they are best for and a switch that says On
# or Off, older models folded away. The mark follows the page's two colours: teal is the machine
# deciding (everything on), terracotta is the human's hand (something switched off, as on Forced).
NAME_WORDS = {
    "gpt": "GPT", "glm": "GLM", "deepseek": "DeepSeek", "minimax": "MiniMax", "mimo": "MiMo",
    "qwen": "Qwen", "kimi": "Kimi", "grok": "Grok", "gemini": "Gemini", "claude": "Claude", "oss": "OSS",
    "openrouter": "OpenRouter", "github": "GitHub", "copilot": "Copilot",
}
ROLE_WORDS = {
    "default": "everyday tasks", "hard-reasoning": "hard reasoning", "implementation": "building",
    "review": "reviews", "debug": "debugging", "repo-map": "repo maps", "long-context": "long documents",
    "frontend-visual": "front-end and visual work", "audio-video": "audio and video",
    "research-scout": "web research",
}


def _pretty(key: str) -> str:
    """deepseek-v4-flash -> DeepSeek V4 Flash; used when the roster has no card with a name."""
    words: list[str] = []
    for part in key.split("-"):
        low = part.casefold()
        if part.isdigit() and words and re.fullmatch(r"\d+(\.\d+)*", words[-1]):
            words[-1] += "." + part   # claude-opus-4-6 -> Claude Opus 4.6
        elif words == ["GPT"] and part[:1].isdigit():
            words[-1] += "-" + part   # gpt-6-sol -> GPT-6 Sol, as OpenAI writes it
        elif low in NAME_WORDS:
            words.append(NAME_WORDS[low])
        elif re.fullmatch(r"[a-z]\d.*", low):
            words.append(part[0].upper() + part[1:])
        elif re.match(r"qwen\d", low):
            words.append("Qwen " + part[4:])
        elif low.isalpha():
            words.append(part.capitalize())
        else:
            words.append(part)
    return " ".join(words)


def _name(model: dict[str, Any] | None, key: str | None = None) -> str:
    for lane in (model or {}).get("lanes") or []:
        if lane.get("harness") == "chatgpt-chat" and lane.get("worker_row") and lane.get("worker_level"):
            level = {"instant": "Instant", "medium": "Medium", "high": "High",
                     "xhigh": "Extra High", "pro": "Pro"}.get(lane["worker_level"], _pretty(lane["worker_level"]))
            count = lane.get("max_parallel", 1)
            return f"ChatGPT picker: {lane['worker_row']} · Thinking: {level} · Up to {count} {'task' if count == 1 else 'tasks'} at once"
    card = (model or {}).get("card") or {}
    return str(card.get("name") or _pretty((model or {}).get("model") or key or ""))


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _first_sentence(text: str, limit: int = 160) -> str:
    """The roster's own words, cut to one sentence that fits a line."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    head = re.split(r"(?<=\.)\s", text, maxsplit=1)[0].rstrip(".;: ")
    # A note written in capitals is not shouted here: three or more capital words in a row read as words.
    head = re.sub(r"\b[A-Z]{2,}(?:[\s,;:]+[A-Z]+\b){2,}", lambda m: m.group(0).lower(), head)
    head = head[:1].upper() + head[1:]
    head = head if len(head) <= limit else head[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return head[:1].upper() + head[1:]


def _best_for(model: dict[str, Any], pool: str) -> str:
    card = model.get("card") or {}
    if card.get("best_for"):
        return str(card["best_for"])
    places = (model.get("role_ranks") or {}).get(pool) or {}
    first = [ROLE_WORDS.get(role, role.replace("-", " ")) for role, rank in places.items() if rank == 1]
    if first:
        return "Picked first for " + _join(first[:3])
    if places:
        roles = sorted(places, key=lambda role: places[role])
        return "Also picked for " + _join([ROLE_WORDS.get(r, r.replace("-", " ")) for r in roles[:3]])
    for lane in model.get("lanes") or []:
        if lane.get("reason"):
            return _first_sentence(lane["reason"])
        if lane.get("roles"):
            return "Listed for " + _join([ROLE_WORDS.get(r, r.replace("-", " ")) for r in lane["roles"][:3]])
    evidence = model.get("evidence") or {}
    for lane in model.get("lanes") or []:
        if lane.get("notes"):
            return _first_sentence(lane["notes"])
    if evidence.get("note"):
        return _first_sentence(evidence["note"])
    return "No notes in the roster yet"


def _day(value: Any) -> str:
    """"29 September" from "2026-09-29"; the text itself when it is not a date."""
    try:
        day = fleetctl.dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{day.day} {day:%B}"


def _older_reason(model: dict[str, Any], option: dict[str, Any] | None = None,
                  by_key: dict[str, dict[str, Any]] | None = None) -> str:
    """The line under an older model: what replaced it and when, and whether it still leads anywhere."""
    card = model.get("card") or {}
    option = option or {}
    if option.get("retired_on"):
        return f"Withdrawn by its maker on {_day(option['retired_on'])}, so it can no longer run"
    successor = option.get("superseded_by")
    if successor:
        text = f"Replaced by {_name((by_key or {}).get(successor), successor)}"
        if card.get("superseded_on"):
            text += f" on {_day(card['superseded_on'])}"
        leads = [item for item in card.get("better_than_successor_at") or [] if isinstance(item, dict)]
        if leads:
            text += f"; still ahead on {len(leads)} measure{'s' if len(leads) != 1 else ''}, see Details"
        return text
    if card.get("best_for"):
        return str(card["best_for"])
    for lane in model.get("lanes") or []:
        if lane.get("reason"):
            return _first_sentence(lane["reason"])
        status = lane.get("admission_status")
        if status and status != "active":
            return "Not cleared for routing yet"
    if option.get("run_as"):
        return "Runs only when a task asks for it by name"
    return "Crossfeed has no way to run it"


# ---- a page about each model, and where an older one is still better -----------------------------------
# Clicking a model opens a page about it, and where an older model is still better at something than the one that
# replaced it (only when true, according to Artificial Analysis and other benchmarks) the page says so. Both come
# from the roster's cards (page_url, better_than_successor_at: each with both numbers and a source), researched
# and dated; with no card field the page shows nothing, never a guess.
SOURCE_NAMES = {"artificialanalysis.ai": "Artificial Analysis", "tbench.ai": "Terminal-Bench",
                "openai.com": "OpenAI", "anthropic.com": "Anthropic", "blog.google": "Google", "ai.google.dev": "Google"}
MARGIN_WORDS = {"clear": "clear lead", "small": "small lead"}


def _safe_url(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") and not any(c in value for c in " <>\"'") else None


def _source_name(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    host = host[4:] if host.startswith("www.") else host
    return SOURCE_NAMES.get(host, host)


def _page_link(model: dict[str, Any], name: str) -> str:
    """The model's name, as a link to the page about it when the roster has one."""
    url = _safe_url((model.get("card") or {}).get("page_url"))
    if not url:
        return f'<span class="nm">{html.escape(name)}</span>'
    return (f'<a class="nm" href="{html.escape(url)}" target="_blank" rel="noopener noreferrer" '
            f'title="{html.escape(name)} on {html.escape(_source_name(url))}">{html.escape(name)}{OUT}'
            f'<span class="vh"> (opens {html.escape(_source_name(url))} in a new tab)</span></a>')


def _leads(model: dict[str, Any], option: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> str:
    card = model.get("card") or {}
    items = [item for item in card.get("better_than_successor_at") or []
             if isinstance(item, dict) and item.get("capability") and item.get("this") and item.get("successor")]
    successor = option.get("superseded_by") or card.get("superseded_by")
    if not items or not successor:
        return ""
    other = _name(by_key.get(successor), successor)
    rows = []
    for item in items:
        margin = MARGIN_WORDS.get(str(item.get("margin")), "")
        rows.append(f'<li><span class="cap">{html.escape(str(item["capability"]))}</span><span class="val">'
                    f'<b class="num">{html.escape(str(item["this"]))}</b> vs <span class="num">'
                    f'{html.escape(str(item["successor"]))}</span>{f" · {margin}" if margin else ""}</span></li>')
    sources: dict[str, str] = {}
    for item in items:
        url = _safe_url(item.get("source"))
        if url:
            sources.setdefault(url, str(item.get("read") or ""))
    cited = ", ".join(f'<a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">'
                      f'{html.escape(_source_name(url))}</a>' + (f" (read {html.escape(_day(read))})" if read else "")
                      for url, read in sources.items())
    note = f'<p class="src">Source: {cited}.</p>' if cited else ""
    check = card.get("cross_check")
    if isinstance(check, str) and check.strip():
        note += f'<p class="src">Checked elsewhere: {html.escape(check.strip())}</p>'
    return (f'<div class="leads"><p class="lh">Still better than {html.escape(other)} at</p>'
            f'<ul>{"".join(rows)}</ul>{note}</div>')


def _allowance(pool: dict[str, Any], model: dict[str, Any]) -> str:
    card = model.get("card") or {}
    if card.get("allowance"):
        return str(card["allowance"])
    plan = pool["plan"]
    billing = plan.get("billing")
    name = plan.get("name") or pool["label"]
    if billing == "per-token":
        cap = (pool.get("metered") or {}).get("daily_usd_cap")
        capped = f", capped at ${cap:.2f} a day" if cap else ""
        return f"real money per token ({name}{capped})"
    if billing == "free" or fleetctl.format_price(plan) == "free":
        return f"nothing: {name} is free"
    price = fleetctl.format_price(plan)
    detail = [name] if name != pool["label"] else []
    if price and billing == "subscription":
        detail.append(f"{price} a month")
    return f"the {pool['label']} plan's allowance" + (f" ({', '.join(detail)})" if detail else "")


def _facts(model: dict[str, Any], pool: dict[str, Any], older: bool, option: dict[str, Any] | None = None,
           by_key: dict[str, dict[str, Any]] | None = None) -> str:
    """At most three plain bullets, then everything else the roster knows under More."""
    card = model.get("card") or {}
    # What it is best at is already the line under its name; these are the other two facts.
    bullets = []
    if card.get("speed"):
        bullets.append(("Speed", str(card["speed"])))
    bullets.append(("Spends", _allowance(pool, model)))
    if card.get("access"):
        bullets.append(("Access", str(card["access"])))
    rows = [f'<dt>Model id</dt><dd>{html.escape(str(card.get("run_as") or model["model"]))}</dd>']
    for lane in model["lanes"]:
        for field, value in lane.items():
            if value is not None and value != [] and value != "":
                rows.append(f'<dt>{html.escape(field.replace("_", " ").capitalize())}</dt>'
                            f'<dd>{html.escape(_display_value(value))}</dd>')
    if not model["lanes"]:
        how = "none; a dispatch asks for it by name" if pool.get("direct") else "none; evidence only"
        rows.append(f'<dt>Routing lane</dt><dd>{how}</dd>')
    for field, value in model["evidence"].items():
        rows.append(f'<dt>Evidence: {html.escape(field.replace("_", " "))}</dt>'
                    f'<dd>{html.escape(_display_value(value))}</dd>')
    about = f'<span class="vh"> about {html.escape(_name(model))}</span>'   # many "Details" links need their own names
    return (
        f'<details class="facts"><summary>Details{about}</summary>'
        '<ul class="bullets">' + "".join(f'<li><b>{k}</b> {html.escape(v)}</li>' for k, v in bullets[:3]) + '</ul>'
        + _leads(model, option or {}, by_key or {}) +
        f'<details class="more"><summary>More{about}</summary><dl>{"".join(rows)}</dl></details></details>'
    )


def _usual_name(pool: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> str | None:
    usual = pool.get("usual")
    if not usual:
        return None
    if usual in by_key:
        return _name(by_key[usual])
    for model in by_key.values():   # an alias such as "opus" names a card's model
        card = model.get("card") or {}
        if pool["pool"] in model["pools"] and (card.get("run_as") == usual or usual in (card.get("aliases") or [])):
            return _name(model)
    return _pretty(usual)


# The words under a provider's models, one set per state the switches can be in. The label is
# built from the state and the names (console.js builds the same words, so a click reads the same
# before and after the server answers); the notes are the server's, and travel with the page.
def _models_view(state: str, on: list[dict[str, Any]], total: int, notes: dict[str, str]) -> tuple[str, str, str]:
    """(state, label, note) as the line under the provider says it. `on` is [{name}]."""
    if state == "empty":
        return "auto", "Crossfeed decides", notes["empty"]
    if state == "only":
        return "only", on[0]["name"], notes["only"]
    if state == "all":
        return "all", "All enabled", notes["all"]
    if state == "none":
        return "none", "None on", notes["none"]
    if state == "one":
        return "one", f"Only {on[0]['name']} on", notes["one"]
    return "some", f"{len(on)} of {total} on", notes["some"]


def models_notes(pool: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> dict[str, str]:
    direct, label = bool(pool.get("direct")), pool["label"]
    usual = _usual_name(pool, by_key)
    unavailable = (pool.get("models") or {}).get("unavailable")
    if unavailable:
        every = f"{_name(by_key.get(unavailable), unavailable)} is no longer offered, so every model is on"
    elif direct:
        every = f"Default when a task names no model: {usual}" if usual else "each task names the model it needs"
    else:
        every = f"Crossfeed picks the best for each task, usually {usual}" if usual else "Crossfeed picks the best for each task"
    if any(lane.get("worker_row") for model in by_key.values() if pool["pool"] in model.get("pools", [])
           for lane in model.get("lanes", [])):
        every = "Crossfeed picks among the saved ChatGPT configurations for each task"
    return {
        "empty": "the roster lists no models here yet",
        "only": "the only model this plan offers",
        "all": every,
        "some": ("a task that asks for an off model gets the nearest one that is on" if direct
                 else "Crossfeed picks the best of them for each task"),
        "one": "every run on this provider uses it",
        "none": f"{label} is off until you switch one on",
    }


def models_summary(pool: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> tuple[str, str, str]:
    """(state, label, note) for the line under the provider. Older models are not counted."""
    models = pool.get("models") or {}
    current = models.get("current", [])
    on = [{"name": _name(by_key.get(key), key)} for key in models.get("on", []) if key in current]
    return _models_view(models.get("state", "empty"), on, len(current), models_notes(pool, by_key))


def _summary_html(pool: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> str:
    state, label, note = models_summary(pool, by_key)
    return (f'<i class="mark" aria-hidden="true"></i><b class="v">{html.escape(label)}</b>'
            f'<span class="n">{html.escape(note)}</span>')


def _option(model: dict[str, Any], pool: dict[str, Any], option: dict[str, Any],
            by_key: dict[str, dict[str, Any]] | None = None) -> str:
    key, name = model["model"], _name(model)
    target = _model_id(pool["pool"], key)
    older = not option["current"]
    line = _older_reason(model, option, by_key) if older else _best_for(model, pool["pool"])
    if any(lane.get("catalog_state") == "sleeping" for lane in model.get("lanes", [])):
        line = "Sleeping; wakes automatically when selected" + (". " + line if line else "")
    if older and option.get("run_as"):
        line += "; " + (option.get("retention_reason") or "Off: no recorded advantage over a current model")
    for lane in model.get("lanes", []):
        if lane.get("harness") == "chatgpt-chat" and lane.get("worker_row"):
            count = lane.get("max_parallel", 1)
            original = line
            line = f"{count} saved {'chat' if count == 1 else 'chats'}. Text only."
            if older:
                line += " " + original
            elif lane.get("catalog_state") == "sleeping":
                line += " Wakes automatically."
            break
    on = option["on"] is True
    blocked = older and not option.get("retention_reason")
    if option["run_as"]:
        # A switch: pressing it sends the state it would set, so two tabs cannot undo each other.
        control = (f'<button class="sw" type="submit" role="switch" aria-checked="{"true" if on else "false"}" '
                   f'{"disabled " if blocked else ""}name="switch" value="{html.escape(key)}={"off" if on else "on"}" data-key="{html.escape(key)}" '
                   f'data-name="{html.escape(name)}" aria-label="Use {html.escape(name)}">'
                   f'<span class="track" aria-hidden="true"><i></i></span><span class="st">{"On" if on else "Off"}</span></button>')
    elif option.get("retired_on"):
        control = '<span class="na">Withdrawn</span>'   # its maker took it away: no switch could run it
    else:
        control = '<span class="na">Unavailable</span>'   # the router has no lane for it: nothing a switch could do
    state = "" if not option["run_as"] else (" on" if on else " off")
    older_label = '<span class="older-label">Older model</span>' if older else ''
    active_badge = active_note = ''
    if older and option["run_as"] and not blocked:
        hidden = '' if on else ' hidden'
        active_badge = f'<span class="older-active"{hidden}>active</span>'
        active_note = f'<span class="older-active-note"{hidden}>older, still used where it leads</span>'
    return (f'<li class="opt{" older" if older else ""}{state}" id="{target}" data-search-label="{html.escape(name)}" '
            f'data-model="{html.escape(key)}" tabindex="-1"><div class="row">{_handle(name)}<div class="txt">'
            f'{_page_link(model, name)}{older_label}{active_badge}<span class="bf">{html.escape(line)}</span>'
            f'{active_note}</div>{control}</div>'
            f'{_facts(model, pool, older, option, by_key)}</li>')


def _picker(pool: dict[str, Any], by_key: dict[str, dict[str, Any]], form_token: str,
            order: dict[str, Any] | None = None) -> str:
    pool_id = html.escape(pool["pool"])
    label = html.escape(pool["label"])
    options = pool.get("options") or []
    if order:
        ranking = order['ranks']['models'][pool['pool']][order['sort']['models'][pool['pool']]]
        options = sorted(options, key=lambda option: ranking.index(option['model']))
    runnable = [o for o in options if o["run_as"]]
    models = pool.get("models") or {}
    state, _, _ = models_summary(pool, by_key)
    current = [o for o in options if o["current"]]
    older = [o for o in options if not o["current"]]
    listing = f'<ul class="opts">{"".join(_option(by_key[o["model"]], pool, o, by_key) for o in current)}</ul>' if current else ""
    if not options:
        listing = ('<p class="empty">The roster lists no models for this provider yet, so there is nothing to '
                   'switch here. Add them to the roster to pick one.</p>')
    if older:
        switches = [o for o in older if o["run_as"]]
        on_older = sum(1 for o in switches if o["on"])
        some_off = bool(switches) and on_older < len(switches)
        rule = ('<p class="scope">An older model runs only when a task asks for it by name. It never stands in '
                'for a model that is off. A recorded advantage over a current model is required to switch it on.</p>' if switches and pool.get("direct") else "")
        hidden = '' if on_older else ' hidden'
        active_summary = (f'<span class="older-active older-active-summary"{hidden}>active</span>'
                          f'<span class="older-active-note older-active-summary-note"{hidden}>'
                          'older, still used where it leads</span>') if switches else ''
        listing += (f'<details class="older"><summary>Older models <span class="count">{len(older)}'
                    f'{f" · {on_older} on" if some_off else ""}</span>{active_summary}</summary>{rule}'
                    f'<ul class="opts">{"".join(_option(by_key[o["model"]], pool, o, by_key) for o in older)}</ul></details>')
    if order and order['flat'][pool['pool']]:
        listing = f'<ul class="opts">{"".join(_option(by_key[o["model"]], pool, o, by_key) for o in options)}</ul>'
    # One short line at most: the line under "Models" already says what the switches add up to.
    if runnable and pool.get("direct"):
        hint = f"These switches steer Crossfeed's agents. The {label} app on your own screen keeps its own model picker."
    elif len(runnable) == 1:
        hint = f"Switch it off and Crossfeed leaves {label} out."
    else:
        hint = ""
    chat_setups = any(lane.get("harness") == "chatgpt-chat" and lane.get("worker_row")
                      for option in options for lane in by_key[option["model"]].get("lanes", []))
    if chat_setups:
        hint = "Row labels come from ChatGPT’s picker; they do not confirm the underlying model. Each saved chat lets one task run at a time."
    choice_label = "Choose chat setups" if chat_setups else "Choose models"
    runnable_now = [o for o in runnable if o["current"]]
    all_on = (f'<button class="all-on" type="submit" name="model" value="auto"'
              f'{"" if any(not o["on"] for o in runnable_now) else " hidden"}>Switch all on</button>'
              if len(runnable_now) > 1 else "")
    notes = models_notes(pool, by_key)
    note_attrs = " ".join(f'data-note-{name}="{html.escape(text)}"' for name, text in notes.items())
    return (
        f'<details class="pick" id="pick-{pool_id}" data-pool="{pool_id}" {note_attrs}>'
        f'<summary class="pick-line"><span class="k">{choice_label}</span><span class="now {state}">{_summary_html(pool, by_key)}</span>'
        f'{CHEVRON}</summary><div class="picker">'
        + _sort_control('models', order['sort']['models'][pool['pool']] if order else 'your', pool['pool'])
        +
        f'<form class="choose" method="post" action="/model"><input type="hidden" name="pool" value="{pool_id}">'
        f'<input type="hidden" name="t" value="{html.escape(form_token)}">'
        + (f'<div class="head"><p class="scope">{hint}</p>{all_on}</div>' if hint or all_on else "")
        + f'{listing}</form></div></details>'
    )


def snapshot_payload(overview: dict[str, Any]) -> dict[str, Any]:
    by_key = {m["model"]: m for m in overview["models"]}
    pools = []
    for p in overview["pools"]:
        state, label, note = models_summary(p, by_key)
        pools.append({"pool": p["pool"], "level": p["level"], "means": _level_meaning(p["level"]),
                      "gauge": _gauge(p), "on": (p.get("models") or {}).get("on", []),
                      "state": state, "label": label, "note": note})
    return {"pools": pools, "brief": _brief(overview), "brief_full": _brief(overview, True),
            "console_order": overview.get('console_order') or display_order(overview), "stamp": _stamp(overview)}


SOURCE_WORDS = {"codexbar": "CodexBar"}


def _stamp(overview: dict[str, Any]) -> str:
    readings = overview["quota_readings"]
    if readings["measured_pools"]:
        sources = ", ".join(SOURCE_WORDS.get(source, source) for source in readings["sources"])
        return f'Limits read {_ago(readings["newest_age_s"])} from {sources}'
    return "No limits could be read (CodexBar is closed or signed out), so Crossfeed spends as if every plan had room"


# ---- model names written in files Crossfeed does not own (fleetctl.sync_pins) ----------------------------
# A switch only reaches runs that go through Crossfeed. A file that names a model (a Codex agent seat, an app's own
# default) runs it whatever the switch says, so the page says where that is so and what was done about it.
def _pins_note(pool: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> str:
    rows = pool.get("pins") or []

    def named(value: Any, model: Any = None) -> str:
        key = model if isinstance(model, str) and model in by_key else value
        return _name(by_key.get(key), str(value))

    notes = []
    moved: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        if row.get("action") == "held" and row.get("original"):
            moved.setdefault((str(row["original"]), str(row["value"])), []).append(row["file"])
    retired = (pool.get("models") or {}).get("retired") or {}
    for (was, now), files in moved.items():
        runs = f'{_join([html.escape(f) for f in files])} now {"run" if len(files) > 1 else "runs"} {html.escape(named(now))}'
        if was in retired or named(was) in {named(key) for key in retired}:
            notes.append(("moved", f'{runs}: {html.escape(named(was))} was withdrawn by its maker.'))
        else:
            notes.append(("moved", f'{runs} while {html.escape(named(was))} is off, and '
                                   f'{"get" if len(files) > 1 else "gets"} it back when you switch it on.'))
    for row in rows:
        if row.get("blocked") and row.get("action") == "off":
            what = f' ({html.escape(row["what"])})' if row.get("what") else ""
            gone = "withdrawn by its maker" if row["blocked"] == "retired" else "switched off"
            fix = ("Crossfeed does not edit this file, so change the model there." if not row.get("manage")
                   else "No model is on to take its place.")
            notes.append(("open", f'<span class="num">{html.escape(row["path"])}</span>{what} still names '
                                  f'{html.escape(named(row["value"], row.get("model")))}, which is {gone}. {fix}'))
    if not notes:
        return ""
    return ('<div class="pins">' + "".join(f'<p class="pin {kind}"><i aria-hidden="true"></i><span>{text}</span></p>'
                                         for kind, text in notes) + "</div>")


def _recent_runs(overview: dict[str, Any]) -> str:
    runs = overview.get("recent_runs") or []
    if not runs:
        return '<section class="agents"><h2>Recent runs</h2><p>No model receipts recorded yet.</p></section>'
    rows = []
    for run in runs:
        actual = run.get("actual_model")
        ran = (f"{actual} (provider reported)" if actual else
               f"{run.get('selected_model') or 'unknown'} (selected; underlying model unconfirmed)")
        values = [run.get("ended_at") or run.get("started_at") or "", run.get("harness") or "",
                  run.get("requested_model") or "automatic selection", ran,
                  str(run.get("returncode", ""))]
        rows.append("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in values) + "</tr>")
    return ('<section class="agents recent-runs"><h2>Recent runs</h2><div class="run-table"><table><thead><tr><th>Time (UTC)</th>'
            '<th>App</th><th>Asked for</th><th>Ran on</th><th>Exit</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')


def provider_setup(sources: list[dict[str, Any]], token: str) -> str:
    token = html.escape(token)
    names = {"openai-compatible": "OpenAI-compatible API", "openrouter": "OpenRouter, paid or free",
             "crossfeed-chat": "Crossfeed Chat", "codex": "Codex CLI", "claude": "Claude CLI",
             "agy": "Antigravity CLI", "opencode": "OpenCode CLI", "copilot": "Copilot CLI (Auto)"}
    choices = ''.join(f'<option value="{kind}">{names[kind]}</option>' for kind in providers.KINDS)
    rows = ''.join('<li><span>' + html.escape(source['label']) + ' · ' + html.escape(names[source['kind']]) + '</span>'
                   '<form class="provider-remove" method="post" action="/provider/remove">'
                   f'<input type="hidden" name="t" value="{token}">'
                   f'<input type="hidden" name="id" value="{html.escape(source["id"])}">'
                   '<button type="submit">Remove</button></form></li>' for source in sources)
    return ('<section class="provider-setup" aria-label="Provider setup"><details>'
            '<summary class="provider-open"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true">'
            '<path d="M12 5v14M5 12h14"/></svg>Add provider</summary>'
            '<div class="provider-layout"><div class="provider-intro"><h2>Connect a provider</h2>'
            '<p>Bring your models into Crossfeed. Choose a connection, check what is available, '
            'then pick the models you want.</p><p class="quiet">Checking finds models. API and CLI connections '
            'need a verified test run before Crossfeed can use them.</p></div>'
            '<form class="provider-form" method="post" action="/provider/add" autocomplete="off">'
            f'<input type="hidden" name="t" value="{token}">'
            '<label>Connection type<select name="kind" aria-describedby="provider-type-help">' + choices + '</select></label>'
            '<p id="provider-type-help" class="quiet">Connect any service with an OpenAI-compatible API, '
            'including local model servers.</p><div class="provider-fields">'
            '<label>Provider name<input name="label" maxlength="80" placeholder="My model server"></label>'
            '<label>Short ID<input name="id" required pattern="[a-z][a-z0-9-]{0,47}" placeholder="my-server" '
            'aria-describedby="provider-id-help"></label></div>'
            '<p id="provider-id-help" class="quiet">A unique name for settings: lowercase letters, numbers and hyphens.</p>'
            '<div data-provider-section="connection"><label>API base URL<input name="base_url" type="url" '
            'placeholder="https://api.example.com/v1" aria-describedby="provider-url-help"></label>'
            '<p id="provider-url-help" class="quiet">Use HTTPS, or HTTP for a server on this computer. '
            'Crossfeed Chat uses http://127.0.0.1:PORT/v1; OpenRouter uses https://openrouter.ai/api/v1. '
            'CLI connections use their own sign-in; leave URL and keys empty.</p>'
            '<label>Key reference (optional)<input name="credential_ref" spellcheck="false" '
            'placeholder="env:MY_API_KEY" aria-describedby="provider-key-help"></label>'
            '<p id="provider-key-help" class="quiet">An environment variable, an absolute key-file path, '
            'or op://vault-id/item/field. Use a reference rather than a secret here.</p>'
            '<label>Or paste an API key<input name="key" type="password" autocomplete="new-password" '
            'aria-describedby="provider-secret-help"></label>'
            '<p id="provider-secret-help" class="quiet">Stored in a private local file; never shown again.</p></div>'
            '<div class="provider-discovery" hidden><label>Find a model<input type="search" class="provider-model-filter" '
            'placeholder="Filter available models" aria-controls="provider-model-list"></label>'
            '<fieldset id="provider-model-list" class="provider-model-list"><legend>Available models</legend></fieldset>'
            '<p class="provider-model-count quiet" role="status"></p></div>'
            '<label>Selected model IDs<textarea name="models" rows="3" required '
            'aria-describedby="provider-model-help" spellcheck="false"></textarea></label>'
            '<p id="provider-model-help" class="quiet">Check the connection to fetch models, then choose up to 200. '
            'You can also enter IDs here, one per line. For CLIs use native IDs; Copilot uses auto.</p>'
            '<div data-provider-section="budget"><label>Daily estimated API budget (USD)<input name="daily_cap" '
            'type="number" min="0" max="100000" step="0.01" value="0" aria-describedby="provider-cap-help"></label>'
            '<p id="provider-cap-help" class="quiet">For compatible APIs and OpenRouter. Zero blocks calls. '
            'Set a billing limit at the provider too; this budget uses recorded estimates.</p></div>'
            '<div data-provider-section="relay"><label>Your acceptance of relay account risk<textarea name="acceptance" '
            'rows="2" aria-describedby="provider-risk-help"></textarea></label>'
            '<p id="provider-risk-help" class="quiet">For Crossfeed Chat only. Record your acceptance of '
            'the subscription-relay account risk before connecting it.</p></div>'
            '<div class="provider-actions"><button type="submit" formaction="/provider/probe" formnovalidate>'
            'Check connection</button><button class="provider-save" type="submit">Add provider</button></div>'
            '<p class="provider-result" role="status" aria-live="polite"></p></form></div></details>'
            + (f'<ul class="provider-sources" aria-label="Added providers">{rows}</ul>' if rows else '')
            + (('<p class="quiet">Removing a provider keeps its settings backup and key file for recovery.</p>') if rows else '')
            + '</section>')


def _pool_heading(label: str) -> str:
    # One inline formatting context keeps the translation guard's spans and their spaces together.
    title, separator, detail = label.partition(" via ")
    if not separator:
        title, separator, detail = label.partition(" · ")
    suffix = (f'<small>{"via " if separator == " via " else ""}{html.escape(detail)}</small>' if detail else '')
    return f'<span class="pool-title">{html.escape(title)}{" " if detail else ""}{suffix}</span>'


def render_page(overview: dict[str, Any], form_token: str) -> str:
    stamp = html.escape(_stamp(overview))
    by_key = {m["model"]: m for m in overview["models"]}
    rows = []
    order = overview.get('console_order') or display_order(overview)
    ranking = order['ranks']['pools'][order['sort']['pools']]
    for pool in sorted(overview["pools"], key=lambda p: ranking.index(p['pool'])):
        pool_id = html.escape(pool["pool"])
        rows.append(
            f'<article class="pool lvl-{pool["level"]}" id="pool-{pool_id}" data-pool="{pool_id}" data-search-label="{html.escape(pool["label"])}" tabindex="-1">'
            f'<div class="who"><h2>{_handle(pool["label"])}{_pool_heading(pool["label"])}</h2>{_plan_lines(pool)}</div>'
            f'<div class="gauge">{_gauge(pool)}{_pro_usage(pool)}</div>'
            f'<div class="ctl">{_slider(pool, form_token)}</div>'
            f'{_picker(pool, by_key, form_token, order)}{_pins_note(pool, by_key)}'
            "</article>"
        )
    body = (
        "<h1>Models and spend</h1>"
        '<p class="lede">Enable models for agents to choose from. Each provider shows its default or how Crossfeed picks. '
        "Slide left to conserve each plan’s quota, right to use more. Agents read these settings "
        "before they plan.</p>"
        f'<div class="quota-reading"><p class="stamp">{stamp}</p>'
        '<form class="quota-refresh" method="post" action="/refresh">'
        f'<input type="hidden" name="t" value="{html.escape(form_token)}">'
        f'<button type="submit" aria-label="Read the limits again now" title="Read the limits again now">{REFRESH}'
        '</button></form></div>'
        + provider_setup(overview.get('provider_sources', []), form_token)
        + _sort_control('pools', order['sort']['pools'])
        + '<p class="order-note">Your order changes this page only; Crossfeed still picks by evidence and quota.</p>'
        + f'<div class="order-state" hidden data-order="{html.escape(json.dumps(order))}" data-token="{html.escape(form_token)}"></div>'
        +
        f'<section class="pools" aria-label="Providers">{"".join(rows)}</section>'
        f'{_recent_runs(overview)}'
        '<p class="save-status" role="status" aria-live="polite"></p>'
        '<section class="agents" aria-labelledby="agents-h"><h2 class="sc" id="agents-h">What agents read</h2>'
        '<div class="brief"><button class="brief-toggle" type="button" aria-expanded="false" aria-controls="agent-brief">Show full</button>'
        '<button class="copy" type="button" aria-label="Copy what agents read">'
        f'{COPY}<span class="cw">Copy</span></button>'
        f'<pre id="agent-brief">{html.escape(_brief(overview))}</pre>'
        f'<pre class="brief-full" hidden>{html.escape(_brief(overview, True))}</pre></div></section>'
    )
    return shell("Providers", body, front=True)


def message_page(title: str, text: str) -> str:
    return shell(title, f"<h1>{html.escape(title)}</h1><p class=\"lede\">{html.escape(text)}</p>")


# ---- the server -----------------------------------------------------------------------------
def make_handler(console: Console) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "console"
        sys_version = ""
        # One connection carries the page, its assets and every click after it; HTTP/1.0 opened
        # a new one per request. Every response states its length, which keep-alive needs.
        protocol_version = "HTTP/1.1"
        timeout = 60   # an idle kept-alive connection gives its thread back after a minute

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            # Silent on purpose: the default log line carries the query string, and the
            # sign-in link's query string is the secret.
            return

        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            extra = dict(extra or {})
            self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _html(self, status: int, page: str, extra: dict[str, str] | None = None) -> None:
            self._send(status, page.encode("utf-8"), "text/html; charset=utf-8", extra)

        def _refuse(self, status: int, title: str, text: str) -> None:
            self._html(status, message_page(title, text))

        def _redirect(self, location: str, extra: dict[str, str] | None = None) -> None:
            headers = {"Location": location, **(extra or {})}
            self._send(303, b"", "text/plain; charset=utf-8", headers)

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            if not console.host_ok(self.headers):
                # Plain text: a rebinding attacker learns nothing, and nothing is rendered.
                self._send(421, b"This console only answers on 127.0.0.1.\n", "text/plain; charset=utf-8")
                return
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/login":
                key = urllib.parse.parse_qs(url.query).get("key", [""])[0]
                if not hmac.compare_digest(key, console.login_key):
                    self._refuse(403, "This link has expired",
                                 "Sign-in links work only while the console that printed them is running. "
                                 "Start the console again for a fresh one.")
                    return
                cookie = f"{COOKIE}={console.session}; HttpOnly; SameSite=Strict; Path=/"
                self._redirect("/", {"Set-Cookie": cookie})
                return
            if url.path.startswith("/static/"):
                self._static(url.path[len("/static/"):])
                return
            if url.path not in {"/", "/snapshot"}:
                self._refuse(404, "Nothing here", "This console has one page.")
                return
            if not console.signed_in(self.headers):
                self._refuse(401, "Signed out",
                             "Open the console from the link it printed when it started. "
                             "The link signs this browser in for as long as the console runs.")
                return
            try:
                overview = console.overview()
                if url.path == "/snapshot":
                    self._send(200, json.dumps(snapshot_payload(overview)).encode(), "application/json; charset=utf-8")
                    return
                page = render_page(overview, console.form_token)
            except (fleetctl.FleetError, OSError, ValueError) as exc:
                self._refuse(500, "The fleet could not be read", str(exc))
                return
            self._html(200, page)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if not console.host_ok(self.headers):
                self._send(421, b"This console only answers on 127.0.0.1.\n", "text/plain; charset=utf-8")
                return
            if not console.origin_ok(self.headers):
                self._refuse(403, "Refused", "Changes are accepted only from this console's own page.")
                return
            if not console.signed_in(self.headers):
                self._refuse(401, "Signed out", "Open the console from the link it printed when it started.")
                return
            path = urllib.parse.urlsplit(self.path).path
            if path not in {"/level", "/preference", "/refresh", "/model", "/order", "/provider/probe", "/provider/add", "/provider/remove"}:
                self._refuse(404, "Nothing here", "This console has one page.")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > (65536 if path == '/order' or path.startswith('/provider/') else 4096):
                self._refuse(413, "Refused", "That request is too large to be a setting.")
                return
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            if not console.form_ok(fields.get("t", [""])[0]):
                self._refuse(403, "Refused", "That form came from an older console. Reload the page and try again.")
                return
            pool = fields.get("pool", [""])[0]
            level = fields.get("level", [""])[0]
            try:
                outcomes = None
                if path.startswith('/provider/'):
                    get = lambda name, default="": fields.get(name, [default])[0]
                    if path == '/provider/remove':
                        result = providers.remove(console.overlay_path, get('id'))
                    else:
                        kwargs = dict(kind=get('kind'), base_url=get('base_url'), credential_ref=get('credential_ref'),
                                      key=get('key'), models=get('models'))
                        if path == '/provider/probe':
                            result = providers.probe(**kwargs)
                        else:
                            result = providers.add(console.overlay_path, id=get('id'), label=get('label'),
                                                   acceptance=get('acceptance'), daily_cap=get('daily_cap', '0'), **kwargs)
                    if self.headers.get("Accept") == "application/json":
                        self._send(200, json.dumps(result).encode(), "application/json; charset=utf-8")
                    elif path == '/provider/probe':
                        text = result['message'] + " Available IDs: " + ", ".join(result['models'])
                        self._html(200, shell("Connection checked", "<h1>Connection checked</h1><p>" + html.escape(text)
                                             + '</p><p><a href="/">Back to providers</a></p>'))
                    else:
                        self._redirect('/')
                    return
                if path == '/order':
                    console.set_order(json.loads(fields.get('order', [''])[0]))
                elif path == "/refresh":
                    outcomes = console.refresh_now()
                elif path == "/level":
                    console.set_level(pool, level)
                elif path == "/model":
                    change = fields.get("switch", [""])[0]
                    if change:   # "<model>=on" or "<model>=off": the state to set, never a flip
                        model, _, state = change.rpartition("=")
                        if state not in {"on", "off"}:
                            raise ValueError("unknown state")
                        console.set_toggle(pool, model, state == "on")
                    else:
                        console.set_choice(pool, fields.get("model", [""])[0])
                else:
                    console.set_preference(fields.get("model", [""])[0], fields.get("preference", [""])[0])
            except providers.ProviderError as exc:
                if self.headers.get("Accept") == "application/json":
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                else:
                    self._refuse(400, "Not saved", str(exc))
                return
            except (ValueError, fleetctl.FleetError, OSError):
                self._refuse(400, "Not changed", "That provider, model or setting does not exist.")
                return
            if self.headers.get("Accept") == "application/json":
                payload = snapshot_payload(console.overview(refresh=False))
                if outcomes is not None:
                    payload["refresh"] = outcomes
                self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
                return
            self._redirect(f"/#pool-{urllib.parse.quote(pool)}" if path in {"/level", "/model"} else "/")

        def _static(self, name: str) -> None:
            target = (ASSETS / name).resolve()
            if ASSETS.resolve() not in target.parents or target.suffix not in STATIC_TYPES or not target.is_file():
                self._refuse(404, "Nothing here", "This console has one page.")
                return
            headers = {"Cache-Control": STATIC_CACHE[target.suffix]}
            if target.suffix == ".woff2":
                # A translated copy of a page (Google's page translator) is shown from Google's address: without this
                # header it drops the typefaces and the page falls back to a wider face.
                headers["Access-Control-Allow-Origin"] = "*"
            self._send(200, target.read_bytes(), STATIC_TYPES[target.suffix], headers)

    return Handler


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def bind(console: Console, preferred: int) -> _Server:
    """127.0.0.1 only. The preferred port, else the next twenty, else any free one."""
    for port in [*range(preferred, preferred + 21), 0]:
        try:
            server = _Server(("127.0.0.1", port), make_handler(console))
        except OSError:
            continue
        console.port = server.server_address[1]  # the handler reads it through the closure
        return server
    raise OSError("no free port on 127.0.0.1")


def serve(overlay_path: Path, state_dir: Path, port: int = 8768, open_browser: bool = True) -> int:
    fleetctl.read_overlay(overlay_path, state_dir)  # fail before binding when there is nothing to show
    console = Console(overlay_path, state_dir, port)
    server = bind(console, port)
    link = f"http://127.0.0.1:{console.port}/login?key={console.login_key}"
    print(f"{PRODUCT_NAME} console on http://127.0.0.1:{console.port}/ (Ctrl-C stops it)")
    print(f"Sign-in link for this launch: {link}", flush=True)
    if open_browser:
        webbrowser.open(link)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
