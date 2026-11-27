// The web panel: the same Tiles menu the Mac app draws, served by the console at /panel so a Linux
// desktop gets it too. It reads the live glance document, writes only the preferences file, and
// asks the console to use a Claude reset when the operator says so. No external fonts, no network.
(function () {
  const TOKEN = document.body.dataset.formToken || "";
  const host = document.getElementById("host");
  const sheetHost = document.getElementById("sheet");
  const live = document.getElementById("pagelive");
  const dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)");
  const themeOf = () => (dark && dark.matches ? "dark" : "light");
  const BUILTIN_OVERVIEW = ["alert", "providers", "subscriptions", "tokens", "links"];
  const BUILTIN_PROVIDER = ["limits", "resets", "credits", "tokens", "policy", "links"];

  const S = {
    data: window.GLANCE || { pools: [] }, prefs: window.PREFS || {}, tab: "overview", mode: "used",
    tabOrder: [], confirm: null, used: null, confirmError: null, settings: null, theme: themeOf(),
    saveNote: null, saveError: false, order: null, orderTab: null,
  };
  const prefs = () => S.prefs;
  const tilesFor = (tab) => ((prefs().tiles || {})[tab]) || (tab === "overview" ? BUILTIN_OVERVIEW.slice() : tab === "other" ? ["other"] : BUILTIN_PROVIDER.slice());
  const offFor = (tab) => new Set(((prefs().hidden_tiles || {})[tab]) || []);
  const tabIds = () => {
    const listed = (prefs().tabs || []).filter((id) => id !== "other");
    const hidden = new Set(prefs().hidden_tabs || []);
    return listed.filter((id) => id === "overview" || (!hidden.has(id) && window.VM.tabs[id]));
  };
  const tabsWithOther = () => [...tabIds(), "other"];

  // ---- the panel ---------------------------------------------------------------------------
  let api = null;
  function render() {
    const order = window.blocksFor(S.tab, "B");
    host.innerHTML = window.renderPanel("B", S.tab, S.theme, {
      order: S.order && S.orderTab === S.tab ? S.order : order,
      tabOrder: S.tabOrder.length ? S.tabOrder : window.VM.order,
      confirm: S.confirm, used: S.used, confirmError: S.confirmError, mode: S.mode,
    });
    const panel = host.querySelector(".panel");
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
    S.confirm = null; S.confirmError = null;
    location.hash = "#tab=" + encodeURIComponent(id);
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

  function setTiles(tab, list) {
    const off = offFor(tab);
    prefs().tiles = { ...(prefs().tiles || {}), [tab]: list };
    prefs().hidden_tiles = { ...(prefs().hidden_tiles || {}), [tab]: [...off].filter((id) => list.includes(id)) };
  }

  function bind(panel) {
    panel.querySelectorAll("[data-act]").forEach((el) => el.addEventListener("click", async (event) => {
      const act = el.dataset.act, key = el.dataset.key;
      if (act === "refresh") { event.preventDefault(); await reload(); return; }
      if (act === "settings") { event.preventDefault(); openSettings(); return; }
      if (act === "policy") { event.preventDefault(); location.href = "/settings"; return; }
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

  // ---- settings ------------------------------------------------------------------------------
  function openSettings() {
    const tab = S.tab === "other" ? "overview" : S.tab;
    S.settings = { tab, note: null, error: false };
    location.hash = "#settings";
    renderSheet(true);
    const first = sheetHost.querySelector("button");
    if (first) first.focus();
  }

  function settingTabs() {
    return ["overview", ...tabIds().filter((id) => id !== "overview"), ...(window.VM.other.length ? ["other"] : [])];
  }

  function renderSheet(force) {
    if (!S.settings && !force) { sheetHost.innerHTML = ""; return; }
    if (!S.settings) return;
    const tab = S.settings.tab;
    const hidden = new Set(prefs().hidden_tabs || []);
    const listed = tilesFor(tab);
    const off = offFor(tab);
    const rows = listed.map((id, index) => {
      const on = !off.has(id);
      const noData = window.tileAvailable && !window.tileAvailable(tab, id) ? ` <span class="q">· no reading</span>` : "";
      return `<div class="trow" data-tile="${escAttr(id)}"><span class="tn">${esc(window.tileLabel ? window.tileLabel(id) : id)}${id.startsWith("plugin:") ? ` <span class="q">plugin</span>` : ""}${noData}</span>
        <button type="button" data-move="up" data-id="${escAttr(id)}" ${index === 0 ? "disabled" : ""} aria-label="Move ${escAttr(i18nName(id))} up">↑</button>
        <button type="button" data-move="down" data-id="${escAttr(id)}" ${index === listed.length - 1 ? "disabled" : ""} aria-label="Move ${escAttr(i18nName(id))} down">↓</button>
        <button type="button" data-toggle="${escAttr(id)}" class="${on ? "on" : ""}" aria-pressed="${on}">${on ? "On" : "Off"}</button></div>`;
    }).join("");
    const addable = candidateTiles(tab).filter((id) => !listed.includes(id));
    const add = addable.length ? `<div class="add">${addable.map((id) => `<button type="button" data-add="${escAttr(id)}">Add ${esc(window.tileLabel ? window.tileLabel(id) : id)}</button>`).join("")}</div>` : `<div class="note">Every tile this tab knows is already listed.</div>`;
    const picker = settingTabs().map((id) => `<button type="button" data-settings-tab="${escAttr(id)}" aria-pressed="${id === tab}">${esc(window.VM.tabTitle ? window.VM.tabTitle(id) : id)}</button>`).join("");
    sheetHost.innerHTML = `<section class="sheet" id="settings" aria-label="Settings">
      <h2>Settings</h2>
      <p class="hint">Show, order and choose the tiles on each tab. Nothing here changes what agents spend.</p>
      <div class="field"><div class="flabel">Show</div><div class="seg2" role="group" aria-label="Show used or left">
        <button type="button" data-show="used" aria-pressed="${S.mode === "used"}">Used</button>
        <button type="button" data-show="left" aria-pressed="${S.mode === "left"}">Left</button></div></div>
      <div class="field"><div class="flabel">Tabs</div><div class="trow"><span class="tn">Overview<span class="q"> · always shown</span></span><button type="button" class="on" aria-pressed="true" disabled>On</button></div>
        ${tabIds().filter((id) => id !== "overview").map((id) => `<div class="trow"><span class="tn">${esc(window.VM.tabTitle(id))}</span><button type="button" data-tab-toggle="${escAttr(id)}" class="${hidden.has(id) ? "" : "on"}" aria-pressed="${!hidden.has(id)}">${hidden.has(id) ? "Off" : "On"}</button></div>`).join("")}
        <div class="trow"><span class="tn">Other<span class="q"> · always shown</span></span><button type="button" class="on" aria-pressed="true" disabled>On</button></div></div>
      <div class="field"><div class="flabel">Tiles on this tab</div><div class="tabs2" role="group" aria-label="Tab to edit">${picker}</div>${rows}${add}</div>
      <div class="acts"><button type="button" class="ghost" data-sheet="close">Cancel</button><button type="button" class="primary" data-sheet="save">Save</button></div>
      ${S.settings.note ? `<div class="note ${S.settings.error ? "error" : ""}">${esc(S.settings.note)}</div>` : ""}
    </section>`;
    sheetHost.querySelectorAll("[data-settings-tab]").forEach((button) => button.addEventListener("click", () => {
      S.settings.tab = button.dataset.settingsTab;
      renderSheet(true);
    }));
    sheetHost.querySelectorAll("[data-show]").forEach((button) => button.addEventListener("click", () => {
      S.mode = button.dataset.show;
      prefs().show = S.mode;
      window.setMode(S.mode);
      renderSheet(true); render();
    }));
    sheetHost.querySelectorAll("[data-tab-toggle]").forEach((button) => button.addEventListener("click", () => {
      const id = button.dataset.tabToggle;
      const hiddenTabs = new Set(prefs().hidden_tabs || []);
      if (hiddenTabs.has(id)) hiddenTabs.delete(id); else hiddenTabs.add(id);
      prefs().hidden_tabs = [...hiddenTabs];
      renderSheet(true);
    }));
    sheetHost.querySelectorAll("[data-toggle]").forEach((button) => button.addEventListener("click", () => {
      const id = button.dataset.toggle;
      const set = offFor(S.settings.tab);
      if (set.has(id)) set.delete(id); else set.add(id);
      prefs().hidden_tiles = { ...(prefs().hidden_tiles || {}), [S.settings.tab]: [...set] };
      renderSheet(true);
    }));
    sheetHost.querySelectorAll("[data-move]").forEach((button) => button.addEventListener("click", () => {
      const id = button.dataset.id, list = tilesFor(S.settings.tab);
      const at = list.indexOf(id), to = at + (button.dataset.move === "up" ? -1 : 1);
      if (at < 0 || to < 0 || to >= list.length) return;
      const next = list.slice(); next.splice(at, 1); next.splice(to, 0, id);
      setTiles(S.settings.tab, next);
      renderSheet(true);
      const again = sheetHost.querySelector(`[data-move="${button.dataset.move}"][data-id="${cssEscape(id)}"]`);
      if (again) again.focus();
    }));
    sheetHost.querySelectorAll("[data-add]").forEach((button) => button.addEventListener("click", () => {
      const id = button.dataset.add, list = tilesFor(S.settings.tab);
      if (!list.includes(id)) setTiles(S.settings.tab, [...list, id]);
      renderSheet(true);
    }));
    sheetHost.querySelector('[data-sheet="close"]').addEventListener("click", closeSettings);
    sheetHost.querySelector('[data-sheet="save"]').addEventListener("click", async () => {
      const written = await save("Saved.");
      S.settings.note = written ? "Saved. The Mac menu reads the same file." : (S.saveNote || "Not saved.");
      S.settings.error = !written;
      renderSheet(true);
    });
  }

  function i18nName(id) { return window.tileLabel ? window.tileLabel(id) : id; }

  function candidateTiles(tab) {
    const builtin = tab === "overview" ? BUILTIN_OVERVIEW : tab === "other" ? ["other"] : BUILTIN_PROVIDER;
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
  let saveQueue = Promise.resolve();
  async function save(note) {
    const body = JSON.stringify({ t: TOKEN, prefs: prefs() });
    try {
      const response = await fetch("/panel/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const written = await response.json();
      S.prefs = written.prefs || prefs();
      S.saveNote = note || "Saved.";
      S.saveError = false;
      // The saved document is now the truth about which tiles exist and in what order: the view
      // model reads its tiles from it, so hand the written document over before drawing.
      window.PREFS = S.prefs;
      if (window.VM) window.VM.prefs = S.prefs;
      S.order = null; S.orderTab = null;
      if (live) live.textContent = S.saveNote;
      render();
      return true;
    } catch (error) {
      S.saveNote = "Not saved: " + String(error).replace(/^Error: /, "");
      S.saveError = true;
      if (live) live.textContent = S.saveNote;
      return false;
    }
  }

  async function reload() {
    try {
      const response = await fetch("/panel.json", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      window.buildModel(data, data.panel || S.prefs);
      S.prefs = window.PREFS;
      S.mode = S.prefs.show === "left" ? "left" : "used";
      window.setMode(S.mode);
      S.tabOrder = window.VM.order.slice();
      S.order = null; S.orderTab = null;
      render();
      if (live) live.textContent = "Read again.";
    } catch (error) {
      if (live) live.textContent = "Could not read the readings: " + String(error);
    }
  }

  function readHash() {
    const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
    if (hash.get("settings") !== null) { openSettings(); return; }
    const tab = hash.get("tab");
    const allowed = tabsWithOther();
    if (tab && allowed.includes(tab)) S.tab = tab;
    if (!allowed.includes(S.tab)) S.tab = "overview";
  }

  // Escape and the header buttons are one place, so a keyboard user never gets stuck.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && S.settings) { event.preventDefault(); closeSettings(); }
  });
  // One URL carries the open tab and the settings sheet, so the tray's link and a reload land in the same place.
  window.addEventListener("hashchange", () => {
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
  if (dark && dark.addEventListener) dark.addEventListener("change", () => { S.theme = themeOf(); render(); });

  // ---- start ---------------------------------------------------------------------------------
  async function boot() {
    try {
      const response = await fetch("/panel.json", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      window.buildModel(data, data.panel || {});
      S.prefs = window.PREFS;
      S.mode = S.prefs.show === "left" ? "left" : "used";
      window.setMode(S.mode);
      S.tabOrder = window.VM.order.slice();
      readHash();
      render();
      if (live) live.textContent = "Ready.";
      window.__panel = { S, render, reload, selectTab, openSettings, save, api: () => api };
      window.__ready = true;
    } catch (error) {
      if (live) live.textContent = "Could not read the panel: " + String(error);
    }
  }
  window.__panel = { S, render, reload, selectTab, openSettings, save, api: () => api };
  window.__drag = (blockId, dx, dy) => {
    if (!api) return null;
    api.press(blockId);
    return api.dragBy(dx, dy);
  };
  window.__dragEnd = () => { if (api) api.release(); };
  boot();

  function esc(text) { return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
  function escAttr(text) { return esc(text); }
  function cssEscape(text) { return String(text).replace(/"/g, '\\"'); }
})();
