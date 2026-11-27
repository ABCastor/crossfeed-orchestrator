// The panel's view model and renderer, run headless: the four r11b deltas, the tile order the
// preferences file gives, plugin tiles, and the promise that nothing external is loaded.
"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const PANEL = path.join(ROOT, "scripts", "console-assets", "panel");

function sandbox() {
  // In a browser `window` IS the global object, and the panel's scripts rely on that (one script
  // sets window.blocksFor, another calls it by name). Mirror that here rather than half-emulate it.
  const globals = { console, Intl, JSON };
  globals.window = globals;
  const context = vm.createContext(globals);
  for (const file of ["logos.js", "model.js", "render.js"]) {
    vm.runInContext(fs.readFileSync(path.join(PANEL, file), "utf8"), context, { filename: file });
  }
  return context;
}

function glance(overrides = {}) {
  const days = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14];
  return {
    schema: 1,
    title: "Claude 12% · Codex 40%",
    generated_at: "2026-11-27T10:00:00Z",
    pools: [
      { pool: "claude", label: "Claude", state: "healthy", used_percent: 50, subscription: "claude",
        plan: { name: "Claude Max 20x" }, status_url: "https://status.claude.com/",
        reading_age_s: 240, reading_observed_at: "2026-11-27T09:56:00Z",
        tokens: { today: 240000, days },
        limits: [
          { name: "weekly", title: "Weekly", minutes: 10080, used_percent: 50, active: true,
            resets: "Tue 1 Dec 14:00", reset_at: "2026-12-01T13:00:00Z",
            pace: { elapsed_percent: 39.7, status: "deficit", words: "10% in deficit, runs out in 2d 18h", runs_out_in_s: 240235 } },
          { name: "claude-weekly-scoped-fable", title: "Weekly", scope: "Fable only", minutes: 10080, used_percent: 16,
            active: true, resets: "Tue 1 Dec 14:00",
            pace: { elapsed_percent: 39.7, status: "surplus", words: "24% surplus" } },
        ],
        limit_resets: [
          { key: "weekly", label: "Weekly limit reset", left: 2, total: 2, clears: ["seven_day"],
            expires_at: "2026-12-10T00:00:00Z", usable_now: true, next: true },
        ] },
      { pool: "codex", label: "Codex (ChatGPT)", state: "healthy", used_percent: 71, subscription: "codex",
        plan: { name: "ChatGPT Pro" }, reading_age_s: 300,
        limits: [{ name: "weekly", title: "Weekly", minutes: 10080, used_percent: 71, active: true,
                   resets: "Thu 3 Dec 00:15", pace: { elapsed_percent: 23, status: "deficit", words: "48% in deficit" } }],
        credits: { balance: 2, expires_at: "2026-12-12T22:44:04Z" } },
      { pool: "github-copilot-student", label: "GitHub Copilot", state: "healthy", used_percent: 37,
        limits: [{ name: "monthly", title: "Monthly", minutes: 43200, used_percent: 37, active: true, resets: "Thu 10 Dec" }] },
      { pool: "gemini-metered", label: "Gemini API", plan: { name: "Gemini API key" } },
    ],
    subscriptions: [{ id: "claude", level: "normal", pools: ["claude"] }, { id: "codex", level: "forced", pools: ["codex"] }],
    subscription_spend: {
      plans: [{ id: "claude", name: "Claude Max 20x", amount: "183.00", currency: "GBP", period: "month", share: null, operator_amount: "183.00" },
              { id: "codex", name: "ChatGPT Pro", amount: "200", currency: "USD", period: "month", share: "1/2", operator_amount: "100.00" }],
      monthly: [], unpriced: 0, unsplit: 1, unavailable: false,
      usd: { total: "432.33", operator: "332.33", rates_as_of: "2026-11-27", approximate: true },
    },
    plugins: [{ plugin: "hello-tile", tile: "hello-tile/bar", tab: "claude", observed_at: "2026-11-27T10:00:00Z", error: null,
                doc: { title: "A pace bar", status: "warn", rows: [
                  { kind: "bar", label: "Minutes", used: 58, pace: 41, window: "Monthly", resets_at: "2026-12-01T00:00:00Z" },
                  { kind: "value", label: "Failed today", value: "3", tone: "warn" },
                  { kind: "link", label: "Open", url: "https://example.com/run" }] } }],
    ...overrides,
  };
}

