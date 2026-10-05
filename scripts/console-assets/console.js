/* Crossfeed Orchestrator's console page. Progressive enhancement: every control is a plain form the
   server accepts without this script. With it, a click changes the page in the same frame (a switch
   flips at once, then is confirmed or put back), nothing reloads, and saves go one at a time so the
   last click always wins. The pure parts are exported for tests; boot() wires the page. */
const Crossfeed = (() => {
  // "/" opens search when not typing; Cmd-K (Mac) or Ctrl-K (elsewhere) opens it always.
  function searchShortcut(event) {
    if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && String(event.key).toLowerCase() === 'k') return 'k';
    if (event.key === '/' && !event.metaKey && !event.ctrlKey && !event.altKey && !isTyping(event.target)) return '/';
    return null;
  }
  function isTyping(target) {
    return Boolean(target && target.matches && target.matches('input, textarea, select, [contenteditable=""], [contenteditable="true"]'));
  }
  function isMac(nav) {
    const platform = (nav && (nav.userAgentData?.platform || nav.platform || nav.userAgent)) || '';
    return /mac|iphone|ipad|ipod/i.test(platform);
  }
  function keyHint(nav) { return isMac(nav) ? '⌘K' : 'Ctrl K'; }

  // Saves: one at a time, newest wins. Each change is shown at once (apply), sent in order, and
  // confirmed by the server's answer. A failed save puts back the last confirmed value, unless a
  // newer click on the same control has already replaced it.
  function createSaver({send, onConfirmed, onFailed}) {
    let queue = Promise.resolve();
    let seq = 0;
    const latest = new Map();
    function save(key, request) {
      const mine = ++seq;
      latest.set(key, mine);
      const run = queue.then(async () => {
        try {
          const data = await send(request);
          if (latest.get(key) === mine) latest.delete(key);
          onConfirmed(data, key, latest.has(key));
          return true;
        } catch (error) {
          const newer = latest.get(key) !== mine;
          if (!newer) latest.delete(key);
          onFailed(key, newer, error);
          return false;
        }
      });
      queue = run.catch(() => false);
      return run;
    }
    return {save, pending: key => latest.has(key), busy: () => latest.size > 0, get revision() { return seq; }};
  }

  // What a provider's switches add up to, in the words console.py writes for the same state.
  // models: [{name, on}], notes: the server's note for each state (they travel on the page).
  function switchView(models, notes) {
    const on = models.filter(model => model.on);
    if (!models.length) return {state: 'auto', label: 'Crossfeed decides', note: notes.empty || ''};
    if (on.length === models.length) {
      return models.length === 1 ? {state: 'only', label: models[0].name, note: notes.only || ''}
                                 : {state: 'all', label: 'All enabled', note: notes.all || ''};
    }
    if (!on.length) return {state: 'none', label: 'None on', note: notes.none || ''};
    if (on.length === 1) return {state: 'one', label: `Only ${on[0].name} on`, note: notes.one || ''};
    return {state: 'some', label: `${on.length} of ${models.length} on`, note: notes.some || ''};
  }
  // "<model>=on" or "<model>=off": what a switch sends; the state, never a flip.
  function splitChange(text) {
    const at = String(text).lastIndexOf('=');
    return at < 0 ? [String(text), ''] : [text.slice(0, at), text.slice(at + 1)];
  }

  // Copy: the Clipboard API where the page may use it, else the older select-and-copy; true when it worked.
  async function copyText(text, nav, doc) {
    try {
      if (nav && nav.clipboard && nav.clipboard.writeText) { await nav.clipboard.writeText(text); return true; }
    } catch (_) { /* fall through to the older way */ }
    if (!doc || !doc.createElement) return false;
    const focused = doc.activeElement;
    const area = doc.createElement('textarea');
    area.value = text; area.setAttribute('readonly', ''); area.className = 'vh';
    doc.body.append(area); area.select();
    let done = false;
    try { done = doc.execCommand('copy'); } catch (_) { done = false; }
    area.remove();
    focused?.focus({preventScroll: true});
    return done;
  }

  // FLIP: read the visible positions, move the real nodes, then animate back from those positions.
  // WAAPI never writes inline transforms; cancel/finish leaves the final layout immediately usable.
  function createOrderMotion(doc, win) {
    const preference = win.matchMedia?.('(prefers-reduced-motion: reduce)');
    const active = new Map();
    let bounds = new Map();
    let boundsScrollX = 0, boundsScrollY = 0;
    let anchorFrame = null, anchorOriginal = null;
    function settledRect(node) {
      const rect = bounds.get(node);
      if (!rect) return null;
      const dx = (win.scrollX || 0) - boundsScrollX, dy = (win.scrollY || 0) - boundsScrollY;
      return {left: rect.left - dx, right: rect.right - dx, top: rect.top - dy, height: rect.height};
    }
    const stop = () => { for (const animation of active.values()) animation.cancel(); active.clear(); };
    preference?.addEventListener?.('change', event => { if (event.matches) stop(); });
    function change(mutate) {
      const nodes = [...doc.querySelectorAll('.pool, .opt')];
      const before = new Map(nodes.map(node => [node, node.getBoundingClientRect?.()]));
      stop();
      const anchor = doc.documentElement;
      if (anchorOriginal === null) anchorOriginal = anchor?.style?.overflowAnchor || '';
      if (anchorFrame !== null) win.cancelAnimationFrame?.(anchorFrame);
      const scrollX = win.scrollX || 0, scrollY = win.scrollY || 0;
      if (anchor?.style) anchor.style.overflowAnchor = 'none';
      mutate();
      void anchor?.offsetHeight;
      win.scrollTo?.(scrollX, scrollY);
      anchorFrame = win.requestAnimationFrame?.(() => {
        win.scrollTo?.(scrollX, scrollY);
        if (anchor?.style) anchor.style.overflowAnchor = anchorOriginal;
        anchorOriginal = null; anchorFrame = null;
      });
      if (preference?.matches) return;
      const after = new Map(nodes.map(node => [node, node.getBoundingClientRect?.()]));
      bounds = after;
      boundsScrollX = win.scrollX || 0; boundsScrollY = win.scrollY || 0;
      for (const node of nodes) {
        const first = before.get(node), last = after.get(node);
        if (!first?.height || !last?.height || !node.animate) continue;
        let x = first.left - last.left, y = first.top - last.top;
        // Model rows inherit their card's motion. Animate only the remainder to avoid moving twice.
        const parent = node.matches('.opt') ? node.closest('.pool') : null;
        if (parent && before.get(parent)?.height && after.get(parent)?.height && parent.animate) {
          x -= before.get(parent).left - after.get(parent).left;
          y -= before.get(parent).top - after.get(parent).top;
        }
        if (!Number.isFinite(x) || !Number.isFinite(y) || (Math.abs(x) < .5 && Math.abs(y) < .5)) continue;
        const animation = node.animate([{transform: `translate(${x}px, ${y}px)`}, {transform: 'none'}],
          {duration: 220, easing: 'cubic-bezier(0.16, 1, 0.3, 1)'});
        active.set(node, animation);
        animation.onfinish = animation.oncancel = () => {
          if (active.get(node) === animation) active.delete(node);
        };
      }
    }
    return {change, targetAt(nodes, x, y) {
      // Hit-test the settled layout during motion, so a row sliding under a stationary pointer
      // cannot turn the previous insertion into a different insertion on the next pointer event.
      if (!active.size) return doc.elementFromPoint?.(x, y);
      return nodes.find(node => {
        const rect = settledRect(node);
        return rect && x >= rect.left && x < rect.right && y >= rect.top && y < rect.top + rect.height;
      });
    }, rect: node => active.size && settledRect(node) || node.getBoundingClientRect()};
  }

  function wireOrderHandle(handle, actions) {
    let pointer = null, frame = null, position = null;
    const win = handle.ownerDocument?.defaultView;
    function stopScroll() {
      if (frame !== null) win?.cancelAnimationFrame?.(frame);
      frame = null; position = null;
    }
    function scrollEdge() {
      frame = null;
      if (pointer === null || !position || !win) return;
      const band = 64, y = position.y, height = win.innerHeight;
      const velocity = y < band ? -Math.min(12, (band - y) / 5) :
        y > height - band ? Math.min(12, (y - height + band) / 5) : 0;
      if (velocity) {
        const before = win.scrollY;
        win.scrollBy(0, velocity);
        if (win.scrollY !== before) {
          actions.locate(position.x, position.y);
          // An edge scroll can reorder the captured node without a pointermove event.
          if (pointer !== null) handle.setPointerCapture?.(pointer);
        }
      }
      if (pointer !== null) frame = win.requestAnimationFrame(scrollEdge);
    }
    handle.addEventListener('keydown', event => {
      if (!['ArrowUp', 'ArrowDown', 'Enter', 'Escape'].includes(event.key)) return;
      event.preventDefault();
      if (event.key === 'Escape' || event.key === 'Enter') {
        const captured = pointer; pointer = null; stopScroll();
        if (captured !== null && handle.hasPointerCapture?.(captured)) handle.releasePointerCapture?.(captured);
        if (event.key === 'Escape') actions.cancel(); else actions.commit();
      }
      else { actions.begin(); actions.move(event.key === 'ArrowUp' ? -1 : 1); }
    });
    handle.addEventListener('pointerdown', event => {
      if (pointer !== null || event.isPrimary === false || (event.button !== undefined && event.button !== 0)) return;
      event.preventDefault();
      actions.begin(); pointer = event.pointerId;
      position = {x: event.clientX, y: event.clientY};
      if (win?.requestAnimationFrame) frame = win.requestAnimationFrame(scrollEdge);
      handle.focus({preventScroll: true}); handle.setPointerCapture?.(pointer);
    });
    handle.addEventListener('pointermove', event => {
      if (pointer === null || pointer !== event.pointerId) return;
      event.preventDefault(); position = {x: event.clientX, y: event.clientY};
      actions.locate(event.clientX, event.clientY);
      // A reordered node moves in the DOM; renew capture after the move so touch keeps following it.
      handle.setPointerCapture?.(pointer);
    });
    handle.addEventListener('pointerup', event => {
      if (pointer === null || pointer !== event.pointerId) return;
      pointer = null; stopScroll(); actions.commit();
    });
    handle.addEventListener('pointercancel', event => {
      if (pointer === null || pointer !== event.pointerId) return;
      pointer = null; stopScroll(); actions.cancel();
    });
    handle.addEventListener('lostpointercapture', () => {
      if (pointer !== null && !handle.hasPointerCapture?.(pointer)) { pointer = null; stopScroll(); actions.cancel(); }
    });
  }

  return {searchShortcut, isTyping, isMac, keyHint, createSaver, switchView, splitChange, copyText, wireOrderHandle, createOrderMotion};
})();

