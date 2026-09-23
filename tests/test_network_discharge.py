import random

import src.ui.control_panel as control_panel
import src.core.main as main
from src.ui.canvas_gemini import HEIGHT, H_Y, INT_X, LANE, ROAD_W, STOP, WIDTH
from src.core.signal_controller import (
    DISCHARGE_ACTIVE,
    DISCHARGE_ALL_RED,
    DISCHARGE_DUE_LEG_GRACE_FRAMES,
    DISCHARGE_INACTIVE,
    DISCHARGE_RECOVERY_FAILED,
    DISCHARGE_STOPPING_ALL_RED,
    DISCHARGE_STOPPING_YELLOW,
    DISCHARGE_TRANSITION_YELLOW,
    DISCHARGE_WAITING,
    DischargeStage,
    SignalController,
)
from src.ui.telemetry_dashboard import COLOR_WARNING, TelemetryDashboard
from src.telemetry.telemetry_exporter import TelemetryExporter
from tests.helpers import make_bus_for_leg, rectangles_overlap, NODE_A, NODE_B
from src.core.vehicle import Vehicle


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
    x = 100 if node_x == NODE_A else 500
    vehicle = Vehicle(
        x,
        H_Y - 1.5 * LANE,
        "EB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    if node_x == NODE_B:
        vehicle.passed_nodes.add(NODE_A)
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


def vehicle_in_box(node_x, direction):
    if direction == "EB":
        vehicle = Vehicle(node_x + ROAD_W / 2 - 6, H_Y - 1.5 * LANE, "EB")
    elif direction == "WB":
        vehicle = Vehicle(node_x - ROAD_W / 2 + 6, H_Y + 1.5 * LANE, "WB")
    elif direction == "NB":
        vehicle = Vehicle(
            node_x - 1.5 * LANE,
            H_Y - ROAD_W / 2 + 6,
            "NB",
            assigned_node_x=node_x,
        )
    else:
        vehicle = Vehicle(
            node_x + 1.5 * LANE,
            H_Y + ROAD_W / 2 - 6,
            "SB",
            assigned_node_x=node_x,
        )
    vehicle.speed = 0.0
    return vehicle


def wb_vehicle_before_node_a():
    vehicle = Vehicle(
        NODE_A + 200,
        H_Y + 1.5 * LANE,
        "WB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    vehicle.passed_nodes.add(NODE_B)
    vehicle.speed = 0.0
    return vehicle


def test_manual_eastbound_discharge_is_downstream_first_then_coordinated():
    controller, config = make_controller("Eastbound Corridor")
    upstream_a = eb_vehicle_before_node(NODE_A)
    upstream_b = eb_vehicle_before_node(NODE_B)
    vehicles = [upstream_a, upstream_b]

    request_discharge(controller, config, vehicles, "Eastbound Corridor")
    observed = advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
    )

    assert DISCHARGE_ALL_RED in observed
    assert controller.current_discharge_stage.label == "Node B downstream"
    assert_exclusive_greens(controller.get_all_signals(), {NODE_B: "EB"})

    upstream_b.passed_nodes.add(NODE_B)
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
        controller.get_all_signals(), {NODE_A: "EB", NODE_B: "EB"}
    )


