#!/usr/bin/env python3
"""Prompt-free model receipts shared by every agent wrapper."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import sys
import sqlite3
import uuid
import math
from pathlib import Path

SCHEMA = "crossfeed-model-run/v1"
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")

FAMILY_BY_ROLE = {
    "implementation": "coding-agent", "builder": "coding-agent", "hard-builder": "coding-agent",
    "failed-builder": "coding-agent", "debug": "coding-agent", "debugger": "coding-agent",
    "review": "review", "reviewer": "review", "audit": "review", "second-opinion": "review",
    "repo-map": "repo-qa", "lookup": "repo-qa", "probe": "repo-qa", "explorer": "repo-qa",
    "hard-reasoning": "reasoning", "judgment": "reasoning", "filter": "extraction",
    "classification": "extraction", "schema": "extraction", "research-scout": "research",
    "research": "research", "scout": "research", "frontend-visual": "visual",
    "owned-dispatch": "coding-agent", "audit-retry": "review",
    "long-context": "repo-qa", "audio-video": "visual",
}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def state_dir():
    return Path(os.environ.get("FLEET_STATE_DIR") or
                (os.environ.get("XDG_STATE_HOME") or "~/.local/state") + "/orchestrator").expanduser()


def roster(*, discover=False):
    path = Path(os.environ.get("ACCESS_OVERLAY") or
                (os.environ.get("XDG_CONFIG_HOME") or "~/.config") + "/orchestrator/access-overlay.json").expanduser()
    try:
        data = json.loads(path.read_text())
        # Receipts for ordinary lanes need only declared metadata. Importing the
        # fleet controller and probing another transport made every wrapper
        # depend on gateway discovery, including standalone wrapper fixtures.
        if discover and ("chatgpt_gateway" in data or
                         data.get("provider_sources") or
                         any(row.get("gateway_service") for row in data.get("lanes", []))):
            try:
                from fleetctl import read_overlay
            except ImportError:
                from scripts.fleetctl import read_overlay
            return read_overlay(path, state_dir())
        return data
    except (OSError, ValueError):
        return {}


def normalize_model_alias(model, roster=None):
    """Resolve declared aliases and provider prefixes without erasing versions."""
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        return model
    data = roster if roster is not None else globals()["roster"]()
    bare = model.rsplit("/", 1)[-1]
    cards = [(key, card) for key, card in (data.get("model_cards") or {}).items()
             if isinstance(card, dict) and not key.startswith("_")]
    # A canonical key is never remapped by another card's overlapping aliases.
    if any(key.rsplit("/", 1)[-1] == bare for key, _ in cards):
        return bare
    for key, card in sorted(cards, key=lambda item: item[1].get("status", "current") != "current"):
        aliases = (card.get("run_as"), *(card.get("aliases") or []))
        if any(isinstance(alias, str) and alias.rsplit("/", 1)[-1] == bare for alias in aliases):
            return key.rsplit("/", 1)[-1]
    for lane in data.get("lanes") or []:
        if (lane.get("model_key") and isinstance(lane.get("selector"), str)
                and lane["selector"].rsplit("/", 1)[-1] == bare):
            return lane["model_key"].rsplit("/", 1)[-1]
    return bare


def dynamic_model_selection(record, roster=None):
    """Auto and service routers intentionally resolve to a different native model."""
    dynamic = {"auto", "default", "unknown", "openrouter/free", "github-copilot-auto",
               "openrouter-free-router", "service-chosen"}
    if record.get("selected_model") in dynamic or record.get("selector") in dynamic:
        return True
    data = roster if roster is not None else globals()["roster"]()
    for lane in data.get("lanes") or []:
        if lane.get("lane_id") == record.get("lane_id") and lane.get("selector") in dynamic:
            return True
    return False


def model_identity_drift(record, roster=None):
    actual, selected = record.get("actual_model"), record.get("selected_model")
    return bool(actual and selected and not dynamic_model_selection(record, roster)
                and normalize_model_alias(actual, roster) != normalize_model_alias(selected, roster))


def mapped_pi_effort(effort):
    """Pi's native enum ends at xhigh; canonical max maps to that ceiling."""
    if effort == "max":
        return "xhigh"
    if effort not in {"off", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("unsupported Pi effort")
    return effort


def pi_assistant_messages(event):
    if event.get("type") == "message_end":
        messages = [event.get("message")]
    elif event.get("type") == "agent_end":
        messages = event.get("messages") or []
    else:
        messages = []
    return [message for message in messages
            if isinstance(message, dict) and message.get("role") == "assistant"]


def observed_usage(wrapper, events):
    """Keep native Pi usage prompt-free and count each assistant message once."""
    if wrapper != "pi" or not events or not events.is_file():
        return {}
    ended, snapshot = [], []
    for line in events.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        messages = pi_assistant_messages(event)
        if event.get("type") == "message_end":
            ended.extend(messages)
        elif event.get("type") == "agent_end":
            snapshot.extend(messages)
    totals, cost, seen = {}, None, set()
    fields = {"input": "input", "output": "output", "cacheRead": "cache_read",
              "cacheWrite": "cache_write", "totalTokens": "total"}
    for message in ended + snapshot:
        signature = json.dumps(message, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        for source, target in fields.items():
            value = usage.get(source)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                totals[target] = totals.get(target, 0) + value
        value = (usage.get("cost") or {}).get("total") if isinstance(usage.get("cost"), dict) else None
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            cost = (cost or 0) + value
    result = {"tokens": totals} if totals else {}
    if cost is not None:
        result["cost"] = {"estimated_usd": cost, "source": "pi-native"}
    return result


def family_for_role(role):
    return FAMILY_BY_ROLE.get(role)


def selection_metadata(selection=None, *, model, lane_id=None, effort=None,
                       pool=None, harness=None, role=None, family=None):
    """Whitelist a receipt and bind it to the option that actually executed.

    Invalid/stale receipts are explicit missing selection, never an unrelated
    dispatch's replay record. This does not invent task correctness or usage.
    """
    result = {"effort": effort or None, "role": role or None, "family": family or None,
              "selection": None}
    source = selection if selection is not None else os.environ.get("FLEET_SELECTION_FILE")
    if source:
        try:
            payload = json.loads(Path(source).read_text()) if isinstance(source, (str, Path)) else source
            choice = payload.get("choice")
            if payload.get("schema") != "fleet-selection/v1" or not isinstance(choice, dict):
                raise ValueError("invalid selection receipt")
            selected_key = choice.get("model_key") or choice.get("model")
            selected_effort = choice.get("effort") or choice.get("level")
            if not model or selected_key != model or not effort or selected_effort != effort:
                raise ValueError("selection model or effort mismatch")
            for field, actual in (("lane_id", lane_id), ("pool", pool), ("harness", harness)):
                if choice.get(field) is not None and choice[field] != actual:
                    raise ValueError("selection " + field + " mismatch")
            if role and role != "default" and payload.get("role") != role:
                raise ValueError("selection role mismatch")
            if family and payload.get("family") != family:
                raise ValueError("selection family mismatch")
            def identity(value):
                return value if isinstance(value, str) and MODEL_ID.fullmatch(value) else None
            def numeric(value):
                return value if type(value) in (int, float) and math.isfinite(value) else None
            def option(raw):
                clean = {}
                for key in ("pool", "harness", "model_key", "model", "run_as", "level", "effort", "lane_id"):
                    value = identity(raw.get(key))
                    if (value is None and raw.get("harness") == "chatgpt-chat"
                            and key in {"model_key", "model", "run_as", "lane_id"}):
                        candidate = raw.get(key)
                        if isinstance(candidate, str):
                            try:
                                from chatgpt_transport import canonical_selector, Rejected
                            except ImportError:
                                from scripts.chatgpt_transport import canonical_selector, Rejected
                            selector = candidate
                            try:
                                if canonical_selector(selector):
                                    value = candidate
                            except Rejected:
                                pass
                    if value is not None:
                        clean[key] = value
                for key in ("score", "quality", "effective_quality", "latency_s", "cost_usd", "selection_probability"):
                    value = numeric(raw.get(key))
                    if value is not None:
                        clean[key] = value
                if isinstance(raw.get("quality"), dict):
                    clean["quality"] = {k: v for k in ("mean", "sd", "effective", "n_sources")
                                        if (v := numeric(raw["quality"].get(k))) is not None}
                if isinstance(raw.get("q"), dict):
                    clean["q"] = {k: v for k in ("mean", "sd", "n_sources")
                                  if (v := numeric(raw["q"].get(k))) is not None}
                    if type(raw["q"].get("unknown")) is bool:
                        clean["q"]["unknown"] = raw["q"]["unknown"]
                fallback = raw.get("pro_fallback")
                if isinstance(fallback, dict) and isinstance(fallback.get("requested_model"), str):
                    clean["pro_fallback"] = {key: fallback[key] for key in ("requested_model", "reason", "order") if key in fallback}
                return clean
            clean = {"schema": "fleet-selection/v1", "choice": option(choice),
                     "top3": [option(row) for row in (payload.get("top3") or [])[:3] if isinstance(row, dict)],
                     "lambdas": {key: numeric(raw) for key, raw in (payload.get("lambdas") or {}).items()
                                 if identity(key) and (raw is None or numeric(raw) is not None)}}
            for key in ("role", "family", "stakes"):
                value = identity(payload.get(key))
                if value is not None:
                    clean[key] = value
            probability = numeric(payload.get("selection_probability"))
            if probability is not None and 0 <= probability <= 1:
                clean["selection_probability"] = probability
            if type(payload.get("exploration")) is bool:
                clean["exploration"] = payload["exploration"]
            result["selection"] = clean
            result["role"] = role if role and role != "default" else clean.get("role") or role or None
            result["family"] = family or clean.get("family")
        except (OSError, ValueError, TypeError, AttributeError):
            # Fixed diagnostics avoid leaking receipt payloads or filesystem paths.
            result["selection_error"] = "receipt unavailable, invalid, or does not match actual model/lane/effort/role/family"
    result["family"] = result["family"] or family_for_role(result["role"])
    return result


def begin(wrapper, requested, selected, lane, role, effort=None, selection=None, family=None):
    data = roster(discover=wrapper in {"chatgpt-chat", "pi"})
    if wrapper == "codex" and (not selected or not requested):
        config = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"
        try:
            text = config.read_text().split("[", 1)[0]
            match = re.search(r'^model\s*=\s*[\"\']([^\"\']+)', text, re.M)
            default = match.group(1) if match else ""
            selected = selected or default
            requested = requested or default
        except OSError:
            pass
    item = next((row for row in data.get("lanes", []) if row.get("lane_id") == lane), {})
    if not item:
        item = next((row for row in data.get("lanes", [])
                     if row.get("selector") == selected and row.get("harness") == wrapper), {})
    pool = item.get("quota_pool") or (wrapper if wrapper in {"codex", "claude"} else None)
    key = item.get("model_key") if item.get("selector") == selected else None
    if not key and wrapper == "agy" and item.get("model_key"):
        # AGY's effort is encoded in the selector suffix. Changing only that
        # suffix retains canonical family identity, not native provider proof.
        base = lambda value: re.sub(r"-(none|minimal|low|medium|high|xhigh|max|ultra)$", "", value or "")
        if base(selected) in {base(item.get("selector")), base(item["model_key"])}:
            key = item["model_key"]
    if not key:
        for model, card in (data.get("model_cards") or {}).items():
            if isinstance(card, dict) and card.get("pool") == pool and selected in (
                    model, card.get("run_as"), *(card.get("aliases") or [])):
                key = model
                break
    record = {"schema": SCHEMA, "run_id": str(uuid.uuid4()), "harness": wrapper,
            "requested_model": requested or None, "requested_role": role or None,
            "selected_model": key or selected or None, "selector": selected or None,
            "actual_model": None, "identity_source": "unconfirmed", "lane_id": lane or item.get("lane_id") or None,
            "quota_pool": pool, "started_at": now(),
            "dispatch_id": os.environ.get("CROSSFEED_DISPATCH_ID"),
            "afk_attempt_id": os.environ.get("AFK_ATTEMPT_ID")}
    record.update(selection_metadata(selection, model=record["selected_model"], lane_id=record["lane_id"],
                                     effort=effort, pool=pool, harness=wrapper, role=role, family=family))
    return record


def observed_model(wrapper, events, database=None):
    """Only native identity fields count. A model name in answer text is never evidence."""
    found = None
    thread_id = None
    session_id = None
    if not events or not events.is_file():
        return None
    for line in events.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        value = None
        if wrapper == "codex" and event.get("type") == "thread.started":
            candidate = event.get("thread_id")
            if isinstance(candidate, str) and re.fullmatch(r"[a-f0-9-]{36}", candidate):
                thread_id = candidate
        if wrapper == "copilot" and event.get("type") == "session.auto_mode_resolved":
            value = (event.get("data") or {}).get("chosenModel")
        elif wrapper == "claude" and event.get("type") == "system" and event.get("subtype") == "init":
            value = event.get("model")
        elif wrapper == "codex" and event.get("type") in {"session_meta", "turn_context"}:
            value = (event.get("payload") or {}).get("model")
        elif wrapper == "opencode":
            session_id = event.get("sessionID") or session_id
            part = event.get("part") or {}
            value = part.get("modelID")
            if value and part.get("providerID"):
                value = str(part["providerID"]) + "/" + str(value)
        elif wrapper == "pi":
            for message in pi_assistant_messages(event):
                model, provider = message.get("model"), message.get("provider")
                if isinstance(model, str) and MODEL_ID.fullmatch(model):
                    value = (provider + "/" + model if isinstance(provider, str)
                             and MODEL_ID.fullmatch(provider) else model)
        elif wrapper == "openrouter" and event.get("type") == "crossfeed.provider_model":
            value = event.get("model")
        if (isinstance(value, str) and MODEL_ID.fullmatch(value)
                and value not in {"auto", "default", "unknown", "openrouter/free", "github-copilot-auto"}):
            found = value
    if wrapper == "opencode" and not found and session_id and database and database.is_file():
        try:
            with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT json_extract(data, '$.providerID'), json_extract(data, '$.modelID') "
                                 "FROM message WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant' "
                                 "ORDER BY time_created DESC LIMIT 1", (session_id,)).fetchone()
            if row and all(isinstance(value, str) and MODEL_ID.fullmatch(value) for value in row):
                found = "/".join(row)
        except sqlite3.Error:
            pass  # No native identity available. Never guess from an unrelated session.
    if wrapper == "codex" and not found and thread_id:
        folder = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "sessions"
        # Session filenames carry the exact native thread id, so parallel runs cannot cross-identify.
        for session in folder.glob(f"**/*{thread_id}.jsonl"):
            found = observed_model("codex", session)
            if found:
                break
    return found


def answer(path):
    text = path.read_text()
    final = None
    stream = False
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in {"system", "assistant", "result"}:
            stream = True
            if event.get("type") == "result" and not event.get("is_error"):
                final = event.get("result")
    if stream:
        if not isinstance(final, str) or not final.strip():
            raise ValueError("no successful final result in Claude stream")
        return final
    return text.rstrip()  # Plain-output older CLIs/fakes remain supported, with unconfirmed identity.


def worker_notice(record):
    selected = record.get("selected_model") or "unknown (CLI default)"
    return (f"Crossfeed selected model: {selected}; CLI selector: {record.get('selector') or 'default'}. "
            f"Requested: {record.get('requested_model') or 'automatic selection'}. "
            "Use this selected identity when describing this dispatch. The provider may resolve an alias or "
            "Auto selector later; do not invent an underlying model. The caller receives a model receipt.\n"
            "=== CROSSFEED TASK ===\n")


def receipt(record):
    actual = record.get("actual_model") if not record.get("provider_identity_unconfirmed") else None
    ran = f"ran on {actual} (provider reported)" if actual else (
        f"selected {record.get('selected_model') or 'unknown'}; underlying model unconfirmed")
    fallback = record.get("pro_fallback")
    note = f" Pro fallback to {record.get('selector')}: {fallback['reason']}." if fallback else ""
    return (f"Crossfeed model receipt: requested {record.get('requested_model') or 'automatic selection'}; "
            f"{ran}; selector {record.get('selector') or 'default'}; "
            f"exit {record['returncode']}; run {record['run_id']}." + note)


def append(record):
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "runs.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (folder / "runs.jsonl").open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def finish(path, events, returncode, last, schema, database=None, result_file=None):
    record = json.loads(path.read_text())
    result = None
    invalid_result = False
    if schema and returncode == 0:
        try:
            result_path = result_file or last
            if result_path is None:
                raise ValueError("missing JSON result")
            result = result_path.read_text()
            json.loads(result)
        except (OSError, ValueError):
            invalid_result = True
            returncode = 8
            record["deliverable_error"] = "invalid JSON result"
    record["actual_model"] = observed_model(record["harness"], events, database)
    if record.get("provider_identity_unconfirmed"):
        # The relay echoes a configured routing label, not a backend identity.
        record["provider_reported_selector"] = record["actual_model"]
        record["actual_model"] = None
    record["identity_source"] = ("provider" if record["actual_model"] else
                                 "unconfirmed" if record["harness"] == "chatgpt-chat" or record.get("provider_identity_unconfirmed") else "selection-only")
    record.update(ended_at=now(), returncode=returncode,
                  status="ok" if returncode == 0 else "error")
    if returncode in {124, 125}:
        record["status"] = "timeout" if returncode == 124 else "idle-killed"
    # OpenCode retains its separate priced telemetry; Pi reports native usage here.
    record.update(observed_usage(record["harness"], events))
    if model_identity_drift(record):
        print(f"Crossfeed WARNING: MODEL IDENTITY DRIFT: selected {record['selected_model']}; "
              f"provider reported {record['actual_model']}; run {record['run_id']}.", file=sys.stderr)
    append(record)
    path.write_text(json.dumps(record, sort_keys=True) + "\n")
    if last:
        # Even a failed run gets a fresh sidecar; its old deliverable is not this run's answer.
        Path(str(last) + ".crossfeed.json").write_text(json.dumps(record, indent=2) + "\n")
    print(receipt(record), file=sys.stderr)
    if schema and returncode == 0:
        # Preserve the worker's schema and bytes; identity lives in the sidecar.
        print(result, end="")
    return 8 if invalid_result else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["begin", "prompt", "finish", "answer"])
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--wrapper")
    parser.add_argument("--requested", default="")
    parser.add_argument("--selected", default="")
    parser.add_argument("--lane", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--family")
    parser.add_argument("--effort")
    parser.add_argument("--selection-file", type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--returncode", type=int, default=0)
    parser.add_argument("--last", type=Path)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--schema", action="store_true")
    args = parser.parse_args()
    if args.action == "begin":
        args.path.write_text(json.dumps(begin(args.wrapper, args.requested, args.selected, args.lane, args.role,
                                             args.effort, args.selection_file, args.family)))
    elif args.action == "prompt":
        print(worker_notice(json.loads(args.path.read_text())), end="")
    elif args.action == "answer":
        print(answer(args.path))
    else:
        return finish(args.path, args.events, args.returncode, args.last, args.schema, args.database, args.result_file)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Crossfeed: model receipt failed: {exc}", file=sys.stderr)
        raise SystemExit(8)
