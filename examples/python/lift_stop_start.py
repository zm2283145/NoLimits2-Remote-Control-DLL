"""Stop and restart a lift chain with the lift speed, in any block mode (also Auto).

    python lift_stop_start.py [--host H] [--port P] [--coaster NAME] [--lift "Lift"] [--hold 10]

The chain ramps down with its own deceleration and the train on it holds; then it ramps back up.
The design speed is read first and always restored, even on Ctrl+C.
"""
import argparse, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "client"))
from nl2bridge import NL2Bridge

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=15152)
ap.add_argument("--coaster", default="0")
ap.add_argument("--lift", default=None, help="lift section name (default: the first section with a lift)")
ap.add_argument("--hold", type=float, default=10.0, help="seconds to keep the lift stopped")
a = ap.parse_args()

with NL2Bridge(a.host, a.port) as nl:
    assert nl.bridge_info()["api"] >= 6, "needs NL2Bridge API 6"
    c = nl.coaster(int(a.coaster) if a.coaster.isdigit() else a.coaster)
    lift = nl.block_id(c, a.lift) if a.lift else next(b["id"] for b in nl.blocks(c) if b["hasLift"])
    design = nl.device_params(c, lift)["lift"]["speed"]
    if design <= 0:
        sys.exit("the lift is already at speed 0 - reload the park to get its design speed back")

    def show(tag, secs):
        for _ in range(int(secs * 2)):
            p = nl.device_params(c, lift)["lift"]
            print(f"{tag}: chain {p['current']:.2f} m/s (target speed {p['speed']:.2f})")
            time.sleep(0.5)

    try:
        nl.set_device_params(c, lift, "lift", speed=0)
        show("LIFT STOP ", a.hold)
    finally:
        nl.set_device_params(c, lift, "lift", speed=design)
    show("LIFT START", 3)
