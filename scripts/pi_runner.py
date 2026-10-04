#!/usr/bin/env python3
"""Pi-specific worker supervisor. No login, provider probing, or network of its own."""
from __future__ import annotations

import argparse
import errno
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
# Closed list: providers requiring OAuth/ambient cloud credentials are never admitted.
KEY_ENV = {
    "google": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY", "zai": "ZAI_API_KEY",
    "zai-coding-cn": "ZAI_CODING_CN_API_KEY", "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY", "opencode-go": "OPENCODE_API_KEY",
    "opencode": "OPENCODE_API_KEY", "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY", "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY", "xai": "XAI_API_KEY",
}
THINKING = {value: value for value in ("off", "minimal", "low", "medium", "high", "xhigh")}
THINKING["max"] = "xhigh"


class Rejected(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def diagnostic(message):
    print("pi-agent: " + message, file=sys.stderr, flush=True)


def seconds(raw):
    if not raw.isdigit():
        raise argparse.ArgumentTypeError("must be a non-negative whole number of seconds")
    return int(raw)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    selectors_arg = parser.add_mutually_exclusive_group(required=True)
    selectors_arg.add_argument("--lane")
    selectors_arg.add_argument("--model")
    selectors_arg.add_argument("--model-key")
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path)
    parser.add_argument("--dir", type=Path, default=Path.cwd())
    parser.add_argument("--effort", choices=tuple(THINKING), default="medium")
    parser.add_argument("--effort-role", default="")
    parser.add_argument("--mode", choices=("ro", "rw"), default="ro")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--read-only", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--modality", default="text")
    parser.add_argument("--idle", "--idle-timeout", type=seconds, default=2400)
    parser.add_argument("--wall", "--timeout", type=seconds, default=0)
    parser.add_argument("--kill-after", type=seconds, default=30)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--last", type=Path)
    argv = sys.argv[1:]
    if argv[:1] == ["run"]:
        argv = argv[1:]
    args = parser.parse_args(argv)
    args.dir = args.dir.resolve()
    if not args.dir.is_dir():
        parser.error("--dir must name an existing directory")
    try:
        args.prompt = args.prompt_file.read_text() if args.prompt_file else args.prompt
    except (OSError, UnicodeError):
        parser.error("--prompt-file is not readable text")
    if not args.prompt.strip():
        parser.error("prompt must not be empty")
    for name in ("events", "last"):
        value = getattr(args, name)
        if value:
            setattr(args, name, value.resolve())
    paths = [p for p in (args.events, args.last, args.prompt_file.resolve() if args.prompt_file else None) if p]
    if len(set(paths)) != len(paths):
        parser.error("prompt-file, events and last must be separate paths")
    args.mode = "write" if args.write or args.mode == "rw" else "read-only"
    if args.read_only:
        args.mode = "read-only"
    return args


def roster_call(*args):
    try:
        result = subprocess.run([str(HERE / "roster.sh"), *args], capture_output=True, text=True)
    except OSError:
        raise Rejected(127, "roster.sh dependency unavailable")
    if result.returncode:
        raise Rejected(127 if result.returncode == 127 else 3, "roster dependency unavailable" if result.returncode == 127 else "lane or model rejected by roster")
    return result.stdout.strip()


def lane_for(args):
    if args.model and args.model.split("/", 1)[0] not in KEY_ENV:
        raise Rejected(5, "provider login is forbidden in Pi; use an explicit API-key provider")
    if args.model and args.model.startswith("anthropic/"):
        credential("anthropic")
    if args.modality != "text":
        raise Rejected(3, "only text prompts are supported")
    lane_id = args.lane or (roster_call("lookup", "pi", args.model) if args.model else
                           roster_call("resolve-lane", args.model_key, "pi"))
    try:
        lane = json.loads(roster_call("lane-json", lane_id))
    except ValueError:
        raise Rejected(3, "invalid roster lane")
    if not isinstance(lane, dict) or lane.get("harness") != "pi":
        raise Rejected(3, "selected lane must use the pi harness")
    roster_call("check-lane", lane_id, args.mode)
    model = lane.get("selector", "")
    if not isinstance(model, str) or "/" not in model or any(c.isspace() for c in model):
        raise Rejected(3, "lane selector must be provider/model")
    provider, model_id = model.split("/", 1)
    if not model_id or model_id.startswith("-") or lane.get("provider") != provider:
        raise Rejected(3, "invalid provider/model selector")
    return lane_id, model, provider


