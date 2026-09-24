"""The calibration record (main.calibration_rows) and the campaign report built
from it (src/telemetry/calibration_report.py)."""
import csv

from openpyxl import Workbook

import src.core.main as main
import src.ui.control_panel as control_panel
from src.core.signal_controller import SignalController
from src.telemetry import calibration_report

MEASURED = {
    "saturation_flow": 1500.0, "startup_lost_sec": 1.2, "startup_lost_source": "measured",
    "excluded_headways": 1, "queue_sample": 30, "vehicles_timed": 30, "headways_used": 24,
    "median_headway_sec": 2.3, "mean_headway_sec": 2.4,
}


def _calibrate(monkeypatch):
    monkeypatch.setattr(main, "calibrate_saturation_flow", lambda *a, **k: dict(MEASURED))
    main.calibrate_and_apply_webster(SignalController(control_panel.global_config))
    return {(r[0], r[1]): r for r in main.calibration_rows()}


def test_the_record_reports_what_the_run_was_timed_with(monkeypatch):
    rows = _calibrate(monkeypatch)
    timing = control_panel.global_config["_signal_timing"]
    assert rows[("Saturation flow", "Saturation flow S")][2] == 1500.0
    assert rows[("Saturation flow", "Headways used")][2] == 24
    assert rows[("Saturation flow", "Start-up lost time l1")][2] == 1.2
    assert rows[("Change intervals", "Yellow Y")][2] == timing["yellow_sec"]
    assert rows[("Change intervals", "All-red AR")][2] == timing["all_red_sec"]
    splits = control_panel.global_config["webster_splits"]
    for index, (node_x, split) in enumerate(sorted(splits.items())):
        section = f"Webster: node {'AB'[index]} (x={node_x})"
        assert rows[(section, "Cycle used")][2] == split["cycle_time_sec"]
        assert rows[(section, "Cycle source")][2] == split["cycle_source"]
    assert all(len(row) == len(main.CALIBRATION_COLUMNS) for row in rows.values())


def _summary_row(**overrides):
    row = {header: "" for header in main.EXPERIMENT_SUMMARY_HEADERS}
    row.update({
        "campaign_id": "c0ffee00-camp", "model": "None", "seed": "234", "config_hash": "abc123def456",
        "checkpoint_min": "15", "test_duration_min": "15", "git_sha": "deadbeef", "workbook_filename": "run.xlsx",
        "network_to_calibrated_s_ratio": "0.97", "network_saturation_headway_samples": "120",
        "latent_demand_share_at_end": "0.01", "geh_entry_share_below_5": "1.0", "converged": "True",
    })
    row.update(overrides)
    return row


def test_the_campaign_report_shows_the_calibration_and_flags_runs(monkeypatch, tmp_path):
    _calibrate(monkeypatch)
    workbook = Workbook()
    main.write_calibration_sheet(workbook.active)
    workbook.active.title = "Calibration"
    workbook.save(tmp_path / "run.xlsx")
    rows = [_summary_row(), _summary_row(model="rule-based", latent_demand_share_at_end="0.12"),
            _summary_row(campaign_id="another-campaign")]
    with (tmp_path / "experiment_summary_20260924.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=main.EXPERIMENT_SUMMARY_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    path = calibration_report.write_campaign_report("c0ffee00-camp", tmp_path)
    page = path.read_text(encoding="utf-8")
    assert path.name == "calibration_report_c0ffee00-camp.html"
    assert "Saturation flow S" in page and "1500" in page
    assert "1 of 2 runs pass every judged check" in page
    assert "class='num flag'>12.0%" in page
    assert "another-campaign" not in page
