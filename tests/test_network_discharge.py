import random

import control_panel
import main
from canvas_gemini import HEIGHT, H_Y, INT_X, LANE, ROAD_W, STOP, WIDTH
from signal_controller import (
    DISCHARGE_ACTIVE,
    DISCHARGE_ALL_RED,
    DISCHARGE_INACTIVE,
    DISCHARGE_STOPPING_ALL_RED,
    DISCHARGE_STOPPING_YELLOW,
    DISCHARGE_TRANSITION_YELLOW,
    DISCHARGE_WAITING,
    SignalController,
)
from telemetry_dashboard import COLOR_WARNING, TelemetryDashboard
from telemetry_exporter import TelemetryExporter
from tests.helpers import make_bus_for_leg, rectangles_overlap
from vehicle import Vehicle


def make_controller(selection=control_panel.DISCHARGE_AUTO):
    config = {
        "green_time": 100,
        "discharge_selection": selection,
        "discharge_start_requested": False,
        "discharge_stop_requested": False,
    }
    controller = SignalController(
        config,
        yellow_time=2,
        red_clearance_time=2,
        discharge_min_green=2,
        discharge_max_green=8,
        discharge_stall_time=4,
        discharge_queue_target=0,
    )
    return controller, config


def eb_vehicle_before_node(node_x):
    x = 100 if node_x == 300 else 500
    vehicle = Vehicle(
        x,
        H_Y - 1.5 * LANE,
        "EB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    if node_x == 700:
        vehicle.passed_nodes.add(300)
    vehicle.speed = 0.0
    return vehicle


def vertical_vehicle(node_x, direction="NB", offset=0):
    lane_x = node_x - 1.5 * LANE if direction == "NB" else node_x + 1.5 * LANE
    y = H_Y + ROAD_W / 2 + STOP + 80 + offset if direction == "NB" else H_Y - ROAD_W / 2 - STOP - 80 - offset
    vehicle = Vehicle(
        lane_x,
        y,
        direction,
        target_turn="STRAIGHT",
        lane_index=1,
        assigned_node_x=node_x,
    )
    vehicle.speed = 0.0
    return vehicle


def request_discharge(controller, config, vehicles, selection):
    config["discharge_selection"] = selection
    config["discharge_start_requested"] = True
    controller.update(vehicles)
    assert controller.discharge_state == DISCHARGE_TRANSITION_YELLOW


def advance_until(controller, vehicles, predicate, limit=40):
    observed = []
    for _ in range(limit):
        controller.update(vehicles)
        observed.append(controller.discharge_state)
        if predicate():
            return observed
    raise AssertionError(
        f"Discharge condition not reached; states={observed}, "
        f"status={controller.get_discharge_status()}"
    )


def assert_exclusive_greens(signals, expected):
    for node_x, node_signals in signals.items():
        green = [key for key, value in node_signals.items() if value == "GREEN"]
        expected_green = [expected[node_x]] if node_x in expected else []
        assert green == expected_green


def test_manual_eastbound_discharge_is_downstream_first_then_coordinated():
    controller, config = make_controller("Eastbound Corridor")
    upstream_a = eb_vehicle_before_node(300)
    upstream_b = eb_vehicle_before_node(700)
    vehicles = [upstream_a, upstream_b]

    request_discharge(controller, config, vehicles, "Eastbound Corridor")
    observed = advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
    )

    assert DISCHARGE_ALL_RED in observed
    assert controller.current_discharge_stage.label == "Node B downstream"
    assert_exclusive_greens(controller.get_all_signals(), {700: "EB"})

    upstream_b.passed_nodes.add(700)
    advance_until(
        controller,
        vehicles,
        lambda: (
            controller.discharge_state == DISCHARGE_ACTIVE
            and controller.current_discharge_stage.label
            == "Node B → Node A coordinated"
        ),
    )
    assert_exclusive_greens(
        controller.get_all_signals(), {300: "EB", 700: "EB"}
    )


