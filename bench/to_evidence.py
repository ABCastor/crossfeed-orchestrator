#!/usr/bin/env python3
"""Replace local selector evidence with the PREREG fit half only."""
import argparse
import copy
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench.split import FAMILY_MAP, iter_split_rows, task_split, task_folder, load_exclusions
from bench.cost import report_rows


def model_key(model, level=None):
    # Grid's Go model is a provider-qualified selector; evidence uses its key.
    key = model.split("/", 1)[-1]
    if key.startswith("gemini-") and level and key.endswith("-" + level):
        key = key[:-(len(level) + 1)]
    return key


def fit_costs(paths, rows):
    """Never attribute a mixed fit/heldout quota batch to its fit subset.

    Snapshots debit whole option batches. Without a fit-only batch manifest,
    any file containing heldout IDs is unsafe for automatic cost estimation.
    Only task IDs of rejected cells are inspected, never their payload.
    """
    mixed = False
    excluded = load_exclusions(paths)
    for path in paths:
        with Path(path).open() as stream:
            for line in stream:
                if line.strip() and (task_split(json.loads(line)["task"]) != "fit"
                                     or task_folder(json.loads(line)["task"]) in excluded):
                    mixed = True
    if mixed:
        return {"schema": "crossfeed-quota-cost/v1", "split": "fit", "options": [],
                "unavailable": "mixed-split batches lack fit-only quota attribution; no cost imputed"}
    result = report_rows(rows)
    result["split"] = "fit"
    return result


def posterior(q, counts, assumptions):
    """Mirror the live evidence.py Beta update, using external sources only."""
    result = copy.deepcopy(q)
    sources = q.get("sources") or []
    strength = q.get("prior_strength", 0)
    if strength and sources:
        if len(sources) != 1 or "prior_mean" not in sources[0]:
            raise ValueError("cannot reconstruct external prior")
        mean = sources[0]["prior_mean"]
        a, b = max(strength * mean, 1e-6), max(strength * (1 - mean), 1e-6)
    else:
        unknown = assumptions.get("unknown", {})
        a, b = unknown.get("alpha", 1), unknown.get("beta", 1)
    a += counts["passes"]
    b += counts["trials"] - counts["passes"]
    sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
    unknown = not sources and not counts["trials"]
    if unknown:
        sd = max(sd, assumptions.get("unknown", {}).get("sd_floor", 0.15))
    elif sources and not counts["trials"]:
        sd = max(sd, assumptions.get("aggregation", {}).get("external_sd_floor", 0.15))
    if q.get("imputation"):
        sd = math.hypot(sd, q["imputation"]["residual_sd"])
    result.update(mean=a / (a + b), sd=sd, unknown=unknown, n_sources=int(bool(sources)))
    return result


def build_evidence(paths, levels, costs=None):
    paths = list(paths)
    excluded = load_exclusions(paths)
    attempted = [dict(row, excluded=True) if task_folder(row["task"]) in excluded else row
                 for row in iter_split_rows(paths, "fit", exclude_tasks=False)]
    rows = [row for row in attempted if not row.get("excluded")]
    counts = defaultdict(lambda: {"passes": 0, "trials": 0})
    telemetry = defaultdict(list)
    identities = {}
    for row in rows:
        if type(row.get("pass")) is not bool:
            raise ValueError("fit pass must be boolean")
        key = (model_key(row["model"], row["level"]), row["level"])
        identity = (row["pool"], row["option"])
        if identity in identities and identities[identity] != key:
            raise ValueError("option identity changed")
        identities[identity] = key
        counts[key + (FAMILY_MAP[row["family"]],)]["trials"] += 1
        counts[key + (FAMILY_MAP[row["family"]],)]["passes"] += int(row["pass"])
        telemetry[key].append(row)
    if costs is None:
        costs = fit_costs(paths, attempted)
    if costs.get("schema") != "crossfeed-quota-cost/v1" or costs.get("split") != "fit":
        raise ValueError("costs must be bench.cost report with split=fit; mixed/heldout costs forbidden")
    measured_costs = {}
    for item in costs["options"]:
        key = identities.get((item["pool"], item["option"]))
        value = item.get("percent_binding_window_per_task")
        if key is None or value is None:
            continue
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                or item.get("unmeasured_tasks", 0) != 0):
            raise ValueError("invalid measured fit cost")
        where = key + (item["pool"],)
        if where in measured_costs and measured_costs[where] != value:
            raise ValueError("conflicting costs for same model/level/pool")
        measured_costs[where] = value
    result = copy.deepcopy(levels)
    available = {(row.get("model_key", row.get("model")), row["level"]) for row in result["rows"]}
    for key in sorted(set(telemetry) - available):
        result["rows"].append({"model_key": key[0], "model": key[0], "level": key[1],
                               "q": {}, "flags": []})
    for row in result["rows"]:
        key = (row.get("model_key", row.get("model")), row["level"])
        families = set(FAMILY_MAP.values()) | set(row.get("q", {})) | set(row.get("own", {}))
        row["own"] = {family: dict(counts[key + (family,)]) for family in sorted(families)}
        row["own_cost"] = {pool: {"percent_per_task": value, "source": "bench.cost", "split": "fit"}
                           for (model, level, pool), value in measured_costs.items() if (model, level) == key}
        row["q"] = {family: posterior(row.get("q", {}).get(family, {}), row["own"][family],
                                     result.get("assumptions", {})) for family in families}
        flags = [flag for flag in row.get("flags", []) if not flag.startswith("unknown")]
        flags += ["unknown:" + family for family, q in row["q"].items() if q["unknown"]]
        row["flags"] = sorted(set(flags + ["bench_fit_only"]))
        # Remove cached local telemetry from L as well. External estimates are
        # permitted; stale locally measured medians would be a second leak.
        cells = telemetry[key]
        for field in ("latency_s", "tokens_per_task"):
            row.pop(field, None)
        durations = [c["duration_s"] for c in cells if c.get("duration_s") is not None]
        row["latency_s"] = statistics.median(durations) if durations else 120.0
        row["tokens_per_task"] = {side: statistics.median(values) if values else
                                  result.get("assumptions", {}).get("estimates", {}).get("tokens_" + side, default)
                                  for side, default in (("in", 4000), ("out", 1500))
                                  for values in [[c["tokens_" + side] for c in cells if c.get("tokens_" + side) is not None]]}
    result["bench"] = {"split": "fit", "split_rule": "sha256(task folder name) even",
                       "fit_cells": len(rows), "fit_attempted_cells": len(attempted), "fit_tasks": sorted({row["task"] for row in rows}),
                       "excluded_task_folders": sorted(excluded), "cost_unavailable": costs.get("unavailable")}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--split", choices=["fit"], required=True)
    parser.add_argument("--levels-in", required=True, type=Path)
    parser.add_argument("--levels-out", required=True, type=Path)
    parser.add_argument("--costs", type=Path)
    args = parser.parse_args(argv)
    if args.levels_in.resolve() == args.levels_out.resolve():
        parser.error("levels-out must be a separate copy")
    try:
        result = build_evidence(args.results, json.loads(args.levels_in.read_text()),
                                json.loads(args.costs.read_text()) if args.costs else None)
        args.levels_out.parent.mkdir(parents=True, exist_ok=True)
        args.levels_out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, "to_evidence: %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
