"""Deterministic, non-LLM decision source: conditional actuated TSP/DBL.

This is the classical comparator for the LLM controller. It reads exactly
the telemetry the agent reads, applies a fixed rule, and emits exactly the
positional flag object a model is asked to emit, so the guard, the
locked-route overlay, decision.json, the merge in main.py, the turn log and
every export treat a rule turn identically to a model turn. Only the
decision-maker differs, which is what makes rule-vs-LLM a clean paired
comparison of decision quality rather than of mechanism.

The rule is conditional actuated priority, not naive always-grant:

    TSP  an actionable approaching bus that WOULD STOP without help
         (telemetry's would_have_stopped: red ahead, or its ETA is past the
         residual green) AND the conflicting cross-street's queued passenger
         load is below RULE_CROSS_QUEUE_THRESHOLD_PAX, capped at
         MAX_TSP_GRANTS_PER_NODE per node per decision (the highest net
         passenger benefit wins). A bus arriving inside the green gains
         nothing from priority, so granting it only spends cross-street time.
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
    """True when the operator selected a non-LLM decider (the rule, the
    passenger-pressure gate or the trained RL policy) instead of a model. All
    three share what this flag gates: no inference latency, no sampling,
    no tokens, and a decision that lands on the very next merge tick."""
    return (
        isinstance(model, str)
        and model.strip().lower() in (*control_panel.NON_LLM_MODELS, "max-pressure")
    )


def is_max_pressure_model(model) -> bool:
    return (
        isinstance(model, str)
        and model.strip().lower() in (control_panel.MAX_PRESSURE_MODEL, "max-pressure")
    )


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
                "eta_sec": nearest.get("eta_to_stop_bar_sec_freeflow"),
                "movement": leg.get("movement", "STRAIGHT"),
                "exit_direction": leg.get("exit_direction"),
                "actionable": bool(agent.is_actionable(landed_eta)),
                "cross_pax": cross_street_passengers(telemetry, node_key, approach),
                # Older telemetry without the field: assume it would stop, the
                # pre-existing behaviour.
                "would_stop": bool(nearest.get("would_have_stopped", True)),
                # The bus's own receiving lane (its movement, its lane), not
                # the approach's straight-through average.
                "receiving_blocked": bool(nearest.get("receiving_blocked", False)),
            }
        )
    return records


def _route_flag(telemetry, route_id, key):
    routes = telemetry.get("routes") or {}
    route = routes.get(route_id) if isinstance(routes, dict) else None
    return bool(route.get(key, False)) if isinstance(route, dict) else False


def _decide(telemetry, decision_lag_sec, label, tsp_score, max_tsp_grants_per_node):
    """Shared decision core for every deterministic comparator.

    ``tsp_score(record)`` returns ``(score, note)``: a numeric score when the
    bus qualifies for TSP (higher wins the per-node cap), or ``None`` with a
    withheld note. DBL handling and the per-node cap are identical across
    comparators, so only the TSP criterion differs between arms.
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
        if record["receiving_blocked"]:
            # A green bought for a bus with nowhere to go releases nothing;
            # the controller would refuse it (DOWNSTREAM_BLOCKED) anyway.
            withheld_notes.append(
                f"TSP {record['route_id']}@{record['node_key']} receiving lane blocked"
            )
            continue
        score, note = tsp_score(record)
        if score is None:
            withheld_notes.append(note)
            continue
        record["score"], record["note"] = score, note
        by_node.setdefault(record["node_key"], []).append(record)

    for node_key in sorted(by_node):
        qualifying = by_node[node_key]
        # Highest score first; ETA then route id break ties so the same
        # telemetry always yields the same decision.
        qualifying.sort(
            key=lambda item: (
                -item["score"],
                item["landed_eta_sec"]
                if item["landed_eta_sec"] is not None
                else float("inf"),
                item["route_id"],
            )
        )
        for rank, record in enumerate(qualifying):
            if rank < max_tsp_grants_per_node:
                tsp_routes.add(record["route_id"])
                granted_notes.append(record["note"])
            else:
                withheld_notes.append(
                    f"TSP {record['route_id']}@{node_key} node cap "
                    f"{max_tsp_grants_per_node}"
                )

    for route_id in sorted(dbl_routes):
        granted_notes.append(f"DBL {route_id}")

    if granted_notes:
        reason = f"{label}: " + "; ".join(granted_notes)
    else:
        reason = f"{label}: no grant"
    if withheld_notes:
        reason += " | withheld: " + "; ".join(withheld_notes)

    return {
        "tsp": [route_id in tsp_routes for route_id in guard.ROUTE_ORDER],
        "dbl": [route_id in dbl_routes for route_id in guard.ROUTE_ORDER],
        "reason": reason[: guard.MAX_REASON_LEN],
    }


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

    def score(record):
        where = f"{record['route_id']}@{record['node_key']}"
        if not record["would_stop"]:
            return None, f"TSP {where} arrives on green"
        if record["cross_pax"] >= cross_queue_threshold_pax:
            return None, (
                f"TSP {where} cross {record['cross_pax']}>={cross_queue_threshold_pax}"
            )
        return record["passengers"] - record["cross_pax"], (
            f"TSP {where} {record['passengers']}pax vs cross "
            f"{record['cross_pax']}<{cross_queue_threshold_pax}"
        )

    return _decide(telemetry, decision_lag_sec, "rule", score, max_tsp_grants_per_node)


