// NL2Serial.h - Arduino library for NL2Bridge (NoLimits 2 ride control) through serial_gateway.py.
//
// The Arduino talks plain text over USB serial to serial_gateway.py on the PC, which runs NL2Bridge.
// Every NL2Bridge feature has a method here; status from the game arrives through callbacks.
//
//   NL2Serial nl2;
//   void onStation(const NL2Station& s) { digitalWrite(8, s.is(NL2_ST_CAN_DISPATCH)); }
//   void setup() { Serial.begin(115200); nl2.onStation = onStation; nl2.begin(Serial); }
//   void loop()  { nl2.poll(); if (buttonPressed) nl2.dispatch(0); }
//
// Blocks, stations and special tracks can be given by number (nl2.dispatch(0)) or by the name used in the
// NoLimits 2 editor (nl2.brakes("Brake Run", "closed")) - F("...") names work too and save RAM.
// Full protocol: README.md in the examples/arduino folder.
#pragma once
#include <Arduino.h>

#ifndef NL2_LINE_MAX
#define NL2_LINE_MAX 120            // longest line from the gateway; longer lines are cut (names get truncated)
#endif

// ---- station flags (NL2Station::flags) ----
#define NL2_ST_ESTOP              (1UL << 0)
#define NL2_ST_MANUAL             (1UL << 1)   // station is in manual dispatch
#define NL2_ST_CAN_DISPATCH       (1UL << 2)
#define NL2_ST_CAN_CLOSE_GATES    (1UL << 3)
#define NL2_ST_CAN_OPEN_GATES     (1UL << 4)
#define NL2_ST_CAN_CLOSE_HARNESS  (1UL << 5)
#define NL2_ST_CAN_OPEN_HARNESS   (1UL << 6)
#define NL2_ST_CAN_RAISE_PLATFORM (1UL << 7)
#define NL2_ST_CAN_LOWER_PLATFORM (1UL << 8)
#define NL2_ST_CAN_LOCK_FLYER     (1UL << 9)
#define NL2_ST_CAN_UNLOCK_FLYER   (1UL << 10)
#define NL2_ST_TRAIN_READY        (1UL << 11)
#define NL2_ST_GATES_OPEN         (1UL << 12)
#define NL2_ST_GATES_CLOSED       (1UL << 13)
#define NL2_ST_HARNESS_OPEN       (1UL << 14)
#define NL2_ST_HARNESS_CLOSED     (1UL << 15)
#define NL2_ST_PLATFORM_RAISED    (1UL << 16)
#define NL2_ST_PLATFORM_LOWERED   (1UL << 17)
#define NL2_ST_FLYER_UNLOCKED     (1UL << 18)
#define NL2_ST_FLYER_LOCKED       (1UL << 19)
#define NL2_ST_HAS_GATES          (1UL << 20)
#define NL2_ST_HAS_PLATFORM       (1UL << 21)
#define NL2_ST_HAS_FLYER          (1UL << 22)
#define NL2_ST_HAS_TRAIN          (1UL << 23)
#define NL2_ST_ROWS_OPEN          (1UL << 24)  // at least one single row is released
#define NL2_ST_CUSTOM_TRAIN       (1UL << 25)  // train in station is drawn by a park script (no seats / rows)

// ---- block flags (NL2Block::flags) ----
#define NL2_BLK_CAN_ADV_FWD   0x01
#define NL2_BLK_CAN_ADV_BWD   0x02
#define NL2_BLK_IS_BLOCK      0x04
#define NL2_BLK_SCRIPTED      0x08
#define NL2_BLK_HAS_BRAKE     0x10
#define NL2_BLK_HAS_LIFT      0x20
#define NL2_BLK_HAS_TRANSPORT 0x40

