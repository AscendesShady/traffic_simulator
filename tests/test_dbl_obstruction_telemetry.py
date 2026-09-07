"""DBL-lane usability signals the LLM agent needs before enabling DBL.

The agent used to enable DBL blind. These tests cover the telemetry and
minimap fields that tell it whether a route's DBL lane can actually be
entered, so it can prefer TSP where the lane is blocked.
"""
import agent
import control_panel
from canvas_gemini import H_Y, LANE
from signal_controller import SignalController
from telemetry_exporter import TelemetryExporter
from vehicle import DBL_LANE_INDEX, Vehicle
from tests.helpers import make_bus_for_leg


ROUTE_ID = "R3_EB_ONLY"
NODE_A = 300


def lane_center_y(lane_index, direction="EB"):
    offset = (lane_index + 0.5) * LANE
    return H_Y - offset if direction == "EB" else H_Y + offset


def parked_blocker(bus, lane_index=DBL_LANE_INDEX, speed=0.0):
    blocker = Vehicle(
        x=bus.x,
        y=lane_center_y(lane_index, bus.direction),
        direction=bus.direction,
        max_speed=0.0,
        lane_index=lane_index,
    )
    blocker.speed = speed
    return blocker


def route_block(vehicles, tmp_path):
    controller = SignalController({"green_time": 20})
    exporter = TelemetryExporter(tmp_path / "state.json", 1)
    payload = exporter.build_payload(controller, vehicles, 60)
    return payload["routes"][ROUTE_ID]


def test_telemetry_reports_dbl_obstruction(tmp_path):
    control_panel.bus_routes_config[ROUTE_ID]["dbl_enabled"] = True

    bus = make_bus_for_leg(ROUTE_ID, NODE_A, "OBSTRUCTED_BUS")
    blocked = route_block([bus, parked_blocker(bus)], tmp_path)
    assert blocked["dbl_lane_obstructed"] is True
    assert blocked["nearest_bus_in_dbl_lane"] is False

    clear_bus = make_bus_for_leg(ROUTE_ID, NODE_A, "CLEAR_BUS")
    clear = route_block([clear_bus], tmp_path)
    assert clear["dbl_lane_obstructed"] is False

    # Traffic moving through the corridor clears on its own, so it is not
    # reported as an obstruction.
    moving_bus = make_bus_for_leg(ROUTE_ID, NODE_A, "MOVING_BLOCKER_BUS")
    moving = route_block(
        [moving_bus, parked_blocker(moving_bus, speed=1.0)], tmp_path
    )
    assert moving["dbl_lane_obstructed"] is False

    # A bus that already reached the DBL lane needs no merge.
    merged_bus = make_bus_for_leg(ROUTE_ID, NODE_A, "MERGED_BUS")
    merged_bus.lane_index = DBL_LANE_INDEX
    merged_bus.y = lane_center_y(DBL_LANE_INDEX)
    merged = route_block([merged_bus, parked_blocker(merged_bus)], tmp_path)
    assert merged["dbl_lane_obstructed"] is False
    assert merged["nearest_bus_in_dbl_lane"] is True


def test_dbl_obstruction_false_when_no_bus(tmp_path):
    block = route_block([], tmp_path)

    assert block["dbl_lane_obstructed"] is False
    assert block["nearest_bus_in_dbl_lane"] is False
    assert block["nearest_bus_id"] is None


def test_minimap_surfaces_dbl_usability():
    telemetry = {
        "simulation_time_seconds": 10.0,
        "routes": {
            route_id: {
                "active": True,
                "tsp_enabled": False,
                "dbl_enabled": route_id == "R1_EB_A_NB",
                "dbl_lane_obstructed": route_id == "R1_EB_A_NB",
                "nearest_bus_in_dbl_lane": False,
            }
            for route_id in agent.guard.ROUTE_ORDER
        },
        "buses": [
            {
                "bus_id": "BLOCKED_DBL_BUS",
                "route_id": "R1_EB_A_NB",
                "direction": "EB",
                "route_leg": {"node_x": 300, "movement": "LEFT"},
                "leg_state": "APPROACHING",
                "distance_to_stop_bar_px": 120.0,
                "eta_to_stop_bar_sec_freeflow": 2.0,
                "passengers": 45,
                "priority_granted": False,
            }
        ],
    }

    minimap = agent.read_minimap({"telemetry": telemetry})["minimap"]
    lines = {
        line.split(":")[0].split(") ")[-1]: line
        for line in minimap.splitlines()
        if line.startswith(tuple("123456"))
    }

    blocked_line = lines["R1_EB_A_NB"]
    assert "dbl=True" in blocked_line
    assert "dbl_lane_obstructed=True" in blocked_line
    assert "nearest_bus_in_dbl_lane=False" in blocked_line

    # Routes without an approaching bus still report the fields.
    quiet_line = lines["R2_EB_B_NB"]
    assert "dbl_lane_obstructed=False" in quiet_line
    assert "nearest_bus_in_dbl_lane=False" in quiet_line
    assert "none approaching" in quiet_line


def test_prompt_teaches_dbl_obstruction_policy():
    assert "dbl_lane_obstructed=true" in agent.SYSTEM_PROMPT
    assert "nearest_bus_in_dbl_lane=false" in agent.SYSTEM_PROMPT


def test_no_hard_coded_dbl_refusal_in_guard():
    """DBL usability stays a model policy call, not a coded veto."""
    import guard

    guard_source = open(guard.__file__, encoding="utf-8").read()
    assert "dbl_lane_obstructed" not in guard_source

    flags = guard.validate_flags_positional(
        {
            "reason": "test",
            "tsp": [False] * len(guard.ROUTE_ORDER),
            "dbl": [True] * len(guard.ROUTE_ORDER),
        }
    )
    # The guard still accepts DBL on every route: only the model decides
    # whether an obstructed lane is worth enabling.
    assert flags == {
        route_id: {"tsp": False, "dbl": True} for route_id in guard.ROUTE_ORDER
    }
