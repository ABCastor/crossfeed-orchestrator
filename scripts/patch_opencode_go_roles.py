#!/usr/bin/env python3
"""Prepare a guarded OpenCode Go value-routing overlay patch.

Dry-run is the default. The source overlay is only replaced with --apply, after
the candidate has passed roster validation and a timestamped backup is saved.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Any


POOL = "opencode-go"
SHORTLIST: dict[str, list[str]] = {
    "mimo-v2.6-flash": ["default", "repo-map", "research-scout"],
    "deepseek-v4.1-flash": ["default", "repo-map", "research-scout", "review"],
    "qwen3.8-flash": ["implementation"],
    "glm-5.3-flash": ["debug", "review"],
    "mimo-v2.6-pro": ["debug", "hard-reasoning", "review"],
}

# Order within each router role follows the report's value/quality judgement.
# Other providers keep their relative position in the existing band list.
ROUTE_ORDER: dict[str, list[str]] = {
    "default": ["mimo-v2.6-flash", "deepseek-v4.1-flash"],
    "repo-map": ["mimo-v2.6-flash", "deepseek-v4.1-flash"],
    "research-scout": ["mimo-v2.6-flash", "deepseek-v4.1-flash"],
    "implementation": ["qwen3.8-flash"],
    "debug": ["mimo-v2.6-pro", "glm-5.3-flash"],
    "hard-reasoning": ["mimo-v2.6-pro"],
    "review": ["mimo-v2.6-pro", "glm-5.3-flash", "deepseek-v4.1-flash"],
}


# BR-REFRESH-001
def helper_call_cost(price: dict[str, Any]) -> float | None:
    """USD for the standard helper call: 8k input + 2k output tokens.

    Catalog prices are per million tokens. Unknown/invalid is never free.
    """
    values = [price.get(key) for key in ("input", "output")]
    if any(not isinstance(x, (int, float)) or isinstance(x, bool)
           or not math.isfinite(x) or x < 0 for x in values):
        return None
    return (8000 * values[0] + 2000 * values[1]) / 1_000_000


class PatchError(RuntimeError):
    """A candidate cannot safely be prepared or installed."""


def _is_opencode_go(lane: dict[str, Any]) -> bool:
    return (lane.get("harness") == "opencode"
            and lane.get("provider") == POOL
            and lane.get("quota_pool") == POOL)


def migrate(source: dict[str, Any]) -> dict[str, Any]:
    """Set shortlist roles and keep only shortlist lanes in automatic Go routes."""
    if not isinstance(source, dict) or not isinstance(source.get("lanes"), list):
        raise PatchError("overlay must be an object with a lanes array")

    roster = copy.deepcopy(source)
    go_lanes = [lane for lane in roster["lanes"] if _is_opencode_go(lane)]
    malformed_go = [lane.get("lane_id", "<missing lane_id>") for lane in roster["lanes"]
                    if lane.get("quota_pool") == POOL and not _is_opencode_go(lane)]
    if malformed_go:
        raise PatchError("OpenCode Go pool has lanes with inconsistent harness/provider: "
                         + ", ".join(malformed_go))

    by_model: dict[str, list[dict[str, Any]]] = {}
    go_ids: set[str] = set()
    for lane in go_lanes:
        model = lane.get("model_key")
        if not isinstance(model, str):
            raise PatchError(f"OpenCode Go lane {lane.get('lane_id', '<missing lane_id>')} has no model_key")
        by_model.setdefault(model, []).append(lane)
        lane_id = lane.get("lane_id")
        if isinstance(lane_id, str):
            go_ids.add(lane_id)

    for model in SHORTLIST:
        eligible = [lane for lane in by_model.get(model, [])
                    if lane.get("access_status") == "verified"
                    and lane.get("admission_status") == "active"]
        if not eligible:
            raise PatchError(f"shortlisted OpenCode Go model is not active and verified: {model}")

    for lane in go_lanes:
        lane["roles"] = SHORTLIST.get(lane["model_key"], []).copy()

    route_roles = roster.get("routing", {}).get("roles")
    if not isinstance(route_roles, dict):
        raise PatchError("overlay routing.roles must be an object")

    route_ids: dict[str, list[str]] = {}
    for role, models in ROUTE_ORDER.items():
        missing_role = role not in route_roles
        if missing_role:
            raise PatchError(f"overlay is missing routing role needed by shortlist: {role}")
        route_ids[role] = [lane["lane_id"] for model in models
                           for lane in by_model.get(model, [])
                           if _is_opencode_go(lane)
                           and lane.get("access_status") == "verified"
                           and lane.get("admission_status") == "active"]

    for role, bands in route_roles.items():
        if not isinstance(bands, dict):
            continue
        for band, entries in bands.items():
            if not isinstance(entries, list):
                continue
            old_go_positions = [i for i, lane_id in enumerate(entries)
                                if isinstance(lane_id, str) and lane_id in go_ids]
            insertion = (sum(1 for item in entries[:min(old_go_positions)] if item not in go_ids)
                         if old_go_positions else len([item for item in entries if item not in go_ids]))
            retained = [lane_id for lane_id in entries
                        if not (isinstance(lane_id, str) and lane_id in go_ids)]
            selected = route_ids.get(role, [])
            retained[insertion:insertion] = selected
            bands[band] = retained

    return roster


def dump(roster: dict[str, Any]) -> str:
    return json.dumps(roster, indent=2, ensure_ascii=False) + "\n"


def validate_overlay(contents: str, *, label: str) -> None:
    """Run the repo's roster validator against an isolated temporary file."""
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="opencode-go-overlay-") as temp:
        temp_path = Path(temp)
        candidate = temp_path / "access-overlay.json"
        candidate.write_text(contents)
        env = os.environ.copy()
        env.update({
            "ACCESS_OVERLAY": str(candidate),
            "FLEET_STATE_DIR": str(temp_path / "state"),
            "FLEET_NO_AUTO_REFRESH": "1",
        })
        result = subprocess.run(["bash", str(root / "scripts/roster.sh"), "validate"],
                                cwd=root, env=env, capture_output=True, text=True)
        if result.returncode:
            detail = (result.stderr + result.stdout).strip()
            raise PatchError(f"{label} failed roster validation (exit {result.returncode}):\n{detail}")


