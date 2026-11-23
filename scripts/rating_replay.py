"""Read-only replay of historical selector receipts; never import fleetctl."""

import argparse
import csv
import heapq
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from statistics import mean, median

try:
    from . import rating
except ImportError:  # Installed CLI runs from scripts/.
    import rating

HOLDOUT = datetime(2026, 11, 22, tzinfo=timezone.utc)
KINDS = {"coding-agent": "coding", "repo-qa": "research", "research": "research",
         "review": "review", "writing": "writing"}
STAKES = {"low": "routine", "normal": "routine", "high": "important", "irreversible": "critical"}


def stamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.utcoffset() is not None else None
    except (AttributeError, ValueError):
        return None


def price(item):
    cash = item.get("cash") or {}
    funding = cash.get("funding")
    capacity = item.get("capacity") or {}
    if funding == "metered":
        cash_price = rating.number(cash.get("estimate_usd"))
        if cash_price is None or cash_price < 0:
            return None
        if not capacity:  # A metered-only lane has no subscription debit.
            return cash_price
    elif funding in {"included", "verified-free"}:
        cash_price = 0.0
    else:
        return None
    cost = item.get("cost") or {}
    pool = item.get("pool_price") or {}
    quota = rating.quota_price(cost.get("percent"), capacity.get("weekly_usd_equivalent"),
                               pool.get("lambda"), window_minutes=pool.get("window_minutes"))
    return quota + cash_price if quota is not None else None


def offer(item):
    pool, level, key = (item.get(k) for k in ("pool", "level", "model_key"))
    if not all(isinstance(k, str) and k for k in (pool, level, key)):
        return None
    model = item.get("model_identity") or f"legacy:{key}"
    q = item.get("q") or {}
    grade = q.get("mean") if q.get("unknown") is False else None
    return rating.Offer(rating.Route(model, level, pool), price(item), grade,
                        (item.get("cash") or {}).get("funding"))


@dataclass(frozen=True)
class Event:
    id: str
    started: datetime
    task: rating.Task | None
    current: rating.Offer | None
    offers: tuple[rating.Offer, ...]
    exit_code: int | None
    pinned: bool = False


def receipt(path):
    item = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(item, dict):
        raise ValueError("receipt is not an object")
    return item


def event(path, selections):
    d = receipt(path)
    started = stamp(d.get("started_at"))
    if not started or d.get("dispatch_id") != path.parent.name:
        raise ValueError("invalid dispatch identity or time")
    rc = d.get("returncode")
    args = path.parent.name, started
    try:
        linked = Path(d["selection_file"]).resolve()
        if linked.parent != selections.resolve():
            raise ValueError("selection outside state directory")
        selection = receipt(linked)
        current = offer(selection["choice"])
    except (KeyError, TypeError, OSError, ValueError):
        return Event(*args, None, None, (), rc)
    offers = {current.route: current} if current is not None else {}
    for entry in selection.get("top3") or []:
        candidate = offer(entry)
        if candidate is not None:
            offers.setdefault(candidate.route, candidate)
    marker = (selection.get("hard_reasoning_preference") or {}).get("route")
    hard = bool(marker or selection.get("role") == "hard-reasoning")
    supported_hard = {"opus": "opus", "gpt-fallback": "astra"}.get(marker)
    kind = KINDS.get(selection.get("family"))
    stakes = STAKES.get(selection.get("stakes"))
    task = rating.Task(kind, "hard" if hard else "ordinary", stakes, supported_hard) if kind and stakes else None
    return Event(*args, task, current, tuple(offers.values()), rc, bool(selection.get("target")))


def checked_outcomes(path, events, counts):
    """Join only unique, proof-gated production outcomes; fit benchmark rows are not labels."""
    by_attempt = defaultdict(set)
    proofs = defaultdict(set)
    current_ids = {e.id: e.current.route for e in events if e.current is not None}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("ledger row is not an object")
            except ValueError:
                counts["unreadable ledger rows"] += 1
                continue
            if row.get("evidence_source") == "bench.fit" and row.get("outcome"):
                counts["benchmark fit rows"] += 1
            attempt = row.get("afk_attempt_id")
            if attempt and row.get("schema") == "crossfeed-model-run/v1":
                id_ = row.get("dispatch_id")
                route = rating.Route(row.get("actual_model_identity"), row.get("effort"), row.get("quota_pool"))
                if id_ in current_ids:
                    # Keep mismatched or unresolved execution from crediting the requested route.
                    by_attempt[attempt].add(id_ if route == current_ids[id_] else None)
            if row.get("schema") != "afk-attempt/v1":
                continue
            counts["AFK records"] += 1
            proof = row.get("proof") or {}
            rc = proof.get("returncode")
            passed = row.get("result") == "verified" and rc == 0 and not row.get("failure_class")
            failed = row.get("result") == "failed" and row.get("failure_class") == "proof-failed" and rc != 0
            ended = stamp(row.get("ended_at"))
            if (passed or failed) and type(rc) is int and proof.get("output_sha256") and row.get("attempt_id") and ended:
                proofs[row["attempt_id"]].add((ended, passed))
                counts["qualified AFK records"] += 1
    by_dispatch = defaultdict(set)
    for attempt_id, outcomes in proofs.items():
        ids = by_attempt.get(attempt_id, set())
        if None not in ids and len(ids) == len(outcomes) == 1:
            by_dispatch[next(iter(ids))].add(attempt_id)
    return {id_: next(iter(proofs[next(iter(ids))])) for id_, ids in by_dispatch.items() if len(ids) == 1}


