"""Progress files for headless runs and the monitor that reads them."""
import time
import tkinter as tk

import pytest

import run_monitor
from src.experiments import headless_run, progress


def test_a_run_reports_its_progress_and_its_finish(tmp_path, monkeypatch):
    monkeypatch.setenv(progress.PROGRESS_DIR_ENV, str(tmp_path))
    reporter = progress.ProgressReporter("seed 5 baseline", total=1200, every_frames=600)
    for frame in range(1, 601):
        reporter.update(frame)
    [record] = progress.read_all()
    assert record["state"] == "running" and record["done"] == 600 and record["fraction"] == 0.5
    reporter.finish()
    [record] = progress.read_all()
    assert record["state"] == "done" and record["fraction"] == 1.0


def test_a_run_that_stops_reporting_reads_as_stalled(tmp_path, monkeypatch):
    monkeypatch.setenv(progress.PROGRESS_DIR_ENV, str(tmp_path))
    progress.ProgressReporter("orphan", total=100)
    [record] = progress.read_all(now=time.time() + progress.STALE_AFTER_SEC + 1)
    assert record["state"] == "stalled"
    assert progress.clear_finished() == 0          # stalled counts only once it is stale
    assert progress.read_all(now=time.time()) != []


def test_headless_runs_report_only_when_labelled(tmp_path, monkeypatch):
    monkeypatch.setenv(progress.PROGRESS_DIR_ENV, str(tmp_path))
    headless_run.run(3, 120, tsp=False)
    assert progress.read_all() == []
    headless_run.run(3, 120, tsp=False, progress_label="labelled run")
    [record] = progress.read_all()
    assert record["label"] == "labelled run" and record["state"] == "done" and record["done"] == 120


def test_a_batch_without_progress_files_is_counted_from_its_outputs(tmp_path):
    done = tmp_path / "a.json"
    done.write_text("{}", encoding="utf-8")
    jobs = tmp_path / "jobs.txt"
    jobs.write_text(f"0.6 234 baseline {done}\n0.6 764 baseline {tmp_path / 'b.json'}\n", encoding="utf-8")
    record = progress.jobs_progress(jobs)
    assert (record["done"], record["total"], record["state"]) == (1, 2, "running")


def test_the_monitor_shows_every_run(tmp_path, monkeypatch):
    monkeypatch.setenv(progress.PROGRESS_DIR_ENV, str(tmp_path))
    progress.ProgressReporter("campaign abc", total=4, kind="campaign", every_frames=1).update(1)
    progress.ProgressReporter("abc rule-based seed 5", total=3600, every_frames=1).update(1800)
    root = tk.Tk()
    try:
        monitor = run_monitor.Monitor(root)
        rows = [monitor.tree.item(iid) for iid in monitor.tree.get_children()]
        assert [row["text"] for row in rows] == ["campaign abc", "abc rule-based seed 5"]
        assert rows[1]["values"][0] == "RUNNING" and "50%" in rows[1]["values"][1]
        assert "1 / 4 runs" in rows[0]["values"][2]
        assert monitor.headline.cget("text") == "2 running"
    finally:
        root.destroy()


def test_clear_finished_drops_a_finished_jobs_batch(tmp_path, monkeypatch):
    monkeypatch.setenv(progress.PROGRESS_DIR_ENV, str(tmp_path))
    output = tmp_path / "a.json"
    output.write_text("{}", encoding="utf-8")
    finished, running = tmp_path / "done.txt", tmp_path / "running.txt"
    finished.write_text(f"0.6 234 baseline {output}\n", encoding="utf-8")
    running.write_text(f"0.6 764 baseline {tmp_path / 'b.json'}\n", encoding="utf-8")
    root = tk.Tk()
    try:
        monitor = run_monitor.Monitor(root, jobs=[finished, running])
        monitor.clear()
        assert monitor.jobs == [running]
    finally:
        root.destroy()


def test_a_pytest_session_row_counts_tests_and_removes_itself(tmp_path, monkeypatch):
    """The row tests/conftest.py publishes: tests done and failed, a rate over
    the whole session, no false stall during one slow test, gone at the end."""
    monkeypatch.setenv(progress.PROGRESS_DIR_ENV, str(tmp_path))
    reporter = progress.ProgressReporter(
        "pytest tests", 4, kind="tests", every_frames=1, mean_rate=True, stale_after_sec=600
    )
    reporter.meta["failed"] = 1
    reporter.started -= 60  # time.time() ticks every 15.6 ms on Windows: no elapsed time, no rate
    reporter.update(2)
    [record] = progress.read_all(tmp_path, now=time.time() + 300)  # a 5-minute test, not a dead process
    assert record["state"] == "running"
    amount, speed = run_monitor.row_values(record)[2:4]
    assert amount == "2 / 4 tests · 1 failed" and speed.endswith("tests/min")
    reporter.remove()
    assert progress.read_all(tmp_path) == []
