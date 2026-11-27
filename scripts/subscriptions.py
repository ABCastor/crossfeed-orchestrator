"""Private subscription estimates for the console and glance, never for routing."""
from __future__ import annotations

import datetime as dt
import fcntl
import http.client
import json
import math
import os
import re
import stat
import tempfile
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

SCHEMA = 1
PERIODS = {"monthly": Decimal(1), "yearly": Decimal(12)}
AGY_POOLS = {"antigravity-gemini", "antigravity-3p", "antigravity-argon"}
FIELDS = ("amount", "currency", "period", "renewal", "operator_share_percent", "other_payer")
MAX_BYTES = 256 * 1024
BILLING = ("amount", "currency", "period")
SHARE = ("operator_share_percent", "other_payer")
ASSUMED_SHARE = "50"  # BR-SUBS-002: an assumption until the operator sets the real split
NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}


def _promo(plan: dict) -> tuple[bool, str | None]:
    """A promo price is named in the plan or its price_source. The end month is known only when the
    overlay says it (promo_until) or says how many months from as_of; otherwise it stays unknown."""
    text = f"{plan.get('name') or ''} {plan.get('price_source') or ''}".casefold()
    until = plan.get("promo_until")
    if isinstance(until, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}(-[0-9]{2})?", until):
        return True, until[:7]
    if "promo" not in text:
        return False, None
    start = re.fullmatch(r"([0-9]{4})-([0-9]{2})", str(plan.get("as_of") or ""))
    months = re.search(r"\b([0-9]{1,2}|" + "|".join(NUMBER_WORDS) + r")\s+months?\b", text)
    if not start or not months:
        return True, None
    count = months.group(1)
    count = int(count) if count.isdigit() else NUMBER_WORDS[count]
    index = int(start.group(1)) * 12 + int(start.group(2)) - 1 + count
    return True, f"{index // 12:04d}-{index % 12 + 1:02d}"


def _price_defaults(plan: dict) -> dict:
    """What the access overlay already says about this plan's price (BR-SUBS-001)."""
    price, currency = plan.get("price"), plan.get("currency")
    out = dict.fromkeys(BILLING)
    if isinstance(price, bool) or not isinstance(price, (int, float, str)) or not isinstance(currency, str):
        return out
    try:
        amount = _decimal(format(Decimal(str(price)), "f"), 999_999_999)
    except (InvalidOperation, ValueError):
        return out
    if amount is None or not re.fullmatch("[A-Za-z]{3}", currency.strip()):
        return out
    if "." in amount:
        amount = format(Decimal(amount).quantize(Decimal("0.01")), "f")
    return {"amount": amount, "currency": currency.strip().upper(), "period": "monthly"}


def _share_defaults(plan: dict) -> tuple[dict, str | None]:
    """A split counts as confirmed only when the plan names both the share and the other payer."""
    other = plan.get("other_payer")
    share = plan.get("operator_share_percent")
    named = isinstance(share, (int, float, str)) and not isinstance(share, bool)
    if not isinstance(other, str) or not other.strip():
        found = re.search(r"\bshared with ([A-Z][\w-]{0,30})", str(plan.get("name") or "") + " " + str(plan.get("price_source") or ""))
        other = found.group(1) if found else None
        named = False
    if not other:
        return dict.fromkeys(SHARE), None
    # BR-SUBS-003: a split the overlay states is the operator's, not a default to change.
    return ({"operator_share_percent": str(share) if named else ASSUMED_SHARE, "other_payer": other.strip()},
            "overlay" if named else "default")


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
            share, share_source = _share_defaults(plan)
            plans[key] = {"id": key, "name": str(name), "pools": members,
                          "defaults": {**_price_defaults(plan), **share},
                          "share_source": share_source,
                          "promo": _promo(plan)[0], "promo_until": _promo(plan)[1],
                          "free": _price_defaults(plan)["amount"] == "0.00", **dict.fromkeys(FIELDS)}
        plans[key]["pools"].append(pool)
    return plans


def free_plans(roster: dict) -> list[dict]:
    """Plans the overlay marks free: shown as Free, never a bill and never a dial."""
    out = []
    for pool, config in sorted((roster.get("quota_pools") or {}).items()):
        plan = (config or {}).get("plan") if isinstance(config, dict) else None
        if isinstance(plan, dict) and plan.get("billing") == "free" and plan.get("price") in (0, "0", 0.0):
            out.append({"id": pool, "name": str(plan.get("name") or pool)})
    return out


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
    """The operator's value wins field by field; a field the operator never set takes the plan's default."""
    rows = []
    for key, plan in plans.items():
        mine, base = _billing(saved.get(key, {})), plan.get("defaults") or {}
        row = {**plan, **mine}
        priced = mine["amount"] is not None
        if not priced:
            row.update({name: base.get(name) for name in BILLING})
        for name in SHARE + ("renewal",):
            if row.get(name) is None and name in base:
                row[name] = base[name]
        row["source"] = "operator" if priced else "default" if row["amount"] is not None else None
        row["share_source"] = ("operator" if mine["operator_share_percent"] is not None else
                               plan.get("share_source") if row["operator_share_percent"] is not None else None)
        row["promo_until"] = plan.get("promo_until")
        rows.append(row)
    return rows


