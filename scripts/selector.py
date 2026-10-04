"""Select an admitted model and level using local evidence and quota projections.

The fleet module is injected so routing and wrapper admission remain the source
of truth. Evidence and immutable dispatch receipts are local state, never roster
data. An API-equivalent cost is an estimate, not a measured subscription debit.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import random
import statistics
from pathlib import Path
import re
import shlex
import tempfile
import uuid
from typing import Any

try:
    from run_identity import FAMILY_BY_ROLE
except ModuleNotFoundError:
    from scripts.run_identity import FAMILY_BY_ROLE


DEFAULTS = {"target": 90.0, "lambda0": 1.0, "mu": .001, "explore": .1,
            "uncertainty_weight": 1.0,
            "high_cost_cap": 1.0,
            "stakes_weights": {"low": 1.0, "normal": 2.0, "high": 40.0}}
FAMILIES = {"coding-agent", "review", "repo-qa", "reasoning", "extraction", "research", "visual"}
WRAPPERS = {name: name + "-agent.sh" for name in
            ("codex", "claude", "opencode", "agy", "copilot", "openrouter", "pi")}
WRAPPERS["chatgpt-chat"] = "chatgpt-agent.sh"


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def pool_lambda(projected_used_percent_at_reset: float | None,
                target: float = 90.0, lambda0: float = 1.0) -> float | None:
    """Return the stateless pool price, or None for an unknown projection.

    Inputs are percentage points (100 means exhausted), a target in [0,100),
    and a nonnegative price scale. This intentionally replaces stateful dual
    mirror descent with a reproducible price based only on this projection.
    """
    if not 0 <= target < 100 or lambda0 < 0:
        raise ValueError("target must be in [0, 100), lambda0 must be nonnegative")
    projected = _number(projected_used_percent_at_reset)
    return None if projected is None else lambda0 * max(0.0, projected - target) / (100.0 - target)


def _fleet(fleet: Any) -> Any:
    return fleet if fleet is not None else importlib.import_module("fleetctl")


def _band(fleet: Any, roster: dict, runtime: dict, pool: str, state: str, evidence: dict) -> str:
    # Pass this roster's policy explicitly; fleet's process-wide cache may refer
    # to a different overlay when a caller supplies a synthetic/custom roster.
    policy = fleet.resolve_quota_policy(roster)[0]
    gated = fleet.gating_state(state, evidence, policy)
    band = "quality_first" if policy == "off" or gated in {"UNKNOWN", "ABUNDANT", "HEALTHY"} else (
        "conserve" if gated == "CONSERVE" else "critical")
    return fleet.level_band(fleet.pool_level(runtime, pool), band)


def _lineage(roster: dict, option: dict) -> str:
    card = roster.get("model_cards", {}).get(option["model_key"], {})
    entry = roster.get("model_evidence", {}).get(option["model_key"], {})
    declared = card.get("lineage") or card.get("vendor") or entry.get("lineage") or entry.get("vendor")
    if declared:
        return str(declared).lower()
    key = option["model_key"].lower()
    for prefix, vendor in (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
                           ("claude", "anthropic"), ("gemini", "google"),
                           ("kimi", "moonshot"), ("glm", "zai"), ("grok", "xai"),
                           ("qwen", "alibaba"), ("deepseek", "deepseek"), ("minimax", "minimax")):
        if key.startswith(prefix):
            return vendor
    return str(option.get("provider") or "unknown").lower()


def enumerate_options(roster: dict, runtime: dict, role: str, *, mode: str | None = None,
                      modality: str = "text", exclude_lineage: str | None = None,
                      fleet: Any = None) -> tuple[list[dict], list[str]]:
    """Return admitted actual (pool, harness, model, level) options and refusals."""
    fleet = _fleet(fleet)
    policy = roster.get("policy", {}).get("selector", {})
    mode = mode or policy.get("mode_by_role", {}).get(role) or (
        "write" if FAMILY_BY_ROLE.get(role) == "coding-agent" else "read-only")
    allowed = policy.get("allowed_pools_by_role", {})
    allowed = allowed.get(role, allowed.get("default"))
    if allowed is not None and not isinstance(allowed, list):
        raise fleet.FleetError("selector allowed_pools_by_role values must be lists")
    candidates = [dict(lane) for lane in roster.get("lanes", [])]
    for pool, wrapper in fleet.DIRECT_POOLS.items():
        if fleet.pool_is_direct(roster, pool):
            for model, run_as in fleet.choosable_models(roster, pool).items():
                card = roster.get("model_cards", {}).get(model, {})
                candidates.append({"model_key": model, "model": run_as, "harness": pool,
                                   "quota_pool": pool, "direct": True,
                                   "capabilities": card.get("capabilities", {"input": ["text"]}),
                                   "allowed_modes": card.get("allowed_modes", ["read-only", "write"]),
                                   "access_status": card.get("access_status", "verified"),
                                   "admission_status": card.get("admission_status", "active")})
    options, rejected, seen = [], [], set()
    states = {}
    for lane in candidates:
        key, harness, pool = lane.get("model_key"), lane.get("harness"), lane.get("quota_pool")
        label = lane.get("lane_id") or f"{pool}/{key}"
        why = None
        if not key or not pool or harness not in WRAPPERS:
            why = "missing model/pool or unsupported wrapper"
        elif allowed is not None and pool not in allowed:
            why = "pool forbidden for role"
        elif lane.get("access_status") != "verified" or lane.get("admission_status") != "active":
            why = "access or admission not active/verified"
        elif not lane.get("direct") and not lane.get("roles"):
            why = "explicit-only lane (no routing roles)"
        elif fleet.retired_on(roster, key):
            why = "model retired"
        elif fleet.model_is_older(roster, pool, key) and not fleet.older_model_reason(roster, pool, key):
            why = "older model without pool-specific retention evidence"
        elif fleet.model_preference(runtime, key) == "off" or not fleet.pool_switches(roster, runtime, pool).get(key, False):
            why = "model switched off or older without retention evidence"
        elif mode not in lane.get("allowed_modes", []):
            why = f"mode {mode}"
        elif harness == "agy" and mode == "read-only":
            # The existing fanout guard refuses this combination: native
            # terminal sandboxing is not a proven read-only workspace boundary.
            why = "AGY has no proven read-only boundary"
        elif modality not in lane.get("capabilities", {}).get("input", ["text"]):
            why = f"modality {modality}"
        elif harness == "chatgpt-chat" and role not in lane.get("roles", []):
            why = "role not declared for configured ChatGPT lane"
        elif harness == "chatgpt-chat" and not lane.get("worker_label"):
            why = "ChatGPT lane has no saved worker label"
        elif harness == "chatgpt-chat" and fleet.chatgpt_pro.blocked(roster, runtime, lane):
            why = fleet.chatgpt_pro.blocked(roster, runtime, lane)
        elif harness == "chatgpt-chat" and (lane.get("gateway_status", {}).get("quota_blocked")
                                            or lane.get("gateway_status", {}).get("rate_limited")):
            why = "worker quota paused"
        if why:
            rejected.append(f"{label}: {why}")
            continue
        state, pool_evidence = states.setdefault(pool, fleet.current_pool_state(runtime, pool, roster=roster))
        band = _band(fleet, roster, runtime, pool, state, pool_evidence)
        if pool == fleet.ROUTING_POOL and band == "critical":
            role_policies = roster.get("routing", {}).get("roles", {})
            role_policy = role_policies.get(role) or role_policies.get("default") or {}
            admitted = role_policy.get("critical", role_policy.get("quality_first", []))
            admitted, _ = fleet.apply_model_toggles(admitted, roster, runtime)
            if lane.get("lane_id") not in admitted:
                rejected.append(f"{label}: outside CRITICAL role allowlist")
                continue
        option = {"pool": pool, "harness": harness, "model_key": key, "model": key,
                  "run_as": lane.get("model") or lane.get("selector") or key,
                  "mode": mode, "band": band, "pool_state": state,
                  "provider": lane.get("provider")}
        if lane.get("lane_id"):
            option["lane_id"] = lane["lane_id"]
        if lane.get("worker_label"):
            option.update(worker_label=lane["worker_label"], worker_level=lane["worker_level"])
        option["lineage"] = _lineage(roster, option)
        if exclude_lineage and option["lineage"] == exclude_lineage.lower():
            rejected.append(f"{label}: excluded lineage {exclude_lineage}")
            continue
        if state == "EXHAUSTED":
            rejected.append(f"{label}: pool exhausted or switched off")
            continue
        if lane.get("direct"):
            busy = fleet.pool_level(runtime, pool) == "low" and fleet.live_pool_leases(runtime, pool)
        else:
            busy = fleet.lane_free_slots(runtime, lane, state, pool_evidence) <= 0
        if busy:
            rejected.append(f"{label}: at capacity")
            continue
        entry = roster.get("effort", {}).get(key, {})
        refusal = entry.get("refuse_roles", {}).get(role) or entry.get("refuse_roles", {}).get(f"{band}/{role}")
        if refusal:
            rejected.append(f"{label}: {refusal}")
            continue
        levels = entry.get("levels", {}).get(harness)
        if levels is None:
            rejected.append(f"{label}: missing effort controls")
            continue
        # If switches make this model a replacement, score only levels it will
        # actually run. Explicit enumeration must not bypass stand-in ceilings.
        replacements = fleet.stand_ins(roster, runtime, pool).values()
        stand_in = key in replacements or option["run_as"] in replacements
        for requested in levels or [None]:
            try:
                resolved = fleet.resolve_effort(roster, key, role, harness, requested,
                                                band=band, stand_in=stand_in)
            except fleet.FleetError as exc:
                rejected.append(f"{label}/{requested}: {exc}")
                continue
            if resolved["source"] == "missing":
                rejected.append(f"{label}/{requested}: {resolved['reason']}")
                continue
            level = resolved["effort"] or entry.get("default")
            if harness == "copilot":
                if lane.get("selector") != "auto" or level != "service-chosen":
                    rejected.append(f"{label}: Copilot requires Auto/service-chosen")
                    continue
            if harness == "openrouter" and requested is not None:
                rejected.append(f"{label}/{requested}: wrapper has no effort flag")
                continue
            actual = dict(option, level=level, effort=level, effort_reason=resolved["reason"], stand_in=stand_in)
            if harness == "agy" and re.search(r"-(low|medium|high|max)$", actual["run_as"]):
                actual["run_as"] = re.sub(r"-(low|medium|high|max)$", "-" + level, actual["run_as"])
            identity = (pool, harness, key, level)
            if identity not in seen:
                seen.add(identity)
                options.append(actual)
    return options, rejected


def _pool_price(roster: dict, runtime: dict, pool: str, policy: dict, fleet: Any,
                *, model_key: str | None = None) -> dict:
    """Price the greatest applicable pressure: projection when known, usage otherwise.

    Pool summaries exclude model-only windows. Option prices also include windows
    naming that model's family, using the oracle label or its scoped window id.
    Spend levels govern admission, not the measured price or displayed quota.
    """
    if fleet.pool_has_no_known_limit(roster, pool):
        state, _ = fleet.current_pool_state(runtime, pool, roster=roster)
        return {"lambda": 0.0, "state": state, "binding_window": None, "window": {},
                "window_minutes": None, "projected_used_percent_at_reset": None,
                "projection_unknown": False, "projection_basis": "none-known"}

    def words(text: str) -> set[str]:
        return set(re.findall(r"[a-z][a-z0-9]*", text.lower()))
    pool_label = str(roster.get("quota_pools", {}).get(pool, {}).get("label") or pool)
    noise = words(pool) | {
        "weekly", "week", "monthly", "month", "daily", "day", "session", "hour", "hours",
        "primary", "secondary", "tertiary", "rolling", "quota", "summary", "window", "limit",
        "only", "scoped", "and", "or", "h", "d", "m",
    }
    pool_terms = words(pool_label) - noise
    model_words = set().union(*(words(key) for key in {
        *roster.get("model_cards", {}), *roster.get("effort", {}),
        *fleet.choosable_models(roster, pool),
    })) - noise
    snapshot = runtime.get("quota_snapshots", {}).get(pool)
    applicable = {}
    for name, window in (snapshot or {}).get("windows", {}).items():
        scope = words(name + " " + str(window.get("label") or ""))
        terms = scope - noise
        scoped = bool(terms and terms != pool_terms and (
            scope & {"only", "scoped"} or terms & model_words))
        if scoped and (model_key is None or not terms & words(model_key)):
            continue
        applicable[name] = window
    scoped_runtime = runtime
    if snapshot is not None:
        scoped_runtime = dict(runtime, quota_snapshots={
            **runtime["quota_snapshots"], pool: dict(snapshot, windows=applicable),
        })
    state, _ = fleet.current_pool_state(runtime, pool, roster=roster)
    _, evidence = fleet.current_pool_state(fleet._without_levels(scoped_runtime), pool, roster=roster)
    projected = []
    for name, window in evidence.get("windows", {}).items():
        value = _number(window.get("projected_used_percent_at_reset"))
        if value is None:
            value = _number(window.get("surplus", {}).get("projected_used_percent_at_reset"))
        if value is None:
            value = _number(fleet.window_surplus(window).get("projected_used_percent_at_reset"))
        if value is None:
            eta, used, left = (_number(window.get(k)) for k in ("eta_seconds", "used_percent", "seconds_to_reset"))
            if eta is not None and eta > 0 and used is not None and left is not None:
                value = used + (100 - used) * left / eta
        projected.append((name, window, value))
    binding = max(projected, key=lambda row: row[2] if row[2] is not None else (
        _number(row[1].get("used_percent")) or 0), default=(None, {}, None))
    name, window, value = binding
    price = pool_lambda(value, policy.get("target", DEFAULTS["target"]), policy.get("lambda0", DEFAULTS["lambda0"]))
    if price is None:
        price = float(policy.get("lambda_unknown", .5 * policy.get("lambda0", DEFAULTS["lambda0"])))
        if not math.isfinite(price) or price < 0:
            raise fleet.FleetError("lambda_unknown must be finite and nonnegative")
    return {"lambda": price, "state": state, "binding_window": name,
            "window": window,
            "window_minutes": window.get("window_minutes"),
            "projected_used_percent_at_reset": value,
            "projection_unknown": value is None, "projection_basis": "snapshot_or_window_pace" if value is not None else "unknown"}


def _allowance_usd(roster: dict, pool: str, binding: dict) -> float | None:
    plan = roster.get("quota_pools", {}).get(pool, {}).get("plan", {})
    allowance = plan.get("allowance")
    if isinstance(allowance, dict):
        currency = allowance.get("currency", plan.get("currency"))
        window = allowance.get("window") or allowance.get("window_name")
        minutes = allowance.get("window_minutes")
        amount = _number(allowance.get("amount", allowance.get("usd")))
        if "usd" in allowance:
            currency = currency or "USD"
    else:
        currency = plan.get("currency")
        window = plan.get("allowance_window") or plan.get("window")
        minutes = plan.get("allowance_window_minutes")
        amount = _number(allowance)
    matches = (window is not None or minutes is not None)
    if window is not None:
        matches = matches and window == binding["binding_window"]
    if minutes is not None:
        matches = matches and minutes == binding.get("window_minutes")
    if currency != "USD" or amount is None or amount <= 0 or not matches:
        return None
    return amount


def _cost(row: dict, allowance: float | None) -> tuple[float | None, float | None]:
    tokens, prices = row.get("tokens_per_task") or {}, row.get("price_1m") or {}
    values = [_number(data.get(side)) for data in (tokens, prices) for side in ("in", "out")]
    if any(value is None or value < 0 for value in values):
        return None, None
    incoming, outgoing, in_price, out_price = values
    usd = (incoming * in_price + outgoing * out_price) / 1_000_000
    return usd, usd / allowance * 100 if allowance is not None else None


def _task_cost(roster: dict, option: dict, row: dict, binding: dict,
               median_usd: float | None) -> tuple[dict, list[str]]:
    pool = option["pool"]
    if binding.get("projection_basis") == "none-known":
        chat = option.get("harness") == "chatgpt-chat" or pool == "chatgpt-work"
        usd = None if chat else _cost(row, None)[0]
        return {"estimated_usd": usd, "percent": None, "allowance_usd": None,
                "estimated": True, "unknown": True, "basis": "no_known_limit",
                "penalty_percent": 0.0}, ["chat_usage_unpriced" if chat else "usage_unpriced"]
    allowance = _allowance_usd(roster, pool, binding)
    usd, percent = _cost(row, allowance)
    own = row.get("own_cost") or {}
    own = own.get(pool, {}) if isinstance(own.get(pool), dict) else (
        own if own.get("pool", row.get("pool")) == pool else {})
    measured = _number(own.get("percent_per_task"))
    flags = []
    basis = "api_equivalent_allowance" if percent is not None else "unknown"
    if measured is not None and measured >= 0:
        percent, basis = measured, "measured"
    elif percent is None and pool == "opencode-go":
        table = roster.get("quota_pools", {}).get(pool, {}).get("request_estimates", {}).get("per_5h_week_month", {})
        estimates = table.get(option["model_key"], table.get(option["run_as"], {}))
        if isinstance(estimates, dict) and option["level"] in estimates:
            estimates = estimates[option["level"]]
        name = str(binding.get("binding_window") or "").lower()
        minutes = binding.get("window_minutes")
        column = 2 if "month" in name else 1 if "week" in name else 0 if "5h" in name or "five_hour" in name else (
            0 if minutes and minutes <= 300 else 1 if minutes and minutes <= 10080 else 2 if minutes else None)
        count = None
        if column is not None:
            if isinstance(estimates, (list, tuple)) and len(estimates) > column:
                count = _number(estimates[column])
            elif isinstance(estimates, dict):
                count = _number(estimates.get(("5h", "week", "month")[column]))
        if count is not None and count > 0:
            requests = _number(own.get("requests_per_task"))
            percent = 100 / count * (requests if requests is not None and requests >= 0 else 1)
            basis = "go_request_estimate"
    nominal = _number(roster.get("quota_pools", {}).get(pool, {}).get("nominal_percent_per_task", .5))
    nominal = nominal if nominal is not None and nominal >= 0 else .5
    if percent is None and usd is not None and median_usd is not None and median_usd > 0:
        percent, basis = usd / median_usd * nominal, "relative_proxy"
        flags.append("cost_relative_proxy")
    if percent is None:
        flags.append("cost_unknown_nominal")
    return {"estimated_usd": usd, "percent": percent, "allowance_usd": allowance,
            "estimated": basis != "measured", "unknown": percent is None, "basis": basis,
            "penalty_percent": percent if percent is not None else nominal}, flags


def _command(option: dict, role: str, receipt: Path, *, fleet: Any = None) -> list[str]:
    harness, level = option["harness"], option["level"]
    if harness == "chatgpt-chat" and ((level != "service-chosen" and
                                (not option.get("worker_label") or level != option.get("worker_level")))
                               or option["mode"] != "read-only"):
        raise _fleet(fleet).FleetError("Crossfeed Chat command requires read-only mode and owner-configured effort")
    argv = ["env", f"FLEET_SELECTION_FILE={receipt}",
            str(Path(__file__).resolve().parent / WRAPPERS[harness])]
    if harness in {"codex", "claude"}:
        argv += ["--model", option["run_as"], "--role", role]
    elif harness == "agy":
        argv += ["--lane", option["lane_id"], "--model", option["run_as"], "--role", role]
    elif harness in {"opencode", "openrouter"}:
        argv += ["--lane", option["lane_id"]]
        if harness == "opencode":
            argv += ["--effort-role", role]
    elif harness == "copilot":
        argv += ["--model", "auto"]
    elif harness == "pi":
        argv += ["run", "--lane", option["lane_id"], "--effort-role", role, "--mode", "ro" if option["mode"] == "read-only" else "rw"]
    elif harness == "chatgpt-chat":
        argv += ["run", "--lane", option["lane_id"], "--effort-role", role, "--mode", "ro"]
    if harness != "chatgpt-chat" and level not in {"provider-default", "service-chosen"}:
        argv += [{"codex": "--reasoning", "claude": "--effort", "agy": "--effort", "opencode": "--variant", "pi": "--effort"}[harness], level]
    if harness == "codex":
        argv += ["--sandbox", "read-only" if option["mode"] == "read-only" else "workspace-write"]
    elif harness in {"claude", "copilot"} and option["mode"] == "read-only":
        argv += ["--read-only"]
    elif harness == "opencode":
        argv += ["--read-only" if option["mode"] == "read-only" else "--write"]
    argv += ["--prompt", "<task>"]
    return argv


def _receipt(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        # Atomic create-only publication; unlike replace, cannot overwrite an
        # existing receipt even in the unlikely event of a UUID collision.
        os.link(temporary, path)
        path.chmod(0o400)
    finally:
        temporary.unlink()


def _allow_identity(entry: str) -> tuple[str, str, str]:
    # Pool and effort delimit the model key, which can itself be namespaced.
    parts = entry.split(":")
    return parts[0], ":".join(parts[1:-1]), parts[-1]


def parse_allow(value: str | Path | None, fleet: Any) -> list[str] | None:
    """Normalize a comma list or file once so receipts survive file changes."""
    if value is None:
        return None
    value = str(value)
    path = Path(value).expanduser()
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False  # A long inline list need not be a valid filename.
    try:
        if is_file or ":" not in value:
            value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise fleet.FleetError(f"cannot read allow list: {exc}") from exc
    entries = []
    for entry in re.split(r"[,\n]", value):
        entry = entry.strip()
        if not entry:
            continue
        segments = [part.strip() for part in entry.split(":")]
        parts = _allow_identity(":".join(segments))
        if len(segments) < 3 or not all(segments) or any("*" in part for part in parts[:2]) or (
                "*" in parts[2] and parts[2] != "*"):
            raise fleet.FleetError(f"invalid allow entry {entry!r}: expected pool:model_key:level (level may be *)")
        normalized = ":".join(parts)
        if normalized not in entries:
            entries.append(normalized)
    if not entries:
        raise fleet.FleetError("allow list is empty")
    return entries


def select_option(roster: dict, runtime: dict, state_dir: Path, role: str, *,
                  stakes: str = "normal", family: str | None = None,
                  exclude_lineage: str | None = None, mode: str | None = None,
                  modality: str = "text", allow: str | Path | None = None,
                  lead: str | None = None, fleet: Any = None,
                  target: tuple[str, str] | None = None) -> dict:
    """Return choice/top3/lambdas and write an immutable replay receipt."""
    fleet = _fleet(fleet)
    state_dir = Path(state_dir).expanduser().resolve()
    fleet.chatgpt_pro.refresh(roster, state_dir)
    policy = roster.get("policy", {}).get("selector", {})
    if stakes not in {"low", "normal", "high", "irreversible"}:
        raise fleet.FleetError(f"unknown stakes: {stakes}")
    family = family or policy.get("families_by_role", {}).get(role) or FAMILY_BY_ROLE.get(role, "coding-agent")
    if family not in FAMILIES:
        raise fleet.FleetError(f"unknown task family: {family}")
    options, rejected = enumerate_options(roster, runtime, role, mode=mode, modality=modality,
                                          exclude_lineage=exclude_lineage, fleet=fleet)
    if target is not None:
        requested = next((lane for lane in roster.get("lanes", [])
                          if (lane.get("harness"), lane.get("model_key")) == target), {})
        reason = fleet.chatgpt_pro.blocked(roster, runtime, requested)
        if reason:
            replacements = fleet.chatgpt_pro.replacements(roster, runtime, requested)
            keys = [lane["model_key"] for lane in replacements]
            options = [option for option in options if option["harness"] == "chatgpt-chat" and option["model_key"] in keys]
            for option in options:
                option["pro_fallback"] = fleet.chatgpt_pro.fallback(requested, reason)
        else:
            options = [option for option in options
                       if (option["harness"], option["model_key"]) == target]
        if not options:
            raise fleet.FleetError("requested model has no eligible selector option")
    if lead is not None and lead not in roster.get("quota_pools", {}):
        raise fleet.FleetError(f"unknown lead pool {lead}")
    pressure = fleet.lead_pressure(runtime, lead, roster)
    if pressure and stakes != "irreversible":
        retained = []
        for option in options:
            if option["pool"] == lead:
                rejected.append(f"{lead}/{option['model_key']}: pressured lead pool excluded")
            else:
                retained.append(option)
        options = retained
    allow_list = parse_allow(allow, fleet)
    if allow_list is not None:
        allowed = {_allow_identity(entry) for entry in allow_list}
        retained = []
        for option in options:
            identity = (option["pool"], option["model_key"], option["level"])
            if identity in allowed or (*identity[:2], "*") in allowed:
                retained.append(option)
            else:
                rejected.append(":".join(identity) + ": not in allow list")
        options = retained
        if not options:
            raise fleet.FleetError("no eligible selector option in allow list: " + "; ".join(rejected))
    capped = set()
    for option in options:
        pool = option["pool"]
        daily_cap = roster.get("quota_pools", {}).get(pool, {}).get("daily_usd_cap")
        if daily_cap is not None and pool not in capped:
            spent = fleet.pool_spent_since(state_dir, roster, pool, fleet.local_day_start())
            if spent >= float(daily_cap):
                capped.add(pool)
                rejected.append(f"{pool}: daily spend cap reached")
    options = [option for option in options if option["pool"] not in capped]
    prices = {pool: _pool_price(roster, runtime, pool, policy, fleet) for pool in roster.get("quota_pools", {})}
    for option in options:
        if option["pool"] not in prices:
            prices[option["pool"]] = _pool_price(roster, runtime, option["pool"], policy, fleet)
    path = state_dir / "evidence" / "levels.json"
    evidence = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = {(row.get("model_key") or row.get("model"), row.get("level")): row for row in evidence.get("rows", [])}
    scored = []
    weights = dict(DEFAULTS["stakes_weights"], **policy.get("stakes_weights", {}))
    uncertainty_weight = max(1e-9, float(policy.get("uncertainty_weight", DEFAULTS["uncertainty_weight"])))
    mu = float(policy.get("mu", DEFAULTS["mu"])) * (0 if stakes == "irreversible" else .5 if stakes == "high" else 1)
    high_cost_cap = _number(policy.get("high_cost_cap", DEFAULTS["high_cost_cap"]))
    if high_cost_cap is None or high_cost_cap < 0:
        raise fleet.FleetError("high_cost_cap must be finite and nonnegative")
    pool_costs = {}
    for option in options:
        usd, _ = _cost(rows.get((option["model_key"], option["level"]), {}), None)
        if usd is not None:
            pool_costs.setdefault(option["pool"], []).append(usd)
    medians = {pool: statistics.median(costs) for pool, costs in pool_costs.items()}
    for option in options:
        row = rows.get((option["model_key"], option["level"]), {})
        strata = row.get("q_by_stakes", {})
        scoped = strata.get(stakes, {}).get(family)
        if scoped is None and stakes == "irreversible":
            scoped = strata.get("high", {}).get(family)
        q = scoped if scoped is not None else row.get("q", {}).get(family) or {}
        mean, sd = _number(q.get("mean")), _number(q.get("sd"))
        unknown = mean is None or not q.get("n_sources") and not row.get("own", {}).get(family, {}).get("trials")
        unknown = unknown or q.get("unknown", False) or "unknown" in (row.get("flags") or [])
        mean = 0.5 if mean is None else min(1.0, max(0.0, mean))
        sd = max(0.15, sd or 0.0) if unknown else max(0.0, sd if sd is not None else 0.15)
        if stakes == "irreversible" and unknown:
            rejected.append(f"{option['model_key']}/{option['level']}: quality unknown; irreversible forbids exploration")
            continue
        quality = mean if not unknown else max(0.0, mean - uncertainty_weight * sd)
        pool_price = _pool_price(roster, runtime, option["pool"], policy, fleet,
                                 model_key=option["model_key"])
        cost, cost_flags = _task_cost(roster, option, row, pool_price, medians.get(option["pool"]))
        price = pool_price["lambda"]
        latency = _number(row.get("latency_by_stakes", {}).get(stakes, row.get("latency_s")))
        penalty = price * cost["penalty_percent"]
        if stakes == "high":
            penalty = min(penalty, high_cost_cap)
        score = quality if stakes == "irreversible" else weights[stakes] * quality - penalty - mu * (latency or 0)
        scored.append(dict(option, quality=quality, q={"mean": mean, "sd": sd, "unknown": unknown},
                           score=score, cost=cost, latency_s=latency, mu=mu, pool_price=pool_price,
                           flags=list(row.get("flags") or []) + cost_flags + (["quality_unknown"] if unknown else []) +
                                 (["quota_projection_unknown"] if pool_price["projection_unknown"] else [])))
    # Missing cells are not a cheaper substitute for measured equal-mean cells.
    # This dominance rule matters even when their price difference exceeds one
    # uncertainty penalty; the raw formula alone cannot enforce that promise.
    measured_means = {item["q"]["mean"] for item in scored if not item["q"]["unknown"]}
    unknown_options = [item for item in scored if item["q"]["unknown"]]
    for item in scored:
        if item["q"]["unknown"] and item["q"]["mean"] in measured_means and not item.get("pro_fallback"):
            rejected.append(f"{item['model_key']}/{item['level']}: unknown cell dominated by measured equal-mean option")
    scored = [item for item in scored if not (item["q"]["unknown"] and item["q"]["mean"] in measured_means
                                             and not item.get("pro_fallback"))]
    scored.sort(key=lambda item: (-item["score"], -item["quality"], item["q"]["unknown"], item["q"]["sd"],
                                  item["pool"], item["model_key"], item["level"]))
    if not scored:
        raise fleet.FleetError("no eligible selector option: " + "; ".join(rejected))
    explore = float(policy.get("explore", DEFAULTS["explore"])) if stakes == "low" and unknown_options else 0.0
    if not math.isfinite(explore) or not 0 <= explore <= 1:
        raise fleet.FleetError("explore must be in [0, 1]")
    greedy = scored[0]
    if target is not None and any(option.get("pro_fallback") for option in scored):
        scored.sort(key=lambda option: (option.get("worker_level") != "xhigh", -option["score"]))
        greedy = scored[0]
        explore = 0.0
    exploring = bool(explore and random.random() < explore)
    choice = random.choice(unknown_options) if exploring else greedy
    for item in scored + unknown_options:
        item["selection_probability"] = (1 - explore if item is greedy else 0) + (
            explore / len(unknown_options) if item["q"]["unknown"] and unknown_options else 0)
    receipt = state_dir / "selections" / (str(uuid.uuid4()) + ".json")
    top3 = scored[:3]
    result = {"schema": "fleet-selection/v1", "choice": choice, "top3": top3,
              "selection_probability": choice["selection_probability"], "exploration": exploring,
              "explore": explore, "unknown_options": len(unknown_options),
              "lambdas": {pool: value["lambda"] for pool, value in prices.items()},
              "pool_prices": prices, "role": role, "family": family, "stakes": stakes,
              "rejected": rejected, "allow": allow_list, "selection_file": str(receipt),
              "target": list(target) if target is not None else None,
              "lead": lead, "lead_pressure": pressure}
    snapshot = json.loads(json.dumps(result))
    receipt_options = [choice] + [item for item in top3 if item is not choice]
    for index, option in enumerate(receipt_options):
        option_receipt = receipt if index == 0 else state_dir / "selections" / (str(uuid.uuid4()) + ".json")
        payload = dict(snapshot, choice=json.loads(json.dumps(option)), selection_file=str(option_receipt),
                       selection_probability=option["selection_probability"], exploration=exploring if index == 0 else False)
        _receipt(option_receipt, payload)
        option["selection_file"] = str(option_receipt)
        option["command_argv"] = _command(option, role, option_receipt, fleet=fleet)
        option["command"] = shlex.join(option["command_argv"])
    return result
