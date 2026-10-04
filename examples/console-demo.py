#!/usr/bin/env python3
"""Open the real console with fictional plans and isolated temporary state."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def demo_overlay():
    roster = json.loads((ROOT / "examples/access-overlay.example.json").read_text())
    pools = ("claude", "codex", "opencode-go", "chatgpt-work")
    roster["quota_pools"] = {p: roster["quota_pools"][p] for p in pools}
    for pool, price in zip(pools, (35, 25, 15, 45)):
        data = roster["quota_pools"][pool]
        data["plan"].update(name="Separate demo subscription", price=price, currency="USD",
                            billing="subscription", allowance=None)
        data["quota_refresh"] = None
        data.pop("model_pins", None)
        data.pop("shares_limits_with", None)
        data["quota_source"] = "Fictional screenshot fixture"
    roster["quota_pools"]["chatgpt-work"]["label"] = "ChatGPT Chat"
    roster["quota_pools"]["chatgpt-work"]["plan"]["name"] = "Demo ChatGPT account A"
    roster["quota_pools"]["codex"]["plan"]["name"] = "Demo ChatGPT account B"
    models = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku",
              "gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "kimi-k3", "glm-5.2", "minimax-m3"}
    roster["lanes"] = [lane for lane in roster["lanes"]
                       if lane.get("quota_pool") in pools and lane.get("model_key") in models]
    for key, level, name in (("demo-pro", "pro", "Latest Pro"),
                             ("demo-extra-high", "xhigh", "Latest Extra High"),
                             ("demo-high", "high", "Latest High")):
        chat = copy.deepcopy(roster["chatgpt_gateway"]["lane_template"])
        selector = "chatgpt:" + key
        chat.update(lane_id=selector, model=selector, model_key=selector,
                    worker_label=key, worker_row="Latest", worker_level=level,
                    max_parallel=1 if level == "pro" else 2,
                    access_status="verified", admission_status="active",
                    catalog_state="ready", selector=selector)
        roster["lanes"].append(chat)
        roster.setdefault("model_cards", {})[selector] = {
            "name": name, "pool": "chatgpt-work", "status": "current",
            "best_for": "Read-only text through a saved ChatGPT worker"}
    roster["quota_pools"]["chatgpt-work"]["pro_weekly_allowance"] = 120
    roster["model_evidence"] = {}
    roster.pop("chatgpt_gateway", None)
    roster["telemetry"] = {}
    roster["model_cards"] = {key: value for key, value in roster.get("model_cards", {}).items()
                             if key in models or key.startswith("chatgpt:demo-")}
    roster["catalogue_only"] = []
    return roster


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gaddi", default="gaddi", help="Gaddi CLI executable")
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="crossfeed-console-demo-") as folder:
        state = Path(folder)
        os.environ.update(ACCESS_OVERLAY=str(state / "overlay.json"), FLEET_STATE_DIR=str(state),
                          FLEET_NO_AUTO_REFRESH="1")
        sys.path.insert(0, str(ROOT / "scripts"))
        import console

        # Direct Codex defaults otherwise read the real CLI config. Pin this demo only.
        patcher = patch.object(console.fleetctl, "codex_default_model", return_value="gpt-6.1-sol")
        patcher.start()
        patch.object(console.fleetctl, "recent_model_runs", return_value=[]).start()

        overlay = demo_overlay()
        for lane in overlay["lanes"]:
            if lane.get("harness") == "chatgpt-chat":
                lane["auth"]["key_file"] = str(state / "unused-demo-key")
        (state / "overlay.json").write_text(json.dumps(overlay))
        now = console.fleetctl.utc_now()
        # Fictional completed requests populate the local Pro estimate without a gateway.
        receipts = [{"run_id": f"demo-{n}", "harness": "chatgpt-chat", "returncode": 0,
                     "selector": "chatgpt:demo-pro", "quota_pool": "chatgpt-work",
                     "ended_at": console.fleetctl.iso(now - dt.timedelta(days=2)),
                     "selected_model": "chatgpt:demo-pro"} for n in range(48)]
        (state / "runs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in receipts))
        runtime = {"switches": {"claude": "normal", "codex": "high", "opencode-go": "low"},
                   "quota_snapshots": {}}
        for pool, used, days in (("claude", 38, 4), ("codex", 24, 5), ("opencode-go", 76, 2)):
            runtime["quota_snapshots"][pool] = {
                "source": "demo", "observed_at": console.fleetctl.iso(now),
                "windows": {"weekly": {"used_percent": used, "window_minutes": 10080,
                            "label": "Weekly quota used",
                            "reset_at": console.fleetctl.iso(now + dt.timedelta(days=days)),
                            "will_last_to_reset": True}}}
        (state / "runtime.json").write_text(json.dumps(runtime))
        app = console.Console(state / "overlay.json", state, args.port)
        server = console.bind(app, args.port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        link = f"http://127.0.0.1:{app.port}/login?key={app.login_key}"
        # The one-use login key stays out of terminal output and saved screenshots.
        result = subprocess.run([args.gaddi, "--json", "open", link, "--group", "Crossfeed demo"],
                                capture_output=True, text=True)
        if result.returncode:
            server.shutdown()
            server.server_close()
            raise SystemExit("Gaddi could not open the demo tab. Check its browser connection.")
        opened = json.loads(result.stdout)
        print(json.dumps({"demo": True, "url": f"http://127.0.0.1:{app.port}/",
                          "tab": (opened.get("result") or {}).get("id")}), flush=True)
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
