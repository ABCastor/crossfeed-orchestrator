"""The real console, serving a fixed glance document, for the browser flows in tests/panel-flows.cjs.

It is the console's own page, routes, sign-in and preferences write; only the glance document and the
"read every source now" step are replaced, so a flow exercises the panel without any provider. Prints one
JSON line (port, sign-in path, preferences file, state directory) and serves until stopped.

    python3 tests/panel_flow_server.py <fixture.json> [<working-dir>]
"""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["FLEET_NO_AUTO_REFRESH"] = "1"


def main() -> None:
    fixture = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    work = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp(prefix="panel-flows-"))
    work.mkdir(parents=True, exist_ok=True)
    os.environ["FLEET_PANEL_PREFS"] = str(work / "panel.json")
    from tests import test_console
    console = test_console.console
    overlay = work / "overlay.json"
    overlay.write_text(json.dumps(test_console.base_overlay()), encoding="utf-8")
    app = console.Console(overlay, work / "state", 0)
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    calls = {"refresh": 0}

    def payload():
        document = json.loads(json.dumps(fixture))
        document["panel"] = console.panel_prefs.load()
        when = app._refreshed_at
        document["refreshed_at"] = dt.datetime.fromtimestamp(when, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if when else \
            stale.strftime("%Y-%m-%dT%H:%M:%SZ")
        document["__refresh_calls"] = calls["refresh"]
        return document

    def refresh_now():
        calls["refresh"] += 1
        import time
        app._refreshed_at = time.time()
        return {}

    app.panel_payload = payload
    app.refresh_now = refresh_now
    server = console.bind(app, 0)
    print(json.dumps({"port": app.port, "login": f"/login?key={app.login_key}", "prefs": os.environ["FLEET_PANEL_PREFS"],
                      "work": str(work)}), flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.stdin.read()  # stops when the flow script closes our stdin


if __name__ == "__main__":
    main()
