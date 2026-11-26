"""The console: one local page that shows every provider pool and sets how much of it to spend.

It is a door, not a view: a click here changes where agents route. So it is locked like one.

- It binds 127.0.0.1 and nothing else. There is no option to widen that.
- Each launch mints a sign-in key and trades it for an HttpOnly, SameSite=Strict cookie.
  The cookie and form token are derived separately from a private per-install secret so an
  already signed-in browser can keep working when this loopback server restarts.
- Every request must name this server in its Host header (127.0.0.1:PORT or localhost:PORT).
  That defeats DNS rebinding, where a hostile page points its own hostname at 127.0.0.1.
- Every change is a POST that must come from this origin (Origin, else Referer, and a
  same-origin Sec-Fetch-Site when the browser sends one) and carry the install's form token.
  That defeats a cross-site form post even if a browser were to attach the cookie.
- Responses forbid framing, inline scripts, off-origin loads and caching.

Standard library only. The page renders from `fleetctl.fleet_overview`, the same structure
`fleetctl.py brief` prints, so what the operator sees and what agents read cannot drift.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import html
import http.server
import importlib.util
import json
import math
import os
import re
import secrets
import stat
import sys
import threading
import time
import types
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

_selector_spec = importlib.util.spec_from_file_location("console_selector", HERE / "selector.py")
assert _selector_spec and _selector_spec.loader
selector = importlib.util.module_from_spec(_selector_spec)
_selector_spec.loader.exec_module(selector)

# What a translator must never change (model names, ids, amounts, the signature): marked on every page as it is made. The
# rules, written once for every Castor product, are in console-assets/keep.py, and in keep.js for what the page's script adds.
_keep_spec = importlib.util.spec_from_file_location("console_keep", ASSETS / "keep.py")
assert _keep_spec and _keep_spec.loader
console_keep = importlib.util.module_from_spec(_keep_spec)
sys.modules.setdefault("console_keep", console_keep)
_keep_spec.loader.exec_module(console_keep)
keep_html = console_keep.keep_html

try:
    import phone_view
    import providers
except ImportError:
    from scripts import phone_view, providers

PRODUCT_NAME = fleetctl.PRODUCT_NAME
LEVEL_WORDS = {"off": "Off", "low": "Low", "normal": "Normal", "high": "High", "forced": "Max"}
LEVELS = tuple(LEVEL_WORDS)
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


def _install_secret(state_dir: Path) -> bytes:
    """Read or create the same private key, including during overlapping starts."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(state_dir / 'console-session.key', flags, 0o600)
    with os.fdopen(fd, 'r+b', buffering=0) as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise OSError('The console session key must be an operator-owned regular file with mode 600.')
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        key = file.read(33)
        if not key:
            key = secrets.token_bytes(32)
            file.write(key)
            os.fsync(file.fileno())
        if len(key) != 32:
            raise OSError('The console session key is damaged.')
        return key


class Console:
    """One loopback console and its install-local state."""

    def __init__(self, overlay_path: Path, state_dir: Path, port: int) -> None:
        self.overlay_path = overlay_path
        self.state_dir = state_dir
        self.port = port
        self.login_key = secrets.token_urlsafe(32)
        secret = _install_secret(state_dir)
        self.session = hmac.digest(secret, b'crossfeed console session v1', 'sha256').hex()
        self.form_token = hmac.digest(secret, b'crossfeed console form v1', 'sha256').hex()
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
        return hmac.compare_digest(cookies.get(COOKIE, '').encode(), self.session.encode())

    def form_ok(self, token: str) -> bool:
        return hmac.compare_digest((token or '').encode(), self.form_token.encode())

    # ---- the fleet ------------------------------------------------------------------------
    def overview(self, refresh: bool = True) -> dict[str, Any]:
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        if refresh:
            self.refresh_in_background(roster)
        runtime = fleetctl.load_json(self.state_dir / "runtime.json", {}) or {}
        overview = fleetctl.fleet_overview(roster, runtime, self.state_dir)
        overview['_brief_context'] = {'roster': roster, 'runtime': runtime}
        overview['subscription_spend'] = fleetctl.subscriptions.view(
            fleetctl.subscriptions.path_for(self.overlay_path), roster, overview)
        overview['provider_sources'] = providers.list_sources(roster)
        evidence = fleetctl.load_json(self.state_dir / 'evidence' / 'levels.json', {}) or {}
        overview['console_order'] = display_order(overview, runtime.get('console_order'), roster, evidence)
        return overview

    def add_benchmark_link(self, model: str, url: str, label: str) -> None:
        url = _benchmark_url(url)
        label = label.strip()
        if not url or len(label) > 120:
            raise ValueError('invalid benchmark link')
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir, discover=False)
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            known = {canonical for canonical, _ in _comparison_evidence(roster, self.state_dir)}
            known.update(selector.canonical_model(row['model'], roster) for row in fleetctl.model_overview(roster, runtime))
            known.update(lane['benchmark_identity'] for lane in roster.get('lanes', []) if lane.get('benchmark_identity'))
            if model not in known:
                raise ValueError('unknown model')
            rows = runtime.setdefault('console_benchmark_links', {}).setdefault(model, [])
            if not any(row['url'] == url for row in rows):
                if len(rows) >= 100:
                    raise ValueError('too many links for this model')
                rows.append({'url': url, 'label': label, 'set_by': 'operator', 'set_at': fleetctl.iso()})

    def save_subscription(self, fields: dict) -> None:
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir, discover=False)
        fleetctl.subscriptions.save(fleetctl.subscriptions.path_for(self.overlay_path), roster,
                                    fields.get('id', ''), fields)

    def set_pro_allowance(self, pool: str, allowance: str) -> None:
        if not re.fullmatch(r"[0-9]{1,6}", allowance):
            raise ValueError("invalid allowance")
        discovered = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        if not any(fleetctl.chatgpt_pro.is_pro(lane) and lane.get("quota_pool") == pool
                   for lane in discovered.get("lanes", [])):
            raise ValueError("not a Pro pool")
        with providers.edit_overlay(self.overlay_path) as roster:
            if pool not in roster.get("quota_pools", {}):
                raise ValueError("not a Pro pool")
            roster["quota_pools"][pool]["pro_weekly_allowance"] = int(allowance)

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

    # BR-SWITCH-001
    def set_level(self, pool: str, level: str, reason: str | None = None) -> None:
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        if level not in LEVELS:
            raise ValueError("unknown level")
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            set_level = getattr(fleetctl, 'set_level', None)
            if set_level:
                set_level(roster, runtime, pool, level, actor='owner', reason=reason)
            elif pool in roster.get('quota_pools', {}) and level in fleetctl.LEVELS:
                fleetctl.set_pool_level(runtime, pool, level, actor='owner', reason=reason)
            else:
                raise ValueError('unknown pool or level')

    def set_archive(self, pool: str, model: str, archived: bool, reason: str | None = None) -> None:
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            set_archive = getattr(fleetctl, 'set_model_archive', None)
            if not set_archive:
                raise ValueError('archive controls unavailable')
            set_archive(roster, runtime, pool, model, archived, actor='owner', reason=reason)
        fleetctl.pins_after_switch(roster, self.state_dir, actor='owner', reason=reason)

    # BR-SWITCH-001
    def set_toggle(self, pool: str, model: str, on: bool, reason: str | None = None) -> None:
        """Switch one model of a provider on or off."""
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            fleetctl.set_model_toggle(runtime, roster, pool, model, on, actor="owner", reason=reason)
        fleetctl.pins_after_switch(roster, self.state_dir, actor="owner", reason=reason)   # files that name a model follow the switch, as on the CLI

    # BR-SWITCH-001
    def set_choice(self, pool: str, model: str, reason: str | None = None) -> None:
        """Run only one model on a provider (a model key), or switch every model on ("auto")."""
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            if model == "auto":
                # "Switch all on" is the list you see: the current models. An older model stays as you left it;
                # it never stands in for another, and runs only when a task names it.
                for key in fleetctl.current_models(roster, pool):
                    fleetctl.set_model_toggle(runtime, roster, pool, key, True, actor="owner", reason=reason)
                fleetctl.record_control(runtime, f"choice:{pool}", None, "auto", "owner", reason)
            else:
                fleetctl.set_model_choice(runtime, roster, pool, model, actor="owner", reason=reason)
        fleetctl.pins_after_switch(roster, self.state_dir, actor="owner", reason=reason)

    # BR-SWITCH-001
    def set_preference(self, model: str, preference: str, reason: str | None = None) -> None:
        roster = fleetctl.read_overlay(self.overlay_path, self.state_dir)
        if model not in {item["model"] for item in fleetctl.model_overview(roster, {})}:
            raise ValueError("unknown model")
        if preference not in fleetctl.MODEL_PREFERENCES:
            raise ValueError("unknown model preference")
        with fleetctl.locked_runtime(self.state_dir) as runtime:
            for pool in roster.get("quota_pools", {}):
                if model in fleetctl.choosable_models(roster, pool):
                    fleetctl.set_model_toggle(runtime, roster, pool, model, preference != "off", actor="owner", reason=reason)
            before = fleetctl.model_preference(runtime, model)
            fleetctl.set_model_preference(runtime, model, preference)
            fleetctl.record_control(runtime, f"preference:{model}", before, preference, "owner", reason)
        fleetctl.pins_after_switch(roster, self.state_dir, actor="owner", reason=reason)


# ---- rendering ------------------------------------------------------------------------------
POOL_SORTS = {'your': 'Your order', 'quota': 'Quota left', 'reset': 'Resets soonest', 'name': 'Name'}
MODEL_SORTS = {'your': 'Your order', 'quality': 'Quality', 'cheapest': 'Cheapest', 'name': 'Name'}
AGY_ALLOWANCES = {'antigravity-gemini': 'Gemini', 'antigravity-3p': 'Claude/GPT', 'antigravity-argon': 'Argon'}
WORKBENCH_ROLES = {'implementation': 'Implementation', 'review': 'Review', 'hard-reasoning': 'Hard reasoning',
                   'debug': 'Debug', 'default': 'Default', 'research-scout': 'Research scout', 'repo-map': 'Repo map'}
VALUE_MODES = {'best-value': 'Best value', 'absolute-best': 'Absolute best'}


def _agy_summary(pools: list[dict[str, Any]]) -> str:
    """Summarize independent allowances without inventing a subscription-wide quota."""
    by_pool = {p['pool']: p for p in pools}
    words = []
    for key, label in AGY_ALLOWANCES.items():
        if key not in by_pool:
            continue
        pool = by_pool[key]
        if pool['state'] == 'EXHAUSTED':
            state = 'used up'
            if pool.get('until'):
                reset = fleetctl.parse_iso(pool['until']).astimezone()
                state += f' until {reset.day} {reset:%b}'
        else:
            readings = [_number(limit.get('used_percent')) for limit in pool.get('limits') or []]
            readings = [value for value in readings if value is not None]
            reading = _number((pool.get('quota') or {}).get('used_percent'))
            if reading is not None:
                readings.append(reading)
            state = f'{max(readings):.0f}% used' if readings else 'no reading'
        words.append(f'{label} {state}')
    return ' · '.join(words)


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
    defaults = {}
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
        def quality_rank(key):
            model = by_model.get(key) or {}
            levels = [lane.get('worker_level') for lane in model.get('lanes') or []
                      if lane.get('harness') == 'chatgpt-chat']
            level_order = {'pro': 0, 'xhigh': 1, 'high': 2, 'medium': 3, 'instant': 4}
            # Saved picker levels are distinct quality tiers, even when one has no samples yet.
            if levels:
                return (0, min(level_order.get(level, 5) for level in levels), 0)
            return (1, *measured(key, qualities, True),
                    (model.get('card') or {}).get('order', float('inf')))
        quality_order = sorted(current, key=quality_rank)
        if not saved['models'].get(pool):
            models[pool] = quality_order[:]
        defaults[pool] = 'your' if saved['models'].get(pool) else 'quality'
        ranks['models'][pool] = {'your': models[pool],
            'quality': quality_order,
            'cheapest': sorted(current, key=lambda key: measured(key, prices)),
            'name': sorted(current, key=lambda key: _name(by_model.get(key), key).casefold())}
    sorts = {'pools': saved['sort']['pools'],
             'models': {pool: saved['sort']['models'].get(pool, defaults[pool]) for pool in by_pool}}
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


LENS = (ASSETS / "castor-tool-search.svg").read_text(encoding="utf-8")
SETTINGS_ICON = (ASSETS / "castor-tool-settings.svg").read_text(encoding="utf-8")
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


