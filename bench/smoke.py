#!/usr/bin/env python3
"""Offline reference/empty checks and a two-option grid, with synthetic usage."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bench import evaluate, generate, run_grid


def public_key(prompt, workspace):
    files = {p.relative_to(workspace).as_posix(): p.read_text(encoding="utf-8")
             for p in sorted(workspace.rglob("*")) if p.is_file() and p.name != "PROMPT.md"}
    return hashlib.sha256(json.dumps([prompt, files], sort_keys=True).encode()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--resamples", type=int, default=10000)
    args = parser.parse_args(argv)
    base = args.out.resolve()
    if base.exists() and any(base.iterdir()):
        parser.error("smoke output directory must be empty")
    base.mkdir(parents=True, exist_ok=True)
    paths = [path for seed in (1, 2) for path in generate.generate(seed, base / "tasks" / ("seed%d" % seed))]
    empty = base / "empty.txt"
    empty.write_text("")
    counts = {"reference_passes": 0, "empty_failures": 0}
    solutions = {}
    checker = Path(__file__).with_name("check.py")
    for task in paths:
        for reply, should_pass, key in ((task / "reference" / "reply.txt", True, "reference_passes"),
                                        (empty, False, "empty_failures")):
            checked = subprocess.run([sys.executable, str(checker), str(task),
                                      str(task / "reference" / "workspace"), str(reply)],
                                     capture_output=True, text=True, check=True, timeout=90)
            verdict = json.loads(checked.stdout)
            if verdict["pass"] is not should_pass:
                raise RuntimeError("%s: %s" % (task.name, verdict))
            counts[key] += 1
        key = public_key((task / "PROMPT.md").read_text(encoding="utf-8"), task / "workspace")
        reference_files = {p.relative_to(task / "reference" / "workspace").as_posix(): p.read_text(encoding="utf-8")
                           for p in (task / "reference" / "workspace").rglob("*") if p.is_file()}
        solutions[key] = [reference_files, (task / "reference" / "reply.txt").read_text(encoding="utf-8")]
    # A deliberately privileged offline oracle exercises plumbing, not model quality.
    gold = base / "gold.json"
    gold.write_text(json.dumps(solutions, sort_keys=True))
    worker = base / "offline_worker.py"
    worker.write_text('''import hashlib,json,sys
from pathlib import Path
mode,prompt,workspace,usage,gold = sys.argv[1:]
workspace = Path(workspace)
files = {p.relative_to(workspace).as_posix(): p.read_text() for p in sorted(workspace.rglob('*')) if p.is_file() and p.name != 'PROMPT.md'}
key = hashlib.sha256(json.dumps([Path(prompt).read_text(),files],sort_keys=True).encode()).hexdigest()
if mode == 'reference':
    output,reply = json.loads(Path(gold).read_text())[key]
    for name,content in output.items():
        path = workspace/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(content)
    print(reply,end='')
else:
    print('',end='')
Path(usage).write_text(json.dumps(dict(tokens_in=100,tokens_out=20,pool_percent=0.01)))
''', encoding="utf-8")
    options = {"options": [
        {"id": mode, "model": "offline-" + mode, "level": "none", "pool": "synthetic",
         "price_1m": {"in": price, "out": price},
         "command": [sys.executable, str(worker), mode, "{prompt_file}", "{workdir}", "{usage_file}", str(gold)]}
        for mode, price in (("reference", 2), ("empty", 1))]}
    option_file = base / "options.json"
    option_file.write_text(json.dumps(options, indent=2))
    policies = {"options_file": "options.json", "fit_seeds": [1], "heldout_seeds": [2],
                "always_max": "reference", "always_cheapest": "empty",
                "fixed_seats": {family: "empty" for family in generate.FAMILIES},
                "selector": {"%s:%s" % (json.loads((task / "meta.json").read_text())["seed"], task.name): "reference" for task in paths}}
    policy_file = base / "policies.json"
    policy_file.write_text(json.dumps(policies, indent=2))
    results = base / "results.jsonl"
    with contextlib.redirect_stdout(io.StringIO()) as output:
        run_grid.main(["--tasks", str(base / "tasks"), "--options", str(option_file), "--out", str(results), "--parallel", "2"])
        run_grid.main(["--tasks", str(base / "tasks"), "--options", str(option_file), "--out", str(results)])
    statuses = [json.loads(line) for line in output.getvalue().splitlines()]
    rows = [json.loads(line) for line in results.read_text().splitlines()]
    if len(rows) != 192 or any(row["pass"] != (row["option"] == "reference") for row in rows):
        raise RuntimeError("offline grid did not give the expected 96 reference passes and 96 empty failures")
    if statuses[-1] != {"completed": 0, "skipped": 192}:
        raise RuntimeError("resume reran completed cells")
    summary = evaluate.evaluate(results, policy_file, "heldout", seed=7, resamples=args.resamples)
    (base / "evaluation.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    (base / "evaluation.md").write_text(evaluate.markdown(summary) + "\n")
    counts.update({"tasks": len(paths), "grid_rows": len(rows), "resume_skipped": 192,
                   "bootstrap_resamples": args.resamples, "synthetic_only": True})
    (base / "verification.json").write_text(json.dumps(counts, indent=2) + "\n")
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
