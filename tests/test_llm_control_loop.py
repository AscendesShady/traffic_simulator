import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

import agent
import control_panel
import guard
import main
from signal_controller import SignalController
from tests.helpers import make_bus_for_leg


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


def positional_output(tsp_route=None, dbl_route=None, reason=""):
    return {
        "reason": reason,
        "tsp": [route_id == tsp_route for route_id in guard.ROUTE_ORDER],
        "dbl": [route_id == dbl_route for route_id in guard.ROUTE_ORDER],
    }


def agent_state(**overrides):
    state = {
        "telemetry": {},
        "minimap": "",
        "locked_routes": set(),
        "raw_output": "",
        "call_metrics": {},
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
        + json.dumps(positional_output(tsp_route="R1_EB_A_NB"))
        + "\n``` trailing text"
    )

    extracted = guard.extract_json(raw)
    assert guard.validate_flags_positional(extracted) == flags

    invalid = {"flags": valid_flags()}
    invalid["flags"]["R1_EB_A_NB"]["tsp"] = 1
    assert guard.validate_flags(invalid) is None

    extra_route = {"flags": valid_flags()}
    extra_route["flags"]["HALLUCINATED"] = {"tsp": False, "dbl": False}
    assert guard.validate_flags(extra_route) is None


def test_validate_flags_positional_maps_canonical_route_order():
    tsp = [False] * len(guard.ROUTE_ORDER)
    dbl = [False] * len(guard.ROUTE_ORDER)
    tsp[0] = True
    dbl[-1] = True

    mapped = guard.validate_flags_positional({"tsp": tsp, "dbl": dbl})

    assert list(mapped) == guard.ROUTE_ORDER
    assert mapped[guard.ROUTE_ORDER[0]] == {"tsp": True, "dbl": False}
    assert mapped[guard.ROUTE_ORDER[-1]] == {"tsp": False, "dbl": True}
    assert guard.ROUTE_ORDER == sorted(control_panel.bus_routes_config.keys())


def test_position_one_tsp_reaches_merge_and_signal_controller(
    tmp_path, monkeypatch
):
    route_id = guard.ROUTE_ORDER[0]
    assert route_id == "R1_EB_A_NB"
    decision = guard.safe_decision(
        json.dumps(
            positional_output(
                tsp_route=route_id,
                reason="Prioritize the approaching loaded bus.",
            )
        ),
        turn=7,
        model="nemotron-3-nano:4b",
    )
    assert decision["status"] == "OK"
    assert decision["flags"][route_id] == {"tsp": True, "dbl": False}

    decision_path = tmp_path / "decision.json"
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    runtime = {"tick_seconds": 5, "last_status": "INACTIVE"}
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)

    assert main.merge_ai_decision(decision_path) is True
    assert control_panel.bus_routes_config[route_id]["tsp_enabled"] is True

    bus = make_bus_for_leg(route_id, node_x=300, bus_id="POSITIONAL_TSP")
    controller = SignalController(control_panel.global_config)
    controller.update([bus])
    request = controller.get_node_status(300)["active_request"]

    assert request is not None
    assert request["route_id"] == route_id
    assert request["tsp_requested"] is True
    assert request["dbl_requested"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"tsp": [False] * 5, "dbl": [False] * 6},
        {"tsp": [False] * 6, "dbl": [False] * 7},
        {"tsp": [False, False, 1, False, False, False], "dbl": [False] * 6},
        {"tsp": [False] * 6, "dbl": [False, False, "false", False, False, False]},
        {"dbl": [False] * 6},
        {"tsp": [False] * 6},
    ],
)
def test_validate_flags_positional_rejects_malformed_arrays(payload):
    assert guard.validate_flags_positional(payload) is None


