"""Per-bus lifecycle instrumentation for treated-vs-untreated TSP analysis.

Every bus gets one record from spawn to network exit. The record is built up
frame by frame from state the simulator already computes (speed, route leg,
stop-bar geometry, passed_nodes, the controller's priority request) and is
appended to ``bus_events.jsonl`` when the bus leaves the network. Nothing in
here feeds back into the simulation: every public entry point swallows its
own errors so instrumentation can never crash or alter a run.
"""

import json
from pathlib import Path

from src.ui import canvas_gemini as canvas
from src.core.vehicle import Bus, eta_frames_to_stop_bar


BASE_DIR = Path(__file__).resolve().parents[2]
assert (BASE_DIR / "requirements.txt").exists(), (
    f"BASE_DIR does not resolve to the repo root: {BASE_DIR}"
)
DEFAULT_BUS_EVENTS_PATH = BASE_DIR / "logs" / "bus_events.jsonl"
# Same threshold the telemetry queue counter uses to call a vehicle "stopped".
STOP_SPEED_THRESHOLD = 0.25
FRAMES_PER_SECOND = 60.0
TSP_ACTION_NONE = "none"

NODE_EVENT_FIELDS = (
    "node_x",
    "arrival_frame",
    "signal_phase_at_arrival",
    "signal_colour_at_arrival",
    "residual_green_frames",
    "eta_frames_at_decision",
    "would_have_stopped",
    "tsp_requested",
    "tsp_granted",
    "tsp_treated",
    "tsp_action",
    "tsp_adjust_frames",
    "denial_reason",
    "stop_bar_cross_frame",
    "node_wait_frames",
    "node_clear_frame",
    "queue_ahead_veh",
)

BUS_NODE_EVENT_HEADERS = (
    "run_uuid", "campaign_id", "model", "seed", "bus_id", "route_id",
    "node_x", "node_seq", "arrival_frame", "signal_phase_at_arrival",
    "signal_colour_at_arrival", "residual_green_frames",
    "eta_frames_at_decision", "would_have_stopped", "tsp_requested",
    "tsp_granted", "tsp_action", "tsp_adjust_frames", "denial_reason",
    "stop_bar_cross_frame", "node_wait_frames", "node_clear_frame",
    "queue_ahead_veh", "passengers",
)

# Flat column order for the "Bus Events" sheet. A bus traverses at most two
# nodes, so per-node fields are unrolled as node1_* / node2_*.
BUS_EVENT_HEADERS = [
    "bus_id",
    "route_id",
    "spawn_frame",
    "spawn_sim_time",
    "first_stop_frame",
    "completion_frame",
    "completion_sim_time",
    "total_wait_frames",
    "passengers",
    "nodes_traversed",
    "tsp_treated_any",
] + [f"node{slot}_{field}" for slot in (1, 2) for field in NODE_EVENT_FIELDS]


def _new_node_event(node_x):
    return {
        "node_x": int(node_x),
        "arrival_frame": None,
        "signal_phase_at_arrival": None,
        "signal_colour_at_arrival": None,
        "residual_green_frames": None,
        "eta_frames_at_decision": None,
        "would_have_stopped": None,
        "tsp_requested": False,
        "tsp_granted": False,
        "tsp_treated": False,
        "tsp_action": TSP_ACTION_NONE,
        "tsp_adjust_frames": 0,
        "denial_reason": "",
        "stop_bar_cross_frame": None,
        "node_wait_frames": 0,
        "node_clear_frame": None,
        "queue_ahead_veh": 0,
    }


def iter_bus_node_event_rows(record, identity=None):
    """Yield one tidy row per traversed/planned node without a two-node cap."""
    identity = identity or {}
    for node_seq, node in enumerate(record.get("nodes") or [], start=1):
        if not isinstance(node, dict):
            continue
        row = {
            "run_uuid": identity.get("run_uuid"),
            "campaign_id": identity.get("campaign_id"),
            "model": identity.get("model"),
            "seed": identity.get("seed"),
            "bus_id": record.get("bus_id"),
            "route_id": record.get("route_id"),
            "node_seq": node_seq,
            "passengers": record.get("passengers"),
        }
        row.update({field: node.get(field) for field in NODE_EVENT_FIELDS})
        yield {header: row.get(header) for header in BUS_NODE_EVENT_HEADERS}


def flatten_bus_event(record):
    """One spreadsheet row, in BUS_EVENT_HEADERS order, from a stored record."""
    nodes = record.get("nodes") or []
    row = {
        "bus_id": record.get("bus_id"),
        "route_id": record.get("route_id"),
        "spawn_frame": record.get("spawn_frame"),
        "spawn_sim_time": record.get("spawn_sim_time"),
        "first_stop_frame": record.get("first_stop_frame"),
        "completion_frame": record.get("completion_frame"),
        "completion_sim_time": record.get("completion_sim_time"),
        "total_wait_frames": record.get("total_wait_frames"),
        "passengers": record.get("passengers"),
        "nodes_traversed": len(nodes),
        "tsp_treated_any": any(
            bool(node.get("tsp_treated")) for node in nodes if isinstance(node, dict)
        ),
    }
    for slot in (1, 2):
        node = nodes[slot - 1] if len(nodes) >= slot and isinstance(nodes[slot - 1], dict) else {}
        for field in NODE_EVENT_FIELDS:
            row[f"node{slot}_{field}"] = node.get(field)
    return [row[header] for header in BUS_EVENT_HEADERS]