// ---- block states (NL2Block::state) ----
enum NL2BlockState : uint8_t {
  NL2_NO_BLOCK, NL2_OFFLINE, NL2_IDLE, NL2_RESERVED, NL2_APPROACHING_FWD, NL2_APPROACHING_BWD, NL2_LEAVING_FWD,
  NL2_LEAVING_FWD_SELF, NL2_LEAVING_BWD, NL2_LEAVING_BWD_SELF, NL2_FULL_MANUAL, NL2_PASSING_FWD, NL2_PASSING_BWD,
  NL2_STATION, NL2_WAITING_CLEAR_BLOCK, NL2_WAITING_ADVANCE, NL2_COMPLETE_STOPPING, NL2_WAIT_TIME, NL2_PASS_THROUGH,
  NL2_SCRIPTED_STATE
};

// ---- lamp values (NL2Block::lamp) ----
#define NL2_LAMP_OFF   0
#define NL2_LAMP_ON    1
#define NL2_LAMP_FLASH 2

// ---- switch flags (NL2Switch::flags) ----
#define NL2_SW_MOVING     0x01
#define NL2_SW_SWITCHABLE 0x02
#define NL2_SW_MANUAL     0x04   // manual switching allowed on this track
#define NL2_SW_TRANSFER   0x08   // it is a transfer table

// ---- section detail flags (NL2Detail::flags) ----
#define NL2_DET_BEFORE_CENTER      0x0001
#define NL2_DET_BEHIND_CENTER      0x0002
#define NL2_DET_BEFORE_BRAKE_TRIG  0x0004
#define NL2_DET_BEHIND_BRAKE_TRIG  0x0008
#define NL2_DET_BEFORE_LIFT_TRIG   0x0010
#define NL2_DET_BEHIND_LIFT_TRIG   0x0020
#define NL2_DET_BRAKES_ON          0x0040
#define NL2_DET_WAIT_CLEAR_BLOCK   0x0080
#define NL2_DET_WAIT_ADVANCE       0x0100
#define NL2_DET_IS_STATION         0x0200
#define NL2_DET_ADV_FWD_VISIBLE    0x0400
#define NL2_DET_CAN_ADV_FWD        0x0800
#define NL2_DET_ADV_BWD_VISIBLE    0x1000
#define NL2_DET_CAN_ADV_BWD        0x2000
#define NL2_DET_SCRIPTED           0x4000
#define NL2_DET_OCCUPIED           0x8000

// ---- what the gateway streams (NL2Serial::watch) ----
#define NL2_WATCH_MODE     0x001   // MODE: block mode + E-stop
#define NL2_WATCH_BLOCKS   0x002   // BLK : every section's state / occupancy / lamp
#define NL2_WATCH_STATIONS 0x004   // STN : station flags
#define NL2_WATCH_SWITCHES 0x008   // SW  : switch / transfer table positions
#define NL2_WATCH_SENSORS  0x010   // SEN : track sensors (triggers)
#define NL2_WATCH_TRAINS   0x020   // TRN : where every train is + speed
#define NL2_WATCH_ROWS     0x040   // ROWS/ROW : single-row restraints of the train in each station
#define NL2_WATCH_EVENTS   0x080   // EVT : sensor enter/leave, mode changes, in-game advance presses
#define NL2_WATCH_DETAIL   0x100   // DET : per-section position flags + live lift / transport speed
#define NL2_WATCH_DEFAULT  (NL2_WATCH_MODE | NL2_WATCH_BLOCKS | NL2_WATCH_STATIONS | NL2_WATCH_SWITCHES | NL2_WATCH_EVENTS)
#define NL2_WATCH_ALL      0x1FF

// A block / station / track reference: a number or a name.
struct NL2Ref {
  const char* s = nullptr;
  const __FlashStringHelper* f = nullptr;
  long n = 0;
  NL2Ref(int v) : n(v) {}
  NL2Ref(long v) : n(v) {}
  NL2Ref(const char* v) : s(v) {}
  NL2Ref(const __FlashStringHelper* v) : f(v) {}
};

