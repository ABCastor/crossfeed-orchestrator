"""One local cash ledger; OpenRouter-compatible catalog + prompt-only SSE contract."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import urllib.parse
import uuid

MODELS = frozenset({"xiaomi/mimo-v2.6-pro", "xiaomi/mimo-v2.6-flash"})
POOL = "openrouter-mimo"
SCHEMA = "crossfeed-cash-budget/v1"
PROFILE = "catalog-sse-v1"
ID = re.compile(r"[a-z][a-z0-9._-]{0,63}")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,159}")
AMOUNT = re.compile(r"(?:\d{1,12}(?:\.\d{1,15})?|\.\d{1,15})(?:[eE][+-]?\d{1,2})?")


class BudgetError(ValueError):
    pass


def dollars(value):
    try:
        raw = str(value)
        if not AMOUNT.fullmatch(raw):
            raise InvalidOperation
        amount = Decimal(raw)
        if not amount.is_finite() or amount < 0 or amount > 10**9 or amount.as_tuple().exponent < -15:
            raise InvalidOperation
        return amount
    except InvalidOperation:
        raise BudgetError("missing or invalid price/cost") from None


def positive_integer(value):
    if type(value) is not int or not 0 < value <= 10_000_000:
        raise BudgetError("missing or invalid token/byte bound")
    return value


def _instant(value):
    try:
        instant = dt.datetime.fromisoformat(value)
        if instant.tzinfo is not None:
            return instant
    except (TypeError, ValueError):
        pass
    raise BudgetError("missing or invalid expiry")


def _live_until(value, label):
    if _instant(value) <= dt.datetime.now(dt.timezone.utc):
        raise BudgetError(f"{label} expired")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise BudgetError("duplicate key in cash configuration or ledger")
        result[key] = value
    return result


def _owner_json(path, label):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise BudgetError(f"{label} must be an owner-only regular file")
            value = json.load(handle, object_pairs_hook=_pairs)
        if not isinstance(value, dict):
            raise BudgetError(f"invalid {label}")
        return value
    except (OSError, ValueError) as exc:
        if isinstance(exc, BudgetError):
            raise
        raise BudgetError(f"missing or unreadable {label}") from None


def _owner_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise BudgetError("cash directory must be owned by the operator and not writable by others")


def ledger_path():
    # Never partition cash by --state-dir, FLEET_STATE_DIR or XDG_STATE_HOME.
    return Path.home() / ".local/state/orchestrator/cash-budget.json"


def owner_overlay(path):
    expected = Path.home() / ".config/orchestrator/access-overlay.json"
    if Path(path).expanduser().resolve() != expected.resolve():
        raise BudgetError("paid transport requires the canonical owner access overlay")
    _owner_directory(expected.parent)
    return _owner_json(expected, "owner access overlay")


# BR-CASH-002 / BR-MIMO-002
def envelope(roster, account):
    accounts = roster.get("cash_accounts")
    raw = accounts.get(account) if isinstance(accounts, dict) and isinstance(account, str) and ID.fullmatch(account) else None
    if not isinstance(raw, dict):
        raise BudgetError("no owner cash envelope for the funding account")
    _live_until(raw.get("expires_at"), "cash envelope")
    values = tuple(dollars(raw.get(key)) for key in ("per_call_usd", "daily_usd", "pilot_usd"))
    if not 0 < values[0] <= values[1] <= values[2]:
        raise BudgetError("cash envelope needs positive ordered per-call, daily and pilot limits")
    if "automatic" in raw and type(raw["automatic"]) is not bool:
        raise BudgetError("invalid automatic grant in cash envelope")
    return raw, values


def endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        if (not isinstance(value, str) or len(value) > 300 or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not parsed.path
                or value.endswith("/") or any(c.isspace() or c == "\\" for c in value)
                or not (parsed.scheme == "https"
                        or parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and port)):
            raise ValueError
        return value
    except (TypeError, ValueError, AttributeError):
        raise BudgetError("invalid pinned API endpoint") from None


# BR-CASH-002
def route_config(roster, lane, role):
    route = lane.get("paid_route")
    if lane.get("harness") != "openrouter" or not isinstance(route, dict) or route.get("profile") != PROFILE:
        raise BudgetError("metered lane has no supported bounded prompt-only transport")
    model, provider = route.get("model"), route.get("provider")
    if (not isinstance(model, str) or not TAG.fullmatch(model) or model != lane.get("selector")
            or not isinstance(provider, str) or not TAG.fullmatch(provider)
            or model in {"auto", "openrouter/free"}):
        raise BudgetError("paid route needs an exact model and provider pin")
    if lane.get("paid_mimo") is True and (model not in MODELS or lane.get("quota_pool") != POOL):
        raise BudgetError("paid MiMo only permits MiMo V2.6 Pro and Flash in its own pool")
    roles = route.get("roles")
    if not isinstance(roles, list) or role not in roles or role not in lane.get("roles", []):
        raise BudgetError("role is not approved for this prompt-contained paid route")
    raw_auth = lane.get("auth")
    ref = raw_auth.get("key_ref") if isinstance(raw_auth, dict) else None
    if not isinstance(ref, str) or not ref.startswith("op://"):
        raise BudgetError("paid route requires a 1Password key reference")
    tariff = route.get("tariff")
    if not isinstance(tariff, dict) or set(tariff) != {
            "input", "output", "context_tokens", "max_output_tokens", "max_input_bytes", "source", "valid_until"}:
        raise BudgetError("missing or invalid tariff")
    try:
        _live_until(tariff.get("valid_until"), "paid tariff")
        if not isinstance(tariff["source"], str) or not tariff["source"].strip() or len(tariff["source"]) > 240:
            raise BudgetError("tariff source required")
        rates = tuple(dollars(tariff[k]) for k in ("input", "output"))
        # The provider maxima use the same public USD/million representation as the quote.
        if any(dollars(float(rate)) != rate for rate in rates):
            raise BudgetError("tariff precision cannot be represented by the transport")
        bounds = tuple(positive_integer(tariff[k]) for k in ("context_tokens", "max_output_tokens", "max_input_bytes"))
    except BudgetError as exc:
        raise BudgetError(f"invalid tariff: {exc}") from None
    endpoint(route.get("endpoint"))
    raw, _ = envelope(roster, route.get("account"))
    return route, rates, bounds, Budget(route["account"], raw)


def ceiling_usd(rates, context, output):
    return (rates[0] * context + rates[1] * output) / 1_000_000


# BR-MIMO-001 / BR-CASH-002
def priced_request(entry, body, max_tokens="", *, route=None, rates=None, bounds=None):
    """Use the entire pinned context, never a tokenizer estimate."""
    model = route["model"] if route else body.get("model")
    if (route is None and model not in MODELS) or entry.get("id") != model or body.get("model") != model:
        raise BudgetError("model not approved")
    if (entry.get("architecture") or {}).get("output_modalities") != ["text"]:
        raise BudgetError("text output required")
    prices = entry.get("pricing")
    if not isinstance(prices, dict):
        raise BudgetError("missing catalog tariff")
    live = tuple(dollars(prices.get(field)) for field in ("prompt", "completion"))
    for field, value in prices.items():
        if field in {"prompt", "completion"}:
            continue
        if field == "input_cache_read" and dollars(value) <= live[0]:
            continue
        if dollars(value) != 0:
            raise BudgetError("unsupported billing field")
    context = positive_integer(entry.get("context_length"))
    output = positive_integer((entry.get("top_provider") or {}).get("max_completion_tokens"))
    if route:
        if context > bounds[0] or any(price * 1_000_000 > rate for price, rate in zip(live, rates)):
            raise BudgetError("live price/context exceeds owner tariff")
        context = bounds[0]
        output = min(output, bounds[1])
    else:
        rates = tuple(price * 1_000_000 for price in live)
    if max_tokens:
        output = min(output, positive_integer(int(max_tokens)))
    provider = {"require_parameters": True, "allow_fallbacks": False,
                "max_price": dict(zip(("prompt", "completion"), map(float, rates)))}
    if route:
        provider["only"] = [route["provider"]]
    request = dict(body, max_tokens=output, provider=provider)
    # Reserve against what the endpoint is allowed to bill, not the catalog's cheapest host.
    return request, ceiling_usd(rates, context, output)


class Budget:
    # BR-CASH-002 / BR-MIMO-002
    def __init__(self, account, settings, *, path=None):
        self.account = account
        self.settings, (self.per_call, self.daily, self.pilot) = envelope({"cash_accounts": {account: settings}}, account)
        self.path = Path(path) if path is not None else ledger_path()

    @contextmanager
    def _locked(self, *, write=False):
        if not self.path.exists():
            raise BudgetError("missing cash ledger; owner initialization required")
        directory = self.path.parent
        _owner_directory(directory)
        lock_path = self.path.with_suffix(".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "rb") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BudgetError("invalid cash ledger lock")
            fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            ledger = _owner_json(self.path, "cash ledger")
            accounts = ledger.get("accounts")
            if ledger.get("schema") != SCHEMA or not isinstance(accounts, dict) or self.account not in accounts:
                raise BudgetError("invalid cash ledger or funding account not initialized")
            for account, current in accounts.items():
                if not ID.fullmatch(account) or not isinstance(current, dict) or not isinstance(current.get("calls"), dict):
                    raise BudgetError("corrupt cash ledger account")
                for token, call in current["calls"].items():
                    self._validate(token, call)
            calls = accounts[self.account]["calls"]
            yield calls
            if write:
                fd, filename = tempfile.mkstemp(prefix=".cash-budget-", dir=directory)
                try:
                    with os.fdopen(fd, "w") as handle:
                        json.dump(ledger, handle, sort_keys=True, allow_nan=False)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(filename, self.path)
                    dir_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                finally:
                    if os.path.exists(filename):
                        os.unlink(filename)

    @staticmethod
    def _validate(token, call):
        try:
            if (not isinstance(call, dict) or str(uuid.UUID(token)) != token
                    or not isinstance(call.get("model"), str) or not TAG.fullmatch(call["model"])
                    or dt.date.fromisoformat(call["day"]).isoformat() != call["day"]
                    or type(call.get("blocked", False)) is not bool):
                raise ValueError
            reserved = dollars(call["reserved_usd"])
            if call["cost_usd"] is not None and dollars(call["cost_usd"]) > reserved:
                raise ValueError
            if call.get("blocked") and (call["cost_usd"] is not None or dollars(call.get("overrun_usd")) <= reserved):
                raise ValueError
            if not call.get("blocked") and "overrun_usd" in call:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise BudgetError("corrupt cash ledger call") from None

    @staticmethod
    def _today():
        return dt.datetime.now().astimezone().date().isoformat()

    def _totals(self, calls, today):
        settled = held = lifetime = Decimal(0)
        frozen = False
        for call in calls.values():
            if call["cost_usd"] is None:
                held += dollars(call["reserved_usd"])
                frozen |= call.get("blocked", False)
            else:
                amount = dollars(call["cost_usd"])
                lifetime += amount
                if call["day"] == today:
                    settled += amount
        available = 0 if frozen else max(0, min(self.daily - settled - held, self.pilot - lifetime - held))
        return settled, held, available, frozen

    def snapshot(self, *, require=None):
        with self._locked() as calls:
            day = self._today()
            if require is not None:
                self._admit(calls, day, dollars(require))
            settled, held, available, frozen = self._totals(calls, day)
        return {"account": self.account, "per_call_cap_usd": float(self.per_call), "cap_usd": float(self.daily),
                "settled_usd": float(settled), "held_usd": float(held), "available_usd": float(available),
                "frozen": frozen}

    def _admit(self, calls, day, amount):
        _live_until(self.settings.get("expires_at"), "cash envelope")
        if amount > self.per_call:
            raise BudgetError("per-call cap cannot cover this call")
        _, _, available, frozen = self._totals(calls, day)
        if frozen:
            raise BudgetError("billing exceeded its reservation; owner reconciliation required")
        if amount > available:
            raise BudgetError("daily or pilot cap cannot cover this call")

    # BR-CASH-002 / BR-MIMO-002
    def reserve(self, model, ceiling):
        if not isinstance(model, str) or not TAG.fullmatch(model):
            raise BudgetError("invalid model")
        amount = dollars(ceiling)
        with self._locked(write=True) as calls:
            day = self._today()
            self._admit(calls, day, amount)
            token = str(uuid.uuid4())
            calls[token] = {"day": day, "model": model, "reserved_usd": str(amount), "cost_usd": None}
        return token

    # BR-CASH-002 / BR-MIMO-002
    def settle(self, token, cost):
        amount = dollars(cost)
        overrun = False
        with self._locked(write=True) as calls:
            call = calls.get(token)
            if call is None or call["cost_usd"] is not None or call.get("blocked"):
                raise BudgetError("reservation not outstanding")
            if amount > dollars(call["reserved_usd"]):
                call.update(blocked=True, overrun_usd=str(amount))
                overrun = True
            else:
                call.update(cost_usd=str(amount), day=self._today())
        if overrun:
            raise BudgetError("billing exceeded reserved ceiling")
        return amount
