import { Viewer } from './viewer.js';
import { buildToolpath } from './gcode.js';

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
let pathWorker = null;
try {
  pathWorker = new Worker(new URL('./toolpath-worker.js', import.meta.url), { type: 'module' });
  pathWorker.onmessage = (e) => { if (e.data.seq === pathSeq) setToolpath(e.data.path); };
  pathWorker.onerror = (e) => {
    // The worker couldn't start (old browser): do it here instead.
    console.warn('toolpath worker failed, building the toolpath on the main thread', e);
    pathWorker.terminate();
    pathWorker = null;
    showToolpath(pathText);
  };
} catch (e) {
  console.warn('no module worker, building the toolpath on the main thread', e);
}

function setToolpath(path) {
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
  if (!text) { setToolpath(null); return; }
  if (pathWorker) { pathWorker.postMessage({ seq, text }); return; }
  setTimeout(() => { if (seq === pathSeq) setToolpath(buildToolpath(text)); });
}

function showProgram(text) {
  codeLines = text ? text.replace(/\r?\n$/, '').split(/\r?\n/) : [];
  $('codeSpacer').style.height = codeLines.length * ROW_H + 'px';
  showToolpath(text);
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

  renderName(s.machine_name);
  renderDro();
  renderInfo();
  renderControls();
  filesUpdate(s);
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
  renderControls();
}

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

// ---------------------------------------------------------------- Pi files panel (--files-dir)

const filesPanel = $('filesPanel');
const GCODE_RE = /\.(nc|ngc|gcode|gc|tap|cnc|txt)$/i;
const ICONS = {
  view: '<svg viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M12 4v12m0 0-5-5m5 5 5-5M5 20h14"/></svg>',
  del: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
};
let filesPath = '';
let filesRoot = 'Files';
let filesEnabled = false;
let filesShown = true;     // user hasn't hidden the panel
let filesBusy = false;     // an upload is running
let dropTimer = null;

try { filesShown = localStorage.getItem('cncFilesHidden') !== '1'; } catch { /* storage unavailable */ }

const fmtSize = (n) => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(0) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const fmtDate = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
const joinPath = (dir, name) => (dir ? dir + '/' : '') + name;

function filesMessage(text, kind = '') {
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
  filesPanel.hidden = !filesEnabled;
  filesPanel.classList.toggle('collapsed', !filesShown);
  $('filesToggleBtn').textContent = filesShown ? 'Hide' : 'Show';
  $('filesToggleBtn').classList.toggle('active', filesShown);
}

function filesUpdate(s) {
  const enabled = !!s.files_enabled;
  if (enabled !== filesEnabled) {
    filesEnabled = enabled;
    filesSync();
    if (enabled && filesShown) filesLoad(filesPath);
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

$('filesToggleBtn').addEventListener('click', () => {
  filesShown = !filesShown;
  try { localStorage.setItem('cncFilesHidden', filesShown ? '0' : '1'); } catch { /* ignore */ }
  filesSync();
  if (filesShown) filesLoad(filesPath);
});
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

// Drag & drop onto the panel. dragenter/dragleave also fire for every child element,
// so count them to know when the pointer really left the panel.
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
let dragDepth = 0;
filesPanel.addEventListener('dragenter', (e) => {
  if (!hasFiles(e) || filesBusy) return;
  e.preventDefault();
  if (dragDepth++ === 0) {
    if (!filesShown) { filesShown = true; filesSync(); }
    dropState('over', `Drop to upload to ${filesPath || filesRoot}`);
  }
});
filesPanel.addEventListener('dragover', (e) => {
  if (!hasFiles(e) || filesBusy) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
});
filesPanel.addEventListener('dragleave', (e) => {
  if (!hasFiles(e) || filesBusy) return;
  if (--dragDepth <= 0) {
    dragDepth = 0;
    if (filesPanel.dataset.drop === 'over') dropState('');
  }
});
filesPanel.addEventListener('drop', (e) => {
  if (!hasFiles(e)) return;
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
  if (filesEnabled && filesShown && !filesBusy && document.visibilityState === 'visible') filesLoad(filesPath);
}, 15000);

// ---------------------------------------------------------------- G-code / Console / Settings tabs

let activeTab = 'code';
let consoleSeq = 0;          // last console line we have
let consoleFetching = false;
const cmdHistory = [];
let historyPos = 0;

function setTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.ptab').forEach((b) => b.classList.toggle('active', b.dataset.ptab === tab));
  $('code').hidden = tab !== 'code';
  $('console').hidden = tab !== 'console';
  $('settings').hidden = tab !== 'settings';
  $('resumeBtn').hidden = tab !== 'code' || followCode;
  $('consoleClearBtn').hidden = tab !== 'console';
  if (tab === 'console') {
    $('consoleBadge').hidden = true;
    const log = $('consoleLog');
    log.scrollTop = log.scrollHeight;
  } else if (tab === 'settings') {
    settingsLoad();
    updLoad();
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

$('consoleForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = $('consoleInput');
  const line = input.value.trim();
  if (!line || busy) return;
  if (cmdHistory[cmdHistory.length - 1] !== line) cmdHistory.push(line);
  historyPos = cmdHistory.length;
  input.value = '';
  control({ action: 'mdi', line });  // the server echoes the line and TNG's answer into the console
});
$('consoleInput').addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
  e.preventDefault();
  historyPos = Math.max(0, Math.min(cmdHistory.length, historyPos + (e.key === 'ArrowUp' ? -1 : 1)));
  e.target.value = cmdHistory[historyPos] ?? '';
});

