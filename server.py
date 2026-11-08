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
import platform
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
BRIDGE_TIMEOUT = 3.0  # seconds without an update before the bridge is reported offline
COMMAND_TTL = 2.0      # a command the bridge hasn't picked up by then is dropped, never run late
COMMAND_WAIT = 10.0    # how long a browser request waits for TNG's answer
ESTOP_PROTOCOL = 3     # first bridge version that accepts the E-stop request
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
    # Fallbacks: with tool changes that TNG doesn't track (e.g. done by hand) _current_tool stays 0
    # while _selected_tool has the program's T word; _coordsys_name can be empty while _coordsys
    # has the number (1 = G54).
    ["tool_sel", "p", "_selected_tool"], ["coordsys_num", "p", "_coordsys"],
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
log_notify = LOG.getChild("telegram")


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


def coordsys_name(n):
    """TNG's coordinate system number -> its G code: 1-6 = G54-G59, 7-9 = G59.1-G59.3."""
    n = int(n)
    if 1 <= n <= 6:
        return "G%d" % (53 + n)
    if 7 <= n <= 9:
        return "G59.%d" % (n - 6)
    return ""


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
        "tool": int(_num(v.get("tool")) or _num(v.get("tool_sel"))),
        "tool_name": str(v.get("tool_name") or ""),
        # First likely tool-table parameter that gives a real diameter; 0 = TNG didn't tell us.
        "tool_diameter": next((d for d in (_num(v.get("tool_dia_%d" % i)) for i in range(len(TNG_TOOL_DIAMETER_PARAMS)))
                               if 0 < d < 1000), 0.0),
        "coordsys": str(v.get("coordsys") or "") or coordsys_name(_num(v.get("coordsys_num"))),
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

    def queue_command(self, cmd, first=False):
        with self.cond:
            cmd = dict(cmd, id=self.next_cmd_id)
            self.next_cmd_id += 1
            self.commands.insert(0 if first else len(self.commands), (time.time(), cmd))
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
                if TUNING:
                    TUNING.job_started(new.get("file"))
            else:
                took = ""
                if self.job_started:
                    secs = int(time.time() - self.job_started)
                    took = " after %d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)
                self.log("info", "Job ended at line %s of %s%s" % (new.get("line", "?"), new.get("line_count", "?"), took))
                name = TUNING.job_ended(int(old.get("line") or 0), int(old.get("line_count") or 0)) if TUNING else None
                if name is not None:
                    self.log("ok", "%s has adjusted feeds: mark in the Feeds tab whether they worked" % name)
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
            if NOTIFY:  # also after a reconnect, so a job that ended meanwhile is noticed
                NOTIFY.on_state(self.state, state)
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
        s["tuning_seq"] = TUNING.seq if TUNING else 0
        s["file_rel"] = files_rel(s.get("file"))
        s["update"] = UPDATER.summary() if UPDATER else None
        s["os_updates"] = OS_PACKAGES.count() if OS_PACKAGES else None
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
            if NOTIFY and key in ("power", "temp"):
                NOTIFY.event("pi", text=text)
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
# System window: OS packages (apt), reboot / shut down, host details.
# Running these as root needs a sudoers rule; tools/setup-sudo.sh writes it. Without
# it the window still shows what can be upgraded, but can't install or reboot.
# ---------------------------------------------------------------------------

APT_GET = "/usr/bin/apt-get"
SYSTEMCTL = "/usr/bin/systemctl"
# Exactly these command lines are allowed by tools/setup-sudo.sh (sudo compares the arguments).
SUDO_APT_UPDATE = [APT_GET, "update"]
SUDO_APT_UPGRADE = [APT_GET, "-y", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
                    "upgrade"]
SUDO_POWER = {"reboot": [SYSTEMCTL, "reboot"], "poweroff": [SYSTEMCTL, "poweroff"]}
OS_CHECK_EVERY = 6 * 3600
OS_UPGRADE_TIMEOUT = 3600
APT_LISTS = "/var/lib/apt/lists"
REBOOT_REQUIRED = "/var/run/reboot-required"
C_LOCALE = dict(os.environ, LANG="C", LC_ALL="C")


