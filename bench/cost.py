#!/usr/bin/env python3
"""Report observed binding-window quota debit per task, separately by option."""
import argparse
from collections import defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench.run_grid import read_results


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing observation clock")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation clock needs timezone")
    return parsed


def percent(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 100):
        raise ValueError("missing or invalid window percentage")
    return value


def pool_info(data, pool):
    """The exact fleetctl.show_usage JSON shape, not local token/cost estimates."""
    if data.get("pool") == pool:
        info = data.get("quota")
    else:
        info = data.get("quota_pools", {}).get(pool)
    if not isinstance(info, dict):
        raise ValueError("pool has no quota snapshot (metered USD is not quota)")
    if (info.get("quota_state") == "UNKNOWN" or info.get("confidence") == "stale"
            or info.get("available") is False):
        raise ValueError("pool quota snapshot unavailable or stale")
    return info


def snapshot_delta(before_path, after_path, pool):
    """Compare one unchanged binding window, never two unrelated maxima."""
    try:
        before = json.loads(Path(before_path).read_text(encoding="utf-8"))
        after = json.loads(Path(after_path).read_text(encoding="utf-8"))
        if any(item.get("status") != "ok" for item in (before, after)):
            raise ValueError("usage command failed or produced invalid JSON")
        a, b = pool_info(before["data"], pool), pool_info(after["data"], pool)
        observed_a, observed_b = timestamp(a.get("observed_at")), timestamp(b.get("observed_at"))
        capture_a, capture_b = timestamp(before.get("captured_at")), timestamp(after.get("captured_at"))
        if not observed_a <= capture_a <= observed_b <= capture_b or observed_b <= observed_a:
            raise ValueError("cached or unordered observations do not bracket the batch")
        if a.get("source") != b.get("source"):
            raise ValueError("quota source changed during batch")
        windows_a, windows_b = a.get("windows", {}), b.get("windows", {})
        if not isinstance(windows_a, dict) or not isinstance(windows_b, dict):
            raise ValueError("invalid quota windows")
        names_a = set(windows_a) - set(a.get("non_binding") or [])
        names_b = set(windows_b) - set(b.get("non_binding") or [])
        if not names_a or names_a != names_b:
            raise ValueError("binding windows unavailable or changed during batch")
        binding = sorted(names_a, key=lambda name: (-percent(windows_a[name].get("used_percent")), name))[0]
        wa, wb = windows_a[binding], windows_b[binding]
        reset_a, reset_b = timestamp(wa.get("reset_at")), timestamp(wb.get("reset_at"))
        if reset_a != reset_b or reset_a <= capture_b:
            raise ValueError("binding window reset during batch")
        delta = percent(wb.get("used_percent")) - percent(wa.get("used_percent"))
        if delta < 0:
            raise ValueError("quota percentage decreased; reset or correction is unresolved")
        # A changed bottleneck cannot safely be subtracted as a single window.
        maximum_b = max(percent(windows_b[name].get("used_percent")) for name in names_b)
        if percent(wb.get("used_percent")) != maximum_b:
            raise ValueError("binding bottleneck changed during batch")
        warnings = ["quota percentage is rounded; zero delta does not establish zero cost",
                    "unobserved concurrent activity cannot be excluded"]
        if observed_a < capture_a:
            warnings.append("baseline observation predates batch start")
        if a.get("confidence") != "direct" or b.get("confidence") != "direct":
            warnings.append("observation confidence is not direct")
        shared = a.get("shared_pool") is True or b.get("shared_pool") is True
        return {"delta_percent": delta, "binding_window": binding, "reset_at": wa["reset_at"],
                "shared_pool": shared, "warnings": warnings}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return {"delta_percent": None, "reason": str(exc), "warnings": []}


def report(results_path):
    return report_rows(list(read_results(results_path).values()))


def report_rows(rows):
    """Measure a supplied collection, with the same batch validation as report()."""
    grouped = defaultdict(list)
    evidence_options = defaultdict(set)
    for row in rows:
        grouped[(row["pool"], row["option"])].append(row)
        evidence = row.get("pool_usage") or {}
        evidence_options[(row["pool"], evidence.get("before"), evidence.get("after"))].add(row["option"])
    summaries = []
    for (pool, option), cells in sorted(grouped.items()):
        batches = defaultdict(list)
        for cell in cells:
            evidence = cell.get("pool_usage") or {}
            batches[(evidence.get("before"), evidence.get("after"))].append(cell)
        total, known_tasks, details, warnings = 0, 0, [], set()
        shared = any((cell.get("pool_usage") or {}).get("shared_pool") is True for cell in cells)
        for (before, after), batch in batches.items():
            if any(cell.get("excluded") for cell in batch):
                measured = {"delta_percent": None, "reason": "batch contains excluded identity; option cost attribution unknown", "warnings": []}
            elif not before or not after:
                measured = {"delta_percent": None, "reason": "missing pool usage evidence", "warnings": []}
            elif len(evidence_options[(pool, before, after)]) > 1:
                measured = {"delta_percent": None, "reason": "snapshot pair mixes options; option attribution unknown", "warnings": []}
            else:
                measured = snapshot_delta(before, after, pool)
            delta = measured["delta_percent"]
            if delta is not None:
                total += delta
                known_tasks += len(batch)
            shared = shared or measured.get("shared_pool", False)
            warnings.update(measured["warnings"])
            details.append({"tasks": len(batch), "before": before, "after": after, **measured})
        if shared:
            warnings.add("shared pool: another user's activity adds noise to observed debit")
        summaries.append({"pool": pool, "option": option, "tasks": len(cells), "measured_tasks": known_tasks,
                          "unmeasured_tasks": len(cells) - known_tasks, "shared_pool": shared,
                          "percent_binding_window_per_task": total / len(cells) if known_tasks == len(cells) else None,
                          "tokens_in": sum(c["tokens_in"] for c in cells) if all("tokens_in" in c for c in cells) else None,
                          "tokens_out": sum(c["tokens_out"] for c in cells) if all("tokens_out" in c for c in cells) else None,
                          "batches": details, "warnings": sorted(warnings)})
    return {"schema": "crossfeed-quota-cost/v1", "unit": "percentage points of binding window per task",
            "definition": "Sum of unchanged binding-window snapshot deltas divided by every attempted task for each pool and option; unknown if any batch lacks valid observations.",
            "options": summaries}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--split", choices=["fit"])
    args = parser.parse_args(argv)
    try:
        if args.split:
            from bench.split import iter_split_rows
            from bench.to_evidence import fit_costs
            result = fit_costs([args.results], list(iter_split_rows([args.results], "fit")))
        else:
            result = report(args.results)
        print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, "cost: %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
