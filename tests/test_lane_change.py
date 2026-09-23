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


def test_render_position_interpolates_between_steps_but_not_across_a_pivot():
    import pygame
    v = car(100, 0, 0.7)
    run([v], 1)
    assert (v.prev_x, v.x) == (100.0, 100.7)
    assert v.render_position(0.5) == (100.35, v.y)
    assert v.render_position(1.0) == (v.x, v.y)
    # In view space a fractional world x lands on its own screen pixel.
    view = pygame.Rect(90, v.y - 20, 40, 40)
    assert v.body_rect(0.5, view, 6.0).x == round((100.35 - 90 - 9) * 6)
    v.direction = "NB"                   # pivoted this step: snap, no lerp
    assert v.render_position(0.5) == (v.x, v.y)


def test_chooser_refuses_a_lane_whose_follower_would_have_to_brake():
    slow = car(NODE_A - 500, 0, 0.30)
    fast = car(NODE_A - 530, 0, 0.70)  # 12 px behind the leader: held to ~0.28
    assert fast.choose_discretionary_lane(H_Y, LANE, [slow, fast]) == 1
    tail = car(NODE_A - 560, 1, 0.70)  # 12 px behind the fast car in lane 1
    assert fast.choose_discretionary_lane(H_Y, LANE, [slow, fast, tail]) is None


def test_follower_parked_at_following_distance_does_not_pin_a_mid_slide_truck():
    """Snapshot from a live WB gridlock at Node A: a left-turning truck frozen
    straddling lanes 1/2 because the truck queued behind it in lane 2 had
    stopped at SAFE_GAP_PX -- inside the old 15 px lateral margin -- so the
    slide never finished and the whole corridor sat behind an empty bar."""
    random.seed(3)
    wb_y = lambda i: H_Y + (i + 0.5) * LANE
    mover = Vehicle(924.6, wb_y(1) + 12.5, "WB", 0.6, is_heavy=True,
                    target_turn="LEFT", lane_index=1)
    mover.lane_vacate_target = 2
    follower = Vehicle(964.5, wb_y(2), "WB", 0.6, is_heavy=True,
                       target_turn="LEFT", lane_index=2)
    gap = (follower.x - follower.length / 2) - (mover.x + mover.length / 2)
    assert 11 < gap < 13  # parked exactly where the following law stops it
    run([mover, follower], 120)
    assert mover.lane_index == 2 and mover.y == wb_y(2)
