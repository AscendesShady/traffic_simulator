"""Separate-process LangGraph/Ollama controller for route-level TSP and DBL."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from typing import TypedDict

import guard

try:
    import ollama
except ImportError:  # The simulator still starts and the agent fails all-off.
    ollama = None

try:
    from google import genai as _genai
    from google.genai import types as _genai_types
except ImportError:  # Gemini remains unavailable without the optional SDK.
    _genai = None
    _genai_types = None

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
GEMINI_TIMEOUT_SECONDS = 30.0
OLLAMA_TIMEOUT_SECONDS = 45.0
_GEMINI_CLIENT = None
_GEMINI_CALL_LOCK = threading.Lock()
_OLLAMA_CLIENT = None
_OLLAMA_CALL_LOCK = threading.Lock()

OUTPUT_SCHEMA = json.dumps(
    {
        "reason": (
            "<one sentence: why these flags maximize passenger throughput "
            "this turn>"
        ),
        "tsp": [False for _ in guard.ROUTE_ORDER],
        "dbl": [False for _ in guard.ROUTE_ORDER],
    },
    indent=2,
)
OLLAMA_OUTPUT_FORMAT = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "tsp": {
            "type": "array",
            "items": {"type": "boolean"},
            "minItems": len(guard.ROUTE_ORDER),
            "maxItems": len(guard.ROUTE_ORDER),
        },
        "dbl": {
            "type": "array",
            "items": {"type": "boolean"},
            "minItems": len(guard.ROUTE_ORDER),
            "maxItems": len(guard.ROUTE_ORDER),
        },
    },
    "required": ["reason", "tsp", "dbl"],
    "additionalProperties": False,
}
ROUTE_ORDER_TEXT = "\n".join(
    f"{position}) {route_id}"
    for position, route_id in enumerate(guard.ROUTE_ORDER, start=1)
)
SYSTEM_PROMPT = f"""You control TSP and DBL flags for one traffic network.
Your objective is to maximize passengers_per_minute across the whole network.

TSP gives an approaching bus an early or extended green at its target node.
DBL enables the dynamic bus lane for that route. A 45-passenger bus can justify
priority, but unnecessary priority delays cross traffic. Use
queues_passengers_est to account for that tradeoff.

The route positions are fixed in this exact order:
{ROUTE_ORDER_TEXT}

Return EXACTLY one JSON object matching this literal schema. The first boolean
in each array controls route position 1, the second controls position 2, and so
on. Never write route IDs as JSON keys. Use strict JSON booleans and do not add
markdown, analysis, or extra keys:
{OUTPUT_SCHEMA}

"reason" must be one sentence under about 40 words stating the main throughput
justification for this turn's flag choices. Keep it on one line, with no line
breaks and no quotation marks inside it if avoidable. The "tsp" and "dbl"
arrays matter most: each must contain exactly {len(guard.ROUTE_ORDER)} booleans.

