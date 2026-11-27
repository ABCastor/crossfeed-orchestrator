// Builds the view model the panel draws, from the live glance document the console serves at
// /panel.json. Every word and number comes from that document and from the panel preferences;
// nothing is invented and nothing is fetched from anywhere else. Call it again with a fresh
// document to replace every number without a page load.
window.buildModel = function (glance, preferences) {
  const G = glance || window.GLANCE || { pools: [], subscriptions: [], subscription_spend: { plans: [] }, plugins: [] };
  const P = preferences || window.PREFS || { show: "used", tabs: [], tiles: {}, hidden_tiles: {} };
  window.GLANCE = G;
  window.PREFS = P;
  const NOW = G.generated_at ? Date.parse(G.generated_at) : Date.now();
  const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const pool = (id) => G.pools.find((p) => p.pool === id) || null;
  const fmt = (ms, o) => new Intl.DateTimeFormat("en-GB", { timeZone: TZ, ...o }).format(new Date(ms));
  const dayKey = (ms) => fmt(ms, { year: "numeric", month: "2-digit", day: "2-digit" });
  function when(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const hm = fmt(t, { hour: "2-digit", minute: "2-digit", hour12: false });
    if (dayKey(t) === dayKey(NOW)) return hm;
    if (t - NOW < 6.5 * 864e5) return fmt(t, { weekday: "short" }) + " " + hm;
    return fmt(t, { day: "numeric", month: "short" });
  }
  const date = (iso) => (Number.isFinite(Date.parse(iso)) ? fmt(Date.parse(iso), { day: "numeric", month: "short" }) : "");
  function span(s) {
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }
  const big = (n) => n >= 1e9 ? (n / 1e9).toFixed(n >= 1e10 ? 0 : 1).replace(/\.0$/, "") + "B"
    : n >= 1e6 ? Math.round(n / 1e6) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n || 0);
  const SYM = { USD: "$", GBP: "£", EUR: "€" };
  const money = (amount, currency) => amount == null ? "" : (SYM[currency] || (currency ? currency + " " : "")) + amount;

  // ---- one limit row ------------------------------------------------------------------------
  // window: the limit's plain name; tick: the pace mark (elapsed share); delta: used minus
  // elapsed, positive = deficit (ahead of pace), negative = surplus (slack).
  function row(l) {
    const scope = l.scope ? String(l.scope).replace(/ only$/, "") : null;
    const r = { key: l.name, minutes: l.minutes == null ? 0 : l.minutes, used: Math.min(100, Number(l.used_percent) || 0) };
    r.window = scope ? `${scope} ${String(l.title || l.name).toLowerCase()}` : (l.title || l.name);
    r.resets = l.resets ? "resets " + l.resets : (l.reset_at ? "resets " + when(l.reset_at) : (l.reset_unknown ? "reset unknown" : "no reset clock"));
    const p = l.pace;
    r.full = (Number(l.used_percent) || 0) >= 100;
    r.tick = p && p.elapsed_percent != null && !r.full ? Number(p.elapsed_percent) : null;
    r.delta = r.tick == null ? null : r.used - r.tick;
    const words = p && p.words ? String(p.words) : "";
    const runOut = /runs out in (.+)$/.exec(words);
    r.paceWord = r.delta == null ? null : Math.abs(r.delta) < 1 ? "on pace" : `${Math.round(Math.abs(r.delta))}% ${r.delta > 0 ? "deficit" : "surplus"}`;
    if (runOut && r.delta > 0) r.paceWord = `${Math.round(r.delta)}% deficit`;
    r.state = r.full ? "full" : r.delta == null ? "none" : r.delta > 0 ? "deficit" : "surplus";
    r.runsOutS = runOut ? parseSpan(runOut[1])
      : (p && p.status === "deficit" && p.runs_out_in_s ? Number(p.runs_out_in_s) : null);
    r.runsOut = r.runsOutS ? "Runs out in " + span(r.runsOutS) : null;
    r.hot = r.full || !!r.runsOut || r.used >= 90;
    return r;
  }
  // "2d 18h" -> seconds, so the tightest provider is still the one that runs out first.
  function parseSpan(text) {
    let total = 0;
    const re = /(\d+)\s*([dhms])/g;
    let m;
    while ((m = re.exec(String(text)))) total += Number(m[1]) * { d: 86400, h: 3600, m: 60, s: 1 }[m[2]];
    return total || null;
  }
  // A placeholder ("Starts with your next message") is never a limit: only windows the provider reports.
  const limitsOf = (p) => (p.limits || []).filter((l) => l.active !== false && l.kind !== "refusal").map(row)
    .sort((a, b) => a.minutes - b.minutes);

  function incident(p) {
    const i = p.incident;
    if (!i || i.level === "none" || !i.text) return null;
    return { level: i.level === "major" || i.level === "critical" ? "Major outage" : "Minor issue",
             severe: i.level === "major" || i.level === "critical", text: i.text, url: p.status_url };
  }
  const LEVEL = { off: "Off", low: "Low", normal: "Normal", high: "High", max: "Max", forced: "Max" };
  const policy = (sub) => {
    const s = (G.subscriptions || []).find((x) => x.id === sub);
    return s ? { level: LEVEL[s.level] || s.level } : null;
  };
  const tokenBlock = (p) => {
    const t = p.tokens;
    if (!t || !Array.isArray(t.days)) return null;
    return { today: Number(t.today) || 0, todayText: big(Number(t.today) || 0), days: t.days.map(Number) };
  };
  const updated = (p) => {
    const age = Number(p.reading_age_s);
    if (Number.isFinite(age)) return age <= 60 ? "Updated just now" : `Updated ${Math.round(age / 60)} min ago`;
    return p.reading_observed_at ? "Read " + when(p.reading_observed_at) : "Not read yet";
  };

  // ---- which pools make up which tab ---------------------------------------------------------
  const GROUP = {
    claude: ["claude"],
    codex: ["codex"],
    copilot: ["github-copilot-student"],
    antigravity: ["antigravity-gemini", "antigravity-3p", "antigravity-argon"],
    opencode: ["opencode-go"],
  };
  const METERED = new Set(["gemini-metered"]);
  const memberPools = (tab) => (GROUP[tab] || [tab]).map(pool).filter(Boolean);

  // Delta 4: the plan label is the overlay plan's name without the vendor word the header already says
  // ("Claude Max 20x" under a Claude header reads "Max 20x"; "Claude Ultra" reads "Ultra"). Another
  // vendor's word stays: "Google AI Pro" under an Antigravity header keeps "Google".
  const VENDOR_WORDS = { claude: ["claude", "anthropic"], codex: ["chatgpt", "openai"], copilot: ["github"],
    opencode: ["opencode"], antigravity: ["antigravity"] };
  function planLabel(name, tab) {
    if (!name) return "";
    const parts = String(name).trim().split(/\s+/);
    const words = new Set(VENDOR_WORDS[tab] || []);
    while (parts.length > 1 && words.has(parts[0].toLowerCase())) parts.shift();
    return parts.join(" ");
  }

  // ---- one provider tab ----------------------------------------------------------------------
  function buildTab(id) {
    const pools = memberPools(id);
    if (!pools.length) return null;
    const lead = pools[0];
    const SHORT = { claude: "Claude", codex: "Codex", copilot: "Copilot", antigravity: "AGY", opencode: "OpenCode" };
    const title = SHORT[id] || lead.short_label || String(lead.label || lead.pool).replace(/\s*\(.*?\)\s*/g, "");
    const sections = (pools.length === 1 ? [{ rows: limitsOf(pools[0]) }]
      : pools.map((p) => ({ title: String(p.label || p.pool).split("·").pop().trim(), rows: limitsOf(p) })))
      .filter((section) => section.rows.length || !section.title);
    // Argon reports the same window as Gemini's 5-hour: say so, do not draw it twice.
    if (pools.length > 1) {
      const first = limitsOf(pools[0]);
      for (const section of sections.slice(1)) {
        const dupes = section.rows.filter((r) => first.some((o) => o.key === r.key)).map((r) => r.key);
        section.rows = section.rows.filter((r) => !dupes.includes(r.key));
        if (dupes.length && !section.rows.length) section.note = "Shares the " + (first[0] ? first[0].window : "") + " limit";
      }
    }
    const full = pools.find((p) => p.state === "EXHAUSTED" || (Number(p.used_percent) >= 100 && (p.limits || []).some((l) => l.active !== false)));
    const hot = pools.map((p) => Math.max(0, ...(p.limits || []).filter((l) => l.active !== false).map((l) => Number(l.used_percent) || 0)));
    const worst = Math.max(0, ...hot);
    const creditsSource = pools.find((p) => p.credits && (p.credits.balance != null || p.credits.amount != null));
    const credits = creditsSource ? {
      amount: Number(creditsSource.credits.balance != null ? creditsSource.credits.balance : creditsSource.credits.amount).toLocaleString("en-US"),
      until: date(creditsSource.credits.expires_at) || date(creditsSource.credits.expirations && creditsSource.credits.expirations[0]) || "",
    } : null;
    const tokens = pools.map(tokenBlock).find(Boolean) || null;
    const resets = buildResets(pools);
    return {
      id, title, fullTitle: id === "antigravity" ? "Antigravity" : title,
      shortTitle: id === "antigravity" ? "AGY" : title,
      logo: logoFor(id, pools),
      plan: planLabel((lead.plan || {}).name, id),
      updated: updated(lead),
      badge: full ? { word: "Used up", tone: "hot" } : worst >= 90 ? { word: "Low", tone: "hot" } : null,
      sections, resets, credits, tokens,
      policy: policy(lead.subscription),
      incident: incident(lead),
      pools: pools.map((p) => p.pool),
      pool: lead.pool,
      status_url: (pools.find((p) => p.status_url) || {}).status_url || null,
    };
  }

  const LOGO = {
    claude: "claude-color", codex: "openai", copilot: "githubcopilot", antigravity: "antigravity-color",
    opencode: "opencode", "openrouter-free": "openrouter", "gemini-metered": "gemini-color",
  };
  const logoFor = (id, pools) => LOGO[id] || LOGO[pools[0].pool] || (window.LOGOS[String(pools[0].pool).split("-")[0]] ? String(pools[0].pool).split("-")[0] : null);

  // ---- resets: the Claude grants, as the glance carries them ---------------------------------
  function buildResets(pools) {
    const rows = [];
    for (const p of pools) {
      const grants = p.limit_resets;
      if (!Array.isArray(grants) || !grants.length) continue;
      const used = Object.fromEntries((p.limits || []).map((l) => [l.name, Number(l.used_percent) || 0]));
      // A grant names vendor windows ("seven_day"); a limit row is named by the reading ("weekly").
      const ALIAS = { seven_day: "weekly", five_hour: "session", seven_day_overage_included: "claude-weekly-scoped-fable" };
      const limitFor = (name) => {
        const key = ALIAS[name] || name;
        if (used[key] != null) return key;
        const found = Object.keys(used).find((other) => other === key || other.endsWith("-" + key) || other.startsWith(key + "-"));
        return found || key;
      };
      const titleOf = (name) => name === "seven_day" || name === "weekly" ? "Weekly"
        : name === "session" || name === "five_hour" ? "5-hour"
        : String(name).startsWith("claude-weekly-scoped-") ? "Fable" : String(name);
      for (const g of grants) {
        const clears = Array.isArray(g.clears) && g.clears.length ? g.clears : [];
        const name = clears.length ? titleOf(clears[0]) : "Limit";
        rows.push({
          key: g.key,
          name,
          until: date(g.expires_at),
          next: g.next === true,
          ask: `Use the ${name === "Weekly" ? "weekly" : name === "5-hour" ? "5-hour" : name.toLowerCase()} reset? This can't be undone.`,
          effect: clears.map((k) => { const name = limitFor(k); return used[name] == null ? "" : `${titleOf(k)} ${used[name]}% → 0%`; })
            .filter(Boolean).join(" · ") || null,
        });
      }
    }
    return rows.length ? rows : null;
  }

  // ---- Overview rows ---------------------------------------------------------------------------
  function tightest(t) {
    const rows = t.sections.flatMap((s) => (s.rows || []).map((r) => ({ ...r,
      section: s.title,
      window: s.title && s.title !== "Claude and GPT" ? `${s.title} ${String(r.window).toLowerCase()}` : String(r.window).replace(/^Premium m/, "M") })));
    if (!rows.length) return null;
    const score = (r) => r.full ? 1e12 : r.runsOutS ? 1e9 - r.runsOutS : r.used;
    return rows.reduce((a, b) => (score(b) > score(a) ? b : a), rows[0]);
  }

  // ---- tokens: 14 days, summed over every provider that reports them ---------------------------
  const withTokens = G.pools.filter((p) => tokenBlock(p));
  function sumDays(days, length) {
    const out = new Array(length).fill(0);
    for (const series of days) {
      const offset = length - series.length;
      series.forEach((value, i) => { const at = i + offset; if (at >= 0) out[at] += Number(value) || 0; });
    }
    return out;
  }
  const seriesLength = Math.max(14, ...withTokens.map((p) => tokenBlock(p).days.length));
  const allTokens = withTokens.length ? {
    today: withTokens.reduce((n, p) => n + tokenBlock(p).today, 0),
    todayText: big(withTokens.reduce((n, p) => n + tokenBlock(p).today, 0)),
    days: sumDays(withTokens.map((p) => tokenBlock(p).days), seriesLength),
  } : null;

  // ---- tabs, in the operator's order -----------------------------------------------------------
  const requested = (P.tabs && P.tabs.length ? P.tabs : ["overview", "claude", "codex", "copilot", "antigravity", "opencode", "other"]);
  const hiddenTabs = new Set(P.hidden_tabs || []);
  const tabs = {};
  const order = [];
  for (const id of requested) {
    if (id === "overview" || id === "other" || hiddenTabs.has(id)) continue;
    const tab = buildTab(id);
    if (tab) { tabs[id] = tab; order.push(id); }
  }
  // A pool no group names still deserves a place: it goes to Other, never silently missing.
  const grouped = new Set(Object.values(GROUP).flat());
  const other = G.pools.filter((p) => !grouped.has(p.pool) || METERED.has(p.pool))
    .map((p) => String(p.label || p.pool).replace(/\s*\(.*?\)\s*/g, ""));

  const overview = {};
  for (const id of order) {
    const t = tabs[id], r = tightest(t);
    if (!r) continue;
    overview[id] = { id, title: t.title, plan: t.plan, logo: t.logo, row: r,
                     resets: t.resets ? t.resets.length : 0, badge: t.badge };
  }
  const soon = Object.values(overview).filter((o) => o.row.runsOutS).sort((a, b) => a.row.runsOutS - b.row.runsOutS)[0];
  const verdict = soon ? { tone: "hot", words: `${soon.row.section || soon.title} runs out in ${span(soon.row.runsOutS)}` }
    : Object.keys(overview).length ? { tone: "good", words: "Nothing runs out before it refills" }
    : { tone: "good", words: "No provider has a limit to show yet" };

  // ---- subscriptions ---------------------------------------------------------------------------
  const spend = G.subscription_spend || {};
  const plans = (spend.plans || []).filter((p) => p.amount != null);
  const subs = plans.map((p) => ({
    id: p.id, name: p.name || p.id, note: null,
    shareText: p.share === "1/2" ? "½" : (p.share && p.share !== "1/1" ? p.share : null),
    full: money(p.amount, p.currency),
    yoursText: p.operator_amount != null ? money(p.operator_amount, p.currency) : "—",
  }));
  const usd = spend.usd || null;
  const total = usd ? "≈ $" + Math.round(Number(usd.operator != null ? usd.operator : usd.total) || 0) : "";

  // ---- plugin tiles ----------------------------------------------------------------------------
  const pluginTiles = {};
  for (const entry of G.plugins || []) {
    const key = "plugin:" + entry.tile;
    pluginTiles[key] = { ...entry, title: (entry.doc && entry.doc.title) || entry.tile };
  }

  window.VM = {
    now: NOW, tabs, overview, verdict, subs, total, allTokens, other, span, big, order,
    pluginTiles, prefs: P, hiddenTabs, seriesLength,
    tabIds: requested.filter((id) => id === "overview" || id === "other" || (tabs[id] && !hiddenTabs.has(id))),
    tabTitle: (id) => id === "overview" ? "Overview" : id === "other" ? "Other" : (tabs[id] ? tabs[id].title : id),
  };
  return window.VM;
};
