// Feeds, spindle speed and step height of a G-code program, per operation, and a rewritten copy
// with new values. Used by the "Feeds & steps" window (tune-ui.js).
//
// What can change:
//  - feed rates (F): each distinct F value of an operation can get a new value;
//  - spindle speed (S) of an operation;
//  - peck depth (Q) of drilling cycles (G83, G73);
//  - step height of layer-by-layer operations (2D pocket, adaptive, contour with multiple depths):
//    only smaller. Each pass that steps down more than the new maximum is preceded by copies of
//    itself, lifted so the passes step down evenly. The original pass stays as it was, so the
//    final depth and shape don't change; every copy cuts less than the original pass would have.
//
// The original file is never changed: the result is a new program with a comment block at the
// top listing what changed, in the CAM program's terms, so the values can be copied back into
// Fusion 360. Re-adjusting an adjusted file keeps the link to the values from Fusion.

const WORD_RE = /([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))/g;
const EPS = 1e-6;
const LEVEL_EPS = 5e-4;        // Z values closer than this are one level
const MAJOR_LEVEL = 0.1;       // a level is a pass when it has at least this share of the longest level's length
const HEADER_START = '(--- Feeds and steps adjusted on the CNC dashboard';
const HEADER_END = '(--- end of adjustments ---)';

/** Split a line into its code part (comments removed, upper case) and the comment texts. */
function splitLine(line) {
  const comments = [];
  const code = line.replace(/\(([^)]*)\)/g, (m, c) => { comments.push(c.trim()); return ' '; })
    .replace(/;(.*)$/, (m, c) => { comments.push(c.trim()); return ''; });
  return { code: code.toUpperCase(), comments };
}

function readWords(code) {
  const words = [];
  WORD_RE.lastIndex = 0;
  let m;
  while ((m = WORD_RE.exec(code))) words.push([m[1], parseFloat(m[2])]);
  return words;
}

/**
 * Replace word values in the code part of a line, leaving comments alone.
 * fn(letter, value) returns the new text for the number, or null to keep it.
 */
function replaceWords(line, fn) {
  let out = '';
  let depth = 0;
  let seg = '';
  const flush = () => {
    out += seg.replace(/([A-Za-z])(\s*)([-+]?(?:\d+\.?\d*|\.\d+))/g, (m, l, sp, v) => {
      const r = fn(l.toUpperCase(), parseFloat(v));
      return r === null || r === undefined ? m : l + sp + r;
    });
    seg = '';
  };
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (depth === 0 && c === ';') { flush(); out += line.slice(i); return out; }
    if (c === '(') { if (depth === 0) flush(); depth++; out += c; continue; }
    if (depth > 0) { out += c; if (c === ')') depth--; continue; }
    seg += c;
  }
  flush();
  return out;
}

/** Number for G-code: at most `dec` decimals, no trailing zeros. */
export function fmtNum(v, dec = 3) {
  let s = (Math.round(v * 10 ** dec) / 10 ** dec).toFixed(dec);
  if (s.includes('.')) s = s.replace(/0+$/, '').replace(/\.$/, '');
  return s === '-0' ? '0' : s;
}

const sameNum = (a, b) => Math.abs(a - b) < 1e-9 * Math.max(1, Math.abs(a));
const cleanText = (t) => String(t).replace(/[()]/g, '').replace(/\s+/g, ' ').trim();

/** Fusion 360 tool comment: (T5 D=3. CR=0. - ZMIN=-10.2 - FLAT END MILL) */
function readToolComment(c) {
  const t = /^\s*T(\d+)\s+D=([\d.]+)(.*)$/i.exec(c);
  if (!t) return null;
  const kind = (/-\s*([A-Z][A-Z \-]*?)\s*$/i.exec(t[3])?.[1] || '').toLowerCase();
  return { num: +t[1], d: parseFloat(t[2]) || 0, kind, text: cleanText(c) };
}

