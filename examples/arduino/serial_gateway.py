"""NL2Bridge serial gateway - lets an Arduino (or any serial device) run NoLimits 2 through NL2Bridge.

The Arduino sends one-line text commands over USB serial ("ST 0 dispatch", "LIFTPOWER Lift 0" ...). This script
runs them through the NL2Bridge client and streams ride status back as text lines ("BLK ...", "STN ..."), sending
only what changed. The line protocol is documented in README.md next to this file.

    pip install pyserial
    python serial_gateway.py COM5                       # Arduino on COM5, game on this PC
    python serial_gateway.py COM5 --host 192.168.1.20   # game on another PC
    python serial_gateway.py COM5 --coaster "Fury 325" --log
    python serial_gateway.py --console                  # no Arduino: type the commands yourself to test

On exit (Ctrl+C) it hands the ride back: lift/transport speeds it changed are restored, stations it put in
manual dispatch go back to automatic, an E-stop it set is cleared, and a coaster it put in manual block mode
goes back to automatic once all trains have stopped on blocks (use --no-restore to skip).
"""
import argparse, os, queue, re, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "client"))
from nl2bridge import NL2Bridge, NL2Error  # noqa: E402

WATCH_ITEMS = ("mode", "blocks", "stations", "switches", "sensors", "trains", "rows", "events", "detail")
DEFAULT_WATCH = {"mode", "blocks", "stations", "switches", "events"}
TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def parse(line):
    """Split a command line: "quoted names" stay text, bare integers become int."""
    out = []
    for quoted, bare in TOKEN.findall(line):
        if bare and re.fullmatch(r"-?\d+", bare):
            out.append(int(bare))
        else:
            out.append(bare or quoted)
    return out


def onoff(v):
    s = str(v).lower()
    if s in ("1", "on", "true", "yes", "open"):
        return True
    if s in ("0", "off", "false", "no", "close", "closed"):
        return False
    raise ValueError(f"expected 1 or 0, got '{v}'")


def clean(s):
    return str(s if s is not None else "").replace("\r", " ").replace("\n", " ")


def f2(v):
    return f"{float(v or 0):.2f}"


class SerialLink:
    def __init__(self, port, baud):
        import serial
        self.serial, self.port, self.baud = serial, port, baud
        self.ser, self.buf, self.next_open, self.last_err, self.who_at, self.opened = None, b"", 0.0, None, 0.0, False

    def _open(self):
        if self.ser:
            return True
        if time.time() < self.next_open:
            return False
        try:
            self.ser = self.serial.Serial(self.port, self.baud, timeout=0, write_timeout=2)
            self.buf, self.last_err, self.who_at, self.opened = b"", None, time.time() + 2.5, True
            log(f"Serial {self.port} open at {self.baud} baud - waiting for the Arduino's HELLO")
            return True
        except (OSError, self.serial.SerialException) as e:
            if str(e) != self.last_err:
                log(f"Serial {self.port}: {e} (retrying every 2 s)")
                self.last_err = str(e)
            self.next_open = time.time() + 2.0
            return False

    def _drop(self, e):
        log(f"Serial {self.port} lost: {e}")
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser, self.next_open = None, time.time() + 2.0

    def lines(self):
        if not self._open():
            return []
        try:
            data = self.ser.read(4096)
        except (OSError, self.serial.SerialException) as e:
            self._drop(e)
            return []
        self.buf += data
        *done, self.buf = self.buf.split(b"\n")
        if len(self.buf) > 1024:
            self.buf = b""
        return [d.decode(errors="replace").strip() for d in done if d.strip()]

    def write(self, text):
        if not self.ser:
            return
        try:
            self.ser.write((text + "\n").encode())
        except (OSError, self.serial.SerialException) as e:
            self._drop(e)


class ConsoleLink:
    """Stand-in for the Arduino: commands from the keyboard, replies printed."""

    def __init__(self):
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in sys.stdin:
            self.q.put(line.strip())
        self.q.put(None)

    def lines(self):
        out = []
        while not self.q.empty():
            line = self.q.get()
            if line is None:
                if out:
                    self.q.put(None)       # handle what came before end-of-input first
                    break
                raise KeyboardInterrupt
            if line:
                out.append(line)
        return out

    def write(self, text):
        print(text, flush=True)


