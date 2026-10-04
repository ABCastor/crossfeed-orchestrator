#!/usr/bin/env python3
"""Run one read-only text request through an owner-configured ChatGPT gateway."""
from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import run_identity
import chatgpt_pro
from chatgpt_queue import lane_turn
from chatgpt_transport import Rejected, canonical_selector, request, settings

HERE = Path(__file__).resolve().parent


def seconds(value):
    if not value.isdigit():
        raise argparse.ArgumentTypeError("expected a nonnegative whole number of seconds")
    return int(value)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", required=True)
    prompt = parser.add_mutually_exclusive_group()
    prompt.add_argument("--prompt-file", type=Path)
    prompt.add_argument("--prompt")
    parser.add_argument("--dir", type=Path, default=Path.cwd())
    parser.add_argument("--effort")
    parser.add_argument("--effort-role", default="")
    parser.add_argument("--mode", default="ro")
    parser.add_argument("--modality", default="text")
    parser.add_argument("--idle", type=seconds, default=0)
    parser.add_argument("--wall", type=seconds, default=0)
    parser.add_argument("--kill-after", type=seconds, default=2)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--last", type=Path)
    parser.add_argument("--idempotency-key", help="reuse this key and identical prompt/options for an explicit retry")
    argv = sys.argv[1:]
    action = argv.pop(0) if argv and argv[0] in {"run", "health"} else "run"
    args = parser.parse_args(argv)
    args.action = action
    if args.idempotency_key is not None and (not args.idempotency_key or len(args.idempotency_key) > 128
            or not all(c.isascii() and (c.isalnum() or c in "-._:") for c in args.idempotency_key)):
        parser.error("--idempotency-key must be 1-128 ASCII letters, digits or -._:")
    args.dir = args.dir.resolve()
    if not args.dir.is_dir():
        parser.error("--dir must name an existing directory")
    for name in ("events", "last", "prompt_file"):
        value = getattr(args, name)
        if value:
            setattr(args, name, value.resolve())
    paths = [p for p in (args.events, args.last, args.prompt_file,
                         Path(str(args.last) + ".crossfeed.json") if args.last else None) if p]
    if len(set(paths)) != len(paths):
        parser.error("prompt, events, final and receipt must have separate paths")
    if action == "run":
        try:
            args.prompt = args.prompt_file.read_text() if args.prompt_file else args.prompt
        except (OSError, UnicodeError):
            parser.error("--prompt-file must be readable text")
        if not args.prompt or not args.prompt.strip():
            parser.error("--prompt-file or --prompt with nonempty text is required")
    return args


def lane_for(args):
    roster = run_identity.roster(discover=True)
    lane = next((row for row in roster.get("lanes", [])
                 if row.get("lane_id") == args.lane), None)
    if args.lane.startswith("chatgpt:") and roster.get("chatgpt_catalog", {}).get("error"):
        raise Rejected(roster["chatgpt_catalog"].get("error_code") or 6, roster["chatgpt_catalog"]["error"])
    if not lane or lane.get("harness") != "chatgpt-chat":
        raise Rejected(3, "unknown Crossfeed Chat lane")
    chatgpt_pro.refresh(roster, run_identity.state_dir())
    runtime = json.loads((run_identity.state_dir() / "runtime.json").read_text()) if (run_identity.state_dir() / "runtime.json").exists() else {}
    reason = chatgpt_pro.blocked(roster, runtime, lane)
    if reason:
        choices = chatgpt_pro.replacements(roster, runtime, lane)
        if getattr(args, "effort_role", ""):
            choices = [choice for choice in choices if args.effort_role in choice.get("roles", [])]
        if not choices:
            raise Rejected(4, "Pro unavailable; no admitted Extra High or High replacement")
        original = lane
        lane = dict(choices[0], pro_fallback=chatgpt_pro.fallback(original, reason))
        args.lane = lane["lane_id"]
        args.idempotency_key = None
    if lane.get("gateway_status", {}).get("quota_blocked") or lane.get("gateway_status", {}).get("rate_limited"):
        raise Rejected(4, "Crossfeed Chat worker quota paused")
    if args.mode not in {"ro", "read-only"} or args.modality != "text":
        raise Rejected(3, "Crossfeed Chat supports only read-only text")
    if args.effort not in {None, "service-chosen"}:
        raise Rejected(3, "thinking level is owner-configured in ChatGPT; choose a different lane")
    if (lane.get("access_status") != "verified" or lane.get("admission_status") != "active"
            or "read-only" not in lane.get("allowed_modes", [])
            or "text" not in lane.get("capabilities", {}).get("input", [])):
        raise Rejected(3, "lane has not been admitted for read-only text")
    if (type(lane.get("max_parallel")) is not int or lane["max_parallel"] < 1
            or not canonical_selector(lane.get("selector"))
            or lane.get("selector") != "chatgpt:" + str(lane.get("worker_label"))):
        raise Rejected(3, "lane must declare a positive worker capacity and its configured model label")
    return lane


