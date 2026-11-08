import { Viewer } from './viewer.js';
import { buildToolpath, parseTools } from './gcode.js';
import { log } from './log.js';
import { initTune, openTune, tuneLogLoad } from './tune-ui.js';

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
let bedOffset = null;   // work - machine, measured while idle (see the bed in onState)

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
  $('toolName').textContent = [s.tool_name, toolShown].filter(Boolean).join(' · ');
  $('toolName').title = $('toolName').textContent;
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
  // Since the job started (pauses included); the server keeps the start time.
  const started = s.running && typeof s.job_started === 'number' ? s.job_started : null;
  $('startInfo').textContent = started
    ? new Date(started * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '–';
  $('elapsedInfo').textContent = started ? fmtDuration((s.server_time ?? Date.now() / 1000) - started) : '–';

  updateToolShape();
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

const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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

// The 3D view shows the simplified toolpath, built in a worker (big programs take a second or two).
let pathSeq = 0;
let pathText = '';
let pathStarted = 0;
let pathWorker = null;
try {
  pathWorker = new Worker(new URL('./toolpath-worker.js', import.meta.url), { type: 'module' });
  pathWorker.onmessage = (e) => { if (e.data.seq === pathSeq) setToolpath(e.data.path); };
  pathWorker.onerror = (e) => {
    // The worker couldn't start (old browser): do it here instead.
    log.warn(`toolpath worker failed (${e.message || 'no details'}), building the toolpath on the main thread`);
    pathWorker.terminate();
    pathWorker = null;
    showToolpath(pathText);
  };
} catch (e) {
  log.warn('no module worker, building the toolpath on the main thread:', e);
}

function setToolpath(path) {
  if (path) {
    log.info(`toolpath: ${path.count} of ${path.rawCount} moves drawn (within ${path.tolerance.toPrecision(2)}), ` +
      `built in ${Math.round(performance.now() - pathStarted)} ms`);
  } else if (pathText) {
    log.warn('toolpath: the program has no moves the preview understands');
  }
  viewer.setProgram(path);
  const k = (n) => n >= 1e4 ? Math.round(n / 1000) + 'k' : String(n);
  const unit = path?.inches ? ' in' : ' mm';
  $('pathNote').textContent = !path ? ''
    : path.count < path.rawCount ? `${k(path.count)} of ${k(path.rawCount)} moves drawn (within ${+path.tolerance.toPrecision(2)}${unit})`
    : `${k(path.count)} moves`;
}

function showToolpath(text) {
  const seq = ++pathSeq;
  pathText = text;
  pathStarted = performance.now();
  if (!text) { setToolpath(null); return; }
  if (pathWorker) { pathWorker.postMessage({ seq, text }); return; }
  setTimeout(() => {
    if (seq !== pathSeq) return;
    try {
      setToolpath(buildToolpath(text));
    } catch (e) {
      log.error('toolpath failed:', e);
    }
  });
}

// Tools the shown program uses (from its CAM tool comments) and where it selects each one;
// read again when the program or the CAM program chosen in Settings changes.
let programTools = null;
let toolShown = '';   // the drawn tool's size and source, shown under the tool number
let programToolsCam = '';

/** Draw the tool in use at the current line (Settings → Tool in the 3D view): its size from the
 *  program's tool comments, else the diameter TNG reports for the loaded tool, else the
 *  standard marker. Turned off in Settings: always the standard marker. */
function updateToolShape() {
  const view = state.tool_view || { enabled: true, cam: 'fusion' };
  if (!view.enabled) { viewer.setToolShape(null); return; }
  if (!programTools || programToolsCam !== view.cam) {
    programTools = parseTools(codeLines, view.cam);
    programToolsCam = view.cam;
  }
  const { tools, changes } = programTools;
  const line = state.line || 0;
  let num = null;
  for (const [row, t] of changes) {
    if (row > line && num != null) break;
    num = t;   // before the first T word, show the tool the program is about to use
  }
  let shape = num != null ? tools[num] : null;
  let from = 'program';
  if (!shape?.d && state.tool_diameter > 0) {
    // TNG's tool table is in TNG's units; the 3D view is in the program's units.
    let d = state.tool_diameter;
    const progInches = !!viewer.programInches;
    if (progInches && state.units === 'mm') d /= 25.4;
    else if (!progInches && state.units === 'in') d *= 25.4;
    shape = { d, cr: 0, tipAngle: 0, kind: '' };
    from = 'TNG';
  }
  viewer.setToolShape(shape?.d > 0 ? shape : null);
  // Say which size is drawn and where it came from, so a wrong one can be spotted.
  const unit = viewer.programInches ? 'in' : 'mm';
  toolShown = shape?.d > 0 ? `Ø${+shape.d.toFixed(3)} ${unit} (${from})` : '';
  $('toolName').textContent = [state.tool_name, toolShown].filter(Boolean).join(' · ');
}

function showProgram(text) {
  codeLines = text ? text.replace(/\r?\n$/, '').split(/\r?\n/) : [];
  programTools = null;
  updateToolShape();
  $('codeSpacer').style.height = codeLines.length * ROW_H + 'px';
  showToolpath(text);
  activeLine = -1;
  setActiveLine(0);
}

async function loadProgram(id) {
  programId = id;
  const started = performance.now();
  const req = (programLoading = fetch('/api/program', { cache: 'no-store' }).then((r) => r.json()));
  codeLines = [];
  renderCode(true);
  try {
    const prog = await req;
    if (programLoading !== req || preview) return;  // a newer program arrived, or a preview took over
    log.info(`program loaded: ${prog.name || '(none)'}, ${Math.round((prog.text || '').length / 1024)} KB ` +
      `in ${Math.round(performance.now() - started)} ms`);
    showProgram(prog.text);
    setActiveLine(state.line || 0);
    if (programId !== prog.id) programId = null;  // server changed in between: refetch next update
  } catch (e) {
    log.error('program load failed:', e);
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
let liveDownSince = null; // the live updates stopped; the camera waits so its connection is free

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
  return camEnabled && camWanted && document.visibilityState === 'visible' && !liveDownSince;
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
  log.warn(`camera stream failed after ${Math.round((Date.now() - camOpened) / 1000)} s, retrying in 3 s`);
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
  log.setDebug(s.log_debug);

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
  // Work and machine positions aren't sampled at exactly the same instant, so while the
  // machine moves their difference jitters and the box would wobble. The offset only
  // really changes while standing still (zeroing, G54/G55, tool length), so it's only
  // re-measured when TNG reports the machine idle.
  if (w.length >= 3 && m.length >= 3 && (s.idle || !bedOffset)) {
    bedOffset = [0, 1, 2].map((k) => w[k] - m[k]);
  }
  if (s.limits && bedOffset) {
    const off = bedOffset;
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

  renderName(s.machine_name);
  renderDro();
  renderInfo();
  filesUpdate(s);
  tuneSync(s);
  renderSystem(s.system);
  consoleUpdate(s.console_seq);
  updateBadge(s.update);
  camUpdate(s.camera);
}

// ---------------------------------------------------------------- file preview

let preview = null;   // {name, text} of a local file shown instead of TNG's program

$('openBtn').addEventListener('click', () => $('fileInput').click());
$('fileInput').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  e.target.value = '';  // allow re-opening the same file after editing it
  if (file) openPreview(file.name, await file.text());
});

function openPreview(name, text) {
  preview = { name, text };
  programLoading = null;
  showProgram(text);
  renderCode(true);
  $('previewName').textContent = name;
  $('previewBar').hidden = false;
}

function closePreview() {
  preview = null;
  $('previewBar').hidden = true;
  programId = null;   // makes the next update reload TNG's program
  showProgram('');
  renderCode(true);
}
$('closePreviewBtn').addEventListener('click', closePreview);

// ---------------------------------------------------------------- Pi files panel (--files-dir)

const filesPanel = $('filesPanel');
const GCODE_RE = /\.(nc|ngc|gcode|gc|tap|cnc|txt)$/i;
const ICONS = {
  view: '<svg viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M12 4v12m0 0-5-5m5 5 5-5M5 20h14"/></svg>',
  del: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
  tune: '<svg viewBox="0 0 24 24"><path d="M4 6h9m4 0h3M4 12h3m4 0h9M4 18h11m4 0h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/></svg>',
};
let filesPath = '';
let filesAbs = '';         // the open folder's full path on the Pi
let filesRoot = 'Files';
let filesEnabled = false;
let filesBusy = false;     // an upload is running
let dropTimer = null;

const fmtSize = (n) => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(0) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const fmtDate = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
const joinPath = (dir, name) => (dir ? dir + '/' : '') + name;

function filesMessage(text, kind = '') {
  if (kind === 'err') log.warn('files:', text);
  $('filesMsg').textContent = text;
  $('filesMsg').className = 'files-msg' + (kind ? ' ' + kind : '');
}

/** Drop overlay state: '' (hidden), over, busy, done, error. */
function dropState(st, text = '', progress = 0) {
  clearTimeout(dropTimer);
  filesPanel.dataset.drop = st;
  $('filesDropText').textContent = text;
  $('filesDropBar').style.width = Math.round(progress * 100) + '%';
  if (st === 'done' || st === 'error') dropTimer = setTimeout(() => dropState(''), st === 'done' ? 1400 : 2600);
}

function filesSync() {
  $('filesTab').hidden = !filesEnabled;
  $('feedsTab').hidden = !filesEnabled;
  if (filesEnabled && (savedTab === 'files' || savedTab === 'feeds')) setTab(savedTab);
  else if (!filesEnabled && (activeTab === 'files' || activeTab === 'feeds')) setTab('code');
}

// ---------------------------------------------------------------- Feeds & steps (tune-ui.js)

/** Line where the job in TNG stopped, when `path` is the program loaded in TNG and it isn't running. */
function stoppedLineFor(path) {
  return state.file_rel && state.file_rel === path && !state.running && state.line > 1 ? state.line : 0;
}
initTune({
  openPreview: (name, text) => openPreview(name, text),
  filesReload: () => { if (activeTab === 'files') filesLoad(filesPath); },
  loadedFile: () => state.file_rel || '',
});
$('tuneLoadedBtn').addEventListener('click', () => {
  if (state.file_rel) openTune(state.file_rel, stoppedLineFor(state.file_rel));
});
function tuneSync(s) {
  $('tuneLoadedBtn').hidden = activeTab !== 'code' || !s.file_rel || !!preview;
  if (activeTab === 'feeds') tuneLogLoad(false, s.tuning_seq);
}

function filesUpdate(s) {
  const enabled = !!s.files_enabled;
  if (enabled !== filesEnabled) {
    filesEnabled = enabled;
    filesSync();
  }
}

async function filesLoad(path) {
  let d;
  try {
    d = await (await fetch('/api/files?path=' + encodeURIComponent(path), { cache: 'no-store' })).json();
  } catch {
    d = { ok: false, message: 'Server unreachable' };
  }
  if (!d.ok) {
    if (path) return filesLoad('');  // folder vanished: back to the top
    filesMessage(d.message || 'Cannot read folder', 'err');
    return;
  }
  filesPath = d.path;
  filesRoot = d.root;
  filesAbs = d.abs || '';
  $('filesDirOpenBtn').disabled = !filesAbs || filesAbs === filesDir;

  const parts = d.path ? d.path.split('/') : [];
  let crumbs = `<button data-path="" title="${esc(d.root)}">${esc(d.root)}</button>`;
  parts.forEach((p, i) => {
    crumbs += `<span>/</span><button data-path="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</button>`;
  });
  $('filesCrumbs').innerHTML = crumbs;

  const rows = d.entries.map((e) => {
    const rel = joinPath(d.path, e.name);
    const p = esc(rel);
    if (e.dir) {
      return `<div class="frow"><span class="fname"><button data-open="${p}">📁 ${esc(e.name)}</button></span></div>`;
    }
    const acts = [
      GCODE_RE.test(e.name) ? `<button class="ficon" data-preview="${p}" title="Preview in 3D view">${ICONS.view}</button>` : '',
      GCODE_RE.test(e.name) ? `<button class="ficon" data-tune="${p}" title="Feeds &amp; steps: change feeds and step height (saved as a new file)">${ICONS.tune}</button>` : '',
      `<a class="ficon" href="/api/files/download?path=${encodeURIComponent(rel)}" download title="Download">${ICONS.down}</a>`,
      `<button class="ficon del" data-delete="${p}" title="Delete">${ICONS.del}</button>`,
    ].join('');
    return `<div class="frow" title="${esc(e.name)} – ${fmtDate(e.mtime)}"><span class="fname">${esc(e.name)}</span>
      <span class="fsize">${fmtSize(e.size)}</span><span class="facts">${acts}</span></div>`;
  });
  $('filesList').innerHTML = rows.join('') || '<div class="files-empty">Empty folder – drop files here</div>';
  if (!$('filesMsg').classList.contains('err')) {
    filesMessage(`${d.entries.length} item${d.entries.length === 1 ? '' : 's'} · drop files to upload`);
  }
}

function uploadOne(url, file, onProgress) {
  // XMLHttpRequest instead of fetch: it reports upload progress for the progress bar.
  return new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open('POST', url);
    x.setRequestHeader('X-CNC-Control', '1');
    x.setRequestHeader('Content-Type', 'application/octet-stream');
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    x.onload = () => {
      try { resolve(JSON.parse(x.responseText)); } catch { resolve({ ok: false, message: `HTTP ${x.status}` }); }
    };
    x.onerror = () => resolve({ ok: false, message: 'Server unreachable' });
    x.send(file);
  });
}

