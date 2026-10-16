#!/usr/bin/env python3
"""Prepare the r14 ChatGPT role migration for a live overlay; never writes it.

Text-only read-only roles (review, research, judgment, writing prose) join the
Crossfeed Chat lane template, and `writing` gets the `chatgpt` slot first so a
healthy zero-price pool leads when the caller asks for text with --mode read-only.
Prints a diff for review; --out writes the migrated overlay for the installer to copy.
"""
import argparse
import copy
import difflib
import json
from pathlib import Path

ROLES = ["reviewer", "audit", "second-opinion", "judgment", "research", "research-scout", "scout", "writing"]


def migrate(source):
    roster = copy.deepcopy(source)
    roles = roster["chatgpt_gateway"]["lane_template"]["roles"]
    roles.extend(role for role in ROLES if role not in roles)
    for bands in roster.get("routing", {}).get("roles", {}).get("writing", {}).values():
        if isinstance(bands, list) and "chatgpt" not in bands:
            bands.insert(0, "chatgpt")
    return roster


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("overlay", type=Path)
    parser.add_argument("--out", type=Path, help="write the migrated overlay here (the source is never modified)")
    args = parser.parse_args()
    source = json.loads(args.overlay.read_text())
    migrated = migrate(source)
    dump = lambda data: json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    diff = list(difflib.unified_diff(dump(source).splitlines(True), dump(migrated).splitlines(True),
                                     str(args.overlay), "migrated"))
    print("".join(diff) or "already migrated", end="")
    if args.out:
        if args.out.resolve() == args.overlay.resolve():
            raise SystemExit("--out must not be the source overlay")
        args.out.write_text(dump(migrated))


if __name__ == "__main__":
    main()
