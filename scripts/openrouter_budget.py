"""MiMo-only admission: live price ceiling and a locked, prompt-free daily ledger."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid

MODELS = frozenset({"xiaomi/mimo-v2.6-pro", "xiaomi/mimo-v2.6-flash"})
POOL = "openrouter-mimo"
DEFAULT_CAP = "5"


class BudgetError(ValueError):
    pass


def dollars(value):
    if value is None or isinstance(value, bool):
        raise BudgetError("missing price/cost")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise BudgetError("invalid price/cost") from None
    if not amount.is_finite() or amount < 0:
        raise BudgetError("invalid price/cost")
    return amount


def positive_integer(value):
    if type(value) is not int or value <= 0:
        raise BudgetError("missing token bound")
    return value


# BR-MIMO-001 / BR-MIMO-002
def priced_request(entry, body, max_tokens=""):
    """Reserve the full context, never a tokenizer estimate; bound provider prices too."""
    if entry.get("id") not in MODELS or body.get("model") != entry["id"]:
        raise BudgetError("model not approved")
    if entry.get("architecture", {}).get("output_modalities") != ["text"]:
        raise BudgetError("text output required")
    prices = entry.get("pricing") or {}
    prompt, completion = dollars(prices.get("prompt")), dollars(prices.get("completion"))
    for field, value in prices.items():
        if field in {"prompt", "completion"}:
            continue
        if field == "input_cache_read" and dollars(value) <= prompt:
            continue
        # Tier overrides and new billable features need explicit support first.
        if dollars(value) != 0:
            raise BudgetError("unsupported billing field")
    context = positive_integer(entry.get("context_length"))
    output = positive_integer((entry.get("top_provider") or {}).get("max_completion_tokens"))
    if max_tokens:
        output = min(output, positive_integer(int(max_tokens)))
    request = dict(body, max_tokens=output, provider={
        "require_parameters": True,
        "allow_fallbacks": False,
        "max_price": {"prompt": float(prompt * 1000000), "completion": float(completion * 1000000)},
    })
    return request, prompt * context + completion * output


class Budget:
    # BR-MIMO-002
    def __init__(self, state_dir, cap=DEFAULT_CAP):
        self.directory = Path(state_dir)
        self.path = self.directory / "openrouter-mimo-budget.json"
        self.cap = dollars(cap)
        if self.cap <= 0:
            raise BudgetError("positive cap required")

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "openrouter-mimo-budget.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                try:
                    ledger = json.loads(self.path.read_text())
                except (ValueError, OSError):
                    raise BudgetError("unreadable budget ledger") from None
            else:
                ledger = {"schema": "openrouter-mimo-budget/v1", "calls": {}}
            if ledger.get("schema") != "openrouter-mimo-budget/v1" or not isinstance(ledger.get("calls"), dict):
                raise BudgetError("invalid budget ledger")
            yield ledger
            fd, filename = tempfile.mkstemp(prefix=".mimo-budget-", dir=self.directory)
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(ledger, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(filename, self.path)
            finally:
                if os.path.exists(filename):
                    os.unlink(filename)

    # BR-MIMO-002
    def reserve(self, model, ceiling):
        if model not in MODELS:
            raise BudgetError("model not approved")
        amount = dollars(ceiling)
        day = dt.datetime.now().astimezone().date().isoformat()
        with self.locked() as ledger:
            spent = Decimal(0)
            for call in ledger["calls"].values():
                if call.get("blocked"):
                    raise BudgetError("billing exceeded its reservation; owner reconciliation required")
                # A killed/unknown/spanning-midnight call keeps its reservation, even after reset.
                if call.get("cost_usd") is None:
                    spent += dollars(call["reserved_usd"])
                elif call["day"] == day:
                    spent += dollars(call["cost_usd"])
            if spent + amount > self.cap:
                raise BudgetError("daily cap cannot cover this call")
            token = str(uuid.uuid4())
            ledger["calls"][token] = {"day": day, "model": model, "reserved_usd": str(amount), "cost_usd": None}
        return token

    # BR-MIMO-002
    def settle(self, token, cost):
        amount = dollars(cost)
        overrun = False
        with self.locked() as ledger:
            call = ledger["calls"][token]
            if call["cost_usd"] is not None:
                raise BudgetError("reservation already settled")
            if amount > dollars(call["reserved_usd"]):
                call["blocked"] = True
                overrun = True
            else:
                call["cost_usd"] = str(amount)
                call["day"] = dt.datetime.now().astimezone().date().isoformat()
        if overrun:
            raise BudgetError("billing exceeded reserved ceiling")
        return amount