class Gateway:
    def __init__(self, a, link):
        self.a, self.link = a, link
        self.nl, self.info, self.c, self.cname, self.next_connect, self.warned = None, {}, 0, "", 0.0, False
        self.hello = a.console          # console mode needs no HELLO before READY
        self.watch, self.rate = set(), 0.1
        self.next_poll = self.next_names = 0.0
        self.names = {"blk": {}, "stn": {}, "sw": {}, "sen": {}}
        self.restore, self.manual_stations, self.estop_set, self.manual_mode = {}, set(), False, set()
        self.reset_cache()

    # ------------------------------------------------------------------ plumbing
    def send(self, text):
        if self.a.log:
            log("->", text)
        self.link.write(text)

    def reset_cache(self):
        self.cache = {k: {} for k in ("mode", "blk", "stn", "seats", "sw", "sen", "trn", "rows", "row", "det")}
        self.ev_seq = None

    def connect(self):
        nl = None
        try:
            nl = NL2Bridge(self.a.host, self.a.port, timeout=3.0)
            info = nl.bridge_info()
            if info.get("api", 0) < 7:
                log(f"Warning: bridge API {info.get('api')} - this gateway expects API 7 (NL2Bridge 1.1.0+);"
                    " seat counts read 0")
            self.nl, self.info = nl, info
            key = int(self.a.coaster) if str(self.a.coaster).lstrip("-").isdigit() else self.a.coaster
            self.select(key)
            log(f"Connected to NL2Bridge {info.get('build')} (API {info.get('api')}) at {self.a.host}:{self.a.port},"
                f" coaster {self.c} '{self.cname}'")
            self.warned = False
            self.send("LINK 1")
            if self.hello:
                self.ready()
        except (OSError, NL2Error) as e:
            if nl is not None:
                nl.close()
            self.nl, self.next_connect = None, time.time() + 2.0
            if not self.warned:
                log(f"NoLimits 2 / NL2Bridge not reachable at {self.a.host}:{self.a.port} ({e}) - retrying every 2 s")
                self.warned = True

    def drop(self, e):
        log(f"Lost the game connection: {e}")
        try:
            self.nl.close()
        except Exception:
            pass
        self.nl, self.next_connect = None, time.time() + 2.0
        self.send("LINK 0")

    def ready(self):
        self.reset_cache()
        self.send(f"READY {self.info.get('api', 0)} {clean(self.info.get('build'))} {self.c} {clean(self.cname)}")

    def select(self, key):
        self.c = self.nl.coaster(key)
        self.cname = next(x["name"] for x in self.nl.coasters() if x["index"] == self.c)
        self.load_names()
        self.reset_cache()

    def load_names(self):
        nl, c = self.nl, self.c
        self.names["blk"] = {b["id"]: b["name"] for b in nl.blocks(c)}
        self.names["stn"] = {s["index"]: s["name"] for s in nl.stations(c)}
        self.names["sw"] = {t["index"]: t["name"] for t in nl.special_tracks(c)}
        self.names["sen"] = {s["key"]: s["name"] for s in nl.sensors(c)}
        self.next_names = time.time() + 10.0

    def name(self, kind, key):
        if key not in self.names[kind]:
            self.load_names()
        return clean(self.names[kind].get(key, ""))

    def sid(self, block):
        return block if isinstance(block, int) else self.nl.block_id(self.c, block)

    # ------------------------------------------------------------------ status lines
    def push(self, kind, key, value, line, force):
        if force or self.cache[kind].get(key) != value:
            self.cache[kind][key] = value
            self.send(line)

    def poll(self, only=None, force=False):
        nl, c, w = self.nl, self.c, ({only} if only else self.watch)
        if time.time() >= self.next_names:
            self.load_names()
        if w & {"mode", "blocks"}:
            mode, estop, blocks = nl.block_states(c)
            if "mode" in w:
                self.push("mode", 0, (mode, estop), f"MODE {mode} {int(estop)}", force)
            if "blocks" in w:
                for b in blocks:
                    fl = (b["canAdvanceFwd"] | b["canAdvanceBwd"] << 1 | b["isBlock"] << 2 | b["scripted"] << 3 |
                          b["hasBrake"] << 4 | b["hasLift"] << 5 | b["hasTransport"] << 6)
                    v = (b["state"], b["trains"], b["lamp"], fl, b["brakeMode"], b["liftMode"], b["transportMode"],
                         b["trainMask"])
                    self.push("blk", b["id"], v, f"BLK {b['id']} " + " ".join(map(str, v)) + " "
                              + self.name("blk", b["id"]), force)
        if "stations" in w:
            sts, full = nl.station_states(c), None
            for s in sts:
                v = (s["flags"], s["state"], s["rowsOpenCount"])
                self.push("stn", s["index"], v, f"STN {s['index']} {s['flags']:08X} {s['state']} {s['rowsOpenCount']} "
                          + self.name("stn", s["index"]), force)
                key = (s["hasTrain"], s.get("seats", 0))
                if force or self.cache["seats"].get(s["index"], (None,))[0] != key:
                    full = full or nl.stations(c)       # train index / cars only when the train changes
                    j = full[s["index"]] if s["index"] < len(full) else {}
                    v = (key, j.get("train", -1), j.get("seats", 0), j.get("seatedCars", 0), j.get("seatsPerCar", 0))
                    self.push("seats", s["index"], v, f"SEATS {s['index']} " + " ".join(map(str, v[1:])), force)
        if "switches" in w:
            for t in nl.switch_states(c):
                fl = t["moving"] | t["switchable"] << 1 | t["manualAllowed"] << 2 | t["transferTable"] << 3
                v = (t["current"], t["target"], t["directions"], fl)
                self.push("sw", t["index"], v, f"SW {t['index']} " + " ".join(map(str, v)) + " "
                          + self.name("sw", t["index"]), force)
        if "sensors" in w:
            for s in nl.sensor_states(c):
                v = (s["active"], s["lastTrain"], s["passes"])
                self.push("sen", s["key"], v, f"SEN {s['key']} {int(s['active'])} {s['lastTrain']} {s['passes']} "
                          + self.name("sen", s["key"]), force)
        if "trains" in w:
            for t in nl.trains(c):
                v = (t["blockId"], t["station"], round(t["speed"] * 2) / 2, round(t.get("harness") or 0, 1))
                self.push("trn", t["index"], v, f"TRN {t['index']} {t['blockId']} {t['station']} {f2(t['speed'])} "
                          f"{f2(t.get('harness'))} {clean(t.get('blockName'))}", force)
        if "rows" in w:
            for s in nl.station_states(c):
                self.push_rows(s["index"], s["hasTrain"], force)
        if "detail" in w:
            _, secs = nl.section_detail(c)
            for d in secs:
                v = (d["flags"], d["trainIndex"], d["state"], d["userState"], round(d["liftSpeed"], 1),
                     round(d["transportSpeed"], 1))
                self.push("det", d["id"], v, f"DET {d['id']} {d['flags']:04X} {d['trainIndex']} {d['state']} "
                          f"{d['userState']} {f2(d['liftSpeed'])} {f2(d['transportSpeed'])}", force)
        if "events" in w and not only:
            r = nl.events(0xFFFFFFFF if self.ev_seq is None else self.ev_seq)
            if self.ev_seq is not None:
                for e in r.get("events", []):
                    eid = e.get("sensorKey", e.get("sectionId", -1))
                    self.send(f"EVT {e.get('typeName', e.get('type'))} {eid} {e.get('train', -1)} "
                              + clean(e.get("sensorName") or e.get("sectionName")))
            self.ev_seq = r.get("seq", self.ev_seq)

    def push_rows(self, st, has_train, force):
        rows = self.nl.rows(self.c, st).get("rows", []) if has_train else []
        n_open = sum(1 for r in rows if r.get("open"))
        self.push("rows", st, (len(rows), n_open), f"ROWS {st} {len(rows)} {n_open}", force)
        for r in rows:
            v = (bool(r.get("open")), round((r.get("position") or 0) * 100), r.get("seats", 0))
            self.push("row", (st, r["row"]), v, f"ROW {st} {r['row']} {int(v[0])} {v[1]} {v[2]}", force)

    # ------------------------------------------------------------------ commands
    def handle(self, line):
        if self.a.log:
            log("<-", line)
        args = parse(line)
        if not args:
            return
        cmd, args = str(args[0]).upper(), args[1:]
        if cmd == "HELLO":
            self.hello, self.watch = True, set(DEFAULT_WATCH)
            return self.ready() if self.nl else self.send("LINK 0")
        h = getattr(self, "cmd_" + cmd, None)
        if h is None:
            return self.send(f"ERR {cmd} Unknown command")
        if self.nl is None and cmd not in ("WATCH", "RATE"):
            return self.send(f"ERR {cmd} No connection to NoLimits 2")
        try:
            extra = h(*args)
            self.send(f"OK {cmd}" + (f" {clean(extra)}" if extra not in (None, "") else ""))
        except NL2Error as e:
            self.send(f"ERR {cmd} {clean(e)}")
        except (TypeError, ValueError, KeyError, IndexError) as e:
            self.send(f"ERR {cmd} Bad arguments ({clean(e)})")
        except (OSError, ConnectionError) as e:
            self.send(f"ERR {cmd} {clean(e)}")
            self.drop(e)

    @staticmethod
    def check(ok, why="The game refused it"):
        if not ok:
            raise NL2Error(why)

    # link / listing
    def cmd_PING(self):
        self.check(self.nl.ping())

    def cmd_INFO(self):
        i = self.nl.bridge_info()
        self.send(f"INFO {i.get('api', 0)} {clean(i.get('build'))} {clean(i.get('name'))}")

    def cmd_WATCH(self, *items):
        new = set()
        for it in items:
            it = str(it).lower()
            if it == "all":
                new |= set(WATCH_ITEMS)
            elif it in WATCH_ITEMS:
                new.add(it)
            elif it != "none":
                raise ValueError(f"unknown watch item '{it}'")
        self.watch = new
        self.reset_cache()
        return " ".join(sorted(new)) or "none"

    def cmd_RATE(self, ms):
        self.rate = max(20, int(ms)) / 1000.0

    def cmd_COASTERS(self):
        cs = self.nl.coasters()
        for x in cs:
            self.send(f"COASTER {x['index']} {x.get('blockMode', 0)} {int(bool(x.get('estop')))} "
                      f"{x.get('trains', 0)} {clean(x['name'])}")
        return len(cs)

    def cmd_COASTER(self, key):
        self.select(key)
        return f"{self.c} {self.cname}"

    def _list(self, item):
        self.poll(only=item, force=True)

    def cmd_BLOCKS(self):   self._list("blocks")
    def cmd_STATIONS(self): self._list("stations")
    def cmd_SWITCHES(self): self._list("switches")
    def cmd_SENSORS(self):  self._list("sensors")
    def cmd_TRAINS(self):   self._list("trains")
    def cmd_DETAIL(self):   self._list("detail")
    def cmd_STATUS(self):   self._list("mode")

    def cmd_ROWS(self, st):
        st = st if isinstance(st, int) else self.nl.station_index(self.c, st)
        self.push_rows(st, True, True)

    def cmd_DEVGET(self, block):
        sid = self.sid(block)
        d = self.nl.devices(self.c, sid)
        v = [(d.get(k) or {}).get("state", -1) if (d.get(k) or {}).get("present") else -1
             for k in ("brake", "lift", "transport")]
        self.send(f"DEV {sid} {v[0]} {v[1]} {v[2]} {self.name('blk', sid)}")

    def cmd_PARAMS(self, block):
        sid = self.sid(block)
        p = self.nl.device_params(self.c, sid)
        for dev in ("lift", "transport"):
            if p.get(dev):
                q = p[dev]
                self.send(f"PARAM {sid} {dev} {f2(q['speed'])} {f2(q['accel'])} {f2(q['decel'])} "
                          f"{f2(q.get('current'))} {self.name('blk', sid)}")

    # coaster
    def cmd_MODE(self, mode):
        mode = str(mode).lower()
        self.nl.set_block_mode(self.c, mode)
        if mode == "auto":
            self.manual_mode.discard(self.c)
        else:
            self.manual_mode.add(self.c)

    def cmd_ESTOP(self, on):
        on = onoff(on)
        self.nl.estop(self.c, on)
        self.estop_set = on

    def cmd_RESET(self):
        self.nl.reset_coaster(self.c)

    def cmd_LASH(self, train, on):
        self.nl.lash_train(self.c, int(train), onoff(on))

    # sections / devices
    def cmd_ADV(self, block, direction="fwd"):
        self.check(self.nl.advance(self.c, block, str(direction).lower() in ("bwd", "back", "1")),
                   "This block can not advance now")

    def cmd_BRAKE(self, block, mode):
        self.check(self.nl.set_brakes(self.c, block, str(mode).lower()))

    def cmd_LIFT(self, block, mode):
        self.check(self.nl.set_lift(self.c, block, str(mode).lower()))

    def cmd_TRANSPORT(self, block, mode):
        self.check(self.nl.set_transport(self.c, block, str(mode).lower()))

    def cmd_DEVICE(self, block, device, value):
        self.check(self.nl.set_device(self.c, block, str(device).lower(), int(value)),
                   "The game refused it (needs Full Manual block mode)")

    def _remember(self, sid, dev):
        key = (self.c, sid, dev)
        if key not in self.restore:
            p = self.nl.device_params(self.c, sid).get(dev)
            if not p:
                raise NL2Error(f"This section has no {dev}")
            self.restore[key] = p["speed"]
        return key

    def cmd_SPEED(self, block, device, speed, accel=-1, decel=-1):
        sid, dev = self.sid(block), str(device).lower()
        self._remember(sid, dev)
        self.nl.set_device_params(self.c, sid, dev, float(speed), float(accel), float(decel))

    def cmd_LIFTPOWER(self, block, on):
        sid = self.sid(block)
        design = self.restore[self._remember(sid, "lift")]
        if design < 0.01:
            raise NL2Error("The lift is at speed 0 and its design speed is unknown - send SPEED <block> lift <m/s> once")
        self.nl.set_device_params(self.c, sid, "lift", design if onoff(on) else 0.0)

    def cmd_SWITCH(self, track, direction):
        self.nl.set_switch(self.c, track, int(direction))

    def cmd_SECTION(self, block, cmd, param=0):
        return int(self.nl.section_set(self.c, block, int(cmd), int(param)))

    def cmd_SECTIONGET(self, block, query, x=0):
        ok, iv, dv = self.nl.section_get(self.c, block, int(query), float(x))
        self.send(f"GET {ok} {iv} {dv:.4f}")

    # stations
    def cmd_ST(self, st, op):
        op = str(op).lower()
        st = st if isinstance(st, int) else self.nl.station_index(self.c, st)
        self.nl.station_op(self.c, st, op)
        if op == "manual":
            self.manual_stations.add((self.c, st))
        elif op == "auto":
            self.manual_stations.discard((self.c, st))

    def cmd_ROW(self, st, row, on):
        self.nl.set_row_restraint(self.c, st, int(row), onoff(on))

    # scripted-mode coasters
    def cmd_SBLOCK(self, block, cmd, on=1):
        self.check(self.nl.scripted_block(self.c, block, cmd if isinstance(cmd, int) else str(cmd).lower(), onoff(on)))

    def cmd_REGSTATE(self, block, state, lamp, *text):
        self.nl.register_state(self.c, block, int(state), lamp if isinstance(lamp, int) else str(lamp).lower(),
                               " ".join(map(str, text)))

    def cmd_SETSTATE(self, block, state):
        self.nl.set_state(self.c, block, int(state))

    def cmd_SENTER(self, block): self.check(self.nl.station_entering(self.c, block))
    def cmd_SLEAVE(self, block): self.check(self.nl.station_leaving(self.c, block))
    def cmd_SCLEAR(self, block): self.check(self.nl.station_next_clear(self.c, block))
    def cmd_SOCC(self, block):   self.check(self.nl.station_next_occupied(self.c, block))

    # ------------------------------------------------------------------ main loop / exit
    def run(self):
        while True:
            lines = self.link.lines()
            now = time.time()
            if getattr(self.link, "opened", False):          # serial port (re)opened: wait for a new HELLO
                self.link.opened, self.hello, self.watch = False, False, set()
            if getattr(self.link, "who_at", 0) and now >= self.link.who_at:
                self.link.who_at = 0.0
                if not self.hello:                            # boards that don't reset on port open (Leonardo, ESP32)
                    self.send("WHO")
            for line in lines:
                self.handle(line)
            if self.nl is None and now >= self.next_connect:
                self.connect()
            if self.nl is not None and self.watch and self.hello and now >= self.next_poll:
                self.next_poll = now + self.rate
                try:
                    self.poll()
                except NL2Error as e:
                    log(f"Poll error: {e}")
                except (OSError, ConnectionError) as e:
                    self.drop(e)
            time.sleep(0.005)

    def hand_back(self):
        if self.nl is None:
            return
        for (c, sid, dev), speed in self.restore.items():
            self._try(f"Restore {dev} speed of section {sid}", self.nl.set_device_params, c, sid, dev, speed)
        for c, st in self.manual_stations:
            self._try(f"Station {st} back to automatic dispatch", self.nl.set_manual_dispatch, c, st, False)
        if self.estop_set:
            self._try("Release E-stop", self.nl.estop, self.c, False)
        for c in self.manual_mode:
            # the game only leaves manual block mode once every train has stopped on a block section
            deadline, err, told = time.time() + 120, None, False
            while time.time() < deadline:
                try:
                    self.nl.set_block_mode(c, "auto")
                    log(f"Coaster {c} back to automatic block mode")
                    break
                except NL2Error as e:
                    err = e
                    if not told:
                        log(f"Waiting for trains to stop before returning coaster {c} to automatic mode ({e})...")
                        told = True
                    time.sleep(1)
                except OSError as e:
                    log(f"Coaster {c} back to automatic block mode failed: {e}")
                    break
            else:
                log(f"Coaster {c} back to automatic block mode failed: {err}")

    def _try(self, what, fn, *args):
        try:
            fn(*args)
            log(what)
        except (NL2Error, OSError) as e:
            log(f"{what} failed: {e}")


