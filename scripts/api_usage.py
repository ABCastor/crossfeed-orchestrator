"""API usage: what the pools billed per token, by prepaid credit or through a relay cost.

One adapter per source, each returning the same `api` block the glance carries on a pool row:

    {"balance_usd": 1.79, "credits_usd": 26.0, "key_limit_usd": 10, "key_remaining_usd": 3.39,
     "spend": {"today_usd": 0.0, "week_usd": 0.09, "month_usd": 6.61},
     "budget": {"monthly_usd": 20, "source": "operator"},
     "days": [{"date": "2026-11-27", "usd": 0.4, "requests": 12, "tokens": 51000}],
     "top_models": [{"model": "anthropic/claude-sonnet-5.5", "usd": 3.1, "requests": 221}],
     "source": "openrouter-api | admin-cost-api | local-meter", "observed_at": "..."}

Every field is optional. The sources:

- `openrouter-api`: the OpenRouter account and key figures the quota reader already stores in the
  pool's saved reading (`account_usage`, `per_model`, `per_day`). No request is made here.
- `admin-cost-api`: the OpenAI or Anthropic organisation cost report, for a pool whose overlay block
  says `"api_usage": {"source": "admin-cost-api", "provider": "openai" | "anthropic"}` and only when
  an admin key is stored (`openai-admin`, `anthropic-admin` in the portable secret store). One GET
  per provider, a short timeout, results cached for a quarter of an hour.
- `local-meter`: Crossfeed's own runs (the run ledger) and the Gemini spend ledgers in the state
  directory. These figures are estimates Crossfeed measured from its own dispatches at list prices,
  not the provider's bill; the block says so in `source_label`.

Nothing here runs while a page renders. `refresh()` is called by the same paths that read the quota
sources and writes one cache file in the state directory; `read()` and `attach()` only read it.
A budget is the operator's own figure from the panel preferences, attached at read time and only
shown: no code path here or elsewhere acts on it.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

try:
    import native_usage
except ImportError:  # imported from the console as scripts.api_usage
    from scripts import native_usage
try:
    import secret_store
except ImportError:
    from scripts import secret_store

SCHEMA = 1
CACHE = "api-usage.json"
RUN_LEDGER = "runs.jsonl"
HTTP_TIMEOUT_S = 6
ADMIN_TTL_S = 900
REBUILD_MIN_S = 30
DAYS = 30
MAX_LEDGER_BYTES = 64_000_000
LOCAL_LABEL = "Measured by Crossfeed"

# Pools that bill by the token, by prepaid credit or through a relay even when the overlay says nothing.
KNOWN_API_POOLS = {"openrouter-free", "openrouter-mimo", "gemini-metered", "chatgpt-work"}
BILLING_WORDS = {"per-token", "per_token", "prepaid", "credit", "credits", "relay", "metered"}
BILLING_URLS = {
    "openrouter-free": "https://openrouter.ai/settings/credits",
    "openrouter-mimo": "https://openrouter.ai/settings/credits",
    "gemini-metered": "https://aistudio.google.com/usage",
}
ADMIN_PROVIDERS = {
    "openai": {"secret": "openai-admin", "billing_url": "https://platform.openai.com/settings/organization/billing/overview"},
    "anthropic": {"secret": "anthropic-admin", "billing_url": "https://console.anthropic.com/settings/billing"},
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _amount(value: Any) -> float | None:
    return native_usage._finite_amount(value)


def _usd(value: Any) -> float | None:
    number = _amount(value)
    return None if number is None or number < 0 else round(number, 6)


def pool_config(roster: dict[str, Any] | None, pool: str) -> dict[str, Any]:
    config = ((roster or {}).get("quota_pools") or {}).get(pool)
    return config if isinstance(config, dict) else {}


def is_api_pool(roster: dict[str, Any] | None, pool: str) -> bool:
    """A pool whose use costs money per token or credit, or goes through a relay."""
    config = pool_config(roster, pool)
    plan = config.get("plan") if isinstance(config.get("plan"), dict) else {}
    return (pool in KNOWN_API_POOLS or isinstance(config.get("api_usage"), dict)
            or str(plan.get("billing") or "").lower() in BILLING_WORDS
            or str(config.get("funding") or "").lower() == "metered")


def api_pools(roster: dict[str, Any] | None) -> list[str]:
    return [pool for pool in ((roster or {}).get("quota_pools") or {}) if isinstance(pool, str) and is_api_pool(roster, pool)]


# ---- local meter: Crossfeed's own runs ------------------------------------------------------------
def _tokens_of(record: dict[str, Any]) -> int:
    tokens = record.get("tokens")
    if not isinstance(tokens, dict):
        return 0
    total = tokens.get("total")
    if type(total) is int and total >= 0:
        return total
    return sum(int(tokens.get(key) or 0) for key in ("input", "output", "reasoning") if type(tokens.get(key)) is int)


def _local_day(moment: dt.datetime) -> dt.date:
    return moment.astimezone().date()


def run_records(state_dir: Path, pool: str, lane_ids: set[str]):
    """(local day, model, usd, tokens) for each run the ledger attributes to this pool."""
    path = state_dir / RUN_LEDGER
    try:
        if path.stat().st_size > MAX_LEDGER_BYTES:
            return
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            stamp = _parse(record.get("ended_at") or record.get("started_at"))
            if stamp is None:
                continue
            recorded = record.get("quota_pool")
            if recorded is not None:
                if recorded != pool:
                    continue
            elif record.get("lane_id") not in lane_ids:
                continue
            cost = record.get("cost") if isinstance(record.get("cost"), dict) else {}
            usd = _usd(cost.get("estimated_usd")) or 0.0
            model = record.get("model") if isinstance(record.get("model"), str) else "unknown"
            yield _local_day(stamp), model, usd, _tokens_of(record)


def gemini_ledger_records(state_dir: Path):
    """Completed calls from the Gemini image and media spend ledgers: (local day, model, usd, tokens)."""
    for name in sorted(glob.glob(str(state_dir / "gemini-*-spend.json"))):
        try:
            ledger = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        days = ledger.get("days") if isinstance(ledger, dict) else None
        if not isinstance(days, dict):
            continue
        for key, entries in days.items():
            try:
                day = dt.date.fromisoformat(str(key)[:10])
            except ValueError:
                continue
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and entry.get("status") == "completed":
                    model = entry.get("model") if isinstance(entry.get("model"), str) else Path(name).stem
                    yield day, model, _usd(entry.get("actual_usd")) or 0.0, 0


def _summarise(records, today: dt.date) -> dict[str, Any] | None:
    """Days, spend windows and top models from (day, model, usd, tokens) tuples."""
    by_day: dict[dt.date, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    for day, model, usd, tokens in records:
        bucket = by_day.setdefault(day, {"usd": 0.0, "requests": 0, "tokens": 0})
        bucket["usd"] += usd
        bucket["requests"] += 1
        bucket["tokens"] += tokens
        if (today - day).days < DAYS:
            row = by_model.setdefault(model, {"model": model, "usd": 0.0, "requests": 0})
            row["usd"] += usd
            row["requests"] += 1
    if not by_day:
        return None
    first = max(min(by_day), today - dt.timedelta(days=DAYS - 1))
    days = []
    for offset in range((today - first).days + 1):
        day = first + dt.timedelta(days=offset)
        bucket = by_day.get(day) or {"usd": 0.0, "requests": 0, "tokens": 0}
        days.append({"date": day.isoformat(), "usd": round(bucket["usd"], 6), "requests": bucket["requests"],
                     "tokens": bucket["tokens"]})
    return {"days": days, "spend": spend_of(days, today),
            "top_models": sorted(({**row, "usd": round(row["usd"], 6)} for row in by_model.values()),
                                 key=lambda row: (-row["usd"], -row["requests"], row["model"]))[:5]}


def spend_of(days: list[dict[str, Any]], today: dt.date) -> dict[str, float]:
    """Today, the last seven days and the calendar month so far, from per-day rows."""
    def total(since: dt.date) -> float:
        return round(sum(row["usd"] for row in days
                         if isinstance(row.get("usd"), (int, float)) and since <= dt.date.fromisoformat(row["date"]) <= today), 6)
    return {"today_usd": total(today), "week_usd": total(today - dt.timedelta(days=6)),
            "month_usd": total(today.replace(day=1))}


def local_meter(state_dir: Path, roster: dict[str, Any] | None, pool: str, now: dt.datetime) -> dict[str, Any] | None:
    lanes = {lane["lane_id"] for lane in (roster or {}).get("lanes", []) if lane.get("quota_pool") == pool and lane.get("lane_id")}
    records = list(run_records(state_dir, pool, lanes))
    if pool == "gemini-metered":
        records += list(gemini_ledger_records(state_dir))
    summary = _summarise(records, _local_day(now))
    if summary is None:
        return None
    return {**summary, "source": "local-meter", "source_label": LOCAL_LABEL, "observed_at": _iso(now)}


# ---- OpenRouter: the account the quota reader already stored --------------------------------------
def openrouter_block(snapshot: dict[str, Any], now: dt.datetime) -> dict[str, Any] | None:
    cash = snapshot.get("account_usage") if isinstance(snapshot, dict) else None
    if not isinstance(cash, dict) or not cash:
        return None
    block: dict[str, Any] = {}
    for field, key in (("balance_usd", "account_balance_usd"), ("credits_usd", "account_credits_usd"),
                       ("key_limit_usd", "key_limit_usd"), ("key_remaining_usd", "key_limit_remaining_usd")):
        value = _amount(cash.get(key))
        if value is not None:
            block[field] = round(value, 6)
    spend = {name: _usd(cash.get(key)) for name, key in (("today_usd", "key_usage_daily_usd"),
                                                          ("week_usd", "key_usage_weekly_usd"),
                                                          ("month_usd", "key_usage_monthly_usd"))}
    spend = {name: value for name, value in spend.items() if value is not None}
    if spend:
        block["spend"] = spend
    models = [{"model": row["model"], "usd": round(float(row["usd_30d"]), 6), "requests": int(row["requests"])}
              for row in (snapshot.get("per_model") or []) if isinstance(row, dict) and isinstance(row.get("model"), str)
              and _amount(row.get("usd_30d")) is not None and type(row.get("requests")) is int][:5]
    if models:
        block["top_models"] = models
    days = [{"date": row["date"], "usd": _usd(row.get("usd")) or 0.0, "requests": int(row.get("requests") or 0)}
            for row in (snapshot.get("per_day") or []) if isinstance(row, dict) and isinstance(row.get("date"), str)][-DAYS:]
    if days:
        block["days"] = days
        block.setdefault("spend", spend_of(days, dt.date.fromisoformat(days[-1]["date"])))
    if not block:
        return None
    observed = snapshot.get("observed_at")
    return {**block, "source": "openrouter-api", "observed_at": observed if isinstance(observed, str) else _iso(now)}


# ---- organisation cost APIs behind an admin key ---------------------------------------------------
def _get_json(url: str, headers: dict[str, str], opener: Callable[..., Any] | None, timeout: float) -> Any:
    status, body = native_usage._http_get(url, headers, timeout, opener)
    if status in (401, 403):
        raise PermissionError("the provider rejected the admin key")
    if status != 200:
        raise OSError(f"HTTP {status}")
    parsed = native_usage._json(body)
    if not isinstance(parsed, dict):
        raise ValueError("unreadable answer")
    return parsed


def _fold(rows: list[tuple[str, str, float]], today: dt.date) -> dict[str, Any]:
    """(date, model, usd) rows into days, spend and top models."""
    by_day: dict[str, float] = {}
    by_model: dict[str, float] = {}
    for date, model, usd in rows:
        by_day[date] = by_day.get(date, 0.0) + usd
        by_model[model] = by_model.get(model, 0.0) + usd
    days = [{"date": date, "usd": round(by_day[date], 6)} for date in sorted(by_day)][-DAYS:]
    return {"days": days, "spend": spend_of(days, today) if days else {},
            "top_models": [{"model": model, "usd": round(usd, 6)}
                           for model, usd in sorted(by_model.items(), key=lambda item: (-item[1], item[0]))[:5]]}


def openai_costs(key: str, now: dt.datetime, opener: Callable[..., Any] | None = None,
                 timeout: float = HTTP_TIMEOUT_S) -> dict[str, Any]:
    """OpenAI organisation costs: `GET /v1/organization/costs`, daily buckets, each result `amount.value` in USD."""
    start = int((now - dt.timedelta(days=DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    url = f"https://api.openai.com/v1/organization/costs?start_time={start}&bucket_width=1d&limit={DAYS + 1}&group_by=line_item"
    body = _get_json(url, {"Authorization": f"Bearer {key}", "Accept": "application/json"}, opener, timeout)
    rows: list[tuple[str, str, float]] = []
    for bucket in body.get("data") or []:
        if not isinstance(bucket, dict) or type(bucket.get("start_time")) is not int:
            continue
        date = dt.datetime.fromtimestamp(bucket["start_time"], dt.timezone.utc).date().isoformat()
        for result in bucket.get("results") or []:
            amount = (result.get("amount") or {}) if isinstance(result, dict) else {}
            usd = _usd(amount.get("value")) if str(amount.get("currency") or "usd").lower() == "usd" else None
            if usd is not None:
                label = result.get("line_item") if isinstance(result.get("line_item"), str) else "other"
                rows.append((date, label, usd))
    return _fold(rows, now.date())


def anthropic_costs(key: str, now: dt.datetime, opener: Callable[..., Any] | None = None,
                    timeout: float = HTTP_TIMEOUT_S) -> dict[str, Any]:
    """Anthropic organisation cost report: `GET /v1/organizations/cost_report`; each `amount` is a decimal
    string in the currency's lowest unit (cents), so it is divided by 100."""
    start = (now - dt.timedelta(days=DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    url = ("https://api.anthropic.com/v1/organizations/cost_report?starting_at="
           f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}&bucket_width=1d&limit={DAYS + 1}&group_by[]=description")
    body = _get_json(url, {"x-api-key": key, "anthropic-version": "2023-06-01", "Accept": "application/json"}, opener, timeout)
    rows: list[tuple[str, str, float]] = []
    for bucket in body.get("data") or []:
        stamp = _parse(bucket.get("starting_at")) if isinstance(bucket, dict) else None
        if stamp is None:
            continue
        for result in bucket.get("results") or []:
            if not isinstance(result, dict) or str(result.get("currency") or "USD").upper() != "USD":
                continue
            cents = _amount(result.get("amount"))
            if cents is not None and cents >= 0:
                label = result.get("model") or result.get("description")
                rows.append((stamp.date().isoformat(), label if isinstance(label, str) else "other", cents / 100))
    return _fold(rows, now.date())


ADMIN_FETCH = {"openai": openai_costs, "anthropic": anthropic_costs}


def admin_block(provider: str, entry: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    block = {key: entry[key] for key in ("days", "spend", "top_models") if entry.get(key)}
    block.update(source="admin-cost-api", observed_at=entry.get("fetched_at") or _iso(now),
                 billing_url=ADMIN_PROVIDERS[provider]["billing_url"])
    if entry.get("error"):
        block["error"] = entry["error"]
    return block


# ---- the cache -------------------------------------------------------------------------------------
def _read_cache(state_dir: Path) -> dict[str, Any]:
    try:
        cache = json.loads((state_dir / CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) and cache.get("schema") == SCHEMA else {}


def _write_cache(state_dir: Path, cache: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{CACHE}.", dir=state_dir)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
        os.replace(temporary, state_dir / CACHE)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def refresh(state_dir: Path, roster: dict[str, Any] | None, runtime: dict[str, Any] | None = None, *,
            force: bool = False, now: dt.datetime | None = None,
            opener: Callable[..., Any] | None = None, key_for: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """Read every API pool's sources and save the blocks. Returns the saved blocks by pool.

    The local sources are files on this machine; the network is touched only for an admin cost API whose
    cached answer is older than `ADMIN_TTL_S` (or when forced), once per pool, with a short timeout. A failed
    read keeps the last good answer and records a fixed message, never the key or the provider's text.
    """
    now = now or _now()
    previous = _read_cache(state_dir)
    stamp = _parse(previous.get("generated_at"))
    if not force and stamp and 0 <= (now - stamp).total_seconds() < REBUILD_MIN_S:
        return previous.get("pools") or {}
    if runtime is None:
        try:
            runtime = json.loads((state_dir / "runtime.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            runtime = {}
    snapshots = (runtime or {}).get("quota_snapshots") or {}
    resolve = key_for or secret_store.resolve
    admin_cache = dict(previous.get("admin") or {})
    pools: dict[str, Any] = {}
    for pool in api_pools(roster):
        config = pool_config(roster, pool)
        declared = config.get("api_usage") if isinstance(config.get("api_usage"), dict) else {}
        block: dict[str, Any] | None = None
        provider = declared.get("provider") if declared.get("source") == "admin-cost-api" else None
        if provider in ADMIN_FETCH:
            entry = admin_cache.get(pool) if isinstance(admin_cache.get(pool), dict) else {}
            fetched = _parse(entry.get("fetched_at"))
            due = force or not fetched or (now - fetched).total_seconds() >= ADMIN_TTL_S
            key = None
            if due:
                try:
                    key = resolve(ADMIN_PROVIDERS[provider]["secret"])
                except Exception:  # noqa: BLE001 - a broken store means "no admin key", never a failed refresh
                    key = None
            if key:
                try:
                    entry = {**ADMIN_FETCH[provider](key, now, opener), "fetched_at": _iso(now)}
                except PermissionError:
                    entry = {**entry, "error": "The provider rejected the admin key."}
                except Exception:  # noqa: BLE001 - the exception text can carry the URL; say a fixed thing
                    entry = {**entry, "error": "The cost report could not be read."}
                admin_cache[pool] = entry
            if entry.get("days") or entry.get("error"):
                block = admin_block(provider, entry, now)
        if block is None and pool.startswith("openrouter"):
            block = openrouter_block(snapshots.get(pool) or {}, now)
        local = None
        if block is None or not block.get("days"):
            local = local_meter(state_dir, roster, pool, now)
        if block is None:
            block = local
        elif local and not block.get("days"):
            block = {**block, "days": local["days"], "days_source": "local-meter"}
            block.setdefault("spend", local["spend"])
            block.setdefault("top_models", local["top_models"])
        if block is None:
            continue
        if pool in BILLING_URLS:
            block.setdefault("billing_url", BILLING_URLS[pool])
        pools[pool] = block
    _write_cache(state_dir, {"schema": SCHEMA, "generated_at": _iso(now), "pools": pools, "admin": admin_cache})
    return pools


def read(state_dir: Path | None) -> dict[str, Any]:
    """The blocks the last refresh saved, by pool. Reads one small file; makes no request."""
    return (_read_cache(state_dir).get("pools") or {}) if state_dir else {}


def attach(rows: list[dict[str, Any]], state_dir: Path | None, budgets: dict[str, Any] | None = None) -> None:
    """Put each pool's saved block on its glance row as `api`, with the operator's monthly budget if one is set.

    A budget is only shown: it is the operator's figure and no routing or admission reads it.
    """
    saved = read(state_dir)
    for row in rows:
        block = saved.get(row.get("pool"))
        if not isinstance(block, dict):
            continue
        out = dict(block)
        amount = (budgets or {}).get(row["pool"])
        if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
            out["budget"] = {"monthly_usd": amount, "source": "operator"}
        row["api"] = out
