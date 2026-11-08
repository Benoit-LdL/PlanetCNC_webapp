"""PlanetCNC web monitor - bridge.

Runs INSIDE PlanetCNC TNG's Python interpreter (it needs the `planetcnc` module)
and connects TNG to server.py.

The bridge is deliberately generic, so new dashboard features don't need a new
bridge:
  - The server sends a "spec": which TNG parameters / functions to read. The
    bridge reads them and sends the raw values with every update (5x/s).
  - The server can ask the bridge to call a planetcnc function, but only
    reading functions are allowed.
  - The one exception is the dashboard's E-stop button: the bridge switches
    TNG's E-stop ON. It never releases it, so restarting the machine always
    happens at the machine. Otherwise the bridge never moves, starts, homes or
    changes the machine, and never loads programs into TNG.

When this file changes on disk (e.g. after update.sh), the bridge reloads
itself within a few seconds - no TNG restart needed.

What the bridge does and every problem it meets is sent to the server with the
next update and ends up in the server's log (logs/server.log, marked [TNG]).
Lines from while the server was unreachable are kept and sent once it is back.

TNG needs Python enabled first: Settings -> Python Library must point to the
Python shared library (on the Mekanika Pi it is /usr/local/lib/libpython3.11.so.1.0;
find yours with `ls /usr/local/lib/libpython3* /usr/lib/*/libpython3*`); press "Check", then restart TNG.
It's started by TNG through Expr_WebMonitor.txt + bridge_autostart.py, or by hand
with File -> Run Python Script. It prints "connected" / the reason it can't
connect in TNG's output window.

Only the Python standard library is used.
"""

import json
import os
import runpy
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import deque

import planetcnc

# ---------------- configuration ----------------
SERVER_URL = "http://127.0.0.1:8080"  # where server.py runs; use the Pi's IP if TNG runs on another PC
TOKEN = ""                            # must match server.py --token (empty = none)
INTERVAL = 0.2                        # seconds between state updates (5 Hz)
MAX_PROGRAM_BYTES = 50 * 1024 * 1024  # don't upload programs bigger than this
# ------------------------------------------------

PROTOCOL = 3          # 3: the E-stop request
SLOW_LOOP = 1.0       # an update round taking longer than this (s) is logged
LOG_KEEP = 300        # log lines kept while the server can't be reached

LOG_LINES = deque(maxlen=LOG_KEEP)   # sent to the server with the next update
log_dropped = [0]


def log(level, msg, show=False):
    """Log for the server (level debug | info | warn | error). Warnings, errors and `show`
    lines also appear in TNG's output window."""
    if len(LOG_LINES) == LOG_LINES.maxlen:
        log_dropped[0] += 1
    LOG_LINES.append({"t": time.time(), "level": level, "msg": msg})
    if show or level in ("warn", "error"):
        planetcnc.print("PlanetCNC web bridge: " + msg)


def take_log():
    """The collected log lines for the next update (put back with put_back_log if sending fails)."""
    lines = list(LOG_LINES)
    if log_dropped[0]:
        lines.insert(0, {"t": time.time(), "level": "warn",
                         "msg": "%d log lines lost while the server was unreachable" % log_dropped[0]})
    LOG_LINES.clear()
    log_dropped[0] = 0
    return lines


def put_back_log(lines):
    for line in reversed(lines):
        if len(LOG_LINES) < LOG_LINES.maxlen:
            LOG_LINES.appendleft(line)

# planetcnc functions the server may call: reading only.
READ_PREFIXES = ("hw_", "is_", "get_", "gcode_is_", "cmd_get_", "cmd_is_")
READ_FUNCS = {"param_get", "cmd_list", "cmd_count", "msg_count", "msg_is_active", "starttime"}


def readable(fn_name):
    return fn_name in READ_FUNCS or fn_name.startswith(READ_PREFIXES)


def jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    return str(v)


def tng(fn_name, *args):
    fn = getattr(planetcnc, fn_name, None)
    if fn is None:
        raise AttributeError("TNG has no function planetcnc.%s" % fn_name)
    return fn(*args)


