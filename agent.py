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
DEFAULT_CONTROL = {
    "armed": False,
    "model": "None",
    "tick_seconds": 5,
    "simulation_running": False,
}
FRAME_RESET_MARGIN = 100
# A decision does not land instantly: the model needs seconds to answer, and
# traffic keeps moving meanwhile. The previous turn's measured latency is the
# best available predictor of this turn's, since a given model is consistent
# turn to turn.
DEFAULT_DECISION_LAG_SEC = 8.0
# Beyond this many seconds a bus is too far out for a flag decided now to be
# the right call; it will be reconsidered on a later turn.
ACTIONABLE_HORIZON_SEC = 45.0
GEMINI_TIMEOUT_SECONDS = 30.0
OLLAMA_TIMEOUT_SECONDS = 45.0
_GEMINI_CLIENT = None
_GEMINI_CALL_LOCK = threading.Lock()
_OLLAMA_CLIENT = None
_OLLAMA_CALL_LOCK = threading.Lock()

OUTPUT_SCHEMA = json.dumps(
    {
        "reason": (
            "<one sentence: why this turn's grants are worth the "
            "cross-traffic delay they cause, or why none was justified>"
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
SYSTEM_PROMPT = f"""The signals are already running Webster-optimal timing,
which handles normal traffic well. Your job is NOT to take over. Make
occasional, surgical priority grants ONLY when a bus clearly carries enough
passengers to justify the delay it imposes on cross-traffic. Most turns, the
right answer is little or no priority. You are a light touch on top of a
competent baseline, not the primary controller.

CONGESTION IS FAILURE. The worst outcome is a network filling with stopped
vehicles. Watch total waiting passengers and vehicles across the network. If
you grant priority and the network gets MORE congested, you made it worse.
Every priority grant to a bus takes green from cross-traffic and risks backing
it up. A grant is only correct if the passengers it serves clearly outweigh
the cross-traffic passengers it delays.

DEFAULT TO RESTRAINT. When you are not confident a grant clearly improves
passenger flow, grant nothing. A turn with all flags false is a good, safe
decision whenever no bus decisively outweighs its cross-traffic cost. Granting
priority to every approaching bus is WRONG: it starves cross-traffic and
congests the network. Grant priority only to the single most valuable
bus-approach when its benefit is clear, and often to none.

Do not treat routes independently. Each node is a shared resource, and giving
one route priority takes green from its cross-traffic. Compare directly: a bus
carries about 45 passengers. Cross-traffic on the approach you would red
carries queues_passengers_est passengers. Only grant priority if the bus's 45
passengers clearly exceed the cross-traffic passengers you would delay. If the
cross-traffic queue is already large, do NOT add priority: you would deepen a
queue already costing more passenger-time than the bus saves.

Prefer granting priority to AT MOST one or two approaches per node per turn.
Blanket priority across many routes at once congests the whole network. This is
the most common mistake. Fewer, well-justified grants beat many eager ones.

TSP gives an approaching bus an early or extended green at its target node.
DBL enables the dynamic bus lane for that route.

The route positions are fixed in this exact order:
{ROUTE_ORDER_TEXT}

Return EXACTLY one JSON object matching this literal schema. The first boolean
in each array controls route position 1, the second controls position 2, and so
on. Never write route IDs as JSON keys. Use strict JSON booleans and do not add
markdown, analysis, or extra keys:
{OUTPUT_SCHEMA}

"reason" must be one sentence under about 40 words weighing the passengers
served against the cross-traffic delayed by this turn's flag choices. Keep it on one line, with no line
breaks and no quotation marks inside it if avoidable. The "tsp" and "dbl"
arrays matter most: each must contain exactly {len(guard.ROUTE_ORDER)} booleans.

DECIDE EACH TURN FROM THE CURRENT STATE ONLY. Do not carry flags forward from
previous turns. For every route, look at its approaching_buses count in the
minimap THIS turn:

- If a route shows "approaching_buses=0" or "none approaching", you MUST set
  BOTH its tsp and dbl to false, even if it had priority before. A route with no
  approaching bus gains nothing from priority and only delays cross traffic.
- Only set tsp or dbl true for a route that has an approaching bus this turn
  AND where priority improves passenger throughput.

Your decision does not take effect instantly. DECISION_LAG_SEC tells you how
many seconds pass between this snapshot and when your flags apply. During that
time buses keep moving. For each route, eta_at_decision_land_sec is where the
bus will be when your decision actually takes effect:

- If eta_at_decision_land_sec <= 0 the bus will already be at or past the node
  before your flag applies, so enabling priority for it is WASTED: the green
  fires for empty space. Set it false.
- Prefer routes showing actionable=true: that bus arrives AFTER your decision
  lands and soon enough to benefit from it.
- You are aiming your decision at the near future, not the present. Think about
  where traffic will BE, not where it IS.

DBL only helps if the bus can actually enter the dynamic bus lane. If a route
shows dbl_lane_obstructed=true, do NOT enable dbl for that route: the bus
cannot merge and enabling it wastes the lane. Prefer tsp, which needs no lane
change, for a bus whose dbl lane is blocked. A route showing dbl=true with
nearest_bus_in_dbl_lane=false is evidence the dbl you enabled is not working.

Example: if only route 1 has an approaching bus, the correct output is
tsp=[true,false,false,false,false,false] and
dbl=[false,false,false,false,false,false]. Every other position is false
because those routes have no bus.
"""


class AgentState(TypedDict):
    telemetry: dict
    discharge_active: bool
    minimap: str
    locked_routes: set
    raw_output: str
    call_metrics: dict
    decision: dict
    status: str
    recent_decisions: list
    turn: int
    model: str
    decision_lag_sec: float


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
        call_metrics = state.get("call_metrics", {}) or {}
        if not isinstance(call_metrics, dict):
            call_metrics = {}
        output_tokens = call_metrics.get("output_tokens")
        eval_duration_ns = call_metrics.get("eval_duration_ns")
        tokens_per_sec = None
        numeric_output = isinstance(output_tokens, (int, float)) and not isinstance(
            output_tokens, bool
        )
        numeric_duration = isinstance(
            eval_duration_ns, (int, float)
        ) and not isinstance(eval_duration_ns, bool)
        if numeric_output and numeric_duration and eval_duration_ns > 0:
            tokens_per_sec = round(
                output_tokens / (eval_duration_ns / 1_000_000_000.0), 2
            )
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
            "latency_ms": call_metrics.get("latency_ms"),
            "input_tokens": call_metrics.get("input_tokens"),
            "output_tokens": output_tokens,
            "tokens_per_sec": tokens_per_sec,
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
            "simulation_running": payload.get("simulation_running") is True,
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


def _telemetry_frame_number(telemetry):
    if not isinstance(telemetry, dict):
        return None
    value = telemetry.get("frame_number")
    if isinstance(value, bool):
        return None
    try:
        frame_number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return frame_number if frame_number >= 0 else None


def _track_run_boundary(turn, recent_decisions, last_frame, telemetry):
    """Clear per-run agent state when telemetry jumps back to frame zero."""
    current_frame = _telemetry_frame_number(telemetry)
    reset_detected = bool(
        current_frame is not None
        and last_frame is not None
        and (
            current_frame < last_frame - FRAME_RESET_MARGIN
            or (current_frame == 0 and last_frame > 0)
        )
    )
    if reset_detected:
        turn = 0
        recent_decisions = []
    if current_frame is not None:
        last_frame = current_frame
    return turn, recent_decisions, last_frame, reset_detected


def _finite_nonnegative(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if number < 0 or number != number or number == float("inf"):
        return default
    return number


def decision_lag_seconds(latency_ms) -> float:
    """Seconds a decision is expected to take, from the last measured call.

    Falls back to DEFAULT_DECISION_LAG_SEC on the first turn or whenever the
    previous call reported no usable latency, so the loop always has a horizon.
    """
    seconds = _finite_nonnegative(latency_ms)
    if seconds is None or seconds <= 0:
        return DEFAULT_DECISION_LAG_SEC
    return round(float(seconds) / 1000.0, 1)


def eta_at_decision_land(eta_sec, decision_lag_sec):
    """Where a bus will be, in seconds from the stop bar, when flags apply."""
    eta = _finite_nonnegative(eta_sec)
    if eta is None:
        return None
    return round(float(eta) - float(decision_lag_sec), 1)


def is_actionable(landed_eta_sec) -> bool:
    """True when the bus arrives after the decision lands and soon enough."""
    if landed_eta_sec is None:
        return False
    return 0 < landed_eta_sec < ACTIONABLE_HORIZON_SEC


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

    decision_lag_sec = state.get("decision_lag_sec")
    if _finite_nonnegative(decision_lag_sec) is None:
        decision_lag_sec = DEFAULT_DECISION_LAG_SEC

    actionable_by_node = {}
    for route_buses in approaching.values():
        for bus in route_buses:
            leg = bus.get("route_leg", {})
            landed_eta = eta_at_decision_land(
                bus.get("eta_to_stop_bar_sec_freeflow"), decision_lag_sec
            )
            if not is_actionable(landed_eta):
                continue
            node_key = str(leg.get("node_x"))
            approach = str(leg.get("approach") or bus.get("direction", "?"))
            bucket = actionable_by_node.setdefault(node_key, {}).setdefault(
                approach, {"buses": 0, "passengers": 0}
            )
            bucket["buses"] += 1
            bucket["passengers"] += int(bus.get("passengers", 0))

    lines = [
        f"simulation_time_seconds={telemetry.get('simulation_time_seconds', 0)}",
        f"DECISION_LAG_SEC={decision_lag_sec}   # your decision will take "
        "effect ~this many seconds from this snapshot; buses will have moved "
        "by then",
        "passengers_per_minute="
        f"{throughput.get('passengers_per_minute', 0)}",
        "passengers_per_minute_recent="
        f"{throughput.get('passengers_per_minute_recent', 0)}",
        "queues_passengers_est="
        f"{json.dumps(summary.get('queues_passengers_est', {}), sort_keys=True)}",
        "NODE_SUMMARY:",
    ]
    sorted_nodes = sorted(nodes.items(), key=lambda item: str(item[0]))
    global_queue_pax = summary.get("queues_passengers_est", {})
    for node_position, (node_x, node) in enumerate(sorted_nodes):
        if not isinstance(node, dict):
            continue
        waiting = node.get("queues_passengers_est")
        if not isinstance(waiting, dict):
            # Compatibility for telemetry produced before per-node queues were
            # added. Current telemetry always takes the precise branch above.
            vertical_prefix = "A" if node_position == 0 else "B"
            waiting = {
                "EB": global_queue_pax.get("EB", 0),
                "WB": global_queue_pax.get("WB", 0),
                "NB": global_queue_pax.get(f"{vertical_prefix}_NB", 0),
                "SB": global_queue_pax.get(f"{vertical_prefix}_SB", 0),
            }
        waiting = {
            approach: int(_finite_nonnegative(waiting.get(approach), 0) or 0)
            for approach in ("EB", "WB", "NB", "SB")
        }
        signals = node.get("signals", {})
        signal_text = " ".join(
            f"{approach}={signals.get(approach, 'UNKNOWN')}"
            for approach in ("EB", "WB", "NB", "SB")
        )
        bus_groups = actionable_by_node.get(str(node_x), {})
        actionable_text = " ".join(
            f"{approach}({details['passengers']}pax/{details['buses']}bus)"
            for approach, details in sorted(bus_groups.items())
        ) or "none"
        lines.append(
            f"- NODE {node_x}: phase={node.get('phase', 'UNKNOWN')} "
            f"signals: {signal_text} "
            f"waiting_pax: EB={waiting['EB']} WB={waiting['WB']} "
            f"NB={waiting['NB']} SB={waiting['SB']} "
            f"(total={sum(waiting.values())}) "
            f"actionable_bus: {actionable_text} "
            "conflicts: EW(EB/WB) blocks NS(NB/SB); priority for one "
            "approach gives it exclusive GREEN and sets all others RED"
        )

    lines.append("ROUTES:")
    for route_position, route_id in enumerate(guard.ROUTE_ORDER, start=1):
        route = routes.get(route_id, {}) if isinstance(routes, dict) else {}
        candidates = approaching.get(route_id, [])
        nearest = candidates[0] if candidates else None
        if nearest:
            route_leg = nearest.get("route_leg", {})
            granted = bool(nearest.get("priority_granted", False))
            landed_eta = eta_at_decision_land(
                nearest.get("eta_to_stop_bar_sec_freeflow"), decision_lag_sec
            )
            lines.append(
                f"{route_position}) {route_id}: "
                f"active={bool(route.get('active', False))} "
                f"tsp={bool(route.get('tsp_enabled', False))} "
                f"dbl={bool(route.get('dbl_enabled', False))} "
                f"dbl_lane_obstructed="
                f"{bool(route.get('dbl_lane_obstructed', False))} "
                f"nearest_bus_in_dbl_lane="
                f"{bool(route.get('nearest_bus_in_dbl_lane', False))} "
                f"approaching_buses={len(candidates)} "
                f"nearest_eta_sec={nearest.get('eta_to_stop_bar_sec_freeflow')} "
                f"eta_at_decision_land_sec={landed_eta} "
                f"actionable={is_actionable(landed_eta)} "
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
                f"dbl_lane_obstructed="
                f"{bool(route.get('dbl_lane_obstructed', False))} "
                f"nearest_bus_in_dbl_lane="
                f"{bool(route.get('nearest_bus_in_dbl_lane', False))} "
                "approaching_buses=0 none approaching"
            )

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


def _call_gemini(
    model: str, system_prompt: str, minimap: str
) -> tuple[str, dict]:
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
    usage = getattr(value, "usage_metadata", None)
    metrics = {
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "eval_duration_ns": None,
        "total_duration_ns": None,
    }
    return text, metrics


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


def _call_ollama(model: str, minimap: str) -> tuple[str, dict]:
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
    content = response["message"]["content"]
    metrics = {
        "input_tokens": response.get("prompt_eval_count"),
        "output_tokens": response.get("eval_count"),
        "eval_duration_ns": response.get("eval_duration"),
        "total_duration_ns": response.get("total_duration"),
    }
    return content, metrics


def ai_turn(state: AgentState) -> dict:
    model = state.get("model", "None")
    started = time.time()
    try:
        if not model or model == "None":
            raise RuntimeError("no model selected")
        if _is_gemini(model):
            raw_output, call_metrics = _call_gemini(
                model,
                SYSTEM_PROMPT,
                state.get("minimap", ""),
            )
        else:
            raw_output, call_metrics = _call_ollama(
                model, state.get("minimap", "")
            )
        if not isinstance(raw_output, str):
            raise TypeError("model response content is not text")
        if not isinstance(call_metrics, dict):
            call_metrics = {}
        latency_ms = round((time.time() - started) * 1000.0, 1)
        return {
            "raw_output": raw_output,
            "status": "OK",
            "call_metrics": {
                **call_metrics,
                "latency_ms": latency_ms,
            },
        }
    except Exception as exc:
        latency_ms = round((time.time() - started) * 1000.0, 1)
        return {
            "raw_output": f"Agent error: {type(exc).__name__}: {str(exc)[:500]}",
            "status": "INVALID",
            "call_metrics": {
                "latency_ms": latency_ms,
                "input_tokens": None,
                "output_tokens": None,
                "eval_duration_ns": None,
                "total_duration_ns": None,
            },
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
    last_frame = None
    # First turn has no measurement yet, so start from the conservative default.
    decision_lag_sec = DEFAULT_DECISION_LAG_SEC
    while True:
        control = read_ai_control()
        telemetry = _read_telemetry()
        turn, recent_decisions, last_frame, _reset_detected = _track_run_boundary(
            turn, recent_decisions, last_frame, telemetry
        )
        telemetry_says_stopped = bool(
            isinstance(telemetry, dict)
            and telemetry.get("simulation_running") is False
        )
        if (
            not control["armed"]
            or not control["simulation_running"]
            or telemetry_says_stopped
        ):
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
                        "call_metrics": {},
                        "decision": {},
                        "status": "OK",
                        "recent_decisions": recent_decisions,
                        "turn": turn,
                        "model": model,
                        "decision_lag_sec": decision_lag_sec,
                    }
                )
                recent_decisions = result.get("recent_decisions", recent_decisions)
                # This turn's measured latency predicts the next turn's lag.
                measured = result.get("call_metrics", {})
                if isinstance(measured, dict):
                    decision_lag_sec = decision_lag_seconds(
                        measured.get("latency_ms")
                    )
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
