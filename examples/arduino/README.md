# Arduino ride control panel

This example connects a physical control panel to NoLimits 2. It uses real buttons, key switches, an E-stop and lamps on an Arduino.

```
[buttons / lamps] ── Arduino ──USB serial── serial_gateway.py ──TCP── NL2Bridge.dll in NoLimits 2
```

A plain Arduino (Uno, Nano, Leonardo or Mega) has no network connection. It sends short text commands over USB to `serial_gateway.py` on the PC. The gateway runs them through NL2Bridge and sends ride status back, but only the parts that changed.

All NL2Bridge features are available from the Arduino:
- block states and occupancy
- trains
- stations
- single-row restraints
- switches and transfer tables
- brakes, lifts and transport
- live lift speed
- sensors and events
- E-stop and reset
- scripted-mode commands

| File | What it is |
|---|---|
| `serial_gateway.py` | PC side. Needs Python 3.8+ and `pip install pyserial`. |
| `NL2Panel/NL2Serial.h` | Arduino library. It has one method per command and callbacks for status. Copy it into your own sketch folder. |
| `NL2Panel/NL2Panel.ino` | A complete example panel that fits an Arduino Uno. |

## Quick start

1. Open `NL2Panel/NL2Panel.ino` in the Arduino IDE.
2. Set the config at the top to match your ride's sections in the NoLimits 2 editor: station, lift, and the transfer table. For the transfer, give the special track number (run `SWITCHES` in console mode to list them) and the table positions for the main line and the storage track.
3. Upload the sketch.
4. Start NoLimits 2, open the park and inject NL2Bridge (`NL2BridgeInjector.exe`).
5. Run the gateway with the Arduino's COM port. You can find it in the Arduino IDE under Tools → Port.
   ```
   pip install pyserial
   python serial_gateway.py COM5
   python serial_gateway.py COM5 --host 192.168.1.20 --coaster "Fury 325"   # game on another PC, pick a coaster
   python serial_gateway.py COM5 --log                                      # show every line both ways
   ```
6. The COMM lamp (the on-board LED) turns solid when the panel is linked to the game. From then on, the station is in manual dispatch and the panel's DISPATCH button runs it.

Stop the gateway with Ctrl+C to hand the ride back. It restores any lift or transport speeds it changed, puts stations back into automatic dispatch, releases an E-stop it set, and returns the coaster to automatic block mode if the panel switched it to manual. The game only accepts that switch once every train has stopped on a block, so the gateway may wait up to 2 minutes, for example while a train finishes a lift. Use `--no-restore` to skip all of this.

The Arduino IDE's Serial Monitor and the gateway can't use the COM port at the same time. Close the monitor before you run the gateway.

To test commands without hardware, run `python serial_gateway.py --console`. Type the commands below (for example `BLOCKS`, `ST 0 dispatch`, `WATCH blocks stations`) and the replies are printed.

## The example panel (`NL2Panel.ino`)

Connect each button and key switch between its pin and GND; the sketch uses the internal pull-ups. Connect each lamp or LED through a resistor to GND. Use a relay board or transistor for 12/24 V pilot lamps.

| Pin | Input | Pin | Output lamp |
|---|---|---|---|
| D2 | DISPATCH | D8 | DISPATCH READY |
| D3 | GATES (toggle) | D9 | GATES CLOSED (flashes while moving) |
| D4 | RESTRAINTS (toggle) | D10 | RESTRAINTS LOCKED (flashes if a single row is released) |
| D5 | E-STOP: normally closed contact to GND | D11 | E-STOP (solid = pressed, flashing = needs RESET) |
| D6 | RESET: tap to clear the E-stop, hold 3 s for a simulator reset | D12 | LIFT RUNNING |
| D7 | LIFT START/STOP (toggle) | D13 | COMM (on = linked, slow blink = no game, fast blink = command refused) |
| A0 | ROW SELECT: 10 kΩ pot, wiper to A0 | A4 | STATION OCCUPIED |
| A1 | ROW RELEASE: opens or closes the selected row | | |
| A2 | MANUAL key switch (Manual Block / Auto) | | |
| A3 | TRANSFER key switch (table to the storage / main position, needs MANUAL) | | |
| A5 | ADVANCE: advances the block waiting for it; hold RESET too to advance backwards | | |

The E-stop contact is normally closed. Opening the circuit, whether by pressing the mushroom or through a broken wire, stops the ride. After releasing the mushroom, RESET clears the stop.

The lift chain stays stopped (speed 0) until LIFT START is pressed, like a real ride. Set `LIFT_ON_AT_START true` to start it running.

## Using the library in your own sketch

