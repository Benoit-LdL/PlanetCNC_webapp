// The E-stop button, and the System window: the Pi's details, updates (this app, the bridge
// in TNG, Raspberry Pi OS packages) and reboot / shut down.
//
// The E-stop is the only thing the dashboard can make the machine do. It only switches
// E-stop ON; releasing it and starting again always happen at the machine.
import { log } from './log.js';

const $ = (id) => document.getElementById(id);
const esc = (t) => String(t ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const writeHeaders = { 'X-CNC-Control': '1', 'Content-Type': 'application/json' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ESTOP_PROTOCOL = 3;   // bridge version that accepts the E-stop (server.py ESTOP_PROTOCOL)

let state = {};
let sys = null;             // last /api/system answer
let pollTimer = null;
let restarting = false;

const fmtAgo = (t) => {
  if (!t) return 'not yet';
  const s = Math.round(Date.now() / 1000 - t);
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago`
    : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
};
const fmtDay = (iso) => new Date(iso).toLocaleDateString([], { dateStyle: 'medium' });
const fmtUp = (sec) => {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
};

async function post(path, body = {}) {
  try {
    const r = await fetch(path, { method: 'POST', headers: writeHeaders, body: JSON.stringify(body) });
    return await r.json().catch(() => ({ ok: false, message: `HTTP ${r.status}` }));
  } catch {
    return { ok: false, message: 'Server unreachable' };
  }
}

// ---------------------------------------------------------------- E-stop

let estopBusy = false;

function renderEstop() {
  const online = !!state.bridge_online;
  const proto = state.bridge_protocol || 0;
  let label = 'E-STOP', title = 'Emergency stop: TNG stops the machine at once. Release it at the machine.';
  let disabled = estopBusy;
  if (estopBusy) label = 'STOPPING…';
  else if (online && state.estop) [label, title, disabled] = ['ENGAGED', 'E-stop is active – release it at the machine', true];
  else if (!online) [title, disabled] = ['TNG is not connected – use the E-stop at the machine', true];
  else if (proto < ESTOP_PROTOCOL) [title, disabled] = ['The bridge in TNG is too old for this – see the System window', true];
  for (const b of document.querySelectorAll('[data-estop]')) {
    b.querySelector('.estop-label').textContent = label;
    b.title = title;
    b.disabled = disabled;
    b.classList.toggle('on', online && !!state.estop);
  }
}

async function estop() {
  if (estopBusy) return;
  estopBusy = true;
  renderEstop();
  const res = await post('/api/estop');
  estopBusy = false;
  renderEstop();
  if (!res.ok) {
    log.error(`E-stop failed: ${res.message}`);
    alert(`E-STOP FAILED: ${res.message || 'no answer'}\n\nUse the E-stop at the machine!`);
  }
}

// ---------------------------------------------------------------- top bar button

function renderBadge() {
  const u = state.update, osN = state.os_updates || 0;
  $('verBtn').hidden = false;
  const appN = u?.behind || 0;
  $('verText').textContent = appN ? `Update available (${appN})` : osN ? `OS updates (${osN})` : u?.current || 'System';
  $('verDot').hidden = !(appN || osN);
  $('verBtn').classList.toggle('has-update', !!(appN || osN));
  $('verBtn').title = 'System: versions, updates, reboot' + (appN || osN ? ' – updates available' : '');
}

// ---------------------------------------------------------------- the System window

function row(label, value, title = '') {
  return `<div class="upd-row"><span>${esc(label)}</span><b title="${esc(title || value)}">${esc(value)}</b></div>`;
}

function setStatus(id, text, kind = '') {
  $(id).textContent = text;
  $(id).className = 'upd-status' + (kind ? ' ' + kind : '');
}

function renderHost(h) {
  $('hostRows').innerHTML = [
    row('Model', h.model || '–'),
    row('System', h.os || '–'),
    row('Kernel', h.kernel || '–'),
    row('Python', h.python || '–'),
    row('Address', `${h.hostname} · ${h.address}`),
    row('Up', h.uptime != null ? fmtUp(h.uptime) : '–'),
    row('Dashboard server', 'running ' + fmtUp(Date.now() / 1000 - h.server_started)),
  ].join('');
}

function renderApp(info, running) {
  const c = info.current, l = info.latest;
  $('updCurrent').textContent = c ? `${c.sha} · ${c.subject}` : '–';
  $('updCurrent').title = c ? fmtDay(c.date) : '';
  $('updLatest').textContent = l ? `${l.sha} · checked ${fmtAgo(info.checked)}` : info.git ? `checked ${fmtAgo(info.checked)}` : '–';
  const commits = info.commits || [];
  $('updCommits').innerHTML = commits.map((m) =>
    `<li><code>${esc(m.sha)}</code><span>${esc(m.subject)}</span><small>${esc(m.author)} · ${fmtDay(m.date)}</small></li>`).join('');
  if (restarting) { /* keep the restart message */ }
  else if (!info.git) setStatus('updStatus', `Updates unavailable: ${info.message || 'the app folder must be a git clone – see README'}.`, 'err');
  else if (info.error) setStatus('updStatus', info.error, 'err');
  else if (info.dirty && info.dirty.length) setStatus('updStatus', `Files changed on the Pi, update blocked: ${info.dirty.join(', ')}`, 'err');
  else if (commits.length) setStatus('updStatus', `${commits.length} update${commits.length === 1 ? '' : 's'} available:`);
  else setStatus('updStatus', 'Up to date.', 'ok');
  $('updApplyBtn').disabled = running || info.busy || !commits.length || !!info.error || !!(info.dirty && info.dirty.length);
  $('updApplyBtn').title = running ? 'Not while a program runs' : '';
  $('updRollbackBtn').hidden = !info.can_rollback;
  $('updRollbackBtn').disabled = running || info.busy;
}

function renderBridge(b) {
  const p = b.protocol;
  const running = !b.online ? 'not connected'
    : p == null ? '–'
    : p >= b.expected ? `version ${p} – current`
    : p >= 2 ? `version ${p} – old: it reloads itself after the update`
    : `version ${p} – old: restart TNG once after the update`;
  const copy = { current: 'same as this version', different: 'differs – install the update, or run update.sh',
    missing: 'not found' }[b.installed] || '–';
  $('bridgeRows').innerHTML = row('Running', running) + row('Copy in TNG', copy, b.path);
}

function renderOs(o, running) {
  $('osBlock').hidden = !o;
  $('osNone').hidden = !!o;
  if (!o) return;
  const pk = o.packages || [];
  const job = o.job;
  if (o.busy) setStatus('osStatus', `Upgrading… (started ${fmtAgo(job.started)})`);
  else if (o.error) setStatus('osStatus', o.error, 'err');
  else if (pk.length) setStatus('osStatus', `${pk.length} package${pk.length === 1 ? '' : 's'} can be upgraded.`);
  else setStatus('osStatus', 'Up to date.', 'ok');
  $('osLists').textContent = `Package lists from ${fmtAgo(o.lists_date)}.`
    + (o.kept_back?.length ? ` Held back (upgrade over SSH with apt full-upgrade): ${o.kept_back.join(', ')}.` : '');
  $('osPkgs').hidden = !pk.length;
  $('osPkgCount').textContent = `${pk.length} package${pk.length === 1 ? '' : 's'}`;
  $('osPkgList').innerHTML = pk.map((p) =>
    `<li><b>${esc(p.name)}</b><span>${esc(p.from)} → ${esc(p.to)}</span></li>`).join('');
  $('osReboot').hidden = !o.reboot_required;
  $('osSudo').hidden = !!(o.can_upgrade && o.can_update);
  $('osUpgradeBtn').disabled = running || o.busy || !pk.length || !o.can_upgrade;
  $('osUpgradeBtn').title = running ? 'Not while a program runs' : !o.can_upgrade ? 'Needs tools/setup-sudo.sh (see README)' : '';
  const out = $('osOut');
  out.hidden = !job;
  if (job) {
    const atEnd = out.scrollHeight - out.scrollTop - out.clientHeight < 30;
    const end = job.running ? '' : job.exit === 0 ? '\n— finished —' : `\n— FAILED (exit code ${job.exit}) —`;
    out.textContent = job.output.join('\n') + end;
    if (atEnd) out.scrollTop = out.scrollHeight;
  }
  $('powerSudo').hidden = !!o.can_power;
  $('rebootBtn').disabled = $('poweroffBtn').disabled = running || !o.can_power || o.busy;
  $('rebootBtn').title = $('poweroffBtn').title = running ? 'Not while a program runs' : o.busy ? 'Wait for the upgrade' : '';
}

function render() {
  if (!sys) return;
  renderHost(sys.host);
  renderApp(sys.app, sys.running);
  renderBridge(sys.bridge);
  renderOs(sys.os, sys.running);
  $('powerBlock').hidden = !sys.os;
  $('updCheckBtn').disabled = !!(sys.app.busy || sys.os?.busy) || checking;
}

async function load() {
  try {
    sys = await (await fetch('/api/system', { cache: 'no-store' })).json();
    render();
  } catch {
    setStatus('updStatus', 'Server unreachable', 'err');
  }
  clearTimeout(pollTimer);
  if ($('sysDlg').open) pollTimer = setTimeout(load, sys?.os?.busy ? 1500 : 5000);
}

export function openSystem() {
  if (!$('sysDlg').open) $('sysDlg').showModal();
  load();
}

let checking = false;
async function checkAll() {
  checking = true;
  $('updCheckBtn').disabled = true;
  setStatus('updStatus', 'Checking GitHub…');
  if (sys?.os) setStatus('osStatus', 'Checking the package lists… (can take a minute)');
  const [app, os] = await Promise.all([post('/api/update/check'), sys?.os ? post('/api/system/os/check') : null]);
  checking = false;
  if (app.git === undefined) setStatus('updStatus', app.message || 'Check failed', 'err');
  if (os && !os.ok) setStatus('osStatus', os.message || 'Check failed', 'err');
  await load();
  if (app.git === undefined) setStatus('updStatus', app.message || 'Check failed', 'err');
}

/** After an update the server restarts; wait until it answers again, then load the new page. */
async function waitForServer(message, first, tries) {
  restarting = true;
  setStatus('updStatus', message);
  await sleep(first);
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch('/api/system', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch { /* still restarting */ }
    await sleep(1000);
  }
  restarting = false;
  setStatus('updStatus', 'The server did not come back – check it over SSH: journalctl -u planetcnc-web -n 30', 'err');
}

async function appRun(path, question) {
  if (!confirm(question)) return;
  $('updApplyBtn').disabled = $('updRollbackBtn').disabled = $('updCheckBtn').disabled = true;
  setStatus('updStatus', path.endsWith('rollback') ? 'Rolling back…' : 'Updating…');
  const res = await post(path);
  $('updOut').hidden = false;
  $('updOut').textContent = res.output || res.message || '';
  if (res.restarting) { waitForServer('Restarting the server with the new version…', 2500, 60); return; }
  await load();
  if (!res.ok) {
    setStatus('updStatus', res.message || 'Failed', 'err');
    log.warn(`${path} failed: ${res.message}`);
  }
}

async function osUpgrade() {
  const n = sys?.os?.packages?.length || 0;
  if (!confirm(`Upgrade ${n} Raspberry Pi OS package${n === 1 ? '' : 's'}?\n\n`
    + 'This can take several minutes. TNG keeps running, but nothing should run on the machine meanwhile. '
    + 'Afterwards, restart TNG (or reboot the Pi) so it uses the new libraries.')) return;
  $('osUpgradeBtn').disabled = true;
  const res = await post('/api/system/os/upgrade');
  if (!res.ok) setStatus('osStatus', res.message || 'Failed', 'err');
  else load();
}

async function power(action) {
  const q = action === 'reboot'
    ? 'Reboot the Raspberry Pi?\n\nTNG closes, so the machine can do nothing until TNG is running again. '
      + 'The dashboard is back once the Pi has started (a minute or two).'
    : 'Shut down the Raspberry Pi?\n\nTNG closes. To start it again you must switch the Pi off and on at the machine.';
  if (!confirm(q)) return;
  const res = await post('/api/system/power', { action });
  if (!res.ok) { alert(res.message || 'Failed'); return; }
  $('rebootBtn').disabled = $('poweroffBtn').disabled = true;
  if (action === 'reboot') waitForServer('The Pi is rebooting… this page reloads when it is back.', 20000, 240);
  else setStatus('updStatus', 'The Pi is shutting down. Switch it off and on at the machine to start it again.');
}

// ---------------------------------------------------------------- wiring

export function systemOnState(s) {
  state = s;
  renderEstop();
  renderBadge();
}

export function initSystem() {
  for (const b of document.querySelectorAll('[data-estop]')) b.addEventListener('click', estop);
  $('verBtn').addEventListener('click', openSystem);
  $('sysOpenBtn').addEventListener('click', openSystem);
  $('sysDlgClose').addEventListener('click', () => $('sysDlg').close());
  $('sysDlg').addEventListener('close', () => clearTimeout(pollTimer));
  $('updCheckBtn').addEventListener('click', checkAll);
  $('updApplyBtn').addEventListener('click', () => appRun('/api/update/apply',
    `Install ${sys?.app?.commits?.length || ''} update(s) from GitHub?\n\nThe dashboard restarts and reloads itself; TNG and the machine are not interrupted.`));
  $('updRollbackBtn').addEventListener('click', () => appRun('/api/update/rollback',
    'Go back to the version that was installed before the last update?'));
  $('osUpgradeBtn').addEventListener('click', osUpgrade);
  $('rebootBtn').addEventListener('click', () => power('reboot'));
  $('poweroffBtn').addEventListener('click', () => power('poweroff'));
  renderEstop();
}
