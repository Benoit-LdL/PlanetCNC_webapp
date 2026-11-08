#!/usr/bin/env python3
"""PlanetCNC web monitor - server.

Receives machine state and the loaded G-code program from the bridge script
running inside PlanetCNC TNG (bridge/planetcnc_bridge.py) and serves a live
web dashboard to any browser.

Standard library only - no pip install required.

    python server.py                      # listen on 0.0.0.0:8080
    python server.py --port 8090 --token secret
    python server.py --demo               # simulated machine, no PlanetCNC needed

Camera: a USB webcam is streamed to the dashboard through ffmpeg
(`sudo apt install ffmpeg`). By default /dev/video0 is used if it exists;
ffmpeg only runs while someone is watching.

    python server.py --camera /dev/video1 --camera-size 1280x720 --camera-fps 15
    python server.py --camera none        # disable the camera panel
    python server.py --camera-cmd "rpicam-vid -t 0 -n --codec mjpeg -o -"   # any MJPEG-on-stdout source
"""

import argparse
import base64
import hashlib
import hmac
import json
import logging
import logging.handlers
import math
import mimetypes
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
BRIDGE_TIMEOUT = 3.0  # seconds without an update before the bridge is reported offline
COMMAND_TTL = 2.0      # a command the bridge hasn't picked up by then is dropped, never run late
COMMAND_WAIT = 10.0    # how long a browser request waits for TNG's answer
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
CONSOLE_LINES = 500      # console history kept in memory

# ---------------------------------------------------------------------------
# What the (generic) bridge reads from TNG, and how that becomes dashboard state.
# Changing anything here needs no bridge update: the bridge receives this spec.
# ---------------------------------------------------------------------------

TNG_TOOL_DIAMETER_PARAMS = ("_current_tooldiameter", "_current_tool_diameter", "_current_tooldia", "_tool_diameter")

# Each item: [key, kind, name] - kind p = TNG parameter, f = planetcnc function
# without arguments, c = command id (to know whether TNG has that command).
_ITEMS = [
    ["work", "f", "hw_work"], ["machine", "f", "hw_motor"],
    ["x", "p", "_x"], ["y", "p", "_y"], ["z", "p", "_z"],
    ["mx", "p", "_machine_x"], ["my", "p", "_machine_y"], ["mz", "p", "_machine_z"],
    ["linenum", "p", "_hw_linenum"], ["prog_line", "p", "_prog_line"], ["line_count", "p", "_prog_linecnt"],
    ["speed", "f", "hw_speed"], ["speed_p", "p", "_hw_speed"],
    ["spindle", "f", "hw_spindle"], ["spindle_p", "p", "_hw_spindle"],
    ["tool", "p", "_current_tool"], ["tool_name", "p", "_current_toolname"],
] + [
    # Diameter of the loaded tool from TNG's tool table. The parameter name isn't documented
    # anywhere we could check, so a few likely spellings are tried; unknown ones read as nothing.
    ["tool_dia_%d" % i, "p", name] for i, name in enumerate(TNG_TOOL_DIAMETER_PARAMS)
] + [
    ["coordsys", "p", "_coordsys_name"], ["units", "p", "_units"],
    ["run", "p", "_hw_run"], ["pause", "p", "_hw_pause"], ["estop", "p", "_hw_estop"], ["idle", "p", "_hw_idle"],
    ["file", "p", "_prog_file"], ["time_to_end", "p", "_progress_timetoend"],
] + [["lim_%s%s" % (a, d), "p", "_motorlimit_%s%s" % (a, d)] for a in "xyz" for d in "np"]

BRIDGE_SPEC = {
    "items": _ITEMS,
    "program_key": "file",                          # value holding the loaded program's path
}
BRIDGE_SPEC["id"] = hashlib.sha1(json.dumps(BRIDGE_SPEC, sort_keys=True).encode()).hexdigest()[:12]

# planetcnc functions anyone with dashboard access may call through /api/tng/call (reading only)
TNG_READ_PREFIXES = ("hw_", "is_", "get_", "gcode_is_", "cmd_get_", "cmd_is_")
TNG_READ_FUNCS = {"param_get", "cmd_list", "cmd_count", "msg_count", "msg_is_active", "starttime"}

# ---------------------------------------------------------------------------
# Logging: everything worth knowing afterwards goes to logs/server.log (rotated, cleaned
# up automatically) and the important part also to stdout (journalctl).
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-14s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE = "server.log"
LOG_FILE_COUNT = 10            # server.log + 9 older pieces; the size limit is shared between them
LOG_CLEANUP_EVERY = 3600       # seconds between cleanup runs
LOG_MIN_FREE_DISK = 500 * 2**20  # below this much free space, only the current log file is kept
SLOW_REQUEST = 3.0             # requests taking longer than this are logged as warnings
STREAM_WRITE_TIMEOUT = 20.0    # a browser that stops reading a live stream for this long is dropped
BRIDGE_GAP_WARN = 1.5          # bridge updates normally come every 0.2 s
HEARTBEAT_EVERY = 300          # seconds between status lines in the log
CLIENT_LOG_LIMIT = 120         # lines per minute a browser may send to the log

LOG = logging.getLogger("pcnc")
log_http = LOG.getChild("http")
log_bridge = LOG.getChild("bridge")
log_machine = LOG.getChild("machine")
log_camera = LOG.getChild("camera")
log_update = LOG.getChild("update")
log_files = LOG.getChild("files")
log_client = LOG.getChild("browser")
log_system = LOG.getChild("system")


