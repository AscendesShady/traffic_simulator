"""Turning movements: straight, a left at either node, or two lefts."""
import random

import src.ui.control_panel as control_panel
from src.core import main
from src.core.signal_controller import SignalController
from src.core.vehicle import DBL_LANE_INDEX, Vehicle
from src.ui.canvas_gemini import H_Y, HEIGHT, INT_X, LANE, ROAD_W, STOP, WIDTH
from tests.helpers import NODE_A, NODE_B

LANES = {
    "EB": [H_Y - (i + 0.5) * LANE for i in range(3)],
    "A_NB": [NODE_A - (i + 0.5) * LANE for i in range(3)],
    "B_NB": [NODE_B - (i + 0.5) * LANE for i in range(3)],
    "A_SB": [NODE_A + (i + 0.5) * LANE for i in range(3)],
}


def drive(vehicle, frames, controller=None):
    """Step one vehicle alone under permanent EW green (NS red) until it
    leaves the world; returns its exit (direction, x, y)."""
    controller = controller or SignalController({"green_time": 100000}, 60, 60)
    vehicles = [vehicle]
    for _ in range(frames):
        controller.update(vehicles)
        signals = controller.get_all_signals(INT_X)
        vehicle.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
        if not (-60 <= vehicle.x <= WIDTH + 60 and -60 <= vehicle.y <= HEIGHT + 60):
            break
    return vehicle.direction, vehicle.x, vehicle.y


def test_far_node_left_goes_straight_through_a_and_turns_north_at_b():
    car = Vehicle(-20, LANES["EB"][0], "EB", max_speed=1.0, lane_index=0, left_nodes=(NODE_B,))
    lanes_at_a = []
    controller = SignalController({"green_time": 100000}, 60, 60)
    vehicles = [car]
    for _ in range(6000):
        controller.update(vehicles)
        car.update(controller.get_all_signals(INT_X), INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
        if abs(car.x - NODE_A) < 5:
            lanes_at_a.append(car.lane_index)
        if car.y < -60:
            break
    assert car.direction == "NB" and NODE_A in car.passed_nodes and NODE_B in car.passed_nodes
    assert set(lanes_at_a) == {0}            # straight through A in its own lane
    assert abs(car.x - (NODE_B - 2.5 * LANE)) < 1  # turned from lane 2 at B


def test_double_left_from_south_of_b_exits_south_of_a():
    car = Vehicle(LANES["B_NB"][2], HEIGHT + 20, "NB", max_speed=1.0,
                  lane_index=DBL_LANE_INDEX, assigned_node_x=NODE_B, left_nodes=(NODE_B, NODE_A))
    # Permissive lefts need no green, only a free box; NS gets the green here.
    controller = SignalController({"green_time": 100000}, 60, 60)
    controller.phase = 3
    direction, x, y = drive(car, 8000, controller)
    assert direction == "SB" and y > HEIGHT
    assert abs(x - (NODE_A + 2.5 * LANE)) < 1
    assert car.passed_nodes == {NODE_A, NODE_B}


def test_double_left_from_north_of_a_exits_north_of_b():
    car = Vehicle(LANES["A_SB"][2], -20, "SB", max_speed=1.0,
                  lane_index=DBL_LANE_INDEX, assigned_node_x=NODE_A, left_nodes=(NODE_A, NODE_B))
    controller = SignalController({"green_time": 100000}, 60, 60)
    controller.phase = 3
    direction, x, y = drive(car, 8000, controller)
    assert direction == "NB" and y < 0
    assert abs(x - (NODE_B - 2.5 * LANE)) < 1


def test_spawner_draws_the_three_way_split():
    random.seed(5)
    main.reset_all_spawner_states()
    cfg = {"model": "Poisson", "rate": 60, "turn_split": 0.5, "left_far_share": 0.3, "heavy_ratio": 0.0}
    counts = {"straight": 0, "near": 0, "far": 0}
    for _ in range(40000):   # ~11 sim-minutes at 60 v/m
        vehicles = []
        main.try_spawn_vehicle(vehicles, "EB", "EB", -20, LANES["EB"], cfg, min_gap=0)
        for v in vehicles:
            if not v.left_nodes:
                counts["straight"] += 1
            elif v.left_nodes == (NODE_A,):
                counts["near"] += 1
                assert v.lane_index == DBL_LANE_INDEX and v.target_turn == "LEFT"
            else:
                assert v.left_nodes == (NODE_B,)
                counts["far"] += 1
                assert v.lane_index in (0, 1) and v.target_turn == "STRAIGHT"
    total = sum(counts.values())
    assert total > 200
    assert abs(counts["straight"] / total - 0.5) < 0.06
    assert abs(counts["far"] / total - 0.3) < 0.06
    main.reset_all_spawner_states()


def test_single_node_approaches_never_get_a_second_option():
    for key in ("A_NB", "B_SB"):
        assert len(control_panel.APPROACH_TURN_OPTIONS[key]) == 1
    random.seed(1)
    main.reset_all_spawner_states()
    cfg = {"model": "Poisson", "rate": 60, "turn_split": 0.0, "left_far_share": 0.9, "heavy_ratio": 0.0}
    vehicles = []
    for _ in range(300):
        main.try_spawn_vehicle(vehicles, "A_NB", "NB", HEIGHT + 20, LANES["A_NB"], cfg, min_gap=0)
    assert vehicles and all(v.left_nodes == (NODE_A,) for v in vehicles)
    main.reset_all_spawner_states()
