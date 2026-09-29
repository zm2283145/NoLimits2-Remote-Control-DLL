# Examples

Python (uses `client/nl2bridge.py`, no packages needed):

| Script | What it does |
|---|---|
| `python/monitor.py` | Live one-line status: block mode, occupied blocks, trains and speeds, station. Read-only. |
| `python/dispatch_cycle.py` | One operator cycle in Manual Block mode: wait for a train, gates and restraints open, board, close, dispatch. `--release` hands the coaster back to the game. |
| `python/lift_stop_start.py` | Stops a lift chain by setting its speed to 0 (works in Auto), then restarts it at the design speed. |

Node.js 18+ (no packages needed):

| Script | What it does |
|---|---|
| `node/nl2bridge.mjs` | A minimal client class (framing, JSON and binary replies, block mode, station ops, lift speed) and a 5-second block monitor when run directly. |

Arduino (physical control panel over USB serial, see [arduino/README.md](arduino/README.md)):

| File | What it does |
|---|---|
| `arduino/NL2Panel/NL2Panel.ino` | Example panel for an Arduino Uno: dispatch, gates, restraints, single-row release, E-stop/reset, lift start/stop, manual/transfer keys, advance and status lamps. |
| `arduino/NL2Panel/NL2Serial.h` | Arduino library: one method for every NL2Bridge command, callbacks for status. |
| `arduino/serial_gateway.py` | PC side: links the Arduino's USB serial port to NL2Bridge (`pip install pyserial`). `--console` lets you type the commands to test without hardware. |

All of them take `--host` / `--port` (Node: `node nl2bridge.mjs <host> <port>`), so they also work from another PC.