class LogManager:
    """Sets up the log handlers and keeps the log folder within its size and age limits."""

    def __init__(self):
        self.dir = ""
        self.max_bytes = 0
        self.keep_days = 0
        self.file_handler = None
        self.low_disk_reported = False

    def setup(self, log_dir, max_mb, keep_days, debug):
        LOG.setLevel(logging.DEBUG)
        LOG.propagate = False
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))  # journald adds the time
        LOG.addHandler(console)

        self.max_bytes = max(1, max_mb) * 2**20
        self.keep_days = keep_days
        if log_dir != "none":
            try:
                os.makedirs(log_dir, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(
                    os.path.join(log_dir, LOG_FILE), maxBytes=self.max_bytes // LOG_FILE_COUNT,
                    backupCount=LOG_FILE_COUNT - 1, encoding="utf-8")
                handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
                LOG.addHandler(handler)
                self.dir, self.file_handler = os.path.abspath(log_dir), handler
            except OSError as e:
                LOG.error("cannot write logs to %s: %s - logging to the console only", log_dir, e)
        self.set_debug(debug)

        # Crashes in any thread end up in the log too, with their traceback.
        threading.excepthook = lambda a: LOG.critical(
            "unexpected error in thread %s", a.thread.name if a.thread else "?",
            exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
        sys.excepthook = lambda t, v, tb: LOG.critical("unexpected error", exc_info=(t, v, tb))
        if self.dir:
            threading.Thread(target=self._cleanup_loop, name="log-cleanup", daemon=True).start()

    def set_debug(self, on):
        if self.file_handler:
            self.file_handler.setLevel(logging.DEBUG if on else logging.INFO)

    def debug_enabled(self):
        return bool(self.file_handler) and self.file_handler.level <= logging.DEBUG

    def files(self):
        """[{name, size, mtime}] of the log folder, newest first."""
        out = []
        if self.dir:
            with os.scandir(self.dir) as it:
                for e in it:
                    if e.is_file():
                        st = e.stat()
                        out.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime})
        return sorted(out, key=lambda f: -f["mtime"])

    def path(self, name):
        """Full path of a file in the log folder, or None."""
        if not self.dir or not name or name != os.path.basename(name):
            return None
        full = os.path.join(self.dir, name)
        return full if os.path.isfile(full) else None

    def cleanup(self):
        """Delete log files older than keep_days, then the oldest ones while the folder is over
        its size limit or the disk is nearly full. The file being written is never deleted."""
        active = os.path.join(self.dir, LOG_FILE)
        files = [f for f in self.files() if os.path.join(self.dir, f["name"]) != active]
        removed, freed = [], 0
        cutoff = time.time() - self.keep_days * 86400
        total = sum(f["size"] for f in self.files())
        try:
            low_disk = shutil.disk_usage(self.dir).free < LOG_MIN_FREE_DISK
        except OSError:
            low_disk = False
        for f in sorted(files, key=lambda f: f["mtime"]):  # oldest first
            if f["mtime"] < cutoff or total > self.max_bytes or low_disk:
                try:
                    os.remove(os.path.join(self.dir, f["name"]))
                except OSError as e:
                    LOG.warning("log cleanup: cannot delete %s: %s", f["name"], e)
                    continue
                removed.append(f["name"])
                freed += f["size"]
                total -= f["size"]
        if removed:
            LOG.info("log cleanup: deleted %d old file%s (%.1f MB): %s", len(removed), "" if len(removed) == 1 else "s",
                     freed / 2**20, ", ".join(removed))
        if low_disk and not self.low_disk_reported:
            LOG.warning("disk almost full (less than %d MB free) - keeping only the current log file",
                        LOG_MIN_FREE_DISK // 2**20)
        self.low_disk_reported = low_disk

    def _cleanup_loop(self):
        while True:
            try:
                self.cleanup()
            except Exception:
                LOG.exception("log cleanup failed")
            time.sleep(LOG_CLEANUP_EVERY)


LOGS = LogManager()


def short_agent(ua):
    """Browser and OS from a User-Agent, enough to tell devices apart in the log."""
    ua = ua or ""
    browser = next((b for b in ("Edg/", "OPR/", "Firefox/", "Chrome/", "Safari/") if b in ua), "")
    system = next((s for s in ("Android", "iPhone", "iPad", "Windows", "Mac OS", "Linux") if s in ua), "")
    return ("%s %s" % (browser.rstrip("/").replace("Edg", "Edge"), system)).strip() or ua[:40] or "unknown"


class Streams:
    """Counts the open live connections (dashboards and camera viewers) for the log."""

    def __init__(self):
        self.lock = threading.Lock()
        self.open = {"dashboard": 0, "camera": 0}

    def change(self, kind, delta):
        with self.lock:
            self.open[kind] += delta
            return self.open[kind]


STREAMS = Streams()



def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def derive_state(v):
    """Raw values from the bridge -> the state the dashboard shows."""
    def axes(fn_key, param_keys):
        vals = v.get(fn_key)
        if isinstance(vals, list) and vals:
            return [_num(x) for x in vals]
        return [_num(v.get(k)) for k in param_keys]

    line = _num(v.get("linenum"), -1)
    if line < 0:
        line = _num(v.get("prog_line"))
    lo = [_num(v.get("lim_%sn" % a)) for a in "xyz"]
    hi = [_num(v.get("lim_%sp" % a)) for a in "xyz"]
    return {
        "work": axes("work", ("x", "y", "z")),
        "machine": axes("machine", ("mx", "my", "mz")),
        "line": int(line) + CONFIG.line_offset,
        "line_count": int(_num(v.get("line_count"))),
        "feed": _num(v.get("speed")) or _num(v.get("speed_p")),
        "spindle": _num(v.get("spindle")) or _num(v.get("spindle_p")),
        "tool": int(_num(v.get("tool"))),
        "tool_name": str(v.get("tool_name") or ""),
        # First likely tool-table parameter that gives a real diameter; 0 = TNG didn't tell us.
        "tool_diameter": next((d for d in (_num(v.get("tool_dia_%d" % i)) for i in range(len(TNG_TOOL_DIAMETER_PARAMS)))
                               if 0 < d < 1000), 0.0),
        "coordsys": str(v.get("coordsys") or ""),
        "units": "in" if _num(v.get("units")) == 20 else "mm",
        "running": bool(_num(v.get("run"))),        # _hw_run: executing the NC program
        "paused": bool(_num(v.get("pause"))),
        "estop": bool(_num(v.get("estop"))),
        "idle": bool(_num(v.get("idle"))),          # _hw_idle: on, connected, not moving
        "file": str(v.get("file") or ""),
        "time_to_end": _num(v.get("time_to_end"), -1),
        "limits": {"min": lo, "max": hi} if all(h > l for l, h in zip(lo, hi)) else None,
    }


class Hub:
    """Holds the latest machine state and program, and wakes SSE listeners on change."""

    def __init__(self):
        self.cond = threading.Condition()
        self.state = {}
        self.state_seq = 0
        self.last_update = 0.0
        self.last_state_time = 0.0
        self.program = {"id": "", "name": "", "text": ""}
        self.commands = []   # (queued_at, command) waiting for the bridge
        self.results = {}    # command id -> {"ok": bool, "message": str}
        self.next_cmd_id = 1
        self.console = deque(maxlen=CONSOLE_LINES)
        self.console_seq = 0
        self.cmd_info = {}   # command id -> (queued_at, description), for the log
        self.bridge_updates = 0
        self.offline_since = None
        self.job_started = None      # time.time() when the running job started (None when no job runs)

    # ----- machine commands: browser -> server -> bridge (in its /api/state reply) -----

    def queue_command(self, cmd):
        with self.cond:
            cmd = dict(cmd, id=self.next_cmd_id)
            self.next_cmd_id += 1
            self.commands.append((time.time(), cmd))
            desc = cmd.get("f") or cmd.get("name") or cmd.get("op")
            self.cmd_info[cmd["id"]] = (time.time(), desc)
            log_bridge.debug("command #%d queued: %s %s", cmd["id"], cmd.get("op"), desc)
            return cmd["id"]

    def take_commands(self):
        """Hand the fresh commands to the bridge; expire the stale ones."""
        with self.cond:
            now = time.time()
            fresh = []
            for queued_at, cmd in self.commands:
                if now - queued_at <= COMMAND_TTL:
                    fresh.append(cmd)
                else:
                    self.results[cmd["id"]] = {"ok": False, "message": "TNG did not pick up the command in time"}
                    log_bridge.warning("command #%d (%s) expired: the bridge did not pick it up within %.0f s",
                                       cmd["id"], cmd.get("op"), COMMAND_TTL)
            if fresh:
                log_bridge.debug("handing %d command(s) to the bridge: %s", len(fresh),
                                 ", ".join("#%d" % c["id"] for c in fresh))
            self.commands = []
            self.cond.notify_all()
            return fresh

    def add_results(self, results):
        with self.cond:
            for r in results:
                if isinstance(r, dict) and "id" in r:
                    self.results[r["id"]] = {"ok": bool(r.get("ok")), "message": str(r.get("message") or ""),
                                             "value": r.get("value")}
                    queued_at, desc = self.cmd_info.pop(r["id"], (None, "?"))
                    took = " in %d ms" % ((time.time() - queued_at) * 1000) if queued_at else ""
                    (log_bridge.debug if r.get("ok") else log_bridge.warning)(
                        "command #%s (%s) %s%s%s", r["id"], desc, "ok" if r.get("ok") else "FAILED", took,
                        (": " + str(r.get("message"))) if r.get("message") else "")
            self.cond.notify_all()

    def wait_result(self, cmd_id):
        end = time.time() + COMMAND_WAIT
        with self.cond:
            while cmd_id not in self.results:
                for queued_at, cmd in self.commands:
                    if cmd["id"] == cmd_id and time.time() - queued_at > COMMAND_TTL:
                        self.commands.remove((queued_at, cmd))
                        log_bridge.warning("command #%d dropped: the bridge is not responding", cmd_id)
                        return {"ok": False, "message": "TNG bridge is not responding - command dropped"}
                remaining = end - time.time()
                if remaining <= 0:
                    log_bridge.warning("command #%d: no answer from TNG within %.0f s", cmd_id, COMMAND_WAIT)
                    self.cmd_info.pop(cmd_id, None)
                    return {"ok": False, "message": "No answer from TNG"}
                self.cond.wait(min(remaining, 0.5))
            return self.results.pop(cmd_id)

    # ----- console: what happened, for the dashboard's Console tab -----

    def log(self, kind, text):
        """A line for the dashboard's Console tab (also written to the log). kind: info | ok | warn | err | cmd | tng"""
        level = logging.ERROR if kind == "err" else logging.WARNING if kind == "warn" else logging.INFO
        log_machine.log(level, "%s%s", "command: " if kind == "cmd" else "", text)
        with self.cond:
            self.console_seq += 1
            self.console.append({"seq": self.console_seq, "t": time.time(), "kind": kind, "text": text})
            self.cond.notify_all()

    def console_since(self, after):
        with self.cond:
            return [line for line in self.console if line["seq"] > after]

    def _log_changes(self, old, new):
        """Turn state changes reported by the bridge into console lines."""
        def changed(key):
            return bool(old.get(key)) != bool(new.get(key))
        if changed("estop"):
            self.log("err" if new.get("estop") else "info", "E-stop active" if new.get("estop") else "E-stop released")
        if new.get("file") != old.get("file") and new.get("file"):
            self.log("info", "Program loaded: %s" % new["file"])
        if changed("running"):
            if new.get("running"):
                self.log("info", "Job started: %s" % (new.get("file") or "program"))
            else:
                took = ""
                if self.job_started:
                    secs = int(time.time() - self.job_started)
                    took = " after %d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)
                self.log("info", "Job ended at line %s of %s%s" % (new.get("line", "?"), new.get("line_count", "?"), took))
        if changed("paused"):
            self.log("warn" if new.get("paused") else "info", "Paused" if new.get("paused") else "Resumed")
        # Only in the log: useful when reconstructing what happened.
        if new.get("tool") != old.get("tool") or new.get("tool_diameter") != old.get("tool_diameter"):
            log_machine.info("tool changed: T%s -> T%s %s (diameter from TNG: %s)", old.get("tool"), new.get("tool"),
                             new.get("tool_name") or "", new.get("tool_diameter") or "unknown")
        if new.get("units") != old.get("units") or new.get("coordsys") != old.get("coordsys"):
            log_machine.info("units %s, coordinate system %s", new.get("units"), new.get("coordsys") or "-")
        if changed("idle"):
            log_machine.debug("machine %s", "idle" if new.get("idle") else "busy")

    def set_state(self, state, source=""):
        with self.cond:
            gap = time.time() - self.last_state_time
            if source and gap >= BRIDGE_TIMEOUT:
                if self.offline_since:
                    log_bridge.info("connected from %s (protocol %s) after %.0f s offline",
                                    source, state.get("bridge_protocol"), time.time() - self.offline_since)
                else:
                    log_bridge.info("connected from %s (protocol %s)", source, state.get("bridge_protocol"))
                self.offline_since = None
                self.log("ok", "TNG bridge connected")
            else:
                if source and gap > BRIDGE_GAP_WARN:
                    log_bridge.warning("no update from the bridge for %.1f s (TNG busy, or the Pi overloaded?)", gap)
                if self.state:
                    self._log_changes(self.state, state)
            # Job start time, kept here so a dashboard opened mid-job still shows the real elapsed time.
            if not state.get("running"):
                self.job_started = None
            elif self.job_started is None:
                self.job_started = time.time()
            self.bridge_updates += 1
            self.last_state_time = time.time()
            self.state = state
            self.state_seq += 1
            self.last_update = time.time()
            self.cond.notify_all()

    def set_program(self, name, text):
        pid = hashlib.sha1((name + "\0" + text).encode("utf-8", "replace")).hexdigest()[:16]
        with self.cond:
            self.program = {"id": pid, "name": name, "text": text}
            self.last_update = time.time()
            self.cond.notify_all()
        return pid

    def snapshot(self):
        with self.cond:
            s = dict(self.state)
            s["program_id"] = self.program["id"]
            s["program_name"] = self.program["name"]
            s["bridge_online"] = (time.time() - self.last_update) < BRIDGE_TIMEOUT
            s["server_time"] = time.time()
            s["job_started"] = self.job_started
            seq = self.state_seq
        s["camera"] = {"live": CAMERA.live()} if CAMERA and CAMERA.enabled() else None
        s["machine_name"] = SETTINGS["name"]
        s["tool_view"] = {"enabled": bool(SETTINGS["tool_shape"]), "cam": SETTINGS["tool_cam"]}
        s["log_debug"] = LOGS.debug_enabled()
        s["files_enabled"] = bool(CONFIG.files_dir)
        s["system"] = SYSTEM.latest if SYSTEM else None
        s["console_seq"] = HUB.console_seq
        s["update"] = UPDATER.summary() if UPDATER else None
        return seq, s


# ---------------------------------------------------------------------------
# Camera: ffmpeg -> MJPEG frames -> multipart HTTP stream
# ---------------------------------------------------------------------------

CAMERA_IDLE_STOP = 10.0   # stop ffmpeg this many seconds after the last viewer leaves
CAMERA_STALL = 10.0       # restart ffmpeg if it produces no data for this long
CAMERA_KEEPALIVE = 2.0    # resend a frame this often so dead viewer connections get noticed
# 16x9 dark JPEG sent while there is no camera image
NO_SIGNAL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMQD/2wBDAAgUFBcUFxsbGxsbGyAeICEhISAgICAhISEkJCQq"
    "KiokJCQhISQkKCgqKi4vLisrKisvLzIyMjw8OTlGRkhWVmf/xABMAAEBAAAAAAAAAAAAAAAAAAAABwEBAQAAAAAAAAAAAAAA"
    "AAAAAgMQAQAAAAAAAAAAAAAAAAAAAAARAQAAAAAAAAAAAAAAAAAAAAD/wAARCAAJABADASIAAhEAAxEA/9oADAMBAAIRAxEA"
    "PwCKAGm//9k=")
