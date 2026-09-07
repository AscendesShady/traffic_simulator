import inspect

import control_panel
import main
from canvas_gemini import INT_X
from signal_controller import (
    DISCHARGE_INACTIVE,
    DISCHARGE_PLAN_STAGES,
    DISCHARGE_RECOVERY_FAILED,
    NORMAL,
    PRIORITY_ACTIVE,
    NodeState,
    SignalController,
)
from tests.helpers import make_bus_for_leg


APPROACHES = ("EB", "WB", "NB", "SB")


def test_full_reset_restores_construction_state():
    config = {
        "green_time": 91,
        "discharge_start_requested": True,
        "discharge_stop_requested": True,
    }
    controller = SignalController(
        config,
        yellow_time=7,
        red_clearance_time=9,
        priority_request_timeout=111,
        discharge_min_green=13,
        discharge_max_green=71,
        discharge_stall_time=17,
        discharge_queue_target=2,
    )
    original_nodes = dict(controller.nodes)

    controller.frame_number = 321
    controller._request_sequence = 12
    controller._attempt_counts[("BUS_RESET", 300, 0)] = 3
    node = controller.nodes[300]
    node.phase = 4
    node.timer = 22
    node.priority_state = PRIORITY_ACTIVE
    node.priority_timer = 8
    node.active_request = object()
    node.request_queue.append(object())
    node.reservations[123] = {"vehicle": object()}
    node.terminal_history.append({"state": "COMPLETED"})
    node.suppressed_keys.add(("BUS_RESET", 300, 0))
    controller.discharge_active = True
    controller.discharge_mode = "Eastbound Corridor"
    controller.discharge_state = DISCHARGE_RECOVERY_FAILED
    controller.discharge_timer = 44
    controller.discharge_plan_name = "Eastbound Corridor"
    controller.discharge_stage_index = 1
    controller.discharge_reason = "mutated"
    controller.discharge_recommendation = "mutated"
    controller.discharge_vehicles_discharged = 15
    controller.discharge_cycles = 4
    controller._discharge_transition_signals[300]["EB"] = "GREEN"
    controller._discharge_green_map = {300: "EB"}
    controller._discharge_stop_after_clearance = True
    controller._discharge_completed = True
    controller._discharge_last_progress_frame = 300
    controller._discharge_wait_snapshot = {"upstream": 10}
    controller._discharge_tracked[(1, 300)] = object()
    controller._discharge_last_served["Eastbound Corridor"] = 299
    config["discharge_start_requested"] = True
    config["discharge_stop_requested"] = True

    controller.reset_all_state()

    assert controller.frame_number == 0
    assert controller._request_sequence == 0
    assert controller._attempt_counts == {}
    assert set(controller.nodes) == set(INT_X)
    for node_x, state in controller.nodes.items():
        assert state is not original_nodes[node_x]
        assert state == NodeState()
    assert controller.discharge_active is False
    assert controller.discharge_mode == control_panel.DISCHARGE_AUTO
    assert controller.discharge_state == DISCHARGE_INACTIVE
    assert controller.discharge_timer == 0
    assert controller.discharge_plan_name is None
    assert controller.discharge_stage_index == 0
    assert controller.discharge_reason == "Normal signal control is active"
    assert controller.discharge_recommendation == (
        "Select Auto or a corridor, then start discharge"
    )
    assert controller.discharge_vehicles_discharged == 0
    assert controller.discharge_cycles == 0
    assert controller._discharge_transition_signals == {
        node_x: {approach: "RED" for approach in APPROACHES}
        for node_x in INT_X
    }
    assert controller._discharge_green_map == {}
    assert controller._discharge_stop_after_clearance is False
    assert controller._discharge_completed is False
    assert controller._discharge_last_progress_frame == 0
    assert controller._discharge_wait_snapshot is None
    assert controller._discharge_tracked == {}
    assert controller._discharge_last_served == {
        plan_name: -controller.discharge_max_green
        for plan_name in DISCHARGE_PLAN_STAGES
    }
    assert config["discharge_start_requested"] is False
    assert config["discharge_stop_requested"] is False
    assert config["discharge_runtime"]["controller_state"] == DISCHARGE_INACTIVE


def test_reset_clears_active_priority():
    control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300, "RESET_ACTIVE_BUS")
    controller = SignalController(
        {"green_time": 100}, yellow_time=2, red_clearance_time=2
    )

    for _ in range(20):
        controller.update([bus])
        if controller.nodes[300].priority_state == PRIORITY_ACTIVE:
            break

    assert controller.nodes[300].priority_state == PRIORITY_ACTIVE
    assert controller.nodes[300].active_request is not None

    controller.reset_all_state()

    for node in controller.nodes.values():
        assert node.priority_state == NORMAL
        assert node.priority_timer == 0
        assert node.active_request is None
        assert node.request_queue == []
        assert node.reservations == {}


def test_sim_and_controller_frames_reset_together():
    reset_helper = inspect.getsource(main.perform_full_reset)
    main_source = inspect.getsource(main.main)
    assert "signals.reset_all_state()" in reset_helper
    assert "signals.reset_discharge()" not in reset_helper
    assert main_source.count(
        "master_frame_count = perform_full_reset(vehicles, signals, telemetry)"
    ) == 2

    controller = SignalController({"green_time": 30})
    for _ in range(5):
        controller.update([])
    simulated_master_frame = 5

    controller.reset_all_state()
    simulated_master_frame = 0

    assert simulated_master_frame == controller.frame_number == 0


def test_config_survives_reset():
    config = {"green_time": 73, "custom_config_marker": "preserve-me"}
    controller = SignalController(
        config,
        yellow_time=5,
        red_clearance_time=6,
        priority_request_timeout=701,
        discharge_min_green=81,
        discharge_max_green=509,
        discharge_stall_time=123,
        discharge_queue_target=4,
    )
    expected = {
        "yellow_time": controller.yellow_time,
        "red_clearance_time": controller.red_clearance_time,
        "priority_request_timeout": controller.priority_request_timeout,
        "priority_active_stall_frames": controller.priority_active_stall_frames,
        "priority_active_max_frames": controller.priority_active_max_frames,
        "discharge_min_green": controller.discharge_min_green,
        "discharge_max_green": controller.discharge_max_green,
        "discharge_stall_time": controller.discharge_stall_time,
        "discharge_queue_target": controller.discharge_queue_target,
    }

    controller.frame_number = 999
    controller.discharge_max_green = expected["discharge_max_green"]
    controller.reset_all_state()

    assert controller.global_config is config
    assert config["green_time"] == 73
    assert config["custom_config_marker"] == "preserve-me"
    assert {
        name: getattr(controller, name)
        for name in expected
    } == expected
