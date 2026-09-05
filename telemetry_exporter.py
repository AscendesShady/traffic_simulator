"""Atomic, source-relative telemetry export for the simulator."""

import json
import os
from pathlib import Path
import tempfile
import time

import canvas_gemini as canvas
import control_panel
from vehicle import Bus


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TELEMETRY_PATH = BASE_DIR / "traffic_state_telemetry.json"
CAR_OCCUPANCY = 4


class TelemetryExporter:
    def __init__(self, filename=DEFAULT_TELEMETRY_PATH, export_interval_frames=10):
        self.filename = Path(filename).resolve()
        self.export_interval = max(1, int(export_interval_frames))
        self.frame_counter = 0
        self._throughput_samples = []
        self._trend_window_seconds = 30.0
        self._last_total_served = 0

    def compute_queue_counts(self, vehicles):
        queues = {"EB": 0, "WB": 0, "A_NB": 0, "A_SB": 0, "B_NB": 0, "B_SB": 0}
        for vehicle in vehicles:
            if vehicle.speed >= 0.25:
                continue
            target_node = vehicle.get_next_target_node(canvas.INT_X)
            if not vehicle.is_front_bumper_upstream(
                target_node, canvas.H_Y, canvas.ROAD_W, canvas.STOP
            ):
                continue
            if vehicle.direction == "EB":
                queues["EB"] += 1
            elif vehicle.direction == "WB":
                queues["WB"] += 1
            elif vehicle.direction == "NB":
                queues["A_NB" if target_node == canvas.INT_X[0] else "B_NB"] += 1
            elif vehicle.direction == "SB":
                queues["A_SB" if target_node == canvas.INT_X[0] else "B_SB"] += 1
        return queues

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
        is_pending = priority_state in (
            "REQUESTED",
            "CONFLICT_YELLOW",
            "ALL_RED_CLEARANCE",
        )
        is_active = priority_state in ("PRIORITY_ACTIVE", "PRIORITY_CLEARING")
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
            "priority_granted": priority_state == "PRIORITY_ACTIVE",
            "priority_clearing": priority_state == "PRIORITY_CLEARING",
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
        queues = self.compute_queue_counts(vehicles)
        queues_passengers = {
            approach: vehicle_count * CAR_OCCUPANCY
            for approach, vehicle_count in queues.items()
        }
        demand_state = demand_state or {}
        throughput_state = throughput_state or {}
        pending_demand = sum(
            int(item.get("pending_arrivals", 0))
            for item in demand_state.values()
            if isinstance(item, dict)
        )
        buses = [
            self._bus_state(vehicle, signal_controller)
            for vehicle in vehicles
            if isinstance(vehicle, Bus)
        ]
        routes_block = {}
        for route_id, route_config in control_panel.bus_routes_config.items():
            route_buses = [bus for bus in buses if bus["route_id"] == route_id]
            nearest = None
            if route_buses:
                nearest = min(
                    route_buses,
                    key=lambda bus: bus["eta_to_stop_bar_sec_freeflow"],
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
            node_states[str(node_x)] = {**status, "phase": label}

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
            "simulation_paused": bool(control_panel.global_config.get("is_paused", False)),
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
                "car_occupancy_assumed": CAR_OCCUPANCY,
                "pending_demand": pending_demand,
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
            },
            "demand_generation": demand_state,
            "routes": routes_block,
            "active_buses": buses,
            "approaching_buses": approaching,
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
