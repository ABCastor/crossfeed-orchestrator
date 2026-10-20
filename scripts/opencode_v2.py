#!/usr/bin/env python3
"""Generate a private v2 worker profile. Never writes the owner's configuration."""
import argparse
import json
import subprocess
from pathlib import Path


def rule(action, effect, resource="*"):
    return {"action": action, "resource": resource, "effect": effect}


def worker_config(mode="read-only", context="lean", direct=False, search=False):
    # BR-OPENCODE-WORKER-001
    permissions = [rule("*", "deny")]
    if not direct:
        if mode == "write":
            permissions = [rule("*", "allow")]
        elif search:
            permissions += [rule("websearch", "allow")]
        else:
            permissions += [rule(name, "allow") for name in ("read", "glob", "grep")]
        if context == "shared":
            permissions += [rule("skill", "allow")]
    permissions += [rule(name, "deny") for name in
                    ("external_directory", "subagent", "question", "opencode*")]
    if mode != "write":
        permissions += [rule(name, "deny") for name in ("edit", "shell")]
    agent = "direct" if direct else "fleet-research" if search else "build" if mode == "write" else "plan"
    policies = [
        {"action": "integration.use", "resource": "plugin:*", "effect": "deny"},
        {"action": "integration.use", "resource": "mcp:*", "effect": "deny"},
    ]
    if context == "lean" or direct or search:
        policies.append({"action": "integration.use", "resource": "skill:*", "effect": "deny"})
    # Read-only denies execute through the default rule; read/glob/grep/search
    # are native direct tools in v2. Build's Code Mode retains permission checks.
    # Block discovery
    # plugins explicitly because the v1 external-skills environment flag is gone.
    plugins = []
    if context == "lean" or direct or search:
        plugins += ["-opencode.config.skill", "-opencode.config.compatibility"]
    return {
        "$schema": "https://opencode.ai/config.json",
        "agents": {agent: {"mode": "primary", "permissions": permissions,
                           # Existing synchronized fleet-research profile: maxSteps=6.
                           **({"steps": 6} if search else {})}},
        "permissions": permissions,
        "policies": policies,
        "plugins": plugins,
        "mcp": {"servers": {}},
        "share": "disabled", "update": "disable", "warming": False,
        "formatter": False, "lsp": False, "snapshots": False,
        **({"websearch": {"provider": "exa"}} if search else {"websearch": False}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--mode", choices=("read-only", "write"), default="read-only")
    parser.add_argument("--context", choices=("lean", "shared"), default="lean")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--search", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    config = worker_config(args.mode, args.context, args.direct, args.search)
    # Copy only synchronized ambient instructions/skills, never executable plugins,
    # agent overrides, MCP or provider policy from an inherited profile.
    if args.source and not args.direct:
        instructions = args.source / "AGENTS.md"
        if instructions.is_file():
            config["instructions"] = [str(instructions.resolve())]
        if args.context == "shared":
            config["skills"] = [str((args.source / name).resolve())
                                for name in ("skill", "skills") if (args.source / name).is_dir()]
    if args.directory and not args.direct and not args.search:
        directory = args.directory.resolve()
        git = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
        root = Path(git.stdout.strip()).resolve() if git.returncode == 0 else directory
        ancestors = [directory]
        while ancestors[-1] != root and root in ancestors[-1].parents:
            ancestors.append(ancestors[-1].parent)
        config.setdefault("instructions", []).extend(str(p / "AGENTS.md") for p in reversed(ancestors)
                                                     if (p / "AGENTS.md").is_file())
    args.destination.write_text(json.dumps(config, indent=2) + "\n")
    args.destination.chmod(0o600)


if __name__ == "__main__":
    main()
