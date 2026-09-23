"""Conventional Transit Signal Priority: bounded green extension and early
green (red truncation) that nudge the running Webster cycle, never an
exclusive single-approach green and never a full cycle restart."""
import pytest

import src.ui.control_panel as control_panel
from src.ui.canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from src.core.signal_controller import (
    ARMED,
    CANCELLED,
    COMPLETED,
    DENIED,
    NORMAL,
    REQUESTED,
    TSP_ACTION_EARLY_GREEN,
    TSP_ACTION_EXTENDING,
    TSP_ACTION_NONE,
    TSP_DENY_ETA_WINDOW,
    TSP_DENY_NET_BENEFIT,
    TSP_EARLY_TRUNCATE,
    TSP_EXTENDING,
    SignalController,
)
from src.core.vehicle import DBL_LANE_INDEX, Vehicle
from src.core.signal_controller import PRIORITY_ELIGIBILITY_MAX_PX
from tests.helpers import make_bus_for_leg, NODE_A, NODE_B


GREEN_FRAMES = 100
CAP_FRAMES = 20  # 20% of GREEN_FRAMES


def make_controller(min_green_frames=30, **kwargs):
    return SignalController(
        {"green_time": GREEN_FRAMES},
        yellow_time=2,
        red_clearance_time=2,
        min_green_frames=min_green_frames,
        **kwargs,
    )


def tsp_bus(route_id="R1_EB_A_NB", node_x=NODE_A, bus_id="TSP_BUS"):
    control_panel.bus_routes_config[route_id]["tsp_enabled"] = True
    return make_bus_for_leg(route_id, node_x, bus_id)


def step(controller, vehicles, node_x=NODE_A, move_bus=False):
    """One frame: optionally move the vehicles, then update the signals."""
    if move_bus:
        signals = controller.get_all_signals(INT_X)
        for vehicle in list(vehicles):
            vehicle.update(
                signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller
            )
    controller.update(vehicles)
    return controller.get_node_status(node_x)


def start_green(controller, phase, served_frames=0, vehicles=()):
    """Put the node at the start of `phase` (0 or 3) via a real update, then
    serve `served_frames` more of it. Counting update results from here on
    gives exactly the frames the controller served for that green."""
    controller.phase = phase - 1  # the all-red before it
    controller.timer = controller.red_clearance_time - 1
    status = step(controller, list(vehicles))
    assert status["phase_index"] == phase and status["phase_timer_frames"] == 0
    for _ in range(served_frames):
        step(controller, list(vehicles))


def assert_no_conflicting_green(signals):
    ew_green = signals["EB"] == "GREEN" or signals["WB"] == "GREEN"
    ns_green = signals["NB"] == "GREEN" or signals["SB"] == "GREEN"
    assert not (ew_green and ns_green), signals


# ---------------------------------------------------------------------------
# Unchanged controller fundamentals
# ---------------------------------------------------------------------------

def test_nodes_own_independent_state_despite_legacy_broadcast_setters():
    controller = make_controller()
    node_a = controller.nodes[NODE_A]
    node_b = controller.nodes[NODE_B]

    assert node_a is not node_b

    controller.phase = 2
    controller.timer = 7
    assert (node_a.phase, node_a.timer) == (2, 7)
    assert (node_b.phase, node_b.timer) == (2, 7)

    node_a.phase = 3
    node_a.timer = 1
    assert (node_b.phase, node_b.timer) == (2, 7)
    assert controller.get_all_signals()[NODE_A] != controller.get_all_signals()[NODE_B]


def test_node_local_clearance_can_diverge_initially_aligned_clocks():
    controller = make_controller()
    controller.phase = 2
    controller.timer = 1
    node_a_blocker = Vehicle(NODE_A, H_Y, "NB")

    controller.update([node_a_blocker])

    assert controller.get_node_status(NODE_A)["phase_index"] == 2
    assert controller.get_node_status(NODE_B)["phase_index"] == 3
    assert set(controller.get_all_signals()[NODE_A].values()) == {"RED"}
    assert controller.get_all_signals()[NODE_B] == {
        "EB": "RED", "WB": "RED", "NB": "GREEN", "SB": "GREEN"
    }