async function filesUpload(fileList) {
  if (filesBusy) return;
  filesBusy = true;
  const folder = filesPath;
  let done = 0, error = null;
  for (const f of fileList) {
    const label = fileList.length > 1 ? `${f.name} (${done + 1}/${fileList.length})` : f.name;
    const url = (overwrite) => `/api/files/upload?path=${encodeURIComponent(folder)}&name=${encodeURIComponent(f.name)}`
      + (overwrite ? '&overwrite=1' : '');
    const progress = (p) => dropState('busy', `Uploading ${label}`, p);
    progress(0);
    let res = await uploadOne(url(false), f, progress);
    if (res.exists) {
      dropState('');
      if (!confirm(`"${f.name}" already exists on the Pi. Replace it?`)) continue;
      progress(0);
      res = await uploadOne(url(true), f, progress);
    }
    if (!res.ok) { error = res.message || 'Upload failed'; break; }
    done++;
  }
  filesBusy = false;
  await filesLoad(folder);
  if (error) {
    dropState('error', error);
    filesMessage(error, 'err');
  } else if (done) {
    const what = done === 1 ? fileList[0].name : `${done} files`;
    dropState('done', `Uploaded ${what}`);
    filesMessage(`Uploaded ${what} to ${filesPath || filesRoot}`, 'ok');
  }
}

