// Browser flows for the web panel: the real console serving a fixed glance document (panel_flow_server.py),
// driven by headless Chromium. Needs playwright-core and a Chrome or Chromium; without them it says so and
// exits 0, so the unit suites stay runnable anywhere.
//
//   PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core CHROME=/usr/bin/google-chrome \
//   RENDERS=/abs/dir node tests/panel-flows.cjs
"use strict";
const { spawn } = require("node:child_process");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const ROOT = path.resolve(__dirname, "..");
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_CORE || "playwright-core"); } catch (error) {
  console.log("skip: playwright-core is not installed (set PLAYWRIGHT_CORE)"); process.exit(0);
}
const CHROME = process.env.CHROME || ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"].find((p) => fs.existsSync(p));
if (!CHROME) { console.log("skip: no Chrome or Chromium found (set CHROME)"); process.exit(0); }
const RENDERS = process.env.RENDERS || "";
if (RENDERS) fs.mkdirSync(RENDERS, { recursive: true });

let passed = 0;
const failures = [];
async function flow(name, fn) {
  try { await fn(); passed += 1; console.log("ok   " + name); } catch (error) { failures.push(name); console.log("FAIL " + name + "\n     " + String(error.stack || error).split("\n").slice(0, 4).join("\n     ")); }
}

function startServer(work) {
  return new Promise((resolve, reject) => {
    const child = spawn("python3", [path.join(ROOT, "tests", "panel_flow_server.py"), path.join(ROOT, "tests", "fixtures", "panel-r12.json"), work],
      { cwd: ROOT, stdio: ["pipe", "pipe", "inherit"] });
    let buffer = "";
    child.stdout.on("data", (chunk) => {
      buffer += chunk;
      const line = buffer.split("\n")[0];
      if (line && buffer.includes("\n")) { try { resolve({ child, info: JSON.parse(line) }); } catch (error) { reject(error); } }
    });
    child.on("exit", (code) => reject(new Error("server exited " + code)));
  });
}

