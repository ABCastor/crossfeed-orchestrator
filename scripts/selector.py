"""Select an admitted model and level using local evidence and quota projections.

The fleet module is injected so routing and wrapper admission remain the source
of truth. Evidence and immutable dispatch receipts are local state, never roster
data. An API-equivalent cost is an estimate, not a measured subscription debit.
"""

from __future__ import annotations

from decimal import Decimal
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
    import funding
    import go_quota_tariffs
    from data_policy import paid_refusal
    from openrouter_budget import BudgetError, ceiling_usd, route_config, tariff_metadata
    from run_identity import FAMILY_BY_ROLE, canonical_model, evidence_index
except ModuleNotFoundError:
    from scripts import funding, go_quota_tariffs
    from scripts.data_policy import paid_refusal
    from scripts.openrouter_budget import BudgetError, ceiling_usd, route_config, tariff_metadata
    from scripts.run_identity import FAMILY_BY_ROLE, canonical_model, evidence_index


DEFAULTS = {"target": 90.0, "lambda0": 1.0, "mu": .001, "explore": .1,
            "uncertainty_weight": 1.0,
            "high_cost_cap": 1.0,
            "stakes_weights": {"low": 1.0, "normal": 2.0, "high": 40.0}}
# BR-SELECT-002: variety among near-equal options. Defaults are subject to operator approval.
#   quality_band  how much estimated quality (0..1) a varied choice may give up against the
#                 option plain scoring would pick, per stakes; 0 keeps that pick
#   pace_weight   preference per unit of pool pace (+1 = a whole allowance about to be
#                 destroyed unused, -1 = a whole allowance ahead of pace)
#   recency_half_life_s  how fast a lane's recent use stops counting against it
#   temperature   softness of the random draw over preferences (lower = more decisive)
VARIETY = {"quality_band": {"low": .05, "normal": .03, "high": .01, "irreversible": 0.0},
           "pace_weight": 2.0, "recency_half_life_s": 3600.0, "temperature": .5}
# A deficit smaller than this (allowance fraction) does not outweigh another window's surplus:
# one task at the start of a short window reads as a small deficit and must not flip the pool.
PACE_DEFICIT_DEAD_ZONE = .05
# Recent use is read from the tail of runs.jsonl and from in-flight dispatch receipts.
RECENT_RUNS_TAIL_BYTES = 1 << 20
FAMILIES = {"coding-agent", "review", "debug", "repo-qa", "reasoning", "extraction", "research", "visual", "writing"}
WRAPPERS = {name: name + "-agent.sh" for name in
            ("codex", "claude", "opencode", "agy", "copilot", "openrouter", "pi")}
WRAPPERS["chatgpt-chat"] = "chatgpt-agent.sh"


def _number(value: Any) -> float | None:
    return funding.number(value)


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
    band = "quality_first" if policy == "off" or gated in {"UNKNOWN", "GAUGE_ERROR", "ABUNDANT", "HEALTHY"} else (
        "conserve" if gated == "CONSERVE" else "critical")
    return fleet.level_band(fleet.pool_level(runtime, pool), band)


def _lineage(roster: dict, option: dict) -> str:
    model = option.get("model_identity", option["model_key"])
    card = roster.get("model_cards", {}).get(model, {})
    entry = roster.get("model_evidence", {}).get(model, {})
    declared = card.get("lineage") or card.get("vendor") or entry.get("lineage") or entry.get("vendor")
    if declared:
        return str(declared).lower()
    key = model.lower()
    for prefix, vendor in (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
                           ("claude", "anthropic"), ("gemini", "google"),
                           ("kimi", "moonshot"), ("glm", "zai"), ("grok", "xai"),
                           ("qwen", "alibaba"), ("deepseek", "deepseek"), ("minimax", "minimax")):
        if key.startswith(prefix):
            return vendor
    return str(option.get("provider") or "unknown").lower()


