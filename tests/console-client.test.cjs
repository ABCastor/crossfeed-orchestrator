// The console page's script, tested without a browser: the pure parts directly, and boot() against
// a small document built from the same markup shape console.py renders. Run: node --test tests/
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {Crossfeed, boot} = require('../scripts/console-assets/console.js');

// ---- a small document: enough selector support for what console.js asks of the page ----------
function parseSelector(text) {
  return text.split(',').map(one => one.trim().split(/\s+/).map(compound => {
    const parts = {tag: null, id: null, classes: [], attrs: [], checked: compound.includes(':checked'), enabled: compound.includes(':not(:disabled)')};
    const re = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|([^\]]*)))?\]/g;
    let m;
    while ((m = re.exec(compound.replace(/:checked|:not\(:disabled\)/g, '')))) {
      if (m[1]) parts.tag = m[1].toLowerCase();
      else if (m[2]) parts.id = m[2];
      else if (m[3]) parts.classes.push(m[3]);
      else parts.attrs.push([m[4], m[5] ?? m[6]]);
    }
    return parts;
  }));
}
function matchesCompound(el, c) {
  if (c.checked && !el.checked) return false;
  if (c.enabled && el.disabled) return false;
  if (c.tag && el.tagName !== c.tag) return false;
  if (c.id && el.getAttribute('id') !== c.id) return false;
  if (!c.classes.every(name => el.classList.contains(name))) return false;
  return c.attrs.every(([name, value]) => el.hasAttribute(name) && (value === undefined || el.getAttribute(name) === value));
}
function matches(el, selector) {
  return parseSelector(selector).some(chain => {
    if (!matchesCompound(el, chain[chain.length - 1])) return false;
    let at = el.parentElement;
    for (let i = chain.length - 2; i >= 0; i--) {
      while (at && !matchesCompound(at, chain[i])) at = at.parentElement;
      if (!at) return false;
      at = at.parentElement;
    }
    return true;
  });
}
class El {
  constructor(tag, attrs = {}, children = []) {
    this.tagName = tag; this.attrs = {}; this.children = []; this.parentElement = null; this.listeners = {};
    this.textValue = ''; this.value = attrs.value ?? '';
    for (const [k, v] of Object.entries(attrs)) this.setAttribute(k, v);
    for (const child of children) this.append(child);
  }
  get classList() {
    const el = this;
    const list = () => (el.attrs.class || '').split(/\s+/).filter(Boolean);
    return {contains: n => list().includes(n), add: n => { if (!list().includes(n)) el.attrs.class = [...list(), n].join(' '); },
      remove: n => { el.attrs.class = list().filter(x => x !== n).join(' '); },
      toggle: (n, on) => { const has = list().includes(n); const want = on === undefined ? !has : on;
        el.attrs.class = want ? [...new Set([...list(), n])].join(' ') : list().filter(x => x !== n).join(' '); }};
  }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get id() { return this.attrs.id || ''; }
  get dataset() {
    const el = this;
    return new Proxy({}, {get: (_, k) => el.attrs['data-' + String(k).replace(/[A-Z]/g, c => '-' + c.toLowerCase())],
      set: (_, k, v) => { el.attrs['data-' + String(k).replace(/[A-Z]/g, c => '-' + c.toLowerCase())] = String(v); return true; }});
  }
  get hidden() { return 'hidden' in this.attrs; }
  set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  get open() { return 'open' in this.attrs; }
  set open(v) { if (v) this.attrs.open = ''; else delete this.attrs.open; }
  get disabled() { return 'disabled' in this.attrs; }
  set disabled(v) { if (v) this.attrs.disabled = ''; else delete this.attrs.disabled; }
  get name() { return this.attrs.name; }
  get action() { return this.attrs.action; }
  get title() { return this.attrs.title || ''; }
  get textContent() { return this.textValue + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this.children = []; this.textValue = String(v); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'value') this.value = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  append(...kids) { for (const kid of kids) { kid.remove(); kid.parentElement = this; this.children.push(kid); } }
  replaceChildren() { this.children = []; }
  remove() { const parent = this.parentElement; if (parent) {
    if (this.ownerDocument && (this.ownerDocument.activeElement === this || this.all().includes(this.ownerDocument.activeElement)))
      this.ownerDocument.activeElement = this.ownerDocument.body;
    parent.children = parent.children.filter(c => c !== this);
  } }
  all() { return this.children.flatMap(c => [c, ...c.all()]); }
  querySelectorAll(s) { return this.all().filter(el => matches(el, s)); }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  matches(s) { return matches(this, s); }
  closest(s) { for (let at = this; at; at = at.parentElement) if (at.matches?.(s)) return at; return null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) { for (const fn of this.listeners[type] || []) fn({target: this, preventDefault() {}, ...event}); }
  click() { this.dispatch('click'); if (this.attrs.type === 'submit') this.closest('form')?.submit(this); }
  setCustomValidity(message) { this.validationMessage = message; }
  focus(options) { this.focusOptions = options; this.ownerDocument.activeElement = this; }
  select() { this.focus(); }
  scrollIntoView() { this.scrolled = true; }
  submit(submitter) { this.ownerDocument.dispatch('submit', {target: this, submitter}); }
  requestSubmit(submitter) { this.submit(submitter); }
}
const h = (tag, attrs, ...children) => new El(tag, attrs || {}, children);

function page() {
  const stops = ['off', 'low', 'normal', 'high', 'forced'].map(level => h('button', {
    type: 'submit', name: 'level', value: level, class: `stop ${level}${level === 'normal' ? ' on' : ''}`,
    role: 'radio', 'aria-checked': String(level === 'normal'), title: `means ${level}`}));
  const words = (tag, cls, text) => { const el = h(tag, {class: cls}); el.textContent = text; return el; };
  const sw = (key, name) => h('button', {class: 'sw', type: 'submit', role: 'switch', 'aria-checked': 'true', name: 'switch',
    value: `${key}=off`, 'data-key': key, 'data-name': name}, h('span', {class: 'track'}), words('span', 'st', 'On'));
  const opt = (id, name, key, older) => h('li', {class: `opt${older ? ' older' : ''} on`, id, 'data-search-label': name, 'data-model': key},
    h('div', {class: 'row'}, h('button', {class: 'order-handle', type: 'button'}), h('div', {class: 'txt'}, words('span', 'nm', name)), sw(key, name)));
  const astra = opt('model-astra', 'GPT-6 Astra', 'gpt-6-astra');
  const sol = opt('model-sol', 'GPT-6.1 Sol', 'gpt-6.1-sol');
  const luna = opt('model-luna', 'GPT-6 Luna', 'gpt-6-luna');
  const oldOne = opt('model-old', 'GPT-5.6 Luna', 'gpt-5.6-luna', true);
  const older = h('details', {class: 'older'}, words('span', 'count', '1'), h('ul', {class: 'opts'}, oldOne));
  const now = h('span', {class: 'now all'}, h('b', {class: 'v'}), h('span', {class: 'n'}));
  now.querySelector('.v').textContent = 'All enabled'; now.querySelector('.n').textContent = 'a task that names none gets GPT-6 Astra';
  const allOn = h('button', {class: 'all-on', type: 'submit', name: 'model', value: 'auto', hidden: ''});
  const pick = h('details', {class: 'pick', id: 'pick-codex', 'data-pool': 'codex', 'data-note-all': 'a task that names none gets GPT-6 Astra',
    'data-note-some': 'a task that asks for an off model gets the nearest one that is on', 'data-note-one': 'every run on this provider uses it',
    'data-note-none': 'Codex is off until you switch one on', 'data-note-only': 'the only model this plan offers',
    'data-note-empty': 'the roster lists no models here yet'},
    h('summary', {class: 'pick-line'}, now), h('select', {'data-sort': 'models', 'data-pool': 'codex', value: 'your'}),
    h('form', {class: 'choose', action: '/model'}, h('input', {name: 'pool', value: 'codex'}), h('input', {name: 't', value: 'tok'}),
      h('div', {class: 'head'}, allOn), h('ul', {class: 'opts'}, astra, sol, luna), older));
  const pool = h('article', {class: 'pool lvl-normal', id: 'pool-codex', 'data-pool': 'codex', 'data-search-label': 'Codex (ChatGPT)'},
    h('button', {class: 'order-handle', type: 'button'}),
    h('div', {class: 'gauge'}),
    h('form', {class: 'lv at-2 is-normal', action: '/level'}, h('input', {name: 'pool', value: 'codex'}), h('input', {name: 't', value: 'tok'}), ...stops),
    h('p', {class: 'means'}), pick);
  const lens = h('button', {class: 'find-lens', type: 'button', 'aria-expanded': 'false'});
  const input = h('input', {id: 'global-search-input', type: 'search'});
  const panel = h('div', {class: 'find-panel', id: 'find', hidden: ''}, input, h('kbd', {class: 'key-hint'}),
    h('p', {class: 'search-status'}), h('ul', {id: 'search-results'}));
  const other = h('button', {class: 'elsewhere', type: 'button'});
  const keys = ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna', 'gpt-5.6-luna'];
  const order = {pools: ['codex'], models: {codex: keys}, sort: {pools: 'name', models: {codex: 'your'}},
    ranks: {pools: {your: ['codex'], name: ['codex'], quota: ['codex'], reset: ['codex']},
      models: {codex: {your: keys, name: [...keys].reverse(), quality: keys, cheapest: [...keys].reverse()}}}};
  const body = h('body', {}, h('header', {class: 'mast'}, lens, h('button', {'data-theme-toggle': '', type: 'button'}), panel),
    h('main', {}, h('p', {class: 'stamp'}), h('div', {class: 'order-state', 'data-order': JSON.stringify(order), 'data-token': 'tok'}),
      h('select', {'data-sort': 'pools', value: 'name'}), h('section', {class: 'pools'}, pool), h('p', {class: 'save-status'}),
      h('section', {class: 'agents'}, h('div', {class: 'brief'}, h('button', {class: 'brief-toggle', type: 'button'}),
        h('button', {class: 'copy', type: 'button'}, h('span', {class: 'cw'})), h('pre'), h('pre', {class: 'brief-full', hidden: ''}))), other));
  const document = h('html', {}, body);
  document.documentElement = document; document.body = body; document.hidden = false;
  document.activeElement = body;
  document.getElementById = id => document.querySelectorAll(`#${id}`)[0] || null;
  document.createElement = tag => { const el = h(tag); el.ownerDocument = document; return el; };
  for (const el of document.all()) el.ownerDocument = document;
  document.ownerDocument = document;
  return {document, lens, input, panel, other, pick, now, astra, sol, luna, oldOne, older, allOn, stops};
}

