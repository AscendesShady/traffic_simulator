"""Checkpoint exports, mode-split passenger delay, and the cross-run summary.

These three pieces turn a timed benchmark run into comparison-ready data:
automatic cumulative workbooks at standard sim-minute marks (Part A), delay
split bus vs car and passenger-weighted (Part B), and one persistent CSV row
per checkpoint of every run (Part C). None of this touches simulation
behaviour -- it only reads the same cumulative counters and logs the
existing exports already read.
"""
import csv
import json

import pytest

import src.ui.canvas_gemini as canvas
import src.ui.control_panel as control_panel
import src.core.main as main
from src.core.signal_controller import SignalController
from src.telemetry.telemetry_exporter import TelemetryExporter
from src.core.vehicle import Bus, Vehicle
from src.ui.canvas_gemini import H_Y, LANE
from tests.helpers import make_bus_for_leg, NODE_A, NODE_B


# --------------------------------------------------------------------------
# Shared fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_experiment_paths(tmp_path, monkeypatch):
    """Every test gets its own logs and its own experiment_summary.csv."""
    monkeypatch.setattr(main, "AGENT_TURN_LOG_PATH", tmp_path / "turns.jsonl")
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(main, "BUS_EVENTS_LOG_PATH", tmp_path / "bus_events.jsonl")
    monkeypatch.setattr(main, "TELEMETRY_PATH", tmp_path / "snapshot.json")
    monkeypatch.setattr(main, "EXCEL_EXPORT_DIR", tmp_path / "excel_exports")
    monkeypatch.setattr(main, "EXPERIMENT_SUMMARY_PATH", tmp_path / "experiment_summary.csv")
    monkeypatch.setattr(control_panel, "write_ai_control", lambda *a, **k: None)
    for key in main.network_throughput:
        main.network_throughput[key] = 0
    main.pending_checkpoints_sec.clear()
    yield
    main.pending_checkpoints_sec.clear()


def read_summary_rows():
    path = main.experiment_summary_path()
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------
# Part A -- checkpoint marks
# --------------------------------------------------------------------------

def test_checkpoints_fire_at_marks(monkeypatch):
    """A 15-min test fires intermediate exports at 5 and 10 (the duration
    mark, 15, is the separate, existing finish_timed_test path -- see
    test_checkpoint_does_not_stop_run). A 5-min test has nothing to fire
    intermediately: its only export is the duration mark itself."""
    exported_marks = []
    monkeypatch.setattr(
        main, "export_test_workbook",
        lambda *a, **k: exported_marks.append(k.get("checkpoint_sim_seconds")),
    )
    monkeypatch.setattr(main, "append_experiment_summary_row", lambda mark, **kwargs: None)

    assert main.compute_checkpoint_marks(900) == [300, 600]
    assert main.compute_checkpoint_marks(300) == []
    assert main.compute_checkpoint_marks(1800) == [300, 600, 900]
    assert main.compute_checkpoint_marks(3600) == [300, 600, 900, 1800]
    assert main.compute_checkpoint_marks(7200) == [300, 600, 900, 1800, 3600]
    assert main.compute_checkpoint_marks(None) == []
    assert main.compute_checkpoint_marks(0) == []

    control_panel.global_config["test_running"] = True
    control_panel.global_config["test_duration_sim_seconds"] = 900
    main.pending_checkpoints_sec.extend(main.compute_checkpoint_marks(900))

    main.fire_due_checkpoints(4 * 60 * 60)  # 4:00 -- nothing due yet
    assert exported_marks == []

    main.fire_due_checkpoints(5 * 60 * 60)  # 5:00 -- first mark
    assert exported_marks == [300]

    main.fire_due_checkpoints(9 * 60 * 60)  # 9:00 -- second not due yet
    assert exported_marks == [300]

    main.fire_due_checkpoints(10 * 60 * 60)  # 10:00 -- second mark
    assert exported_marks == [300, 600]

    # The 15:00 (duration) mark was never queued here: it belongs to
    # finish_timed_test, not fire_due_checkpoints.
    main.fire_due_checkpoints(20 * 60 * 60)
    assert exported_marks == [300, 600]


def test_checkpoint_fires_once_for_a_five_minute_test(monkeypatch):
    exported_marks = []
    monkeypatch.setattr(
        main, "export_test_workbook",
        lambda *a, **k: exported_marks.append(k.get("checkpoint_sim_seconds")),
    )
    monkeypatch.setattr(main, "append_experiment_summary_row", lambda mark, **kwargs: None)

    control_panel.global_config["test_running"] = True
    control_panel.global_config["test_duration_sim_seconds"] = 300
    main.pending_checkpoints_sec.extend(main.compute_checkpoint_marks(300))

    main.fire_due_checkpoints(300 * 60)
    assert exported_marks == []  # no intermediate mark for a 5-min test

    destination_calls = []
    monkeypatch.setattr(
        main, "export_test_workbook",
        lambda *a, **k: destination_calls.append(k.get("checkpoint_sim_seconds")),
    )
    main.finish_timed_test(300 * 60)
    assert destination_calls == [300]  # exactly one export total, at cp5


