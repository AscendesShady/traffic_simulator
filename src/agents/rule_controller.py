"""Deterministic, non-LLM decision source: conditional actuated TSP/DBL.

This is the classical comparator for the LLM controller. It reads exactly
the telemetry the agent reads, applies a fixed rule, and emits exactly the
positional flag object a model is asked to emit, so the guard, the
locked-route overlay, decision.json, the merge in main.py, the turn log and
every export treat a rule turn identically to a model turn. Only the
decision-maker differs, which is what makes rule-vs-LLM a clean paired
comparison of decision quality rather than of mechanism.

The rule is conditional actuated priority, not naive always-grant:

    TSP  an actionable approaching bus AND the conflicting cross-street's
         queued passenger load is below RULE_CROSS_QUEUE_THRESHOLD_PAX,
         capped at MAX_TSP_GRANTS_PER_NODE per node per decision (the
         highest net passenger benefit wins).
    DBL  an approaching bus AND no stopped/crawling queue is ahead in its
         left-most DBL lane AND its merge corridor is not obstructed.

Nothing here touches the simulation: it only produces a decision.
"""

import sys

from src.ui import control_panel
from src.core import guard


# The rule's name in the same ai_runtime["model"] field the LLMs use, so
# selecting it needs no second control path.
RULE_MODEL_NAME = control_panel.RULE_BASED_MODEL

# Queued cross-street passengers at or above which priority is withheld.
# The default equals one bus load: grant only when the bus plainly carries
# more people than the cross traffic the grant would hold. Tunable so the
# threshold can be sensitivity-tested and reported.
RULE_CROSS_QUEUE_THRESHOLD_PAX = 45

# Hard restraint against the flooding failure mode: however many buses
# qualify at one node, only this many are granted per decision.
MAX_TSP_GRANTS_PER_NODE = 1

# The rule decides in microseconds, so its decision lands on the very next
# merge tick. This is its horizon by construction, not by measurement: the
# agent's latency carry-forward rounds a rule turn to 0.0 ms and would
# otherwise treat that as "unmeasured" and fall back to the model default.
RULE_DECISION_LAG_SEC = 0.0

# Two-phase geometry: EW (EB/WB) and NS (NB/SB) conflict with each other.
CONFLICTING_APPROACHES = {
    "EB": ("NB", "SB"),
    "WB": ("NB", "SB"),
    "NB": ("EB", "WB"),
    "SB": ("EB", "WB"),
}

APPROACHES = ("EB", "WB", "NB", "SB")


def is_rule_model(model) -> bool:
    """True when the operator selected the rule instead of a model."""
    return isinstance(model, str) and model.strip().lower() == RULE_MODEL_NAME


def _agent_module():
    """Return the agent module without importing it circularly.

    agent.py imports this module, so importing it back at module scope would
    be circular; and when agent.py runs as __main__ a plain ``import agent``
    would execute a second, separate copy. Resolve whatever is already
    loaded first, and fall back to a real import for standalone callers
    such as the tests.
    """
    module = sys.modules.get("agent")
    if module is not None:
        return module
    main_module = sys.modules.get("__main__")
    if str(getattr(main_module, "__file__", "")).endswith("agent.py"):
        return main_module
    from src.agents import agent

    return agent


