"""Separate-process LangGraph/Ollama controller for route-level TSP and DBL."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import TypedDict

import guard

try:
    import ollama
except ImportError:  # The simulator still starts and the agent fails all-off.
    ollama = None

try:
    from langgraph.graph import END, StateGraph
except ImportError:  # Reported as an all-off dependency failure at runtime.
    END = None
    StateGraph = None


BASE_DIR = Path(__file__).resolve().parent
TELEMETRY_PATH = BASE_DIR / "traffic_state_telemetry.json"
AI_CONTROL_PATH = BASE_DIR / "ai_control.json"
DECISION_PATH = BASE_DIR / "decision.json"
VERBOSE_LOG = True
TURN_LOG_PATH = BASE_DIR / "agent_turn_log.jsonl"
STALE_SECONDS = 3.0
RECENT_DECISION_LIMIT = 5
DEFAULT_CONTROL = {"armed": False, "model": "None", "tick_seconds": 5}

OUTPUT_SCHEMA = json.dumps({"flags": guard.all_off_flags()}, indent=2)
SYSTEM_PROMPT = f"""You control TSP and DBL flags for one traffic network.
Your objective is to maximize passengers_per_minute across the whole network.

TSP gives an approaching bus an early or extended green at its target node.
DBL enables the dynamic bus lane for that route. A 45-passenger bus can justify
priority, but unnecessary priority delays cross traffic. Use
queues_passengers_est to account for that tradeoff.

Return EXACTLY one JSON object matching this literal schema, with all six route
IDs and strict JSON booleans. Do not add markdown, analysis, or extra keys:
{OUTPUT_SCHEMA}

If a route has no approaching bus, set both tsp and dbl to false. If a route is
marked GRANTED, preserve its current tsp and dbl values and do not change it.
"""


class AgentState(TypedDict):
    telemetry: dict
    minimap: str
    locked_routes: set
    raw_output: str
    decision: dict
    status: str
    recent_decisions: list
    turn: int
    model: str


def atomic_write_json(path: Path, payload: dict) -> None:
    """Durably replace one JSON object without exposing a partial document."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            json.dump(payload, temp_file, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_name = Path(temp_file.name)
        os.replace(temp_name, path)
    except Exception:
        if temp_name and temp_name.exists():
            try:
                temp_name.unlink()
            except OSError:
                pass
        raise


def log_turn(state: AgentState, decision: dict) -> None:
    """Append one complete, unfiltered agent-turn record without raising."""
    if not VERBOSE_LOG:
        return
    try:
        locked_routes = state.get("locked_routes", [])
        if not isinstance(locked_routes, (list, tuple, set, frozenset)):
            locked_routes = []
        record = {
            "turn": decision.get("turn"),
            "timestamp": decision.get("timestamp"),
            "model": decision.get("model"),
            "status": decision.get("status"),
            "minimap": state.get("minimap", ""),
            "raw_output": state.get("raw_output", ""),
            "flags": decision.get("flags", {}),
            "locked_routes": sorted(locked_routes),
            "stale": state.get("status") in ("STALE", "HELD"),
        }
        with TURN_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def read_ai_control(path: Path = AI_CONTROL_PATH) -> dict:
    """Read the panel mirror; any failure is equivalent to disarmed."""
    try:
        with Path(path).open("r", encoding="utf-8") as control_file:
            payload = json.load(control_file)
        if not isinstance(payload, dict):
            return dict(DEFAULT_CONTROL)
        return {
            "armed": payload.get("armed") is True,
            "model": str(payload.get("model", "None")),
            "tick_seconds": min(
                15,
                max(2, int(payload.get("tick_seconds", 5))),
            ),
        }
    except Exception:
        return dict(DEFAULT_CONTROL)