# BR-DATA-001
def enumerate_options(roster: dict, runtime: dict, role: str, *, mode: str | None = None,
                      modality: str = "text", exclude_lineage: str | None = None,
                      fleet: Any = None, explicit_pools: set[str] | None = None,
                      data_class: str | None = None, prompt: str | None = None) -> tuple[list[dict], list[str]]:
    """Return admitted actual (pool, harness, model, level) options and refusals."""
    fleet = _fleet(fleet)
    policy = roster.get("policy", {}).get("selector", {})
    mode = mode or policy.get("mode_by_role", {}).get(role) or (
        "write" if FAMILY_BY_ROLE.get(role) in {"coding-agent", "debug"} else "read-only")
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
                                   **{k: card[k] for k in ("price_1m", "funding") if k in card},
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
        cash_class = funding.classify(roster, pool, lane)
        legacy_gemini = funding.legacy_gemini(pool, harness)
        bounded_cash = cash_class == "metered" and not legacy_gemini
        privacy_reason = fleet.data_policy_refusal(lane, data_class)
        if privacy_reason:
            why = privacy_reason
        elif not key or not pool or harness not in WRAPPERS:
            why = "missing model/pool or unsupported wrapper"
        # BR-MIMO-001 / BR-CASH-002: documented explicit-only routes.
        elif (legacy_gemini or lane.get("paid_mimo")
              or roster.get("quota_pools", {}).get(pool, {}).get("explicit_only")) and pool not in (explicit_pools or set()):
            why = "paid pool requires explicit --allow"
        elif (not bounded_cash and fleet.data_policy_refusal(lane, "code")
              and (harness != "opencode" or mode != "read-only")):
            why = "public-only lane requires prompt-contained OpenCode read-only call"
        # BR-CASH-001
        elif funding.explicitly_unknown(roster, pool, lane, cash_class):
            why = "funding is unknown"
        elif bounded_cash and "price_1m" in lane and funding.rates(lane["price_1m"]) is None:
            why = "missing or invalid tariff"
        elif allowed is not None and pool not in allowed:
            why = "pool forbidden for role"
        elif lane.get("access_status") != "verified" or lane.get("admission_status") != "active":
            why = "access or admission not active/verified"
        elif lane.get("value_routing_managed") and role not in lane.get("roles", []):
            why = "role not declared for refreshed lane"
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
        elif lane.get("paid_mimo") and role not in lane.get("roles", []):
            why = "paid MiMo requires a prompt-contained reasoning or long-context role"
        elif (harness == "chatgpt-chat" or lane.get("chatgpt_pi")) and role not in lane.get("roles", []):
            why = "role not declared for configured ChatGPT lane"
        elif harness == "chatgpt-chat" and not lane.get("worker_label"):
            why = "ChatGPT lane has no saved worker label"
        elif harness == "chatgpt-chat" and fleet.chatgpt_pro.blocked(roster, runtime, lane):
            why = fleet.chatgpt_pro.blocked(roster, runtime, lane)
        elif lane.get("gateway_service") and (lane.get("gateway_status", {}).get("quota_blocked")
                                            or lane.get("gateway_status", {}).get("rate_limited")):
            why = "worker quota paused"
        if why:
            rejected.append(f"{label}: {why}")
            continue
        identity = (pool, key)
        if identity not in states:
            states[identity] = fleet.current_pool_state(runtime, pool, roster=roster, model_key=key)
        state, pool_evidence = states[identity]
        band = _band(fleet, roster, runtime, pool, state, pool_evidence)
        if pool == fleet.ROUTING_POOL and band == "critical":
            role_policies = roster.get("routing", {}).get("roles", {})
            role_policy = role_policies.get(role) or role_policies.get("default") or {}
            admitted = role_policy.get("critical", role_policy.get("quality_first", []))
            admitted, _ = fleet.apply_model_toggles(admitted, roster, runtime)
            if lane.get("lane_id") not in admitted:
                rejected.append(f"{label}: outside CRITICAL role allowlist")
                continue
        paid_cash = None
        cash_tariff = None
        # BR-CASH-002: the explicit legacy Gemini tool loop keeps the pre-reservation gates.
        if bounded_cash:
            try:
                if mode != "read-only" or modality != "text":
                    raise BudgetError("paid route is prompt-only read-only text")
                route, rates, bounds, budget = route_config(roster, lane, role)
                # BR-CASH-003: metadata-only select checks the class; dispatch also has the exact text.
                if reason := paid_refusal(route, data_class, prompt):
                    raise BudgetError(reason)
                # BR-CASH-004: neither a cap, a target, nor an allow list grants automatic selection.
                if budget.settings.get("automatic") is not True:
                    raise BudgetError("cash envelope does not grant automatic selection")
                worst = ceiling_usd(rates, *bounds[:2])
                paid_cash = funding.quote("metered", budget.snapshot(require=worst), worst_case=float(worst))
                paid_cash.update(tariff_metadata(route["tariff"]))
                cash_tariff = dict(zip(("in", "out"), map(float, rates)))
            except (BudgetError, OSError) as exc:
                rejected.append(f"{label}: {exc}")
                continue
        option = {"pool": pool, "harness": harness, "model_key": key, "model": key,
                  "model_identity": canonical_model(key, roster),
                  "run_as": lane.get("model") or lane.get("selector") or key,
                  "mode": mode, "band": band, "pool_state": state,
                  "provider": lane.get("provider"), "funding": cash_class,
                  "data_class": data_class or os.environ.get("FLEET_DATA_CLASS", "private")}
        if "price_1m" in lane:
            option["source_price_1m"] = lane["price_1m"]
        if paid_cash is not None:
            option["cash"] = paid_cash
            option["cash_tariff_1m"] = cash_tariff
            option["cash_quality_floors"] = route.get("quality_floors")
        if fleet.data_policy_refusal(lane, "code"):
            option["public_only"] = True
        if lane.get("paid_mimo") is True:
            option["paid_mimo"] = True
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
        scored_levels = entry.get("scored_levels_by_role")
        if "scored_levels_by_role" in entry:
            permitted = scored_levels.get(role) if isinstance(scored_levels, dict) else None
            if not isinstance(permitted, list) or not permitted or any(level not in levels for level in permitted):
                rejected.append(f"{label}: no permitted scored effort for role {role}")
                continue
        else:
            permitted = None
        # If switches make this model a replacement, score only levels it will
        # actually run. Explicit enumeration must not bypass stand-in ceilings.
        replacements = fleet.stand_ins(roster, runtime, pool).values()
        stand_in = key in replacements or option["run_as"] in replacements
        for requested in (permitted if permitted is not None else levels or [None]):
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
            if permitted is not None and level not in permitted:
                continue  # a stand-in clamp must not reintroduce a forbidden level
            if harness == "copilot":
                if (level != "service-chosen" or mode != "read-only" or modality != "text"
                        or (lane.get("selector") != "auto" and (pool == "github-copilot-student"
                            or role not in lane.get("roles", []) or not lane.get("soft_session_credit_cap")))):
                    rejected.append(f"{label}: unsupported Copilot model, role, mode or budget")
                    continue
            if harness == "openrouter" and requested is not None:
                rejected.append(f"{label}/{requested}: wrapper has no effort flag")
                continue
            actual = dict(option, level=level, effort=level, effort_reason=resolved["reason"], stand_in=stand_in)
            if harness == "agy" and re.search(r"-(low|medium|high|xhigh|max)$", actual["run_as"]):
                actual["run_as"] = re.sub(r"-(low|medium|high|xhigh|max)$", "-" + level, actual["run_as"])
            identity = (pool, harness, key, level)
            if identity not in seen:
                seen.add(identity)
                options.append(actual)
    return options, rejected


