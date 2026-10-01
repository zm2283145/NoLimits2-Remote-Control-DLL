# Programming against NL2Bridge (Python)

`client/nl2bridge.py` is a single-file Python 3 library with no dependencies. Copy it into your project, or add the `client` folder to `PYTHONPATH`. For other languages, see [PROTOCOL.md](PROTOCOL.md); every method below is one request/reply.

```python
from nl2bridge import NL2Bridge, NL2Error

with NL2Bridge("127.0.0.1", 15152) as nl:
    info = nl.bridge_info()
    assert info["api"] >= 6, "NL2Bridge too old"
    c = nl.coaster("Fury 325")                 # coaster index from a name
    for b in nl.blocks(c):
        if b["isBlock"]:
            print(b["id"], b["name"], b["stateName"], b["trains"])
```

## Concepts

### Addressing
- **Coaster**: an index or a coaster name.
- **Block / section**: a section id or section name (e.g. `"Lift"`, `"Station"`, `"Block Brake"`). Names are resolved with an extra request, so in a fast loop resolve them once and use the ids.
- **Station**: a station index or the station section's name.
- **Switch / transfer table**: a special-track index or name.

Section ids change when the park is edited and saved, so look them up by name when you connect.

### Block modes
NoLimits 2 has three block modes. They decide what you are allowed to do:

| Mode | `set_block_mode` | Who moves the trains | Use it for |
|---|---|---|---|
| Auto | `"auto"` | The game's block logic | Reading everything; stations in manual dispatch; lift speed control |
| Manual Block | `"manual"` | The game, but each block waits for **Advance** | Operator control: advance blocks, dispatch, gates, restraints, switches and transfer tables |
| Full Manual | `"fullmanual"` | You: every brake, lift and transport wheel | Direct device moves (`set_device`). **Stations don't work in this mode** |

A good controller runs in Manual Block mode and drops into Full Manual only for the few seconds a device move needs. The game refuses to change mode while trains are between blocks or moving, and raises `NL2Error` with its reason; retry until it succeeds.

`coasters()[i]["blockMode"]` reports 1 = auto, 2 = manual, 3 = full manual (`BLOCK_MODE_NAMES` maps it).

### Operation mode
Each coaster has an operation mode set in the NL2 editor: normal, shuttle or scripted (`coasters()[i]["operationMode"]`, read-only). Everything in this guide works on **normal** coasters. The raw section commands (`set_brakes`, `set_lift`, `set_transport`, `register_state` ...) only work on **scripted** coasters, where no park script is needed because your program becomes the script.

### Errors
Every refusal raises `NL2Error` with a readable reason, usually the game's own text:
```python
try:
    nl.dispatch(c, "Station")
except NL2Error as e:
    print("refused:", e)          # e.g. "Station is in automatic dispatch mode"
```
Connection problems raise `OSError` / `ConnectionError`. The game must be in play mode: in the editor every call raises `NL2Error("Not in play mode")`.

## Reference

### Park and bridge
| Method | Returns |
|---|---|
| `bridge_info()` | `{name, api, build}`; `api` is 0 for bridges older than API 6. Seat counts need API 7 (1.1.0), `customTrain` API 8 (1.2.0) |
| `ping()` | `True` |
| `coasters()` | list of `{index, name, operationMode, scripted, blockMode, estop, ready, trains, sections, stations, specialTracks}` |
| `coaster(key)` | coaster index |
| `coaster_info(c)` | `{coaster, sections, stations, specialTracks, tracks}` in one call |

### Blocks and trains (reading)
| Method | Returns |
|---|---|
| `blocks(c)` | every section: `id, name, isBlock, station, state, stateName, trains, trainMask, canAdvanceFwd/Bwd, hasBrake/hasLift/hasTransport, brakeMode, liftMode, transportMode, track, trackStart, trackEnd, ...` |
| `block(c, key)` | one section |
| `block_states(c)` | fast binary poll: `(mode, estop, [ {id, state, stateName, trains, lamp, canAdvanceFwd, ...} ])` |
| `section_detail(c)` | fast binary poll with train-position flags: `(info, [ {id, occupied, beforeCenter, behindCenter, brakesOn, canAdvanceFwd, trainIndex, liftSpeed, transportSpeed, ...} ])` |
| `trains(c)` | every train: `index, blockId, blockName, sections, speed, accel, harness, lashed, front/center/rear {track, pos}, seats, seatedCars, seatsPerCar, customTrain` |
| `sensors(c)`, `sensor_states(c)` | track triggers and their live state |
| `events(since)` | sensor enter/leave, block-mode changes and in-game Advance presses since a sequence number |