function start({fetchImpl, prepare, platform = 'MacIntel', clipboard, motion = false, reduced = false, legacyCopy = false, configureWindow} = {}) {
  const env = page();
  prepare?.(env);
  env.document.createTextNode = text => { const node = h('text'); node.textContent = text; return node; };
  for (const el of env.document.all()) el.ownerDocument = env.document;
  const requests = [];
  const timers = [];
  const animations = [];
  const media = {matches: reduced, listeners: [], addEventListener(type, fn) { this.listeners.push(fn); },
    change(value) { this.matches = value; for (const fn of this.listeners) fn({matches: value}); }};
  if (motion) {
    const poolList = env.document.querySelector('.pools');
    const second = h('article', {class: 'pool lvl-normal', id: 'pool-extra', 'data-pool': 'extra', 'data-search-label': 'Extra'},
      h('button', {class: 'order-handle', type: 'button'}));
    poolList.append(second);
    const orderState = env.document.querySelector('.order-state'), order = JSON.parse(orderState.dataset.order);
    order.pools.push('extra');
    for (const key of Object.keys(order.ranks.pools)) order.ranks.pools[key] = ['codex', 'extra'];
    order.ranks.pools.name = ['extra', 'codex'];
    orderState.dataset.order = JSON.stringify(order);
    for (const el of env.document.all()) el.ownerDocument = env.document;
    for (const el of env.document.querySelectorAll('.pool, .opt')) {
      el.style = {};
      el.getBoundingClientRect = () => {
        const pool = el.closest('.pool'), poolTop = [...poolList.children].indexOf(pool) * 300;
        const rowIndex = el.matches('.opt') ? [...pool.querySelectorAll('.opt')].indexOf(el) : -1;
        return {left: 0, right: 600, top: poolTop + (rowIndex < 0 ? 0 : 50 + rowIndex * 30) - window.scrollY,
          height: rowIndex < 0 ? 300 : 30};
      };
      el.animate = (frames, options) => {
        const animation = {el, frames, options, cancelled: false, finished: false,
          cancel() { this.cancelled = true; this.oncancel?.(); },
          finish() { this.finished = true; this.onfinish?.(); }};
        animations.push(animation); return animation;
      };
    }
  }
  if (legacyCopy) env.document.execCommand = () => true;
  const windowListeners = {};
  const window = {
    scrollY: 0, location: {hash: ''}, navigator: {platform, clipboard},
    localStorage: {getItem: () => null, setItem() {}},
    getComputedStyle: () => ({top: '-16px'}), addEventListener(type, fn) { (windowListeners[type] ||= []).push(fn); }, requestAnimationFrame: fn => fn(),
    setTimeout: fn => timers.push(fn), clearTimeout() {}, setInterval() {},
    matchMedia: () => media,
    fetch: fetchImpl || ((url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject}))),
  };
  configureWindow?.(window, env);
  global.FormData = class { constructor(form) { return form.querySelectorAll('input, textarea, select').filter(i => i.name && !i.disabled).map(i => [i.name, i.value]); } };
  const api = boot(env.document, window);
  return {...env, api, window, requests, timers, animations, media, windowListeners};
}
const reply = (request, data) => request.resolve({ok: true, json: async () => data});
const flush = () => new Promise(resolve => setImmediate(resolve));

// ---- pure parts ----------------------------------------------------------------------------------
test('slash opens search only when not typing; Cmd-K and Ctrl-K always', () => {
  const field = {matches: s => s.includes('input')}, page = {matches: () => false};
  assert.equal(Crossfeed.searchShortcut({key: '/', target: page}), '/');
  assert.equal(Crossfeed.searchShortcut({key: '/', target: field}), null);
  assert.equal(Crossfeed.searchShortcut({key: 'k', metaKey: true, target: field}), 'k');
  assert.equal(Crossfeed.searchShortcut({key: 'K', ctrlKey: true, target: page}), 'k');
  assert.equal(Crossfeed.searchShortcut({key: 'k', target: page}), null);
  assert.equal(Crossfeed.searchShortcut({key: 'k', metaKey: true, shiftKey: true, target: page}), null);
});

test('the key hint in the search box names the platform key', () => {
  assert.equal(Crossfeed.keyHint({platform: 'MacIntel'}), '⌘K');
  assert.equal(Crossfeed.keyHint({userAgentData: {platform: 'Windows'}}), 'Ctrl K');
  assert.equal(Crossfeed.keyHint({platform: 'Linux x86_64'}), 'Ctrl K');
});

test('saves run one at a time, the newest click wins, and a stale failure changes nothing', async () => {
  const sent = [], confirmed = [], failed = [];
  const answers = [];
  const saver = Crossfeed.createSaver({
    send: request => new Promise((resolve, reject) => { sent.push(request); answers.push({resolve, reject}); }),
    onConfirmed: (data, key, stillPending) => confirmed.push([data, key, stillPending]),
    onFailed: (key, newer) => failed.push([key, newer]),
  });
  const first = saver.save('model:codex', 'sol');
  const second = saver.save('model:codex', 'luna');
  await flush();
  assert.deepEqual(sent, ['sol']);             // the second waits for the first
  assert.equal(saver.pending('model:codex'), true);
  answers[0].reject(new Error('down'));
  await first;
  assert.deepEqual(failed, [['model:codex', true]]);   // a newer click exists: nothing is put back
  await flush();
  assert.deepEqual(sent, ['sol', 'luna']);
  answers[1].resolve('ok');
  await second;
  assert.deepEqual(confirmed, [['ok', 'model:codex', false]]);
  assert.equal(saver.busy(), false);
});

// ---- the page ------------------------------------------------------------------------------------
const sw = (f, key) => f.document.querySelector(`.sw[data-key="${key}"]`);
const view = f => [f.now.className, f.now.querySelector('.v').textContent, f.now.querySelector('.n').textContent];

test('the switches add up to the same words the server writes', () => {
  const notes = {all: 'A', some: 'S', one: 'O', none: 'N', only: 'Y', empty: 'E'};
  const models = on => on.map((flag, i) => ({name: `M${i}`, on: flag}));
  assert.deepEqual(Crossfeed.switchView(models([true, true, true]), notes), {state: 'all', label: 'All enabled', note: 'A'});
  assert.deepEqual(Crossfeed.switchView(models([true, false, true]), notes), {state: 'some', label: '2 of 3 on', note: 'S'});
  assert.deepEqual(Crossfeed.switchView(models([false, true, false]), notes), {state: 'one', label: 'Only M1 on', note: 'O'});
  assert.deepEqual(Crossfeed.switchView(models([false, false]), notes), {state: 'none', label: 'None on', note: 'N'});
  assert.deepEqual(Crossfeed.switchView(models([true]), notes), {state: 'only', label: 'M0', note: 'Y'});
  assert.deepEqual(Crossfeed.switchView(models([false]), notes), {state: 'none', label: 'None on', note: 'N'});
  assert.deepEqual(Crossfeed.switchView([], notes), {state: 'auto', label: 'Crossfeed decides', note: 'E'});
  assert.deepEqual(Crossfeed.splitChange('gpt-6.1-sol=off'), ['gpt-6.1-sol', 'off']);
  assert.deepEqual(Crossfeed.splitChange('a=b=on'), ['a=b', 'on']);   // the state is what follows the last =
});

test('a switch flips at once, the line says what they add up to, and the server confirms in place', async () => {
  const f = start();
  assert.deepEqual(view(f), ['now all', 'All enabled', 'a task that names none gets GPT-6 Astra']);
  const astra = sw(f, 'gpt-6-astra');
  astra.click();
  assert.equal(astra.getAttribute('aria-checked'), 'false');                             // before any answer
  assert.equal(astra.getAttribute('value'), 'gpt-6-astra=on');                            // the next click sets the opposite
  assert.equal(astra.querySelector('.st').textContent, 'Off');
  assert.match(f.astra.className, /\boff\b/);
  assert.deepEqual(view(f), ['now some', '2 of 3 on', 'a task that asks for an off model gets the nearest one that is on']);   // the older model is not counted
  assert.equal(f.allOn.hidden, false);                                                    // a way back to all on
  await flush();
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].url, '/model');
  assert.equal(f.requests[0].options.body.get('switch'), 'gpt-6-astra=off');
  assert.equal(f.requests[0].options.body.get('pool'), 'codex');
  reply(f.requests[0], {pools: [{pool: 'codex', level: 'normal', means: 'Normal.', gauge: 'g', on: ['gpt-6.1-sol', 'gpt-6-luna', 'gpt-5.6-luna'],
    state: 'some', label: '2 of 3 on', note: "the server's words"}], brief: 'brief', stamp: 'stamp'});
  await flush(); await flush();
  assert.match(f.document.querySelector('.save-status').textContent, /Saved/);
  assert.equal(f.now.querySelector('.n').textContent, "the server's words");
  assert.equal(f.document.querySelector('.agents pre').textContent, 'brief');
});