def sudo_allowed(cmd):
    """True if this user may run exactly `cmd` through sudo without a password."""
    if os.geteuid() == 0:
        return True
    try:
        return subprocess.run(["sudo", "-n", "-l", *cmd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def as_root(cmd):
    return cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd]


class OsPackages:
    """Raspberry Pi OS package updates through apt: what can be upgraded, and upgrading it."""

    INST = re.compile(r"^Inst (\S+) \[([^\]]+)\] \((\S+)")

    def __init__(self):
        self.lock = threading.Lock()
        self.info = {"checked": None, "packages": [], "error": ""}
        self.job = None   # the running or last upgrade: {running, started, ended, exit, output, who}
        threading.Thread(target=self._auto_check, name="os-check", daemon=True).start()

    @staticmethod
    def available():
        return (sys.platform.startswith("linux") and os.path.exists(APT_GET)
                and (os.geteuid() == 0 or bool(shutil.which("sudo"))))

    def count(self):
        with self.lock:
            return len(self.info.get("packages") or [])

    def refresh(self, update_lists):
        """Find the upgradable packages; with update_lists, first fetch the package lists (apt-get update)."""
        info = {"error": "", "checked": self.info.get("checked"),
                "can_update": sudo_allowed(SUDO_APT_UPDATE), "can_upgrade": sudo_allowed(SUDO_APT_UPGRADE),
                "can_power": all(sudo_allowed(c) for c in SUDO_POWER.values())}
        try:
            if update_lists and info["can_update"]:
                r = subprocess.run(as_root(SUDO_APT_UPDATE), stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=300, env=C_LOCALE)
                if r.returncode:
                    info["error"] = "apt-get update failed: " + (
                        (r.stderr or r.stdout).strip().splitlines() or ["?"])[-1]
                info["checked"] = time.time()
            # A dry run of exactly what "upgrade" would do; needs no root.
            r = subprocess.run([APT_GET, "-s", "-o", "Debug::NoLocking=1", "upgrade"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=120, env=C_LOCALE)
            if r.returncode and not info["error"]:
                info["error"] = ((r.stderr or r.stdout).strip().splitlines() or ["apt-get -s upgrade failed"])[-1]
            info["packages"] = [{"name": m.group(1), "from": m.group(2), "to": m.group(3)}
                                for m in map(self.INST.match, r.stdout.splitlines()) if m]
            kept = re.search(r"have been kept back:\n((?: .*\n)+)", r.stdout)
            info["kept_back"] = kept.group(1).split() if kept else []
        except (OSError, subprocess.SubprocessError) as e:
            info["error"] = str(e)
        info["lists_date"] = self._lists_date()
        info["reboot_required"] = os.path.exists(REBOOT_REQUIRED)
        if info["error"]:
            log_update.warning("OS package check: %s", info["error"])
        elif update_lists:
            log_update.info("OS packages: %d can be upgraded%s", len(info["packages"]),
                            "" if info["can_update"] else " (package lists not refreshed: no sudo rule)")
        with self.lock:
            self.info = info
        return self.status()

    @staticmethod
    def _lists_date():
        try:
            times = [e.stat().st_mtime for e in os.scandir(APT_LISTS) if e.name.endswith("Release")]
            return max(times) if times else None
        except OSError:
            return None

    def status(self):
        with self.lock:
            out = dict(self.info)
            job = dict(self.job) if self.job else None
        if job:
            job["output"] = list(job["output"])[-400:]
            out["job"] = job
        out["busy"] = bool(job and job["running"])
        return out

    def upgrade(self, who):
        with self.lock:
            if self.job and self.job["running"]:
                return {"ok": False, "message": "an upgrade is already running"}
            if not self.info.get("can_upgrade"):
                return {"ok": False, "message": "not allowed: run tools/setup-sudo.sh on the Pi once (see README)"}
            self.job = {"running": True, "started": time.time(), "ended": None, "exit": None,
                        "output": deque(maxlen=2000), "who": who}
        HUB.log("cmd", "OS package upgrade started (%s)" % who)
        threading.Thread(target=self._run_upgrade, args=(self.job,), name="os-upgrade", daemon=True).start()
        return {"ok": True, "message": "upgrade started"}

    def _run_upgrade(self, job):
        try:
            proc = subprocess.Popen(as_root(SUDO_APT_UPGRADE), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, env=C_LOCALE, bufsize=1)
            timer = threading.Timer(OS_UPGRADE_TIMEOUT, proc.kill)
            timer.start()
            for line in proc.stdout:
                job["output"].append(line.rstrip("\n"))
            code = proc.wait()
            timer.cancel()
        except OSError as e:
            job["output"].append(str(e))
            code = -1
        with self.lock:
            job.update(running=False, ended=time.time(), exit=code)
        tail = " | ".join(list(job["output"])[-5:])
        (log_update.info if code == 0 else log_update.error)("OS upgrade (exit code %d): %s", code, tail)
        HUB.log("ok" if code == 0 else "err", "OS package upgrade %s" % (
            "finished" if code == 0 else "FAILED (exit code %d) - see the System window" % code))
        self.refresh(update_lists=False)
        if os.path.exists(REBOOT_REQUIRED):
            HUB.log("warn", "The Pi needs a reboot to finish the OS upgrade")

    def _auto_check(self):
        time.sleep(30)
        while True:
            if not HUB.snapshot()[1].get("running") and not (self.job and self.job["running"]):
                try:
                    self.refresh(update_lists=True)
                except Exception:
                    log_update.exception("OS package check failed")
            time.sleep(OS_CHECK_EVERY)


def power_action(action, who):
    """Reboot or shut down the Pi (the caller makes sure no program runs)."""
    cmd = SUDO_POWER.get(action)
    if not cmd:
        return {"ok": False, "message": "unknown action"}
    if not sudo_allowed(cmd):
        return {"ok": False, "message": "not allowed: run tools/setup-sudo.sh on the Pi once (see README)"}
    word = "Reboot" if action == "reboot" else "Shut down"
    HUB.log("warn", "%s of the Pi requested (%s)" % (word, who))

    def run():
        time.sleep(1.0)  # let the answer reach the browser first
        try:
            r = subprocess.run(as_root(cmd), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            if r.returncode:
                HUB.log("err", "%s failed: %s" % (word, (r.stderr or r.stdout).strip()))
        except (OSError, subprocess.SubprocessError) as e:
            HUB.log("err", "%s failed: %s" % (word, e))
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "message": "%s in a moment" % word}


# ---------------------------------------------------------------------------
# Telegram notifications and the job timelapse.
# The bot only reports: messages and camera photos about jobs, and answers /status and
# /photo in the configured chat. It can't control the machine. It only makes outgoing
# connections to Telegram (long polling), so nothing on the Pi is opened to the internet.
# ---------------------------------------------------------------------------

TELEGRAM_API = os.environ.get("PCNC_TELEGRAM_API", "https://api.telegram.org")  # changeable for testing
TELEGRAM_POLL = 50              # seconds a getUpdates long poll waits for messages
TELEGRAM_MAX_UPLOAD = 49 * 2**20
TELEGRAM_OLD_MESSAGE = 120      # commands older than this (s) when they arrive are ignored
SNAPSHOT_WAIT = 8.0             # how long a photo may wait for the camera to start
SNAPSHOT_WARMUP = 1.5           # a camera that was off needs a moment to set its exposure
TIMELAPSE_DIR = os.path.join(APP_DIR, "timelapses")
TIMELAPSE_KEEP = 20             # finished timelapses kept on the Pi
TIMELAPSE_MAX_FRAMES = 300      # frames kept per job: more are thinned out while recording
TIMELAPSE_SECONDS = 20          # aimed length of the finished video
TIMELAPSE_WIDTH = 960


def camera_snapshot(wait=SNAPSHOT_WAIT):
    """A fresh JPEG from the camera (started for a moment if nobody watches), or None."""
    if not CAMERA or not CAMERA.enabled():
        return None
    CAMERA.add_client()
    try:
        with CAMERA.cond:
            seq = CAMERA.frame_seq
            ready = time.time() + (0 if CAMERA.live() else SNAPSHOT_WARMUP)
            CAMERA.cond.wait_for(lambda: CAMERA.frame_seq != seq and CAMERA.frame_time >= ready, timeout=wait)
            return CAMERA.frame if CAMERA.live() else None
    finally:
        CAMERA.remove_client()


class Timelapse:
    """Takes a camera photo every few seconds during a job, and makes a short video of them at the end.

    The frames of a long job are thinned out while recording (every other one is dropped and the
    interval doubled), so a job of any length needs at most 2 x TIMELAPSE_MAX_FRAMES photos on disk.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.rec = None
        try:
            os.makedirs(TIMELAPSE_DIR, exist_ok=True)
            for e in os.scandir(TIMELAPSE_DIR):  # frames of a job the server didn't see end
                if e.is_dir() and e.name.startswith(".frames-"):
                    shutil.rmtree(e.path, ignore_errors=True)
        except OSError as e:
            log_notify.warning("timelapse folder %s: %s", TIMELAPSE_DIR, e)

    def start(self, job_file):
        if not (SETTINGS["timelapse"] and CAMERA and CAMERA.enabled()):
            return
        with self.lock:
            if self.rec:
                return
            folder = os.path.join(TIMELAPSE_DIR, ".frames-%d" % time.time())
            try:
                os.makedirs(folder)
            except OSError as e:
                log_notify.error("timelapse: cannot create %s: %s", folder, e)
                return
            self.rec = {"dir": folder, "file": job_file, "started": time.time(), "stop": threading.Event(),
                        "frames": [], "interval": float(SETTINGS["timelapse_interval"])}
            rec = self.rec
        rec["thread"] = threading.Thread(target=self._record, args=(rec,), name="timelapse", daemon=True)
        rec["thread"].start()
        log_notify.info("timelapse started: a photo every %d s", rec["interval"])

    def _record(self, rec):
        CAMERA.add_client()
        n = 0
        try:
            next_shot = time.time() + SNAPSHOT_WARMUP
            while True:
                stopped = rec["stop"].wait(max(0.0, next_shot - time.time()))
                with CAMERA.cond:
                    jpeg = CAMERA.frame if CAMERA.live() else None
                if jpeg:
                    path = os.path.join(rec["dir"], "%06d.jpg" % n)
                    n += 1
                    try:
                        with open(path, "wb") as f:
                            f.write(jpeg)
                        rec["frames"].append(path)
                    except OSError as e:
                        log_notify.error("timelapse: cannot save a photo: %s", e)
                        break
                if stopped:
                    break  # the photo just taken shows the finished job
                if len(rec["frames"]) >= 2 * TIMELAPSE_MAX_FRAMES:
                    for old in rec["frames"][1::2]:
                        try:
                            os.remove(old)
                        except OSError:
                            pass
                    rec["frames"] = rec["frames"][::2]
                    rec["interval"] *= 2
                    log_notify.info("timelapse: long job, now a photo every %d s", rec["interval"])
                next_shot += rec["interval"]
        finally:
            CAMERA.remove_client()

    def stop(self):
        """End the recording and make the video. Returns its path, or None (no camera, too short)."""
        with self.lock:
            rec, self.rec = self.rec, None
        if not rec:
            return None
        rec["stop"].set()
        rec["thread"].join(30)
        try:
            return self._encode(rec)
        finally:
            shutil.rmtree(rec["dir"], ignore_errors=True)
            self._prune()

    def _encode(self, rec):
        frames = rec["frames"]
        if len(frames) < 3:
            log_notify.info("timelapse: only %d photo(s), no video", len(frames))
            return None
        for i, path in enumerate(frames):  # ffmpeg wants numbers without gaps
            os.replace(path, os.path.join(rec["dir"], "f%06d.jpg" % i))
        fps = max(4, min(30, math.ceil(len(frames) / TIMELAPSE_SECONDS)))
        job = os.path.splitext(os.path.basename(str(rec["file"]).replace("\\", "/")))[0] or "job"
        job = re.sub(r"[^\w.-]+", "_", job)[:60]
        base = os.path.join(TIMELAPSE_DIR, time.strftime("%Y-%m-%d_%H%M", time.localtime(rec["started"])) + "_" + job)
        nice = ["nice", "-n", "10"] if shutil.which("nice") else []
        source = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-framerate", str(fps),
                  "-i", os.path.join(rec["dir"], "f%06d.jpg")]
        attempts = [
            # Standard (limited) range yuv420p: webcams give full-range JPEGs, which phones may not play.
            (base + ".mp4", ["-vf", "scale='min(%d,iw)':-2:out_range=tv,format=yuv420p" % TIMELAPSE_WIDTH,
                             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "tv", "-preset", "veryfast",
                             "-crf", "28", "-movflags", "+faststart", "-an"]),
            # ffmpeg without H.264: a GIF (bigger, but Telegram shows it the same way)
            (base + ".gif", ["-vf", "scale='min(480,iw)':-2:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse"]),
        ]
        t0 = time.time()
        for out, opts in attempts:
            try:
                r = subprocess.run(nice + source + opts + [out], stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=900)
            except (OSError, subprocess.SubprocessError) as e:
                log_notify.error("timelapse: ffmpeg failed: %s", e)
                return None
            if r.returncode == 0 and os.path.isfile(out):
                log_notify.info("timelapse: %s (%d photos, %d fps, %.0f KB) made in %.0f s", os.path.basename(out),
                                len(frames), fps, os.path.getsize(out) / 1024, time.time() - t0)
                return out
            log_notify.warning("timelapse: ffmpeg could not make %s: %s", os.path.basename(out),
                               (r.stderr or "").strip().replace("\n", " | ")[-300:])
            try:
                os.remove(out)
            except OSError:
                pass
        return None

    def _prune(self):
        for old in self.list()[TIMELAPSE_KEEP:]:
            try:
                os.remove(os.path.join(TIMELAPSE_DIR, old["name"]))
            except OSError:
                pass

    def list(self):
        """Finished timelapses, newest first."""
        try:
            files = [{"name": e.name, "size": e.stat().st_size, "mtime": e.stat().st_mtime}
                     for e in os.scandir(TIMELAPSE_DIR) if e.is_file() and e.name.endswith((".mp4", ".gif"))]
        except OSError:
            return []
        return sorted(files, key=lambda f: f["mtime"], reverse=True)

    def path(self, name):
        name = os.path.basename(str(name or ""))
        path = os.path.join(TIMELAPSE_DIR, name)
        return path if name.endswith((".mp4", ".gif")) and os.path.isfile(path) else None

    def recording(self):
        return self.rec is not None


def _html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _job_name(path):
    return os.path.basename(str(path or "").replace("\\", "/")) or "program"


class Notifier:
    """Follows the machine state, runs the timelapse, and reports to Telegram.

    on_state() is called with every state change (under the hub's lock), so it only decides
    what happened and queues it; one worker thread takes photos, makes the timelapse and
    talks to Telegram. A second thread long-polls Telegram for /status and /photo.
    """

    COMMANDS = [("status", "What the machine is doing, with a photo"), ("photo", "A camera photo"),
                ("help", "What this bot does")]

    def __init__(self):
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.bot = ""             # @username of the bot, once getMe worked
        self.error = ""
        self.chats = {}           # chats that wrote to the bot lately: id -> {"name", "t"}, for Settings
        self.offset = 0
        self.job = None           # {"file", "started", "bucket", "estop"} while a job runs
        self.poll_wake = threading.Event()
        threading.Thread(target=self._work, name="notify", daemon=True).start()
        threading.Thread(target=self._poll, name="telegram-poll", daemon=True).start()

    # ----- what happened (called with the hub's lock held: only queue) -----

    def on_state(self, old, new):
        if not old:
            return
        running, was = bool(new.get("running")), bool(old.get("running"))
        if new.get("estop") and not old.get("estop"):  # first: the most urgent message
            self.q.put(("estop", dict(new, line=old.get("line") if was else new.get("line"), running=was)))
        if running and not was:
            self.job = {"file": new.get("file"), "started": time.time(), "bucket": 0}
            self.q.put(("job_start", dict(new)))
        elif was and not running:
            job, self.job = self.job, None
            self.q.put(("job_end", dict(old, job=job, estop=bool(new.get("estop")))))
        if new.get("paused") != old.get("paused") and running and was:
            self.q.put(("paused" if new.get("paused") else "resumed", dict(new)))
        step = SETTINGS["notify_progress"]
        total = new.get("line_count") or 0
        if self.job and running and step and total:
            bucket = int(100.0 * (new.get("line") or 0) / total // step)
            if bucket > self.job["bucket"]:
                first = self.job["bucket"] == 0 and bucket > 1  # joined late: don't report what already passed
                self.job["bucket"] = bucket
                if not first and 0 < bucket * step < 100:
                    self.q.put(("progress", dict(new, pct=bucket * step)))

    def event(self, kind, **data):
        self.q.put((kind, data))

    # ----- the worker -----

    def _work(self):
        while True:
            kind, data = self.q.get()
            try:
                if kind == "job_start" and TIMELAPSE:
                    TIMELAPSE.start(data.get("file"))
                video = TIMELAPSE.stop() if kind == "job_end" and TIMELAPSE else None
                if self.configured():
                    self._report(kind, data, video)
            except Exception:
                log_notify.exception("%s notification failed", kind)

    def _report(self, kind, d, video=None):
        name = _html(_job_name(d.get("file")))
        photo = SETTINGS["notify_photos"]
        quiet = False
        if kind == "job_start":
            left = d.get("time_to_end") or -1
            text = "▶️ <b>Job started</b>: %s%s" % (name, "\nEstimated %s" % fmt_duration(left) if left > 0 else "")
        elif kind == "progress":
            left = d.get("time_to_end") or -1
            text = "⏳ <b>%d %%</b> of %s\nLine %s of %s%s" % (d["pct"], name, d.get("line"), d.get("line_count"),
                                                                    ", %s left" % fmt_duration(left) if left > 0 else "")
        elif kind == "job_end":
            job = d.get("job") or {}
            took = " in %s" % fmt_duration(time.time() - job["started"]) if job.get("started") else ""
            line, total = d.get("line") or 0, d.get("line_count") or 0
            if total and line >= total - 2:
                text = "✅ <b>Job finished</b>: %s%s" % (name, took)
            else:
                text = "⏹ <b>Job stopped%s</b>: %s at line %s of %s%s%s" % (
                    " by E-stop" if d.get("estop") else "", name, line, total or "?",
                    " (%d %%)" % (100 * line / total) if total else "", took.replace(" in", " after"))
            if video:
                self._send_file("sendAnimation", "animation", video, text)
                return
            photo = photo or not (TIMELAPSE and SETTINGS["timelapse"])
        elif kind == "estop":
            text = "\U0001f6d1 <b>E-STOP</b> on %s%s" % (_html(SETTINGS["name"]), " at line %s of %s" % (
                d.get("line"), name) if d.get("running") or d.get("line") else "")
        elif kind == "paused":
            text = "⏸ <b>Paused</b>: %s at line %s" % (name, d.get("line"))
        elif kind == "resumed":
            text, photo, quiet = "▶️ Resumed: %s" % name, False, True
        elif kind == "bridge_lost":
            text, photo = "⚠️ <b>TNG not reachable</b> during the job (no update for %d s). Check the machine." % (
                BRIDGE_TIMEOUT), False
        elif kind == "pi":
            if not SETTINGS["notify_pi"]:
                return
            text, photo = "⚠️ <b>Raspberry Pi</b>: %s" % _html(d.get("text")), False
        else:
            return
        self.send(text, photo=photo, quiet=quiet)

    # ----- Telegram -----

    def configured(self):
        return bool(SETTINGS["telegram_token"] and SETTINGS["telegram_chat"])

    def api(self, method, fields=None, file=None, timeout=30):
        """Call the Bot API. `file` = (field, filename, bytes, content type) uploads it."""
        token = SETTINGS["telegram_token"]
        if not token:
            raise RuntimeError("no bot token")
        fields = {k: (json.dumps(v) if isinstance(v, (list, dict)) else str(v)) for k, v in (fields or {}).items()}
        if file:
            boundary = "pcnc" + os.urandom(12).hex()
            parts = []
            for k, v in fields.items():
                parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode())
            field, filename, data, ctype = file
            parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                          % (boundary, field, filename, ctype)).encode() + data + b"\r\n")
            parts.append(("--%s--\r\n" % boundary).encode())
            body, ctype = b"".join(parts), "multipart/form-data; boundary=" + boundary
        else:
            body, ctype = urlencode(fields).encode(), "application/x-www-form-urlencoded"
        req = urllib.request.Request("%s/bot%s/%s" % (TELEGRAM_API, token, method), data=body,
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                reply = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                reply = json.loads(e.read() or b"{}")
            except ValueError:
                raise RuntimeError("Telegram answered HTTP %d" % e.code)
        except (urllib.error.URLError, OSError) as e:  # never shows the URL, which holds the token
            raise RuntimeError("cannot reach Telegram (%s)" % getattr(e, "reason", e))
        if not reply.get("ok"):
            raise RuntimeError(reply.get("description") or "Telegram refused the request")
        return reply.get("result")

    def send(self, text, photo=False, quiet=False, chat=None):
        """A message, with a camera photo when asked and available. Returns an error text or ""."""
        chat = chat or SETTINGS["telegram_chat"]
        jpeg = camera_snapshot() if photo else None
        try:
            if jpeg:
                self.api("sendPhoto", {"chat_id": chat, "caption": text[:1024], "parse_mode": "HTML",
                                       "disable_notification": quiet}, ("photo", "camera.jpg", jpeg, "image/jpeg"))
            else:
                self.api("sendMessage", {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML",
                                         "disable_notification": quiet, "disable_web_page_preview": True})
            return ""
        except RuntimeError as e:
            log_notify.warning("sending to Telegram failed: %s", e)
            return str(e)

    def _send_file(self, method, field, path, caption):
        if os.path.getsize(path) > TELEGRAM_MAX_UPLOAD:
            self.send(caption + "\n(The timelapse is too big for Telegram; download it from the dashboard.)")
            return
        with open(path, "rb") as f:
            data = f.read()
        ctype = "image/gif" if path.endswith(".gif") else "video/mp4"
        try:
            self.api(method, {"chat_id": SETTINGS["telegram_chat"], "caption": caption[:1024], "parse_mode": "HTML"},
                     (field, os.path.basename(path), data, ctype), timeout=300)
        except RuntimeError as e:
            log_notify.warning("sending the timelapse failed: %s", e)
            self.send(caption)

    def connect(self):
        """Check the token (getMe) and set the bot's command menu. Called when the token changes."""
        if not SETTINGS["telegram_token"]:
            self.bot, self.error = "", ""
            return
        try:
            me = self.api("getMe")
            self.bot, self.error = me.get("username", ""), ""
            self.api("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in self.COMMANDS]})
            log_notify.info("connected as @%s", self.bot)
        except RuntimeError as e:
            self.bot, self.error = "", str(e)
            log_notify.warning("Telegram: %s", e)
        self.offset = 0
        self.poll_wake.set()

    def view(self):
        with self.lock:
            chats = sorted(({"id": k, "name": v["name"], "t": v["t"]} for k, v in self.chats.items()),
                           key=lambda c: -c["t"])
        return {"token_set": bool(SETTINGS["telegram_token"]), "chat": SETTINGS["telegram_chat"],
                "bot": self.bot, "error": self.error, "chats": chats}

    def test(self):
        if not SETTINGS["telegram_token"]:
            return {"ok": False, "message": "enter the bot token first"}
        if not SETTINGS["telegram_chat"]:
            return {"ok": False, "message": "choose the chat first: send /start to your bot, then Refresh"}
        error = self.send("\U0001f44b Test from <b>%s</b>. Messages about jobs will arrive here.\n\n%s" % (
            _html(SETTINGS["name"]), status_text()), photo=SETTINGS["notify_photos"])
        return {"ok": not error, "message": error or "Sent - look in Telegram"}

    # ----- commands from the chat: reading only -----

    def _poll(self):
        while True:
            if not SETTINGS["telegram_token"]:
                self.poll_wake.wait(60)
                self.poll_wake.clear()
                continue
            if not self.bot and not self.error:
                self.connect()
            try:
                updates = self.api("getUpdates", {"offset": self.offset, "timeout": TELEGRAM_POLL,
                                                  "allowed_updates": ["message"]}, timeout=TELEGRAM_POLL + 15)
            except RuntimeError as e:
                if str(e) != self.error:
                    log_notify.warning("Telegram: %s", e)
                self.error = str(e)
                self.poll_wake.wait(30)
                self.poll_wake.clear()
                continue
            if self.error and self.bot:
                self.error = ""
            for u in updates or []:
                self.offset = max(self.offset, u.get("update_id", 0) + 1)
                try:
                    self._command(u.get("message") or {})
                except Exception:
                    log_notify.exception("answering a Telegram message failed")

    def _command(self, msg):
        chat = msg.get("chat") or {}
        if "id" not in chat:
            return
        cid = str(chat["id"])
        name = chat.get("title") or " ".join(filter(None, (chat.get("first_name"), chat.get("last_name")))) \
            or chat.get("username") or cid
        with self.lock:
            self.chats[cid] = {"name": name, "t": time.time()}
            for old in sorted(self.chats, key=lambda k: self.chats[k]["t"])[:-10]:
                del self.chats[old]
        text = (msg.get("text") or "").strip()
        if not text.startswith("/") or time.time() - msg.get("date", 0) > TELEGRAM_OLD_MESSAGE:
            return
        cmd = text.split()[0][1:].split("@")[0].lower()
        if cid != SETTINGS["telegram_chat"]:
            log_notify.info("message from chat %s (%s), which isn't the configured chat", cid, name)
            self.send("This bot reports on a CNC machine. To get its messages in this chat, enter the chat id "
                      "<code>%s</code> in the dashboard: Settings → Telegram." % cid, chat=cid)
            return
        log_notify.info("/%s from %s", cmd, name)
        if cmd == "status":
            self.send(status_text(), photo=True)
        elif cmd == "photo":
            if CAMERA and CAMERA.enabled():
                self.send("\U0001f4f7 %s" % _html(SETTINGS["name"]), photo=True)
            else:
                self.send("There is no camera on this machine.")
        else:
            self.send("This bot reports on <b>%s</b>: jobs starting and ending, progress, pauses and E-stop, with "
                      "camera photos and a timelapse of each job. It only reports: it can't control the machine.\n\n%s"
                      % (_html(SETTINGS["name"]), "\n".join("/%s - %s" % c for c in self.COMMANDS)))


def status_text():
    s = HUB.snapshot()[1]
    head = "<b>%s</b>" % _html(SETTINGS["name"])
    if not s["bridge_online"]:
        return head + " · OFFLINE (TNG not connected)"
    word = "E-STOP" if s.get("estop") else "PAUSED" if s.get("paused") else "RUNNING" if s.get("running") else "IDLE"
    lines = [head + " · " + word]
    if s.get("file"):
        lines.append(_html(_job_name(s["file"])))
    total = s.get("line_count") or 0
    if s.get("running") and total:
        line = s.get("line") or 0
        lines.append("Line %s of %s (%d %%)" % (line, total, 100 * line / total))
        left = s.get("time_to_end") or -1
        started = s.get("job_started")
        lines.append(" · ".join(filter(None, ("running %s" % fmt_duration(time.time() - started) if started else "",
                                                  "%s left" % fmt_duration(left) if left > 0 else ""))))
    units = "in/min" if s.get("units") == "in" else "mm/min"
    lines.append("Feed %d %s · spindle %d rpm · T%s" % (s.get("feed") or 0, units, s.get("spindle") or 0,
                                                                  s.get("tool") or "-"))
    pi = s.get("system") or {}
    if pi.get("temp") is not None:
        lines.append("Pi %.0f °C%s" % (pi["temp"], ", CPU %.0f %%" % pi["cpu"] if pi.get("cpu") is not None else ""))
    return "\n".join(line for line in lines if line)


TNG_PROFILE = os.environ.get("PCNC_PROFILE", os.path.expanduser("~/PlanetCNC/Profiles/Default"))
SERVER_STARTED = time.time()


def _file_digest(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


def os_release():
    info = {}
    for line in _read_text("/etc/os-release").splitlines():
        k, _, v = line.partition("=")
        info[k] = v.strip().strip('"')
    return info.get("PRETTY_NAME") or platform.platform()


def system_view():
    """Everything the System window shows."""
    state = HUB.snapshot()[1]
    installed = os.path.join(TNG_PROFILE, "Scripts", "planetcnc_bridge.py")
    ours, theirs = _file_digest(os.path.join(APP_DIR, "bridge", "planetcnc_bridge.py")), _file_digest(installed)
    return {
        "host": {
            "hostname": socket.gethostname(),
            "address": lan_address(),
            "model": _read_text("/proc/device-tree/model").rstrip("\x00") or platform.machine(),
            "os": os_release(),
            "kernel": "%s %s" % (platform.release(), platform.machine()),
            "python": sys.version.split()[0],
            "uptime": (SYSTEM.latest or {}).get("uptime") if SYSTEM else None,
            "server_started": SERVER_STARTED,
        },
        "app": UPDATER.status() if UPDATER else {"git": False, "message": UPDATES_OFF_REASON},
        "bridge": {
            "online": state["bridge_online"],
            "protocol": state.get("bridge_protocol"),
            "expected": ESTOP_PROTOCOL,
            "installed": "missing" if theirs is None else "current" if theirs == ours else "different",
            "path": installed,
        },
        "os": OS_PACKAGES.status() if OS_PACKAGES else None,
        "running": bool(state.get("running")),
    }


# ---------------------------------------------------------------------------
# Settings changed from the dashboard, kept in settings.json next to this file
# ---------------------------------------------------------------------------

SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
TUNING_FILE = os.path.join(APP_DIR, "tuning.json")   # the feeds log, see TuningLog
DEFAULT_NAME = "CNC Monitor"
MAX_NAME_LENGTH = 40
# camera / files_dir None: what --camera / --files-dir say
# Which CAM program wrote the G-code, for reading the tool sizes from its comments (see gcode.js).
TOOL_CAMS = {"fusion": "Fusion 360"}

SETTINGS = {"name": DEFAULT_NAME, "camera": None, "log_debug": False, "files_dir": None,
            "tool_shape": True, "tool_cam": "fusion",
            # Telegram notifications and the job timelapse (see Notifier, Timelapse)
            "telegram_token": "", "telegram_chat": "", "notify_progress": 25, "notify_photos": True,
            "notify_pi": True, "timelapse": True, "timelapse_interval": 10}
NOTIFY_PROGRESS_STEPS = (0, 10, 20, 25, 50)
TIMELAPSE_INTERVALS = (5, 10, 20, 30, 60)
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
        # Only this user may read it: it holds the Telegram bot token.
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(SETTINGS, f, indent=2)
        try:
            os.chmod(tmp, 0o600)  # also when the file existed with wider rights
        except OSError:
            pass
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


def norm_path(p):
    """A file path as TNG or this server writes it, made comparable (TNG writes //home/...)."""
    if not p:
        return ""
    if p.startswith("//") and os.sep == "/":
        p = "/" + p.lstrip("/")
    return os.path.normcase(os.path.realpath(p))


def files_rel(p):
    """Path of `p` inside the Files folder, or "" when it's elsewhere."""
    if not p or not CONFIG.files_dir:
        return ""
    full, root = norm_path(p), norm_path(CONFIG.files_dir)
    if not full.startswith(root + os.sep):
        return ""
    return os.path.relpath(full, root).replace(os.sep, "/")


class TuningLog:
    """Feeds and steps changed in the dashboard's Feeds & steps window: one entry per saved file,
    with how each job with that file went. Kept in tuning.json next to this file, so the values
    that worked can be copied back into the CAM program later."""

    MAX_ENTRIES = 500
    STATUSES = ("", "good", "bad")

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.entries = []
        self.seq = 0
        self.job = None   # (entry id, start time) while a job with an adjusted file runs
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.entries = [e for e in data.get("entries", []) if isinstance(e, dict) and e.get("id")]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as e:
            log_files.warning("cannot read %s: %s - starting an empty feeds log", path, e)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": self.entries}, f, indent=1)
        os.replace(tmp, self.path)
        self.seq += 1

    def list(self):
        with self.lock:
            return [dict(e) for e in self.entries], self.seq

    def add(self, entry):
        with self.lock:
            entry["id"] = "%x" % int(time.time() * 1000)
            while any(e["id"] == entry["id"] for e in self.entries):
                entry["id"] += "x"
            self.entries.append(entry)
            del self.entries[:-self.MAX_ENTRIES]
            self._save()
            return entry

    def update(self, entry_id, changes):
        with self.lock:
            for e in self.entries:
                if e["id"] == entry_id:
                    e.update(changes)
                    self._save()
                    return dict(e)
        return None

    def delete(self, entry_id):
        with self.lock:
            n = len(self.entries)
            self.entries = [e for e in self.entries if e["id"] != entry_id]
            if len(self.entries) == n:
                return False
            self._save()
            return True

    def job_started(self, path):
        key = norm_path(path) if path else None
        self.job = None
        with self.lock:
            for e in reversed(self.entries):
                if key and e.get("abs") and norm_path(e["abs"]) == key:
                    self.job = (e["id"], time.time())
                    break

    def job_ended(self, line, line_count):
        """Record how the job went; returns the file name when it was an adjusted file."""
        if not self.job:
            return None
        entry_id, started = self.job
        self.job = None
        run = {"start": started, "end": time.time(), "line": line, "lines": line_count,
               "done": bool(line_count) and line >= line_count - 2}
        with self.lock:
            for e in self.entries:
                if e["id"] == entry_id:
                    e.setdefault("runs", []).append(run)
                    del e["runs"][:-20]
                    try:
                        self._save()
                    except OSError as err:
                        log_files.error("cannot save the feeds log: %s", err)
                    return e.get("name", "")
        return None


HUB = Hub()
TUNING = None
CAMERA = None
SYSTEM = None
UPDATER = None
OS_PACKAGES = None
NOTIFY = None
TIMELAPSE = None
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
            if took > SLOW_REQUEST and path not in ("/api/events", "/api/camera.mjpg", "/api/tng/call", "/api/estop",
                                                     "/api/update/check", "/api/update/apply", "/api/update/rollback",
                                                     "/api/system/os/check"):
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
        elif path == "/api/system":
            self._json(200, system_view())
        elif path == "/api/timelapses/download":
            self._timelapse_download()
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
        elif path == "/api/tuning":
            entries, seq = TUNING.list()
            self._json(200, {"ok": True, "entries": entries, "seq": seq})
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
        if path == "/api/estop":
            if self._require_writer():
                self._estop()
            return
        if path == "/api/telegram/test":
            if self._require_writer():
                result = NOTIFY.test() if NOTIFY else {"ok": False, "message": "not available"}
                self._json(200 if result["ok"] else 409, result)
            return
        if path in ("/api/system/os/check", "/api/system/os/upgrade", "/api/system/power"):
            if self._require_writer():
                self._system_action(path.rsplit("/", 1)[1])
            return
        if path in ("/api/files/upload", "/api/files/delete"):
            if self._require_writer():
                (self._files_upload if path.endswith("upload") else self._files_delete)()
            return
        if path == "/api/settings":
            if self._require_writer():
                self._settings_change()
            return
        if path == "/api/tuning":
            if self._require_writer():
                self._tuning_change()
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
        """Check for anything that changes the machine or the Pi (E-stop, uploads, settings, updates)."""
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

    # ---------- E-stop: the one command the dashboard can give the machine ----------

    def _estop(self):
        """Switch TNG's E-stop on. The bridge only ever engages it; releasing happens at the machine."""
        who = "%s %s" % (self.client_address[0], short_agent(self.headers.get("User-Agent", "")))
        state = HUB.snapshot()[1]
        if not state["bridge_online"]:
            result = {"ok": False, "message": "TNG bridge is offline - the dashboard cannot reach the machine"}
        elif (state.get("bridge_protocol") or 1) < ESTOP_PROTOCOL:
            result = {"ok": False, "message": "the bridge running in TNG is too old for the E-stop - "
                                              "install the update, or restart TNG"}
        else:
            HUB.log("err", "E-STOP pressed on the dashboard (%s)" % who)
            result = HUB.wait_result(HUB.queue_command({"op": "estop"}, first=True))
        HUB.log("ok" if result["ok"] else "err",
                "E-stop: %s" % (result.get("message") or ("done" if result["ok"] else "failed")))
        self._json(200 if result["ok"] else 409, result)

    def _timelapse_download(self):
        path = TIMELAPSE.path(self._query("name")) if TIMELAPSE else None
        if not path:
            self._json(404, {"ok": False, "message": "no such timelapse"})
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(os.path.basename(path)))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 256 * 1024)

    # ---------- System window: OS packages, reboot / shut down ----------

    def _system_action(self, action):
        try:
            data = json.loads(self._read_body() or b"{}")
        except ValueError:
            data = {}
        if not OS_PACKAGES:
            self._json(409, {"ok": False, "message": "only available on a Linux computer with apt"})
            return
        if action == "check":
            self._json(200, dict(OS_PACKAGES.refresh(update_lists=True), ok=True))
            return
        if HUB.snapshot()[1].get("running"):
            self._json(409, {"ok": False, "message": "A program is running - do this afterwards"})
            return
        who = "%s %s" % (self.client_address[0], short_agent(self.headers.get("User-Agent", "")))
        if action == "upgrade":
            result = OS_PACKAGES.upgrade(who)
        else:
            result = power_action(str(data.get("action", "")), who)
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
                "tool_cams": [{"id": k, "label": v} for k, v in TOOL_CAMS.items()],
                "telegram": NOTIFY.view() if NOTIFY else None,
                "notify_progress": SETTINGS["notify_progress"], "notify_photos": bool(SETTINGS["notify_photos"]),
                "notify_pi": bool(SETTINGS["notify_pi"]), "timelapse": bool(SETTINGS["timelapse"]),
                "timelapse_interval": SETTINGS["timelapse_interval"],
                "timelapses": TIMELAPSE.list() if TIMELAPSE else [], "has_camera": bool(CAMERA)}

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
        if "telegram_token" in data:
            token = str(data["telegram_token"] or "").strip()
            if token and not re.match(r"^\d+:[\w-]{20,}$", token):
                self._json(400, {"ok": False, "message": "that doesn't look like a bot token (123456:ABC-...)"})
                return
            changes["telegram_token"] = token
        if "telegram_chat" in data:
            chat = str(data["telegram_chat"] or "").strip()
            if chat and not re.match(r"^-?\d+$", chat):
                self._json(400, {"ok": False, "message": "a chat id is a number"})
                return
            changes["telegram_chat"] = chat
        for key, allowed in (("notify_progress", NOTIFY_PROGRESS_STEPS), ("timelapse_interval", TIMELAPSE_INTERVALS)):
            if key in data:
                try:
                    value = int(data[key])
                except (TypeError, ValueError):
                    value = None
                if value not in allowed:
                    self._json(400, {"ok": False, "message": "%s must be one of %s" % (key, allowed)})
                    return
                changes[key] = value
        for key in ("notify_photos", "notify_pi", "timelapse"):
            if key in data:
                changes[key] = bool(data[key])
        try:
            save_settings(changes)
        except OSError as e:
            self._json(500, {"ok": False, "message": "could not save settings: %s" % e})
            return
        LOG.info("settings changed from %s: %s", self.client_address[0],
                 {k: ("***" if k == "telegram_token" and v else v) for k, v in changes.items()})
        if "telegram_token" in changes and NOTIFY:
            NOTIFY.connect()
            v = NOTIFY.view()
            HUB.log("info" if v["bot"] or not v["token_set"] else "warn", "Telegram: %s" % (
                "connected as @%s" % v["bot"] if v["bot"] else v["error"] or "off"))
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

    # ---------- feeds log (Feeds & steps window, Feeds tab) ----------

    def _tuning_change(self):
        try:
            data = json.loads(self._read_body() or b"{}")
            action = data.get("action")
        except (ValueError, AttributeError):
            self._json(400, {"ok": False, "message": "invalid json"})
            return
        try:
            if action == "add":
                rel = str(data.get("path", ""))
                full = self._files_path(rel)
                if not full or not os.path.isfile(full):
                    self._json(404, {"ok": False, "message": "file not found"})
                    return
                ops = data.get("ops")
                if not isinstance(ops, list) or len(json.dumps(ops)) > 200000:
                    self._json(400, {"ok": False, "message": "invalid operations"})
                    return
                entry = TUNING.add({"time": time.time(), "name": os.path.basename(full), "path": rel, "abs": full,
                                    "source": str(data.get("source", ""))[:300], "base": str(data.get("base", ""))[:300],
                                    "units": "in" if data.get("units") == "in" else "mm", "ops": ops,
                                    "status": "", "note": str(data.get("note", ""))[:1000], "runs": []})
                log_files.info("feeds log: %s adjusted from %s (from %s)", rel, entry["source"], self.client_address[0])
                result = (200, {"ok": True, "entry": entry})
            elif action == "update":
                changes = {}
                if "status" in data:
                    if data["status"] not in TuningLog.STATUSES:
                        self._json(400, {"ok": False, "message": "invalid status"})
                        return
                    changes["status"] = data["status"]
                if "note" in data:
                    changes["note"] = str(data["note"])[:1000]
                e = TUNING.update(str(data.get("id", "")), changes)
                result = (200 if e else 404, {"ok": bool(e), "entry": e, "message": "" if e else "entry not found"})
            elif action == "delete":
                ok = TUNING.delete(str(data.get("id", "")))
                result = (200 if ok else 404, {"ok": ok, "message": "deleted" if ok else "entry not found"})
            else:
                self._json(400, {"ok": False, "message": "unknown action"})
                return
        except OSError as e:
            log_files.error("cannot save the feeds log: %s", e)
            self._json(500, {"ok": False, "message": "could not save: %s" % e})
            return
        with HUB.cond:
            HUB.state_seq += 1  # every open dashboard reloads the log
            HUB.cond.notify_all()
        self._json(*result)

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




DEMO_ESTOP = threading.Event()   # the dashboard's E-stop in demo mode
DEMO_ESTOP_HOLD = 8.0            # seconds before the simulated operator releases it "at the machine"


def demo_commands():
    """Answer the server's requests (reads, E-stop) in demo mode."""
    while True:
        time.sleep(0.2)
        results = []
        for c in HUB.take_commands():
            if c.get("op") == "estop":
                DEMO_ESTOP.set()
                results.append({"id": c["id"], "ok": True, "message": "E-stop engaged (demo)"})
            else:
                results.append({"id": c["id"], "ok": True, "message": "demo: %s accepted" % c.get("op")})
        HUB.add_results(results)


def demo_estop(pos, line_no, line_count):
    """Hold the demo machine in E-stop for a while, as if someone then released it at the machine."""
    end = time.time() + DEMO_ESTOP_HOLD
    while time.time() < end:
        HUB.set_state({"work": pos + [0.0] * 6, "machine": [pos[0] + 120.0, pos[1] + 80.0, pos[2] - 40.0] + [0.0] * 6,
                       "line": line_no, "line_count": line_count, "feed": 0.0, "spindle": 0.0, "tool": 1,
                       "units": "mm", "coordsys": "G54", "running": False, "paused": False, "estop": True,
                       "idle": False, "file": "C:/CNC/demo_part.nc", "bridge_protocol": ESTOP_PROTOCOL})
        time.sleep(0.1)
    DEMO_ESTOP.clear()


def run_demo():
    text = demo_program()
    HUB.set_program("C:/CNC/demo_part.nc", text)
    moves = demo_moves(text)
    line_count = len(text.splitlines())
    while True:
        pos = [0.0, 0.0, 25.0]
        start = time.time()
        for line_no, x, y, z, rapid in moves:
            if DEMO_ESTOP.is_set():
                demo_estop(pos, line_no, line_count)
                break
            feed = 5000.0 if rapid else 1500.0
            dist = math.dist(pos, (x, y, z))
            steps = max(1, int(dist / (feed / 60.0) / 0.1))
            p, target = pos, [x, y, z]
            for i in range(1, steps + 1):
                if DEMO_ESTOP.is_set():
                    target = p  # stopped mid-move: the next round sees the E-stop
                    break
                p = [pos[k] + ((x, y, z)[k] - pos[k]) * i / steps for k in range(3)]
                HUB.set_state({
                    "work": p + [0.0] * 6,
                    "machine": [p[0] + 120.0, p[1] + 80.0, p[2] - 40.0] + [0.0] * 6,
                    "line": line_no, "line_count": line_count,
                    "feed": feed, "spindle": 12000.0, "tool": 1, "units": "mm",
                    "coordsys": "G54", "running": True, "paused": False,
                    "estop": False, "idle": False, "file": "C:/CNC/demo_part.nc",
                    "elapsed": time.time() - start, "time_to_end": None, "bridge_protocol": ESTOP_PROTOCOL,
                })
                time.sleep(0.1)
            pos = target
        else:
            if DEMO_ESTOP.is_set():  # pressed during the last move
                demo_estop(pos, line_count, line_count)
        for _ in range(30):  # idle a few seconds between runs
            if DEMO_ESTOP.is_set():
                demo_estop(pos, line_count, line_count)
                break
            HUB.set_state({"work": pos + [0.0] * 6,
                           "machine": [pos[0] + 120.0, pos[1] + 80.0, pos[2] - 40.0] + [0.0] * 6,
                           "line": line_count, "line_count": line_count, "feed": 0.0,
                           "spindle": 0.0, "tool": 1, "units": "mm", "coordsys": "G54",
                           "running": False, "paused": False, "estop": False, "idle": True,
                           "file": "C:/CNC/demo_part.nc", "bridge_protocol": ESTOP_PROTOCOL})
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
            if NOTIFY and HUB.state.get("running"):
                NOTIFY.event("bridge_lost")
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

    global CAMERA, SYSTEM, UPDATER, UPDATES_OFF_REASON, TUNING, OS_PACKAGES, NOTIFY, TIMELAPSE
    TUNING = TuningLog(TUNING_FILE)
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
    if OsPackages.available() and not args.demo:
        OS_PACKAGES = OsPackages()
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

    TIMELAPSE = Timelapse()
    NOTIFY = Notifier()

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