@dataclass(frozen=True)
class Forecast:
    event: Event
    current_p: float | None
    shadow: rating.Plan | None
    checked: bool | None
    reason: str


def forecast(events, outcomes):
    ratings, pending, prices = {}, [], []
    for started, group in groupby(sorted(events, key=lambda e: (e.started, e.id)), key=lambda e: e.started):
        while pending and pending[0][0] < min(started, HOLDOUT):
            _, _, used, task, passed = heapq.heappop(pending)
            ratings = rating.update(used, task, ratings, passed)
        typical = median(prices) if prices else None
        for e in group:
            proof = outcomes.get(e.id)
            if proof is not None and proof[0] < started:
                proof = None
            checked = proof[1] if proof is not None else None
            current_p = rating.success(e.current, e.task, ratings) if e.current and e.task else None
            plan = None
            if not e.current or not e.task:
                reason = "unmapped-or-missing-receipt"
            elif e.pinned:
                reason = "explicit-target"
            elif not typical:
                reason = "no-prior-typical-price"
            else:
                plan = rating.choose(e.offers, e.task, typical, ratings)
                reason = "scored-shortlist" if plan else "unpriced-or-hard-policy"
            yield Forecast(e, current_p, plan, checked, reason)
            # A route estimate existed at this start, but same-time events cannot see it.
            if started < HOLDOUT and e.current and e.current.price is not None:
                prices.append(e.current.price)
            if started < HOLDOUT and e.task and e.current and proof is not None:
                heapq.heappush(pending, (proof[0], e.id, e.current, e.task, checked))


def calibration(rows, outcome):
    pairs = [(f.current_p, actual) for f in rows if f.current_p is not None
             and (actual := outcome(f)) is not None]
    if not pairs:
        return "n=0"
    n = len(pairs)
    bins = []
    for low, high in ((0, .4), (.4, .6), (.6, 1.01)):
        samples = [(p, y) for p, y in pairs if low <= p < high]
        if samples:
            bins.append(f"{len(samples)} at {mean(p for p, _ in samples):.1%} → {mean(y for _, y in samples):.1%}")
    return (f"n={n}, predicted {mean(p for p, _ in pairs):.1%}, observed {mean(y for _, y in pairs):.1%}, "
            f"Brier {mean((p-y)**2 for p, y in pairs):.3f}; bins: " + "; ".join(bins))


def exit_ok(row):
    code = row.event.exit_code
    return code == 0 if type(code) is int else None


