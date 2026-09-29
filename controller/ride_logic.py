"""Ride PLC for the NL2 virtual control panel.

The panel/HMI (nl2_controller.py) only produces inputs and draws outputs. Everything that decides what the
ride does lives here and runs in one background thread (Link) at ~20 Hz:

    poll NL2Bridge -> RideLogic.scan() -> commands back to NL2Bridge

Two kinds of coasters are handled:
  * normal operation   - the game runs the block system; the PLC drives the operating mode, dispatch,
                         restraints, gates, switches/transfer tables, advance and the E-stop.
  * scripted operation - the PLC *is* the block system controller (a port of the game's own
                         Script Park "BlockHelper" controller), driving brakes, lifts, transports,
                         station handler and switches from the block configuration in rides/<coaster>.json.
"""
import json, os, re, sys, threading, time
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "client"))
from nl2bridge import NL2Bridge, NL2Error, BLOCK_MODE_NAMES  # noqa: E402

# Lamp states. Like the real panel's Output_Controller "anti-sync rule": action lamps (start, dispatch, restraints,
# ack) FLASH in the action phase, stop lamps (ride/lift stop, E-stop, E-stop reset) FLASH_STOP in the other half.
OFF, ON, FLASH, FAST, FLASH_STOP = 0, 1, 2, 3, 4
RIDES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rides")
STATS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.json")

LAMPS = ["ack", "ad_l", "ad_r", "hmi_enable", "restraints", "lift_start", "lift_stop",
         "ride_start", "ride_stop", "estop_reset", "estop", "beeper"]


def lamp_lit(state, t):
    """Shared 1 Hz clock: first half of every second = stop phase, second half = action phase."""
    stop_phase = int(t * 2) % 2 == 0
    return (state == ON or (state == FLASH and not stop_phase) or (state == FLASH_STOP and stop_phase)
            or (state == FAST and int(t * 6) % 2 == 0))


class ParkChanged(Exception):
    pass


# ============================================================================ alarms
class Alarms:
    """Alarm list. set()/clear() for conditions, pulse() for one-shot messages (stay until acknowledged)."""

    def __init__(self):
        self.items = {}        # code -> dict(text, level, active, acked, t)
        self.log = deque(maxlen=200)
        self.last_msg = None   # (time, level, text) of the newest one-shot message

    def set(self, code, text, level="fault"):
        a = self.items.get(code)
        if a and a["active"] and a["text"] == text:
            return
        was_active = bool(a and a["active"])
        if not was_active:
            self.log.appendleft((time.time(), level, text))
        self.items[code] = dict(text=text, level=level, active=True, acked=bool(was_active and a["acked"]),
                                t=a["t"] if was_active else time.time())

    def clear(self, code):
        a = self.items.get(code)
        if a and a["active"]:
            a["active"] = False
            if a["acked"]:
                del self.items[code]

    def pulse(self, text, level="info"):
        """One-shot operator message: shown in the HMI banner for a few seconds and kept in the history."""
        self.last_msg = (time.time(), level, text)
        self.log.appendleft(self.last_msg)

    def ack_all(self):
        for code in list(self.items):
            a = self.items[code]
            a["acked"] = True
            if not a["active"]:
                del self.items[code]

    def unacked(self):
        return any(not a["acked"] for a in self.items.values())

    def active_faults(self, exclude=()):
        return [a for code, a in self.items.items()
                if a["active"] and a["level"] == "fault" and not code.startswith(tuple(exclude))]

    def rows(self):
        order = {"fault": 0, "warn": 1, "info": 2}
        return sorted((dict(a) for a in self.items.values()), key=lambda a: (order.get(a["level"], 3), -a["t"]))


# ============================================================================ scripted block controller
# Port of NoLimits 2 "parks/Script Park/blockhelper" (BlockHelper, BlockHelperCondition, BlockHelperController).
FREE, APPROACHING, LEAVING, BEFORE_TRIGGER, BEHIND_TRIGGER, WAITING, WAIT_FOR_CLEAR, WAIT_FOR_ADVANCE, \
    IN_STATION, APPROACHING_BWD, LEAVING_BWD, BEFORE_TRIGGER_BWD = range(12)
STATE_TEXT = {FREE: ("Free", 0), APPROACHING: ("Approaching", 2), APPROACHING_BWD: ("Approaching", 2),
              LEAVING: ("Leaving", 1), LEAVING_BWD: ("Leaving", 1), BEFORE_TRIGGER: ("Before Trigger", 1),
              BEFORE_TRIGGER_BWD: ("Before Trigger", 1), BEHIND_TRIGGER: ("Behind Trigger", 1),
              WAITING: ("Waiting", 1), WAIT_FOR_CLEAR: ("Waiting for Clear Block", 1),
              WAIT_FOR_ADVANCE: ("Waiting for Advance", 1), IN_STATION: ("In Station", 1)}
M_AUTO, M_MANUAL, M_FULL = 0, 1, 2
LEAVE_MODES = ("none", "fwd", "bwd", "launchfwd", "launchbwd")


class Cond:
    def __init__(self, d):
        if "free" in d:
            self.kind, self.obj, self.param, self.prep = "free", d["free"], 0, False
        elif "switch" in d:
            self.kind, self.obj, self.param, self.prep = "switch", d["switch"], int(d.get("dir", 0)), bool(d.get("prepare", False))
        elif "wait" in d:
            self.kind, self.obj, self.param, self.prep = "wait", None, float(d["wait"]), False
        else:
            raise ValueError(f"Unknown condition {d}")
        self.sum = 0.0


class BlockHelper:
    def __init__(self, ctl, name, sid, kind, leave, fwd, bwd):
        if kind not in ("station", "lift", "brake", "storage"):
            raise ValueError(f"block '{name}': unknown type '{kind}'")
        self.ctl, self.name, self.sid, self.kind = ctl, name, sid, kind
        self.leave = LEAVE_MODES.index(leave) if leave in LEAVE_MODES else 1
        self.fwd = [Cond(c) for c in fwd] or None
        self.bwd = [Cond(c) for c in bwd] or None
        self.opposite = self.fwd is not None and self.bwd is not None
        self.mode = M_AUTO

    # ---- game access (through the controller)
    @property
    def state(self):
        return self.ctl.states[self.sid]

    @state.setter
    def state(self, v):
        self.ctl.states[self.sid] = v

    def on_block(self):       return self.ctl.trains_on_block(self.sid)
    def on_section(self):     return self.ctl.det(self.sid, "occupied")
    def flag(self, f):        return self.ctl.det(self.sid, f)
    def out(self, dev, v):    self.ctl.out(self.sid, dev, v)
    def once(self, what):     self.ctl.once(self.sid, what)

    # ---- conditions (BlockHelperCondition.isTrue / acknoledge)
    def cond_true(self, c, prepare, active_switch):
        if c.kind == "free":
            b = self.ctl.by_name.get(c.obj)
            return b is not None and b.state == FREE
        if c.kind == "switch":
            if active_switch and (not prepare or c.prep):
                self.ctl.set_switch(c.obj, c.param)
            return self.ctl.switch_dir(c.obj) == c.param
        if prepare:
            return True
        if c.sum >= c.param:
            return True
        c.sum += self.ctl.dt
        return False

    def process_conds(self, conds, prepare, active_switch):
        if conds is None:
            return False
        return all(self.cond_true(c, prepare, active_switch) for c in conds)

    def backwards_auto(self):
        return self.leave in (2, 4)

    def prepare_next(self):
        if self.mode == M_AUTO and self.ctl.running:
            return self.process_conds(self.bwd if self.backwards_auto() else self.fwd, True, True)
        return False

    def process_next(self):
        if self.mode == M_AUTO:
            if not self.ctl.running:          # RIDE STOP / E-stop: hold every train in its current block
                return False
            return self.process_conds(self.bwd if self.backwards_auto() else self.fwd, False, True)
        if self.mode == M_MANUAL:
            fwd_ok = (self.opposite and self.leave != 0) or self.leave in (1, 3)
            if fwd_ok and self.process_conds(self.fwd, False, False):
                return True
            bwd_ok = (self.opposite and self.leave != 0) or self.leave in (2, 4)
            if bwd_ok and self.process_conds(self.bwd, False, False):
                return True
        return False

    def ack_next_auto(self):
        if self.mode == M_AUTO:
            bw = self.backwards_auto()
            for c in (self.bwd if bw else self.fwd) or []:
                if c.kind == "free" and c.obj in self.ctl.by_name:
                    self.ctl.by_name[c.obj].state = APPROACHING_BWD if bw else APPROACHING
                elif c.kind == "wait":
                    c.sum = 0.0

    def set_next_state(self, conds, st):
        for c in reversed(conds or []):
            if c.kind == "free" and c.obj in self.ctl.by_name:
                self.ctl.by_name[c.obj].state = st

    def initial_state(self):
        if self.on_block() > 0:
            self.state = IN_STATION if self.kind == "station" else WAIT_FOR_CLEAR
        else:
            self.state = FREE

    # ---- panel events
    def on_mode(self, m):
        if self.mode == M_FULL and m != M_FULL:
            self.initial_state()
        self.mode = m

    def on_advance(self, bwd):
        if self.kind == "station":
            self.once("leaving")
        self.state = LEAVING_BWD if bwd else LEAVING
        self.set_next_state(self.bwd if bwd else self.fwd, APPROACHING_BWD if bwd else APPROACHING)

    def can_advance(self, bwd):
        conds = self.bwd if bwd else self.fwd
        return (self.mode == M_MANUAL and self.state == WAIT_FOR_ADVANCE and conds is not None
                and self.process_conds(conds, False, False))

    # ---- per-scan processing
    def process(self):
        if self.mode == M_FULL:
            return
        getattr(self, "p_" + self.kind)()
        if self.mode == M_MANUAL:
            if self.fwd is not None:
                self.ctl.adv_enabled(self.sid, False, self.can_advance(False))
            if self.bwd is not None:
                self.ctl.adv_enabled(self.sid, True, self.can_advance(True))

    def _leave_outputs(self, bwd):
        self.out("brakes", "trim")
        launch = self.leave == (4 if bwd else 3)
        self.out("transport", ("launchbwd" if bwd else "launchfwd") if launch else ("bwd" if bwd else "fwd"))

    def p_station(self):
        s = self.state
        if s == IN_STATION:
            if self.flag("stationWaitingForClearBlock"):
                if self.process_next():
                    self.once("next_clear")
            elif self.flag("stationWaitingForAdvance"):
                if self.mode == M_MANUAL:
                    if self.process_next():
                        self.state = WAIT_FOR_ADVANCE
                    else:
                        self.once("next_occupied")
                elif self.mode == M_AUTO:
                    if self.process_next():
                        self.ack_next_auto()
                        self.state = LEAVING_BWD if self.backwards_auto() else LEAVING
                        self.once("leaving")
                    else:
                        self.once("next_occupied")
        elif s == WAIT_FOR_ADVANCE:
            if self.mode != M_MANUAL or not self.flag("stationWaitingForAdvance") or not self.process_next():
                self.state = IN_STATION
        elif s in (LEAVING, LEAVING_BWD):
            if self.on_block():
                self._leave_outputs(s == LEAVING_BWD)
            else:
                self.state = FREE
        elif s == FREE:
            self.out("transport", "off"); self.out("brakes", "on")
        elif s in (APPROACHING, APPROACHING_BWD):
            if self.on_section():
                self.once("entering")
                self.state = IN_STATION
            else:
                self.out("brakes", "off"); self.out("transport", "off")

    def p_lift(self):
        s = self.state
        if s == FREE:
            self.out("lift", "idle")
        elif s == APPROACHING:
            if self.on_section():
                self.state = BEFORE_TRIGGER
            else:
                self.out("lift", "idle")
        elif s == BEFORE_TRIGGER:
            self.out("lift", "fwd")
            if self.flag("behindLiftTrigger"):
                self.state = WAIT_FOR_CLEAR
        elif s == WAIT_FOR_ADVANCE:
            if self.mode == M_AUTO:
                self.state = WAIT_FOR_CLEAR
            else:
                if not self.process_next():
                    self.state = WAIT_FOR_CLEAR
                self.out("lift", "off")
        elif s == LEAVING:
            self.out("lift", "fwd")
            if self.on_block() == 0:
                self.state = FREE
        elif s == WAIT_FOR_CLEAR:
            if not self.flag("behindLiftTrigger"):
                self.state = BEFORE_TRIGGER
            elif self.process_next():
                if self.mode == M_AUTO:
                    self.ack_next_auto()
                    self.state = LEAVING
                else:
                    self.out("lift", "off")
                    self.state = WAIT_FOR_ADVANCE
            else:
                self.out("lift", "off")

    def p_brake(self):
        s = self.state
        if s == FREE:
            self.out("transport", "off"); self.out("brakes", "on")
        elif s in (APPROACHING, APPROACHING_BWD):
            if self.on_section():
                self.state = BEFORE_TRIGGER if s == APPROACHING else BEFORE_TRIGGER_BWD
            else:
                self.out("brakes", "off" if self.prepare_next() else "on")
                self.out("transport", "off")
        elif s == BEFORE_TRIGGER:
            self.prepare_next()
            self.out("brakes", "trim"); self.out("transport", "fwdbrake")
            if self.flag("behindBrakeTrigger"):
                self.state = BEHIND_TRIGGER
        elif s == BEFORE_TRIGGER_BWD:
            self.prepare_next()
            self.out("brakes", "trim"); self.out("transport", "bwdbrake")
            if self.flag("beforeBrakeTrigger"):
                self.state = BEHIND_TRIGGER
        elif s == BEHIND_TRIGGER:
            if self.process_next():
                if self.mode == M_AUTO:
                    self.ack_next_auto()
                    self.state = LEAVING_BWD if self.backwards_auto() else LEAVING
                else:
                    self.state = WAIT_FOR_ADVANCE
            else:
                self.out("transport", "off"); self.out("brakes", "on")
        elif s == WAIT_FOR_ADVANCE:
            if self.mode == M_AUTO or not self.process_next():
                self.state = BEHIND_TRIGGER
            else:
                self.out("transport", "off"); self.out("brakes", "on")
        elif s in (LEAVING, LEAVING_BWD):
            self._leave_outputs(s == LEAVING_BWD)
            if self.on_block() == 0:
                self.state = FREE
        elif s == WAIT_FOR_CLEAR:
            self.state = BEHIND_TRIGGER if self.flag("behindBrakeTrigger") else BEFORE_TRIGGER

    def p_storage(self):
        # Train.setLashedToTrack is not exposed by NL2Bridge; storage blocks hold the train with transports off.
        s = self.state
        if s == FREE:
            self.out("transport", "off")
        elif s in (APPROACHING, APPROACHING_BWD):
            self.out("transport", "off")
            if self.on_section():
                self.state = BEFORE_TRIGGER if s == APPROACHING else BEFORE_TRIGGER_BWD
        elif s == BEFORE_TRIGGER:
            self.out("transport", "fwd")
            if self.flag("behindCenter"):
                self.state = WAIT_FOR_CLEAR
        elif s == BEFORE_TRIGGER_BWD:
            self.out("transport", "bwd")
            if self.flag("beforeCenter"):
                self.state = WAIT_FOR_CLEAR
        elif s == WAIT_FOR_CLEAR:
            self.out("transport", "off")
            if self.mode == M_MANUAL and self.process_next():
                self.state = WAIT_FOR_ADVANCE
        elif s == WAIT_FOR_ADVANCE:
            self.out("transport", "off")
            if self.mode != M_MANUAL or not self.process_next():
                self.state = WAIT_FOR_CLEAR
        elif s in (LEAVING, LEAVING_BWD):
            self.out("transport", "bwd" if s == LEAVING_BWD else "fwd")
            if not self.on_section():
                self.state = FREE


