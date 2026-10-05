"""Development transport for the NLVM mailbox; never run with the old PC engine."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from nl2bridge import NL2Error

SIGNATURE = 0x20000004
INPUT_BASE = 0x40000000


class PanelLink:
    def __init__(self, bridge, coaster, station_section, *, suppress_messages=True, telemetry_port=15151):
        self.bridge = bridge
        self.coaster = coaster
        self.station = station_section
        info, sections = bridge.section_detail(coaster)
        station = next((s for s in sections if s["id"] == station_section), None)
        if not info["scripted"] or not station or station["userState"] != SIGNATURE:
            raise NL2Error("Reusable in-game controller signature is not present")
        if bridge.bridge_info().get("api", 0) < 11:
            raise NL2Error("This controller requires NL2Bridge API 11 or newer")
        bridge.panel_session(coaster, station_section, suppress_messages=suppress_messages,
                             telemetry_port=telemetry_port)
        self.registered = set()
        self.released = False

    def send(self, *, selected, mode=0, enabled=False, running=False,
             advance=False, dispatch=False, lift=False, jog=False,
             reverse=False, block_hold=False, return_to_park=False, multi_move=False):
        if self.released:
            raise NL2Error("Panel session was released; reconnect before sending")
        if not isinstance(selected, int) or not 1 <= selected <= 4095 or mode not in (0, 1, 2):
            raise ValueError("Invalid selected section or operating mode")
        flags = sum((1 << index) for index, on in enumerate(
            (enabled, running, advance, dispatch, lift, jog, reverse, block_hold)) if on)
        # NLVM's native station predicate does not see the bridge's independent
        # row restraints. Include those interlocks before asking for departure.
        station = self.bridge.station_states(self.coaster)[0]
        if station["canDispatch"] and not station["rowsOpen"] and not station["estop"]:
            flags |= 256
        if multi_move:
            flags |= 512
        if return_to_park:
            flags |= 1024
        state = INPUT_BASE + (selected << 13) + (mode << 11) + flags
        if state not in self.registered:
            self.bridge.register_state(self.coaster, self.station, state, "Panel input", "off")
            self.registered.add(state)
        self.bridge.set_state(self.coaster, self.station, state)

    def release(self, selected):
        # Explicit off frame before stopping heartbeats. The engine remains
        # panel-owned/held until its lease expires, then evaluates handback.
        if self.released:
            return
        try:
            self.send(selected=selected)
        finally:
            self.bridge.panel_session(self.coaster, self.station, enabled=False)
            self.released = True

    def prepare_reconnect(self):
        if self.released:
            raise NL2Error("Panel session was released; reconnect before sending")
        if self.bridge.bridge_info().get('api', 0) < 12:
            raise NL2Error('Planned connection recycling requires API 12')
        return self.bridge.panel_reconnect_ticket(self.coaster)

    def resume_on(self, bridge, ticket):
        """Switch to a new socket using a ticket, then immediately send inputs.

        This preserves the in-game held levels and temporary speed snapshot.
        It never extends the input lease; slow/failed reconnects stop normally.
        """
        if self.released:
            raise NL2Error("Released sessions cannot resume")
        bridge.panel_resume(self.coaster, self.station, ticket)
        self.bridge = bridge

    def configure_training(self, *, enabled=False, faults=0, chance_per_thousand=10, trigger_now=False):
        if self.released:
            raise NL2Error("Panel session was released; reconnect before sending")
        if faults not in range(8) or not isinstance(chance_per_thousand, int) or not 0 <= chance_per_thousand <= 100:
            raise ValueError("Training mask must be 0..7 and chance 0..100 per thousand")
        state = 0x50000000 + faults + (chance_per_thousand << 3) + (8192 if enabled else 0) + (16384 if trigger_now else 0)
        if state not in self.registered:
            self.bridge.register_state(self.coaster, self.station, state, "Training configuration", "off")
            self.registered.add(state)
        self.bridge.set_state(self.coaster, self.station, state)

    def configure_lift(self, section, *, idle=False):
        """Select native idle or operating speed for a fitted lift in Manual/Transfer.

        This does not start the lift; continue sending its held Lift command.
        Use set_device_params for a temporary custom operating speed. API 10
        restores those parameters when returning to Auto or losing the lease.
        """
        if self.released:
            raise NL2Error("Panel session was released; reconnect before sending")
        if not isinstance(section,int) or not 1 <= section <= 4095:
            raise ValueError("Invalid lift section ID")
        if not self.bridge.device_params(self.coaster,section)['lift']:
            raise NL2Error("Selected section has no lift")
        state=0x51000000+(section<<2)+int(bool(idle))
        if state not in self.registered:
            self.bridge.register_state(self.coaster,self.station,state,"Manual lift configuration","off")
            self.registered.add(state)
        self.bridge.set_state(self.coaster,self.station,state)

    def configure_brake(self, section, *, open=None):
        """Manual brake selection; None restores automatic brake control.

        A parked train is released only with an aligned, free destination.
        Tires still require the held jog command. Station brakes belong to
        dispatch/parking controls and cannot be opened with this command.
        """
        if self.released:
            raise NL2Error("Panel session was released; reconnect before sending")
        if not isinstance(section, int) or not 1 <= section <= 4095:
            raise ValueError("Invalid brake section ID")
        _, sections = self.bridge.section_detail(self.coaster)
        fitted = next((s for s in sections if s['id'] == section), None)
        if not fitted or fitted['isStation'] or not self.bridge.devices(self.coaster, section)['brake']['present']:
            raise NL2Error("Selected section has no independently controlled brake")
        state = 0x52000000 + (section << 2) + (0 if open is None else 2 if open else 1)
        if state not in self.registered:
            self.bridge.register_state(self.coaster, self.station, state, "Manual brake configuration", "off")
            self.registered.add(state)
        self.bridge.set_state(self.coaster, self.station, state)
