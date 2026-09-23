"""Max-pressure comparator: same path as the rule, different decider."""
import json

import src.agents.agent as agent
import src.agents.rule_controller as rc
import src.core.guard as guard
import src.ui.control_panel as control_panel
from tests.helpers import NODE_A, NODE_B
from tests.test_rule_controller import bus, flags_by_route, telemetry


def _with_blocked(snapshot, node_x, approach):
    node = snapshot["signal_state"]["nodes"][str(node_x)]
    node["downstream_blocked"] = {approach: True}
    return snapshot


# --- max-pressure --------------------------------------------------------------

def test_max_pressure_grants_when_bus_pressure_beats_cross():
    # 45 pax bus + 10 queued on EB vs 50 on the cross street: 55 - 50 > 0.
    snapshot = telemetry(
        [bus("R1_EB_A_NB", NODE_A, "EB", 20)],
        node_queues={str(NODE_A): {"EB": 10, "NB": 30, "SB": 20}},
    )
    decision = rc.max_pressure_decision(snapshot, decision_lag_sec=0.0)
    assert flags_by_route(decision)["R1_EB_A_NB"]["tsp"] is True
    assert decision["reason"].startswith("passenger-pressure-tsp: TSP R1_EB_A_NB@")

    # The rule withholds the same bus: cross 50 >= 45.
    assert flags_by_route(rc.rule_based_decision(snapshot, 0.0))["R1_EB_A_NB"]["tsp"] is False


def test_max_pressure_withholds_when_cross_pressure_wins():
    snapshot = telemetry(
        [bus("R1_EB_A_NB", NODE_A, "EB", 20)],
        node_queues={str(NODE_A): {"NB": 40, "SB": 40}},
    )
    decision = rc.max_pressure_decision(snapshot, decision_lag_sec=0.0)
    assert flags_by_route(decision)["R1_EB_A_NB"]["tsp"] is False
    assert "pressure 45-80<=0" in decision["reason"]


def test_max_pressure_blocked_downstream_has_no_pressure():
    # Cross street would win, but its downstream is blocked: serving it
    # releases nothing, so its pressure is zero and the bus is served.
    snapshot = _with_blocked(
        telemetry(
            [bus("R4_WB_A_SB", NODE_A, "WB", 20)],
            node_queues={str(NODE_A): {"NB": 90}},
        ),
        NODE_A, "NB",
    )
    assert flags_by_route(rc.max_pressure_decision(snapshot, 0.0))["R4_WB_A_SB"]["tsp"] is True
    # And a bus whose own downstream is blocked is never served.
    snapshot = _with_blocked(telemetry([bus("R4_WB_A_SB", NODE_A, "WB", 20)]), NODE_A, "WB")
    assert flags_by_route(rc.max_pressure_decision(snapshot, 0.0))["R4_WB_A_SB"]["tsp"] is False


def test_bus_whose_own_receiving_lane_is_blocked_gets_no_tsp_from_any_arm():
    # The approach's straight lanes read open (no approach-level flag), but
    # telemetry says this bus's exit lane is full: every comparator withholds.
    snapshot = telemetry([bus("R4_WB_A_SB", NODE_A, "WB", 20)])
    snapshot["active_buses"][0]["receiving_blocked"] = True
    for decide in (rc.rule_based_decision, rc.max_pressure_decision):
        decision = decide(snapshot, 0.0)
        assert flags_by_route(decision)["R4_WB_A_SB"]["tsp"] is False
        assert "receiving lane blocked" in decision["reason"]
    record = next(r for r in rc._candidates(snapshot, 0.0) if r["route_id"] == "R4_WB_A_SB")
    assert record["receiving_blocked"] is True


def test_max_pressure_keeps_rule_dbl_veto_and_node_cap():
    snapshot = telemetry(
        [bus("R1_EB_A_NB", NODE_A, "EB", 20), bus("R4_WB_A_SB", NODE_A, "WB", 25)],
        obstructed=("R4_WB_A_SB",),
    )
    flags = flags_by_route(rc.max_pressure_decision(snapshot, 0.0))
    assert flags["R1_EB_A_NB"] == {"tsp": True, "dbl": True}
    assert flags["R4_WB_A_SB"] == {"tsp": False, "dbl": False}


def test_non_llm_models_dispatch_through_the_rule_path():
    for name in control_panel.NON_LLM_MODELS:
        assert rc.is_rule_model(name)
        assert agent.turn_decision_lag(name, 9.0) == 0.0
    assert rc.is_max_pressure_model("max-pressure") and not rc.is_max_pressure_model("rule-based")

    snapshot = telemetry(
        [bus("R1_EB_A_NB", NODE_A, "EB", 20)],
        node_queues={str(NODE_A): {"EB": 10, "NB": 30, "SB": 20}},
    )
    for model, expect in (("rule-based", False), (control_panel.MAX_PRESSURE_MODEL, True)):
        result = agent.ai_turn({"model": model, "telemetry": snapshot, "decision_lag_sec": 0.0})
        assert json.loads(result["raw_output"])["tsp"][guard.ROUTE_ORDER.index("R1_EB_A_NB")] is expect
        assert result["call_metrics"]["input_tokens"] is None


def test_batch_picker_offers_every_comparator(monkeypatch):
    monkeypatch.setattr(control_panel, "get_ollama_models", lambda: ["None"])
    monkeypatch.setattr(control_panel, "get_api_models", lambda: ["None"])
    assert control_panel.get_batch_model_choices() == [
        control_panel.BATCH_BASELINE_LABEL, "rule-based", control_panel.MAX_PRESSURE_MODEL
    ]
