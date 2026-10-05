# Reusable in-game panel controller

Development work; not part of the deployed PLC/HMI revision.

The same NLVM controller is attached to each configured Scripted-operation
coaster. A generated ride profile supplies section names, routes, station parking
points, and switch guards. The profile contains no artwork or game assets.

## Required behavior

* No control lease: the in-game controller runs automatic blocks and station
  dispatch without a PC controller, DLL, or PLC connection.
* A lease applies only to the selected coaster. Read-only bridge connections do
  not acquire it. Panel Enable off while a lease exists means hold, not handback.
* Both physical Advance and Dispatch levels must stay asserted for station
  departure and the subsequent waiting-brake-to-station movement. An initial
  dispatch pulse is not enough. Releasing either removes powered movement.
* The same continuous hold can advance the next train once the station is
  physically clear. Entry/reconnection and mode changes still require release
  to arm; another dispatch after parking also requires a new dispatch cycle.
* Losing the lease stops a powered move first. Automatic handback requires
  stationary trains, aligned routes, and no emergency stop/fault. Reservations
  must survive handback and a train spanning two sections must not be mistaken
  for an empty block.
* Auto, Manual, and Transfer are controller modes. Changing modes clears held
  commands and requires a release before another movement.
* Manual/Transfer expose only devices actually fitted to the selected section.
  Routes must be aligned and the destination must be free before a move.
* A Block Check hold affects the selected block while the remaining circuit
  continues automatically. A pass requires an actual occupied stopped train.

## Integration boundary

The in-game script performs block decisions on the simulation frame. The PLC
supplies button levels, enabled/running/lift state, operating mode, selected
section, and a heartbeat. Existing gate/row/speed/reset commands remain device
operations, subject to controller ownership and station interlocks.

Do not let the old Python ScriptedBlockController run alongside this script.
There must be one writer of block outputs. Merely switching the editor's
operation setting to Scripted does not implement a block controller.

## Development park

The user has provided a stock copy named **Fury 325 - Scripted**. Inspection on
2026-10-05 confirmed it still used normal operation (`operationMode=0`), with
38 sections, 3 trains, and 1 station. Preserve the original Fury park.

The development copy now uses Scripted operation and runs this controller.
Station dispatch and exact parking points are still undergoing live acceptance.
The previous normal-mode station block-Advance call crashed the game; do not
repeat that call on a normal coaster.

The deployed Opta native PLC has only about 200 bytes of code space remaining.
Before adding its heartbeat integration, measure the complete native build and
reduce code usage or move appropriate work into the sketch. The PLC-to-laptop
connection also needs to be working for final hardware acceptance.

## Primary API references

