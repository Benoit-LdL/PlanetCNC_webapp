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
import math
import mimetypes
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
BRIDGE_TIMEOUT = 3.0  # seconds without an update before the bridge is reported offline
COMMAND_TTL = 2.0      # a command the bridge hasn't picked up by then is dropped, never run late
COMMAND_WAIT = 10.0    # how long a browser request waits for TNG's answer
MAX_JOG_STEP = 100.0   # largest single jog step accepted (mm or inch)
JOG_AXES = "XYZABC"


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

    # ----- machine commands: browser -> server -> bridge (in its /api/state reply) -----

    def queue_command(self, cmd):
        with self.cond:
            cmd = dict(cmd, id=self.next_cmd_id)
            self.next_cmd_id += 1
            self.commands.append((time.time(), cmd))
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
            self.commands = []
            self.cond.notify_all()
            return fresh

    def add_results(self, results):
        with self.cond:
            for r in results:
                if isinstance(r, dict) and "id" in r:
                    self.results[r["id"]] = {"ok": bool(r.get("ok")), "message": str(r.get("message", ""))}
            self.cond.notify_all()

    def wait_result(self, cmd_id):
        end = time.time() + COMMAND_WAIT
        with self.cond:
            while cmd_id not in self.results:
                for queued_at, cmd in self.commands:
                    if cmd["id"] == cmd_id and time.time() - queued_at > COMMAND_TTL:
                        self.commands.remove((queued_at, cmd))
                        return {"ok": False, "message": "TNG bridge is not responding - command dropped"}
                remaining = end - time.time()
                if remaining <= 0:
                    return {"ok": False, "message": "No answer from TNG"}
                self.cond.wait(min(remaining, 0.5))
            return self.results.pop(cmd_id)

    def set_state(self, state, source=""):
        with self.cond:
            if source and time.time() - self.last_state_time >= BRIDGE_TIMEOUT:
                print("Bridge connected from %s" % source, flush=True)
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
            seq = self.state_seq
        s["camera"] = {"live": CAMERA.live()} if CAMERA else None
        s["control_enabled"] = bool(CONFIG.viewer_password)
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


class Camera:
    """Runs ffmpeg while at least one viewer is connected and keeps the latest JPEG frame.

    With a V4L2 device it first asks the camera for MJPEG and copies the frames
    untouched (almost no CPU on a Raspberry Pi). If the camera can't do MJPEG it
    falls back to raw capture + JPEG encoding in ffmpeg.
    """

    def __init__(self, device="", size="", fps=0, command=""):
        self.device, self.size, self.fps, self.command = device, size, fps, command
        self.cond = threading.Condition()
        self.frame = None
        self.frame_seq = 0
        self.frame_time = 0.0
        self.clients = 0
        self.transcode = False       # current capture mode for a V4L2 device
        self.mode_confirmed = False  # a mode has produced frames: stop alternating
        self._reported = set()  # errors already printed since the last good capture
        threading.Thread(target=self._run, daemon=True).start()

    def live(self):
        return time.time() - self.frame_time < 3.0

    def add_client(self):
        with self.cond:
            self.clients += 1
            self.cond.notify_all()

    def remove_client(self):
        with self.cond:
            self.clients -= 1

    def wait_frame(self, last_seq, timeout):
        """Return (jpeg, seq): a newer frame, or after `timeout` a keepalive frame."""
        with self.cond:
            self.cond.wait_for(lambda: self.frame_seq != last_seq, timeout=timeout)
            if self.frame is not None and self.live():
                return self.frame, self.frame_seq
            return NO_SIGNAL_JPEG, self.frame_seq

    def _cmd(self):
        if self.command:
            return shlex.split(self.command)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "v4l2"]
        if not self.transcode:
            cmd += ["-input_format", "mjpeg"]
        if self.size:
            cmd += ["-video_size", self.size]
        if self.fps:
            cmd += ["-framerate", str(self.fps)]
        cmd += ["-i", self.device]
        cmd += ["-c:v", "mjpeg", "-q:v", "6"] if self.transcode else ["-c:v", "copy"]
        return cmd + ["-f", "mjpeg", "-"]

    def _run(self):
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.clients > 0)
            if self._capture():
                self.mode_confirmed = True
            else:
                if not self.command and not self.mode_confirmed:
                    self.transcode = not self.transcode  # try the other capture mode next
                time.sleep(2.0)

    def _capture(self):
        """Run one ffmpeg process until it dies, stalls or nobody watches. True if it produced frames."""
        cmd = self._cmd()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            self._report("camera: cannot start %s: %s" % (cmd[0], e))
            return False

        errors = deque(maxlen=5)
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
                    proc.kill()
                    return
        threading.Thread(target=watchdog, daemon=True).start()

        got = False
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
                    del buf[:end + 2]
                if len(buf) > 16 * 1024 * 1024:
                    buf.clear()
        finally:
            stop.set()
            proc.kill()
            proc.wait()
        if got:
            self._reported.clear()
        else:
            self._report("camera: %s produced no image%s" % (
                cmd[0], (" - " + errors[-1]) if errors else ""))
        return got

    def _publish(self, jpeg):
        with self.cond:
            self.frame = jpeg
            self.frame_seq += 1
            self.frame_time = time.time()
            self.cond.notify_all()

    def _report(self, msg):
        if msg not in self._reported:  # don't flood the console while a camera is unplugged
            print(msg, flush=True)
            self._reported.add(msg)


