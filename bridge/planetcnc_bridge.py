"""PlanetCNC web monitor - bridge.

Runs INSIDE PlanetCNC TNG's Python interpreter (it needs the `planetcnc` module)
and pushes the machine state and the loaded G-code program to server.py.

TNG needs Python enabled first: Settings -> Python Library must point to the
Python shared library (on the Mekanika Pi it is /usr/local/lib/libpython3.11.so.1.0;
find yours with `ls /usr/local/lib/libpython3* /usr/lib/*/libpython3*`); press "Check", then restart TNG.

Start it with File -> Run Python Script (it keeps running in the background).
It prints "connected" / the reason it can't connect in TNG's output window.

It also runs the dashboard's machine commands (home, step jog, stop, load a
program). The server only forwards those from a password-protected dashboard.
On start it writes TNG's command names to ~/tng_commands.txt, to look up the
names for CMD_HOME / CMD_MDI below.

Only the Python standard library is used.
"""

import json
import os
import time
import urllib.error
import urllib.request

import planetcnc

# ---------------- configuration ----------------
SERVER_URL = "http://127.0.0.1:8080"  # where server.py runs; use the Pi's IP if TNG runs on another PC
TOKEN = ""                            # must match server.py --token (empty = none)
INTERVAL = 0.2                        # seconds between state updates (5 Hz)
LINE_OFFSET = 0                       # add to TNG's line number if highlighting is off by one
MAX_PROGRAM_BYTES = 50 * 1024 * 1024  # don't upload programs bigger than this

# TNG command names (see ~/tng_commands.txt for the ones your TNG has)
CMD_HOME = "Machine.Home"             # home all axes
CMD_MDI = "Program.MDI"               # run one line of G-code, like typing it in MDI
CMD_OPEN_CODE = "File.Open_Code"      # load G-code text as the program (documented by PlanetCNC)
COMMANDS_DUMP = os.path.expanduser("~/tng_commands.txt")
# ------------------------------------------------

AXES = "XYZABC"


def param(name, default=None):
    try:
        v = planetcnc.param_get(name)
        return default if v is None else v
    except Exception:
        return default


