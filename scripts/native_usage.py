"""Quota readings straight from each provider, through the provider's own tool or sign-in.

Four readers, one per subscription the fleet meters, each returning the snapshot shape every
quota source produces (`windows.<name> = {used_percent, reset_at, window_minutes, label}`) or
`{"available": False, "kind": ..., "reason": ...}`:

- codex: Codex's own app server (`codex app-server`, `account/rateLimits/read`). Codex keeps
  its sign-in and renews it; this reader never sees a token.
- codex-oauth: CodexBar's usage endpoint with Codex's existing ChatGPT sign-in. No renewal.
- claude: Anthropic's usage endpoint (`GET https://api.anthropic.com/api/oauth/usage`) with the
  default subscription sign-in Claude Code keeps (macOS Keychain item `Claude Code-credentials`,
  else `~/.claude/.credentials.json`), never a bridge token. An expired sign-in is not renewed here.
- antigravity: an operator export, a running local language server, or an explicitly selected
  existing ACP/CLI OAuth profile. Never starts the CLI or an interactive login.
- copilot: GitHub's Copilot entitlement endpoint (`GET https://api.github.com/copilot_internal/user`)
  with the GitHub CLI's own sign-in (`gh auth token`) or a declared key reference.

Credentials go only to the relevant provider on fixed HTTPS hosts. Never written or logged.
A rejected sign-in, a missing tool and an unreadable answer stay distinct and never become 0%.
"""
from __future__ import annotations

import calendar
import datetime as dt
import http.client
import importlib.util
import json
import os
import re
import selectors
import shutil
import signal
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
from pathlib import Path
from typing import Any, Callable

MAX_BODY_BYTES = 2_000_000

# The Codex app server and OAuth endpoints have their own time limits.
DEFAULT_TIMEOUTS = {"codex": 20, "codex-oauth": 10, "claude": 10, "copilot": 10}


