import { Viewer } from './viewer.js';
import { parseGcode } from './gcode.js';

const $ = (id) => document.getElementById(id);
const ROW_H = 20;          // must match .row height in style.css
const OVERSCAN = 30;       // extra G-code rows rendered above/below the visible area
const AXES = ['X', 'Y', 'Z', 'A', 'B', 'C'];

const viewer = new Viewer($('viewport'));

let state = {};
let coordMode = 'work';
let programId = null;
let programLoading = null;
let jobStart = null;
let wasRunning = false;

// ---------------------------------------------------------------- DRO

function renderDro() {
  const vals = state[coordMode] || [];
  const decimals = state.units === 'in' ? 4 : 3;
  // Always show XYZ; rotary axes only if they're in use.
  const shown = AXES.filter((_, i) => i < 3 || Math.abs(vals[i] || 0) > 1e-9);
  const dro = $('dro');
  if (dro.childElementCount !== shown.length) {
    dro.innerHTML = shown.map((a) =>
      `<div class="axis"><span class="name">${a}</span><span class="value">0</span></div>`).join('');
  }
  shown.forEach((a, k) => {
    const v = vals[AXES.indexOf(a)] || 0;
    dro.children[k].querySelector('.value').textContent = v.toFixed(decimals);
  });
}

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {
    coordMode = tab.dataset.coord;
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    renderDro();
  });
}

// ---------------------------------------------------------------- status / job info

function fmtDuration(sec) {
  if (sec == null || !isFinite(sec) || sec < 0) return '–';
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}

function renderInfo() {
  const s = state;
  const online = !!s.bridge_online;
  let st = 'offline', label = 'OFFLINE';
  if (online) {
    if (s.estop) [st, label] = ['estop', 'E-STOP'];
    else if (s.paused) [st, label] = ['paused', 'PAUSED'];
    else if (s.running) [st, label] = ['running', 'RUNNING'];
    else [st, label] = ['idle', 'IDLE'];
  }
  const status = $('status');
  status.dataset.state = st;
  status.textContent = label;

  const file = s.program_name || s.file || '';
  $('fileName').textContent = file || 'No program loaded';
  $('fileName').title = file;

  $('feed').textContent = Math.round(s.feed || 0);
  $('feedUnit').textContent = s.units === 'in' ? 'in/min' : 'mm/min';
  $('spindle').textContent = Math.round(s.spindle || 0);
  $('tool').textContent = s.tool ?? '–';
  $('toolName').textContent = s.tool_name || '';
  $('coordsys').textContent = s.coordsys || '–';

  const total = s.line_count || codeLines.length || 0;
  const line = s.line || 0;
  const pct = total ? Math.min(100, Math.max(0, (line / total) * 100)) : 0;
  $('lineInfo').textContent = total ? `${line} / ${total}` : '– / –';
  $('pctInfo').textContent = pct.toFixed(pct < 10 ? 1 : 0) + ' %';
  $('progressFill').style.width = pct + '%';

  // Remaining: TNG's own estimate when available, else extrapolate from the
  // progress made since we started watching this job.
  let remain = typeof s.time_to_end === 'number' && s.time_to_end >= 0 ? s.time_to_end : null;
  if (remain == null && s.running && jobStart && pct - jobStart.pct > 1) {
    const elapsed = (s.server_time ?? Date.now() / 1000) - jobStart.time;
    remain = elapsed * (100 - pct) / (pct - jobStart.pct);
  }
  $('remainInfo').textContent = s.running ? fmtDuration(remain) : '–';
  $('updateInfo').textContent = online
    ? new Date((s.server_time ?? Date.now() / 1000) * 1000).toLocaleTimeString()
    : 'bridge offline';
}

// ---------------------------------------------------------------- G-code listing (virtualized)

const codeEl = $('code');
const rowsEl = $('codeRows');
let codeLines = [];
let activeLine = 0;
let followCode = true;
let rendered = { first: -1, last: -1, active: -1 };

const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const TOKEN_CLASS = { G: 'tk-g', M: 'tk-m', F: 'tk-f', S: 'tk-s', T: 'tk-t', N: 'tk-n' };
const TOKEN_RE = /(\([^)]*\)?|;.*$)|([A-Za-z])(\s*[-+]?[\d.]*)/g;

function highlight(line) {
  let out = '', last = 0, m;
  TOKEN_RE.lastIndex = 0;
  while ((m = TOKEN_RE.exec(line))) {
    out += esc(line.slice(last, m.index));
    if (m[1]) {
      out += `<span class="tk-c">${esc(m[1])}</span>`;
    } else {
      const L = m[2].toUpperCase();
      const cls = TOKEN_CLASS[L] || ('XYZABCUVWIJKRPQ'.includes(L) ? 'tk-xyz' : '');
      out += cls ? `<span class="${cls}">${esc(m[0])}</span>` : esc(m[0]);
    }
    last = TOKEN_RE.lastIndex;
  }
  return out + esc(line.slice(last));
}

