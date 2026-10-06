# PLC scripted panel sessions

## Reply types

Clients must match the reply ID for each request. A successful command is not a text response.

| Request | Meaning | Successful reply |
| --- | --- | --- |
| 1153 | Claim/release scripted panel session | `R_OK` (1) |
| 1154 | Read session status | `R_STRING` (10), JSON |
| 1155 | Prepare reconnect ticket | `R_STRING` (10), JSON |
| 1156 | Resume session using ticket | `R_OK` (1) |

On 2026-10-06, the Opta integration incorrectly expected reply 10 for requests
1153 and 1156. The bridge accepted the command, but the PLC treated its successful
acknowledgement as failure, closed the connection, and displayed
"Scripted panel input failed". Both calls in the PLC companion sketch were
corrected to expect reply 1. The bridge protocol and DLL needed no change.

The game installation was also updated from bridge 1.2.0 / API 8 to
1.2.7 / API 12, with the matching injector. The installed DLL hash matched
the distribution build. The running bridge subsequently reported API 12.

## Verification

- Native PLC sketch compilation and download succeeded; application build had
  zero errors (three warnings).
- Host regression checks passed for claim/release acknowledgements, existing
  topology parsers, shared-buffer bounds, controller discovery, held-input
  encoding, and scripted lift requests.
- Generated Ethernet/runtime initialization was verified to execute once in
  `setup()`.
- With scripted Fury and the physical control power key enabled, the deployed
  PLC reported connected state 4, scripted detection 1, controller detection 1,
  and connection fault 0.
- The bridge reported an active panel session with no error. Its log confirmed
  periodic socket reconnects and restoration of the session.

This verification covers connection and reconnect behavior; it does not certify
physical ride controls or repeat the full dispatch/transfer test suite.

## PLC console exercise — October 6, 2026

An authorized temporary NL2-only input harness drove the deployed PLC control logic. A changing heartbeat expired after two seconds; control power stayed physically wired. Simulated E-stop/lockout conditions could only add a stop. The harness used the retired manual-command words, never pulsed legacy Execute, and cleared its words when finished. Production source has no input aliases or overrides.

Observed in the simulator through read-only API 12 telemetry:

| Check | Observed result |
| --- | --- |
| Daily console test and full startup sequence | Completed through PLC; startup complete and ride-running feedback became true. |
| Gates | PLC button opened and closed station gates. |
| Lift start and manual lift settings | On/off feedback changed; idle, design, custom and off commands accepted. |
| Device availability | Lift exposes lift capability, with no brake or tire capability; unavailable brake request rejected. |
| Four remote operators | Main held buttons caused no movement with no remotes held. All four enabled dispatch; releasing one stopped the next train's advance. |
| Continuous dispatch/advance hold | Next train began advancing without releasing/repressing the main buttons. |
| Aborted dispatch/reversing | Two-second dispatch, release and held restraints returned the train to the station park position; front position approximately 8044.4. |
| Transfer requests | Switch accepted; jog command accepted; release issued brakes-closed stop. Loaded-train shunt was not verified in this exercise. |
| Guided block test | Station brake request was refused. A successful test on a nonstation occupied brake remains to be checked. |
| Input expiration/cleanup | Physical packed input value 33025 restored; no temporary buttons left held; ride stopped. |

The exercise exposed controller fault 905 reaching the PLC without latching its fault indicator. Production now latches scripted fault codes 901–909, preserves their numbers, and requires a fresh maintenance-key ACK and confirmed connected reset before clearing. Crash code 99 retains its existing path. Corrective text instructs personnel to locate trains and inspect brakes, park sensors and block routes before reset.

Reversing uses reserved PLC presentation station state 255 and transfer text `Train reversing`. It is derived only from an identified panel controller with user state 8. Raw game telemetry is not changed. A native C++ harness verifies the warning clears when reversing ends and that fault 905 propagates. The production PLC compiles within the unchanged 0x1E400 code area. The obsolete unchecked raw-command execution path is retired; current HMI manual/transfer requests retain their existing interlock checks.

A local Modbus service stopped accepting new clients twice and recovered after an authorized target reboot. The operator test used a single synchronized connection afterward. This is an observed network-service issue, not proof of a PLC CPU freeze.
