"""Select an admitted model and level using local evidence and quota projections.

The fleet module is injected so routing and wrapper admission remain the source
of truth. Evidence and immutable dispatch receipts are local state, never roster
data. An API-equivalent cost is an estimate, not a measured subscription debit.
"""

from __future__ import annotations

from datetime import timedelta
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
from typing import Any, Callable

try:
    import funding
    import go_quota_tariffs
    import rating
    import rating_explore
    from success_evidence import SuccessEvidence
    from data_policy import paid_refusal
    from openrouter_budget import BudgetError, ceiling_usd, route_config, tariff_metadata
    from run_identity import FAMILY_BY_ROLE, canonical_model, evidence_index
except ModuleNotFoundError:
    from scripts import funding, go_quota_tariffs, rating, rating_explore
    from scripts.data_policy import paid_refusal
    from scripts.openrouter_budget import BudgetError, ceiling_usd, route_config, tariff_metadata
    from scripts.run_identity import FAMILY_BY_ROLE, canonical_model, evidence_index
    from scripts.success_evidence import SuccessEvidence


DEFAULTS = {"target": 90.0, "lambda0": 1.0, "uncertainty_weight": 1.0}
IMPORTANT_ROLES = frozenset({"hard-reasoning", "review", "debug", "implementation"})
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
        lane_level = lane.get("spend_level", "normal")
        if lane_level not in fleet.LEVELS:
            raise fleet.FleetError(f"{label}: invalid spend level {lane_level!r}")
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
        elif lane_level == "off":
            why = "lane spend level off"
        elif lane_level == "low" and (role not in {"implementation", "hard-reasoning"}
                                      or role not in lane.get("roles", [])):
            why = "low spend level is reserved for declared implementation or hard-reasoning fallback"
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
        if lane.get("chatgpt_pi"):
            option["chatgpt_pi"] = True
        if lane_level != "normal":
            option["spend_level"] = lane_level
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
        if busy or (lane_level == "low" and fleet.live_pool_leases(runtime, pool)):
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


# BR-SELECT-004
OPUS_HARD_REASONING = "claude-opus-5-5"
ASTRA_HARD_REASONING = "gpt-6.2-astra"


def _opus_quota_refusal(roster, runtime, fleet):
    """Read active real usage, independently of projected pace and the spend dial."""
    pool = "claude"
    now = fleet.utc_now()
    snapshot = runtime.get("quota_snapshots", {}).get(pool, {})
    for name, window in snapshot.get("windows", {}).items():
        if (fleet.parse_iso(window["reset_at"]) <= now or
                not fleet.quota_window_applies(roster, pool, name, window, OPUS_HARD_REASONING)):
            continue
        used = _number(window.get("used_percent"))
        minutes = window.get("window_minutes")
        if used is not None and minutes == 300 and used >= 100:
            return "Claude five-hour window exhausted"
        if used is not None and minutes == 10080 and used >= 90:
            return "Claude weekly window at least 90% used"
    return None


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
    largest surplus is, since that is the allowance the next reset destroys. Deficits under 5% count as on pace. Unknown, stale or unlimited pools are neutral (0).
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
    deficits = [(name, pace) for name, pace in readings if pace["value"] <= -0.05]
    if deficits:
        name, pace = min(deficits, key=lambda row: row[1]["value"])
        value = pace["value"]
    else:
        name, pace = max(readings, key=lambda row: row[1]["value"])
        value = max(0.0, pace["value"])
    return {"value": round(value, 4), "basis": "projection" if pace["projected"] else "window_pace",
            "window": name, "status": pace["status"], "words": pace["words"]}


# BR-CASH-004
def _cash_band(scored, stakes, policy):
    """Compare cash only among measured peers without replacing the quota/latency score."""
    if stakes == "irreversible" or not any(map(funding.requires_reservation, scored)):
        return scored, None
    # Retain the comparable-quality and score envelope for bounded cash only.
    weight = policy.get("stakes_weights", {}).get(stakes, {"low": 1, "normal": 2, "high": 40}[stakes])
    band = {"low": .05, "normal": .03, "high": .01}[stakes]
    mu = policy.get("mu", .001) * (.5 if stakes == "high" else 1)

    def comparable_score(item):
        penalty = item["pool_price"]["lambda"] * item["cost"]["penalty_percent"]
        penalty += item.get("capacity", {}).get("scarcity_penalty", 0)
        if stakes == "high":
            penalty = min(penalty, policy.get("high_cost_cap", 1.0))
        return weight * item["quality"] - penalty - mu * (item["latency_s"] or 0)

    greedy = max(scored, key=comparable_score)
    if greedy["q"]["unknown"]:
        return scored, None
    peers = [item for item in scored if not item["q"]["unknown"] and not item.get("pro_fallback")
             and item["quality"] >= greedy["quality"] - band
             and comparable_score(item) >= comparable_score(greedy) - weight * band]
    if not any(map(funding.requires_reservation, peers)):
        return scored, None  # Leave today's nonmetered variety and tie-breaks alone.
    expiring = [item for item in peers if item["funding"] == "included"
                and item["quota_pace"]["value"] > 0]
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