def test_auto_selects_largest_safe_queue_and_keeps_other_movements_red():
    controller, config = make_controller()
    vehicles = [
        vertical_vehicle(300, "NB", 0),
        vertical_vehicle(300, "NB", 35),
        vertical_vehicle(300, "NB", 70),
        vertical_vehicle(700, "SB", 0),
    ]

    request_discharge(controller, config, vehicles, control_panel.DISCHARGE_AUTO)
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
    )

    status = controller.get_discharge_status()
    assert status["mode"] == control_panel.DISCHARGE_AUTO
    assert status["selected"] == "Node A Northbound"
    assert status["status"] == "DISCHARGING"
    assert status["arrivals_suspended"] is True
    assert status["priority_suspended"] is True
    assert_exclusive_greens(controller.get_all_signals(), {300: "NB"})


def test_manual_selection_waits_with_reason_when_receiving_space_is_full():
    controller, config = make_controller("Eastbound Corridor")
    waiting = eb_vehicle_before_node(700)
    downstream_blocker = Vehicle(
        780,
        waiting.y,
        "EB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    downstream_blocker.speed = 0.0
    alternative = vertical_vehicle(700, "NB")
    vehicles = [waiting, downstream_blocker, alternative]

    request_discharge(controller, config, vehicles, "Eastbound Corridor")
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_WAITING,
    )

    status = controller.get_discharge_status()
    assert status["selected"] == "Eastbound Corridor"
    assert status["status"] == "WAITING"
    assert status["reason"] == (
        "Node B eastbound exit has insufficient storage"
    )
    assert status["recommendation"] == "Discharge Node B Northbound"
    assert set(controller.get_all_signals()[700].values()) == {"RED"}


def test_safe_stop_uses_yellow_and_all_red_before_normal_control():
    controller, config = make_controller("Node A Northbound")
    vehicles = [vertical_vehicle(300, "NB")]
    request_discharge(controller, config, vehicles, "Node A Northbound")
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
    )

    config["discharge_stop_requested"] = True
    controller.update(vehicles)
    assert controller.discharge_state == DISCHARGE_STOPPING_YELLOW
    assert controller.get_all_signals()[300]["NB"] == "YELLOW"

    observed = advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_INACTIVE,
    )
    assert DISCHARGE_STOPPING_ALL_RED in observed
    assert controller.is_discharge_active() is False
    assert controller.get_discharge_status()["status"] == "COMPLETED"
    assert controller.get_all_signals()[300] == {
        "EB": "GREEN", "WB": "GREEN", "NB": "RED", "SB": "RED"
    }


def test_safe_stop_remains_all_red_while_either_conflict_box_is_occupied():
    controller, config = make_controller("Node A Northbound")
    waiting = vertical_vehicle(300, "NB")
    blocker = Vehicle(700, H_Y, "EB", target_turn="STRAIGHT", lane_index=1)
    vehicles = [waiting, blocker]
    request_discharge(controller, config, vehicles, "Node A Northbound")
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
    )

    config["discharge_stop_requested"] = True
    controller.update(vehicles)
    for _ in range(10):
        controller.update(vehicles)

    assert controller.discharge_state == DISCHARGE_STOPPING_ALL_RED
    assert controller.is_discharge_active()
    assert all(
        set(node_signals.values()) == {"RED"}
        for node_signals in controller.get_all_signals().values()
    )
    assert controller.get_discharge_status()["reason"] == (
        "Safe stop is waiting for both conflict boxes to clear"
    )


def test_recovery_request_and_active_state_suspend_new_demand():
    controller, config = make_controller("Node B Southbound")
    control_panel.global_config["discharge_start_requested"] = True
    assert main.is_discharge_demand_suspended(controller)

    control_panel.global_config["discharge_start_requested"] = False
    assert not main.is_discharge_demand_suspended(controller)

    config["discharge_start_requested"] = True
    controller.update([vertical_vehicle(700, "SB")])
    assert main.is_discharge_demand_suspended(controller)


