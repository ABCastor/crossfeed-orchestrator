#!/usr/bin/env python3
"""Measure a paired grid using local command templates; never judge with a model."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import quote

FAMILIES = ("fix", "history-fix", "implement", "review", "repo-qa", "reasoning", "extraction")
FIELDS = {"prompt_file", "workdir", "model", "level", "bench_dir", "reply_file",
          "events_file", "usage_file"}
MAX_OUTPUT = 8 * 1024 * 1024
RECEIPT_PREFIX = "Crossfeed model receipt: "
RUN_ID_PATTERN = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


def split_receipt(text):
    """Remove only a final receipt line, preserving all other reply text."""
    lines = text.splitlines(keepends=True)
    last = len(lines) - 1
    while last >= 0 and not lines[last].strip():
        last -= 1
    if last < 0 or not lines[last].startswith(RECEIPT_PREFIX):
        return text, {}
    receipt = lines.pop(last).rstrip()
    identity = {}
    ran = re.search(r"; ran on ([^;\r\n]+?)(?: \([^;\r\n]*\))?; selector ", receipt)
    run = re.search(r"; run (" + RUN_ID_PATTERN + r")\.$", receipt)
    if "underlying model unconfirmed" in receipt:
        identity["identity"] = "unconfirmed"
    if ran and identity.get("identity") != "unconfirmed":
        identity["ran_on"] = ran.group(1)
    if run:
        identity["run_id"] = run.group(1)
    return "".join(lines), identity


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def option_digest(option):
    expanded = [os.path.expanduser(os.path.expandvars(value)).replace("{bench_dir}", str(Path(__file__).resolve().parent))
                for value in option["command"]]
    scripts = {}
    for value in expanded:
        if "{" not in value and Path(value).suffix in (".py", ".sh") and Path(value).is_file():
            scripts[value] = hashlib.sha256(Path(value).read_bytes()).hexdigest()
    return digest({"option": option, "expanded_command": expanded, "scripts": scripts})


def load_options(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    options = config["options"]
    if not isinstance(options, list) or not options:
        raise ValueError("options must be a nonempty array")
    ids = set()
    for option in options:
        ident = option.get("id")
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError("option IDs must be unique nonempty strings")
        ids.add(ident)
        command = option.get("command")
        if isinstance(command, str):
            command = shlex.split(command)
        if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command):
            raise ValueError("command must be an argv array or shell-quoted argv string")
        for item in command:
            for _, field, spec, conv in string.Formatter().parse(item):
                if field is not None and (field not in FIELDS or spec or conv):
                    raise ValueError("unsupported command template field: %s" % field)
        if not all(isinstance(option.get(key), str) for key in ("model", "level", "pool")):
            raise ValueError("each option needs model, level and pool strings")
        if "shared_pool" in option and type(option["shared_pool"]) is not bool:
            raise ValueError("shared_pool must be a boolean")
        timeout = option.get("timeout_s", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_s must be a positive finite number")
        grace = option.get("termination_grace_s", 5)
        if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not math.isfinite(grace) or grace < 0:
            raise ValueError("termination_grace_s must be a finite nonnegative number")
        option["command"] = command
    return options


def task_digest(task):
    hashed = hashlib.sha256()
    if (task / "workspace").is_symlink():
        raise ValueError("task workspace root cannot be a symlink")
    paths = [task / "PROMPT.md", task / "meta.json", task / "check.json"]
    paths += sorted((task / "workspace").rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise ValueError("task fixtures cannot contain symlinks")
        if path.is_file():
            hashed.update(str(path.relative_to(task)).encode() + b"\0" + path.read_bytes() + b"\0")
    return hashed.hexdigest()


def discover_tasks(root, only=None):
    root = Path(root)
    tasks = []
    seen = set()
    # Accept a single seed directory or a parent containing several seed dirs.
    for metadata in sorted(root.rglob("meta.json")):
        task = metadata.parent
        if not (task / "PROMPT.md").is_file() or not (task / "check.json").is_file():
            continue
        if not (task / "workspace").is_dir():
            raise ValueError("task is missing workspace/: %s" % task.name)
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        if meta.get("family") not in FAMILIES or meta.get("difficulty") not in ("easy", "medium", "hard", "expert"):
            raise ValueError("invalid task family or difficulty")
        if type(meta.get("seed")) is not int:
            raise ValueError("seed must be an integer")
        if only and meta["family"] != only:
            continue
        ident = "%s:%s" % (meta["seed"], task.name)
        if ident in seen:
            raise ValueError("duplicate task identity: " + ident)
        seen.add(ident)
        tasks.append((task, meta, ident, task_digest(task)))
    if not tasks:
        raise ValueError("no task fixtures found")
    return tasks


def render_command(option, fields, dry_run=False):
    command = []
    for value in option["command"]:
        # Expand only trusted config strings, before inserting paths/model values.
        value = os.path.expanduser(value if dry_run else os.path.expandvars(value))
        if not dry_run and re.search(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})", value):
            raise ValueError("undefined environment variable in command template")
        command.append(value.format_map(fields))
    return command


def read_results(path):
    previous = {}
    if not Path(path).exists():
        return previous
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            key = (row["task"], row["option"])
            if not isinstance(row.get("pass"), bool):
                raise ValueError("missing boolean pass")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("invalid results line %s; preserve and repair interrupted output: %s" % (number, exc))
        if key in previous and previous[key] != row:
            raise ValueError("conflicting duplicate result: %s" % (key,))
        previous[key] = row
    return previous


def execute(command, cwd, stdout, stderr, timeout, env=None, grace=5):
    with open(stdout, "wb") as out, open(stderr, "wb") as err:
        process = subprocess.Popen(command, cwd=str(cwd), stdout=out, stderr=err,
                                   stdin=subprocess.DEVNULL, env=env, start_new_session=(os.name == "posix"))
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
            # Kill the entire process group even if its leader already exited.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.wait()
            return 124


def prepare_git(workspace):
    for command in (["git", "init", "-q"], ["git", "add", "--all"],
                    ["git", "-c", "user.name=Benchmark", "-c", "user.email=benchmark@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "Prepare isolated fixture"]):
        subprocess.run(command, cwd=str(workspace), check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=30)


def telemetry(usage_file, events_file):
    values = {}
    if usage_file.exists():
        values = json.loads(usage_file.read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError("usage sidecar must contain a JSON object")
    elif events_file.exists():
        # Codex turn.completed and OpenCode step-finish events are totals per step.
        totals = {"tokens_in": 0, "tokens_out": 0}
        found = False
        for line in events_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("telemetry event must contain a JSON object")
            usage = event.get("usage") if event.get("type") == "turn.completed" else None
            if isinstance(usage, dict) and "input_tokens" in usage and "output_tokens" in usage:
                if any(type(usage[key]) is not int or usage[key] < 0 for key in ("input_tokens", "output_tokens")):
                    raise ValueError("event token counts must be nonnegative integers")
                totals["tokens_in"] += usage["input_tokens"]
                totals["tokens_out"] += usage["output_tokens"]
                found = True
            part = event.get("part", {})
            if event.get("type") == "step_finish" and isinstance(part, dict):
                tokens = part.get("tokens", {})
                if not isinstance(tokens, dict):
                    raise ValueError("step tokens must be an object")
                if "input" in tokens and "output" in tokens:
                    if any(type(tokens.get(key, 0)) is not int or tokens.get(key, 0) < 0
                           for key in ("input", "output", "reasoning")):
                        raise ValueError("event token counts must be nonnegative integers")
                    cache = tokens.get("cache", {})
                    if not isinstance(cache, dict) or any(type(cache.get(key, 0)) is not int or cache.get(key, 0) < 0
                                                          for key in ("read", "write")):
                        raise ValueError("cached token counts must be nonnegative integers")
                    totals["tokens_in"] += tokens["input"] + cache.get("read", 0) + cache.get("write", 0)
                    totals["tokens_out"] += tokens["output"] + tokens.get("reasoning", 0)
                    found = True
        if found:
            values = totals
    for key in ("tokens_in", "tokens_out"):
        if key in values and (type(values[key]) is not int or values[key] < 0):
            raise ValueError("%s must be a nonnegative integer" % key)
    if "pool_percent" in values and (isinstance(values["pool_percent"], bool) or
            not isinstance(values["pool_percent"], (int, float)) or
            not math.isfinite(values["pool_percent"]) or values["pool_percent"] < 0):
        raise ValueError("pool_percent must be a finite nonnegative number")
    return {k: values[k] for k in ("tokens_in", "tokens_out", "pool_percent", "model", "level") if k in values}


def receipt_metadata(path):
    if not path.is_file():
        return {}
    text = path.read_bytes().decode('utf-8', errors='replace')
    _, identity = split_receipt(text)
    lines = [line for line in text.splitlines() if line.strip()]
    identity['receipt'] = lines[-1] if lines and lines[-1].startswith(RECEIPT_PREFIX) else None
    return identity


def normalized_model(model):
    return model.removeprefix("opencode-go/")


def usage_snapshot(command, path, timeout=90):
    """Persist a bounded command observation, including unavailable outcomes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"schema": "crossfeed-usage-snapshot/v1", "status": "failed",
                "started_at": datetime.now(timezone.utc).isoformat()}
    with tempfile.TemporaryDirectory(prefix="crossfeed-usage-") as temporary:
        stdout, stderr = Path(temporary) / "stdout", Path(temporary) / "stderr"
        try:
            code = execute(command, Path.cwd(), stdout, stderr, timeout)
            snapshot["exit_code"] = code
            if code:
                snapshot["status"] = "timeout" if code == 124 else "failed"
            elif stdout.stat().st_size > MAX_OUTPUT:
                snapshot["status"] = "output-too-large"
            else:
                data = json.loads(stdout.read_text(encoding="utf-8"),
                                  parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if not isinstance(data, dict):
                    raise ValueError("usage snapshot must be an object")
                snapshot.update(status="ok", data=data)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            snapshot.update(status="invalid-json" if isinstance(exc, ValueError) else "failed",
                            error_type=type(exc).__name__)
    snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(snapshot, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return str(path.resolve())


def measure(task_info, option, results_dir=None):
    task, meta, ident, fixture_hash = task_info
    result = dict(task=ident, family=meta["family"], difficulty=meta["difficulty"], seed=meta["seed"],
                  option=option["id"], pool=option["pool"], model=option["model"], level=option["level"],
                  task_digest=fixture_hash, option_digest=option_digest(option))
    result.update({key: meta[key] for key in ("template_id", "tier") if key in meta})
    cell_dir = Path(results_dir if results_dir is not None else task.parent / 'results') / 'cells' / (quote(ident, safe='') + '__' + quote(option['id'], safe=''))
    cell_dir.mkdir(parents=True, exist_ok=True)
    result['cell_dir'] = str(cell_dir.resolve())
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="crossfeed-grid-") as temporary:
        base = Path(temporary)
        workspace = base / "workspace"
        prompt_file = workspace / "PROMPT.md"
        reply = base / "reply.txt"
        stderr = base / "stderr.txt"
        events = base / "events.jsonl"
        usage = base / "usage.json"
        fields = dict(prompt_file=str(prompt_file), workdir=str(workspace), model=option["model"],
                      level=option["level"], bench_dir=str(Path(__file__).resolve().parent),
                      reply_file=str(base / "final.txt"), events_file=str(events), usage_file=str(usage))
        boundary = None
        excluded = (reply, stderr, events, usage, base / "final.txt", base / "checked.txt")
        try:
            shutil.copytree(task / "workspace", workspace)
            if prompt_file.exists():
                raise ValueError("workspace must not contain reserved PROMPT.md")
            shutil.copyfile(task / "PROMPT.md", prompt_file)
            command = render_command(option, fields)
            if option.get("workspace_git", False):
                prepare_git(workspace)
            if meta["family"] == "history-fix":
                try:
                    from .history import boundary_snapshot
                except ImportError:
                    from history import boundary_snapshot
                boundary = boundary_snapshot(base, workspace, excluded=excluded)
            exit_code = execute(command, workspace, reply, stderr, option.get("timeout_s", 600),
                                grace=option.get("termination_grace_s", 5))
            result["duration_s"] = round(time.monotonic() - started, 6)
            result["exit_code"] = exit_code
            if reply.stat().st_size > MAX_OUTPUT or stderr.stat().st_size > MAX_OUTPUT:
                result.update({"pass": False, "reason": "worker output exceeded 8 MiB"})
                return result
            reply_text, receipt_identity = split_receipt(reply.read_bytes().decode("utf-8", errors="surrogateescape"))
            result.update(receipt_identity)
            diagnostic = stderr.read_text(encoding="utf-8", errors="replace")
            final = base / "final.txt"
            final_identity = {}
            if final.exists():
                if final.stat().st_size > MAX_OUTPUT:
                    result.update({"pass": False, "reason": "final reply exceeded 8 MiB"})
                    return result
                reply_text, final_identity = split_receipt(final.read_bytes().decode("utf-8", errors="surrogateescape"))
                for key, value in final_identity.items():
                    result.setdefault(key, value)
            checked_reply = base / "checked.txt"
            # The checker removes the receipt once; preserve the original bytes here.
            checked_reply.write_bytes((final if final.exists() else reply).read_bytes())
            mismatch = next((identity for identity in (receipt_identity, final_identity)
                             if "ran_on" in identity and normalized_model(identity["ran_on"]) != normalized_model(option["model"])), None)
            if mismatch:
                result.update(mismatch)
                result.update({"pass": False, "reason": "receipt model mismatch", "excluded": True,
                               "actual_model": result["ran_on"]})
                return result
            stand_in = re.search(r"Crossfeed: this run used (\S+), not ", diagnostic)
            if stand_in and normalized_model(stand_in.group(1)) != normalized_model(option["model"]):
                result.update({"pass": False, "reason": "wrapper substituted requested model",
                               "excluded": True, "actual_model": stand_in.group(1)})
                return result
            try:
                usage_values = telemetry(usage, events)
                actual_model = usage_values.pop("model", option["model"])
                actual_level = usage_values.pop("level", option["level"])
                result.update(usage_values)
                if (normalized_model(actual_model), actual_level) != (normalized_model(option["model"]), option["level"]):
                    result.update({"pass": False, "reason": "telemetry option identity mismatch", "excluded": True,
                                   "actual_model": actual_model, "actual_level": actual_level})
                    return result
            except (ValueError, TypeError) as exc:
                result["telemetry_error"] = str(exc)
            if exit_code != 0:
                result.update({"pass": False, "reason": "worker timed out" if exit_code == 124 else
                               "worker exited with code %s" % exit_code})
                return result
            checked = subprocess.run([sys.executable, str(Path(__file__).with_name("check.py")),
                                      str(task), str(workspace), str(checked_reply)],
                                     capture_output=True, text=True,
                                     timeout=(json.loads((task / "check.json").read_text()).get("timeout_s", 90) + 10
                                              if meta["family"] == "history-fix" else 90))
            verdict = json.loads(checked.stdout)
            if checked.returncode or type(verdict.get("pass")) is not bool or not isinstance(verdict.get("reason"), str):
                raise ValueError("checker did not return a valid verdict")
            result.update(verdict)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result.update({"pass": False, "reason": "execution/check error: %s" % type(exc).__name__,
                           "exit_code": result.get("exit_code", 127),
                           "duration_s": round(time.monotonic() - started, 6)})
        finally:
            if boundary is not None:
                try:
                    from .history import boundary_unchanged
                except ImportError:
                    from history import boundary_unchanged
                if not boundary_unchanged(boundary, base, workspace, excluded=excluded):
                    result.update({"pass": False, "strict_pass": False, "reason": "outside repo edited"})
            result.setdefault("strict_pass", result["pass"])
            # Only output artifacts leave isolation; no prompts or hidden checks.
            source = base / 'final.txt' if (base / 'final.txt').is_file() else reply
            (cell_dir / 'reply.txt').write_bytes(source.read_bytes() if source.is_file() else b'')
            with stderr.open('rb') if stderr.is_file() else open(os.devnull, 'rb') as errors:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 4096))
                (cell_dir / 'stderr.tail').write_bytes(errors.read())
            evidence = {'stdout': receipt_metadata(reply), 'final': receipt_metadata(base / 'final.txt')}
            (cell_dir / 'receipt.json').write_text(json.dumps(evidence, sort_keys=True) + '\n', encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True, type=Path)
    parser.add_argument("--options", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--only", choices=FAMILIES)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pool-batches", action="store_true", help="finish each pool before starting another")
    parser.add_argument("--usage-cmd", help="argv command producing fleetctl usage --json snapshots; no shell")
    parser.add_argument("--usage-timeout", type=float, default=90)
    parser.add_argument("--shared-pool", action="append", default=[], help="pool whose plan is shared with another user")
    args = parser.parse_args(argv)
    try:
        if args.parallel < 1:
            raise ValueError("parallel must be at least 1")
        if args.usage_cmd and not args.pool_batches:
            raise ValueError("--usage-cmd requires --pool-batches for attributable observations")
        if not math.isfinite(args.usage_timeout) or args.usage_timeout <= 0:
            raise ValueError("usage timeout must be positive and finite")
        usage_command = [os.path.expanduser(os.path.expandvars(part)) for part in shlex.split(args.usage_cmd or "")]
        if args.usage_cmd and (not usage_command or any(re.search(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})", part)
                                                       for part in usage_command)):
            raise ValueError("empty usage command or undefined environment variable")
        tasks = discover_tasks(args.tasks.resolve(), args.only)
        options = load_options(args.options)
        previous = read_results(args.out)
        pending = []
        for task in tasks:
            for option in options:
                old = previous.get((task[2], option["id"]))
                if old is not None:
                    if old.get("task_digest") != task[3] or old.get("option_digest") != option_digest(option):
                        raise ValueError("resume input changed or provenance absent: %s / %s; use a new output" % (task[2], option["id"]))
                    continue
                pending.append((task, option))
        batches = [pending]
        if args.pool_batches:
            pools = list(dict.fromkeys(option["pool"] for _, option in pending))
            batches = [[cell for cell in pending if cell[1]["pool"] == pool] for pool in pools]
        if args.dry_run:
            planned = []
            for batch in batches:
                if usage_command:
                    for ident in dict.fromkeys(option["id"] for _, option in batch):
                        planned.extend(cell for cell in batch if cell[1]["id"] == ident)
                else:
                    planned.extend(batch)
            for task, option in planned:
                fields = dict(prompt_file="<temporary-workspace>/PROMPT.md", workdir="<temporary-workspace>",
                              model=option["model"], level=option["level"], bench_dir=str(Path(__file__).resolve().parent),
                              reply_file="<temporary-run>/final.txt", events_file="<temporary-run>/events.jsonl",
                              usage_file="<temporary-run>/usage.json")
                print(json.dumps({"task": task[2], "option": option["id"], "command": render_command(option, fields, True)}))
            return 0
        args.out.parent.mkdir(parents=True, exist_ok=True)
        lock = args.out.with_name(args.out.name + ".lock")
        try:
            descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError("output is locked by another run; inspect the .lock owner before removing it")
        try:
            with os.fdopen(descriptor, "w") as owner:
                owner.write(str(os.getpid()) + "\n")
            # Re-read after obtaining lock to close simultaneous-start resume races.
            if read_results(args.out) != previous:
                raise ValueError("results changed while acquiring the output lock; restart")
            with args.out.open("a", encoding="utf-8") as output, ThreadPoolExecutor(max_workers=args.parallel) as executor:
                if args.out.stat().st_size:
                    with args.out.open("rb") as existing:
                        existing.seek(-1, os.SEEK_END)
                        if existing.read(1) != b"\n":
                            output.write("\n")
                for batch in batches:
                    if not batch:
                        continue
                    pool = batch[0][1]["pool"]
                    batch_id = uuid.uuid4().hex
                    evidence_dir = args.out.parent / "usage-batches" / batch_id
                    pool_before, pool_after = evidence_dir / "pool.before.json", evidence_dir / "pool.after.json"
                    if usage_command:
                        usage_snapshot(usage_command, pool_before, args.usage_timeout)
                    option_ids = list(dict.fromkeys(option["id"] for _, option in batch))
                    groups = [[cell for cell in batch if cell[1]["id"] == ident] for ident in option_ids] if usage_command else [batch]
                    for group in groups:
                        option = group[0][1]
                        before, after = pool_before, pool_after
                        if usage_command and len(groups) > 1:
                            before = evidence_dir / ("option-" + quote(option["id"], safe="") + ".before.json")
                            after = evidence_dir / ("option-" + quote(option["id"], safe="") + ".after.json")
                            usage_snapshot(usage_command, before, args.usage_timeout)
                        futures = [executor.submit(measure, task, opt, args.out.parent) for task, opt in group]
                        for future in as_completed(futures):
                            row = future.result()
                            if usage_command:
                                row["pool_usage"] = {"batch_id": batch_id, "before": str(before.resolve()),
                                    "after": str(after.resolve()), "pool_before": str(pool_before.resolve()),
                                    "pool_after": str(pool_after.resolve()),
                                    "shared_pool": pool in args.shared_pool or option.get("shared_pool", False)}
                                if row.get("cell_dir"):
                                    (Path(row["cell_dir"]) / "pool-usage.json").write_text(
                                        json.dumps(row["pool_usage"], sort_keys=True) + "\n", encoding="utf-8")
                            output.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                            output.flush()
                            os.fsync(output.fileno())
                        if usage_command and len(groups) > 1:
                            usage_snapshot(usage_command, after, args.usage_timeout)
                    if usage_command:
                        usage_snapshot(usage_command, pool_after, args.usage_timeout)
            print(json.dumps({"completed": len(pending), "skipped": len(tasks) * len(options) - len(pending)}))
        finally:
            lock.unlink(missing_ok=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, "run_grid: %s\n" % exc)


if __name__ == "__main__":
    sys.exit(main())
