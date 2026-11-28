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

// The real glance marks a pool `measured` and every window `kind: "window"`; the menu bar title reads both.
function measured(doc) {
  for (const pool of doc.pools) {
    if (pool.measured === undefined) pool.measured = pool.used_percent != null && (pool.limits || []).length > 0;
    for (const limit of pool.limits || []) if (limit.kind === undefined) limit.kind = "window";
  }
  return doc;
}

function build(documentData, preferences) {
  const context = sandbox();
  const doc = measured(documentData || glance());
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

test("links live in their own tile: usage and cost and status, no Console (the header already opens it)", () => {
  const html = render(build(), "claude", ["links"]);
  assert.match(html, /href="\/usage#claude"[^>]*>Usage and cost/);
  assert.match(html, /href="https:\/\/status\.claude\.com\/"[^>]*>Status/);
  assert.doesNotMatch(html, /Console/);
  assert.doesNotMatch(render(build(), "overview", ["links"]), /Console/);
  // The tokens tile keeps only its number and sparkline; the whole tile goes to the usage page.
  const tokens = render(build(), "claude", ["tokens"]);
  assert.match(tokens, /tokens today/);
  assert.match(tokens, /class="spark"/);
  assert.match(tokens, /data-href="\/usage#claude"/);
  assert.doesNotMatch(tokens, /Usage and cost/);
});

test("Status is a tile, not a pinned line: an incident, or a calm word, linked to the provider's page", () => {
  const troubled = glance();
  troubled.pools[0].incident = { level: "minor", text: "Some users are affected" };
  const context = build(troubled);
  const html = render(context, "claude");
  assert.doesNotMatch(html, /class="foot"/);
  const status = /data-block="status"[\s\S]*?<div class="blk|data-block="status"[\s\S]*$/.exec(html)[0];
  assert.match(status, /Minor issue/);
  assert.match(status, /Some users are affected/);
  assert.match(status, /data-href="https:\/\/status\.claude\.com\/"/);
  assert.match(render(build(), "claude", ["status"]), /No incident reported/);
  // Overview lists every current incident by itself, is not drawn when there is none, and is pinned only when the operator adds it.
  assert.match(render(context, "overview", ["status"]), /Claude<\/b> · Minor issue/);
  assert.deepEqual([...context.window.blocksFor("overview", "B")], ["alert", "providers", "subscriptions", "tokens", "status", "links"]);
  const quiet = build();
  assert.equal(quiet.window.tileAvailable("overview", "status"), false);
  assert.deepEqual([...quiet.window.blocksFor("overview", "B")], ["alert", "providers", "subscriptions", "tokens", "links"]);
  assert.deepEqual([...quiet.window.defaultTiles("overview")], ["alert", "providers", "subscriptions", "tokens", "links"]);
  const pinned = build(null, { show: "used", tabs: ["overview", "claude"], tiles: { overview: ["alert", "status", "links"] }, hidden_tiles: {} });
  assert.equal(pinned.window.tileAvailable("overview", "status"), true);
  assert.match(render(pinned, "overview", ["status"]), /No incidents/);
  // The operator can still turn an incident's Status tile off.
  const hidden = build(troubled, { show: "used", tabs: ["overview", "claude"], tiles: {}, hidden_tiles: { overview: ["status"] } });
  assert.equal(hidden.window.blocksFor("overview", "B").includes("status"), false);
});

test("everything clickable carries an address: limit rows, Overview cards, plugin tiles", () => {
  const html = render(build(), "claude", ["limits"]);
  assert.match(html, /class="lrow hit" data-href="\/#pool-claude" role="link" tabindex="0"/);
  assert.match(render(build(), "overview", ["providers"]), /data-goto="claude"/);
  const prefs = { show: "used", tabs: ["overview", "claude"], tiles: { claude: ["plugin:hello-tile/bar"] }, hidden_tiles: {}, hidden_tabs: [] };
  assert.match(render(build(null, prefs), "claude"), /class="ptile hit" data-href="https:\/\/example\.com\/run"/);
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
  assert.match(html, /class="ptile( hit)?"/);
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
  assert.deepEqual(tiles, ["alert", "providers", "subscriptions", "tokens", "links"]);   // Status only when there is an incident or it is pinned
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

// ---- API pools ------------------------------------------------------------------------------
const OPENROUTER = { pool: "openrouter-free", label: "OpenRouter", measured: false, plan: { name: "Pay as you go" },
  status_url: "https://status.openrouter.ai/", limits: [],
  api: { balance_usd: 1.79, credits_usd: 26, key_limit_usd: 10, key_remaining_usd: 3.39,
         spend: { today_usd: 0, week_usd: 0.09, month_usd: 6.61 }, budget: { monthly_usd: 20, source: "operator" },
         days: [{ date: "2026-11-26", usd: 0.4, requests: 12, tokens: 51000 }, { date: "2026-11-27", usd: 0.1, requests: 3 }],
         top_models: [{ model: "anthropic/claude-sonnet-5.5", usd: 3.1, requests: 221 }],
         source: "openrouter-api", observed_at: "2026-11-27T09:50:00Z", billing_url: "https://openrouter.ai/settings/credits" } };
const GEMINI = { pool: "gemini-metered", label: "Gemini API", measured: false, limits: [],
  api: { spend: { today_usd: 0.42, week_usd: 1.9, month_usd: 3.35 }, days: [{ date: "2026-11-27", usd: 0.42, requests: 5 }],
         source: "local-meter", source_label: "Measured by Crossfeed", observed_at: "2026-11-27T09:50:00Z" } };
const apiPrefs = (extra) => ({ show: "used", tabs: ["overview", "claude", "openrouter-free", "gemini-metered", "other"], tiles: {}, hidden_tiles: {}, hidden_tabs: [], ...extra });
const withApi = () => { const doc = glance(); doc.pools.push(OPENROUTER); doc.pools.splice(doc.pools.findIndex((p) => p.pool === "gemini-metered"), 1, GEMINI); return doc; };

test("an API pool is a tab of its own, not a line in Other", () => {
  const model = build(withApi(), apiPrefs()).window.VM;
  assert.deepEqual([...model.order], ["claude", "openrouter-free", "gemini-metered"]);
  assert.equal(model.tabs["openrouter-free"].title, "OpenRouter");
  assert.equal(model.tabs["gemini-metered"].title, "Gemini API");
  assert.deepEqual([...model.other], []);
  // A pool with an api block and no tab in the preferences still gets one, after the listed ones.
  const unlisted = build(withApi(), apiPrefs({ tabs: ["overview", "claude", "other"] })).window.VM;
  assert.ok(unlisted.order.includes("openrouter-free") && unlisted.order.includes("gemini-metered"));
});

test("the Spend tile paces the month against the operator's budget and says where a local figure comes from", () => {
  const context = build(withApi(), apiPrefs());
  const html = render(context, "openrouter-free", ["spend"]);
  assert.match(html, /\$6\.61 of \$20\.00/);
  assert.match(html, /This month/);
  assert.match(html, /Today <\/span> <b class="num">\$0\.00|Today<\/span> <b class="num">\$0\.00/);
  assert.match(html, /class="tick"/);                                   // the pace tick: the share of the month gone
  const gemini = render(context, "gemini-metered", ["spend"]);
  assert.match(gemini, /Measured by Crossfeed/);
  assert.match(gemini, /No monthly budget/);
  assert.doesNotMatch(gemini, /class="bar"/);                          // no budget, no bar: figures only
  // The operator's own figure in the preferences wins over the one the glance carried, and left mode flips the words.
  const budgeted = build(withApi(), apiPrefs({ budgets: { "gemini-metered": 10, "openrouter-free": 7 } }));
  assert.match(render(budgeted, "gemini-metered", ["spend"]), /\$3\.35 of \$10\.00/);
  assert.match(render(budgeted, "openrouter-free", ["spend"]), /\$6\.61 of \$7\.00/);
  assert.match(render(budgeted, "gemini-metered", ["spend"], "left"), /\$6\.65 left/);
  const over = build(withApi(), apiPrefs({ budgets: { "openrouter-free": 5 } }));
  assert.match(render(over, "openrouter-free", ["spend"]), /Over budget/);
});

test("Balance, Models and Activity draw what the pool reports and nothing else", () => {
  const context = build(withApi(), apiPrefs());
  const balance = render(context, "openrouter-free", ["balance"]);
  assert.match(balance, /Credit[\s\S]*\$26\.00/);
  assert.match(balance, /Left[\s\S]*\$1\.79/);
  assert.match(balance, /Key limit[\s\S]*\$10\.00/);
  assert.match(balance, /data-href="https:\/\/openrouter\.ai\/settings\/credits"/);
  assert.match(render(context, "openrouter-free", ["models"]), /claude-sonnet-5\.5[\s\S]*\$3\.10/);
  assert.match(render(context, "openrouter-free", ["activity"]), /\$0\.10<\/b> <span class="q">today/);
  // Gemini reports no balance and no models: those tiles are not drawn.
  assert.equal(context.window.tileAvailable("gemini-metered", "balance"), false);
  assert.equal(context.window.tileAvailable("gemini-metered", "models"), false);
  assert.equal(context.window.tileAvailable("gemini-metered", "spend"), true);
  assert.deepEqual([...context.window.blocksFor("gemini-metered", "B")], ["spend", "activity", "status", "links"].filter((id) => id !== "status"));
});

test("an API tab's links add Billing, and the usage link goes to the pool", () => {
  const html = render(build(withApi(), apiPrefs()), "openrouter-free", ["links"]);
  assert.match(html, /href="\/usage#openrouter-free"[^>]*>Usage and cost/);
  assert.match(html, /href="https:\/\/status\.openrouter\.ai\/"[^>]*>Status/);
  assert.match(html, /href="https:\/\/openrouter\.ai\/settings\/credits"[^>]*>Billing/);
});

test("the graph offers the metrics a pool has and a 7, 14 or 30 day range, and keeps the operator's choice", () => {
  const model = build(withApi(), apiPrefs({ tile_options: { "openrouter-free/activity": { metric: "requests", days: 7 } } })).window;
  const sp = model.sparkInfo("openrouter-free", "activity");
  assert.equal(sp.metric, "requests");
  assert.equal(sp.days, 7);
  assert.deepEqual([...sp.metrics], ["tokens", "cost", "requests"]);
  assert.match(render(model, "openrouter-free", ["activity"]), /requests today/);
  // A choice the pool cannot honour falls back; a bad range falls back to 14.
  const odd = build(withApi(), apiPrefs({ tile_options: { "gemini-metered/activity": { metric: "tokens", days: 9 } } })).window;
  assert.equal(odd.sparkInfo("gemini-metered", "activity").metric, "cost");
  assert.equal(odd.sparkInfo("gemini-metered", "activity").days, 14);
  // A provider's token tile can show cost when its usage log priced it, over its 30 days.
  const doc = glance();
  doc.pools[0].usage_cost = { today_usd: 1.5, daily: Array.from({ length: 30 }, (_, i) => ({ day: "2026-11-" + String(i % 28 + 1).padStart(2, "0"), tokens: 1000 * (i + 1), usd: 0.1 * (i + 1) })) };
  const costly = build(doc, apiPrefs({ tile_options: { "claude/tokens": { metric: "cost", days: 30 } } })).window;
  const info = costly.sparkInfo("claude", "tokens");
  assert.equal(info.values.length, 30);
  assert.equal(info.metric, "cost");
  assert.match(render(costly, "claude", ["tokens"]), /\$3\.00<\/b> <span class="q">today/);
});

test("the menu bar title follows icon, text, provider, window, count and extra", () => {
  const model = (menubar) => build(withApi(), apiPrefs({ menubar })).window.VM;
  const base = model({}).barTitle({});
  assert.equal(base.text, "Week 71%");                      // Codex, most in deficit
  assert.equal(base.icon, "provider");
  assert.equal(base.logo, "openai");
  assert.equal(model({}).barTitle({ text: "full" }).text, "Codex Week 71%");
  assert.equal(model({}).barTitle({ text: "number", count: "left" }).text, "29% left");
  assert.equal(model({}).barTitle({ text: "none" }).text, "");
  assert.equal(model({}).barTitle({ icon: "crossfeed" }).icon, "crossfeed");
  assert.equal(model({}).barTitle({ icon: "none", text: "none" }).icon, "crossfeed");   // never an invisible item
  assert.equal(model({}).barTitle({ show: "claude", text: "full" }).text, "Claude Week 50%");
  assert.equal(model({}).barTitle({ show: "claude", text: "full" }).logo, "claude-color");
  assert.equal(model({}).barTitle({ window: "Week", show: "claude", text: "window" }).text, "Week 50%");
  assert.equal(model({}).barTitle({ text: "number", extra: "pace" }).text, "71% · 48% over");
  assert.equal(model({}).barTitle({ show: "claude", text: "number", extra: "reset" }).text, "50% · 4d 3h");
  // a provider with no reading falls back to the most pressed one
  assert.equal(model({}).barTitle({ show: "openrouter-free", text: "number" }).text, "71%");
});

test("settings do something visible: appearance keys, defaults and the page's own words", () => {
  const css = fs.readFileSync(path.join(PANEL, "theme.js"), "utf8");
  assert.match(css, /body\.panelpage\.\$\{theme\}/);        // the page and the sheet take the palette from the chosen theme, not the media query
  const panelCss = fs.readFileSync(path.join(PANEL, "panel.css"), "utf8");
  assert.doesNotMatch(panelCss, /prefers-color-scheme/);
});

test("the panel and the Linux tray title the shared fixture the same way (menubar/title_test.go holds the same table)", () => {
  const doc = JSON.parse(fs.readFileSync(path.join(ROOT, "tests", "fixtures", "panel-r12.json"), "utf8"));
  const prefs = { show: "used", tabs: ["overview", "claude", "codex", "copilot", "openrouter-free", "gemini-metered", "chatgpt-work", "other"], tiles: {}, hidden_tiles: {}, hidden_tabs: [] };
  const model = build(doc, prefs).window.VM;
  const table = [
    [{}, "Week 71%", "provider", "openai"],
    [{ text: "full" }, "Codex Week 71%", "provider", "openai"],
    [{ text: "number", count: "left" }, "29% left", "provider", "openai"],
    [{ show: "claude", text: "full" }, "Claude Week 58%", "provider", "claude-color"],
    [{ window: "5h", text: "full" }, "Claude 5h 41%", "provider", "claude-color"],
    [{ extra: "pace" }, "Week 71% · 48% over", "provider", "openai"],
    [{ extra: "cost" }, "Week 71% · US$11.68 today", "provider", "openai"],
    [{ text: "none" }, "", "provider", "openai"],
    [{ icon: "crossfeed" }, "Week 71%", "crossfeed", "openai"],
    [{ icon: "none" }, "Week 71%", "none", "openai"],
    [{ icon: "none", text: "none" }, "", "crossfeed", "openai"],
    [{ show: "openrouter-free", text: "number" }, "71%", "provider", "openai"],
    [{ icon: "tanks", text: "loud" }, "Week 71%", "provider", "openai"],
  ];
  for (const [choice, text, icon, logo] of table) {
    const got = model.barTitle({ icon: "provider", text: "window", ...choice });
    assert.deepEqual([got.text, got.icon, got.logo], [text, icon, logo], JSON.stringify(choice));
  }
});

// ---- round 12c: held resets, the Castor theme switch, the Resets setting ---------------------------------

function heldGlance(used, held, pace) {
  const doc = glance();
  const weekly = doc.pools[0].limits[0];
  weekly.used_percent = used;
  weekly.resets_held = held;
  weekly.pace = pace || { elapsed_percent: 39.7, status: "deficit", words: "56% in deficit" };
  doc.pools[0].used_percent = used;
  return doc;
}

test("a window with a reset held is never said to run out: the alert line says it is held and offers Use", () => {
  const model = build(heldGlance(96, 1)).window.VM;
  assert.equal(model.verdict.words, "Claude 96% · 🎟️ 1 held");
  assert.equal(model.verdict.tone, "hot");
  assert.deepEqual(JSON.parse(JSON.stringify(model.verdict.use)), { key: "weekly", tab: "claude" });
  assert.doesNotMatch(model.verdict.words, /runs out/i);
  const html = render(build(heldGlance(96, 1)), "overview", ["alert"]);
  assert.match(html, /Claude 96% · 🎟️ 1 held/);
  assert.match(html, /data-act="askgo" data-key="weekly" data-tab="claude">Use<\/button>/);
  assert.doesNotMatch(html, /runs out/i);
  // A used-up window reads the same, with the used-up word.
  assert.equal(build(heldGlance(100, 2)).window.VM.verdict.words, "Claude used up · 🎟️ 2 held");
  // The limit row carries the held mark, and a backend run-out in the pace words cannot reach it.
  const defended = heldGlance(80, 1, { elapsed_percent: 50, status: "deficit", words: "30% in deficit, runs out in 1d 2h", runs_out_in_s: 93600 });
  const row = build(defended).window.VM.tabs.claude.sections[0].rows.find((r) => r.key === "weekly");
  assert.equal(row.runsOut, null);
  assert.equal(row.hot, false);
  const limits = render(build(defended), "claude", ["limits"]);
  assert.match(limits, /pace held"><span class="em">🎟️<\/span> 1 held/);
  assert.doesNotMatch(limits, /Runs out/);
});

test("without a held reset the alert line still says when the tightest window runs out", () => {
  const doc = heldGlance(50, 0, { elapsed_percent: 39.7, status: "deficit", words: "10% in deficit, runs out in 2d 18h", runs_out_in_s: 240235 });
  assert.match(build(doc).window.VM.verdict.words, /runs out in 2d 18h/);
});

test("Use in the alert line opens the reset's own confirmation and never spends it", () => {
  const source = fs.readFileSync(path.join(PANEL, "panel.js"), "utf8");
  const askgo = /act === "askgo"[^\n]*/.exec(source)[0];
  assert.match(askgo, /selectTab\(el\.dataset\.tab\)/);
  assert.match(askgo, /S\.confirm = key/);
  assert.doesNotMatch(askgo, /useReset/);
});

test("the automatic-use notice shows under the alert line", () => {
  const doc = heldGlance(10, 0);
  doc.notices = [{ kind: "auto_reset", text: "Used the weekly reset automatically", at: "2026-11-28T10:00:00Z" }];
  assert.match(render(build(doc), "overview", ["alert"]), /notice q">Used the weekly reset automatically</);
});

test("the header has the Castor theme switch next to refresh and settings, showing the current choice", () => {
  const context = build();
  const header = (appearance) => vm.runInContext(`window.renderPanel("B", "claude", "light", { order: [], appearance: ${JSON.stringify(appearance)} })`, context)
    .match(/<span class="ibtns">.*?<\/span><\/div>/s)[0];
  const order = (html) => [...html.matchAll(/data-act="(theme|refresh|settings)"/g)].map((m) => m[1]);
  assert.deepEqual(order(header("system")), ["theme", "refresh", "settings"]);
  const cycle = [["system", "light", "t-system"], ["light", "dark", "t-sun"], ["dark", "system", "t-moon"]];
  const drawings = { "t-system": /<circle cx="12" cy="12" r="9"\/>/, "t-sun": /<circle cx="12" cy="12" r="5\.25"\/>/, "t-moon": /M20\.6 14\.65A9 9 0 0 1 9\.35 3\.4/ };
  for (const [now, next, mark] of cycle) {
    const html = header(now);
    assert.match(html, new RegExp(`aria-label="Theme: ${now}\\. Switch to ${next}"`));
    assert.match(html, /data-theme-toggle/);
    for (const [other, pattern] of Object.entries(drawings)) {
      assert.equal(pattern.test(html), other === mark, `${now} draws only ${mark}`);
    }
  }
  // The same switch in every header: Overview, a provider, Other.
  for (const tab of ["overview", "claude", "other"]) assert.match(render(context, tab, []), /data-act="theme"/);
  const source = fs.readFileSync(path.join(PANEL, "panel.js"), "utf8");
  assert.match(source, /system: "light", light: "dark", dark: "system"/);   // System -> Light -> Dark -> System
});

test("Settings > General has Resets: Ask first / Use when used up, kept in panel.json beside Appearance", () => {
  const source = fs.readFileSync(path.join(PANEL, "panel.js"), "utf8");
  assert.match(source, /field\("Resets", seg\("auto_use_resets"[^\n]*\["off", "Ask first"\], \["when_used_up", "Use when used up"\]/);
  assert.match(source, /name === "auto_use_resets"\) p\.auto_use_resets = value === "when_used_up" \? "when_used_up" : "off"/);
  assert.match(source, /field\("Appearance"/);   // the Appearance row stays
});
