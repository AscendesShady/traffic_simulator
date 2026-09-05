import json
from types import SimpleNamespace

import pytest

import agent
import control_panel
import guard
import main


@pytest.fixture(autouse=True)
def isolate_agent_turn_log(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "TURN_LOG_PATH", tmp_path / "agent_turn_log.jsonl")


def valid_flags(tsp_route=None, dbl_route=None):
    flags = guard.all_off_flags()
    if tsp_route:
        flags[tsp_route]["tsp"] = True
    if dbl_route:
        flags[dbl_route]["dbl"] = True
    return flags


def agent_state(**overrides):
    state = {
        "telemetry": {},
        "minimap": "",
        "locked_routes": set(),
        "raw_output": "",
        "decision": {},
        "status": "OK",
        "recent_decisions": [],
        "turn": 1,
        "model": "test-model",
    }
    state.update(overrides)
    return state


def test_guard_extracts_reasoning_output_and_enforces_exact_boolean_surface():
    flags = valid_flags(tsp_route="R1_EB_A_NB")
    raw = (
        "<think>ignore {nested reasoning}</think>\n```json\n"
        + json.dumps({"flags": flags})
        + "\n``` trailing text"
    )

    extracted = guard.extract_json(raw)
    assert guard.validate_flags(extracted) == flags

    invalid = {"flags": valid_flags()}
    invalid["flags"]["R1_EB_A_NB"]["tsp"] = 1
    assert guard.validate_flags(invalid) is None

    extra_route = {"flags": valid_flags()}
    extra_route["flags"]["HALLUCINATED"] = {"tsp": False, "dbl": False}
    assert guard.validate_flags(extra_route) is None


def test_guard_failure_is_all_off_logged_and_never_raises(tmp_path, monkeypatch):
    reject_log = tmp_path / "agent_rejects.log"
    monkeypatch.setattr(guard, "REJECT_LOG_PATH", reject_log)

    decision = guard.safe_decision("not JSON", turn=7, model="reasoning-model")

    assert decision["status"] == "HELD_ALL_OFF"
    assert decision["flags"] == guard.all_off_flags()
    assert not any(
        route_flags[flag]
        for route_flags in decision["flags"].values()
        for flag in ("tsp", "dbl")
    )
    logged = json.loads(reject_log.read_text(encoding="utf-8").splitlines()[0])
    assert logged == {"turn": 7, "model": "reasoning-model", "raw": "not JSON"}

    monkeypatch.setattr(guard, "REJECT_LOG_PATH", tmp_path / "missing" / "reject.log")
    assert guard.safe_decision(None, object(), None)["status"] == "HELD_ALL_OFF"


def test_control_panel_writes_atomic_agent_control_and_discovers_models(
    tmp_path, monkeypatch
):
    runtime = {
        "armed": True,
        "model": "local-model:latest",
        "tick_seconds": 9,
        "last_status": "OK",
        "last_turn": 3,
    }
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    destination = tmp_path / "ai_control.json"

    assert control_panel.write_ai_control(destination)
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "armed": True,
        "model": "local-model:latest",
        "tick_seconds": 9,
    }
    assert list(tmp_path.iterdir()) == [destination]

    monkeypatch.setattr(
        control_panel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="NAME ID SIZE MODIFIED\nmodel-a:latest abc 1GB now\nmodel-b:8b def 2GB now\n",
        ),
    )
    assert control_panel.get_ollama_models() == [
        "None",
        "model-a:latest",
        "model-b:8b",
    ]


def test_route_flag_button_repaint_reflects_external_decision(monkeypatch):
    class FakeButton:
        def __init__(self):
            self.options = {}

        def config(self, **options):
            self.options.update(options)

    route_id = "R2_EB_B_NB"
    route = control_panel.bus_routes_config[route_id]
    monkeypatch.setitem(route, "tsp_enabled", True)
    monkeypatch.setitem(route, "dbl_enabled", True)
    buttons = {"tsp": FakeButton(), "dbl": FakeButton()}
    monkeypatch.setattr(
        control_panel, "route_flag_buttons", {route_id: buttons}
    )

    control_panel.repaint_route_flag_buttons()

    assert buttons["tsp"].options == {
        "text": "TSP ACTIVE",
        "fg": control_panel.COLOR_SUCCESS,
    }
    assert buttons["dbl"].options == {
        "text": "DBL ACTIVE",
        "fg": control_panel.COLOR_ACCENT,
    }

    monkeypatch.setitem(route, "tsp_enabled", False)
    monkeypatch.setitem(route, "dbl_enabled", False)
    control_panel.repaint_route_flag_buttons()
    assert buttons["tsp"].options["text"] == "TSP OFF"
    assert buttons["tsp"].options["fg"] == control_panel.COLOR_DANGER
    assert buttons["dbl"].options["text"] == "DBL OFF"
    assert buttons["dbl"].options["fg"] == control_panel.COLOR_DANGER


