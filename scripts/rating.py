"""Provisional shadow policy: inputs are already admitted, never dispatch anything."""

from dataclasses import dataclass
from math import exp, isfinite, log
from typing import Mapping, Sequence

KINDS = ("coding", "review", "research", "writing")
DIFFICULTY = {"easy": -0.7, "ordinary": 0.0, "hard": 0.7}
STAKES = {"routine": 1, "important": 5, "critical": 20}
OPUS = "claude-opus-5-5"
ASTRA = "gpt-6.2-astra"


def number(value):
    """Return a finite JSON number, never coerce None, booleans or strings."""
    return value if type(value) in (int, float) and isfinite(value) else None


def _nonnegative(value):
    value = number(value)
    return value if value is not None and value >= 0 else None


def _probability(value):
    value = _nonnegative(value)
    return value if value is not None and value <= 1 else None


@dataclass(frozen=True, order=True)
class Route:
    model: str
    effort: str
    subscription: str


@dataclass(frozen=True)
class Offer:
    route: Route
    price: float | None  # service-dollar estimate + incremental cash
    benchmark_grade: float | None = None
    funding: str | None = None


@dataclass(frozen=True)
class Task:
    kind: str
    difficulty: str
    stakes: str
    # Set by quota/admission, not by the scoring rule. None defers hard work.
    hard_route: str | None = None

    def __post_init__(self):
        if self.kind not in KINDS or self.difficulty not in DIFFICULTY or self.stakes not in STAKES:
            raise ValueError("unknown task kind, difficulty or stakes")


@dataclass(frozen=True)
class Check:
    price: float
    # On rejected tasks only, keyed by (first route, approved fallback route).
    # Caller supplies this only for a trusted check and authorized escalations.
    fallback_success: Mapping[tuple[Route, Route], float]


@dataclass(frozen=True)
class Plan:
    first: Route
    fallback: Route | None
    price: float  # includes expected retry and check expense if applicable
    success: float
    loss: float


RatingKey = tuple[str, Route]


def quota_price(percent, weekly_capacity, pace, *, window_minutes=10080):
    """Convert debit in its quota window to paced service-dollar equivalents."""
    values = tuple(map(_nonnegative, (percent, weekly_capacity, pace, window_minutes)))
    if None in values or not values[1] or not values[3] or values[0] > 100:
        return None
    percent, weekly_capacity, pace, window_minutes = values
    return number(percent / 100 * weekly_capacity * (window_minutes / 10080) * (1 + pace))


# A competent rerun budgets the standard task debit on the reference
# subscription at neutral pressure. This is a planning value, not the bill
# for a specific lane or a statistic of the live policy's cheapest picks.
REFERENCE_WEEKLY_CAPACITY = 200 * 12 / 52
NOMINAL_TASK_PERCENT = .5
COMPETENT_RERUN_PRICE = NOMINAL_TASK_PERCENT / 100 * REFERENCE_WEEKLY_CAPACITY
LIVE_PRICE_BASIS = "subscription-plan-value/v1"

# BR-SELECT-003
def success_prior(pass_rate, difficulty="ordinary"):
    """Transfer a benchmark pass rate, shifting odds for task difficulty."""
    rate = _probability(pass_rate)
    rate = .5 if rate is None else rate
    odds_multiplier = exp(-DIFFICULTY[difficulty])
    return rate * odds_multiplier / (1 - rate + rate * odds_multiplier)


# BR-SELECT-011
# The live selector reads a job's difficulty as an offset on the benchmark logit
# and as a scale on how much of the benchmark's effort lift the job realises.
# Hard jobs are the Terminal-Bench-class jobs the benchmark measures: offset
# and scale are the long-standing values (scale 1). Ordinary and easy offsets
# are labelled priors (real jobs sit easier than Terminal-Bench; no benchmark
# in the cache labels that gap). The ordinary scale is derived: on the two
# models the evidence holds per effort for both Terminal-Bench and the broader,
# easier Intelligence Index, the index's logit gain over the lowest effort is
# 0.66 of Terminal-Bench's (median of eight pairs; range 0.53 to 0.79). Easy
# applies the same step once more, a labelled prior.
JOB_OFFSET = {"easy": -1.4, "ordinary": -0.7, "hard": 0.7}
EFFORT_LIFT_SCALE = {"easy": 0.66 ** 2, "ordinary": 0.66, "hard": 1.0}
# A benchmark of about a hundred tasks cannot tell two pass rates apart when
# they differ by less than one binomial standard error (labelled prior: n).
BENCHMARK_TASKS = 89
# Role to difficulty used when the caller names none (policy may override).
DIFFICULTY_BY_ROLE = {
    **dict.fromkeys(("lookup", "summarise", "summarize", "format", "extract", "triage", "translate"), "easy"),
    **dict.fromkeys(("implementation", "review", "writing", "design", "explain"), "ordinary"),
    **dict.fromkeys(("hard-reasoning", "debug"), "hard"),
}


def _logit(p):
    return log(p / (1 - p))


