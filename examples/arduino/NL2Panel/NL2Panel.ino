// NL2Panel - example physical ride control panel for NoLimits 2 (NL2Bridge + serial_gateway.py).
//
// Board: Arduino Uno / Nano / Leonardo / Mega (uses 18 I/O pins - fits an Uno). ESP32 works too (change pins).
// Wire every button / key switch between its pin and GND (internal pull-ups, no resistors needed).
// Wire every lamp / LED from its pin through a resistor (or a relay / transistor for real 24 V lamps) to GND.
//
//   INPUTS                                         OUTPUTS
//   D2  DISPATCH           push button             D8  DISPATCH READY lamp
//   D3  GATES              push button (toggle)    D9  GATES CLOSED lamp (flashes while moving)
//   D4  RESTRAINTS         push button (toggle)    D10 RESTRAINTS LOCKED lamp (flashes if a single row is open)
//   D5  E-STOP             NC mushroom to GND      D11 E-STOP lamp (solid = pressed, flashing = needs RESET)
//   D6  RESET              push button             D12 LIFT RUNNING lamp
//   D7  LIFT START/STOP    push button (toggle)    D13 COMM lamp (on-board LED: on = game linked, blink = no game)
//   A0  ROW SELECT         10k pot (ends to 5V/GND, wiper to A0)
//   A1  ROW RELEASE        push button (opens / closes the selected row)
//   A2  MANUAL             key switch (on = Manual Block mode, off = Auto)
//   A3  TRANSFER           key switch (on = transfer table to position 1, off = position 0; needs MANUAL)
//   A4  STATION OCCUPIED lamp  (output)
//   A5  ADVANCE            push button (advances the waiting block; hold RESET too = advance backwards)
//   Hold RESET 3 seconds = simulator reset (trains back to their start positions).
//
// PC side: python serial_gateway.py COM5   (see README.md). The panel takes over dispatching when it connects;
// closing the gateway hands the ride back to the game.
#include "NL2Serial.h"

// ---------------- your ride: names exactly as in the NoLimits 2 editor ----------------
#define STATION          0            // station number (0 = first station of the coaster)
#define STATION_BLOCK    "Station"    // section shown on the STATION OCCUPIED lamp
#define LIFT_BLOCK       "Lift"       // lift section switched by LIFT START/STOP ("" = no lift button)
#define TRANSFER_TRACK   "Transfer"   // special track moved by the TRANSFER key ("" = no transfer)
#define DEFAULT_ROWS     8            // rows on the knob until the game reports the real count
#define LIFT_ON_AT_START false        // true = lift chain runs as soon as the panel connects

// ---------------- pins ----------------
const uint8_t PIN_DISPATCH = 2, PIN_GATES = 3, PIN_RESTRAINTS = 4, PIN_ESTOP = 5, PIN_RESET = 6, PIN_LIFT = 7;
const uint8_t PIN_ROW_KNOB = A0, PIN_ROW_RELEASE = A1, PIN_MANUAL = A2, PIN_TRANSFER = A3, PIN_ADVANCE = A5;
const uint8_t LAMP_READY = 8, LAMP_GATES = 9, LAMP_RESTRAINTS = 10, LAMP_ESTOP = 11, LAMP_LIFT = 12, LAMP_COMM = 13;
const uint8_t LAMP_STATION = A4;

NL2Serial nl2;

// ---------------- debounced input ----------------
struct Input {
  uint8_t pin; bool on = false, raw = false; unsigned long tRaw = 0, tOn = 0;
  void begin(uint8_t p) { pin = p; pinMode(p, INPUT_PULLUP); on = raw = digitalRead(p) == LOW; tOn = millis(); }
  bool changed() {                       // true once when the debounced state flips
    bool r = digitalRead(pin) == LOW;
    if (r != raw) { raw = r; tRaw = millis(); }
    if (on != raw && millis() - tRaw > 30) { on = raw; tOn = millis(); return true; }
    return false;
  }
  bool pressed()  { return changed() && on; }
  unsigned long heldMs() const { return on ? millis() - tOn : 0; }
};
Input btnDispatch, btnGates, btnRestraints, swEstop, btnReset, btnLift, btnRow, keyManual, keyTransfer, btnAdvance;

