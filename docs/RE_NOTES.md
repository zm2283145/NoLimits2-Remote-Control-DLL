# NoLimits 2 – reverse-engineering notes

These notes cover `nolimits2stm.exe` (Steam build). Its PE header timestamp is 0x696F4E97 (2026-01-20); the file is 17,923,144 bytes, image base 0x140000000, and ASLR is not enabled. Addresses below are virtual addresses (VAs); the RVA is the VA minus 0x140000000. Function names like `FUN_1400ab910` are Ghidra's auto-generated names.

## 1. Static analysis
- Addresses were found by static analysis of the game executable in Ghidra and confirmed against the running game. The DLL itself never modifies the executable on disk and works with the unmodified Steam install.
- `nolimits2core.dat` (the script class library) and `nolimits2stm.nld` (the data archive) were not needed: every command code was recovered from the native side.

## 2. How we found the ride-control code
The NLVM script engine registers its native methods by name and signature. Following the registration calls led to:

| Script native | VA | Calls into |
|---|---|---|
| `internSetSectionNodeState (IIII)I` | 0x14037E9A0 | `SectionSet` 0x1400AB910 |
| `internGetSectionNodeState (IIII)I` | 0x14037CE80 | `SectionGet` 0x1400A6460 |
| `internGetSectionNodeStateDouble (IIID)D` | 0x14037CF50 | `SectionGet` 0x1400A6460 |
| `internSetBlockNodeState (IIII)I` | 0x14037E8D0 | block node vtable slot 0x58 |
| `internGetBlockNodeState (III)I` | 0x14037C400 | 0x1400A63C0 (cmd 6 = semi-manual flags, cmd 8 = state/lamp) |
| `Block.getNormalModeState` | 0x140370770 | reads node+0x4D |
| `Coaster.setBlockSystemMode` | 0x140370D10 | 0x1400BBBE0 / 0x1400BC2B0 checks, then 0x1400CF7E0 |
| `SpecialTrack.setSwitchDirection` | 0x140378940 | 0x14047CC40 |
| `SpecialTrack.setManualSwitchDirection` | 0x140378650 | 0x14047CC40 |
| `Simulator.getCoasterCount` | 0x14037A570 | reads `g_pSim` at 0x141011A00 |
| `Experimental.getTelemetryCustomBool/Float` | 0x14037B230 / 0x14037B280 | arrays at 0x141014610 / 0x141014A10 |

Every one of them wraps its work in `Lock(&g_Lock)` / `Unlock(&g_Lock)`. The lock object is at 0x1410144E8, the lock function at 0x14053F290 and the unlock function at 0x14053F870. It is a non-recursive benaphore.

## 3. Object model
```
g_pSim (0x141011A00) -> Sim*;  sim+0x1AA play-mode flag;  *sim = Park*
Park   +0x92 play flag,  +0x98/+0xA0 std::vector<NLCoaster*>
NLCoaster
  +0x068  std::string name
  +0x0C0  std::vector<NLSpecialTrack*>   (switches / transfer tables)
  +0x0F0  std::vector<block*>
  +0x120  std::vector<NLTrain*>          (train+0x540: a double the game zeroes on mode change; meaning unconfirmed)
  +0x180  NLBlockSystem*
  +0x188  std::vector<Station> (0x18 each)   -> StationAt() 0x1404279B0
  +0x290  u8 operation mode (0 normal, 1 shuttle, 2 scripted) - verified live
  +0x291  u8 block mode (1 Auto, 2 Manual Block, 3 Full Manual)
  +0x1A06E u8 ready   +0x1A06F u8 busy   +0x1A070 u8 E-stop
NLBlockSystem
  +0x00/+0x08 std::vector<NLBlockNode*>
  +0x18 SectionEntry* sorted by id,  +0x20 count
  SectionEntry { u32 id; NLBlockNode* node; SectionData* section; }   (0x18 bytes)
NLBlockNode (RTTI: NLStationBlockNode, NLLiftBlockNode, NLBrakeBlockNode, NLTransferBlockNode, NLStorageBlockNode,
             NLSwitchBlockNode, NLScripted*BlockNode ...)
  +0x4C u8 trains on block,  +0x4D u8 state,  +0x480 u8 isBlock
  vtbl+0x08 GetStateText(std::string*, u8* lamp)
  vtbl+0x58 SetState(cmd, param)     cmd 7 = semi-manual move (bit0 fwd, bit1 bwd); scripted nodes also take 1..5
  vtbl+0x88 IsScripted()             0x1400A71B0 returns false for normal nodes, 0x1400A71C0 returns true for scripted ones
  vtbl+0x90 GetStation()
  vtbl+0xA8 SemiManual(op)           1 canFwd, 2 doFwd, 4 canBwd, 5 doBwd
SectionData
  +0x30 brake device  +0x38 lift device  +0x40 transport device
  +0x80 std::string name   +0x878 u32 id
  +0x36C brake mode (0 off, 1 on, 2 trim)
  +0x36D transport mode (0 off, 1 standard, 2 depends on brake);  +0x330/+0x338/+0x340 speed/accel/decel
  +0x36E lift mode (0 off, 1 fwd, 2 fwd idle, 3 bwd)
Station
  +0x41 u8 auto-dispatch;  StationOp 0x1403E26F0(op 0..10);  StationCan 0x1403E2B00(op)
NLSpecialTrack
  vtbl+0x28 direction count;  +0x70 current direction (-1 = moving);  +0x78 target direction
  +0x8C/+0x8D switchable;  +0x8E manual switching allowed
```