def num(name, default=0.0):
    v = param(name, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def flag(name):
    return bool(num(name, 0.0))


def call(fn_name, default=None):
    fn = getattr(planetcnc, fn_name, None)
    if fn is None:
        return default
    try:
        return fn()
    except Exception:
        return default


def post(path, payload, timeout=2.0):
    req = urllib.request.Request(
        SERVER_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Bridge-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def read_state():
    work = call("hw_work") or [num("_x"), num("_y"), num("_z")]
    machine = call("hw_motor") or [num("_machine_x"), num("_machine_y"), num("_machine_z")]
    line = num("_hw_linenum", -1)
    if line < 0:
        line = num("_prog_line", 0)
    return {
        "work": [float(v) for v in work],
        "machine": [float(v) for v in machine],
        "line": int(line) + LINE_OFFSET,
        "line_count": int(num("_prog_linecnt", 0)),
        "feed": float(call("hw_speed", None) or num("_hw_speed")),
        "spindle": float(call("hw_spindle", None) or num("_hw_spindle")),
        "tool": int(num("_current_tool", 0)),
        "tool_name": str(param("_current_toolname", "") or ""),
        "coordsys": str(param("_coordsys_name", "") or ""),
        "units": "in" if num("_units", 0) == 20 else "mm",
        "running": program_running(),
        "paused": flag("_hw_pause"),
        "estop": flag("_hw_estop"),
        "idle": flag("_hw_idle"),
        "file": str(param("_prog_file", "") or ""),
        "time_to_end": num("_progress_timetoend", -1),
        "limits": read_limits(),
    }


def read_limits():
    """Machine travel (Settings -> Motors -> Limits) in machine coordinates, for drawing the bed."""
    lo = [num("_motorlimit_%sn" % a, 0.0) for a in "xyz"]
    hi = [num("_motorlimit_%sp" % a, 0.0) for a in "xyz"]
    if all(h > l for l, h in zip(lo, hi)):
        return {"min": lo, "max": hi}
    return None


# ---------------- machine commands ----------------

def cmd_id(name):
    """TNG's id for a command name, or None if this TNG doesn't have it."""
    try:
        i = planetcnc.cmd_get_id(name)
    except Exception:
        return None
    return i if isinstance(i, int) and i >= 0 else None


def available_controls():
    return {
        "home": cmd_id(CMD_HOME) is not None,
        "jog": cmd_id(CMD_MDI) is not None,
        "load": cmd_id(CMD_OPEN_CODE) is not None,
        "stop": hasattr(planetcnc, "stop"),
    }


def dump_commands():
    """Write every TNG command name to COMMANDS_DUMP, to find the right CMD_* names."""
    try:
        lines = []
        for name in planetcnc.cmd_list():
            try:
                lines.append("%-50s %s" % (name, planetcnc.cmd_get_displayname(cmd_id(name))))
            except Exception:
                lines.append(str(name))
        with open(COMMANDS_DUMP, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        planetcnc.print("PlanetCNC web bridge: could not list TNG commands: %s" % e)


def program_running():
    # _hw_run: "machine is executing the NC program". (planetcnc.is_running() is
    # something else: it only says whether TNG itself is running.)
    return flag("_hw_run")


def machine_moving():
    """True if a program runs, the machine isn't idle, or any axis moved in the last 0.1 s."""
    if program_running() or not flag("_hw_idle"):  # _hw_idle: on, connected and not moving
        return True
    a = call("hw_motor")
    time.sleep(0.1)
    b = call("hw_motor")
    return bool(a and b and any(abs(float(x) - float(y)) > 1e-4 for x, y in zip(a, b)))


def exec_cmd(name, *args):
    i = cmd_id(name)
    if i is None:
        return False, "TNG has no command '%s' - set the right name in planetcnc_bridge.py" % name
    try:
        if hasattr(planetcnc, "cmd_is_enabled") and not planetcnc.cmd_is_enabled(i):
            return False, "TNG has '%s' disabled right now" % name
        ok = planetcnc.cmd_exec(i, *args)
    except Exception as e:
        return False, "%s failed: %s" % (name, e)
    return bool(ok), ("" if ok else "TNG refused '%s'" % name)


def run_command(cmd, loaded):
    """Execute one dashboard command. Returns (ok, message)."""
    action = cmd.get("action")
    if action == "stop":
        try:
            return bool(planetcnc.stop()), "stop sent"
        except Exception as e:
            return False, "stop failed: %s" % e
    if program_running():
        return False, "A program is running"

    if action == "home":
        ok, msg = exec_cmd(CMD_HOME)
        return ok, msg or "homing started"

    if action == "jog":
        axis, dist = cmd.get("axis"), float(cmd.get("dist", 0))
        if axis not in AXES or dist == 0:
            return False, "invalid jog"
        if machine_moving():
            return False, "Machine is not idle (moving, off or not connected) - wait until it stops"
        pos = call("hw_motor")
        k = AXES.index(axis)
        if not pos or k >= len(pos):
            return False, "cannot read the machine position"
        # Absolute machine-coordinate move: G53 is non-modal, so unlike G91 it can't
        # leave TNG in relative mode for the next program.
        line = "G53 G0 %s%.4f" % (axis, float(pos[k]) + dist)
        ok, msg = exec_cmd(CMD_MDI, line)
        return ok, msg or line

    if action == "load":
        name, text = str(cmd.get("name", "")), str(cmd.get("text", ""))
        ok, msg = exec_cmd(CMD_OPEN_CODE, text)
        if ok:
            # TNG has no file name for code loaded this way; remember it for the dashboard.
            loaded.clear()
            loaded.update(name=name or "web upload", text=text, file=None)
        return ok, msg or "loaded in TNG"

    return False, "unknown action"


def program_signature(path):
    try:
        st = os.stat(path)
        return (path, st.st_mtime, st.st_size)
    except OSError:
        return (path, None, None)


def upload_program(path):
    text = ""
    if path and os.path.isfile(path) and os.path.getsize(path) <= MAX_PROGRAM_BYTES:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    return post("/api/program", {"name": path, "text": text}, timeout=30.0).get("program_id")


def main():
    planetcnc.print("PlanetCNC web bridge -> " + SERVER_URL)
    dump_commands()
    controls = available_controls()
    sent_sig = None
    server_pid = None
    backoff = INTERVAL
    last_error = None  # only print when the situation changes, not on every retry
    results = []       # answers to dashboard commands, sent with the next update
    loaded = {}        # program loaded from the dashboard: {name, text, file}
    while True:
        try:
            state = read_state()
            state["controls"] = controls
            if loaded:
                if loaded["file"] is None:
                    loaded["file"] = state["file"]  # whatever TNG reports for code loaded as text
                if state["file"] != loaded["file"]:
                    loaded.clear()                  # another file was opened in TNG since
            if loaded:
                sig = ("web", loaded["name"], len(loaded["text"]))
                if sig != sent_sig or not server_pid:
                    server_pid = post("/api/program", {"name": loaded["name"], "text": loaded["text"]},
                                      timeout=30.0).get("program_id")
                    sent_sig = sig
            else:
                sig = program_signature(state["file"])
                if sig != sent_sig or not server_pid:
                    server_pid = upload_program(state["file"])
                    sent_sig = sig
            state["results"] = results
            reply = post("/api/state", state)
            results = []
            for cmd in reply.get("commands") or []:
                ok, msg = run_command(cmd, loaded)
                planetcnc.print("PlanetCNC web bridge: %s %s - %s" % (
                    cmd.get("action"), "ok" if ok else "FAILED", msg))
                results.append({"id": cmd.get("id"), "ok": ok, "message": msg})
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
        time.sleep(backoff)


def describe_error(e):
    if isinstance(e, urllib.error.HTTPError) and e.code == 403:
        return "server rejected the bridge - TOKEN must match server.py --token (without a token, server and TNG must be on the same machine)"
    if isinstance(e, urllib.error.HTTPError) and e.code == 401:
        return "server asked for a password - the bridge must not go through --viewer-password auth"
    if isinstance(e, (urllib.error.URLError, OSError)):
        reason = getattr(e, "reason", e)
        return "cannot reach %s (%s) - is server.py running there and SERVER_URL right?" % (SERVER_URL, reason)
    return "error %s: %s" % (type(e).__name__, e)


main()