def test_checkpoint_does_not_stop_run(monkeypatch):
    monkeypatch.setattr(main, "export_test_workbook", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_experiment_summary_row", lambda mark, **kwargs: None)

    control_panel.global_config["test_running"] = True
    control_panel.global_config["is_running"] = True
    control_panel.global_config["test_duration_sim_seconds"] = 900
    main.pending_checkpoints_sec.extend(main.compute_checkpoint_marks(900))

    main.fire_due_checkpoints(5 * 60 * 60)

    assert control_panel.global_config["test_running"] is True
    assert control_panel.global_config["is_running"] is True

    # Only the final (duration) mark, via finish_timed_test, stops the run.
    main.finish_timed_test(900 * 60)
    assert control_panel.global_config["test_running"] is False
    assert control_panel.global_config["is_running"] is False


def test_checkpoint_gated_on_test_running(monkeypatch):
    """A plain (non-timed-test) run never fires checkpoints, even with
    marks somehow queued."""
    calls = []
    monkeypatch.setattr(main, "export_test_workbook", lambda *a, **k: calls.append(1))
    control_panel.global_config["test_running"] = False
    main.pending_checkpoints_sec.extend([300, 600])

    main.fire_due_checkpoints(600 * 60)

    assert calls == []


def test_perform_full_reset_schedules_checkpoints_only_for_a_timed_test(monkeypatch):
    class FakeSignals:
        def reset_all_state(self):
            pass

    monkeypatch.setattr(main, "calibrate_and_apply_webster", lambda signals: None)
    monkeypatch.setattr(main, "reset_traffic_generation", lambda: None)

    control_panel.global_config["test_running"] = True
    control_panel.global_config["test_duration_sim_seconds"] = 1800
    main.perform_full_reset([], FakeSignals())
    assert main.pending_checkpoints_sec == [300, 600, 900]

    control_panel.global_config["test_running"] = False
    main.perform_full_reset([], FakeSignals())
    assert main.pending_checkpoints_sec == []


def test_perform_full_reset_snapshots_inputs_and_drops_the_old_decision(monkeypatch, tmp_path):
    """The run's verifiable setup is the start snapshot (the end-of-run inputs
    table shows the decider's terminal flags), and the previous arm's
    decision.json must not open the run as FOREIGN_DECISION."""
    class FakeSignals:
        def reset_all_state(self):
            pass

    monkeypatch.setattr(main, "calibrate_and_apply_webster", lambda signals: None)
    monkeypatch.setattr(main, "reset_traffic_generation", lambda: None)
    stale = tmp_path / "decision.json"
    stale.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "DECISION_PATH", stale)
    control_panel.global_config["ai_runtime"]["last_status"] = "OK"
    control_panel.global_config["test_running"] = True
    control_panel.global_config["random_seed"] = 4242
    main.perform_full_reset([], FakeSignals())
    assert not stale.exists()
    assert control_panel.global_config["ai_runtime"]["last_status"] is None
    rows = control_panel.global_config["_initial_input_rows"]
    assert ["Global", "random_seed", 4242] in rows
    assert all(
        row[2] is False for row in rows
        if row[0].startswith("Route: ") and row[1] in ("tsp_enabled", "dbl_enabled")
    )
    control_panel.global_config["test_running"] = False


def test_checkpoint_filenames_unique():
    names = {
        mark: main.build_test_export_filename(
            "nemotron", 1800, 42, timestamp="20260913_120000",
            checkpoint_sim_seconds=mark,
        )
        for mark in (300, 600, 900, 1800)
    }
    assert len(set(names.values())) == 4
    # "nemotron" is an LLM arm, so it carries its control mode.
    assert names[300] == "nemotron-assisted_5min_42seed_13092026_120000.xlsx"
    assert names[600] == "nemotron-assisted_10min_42seed_13092026_120000.xlsx"
    assert names[900] == "nemotron-assisted_15min_42seed_13092026_120000.xlsx"
    assert names[1800] == "nemotron-assisted_30min_42seed_13092026_120000.xlsx"

    # An unqualified call (the final-mark path) uses its own duration.
    default_tagged = main.build_test_export_filename(
        "nemotron", 1800, 42, timestamp="20260913_120000"
    )
    assert default_tagged == names[1800]


def test_filename_separates_the_two_llm_control_modes():
    """The same model in the two modes must not collide.

    Before this, an assisted and a configured run of one model differed in
    nothing but their timestamp (results/orca-mini-7b_10min_234seed_*.xlsx,
    2026-09-22). The word matches the run's `control_mode` summary column.
    """
    def name(model, mode):
        return main.build_test_export_filename(
            model, 900, 42, timestamp="20260913_120000", control_mode=mode,
        )

    assert name("orca-mini:7b", "assisted") == (
        "orca-mini-7b-assisted_15min_42seed_13092026_120000.xlsx"
    )
    assert name("orca-mini:7b", "configured") == (
        "orca-mini-7b-configured_15min_42seed_13092026_120000.xlsx"
    )

    # Baseline and the rule comparators cannot run configured, so they name
    # themselves and keep exactly the filenames they always had.
    assert name("None", "assisted") == "baseline_15min_42seed_13092026_120000.xlsx"
    assert name(control_panel.RULE_BASED_MODEL, "assisted") == (
        "rule-based_15min_42seed_13092026_120000.xlsx"
    )
    assert name(control_panel.MAX_PRESSURE_MODEL, "assisted") == (
        "passenger-pressure-tsp_15min_42seed_13092026_120000.xlsx"
    )


# --------------------------------------------------------------------------
# Part B -- passenger-weighted delay, split bus vs car
# --------------------------------------------------------------------------

def test_accumulate_frame_metrics_splits_by_mode():
    """A queued bus contributes its full 45 passengers per frame to the bus
    accumulator; a queued car contributes its 4 to the car accumulator."""
    bus = make_bus_for_leg("R1_EB_A_NB", NODE_A, "DELAY_BUS")
    bus.speed = 0.0
    car = Vehicle(0, H_Y - 0.5 * LANE, "EB")
    car.speed = 0.0
    moving_truck = Vehicle(100, H_Y - 0.5 * LANE, "EB", is_heavy=True)
    moving_truck.speed = 5.0  # not stopped: must not count

    for _ in range(10):
        main.accumulate_frame_metrics([bus, car, moving_truck])

    assert main.network_throughput["bus_passenger_delay_frames"] == 45 * 10
    assert main.network_throughput["car_passenger_delay_frames"] == 4 * 10
    assert main.network_throughput["stopped_vehicle_frames"] == 2 * 10
    assert main.network_throughput["vehicles_in_network_sample_count"] == 10
    assert main.network_throughput["vehicles_in_network_frame_sum"] == 3 * 10
    assert main.network_throughput["vehicles_in_network_max"] == 3

    # A frame with fewer vehicles never lowers the running max.
    main.accumulate_frame_metrics([car])
    assert main.network_throughput["vehicles_in_network_max"] == 3