# BR-SELECT-003 · BR-SELECT-005
def routing_price(item):
    """Use only a debit and sourced capacity, or an actual incremental quote."""
    cash = item["cash"]
    if cash["funding"] == "metered":
        return _number(cash.get("estimate_usd"))
    if cash["funding"] not in {"included", "verified-free"}:
        return None
    cost, binding = item["cost"], item["pool_price"]
    capacity = item.get("capacity") or {}
    # Relative API-weighted debits lack a specific window. Treat them as
    # weekly planning proxies instead of shrinking them on a 5-hour gauge.
    minutes = 10080 if cost["basis"] == "relative_proxy" else binding["window_minutes"]
    if not minutes and cost["basis"] == "measured":
        minutes = 10080  # weekly planning proxy when the gauge has no window
    pace = binding["lambda"] + max(0.0, -item["quota_pace"]["value"])
    return rating.subscription_price(cost["percent"], capacity.get("weekly_usd_equivalent"),
                                     pace, window_minutes=minutes,
                                     scarcity=capacity.get("scarcity_penalty", 0))


def _unpriced_rank(item):
    """An unpriced fallback's grade/pressure ordering is not a price quote."""
    pressure = item["pool_price"]["lambda"] + max(0.0, -item["quota_pace"]["value"])
    return item["quality"] - pressure * item["cost"]["penalty_percent"] / 2


def _relay_fallback(scored, stakes):
    """A measured Pi coding relay is a fallback, never the cheapest route."""
    native = next((item for item in scored if not item.get("chatgpt_pi")), scored[0])
    if stakes not in {"low", "normal"} or native["pool"] != "codex":
        return None
    projected = native["pool_price"]["pressure_percent"]
    if projected is None or projected < 100:
        return None
    relays = [item for item in scored if item.get("chatgpt_pi") and not item["q"]["unknown"]
              and item["estimated_price"] is None and _unpriced_rank(item) > _unpriced_rank(native)]
    return max(relays, key=_unpriced_rank, default=None)


def _recent_working_pools(roster: dict, state_dir: Path,
                          included: set[tuple[str, str]], fleet: Any) -> set[str]:
    """Look for a successful operational receipt within one weekly refill period."""
    now = fleet.utc_now()
    pools = {pool for pool, _ in included}

    def recent(stamp):
        try:
            return now - timedelta(days=7) <= fleet.parse_iso(stamp) <= now
        except (AttributeError, TypeError, ValueError):
            return False

    lanes = [lane for lane in roster.get("lanes", [])
             if lane.get("quota_pool") in pools and lane.get("model_key")
             and lane.get("access_status") == "verified" and lane.get("admission_status") == "active"]
    active = included | {(lane["quota_pool"], lane["model_key"]) for lane in lanes}
    working = set()
    for lane in lanes:
        pool, model = lane["quota_pool"], lane["model_key"]
        # An operator smoke is not a checked acceptance rate or a quota reading.
        smoke = (roster.get("model_evidence", {}).get(model) or {}).get("admission_evidence") or {}
        if (lane.get("selector") and smoke.get("selector_requested") == lane["selector"]
                and smoke.get("result") == "PASS" and smoke.get("exit_code_reported_by_operator") == 0
                and recent(smoke.get("date"))):
            working.add(pool)
    pending = {pool for pool, _ in active} - working
    if not pending:
        return working
    try:
        with (state_dir / "runs.jsonl").open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not any(pool in line for pool in pending):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("schema") != "crossfeed-model-run/v1":
                    continue
                pool = record.get("quota_pool")
                if (pool in pending and (pool, record.get("model_identity")) in active
                        and record.get("status") == "ok" and record.get("returncode") == 0
                        and recent(record.get("ended_at"))):
                    working.add(pool)
                    pending.remove(pool)
                    if not pending:
                        break
    except OSError:
        pass  # A receipt we cannot read cannot justify an estimate.
    return working


