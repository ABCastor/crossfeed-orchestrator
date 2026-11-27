// Draws the panel: Tiles (B+), made human. Adapted from the round-11 designer's renderer for the
// live glance document, with the round's four deltas: the Resets tile names itself and says "Use",
// the surplus run is the deficit's shape in green, links live in their own tile, and the plan label
// is the plan's own name. Every movable section is a .blk[data-block]; drag.js lays them out from an
// order (a legal state).
(function () {
  // The view model is built from the live document after this file loads, and again on every
  // reload; read it through the window so a fresh document reaches every drawing function.
  const VM = new Proxy({}, { get: (_, key) => window.VM && window.VM[key], has: (_, key) => !!window.VM && key in window.VM });
  const L = window.LOGOS;
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const TICKET = "🎟\uFE0F", COIN = "🪙";
  const ICON = {
    refresh: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13.2 8.6A5.2 5.2 0 1 1 11.6 4"/><path d="M12.2 1.8v2.8H9.4"/></svg>',
    sliders: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M2 4h12M2 8h12M2 12h12"/><circle cx="10.5" cy="4" r="1.6" fill="var(--bg)"/><circle cx="5" cy="8" r="1.6" fill="var(--bg)"/><circle cx="9" cy="12" r="1.6" fill="var(--bg)"/></svg>',
    ext: '<svg viewBox="0 0 12 12" width="9" height="9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 8.5l5-5M4.5 3.5h4v4"/></svg>',
    updown: '<svg viewBox="0 0 10 12" width="8" height="10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4.5L5 2l2.5 2.5M2.5 7.5L5 10l2.5-2.5"/></svg>',
    grid: '<svg viewBox="0 0 18 18" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2.5" y="2.5" width="5.5" height="5.5" rx="1.4"/><rect x="10" y="2.5" width="5.5" height="5.5" rx="1.4"/><rect x="2.5" y="10" width="5.5" height="5.5" rx="1.4"/><rect x="10" y="10" width="5.5" height="5.5" rx="1.4"/></svg>',
    dots: '<svg viewBox="0 0 18 18" width="17" height="17" fill="currentColor"><circle cx="4" cy="9" r="1.6"/><circle cx="9" cy="9" r="1.6"/><circle cx="14" cy="9" r="1.6"/></svg>',
    grip: '<svg viewBox="0 0 6 10" width="6" height="10" fill="currentColor"><circle cx="1" cy="1" r="1"/><circle cx="5" cy="1" r="1"/><circle cx="1" cy="5" r="1"/><circle cx="5" cy="5" r="1"/><circle cx="1" cy="9" r="1"/><circle cx="5" cy="9" r="1"/></svg>',
  };
  const logo = (name, size) => name && L[name] ? `<span class="logo" style="width:${size}px;height:${size}px">${L[name]}</span>` : "";
  const NAMES = { limits: "Limits", resets: "Resets", credits: "Credits", tokens: "Tokens", policy: "Spend policy",
    links: "Links", alert: "Alert", providers: "Providers", subscriptions: "Subscriptions", other: "Other" };
  const tileName = (id) => NAMES[id] || (VM.pluginTiles[id] ? VM.pluginTiles[id].title : id);

  // ---------- atoms ----------
  // Pace bar. Used mode: the fill grows from the left with what is used; past the pace tick it turns hatched
  // terracotta (deficit); short of the tick, a striped green run reaches the tick (surplus).
  // Left mode: the fill is what is left; the tick is the share that should still be left. Terracotta still
  // means deficit, olive-green still means surplus, and the words say it too.
  let MODE = "used";
  window.setMode = (m) => { MODE = m; };
  function bar(r, h) {
    const u = Math.max(0, Math.min(100, r.used));
    const v = MODE === "left" ? 100 - u : u, t = r.tick == null ? null : MODE === "left" ? 100 - r.tick : r.tick;
    let segs = "";
    if (r.full) segs = MODE === "left" ? "" : `<b class="seg-deficit" style="left:0;width:100%"></b>`;
    else if (t == null) segs = `<b class="seg-fill" style="left:0;width:${v}%"></b>`;
    else if (r.delta > 0) segs = MODE === "left"
      ? `<b class="seg-fill" style="left:0;width:${v}%"></b><b class="seg-deficit" style="left:${v}%;width:${t - v}%"></b>`
      : `<b class="seg-fill" style="left:0;width:${t}%"></b><b class="seg-deficit" style="left:${t}%;width:${v - t}%"></b>`;
    else segs = MODE === "left"
      ? `<b class="seg-fill" style="left:0;width:${t}%"></b><b class="seg-spare" style="left:${t}%;width:${v - t}%"></b>`
      : `<b class="seg-fill" style="left:0;width:${v}%"></b><b class="seg-spare" style="left:${v}%;width:${t - v}%"></b>`;
    const tick = t == null ? "" : `<i class="tick" style="left:${t}%"></i>`;
    const label = `${fig(r)}${r.paceWord ? ", " + r.paceWord : ""}`;
    return `<div class="bar" style="height:${h}px" role="img" aria-label="${esc(label)}"><span class="clip">${segs}</span>${tick}</div>`;
  }
  const fig = (r) => r.full ? "Used up" : MODE === "left" ? `${Math.round(100 - r.used)}% left` : `${Math.round(r.used)}% used`;
  const paceWord = (r) => !r.paceWord ? "" : `<span class="pace ${r.state}">${r.state === "deficit" ? '<i class="up"></i>' : '<i class="dn"></i>'}${esc(r.paceWord)}</span>`;
  const windowLabel = (r) => `<span class="win"><span class="wn">${esc(r.window)}</span> <span class="q">· ${esc(r.resets)}</span></span>`;

  // The one limit row part. Figure where the eye lands, the window named top right, then the bar, then pace.
  function limitRow(r) {
    const l3 = (r.paceWord || r.runsOut) && !r.full ? `<div class="line l3">${paceWord(r)}${r.runsOut ? `<span class="pace deficit">${esc(r.runsOut)}</span>` : ""}</div>` : "";
    return `<div class="lrow"><div class="line l1"><span class="fig num ${r.hot ? "hot" : ""}">${fig(r)}</span>${windowLabel(r)}</div>
      ${bar(r, 6)}${l3}</div>`;
  }
  function spark(days, w, h) {
    const n = days.length || 1, gap = 1.5, bw = Math.max(1, (w - gap * (n - 1)) / n), max = Math.max(...days, 1);
    const bars = days.map((v, i) => {
      const bh = v > 0 ? Math.max(2, Math.round((v / max) * h * 10) / 10) : 1;
      return `<rect x="${(i * (bw + gap)).toFixed(2)}" y="${(h - bh).toFixed(2)}" width="${bw.toFixed(2)}" height="${bh}" rx="${Math.min(1, bw / 2)}" class="${i === n - 1 ? "today" : v > 0 ? "day" : "zero"}"/>`;
    }).join("");
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Tokens per day, last ${n} days">${bars}</svg>`;
  }
  const grip = (id) => `<button class="grip" type="button" tabindex="0" aria-label="Move ${esc(tileName(id))}. Arrow keys move it." data-grip="${esc(id)}">${ICON.grip}</button>`;
  const btn = (txt, cls, act, key) => `<button type="button" class="btn ${cls || ""}" data-act="${act}" data-key="${esc(key || "")}">${esc(txt)}</button>`;

  // ---------- blocks ----------
  // Delta 1: the tile is titled "Resets", the row carries 🎟️ name · Next · until <date> · "Use".
  function resetsBlock(t, o) {
    const rows = t.resets.map((x) => {
      if (o.confirm === x.key) {
        const [ask, rest] = String(x.ask).split("? ");
        return `<div class="confirm" role="group" aria-label="${esc(x.ask)}">
          <div class="ask">${esc(ask)}?<br><span class="q">${esc(rest || "")}</span></div>
          ${x.effect ? `<div class="eff num">${esc(x.effect)}</div>` : ""}
          <div class="acts">${btn("Cancel", "", "cancel", x.key)}${btn("Use", "primary", "use", x.key)}</div></div>`;
      }
      if (o.used === x.key) return `<div class="rrow done"><span class="em">${TICKET}</span><span class="rn">${esc(x.name)}</span><span class="sp"></span><span class="q">Used. Reading again shortly.</span></div>`;
      return `<div class="rrow"><span class="em">${TICKET}</span><span class="rn">${esc(x.name)}</span>${x.next ? `<span class="next" title="Claude Code uses this one next">Next</span>` : ""}<span class="sp"></span><span class="q num until">until ${esc(x.until)}</span>${btn("Use", "sm", "ask", x.key)}</div>`;
    }).join("");
    if (o.confirmError) return `<div class="resets"><div class="rhead">Resets</div>${rows}<div class="eff num">${esc(o.confirmError)}</div></div>`;
    return `<div class="resets"><div class="rhead">Resets</div>${rows}</div>`;
  }
  function limitsBlock(t) {
    return t.sections.map((s) => `<section class="lsec">${s.title ? `<div class="sech">${esc(s.title)}${!s.rows.length && s.note ? ` <span class="q">· ${esc(s.note)}</span>` : ""}</div>` : ""}${s.rows.map(limitRow).join("")}</section>`).join("");
  }
  function creditsBlock(t) {
    const c = t.credits;
    return `<div class="credits rrow"><span class="em">${COIN}</span><span class="rn num">${esc(c.amount)} credits</span><span class="sp"></span><span class="q num until">until ${esc(c.until)}</span></div>`;
  }
  // Delta 3: the tokens tile keeps only its number and sparkline; the links have their own tile.
  function tokensBlock(tk) {
    if (!tk) return `<div class="q">No token log on this machine yet.</div>`;
    return `<div class="tokline"><span class="tok"><b class="num">${esc(tk.todayText)}</b> <span class="q">tokens today</span></span>${spark(tk.days, 52, 16)}</div>`;
  }
  function linksBlock(links) {
    return `<div class="links${links.length < 2 ? " one" : ""}">` + links.map((link) =>
      `<a class="lnk" href="${esc(link.href)}">${esc(link.label)} ${ICON.ext}</a>`).join("") + `</div>`;
  }
  function policyBlock(p) {
    return `<div class="plabel q">Spend policy</div><button type="button" class="popup" aria-haspopup="menu" data-act="policy"><span>${esc(p.level)}</span>${ICON.updown}</button>`;
  }
  function subsBlock() {
    if (!VM.subs.length) return `<div class="q">No subscription price set in the access overlay yet.</div>`;
    const rows = VM.subs.map((s) => `<div class="srow"><span class="sn">${esc(s.name)}${s.note ? ` <span class="q">${esc(s.note)}</span>` : ""}</span><span class="sh">${s.shareText || ""}</span><span class="sf q num">${esc(s.full)}</span><span class="sy num">${esc(s.yoursText)}</span></div>`).join("");
    return `<div class="subs">${rows}<div class="srow total"><span class="sn">You pay a month</span><span class="sh"></span><span class="sf"></span><span class="sy num">${esc(VM.total)}</span></div></div>`;
  }
  // Overview card for one provider: the same figure, bar and pace word, with the window under the bar.
  function overviewCard(id) {
    const o = VM.overview[id], r = o.row;
    const held = o.resets ? `<span class="held num" title="${o.resets} limit resets held"><span class="em">${TICKET}</span>${o.resets}</span>` : "";
    return `<a class="ocard" href="#" data-goto="${esc(id)}"><div class="line l1"><span class="oname">${logo(o.logo, 16)}<span class="t">${esc(o.title)}</span>${held}</span><span class="fig num ${r.hot ? "hot" : ""}">${fig(r)}</span></div>
      ${bar(r, 5)}
      <div class="line l3"><span class="q owin"><span class="wn">${esc(r.window)}</span> · ${esc(r.resets)}</span>${r.full ? "" : paceWord(r)}</div></a>`;
  }
  function alertBlock() {
    const v = VM.verdict;
    return `<div class="alertblk line l1"><span class="verdict ${v.tone}"><i></i>${esc(v.words)}</span></div>`;
  }
  function providersBlock() {
    if (!VM.order.length) return `<div class="q">No provider has a limit to show yet.</div>`;
    return `<div class="olist">${VM.order.filter((id) => VM.overview[id]).map(overviewCard).join("")}</div>`;
  }
  function otherBlock() {
    return `<div class="other"><div class="t">Not metered</div><div class="q">${esc(VM.other.join(" · ") || "Nothing outside a quota pool")}</div></div>`;
  }

  // A plugin's document, drawn with the same parts as a built-in tile: bar = the pace bar.
  function pluginBlock(key) {
    const entry = VM.pluginTiles[key];
    if (!entry) return `<div class="q">This plugin tile is not enabled or has no reading yet.</div>`;
    const doc = entry.doc || {};
    const rows = (doc.rows || []).map((row) => {
      if (row.kind === "bar") {
        const r = { used: row.used == null ? 0 : row.used, tick: row.pace == null ? null : row.pace, delta: row.pace == null ? null : (row.used || 0) - row.pace,
          full: row.used != null && row.used >= 100, window: row.window || "", resets: row.resets_at ? "resets " + row.resets_at.slice(0, 10) : "",
          paceWord: null, runsOut: null, hot: row.used != null && row.used >= 90 };
        return `<div class="prow"><div class="line l1"><span class="fig num">${row.used == null ? "no reading" : Math.round(row.used) + "% used"}</span><span class="win"><span class="wn">${esc(r.window)}</span> <span class="q">${r.resets ? "· " + esc(r.resets) : ""}</span></span></div>${bar(r, 6)}</div>`;
      }
      if (row.kind === "value") return `<div class="prow pvrow"><span class="plabel">${esc(row.label)}</span><span class="pval num ${row.tone === "warn" ? "hot" : ""}">${esc(row.value)}</span></div>`;
      if (row.kind === "sparkline") return `<div class="prow"><div class="tokline"><span class="tok"><span class="q">${esc(row.label)}</span>${row.today ? ` <b class="num">${esc(row.today)}</b>` : ""}</span>${spark(row.values.map(Number), 52, 16)}</div></div>`;
      if (row.kind === "link") return `<div class="prow"><a class="lnk" href="${esc(row.url)}">${esc(row.label)} ${ICON.ext}</a></div>`;
      return `<div class="prow">${esc(row.text || "")}</div>`;
    }).join("");
    // A healthy tile says nothing about being healthy; only a warning or an error is worth a word.
    const status = doc.status && doc.status !== "ok"
      ? `<div class="pstatus ${doc.status === "error" ? "error" : ""}">${esc(doc.status)}</div>` : "";
    return `<div class="ptile"><div class="ptitle">${esc(doc.title || entry.title)}</div>${status}${rows}</div>`;
  }

  // Which tiles a tab has, in the operator's order (panel preferences), each only when it has something to say.
  function available(tabId, id) {
    if (id.startsWith("plugin:")) return !!VM.pluginTiles[id];
    if (tabId === "overview") return ["alert", "providers", "subscriptions", "tokens", "links"].includes(id);
    if (tabId === "other") return id === "other";
    const t = VM.tabs[tabId];
    if (!t) return false;
    if (id === "limits") return t.sections.some((s) => s.rows.length || s.note);
    if (id === "resets") return !!t.resets;
    if (id === "credits") return !!t.credits;
    if (id === "tokens") return !!t.tokens;
    if (id === "policy") return !!t.policy;
    if (id === "links") return true;
    return false;
  }
  window.blocksFor = function (tabId, dir) {
    if (tabId === "other") return ["other"];
    const prefs = VM.prefs || {};
    const listed = ((prefs.tiles || {})[tabId]) || (tabId === "overview"
      ? ["alert", "providers", "subscriptions", "tokens", "links"] : ["limits", "resets", "credits", "tokens", "policy", "links"]);
    const off = new Set(((prefs.hidden_tiles || {})[tabId]) || []);
    return listed.filter((id) => !off.has(id) && available(tabId, id));
  };
  // B+ pairs two small tiles side by side (design r11); drag.js lays a pair out together.
  window.isHalf = (dir, id) => dir === "B" && ["tokens", "policy", "credits"].includes(id);
  window.tileLabel = tileName;
  window.tileAvailable = available;

  function blockHTML(dir, tabId, id, o) {
    let inner = "";
    if (id.startsWith("plugin:")) {
      // A plugin tile can sit on any tab, Overview included: it is checked before the tab's own parts.
      inner = pluginBlock(id);
    } else if (tabId === "overview") {
      if (id === "alert") inner = alertBlock();
      else if (id === "providers") inner = providersBlock();
      else if (id === "subscriptions") inner = subsBlock();
      else if (id === "tokens") inner = tokensBlock(VM.allTokens);
      else if (id === "links") inner = linksBlock([{ label: "Usage and cost", href: "/usage" }, { label: "Console", href: "/" }]);
    } else if (tabId === "other") {
      inner = otherBlock();
    } else {
      const t = VM.tabs[tabId];
      if (id === "limits") inner = limitsBlock(t);
      else if (id === "resets") inner = resetsBlock(t, o);
      else if (id === "credits") inner = creditsBlock(t);
      else if (id === "tokens") inner = tokensBlock(t.tokens);
      else if (id === "policy") inner = policyBlock(t.policy);
      else if (id === "links") {
        const links = [];
        links.push({ label: "Usage and cost", href: "/usage#" + encodeURIComponent(t.pool) });
        if (t.status_url) links.push({ label: "Status", href: t.status_url });
        links.push({ label: "Console", href: "/" });
        inner = linksBlock(links);
      }
    }
    const kind = tabId === "overview" && id === "providers" ? " prov" : "";
    return `<div class="blk${isHalf("B", id) ? " half" : ""}${kind}" data-block="${esc(id)}"><div class="lift">${grip(id)}<div class="inner">${inner}</div></div></div>`;
  }

  // ---------- frame ----------
  function tabsHTML(sel, order) {
    const all = [{ id: "overview", title: "Overview", icon: "grid" },
      ...order.filter((id) => VM.tabs[id]).map((id) => ({ id, title: VM.tabs[id].title, logo: VM.tabs[id].logo })),
      { id: "other", title: "Other", icon: "dots" }];
    return `<nav class="tabs" role="tablist">` + all.map((t) => {
      const on = t.id === sel, r = VM.overview[t.id] && VM.overview[t.id].row;
      const mark = t.logo ? logo(t.logo, 17) : `<span class="glyph">${ICON[t.icon]}</span>`;
      const fixed = t.id === "overview" || t.id === "other";
      const tick = r ? `<i class="qt"><b class="${r.hot ? "hot" : ""}" style="width:${Math.max(2, MODE === "left" ? 100 - r.used : r.used)}%"></b></i>` : `<i class="qt"></i>`;
      return `<button type="button" role="tab" aria-selected="${on}" class="tab${on ? " on" : ""}${fixed ? " fixed" : ""}" data-tab-id="${esc(t.id)}"><span class="tlift">${mark}<span class="tl">${esc(t.title)}</span>${tick}</span></button>`;
    }).join("") + `</nav>`;
  }
  const iconBtns = (tip) => `<span class="ibtns"><button type="button" class="ibtn" title="${esc(tip || "Refresh now")}" data-act="refresh" aria-label="Refresh now">${ICON.refresh}</button><button type="button" class="ibtn" title="Settings" data-act="settings" aria-label="Settings">${ICON.sliders}</button></span>`;
  function header(tabId, key) {
    if (tabId === "overview") {
      return `<header class="hd ov"><div class="line l1"><span class="name">Overview</span><span class="sp"></span>${iconBtns()}</div></header>`;
    }
    if (tabId === "other") return `<header class="hd"><div class="line l1"><span class="name">Other</span><span class="sp"></span>${iconBtns()}</div><div class="line sub"><span class="q">Not metered</span></div></header>`;
    const t = VM.tabs[key || tabId];
    if (!t) return `<header class="hd"><div class="line l1"><span class="name">${esc(tabId)}</span><span class="sp"></span>${iconBtns()}</div></header>`;
    const badge = t.badge ? `<span class="badge ${t.badge.tone}"><i></i>${esc(t.badge.word)}</span>` : "";
    return `<header class="hd"><div class="line l1">${logo(t.logo, 18)}<span class="name">${esc(t.fullTitle || t.title)}</span>${t.plan ? `<span class="plan">${esc(t.plan)}</span>` : ""}<span class="sp"></span>${badge}</div>
      <div class="line sub"><span class="q">${esc(t.updated)}</span><span class="sp"></span>${iconBtns()}</div></header>`;
  }
  // Delta 3: the incident line stays in the provider's header area, without a link of its own.
  function foot(tabId) {
    const t = VM.tabs[tabId];
    if (!t || !t.incident) return "";
    const i = t.incident;
    return `<div class="foot"><div class="incident ${i.severe ? "severe" : ""}"><i></i><span><b>${esc(i.level)}</b> · ${esc(i.text)}</span></div></div>`;
  }

  // o: { order: [block ids], tabOrder: [provider ids], confirm: resetKey, used: resetKey, mode }
  window.renderPanel = function (dir, tabId, theme, o = {}) {
    MODE = o.mode || "used";
    const tabOrder = o.tabOrder || VM.order;
    const order = o.order || blocksFor(tabId, dir);
    const blocks = order.map((id) => blockHTML(dir, tabId, id, { ...o, tabOrder })).join("");
    return `<div class="panel dir${dir} ${theme}" data-dir="${dir}" data-tab="${esc(tabId)}" data-theme="${theme}" data-mode="${MODE}">${tabsHTML(tabId, tabOrder)}<div class="body">${header(tabId, o.tabKey)}<div class="blocks" data-scope="${tabId === "overview" ? "overview" : "provider"}">${blocks}</div>${foot(o.tabKey || tabId)}</div><div class="live" aria-live="polite"></div></div>`;
  };
  window.blockHTML = blockHTML;
  window.ICONS = ICON;
})();
