# PlanetCNC web monitor

A dashboard for the CNC: live position, job progress, a 3D view of the machine
bed and the job's toolpath, the G-code being run, and the USB camera. You can
preview a G-code file before running it, load it into TNG, home or stop the
machine. Open it from any computer or phone on the home network.

There is no password: anyone on the home network who opens the page can use it.
There is no jogging either: moving the machine by hand needs someone standing
next to it.

```
PlanetCNC TNG ──(bridge script)──> server.py ──> browser on your network
                                       ^
                                  USB camera (ffmpeg)
```

Everything below runs on the Raspberry Pi (10.0.0.14). You only use a browser on
other devices.

## Files

| File | What it is |
|---|---|
| `server.py` | The web server. Run it with Python 3; no extra packages needed. |
| `bridge/planetcnc_bridge.py` | Runs inside TNG: reads what the server asks for and runs the dashboard's commands. |
| `bridge/bridge_autostart.py`, `bridge/Expr_WebMonitor.txt` | Start the bridge when TNG starts. |
| `update.sh` | Gets the latest version from GitHub and installs the bridge into TNG. |
| `web/` | The dashboard page. The server serves it; nothing to do with it. |

## One-time setup

1. **Install git and ffmpeg** (ffmpeg is needed for the camera):
   ```bash
   sudo apt install git ffmpeg
   ```
2. **Get the app** from GitHub:
   ```bash
   git clone https://github.com/Benoit-LdL/PlanetCNC_webapp ~/PlanetCNC_webapp
   ```
   Keep it a git clone: the dashboard's update button and `update.sh` rely on it.
3. **Enable Python in TNG.** Find the Python library:
   ```bash
   ls /usr/local/lib/libpython3* /usr/lib/*/libpython3*
   ```
   If nothing is listed, run `sudo apt install libpython3-dev` and look again.
   In TNG, go to **Settings → Python Library**, enter the full path of the
   `libpython3.x.so...` file, press **Check** (it should show the Python
   version), then restart TNG.