def test_safe_decision_does_not_accept_old_keyed_model_contract(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(guard, "REJECT_LOG_PATH", tmp_path / "rejects.log")

    decision = guard.safe_decision(
        json.dumps({"reason": "Old contract.", "flags": valid_flags()}),
        turn=1,
        model="test-model",
    )

    assert decision["status"] == "HELD_ALL_OFF"
    assert decision["flags"] == guard.all_off_flags()


def test_guard_captures_optional_reason_without_weakening_flags(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(guard, "REJECT_LOG_PATH", tmp_path / "rejects.log")
    flags = valid_flags(tsp_route="R1_EB_A_NB")

    valid = guard.safe_decision(
        json.dumps(
            positional_output(
                tsp_route="R1_EB_A_NB",
                reason="Prioritize the loaded bus.",
            )
        ),
        turn=1,
        model="test-model",
    )
    assert valid["status"] == "OK"
    assert valid["flags"] == flags
    assert valid["reason"] == "Prioritize the loaded bus."

    for optional_reason in (None, 42, ["not", "text"], ""):
        payload = positional_output(tsp_route="R1_EB_A_NB")
        if optional_reason is not None:
            payload["reason"] = optional_reason
        decision = guard.safe_decision(
            json.dumps(payload), turn=2, model="test-model"
        )
        assert decision["status"] == "OK"
        assert decision["flags"] == flags
        assert decision["reason"] == ""

    long_reason = guard.safe_decision(
        json.dumps(
            positional_output(
                tsp_route="R1_EB_A_NB", reason="x" * 5000
            )
        ),
        turn=3,
        model="test-model",
    )
    assert long_reason["status"] == "OK"
    assert long_reason["reason"] == "x" * guard.MAX_REASON_LEN

    punctuation = 'Release Node B because queue } exceeds the "normal" load.'
    punctuated = guard.safe_decision(
        json.dumps(
            positional_output(tsp_route="R1_EB_A_NB", reason=punctuation)
        ),
        turn=4,
        model="test-model",
    )
    assert punctuated["status"] == "OK"
    assert punctuated["reason"] == punctuation

    malformed_output = positional_output()
    malformed_output["tsp"][0] = 1
    held = guard.safe_decision(
        json.dumps({**malformed_output, "reason": "This must not survive."}),
        turn=5,
        model="test-model",
    )
    assert held["status"] == "HELD_ALL_OFF"
    assert held["flags"] == guard.all_off_flags()
    assert held["reason"] == ""


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
        "simulation_running": False,
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


def test_api_dropdown_hidden_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert control_panel.get_api_models() == ["None"]


def test_api_dropdown_lists_models_with_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured-for-test")

    assert control_panel.get_api_models() == [
        "None",
        *control_panel.API_MODEL_REGISTRY["GEMINI_API_KEY"],
    ]


def test_single_active_model(tmp_path, monkeypatch):
    class FakeSelector:
        def __init__(self, value):
            self.value = value

        def set(self, value):
            self.value = value

    runtime = {
        "armed": True,
        "model": "local-model:latest",
        "tick_seconds": 5,
        "last_status": "OK",
    }
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    monkeypatch.setattr(control_panel, "AI_CONTROL_PATH", tmp_path / "ai_control.json")

    local_selector = FakeSelector("local-model:latest")
    api_selector = FakeSelector("gemini-2.5-flash")
    selected = control_panel.set_active_ai_model(
        api_selector.value, local_selector
    )
    assert selected == "gemini-2.5-flash"
    assert runtime["model"] == "gemini-2.5-flash"
    assert local_selector.value == "None"
    assert runtime["last_status"] == "MODEL_CHANGED_WAITING"

    local_selector.value = "local-model:latest"
    selected = control_panel.set_active_ai_model(
        local_selector.value, api_selector
    )
    assert selected == "local-model:latest"
    assert runtime["model"] == "local-model:latest"
    assert api_selector.value == "None"
    assert json.loads(control_panel.AI_CONTROL_PATH.read_text(encoding="utf-8"))[
        "model"
    ] == "local-model:latest"


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
                "timestamp": main.time.time(),
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

    invalid = {"timestamp": main.time.time(), "flags": valid_flags()}
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


def test_fresh_decision_applies(tmp_path, monkeypatch):
    now = 1_900_000_000.0
    monkeypatch.setattr(main.time, "time", lambda: now)
    runtime = {"tick_seconds": 5, "last_status": "INACTIVE"}
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    decision_path = tmp_path / "decision.json"
    flags = valid_flags(tsp_route="R1_EB_A_NB")
    decision_path.write_text(
        json.dumps({"timestamp": now, "status": "OK", "flags": flags}),
        encoding="utf-8",
    )

    assert main.merge_ai_decision(decision_path)
    assert runtime["last_status"] == "OK"
    assert control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] is True


def test_stale_decision_held_all_off(tmp_path, monkeypatch):
    now = 1_900_000_000.0
    monkeypatch.setattr(main.time, "time", lambda: now)
    runtime = {"tick_seconds": 5, "last_status": "INACTIVE"}
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "timestamp": now - 3600,
                "status": "OK",
                "flags": valid_flags(dbl_route="R4_WB_A_SB"),
            }
        ),
        encoding="utf-8",
    )

    assert not main.merge_ai_decision(decision_path)
    assert runtime["last_status"] == "STALE_DECISION"
    assert all(
        route[flag] is False
        for route in control_panel.bus_routes_config.values()
        for flag in ("tsp_enabled", "dbl_enabled")
    )


