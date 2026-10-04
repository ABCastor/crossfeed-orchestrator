"""Estimated Pro allowance from existing receipts and retained wake attempts."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import re

WEEK = 7 * 86400


def is_pro(lane):
    return lane.get("harness") == "chatgpt-chat" and (
        lane.get("worker_level") == "pro" or lane.get("selector") == "chatgpt:latest-pro")


def _rows(path):
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        yield row
                except ValueError:
                    continue
    except FileNotFoundError:
        return


def _stamp(value):
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo else None
        except ValueError:
            return None
    if type(value) in (int, float) and math.isfinite(value):
        return value / 1000 if value > 100_000_000_000 else value
    return None


def usage(roster, state_dir, pool, *, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    clock = now.timestamp()
    allowance = roster.get("quota_pools", {}).get(pool, {}).get("pro_weekly_allowance", 200)
    if type(allowance) is not int or allowance < 0:
        raise ValueError("pro_weekly_allowance must be a nonnegative integer")
    lanes = [lane for lane in roster.get("lanes", []) if is_pro(lane) and lane.get("quota_pool") == pool]
    selectors = {lane["selector"] for lane in lanes}
    requests, wakes, timestamps, seen = 0, 0, [], set()
    for row in _rows(Path(state_dir) / "runs.jsonl"):
        stamp = _stamp(row.get("ended_at") or row.get("started_at"))
        if (row.get("harness") != "chatgpt-chat" or row.get("returncode") != 0
                or row.get("selector") not in selectors or row.get("quota_pool") not in (None, pool)
                or stamp is None):
            continue
        identity = (row.get("selector"), row.get("idempotency_key") or row.get("run_id") or stamp)
        if identity in seen:
            continue
        seen.add(identity)
        if clock - WEEK < stamp <= clock:
            requests += 1
            timestamps.append(stamp)
    # Several lanes share one wake log. Read each path once, only counting wakes
    # that sent the polling prompt (including later registration failures).
    try:
        from chatgpt_workers import wake_log_file, wake_state_file
    except ImportError:
        from scripts.chatgpt_workers import wake_log_file, wake_state_file
    paths = {}
    wake_pause = 0
    for lane in lanes:
        try:
            wake_state = json.loads(wake_state_file(lane).read_text())
            wake_pause = max(wake_pause, _stamp(wake_state.get("pro_cooldown_until")) or 0)
        except FileNotFoundError:
            pass
        path = wake_log_file(lane)
        paths.setdefault(path, set()).add(lane.get("worker_label"))
    for path, labels in paths.items():
        for row in _rows(path):
            stamp = _stamp(row.get("ts"))
            sent = row.get("stage") in {
                "worker registration", "new conversation retention", "previous conversation archive"}
            if (row.get("label") in labels and sent and not row.get("refunded")
                    and stamp is not None and clock - WEEK < stamp <= clock):
                wakes += 1
                timestamps.append(stamp)
    total = requests + wakes
    resets = dt.datetime.fromtimestamp(min(timestamps) + WEEK, dt.timezone.utc).isoformat() if timestamps else None
    return {"requests": requests, "wakes": wakes, "used": total, "allowance": allowance,
            "remaining": max(0, allowance - total), "estimate": True,
            "spent": total >= allowance, "next_rolloff_at": resets,
            "wake_paused_until": wake_pause}


def refresh(roster, state_dir, *, now=None):
    pools = {lane["quota_pool"] for lane in roster.get("lanes", []) if is_pro(lane)}
    meters = {}
    for pool in pools:
        try:
            meters[pool] = usage(roster, state_dir, pool, now=now)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # An optional Chat transport must not disable the rest of the fleet.
            # Unknown local allowance state cannot admit more Pro work either.
            meters[pool] = {"estimate": True, "unavailable": True, "spent": False}
    roster["chatgpt_pro_usage"] = meters


def blocked(roster, runtime, lane, *, now=None):
    if not is_pro(lane):
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    pool = lane["quota_pool"]
    try:
        from fleetctl import model_blocked
    except ImportError:
        from scripts.fleetctl import model_blocked
    if model_blocked(roster, runtime, pool, lane["model_key"]):
        return "Pro switched off by owner"
    if roster.get("chatgpt_pro_usage", {}).get(pool, {}).get("unavailable"):
        return "Pro estimate unavailable; allowance configuration or local state unreadable"
    block = runtime.get("chatgpt_pro_blocks", {}).get(pool, {})
    until = _stamp(block.get("until"))
    if until and until > now.timestamp():
        return "Pro downgrade or rate limit reported; paused until " + block["until"]
    status = lane.get("gateway_status", {})
    if status.get("quota_blocked") or status.get("rate_limited"):
        return "Pro paused by Crossfeed Chat"
    if roster.get("chatgpt_pro_usage", {}).get(pool, {}).get("wake_paused_until", 0) > now.timestamp():
        return "Pro wake paused by a reported rate limit"
    if roster.get("chatgpt_pro_usage", {}).get(pool, {}).get("spent"):
        return "Pro rolling 7-day allowance estimate spent"
    return None


def replacements(roster, runtime, lane):
    """Same account's current Extra High, then High. Respect every model switch."""
    try:
        from fleetctl import model_blocked
    except ImportError:
        from scripts.fleetctl import model_blocked
    found = []
    for level in ("xhigh", "high"):
        for item in roster.get("lanes", []):
            if (item.get("harness") == "chatgpt-chat" and item.get("quota_pool") == lane["quota_pool"]
                    and item.get("worker_level") == level
                    and item.get("worker_row", "Latest").casefold() == "latest"
                    and item.get("access_status") == "verified" and item.get("admission_status") == "active"
                    and not item.get("gateway_status", {}).get("quota_blocked")
                    and not item.get("gateway_status", {}).get("rate_limited")
                    and not model_blocked(roster, runtime, item["quota_pool"], item["model_key"])):
                found.append(item)
    return found