def reader_timeout(oracle: str, config: dict[str, Any], timeout: int) -> int:
    declared = config.get("timeout_s")
    if isinstance(declared, (int, float)) and not isinstance(declared, bool) and declared > 0:
        return int(declared)
    return max(int(timeout), DEFAULT_TIMEOUTS[oracle])


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def _percent(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return int(round(min(100.0, max(0.0, float(value)))))


def _label(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    label = " ".join(text.split())
    return label if label and "@" not in label and len(label) <= 40 else None


def unavailable(source: str, kind: str, reason: str) -> dict[str, Any]:
    return {"available": False, "source": source, "kind": kind, "reason": reason}


def _snapshot(source: str, windows: dict[str, Any], idle: dict[str, Any],
              plan: str | None = None, observed_at: dt.datetime | None = None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "available": True,
        "source": source,
        "observed_at": _iso(observed_at or _now()),
        "precision_percentage_points": 1,
        "windows": windows,
    }
    if idle:
        snapshot["idle_windows"] = idle
    plan = _label(plan)
    if plan:
        snapshot["plan"] = plan
    return snapshot


def _add(windows: dict[str, Any], idle: dict[str, Any], name: str, used: int | None,
         reset: dt.datetime | None, minutes: int | None, label: str | None) -> None:
    """A window with a reset clock can be routed on; one without is shown, never routed on."""
    if used is None:
        return
    entry: dict[str, Any] = {"used_percent": used}
    if minutes:
        entry["window_minutes"] = int(minutes)
    label = _label(label)
    if label:
        entry["label"] = label
    if reset is None:
        idle[name] = entry
        return
    entry["reset_at"] = _iso(reset)
    windows[name] = entry


def _duration_name(minutes: int | None) -> str:
    if minutes == 300:
        return "session"
    if minutes == 10080:
        return "weekly"
    return f"window_{minutes}m" if minutes else "window"


def _duration_label(minutes: int | None) -> str | None:
    return {300: "Session", 10080: "Weekly"}.get(minutes or 0)


def _http_get(url: str, headers: dict[str, str], timeout: int,
              opener: Callable[..., Any] | None) -> tuple[int, str]:
    """(status, body). Exceptions other than HTTP errors propagate for the caller's fixed message."""
    request = urllib.request.Request(url, method="GET", headers=headers)
    open_url = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open
    try:
        with open_url(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200) or 200), \
                response.read(MAX_BODY_BYTES + 1).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read(4001).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        finally:
            error.close()
        return int(error.code), body


def _json(body: str) -> Any:
    if not body or len(body) > MAX_BODY_BYTES:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None


def _stop_group(proc: subprocess.Popen) -> None:
    """End the reader's own child and anything it started (its process group, by its PID)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            break
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(proc.pid, signal.SIGKILL)   # stragglers left in the group after the leader exited
    except (ProcessLookupError, PermissionError):
        pass
    if proc.poll() is None:
        proc.wait(timeout=2)


# ---- Codex ----------------------------------------------------------------------------------------
CODEX_SOURCE = "codex-app-server"
CODEX_HANDSHAKE = (
    {"id": 1, "method": "initialize",
     "params": {"clientInfo": {"name": "crossfeed-quota", "version": "1"},
                "capabilities": {"experimentalApi": True}}},
    {"method": "initialized", "params": {}},
    {"id": 2, "method": "account/rateLimits/read", "params": {}},
)


def parse_codex(result: Any, observed_at: dt.datetime | None = None) -> dict[str, Any] | None:
    """Every limit bucket Codex reports. Names follow the window's length, not its slot: today
    Codex's `primary` slot holds the weekly window."""
    if not isinstance(result, dict):
        return None
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        single = result.get("rateLimits")
        buckets = {str(single.get("limitId") or "codex"): single} if isinstance(single, dict) else {}
    windows: dict[str, Any] = {}
    idle: dict[str, Any] = {}
    plan = None
    for limit_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        general = limit_id == "codex"
        if general and isinstance(bucket.get("planType"), str):
            plan = bucket["planType"].strip().capitalize() or None
        scope = _label(bucket.get("limitName")) or limit_id
        for slot in ("primary", "secondary"):
            window = bucket.get(slot)
            if not isinstance(window, dict):
                continue
            minutes = window.get("windowDurationMins")
            minutes = int(minutes) if isinstance(minutes, (int, float)) and minutes > 0 else None
            used = _percent(window.get("usedPercent"))
            name = _duration_name(minutes) if general else f"{limit_id}-{_duration_name(minutes)}"
            label = _duration_label(minutes) if general else f"{scope} only"
            _add(windows, idle, name, used, _parse_time(window.get("resetsAt")), minutes, label)
    if not windows and not idle:
        return None
    return _snapshot(CODEX_SOURCE, windows, idle, plan, observed_at)


def _codex_exchange(binary: str, timeout: int) -> tuple[str, Any]:
    """('ok', result) | (kind, reason). One bounded app-server session; no model turn."""
    try:
        proc = subprocess.Popen(
            [binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except FileNotFoundError:
        return "no-tool", "Codex is not installed (no `codex` command)"
    except OSError as exc:
        return "failed", f"Codex app server could not start ({type(exc).__name__})"
    selector = selectors.DefaultSelector()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write("".join(json.dumps(m) + "\n" for m in CODEX_HANDSHAKE).encode())
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return "failed", "Codex app server closed before answering"
        deadline = time.monotonic() + timeout
        selector.register(proc.stdout, selectors.EVENT_READ)
        buffer = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "failed", f"Codex app server gave no usage within {timeout}s"
            if not selector.select(remaining):
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                return "failed", "Codex app server closed before answering"
            buffer += chunk
            if len(buffer) > MAX_BODY_BYTES:
                return "malformed", "Codex app server answer was too large"
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict) or message.get("id") != 2:
                    continue
                if "error" in message:
                    error = message["error"] if isinstance(message["error"], dict) else {}
                    text = str(error.get("message") or "").lower()
                    if "auth" in text or "login" in text or "sign" in text:
                        return "unauthorized", "Codex is not signed in"
                    return "failed", "Codex could not read its rate limits"
                return "ok", message.get("result")
    finally:
        selector.close()
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        _stop_group(proc)


def observe_codex(config: dict[str, Any], timeout: int = 5,
                  exchange: Callable[[str, int], tuple[str, Any]] | None = None) -> dict[str, Any]:
    binary = str(config.get("binary") or os.environ.get("CODEX_BIN") or "codex")
    kind, result = (exchange or _codex_exchange)(binary, reader_timeout("codex", config, timeout))
    if kind != "ok":
        return unavailable(CODEX_SOURCE, kind, str(result))
    return parse_codex(result) or unavailable(CODEX_SOURCE, "malformed", "Codex reported no rate-limit windows")


# ---- Codex OAuth (CodexBar v0.67.0; see NOTICE) ----------------------------------------------------
CODEX_OAUTH_SOURCE = "codex-oauth-usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_codex_oauth(payload: Any, observed_at: dt.datetime | None = None) -> dict[str, Any] | None:
    """Map only quota windows; reset credits and spend-control data are not time windows."""
    root = _mapping(payload)
    windows: dict[str, Any] = {}
    idle: dict[str, Any] = {}
    limits = [(None, None, root.get("rate_limit"))]
    additional = root.get("additional_rate_limits")
    if isinstance(additional, list):
        for raw in additional:
            row = _mapping(raw)
            scope = _label(row.get("limit_name"))
            feature = _label(row.get("metered_feature"))
            key = _slug(feature or scope or "")
            if key:
                limits.append((key, scope or feature, row.get("rate_limit")))
    for key, scope, limit in limits:
        for slot in ("primary_window", "secondary_window"):
            win = _mapping(_mapping(limit).get(slot))
            seconds, reset, used = (win.get(field) for field in ("limit_window_seconds", "reset_at", "used_percent"))
            if (type(seconds) is not int or not 0 < seconds <= 366 * 86400
                    or type(reset) is not int or reset <= 0 or type(used) is not int or not 0 <= used <= 100):
                continue
            minutes = seconds // 60 or None
            name = _duration_name(minutes)
            label = _duration_label(minutes)
            if key:
                name, label = f"{key}-{name}", f"{scope} only"
            if name in windows or name in idle:
                name = f"{name}-{slot}"
            _add(windows, idle, name, used, _parse_time(reset), minutes, label)
    if not windows:
        return None
    plan = root.get("plan_type")
    return _snapshot(CODEX_OAUTH_SOURCE, windows, idle,
                     plan.strip().capitalize() if isinstance(plan, str) else None, observed_at)


def _owner_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("not an owner-only file")
        return handle.read(MAX_BODY_BYTES + 1)


def observe_codex_oauth(config: dict[str, Any], timeout: int = 5,
                        opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Read Codex's already-issued token. Never start Codex or renew/sign in."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        credentials = _mapping(_json(_owner_file(home / "auth.json").decode("utf-8")))
    except (OSError, ValueError, RecursionError):
        credentials = {}
    tokens = _mapping(credentials.get("tokens"))
    token, account = tokens.get("access_token"), tokens.get("account_id")
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,16000}", token):
        return unavailable(CODEX_OAUTH_SOURCE, "no-auth", "No owner-only Codex ChatGPT sign-in available")
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "CodexBar", "Accept": "application/json"}
    if isinstance(account, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account):
        headers["ChatGPT-Account-Id"] = account
    try:
        status, body = _http_get(CODEX_USAGE_URL, headers, reader_timeout("codex-oauth", config, timeout), opener)
        if status == 200:
            return parse_codex_oauth(_json(body)) or unavailable(
                CODEX_OAUTH_SOURCE, "malformed", "Codex reported no OAuth rate-limit windows")
        if status in (401, 403):
            return unavailable(CODEX_OAUTH_SOURCE, "unauthorized", "Codex ChatGPT sign-in was rejected; no renewal attempted")
        return unavailable(CODEX_OAUTH_SOURCE, "failed", f"Codex usage endpoint returned HTTP {status}")
    except (OSError, ValueError, http.client.HTTPException, RecursionError):
        return unavailable(CODEX_OAUTH_SOURCE, "failed", "Codex usage request failed")


# ---- Claude ---------------------------------------------------------------------------------------
CLAUDE_SOURCE = "claude-oauth-usage"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_LIMIT_KINDS = {"session": ("session", 300, "Session"), "weekly_all": ("weekly", 10080, "Weekly")}
CLAUDE_FALLBACK_FIELDS = (
    ("five_hour", "session", 300, "Session"),
    ("seven_day", "weekly", 10080, "Weekly"),
    ("seven_day_sonnet", "claude-weekly-scoped-sonnet", 10080, "Sonnet only"),
    ("seven_day_opus", "claude-weekly-scoped-opus", 10080, "Opus only"),
    ("seven_day_oauth_apps", "claude-weekly-scoped-oauth-apps", 10080, "OAuth apps only"),
    ("seven_day_cowork", "claude-weekly-scoped-cowork", 10080, "Cowork only"),
)
SUBSCRIPTION_NAMES = {"max": "Claude Max", "pro": "Claude Pro", "team": "Claude Team", "enterprise": "Claude Enterprise"}


def _slug(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def parse_claude(body: str, plan: str | None = None, observed_at: dt.datetime | None = None) -> dict[str, Any] | None:
    payload = _json(body)
    if not isinstance(payload, dict):
        return None
    windows: dict[str, Any] = {}
    idle: dict[str, Any] = {}
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            used = _percent(item.get("percent"))
            reset = _parse_time(item.get("resets_at"))
            if kind in CLAUDE_LIMIT_KINDS:
                name, minutes, label = CLAUDE_LIMIT_KINDS[kind]
                _add(windows, idle, name, used, reset, minutes, label)
            elif kind == "weekly_scoped":
                scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
                model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
                surface = scope.get("surface") if isinstance(scope.get("surface"), dict) else {}
                what = _label(model.get("display_name")) or _label(surface.get("display_name"))
                if what:
                    _add(windows, idle, f"claude-weekly-scoped-{_slug(what)}", used, reset, 10080, f"{what} only")
    if not windows and not idle:
        for field, name, minutes, label in CLAUDE_FALLBACK_FIELDS:
            window = payload.get(field)
            if isinstance(window, dict):
                _add(windows, idle, name, _percent(window.get("utilization")),
                     _parse_time(window.get("resets_at")), minutes, label)
    if not windows and not idle:
        return None
    return _snapshot(CLAUDE_SOURCE, windows, idle, plan, observed_at)


def _claude_credential_records() -> list[dict[str, Any]]:
    """The default interactive subscription profile, not a bridge's CLAUDE_CONFIG_DIR."""
    records = []
    if shutil.which("security"):
        try:
            found = subprocess.run(
                ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if found.returncode == 0:
                records.append(json.loads(found.stdout))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        records.append(_json(_owner_file(Path.home() / ".claude" / ".credentials.json").decode("utf-8")))
    except (OSError, ValueError, RecursionError):
        pass
    return [r for r in records if isinstance(r, dict)]


def claude_token(records: list[dict[str, Any]] | None = None,
                 now: dt.datetime | None = None) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """(token, plan, None) or (None, None, unavailable-result): the unexpired Claude Code sign-in."""
    now = now or _now()
    expired = False
    for record in records if records is not None else _claude_credential_records():
        oauth = record.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            continue
        plan = SUBSCRIPTION_NAMES.get(str(oauth.get("subscriptionType") or "").lower())
        if not plan:
            continue
        expires = _parse_time(oauth.get("expiresAt", 0) / 1000) if type(oauth.get("expiresAt")) in (int, float) else None
        if expires is None:
            continue
        if (expires - now).total_seconds() <= 30:
            expired = True
            continue
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,16000}", token):
            continue
        tier = str(oauth.get("rateLimitTier") or "")
        if "20x" in tier:
            plan += " 20x"
        elif "5x" in tier:
            plan += " 5x"
        return token, plan, None
    if expired:
        return None, None, unavailable(CLAUDE_SOURCE, "expired",
                                       "Claude Code's subscription sign-in has expired; sign in without an environment token")
    return None, None, unavailable(CLAUDE_SOURCE, "no-key", "No usable Claude Code subscription sign-in")


def observe_claude(config: dict[str, Any], timeout: int = 5, opener: Callable[..., Any] | None = None,
                   records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    token, plan, failure = claude_token(records)
    if failure:
        return failure
    try:
        status, body = _http_get(CLAUDE_USAGE_URL, {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "crossfeed-orchestrator",
        }, reader_timeout("claude", config, timeout), opener)
    except Exception as error:  # noqa: BLE001 - fixed message: exception text can carry the request
        return unavailable(CLAUDE_SOURCE, "failed", f"Claude usage request failed ({type(error).__name__})")
    if status in (401, 403):
        return unavailable(CLAUDE_SOURCE, "unauthorized", "Anthropic rejected Claude Code's sign-in for usage")
    if status == 429:
        return unavailable(CLAUDE_SOURCE, "rate-limited", "Anthropic asked to slow down usage reads (HTTP 429)")
    if status != 200:
        return unavailable(CLAUDE_SOURCE, "failed", f"Claude usage request failed (HTTP {status})")
    return parse_claude(body, plan) or unavailable(CLAUDE_SOURCE, "malformed", "Claude usage answer had no limits")


# ---- Antigravity ----------------------------------------------------------------------------------
AGY_SOURCE = "antigravity-cli"
AGY_REPORT_MAX_AGE_S = 15 * 60
AGY_WINDOW_MINUTES = {"5h": 300, "weekly": 10080, "daily": 1440}
AGY_WINDOW_WORDS = {"5h": "5-hour", "weekly": "weekly", "daily": "daily"}


def _agy_family(group_name: str) -> tuple[str, str]:
    """(window-name slug, label word) for a quota group. The slugs keep the names pools already
    filter on (gemini, 3p) and give Argon its own (argon), so no family borrows another's limit."""
    lowered = group_name.lower()
    if "argon" in lowered:
        return "argon", "Argon"
    if "claude" in lowered or "gpt" in lowered:
        return "3p", "Claude/GPT"
    if "gemini" in lowered:
        return "gemini", "Gemini"
    return _slug(group_name) or "other", " ".join(group_name.split()[:2]) or "Antigravity"


def parse_antigravity(stdout: str, observed_at: dt.datetime | None = None) -> dict[str, Any] | None:
    """Every bucket of every group, named `antigravity-quota-summary-<family>-<window>`
    (gemini-5h, 3p-weekly, argon-weekly), so a pool can keep exactly its own family."""
    payload = _json(stdout.strip())
    if not isinstance(payload, dict) or payload.get("status") != "SUCCESS":
        return None
    command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
    data = command.get("data") if isinstance(command.get("data"), dict) else {}
    groups = data.get("groups")
    if command.get("name") != "usage" or not isinstance(groups, list):
        return None
    windows: dict[str, Any] = {}
    idle: dict[str, Any] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        slug, family = _agy_family(str(group.get("name") or ""))
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or not isinstance(bucket.get("id"), str):
                continue
            remaining = bucket.get("remaining_fraction")
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
                continue
            window = str(bucket.get("window") or "")
            minutes = AGY_WINDOW_MINUTES.get(window)
            label = f"{family} {AGY_WINDOW_WORDS.get(window, window)}".strip()
            name = f"antigravity-quota-summary-{slug}-{window}" if window else ""
            if not window or name in windows or name in idle:
                name = f"antigravity-quota-summary-{_slug(bucket['id'])}"   # an unknown or second bucket keeps its id
            reset = _parse_time(bucket.get("reset_time"))
            observed = observed_at or _now()
            # An untouched window reports a reset one full window from now, moving with every read:
            # it has not started, so it is shown and never routed on (as other sources do).
            if float(remaining) >= 1.0 and reset and minutes and \
                    (reset - observed).total_seconds() >= minutes * 60 - 300:
                reset = None
            _add(windows, idle, name, _percent(100.0 * (1.0 - float(remaining))), reset, minutes, label)
    if not windows and not idle:
        return None
    return _snapshot(AGY_SOURCE, windows, idle, None, observed_at)


# CodexBar v0.67.0 local quota summary RPC and JSON decoder; see NOTICE.
AGY_LOCAL_SOURCE = "antigravity-local"
AGY_OAUTH_SOURCE = "antigravity-oauth-usage"
AGY_OAUTH_HOSTS = ("daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com")
AGY_OAUTH_QUOTA = "/v1internal:retrieveUserQuotaSummary"
AGY_OAUTH_TOKEN_HOST = "oauth2.googleapis.com"
AGY_LOCAL_QUOTA = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
_AGY_BUCKET = re.compile(r"(gemini|3p|argon)-(5h|weekly|daily)\Z")
_AGY_PREFIX = "antigravity-quota-summary-"


def _agy_bucket_id(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    bucket = value.strip().lower()
    if bucket == "gemini-argon-wtu-weekly":
        return "argon", "weekly"
    match = _AGY_BUCKET.fullmatch(bucket)
    return match.groups() if match else None


def _agy_store(windows, idle, family, cadence, used, reset):
    name = f"{_AGY_PREFIX}{family}-{cadence}"
    label = {"gemini": "Gemini", "3p": "Claude/GPT", "argon": "Argon"}[family]
    _add(windows, idle, name, _percent(used), reset, AGY_WINDOW_MINUTES[cadence],
         f"{label} {AGY_WINDOW_WORDS[cadence]}")
    return name


def parse_antigravity_local(payload: Any, observed_at: dt.datetime | None = None, *,
                            source: str = AGY_LOCAL_SOURCE) -> dict[str, Any] | None:
    """A known shared-bucket ID is required; per-model quotas cannot substitute for it."""
    root = _mapping(payload)
    if root.get("code") not in (None, 0, "0", "OK"):
        return None
    summary = _mapping(root.get("response") or root.get("summary") or root)
    groups = summary.get("groups")
    if source == AGY_OAUTH_SOURCE and isinstance(summary.get("buckets"), list):
        if not isinstance(groups, list) or not any(_mapping(g).get("buckets") for g in groups):
            groups = [summary]
    if not isinstance(groups, list):
        return None
    observed = observed_at or _now()
    windows: dict[str, Any] = {}
    idle: dict[str, Any] = {}
    for raw in groups:
        buckets = _mapping(raw).get("buckets")
        if not isinstance(buckets, list):
            continue
        for item in buckets:
            bucket = _mapping(item)
            if bucket.get("disabled"):
                continue
            family_cadence = _agy_bucket_id(bucket.get("bucketId") or bucket.get("id"))
            if not family_cadence:
                continue
            family, cadence = family_cadence
            if source == AGY_OAUTH_SOURCE and bucket.get("window") not in (None, cadence):
                continue
            name = f"{_AGY_PREFIX}{family}-{cadence}"
            # Duplicate IDs make the selected quota ambiguous; never let one silently win.
            if name in windows or name in idle:
                return None
            remaining = _mapping(bucket.get("remaining"))
            fraction = bucket.get("remainingFraction")
            if fraction is None:
                fraction = remaining.get("remainingFraction")
                if fraction is None and remaining.get("case") == "remainingFraction":
                    fraction = remaining.get("value")
            if type(fraction) not in (int, float) or not 0 <= fraction <= 1:
                continue
            reset = _parse_time(bucket.get("resetTime"))
            if (source == AGY_OAUTH_SOURCE and fraction == 1 and not bucket.get("description")
                    and reset and abs((reset - observed).total_seconds() - AGY_WINDOW_MINUTES[cadence] * 60) <= 60):
                reset = None  # an unused window's synthetic near-now + duration clock
            _agy_store(windows, idle, family, cadence, 100 * (1 - fraction), reset)
            if name in idle and fraction != 1:
                idle[name]["reset_unknown"] = True
    return _snapshot(source, windows, idle, None, observed) if windows or idle else None


def _agy_reuse_target(deadline: float) -> list[tuple[int, str]]:
    """Return listening loopback ports on exactly one recognised, same-UID process.

    No spawn or CLI discovery invocation: ps/lsof only inspect an existing process.
    Ignore CLI overrides and never pass CSRF data on a subprocess command line.
    """
    def run(argv):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, errors="replace", timeout=remaining, check=True).stdout

    rows = run(["/bin/ps", "-axww", "-o", "pid=,uid=,comm="])
    cli = (Path.home() / ".local/bin/agy").resolve()
    candidates = []
    for line in rows.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or fields[1] != str(os.getuid()) or not fields[0].isdigit():
            continue
        pid, _, executable = fields
        path = Path(executable)
        desktop = (executable.startswith(("/Applications/Antigravity.app/Contents/",
                                         "/Applications/Antigravity IDE.app/Contents/",
                                         "/Applications/Gemini.app/Contents/"))
                   and re.fullmatch(r"language[-_]server(?:[-_][a-z0-9]+)*", path.name))
        if path.is_absolute() and (path == cli or desktop):
            candidates.append((pid, executable, bool(desktop)))
    # Two servers might be signed into different Google accounts. Do not guess.
    if len(candidates) != 1:
        return []
    pid, executable, desktop = candidates[0]
    args = run(["/bin/ps", "-ww", "-p", pid, "-o", "command="]).strip()
    if not (args == executable or args.startswith(executable + " ") or
            args == cli.name or args.startswith(cli.name + " ")):
        return []
    match = re.search(r"(?:^|\s)--csrf_token(?:=|\s+)([A-Za-z0-9._-]{1,512})(?=\s|$)", args)
    if desktop and not match:
        return []
    ports = run(["/usr/sbin/lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN", "-Fn"])
    result = []
    for raw in ports.splitlines():
        match_port = re.fullmatch(r"n127\.0\.0\.1:(\d{1,5})", raw)
        if match_port and 0 < (port := int(match_port[1])) <= 65535:
            result.append((port, match[1] if match else ""))
    return list(dict.fromkeys(result))[:8]


def _agy_local_request(port: int, csrf: str, timeout: float) -> Any:
    # AGY uses a self-signed TLS certificate. Only connect to 127.0.0.1 and a
    # listener on the selected process; http.client follows no redirects/proxy.
    conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=timeout,
                                       context=ssl._create_unverified_context())
    headers = {"Content-Type": "application/json", "Connect-Protocol-Version": "1"}
    if csrf:
        headers["X-Codeium-Csrf-Token"] = csrf
    try:
        conn.request("POST", AGY_LOCAL_QUOTA, b'{"forceRefresh":true}', headers)
        with conn.getresponse() as reply:
            if reply.status == 200:
                return _json(reply.read(MAX_BODY_BYTES + 1).decode("utf-8"))
        return None
    finally:
        conn.close()


def _observe_running_antigravity(timeout: int) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(1, min(timeout, 8))
    try:
        targets = _agy_reuse_target(deadline)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for port, csrf in targets:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = parse_antigravity_local(_agy_local_request(port, csrf, min(remaining, 2)))
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            continue
        if result:
            return result
    return None


def _agy_google_post(host: str, path: str, data: bytes, headers: dict[str, str], timeout: float) -> tuple[int, Any]:
    # Only the fixed Google hosts below; http.client does not follow redirects or use cookies or proxies.
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.request("POST", path, data, headers)
        with conn.getresponse() as reply:
            return reply.status, (_json(reply.read(MAX_BODY_BYTES + 1).decode("utf-8")) if reply.status == 200 else None)
    finally:
        conn.close()


def _agy_oauth_request(host: str, token: str, timeout: float, project: str | None = None) -> Any:
    body = json.dumps({"project": project} if project else {}, separators=(",", ":")).encode()
    _, payload = _agy_google_post(host, AGY_OAUTH_QUOTA, body, {
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "antigravity",
    }, timeout)
    return payload


def _agy_oauth_field(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[!-~]{1,16000}", value))


def _agy_acp_access(doc: dict[str, Any], timeout: float) -> tuple[str | None, str | None, str]:
    # Standard OAuth refresh for an existing ACP profile, as in T3 Code's quota reader.
    # Do not send refresh credentials to quota hosts or retain/save any returned credential.
    form = {key: doc.get(key) for key in ("client_id", "client_secret", "refresh_token")}
    if not all(_agy_oauth_field(value) for value in form.values()):
        return None, None, "no-auth"
    project = doc.get("project_id")
    if project not in (None, "") and not _agy_oauth_field(project):
        return None, None, "no-auth"
    form["grant_type"] = "refresh_token"
    status, data = _agy_google_post(AGY_OAUTH_TOKEN_HOST, "/token", urlencode(form).encode("ascii"),
                                   {"Content-Type": "application/x-www-form-urlencoded"}, timeout)
    token = _mapping(data).get("access_token")
    if status in {400, 401, 403}:
        return None, None, "unauthorized"
    if not _agy_oauth_field(token):
        return None, None, "failed"
    return token, project, ""


def _observe_antigravity_oauth(path: Any, timeout: int, *, acp: bool = False) -> dict[str, Any]:
    """Use one explicitly selected profile. No account discovery or fallback."""
    deadline = time.monotonic() + max(1, min(timeout, 10))
    source = AGY_OAUTH_SOURCE
    try:
        path = Path(path).expanduser()
        if not path.is_absolute():
            raise ValueError
        doc = _mapping(_json(_owner_file(path).decode("utf-8")))
    except (TypeError, OSError, ValueError, RuntimeError):
        return unavailable(source, "no-auth", "No readable Antigravity OAuth session")
    project = None
    if acp:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return unavailable(source, "failed", "Antigravity OAuth exchange timed out")
        try:
            token, project, failure = _agy_acp_access(doc, remaining / 2)
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            failure = "failed"
        if failure:
            return unavailable(source, failure, {
                "no-auth": "No usable Antigravity ACP sign-in",
                "unauthorized": "Google rejected the selected Antigravity ACP sign-in",
                "failed": "Antigravity ACP OAuth exchange failed",
            }[failure])
    else:
        fields = _mapping(doc.get("token"))
        token, expires = fields.get("access_token"), _parse_time(fields.get("expiry"))
        if not _agy_oauth_field(token) or not expires:
            return unavailable(source, "no-auth", "No readable Antigravity OAuth session")
        if (expires - _now()).total_seconds() <= 60:
            return unavailable(source, "expired", "Antigravity's session has expired; renew it in Antigravity")
    now = _now()
    windows, idle = {}, {}
    for host in AGY_OAUTH_HOSTS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            request_at = _now()
            payload = _agy_oauth_request(host, token, remaining / 2, project) if project else \
                _agy_oauth_request(host, token, remaining / 2)
            snap = parse_antigravity_local(payload, request_at, source=source)
        except (OSError, ValueError, TypeError, http.client.HTTPException, RecursionError):
            continue
        if snap:
            for original, target in ((snap.get("windows", {}), windows), (snap.get("idle_windows", {}), idle)):
                target.update({key: row for key, row in original.items() if key not in windows and key not in idle})
    if windows or idle:
        return _snapshot(AGY_OAUTH_SOURCE, windows, idle, None, now)
    return unavailable(AGY_OAUTH_SOURCE, "failed", "Antigravity returned no usable quota summary")


def agy_report_path(config: dict[str, Any]) -> Path:
    configured = config.get("path")
    if configured is not None:
        return Path(configured).expanduser()
    home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return home / "orchestrator" / "antigravity-usage.json"


# BR-QUOTA-001
def observe_antigravity(config: dict[str, Any], timeout: int = 5) -> dict[str, Any]:
    """Read an operator export, or explicitly opt into reusing a running server.

    Neither a forced refresh nor legacy `binary`/`AGY_BIN` can start the CLI. Re-reading
    an export never advances its observation time.
    """
    if "oauth_acp_file" in config:
        if "oauth_session_file" in config:
            return unavailable(AGY_OAUTH_SOURCE, "no-auth", "Select only one Antigravity OAuth profile type")
        return _observe_antigravity_oauth(config["oauth_acp_file"], timeout, acp=True)
    if "oauth_session_file" in config:
        return _observe_antigravity_oauth(config["oauth_session_file"], timeout)
    if config.get("reuse_running") is True:
        result = _observe_running_antigravity(timeout)
        if result:
            return result
    try:
        path = agy_report_path(config)
        # Nonblocking open and fstat on the same descriptor avoid hanging on a FIFO
        # or validating one file and then reading its replacement.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as report:
            info = os.fstat(report.fileno())
            if not stat.S_ISREG(info.st_mode):
                return unavailable(AGY_SOURCE, "malformed", "Antigravity usage export is not a regular file")
            observed_at = _parse_time(info.st_mtime)
            age = (_now() - observed_at).total_seconds() if observed_at else -1
            if age < 0:
                return unavailable(AGY_SOURCE, "malformed", "Antigravity usage export has an invalid capture time")
            if age > AGY_REPORT_MAX_AGE_S:
                return unavailable(AGY_SOURCE, "stale", "Antigravity usage export is older than 15 minutes")
            body = report.read(MAX_BODY_BYTES + 1)
    except FileNotFoundError:
        reason = ("No usable running Antigravity quota server or usage export; automatic CLI sign-in is disabled"
                  if config.get("reuse_running") is True else
                  "No Antigravity usage export; automatic CLI sign-in is disabled")
        return unavailable(AGY_SOURCE, "no-report", reason)
    except (OSError, TypeError, ValueError, RuntimeError):
        return unavailable(AGY_SOURCE, "failed", "Antigravity usage export could not be read")
    try:
        parsed = parse_antigravity(body.decode("utf-8"), observed_at) if len(body) <= MAX_BODY_BYTES else None
    except (UnicodeError, OverflowError, TypeError, ValueError, RecursionError):
        parsed = None
    return parsed or unavailable(AGY_SOURCE, "malformed", "Antigravity CLI usage report was not readable")


# ---- GitHub Copilot -------------------------------------------------------------------------------
COPILOT_SOURCE = "copilot-api"
COPILOT_USAGE_URL = "https://api.github.com/copilot_internal/user"


def _month_minutes_before(reset: dt.datetime) -> int:
    """A monthly allowance renews on the 1st: the window is the month that ends at the reset."""
    previous = (reset - dt.timedelta(days=1))
    return calendar.monthrange(previous.year, previous.month)[1] * 1440


def parse_copilot(body: str, observed_at: dt.datetime | None = None) -> dict[str, Any] | None:
    """Premium requests (the Student allowance) and Chat when it is metered; unlimited is not a window."""
    payload = _json(body)
    if not isinstance(payload, dict):
        return None
    snapshots = payload.get("quota_snapshots")
    if not isinstance(snapshots, dict):
        return None
    reset = _parse_time(payload.get("quota_reset_date_utc")) or _parse_time(payload.get("quota_reset_date"))
    minutes = _month_minutes_before(reset) if reset else None
    windows: dict[str, Any] = {}
    idle: dict[str, Any] = {}
    for key, name, label in (("premium_interactions", "premium_monthly", "Premium"),
                             ("chat", "chat_monthly", "Chat")):
        snap = snapshots.get(key)
        if not isinstance(snap, dict) or snap.get("unlimited") is True:
            continue
        remaining = snap.get("percent_remaining")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            continue
        _add(windows, idle, name, _percent(100.0 - float(remaining)), reset, minutes, label)
    if not windows and not idle:
        return None
    plan = payload.get("copilot_plan")
    return _snapshot(COPILOT_SOURCE, windows, idle,
                     f"Copilot {plan.capitalize()}" if isinstance(plan, str) and plan else None, observed_at)


def _providers_module():
    spec = importlib.util.spec_from_file_location("fleet_providers_keys", Path(__file__).with_name("providers.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copilot_token(config: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    declared = config.get("key")
    if declared:
        try:
            key = _providers_module().resolve_key(str(declared))
        except Exception:  # noqa: BLE001 - the message must not echo anything read
            return None, unavailable(COPILOT_SOURCE, "no-key", "the declared GitHub key reference could not be read")
        return (key, None) if key else (None, unavailable(COPILOT_SOURCE, "no-key", "the declared GitHub key is empty"))
    gh = str(config.get("gh") or "gh")
    try:
        found = subprocess.run([gh, "auth", "token", "--hostname", "github.com"],
                               capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return None, unavailable(COPILOT_SOURCE, "no-key", "no GitHub sign-in: install and sign in the GitHub CLI, or set `key`")
    except (OSError, subprocess.SubprocessError):
        return None, unavailable(COPILOT_SOURCE, "no-key", "the GitHub CLI did not return its sign-in")
    token = found.stdout.strip() if found.returncode == 0 else ""
    if not token or any(c.isspace() for c in token):
        return None, unavailable(COPILOT_SOURCE, "no-key", "the GitHub CLI is not signed in (`gh auth login`)")
    return token, None


def observe_copilot(config: dict[str, Any], timeout: int = 5, opener: Callable[..., Any] | None = None,
                    token: str | None = None) -> dict[str, Any]:
    if token is None:
        token, failure = copilot_token(config)
        if failure:
            return failure
    try:
        status, body = _http_get(COPILOT_USAGE_URL, {
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "X-Github-Api-Version": "2025-04-01",
            "User-Agent": "crossfeed-orchestrator",
        }, reader_timeout("copilot", config, timeout), opener)
    except Exception as error:  # noqa: BLE001
        return unavailable(COPILOT_SOURCE, "failed", f"Copilot usage request failed ({type(error).__name__})")
    if status in (401, 403):
        return unavailable(COPILOT_SOURCE, "unauthorized", "GitHub rejected the sign-in for Copilot usage")
    if status == 404:
        return unavailable(COPILOT_SOURCE, "no-subscription", "GitHub says this account has no Copilot plan")
    if status != 200:
        return unavailable(COPILOT_SOURCE, "failed", f"Copilot usage request failed (HTTP {status})")
    return parse_copilot(body) or unavailable(COPILOT_SOURCE, "malformed", "Copilot answer carried no metered quota")


OBSERVERS: dict[str, Callable[..., dict[str, Any]]] = {
    "codex": observe_codex,
    "codex-oauth": observe_codex_oauth,
    "claude": observe_claude,
    "antigravity": observe_antigravity,
    "copilot": observe_copilot,
}
