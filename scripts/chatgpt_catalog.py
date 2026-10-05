"""Expand one gateway template from current capability evidence, never aliases."""
from __future__ import annotations

import copy
import time

try:
    from chatgpt_transport import Rejected, catalog_models, contacts, request, settings
except ImportError:
    from scripts.chatgpt_transport import Rejected, catalog_models, contacts, request, settings

STATES = {"sleeping", "probed-ok", "unavailable"}


def expand(roster, state_dir, *, persist=False):
    # This optional transport must never become an admission prerequisite for
    # other lanes. Configuration and cache failures share the same boundary as
    # a rejected live catalog, with fixed diagnostics instead of exception data.
    if "chatgpt_gateway" not in roster and not any(
            lane.get("gateway_service") for lane in roster.get("lanes", [])):
        return roster
    try:
        return _expand(roster, state_dir, persist=persist)
    except (Rejected, KeyError, TypeError, ValueError, AttributeError, OSError):
        return _closed_roster(roster, "gateway configuration or catalog cache is invalid or unreadable")


def _closed_roster(roster, error, error_code=6):
    result = copy.deepcopy(roster)
    closed = set()
    states = {}
    for lane in result.get("lanes", []):
        if lane.get("gateway_service") or (lane.get("harness") == "chatgpt-chat"
                                           and lane.get("lane_id", "").startswith("chatgpt:")):
            lane.update(access_status="unverified", admission_status="rejected",
                        verified_at=None, catalog_state="unavailable")
            closed.add(lane["lane_id"])
            states[lane["selector"]] = "unavailable"
    for bands in result.get("routing", {}).get("roles", {}).values():
        for band, ranking in bands.items():
            if isinstance(ranking, list):
                bands[band] = [item for item in ranking if item != "chatgpt" and item not in closed]
    result["chatgpt_catalog"] = {"error": error, "error_code": error_code, "states": states}
    return result


def _expand(roster, state_dir, *, persist=False):
    try:
        from fleetctl import atomic_json, iso, load_json
    except ImportError:
        from scripts.fleetctl import atomic_json, iso, load_json

    gateway = roster["chatgpt_gateway"]
    template = gateway["lane_template"]
    if (not isinstance(gateway["service_id"], str) or not gateway["service_id"].strip()
            or not isinstance(template["provider"], str) or not template["provider"].strip()
            or not isinstance(template["quality_tier"], str)
            or not isinstance(template["roles"], list)
            or not all(isinstance(role, str) for role in template["roles"])
            or not isinstance(template["capabilities"]["input"], list)
            or not isinstance(template["capabilities"]["output"], list)
            or not isinstance(template["timeout_s"], (int, float)) or template["timeout_s"] <= 0):
        raise ValueError()
    authority = {"api_base": template["transport"]["api_base"],
                 "key_file": template["auth"].get("key_ref") or template["auth"].get("key_file")}
    cache_file = state_dir / "chatgpt-catalog.json"
    cached = load_json(cache_file, {})
    if not isinstance(cached, dict) or not isinstance(cached.get("models", {}), dict):
        raise ValueError()
    previous = cached.get("models", {}) if cached.get("authority") == authority else {}
    # Cached rows are display history, never authority to admit a worker.
    previous = catalog_models({"object": "list", "data": [
        {"object": "model", "id": selector, "saved": True,
         "row": row["row"], "level": row["position"], "replicas": row.get("replicas", 1)}
        for selector, row in previous.items()]})
    error, error_code = None, None
    contact, models = {}, {}
    try:
        base, key = settings(template)
        models = catalog_models(request(base, key, "/models", timeout=1))
        status = request(base, key, "/gateway/status", timeout=1)
        contact = contacts(status)
    except Rejected as exc:
        error, error_code = exc.message, exc.code
    known = {**previous, **models}
    if persist:
        atomic_json(cache_file, {"authority": authority, "models": known, "checked_at": iso(), "error": error})
    result = copy.deepcopy(roster)
    generated = []
    for selector, row in sorted(known.items()):
        if gateway.get("models") is not None and selector not in gateway["models"]:
            continue
        label = row.get("worker_label")
        lane_id = gateway.get("lane_prefix", "") + label if gateway.get("lane_prefix") else selector
        if any(lane.get("lane_id") == lane_id for lane in result.get("lanes", []) + generated):
            raise ValueError("gateway catalog lane collides with a configured lane")
        # Every valid saved label can wake in a fresh chat, even before its
        # first contact. Removed labels cannot inherit cached admission.
        active = selector in models and not error
        state = "sleeping" if active and contact.get(label) != "recent" else "probed-ok" if active else "unavailable"
        lane = copy.deepcopy(template)
        lane.update(lane_id=lane_id, model_key=lane_id, selector=selector, harness="chatgpt-chat",
                    quota_pool=template.get("quota_pool", "chatgpt-work"), max_parallel=row["replicas"], max_tasks_per_run=1, retries=0,
                    allowed_modes=["read-only"], access_status="verified" if active else "unverified",
                    admission_status="active" if active else "rejected", verified_at=iso() if active else None,
                    catalog_state=state, gateway_service=gateway["service_id"])
        observed = next((item for item in status["workers"] if item["label"] == label), {}) if not error else {}
        lane.update(worker_label=label, worker_level=row["worker_level"], worker_row=row["row"],
                    gateway_status={key: observed[key] for key in ("quota_blocked", "rate_limited", "quota_until") if key in observed})
        generated.append(lane)
        result.setdefault("model_cards", {})[lane_id] = {**result.get("model_cards", {}).get(lane_id, {}),
            "pool": template.get("quota_pool", "chatgpt-work"), "name": row["name"],
            "lineage": "openai", "status": "older" if row.get("older") else "current", "best_for": "Read-only text through the Chat picker"}
        level = row["worker_level"]
        result.setdefault("effort", {})[lane_id] = {"levels": {"chatgpt-chat": [level]}, "default": level, "knee": level,
            "by_role": {}, "by_band": {}, "refuse_roles": {}, "control": "selector",
            "recheck": "Every saved model and gateway status read",
            "evidence": {"status": "unmeasured", "source": "Gateway /v1/models saved settings; underlying model unconfirmed",
                         "read_on": iso()[:10]}}
        # Pi's local tools are a different measured route from a plain chat.
        # Its provider-default cell never borrows native-chat level evidence.
        if gateway.get("pi_coding") is True and active and not row["older"] and level in {"high", "xhigh"}:
            try:
                from run_identity import FAMILY_BY_ROLE
            except ImportError:
                from scripts.run_identity import FAMILY_BY_ROLE
            pi_lane_id = lane_id + ":pi"
            if any(item.get("lane_id") == pi_lane_id for item in result.get("lanes", []) + generated):
                raise ValueError("Pi gateway lane collides with a configured lane")
            pi_lane = copy.deepcopy(lane)
            pi_lane.update(lane_id=pi_lane_id, harness="pi", provider="crossfeed-chat",
                           selector="crossfeed-chat/" + selector, chatgpt_pi=True,
                           roles=[role for role, family in FAMILY_BY_ROLE.items() if family == "coding-agent"],
                           allowed_modes=["read-only", "write"],
                           transport={**lane["transport"], "api": "openai-completions"})
            generated.append(pi_lane)
            result["effort"][lane_id]["levels"]["pi"] = ["provider-default"]
    result.setdefault("lanes", []).extend(generated)
    # A family slot ranks current gateway lanes without copying model names into
    # the static roster. Unavailable history stays visible but never ranked.
    for role, bands in result.get("routing", {}).get("roles", {}).items():
        for band, ranking in bands.items():
            if isinstance(ranking, list) and "chatgpt" in ranking:
                bands[band] = [candidate for item in ranking for candidate in (
                    [lane["lane_id"] for lane in generated if lane["admission_status"] == "active"
                     and role in lane.get("roles", [])] if item == "chatgpt" else [item])]
    result["chatgpt_catalog"] = {"error": error, "error_code": error_code, "checked_at": iso(),
        "wake": gateway_wake_status(status) if not error else ("Crossfeed Chat wake: unavailable", True),
        "states": {lane["selector"]: lane["catalog_state"] for lane in generated}}
    return _closed_roster(result, error, error_code) if error else result


