"""Per-person summaries of shared subscriptions: what each person owes, and what use was measured.

The honest limit, stated once and kept in the output: Crossfeed measures only what ran through it on
this machine. The provider reports the account's total; the difference is "not measured here". It is
other people's use, and also the operator's own use on the web, a phone or another computer. A shared
login carries no name, so use is never attributed to a person by guesswork. What a person owes comes
from the agreed share in the private ledger, not from use.

Pure functions over the ledger view and the overview; nothing here reads a file, a secret or the network.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}
WEEK_MINUTES = 10080
OPERATOR = "me"
LIMITS = ("Estimates at API list prices from usage logs on one Mac, not what anyone pays. "
          "A shared login does not say who used it, so use is never split by person.")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "other"


def money(currency: str | None, amount: Any) -> str:
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    symbol = SYMBOLS.get(currency or "")
    return f"{symbol}{value:,}" if symbol else f"{currency or ''} {value:,}".strip()


def usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}" if value >= 0.01 or value == 0 else f"${value:.4f}"


def _epoch(value: Any) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _day(epoch: float) -> str:
    when = dt.datetime.fromtimestamp(epoch).astimezone()
    return f"{when.day} {when:%b}"


def _use(entry: dict[str, Any], pools: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The account's reading and what this machine measured for one subscription, with the reason for any gap."""
    members = [pools[key] for key in entry.get("pools", []) if key in pools]
    weekly_all = [(pool["pool"], limit) for pool in members for limit in pool.get("limits", [])
                  if limit.get("kind") == "window" and limit.get("minutes") == WEEK_MINUTES and not limit.get("scope")
                  and limit.get("reset_at") and isinstance(limit.get("used_percent"), (int, float))]
    # A plan with several weekly allowances (Google AI Pro) has no one account figure to compare with.
    weekly = weekly_all[0][1] if len({pool for pool, _ in weekly_all}) == 1 else None
    cost = next((pool["usage_cost"] for pool in members if pool.get("usage_cost")), None)
    out: dict[str, Any] = {"percent": None, "total_usd": None, "measured_usd": None, "measured_tokens": None,
                           "rest_usd": None, "measured_share": None, "basis": None, "exact": True, "stale": False,
                           "overshoot": False, "reason": None}
    reset = _epoch(weekly["reset_at"]) if weekly else None
    if weekly and reset:
        out["percent"] = weekly["used_percent"]
    value = next(iter((entry.get("pool_value") or {}).values()), None)
    if value:
        out["total_usd"] = float(value["used_usd_equivalent"])
    if not cost:
        out["reason"] = "This provider has no usage log on this Mac, so nothing is measured here."
        return out
    out["stale"] = bool(cost.get("stale"))
    hourly = cost.get("hourly")
    if reset and isinstance(hourly, list):
        start = reset - WEEK_MINUTES * 60
        rows = [row for row in hourly if isinstance(row, list) and len(row) == 3 and row[0] * 3600 >= start]
        out["measured_usd"] = sum(row[2] for row in rows)
        out["measured_tokens"] = sum(row[1] for row in rows)
        out["basis"] = f"since the weekly reset on {_day(start)}"
    elif cost.get("last_7d_usd") is not None or cost.get("last_7d_tokens") is not None:
        out["measured_usd"] = cost.get("last_7d_usd")
        out["measured_tokens"] = cost.get("last_7d_tokens")
        out["basis"] = "in the last 7 days"
    else:
        out["reason"] = "Usage cost is not available yet."
        return out
    # An unpriced model in the window means the dollar figure is a floor, never a false zero.
    out["exact"] = cost.get("last_7d_usd") is not None
    total, measured = out["total_usd"], out["measured_usd"]
    if total is not None and measured is not None:
        out["rest_usd"] = max(0.0, total - measured)
        out["overshoot"] = measured > total
        out["measured_share"] = None if total <= 0 else min(100.0, 100.0 * measured / total)
    return out