/** The values from Fusion recorded in an adjusted file's header, per operation index. */
function readHeader(lines) {
  const ops = {};
  let base = '', start = -1, end = -1;
  for (let i = 0; i < Math.min(lines.length, 400); i++) {
    const l = lines[i].trim();
    if (start < 0 && l.startsWith(HEADER_START)) start = i;
    if (start >= 0 && l === HEADER_END) { end = i; break; }
  }
  if (start < 0 || end < 0) return null;
  for (let i = start; i <= end; i++) {
    const m = /^\(tune (.*)\)$/.exec(lines[i].trim());
    if (!m) continue;
    const parts = m[1].split(' ');
    if (parts[0] === 'base') { base = parts.slice(1).join(' '); continue; }
    const op = /^o(\d+)$/.exec(parts[0]);
    if (!op) continue;
    const rec = { F: [], S: [], Q: [], D: null };
    for (const p of parts.slice(1)) {
      const x = /^([FSQD])([-\d.]+)>([-\d.]+)$/.exec(p);
      if (!x) continue;
      const pair = [parseFloat(x[2]), parseFloat(x[3])];
      if (x[1] === 'D') rec.D = pair; else rec[x[1]].push(pair);
    }
    ops[+op[1]] = rec;
  }
  return { start, end, base, ops };
}

/**
 * Read a program: its operations with their feeds, spindle speeds, peck depths and passes.
 * @returns {{lines: string[], units: string, ops: object[], tools: object, header: object|null, problem: string}}
 */
