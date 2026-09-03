import pytest

import control_panel
from canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from signal_controller import (
    ALL_RED_CLEARANCE,
    CONFLICT_YELLOW,
    COMPLETED,
    DENIED,
    NORMAL,
    PRIORITY_ACTIVE,
    PRIORITY_CLEARING,
    RECOVERY_ALL_RED,
    SignalController,
)
from vehicle import Vehicle
from tests.helpers import make_bus_for_leg


ROUTE_LEGS = [
    ("R1_EB_A_NB", 300, "EB", "LEFT", 2),
    ("R2_EB_B_NB", 300, "EB", "STRAIGHT", 1),
    ("R2_EB_B_NB", 700, "EB", "LEFT", 2),
    ("R3_EB_ONLY", 300, "EB", "STRAIGHT", 1),
    ("R3_EB_ONLY", 700, "EB", "STRAIGHT", 1),
    ("R4_WB_A_SB", 700, "WB", "STRAIGHT", 1),
    ("R4_WB_A_SB", 300, "WB", "LEFT", 2),
    ("R5_WB_B_SB", 700, "WB", "LEFT", 2),
    ("R6_WB_ONLY", 700, "WB", "STRAIGHT", 1),
    ("R6_WB_ONLY", 300, "WB", "STRAIGHT", 1),
]


def advance_to_priority(controller, vehicles, node_x, limit=30):
    observed = []
    for _ in range(limit):
        controller.update(vehicles)
        state = controller.get_node_status(node_x)["priority_state"]
        if not observed or observed[-1] != state:
            observed.append(state)
        if state == PRIORITY_ACTIVE:
            return observed
    raise AssertionError(f"Priority was not granted: {observed}")


@pytest.mark.parametrize("route_id,node_x,approach,movement,lane", ROUTE_LEGS)
@pytest.mark.parametrize("feature_mode", ["DBL", "TSP", "COMBINED"])
def test_every_route_leg_gets_exclusive_conflict_safe_priority(
    route_id, node_x, approach, movement, lane, feature_mode
):
    config = control_panel.bus_routes_config[route_id]
    config["tsp_enabled"] = feature_mode in ("TSP", "COMBINED")
    config["dbl_enabled"] = feature_mode in ("DBL", "COMBINED")
    bus = make_bus_for_leg(route_id, node_x, f"BUS_{route_id}_{node_x}")
    controller = SignalController({"green_time": 100}, yellow_time=2, red_clearance_time=2)
    controller.phase = 3 if approach == "EB" else 0
    other_node = 700 if node_x == 300 else 300
    other_phase = controller.get_node_status(other_node)["phase_index"]

    observed = advance_to_priority(controller, [bus], node_x)

    assert CONFLICT_YELLOW in observed
    assert ALL_RED_CLEARANCE in observed
    assert observed[-1] == PRIORITY_ACTIVE
    expected = {direction: "RED" for direction in ("EB", "WB", "NB", "SB")}
    expected[approach] = "GREEN"
    assert controller.get_all_signals(INT_X)[node_x] == expected
    assert controller.get_node_status(other_node)["priority_state"] == NORMAL
    assert controller.get_node_status(other_node)["phase_index"] == other_phase
    request = controller.get_node_status(node_x)["active_request"]
    assert request["route_id"] == route_id
    assert request["node_x"] == node_x
    assert request["movement"] == movement
    assert request["entry_lane"] == lane


@pytest.mark.parametrize("phase", range(6))
@pytest.mark.parametrize("feature", ["tsp_enabled", "dbl_enabled"])
def test_priority_is_safe_from_every_normal_phase_and_each_feature(phase, feature):
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config[feature] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300)
    controller = SignalController({"green_time": 100}, yellow_time=2, red_clearance_time=2)
    controller.phase = phase
    observed = advance_to_priority(controller, [bus], 300)
    assert ALL_RED_CLEARANCE in observed
    assert controller.get_all_signals(INT_X)[300] == {
        "EB": "GREEN", "WB": "RED", "NB": "RED", "SB": "RED"
    }


def test_r2_straight_leg_uses_lane_one_and_rejects_wrong_lane(signal_system):
    config = control_panel.bus_routes_config["R2_EB_B_NB"]
    config["dbl_enabled"] = True
    bus = make_bus_for_leg("R2_EB_B_NB", 300)
    assert bus.lane_index == 1
    assert signal_system.is_bus_dbl_eligible(bus, 300)
    bus.lane_index = 2
    assert not signal_system.is_bus_dbl_eligible(bus, 300)
    heavy_car = Vehicle(bus.x, bus.y, "EB", is_heavy=True, lane_index=1)
    assert not signal_system.is_bus_dbl_eligible(heavy_car, 300)


def test_simultaneous_requests_are_deterministically_ordered():
    for route_id in ("R1_EB_A_NB", "R2_EB_B_NB"):
        control_panel.bus_routes_config[route_id]["tsp_enabled"] = True
    bus_b = make_bus_for_leg("R1_EB_A_NB", 300, "BUS_B")
    bus_a = make_bus_for_leg("R2_EB_B_NB", 300, "BUS_A")
    controller = SignalController({"green_time": 100}, 2, 2)
    controller.update([bus_b, bus_a])
    status = controller.get_node_status(300)
    assert status["active_request"]["bus_id"] == "BUS_A"
    assert [item["bus_id"] for item in status["queued_requests"]] == ["BUS_B"]