# BR-SELECT-003 · BR-SELECT-005
def _capacity_headroom(roster: dict, runtime: dict, pool: str, model: str,
                       reference: float, fleet: Any,
                       recently_worked: Callable[[str], bool] | None = None) -> dict | None:
    """Explicit weekly-equivalent service capacity, not cash or a quota reading."""
    profile = roster.get("quota_pools", {}).get(pool, {}).get("value_capacity")
    if profile is None:
        return None
    capacity = _number(profile.get("weekly_usd_equivalent")) if isinstance(profile, dict) else None
    if capacity is None or capacity <= 0 or not isinstance(profile.get("source"), str) or not profile["source"].strip():
        raise fleet.FleetError(f"{pool}: value_capacity needs positive weekly_usd_equivalent and source")
    state, evidence = fleet.current_pool_state(fleet._without_levels(runtime), pool,
                                              roster=roster, model_key=model)
    applicable = set(evidence.get("applicable_windows", evidence.get("windows", {})))
    observations = []
    for name, window in evidence.get("windows", {}).items():
        if name not in applicable:
            continue
        used = _number(window.get("used_percent"))
        duration = _number(window.get("window_minutes"))
        normalized_name = name.lower()
        if used is not None and ("week" in normalized_name or "month" in normalized_name
                                 or (duration is not None and duration >= 10080)):
            observations.append((max(0., min(100., used)), name))
    used, window = max(observations, default=(None, None))
    remaining = capacity * (1 - used / 100) if used is not None else None
    basis = "observed" if remaining is not None else "unknown"
    estimate = None
    if remaining is None and recently_worked is not None:
        snapshot = runtime.get("quota_snapshots", {}).get(pool) or {}
        idle = snapshot.get("idle_windows", {})
        windows = snapshot.get("windows", {})
        applies = lambda rows: any(fleet.quota_window_applies(roster, pool, name, row, model)
                                   for name, row in rows.items())
        known_reader = state == "GAUGE_ERROR" or pool in fleet.quota_sources(roster)
        unusable = state in {"UNKNOWN", "GAUGE_ERROR"} and (applies(windows) or applies(idle))
        if unusable and known_reader and recently_worked(pool):
            # The midpoint neither presumes a just-refilled week nor prices a
            # missing clock as a fully consumed week. It is for pricing only.
            estimate = capacity / 2
            basis = "neutral_recent_work"
    priced_remaining = estimate if estimate is not None else remaining
    return {"weekly_usd_equivalent": capacity, "remaining_usd_equivalent": remaining,
            "window": window, "used_percent": used, "source": profile["source"],
            "reference_usd_equivalent": reference, "headroom_basis": basis,
            **({"estimated_remaining_usd_equivalent": estimate} if estimate is not None else {}),
            "scarcity_penalty": reference / (reference + (priced_remaining or 0.))}


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


def _planning_row(row):
    estimate = row.get("effort_estimate")
    if not estimate:
        return row
    return {**row, "tokens_per_task": estimate["tokens_per_task"], "price_1m": estimate["price_1m"]}