def test_delay_split_by_mode():
    """The exporter turns the mode-split frame accumulators into
    person-hours and per-passenger means, alongside the existing aggregate."""
    exporter = TelemetryExporter(export_interval_frames=1)
    controller = SignalController({"green_time": 20})
    throughput = {
        "vehicles_served_total": 2,
        "passengers_served_bus": 45,
        "passengers_served_car": 4,
        "stopped_vehicle_frames": 900,
        "bus_passenger_delay_frames": 45 * 600,   # bus stopped 600 frames = 10s
        "car_passenger_delay_frames": 4 * 300,    # car stopped 300 frames = 5s
        "completed_stopped_vehicle_frames": 900,
        "completed_bus_passenger_delay_frames": 45 * 600,
        "completed_car_passenger_delay_frames": 4 * 300,
    }

    payload = exporter.build_payload(controller, [], 60, throughput_state=throughput)
    delay = payload["delay"]

    assert delay["bus_passenger_delay_sec"] == pytest.approx(450.0)
    assert delay["car_passenger_delay_sec"] == pytest.approx(20.0)
    assert delay["mean_bus_passenger_delay_sec"] == pytest.approx(10.0)
    assert delay["mean_car_passenger_delay_sec"] == pytest.approx(5.0)
    # Rounded to 4 decimals in the payload, so compare at that precision.
    assert delay["bus_person_hours_delay"] == pytest.approx(450.0 / 3600.0, abs=1e-4)
    assert delay["car_person_hours_delay"] == pytest.approx(20.0 / 3600.0, abs=1e-4)
    assert delay["total_person_hours_delay"] == pytest.approx(470.0 / 3600.0, abs=1e-4)
    # The old aggregate is unchanged and still present (backward compat).
    assert payload["network_throughput"]["mean_stopped_delay_sec_per_vehicle"] == (
        pytest.approx(900 / 60.0 / 2, abs=0.01)
    )


def test_means_divide_the_completed_cohort_only():
    """A vehicle still queued at the checkpoint is in the cumulative totals
    (network exposure) but not in any per-vehicle or per-passenger mean:
    the means read the completed_* counters _record_completed_vehicle_delay
    fills, so the audit's early-checkpoint inflation cannot happen."""
    done = Vehicle(0, H_Y - 0.5 * LANE, "EB")
    stuck = Vehicle(100, H_Y - 0.5 * LANE, "EB")
    done.speed = stuck.speed = 0.0
    for _ in range(600):
        main.accumulate_frame_metrics([done, stuck])
    done.passed_nodes = {NODE_A}
    main._record_completed_vehicle_delay(done)
    main.network_throughput["vehicles_served_total"] = 1
    main.network_throughput["passengers_served_car"] = 4
    main.network_throughput["passengers_served_total"] = 4

    _seed_regime()
    row = main.build_experiment_summary_row(60)
    assert main.network_throughput["stopped_vehicle_frames"] == 1200
    assert row["car_person_hours_delay"] == pytest.approx(2 * 4 * 10 / 3600, abs=1e-4)
    assert row["mean_stopped_delay_sec_per_vehicle"] == pytest.approx(10.0)  # not 20
    assert row["mean_car_passenger_delay_sec"] == pytest.approx(10.0)
    assert row["mean_control_delay_sec_per_vehicle"] == pytest.approx(10.0)
    assert row["unfinished_stopped_person_hours"] == pytest.approx(4 * 10 / 3600, abs=1e-4)
    assert row["passenger_hours_in_network"] == pytest.approx(8 * 10 / 3600, abs=1e-4)
    assert row["level_of_service"] == "A"

    payload = TelemetryExporter(export_interval_frames=1).build_payload(
        SignalController({"green_time": 20}), [stuck], 600,
        throughput_state=main.network_throughput,
    )
    assert payload["network_throughput"]["mean_stopped_delay_sec_per_vehicle"] == 10.0
    assert payload["network_throughput"]["mean_control_delay_sec_per_vehicle"] == 10.0
    assert payload["delay"]["mean_car_passenger_delay_sec"] == 10.0


def test_level_of_service_reads_control_delay_not_stopped_delay():
    """LOS is arrival-to-departure delay (time below own free-flow speed,
    which sees crawl); stopped delay is only a proxy and is labelled so."""
    crawler = Vehicle(0, H_Y - 0.5 * LANE, "EB", max_speed=1.0)
    crawler.speed = 0.5  # never "stopped", loses half a frame per frame
    for _ in range(60 * 60):
        main.accumulate_frame_metrics([crawler])
    main._record_completed_vehicle_delay(crawler)
    main.network_throughput.update({
        "vehicles_served_total": 1, "passengers_served_car": 4, "passengers_served_total": 4,
    })
    _seed_regime()
    row = main.build_experiment_summary_row(60)
    assert row["mean_stopped_delay_sec_per_vehicle"] == 0.0
    assert row["mean_control_delay_sec_per_vehicle"] == pytest.approx(30.0)
    assert row["level_of_service"] == "C"


def test_node_mean_delay_credits_only_vehicles_that_crossed():
    main.reset_run_metrics()
    crossed = Vehicle(NODE_A - 200, H_Y - 0.5 * LANE, "EB")
    waiting = Vehicle(NODE_A - 300, H_Y - 0.5 * LANE, "EB")
    crossed.speed = waiting.speed = 0.0
    for _ in range(120):
        main.accumulate_frame_metrics([crossed, waiting])
    crossed.passed_nodes = {NODE_A}
    main._record_node_crossings(crossed)
    _seed_regime()
    row = main.build_experiment_summary_row(60)
    assert row["node_A_throughput_veh"] == 1
    assert row["node_A_mean_delay_sec"] == pytest.approx(2.0)  # not 4.0