def post(path, payload, timeout=2.0):
    req = urllib.request.Request(
        SERVER_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Bridge-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


# ---------------- reading: what the spec asks for ----------------

def read_item(kind, name):
    """kind p = parameter, f = function without arguments, c = command id (None if TNG lacks it)."""
    try:
        if kind == "p":
            return jsonable(planetcnc.param_get(name))
        if kind == "f":
            return jsonable(tng(name)) if readable(name) else None
        if kind == "c":
            i = planetcnc.cmd_get_id(name)
            return i if isinstance(i, int) and i >= 0 else None
    except Exception:
        pass
    return None


def read_values(spec):
    return {key: read_item(kind, name) for key, kind, name in spec.get("items", [])}


# ---------------- requests from the server ----------------

def estop_active():
    try:
        return bool(float(planetcnc.param_get("_hw_estop") or 0))
    except (TypeError, ValueError):
        return False


def engage_estop():
    """Switch TNG's E-stop on (the dashboard's E-stop button).

    Only ever switches it ON: when TNG already reports E-stop, nothing is called, so a
    second press can never release it. Releasing happens at the machine.
    """
    if estop_active():
        return True, "E-stop was already active", None
    fn = getattr(planetcnc, "estop", None)
    if fn is None:
        return False, "this TNG has no planetcnc.estop()", None
    fn()
    log("warn", "E-STOP engaged from the web dashboard", show=True)
    return True, "E-stop engaged", None


def run_command(cmd):
    """Answer one request from the server. Returns (ok, message, value).

    Only two kinds of request are accepted, whatever the server asks:
      - "call": a reading planetcnc function;
      - "estop": switch E-stop on (never off).
    The bridge never moves, starts, homes or otherwise changes the machine, and never
    loads programs into TNG.
    """
    op = cmd.get("op")
    if op == "estop":
        return engage_estop()
    if op != "call":
        return False, "the bridge only reads (and engages E-stop): '%s' is not allowed" % op, None
    fn_name = str(cmd.get("f", ""))
    if not readable(fn_name):
        return False, "the bridge only reads: planetcnc.%s is not allowed" % fn_name, None
    return True, "", jsonable(tng(fn_name, *(cmd.get("args") or [])))


# ---------------- program sync ----------------

def file_signature(path):
    try:
        st = os.stat(path)
        return (path, st.st_mtime, st.st_size)
    except OSError:
        return (path, None, None)


def read_program_file(path):
    if path and os.path.isfile(path) and os.path.getsize(path) <= MAX_PROGRAM_BYTES:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    return ""


# ---------------- main loop ----------------

def describe_error(e):
    if isinstance(e, urllib.error.HTTPError) and e.code == 403:
        return "server rejected the bridge - TOKEN must match server.py --token (without a token, server and TNG must be on the same machine)"
    if isinstance(e, urllib.error.HTTPError) and e.code == 401:
        return "server asked for a password - the bridge must not go through --viewer-password auth"
    if isinstance(e, (urllib.error.URLError, OSError)):
        reason = getattr(e, "reason", e)
        return "cannot reach %s (%s) - is server.py running there and SERVER_URL right?" % (SERVER_URL, reason)
    return "error %s: %s" % (type(e).__name__, e)


def source_changed(path, mtime):
    """True if this file was replaced by a version that compiles (a broken one is ignored)."""
    try:
        if os.path.getmtime(path) == mtime:
            return False
        with open(path, "r", encoding="utf-8") as f:
            compile(f.read(), path, "exec")
        return True
    except SyntaxError as e:
        log("error", "new version has an error, keeping the running one: %s" % e)
        return None
    except OSError:
        return False


def main():
    planetcnc.print("PlanetCNC web bridge -> " + SERVER_URL)
    me = globals().get("__file__")
    log("info", "started: protocol %d, Python %s, %s, file from %s" % (
        PROTOCOL, sys.version.split()[0], globals().get("__name__"),
        time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(me))) if me and os.path.isfile(me) else "?"))
    crash = globals().get("BRIDGE_CRASH")  # set by bridge_autostart.py when the previous run crashed
    if crash:
        log("error", "restarted after a crash:\n" + crash)
    my_mtime = os.path.getmtime(me) if me and os.path.isfile(me) else None
    spec = {}          # what to read; sent by the server on the first update
    sent_sig = None
    server_pid = None
    backoff = INTERVAL
    last_error = None  # only print when the situation changes, not on every retry
    results = []       # answers to commands, sent with the next update
    last_ok = time.time()
    offline_since = None
    while True:
        started = time.time()
        timing = {}
        lines = []
        try:
            values = read_values(spec)
            timing["read TNG"] = time.time() - started

            # Keep the server's copy of the loaded program up to date.
            path = str(values.get(spec.get("program_key")) or "")
            if spec:
                sig = file_signature(path)
                name, text = path, None
                if sig != sent_sig or not server_pid:
                    t0 = time.time()
                    if text is None:
                        if path and not os.path.isfile(path):
                            log("warn", "loaded program %s not found on disk" % path)
                        text = read_program_file(path)
                    server_pid = post("/api/program", {"name": name, "text": text}, timeout=30.0).get("program_id")
                    sent_sig = sig
                    timing["send program"] = time.time() - t0
                    log("info", "sent program %s (%.0f KB) in %.1f s" % (name or "(none)", len(text) / 1024,
                                                                          time.time() - t0))

            t0 = time.time()
            lines = take_log()
            reply = post("/api/state", {"protocol": PROTOCOL, "spec_id": spec.get("id"),
                                        "values": values, "results": results, "log": lines})
            lines = []
            timing["send update"] = time.time() - t0
            results = []
            if reply.get("spec"):
                spec = reply["spec"]
                log("info", "received reading spec %s (%d items)" % (spec.get("id"), len(spec.get("items", []))))
            for cmd in reply.get("commands") or []:
                t0 = time.time()
                try:
                    ok, msg, value = run_command(cmd)
                except Exception as e:
                    ok, msg, value = False, "%s: %s" % (type(e).__name__, e), None
                    log("error", "command #%s %s crashed:\n%s" % (cmd.get("id"), cmd.get("op"), traceback.format_exc()))
                took = time.time() - t0
                timing["command #%s" % cmd.get("id")] = took
                if cmd.get("op") != "call" or not ok:
                    log("info" if ok else "warn", "command #%s %s %s %s in %.2f s - %s" % (
                        cmd.get("id"), cmd.get("op"), cmd.get("name") or cmd.get("f") or "",
                        "ok" if ok else "FAILED", took, msg), show=True)
                else:
                    log("debug", "command #%s call %s ok in %.2f s" % (cmd.get("id"), cmd.get("f"), took))
                results.append({"id": cmd.get("id"), "ok": ok, "message": msg, "value": value})
            if reply.get("program_id") != server_pid:
                sent_sig = None  # server restarted / lost the program: resend it
            if last_error != "":
                log("info", "connected to %s%s" % (SERVER_URL, " after %.0f s without connection" % (
                    time.time() - offline_since) if offline_since else ""), show=True)
                last_error = ""
                offline_since = None
            gap = time.time() - last_ok
            if gap > 2.0:
                log("warn", "%.1f s since the previous update reached the server" % gap)
            last_ok = time.time()
            backoff = 0.02 if results else INTERVAL  # report command results right away
        except Exception as e:
            put_back_log(lines)  # not delivered: try again with the next update
            # Keep trying, slowly. Report the reason once so problems are visible in TNG.
            msg = describe_error(e)
            if msg != last_error:
                unexpected = not isinstance(e, (urllib.error.URLError, OSError))
                log("error" if unexpected else "warn", msg + " (retrying)" + (
                    "\n" + traceback.format_exc() if unexpected else ""))
                last_error = msg
                offline_since = offline_since or time.time()
            sent_sig = None
            backoff = min(backoff * 2, 5.0)

        took = time.time() - started
        if took > SLOW_LOOP and not last_error:
            log("warn", "slow update round: %.1f s (%s)" % (took, ", ".join(
                "%s %.2f s" % (k, v) for k, v in timing.items())))

        if my_mtime is not None:
            changed = source_changed(me, my_mtime)
            if changed:
                planetcnc.print("PlanetCNC web bridge: new version found, reloading")
                return "reload"  # the new version logs its start
            if changed is None:
                my_mtime = os.path.getmtime(me)  # don't report the same broken file again
        time.sleep(backoff)


_outcome = main()
RELOAD = _outcome == "reload"  # bridge_autostart.py runs the new version when it sees this
if RELOAD and __name__ != "__planetcnc_bridge__":
    # Started some other way (File -> Run Python Script): run the new version in place.
    runpy.run_path(__file__, run_name="__planetcnc_bridge_reloaded__")
