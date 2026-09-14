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
from src.core.vehicle import Bus


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
    "tsp_treated",
    "tsp_action",
    "tsp_adjust_frames",
    "stop_bar_cross_frame",
    "node_wait_frames",
    "node_clear_frame",
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
        "tsp_treated": False,
        "tsp_action": TSP_ACTION_NONE,
        "tsp_adjust_frames": 0,
        "stop_bar_cross_frame": None,
        "node_wait_frames": 0,
        "node_clear_frame": None,
    }


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
                    self._observe_bus(vehicle, frame_number, signal_controller)
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

    def _observe_bus(self, bus, frame_number, signal_controller):
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
            if upstream:
                if stopped:
                    node["node_wait_frames"] += 1
            elif node["stop_bar_cross_frame"] is None:
                node["stop_bar_cross_frame"] = frame_number
            self._attribute_from_live_request(node, bus.bus_id, signal_controller)

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
