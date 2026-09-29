"""NL2 virtual ride control panel - a replica of the physical operator panel (key switches, E-STOP LOCKOUT,
illuminated pushbuttons, Weintek cMT HMI) driving NoLimits 2 through NL2Bridge.

    python nl2_controller.py [--host H] [--port P] [--coaster NAME]

Needs NL2Bridge.dll injected into NoLimits 2. All ride logic lives in ride_logic.py (a port of the RCPANELV5
PLC program); this file only draws the panel/HMI and turns mouse/keyboard into panel inputs.

Startup (like the real panel): PANEL ENABLE on -> ACKNOWLEDGE (lamp test) -> daily test (once a day) ->
hold RIDE START 5 s -> release, wait 5 s -> hold RIDE START 5 s -> E-STOP RESET -> RIDE START -> LIFT START ->
RESTRAINTS -> hold ADVANCE & DISPATCH.

Keyboard: D / K = ADVANCE & DISPATCH (hold), H = HMI ENABLE, R = RESTRAINTS, A = ACKNOWLEDGE,
S / X = RIDE START / STOP, L / O = LIFT START / STOP, E = E-STOP RESET, Space = E-STOP (push / pull),
Z = E-STOP LOCKOUT, P = PANEL ENABLE key, M = OPERATION MODE key, B = MAINTENANCE ENABLE key, F1..F10 = HMI pages.
"""
import argparse, json, math, os, struct, tempfile, time, wave
import tkinter as tk
from tkinter import messagebox

import ride_logic as rl
from ride_logic import RideLogic, Link, OFF, lamp_lit

try:
    import winsound
except ImportError:                    # not on Windows: no beeper
    winsound = None

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "settings.json")
DEFAULTS = dict(host="127.0.0.1", port=15152, coaster="", two_hand_dispatch=False, dispatch_hold_s=1.5,
                require_hmi_enable=True, hmi_enable_seconds=5, scan_hz=20,
                require_daily_test=True, ride_stop_estop=True, beeper=True)


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump({k: s[k] for k in DEFAULTS}, f, indent=2)


# ---------------------------------------------------------------- panel geometry (design units)
W, H = 1400, 890
HX, HY, K = 272, 42, 0.72               # HMI origin on the panel and HMI px -> panel units
HW, HH = 1024, 768                      # HMI resolution (cMT 10")
FONT = "Segoe UI"
FACE, FACE_EDGE = "#dcdfe0", "#9ea4a8"
F_GREEN, F_YELLOW, F_RED = "#2e9a5a", "#e8d400", "#d23a3a"
LENS = {"green": ("#3dff6a", "#0f4f24"), "red": ("#ff3b3b", "#5e1414"), "amber": ("#ffb21e", "#5e3d0a"),
        "blue": ("#4a9bff", "#0f2458"), "black": ("#ffffff", "#50555a")}
BUTTONS = {   # name: (x, y, lens, plate text, plate colour, plate width, lamp)
    "ack": (125, 735, "blue", "ACKNOWLEDGE", F_GREEN, 170, "ack"),
    "ad_l": (350, 770, "green", "ADVANCE & DISPATCH", F_GREEN, 172, "ad_l"),
    "hmi_enable": (545, 770, "black", "HMI ENABLE", F_GREEN, 140, "hmi_enable"),
    "restraints": (738, 770, "amber", "RESTRAINTS", F_GREEN, 140, "restraints"),
    "ad_r": (930, 770, "green", "ADVANCE & DISPATCH", F_GREEN, 172, "ad_r"),
    "estop_reset": (1132, 315, "blue", "E-STOP RESET", F_RED, 140, "estop_reset"),
    "ride_start": (1135, 520, "green", "RIDE START", F_RED, 130, "ride_start"),
    "ride_stop": (1295, 520, "red", "RIDE STOP", F_RED, 130, "ride_stop"),
    "lift_start": (1135, 770, "green", "LIFT START", F_RED, 130, "lift_start"),
    "lift_stop": (1295, 770, "red", "LIFT STOP", F_RED, 130, "lift_stop"),
}
PLATE_Y = {"ack": 640, "ad_l": 688, "hmi_enable": 688, "restraints": 688, "ad_r": 688, "estop_reset": 245,
           "ride_start": 443, "ride_stop": 443, "lift_start": 688, "lift_stop": 688}
BTN_R = 38
ESTOP = (1305, 120)
LOTO = (1137, 112)
BEEPER = (1300, 300)
KEYS = [  # key: (x, y, legend, positions)
    ("panel", 125, 125, "PANEL ENABLE", [(False, "OFF"), (True, "ON")]),
    ("mode", 125, 300, "OPERATION MODE", [("AUTO", "AUTO"), ("MANUAL", "MANUAL"), ("TRANSFER", "TRANSFER")]),
    ("maint", 125, 475, "MAINTENANCE ENABLE", [(False, "OFF"), (True, "ON")]),
]
FRAMES = [(20, 20, 230, 560, F_GREEN), (20, 580, 230, 840, F_GREEN), (250, 645, 1030, 840, F_GREEN),
          (1050, 20, 1380, 190, F_YELLOW), (1050, 210, 1215, 380, F_RED), (1050, 400, 1380, 600, F_RED),
          (1050, 645, 1380, 840, F_RED)]
KEYBOARD = {"d": "ad_l", "k": "ad_r", "h": "hmi_enable", "r": "restraints", "a": "ack", "s": "ride_start",
            "x": "ride_stop", "l": "lift_start", "o": "lift_stop", "e": "estop_reset"}
PAGES = ["HOME", "MENU", "OVERVIEW", "STATION", "BLOCKS", "TRANSFER", "MAINT", "ALARMS", "DAILY", "SETUP"]
LEGACY = ["OVERVIEW", "STATION", "BLOCKS", "TRANSFER", "MAINT", "ALARMS", "SETUP"]
NAV = [("HOME", "Operator"), ("MENU", "Main Menu"), ("OVERVIEW", "Track"), ("STATION", "Station"),
       ("BLOCKS", "Blocks"), ("TRANSFER", "Transfer"), ("MAINT", "Maint."), ("ALARMS", "Alarms"),
       ("DAILY", "Daily Test"), ("SETUP", "Setup")]

# HMI colours (Weintek blue theme)
BG, PANE, BTN, BTN_ON, BTN_OFF, TXT, DIM = ("#2236e6", "#1427b8", "#1fa37a", "#27d17a", "#8c96e8", "#ffffff",
                                            "#b9c2ff")
CYAN, SQ_OFF, SQ_ON, SQ_ADV = "#9ff4ff", "#1f4f5c", "#39e35c", "#ffb21e"
C_FREE, C_BUSY, C_OCC, C_OFF, C_FM, C_NONE = "#2fe05a", "#ffd21f", "#ff3b3b", "#7b83a6", "#d57cff", "#48509a"


def make_dpi_aware():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (ImportError, AttributeError, OSError):
        pass


def short(name, n=16):
    if name.startswith("Unnamed Section "):
        return "S" + name[16:]
    if name.startswith("Unnamed Special Track "):
        return "ST" + name[22:]
    return name if len(name) <= n else name[:n - 1] + "~"


def mph(v):
    return f"{abs(v) * 2.236936:5.1f} mph"


