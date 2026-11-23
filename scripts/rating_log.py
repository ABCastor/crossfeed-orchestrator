"""Local, append-only shadow forecasts and explicit acceptance evidence."""
import json
import os
import re
from pathlib import Path
from statistics import median

try:
    from . import rating
    from .rating_replay import offer, stamp
except ImportError:  # Installed CLI runs from scripts/.
    import rating
    from rating_replay import offer, stamp

LEDGER = "rating-trials.jsonl"
DISPATCH = "fleet-rating-dispatch/v1"
ACCEPTANCE = "fleet-rating-acceptance/v1"
TAIL_BYTES = 2 * 1024 * 1024


def task_id(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("ID must be an opaque token of 1–128 letters, digits, or ._:-")
    return value


def labels(args, generated_id=None):
    return {"task_id": getattr(args, "task_id", None) or generated_id,
            "work_kind": getattr(args, "work_kind", None) or "unknown",
            "difficulty": getattr(args, "difficulty", None) or "unknown",
            "stakes": getattr(args, "rating_stakes", None) or "unknown"}


def rows(lines):
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def typical_price(state_dir):
    """Use up to 256 prior priced first attempts, before the present forecast."""
    try:
        with (state_dir / LEDGER).open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - TAIL_BYTES))
            if size > TAIL_BYTES:
                handle.readline()
            recent = list(rows(handle))
    except FileNotFoundError:
        return None
    prices = []
    for row in reversed(recent):
        if row.get("schema") == DISPATCH and row.get("attempt_index") == 0:
            price = rating.number((row.get("live_pick") or {}).get("estimated_price"))
            if price is not None and price >= 0:
                prices.append(price)
                if len(prices) == 256:
                    break
    return median(prices) if prices else None


def options(selection):
    if "eligible_options" in selection:
        return selection["eligible_options"]
    choice = selection["choice"]
    return [choice, *(item for item in selection["top3"] if item is not choice)]


def describe(item, rated=None, predicted=None):
    return {**{key: item.get(key) for key in ("pool", "model_key", "model_identity", "level",
                                             "harness", "lane_id")},
            "estimated_price": rated.price if rated else None,
            "predicted_success": predicted,
            "grade": (item.get("q") or {}).get("mean") if (item.get("q") or {}).get("unknown") is False else None,
            "quota_price_inputs": {"cost": item.get("cost"), "pool_price": item.get("pool_price"),
                                   "capacity": item.get("capacity"), "cash": item.get("cash")}}


def forecast(selection, live, task_labels, typical):
    """Freeze benchmark-only predictions; never return an option to the live picker."""
    # Explicit handling of absent labels: stakes don't affect success, but an
    # unknown level of harm cannot support selecting a lowest-loss plan.
    kind, difficulty, stakes = (task_labels[k] for k in ("work_kind", "difficulty", "stakes"))
    marker = (selection.get("hard_reasoning_preference") or {}).get("route")
    hard_route = {"opus": "opus", "gpt-fallback": "astra"}.get(marker)
    task = (rating.Task(kind, difficulty, stakes if stakes in rating.STAKES else "routine", hard_route)
            if kind in rating.KINDS and difficulty in rating.DIFFICULTY else None)
    rated_items = [(item, offer(item)) for item in options(selection)]
    live_offer = offer(live)

    def describe_rated(item, rated):
        return describe(item, rated, rating.success(rated, task, {}) if rated and task else None)

    result = {"live_pick": describe_rated(live, live_offer), "shadow_pick": None,
              "eligible_options": [describe_rated(item, rated) for item, rated in rated_items],
              "typical_price": typical, "hard_route": hard_route}
    if not task or stakes not in rating.STAKES:
        result["shadow_status"] = "unknown_task_labels"
    elif typical is None or typical <= 0:
        result["shadow_status"] = "no_typical_price"
    elif any(rated is None for _, rated in rated_items):
        result["shadow_status"] = "unidentified_option"
    else:
        offers = [rated for _, rated in rated_items]
        try:
            plan = rating.choose(offers, task, typical, {})
        except Exception:  # Scoring does not control dispatch or lose the live forecast.
            result["shadow_status"] = "rating_error"
            return result
        result["shadow_status"] = "scored" if plan else "abstained"
        if plan:
            # The pure policy compares direct plans and breaks ties by Route.
            # Same-route offers can have different transport prices: recover
            # the exact offer that matches the selected prediction and price.
            item, rated = next((item, rated) for item, rated in rated_items
                               if rated.route == plan.first and rated.price == plan.price
                               and rating.success(rated, task, {}) == plan.success)
            result["shadow_pick"] = describe(item, rated, plan.success)
    return result


def debit(before, after, started_at, ended_at):
    """Compare cached readings in the same window; pool deltas aren't causal."""
    a, b, start, end = map(stamp, (before.get("observed_at"), after.get("observed_at"), started_at, ended_at))
    source = before.get("source")
    if (not all((a, b, start, end)) or not (a <= start <= b <= end and a < b)
            or not source or source != after.get("source")):
        return []
    observations = []
    for name, first in before.get("windows", {}).items():
        last = after.get("windows", {}).get(name, {})
        reset = stamp(first.get("reset_at"))
        p, q = rating.number(first.get("used_percent")), rating.number(last.get("used_percent"))
        if (reset and reset == stamp(last.get("reset_at")) and b < reset and
                first.get("window_minutes") == last.get("window_minutes") and
                p is not None and q is not None and 0 <= p <= q <= 100):
            observations.append({"window": name, "source": source, "reset_at": first["reset_at"],
                                 "before_at": before["observed_at"], "after_at": after["observed_at"],
                                 "before_used_percent": p, "after_used_percent": q,
                                 "delta_percentage_points": q - p, "attribution": "pool_window_not_isolated"})
    return observations


def acceptance(state_dir, run_id, verdict, check, recorded_at):
    """Resolve either a dispatch run ID or its verified wrapper run ID."""
    task_id(run_id)
    check = check.strip()
    if not check or len(check) > 2000:
        raise ValueError("--check must describe the check in 1–2000 characters")
    try:
        with (Path(state_dir) / LEDGER).open("rb") as handle:
            matches = [row for row in rows(handle) if row.get("schema") == DISPATCH and
                       run_id in (row.get("run_id"), row.get("worker_run_id"))]
    except FileNotFoundError:
        matches = []
    if len(matches) != 1:
        raise ValueError("no unique rating dispatch for that run ID")
    dispatch = matches[0]
    return {"schema": ACCEPTANCE, "run_id": dispatch["run_id"], "task_id": dispatch["task_id"],
            "worker_run_id": dispatch.get("worker_run_id"), "verdict": verdict, "check": check,
            "recorded_at": recorded_at}
