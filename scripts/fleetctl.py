#!/usr/bin/env python3
"""Quality-first model routing and honest OpenCode Go usage telemetry.

The Go dashboard is the only authoritative quota source. OpenCode's local event
cost is retained as an estimated equivalent cost and is never converted into a
remaining-quota percentage.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import copy
import datetime as dt
import fcntl
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import uuid
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

try:
    import chatgpt_pro
except ImportError:
    # Hooks also load this controller by file path from a different directory.
    # Resolve its sibling without relying on the caller's Python import path.
    _pro_spec = importlib.util.spec_from_file_location("fleet_chatgpt_pro", Path(__file__).with_name("chatgpt_pro.py"))
    chatgpt_pro = importlib.util.module_from_spec(_pro_spec)
    _pro_spec.loader.exec_module(chatgpt_pro)


def _xdg_config() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or "~/.config"


def _xdg_state() -> str:
    return os.environ.get("XDG_STATE_HOME") or "~/.local/state"


# The product's name, in one place: the console page, its title and the brief header read it.
PRODUCT_NAME = "Crossfeed Orchestrator"

SCHEMA = "model-fleet-runtime/v1"
RUN_SCHEMA = "opencode-run-usage/v2"
AFK_SCHEMA = "afk-attempt/v1"
DEFAULT_OVERLAY = Path(
    os.environ.get("ACCESS_OVERLAY", _xdg_config() + "/orchestrator/access-overlay.json")
).expanduser()
DEFAULT_STATE_DIR = Path(
    os.environ.get(
        "FLEET_STATE_DIR", _xdg_state() + "/orchestrator"
    )
).expanduser()
DEFAULT_DB = Path(
    os.environ.get("OPENCODE_DB", "~/.local/share/opencode/opencode.db")
).expanduser()


class FleetError(RuntimeError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path: Path, value: Any, *, allow_nan: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=allow_nan)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def locked_runtime(state_dir: Path) -> Iterator[dict[str, Any]]:
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "runtime.lock"
    state_path = state_dir / "runtime.json"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        runtime = load_json(state_path, {}) or {}
        runtime.setdefault("schema", SCHEMA)
        runtime.setdefault("quota_snapshots", {})
        runtime.setdefault("pool_circuits", {})
        runtime.setdefault("leases", [])
        yield runtime
        runtime["updated_at"] = iso()
        atomic_json(state_path, runtime)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def ledger_lock(state_dir: Path) -> Iterator[None]:
    """Serialize all runs.jsonl access so a parallel append cannot tear a line
    or make a concurrent spend read undercount. Separate from runtime.lock; when
    both are needed the caller takes runtime.lock first (see acquire_lease)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "runs.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_overlay(path: Path, state_dir: Path | None = None, *, discover: bool = True) -> dict[str, Any]:
    roster = load_json(path)
    if not isinstance(roster, dict):
        raise FleetError(f"access overlay is missing or invalid: {path}")
    if roster.get("schema_version") != 3:
        raise FleetError("access overlay schema_version must be 3")
    if discover and ("chatgpt_gateway" in roster or
                     any(lane.get("gateway_service") for lane in roster.get("lanes", []))):
        try:
            from chatgpt_catalog import expand
        except ImportError:
            from scripts.chatgpt_catalog import expand
        roster = expand(roster, state_dir or DEFAULT_STATE_DIR)
    if discover and roster.get("provider_sources"):
        try:
            from .providers import expand_chats
        except ImportError:
            try:
                from providers import expand_chats
            except ImportError:
                from scripts.providers import expand_chats
        roster = expand_chats(roster, state_dir or DEFAULT_STATE_DIR)
    if discover:
        chatgpt_pro.refresh(roster, state_dir or DEFAULT_STATE_DIR)
    return roster


