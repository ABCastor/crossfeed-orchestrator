#!/usr/bin/env python3
"""Replay the LIVE selector in copied state, never run its chosen command."""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench.split import FAMILY_MAP, iter_split_rows
from bench.to_evidence import build_evidence, model_key

LIVE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/fleetctl.py"
ROLE_MAP = {"coding-agent": "implementation", "review": "review", "repo-qa": "repo-map",
            "reasoning": "hard-reasoning", "extraction": "filter"}
SCENARIOS = ("now", "codex-tight", "claude-surplus", "go-tight")


def scenario_runtime(runtime, scenario, now):
    result = copy.deepcopy(runtime)
    if scenario == "now":
        return result
    if scenario not in SCENARIOS:
        raise ValueError("unknown scenario")
    pool = {"codex-tight": "codex", "claude-surplus": "claude", "go-tight": "opencode-go"}[scenario]
    surplus = scenario == "claude-surplus"
    snapshot = result.setdefault("quota_snapshots", {}).setdefault(pool, {})
    snapshot.update(available=True, observed_at=now.isoformat(), source="bench-synthetic-scenario")
    windows = snapshot.setdefault("windows", {})
    if not windows:
        windows["secondary"] = {"label": "Weekly", "window_minutes": 10080}
    for window in windows.values():
        minutes = window.get("window_minutes", 10080)
        window.update(used_percent=5 if surplus else 95,
                      reset_at=(now + timedelta(minutes=minutes / 2)).isoformat())
        for field in ("eta_seconds", "will_last_to_reset", "projected_used_percent_at_reset", "surplus"):
            window.pop(field, None)
    result.setdefault("switches", {})[pool] = "normal"
    result.setdefault("pool_circuits", {}).pop(pool, None)
    return result


def copy_state(source, destination):
    # Dereferenced symlinks become copies: subprocess writes cannot escape to
    # the live template through a writable state alias. Never copy receipts.
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("selections"))


def load_options(paths, result_paths):
    if not paths:
        paths = sorted({p for result in result_paths for p in Path(result).parent.glob("options-*.json")})
    options = {}
    for path in paths:
        for option in json.loads(Path(path).read_text())["options"]:
            if option["id"] in options and options[option["id"]] != option:
                raise ValueError("conflicting option definitions")
            options[option["id"]] = option
    # Synthetic callers can omit options files, but then only observed option
    # identities are known. Never manufacture option IDs from a selector name.
    if not options:
        for row in iter_split_rows(result_paths, "all"):
            option = {k: row[k] for k in ("model", "pool", "level")}
            option["id"] = row["option"]
            if option["id"] in options and options[option["id"]] != option:
                raise ValueError("conflicting observed option identities")
            options[option["id"]] = option
    index = {}
    for option in options.values():
        key = (option["pool"], option.get("model_key") or model_key(option["model"], option["level"]), option["level"])
        if key in index and index[key] != option["id"]:
            raise ValueError("ambiguous grid identity: %s" % (key,))
        index[key] = option["id"]
    return index


def run_policy(results, template, scenario, options=(), script=LIVE_SCRIPT, levels=None,
               costs=None, now=None, runner=subprocess.run):
    now = now or datetime.now(timezone.utc)
    levels_path = Path(levels) if levels else Path(template) / "evidence/levels.json"
    fitted = build_evidence(results, json.loads(levels_path.read_text()),
                            json.loads(Path(costs).read_text()) if costs else None)
    heldout = list(iter_split_rows(results, "heldout"))
    tasks = {}
    for row in heldout:
        tier = row.get("tier", row.get("difficulty"))
        metadata = (FAMILY_MAP[row["family"]], tier)
        if tier not in ("easy", "medium", "hard", "expert"):
            raise ValueError("invalid task tier")
        if row["task"] in tasks and tasks[row["task"]] != metadata:
            raise ValueError("inconsistent task metadata")
        tasks[row["task"]] = metadata
    measured = {(r["task"], r["option"], r["pool"], model_key(r["model"], r["level"]), r["level"])
                for r in heldout if not r.get("excluded")}
    index = load_options(options, results)
    runtime = scenario_runtime(json.loads((Path(template) / "runtime.json").read_text()), scenario, now)
    output = {"schema": "crossfeed-selector-policy/v1", "scenario": scenario,
              "observed_at": now.isoformat(), "mapping": {}, "selections": {}, "status": {},
              "runtime": runtime, "fit_evidence": fitted["bench"],
              "selector_sha256": hashlib.sha256(Path(script).read_bytes()).hexdigest(),
              "scenario_definition": "now preserves snapshot; affected synthetic pool: tight=95%, surplus=5%, halfway through every window; switches normal and circuit cleared"}
    with tempfile.TemporaryDirectory(prefix="crossfeed-selector-") as work:
        for i, (task, (family, tier)) in enumerate(sorted(tasks.items())):
            with tempfile.TemporaryDirectory(prefix=str(i) + "-", dir=work) as task_state:
                state = Path(task_state)
                copy_state(template, state)
                (state / "evidence").mkdir(exist_ok=True)
                (state / "evidence/levels.json").write_text(json.dumps(fitted, allow_nan=False))
                (state / "runtime.json").write_text(json.dumps(runtime, allow_nan=False))
                role, stakes = ROLE_MAP[family], "high" if tier in ("expert", "hard") else "normal"
                command = [sys.executable, str(script), "select", "--role", role, "--stakes", stakes, "--json", "--no-refresh"]
                env = dict(os.environ, FLEET_STATE_DIR=str(state), PYTHONDONTWRITEBYTECODE="1")
                try:
                    completed = runner(command, env=env, capture_output=True, text=True, timeout=60)
                    selection = json.loads(completed.stdout) if completed.stdout.strip() else {}
                    output["selections"][task] = selection
                    choice = selection.get("choice", {})
                    key = (choice.get("pool"), choice.get("model_key"), choice.get("level"))
                    ident = index.get(key)
                    if completed.returncode != 0:
                        status, ident = "selection_error", None
                    elif ident is None or (task, ident, *key) not in measured:
                        status, ident = "unmeasured_choice", None
                    else:
                        status = "measured_choice"
                    output["status"][task] = {"status": status, "grid_option": index.get(key),
                                               "choice_identity": list(key), "returncode": completed.returncode}
                except (subprocess.TimeoutExpired, ValueError) as exc:
                    output["selections"][task] = {"error": type(exc).__name__}
                    output["status"][task] = {"status": "selection_error"}
                    ident = None
                output["mapping"][task] = ident
    output["summary"] = {"heldout_tasks": len(tasks), **{status: sum(v["status"] == status for v in output["status"].values())
                         for status in ("measured_choice", "unmeasured_choice", "selection_error")}}
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", nargs="+", type=Path, required=True)
    parser.add_argument("--state-template", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--options", nargs="+", type=Path)
    parser.add_argument("--levels-in", type=Path)
    parser.add_argument("--costs", type=Path)
    args = parser.parse_args(argv)
    try:
        output = run_policy(args.results, args.state_template, args.scenario, args.options,
                            levels=args.levels_in, costs=args.costs)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(output, indent=2, sort_keys=True, allow_nan=False) + "\n")
        print(json.dumps(output["summary"], sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, "selector_policy: %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
