// The web panel: the same Tiles menu the Mac app draws, served by the console at /panel so a Linux
// desktop gets it too. It reads the live glance document, writes only the preferences file, and
// asks the console to read the sources again or to use a Claude reset when the operator says so.
// No external fonts, no network beyond the console that served it.
(function () {
  const TOKEN = document.body.dataset.formToken || "";
  const host = document.getElementById("host");
  const sheetHost = document.getElementById("sheet");
  const live = document.getElementById("pagelive");
  const dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)");
  const STALE_MS = 60 * 1000;

  const S = {
    data: window.GLANCE || { pools: [] }, prefs: window.PREFS || {}, tab: "overview", mode: "used",
    tabOrder: [], confirm: null, used: null, confirmError: null, settings: null, theme: "light",
    saveNote: null, saveError: false, order: null, orderTab: null, refreshing: false, refreshedAt: null, menu: null,
  };
  const prefs = () => S.prefs;
  const defaultTiles = (tab) => (window.defaultTiles ? window.defaultTiles(tab) : ["limits", "links"]);
  const tilesFor = (tab) => window.tileList(tab);
  const offFor = (tab) => new Set(((prefs().hidden_tiles || {})[tab]) || []);
  const tabIds = () => {
    const hidden = new Set(prefs().hidden_tabs || []);
    return (window.VM.tabIds || []).filter((id) => id !== "other" && (id === "overview" || !hidden.has(id)));
  };
  const tabsWithOther = () => [...tabIds(), "other"];

  // ---- appearance: System follows the browser; Light and Dark override it -------------------------
  function themeOf() {
    const choice = prefs().appearance;
    if (choice === "light" || choice === "dark") return choice;
    return dark && dark.matches ? "dark" : "light";
  }
  function applyTheme() {
    S.theme = themeOf();
    document.body.classList.remove("light", "dark");
    document.body.classList.add(S.theme);
    document.body.dataset.appearance = prefs().appearance || "system";
  }

  // ---- the panel ---------------------------------------------------------------------------
  let api = null;
  function render() {
    applyTheme();
    const order = window.blocksFor(S.tab, "B");
    host.innerHTML = window.renderPanel("B", S.tab, S.theme, {
      order: S.order && S.orderTab === S.tab ? S.order : order,
      tabOrder: S.tabOrder.length ? S.tabOrder : window.VM.order,
      confirm: S.confirm, used: S.used, confirmError: S.confirmError, mode: S.mode,
    });
    const panel = host.querySelector(".panel");
    if (S.refreshing) panel.querySelectorAll('[data-act="refresh"]').forEach((b) => b.classList.add("spin"));
    api = window.attachPanel(panel, {
      log: () => {},
      onOrder: (what, ord) => {
        if (what === "tabs") {
          S.tabOrder = ord.slice();
          reorderTabs(ord);
        } else {
          // The drawn order omits a tile with nothing to say right now; the preferences keep every
          // listed tile, each where the drag left the ones around it.
          S.order = ord.slice();
          S.orderTab = S.tab;
          setTiles(S.tab, mergeListed(S.tab, ord));
          renderSheet();
        }
        save("Tiles reordered.");
      },
      onSelect: (id) => { if (id !== S.tab) selectTab(id); },
    });
    window.__api = api;
    bind(panel);
    renderSheet();
  }

  // A tab drag changes the provider order, here and in the Overview cards (one order, two handles).
  function reorderTabs(ord) {
    const known = new Set((prefs().tabs || []));
    const rest = (prefs().tabs || []).filter((id) => !known.has(id) || !ord.includes(id) && id !== "overview" && id !== "other");
    const next = ["overview", ...ord, ...rest.filter((id) => id !== "overview" && id !== "other" && !ord.includes(id)), "other"];
    prefs().tabs = next;
    S.tabOrder = ord.slice();
  }

  function selectTab(id) {
    S.tab = id;
    S.confirm = null; S.confirmError = null; closeMenu();
    location.hash = "#tab=" + encodeURIComponent(id);
    prefs().last_tab = id;
    change({ last_tab: id });
    render();
  }

  // A tile that is listed but not drawn (no reading yet) keeps its place next to its neighbours.
  function mergeListed(tab, drawn) {
    const listed = tilesFor(tab);
    const out = drawn.slice();
    listed.forEach((id, index) => {
      if (out.includes(id)) return;
      const previous = listed.slice(0, index).reverse().find((other) => out.includes(other));
      out.splice(previous ? out.indexOf(previous) + 1 : 0, 0, id);
    });
    return out;
  }

  // `explicit` is the operator adding a tile. Overview's Status shows itself during an incident without being listed,
  // and stays unlisted through a move: only adding it pins it.
  function setTiles(tab, list, explicit) {
    const saved = (prefs().tiles || {})[tab] || defaultTiles(tab);
    if (tab === "overview" && !explicit && !saved.includes("status")) list = list.filter((id) => id !== "status");
    const off = offFor(tab);
    prefs().tiles = { ...(prefs().tiles || {}), [tab]: list };
    prefs().hidden_tiles = { ...(prefs().hidden_tiles || {}), [tab]: [...off].filter((id) => list.includes(id)) };
  }

  function bind(panel) {
    panel.querySelectorAll("[data-act]").forEach((el) => el.addEventListener("click", async (event) => {
      const act = el.dataset.act, key = el.dataset.key;
      if (act === "refresh") { event.preventDefault(); await refreshAndReload(); return; }
      if (act === "settings") { event.preventDefault(); openSettings(); return; }
      if (act === "budget") { event.preventDefault(); openSettings("budgets"); return; }
      if (act === "sparkmenu") { event.preventDefault(); event.stopPropagation(); openSparkMenu(key, el); return; }
      if (act === "policy") { event.preventDefault(); event.stopPropagation(); openPolicyMenu(el); return; }
      if (act === "ask") { S.confirm = key; S.used = null; S.confirmError = null; }
      else if (act === "cancel") { S.confirm = null; S.confirmError = null; }
      else if (act === "use") { await useReset(key); return; }
      else return;
      render();
      const focus = panel.querySelector(act === "ask" ? '.confirm [data-act="use"]' : `[data-key="${key}"]`);
      if (focus) focus.focus();
    }));
    panel.querySelectorAll("[data-goto]").forEach((a) => a.addEventListener("click", (event) => {
      event.preventDefault();
      selectTab(a.dataset.goto);
    }));
  }

  // ---- everything clickable gives something: one handler follows a tile's address -----------------
  let longPressed = false;
  function follow(href) {
    if (!href) return;
    if (/^https?:/i.test(href)) window.open(href, "_blank", "noopener");
    else location.href = href;
  }
  host.addEventListener("click", (event) => {
    if (longPressed) { longPressed = false; event.preventDefault(); return; }
    if (event.target.closest("button, a, input, .cmenu")) return;
    const hit = event.target.closest("[data-href]");
    if (hit && host.contains(hit)) follow(hit.dataset.href);
  });
  host.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const hit = event.target.closest && event.target.closest("[data-href]");
    if (hit && event.target === hit) { event.preventDefault(); follow(hit.dataset.href); }
  });

  // ---- small menus: the graph's metric and range, the spend policy -----------------------------------
  // rows: { text, on, run } or "-". One menu at a time, closed by Escape, a click elsewhere or a choice.
  function openMenu(anchor, rows, name) {
    closeMenu();
    const box = host.querySelector(".panel");
    const menu = document.createElement("div");
    menu.className = "cmenu popmenu " + (name || "");
    menu.setAttribute("role", "menu");
    menu.innerHTML = rows.map((row) => row === "-" ? "<hr>" : `<button type="button" role="menuitemradio" aria-checked="${!!row.on}">${row.on ? "✓ " : ""}${esc(row.text)}</button>`).join("");
    box.appendChild(menu);
    const a = anchor.getBoundingClientRect(), b = box.getBoundingClientRect();
    menu.style.left = Math.max(6, Math.min(a.left - b.left - 40, b.width - menu.offsetWidth - 6)) + "px";
    menu.style.top = Math.max(6, a.bottom - b.top + 2) + "px";
    S.menu = name || "menu";
    const buttons = [...menu.querySelectorAll("button")];
    rows.filter((row) => row !== "-").forEach((row, i) => buttons[i].addEventListener("click", (event) => {
      event.stopPropagation();
      closeMenu();
      row.run();
    }));
    if (buttons[0]) buttons[0].focus();
    setTimeout(() => document.addEventListener("pointerdown", function off(e) {
      if (!menu.contains(e.target)) { closeMenu(); document.removeEventListener("pointerdown", off); }
    }), 0);
  }
  function closeMenu() {
    S.menu = null;
    document.querySelectorAll(".popmenu").forEach((el) => el.remove());
  }
  function openSparkMenu(key, anchor) {
    const [tab, tile] = key.split("/");
    const info = window.sparkInfo(tab, tile);
    if (!info) return;
    const label = { tokens: "Tokens", cost: "Cost", requests: "Requests" };
    openMenu(anchor, [
      ...info.metrics.map((m) => ({ text: label[m], on: m === info.metric, run: () => setTileOption(key, { metric: m }) })), "-",
      ...[7, 14, 30].map((d) => ({ text: `${d} days`, on: d === info.days, run: () => setTileOption(key, { days: d }) })),
    ], "sparkmenu");
  }
  // The spend policy is a real setting: choosing one posts it to the console, which writes it where the routing reads it.
  const LEVELS = [["off", "Off"], ["low", "Low"], ["normal", "Normal"], ["high", "High"], ["forced", "Max"]];
  function openPolicyMenu(anchor) {
    const subscription = anchor.dataset.subscription, current = anchor.dataset.level;
    openMenu(anchor, LEVELS.map(([id, text]) => ({ text, on: id === current, run: () => setLevel(subscription, id) })), "policymenu");
  }
  async function setLevel(subscription, level) {
    try {
      const response = await fetch("/level", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
        body: new URLSearchParams({ t: TOKEN, pool: subscription, level }).toString() });
      if (!response.ok) throw new Error("HTTP " + response.status);
      if (live) live.textContent = "Spend policy set.";
    } catch (error) {
      if (live) live.textContent = "Spend policy not changed: " + String(error);
    }
    await reload();
  }
  // Capture phase: the graph's own menu replaces the tile menu the drag layer would open on the same right-click.
  host.addEventListener("contextmenu", (event) => {
    const spark = event.target.closest("[data-spark]");
    if (!spark) return;
    event.preventDefault();
    event.stopPropagation();
    openSparkMenu(spark.dataset.spark, spark);
  }, true);
  let pressTimer = null;
  host.addEventListener("pointerdown", (event) => {
    const spark = event.target.closest("[data-spark]");
    if (!spark || event.pointerType === "mouse") return;
    pressTimer = setTimeout(() => { longPressed = true; openSparkMenu(spark.dataset.spark, spark); }, 550);
  });
  ["pointerup", "pointercancel", "pointermove"].forEach((name) => host.addEventListener(name, () => { clearTimeout(pressTimer); }));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && S.menu) closeMenu(); });

  function setTileOption(key, patch) {
    const options = { ...(prefs().tile_options || {}) };
    options[key] = { ...(options[key] || {}), ...patch };
    prefs().tile_options = options;
    window.VM.prefs = prefs();
    render();
    save("Graph changed.", true);
  }

  // ---- one attempt at a Claude reset -----------------------------------------------------------
  async function useReset(key) {
    S.confirm = null; S.confirmError = null;
    const body = new URLSearchParams({ t: TOKEN, pool: "claude", key: key || "" });
    let text = "";
    try {
      const response = await fetch("/use-reset", {
        method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: body.toString(),
      });
      text = (await response.text()).trim();
      if (response.ok) S.used = key; else S.confirmError = text || "The reset was not used.";
    } catch (error) {
      S.confirmError = "The reset was not used: " + String(error);
    }
    if (live) live.textContent = text || S.confirmError || "";
    render();
    if (text && live) live.textContent = text;
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await reload();
  }

  // ---- settings: General, Menu bar, Tiles, Budgets. Every control saves as it is touched. -----------
  const PAGES = [["general", "General"], ["menubar", "Menu bar"], ["tiles", "Tiles"], ["budgets", "Budgets"]];
  function openSettings(page) {
    const tab = S.tab === "other" ? "overview" : S.tab;
    S.settings = { tab, page: page || (S.settings && S.settings.page) || "general", note: null, error: false };
    location.hash = "#settings";
    renderSheet(true);
    const first = sheetHost.querySelector("button");
    if (first) first.focus();
  }

  function settingTabs() {
    return ["overview", ...tabIds().filter((id) => id !== "overview"), ...(window.VM.other.length ? ["other"] : [])];
  }

  const seg = (name, value, choices, label) => `<div class="seg2" role="group" aria-label="${escAttr(label)}">${choices.map(([id, text]) =>
    `<button type="button" data-set="${name}" data-value="${escAttr(id)}" aria-pressed="${String(value) === String(id)}">${esc(text)}</button>`).join("")}</div>`;
  const field = (label, body, hint) => `<div class="field"><div class="flabel">${esc(label)}</div>${body}${hint ? `<div class="fhint">${esc(hint)}</div>` : ""}</div>`;

  // Off, 1, 5 and 15 minutes; a value another writer saved (up to a day) is shown too, so the row never lies.
  const everyChoices = (current) => [[0, "Off"], [1, "1 min"], [5, "5 min"], [15, "15 min"]]
    .concat([0, 1, 5, 15].includes(current) || current == null ? [] : [[current, current >= 60 && current % 60 === 0 ? `${current / 60} h` : `${current} min`]]);
  function generalPage() {
    const p = prefs(), r = p.refresh || {}, open = p.open_on || "last";
    const opens = [["last", "Last used"], ["overview", "Overview"], ...tabIds().filter((id) => id !== "overview").map((id) => [id, window.VM.tabTitle(id)])];
    return field("Appearance", seg("appearance", p.appearance || "system", [["system", "System"], ["light", "Light"], ["dark", "Dark"]], "Appearance"))
      + field("Show", seg("show", S.mode, [["used", "Used"], ["left", "Left"]], "Show used or left"))
      + field("Refresh", `<div class="trow"><span class="tn">When opened<span class="q"> · if older than 1 min</span></span><button type="button" data-toggle-refresh="on_open" class="${r.on_open === false ? "" : "on"}" aria-pressed="${r.on_open !== false}">${r.on_open === false ? "Off" : "On"}</button></div>
        <div class="trow"><span class="tn">Every</span>${seg("every", r.every_minutes == null ? 5 : r.every_minutes, everyChoices(r.every_minutes), "Refresh every")}</div>`)
      + field("Open on", `<select data-set-select="open_on" aria-label="Open on">${opens.map(([id, text]) => `<option value="${escAttr(id)}"${open === id ? " selected" : ""}>${esc(text)}</option>`).join("")}</select>`,
        "Last used opens where you left off.");
  }

  function menubarPage() {
    const mb = prefs().menubar || {};
    const title = window.VM.barTitle(mb);
    const icon = title.icon === "provider" && title.logo && window.LOGOS[title.logo]
      ? `<span class="logo" style="width:16px;height:16px">${window.LOGOS[title.logo]}</span>`
      : title.icon === "crossfeed" ? `<span class="mark" aria-hidden="true"><i></i><i></i><i></i></span>` : "";
    const shows = [["pressed", "Most pressed"], ...tabIds().filter((id) => id !== "overview" && window.VM.tabs[id] && window.VM.tabs[id].sections.some((s) => s.rows.length))
      .map((id) => [id, window.VM.tabTitle(id)])];
    return `<div class="bpreview" role="img" aria-label="Menu bar preview: ${escAttr(title.text || "icon only")}"><span class="bi">${icon}</span><span class="bt num">${esc(title.text)}</span></div>
      <div class="fhint pv">The tray draws this. Some desktops show only the icon.</div>`
      + field("Icon", seg("mb.icon", mb.icon || "provider", [["crossfeed", "Crossfeed"], ["provider", "Provider logo"], ["none", "None"]], "Menu bar icon"))
      + field("Text", seg("mb.text", mb.text || "window", [["full", "Full"], ["window", "Window"], ["number", "Number"], ["none", "None"]], "Menu bar text"))
      + field("Provider", `<select data-set-select="mb.show" aria-label="Provider">${shows.map(([id, text]) => `<option value="${escAttr(id)}"${(mb.show || "pressed") === id ? " selected" : ""}>${esc(text)}</option>`).join("")}</select>`)
      + field("Window", seg("mb.window", mb.window || "tightest", [["tightest", "Tightest"], ["5h", "5h"], ["Week", "Week"], ["Month", "Month"]], "Window"))
      + field("Count", seg("mb.count", mb.count || "used", [["used", "Used"], ["left", "Left"]], "Count used or left"))
      + field("Extra", seg("mb.extra", mb.extra || "none", [["none", "None"], ["reset", "Reset"], ["pace", "Pace"], ["cost", "Cost"]], "Extra after the number"))
      + (mb.extra === "reset" ? field("Reset shows", seg("mb.reset", mb.reset || "countdown", [["countdown", "Countdown"], ["clock", "Clock"]], "Reset as a countdown or a clock time")) : "");
  }

  function tilesPage() {
    const tab = S.settings.tab;
    const hidden = new Set(prefs().hidden_tabs || []);
    const listed = tilesFor(tab);
    const off = offFor(tab);
    const rows = listed.map((id, index) => {
      const on = !off.has(id);
      const noData = window.tileAvailable && !window.tileAvailable(tab, id) ? ` <span class="q">· no reading</span>` : "";
      const info = (id === "tokens" || id === "activity") && window.sparkInfo ? window.sparkInfo(tab, id) : null;
      const label = { tokens: "Tokens", cost: "Cost", requests: "Requests" };
      const opts = info ? `<div class="topts">${seg(`so:${tab}/${id}:metric`, info.metric, info.metrics.map((m) => [m, label[m]]), "Graph metric")}${seg(`so:${tab}/${id}:days`, info.days, [[7, "7d"], [14, "14d"], [30, "30d"]], "Graph range")}</div>` : "";
      return `<div class="trow" data-tile="${escAttr(id)}"><span class="tn">${esc(window.tileLabel ? window.tileLabel(id) : id)}${id.startsWith("plugin:") ? ` <span class="q">plugin</span>` : ""}${noData}</span>
        <button type="button" data-move="up" data-id="${escAttr(id)}" ${index === 0 ? "disabled" : ""} aria-label="Move ${escAttr(i18nName(id))} up">↑</button>
        <button type="button" data-move="down" data-id="${escAttr(id)}" ${index === listed.length - 1 ? "disabled" : ""} aria-label="Move ${escAttr(i18nName(id))} down">↓</button>
        <button type="button" data-toggle="${escAttr(id)}" class="${on ? "on" : ""}" aria-pressed="${on}">${on ? "On" : "Off"}</button></div>${opts}`;
    }).join("");
    const addable = candidateTiles(tab).filter((id) => !listed.includes(id));
    const add = addable.length ? `<div class="add">${addable.map((id) => `<button type="button" data-add="${escAttr(id)}">Add ${esc(window.tileLabel ? window.tileLabel(id) : id)}</button>`).join("")}</div>` : "";
    const picker = settingTabs().map((id) => `<button type="button" data-settings-tab="${escAttr(id)}" aria-pressed="${id === tab}">${esc(window.VM.tabTitle ? window.VM.tabTitle(id) : id)}</button>`).join("");
    return field("Tabs", `<div class="trow"><span class="tn">Overview<span class="q"> · always shown</span></span><button type="button" class="on" aria-pressed="true" disabled>On</button></div>
        ${tabIds().filter((id) => id !== "overview").map((id) => `<div class="trow"><span class="tn">${esc(window.VM.tabTitle(id))}</span><button type="button" data-tab-toggle="${escAttr(id)}" class="${hidden.has(id) ? "" : "on"}" aria-pressed="${!hidden.has(id)}">${hidden.has(id) ? "Off" : "On"}</button></div>`).join("")}
        ${[...hidden].filter((id) => id !== "other").map((id) => `<div class="trow"><span class="tn">${esc(window.VM.tabTitle(id))}</span><button type="button" data-tab-toggle="${escAttr(id)}" aria-pressed="false">Off</button></div>`).join("")}
        <div class="trow"><span class="tn">Other<span class="q"> · always shown</span></span><button type="button" class="on" aria-pressed="true" disabled>On</button></div>`)
      + field("Tiles on this tab", `<div class="tabs2" role="group" aria-label="Tab to edit">${picker}</div>${rows}${add}`);
  }

  function budgetsPage() {
    const apiTabs = tabIds().filter((id) => window.VM.tabs[id] && window.VM.tabs[id].isApi && !window.VM.tabs[id].api.relay);
    if (!apiTabs.length) return `<div class="fhint">No API provider is listed yet.</div>`;
    const budgets = prefs().budgets || {};
    return `<div class="fhint">A monthly figure in US dollars, shown against what you spend. Nothing here stops a call.</div>`
      + apiTabs.map((id) => {
        const t = window.VM.tabs[id], now = t.api && t.api.spend ? t.api.spend.month : "";
        return `<div class="trow"><label class="tn" for="budget-${escAttr(id)}">${esc(t.fullTitle || t.title)}${now ? ` <span class="q">· ${esc(now)} this month</span>` : ""}</label>
          <span class="money"><span class="q">$</span><input id="budget-${escAttr(id)}" type="number" min="0" step="1" inputmode="decimal" data-budget="${escAttr(id)}" value="${budgets[id] == null ? "" : escAttr(budgets[id])}" placeholder="None" aria-label="${escAttr(t.fullTitle || t.title)} monthly budget in US dollars"></span></div>`;
      }).join("");
  }

  function renderSheet(force) {
    if (!S.settings && !force) { sheetHost.innerHTML = ""; return; }
    if (!S.settings) { sheetHost.innerHTML = ""; return; }
    const page = S.settings.page;
    const body = page === "menubar" ? menubarPage() : page === "tiles" ? tilesPage() : page === "budgets" ? budgetsPage() : generalPage();
    const chips = PAGES.map(([id, text]) => `<button type="button" data-page="${id}" aria-pressed="${id === page}">${text}</button>`).join("");
    sheetHost.innerHTML = `<section class="sheet ${S.theme}" id="settings" aria-label="Settings" data-sheetpage="${page}">
      <h2>Settings</h2>
      <div class="tabs2 pages" role="group" aria-label="Settings page">${chips}</div>
      ${body}
      <div class="acts"><span class="note ${S.settings.error ? "error" : ""}" role="status">${esc(S.settings.note || "Changes save as you make them.")}</span><button type="button" class="primary" data-sheet="close">Done</button></div>
    </section>`;
    bindSheet();
  }

  function bindSheet() {
    const on = (selector, handler) => sheetHost.querySelectorAll(selector).forEach((el) => el.addEventListener("click", () => handler(el)));
    on("[data-page]", (el) => { S.settings.page = el.dataset.page; renderSheet(true); focusLike(`[data-page="${el.dataset.page}"]`); });
    on("[data-settings-tab]", (el) => { S.settings.tab = el.dataset.settingsTab; renderSheet(true); focusLike(`[data-settings-tab="${el.dataset.settingsTab}"]`); });
    on("[data-set]", (el) => { applySetting(el.dataset.set, el.dataset.value); focusLike(`[data-set="${el.dataset.set}"][data-value="${el.dataset.value}"]`); });
    on("[data-toggle-refresh]", () => {
      const r = { on_open: true, every_minutes: 5, ...(prefs().refresh || {}) };
      r.on_open = !r.on_open;
      prefs().refresh = r;
      persist("Saved.", '[data-toggle-refresh]');
    });
    on("[data-tab-toggle]", (el) => {
      const id = el.dataset.tabToggle, hiddenTabs = new Set(prefs().hidden_tabs || []);
      if (hiddenTabs.has(id)) hiddenTabs.delete(id); else hiddenTabs.add(id);
      prefs().hidden_tabs = [...hiddenTabs];
      rebuild();
      if (S.tab === id && hiddenTabs.has(id)) S.tab = "overview";
      persist("Saved.", `[data-tab-toggle="${id}"]`);
    });
    on("[data-toggle]", (el) => {
      const id = el.dataset.toggle, set = offFor(S.settings.tab);
      if (set.has(id)) set.delete(id); else set.add(id);
      prefs().hidden_tiles = { ...(prefs().hidden_tiles || {}), [S.settings.tab]: [...set] };
      persist("Saved.", `[data-toggle="${id}"]`);
    });
    on("[data-move]", (el) => {
      const id = el.dataset.id, list = tilesFor(S.settings.tab).slice();
      const at = list.indexOf(id), to = at + (el.dataset.move === "up" ? -1 : 1);
      if (at < 0 || to < 0 || to >= list.length) return;
      list.splice(at, 1); list.splice(to, 0, id);
      setTiles(S.settings.tab, list);
      persist("Saved.", `[data-move="${el.dataset.move}"][data-id="${id}"]`);
    });
    on("[data-add]", (el) => {
      const id = el.dataset.add, list = tilesFor(S.settings.tab);
      if (!list.includes(id)) setTiles(S.settings.tab, [...list, id], true);
      persist("Saved.");
    });
    sheetHost.querySelectorAll("[data-set-select]").forEach((el) => el.addEventListener("change", () => {
      applySetting(el.dataset.setSelect, el.value);
      focusLike(`[data-set-select="${el.dataset.setSelect}"]`);
    }));
    sheetHost.querySelectorAll("[data-budget]").forEach((el) => el.addEventListener("change", () => {
      const value = Number(el.value), budgets = { ...(prefs().budgets || {}) };
      if (el.value !== "" && Number.isFinite(value) && value > 0) budgets[el.dataset.budget] = value; else delete budgets[el.dataset.budget];
      prefs().budgets = budgets;
      persist("Saved.", `[data-budget="${el.dataset.budget}"]`);
    }));
    const close = sheetHost.querySelector('[data-sheet="close"]');
    if (close) close.addEventListener("click", closeSettings);
  }

  function focusLike(selector) {
    const el = sheetHost.querySelector(selector);
    if (el) el.focus();
  }

  // One place turns a setting's name into a change of the document; the page behind it redraws at once.
  function applySetting(name, value) {
    const p = prefs();
    if (name === "appearance") {
      p.appearance = value;
      // The console's own pages keep their theme in this browser under the same three words: follow the choice.
      try { window.localStorage.setItem("crossfeed-theme", value); } catch (error) { /* a browser that keeps nothing */ }
    }
    else if (name === "show") { p.show = value; S.mode = value; window.setMode(value); }
    else if (name === "every") p.refresh = { on_open: true, ...(p.refresh || {}), every_minutes: Number(value) };
    else if (name === "open_on") p.open_on = value;
    else if (name.startsWith("mb.")) p.menubar = { ...(p.menubar || {}), [name.slice(3)]: value };
    else if (name.startsWith("so:")) {
      const [, key, field] = name.split(":");
      const options = { ...(p.tile_options || {}) };
      options[key] = { ...(options[key] || {}), [field]: field === "days" ? Number(value) : value };
      p.tile_options = options;
    }
    persist("Saved.");
  }

  // Redraw from the changed document, then write it; the sheet keeps the focus it had.
  function rebuild() {
    window.PREFS = S.prefs;
    window.buildModel(window.GLANCE, S.prefs);
    S.tabOrder = window.VM.order.slice();
  }
  function persist(note, focus) {
    rebuild();
    armTimer();
    S.order = null; S.orderTab = null;
    render();
    renderSheet(true);
    if (focus) focusLike(focus);
    save(note, true);
  }

  function i18nName(id) { return window.tileLabel ? window.tileLabel(id) : id; }

  function candidateTiles(tab) {
    const builtin = defaultTiles(tab).concat(tab === "overview" ? ["status"] : []);
    const plugins = Object.keys(window.VM.pluginTiles || {}).filter((id) => {
      const entry = window.VM.pluginTiles[id];
      return !entry.tab || entry.tab === tab || tab === "overview";
    });
    return [...builtin, ...plugins];
  }

  function closeSettings() {
    S.settings = null;
    instanceHash();
    renderSheet(true);
    host.querySelector(".panel button") ? host.querySelector(".panel button").focus() : null;
  }

  function instanceHash() {
    history.replaceState(null, "", location.pathname + "#tab=" + encodeURIComponent(S.tab));
  }

  // ---- saving and reading --------------------------------------------------------------------
  // `save` writes the whole document; `change` writes one field against the file as it is now (a tab switch).
  async function post(body) {
    const response = await fetch("/panel/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ t: TOKEN, ...body }) });
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json();
  }
  function change(patch) { post({ change: patch }).catch(() => {}); }
  async function save(note, quiet) {
    try {
      const written = await post({ prefs: prefs() });
      S.saveNote = note || "Saved.";
      S.saveError = false;
      if (S.settings) { S.settings.note = S.saveNote; S.settings.error = false; const n = sheetHost.querySelector(".acts .note"); if (n) { n.textContent = S.saveNote; n.classList.remove("error"); } }
      if (live) live.textContent = S.saveNote;
      if (!quiet) {
        S.prefs = written.prefs || prefs();
        window.PREFS = S.prefs;
        if (window.VM) window.VM.prefs = S.prefs;
        S.order = null; S.orderTab = null;
        render();
      }
      return true;
    } catch (error) {
      S.saveNote = "Not saved: " + String(error).replace(/^Error: /, "");
      S.saveError = true;
      if (S.settings) { S.settings.note = S.saveNote; S.settings.error = true; const n = sheetHost.querySelector(".acts .note"); if (n) { n.textContent = S.saveNote; n.classList.add("error"); } }
      if (live) live.textContent = S.saveNote;
      return false;
    }
  }

  function adopt(data) {
    window.buildModel(data, data.panel || S.prefs);
    S.prefs = window.PREFS;
    S.mode = S.prefs.show === "left" ? "left" : "used";
    window.setMode(S.mode);
    S.tabOrder = window.VM.order.slice();
    S.order = null; S.orderTab = null;
    S.refreshedAt = data.refreshed_at ? Date.parse(data.refreshed_at) : null;
    if (!tabsWithOther().includes(S.tab)) S.tab = "overview";
  }

  async function reload() {
    try {
      const response = await fetch("/panel.json", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("HTTP " + response.status);
      adopt(await response.json());
      render();
      if (live) live.textContent = "Read again.";
    } catch (error) {
      if (live) live.textContent = "Could not read the readings: " + String(error);
    }
  }

  // Show what is cached at once; read every source in the background; draw again when it lands. The refresh
  // icon turns while it runs, and nothing waits on it.
  async function refreshAndReload() {
    if (S.refreshing) return;
    S.refreshing = true;
    document.querySelectorAll('[data-act="refresh"]').forEach((b) => b.classList.add("spin"));
    try {
      await fetch("/panel/refresh", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ t: TOKEN }).toString() });
    } catch (error) { /* the cached reading stays on screen */ }
    S.refreshing = false;
    await reload();
  }
  const staleNow = () => !S.refreshedAt || Date.now() - S.refreshedAt > STALE_MS;
  function refreshOnOpen() {
    const r = prefs().refresh || {};
    if (r.on_open !== false && !document.hidden && staleNow()) refreshAndReload();
  }
  let timer = null;
  function armTimer() {
    if (timer) clearInterval(timer);
    const minutes = (prefs().refresh || {}).every_minutes;
    timer = minutes > 0 ? setInterval(() => { if (!document.hidden) refreshAndReload(); }, minutes * 60 * 1000) : null;
    S.every = minutes > 0 ? minutes : 0;
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshOnOpen(); });

  // The tab to show: the one the link names, else where the operator chose to open (the last one used, or a fixed one).
  function startTab() {
    const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
    const allowed = tabsWithOther();
    const named = hash.get("tab");
    if (named && allowed.includes(named)) return named;
    const open = prefs().open_on || "last";
    const wanted = open === "last" ? prefs().last_tab : open;
    return allowed.includes(wanted) ? wanted : "overview";
  }

  function readHash() {
    const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
    if (hash.get("settings") !== null) { openSettings(); return; }
    S.tab = startTab();
  }

  // Escape and the header buttons are one place, so a keyboard user never gets stuck.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && S.settings) { event.preventDefault(); closeSettings(); }
  });
  // One URL carries the open tab and the settings sheet, so the tray's link and a reload land in the same place.
  window.addEventListener("hashchange", () => {
    if (!window.__ready) return;
    const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
    if (hash.get("settings") !== null) {
      if (!S.settings) openSettings();
      return;
    }
    if (S.settings) { S.settings = null; renderSheet(true); }
    const tab = hash.get("tab");
    if (tab && tabsWithOther().includes(tab) && tab !== S.tab) {
      S.tab = tab; S.confirm = null; S.confirmError = null;
      render();
    }
  });
  if (dark && dark.addEventListener) dark.addEventListener("change", () => { if ((prefs().appearance || "system") === "system") { render(); } });

  // ---- start ---------------------------------------------------------------------------------
  async function boot() {
    try {
      const response = await fetch("/panel.json", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("HTTP " + response.status);
      adopt(await response.json());
      readHash();
      armTimer();
      render();
      if (live) live.textContent = "Ready.";
      window.__panel = api2();
      window.__ready = true;
      refreshOnOpen();
    } catch (error) {
      if (live) live.textContent = "Could not read the panel: " + String(error);
    }
  }
  const api2 = () => ({ S, render, reload, refreshAndReload, selectTab, openSettings, save, api: () => api });
  window.__panel = api2();
  window.__drag = (blockId, dx, dy) => {
    if (!api) return null;
    api.press(blockId);
    return api.dragBy(dx, dy);
  };
  window.__dragEnd = () => { if (api) api.release(); };
  boot();

  function esc(text) { return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
  function escAttr(text) { return esc(text); }
})();
