# main.py
import pygame
import sys
import random
import math
import subprocess
import atexit
import time
from pathlib import Path
import canvas_gemini as canvas
import control_panel
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
BASE_DIR = Path(__file__).resolve().parent
TELEMETRY_PATH = BASE_DIR / "traffic_state_telemetry.json"
DASHBOARD_PATH = BASE_DIR / "telemetry_dashboard.py"


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

    # 3. Register cleanup to prevent zombie dashboard processes on exit
    def cleanup():
        try:
            dashboard_proc.terminate()
        except Exception:
            pass
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

        telemetry.export(
            signal_controller=signals,
            vehicles=vehicles,
            frame_number=master_frame_count,
            demand_state=get_demand_telemetry(),
        )
        pygame.display.flip()

        # Constant GUI polling rate (~60 FPS) decoupled from simulation speed
        root.after(16, simulation_step)

    root.after(16, simulation_step)
    root.mainloop()

if __name__ == "__main__":
    main()