def test_post_recovery_demand_is_metered_without_discarding_backlog():
    main.reset_all_spawner_states()
    main.spawner_states["EB"]["pending_arrivals"] = 7
    main.begin_post_discharge_metering()
    assert main.post_discharge_meter_frames_remaining == 600
    assert main.post_discharge_admission_allowed()

    main.advance_post_discharge_metering()
    assert not main.post_discharge_admission_allowed()
    for _ in range(59):
        main.advance_post_discharge_metering()
    assert main.post_discharge_admission_allowed()
    assert main.spawner_states["EB"]["pending_arrivals"] == 7
    telemetry = main.get_demand_telemetry()
    assert telemetry["EB"]["post_recovery_metering"] is True
    main.reset_all_spawner_states()


def test_telemetry_and_dashboard_expose_recovery_message():
    controller, config = make_controller("Node A Southbound")
    vehicle = vertical_vehicle(300, "SB")
    request_discharge(controller, config, [vehicle], "Node A Southbound")
    advance_until(
        controller,
        [vehicle],
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
    )

    payload = TelemetryExporter(export_interval_frames=1).build_payload(
        controller,
        [vehicle],
        frame_number=10,
        demand_state={},
    )
    recovery = payload["network_discharge"]
    assert recovery["selected"] == "Node A Southbound"
    assert recovery["status"] == "DISCHARGING"
    assert payload["signal_state"]["nodes"]["300"]["phase"].startswith(
        "NETWORK_DISCHARGE_"
    )

    display = TelemetryDashboard.format_discharge_status(recovery)
    assert display["selected"] == "Selected: Node A Southbound"
    assert display["status"].startswith("Status: DISCHARGING")
    assert display["reason"].startswith("Reason: ")
    assert display["recommendation"].startswith("Recommended first action: ")
    assert display["status_color"] != COLOR_WARNING


def test_reset_clears_discharge_commands_and_runtime_state():
    controller, config = make_controller("Node B Northbound")
    request_discharge(
        controller,
        config,
        [vertical_vehicle(700, "NB")],
        "Node B Northbound",
    )
    controller.reset_discharge()
    status = controller.get_discharge_status()
    assert status["active"] is False
    assert status["status"] == "IDLE"
    assert config["discharge_start_requested"] is False
    assert config["discharge_stop_requested"] is False


def test_starting_discharge_cancels_and_audits_pending_priority_requests():
    controller, config = make_controller("Node A Northbound")
    control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300, "RECOVERY_PRIORITY_BUS")
    controller.update([bus])
    assert controller.get_node_status(300)["active_request"] is not None

    request_discharge(controller, config, [bus], "Node A Northbound")

    node_status = controller.get_node_status(300)
    assert node_status["active_request"] is None
    assert node_status["queued_requests"] == []
    terminal = node_status["terminal_history"][-1]
    assert terminal["bus_id"] == "RECOVERY_PRIORITY_BUS"
    assert terminal["state"] == "CANCELLED"
    assert terminal["denial_or_cancel_reason"] == "NETWORK_DISCHARGE_STARTED"
    assert controller.get_discharge_status()["priority_suspended"] is True


def test_active_priority_green_transitions_to_yellow_before_discharge_all_red():
    controller, config = make_controller("Node A Southbound")
    control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300, "ACTIVE_PRIORITY_BUS")
    for _ in range(20):
        controller.update([bus])
        if controller.get_node_status(300)["priority_state"] == "PRIORITY_ACTIVE":
            break
    assert controller.get_all_signals()[300]["EB"] == "GREEN"

    request_discharge(controller, config, [bus], "Node A Southbound")

    assert controller.get_all_signals()[300] == {
        "EB": "YELLOW", "WB": "RED", "NB": "RED", "SB": "RED"
    }
    advance_until(
        controller,
        [bus],
        lambda: controller.discharge_state in (
            DISCHARGE_ALL_RED, DISCHARGE_WAITING, DISCHARGE_ACTIVE
        ),
    )
    if controller.discharge_state != DISCHARGE_ACTIVE:
        assert set(controller.get_all_signals()[300].values()) == {"RED"}