function boot(document, window) {
  const root = document.documentElement;
  root.classList.add('js');
  const mark = document.querySelector('.product-mark');
  if (mark) {
    let visible = true, hovering = false, touching = false, pointerType;
    let phase = 'idle', elapsed = 0, duration = 0, from = 0;
    let angle = 0, level = 0, pipe = 1, frame = null, lastTime = null;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    const wanted = () => hovering || touching;
    const runnable = () => visible && !document.hidden && !reduced.matches;
    const render = () => {
      mark.dataset.markState = phase;
      mark.style.setProperty('--cf-valve', `${angle * 90}deg`);
      mark.style.setProperty('--cf-left', `${level * 7}px`);
      mark.style.setProperty('--cf-right', `${level * -7}px`);
      mark.style.setProperty('--cf-pipe', pipe);
      const flowing = ['opening', 'travel', 'transfer', 'hold', 'closing'].includes(phase) && angle > 0 && pipe < 1;
      mark.style.setProperty('--cf-flow', flowing ? 1 : 0);
      flowing ? mark.setAttribute('data-flow-active', '') : mark.removeAttribute('data-flow-active');
      pipe === 0 ? mark.setAttribute('data-flow-filled', '') : mark.removeAttribute('data-flow-filled');
      flowing && runnable() ? mark.setAttribute('data-flow-running', '') : mark.removeAttribute('data-flow-running');
      runnable() && wanted() ? mark.setAttribute('data-mark-active', '') : mark.removeAttribute('data-mark-active');
    };
    const begin = next => {
      phase = next; elapsed = 0;
      if (next === 'opening') { from = angle; duration = (1 - angle) * 400; }
      if (next === 'travel') { from = pipe; duration = pipe * 800; }
      if (next === 'transfer') { from = level; duration = (1 - level) * 1200; }
      if (next === 'hold') duration = touching && !hovering ? 600 : Infinity;
      if (next === 'closing') { from = angle; duration = angle * 400; }
      if (next === 'reset') { from = level; duration = level * 2000; }
    };
    const advance = delta => {
      // Consume only visible time. Boundaries can share a frame, never the wrong valve state.
      while (phase !== 'idle' && delta >= 0) {
        const used = Math.min(delta, Math.max(0, duration - elapsed));
        elapsed += used; delta -= used;
        const progress = duration === 0 ? 1 : elapsed / duration;
        if (phase === 'opening') angle = from + (1 - from) * progress;
        if (phase === 'travel') pipe = from * (1 - progress);
        if (phase === 'transfer') level = from + (1 - from) * progress;
        if (phase === 'closing') angle = from * (1 - progress);
        if (phase === 'reset') level = from * (1 - progress);
        if (elapsed < duration) break;
        if (phase === 'opening') begin(level === 1 ? 'hold' : pipe === 0 ? 'transfer' : 'travel');
        else if (phase === 'travel') begin('transfer');
        else if (phase === 'transfer') begin('hold');
        else if (phase === 'hold') { touching = false; begin('closing'); }
        else if (phase === 'closing') begin('reset');
        else { phase = 'idle'; pipe = 1; }
      }
      render();
    };
    const moving = () => phase !== 'idle' && !(phase === 'hold' && duration === Infinity);
    const tick = time => {
      frame = null;
      advance(lastTime === null ? 0 : Math.max(0, time - lastTime));
      lastTime = time;
      if (runnable() && moving()) frame = window.requestAnimationFrame(tick);
      else lastTime = null;
    };
    const sync = () => {
      if (reduced.matches) {
        touching = false; phase = 'idle'; angle = level = 0; pipe = 1;
      } else if (wanted()) {
        if (phase === 'idle' || phase === 'closing' || phase === 'reset') begin('opening');
        else if (phase === 'hold') {
          const holdDuration = hovering ? Infinity : 600;
          // A finite touch hold starts now; prior indefinite hover time cannot pay for it.
          if (holdDuration !== duration) elapsed = 0;
          duration = holdDuration;
        }
      } else if (!['idle', 'closing', 'reset'].includes(phase)) begin('closing');
      if (!runnable() && frame !== null) {
        window.cancelAnimationFrame(frame); frame = null; lastTime = null;
      }
      render();
      if (runnable() && moving() && frame === null) frame = window.requestAnimationFrame(tick);
    };
    mark.addEventListener('pointerenter', event => {
      if (event.pointerType !== 'touch') { hovering = true; sync(); }
    });
    mark.addEventListener('pointerleave', event => {
      if (event.pointerType !== 'touch') { hovering = false; sync(); }
    });
    mark.addEventListener('pointerdown', event => { pointerType = event.pointerType; });
    mark.addEventListener('click', event => {
      const tapped = (event.pointerType || pointerType) === 'touch';
      pointerType = undefined;
      if (!tapped) return;
      event.preventDefault(); touching = !touching; sync();
    });
    if (window.IntersectionObserver) new window.IntersectionObserver(entries => {
      visible = entries[0].isIntersecting; sync();
    }).observe(mark);
    document.addEventListener('visibilitychange', sync);
    reduced.addEventListener('change', sync);
    sync();
  }

  // ---- light and dark: the icon shows the choice (system, light, dark), never the resolution ----
  const toggle = document.querySelector('[data-theme-toggle]');
  const themes = ['system', 'light', 'dark'];
  let choice = 'system';
  try { choice = window.localStorage.getItem('crossfeed-theme') || choice; } catch (_) {}
  if (!themes.includes(choice)) choice = 'system';
  const themeSelect = document.querySelector('[data-theme-choice]');
  function theme() {
    if (themeSelect) themeSelect.value = choice;
    root.dataset.themeChoice = choice;
    if (toggle) {
      toggle.setAttribute('aria-label', `Theme: ${choice}. Switch to ${themes[(themes.indexOf(choice) + 1) % 3]}`);
      toggle.setAttribute('aria-pressed', String(choice === 'dark'));
    }
  }
  themeSelect?.addEventListener('change', () => {
    if (!themes.includes(themeSelect.value)) return;
    choice = themeSelect.value; theme();
    try { window.localStorage.setItem('crossfeed-theme', choice); } catch (_) {}
  });
  theme();
  toggle?.addEventListener('click', () => {
    choice = themes[(themes.indexOf(choice) + 1) % 3];
    theme();
    try { window.localStorage.setItem('crossfeed-theme', choice); } catch (_) {}
  });

  // ---- search: the lens, "/" and Cmd-K / Ctrl-K; Esc closes and gives focus back ----
  const lens = document.querySelector('.find-lens');
  const panel = document.querySelector('#find');
  const input = document.querySelector('#global-search-input');
  const results = document.querySelector('#search-results');
  const searchStatus = document.querySelector('.search-status');
  const hint = document.querySelector('.key-hint');
  if (hint) hint.textContent = Crossfeed.keyHint(window.navigator);
  const searchable = [...document.querySelectorAll('[data-search-label]')];
  let returnFocus = null;
  function reveal(target) {
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
      if (parent.matches('details')) parent.open = true;
    }
    target.scrollIntoView({block: 'center'});
    target.focus({preventScroll: true});
  }
  function haystack(el) {
    const provider = el.closest('.pool');
    return [el.dataset.searchLabel, el.dataset.model || '', provider && provider !== el ? provider.dataset.searchLabel : '']
      .join(' ').toLowerCase();
  }
  function search() {
    results.replaceChildren();
    const words = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const matches = words.length ? searchable.filter(el => words.every(word => haystack(el).includes(word))) : [];
    for (const target of matches) {
      const li = document.createElement('li');
      const link = document.createElement('a');
      const provider = target.closest('.pool');
      link.href = `#${target.id}`;
      link.textContent = target.dataset.searchLabel + (provider && provider !== target ? ` · ${provider.dataset.searchLabel}` : '');
      link.addEventListener('click', event => {
        event.preventDefault();
        closeSearch(false);
        reveal(target);
      });
      li.append(link); results.append(li);
    }
    searchStatus.textContent = !words.length ? 'Find a provider or model, older models included.' :
      matches.length ? `${matches.length} result${matches.length === 1 ? '' : 's'}` : 'No results. Try another name or a shorter search.';
  }
  function openSearch() {
    if (!panel) return;
    closeSettings();
    if (panel.hidden) returnFocus = document.activeElement && document.activeElement !== document.body ? document.activeElement : lens;
    panel.hidden = false; lens.setAttribute('aria-expanded', 'true');
    search(); input.focus(); input.select?.();
  }
  function closeSearch(restore = true) {
    if (!panel || panel.hidden) return;
    panel.hidden = true; lens.setAttribute('aria-expanded', 'false');
    if (restore) (returnFocus || lens).focus();
    returnFocus = null;
  }
  // Settings is a non-modal panel; the link remains a working fallback without JS.
  const settingsToggle = document.querySelector('.settings-toggle');
  const settingsPanel = document.querySelector('#settings-panel');
  const settingsCap = settingsPanel?.querySelector('[data-settings-cap]');
  let capLoaded = false;
  function openSettings() {
    if (!settingsPanel) return;
    closeSearch(false);
    settingsPanel.hidden = false;
    settingsToggle.setAttribute('aria-expanded', 'true');
    if (settingsCap && !capLoaded) {
      capLoaded = true;
      window.fetch('/settings?cap=1', {headers: {Accept: 'application/json'}})
        .then(response => { if (!response.ok) throw new Error('Cap unavailable'); return response.json(); })
        .then(result => { settingsCap.textContent = result.cap; })
        .catch(() => { settingsCap.textContent = 'Unavailable, the live cap could not be read.'; capLoaded = false; });
    }
    settingsPanel.querySelector('select, input:not([type="hidden"]), button, a')?.focus();
  }
  function closeSettings(restore = true) {
    if (!settingsPanel || settingsPanel.hidden) return;
    settingsPanel.hidden = true;
    settingsToggle.setAttribute('aria-expanded', 'false');
    if (restore) settingsToggle.focus();
  }
  if (settingsToggle && settingsPanel) {
    settingsToggle.setAttribute('role', 'button');
    settingsToggle.addEventListener('click', event => {
      event.preventDefault();
      settingsPanel.hidden ? openSettings() : closeSettings();
    });
    settingsToggle.addEventListener('keydown', event => {
      if (event.key === ' ') { event.preventDefault(); settingsToggle.click(); }
    });
    if (window.location.hash === '#settings') openSettings();
  }
  lens?.addEventListener('click', () => (panel.hidden ? openSearch() : closeSearch()));
  input?.addEventListener('input', search);
  input?.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === 'ArrowDown') { event.preventDefault(); results.querySelector('a')?.focus(); }
  });
  results?.addEventListener('keydown', event => {
    const links = [...results.querySelectorAll('a')], at = links.indexOf(document.activeElement);
    if (at < 0 || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'ArrowUp' && at === 0) { input.focus(); return; }
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? links.length - 1 :
      Math.max(0, Math.min(links.length - 1, at + (event.key === 'ArrowDown' ? 1 : -1)));
    links[next].focus();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && settingsPanel && !settingsPanel.hidden) { event.preventDefault(); closeSettings(); return; }
    if (event.key === 'Escape' && panel && !panel.hidden) { event.preventDefault(); closeSearch(); return; }
    if (lens && Crossfeed.searchShortcut(event)) { event.preventDefault(); openSearch(); }
  });
  document.addEventListener('click', event => {
    if (settingsPanel && !settingsPanel.hidden && !event.target.closest('.settings-panel, .settings-toggle')) closeSettings(false);
    if (panel && !panel.hidden && !event.target.closest('.find-panel, .find-lens')) closeSearch(false);
  });
  if (window.location.hash) {
    const target = document.getElementById(window.location.hash.slice(1));
    if (target?.id === 'provider-setup') {
      const details = target.querySelector('details'); if (details) details.open = true;
      target.scrollIntoView({block: 'start'});
    }
    if (target?.matches('[data-search-label]')) reveal(target);
  }

  // ---- changes: level, model choice, quota refresh ----
  // One line of feedback, shown at the foot of the screen wherever the page is scrolled, then gone.
  const status = document.querySelector('.save-status');
  let sayTimer = 0;
  const say = text => {
    if (!status) return;
    window.clearTimeout(sayTimer);
    if (!text) { status.classList.remove('shown'); return; }
    status.textContent = text;
    status.classList.add('shown');
    sayTimer = window.setTimeout(() => status.classList.remove('shown'), 3200);
  };
  const confirmed = new Map();   // key -> the server's last word, for putting a failed save back
  const briefToggle = document.querySelector('.brief-toggle');
  const briefText = document.querySelector('.agents pre');
  let briefCompact = briefText?.textContent || '';
  let briefFull = document.querySelector('.brief-full')?.textContent || briefCompact;
  let fullShown = false;
  briefToggle?.addEventListener('click', () => {
    fullShown = !fullShown;
    briefText.textContent = fullShown ? briefFull : briefCompact;
    briefToggle.textContent = fullShown ? 'Show compact' : 'Show full';
    briefToggle.setAttribute('aria-expanded', String(fullShown));
  });
  const orderState = document.querySelector('.order-state');
  const clone = value => JSON.parse(JSON.stringify(value));
  let order = orderState ? JSON.parse(orderState.dataset.order) : null;
  let confirmedOrder = order && clone(order);
  let orderDraft = null;
  const motion = Crossfeed.createOrderMotion(document, window);
  let dropTarget = null;
  function clearDrop() {
    dropTarget?.classList.remove('drop-before'); dropTarget?.classList.remove('drop-after'); dropTarget = null;
  }
  function finishDraft() {
    orderDraft?.handle.closest('.opt, .pool')?.classList.remove('ordering');
    clearDrop(); orderDraft = null;
  }
  const originalGroups = new Map([...document.querySelectorAll('.opt')].map(row => [row, row.parentElement]));
  function applyOrder(animate = true) {
    if (!order) return;
    const mutate = () => {
      const focused = document.activeElement;
      const poolList = document.querySelector('.pools');
      for (const key of order.ranks.pools[order.sort.pools]) {
        const row = document.getElementById(`pool-${key}`);
        if (row && poolList) poolList.append(row);
      }
      for (const [pool, ranking] of Object.entries(order.ranks.models)) {
        const pick = picks.get(pool);
        if (!pick) continue;
        for (const key of ranking[order.sort.models[pool]]) {
          const row = [...pick.querySelectorAll('.opt')].find(row => row.dataset.model === key);
          if (row) originalGroups.get(row).append(row);
        }
        for (const older of pick.querySelectorAll('details.older')) older.hidden = !older.querySelector('.opt');
      }
      for (const select of document.querySelectorAll('[data-sort]')) {
        select.value = select.dataset.sort === 'pools' ? order.sort.pools : order.sort.models[select.dataset.pool];
      }
      if (focused && focused !== document.body) focused.focus({preventScroll: true});
    };
    if (animate) motion.change(mutate); else mutate();
  }
  const picks = new Map([...document.querySelectorAll('.pick')].map(pick => [pick.dataset.pool, pick]));
  const switchesOf = pick => [...pick.querySelectorAll('.sw')];
  const onOf = pick => switchesOf(pick).filter(sw => sw.getAttribute('aria-checked') === 'true').map(sw => sw.dataset.key);
  for (const [pool, pick] of picks) confirmed.set(`model:${pool}`, onOf(pick));
  for (const form of document.querySelectorAll('.lv')) {
    confirmed.set(`level:${form.querySelector('[name=pool]').value}`, form.querySelector('.stop.on')?.value);
  }

  function showLevel(pool, level, means) {
    const row = document.getElementById(`pool-${pool}`);
    const form = row?.querySelector('.lv');
    if (!form) return;
    const stops = [...form.querySelectorAll('.stop')];
    row.className = row.className.replace(/\blvl-\S+/g, `lvl-${level}`);
    form.className = `lv at-${stops.findIndex(b => b.value === level)} is-${level}`;
    let meaning = means;
    for (const button of stops) {
      const selected = button.value === level;
      button.classList.toggle('on', selected);
      button.setAttribute('aria-checked', String(selected));
      button.tabIndex = selected ? 0 : -1;   // one stop in the tab order; arrows move between them
      if (selected && meaning === undefined && button.title) meaning = button.title;
    }
    if (meaning !== undefined) row.querySelector('.means').textContent = meaning;
  }
  function showModels(pool, on, told) {
    const pick = picks.get(pool);
    if (!pick) return;
    const models = [];
    const every = [];
    for (const sw of switchesOf(pick)) {
      const isOn = on.includes(sw.dataset.key);
      const model = sw.dataset.key;
      sw.setAttribute('aria-checked', String(isOn));
      sw.setAttribute('value', `${model}=${isOn ? 'off' : 'on'}`);
      sw.querySelector('.st').textContent = isOn ? 'On' : 'Off';
      sw.closest('.opt').className = sw.closest('.opt').className.replace(/ (on|off)\b/g, '') + (isOn ? ' on' : ' off');
      for (const badge of sw.closest('.opt').querySelectorAll('.older-active, .older-active-note')) badge.hidden = !isOn;
      if (!sw.closest('.opt').classList.contains('older')) {   // an older model is never counted, even in a flat sorted list
        every.push(isOn);
        models.push({name: sw.dataset.name, on: isOn});
      }
    }
    // The server's words when it has spoken, else the same words built here from the page.
    const d = pick.dataset;
    const view = told || Crossfeed.switchView(models, {empty: d.noteEmpty, only: d.noteOnly, all: d.noteAll,
      some: d.noteSome, one: d.noteOne, none: d.noteNone});
    const now = pick.querySelector('.now');
    now.className = `now ${view.state}`;
    now.querySelector('.v').textContent = view.label;
    now.querySelector('.n').textContent = view.note;
    const allOn = pick.querySelector('.all-on');
    if (allOn) allOn.hidden = every.every(Boolean);
    for (const older of pick.querySelectorAll('details.older')) {
      const all = older.querySelectorAll('.sw');
      const rows = [...all].filter(sw => sw.getAttribute('aria-checked') === 'true').length;
      const count = older.querySelector('.count');
      if (count) count.textContent = String(older.querySelectorAll('.opt').length) + (all.length && rows < all.length ? ` · ${rows} on` : '');
      const active = [...older.querySelectorAll('.opt')].some(row => row.querySelector('.older-active') &&
        row.querySelector('.sw')?.getAttribute('aria-checked') === 'true');
      for (const badge of older.querySelectorAll('.older-active-summary, .older-active-summary-note')) badge.hidden = !active;
    }
  }
  function update(data, skip = () => false) {
    for (const pool of data.pools || []) {
      // What the server confirmed is always remembered (a failed newer click puts it back); the
      // page only follows it when no newer click of ours is still on its way.
      confirmed.set(`level:${pool.pool}`, pool.level);
      if (!skip(`level:${pool.pool}`)) showLevel(pool.pool, pool.level, pool.means);
      const row = document.getElementById(`pool-${pool.pool}`);
      if (row && pool.gauge !== undefined) row.querySelector('.gauge').innerHTML = pool.gauge;
      if (pool.on !== undefined) {
        confirmed.set(`model:${pool.pool}`, pool.on);
        if (!skip(`model:${pool.pool}`)) showModels(pool.pool, pool.on, pool.label === undefined ? undefined : pool);
      }
    }
    if (data.brief !== undefined) briefCompact = data.brief;
    if (data.brief_full !== undefined) briefFull = data.brief_full;
    if (briefText && data.brief !== undefined) briefText.textContent = fullShown ? briefFull : briefCompact;
    if (data.console_order !== undefined) {
      confirmedOrder = clone(data.console_order);
      if (!orderDraft && !skip('order')) { order = clone(confirmedOrder); applyOrder(); }
    }
    if (data.stamp !== undefined) document.querySelector('.stamp').textContent = data.stamp;
  }

  async function post(url, body) {
    const response = await window.fetch(url, {method: 'POST', headers: {Accept: 'application/json'}, body});
    if (!response.ok) throw new Error(`save failed: ${response.status}`);
    return response.json();
  }
  const saver = Crossfeed.createSaver({
    send: ({url, body}) => post(url, body),
    onConfirmed: (data, key) => {
      update(data, other => saver.pending(other));
      if (!saver.busy()) say(key === 'order' ? 'Saved this page’s order.' : key.startsWith('model:') ? 'Saved. The next task uses these models.' : 'Saved. Routing uses this setting now.');
    },
    onFailed: (key, newer) => {
      if (!newer) {
        const [kind, pool] = key.split(/:(.*)/s);
        if (key === 'order') { order = clone(confirmedOrder); finishDraft(); applyOrder(); }
        else if (kind === 'level') showLevel(pool, confirmed.get(key));
        else showModels(pool, confirmed.get(key));
      }
      say('Could not save that change, so the page shows the setting that is still in force. Try again.');
    },
  });

  function saveOrder() {
    const body = new URLSearchParams({t: orderState.dataset.token,
      order: JSON.stringify({pools: order.pools, models: order.models, sort: order.sort})});
    saver.save('order', {url: '/order', body});
  }
  function cancelOrder() {
    if (!orderDraft) return;
    order = orderDraft.before; applyOrder(); finishDraft(); say('Order change cancelled.');
  }
  for (const select of document.querySelectorAll('[data-sort]')) {
    select.addEventListener('change', () => {
      const value = select.value;
      cancelOrder();
      if (select.dataset.sort === 'pools') order.sort.pools = value;
      else { order.sort.models[select.dataset.pool] = value; order.flat ||= {}; order.flat[select.dataset.pool] = true; }
      applyOrder(); saveOrder();
    });
  }
  for (const handle of document.querySelectorAll('.order-handle')) {
    const row = handle.closest('.opt') || handle.closest('.pool');
    const isModel = row.matches('.opt');
    const pool = row.closest('.pool').dataset.pool;
    const keyOf = item => isModel ? item.dataset.model : item.dataset.pool;
    function begin() {
      if (orderDraft?.handle === handle) return;
      cancelOrder();
      orderDraft = {handle, before: clone(order), dirty: false};
      row.classList.add('ordering');
    }
    function move(step) {
      if (orderDraft?.handle !== handle) return;
      const siblings = [...row.parentElement.children].filter(item => item.matches(isModel ? '.opt' : '.pool'));
      const at = siblings.indexOf(row), next = Math.max(0, Math.min(siblings.length - 1, at + step));
      if (at === next) return;
      const reordered = [...siblings]; reordered.splice(at, 1); reordered.splice(next, 0, row);
      if (isModel) {
        order.flat ||= {}; order.flat[pool] = true;
        const keys = new Set(siblings.map(keyOf));
        const changed = reordered.map(keyOf);
        order.models[pool] = order.ranks.models[pool][order.sort.models[pool]].map(key => keys.has(key) ? changed.shift() : key);
        order.ranks.models[pool].your = [...order.models[pool]]; order.sort.models[pool] = 'your';
      } else {
        order.pools = reordered.map(keyOf); order.ranks.pools.your = [...order.pools]; order.sort.pools = 'your';
      }
      orderDraft.dirty = true; applyOrder(); handle.focus({preventScroll: true});
      say(`${row.dataset.searchLabel} moved to ${next + 1} of ${siblings.length}. Enter saves; Escape cancels.`);
    }
    Crossfeed.wireOrderHandle(handle, {
      begin, move,
      commit() {
        if (orderDraft?.handle !== handle) return;
        const dirty = orderDraft.dirty; finishDraft();
        if (dirty) saveOrder();
      },
      cancel: cancelOrder,
      locate(x, y) {
        if (orderDraft?.handle !== handle) return;
        const scrollX = window.scrollX || 0, scrollY = window.scrollY || 0;
        if (orderDraft?.pointerX === x && orderDraft?.pointerY === y &&
            orderDraft.pointerScrollX === scrollX && orderDraft.pointerScrollY === scrollY) return;
        if (orderDraft) {
          orderDraft.pointerX = x; orderDraft.pointerY = y;
          orderDraft.pointerScrollX = scrollX; orderDraft.pointerScrollY = scrollY;
        }
        const siblings = [...row.parentElement.children].filter(item => item.matches(isModel ? '.opt' : '.pool'));
        const target = motion.targetAt(siblings, x, y)?.closest(isModel ? '.opt' : '.pool');
        if (target === row) return;
        clearDrop();
        if (!target || target.parentElement !== row.parentElement) return;
        const direction = siblings.indexOf(target) > siblings.indexOf(row) ? 1 : -1;
        const rect = motion.rect(target);
        if ((direction > 0 && y >= rect.top + rect.height / 2) || (direction < 0 && y <= rect.top + rect.height / 2)) {
          dropTarget = target; target.classList.add(direction > 0 ? 'drop-after' : 'drop-before');
          move(siblings.indexOf(target) - siblings.indexOf(row));
        }
      },
    });
  }
  applyOrder(false);

  const providerForm = document.querySelector('.provider-form');
  const providerHelp = {
    'openai-compatible': 'Any OpenAI-compatible API, including local model servers. Use its API URL and key reference.',
    'openrouter': 'Choose paid or free text models from the OpenRouter catalog. Set a daily budget before enabling paid calls.',
    'crossfeed-chat': 'Use saved ChatGPT workers through your local Crossfeed Chat gateway. Text only; Pro answers can take minutes.',
    'codex': 'Uses Codex’s own sign-in on this computer. Enter the native model IDs you want.',
    'claude': 'Uses Claude’s own sign-in on this computer. Enter the native model IDs you want.',
    'agy': 'Uses Antigravity’s own sign-in on this computer. Enter the native model IDs you want.',
    'opencode': 'Uses OpenCode’s own sign-in on this computer. Enter provider/model IDs.',
    'copilot': 'Uses Copilot’s own sign-in on this computer. Auto is the supported selector.',
  };
  function providerType(changed = false) {
    if (!providerForm) return;
    const kind = providerForm.elements.kind.value;
    const cli = ['codex', 'claude', 'agy', 'opencode', 'copilot'].includes(kind);
    const visible = {connection: !cli, budget: ['openai-compatible', 'openrouter'].includes(kind), relay: kind === 'crossfeed-chat'};
    for (const section of providerForm.querySelectorAll('[data-provider-section]')) {
      section.hidden = !visible[section.dataset.providerSection];
      section.querySelectorAll('input, textarea').forEach(input => { input.disabled = section.hidden; });
    }
    document.querySelector('#provider-type-help').textContent = providerHelp[kind];
    providerForm.elements.base_url.required = !cli;
    providerForm.elements.acceptance.required = kind === 'crossfeed-chat';
    providerForm.elements.base_url.readOnly = kind === 'openrouter';
    if (changed) {
      providerForm.elements.key.value = '';
      providerForm.elements.credential_ref.value = '';
      providerForm.elements.daily_cap.value = '0';
      providerForm.elements.base_url.value = kind === 'openrouter' ? 'https://openrouter.ai/api/v1' : '';
      providerForm.elements.models.value = kind === 'copilot' ? 'auto' : '';
      providerForm.elements.models.setCustomValidity('');
      providerForm.elements.models.oninput = null;
      providerForm.querySelector('.provider-discovery').hidden = true;
      providerForm.querySelector('.provider-model-filter').value = '';
      providerForm.querySelectorAll('.provider-model-list label').forEach(row => row.remove());
      providerForm.querySelector('.provider-result').textContent = '';
    }
  }
  providerForm?.elements.kind.addEventListener('change', () => providerType(true));
  providerType();
  providerForm?.querySelector('.provider-model-filter').addEventListener('input', event => {
    const query = event.target.value.trim().toLowerCase();
    providerForm.querySelectorAll('.provider-model-list label').forEach(row => {
      row.hidden = !row.textContent.toLowerCase().includes(query);
    });
  });
  function showProviderModels(models) {
    const list = providerForm.querySelector('.provider-model-list');
    list.querySelectorAll('label').forEach(row => row.remove());
    const selected = new Set(providerForm.elements.models.value.split(/[,\r\n]+/).map(id => id.trim()).filter(Boolean));
    const count = providerForm.querySelector('.provider-model-count');
    const update = () => {
      const checked = [...list.querySelectorAll('input:checked')].map(input => input.value);
      providerForm.elements.models.value = checked.join('\n');
      providerForm.elements.models.setCustomValidity(checked.length > 200 ? 'Choose up to 200 models.' : '');
      count.textContent = `${models.length} available · ${checked.length} selected (up to 200)`;
    };
    for (const model of models) {
      const row = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox'; input.value = model; input.checked = selected.has(model);
      input.addEventListener('change', update);
      const name = document.createElement('span');
      name.setAttribute('translate', 'no'); name.textContent = model;
      row.append(input, name); list.append(row);
    }
    providerForm.elements.models.oninput = () => {
      const ids = new Set(providerForm.elements.models.value.split(/[,\r\n]+/).map(id => id.trim()).filter(Boolean));
      providerForm.elements.models.setCustomValidity(ids.size > 200 ? 'Choose up to 200 models.' : '');
      list.querySelectorAll('input').forEach(input => { input.checked = ids.has(input.value); });
      count.textContent = `${models.length} available · ${list.querySelectorAll('input:checked').length} selected (up to 200)`;
    };
    providerForm.querySelector('.provider-discovery').hidden = false;
    providerForm.querySelector('.provider-model-filter').value = '';
    update();
  }

  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('.provider-form, .provider-remove')) return;
    event.preventDefault();
    const action = event.submitter?.getAttribute('formaction') || form.getAttribute('action');
    const body = new URLSearchParams(new FormData(form));
    if (action.endsWith('/probe') && !['codex', 'claude', 'agy', 'opencode', 'copilot'].includes(body.get('kind'))) body.delete('models');
    const output = form.querySelector('.provider-result') || document.querySelector('.save-status');
    const buttons = [...form.querySelectorAll('button, input:not(:disabled), select:not(:disabled), textarea:not(:disabled)')];
    buttons.forEach(button => { button.disabled = true; });
    output.dataset.error = 'false';
    output.textContent = action.endsWith('/probe') ? 'Checking connection…' : 'Saving…';
    try {
      const response = await window.fetch(action, {method: 'POST', headers: {Accept: 'application/json'}, body});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || 'The request was refused. Reload the page and try again.');
      output.textContent = result.message;
      if (action.endsWith('/probe')) {
        if (result.models?.length) showProviderModels(result.models);
        else output.textContent += ' Enter the model IDs you want to add.';
      } else {
        if (form.elements.key) form.elements.key.value = '';
        window.location.reload();
      }
    } catch (error) {
      output.dataset.error = 'true';
      output.textContent = error.message || 'Could not connect. Check the server and try again.';
    } finally {
      buttons.forEach(button => { button.disabled = false; });
    }
  });

  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('.lv, .choose, .quota-refresh')) return;
    event.preventDefault();
    const body = new URLSearchParams(new FormData(form));
    if (event.submitter?.name) body.set(event.submitter.name, event.submitter.value);
    if (form.matches('.lv')) {
      const pool = body.get('pool'), level = body.get('level');
      showLevel(pool, level);   // at once; the server's answer brings the matching words
      say('');
      saver.save(`level:${pool}`, {url: form.action, body});
      return;
    }
    if (form.matches('.choose')) {
      const pool = body.get('pool');
      const pick = picks.get(pool);
      const on = new Set(onOf(pick));
      const change = body.get('switch');
      if (change) {
        const [model, state] = Crossfeed.splitChange(change);
        if (state === 'on') on.add(model); else on.delete(model);
      } else if (body.get('model') === 'auto') {
        for (const sw of switchesOf(pick)) if (!sw.closest('.opt').classList.contains('older')) on.add(sw.dataset.key);
      }
      showModels(pool, [...on]);
      say('');
      saver.save(`model:${pool}`, {url: form.action, body});
      return;
    }
    const button = form.querySelector('button');
    if (button.getAttribute('aria-disabled') === 'true') return;
    // The icon turns while CodexBar is asked again; the line beside it says so, then gives the new time.
    const stamp = document.querySelector('.stamp');
    const before = stamp ? stamp.textContent : '';
    button.setAttribute('aria-disabled', 'true'); form.setAttribute('aria-busy', 'true'); form.classList.add('busy');
    if (stamp) stamp.textContent = 'Reading the limits again…';
    try {
      const data = await post(form.action, body);
      update(data, key => saver.pending(key));
      const failed = Object.entries(data.refresh || {})
        .filter(([, value]) => value !== 'refreshed' && value !== 'kept newer stored snapshot')
        .map(([pool]) => document.getElementById(`pool-${pool}`)?.dataset.searchLabel || pool);
      say(!failed.length ? 'Limits read again.' :
        `Could not read ${failed.length === 1 ? 'one plan' : failed.length + ' plans'} (${failed.join(', ')}); their last reading stays.`);
    } catch (_) {
      if (stamp) stamp.textContent = before;
      say('Could not read the limits. The last reading stays. Try again.');
    } finally {
      button.removeAttribute('aria-disabled'); form.removeAttribute('aria-busy'); form.classList.remove('busy');
    }
  });

  // ---- copy what agents read: the control at the block's top right, with a "Copied" state ----
  const copyButton = document.querySelector('.copy');
  let copyTimer = 0;
  copyButton?.addEventListener('click', async () => {
    const text = document.querySelector('.agents pre')?.textContent || '';
    const done = await Crossfeed.copyText(text, window.navigator, document);
    const word = copyButton.querySelector('.cw');
    window.clearTimeout(copyTimer);
    copyButton.classList.toggle('done', done);
    if (word) word.textContent = done ? 'Copied' : 'Copy';
    copyButton.setAttribute('aria-label', done ? 'Copied what agents read' : 'Copy what agents read');
    say(done ? 'Copied what agents read.' : 'Could not copy. Select the text and copy it by hand.');
    copyTimer = window.setTimeout(() => {
      copyButton.classList.remove('done');
      if (word) word.textContent = 'Copy';
      copyButton.setAttribute('aria-label', 'Copy what agents read');
    }, 1800);
  });

  // ---- each slider is one radio group: Tab reaches it once, the arrow keys move and save ----
  for (const form of document.querySelectorAll('.lv')) {
    const stops = [...form.querySelectorAll('.stop')];
    for (const button of stops) button.tabIndex = button.classList.contains('on') ? 0 : -1;
    form.addEventListener('keydown', event => {
      const step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[event.key];
      const edge = {Home: 0, End: stops.length - 1}[event.key];
      if (step === undefined && edge === undefined) return;
      event.preventDefault();
      const at = stops.findIndex(button => button.classList.contains('on'));
      const next = stops[edge !== undefined ? edge : Math.max(0, Math.min(stops.length - 1, at + step))];
      if (!next || next.classList.contains('on')) return;
      next.focus();
      form.requestSubmit(next);
    });
  }

  // The knot stretches only when it travels to a different visible tab. Reloading the one-tab console or
  // arriving from a phone (where the tab is hidden) keeps the bead round. Storage is optional in private mode.
  const knotFrom = 'crossfeed-knot-from';
  function visibleTab() {
    const at = document.querySelector('.nav a[aria-current]');
    return at && at.getClientRects().length ? at.getAttribute('href') : 'none';
  }
  if (typeof window.addEventListener === 'function') window.addEventListener('pageswap', event => {
    if (!event.viewTransition) return;
    try { window.sessionStorage.setItem(knotFrom, visibleTab()); } catch (_) { /* private mode */ }
  });
  if (typeof window.addEventListener === 'function') window.addEventListener('pagereveal', event => {
    let from = null;
    try { from = window.sessionStorage.getItem(knotFrom); window.sessionStorage.removeItem(knotFrom); } catch (_) { /* private mode */ }
    if (!event.viewTransition || (from !== 'none' && from !== visibleTab())) return;
    document.documentElement.classList.add('knot-arrives');
    const clear = () => document.documentElement.classList.remove('knot-arrives');
    event.viewTransition.finished.then(clear, clear);
  });

  // ---- stay current: quota readings age, and another tab or an agent may change a setting ----
  async function refresh() {
    if (saver.busy() || document.hidden || !status) return;
    const revision = saver.revision;
    try {
      const response = await window.fetch('/snapshot', {headers: {Accept: 'application/json'}});
      if (response.ok) {
        const data = await response.json();
        if (!saver.busy() && revision === saver.revision) update(data);
      }
    } catch (_) { /* Stored readings remain visible with their age. */ }
  }
  if (status) { window.setTimeout(refresh, 1000); window.setInterval(refresh, 30000); }
  return {update, showModels, showLevel, saver, openSearch, closeSearch};
}

if (typeof document !== 'undefined' && document.documentElement && typeof window !== 'undefined') boot(document, window);
if (typeof module === 'object' && module.exports) module.exports = {Crossfeed, boot};