def http_child(pipe, base, key, payload, idempotency_key):
    os.setsid()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_DFL)
    try:
        pipe.send(("ready", None))
        answer = request(base, key, "/chat/completions", payload, timeout=86400,
                         progress=lambda: pipe.send(("bytes", None)), idempotency_key=idempotency_key)
        pipe.send(("result", answer))
    except Rejected as error:
        pipe.send(("error", (error.code, error.message, error.pro_spent, error.reset_at)))
    except Exception:
        pipe.send(("error", (5, "HTTP transport failed", False, None)))
    finally:
        pipe.close()


@contextlib.contextmanager
def preflight_budget(deadline):
    """Interrupt blocking admission calls inside the same wall budget."""
    if deadline is None:
        yield
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Rejected(124, "wall timeout during preflight")
    def expired(number, frame):
        raise Rejected(124, "wall timeout during preflight")
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    saved_at = time.monotonic()
    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, remaining)
        yield
        if time.monotonic() >= deadline:
            raise Rejected(124, "wall timeout during preflight")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if previous_timer[0]:
            restored = max(.000001, previous_timer[0] - (time.monotonic() - saved_at))
            signal.setitimer(signal.ITIMER_REAL, restored, previous_timer[1])


def supervise(args, base, key, payload, events, interrupted, idempotency_key, lane=None):
    deadline = getattr(args, "wall_deadline", None)
    if deadline is not None and time.monotonic() >= deadline:
        raise Rejected(124, "wall timeout during preflight")
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=http_child, args=(sender, base, key, payload, idempotency_key))
    child.start()
    sender.close()
    started = progress = time.monotonic()
    group_ready = False
    try:
        while True:
            now = time.monotonic()
            if interrupted:
                raise Rejected(128 + interrupted[0], "external cancellation")
            if args.wall and now - started >= args.wall:
                raise Rejected(124, "wall timeout")
            if deadline is not None and now >= deadline:
                raise Rejected(124, "wall timeout")
            if args.idle and now - progress >= args.idle:
                raise Rejected(125, "idle timeout; buffered HTTP bytes are the only progress signal")
            cutoff = deadline if deadline is not None else started + args.wall if args.wall else None
            poll_wait = min(.05, max(0, cutoff - now)) if cutoff is not None else .05
            if receiver.poll(poll_wait):
                try:
                    kind, value = receiver.recv()
                except EOFError:
                    raise Rejected(6, "HTTP child exited without terminal result")
                if kind == "ready":
                    group_ready = True
                    events.write(json.dumps({"type": "crossfeed.transport", "state": "waiting", "buffered": True}) + "\n")
                    events.flush()
                elif kind == "bytes":
                    progress = time.monotonic()
                elif kind == "error":
                    raise Rejected(value[0], value[1], pro_spent=value[2], reset_at=value[3])
                elif kind == "result":
                    if cutoff is not None and time.monotonic() >= cutoff:
                        raise Rejected(124, "wall timeout")
                    choices = value.get("choices")
                    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else {}
                    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
                        raise Rejected(6, "no successful terminal stop")
                    message = choice.get("message", {})
                    if not isinstance(message, dict) or message.get("tool_calls"):
                        raise Rejected(6, "tool response refused")
                    output = message.get("content")
                    if not isinstance(output, str) or not output.strip():
                        raise Rejected(7, "empty final response")
                    if value.get("model") != payload["model"]:
                        raise Rejected(6, "gateway model does not match the requested worker label")
                    metadata = value.get("metadata")
                    metadata = metadata if isinstance(metadata, dict) else {}
                    picker = metadata.get("picker_receipt")
                    if lane and lane.get("pro_fallback") and isinstance(picker, dict) and type(picker.get("level")) is int and picker["level"] < 2:
                        raise Rejected(4, "Pro replacement picker is below High; response refused")
                    if lane and chatgpt_pro.is_pro(lane):
                        lower = isinstance(picker, dict) and (
                            (type(picker.get("level")) is int and picker["level"] != 4)
                            or (isinstance(picker.get("row"), str) and lane.get("worker_row")
                                and picker["row"] != lane["worker_row"]))
                        if lower or chatgpt_pro.LIMIT_REPORT.search(output):
                            raise Rejected(4, "Pro worker reported a downgrade or rate limit", pro_spent=True,
                                           reset_at=chatgpt_pro.reset_at(output, metadata.get("reset_at")))
                    events.write(json.dumps({"type": "crossfeed.terminal", "finish_reason": "stop"}) + "\n")
                    return output.replace(key, "[REDACTED]"), picker

            elif not child.is_alive():
                raise Rejected(6, "HTTP child exited without terminal result")
    finally:
        # Keyed jobs stay available for an explicit identical-payload retry.
        # Killing this HTTP caller does not cancel remote ChatGPT generation.
        child.join(.1)
        if child.is_alive():
            try:
                if group_ready:
                    os.killpg(child.pid, signal.SIGTERM)
                else:
                    child.terminate()
            except ProcessLookupError:
                pass
            child.join(args.kill_after)
        if child.is_alive():
            try:
                if group_ready:
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()
            except ProcessLookupError:
                pass
            child.join()
        receiver.close()


