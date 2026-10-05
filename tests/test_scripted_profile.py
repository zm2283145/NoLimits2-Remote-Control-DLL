"""Reject profiles that could accidentally route into the wrong block."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("build_profile", Path(__file__).parents[1] / "scripted/build_profile.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def circuit():
    return {"before_station": "Brake", "blocks": [
        {"name": "Station", "id": 8, "type": "station"},
        {"name": "Lift", "id": 19, "type": "lift"},
        {"name": "Brake", "id": 4, "type": "brake"}], "routes": [
        {"from": "Station", "to": "Lift"}, {"from": "Lift", "to": "Brake"},
        {"from": "Brake", "to": "Station", "switch": {"name": "Table", "position": 3}}]}


class ProfileTests(unittest.TestCase):
    def rejects(self, edit):
        profile = circuit()
        edit(profile)
        with self.assertRaises(ValueError):
            builder.generate(profile)

    def test_arbitrary_names_ids_order_and_switch(self):
        p = circuit()
        p["blocks"].reverse()
        output = builder.generate(p)
        self.assertIn('c.addBlock("Lift", 1, 19, 1.0f)', output)
        self.assertIn('c.addRoute(b0, b2, false, "Table", 3)', output)
        self.assertNotIn("Fury", output)

    def test_duplicate_name(self):
        self.rejects(lambda p: p["blocks"][1].update(name="Station"))

    def test_duplicate_id(self):
        self.rejects(lambda p: p["blocks"][1].update(id=8))

    def test_misspelled_reverse_is_rejected(self):
        self.rejects(lambda p: p["routes"][0].update(backwards=True))

    def test_route_reverse_cannot_be_text(self):
        self.rejects(lambda p: p["routes"][0].update(reverse="false"))

    def test_missing_destination(self):
        self.rejects(lambda p: p["routes"][0].update(to="Missing"))

    def test_ambiguous_branch(self):
        self.rejects(lambda p: p["routes"].append({"from": "Station", "to": "Brake"}))

    def test_negative_switch_position(self):
        self.rejects(lambda p: p["routes"][-1]["switch"].update(position=-1))

    def test_no_station_feed(self):
        self.rejects(lambda p: p["routes"].pop())

    def test_nonfinite_parking(self):
        self.rejects(lambda p: p["blocks"][0].update(park_distance=float("nan")))

    def test_unrepresentable_section_id(self):
        self.rejects(lambda p: p["blocks"][0].update(id=4096))

    def test_unsupported_multiple_stations(self):
        self.rejects(lambda p: p["blocks"][1].update(type="station"))

    def test_return_requires_physical_station(self):
        self.rejects(lambda p: p["blocks"][0].update(return_to_park=True))

    def test_lift_cannot_discard_logical_occupancy(self):
        self.rejects(lambda p: p["blocks"][1].update(physical_clear=True))

    def test_stop_tolerance_must_fit_parking_margin(self):
        self.rejects(lambda p: p["blocks"][0].update(park_distance=1, stop_tolerance=1))

    def test_negative_stop_timeout(self):
        self.rejects(lambda p: p["blocks"][0].update(stop_timeout_ms=-1))

    def test_circuit_timeout_must_be_finite_positive(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.rejects(lambda p: p.update(lift_to_block_timeout=value))

    def test_multi_move_requires_physical_station(self):
        self.rejects(lambda p: p.update(multi_move={"minimum_gap": 12, "brake_deceleration": 2}))

    def test_multi_move_rejects_invalid_separation(self):
        for gap, decel in ((0, 2), (12, 0), (float("nan"), 2), (12, float("inf"))):
            with self.subTest(gap=gap, decel=decel):
                self.rejects(lambda p: (p["blocks"][0].update(physical_clear=True), p.update(multi_move={"minimum_gap": gap, "brake_deceleration": decel})))

    def test_valid_return_and_multi_move_are_generated(self):
        p = circuit()
        p["blocks"][0].update(physical_clear=True, return_to_park=True, park_distance=3, stop_timeout_ms=6000, stop_tolerance=.5)
        p.update(multi_move={"minimum_gap": 12, "brake_deceleration": 2}, lift_to_block_timeout=70)
        output = builder.generate(p)
        for expected in ("c.setPhysicalClear(b0)", "c.setReturnToPark(b0)", "c.setStopMonitor(b0, 6000, 0.5f)", "c.setMultiMove(12.0f, 2.0f)", "c.setFlightTimeout(70.0f)"):
            self.assertIn(expected, output)


if __name__ == "__main__":
    unittest.main()