def write_bus_events_sheet(sheet, records):
    """Append the header plus one flattened row per completed bus."""
    sheet.append(list(BUS_EVENT_HEADERS))
    for record in records:
        if isinstance(record, dict):
            sheet.append(flatten_bus_event(record))


class BusEventTracker:
    """Accumulate per-bus lifecycle events and append them on completion.

    ``path`` may be a Path or a zero-argument callable returning one, so the
    owning module can re-point the log (tests do this via monkeypatch).
    """

    def __init__(self, path=DEFAULT_BUS_EVENTS_PATH):
        self._path = path
        self._records = {}

    @property
    def path(self):
        target = self._path() if callable(self._path) else self._path
        return Path(target)

    def reset(self):
        """Drop in-flight records; the on-disk log is cleared by the caller."""
        self._records.clear()

    def active_record(self, bus_id):
        return self._records.get(bus_id)

    # -- per-frame accumulation ---------------------------------------------

    def observe(self, vehicles, frame_number, signal_controller=None):
        """Advance every tracked bus by one simulation frame."""
        try:
            frame_number = int(frame_number)
            for vehicle in vehicles:
                if isinstance(vehicle, Bus):
                    self._observe_bus(
                        vehicle, frame_number, signal_controller, vehicles
                    )
        except Exception:
            pass

    def _register(self, bus, frame_number):
        record = {
            "bus_id": bus.bus_id,
            "route_id": getattr(bus, "route_id", ""),
            "spawn_frame": frame_number,
            "spawn_sim_time": frame_number / FRAMES_PER_SECOND,
            "first_stop_frame": None,
            "nodes": [
                _new_node_event(node_x) for node_x in getattr(bus, "route_nodes", [])
            ],
            "completion_frame": None,
            "completion_sim_time": None,
            "total_wait_frames": 0,
            "passengers": int(getattr(bus, "passengers", 0)),
        }
        self._records[bus.bus_id] = record
        return record

    def _observe_bus(self, bus, frame_number, signal_controller, vehicles):
        record = self._records.get(bus.bus_id)
        if record is None:
            record = self._register(bus, frame_number)

        stopped = float(getattr(bus, "speed", 0.0)) < STOP_SPEED_THRESHOLD
        if stopped:
            record["total_wait_frames"] += 1
            if record["first_stop_frame"] is None:
                record["first_stop_frame"] = frame_number

        passed_nodes = getattr(bus, "passed_nodes", set())
        for node in record["nodes"]:
            node_x = node["node_x"]
            if node["node_clear_frame"] is not None:
                continue
            if node_x in passed_nodes:
                node["node_clear_frame"] = frame_number
                if node["stop_bar_cross_frame"] is None:
                    node["stop_bar_cross_frame"] = frame_number
                if not node["tsp_treated"]:
                    self._attribute_from_history(node, bus.bus_id, signal_controller)
                continue

            # Only the bus's current leg accrues approach state; a later node
            # stays untouched until the bus actually reaches it.
            leg = bus.get_active_route_leg(canvas.INT_X)
            if not leg or leg["node_x"] != node_x:
                continue
            upstream = bus.is_front_bumper_upstream(
                node_x, canvas.H_Y, canvas.ROAD_W, canvas.STOP
            )
            if node["arrival_frame"] is None:
                distance = bus.distance_to_node_stop_bar(
                    node_x, h_y=canvas.H_Y, road_w=canvas.ROAD_W, stop_offset=canvas.STOP
                )
                zone = self._eligibility_px(signal_controller)
                if 0 <= distance <= zone:
                    node["arrival_frame"] = frame_number
                    self._capture_arrival_context(
                        node, bus, node_x, distance, signal_controller, vehicles
                    )
            if upstream:
                if stopped:
                    node["node_wait_frames"] += 1
            elif node["stop_bar_cross_frame"] is None:
                node["stop_bar_cross_frame"] = frame_number
            self._attribute_from_live_request(node, bus.bus_id, signal_controller)

    @staticmethod
    def _queue_ahead_count(bus, node_x, vehicles):
        bus_distance = bus.distance_to_node_stop_bar(
            node_x, canvas.H_Y, canvas.ROAD_W, canvas.STOP
        )
        count = 0
        for other in vehicles or []:
            if other is bus or other.direction != bus.direction:
                continue
            if float(getattr(other, "speed", 0.0)) >= STOP_SPEED_THRESHOLD:
                continue
            other_distance = other.distance_to_node_stop_bar(
                node_x, canvas.H_Y, canvas.ROAD_W, canvas.STOP
            )
            if 0.0 <= other_distance < bus_distance:
                count += 1
        return count

    def _capture_arrival_context(
        self, node, bus, node_x, distance, signal_controller, vehicles
    ):
        """Freeze the pre-treatment arrival state used for matched TSP analysis."""
        node["eta_frames_at_decision"] = round(
            eta_frames_to_stop_bar(distance, getattr(bus, "speed", 0.0)), 2
        )
        node["queue_ahead_veh"] = self._queue_ahead_count(
            bus, node_x, vehicles
        )
        try:
            status = signal_controller.get_node_status(node_x)
            phase = int(status.get("phase_index"))
            colour = str((status.get("signals") or {}).get(bus.direction, "RED"))
            node["signal_phase_at_arrival"] = phase
            node["signal_colour_at_arrival"] = colour
            node["residual_green_frames"] = (
                max(
                    0,
                    int(signal_controller.get_green_time(node_x, phase))
                    - int(status.get("phase_timer_frames") or 0),
                )
                if colour == "GREEN"
                else 0
            )
            request = signal_controller.get_priority_status_for_bus(bus, node_x)
            node["tsp_requested"] = bool(
                isinstance(request, dict) and request.get("tsp_requested")
            ) or bool(signal_controller.is_bus_tsp_eligible(bus, node_x))
        except Exception:
            colour = None
        unrestricted_left = getattr(bus, "target_turn", "STRAIGHT") == "LEFT"
        node["would_have_stopped"] = bool(
            node["queue_ahead_veh"]
            or (not unrestricted_left and colour != "GREEN")
        )

    @staticmethod
    def _eligibility_px(signal_controller):
        try:
            return float(signal_controller.get_priority_eligibility_px())
        except Exception:
            return 500.0

    @staticmethod
    def _apply_tsp(node, action, adjust_frames):
        if not action or action == TSP_ACTION_NONE:
            return
        node["tsp_treated"] = True
        node["tsp_requested"] = True
        node["tsp_granted"] = True
        node["tsp_action"] = str(action)
        node["tsp_adjust_frames"] = max(
            int(node.get("tsp_adjust_frames") or 0), int(adjust_frames or 0)
        )

    def _attribute_from_live_request(self, node, bus_id, signal_controller):
        """Mirror the controller's live request while the bus is at this node."""
        try:
            controller_node = signal_controller.nodes.get(node["node_x"])
        except Exception:
            return
        if controller_node is None:
            return
        request = getattr(controller_node, "active_request", None)
        if request is None or getattr(request, "bus_id", None) != bus_id:
            return
        node["tsp_requested"] = bool(getattr(request, "tsp_requested", False))
        node["denial_reason"] = str(
            getattr(request, "denial_or_cancel_reason", "") or ""
        )
        self._apply_tsp(
            node,
            getattr(request, "tsp_action", TSP_ACTION_NONE),
            getattr(request, "tsp_adjust_frames", 0),
        )

    def _attribute_from_history(self, node, bus_id, signal_controller):
        """Catch a treatment finalized on the same frame the bus cleared."""
        try:
            controller_node = signal_controller.nodes.get(node["node_x"])
            history = list(getattr(controller_node, "terminal_history", []))
        except Exception:
            return
        for snapshot in reversed(history):
            if not isinstance(snapshot, dict):
                continue
            if snapshot.get("bus_id") != bus_id:
                continue
            node["tsp_requested"] = bool(snapshot.get("tsp_requested", False))
            node["denial_reason"] = str(
                snapshot.get("denial_or_cancel_reason", "")
                or snapshot.get("tsp_gate_reason", "")
                or ""
            )
            self._apply_tsp(
                node,
                snapshot.get("tsp_action", TSP_ACTION_NONE),
                snapshot.get("tsp_adjust_frames", 0),
            )
            return

    # -- completion -----------------------------------------------------------

    def complete(self, bus, frame_number, signal_controller=None):
        """Finalize and append the bus's record. Returns the record or None."""
        try:
            frame_number = int(frame_number)
            record = self._records.pop(bus.bus_id, None)
            if record is None:
                record = self._register(bus, frame_number)
                self._records.pop(bus.bus_id, None)
            passed_nodes = getattr(bus, "passed_nodes", set())
            for node in record["nodes"]:
                if node["node_clear_frame"] is None and node["node_x"] in passed_nodes:
                    node["node_clear_frame"] = frame_number
                if not node["tsp_treated"]:
                    self._attribute_from_history(node, bus.bus_id, signal_controller)
            record["completion_frame"] = frame_number
            record["completion_sim_time"] = frame_number / FRAMES_PER_SECOND
            self._append(record)
            return record
        except Exception:
            return None

    def _append(self, record):
        try:
            with self.path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(record) + "\n")
        except (OSError, TypeError, ValueError):
            pass