def test_normal_green_begins_only_after_all_red_box_clearance():
    controller = make_controller()
    controller.nodes[NODE_A].phase = 2
    controller.nodes[NODE_A].timer = 1
    blocker = Vehicle(NODE_A, H_Y, "NB")

    controller.update([blocker])
    assert controller.get_node_status(NODE_A)["phase_index"] == 2
    assert set(controller.get_all_signals(INT_X)[NODE_A].values()) == {"RED"}

    controller.update([])
    assert controller.get_node_status(NODE_A)["phase_index"] == 3
    assert controller.get_all_signals(INT_X)[NODE_A] == {
        "EB": "RED", "WB": "RED", "NB": "GREEN", "SB": "GREEN"
    }


def test_dbl_eligibility_succeeds_after_early_migration(signal_system):
    config = control_panel.bus_routes_config["R2_EB_B_NB"]
    config["dbl_enabled"] = True
    bus = make_bus_for_leg("R2_EB_B_NB", NODE_A)
    bus.x = NODE_A - 200
    bus.y = H_Y - 1.5 * LANE
    all_red = {
        node: {direction: "RED" for direction in ("EB", "WB", "NB", "SB")}
        for node in INT_X
    }

    assert bus.lane_index == 1
    assert not signal_system.is_bus_dbl_eligible(bus, NODE_A)

    for _ in range(60):
        bus.update(all_red, INT_X, H_Y, ROAD_W, STOP, LANE, [bus], signal_system)
        if bus.lane_index == DBL_LANE_INDEX:
            break

    assert bus.lane_index == DBL_LANE_INDEX
    assert signal_system.is_bus_dbl_eligible(bus, NODE_A)
    heavy_car = Vehicle(bus.x, bus.y, "EB", is_heavy=True, lane_index=1)
    assert not signal_system.is_bus_dbl_eligible(heavy_car, NODE_A)


def test_simultaneous_requests_are_deterministically_ordered():
    for route_id in ("R1_EB_A_NB", "R2_EB_B_NB"):
        control_panel.bus_routes_config[route_id]["tsp_enabled"] = True
    bus_b = make_bus_for_leg("R1_EB_A_NB", NODE_A, "BUS_B")
    bus_a = make_bus_for_leg("R2_EB_B_NB", NODE_A, "BUS_A")
    controller = make_controller()
    controller.update([bus_b, bus_a])
    status = controller.get_node_status(NODE_A)
    assert status["active_request"]["bus_id"] == "BUS_A"
    assert status["active_request"]["state"] == ARMED
    assert [item["bus_id"] for item in status["queued_requests"]] == ["BUS_B"]
    assert status["queued_requests"][0]["state"] == REQUESTED


def test_duplicate_eligible_frames_do_not_consume_request_ids():
    bus = tsp_bus(bus_id="STABLE_ID_BUS")
    controller = make_controller()

    request_ids = []
    for _ in range(6):
        controller.update([bus])
        request_ids.append(
            controller.get_node_status(NODE_A)["active_request"]["request_id"]
        )

    assert request_ids == ["PRIORITY_000001"] * 6
    assert controller._request_sequence == 1
    assert controller.get_node_status(NODE_A)["active_request"]["wait_frames"] == 5


