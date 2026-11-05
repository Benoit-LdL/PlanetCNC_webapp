# PlanetCNC Web Monitor

A web dashboard for a CNC machine run by PlanetCNC TNG. Open it from any
computer, tablet or phone on the home network to see:

- the live position of every axis, feed, spindle speed and tool;
- job progress, the line being run and the estimated time left;
- a 3D view of the machine bed with the job's toolpath and where the tool has been;
- the USB camera pointed at the machine;
- the G-code program, a console with what happened, and the files on the Pi;
- the health of the Raspberry Pi (CPU, memory, temperature, power supply).

From the dashboard you can also preview a G-code file before cutting, load it
into TNG, upload files to the Pi, home the machine, stop it, and install
updates of this app.

Two things are left out on purpose:

- **No jogging.** Moving the machine by hand needs someone standing next to it.
  **Home** moves the machine too, so it asks for confirmation first.
- **No password.** Anyone on the home network who opens the page can use it.
  Don't make the dashboard reachable from the internet.

## Contents

- [How it works](#how-it-works)
- [What you need](#what-you-need)
- [Installation](#installation)
- [Using the dashboard](#using-the-dashboard)
- [Updating](#updating)
- [Logs and debugging](#logs-and-debugging)
- [Server options](#server-options)
- [Inside the bridge](#inside-the-bridge)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)

## How it works

```
 ┌──────────── Raspberry Pi ─────────────────────────────┐
 │                                                       │
 │  PlanetCNC TNG                                        │
 │   └─ bridge (planetcnc_bridge.py, runs inside TNG)    │
 │        │  machine state 5x/s, program, log lines      │
 │        ▼  ▲ commands (home, stop, load, MDI)          │
 │  server.py ◄── USB camera (through ffmpeg)            │
 │        │                                              │
 └────────┼──────────────────────────────────────────────┘
          ▼ http://<pi>:8080
   browsers on the home network (PC, tablet, phone)
```

There are three parts:

1. **The bridge** (`bridge/planetcnc_bridge.py`) runs inside TNG, because only
   there can Python read TNG's state through the `planetcnc` module. Five
   times a second it reads the values the server asks for and sends them to
   the server. With each answer it gets back the commands to run from the
   dashboard. When the loaded program changes, it sends the program to the
   server as well.
2. **The server** (`server.py`) is a small web server written in Python, using
   only the standard library, so there's nothing to `pip install`. It turns the
   bridge's raw values into what the dashboard shows and pushes every change
   to each open dashboard. It also streams the camera, lists the Pi's files,
   runs updates and writes the log.
3. **The dashboard** (`web/`) is the page the browser loads. It draws the 3D
   view with three.js, which the browser downloads from a CDN, so the device
   viewing it needs internet access. The Pi itself doesn't.

The bridge and the server run on the same Pi and talk over `127.0.0.1`. By
default the server only accepts a bridge from the Pi itself.

### Files

| File | What it is |
|---|---|
| `server.py` | The web server. |
| `bridge/planetcnc_bridge.py` | Runs inside TNG: reads TNG's state, runs the dashboard's commands. |
| `bridge/bridge_autostart.py` | Started by TNG at startup; runs the bridge in the background and restarts it after a crash. |
| `bridge/Expr_WebMonitor.txt` | Tells TNG to run `bridge_autostart.py` when it starts. |
| `update.sh` | Gets the latest version from GitHub and copies the bridge files into TNG's profile. |
| `web/` | The dashboard page (HTML, CSS, JavaScript). |
| `settings.json` | Created when you change a setting in the dashboard. Not part of git. |
| `logs/` | The server's log files. Not part of git. |

## What you need

- A Raspberry Pi (or another Linux computer) running **PlanetCNC TNG** with
  the machine connected. This guide uses the setup it was written for:
  - user `mekanika`;
  - app in `/home/mekanika/PlanetCNC_webapp`;
  - TNG profile in `/home/mekanika/PlanetCNC/Profiles/Default`;
  - Pi address `10.0.0.14`.

  Replace these with your own where they differ.
- **Python 3.8 or newer.** Raspberry Pi OS already has it.
- **git**, to install and update the app.
- **ffmpeg**, only if you want the camera.
- A **USB camera** (optional). Most webcams work. A camera that sends MJPEG
  costs the Pi almost no CPU.

## Installation

Do all of this on the Pi, at its keyboard or over SSH
(`ssh mekanika@10.0.0.14`).

### 1. Install git and ffmpeg

```bash
sudo apt update
sudo apt install git ffmpeg
```

### 2. Get the app

```bash
git clone https://github.com/Benoit-LdL/PlanetCNC_webapp ~/PlanetCNC_webapp
```

Keep it a git clone: the dashboard's update button and `update.sh` rely on it.
Don't edit files in this folder by hand, because changed files block updates.

### 3. Try the server

```bash
cd ~/PlanetCNC_webapp
python3 server.py --demo
```

`--demo` simulates a machine running a job, so you can check the dashboard
before TNG is involved. Open **http://10.0.0.14:8080** in a browser on another
device. You should see the demo part being cut. Press Ctrl+C to stop the
server.

### 4. Enable Python in TNG

TNG has to know where Python's shared library is. Find it:

```bash
ls /usr/local/lib/libpython3* /usr/lib/*/libpython3*
```

If nothing is listed, run `sudo apt install libpython3-dev` and look again.

In TNG, open **Settings → Python Library** and enter the full path of the
`libpython3.x.so...` file, e.g. `/usr/local/lib/libpython3.11.so.1.0`. Press
**Check**: it should show the Python version. Then restart TNG.

### 5. Install the bridge into TNG

```bash
~/PlanetCNC_webapp/update.sh
```

This copies three files into TNG's profile:

| Copied to | File |
|---|---|
| `Profiles/Default/Scripts/` | `planetcnc_bridge.py`, `bridge_autostart.py` |
| `Profiles/Default/Expr/` | `Expr_WebMonitor.txt` |

TNG runs the `#OnInit` section of every `Expr*.txt` file in that folder when
it starts. `Expr_WebMonitor.txt` uses it to start the bridge through
`bridge_autostart.py`, in the background so TNG doesn't wait for it. It's a
separate file, so the machine maker's own `Expr.txt` stays untouched.

For another profile, put `PCNC_PROFILE=/path/to/profile` in front of the
command. `Expr_WebMonitor.txt` and `bridge_autostart.py` contain the full
profile path, so adjust it in both files too.

### 6. Start the server at boot

Create the service file:

```bash
sudo nano /etc/systemd/system/planetcnc-web.service
```

with this content (change the user and paths if yours differ):

```ini
[Unit]
Description=PlanetCNC web monitor
After=network-online.target
Wants=network-online.target

[Service]
User=mekanika
EnvironmentFile=-/etc/planetcnc-web.env
WorkingDirectory=/home/mekanika/PlanetCNC_webapp
ExecStart=/usr/bin/python3 /home/mekanika/PlanetCNC_webapp/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`Restart=always` matters: after an update the server stops itself, and
systemd starts the new version.

Then enable it:

```bash
sudo usermod -aG video mekanika           # let that user read the camera
sudo systemctl daemon-reload
sudo systemctl enable --now planetcnc-web
```

To pass [server options](#server-options), add them to the `ExecStart` line,
then run `sudo systemctl daemon-reload && sudo systemctl restart planetcnc-web`.

### 7. Restart TNG and check

Restart TNG (or the whole Pi). Then check each part:

| Check | Expected |
|---|---|
| TNG's output window | `PlanetCNC web bridge: connected to http://127.0.0.1:8080` |
| `systemctl status planetcnc-web` | `active (running)` |
| `journalctl -u planetcnc-web -n 20` | `PlanetCNC web monitor on http://10.0.0.14:8080` and `bridge: connected from 127.0.0.1` |
| http://10.0.0.14:8080 | Status shows `IDLE` (not `OFFLINE`), the position matches TNG |

If something doesn't match, see [Troubleshooting](#troubleshooting).

### Starting by hand instead

For testing without the service and the autostart:

```bash
cd ~/PlanetCNC_webapp && python3 server.py
```

and in TNG, **File → Run Python Script** →
`Profiles/Default/Scripts/planetcnc_bridge.py`. The server only runs as long
as its terminal stays open.

## Using the dashboard

Open **http://10.0.0.14:8080**. The page adapts to tablets and phones.

### Top bar

- The machine's name (set in Settings) and the loaded file.
- The version button. When an update is available it says so; click it to
  open Settings.
- The status:

  | Status | Meaning |
  |---|---|
  | `RUNNING` (green) | A job is running. |
  | `PAUSED` (yellow) | The job is paused. |
  | `E-STOP` (red, blinking) | The emergency stop is active. |
  | `IDLE` | Connected, no job running. |
  | `OFFLINE` | The server gets no updates from the bridge, or this page lost its connection to the server. |

- The thin bar below the top bar shows job progress.

### Left panel

- **Position** of each axis. Switch between **Work** and **Machine**
  coordinates with the tabs. A, B and C only appear when they are used.
- **Feed, spindle, tool, coordinate system.**
- **Line, progress, time remaining** (TNG's own estimate when it has one) and
  the time of the last update.
- **Machine:**
  - **Home** asks for confirmation, then homes all axes.
  - **Stop** stops the machine, also during a job.
  - Homing and loading are refused while a program runs. The line below the
    buttons shows TNG's answer, or why a command was refused.
- **Raspberry Pi:** CPU, memory, temperature and disk use, refreshed every 2
  seconds, plus uptime.
  - A bar turns amber or red, with a word (busy, high, warm, hot, full), when a
    value gets high. The Pi slows itself down from about 80 °C.
  - Next to the title is the power status from the Pi's firmware:
    **Under-voltage!** means the power supply is too weak right now, and
    "since boot" means it happened earlier. A weak supply is a common cause of
    random Pi problems.

### 3D view (middle)

- **The grid and box** are the machine's real travel, from TNG's motor limits
  (Settings → Motors → Limits), placed relative to your current work zero.
  The red, green and blue arrows mark work zero.
- **The toolpath.** Cutting moves are shaded by depth: light at the top, dark
  blue at the deepest cut. Rapids are dark red. The thin box around the job
  covers every move.
  - Big 3D jobs have millions of tiny moves, too many to draw smoothly. The
    view drops points that lie within a small distance of a straight line
    through their neighbours. The shape stays, but there are far fewer lines.
  - The note under the view says how many moves are drawn and how far the
    drawing can be from the real path, for example "10k of 480k moves drawn
    (within 0.012 mm)".
- **Trace** (yellow) shows where the tool actually went during this job.
- **3D / Top / Front / Right** switch the angle; **Fit** shows the bed and the
  job; **Follow** keeps the tool centred.
- **Open file** previews a G-code file from your computer, without touching
  TNG, so you can check its origin and orientation against the bed.
  - **Load in TNG** makes it TNG's loaded program. It doesn't start it.
  - **Close preview** goes back to TNG's program.
- **Mouse:** left-drag rotates (3D only), right-drag pans, the wheel zooms.
  **Touch:** one finger rotates, two fingers zoom and pan.

### Camera (right, top)

- The green dot means the camera is live; red means no picture.
- **Full**, or a double-click, shows it fullscreen. **Hide** folds it away;
  each browser remembers this.
- The Pi only runs the camera while someone has the dashboard open. A browser
  tab in the background doesn't keep it running.

### Files (right, middle)

A folder on the Pi: the Desktop by default (see `--files-dir`).

- Click a folder to open it, and the path at the top to go back. The list
  refreshes by itself every 15 seconds.
- Per file: the eye previews G-code in the 3D view, the arrow downloads the
  file, and the bin deletes it (after asking).
- **Drag files onto the panel**, or press **Upload**, to put them in the folder
  shown. An existing file is only replaced after you confirm.

### G-code, Console and Settings (right, bottom)

**G-code.** The line being run is highlighted and kept in view. Scroll to look
around; **Follow line** jumps back to the current line.

**Console.** What happened, with times:

- commands from the dashboard and TNG's answers;
- job started, ended or paused, E-stop, program loaded;
- the bridge connecting or getting lost.

A dot on the tab means new lines arrived (red if one is an error).

- The input at the bottom sends one G-code line to TNG's MDI, as if you typed
  it in TNG. Enter sends it; ↑/↓ recall earlier lines. It's refused while a
  program runs or the machine moves. **A line like `G0 Z10` moves the
  machine:** only send one when someone is at the machine.
- **Clear** empties this view only. The server keeps the last 500 lines.

**Settings.**

- **Name:** the machine's name, shown in the top bar and the browser tab, for
  everyone.
- **Camera:** **Automatic** (the first camera found), a specific camera, or
  **Off**. The change applies right away.
  - USB cameras are listed by their stable name, so the choice still works
    when the camera is plugged into another port.
  - **Rescan** looks again after you plug a camera in.
- **Software:** the installed version and updates, see [Updating](#updating).
- **Logs:** see [Logs and debugging](#logs-and-debugging).

Settings are saved in `settings.json` next to `server.py`. Updates keep them.

## Updating

**From the dashboard:** open **Settings → Software**.

- It shows the installed version and what is on GitHub. GitHub is checked
  every 6 hours; **Check for updates** checks now.
- **Install update** installs the new version:
  - the server restarts and the page reloads itself;
  - the bridge inside TNG notices its new file and reloads itself within a few
    seconds;
  - TNG and the machine keep running.
- **Roll back** returns to the version installed before the last update.
- Updating is refused while a program runs.
- If the section says updates are unavailable, it says why (git missing, or
  the folder isn't a git clone).

**Over SSH:**

```bash
~/PlanetCNC_webapp/update.sh             # latest version
~/PlanetCNC_webapp/update.sh rollback    # the version before the last update
sudo systemctl restart planetcnc-web     # then restart the server
```

Notes:

- Files changed by hand in `~/PlanetCNC_webapp` block updates, because git
  would overwrite them. `update.sh` names them. `git checkout -- <file>`
  undoes a change.
- `bridge_autostart.py` is only read when TNG starts. When an update changes
  it, restart TNG once at a convenient moment.
- An update applied with a plain `git pull` doesn't copy the bridge into TNG.
  Use `update.sh`, which does both.

## Logs and debugging

The server keeps a log of everything worth knowing afterwards, so a problem
can be looked at after it happened.

### What gets logged

| Source (in the log) | What |
|---|---|
| `pcnc` | Server start and stop with the version, settings changes, log cleanup. |
| `pcnc.bridge` | The bridge connecting, pauses in its updates, programs received, every command with its result and how long it took. |
| `pcnc.bridge` `[TNG]` | Lines written by the bridge inside TNG: its start, errors with their traceback, slow update rounds. |
| `pcnc.machine` | Everything in the Console tab: jobs, pauses, E-stop, tool changes, commands. |
| `pcnc.http` | Dashboards connecting and disconnecting (with device and duration), slow requests, errors with their traceback. |
| `pcnc.browser` | Errors and connection problems in a dashboard, sent by the page itself, with the device's address and browser. |
| `pcnc.camera` | ffmpeg starting and stopping (with the reason), camera errors, viewers. |
| `pcnc.system` | A status line every 5 minutes (bridge rate, job line, open dashboards, server memory, CPU, temperature), and warnings for under-voltage, throttling, heat, a full disk or memory. |
| `pcnc.update` | Update checks and installs with git's output. |
| `pcnc.files` | Uploads and deletions. |

The bridge keeps up to 300 lines while it can't reach the server and sends
them once the connection is back. Its log covers server outages too.

Example:

```
2026-11-05 18:52:27.104 INFO    pcnc.machine   Job started: //home/mekanika/Desktop/kumiko v6.nc
2026-11-05 18:57:27.880 INFO    pcnc.system    status: bridge online (5.0 updates/s), job line 9779/114404 (9 %), 5:27:27 left, 2 dashboard(s), 1 camera viewer(s), 31 threads, server memory 38 MB, CPU 9 %, 37 °C
2026-11-05 19:02:41.552 WARNING pcnc.browser   [10.0.0.31 Chrome Android] live connection lost: no update for 6 s; last update 6.2 s ago
2026-11-05 19:02:41.560 INFO    pcnc.browser   [10.0.0.31 Chrome Android] live connection back after 1 s
```

### Where to read it

- **In the dashboard:** **Settings → Logs.** **Show recent** shows the end of
  the log. Choose "Warnings and errors" to see only problems. Each log file
  can be downloaded from there.
- **On the Pi:** `~/PlanetCNC_webapp/logs/server.log`, for example
  `tail -f ~/PlanetCNC_webapp/logs/server.log`.
- **The systemd journal** gets the same lines, except the detailed ones:
  `journalctl -u planetcnc-web -f`.
- **TNG's output window** shows the bridge's warnings and errors.

### Detailed logging

**Settings → Logs → Detailed logging** adds every request, every command
handed to the bridge, camera viewers and more. Use it while chasing a problem,
then turn it off again. It's remembered across restarts. `--debug` switches it
on from the command line.

### Automatic cleanup

The logs can't fill up the Pi:

- `server.log` is split into pieces: when it reaches a tenth of the size limit
  it becomes `server.log.1`, and so on. At most 10 pieces exist.
- Once an hour (and at startup) the server deletes log files older than 30
  days, and the oldest files while the folder is over its size limit (50 MB by
  default).
- If the disk has less than 500 MB free, only the current file is kept.

Change the limits with `--log-max-mb` and `--log-keep-days`, or move the logs
with `--log-dir`. The systemd journal has its own limits (by default at most
10 % of the disk). `journalctl --disk-usage` shows its size.

### Debugging a problem: where to start

1. Note the time it happened.
2. **Settings → Logs → Show recent**, "Warnings and errors", and look around
   that time.
3. For more context, download `server.log` and search for that time. The
   status line every 5 minutes shows the state before and after.
4. If it can happen again, turn on **Detailed logging** and wait for it.

## Server options

Add them to the `ExecStart` line of the service, or after
`python3 server.py` by hand.

| Option | Default | What it does |
|---|---|---|
| `--port 8090` | 8080 | Port of the dashboard. |
| `--host 127.0.0.1` | 0.0.0.0 | Address to listen on (0.0.0.0 = every network). |
| `--token secret` | none | The bridge must send this token; set `TOKEN` in the bridge to the same. Needed only when the bridge runs on another computer. |
| `--camera /dev/video1` | auto | Camera to use: a device, `auto` (the first camera found) or `none`. A camera chosen in Settings wins. |
| `--camera-size 640x480` | 1280x720 | Camera resolution. |
| `--camera-fps 10` | 15 | Camera frame rate. |
| `--camera-cmd "..."` | none | Any command that writes MJPEG to stdout, e.g. `rpicam-vid -t 0 -n --codec mjpeg -o -` for a Pi camera module. Replaces `--camera`. |
| `--files-dir ~/gcode` | ~/Desktop | Folder shown in the Files panel; `none` hides the panel. |
| `--line-offset -1` | 0 | Corrects the highlighted G-code line if it is always one off. |
| `--log-dir /path` | logs/ next to server.py | Log folder; `none` logs to the console (journal) only. |
| `--log-max-mb 100` | 50 | Most disk space the log files may use. |
| `--log-keep-days 14` | 30 | Delete log files older than this. |
| `--debug` | off | Detailed logging from the start. |
| `--demo` | off | Simulated machine, for trying the dashboard without TNG. |

Bridge settings (`SERVER_URL`, `TOKEN`, update interval) are at the top of
`bridge/planetcnc_bridge.py`. `SERVER_URL` stays `http://127.0.0.1:8080` as
long as the bridge and the server run on the same Pi.

## Inside the bridge

The bridge is deliberately generic, so new dashboard features normally don't
need a new bridge:

- **Reading.** The server sends the bridge a list of TNG parameters and
  functions to read (`BRIDGE_SPEC` in `server.py`). The bridge sends the raw
  values; the server turns them into what the dashboard shows.
- **Commands.** The server sends commands from a small fixed set:
  - call an allowed `planetcnc` function (reading functions, plus `stop`,
    `pause`, `start` and a few others);
  - run a TNG command by name;
  - load G-code.

  A command can require that no program runs, or that the machine is idle. The
  bridge checks that right before running it, with TNG's state at that moment.
- **Self-reload.** When `planetcnc_bridge.py` changes on disk (after an
  update), the bridge loads the new version within a few seconds. A new
  version that doesn't compile is ignored, and the running one keeps going.
- **Crash restart.** If the bridge crashes, `bridge_autostart.py` shows the
  error in TNG's output window and starts it again after 10 seconds. The
  restarted bridge sends the error to the server's log.

The TNG command names the dashboard uses are at the top of `server.py`
(`CMD_HOME`, `CMD_MDI`, `CMD_OPEN_CODE`). To see TNG's values and commands
yourself (reading only):

```bash
curl http://10.0.0.14:8080/api/tng/commands
curl 'http://10.0.0.14:8080/api/tng/call?f=param_get&args=["_hw_version"]'
curl 'http://10.0.0.14:8080/api/tng/call?f=hw_motor'
```

## Troubleshooting

Look at **Settings → Logs** first: most problems leave a warning there.

| Symptom | What to check |
|---|---|
| Status says `OFFLINE`, but the server answers | The bridge isn't sending. TNG's output window gives the reason: no bridge message at all means it never started (check **Settings → Python Library** in TNG, then restart TNG); "rejected" means `TOKEN` and `--token` differ; "cannot reach" means the server isn't running. |
| `OFFLINE` on one device while others are fine | That page lost its connection. It reconnects by itself within a few seconds; the log shows `live connection lost` / `back` lines for that device. If it doesn't come back, reload the page. |
| A dashboard tab won't load or stays `OFFLINE` | Browsers allow 6 connections to one server, and each dashboard tab uses 2 (updates and camera). Close dashboard tabs you don't need. |
| Page doesn't open at all | Is the server running (`systemctl status planetcnc-web`)? Is the address right? `journalctl -u planetcnc-web -n 50` shows why it stopped. |
| The 3D view stays empty | The device needs internet access to load three.js. |
| A button says "TNG has no command …" | List TNG's command names with `/api/tng/commands` (see [Inside the bridge](#inside-the-bridge)) and set the right one at the top of `server.py`. |
| Updates say files were changed locally | Something in `~/PlanetCNC_webapp` was edited by hand. `cd ~/PlanetCNC_webapp && git status` shows what; `git checkout -- <file>` undoes it. |
| No bed grid, only a small grid around zero | TNG's motor limits aren't set (Settings → Motors → Limits). |
| Camera shows "No camera signal" | Check `ls /dev/video*` and the `pcnc.camera` lines in the log (they include ffmpeg's error). `v4l2-ctl --list-formats-ext` (package `v4l-utils`) lists the sizes the camera supports; set one with `--camera-size`. |
| No camera panel at all | ffmpeg isn't installed (Settings → Camera says so), Camera is set to **Off**, or no camera is plugged in. After plugging one in, press **Rescan**. |
| Highlighted line is always one off | Start the server with `--line-offset 1` or `--line-offset -1`. |
| The Pi acts up at random | Look for `under-voltage` in the log or the Pi panel: use a stronger power supply. |

## Uninstalling

```bash
sudo systemctl disable --now planetcnc-web
sudo rm /etc/systemd/system/planetcnc-web.service
rm ~/PlanetCNC/Profiles/Default/Expr/Expr_WebMonitor.txt
rm ~/PlanetCNC/Profiles/Default/Scripts/planetcnc_bridge.py ~/PlanetCNC/Profiles/Default/Scripts/bridge_autostart.py
rm -rf ~/PlanetCNC_webapp
```

Then restart TNG.
