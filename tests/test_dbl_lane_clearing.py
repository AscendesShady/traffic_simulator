"""DBL clears every general vehicle from its reserved approach lane.

An active Dynamic Bus Lane orders vehicles ahead of and behind its target bus
out of lane 2 immediately, with no bus-proximity threshold. A safe change is
never forced when both receiving lanes are occupied. Every scenario also
checks that no two vehicles ever overlap.
"""
import itertools

import src.ui.control_panel as control_panel
from src.ui.canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from src.core.signal_controller import SignalController
from src.core.vehicle import DBL_LANE_INDEX, Vehicle
from tests.helpers import make_bus_for_leg, rectangles_overlap, NODE_A, NODE_B


NODE_A = NODE_A
NODE_B = NODE_B
STOP_BAR_X = NODE_A - ROAD_W / 2 - STOP
ALL_GREEN = {
    node: {direction: "GREEN" for direction in ("EB", "WB", "NB", "SB")}
    for node in INT_X
}


def lane_center_y(lane_index, direction="EB"):
    offset = (lane_index + 0.5) * LANE
    return H_Y - offset if direction == "EB" else H_Y + offset


def make_controller():
    return SignalController(
        {"green_time": 60, "is_running": True},
        yellow_time=3,
        red_clearance_time=3,
    )


def enable_dbl(route_id="R3_EB_ONLY"):
    route = control_panel.bus_routes_config[route_id]
    route["active"] = True
    route["dbl_enabled"] = True
    route["tsp_enabled"] = False


def dbl_bus(dist_to_stop_bar, lane_index=DBL_LANE_INDEX, bus_id="DBL_BUS"):
    bus = make_bus_for_leg("R3_EB_ONLY", NODE_A, bus_id)
    bus.lane_index = lane_index
    bus.y = lane_center_y(lane_index)
    bus.x = STOP_BAR_X - dist_to_stop_bar - bus.length / 2.0
    return bus


def car(x, lane_index, max_speed=1.0, direction="EB"):
    vehicle = Vehicle(
        x=x,
        y=lane_center_y(lane_index, direction),
        direction=direction,
        max_speed=max_speed,
        lane_index=lane_index,
    )
    vehicle.speed = max_speed
    return vehicle


def assert_no_overlap(vehicles):
    for first, second in itertools.combinations(vehicles, 2):
        assert not rectangles_overlap(first, second), (
            f"overlap between {first.__class__.__name__}@({first.x:.1f},{first.y:.1f}) "
            f"and {second.__class__.__name__}@({second.x:.1f},{second.y:.1f})"
        )


def run(controller, vehicles, frames, signal_data=None):
    """Advance every vehicle and the controller together, checking overlap
    after every frame. The bus is updated first so the request arms before
    the cars look at it, matching main.py's list order for a leading bus."""
    for _ in range(frames):
        signals = signal_data or controller.get_all_signals(INT_X)
        for vehicle in list(vehicles):
            vehicle.update(
                signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller
            )
        controller.update(vehicles)
        assert_no_overlap(vehicles)


# ---------------------------------------------------------------------------
# FIX 1: cars ahead of the bus vacate the DBL lane
# ---------------------------------------------------------------------------

def test_car_ahead_of_dbl_bus_moves_out_of_the_lane():
    enable_dbl()
    controller = make_controller()
    bus = dbl_bus(dist_to_stop_bar=300)
    blocker = car(bus.x + 70, DBL_LANE_INDEX)
    vehicles = [bus, blocker]

    controller.update(vehicles)
    assert controller.get_active_dbl_request(NODE_A, "EB")["bus_id"] == "DBL_BUS"

    run(controller, vehicles, 120, ALL_GREEN)

    assert blocker.lane_index in (0, 1)
    assert blocker.lane_index != DBL_LANE_INDEX
    assert blocker.y == lane_center_y(blocker.lane_index)
    assert blocker.lane_vacate_target is None
    # The car kept moving the whole time: it was never stopped or removed.
    assert blocker.x > bus.x
    assert blocker in vehicles


def test_car_ahead_far_from_bus_is_cleared_without_proximity_gate():
    enable_dbl()
    controller = make_controller()
    bus = dbl_bus(dist_to_stop_bar=390)  # inside the link-length eligibility cap
    far_car = car(bus.x + bus.length / 2 + 9 + 260, DBL_LANE_INDEX)
    vehicles = [bus, far_car]
    controller.update(vehicles)
    assert controller.get_active_dbl_request(NODE_A, "EB")

    # Both drive at the same speed so the old 200px gate would never have
    # fired. The unconditional reservation still clears the car.
    run(controller, vehicles, 120, ALL_GREEN)

    assert far_car.lane_index in (0, 1)
    assert far_car.lane_index != DBL_LANE_INDEX
    assert far_car.lane_vacate_target is None