export function analyze(text) {
  const lines = text.split(/\r?\n/);
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  const header = readHeader(lines);
  const tools = {};
  const recs = [];        // per line: modal state and the move it makes
  const ops = [];
  let op = null;
  let motionSince = true; // a move since the last operation comment (true: the first comment opens op 1)
  let tool = null;

  // Modal state
  let pos = [0, 0, 0], known = [false, false, false];
  let motion = 0, absolute = true, arcAbsolute = false, plane = 17, inches = false;
  let feed = null, speed = null, inverseTime = false;

  const newOp = (name, i) => {
    op = { index: ops.length, name: name || `Operation ${ops.length + 1}`, start: i, nameLine: i, end: i, tool, toolSet: false,
           feeds: new Map(), speeds: new Map(), pecks: new Map(), moves: [], zMax: -Infinity, problems: [] };
    ops.push(op);
  };

  for (let i = 0; i < lines.length; i++) {
    const { code, comments } = splitLine(lines[i]);
    if (header && i >= header.start && i <= header.end) { recs.push(null); continue; }
    for (const c of comments) {
      const t = readToolComment(c);
      if (t) tools[t.num] = t;
    }
    const words = readWords(code);
    // Comments this module adds around repeated passes don't start an operation.
    const isCommentLine = !words.length && comments.length && !comments.some((c) => readToolComment(c))
      && !comments.some((c) => /^(extra|original) pass /i.test(c));
    if (isCommentLine) {
      // An operation starts at a comment line once the previous one has moved; consecutive
      // comments before the first move: the last one names the operation (Fusion writes the
      // operation name right before its tool change).
      if (motionSince) { newOp(cleanText(comments.join(' ')), i); motionSince = false; }
      else { op.name = cleanText(comments.join(' ')) || op.name; op.nameLine = i; }
      recs.push(null);
      continue;
    }
    if (!op) newOp('', i);
    op.end = i;
    if (!words.length) { recs.push(null); continue; }

    const w = {};
    const gs = [];
    let other = false;    // words besides plain motion (tool change, spindle, offsets...)
    let moveG = null;     // explicit motion word on this line
    for (const [l, v] of words) {
      if (l === 'G') gs.push(v); else w[l] = v;
      if (l === 'M' || l === 'T' || l === 'S' || l === 'H' || l === 'D' || l === 'P' || l === 'L') other = true;
    }
    let skip = false, homing = false;
    for (const g of gs) {
      const gi = Math.round(g * 10);
      if (gi === 0 || gi === 10 || gi === 20 || gi === 30) { motion = gi / 10; moveG = motion; }
      else if (gi === 170 || gi === 180 || gi === 190) { plane = gi / 10; }
      else if (gi === 200) { inches = true; other = true; }
      else if (gi === 210) { inches = false; other = true; }
      else if (gi === 900) absolute = true;
      else if (gi === 910) absolute = false;
      else if (gi === 901) arcAbsolute = true;
      else if (gi === 911) arcAbsolute = false;
      else if (gi === 930) { inverseTime = true; other = true; }
      else if (gi === 940 || gi === 950) { inverseTime = false; }
      else if (gi === 280 || gi === 300 || gi === 530) { skip = true; homing = gi !== 530; other = true; }
      else if (gi === 800) { if (motion >= 80) motion = 0; other = true; }
      else if (gi >= 730 && gi <= 890 && gi % 10 === 0) { motion = gi / 10; moveG = motion; other = true; }
      else other = true; // G4, G10, G40-G49, G54-G59, G61/G64, G98/G99, ...
    }
    if ('T' in w) {
      tool = w.T;
      if (!op.toolSet) { op.tool = tool; op.toolSet = true; }
    }
    if ('F' in w) feed = w.F;
    if ('S' in w) {
      speed = w.S;
      op.speeds.set(w.S, (op.speeds.get(w.S) || 0) + 1);
    }

    const rec = { i, op: op.index, motion, moveG, absolute, arcAbsolute, plane, feed, other,
                  from: pos.slice(), fromKnown: known.slice(), to: null, kind: '' };
    recs.push(rec);
    const hasAxis = 'X' in w || 'Y' in w || 'Z' in w;

    if (skip) {
      // G28/G30 go home through the given point, G53 moves in machine coordinates: afterwards
      // the work position of the moved axes isn't known here.
      'XYZ'.split('').forEach((a, k) => { if (a in w || (homing && !hasAxis)) known[k] = false; });
      rec.to = pos.slice();
      continue;
    }
    if (motion >= 73) {
      // Drilling cycle: Q is the peck depth.
      if ('Q' in w && (motion === 83 || motion === 73)) op.pecks.set(w.Q, (op.pecks.get(w.Q) || 0) + 1);
      op.hasCycle = true;
      if (hasAxis) {
        const t = pos.slice();
        if ('X' in w) t[0] = absolute ? w.X : pos[0] + w.X;
        if ('Y' in w) t[1] = absolute ? w.Y : pos[1] + w.Y;
        rec.to = t;
        rec.kind = 'cycle';
        if (feed !== null) addFeed(op, feed, 'drill', 0);
        motionSince = true;
        pos = t;
      }
      continue;
    }
    if (!hasAxis) continue;
    const t = [0, 1, 2].map((k) => {
      const a = 'XYZ'[k];
      if (!(a in w)) return pos[k];
      known[k] = known[k] || absolute;
      return absolute ? w[a] : pos[k] + w[a];
    });
    rec.to = t;
    op.zMax = Math.max(op.zMax, t[2]);
    if (known[2]) op.zMax = Math.max(op.zMax, pos[2]);
    motionSince = true;
    if (motion === 0) {
      rec.kind = 'rapid';
    } else if (feed !== null && !inverseTime) {
      const dxy = Math.hypot(t[0] - pos[0], t[1] - pos[1]);
      const dz = t[2] - pos[2];
      const len = motion === 1 ? Math.hypot(dxy, dz) : arcLength(pos, t, w, plane, motion === 2) || dxy;
      const flat = Math.abs(dz) < EPS && (dxy > EPS || motion !== 1);
      rec.kind = flat ? 'level' : dxy < EPS ? (dz < 0 ? 'plunge' : 'up') : dz < 0 ? 'ramp' : 'climb';
      rec.len = len;
      op.moves.push(rec);
    }
    if (inverseTime) op.problems.push('inverse time feed (G93)');
    pos = t;
  }
  for (const o of ops) finishOp(o, recs);
  // Drop empty operations (only comments or setup lines).
  const used = ops.filter((o) => o.feeds.size || o.speeds.size || o.pecks.size);
  used.forEach((o, k) => { o.index = k; o.fusion = header?.ops?.[k + 1] || null; });

  return { lines, recs, units: inches ? 'in' : 'mm', ops: used, allOps: ops, tools, header,
           base: header?.base || '' };
}

function addFeed(op, f, kind, len) {
  let e = op.feeds.get(f);
  if (!e) { e = { value: f, lens: {}, count: 0 }; op.feeds.set(f, e); }
  e.lens[kind] = (e.lens[kind] || 0) + len;
  e.count++;
}