def credential(provider):
    variable = KEY_ENV.get(provider)
    if not variable:
        raise Rejected(5, "provider requires unsupported login or auth; Pi accepts explicit API keys only")
    # An explicit file wins over environment, and is never echoed or placed in argv.
    key_file = os.environ.get("PI_API_KEY_FILE") or os.environ.get(variable.removesuffix("_KEY") + "_KEY_FILE")
    if provider == "google":
        key_file = key_file or os.environ.get("GOOGLE_API_KEY_FILE")
    try:
        key = Path(key_file).read_text().strip() if key_file else os.environ.get(variable, "").strip()
    except (OSError, UnicodeError):
        raise Rejected(5, "API key file is unreadable")
    if not key or key.startswith("sk-ant-oat"):
        raise Rejected(5, "explicit API key required; subscription/OAuth credentials are forbidden")
    return variable, key


def signal_group(child, sig):
    try:
        os.killpg(child.pid, sig)
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        # macOS can deny a signal to a group whose leader has already exited.
        # poll() also reaps a zombie; permission errors on a live child remain fatal.
        if exc.errno == errno.EPERM:
            if child.poll() is not None:
                return False
            # TERM can have taken effect before waitpid observes the exit.
            try:
                child.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                raise exc
            return False
        raise


def terminate_group(child, grace):
    diagnostic("sending TERM to Pi process group")
    if not signal_group(child, signal.SIGTERM):
        child.wait()
        return
    end = time.monotonic() + grace
    while time.monotonic() < end:
        child.poll()  # Reap the group leader while descendants finish.
        if not signal_group(child, 0):
            child.wait()
            return
        time.sleep(0.05)
    diagnostic("KILL-AFTER LIMIT FIRED; sending KILL to Pi process group")
    signal_group(child, signal.SIGKILL)
    child.wait()


def assistant_text(events):
    terminal = None
    for event in events:
        if event.get("type") == "agent_end":
            terminal = event
    if terminal is None:
        raise Rejected(6, "no terminal agent_end event")
    if terminal.get("willRetry") is True:
        raise Rejected(6, "retry was pending at stream end")
    messages = terminal.get("messages")
    assistants = [m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"] if isinstance(messages, list) else []
    if not assistants:
        raise Rejected(6, "terminal event has no assistant message")
    message = assistants[-1]
    if message.get("stopReason") in ("error", "aborted"):
        raise Rejected(5, "provider returned an error or aborted response")
    if message.get("stopReason") not in ("stop", "length"):
        raise Rejected(6, "no completed terminal assistant message")
    content = message.get("content")
    text = "".join(part.get("text", "") for part in content
                   if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)) if isinstance(content, list) else ""
    if not text.strip():
        raise Rejected(7, "terminal answer is empty")
    return text.strip()


def catalog_cost(usage, pricing):
    total = pricing.get("request_usd", 0)
    for field in ("input", "output", "cacheRead", "cacheWrite"):
        tokens = usage.get(field, 0)
        if type(tokens) not in (int, float) or not math.isfinite(tokens) or tokens < 0:
            return None
        total += tokens * pricing[field] / 1000000
    if not any(field in usage for field in ("input", "output")):
        return None
    return {"total": total}


