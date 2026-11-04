"""PlanetCNC web monitor - bridge.

Runs INSIDE PlanetCNC TNG's Python interpreter (it needs the `planetcnc` module)
and connects TNG to server.py.

The bridge is deliberately generic, so new dashboard features don't need a new
bridge:
  - The server sends a "spec": which TNG parameters / functions to read. The
    bridge reads them and sends the raw values with every update (5x/s).
  - The server sends commands from a small fixed vocabulary (call an allowed
    planetcnc function, run a TNG command by name, relative move, load code).
Safety checks that need TNG's state at the moment a command runs stay here:
a command can require that no program runs, or that the machine is idle.

When this file changes on disk (e.g. after update.sh), the bridge reloads
itself within a few seconds - no TNG restart needed.

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
import time
import urllib.error
import urllib.request

import planetcnc

# ---------------- configuration ----------------
SERVER_URL = "http://127.0.0.1:8080"  # where server.py runs; use the Pi's IP if TNG runs on another PC
TOKEN = ""                            # must match server.py --token (empty = none)
INTERVAL = 0.2                        # seconds between state updates (5 Hz)
MAX_PROGRAM_BYTES = 50 * 1024 * 1024  # don't upload programs bigger than this
# ------------------------------------------------

PROTOCOL = 2
AXES = "XYZABCUVW"

# planetcnc functions the server may call. Reading is harmless; the write functions
# are only ever requested from the password-protected control path of the server.
READ_PREFIXES = ("hw_", "is_", "get_", "gcode_is_", "cmd_get_", "cmd_is_")
READ_FUNCS = {"param_get", "cmd_list", "cmd_count", "msg_count", "msg_is_active", "starttime"}
WRITE_FUNCS = {"stop", "pause", "start", "estop", "param_set", "output", "output_pwm", "output_rc",
               "msg_click_ok", "msg_click_cancel"}


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


# ---------------- commands ----------------

def param_num(name):
    try:
        return float(planetcnc.param_get(name))
    except Exception:
        return 0.0


def program_running(spec):
    return bool(param_num(spec.get("guards", {}).get("run", "_hw_run")))


def machine_busy(spec):
    """Program running, machine not idle, or any axis moved in the last 0.1 s."""
    if program_running(spec) or not param_num(spec.get("guards", {}).get("idle", "_hw_idle")):
        return True
    a = tng("hw_motor")
    time.sleep(0.1)
    b = tng("hw_motor")
    return bool(a and b and any(abs(float(x) - float(y)) > 1e-4 for x, y in zip(a, b)))


def exec_tng_command(name, *args):
    i = read_item("c", name)
    if i is None:
        return False, "TNG has no command '%s'" % name
    if hasattr(planetcnc, "cmd_is_enabled") and not planetcnc.cmd_is_enabled(i):
        return False, "TNG has '%s' disabled right now" % name
    ok = planetcnc.cmd_exec(i, *args)
    return bool(ok), ("" if ok else "TNG refused '%s'" % name)


def run_command(cmd, spec, loaded):
    """Execute one command from the server. Returns (ok, message, value)."""
    guard = cmd.get("guard")
    if guard in ("idle", "not_running") and program_running(spec):
        return False, "A program is running", None
    if guard == "idle" and machine_busy(spec):
        return False, "Machine is not idle (moving, off or not connected) - wait until it stops", None

    op = cmd.get("op")
    args = cmd.get("args") or []
    if op == "call":
        fn_name = str(cmd.get("f", ""))
        if not (readable(fn_name) or fn_name in WRITE_FUNCS):
            return False, "planetcnc.%s is not allowed" % fn_name, None
        value = jsonable(tng(fn_name, *args))
        if fn_name in WRITE_FUNCS:
            return value is not False, ("%s sent" % fn_name) if value is not False else ("TNG refused %s" % fn_name), value
        return True, "", value

    if op == "cmd":
        ok, msg = exec_tng_command(str(cmd.get("name", "")), *args)
        return ok, msg, None

    if op == "move_rel":
        # Absolute machine-coordinate move from the position right now: G53 is non-modal,
        # so unlike G91 it can't leave TNG in relative mode for the next program.
        axis, dist = str(cmd.get("axis", "")), float(cmd.get("dist", 0))
        pos = tng("hw_motor")
        k = AXES.find(axis)
        if k < 0 or not pos or k >= len(pos) or dist == 0:
            return False, "invalid move", None
        line = "G53 G0 %s%.4f" % (axis, float(pos[k]) + dist)
        ok, msg = exec_tng_command(str(cmd.get("via", "")), line)
        return ok, msg or line, None

    if op == "load_code":
        name, text = str(cmd.get("name", "")), str(cmd.get("text", ""))
        ok, msg = exec_tng_command(str(cmd.get("via", "")), text)
        if ok:
            # TNG has no file name for code loaded as text; remember it for the dashboard.
            loaded.clear()
            loaded.update(name=name or "web upload", text=text, path=None)
        return ok, msg or "loaded in TNG", None

    return False, "unknown command '%s'" % op, None


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
        planetcnc.print("PlanetCNC web bridge: new version has an error, keeping the running one: %s" % e)
        return None
    except OSError:
        return False


def main():
    planetcnc.print("PlanetCNC web bridge -> " + SERVER_URL)
    me = globals().get("__file__")
    my_mtime = os.path.getmtime(me) if me and os.path.isfile(me) else None
    spec = {}          # what to read; sent by the server on the first update
    sent_sig = None
    server_pid = None
    backoff = INTERVAL
    last_error = None  # only print when the situation changes, not on every retry
    results = []       # answers to commands, sent with the next update
    loaded = {}        # program loaded from the dashboard: {name, text, path}
    while True:
        try:
            values = read_values(spec)

            # Keep the server's copy of the loaded program up to date.
            path = str(values.get(spec.get("program_key")) or "")
            if loaded:
                if loaded["path"] is None:
                    loaded["path"] = path  # whatever TNG reports for code loaded as text
                if path != loaded["path"]:
                    loaded.clear()         # another file was opened in TNG since
            if spec:
                if loaded:
                    sig = ("web", loaded["name"], len(loaded["text"]))
                    name, text = loaded["name"], loaded["text"]
                else:
                    sig = file_signature(path)
                    name, text = path, None
                if sig != sent_sig or not server_pid:
                    if text is None:
                        text = read_program_file(path)
                    server_pid = post("/api/program", {"name": name, "text": text}, timeout=30.0).get("program_id")
                    sent_sig = sig

            reply = post("/api/state", {"protocol": PROTOCOL, "spec_id": spec.get("id"),
                                        "values": values, "results": results})
            results = []
            if reply.get("spec"):
                spec = reply["spec"]
            for cmd in reply.get("commands") or []:
                try:
                    ok, msg, value = run_command(cmd, spec, loaded)
                except Exception as e:
                    ok, msg, value = False, "%s: %s" % (type(e).__name__, e), None
                if cmd.get("op") != "call" or not ok:
                    planetcnc.print("PlanetCNC web bridge: %s %s - %s" % (
                        cmd.get("op"), "ok" if ok else "FAILED", msg))
                results.append({"id": cmd.get("id"), "ok": ok, "message": msg, "value": value})
            if reply.get("program_id") != server_pid:
                sent_sig = None  # server restarted / lost the program: resend it
            if last_error != "":
                planetcnc.print("PlanetCNC web bridge: connected to " + SERVER_URL)
                last_error = ""
            backoff = 0.02 if results else INTERVAL  # report command results right away
        except Exception as e:
            # Keep trying, slowly. Report the reason once so problems are visible in TNG.
            msg = describe_error(e)
            if msg != last_error:
                planetcnc.print("PlanetCNC web bridge: " + msg + " (retrying)")
                last_error = msg
            sent_sig = None
            backoff = min(backoff * 2, 5.0)

        if my_mtime is not None:
            changed = source_changed(me, my_mtime)
            if changed:
                planetcnc.print("PlanetCNC web bridge: new version found, reloading")
                return "reload"
            if changed is None:
                my_mtime = os.path.getmtime(me)  # don't report the same broken file again
        time.sleep(backoff)


_outcome = main()
RELOAD = _outcome == "reload"  # bridge_autostart.py runs the new version when it sees this
if RELOAD and __name__ != "__planetcnc_bridge__":
    # Started some other way (File -> Run Python Script): run the new version in place.
    runpy.run_path(__file__, run_name="__planetcnc_bridge_reloaded__")
