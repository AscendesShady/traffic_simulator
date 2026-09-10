"""Latency-aware control: aim the decision at where traffic WILL be.

A model needs seconds to answer, and buses keep moving meanwhile. The agent
carries the previous turn's measured latency forward as a decision horizon and
shows, per route, where the nearest bus will be when the flags actually land.
"""
import agent


def telemetry_with_eta(eta_sec, route_id="R1_EB_A_NB"):
    return {
        "simulation_time_seconds": 12.0,
        "routes": {
            other: {"active": True, "tsp_enabled": False, "dbl_enabled": False}
            for other in agent.guard.ROUTE_ORDER
        },
        "active_buses": [
            {
                "bus_id": "LATENCY_BUS",
                "route_id": route_id,
                "direction": "EB",
                "route_leg": {"node_x": 300, "movement": "LEFT"},
                "leg_state": "APPROACHING",
                "distance_to_stop_bar_px": 120.0,
                "eta_to_stop_bar_sec_freeflow": eta_sec,
                "passengers": 45,
                "priority_granted": False,
            }
        ],
    }


def route_line(minimap, route_id="R1_EB_A_NB"):
    for line in minimap.splitlines():
        if route_id in line:
            return line
    raise AssertionError(f"{route_id} missing from minimap:\n{minimap}")


def test_minimap_includes_lag_header():
    minimap = agent.read_minimap(
        {"telemetry": telemetry_with_eta(20.0), "decision_lag_sec": 10.0}
    )["minimap"]

    header = [
        line for line in minimap.splitlines() if line.startswith("DECISION_LAG_SEC=")
    ]
    assert header, minimap
    assert header[0].startswith("DECISION_LAG_SEC=10.0")
    # The horizon must be stated before the routes it applies to.
    assert minimap.index("DECISION_LAG_SEC=") < minimap.index("ROUTES:")


def test_eta_at_decision_land_computed():
    """A bus 3s out is 7s PAST the node once a 10s decision lands."""
    minimap = agent.read_minimap(
        {"telemetry": telemetry_with_eta(3.0), "decision_lag_sec": 10.0}
    )["minimap"]
    line = route_line(minimap)

    assert "nearest_eta_sec=3.0" in line
    assert "eta_at_decision_land_sec=-7.0" in line
    assert "actionable=False" in line


def test_actionable_true_for_future_bus():
    minimap = agent.read_minimap(
        {"telemetry": telemetry_with_eta(15.0), "decision_lag_sec": 10.0}
    )["minimap"]
    line = route_line(minimap)

    assert "eta_at_decision_land_sec=5.0" in line
    assert "actionable=True" in line


def test_first_turn_uses_default_lag():
    """No prior measurement must not crash, and must still give a horizon."""
    assert agent.decision_lag_seconds(None) == agent.DEFAULT_DECISION_LAG_SEC
    assert agent.decision_lag_seconds(0) == agent.DEFAULT_DECISION_LAG_SEC
    assert agent.decision_lag_seconds("nonsense") == agent.DEFAULT_DECISION_LAG_SEC
    assert agent.decision_lag_seconds(float("nan")) == (
        agent.DEFAULT_DECISION_LAG_SEC
    )

    # A minimap built with no lag in state falls back to the default.
    minimap = agent.read_minimap({"telemetry": telemetry_with_eta(20.0)})["minimap"]
    assert f"DECISION_LAG_SEC={agent.DEFAULT_DECISION_LAG_SEC}" in minimap


def test_decision_lag_carried_forward():
    """The measured latency of one turn becomes the next turn's horizon."""
    assert agent.decision_lag_seconds(10000) == 10.0

    # Replay the loop's carry-forward: turn 1 starts on the default, measures
    # 10s, and turn 2 must be built with that measurement as its horizon.
    lag = agent.DEFAULT_DECISION_LAG_SEC
    turn_one = agent.read_minimap(
        {"telemetry": telemetry_with_eta(20.0), "decision_lag_sec": lag}
    )["minimap"]
    assert f"DECISION_LAG_SEC={agent.DEFAULT_DECISION_LAG_SEC}" in turn_one

    turn_one_result = {"call_metrics": {"latency_ms": 10000}}
    lag = agent.decision_lag_seconds(
        turn_one_result["call_metrics"].get("latency_ms")
    )

    turn_two = agent.read_minimap(
        {"telemetry": telemetry_with_eta(20.0), "decision_lag_sec": lag}
    )["minimap"]
    assert "DECISION_LAG_SEC=10.0" in turn_two
    # The same 20s bus is judged against the newly measured horizon.
    assert "eta_at_decision_land_sec=10.0" in route_line(turn_two)

    # A turn whose call reported no latency keeps a usable horizon.
    assert agent.decision_lag_seconds(
        {"call_metrics": {}}["call_metrics"].get("latency_ms")
    ) == agent.DEFAULT_DECISION_LAG_SEC


def test_actionable_window_rejects_far_future_buses():
    """A bus beyond the horizon is not this turn's decision to make."""
    beyond = agent.ACTIONABLE_HORIZON_SEC + 10.0
    assert agent.is_actionable(beyond) is False
    assert agent.is_actionable(None) is False
    assert agent.is_actionable(0) is False
    assert agent.is_actionable(1.0) is True


def test_prompt_teaches_latency_aware_reasoning():
    assert "DECISION_LAG_SEC" in agent.SYSTEM_PROMPT
    assert "eta_at_decision_land_sec" in agent.SYSTEM_PROMPT
    assert "actionable=true" in agent.SYSTEM_PROMPT