function renderConsoleInput() {
  const input = $('consoleInput');
  input.disabled = !state.bridge_online;
  $('consoleForm').querySelector('button').disabled = input.disabled || busy;
  input.placeholder = !state.bridge_online ? 'TNG bridge offline' : 'G-code to send to TNG (MDI), e.g. G0 Z10';
}

$('consoleLog').innerHTML = '<div class="console-empty">Messages appear here: commands from this dashboard, TNG\'s answers and machine events.</div>';

// ---------------------------------------------------------------- settings: name, camera

let machineName = '';

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
  if (!res.ok) updStatus(res.message || 'Failed', 'err');
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
  row.querySelector('.v').textContent = text + (level === 'err' ? ' ' + errWord : level === 'warn' ? ' ' + warnWord : '');
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

// ---------------------------------------------------------------- machine control (no jogging: moving the
// machine by hand needs someone standing next to it)

let busy = false;     // a command is waiting for TNG's answer
let shownWhy = '';

function ctlMessage(text, kind = '') {
  const m = $('ctlMsg');
  m.textContent = text;
  m.className = 'ctl-msg' + (kind ? ' ' + kind : '');
}

function renderControls() {
  const s = state;
  const avail = s.controls || {};
  const online = !!s.bridge_online;
  const idle = online && !s.running && !s.estop;
  const why = !online ? 'TNG bridge offline.'
    : s.estop ? 'E-stop is active.'
    : s.running ? 'A program is running – only Stop is available.' : '';
  $('homeBtn').disabled = busy || !idle || !avail.home;
  $('stopBtn').disabled = !online || !avail.stop;
  $('loadTngBtn').disabled = busy || !idle || !avail.load;
  $('loadTngBtn').title = !avail.load ? 'TNG bridge offline or command not available'
    : 'Make this the loaded program in TNG (does not start it)';
  // Show why controls are disabled; clear that note (but not command results) once it no longer applies.
  if (why && !busy) ctlMessage(why);
  else if (!why && shownWhy && $('ctlMsg').textContent === shownWhy) ctlMessage('');
  shownWhy = why;
  renderConsoleInput();
}

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
  ctlMessage(res.ok ? (res.message || 'Done') : (res.message || 'Failed'), res.ok ? 'ok' : 'err');
  renderControls();
  return res;
}

$('homeBtn').addEventListener('click', () => {
  if (confirm('Home the machine now?\n\nAll axes will move to their home switches. Make sure nothing is in the way.')) {
    control({ action: 'home' });
  }
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

try {
  const tab = localStorage.getItem('cncTab');
  if (tab === 'console' || tab === 'settings') setTab(tab);
} catch { /* ignore */ }
renderDro();
renderCode(true);
connect();
