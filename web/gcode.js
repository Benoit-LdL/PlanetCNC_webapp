// Minimal G-code interpreter for the job preview.
// Supports G0/G1/G2/G3 (IJK + R, helical), G17/18/19, G20/21, G90/91, G90.1/91.1,
// G81-G89 drilling cycles (G98/G99) and G53 (skipped, machine coords).
// Not supported: expressions, # parameters, subroutines/loops, cutter compensation.
//
// The preview does not draw every tool move (that gets slow on big 3D jobs). It draws the
// job's outline: the area the cutting moves cover, seen from above.

const WORD_RE = /([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))/g;
const ARC_TOLERANCE = 0.05;   // max chord deviation in program units
const MAX_ARC_SEGMENTS = 360;
const GRID_CELLS = 1000;      // outline resolution along the job's longest side

function stripComments(line) {
  return line.replace(/\([^)]*\)/g, ' ').replace(/;.*$/, '').toUpperCase();
}

/**
 * Run the program, calling emit(from, to, isRapid) for every straight move (arcs are split
 * into short chords). Returns true if the program ends in inches (G20).
 */
function walkGcode(text, emit) {
  let cur = [0, 0, 0];
  let motion = 0;
  let absolute = true;
  let arcAbsolute = false;
  let plane = 17;
  let inches = false;
  let cycle = null;     // active canned cycle G code
  let retractOld = true; // G98
  let cycleR = 0;
  let cycleZ = 0;

  const src = text.split(/\r?\n/);
  for (let i = 0; i < src.length; i++) {
    const code = stripComments(src[i]);
    if (!code.trim() || code.trim().startsWith('%')) continue;

    const w = {};
    const gs = [];
    WORD_RE.lastIndex = 0;
    let m;
    while ((m = WORD_RE.exec(code))) {
      const v = parseFloat(m[2]);
      if (m[1] === 'G') gs.push(v);
      else w[m[1]] = v;
    }

    let skipMove = false;
    for (const g of gs) {
      const gi = Math.round(g * 10);
      if (gi === 0 || gi === 10 || gi === 20 || gi === 30) { motion = gi / 10; cycle = null; }
      else if (gi === 170) plane = 17;
      else if (gi === 180) plane = 18;
      else if (gi === 190) plane = 19;
      else if (gi === 200) { inches = true; }
      else if (gi === 210) { inches = false; }
      else if (gi === 900) absolute = true;
      else if (gi === 910) absolute = false;
      else if (gi === 901) arcAbsolute = true;
      else if (gi === 911) arcAbsolute = false;
      else if (gi === 530 || gi === 280 || gi === 300 || gi === 100 || gi === 920) skipMove = true;
      else if (gi === 800) cycle = null;
      else if (gi >= 810 && gi <= 890 && gi % 10 === 0) { cycle = gi / 10; motion = -1; }
      else if (gi === 980) retractOld = true;
      else if (gi === 990) retractOld = false;
    }
    if (skipMove) continue;

    const hasXYZ = 'X' in w || 'Y' in w || 'Z' in w;
    const axis = (k, letter) =>
      letter in w ? (absolute ? w[letter] : cur[k] + w[letter]) : cur[k];

    if (cycle !== null) {
      if ('R' in w) cycleR = w.R;
      if ('Z' in w) cycleZ = w.Z;
      if (!('X' in w || 'Y' in w)) continue;
      const startZ = cur[2];
      const x = axis(0, 'X'), y = axis(1, 'Y');
      const r = absolute ? cycleR : startZ + cycleR;
      const z = absolute ? cycleZ : r + cycleZ;
      const p1 = [x, y, startZ], p2 = [x, y, r], p3 = [x, y, z];
      emit(cur, p1, true);
      emit(p1, p2, true);
      emit(p2, p3, false);
      const back = [x, y, retractOld ? Math.max(startZ, r) : r];
      emit(p3, back, true);
      cur = back;
      continue;
    }

    if (!hasXYZ || motion < 0) continue;
    const target = [axis(0, 'X'), axis(1, 'Y'), axis(2, 'Z')];

    if (motion === 2 || motion === 3) {
      arcSegments(cur, target, w, motion === 2, plane, arcAbsolute, (a, b) => emit(a, b, false));
    } else {
      emit(cur, target, motion === 0);
    }
    cur = target;
  }
  return inches;
}