def test_delay_zero_when_nothing_served():
    exporter = TelemetryExporter(export_interval_frames=1)
    controller = SignalController({"green_time": 20})
    payload = exporter.build_payload(controller, [], 60, throughput_state={})
    delay = payload["delay"]
    assert delay["bus_passenger_delay_sec"] == 0.0
    assert delay["mean_bus_passenger_delay_sec"] is None
    assert delay["mean_car_passenger_delay_sec"] is None
    assert delay["total_person_hours_delay"] == 0.0


# --------------------------------------------------------------------------
# Part C -- the cross-run summary CSV
# --------------------------------------------------------------------------

def _seed_regime():
    control_panel.global_config["test_model"] = "rule-based"
    control_panel.global_config["test_seed"] = 42
    control_panel.global_config["test_duration_sim_seconds"] = 900
    control_panel.global_config["_active_vehicle_speed_scale"] = 0.5
    control_panel.global_config["measured_saturation_flow"] = 1291.0


def test_checkpoint_cumulative():
    """The 10-minute checkpoint's row carries the FULL running total from
    t=0, not the interval since the 5-minute mark: two checkpoints taken
    after successive additions to the same never-reset counters must show
    strictly additive totals."""
    _seed_regime()
    main.network_throughput["passengers_served_total"] = 100
    main.network_throughput["passengers_served_bus"] = 45
    main.network_throughput["passengers_served_car"] = 55
    main.network_throughput["bus_passenger_delay_frames"] = 45 * 60
    main.network_throughput["car_passenger_delay_frames"] = 55 * 30

    row_5min = main.build_experiment_summary_row(300)
    assert row_5min["checkpoint_min"] == pytest.approx(5.0)
    assert row_5min["passengers_served_total"] == 100

    # More traffic served between the two checkpoints; counters keep adding.
    main.network_throughput["passengers_served_total"] += 150
    main.network_throughput["passengers_served_bus"] += 45
    main.network_throughput["passengers_served_car"] += 105
    main.network_throughput["bus_passenger_delay_frames"] += 45 * 90

    row_10min = main.build_experiment_summary_row(600)
    assert row_10min["checkpoint_min"] == pytest.approx(10.0)
    # Cumulative, i.e. includes everything the 5-minute row already had.
    assert row_10min["passengers_served_total"] == 250
    assert row_10min["passengers_served_bus"] == 90
    assert row_10min["passengers_served_car"] == 160
    assert row_10min["bus_person_hours_delay"] > row_5min["bus_person_hours_delay"]


def test_summary_row_appended_per_checkpoint(monkeypatch):
    """A 15-minute run appends exactly 3 rows (cp5, cp10, cp15), each with
    the correct checkpoint_min."""
    _seed_regime()
    control_panel.global_config["test_duration_sim_seconds"] = 900
    control_panel.global_config["test_running"] = True
    control_panel.global_config["is_running"] = True
    monkeypatch.setattr(main, "export_test_workbook", lambda *a, **k: None)
    main.pending_checkpoints_sec.extend(main.compute_checkpoint_marks(900))

    main.fire_due_checkpoints(5 * 60 * 60)
    main.fire_due_checkpoints(10 * 60 * 60)
    main.finish_timed_test(15 * 60 * 60)

    rows = read_summary_rows()
    assert len(rows) == 3
    assert [float(row["checkpoint_min"]) for row in rows] == [5.0, 10.0, 15.0]
    assert all(row["model"] == "rule-based" for row in rows)
    assert all(row["seed"] == "42" for row in rows)


def test_summary_not_cleared_on_reset(monkeypatch):
    """RESET clears the per-run session logs, but never the accumulating
    experiment summary -- only manual deletion does."""
    _seed_regime()
    assert main.append_experiment_summary_row(300) is True
    assert main.experiment_summary_path().exists()
    rows_before = read_summary_rows()
    assert len(rows_before) == 1

    main.TELEMETRY_LOG_PATH.write_text("x\n", encoding="utf-8")
    main.AGENT_TURN_LOG_PATH.write_text("x\n", encoding="utf-8")
    main.BUS_EVENTS_LOG_PATH.write_text("x\n", encoding="utf-8")

    class FakeSignals:
        def reset_all_state(self):
            pass

    monkeypatch.setattr(main, "calibrate_and_apply_webster", lambda signals: None)
    monkeypatch.setattr(main, "reset_traffic_generation", lambda: None)
    control_panel.global_config["test_running"] = False

    main.perform_full_reset([], FakeSignals())

    assert not main.TELEMETRY_LOG_PATH.exists()
    assert not main.AGENT_TURN_LOG_PATH.exists()
    assert not main.BUS_EVENTS_LOG_PATH.exists()
    # The master dataset survives, untouched, across the reset.
    assert main.experiment_summary_path().exists()
    assert read_summary_rows() == rows_before


def test_summary_has_all_columns():
    _seed_regime()
    row = main.build_experiment_summary_row(300)
    assert set(row) == set(main.EXPERIMENT_SUMMARY_HEADERS)
    # Spot-check one column from each named group in the spec.
    for column in (
        "timestamp", "model", "seed", "checkpoint_min", "test_duration_min",
        "vehicle_speed_scale", "saturation_flow_veh_hr", "cycle_sec", "vc_ratio",
        "demand_EB_veh_min", "demand_symmetric",
        "passengers_served_total", "passengers_served_bus", "pax_per_min",
        "total_person_hours_delay", "bus_person_hours_delay",
        "mean_bus_passenger_delay_sec", "mean_car_passenger_delay_sec",
        "mean_vehicles_in_network", "max_vehicles_in_network",
        "buses_tsp_treated", "buses_untreated",
        "mean_treated_bus_wait_sec", "mean_untreated_bus_wait_sec",
        "mean_decision_latency_ms", "guard_reject_rate", "total_decisions",
        "decision_lag_sec_used", "level_of_service",
    ):
        assert column in row, column