def test_queued_timeout_is_terminal_and_cannot_renew_in_place():
    for route_id in ("R1_EB_A_NB", "R2_EB_B_NB"):
        control_panel.bus_routes_config[route_id]["tsp_enabled"] = True
    first = make_bus_for_leg("R1_EB_A_NB", NODE_A, "BUS_A")
    queued = make_bus_for_leg("R2_EB_B_NB", NODE_A, "BUS_B")
    controller = make_controller(priority_request_timeout=5)

    for _ in range(9):
        controller.update([first, queued])

    status = controller.get_node_status(NODE_A)
    timed_out = [
        item for item in status["terminal_history"] if item["bus_id"] == "BUS_B"
    ]
    assert len(timed_out) == 1
    assert timed_out[0]["state"] == DENIED
    assert timed_out[0]["denial_or_cancel_reason"] == "REQUEST_TIMEOUT"
    assert timed_out[0]["attempt_number"] == 1
    assert not any(item["bus_id"] == "BUS_B" for item in status["queued_requests"])

    for _ in range(10):
        controller.update([first, queued])
    assert controller._request_sequence == 2

    queued.x = -controller.get_priority_eligibility_px()
    controller.update([first, queued])
    queued.x = NODE_A - 110
    controller.update([first, queued])
    # BUS_A's armed request timed out too, so the retry may arm directly.
    status = controller.get_node_status(NODE_A)
    candidates = list(status["queued_requests"])
    if status["active_request"]:
        candidates.append(status["active_request"])
    retry = next(item for item in candidates if item["bus_id"] == "BUS_B")
    assert retry["attempt_number"] == 2
    assert retry["request_id"] == "PRIORITY_000003"


# ---------------------------------------------------------------------------
# Mechanism 1: green extension
# ---------------------------------------------------------------------------

def test_extension_holds_green_for_bus():
    """EW green about to end with the bus still upstream: the green is held
    until the bus crosses the stop bar, then the normal yellow follows and
    the request completes once the bus has passed the node."""
    bus = tsp_bus()
    controller = make_controller()
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 3
    vehicles = [bus]

    observed = []
    for _ in range(600):
        status = step(controller, vehicles, move_bus=True)
        observed.append(status["priority_state"])
        assert_no_conflicting_green(status["signals"])
        if NODE_A in bus.passed_nodes and status["active_request"] is None:
            break

    assert TSP_EXTENDING in observed
    assert observed[-1] == NORMAL
    history = controller.get_node_status(NODE_A)["terminal_history"]
    assert history[-1]["bus_id"] == bus.bus_id
    assert history[-1]["state"] == COMPLETED
    assert history[-1]["tsp_action"] == TSP_ACTION_EXTENDING
    assert 1 <= history[-1]["tsp_adjust_frames"] <= CAP_FRAMES
    # The extension ended the moment the bus cleared, not at the cap.
    assert history[-1]["tsp_adjust_frames"] < CAP_FRAMES
    node = controller.nodes[NODE_A]
    assert node.last_tsp_action == TSP_ACTION_EXTENDING
    assert node.last_tsp_adjust_frames == history[-1]["tsp_adjust_frames"]


def test_extension_withheld_when_bus_cannot_reach_the_bar_within_the_cap():
    """The extension twin of the early-green gate (T6 replay, 2026-09-21):
    a bus whose ETA at its current speed exceeds the cap would meet the red
    at the end of the held green anyway, so the cross street would pay the
    whole cap for nothing. The green ends on time, the reason is recorded
    and the request stays armed for an early green."""
    bus = tsp_bus()
    bus.x -= 3 * CAP_FRAMES  # 65 px out at 0.5 px/frame: ETA 130 > cap 20
    bus.speed = 0.5
    controller = make_controller()
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 3

    observed = [step(controller, [bus])["priority_state"] for _ in range(4)]
    assert TSP_EXTENDING not in observed
    status = controller.get_node_status(NODE_A)
    assert status["signals"]["EB"] == "YELLOW"  # ended on time
    assert status["active_request"]["state"] == ARMED
    assert status["active_request"]["tsp_action"] == TSP_ACTION_NONE
    assert status["active_request"]["tsp_gate_reason"] == TSP_DENY_ETA_WINDOW
    # Still armed: once the cross street's green runs, the early-green gate
    # gets its turn (the bus is then close enough for the cut to pay).
    later = [step(controller, [bus])["priority_state"] for _ in range(40)]
    assert TSP_EARLY_TRUNCATE in later and TSP_EXTENDING not in later

    # The same bus close enough to cross inside the cap is extended for.
    near = tsp_bus(bus_id="NEAR_BUS")
    near.speed = 0.5  # 5 px out: ETA 10 <= cap 20
    controller = make_controller()
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 3
    assert TSP_EXTENDING in [step(controller, [near])["priority_state"] for _ in range(10)]


