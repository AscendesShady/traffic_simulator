"""Atomic, source-relative telemetry export for the simulator."""

import json
import os
from pathlib import Path
import tempfile
import time

from src.ui import canvas_gemini as canvas
from src.ui import control_panel
from src.core.vehicle import (
    Bus,
    DBL_LANE_INDEX,
    dbl_lane_is_obstructed,
    dbl_lane_queue_ahead,
)
from src.telemetry import real_world_units as units


BASE_DIR = Path(__file__).resolve().parents[2]
assert (BASE_DIR / "requirements.txt").exists(), (
    f"BASE_DIR does not resolve to the repo root: {BASE_DIR}"
)
DEFAULT_TELEMETRY_PATH = BASE_DIR / "data" / "traffic_state_telemetry.json"

APPROACHES = ("EB", "WB", "NB", "SB")
# A downstream stretch counts as blocked when the free road left on it is
# shorter than about two queued vehicles (car length plus standing gap each),
# i.e. a vehicle released into it has essentially nowhere to go.
DOWNSTREAM_BLOCKED_PX = 2 * units.SIM_QUEUE_SPACING_PX
# Standing gap a stopped vehicle keeps to the one ahead (vehicle.py SAFE_GAP).
VEHICLE_QUEUE_GAP_PX = units.SIM_QUEUE_GAP_PX