def test_summary_mechanism_columns_from_bus_events(monkeypatch):
    """buses_tsp_treated/untreated and their mean waits are aggregated from
    the Bus Events log -- the causal treated-vs-untreated evidence."""
    _seed_regime()
    events = [
        {"bus_id": "T1", "nodes": [{"tsp_treated": True}], "total_wait_frames": 600},
        {"bus_id": "T2", "nodes": [{"tsp_treated": True}], "total_wait_frames": 300},
        {"bus_id": "U1", "nodes": [{"tsp_treated": False}], "total_wait_frames": 1200},
    ]
    with main.BUS_EVENTS_LOG_PATH.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    # The exporter refuses a row whose bus count disagrees with its own Bus
    # Events log, so the throughput counter must see the same three buses.
    main.network_throughput["buses_served"] = 3
    main.network_throughput["vehicles_served_total"] = 3

    row = main.build_experiment_summary_row(300)

    assert row["buses_tsp_treated"] == 2
    assert row["buses_untreated"] == 1
    assert row["mean_treated_bus_wait_sec"] == pytest.approx((10.0 + 5.0) / 2)
    assert row["mean_untreated_bus_wait_sec"] == pytest.approx(20.0)


def test_summary_llm_meta_from_decisions(monkeypatch):
    _seed_regime()
    decisions = [
        {"turn": 1, "status": "OK", "latency_ms": 100.0},
        {"turn": 2, "status": "OK", "latency_ms": 300.0},
        {"turn": 3, "status": "HELD_ALL_OFF", "latency_ms": None},
    ]
    with main.AGENT_TURN_LOG_PATH.open("w", encoding="utf-8") as handle:
        for decision in decisions:
            handle.write(json.dumps(decision) + "\n")

    row = main.build_experiment_summary_row(300)

    assert row["total_decisions"] == 3
    assert row["guard_reject_rate"] == pytest.approx(1 / 3, abs=0.01)
    assert row["mean_decision_latency_ms"] == pytest.approx(200.0)
    assert row["decision_lag_sec_used"] == pytest.approx(0.2)


def test_summary_demand_symmetry_flag():
    _seed_regime()
    for key in ("EB", "WB", "A_NB", "A_SB", "B_NB", "B_SB"):
        control_panel.approach_configs[key]["active"] = True
        control_panel.approach_configs[key]["rate"] = 12

    row = main.build_experiment_summary_row(300)
    assert row["demand_symmetric"] is True

    control_panel.approach_configs["WB"]["rate"] = 20
    row = main.build_experiment_summary_row(300)
    assert row["demand_symmetric"] is False


# --------------------------------------------------------------------------
# End-to-end: 15-minute run report
# --------------------------------------------------------------------------

def test_fifteen_minute_run_produces_three_checkpoints_and_rows(monkeypatch, tmp_path):
    """One 15-minute test: cp5/cp10/cp15 workbooks are attempted and 3
    summary rows land, matching the report acceptance criteria."""
    pytest.importorskip("openpyxl")
    _seed_regime()
    control_panel.global_config["test_running"] = True
    control_panel.global_config["is_running"] = True
    control_panel.global_config["test_duration_sim_seconds"] = 900

    workbook_calls = []
    real_export = main.export_test_workbook

    def spy_export(model, duration, seed, destination=None, checkpoint_sim_seconds=None):
        result = real_export(
            model, duration, seed,
            destination=tmp_path / f"cp{int(checkpoint_sim_seconds)}.xlsx",
            checkpoint_sim_seconds=checkpoint_sim_seconds,
        )
        workbook_calls.append(checkpoint_sim_seconds)
        return result

    monkeypatch.setattr(main, "export_test_workbook", spy_export)
    main.pending_checkpoints_sec.extend(main.compute_checkpoint_marks(900))

    main.fire_due_checkpoints(5 * 60 * 60)
    main.fire_due_checkpoints(10 * 60 * 60)
    main.finish_timed_test(900 * 60)

    assert workbook_calls == [300, 600, 900]
    for mark in (300, 600, 900):
        assert (tmp_path / f"cp{mark}.xlsx").exists()
    rows = read_summary_rows()
    assert len(rows) == 3
    assert [float(row["checkpoint_min"]) for row in rows] == [5.0, 10.0, 15.0]


# --------------------------------------------------------------------------
# Part D -- steady-state DVs (warm-up discard)
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_warmup_snapshot():
    main.network_throughput_at_warmup.clear()
    yield
    main.network_throughput_at_warmup.clear()


def _write_telemetry_rows(rows):
    with main.TELEMETRY_LOG_PATH.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_warmup_snapshot_taken_once_at_the_discard_frame():
    control_panel.global_config["warmup_discard_frames"] = 7200
    main.network_throughput["passengers_served_total"] = 40
    main.snapshot_warmup_baseline(7199)
    assert not main.network_throughput_at_warmup
    main.snapshot_warmup_baseline(7200)
    assert main.network_throughput_at_warmup["passengers_served_total"] == 40
    assert main.network_throughput_at_warmup["frame"] == 7200
    main.network_throughput["passengers_served_total"] = 90
    main.snapshot_warmup_baseline(7201)  # never re-taken within a run
    assert main.network_throughput_at_warmup["passengers_served_total"] == 40
    main.perform_full_reset([], SignalController({"green_time": 100}))
    assert not main.network_throughput_at_warmup


