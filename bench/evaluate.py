#!/usr/bin/env python3
"""Evaluate routing policies offline, with legacy or preregistered task splits.

Costs are API-equivalent USD, not subscription invoices. No token or quota
measurement is inferred. Bootstrap samples tasks with replacement, keeping all
policies paired. Random baselines and the cost-quality hull use fit data only.
"""

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import re
import statistics
import sys


class EvaluationError(ValueError):
    """Invalid input would make a comparison misleading."""


def task_match(task):
    if not isinstance(task, str):
        return None
    return (re.fullmatch(r"(-?\d+):(.+)-(\d+)", task) or
            re.fullmatch(r"(-?\d+):(history-fix)-([0-9a-f]{12})", task))


def number(value, label, optional=False):
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError("%s must be a number" % label)
    if not math.isfinite(value) or value < 0:
        raise EvaluationError("%s must be finite and nonnegative" % label)
    return float(value)


def read_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as exc:
        raise EvaluationError("Cannot read %s: %s" % (path, exc)) from exc


def percentile(values, probability):
    """Linear interpolation between adjacent sorted observations."""
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * probability
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def mcnemar_exact(left_only, right_only):
    """Two-sided exact binomial discordance test, stable for large counts."""
    n = left_only + right_only
    if n == 0:
        return 1.0
    k = min(left_only, right_only)
    log_pmf = (math.lgamma(n + 1) - math.lgamma(k + 1)
               - math.lgamma(n - k + 1) - n * math.log(2))
    relative_sum = term = 1.0
    for j in range(k, 0, -1):
        term *= j / (n - j + 1)
        relative_sum += term
        if term < relative_sum * 1e-16:
            break
    return min(1.0, 2 * math.exp(log_pmf) * relative_sum)


def load_options(option_doc):
    if not isinstance(option_doc, dict) or not isinstance(option_doc.get("options"), list):
        raise EvaluationError("Options must contain an options array")
    options = {}
    prices = option_doc.get("prices", {})
    if not isinstance(prices, dict):
        raise EvaluationError("options.prices must be an object")
    for option in option_doc["options"]:
        if not isinstance(option, dict) or not isinstance(option.get("id"), str):
            raise EvaluationError("Each option needs a string id")
        ident = option["id"]
        if not ident or ident in options:
            raise EvaluationError("Duplicate or empty option id: %s" % ident)
        option = dict(option)
        price = option.get("price_1m", prices.get(ident))
        if price is not None:
            if not isinstance(price, dict):
                raise EvaluationError("Price for %s must have in and out" % ident)
            rates = {key: number(price.get(key), "price %s.%s" % (ident, key), optional=True)
                     for key in ("in", "out")}
            option["price_1m"] = rates if None not in rates.values() else None
        pool = option.get("pool")
        if pool is not None and (not isinstance(pool, str) or not pool):
            raise EvaluationError("Option pool must be a nonempty string")
        options[ident] = option
    if not options:
        raise EvaluationError("The option grid is empty")
    return options


def load_inputs(results_path, policies_path, split):
    config = read_json(policies_path)
    if not isinstance(config, dict):
        raise EvaluationError("Policies must be a JSON object")
    base = Path(policies_path).resolve().parent
    if not isinstance(config.get("options_file"), str):
        raise EvaluationError("policies.options_file is required")
    options = load_options(read_json(base / config["options_file"]))
    seed_sets = {}
    for name in ("fit", "heldout"):
        seeds = config.get(name + "_seeds")
        if (not isinstance(seeds, list) or not seeds or
                any(isinstance(s, bool) or not isinstance(s, int) for s in seeds) or
                len(set(seeds)) != len(seeds)):
            raise EvaluationError("%s_seeds must be a nonempty list of unique integers" % name)
        seed_sets[name] = set(seeds)
    if seed_sets["fit"] & seed_sets["heldout"]:
        raise EvaluationError("Fit and heldout seeds overlap")
    rows, metadata = {}, {}
    task_digests, option_digests = {}, {}
    try:
        with Path(results_path).open(encoding="utf-8") as stream:
            for lineno, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise EvaluationError("Invalid result JSON at line %d" % lineno) from exc
                if not isinstance(row, dict):
                    raise EvaluationError("Result line %d is not an object" % lineno)
                if row.get("excluded"):
                    raise EvaluationError("Excluded measurement at line %d must be rerun" % lineno)
                task, option = row.get("task"), row.get("option")
                match = task_match(task)
                if not match or not isinstance(option, str) or option not in options:
                    raise EvaluationError("Invalid task or unknown option at line %d" % lineno)
                for field in ("model", "level"):
                    if row.get(field) is not None and row[field] != options[option].get(field):
                        raise EvaluationError("Result %s disagrees with option config at line %d" % (field, lineno))
                for field, key, seen in (("task_digest", task, task_digests),
                                         ("option_digest", option, option_digests)):
                    digest = row.get(field)
                    if digest is not None:
                        if not isinstance(digest, str) or not digest:
                            raise EvaluationError("%s must be a nonempty string" % field)
                        if key in seen and seen[key] != digest:
                            raise EvaluationError("Inconsistent %s for %s" % (field, key))
                        seen[key] = digest
                seed = row.get("seed")
                if (isinstance(seed, bool) or not isinstance(seed, int) or
                        seed != int(match.group(1)) or row.get("family") != match.group(2)):
                    raise EvaluationError("Inconsistent task metadata at line %d" % lineno)
                if row.get("difficulty") not in ("easy", "medium", "hard", "expert"):
                    raise EvaluationError("Invalid difficulty at line %d" % lineno)
                if not isinstance(row.get("pass"), bool):
                    raise EvaluationError("pass must be boolean at line %d" % lineno)
                if (isinstance(row.get("exit_code"), bool) or
                        not isinstance(row.get("exit_code"), int) or
                        not isinstance(row.get("reason"), str)):
                    raise EvaluationError("Result needs integer exit_code and string reason")
                number(row.get("duration_s"), "duration_s at line %d" % lineno)
                for key in ("tokens_in", "tokens_out", "pool_percent"):
                    number(row.get(key), key + " at line %d" % lineno, optional=True)
                if row.get("pool") is not None:
                    if not isinstance(row["pool"], str) or not row["pool"]:
                        raise EvaluationError("Result pool must be a nonempty string")
                    if options[option].get("pool") not in (None, row["pool"]):
                        raise EvaluationError("Result pool disagrees with option config")
                meta = (row["seed"], row["family"], row["difficulty"])
                if task in metadata and metadata[task] != meta:
                    raise EvaluationError("Inconsistent metadata for %s" % task)
                metadata[task] = meta
                cell = (task, option)
                if cell in rows and rows[cell] != row:
                    raise EvaluationError("Conflicting duplicate result for %s / %s" % cell)
                rows[cell] = row
    except OSError as exc:
        raise EvaluationError("Cannot read results: %s" % exc) from exc
    relevant_seeds = seed_sets["fit"] | seed_sets[split]
    expected = config.get("expected_task_ids")
    if expected is not None:
        if isinstance(expected, str):
            expected = read_json(base / expected)
        if (not isinstance(expected, list) or any(not isinstance(task, str) for task in expected) or
                len(set(expected)) != len(expected)):
            raise EvaluationError("expected_task_ids must be a list of unique task IDs or a JSON list path")
        expected_relevant = set()
        for task in expected:
            match = task_match(task)
            if not match:
                raise EvaluationError("Invalid expected task ID: %s" % task)
            if int(match.group(1)) in relevant_seeds:
                expected_relevant.add(task)
        observed_relevant = {task for task, meta in metadata.items() if meta[0] in relevant_seeds}
        if expected_relevant != observed_relevant:
            raise EvaluationError("Expected task set differs from measured tasks: %d absent, %d unexpected" %
                                  (len(expected_relevant - observed_relevant),
                                   len(observed_relevant - expected_relevant)))
    for seed in relevant_seeds:
        if not any(meta[0] == seed for meta in metadata.values()):
            raise EvaluationError("No measured tasks for configured seed %s" % seed)
    tasks = sorted(task for task, meta in metadata.items() if meta[0] in relevant_seeds)
    missing = [(task, option) for task in tasks for option in sorted(options)
               if (task, option) not in rows]
    if missing:
        raise EvaluationError("Incomplete paired grid: %d missing cells, first %s / %s" %
                              (len(missing), missing[0][0], missing[0][1]))
    return config, base, options, rows, metadata, seed_sets


