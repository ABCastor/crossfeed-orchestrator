"""Quota readings straight from each provider, through the provider's own tool or sign-in.

The readers return the snapshot shape every
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
- openrouter: informational cash balances and spend from the free wrapper's existing key file.

Credentials go only to the relevant provider on fixed HTTPS hosts. Never written or logged.
A rejected sign-in, a missing tool and an unreadable answer stay distinct and never become 0%.
"""
from __future__ import annotations

import base64
import calendar
import collections
import datetime as dt
import fcntl
import http.client
import importlib.util
import json
import math
import os
import re
import selectors
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
from pathlib import Path
from typing import Any, Callable

MAX_BODY_BYTES = 2_000_000

# The Codex app server and OAuth endpoints have their own time limits.
DEFAULT_TIMEOUTS = {"codex": 20, "codex-oauth": 10, "claude": 10, "copilot": 10, "openrouter": 10}


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


def _finite_amount(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        amount = float(raw)
    except (OverflowError, ValueError):
        return None
    return amount if math.isfinite(amount) else None


def _duration_name(minutes: int | None) -> str:
    if minutes == 300:
        return "session"
    if minutes == 10080:
        return "weekly"
    return f"window_{minutes}m" if minutes else "window"


def _duration_label(minutes: int | None) -> str | None:
    return {300: "Session", 10080: "Weekly"}.get(minutes or 0)


def _http_get(url: str, headers: dict[str, str], timeout: float,
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
    spend_credits = {}
    for limit_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        general = limit_id == "codex"
        if general:
            if isinstance(bucket.get("planType"), str):
                plan = bucket["planType"].strip().capitalize() or None
            spend_credits = _mapping(bucket.get("credits"))
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
    result = _snapshot(CODEX_SOURCE, windows, idle, plan, observed_at)
    balance = _finite_amount(spend_credits.get("balance")) if spend_credits.get("unlimited") is not True else None
    if balance is not None:
        result["credits"] = _credit(balance, "credits", CODEX_SOURCE,
                                    spend_credits.get("expiresAt"), spend_credits.get("renewsAt"))
    return result if windows or idle or "credits" in result else None


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
CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CODEX_RESET_CREDITS_SOURCE = "codex-oauth-rate-limit-reset-credits"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _credit(balance, unit, source, expires_at=None, renews_at=None):
    expiry, renewal = _parse_time(expires_at), _parse_time(renews_at)
    return {"balance": balance, "unit": unit, "source": source,
            "expires_at": _iso(expiry) if expiry else None, "renews_at": _iso(renewal) if renewal else None}


def parse_codex_reset_credits(payload: Any, observed_at: dt.datetime) -> dict[str, Any] | None:
    """Only available, unexpired credits; never persist redemption identifiers."""
    root = _mapping(payload)
    rows = root.get("credits")
    if not isinstance(rows, list) or type(root.get("available_count")) is not int:
        return None
    expirations = []
    for raw in rows:
        row = _mapping(raw)
        if row.get("status") != "available":
            continue
        raw_expiry = row.get("expires_at")
        expiry = _parse_time(raw_expiry)
        if (raw_expiry is not None and expiry is None) or (expiry and expiry <= observed_at):
            continue
        expirations.append(_iso(expiry) if expiry else None)
    expirations.sort(key=lambda value: (value is None, value))
    return {**_credit(len(expirations), "limit resets", CODEX_RESET_CREDITS_SOURCE,
                      expirations[0] if expirations else None, root.get("renews_at")),
            "expirations": expirations}


def parse_codex_oauth(payload: Any, observed_at: dt.datetime | None = None) -> dict[str, Any] | None:
    """Quota windows and spend credits; reset-credit inventory is a separate read."""
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
    credits = _mapping(root.get("credits"))
    balance = _finite_amount(credits.get("balance")) if credits.get("unlimited") is not True else None
    if not windows and balance is None:
        return None
    plan = root.get("plan_type")
    result = _snapshot(CODEX_OAUTH_SOURCE, windows, idle,
                       plan.strip().capitalize() if isinstance(plan, str) else None, observed_at)
    if balance is not None:
        result["account_usage"] = {"credits_balance": balance}
        result["credits"] = _credit(balance, "credits", CODEX_OAUTH_SOURCE,
                                    credits.get("expires_at"), credits.get("renews_at"))
    return result


CODEX_BILLING_SOURCE = "codex-oauth-billing"
CODEX_BACKEND = "https://chatgpt.com/backend-api/"
HISTORY_DAYS = 30


def _day(raw: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError:
        return None


def _credit_amount(raw: Any) -> float | None:
    amount = _finite_amount(raw)
    return amount if amount is not None and 0 <= amount < 1e12 else None


def parse_codex_billing(subscription: Any, balance: Any, breakdown: Any, events: Any,
                        today: dt.date) -> dict[str, Any] | None:
    """Codex's own ChatGPT sign-in against the same endpoints ChatGPT's billing page calls.

    `subscription` carries the paid period end (`active_until`) and whether it renews; `balance`
    the funded credit grants with expiry dates; `events` every credit spend (summed per day, no
    event or account identifiers kept); `breakdown` the share of the weekly limit used per day
    and per model (its `units` are percent; its per-model `credits` field is not on the same
    scale as the events, so it is not shown). Absent or malformed parts are omitted, never zero.
    """
    out: dict[str, Any] = {"source": CODEX_BILLING_SOURCE}
    sub = _mapping(subscription)
    if sub:
        entitlement = _mapping(sub.get("entitlement"))
        until = _parse_time(sub.get("active_until") or entitlement.get("expires_at"))
        will_renew = sub.get("will_renew") if isinstance(sub.get("will_renew"), bool) else None
        plan = _label(sub.get("plan_type") or entitlement.get("subscription_plan"))
        period = _label(sub.get("billing_period") or entitlement.get("billing_period"))
        cancels = _parse_time(entitlement.get("cancels_at"))
        currency = sub.get("billing_currency") if isinstance(sub.get("billing_currency"), str) else None
        if plan:
            out["plan"] = plan
        if period:
            out["period"] = period
        if currency and re.fullmatch(r"[A-Z]{3}", currency):
            out["currency"] = currency
        if until:
            out["active_until"] = _iso(until)
        if will_renew is not None:
            out["will_renew"] = will_renew
            if until and will_renew:
                out["renews_at"] = _iso(until)
        if cancels:
            out["cancels_at"] = _iso(cancels)
    details = _mapping(balance).get("expiring_balance_details")
    if isinstance(details, list):
        grants = []
        for raw in details:
            grant = _mapping(raw)
            remaining, expiry = _credit_amount(grant.get("amount_remaining")), _parse_time(grant.get("expiry_date"))
            kind = grant.get("grant_type")
            if remaining is None or not expiry:
                continue
            row = {"credits": remaining, "expires_at": _iso(expiry)}
            granted = _credit_amount(grant.get("amount_granted"))
            if granted is not None:
                row["granted"] = granted
            if isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,40}", kind):
                row["kind"] = kind
            grants.append(row)
        out["grants"] = sorted(grants, key=lambda row: row["expires_at"])
    rows = _mapping(events).get("data")
    if isinstance(rows, list):
        start = today - dt.timedelta(days=HISTORY_DAYS - 1)
        daily: dict[dt.date, float] = {}
        for raw in rows:
            row = _mapping(raw)
            day, amount = _day(row.get("date")), _credit_amount(row.get("credit_amount"))
            if day is None or amount is None:
                return None if not out.get("plan") and len(out) == 1 else out
            if start <= day <= today:
                daily[day] = daily.get(day, 0.0) + amount
        out["credit_history"] = [{"date": day.isoformat(), "credits": round(daily.get(day, 0.0), 6)}
                                 for day in (start + dt.timedelta(days=i) for i in range(HISTORY_DAYS))]
        out["credits_today"] = round(daily.get(today, 0.0), 6)
        out["credits_7d"] = round(sum(v for d, v in daily.items() if d > today - dt.timedelta(days=7)), 6)
        out["credits_30d"] = round(sum(daily.values()), 6)
    days = _mapping(breakdown).get("data")
    if isinstance(days, list):
        usage: list[dict[str, Any]] = []
        per_model: dict[str, float] = {}
        for raw in days:
            row = _mapping(raw)
            day = _day(row.get("date"))
            attribution = row.get("attribution")
            if day is None or not isinstance(attribution, list):
                continue
            usage.append({"date": day.isoformat(), "percent": round(sum(
                _finite_amount(_mapping(a).get("value")) or 0 for a in attribution), 6)})
            for item in attribution:
                name, value = _mapping(item).get("model"), _finite_amount(_mapping(item).get("value"))
                if isinstance(name, str) and re.fullmatch(r"[\w./:-]{1,80}", name, re.ASCII) and value and value > 0:
                    per_model[name] = per_model.get(name, 0.0) + value
        out["limit_use_history"] = sorted(usage, key=lambda row: row["date"])[-HISTORY_DAYS:]
        out["limit_use_by_model_30d"] = [{"model": name, "percent": round(value, 6)} for name, value in
                                         sorted(per_model.items(), key=lambda item: (-item[1], item[0]))[:6]]
    return out if len(out) > 1 else None


def _codex_billing(headers: dict[str, str], account: str | None, budget: float,
                   opener: Callable[..., Any] | None) -> tuple[dict[str, Any] | None, list[str]]:
    """Four concurrent GETs on fixed ChatGPT paths; failures degrade to named gaps."""
    if not account:
        return None, ["no account id"]
    paths = {
        "subscription": "subscriptions?" + urlencode({"account_id": account}),
        "balance": f"accounts/{account}/remaining_balance",
        "breakdown": "wham/usage/daily-token-usage-breakdown",
        "events": "wham/usage/credit-usage-events",
    }
    results: dict[str, Any] = {}
    failed: list[str] = []

    def fetch(name: str) -> None:
        try:
            status, body = _http_get(CODEX_BACKEND + paths[name], headers, budget, opener)
            payload = _json(body) if status == 200 else None
            if payload is None:
                failed.append(name)
            else:
                results[name] = payload
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            failed.append(name)

    threads = [threading.Thread(target=fetch, args=(name,), daemon=True) for name in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(budget + 1)
    parsed = parse_codex_billing(results.get("subscription"), results.get("balance"), results.get("breakdown"),
                                 results.get("events"), _now().astimezone().date()) if results else None
    return parsed, sorted(set(failed) | {name for name in paths if name not in results})


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
        budget = reader_timeout("codex-oauth", config, timeout)
        supplemental = config.get("reset_credits") is True
        extra_timeout = min(4, budget / 3) if supplemental else 0
        status, body = _http_get(CODEX_USAGE_URL, headers, budget - extra_timeout, opener)
        if status == 200:
            observed = parse_codex_oauth(_json(body))
            if not observed:
                return unavailable(CODEX_OAUTH_SOURCE, "malformed", "Codex reported no OAuth usage")
            if supplemental:
                status, body = _http_get(CODEX_RESET_CREDITS_URL, headers, extra_timeout, opener)
                if status == 200:
                    inventory = parse_codex_reset_credits(_json(body), _now())
                    if inventory is None:
                        return unavailable(CODEX_OAUTH_SOURCE, "malformed", "Codex reset-credit inventory was unreadable")
                    if "credits" in observed:
                        observed["spend_credits"] = observed["credits"]
                    observed["credits"] = inventory
                else:
                    return unavailable(CODEX_OAUTH_SOURCE, "failed", f"Codex reset credits returned HTTP {status}")
            if config.get("billing") is not False:
                # Display-only: a billing failure never fails the quota reading it rides on.
                billing, missing = _codex_billing(headers, account if "ChatGPT-Account-Id" in headers else None,
                                                  min(4.0, max(1.0, budget / 2)), opener)
                if billing:
                    observed["billing"] = {**billing, "observed_at": _iso(_now()), **({"missing": missing} if missing else {})}
                else:
                    observed["billing_error"] = "Codex billing endpoints returned nothing usable"
            return observed
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
    extra = _mapping(payload.get("extra_usage"))
    cap, spent = (_finite_amount(extra.get(key)) for key in ("monthly_limit", "used_credits"))
    result = _snapshot(CLAUDE_SOURCE, windows, idle, plan, observed_at)
    if extra.get("is_enabled") is True and cap is not None and spent is not None and min(cap, spent) >= 0:
        currency = extra.get("currency") or "USD"
        if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency):
            # Claude OAuth reports currency minor units. This is remaining paid headroom,
            # not a quota reset or a grant of free spend.
            result["credits"] = {**_credit(max(0, cap - spent) / 100, currency, CLAUDE_SOURCE,
                                           extra.get("expires_at"), extra.get("renews_at")),
                                 "kind": "extra_usage", "used": spent / 100, "limit": cap / 100}
    return result if windows or idle or "credits" in result else None


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
# This quota response is client-version dependent. A generic "antigravity" UA omits Gemini
# windows even with a valid sign-in. Use the verified T3 Hub protocol/version prefix.
AGY_OAUTH_USER_AGENT = "antigravity/hub/2.24.0"
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
    provider_used: dict[str, float] = {}
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
            used = 100 * (1 - fraction)
            _agy_store(windows, idle, family, cadence, used, reset)
            if source == AGY_OAUTH_SOURCE:
                provider_used[name] = round(used, 8)
            if name in idle and (source == AGY_OAUTH_SOURCE or fraction != 1):
                idle[name]["reset_unknown"] = True
    if not windows and not idle:
        return None
    snap = _snapshot(source, windows, idle, None, observed)
    if provider_used:
        snap["provider_used_percent"] = provider_used
    return snap


AGY_OAUTH_MODEL_QUOTA = "/v1internal:retrieveUserQuota"
AGY_OAUTH_MODELS = "/v1internal:fetchAvailableModels"
_AGY_FAMILY = re.compile(r"gemini-\d+(?:\.\d+)?-([a-z]+)(?:-|\Z)")


def _agy_model_family(model: str) -> str:
    if model.startswith("claude-"):
        return "Claude"
    if model.startswith("gpt-"):
        return "GPT"
    match = _AGY_FAMILY.match(model)
    return match.group(1).capitalize() if match and match.group(1) not in {"flash", "pro"} else "Gemini"


def parse_antigravity_model_quota(payload: Any, families: list[str], observed_at: dt.datetime | None = None,
                                  shared: dict[str, Any] | None = None) -> list[dict[str, Any]] | None:
    """Per-model quota (T3 Code's `retrieveUserQuota`, else `fetchAvailableModels`), grouped by pool.

    Models that report the same remaining fraction and reset share one pool; a pool that equals a
    summary window (Gemini 5-hour, weekly) is labelled as sharing it. `families` keeps only the
    requested model families (e.g. ["argon"]); with none requested nothing is returned.
    """
    root = _mapping(payload)
    rows: list[tuple[str, Any, Any]] = []
    if isinstance(root.get("buckets"), list):
        rows = [(b.get("modelId"), b.get("remainingFraction"), b.get("resetTime")) for b in map(_mapping, root["buckets"])]
    elif isinstance(root.get("models"), dict):
        for model_id, model in root["models"].items():
            info = _mapping(_mapping(model).get("quotaInfo"))
            rows.append((model_id, info.get("remainingFraction"), info.get("resetTime")))
    else:
        return None
    wanted = {family.lower() for family in families}
    groups: dict[tuple[float, str | None], dict[str, Any]] = {}
    for model, fraction, reset in rows:
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", model):
            continue
        if _agy_model_family(model).lower() not in wanted:
            continue
        # A missing fraction beside a reset is a protobuf zero, but only a stated value is shown.
        if type(fraction) not in (int, float) or not 0 <= fraction <= 1:
            continue
        stamp = _parse_time(reset)
        key = (round(float(fraction), 8), _iso(stamp) if stamp else None)
        group = groups.setdefault(key, {"models": [], "families": set()})
        group["models"].append(model)
        group["families"].add(_agy_model_family(model))
    result = []
    for (fraction, reset), group in sorted(groups.items(), key=lambda item: item[0][1] or ""):
        used = round(100 * (1 - fraction), 8)
        entry: dict[str, Any] = {"label": " · ".join(sorted(group["families"])) + " models",
                                 "models": sorted(group["models"]), "used_percent": used}
        if reset:
            entry["reset_at"] = reset
        for name, row in (shared or {}).items():
            same_reset = bool(reset) and row.get("reset_at") == reset
            if same_reset and abs(float(row.get("used_percent", -1000)) - used) < 0.01:
                entry["shared_with"] = row.get("label") or name
                break
        result.append(entry)
    return result


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
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": AGY_OAUTH_USER_AGENT,
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


def _agy_cli_keychain(timeout: float) -> dict[str, Any]:
    """Antigravity CLI's own macOS go-keyring entry, captured without a shell or logs."""
    if timeout <= 0 or not Path("/usr/bin/security").is_file():
        return {}
    found = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
    )
    body = found.stdout.strip()
    if found.returncode != 0 or len(body) > MAX_BODY_BYTES:
        return {}
    if body.startswith(b"go-keyring-base64:"):
        body = base64.b64decode(body.partition(b":")[2], validate=True)
    elif body.startswith(b"go-keyring-encoded:"):
        body = bytes.fromhex(body.partition(b":")[2].decode("ascii"))
    return _mapping(_json(body.decode("utf-8")))


def _agy_model_quota(token: str, project: str | None, families: list[str], summary: dict[str, Any],
                     provider_used: dict[str, float], budget: float) -> list[dict[str, Any]]:
    """Best effort: a failure here never costs the summary windows already read."""
    shared = {name: {**row, "used_percent": provider_used.get(name, row.get("used_percent", 0))}
              for name, row in summary.items()}
    body = json.dumps({"project": project} if project else {}, separators=(",", ":")).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "User-Agent": AGY_OAUTH_USER_AGENT}
    deadline = time.monotonic() + budget
    for path in (AGY_OAUTH_MODEL_QUOTA, AGY_OAUTH_MODELS):
        for host in AGY_OAUTH_HOSTS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            try:
                status, payload = _agy_google_post(host, path, body, headers, min(4.0, remaining))
                rows = parse_antigravity_model_quota(payload, families, None, shared) if status == 200 else None
            except (OSError, ValueError, TypeError, http.client.HTTPException, RecursionError):
                continue
            if rows:
                return rows
    return []