HUB = Hub()
CAMERA = None
REJECTED_BRIDGES = set()  # addresses already reported for a wrong token
CONFIG = argparse.Namespace(token="", viewer_password="")


class Handler(BaseHTTPRequestHandler):
    server_version = "PlanetCNCWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; the bridge posts several times per second

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

    def _viewer_authorized(self):
        if not CONFIG.viewer_password:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
                return hmac.compare_digest(pw, CONFIG.viewer_password)
            except Exception:
                return False
        return False

    def _require_viewer(self):
        if self._viewer_authorized():
            return True
        self._send(401, "Authentication required", "text/plain",
                   {"WWW-Authenticate": 'Basic realm="PlanetCNC monitor"'})
        return False

    # ---------- routes ----------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._require_viewer():
            return
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
        else:
            self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/control":
            self._control()
            return
        if not self._bridge_authorized():
            client = self.client_address[0]
            if client not in REJECTED_BRIDGES:
                REJECTED_BRIDGES.add(client)
                print("Rejected bridge from %s: %s" % (client, (
                    "wrong token (TOKEN in the bridge must match --token)" if CONFIG.token else
                    "without --token only a bridge on this machine is accepted")), flush=True)
            self.close_connection = True  # the request body was not read
            self._json(403, {"error": "bad token"})
            return
        try:
            data = json.loads(self._read_body() or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid json"})
            return
        if path == "/api/state":
            HUB.add_results(data.pop("results", None) or [])
            HUB.set_state(data, source=self.client_address[0])
            with HUB.cond:
                pid = HUB.program["id"]
            # Tell the bridge which program we hold so it can resend after a server restart,
            # and hand it any machine commands from the dashboard.
            self._json(200, {"ok": True, "program_id": pid, "commands": HUB.take_commands()})
        elif path == "/api/program":
            pid = HUB.set_program(str(data.get("name", "")), str(data.get("text", "")))
            self._json(200, {"ok": True, "program_id": pid})
        else:
            self._json(404, {"error": "not found"})

    def _control(self):
        """Machine commands from the dashboard: home, jog, stop, load a program."""
        self.close_connection = True  # error replies below may leave the body unread
        if not CONFIG.viewer_password:
            self._json(403, {"ok": False, "message":
                             "Machine control needs a dashboard password: start server.py with --viewer-password"})
            return
        if not self._require_viewer():
            return
        # A custom header can't be sent by another website without CORS approval, so a
        # page elsewhere can't use the browser's saved password to move the machine.
        if self.headers.get("X-CNC-Control") != "1":
            self._json(403, {"ok": False, "message": "missing X-CNC-Control header"})
            return
        try:
            data = json.loads(self._read_body() or b"{}")
        except ValueError:
            self._json(400, {"ok": False, "message": "invalid json"})
            return

        action = data.get("action")
        if action in ("stop", "home"):
            cmd, desc = {"action": action}, action
        elif action == "jog":
            axis = str(data.get("axis", "")).upper()
            try:
                dist = float(data.get("dist"))
            except (TypeError, ValueError):
                dist = 0.0
            if len(axis) != 1 or axis not in JOG_AXES or not 0 < abs(dist) <= MAX_JOG_STEP:
                self._json(400, {"ok": False, "message": "invalid jog"})
                return
            cmd, desc = {"action": "jog", "axis": axis, "dist": dist}, "jog %s%+g" % (axis, dist)
        elif action == "load":
            name, text = str(data.get("name", "")), str(data.get("text", ""))
            if not text.strip():
                self._json(400, {"ok": False, "message": "empty program"})
                return
            cmd, desc = {"action": "load", "name": name, "text": text}, "load %s" % name
        else:
            self._json(400, {"ok": False, "message": "unknown action"})
            return

        state = HUB.snapshot()[1]
        if not state["bridge_online"]:
            self._json(409, {"ok": False, "message": "TNG bridge is offline"})
            return
        if action != "stop" and state.get("running"):
            self._json(409, {"ok": False, "message": "A program is running"})
            return
        print("Control: %s (from %s)" % (desc, self.client_address[0]), flush=True)
        result = HUB.wait_result(HUB.queue_command(cmd))
        if not result["ok"]:
            print("Control: %s failed: %s" % (desc, result["message"]), flush=True)
        self._json(200 if result["ok"] else 409, result)

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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

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
        CAMERA.add_client()
        try:
            seq = -1
            while True:
                jpeg, seq = CAMERA.wait_frame(seq, CAMERA_KEEPALIVE)
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                 % len(jpeg) + jpeg + b"\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            CAMERA.remove_client()


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


DEMO_CONTROLS = {"home": True, "jog": True, "load": True, "stop": True}


def demo_commands():
    """Answer dashboard commands like TNG would, without moving anything."""
    while True:
        time.sleep(0.2)
        HUB.add_results([{"id": c["id"], "ok": True, "message": "demo: %s accepted" % c["action"]}
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
                    "estop": False, "idle": False, "file": "C:/CNC/demo_part.nc", "controls": DEMO_CONTROLS,
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
                           "file": "C:/CNC/demo_part.nc", "controls": DEMO_CONTROLS})
            time.sleep(0.1)


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
    ap.add_argument("--viewer-password", default=os.environ.get("PCNC_VIEWER_PASSWORD", ""),
                    help="require HTTP basic auth (any username) for the dashboard")
    ap.add_argument("--demo", action="store_true", help="simulate a running machine")
    ap.add_argument("--camera", default=os.environ.get("PCNC_CAMERA", "auto"),
                    help="V4L2 camera device, 'auto' (/dev/video0 if present) or 'none'")
    ap.add_argument("--camera-size", default="1280x720", help="requested resolution, e.g. 640x480")
    ap.add_argument("--camera-fps", type=int, default=15, help="requested frame rate")
    ap.add_argument("--camera-cmd", default=os.environ.get("PCNC_CAMERA_CMD", ""),
                    help="custom command that writes MJPEG to stdout (overrides --camera)")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # show messages in journalctl right away, not when a buffer fills
    CONFIG.token = args.token
    CONFIG.viewer_password = args.viewer_password

    global CAMERA
    device = args.camera
    if device == "auto":
        device = "/dev/video0" if os.path.exists("/dev/video0") else "none"
    if args.camera_cmd:
        CAMERA = Camera(command=args.camera_cmd)
        print("Camera: " + args.camera_cmd)
    elif device != "none":
        if shutil.which("ffmpeg"):
            CAMERA = Camera(device, args.camera_size, args.camera_fps)
            print("Camera: %s (%s @ %d fps)" % (device, args.camera_size, args.camera_fps))
        else:
            print("Camera: ffmpeg not found, camera disabled (sudo apt install ffmpeg)")

    if args.demo:
        threading.Thread(target=run_demo, daemon=True).start()
        threading.Thread(target=demo_commands, daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("PlanetCNC web monitor on http://%s:%d%s" % (
        lan_address() if args.host == "0.0.0.0" else args.host, args.port,
        "  (demo mode)" if args.demo else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
