#!/usr/bin/env python3
"""One failed provider alongside working quota providers, without network access."""
import datetime as dt
import json
import os
import sys

provider = sys.argv[sys.argv.index("--provider") + 1]
now = dt.datetime.now(dt.timezone.utc)
mode = os.environ.get("CODEXBAR_GO_RESPONSE", "error")
if provider == "opencodego" and mode != "ok":
    print("OpenCode Go usage page payload missing usage fields", file=sys.stderr)
    print("OpenCode Go returned 500", file=sys.stderr)
    if mode == "stderr":
        print("Error: OpenCode Go API error: HTTP 500", file=sys.stderr)
    elif mode == "error":
        print(json.dumps([{"provider": provider, "error": {
            "message": "OpenCode Go API error: HTTP 500", "kind": "provider"}}]))
    if mode != "nonzero-usage":
        sys.exit(1)

print(json.dumps([{"provider": provider, "usage": {
    "updatedAt": now.isoformat(),
    "primary": {"usedPercent": 20, "windowMinutes": 10080,
                "resetsAt": (now + dt.timedelta(days=2)).isoformat()}}}]))
sys.exit(1 if provider == "opencodego" and mode == "nonzero-usage" else 0)
