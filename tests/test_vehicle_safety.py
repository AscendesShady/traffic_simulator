import pytest

import src.ui.control_panel as control_panel
from src.ui.canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from src.core.signal_controller import SignalController
from src.core.vehicle import (
    Bus,
    DBL_LANE_INDEX,
    ROUTE_MERGE_AREA_PX,
    Vehicle,
)
from tests.helpers import make_bus_for_leg, rectangles_overlap, NODE_A, NODE_B


def signals_for(direction, state):
    node = {item: "RED" for item in ("EB", "WB", "NB", "SB")}
    node[direction] = state
    return {NODE_A: dict(node), NODE_B: dict(node)}


@pytest.mark.parametrize("invalid", [None, "INVALID", (50, 220, 50), 123, {}])
def test_invalid_signal_values_fail_closed(invalid):
    vehicle = Vehicle(NODE_A - 90, H_Y - 1.5 * LANE, "EB", lane_index=1)
    data = {NODE_A: {"EB": invalid}, NODE_B: {"EB": invalid}}
    for _ in range(30):
        vehicle.update(data, INT_X, H_Y, ROAD_W, STOP, LANE, [vehicle], None)
    assert vehicle.distance_to_node_stop_bar(NODE_A, H_Y, ROAD_W, STOP) >= 0
    assert vehicle.speed == 0


@pytest.mark.parametrize(
    "direction,start,expected",
    [
        ("EB", (NODE_A - 60, H_Y - 2.5 * LANE), "NB"),
        ("WB", (NODE_A + 60, H_Y + 2.5 * LANE), "SB"),
        ("NB", (NODE_A - 2.5 * LANE, 360), "WB"),
        ("SB", (NODE_A + 2.5 * LANE, 240), "EB"),
    ],
)
def test_left_turn_displacement_is_continuous(direction, start, expected):
    vehicle = Vehicle(*start, direction, target_turn="LEFT", lane_index=2)
    if direction == "WB":
        vehicle.passed_nodes.add(NODE_B)
    all_green = {node: {item: "GREEN" for item in ("EB", "WB", "NB", "SB")} for node in INT_X}
    maximum_step = 0
    for _ in range(150):
        before = (vehicle.x, vehicle.y)
        vehicle.update(all_green, INT_X, H_Y, ROAD_W, STOP, LANE, [vehicle], None)
        maximum_step = max(maximum_step, ((vehicle.x - before[0]) ** 2 + (vehicle.y - before[1]) ** 2) ** 0.5)
        if vehicle.direction == expected:
            break
    assert vehicle.direction == expected
    assert maximum_step <= vehicle.max_speed + 1e-9


