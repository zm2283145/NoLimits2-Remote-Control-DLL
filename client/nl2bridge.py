"""NL2Bridge Python library - ride control for NoLimits 2 through the injected NL2Bridge.dll.

    from nl2bridge import NL2Bridge
    with NL2Bridge() as nl:
        c = nl.coaster("Fury 325")                 # or an index
        nl.set_block_mode(c, "manual")             # auto | manual | fullmanual
        for b in nl.blocks(c): print(b["name"], b["stateName"])
        st = nl.station_status(c, 0)               # index or name of the station section
        nl.set_manual_dispatch(c, 0, True)
        nl.open_gates(c, 0); nl.close_restraints(c, 0); nl.dispatch(c, 0)
        nl.set_switch(c, "Unnamed Special Track 1", 2)

Blocks, stations, special tracks and sensors can be addressed by name (case-insensitive) or number.
Station operations raise NL2Error with the game's reason when they are refused (e.g. "Station is in
automatic dispatch mode", "No train stopped in the station").
"""
import json, socket, struct

DEFAULT_PORT = 15152
R_OK, R_ERROR, R_INT, R_STRING = 1, 2, 8, 10

BLOCK_STATES = ["No Block", "Offline", "Idle", "Reserved", "Approaching Fwd", "Approaching Bwd", "Leaving Fwd",
                "Leaving Fwd to Ourself", "Leaving Bwd", "Leaving Bwd to Ourself", "Full Manual Mode",
                "Passing Fwd to Trigger", "Passing Bwd to Trigger", "Station", "Waiting for Clear Block",
                "Waiting for Advance", "Complete Stopping", "Wait-Time Pause", "Pass Through", "Scripted"]
BLOCK_MODES = {"auto": 0, "manual": 1, "fullmanual": 2}
BLOCK_MODE_NAMES = {1: "auto", 2: "manual", 3: "fullmanual"}      # value reported in coaster JSON "blockMode"

STATION_OPS = {"manual": 0, "auto": 1, "dispatch": 2, "gates_open": 3, "gates_close": 4,
               "harness_open": 5, "harness_close": 6, "platform_raise": 7, "platform_lower": 8,
               "flyer_unlock": 9, "flyer_lock": 10}
STATION_FLAGS = ["estop", "manual", "canDispatch", "canCloseGates", "canOpenGates", "canCloseHarness",
                 "canOpenHarness", "canRaisePlatform", "canLowerPlatform", "canLockFlyer", "canUnlockFlyer",
                 "trainReady", "gatesOpen", "gatesClosed", "harnessOpen", "harnessClosed", "platformRaised",
                 "platformLowered", "flyerUnlocked", "flyerLocked", "hasGates", "hasPlatform", "hasFlyer", "hasTrain",
                 "rowsOpen", "customTrain"]

BRAKE_CMDS = {"open": 7, "off": 7, "closed": 8, "on": 8, "trim": 12}
LIFT_CMDS = {"fwd": 23, "bwd": 24, "off": 25, "idle": 41}
TRANSPORT_CMDS = {"off": 9, "fwd": 10, "bwd": 11, "fwdbrake": 13, "bwdbrake": 14, "launchfwd": 17, "launchbwd": 18}
SCRIPTED_BLOCK_CMDS = {"advance_fwd_visible": 1, "advance_bwd_visible": 2,
                       "advance_fwd_enabled": 3, "advance_bwd_enabled": 4}
LAMPS = {"off": 0, "on": 1, "flash": 2, "flashing": 2}
SECTION_DETAIL_FLAGS = ["beforeCenter", "behindCenter", "beforeBrakeTrigger", "behindBrakeTrigger",
                        "beforeLiftTrigger", "behindLiftTrigger", "brakesOn", "stationWaitingForClearBlock",
                        "stationWaitingForAdvance", "isStation", "advFwdVisible", "canAdvanceFwd",
                        "advBwdVisible", "canAdvanceBwd", "scripted", "occupied"]


class NL2Error(RuntimeError):
    pass