If a route has no approaching bus, set both tsp and dbl to false. If a route is
marked GRANTED, preserve its current tsp and dbl values and do not change it.
"""


class AgentState(TypedDict):
    telemetry: dict
    discharge_active: bool
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
        telemetry = state.get("telemetry", {})
        if not isinstance(telemetry, dict):
            telemetry = {}
        throughput = telemetry.get("network_throughput", {})
        if not isinstance(throughput, dict):
            throughput = {}
        record = {
            "turn": decision.get("turn"),
            "timestamp": decision.get("timestamp"),
            "model": decision.get("model"),
            "status": decision.get("status"),
            "minimap": state.get("minimap", ""),
            "raw_output": state.get("raw_output", ""),
            "flags": decision.get("flags", {}),
            "reason": decision.get("reason", ""),
            "pax_per_min_recent": throughput.get(
                "passengers_per_minute_recent"
            ),
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


def check_discharge(state: AgentState) -> dict:
    """Stand down with fresh all-off flags while recovery owns the network."""
    telemetry = state.get("telemetry", {})
    if not isinstance(telemetry, dict):
        telemetry = {}
    discharge = telemetry.get("network_discharge", {})
    if not isinstance(discharge, dict):
        discharge = {}
    if bool(discharge.get("active", False)):
        controller_state = discharge.get("controller_state", "UNKNOWN")
        decision = {
            "schema_version": 1,
            "turn": int(state.get("turn", 0)),
            "timestamp": time.time(),
            "model": str(state.get("model", "None")),
            "status": "STANDDOWN_DISCHARGE",
            "reason": (
                f"Discharge active ({controller_state}); agent standing down."
            ),
            "flags": guard.all_off_flags(),
        }
        return {
            "decision": decision,
            "status": "STANDDOWN",
            "raw_output": "",
            "discharge_active": True,
        }
    return {"discharge_active": False}


def _route_after_discharge(state: AgentState) -> str:
    return "standdown" if state.get("discharge_active") else "continue"


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
    for route_position, route_id in enumerate(guard.ROUTE_ORDER, start=1):
        route = routes.get(route_id, {}) if isinstance(routes, dict) else {}
        candidates = approaching.get(route_id, [])
        nearest = candidates[0] if candidates else None
        if nearest:
            route_leg = nearest.get("route_leg", {})
            granted = bool(nearest.get("priority_granted", False))
            lines.append(
                f"{route_position}) {route_id}: "
                f"active={bool(route.get('active', False))} "
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
                f"{route_position}) {route_id}: "
                f"active={bool(route.get('active', False))} "
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
    telemetry = state.get("telemetry", {})
    if not isinstance(telemetry, dict):
        telemetry = {}
    locked = set()
    active_buses = telemetry.get("active_buses", [])
    if not isinstance(active_buses, list):
        active_buses = []
    for bus in active_buses:
        if not isinstance(bus, dict):
            continue
        route_id = bus.get("route_id")
        if route_id not in guard.VALID_ROUTES:
            continue
        # Vehicle leg_state describes geometry (APPROACHING,
        # IN_INTERSECTION, TURNING, ...), while these two telemetry booleans
        # expose the controller's grant lifecycle. Keep a route locked through
        # both the active-green and clearing portions of an accepted grant.
        if bool(bus.get("priority_granted", False)) or bool(
            bus.get("priority_clearing", False)
        ):
            locked.add(route_id)
    minimap = state.get("minimap", "")
    if locked:
        minimap += "\nLOCKED_ROUTES=" + ",".join(sorted(locked))
    return {"locked_routes": locked, "minimap": minimap}


def _is_gemini(model: str) -> bool:
    return isinstance(model, str) and model.startswith("gemini-")


def _get_gemini_client():
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        if _genai is None:
            raise RuntimeError("google-genai package not installed")
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        _GEMINI_CLIENT = _genai.Client(api_key=api_key)
    return _GEMINI_CLIENT


def _call_gemini(model: str, system_prompt: str, minimap: str) -> str:
    """Call Gemini with low temperature and a hard, non-overlapping timeout."""
    client = _get_gemini_client()
    if _genai_types is None:
        raise RuntimeError("google-genai package not installed")
    call_lock = _GEMINI_CALL_LOCK
    if not call_lock.acquire(blocking=False):
        raise RuntimeError("previous Gemini request is still running")

    result_queue = queue.Queue(maxsize=1)
    prompt = system_prompt + "\n\n" + minimap

    def request():
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=_genai_types.GenerateContentConfig(temperature=0.2),
            )
            outcome = (True, response)
        except Exception as exc:
            outcome = (False, exc)
        finally:
            call_lock.release()
        result_queue.put(outcome)

    request_thread = threading.Thread(
        target=request,
        name="gemini-agent-request",
        daemon=True,
    )
    try:
        request_thread.start()
    except Exception:
        call_lock.release()
        raise

    try:
        succeeded, value = result_queue.get(timeout=GEMINI_TIMEOUT_SECONDS)
    except queue.Empty as exc:
        raise TimeoutError(
            f"Gemini request exceeded {GEMINI_TIMEOUT_SECONDS:g}s timeout"
        ) from exc
    if not succeeded:
        raise value
    text = getattr(value, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("empty Gemini response")
    return text


def _ollama_format_is_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    return "format" in message and any(
        marker in message
        for marker in (
            "unexpected keyword",
            "unexpected argument",
            "unknown field",
            "unknown parameter",
            "not supported",
            "unsupported",
        )
    )


def _get_ollama_client():
    """Return an Ollama client with a native HTTP request timeout when available."""
    global _OLLAMA_CLIENT
    if ollama is None:
        raise RuntimeError("ollama Python package is not installed")
    if _OLLAMA_CLIENT is None:
        client_type = getattr(ollama, "Client", None)
        if client_type is None:
            # Supports lightweight test doubles and older clients; the outer
            # thread deadline below remains authoritative.
            return ollama
        _OLLAMA_CLIENT = client_type(timeout=OLLAMA_TIMEOUT_SECONDS)
    return _OLLAMA_CLIENT


def _call_ollama(model: str, minimap: str) -> str:
    client = _get_ollama_client()
    call_lock = _OLLAMA_CALL_LOCK
    if not call_lock.acquire(blocking=False):
        raise RuntimeError("previous Ollama request is still running")

    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": minimap},
        ],
        "options": {"temperature": 0.2},
    }

    result_queue = queue.Queue(maxsize=1)

    def request_ollama():
        try:
            try:
                response = client.chat(format=OLLAMA_OUTPUT_FORMAT, **request)
            except Exception as exc:
                if not _ollama_format_is_unsupported(exc):
                    raise
                response = client.chat(**request)
            outcome = (True, response)
        except Exception as exc:
            outcome = (False, exc)
        finally:
            call_lock.release()
        result_queue.put(outcome)

    request_thread = threading.Thread(
        target=request_ollama,
        name="ollama-agent-request",
        daemon=True,
    )
    try:
        request_thread.start()
    except Exception:
        call_lock.release()
        raise

    try:
        succeeded, value = result_queue.get(timeout=OLLAMA_TIMEOUT_SECONDS)
    except queue.Empty as exc:
        # A thread deadline cannot kill the underlying HTTP/server inference;
        # the daemon and call lock let the agent continue without overlapping it.
        raise TimeoutError(
            f"Ollama request exceeded {OLLAMA_TIMEOUT_SECONDS:g}s timeout"
        ) from exc
    if not succeeded:
        raise value
    response = value
    return response["message"]["content"]


def ai_turn(state: AgentState) -> dict:
    model = state.get("model", "None")
    try:
        if not model or model == "None":
            raise RuntimeError("no model selected")
        if _is_gemini(model):
            raw_output = _call_gemini(
                model,
                SYSTEM_PROMPT,
                state.get("minimap", ""),
            )
        else:
            raw_output = _call_ollama(model, state.get("minimap", ""))
        if not isinstance(raw_output, str):
            raise TypeError("model response content is not text")
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
        "reason": "",
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
    workflow.add_node("check_discharge", check_discharge)
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
        {"continue": "check_discharge", "hold": "hold"},
    )
    workflow.add_conditional_edges(
        "check_discharge",
        _route_after_discharge,
        {"continue": "read_minimap", "standdown": "write_decision"},
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
                        "discharge_active": False,
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