def test_extension_capped():
    """A bus that never clears gets exactly 20% of the Webster green extra
    and not one frame more; then the green ends through yellow anyway."""
    bus = tsp_bus()  # never moved: stays upstream forever
    controller = make_controller()
    start_green(controller, 0, vehicles=[bus])

    ew_green_frames = 1  # the frame the green began on
    extending_frames = 0
    for _ in range(GREEN_FRAMES + CAP_FRAMES + 10):
        status = step(controller, [bus])
        if status["signals"]["EB"] == "GREEN":
            ew_green_frames += 1
        if status["priority_state"] == TSP_EXTENDING:
            extending_frames += 1
            assert status["tsp_action"] == TSP_ACTION_EXTENDING
            assert status["tsp_adjust_frames"] <= CAP_FRAMES

    assert extending_frames == CAP_FRAMES
    assert ew_green_frames == GREEN_FRAMES + CAP_FRAMES
    status = controller.get_node_status(NODE_A)
    assert status["priority_state"] == NORMAL
    assert status["tsp_last_action"] == TSP_ACTION_EXTENDING
    assert status["tsp_last_adjust_frames"] == CAP_FRAMES
    # The request is still live (bus is still there) but has spent its one
    # action: it now simply waits for the ordinary next green.
    assert status["active_request"]["state"] == ARMED
    assert status["active_request"]["tsp_action"] == TSP_ACTION_EXTENDING
    assert status["active_request"]["tsp_adjust_frames"] == CAP_FRAMES


def test_tsp_extends_whole_phase_not_exclusive():
    """An EW extension keeps BOTH EB and WB green with NS red -- it holds the
    phase, it never isolates the bus's own approach."""
    bus = tsp_bus()
    controller = make_controller()
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 2

    seen_extension = False
    for _ in range(CAP_FRAMES + 5):
        status = step(controller, [bus])
        if status["priority_state"] == TSP_EXTENDING:
            seen_extension = True
            assert status["signals"] == {
                "EB": "GREEN", "WB": "GREEN", "NB": "RED", "SB": "RED"
            }
    assert seen_extension


def test_extension_from_westbound_bus_also_holds_both_directions():
    bus = tsp_bus("R6_WB_ONLY", NODE_B, "WB_TSP_BUS")
    controller = make_controller()
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 2

    seen_extension = False
    for _ in range(CAP_FRAMES + 5):
        status = step(controller, [bus], node_x=NODE_B)
        if status["priority_state"] == TSP_EXTENDING:
            seen_extension = True
            assert status["signals"]["EB"] == "GREEN"
            assert status["signals"]["WB"] == "GREEN"
    assert seen_extension


# ---------------------------------------------------------------------------
# Mechanism 2: early green (red truncation)
# ---------------------------------------------------------------------------

def test_early_green_truncates_conflicting():
    """Bus arrives during NS green: NS is cut by up to 20% (never below
    MIN_GREEN), then the ordinary yellow + all-red run, then EW green."""
    bus = tsp_bus()
    controller = make_controller(min_green_frames=30)
    start_green(controller, 3, served_frames=9)  # 10 frames of NS green shown

    phases = []
    states = set()
    ns_green_frames = 10
    for _ in range(200):
        status = step(controller, [bus])
        assert_no_conflicting_green(status["signals"])
        phases.append(status["phase_index"])
        states.add(status["priority_state"])
        if status["phase_index"] == 3:
            ns_green_frames += 1
        if status["phase_index"] == 0:
            break

    assert TSP_EARLY_TRUNCATE in states
    assert ns_green_frames == GREEN_FRAMES - CAP_FRAMES
    # Proper sequence: NS green -> NS yellow -> all-red -> EW green.
    assert phases.index(4) < phases.index(5) < phases.index(0)
    status = controller.get_node_status(NODE_A)
    assert status["tsp_last_action"] == TSP_ACTION_EARLY_GREEN
    assert status["tsp_last_adjust_frames"] == CAP_FRAMES
    assert status["active_request"]["state"] == ARMED
    assert status["active_request"]["tsp_action"] == TSP_ACTION_EARLY_GREEN
    assert status["active_request"]["tsp_adjust_frames"] == CAP_FRAMES


