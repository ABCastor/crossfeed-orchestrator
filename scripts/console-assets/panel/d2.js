// d2.js: declarative dragging, the small version (DRAG-1, after Horowitz, Blinn and Heer, UIST 2026).
// You write one pure view: state -> layout, a flat map of node id -> {x, y, w, h, o}.
// A drag never says where things go in pixels. It declares which future states it can lead to;
// the engine lays out each future speculatively, finds where the grabbed node would be in it,
// and shows the future whose node sits nearest the pointer (blending the two around it).
// A drag can also live in two dimensions (axis 'xy'): then every future within reach is weighted by
// how near its node sits to the pointer, so the layout flows between them with no step (DRAG-2, for indenting).
// No framework; about 180 lines.
const D2 = (() => {
  const GEO = ['x', 'y', 'w', 'h', 'o'];
  const lerp = (a, b, t) => a + (b - a) * t;

  // A whole diagram between two futures. A node only in one of them fades.
  function blend(A, B, t) {
    if (t <= 0) return A; if (t >= 1) return B;
    const out = {};
    for (const id in A) {
      const a = A[id], b = B[id];
      if (!b) { out[id] = { ...a, o: (a.o ?? 1) * (1 - t) }; continue; }
      const n = { ...(t < 0.5 ? a : b) };
      for (const k of GEO) if (k in a || k in b) n[k] = lerp(a[k] ?? (k === 'o' ? 1 : 0), b[k] ?? (k === 'o' ? 1 : 0), t);
      out[id] = n;
    }
    for (const id in B) if (!A[id]) out[id] = { ...B[id], o: (B[id].o ?? 1) * t };
    return out;
  }

  // Many futures at once, by weight (weights sum to 1). A node missing from some futures fades by their share.
  function blendMany(list) {
    if (list.length === 1) return list[0][0];
    const ids = new Set(); for (const [L] of list) for (const id in L) ids.add(id);
    const out = {};
    for (const id of ids) {
      let best = null, bw = -1, ws = 0, o = 0; const acc = { x: 0, y: 0, w: 0, h: 0 };
      for (const [L, w] of list) {
        const n = L[id]; if (!n) continue;
        if (w > bw) { bw = w; best = n; }
        for (const k in acc) acc[k] += (n[k] ?? 0) * w;
        o += (n.o ?? 1) * w; ws += w;
      }
      const m = { ...best }; for (const k in acc) m[k] = acc[k] / ws; m.o = o; out[id] = m;
    }
    return out;
  }

  // Specs: what a drag can lead to.
  const closest = (states) => ({ kind: 'closest', states });
  // One number, solved every frame so the grabbed point stays under the pointer.
  const vary = (state, { get, set, min, max, snap }) => ({ kind: 'vary', state, get, set, min, max, snap });

  // Golden-section search for the parameter whose rendered anchor is nearest the target.
  function solve(f, lo, hi) {
    const g = (Math.sqrt(5) - 1) / 2; let a = lo, b = hi, c = b - g * (b - a), d = a + g * (b - a);
    let fc = f(c), fd = f(d), n = 0;
    while (b - a > 1e-3 * (hi - lo) && n++ < 40) {
      if (fc < fd) { b = d; d = c; fd = fc; c = b - g * (b - a); fc = f(c); }
      else { a = c; c = d; fc = fd; d = a + g * (b - a); fd = f(d); }
    }
    return (a + b) / 2;
  }

  // begin({ from, view, spec, id, anchor, axis, float, drop, p0, paint })
  //   view(state) -> nodes; id: the grabbed node; anchor(node) -> {x, y}: the point that follows the pointer;
  //   axis: 'x' | 'y'; float(nodeId) -> bool: nodes lifted off the layout to ride under the pointer;
  //   drop(p) -> a state or null: a drop target that wins over the spec while the pointer is in it.
  function begin(o) {
    const L0 = o.view(o.from);
    const a0 = o.offset ? null : o.anchor(L0[o.id]);
    const off = o.offset || { x: o.p0.x - a0.x, y: o.p0.y - a0.y };
    const ax = o.axis || 'x';
    let futures = null, renders = 1;
    if (o.spec.kind === 'closest') {
      futures = o.spec.states.map((s, i) => { const L = o.view(s); renders++; const n = L[o.id]; return n ? { s, L, at: o.anchor(n), i } : null; }).filter(Boolean);
      if (ax !== 'xy') futures.sort((p, q) => p.at[ax] - q.at[ax]);
    }
    let last = { state: o.from, nodes: L0 };

    function lift(nodes, target, src) {
      if (!o.float) return nodes;
      const n = src[o.id] || nodes[o.id]; if (!n) return nodes;
      const at = o.anchor(n), dx = target.x - at.x, dy = target.y - at.y, out = { ...nodes };
      // liftY: the lifted nodes follow the pointer up and down only; across, they take the blended layout (the indent).
      for (const id in src) if (o.float(id)) out[id] = o.liftY && nodes[id] ? { ...nodes[id], y: src[id].y + dy, o: 1, lifted: true } : { ...src[id], x: src[id].x + dx, y: src[id].y + dy, o: 1, lifted: true };
      return out;
    }

    function move(p) {
      const t0 = performance.now();
      const target = { x: p.x - off.x, y: p.y - off.y };
      let state, nodes, info = { renders };
      const dropped = o.drop ? o.drop(p) : null;
      if (dropped) {
        state = dropped; nodes = lift(o.view(dropped), target, L0); info.drop = true;
      } else if (futures && ax === 'xy') {
        const ds = futures.map((f) => Math.hypot(f.at.x - target.x, f.at.y - target.y));
        let ni = 0; ds.forEach((d, i) => { if (d < ds[ni]) ni = i; });
        const near = futures[ni]; state = near.s;
        let mix;
        if (ds[ni] < 1e-3) mix = near.L;
        else {
          const w = ds.map((d) => 1 / Math.pow(d, 4)), top = w[ni];
          const keep = futures.map((f, i) => [f.L, w[i]]).filter(([, x]) => x > top * 0.01), sum = keep.reduce((t, [, x]) => t + x, 0);
          mix = blendMany(keep.map(([L, x]) => [L, x / sum]));
        }
        nodes = lift(mix, target, near.L);
        info.points = futures.map((f) => f.at); info.near = ni; info.nearIdx = near.i;
      } else if (futures) {
        const v = target[ax];
        let i = 0; while (i < futures.length - 1 && futures[i + 1].at[ax] <= v) i++;
        const A = futures[i], B = futures[Math.min(i + 1, futures.length - 1)];
        const span = B.at[ax] - A.at[ax];
        const t = span > 0 ? Math.max(0, Math.min(1, (v - A.at[ax]) / span)) : 0;
        const near = (v < A.at[ax] || t < 0.5) ? A : B;
        state = near.s;
        nodes = lift(blend(A.L, B.L, t), target, near.L);
        info.points = futures.map((f) => f.at); info.near = futures.indexOf(near); info.nearIdx = near.i; info.t = t;
      } else {
        const sp = o.spec;
        const f = (val) => { renders++; const n = o.view(sp.set(sp.state, val))[o.id]; return n ? Math.abs(o.anchor(n)[ax] - target[ax]) : 1e9; };
        const val = solve(f, sp.min, sp.max);
        nodes = o.view(sp.set(sp.state, val));
        state = sp.set(sp.state, Math.max(sp.min, Math.min(sp.max, sp.snap ? sp.snap(val) : val)));
        info.value = sp.get(state);
      }
      info.ms = performance.now() - t0; info.renders = renders; info.futures = futures ? futures.length : 0;
      last = { state, nodes, info };
      o.paint(nodes, state, info);
      return last;
    }
    return { move, end: () => last.state, last: () => last };
  }

  // Ease from what is on screen to where the new state puts things. Reduced motion: no tween.
  function tween(A, B, ms, paint, done) {
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce || ms <= 0) { paint(B, 1); done && done(); return; }
    const t0 = performance.now();
    const ease = (t) => 1 - Math.pow(1 - t, 3);
    (function step(now) {
      const t = Math.min(1, (now - t0) / ms);
      paint(t >= 1 ? B : blend(A, B, ease(t)), t);
      if (t < 1) requestAnimationFrame(step); else done && done();
    })(t0);
  }

  return { begin, closest, vary, blend, blendMany, tween };
})();