def fallback(lane, reason):
    return {"requested_model": lane["model_key"], "reason": reason,
            "order": ["xhigh", "high"]}


def reset_at(message, value=None, *, now=None):
    """Use an explicit timestamp or unambiguous relative reset, otherwise 24 h."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = _stamp(value)
    if stamp is None:
        match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})", message or "")
        stamp = _stamp(match[0]) if match else None
    if stamp is None:
        match = re.search(r"(?:reset|retry|try again|available again|paused)[^\n.]{0,40}?\b(?:in|for)\s+(\d+)\s*(minutes?|hours?|days?)", message or "", re.I)
        if match:
            stamp = now.timestamp() + int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2][0].lower()]
    if stamp is None or stamp <= now.timestamp():
        stamp = now.timestamp() + 86400
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat().replace("+00:00", "Z")


RATE_LIMIT = re.compile(r"rate[ -]?limit|quota_blocked|rate_limited|(?:you(?:'ve| have)|pro)[^\n]{0,50}(?:reached|exceeded|exhausted)[^\n]{0,30}(?:limit|quota|allowance)", re.I)
LIMIT_REPORT = re.compile(
    r"(?:you(?:['’]ve| have)|i(?:['’]ve| have))\s+(?:reached|exceeded|hit)[^\n.]{0,60}(?:limit|quota|allowance)"
    r"|^(?:ChatGPT\s+)?rate[ -]?limit(?:ed|\s+(?:reached|exceeded))"
    r"|\b(?:I|this chat)\b[^\n.]{0,40}(?:switched|downgraded)[^\n.]{0,50}(?:Thinking|Medium)",
    re.I | re.M)


def mark_spent(state_dir, lane, *, until=None):
    try:
        from fleetctl import locked_runtime
    except ImportError:
        from scripts.fleetctl import locked_runtime
    until = reset_at("", until)
    with locked_runtime(Path(state_dir)) as runtime:
        blocks = runtime.setdefault("chatgpt_pro_blocks", {})
        old = blocks.get(lane["quota_pool"], {}).get("until")
        blocks[lane["quota_pool"]] = {"until": max(old or until, until), "source": "pro-worker-downgrade"}


def text(meter):
    if meter.get("unavailable"):
        return "Pro estimate unavailable; check allowance configuration and local usage/wake state"
    return (f"Pro estimate: {meter['used']}/{meter['allowance']} in rolling 7 days "
            f"({meter['requests']} answered requests + {meter['wakes']} wakes); "
            f"{meter['remaining']} estimated remaining")
