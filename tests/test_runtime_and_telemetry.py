import ast
import inspect
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

import control_panel
import canvas_gemini as canvas
import main
import pygame
import telemetry_dashboard as telemetry_dashboard_module
from canvas_gemini import H_Y, LANE
from signal_controller import SignalController
from telemetry_dashboard import HISTORY_MAX_POINTS, TelemetryDashboard, _gpu_none
from telemetry_exporter import DEFAULT_TELEMETRY_PATH, TelemetryExporter
from vehicle import Bus, Vehicle
from tests.helpers import make_bus_for_leg


def lane_options():
    return {
        "EB": [H_Y - 0.5 * LANE, H_Y - 1.5 * LANE, H_Y - 2.5 * LANE],
        "WB": [H_Y + 0.5 * LANE, H_Y + 1.5 * LANE, H_Y + 2.5 * LANE],
    }


@pytest.fixture
def preserve_sim_random_state():
    state = main.random.getstate()
    yield
    main.random.setstate(state)
    main.reset_all_spawner_states()


def _collect_spawn_sequence(frame_count=240):
    vehicles = []
    config = {
        "model": "Poisson",
        "rate": 600,
        "turn_split": 0.7,
        "heavy_ratio": 0.25,
    }
    sequence = []
    for frame in range(frame_count):
        previous_count = len(vehicles)
        main.try_spawn_vehicle(
            vehicles,
            "EB",
            "EB",
            -20,
            lane_options()["EB"],
            config,
            min_gap=0,
        )
        if len(vehicles) == previous_count:
            continue
        vehicle = vehicles[-1]
        sequence.append(
            (
                frame,
                vehicle.is_heavy,
                vehicle.max_speed,
                vehicle.lane_index,
                vehicle.target_turn,
                vehicle.color,
            )
        )
    return sequence


def _seeded_spawn_sequence(seed):
    control_panel.global_config["random_seed"] = seed
    main.reset_traffic_generation()
    return _collect_spawn_sequence()


def test_same_seed_same_spawns(preserve_sim_random_state):
    first = _seeded_spawn_sequence(20260906)
    second = _seeded_spawn_sequence(20260906)

    assert first
    assert first == second


def test_different_seed_differs(preserve_sim_random_state):
    first = _seeded_spawn_sequence(101)
    second = _seeded_spawn_sequence(202)

    assert first
    assert second
    assert first != second


def test_none_seed_nondeterministic(monkeypatch):
    seed_calls = []
    monkeypatch.setitem(control_panel.global_config, "random_seed", None)
    monkeypatch.setattr(main.random, "seed", lambda value: seed_calls.append(value))

    assert main.apply_configured_random_seed() is None
    assert seed_calls == []


def test_reset_reproduces_with_seed(preserve_sim_random_state):
    control_panel.global_config["random_seed"] = 8675309
    main.reset_traffic_generation()
    before_reset = _collect_spawn_sequence()

    # Consume more RNG and source state before exercising the production reset.
    _collect_spawn_sequence(60)
    main.reset_traffic_generation()
    after_reset = _collect_spawn_sequence()

    assert before_reset
    assert before_reset == after_reset
    assert all(value == 0.0 for value in main.bus_dispatch_counters.values())
    assert main.bus_sequence_counter == 0


def test_seed_config_accepts_integer_and_blank(monkeypatch):
    monkeypatch.setitem(control_panel.global_config, "random_seed", None)

    assert control_panel.set_random_seed(" 12345 ") == 12345
    assert control_panel.global_config["random_seed"] == 12345
    assert control_panel.set_random_seed("   ") is None
    assert control_panel.global_config["random_seed"] is None


def test_sim_launches_stopped():
    assert control_panel.global_config["is_running"] is False
    assert control_panel.global_config["start_requested"] is False
    source = inspect.getsource(main.main)
    assert "if is_running and not is_paused and not run_just_reset:" in source
    assert 'if not control_panel.global_config.get("is_running"' not in source


def test_start_performs_full_reset(tmp_path, monkeypatch):
    telemetry_log = tmp_path / "telemetry_log.jsonl"
    turn_log = tmp_path / "agent_turn_log.jsonl"
    telemetry_log.write_text("old telemetry\n", encoding="utf-8")
    turn_log.write_text("old turn\n", encoding="utf-8")
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", telemetry_log)
    monkeypatch.setattr(main, "AGENT_TURN_LOG_PATH", turn_log)
    seed_applications = []
    monkeypatch.setattr(
        main,
        "reset_traffic_generation",
        lambda: seed_applications.append(
            control_panel.global_config.get("random_seed")
        ),
    )

    class FakeSignals:
        frame_number = 99

        def reset_all_state(self):
            self.frame_number = 0

    class FakeTelemetry:
        reset_count = 0

        def reset_session(self):
            self.reset_count += 1

    signals = FakeSignals()
    telemetry = FakeTelemetry()
    vehicles = [object(), object()]
    for key in main.network_throughput:
        main.network_throughput[key] = 99
    monkeypatch.setitem(control_panel.global_config, "random_seed", 4242)

    frame = main.perform_full_reset(vehicles, signals, telemetry)

    assert frame == 0
    assert vehicles == []
    assert signals.frame_number == 0
    assert telemetry.reset_count == 1
    assert seed_applications == [4242]
    assert not telemetry_log.exists()
    assert not turn_log.exists()
    assert set(main.network_throughput.values()) == {0}


def test_stop_preserves_logs(tmp_path, monkeypatch):
    telemetry_log = tmp_path / "telemetry_log.jsonl"
    turn_log = tmp_path / "agent_turn_log.jsonl"
    telemetry_log.write_text("completed telemetry\n", encoding="utf-8")
    turn_log.write_text("completed turns\n", encoding="utf-8")
    monkeypatch.setitem(control_panel.global_config, "is_running", True)
    monkeypatch.setitem(control_panel.global_config, "start_requested", False)

    result = control_panel.request_start_stop()

    assert result == "STOPPED"
    assert control_panel.global_config["is_running"] is False
    assert telemetry_log.read_text(encoding="utf-8") == "completed telemetry\n"
    assert turn_log.read_text(encoding="utf-8") == "completed turns\n"


def test_start_after_stop_is_fresh(tmp_path, monkeypatch):
    telemetry_log = tmp_path / "telemetry_log.jsonl"
    turn_log = tmp_path / "agent_turn_log.jsonl"
    telemetry_log.write_text("run A\n", encoding="utf-8")
    turn_log.write_text("run A\n", encoding="utf-8")
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", telemetry_log)
    monkeypatch.setattr(main, "AGENT_TURN_LOG_PATH", turn_log)
    monkeypatch.setattr(main, "reset_traffic_generation", lambda: None)

    class FakeSignals:
        def reset_all_state(self):
            pass

    monkeypatch.setitem(control_panel.global_config, "is_running", True)
    assert control_panel.request_start_stop() == "STOPPED"
    assert telemetry_log.exists() and turn_log.exists()
    assert control_panel.request_start_stop() == "START_REQUESTED"

    frame = main.perform_full_reset([object()], FakeSignals())

    assert frame == 0
    assert not telemetry_log.exists()
    assert not turn_log.exists()