def save(path: Path, roster: dict, key: str, fields: dict) -> None:
    plans = defaults(roster)
    if key not in plans:
        raise ValueError("Unknown subscription.")
    billing = _billing(fields)
    base = plans[key].get("defaults") or {}
    # A value equal to the plan's default stays a default, so a later change in the overlay still shows.
    if billing["amount"] == base.get("amount") and (billing["currency"], billing["period"]) == (base.get("currency"), base.get("period")):
        billing.update({name: None for name in BILLING})
    for name in SHARE:
        if billing[name] == base.get(name):
            billing[name] = None
    # Per-directory write lock serializes updates without opening a second file.
    with_directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(with_directory, fcntl.LOCK_EX)
        saved = _read(path)
        saved[key] = billing
        # Only what the operator set is stored; defaults are read from the overlay each time.
        rows = [{"id": k, "name": plan["name"], "pools": plan["pools"], **_billing(saved.get(k, {}))}
                for k, plan in plans.items()]
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


# BR-SUBS-004: one USD total, at ECB reference rates, fetched at most once a day.
FX_URL = "https://api.frankfurter.dev/v1/latest?base=USD"
FX_CACHE = "fx-rates.json"
FX_TTL_S = 86400
FX_TIMEOUT_S = 4
FX_MAX_BYTES = 64 * 1024


FX_ENV = "FLEET_FX_RATES"  # a fixed {"as_of", "rates"} document: keeps an offline or test host off the network