def test_auto_selects_largest_safe_queue_and_keeps_other_movements_red():
    controller, config = make_controller()
    vehicles = [
        vertical_vehicle(NODE_A, "NB", 0),
        vertical_vehicle(NODE_A, "NB", 35),
        vertical_vehicle(NODE_A, "NB", 70),
        vertical_vehicle(NODE_B, "SB", 0),
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
    assert_exclusive_greens(controller.get_all_signals(), {NODE_A: "NB"})


def test_manual_selection_waits_with_reason_when_receiving_space_is_full():
    controller, config = make_controller("Eastbound Corridor")
    waiting = eb_vehicle_before_node(NODE_B)
    downstream_blocker = Vehicle(
        NODE_B + 80,
        waiting.y,
        "EB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    downstream_blocker.speed = 0.0
    alternative = vertical_vehicle(NODE_B, "NB")
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
    assert set(controller.get_all_signals()[NODE_B].values()) == {"RED"}


def test_direction_aware_clearance_releases_same_direction_exiter():
    controller, _config = make_controller()
    stage = DischargeStage("Node A eastbound", ((NODE_A, "EB"),))
    exiter = vehicle_in_box(NODE_A, "EB")
    waiting = eb_vehicle_before_node(NODE_A)

    assert not controller.is_intersection_clear(NODE_A, [exiter])
    assert controller.is_intersection_clear_for_greens(
        NODE_A, stage.greens, [exiter]
    )
    assert controller._stage_readiness(stage, [exiter, waiting])[0] is True

    cross_direction = vehicle_in_box(NODE_A, "NB")
    assert not controller.is_intersection_clear_for_greens(
        NODE_A, stage.greens, [cross_direction]
    )
    assert controller._stage_readiness(
        stage, [cross_direction, waiting]
    )[0] is False


def test_auto_selects_blocker_draining_plan():
    controller, config = make_controller()
    blocker = make_bus_for_leg(
        "R2_EB_B_NB", NODE_A, "BUS_R2_EB_B_NB_GRIDLOCK"
    )
    blocker.x = NODE_A + 80.5
    blocker.leg_state = "IN_INTERSECTION"
    blocker.speed = 0.0
    downstream_leader = Vehicle(
        NODE_A + 120,
        H_Y - 1.5 * LANE,
        "EB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    downstream_leader.passed_nodes.add(NODE_A)
    downstream_leader.speed = 0.0
    vehicles = [blocker, downstream_leader]

    request_discharge(
        controller, config, vehicles, control_panel.DISCHARGE_AUTO
    )
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
    )

    status = controller.get_discharge_status()
    assert status["mode"] == control_panel.DISCHARGE_AUTO
    assert status["selected"] == "Eastbound Corridor"
    assert controller.current_discharge_stage.label == "Node B downstream"
    assert_exclusive_greens(controller.get_all_signals(), {NODE_B: "EB"})

    # Both start standing, 9.5 px apart (under the 3 m minimum gap): the car
    # has to pull away first and the bus follows at a bus's ~1.2 m/s^2, so
    # clearing the box takes several seconds, not the 2 s a 45 m/s^2 ramp did.
    for _ in range(420):
        signals = controller.get_all_signals()
        for vehicle in vehicles:
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
        controller.update(vehicles)
        if NODE_A in blocker.passed_nodes:
            break

    assert NODE_A in blocker.passed_nodes
    assert controller.is_intersection_clear(NODE_A, vehicles)


def test_auto_reranks_when_latched_candidate_unready():
    controller, config = make_controller()
    waiting_wb = wb_vehicle_before_node_a()
    blocker_eb = vehicle_in_box(NODE_A, "EB")
    blocker_nb = vehicle_in_box(NODE_A, "NB")
    vehicles = [waiting_wb, blocker_eb, blocker_nb]

    request_discharge(
        controller, config, vehicles, control_panel.DISCHARGE_AUTO
    )
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_WAITING,
    )
    assert controller.discharge_plan_name is not None

    alternative = vertical_vehicle(NODE_B, "NB")
    vehicles.append(alternative)
    # The clockwise rotation holds its due leg for a bounded grace period, so
    # a persistently blocked leg is handed over to the next servable leg
    # rather than stalling recovery.
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
        limit=DISCHARGE_DUE_LEG_GRACE_FRAMES + 60,
    )

    assert controller.discharge_state == DISCHARGE_ACTIVE
    assert controller.discharge_plan_name == "Node B Northbound"
    assert_exclusive_greens(controller.get_all_signals(), {NODE_B: "NB"})


