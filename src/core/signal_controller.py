"""Signal timing, intersection reservations, and bus-priority arbitration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.ui import control_panel
from src.ui.canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from src.core.vehicle import (
    Bus,
    DBL_LANE_INDEX,
    dbl_lane_is_obstructed,
    dbl_lane_queue_ahead,
    ENTRY_ZONE_PX,
    eta_frames_to_stop_bar,
    bus_eta_frames,
    SAFE_GAP_PX,
)

log = logging.getLogger(__name__)

RED = "RED"
YELLOW = "YELLOW"
GREEN = "GREEN"
VALID_SIGNAL_STATES = {RED, YELLOW, GREEN}

NORMAL = "NORMAL"
REQUESTED = "REQUESTED"
ARMED = "ARMED"
TSP_EXTENDING = "TSP_EXTENDING"
TSP_EARLY_TRUNCATE = "TSP_EARLY_TRUNCATE"
COMPLETED = "COMPLETED"
DENIED = "DENIED"
CANCELLED = "CANCELLED"

TSP_ACTION_NONE = "none"
TSP_ACTION_EXTENDING = "extending"
TSP_ACTION_EARLY_GREEN = "early_green"

# Why an early green was withheld. A request that crosses the stop bar
# untreated after one of these terminates DENIED carrying the reason.
TSP_DENY_ETA_WINDOW = "TSP_ETA_OUTSIDE_GREEN_WINDOW"
TSP_DENY_NET_BENEFIT = "TSP_CUT_BELOW_CLEARANCE"
# The bus's receiving lane beyond the node cannot take it: a green bought
# for it would release nothing, so no timing changes until the lane clears.
TSP_DENY_DOWNSTREAM_BLOCKED = "DOWNSTREAM_BLOCKED"
# A standing vehicle appeared ahead of the bus in the reserved lane after the
# request was accepted (a left turner that stopped for a reservation, a
# spillback hold): the lane can no longer deliver the bus, so DBL is revoked.
DBL_REVOKED_LANE_BLOCKED = "DBL_REVOKED_LANE_BLOCKED"

# A bus may not request priority at a node it has not yet been released
# toward: the eligibility zone can never exceed the link between the nodes.
PRIORITY_ELIGIBILITY_MIN_PX = 250.0
PRIORITY_ELIGIBILITY_MAX_PX = float(INT_X[1] - INT_X[0])

# Conventional TSP bounds: one adjustment may move at most this fraction of
# the affected phase's Webster green, and a truncated phase always keeps at
# least MIN_GREEN_FRAMES of green (5 s at 60 fps).
TSP_MAX_ADJUST_FRACTION = 0.20
MIN_GREEN_FRAMES = 300

DISCHARGE_INACTIVE = "INACTIVE"
DISCHARGE_TRANSITION_YELLOW = "TRANSITION_YELLOW"
DISCHARGE_ALL_RED = "DISCHARGE_ALL_RED"
DISCHARGE_WAITING = "WAITING"
DISCHARGE_ACTIVE = "DISCHARGING"
DISCHARGE_RECOVERY_FAILED = "RECOVERY_FAILED"
DISCHARGE_STAGE_YELLOW = "STAGE_YELLOW"
DISCHARGE_STOPPING_YELLOW = "STOPPING_YELLOW"
DISCHARGE_STOPPING_ALL_RED = "STOPPING_ALL_RED"


@dataclass(frozen=True)
class DischargeStage:
    label: str
    greens: tuple[tuple[int, str], ...]


NODE_A_X, NODE_B_X = INT_X[0], INT_X[1]

DISCHARGE_PLAN_STAGES = {
    "Eastbound Corridor": (
        DischargeStage("Node B downstream", ((NODE_B_X, "EB"),)),
        DischargeStage("Node B → Node A coordinated", ((NODE_A_X, "EB"), (NODE_B_X, "EB"))),
    ),
    "Westbound Corridor": (
        DischargeStage("Node A downstream", ((NODE_A_X, "WB"),)),
        DischargeStage("Node A → Node B coordinated", ((NODE_A_X, "WB"), (NODE_B_X, "WB"))),
    ),
    "Node A Northbound": (DischargeStage("Node A northbound", ((NODE_A_X, "NB"),)),),
    "Node A Southbound": (DischargeStage("Node A southbound", ((NODE_A_X, "SB"),)),),
    "Node B Northbound": (DischargeStage("Node B northbound", ((NODE_B_X, "NB"),)),),
    "Node B Southbound": (DischargeStage("Node B southbound", ((NODE_B_X, "SB"),)),),
}

# Auto recovery walks each node clockwise (N -> E -> S -> W), with the two
# corridors acting as the shared east and west legs. Legs with nothing waiting
# are skipped, so the rotation never spends a green on an empty approach.
DISCHARGE_CLOCKWISE_ORDER = (
    "Node A Northbound",
    "Eastbound Corridor",
    "Node A Southbound",
    "Westbound Corridor",
    "Node B Northbound",
    "Node B Southbound",
)
assert set(DISCHARGE_CLOCKWISE_ORDER) == set(DISCHARGE_PLAN_STAGES)
# Upper bound on how long the rotation waits for the leg whose turn it is
# before it may skip ahead. A leg is usually blocked only while the previous
# stage's vehicles clear the box, so a short grace keeps the order exact.
# The effective grace is also capped against discharge_stall_time so the
# hold can never outlive the watchdog and turn a transient block into a
# spurious RECOVERY_FAILED.
DISCHARGE_DUE_LEG_GRACE_FRAMES = 120


@dataclass
class PriorityRequest:
    request_id: str
    bus: Bus
    bus_id: str
    route_id: str
    route_leg_index: int
    node_x: int
    originating_approach: str
    movement: str
    entry_lane: int
    exit_direction: str
    conflicting_approaches: tuple[str, ...]
    requested_at_frame: int
    state: str = REQUESTED
    expires_at_frame: int = 0
    denial_or_cancel_reason: str = ""
    tsp_requested: bool = False
    dbl_requested: bool = False
    attempt_number: int = 1
    tsp_action: str = TSP_ACTION_NONE
    tsp_adjust_frames: int = 0
    tsp_gate_reason: str = ""
    # Instrumentation only: DBL becomes an actual grant when this request is
    # promoted to the node's active request and owns the reserved lane.
    dbl_granted: bool = False
    dbl_revoke_reason: str = ""
    metrics_finalized: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "bus_id": self.bus_id,
            "route_id": self.route_id,
            "route_leg_index": self.route_leg_index,
            "node_x": self.node_x,
            "originating_approach": self.originating_approach,
            "movement": self.movement,
            "entry_lane": self.entry_lane,
            "exit_direction": self.exit_direction,
            "conflicting_approaches": list(self.conflicting_approaches),
            "requested_at_frame": self.requested_at_frame,
            "state": self.state,
            "expires_at_frame": self.expires_at_frame,
            "denial_or_cancel_reason": self.denial_or_cancel_reason,
            "tsp_requested": self.tsp_requested,
            "dbl_requested": self.dbl_requested,
            "attempt_number": self.attempt_number,
            "tsp_action": self.tsp_action,
            "tsp_adjust_frames": self.tsp_adjust_frames,
            "tsp_gate_reason": self.tsp_gate_reason,
            "dbl_revoke_reason": self.dbl_revoke_reason,
        }


@dataclass
class NodeState:
    phase: int = 0
    timer: int = 0
    priority_state: str = NORMAL
    priority_timer: int = 0
    active_request: PriorityRequest | None = None
    request_queue: list[PriorityRequest] = field(default_factory=list)
    reservations: dict[int, dict[str, Any]] = field(default_factory=dict)
    terminal_history: list[dict[str, Any]] = field(default_factory=list)
    suppressed_keys: set[tuple[str, int, int]] = field(default_factory=set)
    # Live TSP event: frames of adjustment applied so far (extension) or the
    # frame count the truncated green must reach before it ends.
    tsp_cap_frames: int = 0
    tsp_target_end: int = 0
    # Last completed TSP event, kept until the next one so a 10-frame
    # telemetry sample cannot miss a short truncation.
    last_tsp_action: str = TSP_ACTION_NONE
    last_tsp_adjust_frames: int = 0
    # approach -> (vehicle, first denied frame) for a left-turner held at the
    # bar by same-approach through traffic in the corner sweep. See
    # LEFT_TURN_STARVATION_FRAMES.
    left_turn_waiting: dict[str, tuple[Any, int]] = field(default_factory=dict)
    # AI Configured (apply_plan): green length per green phase {0: EW, 3: NS}
    # in frames while a plan is live (None = Webster), the approaches whose
    # lane 2 is commanded reserved, and a pending "end this green now".
    plan_green_frames: dict[int, int] | None = None
    dbl_commanded: set[str] = field(default_factory=set)
    cut_pending: bool = False
    # Coordination transition for the cycle in progress: frames added to (or,
    # negative, taken from) each green so the node returns to its offset.
    transition_adjust: dict[int, int] = field(default_factory=dict)


# Coordination transition: the most a single cycle may move either green
# towards the master schedule, as a fraction of that green. A bounded
# correction spread over several cycles, in the spirit of the "short-way"
# transition methods of NCHRP Report 812 (Signal Timing Manual, 2nd ed.),
# rather than a jump that would starve one phase to catch up in one go.
COORDINATION_MAX_ADJUST_FRACTION = 0.2

# A same-approach left turn yields to through traffic in the corner sweep
# (movements_conflict). Under a crawling box that stream never leaves a gap
# and the left-turner -- every R1/R2/R4/R5 bus at its first node -- sat at
# green for up to 40 s while TSP held cross traffic for nothing. After this
# many frames of denial, NEW through entries from that approach are held at
# the bar so the corner drains and the left turn goes; vehicles already in
# the box are untouched.
LEFT_TURN_STARVATION_FRAMES = 60
FRAMES_PER_SECOND = 60

# NOT a mechanism, a finding. Cutting a green short once its receiving lanes
# have spilled back (so it discharges nobody) was implemented and measured on
# 2026-09-22, seed 234, 15 min, benchmark regime: it fired 5 times and served
# 8,350 pax against 8,960 for the untreated run -- 6.8 % WORSE, with more
# vehicles standing at the end (221 vs 204). Truncating a blocked green
# removes the green from the movement that discharges first once the
# downstream link clears, and the cross street it hands the time to feeds the
# same saturated box. A saturated corridor wants offset/coordination between
# the nodes, not phase truncation. Do not re-add this without measuring it.


class SignalController:
    """Own independent signal and priority state for every intersection node."""

    def __init__(
        self,
        global_config,
        yellow_time=60,
        red_clearance_time=60,
        priority_request_timeout=7200,
        discharge_min_green=180,
        discharge_max_green=600,
        discharge_stall_time=180,
        discharge_queue_target=0,
        tsp_max_adjust_fraction=TSP_MAX_ADJUST_FRACTION,
        min_green_frames=MIN_GREEN_FRAMES,
    ):
        self.global_config = global_config
        self.yellow_time = max(1, int(yellow_time))
        self.red_clearance_time = max(1, int(red_clearance_time))
        self.priority_request_timeout = max(1, int(priority_request_timeout))
        self.tsp_max_adjust_fraction = max(0.0, min(1.0, float(tsp_max_adjust_fraction)))
        self.min_green_frames = max(1, int(min_green_frames))
        self.discharge_min_green = max(1, int(discharge_min_green))
        self.discharge_max_green = max(
            self.discharge_min_green, int(discharge_max_green)
        )
        self.discharge_stall_time = max(1, int(discharge_stall_time))
        self.discharge_queue_target = max(0, int(discharge_queue_target))
        self.reset_all_state()

    def reset_all_state(self):
        """Restore construction-time runtime state while preserving configuration."""
        try:
            eligibility_px = float(
                self.global_config.get("priority_eligibility_px", 500)
            )
        except (TypeError, ValueError, OverflowError):
            eligibility_px = 500.0
        # This episode snapshot changes only on construction or full reset, so
        # moving the UI slider cannot alter an already-running benchmark.
        self.priority_eligibility_px = max(
            PRIORITY_ELIGIBILITY_MIN_PX,
            min(PRIORITY_ELIGIBILITY_MAX_PX, eligibility_px),
        )
        if self.priority_eligibility_px != eligibility_px:
            log.warning(
                "priority_eligibility_px %.0f clamped to %.0f (link length %.0f px)",
                eligibility_px, self.priority_eligibility_px,
                PRIORITY_ELIGIBILITY_MAX_PX,
            )
        self.frame_number = 0
        self.nodes = {node_x: NodeState() for node_x in INT_X}
        # {"cycle": frames, "offsets": {node_x: frames}} once set_coordination
        # has run for this episode; None = independent nodes.
        self.coordination = None
        self._request_sequence = 0
        self._attempt_counts: dict[tuple[str, int, int], int] = {}
        self.experiment_metrics = {
            "tsp_requests_raised": 0,
            "tsp_requests_granted": 0,
            "tsp_requests_denied": 0,
            "tsp_denial_reasons": {},
            "tsp_actions_early_green": 0,
            "tsp_actions_extending": 0,
            "tsp_total_adjust_frames": 0,
            "dbl_requests_raised": 0,
            "dbl_requests_granted": 0,
            "dbl_requests_denied": 0,
            "dbl_denial_reasons": {},
            "dbl_actions_activated": 0,
            "dbl_total_active_frames": 0,
            "discharge_activations": 0,
            "discharge_total_frames": 0,
            "discharge_recovery_failed": False,
            "node_tsp_grants": {str(node_x): 0 for node_x in INT_X},
        }
        self.discharge_active = False
        self.discharge_mode = control_panel.DISCHARGE_AUTO
        self.discharge_state = DISCHARGE_INACTIVE
        self.discharge_timer = 0
        self.discharge_plan_name: str | None = None
        self.discharge_stage_index = 0
        self.discharge_reason = "Normal signal control is active"
        self.discharge_recommendation = (
            "Select Auto or a corridor, then start discharge"
        )
        self.discharge_vehicles_discharged = 0
        self.discharge_cycles = 0
        self._discharge_transition_signals = {
            node_x: {approach: RED for approach in ("EB", "WB", "NB", "SB")}
            for node_x in INT_X
        }
        self._discharge_green_map: dict[int, str] = {}
        self._discharge_stop_after_clearance = False
        self._discharge_completed = False
        self._discharge_last_progress_frame = 0
        self._discharge_wait_snapshot: dict[str, Any] | None = None
        self._discharge_tracked: dict[tuple[int, int], Any] = {}
        self._discharge_last_served = {
            plan_name: -self.discharge_max_green
            for plan_name in DISCHARGE_PLAN_STAGES
        }
        self._discharge_cycle_index = 0
        self._discharge_due_wait_frames = 0
        self._discharge_due_plan: str | None = None
        self.global_config["discharge_start_requested"] = False
        self.global_config["discharge_stop_requested"] = False
        self._publish_discharge_status()

    # Backward-compatible Node A getters plus broadcast setup setters.
    # Assigning phase/timer writes the same initial value into both distinct
    # NodeState objects; it does not create shared storage. Production callers
    # should use get_node_status() because runtime updates are per-node.
    @property
    def phase(self):
        return self.nodes[INT_X[0]].phase

    @phase.setter
    def phase(self, value):
        for state in self.nodes.values():
            state.phase = int(value) % 6
            state.timer = 0

    @property
    def timer(self):
        return self.nodes[INT_X[0]].timer

    @timer.setter
    def timer(self, value):
        for state in self.nodes.values():
            state.timer = int(value)

    @property
    def tsp_extension_timer(self):
        node = self.nodes[INT_X[0]]
        return node.priority_timer if node.priority_state == TSP_EXTENDING else 0

    @property
    def current_discharge_stage(self):
        stages = DISCHARGE_PLAN_STAGES.get(self.discharge_plan_name, ())
        if 0 <= self.discharge_stage_index < len(stages):
            return stages[self.discharge_stage_index]
        return None

    def is_discharge_active(self):
        return bool(self.discharge_active)

    def _discharge_display_status(self):
        if not self.discharge_active:
            return "COMPLETED" if self._discharge_completed else "IDLE"
        if self.discharge_state == DISCHARGE_ACTIVE:
            return "DISCHARGING"
        if self.discharge_state == DISCHARGE_WAITING:
            return "WAITING"
        if self.discharge_state == DISCHARGE_RECOVERY_FAILED:
            return "RECOVERY_FAILED"
        if self.discharge_state in (
            DISCHARGE_STOPPING_YELLOW,
            DISCHARGE_STOPPING_ALL_RED,
        ):
            return "STOPPING"
        return "TRANSITIONING"

    def _publish_discharge_status(self):
        stage = self.current_discharge_stage
        selected = self.discharge_plan_name or self.discharge_mode
        runtime = {
            "active": bool(self.discharge_active),
            "mode": self.discharge_mode,
            "selected": selected,
            "status": self._discharge_display_status(),
            "controller_state": self.discharge_state,
            "reason": self.discharge_reason,
            "recommendation": self.discharge_recommendation,
            "stage": stage.label if stage else "",
            "stage_index": self.discharge_stage_index if stage else None,
            "vehicles_discharged": self.discharge_vehicles_discharged,
            "cycles": self.discharge_cycles,
            "arrivals_suspended": bool(self.discharge_active),
            "priority_suspended": bool(self.discharge_active),
        }
        self.global_config["discharge_runtime"] = runtime
        return dict(runtime)

    def get_discharge_status(self):
        return self._publish_discharge_status()

    def reset_discharge(self):
        """Return only the network-recovery subsystem to its idle state."""
        self.discharge_active = False
        self.discharge_mode = control_panel.DISCHARGE_AUTO
        self.discharge_state = DISCHARGE_INACTIVE
        self.discharge_timer = 0
        self.discharge_plan_name = None
        self.discharge_stage_index = 0
        self.discharge_reason = "Normal signal control is active"
        self.discharge_recommendation = (
            "Select Auto or a corridor, then start discharge"
        )
        self.discharge_vehicles_discharged = 0
        self.discharge_cycles = 0
        self._discharge_green_map = {}
        self._discharge_stop_after_clearance = False
        self._discharge_completed = False
        self._discharge_wait_snapshot = None
        self._discharge_tracked.clear()
        self.global_config["discharge_start_requested"] = False
        self.global_config["discharge_stop_requested"] = False
        self._publish_discharge_status()

    def _matching_stage_vehicles(self, stage, vehicles):
        matches = []
        seen = set()
        for node_x, approach in stage.greens:
            for vehicle in vehicles or []:
                if id(vehicle) in seen or vehicle.direction != approach:
                    continue
                if node_x in getattr(vehicle, "passed_nodes", set()):
                    continue
                if vehicle.get_next_target_node(INT_X) != node_x:
                    continue
                if not vehicle.is_front_bumper_upstream(
                    node_x, H_Y, ROAD_W, STOP
                ):
                    continue
                matches.append(vehicle)
                seen.add(id(vehicle))
        return matches

    def _plan_vehicles(self, plan_name, vehicles):
        matches = []
        seen = set()
        for stage in DISCHARGE_PLAN_STAGES[plan_name]:
            for vehicle in self._matching_stage_vehicles(stage, vehicles):
                if id(vehicle) not in seen:
                    matches.append(vehicle)
                    seen.add(id(vehicle))
        return matches

    def _network_upstream_count(self, vehicles):
        count = 0
        for vehicle in vehicles or []:
            node_x = vehicle.get_next_target_node(INT_X)
            if node_x in getattr(vehicle, "passed_nodes", set()):
                continue
            if vehicle.is_front_bumper_upstream(node_x, H_Y, ROAD_W, STOP):
                count += 1
        return count

    def _first_relevant_stage_index(self, plan_name, vehicles):
        stages = DISCHARGE_PLAN_STAGES[plan_name]
        for index, stage in enumerate(stages):
            if (
                self._matching_stage_vehicles(stage, vehicles)
                or self._stage_same_direction_occupants(stage, vehicles)
            ):
                return index
        return None

    def _stage_same_direction_occupants(self, stage, vehicles):
        """Return box occupants that can exit under this protected stage."""
        occupants = []
        seen = set()
        for node_x, approach in stage.greens:
            for vehicle in vehicles or []:
                if id(vehicle) in seen or vehicle.direction != approach:
                    continue
                if self.vehicle_occupies_intersection(vehicle, node_x):
                    occupants.append(vehicle)
                    seen.add(id(vehicle))
        return occupants

    def _served_stage_vehicles(self, stage, vehicles):
        served = []
        seen = set()
        for vehicle in (
            self._matching_stage_vehicles(stage, vehicles)
            + self._stage_same_direction_occupants(stage, vehicles)
        ):
            if id(vehicle) not in seen:
                served.append(vehicle)
                seen.add(id(vehicle))
        return served

    def _stage_readiness(self, stage, vehicles):
        for node_x, approach in stage.greens:
            if not self.is_intersection_clear_for_greens(
                node_x, stage.greens, vehicles
            ):
                node_name = "Node A" if node_x == INT_X[0] else "Node B"
                return (
                    False,
                    f"{node_name} intersection is blocked by a "
                    "cross-direction vehicle",
                )

        matching = self._matching_stage_vehicles(stage, vehicles)
        exiting = self._stage_same_direction_occupants(stage, vehicles)
        if exiting:
            return True, "Same-direction box occupants can exit under this green"
        if not matching:
            return False, "No vehicles are waiting for this discharge stage"

        for vehicle in matching:
            node_x = vehicle.get_next_target_node(INT_X)
            if not vehicle.is_spillback_blocked(
                node_x, H_Y, ROAD_W, vehicles
            ):
                return True, "Receiving space and conflict clearance are available"

        node_x, approach = stage.greens[0]
        node_name = "Node A" if node_x == INT_X[0] else "Node B"
        direction_name = {
            "EB": "eastbound",
            "WB": "westbound",
            "NB": "northbound",
            "SB": "southbound",
        }[approach]
        location_name = (
            f"{node_name} {direction_name} exit"
            if (node_x, approach) in ((INT_X[1], "EB"), (INT_X[0], "WB"))
            else f"{node_name} {direction_name} receiving lane"
        )
        return (
            False,
            f"{location_name} has insufficient storage",
        )

    def _box_occupant_directions(self, vehicles):
        """Map each conflict box to current occupant travel directions."""
        result = {node_x: set() for node_x in INT_X}
        for node_x in INT_X:
            for vehicle in vehicles or []:
                if self.vehicle_occupies_intersection(vehicle, node_x):
                    result[node_x].add(vehicle.direction)
        return result

    def _dependency_bonus(self, plan_name):
        bonus = 0
        left_exit = {"EB": "NB", "WB": "SB", "NB": "WB", "SB": "EB"}
        for node_x, node in self.nodes.items():
            for reservation in node.reservations.values():
                approach = reservation["approach"]
                movement = reservation["movement"]
                exit_direction = (
                    left_exit[approach] if movement == "LEFT" else approach
                )
                if (
                    node_x == INT_X[0]
                    and exit_direction == "EB"
                    and plan_name == "Eastbound Corridor"
                ):
                    bonus += 10000
                if (
                    node_x == INT_X[1]
                    and exit_direction == "WB"
                    and plan_name == "Westbound Corridor"
                ):
                    bonus += 10000
        return bonus

    def _rank_discharge_candidates(self, vehicles, exclude=None):
        ranked = []
        for plan_name in DISCHARGE_PLAN_STAGES:
            if plan_name == exclude:
                continue
            stage_index = self._first_relevant_stage_index(plan_name, vehicles)
            if stage_index is None:
                continue
            stage = DISCHARGE_PLAN_STAGES[plan_name][stage_index]
            ready, readiness_reason = self._stage_readiness(stage, vehicles)
            queue_count = len(self._plan_vehicles(plan_name, vehicles))
            fairness = min(
                1000,
                max(0, self.frame_number - self._discharge_last_served[plan_name]),
            )
            score = self._dependency_bonus(plan_name) + queue_count * 100 + fairness
            ranked.append(
                {
                    "plan": plan_name,
                    "stage_index": stage_index,
                    "ready": ready,
                    "reason": readiness_reason,
                    "queue_count": queue_count,
                    "score": score,
                }
            )
        ranked.sort(
            key=lambda item: (
                not item["ready"],
                -item["score"],
                item["plan"],
            )
        )
        return ranked

    def _next_clockwise_candidate(self, vehicles):
        """Return the next servable plan in clockwise order, or None.

        The scan starts at the current cycle position so recovery works
        around the network one leg at a time. Legs with no waiting vehicles
        are skipped entirely. Among the legs that do have vehicles the first
        ready one wins, so a leg momentarily blocked by cross traffic cannot
        stall the whole rotation; if none is ready the due leg is returned so
        the caller waits on it under the existing stall timer.
        """
        order_length = len(DISCHARGE_CLOCKWISE_ORDER)
        servable = []
        for offset in range(order_length):
            index = (self._discharge_cycle_index + offset) % order_length
            plan_name = DISCHARGE_CLOCKWISE_ORDER[index]
            stage_index = self._first_relevant_stage_index(plan_name, vehicles)
            if stage_index is None:
                continue
            stage = DISCHARGE_PLAN_STAGES[plan_name][stage_index]
            ready, reason = self._stage_readiness(stage, vehicles)
            servable.append(
                {
                    "plan": plan_name,
                    "stage_index": stage_index,
                    "ready": ready,
                    "reason": reason,
                    "cycle_index": index,
                }
            )
        if not servable:
            self._discharge_due_plan = None
            self._discharge_due_wait_frames = 0
            return None

        due = servable[0]
        if due["plan"] != self._discharge_due_plan:
            self._discharge_due_plan = due["plan"]
            self._discharge_due_wait_frames = 0
        if due["ready"]:
            self._discharge_due_wait_frames = 0
            return due

        # Hold the rotation on the due leg while its block looks transient,
        # but always yield before the stall watchdog would fail recovery.
        self._discharge_due_wait_frames += 1
        grace = min(
            DISCHARGE_DUE_LEG_GRACE_FRAMES, max(0, self.discharge_stall_time // 2)
        )
        if self._discharge_due_wait_frames <= grace:
            return due
        return next(
            (item for item in servable if item["ready"]),
            due,
        )

    def _advance_discharge_cycle(self, completed_plan):
        """Move the clockwise cursor past the leg that just finished."""
        try:
            position = DISCHARGE_CLOCKWISE_ORDER.index(completed_plan)
        except ValueError:
            return
        self._discharge_cycle_index = (position + 1) % len(
            DISCHARGE_CLOCKWISE_ORDER
        )

    def _recommend_discharge_plan(self, vehicles, exclude=None):
        ranked = self._rank_discharge_candidates(vehicles, exclude=exclude)
        for candidate in ranked:
            if candidate["ready"]:
                return f"Discharge {candidate['plan']}"
        if ranked:
            return "Maintain arrival suspension while downstream traffic drains"
        return "Network approaches are clear; safely return to normal control"

    def _cancel_priority_for_discharge(self):
        for node in self.nodes.values():
            for request in list(node.request_queue):
                request.denial_or_cancel_reason = "NETWORK_DISCHARGE_STARTED"
                self._finalize_request(node, request, CANCELLED)
            node.request_queue.clear()
            if node.active_request:
                node.active_request.denial_or_cancel_reason = (
                    "NETWORK_DISCHARGE_STARTED"
                )
                self._finalize_request(node, node.active_request, CANCELLED)
            node.active_request = None
            node.priority_state = NORMAL
            node.priority_timer = 0

    def _capture_discharge_transition(self):
        self._discharge_transition_signals = {
            node_x: (
                self._discharge_signals_for_node(node_x)
                if self.discharge_active
                else self._base_signals_for_node(node)
            )
            for node_x, node in self.nodes.items()
        }

    def _start_discharge(self, mode, vehicles):
        if mode not in control_panel.DISCHARGE_OPTIONS:
            mode = control_panel.DISCHARGE_AUTO
        self._capture_discharge_transition()
        self._cancel_priority_for_discharge()
        self.discharge_active = True
        self.experiment_metrics["discharge_activations"] += 1
        self.discharge_mode = mode
        self.discharge_state = DISCHARGE_TRANSITION_YELLOW
        self.discharge_timer = 0
        self.discharge_plan_name = None if mode == control_panel.DISCHARGE_AUTO else mode
        self.discharge_stage_index = 0
        self.discharge_reason = (
            "Normal signal control and priority are transitioning to recovery"
        )
        self.discharge_recommendation = (
            "Hold all approaches while the protected discharge is prepared"
        )
        self.discharge_vehicles_discharged = 0
        self.discharge_cycles = 0
        self._discharge_green_map = {}
        self._discharge_stop_after_clearance = False
        self._discharge_completed = False
        self._discharge_last_progress_frame = self.frame_number
        self._discharge_wait_snapshot = None
        self._discharge_tracked.clear()
        self._publish_discharge_status()

    def _set_discharge_waiting(self, reason, recommendation):
        if self.discharge_state != DISCHARGE_WAITING:
            self._discharge_last_progress_frame = self.frame_number
            self._discharge_wait_snapshot = None
        self.discharge_state = DISCHARGE_WAITING
        self.discharge_reason = reason
        self.discharge_recommendation = recommendation
        self._discharge_green_map = {}

    def _waiting_progress_snapshot(self, vehicles):
        ranked = self._rank_discharge_candidates(vehicles)
        occupants = self._box_occupant_directions(vehicles)
        return {
            "upstream": self._network_upstream_count(vehicles),
            "reservations": sum(
                len(node.reservations) for node in self.nodes.values()
            ),
            "occupants": sum(len(items) for items in occupants.values()),
            "ready": frozenset(
                (item["plan"], item["stage_index"])
                for item in ranked
                if item["ready"]
            ),
        }

    @staticmethod
    def _waiting_snapshot_has_progress(previous, current):
        if previous is None:
            return False
        return (
            current["upstream"] < previous["upstream"]
            or current["reservations"] < previous["reservations"]
            or current["occupants"] < previous["occupants"]
            or bool(current["ready"] - previous["ready"])
        )

    def _recovery_failure_reason(self, vehicles):
        occupants = self._box_occupant_directions(vehicles)
        details = []
        for node_x in INT_X:
            directions = sorted(occupants[node_x])
            if not directions:
                continue
            node_name = "Node A" if node_x == INT_X[0] else "Node B"
            direction_text = "/".join(directions)
            needed = "/".join(f"{item} green" for item in directions)
            details.append(
                f"{node_name} blocked by {direction_text} vehicle; needs {needed}"
            )
        waited_seconds = (
            self.frame_number - self._discharge_last_progress_frame
        ) / 60.0
        blocker_text = "; ".join(details) or "No safe ready discharge stage"
        return (
            f"{blocker_text}. No safe ready stage after "
            f"{waited_seconds:.1f}s."
        )

    def _enter_recovery_failed(self, vehicles):
        self.experiment_metrics["discharge_recovery_failed"] = True
        self.discharge_state = DISCHARGE_RECOVERY_FAILED
        self.discharge_reason = self._recovery_failure_reason(vehicles)
        self.discharge_recommendation = (
            "RESET VEHICLES required - no safe discharge stage can drain "
            "the current blockage."
        )
        self._discharge_green_map = {}
        self._publish_discharge_status()

    def _activate_current_stage(self, vehicles):
        stage = self.current_discharge_stage
        if stage is None:
            return False
        ready, reason = self._stage_readiness(stage, vehicles)
        if not ready:
            self._set_discharge_waiting(
                reason,
                self._recommend_discharge_plan(
                    vehicles,
                    exclude=(
                        self.discharge_plan_name
                        if self.discharge_mode != control_panel.DISCHARGE_AUTO
                        else None
                    ),
                ),
            )
            return False

        self.discharge_state = DISCHARGE_ACTIVE
        self.discharge_timer = 0
        self._discharge_due_wait_frames = 0
        self._discharge_due_plan = None
        self._discharge_green_map = dict(stage.greens)
        self.discharge_reason = reason
        self.discharge_recommendation = (
            "Continue this protected movement until its stage completes"
        )
        self._discharge_last_progress_frame = self.frame_number
        self._discharge_wait_snapshot = None
        for node_x, _approach in stage.greens:
            for vehicle in self._served_stage_vehicles(stage, vehicles):
                if vehicle.get_next_target_node(INT_X) == node_x:
                    self._discharge_tracked[(id(vehicle), node_x)] = vehicle
        return True

    def _choose_or_wait_for_discharge(self, vehicles):
        if (
            self.discharge_mode == control_panel.DISCHARGE_AUTO
            and self._network_upstream_count(vehicles) <= self.discharge_queue_target
            and all(self.is_intersection_clear(node_x, vehicles) for node_x in INT_X)
        ):
            self._begin_safe_discharge_stop(
                "Network occupancy is below the recovery threshold"
            )
            return

        if self.discharge_plan_name is None:
            if self.discharge_mode == control_panel.DISCHARGE_AUTO:
                candidate = self._next_clockwise_candidate(vehicles)
                if candidate is None:
                    self._set_discharge_waiting(
                        "No queued approach currently requires a discharge green",
                        "Maintain arrival suspension while occupied lanes drain",
                    )
                    return
                self.discharge_plan_name = candidate["plan"]
                self.discharge_stage_index = candidate["stage_index"]
                if not candidate["ready"]:
                    self._set_discharge_waiting(
                        candidate["reason"],
                        "Maintain arrival suspension while downstream traffic drains",
                    )
                    return
            else:
                self.discharge_plan_name = self.discharge_mode
                stage_index = self._first_relevant_stage_index(
                    self.discharge_plan_name, vehicles
                )
                if stage_index is None:
                    self._begin_safe_discharge_stop(
                        f"{self.discharge_plan_name} is already clear"
                    )
                    return
                self.discharge_stage_index = stage_index

        stages = DISCHARGE_PLAN_STAGES[self.discharge_plan_name]
        while self.discharge_stage_index < len(stages):
            stage = stages[self.discharge_stage_index]
            if (
                self._matching_stage_vehicles(stage, vehicles)
                or self._stage_same_direction_occupants(stage, vehicles)
            ):
                self._activate_current_stage(vehicles)
                return
            self.discharge_stage_index += 1

        completed_plan = self.discharge_plan_name
        self._discharge_last_served[completed_plan] = self.frame_number
        self.discharge_cycles += 1
        if self.discharge_mode == control_panel.DISCHARGE_AUTO:
            self._advance_discharge_cycle(completed_plan)
            self.discharge_plan_name = None
            self.discharge_stage_index = 0
            self._set_discharge_waiting(
                f"{completed_plan} discharge is complete",
                self._recommend_discharge_plan(vehicles),
            )
        else:
            self._begin_safe_discharge_stop(
                f"{completed_plan} discharge is complete"
            )

    def _track_discharged_vehicles(self, vehicles):
        live_ids = {id(vehicle) for vehicle in vehicles or []}
        for key, vehicle in list(self._discharge_tracked.items()):
            _vehicle_id, node_x = key
            if id(vehicle) not in live_ids or node_x in vehicle.passed_nodes:
                self.discharge_vehicles_discharged += 1
                del self._discharge_tracked[key]

    def _finish_current_discharge_stage(self, reason):
        stage = self.current_discharge_stage
        if stage:
            self._discharge_last_served[self.discharge_plan_name] = self.frame_number
        self._capture_discharge_transition()
        self.discharge_state = DISCHARGE_STAGE_YELLOW
        self.discharge_timer = 0
        self.discharge_stage_index += 1
        self.discharge_reason = reason
        self.discharge_recommendation = (
            "Observe yellow and all-red before the next protected movement"
        )

    def _begin_safe_discharge_stop(self, reason):
        self._discharge_stop_after_clearance = True
        self.discharge_reason = reason
        self.discharge_recommendation = (
            "Return to normal control after all-red and empty conflict boxes"
        )
        if self.discharge_state == DISCHARGE_ACTIVE:
            self._capture_discharge_transition()
            self.discharge_state = DISCHARGE_STOPPING_YELLOW
        else:
            self.discharge_state = DISCHARGE_STOPPING_ALL_RED
        self.discharge_timer = 0
        self._discharge_green_map = {}

    def _finish_discharge(self):
        self.discharge_active = False
        self.discharge_state = DISCHARGE_INACTIVE
        self.discharge_timer = 0
        self.discharge_plan_name = None
        self.discharge_stage_index = 0
        self._discharge_green_map = {}
        self._discharge_stop_after_clearance = False
        self._discharge_completed = True
        self.discharge_reason = "Recovery ended through a safe all-red transition"
        self.discharge_recommendation = "Normal signal control has resumed"
        for node in self.nodes.values():
            node.phase = 0
            node.timer = 0
            node.priority_state = NORMAL
            node.priority_timer = 0
        self._publish_discharge_status()

    def _consume_discharge_commands(self, vehicles):
        start_requested = bool(
            self.global_config.get("discharge_start_requested", False)
        )
        stop_requested = bool(
            self.global_config.get("discharge_stop_requested", False)
        )
        if start_requested:
            self.global_config["discharge_start_requested"] = False
            mode = self.global_config.get(
                "discharge_selection", control_panel.DISCHARGE_AUTO
            )
            if not self.discharge_active:
                self._start_discharge(mode, vehicles)
            elif self.discharge_state == DISCHARGE_RECOVERY_FAILED:
                self.discharge_reason = (
                    "Recovery failed; use SAFE STOP or RESET VEHICLES before "
                    "starting another discharge"
                )
                self.discharge_recommendation = (
                    "RESET VEHICLES required - no safe discharge stage can "
                    "drain the current blockage."
                )
            else:
                self.discharge_mode = (
                    mode if mode in control_panel.DISCHARGE_OPTIONS
                    else control_panel.DISCHARGE_AUTO
                )
                self.discharge_plan_name = (
                    None
                    if self.discharge_mode == control_panel.DISCHARGE_AUTO
                    else self.discharge_mode
                )
                self.discharge_stage_index = 0
                self._discharge_stop_after_clearance = False
                if self.discharge_state == DISCHARGE_ACTIVE:
                    self._capture_discharge_transition()
                    self.discharge_state = DISCHARGE_STAGE_YELLOW
                    self.discharge_timer = 0
                    self._discharge_green_map = {}
                    self.discharge_reason = (
                        "Operator requested a different discharge selection"
                    )
                    self.discharge_recommendation = (
                        "Observe yellow and all-red before changing movement"
                    )
                else:
                    self.discharge_state = DISCHARGE_ALL_RED
                    self.discharge_timer = 0
                    self.discharge_reason = (
                        "Changing selection through protected all-red"
                    )
        if stop_requested:
            self.global_config["discharge_stop_requested"] = False
            if self.discharge_active:
                self._begin_safe_discharge_stop("Operator requested safe stop")

    def _update_discharge(self, vehicles):
        self._track_discharged_vehicles(vehicles)
        self.discharge_timer += 1
        if self.discharge_state == DISCHARGE_RECOVERY_FAILED:
            return
        if self.discharge_state == DISCHARGE_TRANSITION_YELLOW:
            if self.discharge_timer >= self.yellow_time:
                self.discharge_state = DISCHARGE_ALL_RED
                self.discharge_timer = 0
                self.discharge_reason = (
                    "All approaches are red before discharge selection"
                )
            return

        if self.discharge_state == DISCHARGE_STAGE_YELLOW:
            if self.discharge_timer >= self.yellow_time:
                self.discharge_state = DISCHARGE_ALL_RED
                self.discharge_timer = 0
                self._discharge_green_map = {}
            return

        if self.discharge_state == DISCHARGE_STOPPING_YELLOW:
            if self.discharge_timer >= self.yellow_time:
                self.discharge_state = DISCHARGE_STOPPING_ALL_RED
                self.discharge_timer = 0
                self._discharge_green_map = {}
            return

        if self.discharge_state == DISCHARGE_STOPPING_ALL_RED:
            if (
                self.discharge_timer >= self.red_clearance_time
                and all(
                    self.is_intersection_clear(node_x, vehicles)
                    for node_x in INT_X
                )
            ):
                self._finish_discharge()
            elif self.discharge_timer >= self.red_clearance_time:
                self.discharge_reason = (
                    "Safe stop is waiting for both conflict boxes to clear"
                )
            return

        if self.discharge_state == DISCHARGE_ALL_RED:
            if self.discharge_timer >= self.red_clearance_time:
                if self._discharge_stop_after_clearance:
                    self.discharge_state = DISCHARGE_STOPPING_ALL_RED
                    self.discharge_timer = 0
                else:
                    self._choose_or_wait_for_discharge(vehicles)
            return

        if self.discharge_state == DISCHARGE_WAITING:
            snapshot = self._waiting_progress_snapshot(vehicles)
            if self._waiting_snapshot_has_progress(
                self._discharge_wait_snapshot, snapshot
            ):
                self._discharge_last_progress_frame = self.frame_number
            self._discharge_wait_snapshot = snapshot
            waited = self.frame_number - self._discharge_last_progress_frame

            if self.discharge_mode == control_panel.DISCHARGE_AUTO:
                self.discharge_plan_name = None
                self.discharge_stage_index = 0
                self._choose_or_wait_for_discharge(vehicles)
                if (
                    waited >= self.discharge_stall_time
                    and self.discharge_state == DISCHARGE_WAITING
                ):
                    self._enter_recovery_failed(vehicles)
                return

            if waited >= self.discharge_stall_time:
                self._enter_recovery_failed(vehicles)
                return
            self._choose_or_wait_for_discharge(vehicles)
            return

        if self.discharge_state != DISCHARGE_ACTIVE:
            return

        stage = self.current_discharge_stage
        if stage is None:
            self.discharge_state = DISCHARGE_ALL_RED
            self.discharge_timer = 0
            return

        for node_x, _approach in stage.greens:
            for vehicle in self._served_stage_vehicles(stage, vehicles):
                if vehicle.get_next_target_node(INT_X) == node_x:
                    self._discharge_tracked.setdefault(
                        (id(vehicle), node_x), vehicle
                    )
        self._track_discharged_vehicles(vehicles)
        served = self._served_stage_vehicles(stage, vehicles)
        if any(vehicle.speed >= 0.25 for vehicle in served):
            self._discharge_last_progress_frame = self.frame_number

        if self.discharge_timer < self.discharge_min_green:
            return
        if not served:
            self._finish_current_discharge_stage(
                f"{stage.label} queue has cleared"
            )
            return
        if self.discharge_timer >= self.discharge_max_green:
            self._finish_current_discharge_stage(
                f"{stage.label} reached its protected maximum green"
            )
            return
        if (
            self.frame_number - self._discharge_last_progress_frame
            >= self.discharge_stall_time
        ):
            self._finish_current_discharge_stage(
                f"{stage.label} stopped making progress"
            )

    def _discharge_signals_for_node(self, node_x):
        result = {approach: RED for approach in ("EB", "WB", "NB", "SB")}
        if self.discharge_state in (
            DISCHARGE_TRANSITION_YELLOW,
            DISCHARGE_STAGE_YELLOW,
            DISCHARGE_STOPPING_YELLOW,
        ):
            previous = self._discharge_transition_signals.get(node_x, {})
            return {
                approach: (
                    YELLOW if previous.get(approach) in (GREEN, YELLOW) else RED
                )
                for approach in result
            }
        if self.discharge_state == DISCHARGE_ACTIVE:
            approach = self._discharge_green_map.get(node_x)
            if approach:
                result[approach] = GREEN
        return result

    # --- AI Configured: an externally written timing plan -------------------
    # The plan owns green lengths and the lane-2 flashers only. Yellow,
    # all-red, the intersection-clear wait, reservations and every collision
    # check stay here, and no plan at all means Webster (fail closed).

    def apply_plan(self, plan):
        """Run ``plan`` (guard.validate_signal_plan output, keyed by the
        string node x) from this frame: a running green longer than its new
        length ends now through yellow, a cut waits for the minimum green,
        and TSP requests in flight are cancelled -- the plan is the priority."""
        for node_x, node in self.nodes.items():
            node_plan = plan[str(node_x)]
            node.plan_green_frames = {
                0: int(node_plan["ew_green_sec"] * FRAMES_PER_SECOND),
                3: int(node_plan["ns_green_sec"] * FRAMES_PER_SECOND),
            }
            node.dbl_commanded = {
                approach for approach, on in node_plan["dbl"].items() if on
            }
            if node_plan["end_current_green_now"] and node.phase in (0, 3):
                node.cut_pending = True
            for request in [node.active_request, *node.request_queue]:
                if request is not None:
                    request.denial_or_cancel_reason = "PLAN_CONTROL"
                    self._finalize_request(node, request, CANCELLED)
            node.request_queue.clear()
            node.active_request = None
            node.priority_state = NORMAL

    def clear_plan(self):
        """Back to Webster timing and no commanded lanes (stale or invalid
        plan, disarm, reset)."""
        for node in self.nodes.values():
            node.plan_green_frames = None
            node.dbl_commanded = set()
            node.cut_pending = False

    @property
    def plan_active(self):
        return any(node.plan_green_frames for node in self.nodes.values())

    def get_plan(self):
        """The live plan per string node x (telemetry), or None."""
        if not self.plan_active:
            return None
        return {
            str(node_x): {
                "ew_green_sec": node.plan_green_frames[0] / FRAMES_PER_SECOND,
                "ns_green_sec": node.plan_green_frames[3] / FRAMES_PER_SECOND,
                "dbl": sorted(node.dbl_commanded),
            }
            for node_x, node in self.nodes.items()
        }

    def dbl_excludes_left_turns(self, target_node_x, direction):
        """A bus-requested DBL keeps left-turners out of lane 2 (they hold in
        a general lane); a commanded lane is shared with them."""
        node = self.nodes.get(target_node_x)
        if node and direction in node.dbl_commanded:
            return False
        return self.is_dbl_active_for_approach(target_node_x, direction)

    def get_green_time(self, node_x=None, phase=0):
        """Green duration in frames for one node's current phase.

        A live plan (apply_plan) wins; else Webster splits are per node and
        per phase, so EW and NS no longer share a single slider value. Falls
        back to the legacy green_time when no calibration has run yet. Under
        coordination the cycle's transition adjustment is included, so TSP
        feasibility, extension caps and telemetry all read the green the
        node will actually run.
        """
        node = self.nodes.get(node_x)
        if node is not None and node.plan_green_frames:
            return node.plan_green_frames[3 if phase == 3 else 0]
        base = self._base_green_frames(node_x, phase)
        if node is not None and self.coordination:
            base += node.transition_adjust.get(3 if phase == 3 else 0, 0)
        return max(1, int(base))

    def _base_green_frames(self, node_x, phase):
        splits = self.global_config.get("webster_splits") or {}
        node_split = splits.get(node_x) or splits.get(str(node_x))
        if node_split:
            key = "NS_green_frames" if phase == 3 else "EW_green_frames"
            frames = node_split.get(key)
            if isinstance(frames, (int, float)) and frames > 0:
                return max(1, int(frames))
        return max(1, int(self.global_config.get("green_time", 240)))

    # --- coordination -----------------------------------------------------
    #
    # Coordinated operation: both nodes run one common cycle, and each node's
    # EW green is scheduled to start at its offset against a master clock
    # (this controller's frame_number). A node is placed in its cycle at
    # reset so it starts on schedule, and every time it begins an EW green it
    # compares the start with the schedule and spreads the error over that
    # cycle's two greens, within COORDINATION_MAX_ADJUST_FRACTION. The same
    # correction is what returns a node to coordination after a TSP green
    # extension or early green, an all-red that waited for a blocked box, or
    # a discharge episode -- the "recovery" transit priority requires.

    def set_coordination(self, cycle_frames, offsets):
        if not cycle_frames:
            self.coordination = None
            return
        cycle = int(cycle_frames)
        self.coordination = {
            "cycle": cycle,
            "offsets": {int(k): int(v) % cycle for k, v in (offsets or {}).items()},
        }
        for node_x, node in self.nodes.items():
            offset = self.coordination["offsets"].get(node_x, 0)
            self._place_in_cycle(node_x, node, (-offset) % cycle)

    def _phase_durations(self, node_x):
        return [
            self._base_green_frames(node_x, 0), self.yellow_time, self.red_clearance_time,
            self._base_green_frames(node_x, 3), self.yellow_time, self.red_clearance_time,
        ]

    def _place_in_cycle(self, node_x, node, position):
        durations = self._phase_durations(node_x)
        position = int(position) % max(1, sum(durations))
        for phase, duration in enumerate(durations):
            if position < duration:
                node.phase, node.timer = phase, position
                return
            position -= duration
        node.phase, node.timer = 0, 0

    def _coordinate(self, node_x, node):
        """Set this cycle's transition so the node heads back to its offset."""
        node.transition_adjust = {}
        if not self.coordination or self.plan_active or self.discharge_active:
            return
        cycle = self.coordination["cycle"]
        offset = self.coordination["offsets"].get(node_x, 0)
        error = (self.frame_number - offset) % cycle
        if error > cycle / 2:
            error -= cycle          # negative: started early
        need = -error               # frames this cycle must gain (or lose)
        if need == 0:
            return
        green = {0: self._base_green_frames(node_x, 0), 3: self._base_green_frames(node_x, 3)}
        adjust = {}
        remaining = float(need)
        for phase in (0, 3):
            share = need * green[phase] / float(green[0] + green[3]) if phase == 0 else remaining
            cap = COORDINATION_MAX_ADJUST_FRACTION * green[phase]
            floor = -min(cap, max(0, green[phase] - self.min_green_frames))
            adjust[phase] = int(round(max(floor, min(cap, share))))
            remaining -= adjust[phase]
        node.transition_adjust = adjust

    @staticmethod
    def _normal_signals_for_phase(phase):
        result = {"EB": RED, "WB": RED, "NB": RED, "SB": RED}
        if phase == 0:
            result["EB"] = result["WB"] = GREEN
        elif phase == 1:
            result["EB"] = result["WB"] = YELLOW
        elif phase == 3:
            result["NB"] = result["SB"] = GREEN
        elif phase == 4:
            result["NB"] = result["SB"] = YELLOW
        return result

    @staticmethod
    def _vehicle_bounds(vehicle):
        if vehicle.direction in ("EB", "WB"):
            return (
                vehicle.x - vehicle.length / 2.0,
                vehicle.y - vehicle.width / 2.0,
                vehicle.x + vehicle.length / 2.0,
                vehicle.y + vehicle.width / 2.0,
            )
        return (
            vehicle.x - vehicle.width / 2.0,
            vehicle.y - vehicle.length / 2.0,
            vehicle.x + vehicle.width / 2.0,
            vehicle.y + vehicle.length / 2.0,
        )

    def vehicle_occupies_intersection(self, vehicle, node_x):
        half_w = ROAD_W / 2.0
        min_x, max_x = node_x - half_w, node_x + half_w
        min_y, max_y = H_Y - half_w, H_Y + half_w
        vx1, vy1, vx2, vy2 = self._vehicle_bounds(vehicle)
        return vx1 < max_x and vx2 > min_x and vy1 < max_y and vy2 > min_y

    def is_intersection_clear_for_greens(
        self, node_x, greens, vehicles, road_w=ROAD_W, h_y=H_Y
    ):
        """Apply direction-aware conflict-box clearance during discharge only."""
        half_w = road_w / 2.0
        min_x, max_x = node_x - half_w, node_x + half_w
        min_y, max_y = h_y - half_w, h_y + half_w
        greened_dirs = {
            approach for green_node_x, approach in greens
            if green_node_x == node_x
        }
        for vehicle in vehicles or []:
            vx1, vy1, vx2, vy2 = self._vehicle_bounds(vehicle)
            overlaps = (
                vx1 < max_x
                and vx2 > min_x
                and vy1 < max_y
                and vy2 > min_y
            )
            if not overlaps:
                continue
            if vehicle.direction in greened_dirs:
                continue
            return False
        return True

    def is_intersection_clear(self, int_x, vehicles, road_w=ROAD_W, h_y=H_Y):
        half_w = road_w / 2.0
        min_x, max_x = int_x - half_w, int_x + half_w
        min_y, max_y = h_y - half_w, h_y + half_w
        for vehicle in vehicles or []:
            vx1, vy1, vx2, vy2 = self._vehicle_bounds(vehicle)
            if vx1 < max_x and vx2 > min_x and vy1 < max_y and vy2 > min_y:
                return False
        return True

    @staticmethod
    def _axis(direction):
        return "EW" if direction in ("EB", "WB") else "NS"

    @classmethod
    def movements_conflict(cls, approach_a, movement_a, approach_b, movement_b):
        # Perpendicular axes always conflict. The normal controller clears the
        # box under all-red before changing which axis receives green.
        if cls._axis(approach_a) != cls._axis(approach_b):
            return True

        # Opposing approaches on the same axis use physically separated paths.
        # On one approach, however, a long square-corner left turn briefly
        # sweeps the adjacent through lane. The dynamic entry check below can
        # release through traffic as soon as that small shared area is clear.
        return (
            approach_a == approach_b
            and (movement_a == "LEFT" or movement_b == "LEFT")
        )

    @staticmethod
    def _corner_sweep_conflict(entry_vehicle, entry_movement, other_vehicle, other_movement):
        """Whether two vehicles of one approach, one of them turning near-side,
        really share road in the box.

        The turn pivots at a square corner, so its body swings half its length
        either side of its lane centre: past the lane edge only when it is
        longer than a lane is wide (LANE, 5.5 m) -- a truck by 0.75 m, a bus
        by 2.5 m, a car not at all. Only then does it sweep the neighbouring
        lane. Treating every near-side turn as blocking every through lane of
        its approach stopped the whole approach for each turning car: at 0.6x
        demand, below Webster capacity, queues reached the boundary on every
        source (2026-09-23 audit). Same-lane pairs keep the conservative
        answer; car following orders them anyway."""
        if "LEFT" not in (entry_movement, other_movement):
            return True
        turner = entry_vehicle if entry_movement == "LEFT" else other_vehicle
        lane_gap = abs(
            int(getattr(entry_vehicle, "lane_index", 0))
            - int(getattr(other_vehicle, "lane_index", 0))
        )
        if lane_gap == 0:
            return True
        return lane_gap == 1 and float(getattr(turner, "length", 0.0)) > LANE

    def _left_turn_cleared_adjacent_through_lane(
        self, vehicle, originating_approach, node_x
    ):
        """Whether a completed corner pivot no longer sweeps a through lane."""
        if vehicle.direction == originating_approach:
            return False
        vx1, vy1, vx2, vy2 = self._vehicle_bounds(vehicle)
        if originating_approach == "EB":
            return vy2 <= H_Y - 2 * LANE
        if originating_approach == "WB":
            return vy1 >= H_Y + 2 * LANE
        if originating_approach == "NB":
            return vx2 <= node_x - 2 * LANE
        if originating_approach == "SB":
            return vx1 >= node_x + 2 * LANE
        return False

    def _straight_vehicle_cleared_left_turn_corner(
        self, vehicle, originating_approach, node_x
    ):
        """Whether a through vehicle's rear has left the corner turn sweep."""
        if vehicle.direction != originating_approach:
            return False
        vx1, vy1, vx2, vy2 = self._vehicle_bounds(vehicle)
        if originating_approach == "EB":
            return vx1 >= node_x - 2 * LANE
        if originating_approach == "WB":
            return vx2 <= node_x + 2 * LANE
        if originating_approach == "NB":
            return vy2 <= H_Y + 2 * LANE
        if originating_approach == "SB":
            return vy1 >= H_Y - 2 * LANE
        return False

    def _vehicle_blocks_entry(
        self,
        entry_approach,
        entry_movement,
        other_vehicle,
        other_approach,
        other_movement,
        node_x,
        entry_vehicle=None,
    ):
        if not self.movements_conflict(
            entry_approach,
            entry_movement,
            other_approach,
            other_movement,
        ):
            return False
        if (
            entry_vehicle is not None
            and entry_approach == other_approach
            and not self._corner_sweep_conflict(
                entry_vehicle, entry_movement, other_vehicle, other_movement
            )
        ):
            return False

        # Once an existing left-turner has cleared the shared corner, following
        # straight traffic or another left-turner may enter under the same
        # green. The follower still observes normal vehicle spacing after it
        # pivots onto the leader's exit lane.
        if (
            entry_approach == other_approach
            and entry_movement in ("STRAIGHT", "LEFT")
            and other_movement == "LEFT"
            and self._left_turn_cleared_adjacent_through_lane(
                other_vehicle, other_approach, node_x
            )
        ):
            return False

        # Conversely, a new left-turner can enter once an existing through
        # vehicle's rear has cleared the small corner sweep area.
        if (
            entry_approach == other_approach
            and entry_movement == "LEFT"
            and other_movement == "STRAIGHT"
            and self._straight_vehicle_cleared_left_turn_corner(
                other_vehicle, other_approach, node_x
            )
        ):
            return False
        return True

    def _movement_for_vehicle(self, vehicle, node_x):
        stored = getattr(vehicle, "intersection_entry_movements", {}).get(node_x)
        if stored:
            return stored
        if isinstance(vehicle, Bus):
            leg = vehicle.get_active_route_leg(INT_X)
            if leg and leg["node_x"] == node_x:
                return leg["movement"]
        return getattr(vehicle, "target_turn", "STRAIGHT")

    def _approach_for_vehicle(self, vehicle, node_x):
        return getattr(vehicle, "intersection_entry_approaches", {}).get(
            node_x, vehicle.direction
        )

    def _cleanup_reservations(self, vehicles):
        live_ids = {id(vehicle) for vehicle in vehicles or []}
        for node_x, node in self.nodes.items():
            for vehicle_key, reservation in list(node.reservations.items()):
                vehicle = reservation["vehicle"]
                if (
                    vehicle_key not in live_ids
                    or node_x in getattr(vehicle, "passed_nodes", set())
                ):
                    del node.reservations[vehicle_key]

    def entry_would_be_granted(self, vehicle, node_x, vehicles):
        """Whether request_intersection_entry would grant this vehicle entry
        now, with no side effects. Lets a vehicle still inside its braking
        distance see that the box will not open and brake comfortably, the
        way a SUMO vehicle sees a link's state well before reaching it --
        instead of learning it at the 25 px booking distance, where the only
        way to stop is harder than any road vehicle can brake (78 of 130
        braking-bound events in a 2-minute audit run were exactly that)."""
        node = self.nodes.get(node_x)
        if node is None:
            return False
        if id(vehicle) in node.reservations:
            return True
        return self._entry_blocker(vehicle, node, node_x, vehicles) is None

    def _entry_blocker(self, vehicle, node, node_x, vehicles):
        """None when nothing blocks this vehicle's entry, else the
        (approach, movement) of what does ("DISCHARGE"/"STARVED" for the
        non-vehicle reasons)."""
        approach = vehicle.direction
        movement = self._movement_for_vehicle(vehicle, node_x)
        if self.discharge_active:
            if (
                self.discharge_state != DISCHARGE_ACTIVE
                or self._discharge_green_map.get(node_x) != approach
            ):
                return ("DISCHARGE", None)
        if movement == "STRAIGHT" and self._left_turn_starved(node, approach, node_x, vehicles):
            # Only a through vehicle the waiting turner would actually sweep
            # is held; the other lanes keep flowing.
            waiting = node.left_turn_waiting[approach][0]
            if self._corner_sweep_conflict(vehicle, movement, waiting, "LEFT"):
                return ("STARVED", None)
        for reservation in node.reservations.values():
            if self._vehicle_blocks_entry(
                approach, movement, reservation["vehicle"],
                reservation["approach"], reservation["movement"], node_x,
                entry_vehicle=vehicle,
            ):
                return (reservation["approach"], reservation["movement"])
        # Protect against an unregistered vehicle placed inside the box by a
        # test, reset, or legacy caller.
        for other in vehicles or []:
            if other is vehicle or not self.vehicle_occupies_intersection(other, node_x):
                continue
            other_approach = self._approach_for_vehicle(other, node_x)
            other_movement = self._movement_for_vehicle(other, node_x)
            if self._vehicle_blocks_entry(
                approach, movement, other, other_approach, other_movement, node_x,
                entry_vehicle=vehicle,
            ):
                return (other_approach, other_movement)
        return None

    def request_intersection_entry(self, vehicle, node_x, vehicles):
        """Reserve a movement before a vehicle crosses the stop bar."""
        node = self.nodes.get(node_x)
        if node is None:
            return False
        vehicle_key = id(vehicle)
        if vehicle_key in node.reservations:
            return True

        approach = vehicle.direction
        movement = self._movement_for_vehicle(vehicle, node_x)
        blocker = self._entry_blocker(vehicle, node, node_x, vehicles)
        if blocker is not None:
            if movement == "LEFT" and blocker[0] == approach:
                node.left_turn_waiting.setdefault(approach, (vehicle, self.frame_number))
            return False

        if movement == "LEFT":
            node.left_turn_waiting.pop(approach, None)
        node.reservations[vehicle_key] = {
            "vehicle": vehicle,
            "approach": approach,
            "movement": movement,
            "reserved_at_frame": self.frame_number,
        }
        vehicle.intersection_entry_approaches[node_x] = approach
        vehicle.intersection_entry_movements[node_x] = movement
        return True

    def _left_turn_starved(self, node, approach, node_x, vehicles):
        """True while a left-turner from ``approach`` has been denied the
        corner for LEFT_TURN_STARVATION_FRAMES and is still waiting at the
        bar; a stale entry (vehicle gone, or through the node) is dropped."""
        waiting = node.left_turn_waiting.get(approach)
        if waiting is None:
            return False
        left_vehicle, since = waiting
        alive = any(other is left_vehicle for other in vehicles or [])
        if not alive or node_x in getattr(left_vehicle, "passed_nodes", set()):
            node.left_turn_waiting.pop(approach, None)
            return False
        return self.frame_number - since >= LEFT_TURN_STARVATION_FRAMES

    def cancel_intersection_entry(self, vehicle, node_x):
        """Release a reservation while the vehicle is still upstream."""
        node = self.nodes.get(node_x)
        # Deliberately leaves left_turn_waiting alone: a denied vehicle
        # cancels every frame it sits stopped, which would erase the wait it
        # just registered. The entry clears itself when the turn is granted,
        # or in _left_turn_starved once the vehicle is gone or through.
        if node is not None:
            node.reservations.pop(id(vehicle), None)
        # A released booking leaves no movement behind while the vehicle is
        # still short of the box: the next request must be judged on what it
        # does now (a missed turn goes straight), not on the old booking. In
        # the box the stored movement is what identifies a turn mid-pivot.
        if node_x in getattr(vehicle, "intersection_entry_movements", {}) and hasattr(
            vehicle, "is_front_bumper_upstream"
        ) and vehicle.is_front_bumper_upstream(node_x, H_Y, ROAD_W, STOP):
            getattr(vehicle, "intersection_entry_movements", {}).pop(node_x, None)
            getattr(vehicle, "intersection_entry_approaches", {}).pop(node_x, None)

    def distance_to_node_stop_bar(self, vehicle, node_x):
        return vehicle.distance_to_node_stop_bar(
            node_x, h_y=H_Y, road_w=ROAD_W, stop_offset=STOP
        )

    def get_priority_eligibility_px(self):
        """Eligibility distance frozen for the current simulation episode."""
        return self.priority_eligibility_px

    @staticmethod
    def _live_route_config(bus):
        live_cfg = control_panel.bus_routes_config.get(bus.route_id)
        return live_cfg if live_cfg is not None else bus.route_info

    def is_bus_tsp_eligible(self, bus, target_node):
        if not isinstance(bus, Bus):
            return False
        live_cfg = self._live_route_config(bus)
        if not live_cfg.get("tsp_enabled", False):
            return False
        leg = bus.get_active_route_leg(INT_X)
        if not leg or leg["node_x"] != target_node:
            return False
        # Near-side stop: priority check-in waits until the bus has served
        # it. A request raised before the dwell would spend the green on a
        # standing bus -- why TSP deployments detect buses downstream of a
        # near-side stop (FTA/ITS America, Transit Signal Priority: A Planning
        # and Implementation Handbook, 2005).
        if getattr(bus, "near_side_stop_pending", None) == target_node:
            return False
        dist = self.distance_to_node_stop_bar(bus, target_node)
        return 0 <= dist <= self.get_priority_eligibility_px()

    def is_dbl_requested_for_bus_leg(self, bus, target_node):
        """Return whether live route configuration requests DBL on this leg."""
        if not isinstance(bus, Bus):
            return False
        live_cfg = self._live_route_config(bus)
        node = self.nodes.get(target_node)
        commanded = bool(node and bus.direction in node.dbl_commanded)
        if not live_cfg.get("dbl_enabled", False) and not commanded:
            return False
        leg = bus.get_active_route_leg(INT_X)
        return bool(leg and leg["node_x"] == target_node)

    def is_dbl_enabled_for_bus_leg(self, bus, target_node, all_vehicles=None):
        """Return live DBL intent only when the current lane is usable."""
        if not self.is_dbl_requested_for_bus_leg(bus, target_node):
            return False
        if all_vehicles is not None and dbl_lane_is_obstructed(
            bus,
            all_vehicles,
            H_Y,
            LANE,
            target_node=target_node,
        ):
            return False
        return True

    def is_bus_dbl_eligible(self, bus, target_node, all_vehicles=None):
        if not self.is_dbl_enabled_for_bus_leg(bus, target_node, all_vehicles):
            return False
        if getattr(bus, "dbl_merge_abandoned_for_leg", False):
            return False
        leg = bus.get_active_route_leg(INT_X)
        if bus.lane_index != DBL_LANE_INDEX:
            return False
        dist = self.distance_to_node_stop_bar(bus, target_node)
        return 0 <= dist <= self.get_priority_eligibility_px()

    @staticmethod
    def _request_key(request):
        return (request.bus_id, request.node_x, request.route_leg_index)

    def _has_request(self, node, key):
        if node.active_request and self._request_key(node.active_request) == key:
            return True
        return any(self._request_key(item) == key for item in node.request_queue)

    def _build_request(self, bus, leg, tsp_requested, dbl_requested):
        self._request_sequence += 1
        key = (bus.bus_id, leg["node_x"], leg["route_leg_index"])
        attempt_number = self._attempt_counts.get(key, 0) + 1
        self._attempt_counts[key] = attempt_number
        approach = leg["approach"]
        conflicts = tuple(
            item for item in ("EB", "WB", "NB", "SB") if item != approach
        )
        if tsp_requested:
            self.experiment_metrics["tsp_requests_raised"] += 1
        if dbl_requested:
            self.experiment_metrics["dbl_requests_raised"] += 1
        return PriorityRequest(
            request_id=f"PRIORITY_{self._request_sequence:06d}",
            bus=bus,
            bus_id=bus.bus_id,
            route_id=bus.route_id,
            route_leg_index=leg["route_leg_index"],
            node_x=leg["node_x"],
            originating_approach=approach,
            movement=leg["movement"],
            entry_lane=DBL_LANE_INDEX if dbl_requested else leg["entry_lane"],
            exit_direction=leg["exit_direction"],
            conflicting_approaches=conflicts,
            requested_at_frame=self.frame_number,
            expires_at_frame=self.frame_number + self.priority_request_timeout,
            tsp_requested=tsp_requested,
            dbl_requested=dbl_requested,
            attempt_number=attempt_number,
        )

    def _collect_priority_requests(self, vehicles):
        eligible_keys = {node_x: set() for node_x in self.nodes}
        for vehicle in vehicles or []:
            if not isinstance(vehicle, Bus):
                continue
            leg = vehicle.get_active_route_leg(INT_X)
            if not leg:
                continue
            node_x = leg["node_x"]
            tsp_requested = self.is_bus_tsp_eligible(vehicle, node_x)
            dbl_requested = self.is_bus_dbl_eligible(vehicle, node_x, vehicles)
            if not (tsp_requested or dbl_requested):
                continue
            node = self.nodes[node_x]
            key = (vehicle.bus_id, node_x, leg["route_leg_index"])
            eligible_keys[node_x].add(key)
            if key in node.suppressed_keys or self._has_request(node, key):
                continue
            request = self._build_request(
                vehicle, leg, tsp_requested=tsp_requested, dbl_requested=dbl_requested
            )
            node.request_queue.append(request)
            node.request_queue.sort(
                key=lambda item: (item.requested_at_frame, item.bus_id)
            )

        # A terminal request cannot continuously renew while the same bus is
        # still sitting in the eligibility zone. It becomes eligible for a new
        # attempt only after leaving that zone (or disabling the feature) for
        # at least one controller update.
        for node_x, node in self.nodes.items():
            node.suppressed_keys.intersection_update(eligible_keys[node_x])

    def _request_snapshot(self, request):
        snapshot = request.as_dict()
        snapshot["wait_frames"] = max(
            0, self.frame_number - request.requested_at_frame
        )
        return snapshot

    def _finalize_request(self, node, request, terminal_state):
        if not request.metrics_finalized:
            reason = (
                request.denial_or_cancel_reason
                or request.tsp_gate_reason
                or terminal_state
            )
            if request.tsp_requested:
                if request.tsp_action != TSP_ACTION_NONE:
                    self.experiment_metrics["tsp_requests_granted"] += 1
                    self.experiment_metrics["tsp_total_adjust_frames"] += max(
                        0, int(request.tsp_adjust_frames or 0)
                    )
                    action_key = (
                        "tsp_actions_early_green"
                        if request.tsp_action == TSP_ACTION_EARLY_GREEN
                        else "tsp_actions_extending"
                    )
                    self.experiment_metrics[action_key] += 1
                    node_key = str(request.node_x)
                    grants = self.experiment_metrics["node_tsp_grants"]
                    grants[node_key] = grants.get(node_key, 0) + 1
                else:
                    self.experiment_metrics["tsp_requests_denied"] += 1
                    reasons = self.experiment_metrics["tsp_denial_reasons"]
                    reasons[reason] = reasons.get(reason, 0) + 1
            if request.dbl_requested:
                if request.dbl_granted:
                    self.experiment_metrics["dbl_requests_granted"] += 1
                    self.experiment_metrics["dbl_actions_activated"] += 1
                    self.experiment_metrics["dbl_total_active_frames"] += max(
                        0, self.frame_number - request.requested_at_frame
                    )
                else:
                    self.experiment_metrics["dbl_requests_denied"] += 1
                    reasons = self.experiment_metrics["dbl_denial_reasons"]
                    reasons[reason] = reasons.get(reason, 0) + 1
            request.metrics_finalized = True
        request.state = terminal_state
        snapshot = self._request_snapshot(request)
        snapshot["terminal_frame"] = self.frame_number
        node.terminal_history.append(snapshot)
        if len(node.terminal_history) > 50:
            del node.terminal_history[:-50]
        node.suppressed_keys.add(self._request_key(request))

    def _request_is_live(self, request, vehicles):
        if request.bus not in (vehicles or []):
            request.denial_or_cancel_reason = "BUS_REMOVED"
            return False
        if self.frame_number > request.expires_at_frame:
            request.denial_or_cancel_reason = "REQUEST_TIMEOUT"
            return False
        leg = request.bus.get_active_route_leg(INT_X)
        if not leg:
            request.denial_or_cancel_reason = "ROUTE_COMPLETE"
            return False
        if (
            leg["node_x"] != request.node_x
            or leg["route_leg_index"] != request.route_leg_index
        ):
            request.denial_or_cancel_reason = "ROUTE_LEG_CHANGED"
            return False
        live_cfg = self._live_route_config(request.bus)
        if not (
            (request.tsp_requested and live_cfg.get("tsp_enabled", False))
            or (request.dbl_requested and live_cfg.get("dbl_enabled", False))
        ):
            request.denial_or_cancel_reason = "FEATURE_DISABLED"
            return False
        # The gate's lane check, re-run every frame the request lives: a
        # vehicle that was moving when DBL was accepted and has since stopped
        # ahead of the bus makes the reserved lane useless, so DBL is revoked
        # -- the request ends if it was DBL only, else it carries on as TSP.
        if request.dbl_requested and dbl_lane_queue_ahead(
            request.bus, vehicles, H_Y, target_node=request.node_x, lane_w=LANE
        ):
            request.dbl_revoke_reason = DBL_REVOKED_LANE_BLOCKED
            if not request.tsp_requested:
                request.denial_or_cancel_reason = DBL_REVOKED_LANE_BLOCKED
                return False
            request.dbl_requested = request.dbl_granted = False
            request.entry_lane = leg["entry_lane"]
        return True

    def _normal_phase_update(self, node, vehicles, node_x):
        node.timer += 1
        if node.phase not in (0, 3):
            node.cut_pending = False
        elif node.cut_pending and node.timer >= self.min_green_frames:
            node.cut_pending = False
            self._advance_phase(node)
            return
        if node.phase in (0, 3):
            maximum = self.get_green_time(node_x, node.phase)
        elif node.phase in (1, 4):
            maximum = self.yellow_time
        else:
            maximum = self.red_clearance_time
        if node.timer < maximum:
            return
        if node.phase in (2, 5) and not self.is_intersection_clear(node_x, vehicles):
            return
        self._advance_phase(node)

    @staticmethod
    def _advance_phase(node):
        node.timer = 0
        node.phase = (node.phase + 1) % 6

    @staticmethod
    def _phase_for_approach(approach):
        return 0 if approach in ("EB", "WB") else 3

    @staticmethod
    def _conflicting_phase(phase):
        return 3 if phase == 0 else 0

    def _tsp_cap_frames(self, node_x, phase):
        return int(self.get_green_time(node_x, phase) * self.tsp_max_adjust_fraction)

    @staticmethod
    def _bus_can_use_grant(request, node_x, vehicles):
        """Return whether the requested bus is physically positioned to proceed."""
        bus = request.bus
        leg = bus.get_active_route_leg(INT_X)
        return bool(
            bus in (vehicles or [])
            and leg
            and leg["node_x"] == node_x
            and leg["route_leg_index"] == request.route_leg_index
            and bus.lane_index == request.entry_lane
            and not getattr(bus, "must_hold_for_lane", False)
        )

    def _request_can_start_tsp(self, request, node_x, vehicles):
        """Feasibility gate: one bounded action per request, only for a bus
        that can actually use it, is still upstream of the stop bar, and has
        room in the lane its movement enters (re-checked every frame; the
        request stays armed and a bus that crosses untreated is DENIED with
        the gate reason)."""
        if not (
            request.tsp_requested
            and request.tsp_action == TSP_ACTION_NONE
            and self._bus_can_use_grant(request, node_x, vehicles)
            and request.bus.is_front_bumper_upstream(node_x, H_Y, ROAD_W, STOP)
        ):
            return False
        bus = request.bus
        if bus.receiving_space_px(node_x, vehicles, H_Y, ROAD_W, LANE) < bus.length + SAFE_GAP_PX:
            request.tsp_gate_reason = TSP_DENY_DOWNSTREAM_BLOCKED
            return False
        if request.tsp_gate_reason == TSP_DENY_DOWNSTREAM_BLOCKED:
            request.tsp_gate_reason = ""
        return True

    def _terminal_state_for(self, request):
        if request.denial_or_cancel_reason == "REQUEST_TIMEOUT":
            return DENIED
        if request.denial_or_cancel_reason:
            return CANCELLED
        return COMPLETED

    @staticmethod
    def _finished_state_for(request):
        """A bus that crossed untreated after its early green was gated out
        finishes DENIED, carrying the gate's reason; anything else COMPLETED."""
        if (
            request.tsp_requested
            and request.tsp_action == TSP_ACTION_NONE
            and request.tsp_gate_reason
        ):
            request.denial_or_cancel_reason = request.tsp_gate_reason
            return DENIED
        return COMPLETED

    def _prune_queue(self, node, node_x, vehicles):
        live_queue = []
        for queued_request in node.request_queue:
            if node_x in queued_request.bus.passed_nodes:
                self._finalize_request(
                    node, queued_request, self._finished_state_for(queued_request)
                )
                continue
            if self._request_is_live(queued_request, vehicles):
                live_queue.append(queued_request)
                continue
            self._finalize_request(
                node, queued_request, self._terminal_state_for(queued_request)
            )
        node.request_queue = live_queue

    def _refresh_active_request(self, node, node_x, vehicles):
        """Retire a finished or dead head request, then arm the next one.

        Arming attaches the request immediately -- there is no signal
        transition to wait for, since TSP only nudges the running cycle. An
        armed DBL request reserves its lane from this moment.
        """
        request = node.active_request
        if request is not None:
            finished = node_x in request.bus.passed_nodes
            dead = not finished and not self._request_is_live(request, vehicles)
            if finished or dead:
                self._finalize_request(
                    node,
                    request,
                    self._finished_state_for(request)
                    if finished
                    else self._terminal_state_for(request),
                )
                node.active_request = None
                if node.priority_state == TSP_EXTENDING:
                    # The bus is gone or done; the held green ends now,
                    # through the normal yellow.
                    self._end_extension(node)
        if node.active_request is None and node.request_queue:
            request = node.request_queue.pop(0)
            request.state = ARMED
            if request.dbl_requested:
                request.dbl_granted = True
            node.active_request = request

    def _begin_extension(self, node, request, cap):
        node.priority_state = TSP_EXTENDING
        request.state = TSP_EXTENDING
        request.tsp_action = TSP_ACTION_EXTENDING
        node.tsp_cap_frames = cap
        # This frame the green would have ended; holding it is the first
        # extended frame.
        node.priority_timer = 1
        request.tsp_adjust_frames = 1
        node.timer += 1

    def _end_extension(self, node):
        node.last_tsp_action = TSP_ACTION_EXTENDING
        node.last_tsp_adjust_frames = node.priority_timer
        node.priority_state = NORMAL
        node.priority_timer = 0
        node.tsp_cap_frames = 0
        request = node.active_request
        if request is not None and request.state == TSP_EXTENDING:
            request.state = ARMED
        self._advance_phase(node)

    def _extension_update(self, node, node_x, vehicles):
        request = node.active_request
        if request is None or not request.bus.is_front_bumper_upstream(
            node_x, H_Y, ROAD_W, STOP
        ):
            # Bus crossed the stop bar: the extension did its job.
            self._end_extension(node)
            return
        if node.priority_timer >= node.tsp_cap_frames:
            # Cap reached without the bus clearing: the green ends anyway and
            # the bus waits for its normal green. No second action follows.
            self._end_extension(node)
            return
        node.priority_timer += 1
        node.timer += 1
        request.tsp_adjust_frames = node.priority_timer

    def _extension_is_feasible(self, request, node_x, cap):
        """Arrival-time gate on an extension, the twin of the early-green
        gate: the bus must be predicted to cross the stop bar within the cap
        (unimpeded ETA, the shared estimator vehicle.bus_eta_frames). Otherwise the green
        is held for a bus that still meets the red at the end of it -- the
        cross street pays the whole cap for nothing -- so the green ends on
        time and the request stays armed for an early green instead. A bus
        that then crosses untreated finishes DENIED with this reason."""
        bus = request.bus
        eta = bus_eta_frames(bus, self.distance_to_node_stop_bar(bus, node_x))
        if eta > cap:
            request.tsp_gate_reason = TSP_DENY_ETA_WINDOW
            return False
        request.tsp_gate_reason = ""
        return True

    def _early_green_is_feasible(self, node, request, node_x, target_end, cut):
        """Arrival-time gate on a truncation, evaluated afresh every frame.

        The cut must outweigh the yellow + all-red it runs into, and the bus
        must be predicted to cross the stop bar inside the green the cut
        brings forward -- a bus still an ETA away when that green ends would
        meet the next red and pay for the very cycle it requested. A bus
        already queued at the bar crosses when the green opens, so its
        predicted crossing is max(eta, green start). Records the reason on
        the request when withheld; the request stays armed and is re-checked
        as the bus closes in.
        """
        clearance = self.yellow_time + self.red_clearance_time
        if cut <= clearance:
            request.tsp_gate_reason = TSP_DENY_NET_BENEFIT
            return False
        bus = request.bus
        eta = bus_eta_frames(bus, self.distance_to_node_stop_bar(bus, node_x))
        earliest_green = (target_end - node.timer) + clearance
        latest_useful = earliest_green + self.get_green_time(
            node_x, self._phase_for_approach(request.originating_approach)
        )
        crossing = max(eta, earliest_green)
        if not earliest_green <= crossing <= latest_useful:
            request.tsp_gate_reason = TSP_DENY_ETA_WINDOW
            return False
        request.tsp_gate_reason = ""
        return True

    def _begin_early_green(self, node, request, node_x):
        """Shorten the running conflicting green, bounded by the cap and the
        MIN_GREEN floor. Returns False when there is nothing left to cut or
        the cut fails the arrival-time gate."""
        conflicting = node.phase
        green = self.get_green_time(node_x, conflicting)
        cap = self._tsp_cap_frames(node_x, conflicting)
        # The cut is cap-sized and the ARRIVAL WINDOW gates it
        # (_early_green_is_feasible), deliberately: sizing the cut to the ETA
        # point estimate was tried on 2026-09-22 and regressed T1/T2, because
        # the estimator then read the bus's INSTANTANEOUS speed and so ran
        # long for a bus that was still accelerating -- a near bus yielded
        # cut == 0 and never got its early green. Under IDM the shared
        # estimator (vehicle.bus_eta_frames) is now the unimpeded kinematic
        # arrival time; re-measure before sizing the cut from it.
        shortened = max(self.min_green_frames, green - cap)
        target_end = max(node.timer + 1, shortened)
        cut = green - target_end
        if cut <= 0 or not self._early_green_is_feasible(
            node, request, node_x, target_end, cut
        ):
            return False
        node.priority_state = TSP_EARLY_TRUNCATE
        node.tsp_target_end = target_end
        node.priority_timer = cut
        request.state = TSP_EARLY_TRUNCATE
        request.tsp_action = TSP_ACTION_EARLY_GREEN
        request.tsp_adjust_frames = cut
        self._truncation_update(node, node_x)
        return True

    def _end_truncation(self, node):
        node.last_tsp_action = TSP_ACTION_EARLY_GREEN
        node.last_tsp_adjust_frames = node.priority_timer
        node.priority_state = NORMAL
        node.priority_timer = 0
        node.tsp_target_end = 0
        request = node.active_request
        if request is not None and request.state == TSP_EARLY_TRUNCATE:
            request.state = ARMED
        self._advance_phase(node)

    def _truncation_update(self, node, node_x):
        # Runs on node fields alone, so a request that dies mid-truncation
        # does not stretch the conflicting green back out again.
        node.timer += 1
        if node.timer >= node.tsp_target_end:
            self._end_truncation(node)

    def _priority_update(self, node_x, node, vehicles):
        self._prune_queue(node, node_x, vehicles)
        self._refresh_active_request(node, node_x, vehicles)

        if node.priority_state == TSP_EXTENDING:
            self._extension_update(node, node_x, vehicles)
            return
        if node.priority_state == TSP_EARLY_TRUNCATE:
            self._truncation_update(node, node_x)
            return

        request = node.active_request
        if request is not None and self._request_can_start_tsp(
            request, node_x, vehicles
        ):
            bus_phase = self._phase_for_approach(request.originating_approach)
            if node.phase == bus_phase:
                # Green extension: only when this green would otherwise end
                # now with the bus still upstream.
                if node.timer + 1 >= self.get_green_time(node_x, bus_phase):
                    cap = self._tsp_cap_frames(node_x, bus_phase)
                    if cap > 0 and self._extension_is_feasible(request, node_x, cap):
                        self._begin_extension(node, request, cap)
                        return
            elif node.phase == self._conflicting_phase(bus_phase):
                if self._begin_early_green(node, request, node_x):
                    return
        self._normal_phase_update(node, vehicles, node_x)

    def update(self, vehicles=None):
        vehicles = vehicles or []
        self.frame_number += 1
        if self.discharge_active:
            self.experiment_metrics["discharge_total_frames"] += 1
        self._cleanup_reservations(vehicles)
        self._consume_discharge_commands(vehicles)
        if self.discharge_active:
            self._update_discharge(vehicles)
            self._publish_discharge_status()
            return
        if self.plan_active:
            for node_x, node in self.nodes.items():
                self.experiment_metrics["dbl_total_active_frames"] += len(node.dbl_commanded)
                self._normal_phase_update(node, vehicles, node_x)
        else:
            self._collect_priority_requests(vehicles)
            for node_x, node in self.nodes.items():
                self._priority_update(node_x, node, vehicles)
        if self.coordination:
            for node_x, node in self.nodes.items():
                if node.phase == 0 and node.timer == 0:
                    self._coordinate(node_x, node)
        self._publish_discharge_status()

    def get_experiment_metrics(self):
        """Detached JSON-safe counters for the current benchmark run.

        Requests still live at the final frame are classified as granted when
        the mechanism actually activated, otherwise denied with RUN_ENDED.
        The controller itself is not mutated, so checkpoint exports remain
        observational and cannot change simulation behavior.
        """
        result = {
            **self.experiment_metrics,
            "tsp_denial_reasons": dict(
                self.experiment_metrics["tsp_denial_reasons"]
            ),
            "dbl_denial_reasons": dict(
                self.experiment_metrics["dbl_denial_reasons"]
            ),
            "node_tsp_grants": dict(
                self.experiment_metrics["node_tsp_grants"]
            ),
        }
        pending = []
        for node in self.nodes.values():
            if node.active_request is not None:
                pending.append(node.active_request)
            pending.extend(node.request_queue)
        for request in pending:
            if request.metrics_finalized:
                continue
            if request.tsp_requested:
                if request.tsp_action != TSP_ACTION_NONE:
                    result["tsp_requests_granted"] += 1
                    result["tsp_total_adjust_frames"] += max(
                        0, int(request.tsp_adjust_frames or 0)
                    )
                    action_key = (
                        "tsp_actions_early_green"
                        if request.tsp_action == TSP_ACTION_EARLY_GREEN
                        else "tsp_actions_extending"
                    )
                    result[action_key] += 1
                    node_key = str(request.node_x)
                    result["node_tsp_grants"][node_key] = (
                        result["node_tsp_grants"].get(node_key, 0) + 1
                    )
                else:
                    result["tsp_requests_denied"] += 1
                    reasons = result["tsp_denial_reasons"]
                    reasons["RUN_ENDED"] = reasons.get("RUN_ENDED", 0) + 1
            if request.dbl_requested:
                if request.dbl_granted:
                    result["dbl_requests_granted"] += 1
                    result["dbl_actions_activated"] += 1
                    result["dbl_total_active_frames"] += max(
                        0, self.frame_number - request.requested_at_frame
                    )
                else:
                    result["dbl_requests_denied"] += 1
                    reasons = result["dbl_denial_reasons"]
                    reasons["RUN_ENDED"] = reasons.get("RUN_ENDED", 0) + 1
        return result

    def _base_signals_for_node(self, node):
        # TSP never isolates one approach: an extension is simply the running
        # phase held longer and a truncation is the running phase ending
        # sooner, so every state shows the ordinary signals for node.phase.
        return self._normal_signals_for_phase(node.phase)

    def _signals_for_node(self, node_x, node):
        if self.discharge_active:
            return self._discharge_signals_for_node(node_x)
        return self._base_signals_for_node(node)

    def get_all_signals(self, int_x_list=INT_X):
        return {
            node_x: self._signals_for_node(node_x, self.nodes[node_x])
            for node_x in int_x_list
            if node_x in self.nodes
        }

    def _commanded_dbl_dict(self, node_x, direction):
        return {
            "request_id": None, "bus_id": None, "route_id": None,
            "route_leg_index": None, "node_x": node_x,
            "originating_approach": direction, "movement": None,
            "entry_lane": DBL_LANE_INDEX, "exit_direction": None,
            "conflicting_approaches": [], "requested_at_frame": None,
            "state": "ACTIVE", "expires_at_frame": None,
            "denial_or_cancel_reason": "", "tsp_requested": False,
            "dbl_requested": True, "attempt_number": 0, "tsp_action": TSP_ACTION_NONE,
            "tsp_adjust_frames": 0, "tsp_gate_reason": "", "dbl_revoke_reason": "",
            "commanded": True,
        }

    def is_dbl_active_for_approach(self, target_node_x, direction, all_vehicles=None):
        node = self.nodes.get(target_node_x)
        if node and direction in node.dbl_commanded:
            return True
        if not node or not node.active_request:
            return False
        request = node.active_request
        return request.dbl_requested and request.originating_approach == direction

    def get_active_dbl_request(self, target_node_x, direction=None):
        node = self.nodes.get(target_node_x)
        if node and node.dbl_commanded:
            commanded = direction if direction in node.dbl_commanded else (
                sorted(node.dbl_commanded)[0] if direction is None else None
            )
            if commanded is not None:
                return self._commanded_dbl_dict(target_node_x, commanded)
        if not node or not node.active_request or not node.active_request.dbl_requested:
            return None
        request = node.active_request
        if direction is not None and request.originating_approach != direction:
            return None
        return request.as_dict()

    def get_all_dbl_states(self, int_x_list=INT_X, vehicles=None):
        states = {}
        for node_x in int_x_list:
            result = {
                "EB": "INACTIVE",
                "WB": "INACTIVE",
                "NB": "INACTIVE",
                "SB": "INACTIVE",
            }
            node = self.nodes.get(node_x)
            if node:
                for approach in node.dbl_commanded:
                    result[approach] = "ACTIVE"
                for queued in node.request_queue:
                    if queued.dbl_requested:
                        result[queued.originating_approach] = "TRANSITIONING"
                request = node.active_request
                if request and request.dbl_requested:
                    upstream = request.bus.is_front_bumper_upstream(
                        node_x, H_Y, ROAD_W, STOP
                    )
                    result[request.originating_approach] = (
                        "ACTIVE" if upstream else "CLEARING"
                    )
            states[node_x] = result
        return states

    def _tsp_action_for_node(self, node):
        if node.priority_state == TSP_EXTENDING:
            return TSP_ACTION_EXTENDING
        if node.priority_state == TSP_EARLY_TRUNCATE:
            return TSP_ACTION_EARLY_GREEN
        return TSP_ACTION_NONE

    def get_node_status(self, node_x):
        node = self.nodes[node_x]
        return {
            "node_x": node_x,
            "phase_index": node.phase,
            "phase_timer_frames": node.timer,
            "priority_state": node.priority_state,
            "priority_timer_frames": node.priority_timer,
            "tsp_action": self._tsp_action_for_node(node),
            "tsp_adjust_frames": (
                node.priority_timer if node.priority_state != NORMAL else 0
            ),
            "tsp_last_action": node.last_tsp_action,
            "tsp_last_adjust_frames": node.last_tsp_adjust_frames,
            "signals": self._signals_for_node(node_x, node),
            "discharge_active": bool(self.discharge_active),
            "discharge_state": self.discharge_state,
            "discharge_plan": self.discharge_plan_name,
            "active_request": (
                self._request_snapshot(node.active_request)
                if node.active_request
                else None
            ),
            "queued_requests": [
                self._request_snapshot(item) for item in node.request_queue
            ],
            "terminal_history": list(node.terminal_history),
            "reservation_count": len(node.reservations),
        }

    def routes_with_live_requests(self):
        """Route IDs with a request the controller currently holds (armed,
        queued or adjusting) at any node. The decision merge keeps these
        routes' flags as they are: a request is only ever created under an
        enabled flag, and the flag dropping cancels it (FEATURE_DISABLED) --
        so a stale or held decision, or a decider working from a snapshot
        taken before the request existed, must not withdraw a treatment the
        safety authority already committed to. Flags gate *new* requests."""
        routes = set()
        for node in self.nodes.values():
            for request in (node.active_request, *node.request_queue):
                if request is not None:
                    routes.add(request.route_id)
        return routes

    def get_priority_status_for_bus(self, bus, node_x):
        node = self.nodes.get(node_x)
        if not node:
            return None
        candidates = []
        if node.active_request:
            candidates.append(node.active_request)
        candidates.extend(node.request_queue)
        for request in candidates:
            if request.bus is bus:
                return self._request_snapshot(request)
        return None

    def get_latest_terminal_status_for_bus(self, bus, node_x=None):
        """Return the latest retained terminal event for a bus, if any."""
        latest = None
        for candidate_node_x, node in self.nodes.items():
            if node_x is not None and candidate_node_x != node_x:
                continue
            for event in reversed(node.terminal_history):
                if event["bus_id"] == bus.bus_id:
                    if latest is None or event["terminal_frame"] > latest["terminal_frame"]:
                        latest = event
                    break
        return dict(latest) if latest else None
