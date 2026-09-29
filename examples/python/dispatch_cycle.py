"""One operator cycle in Manual Block mode: wait for a train, open gates + restraints, board, close, dispatch.

    python dispatch_cycle.py [--host H] [--port P] [--coaster NAME] [--station NAME] [--board SECONDS]

Leaves the station in manual dispatch; run with --release to hand it back to the game (auto dispatch + auto mode).
"""
import argparse, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "client"))
from nl2bridge import NL2Bridge, NL2Error

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=15152)
ap.add_argument("--coaster", default="0")
ap.add_argument("--station", default="0")
ap.add_argument("--board", type=float, default=8.0)
ap.add_argument("--release", action="store_true")
a = ap.parse_args()


def retry(fn, *args, tries=40, wait=0.5):
    """Mode changes are refused while trains are moving between blocks - keep trying."""
    for _ in range(tries):
        try:
            return fn(*args)
        except NL2Error as e:
            last = e
            time.sleep(wait)
    raise last


with NL2Bridge(a.host, a.port) as nl:
    c = nl.coaster(int(a.coaster) if a.coaster.isdigit() else a.coaster)
    st = nl.station_index(c, int(a.station) if a.station.isdigit() else a.station)
    if a.release:
        nl.set_manual_dispatch(c, st, False)
        retry(nl.set_block_mode, c, "auto")
        print("station and coaster handed back to the game")
        sys.exit()

    retry(nl.set_block_mode, c, "manual")
    nl.set_manual_dispatch(c, st, True)
    print("waiting for a train in the station...")
    while not nl.station_status(c, st)["trainReady"]:
        time.sleep(0.2)

    nl.open_gates(c, st)
    nl.open_restraints(c, st)
    print(f"boarding for {a.board:.0f} s")
    time.sleep(a.board)
    nl.close_restraints(c, st)
    nl.close_gates(c, st)
    while not nl.station_status(c, st)["canDispatch"]:
        time.sleep(0.2)
    nl.dispatch(c, st)
    print("dispatched")