def test_waiting_never_all_red_forever():
    controller, config = make_controller()
    vehicles = [
        vehicle_in_box(NODE_A, "EB"),
        vehicle_in_box(NODE_A, "NB"),
    ]

    request_discharge(
        controller, config, vehicles, control_panel.DISCHARGE_AUTO
    )
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_RECOVERY_FAILED,
        limit=20,
    )

    status = controller.get_discharge_status()
    assert status["status"] == "RECOVERY_FAILED"
    assert "Node A blocked by EB/NB vehicle" in status["reason"]
    assert "No safe ready stage" in status["reason"]
    assert status["recommendation"].startswith("RESET VEHICLES required")
    for node_signals in controller.get_all_signals().values():
        assert set(node_signals.values()) == {"RED"}

    controller.update(vehicles)
    assert controller.discharge_state == DISCHARGE_RECOVERY_FAILED

    config["discharge_selection"] = "Node B Northbound"
    config["discharge_start_requested"] = True
    controller.update(vehicles)
    assert controller.discharge_state == DISCHARGE_RECOVERY_FAILED

    config["discharge_stop_requested"] = True
    controller.update(vehicles)
    assert controller.discharge_state == DISCHARGE_STOPPING_ALL_RED
    for node_signals in controller.get_all_signals().values():
        assert set(node_signals.values()) == {"RED"}


def test_manual_plan_not_silently_switched():
    controller, config = make_controller("Westbound Corridor")
    vehicles = [wb_vehicle_before_node_a(), vehicle_in_box(NODE_A, "EB")]

    request_discharge(controller, config, vehicles, "Westbound Corridor")
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_RECOVERY_FAILED,
        limit=20,
    )

    status = controller.get_discharge_status()
    assert status["mode"] == "Westbound Corridor"
    assert status["selected"] == "Westbound Corridor"
    assert controller.discharge_plan_name == "Westbound Corridor"
    for node_signals in controller.get_all_signals().values():
        assert set(node_signals.values()) == {"RED"}


def test_no_perpendicular_overlap_during_recovery():
    controller, config = make_controller()
    blocker = vehicle_in_box(NODE_A, "EB")
    downstream_leader = Vehicle(
        NODE_A + 110,
        H_Y - 1.5 * LANE,
        "EB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    downstream_leader.passed_nodes.add(NODE_A)
    vehicles = [blocker, downstream_leader]
    request_discharge(
        controller, config, vehicles, control_panel.DISCHARGE_AUTO
    )

    for _ in range(30):
        signals = controller.get_all_signals()
        for node_signals in signals.values():
            green_directions = [
                direction
                for direction, state in node_signals.items()
                if state == "GREEN"
            ]
            assert len(green_directions) <= 1
        controller.update(vehicles)


def test_auto_selects_westbound_for_node_b_blocker():
    controller, config = make_controller()
    blocker = make_bus_for_leg(
        "R4_WB_A_SB", NODE_B, "BUS_R4_WB_A_SB_GRIDLOCK"
    )
    blocker.x = NODE_B - 80.5
    blocker.leg_state = "IN_INTERSECTION"
    blocker.speed = 0.0
    downstream_leader = Vehicle(
        NODE_B - 120,
        H_Y + 1.5 * LANE,
        "WB",
        target_turn="STRAIGHT",
        lane_index=1,
    )
    downstream_leader.passed_nodes.add(NODE_B)
    downstream_leader.speed = 0.0
    vehicles = [blocker, downstream_leader]

    request_discharge(
        controller, config, vehicles, control_panel.DISCHARGE_AUTO
    )
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
    )

    assert controller.discharge_plan_name == "Westbound Corridor"
    assert controller.current_discharge_stage.label == "Node A downstream"
    assert_exclusive_greens(controller.get_all_signals(), {NODE_A: "WB"})