def default_ride_config(info):
    """Simple circuit: every block of the main track in ride order, each one waits for the next to be free.
    Blocks off the main circuit (transfer, storage) get no conditions."""
    order = [s for s in main_track_order(info)[1] if s["isBlock"]]
    ids = [s["id"] for s in order]
    blocks = []
    for s in order + [s for s in info["sections"] if s["isBlock"] and s["id"] not in ids]:
        nt = s.get("nodeType", "")
        kind = ("station" if s["station"] or "Station" in nt else "lift" if s["hasLift"] else
                "storage" if "Storage" in nt else "brake")
        fwd = []
        if s["id"] in ids and len(order) > 1:
            fwd = [{"free": order[(ids.index(s["id"]) + 1) % len(order)]["name"]}]
        blocks.append(dict(name=s["name"], type=kind, leave="fwd", fwd=fwd, bwd=[]))
    return dict(coaster=info["coaster"]["name"],
                note="Generated automatically: main-track blocks in ride order, each waits for the next one to be "
                     "free. Edit it like the Script Park BlockScript: type station|lift|brake|storage, "
                     "leave fwd|bwd|launchfwd|launchbwd, fwd/bwd condition lists of {\"free\": block}, "
                     "{\"switch\": special track name, \"dir\": n, \"prepare\": false}, {\"wait\": seconds}. "
                     "The blocks are rebuilt automatically when the track's block sections change in NL2; "
                     "add \"custom_blocks\": true to keep hand edits instead.",
                blocks=blocks)


class ScriptedBlockController:
    """Drives a coaster in scripted operation mode (what a BlockSystemController script normally does)."""

    OUT_CMDS = {"brakes": {"off": 7, "on": 8, "trim": 12},
                "transport": {"off": 9, "fwd": 10, "bwd": 11, "fwdbrake": 13, "bwdbrake": 14,
                              "launchfwd": 17, "launchbwd": 18},
                "lift": {"fwd": 23, "bwd": 24, "off": 25, "idle": 41}}
    ONCE_CMDS = {"entering": 21, "leaving": 5, "next_clear": 2, "next_occupied": 3}

    def __init__(self, logic, info):
        self.logic, self.info = logic, info
        self.name = info["coaster"]["name"]
        self.path, self.cfg = logic.cfg_path, logic.cfg
        by_sec = {s["name"]: s for s in info["sections"]}
        self.blocks, self.by_name, self.states = [], {}, {}
        for b in self.cfg["blocks"]:
            s = by_sec.get(b["name"])
            if not s or not s["isBlock"]:
                logic.alarms.set("CFG:" + b["name"], f"Ride config: block '{b['name']}' not found", "warn")
                continue
            bh = BlockHelper(self, b["name"], s["id"], b.get("type", "brake"), b.get("leave", "fwd"),
                             b.get("fwd", []), b.get("bwd", []))
            self.blocks.append(bh)
            self.by_name[bh.name] = bh
            self.states[bh.sid] = FREE
        self.by_sid = {b.sid: b for b in self.blocks}
        self.cache, self.sent_state, self.sent_adv = {}, {}, {}
        self.ops = []
        self.snap = None
        self.running = False
        self.dt = 0.05
        self.mode = None
        self.registered = False

    # ---- accessors used by BlockHelper
    def det(self, sid, f):
        d = self.snap["detail"].get(sid)
        return bool(d and d.get(f))

    def trains_on_block(self, sid):
        b = self.snap["blocks"].get(sid)
        return b["trains"] if b else 0

    def out(self, sid, dev, v):
        if dev == "lift" and not self.logic.lift_running:
            v = "off"                                    # LIFT STOP overrides the block logic
        key, now = (sid, dev), time.time()
        old = self.cache.get(key)
        if not old or old[0] != v or now - old[1] > 2.0:  # resend now and then in case something else changed it
            self.cache[key] = (v, now)
            self.ops.append(("section", sid, self.OUT_CMDS[dev][v]))

    def once(self, sid, what):
        self.ops.append(("section", sid, self.ONCE_CMDS[what]))

    def adv_enabled(self, sid, bwd, en):
        if self.sent_adv.get((sid, bwd)) != en:
            self.sent_adv[(sid, bwd)] = en
            self.ops.append(("blockcmd", sid, 4 if bwd else 3, en))

    def _switch(self, name):
        for t in self.snap["switches"]:
            if t["name"] == name or t["index"] == name:
                return t
        return None

    def set_switch(self, name, d):
        t = self._switch(name)
        if t and t["current"] != d and t["target"] != d:
            key, now = ("sw", t["index"]), time.time()
            old = self.cache.get(key)
            if not old or old[0] != d or now - old[1] > 1.0:
                self.cache[key] = (d, now)
                self.ops.append(("switch", t["index"], d))

    def switch_dir(self, name):
        t = self._switch(name)
        return t["current"] if t else -1

    # ---- entry points from RideLogic
    def on_advance(self, sid, bwd):
        b = self.by_sid.get(sid)
        if b and b.can_advance(bwd):
            b.on_advance(bwd)
            return True
        return False

    def scan(self, nl, ci, snap, running, dt):
        self.snap, self.running, self.dt = snap, running, dt
        self.ops = []
        if not self.registered:
            for b in self.blocks:
                for st, (text, lamp) in STATE_TEXT.items():
                    nl.register_state(ci, b.sid, st, text, lamp)
                nl.scripted_block(ci, b.sid, 1, b.fwd is not None)
                nl.scripted_block(ci, b.sid, 2, b.bwd is not None)
                b.initial_state()
            self.registered = True
        mode = {1: M_AUTO, 2: M_MANUAL, 3: M_FULL}.get(snap["coaster"]["blockMode"], M_AUTO)
        if mode != self.mode:
            for b in self.blocks:
                b.on_mode(mode)
            self.mode = mode
            self.sent_adv.clear()
        for b in self.blocks:
            b.process()
        for b in self.blocks:
            if self.sent_state.get(b.sid) != b.state:
                self.sent_state[b.sid] = b.state
                self.ops.append(("state", b.sid, b.state))
        for op in self.ops:
            try:
                if op[0] == "section":
                    nl.section_set(ci, op[1], op[2])
                elif op[0] == "state":
                    nl.set_state(ci, op[1], op[2])
                elif op[0] == "blockcmd":
                    nl.scripted_block(ci, op[1], op[2], op[3])
                elif op[0] == "switch":
                    nl.set_switch(ci, op[1], op[2])
            except NL2Error as e:
                self.logic.alarms.pulse(f"Block controller: {e}", "warn")

    def state_text(self, sid):
        b = self.by_sid.get(sid)
        return STATE_TEXT[b.state][0] if b else None


# ============================================================================ ride file, HMI layout, statistics
def ride_file_path(name):
    return os.path.join(RIDES_DIR, re.sub(r'[\\/:*?"<>|]', "_", name) + ".json")


def short_name(name):
    for pre, rep in (("Unnamed Section ", "S"), ("Unnamed Special Track ", "ST")):
        if name.startswith(pre):
            return rep + name[len(pre):]
    return name


NODE_KIND = {"NLStationBlockNode": "Station", "NLLiftBlockNode": "Lift", "NLBrakeBlockNode": "Brake",
             "NLTransferBlockNode": "Transfer", "NLStorageBlockNode": "Storage"}


def block_labels(info, labels=None):
    """Operator names for every block section (sid -> text): Station, Lift 1, Brake 3, Transfer, Storage 2 ...
    numbered in ride order. Sections the designer named keep their name; ride-file 'labels' override both.
    Non-block (free-run) sections get no label."""
    labels = labels or {}
    order = [s for s in main_track_order(info)[1] if s["isBlock"]]
    seen = {s["id"] for s in order}
    order += [s for s in info["sections"] if s["isBlock"] and s["id"] not in seen]
    kinds = [NODE_KIND.get(s.get("nodeType"), "Station" if s["station"] else "Lift" if s["hasLift"] else "Block")
             for s in order]
    total = {k: kinds.count(k) for k in kinds}
    n, out = {}, {}
    for s, k in zip(order, kinds):
        n[k] = n.get(k, 0) + 1
        auto = k if total[k] == 1 else f"{k} {n[k]}"
        custom = s["name"] if s["name"].strip() and not s["name"].startswith("Unnamed ") else None
        out[s["id"]] = labels.get(s["name"]) or custom or auto
    return out