struct NL2Block {
  long id; uint8_t state, trains, lamp, flags, brakeMode, liftMode, transportMode; unsigned long trainMask;
  const char* name;
  bool occupied() const { return trains > 0; }
  bool has(uint8_t flag) const { return flags & flag; }
};
struct NL2Station {
  int index; unsigned long flags; uint8_t state, rowsOpen; const char* name;
  bool is(unsigned long flag) const { return flags & flag; }
};
struct NL2Switch {
  int index; int8_t current, target; uint8_t directions, flags; const char* name;   // current -1 = moving
  bool has(uint8_t flag) const { return flags & flag; }
};
struct NL2Sensor { long key; bool active; int lastTrain; unsigned long passes; const char* name; };
struct NL2Train  { int index; long blockId; int station; float speed, harness; const char* blockName; };
struct NL2Row    { int station, row; bool open; uint8_t position; int seats; };  // position 0..100 % open
struct NL2Event  { const char* type; long id; int train; const char* name; };
struct NL2Detail {
  long id; uint16_t flags; int trainIndex; uint8_t state; long userState; float liftSpeed, transportSpeed;
  bool has(uint16_t flag) const { return flags & flag; }
};
struct NL2Param   { long blockId; const char* device; float speed, accel, decel, current; const char* name; };
struct NL2Device  { long blockId; int brake, lift, transport; const char* name; };      // -1 = not present
struct NL2Coaster { int index, blockMode; bool estop; int trains; const char* name; };

class NL2Serial {
public:
  // ---- callbacks: assign the ones you need ----
  void (*onReady)(int api, const char* build, int coaster, const char* coasterName) = nullptr;  // configure here
  void (*onLink)(bool connected) = nullptr;                   // gateway lost / found the game
  void (*onReply)(bool ok, const char* cmd, const char* text) = nullptr;   // OK / ERR for every command
  void (*onMode)(const char* mode, bool estop) = nullptr;     // "auto" | "manual" | "fullmanual"
  void (*onBlock)(const NL2Block&) = nullptr;
  void (*onStation)(const NL2Station&) = nullptr;
  void (*onSwitch)(const NL2Switch&) = nullptr;
  void (*onSensor)(const NL2Sensor&) = nullptr;
  void (*onTrain)(const NL2Train&) = nullptr;
  void (*onRows)(int station, int count, int openCount) = nullptr;
  // seat capacity of the train in a station (train -1 / seats 0 = empty). customTrain = drawn by a park script
  // instead of an NL2 car model, so it has no seats or per-row restraints
  void (*onSeats)(int station, int train, int seats, int seatedCars, int seatsPerCar, bool customTrain) = nullptr;
  void (*onRow)(const NL2Row&) = nullptr;
  void (*onEvent)(const NL2Event&) = nullptr;
  void (*onDetail)(const NL2Detail&) = nullptr;
  void (*onParam)(const NL2Param&) = nullptr;
  void (*onDevice)(const NL2Device&) = nullptr;
  void (*onCoaster)(const NL2Coaster&) = nullptr;
  void (*onGet)(int ok, long intValue, float value) = nullptr;
  void (*onInfo)(int api, const char* build, const char* name) = nullptr;
  void (*onOther)(const char* type, const char* rest) = nullptr;  // anything else

  bool linked = false;   // gateway is connected to the game

  void begin(Stream& s) { _s = &s; _n = 0; _heard = false; hello(); }
  void poll() {
    while (_s && _s->available()) {
      char ch = _s->read();
      if (ch == '\n') { _b[_n] = 0; _heard = true; handle(_b); _n = 0; }
      else if (ch != '\r' && _n < NL2_LINE_MAX) _b[_n++] = ch;
    }
    if (!_heard && millis() - _helloAt > 2000) hello();   // gateway not started yet (or missed the first HELLO)
  }