* [Block callbacks](https://nolimitscoaster.com/nolimits2/help/api/com/nolimitscoaster/BlockSystemController.html)
* [Block state](https://nolimitscoaster.com/nolimits2/help/api/com/nolimitscoaster/Block.html)
* Installed game documentation: Section, Train, Coaster, and Script APIs.

## Verification status - 2026-10-05

This is development software, not the deployed PLC/HMI revision. NL2 compiles
and runs the controller on the three-train Fury development copy. Twenty
profile validation tests and 46 controller decision fixtures pass. Fixtures
execute the actual controller against mock APIs; they do not prove physics.
The bridge fixture verifies 3,200 status reads without pressing Advance.

Live acceptance recorded locally:

* Intentional native Manual Block Forward is accepted; status polling does not
  press it. Native Full Manual lift and brake controls work and Auto resumes.
* Dual held departure, stop on release, physical station clearance while the
  departure occupies transfer, and waiting-brake end parking work.
* Continuous holding advances and parks the next train without a release/repress.
* Optional multi-move starts before the departing rear clears the station,
  completes arrival without a fault, and stops both trains on release; holding
  again resumes and completes parking.
* An interrupted departure reverses while Return is held, stops when released,
  and returns to park when held again. The live final front position differed
  from the initial position by -0.594 m. Station park calibration remains
  ride-specific; do not treat that observation as a universal tolerance.
* Empty lift runs at native idle speed (0.4 m/s on Fury), obeys panel Lift On/Off,
  and idles again after the panel lease expires.
* An occupied lift stops under Block Hold, stays stopped when hold clears, and
  restarts only after Lift Start is released and pressed again.
* Forced selected missed-park training produces fault 908 and stops powered
  outputs after configured deceleration. Native coaster reset clears the fault.
* Forced stalled-flight training produces fault 907 after 70 simulation seconds;
  powered outputs stop and native reset clears the emergency stop.
* API 9 panel-session claims prevent a second bridge connection from writing
  coaster outputs while allowing spectator reads. Native Attraction Mode
  suppression is acknowledged while connected, and normal messages are restored
  after a missed heartbeat, explicit release, or TCP disconnect.

* All three storage routes complete station-to-table parking, guarded alignment,
  forward storage parking/lashing, reverse table return, main alignment and
  reverse station parking. Storage 1 and 2 entry also stops on Jog release and
  resumes on re-hold. Switch positions 0/1/2 correspond to section IDs 35/36/37.
* Manual control stops an unselected lift and drives the selected one. An
  occupied Fury lift runs at native idle (0.4 m/s), stops, restarts at its
  operating speed and accepts custom speeds. Repeated manual speed edits retain
  the original snapshot; Auto and a lost connection restore original parameters.
* An actual simulated crash stops NLVM frame callbacks. The independent bridge
  observer latches 909, asserts E-stop and preserves the fault mailbox despite
  continued panel inputs. Successful native reset clears the fault and restores
  Ready/Auto. The native crash Messages window still opens even with Attraction
  Mode acknowledged and a persistent native connection; hiding this particular
  window is not supported by the tested suppression command.

* An interrupted multi-move stops both trains after the panel lease expires,
  then hands back to automatic operation and parks the incoming train without
  a fault. Main-route alignment checks exclude inactive storage branches.
* Forced station overshoot latches fault 904 and stops powered outputs; reset
  restores operation. Three consecutive live operating cycles park without false
  stop faults with the development Fury profile's 2 m station stop tolerance.
  The cycle test includes a fresh operator Lift Start after a block stop clears.
* Manual station departure uses the main held pair to pull the entire train
  onto the feeder table and park it. A live check exposed an extra-jog requirement
  in this path; the corrected source passes both its regression and live test.

The Fury profile includes the circuit and three storage tracks.
No deployed PLC/HMI sends the development protocol yet. Four remote positions,
PLC message integration, the HMI fault-training page and timing history remain
integration work. Session arbitration applies to bridge connections; native game
controls and other connections to the separate stock telemetry server are outside
that arbitration.

## Physical versus logical station occupancy

The development profile uses separate Station and pass-through Transfer blocks.
Station frees when its physical section is empty. NL2 can still logically assign
a departing train on Transfer and an incoming train to Station. A guarded,
aligned reservation for those two identified trains is expected overlap; actual
unexpected multiple occupancy remains a latched fault. Stock multiple-train
warnings are disabled only during verified expected overlap, and restored otherwise.

Multi-move defaults off and is enabled by the panel packet. Its separation test
uses bogie geometry, closing speed and configured deceleration. This initial
implementation accepts a straight, collinear approach; curved approaches need a
track-distance sensor before they can be supported.

## Lift and fault behavior

Lift stops while panel-owned require a fresh Lift Start after blocking conditions
clear. Pressing while still blocked does not queue a restart. Without panel
ownership the controller restarts automatically. An empty enabled lift idles;
a commanded Lift Off, ride stop or fault overrides idle.

Fault states use 0x20010000 + code on the station block and latch game E-stop:
901 invalid profile, 902 unexpected occupancy, 904 calibrated stop overshoot,
905 failure to stop, 906 multi-move separation loss, 907 circuit timeout,
908 missed station park, 909 simulator crash/offline after the coaster was online.
A coasting train is stopped by the downstream brakes;
E-stop is not an instantaneous stop of every moving train.

The flight timeout is 70 simulation seconds from the lift release command to
physical arrival on the next block's section. Pausing the simulator does not
consume that time. Training is off by default. Random chances are per eligible
event and limited to 10%; the default configured chance is 1%. Trigger-next uses
the selected mask. Stalled-flight injection uses Train.setLashedToTrack after
leaving the lift; it simulates an immobilized train, not a physical valley.
Overshoot training requires a calibrated nonzero stop tolerance; missed-park
training requires a configured stop timeout. Neither setting is universal across rides.

## Bridge 1.2.2 correction

Scripted status queries previously called native operation 2, which presses
Forward. Status now reads scripted button visibility/enabled fields without any
native button call. Intentional Forward/Backward use operations 2/5 only after
checking those fields. Normal-mode availability queries remain operations 1/4.
Unknown section coordinates serialize as finite zero values, avoiding invalid
JSON from uninitialized storage-track coordinates. API version remains 8.

## Bridge 1.2.5 panel sessions, manual parameters and crash observer (API 10)

The development PanelLink requires controller signature revision 3 and claims a coaster before sending held inputs. Claims
expire after 750 ms without an input heartbeat. Release and disconnect restore
messages immediately through a background worker; output ownership retains its
750 ms grace period to match the script's safe handback.

By default PanelLink requests message suppression using the documented, licensed
[native telemetry Attraction Mode command](https://nolimitscoaster.com/nolimits2/help/pages/telemetry.html).
Enable the native telemetry server in NL2 Setup (default localhost port 15151).
This installation accepted the command. API 1154 reports requested/applied state
and any error. Failure is reported rather than bypassing license restrictions.

Attraction Mode affects the simulator globally while any requesting panel lease
is active; it is not scoped to one coaster. The baseline for this deployment is
normal mode (Attraction Mode off), and the watchdog restores that baseline when
the final requesting lease ends. The command's acknowledgement alone does not
prove that a particular crash dialog is hidden. Live testing confirmed that the
native crash Messages window still opens on this build. Suppressing messages
must never be treated as suppressing faults.

The bridge crash observer runs separately from NLVM under the game lock. For a
claimed panel coaster that was Ready/online, Ready=false plus Offline=0 for
250 ms latches fault 909. It exposes `panelFault` in coaster JSON and writes
`0x20010000 + 909` into the primary station block. Continued input heartbeats
cannot overwrite that fault. Motion writes and E-stop release are rejected until
reset; E-stop assertion remains available. A reset request suspends enforcement
while resetting, but the latch clears only when Ready/online returns. Coasters
that are initially offline or have no panel claim do not acquire a crash fault.

The native telemetry connection remains open while suppression is requested,
with an Idle heartbeat. The worker explicitly sends Attraction Mode off before
closing it. Native transport fixtures check persistent sockets, fragmented
replies, heartbeat framing, explicit restoration and refused requests.

Manual lift configuration selects native idle without starting the lift. Custom
lift/transport parameters changed in a claimed Manual/Transfer session are
temporary: original parameters are captured once and restored on Auto/Hold,
release, expired heartbeat, disconnect, or reset. The worker validates coaster
and device identity before restoring. Claims and mutation checks/writes are
serialized to prevent a different connection claiming ownership between a
permission check and its write. Speed snapshots and lease mode are observed
under the game lock so a stale mode snapshot cannot undo a new manual edit.

To configure another closed-circuit ride, copy `example_profile.json`, replace
section IDs/names and calibrate parking/timeouts, then run
`python scripted/build_profile.py ride.json output_folder`. Attach the generated
`PanelController.nl2script` to the ride configured for Scripted operation and
include all generated NLVM files. Reload the park after changing the profile;
a coaster reset alone does not reload compiled script sources.
