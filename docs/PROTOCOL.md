# NL2Bridge wire protocol (API 7)

This is the reference for talking to NL2Bridge from any language. If you use Python, the ready-made client in `client/nl2bridge.py` already implements everything here (see [API.md](API.md)).

## Connection
- TCP, default port **15152**. A `<dll name>.port` file next to the DLL, or the `NL2BRIDGE_PORT` environment variable of the game process, changes it.
- The server listens on **all network interfaces** and has **no authentication**. Anyone who can reach the port can control the ride. Keep it behind your firewall (block the port for public networks), or only use it on a trusted LAN.
- Any number of clients can be connected at the same time. Each connection is served by its own thread; requests on one connection are answered in order.
- Every request runs while holding the game's own simulation/script lock (the lock NL2's script natives take), so a reply is always a consistent snapshot.
- The game must be in **play mode** (not the editor). Otherwise most requests return the error `Not in play mode`.

## Framing
Identical to the official NoLimits 2 telemetry server. All integers and floats are **big-endian**.

```
'N' (0x4E) | u16 messageId | u32 requestId | u16 dataSize | data[dataSize] | 'L' (0x4C)
```

- The reply echoes your `requestId`. Use an increasing counter.
- The maximum payload is 65535 bytes in either direction. A reply that would be bigger is replaced by an Error; use the per-part JSON queries (1201/1203/1202) instead of 1204 on very large parks.
- A malformed frame (wrong start or end byte) clears the connection's input buffer.