SYS_V4L = "/sys/class/video4linux"
V4L_BY_ID = "/dev/v4l/by-id"
# Video devices of the Pi itself (hardware codecs, image processor), not cameras
NOT_CAMERAS = ("bcm2835-codec", "bcm2835-isp", "rpivid", "rpi-hevc", "pispbe", "hevc")


def _read_text(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def list_cameras():
    """Capture devices as [{"path", "label"}], by their stable /dev/v4l/by-id name where there is one
    (a USB camera keeps that name when it is plugged into another port)."""
    by_id = {}
    try:
        for n in sorted(os.listdir(V4L_BY_ID)):
            full = os.path.join(V4L_BY_ID, n)
            by_id.setdefault(os.path.realpath(full), full)
    except OSError:
        pass
    try:
        names = os.listdir(SYS_V4L)
    except OSError:
        return []
    cams = []
    for n in sorted(names, key=lambda n: int(n[5:]) if n[5:].isdigit() else 1 << 30):
        base = os.path.join(SYS_V4L, n)
        # A USB camera has several nodes; index 0 is the picture, the others metadata.
        if not n.startswith("video") or _read_text(os.path.join(base, "index"), "0") != "0":
            continue
        name = _read_text(os.path.join(base, "name"), n)
        if name.startswith(NOT_CAMERAS):
            continue
        dev = "/dev/" + n
        cams.append({"path": by_id.get(dev, dev), "label": "%s (%s)" % (name, dev)})
    return cams


class Camera:
    """Runs ffmpeg while at least one viewer is connected and keeps the latest JPEG frame.

    With a V4L2 device it first asks the camera for MJPEG and copies the frames
    untouched (almost no CPU on a Raspberry Pi). If the camera can't do MJPEG it
    falls back to raw capture + JPEG encoding in ffmpeg.

    `choice` is "auto" (the first camera found), "none", or a device path.
    """

    def __init__(self, choice="auto", size="", fps=0, command=""):
        self.choice, self.size, self.fps, self.command = choice, size, fps, command
        self.cond = threading.Condition()
        self.frame = None
        self.frame_seq = 0
        self.frame_time = 0.0
        self.clients = 0
        self.transcode = False       # current capture mode for a V4L2 device
        self.mode_confirmed = False  # a mode has produced frames: stop alternating
        self.proc = None
        self.stop_reason = ""
        self.generation = 0          # bumped when the device changes
        self._resolved = (0.0, None)
        self._reported = set()  # errors already printed since the last good capture
        threading.Thread(target=self._run, daemon=True).start()

    def device(self):
        """The device to open now, or None. Looked up at most every few seconds."""
        if self.choice == "none":
            return None
        if self.choice != "auto":
            return self.choice
        t, dev = self._resolved
        if time.time() - t > 3.0:
            cams = list_cameras()
            dev = cams[0]["path"] if cams else None
            self._resolved = (time.time(), dev)
        return dev

    def enabled(self):
        """Whether the dashboard shows the camera panel. A chosen camera that is unplugged
        still shows (as 'no signal'); 'auto' without any camera doesn't."""
        return bool(self.command) or self.device() is not None

    def set_choice(self, choice):
        with self.cond:
            self.choice = choice
            self._resolved = (0.0, None)
            self.transcode = self.mode_confirmed = False
            self.generation += 1
            self._reported.clear()
            if self.proc:
                self.stop_reason = "camera changed"
                self.proc.kill()  # _run starts over with the new device
            self.cond.notify_all()
        log_camera.info("camera set to %s", choice)

    def live(self):
        return time.time() - self.frame_time < 3.0

    def add_client(self):
        with self.cond:
            self.clients += 1
            self.cond.notify_all()
            log_camera.debug("viewer added (%d watching)", self.clients)

    def remove_client(self):
        with self.cond:
            self.clients -= 1
            log_camera.debug("viewer left (%d watching)", self.clients)

    def wait_frame(self, last_seq, timeout):
        """Return (jpeg, seq): a newer frame, or after `timeout` a keepalive frame."""
        with self.cond:
            self.cond.wait_for(lambda: self.frame_seq != last_seq, timeout=timeout)
            if self.frame is not None and self.live():
                return self.frame, self.frame_seq
            return NO_SIGNAL_JPEG, self.frame_seq

    def _cmd(self, device):
        if self.command:
            return shlex.split(self.command)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "v4l2"]
        if not self.transcode:
            cmd += ["-input_format", "mjpeg"]
        if self.size:
            cmd += ["-video_size", self.size]
        if self.fps:
            cmd += ["-framerate", str(self.fps)]
        cmd += ["-i", device]
        cmd += ["-c:v", "mjpeg", "-q:v", "6"] if self.transcode else ["-c:v", "copy"]
        return cmd + ["-f", "mjpeg", "-"]

    def _run(self):
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.clients > 0)
            device = None if self.command else self.device()
            if not self.command and not device:
                with self.cond:
                    self.cond.wait(2.0)  # nothing to open; woken early when the choice changes
                continue
            gen = self.generation
            got = self._capture(device, gen)
            if gen != self.generation:
                continue  # switched to another device meanwhile: start fresh
            if got:
                self.mode_confirmed = True
            else:
                if not self.command and not self.mode_confirmed:
                    self.transcode = not self.transcode  # try the other capture mode next
                time.sleep(2.0)

    def _capture(self, device, gen):
        """Run one ffmpeg process until it dies, stalls or nobody watches. True if it produced frames."""
        cmd = self._cmd(device)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            self._report("cannot start %s: %s" % (cmd[0], e))
            return False
        started = time.time()
        log_camera.info("starting capture (pid %d): %s", proc.pid, " ".join(cmd))
        with self.cond:
            self.proc = proc
            self.stop_reason = ""
            if gen != self.generation:
                self.stop_reason = "camera changed"
                proc.kill()  # the choice changed while starting

        errors = deque(maxlen=20)
        threading.Thread(target=lambda: errors.extend(
            ln.decode("utf-8", "replace").strip() for ln in proc.stderr), daemon=True).start()

        last_data = [time.time()]
        stop = threading.Event()

        def watchdog():
            idle_since = None
            while not stop.wait(1.0):
                now = time.time()
                with self.cond:
                    idle_since = None if self.clients else (idle_since or now)
                if now - last_data[0] > CAMERA_STALL or (idle_since and now - idle_since > CAMERA_IDLE_STOP):
                    self.stop_reason = ("no viewers for %d s" % CAMERA_IDLE_STOP if idle_since and
                                        now - idle_since > CAMERA_IDLE_STOP else
                                        "stalled: no data from the camera for %d s" % CAMERA_STALL)
                    proc.kill()
                    return
        threading.Thread(target=watchdog, daemon=True).start()

        got = False
        frames = 0
        buf = bytearray()
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                last_data[0] = time.time()
                buf += chunk
                # Split the concatenated JPEGs on SOI/EOI markers (0xFFD9 can't occur
                # inside entropy-coded data, so this is safe for webcam frames).
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        del buf[:-1]
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        del buf[:start]
                        break
                    self._publish(bytes(buf[start:end + 2]))
                    got = True
                    frames += 1
                    del buf[:end + 2]
                if len(buf) > 16 * 1024 * 1024:
                    buf.clear()
        finally:
            stop.set()
            proc.kill()
            code = proc.wait()
            with self.cond:
                self.proc = None
        reason = self.stop_reason or "exited with code %s" % code
        took = time.time() - started
        (log_camera.info if got else log_camera.debug)(
            "capture stopped after %.0f s, %d frames (%.1f/s): %s", took, frames, frames / max(took, 0.001), reason)
        if errors:
            log_camera.debug("%s said: %s", cmd[0], " | ".join(errors))
        if got:
            self._reported.clear()
        else:
            self._report("%s produced no image%s" % (cmd[0], (" - " + " | ".join(list(errors)[-3:])) if errors else ""))
        return got

    def _publish(self, jpeg):
        with self.cond:
            self.frame = jpeg
            self.frame_seq += 1
            self.frame_time = time.time()
            self.cond.notify_all()

    def _report(self, msg):
        if msg not in self._reported:  # don't flood the log while a camera is unplugged
            log_camera.warning(msg)
            self._reported.add(msg)
        else:
            log_camera.debug(msg)