def _window_projection(window: dict, fleet: Any) -> tuple[float | None, dict]:
    value = _number(window.get("projected_used_percent_at_reset"))
    forecast = window.get("surplus")
    if not isinstance(forecast, dict):
        forecast = fleet.window_surplus(window)
    if value is None:
        value = _number(forecast.get("projected_used_percent_at_reset"))
    if value is None:
        eta, used, left = (_number(window.get(k)) for k in ("eta_seconds", "used_percent", "seconds_to_reset"))
        if eta is not None and eta > 0 and used is not None and left is not None:
            value = used + (100 - used) * left / eta
    return value, forecast


def _claude_weekly_projection(roster: dict, runtime: dict, option: dict, fleet: Any) -> float | None:
    pool = option["pool"]
    if option["harness"] != "claude" or option["funding"] != "included":
        return None
    state, evidence = fleet.current_pool_state(fleet._without_levels(runtime), pool, roster=roster,
                                               model_key=option["model_key"])
    if state in {"UNKNOWN", "GAUGE_ERROR", "EXHAUSTED"} or evidence.get("confidence") == "stale":
        return None
    weekly = []
    for name in evidence.get("applicable_windows", []):
        window = evidence["windows"][name]
        if window.get("window_minutes") != 7 * 24 * 60:
            continue
        value, _ = _window_projection(window, fleet)
        used = _number(window.get("used_percent"))
        if value is None or used is None:
            return None  # A boolean "lasts to reset" cannot prove a 20% reserve.
        weekly.append(max(used, value))
    return max(weekly, default=None)


def _pool_price(roster: dict, runtime: dict, pool: str, policy: dict, fleet: Any,
                *, model_key: str | None = None) -> dict:
    """Price the greatest applicable pressure, with actual usage flooring forecasts.

    Pool summaries exclude model-only windows. Option prices also include windows
    naming that model's family, using the oracle label or its scoped window id.
    Spend levels govern admission, not the measured price or displayed quota.
    """
    if fleet.pool_has_no_known_limit(roster, pool):
        state, _ = fleet.current_pool_state(runtime, pool, roster=roster)
        return {"lambda": 0.0, "state": state, "binding_window": None, "window": {},
                "window_minutes": None, "projected_used_percent_at_reset": None,
                "projection_unknown": False, "projection_basis": "none-known"}

    state, _ = fleet.current_pool_state(runtime, pool, roster=roster, model_key=model_key)
    measured_state, evidence = fleet.current_pool_state(fleet._without_levels(runtime), pool,
                                                        roster=roster, model_key=model_key)
    priced = []
    target = policy.get("target", DEFAULTS["target"])
    lambda0 = policy.get("lambda0", DEFAULTS["lambda0"])
    unknown_price = float(policy.get("lambda_unknown", .5 * lambda0))
    if not math.isfinite(unknown_price) or unknown_price < 0:
        raise fleet.FleetError("lambda_unknown must be finite and nonnegative")
    if measured_state == "GAUGE_ERROR":
        # A broken configured gauge gets at least the full reference price
        # (lambda0), rather than the half-price unmeasured fallback or surplus's
        # zero: prefer similarly capable measured pools, but keep this eligible.
        return {"lambda": max(unknown_price, lambda0), "state": state,
                "binding_window": None, "window": {}, "window_minutes": None,
                "projected_used_percent_at_reset": None, "projection_unknown": True,
                "projection_basis": "gauge_error", "gauge_error": evidence["gauge_error"]}
    for name, window in evidence.get("windows", {}).items():
        if name not in evidence.get("applicable_windows", evidence.get("windows", {})):
            continue
        value, forecast = _window_projection(window, fleet)
        used = _number(window.get("used_percent")) or 0
        pressure = max(used, value) if value is not None else used
        price = pool_lambda(pressure, target, lambda0)
        # An oracle pace that says the window lasts to its reset is a surplus,
        # not an unknown projection. It carries no numeric projection, so
        # without this the documented zero price for a surplus collapses into
        # lambda_unknown and a barely-used monthly window is priced like a
        # squeezed pool. A crude elapsed-window fallback is still not a
        # projection; only the oracle's observed pace can make this claim.
        pace_surplus = (value is None and forecast.get("basis") == "observed_pace"
                        and forecast.get("surplus") is True)
        if pace_surplus:
            price = 0.0
        elif value is None:
            price = max(unknown_price, price)
        priced.append((name, window, value, pressure, price, pace_surplus))
    # Every applicable window contributes its own forecast/unknown-risk price.
    # Adding another window must never erase a more restrictive priced limit.
    binding = max(priced, key=lambda row: (row[4], row[3]),
                  default=(None, {}, None, None, unknown_price, False))
    name, window, value, pressure, price, pace_surplus = binding
    return {"lambda": price, "state": state, "binding_window": name,
            "window": window,
            "window_minutes": window.get("window_minutes"),
            "projected_used_percent_at_reset": value,
            "pressure_percent": pressure,
            "projection_unknown": value is None and not pace_surplus,
            "projection_basis": ("observed_pace" if pace_surplus else
                                 "snapshot_or_window_pace" if value is not None else "unknown")}


