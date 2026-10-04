#!/usr/bin/env python3
"""Quota oracle for the Codex pool, read from Codex's own session logs.

No third-party app, no network call, no credential access: Codex records the
rate-limit headers it receives into the JSONL transcript of every session it
runs, and the newest such record is the freshest quota reading available on the
machine. That makes this the portable oracle -- it works anywhere Codex runs,
which is the whole point on Linux, where the macOS menu-bar readers do not.

Cross-checked against an independent reader on the same account: identical
reset timestamps to the second.

Wire it up in the access overlay:

    "quota_pools": {
      "codex": {
        "quota_refresh": {
          "oracle": "command",
          "command": ["python3", "/path/to/scripts/oracle-codex-local.py"],
          "ttl_s": 900
        }
      }
    }

Emits the shape every non-builtin oracle emits:

    {"windows": {"<name>": {"used_percent": 0-100, "reset_at": "<ISO8601>",
                            "window_minutes": <int>}}}

or, when it cannot read anything:

    {"available": false, "reason": "<why>"}
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

SESSIONS = Path(
    os.environ.get("CODEX_HOME", "~/.codex")
).expanduser() / "sessions"

# Matched on the raw line rather than parsed as JSON: these transcripts are large
# and mostly irrelevant, and only lines carrying a rate-limit record are worth
# the decode.
RATE_LINE = re.compile(r'"rate_limits"\s*:\s*\{')
MAX_FILES = 40


def _iso(epoch: float) -> str:
    return (
        dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _windows_from(record: dict) -> dict:
    """Codex publishes `primary` and `secondary` slots; either may be absent."""
    windows: dict[str, dict] = {}
    for slot in ("primary", "secondary"):
        window = record.get(slot)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        resets_at = window.get("resets_at")
        if used is None or resets_at is None:
            continue
        try:
            entry = {
                "used_percent": int(round(float(used))),
                "reset_at": _iso(float(resets_at)),
            }
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        minutes = window.get("window_minutes")
        if isinstance(minutes, (int, float)) and minutes > 0:
            entry["window_minutes"] = int(minutes)
        windows[slot] = entry
    return windows


def main() -> int:
    if not SESSIONS.is_dir():
        print(json.dumps({"available": False, "reason": f"no session dir at {SESSIONS}"}))
        return 0

    # Newest transcripts first, and stop at the first usable record: an older
    # reading is strictly worse than the one already found.
    files = sorted(
        SESSIONS.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:MAX_FILES]

    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        # Reverse: the last rate-limit record in a session is its most recent.
        for line in reversed(lines):
            if not RATE_LINE.search(line):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            stack = [payload]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    limits = node.get("rate_limits")
                    if isinstance(limits, dict):
                        windows = _windows_from(limits)
                        if windows:
                            print(json.dumps({"windows": windows}))
                            return 0
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)

    print(
        json.dumps(
            {
                "available": False,
                "reason": f"no rate_limits record in the {len(files)} newest transcripts; "
                "run any Codex command once to produce one",
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