def test_westbound_dbl_clears_vehicle_ahead_symmetrically():
    enable_dbl("R6_WB_ONLY")
    controller = make_controller()
    bus = make_bus_for_leg("R6_WB_ONLY", NODE_B, "WB_DBL_BUS")
    bus.lane_index = DBL_LANE_INDEX
    bus.y = lane_center_y(DBL_LANE_INDEX, "WB")
    wb_stop_bar_x = NODE_B + ROAD_W / 2 + STOP
    bus.x = wb_stop_bar_x + 300 + bus.length / 2.0
    blocker = car(
        bus.x - 80,
        DBL_LANE_INDEX,
        direction="WB",
    )
    vehicles = [bus, blocker]

    controller.update(vehicles)
    assert controller.get_active_dbl_request(NODE_B, "WB")
    run(controller, vehicles, 120, ALL_GREEN)

    assert blocker.lane_index in (0, 1)
    assert blocker.lane_index != DBL_LANE_INDEX
    assert blocker.y == lane_center_y(blocker.lane_index, "WB")


def test_car_ahead_keeps_driving_when_both_other_lanes_are_blocked():
    enable_dbl()
    controller = make_controller()
    bus = dbl_bus(dist_to_stop_bar=300)
    blocker = car(bus.x + 70, DBL_LANE_INDEX)
    # Same-speed traffic alongside in lanes 1 and 0 keeps both corridors
    # occupied for the whole run.
    side_lane_1 = car(blocker.x, 1)
    side_lane_0 = car(blocker.x, 0)
    vehicles = [bus, blocker, side_lane_1, side_lane_0]
    controller.update(vehicles)
    assert controller.get_active_dbl_request(NODE_A, "EB")

    start_x = blocker.x
    start_y = blocker.y
    run(controller, vehicles, 90, ALL_GREEN)

    assert blocker.lane_index == DBL_LANE_INDEX
    assert blocker.y == start_y
    assert blocker.lane_vacate_target is None
    # It was neither stopped nor removed; it simply carried on.
    assert blocker.x > start_x + 60
    assert blocker.speed > 0
    assert blocker in vehicles


def test_car_behind_dbl_bus_vacates_reserved_lane():
    enable_dbl()
    controller = make_controller()
    bus = dbl_bus(dist_to_stop_bar=200)
    follower = car(bus.x - 90, DBL_LANE_INDEX)
    vehicles = [bus, follower]
    controller.update(vehicles)
    assert controller.get_active_dbl_request(NODE_A, "EB")

    run(controller, vehicles, 120, ALL_GREEN)

    assert follower.speed > 0.0
    assert follower.lane_index in (0, 1)
    assert follower.lane_index != DBL_LANE_INDEX
    assert follower.lane_vacate_target is None
    assert follower.y == lane_center_y(follower.lane_index)


# ---------------------------------------------------------------------------
# FIX 2: a car blocking the bus's merge into the DBL lane makes room
# ---------------------------------------------------------------------------

def test_car_blocking_dbl_merge_eases_off_so_the_bus_can_merge():
    enable_dbl()
    controller = make_controller()
    # Bus in its configured lane 1, far upstream, wanting the DBL lane.
    bus = dbl_bus(dist_to_stop_bar=450, lane_index=1)
    # A faster car just behind it in the DBL lane sits inside the merge
    # corridor; left alone it would shadow the bus for hundreds of frames.
    blocker = car(bus.x - 20, DBL_LANE_INDEX, max_speed=1.1)
    vehicles = [bus, blocker]

    run(controller, vehicles, 1, ALL_GREEN)
    assert bus.must_hold_for_lane is True
    assert bus.dbl_merge_blocker is blocker
    assert blocker.dbl_merge_yield_slow is True

    # Until the merge completes the car only eases off; it never stops.
    frames_to_merge = None
    for frame in range(1, 151):
        run(controller, vehicles, 1, ALL_GREEN)
        if bus.lane_index == DBL_LANE_INDEX:
            frames_to_merge = frame
            break
        assert blocker.speed > 0.0
        assert blocker.lane_index == DBL_LANE_INDEX

    assert frames_to_merge is not None
    assert bus.must_hold_for_lane is False
    assert bus.dbl_merge_abandoned_for_leg is False
    assert bus.dbl_merge_blocker is None
    assert blocker in vehicles


def test_only_the_nearest_blocker_is_asked_to_make_room():
    enable_dbl()
    controller = make_controller()
    bus = dbl_bus(dist_to_stop_bar=450, lane_index=1)
    near = car(bus.x - 20, DBL_LANE_INDEX)
    far = car(bus.x - 40, DBL_LANE_INDEX)
    vehicles = [bus, near, far]

    run(controller, vehicles, 1, ALL_GREEN)

    assert bus.dbl_merge_blocker is near
    assert near.dbl_merge_yield_slow is True
    assert far.dbl_merge_yield_slow is False


def test_parked_merge_blocker_prevents_dbl_attempt():
    """A stopped obstruction vetoes DBL before it can hold the bus."""
    enable_dbl()
    controller = make_controller()
    bus = dbl_bus(dist_to_stop_bar=5, lane_index=1)
    parked = car(bus.x, DBL_LANE_INDEX, max_speed=0.0)
    vehicles = [bus, parked]

    run(controller, vehicles, 60, ALL_GREEN)

    assert bus.lane_index == 1
    assert bus.must_hold_for_lane is False
    assert bus.dbl_merge_abandoned_for_leg is True
    assert bus.dbl_merge_blocker is None
    assert controller.get_active_dbl_request(NODE_A, "EB") is None
    assert parked.speed == 0.0
    assert parked.lane_index == DBL_LANE_INDEX
    assert parked.y == lane_center_y(DBL_LANE_INDEX)