def test_missing_timestamp_treated_stale(tmp_path, monkeypatch):
    runtime = {"tick_seconds": 5, "last_status": "INACTIVE"}
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        json.dumps({"status": "OK", "flags": valid_flags(tsp_route="R2_EB_B_NB")}),
        encoding="utf-8",
    )

    assert not main.merge_ai_decision(decision_path)
    assert runtime["last_status"] == "STALE_DECISION"
    assert all(
        route[flag] is False
        for route in control_panel.bus_routes_config.values()
        for flag in ("tsp_enabled", "dbl_enabled")
    )


def test_invalid_timestamp_treated_stale(tmp_path, monkeypatch):
    runtime = {"tick_seconds": 5, "last_status": "INACTIVE"}
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    decision_path = tmp_path / "decision.json"

    for invalid_timestamp in ("not-a-time", float("nan"), float("inf")):
        decision_path.write_text(
            json.dumps(
                {
                    "timestamp": invalid_timestamp,
                    "status": "OK",
                    "flags": valid_flags(tsp_route="R6_WB_ONLY"),
                }
            ),
            encoding="utf-8",
        )
        assert not main.merge_ai_decision(decision_path)
        assert runtime["last_status"] == "STALE_DECISION"
        assert all(
            route[flag] is False
            for route in control_panel.bus_routes_config.values()
            for flag in ("tsp_enabled", "dbl_enabled")
        )


def test_stale_threshold_scales_with_tick(tmp_path, monkeypatch):
    now = 1_900_000_000.0
    monkeypatch.setattr(main.time, "time", lambda: now)
    runtime = {"tick_seconds": 15, "last_status": "INACTIVE"}
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    decision_path = tmp_path / "decision.json"
    flags = valid_flags(tsp_route="R3_EB_ONLY")

    decision_path.write_text(
        json.dumps({"timestamp": now - 20, "status": "OK", "flags": flags}),
        encoding="utf-8",
    )
    assert main.merge_ai_decision(decision_path)
    assert control_panel.bus_routes_config["R3_EB_ONLY"]["tsp_enabled"] is True

    decision_path.write_text(
        json.dumps({"timestamp": now - 50, "status": "OK", "flags": flags}),
        encoding="utf-8",
    )
    assert not main.merge_ai_decision(decision_path)
    assert runtime["last_status"] == "STALE_DECISION"