// ---------------- ride state from the game ----------------
unsigned long stFlags = 0;        // station flags (NL2_ST_...)
bool gameEstop = false, gameManual = false, stationOccupied = false, liftOn = LIFT_ON_AT_START, ready = false;
int rowCount = DEFAULT_ROWS;
unsigned long rowsOpen = 0;       // bit (row-1) set = that row is released
long waitFwd[8], waitBwd[8];      // blocks waiting for ADVANCE
uint8_t nFwd = 0, nBwd = 0;
unsigned long errorUntil = 0, lastModeTry = 0;
bool resetHoldDone = false;

void listSet(long* list, uint8_t& n, long id, bool on) {
  for (uint8_t i = 0; i < n; i++)
    if (list[i] == id) { if (!on) list[i] = list[--n]; return; }
  if (on && n < 8) list[n++] = id;
}

bool isName(const char* a, const char* b) { return *b && !strcasecmp(a, b); }

// ---------------- callbacks ----------------
void onReady(int api, const char* build, int coaster, const char* name) {
  ready = true; nFwd = nBwd = 0; rowsOpen = 0; stFlags = 0;
  nl2.watch(NL2_WATCH_DEFAULT | NL2_WATCH_ROWS);
  nl2.manualDispatch(STATION, true);                 // panel buttons dispatch, not the game's timer
  nl2.blockMode(keyManual.on ? "manual" : "auto");
  if (*LIFT_BLOCK) nl2.liftPower(LIFT_BLOCK, liftOn);
  if (!swEstop.on) nl2.estop(true);
  lastModeTry = millis();
}
void onLink(bool up) { if (!up) ready = false; }
void onReply(bool ok, const char* cmd, const char* text) { if (!ok) errorUntil = millis() + 1500; }
void onMode(const char* mode, bool estop) { gameEstop = estop; gameManual = strcmp(mode, "auto") != 0; }
void onStation(const NL2Station& s) { if (s.index == STATION) stFlags = s.flags; }
void onBlock(const NL2Block& b) {
  if (isName(b.name, STATION_BLOCK)) stationOccupied = b.occupied();
  listSet(waitFwd, nFwd, b.id, b.has(NL2_BLK_CAN_ADV_FWD));
  listSet(waitBwd, nBwd, b.id, b.has(NL2_BLK_CAN_ADV_BWD));
}
void onRows(int station, int count, int open) {
  if (station != STATION) return;
  if (count > 0) rowCount = count;
  if (open == 0) rowsOpen = 0;
}
void onRow(const NL2Row& r) {
  if (r.station != STATION || r.row < 1 || r.row > 32) return;
  if (r.open) rowsOpen |= 1UL << (r.row - 1); else rowsOpen &= ~(1UL << (r.row - 1));
}

// ---------------- setup / loop ----------------
void setup() {
  const uint8_t lamps[] = {LAMP_READY, LAMP_GATES, LAMP_RESTRAINTS, LAMP_ESTOP, LAMP_LIFT, LAMP_COMM, LAMP_STATION};
  for (uint8_t p : lamps) pinMode(p, OUTPUT);
  btnDispatch.begin(PIN_DISPATCH); btnGates.begin(PIN_GATES); btnRestraints.begin(PIN_RESTRAINTS);
  swEstop.begin(PIN_ESTOP); btnReset.begin(PIN_RESET); btnLift.begin(PIN_LIFT); btnRow.begin(PIN_ROW_RELEASE);
  keyManual.begin(PIN_MANUAL); keyTransfer.begin(PIN_TRANSFER); btnAdvance.begin(PIN_ADVANCE);

  nl2.onReady = onReady; nl2.onLink = onLink; nl2.onReply = onReply; nl2.onMode = onMode;
  nl2.onStation = onStation; nl2.onBlock = onBlock; nl2.onRows = onRows; nl2.onRow = onRow;
  Serial.begin(115200);
  nl2.begin(Serial);
}

int selectedRow() {
  int r = map(analogRead(PIN_ROW_KNOB), 0, 1024, 1, rowCount + 1);
  return constrain(r, 1, rowCount);
}