function arcSegments(start, end, w, clockwise, plane, arcAbsolute, emit) {
  // Map to plane coordinates: a/b in-plane axes, c the linear (helix) axis.
  const [ia, ib, ic, ka, kb] =
    plane === 17 ? [0, 1, 2, 'I', 'J'] : plane === 18 ? [2, 0, 1, 'K', 'I'] : [1, 2, 0, 'J', 'K'];
  const sa = start[ia], sb = start[ib], ea = end[ia], eb = end[ib];
  let ca, cb;

  if ('R' in w) {
    let r = w.R;
    const dx = ea - sa, dy = eb - sb;
    const d = Math.hypot(dx, dy);
    if (d === 0) { emit(start, end); return; }
    const h2 = r * r - (d / 2) ** 2;
    const h = h2 > 0 ? Math.sqrt(h2) : 0;
    // Center is offset perpendicular from chord midpoint; sign picks short/long arc.
    let sign = clockwise ? -1 : 1;
    if (r < 0) { sign = -sign; r = -r; }
    ca = sa + dx / 2 - sign * h * dy / d;
    cb = sb + dy / 2 + sign * h * dx / d;
  } else {
    const oa = w[ka] ?? 0, ob = w[kb] ?? 0;
    ca = arcAbsolute ? oa : sa + oa;
    cb = arcAbsolute ? ob : sb + ob;
  }

  const r = Math.hypot(sa - ca, sb - cb);
  const a0 = Math.atan2(sb - cb, sa - ca);
  const a1 = Math.atan2(eb - cb, ea - ca);
  let sweep = a1 - a0;
  if (clockwise) { if (sweep >= -1e-9) sweep -= 2 * Math.PI; }
  else if (sweep <= 1e-9) sweep += 2 * Math.PI;
  if ('P' in w && w.P > 1) sweep += (clockwise ? -2 : 2) * Math.PI * (Math.round(w.P) - 1);

  const step = r > ARC_TOLERANCE ? 2 * Math.acos(1 - ARC_TOLERANCE / r) : Math.PI / 4;
  const n = Math.min(MAX_ARC_SEGMENTS, Math.max(2, Math.ceil(Math.abs(sweep) / step)));
  let prev = start;
  for (let s = 1; s <= n; s++) {
    const t = s / n;
    const ang = a0 + sweep * t;
    const p = [0, 0, 0];
    p[ia] = ca + r * Math.cos(ang);
    p[ib] = cb + r * Math.sin(ang);
    p[ic] = start[ic] + (end[ic] - start[ic]) * t;
    if (s === n) { p[ia] = ea; p[ib] = eb; }
    emit(prev, p);
    prev = p;
  }
}

/**
 * Outline of the job seen from above.
 *
 * The cutting moves are drawn into a grid, small gaps between neighbouring passes are closed
 * (so a pocket or 3D surface becomes one area instead of stripes), enclosed holes are filled
 * (a part cut out with a profile shows as its outer shape only), and the border of what is
 * left is traced. Rapids are ignored, and so are feed moves made
 * entirely at the program's highest Z (travel moves written as G1 by some post-processors).
 *
 * @param {string} text
 * @returns {{positions: Float32Array, count: number, z: number,
 *            bounds: {min: number[], max: number[]}, inches: boolean} | null}
 *   positions holds `count` line segments (6 floats each), all at height z (the deepest cut).
 *   bounds covers every move of the program, rapids included. null if the program never moves.
 */