def test_steady_columns_use_only_the_post_warmup_window():
    """Cumulative columns are untouched; *_steady divide the post-warm-up
    delta by the steady window, so fill traffic cannot drag the DV."""
    _seed_regime()
    control_panel.global_config["warmup_discard_frames"] = 7200  # 120 s
    main.network_throughput.update({
        "passengers_served_total": 100, "passengers_served_bus": 45,
        "passengers_served_car": 55,
        "bus_passenger_delay_frames": 45 * 60, "car_passenger_delay_frames": 55 * 60,
        "completed_bus_passenger_delay_frames": 45 * 60,
        "completed_car_passenger_delay_frames": 55 * 60,
    })
    main.snapshot_warmup_baseline(7200)
    main.network_throughput.update({
        "passengers_served_total": 580, "passengers_served_bus": 225,
        "passengers_served_car": 355,
        "bus_passenger_delay_frames": 45 * 60 + 180 * 60 + 999,  # + still-queued exposure
        "car_passenger_delay_frames": 55 * 60 + 300 * 60 + 999,
        "completed_bus_passenger_delay_frames": 45 * 60 + 180 * 60,
        "completed_car_passenger_delay_frames": 55 * 60 + 300 * 60,
    })

    row = main.build_experiment_summary_row(600)  # T = 10 min

    assert row["pax_per_min"] == pytest.approx(58.0)          # 580 / 10, unchanged
    assert row["warmup_discard_sec"] == 120.0
    assert row["steady_window_sec"] == 480.0
    assert row["pax_per_min_steady"] == pytest.approx(60.0)   # 480 / 8
    assert row["bus_person_hours_delay_steady"] == pytest.approx((180 * 60 + 999) / 60 / 3600, abs=1e-4)
    assert row["car_person_hours_delay_steady"] == pytest.approx((300 * 60 + 999) / 60 / 3600, abs=1e-4)
    assert row["total_person_hours_delay_steady"] == pytest.approx((480 * 60 + 1998) / 60 / 3600, abs=1e-4)
    assert row["mean_bus_passenger_delay_sec_steady"] == pytest.approx(180 / 180)
    assert row["mean_car_passenger_delay_sec_steady"] == pytest.approx(300 / 300)
    for column in main.EXPERIMENT_SUMMARY_HEADERS:
        assert column in row


def test_steady_columns_are_none_before_warmup_ends():
    _seed_regime()
    control_panel.global_config["warmup_discard_frames"] = 7200
    main.network_throughput["passengers_served_total"] = 50
    main.network_throughput["passengers_served_car"] = 50
    row = main.build_experiment_summary_row(60)
    assert row["pax_per_min_steady"] is None
    assert row["steady_window_sec"] == 0.0
    assert row["pax_per_min"] == pytest.approx(50.0)


def test_converged_flag_compares_cumulative_rate_at_0_8T():
    _seed_regime()
    main.network_throughput["passengers_served_total"] = 600  # 60/min at T=600
    main.network_throughput["passengers_served_car"] = 600
    _write_telemetry_rows([
        {"sim_time_s": 300, "pax_per_min_cumulative": 40.0},
        {"sim_time_s": 480, "pax_per_min_cumulative": 58.0},  # within 5% of 60
        {"sim_time_s": 540, "pax_per_min_cumulative": 30.0},  # after 0.8T: ignored
    ])
    assert main.build_experiment_summary_row(600)["converged"] is True
    _write_telemetry_rows([{"sim_time_s": 480, "pax_per_min_cumulative": 50.0}])
    assert main.build_experiment_summary_row(600)["converged"] is False
    _write_telemetry_rows([])
    assert main.build_experiment_summary_row(600)["converged"] is None


def test_converged_requires_flat_network_load():
    """A cumulative rate is stable by construction; a run whose accumulation
    is still climbing at the horizon (Run 8: 150 -> 230 vehicles between
    0.8T and T with the rate within 5%) is not converged."""
    _seed_regime()
    main.network_throughput["passengers_served_total"] = 600
    main.network_throughput["passengers_served_car"] = 600
    _write_telemetry_rows([
        {"sim_time_s": 480, "pax_per_min_cumulative": 58.0, "vehicles_in_network": 150},
        {"sim_time_s": 600, "pax_per_min_cumulative": 60.0, "vehicles_in_network": 230},
    ])
    assert main.build_experiment_summary_row(600)["converged"] is False
    _write_telemetry_rows([
        {"sim_time_s": 480, "pax_per_min_cumulative": 58.0, "vehicles_in_network": 150},
        {"sim_time_s": 600, "pax_per_min_cumulative": 60.0, "vehicles_in_network": 158},
    ])
    assert main.build_experiment_summary_row(600)["converged"] is True


def test_old_summary_csv_is_rotated_not_misaligned():
    """A dataset written with an older header is set aside intact and a
    fresh file starts with the current header."""
    _seed_regime()
    old_header = "timestamp,model,seed\n"
    live = main.experiment_summary_path()
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(old_header + "1,rule-based,1\n", encoding="utf-8")

    assert main.append_experiment_summary_row(300) is True

    rows = read_summary_rows()
    assert len(rows) == 1 and "pax_per_min_steady" in rows[0]
    rotated = [
        path for path in live.parent.glob("experiment_summary_*_schema_*.csv")
    ]
    assert len(rotated) == 1
    assert rotated[0].read_text(encoding="utf-8") == old_header + "1,rule-based,1\n"


def test_default_benchmark_is_an_hour_with_two_minute_warmup():
    assert control_panel.TEST_DURATIONS["1 hr"] == 3600
    assert main.WARMUP_DISCARD_FRAMES == 7200
    fresh = {
        key: value for key, value in control_panel.global_config.items()
        if key in ("warmup_discard_frames",)
    }
    assert fresh == {"warmup_discard_frames": 7200}
    assert any(
        param == "warmup_discard_frames" and value == 7200
        for _section, param, value in main.control_panel_input_rows()
    )


