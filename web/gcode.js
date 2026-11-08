// Minimal G-code interpreter for the job preview.
// Supports G0/G1/G2/G3 (IJK + R, helical), G17/18/19, G20/21, G90/91, G90.1/91.1,
// G81-G89 drilling cycles (G98/G99) and G53 (skipped, machine coords).
// Not supported: expressions, # parameters, subroutines/loops, cutter compensation.
//
// Big 3D jobs have millions of tiny moves, too many to draw smoothly. The preview keeps their
// shape but drops points that add no visible detail (see buildToolpath).

const WORD_RE = /([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))/g;
const ARC_TOLERANCE = 0.05;   // max chord deviation in program units
const MAX_ARC_SEGMENTS = 360;
const MAX_SEGMENTS = 300000;  // most line segments drawn; the tolerance grows until a job fits
const DP_CHUNK = 4096;        // simplify long runs of moves in pieces of this many points

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

const RAPID = 0, FEED = 1, BREAK = 2;   // what kind of move ends at a point (BREAK: none, a jump)

/**
 * The program's tool moves, simplified so even big 3D jobs draw quickly.
 *
 * Every move is kept in shape, but points closer than `tolerance` to the line through their
 * neighbours are dropped (Douglas-Peucker on each run of feed or rapid moves). The tolerance
 * starts well below what the screen can show and doubles until the job fits MAX_SEGMENTS.
 *
 * @param {string} text
 * @returns {{positions: Float32Array, rapid: Uint8Array, count: number, rawCount: number,
 *            tolerance: number, cutZ: number[], bounds: {min: number[], max: number[]},
 *            inches: boolean} | null}
 *   positions holds `count` line segments (6 floats each); rapid[i] is 1 for a rapid move.
 *   cutZ is [highest, lowest] Z of the feed moves. null if the program never moves.
 */
export function buildToolpath(text) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  let pts = new Float64Array(3 << 16);
  let kinds = new Uint8Array(1 << 16);
  let n = 0;
  let last = null;
  let cutTop = -Infinity, cutLow = Infinity;

  const add = (p, kind) => {
    if (n === kinds.length) {
      const p2 = new Float64Array(pts.length * 2); p2.set(pts); pts = p2;
      const k2 = new Uint8Array(kinds.length * 2); k2.set(kinds); kinds = k2;
    }
    pts[n * 3] = p[0]; pts[n * 3 + 1] = p[1]; pts[n * 3 + 2] = p[2];
    kinds[n++] = kind;
    last = p;
  };

  const inches = walkGcode(text, (a, b, isRapid) => {
    for (let k = 0; k < 3; k++) {
      const lo = Math.min(a[k], b[k]), hi = Math.max(a[k], b[k]);
      if (lo < min[k]) min[k] = lo;
      if (hi > max[k]) max[k] = hi;
    }
    if (!last || a[0] !== last[0] || a[1] !== last[1] || a[2] !== last[2]) add(a, BREAK);
    add(b, isRapid ? RAPID : FEED);
    // Depth range for shading: feed moves along the work, not plunges or retracts from safe height.
    if (!isRapid && (a[0] !== b[0] || a[1] !== b[1])) {
      cutTop = Math.max(cutTop, a[2], b[2]);
      cutLow = Math.min(cutLow, a[2], b[2]);
    }
  });
  if (min[0] === Infinity) return null;

  const mm = inches ? 1 / 25.4 : 1;
  const span = Math.max(max[0] - min[0], max[1] - min[1], max[2] - min[2]);
  let tolerance = Math.max(span * 1e-4, 0.002 * mm);
  let keep, count;
  for (let tries = 0; ; tries++) {
    [keep, count] = simplify(pts, kinds, n, tolerance);
    if (count <= MAX_SEGMENTS || tries === 12) break;
    tolerance *= 2;
  }

  const positions = new Float32Array(count * 6);
  const rapid = new Uint8Array(count);
  let s = 0, prev = -1;
  for (let i = 0; i < n; i++) {
    if (!keep[i]) continue;
    if (kinds[i] !== BREAK && prev >= 0) {
      positions.set([pts[prev * 3], pts[prev * 3 + 1], pts[prev * 3 + 2], pts[i * 3], pts[i * 3 + 1], pts[i * 3 + 2]], s * 6);
      rapid[s++] = kinds[i] === RAPID ? 1 : 0;
    }
    prev = i;
  }
  let rawCount = 0;
  for (let i = 0; i < n; i++) if (kinds[i] !== BREAK) rawCount++;
  if (cutLow === Infinity) [cutTop, cutLow] = [max[2], min[2]];
  return { positions, rapid, count: s, rawCount, tolerance, cutZ: [cutTop, cutLow], bounds: { min, max }, inches };
}