  // ---- link / lists (answers come back through the callbacks) ----
  void hello()                 { _helloAt = millis(); cmd(F("HELLO")); end(); }
  void ping()                  { cmd(F("PING")); end(); }
  void info()                  { cmd(F("INFO")); end(); }
  void coasters()              { cmd(F("COASTERS")); end(); }
  void coaster(NL2Ref c)       { cmd(F("COASTER")); ref(c); end(); }
  void status()                { cmd(F("STATUS")); end(); }      // one MODE line
  void blocks()                { cmd(F("BLOCKS")); end(); }      // one BLK line per section
  void stations()              { cmd(F("STATIONS")); end(); }
  void switches()              { cmd(F("SWITCHES")); end(); }
  void sensors()               { cmd(F("SENSORS")); end(); }
  void trains()                { cmd(F("TRAINS")); end(); }
  void detail()                { cmd(F("DETAIL")); end(); }
  void rows(NL2Ref station)    { cmd(F("ROWS")); ref(station); end(); }
  void devices(NL2Ref block)   { cmd(F("DEVGET")); ref(block); end(); }
  void params(NL2Ref block)    { cmd(F("PARAMS")); ref(block); end(); }
  void watch(uint16_t mask) {
    static const char* const items[] = {"mode", "blocks", "stations", "switches", "sensors", "trains", "rows",
                                        "events", "detail"};
    cmd(F("WATCH"));
    if (!mask) word("none");
    for (uint8_t i = 0; i < 9; i++) if (mask & (1 << i)) word(items[i]);
    end();
  }
  void rate(int ms)            { cmd(F("RATE")); num(ms); end(); }

  // ---- whole coaster ----
  void blockMode(const char* mode) { cmd(F("MODE")); word(mode); end(); }   // "auto" | "manual" | "fullmanual"
  void estop(bool on)          { cmd(F("ESTOP")); num(on); end(); }
  void resetCoaster()          { cmd(F("RESET")); end(); }                 // simulator reset: trains to start
  void lash(int train, bool on) { cmd(F("LASH")); num(train); num(on); end(); }

  // ---- blocks and their devices ----
  void advance(NL2Ref block, bool backwards = false) {
    cmd(F("ADV")); ref(block); word(backwards ? "bwd" : "fwd"); end();
  }
  void brakes(NL2Ref block, const char* mode)    { cmd(F("BRAKE")); ref(block); word(mode); end(); }     // open|closed|trim
  void lift(NL2Ref block, const char* mode)      { cmd(F("LIFT")); ref(block); word(mode); end(); }      // fwd|bwd|idle|off
  void transport(NL2Ref block, const char* mode) { cmd(F("TRANSPORT")); ref(block); word(mode); end(); } // off|fwd|bwd|fwdbrake|bwdbrake|launchfwd|launchbwd
  // Full Manual block mode: like the game's full-manual panel. brake 0 open/1 closed, lift 0/1, transport 0 off/1 fwd/2 bwd
  void device(NL2Ref block, const char* dev, int value) { cmd(F("DEVICE")); ref(block); word(dev); num(value); end(); }
  // Live speed in any block mode. dev "lift" | "transport"; m/s and m/s^2, negative = unchanged
  void speed(NL2Ref block, const char* dev, float mps, float accel = -1, float decel = -1) {
    cmd(F("SPEED")); ref(block); word(dev); flt(mps); flt(accel); flt(decel); end();
  }
  void liftPower(NL2Ref block, bool run) { cmd(F("LIFTPOWER")); ref(block); num(run); end(); }  // 0 = stop chain, 1 = design speed

  // ---- stations ----
  void stationOp(NL2Ref st, const char* op) { cmd(F("ST")); ref(st); word(op); end(); }
  void manualDispatch(NL2Ref st, bool on)   { stationOp(st, on ? "manual" : "auto"); }
  void dispatch(NL2Ref st)         { stationOp(st, "dispatch"); }
  void openGates(NL2Ref st)        { stationOp(st, "gates_open"); }
  void closeGates(NL2Ref st)       { stationOp(st, "gates_close"); }
  void openRestraints(NL2Ref st)   { stationOp(st, "harness_open"); }
  void closeRestraints(NL2Ref st)  { stationOp(st, "harness_close"); }   // also closes single released rows
  void raiseFloor(NL2Ref st)       { stationOp(st, "platform_raise"); }
  void dropFloor(NL2Ref st)        { stationOp(st, "platform_lower"); }
  void unlockFlyer(NL2Ref st)      { stationOp(st, "flyer_unlock"); }
  void lockFlyer(NL2Ref st)        { stationOp(st, "flyer_lock"); }
  // Single-row restraints: row 1 = front row, 0 = every row. Dispatch is refused while a row is open.
  void rowRestraint(NL2Ref st, int row, bool open) { cmd(F("ROW")); ref(st); num(row); num(open); end(); }
  void openRow(NL2Ref st, int row)  { rowRestraint(st, row, true); }
  void closeRow(NL2Ref st, int row) { rowRestraint(st, row, false); }