def window_routing_pace(window: dict, fleet: Any) -> dict | None:
    """The CodexBar pace of one window plus the signed value routing uses for it.

    Without an oracle projection the value is the pace itself, (elapsed% - used%) / 100. With
    one (a projected total at reset, or an ETA to exhaustion), the value is the same quantity
    computed from that better estimate: elapsed fraction * (1 - projected / 100). Both are the
    allowance the reset destroys at the expected rate (+) or the shortfall before it (-),
    weighted by how much of the window the estimate has seen.
    """
    pace = fleet.window_pace(window)
    if not pace:
        return None
    elapsed = pace["elapsed_percent"] / 100
    used = pace["used_percent"]
    projected = _number(window.get("projected_used_percent_at_reset"))
    if projected is None:
        eta, left = _number(window.get("eta_seconds")), _number(window.get("seconds_to_reset"))
        if eta is not None and eta > 0 and left is not None:
            projected = used + (100 - used) * left / eta
    if projected is None:
        value, projected_basis = pace["delta_percent"] / 100, False
    else:
        value, projected_basis = elapsed * (1 - max(projected, used) / 100), True
    return dict(pace, value=max(-1.0, min(1.0, value)), projected=projected_basis)


# BR-SELECT-002
def pool_pace(roster: dict, runtime: dict, pool: str, fleet: Any, *,
              model_key: str | None = None) -> dict:
    """One signed pace for a pool, in allowance fractions: + surplus, - deficit.

    A task spends from every applicable window at once, so any window running out before its
    reset makes the pool one to avoid: the worst deficit is the pool's pace. Otherwise the
    largest surplus is, since that is the allowance the next reset destroys. Deficits within
    PACE_DEFICIT_DEAD_ZONE count as on pace. Unknown, stale or unlimited pools are neutral (0).
    """
    if fleet.pool_has_no_known_limit(roster, pool):
        return {"value": 0.0, "basis": "none-known", "window": None}
    state, evidence = fleet.current_pool_state(fleet._without_levels(runtime), pool,
                                               roster=roster, model_key=model_key)
    if state in {"GAUGE_ERROR", "UNKNOWN"} or evidence.get("gauge_error"):
        return {"value": 0.0, "basis": "unknown", "window": None}
    windows = evidence.get("windows") or {}
    applicable = evidence.get("applicable_windows", windows)
    readings = [(name, window_routing_pace(window, fleet)) for name, window in windows.items()
                if name in applicable]
    readings = [(name, pace) for name, pace in readings if pace]
    if not readings:
        return {"value": 0.0, "basis": "unknown", "window": None}
    deficits = [(name, pace) for name, pace in readings if pace["value"] <= -PACE_DEFICIT_DEAD_ZONE]
    if deficits:
        name, pace = min(deficits, key=lambda row: row[1]["value"])
        value = pace["value"]
    else:
        name, pace = max(readings, key=lambda row: row[1]["value"])
        value = max(0.0, pace["value"])
    return {"value": round(value, 4), "basis": "projection" if pace["projected"] else "window_pace",
            "window": name, "status": pace["status"], "words": pace["words"]}