def outcome(row, option):
    price = option.get("price_1m")
    measured = row.get("tokens_in") is not None and row.get("tokens_out") is not None
    cost = ((row["tokens_in"] * price["in"] + row["tokens_out"] * price["out"]) / 1e6
            if price is not None and measured else None)
    return {"option": row["option"], "pass": row["pass"], "cost_usd": cost,
            "latency_s": float(row["duration_s"]),
            "pool": row.get("pool") or option.get("pool"),
            "pool_percent": row.get("pool_percent")}


def mapping(value, base, label):
    if isinstance(value, str):
        value = read_json(base / value)
    elif isinstance(value, dict) and set(value) == {"mapping_file"}:
        value = read_json(base / value["mapping_file"])
    if not isinstance(value, dict):
        raise EvaluationError("%s must be a task-to-option object or JSON file path" % label)
    if "mapping" in value and isinstance(value["mapping"], dict):
        value = value["mapping"]
    return value


def random_choice(seed, label, task, choices):
    digest = hashlib.sha256((str(seed) + "|" + label + "|" + task).encode()).digest()
    return random.Random(int.from_bytes(digest, "big")).choice(choices)


def nondominated(points):
    """Keep all exact ties; dominate only with at least one strict improvement."""
    return sorted([point for point in points if not any(
        other["cost_per_task_usd"] <= point["cost_per_task_usd"] and
        other["pass_rate"] >= point["pass_rate"] and
        (other["cost_per_task_usd"] < point["cost_per_task_usd"] or
         other["pass_rate"] > point["pass_rate"]) for other in points)],
        key=lambda point: (point["cost_per_task_usd"], -point["pass_rate"], point["id"]))


def cost_quality_hull(points):
    """Upper concave envelope: remove points beaten by a mixture of two others."""
    frontier = nondominated(points)
    coordinates = {}
    for point in frontier:
        coordinates.setdefault((point["cost_per_task_usd"], point["pass_rate"]), []).append(point)
    hull = []
    for coordinate in sorted(coordinates):
        while len(hull) >= 2:
            a, b = hull[-2:]
            cross = ((b[0] - a[0]) * (coordinate[1] - b[1]) -
                     (b[1] - a[1]) * (coordinate[0] - b[0]))
            if cross < 0:
                break
            hull.pop()
        hull.append(coordinate)
    return [point for coordinate in hull for point in coordinates[coordinate]]


def build_policies(config, base, options, grid, fit_tasks, tasks, metadata):
    policies, selector_names, warnings = {}, [], []
    def add(name, choices):
        if name in policies:
            raise EvaluationError("Duplicate policy name %s" % name)
        for task in tasks:
            if (task not in choices or not isinstance(choices[task], str) or
                    choices[task] not in options):
                raise EvaluationError("Missing or unknown choice for %s in %s" % (task, name))
        policies[name] = {task: dict(grid[(task, choices[task])]) for task in tasks}
    def constant(name, option):
        add(name, {task: option for task in tasks})
    for name in ("always_max", "always_cheapest"):
        constant(name.replace("_", "-"), config.get(name))
    seats = config.get("fixed_seats")
    if not isinstance(seats, dict):
        raise EvaluationError("fixed_seats must map every task family to an option")
    add("fixed-seats", {task: seats.get(metadata[task][1]) for task in tasks})
    fit_rates = {option: statistics.mean(grid[(task, option)]["pass"] for task in fit_tasks)
                 for option in options}
    best = min(options, key=lambda option: (-fit_rates[option], option))
    constant("single-best", best)
    oracle = {}
    for task in tasks:
        candidates = [grid[(task, option)] for option in sorted(options)]
        candidates = [item for item in candidates if item["pass"]] or candidates
        known = all(item["cost_usd"] is not None for item in candidates)
        chosen = min(candidates, key=lambda item: (item["cost_usd"], item["option"])) if known else candidates[0]
        oracle[task] = dict(chosen, oracle_cost_known=known)
        if not known:
            oracle[task]["measured_cost_usd"] = chosen["cost_usd"]
            oracle[task]["cost_usd"] = None
    policies["oracle"] = oracle
    if "selector" in config:
        add("selector", mapping(config["selector"], base, "selector"))
        selector_names.append("selector")
    variants = config.get("selector_variants", {})
    if not isinstance(variants, dict):
        raise EvaluationError("selector_variants must map variant names to mappings")
    for name, value in sorted(variants.items()):
        ident = "selector:" + name
        add(ident, mapping(value, base, ident))
        selector_names.append(ident)
    scenarios = config.get("quota_scenarios", [])
    if not isinstance(scenarios, list):
        raise EvaluationError("quota_scenarios must be a list")
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str):
            raise EvaluationError("Each quota scenario needs an id")
        ident = "selector-quota:" + scenario["id"]
        add(ident, mapping(scenario.get("selector"), base, ident))
        selector_names.append(ident)
    policy_seed = config.get("policy_seed", 0)
    if isinstance(policy_seed, bool) or not isinstance(policy_seed, int):
        raise EvaluationError("policy_seed must be an integer")
    for option in sorted(options):
        constant("fixed-level:" + option, option)
    models = {}
    for option, info in sorted(options.items()):
        model = info.get("model", option)
        level = info.get("level", "default")
        if not isinstance(model, str) or not isinstance(level, str):
            raise EvaluationError("Option model and level must be strings")
        models.setdefault(model, {}).setdefault(level, option)
    for model, levels in sorted(models.items()):
        choices = sorted(levels.values())
        name = "random-level:" + model
        add(name, {task: random_choice(policy_seed, name, task, choices) for task in tasks})
    fit_points = []
    for option in sorted(options):
        costs = [grid[(task, option)]["cost_usd"] for task in fit_tasks]
        fit_points.append({"id": option, "pass_rate": fit_rates[option],
                           "cost_per_task_usd": statistics.mean(costs) if None not in costs else None})
    if all(point["cost_per_task_usd"] is not None for point in fit_points):
        hull = cost_quality_hull(fit_points)
        choices = sorted(point["id"] for point in hull)
        add("zero", {task: random_choice(policy_seed, "zero", task, choices) for task in tasks})
    else:
        hull = None
        warnings.append("Zero baseline unavailable: fit costs are incomplete; no hull was imputed.")
    return policies, selector_names, scenarios, best, hull, warnings