def _read_telemetry(path: Path = TELEMETRY_PATH) -> dict | None:
    try:
        with Path(path).open("r", encoding="utf-8") as telemetry_file:
            telemetry = json.load(telemetry_file)
        return telemetry if isinstance(telemetry, dict) else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _finite_nonnegative(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if number < 0 or number != number or number == float("inf"):
        return default
    return number


def actionable_buses(telemetry: dict) -> dict[str, list[dict]]:
    """Return only buses still approaching a real unfinished route leg."""
    by_route = {route_id: [] for route_id in guard.VALID_ROUTES}
    for bus in telemetry.get("active_buses", []):
        if not isinstance(bus, dict):
            continue
        route_id = bus.get("route_id")
        if route_id not in by_route:
            continue
        if not isinstance(bus.get("route_leg"), dict):
            continue
        if bus.get("leg_state") != "APPROACHING":
            continue
        distance = _finite_nonnegative(bus.get("distance_to_stop_bar_px"))
        eta = _finite_nonnegative(bus.get("eta_to_stop_bar_sec_freeflow"))
        if distance is None or eta is None:
            continue
        by_route[route_id].append(bus)
    for buses in by_route.values():
        buses.sort(key=lambda bus: float(bus["eta_to_stop_bar_sec_freeflow"]))
    return by_route


def load_save(state: AgentState) -> dict:
    telemetry = _read_telemetry()
    if telemetry is None:
        return {"telemetry": {}, "status": "STALE"}
    if telemetry.get("simulation_paused", False):
        return {"telemetry": telemetry, "status": "STALE"}
    try:
        age = time.time() - float(telemetry["timestamp"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"telemetry": telemetry, "status": "STALE"}
    if age > STALE_SECONDS:
        return {"telemetry": telemetry, "status": "STALE"}
    return {"telemetry": telemetry, "status": "OK"}


def _route_after_load(state: AgentState) -> str:
    return "hold" if state.get("status") == "STALE" else "continue"


def read_minimap(state: AgentState) -> dict:
    telemetry = state.get("telemetry", {})
    routes = telemetry.get("routes", {})
    throughput = telemetry.get("network_throughput", {})
    summary = telemetry.get("network_summary", {})
    nodes = telemetry.get("signal_state", {}).get("nodes", {})
    approaching = actionable_buses(telemetry)

    lines = [
        f"simulation_time_seconds={telemetry.get('simulation_time_seconds', 0)}",
        "passengers_per_minute="
        f"{throughput.get('passengers_per_minute', 0)}",
        "passengers_per_minute_recent="
        f"{throughput.get('passengers_per_minute_recent', 0)}",
        "queues_passengers_est="
        f"{json.dumps(summary.get('queues_passengers_est', {}), sort_keys=True)}",
        "NODES:",
    ]
    for node_x, node in sorted(nodes.items(), key=lambda item: str(item[0])):
        if not isinstance(node, dict):
            continue
        lines.append(
            f"- node={node_x} phase={node.get('phase', 'UNKNOWN')} "
            f"signals={json.dumps(node.get('signals', {}), sort_keys=True)}"
        )

    lines.append("ROUTES:")
    for route_id in sorted(guard.VALID_ROUTES):
        route = routes.get(route_id, {}) if isinstance(routes, dict) else {}
        candidates = approaching.get(route_id, [])
        nearest = candidates[0] if candidates else None
        if nearest:
            route_leg = nearest.get("route_leg", {})
            granted = bool(nearest.get("priority_granted", False))
            lines.append(
                f"- {route_id}: active={bool(route.get('active', False))} "
                f"tsp={bool(route.get('tsp_enabled', False))} "
                f"dbl={bool(route.get('dbl_enabled', False))} "
                f"approaching_buses={len(candidates)} "
                f"nearest_eta_sec={nearest.get('eta_to_stop_bar_sec_freeflow')} "
                f"target_node={route_leg.get('node_x')} "
                f"passengers={int(nearest.get('passengers', 0))} "
                f"priority={'GRANTED - do not change' if granted else 'not granted'}"
            )
        else:
            lines.append(
                f"- {route_id}: active={bool(route.get('active', False))} "
                f"tsp={bool(route.get('tsp_enabled', False))} "
                f"dbl={bool(route.get('dbl_enabled', False))} "
                "approaching_buses=0 none approaching"
            )

    recent = state.get("recent_decisions", [])[-RECENT_DECISION_LIMIT:]
    if recent:
        compact_recent = [
            {
                "turn": decision.get("turn"),
                "status": decision.get("status"),
                "flags": decision.get("flags", {}),
            }
            for decision in recent
            if isinstance(decision, dict)
        ]
        lines.append("RECENT_DECISIONS=" + json.dumps(compact_recent, sort_keys=True))
    return {"minimap": "\n".join(lines)}


def check_locked(state: AgentState) -> dict:
    approaching = actionable_buses(state.get("telemetry", {}))
    locked = {
        route_id
        for route_id, buses in approaching.items()
        if buses and bool(buses[0].get("priority_granted", False))
    }
    minimap = state.get("minimap", "")
    if locked:
        minimap += "\nLOCKED_ROUTES=" + ",".join(sorted(locked))
    return {"locked_routes": locked, "minimap": minimap}


def ai_turn(state: AgentState) -> dict:
    model = state.get("model", "None")
    try:
        if ollama is None:
            raise RuntimeError("ollama Python package is not installed")
        if not model or model == "None":
            raise RuntimeError("no Ollama model selected")
        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": state.get("minimap", "")},
            ],
            options={"temperature": 0.2},
        )
        raw_output = response["message"]["content"]
        if not isinstance(raw_output, str):
            raise TypeError("Ollama response content is not text")
        return {"raw_output": raw_output, "status": "OK"}
    except Exception as exc:
        return {
            "raw_output": f"Agent error: {type(exc).__name__}: {str(exc)[:500]}",
            "status": "INVALID",
        }


