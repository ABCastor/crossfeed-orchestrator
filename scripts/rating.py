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


# Provisional planning anchor, not an observed acceptance rate. The operator's
# brief calls for 80–85% on a typical routine job; use its midpoint and reuse
# the replay's weak benchmark prior and difficulty offsets.
TYPICAL_SUCCESS = .825


def success_prior(grade, difficulty="ordinary"):
    gap = log(TYPICAL_SUCCESS / (1 - TYPICAL_SUCCESS)) + seed(grade) - DIFFICULTY[difficulty]
    return 1 / (1 + exp(-gap))


def subscription_price(percent, weekly_capacity, pace, *, window_minutes, scarcity=0):
    """Price the same quota share higher in a smaller subscription.

    Normalize opportunity cost to a roughly $200/month reference subscription,
    rather than multiplying by the plan's own list value (which reverses the
    incentive). Pace and observed remaining capacity add nonnegative premiums.
    """
    reference = 200 * 12 / 52
    if not weekly_capacity or weekly_capacity <= 0:
        return None
    return quota_price(percent, reference * reference / weekly_capacity,
                       pace + scarcity, window_minutes=window_minutes)


def expected_cost(price, probability, failure_price):
    """Cost to run the route plus the expected cost of checked failure."""
    return None if price is None else price + (1 - probability) * failure_price


def seed(grade):
    """Use a benchmark weakly; a benchmark fraction is not a pass forecast."""
    grade = _probability(grade)
    if grade is None:
        return 0.0
    grade = min(0.95, max(0.05, grade))
    return 0.25 * log(grade / (1 - grade))


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
