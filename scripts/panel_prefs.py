"""Panel preferences: which tabs and tiles the menu and the web panel show, and in what order.

One file, `~/.config/orchestrator/panel.json` (XDG; `FLEET_PANEL_PREFS` overrides), read by the
Mac menu app and by the Linux web panel through the console. Written only by
`fleetctl panel set` and by the console's POST /panel/prefs, both through `save()` here, so the
two can never disagree about what a legal document is.

The file is mode 600 and replaced atomically: a reader never sees half a document. Unknown keys
are preserved, so a newer writer's field survives a round trip through an older reader. Every
read fills the defaults the operator has not set; nothing is invented beyond the shape below.

    {"schema": 1, "show": "used",
     "tabs": ["overview", "claude", "codex", "copilot", "antigravity", "opencode", "other"],
     "hidden_tabs": [],
     "tiles": {"overview": ["alert", "providers", "subscriptions", "tokens", "links"],
               "claude": ["limits", "resets", "credits", "tokens", "policy", "links"]},
     "hidden_tiles": {"claude": ["credits"]}}
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
DEFAULT_TABS = ["overview", "claude", "codex", "copilot", "antigravity", "opencode", "other"]
DEFAULT_OVERVIEW_TILES = ["alert", "providers", "subscriptions", "tokens", "links"]
DEFAULT_PROVIDER_TILES = ["limits", "resets", "credits", "tokens", "policy", "links"]
BUILTIN_TILES = ("alert", "providers", "subscriptions", "tokens", "links",
                 "limits", "resets", "credits", "policy", "other")
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
    out["tiles"] = tiles

    hidden_tiles: dict[str, list[str]] = {}
    raw_hidden = doc.get("hidden_tiles") if isinstance(doc.get("hidden_tiles"), dict) else {}
    for tab, value in raw_hidden.items():
        key = _tab(tab)
        if key and key not in hidden_tiles:
            hidden_tiles[key] = _tile_list(value)
    out["hidden_tiles"] = hidden_tiles
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
    for key in ("show", "tabs", "hidden_tabs", "tiles", "hidden_tiles"):
        if key not in change:
            continue
        value = change[key]
        if key == "tiles" and isinstance(value, dict) and isinstance(merged.get("tiles"), dict):
            merged["tiles"] = {**merged["tiles"], **value}
        elif key == "hidden_tiles" and isinstance(value, dict) and isinstance(merged.get("hidden_tiles"), dict):
            merged["hidden_tiles"] = {**merged["hidden_tiles"], **value}
        else:
            merged[key] = value
    return normalize(merged)