def _masthead(settings: bool = False, settings_content: str = "", benchmarks: bool = False) -> str:
    """The Castor product header: everything stands on one line that is exactly the column.

    It is fixed to the screen and every part rides up with the page, 1:1, until the air above the
    name is halved; then it holds, the name at its own size (console.css, "v2.8 header").
    """
    name = html.escape(PRODUCT_NAME)
    provider_current = ' aria-current="page"' if not settings and not benchmarks else ""
    benchmark_current = ' aria-current="page"' if benchmarks else ''
    settings_current = ' aria-current="page"' if settings else ""
    settings_controls = ' aria-expanded="false" aria-controls="settings-panel"' if settings_content else ""
    return (
        '<header class="mast" data-line><i class="here" aria-hidden="true"></i><span class="brand">'
        f'<a class="product-mark" href="/" aria-label="{name} console">{MARK}</a>'
        f'<a class="compact-brand" href="/">{BRAND_WORDS}<span class="star" aria-hidden="true">*</span></a></span>'
        f'<nav class="nav" aria-label="Main"><a href="/"{provider_current}><span class="tl" data-t="Providers">Providers</span></a>'
        f'<a href="/benchmarks"{benchmark_current}><span class="tl" data-t="Benchmarks">Benchmarks</span></a></nav>'
        '<span class="bar-tools"><span class="find">'
        '<button class="find-lens" type="button" aria-label="Search everything  /" title="Search everything  /" '
        'aria-keyshortcuts="/ Meta+K Control+K" aria-expanded="false" aria-controls="find">'
        f'{LENS}</button></span>{THEME_TOGGLE}'
        f'<a class="settings-toggle" href="/settings"{settings_current} aria-label="Settings" title="Settings"{settings_controls}>{SETTINGS_ICON}</a></span>'
        '<div class="find-panel" id="find" hidden><label for="global-search-input">Search everything</label>'
        '<span class="find-field"><input id="global-search-input" type="search" autocomplete="off" '
        'placeholder="Find a provider or model" aria-controls="search-results" aria-keyshortcuts="Meta+K Control+K">'
        '<kbd class="key-hint" aria-hidden="true">⌘K</kbd></span>'
        '<p class="search-status" role="status"></p><ul id="search-results"></ul></div>'
        + (f'<section class="settings-panel" id="settings-panel" aria-labelledby="settings-title" hidden>'
           f'<h2 id="settings-title">Settings</h2>{settings_content}</section>' if settings_content else '')
        + '</header>'
    )


def shell(title: str, body: str, front: bool = False, settings: bool = False, settings_content: str = "",
          benchmarks: bool = False, dashboard: bool = False) -> str:
    """A page. `front` is the console's front page, where the name starts as large as its row allows."""
    fonts = "".join(f'<link rel="preload" href="/static/fonts/{font}" as="font" type="font/woff2" crossorigin>'
                    for font in PRELOAD_FONTS)
    front_class = ' class="dashboard"' if dashboard else ' class="front"' if front else ''
    return keep_html(
        f'<!doctype html><html lang="en"{front_class}><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{html.escape(title)} · {html.escape(PRODUCT_NAME)}</title>{fonts}"
        f'<link rel="icon" type="image/svg+xml" href="{TAB_ICON}">'  # inline: no favicon.ico request
        f'<link rel="stylesheet" href="/static/console.css?v={_asset_version("console.css")}">'
        f'<script src="/static/console.js?v={_asset_version("console.js")}" defer></script>'
        f'<script src="/static/keep.js?v={_asset_version("keep.js")}" defer></script></head>'
        f'<body><div class="wrap">{_masthead(settings, settings_content, benchmarks)}<main>{body}</main>{FOOTER}</div></body></html>'
    )


def _settings_cap(overview: dict[str, Any]) -> str:
    roster = overview.get('_brief_context', {}).get('roster', {})
    cap = "Unavailable, Crossfeed Chat did not report a live cap."
    template = roster.get('chatgpt_gateway', {}).get('lane_template')
    if template:
        try:
            from chatgpt_transport import request, settings, Rejected
            base, key = settings(template)
            limits = request(base, key, '/gateway/status', timeout=1).get('wake_limits', {})
            hourly = limits.get('hourly_cap')
            if type(hourly) is int and hourly >= 0:
                cap = f"{hourly} fresh chats per rolling hour" if hourly else "No hourly cap"
        except (Rejected, OSError, ValueError, TypeError, AttributeError):
            pass
    return cap


def _settings_content(overview: dict[str, Any], token: str, *, panel: bool = False) -> str:
    roster = overview.get('_brief_context', {}).get('roster', {})
    cap = "Loading the live cap…" if panel else _settings_cap(overview)
    forms = []
    for pool, config in roster.get('quota_pools', {}).items():
        if any(fleetctl.chatgpt_pro.is_pro(lane) and lane.get('quota_pool') == pool for lane in roster.get('lanes', [])):
            forms.append(f'<form method="post" action="/settings/pro" class="setting-row">'
                         f'<input type="hidden" name="t" value="{html.escape(token)}">'
                         f'<input type="hidden" name="pool" value="{html.escape(pool)}">'
                         + ('<input type="hidden" name="return" value="panel">' if panel else '') +
                         f'<label>Pro weekly allowance<input type="number" name="allowance" min="0" max="999999" required '
                         f'value="{config.get("pro_weekly_allowance", 200)}"></label><button type="submit">Save allowance</button>'
                         '<p class="quiet">Local estimate over the last seven days, not a published ChatGPT limit. '
                         'This changes the Pro meter and its fallback threshold. Zero disables Pro by this estimate.</p>'
                         '<p class="wb-error" role="status"></p></form>')
    return ('<section class="settings"><div class="setting-row"><label for="theme-choice">Theme</label>'
                 '<select id="theme-choice" data-theme-choice><option value="system">System</option>'
                 '<option value="light">Light</option><option value="dark">Dark</option></select>'
                 '<p class="quiet">Saved in this browser.</p></div>' + ''.join(forms) +
                 ('<div class="setting-row"><a href="/settings#subscriptions">Subscriptions</a></div>'
                  if panel else _subscription_settings(overview, token)) +
                 f'<div class="setting-row"><h2>Fresh ChatGPT chats</h2><p>Hourly cap: <span data-settings-cap>{html.escape(cap)}</span>.</p>'
                 '<p class="quiet">Read the service status for the live cap. Set <code>--wake-hourly-cap</code> '
                 'in the Crossfeed Chat service launcher and restart that service. This console cannot change it.</p></div>'
                 '<div class="setting-row"><h2>Provider keys</h2><p>Keys entered here stay in <code>provider-keys/</code> beside your access overlay, '
                 'or in the secret reference you supplied. Values are never shown here.</p>'
                 '<a href="/#provider-setup">Add provider</a></div>'
                 '<div class="setting-row"><a href="/phone">Phone view</a></div></section>')


def _subscription_settings(overview: dict, token: str) -> str:
    ledger = overview.get('subscription_spend') or {}
    out = ['<section class="setting-row" id="subscriptions"><h2>Subscriptions</h2>']
    if ledger.get('unavailable'):
        out.append('<p>The subscriptions file is unreadable. Check its format and mode (600).</p>')
    for month in ledger.get('monthly', []):
        own = (f'{month["currency"]} {month["operator"]}' if month['operator'] is not None else 'not set')
        out.append(f'<p>{html.escape(month["currency"])} {html.escape(month["total"])} / month recorded'
                   f' · Your share: {html.escape(own)}</p>')
    if ledger.get('unpriced'):
        out.append(f'<p class="quiet">{ledger["unpriced"]} without a price. No price is assumed.</p>')
    labels = {p['pool']: p['label'] for p in overview.get('pools', [])}

    def input_field(row, name, label, kind='text', attrs=''):
        value = html.escape(row[name] or '', quote=True)
        return f'<label>{label}<input name="{name}" type="{kind}" value="{value}" {attrs}></label>'

    for row in ledger.get('subscriptions', []):
        out.append(f'<details class="setting-row"><summary>{html.escape(row["name"])}</summary>'
                   '<form method="post" action="/settings/subscriptions">'
                   f'<input type="hidden" name="t" value="{html.escape(token, quote=True)}">'
                   f'<input type="hidden" name="id" value="{html.escape(row["id"], quote=True)}">')
        out.append(input_field(row, 'amount', 'Plan price', 'number', 'min="0" step="0.01"'))
        out.append(input_field(row, 'currency', 'Currency', attrs='maxlength="3" placeholder="EUR"'))
        out.append('<label>Billing period<select name="period">')
        for value, label in (('', 'Not set'), ('monthly', 'Monthly'), ('yearly', 'Yearly')):
            selected = ' selected' if (row['period'] or '') == value else ''
            out.append(f'<option value="{value}"{selected}>{label}</option>')
        out.append('</select></label>')
        out.append(input_field(row, 'renewal', 'Next renewal', 'date'))
        out.append(input_field(row, 'operator_share_percent', 'Your share (%)', 'number', 'min="0" max="100" step="0.01"'))
        out.append(input_field(row, 'other_payer', 'Other payer (optional)', attrs='maxlength="60"'))
        if row['other_share_percent'] is not None:
            out.append(f'<p class="quiet">Other share: {html.escape(row["other_share_percent"])}%</p>')
        out.append('<button type="submit">Save</button></form>')
        for pool in row['pools']:
            value = row['pool_value'].get(pool)
            equivalent = (f'USD {value["used_usd_equivalent"]} of {value["capacity_usd_equivalent"]} equivalent'
                          if value else 'Value not measured')
            paid = f'{row["currency"]} {row["monthly"]} / month for the plan' if row['monthly'] is not None else 'Price not set'
            out.append(f'<p class="quiet">{html.escape(labels.get(pool, pool))}: {html.escape(equivalent)}'
                       f' · {html.escape(paid)}</p>')
        out.append('</details>')
    out.append('<p class="quiet">Service value uses the current weekly quota window, not charges. '
               'Shared pools show the same plan price; the total counts it once.</p></section>')
    return ''.join(out)


def render_settings(overview: dict[str, Any], token: str) -> str:
    return shell('Settings', '<h1>Settings</h1><p class="lede">Preferences and local limits.</p>'
                 + _settings_content(overview, token), settings=True)


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


def _limit_row(limit: dict[str, Any], pool: str = "", *, spare_allowed: bool = True) -> str:
    limit = {**limit, "scope": _scope_words(limit.get("scope"), pool)}
    if limit["kind"] == "refusal":
        title = html.escape(limit["title"])
        until = html.escape(limit["reset_at"])
        return (f'<li class="lim hot full"><span class="ln">{title}</span>'
                f'<span class="pc"><b>EXHAUSTED</b></span>'
                f'<span class="rs">resets <time datetime="{until}">{until}</time></span></li>')
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
    used = limit["used_percent"]
    hot = used >= 75
    if limit["reset_at"] is None:
        reset = "reset not reported" if limit.get("reset_unknown") else "not started yet"
    else:
        reset = f'resets {limit["resets"]}'
    when = html.escape(reset)
    use = limit.get("spend_down") and spare_allowed and used < 90
    if use:
        when += ' <span class="use">· spare, use it</span>'
    classes = "lim" + (" hot" if hot else "") + (" full" if used >= 100 else "")
    text = f'{limit["title"]}{", " + limit["scope"] if limit.get("scope") else ""}: {used:g}% used, {reset}' + (
        ", with allowance to spare" if use else "")
    return (f'<li class="{classes}" aria-label="{html.escape(text)}"><span class="ln">{title}{scope}</span>'
            f'{_meter(used, hot)}<span class="pc"><b class="num">{used:g}%</b></span>'
            f'<span class="rs" aria-hidden="true">{when}</span></li>')


def _gauge(pool: dict[str, Any]) -> str:
    """Every limit of the pool's plan, one row each, or why there is none to show."""
    state = pool["state"]
    limits = pool.get("limits") or []
    exhausted = ""
    if state == "EXHAUSTED" and pool.get("until"):
        until = fleetctl.reset_words(pool["until"])
        label = 'EXHAUSTED' if pool['pool'] in AGY_ALLOWANCES else 'Used up'
        exhausted = f'<p class="q hot"><b>{label}</b>, back {html.escape(until)}</p>'
        if not limits:
            return exhausted
    if limits:
        renews = ""
        if pool.get("renews_at"):
            renews = f'<p class="q renews">Plan renews {html.escape(fleetctl.reset_words(pool["renews_at"]))}</p>'
        spare_allowed = pool.get("routing_state", state) not in {"CRITICAL", "EXHAUSTED"}
        return f'{exhausted}<ul class="limits" aria-label="Limits">{"".join(_limit_row(l, pool["pool"], spare_allowed=spare_allowed) for l in limits)}</ul>{renews}'
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


