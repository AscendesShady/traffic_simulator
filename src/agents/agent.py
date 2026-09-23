"""Separate-process LangGraph/Ollama controller for route-level TSP and DBL."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import queue
import tempfile
import threading
import time
from typing import TypedDict

# Running this file directly (python src/.../x.py, or the IDE Run button) puts
# its own folder on sys.path instead of the repo root; put the root back so
# the src.* imports below resolve the same way they do under run.py / -m.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from src.core import guard
from src.core.signal_controller import TSP_MAX_ADJUST_FRACTION
from src.agents import rule_controller
from src.ui import control_panel

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
    import openai as _openai
except ImportError:  # OpenAI remains unavailable without the optional SDK.
    _openai = None

try:
    from langgraph.graph import END, StateGraph
except ImportError:  # Reported as an all-off dependency failure at runtime.
    END = None
    StateGraph = None


BASE_DIR = Path(__file__).resolve().parents[2]
assert (BASE_DIR / "requirements.txt").exists(), (
    f"BASE_DIR does not resolve to the repo root: {BASE_DIR}"
)
TELEMETRY_PATH = BASE_DIR / "data" / "traffic_state_telemetry.json"
AI_CONTROL_PATH = BASE_DIR / "data" / "ai_control.json"
DECISION_PATH = BASE_DIR / "data" / "decision.json"
VERBOSE_LOG = True
TURN_LOG_PATH = BASE_DIR / "logs" / "agent_turn_log.jsonl"
STALE_SECONDS = 3.0
RECENT_DECISION_LIMIT = 5
DEFAULT_CONTROL = {
    "armed": False,
    "model": "None",
    "tick_seconds": 5,
    "simulation_running": False,
    "control_mode": "assisted",
}
CONTROL_MODE_ASSISTED = "assisted"
CONTROL_MODE_CONFIGURED = "configured"
FRAME_RESET_MARGIN = 100
# A decision does not land instantly: the model needs seconds to answer, and
# traffic keeps moving meanwhile. The previous turn's measured latency is the
# best available predictor of this turn's, since a given model is consistent
# turn to turn.
DEFAULT_DECISION_LAG_SEC = 8.0
# Beyond this many seconds a bus is too far out for a flag decided now to be
# the right call; it will be reconsidered on a later turn.
ACTIONABLE_HORIZON_SEC = 45.0
class ModelBusyError(RuntimeError):
    """The previous turn's call is still occupying the provider lock, so
    this turn never reached the model.

    Distinct from every other failure because it is not one: nothing was
    asked and nothing answered. A provider timeout abandons its worker
    thread (the HTTP request cannot be killed) and that thread keeps the
    lock until it finishes, so one slow call used to poison every tick
    behind it -- each recorded as a guarded REJECT that published an
    all-off decision. In campaign 2026-09-22 that was 611 of grok-4.6's
    787 rejects, a 98.6% "reject rate" and decisions_effective = 0 for an
    arm whose model was working. A busy tick is a skipped grid point
    (SKIPPED_SLOW): it is counted, and decision.json is left alone.
    """


GEMINI_TIMEOUT_SECONDS = 30.0
OPENAI_TIMEOUT_SECONDS = 30.0
GROK_TIMEOUT_SECONDS = 30.0
# xAI serves Grok through an OpenAI-compatible endpoint, so it reuses the
# openai SDK and the same call path as OpenAI, pointed at a different host.
GROK_BASE_URL = "https://api.x.ai/v1"
OLLAMA_TIMEOUT_SECONDS = 45.0
# CPU split with the simulator on the same machine. Ollama defaults to one
# inference thread per physical core, so a model that does not fit in VRAM
# and spills layers to the CPU takes every core, and the simulator's single
# thread -- whose 60 Hz step sets the pace every latency figure depends on --
# is time-sliced against it. Leave SIM_RESERVED_CORES physical cores to the
# simulator (its thread, Tk, the agent and the OS); main raises its own
# priority as well. OLLAMA_NUM_THREAD in the environment overrides.
SIM_RESERVED_CORES = 2


def ollama_num_thread():
    override = os.environ.get("OLLAMA_NUM_THREAD", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    # ponytail: assumes 2-way SMT (Ryzen, non-hybrid Intel); on a hybrid
    # P/E-core CPU set OLLAMA_NUM_THREAD instead.
    physical = max(1, (os.cpu_count() or 2) // 2)
    return max(1, physical - SIM_RESERVED_CORES)


OLLAMA_NUM_THREAD = ollama_num_thread()

# One context window for every local model. Ollama sizes the window from the
# model's own maximum when none is given -- 65,536 tokens for llama3.2:3b,
# a 10 GB allocation that put 42 % of the model on the CPU of an 8 GB GPU,
# where it competed with the simulator; at 8,192 the same model is 3.1 GB and
# 100 % GPU (2026-09-23, Ollama 0.34). The turn prompt is 2.2-3.0k tokens and
# a reasoning model's answer up to ~2.3k, so 8k holds both; a fixed window
# also means every arm decides with the same context.
OLLAMA_NUM_CTX = 8192
# A prompt that fills the window is truncated by Ollama without an error, and
# a model answering a cut-off prompt still emits valid JSON. Fail closed.
OLLAMA_CONTEXT_MARGIN_TOKENS = 256
_MODEL_CONTEXT_TOKENS = {}


def model_context_tokens(client, model):
    """The window Ollama actually gives ``model``: OLLAMA_NUM_CTX, capped at
    the model's own maximum (orca-mini:7b has 4,096), read once per model."""
    if model not in _MODEL_CONTEXT_TOKENS:
        native = None
        try:
            info = client.show(model)
            model_info = getattr(info, "modelinfo", None) or {}
            native = min(
                (int(v) for k, v in model_info.items()
                 if k.endswith(".context_length") and "original" not in k),
                default=None,
            )
        except Exception:
            native = None
        _MODEL_CONTEXT_TOKENS[model] = min(OLLAMA_NUM_CTX, native or OLLAMA_NUM_CTX)
    return _MODEL_CONTEXT_TOKENS[model]