$('filesUploadBtn').addEventListener('click', () => $('filesUploadInput').click());
$('filesUploadInput').addEventListener('change', (e) => {
  const files = [...e.target.files];
  e.target.value = '';
  if (files.length) filesUpload(files);
});
filesPanel.addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.path !== undefined) filesLoad(b.dataset.path);
  else if (b.dataset.open) filesLoad(b.dataset.open);
  else if (b.dataset.tune) openTune(b.dataset.tune, stoppedLineFor(b.dataset.tune));
  else if (b.dataset.preview) {
    const path = b.dataset.preview;
    try {
      const r = await fetch('/api/files/download?inline=1&path=' + encodeURIComponent(path), { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      openPreview(path.split('/').pop(), await r.text());
      filesMessage('Previewing ' + path.split('/').pop());
    } catch (err) {
      filesMessage('Cannot open: ' + err.message, 'err');
    }
  } else if (b.dataset.delete) {
    const path = b.dataset.delete;
    if (!confirm(`Delete "${path.split('/').pop()}" from the Pi?\n\nThis cannot be undone.`)) return;
    let res;
    try {
      const r = await fetch('/api/files/delete', {
        method: 'POST', headers: { 'X-CNC-Control': '1', 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      res = await r.json().catch(() => ({ ok: false, message: `HTTP ${r.status}` }));
    } catch {
      res = { ok: false, message: 'Server unreachable' };
    }
    filesMessage(res.message || (res.ok ? 'Deleted' : 'Delete failed'), res.ok ? 'ok' : 'err');
    await filesLoad(filesPath);
  }
});

// Drag & drop onto the right panel, whichever tab it shows. dragenter/dragleave also fire
// for every child element, so count them to know when the pointer really left the panel.
const sidePanel = $('sidePanel');
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
let dragDepth = 0;
sidePanel.addEventListener('dragenter', (e) => {
  if (!hasFiles(e) || filesBusy || !filesEnabled) return;
  e.preventDefault();
  if (dragDepth++ === 0) {
    if (activeTab !== 'files') setTab('files');
    dropState('over', `Drop to upload to ${filesPath || filesRoot}`);
  }
});
sidePanel.addEventListener('dragover', (e) => {
  if (!hasFiles(e) || filesBusy || !filesEnabled) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
});
sidePanel.addEventListener('dragleave', (e) => {
  if (!hasFiles(e) || filesBusy || !filesEnabled) return;
  if (--dragDepth <= 0) {
    dragDepth = 0;
    if (filesPanel.dataset.drop === 'over') dropState('');
  }
});
sidePanel.addEventListener('drop', (e) => {
  if (!hasFiles(e) || !filesEnabled) return;
  e.preventDefault();
  dragDepth = 0;
  const files = [...e.dataTransfer.files];
  if (files.length) filesUpload(files);
});
// A file dropped next to the panel would make the browser open it and leave the dashboard.
window.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener('drop', (e) => { if (hasFiles(e)) e.preventDefault(); });
// Pick up files added on the Pi itself every now and then.
setInterval(() => {
  if (filesEnabled && activeTab === 'files' && !filesBusy && document.visibilityState === 'visible') filesLoad(filesPath);
}, 15000);

// ---------------------------------------------------------------- G-code / Console / Files / Settings tabs

let activeTab = 'code';
let savedTab = '';           // tab from the last visit; Files only appears once the server says it's on
let consoleSeq = 0;          // last console line we have
let consoleFetching = false;

function setTab(tab) {
  activeTab = tab;
  savedTab = '';
  document.querySelectorAll('.ptab').forEach((b) => b.classList.toggle('active', b.dataset.ptab === tab));
  $('code').hidden = tab !== 'code';
  $('console').hidden = tab !== 'console';
  filesPanel.hidden = tab !== 'files';
  $('settings').hidden = tab !== 'settings';
  $('tunePanel').hidden = tab !== 'feeds';
  $('tuneLoadedBtn').hidden = tab !== 'code' || !state.file_rel || !!preview;
  $('resumeBtn').hidden = tab !== 'code' || followCode;
  $('consoleClearBtn').hidden = tab !== 'console';
  $('filesUploadBtn').hidden = tab !== 'files';
  if (tab === 'console') {
    $('consoleBadge').hidden = true;
    const log = $('consoleLog');
    log.scrollTop = log.scrollHeight;
  } else if (tab === 'settings') {
    settingsLoad();
    updLoad();
    logsLoad();
  } else if (tab === 'files') {
    filesLoad(filesPath);
  } else if (tab === 'feeds') {
    tuneLogLoad(true);
  } else {
    renderCode(true);
  }
  try { localStorage.setItem('cncTab', tab); } catch { /* ignore */ }
}
document.querySelectorAll('.ptab').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.ptab)));
$('consoleClearBtn').addEventListener('click', () => { $('consoleLog').innerHTML = ''; });