def switch_labels(info):
    """Operator names for switches / transfer tables."""
    out, n = {}, {}
    sts = info.get("specialTracks", [])
    for s in sts:
        k = "Transfer Table" if s.get("type") == "transferTable" else "Switch"
        n[k] = n.get(k, 0) + 1
        tot = sum(1 for x in sts if ("Transfer Table" if x.get("type") == "transferTable" else "Switch") == k)
        out[s["index"]] = s["name"] if not s["name"].startswith("Unnamed ") else k if tot == 1 else f"{k} {n[k]}"
    return out


def transfer_geometry(info):
    """Transfer block section, transfer table (special track index) and storage block sections of a coaster."""
    secs = info["sections"]
    return dict(
        xfer=next((s["id"] for s in secs if s.get("nodeType") == "NLTransferBlockNode"), None),
        table=next((s["index"] for s in info.get("specialTracks", []) if s.get("type") == "transferTable"), None),
        storage=[s["id"] for s in secs if s.get("nodeType") == "NLStorageBlockNode"])


def nl2_window():
    """(hwnd, minimized) of the NoLimits 2 window on this PC, or (None, False). A minimized NL2 does not simulate."""
    try:
        import ctypes
        import ctypes.wintypes as W
        u = ctypes.windll.user32
    except (ImportError, AttributeError, OSError):
        return None, False
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
    def cb(h, _l):
        n = u.GetWindowTextLengthW(h)
        if n and u.IsWindowVisible(h):
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            if b.value.endswith("NoLimits 2"):
                found.append(h)
        return True
    u.EnumWindows(cb, 0)
    return (found[0], bool(u.IsIconic(found[0]))) if found else (None, False)


def restore_nl2_window():
    """Un-minimize NoLimits 2 without stealing focus (SW_SHOWNOACTIVATE). Returns True if it was minimized."""
    h, mini = nl2_window()
    if h and mini:
        import ctypes
        ctypes.windll.user32.ShowWindow(h, 4)
        return True
    return False


def main_track_order(info):
    """(main track, main-track sections in ride order starting at the first station block)."""
    secs = info["sections"]
    tracks = [s["track"] for s in secs if s["track"] >= 0]
    if not tracks:
        return -1, []
    main = max(set(tracks), key=tracks.count)
    on = sorted((s for s in secs if s["track"] == main), key=lambda s: s["trackStart"])
    i = next((k for k, s in enumerate(on) if s["station"] and s["isBlock"]), 0)
    return main, on[i:] + on[:i]


def block_zones(info):
    """Main-track blocks in ride order; each zone = its block section plus the non-block sections after it."""
    zones = []
    for s in main_track_order(info)[1]:
        if s["isBlock"] or not zones:
            zones.append(dict(sid=s["id"], name=s["name"], station=bool(s["station"]), lift=bool(s["hasLift"]),
                              secs=[s["id"]]))
        else:
            zones[-1]["secs"].append(s["id"])
    return zones


def _after_lifts(zones):
    """Index of the first zone after the (consecutive) lift(s) that follow the station, or None."""
    k = 1
    while k < len(zones) and not zones[k]["lift"]:
        k += 1
    if k >= len(zones):
        return None
    while k < len(zones) and zones[k]["lift"]:
        k += 1
    return k


def block_signature(info):
    """Everything the controller layout is built from. When it changes (a section renamed, a block node added or
    removed, a switch edited) the controller reloads the coaster."""
    return (tuple(sorted((s["id"], s["name"], bool(s["isBlock"]), s.get("nodeType", ""), bool(s["station"]))
                         for s in info["sections"])),
            tuple((t["index"], t["name"], t.get("type")) for t in info.get("specialTracks", [])))


def default_hmi(info):
    zones = block_zones(info)
    k = _after_lifts(zones)
    tf = zones[k - 1]["name"] if k else None
    tt = zones[k]["name"] if k and k < len(zones) else None
    return dict(title="Six Flags\nRide Operation", rows=8, seats_per_row=4, labels={},
                timer={"from": tf, "to": tt, "min": None, "max": None},
                note="rows: fallback row count until a train is seen in the station (the controller reads the real "
                     "count from the train). labels: {\"section name\": \"HMI text\"} for the track zones and storage. timer: the "
                     "'Lift to Block Brake' timer runs from a train leaving section 'from' until it reaches "
                     "section 'to'; min/max (seconds) raise a run-time warning like the real panel.")


def load_ride_file(info):
    """Load (or create) rides/<coaster>.json. Returns (cfg, path). Raises OSError/ValueError on a bad file."""
    path = ride_file_path(info["coaster"]["name"])
    os.makedirs(RIDES_DIR, exist_ok=True)
    changed = False
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg, changed = default_ride_config(info), True
    hmi = default_hmi(info)
    if not isinstance(cfg.get("hmi"), dict):
        cfg["hmi"], changed = hmi, True
    for k, v in hmi.items():
        if k not in cfg["hmi"]:
            cfg["hmi"][k], changed = v, True
    changed |= _sync_ride_file(cfg, info, path, hmi)
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    return cfg, path