  // ---- switches / transfer tables (Manual or Full Manual block mode) ----
  void setSwitch(NL2Ref track, int direction) { cmd(F("SWITCH")); ref(track); num(direction); end(); }

  // ---- raw section commands / queries (see docs/PROTOCOL.md) ----
  void sectionCmd(NL2Ref block, int c, int param = 0) { cmd(F("SECTION")); ref(block); num(c); num(param); end(); }
  void sectionGet(NL2Ref block, int query, float x = 0) { cmd(F("SECTIONGET")); ref(block); num(query); flt(x); end(); }

  // ---- scripted-mode coasters ----
  void scriptedBlock(NL2Ref block, const char* c, bool on) { cmd(F("SBLOCK")); ref(block); word(c); num(on); end(); }
  void registerState(NL2Ref block, int state, const char* lamp, const char* text) {
    cmd(F("REGSTATE")); ref(block); num(state); word(lamp); _s->print(' '); _s->print(text); end();
  }
  void setState(NL2Ref block, int state) { cmd(F("SETSTATE")); ref(block); num(state); end(); }
  void stationEntering(NL2Ref block)     { cmd(F("SENTER")); ref(block); end(); }
  void stationLeaving(NL2Ref block)      { cmd(F("SLEAVE")); ref(block); end(); }
  void stationNextClear(NL2Ref block)    { cmd(F("SCLEAR")); ref(block); end(); }
  void stationNextOccupied(NL2Ref block) { cmd(F("SOCC")); ref(block); end(); }

  // ---- anything else: a full command line without the newline ----
  void raw(const char* line) { if (_s) { _s->print(line); end(); } }

private:
  Stream* _s = nullptr;
  char _b[NL2_LINE_MAX + 1];
  uint8_t _n = 0;
  bool _heard = false;
  unsigned long _helloAt = 0;

  void cmd(const __FlashStringHelper* c) { if (_s) _s->print(c); }
  void end() { if (_s) _s->print('\n'); }
  void word(const char* w) { if (_s) { _s->print(' '); _s->print(w); } }
  void num(long v) { if (_s) { _s->print(' '); _s->print(v); } }
  void flt(float v) { if (_s) { _s->print(' '); _s->print(v, 3); } }
  void ref(const NL2Ref& r) {
    if (!_s) return;
    _s->print(' ');
    if (r.s) { _s->print('"'); _s->print(r.s); _s->print('"'); }
    else if (r.f) { _s->print('"'); _s->print(r.f); _s->print('"'); }
    else _s->print(r.n);
  }

  static char* tok(char*& p) {
    while (*p == ' ') p++;
    if (!*p) return (char*)"";
    char* t = p;
    while (*p && *p != ' ') p++;
    if (*p) *p++ = 0;
    return t;
  }
  static long ni(char*& p) { return strtol(tok(p), nullptr, 10); }
  static unsigned long nu(char*& p, int base = 10) { return strtoul(tok(p), nullptr, base); }
  static float nf(char*& p) { return atof(tok(p)); }
  static const char* rest(char*& p) { while (*p == ' ') p++; return p; }