def test_agent_death_self_heals(tmp_path, monkeypatch):
    clock = {"now": 1_900_000_000.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["now"])
    runtime = {"tick_seconds": 5, "last_status": "INACTIVE"}
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "timestamp": clock["now"],
                "status": "OK",
                "flags": valid_flags(dbl_route="R5_WB_B_SB"),
            }
        ),
        encoding="utf-8",
    )

    assert main.merge_ai_decision(decision_path)
    assert control_panel.bus_routes_config["R5_WB_B_SB"]["dbl_enabled"] is True

    clock["now"] += 16
    assert not main.merge_ai_decision(decision_path)
    assert runtime["last_status"] == "STALE_DECISION"
    assert all(
        route[flag] is False
        for route in control_panel.bus_routes_config.values()
        for flag in ("tsp_enabled", "dbl_enabled")
    )


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
    for position, route_id in enumerate(guard.ROUTE_ORDER, start=1):
        assert f"{position}) {route_id}:" in minimap_update["minimap"]


def test_minimap_has_no_recent_decisions():
    old_flags = valid_flags(tsp_route="R1_EB_A_NB", dbl_route="R4_WB_A_SB")
    state = agent_state(
        telemetry={"routes": {}, "active_buses": []},
        recent_decisions=[
            {"turn": 7, "status": "OK", "flags": old_flags},
        ],
    )

    minimap = agent.read_minimap(state)["minimap"]

    assert "RECENT_DECISIONS" not in minimap
    assert '"R1_EB_A_NB"' not in minimap
    assert '"R4_WB_A_SB"' not in minimap


def test_minimap_still_has_routes_and_nodes():
    telemetry = {
        "simulation_time_seconds": 42.0,
        "routes": {
            route_id: {
                "active": route_id == "R1_EB_A_NB",
                "tsp_enabled": False,
                "dbl_enabled": False,
            }
            for route_id in guard.ROUTE_ORDER
        },
        "network_throughput": {
            "passengers_per_minute": 80.0,
            "passengers_per_minute_recent": 95.0,
        },
        "network_summary": {"queues_passengers_est": {"EB": 20, "WB": 12}},
        "signal_state": {
            "nodes": {
                "300": {"phase": "EW_GREEN", "signals": {"EB": "GREEN"}},
                "780": {"phase": "NS_GREEN", "signals": {"NB": "GREEN"}},
            }
        },
        "active_buses": [],
    }

    minimap = agent.read_minimap(agent_state(telemetry=telemetry))["minimap"]

    assert "simulation_time_seconds=42.0" in minimap
    assert "passengers_per_minute=80.0" in minimap
    assert "passengers_per_minute_recent=95.0" in minimap
    assert 'queues_passengers_est={"EB": 20, "WB": 12}' in minimap
    assert "NODES:" in minimap
    assert "node=300 phase=EW_GREEN" in minimap
    assert "node=780 phase=NS_GREEN" in minimap
    assert "ROUTES:" in minimap
    for position, route_id in enumerate(guard.ROUTE_ORDER, start=1):
        assert f"{position}) {route_id}:" in minimap
    assert "approaching_buses=0 none approaching" in minimap


def test_granted_route_stays_locked_through_clearing():
    clearing = {
        "bus_id": "CLEARING",
        "route_id": "R2_EB_B_NB",
        "route_leg": {"node_x": 780, "movement": "LEFT"},
        "leg_state": "IN_INTERSECTION",
        "distance_to_stop_bar_px": 0.0,
        "eta_to_stop_bar_sec_freeflow": 0.0,
        "priority_granted": False,
        "priority_clearing": True,
    }
    state = agent_state(telemetry={"active_buses": [clearing]})

    locked_update = agent.check_locked(state)

    assert locked_update["locked_routes"] == {"R2_EB_B_NB"}
    assert "LOCKED_ROUTES=R2_EB_B_NB" in locked_update["minimap"]


def test_clearing_bus_not_shown_as_approaching():
    route_id = "R2_EB_B_NB"
    clearing = {
        "bus_id": "CLEARING",
        "route_id": route_id,
        "route_leg": {"node_x": 780, "movement": "LEFT"},
        "leg_state": "IN_INTERSECTION",
        "distance_to_stop_bar_px": 0.0,
        "eta_to_stop_bar_sec_freeflow": 0.0,
        "passengers": 60,
        "priority_granted": False,
        "priority_clearing": True,
    }
    telemetry = {
        "routes": {route_id: {"active": True, "tsp_enabled": True}},
        "active_buses": [clearing],
    }
    state = agent_state(telemetry=telemetry)

    minimap_update = agent.read_minimap(state)
    locked_update = agent.check_locked({**state, **minimap_update})

    route_line = next(
        line
        for line in minimap_update["minimap"].splitlines()
        if line.startswith(
            f"{guard.ROUTE_ORDER.index(route_id) + 1}) {route_id}:"
        )
    )
    assert "approaching_buses=0 none approaching" in route_line
    assert "nearest_eta_sec" not in route_line
    assert locked_update["locked_routes"] == {route_id}