### Generic replies
| Id | Meaning | Data |
|---|---|---|
| 1 | OK | none |
| 2 | Error | UTF-8 text: the reason (often the game's own message, e.g. `Station is in automatic dispatch mode`) |
| 8 | Int | i32 |
| 10 | String | UTF-8 text, usually JSON |

Requests that return an **Int** reply 1 for success and 0 for "the game refused"; requests that return **OK** raise Error on refusal.

## Identifiers
| Name | What it is | Where to get it |
|---|---|---|
| coaster | 0-based index of the coaster in the park | 1200 |
| sectionId | the game's section id (stable while the park is loaded; changes when the park is edited and re-saved) | 1201 `id` |
| station | 0-based station index on the coaster | 1203 `index` |
| special track | 0-based index of a switch or transfer table | 1202 `index` |
| train | 0-based train index | 1205 `index` |

Look sections up **by name** at startup rather than hard-coding ids.

## Message reference

### Bridge and park
| Id | Request | Reply |
|---|---|---|
| 1000 | – | OK (ping) |
| 1001 | – | String JSON `{name, api, build}`. Require `api >= 6` for everything in this document; seat counts (marked API 7) need `api >= 7`. Bridges older than API 6 answer Error `Unknown message`. |
| 1200 | – | String JSON list of coasters: `index, name, operationMode (0 normal, 1 shuttle, 2 scripted), scripted, blockMode (1 auto, 2 manual block, 3 full manual), estop, ready, trains, sections, stations, specialTracks` |
| 1204 | i32 coaster | String JSON `{coaster, sections, stations, specialTracks, tracks}` (the lists below in one reply). `tracks`: `[{index, class, start, end}]`; each end is `{special, port}` (joined to a switch/transfer table port), `{track}` or `null`. |

Note: `blockMode` is **reported** as 1/2/3 but **set** with 0/1/2 (message 1130).

### Sections and blocks
| Id | Request | Reply |
|---|---|---|
| 1201 | i32 coaster | String JSON list of sections: `id, name, hasNode, isBlock, scripted, station, nodeType, state, stateName, stateText, lamp, trains, trainMask, canAdvanceFwd, canAdvanceBwd, hasBrake, hasLift, hasTransport, brakeMode, liftMode, transportMode, userState, advFwdVisible, advFwdEnabled, advBwdVisible, advBwdEnabled, track, trackStart, trackEnd` |
| 1210 | i32 coaster | **1211** binary block poll (below) |
| 1216 | i32 coaster | **1217** binary section-detail poll (below) |
| 1110 | i32 coaster, i32 sectionId, i32 dir (1 forward, 2 backward) | Int. Advance a block: the in-game "Advance" button in Manual Block mode (or a scripted block's advance). |

#### 1211 block record (16 bytes each)
Header: `u8 blockMode, u8 estop, u16 count`, then `count` records:
```
u32 sectionId | u8 state | u8 trainsOnBlock | u8 lamp | u8 flags | u8 brakeMode | u8 liftMode | u8 transportMode | u8 0 | u32 trainMask
flags:  bit0 can advance forward   bit1 can advance backward   bit2 is a block   bit3 scripted node
        bit4 has brake             bit5 has lift               bit6 has transport
brakeMode 0 open, 1 closed, 2 trim | liftMode 0 off, 1 forward, 2 forward idle, 3 backward
transportMode 0 off, 1 on, 2 depends on brake | 0xFF = no such device / no block node
trainMask: bit n set = train n is on the section
lamp: 0 off, 1 on, 2 flashing
```
Block `state` (the game's own names): 0 No Block, 1 Offline, 2 Idle, 3 Reserved, 4 Approaching Fwd, 5 Approaching Bwd, 6 Leaving Fwd, 7 Leaving Fwd to Ourself, 8 Leaving Bwd, 9 Leaving Bwd to Ourself, 10 Full Manual Mode, 11 Passing Fwd to Trigger, 12 Passing Bwd to Trigger, 13 Station, 14 Waiting for Clear Block, 15 Waiting for Advance, 16 Complete Stopping, 17 Wait-Time Pause, 18 Pass Through, 19 Scripted.

#### 1217 section detail (20 bytes each)
Header: `u8 operationMode, u8 blockMode, u8 estop, u8 0, u16 count`, then per section:
```
u32 sectionId | u16 flags | i8 trainIndex (-1 none) | u8 state | i32 userState (-1 unless scripted) | f32 liftSpeed | f32 transportSpeed
flags: bit0 train before centre   bit1 behind centre   bit2 before brake trigger   bit3 behind brake trigger
       bit4 before lift trigger    bit5 behind lift trigger   bit6 brakes on   bit7 station waiting for clear block
       bit8 station waiting for advance   bit9 is station   bit10 advance-fwd visible (scripted)   bit11 can advance fwd
       bit12 advance-bwd visible (scripted)   bit13 can advance bwd   bit14 scripted node   bit15 occupied
```
The position flags are the game's own `Section.isTrainBefore/Behind*` queries, the same ones the game's block logic uses to decide when a train has arrived.

### Trains
| Id | Request | Reply |
|---|---|---|
| 1205 | i32 coaster | String JSON list: `index, blockId, blockName, sections[], station (-1), speed (m/s), accel, harness (0 closed..1 open), flyer, lashed, front/center/rear {track, pos}, seats, seatedCars, seatsPerCar` (API 7) |
| 1151 | i32 coaster, i32 train, i32 lashed (1/0) | OK. The train's "Lashed To Track" flag. The game only allows Auto/Manual block mode when every train on a storage track is lashed and no other train is. |

### Coaster-wide control
| Id | Request | Reply |
|---|---|---|
| 1130 | i32 coaster, i32 mode (0 Auto, 1 Manual Block, 2 Full Manual) | Int 1, or Error with the game's reason (e.g. `Each train must be on a block section`, `All trains need to stop`) |
| 1131 | i32 coaster, u8 on | OK. Emergency stop on/off. |
| 1132 | i32 coaster | OK. Simulation reset (`Coaster.requestReset`): trains back to their start positions, block mode back to Auto. Only runs while the game is simulating; a minimized NL2 window is paused. |

### Stations
| Id | Request | Reply |
|---|---|---|
| 1203 | i32 coaster | String JSON list: `index, name, sectionId, flags, state, manualDispatch, hasTrain, train (-1), seats, seatedCars, seatsPerCar, trainReady, canDispatch, waitingForClearBlock, waitingForAdvance` and objects `gates {present, position, open, closed, opening, closing, canOpen, canClose}`, `harness {position, open, closed, moving, canOpen, canClose, rowsOpen}`, `platform {present, position, raised, lowered, moving, canRaise, canLower}`, `flyer {present, position, locked, unlocked, moving, canLock, canUnlock}`. `train`, `seats`, `seatedCars` and `seatsPerCar` are API 7. |
| 1212 | i32 coaster | **1213**: `u16 count`, then per station 8 bytes: `u32 flags, u8 state, u8 rowsOpenCount, u16 seats` (seat capacity of the train in the station, 0 if empty; API 7, 0 before) |
| 1141 | i32 coaster, i32 station, i32 op | OK, or Error with the reason the game refused (recommended) |
| 1140 | i32 coaster, i32 station, i32 op | Int 1/0 (unchecked, no reason) |
| 1206 | i32 coaster, i32 station | String JSON per-row restraints of the train in the station: `{enabled, station, hasTrain, train, trainHarness, trainMotion, rowsOpen, seats, seatedCars, rows:[{row, position, open, closed, opening, closing, individual, seats}], cars:[{..., seats}]}`. Row 1 is the front row. `seats` fields are API 7. |
| 1142 | i32 coaster, i32 station, i32 row (1-based, 0 = all), i32 open (1/0) | OK or Error. Open or close one row's restraints. |

Station ops:
| op | Action |
|---|---|
| 0 / 1 | manual dispatch / automatic dispatch |
| 2 | dispatch |
| 3 / 4 | gates open / close |
| 5 / 6 | harness (restraints) open / close |
| 7 / 8 | platform raise / lower (floorless coaster floor) |
| 9 / 10 | flyer seats unlock / lock |

Ops 2–10 need the station in manual dispatch mode and a train stopped in it, exactly like the in-game buttons, and the game refuses unsafe moves. **In Full Manual block mode the game refuses every station op**; run in Manual Block mode.

While any single row is open (1142), the bridge refuses dispatch, and "harness close" (op 6) also closes the open rows.

Seat capacity (API 7) is counted from the train's car models: each seat is a `HEAD` node in a car's model, the same list the game uses for on-ride seat cameras. `seats` is the train total, `seatedCars` the cars that have seats (a front car or locomotive without seats is not counted) and `seatsPerCar` the largest car. A park whose trains are drawn by a script on top of an invisible NL2 train has no car models to count, so it reports 0 seats and has no per-row restraints.

Station flags (1213 and `flags` in 1203); bits 0–10 match the official telemetry station state:
```
bit0 E-stop            bit1 manual dispatch   bit2 can dispatch       bit3 can close gates   bit4 can open gates
bit5 can close harness bit6 can open harness  bit7 can raise platform bit8 can lower platform
bit9 can lock flyer    bit10 can unlock flyer bit11 train ready (stopped in station)
bit12 gates open       bit13 gates closed     bit14 harness open      bit15 harness closed   bit16 platform raised
bit17 platform lowered bit18 flyer unlocked   bit19 flyer locked      bit20 has gates        bit21 has platform
bit22 has flyer        bit23 train in station bit24 some rows open (per-row restraints)
```
The "can" bits are only set in manual dispatch mode; the position bits work in every mode.

### Devices: brakes, lifts, transport wheels
| Id | Request | Reply |
|---|---|---|
| 1207 | i32 coaster, i32 sectionId | String JSON `{brake, lift, transport}`, each `{present, state, type}` |
| 1150 | i32 coaster, i32 sectionId, i32 device (0 brake, 1 lift, 2 transport), i32 value | Int 1/0 or Error. Brake 0 open / 1 closed; lift 0 off / 1 on; transport 0 off / 1 forward / 2 backward. Same as the game's Full Manual control panel: needs **Full Manual** block mode, no pause, no E-stop, coaster ready. |
| 1208 | i32 coaster, i32 sectionId | String JSON `{lift, transport}`, each `null` or `{speed, accel, decel, idleMode, current}` (m/s, m/s²) |
| 1152 | i32 coaster, i32 sectionId, i32 device (1 lift, 2 transport), f64 speed, f64 accel, f64 decel | OK. Changes the device's parameters live, **in any block mode**. Values < 0 are left unchanged; accel/decel must stay > 0. |

1152 is how to stop and start a lift in Auto or Manual Block mode: set `speed` to 0 and the chain ramps down with its deceleration while the train on it holds; set it back to the design speed (read it with 1208 first) and it ramps up again. The chain animation and sound follow. The lift's idle speed is fixed by the game (half the speed, capped at 0.4 m/s).

### Switches and transfer tables
| Id | Request | Reply |
|---|---|---|
| 1202 | i32 coaster | String JSON list: `index, name, type (switch / switchFork / switchMerge / transferTable), class, directions, current, target, moving, switchable, manualAllowed` |
| 1214 | i32 coaster | **1215**: `u16 count`, then per track 4 bytes: `i8 current (-1 moving), i8 target, u8 directions, u8 flags` (bit0 moving, bit1 switchable, bit2 manual switching allowed, bit3 transfer table) |
| 1120 | i32 coaster, i32 index, i32 direction | Int 1 (moving / already there), 0 (not switchable now: a train is on it), or Error. Needs Manual or Full Manual block mode with manual switching enabled on the track (or a scripted coaster). |

### Sensors and events
| Id | Request | Reply |
|---|---|---|
| 1300 | i32 coaster (-1 all) | String JSON list of track triggers: `key, coaster, track, id, name, named, step, active, trains, lastTrain, enters, leaves, msSinceChange` |
| 1301 | i32 coaster (-1 all) | **1302**: `u16 count`, then 16 bytes each: `u32 key, u8 active, u8 lastTrain (0xFF none), u16 0, u32 trainMask, u32 passes` |
| 1303 | u32 sinceSeq | String JSON `{enabled, seq, events:[{seq, coaster, type, typeName, msAgo, ...}]}` |

Event types: 1 `sensorEnter` / 2 `sensorLeave` (`sensorKey, sensorId, sensorName, train`), 3 `modeAuto`, 4 `modeManualBlock`, 5 `modeFullManual`, 6 `advanceFwdPressed` / 7 `advanceBwdPressed` (`sectionId, sectionName`). Pass the returned `seq` as `sinceSeq` next time; send `0xFFFFFFFF` to learn the current `seq` without receiving history. At most 300 events are returned per call.

Sensor names are captured when the park's scripts create their triggers, so inject the bridge **before** loading the park (or reload it).

### Raw section and scripted-block commands
These are for coasters set to **Scripted operation** in the NL2 editor, where the game hands brakes, lifts and tires to a script. On normal coasters the game's block logic owns the devices and refuses them (use 1150/1152 instead).

| Id | Request | Reply |
|---|---|---|
| 1100 | i32 coaster, i32 sectionId, i32 cmd, i32 param | Int 1/0. `Section` command (table below). |
| 1101 | i32 coaster, i32 sectionId, i32 query, f64 in | **1102**: `i32 ok, i32 intValue, f64 doubleValue` |
| 1111 | i32 coaster, i32 sectionId, i32 cmd, i32 param | Int. Scripted block node: 1/2 show the forward/backward Advance button, 3/4 enable it (param 1/0). |
| 1112 | i32 coaster, i32 sectionId, i32 state, i32 lamp (0 off, 1 on, 2 flash), UTF-8 text (rest of the message) | OK. `Block.registerState`: text and lamp shown on the in-game panel. |
| 1113 | i32 coaster, i32 sectionId, i32 state | OK. `Block.setState`. |

Section commands (1100):
| cmd | Action | cmd | Action |
|---|---|---|---|
| 7 / 8 / 12 | brakes open / closed / trim | 23 / 24 / 25 | lift forward / backward / off |
| 41 | lift forward idle | 9 | transport off |
| 10 / 11 | transport forward / backward | 13 / 14 | transport forward / backward depending on brake |
| 17 / 18 | launch forward / backward | 21 / 5 | station: train entering / leaving |
| 2 / 3 | station: next block clear / occupied | 44 | station manual dispatch mode (param 1) |

Section queries (1101): 0x1c train mask, 0x0f/0x10 before/behind brake trigger, 0x13 brake stops completely, 0x14 brake wait time (double), 0x16 is station, 0x1a/0x1b before start/behind end (in = distance), 0x20/0x21 before/behind centre, 0x22/0x23 before/behind lift trigger, 0x26 transport speed (double), 0x27 brakes on, 0x28 lift speed (double), 0x2a has lift, 0x2c station manual dispatch.

## Example: raw frame
Ping with request id 1:
```
4E 03E8 00000001 0000 4C
```
Reply:
```
4E 0001 00000001 0000 4C
```
Set block mode of coaster 0 to Manual Block (1130 = 0x046A, data `00000000 00000001`):
```
4E 046A 00000002 0008 00000000 00000001 4C
```