def _task_cost(roster: dict, option: dict, row: dict, binding: dict,
               median_usd: float | None, *, standardize: bool = True) -> tuple[dict, list[str]]:
    pool = option["pool"]
    plan = (row.get("effort_estimate") or {}) if standardize else {}
    priced = _planning_row(row) if plan else row
    units = plan.get("units", 1.0)
    planning = {"effort_units": units, "effort_estimate": plan} if plan else {}
    if binding.get("projection_basis") == "none-known":
        chat = option.get("harness") == "chatgpt-chat" or pool == "chatgpt-work"
        usd = None if chat else _display_usd(priced, _legacy_pair(priced.get("price_1m")))
        return {"estimated_usd": usd, "percent": None, "allowance_usd": None,
                "estimated": True, "unknown": True, "basis": "no_known_limit",
                "penalty_percent": 0.0, **planning}, ["chat_usage_unpriced" if chat else "usage_unpriced"]
    allowance = _allowance_usd(roster, pool, binding)
    usd, percent = _cost(priced, allowance)
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
            percent = 100 / count * requests * units
            basis = "go_request_estimate"
            request_model = {"requests_per_task": requests, "typical_requests_in_window": count,
                             "request_count_basis": "observed_median" if observed else "unmeasured_one_request",
                             "request_estimate_source": tariff_source}
            if not observed:
                flags.append("go_task_request_count_unmeasured")
    nominal = _number(roster.get("quota_pools", {}).get(pool, {}).get(
        "nominal_percent_per_task", rating.NOMINAL_TASK_PERCENT))
    nominal = nominal if nominal is not None and nominal >= 0 else .5
    if percent is None and usd is not None and median_usd is not None and median_usd > 0:
        percent, basis = usd / median_usd * nominal, "relative_proxy"
        flags.append("cost_relative_proxy")
    if percent is None:
        flags.append("cost_unknown_nominal")
    effort_proxy = {}
    if pool == "claude" and row.get("token_estimate") and basis in {"relative_proxy", "api_equivalent_allowance"}:
        effort_proxy = {"effort_proxy": {**row["token_estimate"],
                                        "tokens_per_task": row["tokens_per_task"],
                                        "weight_basis": "API prices, not known Claude Max weights"}}
    return {"estimated_usd": usd, "percent": percent, "allowance_usd": allowance,
            "estimated": basis != "measured", "unknown": percent is None, "basis": basis,
            "penalty_percent": percent if percent is not None else nominal * units,
            **request_model, **effort_proxy, **planning}, flags


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