# ---------------------------------------------------------------------------
# Raspberry Pi health: CPU, RAM, temperature, disk, power (Linux only)
# ---------------------------------------------------------------------------

PROC_STAT = "/proc/stat"
PROC_MEMINFO = "/proc/meminfo"
PROC_UPTIME = "/proc/uptime"
THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"


class SystemStats:
    """Samples the Pi's health every few seconds; `latest` is sent with every state update."""

    def __init__(self, interval=2.0):
        self.interval = interval
        self.latest = None
        self._last_cpu = None
        self._throttled = None
        self._throttle_checked = 0.0
        self._levels = {}   # last reported level per warning, so each change is logged once
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def available():
        return os.path.exists(PROC_STAT)

    def _run(self):
        while True:
            try:
                self.latest = self._sample()
                self._warn(self.latest)
            except Exception:  # never let a stats hiccup take the server down
                log_system.exception("system stats failed")
            time.sleep(self.interval)

    def _level(self, key, level, text):
        """Log `text` when the level of `key` changes (None = all fine)."""
        if self._levels.get(key) == level:
            return
        if level or key in self._levels:
            log_system.log(logging.WARNING if level else logging.INFO, text)
        self._levels[key] = level

    def _warn(self, s):
        t = s.get("throttled")
        if t is not None:
            now = t & 0xF
            words = [w for bit, w in ((1, "under-voltage"), (2, "CPU frequency capped"), (4, "throttled"),
                                      (8, "temperature limit")) if now & bit]
            self._level("power", now or None, "Pi power/temperature: " + (", ".join(words) if words else "back to normal")
                        + " (vcgencmd get_throttled = 0x%x)" % t)
        temp = s.get("temp")
        if temp is not None:
            self._level("temp", "hot" if temp >= 80 else "warm" if temp >= 75 else None,
                        "Pi temperature %.0f \u00b0C%s" % (temp, " - the Pi slows itself down from 80 \u00b0C"
                                                            if temp >= 75 else ""))
        if s.get("mem_total"):
            used = s["mem_used"] / s["mem_total"]
            self._level("mem", "high" if used >= 0.9 else None, "Pi memory %.0f %% used" % (used * 100))
        if s.get("disk_total"):
            used = s["disk_used"] / s["disk_total"]
            self._level("disk", "high" if used >= 0.9 else None, "Pi disk %.0f %% full" % (used * 100))

    def _cpu_percent(self):
        with open(PROC_STAT) as f:
            vals = [int(v) for v in f.readline().split()[1:]]
        idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)  # idle + iowait
        prev, self._last_cpu = self._last_cpu, (idle, total)
        if not prev or total == prev[1]:
            return None
        return 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))

    def _sample(self):
        s = {"cpu": self._cpu_percent()}
        mem = {}
        with open(PROC_MEMINFO) as f:
            for line in f:
                key, _, rest = line.partition(":")
                mem[key] = int(rest.split()[0]) * 1024
        if "MemTotal" in mem and "MemAvailable" in mem:
            s["mem_total"] = mem["MemTotal"]
            s["mem_used"] = mem["MemTotal"] - mem["MemAvailable"]
        try:
            with open(THERMAL_ZONE) as f:
                s["temp"] = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            s["temp"] = None
        try:
            du = shutil.disk_usage("/")
            s["disk_total"], s["disk_used"] = du.total, du.used
        except OSError:
            pass
        try:
            with open(PROC_UPTIME) as f:
                s["uptime"] = float(f.read().split()[0])
        except (OSError, ValueError):
            pass
        s["throttled"] = self._get_throttled()
        return s

    def _get_throttled(self):
        """Raspberry Pi firmware flags (under-voltage, throttling), checked every 10 s."""
        if time.time() - self._throttle_checked >= 10.0:
            self._throttle_checked = time.time()
            exe = shutil.which("vcgencmd")
            if exe:
                try:
                    out = subprocess.run([exe, "get_throttled"], capture_output=True, text=True, timeout=3).stdout
                    self._throttled = int(out.strip().split("=")[1], 16)  # "throttled=0x50005"
                except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                    self._throttled = None
        return self._throttled


# ---------------------------------------------------------------------------
# Software update from GitHub (the app folder must be a git clone; see update.sh)
# ---------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATE_SCRIPT = os.path.join(APP_DIR, "update.sh")
UPDATE_CHECK_EVERY = 6 * 3600
UPDATES_OFF_REASON = "updates are not available"


class Updater:
    def __init__(self):
        self.lock = threading.Lock()
        self.info = {"git": True, "checked": None, "error": ""}
        self.busy = False
        threading.Thread(target=self._auto_check, daemon=True).start()

    @staticmethod
    def available():
        return bool(shutil.which("git")) and os.path.isdir(os.path.join(APP_DIR, ".git"))

    def _git(self, *args, timeout=30):
        r = subprocess.run(["git", *args], cwd=APP_DIR, capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip() or "git %s failed" % args[0])
        return r.stdout.rstrip()  # keep leading spaces: `git status --porcelain` lines start with one

    def _commit(self, ref):
        sha, subject, date = self._git("log", "-1", "--format=%h%x1f%s%x1f%cI", ref).split("\x1f")
        return {"sha": sha, "subject": subject, "date": date}

    def refresh(self, fetch):
        info = {"git": True, "error": "", "checked": self.info.get("checked")}
        try:
            if fetch:
                self._git("fetch", "--quiet", "origin", timeout=60)
                info["checked"] = time.time()
            info["branch"] = self._git("rev-parse", "--abbrev-ref", "HEAD")
            info["current"] = self._commit("HEAD")
            try:
                info["latest"] = self._commit("@{u}")
                log = self._git("log", "--format=%h%x1f%s%x1f%cI%x1f%an", "HEAD..@{u}")
                info["commits"] = [dict(zip(("sha", "subject", "date", "author"), line.split("\x1f")))
                                   for line in log.splitlines() if line]
                info["behind"] = len(info["commits"])
                info["ahead"] = int(self._git("rev-list", "--count", "@{u}..HEAD"))
            except RuntimeError:
                info["error"] = "this branch has no counterpart on GitHub"
            info["dirty"] = [line[3:] for line in
                             self._git("status", "--porcelain", "--untracked-files=no").splitlines()]
            info["can_rollback"] = os.path.isfile(os.path.join(APP_DIR, ".git", "pcnc_previous"))
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            info["error"] = str(e)
        info["auto_restart"] = bool(os.environ.get("INVOCATION_ID"))  # set when systemd runs us
        if info["error"]:
            log_update.warning("update check: %s", info["error"])
        elif fetch:
            log_update.info("checked GitHub: %s (installed %s)", "%d update(s) available" % info.get("behind", 0)
                            if info.get("behind") else "up to date", info["current"]["sha"])
        with self.lock:
            self.info = info
        return dict(info, ok=not info["error"])

    def status(self):
        with self.lock:
            return dict(self.info, busy=self.busy)

    def summary(self):
        with self.lock:
            return {"behind": self.info.get("behind", 0), "current": (self.info.get("current") or {}).get("sha")}

    def check(self):
        return self.refresh(fetch=True)

    def apply(self, action):
        """Run update.sh (update | rollback). Returns {ok, output, restarting}."""
        with self.lock:
            if self.busy:
                return {"ok": False, "message": "an update is already running"}
            self.busy = True
        try:
            before = self._git("rev-parse", "HEAD")
            r = subprocess.run(["sh", UPDATE_SCRIPT, action], cwd=APP_DIR, capture_output=True, text=True, timeout=180)
            output = (r.stdout + r.stderr).strip()
            changed = self._git("rev-parse", "HEAD") != before
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            log_update.exception("%s failed", action)
            return {"ok": False, "message": str(e), "output": str(e)}
        finally:
            with self.lock:
                self.busy = False
        self.refresh(fetch=False)
        restarting = r.returncode == 0 and changed and bool(os.environ.get("INVOCATION_ID"))
        if r.returncode == 0 and changed and not restarting:
            output += "\nRestart server.py to run the new version."
        (log_update.info if r.returncode == 0 else log_update.error)(
            "%s (exit code %d): %s", action, r.returncode, output.replace("\n", " | "))
        return {"ok": r.returncode == 0, "message": output.splitlines()[-1] if output else "",
                "output": output, "restarting": restarting}

    def _auto_check(self):
        time.sleep(20)
        while True:
            self.refresh(fetch=True)
            time.sleep(UPDATE_CHECK_EVERY)


