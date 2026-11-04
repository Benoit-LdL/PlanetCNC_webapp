"""Start the web monitor bridge in the background when TNG starts.

Called from TNG's #OnInit event (Profiles/Default/Expr/Expr_WebMonitor.txt):

    #OnInit
    py("/home/mekanika/PlanetCNC/Profiles/Default/Scripts/bridge_autostart.py", 0);

The bridge loops forever, so it gets its own thread here: py() returns
immediately and TNG keeps starting up. The 0 runs it in TNG's main Python
interpreter, which lives as long as TNG, so the thread keeps running.
"""

import os
import runpy
import threading

import planetcnc

# Full path of planetcnc_bridge.py on the Pi
BRIDGE_PATH = "/home/mekanika/PlanetCNC/Profiles/Default/Scripts/planetcnc_bridge.py"
THREAD_NAME = "planetcnc-web-bridge"

if any(t.name == THREAD_NAME for t in threading.enumerate()):
    planetcnc.print("PlanetCNC web bridge: already running")
elif not os.path.isfile(BRIDGE_PATH):
    planetcnc.print("PlanetCNC web bridge: not found at " + BRIDGE_PATH + " - fix BRIDGE_PATH in bridge_autostart.py")
else:
    threading.Thread(target=runpy.run_path, args=(BRIDGE_PATH,), name=THREAD_NAME, daemon=True).start()