class TelemetryExporter:
    def __init__(self, filename=DEFAULT_TELEMETRY_PATH, export_interval_frames=10):
        self.filename = Path(filename).resolve()
        self.export_interval = max(1, int(export_interval_frames))
        self.frame_counter = 0
        self._throughput_samples = []
        self._trend_window_seconds = 30.0
        self._last_total_served = 0

    def reset_session(self):
        """Clear per-run rate history and force the next export at frame zero."""
        self.frame_counter = self.export_interval - 1
        self._throughput_samples.clear()
        self._last_total_served = 0

    @staticmethod
    def _empty_queue_table():
        return {
            str(node_x): {approach: 0 for approach in ("EB", "WB", "NB", "SB")}
            for node_x in canvas.INT_X
        }

    @staticmethod
    def _iter_queued_vehicles(vehicles):
        """Yield (node_key, approach, vehicle) for every stopped upstream vehicle.

        A vehicle is queued when it is effectively stationary and its front
        bumper is still upstream of the stop bar of its next target node.
        """
        node_keys = {str(node_x) for node_x in canvas.INT_X}
        for vehicle in vehicles:
            if vehicle.speed >= 0.25:
                continue
            target_node = vehicle.get_next_target_node(canvas.INT_X)
            node_key = str(target_node)
            if node_key not in node_keys:
                continue
            if not vehicle.is_front_bumper_upstream(
                target_node, canvas.H_Y, canvas.ROAD_W, canvas.STOP
            ):
                continue
            if vehicle.direction not in ("EB", "WB", "NB", "SB"):
                continue
            yield node_key, vehicle.direction, vehicle

    def compute_queue_counts_by_node(self, vehicles):
        """Count stopped upstream vehicles by node and physical approach."""
        queues = self._empty_queue_table()
        for node_key, approach, _vehicle in self._iter_queued_vehicles(vehicles):
            queues[node_key][approach] += 1
        return queues

    def compute_queue_passengers_by_node(self, vehicles):
        """Sum ACTUAL passengers of stopped upstream vehicles, by node and approach.

        Uses each vehicle's real passenger count (car=4, truck=1, bus=45)
        rather than a flat per-vehicle weight, so a queued bus is worth 45 and
        a queued truck is worth 1. This is the same weighting the throughput
        counter applies to served vehicles.
        """
        passengers = self._empty_queue_table()
        for node_key, approach, vehicle in self._iter_queued_vehicles(vehicles):
            passengers[node_key][approach] += int(getattr(vehicle, "passengers", 0))
        return passengers

    def compute_bus_distribution(self, vehicles):
        """Count buses present in the network right now, by location.

        Uses the same node x approach grid as the queue tables (the node a
        bus is next headed to, and its current direction of travel) so this
        reads as "how many buses are on each corridor/approach segment" --
        not just buses queued at a stop bar, but every bus still in transit
        toward a node. Also reports the DBL-lane occupancy and per-route
        counts, all as of this same export's timestamp/frame.
        """
        by_node_approach = self._empty_queue_table()
        by_route = {}
        in_dbl_lane = 0
        total = 0
        node_keys = {str(node_x) for node_x in canvas.INT_X}
        for vehicle in vehicles:
            if not isinstance(vehicle, Bus):
                continue
            total += 1
            route_id = str(getattr(vehicle, "route_id", "")) or "UNASSIGNED"
            by_route[route_id] = by_route.get(route_id, 0) + 1
            if getattr(vehicle, "lane_index", None) == DBL_LANE_INDEX:
                in_dbl_lane += 1
            target_node = vehicle.get_next_target_node(canvas.INT_X)
            node_key = str(target_node)
            if node_key in node_keys and vehicle.direction in APPROACHES:
                by_node_approach[node_key][vehicle.direction] += 1
        return {
            "total_buses": total,
            "buses_by_node_approach": by_node_approach,
            "buses_by_route": by_route,
            "buses_in_dbl_lane": in_dbl_lane,
        }

    def compute_queue_length_by_node(self, vehicles):
        """Report how far back each approach's queue reaches, in metres.

        For every node and approach this is the stop-bar distance of the
        furthest-back queued vehicle (the same front-bumper-to-stop-bar
        distance the bus ETA uses), converted with the saturation-anchored
        metres-per-pixel scale. An approach with nothing queued reports 0.0.
        """
        lengths_px = self._empty_queue_table()
        for node_key, approach, vehicle in self._iter_queued_vehicles(vehicles):
            distance = vehicle.distance_to_node_stop_bar(
                int(node_key), canvas.H_Y, canvas.ROAD_W, canvas.STOP
            )
            if distance > lengths_px[node_key][approach]:
                lengths_px[node_key][approach] = distance
        return {
            node_key: {
                approach: round(units.px_to_m(max(0.0, px)), 1)
                for approach, px in row.items()
            }
            for node_key, row in lengths_px.items()
        }

    @staticmethod
    def _downstream_stretch(node_x, approach):
        """Return (axis, low, high) for the road a movement enters after node_x.

        The stretch runs from the far edge of the node's conflict box, in the
        approach's direction of travel, to the next node's conflict box or the
        canvas edge, whichever comes first. Straight-through travel only.
        """
        half_w = canvas.ROAD_W / 2.0
        sorted_nodes = sorted(canvas.INT_X)
        if approach == "EB":
            nexts = [nx for nx in sorted_nodes if nx > node_x]
            end = (nexts[0] - half_w) if nexts else float(canvas.WIDTH)
            return "x", node_x + half_w, end
        if approach == "WB":
            prevs = [nx for nx in sorted_nodes if nx < node_x]
            start = (prevs[-1] + half_w) if prevs else 0.0
            return "x", start, node_x - half_w
        if approach == "NB":
            return "y", 0.0, canvas.H_Y - half_w
        return "y", canvas.H_Y + half_w, float(canvas.HEIGHT)

    def compute_downstream_space_by_node(self, vehicles):
        """Estimate the free road on the far side of each node, per approach.

        Returns (space_m, blocked): both are node key -> approach dicts. The
        free space is the downstream stretch length minus the room taken by
        vehicles already travelling on it (each vehicle's length plus one
        standing gap), spread across the approach's lanes so the figure reads
        as metres of free road per lane. ``blocked`` is True when that free
        space is under ``DOWNSTREAM_BLOCKED_PX``: a green would only release
        vehicles into a road that cannot absorb them.
        """
        half_w = canvas.ROAD_W / 2.0
        space_m = self._empty_queue_table()
        blocked = {
            node_key: {approach: False for approach in APPROACHES}
            for node_key in space_m
        }
        for node_x in canvas.INT_X:
            node_key = str(node_x)
            for approach in APPROACHES:
                axis, low, high = self._downstream_stretch(node_x, approach)
                length_px = max(0.0, high - low)
                occupied_px = 0.0
                for vehicle in vehicles:
                    if vehicle.direction != approach:
                        continue
                    if axis == "x":
                        along, across = vehicle.x, vehicle.y
                        # Same horizontal road; the direction filter already
                        # picks the EB or WB carriageway.
                        on_road = abs(across - canvas.H_Y) <= half_w
                    else:
                        along, across = vehicle.y, vehicle.x
                        # Vertical roads are node-specific.
                        on_road = abs(across - node_x) <= half_w
                    if on_road and low <= along <= high:
                        occupied_px += float(vehicle.length) + VEHICLE_QUEUE_GAP_PX
                free_px = max(
                    0.0, length_px - occupied_px / max(1, canvas.LANES)
                )
                space_m[node_key][approach] = round(units.px_to_m(free_px), 1)
                blocked[node_key][approach] = free_px < DOWNSTREAM_BLOCKED_PX
        return space_m, blocked

    @staticmethod
    def _flatten_queue_counts(queues_by_node):
        """Preserve the original public aggregate queue contract."""
        node_a = queues_by_node.get(str(canvas.INT_X[0]), {})
        node_b = queues_by_node.get(str(canvas.INT_X[1]), {})
        return {
            "EB": int(node_a.get("EB", 0)) + int(node_b.get("EB", 0)),
            "WB": int(node_a.get("WB", 0)) + int(node_b.get("WB", 0)),
            "A_NB": int(node_a.get("NB", 0)),
            "A_SB": int(node_a.get("SB", 0)),
            "B_NB": int(node_b.get("NB", 0)),
            "B_SB": int(node_b.get("SB", 0)),
        }

    def compute_queue_counts(self, vehicles):
        return self._flatten_queue_counts(
            self.compute_queue_counts_by_node(vehicles)
        )

    @staticmethod
    def build_vehicle_position_snapshot(vehicles):
        """Return a compact, JSON-safe position/state row for every vehicle.

        This is observational telemetry only. It lets an exported AI turn be
        reconstructed spatially without feeding any new value back into the
        controller or changing vehicle movement.
        """
        snapshot = []
        for index, vehicle in enumerate(vehicles or [], start=1):
            is_bus = isinstance(vehicle, Bus)
            kind = "bus" if is_bus else ("truck" if vehicle.is_heavy else "car")
            snapshot.append(
                {
                    "snapshot_id": (
                        str(getattr(vehicle, "bus_id", ""))
                        if is_bus
                        else f"{kind}-{index}"
                    ),
                    "vehicle_type": kind,
                    "route_id": str(getattr(vehicle, "route_id", "")) or None,
                    "x_px": round(float(getattr(vehicle, "x", 0.0)), 3),
                    "y_px": round(float(getattr(vehicle, "y", 0.0)), 3),
                    "direction": str(getattr(vehicle, "direction", "")),
                    "lane_index": getattr(vehicle, "lane_index", None),
                    "target_turn": str(getattr(vehicle, "target_turn", "")),
                    "leg_state": str(getattr(vehicle, "leg_state", "")),
                    "speed_px_per_frame": round(
                        float(getattr(vehicle, "speed", 0.0)), 4
                    ),
                    "max_speed_px_per_frame": round(
                        float(getattr(vehicle, "max_speed", 0.0)), 4
                    ),
                    "passengers": int(getattr(vehicle, "passengers", 0)),
                    "assigned_node_x": getattr(vehicle, "assigned_node_x", None),
                    "passed_nodes": sorted(
                        int(node) for node in getattr(vehicle, "passed_nodes", set())
                    ),
                    "lane_vacate_target": getattr(
                        vehicle, "lane_vacate_target", None
                    ),
                    "must_hold_for_lane": bool(
                        getattr(vehicle, "must_hold_for_lane", False)
                    ),
                    "route_merge_hold_active": bool(
                        getattr(vehicle, "route_merge_hold_active", False)
                    ),
                    "dbl_merge_yield_slow": bool(
                        getattr(vehicle, "dbl_merge_yield_slow", False)
                    ),
                }
            )
        return snapshot

    @staticmethod
    def _phase_label(node_status):
        if node_status.get("discharge_active", False):
            state = node_status.get("discharge_state", "ACTIVE")
            return f"NETWORK_DISCHARGE_{state}"
        if node_status["priority_state"] != "NORMAL":
            return node_status["priority_state"]
        return {
            0: "EW_GREEN",
            1: "EW_YELLOW",
            2: "ALL_RED",
            3: "NS_GREEN",
            4: "NS_YELLOW",
            5: "ALL_RED",
        }.get(node_status["phase_index"], "UNKNOWN")

    def _bus_state(self, bus, signal_controller):
        leg = bus.get_active_route_leg(canvas.INT_X)
        target_node = leg["node_x"] if leg else bus.get_next_target_node(canvas.INT_X)
        distance = bus.distance_to_node_stop_bar(
            target_node, canvas.H_Y, canvas.ROAD_W, canvas.STOP
        )
        free_flow_speed = max(getattr(bus, "max_speed", 1.0), 1e-6)
        eta_frames_freeflow = distance / free_flow_speed if distance > 0 else 0.0
        live_speed = max(bus.speed, 1e-6)
        eta_frames_live = distance / live_speed if distance > 0 else 0.0
        eta_frames_live = min(eta_frames_live, 6000.0)
        live_cfg = control_panel.bus_routes_config.get(bus.route_id, bus.route_info)
        priority = signal_controller.get_priority_status_for_bus(bus, target_node)
        latest_terminal = signal_controller.get_latest_terminal_status_for_bus(bus)
        priority_state = priority.get("state") if priority else None
        # REQUESTED = queued behind another bus; ARMED = the node's live
        # request (DBL lane reserved, TSP watching for its moment); the two
        # TSP_* states = a bounded signal adjustment actually being applied.
        is_pending = priority_state == "REQUESTED"
        is_adjusting = priority_state in ("TSP_EXTENDING", "TSP_EARLY_TRUNCATE")
        is_active = priority_state == "ARMED" or is_adjusting
        is_clearing = is_active and bus.leg_state != "APPROACHING"
        tsp_requested = bool(priority and priority.get("tsp_requested"))
        dbl_requested = bool(priority and priority.get("dbl_requested"))
        return {
            "bus_id": bus.bus_id,
            "route_id": bus.route_id,
            "direction": bus.direction,
            "x": round(bus.x, 1),
            "y": round(bus.y, 1),
            "speed": round(bus.speed, 2),
            "passengers": bus.passengers,
            "lane_index": bus.lane_index,
            "in_dbl_lane": bool(
                leg is not None and bus.lane_index == DBL_LANE_INDEX
            ),
            "target_node_x": target_node,
            "distance_to_stop_bar_px": round(distance, 1),
            "eta_to_stop_bar_sec_freeflow": round(eta_frames_freeflow / 60.0, 2),
            "eta_to_stop_bar_sec_live": round(eta_frames_live / 60.0, 2),
            "target_turn": bus.target_turn,
            "route_leg": leg,
            "leg_state": bus.leg_state,
            "tsp_enabled": bool(live_cfg.get("tsp_enabled", False)),
            "dbl_enabled": bool(live_cfg.get("dbl_enabled", False)),
            "priority_requested": priority is not None,
            "priority_transitioning": is_pending,
            "priority_granted": is_adjusting,
            "priority_clearing": is_clearing,
            "tsp_action": priority.get("tsp_action", "none") if priority else "none",
            "tsp_adjust_frames": (
                int(priority.get("tsp_adjust_frames", 0)) if priority else 0
            ),
            "priority_terminal": latest_terminal if priority is None else None,
            "latest_priority_terminal_event": latest_terminal,
            "tsp_priority_pending": tsp_requested and is_pending,
            "dbl_priority_pending": dbl_requested and is_pending,
            "tsp_active_triggered": tsp_requested and is_active,
            "dbl_active_triggered": dbl_requested and is_active,
            "priority_request": priority,
        }

    def build_payload(
        self,
        signal_controller,
        vehicles,
        frame_number,
        demand_state=None,
        throughput_state=None,
    ):
        queues_by_node = self.compute_queue_counts_by_node(vehicles)
        queues = self._flatten_queue_counts(queues_by_node)
        queues_passengers_by_node = self.compute_queue_passengers_by_node(vehicles)
        queues_passengers = self._flatten_queue_counts(queues_passengers_by_node)
        queue_length_m_by_node = self.compute_queue_length_by_node(vehicles)
        downstream_space_m_by_node, downstream_blocked_by_node = (
            self.compute_downstream_space_by_node(vehicles)
        )
        demand_state = demand_state or {}
        throughput_state = throughput_state or {}
        pending_demand = sum(
            int(item.get("pending_arrivals", 0))
            for item in demand_state.values()
            if isinstance(item, dict)
        )
        stopped_frames = int(throughput_state.get("stopped_vehicle_frames", 0) or 0)
        vehicles_served = int(throughput_state.get("vehicles_served_total", 0) or 0)
        bus_delay_frames = int(
            throughput_state.get("bus_passenger_delay_frames", 0) or 0
        )
        car_delay_frames = int(
            throughput_state.get("car_passenger_delay_frames", 0) or 0
        )
        bus_pax_served = int(throughput_state.get("passengers_served_bus", 0) or 0)
        car_pax_served = int(throughput_state.get("passengers_served_car", 0) or 0)
        bus_passenger_delay_sec = round(bus_delay_frames / 60.0, 1)
        car_passenger_delay_sec = round(car_delay_frames / 60.0, 1)
        vehicle_positions = self.build_vehicle_position_snapshot(vehicles)
        buses = [
            self._bus_state(vehicle, signal_controller)
            for vehicle in vehicles
            if isinstance(vehicle, Bus)
        ]
        bus_objects = {
            vehicle.bus_id: vehicle
            for vehicle in vehicles
            if isinstance(vehicle, Bus)
        }
        routes_block = {}
        for route_id, route_config in control_panel.bus_routes_config.items():
            route_buses = [bus for bus in buses if bus["route_id"] == route_id]
            nearest = None
            if route_buses:
                nearest = min(
                    route_buses,
                    key=lambda bus: bus["eta_to_stop_bar_sec_freeflow"],
                )
            # Whether the nearest approaching bus could actually complete a
            # DBL merge right now. With no approaching bus there is nothing to
            # judge, so the route reports False rather than a guess.
            nearest_object = (
                bus_objects.get(nearest["bus_id"]) if nearest else None
            )
            target_node = nearest["target_node_x"] if nearest else None
            dbl_queue_ahead = (
                dbl_lane_queue_ahead(
                    nearest_object,
                    vehicles,
                    canvas.H_Y,
                    target_node=target_node,
                    lane_w=canvas.LANE,
                )
                if nearest_object is not None and target_node is not None
                else []
            )
            dbl_lane_obstructed = bool(
                nearest is not None
                and nearest["route_leg"] is not None
                and nearest_object is not None
                and dbl_lane_is_obstructed(
                    nearest_object,
                    vehicles,
                    canvas.H_Y,
                    canvas.LANE,
                    target_node=target_node,
                )
            )
            routes_block[route_id] = {
                "route_name": route_config.get("name", route_id),
                "active": bool(route_config.get("active", False)),
                "tsp_enabled": bool(route_config.get("tsp_enabled", False)),
                "dbl_enabled": bool(route_config.get("dbl_enabled", False)),
                "buses_on_route": len(route_buses),
                "route_passengers_total": sum(
                    int(bus["passengers"]) for bus in route_buses
                ),
                "nearest_bus_id": nearest["bus_id"] if nearest else None,
                "nearest_bus_eta_sec": (
                    nearest["eta_to_stop_bar_sec_freeflow"] if nearest else None
                ),
                "nearest_bus_target_node_x": (
                    nearest["target_node_x"] if nearest else None
                ),
                "nearest_bus_priority_granted": (
                    bool(nearest["priority_granted"]) if nearest else False
                ),
                "nearest_bus_priority_pending": (
                    bool(nearest["priority_transitioning"]) if nearest else False
                ),
                "nearest_bus_in_dbl_lane": (
                    bool(nearest["in_dbl_lane"]) if nearest else False
                ),
                "dbl_lane_queue_ahead": len(dbl_queue_ahead),
                "dbl_lane_obstructed": dbl_lane_obstructed,
            }
        approaching = [
            bus
            for bus in buses
            if bus["route_leg"] is not None and bus["distance_to_stop_bar_px"] >= 0
        ]
        node_states = {}
        phase_labels = []
        for node_x in canvas.INT_X:
            status = signal_controller.get_node_status(node_x)
            label = self._phase_label(status)
            phase_labels.append(label)
            node_key = str(node_x)
            node_queue_passengers = queues_passengers_by_node[node_key]
            node_states[node_key] = {
                **status,
                "phase": label,
                "queues": queues_by_node[node_key],
                "queues_passengers_est": node_queue_passengers,
                "total_waiting_passengers_est": sum(
                    node_queue_passengers.values()
                ),
                "queue_length_m": queue_length_m_by_node[node_key],
                "downstream_space_m": downstream_space_m_by_node[node_key],
                "downstream_blocked": downstream_blocked_by_node[node_key],
            }

        current_phase = phase_labels[0] if len(set(phase_labels)) == 1 else "MIXED"
        green_frames = signal_controller.get_green_time()
        yellow_frames = signal_controller.yellow_time
        all_red_frames = signal_controller.red_clearance_time
        discharge_status = signal_controller.get_discharge_status()

        simulation_seconds = frame_number / 60.0
        total_served = int(throughput_state.get("passengers_served_total", 0))
        if total_served < self._last_total_served:
            self._throughput_samples.clear()
        self._last_total_served = total_served

        sample = (simulation_seconds, total_served)
        if not self._throughput_samples or self._throughput_samples[-1] != sample:
            self._throughput_samples.append(sample)
        cutoff = simulation_seconds - self._trend_window_seconds
        self._throughput_samples = [
            item for item in self._throughput_samples if item[0] >= cutoff
        ]
        recent_rate = 0.0
        if len(self._throughput_samples) >= 2:
            start_seconds, start_passengers = self._throughput_samples[0]
            end_seconds, end_passengers = self._throughput_samples[-1]
            elapsed_minutes = (end_seconds - start_seconds) / 60.0
            if elapsed_minutes > 1e-6:
                recent_rate = (
                    end_passengers - start_passengers
                ) / elapsed_minutes

        return {
            "schema_version": 3,
            "timestamp": round(time.time(), 3),
            "frame_number": frame_number,
            "simulation_time_seconds": round(simulation_seconds, 3),
            "simulation_running": bool(
                control_panel.global_config.get("is_running", False)
            ),
            "simulation_paused": bool(control_panel.global_config.get("is_paused", False)),
            # Signal timing is a key experimental parameter, so the calibrated
            # saturation flow and the resulting splits travel with every frame.
            "signal_timing": {
                "measured_saturation_flow_veh_per_hr": (
                    control_panel.global_config.get("measured_saturation_flow")
                ),
                # Each independent node uses its own derived Webster cycle.
                "cycle_time_sec": {
                    str(node_x): cycle
                    for node_x, cycle in (
                        control_panel.global_config.get("cycle_time_sec") or {}
                    ).items()
                },
                "calibrating": bool(
                    control_panel.global_config.get("calibrating", False)
                ),
                "webster_splits": {
                    str(node_x): split
                    for node_x, split in (
                        control_panel.global_config.get("webster_splits") or {}
                    ).items()
                },
            },
            "simulation_speed": float(control_panel.global_config.get("sim_speed", 1.0)),
            "signal_state": {
                "current_phase": current_phase,
                "timing": {
                    "frames_per_second": 60,
                    "green_frames": green_frames,
                    "yellow_frames": yellow_frames,
                    "all_red_frames": all_red_frames,
                    "nominal_cycle_frames": 2
                    * (green_frames + yellow_frames + all_red_frames),
                },
                "nodes": node_states,
            },
            "network_discharge": discharge_status,
            "network_summary": {
                "total_vehicles": len(vehicles),
                "total_buses": len(buses),
                "passenger_volume": sum(
                    int(getattr(vehicle, "passengers", 0)) for vehicle in vehicles
                ),
                "queues": queues,
                "queues_passengers_est": queues_passengers,
                "queues_by_node": queues_by_node,
                "queues_passengers_est_by_node": queues_passengers_by_node,
                # Physical queue extent and far-side room, in metres (display
                # scale from real_world_units; never fed back into physics).
                "queue_length_m_by_node": queue_length_m_by_node,
                "downstream_space_m_by_node": downstream_space_m_by_node,
                "downstream_blocked_by_node": downstream_blocked_by_node,
                "pending_demand": pending_demand,
                # Live network mean speed (px/frame); the Units tab converts
                # it to km/h under the saturation-flow anchor.
                "mean_speed_px_per_frame": round(
                    sum(float(getattr(v, "speed", 0.0)) for v in vehicles)
                    / len(vehicles), 3
                ) if vehicles else None,
            },
            "network_throughput": {
                "passengers_served_total": total_served,
                "passengers_served_bus": int(
                    throughput_state.get("passengers_served_bus", 0)
                ),
                "passengers_served_car": int(
                    throughput_state.get("passengers_served_car", 0)
                ),
                "vehicles_served_total": int(
                    throughput_state.get("vehicles_served_total", 0)
                ),
                "buses_served": int(throughput_state.get("buses_served", 0)),
                "cars_served": int(throughput_state.get("cars_served", 0)),
                "passengers_per_minute": round(
                    total_served / max(simulation_seconds / 60.0, 1e-9), 1
                ),
                "passengers_per_minute_recent": round(recent_rate, 1),
                "trend_window_seconds": self._trend_window_seconds,
                # Stopped delay: vehicle-seconds spent below the queue speed
                # threshold, and its mean per served vehicle (HCM control
                # delay proxy for the Units tab's level of service).
                "stopped_vehicle_seconds": round(stopped_frames / 60.0, 1),
                "mean_stopped_delay_sec_per_vehicle": (
                    round(stopped_frames / 60.0 / vehicles_served, 2)
                    if vehicles_served else None
                ),
            },
            # Passenger-weighted delay, split bus vs car/truck: the standard
            # transit-priority metric (occupancy x wait), since a bus and a
            # car waiting the same time do not cost passengers the same.
            "delay": {
                "bus_passenger_delay_sec": bus_passenger_delay_sec,
                "car_passenger_delay_sec": car_passenger_delay_sec,
                "mean_bus_passenger_delay_sec": (
                    round(bus_passenger_delay_sec / bus_pax_served, 2)
                    if bus_pax_served else None
                ),
                "mean_car_passenger_delay_sec": (
                    round(car_passenger_delay_sec / car_pax_served, 2)
                    if car_pax_served else None
                ),
                "total_person_hours_delay": round(
                    (bus_passenger_delay_sec + car_passenger_delay_sec) / 3600.0, 4
                ),
                "bus_person_hours_delay": round(
                    bus_passenger_delay_sec / 3600.0, 4
                ),
                "car_person_hours_delay": round(
                    car_passenger_delay_sec / 3600.0, 4
                ),
            },
            "demand_generation": demand_state,
            "routes": routes_block,
            "active_buses": buses,
            "approaching_buses": approaching,
            # Network-wide bus counts by node/approach segment, DBL lane, and
            # route, all as of this same payload's timestamp/frame_number.
            "bus_distribution": self.compute_bus_distribution(vehicles),
            # Full-network observational snapshot for decision auditing. The
            # model still receives the concise minimap; this records the
            # physical state from which that minimap was derived.
            "vehicle_positions": vehicle_positions,
        }

    def export(
        self,
        signal_controller,
        vehicles,
        frame_number,
        demand_state=None,
        throughput_state=None,
    ):
        self.frame_counter += 1
        if self.frame_counter % self.export_interval != 0:
            return False

        payload = self.build_payload(
            signal_controller,
            vehicles,
            frame_number,
            demand_state=demand_state,
            throughput_state=throughput_state,
        )
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.filename.parent,
                delete=False,
            ) as temp_file:
                json.dump(payload, temp_file, indent=2)
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_name = Path(temp_file.name)

            for attempt in range(4):
                try:
                    os.replace(temp_name, self.filename)
                    return True
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(0.01)
        except Exception as exc:
            print(f"[TelemetryExporter] Warning: Failed to export telemetry -> {exc}")
            if temp_name and temp_name.exists():
                try:
                    temp_name.unlink()
                except OSError:
                    pass
        return False
