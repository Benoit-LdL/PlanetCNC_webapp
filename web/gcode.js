// Minimal G-code interpreter for toolpath preview.
// Supports G0/G1/G2/G3 (IJK + R, helical), G17/18/19, G20/21, G90/91, G90.1/91.1,
// G81-G89 drilling cycles (G98/G99) and G53 (skipped, machine coords).
// Not supported: expressions, # parameters, subroutines/loops, cutter compensation.

const WORD_RE = /([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))/g;
const ARC_TOLERANCE = 0.05;   // max chord deviation in program units
const MAX_ARC_SEGMENTS = 360;

function stripComments(line) {
  return line.replace(/\([^)]*\)/g, ' ').replace(/;.*$/, '').toUpperCase();
}

/**
 * @param {string} text
 * @returns {{positions: Float32Array, lines: Int32Array, rapid: Uint8Array,
 *            count: number, bounds: {min: number[], max: number[]} | null, inches: boolean}}
 *   Segment i goes from positions[i*6..i*6+2] to positions[i*6+3..i*6+5],
 *   lines[i] is the 1-based source line number.
 */
export function parseGcode(text) {
  const pos = [];
  const lines = [];
  const rapid = [];
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];

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

  const addSeg = (a, b, lineNo, isRapid) => {
    pos.push(a[0], a[1], a[2], b[0], b[1], b[2]);
    lines.push(lineNo);
    rapid.push(isRapid ? 1 : 0);
    for (let k = 0; k < 3; k++) {
      const lo = Math.min(a[k], b[k]), hi = Math.max(a[k], b[k]);
      if (lo < min[k]) min[k] = lo;
      if (hi > max[k]) max[k] = hi;
    }
  };

  const src = text.split(/\r?\n/);
  for (let i = 0; i < src.length; i++) {
    const lineNo = i + 1;
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
      addSeg(cur, p1, lineNo, true);
      addSeg(p1, p2, lineNo, true);
      addSeg(p2, p3, lineNo, false);
      const back = [x, y, retractOld ? Math.max(startZ, r) : r];
      addSeg(p3, back, lineNo, true);
      cur = back;
      continue;
    }

    if (!hasXYZ || motion < 0) continue;
    const target = [axis(0, 'X'), axis(1, 'Y'), axis(2, 'Z')];

    if (motion === 2 || motion === 3) {
      arcSegments(cur, target, w, motion === 2, plane, arcAbsolute, (a, b) => addSeg(a, b, lineNo, false));
    } else {
      addSeg(cur, target, lineNo, motion === 0);
    }
    cur = target;
  }

  const count = lines.length;
  return {
    positions: new Float32Array(pos),
    lines: Int32Array.from(lines),
    rapid: Uint8Array.from(rapid),
    count,
    bounds: count ? { min, max } : null,
    inches,
  };
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

/** Index of the first segment whose source line is >= lineNo (segments are in file order). */
export function segmentIndexForLine(lines, lineNo) {
  let lo = 0, hi = lines.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (lines[mid] < lineNo) lo = mid + 1; else hi = mid;
  }
  return lo;
}