function renderCode(force = false) {
  if (!codeLines.length) {
    rowsEl.innerHTML = `<div class="code-empty">${programLoading ? 'Loading program…' : 'No program loaded'}</div>`;
    rendered = { first: -1, last: -1, active: -1 };
    return;
  }
  const top = codeEl.scrollTop, h = codeEl.clientHeight;
  const first = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
  const last = Math.min(codeLines.length, Math.ceil((top + h) / ROW_H) + OVERSCAN);
  if (!force && first === rendered.first && last === rendered.last && activeLine === rendered.active) return;
  let html = '';
  for (let i = first; i < last; i++) {
    const n = i + 1;
    const cls = n === activeLine ? 'row active' : n < activeLine ? 'row done' : 'row';
    html += `<div class="${cls}"><span class="ln">${n}</span><span class="code-text">${highlight(codeLines[i])}</span></div>`;
  }
  rowsEl.style.transform = `translateY(${first * ROW_H}px)`;
  rowsEl.innerHTML = html;
  rendered = { first, last, active: activeLine };
}

function scrollToActive() {
  if (!followCode || !activeLine) return;
  const target = (activeLine - 1) * ROW_H - codeEl.clientHeight / 3;
  codeEl.scrollTop = Math.max(0, target);
}

function setActiveLine(n) {
  if (n === activeLine) return;
  activeLine = n;
  viewer.setCurrentLine(n);
  scrollToActive();
  renderCode();
}

codeEl.addEventListener('scroll', () => renderCode());
new ResizeObserver(() => renderCode()).observe(codeEl);
// Only real user input stops auto-follow (programmatic scrolls also fire 'scroll').
for (const ev of ['wheel', 'touchstart', 'pointerdown']) {
  codeEl.addEventListener(ev, () => {
    if (!followCode) return;
    followCode = false;
    $('resumeBtn').hidden = false;
  }, { passive: true });
}
$('resumeBtn').addEventListener('click', () => {
  followCode = true;
  $('resumeBtn').hidden = true;
  scrollToActive();
});

function showProgram(text) {
  codeLines = text ? text.replace(/\r?\n$/, '').split(/\r?\n/) : [];
  $('codeSpacer').style.height = codeLines.length * ROW_H + 'px';
  viewer.setProgram(text ? parseGcode(text) : null);
  activeLine = -1;
  setActiveLine(0);
}

async function loadProgram(id) {
  programId = id;
  const req = (programLoading = fetch('/api/program', { cache: 'no-store' }).then((r) => r.json()));
  codeLines = [];
  renderCode(true);
  try {
    const prog = await req;
    if (programLoading !== req || preview) return;  // a newer program arrived, or a preview took over
    showProgram(prog.text);
    setActiveLine(state.line || 0);
    if (programId !== prog.id) programId = null;  // server changed in between: refetch next update
  } catch (e) {
    console.error('program load failed', e);
    programId = null;
  } finally {
    if (programLoading === req) programLoading = null;
    renderCode(true);
  }
}

// ---------------------------------------------------------------- viewer toolbar

for (const b of document.querySelectorAll('.vbtn[data-view]')) {
  b.addEventListener('click', () => {
    document.querySelectorAll('.vbtn[data-view]').forEach((x) => x.classList.toggle('active', x === b));
    viewer.setView(b.dataset.view);
  });
}
$('fitBtn').addEventListener('click', () => viewer.fit());
$('followBtn').addEventListener('click', (e) => {
  const on = e.currentTarget.classList.toggle('active');
  viewer.setFollow(on);
});
$('traceBtn').addEventListener('click', (e) => {
  const on = e.currentTarget.classList.toggle('active');
  viewer.setTraceVisible(on);
});

// ---------------------------------------------------------------- camera

const camPanel = $('camPanel');
const camImg = $('camImg');
const camToggle = $('camToggleBtn');
let camEnabled = false;   // server has a camera
let camWanted = true;     // user hasn't hidden it
let camOpened = 0;        // when the current stream was opened
let camRetry = null;

try { camWanted = localStorage.getItem('cncCamHidden') !== '1'; } catch { /* storage unavailable */ }

function camStart() {
  clearTimeout(camRetry);
  camOpened = Date.now();
  camImg.hidden = false;
  camImg.src = '/api/camera.mjpg?t=' + camOpened;
}