function addConsoleLines(lines) {
  const log = $('consoleLog');
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
  log.querySelector('.console-empty')?.remove();
  for (const l of lines) {
    const row = document.createElement('div');
    row.className = 'cline ' + l.kind;
    const time = new Date(l.t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    row.innerHTML = `<span class="ct">${time}</span><span class="cx">${esc(l.text)}</span>`;
    log.appendChild(row);
  }
  while (log.childElementCount > 500) log.firstChild.remove();
  if (atBottom || activeTab !== 'console') log.scrollTop = log.scrollHeight;
  if (activeTab !== 'console' && lines.length) {
    const badge = $('consoleBadge');
    badge.hidden = false;
    if (lines.some((l) => l.kind === 'err')) badge.classList.add('err');
  }
}

async function consoleUpdate(seq) {
  if (seq == null || consoleFetching) return;
  if (seq < consoleSeq) {  // server restarted: start over
    consoleSeq = 0;
    $('consoleLog').innerHTML = '';
  }
  if (seq === consoleSeq) return;
  consoleFetching = true;
  try {
    const d = await (await fetch('/api/console?after=' + consoleSeq, { cache: 'no-store' })).json();
    if (d.lines.length) {
      consoleSeq = d.lines[d.lines.length - 1].seq;
      addConsoleLines(d.lines);
    }
  } catch { /* next update retries */ }
  consoleFetching = false;
}

$('consoleLog').innerHTML = '<div class="console-empty">Messages appear here: jobs, machine events, updates and the bridge connecting.</div>';

// ---------------------------------------------------------------- settings: name, camera, files folder

let machineName = '';
let filesDir = '';

function setMessage(text, kind = '') {
  $('setMsg').textContent = text;
  $('setMsg').className = 'set-msg' + (kind ? ' ' + kind : '');
}

function renderName(name) {
  if (!name || name === machineName) return;
  machineName = name;
  $('machineName').textContent = name;
  document.title = name;
  if (document.activeElement !== $('nameInput')) $('nameInput').value = name;
}

function renderSettings(d) {
  renderName(d.name);
  $('nameInput').value = d.name;
  const sel = $('camSelect');
  const opts = [['auto', 'Automatic (first camera found)'], ...d.cameras.map((c) => [c.path, c.label]), ['none', 'Off']];
  // A chosen camera that is unplugged right now stays in the list, so the choice is visible.
  if (!opts.some(([v]) => v === d.camera)) opts.splice(opts.length - 1, 0, [d.camera, d.camera + ' (not connected)']);
  sel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
  sel.value = d.camera;
  const fixed = d.camera_cmd || !!d.camera_note;
  sel.disabled = $('camRescanBtn').disabled = fixed;
  $('camHint').textContent = d.camera_note ? `Camera choice unavailable: ${d.camera_note}.`
    : `${d.cameras.length} camera${d.cameras.length === 1 ? '' : 's'} found. The choice is kept when the Pi restarts.`;
  filesDir = d.files_dir || '';
  if (document.activeElement !== $('filesDirInput')) $('filesDirInput').value = filesDir;
  $('filesDirResetBtn').disabled = !d.files_dir_set;
  $('filesDirOpenBtn').disabled = !filesAbs || filesAbs === filesDir;
  $('filesDirHint').textContent = 'Shown in the Files tab; kept when the Pi restarts. '
    + (d.files_dir_set ? `Default: ${d.files_default || 'none'}.` : 'This is the default (--files-dir).');
  $('toolShape').checked = !!d.tool_shape;
  const cams = $('toolCamSelect');
  cams.innerHTML = (d.tool_cams || []).map((c) => `<option value="${esc(c.id)}">${esc(c.label)}</option>`).join('');
  cams.value = d.tool_cam;
  cams.disabled = !d.tool_shape;
}

async function settingsLoad() {
  try {
    renderSettings(await (await fetch('/api/settings', { cache: 'no-store' })).json());
  } catch {
    setMessage('Server unreachable', 'err');
  }
}

async function settingsSave(changes) {
  let res;
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: { 'X-CNC-Control': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify(changes),
    });
    res = await r.json().catch(() => ({ ok: false, message: `HTTP ${r.status}` }));
  } catch {
    res = { ok: false, message: 'Server unreachable' };
  }
  if (res.ok) renderSettings(res);
  setMessage(res.message || (res.ok ? 'Saved' : 'Failed'), res.ok ? 'ok' : 'err');
  if (!res.ok) log.warn(`saving settings ${JSON.stringify(changes)} failed: ${res.message}`);
  return res;
}