def test_main_merge_applies_valid_flags_and_rejects_invalid_flags(
    tmp_path, monkeypatch
):
    runtime = {
        "armed": True,
        "model": "test-model",
        "tick_seconds": 5,
        "last_status": "WAITING_FOR_DECISION",
        "last_turn": 0,
    }
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    for route in control_panel.bus_routes_config.values():
        monkeypatch.setitem(route, "tsp_enabled", False)
        monkeypatch.setitem(route, "dbl_enabled", False)

    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "turn": 42,
                "status": "OK",
                "flags": valid_flags(
                    tsp_route="R1_EB_A_NB",
                    dbl_route="R4_WB_A_SB",
                ),
            }
        ),
        encoding="utf-8",
    )

    assert main.merge_ai_decision(decision_path)
    assert control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] is True
    assert control_panel.bus_routes_config["R4_WB_A_SB"]["dbl_enabled"] is True
    assert runtime["last_status"] == "OK"
    assert runtime["last_turn"] == 42

    invalid = {"flags": valid_flags()}
    del invalid["flags"]["R6_WB_ONLY"]
    decision_path.write_text(json.dumps(invalid), encoding="utf-8")
    assert not main.merge_ai_decision(decision_path)
    assert runtime["last_status"] == "INVALID_DECISION"
    assert all(
        route[flag] is False
        for route in control_panel.bus_routes_config.values()
        for flag in ("tsp_enabled", "dbl_enabled")
    )


def test_missing_decision_leaves_manual_flags_unchanged(tmp_path, monkeypatch):
    runtime = {"last_status": "INACTIVE", "last_turn": 0}
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    route = control_panel.bus_routes_config["R2_EB_B_NB"]
    monkeypatch.setitem(route, "tsp_enabled", True)

    assert not main.merge_ai_decision(tmp_path / "missing.json")
    assert route["tsp_enabled"] is True
    assert runtime["last_status"] == "WAITING_FOR_DECISION"


def test_minimap_uses_only_approaching_unfinished_bus_legs():
    actionable = {
        "bus_id": "ACTIONABLE",
        "route_id": "R1_EB_A_NB",
        "direction": "NB",
        "route_leg": {"node_x": 300, "movement": "LEFT"},
        "leg_state": "APPROACHING",
        "distance_to_stop_bar_px": 120.0,
        "eta_to_stop_bar_sec_freeflow": 2.0,
        "passengers": 45,
        "priority_granted": True,
    }
    completed = {
        **actionable,
        "bus_id": "COMPLETE",
        "route_leg": None,
        "leg_state": "COMPLETE",
        "distance_to_stop_bar_px": 0.0,
        "eta_to_stop_bar_sec_freeflow": 0.0,
    }
    past_bar = {
        **actionable,
        "bus_id": "PAST_BAR",
        "distance_to_stop_bar_px": -5.0,
        "eta_to_stop_bar_sec_freeflow": 0.0,
    }
    telemetry = {
        "simulation_time_seconds": 10.0,
        "routes": {
            route_id: {
                "active": True,
                "tsp_enabled": route_id == "R1_EB_A_NB",
                "dbl_enabled": False,
            }
            for route_id in guard.VALID_ROUTES
        },
        "network_throughput": {
            "passengers_per_minute": 100.0,
            "passengers_per_minute_recent": 120.0,
        },
        "network_summary": {"queues_passengers_est": {"EB": 20}},
        "signal_state": {"nodes": {"300": {"phase": "EW_GREEN"}}},
        "active_buses": [completed, past_bar, actionable],
    }
    state = agent_state(telemetry=telemetry)

    approaching = agent.actionable_buses(telemetry)
    assert [bus["bus_id"] for bus in approaching["R1_EB_A_NB"]] == [
        "ACTIONABLE"
    ]

    minimap_update = agent.read_minimap(state)
    locked_update = agent.check_locked({**state, **minimap_update})
    assert "approaching_buses=1" in minimap_update["minimap"]
    assert "direction=NB" not in minimap_update["minimap"]
    assert "COMPLETE" not in minimap_update["minimap"]
    assert locked_update["locked_routes"] == {"R1_EB_A_NB"}
    assert "LOCKED_ROUTES=R1_EB_A_NB" in locked_update["minimap"]


