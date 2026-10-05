"""Private loopback HTTP transport and fail-fast Crossfeed Chat worker admission."""
from __future__ import annotations

import json
from pathlib import Path
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


class Rejected(Exception):
    def __init__(self, code, message, *, pro_spent=False, reset_at=None):
        self.code, self.message = code, message
        self.pro_spent, self.reset_at = pro_spent, reset_at


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


LEVEL_NAMES = ("instant", "medium", "high", "xhigh", "pro")


def canonical_selector(value):
    """Accept Crossfeed Chat's exact saved-label IDs, without decoding aliases."""
    if not isinstance(value, str) or not value.startswith("chatgpt:"):
        return None
    label = value[len("chatgpt:"):]
    if (not label or len(label.encode()) > 128 or label != label.strip()
            or any(unicodedata.category(c).startswith("C") for c in label)):
        raise Rejected(3, "invalid ChatGPT worker selector")
    return label


def catalog_models(value):
    if value.get("object") != "list" or not isinstance(value.get("data"), list):
        raise Rejected(6, "invalid gateway catalog")
    found, seen = {}, set()
    for row in value["data"]:
        if not isinstance(row, dict) or row.get("object") != "model":
            raise Rejected(6, "invalid gateway model row")
        selector = row.get("id")
        label = canonical_selector(selector)
        if label is None or selector in seen or not isinstance(row.get("saved"), bool):
            raise Rejected(6, "duplicate or invalid gateway model selector")
        seen.add(selector)
        # A previously contacted but unsaved label has no wake configuration.
        if not row["saved"]:
            continue
        name, position = row.get("row"), row.get("level")
        replicas = row.get("replicas", 1)
        if (not isinstance(name, str) or not name.strip() or name != name.strip() or len(name.encode()) > 128
                or any(unicodedata.category(c).startswith("C") for c in name)
                or type(position) is not int or not 0 <= position < len(LEVEL_NAMES)):
            raise Rejected(6, "invalid saved worker row or level")
        if type(replicas) is not int or replicas < 1:
            raise Rejected(6, "invalid saved worker replica count")
        found[selector] = {"id": selector, "name": name + " / " + LEVEL_NAMES[position],
                           "worker_label": label, "worker_level": LEVEL_NAMES[position],
                           "row": name, "position": position, "replicas": replicas,
                           "older": name.casefold() != "latest"}
    return found


def settings(lane):
    auth = lane.get("auth", {})
    accepted = auth.get("owner_acceptance", {})
    if (auth.get("kind") != "subscription-relay" or auth.get("terms_class") != "owner-accepted"
            or not accepted.get("quote") or not accepted.get("date")):
        raise Rejected(5, "subscription relay requires recorded owner acceptance; vendor permission is unconfirmed")
    if lane.get("effort", {}).get("shape") != "none":
        raise Rejected(3, "Crossfeed Chat cannot expose per-request thinking controls")
    base = lane.get("transport", {}).get("api_base", "").rstrip("/")
    try:
        url = urllib.parse.urlsplit(base)
        valid = (url.scheme == "http" and url.hostname == "127.0.0.1"
                 and url.port and not url.username and not url.password and url.path == "/v1"
                 and not url.query and not url.fragment)
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise Rejected(3, "lane API base must be an explicit 127.0.0.1 HTTP /v1 endpoint")
    try:
        if "key_ref" in auth:
            try:
                from providers import resolve_key, ProviderError
            except ImportError:
                from scripts.providers import resolve_key, ProviderError
            try:
                key = resolve_key(auth["key_ref"])
            except ProviderError:
                raise Rejected(5, "bearer-key reference is missing or unreadable") from None
        else:
            key = Path(auth["key_file"]).expanduser().read_text().strip()
    except (OSError, KeyError, TypeError, UnicodeError):
        raise Rejected(5, "bearer-key file is missing or unreadable")
    if not key or len(key) > 8192 or any(c.isspace() for c in key):
        raise Rejected(5, "invalid bearer-key file")
    return base, key


def request(base, key, path, payload=None, timeout=3, progress=None, idempotency_key=None, session_id=None):
    headers = {"Authorization": "Bearer " + key}
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if session_id:
        headers["X-Crossfeed-Session"] = session_id
    req = urllib.request.Request(base + path, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            chunks, size = [], 0
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise Rejected(5, "oversized gateway response")
                chunks.append(chunk)
                if progress:
                    progress()
            value = json.loads(b"".join(chunks))
            if not isinstance(value, dict):
                raise ValueError()
            return value
    except urllib.error.HTTPError as error:
        # Read only to classify a worker's limit/reset; never echo gateway text.
        try:
            from chatgpt_pro import RATE_LIMIT, reset_at
        except ImportError:
            from scripts.chatgpt_pro import RATE_LIMIT, reset_at
        try:
            value = json.loads(error.read(65536))
            detail = value.get("error", {})
            message = detail.get("message", "") if isinstance(detail, dict) else str(detail)
            reason = detail.get("code", "") if isinstance(detail, dict) else ""
            limited = error.code == 429 or bool(RATE_LIMIT.search(message + " " + reason))
            until = reset_at(message, detail.get("reset_at") if isinstance(detail, dict) else None) if limited else None
        except (ValueError, AttributeError, TypeError):
            limited, until = error.code == 429, None
        code = 4 if error.code == 429 else 6 if error.code in {503, 504} else 5
        raise Rejected(code, "gateway HTTP " + str(error.code), pro_spent=limited, reset_at=until)
    except (OSError, ValueError, urllib.error.URLError):
        raise Rejected(6, "gateway unavailable or no valid terminal response")


def contacts(status):
    """Validate labelled contacts supplied by Crossfeed Chat."""
    rows = status.get("workers")
    if not isinstance(rows, list):
        raise Rejected(6, "gateway did not list labelled worker contacts")
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            raise Rejected(6, "invalid gateway worker contact")
        if row["label"] in found:
            raise Rejected(6, "duplicate gateway worker label")
        found[row["label"]] = row.get("contact")
    return found