$('nameForm').addEventListener('submit', (e) => {
  e.preventDefault();
  settingsSave({ name: $('nameInput').value });
});
$('camSelect').addEventListener('change', async (e) => {
  const res = await settingsSave({ camera: e.target.value });
  if (res.ok && camShouldStream()) camStart();  // reopen the stream on the new camera
});
$('camRescanBtn').addEventListener('click', () => settingsLoad());
$('toolShape').addEventListener('change', (e) => settingsSave({ tool_shape: e.target.checked }));
$('toolCamSelect').addEventListener('change', (e) => settingsSave({ tool_cam: e.target.value }));

async function filesDirSave(folder) {
  const res = await settingsSave({ files_dir: folder });
  if (res.ok) {
    filesPath = '';
    filesAbs = '';
    if (res.files_dir) filesLoad('');
  }
}
$('filesDirForm').addEventListener('submit', (e) => {
  e.preventDefault();
  filesDirSave($('filesDirInput').value);
});
$('filesDirOpenBtn').addEventListener('click', () => filesDirSave(filesAbs));
$('filesDirResetBtn').addEventListener('click', () => filesDirSave(''));

// ---------------------------------------------------------------- software update

let updInfo = null;
let updRestarting = false;

const fmtAgo = (t) => {
  if (!t) return 'not yet';
  const s = Math.round(Date.now() / 1000 - t);
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
};
const fmtDay = (iso) => new Date(iso).toLocaleDateString([], { dateStyle: 'medium' });

