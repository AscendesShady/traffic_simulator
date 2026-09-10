import pytest

import control_panel
from canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from signal_controller import SignalController
from vehicle import (
    Bus,
    DBL_LANE_INDEX,
    ROUTE_MERGE_AREA_PX,
    Vehicle,
)
from tests.helpers import make_bus_for_leg, rectangles_overlap


def signals_for(direction, state):
    node = {item: "RED" for item in ("EB", "WB", "NB", "SB")}
    node[direction] = state
    return {300: dict(node), 700: dict(node)}


@pytest.mark.parametrize("invalid", [None, "INVALID", (50, 220, 50), 123, {}])
def test_invalid_signal_values_fail_closed(invalid):
    vehicle = Vehicle(210, H_Y - 1.5 * LANE, "EB", lane_index=1)
    data = {300: {"EB": invalid}, 700: {"EB": invalid}}
    for _ in range(30):
        vehicle.update(data, INT_X, H_Y, ROAD_W, STOP, LANE, [vehicle], None)
    assert vehicle.distance_to_node_stop_bar(300, H_Y, ROAD_W, STOP) >= 0
    assert vehicle.speed == 0


@pytest.mark.parametrize(
    "direction,start,expected",
    [
        ("EB", (240, H_Y - 2.5 * LANE), "NB"),
        ("WB", (360, H_Y + 2.5 * LANE), "SB"),
        ("NB", (300 - 2.5 * LANE, 360), "WB"),
        ("SB", (300 + 2.5 * LANE, 240), "EB"),
    ],
)
def test_left_turn_displacement_is_continuous(direction, start, expected):
    vehicle = Vehicle(*start, direction, target_turn="LEFT", lane_index=2)
    if direction == "WB":
        vehicle.passed_nodes.add(700)
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
        220,
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
    car = Vehicle(210, H_Y - 1.5 * LANE, "EB", target_turn="STRAIGHT", lane_index=1)
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
        220,
        H_Y - 2.5 * LANE,
        "EB",
        is_heavy=True,
        target_turn="LEFT",
        lane_index=2,
    )
    through = Vehicle(
        215,
        H_Y - 1.5 * LANE,
        "EB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    vehicles = [turning, through]
    assert controller.request_intersection_entry(turning, 300, vehicles)

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
            turning, "EB", 300
        ):
            break

    signals = controller.get_all_signals(INT_X)
    before = through.x
    assert controller.request_intersection_entry(through, 300, vehicles)
    through.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)

    assert signals[300]["EB"] == "GREEN"
    assert controller.vehicle_occupies_intersection(turning, 300)
    assert through.x > before
    assert id(through) in controller.nodes[300].reservations


def test_conflict_matrix_allows_same_green_axis_but_blocks_perpendicular_axis():
    controller = SignalController({"green_time": 999})

    assert controller.movements_conflict("EB", "LEFT", "EB", "STRAIGHT")
    assert not controller.movements_conflict("EB", "LEFT", "WB", "STRAIGHT")
    assert controller.movements_conflict("EB", "LEFT", "NB", "STRAIGHT")
    assert controller.movements_conflict("EB", "LEFT", "EB", "LEFT")


def test_r1_bus_follows_left_turner_before_entire_node_is_empty():
    leader = make_bus_for_leg("R1_EB_A_NB", 300, "R1_LEADER")
    follower = make_bus_for_leg("R1_EB_A_NB", 300, "R1_FOLLOWER")
    leader.x = 220
    follower.x = 178
    controller = SignalController({"green_time": 999})
    vehicles = [leader, follower]
    assert controller.request_intersection_entry(leader, 300, vehicles)

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
            leader, "EB", 300
        ):
            break

    assert controller.vehicle_occupies_intersection(leader, 300)
    assert 300 not in leader.passed_nodes
    assert controller.request_intersection_entry(follower, 300, vehicles)

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
    assert 300 in follower.passed_nodes


def test_dbl_route_bus_moves_to_outer_lane_early():
    config = control_panel.bus_routes_config["R2_EB_B_NB"]
    config["dbl_enabled"] = True
    bus = make_bus_for_leg("R2_EB_B_NB", 300, "EARLY_DBL")
    bus.x = -100
    bus.y = H_Y - 1.5 * LANE
    controller = SignalController({"green_time": 999})
    starting_y = bus.y

    assert bus.target_turn == "STRAIGHT"
    assert bus.distance_to_node_stop_bar(300, H_Y, ROAD_W, STOP) > 250

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
    bus = make_bus_for_leg("R2_EB_B_NB", 700, "LEFT_NO_DBL")
    bus.x = 500
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