### Coaster-wide control
| Method | Does |
|---|---|
| `set_block_mode(c, "auto" \| "manual" \| "fullmanual")` | change block mode (raises with the game's reason if refused) |
| `estop(c, on=True)` | emergency stop on/off |
| `reset_coaster(c)` | simulation reset: trains back to their start positions, block mode back to Auto |
| `advance(c, block, backwards=False)` | the in-game Advance button (Manual Block mode); returns `False` if the block can't advance now |

### Stations
| Method | Does |
|---|---|
| `stations(c)`, `station_status(c, st)` | full station state: gates, harness, platform, flyer, `canDispatch`, `trainReady`, `train` and its `seats` ... |
| `station_states(c)` | fast binary poll of every station (booleans, plus `seats` of the train in it) |
| `station_seats(c, st)` | seat capacity of the train in the station: `{train, seats, seatedCars, seatsPerCar, customTrain}` (0 seats if empty, or if the park draws its trains with a script) |
| `custom_train(c, st)` | `True` when the train in the station is drawn by a park script instead of an NL2 car model |
| `set_manual_dispatch(c, st, manual=True)` | manual or automatic dispatch |
| `dispatch(c, st)` | dispatch the train |
| `open_gates` / `close_gates` | gates |
| `open_restraints` / `close_restraints` | all restraints |
| `rows(c, st)` | per-row restraint state (and seats per row) of the train in the station |
| `open_row(c, st, row)` / `close_row(c, st, row)` | one row (1 = front) |
| `raise_floor` / `drop_floor` | floorless coaster floor / platform |
| `unlock_flyer` / `lock_flyer` | flying coaster seats |

Station operations need manual dispatch and a train stopped in the station, and the game refuses unsafe moves (for example dispatching with the gates open). They don't work in Full Manual block mode.

### Devices
| Method | Does |
|---|---|
| `devices(c, block)` | `{brake, lift, transport}` each `{present, state, type}` |
| `set_device(c, block, "brake" \| "lift" \| "transport", value)` | Full Manual only. Brake 0 open / 1 closed; lift 0 off / 1 on; transport 0 off / 1 forward / 2 backward |
| `device_params(c, block)` | `{lift, transport}` each `None` or `{speed, accel, decel, idleMode, current}` |
| `set_device_params(c, block, "lift" \| "transport", speed=, accel=, decel=)` | change speed / acceleration live, **in any block mode**; omitted values stay unchanged |
| `lash_train(c, train, lashed=True)` | a train's "Lashed To Track" flag (needed for trains parked on storage tracks) |

### Switches and transfer tables
| Method | Does |
|---|---|
| `special_tracks(c)` | every switch / transfer table: `type, directions, current, target, moving, switchable, manualAllowed` |
| `switch_states(c)` | fast binary poll |
| `set_switch(c, track, direction)` | move it (Manual or Full Manual block mode, with manual switching enabled on the track) |

### Scripted-operation coasters only
`set_brakes`, `set_lift`, `set_transport`, `scripted_block`, `register_state`, `set_state`, `station_entering`, `station_leaving`, `station_next_clear`, `station_next_occupied`, `section_set`, `section_get`. See [PROTOCOL.md](PROTOCOL.md#raw-section-and-scripted-block-commands).

## Recipes

### Poll loop
Use the binary polls in a loop and the JSON calls once at startup:
```python
import time
with NL2Bridge() as nl:
    c = nl.coaster("Fury 325")
    names = {s["id"]: s["name"] for s in nl.blocks(c)}
    while True:
        mode, estop, blocks = nl.block_states(c)
        busy = [names[b["id"]] for b in blocks if b["isBlock"] and b["trains"]]
        print(mode, "E-STOP" if estop else "", busy)
        time.sleep(0.1)
```
20 polls per second of `block_states` + `section_detail` + `station_states` is comfortable.

### Load and dispatch a train (Manual Block mode)
```python
c = nl.coaster("Fury 325"); st = "Station"
nl.set_block_mode(c, "manual")
nl.set_manual_dispatch(c, st, True)
while not nl.station_status(c, st)["trainReady"]:
    time.sleep(0.2)
nl.open_gates(c, st); nl.open_restraints(c, st)
time.sleep(10)                                  # riders board
nl.close_restraints(c, st); nl.close_gates(c, st)
while not nl.station_status(c, st)["canDispatch"]:
    time.sleep(0.2)
nl.dispatch(c, st)
```

### Advance the block before the station
```python
if nl.block(c, "Waiting Brake")["canAdvanceFwd"]:
    nl.advance(c, "Waiting Brake")
```

### Stop and start a lift (any block mode)
```python
lift = nl.block_id(c, "Lift")
design = nl.device_params(c, lift)["lift"]["speed"]    # remember it before changing anything
nl.set_device_params(c, lift, "lift", speed=0)          # LIFT STOP: the chain ramps down, the train holds
...
nl.set_device_params(c, lift, "lift", speed=design)     # LIFT START
```
Always restore the design speed when your program exits; the game keeps the value until the park is reloaded.

### Move a device by hand (Full Manual)
```python
nl.set_block_mode(c, "fullmanual")
nl.set_device(c, "Transfer", "brake", 0)          # open the brakes
nl.set_device(c, "Transfer", "transport", 1)      # transport wheels forward
...
nl.set_device(c, "Transfer", "transport", 0)
nl.set_device(c, "Transfer", "brake", 1)
nl.set_block_mode(c, "manual")                    # retry while it raises "All trains need to stop"
```
Stop a train on a block with `section_detail`: once the destination block is occupied and its `beforeCenter` flag drops (forward), the train has reached the centre.

### Transfer table
```python
t = nl.special_tracks(c)[0]
if t["switchable"] and not t["moving"]:
    nl.set_switch(c, t["index"], 2)               # position 2
```
A transfer table only moves when no train is half on it. Trains parked on storage tracks must be lashed (`lash_train`) before the game will return to Auto or Manual Block mode.

### React to events
```python
seq = nl.events(0xFFFFFFFF)["seq"]
while True:
    ev = nl.events(seq); seq = ev["seq"]
    for e in ev["events"]:
        if e["typeName"] == "sensorEnter":
            print("train", e["train"], "hit", e["sensorName"])
    time.sleep(0.05)
```

## Command-line tool
`client/nl2bridge_client.py` wraps the library for quick tests:
```
python client/nl2bridge_client.py coasters
python client/nl2bridge_client.py blocks "Fury 325"
python client/nl2bridge_client.py watch 0
python client/nl2bridge_client.py mode 0 manual
python client/nl2bridge_client.py station 0 Station gates_open
python client/nl2bridge_client.py --port 15153 switches 0
```
