"""Build live success priors from raw benchmark results, not roster ranks."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict

try:
    from . import evidence, rating
except ImportError:  # Installed CLI runs from scripts/.
    import evidence
    import rating

TERMINAL = "aa_terminal"
# There is no exact-effort public review or debug benchmark in the cache.
# Terminal tasks are the common agent-work proxy, not measurements of those roles.
FAMILY_METRICS = {
    "coding-agent": (TERMINAL, "aa_tau2"), "debug": (TERMINAL,), "review": (TERMINAL,),
    "repo-qa": ("aa_lcr", TERMINAL), "reasoning": ("aa_lcr",),
    "research": ("aa_lcr", "aa_tau2"), "extraction": ("aa_ifbench",),
}


def _raw_sources(row):
    sources = {}
    for source in row.get("sources", ()):
        mapping = source.get("mapping")
        value = rating.number(source.get("value"))
        if (value is None or not 0 <= value <= 100 or source.get("imputed")
                or not (rating.number(source.get("weight")) or 0) > 0):
            continue
        if source.get("metric") not in evidence.ASSUMPTIONS["sources"].get(mapping, {}).get("metrics", ()):
            continue
        sources.setdefault(mapping, source)
    return sources


def _fraction(source):
    value = source["value"]
    # The built-in leaderboard and AA model-page rows explicitly store percent.
    percent = value > 1 or (source.get("url") and source["mapping"] == TERMINAL)
    return value / 100 if percent else value


def _index(sources):
    source = sources.get("aa_v5_coding_proxy")
    if source and str(source.get("index_version")).lower().removeprefix("v") == "5":
        return source["value"]
    return None


def _terminal_fit(sources_by_cell):
    """Regress on co-observed raw TB4 cells and give each model equal weight."""
    by_model = defaultdict(list)
    for (model, _), sources in sources_by_cell.items():
        if TERMINAL in sources and (x := _index(sources)) is not None:
            by_model[model].append((x, _fraction(sources[TERMINAL])))
    if len(by_model) < 3:
        return None
    pairs = [(x, y, 1 / len(cells)) for cells in by_model.values() for x, y in cells]
    xbar = sum(x * w for x, _, w in pairs) / len(by_model)
    ybar = sum(y * w for _, y, w in pairs) / len(by_model)
    denominator = sum(w * (x - xbar) ** 2 for x, _, w in pairs)
    if not denominator:
        return None
    slope = sum(w * (x - xbar) * (y - ybar) for x, y, w in pairs) / denominator
    if slope <= 0:
        return None
    return xbar, ybar, slope, min(x for x, _, _ in pairs), max(x for x, _, _ in pairs), len(pairs)


class SuccessEvidence:
    """Calculate an estimate and auditable source for an exact model/effort."""

    def __init__(self, rows):
        self.sources = {key: _raw_sources(row) for key, row in rows.items()}
        self.by_model = defaultdict(dict)
        for (model, level), sources in self.sources.items():
            self.by_model[model][level] = sources
        self.fit = _terminal_fit(self.sources)
        # No model evidence: use the lower tail of observed results, not the
        # roster rank or a reward for missing data. An empty corpus stays neutral.
        rates = [_fraction(s[TERMINAL]) for s in self.sources.values() if TERMINAL in s]
        self.low_rate = min(rates, default=.5)
        self.has_rates = bool(rates)

    # BR-SELECT-003
    def rate(self, model, level, family):
        metrics = FAMILY_METRICS.get(family, ())
        exact = self.sources.get((model, level), {})
        for mapping in metrics:
            if mapping in exact:
                basis = ("role_proxy_benchmark_estimate" if mapping == TERMINAL and family != "coding-agent"
                         else "benchmark")
                return self._result(exact[mapping], level, basis)
        transfers = metrics if TERMINAL in metrics else (*metrics, TERMINAL)
        for mapping in transfers:
            if mapping in exact:
                return self._result(exact[mapping], level, "cross_family_benchmark_estimate")
            siblings = [(effort, rows) for effort, rows in self.by_model.get(model, {}).items() if mapping in rows]
            if not siblings:
                continue
            target = evidence.EFFORT_OUTPUT.get(level)
            # Unknown effort labels have no distance; choose the weaker result
            # on a tie rather than relying on benchmark or roster input order.
            distance = lambda effort: abs(math.log(target / evidence.EFFORT_OUTPUT[effort])) if (
                target and effort in evidence.EFFORT_OUTPUT) else math.inf
            sibling, rows = min(siblings, key=lambda pair: (distance(pair[0]), _fraction(pair[1][mapping]), pair[0]))
            rate, basis, ref = self._result(rows[mapping], sibling, "sibling_benchmark_estimate")
            x, sibling_x = _index(exact), _index(rows)
            if mapping == TERMINAL and self.fit and x is not None and sibling_x is not None:
                rate -= self.fit[2] * max(0., sibling_x - x)
                ref["adjustment"] = "aa_v5_terminal_fit"
            elif target and (sibling_effort := evidence.EFFORT_OUTPUT.get(sibling)):
                rate *= min(1., target / sibling_effort)
                ref["adjustment"] = "lower_effort_output_ratio"
            return max(0., rate), basis, ref
        if self.fit and (x := _index(exact)) is not None:
            xbar, ybar, slope, lo, hi, samples = self.fit
            # Cap above the validated range; below it do not award the lowest
            # observed talent level to an even lower index.
            rate = ybar + slope * (min(hi, x) - xbar)
            return max(0., min(1., rate)), "aa_v5_terminal_fit_estimate", {
                "source": "artificial_analysis", "metric": "artificial_analysis_intelligence_index",
                "target": "terminal_bench_4", "fit_samples": samples,
                "outside_fit_range": not lo <= x <= hi,
            }
        return self.low_rate, ("observed_low_benchmark_estimate" if self.has_rates else "no_benchmark_estimate"), None

    @staticmethod
    def _result(source, effort, basis):
        return _fraction(source), basis, {"effort": effort, **{
            k: source[k] for k in ("source", "metric", "value", "read_on", "url", "served_snapshot", "harness") if k in source
        }}