def run(args):
    started = time.monotonic()
    args.wall_deadline = started + args.wall if args.wall else None
    interrupted = []
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda number, frame: interrupted.append(number))
    requested_lane = args.lane
    with preflight_budget(args.wall_deadline):
        lane = lane_for(args)
    note_at = started + 60
    def check():
        nonlocal note_at
        if interrupted:
            raise Rejected(128 + interrupted[0], "external cancellation")
        now = time.monotonic()
        if args.wall and now - started >= args.wall:
            raise Rejected(124, "wall timeout while waiting for lane")
        if now >= note_at:
            print("chatgpt-agent: waiting for lane " + args.lane, file=sys.stderr, flush=True)
            note_at = now + 60
    if args.action == "health":
        with preflight_budget(args.wall_deadline):
            settings(lane)
        check()
        print("Crossfeed Chat gateway admitted; configured " + lane["model_key"] + "; identity unconfirmed", file=sys.stderr)
        return 0
    tried = set()
    while True:
        tried.add(args.lane)
        args.pro_downgraded = False
        with lane_turn(args.lane, check) as release_turn:
            try:
                status = run_turn(args, lane, interrupted, started, check, release_turn)
            except Rejected as error:
                if not (chatgpt_pro.is_pro(lane) or lane.get("pro_fallback")) or error.code != 4:
                    raise
                args.pro_downgraded = True
                status = error.code
        if status == 0 or interrupted or status in {124, 125, 129, 130, 143}:
            return status
        if not args.pro_downgraded and not lane.get("pro_fallback"):
            return status
        with preflight_budget(args.wall_deadline):
            roster = run_identity.roster(discover=True)
        runtime_path = run_identity.state_dir() / "runtime.json"
        runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
        original_name = lane.get("pro_fallback", {}).get("requested_model") or requested_lane
        original = next((row for row in roster.get("lanes", []) if original_name in (row["lane_id"], row["model_key"])), lane)
        choices = [row for row in chatgpt_pro.replacements(roster, runtime, original)
                   if row["lane_id"] not in tried and (not args.effort_role or args.effort_role in row.get("roles", []))]
        if not choices:
            return status
        lane = dict(choices[0], pro_fallback=chatgpt_pro.fallback(original,
                    chatgpt_pro.blocked(roster, runtime, original) or "Extra High replacement failed"))
        args.lane = lane["lane_id"]
        # A fallback is a different payload, never reuse the Pro job's key.
        args.idempotency_key = None
        print("chatgpt-agent: Pro fallback selected " + lane["selector"] + "; " + lane["pro_fallback"]["reason"], file=sys.stderr)


