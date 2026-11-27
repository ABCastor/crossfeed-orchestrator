"""Share one machine's quota readings with another over the tailnet.

The Mac holds every provider sign-in, so it is the single reader. It serves its saved
snapshots (numbers, reset clocks, source names; no keys, no account names) from the phone
view. A second machine (a Linux server) pulls them, keeps a copy with the fetch time, and
adopts a pool's snapshot when it is newer than its own. Its own readers still run for any
pool whose adopted reading has aged past the pool's TTL, so a sleeping Mac degrades to
"stale, and here is how old", never to a blind selection.

Configure the puller with `<state>/readings-source.json` {"url": "https://<mac>.<tailnet>.ts.net:8444/crossfeed/readings.json"}
or $FLEET_READINGS_URL. Without either, nothing here runs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Callable

SCHEMA = 1
CONFIG = "readings-source.json"
CACHE = "mac-readings.json"
MAX_BYTES = 1_000_000
FETCH_TIMEOUT_S = 4
# What a snapshot may carry across. Anything else is dropped at the door, on both sides.
KEYS = ("available", "observed_at", "source", "plan", "windows", "idle_windows", "precision_percentage_points",
        "provider_used_percent", "provider", "credits", "spend_credits", "account_usage", "billing",
        "per_model", "per_model_error", "model_quota")
SECRETISH = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|sk-[\w-]{12,}|Bearer\s+\S+|eyJ[\w-]{20,}")


def _clean(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict) or snapshot.get("available") is not True:
        return None
    kept = {key: snapshot[key] for key in KEYS if key in snapshot}
    if not isinstance(kept.get("observed_at"), str) or SECRETISH.search(json.dumps(kept)):
        return None
    return kept


def export(state_dir: Path, now: dt.datetime | None = None) -> dict[str, Any]:
    """The readings this machine would share: usable snapshots only, allow-listed keys."""
    try:
        runtime = json.loads((state_dir / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        runtime = {}
    snapshots = runtime.get("quota_snapshots") if isinstance(runtime, dict) else None
    cleaned = {pool: row for pool, snap in sorted((snapshots or {}).items())
               if isinstance(pool, str) and (row := _clean(snap))}
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"schema": SCHEMA, "generated_at": stamp, "snapshots": cleaned}


def source_url(state_dir: Path) -> str | None:
    url = os.environ.get("FLEET_READINGS_URL")
    if not url:
        try:
            url = json.loads((state_dir / CONFIG).read_text(encoding="utf-8")).get("url")
        except (OSError, ValueError, AttributeError):
            return None
    return url if isinstance(url, str) and url.startswith("https://") else None


def _http_get(url: str, timeout: float) -> bytes:
    # The tailnet gateway names the caller; nothing secret is sent.
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "crossfeed-readings"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https only, checked above
        return response.read(MAX_BYTES + 1)


def pull(state_dir: Path, *, fetch: Callable[[str, float], bytes] | None = None,
         now: dt.datetime | None = None) -> dict[str, Any] | None:
    """Fetch the other machine's readings and keep them; on any failure return the last copy."""
    url = source_url(state_dir)
    if not url:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    cache = state_dir / CACHE
    try:
        body = (fetch or _http_get)(url, FETCH_TIMEOUT_S)
        if len(body) > MAX_BYTES:
            raise ValueError("too large")
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("snapshots"), dict):
            raise ValueError("unexpected shape")
        snapshots = {pool: row for pool, snap in data["snapshots"].items()
                     if isinstance(pool, str) and (row := _clean(snap))}
        kept = {"fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "snapshots": snapshots}
        state_dir.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(kept, sort_keys=True), encoding="utf-8")
        os.replace(temporary, cache)
        return kept
    except Exception:  # noqa: BLE001 - asleep, offline or odd: serve the last good copy
        try:
            kept = json.loads(cache.read_text(encoding="utf-8"))
            return kept if isinstance(kept, dict) and isinstance(kept.get("snapshots"), dict) else None
        except (OSError, ValueError):
            return None


def _parse(stamp: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def adopt(runtime: dict[str, Any], kept: dict[str, Any] | None, pools: set[str] | None = None) -> list[str]:
    """Put the other machine's snapshot in `runtime` for each pool where it is newer than ours.

    The snapshot is marked origin "mac" with the time it was fetched. A failure our own
    reader recorded for that pool is cleared: a good reading from the Mac supersedes it.
    Returns the pools adopted.
    """
    if not kept:
        return []
    snapshots = runtime.setdefault("quota_snapshots", {})
    errors = runtime.setdefault("quota_refresh_errors", {})
    adopted = []
    for pool, row in kept["snapshots"].items():
        if pools is not None and pool not in pools:
            continue
        theirs = _parse(row.get("observed_at"))
        mine = _parse((snapshots.get(pool) or {}).get("observed_at"))
        if theirs is None or (mine is not None and mine >= theirs):
            continue
        snapshots[pool] = {**row, "origin": "mac", "origin_fetched_at": kept.get("fetched_at")}
        errors.pop(pool, None)
        adopted.append(pool)
    return adopted


def origin_of(snapshot: dict[str, Any]) -> str:
    return "mac" if snapshot.get("origin") == "mac" else "local"