4. **Install the bridge into TNG's profile** (`~/PlanetCNC/Profiles/Default`;
   set `PCNC_PROFILE=/other/profile` in front of the command for another one):
   ```bash
   ~/PlanetCNC_webapp/update.sh
   ```
   This also installs the autostart files (see
   [Start the bridge when TNG starts](#start-the-bridge-when-tng-starts)).
   Restart TNG once afterwards.

The examples use `/home/mekanika/PlanetCNC_webapp` for the web app folder.

## Updating

Open the **Settings** tab (right, next to G-code and Console). Under
**Software** it shows the installed version and what is on GitHub (checked
every 6 hours, or press **Check for updates**). **Install update** installs
it: the server restarts and the page reloads itself, and the bridge inside TNG
reloads itself within a few seconds. TNG and the machine keep running. **Roll
back** returns to the version before the last update. Updating is refused while
a program runs. When an update is waiting, the version button in the top bar
says so and a dot appears on the Settings tab; the button opens the tab.

If the Software section says updates are unavailable, it names the reason
(git missing, or the folder isn't a git clone).

The same over SSH:
```bash
~/PlanetCNC_webapp/update.sh             # latest version
~/PlanetCNC_webapp/update.sh rollback    # previous version
sudo systemctl restart planetcnc-web     # then restart the server
```
Files changed by hand in `~/PlanetCNC_webapp` block updates (git would
overwrite them); `update.sh` names them.

## Starting it

1. **Start the server** (in a terminal on the Pi, or over SSH):
   ```bash
   cd /home/mekanika/PlanetCNC_webapp
   python3 server.py
   ```
   It prints `PlanetCNC web monitor on http://10.0.0.14:8080` and, if a camera
   is found, `Camera: /dev/video0 ...`.
2. **Start the bridge in TNG:** **File → Run Python Script**, then pick
   `planetcnc_bridge.py`. TNG's output window should show
   `PlanetCNC web bridge: connected to http://127.0.0.1:8080`, and the server
   terminal `Bridge connected from 127.0.0.1`. To have TNG start it by itself,
   see [Start the bridge when TNG starts](#start-the-bridge-when-tng-starts).
3. **Open the dashboard** on any device on the network:
   **http://10.0.0.14:8080**

The server keeps running only as long as its terminal does. To start it
automatically at boot instead, see [Start the server at boot](#start-the-server-at-boot).

## Using the dashboard

**Top bar.** The loaded file, and the machine status: `RUNNING` (green),
`PAUSED` (yellow), `E-STOP` (red, blinking), `IDLE`, or `OFFLINE` when the
server gets no updates from the bridge. The thin bar below shows job progress.

**Left panel.**
- Position of each axis. Switch between **Work** and **Machine** coordinates
  with the tabs. A, B and C only appear if they are used.
- Feed, spindle speed, tool and coordinate system.
- Current line, progress, estimated time remaining and the time of the last
  update.

**Machine (left panel, bottom).**
- **Home** asks for confirmation, then homes all axes.
- **Stop** stops the machine, also during a job.
- Homing and loading are refused while a program runs. The line below the
  buttons shows TNG's answer or the reason a command was refused.

**Raspberry Pi (left panel, bottom).** CPU, RAM, temperature and disk use of
the Pi, refreshed every 2 seconds, plus uptime. A bar turns amber or red with a
word (busy, high, warm, hot, full) when a value gets high; the Pi slows itself
down from about 80 °C. Next to the title, the power status from the Pi's
firmware: **Under-voltage!** means the power supply is too weak right now;
"since boot" means it happened earlier. A weak supply is a common cause of
random Pi problems.

**3D view (middle).**
- The grid and box are the machine's real travel, from TNG's motor limits
  (Settings → Motors → Limits), placed relative to your current work zero. The
  red/green/blue arrows mark work zero.
- The job is drawn as its toolpath. Cutting moves are shaded by depth: light
  at the top, dark blue at the deepest cut. Rapids are dark red. Big 3D jobs
  are simplified so they stay smooth: points that lie within a small distance
  of a straight line are dropped. The note under the view says how many moves
  are drawn and how far the drawing may be from the real path (e.g. "10k of
  480k moves drawn (within 0.012 mm)"). The thin box around the job covers
  every move.
- **Open file** previews a G-code file from your computer: the 3D view and
  G-code list show that file, so you can check its origin and orientation
  against the bed before running it. TNG is not touched. **Load in TNG** makes
  it TNG's loaded program (it does not start it); **Close preview** goes back
  to TNG's program.
- **3D / Top / Front / Right** switch the camera angle.
- **Fit** zooms to the bed and the job.
- **Follow** keeps the tool in the center.
- **Trace** shows (yellow) where the tool has actually moved.
- Mouse: left-drag rotates (3D view only), right-drag pans, wheel zooms. On a
  phone or tablet: one finger rotates, two fingers zoom and pan.

**Camera (right, top).**
- The green dot means the camera is live; red means no signal.
- **Full** (or double-click the picture) shows it fullscreen.
- **Hide** folds it away; this is remembered per browser.
- The Pi only runs the camera while someone has the dashboard open, and the
  stream pauses when the browser tab is in the background.

**Files (right, middle).** A folder on the Pi: your Desktop by default (see
`--files-dir`).
- Click a folder to open it, and the path at the top to go back. The list
  refreshes by itself every 15 seconds.
- Per file: the eye opens a G-code file in the 3D view (like **Open file**),
  the arrow downloads it, the bin deletes it (asks first).
- **Drag files onto the panel** to upload them to the folder shown: it lights
  up while you hover, shows a progress bar, then a green check. **Upload**
  does the same through a file picker. An existing file is only replaced
  after you confirm.
- **Hide** folds the panel away; this is remembered per browser.

**Console (right, bottom, second tab).** A log of what happened, with times:
commands from the dashboard and TNG's answers, job started/ended, paused,
E-stop, program loaded, bridge connected/lost. A dot on the tab means new
lines arrived while you were on the G-code tab (red if one is an error).
- The line at the bottom sends one G-code line to TNG's MDI, like typing it in
  TNG (Enter to send, ↑/↓ for earlier commands). Refused while a program
  runs or the machine moves. A line like `G0 Z10` moves the machine, so only
  send one when someone is at the machine.
- **Clear** empties this view only; the server keeps the last 500 lines, so a
  reload shows them again.

**G-code (right, bottom).** The line being run is highlighted and kept in view.
Scroll freely to look around; press **Follow line** to jump back to the
current line.

**Settings (right, bottom, third tab).**
- **Name**: the machine's name, shown in the top bar and the browser tab, for
  everyone who opens the dashboard.
- **Camera**: which camera to show: **Automatic** (the first one found),
  a specific camera, or **Off**. The change applies right away. USB cameras
  are listed by their stable name, so the choice survives plugging the camera
  into another port. **Rescan** looks again after you plug one in.
- **Software**: updates, see [Updating](#updating).

Settings are saved in `settings.json` next to `server.py` (not part of git,
so updates keep them).

The page adapts to tablets and phones.

## Server options

```bash
python3 server.py --port 8090                 # different port
python3 server.py --token secret              # bridge must send this token (set TOKEN in the bridge too)
python3 server.py --camera /dev/video1        # other camera (a camera chosen in Settings wins)
python3 server.py --camera-size 640x480 --camera-fps 10   # lighter stream
python3 server.py --camera none               # no camera
python3 server.py --files-dir ~/gcode         # folder for "Pi files" (default ~/Desktop; 'none' turns it off)
python3 server.py --line-offset -1            # if the highlighted G-code line is always one off
python3 server.py --demo                      # simulated machine, for trying the page without TNG
```

Bridge settings (`SERVER_URL`, `TOKEN`, update rate) are at the top of
`bridge/planetcnc_bridge.py`. `SERVER_URL` stays `http://127.0.0.1:8080`,
because the bridge and the server run on the same Pi.

## TNG command names

The TNG command names the dashboard uses are set at the top of `server.py`
(`CMD_HOME`, `CMD_MDI`, `CMD_OPEN_CODE`).

## How the bridge works

The bridge is generic, so new dashboard features normally don't need a new
bridge:

- **Reading:** the server sends the bridge a list of TNG parameters and
  functions to read (`BRIDGE_SPEC` in `server.py`). The bridge sends the raw
  values 5 times a second; the server turns them into what the dashboard shows.
- **Commands:** the server sends commands from a small fixed set: call an
  allowed `planetcnc` function, run a TNG command by name, a relative move,
  load G-code. Commands can require that no program runs or that the machine
  is idle; the bridge checks that right before running them, with TNG's
  current state.
- **Self-reload:** when `planetcnc_bridge.py` changes on disk (e.g. after an
  update), the bridge loads the new version within a few seconds. A version
  that doesn't compile is ignored and the running one keeps going.

To look at TNG's values yourself (reading only):

```bash
curl http://10.0.0.14:8080/api/tng/commands
curl 'http://10.0.0.14:8080/api/tng/call?f=param_get&args=["_hw_version"]'
curl 'http://10.0.0.14:8080/api/tng/call?f=hw_motor'
```

## Start the server at boot

Create `/etc/systemd/system/planetcnc-web.service` (change the paths
if you put the web app elsewhere):

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

Then:

```bash
sudo usermod -aG video mekanika               # let that user read the camera
sudo systemctl daemon-reload
sudo systemctl enable --now planetcnc-web
journalctl -u planetcnc-web -f            # see the server's messages
```

## Start the bridge when TNG starts

TNG runs the `#OnInit` event from the `Expr*.txt` files in its profile's
`Expr` folder each time it starts. `bridge/Expr_WebMonitor.txt` uses it to
start the bridge through `bridge/bridge_autostart.py`. That launcher runs the
bridge in the background, so TNG doesn't wait for it. Both files contain
the paths for this Pi's profile, `/home/mekanika/PlanetCNC/Profiles/Default`.

`update.sh` copies them into place: the bridge files into the profile's
`Scripts` folder, `Expr_WebMonitor.txt` into its `Expr` folder. The separate
`Expr_WebMonitor.txt` leaves Mekanika's own `Expr.txt` untouched. After the
first install, restart TNG (or the Pi); TNG's output window then shows
`PlanetCNC web bridge: connected ...`.

To undo it, delete `Expr/Expr_WebMonitor.txt` and restart TNG.

## Troubleshooting

| Symptom | What to check |
|---|---|
| A control button says "TNG has no command …" | List TNG's command names with `/api/tng/commands` (see [How the bridge works](#how-the-bridge-works)) and set the right one at the top of `server.py`. |
| Update button says files were changed locally | Something in `~/PlanetCNC_webapp` was edited by hand. `cd ~/PlanetCNC_webapp && git status` shows what; `git checkout -- <file>` undoes it. |
| No bed grid, only a small grid around zero | TNG's motor limits aren't set (Settings → Motors → Limits). |
| Status says `OFFLINE` | The bridge isn't sending. Look at TNG's output window: no bridge message at all means it never started (check **Settings → Python Library**, then start it again); "rejected the token" means `TOKEN` and `--token` differ; "cannot reach" means the server isn't running. |
| Page doesn't open | Is the server running? Is the address `http://10.0.0.14:8080`? |
| Camera shows "No camera signal" | Check `ls /dev/video*`; run `v4l2-ctl --list-formats-ext` (package `v4l-utils`) and try a size the camera supports with `--camera-size`. The server terminal prints the ffmpeg error. |
| No camera panel at all | ffmpeg isn't installed (Settings → Camera says so), Camera is set to **Off**, or no camera is plugged in. After plugging one in, press **Rescan** in Settings. |
| A 4th dashboard tab won't load | Browsers allow 6 connections per server, and each tab uses 2. Close a tab. |
| Highlighted line is one off | Start the server with `--line-offset 1` or `--line-offset -1`. |