def _sync_ride_file(cfg, info, path, hmi):
    """Follow track edits made in the NL2 editor: when the coaster's block sections no longer match the ride file
    (renamed, added or removed blocks) the generated block script is rebuilt (the old file is kept as .bak) unless
    "custom_blocks": true. Timer ends and learned storage positions that point at vanished sections are reset."""
    names = {s["name"] for s in info["sections"]}
    gen = default_ride_config(info)
    old = cfg.get("blocks") if isinstance(cfg.get("blocks"), list) else []
    changed = False
    if not cfg.get("custom_blocks") and \
            sorted(str(b.get("name")) for b in old) != sorted(b["name"] for b in gen["blocks"]):
        if old:
            try:
                with open(path + ".bak", "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
            except OSError:
                pass
        cfg["blocks"], cfg["note"], changed = gen["blocks"], gen["note"], True
    tm = cfg["hmi"].get("timer")
    if not isinstance(tm, dict):
        cfg["hmi"]["timer"], changed = hmi["timer"], True
    elif tm.get("from") not in names or tm.get("to") not in names:
        tm["from"], tm["to"], changed = hmi["timer"]["from"], hmi["timer"]["to"], True
    xs = (cfg.get("transfer") or {}).get("storage")
    if isinstance(xs, dict):
        for k in [k for k, v in xs.items() if isinstance(v, str) and v not in names]:
            del xs[k]
            changed = True
    return changed


def join_from_tracks(info):
    """Transfer join from the bridge's track connections (bridge v5d+), or None when unknown."""
    main, order = main_track_order(info)
    tables = {t["index"] for t in info.get("specialTracks", []) if t.get("type") == "transferTable"}
    trk = next((t for t in info.get("tracks", []) if t.get("index") == main), None)
    if trk and order:
        ends = [trk.get(k) for k in ("start", "end")]
        if any(isinstance(e, dict) and e.get("special") in tables for e in ends):
            return min(order, key=lambda s: s["trackStart"])["id"]
    return None


def transfer_join(info, xcfg=None):
    """Main-track section a train enters when it comes off the transfer table (the table sits just before it).
    From the track connections reported by the bridge (a track whose start/end is joined to the table joins it at
    track position 0 / its end); else learned from train movement (ride file transfer.joins_before); default: the
    section after the station block."""
    order = main_track_order(info)[1]
    sid = join_from_tracks(info)
    if sid is not None:
        return sid
    name = (xcfg or {}).get("joins_before")
    sid = next((s["id"] for s in order if s["name"] == name), None) if name else None
    if sid is None:
        i = next((k for k, s in enumerate(order) if s["station"] and s["isBlock"]), None)
        if i is not None and len(order) > 1:
            sid = order[(i + 1) % len(order)]["id"]
    return sid


def hmi_layout(info, hmi, xcfg=None):
    """Track zones for the HMI home page: bottom row (drawn right to left) = the block before the station, the
    station and everything up to the lift(s); top row (left to right) = the rest of the circuit. Spots are drawn
    for block sections (station, lift, brake, transfer, storage) and trim brakes only; plain free-run sections
    count as part of the spot before them. A transfer table block that is part of the circuit is drawn inline
    where trains cross it. Station and lift blocks show Enter / Park / Exit progress squares."""
    zones = block_zones(info)
    labels = hmi.get("labels") or {}
    names = block_labels(info, labels)
    by_id = {s["id"]: s for s in info["sections"]}
    xg = transfer_geometry(info)
    join = transfer_join(info, xcfg) if xg["xfer"] is not None and xg["table"] is not None else None
    if join is not None:
        for i, z in enumerate(zones):
            if join in z["secs"]:
                j = z["secs"].index(join)
                xz = dict(sid=xg["xfer"], name=by_id[xg["xfer"]]["name"], station=False, lift=False,
                          secs=[xg["xfer"]] + z["secs"][j:])
                if j == 0:
                    zones.insert(i, xz)
                else:
                    z["secs"] = z["secs"][:j]
                    zones.insert(i + 1, xz)
                break
    for z in zones:
        z["label"] = names.get(z["sid"], z["name"])
        sq = []
        for sid in z["secs"]:
            s = by_id[sid]
            if not sq or (s.get("hasBrake") and not s["isBlock"]):
                sq.append([sid])
            else:
                sq[-1].append(sid)
        z["squares"] = sq
        z["trims"] = [by_id[q[0]]["name"] for q in sq[1:]]
        z["phased"] = bool(z["station"] or z["lift"])
        z["transfer"] = z["sid"] == xg["xfer"]
    n = len(zones)
    k = _after_lifts(zones)
    if k is None:
        k = max(1, (n + 1) // 2)
    if k < n:
        bottom, top = [zones[-1]] + zones[:k], zones[k:-1]
    else:
        bottom, top = zones, []
    main = main_track_order(info)[0]
    inline = {z["sid"] for z in zones}
    off = [s for s in info["sections"] if s["isBlock"] and s["track"] != main and s["id"] not in inline]
    off.sort(key=lambda s: (s.get("nodeType") != "NLTransferBlockNode", s["id"]))
    storage = [dict(sid=s["id"], name=s["name"], label=names.get(s["id"], s["name"]),
                    transfer=s.get("nodeType") == "NLTransferBlockNode") for s in off[:4]]
    return dict(bottom=bottom, top=top, storage=storage, before_station=zones[-1]["sid"] if n > 1 else None,
                labels=names, switch_labels=switch_labels(info), join=join)


def block_phase(lift, d, b):
    """Progress of a train through a station or lift block: 0 = enter, 1 = park (stop position: the station
    stop, or the lift trigger where it waits for the next block), 2 = exit; None = block not occupied."""
    if not d or not d.get("occupied"):
        return None
    st = (b or {}).get("stateName", "")
    if "Waiting" in st:
        return 1
    if st.startswith("Leaving"):
        return 2
    if lift:
        if d.get("behindLiftTrigger"):
            return 2
        return 1 if d.get("behindCenter") else 0
    if d.get("behindCenter"):
        return 2
    return 0 if d.get("beforeCenter") else 1


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_json_key(path, key, value):
    d = _load_json(path)
    d[key] = value
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
    except OSError:
        pass


class RideStats:
    """Rider / dispatch counters shown on the HMI home page, persisted per coaster in stats.json."""

    def __init__(self, coaster):
        self.coaster = coaster
        self.d = dict(current=0, total_disp=0, total_riders=0, hours={}, days={}, recent=[])
        self.d.update(_load_json(STATS_PATH).get(coaster, {}))

    def save(self):
        _save_json_key(STATS_PATH, self.coaster, self.d)

    def dispatch(self, now):
        riders = self.d["current"]
        hk = time.strftime("%Y-%m-%d %H", time.localtime(now))
        for key, bucket in ((hk, "hours"), (hk[:10], "days")):
            v = self.d[bucket].setdefault(key, [0, 0])
            v[0] += 1
            v[1] += riders
        self.d["recent"] = [r for r in self.d["recent"] if now - r[0] < 3600] + [[now, riders]]
        self.d["total_disp"] += 1
        self.d["total_riders"] += riders
        self.d["current"] = 0
        for bucket, keep in (("hours", 48), ("days", 60)):
            for k in sorted(self.d[bucket])[:-keep]:
                del self.d[bucket][k]
        self.save()

    def add_riders(self, delta, cap):
        self.d["current"] = max(0, min(cap, self.d["current"] + delta))
        self.save()

    def view(self, now):
        recent = [r for r in self.d["recent"] if now - r[0] < 3600]
        prev = self.d["hours"].get(time.strftime("%Y-%m-%d %H", time.localtime(now - 3600)), [0, 0])
        day = self.d["days"].get(time.strftime("%Y-%m-%d", time.localtime(now)), [0, 0])
        return dict(current=self.d["current"], pph=sum(r[1] for r in recent), prev_people=prev[1], ppd=day[1],
                    dph=len(recent), prev_disp=prev[0], dpd=day[0], total=self.d["total_disp"])


# ============================================================================ ride PLC
STARTUP_TEXT = {0: "Panel is off - turn PANEL ENABLE on", 10: "Lamp test - press ACKNOWLEDGE",
                15: "Daily test required - Main Menu > Daily Test",
                20: "Startup warning 1 - hold RIDE START for 5 seconds",
                30: "Startup delay - release RIDE START and wait",
                40: "Startup warning 2 - hold RIDE START for 5 seconds",
                50: "Press E-STOP RESET to arm the ride", 60: ""}
DAILY_CHECKS = [("estop", "Push the E-STOP"), ("lockout", "Turn the E-STOP LOCKOUT"),
                ("estop_reset", "Press E-STOP RESET"), ("ride_start", "Press RIDE START"),
                ("ride_stop", "Press RIDE STOP"), ("lift_start", "Press LIFT START"), ("lift_stop", "Press LIFT STOP")]
PANEL_KEY = "_panel"
NOT_LATCHING = ("ESTOP", "LOCKOUT")


class RideLogic:
    """Port of the RCPANELV5 Opta program (Startup_Sequence, Ride_Logic, Station_Manager, NL2_Manager,
    Output_Controller, Fault/Warning/Daily_Test managers) onto the richer NL2Bridge API."""

    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.alarms = Alarms()
        self.keys = dict(panel=False, mode="AUTO", maint=False, lockout=False)
        self.estop_pb = False
        self.held = set()
        self._presses, self._intents = [], []
        self.seq, self.t_hold, self.t_delay = 0, None, None
        self.ride_running = self.lift_running = False
        self.faulted, self.fault_text, self.silenced = False, "", False
        self.daily_active, self.daily_checks = False, set()
        self.daily_ts = _load_json(STATS_PATH).get(PANEL_KEY, {}).get("daily_passed", 0)
        self.hmi_until = 0.0
        self.full_manual = False
        self.station_sel = 0
        self.lamps = {k: OFF for k in LAMPS}
        self.view = dict(connected=False, status="Connecting...")
        self.bc = None
        self.ci = self.info = None
        self.cfg = self.cfg_path = self.layout = self.stats = None
        self.timer_sids = (None, None)
        self.prev_panel = False
        self.next_mode_try = self.next_disp_try = self.next_estop_try = 0.0
        self.estop_cmd, self.estop_grace = False, 0.0
        self.ad_since, self.ad_done, self.dev_sent = None, False, False
        self.prev_has = None
        self.parked_since = None
        self.release_until = 0.0
        self.prev_mode_key = None
        self.arrived = False
        self.rows_api = False
        self.n_rows = None
        self.lift_beep_until = 0.0
        self.timer_run, self.timer_prev, self.timer_last = {}, {}, 0.0
        self.run_warn = (0.0, "")
        self.xfer_ilk = []
        self.xseq = None                  # running transfer sequence (ADVANCE / REVERSE TRAIN)
        self.xgeo = dict(xfer=None, table=None, storage=[])
        self.dev_pending = []             # full-manual device commands waiting for the game to enter full manual
        self.reset_quiet = 0.0
        self.game_auto_dispatch = False   # HMI toggle: let the game auto-dispatch while the controller is connected
        self.disp_taken = set()           # stations the controller put into manual dispatch (restored on exit)
        self.reset_armed = 0.0
        self.nl2_minimized, self.next_win_check = False, 0.0
        self.events = deque(maxlen=200)
        self.last_scan = time.time()

    # ---------------------------------------------------------------- inputs (UI thread)
    def press(self, name):
        with self.lock:
            self._presses.append(name)

    def hold(self, name, down):
        with self.lock:
            (self.held.add if down else self.held.discard)(name)

    def set_key(self, key, value):
        with self.lock:
            self.keys[key] = value

    def set_estop_pb(self, pushed):
        with self.lock:
            self.estop_pb = pushed

    def intent(self, *args):
        """HMI touch command, e.g. ('gates_toggle',), ('switch', idx, dir), ('advance', sid, bwd)."""
        with self.lock:
            self._intents.append(args)

    def hmi_enabled(self):
        return not self.settings.get("require_hmi_enable", True) or time.time() < self.hmi_until

    # ---------------------------------------------------------------- daily test (valid until the next 3 AM)
    def _daily_valid(self, now):
        lt = time.localtime(now)
        boundary = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 3, 0, 0, 0, 0, -1))
        if now < boundary:
            boundary -= 86400
        return self.daily_ts >= boundary

    def _daily_pass(self, now):
        self.daily_ts = now
        self.daily_active, self.daily_checks = False, set()
        _save_json_key(STATS_PATH, PANEL_KEY, {"daily_passed": now})

    # ---------------------------------------------------------------- link callbacks (Link thread)
    def on_connect(self, ci, info):
        self.ci, self.info = ci, info
        self.bc = None
        self.alarms.clear("COMM")
        self.alarms.clear("CFG")
        try:
            self.cfg, self.cfg_path = load_ride_file(info)
        except (OSError, ValueError) as e:
            self.cfg_path = ride_file_path(info["coaster"]["name"])
            self.cfg = dict(blocks=default_ride_config(info)["blocks"], hmi=default_hmi(info), broken=True)
            self.alarms.set("CFG", f"Ride file error ({os.path.basename(self.cfg_path)}): {e}", "fault")
        self.layout = hmi_layout(info, self.cfg["hmi"], self.cfg.get("transfer"))
        names = {s["name"]: s["id"] for s in info["sections"]}
        tm = self.cfg["hmi"].get("timer") or {}
        self.timer_sids = (names.get(tm.get("from")), names.get(tm.get("to")))
        self.timer_run, self.timer_prev = {}, {}
        self.stats = RideStats(info["coaster"]["name"])
        self.prev_has = None
        self.n_rows = None
        self.xgeo = transfer_geometry(info)
        self.xseq, self.dev_pending, self.full_manual = None, [], False
        self.game_auto_dispatch, self.disp_taken, self.next_disp_try = False, set(), 0.0
        self.lift_speed, self.lift_cmd, self.lift_api, self.next_lift_check = None, {}, True, 0.0
        xc = self.cfg.setdefault("transfer", {})
        xc.setdefault("main", None)
        xc.setdefault("storage", {})
        xc.setdefault("note", "main = transfer table position (0-based) that lines up with the main track; storage = "
                              "{position: storage section name}. Both are learned automatically.")
        self.alarms.pulse(f"Connected to {info['coaster']['name']}")

    def row_count(self):
        """Rows of the coaster's trains, read from the train in the station (ride file hmi.rows until one is seen)."""
        if self.n_rows:
            return self.n_rows
        try:
            return max(1, int(((self.cfg or {}).get("hmi") or {}).get("rows", 8)))
        except (TypeError, ValueError):
            return 8

    def _save_cfg(self):
        if self.cfg and self.cfg_path and not self.cfg.get("broken"):
            try:
                with open(self.cfg_path, "w", encoding="utf-8") as f:
                    json.dump(self.cfg, f, indent=2)
            except OSError as e:
                self.alarms.pulse(f"Could not save the ride file: {e}", "warn")

    def on_disconnect(self, why):
        self.bc = None
        self.alarms.set("COMM", f"No connection to NL2Bridge ({why})", "fault")
        with self.lock:
            self.view = dict(connected=False, status=why, alarms=self.alarms.rows(), log=list(self.alarms.log),
                             msg=self.alarms.last_msg, keys=dict(self.keys), seq=self.seq)
            self.lamps = {k: OFF for k in LAMPS}
            self.lamps["ack"] = FLASH
            self.lamps["estop"] = ON if self.estop_pb or self.keys["lockout"] else OFF

    def release(self, nl):
        """Controller closing: give the stations it took over back to the game's automatic dispatch and put the lift
        chains back to their design speed."""
        for si in sorted(self.disp_taken):
            try:
                nl.station_op(self.ci, si, "auto")
            except (NL2Error, OSError):
                pass
        self.disp_taken = set()
        for sid, v in (self.lift_speed or {}).items():
            try:
                nl.set_device_params(self.ci, sid, "lift", speed=v)
            except (NL2Error, OSError):
                pass
        self.lift_cmd = {}
        if self.estop_cmd:                       # hand the ride back to the game without the panel's E-stop latched
            try:
                nl.estop(self.ci, False)
            except (NL2Error, OSError):
                pass
            self.estop_cmd = False

    def _lift_power(self, nl, snap, run):
        """The lift chains only turn while the panel is on, the ride is started and LIFT START is latched; otherwise
        their speed is set to 0 so the chain ramps down with the game's own deceleration (works in every block mode).
        The design speed is read from the game once and kept in the ride file, so it survives a controller crash
        that left a chain at 0."""
        if not self.lift_api:
            return
        now = time.time()
        if self.lift_speed is None:
            saved = (self.cfg or {}).setdefault("lift", {}).setdefault("speeds", {})
            self.cfg["lift"].setdefault("note", "speeds = design lift chain speed (m/s) per lift section, learned from "
                                                "the game. The controller sets them to 0 while the lift is stopped.")
            speeds, changed = {}, False
            for s in (self.info or {}).get("sections", []):
                if not s.get("hasLift"):
                    continue
                try:
                    p = nl.device_params(self.ci, s["id"]).get("lift")
                except NL2Error as e:
                    if "Unknown message" in str(e):
                        self.lift_api = False
                        self.alarms.pulse("NL2Bridge is older than API 6 - LIFT STOP cannot stop the chain", "warn")
                        return
                    raise
                if not p:
                    continue
                v = float(p["speed"])
                if v > 0.01:
                    if abs(float(saved.get(s["name"], 0) or 0) - v) > 1e-4:
                        saved[s["name"]], changed = round(v, 4), True
                else:
                    v = float(saved.get(s["name"], 0) or 0)
                    if v <= 0.01:
                        self.alarms.pulse(f"Lift {self.lab(s['id'])} has no speed saved - reload the park once", "warn")
                        continue
                speeds[s["id"]] = v
            self.lift_speed = speeds
            if changed:
                self._save_cfg()
        for sid, v in self.lift_speed.items():
            want = v if run else 0.0
            if self.lift_cmd.get(sid) != want:
                if self._do(f"Lift {self.lab(sid)}", nl.set_device_params, self.ci, sid, "lift", want):
                    self.lift_cmd[sid] = want
        if now >= self.next_lift_check:          # the game can reset a chain (e.g. park reload): re-assert it
            self.next_lift_check = now + 3.0
            for sid, want in list(self.lift_cmd.items()):
                try:
                    if abs(float((nl.device_params(self.ci, sid).get("lift") or {}).get("speed", want)) - want) > 1e-3:
                        self.lift_cmd.pop(sid)
                except NL2Error:
                    pass

    def _do(self, what, fn, *a, quiet=False):
        try:
            r = fn(*a)
            return True if r is None else r
        except NL2Error as e:
            if not quiet:
                self.alarms.pulse(f"{what}: {e}", "warn")
            return False

    # ---------------------------------------------------------------- the scan
    def scan(self, nl, snap):
        now = time.time()
        self._snap = snap
        self.rows_api = bool(snap.get("rows_api"))
        dt = min(0.5, now - self.last_scan)
        self.last_scan = now
        with self.lock:
            presses, self._presses = self._presses, []
            intents, self._intents = self._intents, []
            keys = dict(self.keys)
            estop_pb = self.estop_pb
            held = set(self.held)
        S = self.settings
        ci, c, al = self.ci, snap["coaster"], self.alarms
        scripted = c["operationMode"] == 2
        panel, loto_ok = keys["panel"], not keys["lockout"]
        stations = snap["stations"]
        if self.station_sel >= len(stations):
            self.station_sel = 0
        st = stations[self.station_sel] if stations else None
        lamps = {k: OFF for k in LAMPS}

        def pb(name):                       # button held now, or tapped since the last scan
            return name in held or name in presses

        ad_held = (pb("ad_l") and pb("ad_r")) if S.get("two_hand_dispatch") else (pb("ad_l") or pb("ad_r"))
        if "hmi_enable" in presses:
            self.hmi_until = now + float(S.get("hmi_enable_seconds", 5))
        hmi_ok = self.hmi_enabled()
        game_estop = bool(c["estop"])

        # ---- safety inputs -> alarms
        if estop_pb:
            al.set("ESTOP_PB", "EMERGENCY STOP - E-STOP pushbutton pressed", "fault")
        else:
            al.clear("ESTOP_PB")
        if not loto_ok:
            al.set("LOCKOUT", "E-STOP LOCKOUT switch engaged - ride locked out", "fault")
        else:
            al.clear("LOCKOUT")
        if not c["ready"] and now >= self.reset_quiet:
            al.set("RESET", "Coaster needs a reset (in game)", "fault")
        elif c["ready"]:
            al.clear("RESET")

        # ---- daily test (Seq 15): operate every safety device once
        daily_ok = not S.get("require_daily_test", True) or self._daily_valid(now)
        if self.seq == 15 and self.daily_active:
            for code, _ in DAILY_CHECKS:
                if (code == "estop" and estop_pb) or (code == "lockout" and not loto_ok) or code in presses:
                    self.daily_checks.add(code)
            if all(code in self.daily_checks for code, _ in DAILY_CHECKS):
                self._daily_pass(now)
                daily_ok = True
                al.pulse("Daily test complete")

        # ---- startup sequence (Startup_Sequence)
        seq0 = self.seq
        if not panel:
            self.seq = 0
        elif self.seq == 0:
            self.seq = 10
        if self.seq == 10 and "ack" in presses:
            self.seq = 15
        if self.seq == 15 and daily_ok:
            self.seq = 20
        if self.seq in (20, 40):
            if pb("ride_start"):
                self.t_hold = self.t_hold or now
                if now - self.t_hold >= 5.0:
                    self.seq, self.t_hold = self.seq + 10, None
            else:
                self.t_hold = None
        if self.seq == 30:
            if pb("ride_start"):
                self.t_delay = None
            else:
                self.t_delay = self.t_delay or now
                if now - self.t_delay >= 5.0:
                    self.seq, self.t_delay = 40, None
        if 30 <= self.seq <= 40 and (estop_pb or not loto_ok):
            self.seq = 20
            al.pulse("Startup interrupted by E-stop / lockout - restart from warning 1", "warn")
        if self.seq == 50 and "estop_reset" in presses:
            if estop_pb:
                al.pulse("Pull the E-STOP pushbutton out before resetting")
            elif not loto_ok:
                al.pulse("Turn the E-STOP LOCKOUT off before resetting")
            else:
                self.seq = 60
        if self.seq == 60 and (estop_pb or not loto_ok):
            self.seq = 50
        if self.seq == 60 and game_estop and not self.estop_cmd and now > self.estop_grace:
            self.seq = 50
            al.pulse("Emergency stop from the game - press E-STOP RESET", "warn")
        if self.seq != seq0 and STARTUP_TEXT.get(self.seq) and self.seq != 60:
            al.pulse(STARTUP_TEXT[self.seq])
        elif self.seq == 60 and seq0 != 60:
            al.pulse("Ride armed - press RIDE START")
        system_active = self.seq == 60
        if self.seq != 15:
            self.daily_active = False

        # ---- faults: latch, reset with MAINTENANCE ENABLE + ACKNOWLEDGE (Fault_Manager)
        faults = al.active_faults(NOT_LATCHING)
        if faults and not self.faulted:
            self.faulted, self.silenced, self.fault_text = True, False, faults[0]["text"]
        if "ack" in presses:
            al.ack_all()
            if self.faulted and self.seq != 10:
                if not keys["maint"]:
                    self.silenced = False
                    al.pulse("Fault reset needs the MAINTENANCE ENABLE key + ACKNOWLEDGE", "warn")
                elif estop_pb:
                    al.pulse("Pull the E-STOP pushbutton out before resetting the fault", "warn")
                elif faults:
                    al.pulse("Fault still present: " + faults[0]["text"], "warn")
                else:
                    self.faulted = False
                    al.pulse("Fault reset")

        # ---- ride / lift latches (Ride_Logic); turning the OPERATION MODE key drops RIDE START out
        if self.prev_mode_key is not None and keys["mode"] != self.prev_mode_key:
            if self.ride_running:
                al.pulse(f"OPERATION MODE {keys['mode']} - ride stopped, press RIDE START", "warn")
            if self.xseq:
                self._xfer_abort(nl, "OPERATION MODE changed")
            self.ride_running = self.lift_running = False
            self.full_manual = False
            self.dev_pending = []
        self.prev_mode_key = keys["mode"]
        self.ride_running = system_active and not self.faulted and \
            (pb("ride_start") or self.ride_running) and not pb("ride_stop")
        lift_was = self.lift_running
        self.lift_running = self.ride_running and (pb("lift_start") or self.lift_running) and not pb("lift_stop")
        lift_occupied = any(b.get("hasLift") and b.get("trains") for b in snap["blocks"].values())
        if self.lift_running and not lift_was and lift_occupied:
            self.lift_beep_until = now + 5.0
        self._lift_power(nl, snap, panel and self.lift_running)

        # ---- game E-stop mirrors the panel safety chain (NL2_Manager)
        want = estop_pb or not loto_ok or self.faulted or self.seq != 60 or \
            (S.get("ride_stop_estop", True) and not self.ride_running)
        if panel or estop_pb or not loto_ok:
            if want != self.estop_cmd:
                self.estop_cmd, self.next_estop_try = want, 0.0
                if not want:
                    self.estop_grace = now + 2.0
            if want != game_estop and now >= self.next_estop_try:
                self.next_estop_try = now + 1.0
                self._do("E-stop", nl.estop, ci, want)
        elif self.estop_cmd:
            self.estop_cmd = False
            self._do("E-stop", nl.estop, ci, False)
        estop = estop_pb or not loto_ok or game_estop

        # ---- panel enable key
        if panel and not self.prev_panel:
            al.pulse("PANEL ENABLE on")
        if not panel and self.prev_panel:
            al.pulse("PANEL ENABLE off - ride handed back to the game")
            al.clear("MODE")
        self.prev_panel = panel

        # ---- station dispatch mode: while the controller is connected it owns dispatching, so the game's stations
        #      are held in manual dispatch unless GAME AUTO DISPATCH is switched on from the HMI
        if now >= self.next_disp_try:
            self.next_disp_try = now + 2.0
            for s in stations:
                if bool(s["manualDispatch"]) == self.game_auto_dispatch:
                    if self._do("Station dispatch mode", nl.station_op, ci, s["index"],
                                "auto" if self.game_auto_dispatch else "manual"):
                        if not self.game_auto_dispatch:
                            self.disp_taken.add(s["index"])

        cur_mode = BLOCK_MODE_NAMES.get(c["blockMode"], "?")
        self.xfer_ilk = [
            ("OPERATION MODE = TRANSFER", keys["mode"] == "TRANSFER" or keys["maint"]),
            ("Ride running (RIDE START)", self.ride_running),
            ("Block system in manual", cur_mode in ("manual", "fullmanual")),
            ("All trains stationary", bool(snap["trains"]) and all(abs(tr["speed"]) < 0.05 for tr in snap["trains"])),
            ("No emergency stop", not game_estop),
            ("Coaster ready (no reset needed)", bool(c["ready"]))]
        if not scripted:
            self._xfer_learn(snap, cur_mode)
        self._learn_join(snap)
        if self.xseq:
            if not panel:
                self._xfer_abort(nl, "PANEL ENABLE off")
            elif estop or not c["ready"]:
                self._xfer_abort(nl, "emergency stop")
            else:
                self._xfer_run(nl, snap, now, cur_mode)
        if now >= self.next_win_check:
            self.next_win_check = now + 2.0
            self.nl2_minimized = nl2_window()[1]

        # ---- station state
        has = bool(st and st["hasTrain"])
        sid = st["sectionId"] if st else None
        locked = bool(st and st["harness"]["closed"])
        gates_ok = bool(st and (not st["gates"]["present"] or st["gates"]["closed"]))
        stopped = bool(has and st["trainReady"])
        self.parked_since = (self.parked_since or now) if has else None
        parked = has and now - self.parked_since >= 1.0
        if self.prev_has is not None and has != self.prev_has:
            if not has and c["ready"] and not game_estop and self.stats:
                self.stats.dispatch(now)
            self.arrived = has and panel
        self.prev_has = has
        # arrival (once the train has stopped): unlock flyer, raise floor, open restraints + gates
        if self.arrived and stopped and panel and self.ride_running:
            self.arrived = False
            for op, dev, cond in (("flyer_unlock", "flyer", "locked"), ("platform_raise", "platform", "lowered"),
                                  ("harness_open", "harness", "closed"), ("gates_open", "gates", "closed")):
                if st[dev].get("present", True) and st[dev].get(cond):
                    self._do(op, nl.station_op, ci, st["index"], op, quiet=True)
        elif not has:
            self.arrived = False
        action_active = pb("restraints") or now < self.release_until
        dev_pending = bool(st and ((st["platform"]["present"] and not st["platform"]["lowered"]) or
                                   (st["flyer"]["present"] and not st["flyer"]["locked"])))
        dispatch_ready = bool(self.ride_running and self.lift_running and has and locked and gates_ok
                              and not action_active and (st["canDispatch"] or dev_pending))
        # waiting brake -> station advance (manual block modes)
        wb = self.layout["before_station"] if self.layout else None
        if self.bc and wb in self.bc.by_sid:
            wb_can = self.bc.by_sid[wb].can_advance(False)
        else:
            wb_can = bool(cur_mode != "auto" and snap["detail"].get(wb, {}).get("canAdvanceFwd"))
        adv_ready = bool(self.ride_running and wb is not None and wb_can and not has)

        if panel:
            # ---- operating mode key -> game block mode (MANUAL / TRANSFER drop into full manual on their own
            #      while a device command or a transfer sequence needs it)
            want_mode = "fullmanual" if self.full_manual else \
                {"AUTO": "auto", "MANUAL": "manual", "TRANSFER": "manual"}[keys["mode"]]
            if cur_mode == want_mode:
                al.clear("MODE")
            elif now >= self.next_mode_try:
                self.next_mode_try = now + (0.5 if self.xseq or self.dev_pending else 2.0)
                step = "manual" if want_mode == "fullmanual" and cur_mode == "auto" else want_mode
                if cur_mode == "fullmanual" and step != "fullmanual":
                    self._lash_fix(nl, snap)
                try:
                    nl.set_block_mode(ci, step)
                    al.clear("MODE")
                except NL2Error as e:
                    al.set("MODE", f"{keys['mode']}: game refused {step} block mode - {e}", "warn")
            # ---- full-manual device commands (MAINTENANCE page) once the game is in full manual
            if self.dev_pending and cur_mode == "fullmanual":
                for sid_, dev, val, _t in self.dev_pending:
                    if dev == "transport" and val:
                        for tr in snap["trains"]:
                            if tr.get("lashed") and sid_ in tr["sections"]:
                                self._do("Unlash train", nl.lash_train, ci, tr["index"], False)
                    if not self._do(f"{dev.capitalize()} {self.lab(sid_)}", nl.set_device, ci, sid_, dev, val):
                        al.pulse(f"{dev.capitalize()} command refused by the game", "warn")
                self.dev_pending = []
            elif self.dev_pending and now - self.dev_pending[0][3] > 6.0:
                self.dev_pending = []
                al.pulse("Device command dropped - the game did not enter full manual", "warn")
            # ---- ADVANCE & DISPATCH: hold to dispatch (NL2_Manager: 1.5 s), or advance the waiting brake
            if ad_held:
                first = self.ad_since is None
                self.ad_since = self.ad_since or now
                if dispatch_ready:
                    if dev_pending and not self.dev_sent:
                        self.dev_sent = True
                        if st["platform"]["present"] and not st["platform"]["lowered"]:
                            self._do("Floor", nl.station_op, ci, st["index"], "platform_lower")
                        if st["flyer"]["present"] and not st["flyer"]["locked"]:
                            self._do("Flyer", nl.station_op, ci, st["index"], "flyer_lock")
                    if not self.ad_done and not dev_pending and st["canDispatch"] and \
                            now - self.ad_since >= float(S.get("dispatch_hold_s", 1.5)):
                        self.ad_done = bool(self._do("Dispatch", nl.dispatch, ci, st["index"]))
                elif adv_ready and not self.ad_done:
                    self.ad_done = self._advance(nl, wb, False)
                elif first and not self.ad_done:
                    al.pulse("Dispatch refused: " + self._why_not(st, cur_mode, system_active, has, locked, gates_ok))
            else:
                self.ad_since, self.ad_done, self.dev_sent = None, False, False

            # ---- RESTRAINTS button: lock restraints and close the gates (Station_Manager / NL2_Manager)
            if "restraints" in presses:
                if not has:
                    al.pulse("Restraints: no train in the station")
                else:
                    self._do("Restraints", nl.station_op, ci, st["index"], "harness_close")
                    if st["gates"]["present"] and not st["gates"]["closed"]:
                        self._do("Gates", nl.station_op, ci, st["index"], "gates_close")

            for it in intents:
                self._intent(nl, it, keys, scripted, cur_mode, hmi_ok, st, has)
        else:
            for it in intents:
                if it[0] in ("station_sel", "add_riders", "rider_up", "rider_down", "silence"):
                    self._intent(nl, it, keys, scripted, cur_mode, hmi_ok, st, has)
                else:
                    al.pulse("Panel is off - turn the PANEL ENABLE key on")
                    break

        # ---- lamps (Output_Controller)
        run, s10 = self.ride_running, self.seq == 10
        lamps["ride_start"] = ON if run or (pb("ride_start") and self.seq in (20, 40)) else \
            FLASH if system_active or self.seq in (10, 20, 40) else OFF
        lamps["ride_stop"] = FLASH_STOP if run else ON if system_active else FLASH_STOP if s10 else OFF
        lamps["lift_start"] = ON if self.lift_running else FLASH if run or s10 else OFF
        lamps["lift_stop"] = FLASH_STOP if self.lift_running else ON if run else FLASH_STOP if s10 else OFF
        if s10:
            lamps["restraints"] = FLASH
        elif run and parked:
            lamps["restraints"] = FAST if st["harness"].get("moving") else ON if locked else FLASH
        if s10:
            ad = FLASH
        elif not run:
            ad = OFF
        elif dispatch_ready:
            ad = ON if ad_held else FLASH
        elif adv_ready:
            ad = FLASH if ad_held else ON
        else:
            ad = OFF
        lamps["ad_l"] = lamps["ad_r"] = ad
        if s10:
            lamps["estop"] = lamps["estop_reset"] = FLASH_STOP
        elif estop_pb or not loto_ok or self.seq == 50:
            lamps["estop"] = ON
            lamps["estop_reset"] = FLASH_STOP if self.seq == 50 and not estop_pb and loto_ok else OFF
        elif system_active:
            lamps["estop"] = FLASH_STOP
        lamps["ack"] = FLASH if s10 or (self.faulted and keys["maint"]) else \
            ON if "ack" in held and self.seq != 20 else OFF
        lamps["hmi_enable"] = ON if (hmi_ok and S.get("require_hmi_enable", True) and panel) else OFF
        lamps["beeper"] = FLASH_STOP if s10 or (self.faulted and not self.silenced) else \
            ON if self.seq == 20 and pb("ride_start") else \
            FLASH_STOP if self.seq == 40 and pb("ride_start") else FLASH if now < self.lift_beep_until else OFF

        # ---- lift -> block brake timer
        tf, tt = self.timer_sids
        if tf is not None and tt is not None:
            for tr in snap["trains"]:
                i, on_from = tr["index"], tf in tr["sections"]
                if self.timer_prev.get(i) and not on_from:
                    self.timer_run[i] = now
                self.timer_prev[i] = on_from
                if i in self.timer_run and tt in tr["sections"]:
                    self.timer_last = now - self.timer_run.pop(i)
                    tm = self.cfg["hmi"].get("timer") or {}
                    if tm.get("min") is not None and self.timer_last < tm["min"]:
                        self.run_warn = (now + 30, f"Run time too fast: {self.timer_last:.0f} s")
                    elif tm.get("max") is not None and self.timer_last > tm["max"]:
                        self.run_warn = (now + 30, f"Run time too slow: {self.timer_last:.0f} s")
            self.timer_run = {i: t0 for i, t0 in self.timer_run.items() if now - t0 < 600}
        timer = now - max(self.timer_run.values()) if self.timer_run else self.timer_last

        # ---- warnings (Warning_Manager) + startup prompts
        warns = []
        if now < self.run_warn[0]:
            warns.append(self.run_warn[1])
        if panel and self.seq != 60:
            warns.append(STARTUP_TEXT[self.seq])
        if not daily_ok:
            warns.append("Daily test has not been completed")
        if self.nl2_minimized:
            warns.append("NoLimits 2 is minimized - the simulation is paused")
        if run and keys["mode"] == "AUTO" and not self.lift_running:
            warns.append("Lift is not running - press LIFT START")

        # ---- events: in-game Advance buttons feed the scripted controller; everything goes to the event log
        for ev in snap["events"]:
            if ev.get("coaster") != ci:
                continue
            self.events.appendleft((time.time(), ev))
            if ev["typeName"] in ("advanceFwdPressed", "advanceBwdPressed") and self.bc:
                self.bc.on_advance(ev["sectionId"], ev["typeName"] == "advanceBwdPressed")

        # ---- scripted block system
        if scripted:
            if self.bc is None and self.cfg and not al.items.get("CFG", {}).get("active"):
                try:
                    self.bc = ScriptedBlockController(self, self.info)
                    al.pulse(f"Scripted block controller running ({len(self.bc.blocks)} blocks, "
                             f"{os.path.basename(self.bc.path)})")
                except (OSError, ValueError, KeyError, TypeError) as e:
                    al.set("CFG", f"Ride config error: {e}", "fault")
            if self.bc:
                self.bc.scan(nl, ci, snap, self.ride_running and not estop, dt)
        else:
            self.bc = None

        parked_train = next((tr["index"] + 1 for tr in snap["trains"] if has and sid in tr["sections"]), 0)
        hmi = self.cfg["hmi"] if self.cfg else {}
        rj = snap.get("rows") or {}
        if rj.get("hasTrain") and rj.get("rows"):
            self.n_rows = len(rj["rows"])          # every train of a coaster has the same cars
        with self.lock:
            self.lamps = lamps
            self.view = dict(
                connected=True, snap=snap, scripted=scripted, mode=cur_mode, keys=keys, panel=panel,
                ride_running=self.ride_running, lift_running=self.lift_running, estop=estop, estop_pb=estop_pb,
                game_estop=game_estop, lockout=not loto_ok, seq=self.seq, system_active=system_active,
                startup=STARTUP_TEXT.get(self.seq, ""), faulted=self.faulted, fault_text=self.fault_text,
                silenced=self.silenced, warnings=warns, daily_ok=daily_ok, daily_active=self.daily_active,
                daily_checks=[(code, text, code in self.daily_checks) for code, text in DAILY_CHECKS],
                hmi_ok=hmi_ok, station_sel=self.station_sel, alarms=al.rows(), log=list(al.log),
                msg=al.last_msg, xfer_ilk=list(self.xfer_ilk), events=list(self.events)[:60],
                full_manual=self.full_manual, ad_held=ad_held, dispatch_ready=dispatch_ready,
                adv_ready=adv_ready, layout=self.layout, hmi=hmi,
                stats=self.stats.view(now) if self.stats else {}, timer=timer, parked_train=parked_train,
                n_rows=self.row_count(), riders_cap=self.row_count() * int(hmi.get("seats_per_row", 4)),
                bc_states={b.sid: self.bc.state_text(b.sid) for b in self.bc.blocks} if self.bc else {},
                bc_path=self.cfg_path, xseq=self._xfer_view(), xfer=self._xfer_options(snap, st),
                reset_armed=now < self.reset_armed, nl2_minimized=self.nl2_minimized,
                game_auto_dispatch=self.game_auto_dispatch,
                status=f"{c['name']} - {'scripted' if scripted else 'normal'} operation")

    def _why_not(self, st, mode, system_active, has, locked, gates_ok):
        if not system_active:
            return STARTUP_TEXT.get(self.seq) or "ride not armed"
        if self.faulted:
            return "fault active"
        if not self.ride_running:
            return "ride not started (RIDE START)"
        if not self.lift_running:
            return "lift not running (LIFT START)"
        if not st:
            return "no station"
        if not has:
            return "no train in the station"
        if not locked:
            n = (st.get("harness") or {}).get("rowsOpen", 0)
            return f"{n} row(s) released - press RESTRAINTS" if n else "restraints not locked (RESTRAINTS)"
        if not gates_ok:
            return "gates not closed"
        return self._why_no_dispatch(st, mode)

    @staticmethod
    def _why_no_dispatch(st, mode):
        if not st["manualDispatch"]:
            return "station is in automatic dispatch"
        if not st["hasTrain"]:
            return "no train in the station"
        if st["gates"]["present"] and not st["gates"]["closed"]:
            return "gates not closed"
        if not st["harness"]["closed"]:
            return "restraints not closed and locked"
        if mode == "fullmanual":
            return "full manual mode"
        return "next block not clear / train not ready"

    def _advance(self, nl, sid, bwd):
        if self.bc:
            if self.bc.on_advance(sid, bwd):
                return True
            self.alarms.pulse("Advance refused: block not waiting for advance or next block occupied")
            return False
        ok = self._do("Advance", nl.advance, self.ci, sid, bwd)
        if ok is False or ok == 0:
            self.alarms.pulse("Advance refused by the game (block can not advance now)")
            return False
        return True

    def _intent(self, nl, it, keys, scripted, mode, hmi_ok, st, has):
        al, ci = self.alarms, self.ci
        kind = it[0]
        if kind == "station_sel":
            self.station_sel = it[1]
            return
        if kind in ("add_riders", "rider_up", "rider_down"):
            if self.stats:
                hmi = self.cfg["hmi"]
                self.stats.add_riders({"add_riders": 5, "rider_up": 1, "rider_down": -1}[kind],
                                      self.row_count() * int(hmi.get("seats_per_row", 4)))
            return
        if kind == "silence":
            self.silenced = True
            return
        if kind == "daily_start":
            if self.seq == 15:
                self.daily_active, self.daily_checks = True, set()
                al.pulse("Daily test started - operate every device in the list")
            else:
                al.pulse("The daily test runs during startup, after the lamp test")
            return
        if kind == "full_manual":
            if it[1] and not keys["maint"] and keys["mode"] == "AUTO":
                al.pulse("Full manual needs OPERATION MODE = MANUAL / TRANSFER or the MAINTENANCE ENABLE key")
            elif self.xseq:
                al.pulse("A transfer sequence is running - wait for it or press STOP")
            else:
                self.full_manual = bool(it[1])
                self.next_mode_try = 0.0
            return
        if kind == "xfer_stop":
            if self.xseq:
                self._xfer_abort(nl, "STOP pressed")
            return
        if kind == "game_auto_dispatch":
            if not hmi_ok:
                al.pulse("Press HMI ENABLE first")
                return
            self.game_auto_dispatch = not self.game_auto_dispatch
            self.next_disp_try = 0.0
            al.pulse("GAME AUTO DISPATCH ON - the game dispatches trains by itself" if self.game_auto_dispatch else
                     "GAME AUTO DISPATCH OFF - stations in manual dispatch, the controller dispatches")
            return
        if kind == "sim_reset":
            self._sim_reset(nl)     # guarded by its own confirm press, no HMI ENABLE needed
            return
        if not hmi_ok:
            al.pulse("Press HMI ENABLE first")
            return
        if kind in ("xfer_advance", "xfer_reverse"):
            self._xfer_start(nl, kind == "xfer_reverse", keys, mode, st)
            return
        if kind in ("release", "harness_open"):
            row = it[1] if kind == "release" and len(it) > 1 and it[1] is not None else None
            if not has:
                al.pulse("Release restraints: no train in the station")
            elif not self.ride_running:
                al.pulse("Release restraints: ride not running")
            elif row is not None and self.rows_api:
                self.release_until = time.time() + 0.6
                self._do(f"Release row {row + 1}", nl.open_row, ci, st["index"], row + 1)
            else:
                if row is not None:
                    al.pulse("Bridge has no per-row restraints (needs NL2Bridge v4) - releasing all rows")
                self.release_until = time.time() + 0.6
                self._do("Restraints", nl.station_op, ci, st["index"], "harness_open")
            return
        toggles = {"gates_toggle": ("gates", "open", "gates_close", "gates_open"),
                   "floor_toggle": ("platform", "raised", "platform_lower", "platform_raise"),
                   "flyer_toggle": ("flyer", "locked", "flyer_unlock", "flyer_lock")}
        if kind in toggles:
            dev, state, op_on, op_off = toggles[kind]
            if not st or not st[dev].get("present"):
                al.pulse(f"This station has no {dev}")
                return
            if kind == "gates_toggle":
                if not self.ride_running:
                    al.pulse("Gates: ride not running")
                    return
                if keys["mode"] == "AUTO" and not has and not st["gates"]["open"]:
                    al.pulse("Gates: no train in the station (AUTO)")
                    return
            kind = op_on if st[dev].get(state) else op_off
        if kind in ("gates_open", "gates_close", "platform_raise", "platform_lower", "flyer_lock", "flyer_unlock",
                    "harness_close"):
            if st:
                self._do(kind.replace("_", " ").capitalize(), nl.station_op, ci, st["index"], kind)
        elif kind == "switch":
            bad = next((lab for lab, ok in self.xfer_ilk if not ok), None)
            if bad:
                al.pulse("Transfer refused - interlock not made: " + bad)
            elif self.xseq:
                al.pulse("Transfer refused - a train move is running")
            else:
                self._do("Transfer", nl.set_switch, ci, it[1], it[2])
        elif kind == "advance":
            if mode == "auto":
                al.pulse("Advance needs OPERATION MODE = MANUAL or TRANSFER")
            elif not self.ride_running:
                al.pulse("Advance: ride not running")
            else:
                self._advance(nl, it[1], it[2])
        elif kind in ("brakes", "lift", "transport") and not scripted:
            # normal operation: drive the section device like the game's full-manual panel (API v5)
            val = {"brakes": {"open": 0, "closed": 1}, "lift": {"off": 0, "fwd": 1},
                   "transport": {"off": 0, "fwd": 1, "bwd": 2}}[kind].get(it[2])
            dev = {"brakes": "brake"}.get(kind, kind)
            if keys["mode"] == "AUTO" and not keys["maint"]:
                al.pulse("Device control needs OPERATION MODE = MANUAL / TRANSFER or the MAINTENANCE ENABLE key")
            elif self.xseq:
                al.pulse("A transfer sequence is running - wait for it or press STOP")
            elif val is None:
                al.pulse(f"{kind.capitalize()} '{it[2]}' is not available in full manual")
            else:
                if not self.full_manual:
                    al.pulse("Full manual - the block system is off, you are driving the devices")
                self.full_manual = True
                self.next_mode_try = min(self.next_mode_try, time.time())
                self.dev_pending.append((it[1], dev, val, time.time()))
        elif kind in ("brakes", "lift", "transport"):
            if not keys["maint"]:
                al.pulse("Direct device control needs the MAINTENANCE ENABLE key")
            elif mode != "fullmanual":
                al.pulse("Select FULL MANUAL first (the block controller owns the devices otherwise)")
            else:
                fn = {"brakes": nl.set_brakes, "lift": nl.set_lift, "transport": nl.set_transport}[kind]
                if not self._do(kind.capitalize(), fn, ci, it[1], it[2]):
                    al.pulse(f"{kind.capitalize()} command refused by the game")

    # ---------------------------------------------------------------- helpers
    def lab(self, sid):
        return ((self.layout or {}).get("labels") or {}).get(sid) or short_name(
            next((s["name"] for s in (self.info or {}).get("sections", []) if s["id"] == sid), str(sid)))

    def _sim_reset(self, nl):
        """SIMULATION RESET (the in-game panel's reset key): every train back to its start position."""
        al, now = self.alarms, time.time()
        if now >= self.reset_armed:
            self.reset_armed = now + 3.0
            al.pulse("SIMULATION RESET: press again within 3 s to return every train to its start position", "warn")
            return
        self.reset_armed = 0.0
        if restore_nl2_window():
            al.pulse("NoLimits 2 was minimized - restored it so the simulation can run")
        if self.xseq:
            self._xfer_abort(nl, "simulation reset", quiet=True)
        if self._do("Simulation reset", nl.reset_coaster, self.ci):
            self.reset_quiet = time.time() + 4.0  # the game drops 'ready' for a moment while it resets
            self.full_manual, self.dev_pending = False, []
            self.ride_running = self.lift_running = False
            self.arrived, self.bc, self.next_mode_try = False, None, time.time() + 1.0
            self.timer_run, self.timer_prev = {}, {}
            al.pulse("SIMULATION RESET - trains returned to their start positions; press RIDE START", "warn")

    # ---------------------------------------------------------------- transfer: ADVANCE / REVERSE TRAIN
    # Normal-operation coasters only. The game's manual block mode never centres a train on a transfer table, so the
    # moves run in full manual with the section devices (brakes open + transport wheels), stopping on the game's own
    # centre sensors of the destination block, then hand back to manual block mode.
    def _xcfg(self):
        return (self.cfg or {}).get("transfer") or {}

    def _storage_map(self):
        by_name = {s["name"]: s["id"] for s in (self.info or {}).get("sections", [])}
        out = {}
        for k, v in (self._xcfg().get("storage") or {}).items():
            sid = by_name.get(v) if isinstance(v, str) else v
            if sid is not None and str(k).lstrip("-").isdigit():
                out[int(k)] = sid
        return out

    def _learn_join(self, snap):
        """Learn where the transfer table joins the circuit: a train driving forward off the table onto the main
        track enters section B first, so the table sits right before B. Moves the HMI's Transfer spot there."""
        g = self.xgeo
        if not g or g["xfer"] is None or not self.cfg or not self.layout or join_from_tracks(self.info) is not None:
            return
        main, order = main_track_order(self.info)
        on_main = {s["id"]: s for s in order}
        for tr in snap["trains"]:
            front = tr.get("front") or {}
            if g["xfer"] not in tr["sections"] or front.get("track") != main or tr["speed"] < 0.3:
                continue
            cand = [on_main[s] for s in tr["sections"] if s in on_main and on_main[s]["trackStart"] <= front["pos"]]
            if not cand:
                continue
            b = min(cand, key=lambda s: s["trackStart"])
            xc = self.cfg.setdefault("transfer", {})
            if b["id"] != self.layout.get("join"):
                xc["joins_before"] = b["name"]
                self._save_cfg()
                self.layout = hmi_layout(self.info, self.cfg["hmi"], xc)
                self.alarms.pulse(f"Transfer position on the track learned: before {self.lab(b['id'])}")
            return

    def _xfer_learn(self, snap, cur_mode):
        g, xc = self.xgeo, self._xcfg()
        if g["table"] is None or g["xfer"] is None or not self.cfg or xc.get("main") is not None:
            return
        if g["table"] >= len(snap["switches"]):
            return
        sw = snap["switches"][g["table"]]
        if sw["moving"] or sw["current"] < 0:
            return
        st_sids = {s["sectionId"] for s in snap["stations"]}
        straddle = any(g["xfer"] in tr["sections"] and st_sids & set(tr["sections"]) for tr in snap["trains"])
        if straddle or cur_mode == "auto":
            self.cfg["transfer"]["main"] = sw["current"]
            self._save_cfg()
            self.alarms.pulse(f"Transfer table main-line position learned: POS {sw['current'] + 1}")

    def _xfer_options(self, snap, st):
        """What ADVANCE / REVERSE TRAIN would do right now (text for the HMI), or the reason they can't."""
        g = self.xgeo
        if g["xfer"] is None or g["table"] is None or g["table"] >= len(snap["switches"]):
            return dict(present=False)
        det = snap["detail"]
        occ = lambda sid: bool(sid is not None and det.get(sid, {}).get("occupied"))  # noqa: E731
        sw = snap["switches"][g["table"]]
        main, smap = self._xcfg().get("main"), self._storage_map()
        st_sid = st["sectionId"] if st else None
        cur = sw["current"]
        adv = rev = None
        if main is not None and not sw["moving"]:
            if cur == main:
                if occ(st_sid) and not occ(g["xfer"]):
                    adv = "Station > transfer table"
                if occ(g["xfer"]) and not occ(st_sid):
                    rev = "Transfer table > station"
            elif cur >= 0:
                tgt = smap.get(cur)
                if occ(g["xfer"]) and not (tgt is not None and occ(tgt)):
                    adv = f"Transfer table > {self.lab(tgt)}" if tgt is not None else "Transfer table > storage"
                if tgt is not None and occ(tgt) and not occ(g["xfer"]):
                    rev = f"{self.lab(tgt)} > transfer table"
        return dict(present=True, main=main, storage={k: self.lab(v) for k, v in smap.items()}, advance=adv,
                    reverse=rev, table=g["table"], xfer=g["xfer"])

    def _lash_fix(self, nl, snap):
        """The game only leaves full manual when every train standing on a storage track is 'Lashed To Track' and
        no other train is - set the flags the way the game wants them (stationary trains only)."""
        storage = set(self.xgeo.get("storage") or [])
        if not storage or "lashed" not in (snap["trains"][0] if snap["trains"] else {}):
            return
        for tr in snap["trains"]:
            if abs(tr["speed"]) >= 0.03:
                continue
            want = bool(tr["sections"]) and set(tr["sections"]) <= storage
            if bool(tr["lashed"]) != want:
                self._do("Lash train" if want else "Unlash train", nl.lash_train, self.ci, tr["index"], want,
                         quiet=True)

    def _xfer_view(self):
        x = self.xseq
        return dict(name=x["name"], stage=x["stage"]) if x else None

    def _xfer_start(self, nl, bwd, keys, cur_mode, st):
        al, g, snap = self.alarms, self.xgeo, self._snap
        what = "REVERSE TRAIN" if bwd else "ADVANCE TRAIN"

        def no(why):
            al.pulse(f"{what} refused: {why}")
        if self.xseq:
            return no("a train move is already running")
        if snap["coaster"]["operationMode"] == 2:
            return no("scripted operation - use the block controller")
        if g["xfer"] is None or g["table"] is None or g["table"] >= len(snap["switches"]):
            return no("this coaster has no transfer table")
        if keys["mode"] != "TRANSFER":
            return no("OPERATION MODE must be TRANSFER")
        if not self.ride_running:
            return no("ride not running (RIDE START)")
        if snap["coaster"]["estop"] or not snap["coaster"]["ready"]:
            return no("emergency stop / coaster needs a reset")
        if cur_mode not in ("manual", "fullmanual"):
            return no("block system not in manual yet")
        if not all(abs(tr["speed"]) < 0.05 for tr in snap["trains"]):
            return no("a train is moving")
        sw = snap["switches"][g["table"]]
        if sw["moving"] or sw["current"] < 0:
            return no("the transfer table is moving")
        main = self._xcfg().get("main")
        if main is None:
            return no("transfer table main-line position not known yet - run the ride in AUTO once "
                      "(or set transfer.main in the ride file)")
        det = snap["detail"]
        occ = lambda sid: bool(sid is not None and det.get(sid, {}).get("occupied"))  # noqa: E731
        xf, cur, smap = g["xfer"], sw["current"], self._storage_map()
        st_sid = st["sectionId"] if st else None
        tgt = smap.get(cur)
        if not bwd and cur == main:
            if not occ(st_sid):
                return no("no train in the station")
            if occ(xf):
                return no("the transfer table is occupied")
            if st["platform"]["present"] and not st["platform"]["lowered"]:
                return no("the floor is raised")
            if st["flyer"]["present"] and not st["flyer"]["locked"]:
                return no("the flyer seats are unlocked")
            plan = dict(name="Station > transfer table", drive=[st_sid, xf], dirv=1, stop="center_fwd", target=xf,
                        clear=[st_sid])
        elif not bwd:
            if not occ(xf):
                return no("no train on the transfer table")
            if tgt is not None and occ(tgt):
                return no(f"{self.lab(tgt)} is occupied")
            cands = [tgt] if tgt is not None else [s for s in g["storage"] if not occ(s)]
            if not cands:
                return no("no empty storage track")
            plan = dict(name=f"Transfer table > {self.lab(tgt) if tgt is not None else 'storage'}",
                        drive=[xf] + cands, dirv=1, stop="storage", target=tgt, cands=cands, clear=[xf])
        elif cur == main:
            if not occ(xf):
                return no("no train on the transfer table")
            if occ(st_sid):
                return no("the station is occupied")
            plan = dict(name="Transfer table > station", drive=[xf, st_sid], dirv=2, stop="center_bwd",
                        target=st_sid, clear=[xf])
        else:
            if occ(xf):
                return no("the transfer table is occupied")
            if tgt is None:
                return no(f"storage track for table POS {cur + 1} not known yet - put a train into it with "
                          "ADVANCE TRAIN first (or set transfer.storage in the ride file)")
            if not occ(tgt):
                return no(f"no train on {self.lab(tgt)}")
            plan = dict(name=f"{self.lab(tgt)} > transfer table", drive=[tgt, xf], dirv=2, stop="center_bwd",
                        target=xf, clear=[tgt])
        self.xseq = dict(plan, stage="mode", t0=time.time(), pos=cur, seen_bc=False, clear_t=None)
        self.full_manual, self.next_mode_try = True, 0.0
        al.pulse(f"{what}: {plan['name']}")

    def _xfer_devices(self, nl, x, run):
        blocks = self._snap["blocks"]
        for sid in x["drive"]:
            b = blocks.get(sid, {})
            if b.get("hasBrake"):
                self._do("Brake", nl.set_device, self.ci, sid, "brake", 0 if run else 1, quiet=not run)
            if b.get("hasTransport"):
                self._do("Transport", nl.set_device, self.ci, sid, "transport", x["dirv"] if run else 0,
                         quiet=not run)

    def _xfer_abort(self, nl, why, quiet=False):
        x, self.xseq = self.xseq, None
        if x and x["stage"] in ("move", "settle"):
            self._xfer_devices(nl, x, False)
        elif x and x["stage"] == "mode":
            self.full_manual = False
        if not quiet:
            self.alarms.pulse(f"Train move stopped ({why}) - check the train position; use the MAINTENANCE "
                              "page (full manual) to finish by hand", "warn")

    def _xfer_arrived(self, x, det, now):
        occ = lambda sid: bool(det.get(sid, {}).get("occupied"))  # noqa: E731
        if any(occ(s) for s in x["clear"]):
            return False
        if x["stop"] == "storage":
            s = x["target"] if x["target"] is not None else next((c for c in x["cands"] if occ(c)), None)
            if s is None or not occ(s):
                return False
            x["target"] = s
            d = det.get(s, {})
            x["seen_bc"] = x["seen_bc"] or bool(d.get("beforeCenter"))
            x["clear_t"] = x["clear_t"] or now
            # storage block with centre sensing: stop at its centre; otherwise run on 2.5 s past the table
            return (x["seen_bc"] and not d.get("beforeCenter")) or now - x["clear_t"] >= 2.5
        d = det.get(x["target"], {})
        if not d.get("occupied"):
            return False
        return not d.get("beforeCenter") if x["stop"] == "center_fwd" else not d.get("behindCenter")

    def _xfer_run(self, nl, snap, now, cur_mode):
        x, al = self.xseq, self.alarms
        if x["stage"] == "mode":
            if cur_mode == "fullmanual":
                # a train parked on a storage track is 'Lashed To Track' - release it before driving it out
                for tr in snap["trains"]:
                    if tr.get("lashed") and set(tr["sections"]) & set(x["drive"]):
                        self._do("Unlash train", nl.lash_train, self.ci, tr["index"], False)
                self._xfer_devices(nl, x, True)
                x.update(stage="move", t0=now)
            elif now - x["t0"] > 8.0:
                why = (al.items.get("MODE") or {}).get("text", "game refused full manual")
                self.xseq, self.full_manual = None, False
                al.pulse(f"Train move cancelled - {why}", "warn")
        elif x["stage"] == "move":
            if self._xfer_arrived(x, snap["detail"], now):
                self._xfer_devices(nl, x, False)
                x.update(stage="settle", t0=now)
            elif now - x["t0"] > 90.0:
                self._xfer_abort(nl, "train did not arrive within 90 s")
        elif x["stage"] == "settle":
            if now - x["t0"] > 1.0 and all(abs(tr["speed"]) < 0.03 for tr in snap["trains"]):
                if x["stop"] == "storage" and x["target"] is not None:
                    # the game only leaves full manual once trains on storage tracks are 'Lashed To Track'
                    for tr in snap["trains"]:
                        if x["target"] in tr["sections"] and not tr.get("lashed"):
                            self._do("Lash train", nl.lash_train, self.ci, tr["index"], True)
                if x["stop"] == "storage" and x["target"] is not None and self.cfg:
                    name = next((s["name"] for s in self.info["sections"] if s["id"] == x["target"]), None)
                    if self.cfg["transfer"]["storage"].get(str(x["pos"])) != name:
                        self.cfg["transfer"]["storage"][str(x["pos"])] = name
                        self._save_cfg()
                self.full_manual, self.next_mode_try = False, 0.0
                x.update(stage="back", t0=now)
        elif x["stage"] == "back":
            if cur_mode == "manual":
                self.xseq = None
                al.pulse(f"Train move complete: {x['name']}")
            elif now - x["t0"] > 10.0:
                self.xseq = None
                al.pulse("Train parked, but the game has not returned to manual block mode yet", "warn")

    # ---------------------------------------------------------------- UI read
    def snapshot(self):
        with self.lock:
            return dict(self.view), dict(self.lamps)


