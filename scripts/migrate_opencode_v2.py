#!/usr/bin/env python3
"""Prepare v2 Go picker restrictions in a separate output directory, never live files.

V2 ignores provider.*.whitelist. Restrict the known catalog using native model
disabled flags instead. Re-run after catalog changes: v2 has no model wildcard.
"""
import argparse
import copy
import json
import os
from pathlib import Path

from fleetctl import _load_jsonc, _lane_is_admitted


def admitted_models(roster):
    return {lane["selector"].removeprefix("opencode-go/")
            for lane in roster.get("lanes", [])
            if lane.get("harness") == "opencode" and lane.get("provider") == "opencode-go"
            and _lane_is_admitted(lane)}


def migrate(config, admitted, catalog):
    result = copy.deepcopy(config)
    legacy = result.get("provider", {}).get("opencode-go", {})
    legacy.pop("whitelist", None)
    legacy.pop("blacklist", None)
    models = result.setdefault("providers", {}).setdefault("opencode-go", {}).setdefault("models", {})
    for model in sorted(set(catalog) | set(admitted) | set(models) | set(legacy.get("models", {}))):
        models.setdefault(model, {})["disabled"] = model not in admitted
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=cache_home / "opencode/models.json")
    parser.add_argument("--config", type=Path, action="append")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    configs = args.config or [p for p in (config_home / "opencode/opencode.json",
                config_home / "opencode/opencode.jsonc",
                config_home / "opencode/fleet-worker/opencode.json",
                config_home / "opencode/fleet-worker/opencode.jsonc") if p.is_file()]
    catalog = json.loads(args.catalog.read_text())["opencode-go"]["models"]
    admitted = admitted_models(json.loads(args.overlay.read_text()))
    if not admitted or not catalog:
        parser.error("an admitted Go roster and complete cached Go catalog are required")
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    for i, source in enumerate(configs):
        target = args.out / f"{i}-{source.parent.name}-{source.name}"
        if target.resolve() == source.resolve() or target.is_symlink():
            parser.error("output must be a separate regular file")
        candidate = migrate(_load_jsonc(source), admitted, catalog)
        target.write_text(json.dumps(candidate, indent=2) + "\n")
        target.chmod(0o600)
        print(f"{source} -> {target}")


if __name__ == "__main__":
    main()