# BR-SELECT-002, BR-SELECT-003, BR-SELECT-004
def select_option(roster: dict, runtime: dict, state_dir: Path, role: str, *,
                  stakes: str = "normal", family: str | None = None,
                  exclude_lineage: str | None = None, mode: str | None = None,
                  modality: str = "text", allow: str | Path | None = None,
                  lead: str | None = None, fleet: Any = None,
                  target: tuple[str, str] | None = None, data_class: str | None = None,
                  prompt: str | None = None, value_mode: str = "best-value",
                  persist_receipts: bool = True, collect_eligible: bool = False,
                  rating_stakes: str | None = None, no_explore: bool = False,
                  explore_seed: int | None = None, difficulty: str | None = None) -> dict:
    """Rank eligible options; read-only previews do not refresh or emit receipts."""
    fleet = _fleet(fleet)
    state_dir = Path(state_dir).expanduser().resolve()
    if persist_receipts:
        fleet.chatgpt_pro.refresh(roster, state_dir)
    policy = roster.get("policy", {}).get("selector", {})
    if value_mode not in {"best-value", "absolute-best"}:
        raise fleet.FleetError(f"unknown value mode: {value_mode}")
    rating_stakes = rating_stakes or ("critical" if value_mode == "absolute-best" else
                                     "important" if role in IMPORTANT_ROLES else "routine")
    if rating_stakes not in rating.STAKES:
        raise fleet.FleetError(f"unknown rating stakes: {rating_stakes}")
    entitled_only = policy.get("entitled_only", False)
    if not isinstance(entitled_only, bool):
        raise fleet.FleetError("entitled_only must be a boolean")
    if stakes not in {"low", "normal", "high", "irreversible"}:
        raise fleet.FleetError(f"unknown stakes: {stakes}")
    role_family = policy.get("families_by_role", {}).get(role) or FAMILY_BY_ROLE.get(role, "coding-agent")
    family = family or role_family
    difficulty = difficulty or ("hard" if role == "hard-reasoning" else "ordinary")
    if difficulty not in rating.DIFFICULTY:
        raise fleet.FleetError(f"unknown task difficulty: {difficulty}")
    if family not in FAMILIES:
        raise fleet.FleetError(f"unknown task family: {family}")
    prefer_opus = policy.get("prefer_opus_hard_reasoning", False)
    if not isinstance(prefer_opus, bool):
        raise fleet.FleetError("prefer_opus_hard_reasoning must be a boolean")
    hard_preference = prefer_opus and role == "hard-reasoning"
    prefer_opus = hard_preference and stakes != "irreversible"
    opus_refusal = _opus_quota_refusal(roster, runtime, fleet) if prefer_opus else None
    allow_list = parse_allow(allow, fleet)
    explicit_pools = {entry.split(":", 1)[0] for entry in allow_list or []}
    options, rejected = enumerate_options(roster, runtime, role, mode=mode, modality=modality,
                                          exclude_lineage=exclude_lineage, fleet=fleet, explicit_pools=explicit_pools,
                                          data_class=data_class, prompt=prompt)
    if entitled_only:
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
            if fleet.lead_blocks(runtime, roster, pressure, option):
                reason = "pressured lead scoped window excluded" if pressure.get("scoped_window") else "pressured lead pool excluded"
                rejected.append(f"{lead}/{option['model_key']}: {reason}")
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
    if hard_preference:
        retained = []
        for option in options:
            model = option["model_identity"]
            reason = ("Sonnet excluded from hard reasoning" if model.startswith("claude-sonnet-") else
                      opus_refusal if option["pool"] == "claude" and model == OPUS_HARD_REASONING else None)
            if reason:
                rejected.append(f"{option['model_key']}/{option['level']}: {reason}")
            else:
                retained.append(option)
        options = retained
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
            row = dict(row, price_1m=price if isinstance(price, dict) else {})
            estimate = row.get("effort_estimate")
            if estimate:
                rates = funding.rates(price)
                row["effort_estimate"] = {**estimate, "price_1m": dict(zip(("in", "out"), rates)) if rates else None}
            return row
        return row
    pool_cash = fleet.cash_by_pool(roster, state_dir)
    scored = []
    uncertainty_weight = max(1e-9, float(policy.get("uncertainty_weight", DEFAULTS["uncertainty_weight"])))
    pool_costs = {}
    for option in options:
        usd, _ = _cost(_planning_row(evidence_for(option)), None)
        if usd is not None:
            pool_costs.setdefault(option["pool"], []).append(usd)
    medians = {pool: statistics.median(costs) for pool, costs in pool_costs.items()}
    have_hard_grade = stakes == "high" and any(
        evidence_for(option).get("q_by_stakes", {}).get("high", {}).get(family, {}).get("unknown") is False
        for option in options)
    capacity_reference = _number(policy.get("capacity_reference_usd_equivalent", 2.30))
    if capacity_reference is None or capacity_reference <= 0:
        raise fleet.FleetError("capacity_reference_usd_equivalent must be positive")
    capacities, paces = {}, {}
    working_pools = None

    def recently_worked(pool):
        nonlocal working_pools
        if working_pools is None:
            included = {(o["pool"], o["model_identity"]) for o in options if o["funding"] == "included"}
            working_pools = _recent_working_pools(roster, state_dir, included, fleet)
        return pool in working_pools

    for option in options:
        row = evidence_for(option)
        strata = row.get("q_by_stakes", {})
        scoped = strata.get(stakes, {}).get(family)
        if scoped is None and stakes == "irreversible":
            scoped = strata.get("high", {}).get(family)
        hard_grade = strata.get("high", {}).get(family)
        bounded_cash = funding.requires_reservation(option)
        if hard_grade is not None and ((difficulty == "hard" and not hard_grade.get("unknown"))
                                       or (bounded_cash and role in IMPORTANT_ROLES)):
            # Use hard fit on hard tasks even when the rerun budget is small.
            # Bounded paid routes keep the exact hard-role quality check.
            scoped = hard_grade
        elif (stakes == "high" and not have_hard_grade and scoped is not None and scoped.get("unknown")
                and family in strata.get("low", {})):
            # Preserve legacy high-value routing without mislabeling the fit
            # as a hard grade. Irreversible selection still fails closed.
            scoped = {**strata["low"][family], "basis": "local_fit_no_hard_benchmark"}
        q = scoped if scoped is not None else row.get("q", {}).get(family) or {}
        mean, sd = _number(q.get("mean")), _number(q.get("sd"))
        unknown = mean is None or not q.get("n_sources") and not row.get("own", {}).get(family, {}).get("trials")
        unknown = unknown or q.get("unknown", False) or "unknown" in (row.get("flags") or [])
        opus = (prefer_opus and option["pool"] == "claude" and option["funding"] == "included"
                and option["model_identity"] == OPUS_HARD_REASONING)
        # BR-CASH-004: the operator's floor applies to the exact task family and effort.
        if bounded_cash:
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
        latency = _number(row.get("latency_by_stakes", {}).get(
            stakes, row.get("effort_estimate", {}).get("latency_s", row.get("latency_s"))))
        telemetry = row.get("source_telemetry", {})
        source = telemetry.get(option["pool"] + "/" + option["harness"], telemetry.get(option["pool"], {}))
        if _number(source.get("latency_s")) is not None:
            latency = _number(source["latency_s"])
        capacity_key = option["pool"], option["model_key"]
        if capacity_key not in capacities:
            capacities[capacity_key] = (
                _capacity_headroom(roster, runtime, *capacity_key, capacity_reference, fleet, recently_worked)
                if option["funding"] == "included" else None)
        capacity = capacities[capacity_key]
        if capacity_key not in paces:
            paces[capacity_key] = pool_pace(roster, runtime, option['pool'], fleet, model_key=option['model_key'])
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
                           **({"hard_reasoning_opus": True} if opus else {}),
                           q={"mean": mean, "sd": sd, "unknown": unknown},
                           cost=cost, latency_s=latency, pool_price=pool_price, quota_pace=paces[capacity_key],
                           **({"capacity": capacity} if capacity is not None else {}),
                           flags=list(row.get("flags") or []) + cost_flags + (["quality_unknown"] if unknown else []) +
                                 (["quota_projection_unknown"] if pool_price["projection_unknown"] else [])))
    # Low spend remains a last resort for its declared roles, not a score
    # tiebreak or a way to retry past an eligible normal subscription.
    if any(item["funding"] == "included" and item.get("spend_level") != "low" for item in scored):
        for item in list(scored):
            if item.get("spend_level") == "low":
                rejected.append(f"{item['model_key']}/{item['level']}: low spend fallback withheld; "
                                "another subscription is eligible")
                scored.remove(item)
    eligible = list(scored) if collect_eligible else None
    # Missing cells are not a cheaper substitute for measured equal-mean cells.
    # This dominance rule matters even when their price difference exceeds one
    # uncertainty penalty; the raw formula alone cannot enforce that promise.
    measured_means = {item["q"]["mean"] for item in scored if not item["q"]["unknown"]}
    unknown_options = [item for item in scored if item["q"]["unknown"]]
    for item in scored:
        if (item["q"]["unknown"] and item["q"]["mean"] in measured_means and
                not (item.get("pro_fallback") or item.get("hard_reasoning_opus"))):
            rejected.append(f"{item['model_key']}/{item['level']}: unknown cell dominated by measured equal-mean option")
    scored = [item for item in scored if not (item["q"]["unknown"] and item["q"]["mean"] in measured_means
                                             and not (item.get("pro_fallback") or item.get("hard_reasoning_opus")))]
    if not scored:
        raise fleet.FleetError("no eligible selector option: " + "; ".join(rejected))
    exploration_cap = _number(policy.get("exploration_cap", rating_explore.DEFAULT_EXPLORATION_CAP))
    if exploration_cap is None or not 0 <= exploration_cap <= 1:
        raise fleet.FleetError("selector exploration_cap must be in [0, 1]")
    try:
        counts = rating_explore.checked_counts(state_dir)
    except OSError:
        counts = {}
        no_explore = True
    calibration = SuccessEvidence(rows)
    for item in scored:
        item.update(estimated_price=routing_price(item), pricing_basis=rating.LIVE_PRICE_BASIS)
        rate, basis, source = calibration.rate(item["model_identity"], item["level"], family)
        prior = rating.success_prior(rate, difficulty)
        item.update(p_success_prior=prior, prior_basis=basis, prior_source=source)
        key = rating_explore.route_key(item)
        passed, failed = (counts.get((key, verdict), 0) for verdict in ("pass", "fail"))
        alpha, beta = rating_explore.posterior(prior, passed, failed)
        item["posterior"] = (alpha, beta)
        item["checked_passes"], item["checked_failures"] = passed, failed
        item["p_success"] = alpha / (alpha + beta)
    typical = rating.COMPETENT_RERUN_PRICE
    failure_price = rating.STAKES[rating_stakes] * typical
    for item in scored:
        expected = rating.expected_cost(item["estimated_price"], item["p_success"], failure_price)
        item["expected_cost"] = expected
        # Historical receipts sort score descending; expected cost itself sorts ascending.
        item["score"] = (-expected if expected is not None else _unpriced_rank(item))
    if stakes == "irreversible":
        # BR-CASH-004: do not trade known quality for price or a sampled probability.
        for item in scored:
            item["score"] = item["quality"]
        scored.sort(key=lambda item: (-item["quality"], item["q"]["sd"],
                                      item["pool"], item["model_key"], item["level"]))
    else:
        scored.sort(key=lambda item: (item["expected_cost"] is None, -item["score"], -item["p_success"],
                                      item["q"]["unknown"], item["q"]["sd"],
                                      item["cost"]["estimated_usd"]
                                      if ("source_price_1m" in item and item["cost"]["estimated_usd"] is not None)
                                      else math.inf,
                                      item["pool"], item["model_key"], item["level"]))
    hard_reasoning_preference = None
    preferred = []
    if prefer_opus:
        preferred = [o for o in scored if o.get("hard_reasoning_opus")]
        if preferred:
            hard_reasoning_preference = {"route": "opus"}
        else:
            gpt = [o for o in scored if o["pool"] == "codex"]
            preferred = [o for o in gpt if o["model_identity"] == ASTRA_HARD_REASONING] or gpt
            hard_reasoning_preference = {"route": "gpt-fallback" if preferred else "no-gpt-fallback",
                                         "reason": opus_refusal or "Opus not admitted"}
        if preferred:
            ids = {id(o) for o in preferred}
            scored = preferred + [o for o in scored if id(o) not in ids]
    if target is not None and any(option.get("pro_fallback") for option in scored):
        # An exact ChatGPT Pro request follows its established effort fallback.
        scored.sort(key=lambda option: option.get("worker_level") != "xhigh")
    cash_preference = None
    relay = None
    if target is None and not preferred:
        cash_peers, cash_preference = _cash_band(scored, stakes, policy)
        if cash_preference:
            ids = {id(item) for item in cash_peers}
            scored = cash_peers + [item for item in scored if id(item) not in ids]
        elif not any(map(funding.requires_reservation, scored)):
            relay = _relay_fallback(scored, stakes)
            if relay is not None:
                scored = [relay, *(item for item in scored if item is not relay)]
            elif stakes != "irreversible":
                # Without the pressure exception an unpriced relay is a fallback,
                # even if every eligible route lacks a complete price.
                scored.sort(key=lambda item: bool(item.get("chatgpt_pi") and item["estimated_price"] is None))
    mean_pick = choice = scored[0]
    sample_p = sample_cost = None
    if (not no_explore and target is None and stakes != "irreversible" and exploration_cap
            and not cash_preference and relay is None):
        try:
            allowed = rating_explore.budget_allows(rating_explore.read_totals(state_dir), exploration_cap)
        except (OSError, ValueError):
            allowed = False
        if allowed:
            choice, sample_p, sample_cost = rating_explore.sample_choice(
                preferred or scored, failure_price, random.Random(explore_seed))
    exploring = choice is not mean_pick
    sampling = {"cap": exploration_cap, "mean_pick": rating_explore.pick_summary(mean_pick),
                "sampled_pick": (dict(rating_explore.pick_summary(choice), sampled_p_success=sample_p,
                                      sampled_expected_cost=sample_cost) if sample_p is not None else None)}
    for item in scored:
        # One Thompson draw is not an estimate of its selection probability.
        item["selection_probability"] = None if sample_p is not None else float(item is choice)
    receipt = state_dir / "selections" / (str(uuid.uuid4()) + ".json") if persist_receipts else None
    top3 = scored[:3]
    result = {"schema": "fleet-selection/v1", "choice": choice, "top3": top3,
              "selection_probability": choice["selection_probability"], "exploration": exploring,
              "unknown_options": len(unknown_options),
              "graded_options": sum(not item["q"]["unknown"] for item in scored),
              "value_mode": value_mode, "rating_stakes": rating_stakes, "rating_sampling": sampling,
              "typical_price": typical, "typical_price_basis": "competent_rerun_reference",
              "failure_price": failure_price,
              "calibration_difficulty": difficulty, "cash_preference": cash_preference,
              "quota_fallback": "measured-pi-coding-relay" if relay is not None else None,
              "hard_reasoning_preference": hard_reasoning_preference,
              "lambdas": {pool: value["lambda"] for pool, value in prices.items()},
              "pool_prices": prices, "pool_cash": pool_cash, "role": role, "family": family, "stakes": stakes,
              "rejected": rejected, "allow": allow_list, "selection_file": str(receipt) if receipt else None,
              "target": list(target) if target is not None else None,
              "lead": lead, "lead_pressure": pressure}
    if receipt is None:
        if collect_eligible:
            result["eligible_options"] = eligible
        return result
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
    if collect_eligible:
        result["eligible_options"] = eligible
    return result
