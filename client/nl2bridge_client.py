#!/usr/bin/env python3
"""NL2Bridge command-line client (uses nl2bridge.py). Names or numbers work for coasters, blocks, stations, switches.

  python nl2bridge_client.py [--port N] <command> ...

  coasters
  info <coaster>                                   # full JSON: sections, stations, special tracks
  blocks <coaster>                                 # block table
  watch <coaster>                                  # live block table
  mode <coaster> auto|manual|fullmanual            # block mode
  estop <coaster> on|off
  brakes <coaster> <block> open|closed|trim
  lift <coaster> <block> fwd|bwd|idle|off
  transport <coaster> <block> off|fwd|bwd|fwdbrake|bwdbrake|launchfwd|launchbwd
  advance <coaster> <block> [fwd|bwd]
  scripted <coaster> <block> advance_fwd_visible|advance_bwd_visible|advance_fwd_enabled|advance_bwd_enabled on|off
  stations <coaster> [watch]
  station <coaster> <station> manual|auto|dispatch|gates_open|gates_close|harness_open|harness_close|
                              platform_raise|platform_lower|flyer_unlock|flyer_lock
          (aliases: open_gates close_gates open_restraints close_restraints raise_floor drop_floor lock_flyer unlock_flyer)
  switches <coaster>
  switch <coaster> <specialTrack> <direction>
  sensors [coaster] [watch]
  raw-set <coaster> <sectionId> <cmd> <param>
  raw-get <coaster> <sectionId> <cmd> [in]
"""
import json, sys, time
from nl2bridge import NL2Bridge, NL2Error, BLOCK_STATES

BRAKE = {0: "open", 1: "closed", 2: "trim", 255: "-"}
LIFT = {0: "off", 1: "fwd", 2: "idle", 3: "bwd", 255: "-"}
TRANSPORT = {0: "off", 1: "on", 2: "dep.brake", 255: "-"}
ALIASES = {"open_gates": "gates_open", "close_gates": "gates_close", "open_restraints": "harness_open",
           "close_restraints": "harness_close", "raise_floor": "platform_raise", "drop_floor": "platform_lower",
           "lock_flyer": "flyer_lock", "unlock_flyer": "flyer_unlock"}


def key(s):
    return int(s) if s.lstrip("-").isdigit() else s


def yn(v):
    return "Y" if v else "-"


def print_stations(nl, c):
    for s in nl.stations(c):
        g, h, p, f = s["gates"], s["harness"], s["platform"], s["flyer"]
        print(f"[{s['index']}] {s['name'][:26]:26s} {'MANUAL' if s['manualDispatch'] else 'auto  '} "
              f"train={yn(s['hasTrain'])} ready={yn(s['trainReady'])} canDispatch={yn(s['canDispatch'])}  "
              f"gates={'open' if g['open'] else 'closed' if g['closed'] else 'moving' if g['present'] else 'n/a'}  "
              f"harness={'open' if h['open'] else 'closed' if h['closed'] else 'moving'}  "
              f"floor={'raised' if p['raised'] else 'lowered' if p['lowered'] else 'moving' if p['present'] else 'n/a'}  "
              f"flyer={'locked' if f['locked'] else 'unlocked' if f['unlocked'] else 'moving' if f['present'] else 'n/a'}")


