"""Plugin tiles: a small, declarative way for anyone to add a tile to the Crossfeed panel.

A plugin is a git repository (or a folder) with a `crossfeed-plugin.json` manifest. Each of its
tiles names one command; the command runs in the plugin's own folder and prints a tile document
(kind `bar`, `value`, `sparkline`, `text`, `link`). Crossfeed validates that document and draws it
with the same parts as its own tiles: no plugin code runs inside the UI.

The security model, in one paragraph. A plugin command runs with a minimal environment built from
nothing — PATH, HOME, LANG and CROSSFEED_PLUGIN_STATE — so no provider token, API key or FLEET_*
variable can reach it, whatever the parent process holds; its stdin is closed, its output is capped
at 64 KB, and its timeout is enforced by killing the process group. It is not a sandbox: the
command runs as the operator's own user and can read what that user can read. That is why nothing
installs, updates or enables itself: `fleetctl plugin add` without `--yes` prints the exact
commands it would run and stops, `update` shows the new commit and the command diff first, and the
glance only ever runs tiles of plugins the operator enabled.

Standard library only.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SCHEMA = 1
MANIFEST = "crossfeed-plugin.json"
MAX_ROWS = 8
MAX_STRING = 120
MAX_STDOUT = 64 * 1024
MAX_TIMEOUT_S = 30
MIN_REFRESH_S = 30
MAX_REFRESH_S = 86400
DEFAULT_REFRESH_S = 300
DEFAULT_TIMEOUT_S = 10
TOTAL_BUDGET_S = 15.0
MAX_TILES_PER_PLUGIN = 20
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z?$")
ROW_KINDS = ("bar", "value", "sparkline", "text", "link")
TONES = ("ok", "warn", "error")


class PluginError(RuntimeError):
    """A manifest, a command or an output the plugin contract does not allow."""


# ---- places ------------------------------------------------------------------------------
def _xdg(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default).expanduser()


def plugins_dir() -> Path:
    override = os.environ.get("FLEET_PLUGINS_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_DATA_HOME", "~/.local/share") / "crossfeed" / "plugins"


def registry_path() -> Path:
    override = os.environ.get("FLEET_PLUGINS_FILE")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_CONFIG_HOME", "~/.config") / "orchestrator" / "plugins.json"


def state_root(state_dir: Path) -> Path:
    """The private per-plugin state dir a command gets as CROSSFEED_PLUGIN_STATE."""
    return Path(state_dir) / "plugin-state"


def cache_root(state_dir: Path) -> Path:
    return Path(state_dir) / "plugin-cache"


def trash_root(state_dir: Path) -> Path:
    return Path(state_dir) / "plugin-trash"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _now() -> float:
    return time.time()


def _iso(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


# ---- registry ----------------------------------------------------------------------------
def load_registry(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """id -> {source, commit, enabled}; a missing or damaged file is no plugins at all."""
    target = path or registry_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for plugin, record in raw.items():
        if not isinstance(plugin, str) or not ID.fullmatch(plugin) or not isinstance(record, dict):
            continue
        out[plugin] = {
            "source": record.get("source") if isinstance(record.get("source"), str) else "",
            "commit": record.get("commit") if isinstance(record.get("commit"), str) else "",
            "enabled": record.get("enabled") is True,
        }
    return out


def save_registry(registry: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    _atomic_json(path or registry_path(), registry)


def plugin_dir(plugin: str, root: Path | None = None) -> Path:
    if not ID.fullmatch(plugin or ""):
        raise PluginError(f"unknown plugin id {plugin!r}")
    return (root or plugins_dir()) / plugin


# ---- manifest ----------------------------------------------------------------------------
def _text(value: Any, limit: int = MAX_STRING) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _https(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("https://") and len(value) <= 500:
        return value
    return None


def _int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


def validate_manifest(doc: Any) -> dict[str, Any]:
    """The manifest as Crossfeed runs it, or PluginError saying what is wrong."""
    if not isinstance(doc, dict):
        raise PluginError(f"{MANIFEST} must be a JSON object")
    if doc.get("schema") != SCHEMA:
        raise PluginError(f"{MANIFEST} must declare schema {SCHEMA}")
    plugin = doc.get("id")
    if not isinstance(plugin, str) or not ID.fullmatch(plugin):
        raise PluginError("the plugin id must be lowercase letters, digits and dashes, 1 to 40 characters")
    name = _text(doc.get("name"), MAX_STRING) or plugin
    version = _text(doc.get("version"), 40) or "0.0.0"
    tiles_raw = doc.get("tiles")
    if not isinstance(tiles_raw, list) or not tiles_raw:
        raise PluginError("a plugin declares at least one tile")
    if len(tiles_raw) > MAX_TILES_PER_PLUGIN:
        raise PluginError(f"a plugin declares at most {MAX_TILES_PER_PLUGIN} tiles")
    tiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in tiles_raw:
        if not isinstance(raw, dict):
            raise PluginError("each tile must be a JSON object")
        tile = raw.get("id")
        if not isinstance(tile, str) or not ID.fullmatch(tile):
            raise PluginError("a tile id must be lowercase letters, digits and dashes, 1 to 40 characters")
        if tile in seen:
            raise PluginError(f"tile id {tile!r} is declared twice")
        seen.add(tile)
        command = raw.get("command")
        if not isinstance(command, list) or not command or len(command) > 32:
            raise PluginError(f"tile {tile}: command must be a list of 1 to 32 arguments")
        if not all(isinstance(arg, str) and arg and len(arg) <= 512 for arg in command):
            raise PluginError(f"tile {tile}: every command argument must be a non-empty string under 512 characters")
        tab = raw.get("tab") if isinstance(raw.get("tab"), str) and ID.fullmatch(raw.get("tab")) else "overview"
        tiles.append({
            "id": tile,
            "title": _text(raw.get("title"), MAX_STRING) or tile,
            "command": list(command),
            "refresh_seconds": _int(raw.get("refresh_seconds"), DEFAULT_REFRESH_S, MIN_REFRESH_S, MAX_REFRESH_S),
            "timeout_seconds": _int(raw.get("timeout_seconds"), DEFAULT_TIMEOUT_S, 1, MAX_TIMEOUT_S),
            "tab": tab,
        })
    return {"schema": SCHEMA, "id": plugin, "name": name, "version": version,
            "homepage": _https(doc.get("homepage")), "tiles": tiles}


def read_manifest(folder: Path) -> dict[str, Any]:
    path = Path(folder) / MANIFEST
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PluginError(f"no {MANIFEST} in {folder}") from None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PluginError(f"{MANIFEST} is not readable JSON: {exc}") from None
    return validate_manifest(doc)


def manifest_from_git(folder: Path, ref: str) -> dict[str, Any]:
    result = subprocess.run(["git", "-C", str(folder), "show", f"{ref}:{MANIFEST}"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise PluginError(f"{ref} has no {MANIFEST}")
    try:
        return validate_manifest(json.loads(result.stdout))
    except json.JSONDecodeError as exc:
        raise PluginError(f"{MANIFEST} at {ref} is not readable JSON: {exc}") from None


# ---- tile document -----------------------------------------------------------------------
def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _percent(value: Any) -> float | None:
    return float(value) if _finite(value) and 0 <= value <= 100 else None


def validate_tile(doc: Any, *, title: str = "Plugin") -> dict[str, Any]:
    """The tile document as the panel draws it. Raises PluginError('Plugin output invalid').

    Limits are refusals, not tidy-ups: more than 8 rows, a string over 120 characters, a number
    that is not finite or a percentage outside 0-100 makes the whole document invalid, so the
    panel shows one honest error tile instead of a half-drawn one.
    """
    def bad(reason: str) -> PluginError:
        return PluginError(f"Plugin output invalid: {reason}")

    if not isinstance(doc, dict):
        raise bad("not a JSON object")
    status = doc.get("status", "ok")
    if status not in ("ok", "warn", "error"):
        raise bad("status must be ok, warn or error")
    out_title = doc.get("title", title)
    if out_title is not None and (not isinstance(out_title, str) or len(out_title) > MAX_STRING):
        raise bad(f"title must be text of at most {MAX_STRING} characters")
    rows_raw = doc.get("rows", [])
    if not isinstance(rows_raw, list):
        raise bad("rows must be a list")
    if len(rows_raw) > MAX_ROWS:
        raise bad(f"at most {MAX_ROWS} rows")
    rows: list[dict[str, Any]] = []

    def string(row: dict[str, Any], key: str, required: bool = True, limit: int = MAX_STRING) -> str | None:
        value = row.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or len(value) > limit:
            raise bad(f"{key} must be text of at most {limit} characters")
        return value

    for raw in rows_raw:
        if not isinstance(raw, dict):
            raise bad("every row must be a JSON object")
        kind = raw.get("kind")
        if kind not in ROW_KINDS:
            continue  # an unknown part is dropped, never a reason to fail the whole tile
        if kind == "bar":
            used, pace = raw.get("used"), raw.get("pace")
            row = {"kind": "bar", "label": string(raw, "label"), "used": None, "pace": None,
                   "window": string(raw, "window", required=False),
                   "resets_at": string(raw, "resets_at", required=False)}
            if used is not None:
                row["used"] = _percent(used)
                if row["used"] is None:
                    raise bad("used must be a number from 0 to 100")
            if pace is not None:
                row["pace"] = _percent(pace)
                if row["pace"] is None:
                    raise bad("pace must be a number from 0 to 100")
            if row["resets_at"] is not None:
                if not ISO_UTC.fullmatch(row["resets_at"]):
                    raise bad("resets_at must be an ISO-8601 UTC time")
                row["resets_at"] = row["resets_at"].rstrip("Z") + "Z"
            rows.append(row)
        elif kind == "value":
            tone = raw.get("tone", "ok")
            if tone not in TONES:
                raise bad("tone must be ok, warn or error")
            rows.append({"kind": "value", "label": string(raw, "label"), "value": string(raw, "value"), "tone": tone})
        elif kind == "sparkline":
            values = raw.get("values")
            if not isinstance(values, list) or not values or len(values) > 14:
                raise bad("values must be a list of 1 to 14 numbers")
            if not all(_finite(value) for value in values):
                raise bad("values must be finite numbers")
            rows.append({"kind": "sparkline", "label": string(raw, "label"),
                         "values": [float(value) for value in values],
                         "today": string(raw, "today", required=False)})
        elif kind == "text":
            rows.append({"kind": "text", "text": string(raw, "text")})
        else:  # link
            url = _https(raw.get("url"))
            if not url:
                continue  # a link is only ever https, and only ever shown when it is
            rows.append({"kind": "link", "label": string(raw, "label"), "url": url})
    return {"title": (out_title or title)[:MAX_STRING], "status": status, "rows": rows}


# ---- running one tile --------------------------------------------------------------------
def command_env(plugin: str, state_dir: Path) -> dict[str, str]:
    """Built from nothing: no provider credential, no *_TOKEN, no *_KEY, no FLEET_*."""
    home = os.environ.get("HOME") or "/tmp"
    env = {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin", "HOME": home,
           "LANG": os.environ.get("LANG") or "C.UTF-8", "CROSSFEED_PLUGIN_STATE": str(state_root(state_dir) / plugin)}
    return env


def _execute(command: list[str], cwd: Path, env: dict[str, str], timeout_s: float) -> dict[str, Any]:
    """Run one command with stdin closed, output capped and the process group killed on timeout."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, start_new_session=True)
        except (OSError, ValueError) as exc:
            return {"ok": False, "reason": f"the command could not start: {exc}", "stdout": b"", "returncode": None,
                    "timed_out": False}
        try:
            process.wait(timeout=max(0.1, timeout_s))
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_group(process)
            timed_out = True
        if process.poll() is None:
            _kill_group(process)
        out.seek(0)
        stdout = out.read(MAX_STDOUT + 1)
        err.seek(0)
        stderr = err.read(4096)
    return {"ok": not timed_out, "timed_out": timed_out, "returncode": process.returncode,
            "stdout": stdout, "stderr": stderr}


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _cache_file(state_dir: Path, plugin: str, tile: str) -> Path:
    return cache_root(state_dir) / plugin / f"{tile}.json"