void buttons() {
  // E-STOP: a normally-closed circuit; opening it (or a broken wire) stops the ride
  if (swEstop.changed() && !swEstop.on) nl2.estop(true);

  if (btnDispatch.pressed()) {
    if (stFlags & NL2_ST_CAN_DISPATCH) nl2.dispatch(STATION); else errorUntil = millis() + 800;
  }
  if (btnGates.pressed()) {
    if (stFlags & NL2_ST_GATES_OPEN) nl2.closeGates(STATION); else nl2.openGates(STATION);
  }
  if (btnRestraints.pressed()) {
    if (stFlags & (NL2_ST_HARNESS_OPEN | NL2_ST_ROWS_OPEN)) nl2.closeRestraints(STATION);
    else nl2.openRestraints(STATION);
  }
  if (btnRow.pressed()) {
    int row = selectedRow();
    if (rowsOpen & (1UL << (row - 1))) nl2.closeRow(STATION, row); else nl2.openRow(STATION, row);
  }
  if (btnLift.pressed() && *LIFT_BLOCK) {
    liftOn = !liftOn;
    nl2.liftPower(LIFT_BLOCK, liftOn);
  }
  if (btnAdvance.pressed()) {
    bool back = btnReset.on;
    if (back ? nBwd : nFwd) nl2.advance(back ? waitBwd[0] : waitFwd[0], back); else errorUntil = millis() + 800;
  }
  if (keyManual.changed()) { nl2.blockMode(keyManual.on ? "manual" : "auto"); lastModeTry = millis(); }
  if (keyTransfer.changed() && *TRANSFER_TRACK) {
    if (keyManual.on) nl2.setSwitch(TRANSFER_TRACK, keyTransfer.on ? 1 : 0); else errorUntil = millis() + 800;
  }

  // RESET: tap = clear the E-stop (circuit must be closed again), hold 3 s = simulator reset
  if (btnReset.changed()) {
    if (!btnReset.on && !resetHoldDone && gameEstop && swEstop.on && !btnAdvance.on) nl2.estop(false);
    resetHoldDone = false;
  }
  if (btnReset.heldMs() > 3000 && !resetHoldDone) { resetHoldDone = true; nl2.resetCoaster(); }
}

void lamps() {
  unsigned long now = millis();
  bool flash = (now / 400) % 2, fast = (now / 100) % 2;
  if (!ready) {
    const uint8_t all[] = {LAMP_READY, LAMP_GATES, LAMP_RESTRAINTS, LAMP_ESTOP, LAMP_LIFT, LAMP_STATION};
    for (uint8_t p : all) digitalWrite(p, LOW);
    digitalWrite(LAMP_COMM, (now / 1000) % 2);
    return;
  }
  bool gatesMoving = (stFlags & NL2_ST_HAS_GATES) && !(stFlags & (NL2_ST_GATES_OPEN | NL2_ST_GATES_CLOSED));
  bool harnessMoving = !(stFlags & (NL2_ST_HARNESS_OPEN | NL2_ST_HARNESS_CLOSED)) && (stFlags & NL2_ST_HAS_TRAIN);
  digitalWrite(LAMP_READY, (stFlags & NL2_ST_CAN_DISPATCH) != 0);
  digitalWrite(LAMP_GATES, (stFlags & NL2_ST_GATES_CLOSED) || (gatesMoving && flash));
  bool restraintsBusy = (stFlags & NL2_ST_ROWS_OPEN) || harnessMoving;
  digitalWrite(LAMP_RESTRAINTS, restraintsBusy ? flash : (stFlags & NL2_ST_HARNESS_CLOSED) != 0);
  digitalWrite(LAMP_ESTOP, !swEstop.on || (gameEstop && flash));
  digitalWrite(LAMP_LIFT, liftOn);
  digitalWrite(LAMP_STATION, stationOccupied);
  digitalWrite(LAMP_COMM, now < errorUntil ? fast : HIGH);
}

void loop() {
  nl2.poll();
  if (ready) {
    buttons();
    // the game refuses block-mode changes while trains are moving - keep asking until it matches the key
    if (gameManual != keyManual.on && millis() - lastModeTry > 3000) {
      nl2.blockMode(keyManual.on ? "manual" : "auto");
      lastModeTry = millis();
    }
  } else {
    Input* all[] = {&btnDispatch, &btnGates, &btnRestraints, &swEstop, &btnReset, &btnLift, &btnRow, &keyManual,
                    &keyTransfer, &btnAdvance};
    for (Input* in : all) in->changed();      // keep the switch positions current while waiting for the game
  }
  lamps();
}
