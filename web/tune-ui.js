// "Feeds & steps" window (change feeds, spindle speed, peck depth and step height of a program on
// the Pi, saved as a new file) and the Feeds tab (what was changed, and what worked).
// Nothing here talks to TNG: the new file is loaded in TNG at the machine.
import { analyze, apply, fmtNum, splitPlan, defaultTop, summarize } from './tune.js';
import { log } from './log.js';

const $ = (id) => document.getElementById(id);
const esc = (t) => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const writeHeaders = { 'X-CNC-Control': '1', 'Content-Type': 'application/json' };
const STEPS = [-20, -10, 10, 20];

let hooks = {};
let cur = null;      // {path, name, folder, a, edits, stoppedLine}

export function initTune(h) {
  hooks = h;
  $('tuneClose').addEventListener('click', () => $('tuneDlg').close());
  $('tuneSave').addEventListener('click', save);
  $('tunePreview').addEventListener('click', () => {
    if (!cur) return;
    hooks.openPreview($('tuneName').value || cur.name, build().text);
    $('tuneDlg').close();
  });
  $('tuneOps').addEventListener('input', onInput);
  $('tuneOps').addEventListener('click', onClick);
  $('tuneList').addEventListener('click', onLogClick);
  $('tuneList').addEventListener('change', onLogChange);
}

// ---------------------------------------------------------------- the window