def test_every_checkpoint_workbook_carries_the_runs_summary_rows_so_far(monkeypatch):
    """A 10-min run's 5-min workbook holds the 5-min row; its final workbook
    holds the 5-min AND 10-min rows, identical to what the CSV received."""
    from openpyxl import load_workbook
    _seed_regime()
    main.network_throughput["passengers_served_total"] = 600
    main.network_throughput["passengers_served_car"] = 600
    main._run_summary_rows.clear()
    control_panel.global_config.update({
        "test_running": True, "test_model": "None", "test_seed": 7,
        "test_duration_sim_seconds": 600,
    })
    main.pending_checkpoints_sec[:] = main.compute_checkpoint_marks(600)

    main.fire_due_checkpoints(300 * 60)
    main.finish_timed_test(600 * 60)

    reports = sorted(main.EXCEL_EXPORT_DIR.glob("*_gridlock.xlsx"))
    workbooks = sorted(set(main.EXCEL_EXPORT_DIR.glob("*.xlsx")) - set(reports))
    assert len(workbooks) == 2 and len(reports) == 2  # each run workbook gets its gridlock report
    sheets = []
    for path in workbooks:
        rows = list(load_workbook(path, read_only=True)["Experiment Summary"].iter_rows(values_only=True))
        assert list(rows[0]) == list(main.EXPERIMENT_SUMMARY_HEADERS)
        sheets.append([dict(zip(rows[0], r)) for r in rows[1:]])
    by_count = sorted(sheets, key=len)
    assert [r["checkpoint_min"] for r in by_count[0]] == [5.0]
    assert [r["checkpoint_min"] for r in by_count[1]] == [5.0, 10.0]
    csv_rows = read_summary_rows()
    assert [float(r["checkpoint_min"]) for r in csv_rows] == [5.0, 10.0]
    assert csv_rows[0]["workbook_filename"] == by_count[0][0]["workbook_filename"]
    assert csv_rows[0]["workbook_filename"] in {p.name for p in workbooks}


def test_travel_delay_counts_crawl_that_stopped_delay_misses():
    """Travel-time delay is time below the vehicle's own free-flow speed: a
    vehicle at max_speed adds 0, at half speed adds pax/2 per frame, and a
    stopped one adds pax per frame (equal to its stopped-delay contribution).
    Stopped delay sees only the third."""
    bus = make_bus_for_leg("R1_EB_A_NB", NODE_A, "TRAVEL_BUS")
    bus.speed = 0.0
    car_free = Vehicle(0, H_Y - 0.5 * LANE, "EB")
    car_free.speed = car_free.max_speed
    car_crawl = Vehicle(100, H_Y - 0.5 * LANE, "EB")
    car_crawl.speed = car_crawl.max_speed / 2.0

    for _ in range(60):
        main.accumulate_frame_metrics([bus, car_free, car_crawl])

    assert main.network_throughput["bus_passenger_travel_delay_frames"] == pytest.approx(45 * 60)
    assert main.network_throughput["car_passenger_travel_delay_frames"] == pytest.approx(4 * 30)
    assert main.network_throughput["car_passenger_delay_frames"] == 0  # crawl is invisible here


def test_summary_row_has_travel_delay_but_no_per_run_net_saved(monkeypatch):
    """No per-run column may claim 'net person-hours saved': the baseline is
    another run, so that number only exists as a paired difference."""
    _seed_regime()
    main.network_throughput.update({
        "passengers_served_total": 400, "passengers_served_car": 400,
        "bus_passenger_travel_delay_frames": 45 * 3600.0,
        "car_passenger_travel_delay_frames": 4 * 3600.0,
    })
    row = main.build_experiment_summary_row(300)
    assert "net_person_hours_saved" not in main.EXPERIMENT_SUMMARY_HEADERS
    assert "tsp_window_cross_street_person_hours" in main.EXPERIMENT_SUMMARY_HEADERS
    assert row["bus_person_hours_travel_delay"] == pytest.approx(45 / 60, abs=1e-4)
    assert row["car_person_hours_travel_delay"] == pytest.approx(4 / 60, abs=1e-4)
    assert row["total_person_hours_travel_delay"] == pytest.approx(
        row["bus_person_hours_travel_delay"] + row["car_person_hours_travel_delay"]
    )


def test_pair_against_baseline_is_baseline_minus_arm_on_the_same_seed():
    def row(model, seed, travel, bus, car, stopped, converged="True", uuid_=""):
        return {
            "campaign_id": "C", "model": model, "seed": str(seed), "run_uuid": uuid_,
            "total_person_hours_travel_delay_steady": str(travel),
            "bus_person_hours_travel_delay_steady": str(bus),
            "car_person_hours_travel_delay_steady": str(car),
            "total_person_hours_delay_steady": str(stopped),
            "converged": converged,
        }
    rows = [
        row("None", 1, 10.0, 6.0, 4.0, 7.0, uuid_="b1"),
        row("rule", 1, 8.5, 4.0, 4.5, 6.0, uuid_="a1"),
        row("None", 2, 12.0, 7.0, 5.0, 9.0, converged="False", uuid_="b2"),
        row("rule", 2, 12.5, 7.5, 5.0, 9.5, uuid_="a2"),
        row("rule", 3, 5.0, 3.0, 2.0, 4.0, uuid_="a3"),  # no baseline for seed 3
        dict(row("None", 4, 1.0, 0.5, 0.5, 1.0, uuid_="b4"), demand_draw_hash="aaaa"),
        dict(row("rule", 4, 1.0, 0.5, 0.5, 1.0, uuid_="a4"), demand_draw_hash="bbbb"),
    ]
    pairs, unpaired, mismatched = main.pair_against_baseline(rows)
    assert unpaired == ["3"]
    assert mismatched == ["rule/4: demand_draw_hash"]  # a different demand realisation never pairs
    assert [(p["seed"], p["model"]) for p in pairs] == [("1", "rule"), ("2", "rule")]
    first, second = pairs
    assert first["net_person_hours_saved"] == pytest.approx(1.5)
    assert first["bus_person_hours_saved"] == pytest.approx(2.0)
    assert first["car_person_hours_saved"] == pytest.approx(-0.5)  # cars paid for it
    assert first["net_person_hours_saved_stopped"] == pytest.approx(1.0)
    assert first["baseline_run_uuid"] == "b1" and first["arm_run_uuid"] == "a1"
    assert first["converged_both"] is True
    assert second["net_person_hours_saved"] == pytest.approx(-0.5)
    assert second["converged_both"] is False