def mean_known(values):
    return statistics.mean(values) if values and None not in values else None


def metric_vectors(records, oracle, pools):
    return {
        "pass_rate": [float(item["pass"]) for item in records],
        "cost_per_task_usd": [item["cost_usd"] for item in records],
        "latency": [item["latency_s"] for item in records],
        "pass_regret": [float(bound["pass"]) - float(item["pass"])
                        for item, bound in zip(records, oracle)],
        "cost_delta_vs_oracle_usd": [item["cost_usd"] - bound["cost_usd"]
                                     if item["cost_usd"] is not None and bound["cost_usd"] is not None else None
                                     for item, bound in zip(records, oracle)],
        **{"pool_percent_used:" + pool: [
            item["pool_percent"] if item["pool"] == pool else
            (0.0 if item["pool"] is not None else None) for item in records] for pool in pools}}


def aggregate(vectors, indices=None):
    select = lambda values: values if indices is None else [values[i] for i in indices]
    metrics = {}
    for name, values in vectors.items():
        sampled = select(values)
        if name == "latency":
            if not sampled or None in values:
                metrics["median_latency_s"] = metrics["p90_latency_s"] = None
                continue
            ordered = sorted(sampled)
            n = len(ordered)
            metrics["median_latency_s"] = (ordered[n // 2] if n % 2 else
                                            (ordered[n // 2 - 1] + ordered[n // 2]) / 2)
            position = (n - 1) * .9
            lo, hi = math.floor(position), math.ceil(position)
            metrics["p90_latency_s"] = ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)
        elif name.startswith("pool_percent_used:"):
            metrics[name] = sum(sampled) if None not in values else None
        else:
            metrics[name] = sum(sampled) / len(sampled) if None not in values else None
    return metrics


def paired_statistics(vectors, tasks, seed, resamples):
    names = list(vectors)
    observed = {name: aggregate(values) for name, values in vectors.items()}
    # Store policy distributions once, then subtract matched resamples for pairs.
    distributions = {name: {key: [] for key, value in observed[name].items() if value is not None}
                     for name in names}
    rng = random.Random(seed)
    for _ in range(resamples):
        indices = [rng.randrange(len(tasks)) for _ in tasks]
        for name in names:
            for key, value in aggregate(vectors[name], indices).items():
                if key in distributions[name]:
                    distributions[name][key].append(value)
    comparisons = []
    for left, right in itertools.combinations(names, 2):
        differences = {}
        for key in observed[left]:
            a, b = observed[left][key], observed[right][key]
            samples = ([x - y for x, y in zip(distributions[left][key], distributions[right][key])]
                       if a is not None and b is not None else None)
            differences[key] = {"difference": a - b if samples is not None else None,
                                "ci95": [percentile(samples, .025), percentile(samples, .975)]
                                if samples is not None else None}
        left_pass, right_pass = vectors[left]["pass_rate"], vectors[right]["pass_rate"]
        left_only = sum(a == 1 and b == 0 for a, b in zip(left_pass, right_pass))
        right_only = sum(a == 0 and b == 1 for a, b in zip(left_pass, right_pass))
        comparisons.append({"left": left, "right": right, "differences": differences,
                            "mcnemar": {"left_only": left_only, "right_only": right_only,
                                         "p_exact": mcnemar_exact(left_only, right_only)}})
    return observed, comparisons


def adjust_selector_tests(comparisons, selector_names, number_variants):
    # One family includes every reported pair involving at least one selector.
    selected = [pair for pair in comparisons
                if pair["left"] in selector_names or pair["right"] in selector_names]
    multiplier = number_variants - len(selector_names)
    baseline_count = len({pair["left"] for pair in comparisons} |
                         {pair["right"] for pair in comparisons}) - len(selector_names)
    family_size = len(selected) + multiplier * baseline_count
    adjusted = 0.0
    for rank, pair in enumerate(sorted(selected, key=lambda item: item["mcnemar"]["p_exact"])):
        adjusted = max(adjusted, min(1.0, (family_size - rank) * pair["mcnemar"]["p_exact"]))
        pair["mcnemar"]["p_holm_selector"] = adjusted
    return family_size


def replay_quota(scenario, records, tasks):
    pools = scenario.get("pools")
    if not isinstance(pools, dict) or not pools:
        raise EvaluationError("Quota scenario pools must be a nonempty object")
    order = scenario.get("task_order", tasks)
    if not isinstance(order, list) or len(order) != len(tasks) or set(order) != set(tasks):
        raise EvaluationError("Scenario task_order must contain every evaluated task exactly once")
    report = {}
    by_task = dict(zip(tasks, records))
    for pool, settings in sorted(pools.items()):
        if not isinstance(pool, str) or not isinstance(settings, dict):
            raise EvaluationError("Quota pool settings must be objects")
        remaining = number(settings.get("remaining_percent"), "remaining_percent")
        reset = settings.get("reset_after_tasks")
        if reset is not None and (isinstance(reset, bool) or not isinstance(reset, int) or reset < 0):
            raise EvaluationError("reset_after_tasks must be a nonnegative integer")
        reset_allowance = number(settings.get("reset_allowance_percent", 100), "reset_allowance_percent")
        used, expired, overdrawn = 0.0, 0.0, 0.0
        known = True
        reset_happened = False
        for index in range(len(order) + 1):
            if reset == index:
                expired = max(remaining, 0) if known else None
                remaining = reset_allowance
                reset_happened = True
            if index == len(order):
                break
            item = by_task[order[index]]
            if item["pool"] is None or (item["pool"] == pool and item["pool_percent"] is None):
                known = False
            elif item["pool"] == pool:
                use = item["pool_percent"]
                used += use
                overdrawn += max(use - max(remaining, 0), 0)
                remaining -= use
        report[pool] = {"percent_used": used if known else None,
                        "expired_unspent_percent": expired if known else None,
                        "remaining_percent": max(remaining, 0) if known else None,
                        "overdrawn_percent": overdrawn if known else None,
                        "reset_observed": reset_happened, "telemetry_complete": known}
    return {"task_order": order, "pools": report,
            "note": "External choices replayed unchanged; quota overdraw is reported, not rerouted."}


def evaluate(results_path, policies_path, split="heldout", seed=None, resamples=10000,
             prereg=False, tasks_path=None, costs_path=None):
    config = read_json(policies_path)
    if not isinstance(config, dict):
        raise EvaluationError("Policies must be a JSON object")
    if prereg or config.get("split_method") == "task-name-sha256":
        return evaluate_prereg(results_path, policies_path, split, seed, resamples, tasks_path, costs_path)
    seed = 0 if seed is None else seed
    if isinstance(results_path, (list, tuple)):
        if len(results_path) != 1:
            raise EvaluationError("Multiple results files require prereg mode")
        results_path = results_path[0]
    if split not in ("fit", "heldout") or isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise EvaluationError("Use split fit/heldout and a positive integer resample count")
    config, base, options, rows, metadata, seed_sets = load_inputs(results_path, policies_path, split)
    fit_tasks = sorted(task for task, meta in metadata.items() if meta[0] in seed_sets["fit"])
    tasks = sorted(task for task, meta in metadata.items() if meta[0] in seed_sets[split])
    grid = {cell: outcome(row, options[cell[1]]) for cell, row in rows.items()}
    policies, selector_names, scenarios, best, fit_hull, warnings = build_policies(
        config, base, options, grid, fit_tasks, tasks, metadata)
    pools = sorted({item["pool"] for records in policies.values() for item in records.values()
                    if item["pool"] is not None})
    oracle = [policies["oracle"][task] for task in tasks]
    vectors = {name: metric_vectors([records[task] for task in tasks], oracle, pools)
               for name, records in policies.items()}
    for name, records in policies.items():
        vectors[name]["collapse_index"] = [float(records[task]["option"] == config["always_max"])
                                           for task in tasks]
    observed, comparisons = paired_statistics(vectors, tasks, seed, resamples)
    variants = config.get("number_variants", len(selector_names))
    if isinstance(variants, bool) or not isinstance(variants, int) or variants < len(selector_names):
        raise EvaluationError("number_variants must be an integer >= declared selector variants")
    family_size = adjust_selector_tests(comparisons, selector_names, variants)
    summaries = {}
    for name, records in policies.items():
        values = [records[task] for task in tasks]
        metrics = dict(observed[name])
        metrics["pool_percent_used"] = {pool: metrics.pop("pool_percent_used:" + pool) for pool in pools}
        metrics["tasks"] = len(tasks)
        metrics["cost_known_tasks"] = sum(item["cost_usd"] is not None for item in values)
        metrics["pool_usage_known_tasks"] = sum(item["pool"] is not None and item["pool_percent"] is not None
                                                for item in values)
        metrics["collapse_index"] = sum(item["option"] == config["always_max"] for item in values) / len(tasks)
        metrics["choices"] = {task: records[task]["option"] for task in tasks}
        if name == "oracle":
            metrics["cost_choice_unknown_tasks"] = [task for task in tasks if not records[task]["oracle_cost_known"]]
        summaries[name] = metrics
    points = [{"id": name, "cost_per_task_usd": values["cost_per_task_usd"], "pass_rate": values["pass_rate"]}
              for name, values in summaries.items() if values["cost_per_task_usd"] is not None and name != "oracle"]
    frontier = cost_quality_hull(points)
    distinct = {point["cost_per_task_usd"]: point["pass_rate"] for point in frontier}
    coordinates = sorted(distinct.items())
    area = (sum((b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(coordinates, coordinates[1:]))
            if len(coordinates) >= 2 else None)
    quota = {scenario["id"]: replay_quota(scenario,
             [policies["selector-quota:" + scenario["id"]][task] for task in tasks], tasks)
             for scenario in scenarios}
    if len(tasks) < 200:
        warnings.append("Small paired sample: quality differences may be poorly resolved; intervals are not evidence of equivalence.")
    if any(item["cost_usd"] is None for item in oracle):
        warnings.append("Oracle minimum costs are unknown where an eligible option lacks telemetry; cost regret is unknown.")
    return {"schema_version": 1, "split": split, "task_ids": tasks,
            "task_universe": "declared expected_task_ids" if config.get("expected_task_ids") is not None else
                             "union of observed tasks; entirely absent tasks cannot be detected without expected_task_ids",
            "fit_task_count": len(fit_tasks), "single_best_option": best,
            "bootstrap": {"seed": seed, "resamples": resamples, "method": "paired task percentile 95%"},
            "policy_seed": config.get("policy_seed", 0), "policies": summaries,
            "comparisons": comparisons, "fit_option_hull": fit_hull,
            "cost_quality": {"frontier": frontier, "area_usd_pass_rate": area,
                             "cost_range_usd": [coordinates[0][0], coordinates[-1][0]] if coordinates else None,
                             "excluded_unknown_cost_policies": [name for name, values in summaries.items()
                                                                 if values["cost_per_task_usd"] is None],
                             "definition": "Area under the upper concave cost-quality envelope of observed non-oracle policies, including achievable mixtures, only over its observed cost range; descriptive, no extrapolation."},
            "selector_multiplicity": {"declared_variants": len(selector_names), "number_variants": variants,
                                      "holm_family_size": family_size,
                                      "definition": "Holm correction over all reported selector-involving pairs; undeclared tried variants add one hypothesis per non-selector baseline at p=1."},
            "quota_scenarios": quota, "warnings": warnings,
            "definitions": {"pass_regret": "Oracle pass minus policy pass, averaged over tasks.",
                            "cost_delta_vs_oracle_usd": "Policy cost minus oracle minimum cost; may be negative for a cheaper failure. Unknown if any paired cost is unknown.",
                            "collapse_index": "Fraction of task choices equal to configured always_max option.",
                            "cost_per_task_usd": "Mean API-equivalent USD, unknown unless every selected task has both token counts and prices.",
                            "pool_percent_used": "Sum of measured percent of each pool's full allowance, separately by pool; unknown if relevant telemetry is incomplete.",
                            "random_baselines": "Uniform available levels per model, canonical lowest option ID within each level. Zero uniformly mixes upper concave fit cost-quality hull options. SHA256-seeded per task, label, and policy_seed.",
                            "single_best": "Highest fit pass rate; ties choose lexicographically lowest option ID.",
                            "oracle": "Minimum measured cost among passing options, or all options if none pass; ties use option ID. If any eligible cost is unknown, choose lowest ID for pass/latency lookup and mark the oracle cost unknown.",
                            "difference_direction": "Every paired difference is left minus right; percentile interpolation is linear."}}


def split_api():
    try:
        from bench.split import task_split, FAMILY_MAP
    except ModuleNotFoundError:
        from split import task_split, FAMILY_MAP
    return task_split, FAMILY_MAP


def missing_outcome(option=None, reason="missing_measurement"):
    return {"option": option, "pass": None, "cost_usd": None, "latency_s": None,
            "pool": None, "pool_percent": None, "missing_reason": reason}


def load_prereg_inputs(results_paths, config, base, tasks_path, costs_path):
    task_split, family_map = split_api()
    files = config.get("options_files", [config.get("options_file")])
    if not isinstance(files, list) or not files or any(not isinstance(p, str) for p in files):
        raise EvaluationError("prereg needs options_file or options_files")
    options = {}
    for filename in files:
        for ident, option in load_options(read_json(base / filename)).items():
            if ident in options and options[ident] != option:
                raise EvaluationError("Conflicting option config: " + ident)
            options[ident] = option
    try:
        from bench.split import load_exclusions, task_folder
    except ModuleNotFoundError:
        from split import load_exclusions, task_folder
    paths = results_paths if isinstance(results_paths, (list, tuple)) else [results_paths]
    explicit_exclusions = config.get("exclude_tasks_file")
    if explicit_exclusions is not None and not isinstance(explicit_exclusions, str):
        raise EvaluationError("exclude_tasks_file must be a TXT path")
    exclusions = load_exclusions(paths)
    if explicit_exclusions:
        exclusions |= load_exclusions(paths, base / explicit_exclusions)
    excluded_ids = set()
    metadata, rows, digests = {}, {}, {}
    if tasks_path is not None:
        for path in sorted(Path(tasks_path).rglob("meta.json")):
            if path.parent.name in exclusions:
                continue
            meta = read_json(path)
            task = "%s:%s" % (meta.get("seed"), path.parent.name)
            if task in metadata:
                raise EvaluationError("Duplicate task fixture: " + task)
            metadata[task] = (meta.get("seed"), meta.get("family"), meta.get("difficulty"))
        if not metadata:
            raise EvaluationError("No task metadata under --tasks")
    for path in paths:
        try:
            with Path(path).open(encoding="utf-8") as stream:
                for lineno, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError as exc:
                        raise EvaluationError("Invalid result JSON at %s:%d" % (path, lineno)) from exc
                    if not isinstance(row, dict):
                        raise EvaluationError("Result must be an object")
                    task, option = row.get("task"), row.get("option")
                    if isinstance(task, str) and task_folder(task) in exclusions:
                        excluded_ids.add(task)
                        continue
                    if not isinstance(task, str) or ":" not in task or not isinstance(option, str) or option not in options:
                        raise EvaluationError("Invalid task or unknown option")
                    if row.get("excluded"):
                        raise EvaluationError("Excluded measurement must be rerun")
                    meta = (row.get("seed"), row.get("family"), row.get("difficulty"))
                    if (type(meta[0]) is not int or task.split(":", 1)[0] != str(meta[0]) or
                            meta[1] not in family_map or meta[2] not in ("easy", "medium", "hard", "expert")):
                        raise EvaluationError("Inconsistent task metadata: " + task)
                    if task in metadata and metadata[task] != meta:
                        raise EvaluationError("Inconsistent metadata for " + task)
                    if tasks_path is not None and task not in metadata:
                        raise EvaluationError("Unexpected task outside frozen --tasks: " + task)
                    metadata[task] = meta
                    if type(row.get("pass")) is not bool:
                        raise EvaluationError("pass must be boolean")
                    if type(row.get("exit_code")) is not int or not isinstance(row.get("reason"), str):
                        raise EvaluationError("Result needs integer exit_code and string reason")
                    number(row.get("duration_s"), "duration_s")
                    for field in ("tokens_in", "tokens_out", "pool_percent"):
                        number(row.get(field), field, optional=True)
                    for field in ("model", "level", "pool"):
                        if row.get(field) is not None and options[option].get(field) not in (None, row[field]):
                            raise EvaluationError("Result %s disagrees with option config" % field)
                    for field, key in (("task_digest", task), ("option_digest", option)):
                        value = row.get(field)
                        if value is not None:
                            if not isinstance(value, str) or not value:
                                raise EvaluationError(field + " must be a nonempty string")
                            digest_key = (field, key)
                            if digest_key in digests and digests[digest_key] != value:
                                raise EvaluationError("Inconsistent " + field + " for " + key)
                            digests[digest_key] = value
                    cell = (task, option)
                    if cell in rows and rows[cell] != row:
                        raise EvaluationError("Conflicting duplicate result for %s / %s" % cell)
                    rows[cell] = row
        except OSError as exc:
            raise EvaluationError("Cannot read results: %s" % exc) from exc
    expected = config.get("expected_task_ids")
    if isinstance(expected, str):
        expected = read_json(base / expected)
    if expected is not None:
        if (not isinstance(expected, list) or any(not isinstance(t, str) for t in expected) or
                len(set(expected)) != len(expected)):
            raise EvaluationError("expected_task_ids must be unique strings")
        excluded_ids.update(task for task in expected if task_folder(task) in exclusions)
        expected = [task for task in expected if task_folder(task) not in exclusions]
        if set(metadata) - set(expected):
            raise EvaluationError("Unexpected tasks outside expected_task_ids")
        for task in expected:
            if task not in metadata:
                family = next((f for f in sorted(family_map, key=len, reverse=True)
                               if re.search(r"(?:^|[-:])" + re.escape(f) + r"-", task)), None)
                if family is None or ":" not in task:
                    raise EvaluationError("Absent task family unknown; supply --tasks: " + task)
                try:
                    seed = int(task.split(":", 1)[0])
                except ValueError as exc:
                    raise EvaluationError("Invalid expected task: " + task) from exc
                metadata[task] = (seed, family, None)
    if not metadata:
        raise EvaluationError("No tasks to evaluate")
    for task, meta in metadata.items():
        if meta[1] not in family_map:
            raise EvaluationError("Unknown task family: " + str(meta[1]))
        try:
            task_split(task)
        except ValueError as exc:
            raise EvaluationError("Invalid task ID: " + task) from exc
    grid = {cell: outcome(row, options[cell[1]]) for cell, row in rows.items()}
    cost_file = costs_path or config.get("costs_file")
    cost_source, cost_details = None, []
    if cost_file:
        cost_file = Path(cost_file) if costs_path else base / cost_file
        doc = read_json(cost_file)
        if doc.get("schema") != "crossfeed-quota-cost/v1" or not isinstance(doc.get("options"), list):
            raise EvaluationError("costs must use crossfeed-quota-cost/v1")
        costs = {}
        cost_details = doc["options"]
        for item in cost_details:
            if not isinstance(item, dict):
                raise EvaluationError("Quota cost option must be an object")
            key = (item.get("option"), item.get("pool"))
            if key in costs:
                raise EvaluationError("Duplicate quota cost option/pool")
            costs[key] = number(item.get("percent_binding_window_per_task"), "quota cost", optional=True)
        for cell, item in grid.items():
            key = (cell[1], item["pool"])
            # These are measured batch averages, never derived from token counts.
            item["pool_percent"] = costs.get(key)
        cost_source = str(cost_file)
    exclusion_report = {"excluded_task_folders": sorted(exclusions), "excluded_task_ids": sorted(excluded_ids),
                        "rationale": "Uniform PREREG task exclusions apply before metadata/outcome validation, to every fit and held-out policy; excluded harness errors are not failures."}
    return options, grid, metadata, cost_source, cost_details, exclusion_report


def partial_vectors(records, oracle, pools):
    vectors = metric_vectors([dict(item, **{"pass": False}) if item["pass"] is None else item
                              for item in records],
                             [dict(item, **{"pass": False}) if item["pass"] is None else item
                              for item in oracle], pools)
    vectors["pass_rate"] = [float(item["pass"]) if item["pass"] is not None else None for item in records]
    vectors["pass_regret"] = [float(bound["pass"]) - float(item["pass"])
                              if item["pass"] is not None and bound["pass"] is not None else None
                              for item, bound in zip(records, oracle)]
    for pool in pools:
        vectors["pool_percent_per_task:" + pool] = list(vectors["pool_percent_used:" + pool])
    return vectors


def partial_aggregate(vectors):
    metrics = {}
    for key, values in vectors.items():
        if key == "latency":
            metrics["median_latency_s"] = percentile(values, .5) if values and None not in values else None
            metrics["p90_latency_s"] = percentile(values, .9) if values and None not in values else None
        elif key.startswith("pool_percent_used:"):
            metrics[key] = sum(values) if values and None not in values else None
        else:
            metrics[key] = mean_known(values)
    return metrics


def partial_paired_statistics(vectors, tasks, resamples):
    observed = {name: partial_aggregate(values) for name, values in vectors.items()}
    cohorts = {}
    for left, right in itertools.combinations(vectors, 2):
        indices = tuple(i for i in range(len(tasks))
                        if vectors[left]["pass_rate"][i] is not None and vectors[right]["pass_rate"][i] is not None)
        cohorts.setdefault(indices, []).append((left, right))
    by_pair = {}
    for indices, requests in cohorts.items():
        common = [tasks[i] for i in indices]
        if common:
            names = list(dict.fromkeys(name for pair in requests for name in pair))
            subset = {name: {key: [values[i] for i in indices] for key, values in vectors[name].items()}
                      for name in names}
            _, comparisons = paired_statistics(subset, common, 7, resamples)
            results = {(p["left"], p["right"]): p for p in comparisons}
        for left, right in requests:
            if common:
                pair = results.get((left, right))
                if pair is None:
                    reverse = results[(right, left)]
                    pair = {"left": left, "right": right,
                            "differences": {key: {"difference": -v["difference"] if v["difference"] is not None else None,
                                                  "ci95": [-v["ci95"][1], -v["ci95"][0]] if v["ci95"] else None}
                                            for key, v in reverse["differences"].items()},
                            "mcnemar": {"left_only": reverse["mcnemar"]["right_only"],
                                         "right_only": reverse["mcnemar"]["left_only"],
                                         "p_exact": reverse["mcnemar"]["p_exact"]}}
            else:
                pair = {"left": left, "right": right,
                        "differences": {key: {"difference": None, "ci95": None} for key in observed[left]},
                        "mcnemar": {"left_only": 0, "right_only": 0, "p_exact": None}}
            pair.update(paired_task_count=len(common), paired_task_ids=common,
                        missing_task_ids=[task for task in tasks if task not in common],
                        scope="full held-out" if len(common) == len(tasks) else "measured paired subset, exploratory")
            by_pair[(left, right)] = pair
    return observed, [by_pair[pair] for pair in itertools.combinations(vectors, 2)]


def evaluate_prereg(results_paths, policies_path, split, seed, resamples, tasks_path=None, costs_path=None):
    if split != "heldout":
        raise EvaluationError("PREREG reports held-out only")
    if seed not in (None, 7):
        raise EvaluationError("PREREG bootstrap seed is fixed at 7")
    if type(resamples) is not int or resamples < 1:
        raise EvaluationError("Use a positive integer resample count")
    config = read_json(policies_path)
    if config.get("policy_seed", 7) != 7:
        raise EvaluationError("PREREG policy seed is fixed at 7")
    base = Path(policies_path).resolve().parent
    task_split, family_map = split_api()
    options, grid, metadata, cost_source, cost_details, exclusion_report = load_prereg_inputs(results_paths, config, base, tasks_path, costs_path)
    fit = sorted(task for task in metadata if task_split(task) == "fit")
    heldout_universe = sorted(task for task in metadata if task_split(task) == "heldout")
    subset = config.get("heldout_task_ids")
    if isinstance(subset, str):
        subset = read_json(base / subset)
    if subset is not None:
        if (not isinstance(subset, list) or not subset or
                any(not isinstance(task, str) for task in subset) or len(set(subset)) != len(subset)):
            raise EvaluationError("heldout_task_ids must be a nonempty list of unique task IDs or a JSON list path")
        try:
            from bench.split import task_folder
        except ModuleNotFoundError:
            from split import task_folder
        subset = [task for task in subset if task_folder(task) not in exclusion_report["excluded_task_folders"]]
        unknown = set(subset) - set(metadata)
        if unknown:
            raise EvaluationError("Unknown heldout_task_ids: " + sorted(unknown)[0])
        if any(task_split(task) != "heldout" for task in subset):
            raise EvaluationError("heldout_task_ids may contain only held-out tasks")
    tasks = sorted(subset) if subset is not None else heldout_universe
    if not fit or not tasks:
        raise EvaluationError("PREREG needs fit and held-out tasks in the declared universe")
    policies, selectors, warnings = {}, [], []
    def add(name, choices):
        records = {}
        for task in tasks:
            option = choices.get(task)
            if option is not None and (not isinstance(option, str) or option not in options):
                raise EvaluationError("Unknown choice for %s in %s" % (task, name))
            records[task] = dict(grid.get((task, option), missing_outcome(option,
                                "unmeasured_choice" if option is None else "missing_measurement")))
        policies[name] = records
    def constant(name, option):
        add(name, dict.fromkeys(tasks, option))
    for key, ident in (("always_max", "opus55-xhigh"), ("always_cheapest", "ds41-flash-high")):
        if config.get(key, ident) != ident:
            raise EvaluationError("PREREG %s is fixed at %s" % (key, ident))
        if ident not in options:
            raise EvaluationError("PREREG option missing from options config: " + ident)
        constant(key.replace("_", "-"), ident)
    seats = {"coding-agent": "sol-high", "review": "sol-xhigh", "repo-qa": "ds41-flash-high",
             "reasoning": "ds41-flash-high", "extraction": "ds41-flash-high"}
    overrides = config.get("fixed_seats", {})
    if not isinstance(overrides, dict):
        raise EvaluationError("fixed_seats must be a family-to-option mapping")
    for family, default in seats.items():
        if family in ("coding-agent", "review", "repo-qa") and overrides.get(family, default) != default:
            raise EvaluationError("PREREG fixed seat cannot change for " + family)
    for family, option in overrides.items():
        normalized = family_map.get(family, family)
        if normalized in ("coding-agent", "review", "repo-qa") and option != seats[normalized]:
            raise EvaluationError("PREREG fixed seat cannot change for " + family)
        seats[normalized] = option
    add("fixed-seats", {task: seats[family_map[metadata[task][1]]] for task in tasks})
    warnings.append("Fixed seats for reasoning/extraction default to ds41-flash-high; PREREG leaves these unspecified. Override fixed_seats before comparison.")
    complete_fit = [option for option in options if all((task, option) in grid for task in fit)]
    rates = {option: statistics.mean(grid[(task, option)]["pass"] for task in fit) for option in complete_fit}
    best = min(complete_fit, key=lambda option: (-rates[option], option)) if complete_fit else None
    constant("single-best", best)
    if len(complete_fit) != len(options):
        warnings.append("Single-best is provisional: only options with complete fit coverage are eligible; missing fit cells are not failures.")
    for option in sorted(options):
        constant("fixed-level:" + option, option)
    try:
        from bench.to_evidence import model_key
    except ModuleNotFoundError:
        from to_evidence import model_key
    models = {}
    for option, info in sorted(options.items()):
        model, level = info.get("model", option), info.get("level", "default")
        if not isinstance(model, str) or not isinstance(level, str):
            raise EvaluationError("Option model and level must be strings")
        canonical = info.get("model_key", model_key(model, level))
        if not isinstance(canonical, str) or not canonical:
            raise EvaluationError("Option model_key must be a nonempty string")
        models.setdefault(canonical, {}).setdefault(level, option)
    for model, levels in sorted(models.items()):
        name = "random-level:" + model
        add(name, {task: random_choice(7, name, task, sorted(levels.values())) for task in tasks})
    points = [{"id": option, "pass_rate": rates[option],
               "cost_per_task_usd": mean_known([grid[(task, option)]["cost_usd"] for task in fit])}
              for option in sorted(complete_fit)]
    hull = None
    if len(points) == len(options) and all(point["cost_per_task_usd"] is not None for point in points):
        hull = cost_quality_hull(points)
        add("zero", {task: random_choice(7, "zero", task, sorted(p["id"] for p in hull)) for task in tasks})
    else:
        constant("zero", None)
        warnings.append("Zero baseline unavailable: complete fit measurements and fit USD costs are required; no hull was imputed.")
    for name, value in [("selector", config["selector"])] if "selector" in config else []:
        add(name, mapping(value, base, name))
        selectors.append(name)
    variants = config.get("selector_variants", {})
    if not isinstance(variants, dict):
        raise EvaluationError("selector_variants must be an object")
    for name, value in sorted(variants.items()):
        ident = "selector:" + name
        add(ident, mapping(value, base, ident))
        selectors.append(ident)
    oracle = {}
    for task in tasks:
        measured = [grid[(task, option)] for option in sorted(options) if (task, option) in grid]
        passing = [item for item in measured if item["pass"]]
        complete = len(measured) == len(options)
        candidates = passing or measured
        known = complete and all(item["cost_usd"] is not None for item in candidates)
        if candidates:
            chosen = min(candidates, key=lambda item: (item["cost_usd"], item["option"])) if known else candidates[0]
            bound = dict(chosen)
            bound["pass"] = True if passing else (False if complete else None)
            if not known:
                bound["cost_usd"] = None
                bound["latency_s"] = None
            if not complete or not known:
                bound["pool"], bound["pool_percent"] = None, None
        else:
            bound = missing_outcome()
        bound["oracle_cost_known"] = known
        oracle[task] = bound
    policies["oracle"] = oracle
    pools = sorted({info["pool"] for info in options.values() if info.get("pool") is not None})
    oracle_values = [oracle[task] for task in tasks]
    vectors = {name: partial_vectors([records[task] for task in tasks], oracle_values, pools)
               for name, records in policies.items()}
    for name, records in policies.items():
        vectors[name]["collapse_index"] = [float(records[task]["option"] == "opus55-xhigh")
                                           if records[task]["option"] is not None else None for task in tasks]
    observed, comparisons = partial_paired_statistics(vectors, tasks, resamples)
    variants_count = config.get("number_variants", len(selectors))
    if type(variants_count) is not int or variants_count < len(selectors):
        raise EvaluationError("number_variants must be an integer >= declared selector variants")
    # Missing comparisons carry no statistical evidence; use p=1 for multiplicity only.
    unavailable = [pair for pair in comparisons if pair["mcnemar"]["p_exact"] is None]
    for pair in unavailable:
        pair["mcnemar"]["p_exact"] = 1.0
    family_size = adjust_selector_tests(comparisons, selectors, variants_count)
    for pair in unavailable:
        pair["mcnemar"]["p_exact"] = None
        if "p_holm_selector" in pair["mcnemar"]:
            pair["mcnemar"]["p_holm_selector"] = None
    summaries = {}
    for name, records in policies.items():
        values = [records[task] for task in tasks]
        metrics = dict(observed[name])
        metrics["pool_percent_used"] = {pool: metrics.pop("pool_percent_used:" + pool) for pool in pools}
        metrics["pool_percent_per_task"] = {pool: metrics.pop("pool_percent_per_task:" + pool) for pool in pools}
        measured = [i for i, item in enumerate(values) if item["pass"] is not None]
        metrics.update(tasks=len(tasks), measured_tasks=len(measured),
                       missing_task_ids=[tasks[i] for i in range(len(tasks)) if i not in measured],
                       choices={task: records[task]["option"] for task in tasks},
                       missing_measurements={task: records[task].get("missing_reason", "incomplete_oracle_grid")
                                             for task in tasks if records[task]["pass"] is None},
                       cost_known_tasks=sum(item["cost_usd"] is not None for item in values),
                       pool_usage_known_tasks=sum(item["pool"] is not None and item["pool_percent"] is not None for item in values),
                       measured_subset={"task_ids": [tasks[i] for i in measured],
                                        "metrics": partial_aggregate({key: [value[i] for i in measured]
                                                                      for key, value in vectors[name].items()}),
                                        "scope": "exploratory; missing cells can bias this subset"})
        if name == "oracle":
            metrics["cost_choice_unknown_tasks"] = [task for task in tasks if not records[task]["oracle_cost_known"]]
        summaries[name] = metrics
    frontier = cost_quality_hull([{"id": name, "cost_per_task_usd": v["cost_per_task_usd"], "pass_rate": v["pass_rate"]}
                                 for name, v in summaries.items() if name != "oracle" and
                                 v["cost_per_task_usd"] is not None and v["pass_rate"] is not None])
    coords = sorted({(p["cost_per_task_usd"], p["pass_rate"]) for p in frontier})
    area = sum((b[0]-a[0])*(a[1]+b[1])/2 for a,b in zip(coords, coords[1:])) if len(coords) >= 2 else None
    missing = [{"task": task, "option": option} for task in sorted(set(fit) | set(tasks)) for option in sorted(options)
               if (task, option) not in grid]
    return {"schema_version": 2, "mode": "prereg", "exclusions": exclusion_report, "split": "heldout", "split_method": "task-name-sha256",
            "task_ids": tasks, "fit_task_count": len(fit), "fit_task_ids": fit,
            "heldout_universe_task_count": len(heldout_universe), "heldout_subset_declared": subset is not None,
            "task_universe": "frozen tasks directory" if tasks_path else ("declared expected_task_ids" if config.get("expected_task_ids") else "observed union; wholly absent tasks cannot be detected"),
            "single_best_option": best, "single_best_eligible_options": sorted(complete_fit),
            "single_best_provisional": len(complete_fit) != len(options), "fixed_seats": seats,
            "bootstrap": {"seed": 7, "resamples": resamples, "method": "paired task percentile 95%"},
            "policy_seed": 7, "policies": summaries, "comparisons": comparisons, "fit_option_hull": hull,
            "missing_grid_cells": missing, "quota_cost_source": cost_source, "quota_cost_options": cost_details,
            "cost_quality": {"frontier": frontier, "area_usd_pass_rate": area,
                             "cost_range_usd": [coords[0][0], coords[-1][0]] if coords else None,
                             "definition": "Upper cost-quality envelope of fully measured non-oracle policies on all held-out tasks; no extrapolation."},
            "selector_multiplicity": {"declared_variants": len(selectors), "number_variants": variants_count,
                                      "holm_family_size": family_size},
            "quota_scenarios": {},
            "definitions": {"missing_scores": "Full-universe metrics are unknown when required selected measurements are missing. measured_subset is exploratory.",
                            "paired_comparisons": "Task intersection with measured outcomes for both policies. Missing telemetry makes that metric unknown; coverage is explicit.",
                            "oracle": "Pass upper bound is true when any measured option passes; otherwise unknown until every option is measured. Cheapest cost and its latency are unknown until all eligible options are measured with known costs.",
                            "cost_per_task_usd": "Mean API-equivalent token cost, independent of subscription quota cost.",
                            "pool_percent_per_task": "Mean measured per-pool debit, including zero on tasks routed to another known pool. Cost-report values are measured batch averages; shared pools add noise.",
                            "single_best": "Highest fit pass rate among options with complete fit coverage; lexicographic tie-break. Provisional until all options are eligible.",
                            "difference_direction": "Left minus right; 95 percent paired task bootstrap intervals."},
            "warnings": warnings + (["Partial grid: full-universe metrics remain unknown; paired subsets are exploratory."] if missing else [])}


def cost_quality_svg(summary):
    """Standalone, dependency-free SVG; incomplete policies never enter the curve."""
    from html import escape
    points = [{"id": name, "cost_per_task_usd": p["cost_per_task_usd"], "pass_rate": p["pass_rate"]}
              for name, p in summary["policies"].items() if name != "oracle" and
              p["cost_per_task_usd"] is not None and p["pass_rate"] is not None]
    frontier = summary["cost_quality"]["frontier"]
    maximum = max((p["cost_per_task_usd"] for p in points), default=1) or 1
    x = lambda value: 80 + 660 * value / maximum
    y = lambda value: 360 - 280 * value
    lines = ['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="430" viewBox="0 0 800 430" role="img">',
             '<title>Held-out policy cost and quality</title>',
             '<desc>Complete held-out policy measurements only. Costs are API-equivalent USD per task.</desc>',
             '<rect width="800" height="430" fill="white"/>',
             '<g font-family="sans-serif" font-size="12" fill="#222">',
             '<text x="80" y="32" font-size="20">Policy cost and quality (%s)</text>' % escape(summary["split"]),
             '<path d="M80 80 V360 H740" fill="none" stroke="#444"/>',
             '<text x="320" y="415">API-equivalent USD / task</text>',
             '<text x="12" y="65">Pass rate</text>']
    for rate in (0, .25, .5, .75, 1):
        lines.append('<text x="40" y="%.1f">%.0f%%</text>' % (y(rate)+4, rate*100))
    for fraction in (0, .25, .5, .75, 1):
        lines.append('<text x="%.1f" y="385">%.5g</text>' % (x(maximum*fraction), maximum*fraction))
    if frontier:
        lines.append('<polyline points="%s" fill="none" stroke="#166534" stroke-width="2"/>' %
                     " ".join('%.2f,%.2f' % (x(p["cost_per_task_usd"]), y(p["pass_rate"])) for p in frontier))
    for p in points:
        lines.append('<circle cx="%.2f" cy="%.2f" r="4" fill="#2563eb"><title>%s: pass %.4f, USD/task %.6g</title></circle>' %
                     (x(p["cost_per_task_usd"]), y(p["pass_rate"]), escape(p["id"]), p["pass_rate"], p["cost_per_task_usd"]))
    if not points:
        lines.append('<text x="200" y="200">No complete held-out cost and quality measurements.</text>')
    lines += ['</g>', '</svg>', '']
    return "\n".join(lines)


def markdown(summary):
    def fmt(value, precision=4):
        return "unknown" if value is None else ("%.*f" % (precision, value))
    lines = ["# Paired policy evaluation (%s)" % summary["split"], "",
             "%d tasks; single-best fit option: `%s`. Bootstrap: %d resamples, seed %s." %
             (len(summary["task_ids"]), summary["single_best_option"], summary["bootstrap"]["resamples"], summary["bootstrap"]["seed"]), "",
             "| Policy | Pass rate | USD/task | Median s | P90 s | Pass regret | Max share |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, values in summary["policies"].items():
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" %
                     (name.replace("|", "\\|"), fmt(values["pass_rate"]), fmt(values["cost_per_task_usd"], 6),
                      fmt(values["median_latency_s"], 2), fmt(values["p90_latency_s"], 2),
                      fmt(values["pass_regret"]), fmt(values["collapse_index"])))
    if summary.get("mode") == "prereg":
        lines += ["", "Coverage (measured / declared held-out):", ""]
        lines += ["- %s: %d / %d" % (name, values["measured_tasks"], values["tasks"])
                  for name, values in summary["policies"].items()]
        lines += ["", "Full-universe scores stay unknown for incomplete policies. Paired subset coverage is in JSON; subset comparisons are exploratory."]
    lines += ["", "All differences below are left minus right. Costs are API-equivalent USD.", "",
              "| Pair | Pass delta [95% CI] | USD/task delta [95% CI] | McNemar p | Selector Holm p |",
              "|---|---:|---:|---:|---:|"]
    def interval(value):
        return "unknown" if value["ci95"] is None else "%s [%s, %s]" % (
            fmt(value["difference"], 6), fmt(value["ci95"][0], 6), fmt(value["ci95"][1], 6))
    for pair in summary["comparisons"]:
        lines.append("| %s vs %s | %s | %s | %s | %s |" % (
            pair["left"].replace("|", "\\|"), pair["right"].replace("|", "\\|"),
            interval(pair["differences"]["pass_rate"]), interval(pair["differences"]["cost_per_task_usd"]),
            fmt(pair["mcnemar"]["p_exact"], 6), fmt(pair["mcnemar"].get("p_holm_selector"), 6)))
    lines += ["", "Full latency, regret and per-pool paired intervals are in the JSON summary.",
              "Cost-quality area: %s USD × pass rate over the observed frontier range only." %
              fmt(summary["cost_quality"]["area_usd_pass_rate"], 6)]
    for name, quota in summary["quota_scenarios"].items():
        for pool, values in quota["pools"].items():
            lines.append("Quota %s / %s: used %s%%, expired unspent %s%%, overdrawn %s%%." %
                         (name, pool, fmt(values["percent_used"]), fmt(values["expired_unspent_percent"]),
                          fmt(values["overdrawn_percent"])))
    lines += ["", *["- " + warning for warning in summary["warnings"]]]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="+")
    parser.add_argument("--policies", required=True, type=Path)
    parser.add_argument("--split", required=True, choices=("fit", "heldout"))
    parser.add_argument("--json-out", help="Default: RESULTS.summary.json; '-' prints delimited JSON after Markdown")
    parser.add_argument("--seed", type=int, help="Paired-bootstrap seed: legacy 0, prereg fixed 7")
    parser.add_argument("--prereg", action="store_true", help="Task-name hash split, held-out only, partial-grid reporting")
    parser.add_argument("--tasks", type=Path, help="Frozen task directory for the full preregistered universe")
    parser.add_argument("--costs", type=Path, help="Measured crossfeed-quota-cost/v1 JSON")
    parser.add_argument("--svg-out", type=Path, help="Write standalone cost-quality curve SVG")
    parser.add_argument("--resamples", type=int, default=10000)
    args = parser.parse_args(argv)
    try:
        summary = evaluate(args.results, args.policies, args.split, args.seed, args.resamples,
                           args.prereg, args.tasks, args.costs)
        if args.svg_out:
            args.svg_out.write_text(cost_quality_svg(summary), encoding="utf-8")
        encoded = json.dumps(summary, indent=2, sort_keys=True, allow_nan=False)
        print(markdown(summary))
        if args.json_out == "-":
            print("\n--- BEGIN JSON SUMMARY ---\n" + encoded + "\n--- END JSON SUMMARY ---")
        else:
            target = Path(args.json_out) if args.json_out else args.results[0].with_suffix(".summary.json")
            target.write_text(encoded + "\n", encoding="utf-8")
            print("\nJSON summary: %s" % target)
    except (EvaluationError, OSError) as exc:
        parser.exit(2, "Evaluation error: %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