function updateBadge(u) {
  $('verBtn').hidden = !u;
  const behind = u?.behind > 0;
  $('settingsBadge').hidden = !behind;
  if (!u) return;
  $('verText').textContent = behind ? `Update available (${u.behind})` : u.current || 'version';
  $('verDot').hidden = !behind;
  $('verBtn').classList.toggle('has-update', behind);
  $('verBtn').title = behind ? 'Open Settings to install it' : 'Installed version – open Settings to check for updates';
}

function updStatus(text, kind = '') {
  $('updStatus').textContent = text;
  $('updStatus').className = 'upd-status' + (kind ? ' ' + kind : '');
}

function renderUpdate(info) {
  updInfo = info;
  const c = info.current, l = info.latest;
  $('updCurrent').textContent = c ? `${c.sha} · ${c.subject}` : '–';
  $('updCurrent').title = c ? fmtDay(c.date) : '';
  $('updLatest').textContent = l ? `${l.sha} · checked ${fmtAgo(info.checked)}` : info.git ? `checked ${fmtAgo(info.checked)}` : '–';
  const proto = state.bridge_protocol;
  $('updBridge').textContent = !state.bridge_online ? 'offline'
    : proto == null ? '–'
    : proto >= 2 ? 'current – reloads itself after an update'
    : 'old version – restart TNG once after this update';

  const commits = info.commits || [];
  $('updCommits').innerHTML = commits.map((m) =>
    `<li><code>${esc(m.sha)}</code><span>${esc(m.subject)}</span><small>${esc(m.author)} · ${fmtDay(m.date)}</small></li>`).join('');

  const running = !!state.running;
  if (!info.git) updStatus(`Updates unavailable: ${info.message || 'the app folder must be a git clone – see README'}.`, 'err');
  else if (info.error) updStatus(info.error, 'err');
  else if (info.dirty && info.dirty.length) updStatus(`Files changed on the Pi, update blocked: ${info.dirty.join(', ')}`, 'err');
  else if (commits.length) updStatus(`${commits.length} update${commits.length === 1 ? '' : 's'} available:`);
  else updStatus('Up to date.', 'ok');

  $('updApplyBtn').disabled = running || info.busy || !commits.length || !!info.error || !!(info.dirty && info.dirty.length);
  $('updApplyBtn').title = running ? 'Not while a program runs' : '';
  $('updRollbackBtn').hidden = !info.can_rollback;
  $('updRollbackBtn').disabled = running || info.busy;
  $('updCheckBtn').disabled = !info.git || info.busy;
}

async function updLoad() {
  try {
    renderUpdate(await (await fetch('/api/update', { cache: 'no-store' })).json());
  } catch {
    updStatus('Server unreachable', 'err');
  }
}