function arcLength(from, to, w, plane, cw) {
  const [a, b, c, ka, kb] = plane === 17 ? [0, 1, 2, 'I', 'J'] : plane === 18 ? [2, 0, 1, 'K', 'I'] : [1, 2, 0, 'J', 'K'];
  let r;
  if ('R' in w) r = Math.abs(w.R);
  else r = Math.hypot(w[ka] || 0, w[kb] || 0);
  if (!r) return 0;
  const ca = from[a] + (w[ka] || 0), cb = from[b] + (w[kb] || 0);
  let a0 = Math.atan2(from[b] - cb, from[a] - ca), a1 = Math.atan2(to[b] - cb, to[a] - ca);
  let sweep = cw ? a0 - a1 : a1 - a0;
  if (sweep <= EPS) sweep += 2 * Math.PI;
  return Math.hypot(sweep * r, to[c] - from[c]);
}

/** Name each feed by what it is used for, find the passes and whether they can be split. */
function finishOp(op, recs) {
  // Horizontal moves at the operation's top height are travel, not cutting (Fusion for personal
  // use writes rapids as G1 at a high feed).
  for (const r of op.moves) {
    if (r.kind === 'level' && r.to[2] >= op.zMax - EPS) r.kind = 'up';
    addFeed(op, r.feed, r.kind, r.len);
  }
  const feeds = [...op.feeds.values()];
  const sum = (e, ...k) => k.reduce((s, x) => s + (e.lens[x] || 0), 0);
  // Fusion for personal use writes rapids as G1 at a high feed: moves that mostly go up or
  // travel at the operation's top height.
  for (const e of feeds) {
    const total = sum(e, 'level', 'plunge', 'ramp', 'up', 'climb') || 1;
    e.share = { up: sum(e, 'up', 'climb') / total, plunge: sum(e, 'plunge') / total,
                ramp: sum(e, 'ramp') / total, level: sum(e, 'level') / total };
  }
  let main = null;
  for (const e of feeds) if (sum(e, 'level') > 0 && (!main || sum(e, 'level') > sum(main, 'level'))) main = e;
  for (const e of feeds) {
    if (e.lens.drill !== undefined) e.role = 'Drilling feed';
    else if (e === main) e.role = 'Cutting feed';
    else if (e.share.up > 0.5) e.role = 'Retract feed';
    else if (e.share.plunge >= Math.max(e.share.ramp, e.share.level)) e.role = 'Plunge feed';
    else if (e.share.ramp >= e.share.level) e.role = 'Ramp feed';
    else e.role = 'Lead-in/out feed';
  }
  const order = { 'Cutting feed': 0, 'Lead-in/out feed': 1, 'Ramp feed': 2, 'Plunge feed': 3, 'Drilling feed': 4, 'Retract feed': 5 };
  op.feedList = feeds.sort((a, b) => order[a.role] - order[b.role] || b.value - a.value);
  op.plungeFeed = feeds.find((e) => e.role === 'Plunge feed')?.value ?? Math.min(...feeds.map((e) => e.value));
  op.passes = findPasses(op, recs);
}

/**
 * Layer-by-layer passes of an operation: [{z, first, last}] from top to bottom (first/last are the
 * line indexes of the pass's first and last cutting move at that height), with the step of each
 * pass, or {problem} saying why the step height can't be changed.
 */