def main():
    ap = argparse.ArgumentParser(description="NL2Bridge serial gateway for Arduino ride control panels")
    ap.add_argument("serial", nargs="?", help="serial port of the Arduino, e.g. COM5 or /dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--host", default="127.0.0.1", help="PC running NoLimits 2 (default this PC)")
    ap.add_argument("--port", type=int, default=15152, help="NL2Bridge port")
    ap.add_argument("--coaster", default="0", help="coaster index or name (default 0)")
    ap.add_argument("--console", action="store_true", help="read commands from the keyboard instead of serial")
    ap.add_argument("--log", action="store_true", help="print every line sent and received")
    ap.add_argument("--no-restore", action="store_true", help="leave speeds / dispatch / E-stop / block mode as they are on exit")
    a = ap.parse_args()
    if not a.console and not a.serial:
        ap.error("give the Arduino's serial port (e.g. COM5) or --console")
    try:
        link = ConsoleLink() if a.console else SerialLink(a.serial, a.baud)
    except ImportError:
        sys.exit("pyserial is missing: pip install pyserial")
    gw = Gateway(a, link)
    if a.console:
        log("Console mode: type commands (e.g. BLOCKS, WATCH blocks stations, ST 0 dispatch). Ctrl+C to quit.")
    try:
        gw.run()
    except KeyboardInterrupt:
        pass
    finally:
        if not a.no_restore:
            gw.hand_back()


if __name__ == "__main__":
    main()
