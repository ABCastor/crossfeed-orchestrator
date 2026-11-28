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

  const num = (v) => (v == null || v === "" || !Number.isFinite(Number(v)) ? null : Number(v));
  const dollars = (n) => n == null ? "" : "$" + (Math.abs(n) >= 100 ? Math.round(n).toLocaleString("en-US") : n.toFixed(2));
  // The calendar month so far, in the browser's own time zone: the budget bar's pace tick.
  function monthPace() {
    const d = new Date(NOW), start = new Date(d.getFullYear(), d.getMonth(), 1).getTime(), end = new Date(d.getFullYear(), d.getMonth() + 1, 1).getTime();
    return { elapsed: Math.max(0, Math.min(100, (NOW - start) / (end - start) * 100)), elapsedDays: Math.max(0.5, (NOW - start) / 864e5), end };
  }

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
    return s ? { id: s.id, level: LEVEL[s.level] || s.level, raw: s.level === "max" ? "forced" : s.level } : null;
  };
  const tokenBlock = (p) => {
    const t = p.tokens;
    if (!t || !Array.isArray(t.days)) return null;
    return { today: Number(t.today) || 0, todayText: big(Number(t.today) || 0), days: t.days.map(Number) };
  };
  const updated = (p) => {
    const apiAge = p.api && p.api.observed_at ? (NOW - Date.parse(p.api.observed_at)) / 1000 : NaN;
    const age = p.reading_age_s != null ? Number(p.reading_age_s) : (Number.isFinite(apiAge) ? Math.max(0, apiAge) : NaN);
    if (Number.isFinite(age)) return age <= 60 ? "Updated just now" : `Updated ${Math.round(age / 60)} min ago`;
    return p.reading_observed_at ? "Read " + when(p.reading_observed_at) : "Not read yet";
  };

  // ---- daily series: tokens, cost and requests, whichever the pool has -----------------------------
  // An API pool's own days; else the provider's saved usage-cost cache (30 days, tokens and USD); else the
  // 14-day token series. A metric the pool does not report is null, so the menu offers only what exists.
  function seriesOf(p) {
    const a = p.api && Array.isArray(p.api.days) && p.api.days.length ? p.api.days : null;
    if (a) {
      const has = (k) => a.some((r) => r[k] != null);
      return { tokens: has("tokens") ? a.map((r) => Number(r.tokens) || 0) : null,
               cost: has("usd") ? a.map((r) => Number(r.usd) || 0) : null,
               requests: has("requests") ? a.map((r) => Number(r.requests) || 0) : null };
    }
    const daily = p.usage_cost && Array.isArray(p.usage_cost.daily) && p.usage_cost.daily.length ? p.usage_cost.daily : null;
    if (daily) {
      return { tokens: daily.map((r) => Number(r.tokens) || 0), cost: daily.some((r) => r.usd != null) ? daily.map((r) => Number(r.usd) || 0) : null, requests: null };
    }
    const t = p.tokens && Array.isArray(p.tokens.days) ? p.tokens.days.map(Number) : null;
    return t ? { tokens: t, cost: null, requests: null } : null;
  }
  function sumSeries(list) {
    const real = list.filter(Boolean);
    if (!real.length) return null;
    const out = {};
    for (const metric of ["tokens", "cost", "requests"]) {
      const parts = real.map((s) => s[metric]).filter(Boolean);
      if (!parts.length) { out[metric] = null; continue; }
      const length = Math.max(...parts.map((x) => x.length)), sum = new Array(length).fill(0);
      for (const part of parts) part.forEach((v, i) => { sum[i + length - part.length] += Number(v) || 0; });
      out[metric] = sum;
    }
    return out;
  }
  const METRICS = ["tokens", "cost", "requests"];
  const RANGES = [7, 14, 30];
  const tileOptions = (tab, tile) => ((P.tile_options || {})[tab + "/" + tile]) || {};
  // What a sparkline tile draws: the chosen metric over the chosen range, with today's figure in words.
  function sparkFor(series, tab, tile) {
    if (!series) return null;
    const offered = METRICS.filter((m) => series[m]);
    if (!offered.length) return null;
    const opt = tileOptions(tab, tile);
    const fallback = tile === "activity" ? (offered.includes("cost") && series.cost.some((v) => v > 0) ? "cost" : offered.includes("requests") ? "requests" : offered[0]) : (offered.includes("tokens") ? "tokens" : offered[0]);
    const metric = offered.includes(opt.metric) ? opt.metric : fallback;
    const days = RANGES.includes(opt.days) ? opt.days : 14;
    const values = series[metric].slice(-days);
    const today = values.length ? values[values.length - 1] : 0;
    const figure = metric === "cost" ? dollars(today) : metric === "requests" ? Math.round(today).toLocaleString("en-US") : big(Math.round(today));
    const word = metric === "cost" ? "today" : metric === "requests" ? "requests today" : "tokens today";
    return { metric, days, values, today, figure, word, metrics: offered, label: { tokens: "Tokens", cost: "Cost", requests: "Requests" }[metric] };
  }

  // ---- an API pool: spend against the operator's budget, balance, models ---------------------------
  // Paces like any limit: the tick is the share of the month gone, the stripes say ahead or behind it.
  function paceFields(r) {
    r.full = r.used >= 100;
    r.tick = r.tick == null || r.full ? null : r.tick;
    r.delta = r.tick == null ? null : r.used - r.tick;
    r.paceWord = r.delta == null ? null : Math.abs(r.delta) < 1 ? "on pace" : `${Math.round(Math.abs(r.delta))}% ${r.delta > 0 ? "deficit" : "surplus"}`;
    r.state = r.full ? "full" : r.delta == null ? "none" : r.delta > 0 ? "deficit" : "surplus";
    r.hot = r.full || r.used >= 90 || !!r.runsOut;
    return r;
  }
  function apiModel(p) {
    const a = p.api;
    if (!a || typeof a !== "object") return null;
    const budgets = P.budgets || {};
    // Crossfeed Chat is a relay with no money to budget.
    const relay = p.pool === "chatgpt-work";
    const budget = relay ? null : num(budgets[p.pool]) > 0 ? num(budgets[p.pool]) : (a.budget && num(a.budget.monthly_usd) > 0 ? num(a.budget.monthly_usd) : null);
    const sp = a.spend || {};
    const month = num(sp.month_usd), week = num(sp.week_usd), today = num(sp.today_usd);
    const credits = num(a.credits_usd), balance = num(a.balance_usd), limit = num(a.key_limit_usd), remaining = num(a.key_remaining_usd);
    let bar = null;
    if (budget != null && month != null) {
      const pace = monthPace(), used = month / budget * 100;
      bar = { key: "spend", minutes: 0, used: Math.min(100, used), tick: pace.elapsed, window: "This month", resets: "resets " + date(new Date(pace.end).toISOString()),
              figText: `${dollars(month)} of ${dollars(budget)}`, figLeft: `${dollars(Math.max(0, budget - month))} left`, fullText: "Over budget" };
      if (used < 100 && used > pace.elapsed && month > 0) {
        bar.runsOutS = Math.max(60, (budget - month) / (month / pace.elapsedDays) * 86400);
        bar.runsOut = "Budget runs out in " + span(bar.runsOutS);
      }
      paceFields(bar);
    } else if (balance != null && credits > 0) {
      bar = paceFields({ key: "balance", minutes: 0, used: Math.max(0, Math.min(100, (credits - balance) / credits * 100)), tick: null, window: "Credit", resets: "",
                         figText: `${dollars(balance)} left`, figLeft: `${dollars(balance)} left` });
    } else if (limit > 0 && remaining != null) {
      bar = paceFields({ key: "key", minutes: 0, used: Math.max(0, Math.min(100, (limit - remaining) / limit * 100)), tick: null, window: "Key limit", resets: "",
                         figText: `${dollars(remaining)} left`, figLeft: `${dollars(remaining)} left` });
    }
    const spend = month != null || week != null || today != null ? { today: dollars(today), week: dollars(week), month: dollars(month) } : null;
    const hasMoney = [month, week, today].some((v) => v != null && v > 0);
    const hasBalance = balance != null || limit != null;
    const models = (a.top_models || []).slice(0, 4).map((m) => ({ name: String(m.model).replace(/^[^/]+\//, ""), full: m.model,
      usd: num(m.usd), requests: num(m.requests), text: m.usd != null && Number(m.usd) > 0 ? dollars(Number(m.usd)) : "" }));
    const source = a.source === "local-meter" ? (a.source_label || "Measured by Crossfeed") : null;
    return { bar, spend, hasMoney, relay, budget, source, models, series: seriesOf(p), billingUrl: a.billing_url || null, error: a.error || null,
             balance: hasBalance ? { credits: dollars(credits), balance: dollars(balance), left: balance != null ? dollars(balance) : "", limit: dollars(limit), remaining: dollars(remaining) } : null,
             daysSource: a.days_source === "local-meter" ? "Measured by Crossfeed" : null };
  }

  // ---- which pools make up which tab ---------------------------------------------------------
  const GROUP = {
    claude: ["claude"],
    codex: ["codex"],
    copilot: ["github-copilot-student"],
    antigravity: ["antigravity-gemini", "antigravity-3p", "antigravity-argon"],
    opencode: ["opencode-go"],
  };
  // Pools billed per token, by prepaid credit or through a relay are tabs of their own, named by pool key.
  const API_NAME = { "openrouter-free": "OpenRouter", "gemini-metered": "Gemini API", "chatgpt-work": "Crossfeed Chat" };
  // The tab's label is the short one so eight tabs still fit 310 px; the header and Settings say the full name.
  const API_TAB = { "chatgpt-work": "Chat" };
  const TITLES = { claude: "Claude", codex: "Codex", copilot: "Copilot", antigravity: "AGY", opencode: "OpenCode", ...API_NAME };
  const isApi = (id) => !!API_NAME[id] || !!(pool(id) && pool(id).api);
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
    const SHORT = { claude: "Claude", codex: "Codex", copilot: "Copilot", antigravity: "AGY", opencode: "OpenCode", ...API_NAME };
    const title = API_TAB[id] || SHORT[id] || lead.short_label || String(lead.label || lead.pool).replace(/\s*\(.*?\)\s*/g, "");
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
    const series = sumSeries(pools.map(seriesOf));
    const api = isApi(id) ? apiModel(lead) : null;
    const resets = buildResets(pools);
    return {
      id, title, fullTitle: id === "antigravity" ? "Antigravity" : API_NAME[id] || title,
      shortTitle: id === "antigravity" ? "AGY" : title,
      logo: logoFor(id, pools),
      plan: planLabel((lead.plan || {}).name, id),
      updated: updated(lead),
      badge: full ? { word: "Used up", tone: "hot" } : worst >= 90 ? { word: "Low", tone: "hot" } : null,
      sections, resets, credits, tokens, series, api, isApi: isApi(id),
      policy: policy(lead.subscription),
      incident: incident(lead),
      pools: pools.map((p) => p.pool),
      pool: lead.pool,
      status_url: (pools.find((p) => p.status_url) || {}).status_url || null,
    };
  }

  const LOGO = {
    claude: "claude-color", codex: "openai", copilot: "githubcopilot", antigravity: "antigravity-color",
    opencode: "opencode", "openrouter-free": "openrouter", "gemini-metered": "gemini-color", "chatgpt-work": "chat",
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
  const requested = (P.tabs && P.tabs.length ? P.tabs.slice() : ["overview", "claude", "codex", "copilot", "antigravity", "opencode", "openrouter-free", "gemini-metered", "chatgpt-work", "other"]);
  const hiddenTabs = new Set(P.hidden_tabs || []);
  const tabs = {};
  const order = [];
  for (const id of requested) {
    if (id === "overview" || id === "other" || hiddenTabs.has(id)) continue;
    const tab = buildTab(id);
    if (tab) { tabs[id] = tab; order.push(id); }
  }
  // A pool no group names still deserves a place: it goes to Other, never silently missing. A pool with an
  // `api` block and no tab of its own is added after the listed tabs, so a new API account shows up without an edit.
  const grouped = new Set([...Object.values(GROUP).flat(), ...order, ...requested]);
  for (const p of G.pools) {
    if (p.api && !grouped.has(p.pool) && !hiddenTabs.has(p.pool)) {
      const tab = buildTab(p.pool);
      if (tab) { tabs[p.pool] = tab; order.push(p.pool); requested.push(p.pool); grouped.add(p.pool); }
    }
  }
  const other = G.pools.filter((p) => !grouped.has(p.pool))
    .map((p) => {
      const name = String(p.label || p.pool).replace(/\s*\(.*?\)\s*/g, "");
      const a = apiModel(p);
      return a && a.spend && a.spend.month ? `${name} · ${a.spend.month} this month${a.source ? " (" + a.source.toLowerCase() + ")" : ""}` : name;
    });

  const overview = {};
  for (const id of order) {
    const t = tabs[id];
    // An API tab speaks for itself with its spend or credit bar; it has no provider limit to name.
    const r = t.api && t.api.bar ? { ...t.api.bar, section: t.title + " budget" } : tightest(t);
    if (!r) continue;
    overview[id] = { id, title: t.title, plan: t.plan, logo: t.logo, row: r,
                     resets: t.resets ? t.resets.length : 0, badge: t.badge };
  }
  const soon = Object.values(overview).filter((o) => o.row.runsOutS).sort((a, b) => a.row.runsOutS - b.row.runsOutS)[0];
  const verdict = soon ? { tone: "hot", words: `${soon.row.section || soon.title} runs out in ${span(soon.row.runsOutS)}` }
    : Object.keys(overview).length ? { tone: "good", words: "Nothing runs out before it refills" }
    : { tone: "good", words: "No provider has a limit to show yet" };


  // ---- the menu bar item: icon and text, exactly as the tray draws them -----------------------------
  // The Linux tray (menubar/main.go) and the Settings preview both follow this: the most pressed allowance
  // (or the chosen provider), named, with its window and figure; the icon is the Crossfeed mark, that
  // provider's logo, or nothing.
  const AGY_NAMES = { "antigravity-gemini": "Gemini", "antigravity-3p": "Claude/GPT", "antigravity-argon": "Argon" };
  const groupOf = (p) => Object.keys(GROUP).find((k) => GROUP[k].includes(p.pool)) || p.pool;
  const barWindowName = (l) => {
    const m = l && l.minutes;
    if (m != null) {
      if (m === 300) return "5h";
      if (m === 1440) return "Day";
      if (m === 10080) return "Week";
      if (m >= 40000 && m <= 45000) return "Month";
      if (m < 1440) return Math.round(m / 60) + "h";
    }
    const t = String((l && l.title) || "").toLowerCase();
    if (t.includes("5-hour") || t.includes("5h")) return "5h";
    if (t.includes("week")) return "Week";
    if (t.includes("month")) return "Month";
    if (t.includes("day")) return "Day";
    if (t.includes("session")) return "Session";
    return String((l && l.title) || "Usage").split(" ")[0].slice(0, 8);
  };
  const shortCountdown = (iso) => {
    const s = Math.floor((Date.parse(iso) - NOW) / 1000);
    if (!Number.isFinite(s)) return null;
    if (s <= 0) return "now";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${Math.max(1, m)}m`;
  };
  function barTitle(mb) {
    const prefs = { icon: "provider", show: "pressed", window: "tightest", count: "used", text: "window", extra: "none", reset: "countdown", ...(mb || {}) };
    // The same defaults panel_prefs.py fills: a value outside its choices is the default.
    const choose = (key, allowed, fallback) => { if (!allowed.includes(prefs[key])) prefs[key] = fallback; };
    choose("icon", ["crossfeed", "provider", "none"], "provider"); choose("text", ["full", "window", "number", "none"], "window");
    choose("extra", ["none", "reset", "pace", "cost"], "none"); choose("reset", ["countdown", "clock"], "countdown");
    choose("window", ["tightest", "5h", "Week", "Month"], "tightest"); choose("count", ["used", "left"], "used");
    if (prefs.icon === "none" && prefs.text === "none") prefs.icon = "crossfeed";
    const windows = (p) => (p.limits || []).filter((l) => l.kind === "window" && l.used_percent != null && l.active !== false);
    // How many points a window is ahead of or behind an even burn: the glance's own count, else read from its words.
    const pts = (pace) => pace ? (num(pace.points) != null ? Number(pace.points) : /(\d+)%/.test(pace.words || "") ? Number(/(\d+)%/.exec(pace.words)[1])
      : pace.delta_percent != null ? Math.abs(Math.round(pace.delta_percent)) : 0) : 0;
    const points = (pace) => (pace && pace.status === "deficit" ? pts(pace) : 0);
    const live = G.pools.filter((p) => p.measured && p.used_percent != null);
    let ranked = [];
    if (prefs.window !== "tightest") {
      ranked = live.map((p) => { const hit = windows(p).find((l) => barWindowName(l) === prefs.window && !l.scope) || windows(p).find((l) => barWindowName(l) === prefs.window);
        return hit ? { p, limit: hit, used: Number(hit.used_percent), deficit: points(hit.pace) } : null; }).filter(Boolean);
    }
    if (!ranked.length) {
      ranked = live.map((p) => {
        const used = Number(p.used_percent), ws = windows(p), match = ws.filter((l) => Math.abs(Number(l.used_percent) - used) < 0.5);
        const limit = match.find((l) => !l.scope) || match[0] || ws.slice().sort((a, b) => b.used_percent - a.used_percent)[0] || null;
        return { p, limit, used, deficit: points(p.pace) };
      });
    }
    ranked.sort((a, b) => b.deficit - a.deficit || b.used - a.used);
    const focal = (prefs.show !== "pressed" ? ranked.find((r) => groupOf(r.p) === prefs.show) : null) || ranked[0];
    if (!focal) return { text: "", icon: prefs.icon === "none" ? "none" : "crossfeed", logo: null, name: "", window: "", used: null, tooltip: "Crossfeed · no quota read", deficit: false };
    const name = AGY_NAMES[focal.p.pool] || (tabs[groupOf(focal.p)] ? tabs[groupOf(focal.p)].title : TITLES[groupOf(focal.p)] || String(focal.p.name || focal.p.pool).slice(0, 14));
    const window = barWindowName(focal.limit);
    const value = Math.max(0, Math.min(100, Math.round(prefs.count === "left" ? 100 - focal.used : focal.used)));
    const amount = value + "%" + (prefs.count === "left" ? " left" : "");
    let text = prefs.text === "full" ? `${name} ${window} ${amount}` : prefs.text === "window" ? `${window} ${amount}` : prefs.text === "number" ? amount : "";
    const pace = focal.limit && focal.limit.pace || focal.p.pace;
    const extras = {
      reset: focal.limit && focal.limit.reset_at ? (prefs.reset === "clock" ? when(focal.limit.reset_at) : shortCountdown(focal.limit.reset_at)) : null,
      pace: pace && pace.status === "deficit" ? pts(pace) + "% over" : pace && pace.status === "surplus" ? pts(pace) + "% under" : pace && pace.status === "on_pace" ? "on pace" : null,
      cost: (() => { const sum = G.pools.reduce((n, p) => n + (num(p.usage_cost && p.usage_cost.today_usd) || 0) + (num(p.api && p.api.spend && p.api.spend.today_usd) || 0), 0); return sum > 0 ? "US$" + sum.toFixed(2) + " today" : null; })(),
    };
    const extra = extras[prefs.extra] || null;
    if (text && extra) text += " · " + extra;
    const logo = LOGO[groupOf(focal.p)] || LOGO[focal.p.pool] || null;
    return { text, icon: prefs.icon === "provider" && !logo ? "crossfeed" : prefs.icon, logo, name, window, used: focal.used,
             tooltip: focal.used >= 100 ? `${name} used up` : `${name} ${window}: ${Math.round(focal.used)}% used`, deficit: focal.deficit > 0 };
  }

  // ---- every provider's current incident, for the Overview Status tile -------------------------------
  const incidents = order.filter((id) => tabs[id].incident).map((id) => ({ id, title: tabs[id].title, logo: tabs[id].logo, ...tabs[id].incident }));
  const allSeries = sumSeries(G.pools.map(seriesOf));

  // ---- subscriptions ---------------------------------------------------------------------------
  const spend = G.subscription_spend || {};
  const plans = (spend.plans || []).filter((p) => p.amount != null);
  const subs = plans.map((p) => ({
    id: p.id, name: p.name || p.id, note: null,
    shareText: p.share === "1/2" ? "½" : (p.share && p.share !== "1/1" ? p.share : null),
    full: money(p.amount, p.currency),
    yoursText: p.operator_usd != null ? "$" + Math.round(Number(p.operator_usd))
      : (p.operator_amount != null ? money(p.operator_amount, p.currency) : "—"),
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
    pluginTiles, prefs: P, hiddenTabs, seriesLength, incidents, allSeries, sparkFor, tileOptions, dollars, isApi, barTitle,
    tabIds: requested.filter((id) => id === "overview" || id === "other" || (tabs[id] && !hiddenTabs.has(id))),
    tabTitle: (id) => id === "overview" ? "Overview" : id === "other" ? "Other" : (tabs[id] ? (API_NAME[id] || tabs[id].title) : TITLES[id] || id),
  };
  return window.VM;
};
