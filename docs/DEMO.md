# Console demo and screenshots

The demo runs the real console with fictional subscriptions, prices and quota observations. Its overlay and runtime live in a temporary directory, quota refresh is disabled, model pins are removed, and the direct Codex default is fixed for the demo instead of reading your CLI configuration. Three fictional ChatGPT workers and 48 fictional completed requests show the local Pro estimate and its fallback order; their key and wake-state paths stay inside the temporary directory. It starts no workers and reads no gateway key. Closing the demo process removes its temporary state.

You need the [Gaddi](https://github.com/Anneo22/gaddi) CLI and its connected browser extension. Put `gaddi` on PATH or pass `--gaddi /path/to/gaddi`.

```bash
python3 examples/console-demo.py
```

The console opens in a background tab. The command prints its URL and tab ID, keeping the one-use login key out of terminal output. Keep the process running while you capture it.

In another terminal, replace `TAB` with that ID:

```bash
gaddi emulate TAB '{"width":1000,"height":1220,"colorScheme":"dark"}'
gaddi screenshot TAB
gaddi emulate TAB '{"width":1000,"height":1220,"colorScheme":"light"}'
gaddi screenshot TAB

gaddi click TAB '#pick-chatgpt-work > summary'
gaddi emulate TAB '{"width":1000,"height":600,"colorScheme":"dark"}'
gaddi scroll TAB 450
gaddi screenshot TAB
```

These produce the dark overview, light overview and ChatGPT model-switch close-up. Gaddi prints each capture's local path. On macOS, convert the captures to PNG with `sips -s format png CAPTURE --out docs/console-dark.png`, using the corresponding filename for each image. Apply lossless PNG compression before committing; keep the captured pixels intact. Reset emulation with `gaddi emulate TAB '{"reset":true}'`, close your tab with `gaddi close TAB`, then stop the demo with Ctrl-C. Normal use is `python3 scripts/fleetctl.py console` against your own configuration.
