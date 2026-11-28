"""Panel preferences: which tabs and tiles the menu and the web panel show, and in what order.

One file, `~/.config/orchestrator/panel.json` (XDG; `FLEET_PANEL_PREFS` overrides), read by the
Mac menu app and by the Linux web panel through the console. Written only by
`fleetctl panel set` and by the console's POST /panel/prefs, both through `save()` here, so the
two can never disagree about what a legal document is.

The file is mode 600 and replaced atomically: a reader never sees half a document. Unknown keys
are preserved, so a newer writer's field survives a round trip through an older reader. Every
read fills the defaults the operator has not set; nothing is invented beyond the shape below.

    {"schema": 1, "show": "used", "appearance": "system",
     "tabs": ["overview", "claude", "codex", "copilot", "antigravity", "opencode",
              "openrouter-free", "gemini-metered", "chatgpt-work", "other"],
     "hidden_tabs": [],
     "tiles": {"overview": ["alert", "providers", "subscriptions", "tokens", "links"],
               "claude": ["limits", "resets", "credits", "tokens", "policy", "status", "links"]},
     "hidden_tiles": {"claude": ["credits"]},
     "menubar": {"icon": "provider", "show": "pressed", "window": "tightest", "count": "used",
                 "text": "window", "extra": "none", "reset": "countdown",
                 "notify_low": false, "notify_reset": false},
     "refresh": {"on_open": true, "every_minutes": 5},
     "open_on": "last", "last_tab": "overview",
     "budgets": {"openrouter-free": 20},
     "tile_options": {"claude/tokens": {"metric": "tokens", "days": 14}}}

`appearance` is system, light or dark. `menubar` is what the menu bar item draws: its icon (the
Crossfeed mark, the logo of the provider the title is about, or none) and its text. `refresh` says
whether opening the menu reads again when the data is older than a minute, and how often a background
reading runs in minutes (0 is off, at most 1440). `open_on` is `last` (the tab used last, kept in `last_tab`) or a tab id.
`budgets` are the operator's monthly USD figure per API pool that reports money (not Crossfeed Chat):
shown against spend, never enforced.
`tile_options` is the sparkline's metric (tokens, cost, requests) and range (7, 14 or 30 days) per tile.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = 1
SHOWS = ("used", "left")
DEFAULT_SHOW = "used"
# The provider order the menu ships with; a tab id is any pool or group id the glance knows.
# Pools billed per token, by prepaid credit or through a relay are first-class tabs, named by pool key.
API_TABS = ["openrouter-free", "gemini-metered", "chatgpt-work"]
DEFAULT_TABS = ["overview", "claude", "codex", "copilot", "antigravity", "opencode", *API_TABS, "other"]
# Overview's Status tile is not in the default list: it appears by itself while an incident exists, and is
# pinned (shown with none) only when the operator adds it to the list.
DEFAULT_OVERVIEW_TILES = ["alert", "providers", "subscriptions", "tokens", "links"]
DEFAULT_PROVIDER_TILES = ["limits", "resets", "credits", "tokens", "policy", "status", "links"]
DEFAULT_API_TILES = ["limits", "spend", "balance", "models", "activity", "status", "links"]
# Crossfeed Chat is a relay that reports no money: no spend, no balance, no budget.
RELAY_TABS = ("chatgpt-work",)
DEFAULT_RELAY_TILES = ["limits", "activity", "models", "status", "links"]
BUILTIN_TILES = ("alert", "providers", "subscriptions", "tokens", "links", "status",
                 "limits", "resets", "credits", "policy", "other",
                 "spend", "balance", "models", "activity")
APPEARANCES = ("system", "light", "dark")
DEFAULT_APPEARANCE = "system"
MENUBAR_ICONS = ("crossfeed", "provider", "none")
MENUBAR_TEXTS = ("full", "window", "number", "none")
MENUBAR_EXTRAS = ("none", "reset", "pace", "cost")
MENUBAR_RESETS = ("countdown", "clock")
MENUBAR_WINDOWS = ("tightest", "5h", "Week", "Month")
DEFAULT_MENUBAR = {"icon": "provider", "show": "pressed", "window": "tightest", "count": "used",
                   "text": "window", "extra": "none", "reset": "countdown",
                   "notify_low": False, "notify_reset": False}
MAX_REFRESH_MINUTES = 1440
DEFAULT_REFRESH = {"on_open": True, "every_minutes": 5}
METRICS = ("tokens", "cost", "requests")
RANGES = (7, 14, 30)
MAX_BUDGET_USD = 1_000_000
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PLUGIN_TILE = re.compile(r"^plugin:[a-z0-9][a-z0-9-]{0,39}/[a-z0-9][a-z0-9-]{0,39}$")


class PrefsError(ValueError):
    """A document that cannot mean anything: refused rather than half-applied."""


def path() -> Path:
    """The one file: the override, else the XDG config home, else ~/.config."""
    override = os.environ.get("FLEET_PANEL_PREFS")
    if override:
        return Path(override).expanduser()
    root = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return (Path(root).expanduser() / "orchestrator" / "panel.json")


def default_tiles(tab: str) -> list[str]:
    if tab == "overview":
        return list(DEFAULT_OVERVIEW_TILES)
    if tab == "other":
        return ["other"]
    if tab in RELAY_TABS:
        return list(DEFAULT_RELAY_TILES)
    if tab in API_TABS:
        return list(DEFAULT_API_TILES)
    return list(DEFAULT_PROVIDER_TILES)


def _tab(value: Any) -> str | None:
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def _tile(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value in BUILTIN_TILES or PLUGIN_TILE.fullmatch(value) else None


def _tile_list(value: Any) -> list[str]:
    """Order kept, duplicates and unknown ids dropped: an unknown tile renders nothing, so drop it."""
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            tile = _tile(item)
            if tile and tile not in out:
                out.append(tile)
    return out


def _tab_list(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            tab = _tab(item)
            if tab and tab not in out:
                out.append(tab)
    return out


def _menubar(value: Any) -> dict[str, Any]:
    """The menu bar item's choices: known keys checked, unknown keys kept, missing ones defaulted."""
    given = value if isinstance(value, dict) else {}
    out: dict[str, Any] = dict(given)
    choices = {"icon": MENUBAR_ICONS, "text": MENUBAR_TEXTS, "extra": MENUBAR_EXTRAS,
               "reset": MENUBAR_RESETS, "window": MENUBAR_WINDOWS, "count": SHOWS}
    for key, allowed in choices.items():
        out[key] = given.get(key) if given.get(key) in allowed else DEFAULT_MENUBAR[key]
    shown = given.get("show")
    out["show"] = shown if shown == "pressed" or _tab(shown) else DEFAULT_MENUBAR["show"]
    for key in ("notify_low", "notify_reset"):
        out[key] = given.get(key) if isinstance(given.get(key), bool) else DEFAULT_MENUBAR[key]
    return out