def summary(rows, counts):
    held = [f for f in rows if f.event.started >= HOLDOUT]
    kinds = (*rating.KINDS, "unmapped")

    def kind(f):
        return f.event.task.kind if f.event.task else "unmapped"

    def table(subset):
        yield "| Work | Attempts | Multiple recorded options | Current priced | Shadow scored | Changed | Checked | Both-priced totals (current → shadow) |"
        yield "|---|---:|---:|---:|---:|---:|---:|---:|"
        for name in (*kinds, "all"):
            group = [f for f in subset if name == "all" or kind(f) == name]
            plans = [f for f in group if f.shadow]
            priced = [f for f in plans if f.event.current.price is not None]
            cost = (f"{sum(f.event.current.price for f in priced):.5f} → "
                    f"{sum(f.shadow.price for f in priced):.5f} (n={len(priced)})") if priced else "—"
            yield (f"| {name} | {len(group)} | {sum(len(f.event.offers) > 1 for f in group)} | "
                   f"{sum(f.event.current is not None and f.event.current.price is not None for f in group)} | "
                   f"{len(plans)} | {sum(f.shadow.first != f.event.current.route for f in plans)} | "
                   f"{sum(f.checked is not None for f in group)} | {cost} |")

    yield "# Rating shadow replay"
    yield "\nRead-only replay of dispatch **attempts**, not deduplicated jobs. A blank shadow pick means abstain."
    yield ("\n**Adoption bar not met by this replay:** history contains no paired policy trials. "
           "The bar is ≥100 held-out paired checked tasks, ≥20 per promoted kind, no lower checked success "
           "overall or in any promoted kind, ≥10% lower full price, and no added cash, reserve violations or missed critical tasks.")
    yield "\n## All available attempts\n"
    yield from table(rows)
    yield f"\n## Held-out attempts (from {HOLDOUT:%Y-%m-%d %H:%M} UTC)\n"
    yield from table(held)
    yield "\n## Calibration of the historical option (not the unrun alternative)\n"
    for label, group in (("All attempts, before-outcome predictions", rows), ("Held-out", held)):
        yield f"- {label}, checked work: {calibration(group, lambda f: f.checked)}."
        yield f"- {label}, exit-zero diagnostic only: {calibration(group, exit_ok)}."
    yield "\n## Limits\n"
    yield (f"- Ledger: {counts['benchmark fit rows']} fit benchmark outcomes excluded; "
           f"{counts['AFK records']} AFK records, {counts['qualified AFK records']} proof-qualified, "
           f"{counts['joined proofs']} uniquely linked to dispatch attempts. "
           f"Unreadable dispatch/ledger records: {counts['unreadable dispatch receipts']}/{counts['unreadable ledger rows']}.")
    yield ("- Only recorded choice and top three are available, after old rules pruned the field; "
           "there is no way to reconstruct other eligible options or compare unrun outcomes. "
           "Price totals cover only attempts where both picks are priced, not the full feed.")
    yield ("- Non-hard tasks default to ordinary; prior low/normal/high/irreversible map to routine/routine/important/critical. "
           "Legacy default and unlogged task difficulty can make the probabilities misleading. "
           "No conditional fallback outcomes or priced checks are available, so ladders are not replayed.")
    yield ("- Prices use recorded debit × window-adjusted service capacity × (1 + pace), plus incremental cash when metered. "
           "A missing capacity, debit, funding or check stays unknown. Go’s request estimates and Claude’s relative API-weighted "
           "quota proxy are not measured in interchangeable units; a large apparent saving can reflect those assumptions. "
           "These totals are not observed savings or settled bills.")
    yield ("- Log stable task IDs across attempts; pre-choice work kind, difficulty and stakes; all admitted exact model/effort/pool "
           "options and their priced quota windows; checked acceptance/rejection, check cost and fallback link; and observed "
           "debit, cash, reserve state and elapsed time. Fresh paired checked work is needed for policy comparison.")


def route_name(route):
    return f"{route.model}/{route.effort}/{route.subscription}" if route else None


def details(rows, out):
    writer = csv.writer(out, delimiter="\t", lineterminator="\n")
    writer.writerow(("dispatch_id", "started_at", "split", "kind", "difficulty", "stakes", "recorded_options",
                     "current_pick", "current_estimated_price", "current_predicted_success", "shadow_pick",
                     "shadow_estimated_price", "shadow_predicted_success", "dispatch_returncode", "checked", "status"))
    for f in rows:
        e, plan = f.event, f.shadow
        writer.writerow((e.id, e.started.isoformat(), "held-out" if e.started >= HOLDOUT else "fit",
                         e.task.kind if e.task else None, e.task.difficulty if e.task else None,
                         e.task.stakes if e.task else None, len(e.offers),
                         route_name(e.current.route) if e.current else None, e.current.price if e.current else None,
                         f.current_p, route_name(plan.first) if plan else None, plan.price if plan else None,
                         plan.success if plan else None, e.exit_code, f.checked, f.reason))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--details", action="store_true", help="emit TSV on stdout and Markdown report on stderr")
    args = parser.parse_args()
    state = args.state_dir.resolve()
    counts = Counter()
    events = []
    for path in (state / "dispatches").glob("*/dispatch.json"):
        try:
            events.append(event(path, state / "selections"))
        except (OSError, ValueError, TypeError, AttributeError):
            counts["unreadable dispatch receipts"] += 1
    outcomes = checked_outcomes(state / "runs.jsonl", events, counts)
    counts["joined proofs"] = len(outcomes)
    rows = list(forecast(events, outcomes))
    if args.details:
        details(rows, sys.stdout)
    print("\n".join(summary(rows, counts)), file=sys.stderr if args.details else sys.stdout)


if __name__ == "__main__":
    main()
