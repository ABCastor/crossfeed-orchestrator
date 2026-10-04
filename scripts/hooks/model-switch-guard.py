#!/usr/bin/env python3
"""model-switch-guard: a Claude Code PreToolUse hook that keeps agents from starting a model
that was switched off in the Crossfeed console.

Why: a model was switched off, an agent ran `codex-agent.sh --model gpt-6-astra`, the wrapper ran
GPT-6 Sol instead and said so only on stderr, and the agent's command and report both named Astra.
The wrappers stand in; this hook makes the agent learn it BEFORE it runs anything, so it rewrites
its own command and its report names the model that really runs.

What it checks (read-only, stdlib only):
  Bash   a command that starts an agent with a model: any Crossfeed wrapper (codex-agent.sh,
         claude-agent.sh, agy-agent.sh, opencode-agent.sh, openrouter-agent.sh, gemini-media.sh,
         gemini-image.sh, and the tasks file of fanout.sh), and the raw CLIs (codex exec -m,
         claude --model, opencode run -m, agy --model). A codex-agent.sh or `codex exec` with no
         model runs Codex's configured default, which is checked too. An agent switching a model or
         provider back on (fleetctl.py model-toggle ... on, model-choice, level, switch, pins undo)
         is refused as well: the switches belong to whoever runs the fleet.
  Agent  a subagent `model` (opus, sonnet, haiku, fable) that is off on the Claude pool.
A refusal names what to write instead. Nothing else is ever blocked.

The switches are read through fleetctl.py beside this folder, from the same roster and state
paths the wrappers use (ACCESS_OVERLAY, FLEET_STATE_DIR, XDG_*; a command's own `VAR=value` prefix
or `export` is honoured), so the hook and the console cannot disagree. It fails OPEN: any error,
an unreadable roster, or a command it cannot parse lets the tool call through untouched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

# Cheap test before any parsing or import: most Bash commands never name one of these.
PREFILTER = re.compile(
    r"(?:codex|claude|agy|opencode|openrouter)-agent\.sh|gemini-(?:media|image)\.sh|fanout\.sh|fleetctl\.py"
    r"|(?:^|[\s/;&|(`'\"])(?:codex|claude|opencode|agy)(?:\s|$)"
)
WRAPPER_POOL = {"codex-agent.sh": "codex", "claude-agent.sh": "claude"}
LANE_WRAPPERS = {"agy-agent.sh": "agy", "opencode-agent.sh": "opencode", "openrouter-agent.sh": "openrouter"}
GEMINI_TRANSPORTS = {"gemini-media.sh", "gemini-image.sh"}
GEMINI_CHEAP = "gemini-3.1-flash-image"   # gemini-image.sh --cheap
PREFIXES = {"env", "command", "exec", "nohup", "time", "builtin", "caffeinate", "stdbuf"}
SHELLS = {"bash", "sh", "zsh", "dash"}
ENV_NAMES = ("ACCESS_OVERLAY", "FLEET_STATE_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "CODEX_HOME")
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
OPERATORS = set(";&|()<>")
AGENT_ALIASES = ("opus", "sonnet", "haiku", "fable")


# ---- reading a shell command ---------------------------------------------------------------------
def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: text fed to a program on stdin is data, not a command."""
    out, lines, index = [], command.split("\n"), 0
    while index < len(lines):
        line = lines[index]
        out.append(line)
        match = re.search(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2", line)
        index += 1
        if match:
            end, dash = match.group(3), match.group(1) == "-"
            while index < len(lines) and (lines[index].lstrip("\t") if dash else lines[index]) != end:
                index += 1
            index += 1
    return "\n".join(out)


def unquoted_newlines_to_semicolons(command: str) -> str:
    """Newlines end a command unless they sit inside quotes or follow a backslash."""
    out, quote, index = [], None, 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            if command[index + 1] == "\n":
                out.append(" ")
            else:
                out.append(command[index:index + 2])
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "\n":
            char = ";"
        elif char == "#" and (not out or out[-1] in " \t;"):
            newline = command.find("\n", index)
            index = len(command) if newline < 0 else newline
            continue
        out.append(char)
        index += 1
    return "".join(out)


def segments(command: str) -> list[list[str]]:
    """Simple commands, as token lists, split at ; & && || | ( )."""
    lexer = shlex.shlex(unquoted_newlines_to_semicolons(strip_heredocs(command)), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""   # comments are already gone; a # inside a word is part of the word
    found, current = [], []
    for token in lexer:
        if token and set(token) <= OPERATORS:
            if current:
                found.append(current)
            current = []
        else:
            current.append(token)
    if current:
        found.append(current)
    return found


def program_of(tokens: list[str], env: dict[str, str]) -> tuple[str, list[str]] | None:
    """(program basename, its arguments) after env assignments and prefixes like env, timeout, bash."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        assignment = ASSIGNMENT.match(token)
        if assignment:
            env[assignment.group(1)] = assignment.group(2)
            index += 1
            continue
        name = os.path.basename(token)
        if name in PREFIXES:
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            continue
        if name in {"timeout", "gtimeout", "nice"}:
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 2 if tokens[index] in {"-k", "-s", "-n", "--kill-after", "--signal"} else 1
            if name != "nice":
                index += 1   # the duration
            continue
        if name in SHELLS or name in {"python", "python3"}:
            rest = tokens[index + 1:]
            if name in SHELLS and rest[:1] == ["-c"] and len(rest) > 1:
                return ("-c", [rest[1]])
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            if not rest:
                return None
            return os.path.basename(rest[0]), rest[1:]
        return name, tokens[index + 1:]
    return None


def option(args: list[str], *names: str) -> str | None:
    """The value of the last --name VALUE or --name=VALUE among args."""
    value = None
    for index, arg in enumerate(args):
        for name in names:
            if arg == name and index + 1 < len(args):
                value = args[index + 1]
            elif name.startswith("--") and arg.startswith(name + "="):
                value = arg[len(name) + 1:]
    return value


def codex_config_model(args: list[str]) -> str | None:
    """A model set with -c/--config model=... on a codex command line."""
    value = None
    for index, arg in enumerate(args):
        setting = None
        if arg in {"-c", "--config"} and index + 1 < len(args):
            setting = args[index + 1]
        elif arg.startswith("--config="):
            setting = arg[len("--config="):]
        if setting:
            match = re.match(r"^\s*model\s*=\s*(.+?)\s*$", setting)
            if match:
                value = match.group(1).strip("\"'")
    return value


# ---- the switches, through fleetctl ------------------------------------------------------------------
class Switches:
    def __init__(self, env: dict[str, str]):
        spec = importlib.util.spec_from_file_location(
            "crossfeed_fleetctl", Path(__file__).resolve().parent.parent / "fleetctl.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.fleet = module
        self.env = env
        overlay = Path(env.get("ACCESS_OVERLAY") or module.DEFAULT_OVERLAY).expanduser()
        state = Path(env.get("FLEET_STATE_DIR") or module.DEFAULT_STATE_DIR).expanduser()
        self.roster = module.read_overlay(overlay)
        self.runtime = module.load_json(state / "runtime.json", {}) or {}

    def verdict(self, name: str | None, **where: Any) -> dict[str, Any] | None:
        if not name:
            return None
        return self.fleet.start_verdict(self.roster, self.runtime, name, **where)

    def lane(self, lane_id: str | None) -> dict[str, Any] | None:
        return self.fleet.lane_map(self.roster).get(lane_id) if lane_id else None

    def lane_by_key(self, key: str | None, harness: str) -> dict[str, Any] | None:
        return next((lane for lane in self.roster.get("lanes", [])
                     if key and lane.get("model_key") == key and lane.get("harness") == harness), None)

    def codex_default(self) -> str | None:
        saved = os.environ.get("CODEX_HOME")
        try:
            if self.env.get("CODEX_HOME"):
                os.environ["CODEX_HOME"] = self.env["CODEX_HOME"]
            return self.fleet.codex_default_model()
        finally:
            if saved is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = saved

    def lane_verdict(self, lane_id: str | None, harness: str) -> dict[str, Any] | None:
        lane = self.lane(lane_id)
        if not lane or not lane.get("model_key"):
            return None
        return self.verdict(lane["model_key"], harness=lane.get("harness") or harness)


def said(verdict: dict[str, Any], what: str, flag: str | None = None) -> str:
    """One refusal in plain words: what the command asked for, why it cannot start, what to write instead."""
    if flag and verdict["instead"]:
        text = f"{what}: {verdict['reason']} Write {flag} {verdict['instead'][0]}, the model that runs in its place."
        if verdict["why"] != "retired":
            text += " Only the owner can switch it back on, in the console."
        return text
    return f"{what}: {verdict['message']}"


# ---- what one simple command would start --------------------------------------------------------
def check_segment(program: str, args: list[str], env: dict[str, str], cwd: str, load) -> list[str]:
    problems: list[str] = []

    if program in WRAPPER_POOL:
        pool = WRAPPER_POOL[program]
        asked = option(args, "--model")
        switches = load()
        if asked:
            verdict = switches.verdict(asked, pool=pool)
            if verdict:
                problems.append(said(verdict, f"{program} --model {asked}", "--model"))
        else:
            default = "opus" if pool == "claude" else switches.codex_default()
            verdict = switches.verdict(default, pool=pool)
            if verdict:
                problems.append(said(verdict, f"{program} with no --model runs {default}", "--model"))
    elif program in LANE_WRAPPERS:
        harness = LANE_WRAPPERS[program]
        switches = load()
        lane_id, key, model = option(args, "--lane"), option(args, "--model-key"), option(args, "--model")
        if model:
            verdict = switches.verdict(model, harness=harness)
            if verdict:
                problems.append(said(verdict, f"{program} --model {model}"))
        if key:
            lane = switches.lane_by_key(key, harness)
            verdict = switches.lane_verdict(lane["lane_id"], harness) if lane else None
            if verdict:
                problems.append(said(verdict, f"{program} --model-key {key}"))
        if lane_id:
            verdict = switches.lane_verdict(lane_id, harness)
            if verdict:
                problems.append(said(verdict, f"{program} --lane {lane_id}") + " Or drop --lane and let the router pick.")
    elif program in GEMINI_TRANSPORTS:
        model = option(args, "--model") or (GEMINI_CHEAP if program == "gemini-image.sh" and "--cheap" in args else None)
        if model:
            verdict = load().verdict(model, provider="google")
            if verdict:
                problems.append(f"{program} --model {model}: {verdict['reason']}")
    elif program == "codex":
        words = [arg for arg in args if not arg.startswith("-")]
        runs = bool(words) and words[0] in {"exec", "e"}
        asked = option(args, "-m", "--model") or codex_config_model(args)
        if asked or runs:
            switches = load()
            default = None if asked else switches.codex_default()
            verdict = switches.verdict(asked or default, pool="codex")
            if verdict:
                what = f"codex -m {asked}" if asked else f"codex exec with no -m runs {default}"
                problems.append(said(verdict, what, "-m"))
    elif program == "claude":
        for flag in ("--model", "--fallback-model"):
            asked = option(args, flag)
            verdict = load().verdict(asked, pool="claude") if asked else None
            if verdict:
                problems.append(said(verdict, f"claude {flag} {asked}", flag))
    elif program == "opencode":
        asked = option(args, "-m", "--model")
        verdict = load().verdict(asked, harness="opencode") if asked else None
        if verdict:
            problems.append(said(verdict, f"opencode -m {asked}"))
    elif program == "agy":
        asked = option(args, "--model")
        verdict = load().verdict(asked, harness="agy") if asked else None
        if verdict:
            problems.append(said(verdict, f"agy --model {asked}"))
    elif program == "fanout.sh":
        problems.extend(check_fanout(args, env, cwd, load))
    elif program == "fleetctl.py":
        problems.extend(check_switch_change(args, env))
    return problems


def check_fanout(args: list[str], env: dict[str, str], cwd: str, load) -> list[str]:
    positional = [arg for index, arg in enumerate(args)
                  if not arg.startswith("-") and (index == 0 or args[index - 1] not in
                                                  {"--agent", "--parallel", "--timeout", "--out"})]
    if not positional:
        return []
    path = Path(os.path.expanduser(positional[0]))
    path = path if path.is_absolute() else Path(cwd) / path
    if not path.is_file() or path.stat().st_size > 1_000_000:
        return []
    default_agent = option(args, "--agent") or "codex"
    switches, problems = None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            task = json.loads(line)
        except ValueError:
            continue
        if not isinstance(task, dict):
            continue
        agent, name = task.get("agent") or default_agent, task.get("id") or "a task"
        switches = switches or load()
        verdict = None
        if agent in {"codex", "claude"}:
            asked = task.get("model") or ("opus" if agent == "claude" else switches.codex_default())
            verdict = switches.verdict(asked, pool=agent)
            if verdict:
                where = f"fanout task {name} ({path.name}) " + (
                    f'"model": "{asked}"' if task.get("model") else f"names no model, so it runs {asked}")
                problems.append(said(verdict, where, '"model":'))
                continue
        elif agent in {"opencode", "agy", "openrouter"}:
            if task.get("lane_id"):
                verdict = switches.lane_verdict(task["lane_id"], agent)
            elif task.get("model_key"):
                lane = switches.lane_by_key(task["model_key"], agent)
                verdict = switches.lane_verdict(lane["lane_id"], agent) if lane else None
            elif task.get("model"):
                verdict = switches.verdict(task["model"], harness=agent)
        if verdict:
            problems.append(said(verdict, f"fanout task {name} ({path.name})"))
    return problems


def check_switch_change(args: list[str], env: dict[str, str]) -> list[str]:
    """An agent must not turn back on what the person running the fleet turned off. Turning things off
    is always fine, and a command aimed at another roster or state folder (a test fixture) is not
    aimed at the live fleet."""
    if env.get("__set_here__") or any(
            arg in {"--overlay", "--state-dir"} or arg.startswith(("--overlay=", "--state-dir=")) for arg in args):
        return []
    words = [arg for arg in args if not arg.startswith("-")]
    if not words:
        return []
    command, rest = words[0], words[1:]
    refused = (
        (command == "model-toggle" and rest[-1:] == ["on"] and len(rest) == 3)
        or (command == "model-choice" and len(rest) == 2)
        or (command == "level" and len(rest) == 2 and rest[1] != "off")
        or (command == "switch" and len(rest) == 2 and rest[1] != "off")
        or (command == "pins" and rest[:1] == ["undo"])
    )
    if not refused:
        return []
    return [f"fleetctl.py {' '.join(words)}: the model and provider switches are the owner's, set in the Crossfeed "
            "console. An agent may switch things off, never back on. Use a model that is on (fleetctl.py brief "
            "names what is off and what runs instead), or ask the owner."]


def check_bash(command: str, cwd: str, env: dict[str, str], cache: dict[str, Any], depth: int = 0,
               base: dict[str, str] | None = None) -> list[str]:
    """Every refusal for a Bash command. `env` is what the command runs with so far; `base` is the
    hook's own environment, so a roster or state folder the command itself points at can be told apart."""
    base = dict(env) if base is None else base
    if not command or not PREFILTER.search(command) or depth > 2:
        return []
    problems: list[str] = []
    exported: dict[str, str] = dict(env)
    for tokens in segments(command):
        if tokens[:1] == ["export"]:
            for token in tokens[1:]:
                match = ASSIGNMENT.match(token)
                if match and match.group(1) in ENV_NAMES:
                    exported[match.group(1)] = match.group(2)
            continue
        local = dict(exported)
        found = program_of(tokens, local)
        if not found:
            continue
        program, args = found
        if program == "-c":
            problems.extend(check_bash(args[0], cwd, local, cache, depth + 1, base))
            continue
        if tokens[:1] == ["cd"] or program == "cd":
            continue
        scoped = {name: local[name] for name in ENV_NAMES if name in local}

        def load(scoped=scoped):
            where = {name: value for name, value in scoped.items() if name in ENV_NAMES}
            key = json.dumps(where, sort_keys=True)
            if key not in cache:
                cache[key] = Switches(where)
            return cache[key]
        if any(local.get(name) != base.get(name) for name in ("ACCESS_OVERLAY", "FLEET_STATE_DIR")):
            scoped["__set_here__"] = "1"
        problems.extend(check_segment(program, args, scoped, cwd, load))
    return problems


def check_agent(tool_input: dict[str, Any], cache: dict[str, Any]) -> list[str]:
    asked = tool_input.get("model")
    if not isinstance(asked, str) or not asked or asked == "inherit":
        return []
    switches = cache.setdefault("{}", Switches({}))
    verdict = switches.verdict(asked, pool="claude")
    if not verdict:
        return []
    alias = None
    if verdict["instead"]:
        picked = verdict["instead"][0]
        cards = switches.fleet.model_cards(switches.roster)
        for key, card in cards.items():
            if picked in (key, card.get("run_as")):
                alias = next((name for name in card.get("aliases") or [] if name in AGENT_ALIASES), None)
    text = verdict["reason"]
    if alias:
        text += f' Use model "{alias}" instead, the model that runs in its place, or leave model out.'
    else:
        text += " Leave model out, or use a model that is switched on (fleetctl.py brief names them)."
    if verdict["why"] != "retired":
        text += " Only the owner can switch it back on, in the console."
    return [f'Agent model "{asked}": {text} Name the model that really runs when you report it.']


def decide(payload: dict[str, Any]) -> str | None:
    tool, tool_input = payload.get("tool_name"), payload.get("tool_input") or {}
    cache: dict[str, Any] = {}
    if tool == "Bash":
        env = {name: os.environ[name] for name in ENV_NAMES if os.environ.get(name)}
        problems = check_bash(str(tool_input.get("command") or ""), str(payload.get("cwd") or os.getcwd()), env, cache)
    elif tool in {"Agent", "Task"}:
        problems = check_agent(tool_input, cache)
    else:
        return None
    if not problems:
        return None
    text = "Crossfeed: " + " ".join(problems)
    if tool == "Bash" and not all(problem.startswith("fleetctl.py ") for problem in problems):
        text += " Rewrite the command, and name the model that really runs when you report it."
    return text


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
        if payload.get("tool_name") == "Bash" and not PREFILTER.search(str((payload.get("tool_input") or {}).get("command") or "")):
            return 0
        reason = decide(payload)
    except Exception:  # noqa: BLE001 - fail open: never block work because the roster or a command could not be read
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
