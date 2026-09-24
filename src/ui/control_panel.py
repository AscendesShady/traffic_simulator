# control_panel.py
import json
import os
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import ttk
import tempfile
import sys

from src.experiments import batch_runner
from src.ui.canvas_gemini import INT_X, PX_PER_M
from src.telemetry import real_world_units as units

NODE_A_X, NODE_B_X = INT_X[0], INT_X[1]

# ==========================================================
# COLOR PALETTE & DESIGN SYSTEM CONSTANTS
# ==========================================================
COLOR_BG = "#1A1E29"          # Deep rich background
COLOR_CARD = "#222834"        # Card background
COLOR_CARD_ALT = "#272E3D"    # Inner row background
COLOR_CARD_BORDER = "#333D50" # Modern subtle border
COLOR_ACCENT = "#2D8CFF"      # Accent blue
COLOR_SUCCESS = "#2ECC71"     # Active green
COLOR_WARNING = "#F59E0B"     # Amber / Orange
COLOR_DANGER = "#EF4444"      # Red / Alert
COLOR_TEXT_PRIMARY = "#FFFFFF"# Crisp white
COLOR_TEXT_SECONDARY = "#94A3B8"# Soft light blue-gray
# An OFF toggle chip's resting fill: distinctly lighter than COLOR_CARD_BORDER
# (which is a decorative outline/border shade, not meant to double as a fill)
# so an OFF chip reads as a real, visible control state rather than blending
# into the card behind it.
COLOR_TOGGLE_OFF = "#475569"

FONT_FAMILY = "Segoe UI"      # Clean UI font

# Type scale: exactly three sizes (title / section / body) so nothing in the
# panel is a one-off. Body is 9pt: the panel is a dense desktop tool in a
# ~400px column, where 16px body copy would leave no room for controls.
FONT_TITLE = (FONT_FAMILY, 13, "bold")
FONT_SECTION = (FONT_FAMILY, 10, "bold")
FONT_BODY = (FONT_FAMILY, 9)
FONT_BODY_BOLD = (FONT_FAMILY, 9, "bold")

# Spacing grid (px). Every gutter, card pad and row gap is one of these.
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
PAGE_GUTTER = SPACE_MD       # column edge -> card edge (matches the canvas gutter)
CARD_PAD = SPACE_SM          # card edge -> content
ROW_GAP = SPACE_XS           # between rows inside a card
# Collapsed cards stack on the tight end of the grid: with eight of them the
# whole column (title, status, every header) fits a main window that is only
# as tall as the simulation canvas, so nothing scrolls until a card opens.
SECTION_GAP = SPACE_XS       # between cards
SECTION_HEADER_PAD_Y = SPACE_XS  # disclosure header: ~28px tall at 10pt
BUTTON_PAD_Y = 7             # action buttons: ~32px tall at 9pt
CHIP_PAD_Y = 4               # toggle chips: ~26px tall
# Shared column grid inside a card: label | control. A fixed label width
# keeps every selector and entry starting on the same vertical line. Sliders
# do not use the column -- see add_slider_row -- so it only needs to fit the
# longest selector label ("Local model").
LABEL_COLUMN_CHARS = 11

BASE_DIR = Path(__file__).resolve().parents[2]
assert (BASE_DIR / "requirements.txt").exists(), (
    f"BASE_DIR does not resolve to the repo root: {BASE_DIR}"
)
AI_CONTROL_PATH = BASE_DIR / "data" / "ai_control.json"

API_MODEL_REGISTRY = {
    "GEMINI_API_KEY": [
        "gemini-2.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-pro",
    ],
    "OPENAI_API_KEY": [
        "gpt-5",
        "gpt-5-mini",
        "gpt-4.1",
    ],
    "GROK_API_KEY": [
        "grok-4.6",
        "grok-4",
        "grok-3-mini",
    ],
}

SYM_DOT = "\u25cf"            # ●
SYM_PAUSE = "\u23f8"          # ⏸
SYM_PLAY = "\u25b6"           # ▶
SYM_STOP = "\u25a0"           # ■
SYM_RESET = "\u21ba"          # ↺
SYM_BUS = "\u1f68c"           # 🚌
SYM_DISCLOSURE_OPEN = "\u25bc"    # ▼
SYM_DISCLOSURE_CLOSED = "\u25b6"  # ▶

SYM_HOURGLASS = "\N{HOURGLASS WITH FLOWING SAND}"  # ⏳ START while calibrating
SYM_EXPAND = "\N{NORTH EAST AND SOUTH WEST ARROW}"   # ⤢ grow the window
SYM_COMPACT = "\N{NORTH WEST AND SOUTH EAST ARROW}"  # ⤡ shrink it back

# Window shapes, owned by main.WindowShapeController; the panel only shows a
# button whose label names the *next* shape (the Start/Stop convention) and
# asks main, through window_shape_hooks, to cycle. The current shape is read
# back from global_config["window_shape"], which the controller also updates
# when the OS maximize/restore buttons change the window.
WINDOW_SHAPE_BUTTON_LABELS = {
    "compact": f"{SYM_EXPAND}  Large",
    "large": f"{SYM_EXPAND}  Maximize",
    "maximized": f"{SYM_COMPACT}  Compact",
}

# Card-header colour by group, so the column reads as three tiers: what you
# configure (white), what you run (amber), what you intervene with (red).
SECTION_ACCENT_CONFIG = COLOR_TEXT_PRIMARY
SECTION_ACCENT_RUN = COLOR_WARNING
SECTION_ACCENT_INTERVENTION = COLOR_DANGER
# Launch order of the collapsible cards, top to bottom.
SECTION_ORDER = (
    "Approach Traffic",
    "Bus Routes",
    "Single Run",
    "Batch Run",
    "Tuning",
    "Gridlock Discharge",
)

CONTROL_PANEL_MIN_HEIGHT = 360
CONTROL_PANEL_BOTTOM_MARGIN = 40
# Standalone-window width and the widest a wrapped status line may run.
# Both sized for the portrait column this panel now lays itself out as,
# whether it owns its own window or is mounted into MainWindow's left pane.
CONTROL_PANEL_WIDTH = 256
PORTRAIT_WRAP_LENGTH = 196
BENCHMARK_COUNTDOWN_WIDTH = len("00:00 PAUSED")

# Timed-benchmark durations, measured in SIMULATION seconds so a run is
# comparable regardless of how fast the host renders it.
TEST_DURATIONS = {
    "5 min": 300,
    "10 min": 600,
    "15 min": 900,
    "30 min": 1800,
    "1 hr": 3600,
    "2 hr": 7200,
}

DISCHARGE_AUTO = "Auto (Recommended)"
DISCHARGE_OPTIONS = (
    DISCHARGE_AUTO,
    "Eastbound Corridor",
    "Westbound Corridor",
    "Node A Northbound",
    "Node A Southbound",
    "Node B Northbound",
    "Node B Southbound",
)

# Explicit baseline entry for the Batch Benchmark Runner's model picker, so
# the no-LLM Webster baseline can be swept as a batch condition alongside
# real decision sources. Maps to the ordinary "None" model when applied.
BATCH_BASELINE_LABEL = "None (baseline)"

# The largest hard call timeout agent.py imposes on any provider
# (agent.OLLAMA_TIMEOUT_SECONDS; the API providers are 30 s): the most one
# stuck call can cost is ceil(ceiling / tick) grid points, skipped and
# counted. tests/test_llm_control_loop.py pins it to agent.py's constants.
AGENT_CALL_TIMEOUT_CEILING_SEC = 45
# One decision interval for every arm (the paired DV needs one schedule).
# It is set by the bus, not by the slowest model: a bus reaches Node A's
# stop line about 39 s after entering (350 m at 9 m/s) and is inside the
# TSP eligibility zone for the last 100 m (default; up to 350 m), about
# 11 s, so 10 s gives every bus about four decision points on its approach
# and at least one inside the zone; 60 s left many buses reaching A before
# any decision had seen them. It
# must also clear the slowest arm's p95 latency by a margin -- the kept
# local models answer in 0.7-1.7 s median, 2.0 s worst, at the fixed 8k
# context (2026-09-23 benchmark) -- and print_campaign_summary flags any
# arm that skipped more than SKIP_RATE_TOLERANCE of its grid points.
DEFAULT_TICK_SECONDS = 10

DEFAULT_BATCH_RUNTIME = {
    "active": False,
    "models": [],
    "seeds": [],
    "total": 0,
    "current": None,
    "results": [],
    # STOP BATCH freezes the sweep in place (the run in flight pauses);
    # RESUME continues it, END discards it and resets the simulation.
    "paused": False,
    # Independent of the Single Run card's own "Decision interval" slider:
    # applies to the single Benchmark Test and to every queued Batch
    # Benchmark run, so an unattended sweep is never silently governed by
    # whatever the Single Run panel happens to be set to.
    # Same default as the Single Run card (DEFAULT_TICK_SECONDS).
    "tick_seconds": DEFAULT_TICK_SECONDS,
}

# Live widget references used by the periodic repaint poller. The data in
# bus_routes_config remains authoritative whether a human or the LLM changed it.
route_flag_buttons = {}
# Collapsible sections by title: {"card", "header", "body", "state", "toggle"}.
# Filled by make_section so callers can expand/collapse without widget access.
control_panel_sections = {}
# main.py installs its shape controller's cycle() here at startup; the panel
# never imports main (main imports the panel at load time).
window_shape_hooks = {"cycle": None}


def request_window_shape_cycle():
    """Ask the composition root for the next window shape; a no-op when the
    panel is running standalone with nothing installed."""
    callback = window_shape_hooks.get("cycle")
    if callback is not None:
        callback()

# ==========================================================
# SHARED STATE DICTIONARIES (Accessed by main.py)
# ==========================================================
global_config = {
    "is_paused": False,
    "sim_speed": 1.0,        # 0.5x to 3.0x speed multiplier
    "green_time": 240,       # Signal green phase duration in frames
    "random_seed": None,     # None = OS entropy; int = reproducible traffic
    # Applied to SignalController on START/reset. Capped at the 400 px link
    # between the nodes: a bus cannot request priority at a node it has not
    # been released toward (the controller clamps and logs anything above).
    "priority_eligibility_px": 400,
    "vehicle_speed_scale": 0.5,      # Pending scale, applied on START/reset
    # Car-following/lane-change engine, applied on START/reset: "idm"
    # (physically calibrated, the default; vehicle.py) or "legacy" (the
    # former gap/speed rule, kept for comparison only).
    "movement_model": "idm",
    # Signal change intervals, applied on START/reset: "ite" computes yellow
    # and all-red from the ITE kinematic formulas at the configured speed
    # (main.signal_change_intervals); "legacy" keeps the former 1 s + 1 s.
    "signal_change_intervals": "ite",
    # "coordinated": both nodes run one common cycle with a progression
    # offset and lock to it every cycle; "independent": each node runs its
    # own Webster cycle from phase 0 (the former behaviour, whose relative
    # offset drifts). Progression is timed for this direction.
    "signal_coordination": "coordinated",
    "coordination_direction": "EB",
    # Bus dwell at each route stop (bus_routes_config[...]["stops"]), per the
    # Transit Capacity and Quality of Service Manual, 3rd ed. (TCRP Report
    # 165, 2013), Ch. 6: dwell = door open/close time + passenger service,
    # boarding and alighting through separate doors (the longer governs).
    # Values sit inside the TCQSM ranges and are scenario defaults to be
    # calibrated for a real corridor. Boardings and alightings are equal in
    # expectation, so bus occupancy stays BUS_PASSENGERS (a declared
    # simplification: the passenger DVs keep their fixed bus weight).
    # Floating-car-data sampling period, sim seconds; 0 = off (main._sample_fcd).
    "fcd_period_s": 0,
    "bus_dwell": {
        "door_time_sec": 4.0,
        "board_sec_per_pax": 3.0,
        "alight_sec_per_pax": 2.0,
        "mean_boardings": 4.0,
        "mean_alightings": 4.0,
        "doors": 2,
    },
    "_active_vehicle_speed_scale": 0.5,  # Runtime snapshot for this episode
    "reset_triggered": False,# Flag to wipe canvas vehicles
    "start_requested": False,# START requests a fresh run from frame zero
    "is_running": False,     # Sim launches idle; START begins a fresh run
    "run_has_started": False,# Distinguishes launch-idle from a completed STOP
    # Timed-benchmark length. Defaults to an hour: a 2-minute warm-up plus a
    # steady window long enough for the cumulative DV to converge. None = free run.
    "test_duration_sim_seconds": 3600,
    # Frames discarded from the front of every steady-state DV (main.py).
    "warmup_discard_frames": 18000,
    "test_running": False,   # True while a timed benchmark run is active
    "test_model": "None",    # Model captured when the test started
    "test_seed": None,       # Seed captured when the test started
    "test_last_export": "",  # Filename written by the last completed test
    "batch_start_requested": False,  # RUN BATCH requests one queued sweep
    "batch_stop_requested": False,   # STOP BATCH: pause the sweep in place
    "batch_resume_requested": False, # RESUME: continue the paused sweep
    "batch_end_requested": False,    # END: discard the sweep, full reset
    "batch_runtime": dict(DEFAULT_BATCH_RUNTIME),
    "sim_time_seconds": 0.0, # Simulation clock, published by main.py
    "window_shape": "compact",  # compact | large | maximized, owned by main.py
    # Derived per-node cycle lengths, published at START after calibration.
    # This is runtime output, never an operator input.
    "cycle_time_sec": {},
    "calibrating": False,    # True while saturation flow is measured
    "measured_saturation_flow": None,  # veh/hr, measured each START
    "webster_splits": {},    # Per-node green frames from Webster
    "discharge_selection": DISCHARGE_AUTO,
    "discharge_start_requested": False,
    "discharge_stop_requested": False,
    "discharge_runtime": {
        "active": False,
        "selected": DISCHARGE_AUTO,
        "status": "IDLE",
        "reason": "Normal signal control is active",
        "recommendation": "Select Auto or a corridor, then start discharge",
        "stage": "",
        "vehicles_discharged": 0,
    },
    "ai_runtime": {
        "armed": False,
        "model": "None",
        # "assisted": the model requests TSP/DBL per route over Webster;
        # "configured": the model writes the timing plan and the lane-2
        # flashers itself (AI/LLM-Based only; guard.validate_signal_plan).
        "control_mode": "assisted",
        "tick_seconds": DEFAULT_TICK_SECONDS,
        "last_status": "INACTIVE",
        "last_turn": 0,
    },
}


def set_random_seed(value):
    """Store a validated traffic seed; blank input restores OS entropy."""
    text = "" if value is None else str(value).strip()
    normalized = None if not text else int(text)
    global_config["random_seed"] = normalized
    return normalized


# TSP eligibility zone slider: 62.5 m up to 350 m (default 100 m, 400 px).
PRIORITY_ELIGIBILITY_UI_MIN_PX = 250
PRIORITY_ELIGIBILITY_UI_MAX_PX = 350 * PX_PER_M


def set_priority_eligibility_px(value):
    """Store the eligibility distance to apply on the next START/reset."""
    normalized = max(
        PRIORITY_ELIGIBILITY_UI_MIN_PX,
        min(PRIORITY_ELIGIBILITY_UI_MAX_PX, int(round(float(value)))),
    )
    global_config["priority_eligibility_px"] = normalized
    return normalized


