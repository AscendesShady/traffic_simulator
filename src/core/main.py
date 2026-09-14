# main.py
import pygame
import sys
import random
import math
import json
import csv
import subprocess
import atexit
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path
# Running this file directly (python src/.../x.py, or the IDE Run button) puts
# its own folder on sys.path instead of the repo root; put the root back so
# the src.* imports below resolve the same way they do under run.py / -m.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from src.ui import canvas_gemini as canvas
from src.ui import control_panel
from src.core import guard
from src.core import webster
from src.experiments import batch_runner
from src.core.vehicle import Vehicle, Bus
from src.core.signal_controller import SignalController
from src.ui.telemetry_dashboard import TelemetryDashboard, build_excel_export_filename
from src.telemetry.telemetry_exporter import TelemetryExporter
from src.telemetry.bus_event_log import BusEventTracker, write_bus_events_sheet
from src.telemetry import real_world_units

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
    # Vehicle-frames spent stopped (speed below the queue threshold); the
    # exporter turns this into mean stopped delay per served vehicle.
    "stopped_vehicle_frames": 0,
    # Passenger-frames of stopped delay, split bus vs car/truck -- the
    # exporter turns these into person-hours and per-passenger means.
    "bus_passenger_delay_frames": 0,
    "car_passenger_delay_frames": 0,
    # Congestion sampled once per simulated frame, for a run-long mean/max.
    "vehicles_in_network_frame_sum": 0,
    "vehicles_in_network_sample_count": 0,
    "vehicles_in_network_max": 0,
}


def accumulate_frame_metrics(vehicles):
    """Update network_throughput's per-frame cumulative counters.

    Called once per simulated frame (never per callback, so it stays exact
    regardless of how many frames one Tk callback processes): the stopped-
    vehicle count, that same delay split passenger-weighted by mode (a
    queued bus's whole passenger load counts toward bus delay, a queued
    car/truck's toward car delay -- the standard transit-priority weighting),
    and network occupancy for a run-long mean/max. Every counter here is
    cumulative from t=0 and is zeroed only by perform_full_reset.
    """
    stopped_count = 0
    stopped_bus_pax = 0
    stopped_car_pax = 0
    for v in vehicles:
        if v.speed < 0.25:
            stopped_count += 1
            pax = int(getattr(v, "passengers", 0))
            if isinstance(v, Bus):
                stopped_bus_pax += pax
            else:
                stopped_car_pax += pax
    network_throughput["stopped_vehicle_frames"] += stopped_count
    network_throughput["bus_passenger_delay_frames"] += stopped_bus_pax
    network_throughput["car_passenger_delay_frames"] += stopped_car_pax
    network_throughput["vehicles_in_network_frame_sum"] += len(vehicles)
    network_throughput["vehicles_in_network_sample_count"] += 1
    if len(vehicles) > network_throughput["vehicles_in_network_max"]:
        network_throughput["vehicles_in_network_max"] = len(vehicles)


BASE_DIR = Path(__file__).resolve().parents[2]
assert (BASE_DIR / "requirements.txt").exists(), (
    f"BASE_DIR does not resolve to the repo root: {BASE_DIR}"
)
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
RESULTS_DIR = BASE_DIR / "results"
TELEMETRY_PATH = DATA_DIR / "traffic_state_telemetry.json"
AGENT_PATH = BASE_DIR / "src" / "agents" / "agent.py"
DECISION_PATH = DATA_DIR / "decision.json"
DECISION_STALE_MULTIPLIER = 3
DECISION_STALE_FLOOR_SEC = 12.0
AGENT_TURN_LOG_PATH = LOGS_DIR / "agent_turn_log.jsonl"
TELEMETRY_LOG_PATH = LOGS_DIR / "telemetry_log.jsonl"
BUS_EVENTS_LOG_PATH = LOGS_DIR / "bus_events.jsonl"
EXCEL_EXPORT_DIR = RESULTS_DIR
# The master cross-run comparison dataset: one row per checkpoint of every
# run, appended forever. Never cleared by RESET -- only the operator deleting
# the file starts a new dataset. See append_experiment_summary_row.
EXPERIMENT_SUMMARY_PATH = RESULTS_DIR / "experiment_summary.csv"
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
# Per-bus lifecycle records (spawn, waits, TSP treatment, completion). The
# path is resolved lazily so tests can re-point BUS_EVENTS_LOG_PATH.
bus_event_tracker = BusEventTracker(lambda: BUS_EVENTS_LOG_PATH)


def apply_configured_random_seed():
    """Initialize the traffic RNG from the operator's current seed setting."""
    seed = control_panel.global_config.get("random_seed")
    if seed is not None:
        normalized_seed = int(seed)
        random.seed(normalized_seed)
        print(f"[SIM] Deterministic run, seed={normalized_seed}")
        return normalized_seed
    print("[SIM] Nondeterministic run (no seed set)")
    return None


def apply_configured_vehicle_speed_scale():
    """Snapshot the pending speed scale for the next traffic episode."""
    try:
        scale = float(control_panel.global_config.get("vehicle_speed_scale", 0.5))
    except (TypeError, ValueError, OverflowError):
        scale = 0.5
    scale = max(0.25, min(1.0, scale))
    control_panel.global_config["vehicle_speed_scale"] = scale
    control_panel.global_config["_active_vehicle_speed_scale"] = scale
    return scale


def get_active_vehicle_speed_scale():
    """Return the speed scale frozen at the most recent START/reset."""
    try:
        return float(
            control_panel.global_config.get("_active_vehicle_speed_scale", 0.5)
        )
    except (TypeError, ValueError, OverflowError):
        return 0.5