def supervise(args, command, env, events_file, key, interrupted):
    child, poller, terminated = None, None, False
    records, invalid = [], False
    def receive(label, raw):
        nonlocal invalid
        # Redact at line boundaries, so keys split across read() chunks cannot escape.
        text = raw.decode("utf-8", errors="replace")
        if key:
            text = text.replace(key, "[REDACTED]")
        if label == "stderr":
            print(text, file=sys.stderr, flush=True)
            return
        try:
            record = json.loads(text)
            if not isinstance(record, dict):
                raise ValueError()
        except ValueError:
            invalid = True
            return
        if getattr(args, "custom_api", False):
            # Generic discovery supplies no pricing, so Pi's zero defaults are unknown.
            # OpenRouter supplies catalog prices: derive the estimate from native token usage.
            messages = [record.get("message"), *(record.get("messages") or [])]
            for message in messages:
                if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                    usage = message["usage"]
                    usage.pop("cost", None)
                    if getattr(args, "custom_pricing", None):
                        cost = catalog_cost(usage, args.custom_pricing)
                        if cost is not None:
                            usage["cost"] = cost
        records.append(record)
        events_file.write(json.dumps(record) + "\n")
        events_file.flush()

    try:
        child = subprocess.Popen(command, cwd=args.dir, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        # Communicate prompt over stdin; argv contains neither prompt nor key.
        os.set_blocking(child.stdin.fileno(), False)
        pending_prompt = memoryview(args.prompt.encode())
        poller = selectors.DefaultSelector()
        poller.register(child.stdin, selectors.EVENT_WRITE, "stdin")
        for stream, label in ((child.stdout, "stdout"), (child.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            poller.register(stream, selectors.EVENT_READ, label)
        started = activity = tick = time.monotonic()
        pending = {"stdout": b"", "stderr": b""}
        while poller.get_map() or child.poll() is None or signal_group(child, 0):
            now = time.monotonic()
            reason = 128 + interrupted[0] if interrupted else 124 if args.wall and now - started >= args.wall else 125 if args.idle and now - activity >= args.idle else 0
            if reason:
                diagnostic(("EXTERNAL SIGNAL" if interrupted else "WALL-CLOCK LIMIT FIRED" if reason == 124 else "IDLE LIMIT FIRED") + "; no partial answer published")
                terminate_group(child, args.kill_after)
                terminated = True
                raise Rejected(reason, "Pi process group terminated")
            if now - tick >= 10:
                events_file.write(json.dumps({"type": "crossfeed.liveness", "elapsed_s": round(now - started, 1), "idle_s": round(now - activity, 1)}) + "\n")
                events_file.flush()
                tick = now
            for ready, _ in poller.select(0.1):
                label, stream = ready.data, ready.fileobj
                if label == "stdin":
                    try:
                        count = os.write(stream.fileno(), pending_prompt[:65536])
                        pending_prompt = pending_prompt[count:]
                    except BrokenPipeError:
                        pending_prompt = memoryview(b"")
                    if not pending_prompt:
                        poller.unregister(stream)
                        stream.close()
                    continue
                raw = os.read(stream.fileno(), 65536)
                if raw:
                    activity = time.monotonic()
                    pending[label] += raw
                    while b"\n" in pending[label]:
                        line, pending[label] = pending[label].split(b"\n", 1)
                        receive(label, line)
                else:
                    poller.unregister(stream)
                    stream.close()
                    if pending[label]:
                        receive(label, pending[label])
                        pending[label] = b""
        if child.wait() != 0:
            raise Rejected(5, "Pi exited with a provider/tool failure")
        if invalid:
            raise Rejected(5, "Pi emitted malformed JSON")
        return assistant_text(records)
    finally:
        if child is not None:
            child.poll()
            if not terminated and signal_group(child, 0):
                terminate_group(child, args.kill_after)
        if poller is not None:
            poller.close()
        if child is not None:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def run(args, interrupted):
    lane, model, provider = lane_for(args)
    configured = json.loads(roster_call("lane-json", lane))
    custom = configured.get("provider_source") and configured.get("transport", {}).get("api") == "openai-completions"
    if custom:
        try:
            from providers import resolve_key, api_base, ProviderError
            base = api_base(configured["transport"]["api_base"], "openai-compatible")
            if configured.get("auth", {}).get("kind") != "api-key" or configured["auth"].get("terms_class") != "api-only":
                raise ProviderError("API-key authentication required")
            key = resolve_key(configured["auth"].get("key_ref", ""))
            variable = "CROSSFEED_PROVIDER_KEY"
        except (ProviderError, KeyError, ValueError):
            raise Rejected(5, "custom API provider configuration or key reference is invalid") from None
    else:
        variable, key = credential(provider)
    binary = shutil.which("pi")
    if not binary:
        raise Rejected(127, "pi binary is not on PATH")
    for dependency in ("fleetctl.py", "run_identity.py"):
        if not (HERE / dependency).is_file():
            raise Rejected(127, dependency + " is required")
    args.custom_api = bool(custom)
    args.custom_pricing = configured.get("api_pricing") if custom else None
    if args.custom_pricing is not None:
        if (not isinstance(args.custom_pricing, dict)
                or any(type(args.custom_pricing.get(field)) not in (int, float)
                       or not math.isfinite(args.custom_pricing[field]) or args.custom_pricing[field] < 0
                       for field in ("input", "output", "cacheRead", "cacheWrite", "request_usd"))):
            raise Rejected(5, "API catalog pricing is invalid; check the connection again")
    thinking = "off" if custom else THINKING[args.effort]
    diagnostic("effort " + args.effort + " -> thinking " + thinking)
    with tempfile.TemporaryDirectory(prefix="pi-agent.") as scratch:
        work = Path(scratch)
        identity, events = work / "identity.json", args.events or work / "events.jsonl"
        env = os.environ.copy()
        for name in list(env):
            if name in KEY_ENV.values() or "OAUTH" in name or name.startswith(("GOOGLE_", "GCLOUD_", "PI_", "ANTHROPIC_")):
                env.pop(name, None)
        env[variable] = key
        env["PI_CODING_AGENT_DIR"] = str(work / "agent")
        if custom:
            agent = Path(env["PI_CODING_AGENT_DIR"])
            agent.mkdir(mode=0o700)
            (agent / "models.json").write_text(json.dumps({"providers": {provider: {
                "baseUrl": base, "api": "openai-completions", "apiKey": "${" + variable + "}" if key else "local-no-key",
                "models": [{"id": model.split("/", 1)[1], "reasoning": False, "input": ["text"],
                            **({"cost": {field: args.custom_pricing[field] for field in ("input", "output", "cacheRead", "cacheWrite")}}
                               if args.custom_pricing else {})}]
            }}}))
        # No inherited auth.json, arbitrary providers, skill/extension hooks or local trust files.
        command = [binary, "-p", "--mode", "json", "--no-session", "--no-extensions", "--no-skills",
                   "--no-prompt-templates", "--no-approve", "--provider", provider, "--model", model.split("/", 1)[1], "--thinking", thinking]
        if args.mode == "read-only":
            command += ["--tools", "read,grep,find,ls"]
        ttl = args.wall + args.kill_after + 60 if args.wall else 2147483647
        acquired = subprocess.run([str(HERE / "fleetctl.py"), "acquire", "--lane", lane, "--ttl", str(ttl)], capture_output=True, text=True)
        token = acquired.stdout.strip()
        if acquired.returncode or not token:
            raise Rejected(4, "quota lease refused, lane capacity or daily cap reached")
        status, output, begun = 5, None, False
        try:
            begin = subprocess.run([sys.executable, str(HERE / "run_identity.py"), "begin", "--path", str(identity),
                                    "--wrapper", "pi", "--requested", args.model or args.model_key or lane,
                                    "--selected", model, "--lane", lane, "--role", args.effort_role, "--effort",
                                    "provider-default" if custom else args.effort], capture_output=True)
            if begin.returncode:
                raise Rejected(5, "identity receipt initialization failed")
            begun = True
            record = json.loads(identity.read_text())
            record["native_effort"] = thinking
            if custom:
                record["effort"] = "provider-default"
                record["effort_shape"] = "none"
                record["usage_source"] = ("pi-native-tokens; OpenRouter catalog pricing estimate"
                                          if args.custom_pricing else "pi-native-tokens; API pricing unknown")
            identity.write_text(json.dumps(record) + "\n")
            if interrupted:
                raise Rejected(128 + interrupted[0], "external signal received before Pi launch")
            notice = subprocess.run([sys.executable, str(HERE / "run_identity.py"), "prompt", "--path", str(identity)], capture_output=True, text=True)
            if notice.returncode:
                raise Rejected(5, "identity prompt initialization failed")
            args.prompt = notice.stdout + "\n" + args.prompt
            with events.open("w", encoding="utf-8") as handle:
                output = supervise(args, command, env, handle, key, interrupted)
            if args.last:
                args.last.write_text(output + "\n")
            status = 0
        except Rejected as error:
            status = error.code
            diagnostic(error.message)
        except (OSError, ValueError) as error:
            diagnostic("Pi run or output artifact failed (errno " + str(getattr(error, "errno", None)) + ")")
            status = 5
        finally:
            if status == 0 and interrupted:
                status = 128 + interrupted[0]
            if begun:
                options = [sys.executable, str(HERE / "run_identity.py"), "finish", "--path", str(identity), "--events", str(events), "--returncode", str(status)]
                if args.last:
                    options += ["--last", str(args.last)]
                receipt_failed = False
                try:
                    receipt = subprocess.run(options, capture_output=True, text=True)
                    if receipt.stderr:
                        print(receipt.stderr.replace(key, "[REDACTED]") if key else receipt.stderr, file=sys.stderr, end="")
                    receipt_failed = receipt.returncode != 0
                except OSError:
                    receipt_failed = True
                if receipt_failed:
                    diagnostic("receipt/usage accounting failed")
                    if status == 0:
                        status = 5
            try:
                released = subprocess.run([str(HERE / "fleetctl.py"), "release", "--token", token], capture_output=True)
                release_failed = released.returncode != 0
            except OSError:
                release_failed = True
            if release_failed:
                diagnostic("WARNING quota lease release failed; recorded run outcome is unchanged")
        if status == 0 and interrupted:
            diagnostic("WARNING cancellation arrived during accounting; preserving the recorded completed outcome")
        if status == 0:
            sys.stdout.write(output + "\n")
        return status


def main():
    args = arguments()
    interrupted, previous = [], {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[signum] = signal.signal(signum, lambda signum, frame: interrupted.append(signum))
    try:
        return run(args, interrupted)
    except Rejected as error:
        diagnostic(error.message)
        return error.code
    except OSError:
        diagnostic("required dependency or output artifact unavailable")
        return 127
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