def _observe_antigravity_oauth(path: Any, timeout: int, *, acp: bool = False,
                                keychain: bool = False, model_families: list[str] | None = None) -> dict[str, Any]:
    """Use one explicitly selected profile. No account discovery or fallback."""
    deadline = time.monotonic() + max(1, min(timeout, 10))
    source = AGY_OAUTH_SOURCE
    try:
        if keychain:
            doc = _agy_cli_keychain(min(3, deadline - time.monotonic()))
        else:
            path = Path(path).expanduser()
            if not path.is_absolute():
                raise ValueError
            doc = _mapping(_json(_owner_file(path).decode("utf-8")))
    except (TypeError, OSError, ValueError, RuntimeError, subprocess.SubprocessError):
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
    windows, idle, provider_used = {}, {}, {}
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
                new = {key: row for key, row in original.items() if key not in windows and key not in idle}
                target.update(new)
                provider_used.update({key: snap["provider_used_percent"][key] for key in new})
    if windows or idle:
        result = {**_snapshot(source, windows, idle, None, now), "provider_used_percent": provider_used}
        if model_families:
            quota = _agy_model_quota(token, project, model_families, {**windows, **idle}, provider_used,
                                     max(1.0, deadline - time.monotonic()))
            if quota:
                result["model_quota"] = quota
        return result
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
    keychain = config.get("oauth_cli_keychain") is True
    if sum((keychain, "oauth_acp_file" in config, "oauth_session_file" in config)) > 1:
        return unavailable(AGY_OAUTH_SOURCE, "no-auth", "Select only one Antigravity OAuth profile type")
    families = [f for f in config.get("model_quota", []) if isinstance(f, str)] if isinstance(config.get("model_quota"), list) else []
    if keychain:
        return _observe_antigravity_oauth(None, timeout, keychain=True, model_families=families)
    if "oauth_acp_file" in config:
        return _observe_antigravity_oauth(config["oauth_acp_file"], timeout, acp=True, model_families=families)
    if "oauth_session_file" in config:
        return _observe_antigravity_oauth(config["oauth_session_file"], timeout, model_families=families)
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
    provider_used: dict[str, float] = {}
    for key, name, label in (("premium_interactions", "premium_monthly", "Premium"),
                             ("chat", "chat_monthly", "Chat")):
        snap = snapshots.get(key)
        if not isinstance(snap, dict) or snap.get("unlimited") is True:
            continue
        remaining = snap.get("percent_remaining")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            continue
        used = 100.0 - float(remaining)
        _add(windows, idle, name, _percent(used), reset, minutes, label)
        if 0 <= used <= 100:
            provider_used[name] = round(used, 8)
    if not windows and not idle:
        return None
    plan = payload.get("copilot_plan")
    result = _snapshot(COPILOT_SOURCE, windows, idle,
                       f"Copilot {plan.capitalize()}" if isinstance(plan, str) and plan else None, observed_at)
    result["provider_used_percent"] = provider_used  # informational; routing keeps whole percentage points
    return result


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