function camStop() {
  clearTimeout(camRetry);
  camImg.removeAttribute('src');
  camImg.hidden = true;
}

function camShouldStream() {
  return camEnabled && camWanted && document.visibilityState === 'visible';
}

function camSync() {
  camPanel.hidden = !camEnabled;
  camPanel.classList.toggle('collapsed', !camWanted);
  camToggle.textContent = camWanted ? 'Hide' : 'Show';
  camToggle.classList.toggle('active', camWanted);
  $('camFullBtn').hidden = !camWanted;
  const streaming = camImg.hasAttribute('src');
  if (camShouldStream() && !streaming) camStart();
  else if (!camShouldStream() && streaming) camStop();
}

function camUpdate(cam) {
  const enabled = !!cam;
  if (enabled !== camEnabled) { camEnabled = enabled; camSync(); }
  if (!enabled || !camWanted) { camPanel.dataset.state = 'off'; return; }
  // The server streams a placeholder frame while there's no picture; it reports
  // whether the camera really delivers images in every state update.
  if (cam.live) {
    camPanel.dataset.state = 'live';
  } else if (Date.now() - camOpened > 4000) {
    camPanel.dataset.state = 'nosignal';
    $('camMsg').textContent = 'No camera signal';
  } else {
    camPanel.dataset.state = 'connecting';
    $('camMsg').textContent = 'Connecting…';
  }
}

camImg.addEventListener('error', () => {
  if (!camShouldStream()) return;
  camImg.hidden = true;
  camPanel.dataset.state = 'nosignal';
  $('camMsg').textContent = 'Camera stream lost – retrying…';
  camRetry = setTimeout(camStart, 3000);
});
camToggle.addEventListener('click', () => {
  camWanted = !camWanted;
  try { localStorage.setItem('cncCamHidden', camWanted ? '0' : '1'); } catch { /* ignore */ }
  camSync();
});
$('camFullBtn').addEventListener('click', () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else camPanel.requestFullscreen?.().catch(() => {});
});
$('camView').addEventListener('dblclick', () => $('camFullBtn').click());
// Don't keep a video stream running from the Pi for a background tab.
document.addEventListener('visibilitychange', camSync);

// ---------------------------------------------------------------- live updates

function onState(s) {
  state = s;

  if (s.running && !wasRunning) {
    const total = s.line_count || codeLines.length;
    jobStart = { time: s.server_time ?? Date.now() / 1000, pct: total ? (s.line || 0) / total * 100 : 0 };
    viewer.clearTrace();
  }
  if (!s.running) jobStart = null;
  wasRunning = !!s.running;

  // While a file is previewed, the live program waits; it's reloaded when the preview closes.
  if (!preview) {
    if (s.program_id && s.program_id !== programId && !programLoading) loadProgram(s.program_id);
    if (!s.program_id && programId) {
      programId = null;
      showProgram('');
      renderCode(true);
    }
  }

  const w = s.work || [];
  const m = s.machine || [];
  // The bed comes in machine coordinates; shift it by the work offset (work - machine)
  // so it lines up with the toolpath, which is in work coordinates.
  if (s.limits && w.length >= 3 && m.length >= 3) {
    const off = [0, 1, 2].map((k) => w[k] - m[k]);
    viewer.setBed({
      min: [0, 1, 2].map((k) => s.limits.min[k] + off[k]),
      max: [0, 1, 2].map((k) => s.limits.max[k] + off[k]),
    });
  } else if (s.bridge_online) {
    viewer.setBed(null);
  }
  if (w.length >= 3) {
    viewer.setToolPosition(w[0], w[1], w[2], !!(s.bridge_online && s.running && !s.paused));
  }
  if (!programLoading && !preview) setActiveLine(s.line || 0);

  renderDro();
  renderInfo();
  renderControls();
  camUpdate(s.camera);
}

// ---------------------------------------------------------------- file preview

let preview = null;   // {name, text} of a local file shown instead of TNG's program

$('openBtn').addEventListener('click', () => $('fileInput').click());
$('fileInput').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  e.target.value = '';  // allow re-opening the same file after editing it
  if (!file) return;
  preview = { name: file.name, text: await file.text() };
  programLoading = null;
  showProgram(preview.text);
  renderCode(true);
  $('previewName').textContent = file.name;
  $('previewBar').hidden = false;
  renderControls();
});

function closePreview() {
  preview = null;
  $('previewBar').hidden = true;
  programId = null;   // makes the next update reload TNG's program
  showProgram('');
  renderCode(true);
  renderControls();
}
$('closePreviewBtn').addEventListener('click', closePreview);