def test_agent_call_is_single_network_call_and_guarded(monkeypatch):
    flags = valid_flags(tsp_route="R2_EB_B_NB")
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        return {"message": {"content": json.dumps({"flags": flags})}}

    monkeypatch.setattr(agent, "ollama", SimpleNamespace(chat=fake_chat))
    state = agent_state(minimap="whole network", turn=8)
    raw_update = agent.ai_turn(state)
    guarded = agent.anti_cheat({**state, **raw_update})

    assert len(calls) == 1
    assert calls[0]["model"] == "test-model"
    assert calls[0]["options"] == {"temperature": 0.2}
    assert calls[0]["messages"][1]["content"] == "whole network"
    assert guarded["decision"]["status"] == "OK"
    assert guarded["decision"]["flags"] == flags


def test_langgraph_runs_one_complete_network_turn(tmp_path, monkeypatch):
    flags = valid_flags(dbl_route="R5_WB_B_SB")
    telemetry = {
        "timestamp": agent.time.time(),
        "simulation_time_seconds": 12.0,
        "routes": {
            route_id: {
                "active": True,
                "tsp_enabled": False,
                "dbl_enabled": False,
            }
            for route_id in guard.VALID_ROUTES
        },
        "network_throughput": {
            "passengers_per_minute": 20.0,
            "passengers_per_minute_recent": 30.0,
        },
        "network_summary": {"queues_passengers_est": {}},
        "signal_state": {"nodes": {}},
        "active_buses": [],
    }
    monkeypatch.setattr(agent, "_read_telemetry", lambda: telemetry)
    monkeypatch.setattr(agent, "DECISION_PATH", tmp_path / "decision.json")
    monkeypatch.setattr(
        agent,
        "ollama",
        SimpleNamespace(
            chat=lambda **kwargs: {
                "message": {
                    "content": "<think>compare passenger queues</think>\n"
                    + json.dumps({"flags": flags})
                }
            }
        ),
    )

    result = agent.build_graph().invoke(agent_state(turn=11))

    assert result["decision"]["status"] == "OK"
    assert result["decision"]["flags"] == flags
    assert result["recent_decisions"][-1]["turn"] == 11
    assert json.loads(agent.DECISION_PATH.read_text(encoding="utf-8"))["flags"] == flags
    turn_records = [
        json.loads(line)
        for line in agent.TURN_LOG_PATH.read_text(encoding="utf-8").splitlines()
    ]
    assert len(turn_records) == 1
    assert turn_records[0]["turn"] == 11
    assert turn_records[0]["status"] == "OK"
    assert "<think>compare passenger queues</think>" in turn_records[0]["raw_output"]
    assert turn_records[0]["flags"] == flags
    assert "ROUTES:" in turn_records[0]["minimap"]
    assert turn_records[0]["stale"] is False