function build(documentData, preferences) {
  const context = sandbox();
  const doc = documentData || glance();
  const prefs = preferences || { show: "used", tabs: ["overview", "claude", "codex", "copilot", "other"],
                                 tiles: {}, hidden_tiles: {}, hidden_tabs: [] };
  vm.runInContext("window.__doc = " + JSON.stringify(doc) + "; window.__prefs = " + JSON.stringify(prefs) + ";", context);
  vm.runInContext("window.buildModel(window.__doc, window.__prefs)", context);
  return context;
}

const render = (context, tab, order, mode) =>
  vm.runInContext(`window.renderPanel("B", ${JSON.stringify(tab)}, "light", { order: ${order ? JSON.stringify(order) : "null"}, mode: ${JSON.stringify(mode || "used")} })`, context);

test("the plan label is the overlay plan's name without the vendor word the header already says", () => {
  const context = build();
  const vmModel = context.window.VM;
  assert.equal(vmModel.tabs.claude.plan, "Max 20x");
  assert.equal(vmModel.tabs.codex.plan, "Pro");
  const ultra = glance();
  ultra.pools[0].plan = { name: "Claude Ultra" };
  assert.equal(build(ultra).window.VM.tabs.claude.plan, "Ultra");
  // Another vendor's word stays: an Antigravity header does not make "Google AI Pro" redundant.
  const agy = glance();
  agy.pools.push({ pool: "antigravity-gemini", label: "Antigravity · Gemini", used_percent: 62,
                   plan: { name: "Google AI Pro" },
                   limits: [{ name: "five_hour", title: "5-hour", minutes: 300, used_percent: 62, active: true, resets: "today 16:15" }] });
  const prefs = { show: "used", tabs: ["overview", "antigravity"], tiles: {}, hidden_tiles: {}, hidden_tabs: [] };
  assert.equal(build(agy, prefs).window.VM.tabs.antigravity.plan, "Google AI Pro");
  assert.equal(build(agy, prefs).window.VM.tabs.antigravity.title, "AGY");
  assert.equal(build(agy, prefs).window.VM.tabs.antigravity.fullTitle, "Antigravity");
});

test("the Resets tile names itself and its row button is Use", () => {
  const html = render(build(), "claude");
  assert.match(html, /<div class="rhead">Resets<\/div>/);
  assert.match(html, /data-act="ask"[^>]*>Use<\/button>/);
  assert.doesNotMatch(html, /Use reset/);
  // The confirm keeps the contract's words and both buttons.
  const confirmed = vm.runInContext('window.renderPanel("B","claude","light",{order:["limits","resets"],confirm:"weekly"})',
                                    build());
  assert.match(confirmed, /Use the weekly reset\?/);
  assert.match(confirmed, /This can't be undone\./);
  assert.match(confirmed, /Cancel/);
  assert.match(confirmed, /data-act="use"[^>]*>Use<\/button>/);
});

test("a surplus run is the deficit's shape in green, from the fill end to the pace tick", () => {
  const html = render(build(), "claude", ["limits"]);
  assert.match(html, /class="seg-spare"/);
  assert.match(html, /class="seg-deficit"/);
  const css = fs.readFileSync(path.join(PANEL, "panel.css"), "utf8");
  const spare = /\.bar \.seg-spare \{([^}]*)\}/.exec(css)[1];
  assert.match(spare, /repeating-linear-gradient/);          // striped, like the deficit
  assert.doesNotMatch(spare, /top: 25%/);                     // full bar height, not an inset run
});

test("links live in their own tile: usage and cost, status, console", () => {
  const html = render(build(), "claude");
  assert.match(html, /href="\/usage#claude"[^>]*>Usage and cost/);
  assert.match(html, /href="https:\/\/status\.claude\.com\/"[^>]*>Status/);
  assert.match(html, /href="\/"[^>]*>Console/);
  // The tokens tile keeps only its number and sparkline.
  const tokens = render(build(), "claude", ["tokens"]);
  assert.match(tokens, /tokens today/);
  assert.match(tokens, /class="spark"/);
  assert.doesNotMatch(tokens, /Usage and cost/);
  // The incident line stays in the header area without its own link.
  const troubled = glance();
  troubled.pools[0].incident = { level: "minor", text: "Some users are affected" };
  const withIncident = render(build(troubled), "claude");
  const incident = /<div class="foot"><div class="incident[\s\S]*?<\/div><\/div>/.exec(withIncident)[0];
  assert.match(incident, /Some users are affected/);
  assert.doesNotMatch(incident, /<a |Status/);
});