def _atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=False, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _backup_path(overlay: Path) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    return overlay.with_name(f"{overlay.name}.bak.{stamp}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", type=Path, required=True,
                        help="source overlay; unchanged unless --apply is supplied")
    parser.add_argument("--patch", type=Path, required=True,
                        help="unified patch file to write")
    parser.add_argument("--apply", action="store_true",
                        help="after validation, back up and atomically replace --overlay")
    args = parser.parse_args(argv)

    overlay = args.overlay.expanduser()
    patch_path = args.patch.expanduser()
    try:
        if overlay.is_symlink():
            raise PatchError("refusing a symlink source overlay; pass the resolved file path explicitly")
        if overlay.resolve() == patch_path.resolve():
            raise PatchError("--patch must differ from --overlay")
        original = overlay.read_bytes()
        source = json.loads(original)
        migrated = migrate(source)
        updated = dump(migrated).encode("utf-8")
        validate_overlay(updated.decode("utf-8"), label="candidate")

        diff = "".join(difflib.unified_diff(
            original.decode("utf-8").splitlines(keepends=True),
            updated.decode("utf-8").splitlines(keepends=True),
            fromfile=str(overlay), tofile=str(overlay)))
        patch_path.write_text(diff)
        print(f"Patch: {patch_path}")
        print("Candidate validated; " + ("patch is empty" if not diff else "source remains unchanged"))
        if not diff:
            print("No changes needed; no backup created.")
            return 0
        if not args.apply:
            print("Dry run; pass --apply to back up and replace the source overlay.")
            print(diff, end="")
            return 0

        backup = _backup_path(overlay)
        shutil.copy2(overlay, backup)
        print(f"Backup: {backup}")
        try:
            _atomic_write(overlay, updated)
            validate_overlay(overlay.read_text(), label="applied overlay")
        except BaseException:
            _atomic_write(overlay, backup.read_bytes())
            raise
        print(f"Applied and validated: {overlay}")
        return 0
    except (OSError, json.JSONDecodeError, PatchError) as exc:
        print(f"patch_opencode_go_roles: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
