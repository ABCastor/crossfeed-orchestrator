"""Provider setup, using existing adapters and the overlay's admission gates."""
from __future__ import annotations

import contextlib
import copy
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

KINDS = ("openai-compatible", "crossfeed-chat", "codex", "claude", "agy", "opencode", "copilot", "openrouter")
CLI = {"codex": "codex", "claude": "claude", "agy": "agy", "opencode": "opencode", "copilot": "copilot"}
FIXED_POOLS = {"codex": "codex", "claude": "claude", "copilot": "github-copilot-student"}
ID = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}\Z")


class ProviderError(ValueError):
    pass


def reference(value):
    value = value.strip()
    if not value:
        return ""
    if re.fullmatch(r"(?:env:)?[A-Za-z_][A-Za-z0-9_]*", value):
        return "env:" + value.removeprefix("env:")
    if value.startswith("op://"):
        if not re.fullmatch(r"op://[a-z0-9]{26}/[^/\r\n]+/[^/\r\n]+", value):
            raise ProviderError("Use a 1Password reference with the vault ID, item and field.")
        return value
    path = value.removeprefix("file:")
    if not Path(path).expanduser().is_absolute() or any(c in value for c in "\r\n\0"):
        raise ProviderError("Use an environment variable name, an absolute key-file path or an op:// reference.")
    return "file:" + str(Path(path).expanduser())


def resolve_key(ref):
    ref = reference(ref)
    try:
        if not ref:
            return ""
        if ref.startswith("env:"):
            key = os.environ.get(ref[4:], "")
        elif ref.startswith("file:"):
            key = Path(ref[5:]).read_text(encoding="utf-8")
        else:
            result = subprocess.run(["op", "read", ref], capture_output=True, text=True, timeout=10)
            if result.returncode:
                raise ProviderError("1Password could not read that reference. Check access and try again.")
            key = result.stdout
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ProviderError("The key reference could not be read. Check the variable, file or 1Password access.") from None
    key = key.strip()
    if not key or len(key) > 8192 or any(c.isspace() for c in key):
        raise ProviderError("The key is missing or invalid. Supply one key without spaces.")
    if key.startswith("sk-ant-oat"):
        raise ProviderError("Use an API key. Subscription login tokens are not supported here.")
    return key


