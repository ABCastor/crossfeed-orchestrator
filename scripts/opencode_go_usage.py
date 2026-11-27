"""OpenCode Go usage, read from OpenCode's own usage endpoint.

`GET https://opencode.ai/zen/go/v1/usage` with the Go API key as a bearer token answers
`{"usage": {"rolling": M, "weekly": M, "monthly": M}}`, each meter
`{"status": "ok" | "rate-limited", "percent": n, "resetsAt": ISO}`. OpenCode enforces
20% of a model's monthly allowance per five hours, 50% per week and 100% per month;
the endpoint already reports each window as a percentage of its own limit.

The result is a quota snapshot in the shape every other source produces
(`windows.<name> = {used_percent, reset_at, window_minutes, label}`), or
`{"available": False, "kind": ..., "reason": ...}`. A rejected key, a missing
subscription and an unreadable answer stay distinct and never become 0%.

The key never leaves this module except in the Authorization header: it is not
logged, not put in a reason, not written to disk.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import shutil
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
SOURCE = "opencode-go-api"
KEY_ENV = "OPENCODE_API_KEY"
MAX_BODY_BYTES = 1_000_000
# Endpoint meter -> (snapshot window name, minutes, label). The names match the
# manual `snapshot` command, so both sources describe the pool the same way.
METERS = (
    ("rolling", "rolling_5h", 300, "5-hour"),
    ("weekly", "weekly", 10080, "Weekly"),
    ("monthly", "monthly", 43200, "Monthly"),
)
REQUIRED_METERS = {"rolling", "weekly"}


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def unavailable(kind: str, reason: str) -> dict[str, Any]:
    return {"available": False, "source": SOURCE, "kind": kind, "reason": reason}


def parse_usage(body: str, observed_at: dt.datetime | None = None) -> dict[str, Any] | None:
    """The snapshot for a successful answer, or None when the body is not a usage report."""
    if not body or len(body) > MAX_BODY_BYTES:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    windows: dict[str, Any] = {}
    idle: dict[str, Any] = {}
    for meter_name, name, minutes, label in METERS:
        meter = usage.get(meter_name)
        percent = meter.get("percent") if isinstance(meter, dict) else None
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or percent != percent:
            if meter_name in REQUIRED_METERS:
                return None
            continue
        used = int(round(min(100.0, max(0.0, float(percent)))))
        # A rate-limited window is exhausted whatever the rounded percent says.
        if meter.get("status") == "rate-limited":
            used = 100
        reset = _parse_time(meter.get("resetsAt"))
        entry: dict[str, Any] = {"used_percent": used, "window_minutes": minutes, "label": label}
        if reset is None:
            # No reset clock: shown as a reading, never routed on (same as other sources).
            idle[name] = entry
            continue
        entry["reset_at"] = _iso(reset)
        windows[name] = entry
    if not windows:
        return None
    snapshot: dict[str, Any] = {
        "available": True,
        "source": SOURCE,
        "observed_at": _iso(observed_at or dt.datetime.now(dt.timezone.utc)),
        "precision_percentage_points": 1,
        "windows": windows,
    }
    if idle:
        snapshot["idle_windows"] = idle
    return snapshot


def _error_type(body: str) -> str | None:
    if not body or len(body) > 4000:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    return kind if isinstance(kind, str) else None


def classify_failure(status: int, body: str) -> dict[str, Any]:
    """Error names are stable across console migrations; status codes are not, so names win."""
    error = _error_type(body)
    if error == "EntitlementError" or (error is None and status == 403):
        return unavailable("no-subscription", "OpenCode says this key has no Go subscription")
    if error == "AuthError" or (error is None and status == 401):
        return unavailable("unauthorized", "OpenCode rejected the Go API key")
    return unavailable("failed", f"OpenCode Go usage request failed (HTTP {status})")


def _opencode_auth_file() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "opencode" / "auth.json"


def _local_key_file() -> str | None:
    """`~/.config/pi-keys/opencode` (or $PI_KEYS_DIR/opencode): owner-only, one line. Never read otherwise."""
    path = Path(os.environ.get("PI_KEYS_DIR") or "~/.config/pi-keys").expanduser() / "opencode"
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 512:
            return None
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return key if key and "\n" not in key and " " not in key else None


def _providers_module():
    spec = importlib.util.spec_from_file_location("fleet_providers_keys", Path(__file__).with_name("providers.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_key(config: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """(key, None) or (None, unavailable-result). Never returns the key inside a reason.

    `key` in the config is a reference the provider setup already understands: an
    environment variable name, an absolute file path, or `op://vault-id/item/field`
    read through 1Password at run time. A declared reference that fails is reported
    as such, with no fallback. Without one: $OPENCODE_API_KEY, then the Go key
    OpenCode itself keeps in its auth.json.
    """
    declared = config.get("key")
    if declared and str(declared).startswith("op://") and not shutil.which("op"):
        # A machine without 1Password (the Linux server) keeps the same key as a private one-line file.
        key = _local_key_file()
        if key:
            return key, None
    if declared:
        try:
            key = _providers_module().resolve_key(str(declared))
        except Exception:  # noqa: BLE001 - the message must not echo anything read
            return None, unavailable("no-key", "the declared Go key reference could not be read")
        return (key, None) if key else (None, unavailable("no-key", "the declared Go key reference is empty"))
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key, None
    try:
        auth = json.loads(_opencode_auth_file().read_text(encoding="utf-8"))
        record = auth.get("opencode-go") if isinstance(auth, dict) else None
        if isinstance(record, dict) and record.get("type") == "api":
            key = str(record.get("key") or "").strip()
            if key:
                return key, None
    except (OSError, ValueError):
        pass
    return None, unavailable("no-key", f"no Go API key: set `key` in quota_refresh, ${KEY_ENV}, "
                                       "or sign OpenCode in to OpenCode Go")


def observe(
    config: dict[str, Any],
    timeout: int = 5,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One read of the usage endpoint, as a snapshot or an explicit unavailable result."""
    url = str(config.get("url") or USAGE_URL)
    if not url.startswith("https://"):
        return unavailable("failed", "the Go usage url must be https (the key travels in a header)")
    key, failure = resolve_key(config)
    if failure:
        return failure
    request = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "crossfeed-orchestrator",
    })
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            body = response.read(MAX_BODY_BYTES + 1).decode("utf-8", "replace")
            content_type = str(response.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as error:
        try:
            body = error.read(4001).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        finally:
            error.close()
        return classify_failure(int(error.code), body)
    except Exception as error:  # noqa: BLE001 - a fixed message: exception text can carry the request
        return unavailable("failed", f"OpenCode Go usage request failed ({type(error).__name__})")
    snapshot = parse_usage(body)
    if snapshot:
        return snapshot
    # A redirect to a sign-in page arrives as a successful HTML answer.
    if "text/html" in content_type.lower() or body.lstrip().lower().startswith(("<!doctype", "<html")):
        return unavailable("unauthorized", "OpenCode answered with a sign-in page, not usage")
    return unavailable("malformed", "OpenCode Go usage answer was not a usage report")