export function buildOutline(text) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  let cuts = new Float32Array(5 << 14);   // x0 y0 x1 y1 lowestZ per feed move
  let n = 0;
  let cutMinZ = Infinity;

  const inches = walkGcode(text, (a, b, isRapid) => {
    for (let k = 0; k < 3; k++) {
      const lo = Math.min(a[k], b[k]), hi = Math.max(a[k], b[k]);
      if (lo < min[k]) min[k] = lo;
      if (hi > max[k]) max[k] = hi;
    }
    if (isRapid) return;
    if (n + 5 > cuts.length) {
      const grown = new Float32Array(cuts.length * 2);
      grown.set(cuts);
      cuts = grown;
    }
    const zLow = Math.min(a[2], b[2]);
    cuts[n++] = a[0]; cuts[n++] = a[1]; cuts[n++] = b[0]; cuts[n++] = b[1]; cuts[n++] = zLow;
    if (zLow < cutMinZ) cutMinZ = zLow;
  });
  if (min[0] === Infinity) return null;

  const bounds = { min, max };
  const empty = { positions: new Float32Array(0), count: 0, z: min[2], bounds, inches };
  if (!n) return empty;

  // Feed moves entirely at the top Z are travel, unless everything happens at one height.
  const topZ = max[2] - 1e-3;
  const skipTop = cutMinZ < topZ;
  const used = (i) => !skipTop || cuts[i + 4] < topZ;

  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let i = 0; i < n; i += 5) {
    if (!used(i)) continue;
    x0 = Math.min(x0, cuts[i], cuts[i + 2]); x1 = Math.max(x1, cuts[i], cuts[i + 2]);
    y0 = Math.min(y0, cuts[i + 1], cuts[i + 3]); y1 = Math.max(y1, cuts[i + 1], cuts[i + 3]);
  }

  // Grid sizing. The gap-closing radius covers usual stepovers (a few mm) without swallowing
  // the detail of small jobs.
  const mm = inches ? 1 / 25.4 : 1;
  const span = Math.max(x1 - x0, y1 - y0);
  const cell = Math.max(span / GRID_CELLS, 0.01 * mm);
  const radius = Math.max(Math.min(Math.max(1.5 * mm, span * 0.0075), span * 0.05), 2 * cell);
  const rc = radius / cell;
  const pad = Math.ceil(rc) + 2;
  const W = Math.ceil((x1 - x0) / cell) + 1 + 2 * pad;
  const H = Math.ceil((y1 - y0) / cell) + 1 + 2 * pad;
  const ox = x0 - pad * cell, oy = y0 - pad * cell;

  // 1. Cells the tool center passes over
  const cut = new Uint8Array(W * H);
  for (let i = 0; i < n; i += 5) {
    if (!used(i)) continue;
    const ax = (cuts[i] - ox) / cell, ay = (cuts[i + 1] - oy) / cell;
    const dx = (cuts[i + 2] - ox) / cell - ax, dy = (cuts[i + 3] - oy) / cell - ay;
    const steps = Math.ceil(Math.max(Math.abs(dx), Math.abs(dy)));
    for (let s = 0; s <= steps; s++) {
      const t = steps ? s / steps : 0;
      cut[Math.round(ay + dy * t) * W + Math.round(ax + dx * t)] = 1;
    }
  }

  // 2. Grow by the radius, then flood the outside from the border; whatever the flood can't
  //    reach (the cut area and holes enclosed by it) is inside.
  const near = distanceField(cut, W, H);
  const outside = new Uint8Array(W * H);
  const stack = new Int32Array(W * H);
  let sp = 0;
  const push = (j) => { if (!outside[j] && near[j] > rc) { outside[j] = 1; stack[sp++] = j; } };
  for (let x = 0; x < W; x++) { push(x); push((H - 1) * W + x); }
  for (let y = 0; y < H; y++) { push(y * W); push(y * W + W - 1); }
  while (sp) {
    const j = stack[--sp];
    const x = j % W;
    if (x > 0) push(j - 1);
    if (x < W - 1) push(j + 1);
    if (j >= W) push(j - W);
    if (j < W * (H - 1)) push(j + W);
  }

  // 3. Shrink back by half the radius. The outline ends up a little outside the tool center,
  //    roughly where a small tool's edge would cut, and drill points and single-line
  //    engraving strokes stay visible instead of collapsing to nothing.
  const fromOutside = distanceField(outside, W, H);
  const solid = cut;
  for (let i = 0; i < solid.length; i++) if (fromOutside[i] > rc / 2) solid[i] = 1;

  // 4. Trace the border (marching squares, edge midpoints)
  const z = cutMinZ;
  const out = [];
  const ex = [0.5, 1, 0.5, 0], ey = [0, 0.5, 1, 0.5];   // edge midpoints: bottom right top left
  for (let y = 0; y < H - 1; y++) {
    for (let x = 0; x < W - 1; x++) {
      const i = y * W + x;
      const c = solid[i] | (solid[i + 1] << 1) | (solid[i + W + 1] << 2) | (solid[i + W] << 3);
      const e = MS_EDGES[c];
      for (let k = 0; k < e.length; k += 2) {
        out.push(ox + (x + ex[e[k]]) * cell, oy + (y + ey[e[k]]) * cell, z,
          ox + (x + ex[e[k + 1]]) * cell, oy + (y + ey[e[k + 1]]) * cell, z);
      }
    }
  }
  return { positions: new Float32Array(out), count: out.length / 6, z, bounds, inches };
}

// Marching squares: corner bits 1=(x,y) 2=(x+1,y) 4=(x+1,y+1) 8=(x,y+1) -> pairs of edges
// (0 bottom, 1 right, 2 top, 3 left) joined by a border segment.
const MS_EDGES = [[], [3, 0], [0, 1], [3, 1], [1, 2], [3, 0, 1, 2], [0, 2], [3, 2],
  [2, 3], [0, 2], [0, 1, 2, 3], [1, 2], [1, 3], [0, 1], [3, 0], []];

/** Chamfer distance (in cells) from every cell to the nearest set cell of src. */
function distanceField(src, W, H) {
  const D = Math.SQRT2;
  const d = new Float32Array(W * H);
  for (let i = 0; i < d.length; i++) d[i] = src[i] ? 0 : 1e9;
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const i = y * W + x;
      let v = d[i];
      if (!v) continue;
      if (x > 0) v = Math.min(v, d[i - 1] + 1);
      if (y > 0) {
        v = Math.min(v, d[i - W] + 1);
        if (x > 0) v = Math.min(v, d[i - W - 1] + D);
        if (x < W - 1) v = Math.min(v, d[i - W + 1] + D);
      }
      d[i] = v;
    }
  }
  for (let y = H - 1; y >= 0; y--) {
    for (let x = W - 1; x >= 0; x--) {
      const i = y * W + x;
      let v = d[i];
      if (!v) continue;
      if (x < W - 1) v = Math.min(v, d[i + 1] + 1);
      if (y < H - 1) {
        v = Math.min(v, d[i + W] + 1);
        if (x < W - 1) v = Math.min(v, d[i + W + 1] + D);
        if (x > 0) v = Math.min(v, d[i + W - 1] + D);
      }
      d[i] = v;
    }
  }
  return d;
}