def test_early_green_late_arrival_cuts_only_what_is_left():
    """A bus that appears after the truncation point ends the conflicting
    green now, and the recorded adjustment is only the remainder."""
    bus = tsp_bus()
    controller = make_controller(min_green_frames=30)
    start_green(controller, 3, served_frames=GREEN_FRAMES - 10)

    status = step(controller, [bus])
    # This frame becomes the last green one; the 9 after it are cut.
    assert status["tsp_last_action"] == TSP_ACTION_EARLY_GREEN
    assert status["tsp_last_adjust_frames"] == 9
    assert status["phase_index"] == 4


def test_early_green_respects_min_green():
    """Conflicting phase cannot be shortened below MIN_GREEN: with the floor
    at the full green there is nothing to cut, so no action fires and the
    bus waits for the ordinary green."""
    bus = tsp_bus()
    controller = make_controller(min_green_frames=GREEN_FRAMES)
    start_green(controller, 3, served_frames=9)

    ns_green_frames = 10
    states = set()
    for _ in range(200):
        status = step(controller, [bus])
        states.add(status["priority_state"])
        if status["phase_index"] == 3:
            ns_green_frames += 1
        if status["phase_index"] == 0:
            break

    assert states == {NORMAL}
    assert ns_green_frames == GREEN_FRAMES
    status = controller.get_node_status(NODE_A)
    assert status["tsp_last_action"] == TSP_ACTION_NONE
    assert status["active_request"]["state"] == ARMED
    assert status["active_request"]["tsp_action"] == TSP_ACTION_NONE


def test_early_green_floor_bounds_partial_cut():
    """Floor above (green - cap): the cut shrinks to what the floor allows."""
    bus = tsp_bus()
    controller = make_controller(min_green_frames=GREEN_FRAMES - 7)
    start_green(controller, 3, served_frames=9)

    ns_green_frames = 10
    for _ in range(200):
        status = step(controller, [bus])
        if status["phase_index"] == 3:
            ns_green_frames += 1
        if status["phase_index"] == 0:
            break

    assert ns_green_frames == GREEN_FRAMES - 7
    assert controller.get_node_status(NODE_A)["tsp_last_adjust_frames"] == 7


# ---------------------------------------------------------------------------
# Recovery and safety
# ---------------------------------------------------------------------------

def test_no_full_restart_after_tsp():
    """After a TSP event the controller resumes ordinary Webster progression
    from where the perturbation left it. Nothing jumps back to a fresh
    green-from-zero for the bus's phase."""
    bus = tsp_bus()
    controller = make_controller(min_green_frames=30)
    start_green(controller, 0, vehicles=[bus])

    phase_runs = [[0, 1]]  # (phase_index, frames) in order
    for _ in range(GREEN_FRAMES * 4):
        status = step(controller, [bus])
        phase = status["phase_index"]
        if phase_runs and phase_runs[-1][0] == phase:
            phase_runs[-1][1] += 1
        else:
            phase_runs.append([phase, 1])

    # Extension happened on the first EW green (bus never clears).
    assert controller.nodes[NODE_A].last_tsp_action == TSP_ACTION_EXTENDING
    sequence = [phase for phase, _frames in phase_runs]
    # Strict cyclic order, no phase repeated back-to-back and no skip to 0.
    for previous, current in zip(sequence, sequence[1:]):
        assert current == (previous + 1) % 6, sequence
    lengths = {phase: frames for phase, frames in phase_runs[1:-1]}
    # The one perturbed EW green is phase_runs[0]; every later green ran its
    # plain Webster length -- the extension was absorbed, not paid back with
    # a restart.
    assert phase_runs[0] == [0, GREEN_FRAMES + CAP_FRAMES]
    assert lengths[3] == GREEN_FRAMES
    assert lengths[0] == GREEN_FRAMES