def set_vehicle_speed_scale(value):
    """Store the vehicle speed multiplier to apply on the next START/reset."""
    normalized = max(0.25, min(1.0, float(value)))
    global_config["vehicle_speed_scale"] = normalized
    return normalized


def get_webster_timing_summary():
    """Return presentation-ready Webster timing data for the control panel.

    The measured saturation flow applies to both nodes, while the cycle,
    green split and demand ratios are node-specific. Keeping those levels
    separate prevents the UI from repeating one dense diagnostic sentence
    for every node.
    """
    if global_config.get("calibrating", False):
        return {
            "state": "CALIBRATING",
            "message": "Measuring lane capacity - please wait...",
            "saturation_flow": None,
            "nodes": [],
        }
    saturation = global_config.get("measured_saturation_flow")
    if not saturation:
        return {
            "state": "IDLE",
            "message": "Timing is calculated automatically when START is pressed.",
            "saturation_flow": None,
            "nodes": [],
        }
    splits = global_config.get("webster_splits") or {}
    nodes = []
    has_oversaturated_node = False
    for node_x, node_name in ((NODE_A_X, "A"), (NODE_B_X, "B")):
        split = splits.get(node_x) or splits.get(str(node_x))
        if not isinstance(split, dict):
            nodes.append({
                "position": node_x,
                "name": node_name,
                "available": False,
                "status": "Unavailable",
            })
            continue
        oversaturated = bool(split.get("oversaturated", False))
        if oversaturated:
            has_oversaturated_node = True
        nodes.append({
            "position": node_x,
            "name": node_name,
            "available": True,
            "status": "Oversaturated" if oversaturated else "Optimal",
            "oversaturated": oversaturated,
            "cycle_time_sec": float(split.get("cycle_time_sec") or 0.0),
            "total_ratio": float(split.get("Y") or 0.0),
            "ew_ratio": float(split.get("y_ew") or 0.0),
            "ns_ratio": float(split.get("y_ns") or 0.0),
            "ew_green_sec": float(split.get("EW_green_sec") or 0.0),
            "ns_green_sec": float(split.get("NS_green_sec") or 0.0),
        })
    return {
        "state": "OVERSATURATED" if has_oversaturated_node else "READY",
        "message": f"Measured lane capacity: {float(saturation):,.0f} vehicles/hour/lane",
        "saturation_flow": float(saturation),
        "nodes": nodes,
    }


def describe_webster_timing():
    """Full per-node Webster diagnostics for logs and compatibility callers."""
    summary = get_webster_timing_summary()
    if not summary["nodes"]:
        if summary["state"] == "CALIBRATING":
            return "Calibrating Webster, please wait...", summary["state"]
        return "Webster timing calibrates on START", summary["state"]

    saturation = summary["saturation_flow"]
    lines = []
    for node in summary["nodes"]:
        node_x = node["position"]
        node_name = node["name"]
        if not node["available"]:
            lines.append(f"NODE {node_x} ({node_name}): Webster timing unavailable")
            continue
        cycle = node["cycle_time_sec"]
        total_y = node["total_ratio"]
        y_ew = node["ew_ratio"]
        y_ns = node["ns_ratio"]
        ew_green = node["ew_green_sec"]
        ns_green = node["ns_green_sec"]
        if node["oversaturated"]:
            lines.append(
                f"NODE {node_x} ({node_name}):  S={float(saturation):.0f} veh/hr  "
                f"Y={total_y:.2f}  OVERSATURATED — cycle capped at {cycle:.0f}s"
            )
            lines.append(
                "                 Reduce demand or raise vehicle speed; "
                f"EW green {ew_green:.1f}s | NS green {ns_green:.1f}s  "
                f"(y_ew={y_ew:.2f}, y_ns={y_ns:.2f})"
            )
        else:
            lines.append(
                f"NODE {node_x} ({node_name}):  S={float(saturation):.0f} veh/hr  "
                f"Y={total_y:.2f}  cycle={cycle:.0f}s (optimal)"
            )
            lines.append(
                f"                 EW green {ew_green:.1f}s | "
                f"NS green {ns_green:.1f}s  "
                f"(y_ew={y_ew:.2f}, y_ns={y_ns:.2f})"
            )
    return "\n".join(lines), summary["state"]


def request_start_stop():
    """Request a fresh START, or STOP the active run without clearing data."""
    if global_config.get("is_running", False):
        global_config["is_running"] = False
        global_config["start_requested"] = False
        # A manual STOP abandons any timed test. Logs are deliberately kept so
        # the operator can still export the partial run by hand.
        global_config["test_running"] = False
        return "STOPPED"
    global_config["start_requested"] = True
    return "START_REQUESTED"


def format_test_countdown(duration_sim_seconds, sim_time_seconds):
    """Return MM:SS of simulation time left in a timed run, or None.

    Counts down the simulation clock, so it stays truthful at any sim speed
    and reaches zero exactly when the auto-stop fires.
    """
    try:
        duration = float(duration_sim_seconds)
        elapsed = float(sim_time_seconds)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    remaining = max(0.0, duration - max(0.0, elapsed))
    minutes, seconds = divmod(int(remaining), 60)
    return f"{minutes:02d}:{seconds:02d}"


def request_start_test():
    """Begin one timed benchmark run at the currently configured duration.

    The run reuses the ordinary START path, so it gets the same full clean
    reset. Model and seed are whatever the operator already selected; they
    are captured here so a mid-run change cannot rename the export.
    """
    duration = global_config.get("test_duration_sim_seconds")
    if not duration:
        return None
    global_config["test_model"] = str(
        global_config.get("ai_runtime", {}).get("model", "None")
    )
    global_config["test_seed"] = global_config.get("random_seed")
    global_config["test_control_mode"] = str(
        global_config.get("ai_runtime", {}).get("control_mode", CONTROL_MODE_ASSISTED)
    )
    global_config["test_running"] = True
    global_config["start_requested"] = True
    return int(duration)


def request_pause_resume():
    """Toggle an active run's pause without changing its run lifecycle."""
    if not global_config.get("is_running", False):
        return bool(global_config.get("is_paused", False))
    global_config["is_paused"] = not global_config.get("is_paused", False)
    return global_config["is_paused"]


def set_disclosure_state(button, body, title, expanded, pack_options=None):
    """Show or hide one UI section without destroying any child widgets."""
    expanded = bool(expanded)
    arrow = SYM_DISCLOSURE_OPEN if expanded else SYM_DISCLOSURE_CLOSED
    button.config(text=f"{arrow}  {title}")
    if expanded:
        body.pack(**(pack_options or {"fill": "x"}))
    else:
        body.pack_forget()
    return expanded


def bounded_control_panel_height(requested_height, screen_height, window_y=0):
    """Fit a disclosure-panel window on screen without making it unusably short."""
    requested = max(CONTROL_PANEL_MIN_HEIGHT, int(requested_height))
    available = max(
        CONTROL_PANEL_MIN_HEIGHT,
        int(screen_height) - max(0, int(window_y)) - CONTROL_PANEL_BOTTOM_MARGIN,
    )
    return min(requested, available)


def snap_scale_value(value, lower, upper, step):
    """Clamp ``value`` to [lower, upper] on the grid ``lower + n * step``.

    Pointer drags leave a ttk.Scale at an arbitrary fraction (12.37); a
    keyboard step from there would land on 13.37, which the integer labels
    still show as 13 but the next drag or export would carry the tail. Floor
    to the grid so every keyboard step lands on a labelled value.
    """
    lower, upper = min(lower, upper), max(lower, upper)
    steps = int((float(value) - lower) / step + 1e-9)
    snapped = lower + steps * step
    return round(max(lower, min(upper, snapped)), 10)


def enable_scale_keyboard(scale, step):
    """Give a ttk.Scale precise, clamped keyboard adjustment.

    Clicking the scale (or tabbing to it) gives it keyboard focus. Left/Down
    and Right/Up move exactly one ``step`` on the scale's value grid; Home
    and End jump to the ends. ``Scale.set`` continues to invoke the scale's
    existing command callback, so keyboard and pointer changes update exactly
    the same configuration and labels.
    """
    step = abs(float(step))
    if step == 0:
        raise ValueError("Keyboard scale step must be greater than zero")
    scale.configure(takefocus=True)

    def focus_scale(_event=None):
        scale.focus_set()

    def bounds():
        lower = float(scale.cget("from"))
        upper = float(scale.cget("to"))
        return min(lower, upper), max(lower, upper)

    def move(direction):
        def adjust(_event=None):
            lower, upper = bounds()
            current = snap_scale_value(scale.get(), lower, upper, step)
            value = max(lower, min(upper, current + direction * step))
            # Prevent binary floating-point tails from reaching labels/config.
            scale.set(round(value, 10))
            return "break"

        return adjust

    def jump(to_upper):
        def adjust(_event=None):
            lower, upper = bounds()
            scale.set(upper if to_upper else lower)
            return "break"

        return adjust

    scale.bind("<Button-1>", focus_scale, add="+")
    scale.bind("<Left>", move(-1), add="+")
    scale.bind("<Down>", move(-1), add="+")
    scale.bind("<Right>", move(1), add="+")
    scale.bind("<Up>", move(1), add="+")
    scale.bind("<Home>", jump(False), add="+")
    scale.bind("<End>", jump(True), add="+")
    return scale


# ==========================================================
# WIDGET HELPERS -- one look for every card, button, row and toggle
# ==========================================================
BUTTON_VARIANTS = {
    # variant: (background, foreground, pressed background)
    "primary": (COLOR_ACCENT, COLOR_TEXT_PRIMARY, "#1E70E0"),
    "success": (COLOR_SUCCESS, COLOR_BG, "#24A35A"),
    "warning": (COLOR_WARNING, COLOR_BG, "#D97706"),
    "danger": (COLOR_DANGER, COLOR_TEXT_PRIMARY, "#D32F2F"),
}
BUTTON_HOVER_MIX = 0.12


def blend_toward(color, target, amount):
    """Mix ``color`` toward ``target`` by ``amount`` (0-1); both "#rrggbb"."""
    c = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    t = [int(target[i:i + 2], 16) for i in (1, 3, 5)]
    mixed = [round(a + (b - a) * amount) for a, b in zip(c, t)]
    return "#{:02X}{:02X}{:02X}".format(*mixed)


def add_hover_state(button):
    """Lighten a button while the pointer is over it (rulebook hover state).

    The rest colour is read on every entry, so a button whose background is
    repainted by a status poller (START/STOP, PAUSE/RESUME) still hovers
    from whatever colour it currently shows.
    """
    def enter(_event=None):
        if str(button.cget("state")) == "disabled":
            return
        button._hover_rest_bg = button.cget("bg")
        button.config(
            bg=blend_toward(button._hover_rest_bg, "#FFFFFF", BUTTON_HOVER_MIX)
        )

    def leave(_event=None):
        rest = getattr(button, "_hover_rest_bg", None)
        if rest is not None and str(button.cget("state")) != "disabled":
            button.config(bg=rest)
        button._hover_rest_bg = None

    button.bind("<Enter>", enter, add="+")
    button.bind("<Leave>", leave, add="+")
    return button


def refresh_hover_rest_bg(button, bg):
    """Keep a hover-decorated button's cached rest colour in sync with a repaint.

    add_hover_state caches the resting background at <Enter> and restores
    exactly that value at <Leave>. That is correct for a continuously
    repainted status button (see add_hover_state's docstring), but a button
    repainted only on click -- every toggle chip -- is normally still
    "hovered" at the moment of that click (the pointer has to be over it to
    click it), so without this the cached pre-click colour survives the
    repaint and reappears the instant the pointer leaves, i.e. the chip
    never visibly changes state until the next full hover cycle. Call this
    right after any such one-shot repaint.
    """
    if getattr(button, "_hover_rest_bg", None) is not None:
        button._hover_rest_bg = bg


class OutlinedButton(tk.Button):
    """A flat button inside a 1px frame that draws its outline.

    Windows Tk never paints a Button's highlight ring, so an outlined
    secondary button (and a visible keyboard-focus ring) needs a real frame
    around it. The frame is what gets packed or gridded; every geometry
    call on the button is forwarded to it, so callers use this exactly like
    a plain ``tk.Button`` and ``.config(...)`` still targets the button.
    """

    def __init__(self, parent, outline, focus_color=COLOR_ACCENT, **options):
        self.outline_frame = tk.Frame(parent, bg=outline)
        self._outline = outline
        super().__init__(self.outline_frame, **options)
        tk.Button.pack(self, fill="both", expand=True, padx=1, pady=1)
        self.bind(
            "<FocusIn>", lambda _e: self.outline_frame.config(bg=focus_color), add="+"
        )
        self.bind(
            "<FocusOut>", lambda _e: self.outline_frame.config(bg=self._outline), add="+"
        )

    def set_outline(self, color):
        self._outline = color
        self.outline_frame.config(bg=color)

    def pack(self, **kwargs):
        self.outline_frame.pack(**kwargs)

    pack_configure = pack

    def grid(self, **kwargs):
        self.outline_frame.grid(**kwargs)

    grid_configure = grid

    def pack_forget(self):
        self.outline_frame.pack_forget()

    def grid_forget(self):
        self.outline_frame.grid_forget()

    def winfo_manager(self):
        return self.outline_frame.winfo_manager()


def make_button(parent, text, variant="neutral", command=None, **overrides):
    """A flat, full-hit-area action button with hover and focus states.

    One filled ``primary``/``success``/``warning``/``danger`` button per
    card; everything else is ``neutral`` -- an outlined secondary button on
    the card surface -- so the eye lands on the main action. All variants
    share one height (font + BUTTON_PAD_Y) so a row of them lines up.
    """
    surface = parent.cget("bg")
    if variant == "neutral":
        background, foreground, active = surface, COLOR_TEXT_PRIMARY, COLOR_CARD_ALT
        outline = COLOR_CARD_BORDER
    else:
        background, foreground, active = BUTTON_VARIANTS[variant]
        outline = surface
    options = {
        "text": text,
        "font": FONT_BODY_BOLD,
        "bg": background,
        "fg": foreground,
        "activebackground": active,
        "activeforeground": foreground,
        "disabledforeground": COLOR_TEXT_SECONDARY,
        "bd": 0,
        "relief": "flat",
        "padx": SPACE_MD,
        "pady": BUTTON_PAD_Y,
        "cursor": "hand2",
        "highlightthickness": 0,
        "command": command,
    }
    options.update(overrides)
    # Filled buttons get an invisible (surface-coloured) frame so every
    # variant is the same height and shows the same focus ring.
    return add_hover_state(OutlinedButton(parent, outline, **options))


def paint_toggle_chip(button, on, on_color, on_text, off_text):
    """Repaint a two-state chip: filled when on, outlined-looking when off.

    State is carried by the label text as well as the fill, so it never
    depends on colour alone.
    """
    if on:
        bg = on_color
        fg = COLOR_BG if on_color in (COLOR_SUCCESS, COLOR_WARNING) else COLOR_TEXT_PRIMARY
        button.config(
            text=on_text,
            bg=bg,
            fg=fg,
            activebackground=on_color,
            activeforeground=fg,
        )
    else:
        bg = COLOR_TOGGLE_OFF
        button.config(
            text=off_text,
            bg=bg,
            fg=COLOR_TEXT_PRIMARY,
            activebackground=COLOR_CARD_BORDER,
            activeforeground=COLOR_TEXT_PRIMARY,
        )
    # A toggle chip is repainted only on click, and the pointer has to be
    # over it to click it -- so without this, add_hover_state's cached
    # pre-click colour survives the repaint and silently reappears the
    # instant the pointer leaves the chip.
    refresh_hover_rest_bg(button, bg)
    return bool(on)


