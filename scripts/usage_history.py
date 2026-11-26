"""Quota readings over time: a small append-only log beside runtime.json, for display only.

`runtime.json` keeps just the latest reading of each pool. Each refresh that stores a reading also
appends one line per window here (`usage-history.jsonl`), so the console can draw how an allowance
filled up. Routing never reads it. Nothing is back-filled: history starts when this file does.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

NAME = "usage-history.jsonl"
KEEP_S = 35 * 86400
HEARTBEAT_S = 1800        # an unchanged window is written again after this long, so a flat line stays flat
COMPACT_BYTES = 400_000
MAX_LINE = 512


def _epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        from datetime import datetime, timezone
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int((stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp())
    return None


def _lines(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                if len(raw) > MAX_LINE:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(row, dict) and isinstance(row.get("t"), int) and isinstance(row.get("p"), str)
                        and isinstance(row.get("w"), str) and type(row.get("u")) in (int, float)):
                    rows.append(row)
    except OSError:
        pass
    return rows


def record(state_dir: Path, pool: str, observed: dict[str, Any], now: float | None = None) -> int:
    """Append the windows of one stored reading; returns how many lines were written. Never raises."""
    import time

    try:
        when = int(time.time() if now is None else now)
        stamp = _epoch(observed.get("observed_at")) or when
        windows = observed.get("windows") or {}
        if not isinstance(windows, dict) or not windows:
            return 0
        path = state_dir / NAME
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        written = 0
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            last: dict[str, dict[str, Any]] = {}
            for row in _lines(path):
                if row["p"] == pool:
                    last[row["w"]] = row
            out = []
            for name, window in sorted(windows.items()):
                used = window.get("used_percent") if isinstance(window, dict) else None
                if type(used) not in (int, float) or not 0 <= used <= 100:
                    continue
                reset = _epoch(window.get("reset_at"))
                old = last.get(name)
                if old and old["u"] == used and old.get("r") == reset and stamp - old["t"] < HEARTBEAT_S:
                    continue
                if old and stamp <= old["t"]:
                    continue
                line = {"t": stamp, "p": pool, "w": name, "u": used}
                if reset:
                    line["r"] = reset
                if isinstance(window.get("window_minutes"), int):
                    line["m"] = window["window_minutes"]
                if isinstance(window.get("label"), str):
                    line["l"] = window["label"][:60]
                out.append(json.dumps(line, separators=(",", ":")))
            if out:
                os.write(fd, ("\n".join(out) + "\n").encode())
                written = len(out)
            if os.fstat(fd).st_size > COMPACT_BYTES:
                _compact(fd, path, when)
        finally:
            os.close(fd)
        return written
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _compact(fd: int, path: Path, now: int) -> None:
    """Rewrite in place while the caller holds the lock, so a waiting writer never appends to a dead file."""
    keep = [row for row in _lines(path) if row["t"] >= now - KEEP_S]
    data = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in keep).encode()
    os.ftruncate(fd, 0)
    os.write(fd, data)


def series(state_dir: Path, now: float, days: float = 30) -> dict[str, dict[str, dict[str, Any]]]:
    """{pool: {window: {label, minutes, points: [[epoch, used], ...], resets: [epoch, ...]}}}, oldest first."""
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for row in sorted(_lines(state_dir / NAME), key=lambda r: r["t"]):
        if row["t"] < now - days * 86400:
            continue
        entry = result.setdefault(row["p"], {}).setdefault(row["w"], {"label": None, "minutes": None, "points": [],
                                                                      "resets": []})
        entry["label"] = row.get("l") or entry["label"]
        entry["minutes"] = row.get("m") or entry["minutes"]
        entry["points"].append([row["t"], row["u"]])
        if isinstance(row.get("r"), int) and row["r"] not in entry["resets"]:
            entry["resets"].append(row["r"])
    return result