# ---------------------------------------------------------------------------
# Settings changed from the dashboard, kept in settings.json next to this file
# ---------------------------------------------------------------------------

SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
DEFAULT_NAME = "CNC Monitor"
MAX_NAME_LENGTH = 40
# camera / files_dir None: what --camera / --files-dir say
# Which CAM program wrote the G-code, for reading the tool sizes from its comments (see gcode.js).
TOOL_CAMS = {"fusion": "Fusion 360"}

SETTINGS = {"name": DEFAULT_NAME, "camera": None, "log_debug": False, "files_dir": None,
            "tool_shape": True, "tool_cam": "fusion"}
SETTINGS_LOCK = threading.Lock()


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        LOG.warning("cannot read %s: %s - using default settings", SETTINGS_FILE, e)
        return
    if isinstance(saved, dict):
        SETTINGS.update({k: v for k, v in saved.items() if k in SETTINGS})


def save_settings(changes):
    with SETTINGS_LOCK:
        SETTINGS.update(changes)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)


def apply_files_dir():
    """Use the folder chosen in Settings, or --files-dir if none is chosen or it is gone."""
    choice = SETTINGS["files_dir"]
    if choice and os.path.isdir(choice):
        CONFIG.files_dir = os.path.realpath(choice)
    else:
        if choice:
            log_files.warning("folder %s (from Settings) not found, using %s", choice, CONFIG.files_default or "none")
        CONFIG.files_dir = CONFIG.files_default
    if CONFIG.files_dir:
        log_files.info("folder %s", CONFIG.files_dir)


HUB = Hub()
CAMERA = None
SYSTEM = None
UPDATER = None
REJECTED_BRIDGES = set()  # addresses already reported for a wrong token
CLIENT_LOG_RATE = {}      # browser address -> (minute started, lines received), for CLIENT_LOG_LIMIT
CLIENT_LOG_LOCK = threading.Lock()
CONFIG = argparse.Namespace(token="", files_dir="", files_default="", line_offset=0, camera_note="")