def _refresh(value: Any) -> dict[str, Any]:
    """How the menu reads again; unknown keys are kept, as in `menubar`."""
    given = value if isinstance(value, dict) else {}
    every = given.get("every_minutes")
    return {**given,
            "on_open": given["on_open"] if isinstance(given.get("on_open"), bool) else DEFAULT_REFRESH["on_open"],
            "every_minutes": every if type(every) is int and 0 <= every <= MAX_REFRESH_MINUTES else DEFAULT_REFRESH["every_minutes"]}


def _budgets(value: Any) -> dict[str, float]:
    """A monthly USD figure per pool; anything that is not a positive finite number is no budget."""
    out: dict[str, float] = {}
    if isinstance(value, dict):
        for pool, amount in value.items():
            key = _tab(pool)
            if (key and key not in RELAY_TABS and type(amount) in (int, float) and amount == amount and 0 < amount <= MAX_BUDGET_USD
                    and key not in out):
                out[key] = round(float(amount), 2) if float(amount) != int(amount) else int(amount)
    return out


def _tile_options(value: Any) -> dict[str, dict[str, Any]]:
    """`<tab>/<tile>` -> sparkline metric and range; an option that is not one of the choices is dropped."""
    out: dict[str, dict[str, Any]] = {}
    if isinstance(value, dict):
        for key, option in value.items():
            if not (isinstance(key, str) and isinstance(option, dict)):
                continue
            tab, _, tile = key.partition("/")
            if not (_tab(tab) and (tile in BUILTIN_TILES or PLUGIN_TILE.fullmatch(tile))):
                continue
            kept: dict[str, Any] = {}
            if option.get("metric") in METRICS:
                kept["metric"] = option["metric"]
            if type(option.get("days")) is int and option["days"] in RANGES:
                kept["days"] = option["days"]
            if kept:
                out[key] = kept
    return out


