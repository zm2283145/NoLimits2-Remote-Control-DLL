#!/usr/bin/env python3
"""NL2Bridge Viewer - live block and train display for NoLimits 2.

Run:  py nl2_viewer.py            (needs NL2Bridge injected into the game)

- Shows each coaster's sections in ride order as a loop (big tiles = blocks, small tiles = track).
- Ride order is learned automatically by watching trains move and saved to viewer_layout.json,
  so it's right from the start the next time. Use "Relearn order" if you change the park.
- Sections the main loop never passes through (storage tracks, spurs) are shown in a separate row
  together with the transfer table / switch positions.
- Trains are coloured badges (T1, T2 ...) on every section they occupy; the right-hand panel lists them.
- The viewer is read-only: it never sends commands to the game.
"""
import json, os, time, tkinter as tk
from tkinter import ttk
import nl2bridge_client as nb

POLL_MS = 150
HERE = os.path.dirname(os.path.abspath(__file__))
LAYOUT_FILE = os.path.join(HERE, "viewer_layout.json")

BG = "#15181d"; PANEL = "#1e232b"; FG = "#e6e9ef"; DIM = "#8a93a3"; TRACK = "#3a414d"
TRAIN_COLORS = ["#4ea1ff", "#ffb020", "#c678dd", "#56d364", "#ff6b6b", "#2ad4d4", "#f0e14a", "#ff8fd0"]
BRAKE = {0: "open", 1: "closed", 2: "trim"}
LIFT = {0: "off", 1: "fwd", 2: "idle", 3: "bwd"}
TRANSPORT = {0: "off", 1: "on", 2: "dep.brake"}
MODES = {1: "AUTO", 2: "MANUAL BLOCK", 3: "FULL MANUAL"}
OCCUPIED = {"Station", "Waiting for Clear Block", "Waiting for Advance", "Complete Stopping", "Wait-Time Pause",
            "Leaving Fwd", "Leaving Bwd", "Leaving Fwd to Ourself", "Leaving Bwd to Ourself", "Pass Through"}
MOVING_IN = {"Approaching Fwd", "Approaching Bwd", "Passing Fwd to Trigger", "Passing Bwd to Trigger", "Reserved"}


def state_color(state_name, has_train):
    if state_name in OCCUPIED or (has_train and state_name not in MOVING_IN):
        return "#8f1d22"
    if state_name in MOVING_IN:
        return "#a86a12"
    if state_name == "Idle":
        return "#1d6b3a"
    return "#3b4250"  # Offline / No Block / Full Manual / Scripted


