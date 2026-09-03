import ast
import json
from pathlib import Path
import time

import control_panel
import canvas_gemini as canvas
import main
import pygame
from canvas_gemini import H_Y, LANE
from signal_controller import SignalController
from telemetry_dashboard import HISTORY_MAX_POINTS, TelemetryDashboard
from telemetry_exporter import DEFAULT_TELEMETRY_PATH, TelemetryExporter
from vehicle import Vehicle
from tests.helpers import make_bus_for_leg


def lane_options():
    return {
        "EB": [H_Y - 0.5 * LANE, H_Y - 1.5 * LANE, H_Y - 2.5 * LANE],
        "WB": [H_Y + 0.5 * LANE, H_Y + 1.5 * LANE, H_Y + 2.5 * LANE],
    }


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
    assert payload["schema_version"] == 2
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
    assert exporter.export(controller, [], 15)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["frame_number"] == 15
    assert not list(tmp_path.glob("tmp*"))


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


def test_llm_callbacks_remain_placeholders():
    source = Path(control_panel.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    run_llm = functions["on_run_llm"]
    assert len(run_llm.body) == 1 and isinstance(run_llm.body[0], ast.Pass)
    selector = functions["on_llm_engine_selected"]
    assigned_names = {
        target.id
        for node in ast.walk(selector)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert not assigned_names


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
