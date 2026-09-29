"""Live monitor: block states, trains and the station, 5 times a second. Read-only - safe to run any time.

    python monitor.py [--host 127.0.0.1] [--port 15152] [--coaster "Fury 325"]
"""
import argparse, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "client"))
from nl2bridge import NL2Bridge

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=15152)
ap.add_argument("--coaster", default="0")
a = ap.parse_args()

with NL2Bridge(a.host, a.port) as nl:
    print(nl.bridge_info())
    c = nl.coaster(int(a.coaster) if a.coaster.isdigit() else a.coaster)
    names = {b["id"]: b["name"] for b in nl.blocks(c)}
    while True:
        mode, estop, blocks = nl.block_states(c)
        busy = ", ".join(f"{names[b['id']]}({b['stateName']})" for b in blocks if b["isBlock"] and b["trains"])
        trains = ", ".join(f"T{t['index'] + 1} {t['blockName']} {t['speed']:.1f} m/s" for t in nl.trains(c))
        st = nl.station_states(c)
        station = "train ready" if st and st[0]["trainReady"] else "empty" if st and not st[0]["hasTrain"] else "-"
        print(f"\r{mode:10s} {'E-STOP ' if estop else ''}| {busy} | {trains} | station: {station}".ljust(160)[:160],
              end="", flush=True)
        time.sleep(0.2)
