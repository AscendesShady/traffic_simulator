"""Phase one of a two-phase campaign: non-LLM arms as parallel headless runs."""
import csv

import pytest

from src.core import main
from src.experiments import parallel_campaign as pc

# Columns that name a run rather than measure it.
IDENTITY_COLUMNS = {
    "run_uuid", "campaign_id", "timestamp", "run_started_utc", "run_ended_utc",
    "wall_clock_sec", "workbook_filename",
}


def _rows(results_dir):
    rows = []
    for path in sorted(results_dir.glob("experiment_summary_*.csv")):
        with path.open(encoding="utf-8", newline="") as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def test_llm_arms_are_refused_with_the_way_to_run_them():
    assert pc.resolve_arm("baseline") == "None"
    assert pc.resolve_arm("rule-based") == "rule-based"
    with pytest.raises(ValueError, match="windowed app"):
        pc.resolve_arm("llama3.1:8b")


def test_a_windowed_batch_can_join_an_existing_campaign(monkeypatch):
    path = main.EXPERIMENT_SUMMARY_PATH.with_name(f"{main.EXPERIMENT_SUMMARY_PATH.stem}_20260101.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("campaign_id,seed\nCAMPAIGN-1,234\n", encoding="utf-8")
    assert main.campaign_stamp_of("CAMPAIGN-1") == "20260101"
    assert main.campaign_stamp_of("nope") is None
    monkeypatch.setenv(main.JOIN_CAMPAIGN_ENV, "CAMPAIGN-1")
    assert main.new_or_joined_campaign({}) == ("CAMPAIGN-1", "20260101")
    monkeypatch.delenv(main.JOIN_CAMPAIGN_ENV)
    fresh_id, _ = main.new_or_joined_campaign({})
    assert fresh_id != "CAMPAIGN-1"


def test_parallel_runs_fold_into_one_paired_campaign_identical_to_sequential(tmp_path):
    """Two workers side by side give exactly the rows one worker gives alone,
    in one campaign, paired against its baseline."""
    measured = {}
    for workers in (2, 1):
        results = tmp_path / f"results_{workers}"
        results.mkdir()
        main.RESULTS_DIR = main.EXCEL_EXPORT_DIR = results
        main.EXPERIMENT_SUMMARY_PATH = results / "experiment_summary.csv"
        campaign_id, outcomes = pc.run_campaign(
            ["baseline", "rule-based"], [5], 1, workers=workers, warmup_sec=10,
            workroot=tmp_path / f"work_{workers}", echo=lambda *_: None,
        )
        assert [o["status"] for o in outcomes] == ["COMPLETED", "COMPLETED"], outcomes
        rows = _rows(results)
        assert {row["campaign_id"] for row in rows} == {campaign_id}
        assert len({row["config_hash"] for row in rows}) == 1
        assert (results / f"paired_dv_{campaign_id}.csv").exists()
        # The calibration report reads the regime off the copied workbooks.
        report = (results / f"calibration_report_{campaign_id}.html").read_text(encoding="utf-8")
        assert "Saturation flow S" in report and "No Calibration sheet" not in report
        by_arm = {row["model"]: row for row in rows}
        assert int(by_arm["None"]["decisions_issued"] or 0) == 0
        assert int(by_arm["rule-based"]["decisions_issued"]) > 0
        # Issued is not accepted: the rule arm read another run's stale
        # telemetry and every turn was held all-off (guard_reject_rate 1.0).
        assert float(by_arm["rule-based"]["guard_reject_rate"]) == 0.0
        measured[workers] = {
            arm: {k: v for k, v in row.items() if k not in IDENTITY_COLUMNS}
            for arm, row in by_arm.items()
        }
    assert measured[2] == measured[1]


def test_the_worker_and_the_windowed_loop_share_one_per_run_path():
    """Equivalence with the windowed batch is by construction -- the same
    step, telemetry recording, checkpoints and completion -- so keep both
    callers on the shared functions rather than a second copy."""
    import inspect
    tk_loop = inspect.getsource(main.main)
    worker = inspect.getsource(pc.run_worker)
    assert "record_telemetry(telemetry, signals, vehicles, master_frame_count)" in tk_loop
    for call in ("main.step_simulation(", "main.record_telemetry(", "main.fire_due_checkpoints(",
                 "main.timed_test_is_complete(", "main.finish_timed_test(", "control_panel.request_start_test()",
                 "main.perform_full_reset("):
        assert call in worker, call


def test_a_headless_run_summarises_its_own_controller():
    """The summary row's TSP/DBL columns come from the run's controller. Only
    the windowed main() used to register it, so every headless and parallel
    campaign row reported zero requests while its bus events showed them."""
    from src.experiments import headless_run
    for route in main.control_panel.bus_routes_config.values():
        route["headway_sec"] = 30   # buses inside a short run
    captured = {}
    headless_run.run(234, 5400, tsp=True, dbl=True, on_finish=lambda v, s: captured.update(signals=s))
    metrics = captured["signals"].get_experiment_metrics()
    assert metrics["tsp_requests_raised"] + metrics["dbl_requests_raised"] > 0
    assert main._controller_experiment_metrics() == metrics


def test_a_rule_turn_carries_no_latency(monkeypatch):
    """A rule decision is released on its snapshot frame: its compute time
    varies with CPU load and would make a rule arm non-deterministic."""
    import time
    from src.agents import agent
    from src.ui import control_panel

    def slow_rule(state):
        time.sleep(0.03)   # compute time, as under CPU load
        return "", {}

    monkeypatch.setattr(agent, "_call_rule", slow_rule)
    result = agent.ai_turn({"model": control_panel.RULE_BASED_MODEL, "telemetry": {}, "minimap": ""})
    assert result["call_metrics"]["latency_ms"] == 0.0