def test_approaching_grant_still_locks():
    bus = {
        "bus_id": "APPROACHING",
        "route_id": "R4_WB_A_SB",
        "route_leg": {"node_x": 300, "movement": "LEFT"},
        "leg_state": "APPROACHING",
        "distance_to_stop_bar_px": 80.0,
        "eta_to_stop_bar_sec_freeflow": 1.5,
        "priority_granted": True,
        "priority_clearing": False,
    }

    locked_update = agent.check_locked(
        agent_state(telemetry={"active_buses": [bus]})
    )

    assert locked_update["locked_routes"] == {"R4_WB_A_SB"}


def test_agent_call_is_single_network_call_and_guarded(monkeypatch):
    flags = valid_flags(tsp_route="R2_EB_B_NB")
    reason = "Prioritize the approaching high-occupancy bus."
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        return {
            "message": {
                "content": json.dumps(
                    positional_output(
                        tsp_route="R2_EB_B_NB", reason=reason
                    )
                )
            }
        }

    monkeypatch.setattr(agent, "ollama", SimpleNamespace(chat=fake_chat))
    state = agent_state(minimap="whole network", turn=8)
    raw_update = agent.ai_turn(state)
    guarded = agent.anti_cheat({**state, **raw_update})

    assert len(calls) == 1
    assert calls[0]["model"] == "test-model"
    assert calls[0]["options"] == {"temperature": 0.2}
    assert calls[0]["format"] == agent.OLLAMA_OUTPUT_FORMAT
    assert calls[0]["messages"][1]["content"] == "whole network"
    assert guarded["decision"]["status"] == "OK"
    assert guarded["decision"]["flags"] == flags
    assert guarded["decision"]["reason"] == reason
    assert "one sentence under about 40 words" in agent.SYSTEM_PROMPT
    assert "Never write route IDs as JSON keys" in agent.SYSTEM_PROMPT
    assert agent.OUTPUT_SCHEMA.index('"reason"') < agent.OUTPUT_SCHEMA.index('"tsp"')