def lane_map(roster: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {lane["lane_id"]: lane for lane in roster["lanes"]}


def lane_pools(roster: dict[str, Any]) -> set[str]:
    """Every quota pool a lane can spend from. Routing may now pick a lane funded
    by a different budget, so the dispatch path has to keep all of them fresh, not
    just the primary one: selecting a lane on an UNKNOWN pool is the stale-quota
    fault this refresh exists to prevent."""
    return {
        lane["quota_pool"]
        for lane in roster.get("lanes", [])
        if lane.get("quota_pool")
    }


# Bands are thresholds on an observed percentage. They answer "how much is gone",
# never "how long until it comes back" -- and only the second question decides
# whether spending is wise, because allowance does not roll over.
#
# The real condition is a SURPLUS: allowance is use-it-or-lose-it exactly when it
# cannot physically be consumed before the reset at the rate it is actually being
# consumed. Elapsed-window proxies ("the last tenth of the window") get this wrong
# in both directions -- they ration a barely-touched pool that will obviously
# expire unused, and they green-light a nearly-spent pool that a burst could still
# exhaust. The fallback below is that proxy, used only when no rate is knowable.
SPEND_DOWN_FRACTION = 0.10
SPEND_DOWN_MAX_S = 3600
# How much of a window must have elapsed before its average burn rate is worth
# extrapolating to the reset. Below this the sample is too short to mean anything.
MIN_BURN_SAMPLE_FRACTION = 0.10


def format_duration(seconds: int) -> str:
    """Compact `2d 3h` / `47m` for a reset clock read by a human at a glance."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def spend_down_horizon_s(window: dict[str, Any]) -> int:
    """Fallback only: how close to its reset a window counts as expiring when no
    burn rate is knowable. The last tenth of the window, capped at an hour.
    """
    minutes = window.get("window_minutes")
    if isinstance(minutes, (int, float)) and minutes > 0:
        return int(min(SPEND_DOWN_MAX_S, minutes * 60 * SPEND_DOWN_FRACTION))
    return SPEND_DOWN_MAX_S


def window_surplus(window: dict[str, Any]) -> dict[str, Any]:
    """Forecast unused allowance without treating critical actual usage as spare."""
    verdict = _window_surplus_forecast(window)
    if _state_for_used(float(window.get("used_percent", 0))) == "CRITICAL":
        verdict.update(surplus=False, constrained_by="actual_usage")
    return verdict


def _window_surplus_forecast(window: dict[str, Any]) -> dict[str, Any]:
    """Will this window still hold unspent allowance when it resets?

    Returns `{"surplus": bool, "basis": str, ...}`. `surplus` true means the
    allowance is about to be destroyed, so it should be spent rather than saved.

    Three bases, best evidence first:

    1. `observed_pace` -- the oracle projects the window's own exhaustion. Trust
       it directly: `eta_seconds > seconds_to_reset` means it runs out AFTER the
       reset, i.e. never, i.e. surplus. `will_last_to_reset` says the same thing
       as a boolean. This is the only basis weighted toward RECENT burn.
    2. `average_burn` -- derived from what every snapshot already carries. With
       `elapsed = window_seconds - seconds_to_reset` and a flat rate of
       `used / elapsed`, the projected total at reset is
           used + (used / elapsed) * seconds_to_reset == used * window / elapsed
       and there is surplus when that lands under 100%. Caveat worth knowing: it
       is the AVERAGE over the window so far, so a pool left idle for hours and
       then hammered reads as low-burn and can be called surplus wrongly. Basis 1
       is preferred precisely because it does not have this blind spot.
    3. `elapsed_window` -- no rate available at all, so fall back to the crude
       proxy: inside the last tenth of the window, capped at an hour.
    """
    seconds_left = int(window.get("seconds_to_reset", 0))
    used = float(window.get("used_percent", 0))

    eta = window.get("eta_seconds")
    lasts = window.get("will_last_to_reset")
    if isinstance(lasts, bool) or isinstance(eta, (int, float)):
        surplus = bool(lasts) if isinstance(lasts, bool) else float(eta) > seconds_left
        return {"surplus": surplus, "basis": "observed_pace", "eta_seconds": eta}

    minutes = window.get("window_minutes")
    if isinstance(minutes, (int, float)) and minutes > 0:
        window_s = float(minutes) * 60
        elapsed = window_s - seconds_left
        # Minimum sample before the rate is worth extrapolating. Found by running
        # this on live pools: a weekly window four hours past its reset projected
        # 375% and 754% used, because it was stretching a few hours of burn across
        # seven days. Early in a window the estimator has enormous variance, and a
        # confident wrong projection is worse than admitting there is no rate yet.
        if elapsed >= window_s * MIN_BURN_SAMPLE_FRACTION:
            projected = used * window_s / elapsed
            return {
                "surplus": projected < 100.0,
                "basis": "average_burn",
                "projected_used_percent_at_reset": round(projected, 1),
            }

    return {
        "surplus": seconds_left <= spend_down_horizon_s(window),
        "basis": "elapsed_window",
    }


def _state_for_used(used_percent: int) -> str:
    if used_percent >= 90:
        return "CRITICAL"
    if used_percent >= 75:
        return "CONSERVE"
    if used_percent >= 50:
        return "HEALTHY"
    return "ABUNDANT"


# Spend levels: how much of each pool's allowance the operator wants used, one setting per pool.
#
#   off     never route there; every wrapper refuses the pool (exit 5)
#   low     cheapest capable lane first, one call at a time; the big model only as a one-shot
#   normal  the measured behaviour: quota bands decide
#   high    strong models freely: no step-down and no frontier clamp until the pool is CRITICAL
#   forced  route there even when the quota looks low (the gauges are ignored)
#
# Stored in runtime.json under `switches`, the key the three-way hand switch already used, so
# existing files keep their meaning and an older reader keeps working: "off" stays "off",
# "forced" is written as the old "on", "normal" is the absence of an entry, and the two new
# values ("low", "high") read as "auto" to anything that predates them.
LEVELS = ("off", "low", "normal", "high", "forced")
# One line each, read by agents in `brief` and by people on the console page.
LEVEL_MEANING = {
    "off": "never used",
    "low": "cheapest lane first, one call at a time, big model only as a one-shot",
    "normal": "routing follows the quota gauges",
    "high": "strong models freely, full parallel until the pool is critical",
    "forced": "used even when the quota looks low",
}
# The same meanings, cut to what an agent needs in a line it reads on every plan.
LEVEL_BRIEF = {
    "off": "never",
    "low": "cheapest lane, 1 at a time, big model one-shot only",
    "normal": "follow the quota gauges",
    "high": "strong models, full parallel until critical",
    "forced": "use even if quota looks low",
}
_LEVEL_STORED = {"off": "off", "low": "low", "high": "high", "forced": "on"}
_STORED_LEVEL = {"off": "off", "low": "low", "high": "high", "on": "forced", "forced": "forced"}
_LEGACY_SWITCH_WORD = {"off": "off", "forced": "on"}


def pool_level(runtime: dict[str, Any], pool: str | None) -> str:
    switches = runtime.get("switches")
    if not isinstance(switches, dict) or pool is None:
        return "normal"
    return _STORED_LEVEL.get(switches.get(pool), "normal")


def set_pool_level(runtime: dict[str, Any], pool: str, level: str) -> None:
    if level not in LEVELS:
        raise FleetError(f"unknown level {level}; levels: {', '.join(LEVELS)}")
    switches = runtime.get("switches")
    if not isinstance(switches, dict):
        switches = runtime["switches"] = {}
    if level == "normal":
        switches.pop(pool, None)
    else:
        switches[pool] = _LEVEL_STORED[level]


PROFILES = ("save-claude", "save-codex", "balanced", "max-quality", "reset")


def detect_lead_pool(explicit: str | None = None, env: dict[str, str] | None = None) -> str | None:
    """Identify the lead's funding pool, never infer Pi funding from an API vendor."""
    if explicit is not None:
        return explicit
    env = os.environ if env is None else env
    # A live Codex thread is stronger than a Claude parent marker inherited by a worker.
    if env.get("CODEX_THREAD_ID"):
        return "codex"
    if env.get("CLAUDECODE") or any(key.startswith("CLAUDE_CODE_") for key in env):
        return "claude"
    if env.get("PI_QUOTA_POOL") or env.get("PI_PROVIDER"):
        return env.get("PI_QUOTA_POOL") or {
            "openai-codex": "codex",
            "google-antigravity": "antigravity-gemini",
        }.get(env.get("PI_PROVIDER", "").lower())
    if any(key.startswith("CODEX_") for key in env):
        return "codex"
    return None


def resolve_lead_pool(roster: dict[str, Any], explicit: str | None = None) -> str | None:
    pool = detect_lead_pool(explicit)
    if pool and pool not in roster.get("quota_pools", {}):
        if explicit is not None:
            raise FleetError(f"unknown lead pool {pool}")
        return None
    return pool


def lead_pressure(runtime: dict[str, Any], pool: str | None,
                  roster: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Lead preservation uses observed pressure, independent of worker spend overrides."""
    if pool is None:
        return None
    state, evidence = current_pool_state(_without_levels(runtime), pool, roster=roster)
    level = pool_level(runtime, pool)
    if level not in {"low", "off"} and state not in {"CONSERVE", "CRITICAL", "EXHAUSTED"}:
        return None
    windows = {name: window for name, window in (evidence.get("windows") or {}).items()
               if name in evidence.get("applicable_windows", evidence.get("windows") or {})}
    name, window = max(windows.items(), key=lambda item: item[1]["used_percent"],
                       default=(None, {}))
    return {"pool": pool, "level": level, "state": state,
            "used_percent": window.get("used_percent"),
            "window": (_compact_window_label(name, window) if name else None)}


def lead_directive(pressure: dict[str, Any] | None) -> str | None:
    if pressure is None:
        return None
    used = pressure["used_percent"]
    reading = (f"{used:g}% of {pressure['window']}" if used is not None else
               f"quota unknown; level {pressure['level']}")
    return (f"lead: you are spending {pressure['pool']} ({reading}): "
            "delegate every task external via dispatch, keep your own turns short, say so")


def _phrase_names_pool(phrase: str | None, pool: str, pools: dict[str, Any]) -> bool:
    """Only explicit pool identifiers or unambiguous provider names can lift off."""
    if not phrase:
        return False
    aliases = {"antigravity-gemini": ["antigravity gemini"], "gemini-metered": ["gemini api"],
               "chatgpt-work": ["chatgpt work"], "github-copilot-student": ["copilot"]}
    if pool in {"antigravity-gemini", "gemini-metered"} and not (
            {"antigravity-gemini", "gemini-metered"} - {pool}) & pools.keys():
        aliases.setdefault(pool, []).append("gemini")
    names = [pool, *aliases.get(pool, [])]
    return any(re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", phrase, re.I)
               for name in names)


def apply_profile(roster: dict[str, Any], runtime: dict[str, Any], name: str, *,
                  who: str, because: str | None = None) -> dict[str, Any]:
    if name not in PROFILES:
        raise FleetError(f"unknown profile {name}")
    pools = roster.get("quota_pools", {})
    if name in {"save-claude", "save-codex"}:
        saved = name.removeprefix("save-")
        wanted = {pool: ("low" if pool == saved else "high") for pool in
                  ("claude", "codex", "antigravity-gemini", "chatgpt-work") if pool in pools}
    else:
        wanted = {pool: ("high" if name == "max-quality" else "normal")
                  for pool, config in pools.items()
                  if name != "max-quality" or not (
                      config.get("daily_usd_cap") is not None or
                      config.get("billing") in {"per-token", "metered", "paid"} or
                      (config.get("plan") or {}).get("billing") in {"per-token", "metered", "paid"})}
    changes, preserved = [], []
    when = iso()
    for pool, level in sorted(wanted.items()):
        before = pool_level(runtime, pool)
        if before == "off" and not _phrase_names_pool(because, pool, pools):
            preserved.append(pool)
        elif before != level:
            set_pool_level(runtime, pool, level)
            change = {"pool": pool, "before": before, "after": level,
                      "who": who, "when": when, "phrase": because, "profile": name}
            runtime.setdefault("profile_changes", []).append(change)
            changes.append(change)
    return {"profile": name, "changes": changes, "preserved_off": preserved}


def profile_report(result: dict[str, Any]) -> str:
    changed = "; ".join(f"{item['pool']} {item['before']} -> {item['after']}"
                        for item in result["changes"]) or "no level changes"
    kept = "; kept off: " + ", ".join(result["preserved_off"]) if result["preserved_off"] else ""
    return f"profile {result['profile']}: {changed}{kept}"


def legacy_switch_word(level: str) -> str:
    """What the three-way `switch` command prints for a level: off, on or auto."""
    return _LEGACY_SWITCH_WORD.get(level, "auto")


def pool_has_no_known_limit(roster: dict[str, Any] | None, pool: str) -> bool:
    config = ((roster or {}).get("quota_pools") or {}).get(pool) or {}
    plan = config.get("plan") if isinstance(config, dict) else None
    return isinstance(plan, dict) and plan.get("limit") == "none-known"


def quota_window_applies(roster: dict[str, Any] | None, pool: str, name: str,
                         window: dict[str, Any], model_key: str | None = None) -> bool:
    """General windows bind every model; model-only limits bind their family.

    A pool summary excludes model-only limits. They remain in the reported
    readings and are included when routing or admitting the matching model.
    """
    roster = roster or {}
    def words(text: str) -> set[str]:
        return set(re.findall(r"[a-z][a-z0-9]*", text.lower()))
    noise = words(pool) | {
        "weekly", "week", "monthly", "month", "daily", "day", "session", "hour", "hours",
        "primary", "secondary", "tertiary", "rolling", "quota", "summary", "window", "limit",
        "only", "scoped", "and", "or", "model", "models", "h", "d", "m",
    }
    pool_terms = words(str(roster.get("quota_pools", {}).get(pool, {}).get("label") or pool)) - noise
    models = {
        *roster.get("model_cards", {}), *roster.get("effort", {}),
        *choosable_models(roster, pool),
    }
    model_words = set().union(*(words(key) for key in models)) - noise
    text = name + " " + str(window.get("label") or "")
    scope = words(text)
    terms = scope - noise
    scoped = bool(terms and terms != pool_terms and (
        scope & {"only", "scoped"} or terms & model_words))
    if not scoped:
        return True
    if model_key is None:
        return False
    alternatives = re.split(r"\b(?:and|or)\b", str(window.get("label") or ""), flags=re.I)
    if len(alternatives) == 1:
        alternatives = re.split(r"\b(?:and|or)\b", name.replace("_", " "), flags=re.I)
    if len(alternatives) > 1:
        return any(quota_window_applies(roster, pool, "scoped", {"label": part + " only"}, model_key)
                   for part in alternatives)
    # A complete model identity is more specific than a family label. Retain
    # version numbers and normalize separators so GPT-6.1-Sol and GPT 6.1 Sol
    # agree without matching Astra through "gpt", or Sonnet 5 through Sonnet 5.5.
    def tokens(value: str) -> list[str]:
        return re.findall(r"[a-z]+|\d+", value.lower())
    def contains(haystack: list[str], needle: list[str]) -> bool:
        return any(haystack[i:i + len(needle)] == needle
                   for i in range(len(haystack) - len(needle) + 1))
    scope_tokens = tokens(text)
    model_tokens = {key: tokens(key) for key in models}
    # Numeric versions are constraints even when the roster has never seen
    # them. Do not let an unknown GPT 6.2 Sol become the whole Sol family.
    identity_tokens = {part for parts in model_tokens.values() for part in parts if not part.isdigit()} - noise
    model_version = [part for part in tokens(model_key) if part.isdigit()]
    for source in (name, str(window.get("label") or "")):
        parts = tokens(source)
        for i, part in enumerate(parts):
            if part not in identity_tokens:
                continue
            version = []
            for value in parts[i + 1:]:
                if not value.isdigit():
                    break
                version.append(value)
            if version and version != model_version:
                return False
    exact = {key: parts for key, parts in model_tokens.items() if contains(scope_tokens, parts)}
    if not exact:
        # Sources also label a version without repeating the provider, e.g.
        # "Sonnet 5.5 only". Keep its numeric specificity before family matching.
        exact = {key: parts[1:] for key, parts in model_tokens.items()
                 if parts and parts[0] in noise and any(part.isdigit() for part in parts[1:])
                 and contains(scope_tokens, parts[1:])}
    if exact:
        most_specific = {key for key, parts in exact.items() if not any(
            len(other) > len(parts) and contains(other, parts) for other in exact.values())}
        return model_key in most_specific
    family_terms = terms & model_words or terms
    return family_terms <= (words(model_key) - noise)


def current_pool_state(
    runtime: dict[str, Any], pool: str, now: dt.datetime | None = None,
    *, roster: dict[str, Any] | None = None, model_key: str | None = None,
) -> tuple[str, dict[str, Any]]:
    now = now or utc_now()
    # The operator's hand setting beats every measurement: "off" saves a pool they want kept,
    # "forced" spends one the gauges say is low. Set with `fleetctl.py level <pool> <level>`
    # (or the older `switch <pool> off|on|auto`), from the console page, or by an agent on
    # the operator's word.
    level = pool_level(runtime, pool)
    if level == "off":
        return "EXHAUSTED", {"source": "switched-off", "level": "off",
                             "until": f"switched back on (fleetctl.py level {pool} normal)"}
    # An open circuit is the provider itself saying "quota exhausted" (a real 429 or limit
    # error), not a gauge estimate. Forced overrides gauges, never a known failure, so the
    # circuit is checked first: forcing past it would only send more calls into the refusal.
    circuit = runtime.get("pool_circuits", {}).get(pool, {})
    circuit_until = circuit.get("until")
    if circuit_until and parse_iso(circuit_until) > now:
        return "EXHAUSTED", {
            "source": "explicit-quota-error",
            "until": circuit_until,
            "limit_name": circuit.get("limit_name"),
        }
    if level == "forced":
        return "ABUNDANT", {"source": "forced-on", "level": "forced"}

    if pool_has_no_known_limit(roster, pool):
        return "UNKNOWN", {"source": "none-known", "confidence": "declared"}

    snapshot = runtime.get("quota_snapshots", {}).get(pool)
    if not snapshot:
        return "UNKNOWN", {"source": "none", "confidence": "unknown"}

    observed = parse_iso(snapshot["observed_at"])
    age_s = max(0, int((now - observed).total_seconds()))
    # Enriched copies, never the stored dicts: the snapshot in runtime.json is
    # observation and must not gain derived fields by aliasing.
    active_windows: dict[str, Any] = {}
    for name, window in snapshot.get("windows", {}).items():
        reset = parse_iso(window["reset_at"])
        if reset <= now:
            continue
        enriched = dict(window)
        enriched["seconds_to_reset"] = max(0, int((reset - now).total_seconds()))
        active_windows[name] = enriched
    applicable_windows = {name: window for name, window in active_windows.items()
                          if quota_window_applies(roster, pool, name, window, model_key)}
    confidence = "direct" if age_s <= 900 else "aging" if age_s <= 3600 else "stale"
    # A console-observed 100% window stays exhausted until its console-stated
    # reset passes, even when the snapshot itself has gone stale: staleness must
    # never fail open into a pool the console last reported as full.
    exhausted_windows = {
        name: window
        for name, window in applicable_windows.items()
        if int(window["used_percent"]) >= 100
    }
    if exhausted_windows:
        until = max(window["reset_at"] for window in exhausted_windows.values())
        return "EXHAUSTED", {
            "source": "opencode-console-dashboard",
            "confidence": confidence,
            "precision_percentage_points": 1,
            "observed_at": snapshot["observed_at"],
            "age_seconds": age_s,
            "bottleneck_used_percent": max(
                int(window["used_percent"]) for window in applicable_windows.values()
            ),
            "limit_names": sorted(exhausted_windows),
            "until": until,
            "windows": active_windows,
            "applicable_windows": sorted(applicable_windows),
            "spend_down": [],
        }
    if not applicable_windows or age_s > 3600:
        return "UNKNOWN", {
            "source": "opencode-console-dashboard",
            "confidence": "stale" if age_s > 3600 else confidence,
            "observed_at": snapshot["observed_at"],
            "age_seconds": age_s,
            # Fresh model-only readings still belong on the console even when
            # they cannot establish the pool's general quota state.
            "windows": active_windows if age_s <= 3600 else {},
            "applicable_windows": sorted(applicable_windows),
            "spend_down": [],
        }

    max_used = max(int(window["used_percent"]) for window in applicable_windows.values())
    # Every window here is already below 100%: the exhausted branch returned
    # above, and a spent window is not use-it-or-lose-it, it is simply gone.
    #
    # Two different questions, deliberately not conflated:
    #   non_binding -- this window will still hold unspent allowance at its reset,
    #                  so it is not a reason to throttle. This drives the GATE.
    #   spend_down  -- the same, AND the reset is imminent, so "spend it now" is
    #                  actionable advice. This drives the LABEL and the urgency.
    # A weekly pool at 5% with six days left is non-binding but nothing is about
    # to be lost; calling that "spend down" would make the flag meaningless.
    non_binding = {}
    expiring = {}
    for name, window in active_windows.items():
        verdict = window_surplus(window)
        window["surplus"] = verdict
        if name not in applicable_windows:
            continue
        if verdict["surplus"]:
            non_binding[name] = window
            if window["seconds_to_reset"] <= spend_down_horizon_s(window):
                expiring[name] = window
    binding_used = max(
        (
            int(window["used_percent"])
            for name, window in applicable_windows.items()
            if name not in non_binding
        ),
        default=0,
    )
    # Unused allowance in one window cannot be spent freely through a second
    # critical limit. Keep its forecast, but suppress actionable spare advice.
    if _state_for_used(binding_used) == "CRITICAL":
        expiring.clear()
    return _state_for_used(max_used), {
        "source": "opencode-console-dashboard",
        "confidence": confidence,
        "precision_percentage_points": 1,
        "observed_at": snapshot["observed_at"],
        "age_seconds": age_s,
        # Observed, and reported as such whatever the policy does with it.
        "bottleneck_used_percent": max_used,
        # Derived: what still constrains the pool once the surplus windows go.
        "binding_used_percent": binding_used,
        "routing_state": _state_for_used(binding_used),
        "non_binding": sorted(non_binding),
        "spend_down": sorted(expiring),
        "windows": active_windows,
        "applicable_windows": sorted(applicable_windows),
    }


def local_observed(db_path: Path, now_ms: int | None = None) -> dict[str, Any]:
    if not db_path.exists():
        return {"available": False, "reason": f"database missing: {db_path}"}
    now_ms = now_ms or int(time.time() * 1000)
    windows = {"rolling_5h": 5 * 3600, "weekly_7d": 7 * 86400, "monthly_30d": 30 * 86400}
    query = """
        SELECT
          COALESCE(SUM(json_extract(p.data, '$.cost')), 0),
          COALESCE(SUM(json_extract(p.data, '$.tokens.total')), 0),
          COALESCE(SUM(json_extract(p.data, '$.tokens.input')), 0),
          COALESCE(SUM(json_extract(p.data, '$.tokens.output')), 0),
          COALESCE(SUM(json_extract(p.data, '$.tokens.reasoning')), 0),
          COALESCE(SUM(json_extract(p.data, '$.tokens.cache.read')), 0),
          COALESCE(SUM(json_extract(p.data, '$.tokens.cache.write')), 0),
          COUNT(*)
        FROM part p
        JOIN message m ON m.id = p.message_id
        WHERE json_extract(p.data, '$.type') = 'step-finish'
          AND json_extract(m.data, '$.providerID') = 'opencode-go'
          AND p.time_created >= ?
    """
    result: dict[str, Any] = {
        "available": True,
        "source": "opencode-local-database",
        "cost_semantics": "estimated-equivalent-usd",
        "authoritative_for_go_quota": False,
        "windows": {},
    }
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for name, seconds in windows.items():
            row = connection.execute(query, (now_ms - seconds * 1000,)).fetchone()
            assert row is not None
            result["windows"][name] = {
                "estimated_cost_usd": float(row[0]),
                "tokens": {
                    "total": int(row[1]),
                    "input": int(row[2]),
                    "output": int(row[3]),
                    "reasoning": int(row[4]),
                    "cache_read": int(row[5]),
                    "cache_write": int(row[6]),
                },
                "completed_steps": int(row[7]),
            }
        connection.close()
    except sqlite3.Error as exc:
        return {"available": False, "reason": str(exc)}
    return result


CODEXBAR_BIN = os.environ.get("CODEXBAR_BIN", "codexbar")

# A CodexBar provider name can differ from the fleet's quota-pool key. opencodego
# is CodexBar's OpenCode Go lane; the routing engine + show_usage key that pool as
# "opencode-go". copilot is the Student-granted Copilot plan, which GitHub bills
# as an individual plan and CodexBar reports under the single account on this
# machine. Map here so a CodexBar snapshot feeds the right pool.
CODEXBAR_POOL_ALIASES = {"opencodego": "opencode-go", "copilot": "github-copilot-student"}

ROUTING_POOL = "opencode-go"

# Refresh knobs. TTL 600s keeps the snapshot inside current_pool_state's "direct"
# confidence band (900s) with margin, and far from its 3600s UNKNOWN cliff.
REFRESH_TTL_S = int(os.environ.get("FLEET_SNAPSHOT_TTL_S", "600"))
# The web-dashboard pools get a longer TTL: they are informational only (routing
# reads ROUTING_POOL), each costs a real fetch, and the claude.ai usage endpoint
# rate-limits when polled hard (CodexBar's own polling shares that budget).
# 1800s still refreshes well inside the 3600s UNKNOWN cliff.
REFRESH_NETWORK_TTL_S = int(os.environ.get("FLEET_SNAPSHOT_NETWORK_TTL_S", "1800"))
# A cooldown so an unreachable oracle (quit, offline, logged out) costs one
# failed attempt per cooldown window rather than one on every single call.
REFRESH_COOLDOWN_S = int(os.environ.get("FLEET_SNAPSHOT_COOLDOWN_S", "120"))
# Short by design: the routing pool answers in ~40ms and a dashboard pool in
# ~1.3s, so a 5s wait means the oracle is wedged and the caller is better served by
# proceeding on the stored snapshot than by blocking.
REFRESH_TIMEOUT_S = int(os.environ.get("FLEET_SNAPSHOT_TIMEOUT_S", "5"))


def _validate_snapshot(data: Any, source: str) -> dict[str, Any]:
    """Shared validator for non-codexbar oracle responses."""
    if not isinstance(data, dict):
        return {"available": False, "source": source, "reason": "snapshot data is not a JSON object"}
    if data.get("available") is False:
        res = {
            "available": False,
            "source": str(data.get("source", source)),
            "reason": str(data.get("reason", "marked unavailable")),
        }
        for k in ("provider", "command", "path", "url"):
            if k in data:
                res[k] = data[k]
        return res

    raw_windows = data.get("windows")
    if not isinstance(raw_windows, dict) or not raw_windows:
        return {"available": False, "source": source, "reason": "missing or invalid rate windows"}

    validated_windows: dict[str, dict[str, Any]] = {}
    for name, window in raw_windows.items():
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        reset_at = window.get("reset_at")
        if used is None or reset_at is None:
            continue
        try:
            used_i = int(round(float(used)))
        except (TypeError, ValueError):
            continue
        if not (0 <= used_i <= 100):
            continue
        try:
            reset_iso = iso(parse_iso(str(reset_at)))
        except (ValueError, AttributeError):
            continue
        entry = {"used_percent": used_i, "reset_at": reset_iso}
        # Optional, and worth passing through: `window_minutes` is what lets the
        # surplus test extrapolate a burn rate, and `will_last_to_reset` /
        # `eta_seconds` let an oracle publish its own projection. Without these a
        # custom oracle would be second-class next to the built-in one, which is
        # exactly backwards on a platform where custom is the only option.
        minutes = window.get("window_minutes")
        if isinstance(minutes, (int, float)) and minutes > 0:
            entry["window_minutes"] = int(minutes)
        if isinstance(window.get("will_last_to_reset"), bool):
            entry["will_last_to_reset"] = window["will_last_to_reset"]
        eta = window.get("eta_seconds")
        if isinstance(eta, (int, float)):
            entry["eta_seconds"] = int(eta)
        label = _window_label_text(window.get("label"))
        if label:
            entry["label"] = label
        validated_windows[str(name)] = entry

    if not validated_windows:
        return {"available": False, "source": source, "reason": "no valid rate windows"}

    observed_raw = data.get("observed_at")
    try:
        observed_iso = iso(parse_iso(str(observed_raw))) if observed_raw else iso()
    except (ValueError, AttributeError):
        observed_iso = iso()

    res_out: dict[str, Any] = {
        "available": True,
        "source": str(data.get("source", source)),
        "observed_at": observed_iso,
        "precision_percentage_points": int(data.get("precision_percentage_points", 1)),
        "windows": validated_windows,
    }
    for k in ("provider", "command", "path", "url"):
        if k in data:
            res_out[k] = data[k]
    return res_out


def _window_label_text(raw: Any) -> str | None:
    """What the quota source calls a limit ("Weekly", "Fable only"), kept only when it is a short plain label."""
    if not isinstance(raw, str):
        return None
    label = " ".join(raw.split())
    return label if label and "@" not in label and len(label) <= 40 else None


def oracle_codexbar(config: dict[str, Any], timeout: int = REFRESH_TIMEOUT_S) -> dict[str, Any]:
    provider = config.get("provider")
    if not provider:
        return {"available": False, "source": "codexbar", "reason": "missing provider in config"}
    return codexbar_observed(str(provider), timeout=timeout)


def oracle_command(config: dict[str, Any], timeout: int = REFRESH_TIMEOUT_S) -> dict[str, Any]:
    cmd = config.get("command")
    if not isinstance(cmd, list) or not cmd:
        return {"available": False, "source": "command", "reason": "missing or invalid command in config"}
    try:
        proc = subprocess.run(
            [str(arg) for arg in cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"available": False, "source": "command", "reason": f"command not found: {cmd[0]}"}
    except subprocess.TimeoutExpired:
        return {"available": False, "source": "command", "reason": f"command timed out after {timeout}s"}
    except Exception as exc:
        return {"available": False, "source": "command", "reason": f"command execution failed: {exc}"}

    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or "").strip()[:200]
        return {"available": False, "source": "command", "reason": f"command exited with code {proc.returncode}: {err_msg}"}

    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        return {"available": False, "source": "command", "reason": f"unparseable output: {exc}"}

    return _validate_snapshot(data, source="command")


def oracle_file(config: dict[str, Any], timeout: int = REFRESH_TIMEOUT_S) -> dict[str, Any]:
    path_str = config.get("path")
    if not path_str or not isinstance(path_str, str):
        return {"available": False, "source": "file", "reason": "missing or invalid path in config"}
    file_path = Path(os.path.expanduser(path_str))
    try:
        content = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"available": False, "source": "file", "reason": f"file not found: {file_path}"}
    except Exception as exc:
        return {"available": False, "source": "file", "reason": f"failed to read file: {exc}"}

    try:
        data = json.loads(content)
    except Exception as exc:
        return {"available": False, "source": "file", "reason": f"unparseable output: {exc}"}

    return _validate_snapshot(data, source="file")


def oracle_http(config: dict[str, Any], timeout: int = REFRESH_TIMEOUT_S) -> dict[str, Any]:
    url = config.get("url")
    if not url or not isinstance(url, str):
        return {"available": False, "source": "http", "reason": "missing or invalid url in config"}
    # Scheme allowlist: urlopen also speaks file:// and ftp://, so an unrestricted url
    # would turn this adapter into an arbitrary local-file reader.
    scheme = url.split(":", 1)[0].lower()
    if scheme not in {"http", "https"}:
        return {"available": False, "source": "http",
                "reason": f"unsupported url scheme: {scheme} (use http or https)"}
    headers: dict[str, str] = {}
    token_env = config.get("token_env")
    if token_env and isinstance(token_env, str):
        token = os.environ.get(token_env, "").strip()
        if token:
            # A bearer token over plain http would cross the wire in clear text, and
            # urllib replays headers on redirect, so refuse rather than leak it.
            if scheme != "https":
                return {"available": False, "source": "http",
                        "reason": "token_env is set but the url is not https"}
            headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except Exception as exc:
        return {"available": False, "source": "http", "reason": f"HTTP GET failed: {exc}"}

    try:
        data = json.loads(body)
    except Exception as exc:
        return {"available": False, "source": "http", "reason": f"unparseable output: {exc}"}

    return _validate_snapshot(data, source="http")


ORACLE_REGISTRY: dict[str, Callable[[dict[str, Any], int], dict[str, Any]]] = {
    "codexbar": oracle_codexbar,
    "command": oracle_command,
    "file": oracle_file,
    "http": oracle_http,
}


def _source_identity(config: dict[str, Any]) -> Any:
    oracle = config.get("oracle", "codexbar")
    if oracle == "codexbar":
        provider = config.get("provider")
        return ("codexbar", str(provider) if provider is not None else None)
    if oracle == "command":
        cmd = config.get("command")
        return ("command", tuple(cmd) if isinstance(cmd, list) else cmd)
    if oracle == "file":
        p = config.get("path")
        return ("file", os.path.expanduser(p) if isinstance(p, str) else p)
    if oracle == "http":
        return ("http", config.get("url"))
    return (oracle, id(config))


def quota_sources(roster: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Which pools have a fetchable quota percentage, from where, and how often.

    Declared per pool in the overlay as `quota_pools.<pool>.quota_refresh`.
    Returns only what the overlay declares, and `{}` for an absent or unreadable overlay.
    """
    sources: dict[str, dict[str, Any]] = {}
    for pool, config in ((roster or {}).get("quota_pools") or {}).items():
        if not isinstance(config, dict) or "quota_refresh" not in config:
            continue
        if pool_has_no_known_limit(roster, pool):
            continue
        declared = config["quota_refresh"]
        if not declared or not isinstance(declared, dict):
            continue
        oracle = declared.get("oracle")
        if not oracle or not isinstance(oracle, str) or oracle not in ORACLE_REGISTRY:
            continue
        try:
            ttl = int(declared.get("ttl_s", REFRESH_NETWORK_TTL_S))
        except (TypeError, ValueError):
            ttl = REFRESH_NETWORK_TTL_S
        entry = dict(declared)
        entry["oracle"] = oracle
        entry["ttl_s"] = ttl
        if "windows" in declared and isinstance(declared["windows"], list):
            entry["windows"] = list(declared["windows"])
        sources[pool] = entry
    return sources


def _parse_codexbar(stdout: str) -> dict[str, Any] | None:
    """CodexBar prints a JSON array (one provider object) to stdout, sometimes
    preceded by non-JSON notice lines (e.g. "[codex notify] ..."). Try the whole
    output first, then each line, keeping the first parseable non-empty list."""
    candidates = [stdout.strip(), *(line.strip() for line in stdout.splitlines())]
    for candidate in candidates:
        if not candidate.startswith("["):
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
    return None


def codexbar_observed(
    provider: str, binary: str | None = None, timeout: int = 25
) -> dict[str, Any]:
    """Shell `codexbar usage --provider <p> --format json` and normalise its
    output into the same snapshot shape `current_pool_state` consumes:
    {source, observed_at, windows:{name:{used_percent, reset_at}}}.

    CodexBar is the authoritative oracle (it reads Claude's Keychain-OAuth token
    itself; the fleet never touches the credential). Providers CodexBar cannot
    currently serve (not logged in / no fetch strategy) return available=False
    with CodexBar's own reason, so the caller leaves them UNKNOWN rather than
    guessing."""
    binary = binary or CODEXBAR_BIN
    try:
        proc = subprocess.run(
            [binary, "usage", "--provider", provider, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"available": False, "provider": provider,
                "reason": f"codexbar binary not found: {binary}"}
    except subprocess.TimeoutExpired:
        return {"available": False, "provider": provider,
                "reason": f"codexbar timed out after {timeout}s"}

    obj = _parse_codexbar(proc.stdout)
    if obj is None:
        detail = (proc.stderr or proc.stdout or "no output").strip()[:200]
        return {"available": False, "provider": provider,
                "reason": f"codexbar returned no usage JSON: {detail}"}
    if obj.get("error"):
        err = obj["error"]
        reason = err.get("message") if isinstance(err, dict) else str(err)
        return {"available": False, "provider": provider, "reason": str(reason)}

    usage = obj.get("usage") or {}
    pace = obj.get("pace") or {}
    labels = obj.get("rateWindowLabels") if isinstance(obj.get("rateWindowLabels"), dict) else {}
    windows: dict[str, Any] = {}
    # Limits the source reports with a percentage but no reset clock: a 5-hour window nothing has
    # been spent in yet has not started, so it has no reset. Shown, never routed on.
    idle: dict[str, Any] = {}

    def add(name: str, used: Any, reset_at: Any, window_minutes: Any = None, label: Any = None) -> None:
        if used is None:
            return
        try:
            used_i = int(round(float(used)))
        except (TypeError, ValueError):
            return
        try:
            minutes = int(window_minutes)
        except (TypeError, ValueError):
            minutes = 0
        label = _window_label_text(label)
        if reset_at is None:
            if 0 <= used_i <= 100:
                idle[name] = {"used_percent": used_i}
                if minutes > 0:
                    idle[name]["window_minutes"] = minutes
                if label:
                    idle[name]["label"] = label
            return
        try:
            reset_iso = iso(parse_iso(str(reset_at)))
        except (ValueError, AttributeError):
            return
        entry: dict[str, Any] = {"used_percent": used_i, "reset_at": reset_iso}
        # Window length is what makes the spend-down horizon proportional: the
        # last tenth of a 5h window is not the last tenth of a weekly one.
        if minutes > 0:
            entry["window_minutes"] = minutes
        # What the source calls this limit, so a person reads "Fable only" and not a slot name.
        if label:
            entry["label"] = label
        # The oracle's own burn projection, when it publishes one. This is the
        # only rate signal weighted toward RECENT usage rather than the whole
        # window's average, so the surplus test prefers it. It appears per slot
        # and only sometimes -- a Claude reading can carry none while a Codex one does.
        slot_pace = pace.get(name) if isinstance(pace, dict) else None
        if isinstance(slot_pace, dict):
            if isinstance(slot_pace.get("willLastToReset"), bool):
                entry["will_last_to_reset"] = slot_pace["willLastToReset"]
            eta = slot_pace.get("etaSeconds")
            if isinstance(eta, (int, float)):
                entry["eta_seconds"] = int(eta)
        windows[name] = entry

    for slot in ("primary", "secondary", "tertiary"):
        window = usage.get(slot)
        if isinstance(window, dict):
            add(
                slot,
                window.get("usedPercent"),
                window.get("resetsAt"),
                window.get("windowMinutes"),
                labels.get(slot),
            )
    for extra in usage.get("extraRateWindows") or []:
        if not isinstance(extra, dict):
            continue
        window = extra.get("window") or {}
        name = str(extra.get("id") or f"extra_{len(windows)}")
        add(
            name,
            window.get("usedPercent"),
            window.get("resetsAt"),
            window.get("windowMinutes"),
            extra.get("title"),
        )

    if not windows:
        return {"available": False, "provider": provider,
                "reason": "codexbar usage carried no rate windows"}

    observed_raw = usage.get("updatedAt") or obj.get("updatedAt")
    try:
        observed_iso = iso(parse_iso(str(observed_raw))) if observed_raw else iso()
    except (ValueError, AttributeError):
        observed_iso = iso()

    result = {
        "available": True,
        "provider": provider,
        "source": "codexbar",
        "observed_at": observed_iso,
        "precision_percentage_points": 1,
        "windows": windows,
    }
    if idle:
        result["idle_windows"] = idle
    plan = codexbar_plan_label(obj)
    if plan:
        result["plan"] = plan
    return result


def codexbar_plan_label(obj: dict[str, Any]) -> str | None:
    """The plan name CodexBar already knows ("Claude Max 20x", "Pro 20x"), or None.

    Only the label is kept. The same payload carries the account's email address,
    which never belongs in runtime state, so anything that looks like one is refused
    rather than trimmed.
    """
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    identity = usage.get("identity") if isinstance(usage.get("identity"), dict) else {}
    dashboard = obj.get("openaiDashboard") if isinstance(obj.get("openaiDashboard"), dict) else {}
    for raw in (dashboard.get("accountPlan"), usage.get("loginMethod"), identity.get("loginMethod")):
        if not isinstance(raw, str):
            continue
        label = " ".join(raw.split())
        if not label or "@" in label or len(label) > 40:
            continue
        return label
    return None


def pool_refresh_ttl(
    pool: str, override: int | None = None, sources: dict[str, dict[str, Any]] | None = None
) -> int:
    """Refresh interval for a pool, from its declared quota source: short for the
    local routing read, long for the metered dashboard reads."""
    if override is not None:
        return override
    registry = sources if sources is not None else {}
    declared = registry.get(pool)
    if declared and declared.get("ttl_s") is not None:
        return int(declared["ttl_s"])
    return REFRESH_NETWORK_TTL_S


def auto_refresh_disabled() -> bool:
    value = os.environ.get("FLEET_NO_AUTO_REFRESH", "").strip().lower()
    return value not in {"", "0", "false", "no"}


def snapshot_needs_refresh(
    runtime: dict[str, Any],
    pool: str,
    ttl_s: int | None = None,
    cooldown_s: int = REFRESH_COOLDOWN_S,
    now: dt.datetime | None = None,
    sources: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """True when this pool's snapshot is older than the TTL (or missing) and the
    last refresh attempt is outside the cooldown."""
    now = now or utc_now()
    ttl = pool_refresh_ttl(pool, ttl_s, sources)
    snapshot = (runtime.get("quota_snapshots") or {}).get(pool)
    if snapshot:
        try:
            if (now - parse_iso(snapshot["observed_at"])).total_seconds() <= ttl:
                return False
        except (KeyError, TypeError, ValueError):
            pass  # unreadable stamp counts as stale
    last_attempt = (runtime.get("quota_refresh_attempts") or {}).get(pool)
    if last_attempt:
        try:
            if (now - parse_iso(last_attempt)).total_seconds() < cooldown_s:
                return False
        except (TypeError, ValueError):
            pass
    return True


def refresh_stale_pools(
    state_dir: Path,
    pools: Iterable[str],
    ttl_s: int | None = None,
    cooldown_s: int = REFRESH_COOLDOWN_S,
    timeout: int = REFRESH_TIMEOUT_S,
    roster: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, str]:
    """Re-read any of `pools` whose snapshot has aged past its TTL from their oracle.

    An explicit console refresh uses force to bypass TTL, cooldown and auto-disable.
    Failed observations still retain the last reading; pool circuits stay intact.

    Staleness is not neutral: current_pool_state degrades a snapshot older than
    an hour to UNKNOWN, and effective_cap then clamps every frontier lane to one
    slot. Before this, only a manual `codexbar-snapshot` cleared that, so an
    unattended fleet silently halved its own concurrency. Refreshing at the point
    of read makes staleness structurally impossible for every caller (Claude,
    Codex, the wrapper scripts, an afk loop) instead of relying on an external
    trigger that has to remember to fire.

    Never raises and never blocks a run: an oracle being unreachable leaves the
    stored snapshot exactly as it was, which is the honest fallback. The fetch
    happens OUTSIDE the runtime lock, so a slow oracle cannot stall a
    concurrent lease acquisition, and a snapshot is only replaced by a strictly
    newer observation, so a parallel writer or a manual override is never
    clobbered. Pool circuits are deliberately left alone: an explicit quota error
    stays authoritative until a human runs codexbar-snapshot, otherwise a 429
    would be reopened by the next route and immediately hit again.
    """
    if auto_refresh_disabled() and not force:
        return {}
    sources = quota_sources(roster)
    wanted = [pool for pool in pools if pool in sources]
    if not wanted:
        return {}
    try:
        runtime = load_json(state_dir / "runtime.json", {}) or {}
        stale = [
            pool
            for pool in wanted
            if force or snapshot_needs_refresh(
                runtime, pool, ttl_s=ttl_s, cooldown_s=cooldown_s, sources=sources
            )
        ]
        if not stale:
            return {}
        # Outside the lock on purpose: this is the part that can take seconds.
        source_observations: dict[Any, dict[str, Any]] = {}
        for pool in stale:
            cfg = sources[pool]
            ident = _source_identity(cfg)
            if ident not in source_observations:
                oracle_name = cfg.get("oracle", "codexbar")
                adapter = ORACLE_REGISTRY.get(oracle_name)
                if adapter:
                    source_observations[ident] = adapter(cfg, timeout=timeout)
                else:
                    source_observations[ident] = {
                        "available": False,
                        "reason": f"unknown oracle: {oracle_name}",
                    }

        fetched: dict[str, dict[str, Any]] = {}
        for pool in stale:
            cfg = sources[pool]
            ident = _source_identity(cfg)
            obs = copy.deepcopy(source_observations[ident])
            if obs.get("available") and "windows" in cfg:
                allowed = set(cfg["windows"])
                filtered = {k: v for k, v in obs.get("windows", {}).items() if k in allowed}
                if "idle_windows" in obs:
                    obs["idle_windows"] = {k: v for k, v in obs["idle_windows"].items() if k in allowed}
                if filtered:
                    obs["windows"] = filtered
                else:
                    obs["available"] = False
                    obs["reason"] = "no matching rate windows"
            fetched[pool] = obs
        outcomes: dict[str, str] = {}
        attempted_at = iso()
        with locked_runtime(state_dir) as locked:
            snapshots = locked.setdefault("quota_snapshots", {})
            attempts = locked.setdefault("quota_refresh_attempts", {})
            for pool, observed in fetched.items():
                attempts[pool] = attempted_at  # stamped on failure too, for the cooldown
                if not observed.get("available"):
                    outcomes[pool] = f"unavailable: {observed.get('reason')}"
                    continue
                existing = snapshots.get(pool)
                if existing:
                    try:
                        if parse_iso(existing["observed_at"]) >= parse_iso(observed["observed_at"]):
                            outcomes[pool] = "kept newer stored snapshot"
                            continue
                    except (KeyError, TypeError, ValueError):
                        pass
                snapshots[pool] = observed
                outcomes[pool] = "refreshed"
        return outcomes
    except Exception:  # noqa: BLE001 - freshness is best-effort, never fatal
        return {}



QUOTA_POLICIES = ("clock_aware", "strict", "off")
DEFAULT_QUOTA_POLICY = "clock_aware"
_POLICY_CACHE: tuple[str, str] | None = None


def _declared_policy(roster: dict[str, Any] | None) -> str | None:
    value = ((roster or {}).get("routing") or {}).get("quota_policy")
    if isinstance(value, str) and value.strip().lower() in QUOTA_POLICIES:
        return value.strip().lower()
    return None


def resolve_quota_policy(
    roster: dict[str, Any] | None = None, *, refresh: bool = False
) -> tuple[str, str]:
    """The active quota policy and where it came from, as (policy, source).

    Precedence is env > overlay > built-in default, so one run can override the
    machine's declared policy without editing config.

      clock_aware  bands gate routing, but a window inside its spend-down
                   horizon stops counting as scarce -- the default
      strict       bands gate on the raw bottleneck; the clock is ignored
      off          no band gating at all; every pool routes quality_first

    Measurement sits outside this switch in all three modes: `usage` reports the
    real observed percentages whatever the policy says. This governs the GATE
    only. A policy that also hid the numbers would leave nothing to decide with.
    """
    global _POLICY_CACHE
    env = os.environ.get("FLEET_QUOTA_POLICY", "").strip().lower()
    if env in QUOTA_POLICIES:
        return env, "env:FLEET_QUOTA_POLICY"
    # Honoured by name because it shipped first as the original escape hatch and
    # is still set in live configs. It means exactly `off`.
    if os.environ.get("FLEET_IGNORE_QUOTA", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "off", "env:FLEET_IGNORE_QUOTA"
    if roster is not None:
        declared = _declared_policy(roster)
        return (declared, "overlay:routing.quota_policy") if declared else (
            DEFAULT_QUOTA_POLICY,
            "default",
        )
    # The gate is reached from call sites that never carry the roster, so the
    # overlay is read once and cached rather than on every band computation.
    if _POLICY_CACHE is None or refresh:
        resolved = (DEFAULT_QUOTA_POLICY, "default")
        try:
            declared = _declared_policy(load_json(DEFAULT_OVERLAY, {}) or {})
            if declared:
                resolved = (declared, "overlay:routing.quota_policy")
        except (OSError, json.JSONDecodeError):
            pass
        _POLICY_CACHE = resolved
    return _POLICY_CACHE


def quota_gating_disabled() -> bool:
    """True when the active policy applies no band gating at all."""
    return resolve_quota_policy()[0] == "off"


def gating_state(
    pool_state: str, evidence: dict[str, Any] | None, policy: str | None = None
) -> str:
    """The state the GATE should read, which is not always the observed state.

    Under `clock_aware`, a pool whose worst window is about to reset is gated on
    the window that survives the reset instead, so expiring allowance gets spent
    rather than protected. EXHAUSTED and UNKNOWN never soften: one is spent, the
    other has no clock to read.
    """
    policy = policy or resolve_quota_policy()[0]
    if policy != "clock_aware" or not evidence:
        return pool_state
    if pool_state in {"EXHAUSTED", "UNKNOWN"}:
        return pool_state
    routing_state = evidence.get("routing_state")
    # `non_binding`, not `spend_down`: the gate turns on whether allowance will
    # survive the reset unspent, not on whether the reset happens to be close.
    if evidence.get("non_binding") and isinstance(routing_state, str):
        return routing_state
    return pool_state


def task_band(pool_state: str, evidence: dict[str, Any] | None = None) -> str:
    policy, _ = resolve_quota_policy()
    if policy == "off":
        return "quality_first"
    state = gating_state(pool_state, evidence, policy)
    if state in {"UNKNOWN", "ABUNDANT", "HEALTHY"}:
        return "quality_first"
    if state == "CONSERVE":
        return "conserve"
    return "critical"


def effective_cap(
    lane: dict[str, Any],
    pool_state: str,
    evidence: dict[str, Any] | None = None,
    level: str = "normal",
) -> int:
    """Concurrency slots this lane may hold right now.

    Single source of truth shared by routing and acquisition, so routing never
    hands back a lane that acquire_lease would immediately refuse.  Frontier
    lanes stay single-slot while the pool state is unproven or tight.

    `level` is the pool's spend level: `low` holds every lane of the pool to one
    slot whatever the gauges say; `high` and `forced` keep the declared
    `max_parallel` through CONSERVE (high still clamps a frontier lane at
    CRITICAL, which is the "within quota" half of the promise). The declared
    `max_parallel` is never exceeded: it encodes what the provider tolerates.
    """
    cap = int(lane.get("max_parallel", 1))
    if level == "low":
        return min(cap, 1)
    policy, _ = resolve_quota_policy()
    if policy == "off":
        return cap
    state = gating_state(pool_state, evidence, policy)
    if level in {"high", "forced"}:
        if state == "CRITICAL" and lane.get("quality_tier") == "frontier":
            cap = min(cap, 1)
        return cap
    # UNKNOWN FAILS OPEN. Absent measurement is not evidence of exhaustion, and
    # this used to clamp here -- which meant anyone without the quota oracle
    # installed got every frontier lane throttled to one slot, permanently and
    # silently. That is a worse failure than the one the clamp guards against:
    # an over-spent pool recovers at its reset, a router nobody can un-throttle
    # does not. EXHAUSTED still fails closed; that is a measurement, not a gap.
    if state in {"CONSERVE", "CRITICAL"} and lane.get("quality_tier") == "frontier":
        cap = min(cap, 1)
    return cap


def lane_free_slots(
    runtime: dict[str, Any],
    lane: dict[str, Any],
    pool_state: str,
    evidence: dict[str, Any] | None = None,
) -> int:
    """Free slots on a lane, counting only leases that are live and unexpired."""
    now = utc_now()
    active = [
        lease
        for lease in runtime.get("leases", [])
        if lease.get("lane_id") == lane.get("lane_id")
        and parse_iso(lease["expires_at"]) > now
        and recorded_pid_is_alive(lease)
    ]
    level = pool_level(runtime, lane.get("quota_pool"))
    if level == "low" and live_pool_leases(runtime, lane.get("quota_pool"), now):
        # Low means one call at a time on the whole pool, not one per lane.
        return 0
    return effective_cap(lane, pool_state, evidence, level) - len(active)


def live_pool_leases(
    runtime: dict[str, Any], pool: str | None, now: dt.datetime | None = None
) -> list[dict[str, Any]]:
    now = now or utc_now()
    return [
        lease
        for lease in runtime.get("leases", [])
        if lease.get("pool") == pool
        and parse_iso(lease["expires_at"]) > now
        and recorded_pid_is_alive(lease)
    ]


# How much of a subscription one call to a lane consumes, relative to its siblings.
# An overlay may state it per lane as `cost_rank` (lower is cheaper) when the provider
# prices models differently; otherwise the quality tier is the proxy, because inside one
# plan the bigger model is the one that eats the allowance faster.
TIER_COST_RANK = {"standard": 1, "observer": 1, "strong": 2, "metered": 2, "frontier": 3}


def lane_cost_rank(lane: dict[str, Any]) -> float:
    declared = lane.get("cost_rank")
    if isinstance(declared, (int, float)) and not isinstance(declared, bool):
        return float(declared)
    return float(TIER_COST_RANK.get(str(lane.get("quality_tier")), 2))


def level_band(level: str, band: str) -> str:
    """The routing band once the routing pool's own spend level is applied.

    low    at least `conserve`: step down to the cheaper lists even with quota to spare
    high   never step down for CONSERVE, only for CRITICAL
    forced / off / normal: unchanged (forced already reads ABUNDANT, off EXHAUSTED)
    """
    if level == "low" and band == "quality_first":
        return "conserve"
    if level == "high" and band == "conserve":
        return "quality_first"
    return band


def order_for_levels(
    candidates: list[str],
    lanes: dict[str, dict[str, Any]],
    runtime: dict[str, Any],
    one_shot: bool = False,
) -> list[str]:
    """Reorder a role's candidates so every `low` pool offers its cheapest lane first.

    The lanes of a low pool keep the SLOTS they held in the role's list, and only
    their order inside those slots changes, cheapest first (ties keep the roster's
    quality order). Other pools are untouched, so a low pool never jumps ahead of
    a lane it was ranked behind. A `one_shot` call keeps the quality order: that is
    the one sanctioned way to reach a big model on a low pool, and the one-slot cap
    in effective_cap keeps it a single call.
    """
    if one_shot:
        return list(candidates)
    result = list(candidates)
    positions: dict[str, list[int]] = {}
    for index, lane_id in enumerate(candidates):
        lane = lanes.get(lane_id)
        if not lane:
            continue
        pool = lane.get("quota_pool")
        if pool_level(runtime, pool) == "low":
            positions.setdefault(pool, []).append(index)
    for slots in positions.values():
        ranked = sorted(slots, key=lambda i: (lane_cost_rank(lanes[candidates[i]]), i))
        for slot, source in zip(slots, ranked):
            result[slot] = candidates[source]
    return result


MODEL_PREFERENCES = ("normal", "off")


def model_preference(runtime: dict[str, Any], model: str) -> str:
    value = (runtime.get("model_preferences") or {}).get(model, "normal")
    return value if value in MODEL_PREFERENCES else "normal"


def set_model_preference(runtime: dict[str, Any], model: str, preference: str) -> None:
    if preference not in MODEL_PREFERENCES:
        raise ValueError("unknown model preference")
    settings = runtime.setdefault("model_preferences", {})
    if preference == "normal":
        settings.pop(model, None)
    else:
        settings[model] = preference


# ---- the models a provider may run ---------------------------------------------------------
# Every model a provider can run has an on/off switch, one set per provider: the same model funded
# by two providers (a subscription and an API key) is switched on one and off the other.
# runtime["model_toggles"] = {pool: [models that are off]}; nothing off is the default.
# What the switches mean is the same everywhere (router, direct wrappers, console, brief):
#   several on : Crossfeed picks the best one that is on for each task
#   one on     : every run on that provider uses it
#   none on    : the provider is not used
# With nothing off, nothing changes: the router's per-role lists pick inside the pool and a direct
# dispatch runs the model its task asks for. With something off, the router skips the models that
# are off, and when a job's own favourites on a provider are all off the models that are on stand in
# at the rank the job gave that provider. A direct wrapper (codex-agent.sh, claude-agent.sh) runs
# the model its task asks for when that one is on, else the nearest model that is on, cheaper side
# first, and refuses when none is on.
#
# Two older settings still read, and are settled into the switches the first time a switch on that
# provider is touched. runtime["model_preferences"] = {model: "off"} is the per-model Off of an older
# console, which excluded a model on every provider. runtime["model_choices"] = {pool: model} is what
# an earlier single-choice picker wrote: it means "only that model on".
#
# Direct pools have no routing lanes; a wrapper passes the model straight to the vendor's own
# CLI. The pool ids are the ones those wrappers already gate on (`switch codex`, `pool-slot
# claude`), so naming them here adds no account detail the scripts did not already carry.
DIRECT_POOLS = {"codex": "codex-agent.sh", "claude": "claude-agent.sh"}
# claude-agent.sh runs this alias when a task names no model (its MODEL default); a test holds
# the two equal. Codex's own default lives in the Codex config (codex_default_model).
CLAUDE_WRAPPER_DEFAULT = "opus"
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
CURRENT_CARD_STATUSES = ("current",)


def model_cards(roster: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Plain-language cards the roster may carry per model: name, best_for, speed, run_as, pool,
    status (current|older), order, allowance, hidden, and the researched facts: page_url (a page
    about the model), superseded_by / superseded_on, better_than_successor_at (what it still does
    better than the model that replaced it, each with both numbers and a source) and line (the
    family a version belongs to, when its name does not say). Optional; the page derives what it
    can without them. Keys starting with "_" are notes, never models."""
    cards = roster.get("model_cards") or {}
    return {key: value for key, value in cards.items()
            if isinstance(value, dict) and not key.startswith("_")}


def codex_default_model() -> str | None:
    """The model the Codex CLI runs when a dispatch names none: `model` in its config.toml."""
    home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    try:
        text = (home / "config.toml").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        value = _toml_top_level_string(text, "model")
    else:
        try:
            value = tomllib.loads(text).get("model")
        except ValueError:
            return None
    return value if isinstance(value, str) and MODEL_ID_RE.match(value) else None


def _toml_top_level_string(text: str, key: str) -> str | None:
    """A top-level `key = "value"` from TOML, for Pythons without tomllib (3.9, 3.10).
    Top-level keys come before the first [table] header, so reading stops there."""
    pattern = re.compile(r"""^%s\s*=\s*(?:"([^"\\]*)"|'([^']*)')\s*(?:#.*)?$""" % re.escape(key))
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            break
        match = pattern.match(line)
        if match:
            return match.group(1) if match.group(1) is not None else match.group(2)
    return None


def direct_default_model(pool: str) -> str | None:
    if pool == "codex":
        return codex_default_model()
    if pool == "claude":
        return CLAUDE_WRAPPER_DEFAULT
    return None


def _lane_is_admitted(lane: dict[str, Any]) -> bool:
    return (bool(lane.get("lane_id")) and lane.get("admission_status") == "active"
            and lane.get("access_status") == "verified" and bool(lane.get("allowed_modes")))


def pool_has_lanes(roster: dict[str, Any], pool: str) -> bool:
    return any(lane.get("quota_pool") == pool for lane in roster.get("lanes", []))


def pool_is_direct(roster: dict[str, Any], pool: str) -> bool:
    return pool in DIRECT_POOLS and not pool_has_lanes(roster, pool)


def _evidence_is_older(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").casefold()
    return any(word in status for word in ("retired", "fallback", "superseded", "rejected", "gone"))


# ---- which models are older --------------------------------------------------------------------
# One rule for every provider, so a superseded model moves to the older list by itself: a model is
# older when the roster names what replaced it, or when the provider can run a newer version of
# the same line (Gemini 3.8 Flash makes 3.7 and 3.6 Flash older the day it gets a lane; Gemini 3.1
# Pro is another line and stays). Only then does a card's own status, or the evidence status, decide.
# Without this rule a routed provider had no older models at all: every admitted lane counted as
# current, so two superseded Flash versions kept as fallbacks were listed beside their successor.
_VERSION_TOKEN = re.compile(r"^([a-z]*?)(\d+(?:\.\d+)*)$")


def model_line(key: str, card: dict[str, Any] | None = None) -> tuple[str, tuple[int, ...]]:
    """("gemini-flash", (3, 8)) for gemini-3.8-flash: the line a model belongs to, and its version.

    The version is the first number in the name (v4, k2.7 and qwen3.7 carry a prefix that stays
    with the line; 5-1 and 4-6 are read as 5.1 and 4.6). A name without a number, such as an alias
    that always tracks the newest model, has no version and is never older by this rule. A card
    may state `line` when the name does not say which family a model belongs to.
    """
    words: list[str] = []
    version: tuple[int, ...] = ()
    tokens = key.casefold().split("-")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        match = None if version else _VERSION_TOKEN.match(token)
        if match:
            numbers = [int(part) for part in match.group(2).split(".")]
            while index + 1 < len(tokens) and tokens[index + 1].isdigit():
                index += 1
                numbers.append(int(tokens[index]))
            version = tuple(numbers)
            if match.group(1):
                words.append(match.group(1))
        else:
            words.append(token)
        index += 1
    line = (card or {}).get("line")
    return (str(line).casefold() if isinstance(line, str) and line else "-".join(words)), version


def superseded_by(roster: dict[str, Any], pool: str, key: str) -> str | None:
    """The model that replaced this one on this provider, or None while it is the newest of its line."""
    cards = model_cards(roster)
    card = cards.get(key, {})
    named = card.get("superseded_by")
    if isinstance(named, str) and named:
        return named
    line, version = model_line(key, card)
    if not version:
        return None
    newest: tuple[tuple[int, ...], str] | None = None
    for other in choosable_models(roster, pool):
        other_line, other_version = model_line(other, cards.get(other, {}))
        if other != key and other_line == line and other_version > version and (newest is None or other_version > newest[0]):
            newest = (other_version, other)
    return newest[1] if newest else None


def model_is_older(roster: dict[str, Any], pool: str, key: str) -> bool:
    if superseded_by(roster, pool, key) or retired_on(roster, key):
        return True
    card = model_cards(roster).get(key, {})
    if card.get("status"):
        return card["status"] not in CURRENT_CARD_STATUSES
    return _evidence_is_older((roster.get("model_evidence") or {}).get(key) or {})


def retired_on(roster: dict[str, Any], key: str) -> str | None:
    """The day a model's vendor withdrew it (a card's `retired_on`), once that day has come.

    A retired model is like one that is switched off and cannot be switched on: it has no switch,
    a run that asks for it gets a stand-in, a lane of it is never routed or leased, and a managed
    pin that names it is rewritten.
    """
    value = model_cards(roster).get(key, {}).get("retired_on")
    if not isinstance(value, str):
        return None
    try:
        day = dt.date.fromisoformat(value[:10])
    except ValueError:
        return None
    return value[:10] if day <= dt.datetime.now().date() else None


def choosable_models(roster: dict[str, Any], pool: str) -> dict[str, str]:
    """{model_key: what to run} for every model this pool can be set to.

    A routed pool: the models of its admitted lanes, run by lane. A direct pool: every model the
    roster places there with a runnable id (a card's run_as, else the key itself), current or
    older, minus cards marked hidden, gone or retired. An evidence entry without a card is choosable only
    while its status is not retired, because an entry like "gpt-5.6-sol-and-terra" names two
    models at once and is not an id a CLI accepts.
    """
    cards = model_cards(roster)
    if not pool_is_direct(roster, pool):
        return {lane["model_key"]: lane["model_key"] for lane in roster.get("lanes", [])
                if lane.get("quota_pool") == pool and _lane_is_admitted(lane)
                and not retired_on(roster, lane["model_key"])}
    found: dict[str, str] = {}
    for key, card in cards.items():
        if (card.get("pool") != pool or card.get("hidden") or card.get("status") == "gone"
                or retired_on(roster, key)):
            continue
        run_as = str(card.get("run_as") or key)
        if MODEL_ID_RE.match(run_as):
            found[key] = run_as
    for key, item in (roster.get("model_evidence") or {}).items():
        if key in found or key in cards or not isinstance(item, dict):
            continue
        owner = item.get("quota_pool") or ("codex" if re.match(r"gpt-\d", key) else None)
        if owner == pool and not _evidence_is_older(item) and MODEL_ID_RE.match(key):
            found[key] = key
    return found


def model_choice(runtime: dict[str, Any], pool: str) -> str | None:
    """The single choice an older version of the console stored for this pool, if any."""
    value = (runtime.get("model_choices") or {}).get(pool)
    return value if isinstance(value, str) and value else None


def effective_model_choice(roster: dict[str, Any], runtime: dict[str, Any], pool: str) -> str | None:
    """That older single choice, when the pool can still run it."""
    chosen = model_choice(runtime, pool)
    return chosen if chosen and chosen in choosable_models(roster, pool) else None


def toggled_off(runtime: dict[str, Any], pool: str) -> set[str]:
    value = (runtime.get("model_toggles") or {}).get(pool)
    return {model for model in value if isinstance(model, str)} if isinstance(value, list) else set()


def older_model_reason(roster: dict[str, Any], pool: str, model: str) -> str | None:
    """A comparative reason to keep an older model, scoped to the pool funding it.

    best_for and lane notes describe jobs, not an advantage over a current model.
    Accept a structured retention reason or the existing sourced comparison cards.
    """
    card = model_cards(roster).get(model, {})
    current = {key for key in choosable_models(roster, pool)
               if not model_is_older(roster, pool, key)}
    reasons = card.get("older_model_reasons")
    reason = reasons.get(pool) if isinstance(reasons, dict) else None
    if (isinstance(reason, dict) and isinstance(reason.get("compared_to"), str)
            and reason["compared_to"] in current):
        if (reason.get("advantage") in ("cheaper", "faster", "better")
                and all(isinstance(reason.get(key), str) and reason[key].strip()
                        for key in ("job", "reason", "evidence"))):
            return (f"{reason['advantage'].capitalize()} than {reason['compared_to']} for "
                    f"{reason['job']}: {reason['reason']} (evidence: {reason['evidence']})")
    successor = superseded_by(roster, pool, model)
    if successor in current:
        comparisons = card.get("better_than_successor_at")
        for item in comparisons if isinstance(comparisons, list) else []:
            if (isinstance(item, dict) and item.get("margin") in (None, "clear", "small", "higher", "lower")
                    and all(isinstance(item.get(key), str) and item[key].strip()
                            for key in ("capability", "source"))
                    and all(type(item.get(key)) in (str, int, float) and str(item[key]).strip()
                            and (not isinstance(item[key], float)
                                 or float("-inf") < item[key] < float("inf"))
                            for key in ("this", "successor"))):
                return (f"{item['capability']}: {item['this']} vs {item['successor']} on "
                        f"{successor} ({item.get('margin') or 'recorded lead'}; evidence: {item['source']})")
    return None


def older_model_audit(roster: dict[str, Any], runtime: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for pool in roster.get("quota_pools") or {}:
        switches = pool_switches(roster, runtime, pool)
        chosen = effective_model_choice(roster, runtime, pool)
        for model in model_order(roster, pool):
            if not model_is_older(roster, pool, model):
                continue
            reason = older_model_reason(roster, pool, model)
            # Read the previous all-on default too, so a dry run exposes what will be retired.
            stored_on = (model not in toggled_off(runtime, pool)
                         and model_preference(runtime, model) != "off"
                         and (chosen is None or model == chosen))
            rows.append({"pool": pool, "model": model, "on": switches[model],
                         "reason": reason, "stored_on": stored_on,
                         "action": "off" if stored_on and not reason else "keep"})
    return rows


def apply_older_model_rule(roster: dict[str, Any], runtime: dict[str, Any]) -> list[dict[str, Any]]:
    rows = older_model_audit(roster, runtime)
    for row in rows:
        if row["action"] == "off":
            set_model_toggle(runtime, roster, row["pool"], row["model"], False)
    return rows


def pool_switches(roster: dict[str, Any], runtime: dict[str, Any], pool: str) -> dict[str, bool]:
    """{model: on} for every model this pool can run, in the order the roster lists them.

    A model is off when this pool's switch is off, when an older console's Off excluded it
    everywhere, or, in a file with an older single choice, when another model was the one chosen.
    """
    chosen = effective_model_choice(roster, runtime, pool)
    down = toggled_off(runtime, pool)
    return {model: model not in down and model_preference(runtime, model) != "off"
            and (chosen is None or model == chosen)
            and (not model_is_older(roster, pool, model) or bool(older_model_reason(roster, pool, model)))
            for model in choosable_models(roster, pool)}


def models_state(switches: dict[str, bool], current: Iterable[str] | None = None) -> str:
    """empty | only (the one model, on) | all | some | one | none: what the switches add up to.

    Only the current models count: an older model that is on runs when a task names it, but never
    stands in for a model that is off, so it neither makes a provider "some on" nor keeps it "on".
    """
    view = {model: value for model, value in switches.items() if current is None or model in set(current)}
    on = sum(view.values())
    if not view:
        return "empty"
    if on == len(view):
        return "only" if len(view) == 1 else "all"
    return "none" if not on else "one" if on == 1 else "some"


def _settle(runtime: dict[str, Any], roster: dict[str, Any], pool: str) -> set[str]:
    """Turn the older settings that reach this pool into switches, and return its models that are off.

    A single choice becomes an Off for every other model of the pool. An older per-model Off
    becomes an Off on every provider that runs the model, so what it excluded stays excluded
    on the others while this pool's switch is free to differ.
    """
    down = set(toggled_off(runtime, pool))
    chosen = effective_model_choice(roster, runtime, pool)
    if chosen:
        down |= {model for model in choosable_models(roster, pool) if model != chosen}
    (runtime.get("model_choices") or {}).pop(pool, None)
    older = runtime.get("model_preferences") or {}
    for model in [key for key in choosable_models(roster, pool) if older.get(key) == "off"]:
        for other in roster.get("quota_pools") or {}:
            if other != pool and model in choosable_models(roster, other):
                _store_off(runtime, other, toggled_off(runtime, other) | {model})
        down.add(model)
        set_model_preference(runtime, model, "normal")
    return down


def _store_off(runtime: dict[str, Any], pool: str, down: set[str]) -> None:
    toggles = runtime.setdefault("model_toggles", {})
    if down:
        toggles[pool] = sorted(down)
    else:
        toggles.pop(pool, None)


def set_model_toggle(runtime: dict[str, Any], roster: dict[str, Any], pool: str, model: str, on: bool) -> None:
    """Switch one model of a pool on or off. Switching all of a pool's models off turns the provider off."""
    if pool not in (roster.get("quota_pools") or {}):
        raise ValueError("unknown pool")
    if model not in choosable_models(roster, pool):
        raise ValueError("that model cannot run on this pool")
    if on and model_is_older(roster, pool, model) and not older_model_reason(roster, pool, model):
        raise ValueError("older model needs a roster reason it beats a current model for a named job")
    down = _settle(runtime, roster, pool)
    _store_off(runtime, pool, down - {model} if on else down | {model})


def set_model_choice(runtime: dict[str, Any], roster: dict[str, Any], pool: str, model: str | None) -> None:
    """Run only this model on the pool (every other one off); None or "auto" switches them all on."""
    if pool not in (roster.get("quota_pools") or {}):
        raise ValueError("unknown pool")
    choosable = choosable_models(roster, pool)
    if model is not None and model != "auto" and model not in choosable:
        raise ValueError("that model cannot run on this pool")
    if model not in (None, "auto") and model_is_older(roster, pool, model) and not older_model_reason(roster, pool, model):
        raise ValueError("older model needs a roster reason it beats a current model for a named job")
    _settle(runtime, roster, pool)
    unjustified = {key for key in choosable if model_is_older(roster, pool, key)
                   and not older_model_reason(roster, pool, key)}
    _store_off(runtime, pool, unjustified if model in (None, "auto") else set(choosable) - {model})


def model_order(roster: dict[str, Any], pool: str, also: Iterable[str] = ()) -> list[str]:
    """The pool's models in the order the roster's cards give them: current first, best first.
    `also` places a model the pool can no longer run (a retired one) where its card would stand."""
    cards = model_cards(roster)

    def rank(key: str) -> tuple[bool, float, str]:
        return model_is_older(roster, pool, key), cards.get(key, {}).get("order", float("inf")), key.casefold()
    return sorted({*choosable_models(roster, pool), *also}, key=rank)


def current_models(roster: dict[str, Any], pool: str) -> list[str]:
    """The models the provider's own app would list at the top, in list order.

    Everything the pool can run that is not older (model_is_older); when a pool has no current
    model at all, every model counts as current.
    """
    order = model_order(roster, pool)
    return [model for model in order if not model_is_older(roster, pool, model)] or order


def resolve_model(roster: dict[str, Any], pool: str, name: str | None) -> str | None:
    """The pool's model a wrapper's --model value names: its key, its run id or a card alias."""
    cards = model_cards(roster)
    for key, run_as in choosable_models(roster, pool).items():
        if name and name in (key, run_as, *(cards.get(key, {}).get("aliases") or [])):
            return key
    return None


def named_model(roster: dict[str, Any], pool: str, name: str | None) -> str | None:
    """The pool's model a name means, a retired one included: its key, run id or alias on a direct
    pool, its key or a lane's selector on a routed one. None for a name the roster does not list."""
    if not name:
        return None
    found = resolve_model(roster, pool, name)
    if found:
        return found
    if pool_is_direct(roster, pool):
        for key, card in model_cards(roster).items():
            if card.get("pool") == pool and name in (key, card.get("run_as"), *(card.get("aliases") or [])):
                return key
        return None
    for lane in roster.get("lanes", []):
        if lane.get("quota_pool") == pool and name in (lane.get("selector"), lane.get("model_key")):
            return lane["model_key"]
    return None


def model_blocked(roster: dict[str, Any], runtime: dict[str, Any], pool: str, key: str) -> str | None:
    """"retired" or "off" when this model of the pool must not be started, None while it may be."""
    if retired_on(roster, key):
        return "retired"
    if pool_switches(roster, runtime, pool).get(key) is False or model_preference(runtime, key) == "off":
        return "off"
    return None


def nearest_on(order: list[str], wanted: str | None, on: list[str]) -> str | None:
    """The model that is on nearest to `wanted` in the order, the cheaper side first on a tie."""
    ranked = [model for model in order if model in on]
    if not ranked or wanted not in order:
        return ranked[0] if ranked else None
    at = order.index(wanted)
    return min(ranked, key=lambda model: (abs(order.index(model) - at), order.index(model) < at))


class NoModelOn(FleetError):
    pass


def model_run_as(roster: dict[str, Any], runtime: dict[str, Any], pool: str, requested: str | None = None) -> str | None:
    """What a direct wrapper passes as its model flag, or None to leave the task's own model.

    With nothing off, or the model the task asks for on, the task's own model stands. Otherwise
    the nearest current model that is on. A retired model is never run: a task that asks for one
    gets the nearest current model that is on, whatever the switches say. Raises NoModelOn when
    none is on: the provider is off (an older model that is on still runs when a task names it,
    but never stands in).
    """
    switches = pool_switches(roster, runtime, pool)
    direct = pool_is_direct(roster, pool)
    asked = requested or (direct_default_model(pool) if direct else None)
    named = named_model(roster, pool, asked) if direct else None
    gone = named if named and retired_on(roster, named) else None
    if (not switches or all(switches.values())) and not gone:
        return None
    on = [model for model, value in switches.items() if value]
    off_message = (f"no model is switched on for {pool} in the {PRODUCT_NAME} console "
                   f"(fleetctl.py model-toggle {pool} <model> on)")
    if not direct:
        if not on:
            raise NoModelOn(off_message)
        return None
    wanted = resolve_model(roster, pool, asked)
    if wanted in on:
        return None
    standing = [model for model in on if model in current_models(roster, pool)]
    if not standing:
        raise NoModelOn(off_message)
    if gone:
        return choosable_models(roster, pool)[nearest_on(model_order(roster, pool, [gone]), gone, standing)]
    if wanted is None:   # a name the roster does not list: stand in for the wrapper's own default
        wanted = resolve_model(roster, pool, direct_default_model(pool))
    return choosable_models(roster, pool)[nearest_on(model_order(roster, pool), wanted, standing)]


def pool_retired(roster: dict[str, Any], pool: str) -> dict[str, str]:
    """{model: the day it was retired} for the models of this pool their vendor has withdrawn."""
    found: dict[str, str] = {}
    for key, card in model_cards(roster).items():
        if card.get("pool") == pool and not card.get("hidden") and retired_on(roster, key):
            found[key] = retired_on(roster, key)
    for lane in roster.get("lanes", []):
        if lane.get("quota_pool") == pool and lane.get("model_key") and retired_on(roster, lane["model_key"]):
            found[lane["model_key"]] = retired_on(roster, lane["model_key"])
    return found


def stand_ins(roster: dict[str, Any], runtime: dict[str, Any], pool: str) -> dict[str, str]:
    """{model that is off or retired: the model id that runs when a task asks for it}. Direct pools
    only: on a routed pool the router simply skips such a model, and with none on nothing stands in."""
    if not pool_is_direct(roster, pool):
        return {}
    found: dict[str, str] = {}
    off = [model for model, on in pool_switches(roster, runtime, pool).items() if not on]
    for model in [*off, *pool_retired(roster, pool)]:
        try:
            picked = model_run_as(roster, runtime, pool, model)
        except NoModelOn:
            return {}
        if picked:
            found[model] = picked
    return found


# ---- pins: a model named in a file Crossfeed does not own ------------------------------------------
# A switch on the console only reaches a run that goes through a wrapper. A file that names a model
# runs it whatever the console says: a model switched off can still be pinned by Codex subagent seats
# (~/.codex/agents/builder.toml and reviewer.toml), so a Codex lead spawning its own builders runs it
# anyway. The roster declares such files per pool:
#
#   quota_pools.<pool>.model_pins = [{"path": "~/.codex/agents/*.toml", "key": "model",
#                                     "format": "toml", "manage": true, "what": "Codex subagent seats"}]
#
# manage true : Crossfeed keeps the pin in step with the switches. While the model it names is off
#               the file names the model that stands in; when it is switched on again the file gets
#               its own name back. What the file said is recorded (pins.json in the state folder,
#               with a copy of the file as it was), so `pins undo` puts everything back.
# manage false: watched only, for a file an app owns and rewrites itself (the Codex app writes
#               ~/.codex/config.toml from its model picker). Crossfeed says so on the console, in
#               `brief` and in `pins`, and never writes it.
# Nothing is declared by default, so nothing outside the state folder is touched until the roster says so.
PINS_SCHEMA = "model-pins/v1"
PIN_FORMATS = ("toml", "json", "frontmatter")


def _pin_pattern(fmt: str, key: str) -> re.Pattern[str]:
    """The value is always the group named `value`; TOML takes either quote."""
    name = re.escape(key)
    if fmt == "toml":
        return re.compile(rf'^[ \t]*{name}[ \t]*=[ \t]*(["\'])(?P<value>[^"\'\n]*)\1', re.M)
    if fmt == "json":
        return re.compile(rf'"{name}"[ \t]*:[ \t]*"(?P<value>[^"\n]*)"')
    return re.compile(rf'^{name}:[ \t]*["\']?(?P<value>[^"\'\n#]*?)["\']?[ \t]*$', re.M)


def _json_depth_at(text: str, position: int) -> int:
    """How many objects or arrays are open at `position` of a JSON or JSONC text, or -1 when
    `position` sits inside a string or a comment, so a pin is read only where the key belongs to
    the top-level object (depth 1)."""
    depth, index, in_string = 0, 0, False
    while index < position:
        char = text[index]
        if in_string:
            if char == "\\":
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
            if index > position:
                return -1
            continue
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = len(text) if close < 0 else close + 2
            if index > position:
                return -1
            continue
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        index += 1
    return -1 if in_string else depth


def read_pin(text: str, fmt: str, key: str) -> tuple[str, int, int] | None:
    """(value, start, end) of the model a file pins, or None when it pins none.

    Only where the key is the file's own: a TOML file's top level (before its first table), a
    markdown file's front matter, a JSON file's top-level object (a "model" inside an agent or
    provider block is that block's, not the file's).
    """
    end = len(text)
    if fmt == "toml":
        table = re.search(r"^[ \t]*\[", text, re.M)
        end = table.start() if table else end
    elif fmt == "frontmatter":
        close = text.find("\n---", 3) if text.startswith("---") else -1
        end = close if close >= 0 else 0
    for match in _pin_pattern(fmt, key).finditer(text, 0, end):
        if fmt == "json" and _json_depth_at(text, match.start()) != 1:
            continue
        return match.group("value"), match.start("value"), match.end("value")
    return None


def declared_pins(roster: dict[str, Any]) -> list[dict[str, Any]]:
    """Every file the roster says names a model, one entry per file that exists."""
    found: list[dict[str, Any]] = []
    for pool, config in (roster.get("quota_pools") or {}).items():
        for pin in (config if isinstance(config, dict) else {}).get("model_pins") or []:
            if not isinstance(pin, dict) or not isinstance(pin.get("path"), str):
                continue
            guess = "toml" if pin["path"].endswith(".toml") else "frontmatter" if pin["path"].endswith(".md") else "json"
            fmt = pin.get("format") or guess
            if fmt not in PIN_FORMATS:
                continue
            for path in sorted(glob.glob(os.path.expanduser(pin["path"]))):
                found.append({"pool": pool, "path": Path(path), "key": str(pin.get("key") or "model"),
                              "format": fmt, "manage": pin.get("manage") is True,
                              "what": pin.get("what") if isinstance(pin.get("what"), str) else None})
    return found


def _pin_stand_in(roster: dict[str, Any], runtime: dict[str, Any], pool: str, value: str) -> str | None:
    """What a pin that names a model that is off should say instead; None when nothing is on."""
    if pool_is_direct(roster, pool):
        try:
            return model_run_as(roster, runtime, pool, value)
        except NoModelOn:
            return None
    usual = usual_model(roster, runtime, pool)
    if not usual or not pool_switches(roster, runtime, pool).get(usual):
        return None
    for lane in roster.get("lanes", []):
        if lane.get("quota_pool") == pool and lane.get("model_key") == usual and _lane_is_admitted(lane):
            return lane.get("selector") if "/" in value else usual
    return None


def _home_path(path: Path) -> str:
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def _read_text_exact(path: Path) -> str:
    """The file's text with its own line endings (read_text would turn CRLF into LF, and an undo
    would then not give the file back byte for byte)."""
    return path.read_bytes().decode("utf-8")


def _write_text_atomic(path: Path, text: str) -> None:
    # A pinned file that is a link (a dotfiles checkout) is written where it points, so the link survives.
    path = path.resolve()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def sync_pins(roster: dict[str, Any], state_dir: Path, write: bool = True, undo: bool = False) -> list[dict[str, Any]]:
    """Bring every managed pin in step with the switches, and report every pin.

    One row per pinned file: `value` is what the file names now, `on` whether that model is
    switched on (None when the roster does not list it), `original` what the file said before
    Crossfeed changed it, and `action`:
      ok        names a model that is on, untouched
      rewritten now names the stand-in, because the model it named is off
      held      still names the stand-in from an earlier change
      restored  names its own model again (it is back on, or `undo` was asked)
      off       names a model that is off or retired and Crossfeed may not write the file (manage
                false), or no model is on to stand in
      no pin / unreadable: nothing to do
    With write false nothing is changed and `rewritten`/`restored` read `would rewrite`/`would restore`.
    `blocked` ("off" or "retired") is set whenever the file, as it stands, names a model that must
    not start: a run started from that file would still use it.
    """
    pins = declared_pins(roster)
    if not pins:
        return []
    runtime = load_json(state_dir / "runtime.json", {}) or {}
    rows: list[dict[str, Any]] = []
    # Reading never writes: `pins status`, `brief` and the console take no lock and create nothing.
    if write:
        state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "pins.lock").open("a+", encoding="utf-8") if write else contextlib.nullcontext() as lock:
        if lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            store = load_json(state_dir / "pins.json", {}) or {}
            records = store.get("pins") if isinstance(store.get("pins"), dict) else {}
            before = json.dumps(records, sort_keys=True)
            backed_up: set[Path] = set()
            for pin in pins:
                pool, path, ident = pin["pool"], pin["path"], f"{pin['path']}::{pin['key']}"
                row = {"pool": pool, "path": _home_path(path), "file": path.name, "key": pin["key"],
                       "manage": pin["manage"], "what": pin["what"], "value": None, "model": None,
                       "on": None, "blocked": None, "original": None, "action": "unreadable"}   # blocked: "off" | "retired" | None
                rows.append(row)
                try:
                    text = _read_text_exact(path)
                except (OSError, UnicodeDecodeError):
                    continue
                found = read_pin(text, pin["format"], pin["key"])
                if not found:
                    row["action"] = "no pin"
                    continue
                value, start, end = found
                record = records.get(ident)
                if record and record.get("written") != value:
                    # Its owner has edited the file since: it is theirs again, and what it says now is its own word.
                    records.pop(ident)
                    record = None
                own = record["original"] if record else value
                own_model = named_model(roster, pool, own)
                own_blocked = bool(own_model and model_blocked(roster, runtime, pool, own_model))
                want = own
                if not undo and own_blocked:
                    want = _pin_stand_in(roster, runtime, pool, own) or value
                if not MODEL_ID_RE.match(want):
                    want = value
                if want != value and pin["manage"]:
                    row["action"] = ("restored" if want == own else "rewritten") if write else (
                        "would restore" if want == own else "would rewrite")
                    row["to"] = want
                    if write:
                        # The file as it was, once, before Crossfeed first changed it: not again for a
                        # second key of the same file (that copy would hold the first key's change).
                        untouched = not any(other.startswith(f"{path}::") for other in records)
                        if untouched and path not in backed_up:
                            backed_up.add(path)
                            keep = state_dir / "pin-backups"
                            keep.mkdir(parents=True, exist_ok=True)
                            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
                            backup = keep / f"{stamp}-{path.parent.name}-{path.name}"
                            backup.write_bytes(text.encode("utf-8"))
                            os.chmod(backup, 0o600)
                        _write_text_atomic(path, text[:start] + want + text[end:])
                        if want == own:
                            records.pop(ident, None)
                        else:
                            records[ident] = {"pool": pool, "original": own, "written": want, "at": iso()}
                        value = want
                else:
                    row["action"] = "held" if record else "ok"
                model = named_model(roster, pool, value)
                blocked = model_blocked(roster, runtime, pool, model) if model else None
                row.update(value=value, model=model, on=None if not model else not blocked, blocked=blocked,
                           original=records.get(ident, {}).get("original"))
                if blocked and row["action"] in {"ok", "held"}:
                    row["action"] = "off"   # nothing was changed, and the file names a model that must not start
            if write and json.dumps(records, sort_keys=True) != before:
                atomic_json(state_dir / "pins.json", {"schema": PINS_SCHEMA, "pins": records})
        finally:
            if lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return rows


def pins_after_switch(roster: dict[str, Any], state_dir: Path) -> list[dict[str, Any]]:
    """Called after every switch change. Never the reason a switch fails to save."""
    try:
        return sync_pins(roster, state_dir)
    except Exception:  # noqa: BLE001 - a file Crossfeed cannot write must not undo the operator's click
        return []


def _pin_line(row: dict[str, Any]) -> str:
    text = f"{row['action']:13s} {row['pool']:10s} {row['path']} ({row['key']} = {row['value']})"
    if row["action"].startswith("would") and row.get("to"):
        text += f", would become {row['to']}"
    if row.get("original"):
        text += f", was {row['original']}"
    if row.get("blocked"):
        text += f": names a model that is {'retired' if row['blocked'] == 'retired' else 'switched off'}"
        if row["action"] == "off":
            text += "; nothing is on to stand in" if row["manage"] else "; watched only, not written"
    return text


def _lane_names(lane: dict[str, Any]) -> tuple[str, ...]:
    selector = str(lane.get("selector") or "")
    return (selector, lane["model_key"], selector.split("/")[-1])


def start_verdict(roster: dict[str, Any], runtime: dict[str, Any], name: str, *, pool: str | None = None,
                  harness: str | None = None, provider: str | None = None) -> dict[str, Any] | None:
    """Why a run that names this model must not start, and what to name instead; None when it may.

    `pool` names a direct pool (codex, claude): the name is its model's key, run id or alias.
    Otherwise the name is matched against the roster's lanes (selector, model key, or the
    selector's last part), narrowed to one harness or provider. A name the roster does not list is
    not Crossfeed's to refuse. A model is refused when every place that lists it has it switched
    off, retired, or its provider set to Off.

    {"model", "pool", "why": "off" | "retired" | "provider off", "instead": [what to name instead,
    nearest first], "message": one plain sentence or two for the agent that wrote the command}.
    """
    if not name:
        return None
    pools = roster.get("quota_pools") or {}
    if pool:
        model = named_model(roster, pool, name)
        places = [(pool, model, None)] if model else []
    else:
        places = [(lane["quota_pool"], lane["model_key"], lane) for lane in roster.get("lanes", [])
                  if lane.get("quota_pool") and lane.get("model_key")
                  and (not harness or lane.get("harness") == harness)
                  and (not provider or lane.get("provider") == provider)
                  and name in _lane_names(lane)]
    verdicts: list[dict[str, Any]] = []
    for where, model, lane in places:
        label = pool_label(where, pools.get(where) or {})
        blocked = model_blocked(roster, runtime, where, model)
        if blocked == "retired":
            why, text = "retired", f"{name} was retired by its provider on {retired_on(roster, model)} and can no longer be run."
        elif pool_level(runtime, where) == "off":
            why, text = "provider off", f"{label} is set to Off in the {PRODUCT_NAME} console, so {name} cannot start."
        elif blocked:
            why, text = "off", f"{name} is switched off for {label} in the {PRODUCT_NAME} console."
        else:
            return None
        verdicts.append({"model": model, "pool": where, "why": why, "lane": lane, "text": text})
    if not verdicts:
        return None
    first = verdicts[0]
    where, model = first["pool"], first["model"]
    instead: list[str] = []
    if first["why"] != "provider off":
        if pool_is_direct(roster, where):
            with contextlib.suppress(NoModelOn):
                picked = model_run_as(roster, runtime, where, name)
                if picked:
                    instead.append(picked)
        else:
            switches = pool_switches(roster, runtime, where)
            order = model_order(roster, where, [model])
            on = [key for key in current_models(roster, where) if switches.get(key)]
            on.sort(key=lambda key: (abs(order.index(key) - order.index(model)), order.index(key) < order.index(model)))
            for key in on:
                lane = next((item for item in roster.get("lanes", [])
                             if item.get("quota_pool") == where and item.get("model_key") == key
                             and _lane_is_admitted(item)
                             and (not harness or item.get("harness") == harness)), None)
                if lane and lane.get("selector") and lane["selector"] not in instead:
                    instead.append(lane["selector"])
            instead = instead[:3]
    message = first["text"]
    if instead:
        message += f" Name {instead[0]} instead" + (
            ", the model that runs in its place." if pool_is_direct(roster, where)
            else f" (or {', '.join(instead[1:])})." if len(instead) > 1 else ".")
    elif first["why"] != "retired":
        message += " Nothing on that provider is switched on to run in its place."
    if first["why"] in {"off", "provider off"}:
        # No command here on purpose: the switches belong to whoever runs the fleet, and an agent
        # that reads a command in a refusal tends to run it.
        message += " Only the owner can switch it back on, in the console."
    return {"model": model, "pool": where, "why": first["why"], "instead": instead, "reason": first["text"],
            "message": message}


def model_gate(roster: dict[str, Any], runtime: dict[str, Any], name: str,
               harness: str | None = None, provider: str | None = None, pool: str | None = None) -> str | None:
    """Why a run that names this model must not start, or None when it may.

    For a wrapper that is handed a model by name with no lane to lease (agy-agent.sh --model, the
    Gemini media and image transports), and for a direct pool with --pool. start_verdict decides.
    """
    verdict = start_verdict(roster, runtime, name, pool=pool, harness=harness, provider=provider)
    return verdict["message"] if verdict else None


def apply_model_toggles(
    candidates: list[str], roster: dict[str, Any], runtime: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Drop the lanes of models that are switched off from a role's candidates.

    Where a job ranks a provider but every model it ranks there is off, the provider's models
    that are on stand in, in the roster's order, at the slot of the first lane the job ranked
    there, so one model left on still does every job routed to its provider. The gates in
    choose_lane still judge every lane.
    """
    lanes = lane_map(roster)

    def pool_of(lane_id: str) -> str | None:
        lane = lanes.get(lane_id)
        return lane.get("quota_pool") if lane else None
    on: dict[str, set[str]] = {}
    for pool in {pool_of(lane_id) for lane_id in candidates} - {None}:
        switches = pool_switches(roster, runtime, pool)
        if switches and not all(switches.values()):
            on[pool] = {model for model, value in switches.items() if value}
    if not on:
        return list(candidates), []
    kept = {lane_id for lane_id in candidates
            if pool_of(lane_id) not in on or lanes[lane_id]["model_key"] in on[pool_of(lane_id)]}
    reached = {pool_of(lane_id) for lane_id in kept}
    result: list[str] = []
    rejected: list[str] = []
    stood: set[str] = set()
    for lane_id in candidates:
        pool = pool_of(lane_id)
        if lane_id in kept:
            if lane_id not in result:
                result.append(lane_id)
            continue
        rejected.append(f"{lane_id}: model switched off")
        if pool not in reached and pool not in stood:
            stood.add(pool)
            # An older model that is on still runs where a job ranks it, but never stands in.
            current = set(current_models(roster, pool))
            result.extend(l["lane_id"] for l in roster.get("lanes", [])
                          if l.get("quota_pool") == pool and l.get("lane_id") and l.get("model_key") in on[pool]
                          and l["model_key"] in current and l["lane_id"] not in result)
    return result, rejected


def codex_mcp_denials(roster: dict[str, Any], directory: Path) -> list[str]:
    """A worker building a tool must not drive the configured live tool instance."""
    rules = roster.get("policy", {}).get("codex_mcp_denials", {})
    if not isinstance(rules, dict):
        raise FleetError("policy.codex_mcp_denials must be an object")
    directory = directory.expanduser().resolve()
    denied = []
    for server, roots in rules.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", server) or not isinstance(roots, list):
            raise FleetError("invalid codex_mcp_denials server or root list")
        for root in roots:
            if not isinstance(root, str) or not root or not Path(root).expanduser().is_absolute():
                raise FleetError(f"invalid MCP denial root for {server}: use an absolute or home-relative path")
            path = Path(root).expanduser().resolve()
            if directory == path or path in directory.parents:
                denied.append(server)
                break
    return sorted(denied)


def resolve_effort(
    roster: dict[str, Any], model: str, role: str = "default",
    harness: str | None = None, explicit: str | None = None,
    band: str | None = None,
    stand_in: bool = False,
) -> dict[str, Any]:
    """Resolve the actual model's effort, never a CLI's saved setting.

    Missing data is returned visibly for route/doctor. Dispatch callers must refuse
    it. A model without exposed controls is an explicit provider-default exception.
    """
    lane = next((row for row in roster.get("lanes", [])
                 if model in (row.get("lane_id"), row.get("selector"))
                 and (not harness or row.get("harness") == harness)), None)
    if not lane and harness == "agy" and model.startswith("gemini-") and model.rsplit("-", 1)[-1] in ("low", "medium", "high", "max"):
        # Explicit levels need not have a separate auto-routing lane. They still
        # belong to the same measured family and use that family's quota pool.
        lane = next((row for row in roster.get("lanes", []) if row.get("harness") == "agy"
                     and row.get("selector", "").rsplit("-", 1)[0] == model.rsplit("-", 1)[0]), None)
    key = lane["model_key"] if lane else model
    harness = harness or (lane or {}).get("harness")
    if not key and harness in DIRECT_POOLS:
        key = direct_default_model(harness) or roster.get("policy", {}).get("effort_defaults", {}).get(harness, "")
    if harness in DIRECT_POOLS:
        key = named_model(roster, harness, key) or key
    table = roster.get("effort", {})
    if not isinstance(table, dict):
        raise FleetError("effort table must be an object")
    entry = table.get(key)
    result = {"model_key": key, "harness": harness, "effort": None,
              "source": "missing", "reason": f"missing effort data for {key}"}
    if not entry:
        # Explicit caller levels for an unlisted direct model are still supported.
        accepted = {"codex": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
                    "claude": ["low", "medium", "high", "xhigh", "max"]}
        if explicit and explicit in accepted.get(harness, []):
            result.update(effort=explicit, source="caller", reason="explicit caller level (unlisted model)")
        return result
    if not isinstance(entry, dict) or not isinstance(entry.get("levels"), dict):
        raise FleetError(f"invalid effort levels for {key}")
    if not isinstance(entry.get("refuse_roles", {}), dict):
        raise FleetError(f"invalid effort refusal data for {key}")
    refusal = entry.get("refuse_roles", {}).get(role) or entry.get("refuse_roles", {}).get(f"{band}/{role}")
    if refusal and not explicit:
        raise FleetError(f"{key}/{role}: {refusal}")
    supported = entry["levels"]
    if not harness and len(supported) == 1:
        harness = next(iter(supported))
        result["harness"] = harness
    if harness not in supported:
        result["reason"] = f"missing supported levels for {key} on {harness}"
        return result
    levels = supported[harness]
    if not isinstance(levels, list) or any(not isinstance(value, str) for value in levels):
        raise FleetError(f"invalid supported effort levels for {key} on {harness}")
    if not isinstance(entry.get("by_role", {}), dict) or not isinstance(entry.get("by_band", {}), dict):
        raise FleetError(f"invalid effort role/band data for {key}")
    band_roles = entry.get("by_band", {}).get(band, {})
    if not isinstance(band_roles, dict) or not isinstance(entry.get("evidence", {}), dict):
        raise FleetError(f"invalid effort band/evidence data for {key}")
    level = explicit or band_roles.get(role) or entry.get("by_role", {}).get(role) or entry.get("default")
    evidence = entry.get("evidence", {})
    result.update(source="caller" if explicit else "roster", reason=(
        "explicit caller level" if explicit else
        f"roster {key}/{role}; {evidence.get('status', 'unmeasured')}, read {evidence.get('read_on', 'unknown')}"
    ))
    if explicit and stand_in and levels:
        ceiling = entry.get("stand_in_ceiling", levels[-1])
        if ceiling not in levels:
            raise FleetError(f"invalid stand-in effort ceiling {ceiling!r} for {key}")
        allowed = levels[:levels.index(ceiling) + 1]
        if explicit not in allowed:
            level = allowed[-1]
            result["reason"] += f"; clamped {explicit} to {level} for stand-in {key} (highest allowed level)"
    if not levels:
        if explicit:
            raise FleetError(f"unsupported effort {explicit!r} for {key} on {harness}; supported: none (no level control exposed)")
        if entry.get("default") not in ("provider-default", "service-chosen"):
            result.update(source="missing", reason=f"{key}: no levels exposed but no default exception recorded")
            return result
        control = "level control support not established" if entry.get("control") == "unverified" else "no level control exposed"
        result["reason"] += f"; {entry['default']}, {control}"
        if lane and lane.get("gateway_service"):
            result.update(effort=entry["default"], reason="Thinking level is encoded in the gateway catalog selector")
        return result
    if level not in levels:
        if harness == "opencode" and not explicit:
            result["reason"] += f"; {level} unsupported, provider-default (unmeasured)"
            return result
        raise FleetError(f"unsupported effort {level!r} for {key} on {harness}; supported: {', '.join(levels)}")
    result["effort"] = level
    return result


def effort_problems(roster: dict[str, Any], state_dir: Path, now: dt.datetime | None = None) -> list[str]:
    """Read-only evidence health, including explicit-only lanes and direct seats."""
    now = now or utc_now()
    if not isinstance(roster.get("effort", {}), dict):
        return ["roster: effort table must be an object"]
    required = {(lane["model_key"], lane["harness"]) for lane in roster.get("lanes", [])
                if lane.get("admission_status") == "active"}
    required.update((key, card["pool"]) for key, card in model_cards(roster).items()
                    if card.get("pool") in DIRECT_POOLS and card.get("status") == "current" and not card.get("hidden"))
    catalog = load_json(state_dir / "market" / "market-catalog.json", {}) or {}
    try:
        fresh = 0 <= (now - parse_iso(catalog["fetched_at"])).total_seconds() <= 30 * 86400
    except (KeyError, TypeError, ValueError):
        fresh = False
    problems = []
    for key, harness in sorted(required):
        entry = roster.get("effort", {}).get(key)
        if not isinstance(entry, dict):
            problems.append(f"{key}: missing effort data ({harness})")
            continue
        evidence = entry.get("evidence", {})
        if not isinstance(evidence, dict):
            problems.append(f"{key}: invalid effort evidence, expected object")
            evidence = {}
        try:
            age = (now.date() - dt.date.fromisoformat(evidence["read_on"])).days
            if age < 0 or age > 30:
                problems.append(f"{key}: stale effort evidence ({age} days, maximum 30)")
            elif age >= 23:
                due = dt.date.fromisoformat(evidence["read_on"]) + dt.timedelta(days=30)
                problems.append(f"{key}: effort evidence recheck due {due.isoformat()} (in {30 - age} days; 7-day warning)")
        except (KeyError, TypeError, ValueError):
            problems.append(f"{key}: missing/invalid effort evidence read_on")
        if not evidence.get("source") or evidence.get("status") not in ("measured", "unmeasured") or not entry.get("recheck"):
            problems.append(f"{key}: effort evidence needs source, measured/unmeasured status and recheck trigger")
        if evidence.get("status") == "measured" and not evidence.get("index_version"):
            problems.append(f"{key}: measured effort evidence missing index_version")
        refusals = entry.get("refuse_roles", {})
        if not isinstance(refusals, dict) or any(not isinstance(reason, str) or not reason for reason in refusals.values()):
            problems.append(f"{key}: invalid effort refusal data")
        level_table = entry.get("levels", {})
        levels = level_table.get(harness) if isinstance(level_table, dict) else None
        if not isinstance(levels, list):
            problems.append(f"{key}: missing supported effort levels for {harness}")
        else:
            if entry.get("stand_in_ceiling", levels[-1] if levels else None) not in (levels or [None]):
                problems.append(f"{key}: unsupported stand-in effort ceiling")
            roles, bands = entry.get("by_role", {}), entry.get("by_band", {})
            if not isinstance(roles, dict) or not isinstance(bands, dict) or any(not isinstance(row, dict) for row in bands.values()):
                problems.append(f"{key}: invalid effort role/band data")
                roles, bands = {}, {}
            values = [entry.get("default"), entry.get("knee"), *roles.values(),
                      *(value for row in bands.values() for value in row.values())]
            allowed = levels or ["provider-default", "service-chosen"]
            if any(value not in allowed for value in values):
                problems.append(f"{key}: unsupported effort default/knee/role level")
        market = catalog.get("artificial_analysis", {}).get(key, {}) if fresh else {}
        version = market.get("index_version") or catalog.get("index_version")
        if version and evidence.get("index_version") and version != evidence["index_version"]:
            problems.append(f"{key}: effort index version changed ({evidence['index_version']} -> {version})")
        knee = entry.get("knee")
        current = market.get("levels", {}).get(knee, {})
        if current.get("ambiguous"):
            problems.append(f"{key}: effort knee {knee} snapshot ambiguous; recheck serving snapshot")
        curve = evidence.get("curve", {})
        old = curve.get(knee, {}) if isinstance(curve, dict) else {}
        if not isinstance(old, dict):
            problems.append(f"{key}: invalid effort curve for {knee}")
            old = {}
        measured = current.get("intelligence_index")
        recorded = old.get("intelligence_index")
        if isinstance(measured, (int, float)) and isinstance(recorded, (int, float)) and abs(measured - recorded) > 2:
            problems.append(f"{key}: effort knee {knee} score changed by more than 2 points ({recorded} -> {measured})")
    return problems


def choose_lane(
    roster: dict[str, Any],
    runtime: dict[str, Any],
    role: str,
    mode: str,
    modality: str,
    harness: str | None = None,
    one_shot: bool = False,
) -> dict[str, Any]:
    # The primary pool sets the BAND (how tight the main subscription is, hence how
    # far to step down), but it must not gate lanes that spend a different budget.
    # Exhausting it used to raise here, which killed routing outright -- the exact
    # moment untouched headroom in another pool matters most. Each candidate is now
    # judged against its OWN pool below.
    pool = ROUTING_POOL
    state, evidence = current_pool_state(runtime, pool, roster=roster)
    roles = roster.get("routing", {}).get("roles", {})
    role_policy = roles.get(role) or roles.get("default")
    if not role_policy:
        raise FleetError(f"unknown routing role: {role}")
    band = task_band(state, evidence)
    if not one_shot:
        # A one-shot keeps the measured band: stepping a low pool down to its cheap
        # lists would take the big model it asked for off the table.
        band = level_band(pool_level(runtime, pool), band)
    candidates = list(role_policy.get(band) or role_policy.get("quality_first", []))
    lanes = lane_map(roster)
    # A model-only limit can tighten an otherwise healthy pool. Its band's
    # alternatives may live in a disjoint list, so append them before the
    # existing toggle/admission checks. Keep unrelated quality candidates first.
    discovered_bands = {band}
    for lane_id in candidates:
        lane = lanes.get(lane_id, {})
        if lane.get("quota_pool", pool) != pool or not lane.get("model_key"):
            continue
        model_state, model_evidence = current_pool_state(runtime, pool, roster=roster,
                                                          model_key=lane["model_key"])
        model_band = task_band(model_state, model_evidence)
        if not one_shot:
            model_band = level_band(pool_level(runtime, pool), model_band)
        if model_band not in discovered_bands:
            discovered_bands.add(model_band)
            alternatives = role_policy.get(model_band) or role_policy.get("quality_first", [])
            candidates.extend(key for key in alternatives if key not in candidates)
    pro_fallbacks = {}
    expanded = []
    pro_pools = set()
    for lane_id in candidates:
        lane = lanes.get(lane_id, {})
        reason = chatgpt_pro.blocked(roster, runtime, lane)
        if reason:
            pro_pools.add(lane["quota_pool"])
            for replacement in chatgpt_pro.replacements(roster, runtime, lane):
                expanded.append(replacement["lane_id"])
                pro_fallbacks[replacement["lane_id"]] = chatgpt_pro.fallback(lane, reason)
        else:
            expanded.append(lane_id)
    # Once a role's Pro seat needs replacing, no lower ChatGPT level can take it.
    expanded = [key for key in expanded if not (
        lanes.get(key, {}).get("quota_pool") in pro_pools
        and lanes.get(key, {}).get("harness") == "chatgpt-chat"
        and lanes.get(key, {}).get("worker_level") not in {"xhigh", "high"})]
    candidates, rejected = apply_model_toggles(expanded, roster, runtime)
    # The generic switch stand-in can reintroduce Medium when both safe
    # replacements are off. Keep the Pro policy after that expansion too.
    candidates = [key for key in candidates if not (
        lanes.get(key, {}).get("quota_pool") in pro_pools
        and lanes.get(key, {}).get("harness") == "chatgpt-chat"
        and lanes.get(key, {}).get("worker_level") not in {"xhigh", "high"})]
    candidates = order_for_levels(candidates, lanes, runtime, one_shot)
    for pro_pool in pro_pools:
        slots = [index for index, key in enumerate(candidates) if lanes.get(key, {}).get("quota_pool") == pro_pool]
        ordered = sorted((candidates[index] for index in slots), key=lambda key: lanes[key].get("worker_level") != "xhigh")
        for index, key in zip(slots, ordered):
            candidates[index] = key
    for lane_id in candidates:
        lane = lanes.get(lane_id)
        if not lane:
            rejected.append(f"{lane_id}: missing")
            continue
        # Selector-encoded harnesses must route the same level they report.
        # Remap before admission and capacity checks, including older overlays
        # that still rank a high selector ahead of the recorded medium knee.
        if lane.get("harness") == "agy":
            resolved = resolve_effort(roster, lane["model_key"], role, "agy", band=band)
            selector = lane.get("selector", "")
            if resolved["effort"] and re.search(r"-(low|medium|high|max)$", selector):
                desired = re.sub(r"-(low|medium|high|max)$", "-" + resolved["effort"], selector)
                if desired != selector:
                    replacement = next((row for row in lanes.values()
                                        if row.get("selector") == desired and row.get("harness") == "agy"
                                        and row.get("model_key") == lane["model_key"]
                                        and row.get("quota_pool") == lane.get("quota_pool")), None)
                    if replacement is None:
                        rejected.append(f"{lane_id}: no admitted effort selector {desired}")
                        continue
                    lane = replacement
                    lane_id = lane["lane_id"]
        if model_preference(runtime, lane["model_key"]) == "off":
            rejected.append(f"{lane_id}: model switched off")
            continue
        if retired_on(roster, lane["model_key"]):
            rejected.append(f"{lane_id}: model retired")
            continue
        if harness and lane.get("harness") != harness:
            # A role's candidates may span harnesses, but each wrapper can only run
            # its own. Without this filter a wrapper is handed a lane it must
            # refuse, and the run dies at exactly the moment routing stepped down.
            rejected.append(f"{lane_id}: harness {lane.get('harness')}")
            continue
        if lane.get("access_status") != "verified":
            rejected.append(f"{lane_id}: access {lane.get('access_status')}")
            continue
        if lane.get("admission_status") != "active":
            rejected.append(f"{lane_id}: admission {lane.get('admission_status')}")
            continue
        if lane.get("gateway_status", {}).get("quota_blocked") or lane.get("gateway_status", {}).get("rate_limited"):
            rejected.append(f"{lane_id}: worker quota paused")
            continue
        if mode not in lane.get("allowed_modes", []):
            rejected.append(f"{lane_id}: mode {mode}")
            continue
        inputs = lane.get("capabilities", {}).get("input", ["text"])
        if modality not in inputs:
            rejected.append(f"{lane_id}: modality {modality}")
            continue
        lane_pool = lane.get("quota_pool", pool)
        lane_state, lane_evidence = current_pool_state(runtime, lane_pool, roster=roster,
                                                       model_key=lane["model_key"])
        if lane_state == "EXHAUSTED":
            # Routing must never hand back a lane acquire_lease would refuse.
            why = "switched off" if lane_evidence.get("source") == "switched-off" else "exhausted"
            rejected.append(f"{lane_id}: pool {lane_pool} {why}")
            continue
        lane_band = band
        if lane_pool == pool:
            lane_band = task_band(lane_state, lane_evidence)
            if not one_shot:
                lane_band = level_band(pool_level(runtime, pool), lane_band)
            if lane_band != band:
                admitted = role_policy.get(lane_band) or role_policy.get("quality_first", [])
                admitted, _ = apply_model_toggles(admitted, roster, runtime)
                if lane_id not in admitted:
                    rejected.append(f"{lane_id}: outside {lane_band.upper()} role allowlist")
                    continue
        if lane_free_slots(runtime, lane, lane_state, lane_evidence) <= 0:
            # A busy lane must not block the task: fall through to the next
            # ranked candidate rather than fail.  Quality still leads, but a
            # lane already at cap is not available quality.
            rejected.append(f"{lane_id}: at capacity")
            continue
        result = dict(lane)
        if lane_id in pro_fallbacks:
            result["pro_fallback"] = pro_fallbacks[lane_id]
        effort = resolve_effort(roster, lane["model_key"], role, lane["harness"], band=lane_band)
        result["effort"] = effort["effort"]
        result["effort_reason"] = effort["reason"]
        result["routing"] = {
            "role": role,
            "band": lane_band,
            "pool": lane_pool,
            "pool_state": lane_state,
            "pool_evidence": lane_evidence,
            "level": pool_level(runtime, lane_pool),
            "one_shot": one_shot,
            "principle": "highest task-specific quality before cost within the active band",
        }
        return result
    if harness == "chatgpt-chat" and roster.get("chatgpt_catalog", {}).get("error"):
        raise FleetError("Crossfeed Chat gateway admission closed: " + roster["chatgpt_catalog"]["error"])
    raise FleetError(
        f"no eligible lane for role={role}, mode={mode}, modality={modality}; "
        + "; ".join(rejected)
    )


def select_option(
    roster: dict[str, Any], runtime: dict[str, Any], state_dir: Path, role: str,
    *, stakes: str = "normal", family: str | None = None,
    exclude_lineage: str | None = None, mode: str | None = None, modality: str = "text",
    allow: str | Path | None = None, lead: str | None = None,
    target: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Keep additive selection separate while sharing the existing fleet gates."""
    spec = importlib.util.spec_from_file_location("fleet_selector", Path(__file__).with_name("selector.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.select_option(
        roster, runtime, state_dir, role, stakes=stakes, family=family,
        exclude_lineage=exclude_lineage, mode=mode, modality=modality, allow=allow, lead=lead,
        fleet=types.SimpleNamespace(**globals()),
        target=target,
    )


def dispatch_argv(option: dict[str, Any], prompt: str, directory: Path, last: Path) -> list[str]:
    """Fill the selector's argv without parsing its shell display string."""
    argv = list(option["command_argv"])
    slots = [index for index in range(len(argv) - 1)
             if argv[index:index + 2] == ["--prompt", "<task>"]]
    if len(slots) != 1:
        raise FleetError("selector command must contain one --prompt <task> placeholder")
    argv[slots[0] + 1] = prompt
    # OpenRouter is tool-less and deliberately rejects --dir.
    if option["harness"] != "openrouter":
        argv += ["--dir", str(directory)]
    argv += ["--last", str(last)]
    return argv


def _publish_dispatch_file(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def dispatch_selection(selection: dict[str, Any], args: argparse.Namespace, state_dir: Path,
                       prompt: str, *, response: dict[str, Any] | None = None) -> int:
    directory = args.dir.expanduser().resolve()
    requested_last = args.last.expanduser().resolve() if args.last else None
    options = [selection["choice"]]
    seen = {selection["choice"]["selection_file"]}
    for option in selection["top3"][:3]:
        if (selection["choice"].get("worker_level") == "pro" or selection["choice"].get("pro_fallback")) and (
                option.get("harness") == "chatgpt-chat" and option.get("worker_level") not in {"pro", "xhigh", "high"}):
            continue
        if option["selection_file"] not in seen:
            options.append(option)
            seen.add(option["selection_file"])
    for index, option in enumerate(options):
        dispatch_id = str(uuid.uuid4())
        attempt_dir = state_dir.resolve() / "dispatches" / dispatch_id
        last = attempt_dir / "answer.txt"
        identity = Path(str(last) + ".crossfeed.json")
        receipt = attempt_dir / "dispatch.json"
        argv = dispatch_argv(option, prompt, directory, last)
        env = dict(os.environ, FLEET_STATE_DIR=str(state_dir.resolve()),
                   ACCESS_OVERLAY=str(args.overlay.expanduser().resolve()),
                   CROSSFEED_DISPATCH_ID=dispatch_id)
        # The incoming API credential is never a worker/provider credential.
        env.pop("CROSSFEED_API_KEY", None)
        notice = (f"fleetctl: selected pool={option['pool']} model={option['model_key']} "
                  f"level={option['level']} selection={option['selection_file']} receipt={receipt} "
                  f"model_receipt={identity} diagnostics={attempt_dir / 'stderr.log'}")
        print(notice, file=sys.stderr, flush=True)
        if args.dry_run:
            print(json.dumps({"command_argv": argv, "cwd": str(directory),
                              "selection_file": option["selection_file"],
                              "receipt": str(receipt), "model_receipt": str(identity)}, indent=2))
            return 0
        attempt_dir.mkdir(parents=True)
        record = {"schema": "fleet-dispatch/v1", "dispatch_id": dispatch_id,
                  "started_at": iso(), "selection_file": option["selection_file"],
                  "pool": option["pool"], "selected_model": option["model_key"],
                  "level": option["level"], "model_receipt": str(identity),
                  "stderr_file": str(attempt_dir / "stderr.log")}
        atomic_json(receipt, record)
        # Failed workers may have printed partial answers. Keep those private so
        # fallback publishes exactly one successful worker's bytes to the caller.
        output = attempt_dir / "stdout.log"
        with (attempt_dir / "stderr.log").open("wb") as diagnostic, output.open("wb") as stdout:
            try:
                result = subprocess.run(argv, cwd=directory, env=env, stderr=diagnostic, stdout=stdout)
                returncode = result.returncode if result.returncode >= 0 else 128 - result.returncode
            except OSError:
                # Missing wrappers and process setup failures are not lane refusals.
                returncode = 127
        # Wrappers publish identity only after setting their supervised launch flag.
        # Each attempt has a new path and ID: inherited sidecars never participate.
        launched = identity.exists()
        record.update(ended_at=iso(), returncode=returncode,
                      launch_evidence="model-receipt-present" if launched else "no-model-receipt")
        atomic_json(receipt, record)
        if returncode == 0:
            if requested_last:
                _publish_dispatch_file(last, requested_last)
                _publish_dispatch_file(identity, Path(str(requested_last) + ".crossfeed.json"))
            if response is not None:
                response.update(content=(last if last.is_file() else output).read_text(encoding="utf-8", errors="replace"),
                                option=option, dispatch=record)
                return 0
            with output.open("rb") as stream:
                if hasattr(sys.stdout, "buffer"):
                    shutil.copyfileobj(stream, sys.stdout.buffer)
                    sys.stdout.buffer.flush()
                else:
                    sys.stdout.write(stream.read().decode("utf-8", errors="replace"))
            return 0
        # Explicit cancellation must not start fresh work against the caller's wish.
        if returncode in {129, 130, 143} or index == len(options) - 1:
            print(f"fleetctl: {option['harness']}:{option['model_key']} failed (exit {returncode}); "
                  "no replacement used", file=sys.stderr, flush=True)
            return returncode
        replacement = options[index + 1]
        print(f"fleetctl: {option['harness']}:{option['model_key']} failed (exit {returncode}); "
              f"using {replacement['harness']}:{replacement['model_key']} instead", file=sys.stderr, flush=True)
    raise FleetError("selector returned no dispatch options")


def clean_leases(runtime: dict[str, Any], now: dt.datetime | None = None) -> None:
    now = now or utc_now()
    runtime["leases"] = [
        lease
        for lease in runtime.get("leases", [])
        if parse_iso(lease["expires_at"]) > now and recorded_pid_is_alive(lease)
    ]


def recorded_pid_is_alive(record: dict[str, Any]) -> bool:
    """Return False only when a valid recorded PID is definitely gone."""
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return True
    return True


def local_day_start(now: dt.datetime | None = None) -> dt.datetime:
    """Start of the current calendar day in local time, returned as a UTC instant."""
    local = (now or utc_now()).astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(dt.timezone.utc)


def pool_lane_ids(roster: dict[str, Any], pool: str) -> set[str]:
    return {
        lane["lane_id"]
        for lane in roster.get("lanes", [])
        if lane.get("quota_pool") == pool
    }


def pool_spent_since(
    state_dir: Path, roster: dict[str, Any], pool: str, since: dt.datetime
) -> float:
    """Sum recorded estimated USD for a quota pool since `since`.

    A metered pool's cost is a models.dev-priced estimate, not authoritative billing;
    this is a safety rail. Lines are matched by recorded quota_pool when present, else
    by lane membership so pre-schema ledger lines still count.
    """
    path = state_dir / "runs.jsonl"
    if not path.exists():
        return 0.0
    lanes = pool_lane_ids(roster, pool)
    total = 0.0
    with ledger_lock(state_dir), path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
                observed = parse_iso(record.get("ended_at") or record.get("started_at"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            recorded_pool = record.get("quota_pool")
            if recorded_pool is not None:
                if recorded_pool != pool:
                    continue
            elif record.get("lane_id") not in lanes:
                continue
            if observed < since:
                continue
            total += float((record.get("cost") or {}).get("estimated_usd", 0) or 0)
    return total


def acquire_lease(
    state_dir: Path, roster: dict[str, Any], lane_id: str, ttl_s: int, *, pid: int | None = None
) -> str:
    owner = os.getppid() if pid is None else pid
    lanes = lane_map(roster)
    lane = lanes.get(lane_id)
    if lane_id.startswith("chatgpt:") and roster.get("chatgpt_catalog", {}).get("error"):
        raise FleetError("Crossfeed Chat gateway admission closed: " + roster["chatgpt_catalog"]["error"])
    if lane and lane.get("gateway_service") and (
            lane.get("admission_status") != "active" or lane.get("access_status") != "verified"):
        raise FleetError("Crossfeed Chat gateway admission closed: catalog selector is not currently admitted")
    if not lane:
        raise FleetError(f"unknown lane: {lane_id}")
    with locked_runtime(state_dir) as runtime:
        clean_leases(runtime)
        if chatgpt_pro.is_pro(lane):
            chatgpt_pro.refresh(roster, state_dir)
            reason = chatgpt_pro.blocked(roster, runtime, lane)
            if reason:
                raise FleetError(reason + "; use the Extra High or High ChatGPT lane")
        if lane.get("gateway_status", {}).get("quota_blocked") or lane.get("gateway_status", {}).get("rate_limited"):
            raise FleetError("Crossfeed Chat worker quota paused")
        if retired_on(roster, lane["model_key"]):
            raise FleetError(
                f"model {lane['model_key']} was retired by its provider on "
                f"{retired_on(roster, lane['model_key'])} and can no longer be run. Drop the lane to let the router pick"
            )
        if (pool_switches(roster, runtime, lane["quota_pool"]).get(lane["model_key"]) is False
                or model_preference(runtime, lane["model_key"]) == "off"):
            # A named lane is never silently swapped for another (opencode-agent.sh's rule), so a
            # lane of a model that is switched off is refused, not rerouted.
            raise FleetError(
                f"model {lane['model_key']} is switched off in the {PRODUCT_NAME} console. Drop the lane to "
                f"let the router pick, or switch it on (fleetctl.py model-toggle {lane['quota_pool']} "
                f"{lane['model_key']} on)"
            )
        active = [lease for lease in runtime["leases"] if lease["lane_id"] == lane_id]
        pool_state, pool_evidence = current_pool_state(runtime, lane["quota_pool"], roster=roster,
                                                      model_key=lane["model_key"])
        if pool_state == "EXHAUSTED":
            if pool_evidence.get("source") == "switched-off":
                raise FleetError(
                    f"quota pool {lane['quota_pool']} is switched off by hand "
                    f"(fleetctl.py level {lane['quota_pool']} normal)"
                )
            raise FleetError(
                f"quota pool {lane['quota_pool']} is exhausted until "
                f"{pool_evidence.get('until', 'its reset')}"
            )
        pool = lane["quota_pool"]
        level = pool_level(runtime, pool)
        if level == "low" and live_pool_leases(runtime, pool):
            raise FleetError(
                # Worded to match the wrappers' capacity wait ("active lease(s), cap is"), so
                # an opencode run waits for the slot instead of treating low as a hard failure.
                f"quota pool {pool} is set to low, one call at a time: "
                f"{len(live_pool_leases(runtime, pool))} active lease(s), cap is 1 across the pool"
            )
        daily_cap = roster.get("quota_pools", {}).get(pool, {}).get("daily_usd_cap")
        if daily_cap is not None:
            spent = pool_spent_since(state_dir, roster, pool, local_day_start())
            if spent >= float(daily_cap):
                raise FleetError(
                    f"quota pool {pool} daily spend cap ${float(daily_cap):.2f} reached "
                    f"(estimated ${spent:.4f} spent since local midnight); refusing until the cap "
                    f"resets. Check authoritative spend with the provider."
                )
        cap = effective_cap(lane, pool_state, pool_evidence, level)
        if len(active) >= cap:
            raise FleetError(f"lane {lane_id} has {len(active)} active lease(s), cap is {cap}")
        if pid is not None and os.getppid() != owner:
            raise FleetError("lease caller exited before acquisition")
        token = str(uuid.uuid4())
        runtime["leases"].append(
            {
                "token": token,
                "lane_id": lane_id,
                "pool": lane["quota_pool"],
                "pid": owner,
                "created_at": iso(),
                "expires_at": iso(utc_now() + dt.timedelta(seconds=ttl_s)),
            }
        )
        return token


class PoolBusy(FleetError):
    pass


def acquire_pool_slot(state_dir: Path, pool: str, pid: int, ttl_s: int) -> str | None:
    """At level low, hold the pool's single slot for the life of process `pid`.

    For wrappers whose calls are not router lanes (claude, codex, copilot): they never
    take a lane lease, so without this two of them could run at once on a low pool.
    Returns None, writing nothing, when the pool is not low. The slot is released when
    `pid` exits (leases already die with their recorded process); `ttl_s` only bounds a
    slot left by a process whose id was reused. Raises PoolBusy while another live run
    holds it.
    """
    if pool_level(load_json(state_dir / "runtime.json", {}) or {}, pool) != "low":
        return None
    with locked_runtime(state_dir) as runtime:
        if pool_level(runtime, pool) != "low":
            return None
        clean_leases(runtime)
        if live_pool_leases(runtime, pool):
            raise PoolBusy(f"pool {pool} is set to low and a run is going")
        token = str(uuid.uuid4())
        runtime["leases"].append({
            "token": token,
            "lane_id": None,
            "kind": "pool-slot",
            "pool": pool,
            "pid": pid,
            "created_at": iso(),
            "expires_at": iso(utc_now() + dt.timedelta(seconds=ttl_s)),
        })
        return token


def wait_for_pool_slot(state_dir: Path, pool: str, pid: int, ttl_s: int, wait_s: int) -> int:
    """The wrappers' gate: 0 to go, 5 to refuse after waiting `wait_s` for the running one."""
    deadline = time.monotonic() + max(0, wait_s)
    announced = False
    while True:
        try:
            acquire_pool_slot(state_dir, pool, pid, ttl_s)
            return 0
        except PoolBusy:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f"fleetctl: the {pool} pool is set to low (one run at a time) and a run was still going "
                      f"after {wait_s}s; retry when it ends, or lift it with `fleetctl.py level {pool} normal`",
                      file=sys.stderr)
                return 5
            if not announced:
                print(f"fleetctl: the {pool} pool is set to low (one run at a time); waiting up to {wait_s}s "
                      "for the running one to finish", file=sys.stderr)
                announced = True
            time.sleep(min(5.0, max(0.2, remaining)))


def release_lease(state_dir: Path, token: str) -> None:
    with locked_runtime(state_dir) as runtime:
        runtime["leases"] = [
            lease for lease in runtime.get("leases", []) if lease.get("token") != token
        ]


def clean_path_claims(runtime: dict[str, Any], now: dt.datetime | None = None) -> None:
    now = now or utc_now()
    runtime["path_claims"] = [
        claim
        for claim in runtime.get("path_claims", [])
        if parse_iso(claim["expires_at"]) > now and recorded_pid_is_alive(claim)
    ]


def acquire_path_claim(
    state_dir: Path, paths: list[str], owner: str, ttl_s: int
) -> str:
    """Claim exclusive write access to paths across campaigns AND sessions.

    Worktree isolation only covers git-tracked files; a git-ignored single-copy
    store shared by two concurrent writers still clobbers. Any overlap (same
    path, or one an ancestor of the other) with an active claim is a conflict,
    same owner included — two concurrent writers are the hazard regardless of
    who launched them. The TTL is a dead-process safety net; callers release.
    """
    wanted = sorted({os.path.realpath(os.path.expanduser(p)) for p in paths})
    if not wanted:
        raise FleetError("a path claim needs at least one path")
    with locked_runtime(state_dir) as runtime:
        clean_path_claims(runtime)
        for claim in runtime.get("path_claims", []):
            for held in claim["paths"]:
                for path in wanted:
                    common = os.path.commonpath([held, path])
                    if common in (held, path):
                        raise FleetError(
                            f"write conflict: {path} overlaps {held} held by "
                            f"{claim['owner']} until {claim['expires_at']}; "
                            "serialize or claim disjoint paths"
                        )
        token = str(uuid.uuid4())
        runtime.setdefault("path_claims", []).append(
            {
                "token": token,
                "owner": owner,
                "paths": wanted,
                "pid": os.getppid(),
                "created_at": iso(),
                "expires_at": iso(utc_now() + dt.timedelta(seconds=ttl_s)),
            }
        )
        return token


def release_path_claim(state_dir: Path, token: str) -> None:
    with locked_runtime(state_dir) as runtime:
        runtime["path_claims"] = [
            claim
            for claim in runtime.get("path_claims", [])
            if claim.get("token") != token
        ]


def list_path_claims(state_dir: Path) -> list[dict[str, Any]]:
    with locked_runtime(state_dir) as runtime:
        clean_path_claims(runtime)
        return list(runtime.get("path_claims", []))


def parse_events(path: Path | None) -> dict[str, Any]:
    totals = {
        "total": 0,
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    cost = 0.0
    session_id = None
    finish = None
    errors: list[dict[str, Any]] = []
    if path and path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            session_id = event.get("sessionID") or session_id
            if event.get("type") == "step_finish":
                part = event.get("part", {})
                finish = part.get("reason") or finish
                tokens = part.get("tokens", {})
                cache = tokens.get("cache", {})
                for key in ("total", "input", "output", "reasoning"):
                    totals[key] += int(tokens.get(key, 0) or 0)
                totals["cache_read"] += int(cache.get("read", 0) or 0)
                totals["cache_write"] += int(cache.get("write", 0) or 0)
                cost += float(part.get("cost", 0) or 0)
            if "error" in str(event.get("type", "")).lower() or event.get("error"):
                errors.append(event)
    return {
        "session_id": session_id,
        "finish": finish,
        "tokens": totals,
        "estimated_cost_usd": cost,
        "errors": errors,
    }


QUOTA_RE = re.compile(r"(?:usage limit|quota|GoUsageLimitError|HTTP\s*429|\b429\b)", re.I)
LIMIT_RE = re.compile(r"(?:limitName|limit name)[\"'=:\s]+(5 hour|weekly|monthly)", re.I)
RETRY_RE = re.compile(r"retry-after[\"'=:\s]+(\d+)", re.I)


def run_selection_metadata(selection=None, **actual):
    """Share the wrapper's prompt-free receipt validation with ledger writers."""
    spec = importlib.util.spec_from_file_location("fleet_run_identity", Path(__file__).with_name("run_identity.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.selection_metadata(selection, **actual)


def record_run(
    state_dir: Path,
    roster: dict[str, Any],
    lane_id: str,
    events: Path | None,
    stderr: Path | None,
    returncode: int,
    started_at: str | None,
    context_profile: str = "unspecified",
    agent_profile: str = "unspecified",
    execution_mode: str = "harness",
    identity: Path | None = None,
    effort: str | None = None,
    selection: dict[str, Any] | Path | None = None,
    role: str | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    lane = lane_map(roster).get(lane_id)
    if not lane:
        raise FleetError(f"unknown lane: {lane_id}")
    parsed = parse_events(events)
    stderr_text = ""
    if stderr and stderr.exists():
        stderr_text = stderr.read_text(encoding="utf-8", errors="replace")
    error_text = stderr_text + "\n" + json.dumps(parsed["errors"], ensure_ascii=False)
    quota_error = bool(QUOTA_RE.search(error_text))
    status = "ok" if returncode == 0 and parsed["finish"] == "stop" and not parsed["errors"] else "error"
    if returncode == 124:
        status = "timeout"
    record = {
        "schema": RUN_SCHEMA,
        "run_id": str(uuid.uuid4()),
        "session_id": parsed["session_id"],
        "provider": lane["provider"],
        "model": lane["model_key"],
        "lane_id": lane_id,
        "quota_pool": lane.get("quota_pool"),
        "context_profile": context_profile,
        "agent_profile": agent_profile,
        "execution_mode": execution_mode,
        "started_at": started_at or iso(),
        "ended_at": iso(),
        "status": status,
        "finish": parsed["finish"],
        "tokens": parsed["tokens"],
        "cost": {
            "estimated_usd": parsed["estimated_cost_usd"],
            "source": "opencode-local-model-pricing",
            "authoritative_for_go_quota": False,
        },
        "returncode": returncode,
    }
    shared = {}
    if identity:
        shared = load_json(identity, {}) or {}
        if shared.get("schema") == "crossfeed-model-run/v1":
            record["run_id"] = shared["run_id"]
            record["requested_model"] = shared.get("requested_model")
            record["selected_model"] = shared.get("selected_model")
    metadata = run_selection_metadata(
        selection if selection is not None else shared.get("selection"),
        model=lane["model_key"], lane_id=lane_id, pool=lane.get("quota_pool"),
        harness=lane.get("harness"), effort=effort or shared.get("effort"),
        role=role or shared.get("role"), family=family or shared.get("family"),
    )
    if shared.get("selection_error") and metadata.get("selection") is None:
        metadata["selection_error"] = shared["selection_error"]
    record.update(metadata)
    # The AFK controller assigns this opaque id before it invokes a worker.  It
    # links the worker's priced telemetry to its evidence-gated attempt without
    # putting the objective or prompt into the ledger.
    afk_attempt_id = os.environ.get("AFK_ATTEMPT_ID")
    if afk_attempt_id:
        record["afk_attempt_id"] = afk_attempt_id
    if quota_error:
        limit_match = LIMIT_RE.search(error_text)
        retry_match = RETRY_RE.search(error_text)
        retry_s = int(retry_match.group(1)) if retry_match else 1800
        limit_name = limit_match.group(1).lower() if limit_match else None
        record["error"] = {
            "type": "quota",
            "retry_after_seconds": retry_s,
            "limit_name": limit_name,
        }
        with locked_runtime(state_dir) as runtime:
            runtime.setdefault("pool_circuits", {})[lane["quota_pool"]] = {
                "opened_at": iso(),
                "until": iso(utc_now() + dt.timedelta(seconds=retry_s)),
                "limit_name": limit_name,
                "source": "explicit-run-error",
            }
    state_dir.mkdir(parents=True, exist_ok=True)
    with ledger_lock(state_dir):
        with (state_dir / "runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def append_ledger_record(state_dir: Path, record: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with ledger_lock(state_dir):
        with (state_dir / "runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def linked_attempt_cost(state_dir: Path, attempt_id: str) -> tuple[float, int]:
    """Return the local-priced worker records explicitly tied to one AFK attempt."""
    path = state_dir / "runs.jsonl"
    if not path.exists():
        return 0.0, 0
    total = 0.0
    matched = 0
    with ledger_lock(state_dir), path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("afk_attempt_id") != attempt_id or record.get("schema") == AFK_SCHEMA:
                continue
            total += float((record.get("cost") or {}).get("estimated_usd", 0) or 0)
            matched += 1
    return total, matched


def linked_attempt_metadata(state_dir: Path, attempt_id: str, lane_id: str) -> dict[str, Any]:
    """Actual worker metadata for this attempt only, never the previous route."""
    path = state_dir / "runs.jsonl"
    matches = []
    if path.exists():
        with ledger_lock(state_dir), path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (record.get("afk_attempt_id") == attempt_id and record.get("lane_id") == lane_id
                        and record.get("schema") != AFK_SCHEMA):
                    matches.append(record)
    result = {}
    for key in ("effort", "role", "family", "selection", "selection_error"):
        values = [record[key] for record in matches if record.get(key) is not None]
        if values and all(value == values[0] for value in values):
            result[key] = values[0]
        elif values:
            result["selection_error"] = "conflicting linked worker metadata"
    return result


def record_afk_attempt(
    state_dir: Path,
    roster: dict[str, Any],
    lane_id: str,
    attempt_id: str,
    started_at: str,
    duration_ms: int,
    result: str,
    failure_class: str | None,
    proof_output_hash: str,
    proof_returncode: int,
    worker_returncode: int,
    effort: str | None = None,
    selection: dict[str, Any] | Path | None = None,
    role: str | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    """Append one evidence-gated AFK attempt to the shared run ledger.

    This is deliberately a separate record from a worker's raw telemetry: a
    worker can finish cleanly while failing the caller's proof command.  The
    attempt record is the source for route ranking; raw worker records remain
    authoritative for their local token/cost estimate.
    """
    lane = lane_map(roster).get(lane_id)
    if not lane:
        raise FleetError(f"unknown lane: {lane_id}")
    if result not in {"verified", "failed"}:
        raise FleetError("AFK result must be verified or failed")
    if result == "verified" and (failure_class is not None or proof_returncode != 0):
        raise FleetError("a verified AFK attempt needs a passing proof and no failure class")
    if result == "failed" and not failure_class:
        raise FleetError("a failed AFK attempt needs a failure class")
    spend, linked_runs = linked_attempt_cost(state_dir, attempt_id)
    record = {
        "schema": AFK_SCHEMA,
        "run_id": str(uuid.uuid4()),
        "attempt_id": attempt_id,
        "attempt_kind": "afk",
        "route": lane_id,
        "lane_id": lane_id,
        "provider": lane["provider"],
        "model": lane["model_key"],
        "quota_pool": lane.get("quota_pool"),
        "started_at": started_at,
        "ended_at": iso(),
        "duration_ms": max(0, int(duration_ms)),
        "result": result,
        "failure_class": failure_class,
        "proof": {
            "returncode": proof_returncode,
            "output_sha256": proof_output_hash,
        },
        "worker_returncode": worker_returncode,
        "cost": {
            "estimated_usd": spend,
            "source": "linked-worker-ledger-records" if linked_runs else "no-linked-worker-record",
            "authoritative_for_go_quota": False,
        },
    }
    linked = linked_attempt_metadata(state_dir, attempt_id, lane_id)
    metadata = run_selection_metadata(
        selection if selection is not None else linked.get("selection"),
        model=lane["model_key"], lane_id=lane_id, pool=lane.get("quota_pool"), harness=lane.get("harness"),
        effort=linked.get("effort") or effort, role=role or linked.get("role"),
        family=family or (linked.get("family") if not role or role == "default" or role == linked.get("role") else None),
    )
    if linked.get("selection_error"):
        metadata["selection"] = None
        metadata["selection_error"] = linked["selection_error"]
    record.update(metadata)
    append_ledger_record(state_dir, record)
    return record


def rank_afk_routes(state_dir: Path) -> list[dict[str, Any]]:
    """Report route evidence only.  It never changes routing policy."""
    path = state_dir / "runs.jsonl"
    grouped: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return []
    with ledger_lock(state_dir), path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("schema") != AFK_SCHEMA:
                continue
            route = record.get("route")
            if not isinstance(route, str):
                continue
            bucket = grouped.setdefault(route, {"route": route, "attempts": 0, "verified": 0, "cost": 0.0, "duration_ms": 0})
            bucket["attempts"] += 1
            bucket["verified"] += int(record.get("result") == "verified")
            bucket["cost"] += float((record.get("cost") or {}).get("estimated_usd", 0) or 0)
            bucket["duration_ms"] += int(record.get("duration_ms", 0) or 0)
    ranked = []
    for bucket in grouped.values():
        attempts = bucket["attempts"]
        ranked.append(
            {
                "route": bucket["route"],
                "attempts": attempts,
                "verified": bucket["verified"],
                "verified_correctness": bucket["verified"] / attempts,
                "estimated_cost_usd": bucket["cost"],
                "mean_duration_ms": bucket["duration_ms"] / attempts,
            }
        )
    return sorted(
        ranked,
        key=lambda item: (-item["verified_correctness"], item["estimated_cost_usd"], item["mean_duration_ms"], item["route"]),
    )


def run_ledger_usage(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "runs.jsonl"
    if not path.exists():
        return {"available": False, "source": str(path), "windows": {}}
    now = utc_now()
    cutoffs = {
        "rolling_5h": now - dt.timedelta(hours=5),
        "weekly_7d": now - dt.timedelta(days=7),
        "monthly_30d": now - dt.timedelta(days=30),
    }
    windows: dict[str, dict[str, Any]] = {name: {} for name in cutoffs}
    with ledger_lock(state_dir), path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
                observed = parse_iso(record.get("ended_at") or record.get("started_at"))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if record.get("schema") == "crossfeed-model-run/v1":
                continue
            profile = record.get("agent_profile") or "unspecified"
            tokens = record.get("tokens") or {}
            estimated = float((record.get("cost") or {}).get("estimated_usd", 0) or 0)
            for window, cutoff in cutoffs.items():
                if observed < cutoff:
                    continue
                bucket = windows[window].setdefault(
                    profile,
                    {
                        "runs": 0,
                        "successful_runs": 0,
                        "tokens": {
                            "total": 0,
                            "input": 0,
                            "output": 0,
                            "reasoning": 0,
                            "cache_read": 0,
                            "cache_write": 0,
                        },
                        "estimated_cost_usd": 0.0,
                    },
                )
                bucket["runs"] += 1
                bucket["successful_runs"] += int(record.get("status") == "ok")
                for key in bucket["tokens"]:
                    bucket["tokens"][key] += int(tokens.get(key, 0) or 0)
                bucket["estimated_cost_usd"] += estimated
    return {"available": True, "source": str(path), "windows": windows}


def metered_pool_usage(state_dir: Path, roster: dict[str, Any] | None) -> dict[str, Any]:
    """Headroom for pools governed by a daily USD cap instead of a quota percentage.

    gemini-metered has no percentage window to fetch and never will: it is a
    per-token paid API key, and its real limiter is `acquire_lease` refusing the
    next call once the day's recorded estimated spend reaches `daily_usd_cap`.
    Reporting it as a pool with an UNKNOWN percentage said nothing and looked like
    a missing snapshot; what a reader actually needs is the cap, the spend against
    it, and the fact that the number is an estimate rather than Google's billing.
    """
    if not roster:
        return {}
    metered: dict[str, Any] = {}
    day_start = local_day_start()
    for pool, config in sorted((roster.get("quota_pools") or {}).items()):
        cap = config.get("daily_usd_cap")
        if cap is None:
            continue
        cap = float(cap)
        spent = pool_spent_since(state_dir, roster, pool, day_start)
        metered[pool] = {
            "governed_by": "daily_usd_cap",
            "has_percentage_quota": False,
            "daily_usd_cap": cap,
            "estimated_spent_usd_today": round(spent, 6),
            "estimated_remaining_usd": round(max(0.0, cap - spent), 6),
            "admission": "refused" if spent >= cap else "open",
            "window_started": iso(day_start),
            "cost_semantics": config.get(
                "cost_semantics", "estimated from list pricing; not authoritative billing"
            ),
        }
    return metered


def _atomic_json_ordered(path: Path, value: Any) -> None:
    """Like `atomic_json` but keeps key order and literal UTF-8.

    The overlay is hand-maintained: its ordering carries meaning, so the sorted
    writer would turn a one-key edit into a whole-file diff, and `ensure_ascii`
    would rewrite every accented character and dash in the prose already there.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


POLICY_DESCRIPTIONS = {
    "clock_aware": (
        "bands gate routing, except a window inside its spend-down horizon "
        "(last 10% of the window, max 1h), which is spent rather than protected"
    ),
    "strict": "bands gate routing on the raw observed bottleneck; the reset clock is ignored",
    "off": "no band gating at all; every pool routes quality_first",
}


def policy_command(args: argparse.Namespace, overlay_path: Path) -> None:
    """Show the active quota policy, or write a new machine default.

    Kept separate from `usage` on purpose: `usage` is telemetry a human and an
    agent both call constantly, and a read command that silently rewrites the
    user's config on first run is a surprise nobody asked for. `usage` reports
    the policy and where it came from, which is enough for a stranger to see it
    is unset; changing it is always a deliberate call to this command.
    """
    if args.set_policy:
        roster = load_json(overlay_path)
        if not isinstance(roster, dict):
            raise FleetError(f"access overlay is missing or invalid: {overlay_path}")
        roster.setdefault("routing", {})["quota_policy"] = args.set_policy
        # Write through a symlinked overlay, never over it: os.replace on the link path
        # would swap the link for a plain file and silently fork the operator's config.
        _atomic_json_ordered(overlay_path.resolve(), roster)
        resolve_quota_policy(refresh=True)

    try:
        roster = read_overlay(overlay_path)
    except (FleetError, OSError, json.JSONDecodeError):
        roster = None
    policy, source = resolve_quota_policy(roster)
    declared = _declared_policy(roster)
    payload = {
        "policy": policy,
        "source": source,
        "means": POLICY_DESCRIPTIONS[policy],
        "declared_in_overlay": declared,
        "available": list(QUOTA_POLICIES),
        "measurement_gated": False,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"quota policy: {policy} (from {source})")
    print(f"  {POLICY_DESCRIPTIONS[policy]}")
    if declared is None:
        print(f"  nothing declared in {overlay_path}; this is the built-in default")
        print(f"  set one with: fleetctl.py policy --set {DEFAULT_QUOTA_POLICY}")
    if source.startswith("env:"):
        print(f"  an environment variable is overriding the overlay for this run ({source})")
    print("  measurement is never gated: `usage` reports real percentages in every policy")



_CLI_VERSION = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


def _cli_version(value: Any, *, output: bool = False) -> tuple | None:
    """Order release and prerelease versions, ignoring build metadata."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not output and value.startswith(">="):
        value = value[2:].strip()
    match = (re.search(r"(?<![\w.+-])" + _CLI_VERSION.pattern + r"(?![\w.+-])", value)
             if output else _CLI_VERSION.fullmatch(value))
    if not match:
        return None
    prerelease = match.group(4)
    if prerelease and any(not part for part in prerelease.split(".")):
        return None
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part)
                        for part in prerelease.split(".")) if prerelease else ()
    return (int(match[1]), int(match[2]), int(match[3] or 0),
            0 if prerelease else 1, identifiers)


def cli_version_problems(roster: dict[str, Any]) -> list[str]:
    """Probe only harnesses with declared current-model minimums, once each."""
    issues = []
    requirements: dict[str, list[tuple[str, str, tuple]]] = {}
    for model, card in model_cards(roster).items():
        if card.get("status", "current") != "current" or "min_cli" not in card:
            continue
        minimums = card["min_cli"]
        if not isinstance(minimums, dict):
            issues.append(f"{model}: min_cli must map harness names to minimum versions")
            continue
        for harness, minimum in minimums.items():
            parsed = _cli_version(minimum)
            if not isinstance(harness, str) or not MODEL_ID_RE.fullmatch(harness) or not parsed:
                issues.append(f"{model}: invalid min_cli harness or minimum version")
                continue
            requirements.setdefault(harness, []).append((model, minimum, parsed))
    for harness, models in sorted(requirements.items()):
        binary = shutil.which(harness)
        if not binary:
            issues.append(f"{harness}: CLI missing, required by {', '.join(row[0] for row in models)}")
            continue
        try:
            run = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
            installed = _cli_version(run.stdout.strip() or run.stderr.strip(), output=True)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            issues.append(f"{harness}: CLI version probe failed")
            continue
        if run.returncode != 0 or installed is None:
            issues.append(f"{harness}: CLI version unavailable or unparseable")
            continue
        installed_text = ".".join(str(part) for part in installed[:3])
        if not installed[3]:
            installed_text += "-" + ".".join(str(part[1]) for part in installed[4])
        for model, minimum, required in models:
            if installed < required:
                issues.append(f"{harness} {installed_text}: {model} requires {harness} >= {minimum.removeprefix('>=').strip()}")
    return issues


def _load_jsonc(path: Path) -> Any:
    # Preserve quoted strings, including URLs and escaped quotes, while removing comments.
    quoted = r'"(?:\\.|[^"\\])*"'
    source = path.read_text(encoding="utf-8")
    source = re.sub(quoted + r"|//[^\n]*|/\*[\s\S]*?\*/",
                    lambda match: match[0] if match[0].startswith('"') else " ", source)
    source = re.sub(quoted + r"|,\s*(?=[}\]])",
                    lambda match: match[0] if match[0].startswith('"') else "", source)
    return json.loads(source)


def opencode_whitelist_problems(roster: dict[str, Any]) -> list[str]:
    """Check both runtime configs when they restrict the OpenCode Go picker."""
    admitted = {str(lane.get("selector") or lane.get("model_key") or "").removeprefix("opencode-go/")
                for lane in roster.get("lanes", [])
                if lane.get("harness") == "opencode" and lane.get("provider") == "opencode-go"
                and _lane_is_admitted(lane)}
    if not admitted:
        return []
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    paths = (("primary", Path(os.environ.get("FLEET_OPENCODE_CONFIG")
                              or config_home / "opencode/opencode.jsonc").expanduser()),
             ("fleet-worker", Path(os.environ.get("FLEET_OPENCODE_WORKER_CONFIG")
                                   or config_home / "opencode/fleet-worker/opencode.jsonc").expanduser()))
    issues = []
    for label, path in paths:
        if not path.exists():
            continue
        try:
            config = _load_jsonc(path)
            providers = config.get("provider", {})
            if not isinstance(providers, dict):
                raise ValueError("invalid providers")
            provider = providers.get("opencode-go", {})
            if not isinstance(provider, dict):
                raise ValueError("invalid provider")
            if "whitelist" not in provider:
                continue
            whitelist = provider["whitelist"]
            if not isinstance(whitelist, list) or any(not isinstance(value, str) or
                    not MODEL_ID_RE.fullmatch(value) for value in whitelist):
                raise ValueError("invalid whitelist")
        except (OSError, ValueError, TypeError, AttributeError):
            issues.append(f"OpenCode {label} config {path}: invalid JSONC or Go whitelist")
            continue
        missing = sorted(admitted - set(whitelist))
        stale = sorted(set(whitelist) - admitted)
        if missing:
            issues.append(f"OpenCode {label} whitelist {path}: missing admitted models {', '.join(missing)}")
        if stale:
            issues.append(f"OpenCode {label} whitelist {path}: stale models {', '.join(stale)}")
    return issues


def identity_drift_problems(roster: dict[str, Any], state_dir: Path,
                            now: dt.datetime | None = None) -> list[str]:
    """Use provider-reported receipt identity, never answer text or inferred identity."""
    path = state_dir / "runs.jsonl"
    if not path.exists():
        return []
    spec = importlib.util.spec_from_file_location("fleet_doctor_identity", Path(__file__).with_name("run_identity.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = now or utc_now()
    cutoff = now - dt.timedelta(days=7)
    issues = []
    try:
        with ledger_lock(state_dir), path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or record.get("schema") != "crossfeed-model-run/v1":
                        continue
                    moment = parse_iso(record.get("ended_at") or record.get("started_at"))
                    if moment.tzinfo is None or not cutoff <= moment <= now:
                        continue
                    actual, selected = record.get("actual_model"), record.get("selected_model")
                    if any(not isinstance(model, str) or not module.MODEL_ID.fullmatch(model)
                           for model in (actual, selected)):
                        continue
                    if not module.model_identity_drift(record, roster):
                        continue
                    run_id = record.get("run_id")
                    run_id = run_id if isinstance(run_id, str) and module.MODEL_ID.fullmatch(run_id) else "unnamed"
                    issues.append(f"run {run_id}: identity drift, selected {selected}, provider reported {actual}")
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        issues.append("model receipt ledger unreadable")
    return issues


def doctor_command(overlay_path: Path, state_dir: Path) -> int:
    """Report what this machine can actually do, and name what is missing.

    Written for a first run on an unfamiliar machine: the overlay is the hard
    part of setup, and the failure mode it prevents is a config that validates
    but routes to a CLI that is not installed or a pool nobody measures.
    """
    problems = 0
    print(f"overlay:   {overlay_path}")
    try:
        roster = read_overlay(overlay_path, state_dir)
    except (FleetError, OSError, json.JSONDecodeError) as exc:
        print(f"  BROKEN: {exc}")
        print("  fix: cp examples/access-overlay.example.json "
              f"{overlay_path}")
        return 1
    lanes = roster.get("lanes", [])
    print(f"  ok, {len(lanes)} lanes, {len(roster.get('quota_pools', {}))} pools")
    effort_issues = effort_problems(roster, state_dir)
    print("effort evidence:")
    for issue in effort_issues:
        print(f"  EFFORT   {issue}")
    if not effort_issues:
        print("  ok       all routed and direct models have current effort data")
    problems += len(effort_issues)
    print(f"state dir: {state_dir}")

    drift_issues = (cli_version_problems(roster) + opencode_whitelist_problems(roster)
                    + identity_drift_problems(roster, state_dir))
    print("adapter drift guards:")
    for issue in drift_issues:
        print(f"  FAIL     {issue}")
    if not drift_issues:
        print("  ok       no declared CLI, whitelist, or recent identity drift")
    problems += len(drift_issues)

    policy, source = resolve_quota_policy(roster)
    print(f"policy:    {policy} (from {source})")

    print("harnesses on PATH:")
    gateway_problem = False
    harnesses = sorted({lane.get("harness") for lane in lanes if lane.get("harness")})
    if "chatgpt_gateway" in roster or roster.get("chatgpt_catalog", {}).get("error"):
        try:
            from chatgpt_catalog import doctor
        except ImportError:
            from scripts.chatgpt_catalog import doctor
        catalog = roster.get("chatgpt_catalog", {})
        line, failed = doctor(roster.get("chatgpt_gateway"), catalog.get("states", {}), catalog.get("wake"))
        print("  " + line)
        gateway_problem = failed or bool(roster.get("chatgpt_catalog", {}).get("error"))
        problems += int(gateway_problem)
    binaries = {"opencode": "opencode", "codex": "codex", "claude": "claude",
                "agy": "agy", "copilot": "copilot"}
    for harness in harnesses:
        binary = binaries.get(harness, harness)
        used_by = sum(1 for lane in lanes if lane.get("harness") == harness)
        if harness == "openrouter":
            # Raw HTTP, no CLI: what it needs is its key file (openrouter-agent.sh reads it).
            key_file = Path(os.environ.get("OPENROUTER_KEY_FILE")
                            or Path.home() / ".config/orchestrator/openrouter_api_key")
            if key_file.is_file():
                print(f"  ok       {harness:10s} -> key file present, raw HTTP ({used_by} lanes)")
            else:
                problems += 1
                print(f"  MISSING  {harness:10s} -> no key file at {key_file} ({used_by} lanes unusable)")
            continue
        if harness == "chatgpt-chat":
            continue  # Gateway diagnostics above cover all saved-label lanes.
        found = shutil.which(binary)
        if found:
            print(f"  ok       {harness:10s} -> {found} ({used_by} lanes)")
        else:
            problems += 1
            print(f"  MISSING  {harness:10s} -> `{binary}` not on PATH ({used_by} lanes unusable)")

    print("quota sources:")
    sources = quota_sources(roster)
    for pool in sorted(roster.get("quota_pools", {})):
        if pool_has_no_known_limit(roster, pool):
            print(f"  ok       {pool:24s} no known limit, no quota oracle needed")
            continue
        cfg = (roster["quota_pools"][pool] or {}).get("quota_refresh")
        if pool not in sources or not cfg:
            if (roster["quota_pools"][pool] or {}).get("daily_usd_cap") is not None:
                print(f"  ok       {pool:24s} metered by a daily cap, no percentage quota")
                continue
            problems += 1
            print(f"  NONE     {pool:24s} unmeasured -> routes unthrottled, band is not real")
            continue
        oracle = cfg.get("oracle", "codexbar")
        # CodexBar is an optional third-party reader (macOS app, Linux CLI). Whether it is
        # installed is the question, not which platform this is.
        if oracle == "codexbar" and not shutil.which(CODEXBAR_BIN):
            problems += 1
            print(f"  MISSING  {pool:24s} oracle `codexbar` not on PATH "
                  "(optional third-party reader; or use a `command` oracle)")
        else:
            print(f"  ok       {pool:24s} oracle `{oracle}`")

    unverified = [lane["lane_id"] for lane in lanes
                  if lane.get("access_status") != "verified"]
    if unverified:
        print(f"unverified lanes ({len(unverified)}), refused until you confirm access:")
        for lane_id in unverified[:8]:
            print(f"  {lane_id}")

    print()
    if problems:
        print(f"{problems} thing(s) to fix. Missing effort refuses wrapper dispatch; "
              "unmeasured quota pools route at full quality.")
    else:
        print("No problems found.")
    return 1 if problems else 0


# --- The overview: levels, plans and quota in one structure -----------------------------
# One builder feeds both `brief` (what an agent reads at planning time) and the console
# page (what the operator reads), so the two can never disagree about the fleet.

CURRENCY_SIGN = {"USD": "$", "EUR": "€", "GBP": "£"}
BILLING_WORDS = {"subscription": "subscription", "per-token": "paid per token", "free": "free"}
BILLING_SHORT = {"subscription": "sub", "per-token": "paid per token", "free": "free"}


def window_label(name: str, window: dict[str, Any]) -> str:
    """`5h`, `7d`, `30d`: the window's length, which is what a reader needs to weigh a percentage."""
    minutes = window.get("window_minutes")
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0:
        minutes = int(minutes)
        if minutes % 1440 == 0:
            return f"{minutes // 1440}d"
        if minutes % 60 == 0:
            return f"{minutes // 60}h"
        return f"{minutes}m"
    lowered = name.lower()
    for needle, label in (("week", "7d"), ("month", "30d"), ("5h", "5h"), ("daily", "1d")):
        if needle in lowered:
            return label
    # An oracle's slot name ("primary", "secondary") says nothing about the window's length.
    return ""


# ---- every limit a plan has ----------------------------------------------------------------------
# Every limit the quota source can report is shown, not just the five-hour or the weekly one, with its reset,
# and agents must be fully aware of them too. One list per pool, built once here and read by the console page
# and by `brief`: every window the quota source reports (5-hour, weekly, monthly, a weekly that counts one
# model only), a window that has not started yet, and a paid pool's daily budget.
_DURATION_WORDS = {300: "5-hour", 1440: "Daily", 10080: "Weekly"}
_DURATION_IN_LABEL = re.compile(r"\b(5[- ]?h(?:ours?)?|session|weekly|week|monthly|month|daily|day)\b", re.I)
_SLOT_NAMES = {"primary", "secondary", "tertiary"}
# CodexBar lists a plan's renewal date among its rate windows (id "renewal", nothing used). It is
# a date, not a limit, so it is shown as one.
RENEWAL_WINDOW = "renewal"


def _duration_word(minutes: Any) -> str | None:
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
        return None
    minutes = int(minutes)
    if minutes in _DURATION_WORDS:
        return _DURATION_WORDS[minutes]
    if 40320 <= minutes <= 44640:   # 28 to 31 days
        return "Monthly"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute"


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\band\b", "", text.casefold()))


def limit_name(name: str, window: dict[str, Any], pool_label_text: str = "") -> tuple[str, str | None]:
    """("Weekly", "Fable only"): how long the limit's window is, and what it counts when that is
    narrower than the whole plan. The length comes from the window itself; the scope is the quota
    source's own label with the length words taken out, and is dropped when it only repeats the
    provider's name."""
    duration = _duration_word(window.get("window_minutes"))
    if not duration:
        short = window_label(name, window)
        duration = {"5h": "5-hour", "1d": "Daily", "7d": "Weekly", "30d": "Monthly"}.get(short)
    label = window.get("label")
    if isinstance(label, str) and label.strip():
        scope = " ".join(_DURATION_IN_LABEL.sub(" ", label).split()).strip(" -·:,")
        if _squash(scope) and _squash(scope) in _squash(pool_label_text):
            scope = ""
    else:
        scope = "" if name in _SLOT_NAMES else name   # an unlabelled extra limit keeps its id, never a guess
    if not duration:
        return (scope or "Limit"), None
    return duration, (scope or None)


def reset_words(reset_at: str | None, now: dt.datetime | None = None) -> str:
    """When a limit resets, as a person says it, in this machine's time: "today 13:39",
    "tomorrow 02:00", "Sat 3 Oct 19:16", and the date alone when it is more than a week away."""
    if not reset_at:
        return ""
    now = (now or utc_now()).astimezone()
    moment = parse_iso(reset_at).astimezone(now.tzinfo)
    # A reset one second before the hour reads as that hour: 23:59:59 is midnight, not 23:59.
    if moment.second >= 59:
        moment += dt.timedelta(seconds=1)
    moment = moment.replace(second=0, microsecond=0)
    clock = f"{moment:%H:%M}"
    days = (moment.date() - now.date()).days
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"tomorrow {clock}"
    date = f"{moment:%a} {moment.day} {moment:%b}"
    return f"{date} {clock}" if 0 < days <= 7 else date


def pool_limits(
    pool: dict[str, Any], windows: dict[str, Any], idle: dict[str, Any] | None,
    spend_down: Iterable[str], now: dt.datetime,
) -> tuple[list[dict[str, Any]], str | None]:
    """(limits, renews_at): every limit of one pool, shortest window first, and the plan's renewal date."""
    expiring = set(spend_down)
    limits: list[dict[str, Any]] = []
    renews_at = None
    for name, window in windows.items():
        if name == RENEWAL_WINDOW and not window.get("window_minutes"):
            renews_at = window.get("reset_at")
            continue
        title, scope = limit_name(name, window, pool["label"])
        used = int(window["used_percent"])
        limits.append({
            "name": name, "title": title, "scope": scope, "kind": "window",
            "minutes": window.get("window_minutes"),
            "used_percent": used,
            "state": "full" if used >= 100 else _state_for_used(used).lower(),
            "reset_at": window.get("reset_at"),
            "resets_in_s": int(window.get("seconds_to_reset", 0)),
            "resets": reset_words(window.get("reset_at"), now),
            "spend_down": name in expiring,
        })
    for name, window in (idle or {}).items():
        if name in windows:
            continue
        title, scope = limit_name(name, window, pool["label"])
        limits.append({
            "name": name, "title": title, "scope": scope, "kind": "window",
            "minutes": window.get("window_minutes"),
            "used_percent": int(window.get("used_percent", 0)),
            "state": _state_for_used(int(window.get("used_percent", 0))).lower(),
            "reset_at": None, "resets_in_s": None, "resets": "", "spend_down": False,
        })
    limits.sort(key=lambda item: (item["minutes"] is None, item["minutes"] or 0, bool(item["scope"]), item["name"]))
    metered = pool.get("metered")
    if metered:
        cap, spent = metered["daily_usd_cap"], metered["estimated_spent_usd_today"]
        midnight = local_day_start(now) + dt.timedelta(days=1)
        limits.append({
            "name": "daily_usd_cap", "title": "Daily budget", "scope": None, "kind": "budget",
            "minutes": 1440, "spent_usd": spent, "cap_usd": cap,
            "used_percent": 0 if not cap else max(0, min(100, int(round(100 * spent / cap)))),
            "state": "full" if metered.get("admission") == "refused" else "abundant",
            "reset_at": iso(midnight), "resets_in_s": max(0, int((midnight - now).total_seconds())),
            "resets": "midnight", "spend_down": False,
        })
    return limits, renews_at


def pool_plan(config: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """The pool's plan, minimal: name, monthly price, billing kind, allowance estimate.

    Declared in the overlay as `quota_pools.<pool>.plan`. The name falls back to the
    label the quota oracle reports (CodexBar knows "Claude Max 20x"), and a pool with a
    daily USD cap is pay-per-token by construction. Anything unknown stays None rather
    than being guessed: a wrong price is worse than a missing one.
    """
    declared = config.get("plan") if isinstance(config.get("plan"), dict) else {}
    billing = declared.get("billing")
    if billing not in BILLING_WORDS:
        billing = "per-token" if config.get("daily_usd_cap") is not None else None
    price = declared.get("price")
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price < 0:
        price = None
    currency = str(declared.get("currency") or "USD").upper()
    allowance = declared.get("allowance")
    return {
        "name": declared.get("name") or (snapshot or {}).get("plan"),
        "name_source": "overlay" if declared.get("name") else ("oracle" if (snapshot or {}).get("plan") else None),
        "price": price,
        "currency": currency,
        "billing": billing,
        "allowance": allowance if isinstance(allowance, str) and allowance.strip() else None,
        "as_of": declared.get("as_of"),
        "limit": declared.get("limit"),
    }


def format_price(plan: dict[str, Any]) -> str | None:
    if plan.get("billing") == "free" or plan.get("price") == 0:
        return "free"
    if plan.get("price") is None:
        return None
    sign = CURRENCY_SIGN.get(plan["currency"])
    amount = f"{plan['price']:g}"
    return f"{sign}{amount}" if sign else f"{amount} {plan['currency']}"


def pool_label(pool: str, config: dict[str, Any]) -> str:
    label = config.get("label")
    label = label.strip() if isinstance(label, str) and label.strip() else pool
    if label in {"ChatGPT Chat via Crossfeed Chat", "ChatGPTChat viaCrossfeedChat"}:
        return "ChatGPT chats via Crossfeed Chat"
    return label


def _without_levels(runtime: dict[str, Any]) -> dict[str, Any]:
    """The runtime as the gauges see it, so a forced or switched-off pool still shows its numbers."""
    stripped = dict(runtime)
    stripped["switches"] = {}
    return stripped


def _model_pool(roster: dict[str, Any], item: dict[str, Any], pools: set[str]) -> tuple[str | None, bool]:
    """Display ownership from sibling lanes; a harness-only fallback never admits a model."""
    if item.get("quota_pool") in pools:
        return item["quota_pool"], True
    provider = item.get("provider") or item.get("selector", "").split("/")[0]
    harness = item.get("harness") or provider
    siblings = [lane for lane in roster.get("lanes", []) if lane.get("quota_pool") in pools
                and (not item.get("harness") or lane.get("harness") == harness)
                and ((provider and lane.get("provider") == provider)
                     or (harness and lane.get("harness") == harness))]
    if provider in pools:
        return provider, True
    # Namespace identifies a serving family (Gemini vs Claude), not its version.
    namespace = re.split(r"[-\d]", item.get("model_key", ""), maxsplit=1)[0]
    matching = [lane for lane in siblings if lane["model_key"] == item.get("model_key")]
    if not matching and namespace:
        matching = [lane for lane in siblings if lane["model_key"].startswith(namespace + "-")]
    candidates = {lane["quota_pool"] for lane in matching or siblings}
    if len(candidates) == 1:
        reached = bool(matching or any(provider and lane.get("provider") == provider for lane in siblings))
        return next(iter(candidates)), reached
    # Multiple pools on one harness: use the provider's named model family if
    # available, then a stable fallback. Catalogue/evidence entries stay archived.
    candidates = candidates or {pool for pool in pools if pool.startswith(harness)} or pools
    if not candidates:
        return None, False
    configs = roster.get("quota_pools") or {}
    pool = min(candidates, key=lambda p: (not (namespace and namespace.casefold() in
                                              pool_label(p, configs.get(p, {})).casefold()), p))
    return pool, False


def model_overview(roster: dict[str, Any], runtime: dict[str, Any],
                   visible_pools: set[str] | None = None) -> list[dict[str, Any]]:
    """One entry per model, including retired, catalogue-only and evidence-only models."""
    models: dict[str, dict[str, Any]] = {}
    if visible_pools is None:
        visible_pools = {key for key, config in (roster.get("quota_pools") or {}).items()
                         if config.get("plan") or any(lane.get("quota_pool") == key
                                                     for lane in roster.get("lanes", []))}
    evidence = roster.get("model_evidence") or {}
    roles = roster.get("routing", {}).get("roles", {})
    provider_names = {"antigravity": "Antigravity", "agy": "Antigravity", "github-copilot": "GitHub Copilot",
                      "google": "Google", "openrouter": "OpenRouter"}
    provider_names.update({key: pool_label(key, config) for key, config in (roster.get("quota_pools") or {}).items()})
    entries = [(lane, True) for lane in roster.get("lanes", [])]
    entries += [(lane, False) for lane in roster.get("catalogue_only", [])]
    for lane, routing_lane in entries:
        key = lane["model_key"]
        model = models.setdefault(key, {"model": key, "providers": [], "pools": [], "current_pools": [], "routing_rank": {}, "lanes": [], "evidence": evidence.get(key, {})})
        provider = lane.get("provider") or lane.get("selector", "Other roster models").split("/")[0]
        provider = provider_names.get(provider, provider)
        if provider not in model["providers"]:
            model["providers"].append(provider)
        ranks = []
        rank_numbers = []
        for role, bands in roles.items():
            for band, ranking in bands.items():
                if isinstance(ranking, list) and lane.get("lane_id") in ranking:
                    rank_numbers.append(ranking.index(lane["lane_id"]) + 1)
                    ranks.append(f"{role}: {band.replace('_', ' ')} #{ranking.index(lane['lane_id']) + 1}")
        pool, reached = _model_pool(roster, lane, visible_pools)
        if pool and routing_lane and lane.get("lane_id"):
            # {pool: {role: best quality-first rank}}: the plain "picked first for" line reads it.
            places = model.setdefault("role_ranks", {}).setdefault(pool, {})
            for role, bands in roles.items():
                ranking = bands.get("quality_first") if isinstance(bands, dict) else None
                if isinstance(ranking, list) and lane["lane_id"] in ranking:
                    rank = ranking.index(lane["lane_id"]) + 1
                    places[role] = min(places.get(role, rank), rank)
        if pool and pool not in model["pools"]:
            model["pools"].append(pool)
        if (pool and reached and routing_lane and lane.get("lane_id")
                and lane.get("admission_status") == "active" and lane.get("access_status") == "verified"
                and lane.get("allowed_modes")):
            if pool not in model["current_pools"]:
                model["current_pools"].append(pool)
            if rank_numbers:
                best_rank = min(rank_numbers)
                model["routing_rank"][pool] = min(model["routing_rank"].get(pool, best_rank), best_rank)
        # Explicit display allowlist: no paths, selectors, commands or runtime plumbing.
        fields = {name: lane[name] for name in (
            "admission_status", "access_status", "quality_tier", "evidence_confidence",
            "context_window", "context_length", "cost_class", "cost_rank", "roles",
            "capabilities", "allowed_modes", "notes", "reason", "verified_at",
            "catalog_state", "worker_row", "worker_level", "harness", "max_parallel",
        ) if name in lane}
        if not routing_lane or not lane.get("lane_id"):
            fields["admission_status"] = "catalogue only; no routing lane"
        fields["provider"] = provider
        fields["rankings"] = ranks or ["Not ranked for a routing role"]
        model["lanes"].append(fields)
    for key, item in evidence.items():
        # Evidence-only entries have no lane to supply a pool. Prefer declared
        # ownership, then a known model namespace for display only, never admission.
        ownership = dict(item, model_key=key)
        if re.match(r"gpt-\d", key) and not item.get("quota_pool") and not item.get("provider"):
            ownership["quota_pool"] = "codex"
        pool, _ = _model_pool(roster, ownership, visible_pools)
        models.setdefault(key, {"model": key,
            "providers": [provider_names.get(pool, "Other roster models")],
            "pools": [pool] if pool else [], "current_pools": [], "routing_rank": {}, "lanes": [], "evidence": item})
    cards = model_cards(roster)
    for key, card in cards.items():
        # A card adds display membership even when another provider's lane or evidence
        # already introduced this model. It never admits a routing lane: current_pools
        # stays the router's own word.
        pool = card.get("pool")
        if pool in visible_pools:
            model = models.setdefault(key, {"model": key, "providers": [], "pools": [],
                                           "current_pools": [], "routing_rank": {}, "lanes": [], "evidence": {}})
            if pool not in model["pools"]:
                model["pools"].append(pool)
            provider = provider_names.get(pool, pool)
            if provider not in model["providers"]:
                model["providers"].append(provider)
    for model in models.values():
        model["preference"] = model_preference(runtime, model["model"])
        model["provider"] = " / ".join(model["providers"])
        model["card"] = cards.get(model["model"], {})
    return sorted(models.values(), key=lambda model: model["provider"].casefold())


def usual_model(roster: dict[str, Any], runtime: dict[str, Any], pool: str) -> str | None:
    """The model that runs on this pool when nothing more specific is asked of it.

    A direct pool: the model its wrapper runs when a dispatch names none, or the nearest model
    that is on when that one is off. A routed pool: the first admitted lane of this pool with a
    model that is on in the default role's list for the band routing is in now, else in any
    role's quality-first list, else the pool's first admitted lane.
    """
    switches = pool_switches(roster, runtime, pool)
    if pool_is_direct(roster, pool):
        default = direct_default_model(pool)
        on = [model for model, value in switches.items() if value]
        wanted = resolve_model(roster, pool, default)
        stand_ins = [model for model in on if model in current_models(roster, pool)]
        if not stand_ins or wanted is None or wanted in on:
            return default
        return nearest_on(model_order(roster, pool), wanted, stand_ins)
    lanes = lane_map(roster)
    roles = roster.get("routing", {}).get("roles", {})
    state, evidence = current_pool_state(runtime, ROUTING_POOL, roster=roster)
    band = level_band(pool_level(runtime, ROUTING_POOL), task_band(state, evidence))
    default = roles.get("default") or {}
    lists = [default.get(band) or default.get("quality_first") or []]
    lists += [bands.get("quality_first") or [] for bands in roles.values()]
    lists.append([lane["lane_id"] for lane in roster.get("lanes", []) if lane.get("lane_id")])
    for ranking in lists:
        for lane_id in ranking:
            lane = lanes.get(lane_id)
            if (lane and lane.get("quota_pool") == pool and _lane_is_admitted(lane)
                    and switches.get(lane["model_key"], True)
                    and model_preference(runtime, lane["model_key"]) != "off"):
                return lane["model_key"]
    return None


def pool_model_options(roster: dict[str, Any], runtime: dict[str, Any], pool: str,
                       models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every model shown under a provider, in list order: current first, then older.

    `current` is what the provider's own app would list at the top: a model the pool can run
    (for a routed pool, an admitted lane) that nothing newer of its own line has replaced
    (model_is_older). `run_as` is set exactly when the pool can run the model, `on` says whether
    its switch is on (a model the pool cannot run has no switch), and `superseded_by` names the
    model that replaced an older one, when the roster or the version rule knows it.
    """
    choosable = choosable_models(roster, pool)
    switches = pool_switches(roster, runtime, pool)
    direct = pool_is_direct(roster, pool)
    options = []
    for model in models:
        if pool not in model["pools"]:
            continue
        card = model.get("card") or {}
        if card.get("hidden"):
            continue
        older = model_is_older(roster, pool, model["model"])
        if direct:
            current = not older
        else:
            current = (pool in model["current_pools"] or bool(choosable.get(model["model"]))) and not older
        options.append({"model": model["model"], "current": bool(current),
                        "run_as": choosable.get(model["model"]),
                        "on": switches.get(model["model"]),
                        "superseded_by": superseded_by(roster, pool, model["model"]),
                        "retired_on": retired_on(roster, model["model"]),
                        "retention_reason": older_model_reason(roster, pool, model["model"]) if older else None,
                        "order": card.get("order", model["routing_rank"].get(pool, float("inf")))})
    options.sort(key=lambda o: (not o["current"], o["order"], o["model"].casefold()))
    for option in options:
        option.pop("order")
    return options


def recent_model_runs(state_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    path = state_dir / "runs.jsonl"
    if not path.exists():
        return []
    from collections import deque
    rows: Any = deque(maxlen=limit)
    with ledger_lock(state_dir), path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("schema") == "crossfeed-model-run/v1":
                rows.append(record)
    return list(reversed(rows))


def fleet_overview(
    roster: dict[str, Any], runtime: dict[str, Any], state_dir: Path, now: dt.datetime | None = None
) -> dict[str, Any]:
    now = now or utc_now()
    chatgpt_pro.refresh(roster, state_dir, now=now)
    pools_config = roster.get("quota_pools") or {}
    lane_counts: dict[str, int] = {}
    for lane in roster.get("lanes", []):
        if lane.get("admission_status") == "active" and lane.get("quota_pool"):
            lane_counts[lane["quota_pool"]] = lane_counts.get(lane["quota_pool"], 0) + 1
    snapshots = runtime.get("quota_snapshots") or {}
    metered = metered_pool_usage(state_dir, roster)
    measured_runtime = _without_levels(runtime)
    pools: list[dict[str, Any]] = []
    for pool, config in pools_config.items():
        config = config if isinstance(config, dict) else {}
        no_known_limit = pool_has_no_known_limit(roster, pool)
        snapshot = None if no_known_limit else snapshots.get(pool)
        has_source = not no_known_limit and isinstance(config.get("quota_refresh"), dict)
        relevant = (
            lane_counts.get(pool) or has_source or snapshot or isinstance(config.get("plan"), dict)
            or config.get("daily_usd_cap") is not None
        )
        if not relevant:
            continue
        state, evidence = current_pool_state(measured_runtime, pool, now, roster=roster)
        windows = evidence.get("windows") or {}
        applicable = {name: window for name, window in windows.items()
                      if name in evidence.get("applicable_windows", windows)}
        quota = None
        if applicable:
            name, window = max(
                applicable.items(),
                key=lambda item: (int(item[1]["used_percent"]), -int(item[1].get("seconds_to_reset", 0))),
            )
            quota = {
                "used_percent": int(window["used_percent"]),
                "window": window_label(name, window),
                "resets_in_s": int(window.get("seconds_to_reset", 0)),
                "spend_down": bool(evidence.get("spend_down")),
                "age_s": evidence.get("age_seconds"),
                "confidence": evidence.get("confidence"),
                "source": (snapshot or {}).get("source"),
            }
        level = pool_level(runtime, pool)
        entry = {
            "pool": pool,
            "label": pool_label(pool, config),
            "level": level,
            "means": LEVEL_MEANING[level],
            "state": state,
            "until": evidence.get("until") if state == "EXHAUSTED" else None,
            "quota": quota,
            "binding_used_percent": evidence.get("binding_used_percent"),
            "routing_state": evidence.get("routing_state", state),
            "applicable_windows": evidence.get("applicable_windows", []),
            # A reading that exists but is too old to route on: said as such, never as "none".
            "stale_age_s": evidence.get("age_seconds") if snapshot and not quota and state == "UNKNOWN" else None,
            "measured": has_source,
            "plan": pool_plan(config, snapshot),
            "metered": metered.get(pool),
            "pro_usage": roster.get("chatgpt_pro_usage", {}).get(pool),
            "lanes": lane_counts.get(pool, 0),
        }
        entry["limits"], entry["renews_at"] = pool_limits(
            entry, windows, (snapshot or {}).get("idle_windows") if windows else None,
            evidence.get("spend_down") or [], now)
        pools.append(entry)
    ages = [p["quota"]["age_s"] for p in pools if p["quota"] and p["quota"].get("age_s") is not None]
    sources = sorted({p["quota"]["source"] for p in pools if p["quota"] and p["quota"].get("source")})
    models = model_overview(roster, runtime, {pool["pool"] for pool in pools})
    try:
        pin_rows = sync_pins(roster, state_dir, write=False)
    except OSError:
        pin_rows = []
    for entry in pools:
        pool = entry["pool"]
        stored = model_choice(runtime, pool)
        switches = pool_switches(roster, runtime, pool)
        current = current_models(roster, pool)
        entry["direct"] = pool_is_direct(roster, pool)
        entry["options"] = pool_model_options(roster, runtime, pool, models)
        entry["models"] = {
            "state": models_state(switches, current),
            "current": current,
            "on": [model for model, value in switches.items() if value],
            "off": [model for model, value in switches.items() if not value],
            # an older single choice the pool no longer offers: it changes nothing, and is said so
            "unavailable": stored if stored and stored not in switches else None,
            # {model that is off or retired: what a run that asks for it gets instead}, for a direct pool
            "stand_ins": stand_ins(roster, runtime, pool),
            "retired": pool_retired(roster, pool),
        }
        entry["pins"] = [row for row in pin_rows if row["pool"] == pool]
        entry["usual"] = usual_model(roster, runtime, pool)
    return {
        "product": PRODUCT_NAME,
        "generated_at": iso(now),
        "quota_readings": {"sources": sources, "newest_age_s": min(ages) if ages else None,
                           "measured_pools": sum(1 for p in pools if p["quota"])},
        "levels": list(LEVELS),
        "level_meaning": LEVEL_MEANING,
        "pools": sorted(pools, key=lambda pool: pool["label"].casefold()),
        "models": models,
        "effort_problems": effort_problems(roster, state_dir, now),
        "recent_runs": recent_model_runs(state_dir),
    }


def _brief_entry(pool: dict[str, Any]) -> str:
    parts = [pool["pool"]]
    quota = pool.get("quota")
    metered = pool.get("metered")
    if pool["state"] == "EXHAUSTED" and pool.get("until"):
        parts.append(f"EXHAUSTED until {pool['until'][:16].replace('T', ' ')}Z")
    elif pool["plan"].get("limit") == "none-known":
        parts.append("no known limit")
    elif quota:
        text = f"{quota['used_percent']}%" + (f"/{quota['window']}" if quota["window"] else "")
        if pool["state"] in {"CONSERVE", "CRITICAL"}:
            text += f" {pool['state']}"
        if quota.get("spend_down"):
            text += f" use-it: resets {format_duration(quota['resets_in_s'])}"
        parts.append(text)
    elif metered:
        parts.append(f"${metered['estimated_spent_usd_today']:.2f}/${metered['daily_usd_cap']:.2f} today")
    elif pool.get("stale_age_s") is not None:
        parts.append(f"quota stale ({format_duration(pool['stale_age_s'])} old)")
    elif pool.get("measured"):
        parts.append("quota ?")
    plan = pool["plan"]
    words = [plan["name"]] if plan.get("name") else []
    price = format_price(plan)
    if price == "free":
        words.append("free")
    elif price:
        words.append(f"{price}/mo" if plan.get("billing") == "subscription" else price)
        if plan.get("billing") == "per-token":
            words.append(BILLING_SHORT["per-token"])
    elif plan.get("billing") in {"per-token", "free"}:
        words.append(BILLING_SHORT[plan["billing"]])
    if plan.get("allowance"):
        words.append(f"≈{plan['allowance']}")
    return " ".join(parts + words)


def _limit_text(limit: dict[str, Any]) -> str:
    """One limit as an agent reads it: "weekly Fable only 0%, resets Tue 6 Oct 15:00 (in 6d 5h)"."""
    if limit["kind"] == "budget":
        name = "daily budget"
    else:   # "weekly", "5-hour", and a source's own word for a limit ("Premium") as it writes it
        name = limit["title"].lower() if limit["title"] in {"Weekly", "Monthly", "Daily", "Limit"} else limit["title"]
    if limit.get("scope"):
        name += f" {limit['scope']}"
    if limit["kind"] == "budget":
        text = f"{name} ${limit['spent_usd']:.2f} of ${limit['cap_usd']:.2f}"
        if limit["state"] == "full":
            text += " SPENT"
        return text + ", resets at midnight"
    text = f"{name} {limit['used_percent']}%"
    if limit["state"] in {"conserve", "critical", "full"}:
        text += f" {limit['state'].upper()}"
    if limit["reset_at"] is None:
        return text + ", not started"
    text += f", resets {limit['resets']} (in {format_duration(limit['resets_in_s'])})"
    if limit.get("spend_down"):
        text += " use-it"
    return text


def _models_off_text(pool: dict[str, Any]) -> str | None:
    models = pool.get("models") or {}
    if models.get("state") == "none":
        return f"{pool['pool']} every model (provider off)"
    retired = models.get("retired") or {}
    if models.get("off") or retired:
        stand = models.get("stand_ins") or {}
        names = [*models.get("off", []), *[model for model in retired if model not in models.get("off", [])]]
        if pool.get("direct"):
            return f"{pool['pool']} " + ", ".join(
                model + (" retired" if model in retired else "")
                + (f" ({stand[model]} runs instead)" if stand.get(model) else "") for model in names)
        return f"{pool['pool']} " + ", ".join(
            model + (" retired" if model in retired else "") for model in names) + " (the router skips them)"
    if models.get("unavailable"):
        return f"{pool['pool']} chose {models['unavailable']}, no longer offered: all on"
    return None


def _pins_text(overview: dict[str, Any]) -> list[str]:
    """What an agent must know about model names written outside Crossfeed, at most two lines."""
    rows = [row for pool in overview["pools"] for row in pool.get("pins") or []]
    lines = []
    moved = [row for row in rows if row["action"] == "held"]
    if moved:
        lines.append("Seats moved with the switches: " + ", ".join(
            f"{row['pool']} {row['file']} runs {row['value']} (was {row['original']})" for row in moved))
    leaks = [row for row in rows if row.get("blocked")]
    if leaks:
        lines.append("Not closed, run only through the wrappers: " + ", ".join(
            f"{row['path']} still names {row['value']}" for row in leaks))
    return lines


def _render_verbose_brief(overview: dict[str, Any]) -> str:
    """What an agent reads before it plans: a header, one line per spend level, every limit of
    every measured plan with when it resets, and the models that are switched off with what runs
    in their place."""
    readings = overview["quota_readings"]
    stamp = parse_iso(overview["generated_at"]).astimezone().strftime("%H:%M")
    if readings["measured_pools"]:
        age = readings["newest_age_s"]
        age_s = f", newest {format_duration(age)} old" if age is not None else ""
        header = (f"{overview['product']} brief {stamp} · quota from "
                  f"{', '.join(readings['sources']) or 'snapshots'}{age_s} · ≈ is an estimate")
    else:
        header = (f"{overview['product']} brief {stamp} · no quota readings (CodexBar absent or signed out): "
                  "routing runs at full quality")
    lines = [header]
    for level in reversed(LEVELS):
        members = [p for p in overview["pools"] if p["level"] == level]
        if not members:
            continue
        entries = " | ".join(_brief_entry(p) for p in members)
        lines.append(f"{level.upper()} ({LEVEL_BRIEF[level]}): {entries}")
    limited = [p for p in overview["pools"] if p.get("limits")]
    if limited:
        lines.append("Limits (used, then when each resets; times are this machine's):")
        for pool in limited:
            text = " · ".join(_limit_text(limit) for limit in pool["limits"])
            if pool.get("renews_at"):
                text += f" · plan renews {reset_words(pool['renews_at'], parse_iso(overview['generated_at']))}"
            lines.append(f"  {pool['pool']}: {text}")
    parts = [text for text in (_models_off_text(pool) for pool in overview["pools"]) if text]
    if parts:
        lines.append("Switched off: " + " | ".join(parts))
        lines.append("Never name a switched-off model in a command or a report: name the one that runs.")
    lines.extend(_pins_text(overview))
    if overview.get("effort_problems"):
        lines.append("Effort evidence needs recheck: " + " | ".join(overview["effort_problems"]))
    lines.append("Pick with: fleetctl.py select --role R")
    return "\n".join(lines)


def _compact_window_label(name: str, window: dict[str, Any]) -> str:
    if name == "daily_usd_cap" or "$" in str(window.get("label", "")):
        return "daily $"
    duration = _duration_word(window.get("window_minutes"))
    if duration in {"Weekly", "Monthly", "Daily"}:
        return duration.lower()
    own_label = str(window.get("label") or "")
    label = window_label(name, window) or window_label(own_label, {})
    if not label:
        match = re.fullmatch(r"(\d+)\s*[- ]?\s*(h|hours?|d|days?|m|minutes?)", own_label or name, re.I)
        if match:
            label = match[1] + match[2][0].lower()
    return {"7d": "weekly", "30d": "monthly", "1d": "daily"}.get(label, label) or "-"


def render_brief(
    overview: dict[str, Any], verbose: bool = False, *,
    roster: dict[str, Any] | None = None, runtime: dict[str, Any] | None = None,
    lead: str | None = None,
) -> str:
    """A compact pool table, or the complete legacy brief when verbose is true.

    Roster/runtime are render-only context: prices and binding windows come from
    the selector's algorithm without changing the fleet overview JSON contract.
    Without that context, show the observed quota and leave the price unknown.
    """
    directive = lead_directive(lead_pressure(runtime, lead, roster)) if runtime is not None else None
    workers = [model for model in overview.get("models", [])
               if any(lane.get("catalog_state") for lane in model.get("lanes", []))]
    older_workers = {option["model"] for pool in overview.get("pools", [])
                     for option in pool.get("options", []) if not option.get("current", True)}
    worker_lines = []
    for older, heading in ((False, "ChatGPT workers: "), (True, "Older ChatGPT workers: ")):
        group = [model for model in workers if (model["model"] in older_workers) == older]
        if group:
            worker_lines.append(heading + "; ".join(
                str(model.get("card", {}).get("name") or model["model"]) + " (" +
                ", ".join(sorted({lane["catalog_state"] for lane in model["lanes"] if lane.get("catalog_state")})) + ")"
                for model in group))
    worker_line = "\n".join(worker_lines)
    pro_lines = [chatgpt_pro.text(pool["pro_usage"]) for pool in overview["pools"] if pool.get("pro_usage")]
    if pro_lines:
        worker_line += ("\n" if worker_line else "") + "\n".join(pro_lines)
    if verbose:
        text = _render_verbose_brief(overview)
        return text + ("\n" + worker_line if worker_line else "") + ("\n" + directive if directive else "")
    selector = None
    if roster is not None and runtime is not None:
        spec = importlib.util.spec_from_file_location("fleet_selector", Path(__file__).with_name("selector.py"))
        selector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selector)
    lines = ["pool | level | binding window | used | resets | price | models on"]
    off = []
    for pool in overview["pools"]:
        quota = pool.get("quota") or {}
        window = _compact_window_label(quota.get("window") or "", {})
        used = f"{quota['used_percent']}%" if quota else "-"
        resets = format_duration(quota["resets_in_s"]) if quota else "-"
        price = "-"
        if selector is not None:
            fleet = types.SimpleNamespace(**globals())
            policy = roster.get("policy", {}).get("selector", {})
            binding = selector._pool_price(roster, runtime, pool["pool"], policy, fleet)
            price = f"{binding['lambda']:.2f}"
            if binding["projection_unknown"]:
                price = "~" + price
            name = binding["binding_window"]
            raw = binding["window"]
            window = _compact_window_label(name or "", raw)
            used = f"{raw['used_percent']}%" if raw else "-"
            resets = format_duration(raw["seconds_to_reset"]) if raw else "-"
        elif pool.get("stale_age_s") is not None:
            used = "stale"
        models = pool.get("models") or {}
        on = models.get("on") or []
        current = set(models.get("current", on))
        older_count = sum(model not in current for model in on)
        models_text = ", ".join(model for model in on if model in current) or "-"
        if older_count:
            models_text += f" (+{older_count} older)"
        if window == "-" and pool.get("metered"):
            window = "daily $"
        if pool["plan"].get("limit") == "none-known":
            window, used, resets, price = "no known limit", "-", "-", "0.00"
        lines.append(" | ".join([pool["pool"], pool["level"], window, used, resets,
                                 price, models_text]))
        retired = models.get("retired") or {}
        names = list(dict.fromkeys([*models.get("off", []), *retired]))
        stand = models.get("stand_ins") or {}
        for model in names:
            # Direct wrappers have a deterministic substitute. Routed lanes are
            # selected for the requested role, so do not invent a replacement.
            replacement = stand.get(model) or ("refused" if pool.get("direct") else "select by role")
            off.append(f"{pool['pool']}/{model}" + (" (retired)" if model in retired else "")
                       + f" -> {replacement}")
    lines.append("off: " + ("; ".join(off) or "none"))
    if worker_line:
        lines.append(worker_line)
    if directive:
        lines.append(directive)
    lines.append("pick: fleetctl.py select --role R [--stakes S]")
    return "\n".join(lines)


def show_usage(
    state_dir: Path, db_path: Path, as_json: bool, roster: dict[str, Any] | None = None
) -> None:
    runtime = load_json(state_dir / "runtime.json", {}) or {}
    state, evidence = current_pool_state(runtime, "opencode-go", roster=roster)
    # All non-routing pools with a stored snapshot
    quota_pools: dict[str, Any] = {}
    for pool, snapshot in sorted((runtime.get("quota_snapshots") or {}).items()):
        if pool == ROUTING_POOL:
            continue
        pool_state, pool_evidence = current_pool_state(runtime, pool, roster=roster)
        quota_pools[pool] = {
            "quota_state": pool_state,
            "band": task_band(pool_state, pool_evidence),
            "bottleneck_used_percent": pool_evidence.get("bottleneck_used_percent"),
            "binding_used_percent": pool_evidence.get("binding_used_percent"),
            "non_binding": pool_evidence.get("non_binding") or [],
            "spend_down": pool_evidence.get("spend_down") or [],
            "confidence": pool_evidence.get("confidence"),
            "age_seconds": pool_evidence.get("age_seconds"),
            "observed_at": snapshot.get("observed_at"),
            "source": snapshot.get("source", "unknown"),
            # The reset clock was reaching this function all along and was being
            # dropped here, which is what made the whole pool look clockless.
            "windows": pool_evidence.get("windows") or {},
        }
    metered_pools = metered_pool_usage(state_dir, roster)
    policy, policy_source = resolve_quota_policy(roster)
    result = {
        "pool": "opencode-go",
        "quota_state": state,
        "quota_policy": {"policy": policy, "source": policy_source},
        "levels": {pool: pool_level(runtime, pool) for pool in sorted(runtime.get("switches") or {})
                   if pool_level(runtime, pool) != "normal"},
        "quota": evidence,
        "local_observed": local_observed(db_path),
        "wrapper_run_ledger": run_ledger_usage(state_dir),
        "quota_pools": quota_pools,
        "metered_pools": metered_pools,
        "truth_boundary": {
            "authoritative_percent_source": "authenticated OpenCode Go console",
            "local_cost_is_quota_debit": False,
            "remaining_percent_when_console_snapshot_is_stale": "unknown",
        },
    }
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    # Printed first and unprompted: the policy decides how every line below is
    # acted on, and a routing rule nobody can see is a routing rule nobody chose.
    print(f"quota policy: {policy} (from {policy_source}) — change with `fleetctl.py policy --set`")
    for pool in sorted(runtime.get("switches") or {}):
        level = pool_level(runtime, pool)
        if level != "normal":
            print(f"level: {pool} is {level.upper()} by hand ({LEVEL_MEANING[level]}) — "
                  f"back with `fleetctl.py level {pool} normal`")
    # Say it out loud when there is no measurement. Routing still runs at full
    # quality (UNKNOWN fails open), but silence would read as "quota is fine"
    # when it actually means "nobody is looking".
    if not any(info.get("bottleneck_used_percent") is not None for info in quota_pools.values()):
        print(
            "quota data: UNAVAILABLE for every pool — routing at full quality, unthrottled. "
            "No percentage is being observed, so none of the bands below are real. "
            "Declare a `quota_refresh` source per pool in the access overlay to get "
            "measurement back: `command` and `file` oracles work anywhere, `http` suits a "
            "self-hosted endpoint, and `codexbar` needs the optional third-party CodexBar reader."
        )
    for pool, info in quota_pools.items():
        pct = info.get("bottleneck_used_percent")
        pct_s = f"{pct}% used" if pct is not None else "no active window / stale"
        conf = info.get("confidence")
        conf_s = f", {conf}, age {info.get('age_seconds')}s" if conf else ""
        source_label = info.get("source", "unknown")
        print(f"{pool} ({source_label}): {info['quota_state']} ({info['band']}) — {pct_s}{conf_s}")
        for name, window in sorted(info.get("windows", {}).items()):
            if name in info.get("spend_down", []):
                note = " — SPEND DOWN, resets soon with allowance to burn"
            elif name in info.get("non_binding", []):
                note = " — surplus projected, not throttling on this"
            else:
                note = ""
            surplus = window.get("surplus") or {}
            projected = surplus.get("projected_used_percent_at_reset")
            basis = f" [{surplus.get('basis')}" + (
                f", projects {projected}% at reset]" if projected is not None else "]"
            )
            print(
                f"  {name}: {window['used_percent']}% used, resets {window['reset_at']} "
                f"(in {format_duration(window.get('seconds_to_reset', 0))}){basis}{note}"
            )
    for pool, info in metered_pools.items():
        print(
            f"{pool} (metered): {info['admission']} — "
            f"${info['estimated_spent_usd_today']:.4f} of ${info['daily_usd_cap']:.2f} estimated "
            f"spent today, ${info['estimated_remaining_usd']:.4f} left. "
            "No percentage quota exists for this pool; the daily cap is the limiter."
        )
    print(f"OpenCode Go state: {state}")
    if evidence.get("bottleneck_used_percent") is not None:
        print(
            f"OpenCode Go quota: {evidence['bottleneck_used_percent']}% used at the bottleneck "
            f"({evidence['confidence']}, age {evidence['age_seconds']}s)"
        )
        for name, window in evidence.get("windows", {}).items():
            print(f"  {name}: {window['used_percent']}% used, resets {window['reset_at']}")
    else:
        print("OpenCode Go quota: unavailable or stale; remaining percentage is UNKNOWN")
    observed = result["local_observed"]
    if observed.get("available"):
        for name, window in observed["windows"].items():
            print(
                f"  local {name}: ${window['estimated_cost_usd']:.6f} estimated equivalent, "
                f"{window['tokens']['total']} tokens, {window['completed_steps']} steps"
            )
    ledger = result["wrapper_run_ledger"]
    profiles = ledger.get("windows", {}).get("rolling_5h", {})
    if profiles:
        print("  prompt-free wrapper ledger, rolling 5h:")
        for profile, values in sorted(profiles.items()):
            print(
                f"    {profile}: {values['runs']} runs, {values['successful_runs']} successful, "
                f"{values['tokens']['total']} tokens, ${values['estimated_cost_usd']:.6f} local estimate"
            )
    print("Local cost is not authoritative Go quota debit and is never converted to a quota percentage.")


def snapshot_command(args: argparse.Namespace, state_dir: Path) -> None:
    observed = utc_now()
    windows = {
        "rolling_5h": (args.rolling_used, args.rolling_reset_seconds),
        "weekly": (args.weekly_used, args.weekly_reset_seconds),
        "monthly": (args.monthly_used, args.monthly_reset_seconds),
    }
    snapshot = {
        "source": "opencode-console-dashboard",
        "observed_at": iso(observed),
        "precision_percentage_points": 1,
        "windows": {
            name: {
                "used_percent": used,
                "reset_in_seconds": reset,
                "reset_at": iso(observed + dt.timedelta(seconds=reset)),
            }
            for name, (used, reset) in windows.items()
        },
    }
    with locked_runtime(state_dir) as runtime:
        runtime.setdefault("quota_snapshots", {})["opencode-go"] = snapshot
        if max(args.rolling_used, args.weekly_used, args.monthly_used) < 100:
            runtime.setdefault("pool_circuits", {}).pop("opencode-go", None)
    print(json.dumps(snapshot, indent=2, sort_keys=True))


def codexbar_snapshot_command(
    args: argparse.Namespace, state_dir: Path, roster: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Read authoritative quota from declared or specified oracle sources into quota_snapshots."""
    sources = quota_sources(roster)
    raw_provider = getattr(args, "provider", None)
    if raw_provider:
        items = [p.strip() for p in raw_provider.split(",") if p.strip()]
    else:
        # Only what the config declares. A built-in provider list here would put one
        # user's pool names back into the engine by the back door.
        items = list(sources.keys())

    results: dict[str, Any] = {}
    with locked_runtime(state_dir) as runtime:
        for item in items:
            if item in sources:
                pool_key = item
                cfg = sources[item]
            else:
                matching = [p for p, c in sources.items() if (c.get("provider") or c.get("codexbar_provider")) == item]
                if matching:
                    pool_key = matching[0]
                    cfg = sources[pool_key]
                else:
                    pool_key = CODEXBAR_POOL_ALIASES.get(item, item)
                    cfg = {"oracle": "codexbar", "provider": item}

            oracle_name = cfg.get("oracle", "codexbar")
            if pool_has_no_known_limit(roster, pool_key):
                results[pool_key] = {"available": False, "reason": "no known limit; quota snapshots ignored"}
                continue
            adapter = ORACLE_REGISTRY.get(oracle_name)
            if not adapter:
                results[item] = {"available": False, "reason": f"unknown oracle: {oracle_name}"}
                continue

            observed = adapter(cfg, timeout=getattr(args, "timeout", REFRESH_TIMEOUT_S))
            if not observed.get("available"):
                results[item] = {"available": False, "reason": observed.get("reason")}
                continue

            if "windows" in cfg and isinstance(cfg["windows"], list):
                allowed = set(cfg["windows"])
                filtered = {k: v for k, v in observed.get("windows", {}).items() if k in allowed}
                if filtered:
                    observed["windows"] = filtered
                else:
                    observed["available"] = False
                    observed["reason"] = "no matching rate windows"
                    results[item] = {"available": False, "reason": observed.get("reason")}
                    continue

            runtime.setdefault("quota_snapshots", {})[pool_key] = observed
            if all(int(w["used_percent"]) < 100 for w in observed["windows"].values()):
                runtime.setdefault("pool_circuits", {}).pop(pool_key, None)
            state, evidence = current_pool_state(runtime, pool_key, utc_now(), roster=roster)
            results[pool_key] = {
                "available": True,
                "state": state,
                "band": task_band(state, evidence),
                "bottleneck_used_percent": evidence.get("bottleneck_used_percent"),
                "spend_down": evidence.get("spend_down") or [],
                "observed_at": observed["observed_at"],
                "windows": {n: w["used_percent"] for n, w in observed["windows"].items()},
            }

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for name, info in results.items():
            if info.get("available"):
                pct = info.get("bottleneck_used_percent")
                pct_s = f"{pct}% at bottleneck" if pct is not None else "no active window"
                print(f"{name}: {info['state']} ({info['band']}) — {pct_s}")
                for wname, used in info["windows"].items():
                    print(f"  {wname}: {used}% used")
            else:
                print(f"{name}: unavailable — {info.get('reason')}")
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    api = sub.add_parser("serve-api", help="serve read-only text lanes as a loopback OpenAI-compatible API")
    api.add_argument("--port", type=int, default=4320)
    api.add_argument("--key-file", type=Path, help="0600 bearer key file; otherwise CROSSFEED_API_KEY")
    api.add_argument("--dir", type=Path, default=Path.cwd(), help="read-only worker directory")
    api.add_argument("--role", default="review", help="selector role for every API request")
    provider = sub.add_parser("provider", help="probe, add, list or remove a model source")
    provider.add_argument("action", choices=("probe", "add", "list", "remove"))
    provider.add_argument("id", nargs="?")
    provider.add_argument("--type", dest="kind", default="openai-compatible")
    provider.add_argument("--label", default="")
    provider.add_argument("--base-url", default="")
    provider.add_argument("--key-ref", dest="credential_ref", default="")
    provider.add_argument("--key-stdin", action="store_true", help="read a pasted key from stdin; never put a key in argv")
    provider.add_argument("--models", default="", help="comma-separated IDs; omitted means all returned IDs")
    provider.add_argument("--accept-relay-risk", dest="acceptance", default="", help="your acceptance quote, dated when saved")
    provider.add_argument("--daily-cap", default="0", help="estimated API spend cap in USD; zero blocks calls")
    chatgpt = sub.add_parser("chatgpt", help="refresh saved gateway lanes")
    chatgpt.add_argument("action", choices=["sync"])
    chatgpt.add_argument("label", nargs="?")
    sub.add_parser("roster-json", help="emit the roster with currently probed gateway lanes")
    mcp = sub.add_parser("codex-mcp-denials", help="list live MCP tools disabled for a worker directory")
    mcp.add_argument("directory", type=Path)
    effort = sub.add_parser("effort", help="resolve a model/role effort without using saved tool defaults")
    effort.add_argument("model")
    effort.add_argument("role", nargs="?", default="default")
    effort.add_argument("--harness")
    effort.add_argument("--level", help="explicit caller override")
    effort.add_argument("--stand-in", action="store_true", help="clamp a caller level to the replacement model's allowed levels")
    effort.add_argument("--json", action="store_true")
    effort.add_argument("--explain", action="store_true", help="tab-separated level, model key and reason")
    sub.add_parser("effort-check", help="fail on missing, invalid or stale effort evidence")

    # Auto-refresh keeps a stale snapshot from silently degrading routing; the
    # opt-out exists for a run that wants zero surprise latency or a fixed state.
    refresh_help = (
        "skip automatic snapshot refresh and read stored snapshot as-is "
        "(env: FLEET_NO_AUTO_REFRESH=1)"
    )

    usage = sub.add_parser("usage", help="show direct quota state and exact local telemetry")
    usage.add_argument("--json", action="store_true")
    usage.add_argument("--no-refresh", action="store_true", help=refresh_help)

    snapshot = sub.add_parser("snapshot", help="record an authenticated Go console snapshot")
    snapshot.add_argument("--rolling-used", type=int, required=True)
    snapshot.add_argument("--rolling-reset-seconds", type=int, required=True)
    snapshot.add_argument("--weekly-used", type=int, required=True)
    snapshot.add_argument("--weekly-reset-seconds", type=int, required=True)
    snapshot.add_argument("--monthly-used", type=int, required=True)
    snapshot.add_argument("--monthly-reset-seconds", type=int, required=True)

    codexbar = sub.add_parser(
        "codexbar-snapshot",
        help="read authoritative quota into quota_snapshots (defaults to all declared sources)",
    )
    codexbar.add_argument(
        "--provider",
        default=None,
        help="comma-separated providers or pool names to refresh (default: all declared pools)",
    )
    # Deliberately far longer than REFRESH_TIMEOUT_S: the auto-refresh must never stall a
    # run, but this is the manual act whose whole job is to wait for the slow web pools.
    codexbar.add_argument("--timeout", type=int, default=25)
    codexbar.add_argument("--json", action="store_true")

    route = sub.add_parser("route", help="select a quality-first lane")
    route.add_argument("--role", default="default")
    route.add_argument("--mode", choices=["read-only", "write"], default="read-only")
    route.add_argument("--modality", choices=["text", "image", "audio", "video"], default="text")
    route.add_argument("--selector", action="store_true")
    route.add_argument("--json", action="store_true")
    route.add_argument("--harness", help="only consider lanes for this harness "
                       "(a wrapper can only run its own; prevents routing handing back "
                       "a lane the caller must refuse)")
    route.add_argument("--no-refresh", action="store_true", help=refresh_help)
    route.add_argument("--one-shot", action="store_true",
                       help="a single call that may use a big model even on a low pool "
                            "(keeps the quality order; the pool's one-slot cap still applies)")

    select = sub.add_parser("select", help="choose a pool, model and level from local evidence")
    select.add_argument("--role", required=True)
    select.add_argument("--stakes", choices=["low", "normal", "high", "irreversible"], default="normal")
    select.add_argument("--family")
    select.add_argument("--exclude-lineage")
    select.add_argument("--lead", metavar="POOL", help="lead funding pool; explicit beats harness environment")
    select.add_argument("--allow", metavar="FILE_OR_LIST", help="restrict to pool:model_key:level entries; level * matches any")
    select.add_argument("--mode", choices=["read-only", "write"])
    select.add_argument("--modality", choices=["text", "image", "audio", "video"], default="text")
    select.add_argument("--json", action="store_true")
    select.add_argument("--no-refresh", action="store_true", help=refresh_help)

    dispatch = sub.add_parser("dispatch", help="select and run a worker with runtime fallback")
    dispatch.add_argument("--role", required=True)
    dispatch.add_argument("--stakes", choices=["low", "normal", "high", "irreversible"], default="normal")
    dispatch.add_argument("--family")
    dispatch.add_argument("--exclude-lineage")
    dispatch.add_argument("--lead", metavar="POOL", help="lead funding pool; explicit beats harness environment")
    dispatch.add_argument("--allow", metavar="FILE_OR_LIST", help="restrict to pool:model_key:level entries; level * matches any")
    dispatch.add_argument("--mode", choices=["read-only", "write"])
    task = dispatch.add_mutually_exclusive_group(required=True)
    task.add_argument("--prompt")
    task.add_argument("--prompt-file", type=Path)
    dispatch.add_argument("--dir", type=Path, required=True)
    dispatch.add_argument("--last", type=Path)
    dispatch.add_argument("--dry-run", action="store_true")
    dispatch.add_argument("--no-refresh", action="store_true", help=refresh_help)

    acquire = sub.add_parser("acquire", help="atomically reserve a lane concurrency slot")
    acquire.add_argument("--lane", required=True)
    acquire.add_argument("--ttl", type=int, default=1800)
    acquire.add_argument("--pid", type=int, help="require this original caller to still be the parent")
    acquire.add_argument("--no-refresh", action="store_true", help=refresh_help)

    release = sub.add_parser("release", help="release a concurrency slot")
    release.add_argument("--token", required=True)

    claim = sub.add_parser(
        "claim-paths",
        help="claim exclusive write access to paths (cross-campaign/session conflict, not clobber)",
    )
    claim.add_argument("--owner", required=True, help="who holds the claim, e.g. fanout:<run-id>")
    claim.add_argument("--ttl", type=int, default=14400, help="dead-process safety net in seconds")
    claim.add_argument("paths", nargs="+")

    release_claim = sub.add_parser("release-paths", help="release a path claim")
    release_claim.add_argument("--token", required=True)

    sub.add_parser("claims", help="list active path claims")

    record = sub.add_parser("record", help="append prompt-free structured run telemetry")
    record.add_argument("--lane", required=True)
    record.add_argument("--identity", type=Path, help="shared wrapper model receipt prepared before launch")
    record.add_argument("--events", type=Path)
    record.add_argument("--stderr", type=Path)
    record.add_argument("--returncode", type=int, required=True)
    record.add_argument("--started-at")
    record.add_argument("--context-profile", choices=["lean", "shared", "unspecified"], default="unspecified")
    record.add_argument(
        "--agent-profile",
        choices=["plan", "build", "fleet-research", "direct", "unspecified"],
        default="unspecified",
    )
    record.add_argument("--execution-mode", choices=["harness", "direct"], default="harness")
    record.add_argument("--effort")
    record.add_argument("--selection-file", type=Path)
    record.add_argument("--role")
    record.add_argument("--family")

    afk_record = sub.add_parser("afk-record", help="append one proof-gated AFK attempt to runs.jsonl")
    afk_record.add_argument("--lane", required=True)
    afk_record.add_argument("--attempt-id", required=True)
    afk_record.add_argument("--started-at", required=True)
    afk_record.add_argument("--duration-ms", type=int, required=True)
    afk_record.add_argument("--result", choices=["verified", "failed"], required=True)
    afk_record.add_argument("--failure-class")
    afk_record.add_argument("--proof-output-hash", required=True)
    afk_record.add_argument("--proof-returncode", type=int, required=True)
    afk_record.add_argument("--worker-returncode", type=int, required=True)
    afk_record.add_argument("--effort")
    afk_record.add_argument("--selection-file", type=Path)
    afk_record.add_argument("--role")
    afk_record.add_argument("--family")

    rank_afk = sub.add_parser("rank-routes", help="report AFK route evidence; never changes routing policy")
    rank_afk.add_argument("--json", action="store_true")

    policy = sub.add_parser(
        "policy", help="show or set how quota gates routing (clock_aware | strict | off)"
    )
    policy.add_argument("--json", action="store_true")
    policy.add_argument(
        "--set",
        dest="set_policy",
        choices=QUOTA_POLICIES,
        help="write this policy into the overlay as the machine default",
    )

    sub.add_parser("doctor", help="report what this machine can run and what is missing")
    switch = sub.add_parser(
        "switch", help="older three-way form of `level`: off = off, on = forced, auto = normal"
    )
    switch.add_argument("pool", nargs="?")
    switch.add_argument("state", nargs="?", choices=("off", "on", "auto"))

    level = sub.add_parser(
        "level",
        help="how much of a pool to spend: off | low | normal | high | forced; no value shows it, no pool lists all",
    )
    level.add_argument("pool", nargs="?")
    level.add_argument("value", nargs="?", choices=LEVELS)

    profile = sub.add_parser("profile", help="apply an owner-authorized spend preset; preserve unnamed off pools")
    profile.add_argument("name", choices=PROFILES)
    profile.add_argument("--because", help="owner's phrase; name an off pool explicitly to change it")
    profile.add_argument("--who", help="audit actor (default: detected lead and local user)")

    slot = sub.add_parser(
        "pool-slot",
        help="wrapper gate: at level low, hold the pool's one slot for process --pid (waits, then exits 5)",
    )
    slot.add_argument("pool")
    slot.add_argument("--pid", type=int, required=True, help="the process that holds the slot; it is freed when that exits")
    slot.add_argument("--wait", type=int, default=int(os.environ.get("FLEET_POOL_WAIT_S", "900")),
                      help="seconds to wait for a running one before refusing (env FLEET_POOL_WAIT_S, default 900)")
    slot.add_argument("--ttl", type=int, default=43200, help="bound on a slot left by a reused process id")

    choice = sub.add_parser(
        "model-choice",
        help="run only one model on a pool: no value prints it (empty unless exactly one is on), "
             "a model switches every other one off, auto switches them all on",
    )
    choice.add_argument("pool")
    choice.add_argument("model", nargs="?")

    toggle = sub.add_parser(
        "model-toggle",
        help="switch one model of a pool on or off (the console's switches); no state prints it",
    )
    toggle.add_argument("pool")
    toggle.add_argument("model")
    toggle.add_argument("state", nargs="?", choices=("on", "off"))

    older = sub.add_parser("older-models", help="audit older models and persist the no-reason/off rule")
    older.add_argument("action", nargs="?", choices=["list", "apply"], default="list")
    older.add_argument("--dry-run", action="store_true", help="preview apply without writing any state")
    older.add_argument("--json", action="store_true")

    run = sub.add_parser(
        "model-run",
        help="wrapper gate: the model a direct pool runs for a task that asks for MODEL "
             "(empty = as asked); exits 5 when no model is switched on",
    )
    run.add_argument("pool")
    run.add_argument("model", nargs="?")
    run.add_argument("--explain", action="store_true",
                     help="when a model stands in, print '<model that runs> <model asked for> <off|retired|unlisted>'")

    gate = sub.add_parser(
        "model-gate",
        help="wrapper gate for a model named without a lane: exits 5, saying why, when the console has it off",
    )
    gate.add_argument("model")
    gate.add_argument("--harness", help="only lanes of this harness (agy, opencode, ...)")
    gate.add_argument("--provider", help="only lanes of this provider (google, ...)")
    gate.add_argument("--pool", help="a direct pool (codex, claude): match the model by key, run id or alias")
    gate.add_argument("--reason-only", action="store_true",
                      help="say only why, not what to name instead (for a transport that runs one model)")

    pins = sub.add_parser(
        "pins",
        help="model names written in files outside Crossfeed (quota_pools.<pool>.model_pins): "
             "show them, bring the managed ones in step with the switches, or put them back",
    )
    pins.add_argument("action", nargs="?", choices=("status", "sync", "undo"), default="status")
    pins.add_argument("--json", action="store_true")

    brief = sub.add_parser("brief", help="compact pool table and model replacements for planning")
    brief.add_argument("--json", action="store_true")
    brief.add_argument("--lead", metavar="POOL", help="lead funding pool; explicit beats harness environment")
    brief.add_argument("--verbose", action="store_true", help="every limit, plan, pin and evidence warning")
    brief.add_argument("--no-refresh", action="store_true", help=refresh_help)

    console = sub.add_parser("console", help="open the local console page (127.0.0.1 only, signed-in link)")
    console.add_argument("--port", type=int, default=8768, help="preferred port; the next free one is used if taken")
    console.add_argument("--no-open", action="store_true", help="print the sign-in link instead of opening a browser")

    sub.add_parser("dashboard", help="print the authenticated Go console URL")
    return parser


def run_console(overlay_path: Path, state_dir: Path, port: int, open_browser: bool) -> int:
    """Start the console page; it lives beside this file so an install carries both."""
    spec = importlib.util.spec_from_file_location("orchestrator_console", Path(__file__).with_name("console.py"))
    if not spec or not spec.loader:
        raise FleetError("console.py is missing next to fleetctl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.serve(overlay_path, state_dir, port=port, open_browser=open_browser)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    state_dir = args.state_dir.expanduser()
    if args.command == "dispatch":
        # Selection receipt paths must survive the worker's different cwd.
        state_dir = state_dir.resolve()
    def refresh(pools: Iterable[str], roster: dict[str, Any] | None = None) -> None:
        if not getattr(args, "no_refresh", False):
            refresh_stale_pools(state_dir, pools, roster=roster)

    try:
        if args.command == "serve-api":
            import lanes_api
            try:
                return lanes_api.serve(args.overlay, state_dir, port=args.port, key_file=args.key_file,
                                       directory=args.dir, role=args.role)
            except lanes_api.fleetctl.FleetError as exc:
                # When this file is __main__, the API imports a separate module instance.
                raise FleetError(str(exc)) from None
        if args.command == "provider":
            try:
                from . import providers
            except ImportError:
                import providers
            try:
                if args.action == "list":
                    result = providers.list_sources(read_overlay(args.overlay.expanduser(), discover=False))
                elif args.action == "remove":
                    result = providers.remove(args.overlay, args.id)
                else:
                    key = sys.stdin.read(8193).strip() if args.key_stdin else ""
                    kwargs = dict(kind=args.kind, base_url=args.base_url, credential_ref=args.credential_ref,
                                  key=key, models=args.models)
                    if args.action == "probe":
                        result = providers.probe(**kwargs)
                    else:
                        result = providers.add(args.overlay, id=args.id or "", label=args.label,
                                               acceptance=args.acceptance, daily_cap=args.daily_cap, **kwargs)
            except providers.ProviderError as exc:
                raise FleetError(str(exc)) from None
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "roster-json":
            roster = read_overlay(args.overlay.expanduser(), state_dir)
            if roster.get("chatgpt_catalog", {}).get("error"):
                print("fleetctl: Crossfeed Chat gateway admission closed: " + roster["chatgpt_catalog"]["error"],
                      file=sys.stderr)
            print(json.dumps(roster))
            return 0
        if args.command == "chatgpt":
            if args.label:
                raise FleetError("chatgpt sync takes no label")
            try:
                from chatgpt_catalog import expand
            except ImportError:
                from scripts.chatgpt_catalog import expand
            roster = expand(read_overlay(args.overlay.expanduser(), discover=False), state_dir, persist=True)
            if not roster.get("chatgpt_gateway"):
                raise FleetError("roster has no chatgpt_gateway template")
            for selector, state in roster["chatgpt_catalog"]["states"].items():
                print(selector + ": " + state)
            if roster["chatgpt_catalog"]["error"]:
                raise FleetError(roster["chatgpt_catalog"]["error"])
            return 0
        if args.command == "usage":
            # The overlay carries the metered-pool caps and the quota-source
            # declarations. Read it defensively: `usage` reported quota long before
            # it needed a roster, and a broken overlay must not take the telemetry
            # command down with it (the built-in registry still applies).
            try:
                usage_roster = read_overlay(args.overlay.expanduser(), state_dir)
            except (FleetError, OSError, json.JSONDecodeError):
                usage_roster = None
            # Every pool with a declared quota source: `usage` is the read a human
            # or agent waits on, so it is the right place to pay the dashboard fetch.
            refresh(quota_sources(usage_roster), usage_roster)
            show_usage(state_dir, args.db.expanduser(), args.json, usage_roster)
            return 0
        if args.command == "snapshot":
            for value in (args.rolling_used, args.weekly_used, args.monthly_used):
                if value < 0 or value > 100:
                    raise FleetError("used percentages must be between 0 and 100")
            snapshot_command(args, state_dir)
            return 0
        if args.command == "codexbar-snapshot":
            try:
                cmd_roster = read_overlay(args.overlay.expanduser(), state_dir)
            except (FleetError, OSError, json.JSONDecodeError):
                cmd_roster = None
            codexbar_snapshot_command(args, state_dir, cmd_roster)
            return 0
        if args.command == "doctor":
            return doctor_command(args.overlay.expanduser(), state_dir)
        if args.command == "policy":
            policy_command(args, args.overlay.expanduser())
            return 0
        if args.command == "profile":
            roster = read_overlay(args.overlay.expanduser(), state_dir)
            who = args.who or f"{detect_lead_pool() or 'fleetctl'}:{os.environ.get('USER') or os.getuid()}"
            with locked_runtime(state_dir) as runtime:
                result = apply_profile(roster, runtime, args.name, who=who, because=args.because)
            print(profile_report(result))
            return 0
        if args.command in {"switch", "level"}:
            pools = set(read_overlay(args.overlay.expanduser(), state_dir).get("quota_pools", {}))
            if args.pool and args.pool not in pools:
                raise FleetError(f"unknown pool {args.pool}; pools: {', '.join(sorted(pools))}")
            legacy = args.command == "switch"
            wanted = {"off": "off", "on": "forced", "auto": "normal"}.get(args.state) if legacy else args.value
            if wanted and not args.pool:
                raise FleetError("name the pool to change")
            show = legacy_switch_word if legacy else (lambda value: value)
            if wanted:
                with locked_runtime(state_dir) as runtime:
                    set_pool_level(runtime, args.pool, wanted)
            # Reading never writes: a query must not create or touch the runtime file.
            runtime = load_json(state_dir / "runtime.json", {}) or {}
            if args.pool:
                print(show(pool_level(runtime, args.pool)))
            else:
                for pool in sorted(pools):
                    print(f"{pool}: {show(pool_level(runtime, pool))}")
            return 0
        if args.command == "pool-slot":
            # No overlay read: the gate must never be the reason a wrapper cannot start.
            return wait_for_pool_slot(state_dir, args.pool, args.pid, args.ttl, args.wait)
        if args.command in {"model-choice", "model-toggle", "model-run"}:
            roster = read_overlay(args.overlay.expanduser(), state_dir)
            if args.pool not in (roster.get("quota_pools") or {}):
                raise FleetError(f"unknown pool {args.pool}")
            offered = ", ".join(sorted(choosable_models(roster, args.pool))) or "none"
        if args.command == "model-run":
            # Reading never writes, and a wrapper never waits on it: exit 5 only for "nothing is on".
            runtime = load_json(state_dir / "runtime.json", {}) or {}
            try:
                picked = model_run_as(roster, runtime, args.pool, args.model)
            except NoModelOn as exc:
                print(f"fleetctl: {exc}", file=sys.stderr)
                return 5
            if picked and args.explain:
                asked = args.model or direct_default_model(args.pool) or ""
                key = named_model(roster, args.pool, asked)
                why = "unlisted" if not key else model_blocked(roster, runtime, args.pool, key) or "off"
                print(f"{picked} {asked or '-'} {why}")
            elif picked:
                print(picked)
            return 0
        if args.command == "model-toggle":
            if args.state:
                with locked_runtime(state_dir) as runtime:
                    try:
                        set_model_toggle(runtime, roster, args.pool, args.model, args.state == "on")
                    except ValueError as exc:
                        raise FleetError(f"{exc}: {args.model} (models on {args.pool}: {offered})") from exc
                pins_after_switch(roster, state_dir)
            runtime = load_json(state_dir / "runtime.json", {}) or {}
            switches = pool_switches(roster, runtime, args.pool)
            if args.model not in switches:
                raise FleetError(f"that model cannot run on this pool: {args.model} (models on {args.pool}: {offered})")
            print("on" if switches[args.model] else "off")
            return 0
        if args.command == "model-choice":
            if args.model:
                with locked_runtime(state_dir) as runtime:
                    try:
                        set_model_choice(runtime, roster, args.pool, args.model)
                    except ValueError as exc:
                        raise FleetError(f"{exc}: {args.model} (choosable on {args.pool}: {offered}; or auto)") from exc
                pins_after_switch(roster, state_dir)
            # Reading never writes. What prints is the one model that is on, as a direct wrapper
            # would pass it to its CLI, and nothing while several or none are.
            runtime = load_json(state_dir / "runtime.json", {}) or {}
            switches = pool_switches(roster, runtime, args.pool)
            on = [model for model, value in switches.items() if value]
            if len(on) == 1 and len(switches) > 1:
                print(choosable_models(roster, args.pool)[on[0]])
            return 0
        if args.command == "older-models":
            roster = read_overlay(args.overlay.expanduser(), state_dir)
            if args.action == "apply" and not args.dry_run:
                with locked_runtime(state_dir) as runtime:
                    rows = apply_older_model_rule(roster, runtime)
                pins_after_switch(roster, state_dir)
            else:
                runtime = load_json(state_dir / "runtime.json", {}) or {}
                rows = older_model_audit(roster, runtime)
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                for row in rows:
                    state = "on" if row["on"] else "off"
                    change = ("would switch off" if args.dry_run else "switched off") if row["action"] == "off" and args.action == "apply" else state
                    print(f"{row['pool']} {row['model']}: {change}; {row['reason'] or 'no qualifying roster reason'}"
                          + (" (legacy default was on)" if row["stored_on"] and not row["on"] else ""))
                if not rows:
                    print("no runnable older models")
            return 0
        if args.command == "model-gate":
            # Reading never writes. Exit 5 only when the console has the model, or its provider, off.
            roster = read_overlay(args.overlay.expanduser(), state_dir)
            runtime = load_json(state_dir / "runtime.json", {}) or {}
            if args.pool and args.pool not in (roster.get("quota_pools") or {}):
                raise FleetError(f"unknown pool {args.pool}")
            verdict = start_verdict(roster, runtime, args.model, pool=args.pool, harness=args.harness,
                                    provider=args.provider)
            if verdict:
                print(f"fleetctl: {verdict['reason'] if args.reason_only else verdict['message']}", file=sys.stderr)
                return 5
            return 0
        if args.command == "pins":
            roster = read_overlay(args.overlay.expanduser(), state_dir)
            rows = sync_pins(roster, state_dir, write=args.action != "status", undo=args.action == "undo")
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            elif not rows:
                print("no model pins are declared in the roster (quota_pools.<pool>.model_pins)")
            else:
                for row in rows:
                    print(_pin_line(row))
            # 1 while a file still names a model that is off: that model can still be started.
            return 1 if any(row.get("blocked") for row in rows) else 0
        if args.command == "brief":
            roster = read_overlay(args.overlay.expanduser(), state_dir)
            refresh(quota_sources(roster), roster)
            runtime = load_json(state_dir / "runtime.json", {}) or {}
            overview = fleet_overview(roster, runtime, state_dir)
            lead = resolve_lead_pool(roster, args.lead)
            if lead is not None:
                overview["lead"] = {"pool": lead, "pressure": lead_pressure(runtime, lead, roster)}
            print(json.dumps(overview, indent=2, sort_keys=True) if args.json else
                  render_brief(overview, verbose=args.verbose, roster=roster, runtime=runtime, lead=lead))
            return 0
        if args.command == "console":
            return run_console(args.overlay.expanduser(), state_dir, args.port, not args.no_open)
        if args.command == "dashboard":
            print("https://opencode.ai/auth")
            return 0
        if args.command == "claim-paths":
            print(acquire_path_claim(state_dir, args.paths, args.owner, args.ttl))
            return 0
        if args.command == "release-paths":
            release_path_claim(state_dir, args.token)
            return 0
        if args.command == "claims":
            print(json.dumps(list_path_claims(state_dir), indent=2, sort_keys=True))
            return 0

        roster = read_overlay(args.overlay.expanduser(), state_dir)
        if args.command == "codex-mcp-denials":
            for server in codex_mcp_denials(roster, args.directory):
                print(server)
            return 0
        if args.command == "effort-check":
            issues = effort_problems(roster, state_dir)
            for issue in issues:
                print(f"fleetctl: EFFORT {issue}", file=sys.stderr)
            if not issues:
                print("fleetctl: effort evidence PASS")
            return 1 if issues else 0
        if args.command == "effort":
            runtime = load_json(state_dir / "runtime.json", {}) or {}
            resolved = resolve_effort(roster, args.model, args.role, args.harness, args.level, stand_in=args.stand_in)
            # Routed harnesses use the router's primary pressure band, not CLI
            # names ('opencode'/'agy') that are not quota pools. Direct seats use
            # their own pool. The hand-set spend level changes both paths alike.
            harness = resolved["harness"]
            pool = harness if harness in DIRECT_POOLS else ROUTING_POOL
            state, evidence = current_pool_state(runtime, pool, roster=roster,
                                                  model_key=resolved["model_key"])
            band = level_band(pool_level(runtime, pool), task_band(state, evidence))
            resolved = resolve_effort(roster, args.model, args.role, harness, args.level, band=band, stand_in=args.stand_in)
            if args.json:
                print(json.dumps(resolved, sort_keys=True))
            elif args.explain:
                print(f"{resolved['effort'] or 'provider-default'}\t{resolved['model_key']}\t{resolved['reason']}")
            else:
                print(resolved["effort"] or "provider-default")
                print(f"fleetctl: effort {resolved['effort'] or 'provider-default'}: {resolved['reason']}", file=sys.stderr)
            if resolved["source"] == "missing":
                print(f"fleetctl: {resolved['reason']}", file=sys.stderr)
                return 2
            return 0
        if args.command in {"select", "dispatch"}:
            if args.command == "dispatch":
                prompt = args.prompt if args.prompt is not None else args.prompt_file.expanduser().read_text(encoding="utf-8")
                if not prompt.strip():
                    raise FleetError("dispatch prompt must not be blank")
                if not args.dir.expanduser().is_dir():
                    raise FleetError("dispatch --dir must name an existing directory")
                if args.last and not args.last.expanduser().resolve().parent.is_dir():
                    raise FleetError("dispatch --last parent directory must exist")
            if args.command != "dispatch" or not args.dry_run:
                refresh(set(roster.get("quota_pools", {})) | lane_pools(roster), roster)
            runtime = load_json(state_dir / "runtime.json", {}) or {}
            selection = select_option(
                roster, runtime, state_dir, args.role, stakes=args.stakes,
                family=args.family, exclude_lineage=args.exclude_lineage,
                mode=args.mode, modality=getattr(args, "modality", "text"), allow=args.allow,
                lead=resolve_lead_pool(roster, args.lead),
            )
            if args.command == "dispatch":
                return dispatch_selection(selection, args, state_dir, prompt)
            if args.json:
                print(json.dumps(selection, indent=2, sort_keys=True))
            else:
                print(selection["choice"]["command"])
                for option in selection["top3"]:
                    print(f"{option['pool']}/{option['model_key']}/{option['level']}: {option['score']:.6f}")
                print("Pool prices: " + json.dumps(selection["lambdas"], sort_keys=True))
            return 0
        if args.command == "route":
            # Every pool a candidate lane could spend from, not just the primary:
            # routing is cross-pool now, and a lane picked on an UNKNOWN pool is
            # exactly the stale-quota fault this refresh exists to prevent.
            refresh({ROUTING_POOL, *lane_pools(roster)}, roster)
            runtime = load_json(state_dir / "runtime.json", {}) or {}
            lane = choose_lane(
                roster, runtime, args.role, args.mode, args.modality, args.harness,
                one_shot=args.one_shot,
            )
            if args.json:
                print(json.dumps(lane, indent=2, sort_keys=True))
            elif args.selector:
                print(lane["selector"])
            else:
                print(lane["lane_id"])
            return 0
        if args.command == "acquire":
            # The requested lane's own pool: a no-op for pools CodexBar does not
            # serve, so acquiring a copilot or gemini lane costs nothing extra.
            lane = lane_map(roster).get(args.lane)
            refresh([lane["quota_pool"]] if lane else [], roster)
            print(acquire_lease(state_dir, roster, args.lane, args.ttl, pid=args.pid))
            return 0
        if args.command == "release":
            release_lease(state_dir, args.token)
            return 0
        if args.command == "record":
            record = record_run(
                state_dir,
                roster,
                args.lane,
                args.events,
                args.stderr,
                args.returncode,
                args.started_at,
                args.context_profile,
                args.agent_profile,
                args.execution_mode,
                args.identity,
                effort=args.effort, selection=args.selection_file, role=args.role, family=args.family,
            )
            print(json.dumps(record, sort_keys=True))
            return 0
        if args.command == "afk-record":
            record = record_afk_attempt(
                state_dir, roster, args.lane, args.attempt_id, args.started_at,
                args.duration_ms, args.result, args.failure_class,
                args.proof_output_hash, args.proof_returncode, args.worker_returncode,
                effort=args.effort, selection=args.selection_file, role=args.role, family=args.family,
            )
            print(json.dumps(record, sort_keys=True))
            return 0
        if args.command == "rank-routes":
            ranked = rank_afk_routes(state_dir)
            if args.json:
                print(json.dumps(ranked, indent=2, sort_keys=True))
            else:
                for item in ranked:
                    print(
                        f"{item['route']}: {item['verified']}/{item['attempts']} verified "
                        f"({item['verified_correctness']:.1%}), ${item['estimated_cost_usd']:.6f}, "
                        f"{item['mean_duration_ms']:.0f}ms mean"
                    )
            return 0
    except (FleetError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"fleetctl: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