def calculate_startup_window_layout(screen_width, screen_height):
    """Return screen-aware positions for control, canvas, and telemetry windows."""
    try:
        screen_width = max(1, int(screen_width))
        screen_height = max(1, int(screen_height))
    except (TypeError, ValueError, OverflowError):
        screen_width, screen_height = 1920, 1080

    margin = 10
    gap = 10
    canvas_top = 30
    taskbar_reserve = 40
    usable_bottom = screen_height - taskbar_reserve
    usable_height = usable_bottom - margin
    control_width = min(880, screen_width - 2 * margin)
    control_height = min(1030, usable_height)
    right_x = margin + control_width + gap
    right_width = screen_width - right_x - margin
    telemetry_y = canvas_top + canvas.HEIGHT + gap
    telemetry_height = usable_bottom - telemetry_y

    if right_width >= canvas.WIDTH and telemetry_height >= 400:
        telemetry_width = min(max(900, canvas.WIDTH), right_width)
        return {
            "mode": "tiled",
            "canvas_position": (right_x, canvas_top),
            "control_geometry": (
                f"{control_width}x{control_height}+{margin}+{margin}"
            ),
            "telemetry_geometry": (
                f"{telemetry_width}x{min(430, telemetry_height)}+"
                f"{right_x}+{telemetry_y}"
            ),
        }

    # Small desktops cannot contain all three full interfaces without overlap.
    # Keep every window wholly on-screen and use a predictable cascade instead.
    telemetry_width = min(900, screen_width - 2 * margin)
    telemetry_height = min(430, usable_height)
    canvas_x = max(margin, (screen_width - canvas.WIDTH) // 2)
    control_x = max(margin, screen_width - control_width - margin)
    telemetry_x = max(margin, (screen_width - telemetry_width) // 2)
    return {
        "mode": "cascade",
        "canvas_position": (canvas_x, canvas_top),
        "control_geometry": (
            f"{control_width}x{control_height}+{control_x}+{margin}"
        ),
        "telemetry_geometry": (
            f"{telemetry_width}x{telemetry_height}+{telemetry_x}+30"
        ),
    }


def reset_session_logs():
    """Start a clean set of append-only logs for one simulator run."""
    global _last_telemetry_log_frame
    _last_telemetry_log_frame = None
    bus_event_tracker.reset()
    for path in (TELEMETRY_LOG_PATH, AGENT_TURN_LOG_PATH, BUS_EVENTS_LOG_PATH):
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


DECISION_HEADER_BASE = [
    "turn",
    "timestamp",
    "model",
    "status",
    "reason",
    "pax_per_min_at_turn",
]

TELEMETRY_HEADER = [
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

# Mirrors the dashboard's sheet layout so a timed-test workbook and a manual
# EXPORT ALL workbook can be compared column for column.
LLM_PERFORMANCE_HEADERS = [
    "turn",
    "timestamp",
    "model",
    "status",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "tokens_per_sec",
    "tsp_on_count",
    "dbl_on_count",
    "reason",
    "pax_per_min_recent",
]

LLM_SUMMARY_HEADERS = [
    "model",
    "turns",
    "guard_ok_pct",
    "held_pct",
    "avg_latency_ms",
    "avg_tokens_per_sec",
    "total_output_tokens",
]


def _write_decisions_sheet(sheet, decisions):
    header = list(DECISION_HEADER_BASE)
    for route_id in SESSION_ROUTE_IDS:
        header.extend((f"{route_id}_tsp", f"{route_id}_dbl"))
    header.extend(("locked_routes", "minimap", "raw_output"))
    sheet.append(header)
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
        sheet.append(row)


def _write_telemetry_sheet(sheet, telemetry_rows):
    sheet.append(list(TELEMETRY_HEADER))
    for telemetry in telemetry_rows:
        sheet.append(
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


def _flag_on_counts(decision):
    flags = decision.get("flags", {})
    if not isinstance(flags, dict):
        return 0, 0
    tsp_on = 0
    dbl_on = 0
    for route_flags in flags.values():
        if not isinstance(route_flags, dict):
            continue
        tsp_on += route_flags.get("tsp") is True
        dbl_on += route_flags.get("dbl") is True
    return tsp_on, dbl_on


def _numeric_or_none(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _write_llm_performance_sheet(sheet, decisions):
    """Per-turn model metrics taken straight from the agent turn log."""
    sheet.append(list(LLM_PERFORMANCE_HEADERS))
    for decision in decisions:
        tsp_on, dbl_on = _flag_on_counts(decision)
        sheet.append(
            [
                decision.get("turn"),
                decision.get("timestamp"),
                str(decision.get("model", "None")),
                str(decision.get("status", "UNKNOWN")),
                decision.get("latency_ms"),
                decision.get("input_tokens"),
                decision.get("output_tokens"),
                decision.get("tokens_per_sec"),
                tsp_on,
                dbl_on,
                str(decision.get("reason", "") or "")[:32767],
                decision.get("pax_per_min_recent"),
            ]
        )


def _average_of(values):
    numbers = [
        number
        for number in (_numeric_or_none(value) for value in values)
        if number is not None
    ]
    return round(sum(numbers) / len(numbers), 2) if numbers else None


def summarize_turn_log(decisions):
    """Aggregate guard outcomes and model throughput across logged turns."""
    rows = [row for row in decisions if isinstance(row, dict)]
    turns = len(rows)
    ok_turns = sum(row.get("status") == "OK" for row in rows)
    held_turns = sum(row.get("status") == "HELD_ALL_OFF" for row in rows)
    output_tokens = [
        number
        for number in (
            _numeric_or_none(row.get("output_tokens")) for row in rows
        )
        if number is not None
    ]
    models = [str(row.get("model", "None")) for row in rows]
    return {
        "model": max(set(models), key=models.count) if models else "None",
        "turns": turns,
        "guard_ok_pct": round(ok_turns / turns * 100.0, 2) if turns else 0.0,
        "held_pct": round(held_turns / turns * 100.0, 2) if turns else 0.0,
        "avg_latency_ms": _average_of(row.get("latency_ms") for row in rows),
        "avg_tokens_per_sec": _average_of(
            row.get("tokens_per_sec") for row in rows
        ),
        "total_output_tokens": sum(output_tokens) if output_tokens else 0.0,
    }


def _write_llm_summary_sheet(sheet, decisions):
    sheet.append(list(LLM_SUMMARY_HEADERS))
    rollup = summarize_turn_log(decisions)
    if rollup["turns"]:
        sheet.append(
            [
                rollup["model"],
                rollup["turns"],
                rollup["guard_ok_pct"],
                rollup["held_pct"],
                rollup["avg_latency_ms"],
                rollup["avg_tokens_per_sec"],
                rollup["total_output_tokens"],
            ]
        )


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
        _write_decisions_sheet(decisions_sheet, decisions)
        _write_telemetry_sheet(
            workbook.create_sheet("Telemetry"), telemetry_rows
        )

        destination = (
            Path(output_path)
            if output_path is not None
            else EXCEL_EXPORT_DIR
            / build_excel_export_filename(
                control_panel.global_config.get("ai_runtime", {}).get(
                    "model", "None"
                ),
                max(
                    float(
                        control_panel.global_config.get("sim_time_seconds")
                        or 0.0
                    ),
                    max(
                        (
                            float(row.get("sim_time_s") or 0.0)
                            for row in telemetry_rows
                            if isinstance(row, dict)
                        ),
                        default=0.0,
                    ),
                ),
                control_panel.global_config.get("random_seed"),
            )
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(destination)
        workbook.close()
        print(f"Session exported: {destination}")
        return destination
    except Exception as exc:
        print(f"Session Excel export warning: {exc}")
        return None


CONTROL_INPUT_HEADERS = ["section", "parameter", "value"]


def control_panel_input_rows():
    """Flatten every operator-set input into section/parameter/value rows.

    Kept as a flat table so a new config key never breaks the sheet, and so a
    workbook records exactly which inputs produced it.
    """
    config = control_panel.global_config
    ai_runtime = config.get("ai_runtime", {}) or {}
    rows = [
        ("Global", "random_seed", config.get("random_seed")),
        ("Global", "sim_speed", config.get("sim_speed")),
        ("Global", "test_duration_sim_seconds",
         config.get("test_duration_sim_seconds")),
        ("Global", "discharge_selection", config.get("discharge_selection")),
        ("Global", "llm_model", ai_runtime.get("model", "None")),
        ("Global", "llm_tick_seconds", ai_runtime.get("tick_seconds")),
        ("Global", "llm_armed", bool(ai_runtime.get("armed", False))),
        # Signal timing is derived output rather than an operator input. It is
        # recorded here so the exported run remains fully reproducible.
        ("Signal timing", "measured_saturation_flow_veh_per_hr",
         config.get("measured_saturation_flow")),
    ]
    for node_x, split in (config.get("webster_splits") or {}).items():
        section = f"Signal timing: node {node_x}"
        rows.extend(
            (
                (section, "EW_green_sec", split.get("EW_green_sec")),
                (section, "NS_green_sec", split.get("NS_green_sec")),
                (section, "EW_green_frames", split.get("EW_green_frames")),
                (section, "NS_green_frames", split.get("NS_green_frames")),
                (section, "cycle_time_sec", split.get("cycle_time_sec")),
                (section, "cycle_time_frames", split.get("cycle_time_frames")),
                (section, "lost_time_sec", split.get("lost_time_sec")),
                (section, "cycle_source", split.get("cycle_source")),
                (section, "min_cycle_sec", split.get("min_cycle_sec")),
                (section, "EW_critical_lane_flow_veh_hr",
                 split.get("EW_critical_lane_flow_veh_hr")),
                (section, "NS_critical_lane_flow_veh_hr",
                 split.get("NS_critical_lane_flow_veh_hr")),
                (section, "y_ew", split.get("y_ew")),
                (section, "y_ns", split.get("y_ns")),
                (section, "Y", split.get("Y")),
                (section, "oversaturated", split.get("oversaturated")),
                (section, "webster_optimal_cycle_sec",
                 split.get("webster_optimal_cycle_sec")),
            )
        )
    for key, name in control_panel.APPROACH_NAMES.items():
        approach = control_panel.approach_configs.get(key, {})
        section = f"Approach: {name}"
        rows.extend(
            (
                (section, "active", bool(approach.get("active", False))),
                (section, "generation_model", approach.get("model")),
                (section, "inflow_rate_veh_per_min", approach.get("rate")),
                (section, "turn_split", approach.get("turn_split")),
                (section, "heavy_ratio", approach.get("heavy_ratio")),
            )
        )
    for route_id, route in control_panel.bus_routes_config.items():
        section = f"Route: {route_id}"
        rows.extend(
            (
                (section, "name", route.get("name")),
                (section, "active", bool(route.get("active", False))),
                (section, "headway_sec", route.get("headway_sec")),
                (section, "tsp_enabled", bool(route.get("tsp_enabled", False))),
                (section, "dbl_enabled", bool(route.get("dbl_enabled", False))),
                (section, "manual_dispatch",
                 bool(route.get("manual_dispatch", False))),
            )
        )
    return rows


def write_control_panel_inputs_sheet(sheet):
    """Write the operator-input table onto an open worksheet."""
    sheet.append(list(CONTROL_INPUT_HEADERS))
    for row in control_panel_input_rows():
        sheet.append(list(row))
    return sheet


def write_unit_conversions_sheet(sheet, telemetry=None):
    """Record how this run's pixel/frame data maps to real-world units.

    The anchor constants and derived scales go in every workbook so each
    figure can cite its conversion basis. Telemetry defaults to the last
    exported snapshot on disk, so the live delay/speed rows are filled too.
    """
    if telemetry is None:
        try:
            telemetry = json.loads(TELEMETRY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            telemetry = None
    return real_world_units.write_unit_conversions_sheet(
        sheet,
        control_panel.global_config,
        control_panel.approach_configs,
        control_panel.APPROACH_NAMES,
        telemetry,
    )


EXPERIMENT_SUMMARY_HEADERS = (
    # Identity
    "timestamp", "model", "seed", "checkpoint_min", "test_duration_min",
    # Regime
    "vehicle_speed_scale", "saturation_flow_veh_hr", "cycle_sec", "vc_ratio",
    "demand_EB_veh_min", "demand_WB_veh_min", "demand_A_NB_veh_min",
    "demand_A_SB_veh_min", "demand_B_NB_veh_min", "demand_B_SB_veh_min",
    "demand_symmetric",
    # Throughput
    "passengers_served_total", "passengers_served_bus", "passengers_served_car",
    "buses_served", "cars_served", "pax_per_min",
    # Delay (the headline)
    "total_person_hours_delay", "bus_person_hours_delay", "car_person_hours_delay",
    "mean_bus_passenger_delay_sec", "mean_car_passenger_delay_sec",
    # Congestion
    "mean_vehicles_in_network", "max_vehicles_in_network",
    # Mechanism (the causal proof, from Bus Events)
    "buses_tsp_treated", "buses_untreated",
    "mean_treated_bus_wait_sec", "mean_untreated_bus_wait_sec",
    # LLM meta
    "mean_decision_latency_ms", "guard_reject_rate", "total_decisions",
    "decision_lag_sec_used",
    # LOS
    "level_of_service",
)


def _approach_demand_rates():
    """Configured veh/min per approach, or 0 for an inactive approach."""
    rates = {}
    for key in ("EB", "WB", "A_NB", "A_SB", "B_NB", "B_SB"):
        cfg = control_panel.approach_configs.get(key) or {}
        rates[key] = float(cfg.get("rate") or 0.0) if cfg.get("active", False) else 0.0
    return rates


def _demand_is_symmetric(rates):
    """True when each corridor's two directions carry the same configured
    demand: EB/WB, and each node's own NB/SB pair."""
    pairs = (("EB", "WB"), ("A_NB", "A_SB"), ("B_NB", "B_SB"))
    return all(abs(rates.get(a, 0.0) - rates.get(b, 0.0)) < 1e-9 for a, b in pairs)


def _bus_event_mechanism_summary(bus_events):
    """Treated-vs-untreated bus counts and mean total wait, from Bus Events.

    A bus counts as treated when any node in its lifecycle recorded a real
    TSP action (extension or early green); this is the same tsp_treated_any
    flag the Bus Events sheet exposes.
    """
    treated_wait_sec = []
    untreated_wait_sec = []
    for row in bus_events:
        if not isinstance(row, dict):
            continue
        nodes = row.get("nodes")
        if not isinstance(nodes, list):
            nodes = []
        treated = any(
            isinstance(node, dict) and node.get("tsp_treated")
            for node in nodes
        )
        wait_sec = float(row.get("total_wait_frames", 0) or 0) / 60.0
        (treated_wait_sec if treated else untreated_wait_sec).append(wait_sec)
    return {
        "buses_tsp_treated": len(treated_wait_sec),
        "buses_untreated": len(untreated_wait_sec),
        "mean_treated_bus_wait_sec": (
            round(sum(treated_wait_sec) / len(treated_wait_sec), 2)
            if treated_wait_sec else None
        ),
        "mean_untreated_bus_wait_sec": (
            round(sum(untreated_wait_sec) / len(untreated_wait_sec), 2)
            if untreated_wait_sec else None
        ),
    }


def build_experiment_summary_row(checkpoint_sim_seconds):
    """One cumulative-to-date row for the cross-run comparison dataset.

    Reads the same cumulative sources a checkpoint workbook reads --
    network_throughput's running counters, the Bus Events and Decisions
    JSONL logs, the Webster/saturation-flow config -- so the row and the
    workbook it accompanies always describe the same instant.
    """
    config = control_panel.global_config
    throughput = network_throughput
    checkpoint_seconds = float(checkpoint_sim_seconds or 0.0)

    bus_pax_served = int(throughput.get("passengers_served_bus", 0) or 0)
    car_pax_served = int(throughput.get("passengers_served_car", 0) or 0)
    bus_delay_sec = int(throughput.get("bus_passenger_delay_frames", 0) or 0) / 60.0
    car_delay_sec = int(throughput.get("car_passenger_delay_frames", 0) or 0) / 60.0
    stopped_frames = int(throughput.get("stopped_vehicle_frames", 0) or 0)
    vehicles_served_total = int(throughput.get("vehicles_served_total", 0) or 0)
    mean_stopped_delay_sec_per_vehicle = (
        round(stopped_frames / 60.0 / vehicles_served_total, 2)
        if vehicles_served_total else None
    )
    sample_count = int(throughput.get("vehicles_in_network_sample_count", 0) or 0)
    frame_sum = int(throughput.get("vehicles_in_network_frame_sum", 0) or 0)
    total_served = int(throughput.get("passengers_served_total", 0) or 0)

    decisions = _read_jsonl_rows(AGENT_TURN_LOG_PATH)
    decision_rollup = summarize_turn_log(decisions)
    bus_events = _read_jsonl_rows(BUS_EVENTS_LOG_PATH)
    mechanism = _bus_event_mechanism_summary(bus_events)

    rates = _approach_demand_rates()
    cycle_splits = config.get("cycle_time_sec") or {}
    cycle_sec = cycle_splits.get(canvas.INT_X[0], cycle_splits.get(str(canvas.INT_X[0])))
    mean_latency_ms = decision_rollup.get("avg_latency_ms")

    row = {
        "timestamp": round(time.time(), 3),
        "model": config.get("test_model", "None"),
        "seed": config.get("test_seed"),
        "checkpoint_min": round(checkpoint_seconds / 60.0, 3),
        "test_duration_min": round(
            float(config.get("test_duration_sim_seconds") or 0.0) / 60.0, 3
        ),
        "vehicle_speed_scale": config.get(
            "_active_vehicle_speed_scale", config.get("vehicle_speed_scale")
        ),
        "saturation_flow_veh_hr": config.get("measured_saturation_flow"),
        "cycle_sec": cycle_sec,
        "vc_ratio": real_world_units.network_vc_ratio(
            config, control_panel.approach_configs
        ),
        "demand_EB_veh_min": rates["EB"],
        "demand_WB_veh_min": rates["WB"],
        "demand_A_NB_veh_min": rates["A_NB"],
        "demand_A_SB_veh_min": rates["A_SB"],
        "demand_B_NB_veh_min": rates["B_NB"],
        "demand_B_SB_veh_min": rates["B_SB"],
        "demand_symmetric": _demand_is_symmetric(rates),
        "passengers_served_total": total_served,
        "passengers_served_bus": bus_pax_served,
        "passengers_served_car": car_pax_served,
        "buses_served": int(throughput.get("buses_served", 0) or 0),
        "cars_served": int(throughput.get("cars_served", 0) or 0),
        "pax_per_min": round(total_served / max(checkpoint_seconds / 60.0, 1e-9), 2),
        "total_person_hours_delay": round(
            (bus_delay_sec + car_delay_sec) / 3600.0, 4
        ),
        "bus_person_hours_delay": round(bus_delay_sec / 3600.0, 4),
        "car_person_hours_delay": round(car_delay_sec / 3600.0, 4),
        "mean_bus_passenger_delay_sec": (
            round(bus_delay_sec / bus_pax_served, 2) if bus_pax_served else None
        ),
        "mean_car_passenger_delay_sec": (
            round(car_delay_sec / car_pax_served, 2) if car_pax_served else None
        ),
        "mean_vehicles_in_network": (
            round(frame_sum / sample_count, 2) if sample_count else None
        ),
        "max_vehicles_in_network": int(
            throughput.get("vehicles_in_network_max", 0) or 0
        ),
        "buses_tsp_treated": mechanism["buses_tsp_treated"],
        "buses_untreated": mechanism["buses_untreated"],
        "mean_treated_bus_wait_sec": mechanism["mean_treated_bus_wait_sec"],
        "mean_untreated_bus_wait_sec": mechanism["mean_untreated_bus_wait_sec"],
        "mean_decision_latency_ms": mean_latency_ms,
        # The turn log does not distinguish a guard-rejected malformed
        # decision from an agent hold on stale telemetry; both are logged as
        # HELD_ALL_OFF, so this is that combined rate, not guard.py's
        # rejection log alone.
        "guard_reject_rate": (
            round(decision_rollup["held_pct"] / 100.0, 4)
            if decision_rollup.get("turns") else None
        ),
        "total_decisions": decision_rollup.get("turns", 0),
        # The actual lag this run's decisions carried, from their own
        # measured latency (0 for the rule-based comparator, ~model latency
        # for an LLM) -- not the agent's a-priori default.
        "decision_lag_sec_used": (
            round(mean_latency_ms / 1000.0, 3) if mean_latency_ms is not None else None
        ),
        "level_of_service": real_world_units.hcm_level_of_service(
            mean_stopped_delay_sec_per_vehicle
        ),
    }
    return row


def append_experiment_summary_row(checkpoint_sim_seconds):
    """Append one row to the accumulating experiment_summary.csv.

    Crash-safe and intentionally outside reset_session_logs: this file is
    the master dataset across the whole experiment (every run, every
    checkpoint), so RESET must never touch it -- only manual deletion does.
    """
    try:
        row = build_experiment_summary_row(checkpoint_sim_seconds)
        is_new = not EXPERIMENT_SUMMARY_PATH.exists()
        EXPERIMENT_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EXPERIMENT_SUMMARY_PATH.open(
            "a", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPERIMENT_SUMMARY_HEADERS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)
        return True
    except Exception as exc:
        print(f"Experiment summary append warning: {exc}")
        return False


def build_test_export_filename(
    model, duration_sim_seconds, seed, timestamp=None, checkpoint_sim_seconds=None
):
    """Return ``model_runtime_seed_DDMMYYYY_HHMMSS.xlsx``.

    For a checkpoint workbook, runtime is the cumulative checkpoint horizon.
    That keeps a 30-minute test's 5/10/15/30-minute exports distinct while
    retaining the exact field order used by every other Excel export.
    """
    runtime_seconds = (
        duration_sim_seconds if checkpoint_sim_seconds is None else checkpoint_sim_seconds
    )
    return build_excel_export_filename(
        model,
        runtime_seconds,
        seed,
        timestamp,
    )


def export_test_workbook(
    model, duration_sim_seconds, seed, destination=None, checkpoint_sim_seconds=None
):
    """Write the combined benchmark workbook for one timed-test checkpoint.

    Runs entirely inside the simulator process: every sheet is built from the
    crash-safe JSONL logs on disk, which are cumulative from run start, so a
    checkpoint mid-run and the final export at duration are both automatic
    cumulative snapshots -- just a read of the logs' current state, not an
    interval since the last checkpoint. ``checkpoint_sim_seconds`` is only
    used to name the file when ``destination`` is not given explicitly.
    """
    try:
        from openpyxl import Workbook
    except ImportError:
        print("openpyxl not installed; skipping test export")
        return None

    decisions = _read_jsonl_rows(AGENT_TURN_LOG_PATH)
    telemetry_rows = _read_jsonl_rows(TELEMETRY_LOG_PATH)
    bus_events = _read_jsonl_rows(BUS_EVENTS_LOG_PATH)

    try:
        workbook = Workbook()
        decisions_sheet = workbook.active
        decisions_sheet.title = "Decisions"
        _write_decisions_sheet(decisions_sheet, decisions)
        _write_telemetry_sheet(
            workbook.create_sheet("Telemetry"), telemetry_rows
        )
        _write_llm_performance_sheet(
            workbook.create_sheet("LLM Performance"), decisions
        )
        _write_llm_summary_sheet(
            workbook.create_sheet("LLM Summary"), decisions
        )
        write_control_panel_inputs_sheet(
            workbook.create_sheet("Control Panel Inputs")
        )
        write_bus_events_sheet(workbook.create_sheet("Bus Events"), bus_events)
        write_unit_conversions_sheet(workbook.create_sheet("Unit Conversions"))

        if destination is None:
            destination = EXCEL_EXPORT_DIR / build_test_export_filename(
                model, duration_sim_seconds, seed,
                checkpoint_sim_seconds=checkpoint_sim_seconds,
            )
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(destination)
        workbook.close()
        print(f"Timed test exported: {destination}")
        return destination
    except Exception as exc:
        print(f"Timed test export warning: {exc}")
        return None


# Standard checkpoint marks, in sim-seconds, that a timed test auto-exports
# at (in addition to its own duration). Cumulative from run start every time:
# the "10-minute checkpoint" is everything from t=0 to 10 minutes, not the
# interval since the 5-minute mark, since every source it reads (the JSONL
# logs, network_throughput) already accumulates from t=0.
CHECKPOINT_MARKS_SEC = [300, 600, 900, 1800, 3600, 7200]
# Marks still to fire for the CURRENTLY running test, ascending, populated by
# perform_full_reset and drained by fire_due_checkpoints. The test's own
# duration is deliberately excluded here -- that mark is always the last one
# and is exported and the run stopped by the existing finish_timed_test path,
# so a duration that happens to equal a standard mark (e.g. a 5-minute test)
# fires exactly once instead of twice.
pending_checkpoints_sec = []


def compute_checkpoint_marks(duration_sim_seconds):
    """Standard marks strictly before ``duration_sim_seconds``, ascending.

    The duration itself is not included: finish_timed_test exports and stops
    the run at that mark, so callers that also want it should test for it
    separately (as fire_due_checkpoints does not).
    """
    if not duration_sim_seconds:
        return []
    duration = float(duration_sim_seconds)
    return sorted(mark for mark in CHECKPOINT_MARKS_SEC if mark < duration)


def fire_due_checkpoints(master_frame_count):
    """Export one cumulative, non-stopping workbook per standard mark reached.

    Called every callback alongside the duration check; the run keeps going
    after each of these, only the final (duration) mark stops it. A summary
    row is appended for each fired mark, exactly as for the final one.
    """
    if not pending_checkpoints_sec:
        return
    config = control_panel.global_config
    if not config.get("test_running", False):
        return
    simulation_time_seconds = master_frame_count / 60.0
    while (
        pending_checkpoints_sec
        and simulation_time_seconds >= pending_checkpoints_sec[0]
    ):
        mark = pending_checkpoints_sec.pop(0)
        export_test_workbook(
            config.get("test_model", "None"),
            config.get("test_duration_sim_seconds"),
            config.get("test_seed"),
            checkpoint_sim_seconds=mark,
        )
        append_experiment_summary_row(mark)


def timed_test_is_complete(master_frame_count):
    """True once a running timed test has reached its sim-time duration.

    The comparison is on simulation_time_seconds (frames / 60), which is the
    same clock telemetry reports, so render speed cannot change run length.
    """
    config = control_panel.global_config
    if not config.get("test_running", False):
        return False
    duration = config.get("test_duration_sim_seconds")
    if not duration:
        return False
    simulation_time_seconds = master_frame_count / 60.0
    return simulation_time_seconds >= float(duration)


def finish_timed_test(master_frame_count):
    """Stop the run at its configured sim-time and export the final,
    duration-mark checkpoint workbook (cp<duration>min)."""
    config = control_panel.global_config
    config["is_running"] = False
    config["test_running"] = False
    duration = config.get("test_duration_sim_seconds")
    destination = export_test_workbook(
        config.get("test_model", "None"),
        duration,
        config.get("test_seed"),
        checkpoint_sim_seconds=duration,
    )
    config["test_last_export"] = destination.name if destination else ""
    control_panel.write_ai_control()
    append_experiment_summary_row(duration)
    return destination


# ==========================================================
# BATCH BENCHMARK RUNNER (chains request_start_test, never reimplements it)
# ==========================================================
# A run that overruns this multiple of its own expected wall-clock length
# (duration / sim_speed) -- plus a fixed buffer generous enough for LLM
# inference latency -- is presumed hung (gridlock, a stalled model call,
# repeated timeouts) and is force-stopped and recorded as a FAILED run so
# the batch can move on unattended.
BATCH_TIMEOUT_MULTIPLIER = 3.0
BATCH_TIMEOUT_FIXED_BUFFER_SEC = 120.0
# Substrings looked for (case-insensitively) in a run's agent_turn_log.jsonl
# raw_output/reason text to recognize an API provider's rate-limit/quota
# error distinctly from any other failure.
BATCH_RATE_LIMIT_MARKERS = (
    "429", "rate limit", "ratelimit", "quota", "resourceexhausted",
    "too many requests",
)

# Mutable engine state for the currently active batch, if any. Kept at
# module scope (like pending_checkpoints_sec) because it is driven from
# inside the fixed-step simulation loop, one poll per tick.
_batch_engine = {
    "runner": None,
    "phase": "IDLE",          # IDLE (about to launch) | AWAITING_START | RUN_ACTIVE
    "run_started_wall": None,
}


def _is_batch_api_model(model):
    return model in control_panel.get_api_models()


def _batch_run_hit_rate_limit(model):
    """Scan the just-finished run's own turn log (perform_full_reset clears
    it every run) for this model's rate-limit/quota signature."""
    for row in _read_jsonl_rows(AGENT_TURN_LOG_PATH):
        if str(row.get("model", "")) != model:
            continue
        haystack = f"{row.get('raw_output', '')} {row.get('reason', '')}".lower()
        if any(marker in haystack for marker in BATCH_RATE_LIMIT_MARKERS):
            return True
    return False


def build_batch_runner():
    """Wire a fresh batch_runner.BatchRunner to the real single-run path.

    Every callback is a thin lambda around a control_panel function looked
    up by attribute at call time (not bound eagerly), so tests can
    monkeypatch e.g. control_panel.request_start_test and see the batch
    engine call it -- proving the batch chains the existing single-run path
    instead of reimplementing it.
    """
    def set_seed(seed):
        control_panel.global_config["random_seed"] = seed

    def set_model(model):
        resolved = (
            "None" if model == control_panel.BATCH_BASELINE_LABEL else model
        )
        control_panel.set_active_ai_model(resolved, persist=False)
        control_panel.global_config["ai_runtime"]["armed"] = resolved != "None"
        control_panel.write_ai_control()

    def start_run():
        return control_panel.request_start_test()

    return batch_runner.BatchRunner(
        set_seed=set_seed, set_model=set_model, start_run=start_run
    )


def poll_batch_runner():
    """Drive the Batch Benchmark Runner one tick at a time from inside the
    fixed simulation loop: batch progress can only be observed through the
    same test_running/start_requested flags perform_full_reset flips, so it
    is polled here rather than on any independent timer.
    """
    config = control_panel.global_config
    runtime = config.setdefault(
        "batch_runtime", dict(control_panel.DEFAULT_BATCH_RUNTIME)
    )

    if config.get("batch_start_requested", False):
        config["batch_start_requested"] = False
        runner = build_batch_runner()
        _batch_engine["runner"] = runner
        _batch_engine["phase"] = "IDLE"
        runner.start(list(runtime.get("models", [])), list(runtime.get("seeds", [])))
        runtime["current"] = dict(runner.current) if runner.current else None

    runner = _batch_engine.get("runner")
    if runner is None or not runner.is_active():
        if runner is not None:
            runtime["active"] = False
            runtime["current"] = None
            runtime["results"] = list(runner.results)
            _batch_engine["runner"] = None
        return

    if config.get("batch_stop_requested", False):
        runner.request_stop()
        config["batch_stop_requested"] = False

    phase = _batch_engine["phase"]
    if phase == "IDLE":
        # A run was just queued (by start() or the previous outcome's
        # _advance()); wait a tick for perform_full_reset to pick it up.
        _batch_engine["run_started_wall"] = time.monotonic()
        _batch_engine["phase"] = "AWAITING_START"
    elif phase == "AWAITING_START":
        if config.get("test_running", False):
            _batch_engine["phase"] = "RUN_ACTIVE"
    elif phase == "RUN_ACTIVE":
        model = runner.current["model"]
        elapsed = time.monotonic() - _batch_engine["run_started_wall"]
        duration = config.get("test_duration_sim_seconds") or 0
        sim_speed = max(0.1, config.get("sim_speed", 1.0))
        budget = (
            (duration / sim_speed) * BATCH_TIMEOUT_MULTIPLIER
            + BATCH_TIMEOUT_FIXED_BUFFER_SEC
        )
        still_running = config.get("test_running", False)
        timed_out = still_running and elapsed > budget

        if timed_out:
            control_panel.request_start_stop()
            still_running = False

        if still_running:
            pass  # keep waiting; nothing to report yet
        else:
            is_api_model = _is_batch_api_model(model)
            rate_limited = is_api_model and _batch_run_hit_rate_limit(model)
            if timed_out:
                runner.report_run_outcome(
                    "FAILED", reason="timeout",
                    rate_limited=rate_limited, is_api_model=is_api_model,
                )
            elif rate_limited:
                runner.report_run_outcome(
                    "FAILED", reason="rate_limited",
                    rate_limited=True, is_api_model=is_api_model,
                )
            else:
                runner.report_run_outcome("COMPLETED")

            runtime["results"] = list(runner.results)
            runtime["current"] = dict(runner.current) if runner.current else None
            _batch_engine["phase"] = "IDLE"
            if not runner.is_active():
                runtime["active"] = False
                _batch_engine["runner"] = None

    if runner.current is not None:
        runtime["current"] = dict(runner.current)


def _set_ai_flags(flags):
    for route_id, route_flags in flags.items():
        route_config = control_panel.bus_routes_config[route_id]
        route_config["tsp_enabled"] = route_flags["tsp"]
        route_config["dbl_enabled"] = route_flags["dbl"]


def merge_ai_decision(path=None):
    """Validate and merge one file-based AI decision into live route config."""
    runtime = control_panel.global_config.setdefault("ai_runtime", {})
    # Only an explicitly recorded "None" selection means a baseline run. An
    # absent key leaves the generic merge path alone, so callers that drive
    # decisions without a model selector keep working.
    if "model" in runtime and str(runtime["model"]) == "None":
        # There is no decision to wait for, and a decision file left over from
        # an earlier model must not leak into the unaided run.
        runtime["last_status"] = control_panel.BASELINE_NO_MODEL
        return False
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
    """Clear vehicle and bus source state for a fresh traffic episode."""
    global post_discharge_meter_frames_remaining, bus_sequence_counter
    for state in spawner_states.values():
        state.clear()
        state.update(_new_spawner_state())
    for route_id in bus_dispatch_counters:
        bus_dispatch_counters[route_id] = 0.0
    bus_sequence_counter = 0
    post_discharge_meter_frames_remaining = 0


def reset_traffic_generation():
    """Reset every traffic source and restart its configured RNG sequence."""
    reset_all_spawner_states()
    # Freeze motion tuning for the episode. Slider changes made during a run
    # therefore cannot change the speed of only later arrivals.
    apply_configured_vehicle_speed_scale()
    return apply_configured_random_seed()


# --- Webster saturation-flow calibration ------------------------------------
# Capacity here depends on the configured speed scale and vehicle mix, so the
# saturation flow S that Webster needs is measured fresh at every run start
# rather than hardcoded. The method is the HCM queue discharge: build a
# standing queue behind red, release it with no new arrivals, discard the
# startup vehicles and take the mean headway of the rest.
CALIBRATION_APPROACH = "EB"
CALIBRATION_SPAWN_X = -900          # Upstream of the canvas: real queue storage
CALIBRATION_TARGET_QUEUE = 30       # Measured: 12 gives sd +/-920 veh/hr
                                    # (+/-27%), 30 lands within ~3% of a
                                    # careful 5-seed reference for ~1-2s
CALIBRATION_BUILD_LIMIT_SEC = 180.0
CALIBRATION_DISCHARGE_LIMIT_SEC = 120.0
CALIBRATION_STARTUP_VEHICLES = 4    # Startup lost time, excluded per HCM
CALIBRATION_BUILD_VEH_PER_MIN = 120
CALIBRATION_SEED_FALLBACK = 20260909
DEFAULT_SATURATION_FLOW = 1366.0    # Only used if a calibration run yields none


def _calibration_signals(green):
    """Force the measured approach red or green, with node B always green."""
    measured = "GREEN" if green else "RED"
    return {
        canvas.INT_X[0]: {
            "EB": measured, "WB": "RED", "NB": "RED", "SB": "RED",
        },
        canvas.INT_X[1]: {
            "EB": "GREEN", "WB": "RED", "NB": "RED", "SB": "RED",
        },
    }


def calibrate_saturation_flow(
    speed_scale, heavy_ratio, seed=None, target_queue=CALIBRATION_TARGET_QUEUE
):
    """Measure saturation flow (veh/hr/lane) by discharging a standing queue.

    Deterministic for a given seed. Runs headless and leaves no vehicles
    behind; the caller re-seeds traffic generation afterwards so the measured
    run itself starts from a clean RNG.
    """
    from src.core.signal_controller import SignalController

    approach_cfg = dict(control_panel.approach_configs[CALIBRATION_APPROACH])
    approach_cfg.update(
        {
            "active": True,
            "model": "Poisson",
            "rate": CALIBRATION_BUILD_VEH_PER_MIN,
            "turn_split": 1.0,          # all straight: a single-lane measure
            "heavy_ratio": heavy_ratio,
        }
    )
    lane_y = canvas.H_Y - 1.5 * canvas.LANE
    lane_coords = [lane_y, lane_y, lane_y]

    previous_scale = control_panel.global_config.get(
        "_active_vehicle_speed_scale", speed_scale
    )
    control_panel.global_config["_active_vehicle_speed_scale"] = speed_scale
    random.seed(CALIBRATION_SEED_FALLBACK if seed is None else seed)
    reset_all_spawner_states()

    controller = SignalController(
        {"green_time": 240, "is_running": True},
        yellow_time=60,
        red_clearance_time=60,
    )
    vehicles = []
    upstream = {}

    def queued():
        return [
            vehicle
            for vehicle in vehicles
            if vehicle.is_front_bumper_upstream(
                canvas.INT_X[0], canvas.H_Y, canvas.ROAD_W, canvas.STOP
            )
        ]

    def step(signals, spawning):
        if spawning:
            try_spawn_vehicle(
                vehicles, CALIBRATION_APPROACH, CALIBRATION_APPROACH,
                CALIBRATION_SPAWN_X, lane_coords, approach_cfg, min_gap=40,
            )
        crossings = 0
        for vehicle in list(vehicles):
            key = id(vehicle)
            if key not in upstream:
                upstream[key] = vehicle.is_front_bumper_upstream(
                    canvas.INT_X[0], canvas.H_Y, canvas.ROAD_W, canvas.STOP
                )
            vehicle.update(
                signals, canvas.INT_X, canvas.H_Y,
                road_w=canvas.ROAD_W, stop_offset=canvas.STOP,
                lane_w=canvas.LANE, all_vehicles=vehicles,
                signal_controller=controller,
            )
            after = vehicle.is_front_bumper_upstream(
                canvas.INT_X[0], canvas.H_Y, canvas.ROAD_W, canvas.STOP
            )
            if upstream[key] and not after:
                crossings += 1
            upstream[key] = after
        controller.update(vehicles)
        vehicles[:] = [v for v in vehicles if v.x <= canvas.WIDTH + 150]
        return crossings

    red = _calibration_signals(False)
    for _ in range(int(CALIBRATION_BUILD_LIMIT_SEC * 60)):
        step(red, spawning=True)
        if len(queued()) >= target_queue:
            break

    green = _calibration_signals(True)
    crossing_frames = []
    for frame in range(int(CALIBRATION_DISCHARGE_LIMIT_SEC * 60)):
        for _ in range(step(green, spawning=False)):
            crossing_frames.append(frame)
        if not queued():
            break

    control_panel.global_config["_active_vehicle_speed_scale"] = previous_scale

    headways = [
        (later - earlier) / 60.0
        for earlier, later in zip(crossing_frames, crossing_frames[1:])
    ][CALIBRATION_STARTUP_VEHICLES:]
    if not headways:
        return DEFAULT_SATURATION_FLOW
    mean_headway = sum(headways) / len(headways)
    if mean_headway <= 0:
        return DEFAULT_SATURATION_FLOW
    return 3600.0 / mean_headway


def representative_heavy_ratio():
    """Mean heavy-vehicle share across the configured approaches."""
    ratios = [
        float(cfg.get("heavy_ratio", 0.1))
        for cfg in control_panel.approach_configs.values()
    ]
    return sum(ratios) / len(ratios) if ratios else 0.1


def calibrate_and_apply_webster(signals):
    """Measure S and publish Webster splits before the run clock starts."""
    config = control_panel.global_config
    config["calibrating"] = True
    try:
        saturation = calibrate_saturation_flow(
            config.get("vehicle_speed_scale", 0.5),
            representative_heavy_ratio(),
            config.get("random_seed"),
        )
        config["measured_saturation_flow"] = round(saturation, 0)
        # Tolerate controllers that do not expose the interlock timings,
        # falling back to the production defaults rather than failing a reset.
        lost_time = webster.lost_time_seconds(
            getattr(signals, "yellow_time", 60),
            getattr(signals, "red_clearance_time", 60),
        )
        splits = webster.compute_all_nodes(
            control_panel.approach_configs,
            saturation,
            lost_time_sec=lost_time,
        )
        config["webster_splits"] = splits
        config["cycle_time_sec"] = {
            node_x: split["cycle_time_sec"] for node_x, split in splits.items()
        }
        cycle_summary = ", ".join(
            f"node {node_x}={split['cycle_time_sec']:.1f}s"
            for node_x, split in splits.items()
        )
        print(
            f"[WEBSTER] S={saturation:.0f} veh/hr  "
            f"cycles=({cycle_summary})  "
            f"lost={lost_time:.1f}s  splits={splits}"
        )
        return saturation, splits
    finally:
        config["calibrating"] = False


def perform_full_reset(vehicles, signals, telemetry=None):
    """Restore all per-run simulation state and return the frame-zero value."""
    vehicles.clear()
    signals.reset_all_state()
    if telemetry is not None:
        telemetry.reset_session()
    reset_session_logs()
    network_throughput.update({key: 0 for key in network_throughput})
    # A fresh run gets a fresh checkpoint schedule -- only when this reset is
    # starting a timed test; a plain START or a mid-test manual RESET both
    # restart the sim clock at frame zero, so the marks must restart too.
    pending_checkpoints_sec.clear()
    if control_panel.global_config.get("test_running", False):
        pending_checkpoints_sec.extend(
            compute_checkpoint_marks(
                control_panel.global_config.get("test_duration_sim_seconds")
            )
        )
    # Calibration consumes its own seeded RNG, so traffic generation is
    # reset afterwards and the measured run still starts from the
    # configured seed.
    calibrate_and_apply_webster(signals)
    reset_traffic_generation()
    # Frame zero is returned only after S is locked: calibration is
    # setup, not part of the measured run.
    return 0


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

    base_speed = (
        random.uniform(0.8, 1.1)
        if is_heavy
        else random.uniform(1.0, 1.4)
    )
    speed = base_speed * get_active_vehicle_speed_scale()
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
                route_info=route_info, bus_id=unique_bus_id,
                max_speed=1.0 * get_active_vehicle_speed_scale(),
            ))


# --- Unified single-window shell --------------------------------------------
# One process now owns exactly one tk.Tk(): the control panel and telemetry
# dashboard, previously each a standalone window (the latter in its own OS
# process), mount into panes of this same root instead of creating their own.
MAIN_WINDOW_TITLE = "Traffic Simulator"
# Portrait side panes, each a fraction of the screen width bounded so it is
# never crushed on a small screen or absurdly wide on a huge one. The two
# are sized separately: the control panel is a single column of stacked
# controls and needs only enough width for one slider or one button row,
# while the telemetry dashboard carries a 3 x 3 KPI grid, the phase-cycle
# strip and two node diagrams side by side, so it keeps ~30% more.
CONTROL_PANE_WIDTH_FRACTION = 0.10
MIN_CONTROL_PANE_WIDTH = 256
MAX_CONTROL_PANE_WIDTH = 320
TELEMETRY_PANE_WIDTH_FRACTION = 0.13
MIN_TELEMETRY_PANE_WIDTH = 332
MAX_TELEMETRY_PANE_WIDTH = 420
# Breathing room between the simulation canvas's hairline frame and the
# sashes either side of it. The window is sized so the center pane is exactly
# the canvas plus this gutter -- any wider and the fixed-size network just
# floats in empty background.
SIMULATION_PANE_GUTTER = control_panel.SPACE_MD


def control_pane_width_for(screen_width):
    """Control-panel column width for this screen, bounded by the constants above."""
    return max(
        MIN_CONTROL_PANE_WIDTH,
        min(MAX_CONTROL_PANE_WIDTH, round(screen_width * CONTROL_PANE_WIDTH_FRACTION)),
    )


def telemetry_pane_width_for(screen_width):
    """Telemetry-dashboard column width for this screen, bounded likewise."""
    return max(
        MIN_TELEMETRY_PANE_WIDTH,
        min(MAX_TELEMETRY_PANE_WIDTH, round(screen_width * TELEMETRY_PANE_WIDTH_FRACTION)),
    )


def main_window_width_for(screen_width):
    """Window width that fits both side panes and the canvas with no slack.

    Bounded by the screen: on a display too narrow for all three, the
    center pane simply gets less and clips the canvas, as before.
    """
    fitted = (
        control_pane_width_for(screen_width)
        + telemetry_pane_width_for(screen_width)
        + canvas.WIDTH + 2 * SIMULATION_PANE_GUTTER
    )
    return max(900, min(screen_width - 60, fitted))


def main_window_height_for(screen_height):
    """Window height that frames the canvas with the same gutter on every side.

    The canvas is the one fixed-size element; the two side columns scroll,
    and the telemetry dashboard lays itself out to the viewport it gets, so
    nothing else needs the window taller than the network plus its gutter.
    """
    return min(screen_height - 100, canvas.HEIGHT + 2 * SIMULATION_PANE_GUTTER)


# --- Window shapes -----------------------------------------------------------
# Three sizes the operator cycles through from the control-panel header (the
# OS maximize/restore buttons land on the same three). "compact" is the
# fitted window above with the canvas at native 1:1; "large" is a centred
# window of ~75% of the screen; "maximized" is the OS zoomed state. In the
# two bigger shapes the side panes widen to their maxima and the canvas is
# scaled up (5:3 kept) to fill the centre pane minus the same gutter.
WINDOW_SHAPES = ("compact", "large", "maximized")
LARGE_WINDOW_SCREEN_FRACTION = 0.75
# A window this close to the compact size (after the OS restores it, say)
# still counts as compact.
COMPACT_SHAPE_TOLERANCE_PX = 8
# Below the taskbar and title bar the OS keeps for itself.
LARGE_WINDOW_SCREEN_MARGIN_Y = 80
# Scaled canvas pushes beyond this many native pixel counts are halved to
# ~15Hz (see build_simulation_canvas). Maximized on a 2560-wide screen is
# ~3.2x and stays at the full push rate.
SCALED_PUSH_SKIP_FACTOR = 4


def side_pane_widths_for(shape, screen_width):
    """(control, telemetry) pane widths for a shape on this screen."""
    if shape == "compact":
        return control_pane_width_for(screen_width), telemetry_pane_width_for(screen_width)
    return MAX_CONTROL_PANE_WIDTH, MAX_TELEMETRY_PANE_WIDTH


def large_window_geometry_for(screen_width, screen_height):
    """(width, height, x, y) of the Large shape: ~75% of the screen, centred.

    Never narrower than the widened side panes plus the native canvas, so
    on a screen where 75% is less than that (1080p) Large is at least as
    wide as compact and only gains height.
    """
    min_width = (
        MAX_CONTROL_PANE_WIDTH + MAX_TELEMETRY_PANE_WIDTH
        + canvas.WIDTH + 2 * SIMULATION_PANE_GUTTER
    )
    width = min(
        screen_width - 60,
        max(round(screen_width * LARGE_WINDOW_SCREEN_FRACTION), min_width),
    )
    usable_height = screen_height - LARGE_WINDOW_SCREEN_MARGIN_Y
    height = min(
        usable_height,
        max(
            round(screen_height * LARGE_WINDOW_SCREEN_FRACTION),
            canvas.HEIGHT + 2 * SIMULATION_PANE_GUTTER,
        ),
    )
    x = max(0, (screen_width - width) // 2)
    y = max(0, (usable_height - height) // 2)
    return width, height, x, y


def fit_canvas_size(available_width, available_height):
    """Largest 5:3 integer canvas size that fits the available area.

    The width is a multiple of 5 so the 5:3 height is an exact integer, and
    never below a quarter of native so a transiently tiny pane (mid-resize)
    cannot request a degenerate image.
    """
    floor_width = canvas.WIDTH // 4 - (canvas.WIDTH // 4) % 5
    scale = min(
        max(0.0, float(available_width)) / canvas.WIDTH,
        max(0.0, float(available_height)) / canvas.HEIGHT,
    )
    width = int(canvas.WIDTH * scale)
    width -= width % 5
    width = max(floor_width, width)
    return width, width * 3 // 5


def classify_window_shape(state, width, height, compact_size):
    """Which shape a live window is in, from its OS state and size.

    Used after any resize so an OS maximize/restore (or a drag) is reflected
    in the shape state and the header button, not only the panel's own
    cycle button.
    """
    if state == "zoomed":
        return "maximized"
    compact_width, compact_height = compact_size
    if (
        abs(int(width) - compact_width) <= COMPACT_SHAPE_TOLERANCE_PX
        and abs(int(height) - compact_height) <= COMPACT_SHAPE_TOLERANCE_PX
    ):
        return "compact"
    return "large"


class WindowShapeController:
    """Owns the window's shape: size, side-pane widths and canvas scale.

    ``apply``/``cycle`` change the OS window; everything that depends on the
    resulting size (sash positions, canvas target size) happens later in
    ``_sync``, which the root's ``<Configure>`` event schedules once the
    window has actually been resized -- calling ``sashpos`` before that
    would act on the old geometry. ``_sync`` also runs for resizes the
    operator makes with the OS buttons or by dragging, so the canvas always
    fits whatever the centre pane currently is.
    """

    SYNC_DEBOUNCE_MS = 60

    def __init__(
        self, root, paned, control_outer, telemetry_outer, simulation_pane,
        screen_width, screen_height,
    ):
        self.root = root
        self.paned = paned
        self.control_outer = control_outer
        self.telemetry_outer = telemetry_outer
        self.simulation_pane = simulation_pane
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.shape = "compact"
        self._compact_size = (
            main_window_width_for(screen_width), main_window_height_for(screen_height)
        )
        self._pending_pane_widths = None
        self._fit_canvas = None
        self._after_id = None
        root.bind("<Configure>", self._on_root_configure, add="+")
        simulation_pane.bind("<Configure>", self._on_pane_configure, add="+")

    # --- operator-facing -------------------------------------------------
    def cycle(self):
        index = WINDOW_SHAPES.index(self.shape)
        self.apply(WINDOW_SHAPES[(index + 1) % len(WINDOW_SHAPES)])

    def apply(self, shape):
        if shape not in WINDOW_SHAPES:
            raise ValueError(f"unknown window shape {shape!r}")
        self.shape = shape
        control_panel.global_config["window_shape"] = shape
        self._pending_pane_widths = side_pane_widths_for(shape, self.screen_width)
        if shape == "maximized":
            self.root.state("zoomed")
            return
        if self.root.state() == "zoomed":
            self.root.state("normal")
        if shape == "compact":
            width, height = self._compact_size
            x = max(0, (self.screen_width - width) // 2)
            y = max(0, (self.screen_height - LARGE_WINDOW_SCREEN_MARGIN_Y - height) // 2)
        else:
            width, height, x, y = large_window_geometry_for(
                self.screen_width, self.screen_height
            )
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def attach_canvas(self, set_target_size):
        """Register the canvas resizer (build_simulation_canvas's) and fit it."""
        self._fit_canvas = set_target_size
        self._schedule_sync()

    # --- resize plumbing ---------------------------------------------------
    def _on_root_configure(self, event):
        # A root binding receives every descendant's Configure too.
        if event.widget is not self.root:
            return
        self._schedule_sync()

    def _on_pane_configure(self, _event):
        self._schedule_sync()

    def _schedule_sync(self):
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        self._after_id = self.root.after(self.SYNC_DEBOUNCE_MS, self._sync)

    def _sync(self):
        self._after_id = None
        state = self.root.state()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if state == "iconic" or width < 100:
            return
        detected = classify_window_shape(state, width, height, self._compact_size)
        if detected != self.shape:
            # The OS (or a drag) changed the window under us. Pane widths
            # follow a maximize/restore; a free drag keeps whatever sashes
            # the operator has, it only refits the canvas.
            if detected == "maximized" or self.shape == "maximized":
                self._pending_pane_widths = side_pane_widths_for(
                    detected, self.screen_width
                )
            self.shape = detected
            control_panel.global_config["window_shape"] = detected
        if self._pending_pane_widths is not None:
            pending, self._pending_pane_widths = self._pending_pane_widths, None
            self._apply_pane_widths(*pending)
        self._fit()

    def _apply_pane_widths(self, control_width, telemetry_width):
        """Move both sashes so the side panes take exactly these widths.

        ttk sashes keep their absolute positions across window resizes, so
        restoring from maximized would otherwise leave the widened panes in
        place and squeeze the canvas.
        """
        self.control_outer.configure(width=control_width)
        self.telemetry_outer.configure(width=telemetry_width)
        self.root.update_idletasks()
        paned_width = self.paned.winfo_width()
        if paned_width <= 1:
            return
        # Sash thickness as Tk actually draws it, not a guess.
        sash_width = max(
            0, paned_width - self.paned.sashpos(1) - self.telemetry_outer.winfo_width()
        )
        self.paned.sashpos(0, control_width)
        self.paned.sashpos(1, paned_width - telemetry_width - sash_width)

    def _fit(self):
        if self._fit_canvas is None:
            return
        if self.shape == "compact":
            self._fit_canvas(canvas.WIDTH, canvas.HEIGHT)
            return
        # Hairline frame (1px each side) plus the gutter all round.
        available_width = self.simulation_pane.winfo_width() - 2 * SIMULATION_PANE_GUTTER - 2
        available_height = self.simulation_pane.winfo_height() - 2 * SIMULATION_PANE_GUTTER - 2
        self._fit_canvas(*fit_canvas_size(available_width, available_height))


def build_scrollable_pane(parent, width):
    """A fixed-width, vertically scrollable container for one side pane.

    Mirrors the Canvas + Scrollbar + inner-frame pattern
    TelemetryDashboard.create_scrollable_tab already uses for its own tabs,
    generalized here to host an entire mounted component (the control panel
    or the telemetry dashboard) instead of one dashboard tab.

    Returns (outer_frame, content_frame, scroll_canvas). Pack widgets into
    `content_frame`; `outer_frame` is what the caller adds to the
    PanedWindow.
    """
    outer = tk.Frame(parent, width=width)
    outer.pack_propagate(False)

    scroll_canvas = tk.Canvas(
        outer, width=width, highlightthickness=0, bg=control_panel.COLOR_BG,
    )
    scrollbar = ttk.Scrollbar(
        outer, orient="vertical", command=scroll_canvas.yview
    )
    scroll_canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    scroll_canvas.pack(side="left", fill="both", expand=True)

    content = tk.Frame(scroll_canvas, bg=control_panel.COLOR_BG)
    content_window = scroll_canvas.create_window(
        (0, 0), window=content, anchor="nw"
    )

    # A 1px-wide spacer whose height follows the viewport gives the content
    # frame a natural height of at least the visible pane, so a mounted
    # component that packs with expand=True (the telemetry notebook) fills
    # the pane top to bottom. Natural sizing is untouched: taller content
    # still fires <Configure> and scrolls.
    min_height_spacer = tk.Frame(content, width=1, height=1, bg=control_panel.COLOR_BG)
    min_height_spacer.pack(side="right", fill="y")

    def refresh_scroll_region(_event=None):
        scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all"))

    def fit_content_width(event):
        scroll_canvas.itemconfigure(content_window, width=max(1, event.width))
        min_height_spacer.configure(height=max(1, event.height))

    content.bind("<Configure>", refresh_scroll_region, add="+")
    scroll_canvas.bind("<Configure>", fit_content_width, add="+")

    return outer, content, scroll_canvas


def bind_pane_mousewheel(widget, canvas_widget):
    """Recursively bind wheel scrolling to a pane and every widget inside it.

    Tk delivers <MouseWheel> to whichever widget is directly under the
    pointer, not to an ancestor, so binding only the outer scroll canvas
    would silently miss every scroll gesture made over a button, label, or
    slider sitting on top of it. This walks the already-built widget tree and
    binds each one directly, the same recursive approach
    TelemetryDashboard.bind_tab_mousewheel uses for its own tabs -- as
    opposed to a global bind_all, which would make two independently
    scrolling panes fight over one shared binding.
    """
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        widget.bind(
            sequence,
            lambda event, c=canvas_widget: TelemetryDashboard.scroll_tab_with_wheel(
                event, c
            ),
            add="+",
        )
    for child in widget.winfo_children():
        bind_pane_mousewheel(child, canvas_widget)


def build_main_window():
    """Construct the single Tk root and its three-pane layout.

    Left = control panel, center = simulation canvas, right = telemetry.
    The center pane claims any extra resize space (weight=1); the side panes
    hold their configured width (weight=0) and scroll vertically for
    whatever overflows it. This commit builds only the empty shell -- the
    control panel and telemetry dashboard mount into their panes in later
    checkpoints, and the simulation canvas gets its pygame-fed image later
    still.
    """
    root = tk.Tk()
    root.title(MAIN_WINDOW_TITLE)
    root.configure(bg=control_panel.COLOR_BG)

    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    # Both dimensions are fitted to the canvas (plus the side panes across,
    # plus one gutter all round) rather than to the screen, so the fixed-size
    # network is framed by the same margin on every side instead of floating
    # in empty background on a large monitor.
    window_width = main_window_width_for(screen_width)
    window_height = main_window_height_for(screen_height)
    root.geometry(f"{window_width}x{window_height}")

    paned = ttk.PanedWindow(root, orient="horizontal")
    paned.pack(fill="both", expand=True)

    control_outer, control_pane, control_scroll = build_scrollable_pane(
        paned, control_pane_width_for(screen_width)
    )
    simulation_pane = tk.Frame(paned, bg=control_panel.COLOR_BG)
    telemetry_outer, telemetry_pane, telemetry_scroll = build_scrollable_pane(
        paned, telemetry_pane_width_for(screen_width)
    )
    # Stashed on the pane itself rather than widening this function's return
    # signature: whoever later mounts real content into control_pane or
    # telemetry_pane needs this canvas to wire up bind_pane_mousewheel, but
    # nothing in this checkpoint's empty-shell commit does yet.
    control_pane.scroll_canvas = control_scroll
    telemetry_pane.scroll_canvas = telemetry_scroll

    # Each side pane's own requested width (frozen at its *_pane_width_for by
    # pack_propagate(False) above) is what the PanedWindow sizes it to
    # automatically once mapped; weight=1 on the center pane then gives it
    # everything left over. No sashpos() call is made here -- one made before
    # the window is first mapped collapses the side panes to near zero width.
    # WindowShapeController moves the sashes only from its post-map,
    # <Configure>-driven sync when a shape change needs different widths.
    paned.add(control_outer, weight=0)
    paned.add(simulation_pane, weight=1)
    paned.add(telemetry_outer, weight=0)

    # Extra handles ride on the root so the 4-tuple return (which tests and
    # main() depend on) stays as it is.
    root.paned = paned
    root.window_shape = WindowShapeController(
        root, paned, control_outer, telemetry_outer, simulation_pane,
        screen_width, screen_height,
    )

    return root, control_pane, simulation_pane, telemetry_pane


def build_simulation_canvas(parent):
    """A Tk Canvas fed by one PhotoImage, mutated every push.

    The offscreen surface blit method (task-specified): one PhotoImage is
    created once and itemconfig'd back onto the same canvas image item on
    every push -- never recreated -- so Tk redraws in place without ever
    losing the reference mid-frame. Created at the network's native
    canvas.WIDTH x canvas.HEIGHT; ``canvas_widget.set_target_size(w, h)``
    (used by WindowShapeController) resizes the canvas and PhotoImage in
    place and makes every later push scale the 1000x600 surface to that
    size with pygame's own smoothscale -- the physics surface and geometry
    constants never change, only the pixels handed to Tk.

    Returns (canvas_widget, push_frame), where push_frame(surface) converts
    one pygame Surface to PPM bytes and writes it into the same PhotoImage.
    """
    # The pane shares the side panes' background; the canvas sits in a
    # hairline frame so the network reads as one framed view rather than a
    # black rectangle floating on black.
    frame = tk.Frame(
        parent, bg=control_panel.COLOR_BG,
        highlightthickness=1, highlightbackground=control_panel.COLOR_CARD_BORDER,
    )
    # expand=True without fill: the fixed-size canvas sits centered in the
    # pane. The window is sized so that centering leaves exactly one gutter
    # on every side (build_main_window); if the operator drags the window
    # larger the margins simply grow equally.
    frame.pack(expand=True)
    simulation_canvas = tk.Canvas(
        frame, width=canvas.WIDTH, height=canvas.HEIGHT,
        bg="black", highlightthickness=0,
    )
    simulation_canvas.pack()
    photo = tk.PhotoImage(width=canvas.WIDTH, height=canvas.HEIGHT)
    image_item = simulation_canvas.create_image(0, 0, anchor="nw", image=photo)
    native_size = (canvas.WIDTH, canvas.HEIGHT)

    # Per-target state, rebuilt only when the target size changes: PPM header,
    # a reused destination surface for smoothscale (None at native size), and
    # how many pushes to skip. Pushing a scaled frame costs roughly its pixel
    # count (~14ms at 4K vs ~3ms native), so past SCALED_PUSH_SKIP_FACTOR x
    # native pixels only every other push is written, keeping the 60Hz
    # physics loop and the Tk thread responsive.
    state = {
        "target": native_size,
        "header": f"P6 {canvas.WIDTH} {canvas.HEIGHT} 255 ".encode("ascii"),
        "scaled": None,
        "push_every": 1,
        "pushes": 0,
    }

    def set_target_size(width, height):
        target = (int(width), int(height))
        if target == state["target"]:
            return
        state["target"] = target
        simulation_canvas.configure(width=target[0], height=target[1])
        # A PhotoImage keeps its creation size and silently clips larger
        # data until it is resized explicitly.
        photo.configure(width=target[0], height=target[1])
        state["header"] = f"P6 {target[0]} {target[1]} 255 ".encode("ascii")
        state["scaled"] = None if target == native_size else pygame.Surface(target)
        native_pixels = canvas.WIDTH * canvas.HEIGHT
        state["push_every"] = (
            2 if target[0] * target[1] > SCALED_PUSH_SKIP_FACTOR * native_pixels else 1
        )

    def push_frame(surface):
        state["pushes"] += 1
        if state["pushes"] % state["push_every"]:
            return
        scaled = state["scaled"]
        if scaled is not None:
            surface = pygame.transform.smoothscale(surface, state["target"], scaled)
        photo.configure(
            data=state["header"] + pygame.image.tostring(surface, "RGB"),
            format="PPM",
        )
        simulation_canvas.itemconfig(image_item, image=photo)
        # Tk repaints from its idle queue, which only drains when no timer is
        # due. A scaled push plus a physics step can outlast the 16ms step
        # interval, and then nothing on screen (canvas or side panes) ever
        # repaints. Flushing here makes every pushed frame -- and any pending
        # widget redraw -- actually reach the screen.
        simulation_canvas.update_idletasks()

    simulation_canvas.set_target_size = set_target_size
    return simulation_canvas, push_frame


def main():
    # Seeding makes traffic generation reproducible, not LLM inference. The
    # valid benchmark is the same seed with one ARMED and one DISARMED run;
    # same-seed ARMED runs may diverge because model decisions can differ.
    pygame.init()
    pygame.font.init()
    font = pygame.font.SysFont("Consolas", 13, bold=True)

    # No pygame window is created: the network renders onto an offscreen
    # Surface (canvas_gemini.draw_network and Vehicle.draw are plain pygame
    # drawing calls and work identically on one), which is pushed into a Tk
    # Canvas living in the simulation pane instead of being flipped to a
    # native display. Lane/intersection geometry (canvas_gemini.WIDTH/HEIGHT/
    # LANE/INT_X/H_Y) is untouched -- only where the pixels end up changes.
    screen = pygame.Surface((canvas.WIDTH, canvas.HEIGHT))

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

    # 1. Build the unified window shell, then mount the control panel into
    # its left pane. write_ai_control() behavior is byte-identical to the
    # standalone-window case; the agent subprocess depends on ai_control.json
    # and nothing about what gets written to it changes here.
    root, control_pane, simulation_pane, telemetry_pane = build_main_window()
    control_panel.create_dashboard_window(control_pane)
    bind_pane_mousewheel(control_pane, control_pane.scroll_canvas)

    # 2. Mount the telemetry dashboard into the right pane, in-process. It
    # keeps reading traffic_state_telemetry.json / agent_turn_log.jsonl /
    # ai_control.json from disk exactly as it did as a separate subprocess;
    # only where its widgets live has changed.
    TelemetryDashboard(telemetry_pane)
    bind_pane_mousewheel(telemetry_pane, telemetry_pane.scroll_canvas)

    # 2b. The simulation canvas: a Tk Canvas holding one PhotoImage that
    # every render mutates in place (itemconfig'd once, never recreated or
    # replaced) so Tk redraws it without ever losing the reference mid-
    # frame. Native 1000x600 in the compact shape; the shape controller
    # resizes it (and the pushed, smoothscaled pixels) in the larger shapes.
    _simulation_canvas, push_simulation_frame = build_simulation_canvas(
        simulation_pane
    )
    # getattr-guarded: tests drive main() with stand-in root/canvas objects
    # that have neither the controller nor the resizer.
    shape_controller = getattr(root, "window_shape", None)
    set_canvas_target_size = getattr(_simulation_canvas, "set_target_size", None)
    if shape_controller is not None and set_canvas_target_size is not None:
        shape_controller.attach_canvas(set_canvas_target_size)
        control_panel.window_shape_hooks["cycle"] = shape_controller.cycle

    print("Launching LLM Control Agent...")
    agent_proc = subprocess.Popen(
        [sys.executable, "-m", "src.agents.agent"],
        cwd=str(BASE_DIR),
    )

    # 3. Register cleanup, then build the workbook after agent writes stop.
    def cleanup():
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

    # One WM_DELETE_WINDOW handler for the one real window in this process
    # (each of control_panel's and TelemetryDashboard's own close handling is
    # skipped when mounted, per commits 3 and 4). sys.exit() runs the atexit-
    # registered cleanup() above exactly once -- agent_proc termination and
    # the combined-export workbook build -- the same way the old pygame QUIT
    # handler already relied on it.
    def on_main_window_close():
        control_panel.global_config["is_running"] = False
        try:
            root.destroy()
        except Exception:
            pass
        sys.exit()

    root.protocol("WM_DELETE_WINDOW", on_main_window_close)

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
    # Pushing a rendered frame into the Tk PhotoImage costs several ms (a PPM
    # round-trip over the full canvas), far more than the cheap pygame draw
    # calls that fill the offscreen surface. Halving how often that push
    # happens keeps the visual rate at 30 Hz while the fixed-timestep sim
    # loop above keeps stepping at a full 60 Hz -- unaffected either way,
    # since it measures real elapsed wall time and catches up independently
    # of how often a frame is actually shown.
    visual_frame_counter = 0

    def simulation_step():
        nonlocal master_frame_count, time_accumulator, last_wall_time
        nonlocal discharge_was_active, visual_frame_counter

        now = time.monotonic()
        elapsed = min(max(0.0, now - last_wall_time), max_catchup_seconds)
        last_wall_time = now
        run_just_reset = False

        if control_panel.global_config.get("start_requested", False):
            master_frame_count = perform_full_reset(vehicles, signals, telemetry)
            time_accumulator = 0.0
            discharge_was_active = False
            control_panel.global_config["is_paused"] = False
            control_panel.global_config["reset_triggered"] = False
            control_panel.global_config["is_running"] = True
            control_panel.global_config["run_has_started"] = True
            control_panel.global_config["test_last_export"] = ""
            control_panel.global_config["start_requested"] = False
            control_panel.write_ai_control()
            run_just_reset = True
        elif control_panel.global_config["reset_triggered"]:
            master_frame_count = perform_full_reset(vehicles, signals, telemetry)
            time_accumulator = 0.0
            discharge_was_active = False
            # Export before RESET to preserve the previous run: RESET clears
            # both session logs so the next benchmark run is isolated.
            control_panel.global_config["reset_triggered"] = False
            run_just_reset = True

        sim_speed = control_panel.global_config.get("sim_speed", 1.0)
        is_running = control_panel.global_config.get("is_running", False)
        is_paused = control_panel.global_config.get("is_paused", False)

        if is_running and not is_paused and not run_just_reset:
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
                                bus_event_tracker.complete(v, master_frame_count, signals)
                            else:
                                network_throughput["passengers_served_car"] += passengers
                                network_throughput["cars_served"] += 1
                        vehicles.remove(v)
                # Instrumentation only: samples post-update bus state and the
                # controller's live priority requests for this frame.
                bus_event_tracker.observe(vehicles, master_frame_count, signals)
                accumulate_frame_metrics(vehicles)
                
                time_accumulator -= dt_step
                steps_this_callback += 1

            # If the desktop held the callback during a window move, discard
            # excessive wall-time debt instead of freezing the UI to replay it.
            if time_accumulator >= dt_step:
                time_accumulator = min(time_accumulator, dt_step)
        else:
            # Paused wall time must never become a catch-up burst on resume.
            time_accumulator = 0.0

        # Calibration is setup, not part of the measured run: while it is
        # in progress the frame counter, the published sim clock and the
        # timed-test countdown all stay frozen at zero.
        if control_panel.global_config.get("calibrating", False):
            root.after(16, simulation_step)
            return

        # Publish the simulation clock so the control panel can count a timed
        # test down in sim-time, matching the clock the auto-stop uses.
        control_panel.global_config["sim_time_seconds"] = round(
            master_frame_count / 60.0, 1
        )

        # Intermediate checkpoints (5/10/15/30/60/120 sim-minutes, whichever
        # are below this test's duration) auto-export without stopping the
        # run, on the same sim clock the final auto-stop below uses.
        fire_due_checkpoints(master_frame_count)

        # A timed benchmark ends on the SIMULATION clock, never the wall clock,
        # so the same duration means the same amount of simulated traffic on
        # any machine regardless of render speed.
        if timed_test_is_complete(master_frame_count):
            finish_timed_test(master_frame_count)
            is_running = False

        # Advance the Batch Benchmark Runner, if one is active: it can only
        # observe run completion through the flags perform_full_reset and
        # finish_timed_test just flipped above, so it is polled here.
        poll_batch_runner()

        active_signal_data = signals.get_all_signals(canvas.INT_X)
        active_dbl_data = signals.get_all_dbl_states(canvas.INT_X, vehicles)

        canvas.draw_network(
            screen, 
            signal_data=active_signal_data, 
            dbl_states=active_dbl_data, 
            font=font,
            is_paused=is_paused or not is_running
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

        # Every frame is drawn onto the offscreen surface above (cheap), but
        # the far more expensive PPM push into the visible Tk canvas only
        # happens every other tick: 30 Hz visual against the 60 Hz sim.
        visual_frame_counter += 1
        if visual_frame_counter % 2 == 0:
            push_simulation_frame(screen)

        # Constant GUI polling rate (~60 FPS) decoupled from simulation speed
        root.after(16, simulation_step)

    root.after(16, simulation_step)
    root.mainloop()

if __name__ == "__main__":
    main()