def test_ollama_timeout_holds_all_off(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_chat(**kwargs):
        started.set()
        release.wait(timeout=1.0)
        finished.set()
        return {
            "message": {
                "content": json.dumps(positional_output(reason="Too late."))
            }
        }

    monkeypatch.setattr(agent, "ollama", SimpleNamespace(chat=slow_chat))
    monkeypatch.setattr(agent, "_OLLAMA_CLIENT", None)
    monkeypatch.setattr(agent, "_OLLAMA_CALL_LOCK", threading.Lock())
    monkeypatch.setattr(agent, "OLLAMA_TIMEOUT_SECONDS", 0.02)

    state = agent_state()
    started_at = time.perf_counter()
    failed = agent.ai_turn(state)
    elapsed = time.perf_counter() - started_at

    assert started.is_set()
    assert elapsed < 0.15
    guarded = agent.anti_cheat({**state, **failed})

    assert failed["status"] == "INVALID"
    assert "TimeoutError" in failed["raw_output"]
    assert failed["call_metrics"]["latency_ms"] >= 10.0
    assert failed["call_metrics"]["input_tokens"] is None
    assert failed["call_metrics"]["output_tokens"] is None
    assert guarded["decision"]["status"] == "HELD_ALL_OFF"
    assert guarded["decision"]["flags"] == guard.all_off_flags()

    agent.log_turn({**state, **failed}, guarded["decision"])
    logged = json.loads(agent.TURN_LOG_PATH.read_text(encoding="utf-8"))
    assert logged["latency_ms"] is not None
    assert logged["input_tokens"] is None
    assert logged["output_tokens"] is None
    assert logged["tokens_per_sec"] is None

    release.set()
    assert finished.wait(timeout=0.2)


def test_ollama_normal_call_unaffected(monkeypatch):
    expected = json.dumps(
        positional_output(
            tsp_route="R1_EB_A_NB",
            reason="Prioritize the approaching bus.",
        )
    )
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        @staticmethod
        def chat(**kwargs):
            calls.append(("chat", kwargs))
            return {
                "message": {"content": expected},
                "prompt_eval_count": 120,
                "eval_count": 30,
                "eval_duration": 2_000_000_000,
                "total_duration": 3_000_000_000,
            }

    monkeypatch.setattr(agent, "ollama", SimpleNamespace(Client=FakeClient))
    monkeypatch.setattr(agent, "_OLLAMA_CLIENT", None)
    monkeypatch.setattr(agent, "_OLLAMA_CALL_LOCK", threading.Lock())

    raw, metrics = agent._call_ollama("test-model", "whole network")
    decision = guard.safe_decision(raw, turn=1, model="test-model")

    assert raw == expected
    assert calls[0] == ("init", {"timeout": agent.OLLAMA_TIMEOUT_SECONDS})
    assert calls[1][0] == "chat"
    assert calls[1][1]["format"] == agent.OLLAMA_OUTPUT_FORMAT
    assert metrics == {
        "input_tokens": 120,
        "output_tokens": 30,
        "eval_duration_ns": 2_000_000_000,
        "total_duration_ns": 3_000_000_000,
    }
    assert decision["status"] == "OK"
    assert decision["flags"]["R1_EB_A_NB"]["tsp"] is True

    agent.log_turn(
        agent_state(
            raw_output=raw,
            call_metrics={**metrics, "latency_ms": 3100.0},
        ),
        decision,
    )
    logged = json.loads(agent.TURN_LOG_PATH.read_text(encoding="utf-8"))
    assert logged["latency_ms"] == 3100.0
    assert logged["input_tokens"] == 120
    assert logged["output_tokens"] == 30
    assert logged["tokens_per_sec"] == 15.0


def test_ollama_format_fallback_still_works_under_timeout(
    monkeypatch,
):
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        if "format" in kwargs:
            raise TypeError("unexpected keyword argument 'format'")
        return {
            "message": {
                "content": json.dumps(
                    positional_output(reason="Fallback remained guarded.")
                )
            }
        }

    monkeypatch.setattr(agent, "ollama", SimpleNamespace(chat=fake_chat))
    monkeypatch.setattr(agent, "_OLLAMA_CLIENT", None)
    monkeypatch.setattr(agent, "_OLLAMA_CALL_LOCK", threading.Lock())

    raw = agent.ai_turn(agent_state(minimap="whole network"))
    guarded = agent.anti_cheat({**agent_state(), **raw})

    assert raw["status"] == "OK"
    assert guarded["decision"]["status"] == "OK"
    assert guarded["decision"]["flags"] == guard.all_off_flags()
    assert len(calls) == 2
    assert calls[0]["format"] == agent.OLLAMA_OUTPUT_FORMAT
    assert "format" not in calls[1]


def test_gemini_routing(monkeypatch):
    assert agent._is_gemini("gemini-2.5-flash") is True
    assert agent._is_gemini("llama3.1:8b") is False

    calls = []
    monkeypatch.setattr(
        agent,
        "_call_gemini",
        lambda model, system_prompt, minimap: (
            calls.append((model, system_prompt, minimap))
            or (
                json.dumps(positional_output(reason="Cloud route.")),
                {
                    "input_tokens": 42,
                    "output_tokens": 12,
                    "eval_duration_ns": None,
                    "total_duration_ns": None,
                },
            )
        ),
    )
    monkeypatch.setattr(
        agent,
        "ollama",
        SimpleNamespace(
            chat=lambda **kwargs: pytest.fail("Ollama must not handle Gemini")
        ),
    )

    result = agent.ai_turn(
        agent_state(model="gemini-2.5-flash", minimap="network snapshot")
    )

    assert result["status"] == "OK"
    assert calls == [
        ("gemini-2.5-flash", agent.SYSTEM_PROMPT, "network snapshot")
    ]


def test_gemini_call_uses_key_prompt_and_low_temperature(monkeypatch):
    calls = []

    class FakeConfig:
        def __init__(self, **kwargs):
            self.options = kwargs

    class FakeModels:
        @staticmethod
        def generate_content(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                text="  guarded JSON  ",
                usage_metadata=SimpleNamespace(
                    prompt_token_count=42,
                    candidates_token_count=12,
                ),
            )

    class FakeGenai:
        @staticmethod
        def Client(api_key):
            calls.append({"api_key": api_key})
            return SimpleNamespace(models=FakeModels())

    monkeypatch.setenv("GEMINI_API_KEY", "test-secret")
    monkeypatch.setattr(agent, "_genai", FakeGenai)
    monkeypatch.setattr(
        agent,
        "_genai_types",
        SimpleNamespace(GenerateContentConfig=FakeConfig),
    )
    monkeypatch.setattr(agent, "_GEMINI_CLIENT", None)
    monkeypatch.setattr(agent, "_GEMINI_CALL_LOCK", threading.Lock())

    text, metrics = agent._call_gemini(
        "gemini-2.5-flash", "system", "minimap"
    )

    assert text == "  guarded JSON  "
    assert calls[0] == {"api_key": "test-secret"}
    assert calls[1]["model"] == "gemini-2.5-flash"
    assert calls[1]["contents"] == "system\n\nminimap"
    assert calls[1]["config"].options == {"temperature": 0.2}
    assert metrics == {
        "input_tokens": 42,
        "output_tokens": 12,
        "eval_duration_ns": None,
        "total_duration_ns": None,
    }


def test_gemini_failure_holds_all_off(monkeypatch):
    monkeypatch.setattr(
        agent,
        "_call_gemini",
        lambda *args: (_ for _ in ()).throw(RuntimeError("cloud unavailable")),
    )
    state = agent_state(model="gemini-2.5-flash")

    failed = agent.ai_turn(state)
    guarded = agent.anti_cheat({**state, **failed})

    assert failed["status"] == "INVALID"
    assert guarded["decision"]["status"] == "HELD_ALL_OFF"
    assert guarded["decision"]["flags"] == guard.all_off_flags()


def test_gemini_timeout_holds_all_off(monkeypatch):
    class FakeConfig:
        def __init__(self, **kwargs):
            self.options = kwargs

    class SlowModels:
        @staticmethod
        def generate_content(**kwargs):
            time.sleep(0.2)
            return SimpleNamespace(text="late response")

    monkeypatch.setattr(
        agent,
        "_GEMINI_CLIENT",
        SimpleNamespace(models=SlowModels()),
    )
    monkeypatch.setattr(
        agent,
        "_genai_types",
        SimpleNamespace(GenerateContentConfig=FakeConfig),
    )
    monkeypatch.setattr(agent, "_GEMINI_CALL_LOCK", threading.Lock())
    monkeypatch.setattr(agent, "GEMINI_TIMEOUT_SECONDS", 0.02)
    state = agent_state(model="gemini-2.5-flash")

    started = time.perf_counter()
    failed = agent.ai_turn(state)
    elapsed = time.perf_counter() - started
    guarded = agent.anti_cheat({**state, **failed})

    assert elapsed < 0.15
    assert failed["status"] == "INVALID"
    assert "TimeoutError" in failed["raw_output"]
    assert guarded["decision"]["status"] == "HELD_ALL_OFF"
    assert guarded["decision"]["flags"] == guard.all_off_flags()


def test_langgraph_runs_one_complete_network_turn(tmp_path, monkeypatch):
    flags = valid_flags(dbl_route="R5_WB_B_SB")
    reason = "Open the dynamic lane for the nearest loaded bus."
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
                    + json.dumps(
                        positional_output(
                            dbl_route="R5_WB_B_SB", reason=reason
                        )
                    )
                }
            }
        ),
    )

    result = agent.build_graph().invoke(agent_state(turn=11))

    assert result["decision"]["status"] == "OK"
    assert result["decision"]["flags"] == flags
    assert result["decision"]["reason"] == reason
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
    assert turn_records[0]["reason"] == reason
    assert turn_records[0]["pax_per_min_recent"] == 30.0
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
    assert guarded["decision"]["reason"] == ""

    stale = agent.hold(agent_state(turn=10, status="STALE"))
    written = json.loads(decision_path.read_text(encoding="utf-8"))
    assert stale["status"] == "STALE"
    assert written["status"] == "HELD_ALL_OFF"
    assert written["flags"] == guard.all_off_flags()
    assert written["reason"] == ""
    logged = json.loads(agent.TURN_LOG_PATH.read_text(encoding="utf-8"))
    assert logged["turn"] == 10
    assert logged["status"] == "HELD_ALL_OFF"
    assert logged["reason"] == ""
    assert logged["stale"] is True


