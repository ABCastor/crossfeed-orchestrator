// Declarative dragging for the menu panel (DRAG.md contract, Pack's d2.js engine).
// Legal states are orders: a section (block) or a provider tab may sit at any index; Overview stays first and
// Other stays last. view(order) -> geometry; the engine draws the blend of the two nearest legal orders, the
// carried thing rides under the pointer, release settles on the nearest order, Escape returns to the start.
(function () {
  const M = window.TOKENS.motion;

  // --spring-settle as CSS linear(): stiffness 420, damping 28, mass 1, sampled over 510 ms.
  (function springCss() {
    const { stiffness: k, damping: c, mass: m, settleMs } = M.liftSpring;
    let x = 0, v = 0; const dt = 1 / 1000, pts = [];
    for (let t = 0; t <= settleMs; t++) { if (t % 15 === 0) pts.push(x); const a = (-k * (x - 1) - c * v) / m; v += a * dt; x += v * dt; }
    pts.push(1);
    document.documentElement.style.setProperty("--spring", `linear(${pts.map((p) => +p.toFixed(4)).join(", ")})`);
  })();

  // cubic-bezier(.2,.8,.2,1) solved for x -> y
  function bezier(x1, y1, x2, y2) {
    const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx, cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    const X = (t) => ((ax * t + bx) * t + cx) * t, Y = (t) => ((ay * t + by) * t + cy) * t, dX = (t) => (3 * ax * t + 2 * bx) * t + cx;
    return (x) => { let t = x; for (let i = 0; i < 8; i++) { const e = X(t) - x, d = dX(t); if (Math.abs(e) < 1e-5 || !d) break; t -= e / d; } return Y(Math.max(0, Math.min(1, t))); };
  }
  const EASE = bezier(...M.settleEase);
  const moveTo = (arr, id, k) => { const a = arr.filter((x) => x !== id); a.splice(k, 0, id); return a; };
  const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

  window.attachPanel = function (panel, opts = {}) {
    const dir = panel.dataset.dir;
    const reduce = () => panel.classList.contains("reduce") || matchMedia("(prefers-reduced-motion: reduce)").matches;
    const live = panel.querySelector(".live");
    const log = opts.log || (() => {});
    let anim = null, undo = [];

    // ================= sections =================
    const box = panel.querySelector(".blocks");
    let order = [...box.querySelectorAll(".blk")].map((b) => b.dataset.block);
    const els = {}; for (const b of box.querySelectorAll(".blk")) els[b.dataset.block] = b;
    const W = dir === "B" ? box.clientWidth : panel.clientWidth, GAP = dir === "B" ? 6 : 0, HALF = (W - GAP) / 2;
    const half = (id) => els[id].classList.contains("half");
    let H = {}, HH = {};
    function measure() {
      box.classList.remove("laid");
      for (const id in els) { const e = els[id]; e.style.transform = ""; e.style.height = ""; e.style.width = W + "px"; e.classList.add("spans"); H[id] = e.offsetHeight; e.classList.remove("spans"); if (half(id)) { e.style.width = HALF + "px"; HH[id] = e.offsetHeight; } }
      box.classList.add("laid");
    }
    // view: an order -> where every section sits. B+ pairs two adjacent halves; a half alone spans the row.
    function view(ord) {
      const out = {}; let y = 0;
      for (let i = 0; i < ord.length; i++) {
        const a = ord[i], b = ord[i + 1];
        if (dir === "B" && half(a) && b && half(b)) {
          const h = Math.max(HH[a], HH[b]);
          out[a] = { x: 0, y, w: HALF, h }; out[b] = { x: HALF + GAP, y, w: HALF, h }; y += h + GAP; i++;
        } else { out[a] = { x: 0, y, w: W, h: H[a] }; y += H[a] + GAP; }
      }
      out.__h = { x: 0, y: 0, w: 0, h: Math.max(0, y - GAP) };
      return out;
    }
    function paint(nodes) {
      let bottom = 0;
      for (const id in els) {
        const n = nodes[id]; if (!n) continue; const e = els[id];
        e.style.transform = `translate(${n.x}px, ${n.y}px)`; e.style.width = n.w + "px"; e.style.height = n.h + "px";
        e.classList.toggle("spans", n.w > (HALF + W) / 2); // a half tile alone in its row lays out wide (switches at the midpoint)
        if (!n.lifted) bottom = Math.max(bottom, n.y + n.h);
      }
      box.style.height = Math.max(bottom, nodes.__h ? nodes.__h.h : 0) + "px";
    }
    function settle(A, B, done) {
      if (anim) cancelAnimationFrame(anim);
      const ms = reduce() ? M.reducedMs : M.settleMs, t0 = performance.now();
      const step = (now) => { const t = Math.min(1, (now - t0) / ms); paint(t >= 1 ? B : D2.blend(A, B, EASE(t))); if (opts.onFrame) opts.onFrame("settle", t); if (t < 1) anim = requestAnimationFrame(step); else { anim = null; done && done(); } };
      anim = requestAnimationFrame(step);
    }
    let current = null;
    function layout() { measure(); current = view(order); paint(current); }
    layout();

    const label = (id) => (window.tileLabel ? window.tileLabel(id) : null) || ({ limits: "Limits", resets: "Resets", credits: "Credits", tokens: "Tokens", policy: "Spend policy", subs: "Subscriptions", links: "Links", alert: "Alert", providers: "Providers", subscriptions: "Subscriptions" }[id] || (VM.tabs[id] && VM.tabs[id].title) || id);
    function releaseLine(ord, id) {
      const i = ord.indexOf(id);
      return i === 0 ? `Release: ${label(id)} goes to the top` : `Release: ${label(id)} goes under ${label(ord[i - 1])}`;
    }
    function commit(next, id, how) {
      if (!same(next, order)) { undo.push(order); order = next; opts.onOrder && opts.onOrder("blocks", order); }
      live.textContent = `${label(id)} moved. ${releaseLine(order, id).replace("Release: ", "")}.`;
      log(`${how}: ${order.join(" · ")}`);
    }
    function keyMove(id, k) {
      const next = moveTo(order, id, Math.max(0, Math.min(order.length - 1, k)));
      const A = current; current = view(next); commit(next, id, "key"); settle(A, current);
    }

    // pointer: press anywhere on a section that is not a control; becomes a drag after the slop
    let press = null, drag = null, lastNear = -1;
    function local(e) { const r = box.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
    box.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || drag) return;
      const blk = e.target.closest(".blk"); if (!blk || blk.dataset.block === "other") return;
      if (e.target.closest("button:not(.grip), a, .segc, .popup")) return;
      press = { id: blk.dataset.block, p0: local(e), pid: e.pointerId };
    });
    function begin(e) {
      const id = press.id; box.setPointerCapture(press.pid);
      const states = order.map((_, k) => moveTo(order, id, k));
      const from = order;
      drag = { id, from, eng: D2.begin({ from, view, spec: D2.closest(states), id, anchor: (n) => ({ x: n.x, y: n.y }), axis: dir === "B" ? "xy" : "y", float: (x) => x === id, p0: press.p0, paint: (nodes, st, info) => {
        paint(nodes); current = nodes;
        if (info.nearIdx !== lastNear) { if (lastNear !== -1) { log(`tick: release changes to ${releaseLine(st, id)}`); opts.onTick && opts.onTick(); } lastNear = info.nearIdx; live.textContent = releaseLine(st, id); }
        drag && (drag.state = st);
      } }) };
      els[id].classList.add("lifted"); panel.classList.add("dragging"); lastNear = -1;
      log(`lift ${id}`);
    }
    function pointer(e) { const p = local(e); return dir === "B" ? p : { x: press.p0.x, y: p.y }; } // A+: vertical only
    box.addEventListener("pointermove", (e) => {
      if (!press) return;
      if (!drag) { const p = local(e); if (Math.hypot(p.x - press.p0.x, p.y - press.p0.y) < M.slop) return; begin(e); }
      drag.eng.move(pointer(e));
    });
    function finish(cancel) {
      if (!drag) { press = null; return; }
      const { id, from } = drag, A = drag.eng.last().nodes, next = cancel ? from : drag.eng.end();
      els[id].classList.remove("lifted"); panel.classList.remove("dragging");
      current = view(next);
      if (cancel) { live.textContent = "Move cancelled."; log("cancel"); } else commit(next, id, "drop");
      const B = current; drag = null; press = null;
      settle(A, B);
    }
    box.addEventListener("pointerup", () => finish(false));
    box.addEventListener("pointercancel", () => finish(true));
    box.addEventListener("lostpointercapture", () => { if (drag) finish(true); });
    panel.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && drag) { e.preventDefault(); finish(true); return; }
      const g = e.target.closest && e.target.closest(".grip");
      if (g && ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) {
        e.preventDefault(); const id = g.dataset.grip, i = order.indexOf(id);
        keyMove(id, i + (e.key === "ArrowUp" || e.key === "ArrowLeft" ? -1 : 1)); setTimeout(() => els[id].querySelector(".grip").focus(), 0);
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "z" && undo.length) { e.preventDefault(); const prev = undo.pop(); const A = current; order = prev; current = view(order); settle(A, current); live.textContent = "Move undone."; opts.onOrder && opts.onOrder("blocks", order); log("undo"); }
    });

    // context menu: the same legal ends without dragging
    function menu(x, y, items) {
      panel.querySelectorAll(".cmenu").forEach((m) => m.remove());
      const m = document.createElement("div"); m.className = "cmenu"; m.setAttribute("role", "menu");
      m.innerHTML = items.map((it) => it === "-" ? "<hr>" : `<button role="menuitem" ${it.off ? "disabled" : ""}>${it.t}</button>`).join("");
      const r = panel.getBoundingClientRect(); m.style.left = Math.min(x - r.left, r.width - 160) + "px"; m.style.top = (y - r.top) + "px";
      panel.appendChild(m);
      const btns = [...m.querySelectorAll("button")]; let bi = 0;
      items.filter((i) => i !== "-").forEach((it, i) => btns[i].addEventListener("click", () => { m.remove(); it.f(); }));
      btns.find((b) => !b.disabled)?.focus();
      setTimeout(() => document.addEventListener("pointerdown", function off(ev) { if (!m.contains(ev.target)) { m.remove(); document.removeEventListener("pointerdown", off); } }), 0);
    }
    box.addEventListener("contextmenu", (e) => {
      const blk = e.target.closest(".blk"); if (!blk || blk.dataset.block === "other") return; e.preventDefault();
      const id = blk.dataset.block, i = order.indexOf(id);
      menu(e.clientX, e.clientY, [
        { t: "Move up", off: i === 0, f: () => keyMove(id, i - 1) }, { t: "Move down", off: i === order.length - 1, f: () => keyMove(id, i + 1) },
        { t: "Move to top", off: i === 0, f: () => keyMove(id, 0) }, "-",
        { t: "Reset order", off: same(order, blocksFor(panel.dataset.tab, dir)), f: () => { const A = current; undo.push(order); order = blocksFor(panel.dataset.tab, dir); current = view(order); settle(A, current); opts.onOrder && opts.onOrder("blocks", order); } },
      ]);
    });

    // ================= tabs =================
    const nav = panel.querySelector(".tabs");
    const tels = {}; for (const t of nav.querySelectorAll(".tab")) tels[t.dataset.tabId] = t;
    let torder = [...nav.querySelectorAll(".tab")].map((t) => t.dataset.tabId).filter((id) => id !== "overview" && id !== "other");
    const TW = {};
    (function measureTabs() {
      const total = panel.clientWidth - 12, gap = 2, ids = Object.keys(tels);
      for (const id of ids) { tels[id].style.width = "auto"; TW[id] = tels[id].offsetWidth; }
      const spare = (total - gap * (ids.length - 1) - ids.reduce((s, id) => s + TW[id], 0)) / ids.length;
      for (const id of ids) TW[id] += spare;
    })();
    function tview(ord) {
      const out = {}; let x = 6;
      for (const id of ["overview", ...ord, "other"]) { out[id] = { x, y: 8, w: TW[id], h: 44 }; x += TW[id] + 2; }
      return out;
    }
    function tpaint(nodes) { for (const id in tels) { const n = nodes[id]; tels[id].style.transform = `translateX(${n.x}px)`; tels[id].style.width = n.w + "px"; } }
    let tcur = tview(torder); tpaint(tcur);
    function tsettle(A, B) { const ms = reduce() ? M.reducedMs : M.settleMs, t0 = performance.now(); const step = (now) => { const t = Math.min(1, (now - t0) / ms); tpaint(t >= 1 ? B : D2.blend(A, B, EASE(t))); if (t < 1) requestAnimationFrame(step); }; requestAnimationFrame(step); }
    function tcommit(next, id, how) {
      if (!same(next, torder)) { undo.push(null); torder = next; opts.onOrder && opts.onOrder("tabs", torder); }
      const i = torder.indexOf(id); live.textContent = `${tels[id].textContent.trim()} tab moved to position ${i + 2}.`; log(`${how}: tabs ${torder.join(" · ")}`);
    }
    let tpress = null, tdrag = null, tnear = -1;
    const tlocal = (e) => { const r = nav.getBoundingClientRect(); return { x: e.clientX - r.left, y: 8 }; };
    nav.addEventListener("pointerdown", (e) => { const t = e.target.closest(".tab"); if (!t || e.button !== 0) return; tpress = { id: t.dataset.tabId, p0: tlocal(e), pid: e.pointerId, fixed: t.classList.contains("fixed") }; });
    nav.addEventListener("pointermove", (e) => {
      if (!tpress) return;
      if (!tdrag) {
        if (Math.abs(tlocal(e).x - tpress.p0.x) < M.slop || tpress.fixed) return;
        const id = tpress.id, states = torder.map((_, k) => moveTo(torder, id, k)), from = torder;
        nav.setPointerCapture(tpress.pid); tels[id].classList.add("lifted"); panel.classList.add("dragging"); tnear = -1;
        tdrag = { id, from, eng: D2.begin({ from, view: tview, spec: D2.closest(states), id, anchor: (n) => ({ x: n.x, y: n.y }), axis: "x", float: (x) => x === id, p0: tpress.p0,
          paint: (nodes, st, info) => { tpaint(nodes); tcur = nodes; if (info.nearIdx !== tnear) { if (tnear !== -1) { log("tick: tab order changes"); opts.onTick && opts.onTick(); } tnear = info.nearIdx; live.textContent = `Release: ${tels[id].textContent.trim()} goes to position ${st.indexOf(id) + 2}`; } } }) };
        log(`lift tab ${id}`);
      }
      tdrag.eng.move(tlocal(e));
    });
    function tfinish(cancel) {
      if (!tdrag) { const p = tpress; tpress = null; if (p && !cancel && opts.onSelect) opts.onSelect(p.id); return; }
      const { id, from } = tdrag, A = tdrag.eng.last().nodes, next = cancel ? from : tdrag.eng.end();
      tels[id].classList.remove("lifted"); panel.classList.remove("dragging");
      if (cancel) { live.textContent = "Move cancelled."; log("cancel tab"); } else tcommit(next, id, "drop");
      tcur = tview(next); tdrag = null; tpress = null; tsettle(A, tcur);
    }
    nav.addEventListener("pointerup", () => tfinish(false));
    nav.addEventListener("pointercancel", () => tfinish(true));
    nav.addEventListener("lostpointercapture", () => { if (tdrag) tfinish(true); });
    nav.addEventListener("keydown", (e) => {
      const t = e.target.closest(".tab"); if (!t) return; const id = t.dataset.tabId;
      if (e.key === "Escape" && tdrag) { tfinish(true); return; }
      if (e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight") && !t.classList.contains("fixed")) {
        e.preventDefault(); const i = torder.indexOf(id), next = moveTo(torder, id, Math.max(0, Math.min(torder.length - 1, i + (e.key === "ArrowLeft" ? -1 : 1))));
        const A = tcur; tcur = tview(next); tcommit(next, id, "key"); tsettle(A, tcur);
      }
    });
    nav.addEventListener("contextmenu", (e) => {
      const t = e.target.closest(".tab"); if (!t || t.classList.contains("fixed")) return; e.preventDefault();
      const id = t.dataset.tabId, i = torder.indexOf(id);
      const mv = (k) => { const next = moveTo(torder, id, k), A = tcur; tcur = tview(next); tcommit(next, id, "menu"); tsettle(A, tcur); };
      menu(e.clientX, e.clientY, [{ t: "Move left", off: i === 0, f: () => mv(i - 1) }, { t: "Move right", off: i === torder.length - 1, f: () => mv(i + 1) }]);
    });

    // ================= scripted drags (renders, film strip, tests) =================
    const api = {
      order: () => order, torder: () => torder, layout,
      press(id, at = "grip") { const n = current[id]; const p0 = { x: n.x + (dir === "B" ? 20 : 8), y: n.y + 16 }; press = { id, p0, pid: null }; return p0; },
      // move the carried section by (dx, dy) from where it was pressed; lifts on the first call
      dragBy(dx, dy) { if (!drag) { press.pid = undefined; box.setPointerCapture = () => {}; begin(); } const p = { x: press.p0.x + dx, y: press.p0.y + dy }; drag.eng.move(dir === "B" ? p : { x: press.p0.x, y: p.y }); return drag.state; },
      release() { finish(false); }, cancel() { finish(true); },
      tabDragBy(id, dx) { if (!tdrag) { const n = tcur[id]; tpress = { id, p0: { x: n.x + 10, y: 8 }, fixed: false }; nav.setPointerCapture = () => {}; tels[id].classList.add("lifted"); const states = torder.map((_, k) => moveTo(torder, id, k)); tdrag = { id, from: torder, eng: D2.begin({ from: torder, view: tview, spec: D2.closest(states), id, anchor: (n) => ({ x: n.x, y: n.y }), axis: "x", float: (x) => x === id, p0: tpress.p0, paint: (nodes) => { tpaint(nodes); tcur = nodes; } }) }; } tdrag.eng.move({ x: tpress.p0.x + dx, y: 8 }); },
      tabRelease() { tfinish(false); },
      setOrder(next) { if (same(next, order)) return; const A = current; order = next.slice(); current = view(order); settle(A, current); },
      setTabOrder(next) { if (same(next, torder)) return; const A = tcur; torder = next.slice(); tcur = tview(torder); tsettle(A, tcur); },
      relayout() { const A = current; measure(); const B = view(order); current = B; settle(A, B); },
    };
    panel._api = api;
    return api;
  };
})();