# Cash telemetry is not a quota window and does not modify the free-only price gate.
OPENROUTER_SOURCE = "openrouter-account"
OPENROUTER_BASE = "https://openrouter.ai/api/v1/"
OPENROUTER_CASH_FIELDS = {
    "credits": {"total_credits": "account_credits_usd", "total_usage": "account_usage_usd"},
    "key": {"limit": "key_limit_usd", "limit_remaining": "key_limit_remaining_usd", "usage": "key_usage_usd",
            "usage_daily": "key_usage_daily_usd", "usage_weekly": "key_usage_weekly_usd",
            "usage_monthly": "key_usage_monthly_usd"},
}


def openrouter_key_path() -> Path:
    # Same authority as openrouter-agent.sh, with no environment-token or management-key fallback.
    return Path(os.environ.get("OPENROUTER_KEY_FILE") or Path.home() / ".config/orchestrator/openrouter_api_key")


def _management_key() -> str | None:
    """The OpenRouter management key from the portable store (env, 1Password, Keychain, Secret Service)."""
    try:
        try:
            import secret_store
        except ImportError:
            spec = importlib.util.spec_from_file_location("fleet_secret_store", Path(__file__).with_name("secret_store.py"))
            secret_store = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(secret_store)
        return secret_store.resolve("openrouter-management")
    except Exception:  # noqa: BLE001 - a broken store means "no management key", never a failed quota read
        return None