async function updPost(path) {
  try {
    const r = await fetch(path, { method: 'POST', headers: { 'X-CNC-Control': '1', 'Content-Type': 'application/json' }, body: '{}' });
    return await r.json().catch(() => ({ ok: false, message: `HTTP ${r.status}` }));
  } catch {
    return { ok: false, message: 'Server unreachable' };
  }
}

/** After an update the server restarts; wait until it answers again, then load the new page. */
async function waitForRestart() {
  updRestarting = true;
  updStatus('Restarting the server with the new version…');
  await new Promise((r) => setTimeout(r, 2500));
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch('/api/update', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch { /* still restarting */ }
    await new Promise((r) => setTimeout(r, 1000));
  }
  updStatus('The server did not come back – check it over SSH: journalctl -u planetcnc-web -n 30', 'err');
}

async function updRun(path, question) {
  if (!confirm(question)) return;
  $('updApplyBtn').disabled = $('updRollbackBtn').disabled = $('updCheckBtn').disabled = true;
  updStatus(path.endsWith('rollback') ? 'Rolling back…' : 'Updating…');
  const res = await updPost(path);
  $('updOut').hidden = false;
  $('updOut').textContent = res.output || res.message || '';
  if (res.restarting) { waitForRestart(); return; }
  await updLoad();
  if (!res.ok) {
    updStatus(res.message || 'Failed', 'err');
    log.warn(`${path} failed: ${res.message}`);
  }
}

$('verBtn').addEventListener('click', () => setTab('settings'));
$('updCheckBtn').addEventListener('click', async () => {
  $('updCheckBtn').disabled = true;
  updStatus('Checking GitHub…');
  const res = await updPost('/api/update/check');
  if (res.git !== undefined) renderUpdate(res);
  else updStatus(res.message || 'Check failed', 'err');
});
$('updApplyBtn').addEventListener('click', () => updRun('/api/update/apply',
  `Install ${updInfo?.commits?.length || ''} update(s) from GitHub?\n\nThe dashboard restarts and reloads itself; TNG and the machine are not interrupted.`));
$('updRollbackBtn').addEventListener('click', () => updRun('/api/update/rollback',
  'Go back to the version that was installed before the last update?'));

// ---------------------------------------------------------------- logs (Settings tab)

async function logsLoad() {
  let d;
  try {
    d = await (await fetch('/api/logs', { cache: 'no-store' })).json();
  } catch {
    $('logInfo').textContent = 'Server unreachable';
    return;
  }
  $('logDebug').checked = d.debug;
  $('logDebug').disabled = !d.dir;
  $('logInfo').textContent = !d.dir ? 'No log files: the server was started with --log-dir none.'
    : `${fmtSize(d.total)} of at most ${fmtSize(d.max)} used. Files older than ${d.keep_days} days are deleted. ` +
      `Folder on the Pi: ${d.dir}`;
  $('logFiles').innerHTML = d.files.map((f) =>
    `<li><a href="/api/logs/download?name=${encodeURIComponent(f.name)}" download>${esc(f.name)}</a>` +
    `<span>${fmtSize(f.size)}</span><span>${fmtDate(f.mtime)}</span></li>`).join('');
}

async function logsShow() {
  const view = $('logView');
  view.hidden = false;
  view.textContent = 'Loading…';
  try {
    const r = await fetch('/api/logs/tail?lines=400&level=' + $('logFilter').value, { cache: 'no-store' });
    view.textContent = (await r.text()) || '(nothing yet)';
  } catch {
    view.textContent = 'Server unreachable';
  }
  view.scrollTop = view.scrollHeight;
}

