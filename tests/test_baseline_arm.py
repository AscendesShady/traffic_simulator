"""The no-controller baseline arm must be exactly that: no decision ever
attributed to it, no TSP grant inside it, and any contamination refused
loudly instead of exported as a reference row."""
import json

import pytest

import src.ui.control_panel as control_panel
import src.core.main as main
from src.agents import agent
from src.core import guard
from src.core.signal_controller import SignalController
from src.experiments import headless_run


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "TURN_LOG_PATH", tmp_path / "agent_turn_log.jsonl")
    monkeypatch.setattr(agent, "DECISION_PATH", tmp_path / "decision.json")
    monkeypatch.setattr(main, "AGENT_TURN_LOG_PATH", tmp_path / "turns.jsonl")
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(main, "BUS_EVENTS_LOG_PATH", tmp_path / "bus_events.jsonl")
    monkeypatch.setattr(main, "EXPERIMENT_SUMMARY_PATH", tmp_path / "summary.csv")
    monkeypatch.setattr(control_panel, "write_ai_control", lambda *a, **k: None)
    for key in main.network_throughput:
        main.network_throughput[key] = 0
    main.network_throughput_at_warmup.clear()
    yield
    main.network_throughput_at_warmup.clear()
    control_panel.global_config["test_running"] = False
    control_panel.global_config["test_failed_reason"] = ""


def _telemetry():
    return {
        "timestamp": agent.time.time(),
        "frame_number": 600,
        "simulation_time_seconds": 10.0,
        "routes": {
            route_id: {"active": True, "tsp_enabled": False, "dbl_enabled": False}
            for route_id in guard.VALID_ROUTES
        },
        "network_throughput": {},
        "network_summary": {"queues_passengers_est": {}},
        "signal_state": {"nodes": {}},
        "active_buses": [],
    }


def _turn_log():
    return [
        json.loads(line)
        for line in agent.TURN_LOG_PATH.read_text(encoding="utf-8").splitlines()
    ]


def test_every_baseline_agent_turn_is_observation_only(monkeypatch):
    monkeypatch.setattr(agent, "_read_telemetry", _telemetry)
    graph = agent.build_graph()
    for turn in range(1, 13):  # 60 s of 5-second ticks
        result = graph.invoke(
            {
                "telemetry": {}, "discharge_active": False, "minimap": "",
                "locked_routes": set(), "raw_output": "", "call_metrics": {},
                "decision": {}, "status": "OK", "recent_decisions": [],
                "turn": turn, "model": "None", "decision_lag_sec": 0.0,
            }
        )
        assert result["decision"]["status"] == agent.OBSERVATION_ONLY
        assert result["decision"]["flags"] == guard.all_off_flags()
    statuses = {record["status"] for record in _turn_log()}
    assert statuses == {agent.OBSERVATION_ONLY}
    on_disk = json.loads(agent.DECISION_PATH.read_text(encoding="utf-8"))
    assert on_disk["status"] == agent.OBSERVATION_ONLY


def test_straggler_turn_from_a_previous_arm_cannot_land_as_ok(tmp_path):
    """A model turn that finishes after the panel switched to the baseline
    (or disarmed) is downgraded before it touches decision.json or the log."""
    control_path = tmp_path / "ai_control.json"
    control_path.write_text(json.dumps({"armed": False, "model": "None"}), encoding="utf-8")
    ok_decision = {
        "schema_version": 1, "turn": 7, "timestamp": agent.time.time(),
        "model": "gemini-x", "status": "OK", "flags": guard.all_off_flags(), "reason": "late",
    }
    state = {"turn": 7, "model": "gemini-x", "decision": ok_decision,
             "control_path": str(control_path), "telemetry": {}, "locked_routes": set()}
    agent.write_decision(state)
    assert json.loads(agent.DECISION_PATH.read_text(encoding="utf-8"))["status"] == agent.OBSERVATION_ONLY
    assert _turn_log()[-1]["status"] == agent.OBSERVATION_ONLY

    # Same arm still live: the decision goes through untouched.
    control_path.write_text(json.dumps({"armed": True, "model": "gemini-x"}), encoding="utf-8")
    agent.write_decision(state)
    assert json.loads(agent.DECISION_PATH.read_text(encoding="utf-8"))["status"] == "OK"


def test_baseline_reset_clears_route_flags_left_by_a_previous_model():
    for route in control_panel.bus_routes_config.values():
        route["tsp_enabled"] = True
        route["dbl_enabled"] = True
    control_panel.global_config["test_running"] = True
    control_panel.global_config["test_model"] = "None"
    main.perform_full_reset([], SignalController({"green_time": 100}))
    assert all(
        not route["tsp_enabled"] and not route["dbl_enabled"]
        for route in control_panel.bus_routes_config.values()
    )


def test_headless_baseline_minute_has_no_tsp_treatment():
    control_panel.global_config["test_model"] = "None"
    # Three sim-minutes: a bus needs over a minute to spawn, cross and exit,
    # and only completed buses are recorded -- so a frequent service (the
    # default headways put the first departure at 180 s).
    for route in control_panel.bus_routes_config.values():
        route["headway_sec"] = 30
    records = headless_run.run(seed=3, frames=3 * 3600, tsp=False)
    treated = [
        node for record in records for node in record["nodes"] if node.get("tsp_treated")
    ]
    assert records and not treated


def test_contaminated_baseline_row_is_refused_and_fails_the_run():
    control_panel.global_config["test_model"] = "None"
    control_panel.global_config["test_seed"] = 1
    main.AGENT_TURN_LOG_PATH.write_text(
        json.dumps({"turn": 1, "status": "OK", "model": "gemini-x", "latency_ms": 10}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(main.BaselineContaminationError):
        main.build_experiment_summary_row(300)
    assert main.append_experiment_summary_row(300) is False
    assert not main.experiment_summary_path().exists()
    assert control_panel.global_config["test_failed_reason"].startswith("baseline_contaminated")

    # Observation-only turns are what a watched baseline legitimately logs.
    main.AGENT_TURN_LOG_PATH.write_text(
        json.dumps({"turn": 1, "status": "OBSERVATION_ONLY", "model": "None"}) + "\n",
        encoding="utf-8",
    )
    row = main.build_experiment_summary_row(300)
    assert row["total_decisions"] == 0 and row["buses_tsp_treated"] == 0