_GEMINI_CLIENT = None
_GEMINI_CALL_LOCK = threading.Lock()
_OPENAI_CLIENT = None
_OPENAI_CALL_LOCK = threading.Lock()
_GROK_CLIENT = None
_GROK_CALL_LOCK = threading.Lock()
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

# --- AI Configured: the model writes the timing plan --------------------------
_NODE_PLAN_EXAMPLE = {
    "ew_green_sec": 30, "ns_green_sec": 15, "end_current_green_now": False,
    "dbl": {approach: False for approach in guard.PLAN_APPROACHES},
}
PLAN_OUTPUT_SCHEMA = json.dumps(
    {
        "reason": "<one sentence: the passenger comparison behind this plan>",
        "plan": {node: dict(_NODE_PLAN_EXAMPLE) for node in guard.PLAN_NODES},
    },
    indent=2,
)
PLAN_OLLAMA_FORMAT = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "plan": {
            "type": "object",
            "properties": {
                node: {
                    "type": "object",
                    "properties": {
                        "ew_green_sec": {"type": "number"},
                        "ns_green_sec": {"type": "number"},
                        "end_current_green_now": {"type": "boolean"},
                        "dbl": {
                            "type": "object",
                            "properties": {a: {"type": "boolean"} for a in guard.PLAN_APPROACHES},
                            "required": list(guard.PLAN_APPROACHES),
                            "additionalProperties": False,
                        },
                    },
                    "required": ["ew_green_sec", "ns_green_sec", "end_current_green_now", "dbl"],
                    "additionalProperties": False,
                }
                for node in guard.PLAN_NODES
            },
            "required": list(guard.PLAN_NODES),
            "additionalProperties": False,
        },
    },
    "required": ["reason", "plan"],
    "additionalProperties": False,
}