def gateway_wake_status(status):
    """Summarize the gateway's wake authority without echoing error payloads."""
    try:
        limits, wakes = status["wake_limits"], status["wakes"]
        count, cap, cooldown = limits["attempts"], limits.get("daily_cap", 0), limits["cooldown_until"]
        enabled = status["extension_enabled"]
        if (type(count) is not int or count < 0 or type(cap) is not int or cap < 0
                or type(cooldown) not in (int, float) or type(enabled) is not bool
                or not isinstance(wakes, list)):
            raise ValueError()
        failed = sum(row["state"] in {"failed", "expired"} for row in wakes)
        pending = sum(row["state"] in {"queued", "issued", "claimed"} for row in wakes)
        daily = f"{count}/{cap} attempts today" if cap else f"{count} attempts today, no daily cap"
        hourly, hourly_blocked = "", False
        if "hourly_cap" in limits or "hourly_attempts" in limits:
            hour_count, hour_cap = limits["hourly_attempts"], limits["hourly_cap"]
            if (type(hour_count) is not int or hour_count < 0
                    or type(hour_cap) is not int or hour_cap < 0):
                raise ValueError()
            hourly = f", {hour_count}/{hour_cap} attempts in last hour"
            hourly_blocked = hour_count >= hour_cap
        paused = cooldown > time.time() * 1000
        return (f"Crossfeed Chat wake: extension {'paired' if enabled else 'unpaired'}, "
                f"{daily}{hourly}, cooldown {'active' if paused else 'inactive'}, "
                f"{failed} failed/expired, {pending} pending",
                not enabled or (cap > 0 and count >= cap) or hourly_blocked or paused or failed > 0)
    except (KeyError, TypeError, ValueError):
        return "Crossfeed Chat wake: unavailable", True


def doctor(gateway, known_states, wake_status=None):
    """Report the already-read saved-worker states without a separate CLI."""
    try:
        wake_line, wake_failed = wake_status or ("Crossfeed Chat wake: unavailable", True)
        failed = not known_states or any(state not in {"sleeping", "probed-ok"}
                                        for state in known_states.values())
        rows = ", ".join(f"{selector}={state}" for selector, state in sorted(known_states.items())) or "no saved labels"
        failed = failed or wake_failed
        action = "check Crossfeed Chat workers and extension wake status" if failed else "none"
        return f"{gateway['service_id']}: {rows}; owner action: {action}; {wake_line}", failed
    except (Rejected, OSError, KeyError, TypeError, ValueError):
        return "ChatGPT gateway unavailable; owner action: configure or start Crossfeed Chat", True