def normalize(doc: Any, *, keep_unknown: bool = True) -> dict[str, Any]:
    """The document as both platforms mean it: defaults filled, unreadable pieces replaced.

    Unknown keys survive (a newer writer's field), because this file has two writers and one
    reader that must never delete what the other wrote.
    """
    if not isinstance(doc, dict):
        raise PrefsError("panel preferences must be a JSON object")
    out: dict[str, Any] = dict(doc) if keep_unknown else {}
    out["schema"] = SCHEMA

    show = doc.get("show")
    out["show"] = show if show in SHOWS else DEFAULT_SHOW

    tabs = _tab_list(doc.get("tabs"))
    for tab in DEFAULT_TABS:  # absent ids are appended, not shown as a gap
        if tab not in tabs:
            tabs.append(tab)
    if "overview" in tabs:  # Overview is the door and Other the drawer: neither may move to an edge
        tabs.remove("overview")
    tabs.insert(0, "overview")
    if "other" in tabs:
        tabs.remove("other")
    tabs.append("other")
    out["tabs"] = tabs

    hidden = [tab for tab in _tab_list(doc.get("hidden_tabs")) if tab in tabs and tab != "overview"]
    out["hidden_tabs"] = hidden

    tiles: dict[str, list[str]] = {}
    raw_tiles = doc.get("tiles") if isinstance(doc.get("tiles"), dict) else {}
    for tab, value in raw_tiles.items():
        key = _tab(tab)
        if key and key not in tiles:
            tiles[key] = _tile_list(value)
    for tab in tabs:  # every tab in the order has a list, so a client never has to invent one
        tiles.setdefault(tab, default_tiles(tab))
        # A list saved before the Status tile existed has none: it goes last before Links, where new lists put it.
        if tab not in ("other", "overview") and "status" not in tiles[tab] and tab in raw_tiles:
            at = tiles[tab].index("links") if "links" in tiles[tab] else len(tiles[tab])
            tiles[tab].insert(at, "status")
    out["tiles"] = tiles

    hidden_tiles: dict[str, list[str]] = {}
    raw_hidden = doc.get("hidden_tiles") if isinstance(doc.get("hidden_tiles"), dict) else {}
    for tab, value in raw_hidden.items():
        key = _tab(tab)
        if key and key not in hidden_tiles:
            hidden_tiles[key] = _tile_list(value)
    out["hidden_tiles"] = hidden_tiles

    out["appearance"] = doc.get("appearance") if doc.get("appearance") in APPEARANCES else DEFAULT_APPEARANCE
    out["menubar"] = _menubar(doc.get("menubar"))
    out["refresh"] = _refresh(doc.get("refresh"))
    open_on = doc.get("open_on")
    out["open_on"] = open_on if open_on == "last" or (_tab(open_on) and open_on in tabs) else "last"
    last = doc.get("last_tab")
    out["last_tab"] = last if _tab(last) and last in tabs and last not in hidden else "overview"
    out["budgets"] = _budgets(doc.get("budgets"))
    out["tile_options"] = _tile_options(doc.get("tile_options"))
    return out


def load(prefs_path: Path | None = None) -> dict[str, Any]:
    """The document on disk with defaults filled; a missing or unreadable file is the defaults."""
    target = prefs_path or path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raw = {}
    try:
        return normalize(raw if isinstance(raw, dict) else {})
    except PrefsError:
        return normalize({})


def save(doc: Any, prefs_path: Path | None = None) -> dict[str, Any]:
    """Validate, fill defaults, then replace the file atomically as mode 600. Returns what was written."""
    normalized = normalize(doc)
    target = prefs_path or path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return normalized


def visible_tiles(doc: dict[str, Any], tab: str) -> list[str]:
    """The tiles a tab draws, in order: its list minus the ones the operator turned off."""
    order = (doc.get("tiles") or {}).get(tab) or default_tiles(tab)
    off = set((doc.get("hidden_tiles") or {}).get(tab) or [])
    return [tile for tile in order if tile not in off]


def apply_change(doc: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """One operator edit, expressed as a partial document, applied to the current one.

    The web panel posts the whole document it holds, so this exists only for a caller that
    wants to move one thing (a reorder, a toggle) and cannot race another writer's edit.
    """
    merged = dict(doc)
    for key in ("show", "tabs", "hidden_tabs", "tiles", "hidden_tiles", "appearance", "menubar", "refresh",
                "open_on", "last_tab", "budgets", "tile_options"):
        if key not in change:
            continue
        value = change[key]
        if key in ("tiles", "hidden_tiles", "menubar", "refresh", "budgets", "tile_options") \
                and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return normalize(merged)