function findPasses(op, recs) {
  if (op.hasCycle) return { problem: 'Drilling: change the peck depth instead.' };
  if (op.problems.length) return { problem: 'Uses ' + op.problems[0] + '.' };
  const level = op.moves.filter((r) => r.kind === 'level' && r.feed !== null &&
    op.feeds.get(r.feed)?.role !== 'Retract feed');
  if (!level.length) return { problem: 'No horizontal cutting moves.' };
  const byZ = new Map();
  for (const r of level) {
    const z = Math.round(r.to[2] / LEVEL_EPS) * LEVEL_EPS;
    byZ.set(z, (byZ.get(z) || 0) + r.len);
  }
  const longest = Math.max(...byZ.values());
  const major = new Set([...byZ].filter(([, len]) => len >= MAJOR_LEVEL * longest).map(([z]) => z));
  if (major.size > 400) return { problem: 'A 3D operation (the height changes all the time): change it in Fusion.' };
  const runs = [];
  for (const r of level) {
    const z = Math.round(r.to[2] / LEVEL_EPS) * LEVEL_EPS;
    if (!major.has(z)) continue;
    const last = runs[runs.length - 1];
    if (last && last.zKey === z) last.last = r.i;
    else runs.push({ zKey: z, z: r.to[2], first: r.i, last: r.i });
  }
  for (let k = 1; k < runs.length; k++) {
    if (runs[k].z >= runs[k - 1].z - EPS) {
      return { problem: runs.length > 30
        ? 'A 3D operation (no flat passes from top to bottom): change it in Fusion.'
        : 'The passes don\'t go from top to bottom one after the other: change it in Fusion.' };
    }
  }
  // Lines between two passes (and before the first) must only move: no tool change, spindle or
  // coolant in what gets repeated.
  for (let k = 0; k < runs.length; k++) {
    const from = k === 0 ? blockStart(op, recs, runs[0].first) : runs[k - 1].last + 1;
    runs[k].start = from;
    for (let i = from; i <= runs[k].last; i++) {
      const r = recs[i];
      if (!r) continue;
      if (r.other || r.motion >= 73) return { problem: 'Tool, spindle or other settings change between the passes.' };
      if (!r.absolute) return { problem: 'Uses incremental moves (G91).' };
      if (r.arcAbsolute && r.plane !== 17) return { problem: 'Uses absolute arc centres outside the XY plane.' };
    }
  }
  return { runs, top: null };
}

/** First line of the repeated block before the first pass: back over the lines that only move. */
function blockStart(op, recs, firstCut) {
  let i = firstCut;
  while (i - 1 > op.start) {
    const r = recs[i - 1];
    if (r && (r.other || r.motion >= 73 || !r.absolute)) break;
    i--;
  }
  return i;
}

/**
 * Pass heights of an operation for a given top of material: [{z, step}] from top to bottom.
 * Without a top, the first step is taken to be the same as the second one.
 */
export function passSteps(op, top) {
  const runs = op.passes.runs;
  if (!runs) return [];
  const t = top ?? defaultTop(op);
  return runs.map((r, k) => ({ z: r.z, step: (k ? runs[k - 1].z : t) - r.z }));
}

export function defaultTop(op) {
  const runs = op.passes.runs;
  if (!runs) return 0;
  if (runs.length >= 2) return runs[0].z + (runs[0].z - runs[1].z);
  return runs[0].z < 0 ? 0 : null;
}

/** Number of passes each original pass becomes with a maximum step. */
export function splitPlan(op, top, maxStep) {
  return passSteps(op, top).map((p) => {
    const n = maxStep > 0 && p.step > maxStep + 1e-4 ? Math.ceil(p.step / maxStep - 1e-6) : 1;
    return { ...p, n };
  });
}

/**
 * The program with the new values.
 * @param {object} a  analyze() result
 * @param {object[]} edits per operation: {feeds: Map(old -> new), speeds: Map, pecks: Map,
 *                         maxStep: number|null, top: number|null}
 * @param {object} info {source: file name, date: Date}
 * @returns {{text: string, summary: object[], opLines: number[]}} opLines: first line of each
 *          operation in the new program (1-based)
 */