def build(ledger: dict[str, Any], overview: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """{plans, people, unavailable}. Unpriced plans are left out; an unsplit plan belongs to the operator alone."""
    import time
    now = time.time() if now is None else now
    if ledger.get("unavailable"):
        return {"plans": [], "people": [], "unavailable": True, "as_of": now}
    pools = {pool["pool"]: pool for pool in overview.get("pools", [])}
    plans: list[dict[str, Any]] = []
    people: dict[str, dict[str, Any]] = {OPERATOR: {"key": OPERATOR, "name": "Me", "self": True, "plans": [], "owed": {}}}
    owed: dict[str, dict[str, Decimal]] = {}
    for entry in ledger.get("subscriptions", []):
        if entry.get("amount") is None or entry.get("monthly") is None:
            continue
        monthly = Decimal(entry["monthly"])
        mine = None if entry.get("operator_share_percent") is None else Decimal(entry["operator_share_percent"])
        plan = {"id": entry["id"], "name": entry.get("name") or entry["id"], "currency": entry["currency"],
                "monthly": str(monthly), "operator_percent": None if mine is None else str(mine),
                "other_name": entry.get("other_payer") or "Other payer", "use": _use(entry, pools)}
        plans.append(plan)
        shares = [(OPERATOR, "Me", mine)]
        if mine is not None and mine < 100:
            shares.append((slug(plan["other_name"]), plan["other_name"], Decimal(100) - mine))
        for key, name, percent in shares:
            person = people.setdefault(key, {"key": key, "name": name, "self": False, "plans": [], "owed": {}})
            share = None if percent is None else (monthly * percent / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
            person["plans"].append({"plan": plan["id"], "percent": None if percent is None else str(percent),
                                    "monthly": None if share is None else str(share)})
            if share is not None:
                totals = owed.setdefault(key, {})
                totals[plan["currency"]] = totals.get(plan["currency"], Decimal(0)) + share
    ordered = [people.pop(OPERATOR), *sorted(people.values(), key=lambda p: p["name"].casefold())]
    for person in ordered:
        person["owed"] = {code: str(total) for code, total in sorted(owed.get(person["key"], {}).items())}
    return {"plans": plans, "people": [p for p in ordered if p["plans"]], "unavailable": False, "as_of": now}


def _plan_lines(plan: dict[str, Any], share: dict[str, Any], me: bool) -> list[tuple[str, str]]:
    cur, use = plan["currency"], plan["use"]
    here = "this Mac" if me else "my Mac"
    lines = [("plan", plan["name"])]
    if share["percent"] is None:
        lines.append(("row", f"Share not set: {money(cur, plan['monthly'])} a month in all"))
    else:
        percent = Decimal(share["percent"]).normalize()
        lines.append(("row", f"{'My' if me else 'Your'} share {percent:f}% of {money(cur, plan['monthly'])} a month: "
                             f"{money(cur, share['monthly'])}"))
    if use["percent"] is not None:
        part = f"The account used {use['percent']:g}% of its weekly allowance"
        if use["total_usd"] is not None:
            part += f", about {usd(use['total_usd'])} at API list prices"
        lines.append(("row", part))
    if use["measured_usd"] is not None:
        floor = "" if use["exact"] else "at least "
        text = f"Measured on {here} {use['basis']}: {floor}{usd(use['measured_usd'])}"
        if use["measured_share"] is not None:
            text += f" ({use['measured_share']:.0f}% of the account's use)"
        lines.append(("row", text))
        if use["overshoot"]:
            lines.append(("row", "Not measured here: nothing; the dollar value of the allowance is only approximate"))
        elif use["rest_usd"] is not None:
            bound = "about" if use["exact"] else "at most"
            who = ("other people, or my other devices and the web" if me
                   else "yours, anyone else's, or mine on other devices and the web")
            lines.append(("row", f"Not measured on {here}: {bound} {usd(use['rest_usd'])} ({who})"))
    elif use["reason"]:
        lines.append(("row", use["reason"]))
    if use["stale"]:
        lines.append(("row", "The cost reading is not recent"))
    return lines


def card_lines(model: dict[str, Any], key: str) -> list[tuple[str, str]]:
    """The card as (kind, text) pairs; the plain text, the page and the PNG all render this one list."""
    person = next((p for p in model["people"] if p["key"] == key), None)
    if person is None:
        return []
    plans = {plan["id"]: plan for plan in model["plans"]}
    me = person["self"]
    lines = [("title", "My subscriptions" if me else "Shared subscriptions"),
             ("sub", f"{'My share' if me else person['name'] + chr(39) + 's share'} · as of {_day(model['as_of'])}")]
    for item in person["plans"]:
        lines += _plan_lines(plans[item["plan"]], item, me)
    owed = ", ".join(money(code, total) for code, total in person["owed"].items())
    lines.append(("total", f"{'I owe' if me else 'You owe'} {owed} a month" if owed else "No share set"))
    lines.append(("note", LIMITS))
    return lines


def card_text(model: dict[str, Any], key: str) -> str:
    """Plain text for a message: a blank line before each plan, no markup."""
    out: list[str] = []
    for kind, text in card_lines(model, key):
        if kind in ("plan", "total", "note") and out:
            out.append("")
        out.append(f"  {text}" if kind == "row" else text)
    return "\n".join(out)