def api_base(value, kind):
    try:
        url = urllib.parse.urlsplit(value.strip().rstrip("/"))
        local = url.hostname in {"127.0.0.1", "localhost", "::1"}
        valid = (url.hostname and (url.scheme == "https" or url.scheme == "http" and local)
                 and not url.username and not url.password and not url.query and not url.fragment)
        _ = url.port
        if kind == "crossfeed-chat":
            valid = valid and url.scheme == "http" and url.hostname == "127.0.0.1" and url.port
        if not valid:
            raise ValueError()
        base = urllib.parse.urlunsplit(url)
        if not url.path:
            base += "/v1"
        if kind == "crossfeed-chat" and urllib.parse.urlsplit(base).path != "/v1":
            raise ValueError()
        if kind == "openrouter" and base != "https://openrouter.ai/api/v1":
            raise ValueError()
        return base
    except (ValueError, AttributeError):
        raise ProviderError("Use an HTTPS API base URL, or HTTP on this computer. Crossfeed Chat needs http://127.0.0.1:PORT/v1.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def openrouter_pricing(row):
    """Catalog dollars per token become Pi's dollars per million tokens."""
    prices = row.get("pricing") or {}
    try:
        prompt, completion = float(prices["prompt"]), float(prices["completion"])
        rates = {"input": prompt * 1000000, "output": completion * 1000000,
                 "cacheRead": float(prices.get("input_cache_read", prompt)) * 1000000,
                 "cacheWrite": float(prices.get("input_cache_write", prompt)) * 1000000,
                 "request_usd": float(prices.get("request", 0))}
        if any(not math.isfinite(v) or v < 0 for v in rates.values()):
            return None
        return rates
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def probe(kind, base_url="", credential_ref="", key="", models=""):
    if kind not in KINDS:
        raise ProviderError("Choose a supported provider type.")
    ref = reference(credential_ref)
    pricing = {}
    if kind in {"crossfeed-chat", "openrouter"} and not (key or ref):
        raise ProviderError("Supply the gateway or OpenRouter key, or a reference to it.")
    if key and ref:
        raise ProviderError("Supply either a key reference or a pasted key, not both.")
    if key:
        if len(key) > 8192 or any(c.isspace() for c in key) or key.startswith("sk-ant-oat"):
            raise ProviderError("Use one API key without spaces, not a subscription login token.")
    if kind in CLI:
        if ref or key or base_url:
            raise ProviderError("These CLIs use their own sign-in. Leave the URL and key fields empty.")
        try:
            result = subprocess.run([CLI[kind], "--version"], capture_output=True, timeout=5)
            if result.returncode:
                raise OSError()
        except (OSError, subprocess.SubprocessError):
            raise ProviderError("That CLI is unavailable. Install it and sign in, then try again.") from None
        offered = []
        message = "CLI found. Sign-in and model access still need verification."
        base = ""
    else:
        base = api_base(base_url, kind)
        secret = key or resolve_key(ref)
        if secret and secret in base:
            raise ProviderError("Keep the API key in the key field or reference, not in the URL.")
        headers = {"Accept": "application/json"}
        if secret:
            headers["Authorization"] = "Bearer " + secret
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(urllib.request.Request(base + "/models", headers=headers), timeout=5) as response:
                raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError()
            data = json.loads(raw)
            rows = data["data"]
            if not isinstance(rows, list) or len(rows) > 2000:
                raise ValueError()
            if kind == "crossfeed-chat":
                try:
                    from .chatgpt_transport import catalog_models, Rejected
                except ImportError:
                    from chatgpt_transport import catalog_models, Rejected
                try:
                    offered = list(catalog_models(data))
                except Rejected:
                    raise ValueError() from None
            else:
                offered = list(dict.fromkeys(row["id"] for row in rows
                                             if isinstance(row, dict) and isinstance(row.get("id"), str)
                                             and MODEL.fullmatch(row["id"])))
            if kind == "openrouter":
                pricing = {row["id"]: price for row in rows if isinstance(row, dict)
                           and row.get("id") in offered
                           and (row.get("architecture") or {}).get("output_modalities") == ["text"]
                           and (price := openrouter_pricing(row)) is not None}
                offered = [model for model in offered if model in pricing]
            if not offered or any(secret and secret in item for item in offered):
                raise ValueError()
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise ProviderError("The server refused the key. Check it and try again.") from None
            raise ProviderError("The models call failed. Check the API base URL and server, then try again.") from None
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
            raise ProviderError("The server did not return a usable model list. Check its /v1/models endpoint.") from None
        message = "Connected. Model IDs are available; this does not verify execution or billing."
    if kind in CLI and not models:
        return {"models": [], "selected": [], "base_url": base, "message": message}
    selected = models if isinstance(models, list) else [m.strip() for m in re.split(r"[,\r\n]+", models.strip()) if m.strip()] if models.strip() else offered
    if not selected or len(selected) > 2000 or any(not isinstance(m, str) or (kind != "crossfeed-chat" and not MODEL.fullmatch(m)) for m in selected):
        raise ProviderError("Enter model IDs, separated by commas or new lines.")
    selected = list(dict.fromkeys(selected))
    if offered and any(m not in offered for m in selected):
        raise ProviderError("One of the selected models is absent from the server. Probe again and choose a listed ID.")
    return {"models": offered or selected, "selected": selected, "base_url": base, "message": message, "pricing": pricing}


def list_sources(roster):
    return [{"id": ident, "label": source["label"], "kind": source["kind"],
             "models": source["models"], "state": "saved; admission checked separately"}
            for ident, source in roster.get("provider_sources", {}).items()]


@contextlib.contextmanager
def edit_overlay(path):
    # The existing ordered writer preserves symlinks; a lock avoids lost console/CLI edits.
    try:
        from .fleetctl import read_overlay, _atomic_json_ordered
    except ImportError:
        from fleetctl import read_overlay, _atomic_json_ordered
    path = path.expanduser().resolve()
    with path.with_suffix(path.suffix + ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        roster = read_overlay(path, discover=False)
        original = path.read_bytes()
        yield roster
        backup_dir = path.parent / "overlay-backups"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        fd, backup = tempfile.mkstemp(prefix=path.name + ".", suffix=".json", dir=backup_dir)
        with os.fdopen(fd, "wb") as handle:
            handle.write(original)
        _atomic_json_ordered(path, roster)


def add(path, *, id, kind, label="", base_url="", credential_ref="", key="", models="", acceptance="", daily_cap="0"):
    if not ID.fullmatch(id) or kind not in KINDS:
        raise ProviderError("Use a unique provider ID with lowercase letters, numbers and hyphens, starting with a letter.")
    label = label.strip() or id
    if len(label) > 80 or any(ord(c) < 32 for c in label):
        raise ProviderError("Use a provider name of up to 80 characters.")
    if kind == "crossfeed-chat" and not acceptance.strip():
        raise ProviderError("Record your acceptance of the subscription-relay account risk before adding Crossfeed Chat.")
    try:
        cap = float(daily_cap)
        if not 0 <= cap <= 100000:
            raise ValueError()
    except (ValueError, TypeError):
        raise ProviderError("Enter a daily estimated spend cap of zero or more. Zero blocks paid calls.") from None
    # Probe before any configuration or key-file write, including when models were supplied.
    result = probe(kind, base_url, credential_ref, key, models)
    if not result["selected"]:
        raise ProviderError("Enter the native model IDs you want to add. Copilot uses auto.")
    if len(result["selected"]) > 200:
        raise ProviderError("This server offers more than 200 models. Check the connection, then keep up to 200 IDs to add.")
    ref = reference(credential_ref)
    with edit_overlay(path) as roster:
        sources = roster.setdefault("provider_sources", {})
        if (id in sources or id in roster.get("quota_pools", {})
                or id == "chatgpt" and roster.get("chatgpt_gateway")
                or any(l.get("lane_id", "").startswith(id + ":") for l in roster.get("lanes", []))):
            raise ProviderError("That provider ID already exists. Choose another ID.")
        pool = FIXED_POOLS.get(kind, id)
        source = {"kind": kind, "label": label, "base_url": result["base_url"], "models": result["selected"],
                  "pool": pool, "owns_pool": pool not in roster.get("quota_pools", {})}
        if kind == "crossfeed-chat":
            template = {"harness": "chatgpt-chat", "provider": "openai", "kind": "http",
                        "quality_tier": "unmeasured", "roles": ["default", "lookup", "review", "hard-reasoning", "long-context"],
                        "capabilities": {"input": ["text"], "output": ["text"]}, "timeout_s": 600,
                        "transport": {"api_base": result["base_url"]}, "effort": {"shape": "none"}}
            template["auth"] = {"kind": "subscription-relay", "terms_class": "owner-accepted",
                                "owner_acceptance": {"date": datetime.date.today().isoformat(), "quote": acceptance.strip()}}
            template["quota_pool"] = pool
            template["wake_state_file"] = str(path.expanduser().resolve().parent / "provider-wakes" / id / "wake-state.json")
            source["gateway"] = {"service_id": id, "lane_prefix": id + ":", "lane_template": template,
                                 "models": result["selected"] if models else None}
            lanes = []
        else:
            harness = "pi" if kind in {"openai-compatible", "openrouter"} else kind
            template = {"harness": harness,
                        "provider": {"codex": "openai", "claude": "anthropic", "agy": "antigravity",
                                     "copilot": "github-copilot"}.get(harness, harness),
                        "capabilities": {"input": ["text"], "output": ["text"]},
                        "allowed_modes": ["read-only"], "timeout_s": 600}
            source["owned_cards"], source["owned_effort"] = [], []
            lanes = []
            for index, model in enumerate(result["selected"]):
                selector = id + "/" + model if harness == "pi" else model
                if (any(l["harness"] == harness and l["selector"] == selector for l in roster.get("lanes", []))
                        or harness in {"codex", "claude"} and model in roster.get("model_cards", {})):
                    raise ProviderError("A model route already exists for that CLI. Reuse it instead of adding a duplicate.")
                if kind == "copilot" and model != "auto":
                    raise ProviderError("The Copilot adapter supports only the auto model.")
                lane = copy.deepcopy(template)
                model_key = id + "/" + model if harness == "pi" else model
                if len(model_key) > 80:
                    raise ProviderError("The provider ID and model ID together must fit within 80 characters.")
                lane.update(lane_id=id + "-" + str(index + 1), model_key=model_key, selector=selector,
                            quota_pool=pool, provider=id if harness == "pi" else template["provider"],
                            access_status="unverified", admission_status="active", verified_at=None,
                            roles=["default", "lookup", "review"], max_parallel=1, max_tasks_per_run=1,
                            retries=0, provider_source=id, quality_tier="unmeasured",
                            notes="Discovery only; verify execution and review roles, effort and billing before admission.")
                if kind == "openrouter":
                    lane["api_pricing"] = result["pricing"][model]
                if kind in {"openai-compatible", "openrouter"}:
                    lane["auth"] = {"kind": "api-key", "terms_class": "api-only"}
                if harness == "pi":
                    lane["transport"] = {"api_base": result["base_url"], "api": "openai-completions"}
                    lane["allowed_modes"] = ["read-only"]
                    lane["effort"] = {"shape": "none"}
                    if model_key in roster.get("effort", {}) or model_key in roster.get("model_cards", {}):
                        raise ProviderError("That provider ID collides with an existing model. Choose another ID.")
                    source["owned_effort"].append(model_key)
                    source["owned_cards"].append(model_key)
                    roster.setdefault("effort", {})[model_key] = {"levels": {"pi": []}, "default": "provider-default",
                        "control": "unverified", "evidence": {"status": "unmeasured", "source": "Models discovery only"}}
                    roster.setdefault("model_cards", {})[model_key] = {"name": model, "pool": pool}
                if any(l["lane_id"] == lane["lane_id"] for l in roster.get("lanes", [])):
                    raise ProviderError("That provider ID collides with an existing lane. Choose another ID.")
                if kind in {"copilot", "openrouter"} and model_key not in roster.get("effort", {}):
                    roster.setdefault("effort", {})[model_key] = {"levels": {harness: []}, "default": "service-chosen",
                        "control": "none", "evidence": {"status": "unmeasured", "source": "Existing adapter has no effort control"}}
                    source["owned_effort"].append(model_key)
                lanes.append(lane)
            if harness in {"codex", "claude"} and not any(l.get("quota_pool") == pool for l in roster.get("lanes", [])):
                # A direct pool lists models as cards. Adding a lane would turn off
                # the original card routes by changing pool_is_direct().
                source["model_keys"] = []
                for lane in lanes:
                    model = lane["model_key"]
                    roster.setdefault("model_cards", {})[model] = {
                        "name": model, "pool": pool, "run_as": lane["selector"], "provider_source": id,
                        "access_status": "unverified", "admission_status": "active"}
                    source["model_keys"].append(model)
                    source["owned_cards"].append(model)
                lanes = []
            source["lane_ids"] = [l["lane_id"] for l in lanes]
        # No secret may escape via a name, selector, acceptance quote or error.
        secret = key or resolve_key(ref)
        if secret and secret in json.dumps({"source": source, "lanes": lanes}):
            raise ProviderError("Keep the API key only in the key field.")
        if key:
            key_dir = path.expanduser().resolve().parent / "provider-keys"
            key_dir.mkdir(mode=0o700, exist_ok=True)
            fd, filename = tempfile.mkstemp(prefix=id + ".", dir=key_dir)
            with os.fdopen(fd, "w") as handle:
                handle.write(key)
            ref = "file:" + filename
        source["credential_ref"] = ref
        if kind == "crossfeed-chat":
            source["gateway"]["lane_template"]["auth"]["key_ref"] = ref
        else:
            for lane in lanes:
                if kind in {"openai-compatible", "openrouter"}:
                    lane["auth"]["key_ref"] = ref
            roster.setdefault("lanes", []).extend(lanes)
            for bands in roster.get("routing", {}).get("roles", {}).values():
                for ranking in bands.values():
                    if isinstance(ranking, list):
                        ranking.extend(source["lane_ids"])
        if source["owns_pool"]:
            roster.setdefault("quota_pools", {})[pool] = {"label": label, "plan": {"name": label, "billing": "unknown"}}
            if kind in {"openai-compatible", "openrouter"}:
                roster["quota_pools"][pool]["daily_usd_cap"] = cap
        sources[id] = source
    return {"id": id, "models": result["selected"], "message": result["message"] + (
        " Saved. New lanes are unverified; review and verify them in the overlay before routing." if kind != "crossfeed-chat"
        else " Saved. Saved-worker discovery and the existing relay gates control routing.")}


def remove(path, id):
    with edit_overlay(path) as roster:
        source = roster.get("provider_sources", {}).pop(id, None)
        if source is None:
            raise ProviderError("That added provider does not exist.")
        lanes = {l["lane_id"] for l in roster.get("lanes", []) if l.get("provider_source") == id}
        roster["lanes"] = [l for l in roster.get("lanes", []) if l["lane_id"] not in lanes]
        for bands in roster.get("routing", {}).get("roles", {}).values():
            for band, ranking in bands.items():
                if isinstance(ranking, list):
                    bands[band] = [l for l in ranking if l not in lanes]
        for profile in roster.get("swarm_profiles", {}).values():
            for band in profile.get("bands", {}).values():
                if any(w.get("lane_id") in lanes for w in band.get("workers", [])):
                    raise ProviderError("This provider is used in a swarm profile. Remove those worker references first.")
        for section, owned in (("effort", "owned_effort"), ("model_cards", "owned_cards")):
            for model in source.get(owned, []):
                if not any(l["model_key"] == model for l in roster["lanes"]):
                    roster.get(section, {}).pop(model, None)
        if (source["owns_pool"] and not any(l.get("quota_pool") == source["pool"] for l in roster["lanes"])
                and not any(c.get("pool") == source["pool"] for c in roster.get("model_cards", {}).values() if isinstance(c, dict))):
            roster.get("quota_pools", {}).pop(source["pool"], None)
    return {"id": id, "message": "Provider removed. The previous overlay and any key file are kept for recovery."}


def expand_chats(roster, state_dir):
    """Each added instance uses the same live catalog admission in its own namespace."""
    try:
        from .chatgpt_catalog import expand
    except ImportError:
        from chatgpt_catalog import expand
    for ident, source in roster.get("provider_sources", {}).items():
        if source.get("kind") != "crossfeed-chat":
            continue
        subset = {"chatgpt_gateway": source["gateway"], "lanes": [],
                  "routing": {"roles": {role: {band: ["chatgpt"] for band in bands}
                              for role, bands in roster.get("routing", {}).get("roles", {}).items()}}}
        generated = expand(subset, state_dir / "providers" / ident)
        roster.setdefault("lanes", []).extend(generated["lanes"])
        for section in ("model_cards", "effort"):
            roster.setdefault(section, {}).update(generated.get(section, {}))
        for role, bands in generated["routing"]["roles"].items():
            for band, ranking in bands.items():
                roster["routing"]["roles"][role][band].extend(ranking)
    return roster