## 4. Why scripted mode matters
Every `SectionSet` command first calls `node->IsScripted()` (vtable slot 0x88). On normal nodes that returns false, so commands to brakes, lifts and tires are refused, because the built-in block logic owns them. Reads (`SectionGet`, block state) work in every mode. The semi-manual advance (`SetState` cmd 7) also works on normal nodes; it's the in-game "Advance" button in Manual Block mode.

## 5. Telemetry server
- The network thread is 0x140427C70. Its jump table covers message IDs 0–0x20. Coaster and station messages are queued to the main thread and handled by 0x140429030.
- There are two **undocumented** messages:
  - **22:** `u8 n, n × (u8 index, u8 bool)`. It *toggles* custom bools (a counter at 0x141014610).
  - **23:** `u8 n, n × (u8 index, f32 BE)`. It sets custom floats at 0x141014A10.
- Park scripts read these through `com.nolimitscoaster.Experimental.getTelemetryCustomBool/Float`. This is a DLL-free way to send commands *into* the game, but nothing comes back out.
- The official coaster and station messages call `StationOp` with the same op numbers as the bridge: SetManualMode → 0/1, Dispatch → 2, Gates → 3/4, Harness → 5/6, Platform → 7/8, Flyer car → 9/10.

## 6. Still to confirm live
- The exact meanings of section commands 2 and 3 (station next-block clear/occupied).
- Platform (7/8, floorless floor) and flyer (9/10) ops on a park that has them (Fury 325 has neither). Gates, harness, dispatch and manual/auto were confirmed live.
- The labels of the query codes marked *italic* in README.md.
- That `sim+0x1AA` / `park+0x92` are the only play-mode checks needed; the bridge copies what the telemetry server does.
- How the game's scripted-mode station handler behaves when no script is attached (commands 21 and 5 enter and leave the handler).

