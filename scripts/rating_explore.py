"""Checked-outcome Thompson sampling within a dispatch-wide exploration budget."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

try:
    from . import rating, rating_log
except ImportError:  # Installed CLI runs from scripts/.
    import rating
    import rating_log

PRIOR_STRENGTH = 4
MEAN_COST_BAND = 1.25
DEFAULT_EXPLORATION_CAP = 0.15
TRIAL = "fleet-rating-trial/v1"


def pick_summary(item):
    return {key: item.get(key) for key in (
        "pool", "model_key", "model_identity", "level", "harness", "estimated_price",
        "p_success", "expected_cost",
    )}


def route_key(item):
    model = item.get("model_identity") or item.get("model_key")
    key = (item.get("pool"), model, item.get("level"))
    return key if all(isinstance(part, str) and part for part in key) else None


def posterior(grade, passes=0, failures=0):
    # A Beta distribution needs two positive shape parameters. A clipped grade
    # is still only a proxy; even a benchmark's 100% is not certainty here.
    p = min(0.99, max(0.01, grade))
    return PRIOR_STRENGTH * p + passes, PRIOR_STRENGTH * (1 - p) + failures


def budget_allows(totals, cap):
    total, explored = totals
    # The denominator includes the new dispatch; no burst can exceed the cap.
    return (explored + 1) / (total + 1) <= cap


def checked_counts(state_dir):
    """Count the first verified verdict for each task and exact option.

    Repeated acceptance and fallback attempts on the same task/route do not
    become extra observations. Only accept records can supply labels.
    """
    path = Path(state_dir) / rating_log.LEDGER
    try:
        with path.open("rb") as handle:
            accepted = {}
            for row in rating_log.rows(handle):
                if row.get("schema") == rating_log.ACCEPTANCE and isinstance(row.get("run_id"), str) \
                        and isinstance(row.get("task_id"), str) and row.get("verdict") in {"pass", "fail"} \
                        and isinstance(row.get("check"), str) and row["check"].strip():
                    accepted.setdefault(row["run_id"], (row["task_id"], row["verdict"]))
            if not accepted:
                return {}
            handle.seek(0)
            routes = {}
            for row in rating_log.rows(handle):
                if row.get("schema") != rating_log.DISPATCH or not isinstance(row.get("run_id"), str):
                    continue
                verdict = accepted.get(row["run_id"])
                live = row.get("live_pick")
                if verdict and verdict[0] == row.get("task_id") and isinstance(live, dict):
                    routes[row["run_id"]] = route_key(live)
            counts, seen = Counter(), set()
            for run, (task, verdict) in accepted.items():
                key = routes.get(run)
                if key is not None and (task, key) not in seen:
                    seen.add((task, key))
                    counts[(key, verdict)] += 1
            return counts
    except FileNotFoundError:
        return {}


def read_totals(state_dir):
    """Read the newest trial counter backwards, without parsing a long history."""
    try:
        with (Path(state_dir) / rating_log.LEDGER).open("rb") as handle:
            end = handle.seek(0, 2)
            partial = b""
            schema = json.dumps(TRIAL).encode()
            while end:
                start = max(0, end - 65536)
                handle.seek(start)
                data = handle.read(end - start) + partial
                lines = data.split(b"\n")
                partial = lines.pop(0) if start else b""
                for line in reversed(lines):
                    if schema not in line:
                        continue
                    for row in rating_log.rows([line]):
                        if row.get("schema") != TRIAL:
                            continue
                        total, explored = row.get("dispatches"), row.get("non_greedy_dispatches")
                        if type(total) is int and type(explored) is int and 0 <= explored <= total:
                            return total, explored
                    # A broken counter must fail closed, not silently reset the cap.
                    raise ValueError("invalid latest rating trial counter")
                end = start
    except FileNotFoundError:
        pass
    return 0, 0


def exploration_safe(item):
    pool_price = item.get("pool_price") or {}
    route = route_key(item)
    if not route or item.get("funding") not in {"included", "verified-free"} \
            or item.get("spend_level") == "low" or any("kimi" in part.lower() for part in route):
        return False
    if item.get("funding") == "verified-free" and pool_price.get("projection_basis") == "none-known":
        return True
    used = (pool_price.get("window") or {}).get("used_percent")
    debit = (item.get("cost") or {}).get("percent")
    return (not pool_price.get("projection_unknown")
            and pool_price.get("state") in {"ABUNDANT", "HEALTHY"}
            and all(rating.number(x) is not None and x >= 0 for x in (used, debit))
            and used + debit < 75)


def sample_choice(options, failure_price, rng):
    """Sample only inside the already admitted policy tier and cheap mean band."""
    mean = options[0]
    best_cost = mean.get("expected_cost")
    if best_cost is None or not exploration_safe(mean) or not failure_price:
        return mean, None, None
    band = [item for item in options if item.get("expected_cost") is not None
            and item["expected_cost"] <= best_cost * MEAN_COST_BAND and exploration_safe(item)]
    if len(band) < 2:
        return mean, None, None
    sampled = []
    for item in band:
        p = rng.betavariate(*item["posterior"])
        sampled.append((item, p, rating.expected_cost(item["estimated_price"], p, failure_price)))
    return min(sampled, key=lambda row: row[2])
