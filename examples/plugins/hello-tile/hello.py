#!/usr/bin/env python3
"""The smallest useful plugin: one command, one tile document on stdout.

Run it yourself to see what Crossfeed sees:

    python3 hello.py
    python3 hello.py --bar

It uses only the standard library, reaches no network, and prints one JSON document. The state
directory Crossfeed gives it (CROSSFEED_PLUGIN_STATE) is where a real plugin would keep a cache.
"""
import json
import os
import sys


def hello():
    return {
        "title": "Hello",
        "status": "ok",
        "rows": [
            {"kind": "text", "text": "This tile comes from a plugin."},
            {"kind": "value", "label": "State dir", "value": "given" if os.environ.get("CROSSFEED_PLUGIN_STATE") else "missing"},
            {"kind": "link", "label": "Write your own", "url": "https://github.com/ABCastor/crossfeed-orchestrator/blob/main/docs/PLUGINS.md"},
        ],
    }


def bar():
    return {
        "title": "A pace bar",
        "status": "warn",
        "rows": [
            {"kind": "bar", "label": "Minutes", "used": 58, "pace": 41, "window": "Monthly",
             "resets_at": "2026-12-01T00:00:00Z"},
            {"kind": "value", "label": "Failed today", "value": "3", "tone": "warn"},
            {"kind": "sparkline", "label": "Runs", "values": [3, 5, 2, 8], "today": "8"},
        ],
    }


if __name__ == "__main__":
    print(json.dumps(bar() if "--bar" in sys.argv[1:] else hello()))
