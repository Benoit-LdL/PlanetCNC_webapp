# PlanetCNC web monitor

A dashboard for the CNC: live position, job progress, a 3D view of the machine
bed and toolpath, the G-code being run, and the USB camera. You can preview a
G-code file before running it, load it into TNG, home the machine and jog it in
steps. Open it from any computer or phone on the home network. Machine control
only works when the dashboard has a password.

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
| `bridge/planetcnc_bridge.py` | Runs inside TNG: sends the machine state to the server and runs the dashboard's commands. |
| `bridge/bridge_autostart.py`, `bridge/Expr_WebMonitor.txt` | Start the bridge when TNG starts. |
| `web/` | The dashboard page. The server serves it; nothing to do with it. |

## One-time setup

1. **Install ffmpeg** (needed for the camera):
   ```bash
   sudo apt install ffmpeg
   ```
2. **Enable Python in TNG.** Find the Python library:
   ```bash
   ls /usr/local/lib/libpython3* /usr/lib/*/libpython3*
   ```
   If nothing is listed, run `sudo apt install libpython3-dev` and look again.
   In TNG, go to **Settings → Python Library**, enter the full path of the
   `libpython3.x.so...` file, press **Check** (it should show the Python
   version), then restart TNG.
3. **Copy the bridge into TNG's profile.** Put `bridge/planetcnc_bridge.py` in
   `/home/mekanika/PlanetCNC/Profiles/Default/Scripts/`.

The examples use `/home/mekanika/PlanetCNC_webapp` for the web app folder;
change it if you copied the files somewhere else.

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

**Machine control (left panel, bottom).** Needs a dashboard password (see
[Password and machine control](#password-and-machine-control)).
- Press **Unlock** first. The controls lock again after 2 minutes without use.
- **X− / X+ / Y− / Y+ / Z+ / Z−** move one step of the chosen size (0.1, 1 or
  10 mm). A step is only accepted while the machine stands still, so clicking
  fast doesn't queue up moves.
- **Home** asks for confirmation, then homes all axes.
- **Stop** stops the machine. It works even while locked and during a job.
- Moving, homing and loading are refused while a program runs. The line below
  the buttons shows TNG's answer or the reason a command was refused.

**3D view (middle).**
- The grid and box are the machine's real travel, from TNG's motor limits
  (Settings → Motors → Limits), placed relative to your current work zero. The
  red/green/blue arrows mark work zero.
- **Open file** previews a G-code file from your computer: the 3D view and
  G-code list show that file, so you can check its origin and orientation
  against the bed before running it. TNG is not touched. **Load in TNG** makes
  it TNG's loaded program (it does not start it); **Close preview** goes back
  to TNG's program.
- **3D / Top / Front / Right** switch the camera angle.
- **Fit** zooms to the bed and the toolpath.
- **Follow** keeps the tool in the center.
- **Trace** shows (yellow) where the tool has actually moved.
- Colors: blue = still to cut, green = done, orange = rapid moves.
- Mouse: left-drag rotates (3D view only), right-drag pans, wheel zooms. On a
  phone or tablet: one finger rotates, two fingers zoom and pan.

**Camera (right, top).**
- The green dot means the camera is live; red means no signal.
- **Full** (or double-click the picture) shows it fullscreen.
- **Hide** folds it away; this is remembered per browser.
- The Pi only runs the camera while someone has the dashboard open, and the
  stream pauses when the browser tab is in the background.

**G-code (right, bottom).** The line being run is highlighted and kept in view.
Scroll freely to look around; press **Follow line** to jump back to the
current line.

The page adapts to tablets and phones.

## Server options

```bash
python3 server.py --port 8090                 # different port
python3 server.py --viewer-password secret    # ask for a password (any username)
python3 server.py --token secret              # bridge must send this token (set TOKEN in the bridge too)
python3 server.py --camera /dev/video1        # other camera
python3 server.py --camera-size 640x480 --camera-fps 10   # lighter stream
python3 server.py --camera none               # no camera
python3 server.py --demo                      # simulated machine, for trying the page without TNG
```

Bridge settings (`SERVER_URL`, `TOKEN`, update rate, line offset) are at the top
of `bridge/planetcnc_bridge.py`. `SERVER_URL` stays `http://127.0.0.1:8080`,
because the bridge and the server run on the same Pi.

## Password and machine control

The password is asked by the browser once (any username) and protects the whole
dashboard, including the camera. Keep it out of the service file, which every
user on the Pi can read:

```bash
echo 'PCNC_VIEWER_PASSWORD=choose-a-password' | sudo tee /etc/planetcnc-web.env
sudo chmod 600 /etc/planetcnc-web.env
```

Add `EnvironmentFile=/etc/planetcnc-web.env` under `[Service]` in the service
file below, then `sudo systemctl daemon-reload && sudo systemctl restart planetcnc-web`.

The bridge runs the commands with TNG's command names, set at the top of
`planetcnc_bridge.py` (`CMD_HOME`, `CMD_MDI`, `CMD_OPEN_CODE`). When the bridge
starts, it writes every command name your TNG knows to `~/tng_commands.txt`. If
a button says TNG has no such command, look up the right name there:

```bash
grep -i -E "home|mdi|open_code" ~/tng_commands.txt
```

Jog steps are sent as `G53 G0 <axis><target>` through the MDI command: an
absolute move in machine coordinates, which can't leave TNG in relative (G91)
mode afterwards.

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
bridge in the background, so TNG doesn't wait for it. Both files already
contain the paths for this Pi's profile, `/home/mekanika/PlanetCNC/Profiles/Default`.

1. Copy the files from the web app folder:
   ```bash
   P=/home/mekanika/PlanetCNC/Profiles/Default
   mkdir -p $P/Scripts
   cp bridge/planetcnc_bridge.py bridge/bridge_autostart.py $P/Scripts/
   cp bridge/Expr_WebMonitor.txt $P/Expr/
   ```
   The separate `Expr_WebMonitor.txt` leaves Mekanika's own `Expr.txt`
   untouched.
2. Restart TNG (or the Pi). TNG's output window shows
   `PlanetCNC web bridge: connected ...`.

To undo it, delete `Expr/Expr_WebMonitor.txt` and restart TNG.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Machine control says "off" | The server has no password: see [Password and machine control](#password-and-machine-control). |
| A control button says "TNG has no command …" | Set the right name in `planetcnc_bridge.py` from `~/tng_commands.txt`, copy it to TNG's `Scripts` folder and restart TNG. |
| No bed grid, only a small grid around zero | TNG's motor limits aren't set (Settings → Motors → Limits). |
| Status says `OFFLINE` | The bridge isn't sending. Look at TNG's output window: no bridge message at all means it never started (check **Settings → Python Library**, then start it again); "rejected the token" means `TOKEN` and `--token` differ; "cannot reach" means the server isn't running. |
| Page doesn't open | Is the server running? Is the address `http://10.0.0.14:8080`? |
| Camera shows "No camera signal" | Check `ls /dev/video*`; run `v4l2-ctl --list-formats-ext` (package `v4l-utils`) and try a size the camera supports with `--camera-size`. The server terminal prints the ffmpeg error. |
| No camera panel at all | ffmpeg isn't installed, or no `/dev/video0` existed when the server started. Restart the server after plugging in the camera. |
| A 4th dashboard tab won't load | Browsers allow 6 connections per server, and each tab uses 2. Close a tab. |
| Highlighted line is one off | Set `LINE_OFFSET` in the bridge to `1` or `-1`. |