def main(a):
    port = 15152
    if a[:1] == ["--port"]:
        port, a = int(a[1]), a[2:]
    if not a:
        print(__doc__); return
    cmd, args = a[0], [key(x) for x in a[1:]]
    nl = NL2Bridge(port=port)
    c = nl.coaster(args[0]) if args and cmd != "sensors" else 0
    watch = "watch" in args
    if cmd == "coasters":
        print(json.dumps(nl.coasters(), indent=2))
    elif cmd == "info":
        print(json.dumps(nl.coaster_info(c), indent=2))
    elif cmd in ("blocks", "sections"):
        for s in nl.blocks(c):
            print(f"{s['id']:5d}  {s['name'][:28]:28s} {(s['stateName'] if s['hasNode'] else '-'):24s} lamp={s['lamp']} "
                  f"trains={s['trains']} brake={yn(s['hasBrake'])}:{BRAKE.get(s['brakeMode'], '?'):6s} "
                  f"lift={yn(s['hasLift'])}:{LIFT.get(s['liftMode'], '?'):4s} transport={yn(s['hasTransport'])} "
                  f"{s['nodeType']}{' ' + s['stateText'] if s['stateText'] else ''}")
    elif cmd == "watch":
        names = {s["id"]: s["name"] for s in nl.blocks(c)}
        while True:
            mode, estop, rows = nl.block_states(c)
            print("\x1b[2J\x1b[H" + f"Block mode: {mode}   E-STOP: {'ACTIVE' if estop else 'off'}")
            for r in rows:
                tr = ",".join(str(i + 1) for i in range(32) if r["trainMask"] >> i & 1) or "-"
                print(f"{r['id']:5d} {names.get(r['id'], '')[:24]:24s} {r['stateName']:24s} lamp={r['lamp']} "
                      f"trains[{tr:>5s}] brake={BRAKE.get(r['brakeMode'])} lift={LIFT.get(r['liftMode'])} "
                      f"transport={TRANSPORT.get(r['transportMode'])} adv={'F' if r['canAdvanceFwd'] else ''}{'B' if r['canAdvanceBwd'] else ''}")
            time.sleep(0.1)
    elif cmd == "mode":
        nl.set_block_mode(c, args[1]); print("ok")
    elif cmd == "estop":
        nl.estop(c, args[1] == "on"); print("ok")
    elif cmd == "brakes":
        print(nl.set_brakes(c, args[1], args[2]))
    elif cmd == "lift":
        print(nl.set_lift(c, args[1], args[2]))
    elif cmd == "transport":
        print(nl.set_transport(c, args[1], args[2]))
    elif cmd == "advance":
        print(nl.advance(c, args[1], len(args) > 2 and args[2] == "bwd"))
    elif cmd == "scripted":
        print(nl.scripted_block(c, args[1], args[2], args[3] == "on"))
    elif cmd == "stations":
        while True:
            if watch: print("\x1b[2J\x1b[H", end="")
            print_stations(nl, c)
            if not watch: break
            time.sleep(0.2)
    elif cmd == "station":
        nl.station_op(c, args[1], ALIASES.get(args[2], args[2])); print("ok")
    elif cmd == "switches":
        for s in nl.special_tracks(c):
            print(f"[{s['index']}] {s['name'][:30]:30s} {s['type']:14s} dir {s['current']}/{s['directions']} "
                  f"target={s['target']} moving={yn(s['moving'])} switchable={yn(s['switchable'])}")
    elif cmd == "switch":
        nl.set_switch(c, args[1], args[2]); print("ok")
    elif cmd == "sensors":
        only = nl.coaster(args[0]) if args and args[0] != "watch" else -1
        while True:
            rows = nl.sensors(only)
            print(("\x1b[2J\x1b[H" if watch else "") + f"{len(rows)} sensors")
            for r in rows:
                dot = "\x1b[92m●\x1b[0m" if r["active"] else "○"
                tr = ",".join(f"T{t + 1}" for t in r["trains"]) or "-"
                print(f"{dot} c{r['coaster']} {r['name'][:28]:28s} trains={tr:8s} passes={r['leaves']:4d} "
                      f"last=T{r['lastTrain'] + 1 if r['lastTrain'] >= 0 else '-'}  step={r['step']}")
            if not watch: break
            time.sleep(0.1)
    elif cmd == "raw-set":
        print(nl.section_set(c, args[1], args[2], args[3]))
    elif cmd == "raw-get":
        print(nl.section_get(c, args[1], args[2], float(args[3]) if len(args) > 3 else 0.0))
    else:
        print(__doc__)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        main(sys.argv[1:])
    except NL2Error as e:
        sys.exit(f"error: {e}")
