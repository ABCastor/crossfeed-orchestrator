# Hello tile

The smallest useful Crossfeed plugin: two tiles, the standard library only, no network.

- `hello` (Overview) prints one text row, one value row and one link.
- `bar` (Claude) prints a pace bar, a value row and a sparkline: everything the tile document offers
  except a bare text line.

Install it and look at it:

```bash
python3 ../../../scripts/fleetctl.py plugin add .          # what it would run; changes nothing
python3 ../../../scripts/fleetctl.py plugin add . --yes     # installs and enables it
python3 ../../../scripts/fleetctl.py plugin run hello-tile/bar
```

Open the panel (the menu bar app's "Open panel", the console's `/panel`, or
`fleetctl.py console`), go to Settings, and the two tiles are in the list.

`crossfeed-plugin.json` is the whole contract: see [docs/PLUGINS.md](../../../docs/PLUGINS.md).