def cached(state_dir: Path, plugin: str, tile: str, refresh_seconds: int, now: float | None = None) -> dict[str, Any] | None:
    """A tile's last run, while it is younger than the plugin's refresh_seconds."""
    try:
        stored = json.loads(_cache_file(state_dir, plugin, tile).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    observed = stored.get("observed_at_s") if isinstance(stored, dict) else None
    if not _finite(observed) or not isinstance(stored.get("doc"), dict):
        return None
    if (now if now is not None else _now()) - observed >= refresh_seconds:
        return None
    return stored


def run_tile(plugin: str, folder: Path, tile: dict[str, Any], state_dir: Path,
             *, timeout_s: float | None = None, now: float | None = None, use_cache: bool = True) -> dict[str, Any]:
    """One tile, from cache or by running its command; never raises for a plugin's own failure."""
    started = now if now is not None else _now()
    if use_cache:
        stored = cached(state_dir, plugin, tile["id"], tile["refresh_seconds"], started)
        if stored:
            return {"plugin": plugin, "tile": f"{plugin}/{tile['id']}", "tab": tile["tab"],
                    "doc": stored["doc"], "observed_at": _iso(stored["observed_at_s"]),
                    "error": stored.get("error")}
    state = state_root(state_dir) / plugin
    state.mkdir(parents=True, exist_ok=True)
    limit = tile["timeout_seconds"] if timeout_s is None else min(tile["timeout_seconds"], timeout_s)
    result = _execute(tile["command"], folder, command_env(plugin, state_dir), limit)
    doc, error = None, None
    if result["timed_out"]:
        error = "Plugin timed out"
    elif result["returncode"] != 0:
        error = f"Plugin failed (exit {result['returncode']})"
    elif len(result["stdout"]) > MAX_STDOUT:
        error = "Plugin output too large"
    else:
        try:
            text = result["stdout"].decode("utf-8")
        except UnicodeDecodeError:
            error = "Plugin output invalid: not UTF-8 text"
        else:
            if not text.strip():
                error = "Plugin output invalid: nothing printed"
            else:
                try:
                    doc = validate_tile(json.loads(text), title=tile["title"])
                except json.JSONDecodeError:
                    error = "Plugin output invalid: not JSON"
                except PluginError as exc:
                    error = str(exc)
    observed = _now()
    if error:
        doc = {"title": tile["title"], "status": "error", "rows": [{"kind": "text", "text": error}]}
    _atomic_json(_cache_file(state_dir, plugin, tile["id"]),
                 {"observed_at_s": observed, "doc": doc, "error": error})
    return {"plugin": plugin, "tile": f"{plugin}/{tile['id']}", "tab": tile["tab"], "doc": doc,
            "observed_at": _iso(observed), "error": error}


# ---- the glance's plugin block ------------------------------------------------------------
def glance(state_dir: Path, registry: dict[str, dict[str, Any]] | None = None, *,
           now: float | None = None, budget_s: float = TOTAL_BUDGET_S,
           root: Path | None = None) -> list[dict[str, Any]]:
    """Every enabled plugin tile, in parallel, inside one total budget, cached per its refresh.

    A plugin that is disabled, missing or has an unreadable manifest contributes nothing: the
    panel is a reading, and a broken plugin must not take the reading down.
    """
    registry = load_registry() if registry is None else registry
    started = now if now is not None else _now()
    deadline = started + budget_s
    out: list[dict[str, Any]] = []
    pending: list[tuple[str, Path, dict[str, Any]]] = []
    for plugin, record in sorted(registry.items()):
        if not record.get("enabled"):
            continue
        folder = plugin_dir(plugin, root)
        try:
            manifest = read_manifest(folder)
        except PluginError:
            continue
        for tile in manifest["tiles"]:
            stored = cached(state_dir, plugin, tile["id"], tile["refresh_seconds"], started)
            if stored:
                out.append({"plugin": plugin, "tile": f"{plugin}/{tile['id']}", "tab": tile["tab"],
                            "doc": stored["doc"], "observed_at": _iso(stored["observed_at_s"]),
                            "error": stored.get("error")})
            else:
                pending.append((plugin, folder, tile))
    if not pending:
        return sorted(out, key=lambda row: (row["plugin"], row["tile"]))
    remaining = max(0.0, deadline - _now())
    if remaining <= 0.5:
        # The glance's budget is spent: say so per tile rather than quietly leaving it out.
        for plugin, _folder, tile in pending:
            out.append({"plugin": plugin, "tile": f"{plugin}/{tile['id']}", "tab": tile["tab"],
                        "doc": {"title": tile["title"], "status": "error",
                                "rows": [{"kind": "text", "text": "Plugin timed out"}]},
                        "observed_at": _iso(_now()), "error": "Plugin timed out"})
        return sorted(out, key=lambda row: (row["plugin"], row["tile"]))
    workers = max(1, min(8, len(pending)))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="plugin-tile")
    futures = {executor.submit(run_tile, plugin, folder, tile, state_dir,
                               timeout_s=max(1.0, deadline - _now())): (plugin, tile, folder)
               for plugin, folder, tile in pending}
    try:
        for future, (plugin, tile, _folder) in futures.items():
            wait_s = deadline - _now() + 2.0  # the per-command kill needs a moment past the budget
            try:
                out.append(future.result(timeout=max(0.2, wait_s)))
            except Exception as exc:  # a plugin that overruns or crashes is one error tile
                reason = "Plugin timed out" if isinstance(exc, TimeoutError) else f"Plugin failed: {exc}"
                out.append({"plugin": plugin, "tile": f"{plugin}/{tile['id']}", "tab": tile["tab"],
                            "doc": {"title": tile["title"], "status": "error",
                                    "rows": [{"kind": "text", "text": reason}]},
                            "observed_at": _iso(_now()), "error": reason})
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return sorted(out, key=lambda row: (row["plugin"], row["tile"]))