## 8. Track triggers and sensors
- Every sensor is an engine **NLTrackTrigger**. That covers scripted sensor packs (for example Red's Track Sensors, which call `TrackTrigger.createTrackTriggerAtOffset`) and triggers placed in the editor.
- Script native `nativeAddTrackTriggerAtPositionWithOffset (IIDD)I` at 0x14037F480:
  - It creates an `NLTrackTriggerPoint` (0x80 bytes) with FUN_140464220. Layout: +0x20 step coordinate, +0x48 type (1), +0x50 name (std::string), +0x70 `NLTrackTrigger*`.
  - The `NLTrackTrigger` (0x70 bytes) has +0x10 id, +0x18 listener vector and +0x48 a back-pointer to its point.
  - The point is inserted into the track's list at track+0x150/+0x158 (tracks come from NLCoaster+0xD8/+0xE0) and into the script VM's `NLScriptTrackTriggers` vector (+0x10), stored under VM key 0x14101387C.
- Per-frame event flow (at 0x140351C60):
  1. For every coaster, `coaster+0x1A060` is set to the count of events in `coaster+0x168` (0x18-byte records: {NLTrackTrigger*, NLTrain*, u32, u32 type}).
  2. The scripts run.
  3. `FUN_1400C9EA0` erases the consumed events.
  - The bridge hooks FUN_1400C9EA0 so it sees every event exactly once. Type 1 and type 2 are enter and leave; live logs match (1 = enter, 2 = leave).
- The native `internGetCoasterEvent` exposes {type, trigger id (+0x10), train index (train+0x6BE)} for types 1 and 2.
- Owning scene object name:
  1. `VmStorageGet(&vm, &obj, key 0x141013958)` returns an `NLScriptEntity` (entity id at **+0x10**, as read by NatParentEntity 0x1403800B0).
  2. `EntityObjForId(id, vm)` at 0x14037FCE0 returns the scene object; its name is at +0x30.
  - The bridge hooks the add-trigger native to record name → trigger. The bridge therefore has to be loaded **before** the park starts, or the park has to be reloaded afterwards.

- Track point lists (track+0x150) are **mixed**: only some entries are `NLTrackTriggerPoint`. The bridge accepts an entry only if its vtable is `NLTrackTriggerPoint` (0x140C3B3B8) and its trigger's vtable is `NLTrackTrigger` (0x140C3AED0), both found at runtime from RTTI. It reads through `ReadProcessMemory`. The first build dereferenced every entry and crashed the game.
- Fury 325: 29 triggers across the tracks, events confirmed with train indexes.

## 9. Stations, devices and special tracks (API v2)
- The coaster's station vector at +0x188 has 0x18-byte entries `{NLBlockNode*, Station*, NLSection*}`. `StationAt` (0x1404279B0) returns entry+8. The station's name is its section's name (section+0x80).
- Station: +0x00 NLSection*, +0x10 current NLTrain*, +0x40 state (0x15–0x17 = train stopped and ready), +0x41 auto-dispatch flag, +0x46 has flyer.
- Devices hang off the station's NLSection:
  - +0x68 `NLStationPlatformDevice` (also the floorless floor): +0x30 double position (1 raised / 0 lowered), +0xA8 motion. `DeviceActive` (0x14013EF60) tells whether it is configured.
  - +0x70 `NLStationSeatLockerDevice` (flyer).
  - +0x78 `NLStationGatesDevice`: +0x30 position (1 open), +0x50 opening, +0x51 closing.
- Train: +0x580 float flyer lock (1 locked) and +0x6C5 motion; +0x584 float harness (1 open) and +0x6C6 motion.
- `StationOp` 0x1403E26F0 (op 0–10) and `StationCan` 0x1403E2B00 (query 0–17). Even queries are "can do"; odd queries report a position and only answer in manual dispatch mode, so the bridge reads the devices directly instead.
- The official telemetry station bits (0x140429030) are bit0 E-stop, bit1 manual, then Can(1, 4, 2, 8, 6, 10, 12, 16, 14). The bridge keeps that order for bits 0–10.
- Special tracks: name at +0x98; the kind comes from RTTI (`NLTransferTableSpecialTrack`, `NLSwitchTrackForkSpecialTrack`, `NLSwitchTrackMergeSpecialTrack`).
- `NLScriptedBlockNode::SetState` (vtable +0x58, 0x1400AB850) supports:
  - cmd 1/2: forward/backward Advance button visible (+0x49C/+0x49E).
  - cmd 3/4: button enabled (+0x49D/+0x49F).
  - cmd 5: +0x4F, meaning unknown.
  - Normal nodes accept only cmd 7.
- A scripted node's state text and lamp come from a script-registered map (node+0x488, keyed by +0x498). The bridge reads them but can't set them yet.
- Operation mode (normal / shuttle / scripted) picks the block node classes at park load, so the bridge exposes it read-only.
- Switching block mode back to Auto is refused while any train is between blocks ("Each train must be on a block section"). Keep advancing trains and retry.

## 10. Reset, full-manual devices, transfer tables (API v5)
- **Reset**: the `Coaster.requestReset` native (0x140370C80) sets coaster+0x1A077 = 1 and xchg's the global byte 0x1410119B3 = 1, both under g_Lock. The sim loop (0x140351350) resets flagged coasters. A **minimized NL2 window does not simulate**, so nothing happens until it is restored.
- Coaster flags: +0x1A06E ready, +0x1A06F pause, +0x1A070 E-stop, +0x291 block mode (1 auto / 2 manual / 3 full manual).
- **Section devices**: section+0x30 brake, +0x38 lift, +0x40 transport. State byte at dev+0x21. SetState is vtable +0x38; the game's full-manual controls (`NLDeviceOnOffControl`, `NLDeviceBwdOffFwdControl`) call it under the guards above plus block mode 3.
- **Transfer tables**:
  - The game's manual block mode never centres a train on the table. Advancing the transfer block passes the train straight through to `node->next[table dir]`.
  - Centring therefore runs in full manual: open the brakes, turn on the transports of the source and destination blocks, and stop on the destination block's own centre sensors.
    - Forward: stop when the destination is occupied and section query "before center" drops.
    - Backward: stop when "behind center" drops.
  - The table is switchable only when no train's cars touch both the table track and another track (0x14046A9D0 clears special+0x8C/+0x8D). Fury 325's table takes about 45 s per move.
- **Lashed To Track**: the mode-change check (0x1400BBBE0) refuses Auto/Manual unless every train on a storage track has train+0x6C3 set and no other train does ("Trains on storage tracks must be 'Lashed To Track'").
  - Script native `Train.setLashedToTrack` (0x140379EC0) calls the train command fn 0x14049D5E0: cmd 0 = lash (word +0x6C3 = 1, which also clears +0x6C4 "Move Train"), cmd 1 = unlash.
  - Other commands: 2/3 Move Train on/off, 8 Move Train speed (+0x518), 4–7 harness/flyer motion, 9–12 flags +0x6C7/+0x6C8, 13 per-car vfunc +0x60.
- Leaving full manual also needs every train stopped ("All trains need to stop" appears briefly after a stop) and on a single block.

## 11. Track connections (API v5d)
- `coaster+0xD8` is the track vector:
  - `NLCustomTrack` entries are drawn track pieces.
  - `NLBranchTrack` entries are the moving pieces owned by switches and transfer tables; they use a different layout.
- An `NLCustomTrack` stores two end links, each a `{object*, i32 port}` pair:
  - `+0x1F8` / `+0x200`: the END side, where trains go after the track's end.
  - `+0x208` / `+0x210`: the START side, where trains come from before position 0.
  - `object` is the special track (an entry of the `coaster+0xC0` vector) or track the end is joined to. `port` is the special-track position (direction index) that lines up with it. A null object means the end is free (port −1).
- Fury 325:
  - Track 0 (the main circuit) has both ends linked to the transfer table on port 3 (POS 4). The table therefore sits at track 0's position-0 seam, between the Station (end of the track) and the first section.
  - Storage tracks 1–3 link at their start to table ports 0/1/2. Their ends are free.
- The start/end naming comes from the storage tracks, which only link at `+0x208`. It is consistent with the train path Station (track 0, pos 8044) → table → track 0 pos 0.
- Not yet understood: track 0 also has pointers at `+0x1C0`/`+0x1C8` that the storage tracks lack (maybe direct track-to-track joins).
- Reported by 1204 as `tracks: [{index, class, start, end}]`. Each `start`/`end` is one of:
  - `{special, port}`
  - `{track}`
  - `null`
- The controller uses this to place the Transfer spot on the operator loop, with no learning needed.

## 11. Lift chains, device parameters and full manual (API 6)
- Lift and transport devices keep their speed / acceleration / deceleration as doubles at dev+0x28 / +0x30 / +0x38. Device vtable +0x40 is `SetParams(speed, accel, decel)`; vtable +0x48 returns the current speed.
- `NLLiftDevice`+0x48 points to an `NLChainDevice` with its own copy of the parameters (+0x28/+0x30/+0x38), the current speed at +0x40 and an idle flag at +0x68. `SetParams` on the lift forwards to the chain.
- The chain update (vtable +0x28, 0x14013F9A0): target = +speed (state 1), -speed (state 2) or 0. With the idle flag set the target is halved and clamped to +/-0.4 m/s (below 0.2 it becomes 0). The current speed then ramps towards the target with accel/decel and the chain animation and sound are rescaled from current/speed.
  - So the idle speed is a constant in game code and can't be set on its own.
  - Setting the speed to 0 makes the target 0 in every mode (also idle), so the chain ramps down with its own deceleration and the train on it stops and holds (anti-rollback). Restoring the speed restarts it. This works in Auto block mode, which is how the bridge implements LIFT STOP / LIFT START without scripted operation.
- **Full Manual block mode disables the stations**: every station op fails with "No train stopped in the station" and dispatch with "Dispatch not available in full manual block mode". Controllers should run in Manual Block mode and drop into Full Manual only for direct device moves (brakes / transport / lift via 1150), then go back. Leaving Full Manual needs every train stopped and on one block, so retry while the game answers "All trains need to stop".

## 12. Seats, per-row restraints and custom trains (API 7/8)
- Car objects (0x130 bytes, `NLRollThing` base, created by make_shared; ctor 0x140491a10 / 0x140491670) hold: +0x08 model instances (0x38-byte entries, added by 0x140494720 from 0x1401d7fb0), +0x20 train, +0x28 animation groups, +0x40/+0x48 seat vector (8-byte entries), +0x58 restraint (harness) nodes. Trains list their cars at +0x468 and the steering gears at +0x450 (16-byte shared_ptr entries).
- The car rebuild function 0x140495140 clears +0x58, +0x40 and +0x28 and walks the car's model nodes: nodes named `HARNESS` become restraint nodes (per-row restraints) and nodes named `HEAD` become seats.
- Found from the ride-camera list (0x1403972f0), which formats "%s (Train %d, Car %d, Seat %d)" from the same seat vector. Helpers: 0x140497db0 total car count, 0x140497de0 count of cars with seats, 0x140499550 n-th seated car, 0x140499490 GetCar(n).
- The bridge sums car+0x40 sizes per train (`GetTrainSeats`). Front cars and locomotives with no `HEAD` node count 0. Parks that hide the NL2 train and draw their own with a script have empty model lists, so they read 0 seats and have no per-row restraints ("This train has no animated restraints"). CarRestraintAnim skips such cars too (`cmp [rcx+8],[rcx+10]`). API 8 reports a train whose cars all have an empty model list as `customTrain`.
