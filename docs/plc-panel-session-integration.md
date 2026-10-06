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