def _provider_gauge(pool: dict[str, Any], overview: dict[str, Any]) -> str:
    # A provider refusal deliberately hides gauges from routing evidence. The console can
    # still show the last observed windows beside that refusal, without changing pool data.
    unreported = ('<p class="allowance-note">Usage percentage is not reported for this individual allowance.</p>'
                  if pool['pool'] == 'antigravity-argon'
                  and not any(limit['kind'] == 'window' for limit in pool.get('limits') or []) else '')
    if pool['pool'] not in AGY_ALLOWANCES or pool['state'] != 'EXHAUSTED':
        return _gauge(pool) + unreported
    runtime = overview.get('_brief_context', {}).get('runtime', {})
    snapshot = runtime.get('quota_snapshots', {}).get(pool['pool']) or {}
    now = fleetctl.parse_iso(overview['generated_at'])
    existing = {limit['name'] for limit in pool.get('limits') or []}
    windows = {name: window for name, window in snapshot.get('windows', {}).items()
               if name not in existing and window.get('reset_at') and fleetctl.parse_iso(window['reset_at']) > now}
    if not windows:
        return _gauge(pool) + unreported
    limits, _ = fleetctl.pool_limits(pool, windows, None, [], now)
    age = max(0, int((now - fleetctl.parse_iso(snapshot['observed_at'])).total_seconds()))
    note = f'Last gauge reading {fleetctl.format_duration(age)} ago; provider refusal still applies.'
    return (_gauge({**pool, 'limits': limits + (pool.get('limits') or [])})
            + f'<p class="allowance-note">{html.escape(note)}</p>')


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


def _control_text(record: dict[str, Any] | None) -> str:
    if not record:
        return "Actor not recorded"
    actor = "you" if record.get("actor") == "owner" else "agent"
    when = fleetctl.parse_iso(record["when"]).astimezone()
    return f"{actor}, {when.day} {when:%b %H:%M}"


def _level_attribution(record: dict[str, Any] | None, as_of: str) -> tuple[str, str]:
    words = {"owner": "you", "agent": "an agent"}
    record = record or {}
    actor = record.get('actor') or {'operator': 'owner', 'agent': 'agent'}.get(record.get('set_by'))
    date = record.get('when') or record.get('set_at')
    if actor not in words or not date:
        return "unknown", "Last change not recorded"
    when = fleetctl.parse_iso(date).astimezone()
    days = (fleetctl.parse_iso(as_of).astimezone().date() - when.date()).days
    date = f"today {when:%H:%M}" if days == 0 else "yesterday" if days == 1 else f"{when.day} {when:%b %Y %H:%M}"
    return actor, f"Set by {words[actor]} · {date}"


def _level_meaning(level: str) -> str:
    return WALLET_LEVEL_MEANING['forced'] if level == 'forced' else LEVEL_PLAIN[level] + ' Actual caps and refusals still apply.'


WALLET_LEVEL_MEANING = {"off": "Routing skips this plan.", "low": "Conserve allowance.",
                        "normal": "Follow quota estimates.", "high": "Use allowance more freely.",
                        "forced": "Ignores quota estimates. Actual caps and refusals still apply.",
                        "mixed": "The allowances have different settings. Choose a stop to set them together."}


def _slider(pool: dict[str, Any], form_token: str, as_of: str | None = None, return_fields: str = '') -> str:
    meaning_for = WALLET_LEVEL_MEANING.__getitem__ if return_fields else _level_meaning
    record = pool.get('control')
    actor, attribution = _level_attribution(pool if pool.get('set_by') else record, as_of or fleetctl.iso())
    if not return_fields:
        attribution = _control_text(record)
    current = pool['level']
    at = LEVELS.index(current) if current in LEVELS else 'mixed'
    label = html.escape(pool["label"])
    stops = []
    for level in LEVELS:
        word = LEVEL_WORDS[level]
        meaning = html.escape(meaning_for(level))
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
    pool_id = html.escape(pool['pool'])
    mixed_text = html.escape(WALLET_LEVEL_MEANING['mixed'])
    return (
        '<p class="lvh" aria-hidden="true">How agents use this plan</p>'
        f'<form class="lv at-{at} is-{current}" data-level-actor="{actor}" data-mixed-meaning="{mixed_text}" '
        'method="post" action="/level" role="radiogroup" '
        f'aria-label="How much of {label} to use" aria-describedby="actor-{pool_id} means-{pool_id}">'
        f'<input type="hidden" name="pool" value="{pool_id}"><input type="hidden" name="t" value="{form_token}">'
        f'{return_fields}<span class="rail" aria-hidden="true"><span class="fill"></span></span>{"".join(stops)}</form>'
        f'<p id="actor-{pool_id}" class="control-actor level-actor">{html.escape(attribution)}</p>'
        f'<p class="means" id="means-{pool_id}">{html.escape(meaning_for(current))}</p>'
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
NAME_WORDS = fleetctl.NAME_WORDS
ROLE_WORDS = {
    "default": "everyday tasks", "hard-reasoning": "hard reasoning", "implementation": "building",
    "review": "reviews", "debug": "debugging", "repo-map": "repo maps", "long-context": "long documents",
    "frontend-visual": "front-end and visual work", "audio-video": "audio and video",
    "research-scout": "web research",
}


_pretty = fleetctl.pretty_model_name


def _name(model: dict[str, Any] | None, key: str | None = None) -> str:
    for lane in (model or {}).get("lanes") or []:
        if lane.get("harness") == "chatgpt-chat" and lane.get("worker_row") and lane.get("worker_level"):
            level = {"instant": "Instant", "medium": "Medium", "high": "High",
                     "xhigh": "Extra High", "pro": "Pro"}.get(lane["worker_level"], _pretty(lane["worker_level"]))
            if lane.get("selector") == "chatgpt:media-unattended" or lane.get("model_key") == "chatgpt:media-unattended" or (model or {}).get("model") == "chatgpt:media-unattended" or key == "chatgpt:media-unattended":
                return "ChatGPT media · Images and video · Unattended"
            count = lane.get("max_parallel", 1)
            return f"ChatGPT picker: {lane['worker_row']} · Thinking: {level} · Up to {count} {'task' if count == 1 else 'tasks'} at once"
    card = (model or {}).get("card") or {}
    return str(card.get("name") or _pretty((model or {}).get("model") or key or ""))


def _compact_name(model, key=None, pool=None):
    if pool == 'chatgpt-work':
        for lane in (model or {}).get('lanes', []):
            if lane.get('harness') == 'chatgpt-chat' and lane.get('worker_row') and lane.get('worker_level'):
                return f'{lane["worker_row"]} · {_pretty(lane["worker_level"])}'
    return _name(model, key)


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
            by_key: dict[str, dict[str, Any]] | None = None, *, compact: bool = False) -> str:
    key, name = model["model"], _name(model)
    target = _model_id(pool["pool"], key)
    older = not option["current"]
    if compact:
        name = _compact_name(model, key, pool['pool'])
        blocked = ('Withdrawn' if option.get('retired_on') else 'No runnable route' if not option['run_as'] else
                   'No retention evidence' if older and not option.get('retention_reason') and not option['on'] else '')
        control = _work_switch('switch', option['on'] is True, f'Use {name}',
                               f'{key}={"off" if option["on"] else "on"}', disabled=blocked, hint_id=target)
        esc = html.escape
        identity = esc(f'{pool["pool"]}:{key}')
        canonical = option.get('model_identity') or model.get('model_identity') or key
        return (f'<li class="opt wb-model" id="{target}" data-model="{esc(key)}" '
                f'data-search-label="{esc(name)}" tabindex="-1"><div class="wb-model-name">'
                f'{_benchmark_link(canonical, name)}' + _efficient(option.get('efficient_effort') or model.get('efficient_effort'))
                + ('<small>Older model</small>' if older and option['run_as'] else '') +
                '</div><div class="wb-model-control">'
                f'<button type="button" class="wb-add" data-route="{identity}" aria-label="Compare {esc(name)}" '
                'aria-pressed="false" hidden>Compare</button>' + control +
                f'<button class="wb-add wb-archive" type="submit" formaction="/model-archive" name="archive" '
                f'value="{esc(key)}" aria-label="Archive {esc(name)}">Archive</button>'
                '<span class="wb-error" role="status"></span></div></li>')
    line = _older_reason(model, option, by_key) if older else _best_for(model, pool["pool"])
    if any(lane.get("catalog_state") == "sleeping" for lane in model.get("lanes", [])):
        line = "Sleeping; wakes automatically when selected" + (". " + line if line else "")
    if older and option.get("run_as"):
        line += "; " + (option.get("retention_reason") or "Off: no recorded advantage over a current model")
    for lane in model.get("lanes", []):
        if lane.get("harness") == "chatgpt-chat" and lane.get("worker_row"):
            count = lane.get("max_parallel", 1)
            original = line
            mode = "Media tasks." if key == "chatgpt:media-unattended" else "Text only."
            line = f"{count} saved {'chat' if count == 1 else 'chats'}. {mode}"
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
    if option["run_as"]:
        control = (f'<div class="switch-attribution">{control}<span class="control-actor model-actor">'
                   f'{html.escape(_control_text(option.get("control")))}</span></div>')
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
            order: dict[str, Any] | None = None, *, return_fields: str = '', policy: str = '') -> str:
    pool_id = html.escape(pool["pool"])
    label = html.escape(pool["label"])
    options = pool.get("options") or []
    if return_fields:
        # A single inventory: admission changes the switch, not the section the model belongs to.
        listing = ''.join(_option(by_key.get(o['model'], o), pool, o, compact=True)
                          for o in _sort_model_entries(options, by_key))
        count = len(options)
        count_text = f'{count} model {"entry" if count == 1 else "entries"}'
        return (f'<details class="pick" id="pick-{pool_id}" data-pool="{pool_id}"><summary class="wb-inventory">'
                f'{count_text}</summary>{policy}'
                + f'<form class="wb-models" method="post" action="/model">{return_fields}'
                f'<input type="hidden" name="pool" value="{pool_id}">'
                f'<input type="hidden" name="t" value="{html.escape(form_token)}">'
                f'<ul class="opts">{listing}</ul></form></details>')
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


def _sort_model_entries(options, by_key):
    def details(row):
        model = by_key.get(row['model'], row)
        family = row.get('family') or model.get('family') or row['model']
        cost = _number(row.get('typical_cost_usd', model.get('typical_cost_usd')))
        return family, cost if cost is not None else -1

    families = {}
    for row in options:
        family, cost = details(row)
        families[family] = max(families.get(family, -1), cost)
    return sorted(options, key=lambda row: (-families[details(row)[0]], details(row)[0], -details(row)[1], row['model']))


def _subscription_group(overview, pools, target='antigravity'):
    control = next((s for s in overview.get('subscriptions', []) if s['id'] == target), None)
    if control is None:  # Old overview producers still render the read-only aggregate correctly.
        levels = {p['level'] for p in pools}
        last = max((p.get('control') or {} for p in pools), key=lambda r: r.get('when', ''), default={})
        control = {'level': next(iter(levels)) if len(levels) == 1 else 'mixed', 'control': last}
    return {**control, 'pool': target, 'label': 'Google AI Pro'}


def snapshot_payload(overview: dict[str, Any]) -> dict[str, Any]:
    by_key = {m["model"]: m for m in overview["models"]}
    pools = []
    for p in overview["pools"]:
        state, label, note = models_summary(p, by_key)
        actor, attribution = _level_attribution(p if p.get('set_by') else p.get('control'), overview['generated_at'])
        pools.append({"pool": p["pool"], "subscription": p.get('subscription'), "level": p["level"],
                      'set_by': p.get('set_by'), 'set_at': p.get('set_at'), 'archived': p.get('archived', []),
                      **{key: p[key] for key in ('credits', 'spend_credits', 'status_url', 'usage_cost', 'per_model', 'per_model_error',
                                                 'incident', 'billing', 'billing_error', 'model_quota') if key in p},
                      'wallet_extras': _plan_extras(p, show_status=p['pool'] not in AGY_ALLOWANCES,
                                                   empty=p['pool'] not in AGY_ALLOWANCES),
                      "means": _level_meaning(p["level"]),
                      "control_actor": _control_text(p.get("control")),
                      "wallet_control_actor": attribution, "control_actor_kind": actor,
                      "model_actors": {o["model"]: _control_text(o.get("control")) for o in p.get("options", [])},
                      "gauge": _provider_gauge(p, overview) + _pro_usage(p),
                      "wallet_gauge": _work_gauge(p) + _pro_usage(p),
                      "on": (p.get("models") or {}).get("on", []),
                      "state": state, "label": label, "note": note})
    subscriptions = []
    for row in overview.get('subscriptions', []):
        actor, attribution = _level_attribution(row, overview['generated_at'])
        subscriptions.append({**row, 'control_actor_kind': actor, 'wallet_control_actor': attribution,
                              'means': WALLET_LEVEL_MEANING[row['level']]})
    return {"pools": pools, 'subscriptions': subscriptions, "brief": _brief(overview), "brief_full": _brief(overview, True),
            "agy_summary": _agy_summary(overview['pools']),
            "console_order": overview.get('console_order') or display_order(overview), "stamp": _stamp(overview),
            "wallet_stamp": _wallet_stamp(overview['quota_readings'])}