def anti_cheat(state: AgentState) -> dict:
    decision = guard.safe_decision(
        state.get("raw_output", ""),
        state.get("turn", 0),
        state.get("model", "None"),
    )
    routes = state.get("telemetry", {}).get("routes", {})
    if decision["status"] == "OK":
        for route_id in state.get("locked_routes", set()):
            current = routes.get(route_id, {}) if isinstance(routes, dict) else {}
            decision["flags"][route_id] = {
                "tsp": bool(current.get("tsp_enabled", False)),
                "dbl": bool(current.get("dbl_enabled", False)),
            }
    return {"decision": decision, "status": decision["status"]}


def _remember_decision(state: AgentState, decision: dict) -> list:
    recent = list(state.get("recent_decisions", []))
    recent.append(decision)
    return recent[-RECENT_DECISION_LIMIT:]


def write_decision(state: AgentState) -> dict:
    decision = state["decision"]
    atomic_write_json(DECISION_PATH, decision)
    log_turn(state, decision)
    return {"recent_decisions": _remember_decision(state, decision)}


def hold(state: AgentState) -> dict:
    # Locked design choice: stale or missing telemetry cannot authorize priority.
    decision = {
        "schema_version": 1,
        "turn": int(state.get("turn", 0)),
        "timestamp": round(time.time(), 3),
        "model": str(state.get("model", "None")),
        "status": "HELD_ALL_OFF",
        "flags": guard.all_off_flags(),
    }
    atomic_write_json(DECISION_PATH, decision)
    log_turn(state, decision)
    return {
        "decision": decision,
        "status": "STALE",
        "recent_decisions": _remember_decision(state, decision),
    }


def build_graph():
    if StateGraph is None or END is None:
        raise RuntimeError("langgraph Python package is not installed")
    workflow = StateGraph(AgentState)
    workflow.add_node("load_save", load_save)
    workflow.add_node("read_minimap", read_minimap)
    workflow.add_node("check_locked", check_locked)
    workflow.add_node("ai_turn", ai_turn)
    workflow.add_node("anti_cheat", anti_cheat)
    workflow.add_node("write_decision", write_decision)
    workflow.add_node("hold", hold)
    workflow.set_entry_point("load_save")
    workflow.add_conditional_edges(
        "load_save",
        _route_after_load,
        {"continue": "read_minimap", "hold": "hold"},
    )
    workflow.add_edge("read_minimap", "check_locked")
    workflow.add_edge("check_locked", "ai_turn")
    workflow.add_edge("ai_turn", "anti_cheat")
    workflow.add_edge("anti_cheat", "write_decision")
    workflow.add_edge("write_decision", END)
    workflow.add_edge("hold", END)
    return workflow.compile()


def _dependency_hold(turn: int, model: str, message: str) -> dict:
    decision = guard.safe_decision(message, turn, model)
    atomic_write_json(DECISION_PATH, decision)
    log_turn(
        {
            "minimap": "",
            "raw_output": message,
            "locked_routes": set(),
            "status": "HELD",
        },
        decision,
    )
    return decision


def run_forever() -> None:
    try:
        graph = build_graph()
        graph_error = None
    except Exception as exc:
        graph = None
        graph_error = f"Agent dependency error: {type(exc).__name__}: {exc}"

    turn = 0
    recent_decisions = []
    while True:
        control = read_ai_control()
        if not control["armed"]:
            time.sleep(1.0)
            continue

        turn += 1
        model = control["model"]
        try:
            if graph is None:
                decision = _dependency_hold(turn, model, graph_error or "Agent unavailable")
                recent_decisions = (recent_decisions + [decision])[-RECENT_DECISION_LIMIT:]
            else:
                result = graph.invoke(
                    {
                        "telemetry": {},
                        "minimap": "",
                        "locked_routes": set(),
                        "raw_output": "",
                        "decision": {},
                        "status": "OK",
                        "recent_decisions": recent_decisions,
                        "turn": turn,
                        "model": model,
                    }
                )
                recent_decisions = result.get("recent_decisions", recent_decisions)
        except Exception as exc:
            decision = _dependency_hold(
                turn,
                model,
                f"Agent turn error: {type(exc).__name__}: {str(exc)[:500]}",
            )
            recent_decisions = (recent_decisions + [decision])[-RECENT_DECISION_LIMIT:]
        time.sleep(control["tick_seconds"])


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        pass
