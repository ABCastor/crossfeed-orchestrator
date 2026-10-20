#!/usr/bin/env python3
"""vault_guard: no external worker writes into the owner's notes, whatever its vendor.

Why (20 Oct 2026): a lead's standing brief told every Task Manager test worker to save its
screenshots into the vault, and its launchers made the vault folder each worker's --dir. The
workers ran on Codex (--sandbox danger-full-access) and Antigravity (--dangerously-skip-permissions),
so none of Claude Code's hooks ever saw the writes, and the vault autocommit stored ~300 MB of
images in git. The fault was the instruction, so the guard sits where every worker passes: the
wrapper, not the lead, and not a vendor's own sandbox.

Two layers, both here so every wrapper shares one rule:
  check    refuses before launch (exit 5) when a worker's folder is inside or contains a protected
           root in a mode that can write; when an output file (--last, --events) lands in one;
           when a Codex config grants a protected writable root; or when the brief names a
           protected path as somewhere to put things ("Output folder: ~/Second Brain/1_Notes/...").
  profile  prints a macOS Seatbelt profile that denies every file write under the protected roots.
           Wrappers launch the worker CLI under /usr/bin/sandbox-exec with it, so the whole worker
           process tree (its shell commands, MCP servers it starts, any copy or mv) cannot write
           there even if the brief scan misses a phrasing. Codex in its own sandbox modes is the one
           exception: Seatbelt cannot nest, and Codex's own sandbox already keeps writes to --dir.

Protected roots: the owner's vault (from the account's home directory, not $HOME, so a changed
environment cannot move it) plus any extra paths in CROSSFEED_PROTECTED_ROOTS (os.pathsep list).
Extra roots only add; nothing here can remove the vault. There is no override flag on purpose:
the incident came from the lead, so the lead must not be able to grant itself an exception.
Workers deliver into their own folder; the lead files into the vault through its own harness rules.
"""
from __future__ import annotations

import argparse
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path

REFUSED = 5          # wrapper convention: prelaunch refusal
VAULT_RELATIVE = ("Second Brain", "1_Notes")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# A path mention counts as a write target when the clause before it (or a parenthesis right after
# it) carries one of these. Reading context ("READ FIRST: home ~/.../Task Manager.md") does not.
WRITE_CUE = re.compile(
    r"\b(output|outputs|save|saves|saved|saving|write|writes|writing|written|store|stores|stored|"
    r"storing|put|copy|copies|move|moves|export|exports|drop|place|dump|screenshots?|images?|"
    r"videos?|captures?|recordings?|attachments?|evidence|results? (?:go|land)|deliver\w*)\b"
    r"|--last\b|--out\b|--events\b|(?<![\w-])-o\s|>\s*$",
    re.IGNORECASE)
NEGATION = re.compile(r"\b(never|not|no|don't|dont|without)\b|n't\b", re.IGNORECASE)
CLAUSE_BREAK = re.compile(r"[.;!?\n(]")


def _home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        return Path.home()


def protected_roots() -> list[Path]:
    roots = [_home().joinpath(*VAULT_RELATIVE)]
    for extra in os.environ.get("CROSSFEED_PROTECTED_ROOTS", "").split(os.pathsep):
        if extra.strip():
            roots.append(Path(extra.strip()).expanduser())
    seen, out = set(), []
    for root in roots:
        real = Path(os.path.realpath(root))
        key = str(real).casefold()
        if key not in seen:
            seen.add(key)
            out.append(real)
    return out


def _real(path: str | Path) -> Path:
    return Path(os.path.realpath(Path(path).expanduser()))


def _inside(path: Path, root: Path) -> bool:
    # APFS is case-insensitive by default; compare folded.
    p, r = str(path).casefold().rstrip("/"), str(root).casefold().rstrip("/")
    return p == r or p.startswith(r + "/")


def _spellings(root: Path) -> list[str]:
    home = str(_home())
    forms = {str(root)}
    raw = str(root)
    if raw.startswith(home + "/"):
        rel = raw[len(home) + 1:]
        forms |= {"~/" + rel, "$HOME/" + rel, "${HOME}/" + rel, home + "/" + rel}
    out = set()
    for form in forms:
        out |= {form, form.replace(" ", "\\ "), form.replace(" ", "%20")}
    return sorted(out, key=len, reverse=True)


def brief_targets(prompt: str, roots: list[Path]) -> list[str]:
    """Lines where the brief names a protected path as a place to write."""
    hits = []
    for root in roots:
        for form in _spellings(root):
            for match in re.finditer(re.escape(form), prompt, re.IGNORECASE):
                start, end = match.start(), match.end()
                head = prompt[:start]
                breaks = list(CLAUSE_BREAK.finditer(head))
                clause = head[breaks[-1].end():] if breaks else head
                tail = prompt[end:]
                # A parenthesis right after the path (after a closing quote/backtick) is about it.
                paren = re.match(r"[^\s`'\"]*[`'\"]?\s*\(([^)]{0,120})\)", tail)
                cue_text = clause + " " + (paren.group(1) if paren else "")
                if WRITE_CUE.search(cue_text) and not NEGATION.search(clause):
                    line_start = prompt.rfind("\n", 0, start) + 1
                    line_end = prompt.find("\n", end)
                    line = prompt[line_start:line_end if line_end >= 0 else len(prompt)].strip()
                    if line not in hits:
                        hits.append(line[:200])
    return hits