class Beeper:
    """Panel sounder: a looping piezo-like tone switched on/off with the beeper lamp output."""

    def __init__(self):
        self.on = False
        self.path = None
        if winsound is None:
            return
        self.path = os.path.join(tempfile.gettempdir(), "nl2panel_beep.wav")
        rate, f = 22050, 2400.0
        n = int(rate * 0.25)
        frames = b"".join(struct.pack("<h", int(9000 * math.copysign(1.0, math.sin(2 * math.pi * f * i / rate))))
                          for i in range(n))
        try:
            with wave.open(self.path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(frames)
        except OSError:
            self.path = None

    def set(self, on):
        if on == self.on or not self.path:
            return
        self.on = on
        try:
            if on:
                winsound.PlaySound(self.path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
            else:
                winsound.PlaySound(None, winsound.SND_PURGE)
        except RuntimeError:
            pass


class Panel:
    def __init__(self, root, logic, link, settings, scale=1.0):
        self.root, self.logic, self.link, self.settings = root, logic, link, settings
        self.S = scale
        root.title("NL2 Ride Control Panel")
        root.configure(bg="#2a2d30")
        root.resizable(False, False)
        self.cv = tk.Canvas(root, width=round(W * scale), height=round(H * scale), bg="#2a2d30",
                            highlightthickness=0)
        self.cv.pack()
        self.keys = {"panel": False, "mode": "AUTO", "maint": False, "lockout": False}
        self.estop_pushed = False
        self.page = "HOME"
        self.sub = {}                  # per-page paging / sub-view state
        self.hits = []
        self.held_btn = None
        self.kb_down = set()
        self.lens = {}
        self.ox, self.oy, self.pk = 0.0, 0.0, 1.0
        self.beeper = Beeper()
        self._draw_static()
        self.cv.scale("all", 0, 0, scale, scale)
        self.cv.bind("<ButtonPress-1>", self._mouse_down)
        self.cv.bind("<ButtonRelease-1>", self._mouse_up)
        self.cv.bind("<ButtonPress-3>", lambda e: self._mouse_down(e, right=True))
        root.bind("<KeyPress>", self._key_down)
        root.bind("<KeyRelease>", self._key_up)
        for k, v in self.keys.items():
            logic.set_key(k, v)
        self._tick()
        self._beep_tick()

    # ================================================================ static panel
    def _rr(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
               x0, y1 - r, x0, y0 + r, x0, y0]
        return self.cv.create_polygon(pts, smooth=True, **kw)

    def _draw_static(self):
        cv, S = self.cv, self.S
        self._rr(6, 6, W - 6, H - 6, 18, fill=FACE, outline=FACE_EDGE, width=3 * S)
        for x0, y0, x1, y1, col in FRAMES:
            self._rr(x0, y0, x1, y1, 22, fill="", outline=col, width=3 * S)
        # HMI bezel (Weintek cMT)
        self._rr(250, 20, 1030, 628, 16, fill="#1c1f23", outline="#0b0c0e", width=2 * S)
        self._rr(262, 32, 1018, 606, 6, fill="#0d0f12", outline="")
        cv.create_text(640, 616, text="WEINTEK", fill="#c9ced3", font=(FONT, 7, "bold"))
        cv.create_rectangle(612, 612, 622, 620, outline="#c9ced3")
        for name, (x, y, col, legend, pcol, pw, _) in BUTTONS.items():
            self._plate(x, PLATE_Y[name], legend, pcol, pw)
            self._pushbutton(name, x, y, col)
        # E-STOP LOCKOUT rotary (yellow plate, red handle)
        x, y = LOTO
        self._rr(x - 75, y - 82, x + 75, y + 68, 10, fill="#f2cf00", outline="#8a7400", width=2 * S)
        cv.create_text(x - 44, y - 68, text="NORMAL", fill="#1b1b1b", font=(FONT, 6, "bold"))
        cv.create_text(x + 44, y - 68, text="LOCKOUT", fill="#1b1b1b", font=(FONT, 6, "bold"))
        cv.create_oval(x - 48, y - 48, x + 48, y + 48, fill="#c3281e", outline="#6e120c", width=2 * S)
        cv.create_oval(x - 34, y - 34, x + 34, y + 34, fill="#d8362a", outline="")
        self.loto_handle = cv.create_polygon(0, 0, 0, 0, fill="#a71d14", outline="#5a0d08", width=2 * S)
        self.loto_dot = cv.create_oval(0, 0, 0, 0, fill="#f4f4f4", outline="")
        # E-STOP: red illuminated latching pushbutton
        x, y = ESTOP
        self._plate(x, 50, "E-STOP", F_RED, 110)
        cv.create_oval(x - 42, y - 42, x + 42, y + 42, fill="#c9ced3", outline="#6d747b", width=2 * S)
        cv.create_oval(x - 34, y - 34, x + 34, y + 34, fill="#8d949b", outline="")
        self.estop_lens = cv.create_oval(x - 28, y - 28, x + 28, y + 28, fill=LENS["red"][1], outline="#300",
                                         width=2 * S)
        self.estop_glint = cv.create_oval(x - 16, y - 20, x + 2, y - 10, fill="", outline="")
        self.estop_txt = cv.create_text(x, y + 56, text="", fill="#8a1010", font=(FONT, 7, "bold"))
        # beeper (sounder)
        x, y = BEEPER
        cv.create_oval(x - 30, y - 30, x + 30, y + 30, fill="#18191b", outline="#6d747b", width=2 * S)
        for r in (8, 16, 24):
            cv.create_oval(x - r, y - r, x + r, y + r, outline="#3a3d41", width=S)
        self.beep_ring = cv.create_oval(x - 36, y - 36, x + 36, y + 36, outline="", width=3 * S)
        cv.create_text(x, y + 44, text="BEEPER", fill="#555b61", font=(FONT, 6, "bold"))
        # key switches
        self.key_items = {}
        for key, x, y, legend, pos in KEYS:
            self._plate(x, y - 70, legend, F_GREEN, 186)
            cv.create_oval(x - 30, y - 30, x + 30, y + 30, fill="#d4d8dc", outline="#6d747b", width=2 * S)
            cv.create_oval(x - 22, y - 22, x + 22, y + 22, fill="#9aa1a8", outline="#50565c", width=S)
            cv.create_oval(x - 15, y - 15, x + 15, y + 15, fill="#c7a64a", outline="#6b5a22", width=S)
            for i, (_, lab) in enumerate(pos):
                a = math.radians(self._key_angle(len(pos), i))
                cv.create_text(x + 50 * math.sin(a), y - 42 * math.cos(a), text=lab, fill="#1b1b1b",
                               font=(FONT, 6, "bold"))
            self.key_items[key] = (cv.create_line(0, 0, 0, 0, fill="#b4bac0", width=7 * S, capstyle="round"),
                                   cv.create_oval(0, 0, 0, 0, fill="#c9ced3", outline="#6d747b", width=S))
        self.status_txt = cv.create_text(28, 866, anchor="w", fill="#33383d", font=(FONT, 8), text="")
        cv.create_text(W - 28, 866, anchor="e", fill="#6d747b", font=(FONT, 7),
                       text="D/K dispatch  H hmi  R restraints  A ack  S/X ride  L/O lift  E reset  Space e-stop  "
                            "Z lockout  P/M/B keys  F1-F10 pages")

    def _plate(self, x, y, text, col, w=130):
        self._rr(x - w / 2, y - 13, x + w / 2, y + 13, 6, fill=col, outline="")
        self.cv.create_text(x, y, text=text, fill="#ffffff", font=(FONT, 7, "bold"))

    def _pushbutton(self, name, x, y, col):
        cv, S, r = self.cv, self.S, BTN_R
        cv.create_oval(x - r, y - r, x + r, y + r, fill="#d0d4d8", outline="#6d747b", width=2 * S)
        cv.create_oval(x - r + 6, y - r + 6, x + r - 6, y + r - 6, fill="#8d949b", outline="")
        if col == "black":
            ring = cv.create_oval(x - 24, y - 24, x + 24, y + 24, fill="", outline=LENS["black"][1], width=4 * S)
            lens = cv.create_oval(x - 20, y - 20, x + 20, y + 20, fill="#101214", outline="#000", width=S)
            self.lens[name] = (lens, ring, col)
            return
        lens = cv.create_oval(x - 26, y - 26, x + 26, y + 26, fill=LENS[col][1], outline="#111", width=2 * S)
        glint = cv.create_oval(x - 16, y - 18, x + 2, y - 8, fill="", outline="")
        self.lens[name] = (lens, glint, col)

    @staticmethod
    def _key_angle(n, i):
        return (-45, 45)[i] if n == 2 else (-50, 0, 50)[i]

    # ================================================================ input
    def _hit_button(self, x, y):
        for name, (bx, by, *_) in BUTTONS.items():
            if (x - bx) ** 2 + (y - by) ** 2 <= BTN_R ** 2:
                return name
        return None

    def _mouse_down(self, e, right=False):
        x, y = e.x / self.S, e.y / self.S
        if (x - ESTOP[0]) ** 2 + (y - ESTOP[1]) ** 2 <= 42 ** 2:
            self._toggle_estop()
            return
        if (x - LOTO[0]) ** 2 + (y - LOTO[1]) ** 2 <= 50 ** 2:
            self._toggle_lockout()
            return
        for key, kx, ky, _, pos in KEYS:
            if (x - kx) ** 2 + (y - ky) ** 2 <= 40 ** 2:
                self._turn_key(key, -1 if right else 1)
                return
        if right:
            return
        b = self._hit_button(x, y)
        if b:
            self.held_btn = b
            self.logic.hold(b, True)
            self.logic.press(b)
            return
        if HX <= x < HX + HW * K and HY <= y < HY + HH * K:
            for x0, y0, x1, y1, fn in reversed(self.hits):
                if x0 <= x < x1 and y0 <= y < y1:
                    fn()
                    self._render()
                    return

    def _mouse_up(self, _e):
        if self.held_btn:
            self.logic.hold(self.held_btn, False)
            self.held_btn = None

    def _key_down(self, e):
        if isinstance(e.widget, tk.Entry):
            return
        k = e.keysym.lower()
        if k in self.kb_down:           # ignore keyboard auto-repeat
            return
        self.kb_down.add(k)
        if k in KEYBOARD:
            self.logic.hold(KEYBOARD[k], True)
            self.logic.press(KEYBOARD[k])
        elif k == "space":
            self._toggle_estop()
        elif k == "z":
            self._toggle_lockout()
        elif k in ("p", "m", "b"):
            self._turn_key({"p": "panel", "m": "mode", "b": "maint"}[k], 1)
        elif k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= len(PAGES):
            self.page = PAGES[int(k[1:]) - 1]
            return "break"

    def _key_up(self, e):
        k = e.keysym.lower()
        self.kb_down.discard(k)
        if k in KEYBOARD:
            self.logic.hold(KEYBOARD[k], False)

    def _turn_key(self, key, step):
        pos = next(p for kk, _, _, _, p in KEYS if kk == key)
        vals = [v for v, _ in pos]
        i = (vals.index(self.keys[key]) + step) % len(vals)
        self.keys[key] = vals[i]
        self.logic.set_key(key, vals[i])

    def _toggle_lockout(self):
        self.keys["lockout"] = not self.keys["lockout"]
        self.logic.set_key("lockout", self.keys["lockout"])

    def _toggle_estop(self):
        self.estop_pushed = not self.estop_pushed
        self.logic.set_estop_pb(self.estop_pushed)

    # ================================================================ refresh
    def _tick(self):
        try:
            self._render()
        finally:
            self.root.after(150, self._tick)

    def _beep_tick(self):
        try:
            _, lamps = self.logic.snapshot()
            self.beeper.set(bool(self.settings.get("beeper", True)) and lamp_lit(lamps.get("beeper", OFF), time.time()))
        finally:
            self.root.after(40, self._beep_tick)

    def _render(self):
        t = time.time()
        view, lamps = self.logic.snapshot()
        S = self.S
        for name, (lens, glint, col) in self.lens.items():
            lit = lamp_lit(lamps.get(BUTTONS[name][6], OFF), t)
            pressed = name == self.held_btn or any(KEYBOARD.get(k) == name for k in self.kb_down)
            if col == "black":
                self.cv.itemconfig(glint, outline=LENS[col][0] if lit else LENS[col][1])
                self.cv.itemconfig(lens, width=(3 if pressed else 1) * S)
                continue
            self.cv.itemconfig(lens, fill=LENS[col][0] if lit else LENS[col][1], width=(4 if pressed else 2) * S)
            self.cv.itemconfig(glint, fill="#ffffff" if lit else "", stipple="gray50")
        # E-stop lens (pushed = sits lower / smaller)
        x, y = ESTOP
        r = 23 if self.estop_pushed else 28
        self.cv.coords(self.estop_lens, S * (x - r), S * (y - r), S * (x + r), S * (y + r))
        lit = lamp_lit(lamps.get("estop", OFF), t)
        self.cv.itemconfig(self.estop_lens, fill=LENS["red"][0] if lit else LENS["red"][1])
        self.cv.itemconfig(self.estop_glint, fill="#ffffff" if lit else "", stipple="gray50")
        self.cv.itemconfig(self.estop_txt, text="PUSHED - click to pull" if self.estop_pushed else "")
        # lockout handle
        x, y = LOTO
        a = math.radians(45 if self.keys["lockout"] else -45)
        ca, sa = math.cos(a), math.sin(a)
        poly = []
        for px, py in ((-9, 44), (9, 44), (9, -36), (0, -46), (-9, -36)):
            poly += [S * (x + px * ca - py * sa), S * (y + px * sa + py * ca)]
        self.cv.coords(self.loto_handle, *poly)
        dx, dy = 30 * sa, -30 * ca
        self.cv.coords(self.loto_dot, S * (x + dx - 4), S * (y + dy - 4), S * (x + dx + 4), S * (y + dy + 4))
        # beeper ring
        beep = self.settings.get("beeper", True) and lamp_lit(lamps.get("beeper", OFF), t)
        self.cv.itemconfig(self.beep_ring, outline="#ff5a36" if beep else "")
        # key switches
        for key, kx, ky, _, pos in KEYS:
            i = [v for v, _ in pos].index(self.keys[key])
            a = math.radians(self._key_angle(len(pos), i))
            line, bow = self.key_items[key]
            ex, ey = kx + 26 * math.sin(a), ky - 26 * math.cos(a)
            self.cv.coords(line, S * (kx - 10 * math.sin(a)), S * (ky + 10 * math.cos(a)), S * ex, S * ey)
            self.cv.coords(bow, S * (ex - 9), S * (ey - 9), S * (ex + 9), S * (ey + 9))
        msg = view.get("msg")
        status = view.get("status", "")
        if msg and t - msg[0] < 6:
            status += "   |   " + msg[2]
        self.cv.itemconfig(self.status_txt, text=status)
        self._render_hmi(view, t)
        self.cv.scale("hmi", 0, 0, S, S)

    # ================================================================ HMI drawing helpers
    # Page coordinates -> HMI pixels (ox, oy, pk) -> panel units (HX, HY, K). Legacy pages were designed for
    # 800x480 and are drawn scaled into the 1024x768 screen.
    def X(self, x):
        return HX + (self.ox + x * self.pk) * K

    def Y(self, y):
        return HY + (self.oy + y * self.pk) * K

    def px(self, v):
        return max(1.0, v * self.pk * K * self.S)

    def fnt(self, size, bold=False):
        return FONT, -max(6, round(size * 1.333 * self.pk * K * self.S)), "bold" if bold else "normal"

    def R(self, x0, y0, x1, y1, fill, outline="", width=1):
        return self.cv.create_rectangle(self.X(x0), self.Y(y0), self.X(x1), self.Y(y1), fill=fill, outline=outline,
                                        width=self.px(width), tags="hmi")

    def RR(self, x0, y0, x1, y1, fill, outline="", width=1, r=8):
        x0, y0, x1, y1 = self.X(x0), self.Y(y0), self.X(x1), self.Y(y1)
        r = min(r * self.pk * K, (x1 - x0) / 2, (y1 - y0) / 2)
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
               x0, y1 - r, x0, y0 + r, x0, y0]
        return self.cv.create_polygon(pts, smooth=True, fill=fill, outline=outline, width=self.px(width), tags="hmi")

    def T(self, x, y, text, fill=TXT, size=10, bold=False, anchor="w", **kw):
        if "width" in kw:
            kw["width"] = kw["width"] * self.pk * K * self.S
        return self.cv.create_text(self.X(x), self.Y(y), text=text, fill=fill, anchor=anchor, tags="hmi",
                                   font=self.fnt(size, bold), **kw)

    def B(self, x0, y0, x1, y1, text, fn=None, active=False, enabled=True, fill=None, size=9, fg=None):
        f = fill or (BTN_ON if active else BTN)
        if not enabled:
            f = "#5fae8e" if active else BTN_OFF
        self.RR(x0 + 1, y0 + 2, x1 + 1, y1 + 2, "#0c1680", r=7)               # drop shadow
        self.RR(x0, y0, x1, y1, f, outline="#0b0f14", r=7)
        self.RR(x0 + 3, y0 + 3, x1 - 3, (y0 + y1) / 2, _mix(f, "#ffffff", 0.18), r=5)   # gloss
        self.T((x0 + x1) / 2, (y0 + y1) / 2, text, fill=fg or (TXT if enabled else "#e4e7ff"), size=size, bold=True,
               anchor="center", justify="center")
        if fn and enabled:
            self.hits.append((self.X(x0), self.Y(y0), self.X(x1), self.Y(y1), fn))

    def LED(self, x, y, on, color=C_FREE, r=6):
        self.cv.create_oval(self.X(x - r), self.Y(y - r), self.X(x + r), self.Y(y + r),
                            fill=color if on else "#243078", outline="#0b0f14", tags="hmi")

    def cmd(self, *a):
        return lambda: self.logic.intent(*a)

    def go(self, page):
        return lambda: setattr(self, "page", page)

    def pager(self, key, n, per, y):
        pg = min(self.sub.get(key, 0), max(0, (n - 1) // per))
        self.sub[key] = pg
        pages = max(1, (n + per - 1) // per)
        if pages > 1:
            self.B(520, y, 610, y + 28, "< PREV", lambda: self.sub.__setitem__(key, max(0, pg - 1)), enabled=pg > 0)
            self.T(655, y + 14, f"{pg + 1} / {pages}", anchor="center")
            self.B(700, y, 790, y + 28, "NEXT >", lambda: self.sub.__setitem__(key, pg + 1), enabled=pg < pages - 1)
        return pg * per, pg * per + per

    def _hmi_space(self):
        self.ox, self.oy, self.pk = 0.0, 0.0, 1.0

    def _legacy_space(self):
        self.ox, self.oy, self.pk = 0.0, 125 - 48 * 1.28, 1.28

    # ================================================================ HMI
    def _render_hmi(self, v, t):
        self.cv.delete("hmi")
        self.hits = []
        self._hmi_space()
        self.R(0, 0, HW, HH, BG)
        self._top_strip(v, t)
        snap = v.get("snap")
        connected = bool(v.get("connected") and snap)
        if not connected and self.page not in ("MENU", "SETUP"):
            self.RR(88, 180, 936, 420, "#000000", outline=CYAN, width=3, r=16)
            self.T(512, 250, "NO CONNECTION TO NL2BRIDGE", size=22, bold=True, anchor="center",
                   fill=C_OCC if int(t * 2) % 2 else TXT)
            self.T(512, 310, v.get("status", ""), size=12, anchor="center", fill=DIM, width=800)
            self.T(512, 350, f"{self.settings['host']}:{self.settings['port']}  -  inject NL2Bridge.dll into "
                   "NoLimits 2 on the game PC, or set its IP address", size=11, anchor="center", fill=DIM)
            self.B(392, 372, 632, 410, "CONNECTION SETTINGS", self._settings_dialog, size=11, fg=TXT)
            self._nav(v, t)
            return
        if self.page == "HOME":
            self.page_home(v, snap, t)
        elif self.page == "MENU":
            self.page_menu(v, snap, t)
        elif self.page == "SELECT":
            self.page_select(v, snap, t)
            self._nav(v, t)
        elif self.page in ("DAILY", "OVERVIEW"):
            getattr(self, "page_" + self.page.lower())(v, snap, t)
            self._nav(v, t)
        else:
            self._legacy_space()
            getattr(self, "page_" + self.page.lower())(v, snap, t)
            self._hmi_space()
            self._nav(v, t)

    def _top_strip(self, v, t):
        hmi = v.get("hmi") or {}
        self.B(28, 38, 155, 110, "Main\nMenu", self.go("MENU"), size=13, fg=TXT)
        self.T(162, 74, hmi.get("title", "Six Flags\nRide Operation"), fill="#000000", size=13, anchor="w")
        self.B(278, 38, 405, 110, "Operator\nSelect", self.go("SELECT"), size=11, fg="#000000")
        self.T(512, 20, time.strftime("%m/%d/%y %a %I:%M:%S %p").upper(), size=10, bold=True, anchor="w",
               fill="#d8f6ff")
        for i, m in enumerate(("AUTO", "MANUAL", "TRANSFER")):
            y0 = 33 + i * 30
            on = self.keys["mode"] == m
            self.RR(625, y0, 690, y0 + 22, "#39e35c" if on else "#5c6fd6", outline="#0b0f14", r=4)
            self.T(657, y0 + 11, m.capitalize(), size=7, anchor="center", fill="#000000")
        estop_bad = bool(self.estop_pushed or self.keys["lockout"] or v.get("game_estop"))
        blink = int(t * 2) % 2 == 0
        self._status_sq(800, 18, "E-Stops\nOK" if not estop_bad else "E-Stop\nActive", not estop_bad, blink)
        faulted = bool(v.get("faulted")) or not v.get("connected")
        self._status_sq(906, 18, "Status\nOK" if not faulted else "Status\nFault", not faulted, blink)

    def _status_sq(self, x, y, text, ok, blink):
        fill = "#0f4a22" if ok else ("#d01818" if blink else "#7a0c0c")
        self.RR(x - 3, y - 3, x + 91, y + 75, "#6d747b", r=12)
        self.RR(x, y, x + 88, y + 72, fill, outline="#050607", width=2, r=10)
        self.RR(x + 6, y + 5, x + 82, y + 30, _mix(fill, "#ffffff", 0.15), r=8)
        self.T(x + 44, y + 36, text, size=10, anchor="center", fill="#23b347" if ok else "#ffffff", justify="center")

    def _nav(self, v, t):
        al = v.get("alarms", [])
        msg = v.get("msg")
        if al:
            a = al[0]
            col = {"fault": "#b91c1c", "warn": "#b45309"}.get(a["level"], "#1e40af")
            if not a["acked"] and int(t * 2) % 2:
                col = "#4b5563"
            self.R(0, 596, HW, 632, col)
            self.T(12, 614, ("! " if a["active"] else "") + a["text"], size=10, bold=True)
        elif msg and t - msg[0] < 6:
            self.R(0, 596, HW, 632, "#b45309" if msg[1] == "warn" else "#1e40af")
            self.T(12, 614, msg[2], size=10, bold=True)
        nw = HW / len(NAV)
        unacked = any(not a["acked"] for a in al)
        for i, (p, lab) in enumerate(NAV):
            flash = p == "ALARMS" and unacked and int(t * 2) % 2 == 0
            self.B(i * nw + 4, 650, (i + 1) * nw - 4, 740, lab, self.go(p), active=p == self.page,
                   fill="#b91c1c" if flash else None, size=10)

    # ---------------------------------------------------------------- operator (home) page
    def _sq(self, cx, cy, sz, fill, fn=None):
        h = sz / 2
        self.RR(cx - h - 2, cy - h - 2, cx + h + 2, cy + h + 2, "#0b1a4a", r=5)
        self.RR(cx - h, cy - h, cx + h, cy + h, fill, outline="#9aa8b8", width=1, r=4)
        self.RR(cx - h + 3, cy - h + 3, cx + h - 3, cy - 1, _mix(fill, "#ffffff", 0.22), r=3)
        if fn:
            self.hits.append((self.X(cx - h - 4), self.Y(cy - h - 4), self.X(cx + h + 4), self.Y(cy + h + 4), fn))

    def _zone_row(self, v, snap, zones, y_sq, y_lab, rtl, t):
        if not zones:
            return
        det = snap["detail"]
        manual = v["mode"] != "auto"
        bc = self.logic.bc
        pitch, gap = 34.0, 44.0
        # station / lift blocks draw Enter / Park / Exit squares in place of their single block square
        nsq = [len(z["squares"]) + (2 if z.get("phased") else 0) for z in zones]
        widths = [max(n * pitch, len(z["label"]) * 9.0 + 14) for z, n in zip(zones, nsq)]
        total = sum(widths) + gap * (len(zones) - 1)
        k = min(1.0, 740.0 / total)
        pitch, gap, widths = pitch * k, gap * k, [w * k for w in widths]
        pos = 500 - total * k / 2
        spans, mids = [], []
        for z, w, n in zip(zones, widths, nsq):
            c0 = pos + w / 2 - (n - 1) * pitch / 2
            spans.append((z, [c0 + i * pitch for i in range(n)]))
            pos += w + gap
            mids.append(pos - gap / 2)
        mids.pop()
        d = 9
        if rtl:
            spans = [(z, [1000 - c for c in cs]) for z, cs in spans]
            mids, d = [1000 - m for m in mids], -9
        aw = self.px(3)
        shape = (8 * K * self.S, 10 * K * self.S, 4 * K * self.S)
        for m in mids:
            self.cv.create_line(self.X(m - d), self.Y(y_sq), self.X(m + d), self.Y(y_sq), fill=CYAN, width=aw,
                                arrow="last", arrowshape=shape, tags="hmi")
        for z, centres in spans:
            can_adv = False
            if manual and v["ride_running"]:
                if bc and z["sid"] in bc.by_sid:
                    can_adv = bc.by_sid[z["sid"]].can_advance(False)
                else:
                    can_adv = bool(det.get(z["sid"], {}).get("canAdvanceFwd"))
            blink = int(t * 2) % 2 == 0
            groups = z["squares"]
            if z.get("phased"):
                # squares: (sections lit by, is_block, advance here); phase squares first, then the trims
                first = groups[0]
                ph = rl.block_phase(z["lift"], det.get(z["sid"]), snap["blocks"].get(z["sid"]))
                after = any(det.get(s, {}).get("occupied") for s in first[1:])
                items = [(ph == 0, True, False), (ph == 1, True, True), (ph == 2 or after, True, False)]
                items += [(any(det.get(s, {}).get("occupied") for s in g), False, False) for g in groups[1:]]
            else:
                items = [(any(det.get(s, {}).get("occupied") for s in g), z["sid"] in g, z["sid"] in g)
                         for g in groups]
            for c, (occ, is_block, adv_here) in zip(centres, items):
                fill = SQ_ON if occ else SQ_OFF
                fn = None
                if adv_here and can_adv:
                    fill = SQ_ADV if blink else fill
                    fn = self.cmd("advance", z["sid"], False)
                self._sq(c, y_sq, pitch * (0.72 if is_block else 0.5), fill, fn)
            self.T(sum(centres) / len(centres), y_lab, z["label"], size=14, anchor="center", fill="#ffffff")

    def page_home(self, v, snap, t):
        lay = v.get("layout") or {"top": [], "bottom": [], "storage": []}
        hmi = v.get("hmi") or {}
        # track loop
        self.RR(88, 145, 910, 383, "", outline=CYAN, width=4, r=22)
        aw = self.px(4)
        shape = (10 * K * self.S, 12 * K * self.S, 5 * K * self.S)
        for x0, y0, x1, y1 in ((910, 350, 910, 380), (88, 240, 88, 225)):
            self.cv.create_line(self.X(x0), self.Y(y0), self.X(x1), self.Y(y1), fill=CYAN, width=aw, arrow="last",
                                arrowshape=shape, tags="hmi")
        self._zone_row(v, snap, lay.get("top", []), 143, 175, False, t)
        self._zone_row(v, snap, lay.get("bottom", []), 383, 350, True, t)
        # lift -> block brake timer
        self.R(137, 230, 203, 280, "#000000", outline="#ffffff", width=2)
        self.T(170, 255, "Lift to\nBlock Brake\nTime [S]", size=6, anchor="center", justify="center")
        self.R(137, 288, 197, 322, "#000000", outline=CYAN, width=2)
        self.T(167, 305, f"{int(v.get('timer') or 0)}", size=11, anchor="center")
        st = v.get("stats") or {}
        # people box
        self.R(225, 230, 538, 327, "#000000", outline=CYAN, width=3)
        for i, (val, lab) in enumerate(((st.get("pph", 0), "People Per Hour"), (st.get("prev_people", 0), "Previous Hour"),
                                        (st.get("ppd", 0), "People Per Day"))):
            y = 255 + i * 26
            self.T(262, y, str(val), size=10, anchor="e", fill="#5dff9b")
            self.T(276, y, lab, size=10)
        self.T(389, 290, "Current Riders", size=10)
        self.R(476, 262, 524, 296, "#000000", outline="#ffffff", width=2)
        self.T(500, 279, str(st.get("current", 0)), size=11, anchor="center")
        # dispatch box
        self.R(571, 223, 856, 330, "#000000", outline=CYAN, width=3)
        for i, (lab, val) in enumerate((("Dispatches per hour", st.get("dph", 0)), ("Previous Hour", st.get("prev_disp", 0)),
                                        ("Dispatches per day", st.get("dpd", 0)), ("Total Dispatches", st.get("total", 0)))):
            y = 243 + i * 24
            self.T(581, y, lab, size=10)
            self.T(735, y, str(val), size=10, anchor="center")
        # storage / transfer: one spot per block section off the main circuit
        store = lay.get("storage", [])
        det = snap["detail"]
        rows = store or [dict(label=f"Storage {i + 1}", sid=None) for i in range(3)]
        dy = 37 if len(rows) <= 3 else 88 / (len(rows) - 1)
        y0 = 444 if len(rows) <= 3 else 430
        for i, s in enumerate(rows):
            y = y0 + i * dy
            self.T(312, y, s["label"], size=14 if dy >= 30 else 12, anchor="e",
                   fill="#ffffff" if s.get("sid") is not None else DIM)
            self.cv.create_line(self.X(335), self.Y(y), self.X(415), self.Y(y), fill=CYAN, width=self.px(3),
                                tags="hmi")
            occ = bool(s.get("sid") is not None and det.get(s["sid"], {}).get("occupied"))
            self._sq(372, y, min(26, dy * 0.72), SQ_ON if occ else SQ_OFF if s.get("sid") is not None else "#303a8c")
        # train parked in station
        self.T(110, 461, "Train Parked\nIn Station", size=9, anchor="center", justify="center")
        self.R(62, 486, 160, 517, "#000000", outline="#ffffff", width=2)
        self.T(111, 502, str(v.get("parked_train", 0)), size=11, anchor="center")
        # warning box
        self.RR(432, 432, 922, 537, "#5b6068", r=6)
        self.R(436, 436, 918, 533, "#050607", outline="#2a2d30", width=2)
        self._warnings(v, t)
        # station devices
        sts = snap["stations"]
        stn = sts[min(v["station_sel"], len(sts) - 1)] if sts else None
        rows = max(1, min(24, int(v.get("n_rows") or hmi.get("rows", 8))))
        seats = max(1, min(6, int(hmi.get("seats_per_row", 4))))
        xs = [188 + i * (880 - 188) / max(1, rows - 1) for i in range(rows)] if rows > 1 else [534]
        rsc = min(1.0, ((880 - 188) / max(1, rows - 1)) / 76.0) if rows > 1 else 1.0
        has = bool(stn and stn["hasTrain"])
        g = stn["gates"] if stn else {}
        gates_locked = bool(g.get("present") and g.get("closed"))
        gates_moving = bool(g.get("present") and (g.get("moving") or g.get("opening") or g.get("closing")))
        self.T(118, 553, "Gates", size=14, anchor="center")
        for x in xs:
            # green = gate closed and locked; blinks while moving
            lit = gates_locked or (gates_moving and int(t * 6) % 2)
            gw = 13 * rsc
            self.RR(x - gw, 536, x + gw, 560, "#39e35c" if lit else "#3a3f44", outline="#0b0f14", r=5)
            self.RR(x - gw + 3, 539, x + gw - 3, 547, _mix("#39e35c" if lit else "#3a3f44", "#ffffff", 0.25), r=3)
        run = v["ride_running"]
        bw = 33 * rsc
        for i, x in enumerate(xs):
            self.B(x - bw, 574, x + bw, 636, f"Release\nRow {i + 1}" if rsc > 0.7 else f"Row\n{i + 1}",
                   self.cmd("release", i), fill="#4ccf4a", enabled=True, size=10 if rsc > 0.85 else 8)
        hn = stn["harness"] if stn else {}
        h_open = bool(hn.get("open")) or (has and not hn.get("closed"))
        h_moving = bool(hn.get("moving") or hn.get("opening") or hn.get("closing"))
        rs = (snap.get("rows") or {}).get("rows") or []
        pitch = min(30.0, 120.0 / seats)
        for i, x in enumerate(xs):
            if i < len(rs):
                ro, rm = not rs[i]["closed"], rs[i]["opening"] or rs[i]["closing"]
            else:
                ro, rm = h_open, h_moving
            for j in range(seats):
                y = 657 + j * pitch
                self._seat(x, y, pitch, has, ro, rm and int(t * 6) % 2, rsc)
        # left buttons
        self.B(0, 545, 66, 608, "Add\n5\nRiders", self.cmd("add_riders"), size=7)
        self.B(0, 623, 66, 686, "Rider\nCount\nUp", self.cmd("rider_up"), size=7)
        self.B(0, 698, 66, 761, "Rider\nCount\nDown", self.cmd("rider_down"), size=7)
        self.T(118, 698, "Main Operator\nPanel", size=7, anchor="center", justify="center")
        for i, (x0, name) in enumerate(((73, "ad_l"), (123, "ad_r"))):
            down = name == self.held_btn or any(KEYBOARD.get(k) == name for k in self.kb_down)
            self.RR(x0, 718, x0 + 40, 758, "#c9ced3", r=8)
            self.RR(x0 + 4, 722, x0 + 36, 754, "#39e35c" if down else "#2a2d30", outline="#000", r=6)
            self.T(x0 + 20, 738, "PB", size=9, anchor="center", fill="#000000" if down else "#e6e6e6")
        # right buttons
        plat = bool(stn and stn["platform"].get("present"))
        fly = bool(stn and stn["flyer"].get("present"))
        gat = bool(stn and g.get("present"))
        self.B(938, 448, 1010, 512, "Raise/Lower\nFloor", self.cmd("floor_toggle"), enabled=plat and run, size=8)
        self.B(938, 525, 1010, 590, "Lock/Unlock\nFlyer", self.cmd("flyer_toggle"), enabled=fly and run, size=8)
        self.B(943, 603, 1010, 668, "Release\nAll\nRows", self.cmd("release"), enabled=has and run, size=8)
        self.B(938, 686, 1010, 750, "Open/Close\nGates", self.cmd("gates_toggle"), enabled=gat and run, size=8)

    def _seat(self, x, y, pitch, has, h_open, blink, sc=1.0):
        h = pitch * 0.42
        outline, fill = "#1d7382", "#0d3b44"        # no train
        if has:
            outline, fill = ("#ffd21f", "#0d3b44") if h_open else ("#39e35c", "#0d3b1a")  # open / locked (green)
        if blink:
            outline = "#ffffff"
        self.RR(x - 22 * sc, y - h, x + 14 * sc, y + h, fill, outline=outline, width=1.5, r=4)
        self.RR(x - 27 * sc, y - h + 2, x - 21 * sc, y + h - 2, fill, outline=outline, width=1.5, r=2)
        self.cv.create_arc(self.X(x + 2 * sc), self.Y(y - h), self.X(x + 24 * sc), self.Y(y + h), start=-90,
                           extent=180, style="arc", outline=outline, width=self.px(2), tags="hmi")

    def _warnings(self, v, t):
        lines = []
        for a in v.get("alarms", []):
            if a["active"] and a["level"] == "fault":
                lines.append((a["text"], "#ff4d4d"))
        if v.get("faulted"):
            txt = "FAULT LATCHED: " + (v.get("fault_text") or "") + " - MAINTENANCE ENABLE + ACKNOWLEDGE to reset"
            lines.insert(0, (txt, "#ff4d4d" if int(t * 2) % 2 == 0 else "#ff9a9a"))
        for w in v.get("warnings", []):
            lines.append((w, "#ffd21f"))
        for a in v.get("alarms", []):
            if a["active"] and a["level"] == "warn":
                lines.append((a["text"], "#ffd21f"))
        msg = v.get("msg")
        if msg and t - msg[0] < 6:
            lines.append((msg[2], "#9fd2ff"))
        seen, uniq = set(), []
        for text, col in lines:
            if text not in seen:
                seen.add(text)
                uniq.append((text, col))
        if not uniq:
            self.T(677, 485, "No Warning messages at this time", size=10, anchor="center")
            return
        for i, (text, col) in enumerate(uniq[:4]):
            self.T(446, 450 + i * 22, text, size=9, fill=col, bold=i == 0, width=460)

    # ---------------------------------------------------------------- main menu / operator select / daily test
    def page_menu(self, v, snap, t):
        self.T(512, 150, "Main Menu", size=20, bold=True, anchor="center")
        items = [("HOME", "Operator"), ("OVERVIEW", "Track\nOverview"), ("STATION", "Station"), ("BLOCKS", "Blocks"),
                 ("TRANSFER", "Transfer"), ("MAINT", "Maintenance"), ("ALARMS", "Alarms"), ("DAILY", "Daily Test"),
                 ("SETUP", "Setup")]
        n_alarm = sum(1 for a in v.get("alarms", []) if a["active"] or not a["acked"])
        for i, (p, lab) in enumerate(items):
            cx, cy = 222 + (i % 3) * 290, 260 + (i // 3) * 150
            if p == "ALARMS" and n_alarm:
                lab += f" ({n_alarm})"
            if p == "DAILY" and v.get("connected") and not v.get("daily_ok"):
                lab += "\n(not done)"
            self.B(cx - 125, cy - 55, cx + 125, cy + 55, lab, self.go(p), size=15)
        self.T(512, 720, "F1 Operator  F2 Menu  F3 Track  F4 Station  F5 Blocks  F6 Transfer  F7 Maint  F8 Alarms  "
               "F9 Daily Test  F10 Setup", size=9, anchor="center", fill=DIM)

    def page_select(self, v, snap, t):
        self.T(40, 150, "Operator Select", size=18, bold=True)
        sts = snap["stations"]
        self.T(40, 200, "Operating station (the panel dispatches this station)", size=10, fill=DIM)
        for i, s in enumerate(sts[:8]):
            x = 40 + (i % 4) * 240
            y = 220 + (i // 4) * 80
            self.B(x, y, x + 220, y + 64, self.lab(v, s["sectionId"], s["name"], 20), self.cmd("station_sel", i),
                   active=i == v["station_sel"], size=12)
        self.T(40, 400, "Coaster (reconnects to another coaster in the park)", size=10, fill=DIM)
        for i, name in enumerate(snap["info"].get("coasters", [])[:8]):
            x = 40 + (i % 4) * 240
            y = 420 + (i // 4) * 80
            self.B(x, y, x + 220, y + 64, short(name, 20), lambda n=name: self._pick_coaster(n),
                   active=name == snap["coaster"]["name"], size=12)

    def page_daily(self, v, snap, t):
        self.T(40, 150, "Daily Test", size=18, bold=True)
        ok = v.get("daily_ok")
        req = self.settings.get("require_daily_test", True)
        if not req:
            status, col = "Daily test not required (Setup)", DIM
        elif ok:
            status, col = "Daily test complete - valid until 3:00 AM", C_FREE
        else:
            status, col = "Daily test has not been completed today", C_BUSY
        self.T(40, 190, status, size=12, bold=True, fill=col)
        seq = v.get("seq", 0)
        active = v.get("daily_active")
        self.RR(40, 220, 620, 580, PANE, outline=CYAN, width=2, r=10)
        for i, (code, text, done) in enumerate(v.get("daily_checks", [])):
            y = 255 + i * 44
            self.LED(70, y, done or (ok and not active), C_FREE, r=11)
            self.T(95, y, text, size=12, fill=TXT if active else DIM)
        if seq == 15 and not active:
            self.B(660, 240, 980, 330, "START\nDAILY TEST", self.cmd("daily_start"), size=15)
        elif active:
            self.B(660, 240, 980, 330, "TEST IN PROGRESS", None, active=int(t * 2) % 2 == 0, size=13)
        self.T(660, 360, "The daily test runs during startup, after the lamp test (ACKNOWLEDGE). Operate every "
               "device in the list once: push and pull the E-STOP, turn the E-STOP LOCKOUT on and off, and press "
               "each button. The test stays valid until 3:00 AM.", size=10, fill=DIM, width=330)

    # ---------------------------------------------------------------- legacy pages (800x480 layout, scaled)
    @staticmethod
    def lab(v, sid, name, n=16):
        """Operator name of a block (Station, Lift 2, Brake 3, Transfer, Storage 1 ...)."""
        return ((v.get("layout") or {}).get("labels") or {}).get(sid) or short(name, n)

    def _block_color(self, v, snap, sid):
        b = snap["blocks"].get(sid)
        if not b or not b["isBlock"]:
            return C_NONE
        if b["trains"]:
            return C_OCC
        st = b["state"]
        if st == 19:
            txt = v["bc_states"].get(sid)
            return C_FREE if txt in (None, "Free") else C_BUSY
        if st == 2:
            return C_FREE
        if st == 1:
            return C_OFF
        if st == 10:
            return C_FM
        return C_BUSY

    # ---------------------------------------------------------------- track overview (operator-screen layout)
    @staticmethod
    def _train_sid(snap, tr):
        """Section under the train's front (the trains' holding-block field is not its current block)."""
        by_id = {x["id"]: x for x in snap["info"]["sections"]}
        f = tr["front"]
        best, bd = None, None
        for sid in tr["sections"]:
            x = by_id.get(sid)
            if not x or x["track"] != f["track"]:
                continue
            d = max(0.0, x["trackStart"] - f["pos"], f["pos"] - x["trackEnd"])
            if bd is None or d < bd:
                best, bd = sid, d
        return best if best is not None else (tr["sections"][0] if tr["sections"] else None)

    def _ov_assign(self, snap, tiles):
        """One tile per train: the tile holding the section under its front."""
        self._ov_map, self._ov_where = {}, {}
        for tr in snap["trains"]:
            fs = self._train_sid(snap, tr)
            sid = next((sid for sid, secs in tiles if fs in secs), None)
            if sid is None:
                sid = next((sid for sid, secs in tiles if set(tr["sections"]) & set(secs)), None)
            if sid is not None:
                self._ov_map.setdefault(sid, []).append(tr["index"] + 1)
            self._ov_where[tr["index"]] = sid if sid is not None else fs

    def _ov_tile(self, v, snap, cx, cy, w, sid, secs, label, t, extra=None, can_adv=False):
        h = 52
        det = snap["detail"]
        col = C_OCC if any(det.get(x, {}).get("occupied") for x in secs) else self._block_color(v, snap, sid)
        if can_adv and int(t * 2) % 2 == 0:
            col = SQ_ADV
        x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        self.RR(x0 - 2, y0 - 2, x1 + 2, y1 + 2, "#0b1a4a", r=7)
        self.RR(x0, y0, x1, y1, col, outline="#0b0f14", r=6)
        self.RR(x0 + 3, y0 + 3, x1 - 3, cy - 4, _mix(col, "#ffffff", 0.25), r=4)
        self.T(cx, cy - 9, label, size=12, bold=True, anchor="center", fill="#0b0f14")
        b = snap["blocks"].get(sid, {})
        state = extra or v["bc_states"].get(sid) or b.get("stateName", "")
        self.T(cx, cy + 12, short(state, max(8, int(w / 7))), size=8, anchor="center", fill="#0b0f14")
        for i, n in enumerate(self._ov_map.get(sid, [])[:3]):
            bx, by = x1 - 6 - i * 24, y0 - 4
            self.cv.create_oval(self.X(bx - 12), self.Y(by - 12), self.X(bx + 12), self.Y(by + 12), fill="#ffffff",
                                outline="#0b0f14", width=self.px(2), tags="hmi")
            self.T(bx, by, str(n), size=11, bold=True, anchor="center", fill="#0b0f14")
        if can_adv:
            self.hits.append((self.X(x0), self.Y(y0), self.X(x1), self.Y(y1), self.cmd("advance", sid, False)))

    @staticmethod
    def _xfer_extra(v, snap, sid):
        xf, sw = v.get("xfer") or {}, snap["switches"]
        if xf.get("present") and sid == xf.get("xfer") and xf.get("table") is not None and xf["table"] < len(sw):
            tb = sw[xf["table"]]
            return "table moving" if tb["moving"] or tb["current"] < 0 else f"table POS {tb['current'] + 1}"
        return None

    # ---------------------------------------------------------------- live track loop
    OV_BOX = (150, 205, 590, 385)       # stadium loop (x0, y0, x1, y1) in HMI space

    def _ov_pt(self, u, off=0.0):
        """Point on the loop at ride fraction u (clockwise from the station on the bottom straight) and its outward
        normal. off > 0 moves the point outwards."""
        x0, y0, x1, y1 = self.OV_BOX
        r = (y1 - y0) / 2
        ls = x1 - x0 - 2 * r
        P = 2 * ls + 2 * math.pi * r
        d = (u * P + 0.18 * ls) % P
        cy = y0 + r
        if d < ls:                                    # bottom straight, right -> left
            return x1 - r - d, y1 + off, 0.0, 1.0
        d -= ls
        if d < math.pi * r:                           # left curve, bottom -> top
            a = d / r
            nx, ny = -math.sin(a), math.cos(a)
            return x0 + r + (r + off) * nx, cy + (r + off) * ny, nx, ny
        d -= math.pi * r
        if d < ls:                                    # top straight, left -> right
            return x0 + r + d, y0 - off, 0.0, -1.0
        a = (d - ls) / r                              # right curve, top -> bottom
        nx, ny = math.sin(a), -math.cos(a)
        return x1 - r + (r + off) * nx, cy + (r + off) * ny, nx, ny

    @classmethod
    def _ov_perimeter(cls):
        x0, y0, x1, y1 = cls.OV_BOX
        r = (y1 - y0) / 2
        return 2 * (x1 - x0 - 2 * r) + 2 * math.pi * r

    def _ov_path(self, v, snap):
        """Ride-order path around the loop: [(zone, sid, u0, u1)]. Each block zone gets a share of the loop that
        is part equal, part proportional to its track length, so short blocks stay readable."""
        lay = v.get("layout") or {}
        top, bottom = lay.get("top", []), lay.get("bottom", [])
        zones = bottom[1:] + top + bottom[:1] if top else list(bottom)
        by_id = {x["id"]: x for x in snap["info"]["sections"]}
        ln = lambda sid: max(1.0, by_id[sid]["trackEnd"] - by_id[sid]["trackStart"]) if sid in by_id else 1.0
        zl = [sum(ln(s) for s in z["secs"]) for z in zones]
        total, n = sum(zl) or 1.0, max(1, len(zones))
        path, u = [], 0.0
        for z, L in zip(zones, zl):
            share = 0.35 * L / total + 0.65 / n
            w = {s: (max(ln(s), 0.35 * L) if s == z["sid"] else ln(s)) for s in z["secs"]}
            ws = sum(w.values())
            for s in z["secs"]:
                du = share * w[s] / ws
                path.append((z, s, u, u + du))
                u += du
        return path

    def _ov_train_u(self, snap, path, tr):
        by_id = {x["id"]: x for x in snap["info"]["sections"]}
        for pos in (tr["center"], tr["front"], tr["rear"]):
            for z, sid, u0, u1 in path:
                s = by_id.get(sid)
                if s and s["track"] == pos["track"] and s["trackStart"] - 0.5 <= pos["pos"] <= s["trackEnd"] + 0.5:
                    f = (pos["pos"] - s["trackStart"]) / max(1e-6, s["trackEnd"] - s["trackStart"])
                    return u0 + min(1.0, max(0.0, f)) * (u1 - u0)
        return None

    def _text_w(self, text, size, bold=True):
        if not hasattr(self, "_mfont"):
            import tkinter.font as tkfont
            self._mfont = {b: tkfont.Font(family=FONT, size=-40, weight="bold" if b else "normal") for b in (0, 1)}
        return max(self._mfont[bool(bold)].measure(line) for line in text.split("\n")) * size * 1.333 / 40

    def _ov_labels(self, items):
        """Spread labels along the loop so none overlap. items: dicts with u (ideal), w, h. Sets 'uu' (placed) and
        'side' (+1 outside, -1 inside the loop)."""
        P = self._ov_perimeter()

        def extent(it):
            _, _, nx, ny = self._ov_pt(it["u"])
            return abs(ny) * it["w"] + abs(nx) * it["h"]      # size along the track direction

        need = sum(extent(it) + 10 for it in items)
        groups = [items] if need < 0.92 * P else [items[0::2], items[1::2]]
        for gi, grp in enumerate(groups):
            for it in grp:
                it["side"], it["d"] = (1 if gi == 0 else -1), it["u"] * P
            grp.sort(key=lambda it: it["d"])
            m = len(grp)
            for _ in range(300):
                moved = False
                for i in range(m):
                    a, b = grp[i], grp[(i + 1) % m]
                    if m < 2:
                        break
                    gap = (b["d"] - a["d"]) % P if m > 1 else P
                    req = (extent(a) + extent(b)) / 2 + 8
                    if gap < req - 0.1:
                        push = (req - gap) / 2
                        a["d"] -= push
                        b["d"] += push
                        moved = True
                for it in grp:
                    it["d"] += 0.03 * (it["u"] * P - it["d"])
                if not moved:
                    break
            for it in grp:
                it["uu"] = (it["d"] / P) % 1.0

    def page_overview(self, v, snap, t):
        lay = v.get("layout") or {"top": [], "bottom": [], "storage": []}
        labels = lay.get("labels") or {}
        main_sids = {x for z in lay.get("top", []) + lay.get("bottom", []) for x in z["secs"]}
        off = [x for x in snap["info"]["sections"] if x["isBlock"] and x["id"] not in main_sids]
        self._ov_assign(snap, [(z["sid"], z["secs"]) for z in lay.get("top", []) + lay.get("bottom", [])] +
                        [(x["id"], [x["id"]]) for x in off])
        self.RR(12, 128, 732, 588, PANE, r=8)
        self.T(26, 144, "TRACK OVERVIEW", size=10, bold=True, fill=DIM)
        self.T(718, 144, f"Block system: {v['mode'].upper()}", size=10, bold=True, anchor="e",
               fill=C_FM if v["mode"] == "fullmanual" else TXT)
        det, bc = snap["detail"], self.logic.bc
        by_id = {x["id"]: x for x in snap["info"]["sections"]}
        manual = v["mode"] != "auto" and v["ride_running"]
        blink = int(t * 2) % 2 == 0
        path = self._ov_path(v, snap)

        def line(u0, u1, col, width, off=0.0):
            n = max(2, int((u1 - u0) * self._ov_perimeter() / 5) + 2)
            pts = []
            for i in range(n):
                x, y, _, _ = self._ov_pt(u0 + (u1 - u0) * i / (n - 1) - 1e-6 * (i == n - 1), off)
                pts += [self.X(x), self.Y(y)]
            self.cv.create_line(*pts, fill=col, width=self.px(width), capstyle="butt", tags="hmi")

        line(0.0, 1.0, "#0b1a4a", 14)
        # zone colours (block state, blinking when it can be advanced)
        zcol, zadv = {}, {}
        for z in {id(p[0]): p[0] for p in path}.values():
            can = False
            if manual:
                if bc and z["sid"] in bc.by_sid:
                    can = bc.by_sid[z["sid"]].can_advance(False)
                else:
                    can = bool(det.get(z["sid"], {}).get("canAdvanceFwd"))
            occ = any(det.get(s, {}).get("occupied") for s in z["secs"])
            col = C_OCC if occ else self._block_color(v, snap, z["sid"])
            zcol[z["sid"]] = SQ_ADV if can and blink else col
            zadv[z["sid"]] = can
        for z, sid, u0, u1 in path:
            zc = zcol[z["sid"]]
            if sid == z["sid"]:
                line(u0, u1, zc, 10)
            else:
                line(u0, u1, C_OCC if det.get(sid, {}).get("occupied") else _mix(zc, PANE, 0.55), 6)
            s = by_id.get(sid, {})
            if sid == z["sid"] or (s.get("hasBrake") and not s.get("isBlock")):
                x, y, nx, ny = self._ov_pt(u0)
                k = 9 if sid == z["sid"] else 6               # block start / trim brake tick
                self.cv.create_line(self.X(x - nx * k), self.Y(y - ny * k), self.X(x + nx * k), self.Y(y + ny * k),
                                    fill="#0b0f14" if sid == z["sid"] else "#ffffff", width=self.px(2), tags="hmi")
        # ride direction
        x0, y0, x1, y1 = self.OV_BOX
        cy, aw = (y0 + y1) / 2, self.px(3)
        shape = (8 * K * self.S, 10 * K * self.S, 4 * K * self.S)
        for xa, ya, xb, yb in ((x0 + 26, cy + 12, x0 + 26, cy - 12), (x1 - 26, cy - 12, x1 - 26, cy + 12),
                               ((x0 + x1) / 2 + 12, y1 - 20, (x0 + x1) / 2 - 12, y1 - 20),
                               ((x0 + x1) / 2 - 12, y0 + 20, (x0 + x1) / 2 + 12, y0 + 20)):
            self.cv.create_line(self.X(xa), self.Y(ya), self.X(xb), self.Y(yb), fill=CYAN, width=aw, arrow="last",
                                arrowshape=shape, tags="hmi")
        # labels: one per block, spread out so they never stack
        items = []
        for z, sid, u0, u1 in path:
            if sid != z["sid"]:
                continue
            text = z["label"]
            extra = self._xfer_extra(v, snap, z["sid"])
            if extra:
                text += "\n" + extra
            items.append(dict(z=z, u=(u0 + u1) / 2, text=text, w=self._text_w(text, 10) + 14,
                              h=19 * (text.count("\n") + 1) + 4))
        if items:
            self._ov_labels(items)
        for it in items:
            z, side = it["z"], it["side"]
            bx, by_, nx, ny = self._ov_pt(it["uu"], 0)
            nx, ny = nx * side, ny * side
            gap = 13
            cx = bx + nx * (gap + it["w"] / 2)
            cy_ = by_ + ny * (gap + it["h"] / 2)
            tx, ty, _, _ = self._ov_pt(it["u"], 8 * side)
            ex, ey = cx - nx * it["w"] / 2, cy_ - ny * it["h"] / 2
            if abs(it["uu"] - it["u"]) * self._ov_perimeter() > 5:
                self.cv.create_line(self.X(tx), self.Y(ty), self.X(ex), self.Y(ey), fill=DIM, width=self.px(1),
                                    tags="hmi")
            col = zcol[z["sid"]]
            bx0, by0, bx1, by1 = cx - it["w"] / 2, cy_ - it["h"] / 2, cx + it["w"] / 2, cy_ + it["h"] / 2
            self.RR(bx0, by0, bx1, by1, "#0b1a4a", outline=col, width=2, r=5)
            self.T(cx, cy_, it["text"], size=10, bold=True, anchor="center", justify="center", fill="#ffffff")
            if zadv[z["sid"]]:
                self.hits.append((self.X(bx0), self.Y(by0), self.X(bx1), self.Y(by1),
                                  self.cmd("advance", z["sid"], False)))
        # trains at their live position
        for tr in snap["trains"]:
            u = self._ov_train_u(snap, path, tr)
            if u is None:
                continue
            x, y, _, _ = self._ov_pt(u)
            self.cv.create_oval(self.X(x - 11), self.Y(y - 11), self.X(x + 11), self.Y(y + 11), fill="#ffffff",
                                outline="#0b0f14", width=self.px(2), tags="hmi")
            self.T(x, y, str(tr["index"] + 1), size=11, bold=True, anchor="center", fill="#0b0f14")
        # storage / transfer blocks that are not part of the circuit
        if off:
            self.T(26, 452, "STORAGE / TRANSFER", size=10, bold=True, fill=DIM)
            w = min(150.0, (650 - 14 * (len(off[:5]) - 1)) / max(1, len(off[:5])))
            for i, s in enumerate(off[:5]):
                self._ov_tile(v, snap, 60 + w / 2 + i * (w + 14), 506, w, s["id"], [s["id"]],
                              labels.get(s["id"]) or short(s["name"], 14), t, extra=self._xfer_extra(v, snap, s["id"]))
        # legend
        for i, (lab, col) in enumerate((("free", C_FREE), ("reserved / moving", C_BUSY), ("occupied", C_OCC),
                                        ("offline", C_OFF), ("full manual", C_FM), ("advance (press)", SQ_ADV))):
            x = 30 + i * 117
            self.RR(x, 564, x + 16, 578, col, outline="#0b0f14", r=3)
            self.T(x + 21, 571, lab, size=8, fill=DIM)
        # trains + station
        self.RR(742, 128, 1012, 588, PANE, r=8)
        self.T(754, 144, "TRAINS", size=10, bold=True, fill=DIM)
        for i, tr in enumerate(snap["trains"][:8]):
            y = 174 + i * 30
            self.T(754, y, f"T{tr['index'] + 1}", size=12, bold=True)
            w = self._ov_where.get(tr["index"])
            nm = next((x["name"] for x in snap["info"]["sections"] if x["id"] == w), "-")
            self.T(792, y, self.lab(v, w, nm, 12) if w is not None else "-", size=11)
            self.T(1002, y, mph(tr["speed"]), size=11, anchor="e", fill="#e6ebff")
        sts = snap["stations"]
        if sts:
            st = sts[min(v["station_sel"], len(sts) - 1)]
            y = 174 + min(8, len(snap["trains"])) * 30 + 16
            self.T(754, y, self.lab(v, st["sectionId"], st["name"], 16).upper(), size=10, bold=True, fill=DIM)
            rows = [("Train in station", st["hasTrain"]),
                    ("Gates closed", st["gates"]["closed"] or not st["gates"]["present"]),
                    ("Restraints locked", st["harness"]["closed"]), ("Ready to dispatch", st["canDispatch"]),
                    ("Game auto dispatch", bool(v.get("game_auto_dispatch")))]
            for i, (lab, on) in enumerate(rows):
                self.LED(764, y + 28 + i * 26, on, r=7)
                self.T(780, y + 28 + i * 26, lab, size=10)

    def page_station(self, v, snap, t):
        sts = snap["stations"]
        if not sts:
            self.T(400, 200, "This coaster has no stations", size=14, anchor="center")
            return
        sel = min(v["station_sel"], len(sts) - 1)
        for i, s in enumerate(sts[:6]):
            self.B(10 + i * 130, 48, 134 + i * 130, 76, self.lab(v, s["sectionId"], s["name"], 16), self.cmd("station_sel", i),
                   active=i == sel)
        st = sts[sel]
        self.R(8, 84, 330, 405, PANE)
        self.T(18, 98, "STATUS", size=9, bold=True, fill=DIM)
        b = snap["blocks"].get(st["sectionId"], {})
        rows = [("Train in station", st["hasTrain"], C_FREE), ("Train ready", st["trainReady"], C_FREE),
                ("Manual dispatch (panel)", st["manualDispatch"], C_FREE),
                ("Waiting for clear block", st["waitingForClearBlock"], C_BUSY),
                ("Waiting for advance", st["waitingForAdvance"], C_BUSY),
                ("Ready to dispatch", v.get("dispatch_ready"), C_FREE),
                ("Ride running", v["ride_running"], C_FREE), ("E-stop", v["estop"], C_OCC)]
        for i, (lab, on, col) in enumerate(rows):
            self.LED(26, 122 + i * 24, on, col)
            self.T(40, 122 + i * 24, lab, size=10)
        block_txt = v["bc_states"].get(st["sectionId"]) or b.get("stateName", "")
        self.T(18, 314, "Block: " + block_txt, size=10, fill="#e6ebff")
        if st["hasTrain"] and not st["canDispatch"]:
            self.T(18, 336, "Not ready: " + RideLogic._why_no_dispatch(st, v["mode"]), size=9, fill=C_BUSY,
                   width=300)
        elif v.get("dispatch_ready"):
            self.T(18, 336, "Hold ADVANCE & DISPATCH to dispatch", size=9, fill=C_FREE, width=300)
        gad = bool(v.get("game_auto_dispatch"))
        self.B(18, 366, 320, 398, "GAME AUTO DISPATCH: " + ("ON" if gad else "OFF"), self.cmd("game_auto_dispatch"),
               active=gad)
        self.R(338, 84, 792, 405, PANE)
        self.T(348, 98, "STATION DEVICES" + ("" if v["hmi_ok"] else "   - press HMI ENABLE to operate"), size=9,
               bold=True, fill=DIM if v["hmi_ok"] else C_BUSY)
        devs = [("GATES", st["gates"], "open", "closed", "OPEN", "CLOSE", "gates_open", "gates_close", "canOpen",
                 "canClose"),
                ("RESTRAINTS", st["harness"], "open", "closed", "OPEN", "CLOSE", "harness_open", "harness_close",
                 "canOpen", "canClose"),
                ("FLOOR", st["platform"], "raised", "lowered", "RAISE", "LOWER", "platform_raise", "platform_lower",
                 "canRaise", "canLower"),
                ("FLYER LOCK", st["flyer"], "locked", "unlocked", "LOCK", "UNLOCK", "flyer_lock", "flyer_unlock",
                 "canLock", "canUnlock")]
        y = 118
        for name, d, s1, s2, b1, b2, c1, c2, e1, e2 in devs:
            if name != "RESTRAINTS" and not d.get("present"):
                continue
            moving = d.get("moving") or d.get("opening") or d.get("closing")
            state = "MOVING" if moving else s1.upper() if d.get(s1) else s2.upper() if d.get(s2) else "-"
            self.T(350, y + 20, name, size=11, bold=True)
            self.T(350, y + 42, state, size=10, fill=C_BUSY if moving else "#e6ebff")
            pos = max(0.0, min(1.0, d.get("position", 0.0)))
            self.R(460, y + 36, 560, y + 46, "#0b0f14")
            self.R(460, y + 36, 460 + 100 * pos, y + 46, "#4ad7ff")
            self.B(580, y + 12, 680, y + 50, b1, self.cmd(c1), enabled=bool(d.get(e1)), active=bool(d.get(s1)))
            self.B(690, y + 12, 785, y + 50, b2, self.cmd(c2), enabled=bool(d.get(e2)), active=bool(d.get(s2)))
            y += 66

    def page_blocks(self, v, snap, t):
        secs = [s for s in snap["info"]["sections"] if s["isBlock"]]
        order = list(((v.get("layout") or {}).get("labels") or {}).keys())
        secs.sort(key=lambda s: order.index(s["id"]) if s["id"] in order else len(order))
        manual = v["mode"] != "auto"
        self.R(8, 48, 792, 405, PANE)
        cols = [(16, "BLOCK"), (200, "STATE"), (390, "TRN"), (430, "BRAKE"), (500, "LIFT"), (570, "TRANSP"),
                (650, "ADVANCE")]
        for x, lab in cols:
            self.T(x, 62, lab, size=8, bold=True, fill=DIM)
        per = 11
        a, b = self.pager("blocks", len(secs), per, 372)
        brk, lft, trn = {0: "open", 1: "closed", 2: "trim"}, {0: "off", 1: "fwd", 2: "idle", 3: "bwd"}, \
            {0: "off", 1: "on", 2: "brake"}
        for i, s in enumerate(secs[a:b]):
            y = 84 + i * 26
            bl = snap["blocks"].get(s["id"], {})
            self.R(12, y - 11, 22, y + 9, self._block_color(v, snap, s["id"]))
            self.T(28, y, self.lab(v, s["id"], s["name"], 22), size=9, bold=bool(s["station"]))
            self.T(200, y, v["bc_states"].get(s["id"]) or bl.get("stateName", ""), size=9)
            self.T(398, y, str(bl.get("trains", 0)), size=9)
            self.T(430, y, brk.get(bl.get("brakeMode"), "-") if bl.get("hasBrake") else "-", size=9)
            self.T(500, y, lft.get(bl.get("liftMode"), "-") if bl.get("hasLift") else "-", size=9)
            self.T(570, y, trn.get(bl.get("transportMode"), "-") if bl.get("hasTransport") else "-", size=9)
            if manual:
                bc = self.logic.bc
                if bc and s["id"] in bc.by_sid:
                    bh = bc.by_sid[s["id"]]
                    fwd_ok, bwd_ok = bh.can_advance(False), bh.can_advance(True)
                    has_bwd = bh.bwd is not None
                else:
                    fwd_ok, bwd_ok, has_bwd = bl.get("canAdvanceFwd"), bl.get("canAdvanceBwd"), bl.get("canAdvanceBwd")
                self.B(650, y - 11, 715, y + 10, "ADV >", self.cmd("advance", s["id"], False), enabled=bool(fwd_ok),
                       size=8)
                if has_bwd:
                    self.B(722, y - 11, 787, y + 10, "< ADV", self.cmd("advance", s["id"], True),
                           enabled=bool(bwd_ok), size=8)
        if not manual:
            self.T(16, 386, "Advance buttons are available in MANUAL / TRANSFER mode", size=9, fill=DIM)

    def page_transfer(self, v, snap, t):
        ilk = v.get("xfer_ilk", [])
        ok = bool(ilk) and all(x for _, x in ilk)
        self.R(8, 48, 792, 405, PANE)
        self.T(18, 64, "TRANSFER PERMISSIVES", size=9, bold=True, fill=DIM)
        for i, (lab, good) in enumerate(ilk):
            x, y = 18 + (i % 3) * 258, 88 + (i // 3) * 22
            self.LED(x + 8, y, good, C_FREE if good else C_OCC)
            self.T(x + 20, y, lab, size=9)
        if not ok:
            msg, col = "Transfer locked out - all permissives must be made", C_BUSY
        elif not v["hmi_ok"]:
            msg, col = "Press HMI ENABLE, then select a position", C_BUSY
        else:
            msg, col = "Transfer permitted - select a position", C_FREE
        self.T(18, 142, msg, size=10, bold=True, fill=col)
        sw = snap["switches"]
        xo = v.get("xfer") or {}
        xs = v.get("xseq")
        if not sw:
            self.T(18, 180, "This coaster has no switches or transfer tracks", size=11)
        for i, s in enumerate(sw[:2] if xo.get("present") else sw[:4]):
            y = 162 + i * 54
            self.T(18, y + 12, ((v.get("layout") or {}).get("switch_labels") or {}).get(s["index"]) or short(s["name"], 26), size=11, bold=True)
            kind = {"transferTable": "Transfer table"}.get(s.get("type"), s.get("type", "").capitalize())
            state = "MOVING" if s["moving"] else f"position {s['current'] + 1}"
            self.T(18, y + 34, f"{kind} - {state}", size=9, fill=C_BUSY if s["moving"] else "#e6ebff")
            n = s["directions"]
            bw = min(90, (500 - 8 * n) // max(1, n))
            for d in range(n):
                x = 280 + d * (bw + 8)
                target = s["target"] == d and s["current"] != d
                fill = C_BUSY if target and int(t * 2) % 2 else None
                self.B(x, y, x + bw, y + 44, f"POS {d + 1}", self.cmd("switch", s["index"], d),
                       active=s["current"] == d, enabled=ok and s["switchable"] and not s["moving"] and not xs,
                       fill=fill)
        if xo.get("present"):
            y = 272
            self.R(12, y - 8, 788, y + 96, "#1a2a9a")
            self.T(22, y + 6, "TRAIN MOVE - transfer table", size=9, bold=True, fill=DIM)
            can = ok and v["hmi_ok"] and not xs
            blink = int(t * 2) % 2
            self.B(22, y + 20, 212, y + 66, "ADVANCE TRAIN", self.cmd("xfer_advance"), enabled=can,
                   fill=C_FREE if can and xo.get("advance") else None, size=11)
            self.B(222, y + 20, 412, y + 66, "REVERSE TRAIN", self.cmd("xfer_reverse"), enabled=can,
                   fill=C_FREE if can and xo.get("reverse") else None, size=11)
            self.B(422, y + 20, 512, y + 66, "STOP", self.cmd("xfer_stop"), enabled=bool(xs),
                   fill=C_OCC if xs else None, size=11)
            if xs:
                stage = {"mode": "switching to full manual", "move": "moving", "settle": "stopping",
                         "back": "returning to manual block"}.get(xs["stage"], xs["stage"])
                self.T(524, y + 32, xs["name"], size=10, bold=True, fill=C_BUSY if blink else TXT)
                self.T(524, y + 54, stage.upper(), size=9, fill=C_BUSY)
            else:
                self.T(524, y + 32, "ADVANCE: " + (xo.get("advance") or "-"), size=9,
                       fill=C_FREE if xo.get("advance") else DIM)
                self.T(524, y + 54, "REVERSE: " + (xo.get("reverse") or "-"), size=9,
                       fill=C_FREE if xo.get("reverse") else DIM)
            main = xo.get("main")
            store = ",  ".join(f"POS {k + 1} = {n}" for k, n in sorted((xo.get("storage") or {}).items()))
            self.T(22, y + 84, (f"Main line = POS {main + 1}" if main is not None else
                                "Main line position not learned yet (run the ride in AUTO once)") +
                   ("   |   " + store if store else "") +
                   "   |   Works with restraints open or closed", size=8, fill=DIM)
        off = [s for s in snap["info"]["sections"] if s["isBlock"] and s["track"] != 0]
        if off:
            self.T(18, 386, "Off-track blocks: " + ",  ".join(
                f"{self.lab(v, s['id'], s['name'], 12)} ({snap['blocks'].get(s['id'], {}).get('trains', 0)} trn)" for s in off[:6]),
                size=9, fill=DIM)

    def page_maint(self, v, snap, t):
        keys = v["keys"]
        self.R(8, 48, 792, 405, PANE)
        self.T(18, 64, "MAINTENANCE", size=10, bold=True, fill=DIM)
        self.LED(150, 64, keys["maint"], "#ffb21e")
        self.T(162, 64, "Maint key " + ("ON" if keys["maint"] else "OFF"), size=9)
        fm_ok = v["panel"] and (keys["maint"] or keys["mode"] != "AUTO")
        self.B(270, 52, 400, 78, "FULL MANUAL", self.cmd("full_manual", True), active=v["mode"] == "fullmanual",
               enabled=fm_ok and not v.get("xseq"))
        self.B(408, 52, 560, 78, "EXIT FULL MANUAL", self.cmd("full_manual", False),
               enabled=v["full_manual"] and not v.get("xseq"))
        armed = v.get("reset_armed")
        self.B(600, 52, 782, 78, "CONFIRM RESET" if armed else "SIMULATION RESET", self.cmd("sim_reset"),
               enabled=v["panel"], fill=C_OCC if armed and int(t * 4) % 2 else None, size=8)
        if v["scripted"]:
            live = keys["maint"] and v["mode"] == "fullmanual"
            if not live:
                self.T(18, 96, "MAINTENANCE key ON + FULL MANUAL to drive devices directly (block controller paused)",
                       size=9, fill=C_BUSY)
        else:
            live = fm_ok and not v.get("xseq")
            if not snap.get("dev_api", True):
                self.T(18, 96, "Device control needs NL2Bridge v5 - install the new DLL", size=9, fill=C_BUSY)
                live = False
            elif v["mode"] == "fullmanual":
                self.T(18, 96, "FULL MANUAL - block system off. EXIT FULL MANUAL when every train sits on one block.",
                       size=9, fill=C_BUSY)
            elif live:
                self.T(18, 96, "Pressing a device button puts the ride into FULL MANUAL (block system off)",
                       size=9, fill=DIM)
            else:
                self.T(18, 96, "OPERATION MODE = MANUAL or TRANSFER (or the MAINTENANCE key) to drive devices",
                       size=9, fill=C_BUSY)
        devs = snap.get("devices") or {}
        secs = [s for s in snap["info"]["sections"] if s["isBlock"] and (
                snap["blocks"].get(s["id"], {}).get("hasBrake") or snap["blocks"].get(s["id"], {}).get("hasLift")
                or snap["blocks"].get(s["id"], {}).get("hasTransport"))]
        per = 8
        a, b = self.pager("maint", len(secs), per, 372)
        for i, s in enumerate(secs[a:b]):
            y = 112 + i * 32
            bl = snap["blocks"][s["id"]]
            self.T(18, y + 12, self.lab(v, s["id"], s["name"], 18), size=9, bold=True)
            if v["scripted"]:
                brk = (("OPEN", "open", bl["brakeMode"] == 0), ("CLOSE", "closed", bl["brakeMode"] == 1),
                       ("TRIM", "trim", bl["brakeMode"] == 2))
                lft = (("L FWD", "fwd", bl["liftMode"] == 1), ("L OFF", "off", bl["liftMode"] == 0),
                       ("L BWD", "bwd", bl["liftMode"] == 3))
                trn = (("T OFF", "off", bl["transportMode"] == 0), ("T FWD", "fwd", bl["transportMode"] == 1),
                       ("T BWD", "bwd", False), ("LAUNCH", "launchfwd", False))
            else:
                d = devs.get(s["id"]) or {}
                bs, ls, ts = ((d.get(k) or {}).get("state") if (d.get(k) or {}).get("present") else None
                              for k in ("brake", "lift", "transport"))
                brk = (("OPEN", "open", bs == 0), ("CLOSE", "closed", bs == 1))
                lft = (("L ON", "fwd", ls == 1), ("L OFF", "off", ls == 0))
                trn = (("T OFF", "off", ts == 0), ("T FWD", "fwd", ts == 1), ("T BWD", "bwd", ts == 2))
            x = 160
            if bl["hasBrake"]:
                for lab, m, act in brk:
                    self.B(x, y, x + 52, y + 26, lab, self.cmd("brakes", s["id"], m), active=act, enabled=live, size=8)
                    x += 55
            x = 330
            if bl["hasLift"]:
                for lab, m, act in lft:
                    self.B(x, y, x + 52, y + 26, lab, self.cmd("lift", s["id"], m), active=act, enabled=live, size=8)
                    x += 55
            x = 505
            if bl["hasTransport"]:
                for lab, m, act in trn:
                    self.B(x, y, x + 64, y + 26, lab, self.cmd("transport", s["id"], m), active=act, enabled=live,
                           size=8)
                    x += 68
        if not secs:
            self.T(18, 140, "This coaster has no brake / lift / transport blocks", size=10)
        if v.get("bc_path"):
            self.T(18, 392, "Ride file: " + v["bc_path"], size=8, fill=DIM, width=760)

    def page_alarms(self, v, snap, t):
        sub = self.sub.get("alarm_view", "ALARMS")
        self.B(10, 48, 130, 76, "ALARMS", lambda: self.sub.__setitem__("alarm_view", "ALARMS"), active=sub == "ALARMS")
        self.B(136, 48, 256, 76, "HISTORY", lambda: self.sub.__setitem__("alarm_view", "HISTORY"),
               active=sub == "HISTORY")
        self.B(262, 48, 382, 76, "GAME EVENTS", lambda: self.sub.__setitem__("alarm_view", "EVENTS"),
               active=sub == "EVENTS")
        self.B(530, 48, 650, 76, "SILENCE", self.cmd("silence"), enabled=bool(v.get("faulted")))
        self.B(660, 48, 790, 76, "ACKNOWLEDGE", lambda: self.logic.press("ack"))
        self.R(8, 84, 792, 405, PANE)
        cols = {"fault": "#ff5a5a", "warn": "#ffc233", "info": "#8fc5ff"}
        if sub == "ALARMS":
            rows = v.get("alarms", [])
            if not rows:
                self.T(18, 104, "No alarms", size=10, fill=DIM)
            for i, a in enumerate(rows[:12]):
                y = 100 + i * 25
                self.R(14, y - 9, 22, y + 9, cols.get(a["level"], DIM))
                self.T(30, y, time.strftime("%H:%M:%S", time.localtime(a["t"])), size=9, fill=DIM)
                self.T(100, y, a["text"], size=9, bold=not a["acked"], width=560)
                self.T(785, y, ("ACTIVE" if a["active"] else "") + ("" if a["acked"] else " UNACK"), size=8,
                       anchor="e", fill=cols.get(a["level"], DIM))
        elif sub == "HISTORY":
            for i, (ts, lvl, text) in enumerate(v.get("log", [])[:12]):
                y = 100 + i * 25
                self.R(14, y - 9, 22, y + 9, cols.get(lvl, DIM))
                self.T(30, y, time.strftime("%H:%M:%S", time.localtime(ts)), size=9, fill=DIM)
                self.T(100, y, text, size=9, width=680)
        else:
            if not snap.get("events_enabled"):
                self.T(18, 100, "Game events are not available from this NL2Bridge instance (restart NoLimits 2 and "
                       "inject the v3 DLL first).", size=9, fill=C_BUSY, width=760)
            for i, (ts, ev) in enumerate(v.get("events", [])[:12]):
                y = 124 + i * 23
                extra = ev.get("sensorName") or ev.get("sectionName") or ""
                if "train" in ev:
                    extra += f"  train {ev['train'] + 1}"
                self.T(30, y, time.strftime("%H:%M:%S", time.localtime(ts)), size=9, fill=DIM)
                self.T(100, y, ev["typeName"], size=9, bold=True)
                self.T(260, y, extra, size=9)

    def page_setup(self, v, snap, t):
        s = self.settings
        self.R(8, 48, 792, 405, PANE)
        self.T(18, 64, "CONNECTION", size=9, bold=True, fill=DIM)
        self.LED(26, 90, v.get("connected"), C_FREE if v.get("connected") else C_OCC)
        self.T(40, 90, f"NL2Bridge {s['host']}:{s['port']}  -  " + v.get("status", ""), size=10, width=740)
        self.B(18, 108, 178, 142, "SETTINGS...", self._settings_dialog)
        self.B(186, 108, 346, 142, "RECONNECT", lambda: setattr(self.link, "reconnect", True))
        if v.get("bc_path"):
            self.B(354, 108, 560, 142, "OPEN RIDE FILE", lambda: os.startfile(v["bc_path"]))
        if v.get("connected") and snap:
            self.T(18, 164, "COASTER (click to control another coaster in the park)", size=9, bold=True, fill=DIM)
            for i, name in enumerate(snap["info"].get("coasters", [])[:12]):
                x, y = 18 + (i % 4) * 192, 180 + (i // 4) * 40
                self.B(x, y, x + 184, y + 32, short(name, 22), lambda n=name: self._pick_coaster(n),
                       active=name == snap["coaster"]["name"])
        self.T(18, 318, "OPTIONS", size=9, bold=True, fill=DIM)
        yn = {True: "yes", False: "no"}
        self.T(18, 342, f"Dispatch: {'both buttons' if s['two_hand_dispatch'] else 'either button'}, hold "
               f"{s['dispatch_hold_s']} s   |   HMI enable "
               f"{'required, ' + str(s['hmi_enable_seconds']) + ' s' if s['require_hmi_enable'] else 'not required'}"
               f"   |   Daily test required: {yn[bool(s['require_daily_test'])]}   |   RIDE STOP e-stops the game: "
               f"{yn[bool(s['ride_stop_estop'])]}   |   Beeper: {'on' if s['beeper'] else 'muted'}   |   Game auto "
               f"dispatch: {'ON' if v.get('game_auto_dispatch') else 'off (controller dispatches)'}   |   "
               f"Scan {s['scan_hz']} Hz",
               size=9, width=760)
        self.T(18, 390, "Ride file (HMI labels, run-time limits, scripted blocks): controller\\rides\\<coaster>.json",
               size=8, fill=DIM)

    def _pick_coaster(self, name):
        self.settings["coaster"] = name
        save_settings(self.settings)
        self.link.reconnect = True

    def _settings_dialog(self):
        s = self.settings
        d = tk.Toplevel(self.root)
        d.title("Settings")
        d.transient(self.root)
        d.resizable(False, False)
        fields = [("host", "Game PC IP address or name (127.0.0.1 = this PC)"), ("port", "NL2Bridge port (default 15152)"),
                  ("coaster", "Coaster (name or index, empty = first)"),
                  ("dispatch_hold_s", "Dispatch hold time (s)"), ("hmi_enable_seconds", "HMI enable time (s)"),
                  ("scan_hz", "Scan rate (Hz)")]
        ents = {}
        for i, (k, lab) in enumerate(fields):
            tk.Label(d, text=lab, anchor="w").grid(row=i, column=0, sticky="w", padx=8, pady=3)
            e = tk.Entry(d, width=28)
            e.insert(0, str(s[k]))
            e.grid(row=i, column=1, padx=8, pady=3)
            ents[k] = e
        bools = {}
        for j, (k, lab) in enumerate((("two_hand_dispatch", "Dispatch needs BOTH Advance & Dispatch buttons"),
                                      ("require_hmi_enable", "Touch commands need HMI ENABLE"),
                                      ("require_daily_test", "Require the daily test at startup"),
                                      ("ride_stop_estop", "RIDE STOP e-stops the game (like the real PLC)"),
                                      ("beeper", "Beeper sound"))):
            var = tk.BooleanVar(value=bool(s[k]))
            tk.Checkbutton(d, text=lab, variable=var).grid(row=len(fields) + j, column=0, columnspan=2, sticky="w",
                                                           padx=8)
            bools[k] = var

        def ok():
            try:
                new = dict(host=ents["host"].get().strip() or "127.0.0.1", port=int(ents["port"].get()),
                           coaster=ents["coaster"].get().strip(),
                           dispatch_hold_s=max(0.0, float(ents["dispatch_hold_s"].get())),
                           hmi_enable_seconds=float(ents["hmi_enable_seconds"].get()),
                           scan_hz=max(1.0, float(ents["scan_hz"].get())))
            except ValueError as e:
                messagebox.showerror("Settings", f"Invalid value: {e}", parent=d)
                return
            reconnect = any(new[k] != s[k] for k in ("host", "port", "coaster", "scan_hz"))
            s.update(new)
            s.update({k: var.get() for k, var in bools.items()})
            save_settings(s)
            if reconnect:
                self.link.reconnect = True
            d.destroy()

        row = len(fields) + len(bools)
        tk.Button(d, text="Save", width=10, command=ok).grid(row=row, column=0, pady=8)
        tk.Button(d, text="Cancel", width=10, command=d.destroy).grid(row=row, column=1, pady=8)


def _mix(c1, c2, f):
    """Blend two #rrggbb colours (f = share of c2)."""
    if not c1 or not c1.startswith("#") or len(c1) != 7:
        return c1
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(a, b))


def screen_scale(root):
    """Pixels per panel unit: follow the Windows display scaling but always fit on the screen."""
    try:
        dpi = root.winfo_fpixels("1i")
    except tk.TclError:
        dpi = 96.0
    s = min(dpi / 96.0, (root.winfo_screenheight() - 110 * dpi / 96.0) / H, (root.winfo_screenwidth() - 40) / W)
    return max(0.5, s)


def main():
    ap = argparse.ArgumentParser(description="NL2 virtual ride control panel")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--coaster")
    ap.add_argument("--scale", type=float, help="panel size factor (default: fit the screen)")
    a = ap.parse_args()
    settings = load_settings()
    if not os.path.exists(SETTINGS_PATH):
        save_settings(settings)
    for k in ("host", "port", "coaster"):
        if getattr(a, k) is not None:
            settings[k] = getattr(a, k)
    logic = RideLogic(settings)
    link = Link(logic, settings)
    make_dpi_aware()
    root = tk.Tk()
    scale = a.scale or screen_scale(root)
    root.tk.call("tk", "scaling", scale * 96.0 / 72.0)
    panel = Panel(root, logic, link, settings, scale)
    link.start()

    def close():
        link.stop = True
        panel.beeper.set(False)
        link.join(timeout=2.0)   # lets the link hand the stations back to the game's auto dispatch
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    main()
