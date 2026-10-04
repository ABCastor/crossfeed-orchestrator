// Protect product names, model names, identifiers and amounts in text added by the console.
// Server-rendered text uses keep.py. Mark words, not whole sentences, and retain touching spaces.
(function (root) {
  'use strict';

  const NAMES = [
    'Crossfeed Orchestrator', 'Crossfeed', 'Castor', 'Chip',
    'Artificial Analysis', 'GitHub Copilot', 'Copilot Student', 'Antigravity', 'OpenRouter', 'OpenCode Go', 'OpenCode', 'ChatGPT Plus',
    'ChatGPT', 'Copilot', 'CodexBar', 'Microsoft', 'Anthropic', 'OpenAI', 'Google AI', 'Google', 'Gmail', 'Tailscale', '1Password',
    '(?:Claude|Gemini|GPT|Kimi|DeepSeek|Codex|Opus|Sonnet|Haiku)(?:[ -](?:[A-Z]?\\d[\\w.]*|Opus|Sonnet|Haiku|Max|Pro|Flash|Ultra|Mini|Nano|Turbo|Sol|Terra|Luna|Astra|Code|Thinking|Preview|Latest|Instruct))*',
    'we give a dam', 'We give a dam',
  ];
  const SOURCE = [
    'https?:\\/\\/[^\\s<>"\']+',
    '[\\w.+-]+@[\\w-]+(?:\\.[\\w-]+)+',
    '\\b[a-z0-9][a-z0-9-]*(?:\\.[a-z0-9-]+)*\\.(?:com|org|net|io|dev|app|aero)\\b',
    '\\b(?:' + NAMES.map(n => (n.startsWith('(?:') ? n : n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))).join('|') + ')(?![A-Za-z])',
    '\\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\\b',
    '\\b[a-z][a-z0-9]*(?:-[a-z0-9.]+)*-\\d[a-z0-9.-]*\\b',
    '\\b[a-z][a-z0-9-]*\\/[a-z0-9][a-z0-9.-]*\\b',
    '[$\\u20ac\\u00a3]\\s?\\d[\\d,.]*',
    '\\b[A-Z0-9]{1,2}-[A-Z0-9]{2,5}\\b',
    '\\b(?!(?:UTC|AI)\\b)(?=[A-Z0-9\\u2082]*[A-Z])[A-Z0-9\\u2082]{2,}\\b',
    '\\b\\d[\\d,.]*\\s?(?:nm|NM|kg|km|pkm|mi|ft|kt|lb|hPa)(?![A-Za-z])',
  ].join('|');
  const KEEP = new RegExp(SOURCE, 'g');
  const GAP = /^[\s\-–—/·:,.;()→⇄+*|]*$/;
  const KEPT_TAGS = new Set(['code', 'kbd', 'samp', 'var', 'pre', 'dt', 'th', 'textarea']);
  const KEPT_CLASSES = new Set(['brand', 'compact-brand', 'bn', 'bd', 'wordmark', 'nm', 'key-hint']);
  const WHOLE = /^(?:Home|Show|Apply)$/;
  const NOT_PROSE = 'script,style,svg,textarea,template,noscript,[translate=no],.notranslate,font';

  // The stretches of `text` to keep, as [start, end] pairs: neighbours joined when only punctuation or space lies
  // between them, and every space touching one taken in.
  function runs(text) {
    const found = [];
    KEEP.lastIndex = 0;
    for (let m = KEEP.exec(text); m; m = KEEP.exec(text)) {
      if (m.index > 0 && (text[m.index - 1] === '&' || text[m.index - 1] === '#')) continue;
      found.push([m.index, m.index + m[0].length]);
    }
    const out = [];
    for (const [s, e] of found) {
      const last = out[out.length - 1];
      if (last && GAP.test(text.slice(last[1], s))) last[1] = e; else out.push([s, e]);
    }
    for (const r of out) {
      while (r[0] > 0 && /\s/.test(text[r[0] - 1])) r[0]--;
      while (r[1] < text.length && /\s/.test(text[r[1]])) r[1]++;
    }
    return out.reduce((all, r) => {
      const last = all[all.length - 1];
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]); else all.push(r);
      return all;
    }, []);
  }

  // `runs` plus the spaces at either end of `text` when a kept element stands beside it ("<b>Spends</b> in <code>x</code>"
  // came back "Spends inx" until the space went into a mark of its own).
  function edges(text, before, after) {
    const r = runs(text);
    const lead = before ? (/^\s+/.exec(text) || [''])[0].length : 0;
    const trail = after ? (/\s+$/.exec(text) || [''])[0].length : 0;
    if (lead) r.unshift([0, lead]);
    if (trail && lead !== text.length) r.push([text.length - trail, text.length]);
    return r.sort((x, y) => x[0] - y[0]).reduce((all, x) => {
      const last = all[all.length - 1];
      if (last && x[0] <= last[1]) last[1] = Math.max(last[1], x[1]); else all.push(x);
      return all;
    }, []);
  }

  function allKept(text) {
    const t = text.trim();
    if (!t) return false;
    if (WHOLE.test(t)) return true;
    const r = runs(text);
    return r.length === 1 && text.slice(0, r[0][0]).trim() === '' && text.slice(r[0][1]).trim() === '';
  }

  // Marks the text under `node`, which a script just added.
  function keepTree(node) {
    const doc = node.ownerDocument || node;
    if (node.nodeType === 1) {
      for (const el of [node, ...node.querySelectorAll('*')]) {
        if (el.hasAttribute('translate')) continue;
        if (KEPT_TAGS.has(el.localName) || [...el.classList].some(c => KEPT_CLASSES.has(c))) el.setAttribute('translate', 'no');
      }
    }
    const texts = [];
    if (node.nodeType === 3) texts.push(node);
    else for (const w = doc.createTreeWalker(node, 4); w.nextNode();) texts.push(w.currentNode);
    const kept = n => n && n.nodeType === 1 && n.getAttribute('translate') === 'no';
    // First the elements that hold only a kept word, so the spaces beside them are known; then the text around.
    for (const n of texts) {
      const el = n.parentElement;
      if (el && !el.closest(NOT_PROSE) && (n.nodeValue || '').trim() && allKept(n.nodeValue) && el.childNodes.length === 1) el.setAttribute('translate', 'no');
    }
    for (const n of texts) {
      const el = n.parentElement;
      const text = n.nodeValue || '';
      if (!el || !text.trim() || el.closest(NOT_PROSE) || kept(el)) continue;
      const r = edges(text, kept(n.previousSibling), kept(n.nextSibling));
      if (!r.length) continue;
      const frag = doc.createDocumentFragment();
      let from = 0;
      for (const [s, e] of r) {
        if (s > from) frag.append(text.slice(from, s));
        const span = doc.createElement('span');
        span.setAttribute('translate', 'no');
        span.textContent = text.slice(s, e);
        frag.append(span);
        from = e;
      }
      if (from < text.length) frag.append(text.slice(from));
      n.replaceWith(frag);
    }
  }

  // Keeps watching the page: whatever its script adds is marked in the same task, before a translator reads it.
  function watch(doc) {
    const pending = new Set();
    let queued = false;
    const flush = () => {
      queued = false;
      for (const n of [...pending]) if (n.isConnected) keepTree(n);
      pending.clear();
    };
    new MutationObserver(records => {
      for (const r of records) for (const n of r.addedNodes) if ((n.nodeType === 1 || n.nodeType === 3) && n.localName !== 'font') pending.add(n);
      if (pending.size && !queued) { queued = true; queueMicrotask(flush); }
    }).observe(doc.body, {childList: true, subtree: true});
  }

  const api = {runs, edges, allKept, keepTree, watch};
  if (typeof document !== 'undefined' && document.body && typeof MutationObserver === 'function') watch(document);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.CastorKeep = api;
})(typeof window !== 'undefined' ? window : globalThis);
