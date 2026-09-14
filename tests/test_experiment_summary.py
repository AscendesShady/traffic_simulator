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

import canvas_gemini as canvas
import control_panel
import main
from signal_controller import SignalController
from telemetry_exporter import TelemetryExporter
from vehicle import Bus, Vehicle
from canvas_gemini import H_Y, LANE
from tests.helpers import make_bus_for_leg


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
    path = main.EXPERIMENT_SUMMARY_PATH
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
    monkeypatch.setattr(main, "append_experiment_summary_row", lambda mark: None)

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
    monkeypatch.setattr(main, "append_experiment_summary_row", lambda mark: None)

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
    monkeypatch.setattr(main, "append_experiment_summary_row", lambda mark: None)

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


def test_checkpoint_filenames_unique():
    names = {
        mark: main.build_test_export_filename(
            "nemotron", 1800, 42, timestamp="20260913_120000",
            checkpoint_sim_seconds=mark,
        )
        for mark in (300, 600, 900, 1800)
    }
    assert len(set(names.values())) == 4
    assert names[300] == "nemotron_5min_42seed_13092026_120000.xlsx"
    assert names[600] == "nemotron_10min_42seed_13092026_120000.xlsx"
    assert names[900] == "nemotron_15min_42seed_13092026_120000.xlsx"
    assert names[1800] == "nemotron_30min_42seed_13092026_120000.xlsx"

    # An unqualified call (the final-mark path) uses its own duration.
    default_tagged = main.build_test_export_filename(
        "nemotron", 1800, 42, timestamp="20260913_120000"
    )
    assert default_tagged == names[1800]


# --------------------------------------------------------------------------
# Part B -- passenger-weighted delay, split bus vs car
# --------------------------------------------------------------------------

def test_accumulate_frame_metrics_splits_by_mode():
    """A queued bus contributes its full 45 passengers per frame to the bus
    accumulator; a queued car contributes its 4 to the car accumulator."""
    bus = make_bus_for_leg("R1_EB_A_NB", 300, "DELAY_BUS")
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
    assert main.EXPERIMENT_SUMMARY_PATH.exists()
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
    assert main.EXPERIMENT_SUMMARY_PATH.exists()
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