/** Open the window for a file in the Files folder (path relative to it). */
export async function openTune(path, stoppedLine = 0) {
  const name = path.split('/').pop();
  const folder = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
  $('tuneTitle').textContent = name;
  $('tuneOps').innerHTML = '<p class="muted">Reading the program…</p>';
  $('tuneMsg').textContent = '';
  $('tuneMsg').className = 'tune-msg';
  $('tuneSave').disabled = true;
  $('tunePreview').disabled = true;
  if (!$('tuneDlg').open) $('tuneDlg').showModal();
  let text;
  try {
    const r = await fetch('/api/files/download?inline=1&path=' + encodeURIComponent(path), { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    text = await r.text();
  } catch (e) {
    $('tuneOps').innerHTML = `<p class="tune-err">Cannot read ${esc(name)}: ${esc(e.message)}</p>`;
    return;
  }
  let a;
  try {
    a = analyze(text);
  } catch (e) {
    log.error('feeds: cannot read the program:', e);
    $('tuneOps').innerHTML = `<p class="tune-err">Cannot read this program: ${esc(e.message)}</p>`;
    return;
  }
  if (!a.ops.length) {
    $('tuneOps').innerHTML = '<p class="tune-err">No feed moves found in this file.</p>';
    return;
  }
  const edits = a.ops.map((op) => ({ feeds: new Map(), speeds: new Map(), pecks: new Map(), maxStep: null,
                                     top: defaultTop(op) }));
  cur = { path, name, folder, a, edits, stoppedLine };
  render();
  $('tuneName').value = await freeName(folder, name);
  $('tuneSave').disabled = false;
  $('tunePreview').disabled = false;
}

const tone = (op) => {
  const t = cur.a.tools[op.tool];
  if (!t) return op.tool !== null ? `T${op.tool}` : '';
  return `T${t.num} · Ø${fmtNum(t.d)} ${cur.a.units} ${t.kind}`.trim();
};

function stoppedOp() {
  if (!cur.stoppedLine) return -1;
  const i = cur.stoppedLine - 1;   // 1-based line -> index
  return cur.a.ops.findIndex((op) => i >= op.start && i <= op.end);
}

function render() {
  const { a, edits } = cur;
  const fu = a.units === 'in' ? 'in/min' : 'mm/min';
  const stopped = stoppedOp();
  let html = '';
  if (a.header) {
    html += `<p class="tune-note">This file was already adjusted here${a.base ? ` (from ${esc(a.base)})` : ''}. ` +
      'The Fusion column shows the values from the original program.</p>';
  }
  if (stopped >= 0) {
    html += `<p class="tune-note warn">The job stopped at line ${cur.stoppedLine}, in Op ${stopped + 1} · ${esc(a.ops[stopped].name)}.</p>`;
  }
  a.ops.forEach((op, k) => {
    const e = edits[k];
    const rows = [];
    const row = (kind, key, label, before, val, unit, fusion) => {
      const changed = !sameVal(val, before);
      rows.push(`<tr class="${changed ? 'changed' : ''}"><th>${esc(label)}</th>
        <td class="num">${fmtNum(before, 4)}</td>
        <td><input type="number" inputmode="decimal" min="0" step="any" data-op="${k}" data-kind="${kind}" data-key="${key}"
             value="${fmtNum(val, 4)}" aria-label="${esc(label)}, new value"></td>
        <td class="unit">${unit}</td>
        <td class="num fusion">${fusion !== undefined && !sameVal(fusion, before) ? fmtNum(fusion, 4) : ''}</td></tr>`);
    };
    const fusionOf = (letter, v) => (op.fusion?.[letter] || []).find(([, now]) => sameVal(now, v))?.[0];
    for (const f of op.feedList) {
      row('feeds', f.value, f.role, f.value, e.feeds.get(f.value) ?? f.value, fu, fusionOf('F', f.value));
    }
    for (const s of op.speeds.keys()) row('speeds', s, 'Spindle speed', s, e.speeds.get(s) ?? s, 'rpm', fusionOf('S', s));
    for (const q of op.pecks.keys()) row('pecks', q, 'Peck depth', q, e.pecks.get(q) ?? q, a.units, fusionOf('Q', q));

    html += `<section class="tune-op${k === stopped ? ' stopped' : ''}" data-op="${k}">
      <header><b>Op ${k + 1} · ${esc(op.name)}</b><span class="muted">${esc(tone(op))}</span></header>
      <div class="tune-quick"><span class="muted">All feeds</span>
        ${STEPS.map((p) => `<button class="vbtn" data-op="${k}" data-pct="${p}">${p > 0 ? '+' : '−'}${Math.abs(p)} %</button>`).join('')}
        <button class="vbtn" data-op="${k}" data-reset="1" title="Back to the values in the file">Reset</button></div>
      <table class="tune-table"><thead><tr><th></th><th class="num">In file</th><th>New</th><th></th>
        <th class="num fusion">${op.fusion ? 'Fusion' : ''}</th></tr></thead><tbody>${rows.join('')}</tbody></table>
      ${stepHtml(op, k)}
    </section>`;
  });
  $('tuneOps').innerHTML = html;
}

function stepHtml(op, k) {
  const { a, edits } = cur;
  const e = edits[k];
  if (!op.passes.runs) {
    return `<p class="tune-step muted">Step height: ${esc(op.passes.problem)}</p>`;
  }
  const plan = splitPlan(op, e.top, e.maxStep);
  const before = Math.max(...plan.map((p) => p.step));
  const passes = plan.reduce((s, p) => s + p.n, 0);
  const now = Math.max(...plan.map((p) => p.step / p.n));
  const levels = op.passes.runs.map((r) => fmtNum(r.z, 3)).join(', ');
  const fusionD = op.fusion?.D && sameVal(op.fusion.D[1], before) && !sameVal(op.fusion.D[0], before) ? op.fusion.D[0] : null;
  return `<div class="tune-step">
    <div class="muted">${plan.length} pass${plan.length === 1 ? '' : 'es'} at Z ${levels} ${a.units}; biggest step ${fmtNum(before, 3)} ${a.units}${
      fusionD !== null ? ` (Fusion: ${fmtNum(fusionD, 3)})` : ''}</div>
    <div class="tune-step-row">
      <label>Max. step <input type="number" inputmode="decimal" min="0" step="any" data-op="${k}" data-kind="maxStep"
        value="${e.maxStep ? fmtNum(e.maxStep, 4) : fmtNum(before, 4)}"> ${a.units}</label>
      <label>Top of material Z <input type="number" inputmode="decimal" step="any" data-op="${k}" data-kind="top"
        value="${e.top === null ? '' : fmtNum(e.top, 4)}" placeholder="?"> ${a.units}</label>
    </div>
    <div class="tune-step-res ${passes > plan.length ? 'changed' : ''}">${passes > plan.length
      ? `Becomes ${passes} passes of at most ${fmtNum(now, 3)} ${a.units}: extra passes are added above the original ones.`
      : e.top === null ? 'Enter the top of the material to change the step.' : 'Only smaller steps are possible here.'}</div>
  </div>`;
}

const sameVal = (a, b) => Math.abs(a - b) < 1e-9 * Math.max(1, Math.abs(a));

function onInput(ev) {
  const t = ev.target;
  if (!t.dataset.kind || !cur) return;
  const k = +t.dataset.op;
  const e = cur.edits[k];
  const v = t.value === '' ? null : parseFloat(t.value);
  const kind = t.dataset.kind;
  if (kind === 'maxStep') e.maxStep = v > 0 ? v : null;
  else if (kind === 'top') e.top = v;
  else {
    const key = parseFloat(t.dataset.key);
    if (v > 0 && !sameVal(v, key)) e[kind].set(key, v); else e[kind].delete(key);
    t.closest('tr').classList.toggle('changed', e[kind].has(key));
    return;
  }
  // Step inputs: redraw the step part only, keeping the focus in the field being typed in.
  const sec = t.closest('.tune-op');
  const old = sec.querySelector('.tune-step');
  const tmp = document.createElement('div');
  tmp.innerHTML = stepHtml(cur.a.ops[k], k);
  old.querySelector('.tune-step-res').replaceWith(tmp.querySelector('.tune-step-res'));
}

function onClick(ev) {
  const b = ev.target.closest('button');
  if (!b || !cur) return;
  const k = +b.dataset.op;
  const op = cur.a.ops[k];
  const e = cur.edits[k];
  if (b.dataset.reset) {
    cur.edits[k] = { feeds: new Map(), speeds: new Map(), pecks: new Map(), maxStep: null, top: defaultTop(op) };
  } else if (b.dataset.pct) {
    const f = 1 + b.dataset.pct / 100;
    for (const fe of op.feedList) {
      const v = Math.round((e.feeds.get(fe.value) ?? fe.value) * f);
      if (sameVal(v, fe.value)) e.feeds.delete(fe.value); else e.feeds.set(fe.value, v);
    }
  } else return;
  render();
}

function build() {
  return apply(cur.a, cur.edits, { source: cur.name, date: new Date() });
}

/** name.nc -> name_adj1.nc, or the next number not in the folder yet. */
async function freeName(folder, name) {
  let names = new Set();
  try {
    const d = await (await fetch('/api/files?path=' + encodeURIComponent(folder), { cache: 'no-store' })).json();
    names = new Set((d.entries || []).map((e) => e.name.toLowerCase()));
  } catch { /* guess */ }
  const dot = name.lastIndexOf('.');
  const ext = dot > 0 ? name.slice(dot) : '.nc';
  const stem = (dot > 0 ? name.slice(0, dot) : name).replace(/_adj\d+$/i, '');
  for (let n = 1; ; n++) {
    const c = `${stem}_adj${n}${ext}`;
    if (!names.has(c.toLowerCase())) return c;
  }
}

function message(text, kind = '') {
  $('tuneMsg').innerHTML = text;
  $('tuneMsg').className = 'tune-msg' + (kind ? ' ' + kind : '');
}

async function save() {
  if (!cur) return;
  const name = $('tuneName').value.trim();
  if (!name || /[\\/<>:"|?*]/.test(name) || name.startsWith('.')) { message('Enter a valid file name.', 'err'); return; }
  if (name.toLowerCase() === cur.name.toLowerCase()) { message('Choose a new name: the original file is kept as it is.', 'err'); return; }
  const changes = summarize(cur.a, cur.edits).some((s) => s.values.some((v) => !sameVal(v.before, v.now)));
  if (!changes && !confirm('Nothing was changed. Save a copy anyway?')) return;
  let result;
  try {
    result = build();
  } catch (e) {
    log.error('feeds: building the new program failed:', e);
    message('Could not build the new program: ' + esc(e.message), 'err');
    return;
  }
  $('tuneSave').disabled = true;
  message('Saving…');
  try {
    const url = `/api/files/upload?path=${encodeURIComponent(cur.folder)}&name=${encodeURIComponent(name)}`;
    const r = await fetch(url, { method: 'POST', headers: { 'X-CNC-Control': '1', 'Content-Type': 'application/octet-stream' },
                                 body: result.text });
    const res = await r.json().catch(() => ({ ok: false, message: `HTTP ${r.status}` }));
    if (!res.ok) throw new Error(res.exists ? `${name} already exists: choose another name` : res.message);
    const path = (cur.folder ? cur.folder + '/' : '') + name;
    const ops = result.summary.map((s) => ({ ...s, startLine: result.opLines[s.op - 1] || 0 }));
    const r2 = await fetch('/api/tuning', { method: 'POST', headers: writeHeaders, body: JSON.stringify({
      action: 'add', path, source: cur.name, base: cur.a.base || cur.name, units: cur.a.units, ops }) });
    const res2 = await r2.json().catch(() => ({ ok: false }));
    const stopped = stoppedOp();
    const startHint = stopped >= 0 && result.opLines[stopped]
      ? ` Op ${stopped + 1} starts at line ${result.opLines[stopped]} of the new file.` : '';
    message(`Saved <b>${esc(name)}</b>. Load it in TNG at the machine.${startHint}` +
      (res2.ok ? '' : ' (It could not be added to the Feeds log.)'), 'ok');
    log.info(`feeds: saved ${path} from ${cur.name}`);
    hooks.filesReload?.();
    $('tuneName').value = await freeName(cur.folder, cur.name);
  } catch (e) {
    message('Not saved: ' + esc(e.message), 'err');
  } finally {
    $('tuneSave').disabled = false;
  }
}

// ---------------------------------------------------------------- Feeds tab (the log)

let logEntries = [];
let logSeq = -1;

export async function tuneLogLoad(force = false, seq = null) {
  if (!force && seq !== null && seq === logSeq) return;
  try {
    const d = await (await fetch('/api/tuning', { cache: 'no-store' })).json();
    logEntries = d.entries || [];
    logSeq = d.seq;
  } catch {
    $('tuneList').innerHTML = '<p class="tune-err">Server unreachable</p>';
    return;
  }
  renderLog();
}

const fmtWhen = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
const fmtDur = (s) => { s = Math.round(s); return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; };

function changedValues(op) {
  return (op.values || []).filter((v) => !sameVal(v.fusion, v.now));
}

function valueText(v) {
  return `${esc(v.what)}: <b>${fmtNum(v.now, 4)}</b> ${esc(v.unit)} <span class="muted">(Fusion ${fmtNum(v.fusion, 4)})</span>`;
}

function renderLog() {
  const loaded = hooks.loadedFile?.() || '';
  if (!logEntries.length) {
    $('tuneList').innerHTML = `<p class="muted tune-empty">Nothing adjusted yet. In the Files tab, open a program's
      <b>Feeds &amp; steps</b> window (the sliders button) to change its feeds or step height.</p>`;
    return;
  }
  // What worked, per tool: the newest entry marked "worked" for each tool and operation.
  const best = new Map();
  for (const e of logEntries) {
    if (e.status !== 'good') continue;
    for (const op of e.ops || []) {
      if (!changedValues(op).length) continue;
      best.set(`${op.tool}|${op.name}`, { e, op });
    }
  }
  let html = '';
  if (best.size) {
    html += '<h3>Worked – copy these into Fusion</h3><div class="tune-best">';
    for (const { e, op } of best.values()) {
      html += `<div class="tune-card good"><div><b>${esc(op.tool || 'Tool ?')}</b></div>
        <div class="muted">${esc(op.name)} · ${esc(e.base || e.source)} · ${fmtWhen(e.time)}</div>
        <ul>${changedValues(op).map((v) => `<li>${valueText(v)}</li>`).join('')}</ul></div>`;
    }
    html += '</div>';
  }
  html += '<h3>Adjusted files</h3>';
  for (const e of [...logEntries].reverse()) {
    const isLoaded = loaded && loaded === e.path;
    const runs = (e.runs || []).map((r) => r.done
      ? `<li class="ok">Ran to the end ${fmtWhen(r.end)} (${fmtDur(r.end - r.start)})</li>`
      : `<li class="warn">Stopped at line ${r.line} of ${r.lines} ${fmtWhen(r.end)} (${fmtDur(r.end - r.start)})</li>`).join('');
    const ops = (e.ops || []).map((op) => {
      const ch = changedValues(op);
      if (!ch.length) return '';
      return `<li><b>Op ${op.op} · ${esc(op.name)}</b> <span class="muted">${esc(op.tool)}</span>
        <ul>${ch.map((v) => `<li>${valueText(v)}</li>`).join('')}</ul></li>`;
    }).join('');
    html += `<div class="tune-card ${e.status}${isLoaded ? ' loaded' : ''}" data-id="${esc(e.id)}">
      <div class="tune-card-head"><b>${esc(e.name)}</b>${isLoaded ? '<span class="tag">loaded in TNG</span>' : ''}
        <button class="ficon del" data-del="${esc(e.id)}" title="Remove from the log (the file stays)">✕</button></div>
      <div class="muted">${fmtWhen(e.time)} · from ${esc(e.source)}${e.base && e.base !== e.source ? ` (Fusion file ${esc(e.base)})` : ''}</div>
      <ul class="tune-ops">${ops || '<li class="muted">No values changed</li>'}</ul>
      ${runs ? `<ul class="tune-runs">${runs}</ul>` : '<div class="muted">Not run yet</div>'}
      <div class="tune-status" role="group" aria-label="How did it go?">
        <button class="vbtn${e.status === 'good' ? ' active' : ''}" data-status="good">Worked</button>
        <button class="vbtn${e.status === 'bad' ? ' active bad' : ''}" data-status="bad">Still too much</button>
        <button class="vbtn${!e.status ? ' active' : ''}" data-status="">Not sure yet</button>
      </div>
      <input class="tune-note-in" data-note="1" maxlength="1000" placeholder="Note (sound, chips, finish…)" value="${esc(e.note)}">
    </div>`;
  }
  $('tuneList').innerHTML = html;
}

async function post(body) {
  try {
    const r = await fetch('/api/tuning', { method: 'POST', headers: writeHeaders, body: JSON.stringify(body) });
    return await r.json().catch(() => ({ ok: false, message: `HTTP ${r.status}` }));
  } catch {
    return { ok: false, message: 'Server unreachable' };
  }
}

async function onLogClick(ev) {
  const b = ev.target.closest('button');
  if (!b) return;
  const card = b.closest('.tune-card');
  if (b.dataset.del) {
    if (!confirm('Remove this entry from the Feeds log? The file itself stays on the Pi.')) return;
    await post({ action: 'delete', id: b.dataset.del });
  } else if (b.dataset.status !== undefined && card) {
    await post({ action: 'update', id: card.dataset.id, status: b.dataset.status });
  } else return;
  tuneLogLoad(true);
}

async function onLogChange(ev) {
  const t = ev.target;
  if (!t.dataset.note) return;
  const res = await post({ action: 'update', id: t.closest('.tune-card').dataset.id, note: t.value });
  if (!res.ok) log.warn('feeds: note not saved:', res.message);
}