```cpp
#include "NL2Serial.h"
NL2Serial nl2;

void onStation(const NL2Station& s) { digitalWrite(8, s.is(NL2_ST_CAN_DISPATCH)); }
void onBlock(const NL2Block& b)     { if (!strcmp(b.name, "Brake Run 1")) digitalWrite(9, b.occupied()); }
void onReady(int api, const char* build, int coaster, const char* name) {
  nl2.watch(NL2_WATCH_DEFAULT | NL2_WATCH_ROWS);   // what to stream
  nl2.manualDispatch(0, true);                      // the panel dispatches
}

void setup() {
  pinMode(2, INPUT_PULLUP); pinMode(8, OUTPUT); pinMode(9, OUTPUT);
  nl2.onReady = onReady; nl2.onStation = onStation; nl2.onBlock = onBlock;
  Serial.begin(115200);
  nl2.begin(Serial);
}
void loop() {
  nl2.poll();                                       // call often: reads status and runs the callbacks
  if (digitalRead(2) == LOW) { nl2.dispatch(0); delay(300); }
}
```

You can give blocks, stations and tracks by number or by name: `nl2.brakes("Brake Run 1", "closed")`, `nl2.openRow(0, 3)`, `nl2.setSwitch(F("Transfer"), 1)`. Use `F("...")` to keep names in flash on small boards.

`onReady` runs every time the gateway links to the game, including after the game restarts. Put your setup commands there.

`onReply(ok, cmd, text)` reports every command's result. For example, `ERR ST No train stopped in the station`.

| Method | Does |
|---|---|
| `blockMode("auto" / "manual" / "fullmanual")`, `estop(bool)`, `resetCoaster()` | Whole coaster |
| `dispatch`, `openGates`, `closeGates`, `openRestraints`, `closeRestraints`, `raiseFloor`, `dropFloor`, `unlockFlyer`, `lockFlyer`, `manualDispatch(st, bool)`, `stationOp(st, op)` | Station |
| `openRow(st, row)`, `closeRow(st, row)`, `rowRestraint(st, row, open)` | Single-row restraints: row 1 is the front, 0 is all rows |
| `advance(block, backwards)` | Manual Block advance |
| `brakes(block, "open"/"closed"/"trim")`, `lift(block, "fwd"/"bwd"/"idle"/"off")`, `transport(block, "off"/"fwd"/"bwd"/"fwdbrake"/"bwdbrake"/"launchfwd"/"launchbwd")` | Section devices |
| `device(block, "brake"/"lift"/"transport", value)` | Full Manual device control, like the game's full-manual panel |
| `speed(block, "lift"/"transport", mps, accel, decel)`, `liftPower(block, bool)` | Live speed in any block mode. `liftPower` stops the chain or runs it at design speed. |
| `setSwitch(track, direction)` | Switch or transfer table |
| `lash(train, bool)` | "Lashed To Track" flag for storage tracks |
| `blocks()`, `stations()`, `switches()`, `sensors()`, `trains()`, `detail()`, `rows(st)`, `devices(block)`, `params(block)`, `coasters()`, `coaster(key)`, `status()`, `info()`, `ping()` | Queries; answers arrive through the callbacks |
| `watch(mask)`, `rate(ms)` | What the gateway streams and how often it checks (default 100 ms) |
| `sectionCmd`, `sectionGet`, `scriptedBlock`, `registerState`, `setState`, `stationEntering/Leaving/NextClear/NextOccupied` | Raw section commands and scripted-mode coasters |
| `raw("LINE")` | Send any protocol line |

Flag constants for the callbacks:
- `NL2_ST_*`: station flags
- `NL2_BLK_*`: block flags
- `NL2_SW_*`: switch flags
- `NL2_DET_*`: section detail flags
- `NL2_WATCH_*`: what the gateway streams
- `NL2BlockState`: block state values

All of them are listed in `NL2Serial.h`.

## Serial protocol

The link runs at 115200 baud. Every message is one line of text ending in `\n`, with parts separated by spaces. In commands, write a name with spaces in "double quotes"; a bare number is an id or index. In status lines, the name is always the last part and may contain spaces.

Any device that can write a serial line can use this protocol, not only an Arduino. It works from ESP32, Teensy, Pico or a PLC serial card.

**Handshake**
1. The Arduino sends `HELLO` at startup. It repeats it every 2 s until the gateway answers. The gateway sends `WHO` after opening the port, and the board answers with `HELLO`.
2. The gateway replies `READY <api> <build> <coasterIndex> <coasterName>` once it is linked to the game. If it isn't linked yet, it replies `LINK 0`.
3. `LINK 0` / `LINK 1` report when the game link drops or comes back. `READY` follows every `LINK 1`.
4. After `HELLO`, the gateway streams `mode blocks stations switches events` until the board sends `WATCH`.

**Commands (Arduino → gateway).** Every command is answered with `OK <CMD> [info]` or `ERR <CMD> <reason>`.

