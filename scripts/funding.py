"""Cash quotes, deliberately separate from subscription pressure and list prices."""
from __future__ import annotations

import datetime as dt
import json
import math

CLASSES = frozenset({"included", "verified-free", "metered", "unknown"})
CASH_FIELDS = ("estimate_usd", "worst_case_usd", "per_call_cap_usd", "cap_usd",
               "settled_usd", "held_usd", "available_usd", "reported_usd")
# These are existing wrapper contracts, not inferences from key type or a free sticker price.
_LEGACY_INCLUDED = frozenset({"claude", "codex", "opencode-go", "antigravity",
                              "antigravity-gemini", "antigravity-3p", "antigravity-argon",
                              "chatgpt-work", "github-copilot-student"})
_NONCASH = frozenset({"included", "verified-free"})


def number(value):
    if type(value) in (int, float):
        try:
            if math.isfinite(value):
                return float(value)
        except OverflowError:
            pass
    return None


def rates(value):
    """Normalize the two supported USD/million field pairs, without guessing an ambiguous quote."""
    if not isinstance(value, dict):
        return None
    pairs = []
    for left, right in (("in", "out"), ("input", "output")):
        if left not in value and right not in value:
            continue
        pair = (number(value.get(left)), number(value.get(right)))
        if any(rate is None or rate < 0 for rate in pair):
            return None
        pairs.append(pair)
    if not pairs or any(pair != pairs[0] for pair in pairs[1:]):
        return None
    return pairs[0]


# BR-CASH-001
def classify(roster, pool, lane=None):
    lane = lane or {}
    config = roster.get("quota_pools", {}).get(pool, {})
    plan = config.get("plan") or {}
    declared = lane.get("funding", config.get("funding"))
    if (pool in {"gemini-metered", "openrouter-mimo"} or lane.get("paid_mimo") is True
            or plan.get("billing") == "per-token" or config.get("funding") == "metered"
            or declared == "metered"):
        return "metered"
    if "funding" in lane or "funding" in config:
        value = declared if isinstance(declared, str) and declared in CLASSES else "unknown"
    elif pool == "openrouter-free" and lane.get("harness", "openrouter") == "openrouter":
        value = "verified-free"
    elif pool in _LEGACY_INCLUDED or (plan.get("billing") == "subscription"
                                      and roster.get("policy", {}).get("paid_overage") == "disabled"):
        value = "included"
    else:
        value = "unknown"
    if value == "included" and roster.get("policy", {}).get("paid_overage", "disabled") != "disabled":
        return "unknown"
    return value


def explicitly_unknown(roster, pool, lane, value):
    return value == "unknown" and ("funding" in lane or "funding" in roster.get("quota_pools", {}).get(pool, {}))


def quote(funding, budget=None, *, estimate=None, worst_case=None):
    cash = {"funding": funding, **dict.fromkeys(CASH_FIELDS)}
    if funding in _NONCASH:
        cash.update(estimate_usd=0, worst_case_usd=0)
    elif funding == "metered":
        cash.update(estimate_usd=estimate, worst_case_usd=worst_case)
    if budget:
        cash.update(budget)
    return cash


def legacy_budget(pool, config, state_dir, metered=None):
    cap = number(config.get("daily_usd_cap"))
    if cap is None or cap < 0:
        return {}
    if pool != "openrouter-mimo":
        return {"budget_basis": "estimate-only", "cap_usd": cap,
                "estimated_spent_usd": (metered or {}).get("estimated_spent_usd_today")}
    result = {"cap_usd": cap}
    try:
        ledger = json.loads((state_dir / "openrouter-mimo-budget.json").read_text())
        if ledger.get("schema") != "openrouter-mimo-budget/v1":
            return result
        today = dt.datetime.now().astimezone().date().isoformat()
        settled, held = 0.0, 0.0
        frozen = False
        for call in ledger["calls"].values():
            value = float(call["reserved_usd"] if call["cost_usd"] is None else call["cost_usd"])
            if not math.isfinite(value) or value < 0:
                return result
            if call["cost_usd"] is None:
                held += value
            elif call["day"] == today:
                settled += value
            frozen |= bool(call.get("blocked"))
        return dict(result, settled_usd=settled, held_usd=held,
                    available_usd=0 if frozen else max(0, cap - settled - held))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return result


def summary(cash, *, symbol="$"):
    def usd(value):
        return "unknown" if value is None else f"{symbol}{value:.8g}"
    text = (f"{cash['funding']}, estimated {usd(cash.get('estimate_usd'))}, "
            f"worst case {usd(cash.get('worst_case_usd'))}")
    if cash["funding"] == "metered":
        if cash.get("budget_basis") == "estimate-only":
            text += f", estimate-only usage {usd(cash.get('estimated_spent_usd'))}"
        if cash.get("per_call_cap_usd") is not None:
            text += f", per-call cap {usd(cash['per_call_cap_usd'])}"
        text += ", " + ", ".join(f"{key} {usd(cash.get(key + '_usd'))}"
                                  for key in ("cap", "settled", "held", "available"))
    return text


def receipt_fields(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("funding"), str) or raw["funding"] not in CLASSES:
        return None
    result = {"funding": raw["funding"]}
    for key in (*CASH_FIELDS, "estimated_spent_usd"):
        value = number(raw.get(key))
        if key in raw:
            result[key] = value if value is not None and value >= 0 else None
    if raw.get("budget_basis") == "estimate-only":
        result["budget_basis"] = "estimate-only"
    return result