test("the preferences file decides which tiles exist, in which order", () => {
  const prefs = { show: "left", tabs: ["overview", "codex", "claude"],
                  tiles: { claude: ["links", "limits", "resets"], codex: ["links"] },
                  hidden_tiles: { claude: ["resets"] }, hidden_tabs: [] };
  const context = build(null, prefs);
  const model = context.window.VM;
  assert.deepEqual([...model.order], ["codex", "claude"]);     // the operator's tab order, minus absent ids
  const claude = render(context, "claude", null, "left");
  const tiles = [...claude.matchAll(/data-block="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(tiles, ["links", "limits"]);          // hidden "resets" is not drawn, order kept
  assert.match(claude, /data-mode="left"|16% left|50% left/);
  const hidden = build(null, { ...prefs, hidden_tabs: ["codex"] });
  assert.deepEqual([...hidden.window.VM.order], ["claude"]);
  assert.doesNotMatch(render(hidden, "overview"), /data-tab-id="codex"/);
});

test("a provider tile with nothing to say is not drawn, rather than drawn empty", () => {
  const html = render(build(), "copilot");
  assert.doesNotMatch(html, /data-block="resets"/);
  assert.doesNotMatch(html, /data-block="credits"/);
  assert.doesNotMatch(html, /data-block="policy"/);
  assert.match(html, /data-block="limits"/);
});

test("a plugin tile is drawn with the panel's own parts, and a missing one says so", () => {
  const prefs = { show: "used", tabs: ["overview", "claude"], tiles: { claude: ["limits", "plugin:hello-tile/bar", "plugin:gone/x"] },
                  hidden_tiles: {}, hidden_tabs: [] };
  const html = render(build(null, prefs), "claude");
  assert.match(html, /class="ptile"/);
  assert.match(html, /A pace bar/);
  assert.match(html, /Failed today/);
  assert.match(html, /href="https:\/\/example\.com\/run"/);
  assert.match(html, /58% used/);
  // A plugin tile with no reading is not drawn at all; asked for by name, it says why.
  assert.doesNotMatch(html, /data-block="plugin:gone\/x"/);
  const forced = vm.runInContext('window.renderPanel("B","claude","light",{order:["plugin:gone/x"]})', build(null, prefs));
  assert.match(forced, /not enabled or has no reading/);
  // A plugin tile can sit on Overview too, beside the built-in parts.
  const overviewPrefs = { ...prefs, tiles: { overview: ["alert", "plugin:hello-tile/bar"] } };
  const overview = render(build(null, overviewPrefs), "overview");
  assert.match(overview, /data-block="plugin:hello-tile\/bar"/);
  assert.match(overview, /A pace bar/);
});

test("overview keeps the alert, the providers, the subscriptions, the tokens and the links", () => {
  const html = render(build(), "overview");
  const tiles = [...html.matchAll(/data-block="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(tiles, ["alert", "providers", "subscriptions", "tokens", "links"]);
  assert.match(html, /Gemini|runs out|Nothing runs out/);
  assert.match(html, /You pay a month/);
  assert.match(html, /≈ \$332|\$332/);
  assert.match(html, /href="\/usage"/);
});

test("nothing external is loaded: no web font, no import, no remote URL in the stylesheet", () => {
  for (const file of ["panel.css", "panel.js", "model.js", "render.js", "drag.js", "d2.js", "theme.js", "logos.js", "panel.html"]) {
    const text = fs.readFileSync(path.join(PANEL, file), "utf8");
    assert.doesNotMatch(text, /https?:\/\/(?!status|github|example|www\.w3\.org|api)/, file);  // links are data, not loads
    assert.doesNotMatch(text, /fonts\.googleapis|fonts\.gstatic|@font-face|@import\s+url/, file);
  }
  const css = fs.readFileSync(path.join(PANEL, "panel.css"), "utf8");
  assert.match(css, /font-family:[^;}]*system-ui/);          // the machine's own fonts, not a download
  const html = fs.readFileSync(path.join(PANEL, "panel.html"), "utf8");
  assert.doesNotMatch(html, /<style/);
});