$('logShowBtn').addEventListener('click', logsShow);
$('logFilter').addEventListener('change', () => { if (!$('logView').hidden) logsShow(); });
$('logDebug').addEventListener('change', async (e) => {
  const debug = e.target.checked;
  try {
    const r = await fetch('/api/logs/level', {
      method: 'POST', headers: { 'X-CNC-Control': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ debug }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    setMessage(debug ? 'Detailed logging on' : 'Detailed logging off', 'ok');
  } catch (err) {
    e.target.checked = !debug;
    setMessage('Could not change logging: ' + err.message, 'err');
  }
  logsLoad();
});

// ---------------------------------------------------------------- Raspberry Pi health

const GB = 1024 ** 3;
const fmtGB = (used, total) => `${(used / GB).toFixed(1)}/${(total / GB).toFixed(1)} GB`;

/** Fill one meter row. The word next to the value repeats the warning, so it never relies on color alone. */
function sysRow(id, frac, text, warnAt, errAt, warnWord, errWord) {
  const row = $(id);
  row.hidden = frac == null;
  if (frac == null) return;
  const level = frac >= errAt ? 'err' : frac >= warnAt ? 'warn' : '';
  row.dataset.level = level;
  row.querySelector('.meter i').style.width = Math.max(0, Math.min(1, frac)) * 100 + '%';
  const v = row.querySelector('.v');
  v.textContent = v.title = text + (level === 'err' ? ' ' + errWord : level === 'warn' ? ' ' + warnWord : '');
}

function renderSystem(sys) {
  $('sys').hidden = !sys;
  if (!sys) return;
  sysRow('sysCpuRow', sys.cpu == null ? null : sys.cpu / 100,
    sys.cpu == null ? '' : Math.round(sys.cpu) + ' %', 0.8, 0.95, 'busy', 'busy');
  sysRow('sysMemRow', sys.mem_total ? sys.mem_used / sys.mem_total : null,
    sys.mem_total ? fmtGB(sys.mem_used, sys.mem_total) : '', 0.85, 0.95, 'high', 'full');
  // Temperature bar spans 20–90 °C; the Pi slows itself down from about 80 °C.
  sysRow('sysTempRow', sys.temp == null ? null : (sys.temp - 20) / 70,
    sys.temp == null ? '' : Math.round(sys.temp) + ' °C', (70 - 20) / 70, (80 - 20) / 70, 'warm', 'hot');
  sysRow('sysDiskRow', sys.disk_total ? sys.disk_used / sys.disk_total : null,
    sys.disk_total ? fmtGB(sys.disk_used, sys.disk_total) : '', 0.85, 0.95, 'high', 'full');

  // vcgencmd get_throttled bits: 0 under-voltage, 1 frequency capped, 2 throttled, 3 soft temp limit;
  // bits 16-19 = the same "has happened since boot".
  const t = sys.throttled;
  const power = $('sysPower');
  power.hidden = t == null;
  if (t != null) {
    const [level, text] = t & 0x1 ? ['err', 'Under-voltage!']
      : t & 0x6 ? ['warn', 'Throttled']
      : t & 0x8 ? ['warn', 'Temperature limit']
      : t & 0x10000 ? ['warn', 'Under-voltage since boot']
      : t & 0xE0000 ? ['warn', 'Throttled since boot']
      : ['', 'Power OK'];
    power.dataset.level = level;
    power.textContent = text;
    power.title = 'vcgencmd get_throttled = 0x' + t.toString(16);
  }
  if (sys.uptime != null) {
    const d = Math.floor(sys.uptime / 86400), h = Math.floor((sys.uptime % 86400) / 3600), m = Math.floor((sys.uptime % 3600) / 60);
    $('sysUptime').textContent = 'Up ' + (d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`);
  }
}

// ---------------------------------------------------------------- connection to the server

const STALL_MS = 6000;     // the server sends at least one update a second
let es = null;
let lastMessage = 0;

/** Live updates stopped: show it, log it once, and free the camera's connection for the reconnect. */
function connectionLost(why) {
  if (!liveDownSince) {
    liveDownSince = Date.now();
    log.warn(`live connection lost: ${why}; last update ${lastMessage ? ((Date.now() - lastMessage) / 1000).toFixed(1) + ' s ago' : 'never'}`);
    if (camImg.hasAttribute('src')) camStop();
  }
  state = { ...state, bridge_online: false };
  renderInfo();
}

function connect() {
  es?.close();
  lastMessage = Date.now();  // give the new connection time before the watchdog judges it
  es = new EventSource('/api/events');
  const me = es;
  es.onmessage = (e) => {
    lastMessage = Date.now();
    if (liveDownSince) {
      log.info(`live connection back after ${((Date.now() - liveDownSince) / 1000).toFixed(0)} s`);
      liveDownSince = null;
      camSync();  // reopen the camera stream it closed
    }
    try { onState(JSON.parse(e.data)); } catch (err) { log.error('state update failed:', err); }
  };
  es.onopen = () => {
    log.debug('live connection open');
    // After a server restart the old camera stream is dead: reopen it.
    if (camShouldStream()) camStart();
  };
  es.onerror = () => {
    if (me !== es) return;
    connectionLost(`connection error (${['connecting', 'open', 'closed'][me.readyState]})`);
    // EventSource reconnects by itself unless the server answered with an error.
    if (me.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
  };
}

// A connection can also go silent without an error (Wi-Fi dropped, a stuck proxy, the browser out
// of connections): then the page would show old values forever. Reconnect when updates stop.
setInterval(() => {
  if (!es || document.visibilityState !== 'visible' || Date.now() - lastMessage < STALL_MS) return;
  connectionLost(`no update for ${((Date.now() - lastMessage) / 1000).toFixed(0)} s`);
  log.info('reconnecting');
  connect();
}, 1000);

try {
  const tab = localStorage.getItem('cncTab');
  if (tab === 'console' || tab === 'settings') setTab(tab);
  else if (tab === 'files' || tab === 'feeds') savedTab = tab;
} catch { /* ignore */ }
renderDro();
renderCode(true);
connect();