def test_safe_stop_uses_yellow_and_all_red_before_normal_control():
    controller, config = make_controller("Node A Northbound")
    vehicles = [vertical_vehicle(NODE_A, "NB")]
    request_discharge(controller, config, vehicles, "Node A Northbound")
    advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_ACTIVE,
    )

    config["discharge_stop_requested"] = True
    controller.update(vehicles)
    assert controller.discharge_state == DISCHARGE_STOPPING_YELLOW
    assert controller.get_all_signals()[NODE_A]["NB"] == "YELLOW"

    observed = advance_until(
        controller,
        vehicles,
        lambda: controller.discharge_state == DISCHARGE_INACTIVE,
    )
    assert DISCHARGE_STOPPING_ALL_RED in observed
    assert controller.is_discharge_active() is False
    assert controller.get_discharge_status()["status"] == "COMPLETED"
    assert controller.get_all_signals()[NODE_A] == {
        "EB": "GREEN", "WB": "GREEN", "NB": "RED", "SB": "RED"
    }


def test_safe_stop_remains_all_red_while_either_conflict_box_is_occupied():
    controller, config = make_controller("Node A Northbound")
    waiting = vertical_vehicle(NODE_A, "NB")
    blocker = Vehicle(NODE_B, H_Y, "EB", target_turn="STRAIGHT", lane_index=1)
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
    controller.update([vertical_vehicle(NODE_B, "SB")])
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
    vehicle = vertical_vehicle(NODE_A, "SB")
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
    assert payload["signal_state"]["nodes"][str(NODE_A)]["phase"].startswith(
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
        [vertical_vehicle(NODE_B, "NB")],
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
    bus = make_bus_for_leg("R1_EB_A_NB", NODE_A, "RECOVERY_PRIORITY_BUS")
    controller.update([bus])
    assert controller.get_node_status(NODE_A)["active_request"] is not None

    request_discharge(controller, config, [bus], "Node A Northbound")

    node_status = controller.get_node_status(NODE_A)
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
    bus = make_bus_for_leg("R1_EB_A_NB", NODE_A, "ACTIVE_PRIORITY_BUS")
    # EW green about to end with the bus still upstream: TSP holds it.
    controller.phase = 0
    controller.timer = controller.get_green_time(NODE_A, 0) - 3
    for _ in range(20):
        controller.update([bus])
        if controller.get_node_status(NODE_A)["priority_state"] == "TSP_EXTENDING":
            break
    assert controller.get_node_status(NODE_A)["priority_state"] == "TSP_EXTENDING"
    assert controller.get_all_signals()[NODE_A]["EB"] == "GREEN"
    assert controller.get_all_signals()[NODE_A]["WB"] == "GREEN"

    request_discharge(controller, config, [bus], "Node A Southbound")

    assert controller.get_all_signals()[NODE_A] == {
        "EB": "YELLOW", "WB": "YELLOW", "NB": "RED", "SB": "RED"
    }
    advance_until(
        controller,
        [bus],
        lambda: controller.discharge_state in (
            DISCHARGE_ALL_RED, DISCHARGE_WAITING, DISCHARGE_ACTIVE
        ),
    )
    if controller.discharge_state != DISCHARGE_ACTIVE:
        assert set(controller.get_all_signals()[NODE_A].values()) == {"RED"}


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
        "A_NB": ("NB", HEIGHT + 20, [NODE_A - 0.5 * LANE, NODE_A - 1.5 * LANE, NODE_A - 2.5 * LANE]),
        "A_SB": ("SB", -20, [NODE_A + 0.5 * LANE, NODE_A + 1.5 * LANE, NODE_A + 2.5 * LANE]),
        "B_NB": ("NB", HEIGHT + 20, [NODE_B - 0.5 * LANE, NODE_B - 1.5 * LANE, NODE_B - 2.5 * LANE]),
        "B_SB": ("SB", -20, [NODE_B + 0.5 * LANE, NODE_B + 1.5 * LANE, NODE_B + 2.5 * LANE]),
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
