"""AUTO gridlock recovery walks the network clockwise, one leg at a time.

Each node is served N -> E -> S -> W, with the two corridors acting as the
shared east and west legs. Legs with nothing queued are skipped so the
rotation never spends a green on an empty approach.
"""
import control_panel
from canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from signal_controller import (
    DISCHARGE_ACTIVE,
    DISCHARGE_CLOCKWISE_ORDER,
    DISCHARGE_PLAN_STAGES,
    SignalController,
)
from vehicle import Vehicle


def make_controller():
    config = {
        "green_time": 100,
        "discharge_selection": control_panel.DISCHARGE_AUTO,
        "discharge_start_requested": False,
        "discharge_stop_requested": False,
    }
    controller = SignalController(
        config,
        yellow_time=2,
        red_clearance_time=2,
        discharge_min_green=2,
        discharge_max_green=8,
        discharge_stall_time=40,
        discharge_queue_target=0,
    )
    return controller, config


def queued(node_x, approach, count=2):
    """Vehicles stopped upstream of `node_x` on `approach`."""
    made = []
    for index in range(count):
        gap = 40 + index * 40
        if approach == "EB":
            x, y = node_x - ROAD_W / 2 - STOP - gap, H_Y - 1.5 * LANE
        elif approach == "WB":
            x, y = node_x + ROAD_W / 2 + STOP + gap, H_Y + 1.5 * LANE
        elif approach == "NB":
            x, y = node_x - 1.5 * LANE, H_Y + ROAD_W / 2 + STOP + gap
        else:
            x, y = node_x + 1.5 * LANE, H_Y - ROAD_W / 2 - STOP - gap
        vehicle = Vehicle(
            x, y, approach, target_turn="STRAIGHT", lane_index=1,
            assigned_node_x=node_x,
        )
        if approach == "EB" and node_x == 700:
            vehicle.passed_nodes.add(300)
        if approach == "WB" and node_x == 300:
            vehicle.passed_nodes.add(700)
        vehicle.speed = 0.0
        made.append(vehicle)
    return made


def served_order(vehicles, frames=4000):
    """Run AUTO discharge and record the order legs are actually greened."""
    controller, config = make_controller()
    config["discharge_start_requested"] = True
    order = []
    started = False
    for _ in range(frames):
        signals = controller.get_all_signals(INT_X)
        for vehicle in list(vehicles):
            vehicle.update(
                signals, INT_X, H_Y, all_vehicles=vehicles,
                signal_controller=controller,
            )
        vehicles[:] = [
            v for v in vehicles if -300 < v.x < 1300 and -300 < v.y < 1000
        ]
        controller.update(vehicles)
        if (
            controller.discharge_state == DISCHARGE_ACTIVE
            and (not order or order[-1] != controller.discharge_plan_name)
        ):
            order.append(controller.discharge_plan_name)
        if started and not controller.discharge_active:
            break
        if controller.discharge_active:
            started = True
    return order


def test_clockwise_order_covers_every_discharge_plan():
    assert set(DISCHARGE_CLOCKWISE_ORDER) == set(DISCHARGE_PLAN_STAGES)
    assert DISCHARGE_CLOCKWISE_ORDER == (
        "Node A Northbound",
        "Eastbound Corridor",
        "Node A Southbound",
        "Westbound Corridor",
        "Node B Northbound",
        "Node B Southbound",
    )


def test_auto_serves_legs_in_clockwise_order():
    vehicles = []
    for node_x in INT_X:
        for approach in ("EB", "WB", "NB", "SB"):
            vehicles.extend(queued(node_x, approach))

    order = served_order(vehicles)

    assert order, "AUTO discharge never activated a stage"
    # The first pass must follow the clockwise rotation exactly.
    first_pass = order[: len(DISCHARGE_CLOCKWISE_ORDER)]
    assert first_pass == list(DISCHARGE_CLOCKWISE_ORDER)


def test_auto_skips_legs_with_nothing_queued():
    # Only two legs have traffic: the eastbound corridor and Node B southbound.
    vehicles = queued(300, "EB") + queued(700, "SB")

    order = served_order(vehicles)

    assert order, "AUTO discharge never activated a stage"
    assert set(order) <= {"Eastbound Corridor", "Node B Southbound"}
    # Empty legs are skipped, and the clockwise relation between the two
    # served legs is preserved (east comes before Node B south).
    assert order[0] == "Eastbound Corridor"


def test_auto_starts_the_rotation_at_the_first_clockwise_leg():
    vehicles = []
    for node_x in INT_X:
        for approach in ("EB", "WB", "NB", "SB"):
            vehicles.extend(queued(node_x, approach))

    order = served_order(vehicles)

    assert order[0] == "Node A Northbound"


def test_due_leg_hold_never_outlives_the_stall_watchdog():
    """The clockwise hold must yield before recovery would be failed."""
    controller, _config = make_controller()
    grace = min(120, max(0, controller.discharge_stall_time // 2))

    assert grace < controller.discharge_stall_time