test('one on, none on and switch all on read right, and an older model is never counted', async () => {
  const f = start();
  sw(f, 'gpt-6-astra').click(); sw(f, 'gpt-6.1-sol').click();
  assert.deepEqual(view(f).slice(0, 2), ['now one', 'Only GPT-6 Luna on']);
  assert.equal(f.older.querySelector('.count').textContent, '1');                         // its one model is on
  sw(f, 'gpt-6-luna').click();
  // the older model is still on, and still does not count: no current model is on
  assert.deepEqual(view(f), ['now none', 'None on', 'Codex is off until you switch one on']);
  assert.equal(sw(f, 'gpt-5.6-luna').getAttribute('aria-checked'), 'true');
  assert.equal(f.allOn.hidden, false);
  sw(f, 'gpt-5.6-luna').click();
  assert.equal(f.older.querySelector('.count').textContent, '1 · 0 on');
  await flush();
  assert.deepEqual(f.requests.map(r => r.options.body.get('switch')), ['gpt-6-astra=off']);   // saves go one at a time
  f.allOn.click();
  assert.deepEqual(view(f).slice(0, 2), ['now all', 'All enabled']);
  assert.equal(f.allOn.hidden, true);
  assert.equal(sw(f, 'gpt-6.1-sol').getAttribute('aria-checked'), 'true');
  assert.equal(sw(f, 'gpt-5.6-luna').getAttribute('aria-checked'), 'false');              // all on is the current list: an older model stays as set
  assert.equal(f.older.querySelector('.count').textContent, '1 · 0 on');
  // an older model off leaves the line saying all on, and no "Switch all on" that would change nothing you see
  const h2 = start();
  sw(h2, 'gpt-5.6-luna').click();
  assert.deepEqual(view(h2).slice(0, 2), ['now all', 'All enabled']);
  assert.equal(h2.allOn.hidden, true);
  // two quick clicks on one switch send two different states, in order: off, then on
  const g = start();
  sw(g, 'gpt-6.1-sol').click(); sw(g, 'gpt-6.1-sol').click();
  assert.equal(sw(g, 'gpt-6.1-sol').getAttribute('aria-checked'), 'true');
  await flush();
  reply(g.requests[0], {pools: [{pool: 'codex', level: 'normal', means: 'm', gauge: 'g',
    on: ['gpt-6-astra', 'gpt-6-luna', 'gpt-5.6-luna'], state: 'some', label: '2 of 3 on', note: 'n'}], brief: 'b', stamp: 's'});
  await flush(); await flush();
  assert.equal(sw(g, 'gpt-6.1-sol').getAttribute('aria-checked'), 'true');                  // the older answer does not undo the newer click
  assert.deepEqual(g.requests.map(r => r.options.body.get('switch')), ['gpt-6.1-sol=off', 'gpt-6.1-sol=on']);
});

test('a failed switch save puts back what is still in force and says so', async () => {
  const f = start();
  sw(f, 'gpt-6-astra').click();
  await flush();
  f.requests[0].resolve({ok: false});
  await flush(); await flush();
  assert.equal(sw(f, 'gpt-6-astra').getAttribute('aria-checked'), 'true');
  assert.deepEqual(view(f).slice(0, 2), ['now all', 'All enabled']);
  assert.match(f.document.querySelector('.save-status').textContent, /still in force/);
});

test('a level click moves the slider at once', async () => {
  const f = start();
  f.stops[3].click();
  assert.equal(f.stops[3].getAttribute('aria-checked'), 'true');
  assert.equal(f.stops[2].getAttribute('aria-checked'), 'false');
  assert.equal(f.document.getElementById('pool-codex').className, 'pool lvl-high');
  assert.equal(f.document.querySelector('.lv').className, 'lv at-3 is-high');
  assert.equal(f.document.querySelector('.means').textContent, 'means high');
  assert.deepEqual(f.stops.map(stop => stop.tabIndex), [-1, -1, -1, 0, -1]);   // one stop in the tab order
  await flush();
  assert.equal(f.requests[0].options.body.get('level'), 'high');
});

test('the slider is one radio group: the arrow keys move it and save', async () => {
  const f = start();
  assert.deepEqual(f.stops.map(stop => stop.tabIndex), [-1, -1, 0, -1, -1]);
  const form = f.document.querySelector('.lv');
  form.dispatch('keydown', {key: 'ArrowLeft'});
  assert.equal(f.document.querySelector('.lv').className, 'lv at-1 is-low');
  assert.equal(f.document.activeElement, f.stops[1]);
  form.dispatch('keydown', {key: 'End'});
  assert.equal(f.document.querySelector('.lv').className, 'lv at-4 is-forced');
  form.dispatch('keydown', {key: 'Tab'});                            // any other key is left alone
  assert.equal(f.document.querySelector('.lv').className, 'lv at-4 is-forced');
  await flush();
  assert.equal(f.requests[0].options.body.get('level'), 'low');
});

test('copy puts what agents read on the clipboard and says Copied, then goes back', async () => {
  const copied = [];
  const f = start({clipboard: {writeText: async text => { copied.push(text); }}});
  f.document.querySelector('.agents pre').textContent = 'the brief';
  const button = f.document.querySelector('.copy');
  button.dispatch('click');
  await flush();
  assert.deepEqual(copied, ['the brief']);
  assert.equal(button.classList.contains('done'), true);
  assert.equal(button.querySelector('.cw').textContent, 'Copied');
  assert.equal(button.getAttribute('aria-label'), 'Copied what agents read');
  f.timers.at(-1)();                                                   // the "Copied" state times out
  assert.equal(button.classList.contains('done'), false);
  assert.equal(button.querySelector('.cw').textContent, 'Copy');
  const g = start({clipboard: {writeText: async () => { throw new Error('denied'); }}});
  g.document.querySelector('.copy').dispatch('click');
  await flush();
  assert.equal(g.document.querySelector('.copy').classList.contains('done'), false);   // no false "Copied"
  assert.match(g.document.querySelector('.save-status').textContent, /Could not copy/);
});

test('slash, Cmd-K and Escape: search opens from anywhere and gives focus back', () => {
  const f = start();
  assert.equal(f.document.querySelector('.key-hint').textContent, '⌘K');
  f.other.focus();
  f.document.dispatch('keydown', {key: '/', target: f.other});
  assert.equal(f.panel.hidden, false);
  assert.equal(f.document.activeElement, f.input);
  assert.equal(f.lens.getAttribute('aria-expanded'), 'true');
  f.document.dispatch('keydown', {key: 'Escape', target: f.input});
  assert.equal(f.panel.hidden, true);
  assert.equal(f.document.activeElement, f.other);                  // focus goes back where it was
  f.document.dispatch('keydown', {key: '/', target: f.input});      // typing a slash stays typing
  assert.equal(f.panel.hidden, true);
  f.document.dispatch('keydown', {key: 'k', metaKey: true, target: f.input});
  assert.equal(f.panel.hidden, false);
  const g = start({platform: 'Win32'});
  assert.equal(g.document.querySelector('.key-hint').textContent, 'Ctrl K');
  g.document.dispatch('keydown', {key: 'k', ctrlKey: true, target: g.other});
  assert.equal(g.panel.hidden, false);
});

test('search finds older models by name or id and opens every fold on the way', () => {
  const f = start();
  f.document.dispatch('keydown', {key: '/', target: f.other});
  f.input.value = '5.6 luna';
  f.input.dispatch('input');
  const results = f.document.querySelector('#search-results');
  assert.equal(results.children.length, 1);
  const link = results.children[0].children[0];
  assert.equal(link.textContent, 'GPT-5.6 Luna · Codex (ChatGPT)');
  link.dispatch('click');
  assert.equal(f.older.open, true);
  assert.equal(f.pick.open, true);
  assert.equal(f.panel.hidden, true);
  const target = f.document.getElementById('model-old');
  assert.equal(target.scrolled, true);
  assert.equal(f.document.activeElement, target);
  f.input.value = 'gpt-6-astra';
  f.input.dispatch('input');
  assert.equal(results.children.length, 1);
});