def run_turn(args, lane, interrupted, started, check, release_turn):
    # FIFO orders lease acquisition only. Holding its ticket during HTTP would
    # serialize every replica. Each acquire probes the current catalog, so a
    # queued task also sees capacity changes without a cached roster snapshot.
    ttl = args.wall + args.kill_after + 60 if args.wall else 2147483647
    while True:
        check()
        with subprocess.Popen([sys.executable, str(HERE / "fleetctl.py"), "acquire", "--lane", args.lane,
                               "--ttl", str(ttl), "--pid", str(os.getpid())],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            while True:
                try:
                    out, err = child.communicate(timeout=.05)
                    break
                except subprocess.TimeoutExpired:
                    try:
                        check()
                    except BaseException:
                        child.kill()
                        child.communicate()
                        raise
        token = out.strip()
        if child.returncode == 0 and token:
            break
        # Capacity can clear; admission, switches and spend refusals cannot.
        if "active lease(s), cap is" not in err:
            raise Rejected(4, "quota lease refused")
        time.sleep(.1)
    release_turn()
    status, output, begun = 5, None, False
    with tempfile.TemporaryDirectory(prefix="chatgpt-agent.") as scratch:
        identity = Path(scratch) / "identity.json"
        events = args.events or Path(scratch) / "events.jsonl"
        try:
            check()
            # Completion POST lets Crossfeed Chat wake a sleeping saved label.
            with preflight_budget(args.wall_deadline):
                base, key = settings(lane)
            check()
            if chatgpt_pro.is_pro(lane):
                # Admission can change while the task waits for its lane turn.
                with preflight_budget(args.wall_deadline):
                    roster = run_identity.roster(discover=True)
                runtime_path = run_identity.state_dir() / "runtime.json"
                runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
                fresh = next((row for row in roster.get("lanes", []) if row["lane_id"] == lane["lane_id"]), None)
                if (not fresh or fresh.get("admission_status") != "active" or fresh.get("access_status") != "verified"
                        or chatgpt_pro.blocked(roster, runtime, fresh)):
                    args.pro_downgraded = True
                    raise Rejected(4, "Pro allowance or admission changed before dispatch")
            record = run_identity.begin("chatgpt-chat", lane["model_key"], lane["selector"], args.lane,
                                        args.effort_role, lane.get("worker_level", "service-chosen"))
            fallback = lane.get("pro_fallback")
            if not fallback and record.get("selection"):
                fallback = record["selection"].get("choice", {}).get("pro_fallback")
            if fallback:
                lane["pro_fallback"] = fallback
                record.update(pro_fallback=fallback, requested_model=fallback["requested_model"])
            record.update(configured_model=lane["model_key"], native_effort=None,
                          effort_shape="none", usage_source="unavailable", cancellation_scope="local-http-only")
            record["idempotency_key"] = args.idempotency_key or record["run_id"]
            identity.write_text(json.dumps(record))
            begun = True
            payload = {"model": lane["selector"], "messages": [{"role": "user", "content":
                       run_identity.worker_notice(record) + args.prompt}], "tool_choice": "none", "stream": False}
            with events.open("w") as handle:
                output, receipt = supervise(args, base, key, payload, handle, interrupted, record["idempotency_key"], lane)
            if isinstance(receipt, dict):
                # Retain observations without copying arbitrary gateway text.
                clean = {}
                if type(receipt.get("level")) is int and 0 <= receipt["level"] <= 4:
                    clean["level"] = receipt["level"]
                if receipt.get("source") in {"worker", "extension"}:
                    clean["source"] = receipt["source"]
                if receipt.get("row") == lane.get("worker_row"):
                    clean["row"] = lane["worker_row"]
                woke_at = chatgpt_pro.wake_stamp(receipt)
                if woke_at is not None:
                    clean["observed_at"] = woke_at
                record["picker_receipt"] = clean
                identity.write_text(json.dumps(record))
            if interrupted:
                raise Rejected(128 + interrupted[0], "external cancellation")
            if args.last:
                args.last.write_text(output + "\n")
            status = 0
        except Rejected as error:
            if chatgpt_pro.is_pro(lane) and (error.pro_spent
                    or chatgpt_pro.RATE_LIMIT.search(error.message)):
                chatgpt_pro.mark_spent(run_identity.state_dir(), lane, until=error.reset_at or chatgpt_pro.reset_at(error.message))
                args.pro_downgraded = True
            status = error.code
            print("chatgpt-agent: " + error.message, file=sys.stderr)
        except (OSError, ValueError):
            print("chatgpt-agent: run or artifact persistence failed", file=sys.stderr)
            status = 5
        finally:
            if begun:
                try:
                    run_identity.finish(identity, events, status, args.last, None)
                except (OSError, ValueError):
                    print("chatgpt-agent: receipt persistence failed", file=sys.stderr)
                    status = 5 if status == 0 else status
            released = subprocess.run([sys.executable, str(HERE / "fleetctl.py"), "release", "--token", token], capture_output=True)
            if released.returncode:
                print("chatgpt-agent: WARNING lease release failed", file=sys.stderr)
    if status == 0 and interrupted:
        print("chatgpt-agent: WARNING cancellation arrived during accounting; preserving the recorded completed outcome", file=sys.stderr)
    if status == 0:
        sys.stdout.write(output + "\n")
    return status


def main():
    try:
        return run(arguments())
    except Rejected as error:
        print("chatgpt-agent: " + error.message, file=sys.stderr)
        return error.code
    except (OSError, ValueError):
        print("chatgpt-agent: configuration or dependency unavailable", file=sys.stderr)
        return 5
    except Exception:
        print("chatgpt-agent: worker failed unexpectedly; see local diagnostics", file=sys.stderr)
        return 6


if __name__ == "__main__":
    sys.exit(main())
