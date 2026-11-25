"""Private subscription estimates for the console and glance, never for routing."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import stat
import tempfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

SCHEMA = 1
PERIODS = {"monthly": Decimal(1), "yearly": Decimal(12)}
AGY_POOLS = {"antigravity-gemini", "antigravity-3p", "antigravity-argon"}
FIELDS = ("amount", "currency", "period", "renewal", "operator_share_percent", "other_payer")
MAX_BYTES = 256 * 1024


def path_for(overlay: Path) -> Path:
    return overlay.expanduser().with_name("subscriptions.json")


def defaults(roster: dict) -> dict[str, dict]:
    """Plan names come from the roster; quota-only siblings do not duplicate bills."""
    plans = {}
    for pool, config in sorted((roster.get("quota_pools") or {}).items()):
        if not isinstance(config, dict):
            continue
        plan = config.get("plan") or {}
        if not isinstance(plan, dict) or (plan.get("billing") or config.get("billing")) != "subscription":
            continue
        if pool == "antigravity":  # historical combined pool, not a separate bill
            continue
        key = "antigravity" if pool in AGY_POOLS else pool
        name = plan.get("name") or config.get("label") or pool
        if key not in plans or pool == "antigravity-gemini":
            members = plans.get(key, {}).get("pools", [])
            plans[key] = {"id": key, "name": str(name), "pools": members,
                          "source": "Access overlay (plan); operator (billing)", **dict.fromkeys(FIELDS)}
        plans[key]["pools"].append(pool)
    return plans


def _decimal(text: str | None, maximum: int) -> str | None:
    if text in (None, ""):
        return None
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]{1,9}(\.[0-9]{1,2})?", text):
        raise ValueError("Use a number with at most two decimal places.")
    if Decimal(text) > maximum:
        raise ValueError("Number is out of range.")
    return format(Decimal(text), "f")


def _billing(fields: dict) -> dict:
    if any(fields.get(k) is not None and not isinstance(fields[k], str) for k in FIELDS):
        raise ValueError("Invalid billing settings.")
    amount = _decimal(fields.get("amount"), 999_999_999)
    share = _decimal(fields.get("operator_share_percent"), 100)
    currency, period = (fields.get("currency") or "").strip().upper() or None, fields.get("period") or None
    renewal = fields.get("renewal") or None
    other = (fields.get("other_payer") or "").strip() or None
    if currency and not re.fullmatch("[A-Z]{3}", currency):
        raise ValueError("Enter a three-letter currency, such as EUR.")
    if period and period not in PERIODS:
        raise ValueError("Choose monthly or yearly billing.")
    if amount is not None and (currency is None or period is None):
        raise ValueError("Choose a currency and billing period for the price.")
    if renewal:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", renewal):
            raise ValueError("Use YYYY-MM-DD for renewal.")
        dt.date.fromisoformat(renewal)
    if other and (len(other) > 60 or not other.isprintable()):
        raise ValueError("Use a short label for the other payer.")
    return dict(amount=amount, currency=currency, period=period, renewal=renewal,
                operator_share_percent=share, other_payer=other)


def _read(path: Path) -> dict[str, dict]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as handle:
        status = os.fstat(handle.fileno())
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or stat.S_IMODE(status.st_mode) != 0o600):
            raise ValueError("Subscriptions must be a private, operator-owned file (mode 600).")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Subscriptions file is too large.")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("subscriptions"), list):
        raise ValueError("Unknown subscriptions file format.")
    entries = {}
    for item in doc["subscriptions"]:
        if not isinstance(item, dict) or not isinstance(key := item.get("id"), str) or key in entries:
            raise ValueError("Invalid or duplicated subscription.")
        entries[key] = {**item, **_billing(item)}
    return entries


def _active(plans: dict[str, dict], saved: dict[str, dict]) -> list[dict]:
    return [{**plan, **_billing(saved.get(key, {}))} for key, plan in plans.items()]


def save(path: Path, roster: dict, key: str, fields: dict) -> None:
    plans = defaults(roster)
    if key not in plans:
        raise ValueError("Unknown subscription.")
    billing = _billing(fields)
    # Per-directory write lock serializes updates without opening a second file.
    with_directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(with_directory, fcntl.LOCK_EX)
        saved = _read(path)
        saved[key] = billing
        rows = _active(plans, saved)
        # Keep entries for plans removed from the current roster, but do not count them.
        rows.extend(item for item_key, item in saved.items() if item_key not in plans)
        raw = (json.dumps({"schema": SCHEMA, "subscriptions": rows}, indent=2, ensure_ascii=False) + "\n").encode()
        if len(raw) > MAX_BYTES:
            raise ValueError("Subscriptions file is too large.")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".subscriptions-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    finally:
        os.close(with_directory)


def _money(n: Decimal) -> str:
    return str(n.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _values(roster: dict, overview: dict) -> dict[str, dict]:
    configs = roster.get("quota_pools") or {}
    result = {}
    for pool in overview.get("pools", []):
        if pool.get("gauge_error") or (pool.get("quota") or {}).get("confidence") != "direct":
            continue
        reference = (configs.get(pool["pool"], {}).get("value_capacity") or {})
        if not isinstance(reference, dict) or not reference.get("source"):
            continue
        try:
            capacity = Decimal(str(reference.get("weekly_usd_equivalent")))
            if not capacity.is_finite() or not 0 < capacity < 1_000_000_000:
                continue
        except InvalidOperation:
            continue
        weekly = [r for r in pool.get("limits", []) if r.get("kind") == "window" and r.get("minutes") == 10080
                  and not r.get("scope") and r.get("reset_at") and r.get("state") != "idle"
                  and type(r.get("used_percent")) is int and 0 <= r["used_percent"] <= 100]
        if len(weekly) == 1:
            result[pool["pool"]] = {"used_usd_equivalent": _money(capacity * weekly[0]["used_percent"] / 100),
                                    "capacity_usd_equivalent": _money(capacity),
                                    "source": str(reference["source"])}
    return result


def view(path: Path, roster: dict, overview: dict) -> dict:
    try:
        entries = _active(defaults(roster), _read(path))
    except (OSError, ValueError, RecursionError):
        return {"subscriptions": [], "monthly": [], "unpriced": 0, "unsplit": 0, "unavailable": True}
    totals: dict[str, list[Decimal | None]] = {}
    values = _values(roster, overview)
    unpriced = unsplit = 0
    for entry in entries:
        entry.update(monthly=None, operator_monthly=None, other_share_percent=None,
                     pool_value={k: values[k] for k in entry["pools"] if k in values})
        share = entry["operator_share_percent"]
        if share is not None:
            entry["other_share_percent"] = str(100 - Decimal(share))
        if entry["amount"] is None:
            unpriced += 1
            continue
        currency = entry["currency"]
        cost = Decimal(entry["amount"]) / PERIODS[entry["period"]]
        entry["monthly"] = _money(cost)
        totals.setdefault(currency, [Decimal(0), Decimal(0)])[0] += cost
        if share is None:
            unsplit += 1
            totals[currency][1] = None
        else:
            contribution = cost * Decimal(share) / 100
            entry["operator_monthly"] = _money(contribution)
            if totals[currency][1] is not None:
                totals[currency][1] += contribution
    return {"subscriptions": entries, "monthly": [
        {"currency": currency, "total": _money(total), "operator": _money(own) if own is not None else None}
        for currency, (total, own) in sorted(totals.items())
    ], "unpriced": unpriced, "unsplit": unsplit, "unavailable": False}


def tray_summary(ledger: dict) -> dict:
    return {key: ledger[key] for key in ("monthly", "unpriced", "unsplit", "unavailable")}
