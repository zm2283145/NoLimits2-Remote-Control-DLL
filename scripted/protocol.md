# Panel input mailbox (development)

This protocol is experimental. The deployed PLC does not send it yet.

The primary station block is an input mailbox **only between a bridge write and
the next script frame**. Controller state lives in PanelBlock.state, so consuming
an input cannot destroy a block reservation. The frame immediately restores its
display state. No raw section command or game block-mode change is used to take
ownership.

Protocol 3 payload: `(sectionId << 13) | (mode << 11) | flags`.

| Field | Meaning |
|---|---|
| sectionId | Selected section ID, 1..4095; translated using the generated profile |
| mode | 0 Auto, 1 Manual, 2 Transfer; 3 stops the controller |
| flags bit 0 | Panel Enable |
| flags bit 1 | Ride Running |
| flags bit 2 | Advance held |
| flags bit 3 | Dispatch held |
| flags bit 4 | Lift command; fresh rising edge required after a panel-owned block/setup stop |
| flags bit 5 | Selected-section jog held |
| flags bit 6 | Reverse jog |
| flags bit 7 | Block Check hold on the selected section |
| flags bit 8 | Departure permitted, including all independent row restraints |
| flags bit 9 | Enable optional multi-move |
| flags bit 10 | Return-to-park held (restraints button; not an open-restraints command) |

Mailbox state: `0x40000000 + payload`. Register it through bridge 1112, then
write it through 1113. Claim an API 9 panel session first. The bridge rejects
competing writers while the claim is active; a production integration must also
serialize its own writes and disable competing output controllers.

The script's idle signature is station state `0x20000003`. It must be observed
before attaching a development client; never send this protocol to another
script merely because the coaster uses Scripted operation. The version change
prevents a older client from silently using the new bit layout. Compute
departure permission from live station/row interlocks; do not simply set it true.

Send input levels at least every 200 ms. Every consumed packet refreshes a
750 ms wall-clock lease; simulation pause does not extend it. At expiry the
script stops powered devices. Automatic handback waits at least one second,
stationary trains, aligned circuit routes, and no emergency stop. It retains
reservations and does not move transfer tables or clear emergency stops.

Known limits before production: switch-aware transfer routes and station parking
distances need live calibration; multiple stations are rejected by the profile
builder; cruise/launch profiles and disconnected-mode stock wait-time behavior
need separate acceptance. The generic engine is intended to support configured
closed circuits first. It must not be advertised as safe for arbitrary rides.

A panel-owned lift stopped by a setup/global hold, selected block hold, occupied
destination, or lost route alignment latches a restart requirement. Keep the
command off until the condition clears, then issue a fresh Lift Start edge.
Leaving a latched run request on must not restart it. A start pressed while the
condition remains blocked is not queued. Without panel ownership this latch is
cleared and the lift resumes automatically when the next block clears.
The PLC/HMI must reflect the stopped state and generate this fresh command;
that integration is still pending.


## Training configuration

A separate registered station state `0x50000000 + configuration` is consumed
while the panel owns the coaster. Bits 0..2 select overshoot, missed park and
stalled flight. Bits 3..12 contain chance per thousand (0..100, clamped to 100).
Bit 13 enables training; bit 14 arms the selected faults for the next eligible
event. Trigger bits are consumed once per selected fault; chance is evaluated
per eligible event, never every frame. Lease expiry disables new injections.
The training packet requires Panel Enable, an existing lease, and no fatal fault.
It refreshes that lease but does not replace the current operating input levels.

Fault display state is `0x20010000 + faultCode`; codes are documented in README.
The normal display may briefly contain a mailbox input after a write and before
the next simulation frame. Status clients must recognize these ranges and retain
the last valid controller display instead of treating a transient packet as a
block state or fault. Production should serialize packets and read after frame
consumption.

## Bridge connection ownership (API 9)

API 10 / bridge 1.2.5 requires signature revision 3 for new claims. The main
Protocol 3 input bit layout remains unchanged; revision 3 adds the manual-lift
mailbox below and restoration of temporary manual device parameters.

Message 1153 takes five big-endian signed 32-bit integers:
`coasterIndex, stationSectionId, enabled, suppressMessages, nativeTelemetryPort`.
An enabled claim requires Scripted operation, a real station section, and the
controller's idle signature. Only one live connection may own a coaster. Read
queries remain available to spectators, and emergency-stop assertion remains
available to every connection. Emergency-stop release and device/mode/reset
writes require ownership while a claim is active.

Held-input and training packets renew a 750 ms claim. A changed coaster object
or station section requires a new claim. Disable with 1153 `enabled=0`, using
the owning connection; send an off input frame first. A disable or TCP disconnect
stops requesting message suppression immediately, while ownership retains its
remaining 750 ms grace period for the script's safe handback. A unique client ID
prevents a reused socket handle from inheriting an old connection's ownership.

Message 1154 has no payload and returns JSON:
`{requested, active, nativePort, error}`. Suppression uses native telemetry
Attraction Mode, normally on localhost port 15151. It requires a supporting NL2
license and the native telemetry server to be enabled. This mode is global to
the simulator; the worker restores normal messages after the final requesting
lease ends. The server does not patch license flags or internal message windows.
Check `active` and `error`; a claim itself is not proof that suppression succeeded.

## Manual lift configuration (signature revision 3 / API 10)

Register and write station state `0x51000000 + (liftSectionId << 2) + idle`,
where `idle` is 0 for the operating speed or 1 for native idle. The script
consumes it only during an existing panel lease and only for a configured lift.
It renews the lease without replacing the current button levels. The packet
does not start the lift: continue sending the selected lift's held command.
Only chain lifts may support native idle; the NL2 SDK turns wheels/LIMs off in
that mode. Selecting a non-lift must hide lift controls in the panel.

Mode changes or lease loss clear manual idle selection. Custom/high operating
speeds use device-parameter command 1152 while the claimed panel's mode is
Manual or Transfer. The bridge captures original speed/acceleration/deceleration
once per changed device. Returning to Auto/Hold, release, lease expiry, TCP
disconnect, or reset restores them. Repeated changes do not replace the original
snapshot. A changed coaster or device object is never restored through a stale
pointer. Legacy unclaimed calls keep their previous API behavior.

## Crash fault while NLVM is offline

The script alone cannot report a real NL2 crash because frame callbacks stop.
The independent bridge observer exposes `panelFault=909` in coaster JSON and
station state `0x20010000 + 909`, asserts E-stop and stops devices. Heartbeats
remain accepted so the client can keep reading the latched fault, but they do
not overwrite it. Movement and E-stop release require a successful reset first.
The bridge identifies a sustained Ready=false/Offline=0 transition after the
panel's coaster was Ready/online. Reset clears the latch only after Ready/online
returns. The native crash Messages window still appears on the tested NL2
build despite Attraction Mode; its visibility is separate from fault reporting.