def make_toggle_chip(parent, on, on_color, on_text, off_text, command=None, width=9):
    button = OutlinedButton(
        parent,
        parent.cget("bg"),
        font=FONT_BODY_BOLD,
        bd=0,
        relief="flat",
        padx=SPACE_SM,
        pady=CHIP_PAD_Y,
        width=width,
        cursor="hand2",
        highlightthickness=0,
        command=command,
    )
    paint_toggle_chip(button, on, on_color, on_text, off_text)
    return add_hover_state(button)


def make_label(parent, text, bold=False, color=COLOR_TEXT_PRIMARY, **overrides):
    options = {
        "text": text,
        "font": FONT_BODY_BOLD if bold else FONT_BODY,
        "bg": parent.cget("bg"),
        "fg": color,
        "anchor": "w",
        "justify": "left",
    }
    options.update(overrides)
    return tk.Label(parent, **options)


def add_labeled_row(parent, label_text, pady=(0, ROW_GAP)):
    """A row whose label sits in the shared label column, so every control
    in a card starts on the same vertical line."""
    row = tk.Frame(parent, bg=parent.cget("bg"))
    row.pack(fill="x", pady=pady)
    make_label(
        row, label_text, color=COLOR_TEXT_SECONDARY, width=LABEL_COLUMN_CHARS
    ).pack(side="left")
    return row


def add_slider_row(
    parent, label_text, value_text, from_, to, value, command, step, style,
    pady=(0, ROW_GAP),
):
    """Label and live value on one line, a full-width slider beneath them.

    The panel is a ~256px column: a slider beside its label would be left
    with almost no travel, so the caption takes one line (label left, value
    right) and the slider gets the whole card width below it.

    Returns (value_label, scale). The scale is keyboard-adjustable by
    exactly ``step`` per arrow press.
    """
    surface = parent.cget("bg")
    caption = tk.Frame(parent, bg=surface)
    caption.pack(fill="x")
    make_label(caption, label_text, color=COLOR_TEXT_SECONDARY).pack(side="left")
    value_label = make_label(
        caption, value_text, bold=True, color=COLOR_ACCENT, anchor="e"
    )
    value_label.pack(side="right")
    scale = ttk.Scale(
        parent, from_=from_, to=to, value=value, style=style, command=command
    )
    scale.pack(fill="x", pady=pady)
    enable_scale_keyboard(scale, step=step)
    return value_label, scale


class SplitBar(tk.Canvas):
    """A 100 % bar cut into 2 or 3 segments by 1 or 2 draggable thumbs.

    Tk has no range slider; this is the smallest one that reads like the
    panel's thin scales. ``values`` are the thumb positions in whole percent
    (ascending); the segments are the gaps between 0, the thumbs and 100.
    Drag a thumb, or click it (or Tab to the bar) and use Left/Right, Home/
    End; the focused thumb draws a ring. ``command(segments)`` fires with the
    integer percentages of every segment on each change.
    """

    THUMB_R = 6
    TRACK_H = 4
    HEIGHT = 20
    SEGMENT_COLORS = (COLOR_ACCENT, COLOR_WARNING, COLOR_SUCCESS)

    def __init__(self, parent, values, command=None, **kwargs):
        surface = parent.cget("bg")
        # width=1: a Canvas's default 10 cm request would otherwise widen
        # every approach card past the pane and clip it; fill="x" sizes it.
        kwargs.setdefault("width", 1)
        super().__init__(
            parent, height=self.HEIGHT, bg=surface, highlightthickness=0,
            cursor="hand2", takefocus=True, **kwargs,
        )
        self.values = [int(v) for v in values]
        self.command = command
        self.active = 0          # thumb index that drags or takes the keys
        self._dragging = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_dragging", None))
        self.bind("<FocusIn>", lambda _e: self._draw())
        self.bind("<FocusOut>", lambda _e: self._draw())
        for key, delta in (("<Left>", -1), ("<Down>", -1), ("<Right>", 1), ("<Up>", 1)):
            self.bind(key, lambda _e, d=delta: self._nudge(d))
        self.bind("<Home>", lambda _e: self._set(self.active, 0))
        self.bind("<End>", lambda _e: self._set(self.active, 100))

    def segments(self):
        edges = [0, *self.values, 100]
        return [b - a for a, b in zip(edges, edges[1:])]

    # --- geometry ---------------------------------------------------------
    def _x(self, value):
        pad = self.THUMB_R + 1
        return pad + (self.winfo_width() - 2 * pad) * value / 100.0

    def _value_at(self, x):
        pad = self.THUMB_R + 1
        span = max(1, self.winfo_width() - 2 * pad)
        return int(round(max(0.0, min(100.0, (x - pad) * 100.0 / span))))

    # --- interaction ------------------------------------------------------
    def _press(self, event):
        self.focus_set()
        # Nearest thumb takes the press: the whole bar is the hit area.
        self.active = min(
            range(len(self.values)), key=lambda i: abs(self._x(self.values[i]) - event.x)
        )
        self._dragging = self.active
        self._set(self.active, self._value_at(event.x))

    def _drag(self, event):
        if self._dragging is not None:
            self._set(self._dragging, self._value_at(event.x))

    def _nudge(self, delta):
        self._set(self.active, self.values[self.active] + delta)

    def _set(self, index, value):
        # Thumbs never cross: each is clamped between its neighbours.
        low = self.values[index - 1] if index > 0 else 0
        high = self.values[index + 1] if index + 1 < len(self.values) else 100
        value = max(low, min(high, int(value)))
        if value == self.values[index]:
            return
        self.values[index] = value
        self._draw()
        if self.command:
            self.command(self.segments())

    # --- painting ---------------------------------------------------------
    def _draw(self):
        self.delete("all")
        cy = self.HEIGHT / 2.0
        edges = [0, *self.values, 100]
        for i, (a, b) in enumerate(zip(edges, edges[1:])):
            self.create_rectangle(
                self._x(a), cy - self.TRACK_H / 2.0, self._x(b), cy + self.TRACK_H / 2.0,
                fill=self.SEGMENT_COLORS[i % len(self.SEGMENT_COLORS)], width=0,
            )
        focused = self.focus_get() is self
        for i, value in enumerate(self.values):
            x = self._x(value)
            r = self.THUMB_R
            self.create_oval(
                x - r, cy - r, x + r, cy + r,
                fill=COLOR_TEXT_PRIMARY, outline=COLOR_CARD_BORDER, width=1,
            )
            if focused and i == self.active:
                self.create_oval(
                    x - r - 2, cy - r - 2, x + r + 2, cy + r + 2,
                    outline=COLOR_ACCENT, width=2,
                )


def add_split_bar_row(parent, labels, segments, command):
    """Caption naming every segment with its live percent, a SplitBar below.

    ``labels`` and ``segments`` have equal length (2 or 3); the bar gets
    ``len(labels) - 1`` thumbs at the cumulative segment edges. Returns the
    bar; the caption updates itself.
    """
    surface = parent.cget("bg")
    caption = tk.Frame(parent, bg=surface)
    caption.pack(fill="x")
    value_labels = []
    for i, label in enumerate(labels):
        # One cell per segment, value under its label in the segment's
        # colour, spread across the width so the caption reads against the
        # bar. Stacked, not side by side: three "label value" pairs in a
        # row overrun the narrowest control pane and clip the card.
        cell = tk.Frame(caption, bg=surface)
        cell.pack(side="left", expand=True)
        make_label(cell, label, color=COLOR_TEXT_SECONDARY).pack()
        value = make_label(
            cell, "", bold=True,
            color=SplitBar.SEGMENT_COLORS[i % len(SplitBar.SEGMENT_COLORS)],
        )
        value.pack()
        value_labels.append(value)

    def refresh(parts):
        for label, part in zip(value_labels, parts):
            label.config(text=f"{int(part)}%")
        command(parts)

    edges = []
    running = 0
    for part in segments[:-1]:
        running += int(part)
        edges.append(running)
    bar = SplitBar(parent, edges, command=refresh)
    bar.pack(fill="x", pady=(0, ROW_GAP))
    for label, part in zip(value_labels, segments):
        label.config(text=f"{int(part)}%")
    return bar


def make_spinbox(parent, from_, to, value, width=3):
    """A dark-themed integer stepper on the card surface.

    Typed values are committed by ``bind_spinbox_changes``; the arrow
    buttons and Up/Down keys step by one.
    """
    box = tk.Spinbox(
        parent, from_=from_, to=to, increment=1, width=width, font=FONT_BODY,
        bg=COLOR_CARD_ALT, fg=COLOR_TEXT_PRIMARY, insertbackground=COLOR_TEXT_PRIMARY,
        buttonbackground=COLOR_CARD_BORDER, relief="flat", bd=0,
        highlightthickness=1, highlightbackground=COLOR_CARD_BORDER,
        highlightcolor=COLOR_ACCENT, justify="right",
    )
    box.delete(0, "end")
    box.insert(0, str(int(value)))
    return box


def bind_spinbox_changes(box, apply):
    """Run ``apply(commit)`` for every way a Spinbox value can change.

    Arrow buttons / Up-Down keys (``command``), Return and focus leaving
    are commits (``commit=True``: junk in the field is replaced by the value
    in effect); a key release while typing is not (``commit=False``: a
    half-typed number is left alone but a valid one already takes effect).
    """
    box.config(command=lambda: apply(True))
    box.bind("<Return>", lambda _e: apply(True), add="+")
    box.bind("<FocusOut>", lambda _e: apply(True), add="+")
    box.bind("<KeyRelease>", lambda _e: apply(False), add="+")
    return box


def make_item_card(parent):
    """One bordered item card inside a long editor; returns its content frame.

    The approach and bus-route editors stack six of these each. They sit on
    the tight end of the spacing grid (SPACE_XS above, below and between
    cards) so most of an editor is visible in one expansion of its section.
    """
    card = tk.Frame(
        parent, bg=COLOR_CARD_ALT, highlightbackground=COLOR_CARD_BORDER,
        highlightthickness=1, bd=0,
    )
    card.pack(fill="x", pady=(0, SPACE_XS))
    inner = tk.Frame(card, bg=COLOR_CARD_ALT)
    inner.pack(fill="x", padx=SPACE_SM, pady=SPACE_XS)
    return inner


def make_section(parent, title, accent=COLOR_TEXT_PRIMARY, expanded=True, on_toggle=None):
    """One collapsible card: a full-width disclosure header over a body.

    Returns a dict with ``card``, ``header``, ``body``, ``state`` and
    ``toggle`` so callers (and tests) can drive the section without knowing
    its widgets. The registry ``control_panel_sections`` keeps the same dict
    under ``title``.
    """
    card = tk.Frame(
        parent, bg=COLOR_CARD, highlightbackground=COLOR_CARD_BORDER,
        highlightthickness=1, bd=0,
    )
    card.pack(fill="x", padx=PAGE_GUTTER, pady=(0, SECTION_GAP))
    header = tk.Button(
        card,
        font=FONT_SECTION,
        bg=COLOR_CARD,
        fg=accent,
        activebackground=COLOR_CARD_ALT,
        activeforeground=accent,
        bd=0,
        relief="flat",
        cursor="hand2",
        anchor="w",
        padx=CARD_PAD,
        pady=SECTION_HEADER_PAD_Y,
        highlightthickness=1,
        highlightbackground=COLOR_CARD,
        highlightcolor=COLOR_ACCENT,
    )
    header.pack(fill="x")
    body = tk.Frame(card, bg=COLOR_CARD)
    body_pack = {"fill": "x", "padx": CARD_PAD, "pady": (0, CARD_PAD)}
    state = {"expanded": bool(expanded)}

    def toggle():
        state["expanded"] = set_disclosure_state(
            header, body, title, not state["expanded"], body_pack
        )
        if on_toggle is not None:
            on_toggle()

    header.config(command=toggle)
    set_disclosure_state(header, body, title, state["expanded"], body_pack)
    section = {
        "card": card, "header": header, "body": body,
        "state": state, "toggle": toggle,
    }
    control_panel_sections[title] = section
    return section


def make_subsection_heading(parent, title, first=False):
    """Add a compact label that separates tools merged into one run card."""
    row = tk.Frame(parent, bg=COLOR_CARD)
    row.pack(fill="x", pady=((0 if first else SPACE_MD), SPACE_SM))
    make_label(row, title, bold=True, color=COLOR_TEXT_PRIMARY).pack(side="left")
    rule = tk.Frame(row, bg=COLOR_CARD_BORDER, height=1)
    rule.pack(side="left", fill="x", expand=True, padx=(SPACE_SM, 0))
    return row

# ----------------------------------------------------------
# 6 SIMULTANEOUS BUS ROUTES (waypoints/lanes keyed by node x from canvas.INT_X)
# ----------------------------------------------------------
# Headway slider ceiling: 300 s (5 min) covers an off-peak feeder service.
# 0 is the OFF position, so the usable band is 1-300 s.
MAX_HEADWAY_SEC = 300

# Mean of the car desired-speed draw in main._draw_arrival, uniform(1.0, 1.4)
# px/frame before the speed scale. Used only to show the operator what a
# scale setting means in km/h.
MEAN_CAR_BASE_SPEED_PX_PER_FRAME = 1.2

bus_routes_config = {
    "R1_EB_A_NB": {
        "name": "EB \u2192 Node A (NB)",
        "origin": "EB",
        "destination": "NODE_A_NB",
        "waypoints": {NODE_A_X: "LEFT"},
        "lanes": {NODE_A_X: 2},
        "active": True,
        "headway_sec": 30,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False,
        "stops": [{"node": NODE_A_X, "side": "far"}]
    },
    "R2_EB_B_NB": {
        "name": "EB \u2192 Node B (NB)",
        "origin": "EB",
        "destination": "NODE_B_NB",
        "waypoints": {NODE_A_X: "STRAIGHT", NODE_B_X: "LEFT"},
        "lanes": {NODE_A_X: 1, NODE_B_X: 2},
        "active": True,
        "headway_sec": 45,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False,
        "stops": [{"node": NODE_A_X, "side": "far"}]
    },
    "R3_EB_ONLY": {
        "name": "EB Corridor (Straight)",
        "origin": "EB",
        "destination": "EB_CORRIDOR",
        "waypoints": {NODE_A_X: "STRAIGHT", NODE_B_X: "STRAIGHT"},
        "lanes": {NODE_A_X: 1, NODE_B_X: 1},
        "active": True,
        "headway_sec": 90,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False,
        "stops": [{"node": NODE_A_X, "side": "far"}]
    },
    "R4_WB_A_SB": {
        "name": "WB \u2192 Node A (SB)",
        "origin": "WB",
        "destination": "NODE_A_SB",
        "waypoints": {NODE_B_X: "STRAIGHT", NODE_A_X: "LEFT"},
        "lanes": {NODE_B_X: 1, NODE_A_X: 2},
        "active": True,
        "headway_sec": 30,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False,
        "stops": [{"node": NODE_B_X, "side": "far"}]
    },
    "R5_WB_B_SB": {
        "name": "WB \u2192 Node B (SB)",
        "origin": "WB",
        "destination": "NODE_B_SB",
        "waypoints": {NODE_B_X: "LEFT"},
        "lanes": {NODE_B_X: 2},
        "active": True,
        "headway_sec": 45,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False,
        "stops": [{"node": NODE_B_X, "side": "far"}]
    },
    "R6_WB_ONLY": {
        "name": "WB Corridor (Straight)",
        "origin": "WB",
        "destination": "WB_CORRIDOR",
        "waypoints": {NODE_B_X: "STRAIGHT", NODE_A_X: "STRAIGHT"},
        "lanes": {NODE_B_X: 1, NODE_A_X: 1},
        "active": False,
        "headway_sec": 90,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False,
        "stops": [{"node": NODE_B_X, "side": "far"}]
    }
}