export function apply(a, edits, info) {
  const { lines, recs } = a;
  const out = [];
  const opLines = [];
  // What the new program has set so far, to know when a moved or copied line needs its own G/F.
  let outFeed = null, outMotion = 0;

  const feedFor = (rec) => {
    if (rec.feed === null) return null;
    const op = a.allOps[rec.op];
    const k = a.ops.indexOf(op);
    const m = k >= 0 ? edits[k]?.feeds : null;
    return m && m.has(rec.feed) ? m.get(rec.feed) : rec.feed;
  };
  const editFor = (rec) => {
    const k = a.ops.indexOf(a.allOps[rec.op]);
    return k >= 0 ? edits[k] : null;
  };

  /** One program line with new F/S/Q values (and Z lifted by dz for a copied pass). */
  const emit = (i, dz = 0, zCap = Infinity) => {
    const rec = recs[i];
    let line = lines[i];
    if (!rec) { out.push(line); return; }
    const e = editFor(rec);
    const newFeed = feedFor(rec);
    const words = readWords(splitLine(line).code);
    const has = (l) => words.some(([x]) => x === l);
    const hasAxis = has('X') || has('Y') || has('Z');
    line = replaceWords(line, (l, v) => {
      if (l === 'F' && newFeed !== null && !sameNum(v, newFeed)) return fmtNum(newFeed);
      if (l === 'S' && e?.speeds?.has(v)) return fmtNum(e.speeds.get(v), 0);
      if (l === 'Q' && e?.pecks?.has(v)) return fmtNum(e.pecks.get(v), 4);
      if (l === 'Z' && dz && v < zCap - EPS) return fmtNum(Math.min(v + dz, zCap), 4);
      return null;
    });
    // After a moved or copied block the modal motion or feed may differ from what this line
    // was written for: say it explicitly.
    if (hasAxis && rec.motion <= 3 && rec.moveG === null && outMotion !== rec.motion) {
      line = `G${rec.motion} ` + line;
    }
    outMotion = rec.motion;
    if (newFeed !== null && has('F')) outFeed = newFeed;
    else if (newFeed !== null && hasAxis && rec.motion >= 1 && !sameNum(outFeed ?? NaN, newFeed)) {
      line = line.replace(/\s*$/, '') + ` F${fmtNum(newFeed)}`;
      outFeed = newFeed;
    }
    out.push(line);
  };

  // Plan the copies: line index of a pass block's start -> list of {dz, run, op}
  const copies = new Map();
  a.ops.forEach((op, k) => {
    const e = edits[k];
    if (!e?.maxStep || !op.passes.runs) return;
    const plan = splitPlan(op, e.top, e.maxStep);
    const zCap = op.zMax;
    plan.forEach((p, j) => {
      if (p.n <= 1) return;
      const run = op.passes.runs[j];
      const list = [];
      for (let c = 1; c < p.n; c++) {
        const z = p.z + p.step - p.step * c / p.n;   // from just under the previous pass down
        list.push({ dz: z - p.z, run, op, zCap });
      }
      copies.set(run.start, list);
    });
  });

  const headerLines = adjustmentHeader(a, edits, info);
  let i = 0;
  // Keep a leading % (program start marker) first.
  if (lines.length && lines[0].trim() === '%') { out.push(lines[0]); i = 1; }
  out.push(...headerLines);
  const opStart = new Map(a.ops.map((op) => [op.nameLine, op.index]));
  for (; i < lines.length; i++) {
    if (a.header && i >= a.header.start && i <= a.header.end) continue;
    if (opStart.has(i)) opLines[opStart.get(i)] = out.length + 1;
    const list = copies.get(i);
    if (list) {
      const run = list[0].run;
      const op = list[0].op;
      const first = recs[firstRec(recs, run.start)];
      const start = first.from, startKnown = first.fromKnown;
      const plunge = edits[op.index]?.feeds?.get(op.plungeFeed) ?? op.plungeFeed;
      for (const c of list) {
        out.push(`(extra pass ${fmtNum(run.z + c.dz, 3)} ${a.units}, added to step down less)`);
        for (let j = run.start; j <= run.last; j++) emit(j, c.dz, c.zCap);
        // Back to where the original pass starts: up to the operation's top height, over, down.
        out.push(`G0 Z${fmtNum(op.zMax, 4)}`);
        outMotion = 0;
        if (start && startKnown[0] && startKnown[1]) out.push(`G0 X${fmtNum(start[0], 4)} Y${fmtNum(start[1], 4)}`);
        // No need to go down when the pass starts with a rapid up or over to a height at least
        // as high as where it starts.
        const firstMove = recs.slice(run.start, run.last + 1).find((r) => r && r.to);
        const startsHigh = firstMove && firstMove.motion === 0 && firstMove.to[2] >= start[2] - EPS
          && Math.abs(firstMove.to[0] - start[0]) < EPS && Math.abs(firstMove.to[1] - start[1]) < EPS;
        if (start && startKnown[2] && start[2] < op.zMax - EPS && !startsHigh) {
          const above = start[2] + (a.units === 'in' ? 0.04 : 1);
          if (above < op.zMax - EPS) out.push(`G0 Z${fmtNum(above, 4)}`);
          out.push(`G1 Z${fmtNum(start[2], 4)} F${fmtNum(plunge)}`);
          outMotion = 1;
          outFeed = plunge;
        }
      }
      out.push(`(original pass ${fmtNum(run.z, 3)} ${a.units})`);
      for (let j = run.start; j <= run.last; j++) emit(j);
      i = run.last;
      continue;
    }
    emit(i);
  }
  return { text: out.join('\n') + '\n', summary: summarize(a, edits), opLines };
}