# ---- install, update, enable, remove ------------------------------------------------------
def _git(folder: Path, *args: str, timeout: int = 120) -> str:
    result = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise PluginError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout.strip()


def _source_words(source: str) -> str:
    return "https" if source.startswith("https://") else "folder"


def _plan(manifest: dict[str, Any], source: str, commit: str) -> dict[str, Any]:
    return {"id": manifest["id"], "name": manifest["name"], "version": manifest["version"],
            "source": source, "commit": commit, "kind": _source_words(source),
            "commands": [{"tile": tile["id"], "command": tile["command"]} for tile in manifest["tiles"]]}


def describe_plan(plan: dict[str, Any], *, verb: str) -> str:
    lines = [f"{verb}: {plan['name']} {plan['version']} ({plan['id']})",
             f"source: {plan['source']}",
             f"commit: {plan['commit'] or 'not a git checkout'}",
             "commands it would run:"]
    for row in plan["commands"]:
        lines.append("  " + row["tile"] + ": " + " ".join(row["command"]))
    return "\n".join(lines)


def install(source: str, *, ref: str | None = None, apply: bool = False, root: Path | None = None,
            registry_file: Path | None = None) -> dict[str, Any]:
    """Stage, read and validate a plugin; move it into place and enable it only when apply.

    Without apply nothing reaches the plugin folder or the registry: the caller prints the plan,
    including every command, and stops. That is the operator's look before anything runs.
    """
    root = root or plugins_dir()
    registry_file = registry_file or registry_path()
    if not source.startswith("https://") and not Path(source).expanduser().is_dir():
        raise PluginError("give an https:// git URL or an existing folder")
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
    try:
        commit = ""
        if source.startswith("https://"):
            result = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", source, str(staging / "repo")],
                                    capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise PluginError("the clone failed: " + (result.stderr or result.stdout).strip()[:300])
            folder = staging / "repo"
            if ref:
                _git(folder, "checkout", "--quiet", ref)
            commit = _git(folder, "rev-parse", "HEAD")
        else:
            folder = Path(source).expanduser().resolve()
            shutil.copytree(folder, staging / "repo", ignore=shutil.ignore_patterns(".git"))
            folder = staging / "repo"
            try:
                commit = _git(folder, "rev-parse", "HEAD")
            except PluginError:
                commit = ""
        manifest = read_manifest(folder)
        if ref and commit and len(ref) >= 7 and not commit.startswith(ref) and ref not in commit:
            raise PluginError(f"the checkout is not the requested ref {ref}")
        plan = _plan(manifest, source, commit)
        if plugin_dir(manifest["id"], root).exists():
            raise PluginError(f"plugin {manifest['id']} is already installed; update or remove it first")
        if not apply:
            return {**plan, "installed": False, "enabled": False}
        final = plugin_dir(manifest["id"], root)
        os.replace(folder, final)
        registry = load_registry(registry_file)
        registry[manifest["id"]] = {"source": source, "commit": commit, "enabled": True}
        save_registry(registry, registry_file)
        return {**plan, "installed": True, "enabled": True}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def update(plugin: str, *, apply: bool = False, root: Path | None = None,
           registry_file: Path | None = None) -> dict[str, Any]:
    """Show the commit and the command diff a plugin update would bring; move only when apply."""
    root = root or plugins_dir()
    registry_file = registry_file or registry_path()
    registry = load_registry(registry_file)
    if plugin not in registry:
        raise PluginError(f"unknown plugin {plugin}")
    folder = plugin_dir(plugin, root)
    if not folder.is_dir():
        raise PluginError(f"plugin {plugin} is not installed in {folder}")
    if not (folder / ".git").exists():
        raise PluginError(f"plugin {plugin} came from a folder, so there is no new commit to fetch")
    _git(folder, "fetch", "--quiet", "--tags", "origin")
    target = ""
    for candidate in ("origin/HEAD", f"origin/{_git(folder, 'rev-parse', '--abbrev-ref', 'HEAD')}"):
        try:
            target = _git(folder, "rev-parse", candidate)
            break
        except PluginError:
            continue
    if not target:
        raise PluginError("the remote names no branch to update to")
    current_commit = _git(folder, "rev-parse", "HEAD")
    old = read_manifest(folder)
    new = manifest_from_git(folder, target)
    if new["id"] != plugin:
        raise PluginError(f"the new commit declares plugin id {new['id']}, not {plugin}")
    before = {tile["id"]: tile["command"] for tile in old["tiles"]}
    after = {tile["id"]: tile["command"] for tile in new["tiles"]}
    changes = []
    for tile in sorted(set(before) | set(after)):
        if before.get(tile) != after.get(tile):
            changes.append({"tile": tile, "before": before.get(tile), "after": after.get(tile)})
    report = {"id": plugin, "name": new["name"], "version": new["version"], "from": current_commit,
              "to": target, "unchanged": current_commit == target, "command_changes": changes,
              "updated": False}
    if not apply or current_commit == target:
        return report
    _git(folder, "checkout", "--quiet", target)
    registry[plugin] = {**registry[plugin], "commit": _git(folder, "rev-parse", "HEAD")}
    save_registry(registry, registry_file)
    return {**report, "updated": True}


