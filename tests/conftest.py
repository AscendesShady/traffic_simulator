import copy
import os
import sys
import tkinter

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.ui.control_panel as control_panel
from src.ui.canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from src.core.signal_controller import SignalController


@pytest.fixture
def base_geometry():
    return {"INT_X": INT_X, "H_Y": H_Y, "ROAD_W": ROAD_W, "STOP": STOP, "LANE": LANE}


@pytest.fixture
def signal_system():
    return SignalController(
        global_config={"green_time": 20, "is_running": True},
        yellow_time=3,
        red_clearance_time=3,
    )


@pytest.fixture(autouse=True)
def isolate_runtime_files(tmp_path_factory, monkeypatch):
    """Point every live data/logs/results path at tmp_path.

    The suite calls perform_full_reset / write_ai_control / the guard with
    whatever paths the modules hold, and a test that does not redirect them
    deletes logs/*.jsonl and rewrites data/ai_control.json of a simulator
    running in another process: in the 2026-09-21 batch, three runs lost
    every log row before the minute the suite was run (the workbook's Bus
    Events sheet then disagreed with buses_served and the summary row was
    refused) and the live agent dropped to observation-only. A test may
    still monkeypatch a path of its own; this only moves the default.
    """
    from src.agents import agent
    from src.core import guard, main
    from src.telemetry import bus_event_log, telemetry_exporter
    from src.ui import telemetry_dashboard

    # Its own directory, not tmp_path: tests assert on what they alone put there.
    root = tmp_path_factory.mktemp("runtime")
    data, logs, results = root / "data", root / "logs", root / "results"
    for folder in (data, logs, results):
        folder.mkdir(parents=True)
    for module, name, path in (
        (main, "DATA_DIR", data), (main, "LOGS_DIR", logs), (main, "RESULTS_DIR", results),
        (main, "TELEMETRY_PATH", data / "traffic_state_telemetry.json"),
        (main, "DECISION_PATH", data / "decision.json"),
        (main, "AGENT_TURN_LOG_PATH", logs / "agent_turn_log.jsonl"),
        (main, "TELEMETRY_LOG_PATH", logs / "telemetry_log.jsonl"),
        (main, "BUS_EVENTS_LOG_PATH", logs / "bus_events.jsonl"),
        (main, "TRIPINFO_LOG_PATH", logs / "tripinfo.jsonl"),
        (main, "FCD_LOG_PATH", logs / "fcd.csv"),
        (main, "EXCEL_EXPORT_DIR", results),
        (main, "EXPERIMENT_SUMMARY_PATH", results / "experiment_summary.csv"),
        (main, "INSTANCE_LOCK_PATH", data / "simulator.lock"),
        (guard, "REJECT_LOG_PATH", logs / "agent_rejects.log"),
        (agent, "TELEMETRY_PATH", data / "traffic_state_telemetry.json"),
        (agent, "AI_CONTROL_PATH", data / "ai_control.json"),
        (agent, "DECISION_PATH", data / "decision.json"),
        (agent, "TURN_LOG_PATH", logs / "agent_turn_log.jsonl"),
        (control_panel, "AI_CONTROL_PATH", data / "ai_control.json"),
        (bus_event_log, "DEFAULT_BUS_EVENTS_PATH", logs / "bus_events.jsonl"),
        (telemetry_exporter, "DEFAULT_TELEMETRY_PATH", data / "traffic_state_telemetry.json"),
        (telemetry_dashboard, "TELEMETRY_FILE", data / "traffic_state_telemetry.json"),
        (telemetry_dashboard, "AGENT_TURN_LOG_FILE", logs / "agent_turn_log.jsonl"),
        (telemetry_dashboard, "TELEMETRY_LOG_FILE", logs / "telemetry_log.jsonl"),
        (telemetry_dashboard, "BUS_EVENTS_LOG_FILE", logs / "bus_events.jsonl"),
        (telemetry_dashboard, "AI_CONTROL_FILE", data / "ai_control.json"),
        (telemetry_dashboard, "DASHBOARD_EXPORT_DIR", results),
    ):
        monkeypatch.setattr(module, name, path)


@pytest.fixture(autouse=True)
def destroy_leftover_tk_root():
    """Tear down a Tk root a test leaves behind.

    A root is process-global state: left alive it keeps its interpreter and
    its pending `after` callbacks around for every later test, so one GUI
    test's window state can reach the next one. Tests that destroy their own
    root are unaffected -- this only sweeps up what they miss.
    """
    yield
    root = getattr(tkinter, "_default_root", None)
    if root is None:
        return
    try:
        root.destroy()
    except Exception:
        # Already destroyed, or its interpreter is gone: nothing left to do
        # but drop the reference so the next test starts clean.
        pass
    tkinter._default_root = None


@pytest.fixture(autouse=True)
def restore_shared_configuration():
    """Also drops main's measured-saturation cache.

    Production measures S once per regime and reuses it for every run of a
    sweep, which is the point -- but a cached S from an earlier test makes a
    later test's monkeypatched calibrate_saturation_flow never run, and the
    failure looks like a Webster bug rather than leaked state.
    """
    from src.core import main as _main

    _main._saturation_cache.clear()
    global_snapshot = copy.deepcopy(control_panel.global_config)
    routes_snapshot = copy.deepcopy(control_panel.bus_routes_config)
    approaches_snapshot = copy.deepcopy(control_panel.approach_configs)
    yield
    control_panel.global_config.clear()
    control_panel.global_config.update(global_snapshot)
    control_panel.bus_routes_config.clear()
    control_panel.bus_routes_config.update(routes_snapshot)
    control_panel.approach_configs.clear()
    control_panel.approach_configs.update(approaches_snapshot)
    _main._saturation_cache.clear()