def _node_queue_passengers(telemetry, node_key, node_position):
    """Queued passengers by approach at one node.

    Prefers the node's own queues_passengers_est and falls back to the
    flattened network block, exactly as the agent's minimap does, so the
    rule reads the same numbers the model is shown.
    """
    signal_state = telemetry.get("signal_state") or {}
    nodes = signal_state.get("nodes") or {}
    node = nodes.get(str(node_key)) if isinstance(nodes, dict) else None
    waiting = node.get("queues_passengers_est") if isinstance(node, dict) else None
    if not isinstance(waiting, dict):
        summary = telemetry.get("network_summary") or {}
        flattened = summary.get("queues_passengers_est") or {}
        if not isinstance(flattened, dict):
            flattened = {}
        prefix = "A" if node_position == 0 else "B"
        waiting = {
            "EB": flattened.get("EB", 0),
            "WB": flattened.get("WB", 0),
            "NB": flattened.get(f"{prefix}_NB", 0),
            "SB": flattened.get(f"{prefix}_SB", 0),
        }
    result = {}
    for approach in APPROACHES:
        try:
            value = int(waiting.get(approach, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        result[approach] = max(0, value)
    return result


def _node_positions(telemetry):
    """Map each node key to its sorted position, for the flattened fallback."""
    signal_state = telemetry.get("signal_state") or {}
    nodes = signal_state.get("nodes") or {}
    if not isinstance(nodes, dict):
        return {}
    return {
        str(node_key): position
        for position, node_key in enumerate(sorted(nodes, key=str))
    }


def cross_street_passengers(telemetry, node_key, approach):
    """Queued passengers on the approaches a grant to ``approach`` would hold."""
    positions = _node_positions(telemetry)
    waiting = _node_queue_passengers(
        telemetry, node_key, positions.get(str(node_key), 0)
    )
    conflicting = CONFLICTING_APPROACHES.get(str(approach), ())
    return sum(waiting.get(name, 0) for name in conflicting)


def _candidates(telemetry, decision_lag_sec):
    """One record per route that has an approaching bus.

    ``actionable`` marks the buses whose arrival still falls after this
    decision lands -- the agent's own definition, reused verbatim rather
    than restated, so rule and model judge the same buses.
    """
    agent = _agent_module()
    approaching = agent.actionable_buses(telemetry)
    records = []
    for route_id, buses in approaching.items():
        if not buses:
            continue
        nearest = buses[0]
        leg = nearest.get("route_leg") or {}
        node_key = str(leg.get("node_x"))
        approach = str(leg.get("approach") or nearest.get("direction") or "")
        landed_eta = agent.eta_at_decision_land(
            nearest.get("eta_to_stop_bar_sec_freeflow"), decision_lag_sec
        )
        try:
            passengers = int(nearest.get("passengers", 0) or 0)
        except (TypeError, ValueError):
            passengers = 0
        records.append(
            {
                "route_id": route_id,
                "node_key": node_key,
                "approach": approach,
                "passengers": passengers,
                "landed_eta_sec": landed_eta,
                "actionable": bool(agent.is_actionable(landed_eta)),
                "cross_pax": cross_street_passengers(telemetry, node_key, approach),
            }
        )
    return records


def _route_flag(telemetry, route_id, key):
    routes = telemetry.get("routes") or {}
    route = routes.get(route_id) if isinstance(routes, dict) else None
    return bool(route.get(key, False)) if isinstance(route, dict) else False


def rule_based_decision(
    telemetry,
    decision_lag_sec=None,
    cross_queue_threshold_pax=RULE_CROSS_QUEUE_THRESHOLD_PAX,
    max_tsp_grants_per_node=MAX_TSP_GRANTS_PER_NODE,
):
    """Decide TSP/DBL flags by rule, in the model's own output shape.

    Returns ``{"tsp": [6 bools], "dbl": [6 bools], "reason": str}`` with the
    booleans positioned by guard.ROUTE_ORDER -- the identical structure
    guard.validate_flags_positional accepts from an LLM.
    """
    if not isinstance(telemetry, dict):
        telemetry = {}
    if decision_lag_sec is None:
        decision_lag_sec = RULE_DECISION_LAG_SEC

    tsp_routes = set()
    dbl_routes = set()
    granted_notes = []
    withheld_notes = []

    records = _candidates(telemetry, decision_lag_sec)

    # DBL is a lane reservation rather than a signal grant. A queue ahead or
    # an obstructed merge makes the reservation useless, so telemetry's
    # authoritative combined obstruction flag vetoes the activation.
    for record in records:
        route_id = record["route_id"]
        if _route_flag(
            telemetry, route_id, "dbl_lane_obstructed"
        ) or _route_flag(telemetry, route_id, "dbl_lane_queue_ahead"):
            withheld_notes.append(f"DBL {route_id} lane obstructed or queued")
            continue
        dbl_routes.add(route_id)

    # TSP is conditional and capped, per node.
    by_node = {}
    for record in records:
        if not record["actionable"]:
            continue
        if record["cross_pax"] >= cross_queue_threshold_pax:
            withheld_notes.append(
                f"TSP {record['route_id']}@{record['node_key']} cross "
                f"{record['cross_pax']}>={cross_queue_threshold_pax}"
            )
            continue
        by_node.setdefault(record["node_key"], []).append(record)

    for node_key in sorted(by_node):
        qualifying = by_node[node_key]
        # Highest net passenger benefit first; ETA then route id break ties
        # so the same telemetry always yields the same decision.
        qualifying.sort(
            key=lambda item: (
                -(item["passengers"] - item["cross_pax"]),
                item["landed_eta_sec"]
                if item["landed_eta_sec"] is not None
                else float("inf"),
                item["route_id"],
            )
        )
        for rank, record in enumerate(qualifying):
            if rank < max_tsp_grants_per_node:
                tsp_routes.add(record["route_id"])
                granted_notes.append(
                    f"TSP {record['route_id']}@{node_key} "
                    f"{record['passengers']}pax vs cross {record['cross_pax']}"
                    f"<{cross_queue_threshold_pax}"
                )
            else:
                withheld_notes.append(
                    f"TSP {record['route_id']}@{node_key} node cap "
                    f"{max_tsp_grants_per_node}"
                )

    for route_id in sorted(dbl_routes):
        granted_notes.append(f"DBL {route_id}")

    if granted_notes:
        reason = "rule: " + "; ".join(granted_notes)
    else:
        reason = "rule: no grant"
    if withheld_notes:
        reason += " | withheld: " + "; ".join(withheld_notes)

    return {
        "tsp": [route_id in tsp_routes for route_id in guard.ROUTE_ORDER],
        "dbl": [route_id in dbl_routes for route_id in guard.ROUTE_ORDER],
        "reason": reason[: guard.MAX_REASON_LEN],
    }