# Turning movements per approach. ``turn_split`` is the straight share;
# ``left_far_share`` is the share taking the approach's SECOND left option
# (EB/WB: the left at the far node; A_SB/B_NB: a left at the first node and
# another at the second); the remainder takes the first left option. A_NB
# and B_SB meet one node and have no second option.
approach_configs = {
    "EB":   {"active": True,  "model": "Binomial", "rate": 34, "turn_split": 0.75, "left_far_share": 0.11, "heavy_ratio": 0.10},
    "WB":   {"active": True,  "model": "Binomial", "rate": 32, "turn_split": 0.80, "left_far_share": 0.10, "heavy_ratio": 0.10},
    "A_NB": {"active": True,  "model": "Poisson", "rate": 24,  "turn_split": 0.75, "left_far_share": 0.00, "heavy_ratio": 0.15},
    "A_SB": {"active": True,  "model": "Poisson", "rate": 27,  "turn_split": 0.75, "left_far_share": 0.10, "heavy_ratio": 0.15},
    "B_NB": {"active": True,  "model": "Poisson", "rate": 25,  "turn_split": 0.75, "left_far_share": 0.10, "heavy_ratio": 0.15},
    "B_SB": {"active": True,  "model": "Poisson", "rate": 22,  "turn_split": 0.75, "left_far_share": 0.00, "heavy_ratio": 0.15},
}

# Per approach: the caption for each turning segment, and the nodes each
# left option turns at, in travel order (first option, then second). One
# entry means a single left option; the spawner and the turn bar both read
# this so a new option never has to be wired twice.
APPROACH_TURN_OPTIONS = {
    "EB":   (("Left @A", (NODE_A_X,)), ("Left @B", (NODE_B_X,))),
    "WB":   (("Left @B", (NODE_B_X,)), ("Left @A", (NODE_A_X,))),
    # From the south at A a left heads west and leaves; from the south at B
    # it heads west toward A, where it can turn left again to leave south of
    # A. Mirrored from the north.
    "A_NB": (("Left @A", (NODE_A_X,)),),
    "B_NB": (("Left @B", (NODE_B_X,)), ("Left @B→A", (NODE_B_X, NODE_A_X))),
    "B_SB": (("Left @B", (NODE_B_X,)),),
    "A_SB": (("Left @A", (NODE_A_X,)), ("Left @A→B", (NODE_A_X, NODE_B_X))),
}

# Inflow demand per approach, vehicles per minute. 0 means no arrivals
# (main.should_spawn_vehicle returns early on a non-positive rate).
INFLOW_MIN_VPM = 0
INFLOW_MAX_VPM = 60

# Arrival-generation models selectable per approach (see main.py spawner).
ARRIVAL_MODELS = (
    "Random",
    "Poisson",
    "Binomial",
    "Neg Binomial",
    "Congestion Peak",
)

APPROACH_NAMES = {
    "EB": "EB Corridor",
    "WB": "WB Corridor",
    "A_NB": "Node A (NB)",
    "A_SB": "Node A (SB)",
    "B_NB": "Node B (NB)",
    "B_SB": "Node B (SB)"
}


def repaint_route_flag_buttons():
    """Repaint TSP/DBL chips from the authoritative route configuration."""
    for route_id, buttons in route_flag_buttons.items():
        config = bus_routes_config.get(route_id, {})
        paint_toggle_chip(
            buttons["tsp"], bool(config.get("tsp_enabled", False)),
            COLOR_SUCCESS, "TSP ON", "TSP OFF",
        )
        paint_toggle_chip(
            buttons["dbl"], bool(config.get("dbl_enabled", False)),
            COLOR_ACCENT, "DBL ON", "DBL OFF",
        )


def get_ollama_models():
    """Return locally installed Ollama tags, with safe offline fallbacks."""
    fallback = [
        "None",
        "gemma4:12b",
        "llama3.1:8b",
        "deepseek-r1:32b",
        "qwen3.6:latest",
    ]
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return fallback
        names = [
            line.split()[0]
            for line in result.stdout.strip().splitlines()[1:]
            if line.split()
        ]
        return ["None"] + list(dict.fromkeys(names))
    except Exception:
        return fallback


def get_api_models():
    """Return API models only for providers configured in the environment."""
    models = ["None"]
    for environment_variable, model_ids in API_MODEL_REGISTRY.items():
        if os.environ.get(environment_variable):
            models.extend(model_ids)
    return models


def _effective_tick_seconds():
    """The decision interval actually in force for the agent subprocess.

    A Benchmark Test or Batch Benchmark run (``test_running``, set by
    ``request_start_test()`` for both) uses the Batch Run card's own slider;
    everything else (manual Single Run control) uses the Single Run card's
    slider. Reading this instead of always using ``ai_runtime`` keeps the two
    sliders genuinely independent -- running a batch never overwrites the
    operator's Single Run setting, and a batch sweep is never silently
    governed by whatever that slider happens to show.
    """
    if global_config.get("test_running", False):
        batch_runtime = global_config.get("batch_runtime", DEFAULT_BATCH_RUNTIME)
        return min(TICK_SECONDS_MAX, max(TICK_SECONDS_MIN, int(batch_runtime.get("tick_seconds", DEFAULT_TICK_SECONDS))))
    return min(TICK_SECONDS_MAX, max(TICK_SECONDS_MIN, int(global_config["ai_runtime"].get("tick_seconds", DEFAULT_TICK_SECONDS))))