def test_turn_logging_failure_never_breaks_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "TURN_LOG_PATH", tmp_path / "missing" / "turns.jsonl")
    decision = guard.safe_decision(
        json.dumps(positional_output()), turn=12, model="test-model"
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
        "simulation_running": False,
    }


def test_agent_turn_resets_on_frame_drop():
    previous_decisions = [{"turn": 66}, {"turn": 67}]

    turn, recent, last_frame, reset_detected = agent._track_run_boundary(
        67,
        previous_decisions,
        11_300,
        {"frame_number": 0, "simulation_running": True},
    )
    turn += 1

    assert reset_detected is True
    assert turn == 1
    assert recent == []
    assert last_frame == 0


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


def test_reset_clears_session_logs(tmp_path, monkeypatch):
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

    source = Path(main.__file__).read_text(encoding="utf-8")
    reset_helper = source.split("def perform_full_reset", 1)[1].split(
        "def begin_post_discharge_metering", 1
    )[0]
    assert "reset_session_logs()" in reset_helper
    main_body = source.split("def main():", 1)[1]
    assert main_body.count("perform_full_reset(vehicles, signals, telemetry)") == 2


def test_reset_isolates_runs(tmp_path, monkeypatch):
    telemetry_log = tmp_path / "telemetry_log.jsonl"
    turn_log = tmp_path / "agent_turn_log.jsonl"
    monkeypatch.setattr(main, "TELEMETRY_LOG_PATH", telemetry_log)
    monkeypatch.setattr(main, "AGENT_TURN_LOG_PATH", turn_log)

    telemetry_log.write_text(
        json.dumps({"run": "A", "frame": 60}) + "\n",
        encoding="utf-8",
    )
    turn_log.write_text(
        json.dumps({"run": "A", "turn": 1}) + "\n",
        encoding="utf-8",
    )

    main.reset_session_logs()

    telemetry_log.write_text(
        json.dumps({"run": "B", "frame": 60}) + "\n",
        encoding="utf-8",
    )
    turn_log.write_text(
        json.dumps({"run": "B", "turn": 1}) + "\n",
        encoding="utf-8",
    )

    telemetry_rows = [
        json.loads(line)
        for line in telemetry_log.read_text(encoding="utf-8").splitlines()
    ]
    turn_rows = [
        json.loads(line)
        for line in turn_log.read_text(encoding="utf-8").splitlines()
    ]
    assert telemetry_rows == [{"run": "B", "frame": 60}]
    assert turn_rows == [{"run": "B", "turn": 1}]


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
        "reason": "Prioritize the highest passenger-pressure movement.",
        "pax_per_min_recent": 135.0,
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
        assert decision_headers[4:6] == ["reason", "pax_per_min_at_turn"]
        assert decision_row["reason"] == (
            "Prioritize the highest passenger-pressure movement."
        )
        assert decision_row["pax_per_min_at_turn"] == 135.0
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
