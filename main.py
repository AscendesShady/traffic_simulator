# main.py
import pygame
import sys
import random
import math
import json
import subprocess
import atexit
import time
from pathlib import Path
import canvas_gemini as canvas
import control_panel
import guard
from vehicle import Vehicle, Bus
from signal_controller import SignalController
from telemetry_exporter import TelemetryExporter

# ==========================================================
# STOCHASTIC SPAWNER ENGINE (COMPOUND POISSON / EXACT RATE)
# ==========================================================
CONGESTION_MODEL = "Congestion Peak"
CONGESTION_PEAK_SECONDS = 30
CONGESTION_RECOVERY_SECONDS = 30
CONGESTION_RATE_MULTIPLIER = 4.0
CONGESTION_MIN_PEAK_RATE_VPM = 90.0
MAX_PENDING_ARRIVALS = 5000
POST_DISCHARGE_METER_SECONDS = 10
POST_DISCHARGE_RELEASE_GAP_FRAMES = 60
post_discharge_meter_frames_remaining = 0


def _new_spawner_state():
    return {
        "burst_queue": 0,
        "frames_since_spawn": 999,
        "pending_arrivals": 0,
        "congestion_cycle_frame": 0,
        "peak_active": False,
        "effective_rate_vpm": 0.0,
        "requested_arrivals": 0,
        "admitted_arrivals": 0,
        "overflow_arrivals": 0,
        "last_model": None,
    }


spawner_states = {
    approach_key: _new_spawner_state()
    for approach_key in ("EB", "WB", "A_NB", "A_SB", "B_NB", "B_SB")
}

bus_dispatch_counters = { r_id: 0.0 for r_id in control_panel.bus_routes_config.keys() }
bus_sequence_counter = 0
network_throughput = {
    "passengers_served_total": 0,
    "passengers_served_bus": 0,
    "passengers_served_car": 0,
    "vehicles_served_total": 0,
    "buses_served": 0,
    "cars_served": 0,
}
BASE_DIR = Path(__file__).resolve().parent
TELEMETRY_PATH = BASE_DIR / "traffic_state_telemetry.json"
DASHBOARD_PATH = BASE_DIR / "telemetry_dashboard.py"
AGENT_PATH = BASE_DIR / "agent.py"
DECISION_PATH = BASE_DIR / "decision.json"
DECISION_STALE_MULTIPLIER = 3
DECISION_STALE_FLOOR_SEC = 12.0
AGENT_TURN_LOG_PATH = BASE_DIR / "agent_turn_log.jsonl"
TELEMETRY_LOG_PATH = BASE_DIR / "telemetry_log.jsonl"
EXCEL_EXPORT_DIR = BASE_DIR / "excel_exports"
TELEMETRY_LOG_INTERVAL = 60
SESSION_ROUTE_IDS = (
    "R1_EB_A_NB",
    "R2_EB_B_NB",
    "R3_EB_ONLY",
    "R4_WB_A_SB",
    "R5_WB_B_SB",
    "R6_WB_ONLY",
)
_last_telemetry_log_frame = None


def reset_session_logs():
    """Start a clean pair of append-only logs for one simulator run."""
    global _last_telemetry_log_frame
    _last_telemetry_log_frame = None
    for path in (TELEMETRY_LOG_PATH, AGENT_TURN_LOG_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"Session log reset warning for {path.name}: {exc}")


