"""Signal timing, intersection reservations, and bus-priority arbitration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import control_panel
from canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from vehicle import Bus, DBL_LANE_INDEX

RED = "RED"
YELLOW = "YELLOW"
GREEN = "GREEN"
VALID_SIGNAL_STATES = {RED, YELLOW, GREEN}

NORMAL = "NORMAL"
REQUESTED = "REQUESTED"
CONFLICT_YELLOW = "CONFLICT_YELLOW"
ALL_RED_CLEARANCE = "ALL_RED_CLEARANCE"
PRIORITY_ACTIVE = "PRIORITY_ACTIVE"
PRIORITY_CLEARING = "PRIORITY_CLEARING"
RECOVERY_ALL_RED = "RECOVERY_ALL_RED"
COMPLETED = "COMPLETED"
DENIED = "DENIED"
CANCELLED = "CANCELLED"
INFEASIBLE_GRANT = "INFEASIBLE_GRANT"
ACTIVE_STALL_TIMEOUT = "ACTIVE_STALL_TIMEOUT"

PRIORITY_PROGRESS_EPSILON = 0.5

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


DISCHARGE_PLAN_STAGES = {
    "Eastbound Corridor": (
        DischargeStage("Node B downstream", ((700, "EB"),)),
        DischargeStage("Node B → Node A coordinated", ((300, "EB"), (700, "EB"))),
    ),
    "Westbound Corridor": (
        DischargeStage("Node A downstream", ((300, "WB"),)),
        DischargeStage("Node A → Node B coordinated", ((300, "WB"), (700, "WB"))),
    ),
    "Node A Northbound": (DischargeStage("Node A northbound", ((300, "NB"),)),),
    "Node A Southbound": (DischargeStage("Node A southbound", ((300, "SB"),)),),
    "Node B Northbound": (DischargeStage("Node B northbound", ((700, "NB"),)),),
    "Node B Southbound": (DischargeStage("Node B southbound", ((700, "SB"),)),),
}


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
    active_start_frame: int | None = None
    last_progress_frame: int | None = None
    last_stop_bar_distance: float | None = None


class SignalController:
    """Own independent signal and priority state for every intersection node."""

    def __init__(
        self,
        global_config,
        yellow_time=60,
        red_clearance_time=60,
        priority_request_timeout=900,
        discharge_min_green=180,
        discharge_max_green=600,
        discharge_stall_time=180,
        discharge_queue_target=0,
        priority_active_stall_frames=300,
        priority_active_max_frames=900,
    ):
        self.global_config = global_config
        self.yellow_time = max(1, int(yellow_time))
        self.red_clearance_time = max(1, int(red_clearance_time))
        self.priority_request_timeout = max(1, int(priority_request_timeout))
        self.priority_active_stall_frames = max(
            1, int(priority_active_stall_frames)
        )
        self.priority_active_max_frames = max(1, int(priority_active_max_frames))
        self.discharge_min_green = max(1, int(discharge_min_green))
        self.discharge_max_green = max(
            self.discharge_min_green, int(discharge_max_green)
        )
        self.discharge_stall_time = max(1, int(discharge_stall_time))
        self.discharge_queue_target = max(0, int(discharge_queue_target))
        self.reset_all_state()

    def reset_all_state(self):
        """Restore construction-time runtime state while preserving configuration."""
        self.frame_number = 0
        self.nodes = {node_x: NodeState() for node_x in INT_X}
        self._request_sequence = 0
        self._attempt_counts: dict[tuple[str, int, int], int] = {}
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
        # TSP now uses explicit priority requests instead of direction-blind
        # extension of whichever phase happens to be green.
        return 0

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

    @staticmethod
    def _candidate_drains_blocker(candidate, occupants):
        stage = DISCHARGE_PLAN_STAGES[candidate["plan"]][
            candidate["stage_index"]
        ]
        return any(
            approach in occupants.get(node_x, set())
            for node_x, approach in stage.greens
        )

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
                ranked = self._rank_discharge_candidates(vehicles)
                occupants = self._box_occupant_directions(vehicles)
                candidate = (
                    next(
                        (
                            item
                            for item in ranked
                            if item["ready"]
                            and self._candidate_drains_blocker(item, occupants)
                        ),
                        None,
                    )
                    or next(
                        (item for item in ranked if item["ready"]), None
                    )
                    or (ranked[0] if ranked else None)
                )
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

    def get_green_time(self):
        return max(1, int(self.global_config.get("green_time", 240)))

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
    ):
        if not self.movements_conflict(
            entry_approach,
            entry_movement,
            other_approach,
            other_movement,
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
        if self.discharge_active:
            if (
                self.discharge_state != DISCHARGE_ACTIVE
                or self._discharge_green_map.get(node_x) != approach
            ):
                return False
        active = node.active_request
        if node.priority_state in (PRIORITY_ACTIVE, PRIORITY_CLEARING):
            if active is None or approach != active.originating_approach:
                return False

        for reservation in node.reservations.values():
            if self._vehicle_blocks_entry(
                approach,
                movement,
                reservation["vehicle"],
                reservation["approach"],
                reservation["movement"],
                node_x,
            ):
                return False

        # Protect against an unregistered vehicle placed inside the box by a
        # test, reset, or legacy caller.
        for other in vehicles or []:
            if other is vehicle or not self.vehicle_occupies_intersection(other, node_x):
                continue
            other_approach = self._approach_for_vehicle(other, node_x)
            other_movement = self._movement_for_vehicle(other, node_x)
            if self._vehicle_blocks_entry(
                approach,
                movement,
                other,
                other_approach,
                other_movement,
                node_x,
            ):
                return False

        node.reservations[vehicle_key] = {
            "vehicle": vehicle,
            "approach": approach,
            "movement": movement,
            "reserved_at_frame": self.frame_number,
        }
        vehicle.intersection_entry_approaches[node_x] = approach
        vehicle.intersection_entry_movements[node_x] = movement
        return True

    def cancel_intersection_entry(self, vehicle, node_x):
        """Release a reservation while the vehicle is still upstream."""
        node = self.nodes.get(node_x)
        if node is not None:
            node.reservations.pop(id(vehicle), None)

    def distance_to_node_stop_bar(self, vehicle, node_x):
        return vehicle.distance_to_node_stop_bar(
            node_x, h_y=H_Y, road_w=ROAD_W, stop_offset=STOP
        )

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
        dist = self.distance_to_node_stop_bar(bus, target_node)
        return 0 <= dist <= 250

    def is_dbl_enabled_for_bus_leg(self, bus, target_node):
        """Return the same live DBL intent used by priority eligibility."""
        if not isinstance(bus, Bus):
            return False
        live_cfg = self._live_route_config(bus)
        if not live_cfg.get("dbl_enabled", False):
            return False
        leg = bus.get_active_route_leg(INT_X)
        return bool(leg and leg["node_x"] == target_node)

    def is_bus_dbl_eligible(self, bus, target_node, all_vehicles=None):
        if not self.is_dbl_enabled_for_bus_leg(bus, target_node):
            return False
        leg = bus.get_active_route_leg(INT_X)
        if bus.lane_index != DBL_LANE_INDEX:
            return False
        dist = self.distance_to_node_stop_bar(bus, target_node)
        return 0 <= dist <= 250

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
        return True

    def _normal_phase_update(self, node, vehicles, node_x):
        node.timer += 1
        if node.phase in (0, 3):
            maximum = self.get_green_time()
        elif node.phase in (1, 4):
            maximum = self.yellow_time
        else:
            maximum = self.red_clearance_time
        if node.timer < maximum:
            return
        if node.phase in (2, 5) and not self.is_intersection_clear(node_x, vehicles):
            return
        node.timer = 0
        node.phase = (node.phase + 1) % 6

    def _begin_next_request(self, node):
        if not node.request_queue:
            return False
        request = node.request_queue.pop(0)
        node.active_request = request
        node.priority_timer = 0
        normal_signals = self._normal_signals_for_phase(node.phase)
        if any(value in (GREEN, YELLOW) for value in normal_signals.values()):
            node.priority_state = CONFLICT_YELLOW
            request.state = CONFLICT_YELLOW
        else:
            node.priority_state = ALL_RED_CLEARANCE
            request.state = ALL_RED_CLEARANCE
        return True

    def _cancel_active_request(self, node):
        if node.active_request:
            node.active_request.state = RECOVERY_ALL_RED
        node.priority_state = RECOVERY_ALL_RED
        node.priority_timer = 0

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

    def _start_priority_watchdog(self, node, request, node_x):
        node.active_start_frame = self.frame_number
        node.last_progress_frame = self.frame_number
        node.last_stop_bar_distance = self.distance_to_node_stop_bar(
            request.bus, node_x
        )

    @staticmethod
    def _stop_priority_watchdog(node):
        node.active_start_frame = None
        node.last_progress_frame = None
        node.last_stop_bar_distance = None

    def _deny_active_grant(self, node, request, reason):
        request.denial_or_cancel_reason = reason
        request.state = RECOVERY_ALL_RED
        node.priority_state = RECOVERY_ALL_RED
        node.priority_timer = 0
        self._stop_priority_watchdog(node)

    def _priority_update(self, node_x, node, vehicles):
        live_queue = []
        for queued_request in node.request_queue:
            if self._request_is_live(queued_request, vehicles):
                live_queue.append(queued_request)
                continue
            terminal_state = (
                DENIED
                if queued_request.denial_or_cancel_reason == "REQUEST_TIMEOUT"
                else CANCELLED
            )
            self._finalize_request(node, queued_request, terminal_state)
        node.request_queue = live_queue
        if node.priority_state == NORMAL:
            if self._begin_next_request(node):
                return
            self._normal_phase_update(node, vehicles, node_x)
            return

        request = node.active_request
        if request is None:
            node.priority_state = RECOVERY_ALL_RED
            node.priority_timer = 0
            return
        if node.priority_state in (PRIORITY_ACTIVE, PRIORITY_CLEARING):
            if request.bus not in vehicles:
                request.denial_or_cancel_reason = "BUS_REMOVED"
                self._cancel_active_request(node)
                return
        elif (
            node.priority_state != RECOVERY_ALL_RED
            and not self._request_is_live(request, vehicles)
        ):
            self._cancel_active_request(node)
            return

        node.priority_timer += 1
        if node.priority_state == CONFLICT_YELLOW:
            if node.priority_timer >= self.yellow_time:
                node.priority_state = ALL_RED_CLEARANCE
                request.state = ALL_RED_CLEARANCE
                node.priority_timer = 0
            return
        if node.priority_state == ALL_RED_CLEARANCE:
            if (
                node.priority_timer >= self.red_clearance_time
                and self.is_intersection_clear(node_x, vehicles)
            ):
                if self._bus_can_use_grant(request, node_x, vehicles):
                    node.priority_state = PRIORITY_ACTIVE
                    request.state = PRIORITY_ACTIVE
                    node.priority_timer = 0
                    self._start_priority_watchdog(node, request, node_x)
                else:
                    # Keep the denied request attached until RECOVERY_ALL_RED
                    # completes, so the normal conflict-safe recovery path can
                    # record it and resume ordinary signal service.
                    self._deny_active_grant(
                        node, request, INFEASIBLE_GRANT
                    )
            return
        if node.priority_state == PRIORITY_ACTIVE:
            if not request.bus.is_front_bumper_upstream(
                node_x, H_Y, ROAD_W, STOP
            ):
                node.priority_state = PRIORITY_CLEARING
                request.state = PRIORITY_CLEARING
                self._stop_priority_watchdog(node)
                return
            distance = self.distance_to_node_stop_bar(request.bus, node_x)
            if (
                node.last_stop_bar_distance is None
                or distance
                < node.last_stop_bar_distance - PRIORITY_PROGRESS_EPSILON
            ):
                node.last_progress_frame = self.frame_number
                node.last_stop_bar_distance = distance
            if node.active_start_frame is None:
                self._start_priority_watchdog(node, request, node_x)
                return
            stalled = (
                self.frame_number - node.last_progress_frame
                > self.priority_active_stall_frames
            )
            over_max = (
                self.frame_number - node.active_start_frame
                > self.priority_active_max_frames
            )
            if stalled or over_max:
                self._deny_active_grant(
                    node, request, ACTIVE_STALL_TIMEOUT
                )
            return
        if node.priority_state == PRIORITY_CLEARING:
            if node_x in request.bus.passed_nodes:
                node.priority_state = RECOVERY_ALL_RED
                request.state = RECOVERY_ALL_RED
                node.priority_timer = 0
            return
        if node.priority_state == RECOVERY_ALL_RED:
            if (
                node.priority_timer >= self.red_clearance_time
                and self.is_intersection_clear(node_x, vehicles)
            ):
                approach = request.originating_approach
                node.phase = 0 if approach in ("EB", "WB") else 3
                node.timer = 0
                node.priority_state = NORMAL
                node.priority_timer = 0
                if request.denial_or_cancel_reason in (
                    "REQUEST_TIMEOUT",
                    INFEASIBLE_GRANT,
                    ACTIVE_STALL_TIMEOUT,
                ):
                    terminal_state = DENIED
                elif request.denial_or_cancel_reason:
                    terminal_state = CANCELLED
                else:
                    terminal_state = COMPLETED
                self._finalize_request(node, request, terminal_state)
                node.active_request = None
                self._stop_priority_watchdog(node)

    def update(self, vehicles=None):
        vehicles = vehicles or []
        self.frame_number += 1
        self._cleanup_reservations(vehicles)
        self._consume_discharge_commands(vehicles)
        if self.discharge_active:
            self._update_discharge(vehicles)
            self._publish_discharge_status()
            return
        self._collect_priority_requests(vehicles)
        for node_x, node in self.nodes.items():
            self._priority_update(node_x, node, vehicles)
        self._publish_discharge_status()

    def _base_signals_for_node(self, node):
        request = node.active_request
        if node.priority_state == NORMAL:
            return self._normal_signals_for_phase(node.phase)
        if node.priority_state == CONFLICT_YELLOW:
            normal = self._normal_signals_for_phase(node.phase)
            return {
                approach: YELLOW if state in (GREEN, YELLOW) else RED
                for approach, state in normal.items()
            }
        if node.priority_state in (ALL_RED_CLEARANCE, RECOVERY_ALL_RED):
            return {"EB": RED, "WB": RED, "NB": RED, "SB": RED}
        if node.priority_state in (PRIORITY_ACTIVE, PRIORITY_CLEARING) and request:
            result = {"EB": RED, "WB": RED, "NB": RED, "SB": RED}
            result[request.originating_approach] = GREEN
            return result
        return {"EB": RED, "WB": RED, "NB": RED, "SB": RED}

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

    def is_dbl_active_for_approach(self, target_node_x, direction, all_vehicles=None):
        node = self.nodes.get(target_node_x)
        if not node or not node.active_request:
            return False
        request = node.active_request
        return (
            request.dbl_requested
            and request.originating_approach == direction
            and node.priority_state in (PRIORITY_ACTIVE, PRIORITY_CLEARING)
        )

    def get_active_dbl_request(self, target_node_x, direction=None):
        node = self.nodes.get(target_node_x)
        if not node or not node.active_request or not node.active_request.dbl_requested:
            return None
        if node.priority_state in (NORMAL, RECOVERY_ALL_RED):
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
            if node and node.active_request and node.active_request.dbl_requested:
                if node.priority_state in (CONFLICT_YELLOW, ALL_RED_CLEARANCE):
                    display_state = "TRANSITIONING"
                elif node.priority_state == PRIORITY_ACTIVE:
                    display_state = "ACTIVE"
                elif node.priority_state == PRIORITY_CLEARING:
                    display_state = "CLEARING"
                else:
                    display_state = "INACTIVE"
                result[node.active_request.originating_approach] = display_state
            states[node_x] = result
        return states

    def get_node_status(self, node_x):
        node = self.nodes[node_x]
        return {
            "node_x": node_x,
            "phase_index": node.phase,
            "phase_timer_frames": node.timer,
            "priority_state": node.priority_state,
            "priority_timer_frames": node.priority_timer,
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