def test_same_origin_left_turn_and_through_vehicle_do_not_overlap():
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    bus = Bus(
        NODE_A - 80,
        H_Y - 2.5 * LANE,
        "EB",
        {
            "route_id": "R1_EB_A_NB",
            "origin": "EB",
            "destination": config["destination"],
            "waypoints": dict(config["waypoints"]),
            "lanes": dict(config["lanes"]),
        },
        "TURN_BUS",
    )
    car = Vehicle(NODE_A - 90, H_Y - 1.5 * LANE, "EB", target_turn="STRAIGHT", lane_index=1)
    controller = SignalController({"green_time": 999})
    vehicles = [bus, car]
    for _ in range(300):
        controller.update(vehicles)
        signal_data = controller.get_all_signals(INT_X)
        for vehicle in vehicles:
            vehicle.update(signal_data, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
        assert not rectangles_overlap(bus, car)


def test_green_through_vehicle_does_not_wait_for_compatible_left_turn_to_clear():
    controller = SignalController({"green_time": 999})
    turning = Vehicle(
        NODE_A - 80,
        H_Y - 2.5 * LANE,
        "EB",
        is_heavy=True,
        target_turn="LEFT",
        lane_index=2,
    )
    through = Vehicle(
        NODE_A - 85,
        H_Y - 1.5 * LANE,
        "EB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    vehicles = [turning, through]
    assert controller.request_intersection_entry(turning, NODE_A, vehicles)

    # Advance only the turner until it has cleared the adjacent through lane,
    # but its long body still occupies the wider intersection rectangle.
    for _ in range(100):
        turning.update(
            controller.get_all_signals(INT_X),
            INT_X,
            H_Y,
            ROAD_W,
            STOP,
            LANE,
            vehicles,
            controller,
        )
        if controller._left_turn_cleared_adjacent_through_lane(
            turning, "EB", NODE_A
        ):
            break

    signals = controller.get_all_signals(INT_X)
    before = through.x
    assert controller.request_intersection_entry(through, NODE_A, vehicles)
    through.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)

    assert signals[NODE_A]["EB"] == "GREEN"
    assert controller.vehicle_occupies_intersection(turning, NODE_A)
    assert through.x > before
    assert id(through) in controller.nodes[NODE_A].reservations


def test_conflict_matrix_allows_same_green_axis_but_blocks_perpendicular_axis():
    controller = SignalController({"green_time": 999})

    assert controller.movements_conflict("EB", "LEFT", "EB", "STRAIGHT")
    assert not controller.movements_conflict("EB", "LEFT", "WB", "STRAIGHT")
    assert controller.movements_conflict("EB", "LEFT", "NB", "STRAIGHT")
    assert controller.movements_conflict("EB", "LEFT", "EB", "LEFT")


def test_unrestricted_left_turn_crosses_on_red_when_conflict_free():
    """A general left turn yields for conflicts, not for its approach lamp."""
    vehicle = Vehicle(
        NODE_A - 90,
        H_Y - 2.5 * LANE,
        "EB",
        target_turn="LEFT",
        lane_index=DBL_LANE_INDEX,
    )
    controller = SignalController({"green_time": 999})

    for _ in range(180):
        vehicle.update(
            signals_for("EB", "RED"),
            INT_X,
            H_Y,
            ROAD_W,
            STOP,
            LANE,
            [vehicle],
            controller,
        )
        controller.update([vehicle])
        if vehicle.direction == "NB":
            break

    assert vehicle.direction == "NB"
    assert vehicle.speed > 0.0


def test_unrestricted_left_turn_yields_to_conflicting_reserved_movement():
    left_turner = Vehicle(
        NODE_A - 90,
        H_Y - 2.5 * LANE,
        "EB",
        target_turn="LEFT",
        lane_index=DBL_LANE_INDEX,
    )
    crossing = Vehicle(
        NODE_A - 1.5 * LANE,
        H_Y + ROAD_W / 2 + STOP + 9 + 5,
        "NB",
        target_turn="STRAIGHT",
        lane_index=1,
        assigned_node_x=NODE_A,
    )
    controller = SignalController({"green_time": 999})
    vehicles = [crossing, left_turner]
    assert controller.request_intersection_entry(crossing, NODE_A, vehicles)

    starting_x = left_turner.x
    left_turner.update(
        signals_for("EB", "RED"),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        vehicles,
        controller,
    )

    assert left_turner.x == starting_x
    assert left_turner.speed == 0.0
    assert id(left_turner) not in controller.nodes[NODE_A].reservations


def test_r1_bus_follows_left_turner_before_entire_node_is_empty():
    leader = make_bus_for_leg("R1_EB_A_NB", NODE_A, "R1_LEADER")
    follower = make_bus_for_leg("R1_EB_A_NB", NODE_A, "R1_FOLLOWER")
    leader.x = NODE_A - 80
    follower.x = NODE_A - 122
    controller = SignalController({"green_time": 999})
    vehicles = [leader, follower]
    assert controller.request_intersection_entry(leader, NODE_A, vehicles)

    for _ in range(100):
        leader.update(
            controller.get_all_signals(INT_X),
            INT_X,
            H_Y,
            ROAD_W,
            STOP,
            LANE,
            vehicles,
            controller,
        )
        if controller._left_turn_cleared_adjacent_through_lane(
            leader, "EB", NODE_A
        ):
            break

    assert controller.vehicle_occupies_intersection(leader, NODE_A)
    assert NODE_A not in leader.passed_nodes
    assert controller.request_intersection_entry(follower, NODE_A, vehicles)

    starting_x = follower.x
    for _ in range(200):
        controller.update(vehicles)
        signals = controller.get_all_signals(INT_X)
        leader.update(
            signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller
        )
        follower.update(
            signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller
        )
        assert not rectangles_overlap(leader, follower)

    assert follower.x > starting_x or follower.direction == "NB"
    assert NODE_A in follower.passed_nodes


def test_dbl_route_bus_moves_to_outer_lane_early():
    config = control_panel.bus_routes_config["R2_EB_B_NB"]
    config["dbl_enabled"] = True
    bus = make_bus_for_leg("R2_EB_B_NB", NODE_A, "EARLY_DBL")
    bus.x = -100
    bus.y = H_Y - 1.5 * LANE
    controller = SignalController({"green_time": 999})
    starting_y = bus.y

    assert bus.target_turn == "STRAIGHT"
    assert bus.distance_to_node_stop_bar(NODE_A, H_Y, ROAD_W, STOP) > 250

    bus.update(
        signals_for("EB", "GREEN"),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        [bus],
        controller,
    )

    assert bus.y < starting_y
    assert bus.lane_index == 1


def test_left_turn_lane_change_still_works():
    config = control_panel.bus_routes_config["R2_EB_B_NB"]
    config["dbl_enabled"] = False
    bus = make_bus_for_leg("R2_EB_B_NB", NODE_B, "LEFT_NO_DBL")
    bus.x = NODE_B - 200
    bus.y = H_Y - 1.5 * LANE
    bus.lane_index = 1
    controller = SignalController({"green_time": 999})

    for _ in range(60):
        bus.update(
            signals_for("EB", "RED"),
            INT_X,
            H_Y,
            ROAD_W,
            STOP,
            LANE,
            [bus],
            controller,
        )
        if bus.lane_index == DBL_LANE_INDEX:
            break

    assert bus.target_turn == "LEFT"
    assert bus.lane_index == DBL_LANE_INDEX
    assert bus.y == H_Y - 2.5 * LANE


def test_dbl_migration_is_refused_when_lane_obstructed():
    config = control_panel.bus_routes_config["R2_EB_B_NB"]
    config["dbl_enabled"] = True
    bus = make_bus_for_leg("R2_EB_B_NB", NODE_A, "BLOCKED_DBL")
    bus.x = -100
    bus.y = H_Y - 1.5 * LANE
    blocker = Vehicle(
        bus.x,
        H_Y - 2.5 * LANE,
        "EB",
        max_speed=0,
        lane_index=DBL_LANE_INDEX,
    )
    controller = SignalController({"green_time": 999})
    starting_y = bus.y

    bus.update(
        signals_for("EB", "GREEN"),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        [bus, blocker],
        controller,
    )

    assert bus.y == starting_y
    assert bus.lane_index == 1
    assert bus.must_hold_for_lane is False
    assert bus.dbl_merge_abandoned_for_leg is True


def test_dbl_car_ahead_does_not_deadlock_bus():
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config["dbl_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", NODE_A)
    bus.x = NODE_A - 227
    car = Vehicle(NODE_A - 185, H_Y - 2.5 * LANE, "EB", lane_index=2)
    controller = SignalController({"green_time": 999}, 2, 2)
    vehicles = [bus, car]
    for _ in range(500):
        controller.update(vehicles)
        signals = controller.get_all_signals(INT_X)
        for vehicle in vehicles:
            vehicle.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
    assert car.x > NODE_A
    assert not rectangles_overlap(bus, car)


def test_lane_blocked_bus_holds_upstream_of_stop_bar():
    config = control_panel.bus_routes_config["R2_EB_B_NB"]
    bus = make_bus_for_leg("R2_EB_B_NB", NODE_B)
    bus.x = NODE_B - 200
    bus.lane_index = 1
    bus.y = H_Y - 1.5 * LANE
    blocker = Vehicle(NODE_B - 180, H_Y - 2.5 * LANE, "EB", max_speed=0, lane_index=2)
    controller = SignalController({"green_time": 999})
    vehicles = [bus, blocker]
    for _ in range(250):
        blocker.x = bus.x + 20
        signals = controller.get_all_signals(INT_X)
        bus.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
        controller.update(vehicles)
    assert bus.lane_index == 1
    assert bus.is_front_bumper_upstream(NODE_B, H_Y, ROAD_W, STOP)
    assert bus.speed == 0


def test_multileg_bus_reserves_next_lane_before_crossing_first_node():
    """R4 must not enter Node B if its post-node lane-2 merge has no storage."""
    control_panel.bus_routes_config["R4_WB_A_SB"]["dbl_enabled"] = False
    bus = make_bus_for_leg("R4_WB_A_SB", NODE_B, "R4_ENTRY_GATE")
    controller = SignalController({"green_time": 999})
    merge_x = bus.route_merge_point_x(NODE_B, ROAD_W)
    blocker = Vehicle(
        merge_x,
        H_Y + 2.5 * LANE,
        "WB",
        max_speed=0.0,
        lane_index=2,
    )
    blocker.passed_nodes.add(NODE_B)
    vehicles = [bus, blocker]
    starting_x = bus.x

    bus.update(
        signals_for("WB", "GREEN"),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        vehicles,
        controller,
    )

    assert bus.route_exit_merge_blocked is True
    assert bus.x == starting_x
    assert bus.speed == 0.0
    assert NODE_B not in bus.passed_nodes


def test_dbl_bus_already_in_next_lane_ignores_obsolete_merge_storage_gate(
    monkeypatch,
):
    """DBL moved R4 to lane 2, so no post-Node-B lane-2 merge is pending."""
    config = control_panel.bus_routes_config["R4_WB_A_SB"]
    monkeypatch.setitem(config, "dbl_enabled", True)
    bus = make_bus_for_leg("R4_WB_A_SB", NODE_B, "R4_DBL_IN_TARGET_LANE")
    bus.lane_index = DBL_LANE_INDEX
    bus.y = H_Y + 2.5 * LANE
    controller = SignalController({"green_time": 999})
    merge_x = bus.route_merge_point_x(NODE_B, ROAD_W)
    blocker = Vehicle(
        merge_x,
        H_Y + 2.5 * LANE,
        "WB",
        max_speed=0.0,
        lane_index=DBL_LANE_INDEX,
    )
    blocker.passed_nodes.add(NODE_B)
    vehicles = [bus, blocker]
    controller.update(vehicles)
    starting_x = bus.x

    assert controller.is_dbl_active_for_approach(NODE_B, "WB")
    bus.update(
        signals_for("WB", "GREEN"),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        vehicles,
        controller,
    )

    assert bus.route_exit_merge_blocked is False
    assert bus.x < starting_x
    assert bus.speed > 0.0


def test_r4_moves_to_next_legs_lane_immediately_after_node_b():
    """The lane plan is route-driven and works with both DBL and TSP disabled."""
    control_panel.bus_routes_config["R4_WB_A_SB"]["dbl_enabled"] = False
    control_panel.bus_routes_config["R4_WB_A_SB"]["tsp_enabled"] = False
    bus = make_bus_for_leg("R4_WB_A_SB", NODE_A, "R4_EARLY_ROUTE_MERGE")
    bus.x = NODE_B - 90
    bus.y = H_Y + 1.5 * LANE
    bus.lane_index = 1
    controller = SignalController({"green_time": 999})
    starting_y = bus.y

    bus.update(
        signals_for("WB", "GREEN"),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        [bus],
        controller,
    )

    assert bus.route_merge_active is True
    assert bus.route_merge_desired_y == H_Y + 2.5 * LANE
    assert bus.y > starting_y


def test_route_merge_target_lane_vehicle_behind_yields():
    bus = make_bus_for_leg("R4_WB_A_SB", NODE_A, "R4_COOPERATIVE_MERGE")
    bus.x = NODE_B - 130.0
    bus.y = H_Y + 1.5 * LANE
    bus.lane_index = 1
    follower = Vehicle(
        NODE_B - 80,
        H_Y + 2.5 * LANE,
        "WB",
        max_speed=1.0,
        lane_index=2,
    )
    follower.passed_nodes.add(NODE_B)
    controller = SignalController({"green_time": 999})
    vehicles = [bus, follower]

    # The bus publishes the deterministic merge request; the vehicle behind
    # then yields on its update while traffic ahead remains free to discharge.
    bus.update(
        signals_for("WB", "GREEN"),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        vehicles,
        controller,
    )
    follower.update(
        signals_for("WB", "GREEN"),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        vehicles,
        controller,
    )

    assert bus.route_merge_active is True
    assert follower.speed == 0.0


def test_unresolved_route_merge_holds_near_previous_node_not_node_a():
    bus = make_bus_for_leg("R4_WB_A_SB", NODE_A, "R4_LINK_HOLD")
    merge_x = bus.route_merge_point_x(NODE_B, ROAD_W)
    assert merge_x == pytest.approx(
        NODE_B - ROAD_W / 2.0 - bus.length / 2.0 - ROUTE_MERGE_AREA_PX
    )
    bus.x = merge_x
    bus.y = H_Y + 1.5 * LANE
    bus.lane_index = 1
    blocker = Vehicle(
        merge_x - 25.0,
        H_Y + 2.5 * LANE,
        "WB",
        max_speed=0.0,
        lane_index=2,
    )
    blocker.passed_nodes.add(NODE_B)
    controller = SignalController({"green_time": 999})
    vehicles = [bus, blocker]
    starting_x = bus.x

    bus.update(
        signals_for("WB", "GREEN"),
        INT_X,
        H_Y,
        ROAD_W,
        STOP,
        LANE,
        vehicles,
        controller,
    )

    assert bus.route_merge_hold_active is True
    assert bus.speed == 0.0
    assert bus.x == starting_x
    assert bus.distance_to_node_stop_bar(NODE_A, H_Y, ROAD_W, STOP) > 35.0


def test_r4_completes_with_dbl_and_tsp_off_when_unobstructed():
    """The deterministic route-lane plan is part of the base simulation."""
    config = control_panel.bus_routes_config["R4_WB_A_SB"]
    config["dbl_enabled"] = False
    config["tsp_enabled"] = False
    route_info = {
        "route_id": "R4_WB_A_SB",
        "origin": config["origin"],
        "destination": config["destination"],
        "waypoints": dict(config["waypoints"]),
        "lanes": dict(config["lanes"]),
    }
    bus = Bus(
        NODE_B + 340,
        H_Y + 1.5 * LANE,
        "WB",
        route_info,
        "R4_BASE_SIM",
    )
    controller = SignalController(
        {"green_time": 30}, yellow_time=3, red_clearance_time=3
    )

    for _ in range(3000):
        controller.update([bus])
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
        if bus.y > 660:
            break

    assert bus.passed_nodes == {NODE_B, NODE_A}
    assert bus.direction == "SB"
    assert bus.y > 660


@pytest.mark.parametrize(
    "direction,node_x,start_y",
    [
        ("NB", NODE_A, H_Y + ROAD_W),
        ("NB", NODE_B, H_Y + ROAD_W),
        ("SB", NODE_A, H_Y - ROAD_W),
        ("SB", NODE_B, H_Y - ROAD_W),
    ],
)
def test_vertical_traffic_completes_only_its_physical_node(
    direction, node_x, start_y
):
    lane_x = node_x - 0.5 * LANE if direction == "NB" else node_x + 0.5 * LANE
    vehicle = Vehicle(
        lane_x,
        start_y,
        direction,
        target_turn="STRAIGHT",
        lane_index=0,
        assigned_node_x=node_x,
    )
    all_green = {
        node: {approach: "GREEN" for approach in ("EB", "WB", "NB", "SB")}
        for node in INT_X
    }

    for _ in range(350):
        vehicle.update(all_green, INT_X, H_Y, ROAD_W, STOP, LANE, [vehicle], None)
        if node_x in vehicle.passed_nodes:
            break

    assert vehicle.passed_nodes == {node_x}
    assert vehicle.get_next_target_node(INT_X) == node_x


def test_starved_left_turn_holds_new_through_entries_until_corner_drains():
    """A left-turner denied the corner sweep by a same-approach through
    vehicle for LEFT_TURN_STARVATION_FRAMES makes the controller hold NEW
    through entries at the bar; vehicles already reserved are untouched, and
    the hold lifts once the left turn is granted."""
    from src.core import signal_controller as sc

    controller = SignalController({"green_time": 999})
    # A through car already in the corner sweep with a reservation.
    in_box = Vehicle(NODE_A - 2.5 * LANE, H_Y - 1.5 * LANE, "EB", target_turn="STRAIGHT", lane_index=1)
    turning = Vehicle(NODE_A - 80, H_Y - 2.5 * LANE, "EB", target_turn="LEFT", lane_index=2)
    follower = Vehicle(NODE_A - 100, H_Y - 1.5 * LANE, "EB", target_turn="STRAIGHT", lane_index=1)
    vehicles = [in_box, turning, follower]
    assert controller.request_intersection_entry(in_box, NODE_A, vehicles)
    assert not controller.request_intersection_entry(turning, NODE_A, vehicles)
    # A cancel (what a stopped vehicle issues every frame) must not erase the wait.
    controller.cancel_intersection_entry(turning, NODE_A)
    assert "EB" in controller.nodes[NODE_A].left_turn_waiting

    # Before the threshold a new through vehicle still gets in.
    controller.frame_number += sc.LEFT_TURN_STARVATION_FRAMES - 1
    assert controller.request_intersection_entry(follower, NODE_A, vehicles)
    controller.cancel_intersection_entry(follower, NODE_A)

    # At the threshold, new through entries are held; the reserved one is not.
    controller.frame_number += 1
    assert not controller.request_intersection_entry(follower, NODE_A, vehicles)
    assert id(in_box) in controller.nodes[NODE_A].reservations

    # Corner drains: the through car leaves the sweep, the left turn is
    # granted, the wait clears and through traffic flows again.
    in_box.x = NODE_A + 3 * LANE
    controller.cancel_intersection_entry(in_box, NODE_A)
    assert controller.request_intersection_entry(turning, NODE_A, vehicles)
    assert "EB" not in controller.nodes[NODE_A].left_turn_waiting
    turning.passed_nodes.add(NODE_A)
    controller.cancel_intersection_entry(turning, NODE_A)
    assert controller.request_intersection_entry(follower, NODE_A, vehicles)


def test_left_turn_wait_is_dropped_when_the_turner_is_gone():
    from src.core import signal_controller as sc

    controller = SignalController({"green_time": 999})
    in_box = Vehicle(NODE_A - 2.5 * LANE, H_Y - 1.5 * LANE, "EB", target_turn="STRAIGHT", lane_index=1)
    turning = Vehicle(NODE_A - 80, H_Y - 2.5 * LANE, "EB", target_turn="LEFT", lane_index=2)
    follower = Vehicle(NODE_A - 100, H_Y - 1.5 * LANE, "EB", target_turn="STRAIGHT", lane_index=1)
    assert controller.request_intersection_entry(in_box, NODE_A, [in_box, turning, follower])
    assert not controller.request_intersection_entry(turning, NODE_A, [in_box, turning, follower])
    controller.frame_number += sc.LEFT_TURN_STARVATION_FRAMES
    in_box.x = NODE_A + 3 * LANE
    controller.cancel_intersection_entry(in_box, NODE_A)
    # The turner has left the network: no stale hold on through traffic.
    assert controller.request_intersection_entry(follower, NODE_A, [in_box, follower])
    assert "EB" not in controller.nodes[NODE_A].left_turn_waiting