def _codex_config_hits(configs: list[str], roots: list[Path]) -> list[str]:
    hits = []
    texts = list(configs)
    codex_home = Path(os.environ.get("CODEX_HOME", _home() / ".codex"))
    try:
        texts.append((codex_home / "config.toml").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    for text in texts:
        for line in text.splitlines():
            if "writable_roots" not in line and "add_dir" not in line:
                continue
            for root in roots:
                if any(form.casefold() in line.casefold() for form in _spellings(root)):
                    hits.append(line.strip()[:200])
    return hits


def check(wrapper: str, mode: str, directory: str | None, prompt: str,
          outputs: list[str] | None = None, configs: list[str] | None = None) -> list[str]:
    roots = protected_roots()
    reasons = []
    if directory and mode == "write":
        d = _real(directory)
        for root in roots:
            if _inside(d, root) or _inside(root, d):
                reasons.append(f"worker folder {d} {'is inside' if _inside(d, root) else 'contains'} "
                               f"the protected folder {root}, and this {wrapper} run can write")
    for out in outputs or []:
        if not out:
            continue
        o = _real(out)
        for root in roots:
            if _inside(o, root):
                reasons.append(f"output file {o} is inside the protected folder {root}")
    for line in _codex_config_hits(configs or [], roots) if wrapper == "codex" else []:
        reasons.append(f"Codex config makes a protected folder writable: {line}")
    for line in brief_targets(prompt or "", roots):
        reasons.append(f"the brief names a protected folder as a write target: {line}")
    return reasons


def _sbpl(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile() -> str:
    rules = " ".join(f"(subpath {_sbpl(root)})" for root in protected_roots())
    return f"(version 1)(allow default)(deny file-write* {rules})"


def jail_status() -> tuple[str, str]:
    """('ok', profile) | ('none', '') where no Seatbelt exists and no root is on disk | ('refuse', why)."""
    if not Path(SANDBOX_EXEC).exists():
        if any(root.exists() for root in protected_roots()):
            return "refuse", "sandbox-exec is missing, so writes into the protected folder cannot be blocked"
        return "none", ""
    text = profile()
    try:
        probe = subprocess.run([SANDBOX_EXEC, "-p", text, "/usr/bin/true"], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        return "refuse", f"sandbox-exec could not start ({error.__class__.__name__})"
    if probe.returncode != 0:
        return "refuse", ("this shell is already inside a sandbox (Seatbelt cannot nest), so the worker's "
                          "writes into the protected folder cannot be blocked; launch it from an unsandboxed shell")
    return "ok", text


def jail_argv() -> list[str]:
    """Prefix for a worker launch; raises SystemExit(5) when the block cannot be applied."""
    state, value = jail_status()
    if state == "refuse":
        print(f"vault-guard: refused: {value}", file=sys.stderr)
        raise SystemExit(REFUSED)
    return [SANDBOX_EXEC, "-p", value] if state == "ok" else []


def refuse_or_pass(reasons: list[str]) -> int:
    if not reasons:
        return 0
    for reason in reasons:
        print(f"vault-guard: refused: {reason}", file=sys.stderr)
    print("vault-guard: workers never write into the owner's notes. Give the worker a folder outside the "
          "vault (its repo worktree or run scratch), have it deliver there, and file into the vault yourself.",
          file=sys.stderr)
    return REFUSED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vault_guard.py")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--wrapper", required=True)
    c.add_argument("--mode", choices=("read-only", "write"), required=True)
    c.add_argument("--dir")
    c.add_argument("--out", action="append", default=[])
    c.add_argument("--config", action="append", default=[])
    c.add_argument("--prompt-file", help="read the brief from this file instead of stdin")
    sub.add_parser("profile", help="print the Seatbelt profile, or exit 5 when it cannot be applied")
    sub.add_parser("roots")
    args = parser.parse_args(argv)
    if args.command == "roots":
        print("\n".join(str(r) for r in protected_roots()))
        return 0
    if args.command == "profile":
        state, value = jail_status()
        if state == "refuse":
            print(f"vault-guard: refused: {value}", file=sys.stderr)
            return REFUSED
        print(value)
        return 0
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            prompt = ""
    else:
        prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    return refuse_or_pass(check(args.wrapper, args.mode, args.dir, prompt, args.out, args.config))


if __name__ == "__main__":
    sys.exit(main())