| Command | Arguments |
|---|---|
| `PING`, `INFO`, `STATUS` | – |
| `COASTERS`, `COASTER` | `<index or name>` for `COASTER` |
| `BLOCKS`, `STATIONS`, `SWITCHES`, `SENSORS`, `TRAINS`, `DETAIL` | – (send the full list once) |
| `ROWS` | `<station>` |
| `DEVGET` | `<block>` |
| `PARAMS` | `<block>` |
| `WATCH` | `mode blocks stations switches sensors trains rows events detail` \| `all` \| `none` |
| `RATE` | `<ms>` (poll interval, minimum 20) |
| `MODE` | `auto` \| `manual` \| `fullmanual` |
| `ESTOP` | `1` \| `0` |
| `RESET` | – (simulator reset) |
| `LASH` | `<train> 1\|0` |
| `ADV` | `<block> [fwd\|bwd]` |
| `BRAKE` | `<block> open\|closed\|trim` |
| `LIFT` | `<block> fwd\|bwd\|idle\|off` |
| `TRANSPORT` | `<block> off\|fwd\|bwd\|fwdbrake\|bwdbrake\|launchfwd\|launchbwd` |
| `DEVICE` | `<block> brake\|lift\|transport <value>` (Full Manual) |
| `SPEED` | `<block> lift\|transport <m/s> [accel] [decel]` |
| `LIFTPOWER` | `<block> 1\|0` |
| `ST` | `<station> manual\|auto\|dispatch\|gates_open\|gates_close\|harness_open\|harness_close\|platform_raise\|platform_lower\|flyer_unlock\|flyer_lock` |
| `ROW` | `<station> <row> 1\|0` (row 1 = front, 0 = all) |
| `SWITCH` | `<track> <direction>` |
| `SECTION` | `<block> <cmd> [param]` |
| `SECTIONGET` | `<block> <query> [x]` |
| `SBLOCK` | `<block> <cmd> 1\|0` |
| `REGSTATE` | `<block> <state> off\|on\|flash <text...>` |
| `SETSTATE` | `<block> <state>` |
| `SENTER`, `SLEAVE`, `SCLEAR`, `SOCC` | `<block>` |

**Status lines (gateway → Arduino).** A watched item is sent only when it changes. List queries send every item.

| Line | Fields |
|---|---|
| `MODE` | `<auto\|manual\|fullmanual> <estop 0/1>` |
| `BLK` | `<id> <state> <trains> <lamp> <flags> <brakeMode> <liftMode> <transportMode> <trainMask> <name>` |
| `STN` | `<index> <flags hex> <state> <rowsOpen> <name>` |
| `SW` | `<index> <current (-1 moving)> <target> <directions> <flags> <name>` |
| `SEN` | `<key> <active> <lastTrain (-1 none)> <passes> <name>` |
| `TRN` | `<index> <blockId> <station (-1)> <speed m/s> <harness 0..1> <blockName>` |
| `ROWS` | `<station> <rowCount> <openCount>` (count 0 = no train) |
| `ROW` | `<station> <row> <open 0/1> <position %> <seats>` |
| `SEATS` | `<station> <train (-1 none)> <seats> <seatedCars> <seatsPerCar> <customTrain 0/1>`: capacity of the train in the station, sent with the `stations` watch when the train changes (API 8). Seats come from the car model, so a train drawn by a park script reads 0 seats and `customTrain` 1 (also station flag `NL2_ST_CUSTOM_TRAIN`). Callback: `onSeats` |
| `EVT` | `<type> <sensorKey or sectionId> <train> <name>`, where type is `sensorEnter`, `sensorLeave`, `modeAuto`, `modeManualBlock`, `modeFullManual`, `advanceFwdPressed` or `advanceBwdPressed` |
| `DET` | `<id> <flags hex> <trainIndex> <state> <userState> <liftSpeed> <transportSpeed>` |
| `PARAM` | `<blockId> <lift\|transport> <speed> <accel> <decel> <current> <name>` |
| `DEV` | `<blockId> <brake> <lift> <transport> <name>` (-1 = not present) |
| `COASTER` | `<index> <blockMode 1/2/3> <estop> <trains> <name>` |
| `GET` | `<ok> <int> <double>` |
| `INFO` | `<api> <build> <name>` |

The flag bits are the same as in the binary protocol ([docs/PROTOCOL.md](../../docs/PROTOCOL.md)); `NL2Serial.h` has them as constants. Block mode and station rules are the game's own. For example, switches need Manual Block mode, `DEVICE` needs Full Manual, and dispatch is refused while a single row is released. See [docs/API.md](../../docs/API.md).

## Boards with networking

An ESP32, an ESP8266, or an Arduino with an Ethernet shield can skip the gateway and talk to NL2Bridge over TCP directly using the binary protocol in [docs/PROTOCOL.md](../../docs/PROTOCOL.md). The serial route above is simpler and works with any board.