(async () => {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "panel-flows-"));
  const { child, info } = await startServer(work);
  const base = `http://127.0.0.1:${info.port}`;
  const browser = await playwright.chromium.launch({ executablePath: CHROME, args: ["--no-sandbox"], headless: true });
  const prefsOnDisk = () => JSON.parse(fs.readFileSync(info.prefs, "utf8"));
  const resetPrefs = () => { try { fs.unlinkSync(info.prefs); } catch (error) { /* none yet */ } };

  async function open(options = {}, hash = "") {
    const context = await browser.newContext({ viewport: { width: 330, height: 900 }, colorScheme: options.scheme || "light", deviceScaleFactor: 2 });
    const page = await context.newPage();
    page.on("pageerror", (error) => { if (/ViewTransition/.test(String(error))) return; console.log("     pageerror: " + error); failures.push("pageerror: " + error); });
    if (process.env.DEBUG) page.on("console", (message) => console.log("     console." + message.type() + ": " + message.text()));
    await page.goto(base + info.login + "&next=/panel");
    if (hash) { await page.goto(base + "/panel" + hash); }
    await page.waitForFunction(() => window.__ready === true);
    return { page, context };
  }
  const tab = (page, id) => page.click(`.tab[data-tab-id="${id}"]`);
  const text = (page, selector) => page.locator(selector).first().innerText();
  const shot = async (page, name, scheme) => { if (RENDERS) await page.screenshot({ path: path.join(RENDERS, `${name}-${scheme}.png`), fullPage: true }); };

  await flow("the panel opens with the new tabs and Status, with no Console link in Links", async () => {
    resetPrefs();
    const { page, context } = await open();
    const tabs = await page.$$eval(".tab", (els) => els.map((e) => e.dataset.tabId));
    assert.deepEqual(tabs, ["overview", "claude", "codex", "copilot", "openrouter-free", "gemini-metered", "chatgpt-work", "other"]);
    await tab(page, "claude");
    assert.match(await text(page, '.blk[data-block="status"]'), /Minor issue/);
    assert.equal(await page.locator('.blk[data-block="links"] a').count(), 2);
    assert.doesNotMatch(await text(page, '.blk[data-block="links"]'), /Console/);
    assert.equal(await page.locator(".foot").count(), 0, "no incident is pinned under the header");
    const order = await page.$$eval(".blk", (els) => els.map((e) => e.dataset.block));
    assert.deepEqual(order.slice(-2), ["status", "links"]);
    await context.close();
  });

  await flow("Appearance: Dark and Light override the system and persist across a reload", async () => {
    resetPrefs();
    const { page, context } = await open({ scheme: "light" });
    await page.click('[data-act="settings"]');
    await page.click('[data-set="appearance"][data-value="dark"]');
    await page.waitForFunction(() => document.body.classList.contains("dark"));
    assert.ok(await page.locator(".panel.dark").count());
    assert.ok(await page.locator(".sheet.dark").count());
    await page.waitForTimeout(300);
    assert.equal(prefsOnDisk().appearance, "dark");
    await page.reload(); await page.waitForFunction(() => window.__ready === true);
    assert.ok(await page.locator(".panel.dark").count(), "dark survives a reload while the browser says light");
    await page.click('[data-act="settings"]');
    await page.click('[data-set="appearance"][data-value="light"]');
    await page.waitForFunction(() => document.body.classList.contains("light"));
    await page.click('[data-set="appearance"][data-value="system"]');
    await page.waitForTimeout(300);
    assert.equal(prefsOnDisk().appearance, "system");
    await context.close();
    const dark = await open({ scheme: "dark" });
    assert.ok(await dark.page.locator(".panel.dark").count(), "system follows a dark browser");
    await dark.context.close();
  });

  await flow("Menu bar: a live preview changes with icon and text, and each choice persists", async () => {
    resetPrefs();
    const { page, context } = await open();
    await page.click('[data-act="settings"]');
    await page.click('[data-page="menubar"]');
    const before = await text(page, ".bpreview .bt");
    assert.match(before, /^[A-Za-z0-9]+ \d+%/);
    assert.ok(await page.locator(".bpreview .bi .logo").count(), "the default icon is the provider's logo");
    await page.click('[data-set="mb.icon"][data-value="crossfeed"]');
    assert.ok(await page.locator(".bpreview .mark").count());
    await page.click('[data-set="mb.icon"][data-value="none"]');
    assert.equal(await page.locator(".bpreview .bi *").count(), 0);
    await page.click('[data-set="mb.icon"][data-value="provider"]');
    await page.click('[data-set="mb.text"][data-value="full"]');
    assert.match(await text(page, ".bpreview .bt"), /^Codex Week 71%$/);
    await page.click('[data-set="mb.text"][data-value="number"]');
    assert.equal(await text(page, ".bpreview .bt"), "71%");
    await page.click('[data-set="mb.count"][data-value="left"]');
    assert.equal(await text(page, ".bpreview .bt"), "29% left");
    await page.selectOption('[data-set-select="mb.show"]', "claude");
    assert.equal(await text(page, ".bpreview .bt"), "42% left");
    await page.click('[data-set="mb.text"][data-value="number"]');
    await page.click('[data-set="mb.extra"][data-value="reset"]');
    assert.match(await text(page, ".bpreview .bt"), /^42% left · \d+d \d+h$/, "the reset after the number");
    await page.click('[data-set="mb.reset"][data-value="clock"]');
    assert.match(await text(page, ".bpreview .bt"), /^42% left · (Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d\d:\d\d$/, "the same reset as a clock time");
    await page.click('[data-set="mb.extra"][data-value="none"]');
    await page.click('[data-set="mb.text"][data-value="none"]');
    assert.equal(await text(page, ".bpreview .bt"), "");
    await page.waitForTimeout(300);
    const saved = prefsOnDisk().menubar;
    assert.deepEqual([saved.icon, saved.text, saved.count, saved.show], ["provider", "none", "left", "claude"]);
    await page.reload(); await page.waitForFunction(() => window.__ready === true);
    await page.click('[data-act="settings"]'); await page.click('[data-page="menubar"]');
    assert.equal(await page.locator('[data-set="mb.count"][aria-pressed="true"]').getAttribute("data-value"), "left");
    await context.close();
  });

  await flow("Refresh: on open reads again when older than a minute; the interval and the toggle persist", async () => {
    resetPrefs();
    const calls = async (page) => (await (await page.request.get(base + "/panel.json")).json()).__refresh_calls;
    const first = await open();
    await first.page.waitForFunction(() => !document.querySelector(".ibtn.spin"));
    await first.page.waitForTimeout(400);
    const afterOpen = await calls(first.page);
    assert.ok(afterOpen >= 1, "an old reading is read again when the page opens");
    await first.context.close();
    const second = await open();
    await second.page.waitForTimeout(500);
    assert.equal(await calls(second.page), afterOpen, "a reading under a minute old is not read again");
    await second.page.click('[data-act="settings"]');
    await second.page.click('[data-toggle-refresh]');
    await second.page.click('[data-set="every"][data-value="15"]');
    await second.page.waitForTimeout(300);
    assert.deepEqual(prefsOnDisk().refresh, { on_open: false, every_minutes: 15 });
    await second.page.click('[data-set="every"][data-value="0"]');
    await second.page.waitForTimeout(300);
    assert.equal(prefsOnDisk().refresh.every_minutes, 0);
    await second.context.close();
  });

  await flow("Refresh button turns while it reads and the page redraws", async () => {
    resetPrefs();
    const { page, context } = await open();
    await page.waitForTimeout(500);
    const before = await page.evaluate(() => fetch("/panel.json").then((r) => r.json()).then((d) => d.__refresh_calls));
    await page.click('[data-act="refresh"]');
    await page.waitForTimeout(500);
    const after = await page.evaluate(() => fetch("/panel.json").then((r) => r.json()).then((d) => d.__refresh_calls));
    assert.equal(after, before + 1);
    await context.close();
  });

  await flow("Open on: Last used follows the last tab; a named tab wins; the setting persists", async () => {
    resetPrefs();
    let { page, context } = await open();
    await tab(page, "copilot");
    await page.waitForTimeout(400);
    assert.equal(prefsOnDisk().last_tab, "copilot");
    await context.close();
    ({ page, context } = await open());
    assert.equal(await page.getAttribute(".panel", "data-tab"), "copilot", "the menu opens where it was left");
    await page.click('[data-act="settings"]');
    await page.selectOption('[data-set-select="open_on"]', "codex");
    await page.waitForTimeout(300);
    assert.equal(prefsOnDisk().open_on, "codex");
    await context.close();
    ({ page, context } = await open());
    assert.equal(await page.getAttribute(".panel", "data-tab"), "codex");
    await context.close();
    ({ page, context } = await open({}, "#tab=claude"));
    assert.equal(await page.getAttribute(".panel", "data-tab"), "claude", "a link that names a tab goes there");
    await context.close();
  });

  await flow("OpenRouter tab: spend against the budget, balance, models, activity, status, links", async () => {
    resetPrefs();
    const { page, context } = await open();
    await tab(page, "openrouter-free");
    const order = await page.$$eval(".blk", (els) => els.map((e) => e.dataset.block));
    assert.deepEqual(order, ["spend", "balance", "models", "activity", "status", "links"]);
    const spend = await text(page, '.blk[data-block="spend"]');
    assert.match(spend, /\$6\.61 of \$20\.00/);
    assert.match(spend, /Today\s+\$0\.00/);
    assert.match(spend, /Month\s+\$6\.61/);
    assert.match(await text(page, '.blk[data-block="balance"]'), /\$1\.79/);
    assert.match(await text(page, '.blk[data-block="balance"]'), /Key limit\s+\$10\.00/);
    assert.match(await text(page, '.blk[data-block="models"]'), /claude-sonnet-5\.5\s+\$3\.10/);
    assert.match(await text(page, '.blk[data-block="activity"]'), /today/);
    assert.ok(await page.locator('.blk[data-block="spend"] .seg-deficit, .blk[data-block="spend"] .seg-spare, .blk[data-block="spend"] .seg-fill').count());
    assert.ok(await page.locator('.blk[data-block="spend"] .tick').count(), "the month's pace tick");
    assert.equal(await page.locator('.blk[data-block="links"] a').count(), 3);
    assert.match(await text(page, '.blk[data-block="links"]'), /Billing/);
    await shot(page, "openrouter-tab", "light");
    await context.close();
  });

  await flow("Gemini API tab: measured by Crossfeed, no budget until the operator sets one, and it persists", async () => {
    resetPrefs();
    const { page, context } = await open();
    await tab(page, "gemini-metered");
    const spend = await text(page, '.blk[data-block="spend"]');
    assert.match(spend, /Measured by Crossfeed/);
    assert.match(spend, /No monthly budget/);
    assert.equal(await page.locator('.blk[data-block="spend"] .bar').count(), 0);
    await page.click('[data-act="budget"]');
    assert.equal(await page.getAttribute('[data-page="budgets"]', "aria-pressed"), "true");
    assert.equal(await page.locator('[data-budget="chatgpt-work"]').count(), 0, "Crossfeed Chat reports no money, so it has no budget");
    assert.ok(await page.locator('[data-budget="openrouter-free"]').count());
    await page.fill('[data-budget="gemini-metered"]', "10");
    await page.press('[data-budget="gemini-metered"]', "Tab");
    await page.waitForTimeout(400);
    assert.equal(prefsOnDisk().budgets["gemini-metered"], 10);
    await page.click('[data-sheet="close"]');
    assert.match(await text(page, '.blk[data-block="spend"]'), /\$3\.35 of \$10\.00/);
    assert.equal(await page.locator('.blk[data-block="spend"] .tick').count(), 1);
    await shot(page, "gemini-api-tab", "light");
    await page.reload(); await page.waitForFunction(() => window.__ready === true);
    await tab(page, "gemini-metered");
    assert.match(await text(page, '.blk[data-block="spend"]'), /\$3\.35 of \$10\.00/);
    await context.close();
  });

  await flow("Crossfeed Chat tab shows run counts, not invented money", async () => {
    resetPrefs();
    const { page, context } = await open();
    await tab(page, "chatgpt-work");
    assert.match(await text(page, '.blk[data-block="activity"]'), /requests today/);
    assert.match(await text(page, '.blk[data-block="models"]'), /41 runs/);
    await context.close();
  });

  await flow("click targets: tokens to usage, a limit row to its section, Status to the status page, an Overview card to its tab", async () => {
    resetPrefs();
    const { page, context } = await open();
    await page.route("**/usage**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<title>usage</title>" }));
    await page.route("**/#pool-**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<title>console</title>" }));
    await tab(page, "claude");
    await page.click('.blk[data-block="tokens"] .tokline .tok');
    await page.waitForURL(/\/usage#claude$/);
    await page.goBack(); await page.waitForFunction(() => window.__ready === true);
    await tab(page, "claude");
    await page.click('.blk[data-block="limits"] .lrow');
    await page.waitForURL(/#pool-claude$/);
    await page.goBack(); await page.waitForFunction(() => window.__ready === true);
    await tab(page, "claude");
    await page.context().route("https://status.claude.com/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: "<title>status</title>" }));
    const [popup] = await Promise.all([page.waitForEvent("popup"), page.click('.blk[data-block="status"] .statline')]);
    assert.match(popup.url(), /^https:\/\/status\.claude\.com\//);
    await popup.close();
    await tab(page, "overview");
    assert.match(await text(page, '.blk[data-block="status"]'), /Claude/);
    await page.click('.ocard[data-goto="codex"]');
    assert.equal(await page.getAttribute(".panel", "data-tab"), "codex");
    await page.click('.tab[data-tab-id="overview"]');
    await page.click('.blk[data-block="tokens"] .tokline .tok');
    await page.waitForURL(/\/usage$/);
    await context.close();
  });

  await flow("the graph is editable: right-click offers metric and range, and the choice persists", async () => {
    resetPrefs();
    const { page, context } = await open();
    await tab(page, "claude");
    const before = await text(page, '.blk[data-block="tokens"]');
    assert.match(before, /tokens today/);
    assert.equal(await page.locator('.blk[data-block="tokens"] .spark rect').count(), 14);
    await page.click('.blk[data-block="tokens"] .tokline', { button: "right" });
    assert.ok(await page.locator(".sparkmenu").count());
    await page.click('.sparkmenu button:has-text("Cost")');
    assert.match(await text(page, '.blk[data-block="tokens"]'), /\$\d+\.\d\d\s+today/);
    await page.click('.blk[data-block="tokens"] .tmore');
    await page.click('.sparkmenu button:has-text("30 days")');
    assert.equal(await page.locator('.blk[data-block="tokens"] .spark rect').count(), 30);
    await page.waitForTimeout(400);
    assert.deepEqual(prefsOnDisk().tile_options["claude/tokens"], { metric: "cost", days: 30 });
    await page.reload(); await page.waitForFunction(() => window.__ready === true);
    await tab(page, "claude");
    assert.equal(await page.locator('.blk[data-block="tokens"] .spark rect').count(), 30);
    // Settings carries the same two choices
    await page.click('[data-act="settings"]'); await page.click('[data-page="tiles"]');
    await page.click('[data-settings-tab="claude"]');
    await page.click('[data-set="so:claude/tokens:metric"][data-value="tokens"]');
    await page.click('[data-set="so:claude/tokens:days"][data-value="7"]');
    await page.click('[data-sheet="close"]');
    assert.equal(await page.locator('.blk[data-block="tokens"] .spark rect').count(), 7);
    assert.match(await text(page, '.blk[data-block="tokens"]'), /tokens today/);
    await context.close();
  });

  await flow("Status: a provider tile hides in Settings; Overview shows it by itself during an incident", async () => {
    resetPrefs();
    const { page, context } = await open();
    await page.click('[data-act="settings"]'); await page.click('[data-page="tiles"]');
    await page.click('[data-settings-tab="claude"]');
    await page.click('[data-toggle="status"]');
    await page.click('[data-sheet="close"]');
    await tab(page, "claude");
    assert.equal(await page.locator('.blk[data-block="status"]').count(), 0, "turned off in Settings");
    await tab(page, "codex");
    assert.match(await text(page, '.blk[data-block="status"]'), /No incident reported/);
    await tab(page, "overview");
    assert.ok(await page.locator('.blk[data-block="status"]').count(), "an incident on Claude shows on Overview");
    await context.close();
  });

  await flow("Settings: tabs on and off, tile order and on/off save as touched", async () => {
    resetPrefs();
    const { page, context } = await open();
    await page.click('[data-act="settings"]'); await page.click('[data-page="tiles"]');
    await page.click('[data-tab-toggle="copilot"]');
    await page.waitForTimeout(300);
    assert.deepEqual(prefsOnDisk().hidden_tabs, ["copilot"]);
    assert.equal(await page.locator('.tab[data-tab-id="copilot"]').count(), 0);
    await page.click('[data-tab-toggle="copilot"]');
    await page.click('[data-settings-tab="claude"]');
    await page.click('[data-move="down"][data-id="limits"]');
    await page.waitForTimeout(300);
    assert.equal(prefsOnDisk().tiles.claude[0], "resets");
    await page.click('[data-page="general"]');
    await page.click('[data-set="show"][data-value="left"]');
    await page.waitForTimeout(300);
    assert.equal(prefsOnDisk().show, "left");
    assert.match(await text(page, ".panel"), /% left/);
    await context.close();
  });

  await flow("Spend policy: the tile's menu posts the level the console writes; Reset order returns a tile order to its default", async () => {
    resetPrefs();
    const { page, context } = await open();
    await tab(page, "claude");
    const posted = [];
    await page.route("**/level", (route) => { posted.push(route.request().postData()); route.fulfill({ status: 200, contentType: "application/json", body: "{}" }); });
    await page.click('.blk[data-block="policy"] [data-act="policy"]');
    assert.equal(await page.locator(".policymenu button").count(), 5);
    await page.click('.policymenu button:has-text("High")');
    await page.waitForTimeout(400);
    assert.equal(posted.length, 1);
    const body = new URLSearchParams(posted[0]);
    assert.deepEqual([body.get("pool"), body.get("level")], ["claude", "high"]);
    // tile order: move one in Settings, then the tile menu's Reset order brings the default back
    await page.click('[data-act="settings"]'); await page.click('[data-page="tiles"]'); await page.click('[data-settings-tab="claude"]');
    await page.click('[data-move="down"][data-id="limits"]');
    await page.click('[data-sheet="close"]');
    await page.waitForTimeout(300);
    assert.notEqual((await page.$$eval(".blk", (els) => els.map((e) => e.dataset.block)))[0], "limits");
    await page.click('.blk[data-block="limits"] .sech, .blk[data-block="limits"] .lrow', { button: "right" });
    await page.click('.cmenu button:has-text("Reset order")');
    await page.waitForTimeout(500);
    assert.equal((await page.$$eval(".blk", (els) => els.map((e) => e.dataset.block)))[0], "limits");
    assert.equal(prefsOnDisk().tiles.claude[0], "limits");
    await context.close();
  });

  await flow("the console's Settings page: Theme changes the page and survives a reload; Appearance in the panel moves it too", async () => {
    resetPrefs();
    const { page, context } = await open();
    await page.click('[data-act="settings"]'); await page.click('[data-set="appearance"][data-value="dark"]');
    await page.goto(base + "/settings");
    assert.equal(await page.evaluate(() => document.documentElement.dataset.themeChoice), "dark", "the panel's choice reaches the console pages");
    await page.selectOption("select[data-theme-choice]", "light");
    assert.equal(await page.evaluate(() => document.documentElement.dataset.themeChoice), "light");
    await page.reload();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.themeChoice), "light");
    await context.close();
  });

  // ---- the renders the brief asks for: 310 px, light and dark ----
  for (const scheme of ["light", "dark"]) {
    await flow(`renders ${scheme}`, async () => {
      resetPrefs();
      const { page, context } = await open({ scheme });
      await page.waitForTimeout(500);
      await shot(page, "overview", scheme);
      await tab(page, "openrouter-free"); await shot(page, "openrouter-tab", scheme);
      await tab(page, "gemini-metered"); await shot(page, "gemini-api-tab", scheme);
      await tab(page, "overview");
      await page.click('[data-act="settings"]'); await shot(page, "settings-general", scheme);
      await page.click('[data-page="menubar"]'); await shot(page, "settings-menu-bar", scheme);
      await context.close();
    });
  }

  await browser.close();
  child.stdin.end();
  child.kill();
  console.log(`\n${passed} flows passed, ${failures.length} failed`);
  process.exit(failures.length ? 1 : 0);
})().catch((error) => { console.error(error); process.exit(1); });