class Handler(BaseHTTPRequestHandler):
    server_version = "PlanetCNCWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_request(self, code="-", size="-"):
        if self.command == "POST" and self.path == "/api/state":
            return  # the bridge posts 5 times a second
        log_http.debug("%s %s -> %s", self.client_address[0], self.requestline, code)

    def log_error(self, fmt, *args):
        log_http.warning("%s: %s", self.client_address[0], fmt % args)

    def parse_request(self):
        self._started = time.time()
        return super().parse_request()

    def handle_one_request(self):
        self._started = None
        super().handle_one_request()
        if self._started:
            took = time.time() - self._started
            path = urlparse(getattr(self, "path", "")).path
            if took > SLOW_REQUEST and path not in ("/api/events", "/api/camera.mjpg", "/api/tng/call",
                                                     "/api/update/check", "/api/update/apply", "/api/update/rollback"):
                log_http.warning("slow request: %s %s from %s took %.1f s", self.command, path,
                                 self.client_address[0], took)

    # ---------- helpers ----------

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _bridge_authorized(self):
        if not CONFIG.token:
            # The bridge receives machine commands, so without a token only TNG on this machine may act as one.
            return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
        return hmac.compare_digest(self.headers.get("X-Bridge-Token", ""), CONFIG.token)

    # ---------- routes ----------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(200, HUB.snapshot()[1])
        elif path == "/api/program":
            with HUB.cond:
                prog = dict(HUB.program)
            self._json(200, prog)
        elif path == "/api/events":
            self._sse()
        elif path == "/api/camera.mjpg":
            self._camera_stream()
        elif path == "/api/tng/call":
            self._tng_read(self._query("f"), self._query("args"))
        elif path == "/api/tng/commands":
            self._tng_read("cmd_list", "[]")
        elif path == "/api/update":
            self._json(200, UPDATER.status() if UPDATER else {"git": False, "message": UPDATES_OFF_REASON})
        elif path == "/api/settings":
            self._json(200, self._settings_view())
        elif path == "/api/console":
            try:
                after = int(self._query("after") or 0)
            except ValueError:
                after = 0
            self._json(200, {"lines": HUB.console_since(after)})
        elif path == "/api/files":
            self._files_list()
        elif path == "/api/files/download":
            self._files_download()
        elif path == "/api/logs":
            self._logs_list()
        elif path == "/api/logs/tail":
            self._logs_tail()
        elif path == "/api/logs/download":
            self._logs_download()
        else:
            self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/api/update/check", "/api/update/apply", "/api/update/rollback"):
            self._update({"check": "check", "apply": "update", "rollback": "rollback"}[path.rsplit("/", 1)[1]])
            return
        if path in ("/api/files/upload", "/api/files/delete"):
            if self._require_writer():
                (self._files_upload if path.endswith("upload") else self._files_delete)()
            return
        if path == "/api/settings":
            if self._require_writer():
                self._settings_change()
            return
        if path == "/api/log":
            if self._require_writer():
                self._client_log()
            return
        if path == "/api/logs/level":
            if self._require_writer():
                self._logs_level()
            return
        if not self._bridge_authorized():
            client = self.client_address[0]
            if client not in REJECTED_BRIDGES:
                REJECTED_BRIDGES.add(client)
                log_bridge.warning("rejected a bridge from %s: %s", client, (
                    "wrong token (TOKEN in the bridge must match --token)" if CONFIG.token else
                    "without --token only a bridge on this machine is accepted"))
            self.close_connection = True  # the request body was not read
            self._json(403, {"error": "bad token"})
            return
        try:
            data = json.loads(self._read_body() or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid json"})
            return
        if path == "/api/state":
            self._bridge_log(data.pop("log", None) or [])
            HUB.add_results(data.pop("results", None) or [])
            reply = {"ok": True}
            if "values" in data:
                # Generic bridge: raw values in, dashboard state out. Send the spec when the
                # bridge doesn't have the current one (first update, or after a server update).
                state = derive_state(data["values"])
                state["bridge_protocol"] = data.get("protocol")
                if data.get("spec_id") != BRIDGE_SPEC["id"]:
                    reply["spec"] = BRIDGE_SPEC
                    log_bridge.info("sending the reading spec %s to the bridge (it had %s)",
                                    BRIDGE_SPEC["id"], data.get("spec_id"))
            else:
                state = data  # bridge from before protocol 2: sends the finished state itself
                state["bridge_protocol"] = 1
            HUB.set_state(state, source=self.client_address[0])
            with HUB.cond:
                reply["program_id"] = HUB.program["id"]
            # The program id lets the bridge resend the program after a server restart.
            reply["commands"] = HUB.take_commands()
            self._json(200, reply)
        elif path == "/api/program":
            name, text = str(data.get("name", "")), str(data.get("text", ""))
            pid = HUB.set_program(name, text)
            log_bridge.info("program received: %s (%d lines, %.0f KB, id %s)", name or "(none)",
                            text.count("\n") + (1 if text and not text.endswith("\n") else 0), len(text) / 1024, pid)
            self._json(200, {"ok": True, "program_id": pid})
        else:
            self._json(404, {"error": "not found"})

    def _require_writer(self):
        """Check for anything that changes the machine or the Pi (control, uploads, settings)."""
        self.close_connection = True  # error replies may leave the request body unread
        # A custom header can't be sent by another website without CORS approval, so a
        # page elsewhere can't make the visitor's browser act on the machine.
        if self.headers.get("X-CNC-Control") != "1":
            log_http.warning("refused %s %s from %s: missing X-CNC-Control header", self.command, self.path,
                             self.client_address[0])
            self._json(403, {"ok": False, "message": "missing X-CNC-Control header"})
            return False
        return True

    # ---------- log: from the bridge, from browsers, for the Settings tab ----------

    def _bridge_log(self, entries):
        """Log lines the bridge collected in TNG (sent with its next update, so also those from
        while it couldn't reach the server)."""
        for e in entries[:200]:
            if not isinstance(e, dict):
                continue
            level = {"debug": logging.DEBUG, "warn": logging.WARNING, "error": logging.ERROR}.get(e.get("level"),
                                                                                                  logging.INFO)
            late = time.time() - _num(e.get("t"), time.time())
            log_bridge.log(level, "[TNG] %s%s", str(e.get("msg", ""))[:4000],
                           " (at %s)" % time.strftime("%H:%M:%S", time.localtime(e["t"])) if late > 3 else "")

    def _client_log(self):
        """Errors and events from a dashboard in a browser."""
        try:
            data = json.loads(self._read_body() or b"{}")
        except ValueError:
            self._json(400, {"ok": False})
            return
        ip = self.client_address[0]
        now = time.time()
        with CLIENT_LOG_LOCK:
            window_start, count = CLIENT_LOG_RATE.get(ip, (now, 0))
            if now - window_start > 60:
                window_start, count = now, 0
            entries = data.get("entries") if isinstance(data.get("entries"), list) else []
            allowed = max(0, CLIENT_LOG_LIMIT - count)
            CLIENT_LOG_RATE[ip] = (window_start, count + len(entries))
        who = "%s %s" % (ip, short_agent(self.headers.get("User-Agent")))
        for e in entries[:allowed]:
            if isinstance(e, dict):
                level = {"debug": logging.DEBUG, "warn": logging.WARNING,
                         "error": logging.ERROR}.get(e.get("level"), logging.INFO)
                late = now - _num(e.get("t"), now)  # sent later, e.g. after the connection came back
                log_client.log(level, "[%s] %s%s", who, str(e.get("msg", ""))[:4000],
                               " (page time %s)" % time.strftime("%H:%M:%S", time.localtime(e["t"])) if late > 5 else "")
        if len(entries) > allowed:
            log_client.warning("[%s] %d log lines dropped (more than %d per minute)", who,
                               len(entries) - allowed, CLIENT_LOG_LIMIT)
        if data.get("dropped"):
            log_client.warning("[%s] the page dropped %d log lines it could not send", who, data["dropped"])
        self._json(200, {"ok": True})

    def _logs_list(self):
        files = LOGS.files()
        self._json(200, {"ok": True, "dir": LOGS.dir, "files": files, "total": sum(f["size"] for f in files),
                         "max": LOGS.max_bytes, "keep_days": LOGS.keep_days, "debug": LOGS.debug_enabled()})

    def _logs_tail(self):
        """The end of the current log, optionally only warnings and errors."""
        try:
            lines = max(10, min(2000, int(self._query("lines") or 300)))
        except ValueError:
            lines = 300
        full = LOGS.path(LOG_FILE)
        if not full:
            self._send(200, "No log file (the server was started with --log-dir none).", "text/plain; charset=utf-8")
            return
        with open(full, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 2 * 2**20))  # the last 2 MB is plenty
            text = f.read().decode("utf-8", "replace").splitlines()
        if self._query("level") == "warn":
            text = [t for t in text if " WARNING " in t or " ERROR " in t or " CRITICAL " in t]
        self._send(200, "\n".join(text[-lines:]), "text/plain; charset=utf-8")

    def _logs_download(self):
        full = LOGS.path(self._query("name"))
        if not full:
            self._send(404, "Not found", "text/plain")
            return
        with open(full, "rb") as f:
            self._send(200, f.read(), "text/plain; charset=utf-8",
                       {"Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(os.path.basename(full))})

    def _logs_level(self):
        try:
            debug = bool(json.loads(self._read_body() or b"{}").get("debug"))
        except ValueError:
            self._json(400, {"ok": False, "message": "invalid json"})
            return
        LOGS.set_debug(debug)
        try:
            save_settings({"log_debug": debug})
        except OSError as e:
            LOG.warning("could not save the log level: %s", e)
        LOG.info("detailed (debug) logging %s from %s", "on" if debug else "off", self.client_address[0])
        with HUB.cond:
            HUB.state_seq += 1  # tell the dashboards, so they send their debug lines too
            HUB.cond.notify_all()
        self._json(200, {"ok": True, "debug": debug})

    # ---------- read anything from TNG (through the bridge) ----------

    def _tng_read(self, fn_name, args_json):
        """GET /api/tng/call?f=param_get&args=["_hw_version"] - read-only planetcnc functions only."""
        if not (fn_name in TNG_READ_FUNCS or fn_name.startswith(TNG_READ_PREFIXES)):
            self._json(403, {"ok": False, "message": "only reading functions are allowed here"})
            return
        try:
            args = json.loads(args_json or "[]")
            if not isinstance(args, list):
                raise ValueError
        except ValueError:
            self._json(400, {"ok": False, "message": "args must be a JSON list"})
            return
        if not HUB.snapshot()[1]["bridge_online"]:
            self._json(409, {"ok": False, "message": "TNG bridge is offline"})
            return
        if (HUB.state.get("bridge_protocol") or 1) < 2:
            self._json(409, {"ok": False, "message": "the running bridge is too old - update it"})
            return
        result = HUB.wait_result(HUB.queue_command({"op": "call", "f": fn_name, "args": args}))
        self._json(200 if result["ok"] else 409, result)

    # ---------- software update ----------

    def _update(self, action):
        if not self._require_writer():
            return
        if not UPDATER:
            self._json(409, {"ok": False, "message": UPDATES_OFF_REASON})
            return
        if action == "check":
            self._json(200, UPDATER.check())
            return
        if HUB.snapshot()[1].get("running"):
            self._json(409, {"ok": False, "message": "A program is running - update afterwards"})
            return
        HUB.log("cmd", "software %s" % action)
        result = UPDATER.apply(action)
        HUB.log("ok" if result["ok"] else "err", (result.get("output") or result.get("message", "")).strip()[-300:])
        self._json(200 if result["ok"] else 409, result)
        if result.get("restarting"):
            # systemd (Restart=always) starts the new version a few seconds after we exit.
            LOG.info("restarting to run the new version")
            threading.Timer(1.0, lambda: (logging.shutdown(), os._exit(0))).start()

    # ---------- settings ----------

    def _settings_view(self):
        return {"ok": True, "name": SETTINGS["name"],
                "camera": CAMERA.choice if CAMERA else "none", "camera_cmd": bool(CAMERA and CAMERA.command),
                "camera_note": CONFIG.camera_note, "cameras": list_cameras() if CAMERA else [],
                "files_dir": CONFIG.files_dir, "files_dir_set": bool(SETTINGS["files_dir"]),
                "files_default": CONFIG.files_default,
                "tool_shape": bool(SETTINGS["tool_shape"]), "tool_cam": SETTINGS["tool_cam"],
                "tool_cams": [{"id": k, "label": v} for k, v in TOOL_CAMS.items()]}

    def _settings_change(self):
        try:
            data = json.loads(self._read_body() or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "message": "invalid json"})
            return
        changes = {}
        if "name" in data:
            name = " ".join(str(data["name"]).split())[:MAX_NAME_LENGTH]
            changes["name"] = name or DEFAULT_NAME
        if "camera" in data:
            choice = str(data["camera"])
            if not CAMERA or CAMERA.command:
                self._json(409, {"ok": False, "message": CONFIG.camera_note or "camera disabled"})
                return
            if choice not in ("auto", "none") and choice not in [c["path"] for c in list_cameras()]:
                self._json(400, {"ok": False, "message": "no camera %s" % choice})
                return
            changes["camera"] = choice
        if "files_dir" in data:
            folder = str(data["files_dir"] or "").strip()
            if folder:
                folder = os.path.realpath(os.path.expanduser(folder))
                if not os.path.isdir(folder):
                    self._json(400, {"ok": False, "message": "folder not found: %s" % folder})
                    return
            changes["files_dir"] = folder or None  # empty: back to --files-dir
        if "tool_shape" in data:
            changes["tool_shape"] = bool(data["tool_shape"])
        if "tool_cam" in data:
            if data["tool_cam"] not in TOOL_CAMS:
                self._json(400, {"ok": False, "message": "unknown CAM program %s" % data["tool_cam"]})
                return
            changes["tool_cam"] = data["tool_cam"]
        try:
            save_settings(changes)
        except OSError as e:
            self._json(500, {"ok": False, "message": "could not save settings: %s" % e})
            return
        LOG.info("settings changed from %s: %s", self.client_address[0], changes)
        if "camera" in changes and changes["camera"] != CAMERA.choice:
            CAMERA.set_choice(changes["camera"])
            HUB.log("info", "Camera: %s" % changes["camera"])
        if "name" in changes:
            HUB.log("info", "Machine name: %s" % changes["name"])
        if "tool_shape" in changes or "tool_cam" in changes:
            HUB.log("info", "Tool in 3D view: %s" % (
                "from %s tool comments" % TOOL_CAMS[SETTINGS["tool_cam"]] if SETTINGS["tool_shape"] else "standard"))
        if "files_dir" in changes:
            apply_files_dir()
            HUB.log("info", "Files folder: %s" % (CONFIG.files_dir or "none"))
        with HUB.cond:
            HUB.state_seq += 1  # push the change to every open dashboard
            HUB.cond.notify_all()
        self._json(200, dict(self._settings_view(), message="Saved"))

    # ---------- file folder (--files-dir) ----------

    def _query(self, name):
        return parse_qs(urlparse(self.path).query).get(name, [""])[0]

    def _files_path(self, rel):
        """Absolute path for `rel` inside the files folder, or None if it would leave it."""
        if not CONFIG.files_dir:
            return None
        root = os.path.realpath(CONFIG.files_dir)
        full = os.path.realpath(os.path.join(root, rel.strip("/\\")))
        if full != root and not full.startswith(root + os.sep):
            return None  # "..", absolute paths or links pointing outside
        return full

    def _files_list(self):
        rel = self._query("path")
        full = self._files_path(rel)
        if not full or not os.path.isdir(full):
            self._json(404, {"ok": False, "message": "folder not found"})
            return
        entries = []
        with os.scandir(full) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    st = e.stat()
                    entries.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size, "mtime": st.st_mtime})
                except OSError:
                    pass
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        root = os.path.realpath(CONFIG.files_dir)
        rel = "" if full == root else os.path.relpath(full, root).replace(os.sep, "/")
        self._json(200, {"ok": True, "root": os.path.basename(root) or root, "path": rel, "abs": full,
                         "entries": entries})

    def _files_download(self):
        full = self._files_path(self._query("path"))
        if not full or not os.path.isfile(full):
            self._send(404, "Not found", "text/plain")
            return
        name = os.path.basename(full)
        disposition = "inline" if self._query("inline") else "attachment"
        with open(full, "rb") as f:
            self._send(200, f.read(), "application/octet-stream",
                       {"Content-Disposition": "%s; filename*=UTF-8''%s" % (disposition, quote(name))})

    def _files_upload(self):
        folder = self._files_path(self._query("path"))
        name = os.path.basename(self._query("name").replace("\\", "/")).strip()
        if not folder or not os.path.isdir(folder):
            self._json(404, {"ok": False, "message": "folder not found"})
            return
        if not name or name.startswith(".") or any(c in name for c in '<>:"|?*\0'):
            self._json(400, {"ok": False, "message": "invalid file name"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD_BYTES:
            self._json(413, {"ok": False, "message": "file larger than %d MB" % (MAX_UPLOAD_BYTES // 2**20)})
            return
        target = os.path.join(folder, name)
        if os.path.exists(target) and self._query("overwrite") != "1":
            self._json(409, {"ok": False, "exists": True, "message": "%s already exists" % name})
            return
        # Write to a temporary file first so a broken upload never leaves half a file behind.
        tmp = os.path.join(folder, ".upload-%d-%s" % (os.getpid(), name))
        try:
            with open(tmp, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise OSError("upload interrupted")
                    f.write(chunk)
                    left -= len(chunk)
            os.replace(tmp, target)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            log_files.error("upload of %s from %s failed: %s", target, self.client_address[0], e)
            self._json(500, {"ok": False, "message": "could not save: %s" % e})
            return
        log_files.info("uploaded %s (%d bytes, from %s)", target, length, self.client_address[0])
        self._json(200, {"ok": True, "message": "uploaded %s" % name})

    def _files_delete(self):
        try:
            rel = str(json.loads(self._read_body() or b"{}").get("path", ""))
        except ValueError:
            rel = ""
        full = self._files_path(rel)
        if not full or full == os.path.realpath(CONFIG.files_dir) or not os.path.isfile(full):
            self._json(404, {"ok": False, "message": "file not found (only files can be deleted)"})
            return
        try:
            os.remove(full)
        except OSError as e:
            log_files.error("deleting %s failed: %s", full, e)
            self._json(500, {"ok": False, "message": "could not delete: %s" % e})
            return
        log_files.info("deleted %s (from %s)", full, self.client_address[0])
        self._json(200, {"ok": True, "message": "deleted %s" % os.path.basename(full)})

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self._send(404, "Not found", "text/plain")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "text/javascript"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        # Without a timeout, a browser that vanished without closing the connection (Wi-Fi dropped,
        # laptop asleep) would keep this thread blocked forever.
        self.connection.settimeout(STREAM_WRITE_TIMEOUT)
        who = "%s %s" % (self.client_address[0], short_agent(self.headers.get("User-Agent")))
        log_http.info("dashboard connected: %s (%d open)", who, STREAMS.change("dashboard", 1))
        started, sent, reason = time.time(), 0, "?"
        last_seq = -1
        try:
            while True:
                with HUB.cond:
                    # Wake on change, or every second so 'bridge offline' gets noticed.
                    HUB.cond.wait_for(lambda: HUB.state_seq != last_seq, timeout=1.0)
                seq, snap = HUB.snapshot()
                last_seq = seq
                self.wfile.write(("data: " + json.dumps(snap) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                sent += 1
        except socket.timeout:
            reason = "browser stopped reading for %.0f s" % STREAM_WRITE_TIMEOUT
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            reason = "closed by the browser" if isinstance(e, (BrokenPipeError, ConnectionResetError,
                                                               ConnectionAbortedError)) else str(e)
        finally:
            log_http.info("dashboard disconnected: %s after %s, %d updates sent (%s; %d still open)", who,
                          fmt_duration(time.time() - started), sent, reason, STREAMS.change("dashboard", -1))

    def _camera_stream(self):
        if CAMERA is None:
            self._send(404, "Camera disabled", "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        self.connection.settimeout(STREAM_WRITE_TIMEOUT)
        who = "%s %s" % (self.client_address[0], short_agent(self.headers.get("User-Agent")))
        log_camera.info("viewer connected: %s (%d open)", who, STREAMS.change("camera", 1))
        started, sent, reason = time.time(), 0, "?"
        CAMERA.add_client()
        try:
            seq = -1
            while True:
                jpeg, seq = CAMERA.wait_frame(seq, CAMERA_KEEPALIVE)
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                 % len(jpeg) + jpeg + b"\r\n")
                sent += 1
        except socket.timeout:
            reason = "browser stopped reading for %.0f s" % STREAM_WRITE_TIMEOUT
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            reason = "closed by the browser" if isinstance(e, (BrokenPipeError, ConnectionResetError,
                                                               ConnectionAbortedError)) else str(e)
        finally:
            CAMERA.remove_client()
            log_camera.info("viewer disconnected: %s after %s, %d frames sent (%s; %d still open)", who,
                            fmt_duration(time.time() - started), sent, reason, STREAMS.change("camera", -1))


# ---------------------------------------------------------------------------
# Demo mode: a simulated machine running a generated program
# ---------------------------------------------------------------------------

def demo_program():
    lines = ["(PlanetCNC web monitor - demo program)", "G21 G90 G17", "G54", "T1 M6", "S12000 M3",
             "G0 Z5", "G0 X0 Y0"]
    # Facing / pocket raster in two depth passes
    for depth in (-1.0, -2.0):
        lines.append("G0 X10 Y10")
        lines.append("G1 Z%.3f F300" % depth)
        y = 10.0
        forward = True
        while y <= 60.0:
            lines.append("G1 X%.3f Y%.3f F1500" % (90.0 if forward else 10.0, y))
            y += 4.0
            if y <= 60.0:
                lines.append("G1 Y%.3f" % y)
            forward = not forward
        lines.append("G0 Z5")
    # Circular boss contour with arcs
    lines += ["G0 X70 Y80", "G1 Z-3 F300", "G2 X70 Y80 I-20 J0 F1200", "G0 Z5"]
    # Spiral of small holes
    for i in range(12):
        a = i * math.pi / 6
        x, y = 50 + 25 * math.cos(a), 80 + 25 * math.sin(a) * 0.4
        lines += ["G0 X%.3f Y%.3f" % (x, y), "G1 Z-4 F200", "G0 Z5"]
    # Rounded rectangle outline (G3 corners)
    lines += ["G0 X5 Y0", "G1 Z-2 F300", "G1 X95 F1500", "G3 X100 Y5 I0 J5", "G1 Y105",
              "G3 X95 Y110 I-5 J0", "G1 X5", "G3 X0 Y105 I0 J-5", "G1 Y5", "G3 X5 Y0 I5 J0",
              "G0 Z25", "M5", "G0 X0 Y0", "M30"]
    return "\n".join(lines) + "\n"


def demo_moves(text):
    """Very small interpreter for the demo program -> list of (line_no, x, y, z, rapid)."""
    pos = [0.0, 0.0, 25.0]
    pts = []
    motion = 0
    for n, raw in enumerate(text.splitlines(), start=1):
        code = raw.split("(")[0].upper()
        words = {}
        tok = ""
        for ch in code + " ":
            if ch.isalpha():
                if tok:
                    words.setdefault(tok[0], []).append(float(tok[1:]))
                tok = ch
            elif ch in "0123456789.-+" and tok:
                tok += ch
        if tok and len(tok) > 1:
            words.setdefault(tok[0], []).append(float(tok[1:]))
        for g in words.get("G", []):
            if g in (0, 1, 2, 3):
                motion = int(g)
        if not any(k in words for k in "XYZ"):
            continue
        tgt = [words.get(a, [p])[0] for a, p in zip("XYZ", pos)]
        if motion in (2, 3):
            cx, cy = pos[0] + words.get("I", [0])[0], pos[1] + words.get("J", [0])[0]
            a0 = math.atan2(pos[1] - cy, pos[0] - cx)
            a1 = math.atan2(tgt[1] - cy, tgt[0] - cx)
            r = math.hypot(pos[0] - cx, pos[1] - cy)
            sweep = a1 - a0
            if motion == 2:
                sweep = sweep if sweep < 0 else sweep - 2 * math.pi
            else:
                sweep = sweep if sweep > 0 else sweep + 2 * math.pi
            steps = max(8, int(abs(sweep) * r / 1.0))
            for i in range(1, steps + 1):
                a = a0 + sweep * i / steps
                pts.append((n, cx + r * math.cos(a), cy + r * math.sin(a),
                            pos[2] + (tgt[2] - pos[2]) * i / steps, False))
        else:
            pts.append((n, tgt[0], tgt[1], tgt[2], motion == 0))
        pos = tgt
    return pts




def demo_commands():
    """Answer the server's read requests (/api/tng/call) in demo mode."""
    while True:
        time.sleep(0.2)
        HUB.add_results([{"id": c["id"], "ok": True, "message": "demo: %s accepted" % c.get("action", c.get("op"))}
                         for c in HUB.take_commands()])


def run_demo():
    text = demo_program()
    HUB.set_program("C:/CNC/demo_part.nc", text)
    moves = demo_moves(text)
    line_count = len(text.splitlines())
    while True:
        pos = [0.0, 0.0, 25.0]
        start = time.time()
        for line_no, x, y, z, rapid in moves:
            feed = 5000.0 if rapid else 1500.0
            dist = math.dist(pos, (x, y, z))
            steps = max(1, int(dist / (feed / 60.0) / 0.1))
            for i in range(1, steps + 1):
                p = [pos[k] + ((x, y, z)[k] - pos[k]) * i / steps for k in range(3)]
                HUB.set_state({
                    "work": p + [0.0] * 6,
                    "machine": [p[0] + 120.0, p[1] + 80.0, p[2] - 40.0] + [0.0] * 6,
                    "line": line_no, "line_count": line_count,
                    "feed": feed, "spindle": 12000.0, "tool": 1, "units": "mm",
                    "coordsys": "G54", "running": True, "paused": False,
                    "estop": False, "idle": False, "file": "C:/CNC/demo_part.nc",
                    "elapsed": time.time() - start, "time_to_end": None,
                })
                time.sleep(0.1)
            pos = [x, y, z]
        for _ in range(30):  # idle a few seconds between runs
            HUB.set_state({"work": pos + [0.0] * 6,
                           "machine": [pos[0] + 120.0, pos[1] + 80.0, pos[2] - 40.0] + [0.0] * 6,
                           "line": line_count, "line_count": line_count, "feed": 0.0,
                           "spindle": 0.0, "tool": 1, "units": "mm", "coordsys": "G54",
                           "running": False, "paused": False, "estop": False, "idle": True,
                           "file": "C:/CNC/demo_part.nc"})
            time.sleep(0.1)


def watch_bridge():
    """Note in the console when the bridge stops sending (connecting is logged in set_state)."""
    online = False
    while True:
        time.sleep(1.0)
        with HUB.cond:
            now_online = HUB.last_state_time > 0 and time.time() - HUB.last_state_time < BRIDGE_TIMEOUT
        if online and not now_online:
            with HUB.cond:
                HUB.offline_since = HUB.last_state_time
            HUB.log("warn", "TNG bridge lost - no updates for %d s" % BRIDGE_TIMEOUT)
        online = now_online


def fmt_duration(sec):
    sec = int(sec)
    return "%d:%02d:%02d" % (sec // 3600, sec // 60 % 60, sec % 60) if sec >= 3600 else "%d:%02d" % (sec // 60, sec % 60)


def heartbeat():
    """A status line every few minutes: where the job stood and how the server was doing,
    for reconstructing what happened before a problem."""
    last = (time.time(), HUB.bridge_updates)
    while True:
        time.sleep(HEARTBEAT_EVERY)
        now, updates = time.time(), HUB.bridge_updates
        rate = (updates - last[1]) / (now - last[0])
        last = (now, updates)
        seq, s = HUB.snapshot()
        parts = ["bridge %s (%.1f updates/s)" % ("online" if s["bridge_online"] else "OFFLINE", rate)]
        if s.get("running"):
            total = s.get("line_count") or 0
            parts.append("job line %s/%s (%.0f %%)%s" % (
                s.get("line"), total or "?", 100.0 * s.get("line", 0) / total if total else 0,
                ", %s left" % fmt_duration(s["time_to_end"]) if (s.get("time_to_end") or -1) >= 0 else ""))
        else:
            parts.append("no job running")
        parts.append("%d dashboard(s), %d camera viewer(s)" % (STREAMS.open["dashboard"], STREAMS.open["camera"]))
        parts.append("%d threads" % threading.active_count())
        rss = _read_text("/proc/self/statm").split()
        if len(rss) > 1:
            parts.append("server memory %.0f MB" % (int(rss[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20))
        sysinfo = s.get("system") or {}
        if sysinfo.get("cpu") is not None:
            parts.append("CPU %.0f %%" % sysinfo["cpu"])
        if sysinfo.get("temp") is not None:
            parts.append("%.0f \u00b0C" % sysinfo["temp"])
        log_system.info("status: " + ", ".join(parts))


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """An exception in a request handler: log it with its traceback (default: stderr only)."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout)):
            log_http.debug("connection from %s ended: %s", client_address[0], exc)
        else:
            log_http.error("error while answering %s", client_address[0], exc_info=True)


def lan_address():
    """This machine's address on the local network, i.e. what other computers should open."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # UDP connect sends nothing; it just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def main():
    ap = argparse.ArgumentParser(description="PlanetCNC web monitor server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--token", default=os.environ.get("PCNC_TOKEN", ""),
                    help="shared secret the bridge must send (X-Bridge-Token)")
    ap.add_argument("--viewer-password", help=argparse.SUPPRESS)  # no longer used; old service files still start
    ap.add_argument("--demo", action="store_true", help="simulate a running machine")
    ap.add_argument("--camera", default=os.environ.get("PCNC_CAMERA", "auto"),
                    help="V4L2 camera device, 'auto' (first camera found) or 'none'; "
                         "a camera chosen in the dashboard's settings takes precedence")
    ap.add_argument("--camera-size", default="1280x720", help="requested resolution, e.g. 640x480")
    ap.add_argument("--camera-fps", type=int, default=15, help="requested frame rate")
    ap.add_argument("--camera-cmd", default=os.environ.get("PCNC_CAMERA_CMD", ""),
                    help="custom command that writes MJPEG to stdout (overrides --camera)")
    ap.add_argument("--line-offset", type=int, default=0,
                    help="add to TNG's line number if the highlighted G-code line is off by one")
    ap.add_argument("--files-dir", default=os.environ.get("PCNC_FILES_DIR", "auto"),
                    help="folder shown in the Files tab ('auto' = ~/Desktop, else home; 'none' to disable); "
                         "a folder chosen in the dashboard's settings takes precedence")
    ap.add_argument("--log-dir", default=os.environ.get("PCNC_LOG_DIR", os.path.join(APP_DIR, "logs")),
                    help="folder for the log files (default: logs/ next to server.py; 'none' = console only)")
    ap.add_argument("--log-max-mb", type=int, default=50, help="most disk space the log files may use (MB)")
    ap.add_argument("--log-keep-days", type=int, default=30, help="delete log files older than this")
    ap.add_argument("--debug", action="store_true", help="detailed logging (also switchable in Settings)")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # show messages in journalctl right away, not when a buffer fills
    CONFIG.token = args.token
    load_settings()
    LOGS.setup(args.log_dir, args.log_max_mb, args.log_keep_days, args.debug or SETTINGS["log_debug"])
    try:
        version = subprocess.run(["git", "log", "-1", "--format=%h %s"], cwd=APP_DIR, capture_output=True,
                                 text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = ""
    LOG.info("starting: version %s, Python %s, pid %d%s", version or "unknown", sys.version.split()[0], os.getpid(),
             ", under systemd" if os.environ.get("INVOCATION_ID") else "")
    LOG.info("logs: %s (at most %d MB, kept %d days%s)", LOGS.dir or "console only", args.log_max_mb,
             args.log_keep_days, ", detailed" if LOGS.debug_enabled() else "")
    LOG.debug("options: %s", {k: ("***" if k == "token" and v else v) for k, v in vars(args).items()})
    CONFIG.line_offset = args.line_offset
    files_dir = args.files_dir
    if files_dir == "auto":
        desktop = os.path.expanduser("~/Desktop")
        files_dir = desktop if os.path.isdir(desktop) else os.path.expanduser("~")
    if files_dir != "none":
        if os.path.isdir(files_dir):
            CONFIG.files_default = os.path.realpath(files_dir)
        else:
            log_files.warning("folder %s not found, file view disabled", files_dir)
    apply_files_dir()

    global CAMERA, SYSTEM, UPDATER, UPDATES_OFF_REASON
    threading.Thread(target=watch_bridge, name="watch-bridge", daemon=True).start()
    threading.Thread(target=heartbeat, name="heartbeat", daemon=True).start()
    if SystemStats.available():
        SYSTEM = SystemStats()
    if Updater.available():
        UPDATER = Updater()
        UPDATER.refresh(fetch=False)
    else:
        UPDATES_OFF_REASON = ("git is not installed (sudo apt install git)" if not shutil.which("git") else
                              "%s is not a git clone - see README" % APP_DIR)
        log_update.warning(UPDATES_OFF_REASON)
    if args.camera_cmd:
        CAMERA = Camera(command=args.camera_cmd)
        CONFIG.camera_note = "set by --camera-cmd on the server"
        log_camera.info("custom command: %s", args.camera_cmd)
    elif shutil.which("ffmpeg"):
        choice = SETTINGS["camera"] or args.camera
        CAMERA = Camera(choice, args.camera_size, args.camera_fps)
        log_camera.info("%s (%s @ %d fps); found: %s", choice, args.camera_size, args.camera_fps,
                        ", ".join(c["label"] for c in list_cameras()) or "no camera")
    else:
        CONFIG.camera_note = "ffmpeg is not installed (sudo apt install ffmpeg)"
        log_camera.warning("ffmpeg not found, camera disabled (sudo apt install ffmpeg)")

    if args.demo:
        threading.Thread(target=run_demo, daemon=True).start()
        threading.Thread(target=demo_commands, daemon=True).start()

    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as e:
        LOG.critical("cannot listen on %s:%d: %s (is another copy already running?)", args.host, args.port, e)
        sys.exit(1)
    address = lan_address() if args.host == "0.0.0.0" else args.host
    LOG.info("PlanetCNC web monitor on http://%s:%d%s", address, args.port, "  (demo mode)" if args.demo else "")
    if sys.stdout.isatty():  # started by hand: say plainly which address to open
        print("\n  Open the dashboard%s:\n"
              "    from another device on your network:  http://%s:%d\n"
              "    on this computer:                     http://localhost:%d\n"
              "  Press Ctrl+C to stop.\n" % (" (demo)" if args.demo else "", address, args.port, args.port))
    # systemd stops the service with SIGTERM: turn it into a normal exit so it gets logged.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        LOG.info("stopping")
    except Exception:
        LOG.critical("server stopped by an error", exc_info=True)
        raise


if __name__ == "__main__":
    main()