/** Mark the points to keep; returns [keep, number of segments]. */
function simplify(pts, kinds, n, tolerance) {
  const keep = new Uint8Array(n);
  const tol2 = tolerance * tolerance;
  const stack = new Int32Array(2 * DP_CHUNK + 8);
  let i = 0;
  while (i < n - 1) {
    keep[i] = 1;
    const kind = kinds[i + 1];
    if (kind === BREAK) { i++; continue; }
    // A run of moves of one kind starting at point i, cut into chunks so one huge run stays fast.
    let j = i + 1;
    while (j + 1 < n && kinds[j + 1] === kind && j - i < DP_CHUNK) j++;
    keep[j] = 1;
    let sp = 0;
    stack[sp++] = i; stack[sp++] = j;
    while (sp) {
      const e = stack[--sp], b = stack[--sp];
      let worst = -1, worstD = tol2;
      for (let k = b + 1; k < e; k++) {
        const d = segDist2(pts, k, b, e);
        if (d > worstD) { worstD = d; worst = k; }
      }
      if (worst >= 0) {
        keep[worst] = 1;
        stack[sp++] = b; stack[sp++] = worst;
        stack[sp++] = worst; stack[sp++] = e;
      }
    }
    i = j;
  }
  if (n) keep[n - 1] = 1;
  let count = 0;
  for (let k = 1; k < n; k++) if (keep[k] && kinds[k] !== BREAK) count++;
  return [keep, count];
}

/** Squared distance from point p to the segment a-b (indices into pts). */
function segDist2(pts, p, a, b) {
  const ax = pts[a * 3], ay = pts[a * 3 + 1], az = pts[a * 3 + 2];
  const dx = pts[b * 3] - ax, dy = pts[b * 3 + 1] - ay, dz = pts[b * 3 + 2] - az;
  const px = pts[p * 3] - ax, py = pts[p * 3 + 1] - ay, pz = pts[p * 3 + 2] - az;
  const len2 = dx * dx + dy * dy + dz * dz;
  let t = len2 ? (px * dx + py * dy + pz * dz) / len2 : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  const ex = px - t * dx, ey = py - t * dy, ez = pz - t * dz;
  return ex * ex + ey * ey + ez * ez;
}

/**
 * Fusion 360 tool comment -> [tool number, shape], or null. Fusion writes one per tool at the top:
 *   (T5 D=3. CR=0. - ZMIN=-10.2 - FLAT END MILL)
 *   (T2 D=6. CR=0. TAPER=45deg - ZMIN=-1 - CHAMFER MILL)
 *   (T3 D=3. CR=0. TAPER=118deg - ZMIN=-12 - DRILL)
 */
function readFusionTool(comment) {
  const t = /^\s*T(\d+)\s+D=([\d.]+)(.*)$/i.exec(comment);
  if (!t) return null;
  const rest = t[3];
  const num = (re) => { const x = re.exec(rest); return x ? parseFloat(x[1]) : 0; };
  const kind = (/-\s*([A-Z][A-Z \-]*?)\s*$/i.exec(rest)?.[1] || '').toLowerCase();
  const taper = num(/TAPER=([\d.]+)/i);
  // Fusion gives drills their full point angle (118deg), other tools their half angle (45deg).
  const tipAngle = taper ? (/drill/.test(kind) ? taper : taper * 2) : 0;
  return [+t[1], { d: parseFloat(t[2]) || 0, cr: num(/CR=([\d.]+)/i), tipAngle, kind }];
}

/** Readers for the tool comments of each CAM program (the ids match TOOL_CAMS in server.py). */
const TOOL_READERS = { fusion: readFusionTool };

/**
 * The tools a program uses, read from the tool comments of the given CAM program, plus where
 * each T word selects one, so the 3D view can show the tool in use at any line.
 *
 * @param {string[]} lines program lines; row numbers below are 1-based like the listing
 * @param {string} cam CAM program id, e.g. 'fusion'
 * @returns {{tools: Object<number, {d: number, cr: number, tipAngle: number, kind: string}>,
 *            changes: Array<[number, number]>}}  changes: [row, tool number], in order
 */
export function parseTools(lines, cam) {
  const read = TOOL_READERS[cam];
  const tools = {};
  const changes = [];
  if (!read) return { tools, changes };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    for (const m of line.matchAll(/\(([^)]*)\)/g)) {
      const tool = read(m[1]);
      if (tool) tools[tool[0]] = tool[1];
    }
    const tw = /T\s*(\d+)/i.exec(line.replace(/\([^)]*\)|;.*$/g, ''));
    if (tw) changes.push([i + 1, +tw[1]]);
  }
  return { tools, changes };
}