const modelOrder = f => [...f.pick.querySelectorAll('.opts')[0].children].map(row => row.dataset.model);
const handleOf = row => row.querySelector('.order-handle');
test('keyboard reorders current models, saves on Enter, and cannot pull older models out of their section', async () => {
  const f = start(), handle = handleOf(f.sol);
  handle.dispatch('keydown', {key: 'ArrowUp'});
  assert.deepEqual(modelOrder(f), ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna']);
  assert.equal(f.requests.length, 0);
  assert.equal(f.document.activeElement, handle);
  assert.match(f.document.querySelector('.save-status').textContent, /moved to 1 of 3/);
  assert.equal(f.older.hidden, false);
  handle.dispatch('keydown', {key: 'Enter'});
  await flush();
  const sent = JSON.parse(f.requests[0].options.body.get('order'));
  assert.deepEqual(sent.models.codex, [...modelOrder(f), 'gpt-5.6-luna']);
  handleOf(f.oldOne).dispatch('keydown', {key: 'ArrowUp'});
  handleOf(f.oldOne).dispatch('keydown', {key: 'Enter'});
  await flush();
  assert.equal(f.oldOne.parentElement, f.older.querySelector('.opts'));
  assert.equal(f.requests.length, 1);
  sw(f, 'gpt-5.6-luna').click();
  assert.deepEqual(view(f).slice(0, 2), ['now all', 'All enabled']);
  sw(f, 'gpt-6-astra').click(); f.allOn.click();
  assert.equal(sw(f, 'gpt-5.6-luna').getAttribute('aria-checked'), 'false');
});

test('Escape restores criterion and original groups; refresh does not undo a draft', async () => {
  const f = start();
  const initial = JSON.parse(f.document.querySelector('.order-state').dataset.order);
  const select = f.pick.querySelector('[data-sort]');
  select.value = 'name'; select.dispatch('change'); await flush();
  const handle = handleOf(f.luna);
  handle.dispatch('keydown', {key: 'ArrowDown'});
  const draft = modelOrder(f);
  f.api.update({console_order: initial});
  assert.deepEqual(modelOrder(f), draft);
  handle.dispatch('keydown', {key: 'Escape'});
  assert.equal(select.value, 'name');
  assert.deepEqual(modelOrder(f), initial.ranks.models.codex.name.filter(key => key !== 'gpt-5.6-luna'));
  assert.equal(f.oldOne.parentElement, f.older.querySelector('.opts'));
  assert.equal(f.requests.length, 1); // Escape has no POST
  const g = start();
  handleOf(g.oldOne).dispatch('keydown', {key: 'ArrowUp'});
  handleOf(g.oldOne).dispatch('keydown', {key: 'Escape'});
  assert.equal(g.oldOne.parentElement, g.older.querySelector('.opts'));
  assert.equal(g.older.hidden, false);
  await flush(); assert.equal(g.requests.length, 0);
});

test('failed order POST restores confirmed order, and pointer cancellation does not save', async () => {
  const f = start(), handle = handleOf(f.astra);
  handle.dispatch('keydown', {key: 'ArrowDown'}); handle.dispatch('keydown', {key: 'Enter'});
  await flush(); f.requests[0].resolve({ok: false}); await flush(); await flush();
  assert.deepEqual(modelOrder(f), ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna']);
  assert.match(f.document.querySelector('.save-status').textContent, /Could not save/);
  handle.dispatch('pointerdown', {pointerId: 8, button: 0});
  f.document.elementFromPoint = () => f.luna;
  f.luna.getBoundingClientRect = () => ({top: 0, height: 10});
  handle.dispatch('pointermove', {pointerId: 8, clientX: 0, clientY: 9});
  assert.deepEqual(modelOrder(f), ['gpt-6.1-sol', 'gpt-6-luna', 'gpt-6-astra']);
  handle.dispatch('pointercancel', {pointerId: 8});
  assert.deepEqual(modelOrder(f), ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna']);
  assert.equal(f.requests.length, 1);
});

test('Show full and Copy follow the visible live brief, then return to compact', async () => {
  const copied = [], f = start({clipboard: {writeText: async text => copied.push(text)}});
  f.api.update({brief: 'compact one', brief_full: 'full one'});
  const toggle = f.document.querySelector('.brief-toggle');
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  f.api.update({brief: 'compact two', brief_full: 'full two'});
  f.document.querySelector('.copy').click(); await flush();
  assert.deepEqual(copied, ['full two']);
  toggle.click();
  assert.equal(f.document.querySelector('.agents pre').textContent, 'compact two');
  f.document.querySelector('.copy').click(); await flush();
  assert.deepEqual(copied, ['full two', 'compact two']);
});

test('refresh and order confirmation preserve focused controls', async () => {
  const f = start(), handle = handleOf(f.astra);
  handle.focus();
  const order = JSON.parse(f.document.querySelector('.order-state').dataset.order);
  f.api.update({console_order: order});
  assert.equal(f.document.activeElement, handle);
  handle.dispatch('keydown', {key: 'ArrowDown'}); handle.dispatch('keydown', {key: 'Enter'});
  await flush();
  const saved = JSON.parse(f.requests[0].options.body.get('order'));
  order.models = saved.models; order.sort = saved.sort;
  order.ranks.models.codex.your = [...saved.models.codex]; order.flat = {codex: true};
  reply(f.requests[0], {console_order: order}); await flush(); await flush();
  assert.equal(f.document.activeElement, handle);
});

test('touch pointer commits a group order and lost capture cancels a later drag', async () => {
  const f = start(), handle = handleOf(f.sol), captures = [];
  handle.setPointerCapture = pointer => captures.push([pointer, modelOrder(f)]);
  handle.hasPointerCapture = () => true;
  f.document.elementFromPoint = () => f.astra;
  f.astra.getBoundingClientRect = () => ({top: 0, height: 10});
  handle.dispatch('pointerdown', {pointerId: 12, pointerType: 'touch', button: 0});
  handle.dispatch('pointermove', {pointerId: 12, clientX: 0, clientY: 1});
  assert.deepEqual(captures.at(-1), [12, ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna']]);
  handle.dispatch('lostpointercapture');
  handle.dispatch('pointerup', {pointerId: 12}); await flush();
  const order = JSON.parse(f.requests[0].options.body.get('order'));
  assert.deepEqual(order.models.codex, ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna', 'gpt-5.6-luna']);
  assert.equal(f.oldOne.parentElement, f.older.querySelector('.opts'));
  handle.dispatch('pointerdown', {pointerId: 13, button: 0});
  handle.dispatch('keydown', {key: 'ArrowDown'});
  handle.hasPointerCapture = () => false;
  handle.dispatch('lostpointercapture');
  assert.deepEqual(modelOrder(f), order.models.codex.slice(0, 3));
  assert.equal(f.requests.length, 1);
});

test('sort changes animate cards and model rows, cancel interrupted effects, and leave no inline transforms', async () => {
  const f = start({motion: true});
  assert.equal(f.animations.length, 0); // initial render is already in its final order
  const poolSort = f.document.querySelector('[data-sort="pools"]');
  poolSort.value = 'your'; poolSort.dispatch('change');
  assert.equal(f.animations.filter(a => a.el.matches('.pool')).length, 2);
  assert.equal(f.animations.filter(a => a.el.matches('.opt')).length, 0); // inherit the card's movement
  const first = [...f.animations];
  const modelSort = f.pick.querySelector('[data-sort]');
  modelSort.value = 'name'; modelSort.dispatch('change');
  assert.ok(first.every(a => a.cancelled));
  assert.equal(f.animations.filter(a => a.el.matches('.opt')).length, 2); // two current rows swap; older stays folded
  for (const a of f.animations) {
    assert.equal(a.frames.at(-1).transform, 'none');
    assert.equal(a.options.duration, 220);
    assert.equal(a.options.fill, undefined);
    a.finish();
  }
  assert.ok(f.document.querySelectorAll('.pool, .opt').every(el => el.style.transform === undefined));
  await flush(); assert.equal(f.requests[0].url, '/order');
});

test('keyboard reorder animates both kinds of row and Escape removes the draft state without saving', async () => {
  const f = start({motion: true}), handle = handleOf(f.astra);
  handle.dispatch('keydown', {key: 'ArrowDown'});
  assert.equal(f.animations.filter(a => a.el.matches('.opt')).length, 2);
  assert.equal(f.astra.classList.contains('ordering'), true);
  handle.dispatch('keydown', {key: 'Escape'});
  assert.equal(f.astra.classList.contains('ordering'), false);
  assert.deepEqual(modelOrder(f), ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna']);
  const pool = f.document.getElementById('pool-codex'), poolHandle = handleOf(pool);
  poolHandle.dispatch('keydown', {key: 'ArrowUp'});
  assert.ok(f.animations.some(a => a.el.matches('.pool')));
  poolHandle.dispatch('keydown', {key: 'Escape'});
  await flush(); assert.equal(f.requests.length, 0);
});

test('drag shows an out-of-flow insertion edge, animates a drop, and cancellation clears all draft markers', async () => {
  const f = start({motion: true}), handle = handleOf(f.astra);
  f.document.elementFromPoint = () => f.luna;
  const target = f.luna.getBoundingClientRect();
  handle.dispatch('pointerdown', {pointerId: 1, button: 0});
  handle.dispatch('pointermove', {pointerId: 1, clientX: 0, clientY: target.top + target.height});
  assert.equal(f.luna.classList.contains('drop-after'), true);
  assert.ok(f.animations.some(a => a.el === f.astra));
  const settled = modelOrder(f), count = f.animations.length;
  handle.dispatch('pointermove', {pointerId: 1, clientX: 0, clientY: target.top + target.height});
  assert.deepEqual(modelOrder(f), settled);
  assert.equal(f.animations.length, count); // stationary pointer does not chase transformed targets
  assert.equal(f.luna.classList.contains('drop-after'), true);
  handle.dispatch('pointerup', {pointerId: 1});
  assert.equal(f.luna.classList.contains('drop-after'), false);
  assert.equal(f.astra.classList.contains('ordering'), false);
  await flush(); assert.equal(f.requests.length, 1);
  const saved = modelOrder(f);
  f.document.elementFromPoint = () => f.sol;
  const above = f.sol.getBoundingClientRect();
  handle.dispatch('pointerdown', {pointerId: 2, button: 0});
  handle.dispatch('pointermove', {pointerId: 2, clientX: 0, clientY: above.top});
  assert.equal(f.sol.classList.contains('drop-before'), true);
  handle.dispatch('pointercancel', {pointerId: 2});
  assert.deepEqual(modelOrder(f), saved);
  assert.equal(f.sol.classList.contains('drop-before'), false);
  assert.equal(f.astra.classList.contains('ordering'), false);
  for (const animation of f.animations) animation.finish();
  assert.ok(f.document.querySelectorAll('.pool, .opt').every(el => el.style.transform === undefined));
  assert.equal(f.requests.length, 1);
});

test('reduced motion is read on every reorder and a live preference change cancels movement immediately', () => {
  const f = start({motion: true, reduced: true}), handle = handleOf(f.astra);
  handle.dispatch('keydown', {key: 'ArrowDown'});
  assert.equal(f.animations.length, 0);
  assert.equal(f.astra.classList.contains('ordering'), true); // feedback remains visible
  f.media.change(false);
  handle.dispatch('keydown', {key: 'ArrowDown'});
  assert.ok(f.animations.length > 0);
  f.media.change(true);
  assert.ok(f.animations.every(animation => animation.cancelled));
  const count = f.animations.length;
  handle.dispatch('keydown', {key: 'Escape'});
  assert.equal(f.animations.length, count);
  assert.equal(f.astra.classList.contains('ordering'), false);
});

test('older active labels follow optimistic switches and failed saves', async () => {
  const f = start(), text = f.oldOne.querySelector('.txt');
  const badge = h('span', {class: 'older-active'}), note = h('span', {class: 'older-active-note'});
  text.append(badge, note);
  const summaryBadge = h('span', {class: 'older-active older-active-summary'});
  const summaryNote = h('span', {class: 'older-active-note older-active-summary-note'});
  f.older.append(summaryBadge, summaryNote);
  sw(f, 'gpt-5.6-luna').click();
  assert.equal(badge.hidden, true); assert.equal(note.hidden, true);
  assert.equal(summaryBadge.hidden, true); assert.equal(summaryNote.hidden, true);
  await flush(); f.requests[0].resolve({ok: false}); await flush(); await flush();
  assert.equal(badge.hidden, false); assert.equal(note.hidden, false);
  assert.equal(summaryBadge.hidden, false); assert.equal(summaryNote.hidden, false);
});

test('card pointer drag animates, keeps a stationary drop stable, and cancels without saving', async () => {
  const f = start({motion: true}), pool = f.document.getElementById('pool-codex');
  const target = f.document.getElementById('pool-extra'), handle = handleOf(pool);
  f.document.elementFromPoint = () => target;
  handle.dispatch('pointerdown', {pointerId: 3, button: 0});
  handle.dispatch('pointermove', {pointerId: 3, clientX: 1, clientY: 1});
  const list = f.document.querySelector('.pools');
  assert.deepEqual(list.children.map(el => el.dataset.pool), ['codex', 'extra']);
  assert.equal(target.classList.contains('drop-before'), true);
  assert.equal(f.animations.filter(a => a.el.matches('.pool')).length, 2);
  handle.dispatch('pointermove', {pointerId: 3, clientX: 1, clientY: 1});
  assert.deepEqual(list.children.map(el => el.dataset.pool), ['codex', 'extra']);
  f.api.showLevel('codex', 'high');
  assert.equal(pool.classList.contains('ordering'), true);
  handle.dispatch('pointercancel', {pointerId: 3});
  assert.deepEqual(list.children.map(el => el.dataset.pool), ['extra', 'codex']);
  assert.equal(target.classList.contains('drop-before'), false);
  await flush(); assert.equal(f.requests.length, 0);
});

test('Escape during pointer drag releases capture and later pointer events cannot revive the cancelled draft', async () => {
  const f = start({motion: true}), handle = handleOf(f.astra), released = [];
  handle.hasPointerCapture = () => true;
  handle.releasePointerCapture = id => released.push(id);
  f.document.elementFromPoint = () => f.luna;
  const rect = f.luna.getBoundingClientRect();
  handle.dispatch('pointerdown', {pointerId: 7, button: 0});
  handle.dispatch('pointermove', {pointerId: 7, clientX: 1, clientY: rect.top + rect.height - 1});
  handle.dispatch('keydown', {key: 'Escape'});
  handle.dispatch('pointermove', {pointerId: 7, clientX: 1, clientY: rect.top + rect.height - 1});
  handle.dispatch('pointerup', {pointerId: 7});
  assert.deepEqual(released, [7]);
  assert.deepEqual(modelOrder(f), ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna']);
  assert.equal(f.astra.classList.contains('ordering'), false);
  await flush(); assert.equal(f.requests.length, 0);
});

test('settled animation hit testing follows viewport scroll in both axes', () => {
  const a = {matches: () => false}, b = {matches: () => false}, nodes = [a, b];
  const window = {scrollX: 0, scrollY: 0};
  for (const node of nodes) {
    node.getBoundingClientRect = () => ({left: -window.scrollX, right: 100 - window.scrollX,
      top: nodes.indexOf(node) * 100 - window.scrollY, height: 100});
    node.animate = () => ({cancel() {}});
  }
  const motion = Crossfeed.createOrderMotion({querySelectorAll: () => nodes}, window);
  motion.change(() => nodes.reverse());
  assert.equal(motion.targetAt(nodes, 5, 5), b);
  window.scrollY = 100; window.scrollX = 20;
  assert.equal(motion.targetAt(nodes, 5, 5), a); // A is now at viewport top, while B has scrolled out
  assert.deepEqual(motion.rect(a), {left: -20, right: 80, top: 0, height: 100});
  assert.equal(motion.targetAt(nodes, 85, 5), undefined); // outside the horizontally shifted row
});

test('a stationary pointer is rechecked after scrolling during an animated card drag', async () => {
  const f = start({motion: true}), pool = f.document.getElementById('pool-codex');
  const target = f.document.getElementById('pool-extra'), handle = handleOf(pool);
  f.document.elementFromPoint = () => target;
  handle.dispatch('pointerdown', {pointerId: 14, button: 0});
  handle.dispatch('pointermove', {pointerId: 14, clientX: 5, clientY: 100});
  assert.equal(target.classList.contains('drop-before'), true);
  f.window.scrollY = 300;
  handle.dispatch('pointermove', {pointerId: 14, clientX: 5, clientY: 100});
  assert.equal(target.classList.contains('drop-before'), false); // pointer is now in the unaccepted half of Extra
  handle.dispatch('pointercancel', {pointerId: 14});
  await flush(); assert.equal(f.requests.length, 0);
});

test('the unaccepted half of a drag target shows no insertion marker and pointerup saves nothing', async () => {
  const f = start({motion: true}), handle = handleOf(f.astra);
  f.document.elementFromPoint = () => f.sol;
  const target = f.sol.getBoundingClientRect();
  const before = modelOrder(f);
  handle.dispatch('pointerdown', {pointerId: 15, button: 0});
  handle.dispatch('pointermove', {pointerId: 15, clientX: 5, clientY: target.top + 1});
  assert.equal(f.sol.classList.contains('drop-after'), false);
  assert.equal(f.sol.classList.contains('drop-before'), false);
  assert.deepEqual(modelOrder(f), before);
  handle.dispatch('pointerup', {pointerId: 15});
  await flush(); assert.equal(f.requests.length, 0);
});

test('search results support arrows and edge keys, and legacy copy returns focus', async () => {
  const f = start({legacyCopy: true});
  f.api.openSearch(); f.input.value = 'gpt'; f.input.dispatch('input');
  f.input.dispatch('keydown', {key: 'ArrowDown'});
  const results = f.document.querySelector('#search-results'), links = results.querySelectorAll('a');
  results.dispatch('keydown', {key: 'ArrowDown'}); assert.equal(f.document.activeElement, links[1]);
  results.dispatch('keydown', {key: 'End'}); assert.equal(f.document.activeElement, links.at(-1));
  results.dispatch('keydown', {key: 'Home'}); assert.equal(f.document.activeElement, links[0]);
  results.dispatch('keydown', {key: 'ArrowUp'}); assert.equal(f.document.activeElement, f.input);
  f.other.focus(); f.document.querySelector('.copy').click(); await flush();
  assert.equal(f.document.activeElement, f.other);
  assert.equal(f.document.querySelector('.copy').classList.contains('done'), true);
});


// Navigation must stretch a travelling knot, never pulse the current tab or a previously hidden one.
test('the knot stays round on reload or arrival from a hidden tab and clears after the transition', async () => {
  for (const visible of [true, false]) {
    const env = start();
    const storage = new Map();
    env.window.sessionStorage = {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key)};
    const tab = h('a', {href: '/', 'aria-current': 'page'});
    tab.getClientRects = () => visible ? [{}] : [];
    env.document.querySelector('.mast').append(h('nav', {class: 'nav'}, tab));
    env.windowListeners.pageswap[0]({viewTransition: {}});
    tab.getClientRects = () => [{}];
    let finish;
    const finished = new Promise(resolve => { finish = resolve; });
    env.windowListeners.pagereveal[0]({viewTransition: {finished}});
    assert.equal(env.document.classList.contains('knot-arrives'), true);
    assert.equal(storage.size, 0);
    finish(); await flush();
    assert.equal(env.document.classList.contains('knot-arrives'), false);
  }
});

test('travel between different visible tabs stretches; unavailable storage does not break navigation', () => {
  const env = start(), storage = new Map();
  env.window.sessionStorage = {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
    removeItem: key => storage.delete(key)};
  const tab = h('a', {href: '/', 'aria-current': 'page'});
  tab.getClientRects = () => [{}];
  env.document.querySelector('.mast').append(h('nav', {class: 'nav'}, tab));
  env.windowListeners.pageswap[0]({viewTransition: {}});
  tab.setAttribute('href', '/other');
  env.windowListeners.pagereveal[0]({viewTransition: {finished: Promise.resolve()}});
  assert.equal(env.document.classList.contains('knot-arrives'), false);
  env.window.sessionStorage = {setItem() { throw new Error('private mode'); }, getItem() { throw new Error('private mode'); }};
  assert.doesNotThrow(() => env.windowListeners.pageswap[0]({viewTransition: {}}));
  assert.doesNotThrow(() => env.windowListeners.pagereveal[0]({viewTransition: {finished: Promise.resolve()}}));
});

function providerFixture() {
  const elements = Object.fromEntries(['kind', 'label', 'id', 'base_url', 'credential_ref', 'key', 'models', 'daily_cap', 'acceptance', 't']
    .map(name => [name, h(name === 'kind' ? 'select' : ['models', 'acceptance'].includes(name) ? 'textarea' : 'input',
      {name, value: name === 'kind' ? 'openai-compatible' : name === 't' ? 'tok' : ''})]));
  const probe = h('button', {type: 'submit', formaction: '/provider/probe'}), save = h('button', {type: 'submit'});
  const output = h('p', {class: 'provider-result'});
  const list = h('fieldset', {class: 'provider-model-list'});
  const form = h('form', {class: 'provider-form', action: '/provider/add'}, elements.t, elements.kind, elements.label, elements.id,
    h('p', {id: 'provider-type-help'}),
    h('div', {'data-provider-section': 'connection'}, elements.base_url, elements.credential_ref, elements.key),
    h('div', {class: 'provider-discovery', hidden: ''}, h('input', {class: 'provider-model-filter'}), list, h('p', {class: 'provider-model-count'})),
    elements.models, h('div', {'data-provider-section': 'budget'}, elements.daily_cap),
    h('div', {'data-provider-section': 'relay'}, elements.acceptance), probe, save, output);
  form.elements = elements;
  const f = start({prepare: env => env.document.body.append(form)});
  return {...f, form, elements, probe, save, output, list};
}

test('provider discovery requires an explicit choice and submits only selected model IDs', async () => {
  const f = providerFixture();
  f.elements.key.value = 'synthetic-provider-key';
  f.form.submit(f.probe);
  assert.equal(f.probe.disabled, true);
  assert.equal(f.requests[0].url, '/provider/probe');
  assert.equal(f.requests[0].options.body.get('t'), 'tok');
  assert.equal(f.requests[0].options.body.has('models'), false);
  reply(f.requests[0], {models: ['model-one', 'model-two'], selected: ['model-one', 'model-two'], message: 'Connected.'});
  await flush();
  assert.equal(f.elements.models.value, '');
  const [one, two] = f.list.querySelectorAll('input');
  one.checked = true; one.dispatch('change');
  assert.equal(f.elements.models.value, 'model-one');
  assert.equal(two.checked, false);
  assert.equal(f.elements.key.value, 'synthetic-provider-key');
  f.form.submit(f.save);
  assert.equal(f.requests[1].options.body.get('models'), 'model-one');
  reply(f.requests[1], {message: 'Saved.'}); await flush();
});

test('changing connection clears credentials and shows only relevant fields', () => {
  const f = providerFixture();
  f.elements.key.value = 'synthetic-provider-key'; f.elements.credential_ref.value = 'env:OLD_KEY';
  f.elements.kind.value = 'openrouter'; f.elements.kind.dispatch('change');
  assert.equal(f.elements.key.value, ''); assert.equal(f.elements.credential_ref.value, '');
  assert.equal(f.elements.base_url.value, 'https://openrouter.ai/api/v1');
  assert.equal(f.elements.daily_cap.disabled, false);
  f.elements.kind.value = 'codex'; f.elements.kind.dispatch('change');
  assert.equal(f.elements.base_url.disabled, true); assert.equal(f.elements.key.disabled, true);
  assert.equal(f.elements.daily_cap.disabled, true); assert.equal(f.elements.acceptance.disabled, true);
  f.elements.kind.value = 'crossfeed-chat'; f.elements.kind.dispatch('change');
  assert.equal(f.elements.base_url.disabled, false); assert.equal(f.elements.acceptance.disabled, false);
  assert.equal(f.elements.acceptance.required, true);
});

test('manual IDs can recover from an oversized checkbox selection', async () => {
  const f = providerFixture(); f.form.submit(f.probe);
  reply(f.requests[0], {models: Array.from({length: 201}, (_, i) => `model-${i}`), message: 'Connected.'}); await flush();
  const inputs = f.list.querySelectorAll('input'); inputs.forEach(input => { input.checked = true; });
  inputs[0].dispatch('change'); assert.equal(f.elements.models.validationMessage, 'Choose up to 200 models.');
  f.elements.models.value = 'model-0'; f.elements.models.oninput();
  assert.equal(f.elements.models.validationMessage, '');
  assert.equal(f.list.querySelectorAll('input:checked').length, 1);
});

test('provider errors keep the form editable; saving clears the key before reload', async () => {
  const f = start();
  const key = h('input', {name: 'key', value: 'synthetic-provider-key', type: 'password'});
  const button = h('button', {type: 'submit'}), output = h('p', {class: 'provider-result'});
  const form = h('form', {class: 'provider-form', action: '/provider/add'}, key, button, output);
  form.elements = {key};
  f.document.body.append(form);
  let reloads = 0;
  f.window.location.reload = () => { assert.equal(key.value, ''); reloads++; };
  f.document.dispatch('submit', {target: form, submitter: button});
  f.requests[0].resolve({ok: false, json: async () => ({error: 'The server refused the key. Check it and try again.'})});
  await flush();
  assert.equal(output.textContent, 'The server refused the key. Check it and try again.');
  assert.equal(button.disabled, false);
  assert.equal(key.value, 'synthetic-provider-key');
  assert.equal(reloads, 0);
  f.document.dispatch('submit', {target: form, submitter: button});
  reply(f.requests[1], {message: 'Saved.'});
  await flush();
  assert.equal(reloads, 1);
});


test('pointer edge scrolling is bounded, follows a stationary pointer, and stops on release or cancel', () => {
  const callbacks = new Map(), handlers = {}, scrolls = [], locations = [];
  let next = 0, commits = 0, cancels = 0, captures = 0;
  const win = {innerHeight: 800, scrollY: 100,
    requestAnimationFrame(fn) { callbacks.set(++next, fn); return next; },
    cancelAnimationFrame(id) { callbacks.delete(id); },
    scrollBy(x, y) { scrolls.push(y); this.scrollY += y; }};
  const handle = {ownerDocument: {defaultView: win},
    addEventListener(name, fn) { handlers[name] = fn; },
    focus(options) { assert.deepEqual(options, {preventScroll: true}); },
    setPointerCapture() { captures++; }};
  Crossfeed.wireOrderHandle(handle, {begin() {}, move() {},
    locate(x, y) { locations.push([x, y]); },
    commit() { commits++; }, cancel() { cancels++; }});
  const event = extra => ({pointerId: 1, button: 0, clientX: 100, clientY: 400,
    preventDefault() {}, ...extra});
  const tick = () => { const [id, fn] = callbacks.entries().next().value; callbacks.delete(id); fn(); };
  handlers.pointerdown(event()); tick(); assert.deepEqual(scrolls, []);
  handlers.pointerdown(event({pointerId: 2, isPrimary: false}));
  handlers.pointercancel(event({pointerId: 2}));
  assert.equal(cancels, 0); // an ignored second finger cannot cancel the accepted drag
  assert.equal(callbacks.size, 1);
  handlers.pointermove(event({clientY: 10})); tick();
  assert.ok(scrolls[0] < 0 && scrolls[0] >= -12);
  assert.deepEqual(locations.at(-1), [100, 10]);
  assert.equal(captures, 3); // initial capture, pointer movement, then stationary edge scroll
  handlers.pointermove(event({clientY: 790})); tick();
  assert.ok(scrolls.at(-1) > 0 && scrolls.at(-1) <= 12);
  handlers.pointerup(event()); assert.equal(callbacks.size, 0); assert.equal(commits, 1);
  handlers.pointerdown(event()); handlers.pointercancel(event());
  assert.equal(callbacks.size, 0); assert.equal(cancels, 1);
});

test('keyboard and pointer reorder focus never requests page scrolling', () => {
  const f = start(), handle = handleOf(f.sol);
  handle.dispatch('keydown', {key: 'ArrowUp'});
  assert.deepEqual(handle.focusOptions, {preventScroll: true});
  handle.dispatch('keydown', {key: 'Escape'});
  handle.dispatch('pointerdown', {pointerId: 22, clientX: 0, clientY: 200, button: 0});
  assert.deepEqual(handle.focusOptions, {preventScroll: true});
  handle.dispatch('pointercancel', {pointerId: 22});
});


test('reordering restores scroll with anchoring disabled through deferred layout', () => {
  const queued = [], nodes = [];
  const root = {style: {overflowAnchor: 'auto'}, offsetHeight: 1000};
  const win = {scrollX: 0, scrollY: 905.5, requestAnimationFrame: cb => queued.push(cb),
    scrollTo(x, y) { this.scrollX = x; this.scrollY = y; }};
  const motion = Crossfeed.createOrderMotion({documentElement: root, querySelectorAll: () => nodes}, win);
  motion.change(() => { assert.equal(root.style.overflowAnchor, 'none'); win.scrollY = 378; });
  assert.equal(win.scrollY, 905.5);
  assert.equal(root.style.overflowAnchor, 'none');
  win.scrollY = 378; queued.shift()();
  assert.equal(win.scrollY, 905.5);
  assert.equal(root.style.overflowAnchor, 'auto');
});

function markFixture(env) {
  env.mark = h('a', {class: 'product-mark', href: '/'});
  const values = {};
  env.mark.style = {setProperty(name, value) { values[name] = String(value); }};
  env.markValues = () => ({state: env.mark.dataset.markState,
    valve: Number.parseFloat(values['--cf-valve']), left: Number.parseFloat(values['--cf-left']),
    right: Number.parseFloat(values['--cf-right']), pipe: Number(values['--cf-pipe']), flow: Number(values['--cf-flow'])});
  env.document.querySelector('header').append(env.mark);
}
function startMark(options = {}) {
  return start({...options, prepare: markFixture, configureWindow(win, env) {
    let now = 0, next = 0;
    const frames = new Map();
    win.requestAnimationFrame = fn => { frames.set(++next, fn); return next; };
    win.cancelAnimationFrame = id => frames.delete(id);
    win.IntersectionObserver = class {
      constructor(fn) { env.intersect = visible => fn([{isIntersecting: visible}]); }
      observe() {}
    };
    env.pendingFrames = () => frames.size;
    env.delayedFrame = ms => {
      now += ms;
      const queued = [...frames.values()]; frames.clear();
      for (const fn of queued) fn(now);
    };
    env.advance = (ms, sample = () => {}) => {
      const paint = () => {
        const queued = [...frames.values()]; frames.clear();
        for (const fn of queued) fn(now);
        sample(env.markValues());
      };
      paint();
      const end = now + ms;
      while (now < end) { now = Math.min(end, now + 25); paint(); }
    };
  }});
}
const enterMark = f => f.mark.dispatch('pointerenter', {pointerType: 'mouse'});
const leaveMark = f => f.mark.dispatch('pointerleave', {pointerType: 'mouse'});

function assertPhysicalTrace(f, duration) {
  let previous = f.markValues();
  f.advance(duration, value => {
    assert.equal(value.left + value.right, 0);
    assert.ok(value.left >= 0 && value.left <= 7, `bounded levels ${JSON.stringify(value)}`);
    if (value.left > previous.left) {
      assert.equal(value.valve, 90, 'only an open valve transfers left to right');
      assert.equal(value.pipe, 0, 'liquid reaches the right vessel before its level rises');
    }
    if (value.left < previous.left) {
      assert.equal(value.valve, 0, 'independent refill/drain starts after the valve closes');
      assert.equal(value.flow, 0, 'reset never travels through the pipe');
    }
    if (value.flow && previous.flow) assert.ok(value.pipe <= previous.pipe, 'visible flow never travels backwards');
    previous = value;
  });
}

test('hover beyond the old 8s cycle holds the valve open and equal without looping', () => {
  const f = startMark();
  assert.equal(f.markValues().state, 'idle');
  assert.equal(f.pendingFrames(), 0);
  enterMark(f);
  f.advance(400);
  assert.equal(f.markValues().valve, 90);
  assert.equal(f.markValues().left, 0);
  f.advance(800);
  assert.equal(f.markValues().pipe, 0);
  assert.equal(f.markValues().left, 0);
  assertPhysicalTrace(f, 1200);
  const held = f.markValues();
  assert.deepEqual(held, {state: 'hold', valve: 90, left: 7, right: -7, pipe: 0, flow: 1});
  f.advance(24000);
  assert.deepEqual(f.markValues(), held);
  assert.equal(f.pendingFrames(), 0, 'hold consumes no animation frames');
});

test('a delayed frame followed by hover-to-touch hold cannot invent closing/reset time', () => {
  const f = startMark(); enterMark(f);
  f.advance(0); // First rAF timestamp is zero.
  f.delayedFrame(10000); // One visible, delayed callback overshoots equalisation by 7200ms.
  assert.equal(f.markValues().state, 'hold');
  assert.equal(f.markValues().left, 7);
  f.mark.dispatch('click', {pointerType: 'touch'});
  leaveMark(f); // The remaining interaction is now a finite 600ms touch hold.
  const held = f.markValues();
  f.advance(0); // Same timestamp: no time has passed to close or reset anything.
  assert.deepEqual(f.markValues(), held);
  assert.equal(f.markValues().state, 'hold');
  f.advance(599);
  assert.deepEqual(f.markValues(), held);
  f.advance(1);
  assert.equal(f.markValues().state, 'closing');
  assert.equal(f.markValues().valve, 90);
  assert.equal(f.markValues().left, 7);
  f.advance(200);
  assert.equal(f.markValues().valve, 45);
  assert.equal(f.markValues().left, 7);
  assertPhysicalTrace(f, 2200);
  assert.equal(f.markValues().state, 'idle');
});

test('leave closes first, holds the levels during closure, then refills/drains independently', () => {
  const f = startMark(); enterMark(f); f.advance(3000);
  leaveMark(f);
  assert.equal(f.markValues().state, 'closing');
  f.advance(200);
  assert.equal(f.markValues().valve, 45);
  assert.equal(f.markValues().left, 7);
  f.advance(200);
  assert.equal(f.markValues().valve, 0);
  assert.equal(f.markValues().left, 7);
  assertPhysicalTrace(f, 2000);
  assert.equal(f.markValues().state, 'idle');
  assert.equal(f.markValues().left, 0);
  assert.equal(f.pendingFrames(), 0);
});

test('leave/re-enter during opening, travel, transfer, closure and reset preserves current levels', () => {
  for (const elapsed of [150, 700, 1700, 2900]) {
    for (const away of [0, 100, 650]) {
      const f = startMark(); enterMark(f); assertPhysicalTrace(f, elapsed);
      const beforeLeave = f.markValues(); leaveMark(f);
      assert.equal(f.markValues().left, beforeLeave.left, 'leave never jumps levels');
      assertPhysicalTrace(f, away);
      const beforeEnter = f.markValues(); enterMark(f);
      assert.equal(f.markValues().left, beforeEnter.left, 're-enter never jumps left level');
      assert.equal(f.markValues().right, beforeEnter.right, 're-enter never swaps levels');
      assert.equal(f.markValues().valve, beforeEnter.valve, 're-enter retains current valve angle');
      assertPhysicalTrace(f, 4000);
      assert.equal(f.markValues().state, 'hold');
      assert.equal(f.markValues().left, 7);
      leaveMark(f); assertPhysicalTrace(f, 3000);
      assert.equal(f.markValues().state, 'idle');
    }
  }
});

test('hidden and offscreen pause without catch-up, including leave/re-enter while paused', () => {
  for (const suspend of ['hidden', 'offscreen']) {
    const f = startMark(); enterMark(f); f.advance(1700);
    const visibility = value => {
      if (suspend === 'hidden') { f.document.hidden = !value; f.document.dispatch('visibilitychange'); }
      else f.intersect(value);
    };
    const before = f.markValues(); visibility(false);
    assert.equal(f.pendingFrames(), 0);
    f.advance(20000);
    assert.deepEqual(f.markValues(), before);
    leaveMark(f); enterMark(f);
    assert.equal(f.markValues().left, before.left);
    visibility(true);
    f.advance(0);
    assert.equal(f.markValues().left, before.left, 'resuming does not consume hidden time');
    assertPhysicalTrace(f, 4000);
    assert.equal(f.markValues().state, 'hold');
    leaveMark(f); f.advance(500); visibility(false);
    const reset = f.markValues(); f.advance(20000);
    assert.deepEqual(f.markValues(), reset);
    visibility(true); assertPhysicalTrace(f, 3000);
    assert.equal(f.markValues().state, 'idle');
  }
});

test('touch tap runs one bounded cycle without navigation; repeat tap can close it', () => {
  const f = startMark();
  f.mark.dispatch('pointerenter', {pointerType: 'touch'});
  assert.equal(f.pendingFrames(), 0);
  let prevented = false;
  f.mark.dispatch('pointerdown', {pointerType: 'touch'});
  f.mark.dispatch('click', {preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assertPhysicalTrace(f, 2400);
  assert.equal(f.markValues().state, 'hold');
  assertPhysicalTrace(f, 3000);
  assert.equal(f.markValues().state, 'idle');
  assert.equal(f.pendingFrames(), 0);
  f.mark.dispatch('click', {pointerType: 'touch'}); f.advance(1700);
  const partial = f.markValues(); f.mark.dispatch('click', {pointerType: 'touch'});
  assert.equal(f.markValues().state, 'closing');
  assert.equal(f.markValues().left, partial.left);
  assertPhysicalTrace(f, 3000);
  assert.equal(f.markValues().state, 'idle');
});

test('reduced motion stays static on hover/touch and stops an in-flight sequence', () => {
  const f = startMark({reduced: true}); enterMark(f);
  f.mark.dispatch('click', {pointerType: 'touch'}); f.advance(20000);
  assert.deepEqual(f.markValues(), {state: 'idle', valve: 0, left: 0, right: 0, pipe: 1, flow: 0});
  assert.equal(f.pendingFrames(), 0);
  f.media.change(false); f.advance(1700);
  assert.ok(f.markValues().left > 0);
  f.media.change(true);
  assert.equal(f.markValues().state, 'idle');
  assert.equal(f.markValues().left, 0);
  assert.equal(f.pendingFrames(), 0);
});

function settingsFixture(options = {}) {
  return start({...options, prepare(env) {
    env.gear = h('a', {class: 'settings-toggle', href: '/settings', 'aria-expanded': 'false'});
    env.themeSelect = h('select', {'data-theme-choice': ''});
    env.settings = h('section', {class: 'settings-panel', id: 'settings-panel', hidden: ''}, env.themeSelect,
      h('form', {action: '/settings/pro'}, h('input', {name: 'allowance', value: '200'}), h('button', {type: 'submit'})),
      h('a', {href: '/#provider-setup'}));
    env.document.querySelector('.mast').append(env.gear, env.settings);
  }});
}

test('settings opens anchored without navigating, preserves form, Escape restores gear focus', () => {
  const f = settingsFixture();
  let prevented = false;
  f.gear.dispatch('click', {preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(f.settings.hidden, false);
  assert.equal(f.gear.getAttribute('aria-expanded'), 'true');
  assert.equal(f.gear.getAttribute('role'), 'button');
  assert.equal(f.document.activeElement, f.themeSelect);
  f.settings.querySelector('input').value = '123';
  f.document.dispatch('keydown', {key: 'Escape'});
  assert.equal(f.settings.hidden, true);
  assert.equal(f.document.activeElement, f.gear);
  f.gear.dispatch('keydown', {key: ' '});
  assert.equal(f.settings.hidden, false);
  assert.equal(f.settings.querySelector('input').value, '123');
});

test('settings and search close each other; clicks inside stay open, outside keeps its focus', () => {
  const f = settingsFixture(); f.lens.click(); f.gear.click();
  assert.equal(f.panel.hidden, true);
  f.document.dispatch('click', {target: f.themeSelect});
  assert.equal(f.settings.hidden, false);
  f.other.focus(); f.document.dispatch('click', {target: f.other});
  assert.equal(f.settings.hidden, true);
  assert.equal(f.document.activeElement, f.other);
  f.gear.click(); f.lens.click();
  assert.equal(f.settings.hidden, true);
  assert.equal(f.panel.hidden, false);
  f.document.dispatch('keydown', {key: 'Escape'});
  assert.equal(f.document.activeElement, f.gear, 'search must not restore focus into a hidden settings panel');
});

test('saving panel return reopens settings and its theme selector follows the toolbar cycle', () => {
  const stored = [];
  const f = settingsFixture({configureWindow(win) {
    win.location.hash = '#settings';
    win.localStorage = {getItem: () => 'dark', setItem: (...args) => stored.push(args)};
  }});
  assert.equal(f.settings.hidden, false);
  assert.equal(f.themeSelect.value, 'dark');
  f.document.querySelector('[data-theme-toggle]').click();
  assert.equal(f.themeSelect.value, 'system');
  f.themeSelect.value = 'light'; f.themeSelect.dispatch('change');
  assert.equal(f.document.documentElement.dataset.themeChoice, 'light');
  assert.deepEqual(stored.at(-1), ['crossfeed-theme', 'light']);
});

test('pipe current stays visible through indefinite hold and closure, pauses when hidden, stops before reset', () => {
  const f = startMark(); enterMark(f); f.advance(2400);
  assert.equal(f.markValues().state, 'hold');
  assert.equal(f.markValues().flow, 1);
  assert.equal(f.mark.hasAttribute('data-flow-running'), true);
  f.advance(12000);
  assert.equal(f.markValues().flow, 1);
  assert.equal(f.pendingFrames(), 0);
  f.document.hidden = true; f.document.dispatch('visibilitychange');
  assert.equal(f.mark.hasAttribute('data-flow-running'), false);
  f.document.hidden = false; f.document.dispatch('visibilitychange');
  assert.equal(f.mark.hasAttribute('data-flow-running'), true);
  leaveMark(f); f.advance(200);
  assert.equal(f.markValues().flow, 1);
  assert.equal(f.markValues().left, 7);
  f.advance(200);
  assert.equal(f.markValues().valve, 0);
  assert.equal(f.markValues().flow, 0);
  assert.equal(f.mark.hasAttribute('data-flow-active'), false);
  assert.equal(f.markValues().left, 7);
  assertPhysicalTrace(f, 2000);
});


test('live cap loads only on settings open, once, and failed load remains retryable', async () => {
  const f = start({prepare(env) {
    env.gear = h('a', {class: 'settings-toggle', href: '/settings'});
    env.cap = h('span', {'data-settings-cap': ''});
    env.settings = h('section', {class: 'settings-panel', id: 'settings-panel', hidden: ''}, env.cap);
    env.document.querySelector('.mast').append(env.gear, env.settings);
  }});
  assert.equal(f.requests.length, 0);
  f.gear.click();
  assert.equal(f.requests[0].url, '/settings?cap=1');
  f.requests[0].reject(new Error('Offline')); await flush();
  assert.match(f.cap.textContent, /Unavailable/);
  f.gear.click(); f.gear.click();
  assert.equal(f.requests.length, 2);
  reply(f.requests[1], {cap: '7 fresh chats per rolling hour'}); await flush();
  assert.equal(f.cap.textContent, '7 fresh chats per rolling hour');
  f.gear.click(); f.gear.click();
  assert.equal(f.requests.length, 2);
});

test('re-entering during valve closure preserves current through reopening and pauses hidden or reduced motion', () => {
  const f = startMark(); enterMark(f); f.advance(2400);
  leaveMark(f); f.advance(200);
  assert.equal(f.markValues().valve, 45);
  assert.equal(f.markValues().flow, 1);
  enterMark(f);
  assert.equal(f.markValues().state, 'opening');
  assert.equal(f.markValues().flow, 1, 'reopening never interrupts flow through a valve that has not closed');
  assert.equal(f.mark.hasAttribute('data-flow-active'), true);
  assert.equal(f.mark.hasAttribute('data-flow-running'), true);
  const reopening = f.markValues();
  f.document.hidden = true; f.document.dispatch('visibilitychange');
  assert.equal(f.mark.hasAttribute('data-flow-running'), false);
  f.advance(1000);
  assert.deepEqual(f.markValues(), reopening);
  f.document.hidden = false; f.document.dispatch('visibilitychange');
  assert.equal(f.mark.hasAttribute('data-flow-running'), true);
  f.advance(100);
  assert.equal(f.markValues().valve, 67.5);
  assert.equal(f.markValues().left, 7);
  assert.equal(f.markValues().flow, 1);
  f.advance(100);
  assert.equal(f.markValues().state, 'hold');
  assert.equal(f.markValues().flow, 1);
  assert.equal(f.pendingFrames(), 0);
  leaveMark(f); f.advance(200); enterMark(f);
  f.media.change(true);
  assert.equal(f.markValues().flow, 0);
  assert.equal(f.markValues().state, 'idle');
  assert.equal(f.mark.hasAttribute('data-flow-running'), false);
  assert.equal(f.pendingFrames(), 0);
});

test('reopening a partially transferred vessel resumes transfer without restarting pipe travel', () => {
  const f = startMark(); enterMark(f); f.advance(1700);
  assert.equal(f.markValues().state, 'transfer');
  assert.equal(f.markValues().pipe, 0);
  const partial = f.markValues();
  assert.ok(partial.left > 0 && partial.left < 7);
  leaveMark(f); f.advance(200); enterMark(f);
  assert.equal(f.markValues().flow, 1);
  f.advance(200);
  assert.equal(f.markValues().state, 'transfer');
  assert.equal(f.markValues().pipe, 0, 'pipe stays filled because the interrupted valve never closed');
  assert.equal(f.markValues().flow, 1);
  assert.equal(f.markValues().left, partial.left, 'reopening holds the existing level before transfer resumes');
  assertPhysicalTrace(f, 700);
  assert.equal(f.markValues().state, 'hold');
  assert.equal(f.markValues().left, 7);
  assert.equal(f.markValues().flow, 1);
});

test('reopening during partial pipe travel resumes its remaining distance without a backward jump', () => {
  const f = startMark(); enterMark(f); f.advance(700);
  assert.equal(f.markValues().state, 'travel');
  const partial = f.markValues();
  assert.equal(partial.pipe, .625);
  leaveMark(f); f.advance(200); enterMark(f);
  assert.equal(f.markValues().flow, 1);
  f.advance(200);
  assert.equal(f.markValues().state, 'travel');
  assert.equal(f.markValues().pipe, partial.pipe, 'reopening does not refill the already travelled pipe segment');
  assert.equal(f.markValues().flow, 1);
  f.advance(250);
  assert.equal(f.markValues().pipe, .3125, 'retained travel uses only its remaining 500ms');
  f.advance(250);
  assert.equal(f.markValues().pipe, 0);
  assert.equal(f.markValues().state, 'transfer');
  assert.equal(f.markValues().left, 0);
  assertPhysicalTrace(f, 1200);
  assert.equal(f.markValues().state, 'hold');
});