function firstRec(recs, i) {
  while (i < recs.length && !recs[i]) i++;
  return i;
}

/** The Fusion value behind a value of this file (an adjusted file records it in its header). */
function fusionValue(op, letter, v) {
  const pairs = op.fusion?.[letter] || [];
  const p = pairs.find(([, now]) => sameNum(now, v));
  return p ? p[0] : v;
}

/**
 * What changed, per operation, relative to the values from Fusion:
 * [{op, name, tool, values: [{what, unit, fusion, before, now}]}]
 */
export function summarize(a, edits) {
  const fu = a.units === 'in' ? 'in/min' : 'mm/min';
  return a.ops.map((op, k) => {
    const e = edits[k] || {};
    const values = [];
    for (const f of op.feedList) {
      const now = e.feeds?.get(f.value) ?? f.value;
      values.push({ what: f.role, unit: fu, letter: 'F', fusion: fusionValue(op, 'F', f.value), before: f.value, now });
    }
    for (const s of op.speeds.keys()) {
      const now = e.speeds?.get(s) ?? s;
      values.push({ what: 'Spindle speed', unit: 'rpm', letter: 'S', fusion: fusionValue(op, 'S', s), before: s, now });
    }
    for (const q of op.pecks.keys()) {
      const now = e.pecks?.get(q) ?? q;
      values.push({ what: 'Peck depth', unit: a.units, letter: 'Q', fusion: fusionValue(op, 'Q', q), before: q, now });
    }
    const steps = passSteps(op, e.top);
    if (steps.length) {
      const before = Math.max(...steps.map((s) => s.step));
      const now = e.maxStep && e.maxStep < before - 1e-4
        ? Math.max(...splitPlan(op, e.top, e.maxStep).map((p) => p.step / p.n)) : before;
      const fusion = op.fusion?.D && sameNum(op.fusion.D[1], before) ? op.fusion.D[0] : before;
      values.push({ what: 'Max. stepdown', unit: a.units, letter: 'D', fusion, before, now,
                    passes: splitPlan(op, e.top, e.maxStep).reduce((s, p) => s + p.n, 0), passesBefore: steps.length });
    }
    const t = a.tools[op.tool];
    return { op: k + 1, name: op.name, toolNum: op.tool,
             tool: t ? t.text : op.tool !== null ? `T${op.tool}` : '', values };
  });
}

function adjustmentHeader(a, edits, info) {
  const d = info.date || new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const when = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const base = cleanText(a.base || info.source || '');
  const h = [`${HEADER_START} ${when} from ${cleanText(info.source || '')} ---)`,
             `(Values: in Fusion -> in this file)`];
  const data = [`(tune base ${base})`];
  for (const s of summarize(a, edits)) {
    h.push(`(Op ${s.op}: ${cleanText(s.name)}${s.tool ? ' - ' + cleanText(s.tool) : ''})`);
    const parts = [];
    for (const v of s.values) {
      const changed = !sameNum(v.fusion, v.now);
      h.push(`(  ${v.what}: ${fmtNum(v.fusion, 4)}${changed ? ' -> ' + fmtNum(v.now, 4) : ''} ${v.unit}${changed ? '' : ' (unchanged)'})`);
      parts.push(`${v.letter}${fmtNum(v.fusion, 4)}>${fmtNum(v.now, 4)}`);
    }
    data.push(`(tune o${s.op} ${parts.join(' ')})`);
  }
  return [...h, ...data, HEADER_END];
}