def test_pause_still_independent(monkeypatch):
    monkeypatch.setitem(control_panel.global_config, "is_running", True)
    monkeypatch.setitem(control_panel.global_config, "is_paused", False)

    assert control_panel.request_pause_resume() is True
    assert control_panel.global_config["is_running"] is True
    assert control_panel.request_pause_resume() is False
    assert control_panel.global_config["is_running"] is True

    assert control_panel.request_start_stop() == "STOPPED"
    assert control_panel.request_pause_resume() is False
    assert control_panel.global_config["is_running"] is False


def test_truck_passenger_count():
    truck = Vehicle(0, 0, "EB", is_heavy=True)
    car = Vehicle(0, 0, "EB", is_heavy=False)
    bus = make_bus_for_leg("R1_EB_A_NB", 300, "PAX_BUS")

    assert truck.passengers == 1
    assert car.passengers == 4
    assert isinstance(bus, Bus)
    assert bus.passengers == 45


def test_throughput_counts_truck_as_one(monkeypatch):
    truck = Vehicle(canvas.WIDTH + 61, H_Y - 0.5 * LANE, "EB", is_heavy=True)
    truck.passed_nodes.add(canvas.INT_X[0])
    truck.update = lambda **kwargs: None

    class FakeRoot:
        def geometry(self, _value):
            pass

        def after(self, _delay, callback):
            self.callback = callback

        def mainloop(self):
            self.callback()

    class FakeProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

    class FakeSignals:
        def __init__(self, *args, **kwargs):
            pass

        def update(self, vehicles=None):
            pass

        def is_discharge_active(self):
            return False

        def get_all_signals(self, _nodes):
            return {}

        def get_all_dbl_states(self, _nodes, _vehicles):
            return {}

    class FakeTelemetry:
        def __init__(self, *args, **kwargs):
            pass

        def export(self, **kwargs):
            return False

    spawned = False

    def spawn_truck(vehicles, *args, **kwargs):
        nonlocal spawned
        if not spawned:
            vehicles.append(truck)
            spawned = True

    approaches = {
        key: {"active": key == "EB"}
        for key in ("EB", "WB", "A_NB", "A_SB", "B_NB", "B_SB")
    }
    monotonic_values = iter((100.0, 100.02))

    monkeypatch.setattr(main, "reset_session_logs", lambda: None)
    monkeypatch.setattr(main, "SignalController", FakeSignals)
    monkeypatch.setattr(main, "TelemetryExporter", FakeTelemetry)
    monkeypatch.setattr(main.control_panel, "approach_configs", approaches)
    monkeypatch.setattr(main.control_panel, "create_dashboard_window", FakeRoot)
    monkeypatch.setattr(main.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(main.atexit, "register", lambda callback: callback)
    monkeypatch.setattr(main.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(main, "try_spawn_vehicle", spawn_truck)
    monkeypatch.setattr(main, "check_and_dispatch_buses", lambda *args: None)
    monkeypatch.setattr(main, "get_demand_telemetry", lambda: {})
    monkeypatch.setattr(main.canvas, "draw_network", lambda *args, **kwargs: None)
    monkeypatch.setattr(main.pygame, "init", lambda: None)
    monkeypatch.setattr(main.pygame.font, "init", lambda: None)
    monkeypatch.setattr(main.pygame.font, "SysFont", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        main.pygame.display,
        "Info",
        lambda: SimpleNamespace(current_w=1920, current_h=1080),
    )
    monkeypatch.setattr(main.pygame.display, "set_mode", lambda size: object())
    monkeypatch.setattr(main.pygame.display, "set_caption", lambda title: None)
    monkeypatch.setattr(main.pygame.display, "flip", lambda: None)
    monkeypatch.setattr(main.pygame.event, "get", lambda: [])
    monkeypatch.setitem(main.control_panel.global_config, "is_running", True)
    monkeypatch.setitem(main.control_panel.global_config, "reset_triggered", False)
    monkeypatch.setitem(main.control_panel.global_config, "is_paused", False)
    monkeypatch.setitem(main.control_panel.global_config, "sim_speed", 1.0)
    for key in main.network_throughput:
        monkeypatch.setitem(main.network_throughput, key, 0)

    main.main()

    assert spawned is True
    assert truck.passed_nodes == {canvas.INT_X[0]}
    assert main.network_throughput["passengers_served_car"] == 1
    assert main.network_throughput["passengers_served_total"] == 1
    assert main.network_throughput["cars_served"] == 1
    assert main.network_throughput["vehicles_served_total"] == 1


def test_bus_dispatch_is_lane_aware():
    for config in control_panel.bus_routes_config.values():
        config["active"] = False
        config["manual_dispatch"] = False
    config = control_panel.bus_routes_config["R1_EB_A_NB"]
    config["active"] = True
    config["manual_dispatch"] = True
    vehicles = [Vehicle(-40, H_Y - 0.5 * LANE, "EB", lane_index=0)]
    main.check_and_dispatch_buses(vehicles, lane_options(), 1 / 60)
    assert len(vehicles) == 2
    assert vehicles[-1].route_id == "R1_EB_A_NB"


def test_zero_headway_resets_retained_time():
    route_id = "R1_EB_A_NB"
    for config in control_panel.bus_routes_config.values():
        config["active"] = False
    config = control_panel.bus_routes_config[route_id]
    config["active"] = True
    config["headway_sec"] = 0
    main.bus_dispatch_counters[route_id] = 12.5
    main.check_and_dispatch_buses([], lane_options(), 1 / 60)
    assert main.bus_dispatch_counters[route_id] == 0


def test_telemetry_uses_authoritative_passengers_and_per_node_signals(tmp_path):
    controller = SignalController({"green_time": 20})
    car = Vehicle(0, H_Y - 0.5 * LANE, "EB")
    exporter = TelemetryExporter(tmp_path / "state.json", 1)
    payload = exporter.build_payload(controller, [car], 60)
    assert payload["schema_version"] == 3
    assert payload["network_summary"]["passenger_volume"] == car.passengers
    assert set(payload["signal_state"]["nodes"]) == {"300", "700"}
    assert payload["signal_state"]["timing"] == {
        "frames_per_second": 60,
        "green_frames": 20,
        "yellow_frames": 60,
        "all_red_frames": 60,
        "nominal_cycle_frames": 280,
    }
    assert payload["simulation_time_seconds"] == 1.0


def test_telemetry_includes_congestion_demand_backlog(tmp_path):
    controller = SignalController({"green_time": 20})
    exporter = TelemetryExporter(tmp_path / "state.json", 1)
    demand = {
        "EB": {
            "model": main.CONGESTION_MODEL,
            "pending_arrivals": 7,
            "peak_active": True,
        },
        "WB": {"model": "Poisson", "pending_arrivals": 0},
    }

    payload = exporter.build_payload(
        controller, [], 1, demand_state=demand
    )

    assert payload["network_summary"]["pending_demand"] == 7
    assert payload["demand_generation"] == demand


def test_telemetry_export_atomically_writes_valid_schema(tmp_path):
    destination = tmp_path / "state.json"
    exporter = TelemetryExporter(destination, 1)
    controller = SignalController({"green_time": 20})
    throughput = {
        "passengers_served_total": 49,
        "passengers_served_bus": 45,
        "passengers_served_car": 4,
        "vehicles_served_total": 2,
        "buses_served": 1,
        "cars_served": 1,
    }
    assert exporter.export(controller, [], 3600, throughput_state=throughput)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["frame_number"] == 3600
    assert payload["network_throughput"]["passengers_served_total"] == 49
    assert payload["network_throughput"]["passengers_per_minute"] == 49.0
    assert not list(tmp_path.glob("tmp*"))


def test_schema_v3_exposes_bus_eta_routes_and_passenger_weighted_queues():
    controller = SignalController({"green_time": 20})
    exporter = TelemetryExporter(export_interval_frames=1)
    bus = make_bus_for_leg("R1_EB_A_NB", 300, "ETA_BUS")
    bus.speed = 0.0
    car = Vehicle(0, H_Y - 0.5 * LANE, "EB")
    car.speed = 0.0
    throughput = {
        "passengers_served_total": 49,
        "passengers_served_bus": 45,
        "passengers_served_car": 4,
        "vehicles_served_total": 2,
        "buses_served": 1,
        "cars_served": 1,
    }

    payload = exporter.build_payload(
        controller,
        [bus, car],
        frame_number=3600,
        throughput_state=throughput,
    )

    assert payload["schema_version"] == 3
    assert {
        "signal_state",
        "active_buses",
        "approaching_buses",
        "demand_generation",
        "network_discharge",
    } <= payload.keys()
    assert set(payload["routes"]) == set(control_panel.bus_routes_config)

    bus_state = payload["active_buses"][0]
    assert 0 <= bus_state["eta_to_stop_bar_sec_freeflow"] < float("inf")
    assert 0 <= bus_state["eta_to_stop_bar_sec_live"] <= 100.0

    route = payload["routes"]["R1_EB_A_NB"]
    assert route["buses_on_route"] == 1
    assert route["route_passengers_total"] == bus.passengers
    assert route["nearest_bus_id"] == "ETA_BUS"
    assert route["nearest_bus_eta_sec"] == bus_state["eta_to_stop_bar_sec_freeflow"]
    assert bus_state["lane_index"] == bus.lane_index
    assert bus_state["in_dbl_lane"] is True
    assert all(
        payload["network_summary"]["queues_passengers_est"][approach]
        == vehicle_count * 4
        for approach, vehicle_count in payload["network_summary"]["queues"].items()
    )
    assert payload["network_summary"]["car_occupancy_assumed"] == 4


def test_recent_throughput_uses_rolling_window_and_clears_on_reset():
    controller = SignalController({"green_time": 20})
    exporter = TelemetryExporter(export_interval_frames=1)

    initial = exporter.build_payload(
        controller, [], frame_number=0, throughput_state={"passengers_served_total": 0}
    )
    rising = exporter.build_payload(
        controller,
        [],
        frame_number=600,
        throughput_state={"passengers_served_total": 100},
    )
    recent = exporter.build_payload(
        controller,
        [],
        frame_number=2400,
        throughput_state={"passengers_served_total": 160},
    )
    reset = exporter.build_payload(
        controller,
        [],
        frame_number=2460,
        throughput_state={"passengers_served_total": 0},
    )

    assert initial["network_throughput"]["passengers_per_minute_recent"] == 0.0
    assert rising["network_throughput"]["passengers_per_minute_recent"] == 600.0
    assert recent["network_throughput"]["passengers_per_minute_recent"] == 120.0
    assert reset["network_throughput"]["passengers_per_minute_recent"] == 0.0
    assert exporter._throughput_samples == [(41.0, 0)]


def test_dashboard_freshness_states():
    now = time.time()
    assert TelemetryDashboard.classify_status(None, now) == "CONNECTING"
    assert TelemetryDashboard.classify_status({"timestamp": now}, now) == "LIVE"
    assert TelemetryDashboard.classify_status({"timestamp": now, "simulation_paused": True}, now) == "PAUSED"
    assert TelemetryDashboard.classify_status({"timestamp": now - 10}, now) == "STALE"


def dashboard_sample(frame, simulation_time, vehicles=10, buses=1, queued=2, pending=3):
    return {
        "frame_number": frame,
        "simulation_time_seconds": simulation_time,
        "network_summary": {
            "total_vehicles": vehicles,
            "total_buses": buses,
            "passenger_volume": 0,
            "queues": {"EB": queued},
            "pending_demand": pending,
        },
    }


def test_dashboard_history_downsamples_duplicates_and_clears_on_reset():
    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.initialize_history_state()
    dashboard.draw_trend_charts = lambda: None

    assert dashboard.record_history_sample(dashboard_sample(60, 1.0))
    assert not dashboard.record_history_sample(dashboard_sample(60, 1.0))
    assert not dashboard.record_history_sample(dashboard_sample(90, 1.5))
    assert dashboard.record_history_sample(dashboard_sample(120, 2.0, queued=4))
    assert list(dashboard.history["road_queue"]) == [2, 4]

    assert dashboard.record_history_sample(
        dashboard_sample(0, 0.0, vehicles=4, buses=0, queued=1, pending=0)
    )
    assert list(dashboard.history["time"]) == [0.0]
    assert list(dashboard.history["vehicles"]) == [4]
    assert list(dashboard.queue_history) == [1]


def test_dashboard_history_is_bounded_in_memory():
    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.initialize_history_state()
    dashboard.draw_trend_charts = lambda: None

    for second in range(HISTORY_MAX_POINTS + 25):
        dashboard.record_history_sample(
            dashboard_sample(second * 60, float(second), queued=second)
        )

    assert len(dashboard.history["time"]) == HISTORY_MAX_POINTS
    assert dashboard.history["time"][0] == 25.0
    assert len(dashboard.queue_history) == 20


def test_dashboard_initial_window_fits_smaller_screens_and_remains_useful():
    assert TelemetryDashboard.initial_window_size(1920, 1080) == (900, 780)
    assert TelemetryDashboard.initial_window_size(1366, 768) == (900, 628)
    assert TelemetryDashboard.initial_window_size(640, 480) == (560, 500)


def test_startup_window_layout_tiles_large_and_standard_hd_desktops():
    large = main.calculate_startup_window_layout(2560, 1440)
    hd = main.calculate_startup_window_layout(1920, 1080)

    assert large == {
        "mode": "tiled",
        "canvas_position": (900, 30),
        "control_geometry": "880x1030+10+10",
        "telemetry_geometry": "1000x760+900+640",
    }
    assert hd == {
        "mode": "tiled",
        "canvas_position": (900, 30),
        "control_geometry": "880x1030+10+10",
        "telemetry_geometry": "1000x400+900+640",
    }


def test_startup_window_layout_uses_on_screen_cascade_when_space_is_small():
    layout = main.calculate_startup_window_layout(1366, 768)

    assert layout["mode"] == "cascade"
    assert layout["canvas_position"] == (183, 30)
    assert layout["control_geometry"] == "880x718+476+10"
    assert layout["telemetry_geometry"] == "900x718+233+30"


def test_dashboard_responsive_profile_shrinks_content_without_scrollbars():
    full = TelemetryDashboard.responsive_profile(900, 780)
    compact = TelemetryDashboard.responsive_profile(560, 500)

    assert not full["compact"]
    assert compact["compact"]
    assert compact["header_font"] < full["header_font"]
    assert compact["metric_value_font"] < full["metric_value_font"]
    assert compact["metric_row_height"] < full["metric_row_height"]
    assert compact["phase_height"] < full["phase_height"]
    assert compact["node_height"] < full["node_height"]
    assert compact["chart_height"] < full["chart_height"]


def test_dashboard_line_chart_renders_sampled_series_without_gui():
    class FakeCanvas:
        def __init__(self):
            self.operations = []

        def delete(self, *args):
            self.operations.append(("delete", args, {}))

        @staticmethod
        def winfo_width():
            return 500

        @staticmethod
        def winfo_height():
            return 160

        def create_line(self, *args, **kwargs):
            self.operations.append(("line", args, kwargs))
            return len(self.operations)

        def create_text(self, *args, **kwargs):
            self.operations.append(("text", args, kwargs))
            return len(self.operations)

        def create_oval(self, *args, **kwargs):
            self.operations.append(("oval", args, kwargs))
            return len(self.operations)

        @staticmethod
        def bbox(_item):
            return (0, 0, 120, 12)

    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.initialize_history_state()
    dashboard.draw_trend_charts = lambda: None
    dashboard.record_history_sample(dashboard_sample(60, 1.0, vehicles=8))
    dashboard.record_history_sample(dashboard_sample(120, 2.0, vehicles=12))
    canvas_widget = FakeCanvas()

    dashboard.draw_line_chart(
        canvas_widget,
        (("vehicles", "Vehicles", "#123456"),),
    )

    assert any(
        operation == "line" and kwargs.get("smooth") is True
        for operation, _args, kwargs in canvas_widget.operations
    )
    assert any(
        operation == "text" and "Vehicles: 12.0" in kwargs.get("text", "")
        for operation, _args, kwargs in canvas_widget.operations
    )


def test_dashboard_phase_plan_and_marker_wrap_match_controller_cycle():
    timing = {
        "green_frames": 240,
        "yellow_frames": 60,
        "all_red_frames": 60,
    }
    cycle, segments = TelemetryDashboard.build_nominal_phase_segments(timing)

    assert cycle == 720
    assert segments["EW"][:2] == (
        (0, 240, "GREEN"),
        (240, 300, "YELLOW"),
    )
    assert segments["NS_A"][1:3] == (
        (360, 600, "GREEN"),
        (600, 660, "YELLOW"),
    )
    assert segments["NS_A"] == segments["NS_B"]
    assert TelemetryDashboard.phase_marker_fraction(0, cycle) == 0
    assert TelemetryDashboard.phase_marker_fraction(360, cycle) == 0.5
    assert TelemetryDashboard.phase_marker_fraction(720, cycle) == 0


def test_dashboard_phase_diagram_renders_live_priority_state_without_gui():
    class FakeLabel:
        def config(self, **kwargs):
            self.values = kwargs

    class FakeCanvas:
        def __init__(self):
            self.operations = []

        def _record(self, operation, args, kwargs):
            self.operations.append((operation, args, kwargs))
            return len(self.operations)

        def delete(self, *args):
            return self._record("delete", args, {})

        @staticmethod
        def winfo_width():
            return 900

        @staticmethod
        def winfo_height():
            return 145

        def create_text(self, *args, **kwargs):
            return self._record("text", args, kwargs)

        def create_rectangle(self, *args, **kwargs):
            return self._record("rectangle", args, kwargs)

        def create_line(self, *args, **kwargs):
            return self._record("line", args, kwargs)

        def create_polygon(self, *args, **kwargs):
            return self._record("polygon", args, kwargs)

        def create_oval(self, *args, **kwargs):
            return self._record("oval", args, kwargs)

    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.phase_cycle_canvas = FakeCanvas()
    dashboard.phase_status_lbl = FakeLabel()
    data = dashboard_sample(180, 3.0)
    data["signal_state"] = {
        "timing": {
            "frames_per_second": 60,
            "green_frames": 240,
            "yellow_frames": 60,
            "all_red_frames": 60,
        },
        "nodes": {
            "300": {
                "priority_state": "PRIORITY_ACTIVE",
                "signals": {"EB": "GREEN", "WB": "RED", "NB": "RED", "SB": "RED"},
            },
            "700": {
                "priority_state": "NORMAL",
                "signals": {"EB": "GREEN", "WB": "GREEN", "NB": "RED", "SB": "RED"},
            },
        },
    }

    dashboard.draw_phase_cycle(data)

    assert sum(
        operation == "oval"
        for operation, _args, _kwargs in dashboard.phase_cycle_canvas.operations
    ) == 3
    assert any(
        operation == "polygon"
        for operation, _args, _kwargs in dashboard.phase_cycle_canvas.operations
    )
    assert dashboard.phase_status_lbl.values == {
        "text": "PRIORITY OVERRIDE",
        "fg": "#2D8CFF",
    }
    assert not any(
        operation == "text"
        and kwargs.get("text") in ("PRIORITY OVERRIDE", "NORMAL PLAN", "ALL RED ACTIVE")
        for operation, _args, kwargs in dashboard.phase_cycle_canvas.operations
    )


def test_dashboard_poll_reschedules_after_unexpected_error():
    class FakeRoot:
        def __init__(self):
            self.after_calls = []

        def after(self, delay, callback):
            self.after_calls.append((delay, callback))

    class FakeLabel:
        def config(self, **kwargs):
            self.last = kwargs

    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.root = FakeRoot()
    dashboard.status_lbl = FakeLabel()
    dashboard.last_read_error = None
    dashboard.safe_read_telemetry = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    dashboard.poll_telemetry()
    assert dashboard.root.after_calls == [(250, dashboard.poll_telemetry)]
    assert dashboard.status_lbl.last["text"] == "TELEMETRY ERROR"


def test_gpu_poller_reads_nvidia_smi_and_degrades_gracefully(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "6144, 8192, 73, 187.5, 62\n"

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(telemetry_dashboard_module.subprocess, "run", fake_run)

    assert telemetry_dashboard_module.poll_gpu_stats() == {
        "vram_used_mb": 6144.0,
        "vram_total_mb": 8192.0,
        "gpu_util_pct": 73.0,
        "power_w": 187.5,
        "temp_c": 62.0,
    }
    assert calls[0][0][0] == "nvidia-smi"
    assert calls[0][1]["timeout"] == 2

    monkeypatch.setattr(
        telemetry_dashboard_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert telemetry_dashboard_module.poll_gpu_stats() == _gpu_none()


def test_llm_turn_reader_skips_partial_lines_and_deduplicates_poll(
    tmp_path, monkeypatch
):
    turn_log = tmp_path / "agent_turn_log.jsonl"
    record = {
        "turn": 1,
        "model": "test-model",
        "status": "OK",
        "flags": {},
    }
    turn_log.write_text(
        json.dumps(record) + "\nnot-json\n{\"turn\":",
        encoding="utf-8",
    )
    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.initialize_llm_monitor_state()

    records, size = dashboard.safe_read_agent_turns(turn_log)

    assert records == [record]
    assert size == turn_log.stat().st_size

    class FakeRoot:
        def __init__(self):
            self.after_calls = []

        def after(self, delay, callback):
            self.after_calls.append((delay, callback))

    dashboard.root = FakeRoot()
    dashboard.safe_read_ai_control = lambda: {
        "armed": True,
        "model": "test-model",
    }
    dashboard.safe_read_agent_turns = lambda: ([record], size)
    dashboard.update_llm_performance_display = lambda: None
    monkeypatch.setattr(
        telemetry_dashboard_module, "poll_gpu_stats", lambda: _gpu_none()
    )
    dashboard.poll_llm_performance()
    dashboard.poll_llm_performance()

    assert len(dashboard.llm_samples) == 1
    assert dashboard.llm_seen_turns == {1}
    assert dashboard.root.after_calls == [
        (
            telemetry_dashboard_module.LLM_POLL_MILLISECONDS,
            dashboard.poll_llm_performance,
        ),
        (
            telemetry_dashboard_module.LLM_POLL_MILLISECONDS,
            dashboard.poll_llm_performance,
        ),
    ]


def test_llm_sample_rollup_reconciles_status_tokens_and_gpu():
    flags_on = {
        "R1": {"tsp": True, "dbl": False},
        "R2": {"tsp": False, "dbl": True},
    }
    gpu = {
        "vram_used_mb": 6000,
        "vram_total_mb": 8192,
        "gpu_util_pct": 80,
        "power_w": 180,
        "temp_c": 60,
    }
    first = TelemetryDashboard.build_llm_sample(
        {
            "turn": 1,
            "model": "model-a",
            "status": "OK",
            "latency_ms": 1000,
            "output_tokens": 20,
            "tokens_per_sec": 10,
            "flags": flags_on,
        },
        gpu,
    )
    second = TelemetryDashboard.build_llm_sample(
        {
            "turn": 2,
            "model": "model-a",
            "status": "HELD_ALL_OFF",
            "latency_ms": 3000,
            "output_tokens": None,
            "tokens_per_sec": None,
            "flags": {},
        },
        {**gpu, "vram_used_mb": 7000, "power_w": 200},
    )

    summary = TelemetryDashboard.summarize_llm_samples([first, second])

    assert first["tsp_on_count"] == 1
    assert first["dbl_on_count"] == 1
    assert summary["turns"] == 2
    assert summary["ok_rate_pct"] == 50.0
    assert summary["held_rate_pct"] == 50.0
    assert summary["avg_latency_ms"] == 2000.0
    assert summary["avg_tokens_per_sec"] == 10.0
    assert summary["avg_vram_used_mb"] == 6500.0
    assert summary["peak_vram_used_mb"] == 7000.0
    assert summary["avg_power_w"] == 190.0
    assert summary["total_output_tokens"] == 20.0


def test_llm_excel_export_writes_samples_and_per_model_summary(tmp_path):
    from openpyxl import load_workbook

    class FakeLabel:
        def config(self, **kwargs):
            self.values = kwargs

    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.llm_export_status_lbl = FakeLabel()
    dashboard.llm_samples = [
        {
            "turn": 1,
            "timestamp": 100.0,
            "model": "model-a",
            "status": "OK",
            "latency_ms": 1000.0,
            "input_tokens": 100,
            "output_tokens": 20,
            "tokens_per_sec": 10.0,
            "tsp_on_count": 1,
            "dbl_on_count": 0,
            "reason": "Serve the approaching bus.",
            "vram_used_mb": 6000.0,
            "gpu_util_pct": 80.0,
            "power_w": 180.0,
            "temp_c": 60.0,
            "pax_per_min_recent": 400.0,
        },
        {
            "turn": 2,
            "timestamp": 105.0,
            "model": "model-a",
            "status": "HELD_ALL_OFF",
            "latency_ms": 45000.0,
            "input_tokens": None,
            "output_tokens": None,
            "tokens_per_sec": None,
            "tsp_on_count": 0,
            "dbl_on_count": 0,
            "reason": "",
            "vram_used_mb": 7000.0,
            "gpu_util_pct": 95.0,
            "power_w": 210.0,
            "temp_c": 65.0,
            "pax_per_min_recent": 350.0,
        },
    ]
    destination = tmp_path / "llm_perf_test.xlsx"

    assert dashboard.export_llm_performance(destination) == destination

    workbook = load_workbook(destination, data_only=True)
    try:
        assert workbook.sheetnames == ["Samples", "Summary"]
        samples = workbook["Samples"]
        summary = workbook["Summary"]
        sample_headers = [cell.value for cell in samples[1]]
        summary_headers = [cell.value for cell in summary[1]]
        assert samples.max_row == 3
        assert sample_headers[:4] == ["turn", "timestamp", "model", "status"]
        assert sample_headers[-1] == "pax_per_min_recent"
        assert summary.max_row == 2
        summary_row = {
            header: summary.cell(row=2, column=index + 1).value
            for index, header in enumerate(summary_headers)
        }
        assert summary_row["model"] == "model-a"
        assert summary_row["turns"] == 2
        assert summary_row["guard_ok_pct"] == 50.0
        assert summary_row["held_pct"] == 50.0
        assert summary_row["avg_latency_ms"] == 23000.0
        assert summary_row["total_output_tokens"] == 20.0
    finally:
        workbook.close()
    assert dashboard.llm_export_status_lbl.values["fg"] == "#2ECC71"

    dashboard.llm_samples = []
    empty_destination = tmp_path / "must_not_exist.xlsx"
    assert dashboard.export_llm_performance(empty_destination) is None
    assert not empty_destination.exists()
    assert dashboard.llm_export_status_lbl.values["text"] == "No samples yet"


def test_export_all_creates_four_sheets(tmp_path, monkeypatch):
    from openpyxl import load_workbook

    class FakeLabel:
        def config(self, **kwargs):
            self.values = kwargs

    route_id = telemetry_dashboard_module.ROUTE_ORDER[0]
    decision_log = tmp_path / "agent_turn_log.jsonl"
    telemetry_log = tmp_path / "telemetry_log.jsonl"
    decision_log.write_text(
        json.dumps(
            {
                "turn": 3,
                "timestamp": 100.0,
                "model": "model-a",
                "status": "OK",
                "reason": "Serve route one.",
                "pax_per_min_recent": 180.0,
                "flags": {route_id: {"tsp": True, "dbl": False}},
                "locked_routes": [route_id],
                "minimap": "ROUTES:\n1) route one",
                "raw_output": '{"tsp":[true]}',
            }
        )
        + "\nnot-json\n",
        encoding="utf-8",
    )
    telemetry_log.write_text(
        json.dumps(
            {
                "frame": 120,
                "sim_time_s": 2.0,
                "passengers_served_total": 49,
                "passengers_served_bus": 45,
                "passengers_served_car": 4,
                "buses_served": 1,
                "cars_served": 1,
                "pax_per_min_cumulative": 1470.0,
                "pax_per_min_recent": 49.0,
                "vehicles_in_network": 8,
                "ai_armed": True,
                "ai_last_status": "OK",
                "queues_vehicles": {"EB": 2},
                "queues_passengers_est": {"EB": 8},
            }
        )
        + "\n{\"partial\":",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        telemetry_dashboard_module, "AGENT_TURN_LOG_FILE", decision_log
    )
    monkeypatch.setattr(
        telemetry_dashboard_module, "TELEMETRY_LOG_FILE", telemetry_log
    )

    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.export_all_status_lbl = FakeLabel()
    dashboard.llm_samples = [
        {
            "turn": 3,
            "timestamp": 100.0,
            "model": "model-a",
            "status": "OK",
            "latency_ms": 800.0,
            "input_tokens": 120,
            "output_tokens": 24,
            "tokens_per_sec": 30.0,
            "tsp_on_count": 1,
            "dbl_on_count": 0,
            "reason": "Serve route one.",
            "vram_used_mb": 5000.0,
            "gpu_util_pct": 70.0,
            "power_w": 150.0,
            "temp_c": 58.0,
            "pax_per_min_recent": 49.0,
        }
    ]
    destination = tmp_path / "combined.xlsx"

    assert dashboard.export_all(destination) == destination

    workbook = load_workbook(destination, data_only=True)
    try:
        assert workbook.sheetnames == [
            "Decisions",
            "Telemetry",
            "LLM Performance",
            "LLM Summary",
        ]
        assert all(workbook[name].max_row == 2 for name in workbook.sheetnames)
        decision_headers = [cell.value for cell in workbook["Decisions"][1]]
        assert workbook["Decisions"].cell(
            row=2,
            column=decision_headers.index(f"{route_id}_tsp") + 1,
        ).value is True
        telemetry_headers = [cell.value for cell in workbook["Telemetry"][1]]
        assert workbook["Telemetry"].cell(
            row=2,
            column=telemetry_headers.index("ai_armed") + 1,
        ).value is True
        assert workbook["LLM Performance"]["C2"].value == "model-a"
        assert workbook["LLM Summary"]["A2"].value == "model-a"
    finally:
        workbook.close()
    assert dashboard.export_all_status_lbl.values["fg"] == "#2ECC71"


def test_export_all_missing_logs_ok(tmp_path, monkeypatch):
    from openpyxl import load_workbook

    class FakeLabel:
        def config(self, **kwargs):
            self.values = kwargs

    monkeypatch.setattr(
        telemetry_dashboard_module,
        "AGENT_TURN_LOG_FILE",
        tmp_path / "missing_decisions.jsonl",
    )
    monkeypatch.setattr(
        telemetry_dashboard_module,
        "TELEMETRY_LOG_FILE",
        tmp_path / "missing_telemetry.jsonl",
    )
    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.export_all_status_lbl = FakeLabel()
    dashboard.llm_samples = [
        {"turn": 1, "model": "model-a", "status": "OK"}
    ]
    destination = tmp_path / "missing_logs.xlsx"

    assert dashboard.export_all(destination) == destination

    workbook = load_workbook(destination, data_only=True)
    try:
        assert workbook["Decisions"].max_row == 1
        assert workbook["Telemetry"].max_row == 1
        assert workbook["LLM Performance"].max_row == 2
        assert workbook["LLM Summary"].max_row == 2
    finally:
        workbook.close()


def test_export_all_empty_writes_nothing(tmp_path, monkeypatch):
    class FakeLabel:
        def config(self, **kwargs):
            self.values = kwargs

    monkeypatch.setattr(
        telemetry_dashboard_module,
        "AGENT_TURN_LOG_FILE",
        tmp_path / "missing_decisions.jsonl",
    )
    monkeypatch.setattr(
        telemetry_dashboard_module,
        "TELEMETRY_LOG_FILE",
        tmp_path / "missing_telemetry.jsonl",
    )
    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.export_all_status_lbl = FakeLabel()
    dashboard.llm_samples = []
    destination = tmp_path / "must_not_exist.xlsx"

    assert dashboard.export_all(destination) is None
    assert not destination.exists()
    assert dashboard.export_all_status_lbl.values == {
        "text": "Nothing to export yet",
        "fg": telemetry_dashboard_module.COLOR_WARNING,
    }


def test_combined_decisions_matches_session_columns(tmp_path, monkeypatch):
    from openpyxl import load_workbook

    class FakeLabel:
        def config(self, **kwargs):
            self.values = kwargs

    decision_log = tmp_path / "agent_turn_log.jsonl"
    decision_log.write_text(
        json.dumps(
            {
                "turn": 1,
                "model": "model-a",
                "status": "OK",
                "flags": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    missing_telemetry = tmp_path / "missing_telemetry.jsonl"
    monkeypatch.setattr(
        telemetry_dashboard_module, "AGENT_TURN_LOG_FILE", decision_log
    )
    monkeypatch.setattr(
        telemetry_dashboard_module, "TELEMETRY_LOG_FILE", missing_telemetry
    )
    monkeypatch.setattr(main, "AGENT_TURN_LOG_PATH", decision_log)
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", missing_telemetry)

    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.export_all_status_lbl = FakeLabel()
    dashboard.llm_samples = []
    combined_destination = tmp_path / "combined.xlsx"
    session_destination = tmp_path / "session.xlsx"

    assert dashboard.export_all(combined_destination) == combined_destination
    assert main.export_session_excel(session_destination) == session_destination

    combined = load_workbook(combined_destination, data_only=True)
    session = load_workbook(session_destination, data_only=True)
    try:
        combined_headers = [cell.value for cell in combined["Decisions"][1]]
        session_headers = [cell.value for cell in session["Decisions"][1]]
        assert combined_headers == session_headers
        assert combined_headers == telemetry_dashboard_module.DECISION_EXPORT_HEADERS
    finally:
        combined.close()
        session.close()


def test_summary_snapshot_export_reconciles_live_telemetry(tmp_path):
    from openpyxl import load_workbook

    class FakeLabel:
        def config(self, **kwargs):
            self.values = kwargs

    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.summary_export_status_lbl = FakeLabel()
    dashboard.latest_telemetry = {
        "timestamp": time.time(),
        "frame_number": 120,
        "simulation_time_seconds": 2.0,
        "simulation_paused": False,
        "simulation_speed": 1.0,
        "network_summary": {
            "total_vehicles": 10,
            "total_buses": 2,
            "passenger_volume": 122,
            "queues": {"EB": 3},
            "queues_passengers_est": {"EB": 12},
            "pending_demand": 1,
        },
        "network_throughput": {
            "passengers_served_total": 90,
            "passengers_per_minute_recent": 45.0,
        },
        "network_discharge": {"active": False, "status": "IDLE"},
        "demand_generation": {
            "EB": {
                "active": True,
                "model": "Poisson",
                "configured_rate_vpm": 20,
                "effective_rate_vpm": 20.0,
                "pending_arrivals": 1,
                "peak_active": False,
            }
        },
        "routes": {
            "R1_EB_A_NB": {
                "route_name": "EB to Node A",
                "active": True,
                "tsp_enabled": True,
                "dbl_enabled": False,
                "buses_on_route": 2,
            }
        },
        "signal_state": {
            "nodes": {
                "300": {
                    "node_x": 300,
                    "phase": "EW_GREEN",
                    "phase_index": 0,
                    "phase_timer_frames": 20,
                    "priority_state": "NORMAL",
                    "priority_timer_frames": 0,
                    "signals": {
                        "EB": "GREEN",
                        "WB": "GREEN",
                        "NB": "RED",
                        "SB": "RED",
                    },
                    "active_request": None,
                    "queued_requests": [],
                    "reservation_count": 1,
                }
            }
        },
    }
    destination = tmp_path / "summary.xlsx"

    assert dashboard.export_summary_snapshot(destination) == destination

    workbook = load_workbook(destination, data_only=True)
    try:
        assert workbook.sheetnames == [
            "Overview",
            "Queues & Demand",
            "Routes",
            "Signal Nodes",
        ]
        overview = {
            row[0]: row[1]
            for row in workbook["Overview"].iter_rows(
                min_row=2, values_only=True
            )
        }
        assert overview["network_summary.total_vehicles"] == 10
        assert overview["network_throughput.passengers_served_total"] == 90
        assert workbook["Queues & Demand"]["A2"].value == "EB"
        assert workbook["Routes"]["A2"].value == "R1_EB_A_NB"
        assert workbook["Signal Nodes"]["A2"].value == "300"
    finally:
        workbook.close()
    assert dashboard.summary_export_status_lbl.values["fg"] == "#2ECC71"

    dashboard.latest_telemetry = None
    empty_destination = tmp_path / "no_summary.xlsx"
    assert dashboard.export_summary_snapshot(empty_destination) is None
    assert not empty_destination.exists()


def test_session_trends_export_uses_only_in_memory_history(tmp_path):
    from openpyxl import load_workbook

    class FakeLabel:
        def config(self, **kwargs):
            self.values = kwargs

    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.initialize_history_state()
    dashboard.draw_trend_charts = lambda: None
    dashboard.trends_export_status_lbl = FakeLabel()
    dashboard.record_history_sample(
        dashboard_sample(60, 1.0, vehicles=8, buses=1, queued=2, pending=3)
    )
    dashboard.record_history_sample(
        dashboard_sample(120, 2.0, vehicles=12, buses=2, queued=6, pending=1)
    )
    destination = tmp_path / "trends.xlsx"

    assert dashboard.export_session_trends(destination) == destination

    workbook = load_workbook(destination, data_only=True)
    try:
        assert workbook.sheetnames == ["Session Trends", "Summary"]
        trends = workbook["Session Trends"]
        assert trends.max_row == 3
        assert [cell.value for cell in trends[1]] == [
            "time_seconds",
            "vehicles",
            "buses",
            "road_queue",
            "pending_demand",
            "congestion_pct",
        ]
        summary = {
            row[0]: row[1]
            for row in workbook["Summary"].iter_rows(
                min_row=2, values_only=True
            )
        }
        assert summary["samples"] == 2
        assert summary["average_vehicles"] == 10.0
        assert summary["peak_road_queue"] == 6
    finally:
        workbook.close()
    assert dashboard.trends_export_status_lbl.values["fg"] == "#2ECC71"

    dashboard.clear_history()
    empty_destination = tmp_path / "no_trends.xlsx"
    assert dashboard.export_session_trends(empty_destination) is None
    assert not empty_destination.exists()


def test_runtime_paths_are_source_relative():
    project = Path(main.__file__).resolve().parent
    assert main.DASHBOARD_PATH == project / "telemetry_dashboard.py"
    assert main.TELEMETRY_PATH == project / "traffic_state_telemetry.json"
    assert DEFAULT_TELEMETRY_PATH == project / "traffic_state_telemetry.json"


def test_main_uses_monotonic_elapsed_time_with_bounded_catchup():
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert "time.monotonic()" in source
    assert "max_catchup_seconds" in source
    assert "time_accumulator += elapsed * sim_speed" in source
    assert "max_steps_per_callback = 6" in source
    assert "steps_this_callback < max_steps_per_callback" in source


def test_control_panel_uses_normal_desktop_window_stacking():
    source = Path(control_panel.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    topmost_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "attributes"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "-topmost"
    ]

    assert len(topmost_calls) == 1
    assert isinstance(topmost_calls[0].args[1], ast.Constant)
    assert topmost_calls[0].args[1].value is False
    assert "overrideredirect" not in source


def test_control_panel_exposes_congestion_peak_model():
    source = Path(control_panel.__file__).read_text(encoding="utf-8")
    assert main.CONGESTION_MODEL in source


def test_congestion_backlog_survives_blocked_spawn_and_drains_on_admission(
    monkeypatch,
):
    main.reset_all_spawner_states()
    monkeypatch.setattr(main.random, "random", lambda: 0.0)
    monkeypatch.setattr(main.random, "choice", lambda values: values[0])
    monkeypatch.setattr(main.random, "uniform", lambda low, high: low)
    lanes = lane_options()
    config = {
        "model": main.CONGESTION_MODEL,
        "rate": 30,
        "turn_split": 1.0,
        "heavy_ratio": 0.0,
    }
    blocker = Vehicle(-20, lanes["EB"][0], "EB", lane_index=0)
    vehicles = [blocker]

    main.try_spawn_vehicle(
        vehicles, "EB", "EB", -20, lanes["EB"], config
    )
    state = main.spawner_states["EB"]
    assert state["pending_arrivals"] == 1
    assert state["admitted_arrivals"] == 0
    assert len(vehicles) == 1

    vehicles.clear()
    main.try_spawn_vehicle(
        vehicles, "EB", "EB", -20, lanes["EB"], config
    )
    assert state["pending_arrivals"] == 1
    assert state["requested_arrivals"] == 2
    assert state["admitted_arrivals"] == 1
    assert len(vehicles) == 1
    main.reset_all_spawner_states()


def test_congestion_peak_cycles_to_recovery_and_caps_backlog(monkeypatch):
    main.reset_all_spawner_states()
    monkeypatch.setattr(main.random, "random", lambda: 1.0)
    assert not main.should_spawn_vehicle("EB", main.CONGESTION_MODEL, 30)
    state = main.spawner_states["EB"]
    assert state["peak_active"] is True
    assert state["effective_rate_vpm"] == 120.0

    state["congestion_cycle_frame"] = main.CONGESTION_PEAK_SECONDS * 60
    assert not main.should_spawn_vehicle("EB", main.CONGESTION_MODEL, 30)
    assert state["peak_active"] is False
    assert state["effective_rate_vpm"] == 30.0

    state["congestion_cycle_frame"] = 0
    state["pending_arrivals"] = main.MAX_PENDING_ARRIVALS
    monkeypatch.setattr(main.random, "random", lambda: 0.0)
    assert main.should_spawn_vehicle("EB", main.CONGESTION_MODEL, 30)
    assert state["pending_arrivals"] == main.MAX_PENDING_ARRIVALS
    assert state["overflow_arrivals"] == 1
    main.reset_all_spawner_states()


def test_llm_callbacks_write_runtime_control_instead_of_remaining_placeholders():
    source = Path(control_panel.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for callback_name in (
        "on_run_llm",
        "on_llm_engine_selected",
        "on_tick_seconds_changed",
    ):
        callback = functions[callback_name]
        assert not any(isinstance(node, ast.Pass) for node in ast.walk(callback))
        called_functions = {
            node.func.id
            for node in ast.walk(callback)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "write_ai_control" in called_functions


def test_priority_telemetry_distinguishes_pending_from_active():
    control_panel.bus_routes_config["R1_EB_A_NB"]["dbl_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300, "TELEMETRY_BUS")
    controller = SignalController({"green_time": 100}, 2, 2)
    exporter = TelemetryExporter(export_interval_frames=1)

    controller.update([bus])
    pending = exporter.build_payload(controller, [bus], 1)["active_buses"][0]
    assert pending["priority_transitioning"] is True
    assert pending["priority_requested"] is True
    assert pending["dbl_priority_pending"] is True
    assert pending["priority_granted"] is False
    assert pending["dbl_active_triggered"] is False
    assert controller.get_all_dbl_states()[300]["EB"] == "TRANSITIONING"

    for _ in range(10):
        controller.update([bus])
        if controller.get_node_status(300)["priority_state"] == "PRIORITY_ACTIVE":
            break
    active = exporter.build_payload(controller, [bus], 2)["active_buses"][0]
    assert active["priority_transitioning"] is False
    assert active["priority_requested"] is True
    assert active["priority_granted"] is True
    assert active["dbl_priority_pending"] is False
    assert active["dbl_active_triggered"] is True
    assert controller.get_all_dbl_states()[300]["EB"] == "ACTIVE"


def test_dashboard_shows_active_and_pending_priority_separately():
    class FakeLabel:
        def config(self, **kwargs):
            self.values = kwargs

    class FakeRoot:
        @staticmethod
        def update_idletasks():
            pass

    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.queue_history = []
    dashboard.root = FakeRoot()
    dashboard.vars = {
        key: FakeLabel()
        for key in (
            "vehicles", "buses", "passengers", "queued", "delay",
            "congestion", "tsp", "dbl", "timer"
        )
    }
    dashboard.node_a_canvas = object()
    dashboard.node_b_canvas = object()
    dashboard.draw_intersection = lambda *args, **kwargs: None
    data = {
        "frame_number": 1,
        "network_summary": {
            "total_vehicles": 2,
            "total_buses": 2,
            "passenger_volume": 90,
            "queues": {},
            "pending_demand": 4,
        },
        "active_buses": [
            {"tsp_active_triggered": True, "dbl_priority_pending": True},
            {"tsp_priority_pending": True, "dbl_active_triggered": True},
        ],
        "signal_state": {"nodes": {}},
    }

    dashboard.update_metrics(data)

    assert dashboard.vars["tsp"].values["text"] == "1/1"
    assert dashboard.vars["dbl"].values["text"] == "1/1"
    assert dashboard.vars["queued"].values["text"] == "0/4"


def test_dbl_lamp_uses_distinct_pending_active_and_clearing_colors(monkeypatch):
    monkeypatch.setattr(canvas, "_get_dbl_flash_clock", lambda _paused: 0.0)
    surface = pygame.Surface((100, 100))
    x, y = 50, 50
    light_center = canvas.get_signal_light_center(x, y, "EASTBOUND")
    lamp_center = (light_center[0], light_center[1] - 16)
    expected_colors = {
        "INACTIVE": (0, 0, 0),
        "TRANSITIONING": (245, 158, 11),
        "ACTIVE": (0, 255, 120),
        "CLEARING": (0, 200, 220),
    }

    for state, expected in expected_colors.items():
        canvas.draw_dbl_signal(surface, x, y, "EASTBOUND", state, False)
        assert surface.get_at(lamp_center)[:3] == expected