def test_auto_recovery_reduces_a_saturated_network_without_collisions():
    random.seed(31)
    main.reset_all_spawner_states()
    controller = SignalController(
        control_panel.global_config,
        yellow_time=20,
        red_clearance_time=20,
        discharge_min_green=60,
        discharge_max_green=300,
        discharge_stall_time=120,
        discharge_queue_target=0,
    )
    vehicles = []
    sources = {
        "EB": ("EB", -20, [H_Y - 0.5 * LANE, H_Y - 1.5 * LANE, H_Y - 2.5 * LANE]),
        "WB": ("WB", WIDTH + 20, [H_Y + 0.5 * LANE, H_Y + 1.5 * LANE, H_Y + 2.5 * LANE]),
        "A_NB": ("NB", HEIGHT + 20, [300 - 0.5 * LANE, 300 - 1.5 * LANE, 300 - 2.5 * LANE]),
        "A_SB": ("SB", -20, [300 + 0.5 * LANE, 300 + 1.5 * LANE, 300 + 2.5 * LANE]),
        "B_NB": ("NB", HEIGHT + 20, [700 - 0.5 * LANE, 700 - 1.5 * LANE, 700 - 2.5 * LANE]),
        "B_SB": ("SB", -20, [700 + 0.5 * LANE, 700 + 1.5 * LANE, 700 + 2.5 * LANE]),
    }
    peak_config = {
        "model": main.CONGESTION_MODEL,
        "rate": 30,
        "turn_split": 0.8,
        "heavy_ratio": 0.2,
    }

    try:
        for _ in range(1200):
            for approach_key, (direction, spawn_coord, lanes) in sources.items():
                main.try_spawn_vehicle(
                    vehicles,
                    approach_key,
                    direction,
                    spawn_coord,
                    lanes,
                    peak_config,
                )
            controller.update(vehicles)
            signals = controller.get_all_signals(INT_X)
            for vehicle in list(vehicles):
                vehicle.update(
                    signals,
                    INT_X,
                    H_Y,
                    ROAD_W,
                    STOP,
                    LANE,
                    vehicles,
                    controller,
                )
            vehicles[:] = [
                vehicle
                for vehicle in vehicles
                if -60 <= vehicle.x <= WIDTH + 60
                and -60 <= vehicle.y <= HEIGHT + 60
            ]

        initial_upstream = controller._network_upstream_count(vehicles)
        assert initial_upstream >= 10
        control_panel.global_config["discharge_selection"] = (
            control_panel.DISCHARGE_AUTO
        )
        control_panel.global_config["discharge_start_requested"] = True
        saw_discharge_green = False

        for _ in range(3600):
            controller.update(vehicles)
            signals = controller.get_all_signals(INT_X)
            for node_signals in signals.values():
                assert sum(value == "GREEN" for value in node_signals.values()) <= 1
            saw_discharge_green |= (
                controller.discharge_state == DISCHARGE_ACTIVE
            )
            for vehicle in list(vehicles):
                vehicle.update(
                    signals,
                    INT_X,
                    H_Y,
                    ROAD_W,
                    STOP,
                    LANE,
                    vehicles,
                    controller,
                )
            vehicles[:] = [
                vehicle
                for vehicle in vehicles
                if -60 <= vehicle.x <= WIDTH + 60
                and -60 <= vehicle.y <= HEIGHT + 60
            ]
            for index, first in enumerate(vehicles):
                for second in vehicles[index + 1 :]:
                    if first.direction != second.direction:
                        assert not rectangles_overlap(first, second)
            if not controller.is_discharge_active():
                break

        final_upstream = controller._network_upstream_count(vehicles)
        assert saw_discharge_green
        assert final_upstream < initial_upstream
    finally:
        main.reset_all_spawner_states()
