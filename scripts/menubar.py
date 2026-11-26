"""The limits at a glance, from the fleet overview: one menu-bar line and a dropdown, or plain text.

Reads only what `fleet_overview` already built from the runtime snapshot, the same evidence the
console and `brief` show, so the menu bar can never disagree with routing. Rendering is pure: no
refresh, no network, no clock except the one passed in.

The menu-bar line names the lead pool and every pool that is full or nearly full; the dropdown
lists every measured pool with each of its limits, when it resets and its pace (how far usage is
ahead of or behind an even spend to the reset, as CodexBar shows it), and says plainly when a
reading failed or is old.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

HOT_PERCENT = 80
OLD_READING_S = 2 * 3600
RED, ORANGE, GREY = "#d70015", "#c76a00", "#8a8a8a"


def short_name(pool: dict[str, Any]) -> str:
    """The pool's `short_label` when the overlay declares one, else 'Antigravity · Gemini' -> 'Gemini',
    'Codex (ChatGPT)' -> 'Codex'; Antigravity keeps its prefix where the family alone would be read
    as another provider ('AGY Claude/GPT')."""
    declared = pool.get("short_label")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()[:20]
    label = str(pool.get("label") or pool.get("pool") or "")
    head, _, tail = label.rpartition("·")
    name = re.sub(r"\s*\(.*?\)\s*", " ", tail).strip() or label
    name = name.replace(" and ", "/")
    if head.strip().lower() == "antigravity" and re.search(r"claude|gpt", name, re.I):
        name = "AGY " + name
    return name


def _general(limits: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [l for l in limits if not l.get("scope")]


def tightest(pool: dict[str, Any]) -> dict[str, Any] | None:
    """The limit that stops the pool first: the fullest whole-plan limit, the sooner reset on a tie.
    A provider refusal counts as full."""
    limits = [l for l in pool.get("limits") or [] if l.get("kind") in {"window", "refusal", "budget"}
              and not l.get("retired") and l.get("active") is not False]
    candidates = _general(limits) or limits
    if not candidates:
        return None
    return max(candidates, key=lambda l: (int(l.get("used_percent") or 0),
                                          -(l.get("resets_in_s") if l.get("resets_in_s") is not None else 10**12)))


def is_full(pool: dict[str, Any], limit: dict[str, Any] | None) -> bool:
    mine = pool.get("limits") or []
    if mine and all(l.get("retired") for l in mine):
        return False   # only retired models' windows: nothing there can run out
    return pool.get("state") == "EXHAUSTED" or bool(limit and int(limit.get("used_percent") or 0) >= 100)


def _percent_words(pool: dict[str, Any], limit: dict[str, Any] | None) -> str:
    if is_full(pool, limit):
        return "full"
    return f"{int(limit.get('used_percent') or 0)}%" if limit else "?"


# BR-SELECT-002
def pace_words(pace: dict[str, Any] | None) -> str:
    """'17% surplus', '21% in deficit, runs out in 5d 3h', 'on pace': worded once, by the overview."""
    return str((pace or {}).get("words") or "")


def _limit_words(limit: dict[str, Any]) -> str:
    title = str(limit.get("title") or "Limit")
    if limit.get("scope"):
        title += f" ({limit['scope']})"
    if limit.get("active") is False:
        return f"{title} · {limit.get('note') or 'not running'}"
    used = "full" if int(limit.get("used_percent") or 0) >= 100 else f"{int(limit.get('used_percent') or 0)}%"
    resets = limit.get("resets")
    pace = pace_words(limit.get("pace"))
    return f"{title} {used}" + (f" · resets {resets}" if resets else "") + (f" · {pace}" if pace else "")


def _ago(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "a while"
    seconds = int(seconds)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


SOURCE_WORDS = {"codexbar": "CodexBar", "opencode-go-api": "OpenCode", "codex-app-server": "Codex",
                "claude-oauth-usage": "Claude", "antigravity-cli": "Antigravity", "copilot-api": "GitHub"}


def rows(overview: dict[str, Any], lead: str | None = None, *, include_unmeasured: bool = False) -> list[dict[str, Any]]:
    """One row per pool that has a reading, a failed gauge or a provider refusal; the lead pool first."""
    out = []
    for pool in overview.get("pools") or []:
        limits = [l for l in pool.get("limits") or [] if l.get("kind") in {"window", "refusal"}]
        if not include_unmeasured and not limits and not pool.get("gauge_error"):
            continue
        limit = tightest(pool)
        out.append({
            "pool": pool.get("pool"), "name": short_name(pool), "label": pool.get("label"),
            "level": pool.get("level"), "state": pool.get("state"), "measured": bool(pool.get("measured")),
            "full": is_full(pool, limit), "used_percent": int(limit["used_percent"]) if limit else None,
            "resets": (limit or {}).get("resets") or "", "reset_at": (limit or {}).get("reset_at"),
            "pace": (limit or {}).get("pace"),
            "limits": limits, "gauge_error": pool.get("gauge_error"), "stale_age_s": pool.get("stale_age_s"),
        })
    out.sort(key=lambda row: row["pool"] != lead)
    return out


def title(overview: dict[str, Any], lead: str | None) -> str:
    """Short enough for a menu bar: the lead pool's percentage, every pool at least HOT_PERCENT used,
    then the full ones by name ('Claude 26% · Go 85% · Codex, Argon used up')."""
    parts, full = [], []
    for row in rows(overview, lead):
        if row["full"]:
            (parts.append(f"{row['name']} used up") if row["pool"] == lead else full.append(row["name"]))
        elif row["pool"] == lead or (row["used_percent"] or 0) >= HOT_PERCENT:
            parts.append(f"{row['name']} {row['used_percent']}%" if row["used_percent"] is not None else f"{row['name']} ?")
    if full:
        parts.append(", ".join(full) + " used up")
    return " · ".join(parts) or "Limits OK"


def _error_words(row: dict[str, Any]) -> str | None:
    error = row.get("gauge_error")
    if not error:
        return None
    reason = error.get("reason") if isinstance(error, dict) else str(error)
    reason = " ".join(str(reason or "no reading").split())[:90]
    return f"{row['name']}: not read now ({reason})"


def _escape(text: str) -> str:
    """SwiftBar splits a line at the first '|'; keep the text whole."""
    return text.replace("|", "/")


def swiftbar(overview: dict[str, Any], lead: str | None, plugin: str | None = None) -> str:
    lines = [_escape(title(overview, lead)), "---"]
    for row in rows(overview, lead):
        color = RED if row["full"] else (ORANGE if (row["used_percent"] or 0) >= HOT_PERCENT else None)
        head = f"{row['label']}  {'full' if row['full'] else (str(row['used_percent']) + '%' if row['used_percent'] is not None else 'not read')}"
        if row["resets"]:
            head += f" · resets {row['resets']}"
        if row["pace"] and not row["full"]:
            head += f" · {pace_words(row['pace'])}"
        if row["level"] and row["level"] not in {"normal"}:
            head += f" · set to {row['level']}"
        lines.append(_escape(head) + (f" | color={color}" if color else ""))
        for limit in row["limits"]:
            lines.append("--" + _escape(_limit_words(limit)))
        error = _error_words(row)
        if error:
            lines.append(_escape(error) + f" | color={ORANGE}")
        elif isinstance(row["stale_age_s"], (int, float)) and row["stale_age_s"] > OLD_READING_S:
            lines.append(_escape(f"{row['name']}: last reading {_ago(row['stale_age_s'])}") + f" | color={ORANGE}")
    readings = overview.get("quota_readings") or {}
    lines.append("---")
    sources = ", ".join(SOURCE_WORDS.get(s, s) for s in readings.get("sources") or [])
    if readings.get("measured_pools"):
        lines.append(_escape(f"Read {_ago(readings.get('newest_age_s'))} from {sources}") + f" | color={GREY}")
    else:
        lines.append(f"No limit could be read | color={ORANGE}")
    if plugin:
        lines.append(f"Read the limits again now | bash={plugin} param1=--force terminal=false refresh=true")
    return "\n".join(lines) + "\n"


def text(overview: dict[str, Any], lead: str | None) -> str:
    lines = [title(overview, lead)]
    for row in rows(overview, lead):
        used = "full" if row["full"] else ("not read" if row["used_percent"] is None else f"{row['used_percent']}%")
        pace = pace_words(row["pace"]) if not row["full"] else ""
        lines.append(f"{row['label']}: {used}" + (f", resets {row['resets']}" if row["resets"] else "")
                     + (f", {pace}" if pace else ""))
        for limit in row["limits"]:
            lines.append("  " + _limit_words(limit))
        error = _error_words(row)
        if error:
            lines.append("  " + error)
    return "\n".join(lines) + "\n"


def as_json(overview: dict[str, Any], lead: str | None, spend: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema": 1, "title": title(overview, lead), "pools": rows(overview, lead, include_unmeasured=True),
            "readings": overview.get("quota_readings") or {}, "subscription_spend": spend or {
                "monthly": [], "unpriced": 0, "unsplit": 0, "unavailable": False}}
