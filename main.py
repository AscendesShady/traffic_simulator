# main.py
import pygame
import sys
import random
import math
import json
import os
import subprocess
import atexit
import time
from pathlib import Path
import canvas_gemini as canvas
import control_panel
import guard
import webster
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


def build_test_export_filename(
    model, duration_sim_seconds, seed, timestamp=None
):
    """Self-documenting workbook name: model, sim-duration, seed, wall clock."""
    model_name = str(model or "None")
    model_tag = (
        model_name.replace(":", "-").replace("/", "-")
        if model_name != "None"
        else "baseline"
    )
    duration_tag = f"{int((duration_sim_seconds or 0) // 60)}min"
    seed_tag = f"seed{seed}" if seed is not None else "seedNone"
    stamp = timestamp or time.strftime("%Y%m%d_%H%M%S")
    return f"test_{model_tag}_{duration_tag}_{seed_tag}_{stamp}.xlsx"


def export_test_workbook(
    model, duration_sim_seconds, seed, destination=None
):
    """Write the four-sheet benchmark workbook for one completed timed test.

    Runs entirely inside the simulator process: every sheet is built from the
    crash-safe JSONL logs on disk, so no dashboard handshake is required.
    """
    try:
        from openpyxl import Workbook
    except ImportError:
        print("openpyxl not installed; skipping test export")
        return None

    decisions = _read_jsonl_rows(AGENT_TURN_LOG_PATH)
    telemetry_rows = _read_jsonl_rows(TELEMETRY_LOG_PATH)

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

        if destination is None:
            destination = EXCEL_EXPORT_DIR / build_test_export_filename(
                model, duration_sim_seconds, seed
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
    """Stop the run at its configured sim-time and export the workbook."""
    config = control_panel.global_config
    config["is_running"] = False
    config["test_running"] = False
    destination = export_test_workbook(
        config.get("test_model", "None"),
        config.get("test_duration_sim_seconds"),
        config.get("test_seed"),
    )
    config["test_last_export"] = destination.name if destination else ""
    control_panel.write_ai_control()
    return destination


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
    from signal_controller import SignalController

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


def main():
    # Seeding makes traffic generation reproducible, not LLM inference. The
    # valid benchmark is the same seed with one ARMED and one DISARMED run;
    # same-seed ARMED runs may diverge because model decisions can differ.
    pygame.init()
    pygame.font.init()
    font = pygame.font.SysFont("Consolas", 13, bold=True)

    display_info = pygame.display.Info()
    startup_layout = calculate_startup_window_layout(
        display_info.current_w,
        display_info.current_h,
    )
    canvas_x, canvas_y = startup_layout["canvas_position"]
    os.environ["SDL_VIDEO_WINDOW_POS"] = f"{canvas_x},{canvas_y}"
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
    root.geometry(startup_layout["control_geometry"])

    # 2. Start Decoupled Telemetry Dashboard as a Subprocess
    print("Launching Telemetry Dashboard...")
    dashboard_environment = os.environ.copy()
    dashboard_environment["TRAFFIC_TELEMETRY_GEOMETRY"] = startup_layout[
        "telemetry_geometry"
    ]
    dashboard_proc = subprocess.Popen(
        [sys.executable, str(DASHBOARD_PATH)],
        cwd=str(BASE_DIR),
        env=dashboard_environment,
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
        run_just_reset = False

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                control_panel.global_config["is_running"] = False
                pygame.quit()
                try: root.destroy()
                except Exception: pass
                sys.exit()

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

        # A timed benchmark ends on the SIMULATION clock, never the wall clock,
        # so the same duration means the same amount of simulated traffic on
        # any machine regardless of render speed.
        if timed_test_is_complete(master_frame_count):
            finish_timed_test(master_frame_count)
            is_running = False

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
        pygame.display.flip()

        # Constant GUI polling rate (~60 FPS) decoupled from simulation speed
        root.after(16, simulation_step)

    root.after(16, simulation_step)
    root.mainloop()

if __name__ == "__main__":
    main()