def write_ai_control(path=None):
    """Atomically mirror in-process AI controls for the agent subprocess."""
    runtime = global_config["ai_runtime"]
    payload = {
        "armed": bool(runtime.get("armed", False)),
        "model": str(runtime.get("model", "None")),
        "control_mode": str(runtime.get("control_mode", CONTROL_MODE_ASSISTED)),
        "tick_seconds": _effective_tick_seconds(),
        "simulation_running": bool(global_config.get("is_running", False)),
    }
    destination = AI_CONTROL_PATH if path is None else Path(path)
    temp_name = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            delete=False,
        ) as temp_file:
            json.dump(payload, temp_file, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_name = Path(temp_file.name)
        os.replace(temp_name, destination)
        return True
    except Exception:
        if temp_name and temp_name.exists():
            try:
                temp_name.unlink()
            except OSError:
                pass
        runtime["last_status"] = "CONTROL_WRITE_ERROR"
        return False


# Armed, but no model chosen: the run is an unaided baseline, so the panel
# must not imply a decision is on its way.
BASELINE_NO_MODEL = "BASELINE_NO_MODEL"
# The deterministic non-LLM comparator. It is chosen through the same model
# selector and written to the same ai_control.json field, because it produces
# the same decision through the same path -- only the decider differs.
RULE_BASED_MODEL = "rule-based"
# Max-pressure TSP gate: grant when the bus approach's passenger pressure
# beats the cross street's (rule_controller.max_pressure_decision).
MAX_PRESSURE_MODEL = "passenger-pressure-tsp"
# Every non-LLM decider: no inference latency, no sampling, no tokens.
NON_LLM_MODELS = (RULE_BASED_MODEL, MAX_PRESSURE_MODEL)

# Control strategy: the operator-facing family a decider belongs to. The
# selector is two-level (strategy, then the concrete decider within it);
# ai_runtime["model"] still carries the one backend-neutral model string.
STRATEGY_BASELINE = "Baseline"
STRATEGY_RULE = "Rule-Based"
# The two LLM strategies are the two control modes: Assisted requests
# TSP/DBL per route over Webster, Decided writes the timing plan and the
# lane-2 flashers itself (ai_runtime["control_mode"] "configured").
STRATEGY_LLM_ASSISTED = "AI/LLM Assisted"
STRATEGY_LLM_DECIDED = "AI/LLM Decided"
LLM_STRATEGIES = (STRATEGY_LLM_ASSISTED, STRATEGY_LLM_DECIDED)
CONTROL_STRATEGIES = (
    STRATEGY_BASELINE, STRATEGY_RULE, STRATEGY_LLM_ASSISTED, STRATEGY_LLM_DECIDED,
)
STRATEGY_HINTS = {
    STRATEGY_BASELINE: "Webster timing only -- no TSP/DBL decisions",
    STRATEGY_RULE: "Deterministic TSP/DBL rule, no model",
    STRATEGY_LLM_ASSISTED: "LLM requests TSP/DBL per route; Webster keeps the timing",
    STRATEGY_LLM_DECIDED: "LLM writes green lengths, cuts and lane-2 flashers; Webster only as fallback",
}

CONTROL_MODE_ASSISTED = "assisted"
CONTROL_MODE_CONFIGURED = "configured"
CONFIGURED_TICK_SECONDS = 10   # a plan is re-issued whole; give the model room
# Decision interval bounds. The upper bound must admit one decision per
# signal cycle (~50 s here) and a tick above the slowest model's p95 latency:
# the agent skips-and-counts any grid point that comes due while a call is
# still running, and main.py holds all-off once a decision is older than
# 3 x tick, so a tick below the latency measures a disconnected loop.
TICK_SECONDS_MIN = 2
TICK_SECONDS_MAX = 120
# Batch arm label for an LLM run in the Decided strategy: "<model> [decided]".
CONFIGURED_ARM_SUFFIX = " [decided]"
STRATEGY_CONTROL_MODE = {
    STRATEGY_LLM_DECIDED: CONTROL_MODE_CONFIGURED,
}


def strategy_for(model, control_mode=CONTROL_MODE_ASSISTED):
    """Which control strategy a model string runs under ``control_mode``."""
    if model in ("None", BATCH_BASELINE_LABEL):
        return STRATEGY_BASELINE
    if model in (RULE_BASED_MODEL, MAX_PRESSURE_MODEL):
        return STRATEGY_RULE
    return STRATEGY_LLM_DECIDED if control_mode == CONTROL_MODE_CONFIGURED else STRATEGY_LLM_ASSISTED


def strategy_of(label):
    """Which control strategy a selector/batch label belongs to."""
    return strategy_for(*split_arm_label(label))


def is_llm_strategy(strategy):
    return strategy in LLM_STRATEGIES


def split_arm_label(label):
    """(model, control_mode) from a selector/batch label."""
    label = str(label or "None")
    if label.endswith(CONFIGURED_ARM_SUFFIX):
        return label[: -len(CONFIGURED_ARM_SUFFIX)], CONTROL_MODE_CONFIGURED
    return label, CONTROL_MODE_ASSISTED


def strategy_models(strategy):
    """Concrete deciders selectable under one strategy, first = default.
    Empty for AI/LLM when nothing is installed or keyed."""
    if strategy == STRATEGY_BASELINE:
        return ["None"]
    if strategy == STRATEGY_RULE:
        return [RULE_BASED_MODEL, MAX_PRESSURE_MODEL]
    if not is_llm_strategy(strategy):
        return []
    return [
        m for m in list(get_ollama_models()) + list(get_api_models())
        if m != "None" and m not in NON_LLM_MODELS
    ]


def get_decision_sources():
    """Local decision sources: the non-LLM comparators plus installed Ollama
    tags.

    None of them is a model, but they are selected like one so that a
    comparator run and a model run differ in nothing except who decides.
    """
    models = list(get_ollama_models())
    insert_at = 1 if models and models[0] == "None" else 0
    for name in reversed(NON_LLM_MODELS):
        if name not in models:
            models.insert(insert_at, name)
    return models


def set_active_ai_model(model, other_selector=None, persist=True, control_mode=None):
    """Select exactly one local/API model and persist the shared model ID.

    ``control_mode`` (or a "[configured]" label suffix) selects AI Configured;
    only an AI/LLM-Based decider can run it, everything else is assisted.
    """
    selected_model, label_mode = split_arm_label(model)
    if other_selector is not None:
        other_selector.set("None")
    runtime = global_config["ai_runtime"]
    runtime["model"] = selected_model
    mode = control_mode or label_mode
    if not is_llm_strategy(strategy_for(selected_model)):
        mode = CONTROL_MODE_ASSISTED
    runtime["control_mode"] = mode
    if runtime.get("armed", False):
        runtime["last_status"] = (
            BASELINE_NO_MODEL
            if selected_model == "None"
            else "MODEL_CHANGED_WAITING"
        )
    if persist:
        write_ai_control()
    return selected_model


def get_batch_model_choices():
    """Every decision source selectable as a Batch Benchmark Runner
    condition: the explicit baseline, the rule comparator, every installed
    Ollama tag, and any API model whose provider key is present in the
    environment (get_api_models() already gates on that)."""
    local_choices = [model for model in get_decision_sources() if model != "None"]
    api_choices = [model for model in get_api_models() if model != "None"]
    choices = [BATCH_BASELINE_LABEL] + local_choices + api_choices
    # Every LLM once more as an AI/LLM Decided arm, so one queue can mix them.
    return choices + [
        model + CONFIGURED_ARM_SUFFIX for model in choices
        if is_llm_strategy(strategy_of(model))
    ]


def request_start_batch(models, seeds):
    """Queue a Batch Benchmark Runner sweep: every (model, seed) pair at the
    currently configured duration, chained through the same
    request_start_test single-run path. Regime (demand, headways, speed
    scale, eligibility) is left exactly as the operator calibrated it -- the
    batch changes only seed and model between runs. Returns the queued run
    count, or None if the request is invalid or a batch is already active.
    """
    if not global_config.get("test_duration_sim_seconds"):
        return None
    models = list(dict.fromkeys(models or []))
    seeds = list(dict.fromkeys(seeds or []))
    if not models or not seeds:
        return None
    runtime = global_config.setdefault("batch_runtime", dict(DEFAULT_BATCH_RUNTIME))
    if runtime.get("active", False):
        return None
    total = len(models) * len(seeds)
    runtime.update({
        "active": True,
        "models": models,
        "seeds": seeds,
        "total": total,
        "current": None,
        "results": [],
    })
    global_config["batch_start_requested"] = True
    return total


def request_stop_batch():
    """STOP BATCH: pause the sweep in place -- the run in flight freezes and
    no further run starts until RESUME; END discards the sweep."""
    global_config["batch_stop_requested"] = True


def request_resume_batch():
    global_config["batch_resume_requested"] = True


def request_end_batch():
    """END: abandon the run in flight and the queue, clear the batch state
    and perform the same full reset as RESET (main.poll_batch_runner)."""
    global_config["batch_end_requested"] = True


def create_dashboard_window(parent=None):
    """Build every control-panel widget and return their container.

    With no `parent`, this owns its own top-level window exactly as it always
    has (a standalone `tk.Tk()`, title, geometry, close handling). Passed a
    parent -- a frame inside the unified MainWindow -- it builds the same
    widgets into that frame instead and leaves window chrome (title,
    geometry, "-topmost", WM_DELETE_WINDOW) to whatever owns the real
    top-level window, since only one thing may own that per process.
    `global_config` and `write_ai_control()` behave identically either way;
    the agent subprocess depends on `ai_control.json` and nothing here
    changes what gets written to it.

    Layout: one portrait column of collapsible cards in ``SECTION_ORDER``,
    each built by ``make_section`` so every header, row, button and toggle
    shares the same type scale and spacing grid. Every card starts collapsed
    so the full column is visible at launch.
    """
    route_flag_buttons.clear()
    control_panel_sections.clear()
    root = tk.Tk() if parent is None else parent
    is_toplevel = isinstance(root, (tk.Tk, tk.Toplevel))
    if is_toplevel:
        root.title("Traffic & Transit Control Dashboard")
        root.geometry(f"{CONTROL_PANEL_WIDTH}x1030")
        # Use normal desktop stacking. Forced topmost made the Pygame canvas
        # slide underneath this window and also made focus/drag interaction
        # feel sticky.
        root.attributes("-topmost", False)
    root.configure(bg=COLOR_BG)
    write_ai_control()

    panel_fit_after_id = None
    recurring_after_ids = set()
    panel_destroyed = False

    def schedule_panel_callback(delay_ms, callback):
        if panel_destroyed:
            return None
        holder = {}

        def run_callback():
            recurring_after_ids.discard(holder["id"])
            if not panel_destroyed:
                callback()

        holder["id"] = root.after(delay_ms, run_callback)
        recurring_after_ids.add(holder["id"])
        return holder["id"]

    def cancel_panel_callbacks(event=None):
        nonlocal panel_destroyed, panel_fit_after_id
        if event is not None and event.widget is not root:
            return
        panel_destroyed = True
        if panel_fit_after_id is not None:
            root.after_cancel(panel_fit_after_id)
            panel_fit_after_id = None
        for after_id in tuple(recurring_after_ids):
            root.after_cancel(after_id)
        recurring_after_ids.clear()

    root.bind("<Destroy>", cancel_panel_callbacks, add="+")

    def fit_panel_to_visible_content():
        """Resize only the height after a disclosure section changes state.

        Only meaningful when this panel owns its own top-level window. Mounted
        inside a MainWindow pane, the pane's own scrollable wrapper (a Canvas
        bound to the frame's <Configure> event) already tracks this same
        content-size change; this panel has no window to resize.
        """
        nonlocal panel_fit_after_id
        panel_fit_after_id = None
        root.update_idletasks()
        if not is_toplevel:
            return
        width = root.winfo_width()
        if width <= 1:
            width = CONTROL_PANEL_WIDTH
        x_pos = max(0, root.winfo_x())
        y_pos = max(0, root.winfo_y())
        height = bounded_control_panel_height(
            root.winfo_reqheight(), root.winfo_screenheight(), y_pos
        )
        root.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

    def schedule_panel_fit():
        nonlocal panel_fit_after_id
        if panel_fit_after_id is not None:
            root.after_cancel(panel_fit_after_id)
        panel_fit_after_id = root.after_idle(fit_panel_to_visible_content)

    def on_close():
        global_config["is_running"] = False
        cancel_panel_callbacks()
        try:
            root.destroy()
        except Exception:
            pass
        sys.exit()

    if is_toplevel:
        root.protocol("WM_DELETE_WINDOW", on_close)

    style = ttk.Style()
    if style.theme_use() != 'clam':
        style.theme_use('clam')

    style.configure(
        "Modern.TCombobox",
        fieldbackground=COLOR_CARD_ALT,
        background=COLOR_CARD_BORDER,
        foreground=COLOR_TEXT_PRIMARY,
        darkcolor=COLOR_CARD_BORDER,
        lightcolor=COLOR_CARD_BORDER,
        bordercolor=COLOR_CARD_BORDER,
        arrowcolor=COLOR_TEXT_SECONDARY,
        padding=(SPACE_SM, SPACE_XS),
        font=FONT_BODY,
    )
    style.map(
        "Modern.TCombobox",
        fieldbackground=[("readonly", COLOR_CARD_ALT)],
        bordercolor=[("focus", COLOR_ACCENT)],
        lightcolor=[("focus", COLOR_ACCENT)],
        darkcolor=[("focus", COLOR_ACCENT)],
    )
    root.option_add("*TCombobox*Listbox.font", FONT_BODY)

    # clam's default scrollbar is grey on the dark card: give the model
    # picker one with a visible trough and thumb.
    style.configure(
        "Modern.Vertical.TScrollbar",
        troughcolor=COLOR_CARD_ALT, background=COLOR_CARD_BORDER,
        bordercolor=COLOR_CARD_ALT, arrowcolor=COLOR_TEXT_SECONDARY,
        lightcolor=COLOR_CARD_BORDER, darkcolor=COLOR_CARD_BORDER, width=12,
    )
    style.map(
        "Modern.Vertical.TScrollbar",
        background=[("active", COLOR_TEXT_SECONDARY), ("pressed", COLOR_ACCENT)],
    )

    # One slider look for the whole panel; a focused slider swaps its bevel
    # to amber so the operator can see which control the arrow keys drive.
    # "Thin" is the same slider at two thirds the height, for the paired
    # percentage controls in the approach cards.
    for style_name, background, slider_thickness, slider_length, groove in (
        ("Modern.Horizontal.TScale", COLOR_CARD_ALT, 14, 22, 4),
        ("Global.Horizontal.TScale", COLOR_CARD, 14, 22, 4),
        ("Thin.Horizontal.TScale", COLOR_CARD_ALT, 10, 16, 3),
    ):
        style.configure(
            style_name,
            background=background,
            troughcolor="#141822",
            slidercolor=COLOR_ACCENT,
            bordercolor=COLOR_CARD_BORDER,
            lightcolor=COLOR_ACCENT,
            darkcolor=COLOR_ACCENT,
            groovethickness=groove,
            sliderthickness=slider_thickness,
            sliderlength=slider_length,
        )
        style.map(
            style_name,
            lightcolor=[("focus", COLOR_WARNING)],
            darkcolor=[("focus", COLOR_WARNING)],
            bordercolor=[("focus", COLOR_WARNING)],
        )

    # 1. HEADER & STATUS ---------------------------------------------------
    header_frame = tk.Frame(root, bg=COLOR_BG)
    header_frame.pack(fill="x", padx=PAGE_GUTTER, pady=(SPACE_MD, SPACE_XS))

    # Title on the left, the window-shape cycle button on the right.
    title_row = tk.Frame(header_frame, bg=COLOR_BG)
    title_row.pack(fill="x")
    make_label(
        title_row, "Network Control", font=FONT_TITLE,
        color=COLOR_TEXT_PRIMARY,
    ).pack(side="left", fill="x", expand=True)
    window_shape_btn = make_button(
        title_row,
        WINDOW_SHAPE_BUTTON_LABELS[global_config.get("window_shape", "compact")],
        "neutral", request_window_shape_cycle, padx=SPACE_SM, pady=CHIP_PAD_Y,
    )
    window_shape_btn.pack(side="right")

    def refresh_window_shape_button():
        label = WINDOW_SHAPE_BUTTON_LABELS.get(
            global_config.get("window_shape", "compact"),
            WINDOW_SHAPE_BUTTON_LABELS["compact"],
        )
        if window_shape_btn.cget("text") != label:
            window_shape_btn.config(text=label)
        schedule_panel_callback(250, refresh_window_shape_button)

    schedule_panel_callback(250, refresh_window_shape_button)

    # Status sits under the title: beside it, the title alone fills a
    # portrait-width pane and the status text gets clipped.
    status_badge = tk.Frame(header_frame, bg=COLOR_BG)
    status_badge.pack(fill="x", pady=(SPACE_XS, 0))

    dot_lbl = make_label(status_badge, SYM_DOT, color=COLOR_TEXT_SECONDARY)
    dot_lbl.pack(side="left", padx=(0, SPACE_XS))
    status_text = make_label(
        status_badge, "Idle — press START", bold=True,
        color=COLOR_TEXT_SECONDARY, wraplength=PORTRAIT_WRAP_LENGTH,
    )
    status_text.pack(side="left", fill="x", expand=True)

    # Cards are built in operator order: two configuration editors first,
    # then the merged Single Run and Batch Run workflows (amber), followed
    # by the two intervention cards (red). Every card starts collapsed so
    # the whole column is visible at launch and the operator opens only the
    # card they are working in.
    # 1. PER-APPROACH TRAFFIC ----------------------------------------------
    approaches_section = make_section(
        root, "Approach Traffic", accent=SECTION_ACCENT_CONFIG,
        expanded=False, on_toggle=schedule_panel_fit,
    )
    approaches_body = approaches_section["body"]

    # One item card per approach. Row 1 = name + ON/OFF chip, row 2 = the
    # generation model + inflow stepper, row 3 = the turning-movement split
    # bar, row 4 = trucks %.
    for key, name in APPROACH_NAMES.items():
        inner = make_item_card(approaches_body)

        name_row = tk.Frame(inner, bg=COLOR_CARD_ALT)
        name_row.pack(fill="x", pady=(0, ROW_GAP))
        make_label(name_row, name, bold=True).pack(side="left", fill="x", expand=True)

        def make_toggle(k, btn):
            def toggle():
                approach_configs[k]["active"] = not approach_configs[k]["active"]
                paint_toggle_chip(
                    btn, approach_configs[k]["active"], COLOR_SUCCESS, "ON", "OFF"
                )
            return toggle

        t_btn = make_toggle_chip(
            name_row, approach_configs[key]["active"], COLOR_SUCCESS, "ON", "OFF",
            width=4,
        )
        t_btn.config(command=make_toggle(key, t_btn))
        t_btn.pack(side="right")

        # Row 2: model selector | inflow stepper. Inflow is an exact integer,
        # so a stepper (typed or arrowed, 0-60 v/m) fits it better than a
        # slider and takes a third of the width.
        demand_row = tk.Frame(inner, bg=COLOR_CARD_ALT)
        demand_row.pack(fill="x", pady=(0, ROW_GAP))
        model_box = ttk.Combobox(
            demand_row,
            values=list(ARRIVAL_MODELS),
            width=max(len(model) for model in ARRIVAL_MODELS) - 3,
            state="readonly",
            style="Modern.TCombobox", font=FONT_BODY,
        )
        model_box.set(approach_configs[key]["model"])
        model_box.pack(side="left", fill="x", expand=True)

        def make_model_change(k, box):
            def change(event):
                approach_configs[k]["model"] = box.get()
            return change
        model_box.bind("<<ComboboxSelected>>", make_model_change(key, model_box))

        make_label(demand_row, "v/m", color=COLOR_TEXT_SECONDARY).pack(
            side="right", padx=(SPACE_XS, 0)
        )
        rate_box = make_spinbox(
            demand_row, INFLOW_MIN_VPM, INFLOW_MAX_VPM, approach_configs[key]["rate"]
        )
        rate_box.pack(side="right", padx=(SPACE_SM, 0))

        def make_rate_change(k, box):
            def apply(commit):
                text = box.get().strip()
                if text.lstrip("-").isdigit():
                    value = max(INFLOW_MIN_VPM, min(INFLOW_MAX_VPM, int(text)))
                    approach_configs[k]["rate"] = value
                elif commit:
                    value = int(approach_configs[k]["rate"])  # junk: restore
                else:
                    return  # mid-edit: leave both field and config alone
                if str(value) != text:
                    box.delete(0, "end")
                    box.insert(0, str(value))
            return apply

        bind_spinbox_changes(rate_box, make_rate_change(key, rate_box))

        # Row 3: turning movements as one 100 % bar -- Straight | first left
        # option | second left option (where the approach has one) -- with a
        # thumb at each boundary. Row 4: trucks %.
        options = APPROACH_TURN_OPTIONS[key]
        cfg = approach_configs[key]
        straight = int(round(cfg["turn_split"] * 100))
        far = int(round(cfg.get("left_far_share", 0.0) * 100)) if len(options) > 1 else 0
        segments = [straight, 100 - straight - far] + ([far] if len(options) > 1 else [])

        def make_split_change(k):
            def update(parts):
                approach_configs[k]["turn_split"] = parts[0] / 100.0
                approach_configs[k]["left_far_share"] = (
                    parts[2] / 100.0 if len(parts) > 2 else 0.0
                )
            return update

        add_split_bar_row(
            inner, ["Straight", *(label for label, _nodes in options)], segments,
            make_split_change(key),
        )

        def make_heavy_slider(k, lbl):
            def update(val):
                v_f = float(val) / 100.0
                approach_configs[k]["heavy_ratio"] = v_f
                lbl.config(text=f"{int(v_f*100)}%")
            return update

        heavy_val, heavy_slider = add_slider_row(
            inner, "Trucks", f"{int(approach_configs[key]['heavy_ratio']*100)}%",
            0, 50, int(approach_configs[key]["heavy_ratio"] * 100), None, step=1,
            style="Thin.Horizontal.TScale", pady=0,
        )
        heavy_slider.config(command=make_heavy_slider(key, heavy_val))

    # 2. BUS ROUTES (TSP / DBL) --------------------------------------------
    transit_section = make_section(
        root, "Bus Routes", accent=SECTION_ACCENT_CONFIG, expanded=False,
        on_toggle=schedule_panel_fit,
    )
    transit_body = transit_section["body"]

    # Route selector over its action: abreast, the column leaves the
    # selector too narrow to show a route name.
    dispatch_row = tk.Frame(transit_body, bg=COLOR_CARD)
    dispatch_row.pack(fill="x", pady=(0, SPACE_SM))

    disp_route_box = ttk.Combobox(
        dispatch_row, values=[r["name"] for r in bus_routes_config.values()],
        state="readonly", style="Modern.TCombobox", font=FONT_BODY,
    )
    disp_route_box.set(bus_routes_config["R1_EB_A_NB"]["name"])
    disp_route_box.pack(fill="x", pady=(0, ROW_GAP))

    def trigger_manual_dispatch():
        selected_name = disp_route_box.get()
        for r_id, r_cfg in bus_routes_config.items():
            if r_cfg["name"] == selected_name:
                if r_cfg["active"]:
                    r_cfg["manual_dispatch"] = True
                break

    manual_btn = make_button(
        dispatch_row, "Dispatch now", "warning", trigger_manual_dispatch
    )
    manual_btn.pack(fill="x")

    # One item card per route. Row 1 = name + ON/OFF chip, row 2 = headway
    # slider, row 3 = the TSP and DBL chips as two equal halves. Every
    # callback and bus_routes_config key is the same as before.
    for r_id, r_cfg in bus_routes_config.items():
        inner = make_item_card(transit_body)

        name_row = tk.Frame(inner, bg=COLOR_CARD_ALT)
        name_row.pack(fill="x", pady=(0, ROW_GAP))
        make_label(name_row, r_cfg["name"], bold=True).pack(
            side="left", fill="x", expand=True
        )

        def make_route_toggle(key, btn):
            def toggle():
                bus_routes_config[key]["active"] = not bus_routes_config[key]["active"]
                act = bus_routes_config[key]["active"]
                paint_toggle_chip(btn, act, COLOR_SUCCESS, "ON", "OFF")
                if not act:
                    bus_routes_config[key]["manual_dispatch"] = False
            return toggle

        t_btn = make_toggle_chip(
            name_row, r_cfg["active"], COLOR_SUCCESS, "ON", "OFF", width=4
        )
        t_btn.config(command=make_route_toggle(r_id, t_btn))
        t_btn.pack(side="right")

        def make_hw_slider(key, lbl):
            def update(val):
                sec = int(float(val))
                bus_routes_config[key]["headway_sec"] = sec
                lbl.config(text=f"{sec}s" if sec > 0 else "OFF")
            return update

        hw_val_lbl, hw_slider = add_slider_row(
            inner, "Headway", f"{r_cfg['headway_sec']}s", 0, MAX_HEADWAY_SEC,
            r_cfg["headway_sec"], None, step=1, style="Modern.Horizontal.TScale",
        )
        hw_slider.config(command=make_hw_slider(r_id, hw_val_lbl))

        flags_row = tk.Frame(inner, bg=COLOR_CARD_ALT)
        flags_row.pack(fill="x")
        flags_row.grid_columnconfigure(0, weight=1, uniform="route_flags")
        flags_row.grid_columnconfigure(1, weight=1, uniform="route_flags")

        def make_tsp_toggle(key, btn):
            def toggle():
                bus_routes_config[key]["tsp_enabled"] = not bus_routes_config[key]["tsp_enabled"]
                paint_toggle_chip(
                    btn, bus_routes_config[key]["tsp_enabled"],
                    COLOR_SUCCESS, "TSP ON", "TSP OFF",
                )
            return toggle

        tsp_btn = make_toggle_chip(
            flags_row, r_cfg["tsp_enabled"], COLOR_SUCCESS, "TSP ON", "TSP OFF"
        )
        tsp_btn.config(command=make_tsp_toggle(r_id, tsp_btn))
        tsp_btn.grid(row=0, column=0, sticky="ew", padx=(0, SPACE_XS))

        def make_dbl_toggle(key, btn):
            def toggle():
                bus_routes_config[key]["dbl_enabled"] = not bus_routes_config[key]["dbl_enabled"]
                paint_toggle_chip(
                    btn, bus_routes_config[key]["dbl_enabled"],
                    COLOR_ACCENT, "DBL ON", "DBL OFF",
                )
            return toggle

        dbl_btn = make_toggle_chip(
            flags_row, r_cfg["dbl_enabled"], COLOR_ACCENT, "DBL ON", "DBL OFF"
        )
        dbl_btn.config(command=make_dbl_toggle(r_id, dbl_btn))
        dbl_btn.grid(row=0, column=1, sticky="ew", padx=(SPACE_XS, 0))
        route_flag_buttons[r_id] = {"tsp": tsp_btn, "dbl": dbl_btn}

    # 3. SINGLE RUN: AI / LLM + RUN CONTROLS -------------------------------
    # These controls operate on the same live episode, so they share one
    # disclosure card. Subsection headings retain the old visual landmarks
    # without requiring the operator to coordinate two separate cards.
    single_run_section = make_section(
        root, "Single Run", accent=SECTION_ACCENT_RUN, expanded=False,
        on_toggle=schedule_panel_fit,
    )
    ai_body = single_run_section["body"]
    make_subsection_heading(ai_body, "Control Strategy", first=True)

    selected_model = str(global_config["ai_runtime"].get("model", "None"))
    selected_strategy = strategy_for(
        selected_model, global_config["ai_runtime"].get("control_mode", CONTROL_MODE_ASSISTED)
    )
    if selected_model not in strategy_models(selected_strategy):
        selected_model = "None"
        selected_strategy = STRATEGY_BASELINE
        global_config["ai_runtime"]["model"] = selected_model

    # Level 1: the strategy family. Level 2: the decider within it, greyed
    # out when the family has only one (Baseline, ML) so the row still
    # tells the operator what will run.
    strategy_row = add_labeled_row(ai_body, "Strategy", pady=(0, SPACE_XS))
    strategy_box = ttk.Combobox(
        strategy_row, values=list(CONTROL_STRATEGIES), state="readonly",
        style="Modern.TCombobox", font=FONT_BODY,
        # width=1: fill="x" sizes it; the 20-char default overruns the pane.
        width=1,
    )
    strategy_box.set(selected_strategy)
    strategy_box.pack(side="left", fill="x", expand=True)

    strategy_hint_lbl = make_label(
        ai_body, STRATEGY_HINTS[selected_strategy], color=COLOR_TEXT_SECONDARY,
        wraplength=PORTRAIT_WRAP_LENGTH, justify="left", anchor="w",
    )
    strategy_hint_lbl.pack(fill="x", pady=(0, ROW_GAP))

    decider_row = add_labeled_row(ai_body, "Decider")
    decider_box = ttk.Combobox(
        decider_row, state="readonly", style="Modern.TCombobox", font=FONT_BODY,
        width=1,
    )
    decider_box.pack(side="left", fill="x", expand=True)

    def show_decider_choices(strategy, model):
        choices = strategy_models(strategy)
        decider_box.config(
            values=choices or ["(none installed)"],
            state="readonly" if len(choices) > 1 else "disabled",
        )
        decider_box.set(
            model if model in choices else (choices[0] if choices else "(none installed)")
        )
        strategy_hint_lbl.config(text=STRATEGY_HINTS[strategy])

    show_decider_choices(selected_strategy, selected_model)

    def on_strategy_selected(event):
        strategy = strategy_box.get()
        choices = strategy_models(strategy)
        model = choices[0] if choices else "None"
        show_decider_choices(strategy, model)
        mode = STRATEGY_CONTROL_MODE.get(strategy, CONTROL_MODE_ASSISTED)
        selected_val_lbl.config(text=set_active_ai_model(model, persist=False, control_mode=mode))
        runtime = global_config["ai_runtime"]
        if mode == CONTROL_MODE_CONFIGURED and int(runtime.get("tick_seconds", DEFAULT_TICK_SECONDS)) < CONFIGURED_TICK_SECONDS:
            tick_slider.set(CONFIGURED_TICK_SECONDS)
        write_ai_control()

    strategy_box.bind("<<ComboboxSelected>>", on_strategy_selected)

    def on_decider_selected(event):
        selected_val_lbl.config(text=set_active_ai_model(
            decider_box.get(), persist=False,
            control_mode=STRATEGY_CONTROL_MODE.get(strategy_box.get(), CONTROL_MODE_ASSISTED),
        ))
        write_ai_control()

    decider_box.bind("<<ComboboxSelected>>", on_decider_selected)

    def on_run_llm():
        runtime = global_config["ai_runtime"]
        runtime["armed"] = not runtime.get("armed", False)
        if not runtime["armed"]:
            runtime["last_status"] = "INACTIVE"
        elif str(runtime.get("model", "None")) == "None":
            # Armed with no model selected: there is no decision to wait for,
            # the network simply runs unaided.
            runtime["last_status"] = BASELINE_NO_MODEL
        else:
            runtime["last_status"] = "WAITING_FOR_DECISION"
        if runtime["armed"]:
            runtime["last_turn"] = 0
        write_ai_control()

    tick_runtime = global_config["ai_runtime"]

    def on_tick_seconds_changed(value):
        tick_seconds = min(TICK_SECONDS_MAX, max(TICK_SECONDS_MIN, int(float(value))))
        global_config["ai_runtime"]["tick_seconds"] = tick_seconds
        tick_value_lbl.config(text=f"{tick_seconds}s")
        write_ai_control()

    tick_value_lbl, tick_slider = add_slider_row(
        ai_body, "Decision interval",
        f"{int(tick_runtime.get('tick_seconds', DEFAULT_TICK_SECONDS))}s",
        TICK_SECONDS_MIN, TICK_SECONDS_MAX, tick_runtime.get("tick_seconds", DEFAULT_TICK_SECONDS),
        on_tick_seconds_changed,
        step=1, style="Global.Horizontal.TScale",
    )

    # Arm button, then the live status on its own line under it: a status
    # such as "LLM WAITING_FOR_DECISION | TURN 3" needs the full column.
    run_llm_btn = make_button(ai_body, f"{SYM_PLAY}  Arm strategy", "warning", on_run_llm)
    run_llm_btn.pack(fill="x", pady=(SPACE_XS, ROW_GAP))

    ai_row_status = tk.Frame(ai_body, bg=COLOR_CARD)
    ai_row_status.pack(fill="x", pady=(0, SPACE_SM))
    llm_dot = make_label(ai_row_status, SYM_DOT, color=COLOR_TEXT_SECONDARY)
    llm_dot.pack(side="left", padx=(0, SPACE_XS))
    llm_status_text = make_label(
        ai_row_status, "STRATEGY INACTIVE", bold=True, color=COLOR_TEXT_SECONDARY,
        wraplength=PORTRAIT_WRAP_LENGTH - SPACE_LG,
    )
    llm_status_text.pack(side="left", fill="x", expand=True)

    # Selected model and control scope, one line each.
    selected_row = add_labeled_row(ai_body, "Selected")
    selected_val_lbl = make_label(
        selected_row, selected_model, bold=True, color=COLOR_ACCENT,
        wraplength=PORTRAIT_WRAP_LENGTH - 80,
    )
    selected_val_lbl.pack(side="left", fill="x", expand=True)

    scope_row = add_labeled_row(ai_body, "Scope", pady=0)
    ctrl_val_lbl = make_label(
        scope_row, "TSP + DBL, all bus routes", bold=True, color=COLOR_ACCENT,
        wraplength=PORTRAIT_WRAP_LENGTH - 80,
    )
    ctrl_val_lbl.pack(side="left", fill="x", expand=True)

    def refresh_llm_status():
        runtime = global_config.get("ai_runtime", {})
        armed = bool(runtime.get("armed", False))
        status = str(runtime.get("last_status", "INACTIVE"))
        turn = int(runtime.get("last_turn", 0))
        if not armed:
            color = COLOR_TEXT_SECONDARY
            status_text_value = "STRATEGY INACTIVE"
            button_text = f"{SYM_PLAY}  Arm strategy"
        elif status == BASELINE_NO_MODEL:
            color = COLOR_ACCENT
            status_text_value = "Baseline - no decisions"
            button_text = f"{SYM_STOP}  Disarm strategy"
        else:
            color = {
                "OK": COLOR_SUCCESS,
                "HELD_ALL_OFF": COLOR_DANGER,
                "INVALID_DECISION": COLOR_DANGER,
                "CONTROL_WRITE_ERROR": COLOR_DANGER,
            }.get(status, COLOR_WARNING)
            family = strategy_for(
                runtime.get("model"), runtime.get("control_mode", CONTROL_MODE_ASSISTED)
            ).upper()
            status_text_value = f"{family} {status}"
            if turn:
                status_text_value += f" | TURN {turn}"
            button_text = f"{SYM_STOP}  Disarm strategy"
        llm_dot.config(fg=color)
        llm_status_text.config(text=status_text_value, fg=color)
        run_llm_btn.config(text=button_text)
        schedule_panel_callback(250, refresh_llm_status)

    schedule_panel_callback(250, refresh_llm_status)

    def refresh_route_buttons():
        repaint_route_flag_buttons()
        schedule_panel_callback(250, refresh_route_buttons)

    schedule_panel_callback(250, refresh_route_buttons)

    # Run lifecycle, speed, seed, and Webster output belong to this same
    # single-run workflow. Keep their existing callbacks and state owners.
    make_subsection_heading(ai_body, "Run Controls")
    run_body = ai_body

    # START/PAUSE share a row, RESET gets its own full-width row below -- a
    # narrow side pane has no room for all three abreast.
    controls_row = tk.Frame(run_body, bg=COLOR_CARD)
    controls_row.pack(fill="x", pady=(0, SPACE_SM))
    controls_row.grid_columnconfigure(0, weight=1, uniform="run_buttons")
    controls_row.grid_columnconfigure(1, weight=1, uniform="run_buttons")

    def paint_calibrating():
        """Repaint START as "Calibrating…" and flush it to the screen.

        START's first job in main.py is a synchronous saturation-flow
        measurement; the Tk loop is busy for its whole duration, so the
        100 ms status poller below cannot repaint until it ends. Painting
        here, in the click handler, is the only way the operator sees the
        button change the moment they press it.
        """
        start_stop_btn.config(
            text=f"{SYM_HOURGLASS}  Calibrating…", bg=COLOR_WARNING, fg=COLOR_BG,
            activebackground=COLOR_WARNING, activeforeground=COLOR_BG,
        )
        dot_lbl.config(fg=COLOR_WARNING)
        status_text.config(text="Calibrating signal timing…", fg=COLOR_WARNING)
        root.update_idletasks()

    def toggle_start_stop():
        if request_start_stop() == "START_REQUESTED":
            paint_calibrating()
        write_ai_control()

    start_stop_btn = make_button(
        controls_row, f"{SYM_PLAY}  Start", "success", toggle_start_stop
    )
    start_stop_btn.grid(row=0, column=0, sticky="ew", padx=(0, SPACE_XS))

    def toggle_pause():
        request_pause_resume()

    pause_btn = make_button(
        controls_row, f"{SYM_PAUSE}  Pause", "neutral", toggle_pause
    )
    pause_btn.grid(row=0, column=1, sticky="ew", padx=(SPACE_XS, 0))

    def trigger_reset():
        global_config["reset_triggered"] = True

    reset_btn = make_button(
        run_body, f"{SYM_RESET}  Reset vehicles", "neutral", trigger_reset,
        fg=COLOR_DANGER, activeforeground=COLOR_DANGER,
    )
    reset_btn.pack(fill="x", pady=(0, SPACE_MD))

    def update_speed(val):
        v = float(val)
        global_config["sim_speed"] = v
        speed_val_lbl.config(text=f"{v:.1f}x")

    speed_val_lbl, speed_slider = add_slider_row(
        run_body, "Sim speed", "1.0x", 0.5, 3.0, 1.0, update_speed,
        step=0.1, style="Global.Horizontal.TScale",
    )

    # Seed: caption line (label left, seed in effect right) over the entry
    # and its Set button, the same shape as a slider row.
    seed_caption = tk.Frame(run_body, bg=COLOR_CARD)
    seed_caption.pack(fill="x")
    make_label(seed_caption, "Seed", color=COLOR_TEXT_SECONDARY).pack(side="left")
    # Persistent readout of the seed actually in effect, so the operator can
    # confirm it at a glance instead of relying on the transient status line.
    seed_state_lbl = make_label(seed_caption, "", bold=True, color=COLOR_SUCCESS, anchor="e")
    seed_state_lbl.pack(side="right")

    seed_row = tk.Frame(run_body, bg=COLOR_CARD)
    seed_row.pack(fill="x", pady=(0, ROW_GAP))
    seed_entry = tk.Entry(
        seed_row, width=8, font=FONT_BODY,
        bg=COLOR_CARD_ALT, fg=COLOR_TEXT_PRIMARY,
        insertbackground=COLOR_TEXT_PRIMARY, relief="flat",
        highlightthickness=1, highlightbackground=COLOR_CARD_BORDER,
        highlightcolor=COLOR_ACCENT,
    )
    configured_seed = global_config.get("random_seed")
    if configured_seed is not None:
        seed_entry.insert(0, str(configured_seed))
    seed_entry.pack(side="left", fill="x", expand=True, padx=(0, SPACE_XS), ipady=3)

    def apply_seed_from_entry(_event=None):
        try:
            seed = set_random_seed(seed_entry.get())
        except (TypeError, ValueError):
            status_text.config(text="Seed must be an integer", fg=COLOR_DANGER)
            return
        refresh_seed_state_label()
        if seed is None:
            status_text.config(
                text="Random seed cleared; applies on reset", fg=COLOR_TEXT_SECONDARY
            )
        else:
            status_text.config(
                text=f"Seed {seed} set; applies on reset", fg=COLOR_SUCCESS
            )

    seed_btn = make_button(
        seed_row, "Set", "neutral", apply_seed_from_entry,
        padx=SPACE_MD, pady=CHIP_PAD_Y,
    )
    seed_btn.pack(side="left")

    def refresh_seed_state_label():
        seed = global_config.get("random_seed")
        if seed is None:
            seed_state_lbl.config(text="random", fg=COLOR_TEXT_SECONDARY)
        else:
            seed_state_lbl.config(text=f"set to {seed}", fg=COLOR_SUCCESS)

    refresh_seed_state_label()
    seed_entry.bind("<Return>", apply_seed_from_entry)
    seed_entry.bind("<FocusOut>", apply_seed_from_entry)

    # Webster signal timing (measured capacity, per-node condition, cycle
    # length, green split and demand ratio) is rendered by the telemetry
    # dashboard's Summary tab instead of here -- it is observed output, not
    # an operator input, so it belongs next to the live signal state it
    # describes. get_webster_timing_summary() remains the shared source.

    # 4. BATCH RUN: ONE BENCHMARK + BATCH SWEEP ----------------------------
    batch_run_section = make_section(
        root, "Batch Run", accent=SECTION_ACCENT_RUN, expanded=False,
        on_toggle=schedule_panel_fit,
    )
    test_body = batch_run_section["body"]
    make_subsection_heading(test_body, "Benchmark Test", first=True)

    # Caption line (label left, countdown right) over the duration selector,
    # the same shape as a slider row.
    duration_caption = tk.Frame(test_body, bg=COLOR_CARD)
    duration_caption.pack(fill="x")
    make_label(duration_caption, "Duration", color=COLOR_TEXT_SECONDARY).pack(side="left")
    countdown_lbl = make_label(
        duration_caption, "--:--", bold=True, color=COLOR_TEXT_SECONDARY,
        # Reserve the longest runtime state ("00:00 PAUSED") up front. Without
        # this width, Tk sizes the benchmark card for "--:--" and clips the
        # countdown when the label grows after a test starts.
        width=BENCHMARK_COUNTDOWN_WIDTH, anchor="e",
    )
    countdown_lbl.pack(side="right")
    test_duration_box = ttk.Combobox(
        test_body, values=list(TEST_DURATIONS),
        state="readonly", style="Modern.TCombobox", font=FONT_BODY,
    )
    test_duration_box.pack(fill="x", pady=(0, ROW_GAP))
    for label, seconds in TEST_DURATIONS.items():
        if seconds == global_config.get("test_duration_sim_seconds"):
            test_duration_box.set(label)

    # Own decision-interval slider, independent of the Single Run card's:
    # governs the single Benchmark Test below and every queued Batch
    # Benchmark run, so a batch sweep's cadence never depends on whatever
    # the Single Run panel happens to be set to.
    batch_runtime_config = global_config.setdefault(
        "batch_runtime", dict(DEFAULT_BATCH_RUNTIME)
    )

    def on_batch_tick_seconds_changed(value):
        tick_seconds = min(TICK_SECONDS_MAX, max(TICK_SECONDS_MIN, int(float(value))))
        global_config.setdefault(
            "batch_runtime", dict(DEFAULT_BATCH_RUNTIME)
        )["tick_seconds"] = tick_seconds
        batch_tick_value_lbl.config(text=f"{tick_seconds}s")

    batch_tick_value_lbl, batch_tick_slider = add_slider_row(
        test_body, "Decision interval",
        f"{int(batch_runtime_config.get('tick_seconds', DEFAULT_TICK_SECONDS))}s",
        TICK_SECONDS_MIN, TICK_SECONDS_MAX, batch_runtime_config.get("tick_seconds", DEFAULT_TICK_SECONDS),
        on_batch_tick_seconds_changed,
        step=1, style="Global.Horizontal.TScale",
    )

    def refresh_test_countdown():
        if global_config.get("test_running", False):
            remaining = format_test_countdown(
                global_config.get("test_duration_sim_seconds"),
                global_config.get("sim_time_seconds", 0.0),
            )
            if remaining is not None:
                paused = bool(global_config.get("is_paused", False))
                countdown_lbl.config(
                    text=f"{remaining} PAUSED" if paused else remaining,
                    fg=COLOR_WARNING if paused else COLOR_SUCCESS,
                )
            else:
                countdown_lbl.config(text="--:--", fg=COLOR_TEXT_SECONDARY)
        elif global_config.get("test_last_export"):
            countdown_lbl.config(text="00:00", fg=COLOR_ACCENT)
        else:
            countdown_lbl.config(text="--:--", fg=COLOR_TEXT_SECONDARY)
        schedule_panel_callback(200, refresh_test_countdown)

    schedule_panel_callback(200, refresh_test_countdown)

    def on_test_duration_selected(_event=None):
        global_config["test_duration_sim_seconds"] = TEST_DURATIONS.get(
            test_duration_box.get()
        )
        refresh_batch_preview()

    test_duration_box.bind("<<ComboboxSelected>>", on_test_duration_selected)

    # No separate "Start test" button: a single timed run is a batch of one
    # model x one seed, and going through the batch means it gets the same
    # export, summary row and pairing as every other arm. The batch still
    # chains request_start_test() for each run.

    # --- Batch Benchmark Runner -------------------------------------------
    # Chains request_start_test() across every (model x seed) combination at
    # this same duration, unattended. Regime (demand/headway/speed) is never
    # touched here -- only seed and model vary between queued runs. Kept as
    # one model/seed already selected elsewhere in the panel.
    make_subsection_heading(test_body, "Batch Benchmark")
    batch_body = test_body

    seed_list_row = add_labeled_row(batch_body, "Seeds")
    batch_seed_entry = tk.Entry(
        seed_list_row, font=FONT_BODY, bg=COLOR_CARD_ALT, fg=COLOR_TEXT_PRIMARY,
        insertbackground=COLOR_TEXT_PRIMARY, relief="flat",
    )
    batch_seed_entry.pack(side="left", fill="x", expand=True)

    batch_seed_status_lbl = make_label(
        batch_body, "e.g. 42,43,44 or 42-46 or 42,45-47,50",
        color=COLOR_TEXT_SECONDARY, wraplength=PORTRAIT_WRAP_LENGTH,
    )
    batch_seed_status_lbl.pack(fill="x", pady=(0, SPACE_XS))

    # Selections persist while the panel stays open, per the spec.
    batch_selected_models = []

    model_picker_row = tk.Frame(batch_body, bg=COLOR_CARD)
    model_picker_row.pack(fill="x", pady=(0, SPACE_XS))

    model_count_lbl = make_label(
        model_picker_row, "0 selected", color=COLOR_TEXT_SECONDARY, anchor="e"
    )

    def open_model_picker():
        choices = get_batch_model_choices()
        picker = tk.Toplevel(root)
        picker.title("Select batch models")
        picker.configure(bg=COLOR_CARD)
        picker.transient(root)

        # Keep the chooser a stable size and scroll its checkbox list. Model
        # registries grow with installed Ollama models and configured API
        # providers; the operator must never need to resize this dialog just
        # to reach the last model or the Done button.
        picker_width = 340
        list_height = min(320, max(140, len(choices) * 30 + 24 * len(CONTROL_STRATEGIES)))
        picker.geometry(f"{picker_width}x{list_height + 72}")

        list_frame = tk.Frame(picker, bg=COLOR_CARD)
        list_frame.pack(fill="both", expand=True, padx=SPACE_MD, pady=(SPACE_MD, 0))
        model_canvas = tk.Canvas(
            list_frame, bg=COLOR_CARD, highlightthickness=0, bd=0,
        )
        model_scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=model_canvas.yview,
            style="Modern.Vertical.TScrollbar",
        )
        model_canvas.configure(yscrollcommand=model_scrollbar.set)
        model_canvas.pack(side="left", fill="both", expand=True)
        model_scrollbar.pack(side="right", fill="y")
        choices_frame = tk.Frame(model_canvas, bg=COLOR_CARD)
        choices_window = model_canvas.create_window(
            (0, 0), window=choices_frame, anchor="nw"
        )

        def update_scroll_region(_event=None):
            model_canvas.configure(scrollregion=model_canvas.bbox("all"))

        def fit_choices_width(event):
            model_canvas.itemconfigure(choices_window, width=event.width)

        def scroll_models(event):
            delta = -1 if event.delta > 0 else 1
            model_canvas.yview_scroll(delta, "units")
            return "break"

        choices_frame.bind("<Configure>", update_scroll_region)
        model_canvas.bind("<Configure>", fit_choices_width)
        model_canvas.bind("<MouseWheel>", scroll_models)
        choices_frame.bind("<MouseWheel>", scroll_models)

        vars_by_model = {}

        def add_choice(choice):
            var = tk.BooleanVar(value=choice in batch_selected_models)
            vars_by_model[choice] = var
            # The strategy heading above already says "AI/LLM DECIDED" --
            # repeating "[decided]" on every row under it is just noise.
            display_text = split_arm_label(choice)[0]
            checkbox = tk.Checkbutton(
                choices_frame, text=display_text, variable=var, anchor="w",
                bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY, selectcolor=COLOR_CARD_ALT,
                activebackground=COLOR_CARD, activeforeground=COLOR_TEXT_PRIMARY,
                font=FONT_BODY, highlightthickness=0,
            )
            checkbox.pack(fill="x", anchor="w", padx=(SPACE_SM, 0), pady=2)
            checkbox.bind("<MouseWheel>", scroll_models)

        # Same hierarchy as the Single Run card: one heading per strategy.
        for strategy in CONTROL_STRATEGIES:
            group = [c for c in choices if strategy_of(c) == strategy]
            if not group:
                continue
            make_label(
                choices_frame, strategy.upper(), bold=True,
                color=COLOR_TEXT_SECONDARY, anchor="w",
            ).pack(fill="x", pady=(SPACE_SM, 2))
            for choice in group:
                add_choice(choice)

        def apply_and_close():
            batch_selected_models[:] = [
                model for model, var in vars_by_model.items() if var.get()
            ]
            model_count_lbl.config(text=f"{len(batch_selected_models)} selected")
            refresh_batch_preview()
            picker.destroy()

        make_button(picker, "Done", "primary", apply_and_close).pack(
            fill="x", padx=SPACE_MD, pady=SPACE_MD
        )

    model_picker_btn = make_button(
        model_picker_row, "Select models…", "neutral", open_model_picker
    )
    model_picker_btn.pack(side="left")
    model_count_lbl.pack(side="right", fill="x", expand=True, padx=(SPACE_SM, 0))

    batch_preview_lbl = make_label(
        batch_body, "", color=COLOR_TEXT_SECONDARY,
        wraplength=PORTRAIT_WRAP_LENGTH, justify="left",
    )
    batch_preview_lbl.pack(fill="x", pady=(SPACE_XS, SPACE_XS))

    def batch_can_run():
        seeds, error = batch_runner.parse_seed_list(batch_seed_entry.get())
        return (
            not error and bool(seeds) and bool(batch_selected_models)
            and bool(global_config.get("test_duration_sim_seconds"))
        )

    def batch_checkpoint_marks():
        # Lazy import: main.py (the composition root) imports control_panel
        # at module load time, so importing main back here at call time --
        # long after both modules have finished loading -- reuses its single
        # source of truth for the checkpoint schedule without a load-time
        # circular import.
        try:
            from src.core import main
            return main.CHECKPOINT_MARKS_SEC
        except Exception:
            return batch_runner.DEFAULT_CHECKPOINT_MARKS_SEC

    def refresh_batch_preview(*_args):
        seeds, error = batch_runner.parse_seed_list(batch_seed_entry.get())
        if error:
            batch_seed_status_lbl.config(text=error, fg=COLOR_DANGER)
            seeds = []
        else:
            batch_seed_status_lbl.config(
                text=batch_runner.describe_seeds(seeds), fg=COLOR_TEXT_SECONDARY
            )
        duration = global_config.get("test_duration_sim_seconds")
        preview = batch_runner.compute_batch_preview(
            model_count=len(batch_selected_models),
            seed_count=len(seeds),
            duration_sim_seconds=duration,
            duration_label=test_duration_box.get(),
            checkpoint_marks_sec=batch_checkpoint_marks(),
            sim_speed=global_config.get("sim_speed", 1.0),
        )
        batch_preview_lbl.config(text=preview["text"])
        if not global_config.get("batch_runtime", {}).get("active", False):
            set_batch_button_enabled(
                run_batch_btn, batch_can_run(), COLOR_WARNING, COLOR_BG
            )

    batch_seed_entry.bind("<KeyRelease>", refresh_batch_preview)

    batch_buttons_row = tk.Frame(batch_body, bg=COLOR_CARD)
    batch_buttons_row.pack(fill="x", pady=(0, SPACE_XS))

    def on_run_batch():
        seeds, error = batch_runner.parse_seed_list(batch_seed_entry.get())
        if error or not seeds or not batch_selected_models:
            return
        request_start_batch(list(batch_selected_models), seeds)
        refresh_batch_preview()

    run_batch_btn = make_button(
        batch_buttons_row, "Run batch", "warning", on_run_batch
    )
    run_batch_btn.pack(side="left", fill="x", expand=True, padx=(0, SPACE_XS))

    # The stop slot: one "Stop batch" button while the sweep runs; once
    # stopped it splits down the middle into Resume | End.
    stop_slot = tk.Frame(batch_buttons_row, bg=COLOR_CARD)
    stop_slot.pack(side="left", fill="x", expand=True)
    stop_batch_btn = make_button(stop_slot, "Stop batch", "danger", request_stop_batch)
    stop_batch_btn.pack(fill="x")
    split_row = tk.Frame(stop_slot, bg=COLOR_CARD)
    split_row.grid_columnconfigure(0, weight=1, uniform="stop_split")
    split_row.grid_columnconfigure(1, weight=1, uniform="stop_split")
    resume_batch_btn = make_button(split_row, "Resume", "success", request_resume_batch)
    resume_batch_btn.grid(row=0, column=0, sticky="ew", padx=(0, 1))
    end_batch_btn = make_button(split_row, "End", "danger", request_end_batch)
    end_batch_btn.grid(row=0, column=1, sticky="ew", padx=(1, 0))

    def show_stop_slot(paused):
        if paused and not split_row.winfo_manager():
            stop_batch_btn.pack_forget()
            split_row.pack(fill="x")
        elif not paused and split_row.winfo_manager():
            split_row.pack_forget()
            stop_batch_btn.pack(fill="x")

    # tk.Button's own "disabled" state leaves the filled warning/danger
    # background in place and only swaps the text colour, which reads as
    # grey-on-amber / grey-on-red -- unreadable. Repaint bg+fg explicitly to
    # a neutral card colour when disabled instead, the same way pause_btn
    # and start_stop_btn already do above.
    def set_batch_button_enabled(button, enabled, on_bg, on_fg):
        if enabled:
            button.config(
                state="normal", bg=on_bg, fg=on_fg,
                activebackground=on_bg, activeforeground=on_fg,
            )
        else:
            button.config(
                state="disabled", bg=COLOR_CARD_ALT, fg=COLOR_TEXT_SECONDARY,
                activebackground=COLOR_CARD_ALT, activeforeground=COLOR_TEXT_SECONDARY,
            )

    set_batch_button_enabled(run_batch_btn, False, COLOR_WARNING, COLOR_BG)
    set_batch_button_enabled(stop_batch_btn, False, COLOR_DANGER, COLOR_TEXT_PRIMARY)

    batch_progress_lbl = make_label(
        batch_body, "", bold=True, color=COLOR_TEXT_SECONDARY,
        wraplength=PORTRAIT_WRAP_LENGTH,
    )
    batch_progress_lbl.pack(fill="x", pady=(SPACE_XS, 0))

    # width=1: a Text's default 80-character request would otherwise be the
    # widest thing in the column and force the whole card past the pane;
    # fill="x" below stretches it to the card instead.
    batch_log_text = tk.Text(
        batch_body, width=1, height=5, bg=COLOR_CARD_ALT, fg=COLOR_TEXT_PRIMARY,
        font=FONT_BODY, relief="flat", state="disabled", wrap="word",
        highlightthickness=0, bd=0,
    )
    batch_log_text.pack(fill="x", pady=(SPACE_XS, 0))

    def refresh_batch_status():
        runtime = global_config.get("batch_runtime", DEFAULT_BATCH_RUNTIME)
        active = bool(runtime.get("active", False))
        paused = active and bool(runtime.get("paused", False))
        current = runtime.get("current")
        total = int(runtime.get("total", 0))
        results = runtime.get("results", [])

        show_stop_slot(paused)
        set_batch_button_enabled(stop_batch_btn, active, COLOR_DANGER, COLOR_TEXT_PRIMARY)
        set_batch_button_enabled(
            run_batch_btn, (not active) and batch_can_run(), COLOR_WARNING, COLOR_BG
        )

        if active and current:
            batch_progress_lbl.config(
                text=(
                    f"Run {len(results) + 1}/{total}: {current.get('model')} "
                    f"seed {current.get('seed')} — {'stopped' if paused else 'running…'}"
                ),
                fg=COLOR_WARNING,
            )
        elif active:
            batch_progress_lbl.config(
                text=f"Batch: {len(results)}/{total} complete", fg=COLOR_WARNING
            )
        elif results:
            completed = sum(1 for row in results if row.get("status") == "COMPLETED")
            failed = sum(1 for row in results if row.get("status") == "FAILED")
            skipped = sum(1 for row in results if row.get("status") == "SKIPPED")
            batch_progress_lbl.config(
                text=(
                    f"Batch finished: {completed} completed, "
                    f"{failed} failed, {skipped} skipped"
                ),
                fg=COLOR_SUCCESS,
            )
        else:
            batch_progress_lbl.config(text="", fg=COLOR_TEXT_SECONDARY)

        batch_log_text.config(state="normal")
        batch_log_text.delete("1.0", "end")
        for row in results[-30:]:
            line = f"[{row.get('status')}] {row.get('model')} seed {row.get('seed')}"
            if row.get("reason"):
                line += f" — {row['reason']}"
            batch_log_text.insert("end", line + "\n")
        batch_log_text.config(state="disabled")

        schedule_panel_callback(300, refresh_batch_status)

    schedule_panel_callback(300, refresh_batch_status)
    refresh_batch_preview()

    def refresh_simulation_status():
        running = bool(global_config.get("is_running", False))
        starting = bool(global_config.get("start_requested", False))
        calibrating = bool(global_config.get("calibrating", False))
        paused = bool(global_config.get("is_paused", False))
        testing = bool(global_config.get("test_running", False))
        if starting or calibrating:
            # START is measuring saturation flow; nothing else has begun.
            paint_calibrating()
            pause_btn.config(
                state="disabled", text=f"{SYM_PAUSE}  Pause",
                bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY,
            )
        elif running:
            start_stop_btn.config(
                text=f"{SYM_STOP}  Stop", bg=COLOR_DANGER,
                fg=COLOR_TEXT_PRIMARY, activeforeground=COLOR_TEXT_PRIMARY,
            )
            pause_btn.config(state="normal")
            if paused:
                pause_btn.config(
                    text=f"{SYM_PLAY}  Resume", bg=COLOR_SUCCESS, fg=COLOR_BG
                )
                dot_lbl.config(fg=COLOR_WARNING)
                status_text.config(text="Paused", fg=COLOR_WARNING)
            elif testing:
                pause_btn.config(
                    text=f"{SYM_PAUSE}  Pause", bg=COLOR_CARD,
                    fg=COLOR_TEXT_PRIMARY,
                )
                dot_lbl.config(fg=COLOR_SUCCESS)
                duration = global_config.get("test_duration_sim_seconds")
                duration_label = next(
                    (
                        label
                        for label, seconds in TEST_DURATIONS.items()
                        if seconds == duration
                    ),
                    f"{int(duration) // 60} min" if duration else "?",
                )
                status_text.config(
                    text=(
                        f"Test running: {global_config.get('test_model', 'None')}"
                        f" for {duration_label}"
                    ),
                    fg=COLOR_SUCCESS,
                )
            else:
                pause_btn.config(
                    text=f"{SYM_PAUSE}  Pause", bg=COLOR_CARD,
                    fg=COLOR_TEXT_PRIMARY,
                )
                dot_lbl.config(fg=COLOR_SUCCESS)
                status_text.config(text="Running", fg=COLOR_SUCCESS)
        else:
            start_stop_btn.config(
                text=f"{SYM_PLAY}  Start", bg=COLOR_SUCCESS,
                fg=COLOR_BG, activeforeground=COLOR_BG,
            )
            pause_btn.config(
                state="disabled", text=f"{SYM_PAUSE}  Pause",
                bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY,
            )
            dot_lbl.config(fg=COLOR_TEXT_SECONDARY)
            if global_config.get("test_last_export"):
                status_text.config(
                    text=(
                        "Test complete — exported "
                        f"{global_config['test_last_export']}"
                    ),
                    fg=COLOR_SUCCESS,
                )
            elif global_config.get("run_has_started", False):
                status_text.config(
                    text="Stopped — export or START new run",
                    fg=COLOR_TEXT_SECONDARY,
                )
            else:
                status_text.config(
                    text="Idle — press START", fg=COLOR_TEXT_SECONDARY
                )
        refresh_seed_state_label()
        schedule_panel_callback(100, refresh_simulation_status)

    schedule_panel_callback(100, refresh_simulation_status)

    # 5. MOTION / PRIORITY TUNING -----------------------------------------
    tuning_section = make_section(
        root, "Tuning", accent=SECTION_ACCENT_INTERVENTION, expanded=False,
        on_toggle=schedule_panel_fit,
    )
    tuning_body = tuning_section["body"]

    # Both knobs are stored in sim units (px, a speed multiplier) but an
    # operator reasons in metres and km/h, so each shows the physical value
    # as its readout and keeps the sim unit in a caption underneath. The
    # conversion is real_world_units' single anchor, not a second constant.
    def update_priority_eligibility(value):
        pixels = set_priority_eligibility_px(value)
        eligibility_value.config(text=f"{units.px_to_m(pixels):.0f} m")
        eligibility_note.config(
            text=f"{pixels} px  ({units.meters_per_pixel():.2f} m/px)"
        )

    eligibility_value, eligibility_slider = add_slider_row(
        tuning_body, "Eligibility zone",
        f"{units.px_to_m(global_config['priority_eligibility_px']):.0f} m",
        PRIORITY_ELIGIBILITY_UI_MIN_PX, PRIORITY_ELIGIBILITY_UI_MAX_PX,
        global_config["priority_eligibility_px"],
        update_priority_eligibility, step=10, style="Global.Horizontal.TScale",
        pady=(0, 0),
    )
    eligibility_note = make_label(
        tuning_body,
        f"{global_config['priority_eligibility_px']} px  "
        f"({units.meters_per_pixel():.2f} m/px)",
        color=COLOR_TEXT_SECONDARY,
    )
    eligibility_note.pack(fill="x", pady=(0, ROW_GAP))

    def speed_readout(scale):
        """Mean car free-flow speed at this scale, and the px/frame it is.

        A car's desired speed is drawn uniform(1.0, 1.4) px/frame and
        multiplied by the scale (see main._draw_arrival), so the mean is
        1.2x -- the figure the calibration pins (0.5 -> ~32 km/h).
        """
        px_per_frame = MEAN_CAR_BASE_SPEED_PX_PER_FRAME * scale
        return (
            f"{units.px_per_frame_to_kmh(px_per_frame):.0f} km/h",
            f"{scale:.2f}x = {px_per_frame:.2f} px/frame "
            f"= {units.px_per_frame_to_kmh(px_per_frame) / 3.6:.1f} m/s",
        )

    def update_vehicle_scale(value):
        scale = set_vehicle_speed_scale(value)
        readout, note = speed_readout(scale)
        vehicle_scale_value.config(text=readout)
        vehicle_scale_note.config(text=note)

    _initial_readout, _initial_note = speed_readout(
        global_config["vehicle_speed_scale"]
    )
    vehicle_scale_value, vehicle_scale_slider = add_slider_row(
        tuning_body, "Vehicle speed", _initial_readout,
        0.25, 1.0, global_config["vehicle_speed_scale"],
        update_vehicle_scale, step=0.05, style="Global.Horizontal.TScale",
        pady=(0, 0),
    )
    vehicle_scale_note = make_label(
        tuning_body, _initial_note, color=COLOR_TEXT_SECONDARY
    )
    vehicle_scale_note.pack(fill="x", pady=(0, ROW_GAP))

    make_label(
        tuning_body, "Applies on next START / RESET", color=COLOR_TEXT_SECONDARY,
    ).pack(fill="x")

    # 6. NETWORK GRIDLOCK RECOVERY ----------------------------------------
    recovery_section = make_section(
        root, "Gridlock Discharge", accent=SECTION_ACCENT_INTERVENTION, expanded=False,
        on_toggle=schedule_panel_fit,
    )
    recovery_body = recovery_section["body"]

    # Label over the selector: "Auto (Recommended)" needs the full column.
    make_label(recovery_body, "Corridor", color=COLOR_TEXT_SECONDARY).pack(fill="x")
    discharge_mode_box = ttk.Combobox(
        recovery_body,
        values=list(DISCHARGE_OPTIONS),
        state="readonly",
        style="Modern.TCombobox", font=FONT_BODY,
    )
    discharge_mode_box.set(global_config["discharge_selection"])
    discharge_mode_box.pack(fill="x")

    # The two actions stack under the corridor selector: "Start discharge"
    # does not fit half of this column.
    recovery_actions = tk.Frame(recovery_body, bg=COLOR_CARD)
    recovery_actions.pack(fill="x", pady=(SPACE_XS, SPACE_SM))

    def on_discharge_mode_selected(event):
        global_config["discharge_selection"] = discharge_mode_box.get()

    discharge_mode_box.bind(
        "<<ComboboxSelected>>", on_discharge_mode_selected
    )

    def start_discharge():
        selected = discharge_mode_box.get()
        if selected not in DISCHARGE_OPTIONS:
            selected = DISCHARGE_AUTO
            discharge_mode_box.set(selected)
        global_config["discharge_selection"] = selected
        global_config["discharge_stop_requested"] = False
        global_config["discharge_start_requested"] = True
        runtime = global_config.setdefault("discharge_runtime", {})
        runtime.update(
            {
                "selected": selected,
                "status": "REQUESTED",
                "reason": "Waiting for the simulation controller",
                "recommendation": "Recovery will begin with a safe transition",
            }
        )

    start_discharge_btn = make_button(
        recovery_actions, "Start discharge", "danger", start_discharge
    )
    start_discharge_btn.pack(fill="x", pady=(0, ROW_GAP))

    def safe_stop_discharge():
        global_config["discharge_start_requested"] = False
        global_config["discharge_stop_requested"] = True

    stop_discharge_btn = make_button(
        recovery_actions, "Safe stop", "neutral", safe_stop_discharge
    )
    stop_discharge_btn.pack(fill="x")

    recovery_status = tk.Frame(recovery_body, bg=COLOR_CARD_ALT)
    recovery_status.pack(fill="x")
    discharge_status_lbl = make_label(
        recovery_status, "Selected: Auto (Recommended)  |  Status: IDLE",
        bold=True, color=COLOR_TEXT_SECONDARY, wraplength=PORTRAIT_WRAP_LENGTH,
    )
    discharge_status_lbl.pack(fill="x", padx=SPACE_SM, pady=(SPACE_XS, 0))
    discharge_reason_lbl = make_label(
        recovery_status, "Reason: Normal signal control is active",
        wraplength=PORTRAIT_WRAP_LENGTH,
    )
    discharge_reason_lbl.pack(fill="x", padx=SPACE_SM)
    discharge_recommendation_lbl = make_label(
        recovery_status,
        "Recommended first action: Select Auto or a corridor, then start discharge",
        color=COLOR_WARNING, wraplength=PORTRAIT_WRAP_LENGTH,
    )
    discharge_recommendation_lbl.pack(fill="x", padx=SPACE_SM, pady=(0, SPACE_XS))

    def refresh_discharge_status():
        runtime = global_config.get("discharge_runtime", {})
        selected = runtime.get(
            "selected", global_config.get("discharge_selection", DISCHARGE_AUTO)
        )
        status = runtime.get("status", "IDLE")
        stage = runtime.get("stage", "")
        reason = runtime.get("reason", "Normal signal control is active")
        recommendation = runtime.get(
            "recommendation", "Select Auto or a corridor, then start discharge"
        )
        discharged = int(runtime.get("vehicles_discharged", 0))
        status_color = {
            "DISCHARGING": COLOR_SUCCESS,
            "WAITING": COLOR_WARNING,
            "REQUESTED": COLOR_WARNING,
            "TRANSITIONING": COLOR_WARNING,
            "STOPPING": COLOR_WARNING,
            "COMPLETED": COLOR_SUCCESS,
        }.get(status, COLOR_TEXT_SECONDARY)
        status_text_value = f"Selected: {selected}  |  Status: {status}"
        if stage:
            status_text_value += f"  |  Stage: {stage}"
        if discharged:
            status_text_value += f"  |  Discharged: {discharged}"
        discharge_status_lbl.config(text=status_text_value, fg=status_color)
        discharge_reason_lbl.config(text=f"Reason: {reason}")
        discharge_recommendation_lbl.config(
            text=f"Recommended first action: {recommendation}"
        )
        schedule_panel_callback(250, refresh_discharge_status)

    schedule_panel_callback(250, refresh_discharge_status)

    # Collapsed sections keep every widget alive with its value and callback;
    # only the body frames are unmapped.
    schedule_panel_fit()
    return root