  void handle(char* p) {
    const char* t = tok(p);
    if (!strcmp(t, "BLK")) {
      if (!onBlock) return;
      NL2Block b; b.id = ni(p); b.state = ni(p); b.trains = ni(p); b.lamp = ni(p); b.flags = ni(p);
      b.brakeMode = ni(p); b.liftMode = ni(p); b.transportMode = ni(p); b.trainMask = nu(p); b.name = rest(p);
      onBlock(b);
    } else if (!strcmp(t, "STN")) {
      if (!onStation) return;
      NL2Station s; s.index = ni(p); s.flags = nu(p, 16); s.state = ni(p); s.rowsOpen = ni(p); s.name = rest(p);
      onStation(s);
    } else if (!strcmp(t, "MODE")) {
      const char* m = tok(p); bool e = ni(p);
      if (onMode) onMode(m, e);
    } else if (!strcmp(t, "OK") || !strcmp(t, "ERR")) {
      bool ok = t[0] == 'O'; const char* c = tok(p);
      if (onReply) onReply(ok, c, rest(p));
    } else if (!strcmp(t, "SW")) {
      if (!onSwitch) return;
      NL2Switch w; w.index = ni(p); w.current = ni(p); w.target = ni(p); w.directions = ni(p); w.flags = ni(p);
      w.name = rest(p); onSwitch(w);
    } else if (!strcmp(t, "ROW")) {
      if (!onRow) return;
      NL2Row r; r.station = ni(p); r.row = ni(p); r.open = ni(p); r.position = ni(p); r.seats = ni(p); onRow(r);
    } else if (!strcmp(t, "ROWS")) {
      int st = ni(p), n = ni(p), o = ni(p);
      if (onRows) onRows(st, n, o);
    } else if (!strcmp(t, "SEATS")) {
      int st = ni(p), tr = ni(p), n = ni(p), cars = ni(p), per = ni(p); bool custom = ni(p);
      if (onSeats) onSeats(st, tr, n, cars, per, custom);
    } else if (!strcmp(t, "TRN")) {
      if (!onTrain) return;
      NL2Train r; r.index = ni(p); r.blockId = ni(p); r.station = ni(p); r.speed = nf(p); r.harness = nf(p);
      r.blockName = rest(p); onTrain(r);
    } else if (!strcmp(t, "SEN")) {
      if (!onSensor) return;
      NL2Sensor s; s.key = ni(p); s.active = ni(p); s.lastTrain = ni(p); s.passes = nu(p); s.name = rest(p);
      onSensor(s);
    } else if (!strcmp(t, "EVT")) {
      if (!onEvent) return;
      NL2Event e; e.type = tok(p); e.id = ni(p); e.train = ni(p); e.name = rest(p); onEvent(e);
    } else if (!strcmp(t, "DET")) {
      if (!onDetail) return;
      NL2Detail d; d.id = ni(p); d.flags = nu(p, 16); d.trainIndex = ni(p); d.state = ni(p); d.userState = ni(p);
      d.liftSpeed = nf(p); d.transportSpeed = nf(p); onDetail(d);
    } else if (!strcmp(t, "WHO")) {           // gateway (re)started: introduce ourselves again
      hello();
    } else if (!strcmp(t, "LINK")) {
      linked = ni(p);
      if (onLink) onLink(linked);
    } else if (!strcmp(t, "READY")) {
      linked = true;
      int api = ni(p); const char* build = tok(p); int c = ni(p);
      if (onReady) onReady(api, build, c, rest(p));
    } else if (!strcmp(t, "PARAM")) {
      if (!onParam) return;
      NL2Param q; q.blockId = ni(p); q.device = tok(p); q.speed = nf(p); q.accel = nf(p); q.decel = nf(p);
      q.current = nf(p); q.name = rest(p); onParam(q);
    } else if (!strcmp(t, "DEV")) {
      if (!onDevice) return;
      NL2Device d; d.blockId = ni(p); d.brake = ni(p); d.lift = ni(p); d.transport = ni(p); d.name = rest(p);
      onDevice(d);
    } else if (!strcmp(t, "COASTER")) {
      if (!onCoaster) return;
      NL2Coaster c; c.index = ni(p); c.blockMode = ni(p); c.estop = ni(p); c.trains = ni(p); c.name = rest(p);
      onCoaster(c);
    } else if (!strcmp(t, "GET")) {
      int ok = ni(p); long iv = ni(p); float v = nf(p);
      if (onGet) onGet(ok, iv, v);
    } else if (!strcmp(t, "INFO")) {
      int api = ni(p); const char* build = tok(p);
      if (onInfo) onInfo(api, build, rest(p));
    } else if (onOther) {
      onOther(t, rest(p));
    }
  }
};