def test_no_conflicting_green_during_tsp():
    """Across an extension, an early green, and the ordinary cycles between
    them, no EW and NS approach are ever green in the same frame."""
    bus_a = tsp_bus(bus_id="TSP_A")
    controller = make_controller(min_green_frames=30)
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 3
    vehicles = [bus_a]
    actions = []
    for frame in range(GREEN_FRAMES * 6):
        if frame == 40:
            # Swap in a fresh bus while NS is green so early green fires.
            vehicles = [tsp_bus(bus_id="TSP_B")]
        status = step(controller, vehicles)
        assert_no_conflicting_green(status["signals"])
        assert_no_conflicting_green(controller.get_all_signals(INT_X)[NODE_B])
        if status["tsp_last_action"] not in actions:
            actions.append(status["tsp_last_action"])
    assert TSP_ACTION_EXTENDING in actions
    assert TSP_ACTION_EARLY_GREEN in actions


@pytest.mark.parametrize(
    "wrong_lane,must_hold_for_lane",
    ((True, False), (False, True)),
)
def test_infeasible_bus_no_tsp(wrong_lane, must_hold_for_lane):
    """The feasibility gate is checked before either mechanism: a bus in the
    wrong lane, or one that must hold for a lane change, gets no extension
    and no early green."""
    route_id = "R4_WB_A_SB"
    control_panel.bus_routes_config[route_id]["tsp_enabled"] = True
    bus = make_bus_for_leg(route_id, NODE_A, "INFEASIBLE_BUS")
    if wrong_lane:
        bus.lane_index = 1
        bus.y = H_Y + 1.5 * LANE
    bus.must_hold_for_lane = must_hold_for_lane

    # Extension opportunity: WB bus, EW green ending.
    controller = make_controller(min_green_frames=30)
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 2
    states = set()
    ew_green_frames = 0
    for _ in range(CAP_FRAMES + 5):
        status = step(controller, [bus])
        states.add(status["priority_state"])
        if status["signals"]["WB"] == "GREEN":
            ew_green_frames += 1
    assert states == {NORMAL}
    assert ew_green_frames == 1  # only the one frame the timer still allowed

    # Early-green opportunity: NS green running.
    controller = make_controller(min_green_frames=30)
    start_green(controller, 3, served_frames=9)
    ns_green_frames = 10
    for _ in range(200):
        status = step(controller, [bus])
        assert status["priority_state"] == NORMAL
        if status["phase_index"] == 3:
            ns_green_frames += 1
        if status["phase_index"] == 0:
            break
    assert ns_green_frames == GREEN_FRAMES
    assert controller.get_node_status(NODE_A)["tsp_last_action"] == TSP_ACTION_NONE
    assert (
        controller.get_node_status(NODE_A)["active_request"]["tsp_action"]
        == TSP_ACTION_NONE
    )


def test_bounded_tsp_cannot_gridlock():
    """A relentless stream of TSP buses at one node still cannot starve the
    cross-street: every NS green keeps at least MIN_GREEN, and NS is never
    kept waiting longer than one EW green plus the cap plus the transitions."""
    controller = make_controller(min_green_frames=30)
    controller.phase = 0
    controller.timer = 0
    sequence = 0
    vehicles = [tsp_bus(bus_id="STREAM_0")]

    ns_runs = []
    frames_without_ns = 0
    worst_gap = 0
    tsp_events = 0
    for _ in range(GREEN_FRAMES * 30):
        status = step(controller, vehicles)
        active = status["active_request"]
        if active and active["tsp_action"] != TSP_ACTION_NONE:
            # This bus has had its one nudge; replace it with a fresh one so
            # the node faces a new request every cycle.
            sequence += 1
            tsp_events += 1
            vehicles = [tsp_bus(bus_id=f"STREAM_{sequence}")]
        if status["signals"]["NB"] == "GREEN":
            if not ns_runs or frames_without_ns:
                ns_runs.append(0)
            ns_runs[-1] += 1
            worst_gap = max(worst_gap, frames_without_ns)
            frames_without_ns = 0
        else:
            frames_without_ns += 1

    assert tsp_events >= 10
    assert len(ns_runs) >= 5
    assert min(ns_runs) >= controller.min_green_frames
    transitions = 2 * controller.yellow_time + 2 * controller.red_clearance_time
    assert worst_gap <= GREEN_FRAMES + CAP_FRAMES + transitions