SOURCE_WORDS = {"codexbar": "CodexBar", "opencode-go-api": "OpenCode", "codex-app-server": "Codex",
                "claude-oauth-usage": "Claude", "antigravity-cli": "Antigravity", "copilot-api": "GitHub"}


def _stamp(overview: dict[str, Any]) -> str:
    readings = overview["quota_readings"]
    if readings["measured_pools"]:
        sources = ", ".join(SOURCE_WORDS.get(source, source) for source in readings["sources"])
        return f'Limits read {_ago(readings["newest_age_s"])} from {sources}'
    return "No limits could be read (no provider answered: signed out or offline), so Crossfeed spends as if every plan had room"


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
    return ('<section class="provider-setup" id="provider-setup" aria-label="Provider setup"><details>'
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


def _compare_key(option: dict[str, Any]) -> str:
    return ':'.join((option['pool'], option['model_key'], option['level']))


def _archived_keys(roster: dict[str, Any]) -> set[str]:
    """Roster-wide withdrawals; per-pool operator archives come from overview rows."""
    keys = set()
    for records in (fleetctl.model_cards(roster), roster.get('model_evidence', {})):
        for key, record in records.items():
            if isinstance(record, dict) and (record.get('status') in {'archived', 'retired'} or record.get('archived') is True):
                keys.add(key)
    return keys


def _visible_model(roster, archived, pool, key, lanes):
    if key in archived or fleetctl.retired_on(roster, key):
        return False
    statuses = lanes.get((pool, key), ())
    return not statuses or not all(status in {'retired', 'archived'} for status in statuses)


def _lane_statuses(roster):
    statuses = {}
    for lane in roster.get('lanes', []):
        statuses.setdefault((lane.get('quota_pool'), lane.get('model_key')), []).append(lane.get('admission_status'))
    return statuses


def _unlisted_compare_options(overview, evidence):
    """Keep an admitted route discoverable even if a quote (for example while Off) is unavailable."""
    roster = overview['_brief_context']['roster']
    lanes = {}
    for lane in roster.get('lanes', []):
        lanes.setdefault((lane.get('quota_pool'), lane.get('model_key')), []).append(lane)
    cards = fleetctl.model_cards(roster)
    for pool in overview['pools']:
        pool_id = pool['pool']
        for entry in pool.get('options', []):
            key = entry['model']
            if not entry.get('run_as') or entry.get('archived') or entry.get('retired_on'):
                continue
            configured = lanes.get((pool_id, key), [])
            sources = [lane for lane in configured if lane.get('admission_status') == 'active'
                       and lane.get('access_status') == 'verified']
            if not sources:
                card = cards.get(key, {})
                if (configured or not fleetctl.pool_is_direct(roster, pool_id)
                        or card.get('access_status', 'verified') != 'verified'
                        or card.get('admission_status', 'active') != 'active'):
                    continue
                sources = [card]
            for lane in sources:
                harness = lane.get('harness') or pool_id
                effort = roster.get('effort', {}).get(key, {})
                configured_levels = effort.get('levels') or {}
                levels = (configured_levels.get(harness) if isinstance(configured_levels, dict) else configured_levels)
                for level in levels or [lane.get('worker_level') or effort.get('default') or 'provider-default']:
                    item = {'pool': pool_id, 'model_key': key, 'model_identity': selector.canonical_model(key, roster),
                            **{field: lane[field] for field in ('benchmark_identity', 'benchmark_effort') if field in lane},
                            'harness': harness, 'level': level, 'lane_id': lane.get('lane_id'),
                            'funding': selector.funding.classify(roster, pool_id, lane),
                            'comparison_only': True, 'efficient_effort': entry.get('efficient_effort')}
                    if hasattr(selector, 'api_cost') and hasattr(selector, 'option_evidence'):
                        item['api_cost_usd'] = selector.api_cost(selector.option_evidence(evidence, item), lane.get('price_1m'))
                    if pool['level'] == 'off':
                        item['quota_basis'] = {'plan': pool['plan'].get('name'),
                                               'why': 'The plan is Off for routing; no quota price is applied.'}
                    yield item


def _workbench_query(query: dict[str, list[str]]) -> tuple[str, str, list[str], int]:
    role = query.get('role', ['review'])[0]
    value = query.get('preview-value', query.get('value', ['best-value']))[0]
    requested = query.get('compare', [])
    if role not in WORKBENCH_ROLES or value not in VALUE_MODES or len(requested) > 4:
        raise ValueError('Choose a listed role and mode and at most four routes.')
    count = query.get('count', [str(max(3, len(requested)))])[0]
    if count not in {'1', '2', '3', '4'}:
        raise ValueError('Compare one to four models.')
    return role, value, requested, int(count)


def _comparison_evidence(roster, state_dir):
    load = getattr(fleetctl, 'comparison_evidence', None)
    if load:
        return load(roster, state_dir)
    evidence = fleetctl.load_json(state_dir / 'evidence' / 'levels.json', {}) or {}
    return selector.evidence_index(roster, evidence.get('rows', []))


def workbench_data(overview: dict[str, Any], state_dir: Path, role: str = 'implementation',
                   value: str = 'best-value', requested: tuple[str, ...] | list[str] = (), count: int = 3) -> dict[str, Any]:
    """Replay the selector on the same local inputs as the subscription rows; never emit a receipt."""
    roster = overview['_brief_context']['roster']
    runtime = overview['_brief_context']['runtime']
    evidence = _comparison_evidence(roster, state_dir)
    fleet = types.SimpleNamespace(**vars(fleetctl))

    def replay(allow=None):
        return selector.select_option(roster, runtime, state_dir, role, fleet=fleet, allow=allow,
                                      value_mode=value, persist_receipts=False)

    def selection_failure(exc):
        return str(exc).startswith('no eligible selector option')

    options, refusals = selector.enumerate_options(roster, runtime, role, fleet=fleet)
    archived = _archived_keys(roster)
    lanes = _lane_statuses(roster)
    blocked = {(pool['pool'], entry['model']) for pool in overview['pools']
               for entry in [*pool.get('archived', []), *(o for o in pool.get('options', []) if o.get('archived'))]}
    visible = lambda option: ((option['pool'], option['model_key']) not in blocked
                              and _visible_model(roster, archived, option['pool'], option['model_key'], lanes))
    automatic = {_compare_key(option): option for option in options if visible(option)}
    menu = dict(automatic)
    explicit_pools = {pool for pool, config in roster.get('quota_pools', {}).items()
                      if config.get('explicit_only')}
    if explicit_pools:
        extra, _ = selector.enumerate_options(roster, runtime, role, fleet=fleet, explicit_pools=explicit_pools)
        for option in extra:
            key = _compare_key(option)
            if option['pool'] in explicit_pools and key not in automatic and visible(option):
                menu[key] = dict(option, explicit_preview=True)
    # The menu includes display-only quotes. The replay, not the menu, decides eligibility.
    previews = getattr(selector, 'compare_options', None)
    if previews:
        for option in previews(roster, runtime, state_dir, role, fleet=fleet, value_mode=value):
            if visible(option):
                key = _compare_key(option)
                menu[key] = {**option, **menu.get(key, {})}
    present = {(item['pool'], item['model_key']) for item in menu.values()}
    for option in _unlisted_compare_options(overview, evidence):
        if (option['pool'], option['model_key']) not in present and visible(option):
            menu.setdefault(_compare_key(option), option)
    memo = {}
    result = {}
    try:
        result = replay()
    except fleetctl.FleetError as exc:
        if not selection_failure(exc):
            raise
        refusals = str(exc).partition(': ')[2].split('; ')
    pick = result.get('choice')
    runner = None
    if pick:
        ranked = result['top3']
        memo.update({_compare_key(option): option for option in reversed(ranked)})
        memo[_compare_key(pick)] = pick
        refusals = result['rejected']
        runner = next((option for option in ranked if option['model_key'] != pick['model_key'] and visible(option)), None)
        if runner is None:
            # The receipt may rank three efforts of one model. Keep global refusals in force in
            # the comparison replay, particularly the rule that withholds low-only fallbacks.
            withheld = {line.split(': ', 1)[0] for line in refusals
                        if 'low spend fallback withheld' in line or 'unknown cell dominated' in line}
            alternatives = ','.join(key for key, option in automatic.items()
                                    if option['model_key'] != pick['model_key']
                                    and f'{option["model_key"]}/{option["level"]}' not in withheld)
            if alternatives:
                try:
                    runner = replay(alternatives)['top3'][0]
                except fleetctl.FleetError as exc:
                    if not selection_failure(exc):
                        raise
            if runner:
                memo[_compare_key(runner)] = runner

    hidden_pick = bool(pick and not visible(pick))
    if hidden_pick:
        pick = None
    defaults = [_compare_key(option) for option in [pick, runner, *result.get('top3', [])] if option and visible(option)]
    keys = list(dict.fromkeys(key for key in requested if key in menu))[:count]
    identities = {(menu[key]['pool'], menu[key]['model_key']) for key in keys}
    for key in dict.fromkeys([*defaults, *automatic, *menu]):
        if len(keys) >= count:
            break
        if key not in menu:
            continue
        item = menu[key]
        identity = (item['pool'], item['model_key'])
        if identity not in identities:
            keys.append(key)
            identities.add(identity)
    if len(keys) < count:
        keys = list(dict.fromkeys([*keys, *defaults, *menu]))[:count]
        keys = [key for key in keys if key in menu]
    for key in keys:
        if key not in memo:
            if menu[key].get('comparison_only') and not menu[key].get('explicit_preview') and key not in automatic:
                memo[key] = None
                continue
            try:
                ranked = replay(key)
                memo[key] = next((option for option in [ranked.get('choice'), *ranked['top3']]
                                  if option and _compare_key(option) == key), None)
            except fleetctl.FleetError as exc:
                if not selection_failure(exc):
                    raise
                memo[key] = None
    if value == 'absolute-best':
        why = 'Prioritizes the highest estimated chance of success among available routes.'
    elif pick and pick.get('expected_cost') is not None:
        why = 'Balances estimated success against the cost of using allowance and of having to retry.'
    else:
        why = 'No comparable price; ranks the available routes by estimated quality and allowance pressure.'
    if (result.get('hard_reasoning_preference') or {}).get('route') == 'opus':
        why = 'Opus hard-reasoning preference. ' + why
    elif (result.get('hard_reasoning_preference') or {}).get('route') == 'gpt-fallback':
        why = 'Astra fallback when Opus is unavailable. ' + why
    offered = {item['model_key'] for item in menu.values()}
    hidden_names = archived | {key for _, key in blocked if key not in offered}
    hidden_names |= {key for (pool, key) in lanes if not _visible_model(roster, archived, pool, key, lanes)}
    prioritized = sorted({text for text in refusals if text and not any(key in text for key in hidden_names)},
                         key=lambda text: ('floor' not in text, 'pool exhausted' not in text, text))
    return {'role': role, 'value': value, 'menu': menu, 'keys': keys, 'scores': memo, 'pick': pick,
            'runner': runner, 'exploring': result.get('exploration', False), 'why': why, 'refusals': prioritized[:16], 'hidden_pick': hidden_pick,
            'evidence': evidence,
            'as_of': fleetctl.utc_now().astimezone().strftime('%H:%M'), 'count': count}


def _work_gauge(pool):
    esc = html.escape
    limits = pool.get('limits') or []
    age = (pool.get('quota') or {}).get('age_s')
    stale_age = pool.get('stale_age_s')
    if pool.get('gauge_error'):
        note = f'Last reading {_ago(age)} · refresh failed' if age is not None else 'Refresh failed'
    elif stale_age is not None:
        note = f'Stale reading · {_ago(stale_age)}'
    else:
        note = ''
    rows = []
    windows = [limit for limit in limits if limit['kind'] == 'window']
    weekly = [limit for limit in windows if 'weekly' in limit['title'].lower()]
    weekly.sort(key=lambda limit: bool(_scope_words(limit.get('scope'), pool['pool'])))
    prominent = ((weekly + [limit for limit in windows if limit not in weekly]) if pool['pool'] == 'opencode-go'
                 else weekly or windows[:1])
    extra = []
    for limit in [*prominent, *(limit for limit in limits if limit not in prominent)]:
        if limit['kind'] != 'window':
            rows.append(_limit_row(limit, pool['pool']))
            continue
        scope = _scope_words(limit.get('scope'), pool['pool'])
        name = limit['title']
        if name == 'Weekly' and scope in {'Fable', 'Fable only'}:
            title = 'Fable weekly'
        elif pool['pool'] in AGY_ALLOWANCES:
            title = f'{AGY_ALLOWANCES[pool["pool"]]} {name.lower()}'
        else:
            title = name + (f' · {scope}' if scope else '')
        used = limit['used_percent']
        reset = ('Resets ' + limit['resets'] if limit['reset_at'] else
                 'Reset unknown' if limit.get('reset_unknown') or used else 'No active reset')
        count = f'{100 - used:g}%' if limit['reset_at'] or used or limit.get('reset_unknown') else ''
        if count and note:
            count = 'Last ' + count
        if limit not in prominent:
            value = (' · ' + count + ' left') if count else ''
            extra.append(f'<li class="wb-extra-limit"><span>{esc(title + value)}</span><small>{esc(reset)}</small></li>')
            continue
        visual = _meter(100 - used, used >= 75) if count and not note else ''
        rows.append(f'<li class="wb-limit"><span>{esc(title)}</span>'
                    + (f'<b aria-label="{count} left">{count}<span class="vh"> left</span></b>' if count
                       else '<b class="wb-idle">—</b>' if limit.get('reset_unknown') else '<b class="wb-idle">Idle</b>') + visual
                    + f'<small>{esc(reset)}</small></li>')
    rows.extend(extra)
    if not rows and not note:
        note = 'No published cap' if pool['plan'].get('limit') == 'none-known' else 'No reading'
    refusal = ''
    if pool['state'] == 'EXHAUSTED' and pool.get('until'):
        reset_at = esc(pool['until'])
        reset = esc(fleetctl.reset_words(pool['until']))
        refusal = f'<p class="wb-refusal">Used up · back <time datetime="{reset_at}">{reset}</time></p>'
    return refusal + (f'<p class="wb-muted wb-reading-note">{esc(note)}</p>' if note else '') + (
        f'<ul class="wb-limits" aria-label="Limits">{"".join(rows)}</ul>' if rows else '')


def _compare_cells(option, scored, pool, roster):
    q = (scored or {}).get('q') or {}
    grade = _number(q.get('mean')) if not q.get('unknown') else None
    cost = (scored or {}).get('cost', {})
    quota = _number(cost.get('percent'))
    cash = (scored or {}).get('cash') or {}
    usd = _number(cash.get('estimate_usd')) if option.get('funding') == 'metered' else None
    if usd is None and option.get('funding') == 'metered' and option.get('source_price_1m'):
        usd = _number(cost.get('estimated_usd'))  # the selector prices this cell on its declared lane tariff
    sources = [lane for lane in roster.get('lanes', []) if lane.get('lane_id') == option.get('lane_id')
               and lane.get('quota_pool') == option['pool'] and option.get('lane_id')]
    card = (roster.get('model_cards') or {}).get(option['model_key']) or {}
    if card.get('pool') == option['pool']:
        sources.append(card)
    context = next((source[field] for source in sources for field in ('context_window', 'context_length')
                    if _number(source.get(field)) and source[field] > 0), None)
    price = (scored or {}).get('pool_price') or {}
    used = _number(price.get('window', {}).get('used_percent'))
    capacity = (scored or {}).get('capacity') or {}
    if capacity.get('headroom_basis') == 'neutral_recent_work':
        headroom = 'Unknown · neutral price estimate'
    elif pool.get('gauge_error') or pool.get('stale_age_s') is not None:
        headroom = 'Refresh failed' if pool.get('gauge_error') else 'Stale'
    elif used is not None:
        title, scope = fleetctl.limit_name(price['binding_window'], price['window'], pool.get('label', ''))
        scope = _scope_words(scope, option['pool'])
        headroom = f'{100 - used:g}% left · {title}' + (f' · {scope}' if scope else '')
    else:
        headroom = 'Not reported'
    return [f'{grade:.3f}' if grade is not None else 'Unrated',
            (f'{"≈" if cost.get("estimated") else ""}{quota:.3g}%') if quota is not None else '—',
            f'≈${usd:.4f}' if usd is not None else '—', f'{context:,.0f} tokens' if context else '—',
            pool.get('label') or option['pool'], headroom]


def _money_cell(value):
    amount = _number(value)
    if amount is None:
        return '—'
    text = f'{amount:.4f}' if amount == 0 or amount >= 0.0001 else f'{amount:.8f}'.rstrip('0')
    return f'<span class="wb-number">${text.removesuffix(".")}</span>'


def _quota_cell(item, data, scored):
    basis = item.get('quota_basis') or {}
    qty = _money_cell(item.get('quota_cost_usd'))
    plan, window = basis.get('plan'), basis.get('window')
    if window:
        window = next((label for suffix, label in (('weekly', 'Weekly'), ('monthly', 'Monthly'),
                                                   ('5h', '5-hour'), ('session', 'Session'))
                       if str(window).endswith(suffix)), window)
    details = [' · '.join(str(x) for x in (plan, window) if x)]
    limit = basis.get('window_limit')
    if limit is not None:
        label = f'${limit:g}' if item.get('pool') == 'opencode-go' and _number(limit) is not None else str(limit)
        details.append(f'Allowance: {label}.')
    share, value = _number(basis.get('share_percent')), _number(basis.get('plan_value_usd'))
    if share is not None:
        details.append(f'A typical job uses about {share:.3g}% of this allowance.')
        if qty == '—':
            qty = f'<span class="wb-number">≈{share:.3g}%</span>'
            details.append('There is no dollar estimate for this share.')
    if value is not None:
        details.append(f'Estimated value of this allowance: ${value:,.2f}.')
    details.append(basis.get('why') or 'No quota estimate is available for this route.')
    pick = data.get('pick')
    if pick and _compare_key(pick) == _compare_key(item):
        details.append(f'Why it leads: {data["why"]}')
    elif not scored:
        details.append('The selector did not rank this route for the selected role.')
    elif pick:
        details.append('This route was not the first choice for this role.')
    prose = html.escape(' '.join(s for s in details if s))
    key = html.escape(_compare_key(item))
    return (f'<details class="quota-explain" data-detail="{key}"><summary aria-label="Explain quota cost" title="{prose}">'
            f'{qty}<span aria-hidden="true" class="quota-info">{CHEVRON}</span></summary><p>{prose}</p></details>')


def _efficient(record):
    if not isinstance(record, dict) or not record.get('effort'):
        return ''
    label = html.escape(_pretty(record['effort']))
    why = html.escape(record.get('why') or 'Best estimated success for the total cost.')
    return (f'<details class="efficient-effort"><summary title="{why}">Most efficient: {label}'
            f'<span aria-hidden="true">{CHEVRON}</span></summary><p>{why}</p></details>')


def _comparison_grade(data, item, scored, role, policy):
    family = policy.get('families_by_role', {}).get(role) or selector.FAMILY_BY_ROLE.get(role, 'coding-agent')
    read = getattr(selector, 'option_evidence', None)
    row = read(data['evidence'], item) if read else data['evidence'].get((item['model_identity'], item['level']), {})
    strata = row.get('q_by_stakes', {})
    hard_grade = strata.get('high', {}).get(family)
    scoped = (hard_grade if role == 'hard-reasoning' and hard_grade and not hard_grade.get('unknown')
              else strata.get('normal', {}).get(family))
    replayed = (scored or item).get('q') if role == data['role'] else None
    q = replayed or (scoped if scoped is not None else row.get('q', {}).get(family) or {})
    mean = _number(q.get('mean'))
    sourced = q.get('n_sources') or row.get('own', {}).get(family, {}).get('trials')
    if mean is None or q.get('unknown') or (not replayed and ('unknown' in (row.get('flags') or []) or not sourced)):
        return '—'
    pick = role == data['role'] and data['pick'] and _compare_key(data['pick']) == _compare_key(item)
    cls = ' pick-grade' if pick else ''
    note = '<small>Pick</small>' if pick else ''
    return f'<span class="wb-grade{cls}">{mean:.3f}</span>{note}'


def _workbench(overview: dict[str, Any], data: dict[str, Any]) -> str:
    esc = html.escape
    roster = overview['_brief_context']['roster']
    policy = roster.get('policy', {}).get('selector', {})
    pools = {pool['pool']: pool for pool in overview['pools']}
    models = {model['model']: model for model in overview['models']}
    entries = {(pool['pool'], row['model']): row for pool in overview['pools'] for row in pool.get('options', [])}
    def name(option):
        return _compact_name(models.get(option['model_key']), option['model_key'], option['pool'])
    def route(option):
        if not option:
            return 'None eligible'
        heading = f'<span class="nm">{esc(name(option))}</span>'
        return heading if option['pool'] == 'chatgpt-work' else heading + ' · ' + esc(_pretty(option['level']))
    def select(values, current):
        return ''.join(f'<option value="{esc(str(key))}"{" selected" if key == current else ""}>{esc(text)}</option>'
                       for key, text in values.items())
    def pool_name(key):
        return _wallet_title(pools[key]) if key in pools else key
    def identity(option):
        return option['pool'], option['model_key']
    def preference(option):
        efficient = (option.get('efficient_effort') or {}).get('effort')
        return (efficient == option['level'], _number(option.get('api_cost_usd')) is not None,
                not option.get('comparison_only'))
    grouped = {}
    for key, item in data['menu'].items():
        grouped.setdefault(identity(item), {})[key] = item

    headers, columns = [], []
    choices = sorted(grouped.items(), key=lambda pair: (pool_name(pair[0][0]), name(next(iter(pair[1].values())))))
    grade_roles = ['implementation', 'review', 'debug', 'hard-reasoning']
    families = policy.get('families_by_role', {})
    default_family = families.get('default') or selector.FAMILY_BY_ROLE.get('default', 'coding-agent')
    implementation_family = families.get('implementation') or selector.FAMILY_BY_ROLE['implementation']
    if default_family != implementation_family:
        grade_roles.append('default')
    if data['role'] != 'default' and data['role'] not in grade_roles:
        grade_roles.append(data['role'])
    fields = [(WORKBENCH_ROLES[role], role == data['role']) for role in grade_roles]
    fields += [('Quota / job', False), ('API cost / job', False), ('Cash / job', False),
               ('Context', False), ('Allowance left', False)]
    for index, key in enumerate(data['keys'], 1):
        item = data['menu'][key]
        scored = data['scores'][key]
        links = {}
        for ident, efforts in choices:
            preferred = key if ident == identity(item) else max(efforts, key=lambda k: preference(efforts[k]))
            option = efforts[preferred]
            links.setdefault(pool_name(ident[0]), {})[preferred] = name(option) + (
                ' · ' + AGY_ALLOWANCES[ident[0]] if ident[0] in AGY_ALLOWANCES else '')
        options = ''.join(f'<optgroup label="{esc(pool)}">{select(entries, key)}</optgroup>'
                          for pool, entries in links.items())
        funding = {'included': 'Included', 'verified-free': 'Free route', 'metered': 'Metered'}.get(item.get('funding'), 'Funding unknown')
        effort_options = {k: _pretty(o['level']) for k, o in grouped[identity(item)].items()}
        headers.append(f'<th scope="col"><select class="wb-model-picker" aria-label="Model {index}" data-col="{index}">{options}</select>'
                       f'<small>{esc(pool_name(item["pool"]))} · {funding}</small>'
                       f'<select class="wb-effort-picker" name="compare" aria-label="Effort for {esc(name(item))}" '
                       f'data-col="{index}">{select(effort_options, key)}</select>'
                       + _efficient(item.get('efficient_effort') or entries.get(identity(item), {}).get('efficient_effort')
                                    or models.get(item['model_key'], {}).get('efficient_effort'))
                       + ('<small>Not ranked for this role</small>' if not scored else '') + '</th>')
        quote = {**item, **(scored or {})}
        cells = _compare_cells(quote, quote, pools.get(item['pool'], {}), roster)
        cash, context, headroom = (esc(cells[i]) for i in (2, 3, 5))
        quota = _quota_cell(quote, data, scored)
        raw = _money_cell(quote.get('api_cost_usd', (quote.get('cash') or {}).get('api_equivalent_usd')))
        tariff = {'included': 'Included · no extra charge', 'verified-free': 'No charge · free route'}
        if cash != '—':
            cash = f'<span class="wb-number">{cash}</span>'
        elif item.get('funding') in tariff:
            cash = '<small>' + tariff[item['funding']] + '</small>'
        if context != '—':
            context = f'<span class="wb-number">{context}</span>'
        if '% left' in headroom:
            percent, _, detail = headroom.partition(' · ')
            headroom = (f'<span class="wb-number" aria-label="{percent}">{percent.removesuffix(" left")}</span>'
                        + (f'<small>{detail}</small>' if detail else ''))
        columns.append([*[_comparison_grade(data, item, scored, role, policy) for role in grade_roles],
                        quota, raw, cash, context, headroom])
    if columns:
        lines = []
        for (field, active), cells in zip(fields, zip(*columns)):
            lines.append(f'<tr{" class=active" if active else ""}><th scope="row">{field}</th>'
                         + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>')
        matrix = (f'<table class="wb-matrix" data-count="{len(columns)}" aria-label="Model comparison"><thead><tr>'
                  f'<th scope="col">Model / effort</th>{"".join(headers)}</tr></thead><tbody>{"".join(lines)}</tbody></table>')
    else:
        matrix = '<p class="wb-muted">No route to compare for this role.</p>'
    notices = ''.join(f'<li>{esc(reason)}</li>' for reason in data['refusals'])
    mode_switch = ''.join(f'<button type="submit" name="preview-value" value="{mode}" '
                          f'aria-pressed="{str(data["value"] == mode).lower()}">{label}</button>'
                          for mode, label in VALUE_MODES.items())
    why = ('The selected route is archived and hidden.' if data['hidden_pick'] else
           esc(data['why']) if data['pick'] else 'No route clears the selector gates.')
    alternate = f' <b>Alternate:</b> {route(data["runner"])}.' if data['runner'] else ''
    pick = f'{route(data["pick"])}. ' if data['pick'] else ''
    return (
        '<section class="wb" id="compare" aria-labelledby="compare-heading"><div class="wb-head">'
        '<h1 id="compare-heading">Compare models</h1><small>Grades 0–1 · costs by role</small></div>'
        '<form class="wb-query" method="get" action="/#compare"><div class="wb-toolbar">'
        f'<label>Role <select name="role">{select(WORKBENCH_ROLES, data["role"])}</select></label>'
        f'<label>Models <select name="count" aria-label="Number of models to compare">{select({n: str(n) for n in range(1, 5)}, data["count"])}</select></label>'
        f'<input type="hidden" name="value" value="{data["value"]}">'
        f'<div class="wb-mode" role="group" aria-label="Ranking" aria-describedby="wb-mode-help">{mode_switch}</div>'
        '</div><p id="wb-mode-help" class="wb-mode-help">Best value weighs cost and likely success; Absolute best puts likely success first.</p>'
        '<p class="wb-error" role="status"></p>'
        + f'<p class="wb-pick" role="status"><b>{VALUE_MODES[data["value"]]}:</b> {pick}{"(exploring) " if data["exploring"] else ""}{why}{alternate} '
        f'<small>As of {esc(data["as_of"])}.</small></p>' + matrix
        + '<noscript><button type="submit">Compare</button></noscript></form>'
        '<details class="wb-numbers"><summary>What the numbers mean</summary><div class="wb-muted">'
        '<p>The selector converts grades to a success estimate. Quota / job estimates how much of your plan you spend on a job. '
        'API cost / job is the equivalent API price, not what you pay on an included route. A dash means no comparable reading.</p>'
        + (f'<details><summary>Why other routes are out</summary><ul>{notices}</ul></details>' if notices else '')
        + '<p class="wb-value">Save absolute best ' + _work_switch('role-value', False, 'Save absolute best',
                                                  disabled='Per-run preview only.') + '</p></div></details></section>'
    )


def _work_switch(field: str, on: bool, label: str, value: str = '', *, disabled: str = '', hint_id: str = '',
                 state_text: str | None = None) -> str:
    esc = html.escape
    hint_id = 'why-' + esc(hint_id or field)
    hint = f' aria-describedby="{hint_id}" disabled' if disabled else ''
    return (f'<button type="submit" class="sw wb-switch" role="switch" aria-checked="{str(on).lower()}" '
            f'aria-label="{esc(label)}" name="{esc(field)}" value="{esc(value)}"{hint}>'
            f'<span class="track" aria-hidden="true"><i></i></span>'
            f'<span class="st">{esc(state_text or ("On" if on else "Off"))}</span></button>'
            + (f'<span class="wb-muted" id="{hint_id}">{esc(disabled)}</span>' if disabled else ''))


WALLET_PRIMARY = ('claude', 'codex', 'opencode-go', 'antigravity-gemini')
WALLET_TITLES = {'codex': 'Codex', 'claude': 'Claude Max', 'opencode-go': 'OpenCode Go',
                 'chatgpt-work': 'Crossfeed Chat', 'github-copilot-student': 'Copilot Student',
                 'gemini-metered': 'Gemini API', 'openrouter-free': 'OpenRouter Free'}


def _wallet_title(pool):
    key = pool['pool']
    if key in AGY_ALLOWANCES:
        return 'Antigravity'
    return WALLET_TITLES.get(key, pool.get('short_label') or pool['label'])


def _wallet_plan(pool):
    name = pool['plan'].get('name') or ''
    name = name.removeprefix('Owner ')
    name = {'Claude Max 20x': 'Max 20×',
            'ChatGPT Pro, shared with Edo': 'ChatGPT Pro · shared'}.get(name, name)
    if name == _wallet_title(pool):
        name = ''
    about = pool['plan'].get('allowance')
    if about:
        about = re.sub(r'^(≈\s*|about\s+|around\s+|roughly\s+)', '', str(about).strip(), flags=re.I)
    return (f'<small class="wb-plan">{html.escape(name)}</small>' if name else '') + (
        f'<p class="allow">About {html.escape(about)}</p>' if about else '')


def _refresh_form(form_token, return_fields=''):
    return ('<form class="quota-refresh" method="post" action="/refresh">'
            f'<input type="hidden" name="t" value="{html.escape(form_token)}">{return_fields}'
            f'<button type="submit" aria-label="Read the limits again now" title="Read the limits again now">{REFRESH}'
            '</button></form>')


def _agent_brief(overview):
    return ('<section class="agents" aria-labelledby="agents-h"><h2 class="sc" id="agents-h">What agents read</h2>'
            '<div class="brief"><button class="brief-toggle" type="button" aria-expanded="false" aria-controls="agent-brief">Show full</button>'
            '<button class="copy" type="button" aria-label="Copy what agents read">'
            f'{COPY}<span class="cw">Copy</span></button>'
            f'<pre id="agent-brief">{html.escape(_brief(overview))}</pre>'
            f'<pre class="brief-full" hidden>{html.escape(_brief(overview, True))}</pre></div></section>')


def _order_state(order, token):
    return (f'<div class="order-state" hidden data-order="{html.escape(json.dumps(order))}" '
            f'data-token="{html.escape(token)}"></div>')


STATUS_PAGES = {
    'claude': 'https://status.claude.com/', 'codex': 'https://status.openai.com/',
    'chatgpt-work': 'https://status.openai.com/', 'antigravity-gemini': 'https://status.cloud.google.com/',
    'antigravity-3p': 'https://status.cloud.google.com/', 'antigravity-argon': 'https://status.cloud.google.com/',
    'gemini-metered': 'https://aistudio.google.com/status', 'opencode-go': 'https://status.opencode.ai/',
    'openrouter': 'https://status.openrouter.ai/', 'openrouter-free': 'https://status.openrouter.ai/',
    'openrouter-mimo': 'https://status.openrouter.ai/', 'openrouter-metered': 'https://status.openrouter.ai/',
    'github-copilot-student': 'https://www.githubstatus.com/',
}


def _plan_extras(pool, *, show_status=True, empty=True):
    esc = html.escape
    status = _benchmark_url(pool.get('status_url') or STATUS_PAGES.get(pool['pool'])) if show_status else None
    usage = pool.get('usage_cost') or {}
    per_model = pool.get('per_model') or usage.get('per_model') or []

    def date(value):
        if not value:
            return 'not reported'
        try:
            when = fleetctl.parse_iso(value).astimezone()
            return f'{when.day} {when:%b %Y, %H:%M}'
        except (ValueError, TypeError, AttributeError):
            return 'not reported'

    def cash(amount):
        value = _number(amount)
        if value is None:
            return '—'
        return '<span class="wb-number">' + (f'${value:,.2f}' if not 0 < value < .01 else f'${value:.4f}') + '</span>'

    pieces = []
    for field, label in [('credits', 'Credit balance'), ('spend_credits', 'Spend credit balance')]:
        credits = pool.get(field) or {}
        balance = credits.get('balance')
        if balance is None:
            continue
        unit = credits.get('unit') or 'credits'
        amount = f'{balance:,.2f}'.rstrip('0').rstrip('.') if _number(balance) is not None else str(balance)
        pieces.append(f'<p>{label} <b>{esc(amount)} {esc(str(unit))}</b><br>'
                      f'Expires {esc(date(credits.get("expires_at")))} · New credits arrive {esc(date(credits.get("renews_at")))}</p>')
    if _number(usage.get('today_usd')) is not None or _number(usage.get('last_30d_usd')) is not None:
        tokens = ''
        if (_number(usage.get('today_tokens')) is not None and _number(usage.get('last_30d_tokens')) is not None):
            tokens = f'<br><small>{usage["today_tokens"]:,.0f} today · {usage["last_30d_tokens"]:,.0f} tokens in the last 30 days</small>'
        pieces.append(f'<p>Usage cost: {cash(usage.get("today_usd"))} today · '
                      f'{cash(usage.get("last_30d_usd"))} in the last 30 days{tokens}</p>')
    elif empty:
        pieces.append('<p>Usage cost not reported</p>')
    rows = ''.join(f'<li><span class="nm">{esc(str(row.get("model") or "Unknown model"))}</span> '
                   f'{cash(row.get("usd_30d"))} · {esc(str(row.get("requests", "—")))} requests</li>'
                   for row in per_model)
    if rows:
        pieces.append(f'<details><summary>Spend by model · last 30 days</summary><ul>{rows}</ul></details>')
    elif pool.get('per_model_error'):
        pieces.append('<p>Spend by model is not available for this connection.</p>')
    if status:
        pieces.append(f'<a href="{esc(status)}" rel="noreferrer">Status page</a>')
    return ''.join(pieces)


def _wallet(overview, token, workbench, order):
    esc = html.escape
    by_key = {m['model']: m for m in overview['models']}
    roster = overview['_brief_context']['roster']
    archived, lanes = _archived_keys(roster), _lane_statuses(roster)
    efficiencies = {(item['pool'], item['model_key']): item['efficient_effort'] for item in workbench['menu'].values()
                    if item.get('efficient_effort')}
    benchmark_ids = {(item['pool'], item['model_key']): item['benchmark_identity'] for item in workbench['menu'].values()
                     if item.get('benchmark_identity')}
    return_fields = ''.join(f'<input type="hidden" name="return-{field}" value="{esc(value)}">'
                            for field, value in [('role', workbench['role']), ('value', workbench['value']),
                                                 ('count', str(workbench['count'])),
                                                 *[('compare', key) for key in workbench['keys']]])

    def controls(pool):
        key = esc(pool['pool'])
        current = pool['level']
        switch = ''
        if current != 'mixed':
            on = current != 'off'
            switch = ('<form class="wb-pool-switch" method="post" action="/level">'
                      f'{return_fields}<input type="hidden" name="t" value="{esc(token)}">'
                      f'<input type="hidden" name="pool" value="{key}">'
                      + _work_switch('level', on, f'Use {pool["label"]}', 'off' if on else 'normal') + '</form>')
        slider = _slider(pool, esc(token), overview['generated_at'], return_fields)
        caption = LEVEL_WORDS.get(current, 'Mixed')
        policy = (f'<details class="wb-advanced" id="policy-{key}" open><summary>Spend policy '
                  f'<small class="wb-level">{caption}</small></summary>{slider}</details>')
        return switch, policy

    def allowance_row(pool, title, *, card=False, separate_dial=True):
        key = pool['pool']
        pool_id = esc(key)
        options = [dict(o, model_identity=benchmark_ids.get((key, o['model'])) or selector.canonical_model(o['model'], roster),
                        efficient_effort=o.get('efficient_effort') or efficiencies.get((key, o['model'])))
                   for o in pool.get('options') or [] if not o.get('retired_on') and not o.get('archived')
                   and _visible_model(roster, archived, key, o['model'], lanes)]
        pool = dict(pool, options=options)
        tag = 'h2' if card else 'h3'
        container = 'article' if card else 'section'
        explicit = bool(roster['quota_pools'][key].get('explicit_only'))
        included = [_compact_name(by_key.get(o['model'], o), o['model'], key)
                    for o in options if o['current'] and o['run_as']][:3]
        pool_switch, policy = controls(pool) if card and separate_dial else ('', '')
        plan = _wallet_plan(pool) if card else ''
        flag = ('<div class="wb-explicit">Explicit only '
                + _work_switch('explicit-only', explicit, f'{title} explicit-only pool flag',
                               disabled='Set in roster.', hint_id=pool_id) + '</div>')
        inventory = _picker(pool, by_key, token, order, return_fields=return_fields, policy=flag)
        includes = (f'<p class="wb-includes"><small>Includes</small> <span class="nm">'
                    f'{esc(" · ".join(included))}</span></p>') if included else ''
        ticket = ' wb-ticket' if card else ''
        return (f'<{container} class="pool wb-pool-control{ticket} lvl-{pool["level"]}" '
                f'id="pool-{pool_id}" data-pool="{pool_id}" data-search-label="{esc(pool["label"])}" tabindex="-1">'
                + f'<div class="wb-head"><{tag}>{esc(title)}</{tag}>{pool_switch}</div>{plan}' +
                '<p class="wb-error" role="status"></p>'
                f'<div class="gauge" aria-label="{esc(title)} allowance">{_work_gauge(pool) + _pro_usage(pool)}</div>'
                f'<div class="wallet-extras">{_plan_extras(pool, show_status=card, empty=card)}</div>'
                f'{includes if card else ""}{policy}{inventory}</{container}>')

    def antigravity(group):
        members = esc(json.dumps([p['pool'] for p in group]))
        dial = _subscription_group(overview, group)
        # The dial itself includes Off; do not offer a second on/off control for the same plan.
        _, policy = controls(dial)
        return (
            f'<article class="pool provider-group wb-ticket wb-pool-control lvl-{dial["level"]}" id="provider-antigravity" '
            f'data-pool="antigravity" data-pool-members="{members}" data-search-label="Antigravity Google AI Pro" tabindex="-1">'
            '<div class="wb-head"><h2>Google AI Pro</h2></div><small class="wb-plan">Antigravity</small>'
            '<p class="wb-error" role="status"></p>' + policy +
            f'<div class="wallet-extras agy-extras">{_plan_extras({"pool":group[0]["pool"], "status_url":group[0].get("status_url")}, empty=not any(p.get("usage_cost") for p in group))}</div>'
            '<div class="wb-allowances" id="antigravity-allowances">'
            + ''.join(allowance_row(p, AGY_ALLOWANCES[p['pool']]) for p in group) + '</div></article>')

    by_pool = {p['pool']: p for p in overview['pools']}
    agy = [by_pool[key] for key in AGY_ALLOWANCES if key in by_pool]
    ranking = order['ranks']['pools'][order['sort']['pools']]
    cards = []
    ranking = [*WALLET_PRIMARY, *(key for key in ranking if key not in WALLET_PRIMARY)]
    grouped = False
    for key in ranking:
        pool = by_pool.get(key)
        if not pool:
            continue
        if key in AGY_ALLOWANCES:
            if not grouped:
                grouped = True
                cards.append(antigravity(agy))
        else:
            # An illustrative legacy route can share this group's ID but is not a second subscription.
            cards.append(allowance_row(pool, _wallet_title(pool), card=True,
                                       separate_dial=not (key == 'antigravity' and agy)))

    readings = overview['quota_readings']
    stamp = _wallet_stamp(readings)
    return (
        '<section class="wb-wallet" aria-labelledby="wallet-heading"><div class="wb-head wb-wallet-head">'
        '<h2 id="wallet-heading">Allowance by subscription</h2>'
        f'<div class="quota-reading"><p class="stamp">{esc(stamp)}</p>{_refresh_form(token, return_fields)}</div></div>'
        + f'<section class="pools wb-tickets" aria-label="Subscriptions and routes">{"".join(cards)}</section>'
        + '<p class="wb-footnote">Window bars show allowance left. Stale readings are not current capacity.</p>'
        + _archived_list(overview, by_key, token, return_fields) + '</section>'
    )


def _archived_list(overview, by_key, token, return_fields):
    esc = html.escape
    entries = []
    for pool in overview['pools']:
        for row in _sort_model_entries(pool.get('archived', []), by_key):
            key = row['model']
            name = esc(_name(by_key.get(key, row), key))
            actor, words = _level_attribution(row, overview['generated_at'])
            entries.append(
                f'<li><span><b class="nm">{name}</b> · {esc(pool["label"])}'
                f'<small class="archive-actor" data-actor="{actor}">{esc(words)}</small></span>'
                '<form method="post" action="/model-archive">' + return_fields
                + f'<input type="hidden" name="t" value="{esc(token)}">'
                f'<input type="hidden" name="pool" value="{esc(pool["pool"])}">'
                f'<button type="submit" class="wb-add" name="restore" value="{esc(key)}" aria-label="Restore {name}">'
                'Restore</button></form></li>')
    content = '<ul>' + ''.join(entries) + '</ul>' if entries else '<p class="wb-muted">No archived models.</p>'
    return (f'<details class="wb-archived" id="archived"><summary>Archived <small>{len(entries)}</small></summary>'
            f'{content}</details>')


def _wallet_stamp(readings):
    return (f'Latest reading {_ago(readings["newest_age_s"])}' if readings['measured_pools'] else 'No quota readings')


def render_page(overview: dict[str, Any], form_token: str, workbench: dict[str, Any] | None = None) -> str:
    order = overview.get('console_order') or display_order(overview)
    settings = _settings_content(overview, form_token, panel=True)
    if workbench is not None:
        body = (_workbench(overview, workbench) + _wallet(overview, form_token, workbench, order)
                + '<details class="wb-more" id="provider-setup-tools"><summary>Provider setup and history</summary>'
                + provider_setup(overview.get('provider_sources', []), form_token) + _recent_runs(overview)
                + '<details><summary>What agents read</summary>' + _agent_brief(overview) + '</details></details>'
                + '<p class="save-status" role="status" aria-live="polite"></p>' + _onboarding())
        return shell('Compare models', body, settings_content=settings, dashboard=True)

    by_key = {m["model"]: m for m in overview["models"]}
    ranking = order['ranks']['pools'][order['sort']['pools']]
    def pool_row(pool, *, allowance=False):
        pool_id = html.escape(pool["pool"])
        title = AGY_ALLOWANCES[pool['pool']] if allowance else pool['label']
        tag = 'h3' if allowance else 'h2'
        handle = '' if allowance else _handle(title)
        plan = '' if allowance else _plan_lines(pool)
        gauge = _provider_gauge(pool, overview) + _pro_usage(pool)
        return (
            f'<article class="pool lvl-{pool["level"]}" id="pool-{pool_id}" data-pool="{pool_id}" data-search-label="{html.escape(pool["label"])}" tabindex="-1">'
            f'<div class="who"><{tag}>{handle}{_pool_heading(title)}</{tag}>{plan}</div>'
            f'<div class="gauge">{gauge}</div><div class="ctl">{_slider(pool, form_token, overview["generated_at"])}</div>'
            f'{_picker(pool, by_key, form_token, order)}{_pins_note(pool, by_key)}'
            "</article>"
        )
    agy = [p for key in AGY_ALLOWANCES for p in overview['pools'] if p['pool'] == key]
    grouped = False
    rows = []
    for pool in sorted(overview["pools"], key=lambda p: ranking.index(p['pool'])):
        if pool['pool'] not in AGY_ALLOWANCES:
            rows.append(pool_row(pool))
        elif not grouped:
            grouped = True
            title = 'Antigravity (Google AI Pro)'
            members = html.escape(json.dumps([p['pool'] for p in agy]))
            rows.append(
                f'<article class="pool provider-group" id="provider-antigravity" data-pool="{agy[0]["pool"]}" '
                f'data-pool-members="{members}" data-search-label="{title}" tabindex="-1">'
                f'<div class="who"><h2>{_handle(title)}{_pool_heading(title)}</h2>{_plan_lines(agy[0])}</div>'
                f'<p class="allowance-summary">{html.escape(_agy_summary(agy))}</p>'
                f'<details class="allowances" id="antigravity-allowances">'
                f'<summary>Separate allowances: {", ".join(AGY_ALLOWANCES[p["pool"]] for p in agy)}</summary>'
                '<p class="allowance-note">One Google plan, separate limits and spend settings.</p>'
                + ''.join(pool_row(p, allowance=True) for p in agy)
                + '</details></article>'
            )
    body = (
        "<h1>Models and spend</h1>"
        '<p class="lede">Enable models for agents to choose from. Each provider shows its default or how Crossfeed picks. '
        "Slide left to conserve each plan’s quota, right to use more. Agents read these settings before they plan.</p>"
        f'<div class="quota-reading"><p class="stamp">{html.escape(_stamp(overview))}</p>{_refresh_form(form_token)}</div>'
        + provider_setup(overview.get('provider_sources', []), form_token)
        + _sort_control('pools', order['sort']['pools'])
        + '<p class="order-note">Your order changes this page only; Crossfeed still picks by evidence and quota.</p>'
        + _order_state(order, form_token)
        + f'<section class="pools" aria-label="Providers">{"".join(rows)}</section>'
        + _recent_runs(overview) + '<p class="save-status" role="status" aria-live="polite"></p>' + _agent_brief(overview)
    )
    return shell("Providers", body, front=True, settings_content=settings)


PUBLIC_REPO = 'https://github.com/ABCastor/crossfeed-orchestrator'


def _onboarding():
    esc = html.escape

    def copy(label, body, title=''):
        return (f'<div class="connect-copy">{f"<h3>{esc(title)}</h3>" if title else ""}'
                f'<pre tabindex="0"><code>{esc(body)}</code></pre><button type="button" class="wb-add" data-copy-block>'
                f'{label}</button><span role="status" class="copy-status"></span></div>')

    install = (f'git clone {PUBLIC_REPO} "$HOME/crossfeed-orchestrator"\n'
               'cd "$HOME/crossfeed-orchestrator"\n'
               'mkdir -p "$HOME/.config/orchestrator"\n'
               'cfg="$HOME/.config/orchestrator/access-overlay.json"\n'
               '[ -e "$cfg" ] || cp examples/access-overlay.example.json "$cfg"\n'
               'python3 scripts/fleetctl.py doctor\n'
               'python3 scripts/fleetctl.py console')
    assistants = {'Claude Code': '$HOME/.claude/skills', 'Codex': '$HOME/.agents/skills',
                  'Pi': '${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/skills'}
    commands = []
    for name, directory in assistants.items():
        command = (f'skill="{directory}/crossfeed-orchestrator"\n'
                   'mkdir -p "$skill"\n'
                   'cd "$HOME/crossfeed-orchestrator"\n'
                   'ln -s "$PWD/skill/SKILL.md" "$PWD/skill/references" "$PWD/skill/readers" \\\n'
                   '      "$PWD/scripts" "$PWD/docs" "$skill/"')
        commands.append(copy('Copy command', command, name))
    prompt = ('Use the Crossfeed Orchestrator skill. Read my configured routes and current allowances. '
              'Show which model and thinking level you would use for code review and why. '
              'Do not run a model, change settings, or spend anything.')
    return ('<section class="connect" aria-labelledby="connect-heading"><h2 id="connect-heading">Use your AI assistant</h2>'
            '<p>Crossfeed runs on your machine, with the accounts you already use. Install Python 3.9+, bash, jq, '
            'and the provider command-line tools you want, then sign in to those tools.</p>'
            + copy('Copy setup commands', install, 'Install Crossfeed')
            + '<p>In Provider setup, add your own routes and verify only the lanes you can use. '
            'Connect the skill for your assistant, then restart that assistant.</p><div class="connect-tools">'
            + ''.join(commands) + '</div>' + copy('Copy prompt', prompt, 'Try this prompt')
            + f'<p><a href="{PUBLIC_REPO}/blob/main/docs/PORTABLE-USAGE.md">Portable setup guide</a> · '
            f'<a href="{PUBLIC_REPO}/blob/main/docs/SKILL-INSTALL.md">Skill installation guide</a></p></section>'
            '<section class="connect" id="about" aria-labelledby="about-heading"><h2 id="about-heading">About</h2>'
            '<p>Crossfeed Orchestrator routes tasks across your available models. It compares evidence, '
            'allowance and expected cost, and keeps spend controls on your machine.</p>'
            f'<p><a href="{PUBLIC_REPO}">Source and documentation</a></p></section>')


def _benchmark_anchor(model):
    return 'model-' + model


def _benchmark_link(model, name):
    return f'<a class="nm" href="/benchmarks#{urllib.parse.quote(_benchmark_anchor(model), safe="")}">{html.escape(name)}</a>'


def _benchmark_url(url):
    if not isinstance(url, str) or len(url) > 1800 or re.search(r'[\s\\\x00-\x1f\x7f]', url):
        return None
    try:
        target = urllib.parse.urlsplit(url)
        if target.scheme in {'https', 'http'} and target.hostname and not target.username and not target.password:
            return url
    except ValueError:
        return None
    return None


# These pages have public results for the listed model releases, not every entry in the picker.
BENCHMARK_BOARDS = {
    'terminal_bench': ('Terminal-Bench', 'https://www.tbench.ai/leaderboard'),
    'swebench': ('SWE-bench', 'https://www.swebench.com/'),
}
CHECKED_BENCHMARKS = {
    'terminal_bench': ('2026-11-25', {'claude-opus-5-5', 'deepseek-v4-pro', 'deepseek-v4.1-flash'}),
    'swebench': ('2026-11-25', {'claude-opus-4-6', 'claude-opus-4-5', 'deepseek-v3.2'}),
}


def _benchmark_sources(cards, form_token, links, direct_sources, benchmark_page):
    esc = html.escape
    groups = {}
    for row in cards.get('rows', []):
        groups.setdefault(row['model'], []).extend(row.get('outside', []))
    boards = {**benchmark_page.OUTSIDE_RANKING_PAGES, **BENCHMARK_BOARDS}
    for model, rows in groups.items():
        found = {}
        for source in [*rows, *direct_sources.get(model, [])]:
            if isinstance(source, str):
                source = {'url': source}
            if not isinstance(source, dict):
                continue
            name = source.get('source') or source.get('publisher') or ''
            board_name, board_url = boards.get(name, ('', ''))
            url = _benchmark_url(source.get('url') or source.get('source_url') or board_url)
            if url:
                label = source.get('title') or board_name or name or _source_name(url)
                date = source.get('read_on') or source.get('date') or ''
                if url not in found or str(date) > str(found[url][1]):
                    found[url] = (label, date, 'Roster evidence')
        for board, (date, models) in CHECKED_BENCHMARKS.items():
            if model in models:
                label, url = boards[board]
                found.setdefault(url, (label, date, 'Public leaderboard'))
        for entry in links.get(model, []):
            url = _benchmark_url(entry.get('url'))
            if url and url not in found:
                found[url] = (entry.get('label') or _source_name(url),
                              entry.get('set_at', '').split('T', 1)[0], 'Added by you')
        items = ''.join(f'<li><a href="{esc(url)}" rel="noreferrer">{esc(str(label))}</a>'
                        f'<small>{esc(who)} · {esc(str(date)[:10]) if date else "Date not recorded"}</small></li>'
                        for url, (label, date, who) in found.items())
        listing = f'<ul>{items}</ul>' if items else '<p>No linked benchmark sources yet.</p>'
        form = (f'<form class="benchmark-add" method="post" action="/benchmark-link">'
                f'<input type="hidden" name="t" value="{esc(form_token)}">'
                f'<input type="hidden" name="model" value="{esc(model)}">'
                '<label>Add a benchmark link <input type="url" name="url" placeholder="https://…" maxlength="1800" required></label>'
                '<label>Label (optional) <input name="label" maxlength="120"></label>'
                '<button class="wb-add" type="submit">Add link</button></form>') if form_token else ''
        yield _benchmark_anchor(model), f'<section class="benchmark-sources"><h3>Benchmark sources</h3>{listing}{form}</section>'


def _benchmark_data(overview, cards, evidence):
    roster = overview['_brief_context']['roster']
    canonical = lambda key: selector.canonical_model(key, roster)
    hidden = {(pool['pool'], row['model']) for pool in overview['pools'] for row in pool.get('archived', [])}
    active = {canonical(row['model']) for pool in overview['pools'] for row in pool.get('options', [])
              if not row.get('archived') and not row.get('retired_on')}
    excluded = {canonical(key) for _, key in hidden} - active
    rows = [dict(row, sources=[s for s in row.get('sources', []) if (s.get('pool'), s.get('model_key')) not in hidden])
            for row in cards.get('rows', []) if row['model'] not in excluded]
    seen = {row['model'] for row in rows}
    for model in overview['models']:
        key = canonical(model['model'])
        if key in active and key not in seen:
            rows.append({'model': key, 'name': _name(model), 'level': 'provider-default'})
            seen.add(key)
    references = {}
    for (key, _), row in evidence.items():
        references.setdefault(key, []).extend(row.get('sources') or [])
    for lane in roster.get('lanes', []):
        key = lane.get('benchmark_identity')
        if key and key not in seen and canonical(lane['model_key']) in active:
            rows.append({'model': key, 'level': lane.get('benchmark_effort') or 'provider-default'})
            seen.add(key)
    for records in [fleetctl.model_cards(roster), roster.get('model_evidence') or {}]:
        for key, row in records.items():
            for field in ('benchmarks', 'benchmark_links', 'benchmark_sources'):
                value = row.get(field) if isinstance(row, dict) else None
                if isinstance(value, list):
                    references.setdefault(canonical(key), []).extend(value)
    return dict(cards, rows=rows), references


def render_benchmarks(cards: dict[str, Any], form_token='', links=None, direct_sources=None, settings_content='') -> str:
    try:
        from scripts import benchmark_page
    except ImportError:
        import benchmark_page
    body = benchmark_page.render_body(cards)
    panels = dict(_benchmark_sources(cards, form_token, links or {}, direct_sources or {}, benchmark_page))
    # Augment the existing model disclosures. Job picks and provider entries share these anchors.
    body = re.sub(r'(<article id="([^"]+)"><details class="model-disclosure"><summary\b[^>]*>.*?</summary>)',
                  lambda match: match[1] + panels.get(html.unescape(match[2]), ''), body, flags=re.S)
    return shell('Benchmarks', body, benchmarks=True, dashboard=True, settings_content=settings_content)


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

        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None,
                  *, csp: str = CSP) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", csp)
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
            # Some POST refusals happen before reading the body. Do not parse the leftover bytes as a request.
            if self.command == 'POST':
                self.close_connection = True
            self._html(status, message_page(title, text), {'Connection': 'close'} if self.close_connection else None)

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
                if not hmac.compare_digest(key.encode(), console.login_key.encode()):
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
            if url.path not in {"/", "/snapshot", "/settings", "/benchmarks", "/model-cards.json", "/phone"}:
                self._refuse(404, "Nothing here", "This console has one page.")
                return
            if not console.signed_in(self.headers):
                self._refuse(401, "Signed out",
                             "Open the sign-in link printed by this installation's running console.")
                return
            if url.path == '/phone':
                try:
                    view = phone_view.cached_snapshot(fleetctl, console.overlay_path, console.state_dir)
                    body = phone_view.render(view).encode('utf-8')
                except (fleetctl.FleetError, OSError, ValueError):
                    self._refuse(503, 'Readings unavailable', 'Crossfeed could not read the saved quotas and spend.')
                    return
                self._send(200, body, 'text/html; charset=utf-8', csp=phone_view.CSP)
                return
            try:
                if url.path in {'/benchmarks', '/model-cards.json'}:
                    try:
                        from model_cards import read_cards
                    except ImportError:
                        from scripts.model_cards import read_cards
                    roster = fleetctl.read_overlay(console.overlay_path, console.state_dir, discover=False)
                    cards = read_cards(console.state_dir, roster, fleet=fleetctl)
                    if url.path == '/model-cards.json':
                        self._send(200, json.dumps(cards, separators=(',', ':'), allow_nan=False).encode(), 'application/json; charset=utf-8')
                    else:
                        overview = console.overview(refresh=False)
                        runtime = overview['_brief_context']['runtime']
                        evidence = _comparison_evidence(overview['_brief_context']['roster'], console.state_dir)
                        cards, sources = _benchmark_data(overview, cards, evidence)
                        self._html(200, render_benchmarks(cards, console.form_token,
                                                        runtime.get('console_benchmark_links'), sources,
                                                        _settings_content(overview, console.form_token, panel=True)))
                    return
                if url.path == '/':
                    try:
                        query = urllib.parse.parse_qs(url.query, max_num_fields=24)
                        role, value, requested, count = _workbench_query(query)
                    except ValueError as exc:
                        self._refuse(400, 'Invalid comparison', str(exc))
                        return
                fragment = url.path == '/' and query.get('fragment') == ['compare']
                overview = console.overview(refresh=not fragment)
                if url.path == "/snapshot":
                    self._send(200, json.dumps(snapshot_payload(overview)).encode(), "application/json; charset=utf-8")
                    return
                if url.path == "/settings" and urllib.parse.parse_qs(url.query).get('cap') == ['1']:
                    self._send(200, json.dumps({'cap': _settings_cap(overview)}).encode(), "application/json; charset=utf-8")
                    return
                if url.path == '/settings':
                    page = render_settings(overview, console.form_token)
                else:
                    data = workbench_data(overview, console.state_dir, role, value, requested, count)
                    page = keep_html(_workbench(overview, data)) if fragment else render_page(overview, console.form_token, data)
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
            if path not in {'/settings/pro', '/settings/subscriptions', '/level', '/preference', '/refresh',
                            '/model', '/model-archive', '/benchmark-link', '/order', '/provider/probe', '/provider/add', '/provider/remove'}:
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
            level = fields.get('level', [''])[0]
            if path == '/benchmark-link':
                model = fields.get('model', [''])[0]
                try:
                    console.add_benchmark_link(model, fields.get('url', [''])[0], fields.get('label', [''])[0])
                except (ValueError, OSError, fleetctl.FleetError):
                    self._refuse(400, 'Link not saved', 'Use a complete HTTP or HTTPS URL for a listed model.')
                else:
                    self._redirect('/benchmarks#' + urllib.parse.quote(_benchmark_anchor(model), safe=''))
                return
            if path == '/settings/subscriptions':
                try:
                    console.save_subscription({name: values[0] for name, values in fields.items()})
                except (ValueError, OSError, RecursionError, fleetctl.FleetError):
                    self._refuse(400, 'Not saved', 'Check the price, currency, period, renewal date and share.')
                else:
                    self._redirect('/settings#subscriptions')
                return
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
                        if path == '/provider/remove':
                            result['snapshot'] = snapshot_payload(console.overview(refresh=False))
                        self._send(200, json.dumps(result).encode(), "application/json; charset=utf-8")
                    elif path == '/provider/probe':
                        text = result['message'] + " Available IDs: " + ", ".join(result['models'])
                        self._html(200, shell("Connection checked", "<h1>Connection checked</h1><p>" + html.escape(text)
                                             + '</p><p><a href="/">Back to providers</a></p>'))
                    else:
                        self._redirect('/')
                    return
                if path == '/settings/pro':
                    console.set_pro_allowance(pool, fields.get('allowance', [''])[0])
                    if self.headers.get('Accept') != 'application/json':
                        self._redirect('/#settings' if fields.get('return', [''])[0] == 'panel' else '/settings')
                        return
                elif path == '/order':
                    console.set_order(json.loads(fields.get('order', [''])[0]))
                elif path == "/refresh":
                    outcomes = console.refresh_now()
                elif path == "/level":
                    console.set_level(pool, level, reason=fields.get("reason", [None])[0])
                elif path == '/model-archive':
                    archive = fields.get('archive', [''])[0]
                    restore = fields.get('restore', [''])[0]
                    if bool(archive) == bool(restore):
                        raise ValueError('expected an archive or restore action')
                    console.set_archive(pool, archive or restore, bool(archive), reason=fields.get('reason', [None])[0])
                elif path == "/model":
                    change = fields.get("switch", [""])[0]
                    if change:   # "<model>=on" or "<model>=off": the state to set, never a flip
                        model, _, state = change.rpartition("=")
                        if state not in {"on", "off"}:
                            raise ValueError("unknown state")
                        console.set_toggle(pool, model, state == "on", reason=fields.get("reason", [None])[0])
                    else:
                        console.set_choice(pool, fields.get("model", [""])[0], reason=fields.get("reason", [None])[0])
                else:
                    console.set_preference(fields.get("model", [""])[0], fields.get("preference", [""])[0],
                                           reason=fields.get("reason", [None])[0])
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
                if path == '/settings/pro':
                    payload['allowance'] = int(fields['allowance'][0])
                if outcomes is not None:
                    payload["refresh"] = outcomes
                self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
                return
            role = fields.get('return-role', [''])[0]
            value = fields.get('return-value', [''])[0]
            if path in {'/level', '/model', '/model-archive', '/refresh'} and role in WORKBENCH_ROLES and value in VALUE_MODES:
                count = fields.get('return-count', [''])[0]
                params = {'role': role, 'value': value, 'compare': fields.get('return-compare', [])[:4]}
                if count in {'1', '2', '3', '4'}:
                    params['count'] = count
                query = urllib.parse.urlencode(params, doseq=True)
                anchor = ('archived' if path == '/model-archive' and archive else f'pick-{pool}' if path in {'/model', '/model-archive'}
                          else 'provider-antigravity' if path == '/level' and pool == 'antigravity'
                          else f'pool-{pool}' if path == '/level' else 'compare')
                self._redirect(f'/?{query}#{urllib.parse.quote(anchor)}')
                return
            self._redirect('/#archived' if path == '/model-archive' else
                           f'/#pool-{urllib.parse.quote(pool)}' if path in {'/level', '/model'} else '/')

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