# ============================================================================ link thread
class Link(threading.Thread):
    """Owns the NL2Bridge connection: polls, runs the PLC scan and reconnects when needed."""

    def __init__(self, logic, settings):
        super().__init__(daemon=True)
        self.logic, self.settings = logic, settings
        self.stop = False
        self.reconnect = False

    def run(self):
        while not self.stop:
            try:
                self.session()
            except (OSError, ConnectionError, NL2Error, ParkChanged, KeyError, IndexError, ValueError) as e:
                if self.stop:
                    break
                msg = "park or coaster changed" if isinstance(e, ParkChanged) else (str(e) or type(e).__name__)
                self.logic.on_disconnect(msg)
                time.sleep(0.5 if isinstance(e, ParkChanged) else 2.0)
            self.reconnect = False

    def session(self):
        s = self.settings
        with NL2Bridge(s.get("host", "127.0.0.1"), int(s.get("port", 15152)), timeout=3.0) as nl:
            coasters = nl.coasters()
            if not coasters:
                raise NL2Error("no coasters in the park")
            want, ci = str(s.get("coaster", "")).strip().lower(), 0
            for c in coasters:
                if want and (c["name"].lower() == want or str(c["index"]) == want):
                    ci = c["index"]
            info = nl.coaster_info(ci)
            self.rows_api = True
            self.dev_api, devices = True, {}
            info["coasters"] = [c["name"] for c in coasters]
            name, nsec = info["coaster"]["name"], len(info["sections"])
            seq = nl.events(0xFFFFFFFF)["seq"]
            self.logic.on_connect(ci, info)
            sig = block_signature(info)
            names = {sec["id"]: sec["name"] for sec in info["sections"]}
            sw_static = info["specialTracks"]
            by_id = {sec["id"]: sec for sec in info["sections"]}
            n, trains = 0, []
            period = 1.0 / float(s.get("scan_hz", 20))
            while not self.stop and not self.reconnect:
                t0 = time.time()
                cl = nl.coasters()
                if ci >= len(cl) or cl[ci]["name"] != name or cl[ci]["sections"] != nsec:
                    raise ParkChanged()
                _, _, bl = nl.block_states(ci)
                _, det = nl.section_detail(ci)
                if n % 40 == 1:
                    # section trackStart/trackEnd are clipped by trains on the section: only learn them while empty
                    occ = {d["id"] for d in det if d["occupied"]}
                    fresh = nl.coaster_info(ci)
                    if block_signature(fresh) != sig:
                        raise ParkChanged()
                    for sec in fresh["sections"]:
                        if sec["id"] not in occ and sec["id"] in by_id:
                            by_id[sec["id"]].update(track=sec["track"], trackStart=sec["trackStart"],
                                                    trackEnd=sec["trackEnd"])
                stations = nl.stations(ci)
                rows = None
                if self.rows_api and stations:
                    try:
                        rows = nl.rows(ci, stations[min(self.logic.station_sel, len(stations) - 1)]["index"])
                    except NL2Error as e:
                        if "Unknown message" in str(e):
                            self.rows_api = False     # pre-v4 bridge: Release Row falls back to open-all
                sw = nl.switch_states(ci)
                for a, b in zip(sw, sw_static):
                    a["name"], a["type"] = b["name"], b["type"]
                ev = nl.events(seq)
                seq = ev["seq"]
                if n % 4 == 0:
                    trains = nl.trains(ci)
                if BLOCK_MODE_NAMES.get(cl[ci]["blockMode"]) == "fullmanual" and self.dev_api:
                    if n % 10 == 0:
                        try:
                            devices = {b["id"]: nl.devices(ci, b["id"]) for b in bl
                                       if b.get("hasBrake") or b.get("hasLift") or b.get("hasTransport")}
                        except NL2Error as e:
                            if "Unknown message" not in str(e):
                                raise
                            self.dev_api, devices = False, {}
                else:
                    devices = {}
                n += 1
                snap = dict(coaster=cl[ci], info=info, names=names, blocks={b["id"]: b for b in bl},
                            detail={d["id"]: d for d in det}, stations=stations, switches=sw, trains=trains,
                            rows=rows, rows_api=self.rows_api, devices=devices, dev_api=self.dev_api,
                            events=ev["events"], events_enabled=ev.get("enabled", 0), t=t0)
                self.logic.scan(nl, snap)
                time.sleep(max(0.0, period - (time.time() - t0)))
            if self.stop or self.reconnect:
                self.logic.release(nl)