def test_removed_bus_ends_extension_through_yellow():
    bus = tsp_bus()
    controller = make_controller()
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 2
    for _ in range(5):
        status = step(controller, [bus])
        if status["priority_state"] == TSP_EXTENDING:
            break
    assert status["priority_state"] == TSP_EXTENDING

    status = step(controller, [])
    assert status["priority_state"] == NORMAL
    assert status["phase_index"] == 1
    assert status["signals"] == {
        "EB": "YELLOW", "WB": "YELLOW", "NB": "RED", "SB": "RED"
    }
    assert status["active_request"] is None
    assert status["terminal_history"][-1]["state"] == CANCELLED
    assert status["terminal_history"][-1]["denial_or_cancel_reason"] == "BUS_REMOVED"


def test_live_disable_during_extension_ends_it_through_yellow():
    bus = tsp_bus()
    controller = make_controller()
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 2
    for _ in range(5):
        status = step(controller, [bus])
        if status["priority_state"] == TSP_EXTENDING:
            break
    assert status["priority_state"] == TSP_EXTENDING

    control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] = False
    status = step(controller, [bus])
    assert status["priority_state"] == NORMAL
    assert status["phase_index"] == 1
    assert status["terminal_history"][-1]["denial_or_cancel_reason"] == "FEATURE_DISABLED"


def test_dbl_only_reserves_lane_without_touching_signals():
    """A DBL request arms immediately (cars yield in its lane) but never
    adjusts signal timing on its own."""
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config["dbl_enabled"] = True
    config["tsp_enabled"] = False
    bus = make_bus_for_leg("R1_EB_A_NB", NODE_A, "DBL_BUS")
    bus.lane_index = DBL_LANE_INDEX
    bus.y = H_Y - (DBL_LANE_INDEX + 0.5) * LANE
    controller = make_controller(min_green_frames=30)
    start_green(controller, 3, served_frames=9)

    ns_green_frames = 10
    for _ in range(200):
        status = step(controller, [bus])
        assert status["priority_state"] == NORMAL
        assert controller.get_active_dbl_request(NODE_A, "EB")["bus_id"] == "DBL_BUS"
        assert controller.is_dbl_active_for_approach(NODE_A, "EB")
        if status["phase_index"] == 3:
            ns_green_frames += 1
        if status["phase_index"] == 0:
            break
    assert ns_green_frames == GREEN_FRAMES
    assert controller.get_all_dbl_states()[NODE_A]["EB"] == "ACTIVE"
    assert status["active_request"]["tsp_action"] == TSP_ACTION_NONE


def test_node_status_exposes_tsp_measurement_fields():
    bus = tsp_bus()
    controller = make_controller()
    controller.phase = 0
    controller.timer = GREEN_FRAMES - 2

    idle = step(controller, [bus])
    assert idle["tsp_action"] == TSP_ACTION_NONE
    assert idle["tsp_adjust_frames"] == 0

    extending = step(controller, [bus])
    assert extending["priority_state"] == TSP_EXTENDING
    assert extending["tsp_action"] == TSP_ACTION_EXTENDING
    assert extending["tsp_adjust_frames"] == 1
    assert controller.tsp_extension_timer == 1


# ---------------------------------------------------------------------------
# Early-green arrival-time gate (audit: tsp-early-green-mistiming)
# ---------------------------------------------------------------------------

def place_bus(bus, distance_px, speed):
    """Put an EB bus ``distance_px`` short of Node A's stop bar at ``speed``."""
    stop_bar_x = NODE_A - ROAD_W // 2 - STOP
    bus.x = stop_bar_x - distance_px - bus.length / 2.0
    bus.speed = speed


def run_conflicting_green(controller, bus, frames=200):
    """Serve the NS green with the EB bus present; return the states seen and
    the frame the bus's own green opened (None if it did not)."""
    states = set()
    green_opened = None
    for index in range(frames):
        status = step(controller, [bus])
        assert_no_conflicting_green(status["signals"])
        states.add(status["priority_state"])
        if status["phase_index"] == 0 and green_opened is None:
            green_opened = index
    return states, green_opened


