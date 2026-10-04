#!/usr/bin/env python3
"""Translate a prompt file into verified wrapper flags, without invoking a shell."""
import argparse
import os
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=("codex", "claude", "agy", "opencode"))
    parser.add_argument("--scripts", required=True, type=Path)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--level", required=True)
    parser.add_argument("--events")
    args = parser.parse_args(argv)
    command = [str(args.scripts / (args.harness + "-agent.sh")), "--dir", args.dir, "--model", args.model]
    # Wrappers supervise children in their own process groups. Their cleanup must
    # finish before the grid's outer termination grace ends.
    command += ["--kill-after", "1"]
    if args.harness in ("codex", "claude", "opencode"):
        command += ["--prompt-file", str(args.prompt_file)]
    else:
        command += ["--prompt", args.prompt_file.read_text(encoding="utf-8")]
    if args.harness == "codex":
        command += ["--reasoning", args.level, "--sandbox", "workspace-write"]
    elif args.harness == "claude":
        command += ["--effort", args.level, "--permission-mode", "acceptEdits"]
    elif args.harness == "agy":
        command += ["--effort", args.level, "--sandbox"]
    else:
        command += ["--write", "--context", "lean"]
        if args.level != "none":
            command += ["--variant", args.level]
    if args.events and args.harness in ("codex", "opencode"):
        command += ["--events", args.events]
    # Replace this adapter so the supervisor waits for the wrapper's cleanup,
    # rather than observing an adapter that dies before its children do.
    os.execv(command[0], command)


if __name__ == "__main__":
    sys.exit(main())