def set_enabled(plugin: str, enabled: bool, *, registry_file: Path | None = None,
                root: Path | None = None) -> dict[str, Any]:
    registry_file = registry_file or registry_path()
    registry = load_registry(registry_file)
    if plugin not in registry:
        raise PluginError(f"unknown plugin {plugin}")
    if enabled:
        read_manifest(plugin_dir(plugin, root))  # never enable a folder whose manifest no longer reads
    registry[plugin] = {**registry[plugin], "enabled": enabled}
    save_registry(registry, registry_file)
    return {"id": plugin, "enabled": enabled}


def remove(plugin: str, *, state_dir: Path, root: Path | None = None,
           registry_file: Path | None = None) -> dict[str, Any]:
    """Move the plugin folder into the state dir's trash (never delete it) and forget its record."""
    root = root or plugins_dir()
    registry_file = registry_file or registry_path()
    registry = load_registry(registry_file)
    if plugin not in registry:
        raise PluginError(f"unknown plugin {plugin}")
    folder = plugin_dir(plugin, root)
    moved = None
    if folder.exists():
        trash = trash_root(state_dir)
        trash.mkdir(parents=True, exist_ok=True)
        moved = trash / f"{plugin}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        os.replace(folder, moved)
    registry.pop(plugin, None)
    save_registry(registry, registry_file)
    return {"id": plugin, "moved_to": str(moved) if moved else None, "removed": True}


def listing(registry: dict[str, dict[str, Any]] | None = None, root: Path | None = None) -> list[dict[str, Any]]:
    registry = load_registry() if registry is None else registry
    rows = []
    for plugin, record in sorted(registry.items()):
        row = {"id": plugin, "source": record.get("source"), "commit": record.get("commit"),
               "enabled": bool(record.get("enabled")), "installed": plugin_dir(plugin, root).is_dir(),
               "name": plugin, "version": None, "tiles": []}
        try:
            manifest = read_manifest(plugin_dir(plugin, root))
        except PluginError:
            row["broken"] = True
        else:
            row.update(name=manifest["name"], version=manifest["version"],
                       tiles=[{"tile": tile["id"], "title": tile["title"], "tab": tile["tab"]}
                              for tile in manifest["tiles"]])
        rows.append(row)
    return rows