def log_telemetry_sample(frame_number, payload):
    """Append one schema-derived telemetry sample at roughly one-second gaps."""
    global _last_telemetry_log_frame
    try:
        frame_number = int(frame_number)
    except (TypeError, ValueError, OverflowError):
        return False
    if not isinstance(payload, dict) or frame_number < 0:
        return False
    if _last_telemetry_log_frame is None:
        if frame_number < TELEMETRY_LOG_INTERVAL:
            return False
    elif (
        frame_number <= _last_telemetry_log_frame
        or frame_number - _last_telemetry_log_frame < TELEMETRY_LOG_INTERVAL
    ):
        return False

    network_throughput_state = payload.get("network_throughput", {})
    network_summary_state = payload.get("network_summary", {})
    if not isinstance(network_throughput_state, dict):
        network_throughput_state = {}
    if not isinstance(network_summary_state, dict):
        network_summary_state = {}
    ai_runtime = control_panel.global_config.get("ai_runtime", {})
    if not isinstance(ai_runtime, dict):
        ai_runtime = {}
    record = {
        "frame": frame_number,
        "sim_time_s": payload.get("simulation_time_seconds"),
        "passengers_served_total": network_throughput_state.get(
            "passengers_served_total"
        ),
        "passengers_served_bus": network_throughput_state.get(
            "passengers_served_bus"
        ),
        "passengers_served_car": network_throughput_state.get(
            "passengers_served_car"
        ),
        "buses_served": network_throughput_state.get("buses_served"),
        "cars_served": network_throughput_state.get("cars_served"),
        "pax_per_min_cumulative": network_throughput_state.get(
            "passengers_per_minute"
        ),
        "pax_per_min_recent": network_throughput_state.get(
            "passengers_per_minute_recent"
        ),
        "queues_vehicles": network_summary_state.get("queues"),
        "queues_passengers_est": network_summary_state.get(
            "queues_passengers_est"
        ),
        "vehicles_in_network": network_summary_state.get("total_vehicles"),
        "ai_armed": bool(ai_runtime.get("armed", False)),
        "ai_last_status": ai_runtime.get("last_status"),
    }
    try:
        with TELEMETRY_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record) + "\n")
    except (OSError, TypeError, ValueError):
        return False
    _last_telemetry_log_frame = frame_number
    return True


def _read_jsonl_rows(path):
    """Read all complete JSON-object lines and ignore a damaged crash tail."""
    rows = []
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        pass
    return rows


