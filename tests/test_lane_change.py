"""Discretionary (MOBIL-lite) lane changes for straight cars."""
import random

from src.core.signal_controller import SignalController
from src.core.vehicle import (
    DISCRETIONARY_LANES,
    LANE_CHANGE_MIN_DIST_TO_BAR_PX,
    Vehicle,
)
from src.ui.canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from tests.helpers import NODE_A


def lane_y(lane_index):
    return H_Y - (lane_index + 0.5) * LANE


def car(x, lane_index, max_speed, turn="STRAIGHT"):
    return Vehicle(
        x, lane_y(lane_index), "EB", max_speed=max_speed,
        target_turn=turn, lane_index=lane_index,
    )


def run(vehicles, frames, controller=None):
    """Step the vehicles; returns each vehicle's lane index per frame."""
    controller = controller or SignalController({"green_time": 6000}, 60, 60)
    lanes = {id(v): [] for v in vehicles}
    for _ in range(frames):
        controller.update(vehicles)
        signals = controller.get_all_signals(INT_X)
        for vehicle in vehicles:
            vehicle.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
            lanes[id(vehicle)].append(vehicle.lane_index)
    return lanes


def test_fast_car_overtakes_slow_leader_then_keeps_outer():
    random.seed(3)
    slow = car(NODE_A - 500, 0, 0.30)
    fast = car(NODE_A - 560, 0, 0.70)
    lanes = run([slow, fast], 600)[id(fast)]
    assert 1 in lanes  # pulled into the inner lane to pass
    assert fast.x > slow.x + 60  # and actually got past
    assert lanes[-1] == 0  # then drifted back out (keep-outer bias)
    assert fast.y == lane_y(0)


def test_free_flowing_car_keeps_to_the_outer_lane():
    random.seed(3)
    lone = car(NODE_A - 600, 1, 0.60)
    run([lone], 400)
    assert lone.lane_index == 0


def test_left_turners_and_the_stop_bar_approach_are_left_alone():
    random.seed(3)
    parked = car(NODE_A - 120, 0, 0.0)
    stuck = car(NODE_A - 160, 0, 0.70)  # inside LANE_CHANGE_MIN_DIST_TO_BAR_PX
    assert 0 < stuck.distance_to_node_stop_bar(NODE_A, H_Y, ROAD_W, STOP) < LANE_CHANGE_MIN_DIST_TO_BAR_PX
    run([parked, stuck], 300)
    assert stuck.lane_index == 0

    leader = car(NODE_A - 500, 2, 0.0, turn="LEFT")
    left = car(NODE_A - 560, 2, 0.70, turn="LEFT")
    run([leader, left], 300)
    assert left.lane_index == 2
    assert 2 not in DISCRETIONARY_LANES


def test_chooser_refuses_a_lane_whose_follower_would_have_to_brake():
    slow = car(NODE_A - 500, 0, 0.30)
    fast = car(NODE_A - 530, 0, 0.70)  # 12 px behind the leader: held to ~0.28
    assert fast.choose_discretionary_lane(H_Y, LANE, [slow, fast]) == 1
    tail = car(NODE_A - 560, 1, 0.70)  # 12 px behind the fast car in lane 1
    assert fast.choose_discretionary_lane(H_Y, LANE, [slow, fast, tail]) is None