def _flags(v, names):
    return {n: bool(v >> i & 1) for i, n in enumerate(names)}


class NL2Bridge:
    def __init__(self, host="127.0.0.1", port=DEFAULT_PORT, timeout=5.0):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._req = 0

    def close(self):
        self.s.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ---------------------------------------------------------------- transport
    def call(self, msg_id, data=b""):
        self._req += 1
        self.s.sendall(b"N" + struct.pack(">HIH", msg_id, self._req, len(data)) + data + b"L")
        hdr = self._recv(9)
        rid, _, size = struct.unpack(">HIH", hdr[1:])
        body = self._recv(size + 1)[:-1]
        if rid == R_ERROR:
            raise NL2Error(body.decode(errors="replace"))
        return rid, body

    def _recv(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                raise ConnectionError("NL2Bridge closed the connection")
            b += c
        return b

    def _json(self, msg_id, data=b""):
        return json.loads(self.call(msg_id, data)[1].decode())

    def _int(self, msg_id, data):
        return struct.unpack(">i", self.call(msg_id, data)[1])[0]

    # ---------------------------------------------------------------- name resolution
    @staticmethod
    def _pick(items, key, label, what, idx_field=None):
        if isinstance(key, int):
            for it in items:
                if it.get(idx_field or "index") == key:
                    return it
            raise NL2Error(f"No {what} {key}")
        k = str(key).strip().lower()
        hits = [it for it in items if str(it.get(label, "")).lower() == k]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise NL2Error(f"{what} name '{key}' is ambiguous - use a number")
        raise NL2Error(f"No {what} named '{key}'")

    def coaster(self, key):
        """Coaster index from an index or name."""
        return self._pick(self.coasters(), key, "name", "coaster")["index"]

    def block_id(self, c, key):
        """Section id from a section id or section name."""
        return self._pick(self.blocks(c), key, "name", "section", "id")["id"]

    def station_index(self, c, key):
        """Station index from an index or its section name."""
        return self._pick(self.stations(c), key, "name", "station")["index"]

    def switch_index(self, c, key):
        """Special track index from an index or name."""
        return self._pick(self.special_tracks(c), key, "name", "special track")["index"]

    def _c(self, c):
        return c if isinstance(c, int) else self.coaster(c)

    # ---------------------------------------------------------------- queries
    def ping(self):
        return self.call(1000)[0] == R_OK

    def bridge_info(self):
        """{name, api, build} (9 = sessions; 10 = manual parameters; 11 = brakes/signature 4; 12 = planned reconnect). Older bridges answer 'Unknown message' -> {'api': 0}."""
        try:
            return self._json(1001)
        except NL2Error:
            return {"name": "NL2Bridge", "api": 0, "build": "old"}

    def panel_reconnect_ticket(self, coaster):
        """API 12: prepare a one-use 500 ms ticket for the owning connection."""
        return bytes.fromhex(self._json(1155, struct.pack('>i', self._c(coaster)))['ticket'])

    def panel_resume(self, coaster, station_section, ticket):
        """API 12: resume only after the prior socket closes, before lease expiry."""
        if not isinstance(ticket, bytes) or len(ticket) != 16:
            raise ValueError('Reconnect ticket must contain 16 bytes')
        self.call(1156, struct.pack('>ii', self._c(coaster), int(station_section)) + ticket)

    def device_params(self, c, block):
        """Lift / transport parameters of a section (API 6): {'lift': {speed, accel, decel, idleMode, current} | None,
        'transport': {...} | None}. Speeds in m/s, accelerations in m/s^2. NL2's lift idle speed is fixed
        (speed * 0.5, clamped to 0.4 m/s)."""
        sid = block if isinstance(block, int) else self.block_id(c, block)
        return self._json(1208, struct.pack(">ii", self._c(c), sid))

    def set_device_params(self, c, block, device, speed=-1.0, accel=-1.0, decel=-1.0):
        """Change a lift's or transport's speed / acceleration / deceleration at runtime, in any block mode. The lift
        ramps to the new speed with its acceleration and its chain animation and sound follow.
        device: 'lift' | 'transport'. Values < 0 are left unchanged (API 6)."""
        d = self.DEVICES[device] if isinstance(device, str) else int(device)
        sid = block if isinstance(block, int) else self.block_id(c, block)
        self.call(1152, struct.pack(">iiiddd", self._c(c), sid, d, float(speed), float(accel), float(decel)))

    def coasters(self):
        return self._json(1200)

    def coaster_info(self, c):
        """Everything about one coaster: coaster, sections, stations, specialTracks."""
        return self._json(1204, struct.pack(">i", self._c(c)))

    def blocks(self, c):
        """All track sections with block state, lamp, trains, brake/lift/transport modes."""
        return self._json(1201, struct.pack(">i", self._c(c)))

    def block(self, c, key):
        c = self._c(c)
        return self._pick(self.blocks(c), key, "name", "section", "id")

    def block_states(self, c):
        """Fast binary poll: (mode, estop, [ {id, state, stateName, trains, lamp, ...} ])."""
        _, b = self.call(1210, struct.pack(">i", self._c(c)))
        mode, estop, n = struct.unpack(">BBH", b[:4])
        out = []
        for i in range(n):
            sid, st, trains, lamp, fl, brake, lift, tr, _, mask = struct.unpack(">IBBBBBBBBI", b[4 + i * 16:20 + i * 16])
            out.append(dict(id=sid, state=st, stateName=BLOCK_STATES[st] if st < len(BLOCK_STATES) else "-",
                            trains=trains, lamp=lamp, canAdvanceFwd=bool(fl & 1), canAdvanceBwd=bool(fl & 2),
                            isBlock=bool(fl & 4), scripted=bool(fl & 8), hasBrake=bool(fl & 0x10), hasLift=bool(fl & 0x20),
                            hasTransport=bool(fl & 0x40), brakeMode=brake, liftMode=lift, transportMode=tr,
                            trainMask=mask))
        return BLOCK_MODE_NAMES.get(mode, mode), bool(estop), out

    def stations(self, c):
        """Every station: name, state, hasTrain, train (index or -1), seats / seatedCars / seatsPerCar of that
        train (API 7+), customTrain (API 8+), gates, harness, floor, flyer, ..."""
        return self._json(1203, struct.pack(">i", self._c(c)))

    def station_status(self, c, station):
        c = self._c(c)
        return self._pick(self.stations(c), station, "name", "station")

    def station_states(self, c):
        """Fast binary poll of all stations: list of dicts of booleans (+ raw 'flags', 'state', 'rowsOpenCount'
        and 'seats' = seat capacity of the train in the station, 0 if none / unknown; API 7+)."""
        _, b = self.call(1212, struct.pack(">i", self._c(c)))
        n = struct.unpack(">H", b[:2])[0]
        out = []
        for i in range(n):
            flags, state, rows_open, seats = struct.unpack(">IBBH", b[2 + i * 8:10 + i * 8])
            d = _flags(flags, STATION_FLAGS)
            d.update(index=i, flags=flags, state=state, rowsOpenCount=rows_open, seats=seats)
            out.append(d)
        return out

    def special_tracks(self, c):
        return self._json(1202, struct.pack(">i", self._c(c)))

    def switch_states(self, c):
        _, b = self.call(1214, struct.pack(">i", self._c(c)))
        n = struct.unpack(">H", b[:2])[0]
        out = []
        for i in range(n):
            cur, tgt, dirs, fl = struct.unpack(">bbBB", b[2 + i * 4:6 + i * 4])
            out.append(dict(index=i, current=cur, target=tgt, directions=dirs, moving=bool(fl & 1),
                            switchable=bool(fl & 2), manualAllowed=bool(fl & 4), transferTable=bool(fl & 8)))
        return out

    def sensors(self, c=-1):
        return self._json(1300, struct.pack(">i", -1 if c == -1 else self._c(c)))

    def sensor(self, key, c=-1):
        rows = self.sensors(c)
        if isinstance(key, int):
            for r in rows:
                if r["key"] == key or r["id"] == key:
                    return r
            raise NL2Error(f"No sensor {key}")
        return self._pick(rows, key, "name", "sensor")

    def sensor_states(self, c=-1):
        """Fast binary poll: list of {key, active, lastTrain, trainMask, passes}."""
        _, b = self.call(1301, struct.pack(">i", -1 if c == -1 else self._c(c)))
        n = struct.unpack(">H", b[:2])[0]
        out = []
        for i in range(n):
            key, act, last, _, mask, passes = struct.unpack(">IBBHII", b[2 + i * 16:18 + i * 16])
            out.append(dict(key=key, active=bool(act), lastTrain=-1 if last == 255 else last, trainMask=mask, passes=passes))
        return out

    def trains(self, c):
        """Every train: index, blockId/blockName holding it, sections it occupies, station index (-1),
        speed (m/s), accel, harness/flyer position, front/center/rear {track, pos} along the track and
        seats / seatedCars / seatsPerCar (API 7+) and customTrain (API 8+: 1 when the train has no NL2 car model
        because a park script draws it; such trains report 0 seats)."""
        return self._json(1205, struct.pack(">i", self._c(c)))

    def section_detail(self, c):
        """Fast binary poll for block logic: (info, [per-section dict]). Position flags are the script API's
        Section.isTrainBefore/Behind* queries; userState is the scripted Block.getState() (-1 if not scripted)."""
        _, b = self.call(1216, struct.pack(">i", self._c(c)))
        op, mode, estop, _, n = struct.unpack(">BBBBH", b[:6])
        out = []
        for i in range(n):
            sid, f, tidx, st, us, ls, ts = struct.unpack(">IHbBiff", b[6 + i * 20:26 + i * 20])
            d = _flags(f, SECTION_DETAIL_FLAGS)
            d.update(id=sid, flags=f, trainIndex=tidx, state=st, userState=us, liftSpeed=ls, transportSpeed=ts)
            out.append(d)
        return dict(operationMode=op, scripted=op == 2, blockMode=BLOCK_MODE_NAMES.get(mode, mode), estop=bool(estop)), out

    def events(self, since=0):
        """Events after sequence number `since`: {enabled, seq, events:[{seq, coaster, type, typeName, ...}]}.
        typeName: sensorEnter/sensorLeave (sensorKey, sensorId, sensorName, train), modeAuto/modeManualBlock/
        modeFullManual, advanceFwdPressed/advanceBwdPressed (sectionId, sectionName). Pass the returned seq
        next time; use since=0xFFFFFFFF to just learn the current seq."""
        return self._json(1303, struct.pack(">I", since & 0xFFFFFFFF))

    # ---------------------------------------------------------------- coaster-wide control
    def set_block_mode(self, c, mode):
        """mode: 'auto' | 'manual' (manual block) | 'fullmanual'."""
        m = BLOCK_MODES[mode] if isinstance(mode, str) else mode
        if not self._int(1130, struct.pack(">ii", self._c(c), m)):
            raise NL2Error(f"Game refused block mode '{mode}' (trains must be in a suitable position)")

    def estop(self, c, on=True):
        self.call(1131, struct.pack(">iB", self._c(c), 1 if on else 0))

    def reset_coaster(self, c):
        """Full simulation reset of the coaster: trains return to their start positions (API v5)."""
        self.call(1132, struct.pack(">i", self._c(c)))

    def panel_session(self, c, station_section, *, enabled=True, suppress_messages=False, telemetry_port=15151):
        """API 9: claim/release this connection's scripted panel session.

        Optional message suppression uses NL2's licensed native Attraction Mode.
        The DLL restores messages on disconnect or a missing 750 ms heartbeat.
        """
        if not isinstance(station_section, int) or not 1 <= station_section <= 4095:
            raise ValueError("Invalid station section ID")
        if not isinstance(telemetry_port, int) or not 1 <= telemetry_port <= 65535:
            raise ValueError("Invalid native telemetry port")
        self.call(1153, struct.pack(">iiiii", self._c(c), station_section,
                                   int(bool(enabled)), int(bool(suppress_messages)), telemetry_port))

    def panel_session_status(self):
        """API 9: requested/active native suppression and any refusal reason."""
        return self._json(1154, b"")

    DEVICES = {"brake": 0, "lift": 1, "transport": 2}

    def devices(self, c, block):
        """Brake/lift/transport device state of a section: brake/lift 0 off 1 on, transport 0 off 1 fwd 2 bwd (API v5)."""
        return self._json(1207, struct.pack(">ii", self._c(c), self.block_id(c, block)))

    def set_device(self, c, block, device, value):
        """Drive a section device like the game's full-manual control panel (coaster must be in full manual)."""
        d = self.DEVICES[device] if isinstance(device, str) else int(device)
        return bool(self._int(1150, struct.pack(">iiii", self._c(c), self.block_id(c, block), d, int(value))))

    def lash_train(self, c, train, lashed=True):
        """Set a train's 'Lashed To Track' flag (Train.setLashedToTrack). The game only allows auto/manual block
        mode when every train on a storage track is lashed and no other train is (API v5c)."""
        self.call(1151, struct.pack(">iii", self._c(c), int(train), 1 if lashed else 0))

    # ---------------------------------------------------------------- blocks / sections
    def _section_cmd(self, c, block, cmd, param=0):
        c = self._c(c)
        sid = block if isinstance(block, int) else self.block_id(c, block)
        return bool(self._int(1100, struct.pack(">iiii", c, sid, cmd, param)))

    def section_set(self, c, block, cmd, param=0):
        """Raw NLSection command (see README)."""
        return self._section_cmd(c, block, cmd, param)

    def section_get(self, c, block, cmd, x=0.0):
        c = self._c(c)
        sid = block if isinstance(block, int) else self.block_id(c, block)
        _, b = self.call(1101, struct.pack(">iiid", c, sid, cmd, x))
        return struct.unpack(">iid", b)

    def set_brakes(self, c, block, mode):
        """mode: 'open' | 'closed' | 'trim'."""
        return self._section_cmd(c, block, BRAKE_CMDS[mode])

    def set_lift(self, c, block, mode):
        """mode: 'fwd' | 'bwd' | 'idle' | 'off'."""
        return self._section_cmd(c, block, LIFT_CMDS[mode])

    def set_transport(self, c, block, mode):
        """mode: 'off' | 'fwd' | 'bwd' | 'fwdbrake' | 'bwdbrake' | 'launchfwd' | 'launchbwd'."""
        return self._section_cmd(c, block, TRANSPORT_CMDS[mode])

    def advance(self, c, block, backwards=False):
        """Manual-block / scripted advance. Returns False if the block can not advance now."""
        c = self._c(c)
        sid = block if isinstance(block, int) else self.block_id(c, block)
        return bool(self._int(1110, struct.pack(">iii", c, sid, 2 if backwards else 1)))

    def scripted_block(self, c, block, cmd, value=True):
        """Scripted-mode block node: cmd in SCRIPTED_BLOCK_CMDS (advance button visibility / enable)."""
        c = self._c(c)
        sid = block if isinstance(block, int) else self.block_id(c, block)
        n = SCRIPTED_BLOCK_CMDS[cmd] if isinstance(cmd, str) else cmd
        return bool(self._int(1111, struct.pack(">iiii", c, sid, n, 1 if value else 0)))

    def register_state(self, c, block, state, text, lamp="on"):
        """Scripted mode: Block.registerState - text + lamp ('off'|'on'|'flash' or 0/1/2) shown on the ride panel."""
        c = self._c(c)
        sid = block if isinstance(block, int) else self.block_id(c, block)
        lp = LAMPS[lamp] if isinstance(lamp, str) else int(lamp)
        self.call(1112, struct.pack(">iiii", c, sid, int(state), lp) + str(text).encode())

    def set_state(self, c, block, state):
        """Scripted mode: Block.setState (state must have been registered)."""
        c = self._c(c)
        sid = block if isinstance(block, int) else self.block_id(c, block)
        self.call(1113, struct.pack(">iii", c, sid, int(state)))

    # scripted-mode station handler (Section.setStationEntering/Leaving/NextBlockClear/Occupied)
    def station_entering(self, c, block):      return self._section_cmd(c, block, 21)
    def station_leaving(self, c, block):       return self._section_cmd(c, block, 5)
    def station_next_clear(self, c, block):    return self._section_cmd(c, block, 2)
    def station_next_occupied(self, c, block): return self._section_cmd(c, block, 3)

    # ---------------------------------------------------------------- stations
    def station_op(self, c, station, op):
        """Run a station operation; raises NL2Error with the reason if the game would refuse it."""
        c = self._c(c)
        si = station if isinstance(station, int) else self.station_index(c, station)
        self.call(1141, struct.pack(">iii", c, si, STATION_OPS[op] if isinstance(op, str) else op))

    def set_manual_dispatch(self, c, station, manual=True):
        self.station_op(c, station, "manual" if manual else "auto")

    def dispatch(self, c, station):        self.station_op(c, station, "dispatch")
    def open_gates(self, c, station):      self.station_op(c, station, "gates_open")
    def close_gates(self, c, station):     self.station_op(c, station, "gates_close")
    def open_restraints(self, c, station): self.station_op(c, station, "harness_open")
    def close_restraints(self, c, station): self.station_op(c, station, "harness_close")
    def raise_floor(self, c, station):     self.station_op(c, station, "platform_raise")
    def drop_floor(self, c, station):      self.station_op(c, station, "platform_lower")
    def unlock_flyer(self, c, station):    self.station_op(c, station, "flyer_unlock")
    def lock_flyer(self, c, station):      self.station_op(c, station, "flyer_lock")

    # ---------------------------------------------------------------- per-row restraints (API v4)
    def rows(self, c, station):
        """Per-row restraint state of the train in a station: {rows:[{row, position, open, closed, ...}], rowsOpen, ...}.
        Row 1 is the front car; only cars with restraints count. API 7+ adds seats per row and per car,
        plus top-level seats / seatedCars."""
        c = self._c(c)
        si = station if isinstance(station, int) else self.station_index(c, station)
        return self._json(1206, struct.pack(">ii", c, si))

    def station_seats(self, c, station):
        """Seat capacity of the train in a station (API 7+): {train, seats, seatedCars, seatsPerCar, customTrain}.
        train is -1 and seats 0 when the station is empty. Seats are the car model's HEAD nodes, so a train
        drawn by a park script reports 0 seats and customTrain True (API 8+)."""
        s = self.station_status(c, station)
        out = {k: s.get(k, -1 if k == "train" else 0) for k in ("train", "seats", "seatedCars", "seatsPerCar")}
        out["customTrain"] = bool(s.get("customTrain", 0))
        return out

    def custom_train(self, c, station):
        """True when the train in the station is drawn by a park script instead of an NL2 car model (API 8+)."""
        return self.station_seats(c, station)["customTrain"]

    def set_row_restraint(self, c, station, row, open_):
        """Open (True) or close (False) one row's restraints (row 1 = front, 0 = every row).
        The game still sees the train harness as closed, so the bridge refuses dispatch while any row is open;
        close_restraints() closes the open rows too."""
        c = self._c(c)
        si = station if isinstance(station, int) else self.station_index(c, station)
        self.call(1142, struct.pack(">iiii", c, si, int(row), 1 if open_ else 0))

    def open_row(self, c, station, row):   self.set_row_restraint(c, station, row, True)
    def close_row(self, c, station, row):  self.set_row_restraint(c, station, row, False)

    # ---------------------------------------------------------------- switches / transfer tables
    def set_switch(self, c, track, direction):
        """Move a switch or transfer table to a direction (0..directions-1)."""
        c = self._c(c)
        i = track if isinstance(track, int) else self.switch_index(c, track)
        if not self._int(1120, struct.pack(">iii", c, i, int(direction))):
            raise NL2Error("Game refused the switch change (occupied, moving, or not switchable)")
