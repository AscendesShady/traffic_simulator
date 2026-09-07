"""Bounded DBL merge: a blocked bus lane must never freeze a bus forever.

The invariant under test is that a DBL-enabled bus is never left worse off
than an unequipped bus by more than the bounded merge window. If it can reach
the DBL lane it uses it; if the lane stays blocked it gives the merge up and
runs in its configured lane instead of holding upstream indefinitely.
"""
import control_panel
from canvas_gemini import H_Y, INT_X, LANE
from signal_controller import SignalController
from vehicle import DBL_LANE_INDEX, DBL_MERGE_ABANDON_FRAMES, Vehicle
from tests.helpers import make_bus_for_leg


NODE_A = 300


def lane_center_y(lane_index, direction="EB"):
    offset = (lane_index + 0.5) * LANE
    return H_Y - offset if direction == "EB" else H_Y + offset


def make_controller():
    return SignalController(
        {"green_time": 60, "is_running": True},
        yellow_time=3,
        red_clearance_time=3,
    )


def enable_route(route_id, *, dbl, tsp=False):
    route = control_panel.bus_routes_config[route_id]
    route["active"] = True
    route["dbl_enabled"] = dbl
    route["tsp_enabled"] = tsp
    return route


def blocker_beside(bus, lane_index=DBL_LANE_INDEX):
    """A parked vehicle in `lane_index` alongside the bus, blocking a merge."""
    blocker = Vehicle(
        x=bus.x,
        y=lane_center_y(lane_index, bus.direction),
        direction=bus.direction,
        max_speed=0.0,
        lane_index=lane_index,
    )
    blocker.speed = 0.0
    return blocker


def run(controller, bus, vehicles, frames):
    """Advance the bus and the controller together for `frames` frames."""
    for _ in range(frames):
        signal_data = controller.get_all_signals(INT_X)
        bus.update(signal_data, INT_X, H_Y, all_vehicles=vehicles,
                   signal_controller=controller)
        controller.update(vehicles)


def frames_until_node_passed(controller, bus, vehicles, node_x, limit):
    for frame in range(1, limit + 1):
        signal_data = controller.get_all_signals(INT_X)
        bus.update(signal_data, INT_X, H_Y, all_vehicles=vehicles,
                   signal_controller=controller)
        controller.update(vehicles)
        if node_x in bus.passed_nodes:
            return frame
    return None


def test_dbl_merge_abandoned_when_blocked():
    enable_route("R3_EB_ONLY", dbl=True)
    controller = make_controller()
    bus = make_bus_for_leg("R3_EB_ONLY", NODE_A, "BLOCKED_MERGE_BUS")
    blocker = blocker_beside(bus)
    vehicles = [bus, blocker]

    # Before the window expires the bus is still trying, and still holding.
    run(controller, bus, vehicles, DBL_MERGE_ABANDON_FRAMES - 10)
    assert bus.dbl_merge_abandoned_for_leg is False
    assert bus.must_hold_for_lane is True
    assert bus.lane_index != DBL_LANE_INDEX
    assert NODE_A not in bus.passed_nodes

    # Past the window it gives the merge up and stops holding.
    run(controller, bus, vehicles, 30)
    assert bus.dbl_merge_abandoned_for_leg is True
    assert bus.must_hold_for_lane is False
    assert bus.lane_index == 1
    assert bus.y == lane_center_y(1)

    # And it is no longer frozen: it clears the intersection.
    assert frames_until_node_passed(controller, bus, vehicles, NODE_A, 600)


def test_dbl_merge_succeeds_when_clear():
    enable_route("R3_EB_ONLY", dbl=True)
    controller = make_controller()
    bus = make_bus_for_leg("R3_EB_ONLY", NODE_A, "CLEAR_MERGE_BUS")
    vehicles = [bus]

    run(controller, bus, vehicles, 120)

    assert bus.lane_index == DBL_LANE_INDEX
    assert bus.dbl_merge_abandoned_for_leg is False
    assert bus.dbl_merge_hold_frames == 0
    assert bus.must_hold_for_lane is False


def test_dbl_never_worse_than_no_dbl():
    """The DBL penalty for a blocked lane is bounded, never unbounded."""
    def crossing_frames(dbl_enabled):
        enable_route("R3_EB_ONLY", dbl=dbl_enabled)
        controller = make_controller()
        bus = make_bus_for_leg("R3_EB_ONLY", NODE_A, "INVARIANT_BUS")
        vehicles = [bus, blocker_beside(bus)]
        return frames_until_node_passed(controller, bus, vehicles, NODE_A, 1200)

    without_dbl = crossing_frames(False)
    with_dbl = crossing_frames(True)

    assert without_dbl is not None
    assert with_dbl is not None
    # DBL may cost at most the bounded merge window; it can never freeze the
    # bus, which is what the unbounded hold used to do.
    assert with_dbl <= without_dbl + DBL_MERGE_ABANDON_FRAMES + 60


def test_left_turn_lane_still_required():
    """Turn geometry is not DBL, so the abandon logic must not skip it."""
    enable_route("R1_EB_A_NB", dbl=True)
    controller = make_controller()
    bus = make_bus_for_leg("R1_EB_A_NB", NODE_A, "LEFT_TURN_BUS")
    # Force the bus out of its required turn lane and block the way back.
    bus.lane_index = 1
    bus.y = lane_center_y(1)
    vehicles = [bus, blocker_beside(bus)]

    run(controller, bus, vehicles, DBL_MERGE_ABANDON_FRAMES + 60)

    assert bus.target_turn == "LEFT"
    assert bus.dbl_merge_abandoned_for_leg is False
    assert bus.lane_index != DBL_LANE_INDEX
    assert bus.must_hold_for_lane is True
    assert NODE_A not in bus.passed_nodes


def test_abandon_resets_on_new_leg():
    enable_route("R3_EB_ONLY", dbl=True)
    controller = make_controller()
    bus = make_bus_for_leg("R3_EB_ONLY", NODE_A, "TWO_LEG_BUS")
    blocker = blocker_beside(bus)
    vehicles = [bus, blocker]

    run(controller, bus, vehicles, DBL_MERGE_ABANDON_FRAMES + 20)
    assert bus.dbl_merge_abandoned_for_leg is True

    # Advance to the second leg with the DBL lane clear this time.
    bus.passed_nodes.add(NODE_A)
    vehicles.remove(blocker)
    run(controller, bus, vehicles, 1)

    assert bus.get_active_route_leg(INT_X)["node_x"] == 700
    assert bus.dbl_merge_abandoned_for_leg is False
    assert bus.dbl_merge_hold_frames == 0

    run(controller, bus, vehicles, 120)
    assert bus.lane_index == DBL_LANE_INDEX