class Viewer:
    def __init__(self, root):
        self.root = root
        root.title("NL2Bridge Viewer")
        root.configure(bg=BG)
        root.geometry("1400x820")
        self.bridge = None
        self.coasters = []
        self.cidx = None
        self.sections = []           # from JSON (names, flags)
        self.names = {}
        self.train_count = 0
        self.seq = {}                # train -> list of section ids in entry order
        self.prev_sets = {}          # train -> set of section ids occupied last poll
        self.edges = {}              # "a>b" -> count
        self.layout = self._load_layout()
        self.order_cache = None
        self.last_ok = 0
        self.flash = False

        top = tk.Frame(root, bg=BG); top.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(top, text="Coaster:", bg=BG, fg=DIM, font=("Segoe UI", 11)).pack(side="left")
        self.combo = ttk.Combobox(top, state="readonly", width=32, font=("Segoe UI", 11))
        self.combo.pack(side="left", padx=6)
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.select(self.combo.current()))
        tk.Button(top, text="Relearn order", command=self.relearn, bg=PANEL, fg=FG, relief="flat",
                  activebackground=TRACK, activeforeground=FG).pack(side="left", padx=6)
        self.status = tk.Label(top, text="Connecting...", bg=BG, fg=DIM, font=("Segoe UI", 11))
        self.status.pack(side="left", padx=12)
        self.mode_lbl = tk.Label(top, text="", bg=BG, fg=FG, font=("Segoe UI", 13, "bold"))
        self.mode_lbl.pack(side="right", padx=6)
        self.estop_lbl = tk.Label(top, text="", bg=BG, fg=FG, font=("Segoe UI", 13, "bold"))
        self.estop_lbl.pack(side="right", padx=6)

        body = tk.Frame(root, bg=BG); body.pack(fill="both", expand=True, padx=10, pady=4)
        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        side = tk.Frame(body, bg=PANEL, width=300); side.pack(side="right", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        tk.Label(side, text="TRAINS", bg=PANEL, fg=DIM, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        self.train_frame = tk.Frame(side, bg=PANEL); self.train_frame.pack(fill="x", padx=12)
        tk.Label(side, text="SWITCHES / TRANSFER", bg=PANEL, fg=DIM, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(18, 4))
        self.switch_frame = tk.Frame(side, bg=PANEL); self.switch_frame.pack(fill="x", padx=12)
        tk.Label(side, text="SENSORS", bg=PANEL, fg=DIM, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(18, 4))
        self.sensor_canvas = tk.Canvas(side, bg=PANEL, highlightthickness=0, height=220, width=276)
        self.sensor_canvas.pack(anchor="w", padx=12)
        tk.Label(side, text="LEGEND", bg=PANEL, fg=DIM, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(18, 4))
        for col, txt in [("#1d6b3a", "Clear / idle"), ("#a86a12", "Train approaching"), ("#8f1d22", "Occupied / holding"),
                         ("#3b4250", "Offline / no block")]:
            f = tk.Frame(side, bg=PANEL); f.pack(anchor="w", padx=12, pady=1)
            tk.Label(f, bg=col, width=2).pack(side="left"); tk.Label(f, text=" " + txt, bg=PANEL, fg=FG).pack(side="left")
        tk.Label(side, text="▶ ◀  = can advance fwd / bwd", bg=PANEL, fg=DIM).pack(anchor="w", padx=12, pady=(6, 0))
        tk.Label(side, text="Order is learned from train movement", bg=PANEL, fg=DIM, wraplength=270, justify="left").pack(anchor="w", padx=12, pady=(12, 0))

        self.root.after(100, self.tick)

    # ------------------------------------------------------------ persistence
    def _load_layout(self):
        try:
            with open(LAYOUT_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_layout(self):
        try:
            with open(LAYOUT_FILE, "w") as f:
                json.dump(self.layout, f, indent=1)
        except Exception:
            pass

    def key(self):
        c = self.coasters[self.cidx]
        return f'{c["name"]}#{c["sections"]}'

    # ------------------------------------------------------------ connection / selection
    def connect(self):
        self.bridge = nb.Bridge()
        self.coasters = [c for c in self.bridge.coasters() if c["sections"] > 0]
        self.combo["values"] = [f'{c["index"]}: {c["name"]}  ({c["trains"]} trains)' for c in self.coasters]
        if self.coasters:
            best = max(range(len(self.coasters)), key=lambda i: (self.coasters[i]["trains"], self.coasters[i]["stations"]))
            self.combo.current(best)
            self.select(best)

    def select(self, i):
        self.cidx = i
        c = self.coasters[i]
        self.sections = self.bridge.sections(c["index"])
        self.names = {s["id"]: s["name"] for s in self.sections}
        self.train_count = c["trains"]
        self.edges = dict(self.layout.get(self.key(), {}).get("edges", {}))
        self.seq, self.prev_sets, self.order_cache = {}, {}, None
        self._train_rows = None; self._last_sig = None

    def relearn(self):
        if self.cidx is None: return
        self.edges = {}
        self.layout.pop(self.key(), None); self._save_layout()
        self.seq, self.prev_sets, self.order_cache = {}, {}, None

    # ------------------------------------------------------------ order learning
    def learn(self, rows):
        changed = False
        for t in range(max(self.train_count, 1)):
            cur = {r["id"] for r in rows if r["trainMask"] >> t & 1}
            prev = self.prev_sets.get(t)
            if prev is not None:
                for sid in cur - prev:           # sections this train's front just entered
                    s = self.seq.setdefault(t, [])
                    if s and s[-1] != sid:
                        k = f"{s[-1]}>{sid}"
                        self.edges[k] = self.edges.get(k, 0) + 1
                        changed = True
                    if not s or s[-1] != sid:
                        s.append(sid); del s[:-4]
            elif cur:
                self.seq[t] = sorted(cur)[-1:]
            self.prev_sets[t] = cur
        if changed:
            self.order_cache = None
            self.layout[self.key()] = {"edges": self.edges}
            self._save_layout()

    def ride_order(self):
        if self.order_cache: return self.order_cache
        ids = [s["id"] for s in self.sections]
        succ = {}
        for k, n in self.edges.items():
            a, b = map(int, k.split(">"))
            succ.setdefault(a, []).append((n, b))
        start = next((s["id"] for s in self.sections if s["station"]), ids[0] if ids else None)
        loop, seen, cur = [], set(), start
        while cur is not None and cur not in seen:
            loop.append(cur); seen.add(cur)
            nxt = sorted(succ.get(cur, []), reverse=True)
            cur = next((b for n, b in nxt if b not in seen and n >= 1), None)
        mainline = [i for i in ids if not self.names[i].lower().startswith("storage")]
        closes = bool(loop) and any(k == f"{loop[-1]}>{start}" for k in self.edges)
        learned = len(loop) > 2 and closes and len(loop) >= 0.6 * len(mainline)
        if not learned:
            loop = [i for i in ids if not self.names[i].lower().startswith("storage")]
        if learned:
            # slot sections that were entered in the same poll as a neighbour in after their predecessor
            for i in ids:
                if i in loop or self.names[i].lower().startswith("storage"): continue
                preds = [(n, a) for k, n in self.edges.items() for a, b in [map(int, k.split(">"))] if b == i and a in loop]
                if preds:
                    a = max(preds)[1]; loop.insert(loop.index(a) + 1, i)
        rest = [i for i in ids if i not in set(loop)]
        self.order_cache = (loop, rest, learned)
        return self.order_cache

    # ------------------------------------------------------------ main loop
    def tick(self):
        try:
            if self.bridge is None:
                self.connect()
            if self.cidx is not None:
                c = self.coasters[self.cidx]
                mode, estop, rows = self.bridge.block_states(c["index"])
                switches = self.bridge.switches(c["index"]) if c["specialTracks"] else []
                try:
                    self.sensor_rows = self.bridge.sensors(c["index"])
                except Exception:
                    self.sensor_rows = []
                self.learn(rows)
                self.draw(rows, mode, estop, switches)
            self.status.config(text="Connected", fg="#56d364")
            self.last_ok = time.time()
            self.root.after(POLL_MS, self.tick)
        except Exception as e:
            self.bridge = None
            self.status.config(text=f"Disconnected ({str(e)[:60]}) - retrying...", fg="#ff6b6b")
            self.root.after(2000, self.tick)

    # ------------------------------------------------------------ drawing
    def draw(self, rows, mode, estop, switches):
        now = time.time()
        flash = int(now * 2) % 2 == 0                      # 1 Hz blink for "Waiting for Advance"
        sig = json.dumps([rows, mode, estop, switches, getattr(self, "sensor_rows", []) and
                          [(r["key"], r["active"], r["trains"]) for r in self.sensor_rows], flash, self.canvas.winfo_width(),
                          self.canvas.winfo_height(), self.ride_order()[0]], sort_keys=True, default=str)
        if sig == getattr(self, "_last_sig", None):
            return
        self._last_sig = sig
        self.flash = flash
        R = {r["id"]: r for r in rows}
        S = {s["id"]: s for s in self.sections}
        cv = self.canvas
        cv.addtag_all("old")                               # draw the new frame on top, then drop the old one
        W = max(cv.winfo_width(), 600); H = max(cv.winfo_height(), 400)
        loop, rest, learned = self.ride_order()

        self.mode_lbl.config(text=MODES.get(mode, f"MODE {mode}"))
        self.estop_lbl.config(text="  E-STOP ACTIVE  " if estop else "", bg="#c1121f" if estop else BG, fg="white")

        # Racetrack layout: top row left->right, bottom row right->left
        def w_of(sid): return 150 if S[sid]["hasNode"] else 74
        half_target = sum(w_of(i) for i in loop) / 2
        top, bot, acc = [], [], 0
        for sid in loop:
            (top if acc < half_target else bot).append(sid); acc += w_of(sid)
        gap = 8
        rows_y = [70, 330]
        def place(seq, y, reverse=False):
            total = sum(w_of(i) for i in seq) + gap * max(len(seq) - 1, 0)
            scale = min(1.0, (W - 60) / total) if total else 1
            x = 30 if not reverse else W - 30
            out = []
            for sid in seq:
                w = w_of(sid) * scale
                if reverse: x -= w
                out.append((sid, x, y, w))
                x = x + w + gap * scale if not reverse else x - gap * scale
            return out
        tiles = place(top, rows_y[0]) + place(bot, rows_y[1], reverse=True)

        # connecting track line (the loop)
        if tiles:
            pts = [(x + w / 2, y + 60) for sid, x, y, w in tiles]
            pts.append(pts[0])
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                cv.create_line(x1, y1, x2, y2, fill=TRACK, width=4)
        for sid, x, y, w in tiles:
            self.tile(cv, S[sid], R.get(sid), x, y, w)

        # side tracks / storage
        if rest:
            cv.create_text(30, 520, text="STORAGE / SIDINGS", anchor="w", fill=DIM, font=("Segoe UI", 10, "bold"))
            for sid, x, y, w in place(rest, 540):
                self.tile(cv, S[sid], R.get(sid), x, y, w)
        if not learned:
            cv.create_text(W / 2, H - 20, fill=DIM, font=("Segoe UI", 10),
                           text=f"Learning ride order... {len(self.edges)} track links seen - let the trains run a full lap (saved for next time)")

        cv.delete("old")
        self.draw_sensors()
        self.draw_trains(R)
        self.draw_switches(switches)

    def tile(self, cv, s, r, x, y, w):
        block = s["hasNode"]
        h = 120 if block else 70
        yy = y if block else y + 25
        mask = r["trainMask"] if r else 0
        state = nb.STATE[r["state"]] if r and r["state"] < len(nb.STATE) else "-"
        if block:
            col = state_color(state, mask != 0)
            if state == "Waiting for Advance" and self.flash: col = "#b3262c"
        else:
            col = "#252a33"
        outline = "#ff4d4d" if (mask and not block) else "#0d0f12"
        cv.create_rectangle(x, yy, x + w, yy + h, fill=col, outline=outline, width=2)
        name = s["name"] if not s["name"].startswith("Unnamed Section") else s["name"].replace("Unnamed Section", "Sec")
        cv.create_text(x + 6, yy + 6, text=name, anchor="nw", fill=FG, width=w - 10,
                       font=("Segoe UI", 10 if block else 8, "bold"))
        if block:
            cv.create_text(x + 6, yy + 40, text=state, anchor="nw", fill=FG, width=w - 10, font=("Segoe UI", 9))
            bits = []
            if s["hasBrake"]: bits.append("Brk " + BRAKE.get(r["brake"], "?") if r else "Brk")
            if s["hasLift"]: bits.append("Lift " + LIFT.get(r["lift"], "?") if r else "Lift")
            if s["hasTransport"]: bits.append("Tire " + TRANSPORT.get(r["transport"], "?") if r else "Tire")
            cv.create_text(x + 6, yy + h - 22, text="  ".join(bits), anchor="nw", fill="#cfd5df", width=w - 10,
                           font=("Segoe UI", 8))
            if r:
                adv = ("◀" if r["flags"] & 2 else "") + (" ▶" if r["flags"] & 1 else "")
                if adv: cv.create_text(x + w - 6, yy + 6, text=adv, anchor="ne", fill="#ffd166", font=("Segoe UI", 11, "bold"))
        # train badges
        bx = x + 6
        for t in range(32):
            if mask >> t & 1:
                c = TRAIN_COLORS[t % len(TRAIN_COLORS)]
                by = yy + (62 if block else 36)
                cv.create_oval(bx, by, bx + 30, by + 22, fill=c, outline="")
                cv.create_text(bx + 15, by + 11, text=f"T{t + 1}", fill="#0b0d10", font=("Segoe UI", 9, "bold"))
                bx += 34

    def draw_trains(self, R):
        if getattr(self, "_train_rows", None) is None or len(self._train_rows) != self.train_count:
            for wdg in self.train_frame.winfo_children(): wdg.destroy()
            self._train_rows = []
            for t in range(self.train_count):
                f = tk.Frame(self.train_frame, bg=PANEL); f.pack(fill="x", pady=3)
                tk.Label(f, text=f" T{t + 1} ", bg=TRAIN_COLORS[t % len(TRAIN_COLORS)], fg="#0b0d10",
                         font=("Segoe UI", 10, "bold")).pack(side="left")
                lbl = tk.Label(f, text="", bg=PANEL, fg=FG, justify="left", font=("Segoe UI", 10))
                lbl.pack(side="left"); self._train_rows.append(lbl)
        loop, rest, _ = self.ride_order()
        rank = {sid: i for i, sid in enumerate(loop + rest)}
        for t in range(self.train_count):
            secs = [sid for sid, r in R.items() if r["trainMask"] >> t & 1]
            blocks = [sid for sid in secs if any(s["id"] == sid and s["hasNode"] for s in self.sections)]
            where = blocks or secs
            where.sort(key=lambda sid: rank.get(sid, 999))
            if where:
                main = where[-1]
                st = R[main]["state"]
                txt = f' {self.names.get(main, main)}'
                if st < len(nb.STATE) and any(s["id"] == main and s["hasNode"] for s in self.sections):
                    txt += f'\n {nb.STATE[st]}'
            else:
                txt = " (not on a section)"
            if self._train_rows[t].cget("text") != txt: self._train_rows[t].config(text=txt)

    def draw_sensors(self):
        rows = getattr(self, "sensor_rows", [])
        sc = self.sensor_canvas
        sc.delete("all")
        if not rows:
            sc.create_text(0, 10, anchor="w", fill=DIM, text="none found (inject before loading the park)")
            sc.config(height=24); return
        rows = sorted(rows, key=lambda r: (r["track"], r["step"]))
        sc.config(height=min(22 * len(rows) + 4, 360))
        for i, r in enumerate(rows):
            y = 4 + i * 22
            on = r["active"]
            sc.create_oval(2, y + 3, 16, y + 17, fill="#39ff6a" if on else "#2a2f38", outline="#56d364" if on else "#555")
            sc.create_text(24, y + 10, anchor="w", fill=FG if on else "#c9ced8", text=r["name"][:24], font=("Segoe UI", 10))
            x = 230
            for t in r["trains"]:
                c = TRAIN_COLORS[t % len(TRAIN_COLORS)]
                sc.create_oval(x, y + 2, x + 30, y + 18, fill=c, outline="")
                sc.create_text(x + 15, y + 10, text=f"T{t + 1}", fill="#0b0d10", font=("Segoe UI", 8, "bold"))
                x -= 34

    def draw_switches(self, switches):
        lines = []
        for sw in switches:
            pos = "moving..." if sw["current"] < 0 else f'position {sw["current"]}'
            tgt = "" if sw["current"] == sw["target"] else f' → {sw["target"]}'
            lines.append(f'#{sw["index"]}: {pos}{tgt}  (of {sw["directions"]})')
        txt = "\n".join(lines) or "none"
        if getattr(self, "_sw_lbl", None) is None:
            self._sw_lbl = tk.Label(self.switch_frame, text="", bg=PANEL, fg=FG, justify="left", font=("Segoe UI", 10))
            self._sw_lbl.pack(anchor="w")
        if self._sw_lbl.cget("text") != txt: self._sw_lbl.config(text=txt)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    Viewer(root)
    root.mainloop()