def test_dbl_migration_respects_clear_lane():
    config = control_panel.bus_routes_config["R2_EB_B_NB"]
    config["dbl_enabled"] = True
    bus = make_bus_for_leg("R2_EB_B_NB", 300, "BLOCKED_DBL")
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
    assert bus.must_hold_for_lane is True


def test_dbl_car_ahead_does_not_deadlock_bus():
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config["dbl_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300)
    bus.x = 73
    car = Vehicle(115, H_Y - 2.5 * LANE, "EB", lane_index=2)
    controller = SignalController({"green_time": 999}, 2, 2)
    vehicles = [bus, car]
    for _ in range(500):
        controller.update(vehicles)
        signals = controller.get_all_signals(INT_X)
        for vehicle in vehicles:
            vehicle.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
    assert car.x > 300
    assert not rectangles_overlap(bus, car)


def test_lane_blocked_bus_holds_upstream_of_stop_bar():
    config = control_panel.bus_routes_config["R2_EB_B_NB"]
    bus = make_bus_for_leg("R2_EB_B_NB", 700)
    bus.x = 500
    bus.lane_index = 1
    bus.y = H_Y - 1.5 * LANE
    blocker = Vehicle(520, H_Y - 2.5 * LANE, "EB", max_speed=0, lane_index=2)
    controller = SignalController({"green_time": 999})
    vehicles = [bus, blocker]
    for _ in range(250):
        blocker.x = bus.x + 20
        signals = controller.get_all_signals(INT_X)
        bus.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
        controller.update(vehicles)
    assert bus.lane_index == 1
    assert bus.is_front_bumper_upstream(700, H_Y, ROAD_W, STOP)
    assert bus.speed == 0


def test_multileg_bus_reserves_next_lane_before_crossing_first_node():
    """R4 must not enter Node B if its post-node lane-2 merge has no storage."""
    control_panel.bus_routes_config["R4_WB_A_SB"]["dbl_enabled"] = False
    bus = make_bus_for_leg("R4_WB_A_SB", 700, "R4_ENTRY_GATE")
    controller = SignalController({"green_time": 999})
    merge_x = bus.route_merge_point_x(700, ROAD_W)
    blocker = Vehicle(
        merge_x,
        H_Y + 2.5 * LANE,
        "WB",
        max_speed=0.0,
        lane_index=2,
    )
    blocker.passed_nodes.add(700)
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
    assert 700 not in bus.passed_nodes


def test_r4_moves_to_next_legs_lane_immediately_after_node_b():
    """The lane plan is route-driven and works with both DBL and TSP disabled."""
    control_panel.bus_routes_config["R4_WB_A_SB"]["dbl_enabled"] = False
    control_panel.bus_routes_config["R4_WB_A_SB"]["tsp_enabled"] = False
    bus = make_bus_for_leg("R4_WB_A_SB", 300, "R4_EARLY_ROUTE_MERGE")
    bus.x = 610.0
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
    bus = make_bus_for_leg("R4_WB_A_SB", 300, "R4_COOPERATIVE_MERGE")
    bus.x = 570.0
    bus.y = H_Y + 1.5 * LANE
    bus.lane_index = 1
    follower = Vehicle(
        620.0,
        H_Y + 2.5 * LANE,
        "WB",
        max_speed=1.0,
        lane_index=2,
    )
    follower.passed_nodes.add(700)
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
    bus = make_bus_for_leg("R4_WB_A_SB", 300, "R4_LINK_HOLD")
    merge_x = bus.route_merge_point_x(700, ROAD_W)
    assert merge_x == pytest.approx(
        700 - ROAD_W / 2.0 - bus.length / 2.0 - ROUTE_MERGE_AREA_PX
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
    blocker.passed_nodes.add(700)
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
    assert bus.distance_to_node_stop_bar(300, H_Y, ROAD_W, STOP) > 35.0


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
        1040,
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

    assert bus.passed_nodes == {700, 300}
    assert bus.direction == "SB"
    assert bus.y > 660


@pytest.mark.parametrize(
    "direction,node_x,start_y",
    [
        ("NB", 300, H_Y + ROAD_W),
        ("NB", 700, H_Y + ROAD_W),
        ("SB", 300, H_Y - ROAD_W),
        ("SB", 700, H_Y - ROAD_W),
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