# --- max-pressure comparator ---------------------------------------------------
#
# This passenger-pressure comparator gates bus TSP; it does not select the
# network's phases. The arms here differ only in who sets the TSP/DBL flags -- Webster and the
# SignalController stay the mechanism -- so max-pressure is applied as the
# TSP gate: the bus approach is served when its passenger pressure beats the
# pressure of the cross street a grant would hold. A movement whose
# downstream is blocked carries no pressure (serving it releases nothing),
# which is the spillback term of the original.
#
# ponytail: downstream is a blocked/not-blocked veto from telemetry rather
# than a pax-weighted downstream queue; use downstream_space_m if the arm
# needs a graded downstream term.


def _node_state(telemetry, node_key):
    nodes = (telemetry.get("signal_state") or {}).get("nodes") or {}
    node = nodes.get(str(node_key)) if isinstance(nodes, dict) else None
    return node if isinstance(node, dict) else {}


def approach_pressure(telemetry, node_key, approach, extra_pax=0):
    """Queued passengers upstream of ``approach`` plus ``extra_pax``, or 0
    when its downstream is blocked."""
    blocked = _node_state(telemetry, node_key).get("downstream_blocked") or {}
    if isinstance(blocked, dict) and blocked.get(str(approach)):
        return 0
    positions = _node_positions(telemetry)
    waiting = _node_queue_passengers(
        telemetry, node_key, positions.get(str(node_key), 0)
    )
    return waiting.get(str(approach), 0) + int(extra_pax)


def cross_street_pressure(telemetry, node_key, approach):
    return sum(
        approach_pressure(telemetry, node_key, name)
        for name in CONFLICTING_APPROACHES.get(str(approach), ())
    )


def max_pressure_decision(
    telemetry, decision_lag_sec=None, max_tsp_grants_per_node=MAX_TSP_GRANTS_PER_NODE
):
    """Grant TSP where the bus approach's pressure exceeds the cross street's."""
    if not isinstance(telemetry, dict):
        telemetry = {}

    def score(record):
        where = f"{record['route_id']}@{record['node_key']}"
        own = approach_pressure(
            telemetry, record["node_key"], record["approach"], record["passengers"]
        )
        cross = cross_street_pressure(telemetry, record["node_key"], record["approach"])
        pressure = own - cross
        if pressure <= 0:
            return None, f"TSP {where} pressure {own}-{cross}<=0"
        return pressure, f"TSP {where} pressure {own}-{cross}={pressure}"

    return _decide(
        telemetry, decision_lag_sec, control_panel.MAX_PRESSURE_MODEL, score, max_tsp_grants_per_node
    )