def test_agent_failure_and_stale_telemetry_write_all_off(tmp_path, monkeypatch):
    reject_log = tmp_path / "rejects.log"
    decision_path = tmp_path / "decision.json"
    monkeypatch.setattr(guard, "REJECT_LOG_PATH", reject_log)
    monkeypatch.setattr(agent, "DECISION_PATH", decision_path)
    monkeypatch.setattr(
        agent,
        "ollama",
        SimpleNamespace(chat=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down"))),
    )

    state = agent_state(turn=9)
    failed = agent.ai_turn(state)
    guarded = agent.anti_cheat({**state, **failed})
    assert guarded["decision"]["status"] == "HELD_ALL_OFF"
    assert guarded["decision"]["flags"] == guard.all_off_flags()

    stale = agent.hold(agent_state(turn=10, status="STALE"))
    written = json.loads(decision_path.read_text(encoding="utf-8"))
    assert stale["status"] == "STALE"
    assert written["status"] == "HELD_ALL_OFF"
    assert written["flags"] == guard.all_off_flags()
    logged = json.loads(agent.TURN_LOG_PATH.read_text(encoding="utf-8"))
    assert logged["turn"] == 10
    assert logged["status"] == "HELD_ALL_OFF"
    assert logged["stale"] is True


def test_turn_logging_failure_never_breaks_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "TURN_LOG_PATH", tmp_path / "missing" / "turns.jsonl")
    decision = guard.safe_decision(
        json.dumps({"flags": valid_flags()}), turn=12, model="test-model"
    )

    agent.log_turn(
        agent_state(
            minimap="observed network",
            raw_output="<think>full trace</think>",
            locked_routes={"R1_EB_A_NB"},
        ),
        decision,
    )

    assert decision["status"] == "OK"
    assert not agent.TURN_LOG_PATH.exists()


def test_read_ai_control_is_fail_closed_and_clamps_tick(tmp_path):
    missing = agent.read_ai_control(tmp_path / "missing.json")
    assert missing == agent.DEFAULT_CONTROL

    control_path = tmp_path / "ai_control.json"
    control_path.write_text(
        json.dumps({"armed": True, "model": "m", "tick_seconds": 99}),
        encoding="utf-8",
    )
    assert agent.read_ai_control(control_path) == {
        "armed": True,
        "model": "m",
        "tick_seconds": 15,
    }


def test_paused_or_old_telemetry_routes_to_stale_hold(monkeypatch):
    now = agent.time.time()
    monkeypatch.setattr(
        agent,
        "_read_telemetry",
        lambda: {"timestamp": now, "simulation_paused": True},
    )
    paused = agent.load_save(agent_state())
    assert paused["status"] == "STALE"
    assert agent._route_after_load({**agent_state(), **paused}) == "hold"

    monkeypatch.setattr(
        agent,
        "_read_telemetry",
        lambda: {"timestamp": now - agent.STALE_SECONDS - 0.1},
    )
    assert agent.load_save(agent_state())["status"] == "STALE"


def telemetry_payload(frame=60):
    return {
        "frame_number": frame,
        "simulation_time_seconds": frame / 60,
        "network_throughput": {
            "passengers_served_total": 120,
            "passengers_served_bus": 90,
            "passengers_served_car": 30,
            "buses_served": 2,
            "cars_served": 8,
            "passengers_per_minute": 120.0,
            "passengers_per_minute_recent": 135.0,
        },
        "network_summary": {
            "total_vehicles": 24,
            "queues": {"EB": 3, "WB": 4},
            "queues_passengers_est": {"EB": 12, "WB": 16},
        },
    }


def test_telemetry_log_uses_schema_keys_interval_and_ai_state(
    tmp_path, monkeypatch
):
    destination = tmp_path / "telemetry_log.jsonl"
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", destination)
    monkeypatch.setattr(main, "_last_telemetry_log_frame", None)
    monkeypatch.setitem(
        control_panel.global_config,
        "ai_runtime",
        {"armed": True, "last_status": "OK"},
    )

    assert not main.log_telemetry_sample(59, telemetry_payload(59))
    assert main.log_telemetry_sample(60, telemetry_payload(60))
    assert not main.log_telemetry_sample(60, telemetry_payload(60))
    assert not main.log_telemetry_sample(119, telemetry_payload(119))
    assert main.log_telemetry_sample(121, telemetry_payload(121))

    records = [
        json.loads(line)
        for line in destination.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["frame"] for record in records] == [60, 121]
    assert records[0]["vehicles_in_network"] == 24
    assert records[0]["ai_armed"] is True
    assert records[0]["ai_last_status"] == "OK"
    assert records[0]["queues_vehicles"] == {"EB": 3, "WB": 4}
    assert records[0]["pax_per_min_recent"] == 135.0


def test_telemetry_logging_failure_is_nonfatal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        main,
        "TELEMETRY_LOG_PATH",
        tmp_path / "missing" / "telemetry_log.jsonl",
    )
    monkeypatch.setattr(main, "_last_telemetry_log_frame", None)

    assert not main.log_telemetry_sample(60, telemetry_payload(60))
    assert main._last_telemetry_log_frame is None


