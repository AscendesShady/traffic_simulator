"""DBL queue/merge usability gates model and controller activation.

The agent used to enable DBL blind. These tests cover the telemetry and
minimap fields that tell it whether a route's DBL lane can actually be
entered, so it can prefer TSP where the lane is blocked.
"""
import json

import src.agents.agent as agent
import src.ui.control_panel as control_panel
from src.ui.canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from src.core.signal_controller import SignalController
from src.telemetry.telemetry_exporter import TelemetryExporter
from src.core.vehicle import DBL_LANE_INDEX, Vehicle
from tests.helpers import make_bus_for_leg


ROUTE_ID = "R3_EB_ONLY"
NODE_A = 300


def lane_center_y(lane_index, direction="EB"):
    offset = (lane_index + 0.5) * LANE
    return H_Y - offset if direction == "EB" else H_Y + offset


def parked_blocker(bus, lane_index=DBL_LANE_INDEX, speed=0.0, x_offset=0.0):
    blocker = Vehicle(
        x=bus.x + x_offset if bus.direction == "EB" else bus.x - x_offset,
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
    assert blocked["dbl_lane_queue_ahead"] == 0
    assert blocked["nearest_bus_in_dbl_lane"] is False

    clear_bus = make_bus_for_leg(ROUTE_ID, NODE_A, "CLEAR_BUS")
    clear = route_block([clear_bus], tmp_path)
    assert clear["dbl_lane_obstructed"] is False
    assert clear["dbl_lane_queue_ahead"] == 0

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

    # Being in lane 2 does not hide a stopped queue ahead. This was the defect
    # that let DBL activate while its bus could only sit in ordinary traffic.
    queued_bus = make_bus_for_leg(ROUTE_ID, NODE_A, "QUEUED_BUS")
    queued_bus.lane_index = DBL_LANE_INDEX
    queued_bus.y = lane_center_y(DBL_LANE_INDEX)
    queued_bus.x -= 160.0
    queued = route_block(
        [queued_bus, parked_blocker(queued_bus, x_offset=60.0)], tmp_path
    )
    assert queued["dbl_lane_obstructed"] is True
    assert queued["dbl_lane_queue_ahead"] == 1
    assert queued["nearest_bus_in_dbl_lane"] is True


def test_dbl_obstruction_false_when_no_bus(tmp_path):
    block = route_block([], tmp_path)

    assert block["dbl_lane_obstructed"] is False
    assert block["dbl_lane_queue_ahead"] == 0
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
                "dbl_lane_queue_ahead": 2 if route_id == "R1_EB_A_NB" else 0,
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
    assert "dbl_lane_queue_ahead=2" in blocked_line
    assert "nearest_bus_in_dbl_lane=False" in blocked_line

    # Routes without an approaching bus still report the fields.
    quiet_line = lines["R2_EB_B_NB"]
    assert "dbl_lane_obstructed=False" in quiet_line
    assert "dbl_lane_queue_ahead=0" in quiet_line
    assert "nearest_bus_in_dbl_lane=False" in quiet_line
    assert "none approaching" in quiet_line


def test_prompt_teaches_dbl_obstruction_policy():
    assert "dbl_lane_obstructed=true" in agent.SYSTEM_PROMPT
    assert "dbl_lane_queue_ahead is greater than 0" in agent.SYSTEM_PROMPT
    assert "nearest_bus_in_dbl_lane=false" in agent.SYSTEM_PROMPT
    assert "ALL other vehicles in that lane must clear it immediately" in agent.SYSTEM_PROMPT
    assert "clearance rule is unconditional" in agent.SYSTEM_PROMPT


def test_model_dbl_is_deterministically_vetoed_for_queue_ahead():
    route_id = "R3_EB_ONLY"
    output = {
        "reason": "Enable DBL for the approaching bus.",
        "tsp": [False] * len(agent.guard.ROUTE_ORDER),
        "dbl": [route == route_id for route in agent.guard.ROUTE_ORDER],
    }
    routes = {
        route: {
            "dbl_enabled": False,
            # Count alone must be enough for the deterministic safety veto;
            # do not depend on two telemetry fields agreeing perfectly.
            "dbl_lane_obstructed": False,
            "dbl_lane_queue_ahead": 1 if route == route_id else 0,
        }
        for route in agent.guard.ROUTE_ORDER
    }

    guarded = agent.anti_cheat(
        {
            "raw_output": json.dumps(output),
            "turn": 1,
            "model": "test-model",
            "telemetry": {"routes": routes},
            "locked_routes": set(),
        }
    )

    assert guarded["decision"]["status"] == "OK"
    assert guarded["decision"]["flags"][route_id]["dbl"] is False


def test_controller_never_arms_dbl_with_stopped_queue_ahead():
    control_panel.bus_routes_config[ROUTE_ID]["dbl_enabled"] = True
    control_panel.bus_routes_config[ROUTE_ID]["tsp_enabled"] = False
    controller = SignalController({"green_time": 20})
    bus = make_bus_for_leg(ROUTE_ID, NODE_A, "QUEUED_BUS")
    bus.lane_index = DBL_LANE_INDEX
    bus.y = lane_center_y(DBL_LANE_INDEX)
    bus.x -= 160.0
    queue_head = parked_blocker(bus, x_offset=60.0)

    controller.update([bus, queue_head])

    assert not controller.is_bus_dbl_eligible(
        bus, NODE_A, [bus, queue_head]
    )
    assert controller.get_active_dbl_request(NODE_A, "EB") is None

    # Match main.py's signal-then-vehicle ordering. The physical refusal is
    # latched on the bus, so merely watching the blocker disappear cannot
    # cause the same stale route-level decision to arm DBL at the stop bar.
    bus.update(
        controller.get_all_signals(INT_X),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        [bus, queue_head],
        controller,
    )
    assert bus.dbl_merge_abandoned_for_leg is True
    controller.update([bus])
    assert controller.get_active_dbl_request(NODE_A, "EB") is None

    # A later fresh decision may reconsider DBL by cycling the route flag off
    # and on after the lane is clear.
    control_panel.bus_routes_config[ROUTE_ID]["dbl_enabled"] = False
    bus.update(
        controller.get_all_signals(INT_X),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        [bus],
        controller,
    )
    assert bus.dbl_merge_abandoned_for_leg is False
    control_panel.bus_routes_config[ROUTE_ID]["dbl_enabled"] = True
    controller.update([bus])
    assert controller.get_active_dbl_request(NODE_A, "EB") is not None


def test_queue_ahead_veto_covers_every_route_and_both_directions():
    first_nodes = {
        "R1_EB_A_NB": 300,
        "R2_EB_B_NB": 300,
        "R3_EB_ONLY": 300,
        "R4_WB_A_SB": 700,
        "R5_WB_B_SB": 700,
        "R6_WB_ONLY": 700,
    }
    for route_id, node_x in first_nodes.items():
        route = control_panel.bus_routes_config[route_id]
        route["dbl_enabled"] = True
        route["tsp_enabled"] = False
        controller = SignalController({"green_time": 20})
        bus = make_bus_for_leg(route_id, node_x, f"QUEUE_{route_id}")
        bus.lane_index = DBL_LANE_INDEX
        bus.y = lane_center_y(DBL_LANE_INDEX, bus.direction)
        bus.x += -160.0 if bus.direction == "EB" else 160.0
        queue_head = parked_blocker(bus, x_offset=60.0)

        controller.update([bus, queue_head])

        assert not controller.is_bus_dbl_eligible(
            bus, node_x, [bus, queue_head]
        ), route_id
        assert controller.get_active_dbl_request(
            node_x, bus.direction
        ) is None, route_id


def test_no_hard_coded_dbl_refusal_in_guard():
    """DBL usability stays a model policy call, not a coded veto."""
    import src.core.guard as guard

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
