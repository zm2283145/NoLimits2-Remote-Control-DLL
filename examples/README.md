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

All of them take `--host` / `--port` (Node: `node nl2bridge.mjs <host> <port>`), so they also work from another PC.