# BR-SELECT-002
def recent_use(state_dir: Path, fleet: Any, half_life_s: float, now: Any = None) -> dict:
    """{(pool, model_key): decayed count of recent runs}, finished or still in flight.

    Finished runs come from the wrapper ledger (every route, dispatched or not); runs still
    in flight from dispatch receipts, which exist from launch, so a parallel burst sees its own
    earlier picks. One dispatch counts once. Weight halves every `half_life_s`.
    """
    now = now or fleet.utc_now()
    horizon = 8 * half_life_s
    counts: dict = {}
    seen: set = set()

    def add(pool: Any, model: Any, started: Any, dispatch: Any) -> None:
        if not pool or not model or not started:
            return
        if dispatch:
            if dispatch in seen:
                return
            seen.add(dispatch)
        try:
            age = (now - fleet.parse_iso(str(started))).total_seconds()
        except (TypeError, ValueError):
            return
        if 0 <= age <= horizon:
            key = (str(pool), str(model))
            counts[key] = counts.get(key, 0.0) + 0.5 ** (age / half_life_s)

    dispatches = Path(state_dir) / "dispatches"
    try:
        entries = list(os.scandir(dispatches))
    except OSError:
        entries = []
    cutoff = now.timestamp() - horizon
    for entry in entries:
        try:
            if not entry.is_dir() or entry.stat().st_mtime < cutoff:
                continue
            record = json.loads((Path(entry.path) / "dispatch.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            add(record.get("pool"), record.get("selected_model"), record.get("started_at"),
                record.get("dispatch_id"))
    path = Path(state_dir) / "runs.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - RECENT_RUNS_TAIL_BYTES))
            lines = handle.read().splitlines()
        if size > RECENT_RUNS_TAIL_BYTES:
            lines = lines[1:]   # the first line of a tail is usually cut
    except OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("schema") == "crossfeed-model-run/v1":
            add(record.get("quota_pool"), record.get("selected_model"), record.get("started_at"),
                record.get("dispatch_id"))
    return counts


def _variety_policy(policy: dict, fleet: Any) -> dict:
    raw = policy.get("variety", {})
    if raw is False:
        raw = {"quality_band": 0.0}
    if not isinstance(raw, dict):
        raise fleet.FleetError("selector variety must be an object or false")
    merged = dict(VARIETY, **{key: value for key, value in raw.items() if key != "quality_band"})
    band = raw.get("quality_band", VARIETY["quality_band"])
    band = {stakes: band for stakes in VARIETY["quality_band"]} if _number(band) is not None else (
        dict(VARIETY["quality_band"], **band) if isinstance(band, dict) else None)
    if band is None or any(_number(value) is None or not 0 <= value <= 1 for value in band.values()):
        raise fleet.FleetError("selector variety quality_band must be in [0, 1]")
    merged["quality_band"] = band
    for key in ("pace_weight", "recency_half_life_s", "temperature"):
        value = _number(merged.get(key))
        if value is None or value < 0 or (key != "pace_weight" and value == 0):
            raise fleet.FleetError(f"selector variety {key} must be positive")
        merged[key] = value
    return merged


# BR-SELECT-002
def _vary(scored: list, greedy: dict, stakes: str, weights: dict, variety: dict,
          pace_of: Any, recency_of: Any) -> tuple[list, list]:
    """(candidates, probabilities): the lanes a varied choice may take, and how likely each is.

    Candidates are measured options within the quality band of the option plain scoring picked,
    on both estimated quality and score, so quota price and latency still count. Hard gates ran
    before scoring, so nothing here can admit an option. One option per lane, its best-scored
    level. Preference is pace (surplus favoured, deficit avoided) minus decayed recent use, minus
    how far behind the scored pick the option is (0 to 1 across the band); the draw is a softmax
    over preference, so every candidate keeps a chance and none is certain.
    """
    band = variety["quality_band"].get(stakes, 0.0)
    candidates = [greedy]
    if band > 0 and not greedy["q"]["unknown"]:
        lanes = {(greedy["pool"], greedy["model_key"])}
        for item in scored:
            lane = (item["pool"], item["model_key"])
            if (lane in lanes or item["q"]["unknown"] or item.get("pro_fallback")
                    or item["quality"] < greedy["quality"] - band
                    or item["score"] < greedy["score"] - weights.get(stakes, 1.0) * band):
                continue
            lanes.add(lane)
            candidates.append(item)
    if len(candidates) == 1:
        return candidates, [1.0]
    preferences = []
    recency = recency_of()
    width = weights.get(stakes, 1.0) * band
    for item in candidates:
        pace = pace_of(item["pool"], item["model_key"])
        used = recency.get((item["pool"], item["model_key"]), 0.0)
        # Scoring's own verdict stays a tilt: 0 for its pick, -1 at the edge of the band.
        behind = (item["score"] - greedy["score"]) / width
        preference = variety["pace_weight"] * pace["value"] - used + behind
        item["variety"] = {"pace": pace, "recent_use": round(used, 4), "score_behind": round(behind, 4),
                           "preference": round(preference, 4)}
        preferences.append(preference)
    top = max(preferences)
    raw = [math.exp((value - top) / variety["temperature"]) for value in preferences]
    probabilities = [value / sum(raw) for value in raw]
    for item, probability in zip(candidates, probabilities):
        item["variety"]["probability"] = round(probability, 6)
    return candidates, probabilities


# BR-CASH-004
def _cash_band(scored, greedy, stakes, weights, variety, pace_of):
    """Compare cash only among measured peers without replacing the quota/latency score."""
    if stakes == "irreversible" or greedy["q"]["unknown"] or not any(map(funding.requires_reservation, scored)):
        return scored, None
    band = variety["quality_band"].get(stakes, 0)
    peers = [item for item in scored if not item["q"]["unknown"] and not item.get("pro_fallback")
             and item["quality"] >= greedy["quality"] - band
             and item["score"] >= greedy["score"] - weights[stakes] * band]
    if not any(map(funding.requires_reservation, peers)):
        return scored, None  # Leave today's nonmetered variety and tie-breaks alone.
    expiring = [item for item in peers if item["funding"] == "included"
                and pace_of(item["pool"], item["model_key"])["value"] > 0]
    if expiring:
        return expiring, "expiring-included-quota"

    def marginal(item):
        cash = item["cash"]
        estimate = cash["estimate_usd"]
        return estimate if estimate is not None else cash["worst_case_usd"]

    known = [(marginal(item), item) for item in peers if marginal(item) is not None]
    if not known:
        return scored, None
    cheapest = min(value for value, _ in known)
    preferred = [item for value, item in known if value == cheapest]
    return preferred, "lower-marginal-cash" if len(preferred) < len(peers) else None


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


def _legacy_pair(value):
    if not isinstance(value, dict):
        return None
    pair = tuple(_number(value.get(side)) for side in ("in", "out"))
    return pair if all(number is not None and number >= 0 for number in pair) else None


def _cost(row: dict, allowance: float | None) -> tuple[float | None, float | None]:
    """Legacy score inputs: do not normalize price aliases or change its arithmetic."""
    tokens, prices = (_legacy_pair(row.get(key)) for key in ("tokens_per_task", "price_1m"))
    if tokens is None or prices is None:
        return None, None
    usd = (tokens[0] * prices[0] + tokens[1] * prices[1]) / 1_000_000
    return usd, usd / allowance * 100 if allowance is not None else None


def _display_usd(row, prices):
    tokens = _legacy_pair(row.get("tokens_per_task"))
    if tokens is None or prices is None:
        return None
    amount = float(sum(Decimal(str(token)) * Decimal(str(price))
                       for token, price in zip(tokens, prices)) / 1_000_000)
    return amount if math.isfinite(amount) else None


def _task_cost(roster: dict, option: dict, row: dict, binding: dict,
               median_usd: float | None) -> tuple[dict, list[str]]:
    pool = option["pool"]
    if binding.get("projection_basis") == "none-known":
        chat = option.get("harness") == "chatgpt-chat" or pool == "chatgpt-work"
        usd = None if chat else _display_usd(row, _legacy_pair(row.get("price_1m")))
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
    request_model = {}
    basis = "api_equivalent_allowance" if percent is not None else "unknown"
    if measured is not None and measured >= 0:
        percent, basis = measured, "measured"
    elif pool == "opencode-go":
        profile = roster.get("quota_pools", {}).get(pool, {})
        legacy = profile.get("request_estimates", {})
        table = legacy.get("per_5h_week_month", {})
        estimates = table.get(option["model_key"], table.get(option["run_as"], {}))
        if isinstance(estimates, dict) and option["level"] in estimates:
            estimates = estimates[option["level"]]
        name = str(binding.get("binding_window") or "").lower()
        minutes = binding.get("window_minutes")
        column = 2 if "month" in name else 1 if "week" in name else 0 if "5h" in name or "five_hour" in name else (
            0 if minutes and minutes <= 300 else 1 if minutes and minutes <= 10080 else 2 if minutes else None)
        count = go_quota_tariffs.typical_requests(profile.get("plan"), option["model_key"], column)
        tariff_source = go_quota_tariffs.SOURCE if count is not None else legacy.get("source")
        if count is None and column is not None:
            if isinstance(estimates, (list, tuple)) and len(estimates) > column:
                count = _number(estimates[column])
            elif isinstance(estimates, dict):
                count = _number(estimates.get(("5h", "week", "month")[column]))
        if count is not None and count > 0:
            requests = _number(own.get("requests_per_task"))
            observed = requests is not None and requests > 0
            requests = requests if observed else 1
            percent = 100 / count * requests
            basis = "go_request_estimate"
            request_model = {"requests_per_task": requests, "typical_requests_in_window": count,
                             "request_count_basis": "observed_median" if observed else "unmeasured_one_request",
                             "request_estimate_source": tariff_source}
            if not observed:
                flags.append("go_task_request_count_unmeasured")
    nominal = _number(roster.get("quota_pools", {}).get(pool, {}).get("nominal_percent_per_task", .5))
    nominal = nominal if nominal is not None and nominal >= 0 else .5
    if percent is None and usd is not None and median_usd is not None and median_usd > 0:
        percent, basis = usd / median_usd * nominal, "relative_proxy"
        flags.append("cost_relative_proxy")
    if percent is None:
        flags.append("cost_unknown_nominal")
    return {"estimated_usd": usd, "percent": percent, "allowance_usd": allowance,
            "estimated": basis != "measured", "unknown": percent is None, "basis": basis,
            "penalty_percent": percent if percent is not None else nominal, **request_model}, flags


# BR-DATA-001
def _command(option: dict, role: str, receipt: Path, *, fleet: Any = None) -> list[str]:
    harness, level = option["harness"], option["level"]
    if harness == "chatgpt-chat" and ((level != "service-chosen" and
                                (not option.get("worker_label") or level != option.get("worker_level")))
                               or option["mode"] != "read-only"):
        raise _fleet(fleet).FleetError("Crossfeed Chat command requires read-only mode and owner-configured effort")
    argv = ["env", f"FLEET_SELECTION_FILE={receipt}",
            "FLEET_DATA_CLASS=" + option.get("data_class", "private"),
            str(Path(__file__).resolve().parent / WRAPPERS[harness])]
    if harness in {"codex", "claude"}:
        argv += ["--model", option["run_as"], "--role", role]
    elif harness == "agy":
        argv += ["--lane", option["lane_id"], "--model", option["run_as"], "--role", role]
    elif harness in {"opencode", "openrouter"}:
        argv += ["--lane", option["lane_id"]]
        if harness == "openrouter" and option.get("funding") == "metered":
            argv += ["--paid-mimo" if option.get("paid_mimo") else "--paid", "--role", role]
        if harness == "opencode":
            argv += ["--effort-role", role]
            if option.get("public_only"):
                argv += ["--direct"]
    elif harness == "copilot":
        argv += ["--model", option["run_as"]]
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


# BR-SELECT-002
def select_option(roster: dict, runtime: dict, state_dir: Path, role: str, *,
                  stakes: str = "normal", family: str | None = None,
                  exclude_lineage: str | None = None, mode: str | None = None,
                  modality: str = "text", allow: str | Path | None = None,
                  lead: str | None = None, fleet: Any = None,
                  target: tuple[str, str] | None = None, data_class: str | None = None,
                  prompt: str | None = None, value_mode: str = "best-value") -> dict:
    """Return choice/top3/lambdas and write an immutable replay receipt."""
    fleet = _fleet(fleet)
    state_dir = Path(state_dir).expanduser().resolve()
    fleet.chatgpt_pro.refresh(roster, state_dir)
    policy = roster.get("policy", {}).get("selector", {})
    if value_mode not in {"best-value", "absolute-best"}:
        raise fleet.FleetError(f"unknown value mode: {value_mode}")
    absolute = value_mode == "absolute-best"
    floors = policy.get("quality_floors_by_role", {})
    if not isinstance(floors, dict):
        raise fleet.FleetError("quality_floors_by_role must be a role-to-grade map")
    role_floor = _number(floors.get(role))
    if role in floors and (role_floor is None or not 0 < role_floor <= 1):
        raise fleet.FleetError(f"invalid quality floor for {role}; expected a grade in (0, 1]")
    entitled_only = policy.get("entitled_only", False)
    if not isinstance(entitled_only, bool):
        raise fleet.FleetError("entitled_only must be a boolean")
    if stakes not in {"low", "normal", "high", "irreversible"}:
        raise fleet.FleetError(f"unknown stakes: {stakes}")
    role_family = policy.get("families_by_role", {}).get(role) or FAMILY_BY_ROLE.get(role, "coding-agent")
    if role_floor is not None and family is not None and family != role_family:
        raise fleet.FleetError(f"{role} quality floor requires task family {role_family}")
    family = family or role_family
    if family not in FAMILIES:
        raise fleet.FleetError(f"unknown task family: {family}")
    allow_list = parse_allow(allow, fleet)
    explicit_pools = {entry.split(":", 1)[0] for entry in allow_list or []}
    options, rejected = enumerate_options(roster, runtime, role, mode=mode, modality=modality,
                                          exclude_lineage=exclude_lineage, fleet=fleet, explicit_pools=explicit_pools,
                                          data_class=data_class, prompt=prompt)
    if absolute or entitled_only:
        for option in options:
            if option["funding"] not in {"included", "verified-free"}:
                rejected.append(f"{option['model_key']}/{option['level']}: value policy permits entitlement only")
        options = [option for option in options if option["funding"] in {"included", "verified-free"}]
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
        daily_cap = _number(roster.get("quota_pools", {}).get(pool, {}).get("daily_usd_cap"))
        if daily_cap is not None and pool not in capped and not funding.requires_reservation(option):
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
    rows = evidence_index(roster, evidence.get("rows", []))
    def evidence_for(option):
        row = rows.get((option["model_identity"], option["level"]), {})
        if "source_price_1m" in option:
            price = option["source_price_1m"]
            return dict(row, price_1m=price if isinstance(price, dict) else {})
        return row
    pool_cash = fleet.cash_by_pool(roster, state_dir)
    scored = []
    weights = dict(DEFAULTS["stakes_weights"], **policy.get("stakes_weights", {}))
    uncertainty_weight = max(1e-9, float(policy.get("uncertainty_weight", DEFAULTS["uncertainty_weight"])))
    mu = float(policy.get("mu", DEFAULTS["mu"])) * (0 if stakes == "irreversible" else .5 if stakes == "high" else 1)
    high_cost_cap = _number(policy.get("high_cost_cap", DEFAULTS["high_cost_cap"]))
    if high_cost_cap is None or high_cost_cap < 0:
        raise fleet.FleetError("high_cost_cap must be finite and nonnegative")
    pool_costs = {}
    for option in options:
        usd, _ = _cost(evidence_for(option), None)
        if usd is not None:
            pool_costs.setdefault(option["pool"], []).append(usd)
    medians = {pool: statistics.median(costs) for pool, costs in pool_costs.items()}
    have_hard_grade = stakes == "high" and any(
        evidence_for(option).get("q_by_stakes", {}).get("high", {}).get(family, {}).get("unknown") is False
        for option in options)
    for option in options:
        row = evidence_for(option)
        strata = row.get("q_by_stakes", {})
        scoped = strata.get(stakes, {}).get(family)
        if scoped is None and stakes == "irreversible":
            scoped = strata.get("high", {}).get(family)
        if (stakes == "high" and not have_hard_grade and scoped is not None and scoped.get("unknown")
                and family in strata.get("low", {})):
            # Preserve legacy high-value routing without mislabeling the fit
            # as a hard grade. Irreversible selection still fails closed.
            scoped = {**strata["low"][family], "basis": "local_fit_no_hard_benchmark"}
        if role_floor is not None:
            # A hard role cannot use the easy-fit stratum, even at low stakes.
            scoped = strata.get("high", {}).get(family)
        q = scoped if scoped is not None else row.get("q", {}).get(family) or {}
        mean, sd = _number(q.get("mean")), _number(q.get("sd"))
        unknown = mean is None or not q.get("n_sources") and not row.get("own", {}).get(family, {}).get("trials")
        unknown = unknown or q.get("unknown", False) or "unknown" in (row.get("flags") or [])
        if role_floor is not None and (unknown or mean < role_floor or mean > 1):
            grade = "unknown" if unknown else f"{mean:.3f}"
            rejected.append(f"{option['model_key']}/{option['level']}: {family} grade {grade} "
                            f"does not clear {role} floor {role_floor:.3f}")
            continue
        if absolute and unknown:
            rejected.append(f"{option['model_key']}/{option['level']}: quality unknown; absolute-best needs a grade")
            continue
        # BR-CASH-004: an owner floor applies to the exact task family and effort.
        if funding.requires_reservation(option):
            floors = option.get("cash_quality_floors")
            floor = _number(floors.get(family)) if isinstance(floors, dict) else None
            if (unknown or q.get("imputation") is not None or floor is None
                    or not 0 < floor <= 1 or not floor <= mean <= 1):
                rejected.append(f"{option['model_key']}/{option['level']}: paid quality must be measured "
                                f"at the owner floor for {family}")
                continue
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
        telemetry = row.get("source_telemetry", {})
        source = telemetry.get(option["pool"] + "/" + option["harness"], telemetry.get(option["pool"], {}))
        if _number(source.get("latency_s")) is not None:
            latency = _number(source["latency_s"])
        penalty = price * cost["penalty_percent"]
        if stakes == "high":
            penalty = min(penalty, high_cost_cap)
        score = quality if absolute or stakes == "irreversible" else weights[stakes] * quality - penalty - mu * (latency or 0)
        cash = option.get("cash") or funding.quote(option["funding"])
        if funding.legacy_gemini(option["pool"], option["harness"]):
            cash = pool_cash[option["pool"]]
        if (api_equivalent := _display_usd(row, funding.rates(row.get("price_1m")))) is not None:
            cash = dict(cash, api_equivalent_usd=api_equivalent)
        if option.get("cash_tariff_1m") is not None:
            # Model-market prices never estimate bills for a specific funding account.
            estimate = _display_usd(row, funding.rates(option["cash_tariff_1m"]))
            if estimate is not None and estimate > cash["worst_case_usd"]:
                estimate = None  # Recorded tokens outside this route's bound do not price this task.
            cash = dict(cash, estimate_usd=estimate)
        scored.append(dict(option, cash=cash, quality=quality, quality_basis=q.get("basis"),
                           q={"mean": mean, "sd": sd, "unknown": unknown},
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
                                  (item['cost']['estimated_usd'] if not absolute and stakes != 'irreversible'
                                   and 'source_price_1m' in item and item['cost']['estimated_usd'] is not None else math.inf),
                                  item["pool"], item["model_key"], item["level"]))
    if not scored:
        raise fleet.FleetError("no eligible selector option: " + "; ".join(rejected))
    explore = float(policy.get("explore", DEFAULTS["explore"])) if stakes == "low" and unknown_options else 0.0
    if not math.isfinite(explore) or not 0 <= explore <= 1:
        raise fleet.FleetError("explore must be in [0, 1]")
    claude_preference = False
    if not absolute and role_floor is not None and target is None:
        preferred = []
        for option in scored:
            projection = _claude_weekly_projection(roster, runtime, option, fleet)
            if projection is not None:
                option["claude_weekly_projected_percent"] = projection
                if projection < 80:
                    preferred.append(option)
        if preferred:
            claude_preference = True
            preferred_ids = {id(option) for option in preferred}
            scored = preferred + [option for option in scored if id(option) not in preferred_ids]
    greedy = scored[0]
    if not absolute and target is not None and any(option.get("pro_fallback") for option in scored):
        scored.sort(key=lambda option: (option.get("worker_level") != "xhigh", -option["score"]))
        greedy = scored[0]
        explore = 0.0
    variety = _variety_policy(policy, fleet)
    cash_preference = None
    if target is not None or absolute:
        candidates, probabilities = [greedy], [1.0]
    else:
        paces: dict = {}
        recent: list = []

        def pace_of(pool, model_key):
            if (pool, model_key) not in paces:
                paces[(pool, model_key)] = pool_pace(roster, runtime, pool, fleet, model_key=model_key)
            return paces[(pool, model_key)]

        def recency_of():
            if not recent:
                recent.append(recent_use(state_dir, fleet, variety["recency_half_life_s"]))
            return recent[0]

        if not claude_preference:
            preferred, cash_preference = _cash_band(scored, greedy, stakes, weights, variety, pace_of)
        candidates, probabilities = _vary(preferred, preferred[0], stakes, weights, variety, pace_of, recency_of)
    exploring = bool(explore and random.random() < explore)
    if exploring:
        choice = random.choice(unknown_options)
    elif len(candidates) == 1:
        choice = candidates[0]
    else:
        draw, choice = random.random(), candidates[-1]
        for item, probability in zip(candidates, probabilities):
            draw -= probability
            if draw < 0:
                choice = item
                break
    varied = {id(item): probability for item, probability in zip(candidates, probabilities)}
    for item in scored + unknown_options:
        item["selection_probability"] = (1 - explore) * varied.get(id(item), 0.0) + (
            explore / len(unknown_options) if item["q"]["unknown"] and unknown_options else 0)
    receipt = state_dir / "selections" / (str(uuid.uuid4()) + ".json")
    top3 = scored[:3]
    result = {"schema": "fleet-selection/v1", "choice": choice, "top3": top3,
              "selection_probability": choice["selection_probability"], "exploration": exploring,
              "explore": explore, "unknown_options": len(unknown_options),
              "graded_options": sum(not item["q"]["unknown"] for item in scored),
              "cash_preference": cash_preference, "value_mode": value_mode,
              "quality_floor": role_floor, "claude_preference": claude_preference,
              "variety": {"quality_band": variety["quality_band"].get(stakes, 0.0),
                          "pace_weight": variety["pace_weight"],
                          "recency_half_life_s": variety["recency_half_life_s"],
                          "temperature": variety["temperature"],
                          "candidates": [{"pool": item["pool"], "model_key": item["model_key"],
                                          "level": item["level"], **item.get("variety", {})}
                                         for item in candidates]},
              "lambdas": {pool: value["lambda"] for pool, value in prices.items()},
              "pool_prices": prices, "pool_cash": pool_cash, "role": role, "family": family, "stakes": stakes,
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