def effort_lift_ratio(easier, harder):
    """Median ratio of logit gains over the lowest effort: easier / harder benchmark.

    Each argument maps effort rank to a pass fraction in (0, 1) with the lowest
    effort first; pairs with no gain on the harder benchmark are skipped.
    """
    ratios = sorted(
        (_logit(e) - _logit(easier[0])) / (_logit(h) - _logit(harder[0]))
        for e, h in list(zip(easier, harder))[1:] if _logit(h) > _logit(harder[0]))
    if not ratios:
        return None
    mid = len(ratios) // 2
    return ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2


def effort_prior(pass_rate, lowest_rate, difficulty="ordinary"):
    """Success for a job of this difficulty at an effort whose benchmark rate is pass_rate.

    p = sigmoid(logit(lowest) + scale * (logit(rate) - logit(lowest)) - offset).
    Hard jobs reduce to success_prior exactly. Without a measured lowest
    effort the effort lift is zero and only the offset applies. An easier job
    is never less likely to succeed than a harder one at the same effort.
    """
    rate, lowest = _probability(pass_rate), _probability(lowest_rate)
    rate = .5 if rate is None else rate
    hard = success_prior(rate, "hard")
    if difficulty == "hard" or not 0 < rate < 1:
        return hard if difficulty == "hard" else rate  # a certain pass or fail does not move
    if lowest is None or not 0 < lowest < 1:
        lowest = rate

    def at(level):
        gap = _logit(lowest) + EFFORT_LIFT_SCALE[level] * (_logit(rate) - _logit(lowest)) - JOB_OFFSET[level]
        return 1 / (1 + exp(-gap))

    ordinary = max(at("ordinary"), hard)
    return ordinary if difficulty == "ordinary" else max(at("easy"), ordinary)


# BR-SELECT-005
def subscription_price(percent, weekly_capacity, pace, *, window_minutes, scarcity=0):
    """Price the debit at the plan's window value with the existing scarcity premium."""
    return quota_price(percent, weekly_capacity, pace + scarcity, window_minutes=window_minutes)


def expected_cost(price, probability, failure_price):
    """Cost to run the route plus the expected cost of checked failure."""
    return None if price is None else price + (1 - probability) * failure_price


def seed(grade, *, weight=.25):
    """Transfer clipped benchmark log odds, not the raw benchmark fraction."""
    grade = _probability(grade)
    if grade is None:
        return 0.0
    grade = min(0.95, max(0.05, grade))
    return weight * log(grade / (1 - grade))


def success(offer: Offer, task: Task, corrections: Mapping[RatingKey, float]) -> float:
    gap = (seed(offer.benchmark_grade) + corrections.get((task.kind, offer.route), 0.0)
           - DIFFICULTY[task.difficulty])
    return 1 / (1 + exp(-gap))


def update(offer: Offer, task: Task, corrections: Mapping[RatingKey, float], passed: bool):
    """Return a new ratings map after one externally checked, deduplicated label."""
    if type(passed) is not bool:
        raise ValueError("update needs a checked boolean outcome")
    key = task.kind, offer.route
    change = 0.4 * (int(passed) - success(offer, task, corrections))
    return {**corrections, key: min(2, max(-2, corrections.get(key, 0.0) + change))}


def choose(offers: Sequence[Offer], task: Task, typical_price: float | None,
           corrections: Mapping[RatingKey, float], check: Check | None = None) -> Plan | None:
    """Compare direct plans and, if supported, a checked cheapest-first plan.

    No usable typical price, unknown eligible prices, disallowed or unobserved
    hard routing, and an empty offered set are abstentions, not cheap defaults.
    """
    typical_price = _nonnegative(typical_price)
    if task.difficulty == "hard":
        key = {"opus": (OPUS, "claude"), "astra": (ASTRA, "codex")}.get(task.hard_route)
        offers = [o for o in offers if (o.route.model, o.route.subscription) == key]
    if not typical_price or not offers or any(_nonnegative(o.price) is None for o in offers):
        return None
    if check is not None and _nonnegative(check.price) is None:
        return None  # Cannot price the required check even for a direct run.
    check_price = check.price if check is not None else 0.0
    loss_if_failed = typical_price * STAKES[task.stakes]

    def plan(first, fallback, price, p):
        return Plan(first.route, fallback.route if fallback else None, price, p,
                    price + (1 - p) * loss_if_failed)

    candidates = [plan(o, None, o.price + check_price, success(o, task, corrections)) for o in offers]
    if check is not None:
        first = min(offers, key=lambda o: (o.price, o.route))
        p = success(first, task, corrections)
        for fallback in offers:
            if first.route == fallback.route or fallback.funding not in {"included", "verified-free"}:
                continue  # Never model automatic paid escalation.
            q = _probability(check.fallback_success.get((first.route, fallback.route)))
            if q is not None:
                candidates.append(plan(first, fallback,
                                       first.price + check_price + (1 - p) * (fallback.price + check_price),
                                       p + (1 - p) * q))
    return min(candidates, key=lambda p: (p.loss, p.price, p.fallback is not None, p.first,
                                         p.fallback or p.first))