def _fx_fetch(timeout: float = FX_TIMEOUT_S) -> dict[str, Any] | None:
    """Today's USD reference rates from Frankfurter (ECB), or None. No key, no identifier sent."""
    override = os.environ.get(FX_ENV)
    if override:
        try:
            fixed = json.loads(override)
        except ValueError:
            return None
        return _fx_checked(fixed)
    request = urllib.request.Request(FX_URL, headers={"Accept": "application/json",
                                                      "User-Agent": "crossfeed-orchestrator/1.0"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read(FX_MAX_BYTES))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    if not isinstance(payload, dict) or (payload.get("base") not in (None, "USD")):
        return None
    return _fx_checked({**payload, "as_of": payload.get("as_of") or payload.get("date")})


def _fx_checked(payload: Any) -> dict[str, Any] | None:
    """A rates document checked: {as_of: YYYY-MM-DD, rates: {CODE: units per USD}} or None."""
    if not isinstance(payload, dict):
        return None
    rates = {str(code).upper(): rate for code, rate in (payload.get("rates") or {}).items()
             if isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0}
    as_of = payload.get("as_of")
    if not rates or not isinstance(as_of, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of):
        return None
    return {"as_of": as_of, "base": "USD", "rates": rates}


def _fx_cache(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    rates = (doc or {}).get("rates") if isinstance(doc, dict) else None
    as_of = (doc or {}).get("as_of") if isinstance(doc, dict) else None
    if not isinstance(rates, dict) or not isinstance(as_of, str):
        return None
    kept = {str(code).upper(): rate for code, rate in rates.items()
            if isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0}
    return {"as_of": as_of, "base": "USD", "rates": kept} if kept else None


def _fx_write(path: Path, doc: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fx-rates-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, sort_keys=True)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        pass


def fx_rates(state_dir: Path | None, now: dt.datetime | None = None,
             fetch: Any = None) -> tuple[dict[str, float], str | None]:
    """({currency: units per USD}, as_of) from the saved copy alone, or from an injected fetch.

    A page render never waits on the network: only an explicit fetch (a test's, or
    `refresh_fx` on the refresh path) can reach the reference feed.
    """
    if state_dir is None:
        return {}, None
    path = Path(state_dir).expanduser() / FX_CACHE
    saved = _fx_cache(path)
    today = (now or dt.datetime.now(dt.timezone.utc)).date().isoformat()
    if saved and saved["as_of"] >= today:
        return saved["rates"], saved["as_of"]
    if fetch is not None:
        fresh = fetch()
        if fresh:
            _fx_write(path, fresh)
            return fresh["rates"], fresh["as_of"]
    return (saved["rates"], saved["as_of"]) if saved else ({}, None)


def refresh_fx(state_dir: Path | None, now: dt.datetime | None = None, fetch: Any = None) -> bool:
    """Fetch the day's rates unless the saved copy is today's. One request at most; True when it wrote."""
    if state_dir is None:
        return False
    path = Path(state_dir).expanduser() / FX_CACHE
    today = (now or dt.datetime.now(dt.timezone.utc)).date().isoformat()
    saved = _fx_cache(path)
    if saved and saved["as_of"] >= today:
        return False
    fresh = (fetch or _fx_fetch)()
    if not fresh:
        return False
    _fx_write(path, fresh)
    return True


def _rate(rates: dict[str, float], code: str | None) -> Decimal | None:
    if code == "USD":
        return Decimal(1)
    value = rates.get(code or "")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    return Decimal(str(value))


def _usd_total(entries: list[dict], rates: dict[str, float], as_of: str | None) -> dict[str, Any] | None:
    """One USD total and the operator's share of it; nothing when any priced plan lacks a rate."""
    total = own = Decimal(0)
    seen = False
    unknown_own = False
    for entry in entries:
        if entry.get("monthly") is None:
            continue
        rate = _rate(rates, entry.get("currency"))
        if rate is None:
            return None
        seen = True
        total += Decimal(entry["monthly"]) / rate
        if entry.get("operator_monthly") is None:
            unknown_own = True
        else:
            own += Decimal(entry["operator_monthly"]) / rate
    if not seen:
        return None
    # Each row's own USD figure, so the rows visibly add up to the total.
    for entry in entries:
        if entry.get("operator_monthly") is not None:
            entry["operator_usd"] = _money(Decimal(entry["operator_monthly"]) / _rate(rates, entry.get("currency")))
    return {"total": _money(total), "operator": None if unknown_own else _money(own),
            "rates_as_of": as_of, "approximate": True}


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


def view(path: Path, roster: dict, overview: dict, *, state_dir: Path | None = None,
         now: dt.datetime | None = None, fetch: Any = None) -> dict:
    """The ledger for a page or a menu. Rates come from the saved copy; `fetch` is a test's seam."""
    try:
        entries = _active(defaults(roster), _read(path))
    except (OSError, ValueError, RecursionError):
        return {"subscriptions": [], "monthly": [], "unpriced": 0, "unsplit": 0, "unavailable": True}
    totals: dict[str, list[Decimal | None]] = {}
    values = _values(roster, overview)
    unpriced = unsplit = 0
    controls = {row["id"]: row for row in overview.get("subscriptions", [])}
    for entry in entries:
        control = controls.get(entry["id"], {})
        entry.update({key: control[key] for key in ("level", "set_by", "set_at") if key in control})
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
        # BR-SUBS-005: a plan with no share set belongs to the operator alone.
        if share is None:
            unsplit += 1
            entry["operator_monthly"] = _money(cost)
            if totals[currency][1] is not None:
                totals[currency][1] += cost
        else:
            contribution = cost * Decimal(share) / 100
            entry["operator_monthly"] = _money(contribution)
            if totals[currency][1] is not None:
                totals[currency][1] += contribution
    return {"subscriptions": entries, "free": free_plans(roster), "monthly": [
        {"currency": currency, "total": _money(total), "operator": _money(own) if own is not None else None}
        for currency, (total, own) in sorted(totals.items())
    ], "unpriced": unpriced, "unsplit": unsplit, "unavailable": False, **_usd_view(entries, state_dir, now, fetch)}


def _usd_view(entries: list[dict], state_dir: Path | None, now: dt.datetime | None,
              fetch: Any) -> dict[str, Any]:
    """The one USD total for the ledger, omitted when no rate is known (BR-SUBS-004)."""
    priced = [entry for entry in entries if entry.get("monthly") is not None]
    if not priced:
        return {}
    rates, as_of = fx_rates(state_dir, now=now, fetch=fetch)
    if not rates and not all(entry.get("currency") == "USD" for entry in priced):
        return {}
    block = _usd_total(priced, rates, as_of or (now or dt.datetime.now(dt.timezone.utc)).date().isoformat())
    return {"usd": block} if block else {}


def tray_summary(ledger: dict) -> dict:
    """Totals plus one line per plan, so the menu can say "Claude Max 20x · £183/mo"."""
    plans = [{"id": e["id"], "name": e.get("name") or e["id"], "amount": e.get("amount"),
              "currency": e.get("currency"), "period": e.get("period"), "source": e.get("source"),
              "share": share_fraction(e.get("operator_share_percent")), "operator_amount": e.get("operator_monthly"),
              "operator_usd": e.get("operator_usd")}
             for e in ledger.get("subscriptions", [])]
    out = {**{key: ledger[key] for key in ("monthly", "unpriced", "unsplit", "unavailable")}, "plans": plans}
    if ledger.get("usd"):
        out["usd"] = ledger["usd"]
    return out


def share_fraction(value: Any) -> str | None:
    """The operator's part of a split as a reduced fraction: 50 -> "1/2", 60 -> "3/5", none -> null."""
    if value is None:
        return None
    try:
        part = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not part.is_finite() or not 0 <= part <= 100:
        return None
    hundredths = int((part * 100).to_integral_value(rounding=ROUND_HALF_UP))
    divisor = math.gcd(hundredths, 10_000)
    return f"{hundredths // divisor}/{10_000 // divisor}"