def test_paired_dv_csv_written_with_declared_header(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "EXPERIMENT_SUMMARY_PATH", tmp_path / "experiment_summary.csv")
    pairs, _, _ = main.pair_against_baseline([
        {"campaign_id": "C", "model": "None", "seed": "1", "run_uuid": "b",
         "total_person_hours_travel_delay_steady": "3", "converged": "True"},
        {"campaign_id": "C", "model": "rule", "seed": "1", "run_uuid": "a",
         "total_person_hours_travel_delay_steady": "2", "converged": "True"},
    ])
    path = main.write_paired_dv_csv("C", pairs)
    with path.open(newline="") as handle:
        written = list(csv.DictReader(handle))
    assert path.name == "paired_dv_C.csv"
    assert list(written[0].keys()) == list(main.PAIRED_DV_HEADERS)
    assert float(written[0]["net_person_hours_saved"]) == 1.0
    assert written[0]["bus_person_hours_saved"] == ""  # missing split stays blank, not 0


def test_regime_hash_ignores_treatment_and_is_frozen_at_reset(monkeypatch):
    """Audit item 6: toggling a route's TSP/DBL flag must not move the
    regime hash (that is what the arm does, not the regime it runs in), and
    every checkpoint of one run reports the hash frozen at reset even if
    the live panel changes underneath it."""
    _seed_regime()
    route = control_panel.bus_routes_config["R1_EB_A_NB"]
    before = main._config_regime_hash(control_panel.global_config)
    monkeypatch.setitem(route, "tsp_enabled", not route["tsp_enabled"])
    monkeypatch.setitem(route, "dbl_enabled", not route["dbl_enabled"])
    assert main._config_regime_hash(control_panel.global_config) == before
    monkeypatch.setitem(route, "headway_sec", route["headway_sec"] + 1)  # regime
    assert main._config_regime_hash(control_panel.global_config) != before

    monkeypatch.setitem(control_panel.global_config, "_regime_hash", "frozen-at-reset")
    monkeypatch.setitem(control_panel.global_config, "vehicle_speed_scale", 0.25)
    row = main.build_experiment_summary_row(300)
    assert row["config_hash"] == "frozen-at-reset"


def test_pairing_requires_every_regime_column_to_match():
    def row(model, seed, **fields):
        base = {"campaign_id": "C", "model": model, "seed": str(seed), "run_uuid": model,
                "total_person_hours_travel_delay_steady": "1", "converged": "True",
                "config_hash": "h", "git_sha": "s", "demand_draw_hash": "d",
                "checkpoint_min": "60.0", "test_duration_min": "60.0"}
        base.update(fields)
        return base
    rows = [row("None", 1), row("ok", 1),
            row("None", 2), row("cfg", 2, config_hash="other"),
            row("None", 3), row("code", 3, git_sha="other"),
            row("None", 4), row("window", 4, checkpoint_min="30.0", test_duration_min="30.0"),
            row("None", 5, campaign_id="X"), row("campaign", 5)]
    pairs, unpaired, mismatched = main.pair_against_baseline(rows)
    assert [p["model"] for p in pairs] == ["ok"]
    assert mismatched == [
        "cfg/2: config_hash", "code/3: git_sha",
        "window/4: checkpoint_min, test_duration_min",
    ]
    assert unpaired == ["5"]  # a baseline from another campaign is no baseline


def test_person_hours_totals_equal_sum_of_rounded_parts():
    """The export assertion compares the rounded total against rounded
    parts; rounding the raw sum drifts 1e-4 off for ~13% of inputs."""
    import random
    from src.core.main import _run_export_assertions

    rng = random.Random(1)
    for _ in range(2000):
        bus_h, car_h = rng.uniform(10, 16), rng.uniform(25, 32)
        row = {
            "checkpoint_min": 5, "warmup_discard_sec": 120, "steady_window_sec": 180,
            "passengers_served_total": 0, "passengers_served_bus": 0,
            "passengers_served_car": 0, "buses_served": 0, "buses_tsp_treated": 0,
            "buses_untreated": 0, "actual_decision_interval_sec_median": None,
            "bus_person_hours_delay": round(bus_h, 4),
            "car_person_hours_delay": round(car_h, 4),
            "total_person_hours_delay": round(round(bus_h, 4) + round(car_h, 4), 4),
            "bus_person_hours_travel_delay": round(bus_h, 4),
            "car_person_hours_travel_delay": round(car_h, 4),
            "total_person_hours_travel_delay": round(round(bus_h, 4) + round(car_h, 4), 4),
        }
        _run_export_assertions(row, {})


def test_summary_csv_is_dated_by_campaign_start(monkeypatch):
    """Rows go to experiment_summary_<YYYYMMDD>.csv: the campaign's start
    day for a batch (so one crossing midnight stays in one file), the
    run's start day otherwise."""
    monkeypatch.setitem(control_panel.global_config, "_run_started_wall", 0.0)
    monkeypatch.setitem(control_panel.global_config, "batch_runtime", {})
    import time
    day_zero = time.strftime("%Y%m%d", time.localtime(0.0))
    assert main.experiment_summary_path().name == f"experiment_summary_{day_zero}.csv"
    monkeypatch.setitem(
        control_panel.global_config, "batch_runtime",
        {"campaign_id": "c1", "campaign_stamp": "20260921"},
    )
    assert main.experiment_summary_path().name == "experiment_summary_20260921.csv"