$('loadTngBtn').addEventListener('click', async () => {
  if (!preview) return;
  const { name, text } = preview;
  if (!confirm(`Load "${name}" as the program in TNG?\n\nIt replaces the program currently loaded in TNG. It does not start it.`)) return;
  const res = await control({ action: 'load', name, text });
  if (res.ok) closePreview();
});

// ---------------------------------------------------------------- machine control

const ctlEl = $('ctl');
const RELOCK_MS = 2 * 60 * 1000;
let unlocked = false;
let relockTimer = null;
let busy = false;     // a command is waiting for TNG's answer
let jogStep = 1;

function setUnlocked(on) {
  unlocked = on;
  clearTimeout(relockTimer);
  if (on) relockTimer = setTimeout(() => setUnlocked(false), RELOCK_MS);
  renderControls();
}

function ctlMessage(text, kind = '') {
  const m = $('ctlMsg');
  m.textContent = text;
  m.className = 'ctl-msg' + (kind ? ' ' + kind : '');
}

function renderControls() {
  const s = state;
  const avail = s.controls || {};
  const off = !s.control_enabled;
  ctlEl.dataset.off = off ? '1' : '';
  ctlEl.dataset.locked = unlocked ? '' : '1';
  $('ctlLock').textContent = unlocked ? 'Lock' : 'Unlock';
  $('ctlLock').hidden = off;
  $('stepUnit').textContent = s.units === 'in' ? 'in' : 'mm';

  const online = !!s.bridge_online;
  const idle = online && !s.running && !s.estop;
  const why = off ? 'Machine control is off: start the server with a dashboard password (--viewer-password).'
    : !online ? 'TNG bridge offline.'
    : s.estop ? 'E-stop is active.'
    : s.running ? 'A program is running – only Stop is available.' : '';
  for (const b of document.querySelectorAll('.jbtn')) b.disabled = busy || !idle || !avail.jog;
  $('homeBtn').disabled = busy || !idle || !avail.home;
  $('stopBtn').disabled = off || !online || !avail.stop;
  $('loadTngBtn').disabled = busy || off || !idle || !avail.load;
  $('loadTngBtn').title = off ? 'Needs a dashboard password (--viewer-password)'
    : !avail.load ? 'TNG bridge offline or command not available' : 'Make this the loaded program in TNG (does not start it)';
  // Show why controls are disabled; clear that note (but not command results) once it no longer applies.
  if (why && !busy) ctlMessage(why);
  else if (!why && shownWhy && $('ctlMsg').textContent === shownWhy) ctlMessage('');
  shownWhy = why;
}
let shownWhy = '';

async function control(cmd) {
  busy = true;
  renderControls();
  ctlMessage(cmd.action === 'load' ? 'Sending program to TNG…' : 'Sending…');
  let res;
  try {
    const r = await fetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CNC-Control': '1' },
      body: JSON.stringify(cmd),
    });
    res = await r.json().catch(() => ({ ok: false, message: `HTTP ${r.status}` }));
  } catch (e) {
    res = { ok: false, message: 'Server unreachable' };
  }
  busy = false;
  if (unlocked) setUnlocked(true);  // any use restarts the auto-lock timer
  ctlMessage(res.ok ? (res.message || 'Done') : (res.message || 'Failed'), res.ok ? 'ok' : 'err');
  renderControls();
  return res;
}

$('ctlLock').addEventListener('click', () => setUnlocked(!unlocked));
for (const b of document.querySelectorAll('.step')) {
  b.addEventListener('click', () => {
    jogStep = parseFloat(b.dataset.step);
    document.querySelectorAll('.step').forEach((x) => x.classList.toggle('active', x === b));
  });
}
for (const b of document.querySelectorAll('.jbtn')) {
  b.addEventListener('click', () => {
    if (unlocked && !busy) control({ action: 'jog', axis: b.dataset.axis, dist: jogStep * parseFloat(b.dataset.dir) });
  });
}
$('homeBtn').addEventListener('click', () => {
  if (unlocked && confirm('Home the machine now?\n\nAll axes will move to their home switches.')) control({ action: 'home' });
});
$('stopBtn').addEventListener('click', () => control({ action: 'stop' }));

function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = (e) => {
    try { onState(JSON.parse(e.data)); } catch (err) { console.error(err); }
  };
  es.onopen = () => {
    // After a server restart the old camera stream is dead: reopen it.
    if (camShouldStream()) camStart();
  };
  es.onerror = () => {
    state = { ...state, bridge_online: false };
    renderInfo();
    // EventSource reconnects by itself unless the server answered with an error.
    if (es.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
  };
}

renderDro();
renderCode(true);
connect();
