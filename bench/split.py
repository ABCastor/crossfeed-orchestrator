"""The frozen PREREG task-folder split and shared streaming result reader."""
import hashlib
import json
from pathlib import Path

FAMILY_MAP = {"history-fix": "coding-agent", "fix": "coding-agent",
              "implement": "coding-agent", "review": "review", "repo-qa": "repo-qa",
              "reasoning": "reasoning", "extraction": "extraction"}


def task_folder(task):
    """Return the result ID's task folder without its seed prefix."""
    return task.split(":", 1)[-1]


def load_exclusions(paths, explicit=None):
    """Uniform infrastructure exclusions, named as frozen task folders."""
    manifests = [Path(explicit)] if explicit else sorted({Path(p).parent / "excluded-hook-tasks.txt" for p in paths})
    return {line.strip() for p in manifests if p.exists()
            for line in p.read_text().splitlines() if line.strip() and not line.startswith("#")}


def task_split(task):
    """Result IDs are seed:folder; PREREG hashes the folder, never the seed."""
    if not isinstance(task, str) or not task:
        raise ValueError("task must be a nonempty folder name or result ID")
    folder = task_folder(task)
    if not folder or "/" in folder or "\\" in folder:
        raise ValueError("task must name a folder, not a path")
    return "heldout" if hashlib.sha256(folder.encode()).digest()[-1] & 1 else "fit"


def iter_split_rows(paths, split, exclude_tasks=None):
    """Decode task IDs, filter first, then validate admitted cells only.

    JSON parsing necessarily sees the line. No held-out outcome, identity, cost,
    or metadata is inspected or validated when split=fit.
    """
    if split not in ("fit", "heldout", "all"):
        raise ValueError("invalid split")
    paths = list(paths)
    excluded = set() if exclude_tasks is False else (load_exclusions(paths) if exclude_tasks is None else set(exclude_tasks))
    seen = {}
    for path in paths:
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                if split != "all" and task_split(row["task"]) != split:
                    continue
                if task_folder(row["task"]) in excluded:
                    continue
                cell = (row["task"], row["option"])
                if cell in seen:
                    if seen[cell] != row:
                        raise ValueError("conflicting duplicate cell: %s" % (cell,))
                    continue
                seen[cell] = row
                yield row