def observe_openrouter(config: dict[str, Any], timeout: int = 5,
                       opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    try:
        key = _owner_file(openrouter_key_path()).decode("utf-8").strip()
    except (OSError, ValueError):
        key = ""
    if not key or not re.fullmatch(r"[!-~]{1,16000}", key):
        return unavailable(OPENROUTER_SOURCE, "no-key", "No existing private OpenRouter wrapper key file")
    cash: dict[str, float] = {}
    statuses = []
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    activity = config.get("activity") is True
    per_request = reader_timeout("openrouter", config, timeout) / (len(OPENROUTER_CASH_FIELDS) + activity)
    for endpoint, fields in OPENROUTER_CASH_FIELDS.items():
        try:
            status, body = _http_get(OPENROUTER_BASE + endpoint, headers, per_request, opener)
            statuses.append(status)
            if status != 200:
                continue
            data = _mapping(_mapping(_json(body)).get("data"))
            for field, output in fields.items():
                amount = _finite_amount(data.get(field))
                if amount is not None:
                    cash[output] = amount
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            continue
    if not cash or statuses != [200, 200]:
        if statuses and all(status in (401, 403) for status in statuses):
            return unavailable(OPENROUTER_SOURCE, "unauthorized", "OpenRouter rejected the existing wrapper key")
        return unavailable(OPENROUTER_SOURCE, "failed", "No OpenRouter cash telemetry returned")
    if "account_credits_usd" in cash and "account_usage_usd" in cash:
        balance = cash["account_credits_usd"] - cash["account_usage_usd"]
        if math.isfinite(balance):
            cash["account_balance_usd"] = balance
    result = {**_snapshot(OPENROUTER_SOURCE, {}, {}), "account_usage": cash}
    if activity:
        management = _management_key()
        # Activity is the only call that needs the management key; cash stays on the inference key.
        activity_headers = {**headers, "Authorization": f"Bearer {management}"} if management else headers
        try:
            status, body = _http_get(OPENROUTER_BASE + "activity", activity_headers, per_request, opener)
            if status == 200:
                rows = parse_openrouter_activity(_json(body), _now().date())
                if rows is not None:
                    result["per_model"] = rows
                else:
                    return unavailable(OPENROUTER_SOURCE, "malformed", "OpenRouter activity response was unreadable")
            elif status in (401, 403):
                result["per_model_error"] = (
                    "OpenRouter rejected the stored management key; it needs Activity read access"
                    if management else
                    "OpenRouter activity requires a management key with Activity read access "
                    "(add one: fleetctl secret set openrouter-management)")
            else:
                return unavailable(OPENROUTER_SOURCE, "failed", f"OpenRouter activity returned HTTP {status}")
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            return unavailable(OPENROUTER_SOURCE, "failed", "OpenRouter activity request failed")
    return result


def parse_openrouter_activity(payload: Any, today: dt.date) -> list[dict[str, Any]] | None:
    """Official Activity rows, grouped by day/endpoint; report the last 30 completed UTC days."""
    rows = _mapping(payload).get("data")
    if not isinstance(rows, list):
        return None
    start = today - dt.timedelta(days=30)
    grouped: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = _mapping(raw)
        try:
            day = dt.date.fromisoformat(str(row.get("date"))[:10])  # the API sends "YYYY-MM-DD 00:00:00"
        except ValueError:
            return None  # don't pass incomplete account totals as complete
        if not start <= day < today:
            continue
        model, requests = row.get("model"), row.get("requests")
        amount = _finite_amount(row.get("usage"))
        if (not isinstance(model, str) or not re.fullmatch(r"[\w./:-]{1,200}", model, re.ASCII)
                or type(requests) is not int or requests < 0 or amount is None or amount < 0):
            return None
        total = grouped.setdefault(model, {"model": model, "usd_30d": 0.0, "requests": 0})
        total["usd_30d"] += amount
        total["requests"] += requests
        if not math.isfinite(total["usd_30d"]):
            return None
    return sorted(grouped.values(), key=lambda row: (-row["usd_30d"], row["model"]))


STATUS_URLS = {
    "codex": "https://status.openai.com/", "codex-oauth": "https://status.openai.com/",
    "chatgpt-work": "https://status.openai.com/", "claude": "https://status.claude.com/",
    "antigravity": "https://www.google.com/appsstatus/dashboard/products/npdyhgECDJ6tB66MxXyo/history",
    "opencode-go": "https://status.opencode.ai/", "copilot": "https://www.githubstatus.com/",
    "github-copilot-student": "https://www.githubstatus.com/", "openrouter": "https://status.openrouter.ai/",
    "openrouter-free": "https://status.openrouter.ai/",
}


def display_observation(snapshot, failure, now, ttl_s):
    """Age from the stored measurement, not from routing policy or its last failed refresh."""
    stamp = _parse_time(snapshot.get("observed_at"))
    if stamp is None:
        return {}
    age = max(0, int((now - stamp).total_seconds()))
    return {"reading_age_s": age, "reading_source": snapshot.get("source") or "unknown",
            "reading_observed_at": _iso(stamp), "reading_stale": bool(failure) or age > ttl_s}


def display_extras(pool, config, snapshot, failure, state_dir, now, *, refresh):
    reader = _mapping(config.get("quota_refresh"))
    oracle = reader.get("oracle")
    ttl_s = _finite_amount(reader.get("ttl_s")) if snapshot.get("account_usage") and not snapshot.get("windows") else None
    projection = display_observation(snapshot, failure, now, ttl_s or 3600)
    status = (STATUS_URLS.get(oracle) if isinstance(oracle, str) else None) or STATUS_URLS.get(pool)
    if status:
        projection["status_url"] = status
    for field in ("credits", "spend_credits", "per_model", "per_model_error", "account_usage", "model_quota", "billing", "billing_error"):
        if field in snapshot:
            projection[field] = snapshot[field]
    cost = usage_cost(reader, state_dir, now, refresh=refresh)
    if cost is not None:
        projection["usage_cost"] = cost
    incident = provider_incident(pool, oracle, state_dir, now, refresh=refresh)
    if incident is not None:
        projection["incident"] = incident
    return projection


# ---- Provider incident badges ----------------------------------------------------------------------
# CodexBar v0.67.0, ProviderStatusFetcher.swift: Statuspage `api/v2/summary.json` (Claude, OpenAI,
# GitHub) and Google Workspace's public `incidents.json` filtered to the Gemini product. OpenRouter's
# Datadog page and OpenCode's own page publish public JSON too. Public GETs with no credentials.
STATUS_TTL_S = 300
STATUS_LEVELS = ("none", "maintenance", "minor", "major", "critical")
GOOGLE_GEMINI_PRODUCT = "npdyhgECDJ6tB66MxXyo"
STATUS_FEEDS = {
    "claude": ("statuspage", "https://status.claude.com/api/v2/summary.json"),
    "openai": ("statuspage", "https://status.openai.com/api/v2/summary.json"),
    "github": ("statuspage", "https://www.githubstatus.com/api/v2/summary.json"),
    "openrouter": ("datadog", "https://status.openrouter.ai/config.json"),
    "opencode": ("opencode", "https://status.opencode.ai/api/status"),
    "google": ("google", "https://www.google.com/appsstatus/dashboard/incidents.json"),
}
STATUS_FEED_FOR = {
    "codex": "openai", "codex-oauth": "openai", "chatgpt-work": "openai", "claude": "claude",
    "copilot": "github", "github-copilot-student": "github", "openrouter": "openrouter",
    "openrouter-free": "openrouter", "opencode-go": "opencode", "antigravity": "google",
}
_COMPONENT_LEVEL = {"operational": "none", "degraded_performance": "minor", "degraded": "minor",
                    "partial_outage": "major", "major_outage": "critical", "under_maintenance": "maintenance",
                    "maintenance": "maintenance", "outage": "critical"}
_IMPACT_LEVEL = {"none": "none", "minor": "minor", "major": "major", "critical": "critical", "maintenance": "maintenance"}


def _worst(*levels: str | None) -> str:
    known = [level for level in levels if level in STATUS_LEVELS]
    return max(known, key=STATUS_LEVELS.index) if known else "none"


def _status_text(text: Any, limit: int = 140) -> str | None:
    if not isinstance(text, str):
        return None
    for line in re.sub(r"\*\*|\[([^\]]+)\]\([^)]*\)", r"\1", text.replace("\r", "\n")).split("\n"):
        line = line.strip(" -\t")
        if line and not line.lower().startswith(("summary", "description")):
            return re.sub(r"\s+", " ", line)[:limit]
    return None


def _status_record(level: str, text: Any, since: Any, affected: list[str], feed: str) -> dict[str, Any]:
    stamp = _parse_time(since)
    return {"level": level, "text": _status_text(text), "since": _iso(stamp) if stamp else None,
            "affected": sorted({n.strip()[:60] for n in affected if isinstance(n, str) and n.strip()})[:4], "feed": feed}


def parse_statuspage_summary(payload: Any) -> dict[str, Any] | None:
    root = _mapping(payload)
    status = _mapping(root.get("status"))
    if not status or not isinstance(root.get("components", []), list):
        return None
    level = _IMPACT_LEVEL.get(str(status.get("indicator")), "minor")
    components = [_mapping(c) for c in root.get("components") or []]
    broken = [c for c in components if _COMPONENT_LEVEL.get(str(c.get("status")), "minor") != "none" and not c.get("group")]
    incidents = [_mapping(i) for i in root.get("incidents") or [] if _mapping(i).get("status") not in ("resolved", "postmortem")]
    # The page's own indicator is the badge level (as CodexBar shows it); an open incident's name explains it.
    top = max(incidents, key=lambda i: STATUS_LEVELS.index(_IMPACT_LEVEL.get(str(i.get("impact")), "minor")), default={})
    if level == "none":
        return _status_record("none", None, None, [], "statuspage")
    text = top.get("name") or status.get("description")
    return _status_record(level, text, top.get("created_at") or _mapping(root.get("page")).get("updated_at"),
                          [str(c.get("name")) for c in broken], "statuspage")


def parse_datadog_status(payload: Any) -> dict[str, Any] | None:
    root = _mapping(payload)
    if not isinstance(root.get("components"), list):
        return None
    leaves: list[dict[str, Any]] = []

    def walk(items: list[Any]) -> None:
        for raw in items:
            item = _mapping(raw)
            if isinstance(item.get("components"), list):
                walk(item["components"])
            else:
                leaves.append(item)

    walk(root["components"])
    levels = [_COMPONENT_LEVEL.get(str(c.get("status")), "minor") for c in leaves]
    active = [_mapping(i) for i in root.get("incidents") or [] if not _mapping(i).get("resolved")]
    level = _worst(*levels, *("minor" for _ in active))
    if level == "none":
        return _status_record("none", None, None, [], "datadog")
    top = active[0] if active else {}
    text = top.get("title") or top.get("name") or (_status_text(top.get("description")) if top else None)
    return _status_record(level, text or "Degraded service", top.get("publishedDate"),
                          [str(c.get("name")) for c, lv in zip(leaves, levels) if lv != "none"], "datadog")


def parse_opencode_status(payload: Any) -> dict[str, Any] | None:
    root = _mapping(payload)
    if not isinstance(root.get("status"), str) or not isinstance(root.get("components", []), list):
        return None
    level = _COMPONENT_LEVEL.get(root["status"], "minor")
    if level == "none":
        return _status_record("none", None, None, [], "opencode")
    bad = [_mapping(c) for c in root.get("components") or [] if _COMPONENT_LEVEL.get(str(_mapping(c).get("status")), "minor") != "none"]
    since = max((c.get("since") for c in bad if isinstance(c.get("since"), (int, float))), default=None)
    reasons = sorted({str(c.get("reason")) for c in bad if c.get("reason") not in (None, "none")})
    text = (", ".join(reasons)[:1].upper() + ", ".join(reasons)[1:]) if reasons else None
    return _status_record(level, text, since / 1000 if since else None,
                          [str(c.get("service")) for c in bad], "opencode")


def parse_google_status(payload: Any, product: str = GOOGLE_GEMINI_PRODUCT) -> dict[str, Any] | None:
    if not isinstance(payload, list):
        return None
    best: tuple[int, dict[str, Any], dict[str, Any]] | None = None
    for raw in payload:
        item = _mapping(raw)
        products = item.get("currently_affected_products") or item.get("affected_products") or []
        if item.get("end") or not any(_mapping(p).get("id") == product for p in products if isinstance(products, list)):
            continue
        update = _mapping(item.get("most_recent_update") or ((item.get("updates") or [{}])[-1:] or [{}])[0])
        impact = str(update.get("status") or item.get("status_impact") or "").upper()
        level = {"AVAILABLE": "none", "SERVICE_INFORMATION": "minor", "SERVICE_DISRUPTION": "major",
                 "SERVICE_OUTAGE": "critical", "SERVICE_MAINTENANCE": "maintenance",
                 "SCHEDULED_MAINTENANCE": "maintenance"}.get(impact) or \
            {"low": "minor", "medium": "major", "high": "critical"}.get(str(item.get("severity")).lower(), "minor")
        if best is None or STATUS_LEVELS.index(level) > best[0]:
            best = (STATUS_LEVELS.index(level), item, update)
    if best is None or best[0] == 0:
        return _status_record("none", None, None, [], "google")
    _, item, update = best
    return _status_record(STATUS_LEVELS[best[0]], update.get("text") or item.get("external_desc"),
                          update.get("when") or item.get("modified") or item.get("begin"), ["Gemini"], "google")


STATUS_PARSERS = {"statuspage": parse_statuspage_summary, "datadog": parse_datadog_status,
                  "opencode": parse_opencode_status, "google": parse_google_status}


def fetch_provider_status(opener: Callable[..., Any] | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """{feed: record}; a feed that fails is left out so the cache keeps its last good record."""
    results: dict[str, Any] = {}
    headers = {"Accept": "application/json", "User-Agent": "crossfeed-orchestrator/1.0"}

    def one(name: str) -> None:
        kind, url = STATUS_FEEDS[name]
        try:
            status, body = _http_get(url, headers, 10, opener)
            record = STATUS_PARSERS[kind](_json(body)) if status == 200 else None
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            record = None
        if record:
            results[name] = {**record, "checked_at": _iso(now or _now())}

    threads = [threading.Thread(target=one, args=(name,), daemon=True) for name in STATUS_FEEDS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    return dict(results)


def _write_provider_status(path: Path, opener: Callable[..., Any] | None = None) -> None:
    try:
        old = _mapping(json.loads(_owner_file(path))) if path.exists() else {}
    except (OSError, ValueError, RecursionError):
        old = {}
    fresh = fetch_provider_status(opener)
    if fresh:
        _write_owner_json(path, {**old, **fresh})


def provider_incident(pool: str, oracle: Any, state_dir: Path, now: dt.datetime, *, refresh: bool) -> dict[str, Any] | None:
    """The cached incident for a pool's provider, refreshed in the background; None when unknown.

    A record with level `none` is returned as such so callers can tell "checked, all clear" from
    "never checked"; consumers hide the badge for `none`.
    """
    feed = STATUS_FEED_FOR.get(oracle) if isinstance(oracle, str) else None
    feed = feed or STATUS_FEED_FOR.get(pool)
    if not feed:
        return None
    cache = state_dir / "provider-status.json"
    try:
        saved = _mapping(json.loads(_owner_file(cache)))
    except (OSError, ValueError, RecursionError):
        saved = {}
    record = _mapping(saved.get(feed))
    stamp = _parse_time(record.get("checked_at"))
    age = max(0, int((now - stamp).total_seconds())) if stamp else None
    if refresh and (age is None or age >= STATUS_TTL_S):
        _spawn_locked(cache, ["--status", str(cache.resolve())], state_dir)
    if not record or stamp is None:
        return None
    return {**record, "age_s": age, "stale": age >= STATUS_TTL_S * 6}


def _spawn_locked(cache: Path, args: list[str], state_dir: Path) -> None:
    """One detached child per cache file; retries no faster than every five minutes."""
    fd = None
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(cache.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not os.fstat(fd).st_size or time.time() - os.fstat(fd).st_mtime >= 300:
            os.pwrite(fd, b"1", 0)
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *args], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                     pass_fds=(fd,))
            threading.Thread(target=child.wait, daemon=True).start()
    except OSError:
        pass
    finally:
        if fd is not None:
            os.close(fd)


# ---- Native CLI log cost (API list-price equivalents, not bills) -------------------------------
# CodexBar v0.67.0, Sources/CodexBarCore/Vendored/CostUsage/CostUsageScanner.swift
# and CostUsageScanner+Claude.swift: read Codex token_count and Claude assistant JSONL,
# account for cached tokens and avoid counting cumulative/streamed entries twice.
COST_TTL_S = 1800
COST_LINE_LIMIT = 4_000_000
COST_PRICING_URL = "https://models.dev/api.json"
COST_ORACLES = {"codex": "codex", "codex-oauth": "codex", "claude": "claude", "antigravity": "antigravity"}


def _token_counts(usage, keys):
    return tuple(v if type(v) is int and 0 <= v < 10**12 else 0 for v in (usage.get(k) for k in keys))


def _cost_events(path, provider, deadline):
    markers = ((b'"token_count"', b'"turn_context"', b'"session_meta"') if provider == "codex"
               else (b'"assistant"',))
    with path.open("rb") as stream:
        while line := stream.readline(COST_LINE_LIMIT + 1):
            if time.monotonic() > deadline:
                raise TimeoutError("local cost scan exceeded its time budget")
            if len(line) > COST_LINE_LIMIT:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(COST_LINE_LIMIT + 1)
                continue
            if not any(marker in line for marker in markers) or (provider == "claude" and b'"usage"' not in line):
                continue
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue  # a writer may still be appending a record
            if isinstance(row, dict):
                yield row


def _codex_cost_rows(events):
    keys = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens")
    old, model, service, inherited = None, None, None, False
    for row in events:
        payload = _mapping(row.get("payload"))
        kind = row.get("type")
        if kind in {"turn_context", "session_meta"}:
            # Forked histories can contain replayed events indistinguishable from real turns.
            # Do not count them as new spend; coverage is explicitly lower when present.
            inherited |= bool(payload.get("forked_from_id"))
            model = payload.get("model") or model
            service = payload.get("service_tier") or service
        if kind != "event_msg" or payload.get("type") != "token_count":
            continue
        info = _mapping(payload.get("info"))
        total_usage = _mapping(info.get("total_token_usage"))
        last = _token_counts(_mapping(info.get("last_token_usage")), keys)
        if total_usage:
            total = _token_counts(total_usage, keys)
            previous = old or (0, 0, 0, 0)
            delta = tuple(max(0, val - base) for val, base in zip(total, previous))
            if not any(delta) and total == old:
                continue
            if sum(total[:2]) < sum(previous[:2]):
                delta = last  # counter restarted; don't bill its cumulative history again
            old = total
        else:
            delta = last
        model_evidence = info.get("model") or info.get("model_name") or payload.get("model") or model
        yield row.get("timestamp"), model_evidence, delta, service, inherited


def _usage_rates(models, model, tokens, provider):
    if not isinstance(model, str):
        return None
    key = model.rsplit("/", 1)[-1]
    entry = models.get(key) or models.get(re.sub(r"[-:]20\d\d-?\d\d-?\d\d$", "", key))
    price = _mapping(_mapping(entry).get("cost"))
    if key == "gpt-5.3-codex-spark" and not price:
        return (0, 0, 0, 0)
    tiers = price.get("tiers")
    if isinstance(tiers, list):
        threshold = -1
        for raw in tiers:
            band = _mapping(raw)
            tier = _mapping(band.get("tier"))
            size = tier.get("size")
            if tier.get("type") == "context" and type(size) is int and threshold < size < tokens:
                price, threshold = band, size
    inp, out, read, write = (_finite_amount(price.get(key)) for key in ("input", "output", "cache_read", "cache_write"))
    if inp is None or out is None or inp < 0 or out < 0:
        return None
    # Anthropic's 5-minute prompt-cache writes are 1.25x standard input.
    return inp, out, read, write if write is not None else (inp * 1.25 if provider == "claude" else None)


def _local_cost_roots(provider):
    if provider == "codex":
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return [root / "sessions", root / "archived_sessions"]
    # Include active CLI profile allocations but not Pi/OMP mirrored sessions.
    roots = [Path.home() / ".claude"]
    profile = os.environ.get("CLAUDE_CONFIG_DIR")
    if profile:
        roots.append(Path(profile))
    return list(dict.fromkeys((root / "projects").resolve() for root in roots))


def local_usage_cost(provider, roots, models, now=None):
    """Thirty local calendar days including today. Unpriced/unfinished rows keep token totals only."""
    now = now or _now()
    today = now.astimezone().date()
    start = today - dt.timedelta(days=29)
    # Historical append-only files cannot contain today's usage if not modified in the scan window.
    start_epoch = dt.datetime.combine(start, dt.time()).timestamp()
    deadline = time.monotonic() + 300
    existing_roots = [root for root in roots if root.is_dir()]
    if not existing_roots:
        return None
    buckets = [[0, 0.0, 0], [0, 0.0, 0]]  # tokens, known estimate, missing coverage
    claude_rows = {}
    visited = set()

    def add(day, model, amounts, service=None, excluded=False, one_hour=0):
        inp, out, read, write = amounts
        tokens = inp + out + (read + write if provider == "claude" else 0)
        if not tokens:
            return
        rates = _usage_rates(models, model, tokens - out, provider)
        regular = inp if provider == "claude" else max(0, inp - read - write)
        counts = (regular, out, read, write - one_hour)
        # Exact fast-mode schedules are not in the standard rate card. Don't present a partial
        # USD number as complete if an extension, fork or model is not supported.
        unsupported = excluded or service in ("fast", "priority") or rates is None or any(
            amount and (rate is None or rate < 0) for amount, rate in zip(counts, rates or ()))
        cost = 0 if unsupported else (sum(amount * (rate or 0) for amount, rate in zip(counts, rates))
                                      + one_hour * rates[0] * 2) / 1_000_000
        for bucket in (buckets if day == today else buckets[1:]):
            bucket[0] += 0 if excluded else tokens
            bucket[1] += cost
            bucket[2] += unsupported

    for root in existing_roots:
        for path in root.rglob("*.jsonl"):
            if path.is_symlink():
                continue
            try:
                info = path.stat()
            except FileNotFoundError:
                continue  # log moved between directory enumeration and stat
            identity = (info.st_dev, info.st_ino)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or identity in visited or info.st_mtime < start_epoch:
                continue
            visited.add(identity)
            events = _cost_events(path, provider, deadline)
            if provider == "codex":
                for timestamp, model, amounts, service, inherited in _codex_cost_rows(events):
                    stamp = _parse_time(timestamp)
                    if stamp and start <= (day := stamp.astimezone().date()) <= today:
                        add(day, model, amounts, service, inherited)
            else:
                for row in events:
                    if row.get("type") != "assistant":
                        continue
                    stamp = _parse_time(row.get("timestamp"))
                    if not stamp or not start <= (day := stamp.astimezone().date()) <= today:
                        continue
                    msg = _mapping(row.get("message"))
                    usage = _mapping(msg.get("usage"))
                    model = msg.get("model")
                    if not isinstance(model, str) or model == "<synthetic>":
                        continue
                    amounts = _token_counts(usage, ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                   "cache_creation_input_tokens"))
                    if not any(amounts):
                        continue
                    one_hour, = _token_counts(_mapping(usage.get("cache_creation")), ("ephemeral_1h_input_tokens",))
                    one_hour = min(one_hour, amounts[3])
                    message, scope = msg.get("id"), row.get("requestId") or row.get("sessionId")
                    if all(isinstance(value, str) and value for value in (message, scope)):
                        key = (message, scope)
                        old = claude_rows.get(key)
                        if old is None or (amounts[1], stamp) >= (old[2][1], old[0]):
                            claude_rows[key] = (stamp, model, amounts, one_hour)
                    else:
                        add(day, model, amounts, one_hour=one_hour)
    for stamp, model, amounts, one_hour in claude_rows.values():
        add(stamp.astimezone().date(), model, amounts, one_hour=one_hour)
    result = {"source": f"{provider}-cli-local-logs", "observed_at": _iso(now), "estimate": "API list price"}
    for prefix, (tokens, cost, unknown) in zip(("today", "last_30d"), buckets):
        result[f"{prefix}_tokens"] = tokens
        result[f"{prefix}_usd"] = round(cost, 8) if not unknown else None
    return result


# ---- Antigravity desktop history cost -------------------------------------------------------------
# CodexBar v0.67.0, Sources/CodexBarCore/Providers/Antigravity/AntigravityLocalReader.swift (+
# AntigravityProtoReader.swift, AntigravityLocalSQLite.swift): per-conversation SQLite files hold
# `gen_metadata(idx, data)` protobuf rows with token usage; rows are deduplicated by (conversation,
# idx) and response id, routing variants (-tiered, -low, -thinking) fall back to the base model's
# public price. Read-only; no model is called and nothing is written back.
AGY_COST_SUFFIXES = ("-tiered", "-low", "-thinking")
AGY_ROW_LIMIT = 50_000
AGY_BLOB_LIMIT = 16 * 1024 * 1024


def _pb_fields(data: bytes):
    """Yield (field, wire, value) over one message. Raises ValueError when it is malformed."""
    pos, size = 0, len(data)

    def varint():
        nonlocal pos
        result = 0
        for shift in range(0, 70, 7):
            if pos >= size:
                raise ValueError("truncated")
            byte = data[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
        raise ValueError("varint too long")

    while pos < size:
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if number <= 0:
            raise ValueError("bad field")
        if wire == 0:
            yield number, wire, varint()
        elif wire == 2:
            length = varint()
            if length > size - pos:
                raise ValueError("truncated")
            yield number, wire, data[pos:pos + length]
            pos += length
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if width > size - pos:
                raise ValueError("truncated")
            yield number, wire, data[pos:pos + width]
            pos += width
        else:
            raise ValueError("unsupported wire type")


def _pb_text(value: Any) -> str | None:
    if not isinstance(value, (bytes, bytearray)):
        raise ValueError("not text")
    text = bytes(value).decode("utf-8")
    return text if text.strip() else None


def _pb_timestamp(data: bytes) -> int | None:
    seconds, nanos = None, 0
    for number, wire, value in _pb_fields(data):
        if number == 1 and wire == 0:
            if not 0 < value <= 253_402_300_799:
                raise ValueError("bad seconds")
            seconds = value
        elif number == 2 and wire == 0:
            if value > 999_999_999:
                raise ValueError("bad nanos")
            nanos = value
    return None if seconds is None else seconds * 1000 + nanos // 1_000_000


def parse_antigravity_turn(blob: bytes) -> dict[str, Any] | None:
    """One generation row: usage counters, model, label, timestamp (ms) and step UUID."""
    turn: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    found = False
    try:
        for number, wire, value in _pb_fields(blob):
            if number == 4 and wire == 2:
                turn["step"] = _pb_text(value)
            elif number == 1 and wire == 2:
                found = True
                for chat_no, chat_wire, chat in _pb_fields(value):
                    if chat_no == 4 and chat_wire == 2:
                        for u_no, u_wire, u in _pb_fields(chat):
                            key = {1: "system", 2: "new", 5: "read", 9: "output", 10: "reasoning"}.get(u_no)
                            if key and u_wire == 0:
                                if u >= 10**12:
                                    raise ValueError("implausible count")
                                usage[key] = u
                            elif u_no == 11 and u_wire == 2:
                                usage["response"] = _pb_text(u)
                            elif u_no == 7 and u_wire == 2:
                                usage["bot"] = _pb_text(u)
                    elif chat_no == 9 and chat_wire == 2:
                        for g_no, g_wire, g in _pb_fields(chat):
                            if g_no == 4 and g_wire == 2:
                                turn["ms"] = _pb_timestamp(g)
                    elif chat_no == 19 and chat_wire == 2:
                        turn["model"] = _pb_text(chat)
                    elif chat_no == 21 and chat_wire == 2:
                        turn["label"] = _pb_text(chat)
    except (ValueError, UnicodeError):
        return None
    if not found or not usage:
        return None
    turn["usage"] = usage
    return turn


def _pb_step_metadata(blob: bytes) -> tuple[str | None, int | None, str | None]:
    step, ms, bot = None, None, None
    try:
        for number, wire, value in _pb_fields(blob):
            if number == 1 and wire == 2:
                ms = _pb_timestamp(value)
            elif number == 9 and wire == 2:
                for sub, sub_wire, sub_value in _pb_fields(value):
                    if sub == 7 and sub_wire == 2:
                        bot = _pb_text(sub_value)
            elif number == 12 and wire == 2:
                step = _pb_text(value)
    except (ValueError, UnicodeError):
        return None, None, None
    return step, ms, bot


def _agy_cost_roots(home: Path | None = None) -> list[Path]:
    base = Path(os.environ.get("GEMINI_CLI_HOME") or (home or Path.home()) / ".gemini")
    app = base / "antigravity"
    return [base / "antigravity-cli" / "conversations", app, app / "conversations"]


def _agy_databases(roots: list[Path]) -> list[Path]:
    found: dict[tuple[int, int], Path] = {}
    for root in roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for path in entries:
            if path.suffix.lower() != ".db" or path.is_symlink():
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                found.setdefault((info.st_dev, info.st_ino), path)
    return list(found.values())


def _agy_open(path: Path):
    import sqlite3

    last: Exception | None = None
    for query in ("mode=ro", "mode=ro&immutable=1"):  # immutable ignores a WAL that has no sidecar
        connection = None
        try:
            connection = sqlite3.connect(f"file:{path}?{query}", uri=True, timeout=1)
            connection.execute("select count(*) from gen_metadata limit 1").fetchone()
            return connection
        except sqlite3.Error as error:
            last = error
            if connection is not None:
                connection.close()
    raise OSError("not a readable Antigravity history database") from last


def _agy_rows(path: Path, since_ms: int) -> tuple[list[dict[str, Any]], bool]:
    """Turns of one conversation database, newest window only. `complete` is False when rows were skipped."""
    complete = True
    connection = _agy_open(path)
    try:
        rows = connection.execute("select idx, data from gen_metadata order by idx limit ?", (AGY_ROW_LIMIT + 1,)).fetchall()
        if len(rows) > AGY_ROW_LIMIT:
            rows, complete = rows[:AGY_ROW_LIMIT], False
        steps: dict[str, list[int]] = {}
        turns, pending = [], []
        for idx, blob in rows:
            if not isinstance(blob, bytes) or not 0 < len(blob) <= AGY_BLOB_LIMIT:
                complete = False
                continue
            turn = parse_antigravity_turn(blob)
            if turn is None:
                complete = False
                continue
            turn["row"] = idx
            (turns if turn.get("ms") else pending).append(turn)
        if pending:
            by_uuid: dict[str, list[int]] = {}
            by_bot: dict[str, list[tuple[str | None, int]]] = {}
            try:
                for blob, in connection.execute("select metadata from steps order by idx"):
                    if isinstance(blob, bytes) and len(blob) <= AGY_BLOB_LIMIT:
                        step, ms, bot = _pb_step_metadata(blob)
                        if step and ms:
                            by_uuid.setdefault(step, []).append(ms)
                        if bot and ms:
                            by_bot.setdefault(bot, []).append((step, ms))
            except Exception:  # noqa: BLE001 - no steps table: the rows stay unrecovered
                by_uuid, by_bot = {}, {}
            uses = collections.Counter(t["usage"].get("bot") for t in turns + pending if t["usage"].get("bot"))
            rank: dict[str, int] = {}
            for turn in sorted(pending, key=lambda t: t["row"]):
                step, bot = turn.get("step"), turn["usage"].get("bot")
                offset = rank.get(step or "", 0)
                rank[step or ""] = offset + 1
                ms = None
                if bot and bot in by_bot:
                    # An identifier shared by two steps or two generations cannot date either one.
                    matches = by_bot[bot]
                    if len(matches) == 1 and uses[bot] == 1 and matches[0][0] in (None, step):
                        ms = matches[0][1]
                elif step and offset < len(by_uuid.get(step, ())):
                    ms = by_uuid[step][offset]
                if ms:
                    turn["ms"] = ms
                    turns.append(turn)
                else:
                    complete = False
    finally:
        connection.close()
    return [turn for turn in turns if turn["ms"] >= since_ms], complete


def local_antigravity_cost(models: dict[str, Any], roots: list[Path] | None = None,
                           now: dt.datetime | None = None) -> dict[str, Any] | None:
    now = now or _now()
    today = now.astimezone().date()
    start = today - dt.timedelta(days=29)
    since_ms = int(dt.datetime.combine(start, dt.time()).timestamp() * 1000)
    databases = _agy_databases(roots if roots is not None else _agy_cost_roots())
    if not databases:
        return None
    deadline = time.monotonic() + 60
    complete, seen_rows, seen_replies = True, set(), {}
    events: list[tuple[str, dict[str, Any]]] = []
    for path in databases:
        if time.monotonic() > deadline:
            complete = False
            break
        try:
            turns, ok = _agy_rows(path, since_ms)
        except (OSError, ValueError):
            complete = False
            continue
        complete &= ok
        session = path.stem
        labels: dict[str, set[str]] = {}
        for turn in turns:
            if turn.get("label") and turn.get("model"):
                labels.setdefault(turn["label"], set()).add(turn["model"])
        for turn in turns:
            if (session, turn["row"]) in seen_rows:
                continue  # the same conversation copied into a second root
            seen_rows.add((session, turn["row"]))
            reply = turn["usage"].get("response")
            if reply:
                prior = seen_replies.get((session, reply))
                if prior is not None:
                    complete &= prior == (turn["usage"], turn["ms"])
                    continue
                seen_replies[(session, reply)] = (turn["usage"], turn["ms"])
            known = labels.get(turn.get("label") or "", set())
            turn["model"] = turn.get("model") or (next(iter(known)) if len(known) == 1 else None) or "unknown"
            events.append((session, turn))
    buckets = [[0, 0.0, 0, 0], [0, 0.0, 0, 0]]  # tokens, priced estimate, requests, unpriced requests
    by_model: dict[str, list[float]] = {}
    for _session, turn in events:
        usage = turn["usage"]
        stamp = dt.datetime.fromtimestamp(turn["ms"] / 1000, dt.timezone.utc).astimezone()
        day = stamp.date()
        if not start <= day <= today:
            continue
        inp = usage.get("system", 0) + usage.get("new", 0)
        out, read, reasoning = usage.get("output", 0), usage.get("read", 0), usage.get("reasoning", 0)
        tokens = inp + out + read + reasoning
        model = turn["model"]
        rates = None
        for candidate in (model, *(model[:-len(s)] for s in AGY_COST_SUFFIXES if model.lower().endswith(s))):
            rates = _usage_rates(models, candidate, inp + read, "antigravity")
            if rates:
                break
        cost = None
        if rates and (not read or rates[2] is not None):
            cost = (inp * rates[0] + (out + reasoning) * rates[1] + read * (rates[2] or 0)) / 1_000_000
        row = by_model.setdefault(model, [0, 0.0, 0, 0])
        for bucket in ((*buckets,) if day == today else (buckets[1],)):
            bucket[0] += tokens
            bucket[1] += cost or 0.0
            bucket[2] += 1
            bucket[3] += cost is None
        row[0] += tokens
        row[1] += cost or 0.0
        row[2] += 1
        row[3] += cost is None
    result: dict[str, Any] = {"source": "antigravity-desktop-history", "observed_at": _iso(now),
                              "estimate": "API list price", "partial": not complete}
    for prefix, (tokens, cost, requests, unpriced) in zip(("today", "last_30d"), buckets):
        result[f"{prefix}_tokens"] = tokens
        result[f"{prefix}_usd"] = round(cost, 8) if requests > unpriced or not requests else None
        result[f"{prefix}_requests"] = requests
        result[f"{prefix}_unpriced_requests"] = unpriced
    result["by_model"] = [{"model": name, "tokens": int(row[0]), "usd": round(row[1], 8) if row[2] > row[3] else None,
                           "requests": int(row[2])}
                          for name, row in sorted(by_model.items(), key=lambda item: (-item[1][0], item[0]))[:8]]
    return result


def _write_owner_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        json.dump(value, handle)
    os.replace(temp, path)


def _cached_list_prices(provider, path):
    try:
        models = _mapping(json.loads(_owner_file(path)))
        if models and time.time() - path.stat().st_mtime < 86400:
            return models
    except (OSError, ValueError, RecursionError):
        models = {}
    # Public pricing only. No local log data, tokens or account identity are sent.
    try:
        request = urllib.request.Request(COST_PRICING_URL, headers={"Accept": "application/json",
                                                                  "User-Agent": "crossfeed-orchestrator/1.0"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=8) as response:
            pricing = _mapping(json.loads(response.read(12_000_000)))
        vendors = {"codex": ("openai",), "antigravity": ("google", "anthropic", "openai")}.get(provider, ("anthropic",))
        updated = {key: {"cost": value["cost"]} for vendor in vendors for key, value in
                   _mapping(_mapping(pricing.get(vendor)).get("models")).items() if _mapping(value).get("cost")}
        if updated:
            _write_owner_json(path, updated)
            return updated
    except (OSError, ValueError, http.client.HTTPException, RecursionError):
        pass
    return models


def _write_usage_cost(provider, cache):
    try:
        models = _cached_list_prices(provider, cache.with_name(f"{provider}-cli-list-prices.json"))
        result = (local_antigravity_cost(models) if provider == "antigravity"
                  else local_usage_cost(provider, _local_cost_roots(provider), models))
        if result is None:
            return
        if result["last_30d_usd"] is None:
            # A pricing outage can't replace a complete last-good cost with false zero or a subtotal.
            old = _mapping(json.loads(_owner_file(cache))) if cache.exists() else {}
            if old.get("last_30d_usd") is not None:
                return
        _write_owner_json(cache, result)
    except (OSError, ValueError, RecursionError):
        pass  # don't advance the timestamp for a failed scan


def usage_cost(config, state_dir, now, *, refresh):
    """Read immediately; one background scan per stale provider, never on the routing hot path."""
    oracle = config.get("oracle")
    provider = COST_ORACLES.get(oracle) if isinstance(oracle, str) else None
    if not provider or config.get("local_usage_cost") is not True:
        return None
    cache = state_dir / f"{provider}-cli-usage-cost.json"
    try:
        saved = _mapping(json.loads(_owner_file(cache)))
    except (OSError, ValueError, RecursionError):
        saved = {}
    stamp = _parse_time(saved.get("observed_at"))
    age = max(0, int((now - stamp).total_seconds())) if stamp else None
    if refresh and (age is None or age >= COST_TTL_S):
        fd = None
        try:
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(cache.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Retry no faster than five minutes even when there are no logs or pricing is down.
            if not os.fstat(fd).st_size or time.time() - os.fstat(fd).st_mtime >= 300:
                os.pwrite(fd, b"1", 0)
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--local-cost", provider,
                                          str(cache.resolve())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(fd,))
                threading.Thread(target=child.wait, daemon=True).start()
        except OSError:
            pass
        finally:
            if fd is not None:
                os.close(fd)
    return {**saved, "age_s": age, "stale": age >= COST_TTL_S} if stamp else None


OBSERVERS: dict[str, Callable[..., dict[str, Any]]] = {
    "codex": observe_codex,
    "codex-oauth": observe_codex_oauth,
    "claude": observe_claude,
    "antigravity": observe_antigravity,
    "copilot": observe_copilot,
    "openrouter": observe_openrouter,
}


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--status":
    os.nice(10)
    _write_provider_status(Path(sys.argv[2]))


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--local-cost" and sys.argv[2] in COST_ORACLES.values():
    os.nice(10)
    _write_usage_cost(sys.argv[2], Path(sys.argv[3]))