def test_t1_far_slow_bus_does_not_trigger_early_green():
    """A bus ~3000 frames from the bar cannot use a green that ends in
    ~200 frames: the request stays armed and untreated."""
    bus = tsp_bus()
    place_bus(bus, distance_px=390, speed=0.13)
    controller = make_controller(min_green_frames=30)
    start_green(controller, 3, served_frames=9)

    states, _ = run_conflicting_green(controller, bus, frames=60)

    assert TSP_EARLY_TRUNCATE not in states
    request = controller.get_node_status(NODE_A)["active_request"]
    assert request["state"] == ARMED
    assert request["tsp_action"] == TSP_ACTION_NONE
    assert request["tsp_gate_reason"] == TSP_DENY_ETA_WINDOW


def test_t2_near_bus_gets_early_green_and_crosses_inside_it():
    """A bus whose ETA lands inside the brought-forward green is served by
    it: the truncation fires and the bus crosses during that green."""
    bus = tsp_bus()
    place_bus(bus, distance_px=120, speed=0.45)  # ETA ~267 frames
    # 100-frame greens, 20-frame cap: served 60 leaves 20 truncated frames,
    # then 4 of clearance, so the bus green spans frames [24, 124] from now.
    controller = make_controller(min_green_frames=30)
    start_green(controller, 3, served_frames=60)

    cross_frame = None
    green_window = None
    for index in range(400):
        signals = controller.get_all_signals(INT_X)
        bus.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, [bus], controller)
        status = step(controller, [bus])
        if status["phase_index"] == 0 and green_window is None:
            green_window = [index, None]
        if green_window and green_window[1] is None and status["phase_index"] == 1:
            green_window[1] = index
        if cross_frame is None and not bus.is_front_bumper_upstream(
            NODE_A, H_Y, ROAD_W, STOP
        ):
            cross_frame = index
        if cross_frame is not None and green_window and green_window[1]:
            break

    assert controller.get_node_status(NODE_A)["tsp_last_action"] == TSP_ACTION_EARLY_GREEN
    assert green_window[0] <= cross_frame <= green_window[1], (
        cross_frame, green_window
    )


def test_t3_cut_below_clearance_is_denied_with_reason():
    """A truncation that cannot pay for its own yellow + all-red never
    fires; once the bus crosses untreated the request ends DENIED."""
    bus = tsp_bus()  # at the bar, so it crosses on the ordinary green
    controller = make_controller(min_green_frames=30)
    # 3 frames left to cut <= yellow (2) + all-red (2).
    start_green(controller, 3, served_frames=GREEN_FRAMES - 4)

    for _ in range(400):
        signals = controller.get_all_signals(INT_X)
        bus.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, [bus], controller)
        status = step(controller, [bus])
        if NODE_A in bus.passed_nodes:
            break
    assert NODE_A in bus.passed_nodes

    assert status["tsp_last_action"] == TSP_ACTION_NONE
    terminal = controller.get_latest_terminal_status_for_bus(bus, NODE_A)
    assert terminal["state"] == DENIED
    assert terminal["tsp_action"] == TSP_ACTION_NONE
    assert terminal["denial_or_cancel_reason"] == TSP_DENY_NET_BENEFIT


def test_t4_eligibility_is_clamped_to_link_length(caplog):
    assert PRIORITY_ELIGIBILITY_MAX_PX == INT_X[1] - INT_X[0] == 800
    with caplog.at_level("WARNING", logger="src.core.signal_controller"):
        controller = SignalController(
            {"green_time": GREEN_FRAMES, "priority_eligibility_px": 1200}
        )
    assert controller.get_priority_eligibility_px() == 800
    assert "clamped" in caplog.text

    bus = tsp_bus()
    place_bus(bus, distance_px=850, speed=0.5)
    assert not controller.is_bus_tsp_eligible(bus, NODE_A)
    place_bus(bus, distance_px=790, speed=0.5)
    assert controller.is_bus_tsp_eligible(bus, NODE_A)
