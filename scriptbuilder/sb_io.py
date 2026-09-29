"""Physical controller inputs and lamp outputs for the NL2 Script Builder.

Every input is addressed by a string name and read as a number (buttons 0/1, axes 0.0..1.0):

    key:F1  key:SPACE  key:A           keyboard (global - works while NoLimits 2 has the focus)
    joy0:b3  joy0:x  joy0:pov_up       USB game controller / button box (Windows joystick API)
    COM3:B2  COM3:A0                   Arduino (or any microcontroller) on a serial port, see SERIAL PROTOCOL
    mb:192.168.1.50:di4  mb:...:co4  mb:...:hr2   Modbus TCP PLC / remote I/O: discrete input, coil, holding reg

Outputs (lamps): COM3:L5 (serial), mb:192.168.1.50:co10 (Modbus coil).

SERIAL PROTOCOL (115200 baud, text lines ending in \\n):
    device -> PC   B<n>=0|1        button / switch n changed
                   A<n>=<0..1023>  analog input n (pots, speed lever)
    PC -> device   L<n>=0|1        lamp n off / on (blinking is done by the PC)
"""
import ctypes, ctypes.wintypes as W, socket, struct, threading, time

# ============================================================================ keyboard
_VK = {"SPACE": 0x20, "ENTER": 0x0D, "TAB": 0x09, "ESC": 0x1B, "BACKSPACE": 0x08, "SHIFT": 0x10, "CTRL": 0x11,
       "ALT": 0x12, "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27, "INSERT": 0x2D, "DELETE": 0x2E,
       "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22}
_VK.update({f"F{i}": 0x6F + i for i in range(1, 13)})
_VK.update({f"NUM{i}": 0x60 + i for i in range(10)})
_VK.update({chr(c): c for c in range(0x30, 0x3A)})
_VK.update({chr(c): c for c in range(0x41, 0x5B)})
KEY_NAMES = list(_VK)


class Keyboard:
    """Global keyboard state (GetAsyncKeyState), so keys work while the game window has the focus."""

    def __init__(self):
        self.user32 = ctypes.windll.user32

    def read(self, out):
        for name, vk in _VK.items():
            out["key:" + name] = 1.0 if self.user32.GetAsyncKeyState(vk) & 0x8000 else 0.0


# ============================================================================ joystick (winmm)
class JOYINFOEX(ctypes.Structure):
    _fields_ = [("dwSize", W.DWORD), ("dwFlags", W.DWORD), ("dwXpos", W.DWORD), ("dwYpos", W.DWORD),
                ("dwZpos", W.DWORD), ("dwRpos", W.DWORD), ("dwUpos", W.DWORD), ("dwVpos", W.DWORD),
                ("dwButtons", W.DWORD), ("dwButtonNumber", W.DWORD), ("dwPOV", W.DWORD),
                ("dwReserved1", W.DWORD), ("dwReserved2", W.DWORD)]


class JOYCAPSW(ctypes.Structure):
    _fields_ = [("wMid", W.WORD), ("wPid", W.WORD), ("szPname", W.WCHAR * 32), ("wXmin", W.UINT),
                ("wXmax", W.UINT), ("wYmin", W.UINT), ("wYmax", W.UINT), ("wZmin", W.UINT), ("wZmax", W.UINT),
                ("wNumButtons", W.UINT), ("wPeriodMin", W.UINT), ("wPeriodMax", W.UINT), ("wRmin", W.UINT),
                ("wRmax", W.UINT), ("wUmin", W.UINT), ("wUmax", W.UINT), ("wVmin", W.UINT), ("wVmax", W.UINT),
                ("wCaps", W.UINT), ("wMaxAxes", W.UINT), ("wNumAxes", W.UINT), ("wMaxButtons", W.UINT),
                ("szRegKey", W.WCHAR * 32), ("szOEMVxD", W.WCHAR * 260)]


class Joysticks:
    """All connected game controllers / button boxes: joyN:b1..b32, joyN:x/y/z/r/u/v (0..1), joyN:pov_up/..."""
    AXES = ("x", "y", "z", "r", "u", "v")

    def __init__(self):
        self.winmm = ctypes.windll.winmm
        self.devs, self.next_scan = {}, 0.0

    def scan(self):
        devs = {}
        for i in range(16):
            caps = JOYCAPSW()
            if self.winmm.joyGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
                continue
            ji = JOYINFOEX(dwSize=ctypes.sizeof(JOYINFOEX), dwFlags=0xFF)
            if self.winmm.joyGetPosEx(i, ctypes.byref(ji)) == 0:
                devs[i] = dict(name=caps.szPname, buttons=max(1, caps.wNumButtons), axes=caps.wNumAxes,
                               ranges=[(caps.wXmin, caps.wXmax), (caps.wYmin, caps.wYmax), (caps.wZmin, caps.wZmax),
                                       (caps.wRmin, caps.wRmax), (caps.wUmin, caps.wUmax), (caps.wVmin, caps.wVmax)])
        self.devs = devs
        return devs

    def read(self, out):
        now = time.time()
        if now >= self.next_scan:            # pick up controllers plugged in while running
            self.scan()
            self.next_scan = now + 5.0
        for i, dv in list(self.devs.items()):
            ji = JOYINFOEX(dwSize=ctypes.sizeof(JOYINFOEX), dwFlags=0xFF)
            if self.winmm.joyGetPosEx(i, ctypes.byref(ji)) != 0:
                self.devs.pop(i, None)
                continue
            p = f"joy{i}:"
            for b in range(min(32, dv["buttons"])):
                out[f"{p}b{b + 1}"] = 1.0 if ji.dwButtons >> b & 1 else 0.0
            raw = (ji.dwXpos, ji.dwYpos, ji.dwZpos, ji.dwRpos, ji.dwUpos, ji.dwVpos)
            for k, (name, v, (lo, hi)) in enumerate(zip(self.AXES, raw, dv["ranges"])):
                if k < dv["axes"]:
                    out[p + name] = (v - lo) / (hi - lo) if hi > lo else 0.0
            pov = ji.dwPOV
            on = pov < 36000
            for name, ang in (("up", 0), ("right", 9000), ("down", 18000), ("left", 27000)):
                diff = abs(pov - ang)
                out[p + "pov_" + name] = 1.0 if on and min(diff, 36000 - diff) <= 4500 else 0.0


# ============================================================================ serial (Arduino)
class SerialPort:
    def __init__(self, port):
        import serial                        # pyserial (pip install pyserial)
        self.port = port
        self.ser = serial.Serial(port, 115200, timeout=0)
        self.buf, self.vals, self.lamps = b"", {}, {}
        self.err = None

    def read(self, out):
        try:
            self.buf += self.ser.read(4096)
        except Exception as e:               # unplugged
            self.err = str(e)
            return
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip().decode(errors="ignore")
            if len(line) > 2 and line[0] in "BA" and "=" in line:
                k, v = line[1:].split("=", 1)
                try:
                    n, val = int(k), float(v)
                except ValueError:
                    continue
                self.vals[f"{line[0]}{n}"] = (1.0 if val else 0.0) if line[0] == "B" else val / 1023.0
        for k, v in self.vals.items():
            out[f"{self.port}:{k}"] = v

    def write(self, reg, on):
        if self.lamps.get(reg) != on:
            try:
                self.ser.write(f"L{reg[1:]}={1 if on else 0}\n".encode())
                self.lamps[reg] = on
            except Exception as e:
                self.err = str(e)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def serial_ports():
    try:
        from serial.tools import list_ports
        return [p.device for p in list_ports.comports()]
    except ImportError:
        return []


# ============================================================================ Modbus TCP
class ModbusTCP:
    """Minimal Modbus TCP client: FC1 coils, FC2 discrete inputs, FC3 holding registers, FC5 write coil."""

    def __init__(self, host, unit=1, port=502):
        self.host, self.unit, self.port = host, unit, port
        self.sock, self.tid, self.err = None, 0, None
        self.want = {"di": set(), "co": set(), "hr": set()}
        self.coils_out = {}
        self.retry = 0.0

    def _req(self, pdu):
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port), timeout=0.5)
        self.tid = (self.tid + 1) & 0xFFFF
        self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit) + pdu)
        hdr = self._recv(7)
        body = self._recv(struct.unpack(">H", hdr[4:6])[0] - 1)
        if body[0] & 0x80:
            raise IOError(f"Modbus exception {body[1]}")
        return body

    def _recv(self, n):
        b = b""
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c:
                raise ConnectionError("closed")
            b += c
        return b

    def _read_block(self, fc, addrs):
        lo, hi = min(addrs), max(addrs)
        data = self._req(struct.pack(">BHH", fc, lo, hi - lo + 1))[2:]
        if fc == 3:
            return {lo + i: struct.unpack(">H", data[i * 2:i * 2 + 2])[0] for i in range(hi - lo + 1)}
        return {lo + i: data[i // 8] >> (i % 8) & 1 for i in range(hi - lo + 1)}

    def read(self, out):
        if time.time() < self.retry:
            return
        try:
            for kind, fc in (("di", 2), ("co", 1), ("hr", 3)):
                if self.want[kind]:
                    for a, v in self._read_block(fc, self.want[kind]).items():
                        out[f"mb:{self.host}:{kind}{a}"] = (v / 65535.0) if kind == "hr" else float(v)
            self.err = None
        except (OSError, IOError, IndexError, struct.error) as e:
            self._fail(e)

    def write(self, reg, on):
        a = int(reg[2:])
        if self.coils_out.get(a) == on or time.time() < self.retry:
            return
        try:
            self._req(struct.pack(">BHH", 5, a, 0xFF00 if on else 0))
            self.coils_out[a] = on
        except (OSError, IOError) as e:
            self._fail(e)

    def _fail(self, e):
        self.err = str(e)
        self.close()
        self.coils_out = {}
        self.retry = time.time() + 2.0

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


# ============================================================================ hub
class IOHub:
    """Polls every input backend on its own thread; the engine reads snapshot() and calls set_outputs()."""

    def __init__(self):
        self.kbd, self.joy = Keyboard(), Joysticks()
        self.serial, self.modbus = {}, {}
        self.values, self.lock = {}, threading.Lock()
        self.errors, self.outputs = {}, {}
        self.enable_keyboard = True
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def configure(self, names):
        """Open the serial ports / Modbus hosts that the profile's inputs and outputs use."""
        ports, hosts = set(), {}
        for n in names:
            if not n:
                continue
            if n.upper().startswith("COM") and ":" in n:
                ports.add(n.split(":", 1)[0].upper())
            elif n.startswith("mb:") and n.count(":") >= 2:
                host, reg = n[3:].rsplit(":", 1)
                hosts.setdefault(host, set()).add(reg)
        with self.lock:
            for p in list(self.serial):
                if p not in ports:
                    self.serial.pop(p).close()
            for p in ports:
                if p not in self.serial:
                    try:
                        self.serial[p] = SerialPort(p)
                        self.errors.pop(p, None)
                    except Exception as e:      # missing pyserial, port busy, not plugged in
                        self.errors[p] = str(e)
            for h in list(self.modbus):
                if h not in hosts:
                    self.modbus.pop(h).close()
            for h, regs in hosts.items():
                mb = self.modbus.setdefault(h, ModbusTCP(h))
                mb.want = {"di": set(), "co": set(), "hr": set()}
                for r in regs:
                    if r[:2] in mb.want and r[2:].isdigit():
                        mb.want[r[:2]].add(int(r[2:]))

    def _run(self):
        while not self.stop:
            vals = {}
            try:
                if self.enable_keyboard:
                    self.kbd.read(vals)
                self.joy.read(vals)
            except OSError:
                pass
            with self.lock:
                serial, modbus, outs = dict(self.serial), dict(self.modbus), dict(self.outputs)
            for p, s in serial.items():
                s.read(vals)
                if s.err:
                    self.errors[p] = s.err
            for h, m in modbus.items():
                m.read(vals)
                if m.err:
                    self.errors["mb:" + h] = m.err
                else:
                    self.errors.pop("mb:" + h, None)
            for name, on in outs.items():
                if name.upper().startswith("COM") and ":" in name:
                    p, reg = name.split(":", 1)
                    if p.upper() in serial:
                        serial[p.upper()].write(reg.upper(), on)
                elif name.startswith("mb:"):
                    host, reg = name[3:].rsplit(":", 1)
                    if host in modbus and reg.startswith("co"):
                        modbus[host].write(reg, on)
            with self.lock:
                self.values = vals
            time.sleep(0.01)

    def snapshot(self):
        with self.lock:
            return dict(self.values)

    def set_outputs(self, outs):
        with self.lock:
            self.outputs = dict(outs)

    @staticmethod
    def learn(before, after):
        """Name of the input that changed between two snapshots (a button pressed or an axis moved), or None."""
        best, bd = None, 0.0
        for k, v in after.items():
            d = abs(v - before.get(k, v))
            if d > bd:
                best, bd = k, d
        return best if bd >= 0.35 else None

    def close(self):
        self.stop = True
        for s in self.serial.values():
            s.close()
        for m in self.modbus.values():
            m.close()


# ============================================================================ Arduino sketch generator
def arduino_sketch(buttons, analogs, lamps):
    """buttons/analogs/lamps: [(n, pin, label)] -> .ino source implementing the serial protocol.
    Buttons are wired pin -> switch -> GND (internal pull-up); lamps pin -> LED + resistor (or relay driver) -> GND."""
    def arr(items, idx):
        return ", ".join(str(it[idx]) for it in items) or "0"
    lines = ["// NL2 Script Builder - generated Arduino panel firmware (serial 115200)",
             "// Buttons: pin -> switch -> GND (INPUT_PULLUP). Analogs: 0-5 V pots. Lamps: pin -> LED/relay driver.",
             ""]
    for kind, items in (("button", buttons), ("analog", analogs), ("lamp", lamps)):
        for n, pin, label in items:
            lines.append(f"//   {kind:<6} {n:>2}  pin {str(pin):<4} {label}")
    lines += ["",
              f"const int NB = {len(buttons)}; const int BTN_N[] = {{{arr(buttons, 0)}}}; "
              f"const int BTN_PIN[] = {{{arr(buttons, 1)}}};",
              f"const int NA = {len(analogs)}; const int AN_N[] = {{{arr(analogs, 0)}}}; "
              f"const int AN_PIN[] = {{{arr(analogs, 1)}}};",
              f"const int NL = {len(lamps)}; const int LAMP_N[] = {{{arr(lamps, 0)}}}; "
              f"const int LAMP_PIN[] = {{{arr(lamps, 1)}}};",
              "int lastB[40]; int lastA[16]; unsigned long debounce[40]; unsigned long lastFull = 0;",
              "String rx;",
              "",
              "void setup() {",
              "  Serial.begin(115200);",
              "  for (int i = 0; i < NB; i++) { pinMode(BTN_PIN[i], INPUT_PULLUP); lastB[i] = -1; }",
              "  for (int i = 0; i < NL; i++) { pinMode(LAMP_PIN[i], OUTPUT); digitalWrite(LAMP_PIN[i], LOW); }",
              "  for (int i = 0; i < NA; i++) lastA[i] = -100;",
              "}",
              "",
              "void loop() {",
              "  unsigned long now = millis();",
              "  bool full = now - lastFull > 1000;            // resend every input once a second",
              "  if (full) lastFull = now;",
              "  for (int i = 0; i < NB; i++) {",
              "    int v = digitalRead(BTN_PIN[i]) == LOW ? 1 : 0;",
              "    if ((v != lastB[i] && now - debounce[i] > 15) || full) {",
              "      if (v != lastB[i]) debounce[i] = now;",
              "      lastB[i] = v; Serial.print('B'); Serial.print(BTN_N[i]); Serial.print('='); Serial.println(v);",
              "    }",
              "  }",
              "  for (int i = 0; i < NA; i++) {",
              "    int v = analogRead(AN_PIN[i]);",
              "    if (abs(v - lastA[i]) > 4 || full) {",
              "      lastA[i] = v; Serial.print('A'); Serial.print(AN_N[i]); Serial.print('='); Serial.println(v);",
              "    }",
              "  }",
              "  while (Serial.available()) {",
              "    char ch = Serial.read();",
              "    if (ch == '\\n') {",
              "      int eq = rx.indexOf('=');",
              "      if (rx.length() > 2 && rx[0] == 'L' && eq > 1) {",
              "        int n = rx.substring(1, eq).toInt(), v = rx.substring(eq + 1).toInt();",
              "        for (int i = 0; i < NL; i++) if (LAMP_N[i] == n) digitalWrite(LAMP_PIN[i], v ? HIGH : LOW);",
              "      }",
              "      rx = \"\";",
              "    } else if (ch != '\\r' && rx.length() < 32) rx += ch;",
              "  }",
              "  delay(2);",
              "}", ""]
    return "\n".join(lines)
