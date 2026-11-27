# Plugin tiles

A plugin adds a tile to the Crossfeed panel without a line of Crossfeed code. It is a small folder
with a manifest and one command per tile; the command prints JSON and Crossfeed draws it with the
same parts as its own tiles. There is no plugin API to learn, no build step, and no plugin code in
the panel.

Five minutes, start to finish:

```bash
mkdir crossfeed-hello && cd crossfeed-hello
cat > crossfeed-plugin.json <<'JSON'
{"schema": 1, "id": "hello", "name": "Hello", "version": "0.1.0",
 "tiles": [{"id": "greeting", "title": "Greeting", "command": ["python3", "tile.py"],
            "refresh_seconds": 300, "timeout_seconds": 10, "tab": "overview"}]}
JSON
cat > tile.py <<'PY'
import json
print(json.dumps({"title": "Greeting", "status": "ok",
                  "rows": [{"kind": "text", "text": "This tile comes from a plugin."}]}))
PY
python3 tile.py            # what Crossfeed will see
```

Then install it:

```bash
python3 scripts/fleetctl.py plugin add ~/crossfeed-hello       # prints name, commit and every command; changes nothing
python3 scripts/fleetctl.py plugin add ~/crossfeed-hello --yes # installs it, and enables it
python3 scripts/fleetctl.py plugin run hello/greeting          # runs one tile now
```

`fleetctl plugin list`, `plugin disable hello`, `plugin enable hello`, `plugin update hello`
(prints the new commit and every command change first), and `plugin remove hello` (moves the folder
into the state directory's trash, never deletes it) manage it afterwards. A git URL works wherever a
folder does: `plugin add https://github.com/someone/crossfeed-gh-actions --ref v0.1.0`.

## The manifest

`crossfeed-plugin.json` at the top of the folder, for a folder or a git repository:

```json
{"schema": 1,
 "id": "gh-actions",
 "name": "GitHub Actions",
 "version": "0.1.0",
 "homepage": "https://github.com/someone/crossfeed-gh-actions",
 "tiles": [
   {"id": "runs", "title": "CI runs", "command": ["python3", "tile.py"],
    "refresh_seconds": 300, "timeout_seconds": 10, "tab": "overview"}
 ]}
```

- `id` and every tile `id`: lowercase letters, digits and dashes, 1 to 40 characters. The plugin id
  is its folder name and the first half of its tile key (`gh-actions/runs`).
- `version`, `homepage` (https only) and `title` are for the settings sheet and `plugin list`.
- `command` is an argv list, never a shell string: `["python3", "tile.py"]`, not `"python3 tile.py | tee"`.
  It runs with the plugin folder as the working directory, so relative paths mean files you shipped.
- `refresh_seconds` (default 300, minimum 30) is how long a result is reused before the command runs
  again. `timeout_seconds` (default 10, maximum 30) kills the command.
- `tab` is where the tile belongs: `overview`, `claude`, `codex`, `copilot`, `antigravity`,
  `opencode`, or `other`. The operator moves it afterwards in Settings.
- At most 20 tiles per plugin.

## The tile document

Print one JSON object and nothing else. Anything printed on stderr is ignored; a non-zero exit, a
timeout, or a document that breaks a rule below becomes one visible error tile.

```json
{"title": "CI runs", "status": "ok",
 "rows": [
   {"kind": "bar", "label": "Minutes", "used": 58, "pace": 41, "window": "Monthly", "resets_at": "2026-12-01T00:00:00Z"},
   {"kind": "value", "label": "Failed today", "value": "3", "tone": "warn"},
   {"kind": "sparkline", "label": "Runs", "values": [3,5,2,8], "today": "8"},
   {"kind": "text", "text": "main is green"},
   {"kind": "link", "label": "Open", "url": "https://github.com/someone/crossfeed-gh-actions/actions"}
 ]}
```

| kind | draws | rules |
|---|---|---|
| `bar` | the pace bar: what is used, and the pace tick to compare it with | `used` and `pace` are 0-100; `window` and `resets_at` (ISO-8601 UTC) are optional |
| `value` | a label and one figure | `tone` is `ok`, `warn` or `error` |
| `sparkline` | the small daily bars | 1 to 14 finite numbers |
| `text` | one plain line | at most 120 characters |
| `link` | one link | `https://` only; a plain `http` link is dropped |

Limits that make the whole document invalid (one error tile reading "Plugin output invalid", never a
half-drawn tile): more than 8 rows, any string over 120 characters, a number that is not finite, a
percentage outside 0-100, an unknown tone, or `rows` that is not a list. An unknown `kind` is
dropped instead, so a newer plugin on an older Crossfeed degrades into fewer rows.

## The security model, plainly

A plugin is code from someone else, so Crossfeed treats it as such:

- **No credentials.** The command runs with an environment built from nothing: `PATH`, `HOME`,
  `LANG` and `CROSSFEED_PLUGIN_STATE` (a private per-plugin state directory). No provider token, no
  API key, no `FLEET_*` variable reaches it, whatever the parent process holds. Use
  `CROSSFEED_PLUGIN_STATE` for a cache; never look for a token.
- **No input, bounded output.** stdin is closed, stdout is capped at 64 KB, and a plugin that
  prints more is refused rather than truncated.
- **A deadline.** `timeout_seconds` is enforced by killing the command's whole process group; a
  plugin cannot leave children behind on purpose or by accident.
- **A total budget.** The glance runs every enabled tile in parallel inside 15 seconds, and caches
  each result for its `refresh_seconds`, so a slow plugin costs one refresh, not every open.
- **It is not a sandbox.** The command runs as your own user and can read what you can read. That is
  why nothing installs, updates or enables itself: `plugin add` without `--yes` prints the exact
  commands it would run and stops, `plugin update` shows the new commit and the command diff first,
  and the glance only runs tiles of plugins you enabled. Read the commands before you say yes.

## Testing a plugin

```bash
python3 scripts/fleetctl.py plugin run <plugin>/<tile>        # fresh run, human reading
python3 scripts/fleetctl.py plugin run <plugin>/<tile> --json # the document, exactly as the glance keeps it
```

Inside the plugin folder, `python3 tile.py | python3 -m json.tool` is the whole loop: the command
prints the document, and Crossfeed's rules above are all it checks. `examples/plugins/hello-tile/`
is a plugin with two tiles (a text tile and a pace bar) and no dependency beyond the standard
library; copy it as a starting point.

## Where things live

| Thing | Path |
|---|---|
| Installed plugins | `~/.local/share/crossfeed/plugins/<id>` (`XDG_DATA_HOME`; `FLEET_PLUGINS_DIR` overrides) |
| Registry | `~/.config/orchestrator/plugins.json` (`XDG_CONFIG_HOME`; `FLEET_PLUGINS_FILE` overrides) |
| Private state a plugin may use | `<state-dir>/plugin-state/<id>` |
| Cached tile documents | `<state-dir>/plugin-cache/<id>/<tile>.json` |
| Removed plugins | `<state-dir>/plugin-trash/<id>-<time>/` |

The registry maps a plugin id to `{source, commit, enabled}`. Nothing updates on its own: a plugin
stays at the commit you installed until you run `plugin update`.