def test_session_start_removes_only_session_logs(tmp_path, monkeypatch):
    telemetry_log = tmp_path / "telemetry_log.jsonl"
    turn_log = tmp_path / "agent_turn_log.jsonl"
    unrelated = tmp_path / "keep.txt"
    telemetry_log.write_text("old telemetry\n", encoding="utf-8")
    turn_log.write_text("old turns\n", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", telemetry_log)
    monkeypatch.setattr(main, "AGENT_TURN_LOG_PATH", turn_log)
    monkeypatch.setattr(main, "_last_telemetry_log_frame", 600)

    main.reset_session_logs()

    assert not telemetry_log.exists()
    assert not turn_log.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert main._last_telemetry_log_frame is None


def test_excel_export_builds_decisions_and_telemetry_sheets(
    tmp_path, monkeypatch
):
    from openpyxl import load_workbook

    telemetry_log = tmp_path / "telemetry_log.jsonl"
    turn_log = tmp_path / "agent_turn_log.jsonl"
    output = tmp_path / "session_export.xlsx"
    flags = valid_flags(
        tsp_route="R2_EB_B_NB", dbl_route="R5_WB_B_SB"
    )
    decision = {
        "turn": 4,
        "timestamp": 123.5,
        "model": "reasoning-model",
        "status": "OK",
        "flags": flags,
        "locked_routes": ["R1_EB_A_NB"],
        "minimap": "whole network",
        "raw_output": "<think>compare queues</think>",
    }
    turn_log.write_text(
        json.dumps(decision) + "\n{partial crash tail",
        encoding="utf-8",
    )
    sample = {
        "frame": 60,
        "sim_time_s": 1.0,
        "passengers_served_total": 120,
        "passengers_served_bus": 90,
        "passengers_served_car": 30,
        "buses_served": 2,
        "cars_served": 8,
        "pax_per_min_cumulative": 120.0,
        "pax_per_min_recent": 135.0,
        "vehicles_in_network": 24,
        "ai_armed": True,
        "ai_last_status": "OK",
        "queues_vehicles": {"EB": 3},
        "queues_passengers_est": {"EB": 12},
    }
    telemetry_log.write_text(json.dumps(sample) + "\n", encoding="utf-8")
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", telemetry_log)
    monkeypatch.setattr(main, "AGENT_TURN_LOG_PATH", turn_log)

    assert main.export_session_excel(output) == output

    workbook = load_workbook(output, read_only=True)
    try:
        assert workbook.sheetnames == ["Decisions", "Telemetry"]
        decisions = workbook["Decisions"]
        decision_headers = [cell.value for cell in decisions[1]]
        decision_row = {
            header: decisions.cell(2, index + 1).value
            for index, header in enumerate(decision_headers)
        }
        assert decisions.max_row == 2
        assert decision_row["turn"] == 4
        assert decision_row["R2_EB_B_NB_tsp"] is True
        assert decision_row["R5_WB_B_SB_dbl"] is True
        assert decision_row["locked_routes"] == "R1_EB_A_NB"
        assert "<think>compare queues</think>" in decision_row["raw_output"]

        telemetry = workbook["Telemetry"]
        telemetry_headers = [cell.value for cell in telemetry[1]]
        telemetry_row = {
            header: telemetry.cell(2, index + 1).value
            for index, header in enumerate(telemetry_headers)
        }
        assert telemetry.max_row == 2
        assert telemetry_row["vehicles_in_network"] == 24
        assert telemetry_row["ai_armed"] is True
        assert json.loads(telemetry_row["queues_vehicles"]) == {"EB": 3}
    finally:
        workbook.close()


def test_default_excel_export_uses_dedicated_folder(tmp_path, monkeypatch):
    export_folder = tmp_path / "excel_exports"
    telemetry_log = tmp_path / "telemetry.jsonl"
    telemetry_log.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(main, "EXCEL_EXPORT_DIR", export_folder)
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", telemetry_log)
    monkeypatch.setattr(main, "AGENT_TURN_LOG_PATH", tmp_path / "missing-turns.jsonl")

    destination = main.export_session_excel()

    assert destination is not None
    assert destination.parent == export_folder
    assert destination.name.startswith("session_export_")
    assert destination.suffix == ".xlsx"
    assert destination.exists()
