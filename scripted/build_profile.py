"""Generate per-ride NLVM setup without embedding park assets in the repository."""
import argparse
import json
import math
from pathlib import Path

KINDS = {"station": 0, "lift": 1, "brake": 2, "storage": 3, "transfer": 4, "pass_through": 5}


def validate(profile):
    blocks = profile["blocks"]
    if not 2 <= len(blocks) <= 64:
        raise ValueError("The profile must contain 2..64 blocks")
    names = [b["name"] for b in blocks]
    ids = [b["id"] for b in blocks]
    if len(set(names)) != len(names) or len(set(ids)) != len(ids):
        raise ValueError("Block names and section IDs must be unique")
    if sum(b["type"] == "station" for b in blocks) != 1:
        raise ValueError("This first engine version requires exactly one station")
    for b in blocks:
        if b["type"] not in KINDS or not isinstance(b["id"], int) or not 1 <= b["id"] <= 4095:
            raise ValueError("Invalid block kind or section ID")
        if b.get("physical_clear", False) and b["type"] not in ("station", "pass_through"):
            raise ValueError("Physical-only clearance is restricted to station/pass-through blocks")
        if b.get("return_to_park", False) and (b["type"] != "station" or not b.get("physical_clear", False)):
            raise ValueError("Return to park requires a station with physical clearance")
        distance = b.get("park_distance", 1.0)
        if not isinstance(distance, (int, float)) or not math.isfinite(distance) or distance < 0:
            raise ValueError("Parking distances must be finite and nonnegative")
        timeout = b.get("stop_timeout_ms", 0)
        tolerance = b.get("stop_tolerance", 0)
        if not isinstance(timeout, int) or timeout < 0 or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or tolerance < 0 or (tolerance > 0 and tolerance >= distance):
            raise ValueError("Invalid stop monitor timeout/tolerance")
    auxiliary = [name for b in blocks for name in b.get("auxiliary", [])]
    if len(set(auxiliary)) != len(auxiliary) or set(auxiliary) & set(names):
        raise ValueError("An auxiliary section must have exactly one owner and cannot be another block")
    for b in blocks:
        if len(b.get("auxiliary", [])) > 8:
            raise ValueError("At most eight auxiliary sections per block")
        if b.get("park_section", b["name"]) not in [b["name"], *b.get("auxiliary", [])]:
            raise ValueError("A parking section must belong to its station block group")
    if profile["before_station"] not in names:
        raise ValueError("The before-station block is missing")
    routes = profile["routes"]
    if not 1 <= len(routes) <= 192:
        raise ValueError("The profile must contain 1..192 routes")
    seen = set()
    for r in routes:
        if set(r) - {"from", "to", "reverse", "switch"}:
            raise ValueError("Unknown route field; direction must use reverse")
        if not isinstance(r.get("reverse", False), bool):
            raise ValueError("Route reverse must be a boolean")
        if r["from"] not in names or r["to"] not in names or r["from"] == r["to"]:
            raise ValueError("A route references a missing block or itself")
        guard = r.get("switch")
        if guard and (not isinstance(guard["position"], int) or guard["position"] < 0):
            raise ValueError("Invalid switch position")
        signature = (r["from"], r.get("reverse", False), guard["name"] if guard else None,
                     guard["position"] if guard else None)
        if signature in seen:
            raise ValueError("Ambiguous routes share the same source/direction/switch setting")
        seen.add(signature)
    station = next(b["name"] for b in blocks if b["type"] == "station")
    if not any(r["from"] == profile["before_station"] and r["to"] == station and
               not r.get("reverse", False) for r in routes):
        raise ValueError("The before-station block needs a forward station route")
    timeout = profile.get("lift_to_block_timeout", 70)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Invalid circuit timeout")
    multi = profile.get("multi_move")
    if multi:
        if not isinstance(multi.get("minimum_gap"), (int, float)) or not math.isfinite(multi["minimum_gap"]) or multi["minimum_gap"] < 1 or not isinstance(multi.get("brake_deceleration"), (int, float)) or not math.isfinite(multi["brake_deceleration"]) or multi["brake_deceleration"] <= 0:
            raise ValueError("Invalid multi-move separation/deceleration")
        if not next(b for b in blocks if b["name"] == station).get("physical_clear", False):
            raise ValueError("Multi-move requires physical station clearance")
    return names


def generate(profile):
    names = validate(profile)
    lines = ["// Generated per-ride data; controller logic is shared across ride profiles.",
             "public class RideProfile", "{", "  public static void configure(PanelController c)", "  {"]
    for index, b in enumerate(profile["blocks"]):
        lines.append(f'    int b{index} = c.addBlock({json.dumps(b["name"])}, '
                     f'{KINDS[b["type"]]}, {b["id"]}, {float(b.get("park_distance", 1.0))}f);')
    for r in profile["routes"]:
        guard = r.get("switch")
        switch = json.dumps(guard["name"]) if guard else "null"
        direction = guard["position"] if guard else 0
        reverse = "true" if r.get("reverse", False) else "false"
        lines.append(f'    c.addRoute(b{names.index(r["from"])}, b{names.index(r["to"])}, '
                     f'{reverse}, {switch}, {direction});')
    for index, b in enumerate(profile["blocks"]):
        if "stop_timeout_ms" in b or "stop_tolerance" in b:
            lines.append(f'    c.setStopMonitor(b{index}, {b.get("stop_timeout_ms", 0)}, {float(b.get("stop_tolerance", 0))}f);')
        if b.get("physical_clear", False):
            lines.append(f'    c.setPhysicalClear(b{index});')
        if b.get("return_to_park", False):
            lines.append(f'    c.setReturnToPark(b{index});')
        for name in b.get("auxiliary", []):
            lines.append(f'    c.addAuxiliary(b{index}, {json.dumps(name)});')
        if "park_section" in b:
            lines.append(f'    c.setParkingSection(b{index}, {json.dumps(b["park_section"])});')
    lines.append(f'    c.setFlightTimeout({float(profile.get("lift_to_block_timeout", 70))}f);')
    if profile.get("multi_move"):
        m = profile["multi_move"]
        lines.append(f'    c.setMultiMove({float(m["minimum_gap"])}f, {float(m["brake_deceleration"])}f);')
    lines.extend([f'    c.setBeforeStation(b{names.index(profile["before_station"])});', "  }", "}", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    output = generate(profile)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "RideProfile.nlvm").write_text(output, encoding="utf-8")
    for name in ("PanelBlock.nlvm", "PanelRoute.nlvm", "PanelController.nlvm"):
        source = Path(__file__).with_name(name)
        (args.output / name).write_bytes(source.read_bytes())
    (args.output / "PanelController.nl2script").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<root><script>\n'
        '<description>Reusable panel block controller (development)</description>\n'
        '<class>PanelController</class>\n</script></root>\n', encoding="utf-8")
    print(f"Generated {len(profile['blocks'])} blocks / {len(profile['routes'])} routes in {args.output}")


if __name__ == "__main__":
    main()