def export_session_excel(output_path=None):
    """Build a two-sheet workbook from the crash-safe session JSONL logs."""
    try:
        from openpyxl import Workbook
    except ImportError:
        print("openpyxl not installed; skipping Excel export")
        return None

    decisions = _read_jsonl_rows(AGENT_TURN_LOG_PATH)
    telemetry_rows = _read_jsonl_rows(TELEMETRY_LOG_PATH)
    if not decisions and not telemetry_rows:
        return None

    try:
        workbook = Workbook()
        decisions_sheet = workbook.active
        decisions_sheet.title = "Decisions"
        decision_header = [
            "turn",
            "timestamp",
            "model",
            "status",
            "reason",
            "pax_per_min_at_turn",
        ]
        for route_id in SESSION_ROUTE_IDS:
            decision_header.extend(
                (f"{route_id}_tsp", f"{route_id}_dbl")
            )
        decision_header.extend(("locked_routes", "minimap", "raw_output"))
        decisions_sheet.append(decision_header)
        for decision in decisions:
            flags = decision.get("flags", {})
            if not isinstance(flags, dict):
                flags = {}
            row = [
                decision.get("turn"),
                decision.get("timestamp"),
                decision.get("model"),
                decision.get("status"),
                decision.get("reason", ""),
                decision.get("pax_per_min_recent"),
            ]
            for route_id in SESSION_ROUTE_IDS:
                route_flags = flags.get(route_id, {})
                if not isinstance(route_flags, dict):
                    route_flags = {}
                row.extend(
                    (
                        bool(route_flags.get("tsp", False)),
                        bool(route_flags.get("dbl", False)),
                    )
                )
            locked_routes = decision.get("locked_routes", [])
            if not isinstance(locked_routes, (list, tuple, set, frozenset)):
                locked_routes = []
            row.extend(
                (
                    ",".join(str(route_id) for route_id in locked_routes),
                    str(decision.get("minimap", ""))[:32767],
                    str(decision.get("raw_output", ""))[:32767],
                )
            )
            decisions_sheet.append(row)

        telemetry_sheet = workbook.create_sheet("Telemetry")
        telemetry_header = [
            "frame",
            "sim_time_s",
            "passengers_served_total",
            "passengers_served_bus",
            "passengers_served_car",
            "buses_served",
            "cars_served",
            "pax_per_min_cumulative",
            "pax_per_min_recent",
            "vehicles_in_network",
            "ai_armed",
            "ai_last_status",
            "queues_vehicles",
            "queues_passengers_est",
        ]
        telemetry_sheet.append(telemetry_header)
        for telemetry in telemetry_rows:
            telemetry_sheet.append(
                [
                    telemetry.get("frame"),
                    telemetry.get("sim_time_s"),
                    telemetry.get("passengers_served_total"),
                    telemetry.get("passengers_served_bus"),
                    telemetry.get("passengers_served_car"),
                    telemetry.get("buses_served"),
                    telemetry.get("cars_served"),
                    telemetry.get("pax_per_min_cumulative"),
                    telemetry.get("pax_per_min_recent"),
                    telemetry.get("vehicles_in_network"),
                    telemetry.get("ai_armed"),
                    telemetry.get("ai_last_status"),
                    json.dumps(telemetry.get("queues_vehicles")),
                    json.dumps(telemetry.get("queues_passengers_est")),
                ]
            )

        destination = (
            Path(output_path)
            if output_path is not None
            else EXCEL_EXPORT_DIR
            / f"session_export_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(destination)
        workbook.close()
        print(f"Session exported: {destination}")
        return destination
    except Exception as exc:
        print(f"Session Excel export warning: {exc}")
        return None


def _set_ai_flags(flags):
    for route_id, route_flags in flags.items():
        route_config = control_panel.bus_routes_config[route_id]
        route_config["tsp_enabled"] = route_flags["tsp"]
        route_config["dbl_enabled"] = route_flags["dbl"]


def merge_ai_decision(path=None):
    """Validate and merge one file-based AI decision into live route config."""
    runtime = control_panel.global_config.setdefault("ai_runtime", {})
    decision_path = DECISION_PATH if path is None else Path(path)
    try:
        with decision_path.open("r", encoding="utf-8") as decision_file:
            decision = json.load(decision_file)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        runtime["last_status"] = "WAITING_FOR_DECISION"
        return False

    tick_seconds = runtime.get("tick_seconds", 5)
    try:
        tick_seconds = float(tick_seconds or 5)
    except (TypeError, ValueError, OverflowError):
        tick_seconds = 5.0
    if not math.isfinite(tick_seconds) or tick_seconds <= 0:
        tick_seconds = 5.0
    stale_after = max(
        tick_seconds * DECISION_STALE_MULTIPLIER,
        DECISION_STALE_FLOOR_SEC,
    )
    timestamp = decision.get("timestamp") if isinstance(decision, dict) else None
    try:
        age = time.time() - float(timestamp)
    except (TypeError, ValueError, OverflowError):
        age = None
    if age is None or not math.isfinite(age) or age > stale_after:
        _set_ai_flags(guard.all_off_flags())
        runtime["last_status"] = "STALE_DECISION"
        return False

    try:
        runtime["last_turn"] = int(decision.get("turn", 0))
    except (AttributeError, TypeError, ValueError, OverflowError):
        runtime["last_turn"] = 0

    flags = guard.validate_flags(decision)
    if flags is None:
        _set_ai_flags(guard.all_off_flags())
        runtime["last_status"] = "INVALID_DECISION"
        return False

    _set_ai_flags(flags)
    decision_status = decision.get("status", "OK")
    runtime["last_status"] = (
        decision_status
        if decision_status in ("OK", "HELD_ALL_OFF")
        else "OK"
    )
    return True


def reset_all_spawner_states():
    """Clear stochastic, burst, and congestion backlog state."""
    global post_discharge_meter_frames_remaining
    for state in spawner_states.values():
        state.clear()
        state.update(_new_spawner_state())
    post_discharge_meter_frames_remaining = 0


def begin_post_discharge_metering():
    """Start a bounded low-rate admission period after recovery."""
    global post_discharge_meter_frames_remaining
    post_discharge_meter_frames_remaining = POST_DISCHARGE_METER_SECONDS * 60


def post_discharge_admission_allowed():
    """Permit at most one source admission attempt per simulated second."""
    if post_discharge_meter_frames_remaining <= 0:
        return True
    return (
        post_discharge_meter_frames_remaining
        % POST_DISCHARGE_RELEASE_GAP_FRAMES
        == 0
    )


def advance_post_discharge_metering():
    global post_discharge_meter_frames_remaining
    if post_discharge_meter_frames_remaining > 0:
        post_discharge_meter_frames_remaining -= 1


def _reset_state_for_model_change(state, model_type):
    if state["last_model"] == model_type:
        return
    state.update(
        {
            "burst_queue": 0,
            "frames_since_spawn": 999,
            "pending_arrivals": 0,
            "congestion_cycle_frame": 0,
            "peak_active": False,
            "effective_rate_vpm": 0.0,
            "requested_arrivals": 0,
            "admitted_arrivals": 0,
            "overflow_arrivals": 0,
            "last_model": model_type,
        }
    )


def get_demand_telemetry():
    """Return a JSON-safe snapshot of configured and queued source demand."""
    result = {}
    discharge_runtime = control_panel.global_config.get("discharge_runtime", {})
    admission_suspended = bool(discharge_runtime.get("active", False))
    for approach_key, state in spawner_states.items():
        config = control_panel.approach_configs.get(approach_key, {})
        result[approach_key] = {
            "active": bool(config.get("active", False)),
            "model": config.get("model", "Poisson"),
            "configured_rate_vpm": int(config.get("rate", 0)),
            "effective_rate_vpm": round(state["effective_rate_vpm"], 1),
            "peak_active": bool(state["peak_active"]),
            "pending_arrivals": int(state["pending_arrivals"]),
            "requested_arrivals": int(state["requested_arrivals"]),
            "admitted_arrivals": int(state["admitted_arrivals"]),
            "overflow_arrivals": int(state["overflow_arrivals"]),
            "admission_suspended": admission_suspended,
            "post_recovery_metering": post_discharge_meter_frames_remaining > 0,
        }
    return result


def is_discharge_demand_suspended(signal_controller):
    """Whether recovery owns the network and new demand must remain frozen."""
    return bool(
        signal_controller.is_discharge_active()
        or control_panel.global_config.get("discharge_start_requested", False)
    )

def should_spawn_vehicle(approach_key, model_type, rate_v_m):
    state = spawner_states[approach_key]
    _reset_state_for_model_change(state, model_type)

    if rate_v_m <= 0:
        state["effective_rate_vpm"] = 0.0
        state["peak_active"] = False
        return False

    state["frames_since_spawn"] += 1
    lam_frame = rate_v_m / 3600.0  # Target arrival rate per 1/60s tick

    if model_type == CONGESTION_MODEL:
        peak_frames = CONGESTION_PEAK_SECONDS * 60
        cycle_frames = peak_frames + CONGESTION_RECOVERY_SECONDS * 60
        cycle_position = state["congestion_cycle_frame"] % cycle_frames
        state["peak_active"] = cycle_position < peak_frames
        state["congestion_cycle_frame"] += 1

        effective_rate = (
            max(
                float(rate_v_m) * CONGESTION_RATE_MULTIPLIER,
                CONGESTION_MIN_PEAK_RATE_VPM,
            )
            if state["peak_active"]
            else float(rate_v_m)
        )
        state["effective_rate_vpm"] = effective_rate
        arrival_probability = 1.0 - math.exp(-effective_rate / 3600.0)
        if random.random() < arrival_probability:
            state["requested_arrivals"] += 1
            if state["pending_arrivals"] < MAX_PENDING_ARRIVALS:
                state["pending_arrivals"] += 1
            else:
                state["overflow_arrivals"] += 1

        # A blocked spawn does not consume this demand. try_spawn_vehicle()
        # decrements the backlog only after safely adding a vehicle.
        return state["pending_arrivals"] > 0

    if model_type == "Neg Binomial":
        state["effective_rate_vpm"] = float(rate_v_m)
        state["peak_active"] = False
        MEAN_BURST_SIZE = 2.0
        p_burst_start = lam_frame / MEAN_BURST_SIZE

        if random.random() < p_burst_start:
            state["burst_queue"] += random.randint(1, 3)

        if state["burst_queue"] > 0 and state["frames_since_spawn"] >= 18:
            state["frames_since_spawn"] = 0
            return True
        return False

    elif model_type == "Binomial":
        state["effective_rate_vpm"] = float(rate_v_m)
        state["peak_active"] = False
        min_gap_frames = int(1800.0 / rate_v_m) if rate_v_m > 0 else 999
        if state["frames_since_spawn"] < min_gap_frames:
            return False

        p_metered = lam_frame * 2.0
        if random.random() < p_metered:
            state["frames_since_spawn"] = 0
            return True
        return False

    elif model_type == "Poisson":
        state["effective_rate_vpm"] = float(rate_v_m)
        state["peak_active"] = False
        prob = 1.0 - math.exp(-lam_frame)
        if random.random() < prob:
            state["frames_since_spawn"] = 0
            return True
        return False

    else:
        state["effective_rate_vpm"] = float(rate_v_m)
        state["peak_active"] = False
        if random.random() < lam_frame:
            state["frames_since_spawn"] = 0
            return True
        return False


def try_spawn_vehicle(vehicles, approach_key, direction, spawn_coord, lane_coords, approach_cfg, min_gap=40):
    model_type = approach_cfg.get("model", "Poisson")
    rate_v_m = approach_cfg.get("rate", 12)

    if not should_spawn_vehicle(approach_key, model_type, rate_v_m):
        return

    straight_ratio = approach_cfg.get("turn_split", 0.80)
    if random.random() < straight_ratio:
        target_turn = "STRAIGHT"
        lane_idx = random.choice([0, 1])
    else:
        target_turn = "LEFT"
        lane_idx = 2

    target_lane_coord = lane_coords[lane_idx]

    # Entry Clearance Check
    for v in vehicles:
        if v.direction == direction:
            if direction in ("EB", "WB") and abs(v.x - spawn_coord) < min_gap and abs(v.y - target_lane_coord) < 8:
                return
            elif direction in ("NB", "SB") and abs(v.y - spawn_coord) < min_gap and abs(v.x - target_lane_coord) < 8:
                return

    state = spawner_states[approach_key]
    if model_type == CONGESTION_MODEL:
        state["pending_arrivals"] = max(0, state["pending_arrivals"] - 1)
        state["admitted_arrivals"] += 1
        state["frames_since_spawn"] = 0
    elif model_type == "Neg Binomial" and state["burst_queue"] > 0:
        state["burst_queue"] -= 1

    heavy_ratio = approach_cfg.get("heavy_ratio", 0.10)
    is_heavy = random.random() < heavy_ratio

    speed = random.uniform(0.8, 1.1) if is_heavy else random.uniform(1.0, 1.4)
    colors = [(50, 150, 250), (250, 100, 50), (250, 200, 50), (150, 50, 250), (50, 200, 150)]
    color = (120, 120, 140) if is_heavy else random.choice(colors)

    if direction in ("EB", "WB"):
        vehicles.append(Vehicle(
            x=spawn_coord, y=target_lane_coord, direction=direction,
            max_speed=speed, color=color, is_heavy=is_heavy,
            target_turn=target_turn, lane_index=lane_idx
        ))
    else:
        assigned_node_x = min(canvas.INT_X, key=lambda node_x: abs(node_x - target_lane_coord))
        vehicles.append(Vehicle(
            x=target_lane_coord, y=spawn_coord, direction=direction,
            max_speed=speed, color=color, is_heavy=is_heavy,
            target_turn=target_turn, lane_index=lane_idx,
            assigned_node_x=assigned_node_x
        ))


def check_and_dispatch_buses(vehicles, lane_options, dt):
    global bus_sequence_counter

    for r_id, r_cfg in control_panel.bus_routes_config.items():
        if not r_cfg["active"]:
            r_cfg["manual_dispatch"] = False
            bus_dispatch_counters[r_id] = 0.0  # Reset headway retention when inactive
            continue

        should_dispatch = False

        if r_cfg["manual_dispatch"]:
            should_dispatch = True
            r_cfg["manual_dispatch"] = False

        elif r_cfg["headway_sec"] > 0:
            bus_dispatch_counters[r_id] += dt
            if bus_dispatch_counters[r_id] >= r_cfg["headway_sec"]:
                should_dispatch = True
                bus_dispatch_counters[r_id] = 0.0
        else:
            # Headway OFF means a fresh interval begins when re-enabled.
            bus_dispatch_counters[r_id] = 0.0

        if should_dispatch:
            # A manual departure also restarts the automatic headway clock.
            bus_dispatch_counters[r_id] = 0.0
            direction = r_cfg["origin"]
            spawn_coord = -40 if direction == "EB" else canvas.WIDTH + 40
            
            waypoints = r_cfg.get("waypoints", {})
            first_node_x = 300 if direction == "EB" else 700
            first_node_turn = waypoints.get(first_node_x, "STRAIGHT")
            lane_idx = 2 if first_node_turn == "LEFT" else 1

            target_lane_coord = lane_options[direction][lane_idx]

            # ENTRY CLEARANCE CHECK
            entry_blocked = False
            for v in vehicles:
                if v.direction == direction:
                    same_lane = (
                        abs(v.y - target_lane_coord) < 8
                        if direction in ("EB", "WB")
                        else abs(v.x - target_lane_coord) < 8
                    )
                    longitudinal_gap = (
                        abs(v.x - spawn_coord)
                        if direction in ("EB", "WB")
                        else abs(v.y - spawn_coord)
                    )
                    if same_lane and longitudinal_gap < 60:
                        entry_blocked = True
                        break

            if entry_blocked:
                r_cfg["manual_dispatch"] = True
                continue

            bus_sequence_counter += 1
            unique_bus_id = f"BUS_{r_id}_{bus_sequence_counter:03d}"

            route_info = {
                "route_id": r_id,
                "origin": direction,
                "destination": r_cfg["destination"],
                "waypoints": waypoints,
                "lanes": dict(r_cfg.get("lanes", {})),
            }

            vehicles.append(Bus(
                x=spawn_coord, y=target_lane_coord, direction=direction,
                route_info=route_info, bus_id=unique_bus_id
            ))


def main():
    reset_session_logs()
    pygame.init()
    pygame.font.init()
    font = pygame.font.SysFont("Consolas", 13, bold=True)

    screen = pygame.display.set_mode((canvas.WIDTH, canvas.HEIGHT))
    pygame.display.set_caption("Urban Network Simulation")

    signals = SignalController(global_config=control_panel.global_config, yellow_time=60, red_clearance_time=60)
    telemetry = TelemetryExporter(filename=TELEMETRY_PATH, export_interval_frames=10)

    lane_options = {
        "EB": [canvas.H_Y - (0.5 * canvas.LANE), canvas.H_Y - (1.5 * canvas.LANE), canvas.H_Y - (2.5 * canvas.LANE)],
        "WB": [canvas.H_Y + (0.5 * canvas.LANE), canvas.H_Y + (1.5 * canvas.LANE), canvas.H_Y + (2.5 * canvas.LANE)],
        "A_NB": [canvas.INT_X[0] - (0.5 * canvas.LANE), canvas.INT_X[0] - (1.5 * canvas.LANE), canvas.INT_X[0] - (2.5 * canvas.LANE)],
        "A_SB": [canvas.INT_X[0] + (0.5 * canvas.LANE), canvas.INT_X[0] + (1.5 * canvas.LANE), canvas.INT_X[0] + (2.5 * canvas.LANE)],
        "B_NB": [canvas.INT_X[1] - (0.5 * canvas.LANE), canvas.INT_X[1] - (1.5 * canvas.LANE), canvas.INT_X[1] - (2.5 * canvas.LANE)],
        "B_SB": [canvas.INT_X[1] + (0.5 * canvas.LANE), canvas.INT_X[1] + (1.5 * canvas.LANE), canvas.INT_X[1] + (2.5 * canvas.LANE)],
    }

    vehicles = []
    
    # 1. Start Control Panel
    root = control_panel.create_dashboard_window()

    # 2. Start Decoupled Telemetry Dashboard as a Subprocess
    print("Launching Telemetry Dashboard...")
    dashboard_proc = subprocess.Popen(
        [sys.executable, str(DASHBOARD_PATH)],
        cwd=str(BASE_DIR),
    )

    print("Launching LLM Control Agent...")
    agent_proc = subprocess.Popen(
        [sys.executable, str(AGENT_PATH)],
        cwd=str(BASE_DIR),
    )

    # 3. Register cleanup, then build the workbook after agent writes stop.
    def cleanup():
        try:
            dashboard_proc.terminate()
        except Exception:
            pass
        try:
            agent_proc.terminate()
        except Exception:
            pass
        try:
            agent_proc.wait(timeout=2)
        except Exception:
            pass
        export_session_excel()
    atexit.register(cleanup)

    master_frame_count = 0
    dt_step = 1.0 / 60.0
    time_accumulator = 0.0
    last_wall_time = time.monotonic()
    max_catchup_seconds = 0.25
    # Keep Tk's event loop responsive after a window drag or scheduler delay.
    # Normal 3x operation needs about three steps per 16 ms callback; six gives
    # headroom without allowing a long catch-up burst to monopolize the UI.
    max_steps_per_callback = 6
    discharge_was_active = False

    def simulation_step():
        nonlocal master_frame_count, time_accumulator, last_wall_time
        nonlocal discharge_was_active

        now = time.monotonic()
        elapsed = min(max(0.0, now - last_wall_time), max_catchup_seconds)
        last_wall_time = now

        if not control_panel.global_config.get("is_running", True):
            pygame.quit()
            sys.exit()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                control_panel.global_config["is_running"] = False
                pygame.quit()
                try: root.destroy()
                except Exception: pass
                sys.exit()

        if control_panel.global_config["reset_triggered"]:
            vehicles.clear()
            reset_all_spawner_states()
            signals.reset_discharge()
            network_throughput.update({key: 0 for key in network_throughput})
            control_panel.global_config["reset_triggered"] = False

        sim_speed = control_panel.global_config.get("sim_speed", 1.0)
        is_paused = control_panel.global_config.get("is_paused", False)

        if not is_paused:
            time_accumulator += elapsed * sim_speed

            # FIXED TIMESTEP LOOP: Physics, Signals, Spawners run exactly at 60Hz intervals
            steps_this_callback = 0
            while (
                time_accumulator >= dt_step
                and steps_this_callback < max_steps_per_callback
            ):
                master_frame_count += 1
                cfgs = control_panel.approach_configs

                discharge_suspended = is_discharge_demand_suspended(signals)
                if not discharge_suspended:
                    if post_discharge_admission_allowed():
                        if cfgs["EB"]["active"]: try_spawn_vehicle(vehicles, "EB", "EB", -20, lane_options["EB"], cfgs["EB"])
                        if cfgs["WB"]["active"]: try_spawn_vehicle(vehicles, "WB", "WB", canvas.WIDTH + 20, lane_options["WB"], cfgs["WB"])
                        if cfgs["A_NB"]["active"]: try_spawn_vehicle(vehicles, "A_NB", "NB", canvas.HEIGHT + 20, lane_options["A_NB"], cfgs["A_NB"])
                        if cfgs["A_SB"]["active"]: try_spawn_vehicle(vehicles, "A_SB", "SB", -20, lane_options["A_SB"], cfgs["A_SB"])
                        if cfgs["B_NB"]["active"]: try_spawn_vehicle(vehicles, "B_NB", "NB", canvas.HEIGHT + 20, lane_options["B_NB"], cfgs["B_NB"])
                        if cfgs["B_SB"]["active"]: try_spawn_vehicle(vehicles, "B_SB", "SB", -20, lane_options["B_SB"], cfgs["B_SB"])

                    if post_discharge_meter_frames_remaining <= 0:
                        check_and_dispatch_buses(vehicles, lane_options, dt_step)
                if master_frame_count % 30 == 0:
                    ai_runtime = control_panel.global_config.get("ai_runtime", {})
                    if ai_runtime.get("armed", False):
                        merge_ai_decision()
                signals.update(vehicles=vehicles)
                discharge_is_active = signals.is_discharge_active()
                if discharge_was_active and not discharge_is_active:
                    begin_post_discharge_metering()
                if not discharge_is_active:
                    advance_post_discharge_metering()
                discharge_was_active = discharge_is_active

                active_signal_data = signals.get_all_signals(canvas.INT_X)
                
                for v in vehicles[:]:
                    v.update(
                        signal_data=active_signal_data,
                        int_x_list=canvas.INT_X,
                        h_y=canvas.H_Y,
                        road_w=canvas.ROAD_W,
                        stop_offset=canvas.STOP,
                        lane_w=canvas.LANE,
                        all_vehicles=vehicles,
                        signal_controller=signals
                    )
                    if (v.x < -60 or v.x > canvas.WIDTH + 60 or v.y < -60 or v.y > canvas.HEIGHT + 60):
                        if len(getattr(v, "passed_nodes", set())) > 0:
                            passengers = int(getattr(v, "passengers", 0))
                            network_throughput["passengers_served_total"] += passengers
                            network_throughput["vehicles_served_total"] += 1
                            if isinstance(v, Bus):
                                network_throughput["passengers_served_bus"] += passengers
                                network_throughput["buses_served"] += 1
                            else:
                                network_throughput["passengers_served_car"] += passengers
                                network_throughput["cars_served"] += 1
                        vehicles.remove(v)
                
                time_accumulator -= dt_step
                steps_this_callback += 1

            # If the desktop held the callback during a window move, discard
            # excessive wall-time debt instead of freezing the UI to replay it.
            if time_accumulator >= dt_step:
                time_accumulator = min(time_accumulator, dt_step)
        else:
            # Paused wall time must never become a catch-up burst on resume.
            time_accumulator = 0.0

        active_signal_data = signals.get_all_signals(canvas.INT_X)
        active_dbl_data = signals.get_all_dbl_states(canvas.INT_X, vehicles)

        canvas.draw_network(
            screen, 
            signal_data=active_signal_data, 
            dbl_states=active_dbl_data, 
            font=font,
            is_paused=is_paused
        )

        for v in vehicles:
            v.draw(screen)

        telemetry_exported = telemetry.export(
            signal_controller=signals,
            vehicles=vehicles,
            frame_number=master_frame_count,
            demand_state=get_demand_telemetry(),
            throughput_state=network_throughput,
        )
        if telemetry_exported:
            try:
                with TELEMETRY_PATH.open("r", encoding="utf-8") as telemetry_file:
                    exported_payload = json.load(telemetry_file)
                log_telemetry_sample(
                    exported_payload.get("frame_number", master_frame_count),
                    exported_payload,
                )
            except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                pass
        pygame.display.flip()

        # Constant GUI polling rate (~60 FPS) decoupled from simulation speed
        root.after(16, simulation_step)

    root.after(16, simulation_step)
    root.mainloop()

if __name__ == "__main__":
    main()