def test_priority_waits_for_occupied_conflict_box():
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300)
    blocker = Vehicle(300, H_Y, "NB")
    controller = SignalController({"green_time": 100}, 2, 2)
    controller.phase = 3
    for _ in range(20):
        controller.update([bus, blocker])
    assert controller.get_node_status(300)["priority_state"] == ALL_RED_CLEARANCE
    assert set(controller.get_all_signals(INT_X)[300].values()) == {"RED"}


def test_normal_green_begins_only_after_all_red_box_clearance():
    controller = SignalController(
        {"green_time": 100}, yellow_time=2, red_clearance_time=2
    )
    controller.nodes[300].phase = 2
    controller.nodes[300].timer = 1
    blocker = Vehicle(300, H_Y, "NB")

    controller.update([blocker])
    assert controller.get_node_status(300)["phase_index"] == 2
    assert set(controller.get_all_signals(INT_X)[300].values()) == {"RED"}

    controller.update([])
    assert controller.get_node_status(300)["phase_index"] == 3
    assert controller.get_all_signals(INT_X)[300] == {
        "EB": "RED", "WB": "RED", "NB": "GREEN", "SB": "GREEN"
    }


def test_grant_holds_until_rear_clears_then_recovers():
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300)
    controller = SignalController({"green_time": 100}, 2, 2)
    vehicles = [bus]
    advance_to_priority(controller, vehicles, 300)
    observed = []
    for _ in range(500):
        signals = controller.get_all_signals(INT_X)
        bus.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
        controller.update(vehicles)
        state = controller.get_node_status(300)["priority_state"]
        observed.append(state)
        if state == NORMAL:
            break
    assert PRIORITY_CLEARING in observed
    assert RECOVERY_ALL_RED in observed
    assert observed[-1] == NORMAL
    assert 300 in bus.passed_nodes
    history = controller.get_node_status(300)["terminal_history"]
    assert history[-1]["state"] == COMPLETED
    assert history[-1]["bus_id"] == bus.bus_id
    assert controller.get_latest_terminal_status_for_bus(bus)["state"] == COMPLETED


def test_live_disable_before_grant_cancels_through_all_red():
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300)
    controller = SignalController({"green_time": 100}, 3, 3)
    controller.phase = 3
    controller.update([bus])
    assert controller.get_node_status(300)["priority_state"] == CONFLICT_YELLOW
    config["tsp_enabled"] = False
    controller.update([bus])
    assert controller.get_node_status(300)["priority_state"] == RECOVERY_ALL_RED
    assert set(controller.get_all_signals(INT_X)[300].values()) == {"RED"}


def test_live_disable_during_active_grant_lets_bus_clear_safely():
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config["dbl_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300)
    controller = SignalController({"green_time": 100}, 2, 2)
    vehicles = [bus]
    advance_to_priority(controller, vehicles, 300)
    config["dbl_enabled"] = False
    controller.update(vehicles)
    assert controller.get_node_status(300)["priority_state"] == PRIORITY_ACTIVE
    assert controller.get_all_signals(INT_X)[300] == {
        "EB": "GREEN", "WB": "RED", "NB": "RED", "SB": "RED"
    }


def test_removed_bus_forces_safe_recovery_all_red():
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300)
    controller = SignalController({"green_time": 100}, 2, 2)
    advance_to_priority(controller, [bus], 300)
    controller.update([])
    assert controller.get_node_status(300)["priority_state"] == RECOVERY_ALL_RED
    assert set(controller.get_all_signals(INT_X)[300].values()) == {"RED"}


def test_duplicate_eligible_frames_do_not_consume_request_ids():
    control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300, "STABLE_ID_BUS")
    controller = SignalController({"green_time": 100}, 10, 10)

    request_ids = []
    for _ in range(6):
        controller.update([bus])
        request_ids.append(
            controller.get_node_status(300)["active_request"]["request_id"]
        )

    assert request_ids == ["PRIORITY_000001"] * 6
    assert controller._request_sequence == 1
    assert controller.get_node_status(300)["active_request"]["wait_frames"] == 5


def test_queued_timeout_is_terminal_and_cannot_renew_in_place():
    for route_id in ("R1_EB_A_NB", "R2_EB_B_NB"):
        control_panel.bus_routes_config[route_id]["tsp_enabled"] = True
    first = make_bus_for_leg("R1_EB_A_NB", 300, "BUS_A")
    queued = make_bus_for_leg("R2_EB_B_NB", 300, "BUS_B")
    controller = SignalController(
        {"green_time": 100},
        yellow_time=2,
        red_clearance_time=2,
        priority_request_timeout=5,
    )

    for _ in range(9):
        controller.update([first, queued])

    status = controller.get_node_status(300)
    timed_out = [
        item for item in status["terminal_history"] if item["bus_id"] == "BUS_B"
    ]
    assert len(timed_out) == 1
    assert timed_out[0]["state"] == DENIED
    assert timed_out[0]["denial_or_cancel_reason"] == "REQUEST_TIMEOUT"
    assert timed_out[0]["attempt_number"] == 1
    assert not any(item["bus_id"] == "BUS_B" for item in status["queued_requests"])

    # Remaining continuously eligible does not create renewable, zero-age work.
    for _ in range(10):
        controller.update([first, queued])
    assert controller._request_sequence == 2

    # Leaving the eligibility zone for one update allows an explicit new attempt.
    queued.x = -100
    controller.update([first, queued])
    queued.x = 190
    controller.update([first, queued])
    retry = next(
        item
        for item in controller.get_node_status(300)["queued_requests"]
        if item["bus_id"] == "BUS_B"
    )
    assert retry["attempt_number"] == 2
    assert retry["request_id"] == "PRIORITY_000003"
