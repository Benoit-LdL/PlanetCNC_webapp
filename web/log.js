// Sends what happens in this page (errors, connection trouble, slow loads) to the server's log,
// so a problem seen on a tablet or phone can be looked at later on the Pi (Settings → Logs).

const FLUSH_MS = 3000;     // collect lines this long before sending (errors go at once)
const RETRY_MS = 10000;    // after a failed send
const MAX_QUEUE = 200;     // lines kept while the server can't be reached

const queue = [];
let dropped = 0;
let debugOn = false;
let timer = null;
let sending = false;

function text(a) {
  if (a instanceof Error) return a.stack || `${a.name}: ${a.message}`;
  if (typeof a === 'string') return a;
  try { return JSON.stringify(a); } catch { return String(a); }
}

function add(level, args) {
  const msg = args.map(text).join(' ');
  (console[level] || console.log)(msg);
  if (level === 'debug' && !debugOn) return;
  if (queue.length >= MAX_QUEUE) { queue.shift(); dropped++; }
  queue.push({ t: Date.now() / 1000, level, msg });
  schedule(level === 'error' ? 0 : FLUSH_MS);
}

function schedule(ms) {
  if (timer !== null && ms > 0) return;
  clearTimeout(timer);
  timer = setTimeout(flush, ms);
}

async function flush(leaving = false) {
  timer = null;
  if (sending) { schedule(FLUSH_MS); return; }
  if (!queue.length) return;
  sending = true;
  const entries = queue.splice(0);
  const lost = dropped;
  dropped = 0;
  try {
    const r = await fetch('/api/log', {
      method: 'POST', keepalive: leaving,
      headers: { 'Content-Type': 'application/json', 'X-CNC-Control': '1' },
      body: JSON.stringify({ entries, dropped: lost }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  } catch {
    // Server unreachable: keep the lines (the newest ones if there are too many) and try later.
    queue.unshift(...entries);
    dropped += lost + Math.max(0, queue.length - MAX_QUEUE);
    queue.splice(0, Math.max(0, queue.length - MAX_QUEUE));
    if (!leaving) schedule(RETRY_MS);
  } finally {
    sending = false;
  }
}

export const log = {
  debug: (...a) => add('debug', a),
  info: (...a) => add('info', a),
  warn: (...a) => add('warn', a),
  error: (...a) => add('error', a),
  /** Detailed logging is switched on the server (Settings → Logs); debug lines are only sent then. */
  setDebug(on) { debugOn = !!on; },
};

window.addEventListener('error', (e) => {
  log.error(`page error: ${e.message} (${e.filename || '?'}:${e.lineno || '?'})`, e.error || '');
});
window.addEventListener('unhandledrejection', (e) => log.error('unhandled promise rejection:', e.reason ?? ''));
document.addEventListener('visibilitychange', () => {
  log.debug(`page ${document.visibilityState}`);
  if (document.visibilityState === 'hidden') flush(true);
});
window.addEventListener('pagehide', () => flush(true));

log.info(`page opened: ${location.pathname}, window ${innerWidth}x${innerHeight}, ` +
  `${navigator.onLine ? 'online' : 'offline'}, language ${navigator.language}`);