def signal_plan_prompt(telemetry: dict | None = None) -> str:
    """The AI Configured system prompt, with the run's fixed intervals and
    measured saturation flow filled in from telemetry."""
    timing = ((telemetry or {}).get("signal_state") or {}).get("timing") or {}
    yellow_s = (timing.get("yellow_frames") or 60) / 60.0
    all_red_s = (timing.get("all_red_frames") or 60) / 60.0
    sat_flow = (telemetry or {}).get("measured_saturation_flow_veh_per_hr") or 1900
    return f"""You are the signal controller for two signalized nodes on an arterial
(NODE {control_panel.NODE_A_X} is upstream of NODE {control_panel.NODE_B_X}
for eastbound traffic, the reverse westbound; each node also has a
north-south side street). There is no fixed timing plan: every turn you write
the plan both nodes run until your next turn.

OBJECTIVE: the most passengers moved through the nodes per minute and the
fewest person-hours of delay for everyone - arterial, side street, bus riders,
drivers. A bus carries 45 passengers, a car 4, a truck 1. Judge every choice
by passengers, not vehicles.

HOW THE SIGNALS WORK. Each node has two phases. EW green serves eastbound and
westbound (straight, and left turns when a gap opens) while NB/SB are red; NS
green serves the side street while EB/WB are red. Phases alternate; you set
the length of each green in seconds ({guard.MIN_GREEN_SEC} to
{guard.MAX_GREEN_SEC}). Every switch costs a fixed lost time of {yellow_s:g}s
yellow plus {all_red_s:g}s all-red during which nobody moves - short greens
waste more of the cycle in lost time, long greens make the other side queue.
A lane discharges at most about {int(sat_flow)} vehicles per hour of green
(one every {3600.0 / max(1.0, float(sat_flow)):.1f}s), so a queue longer than
its green can serve grows every cycle. end_current_green_now ends the running
green right now (through its yellow and all-red) - use it when the running
phase is serving nobody and the other side is queued; it cannot cut a green
below {guard.MIN_GREEN_SEC}s. Your plan applies immediately: if the running
green is already longer than the value you set, it ends now. Setting the same
plan again does not restart anything.

HOW THE BUS-LANE FLASHER WORKS. dbl=true on an approach reserves that
approach's left-most lane (lane 2) for buses: the lane flashes, every
through-car in it moves out, and buses on that approach move into it and pass
the car queue. It costs that approach one of its three lanes. Left-turning
cars keep using lane 2 (left turns are made from it), so a bus behind a
left-turner waiting for a gap gains nothing until that left goes -
left_turners_lane2 tells you how many are there. It helps only when a bus is
actually queued behind through-cars on that approach; a reserved lane with no
bus is pure lost capacity. It is re-evaluated every turn: set it again to
keep it.

WHAT YOU SEE, per node: the running phase and residual_green_s; the plan now
in force; per approach queue_len_m (how far back it is queued), waiting_pax,
left_turners_lane2, downstream_free_m (room beyond the node - BLOCKED means
a green there only stalls vehicles inside the intersection: never lengthen a
green into BLOCKED), buses approaching with passengers and eta_sec, and
whether lane 2 is reserved. DECISION_LAG_SEC is how long your plan takes to
land; plan for where traffic will be then.

RULES OF THUMB: give each phase green roughly in proportion to the
passengers queued on it; lengthen the green a full bus is about to reach and
shorten the green that faces an empty approach; do not cut a green a bus is
within a few seconds of using; keep both greens short when both sides are
near-empty and long when both are heavy (fewer switches); reserve lane 2
only for a bus stuck behind through-cars with room ahead.

Return EXACTLY one JSON object matching this literal schema - strict JSON,
no markdown, no analysis, no extra keys; node keys are the strings shown:
{PLAN_OUTPUT_SCHEMA}

"reason" is one sentence under about 40 words naming the passenger
comparison behind this turn's plan, on one line with no quotation marks.
"""
SYSTEM_PROMPT = f"""You add transit signal priority on top of two signalized
nodes (NODE {control_panel.NODE_A_X} is upstream of NODE
{control_panel.NODE_B_X} for eastbound traffic, the reverse for westbound)
that are already running Webster-optimal timing. Each turn you decide only
which bus routes get TSP and DBL. You never set a signal.

OBJECTIVE: the fewest person-hours of delay across everyone in the network,
bus riders and cross-street drivers alike, and the most passengers moved
through the nodes per minute. A bus carries 45 passengers, a car 4, a truck 1.
A grant helps only when the bus riders it saves outnumber the cross-street
passengers it holds.

WHAT THE KNOBS DO:
- TSP on a route asks the controller to serve that route's nearest
  approaching bus at its target node: extend the current green until the bus
  clears, or cut the conflicting phase short so its green starts early. Either
  adjustment is capped at {int(TSP_MAX_ADJUST_FRACTION * 100)}% of the
  phase's green, and the cross street always still gets its yellow, all-red
  and minimum green. The controller only considers a bus inside the
  eligibility distance of its node, and withholds an early green it cannot
  deliver (the cut would not beat the bus to the stop bar, or the bus would
  arrive outside the green it brings forward); such a bus finishes DENIED. A
  TSP flag only requests: it never moves the bus, and a bus stuck in a queue
  cannot use it.
- DBL on a route reserves the left-most approach lane (lane 2) at that bus's
  target node for the bus alone: every car in that lane is ordered out and the
  bus merges into it. It helps only when the bus is queued behind cars it can
  pass; it costs the cars a lane. DBL and TSP are independent: a route can
  have either, both or neither.
- A grant applies to the whole route (all its buses). You decide it for a
  bus the controller has not yet taken a request for; once it has (LOCKED),
  the grant stays in force until that bus is served.

THE TSP TEST, for each route with an approaching bus (every number is on that
route's ROUTES line):
  grant tsp when would_stop=true AND passengers > cross_pax AND
  actionable=true AND the bus's downstream is not BLOCKED.
- would_stop=false: the bus reaches the stop bar inside the residual green
  (residual_green_s). Priority gains it nothing and only holds cross traffic.
  Set tsp=false.
- cross_pax is the passengers queued on the approaches a grant would hold at
  red. If cross_pax >= passengers, set tsp=false: you would delay more people
  than you help. (Head-counts are the proxy: the bus saves up to one red, the
  cross street loses at most one capped adjustment, so a bus with more riders
  than the cross queue is a net gain.)
- actionable=false: the bus arrives before your decision lands
  (eta_at_decision_land_sec <= 0) or is too far out to plan for. Set tsp=false.
- Grant at most ONE route per node per turn. If two qualify at one node, take
  the larger passengers - cross_pax.
- A route with approaching_buses=0 must have tsp=false and dbl=false.

THE DBL TEST, for each route with an approaching bus:
  grant dbl when dbl_lane_queue_ahead=0 AND dbl_lane_obstructed=false.
DBL reserves the left-most approach lane exclusively for its target bus. Once
DBL is activated, ALL other vehicles in that lane must clear it immediately;
this clearance rule is unconditional. Therefore you MUST NOT enable DBL when
the lane cannot already be cleared. If dbl_lane_queue_ahead is greater than 0,
or dbl_lane_obstructed=true, set dbl=false: a stopped/crawling vehicle is in
front of the bus or its merge is blocked, so the bus would only sit in the
queue. This remains true when nearest_bus_in_dbl_lane=true. A route showing
dbl=true with nearest_bus_in_dbl_lane=false is evidence the DBL you enabled is
not working.

TIMING: DECISION_LAG_SEC is how long your decision takes to apply;
eta_at_decision_land_sec is where each bus will be when it does. Decide for
where traffic will BE, not where it is. Decide each turn from the current
state only; never carry flags forward from a previous turn.

NETWORK: queue_len_m is how far back each approach is backed up;
downstream_free_m is the room beyond the node for vehicles to move into.
BLOCKED means none: giving that approach green will NOT help, the vehicles
would stall inside the intersection, so never grant priority into it.
LOCKED_ROUTES are requests the controller already holds; leave them as shown. If both
sides of a node are long-queued, grant nothing there and let the Webster
timing work.

The route positions are fixed in this exact order:
{ROUTE_ORDER_TEXT}

Return EXACTLY one JSON object matching this literal schema. The first boolean
in each array controls route position 1, the second controls position 2, and so
on. Never write route IDs as JSON keys. Use strict JSON booleans and do not add
markdown, analysis, or extra keys:
{OUTPUT_SCHEMA}

"reason" must be one sentence under about 40 words naming the passengers vs
cross_pax comparison behind this turn's grants, or why none qualified. Keep it
on one line, with no line breaks and no quotation marks. Each array must
contain exactly {len(guard.ROUTE_ORDER)} booleans.

Example: only route 1 has a qualifying bus, so the correct output is
tsp=[true,false,false,false,false,false] and
dbl=[false,false,false,false,false,false].
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
    control_path: str
    # Fixed decision-schedule bookkeeping (every arm decides on the same
    # sim-time grid; see run_forever): which grid point this turn answers,
    # in sim-seconds, and the nominal spacing of that grid.
    tick_index: int
    scheduled_sim_time: float
    decision_interval_sec: float
    requested_flags: dict
    control_mode: str
    # Run identity stamped into every published decision. A TypedDict key
    # LangGraph does not know is dropped on invoke -- these were, and every
    # decision left as run_uuid "" and was refused FOREIGN_DECISION.
    run_uuid: str | None
    telemetry_frame: int | None


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
            # Fixed decision-schedule provenance (section 0): which sim-time
            # grid point this was and how wide the grid is, so a summary
            # export can tell offered opportunities from issued decisions
            # without recomputing the schedule from timestamps.
            "tick_index": state.get("tick_index"),
            "scheduled_sim_time": state.get("scheduled_sim_time"),
            "decision_interval_sec": state.get("decision_interval_sec"),
            "sim_time_s": telemetry.get("simulation_time_seconds"),
            "minimap": state.get("minimap", ""),
            # Exact telemetry snapshot used to derive this turn's minimap.
            # This is an audit record only; the concise minimap remains the
            # actual user message sent to an LLM.
            "telemetry_snapshot": telemetry,
            "raw_output": state.get("raw_output", ""),
            "flags": decision.get("flags", {}),
            "requested_flags": state.get("requested_flags", {}),
            # AI Configured: the plan is the decision (decisions_effective and
            # the audit sheet read it from here).
            "schema": decision.get("schema"),
            "plan": decision.get("plan"),
            "reason": decision.get("reason", ""),
            "pax_per_min_recent": throughput.get(
                "passengers_per_minute_recent"
            ),
            "latency_ms": call_metrics.get("latency_ms"),
            "input_tokens": call_metrics.get("input_tokens"),
            "output_tokens": output_tokens,
            "tokens_per_sec": tokens_per_sec,
            "cost_usd": call_metrics.get("cost_usd"),
            "locked_routes": sorted(locked_routes),
            "stale": state.get("status") in ("STALE", "HELD"),
        }
        with TURN_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def log_skipped_tick(
    tick_index: int, scheduled_sim_time: float, model: str, decision_interval_sec: float
) -> None:
    """Record a fixed-schedule grid point that came due while the previous
    turn's model call was still running (section 0's "skipped and counted",
    not deferred): decision.json is left exactly as it was, only the missed
    opportunity is logged, so a slow model's real utilisation is visible
    instead of silently stretching its effective decision interval."""
    if not VERBOSE_LOG:
        return
    try:
        record = {
            "turn": None,
            "timestamp": round(time.time(), 3),
            "model": str(model or "None"),
            "status": "SKIPPED_SLOW",
            "tick_index": tick_index,
            "scheduled_sim_time": scheduled_sim_time,
            "sim_time_s": scheduled_sim_time,
            "decision_interval_sec": decision_interval_sec,
            "flags": {},
            "reason": "previous turn's model call was still running at this tick",
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
                control_panel.TICK_SECONDS_MAX,
                max(control_panel.TICK_SECONDS_MIN, int(payload.get("tick_seconds", 5))),
            ),
            "simulation_running": payload.get("simulation_running") is True,
            "control_mode": (
                CONTROL_MODE_CONFIGURED
                if payload.get("control_mode") == CONTROL_MODE_CONFIGURED
                else CONTROL_MODE_ASSISTED
            ),
        }
    except Exception:
        return dict(DEFAULT_CONTROL)


def is_configured(state) -> bool:
    return state.get("control_mode") == CONTROL_MODE_CONFIGURED


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


def turn_decision_lag(model, carried_lag_sec) -> float:
    """The horizon this turn should plan with.

    A model's horizon is the previous turn's measured latency (carried in
    ``carried_lag_sec``). The rule has no inference to wait for, so it plans
    with its own near-zero lag from the first turn, and a rule turn never
    feeds the model carry-forward.
    """
    if rule_controller.is_rule_model(model):
        return rule_controller.RULE_DECISION_LAG_SEC
    return carried_lag_sec


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
    loaded = {"telemetry": telemetry, "status": "OK"}
    # The frame this turn is decided on: main measures the control delay
    # from it to the frame the decision takes effect.
    frame = _telemetry_frame_number(telemetry)
    if frame is not None:
        loaded["telemetry_frame"] = frame
    return loaded


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


def _approach_metres(table, approach):
    """Format a per-approach metre value; '?' when telemetry lacks the field."""
    if not isinstance(table, dict):
        return "?"
    value = _finite_nonnegative(table.get(approach))
    return "?" if value is None else f"{value:.1f}"


def _left_turners_lane2(node, approach):
    table = node.get("left_turners_lane2")
    return int(table.get(approach, 0) or 0) if isinstance(table, dict) else 0


def read_signal_map(state: AgentState) -> dict:
    """AI Configured's view: per node the running phase, the plan in force
    and per approach what is waiting; buses by approach with ETA."""
    telemetry = state.get("telemetry", {})
    throughput = telemetry.get("network_throughput", {})
    signal_state = telemetry.get("signal_state", {})
    nodes = signal_state.get("nodes", {})
    live_plan = signal_state.get("plan") or {}
    decision_lag_sec = state.get("decision_lag_sec")
    if _finite_nonnegative(decision_lag_sec) is None:
        decision_lag_sec = DEFAULT_DECISION_LAG_SEC
    buses_by_node = {}
    for bus in telemetry.get("active_buses", []) or []:
        leg = bus.get("route_leg") if isinstance(bus, dict) else None
        if not leg or (bus.get("distance_to_stop_bar_px") or -1) < 0:
            continue
        approach = str(leg.get("approach") or bus.get("direction", "?"))
        buses_by_node.setdefault(str(leg.get("node_x")), []).append(
            f"{approach}:{int(bus.get('passengers', 0))}pax/eta{bus.get('eta_to_stop_bar_sec_freeflow', '?')}s"
        )
    lines = [
        f"simulation_time_seconds={telemetry.get('simulation_time_seconds', 0)}",
        f"DECISION_LAG_SEC={decision_lag_sec}",
        f"passengers_per_minute={throughput.get('passengers_per_minute', 0)}",
        f"passengers_per_minute_recent={throughput.get('passengers_per_minute_recent', 0)}",
        "PLAN_IN_FORCE=" + (json.dumps(live_plan, sort_keys=True) if live_plan else "Webster (no plan yet)"),
        "NODES:",
    ]
    for node_x, node in sorted(nodes.items(), key=lambda item: str(item[0])):
        if not isinstance(node, dict):
            continue
        waiting = node.get("queues_passengers_est") or {}
        signals = node.get("signals", {})
        blocked = node.get("downstream_blocked") or {}
        dbl = node.get("dbl_commanded") or []
        residual_s = round((node.get("residual_green_frames") or 0) / 60.0, 1)
        lines.append(
            f"- NODE {node_x}: phase={node.get('phase', 'UNKNOWN')} residual_green_s={residual_s} "
            "signals: " + " ".join(f"{a}={signals.get(a, '?')}" for a in guard.PLAN_APPROACHES)
            + " | " + " ".join(
                f"{a}[waiting_pax={int(_finite_nonnegative(waiting.get(a), 0) or 0)} "
                f"queue_len_m={_approach_metres(node.get('queue_length_m'), a)} "
                f"left_turners_lane2={_left_turners_lane2(node, a)} "
                f"downstream_free_m={_approach_metres(node.get('downstream_space_m'), a)}"
                f"{'(BLOCKED)' if blocked.get(a) else ''} "
                f"lane2_reserved={a in dbl}]"
                for a in guard.PLAN_APPROACHES
            )
            + " buses: " + (" ".join(buses_by_node.get(str(node_x), [])) or "none")
        )
    return {"minimap": "\n".join(lines)}


def read_minimap(state: AgentState) -> dict:
    if is_configured(state):
        return read_signal_map(state)
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
        queue_len_text = " ".join(
            f"{approach}={_approach_metres(node.get('queue_length_m'), approach)}"
            for approach in ("EB", "WB", "NB", "SB")
        )
        downstream_blocked = node.get("downstream_blocked")
        if not isinstance(downstream_blocked, dict):
            downstream_blocked = {}
        downstream_text = " ".join(
            f"{approach}="
            f"{_approach_metres(node.get('downstream_space_m'), approach)}"
            + ("(BLOCKED)" if downstream_blocked.get(approach) else "")
            for approach in ("EB", "WB", "NB", "SB")
        )
        lines.append(
            f"- NODE {node_x}: phase={node.get('phase', 'UNKNOWN')} "
            f"signals: {signal_text} "
            f"waiting_pax: EB={waiting['EB']} WB={waiting['WB']} "
            f"NB={waiting['NB']} SB={waiting['SB']} "
            f"(total={sum(waiting.values())}) "
            f"queue_len_m: {queue_len_text} "
            f"downstream_free_m: {downstream_text} "
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
                f"dbl_lane_queue_ahead="
                f"{int(_finite_nonnegative(route.get('dbl_lane_queue_ahead'), 0) or 0)} "
                f"nearest_bus_in_dbl_lane="
                f"{bool(route.get('nearest_bus_in_dbl_lane', False))} "
                f"approaching_buses={len(candidates)} "
                f"nearest_eta_sec={nearest.get('eta_to_stop_bar_sec_freeflow')} "
                f"eta_at_decision_land_sec={landed_eta} "
                f"actionable={is_actionable(landed_eta)} "
                f"target_node={route_leg.get('node_x')} "
                f"passengers={int(nearest.get('passengers', 0))} "
                f"signal_ahead={nearest.get('signal_colour_ahead', '?')} "
                f"residual_green_s={nearest.get('residual_green_sec', '?')} "
                f"would_stop={bool(nearest.get('would_have_stopped', True))} "
                f"cross_pax={int(_finite_nonnegative(nearest.get('cross_traffic_pax'), 0) or 0)} "
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
                f"dbl_lane_queue_ahead="
                f"{int(_finite_nonnegative(route.get('dbl_lane_queue_ahead'), 0) or 0)} "
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
    if is_configured(state):
        # The plan is re-issued whole every turn; nothing is locked.
        return {"locked_routes": locked, "minimap": state.get("minimap", "")}
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
        # IN_INTERSECTION, TURNING, ...), while these telemetry booleans
        # expose the controller's request lifecycle. Keep a route locked from
        # the moment the controller holds a request for one of its buses
        # (armed for an early green, waiting on the gate) through the
        # active-green and clearing portions of the grant: the controller
        # reads the flag as a continuous hold and cancels a live request the
        # turn the flag drops (FEATURE_DISABLED, and the bus's leg is then
        # suppressed for good), so a decider that re-derives its grants from
        # each snapshot -- cross_pax ticking over the bus load, the bus's
        # landed ETA reaching 0 at the bar -- was withdrawing most of what it
        # had asked for. The decider still decides every *new* request.
        if (
            bool(bus.get("priority_requested", False))
            or bool(bus.get("priority_granted", False))
            or bool(bus.get("priority_clearing", False))
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
        raise ModelBusyError("previous Gemini request is still running")

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


def _is_openai(model: str) -> bool:
    return isinstance(model, str) and model.startswith("gpt-")


def _get_openai_client():
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        if _openai is None:
            raise RuntimeError("openai package not installed")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        _OPENAI_CLIENT = _openai.OpenAI(api_key=api_key)
    return _OPENAI_CLIENT


def _is_grok(model: str) -> bool:
    return isinstance(model, str) and model.startswith("grok-")


def _get_grok_client():
    global _GROK_CLIENT
    if _GROK_CLIENT is None:
        if _openai is None:
            raise RuntimeError("openai package not installed")
        api_key = os.environ.get("GROK_API_KEY")
        if not api_key:
            raise RuntimeError("GROK_API_KEY not set")
        _GROK_CLIENT = _openai.OpenAI(api_key=api_key, base_url=GROK_BASE_URL)
    return _GROK_CLIENT


def _call_openai(
    model: str, system_prompt: str, minimap: str
) -> tuple[str, dict]:
    """Call OpenAI with low temperature and a hard, non-overlapping timeout."""
    return _call_openai_compatible(
        "OpenAI", _get_openai_client(), _OPENAI_CALL_LOCK,
        OPENAI_TIMEOUT_SECONDS, model, system_prompt, minimap,
    )


def _call_grok(
    model: str, system_prompt: str, minimap: str
) -> tuple[str, dict]:
    """Call xAI Grok through its OpenAI-compatible endpoint."""
    return _call_openai_compatible(
        "Grok", _get_grok_client(), _GROK_CALL_LOCK,
        GROK_TIMEOUT_SECONDS, model, system_prompt, minimap,
    )


def _call_openai_compatible(
    provider: str, client, call_lock, timeout_seconds: float,
    model: str, system_prompt: str, minimap: str,
) -> tuple[str, dict]:
    if not call_lock.acquire(blocking=False):
        raise ModelBusyError(f"previous {provider} request is still running")

    result_queue = queue.Queue(maxsize=1)

    def request():
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": minimap},
                ],
                temperature=0.2,
            )
            outcome = (True, response)
        except Exception as exc:
            outcome = (False, exc)
        finally:
            call_lock.release()
        result_queue.put(outcome)

    request_thread = threading.Thread(
        target=request,
        name=f"{provider.lower()}-agent-request",
        daemon=True,
    )
    try:
        request_thread.start()
    except Exception:
        call_lock.release()
        raise

    try:
        succeeded, value = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        # A thread deadline cannot kill the underlying HTTP request; the
        # daemon and call lock let the agent continue without overlapping it.
        raise TimeoutError(
            f"{provider} request exceeded {timeout_seconds:g}s timeout"
        ) from exc
    if not succeeded:
        raise value
    response = value
    choices = getattr(response, "choices", None) or []
    text = choices[0].message.content if choices else None
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"empty {provider} response")
    usage = getattr(response, "usage", None)
    metrics = {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "cost_usd": (
            getattr(usage, "cost_in_usd_ticks", None) / 10_000_000_000.0
            if isinstance(getattr(usage, "cost_in_usd_ticks", None), (int, float))
            else None
        ),
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


def _call_ollama(
    model: str, minimap: str, system_prompt: str = SYSTEM_PROMPT,
    output_format: dict = OLLAMA_OUTPUT_FORMAT,
) -> tuple[str, dict]:
    client = _get_ollama_client()
    call_lock = _OLLAMA_CALL_LOCK
    if not call_lock.acquire(blocking=False):
        raise ModelBusyError("previous Ollama request is still running")

    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": minimap},
        ],
        "options": {
            "temperature": 0.2,
            "num_thread": OLLAMA_NUM_THREAD,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    }

    result_queue = queue.Queue(maxsize=1)

    def request_ollama():
        try:
            try:
                response = client.chat(format=output_format, **request)
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
    prompt_tokens = metrics["input_tokens"] or 0
    window = model_context_tokens(client, model)
    if prompt_tokens >= window - OLLAMA_CONTEXT_MARGIN_TOKENS:
        raise RuntimeError(
            f"prompt used {prompt_tokens} of the {window}-token context: "
            "it may have been truncated, so the decision is not trusted"
        )
    return content, metrics


def _call_rule(state: AgentState) -> tuple[str, dict]:
    """Decide with a non-LLM comparator (rule or max-pressure) instead of a
    model.

    Serializes the rule's positional flags into the same JSON text an LLM is
    asked to produce, so everything downstream -- the guard, the
    locked-route overlay, decision.json, the turn log and the exports --
    handles a rule turn identically to a model turn. There are no tokens to
    report, and the near-zero latency this reports is itself a result: the
    rule has no decision lag where a model does.
    """
    model = state.get("model", "")
    if rule_controller.is_max_pressure_model(model):
        decide = rule_controller.max_pressure_decision
    else:
        decide = rule_controller.rule_based_decision
    decision = decide(
        state.get("telemetry", {}),
        decision_lag_sec=state.get("decision_lag_sec"),
    )
    return json.dumps(decision), {
        "input_tokens": None,
        "output_tokens": None,
        "eval_duration_ns": None,
        "total_duration_ns": None,
    }


OBSERVATION_ONLY = "OBSERVATION_ONLY"


def is_baseline_model(model) -> bool:
    """The no-controller baseline: nothing decides, the agent only observes."""
    return not model or str(model) == "None"


def observation_only_decision(turn, model) -> dict:
    return {
        "schema_version": 1,
        "turn": int(turn or 0),
        "timestamp": round(time.time(), 3),
        "model": str(model or "None"),
        "status": OBSERVATION_ONLY,
        "flags": guard.all_off_flags(),
        "reason": "no-controller baseline: observing only",
    }


def guard_baseline(decision: dict, state: AgentState) -> dict:
    """Last check before anything reaches disk: a baseline turn, or a turn
    whose arm/model changed underneath it (a straggler from the run that
    just ended, landing in the next run's fresh log), is observation only.
    An OK decision can never be attributed to the baseline arm. The live
    loop passes ``control_path`` so the control file is re-read here; a
    state without it (tests driving one turn) skips the straggler check.
    """
    model = str(state.get("model", "None"))
    control_path = state.get("control_path")
    stale_arm = False
    if control_path:
        control = read_ai_control(Path(control_path))
        stale_arm = not control["armed"] or str(control["model"]) != model
    if is_baseline_model(model) or stale_arm:
        decision = observation_only_decision(state.get("turn", 0), model)
    assert not (is_baseline_model(decision.get("model")) and decision.get("status") == "OK")
    return decision


def ai_turn(state: AgentState) -> dict:
    model = state.get("model", "None")
    started = time.time()
    if is_baseline_model(model):
        return {
            "raw_output": "",
            "status": OBSERVATION_ONLY,
            "call_metrics": {"latency_ms": 0.0, "input_tokens": None,
                             "output_tokens": None, "eval_duration_ns": None,
                             "total_duration_ns": None},
        }
    configured = is_configured(state)
    system_prompt = (
        signal_plan_prompt(state.get("telemetry")) if configured else SYSTEM_PROMPT
    )
    try:
        if rule_controller.is_rule_model(model):
            # LLM-only mode: a rule model has no plan to write; the guard
            # holds Webster on the empty output.
            raw_output, call_metrics = ("", {}) if configured else _call_rule(state)
        elif _is_gemini(model):
            raw_output, call_metrics = _call_gemini(
                model,
                system_prompt,
                state.get("minimap", ""),
            )
        elif _is_openai(model):
            raw_output, call_metrics = _call_openai(
                model,
                system_prompt,
                state.get("minimap", ""),
            )
        elif _is_grok(model):
            raw_output, call_metrics = _call_grok(
                model,
                system_prompt,
                state.get("minimap", ""),
            )
        elif configured:
            raw_output, call_metrics = _call_ollama(
                model, state.get("minimap", ""), system_prompt, PLAN_OLLAMA_FORMAT
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
    except ModelBusyError:
        # Not a model failure: propagate so run_forever counts a skipped
        # grid point and leaves the standing decision in place.
        raise
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
    if is_configured(state):
        # The plan is validated whole and clamped by the guard; every safety
        # interval (yellow, all-red, minimum green, clearance) is enforced by
        # the controller, so there is nothing for the model to cheat past.
        decision = guard.safe_signal_plan(
            state.get("raw_output", ""),
            state.get("turn", 0),
            state.get("model", "None"),
        )
        return {"decision": decision, "requested_flags": {}, "status": decision["status"]}
    decision = guard.safe_decision(
        state.get("raw_output", ""),
        state.get("turn", 0),
        state.get("model", "None"),
    )
    requested_flags = {
        route_id: dict(route_flags)
        for route_id, route_flags in (decision.get("flags") or {}).items()
        if isinstance(route_flags, dict)
    }
    routes = state.get("telemetry", {}).get("routes", {})
    if decision["status"] == "OK":
        raw_locked_routes = state.get("locked_routes", set())
        locked_routes = (
            set(raw_locked_routes)
            if isinstance(raw_locked_routes, (set, frozenset, list, tuple))
            else set()
        )
        for route_id, route_flags in decision["flags"].items():
            current = routes.get(route_id, {}) if isinstance(routes, dict) else {}
            # A model instruction is not a safety boundary. Refuse a new DBL
            # activation deterministically when current telemetry says the
            # bus would sit behind a queue or cannot complete its merge. An
            # already-active locked grant is preserved until it safely clears.
            queue_ahead = _finite_nonnegative(
                current.get("dbl_lane_queue_ahead"), 0
            )
            if route_id not in locked_routes and (
                bool(current.get("dbl_lane_obstructed", False))
                or bool(queue_ahead)
            ):
                route_flags["dbl"] = False
        for route_id in locked_routes:
            current = routes.get(route_id, {}) if isinstance(routes, dict) else {}
            decision["flags"][route_id] = {
                "tsp": bool(current.get("tsp_enabled", False)),
                "dbl": bool(current.get("dbl_enabled", False)),
            }
    return {
        "decision": decision,
        "requested_flags": requested_flags,
        "status": decision["status"],
    }


def _remember_decision(state: AgentState, decision: dict) -> list:
    recent = list(state.get("recent_decisions", []))
    recent.append(decision)
    return recent[-RECENT_DECISION_LIMIT:]


def publish_decision(decision: dict, run_uuid, telemetry_frame=None) -> dict:
    """The only writer of decision.json. Every decision names the run (and
    telemetry frame) it was made for, so main.merge_ai_decision can refuse
    one left over from an earlier run, arm or model."""
    decision["run_uuid"] = str(run_uuid or "")
    decision["telemetry_frame"] = telemetry_frame
    atomic_write_json(DECISION_PATH, decision)
    return decision


def write_decision(state: AgentState) -> dict:
    decision = guard_baseline(state["decision"], state)
    publish_decision(decision, state.get("run_uuid"), state.get("telemetry_frame"))
    log_turn(state, decision)
    return {"decision": decision, "recent_decisions": _remember_decision(state, decision)}


def hold(state: AgentState) -> dict:
    # Locked design choice: stale or missing telemetry cannot authorize priority.
    if is_configured(state):
        decision = guard.safe_signal_plan("", state.get("turn", 0), state.get("model", "None"))
    else:
        decision = {
            "schema_version": 1,
            "turn": int(state.get("turn", 0)),
            "timestamp": round(time.time(), 3),
            "model": str(state.get("model", "None")),
            "status": "HELD_ALL_OFF",
            "flags": guard.all_off_flags(),
            "reason": "",
        }
    decision = guard_baseline(decision, state)
    publish_decision(decision, state.get("run_uuid"), state.get("telemetry_frame"))
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


def _dependency_hold(turn: int, model: str, message: str, run_uuid=None) -> dict:
    decision = guard_baseline(
        guard.safe_decision(message, turn, model), {"turn": turn, "model": model}
    )
    publish_decision(decision, run_uuid)
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


# Wall-clock poll while waiting for the next sim-time grid point. Independent
# of decision_interval_sec (typically 2-15s): fine-grained enough that a
# fixed-schedule tick is noticed promptly at any sim_speed, cheap enough that
# polling two small JSON files this often is a non-issue.
TICK_POLL_SEC = 0.2


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
    # Fixed decision schedule (section 0): every arm decides on the same
    # sim-time grid instead of "sleep tick_seconds after the previous turn
    # finishes", which let decision frequency drift with model latency and
    # confounded model comparisons with decision-opportunity counts. A grid
    # point due while the previous turn is still running is skipped and
    # counted (log_skipped_tick), never deferred/caught-up.
    next_scheduled_sim_time = None
    tick_index = 0
    while True:
        control = read_ai_control()
        telemetry = _read_telemetry()
        turn, recent_decisions, last_frame, reset_detected = _track_run_boundary(
            turn, recent_decisions, last_frame, telemetry
        )
        if reset_detected:
            next_scheduled_sim_time = None
            tick_index = 0
        telemetry_says_stopped = bool(
            isinstance(telemetry, dict)
            and telemetry.get("simulation_running") is False
        )
        if (
            not control["armed"]
            or not control["simulation_running"]
            or telemetry_says_stopped
        ):
            next_scheduled_sim_time = None
            tick_index = 0
            time.sleep(1.0)
            continue

        interval = float(control["tick_seconds"])
        if next_scheduled_sim_time is None:
            next_scheduled_sim_time = interval

        sim_time = (
            telemetry.get("simulation_time_seconds")
            if isinstance(telemetry, dict) else None
        )
        if not isinstance(sim_time, (int, float)) or sim_time < next_scheduled_sim_time:
            time.sleep(TICK_POLL_SEC)
            continue

        scheduled_time = next_scheduled_sim_time
        tick_index += 1
        turn += 1
        model = control["model"]
        run_uuid = telemetry.get("run_uuid") if isinstance(telemetry, dict) else None
        try:
            if graph is None:
                decision = _dependency_hold(
                    turn, model, graph_error or "Agent unavailable", run_uuid
                )
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
                        "decision_lag_sec": turn_decision_lag(
                            model, decision_lag_sec
                        ),
                        "control_path": str(AI_CONTROL_PATH),
                        "run_uuid": run_uuid,
                        "telemetry_frame": _telemetry_frame_number(telemetry),
                        "tick_index": tick_index,
                        "scheduled_sim_time": scheduled_time,
                        "decision_interval_sec": interval,
                        "control_mode": control["control_mode"],
                    }
                )
                recent_decisions = result.get("recent_decisions", recent_decisions)
                # This turn's measured latency predicts the next turn's lag.
                # A rule turn is not a measurement of any model, so it leaves
                # the model carry-forward untouched.
                measured = result.get("call_metrics", {})
                if isinstance(measured, dict) and not rule_controller.is_rule_model(
                    model
                ):
                    decision_lag_sec = decision_lag_seconds(
                        measured.get("latency_ms")
                    )
        except ModelBusyError:
            # The grid point came due while the previous call still held the
            # provider: skipped and counted, exactly like one that came due
            # mid-call. decision.json is untouched, so the arm keeps the
            # treatment it last decided instead of being forced all-off.
            turn -= 1
            log_skipped_tick(tick_index, scheduled_time, model, interval)
        except Exception as exc:
            decision = _dependency_hold(
                turn,
                model,
                f"Agent turn error: {type(exc).__name__}: {str(exc)[:500]}",
                run_uuid,
            )
            recent_decisions = (recent_decisions + [decision])[-RECENT_DECISION_LIMIT:]

        # The call above blocked in sim-time too: any further grid point that
        # came due while it ran is a skipped-and-counted opportunity, not a
        # backlog to catch up on.
        telemetry_after = _read_telemetry()
        sim_time_after = (
            telemetry_after.get("simulation_time_seconds")
            if isinstance(telemetry_after, dict) else None
        )
        probe = scheduled_time + interval
        while isinstance(sim_time_after, (int, float)) and probe <= sim_time_after:
            tick_index += 1
            log_skipped_tick(tick_index, probe, model, interval)
            probe += interval
        next_scheduled_sim_time = probe


def exit_when_parent_dies() -> None:
    """Die with the simulator. main.py hands this process a stdin pipe it
    never writes to; the read returns EOF the instant the parent process is
    gone (however it went), and this process exits before it can re-arm off
    the next session's ai_control.json and double-drive that sim. Started
    only from __main__ so importing the module (tests) starts no thread.
    """
    def watch():
        try:
            sys.stdin.buffer.read()
        except Exception:
            pass
        os._exit(0)

    if sys.stdin is not None and not sys.stdin.isatty():
        threading.Thread(target=watch, name="parent-watchdog", daemon=True).start()


if __name__ == "__main__":
    exit_when_parent_dies()
    try:
        run_forever()
    except KeyboardInterrupt:
        pass
