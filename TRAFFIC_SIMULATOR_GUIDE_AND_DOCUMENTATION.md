# Traffic Simulator Guide and Source Documentation

**Purpose:** Explain the simulator architecture, runtime behavior, safety contracts, file responsibilities, and complete embedded Python source so a coding LLM can reconstruct or inspect the application from one document.

Companion documents:

- [Audit and Step-by-Step Fix Report](TRAFFIC_SIMULATOR_AUDIT_AND_STEP_BY_STEP_FIX_REPORT.md)
- [Gridlock Incident Report](TRAFFIC_SIMULATOR_GRIDLOCK_INCIDENT_REPORT.md)

## 1. General overview

This guide is a source-synchronized description of the traffic simulator. `main.py` is the executable entry point; the remaining files are imported modules except `telemetry_dashboard.py`, which `main.py` launches as a child process.

The network has Node A at x=300, Node B at x=700, horizontal center y=300, three 22-pixel lanes per direction, 132-pixel roads, and a 10-pixel stop offset. `canvas_gemini.py` is the authoritative geometry/rendering boundary.

Communication flow:

```text
control_panel configuration and callbacks
                |
                v
main fixed-step loop -> SignalController per-node state
                |             |
                v             v
          Vehicle / Bus movement and conflict reservations
                |
                +-> canvas rendering
                +-> atomic telemetry JSON -> telemetry dashboard
```

Safety contracts:

- Unknown, missing, RGB, or malformed signal values fail closed as RED in vehicle physics.
- Normal left turns reserve the conflict box and remain exclusive until their rear clears.
- DBL and TSP do not directly write colors. They submit a bus/route-leg/node/lane request to one arbiter per node.
- Priority transitions through conflict yellow, all-red clearance, exclusive approach green, rear-clear holding, and recovery all-red.
- Normal and priority green begin only after all-red has cleared the intersection box.
- During an active green, compatible same-axis traffic may enter continuously; it does not wait for the entire node rectangle to become empty.
- A long left turn protects only its genuinely shared adjacent corner lane, releasing following through or left-turn traffic as soon as that corner is clear and normal following can maintain separation.
- A request at Node A never changes Node B state, and vice versa.
- North/south vehicles remain pinned to the physical node whose vertical road they occupy; clearing one node cannot complete the remote node.
- DBL lane authority comes from the active route leg: through legs currently use lane 1 and left legs lane 2.
- Duplicate eligible frames preserve one priority request ID; timed-out requests become durable terminal denials and require a new eligibility edge before retry.
- Pending DBL/TSP is distinct from a granted or rear-clearing priority interval in telemetry, dashboard metrics, and DBL lamps.
- Dashboard trend history is process-local and bounded to 600 samples. Duplicate, paused, and stale snapshots do not add samples, and a backwards frame/time jump clears the previous run.
- The latest telemetry JSON remains a snapshot rather than JSONL; no time-series history is persisted to disk.
- Telemetry exports green, yellow, all-red, and nominal-cycle frame counts so the dashboard phase diagram stays synchronized with the controller's configured timing.
- The phase-cycle background is the nominal plan. Its marker dots are the authoritative live states; blue indicates mixed approaches or node divergence during priority operation.
- The telemetry dashboard starts within the available screen, remains freely resizable, and provides vertical plus horizontal scrolling on both tabs; the wheel scrolls vertically and Shift+wheel scrolls horizontally.
- Runtime and telemetry paths are resolved from the source directory, not the caller's working directory.
- The control panel uses normal window stacking rather than forced topmost behavior, so the canvas can be raised or overlapped normally.
- Catch-up work is capped per Tk callback so a delayed simulation update does not make native window dragging unresponsive.
- Congestion Peak alternates 30 simulated seconds of oversaturated demand with 30 seconds of recovery for each selected approach.
- Peak demand is the greater of four times the rate-slider value or 90 vehicles/minute; recovery uses the configured slider value.
- Blocked congestion arrivals remain as an integer backlog, capped at 5,000 per source, and become vehicle objects only when the spawn boundary is safe.
- Reset and model changes clear congestion backlogs so stale demand cannot flood a restarted scenario.
- Network discharge suspends arrivals, pending-demand admission, automatic/manual bus dispatch, and DBL/TSP arbitration while recovery owns the signal schedule.
- Auto discharge ranks only currently safe movements, prioritizes downstream dependencies and larger queues, and reevaluates after every protected stage.
- Manual discharge selects the operator's recovery objective, but it waits and recommends another action when the requested conflict box or receiving lane is unavailable.
- EB recovery opens Node B before coordinating Node A; WB recovery opens Node A before coordinating Node B.
- Every discharge green is exclusive per node and begins only after yellow, minimum all-red, conflict-box clearance, and receiving-space validation.
- Safe Stop returns to normal timing only after yellow, minimum all-red, and both conflict boxes are empty.
- Collision avoidance, following distance, spillback prevention, and intersection reservations remain authoritative during discharge.
- The LLM controls are placeholders: model selection changes only a label and RUN LLM intentionally does nothing.

Run and verify:

```powershell
.\myenv\Scripts\python.exe main.py
.\myenv\Scripts\python.exe -m py_compile canvas_gemini.py control_panel.py main.py signal_controller.py telemetry_dashboard.py telemetry_exporter.py vehicle.py
.\myenv\Scripts\python.exe -m pytest -q
```

## 2. `canvas_gemini.py` — Network geometry and rendering

Purpose:

Defines the authoritative 1000x600 road geometry, semantic-signal-to-RGB boundary, stop bars, signal heads, DBL indicators, labels, and complete Pygame network rendering. It never decides vehicle eligibility or signal safety.

### Full source: `canvas_gemini.py`

```python
# canvas_gemini.py
import time

import pygame


# ==========================================================
# AUTHORITATIVE NETWORK GEOMETRY
# ==========================================================
WIDTH, HEIGHT = 1000, 600
CANVAS_HEIGHT = HEIGHT

LANE = 22
LANES = 3
ROAD_W = 2 * LANE * LANES  # 132 px
H_Y = 300
INT_X = [300, 700]  # Node A and Node B
STOP = 10


# ==========================================================
# VISUAL DESIGN
# ==========================================================
BG = (30, 80, 30)
ROAD = (50, 50, 50)

WHITE = (230, 230, 230)
LANE_GREY = (130, 130, 130)
YELLOW = (220, 180, 40)
KEEP_CLEAR = (210, 210, 0)
POLE = (160, 160, 160)
LABEL_TEXT = (200, 200, 200)

SIGNAL_COLORS = {
    "RED": (220, 50, 50),
    "YELLOW": (220, 180, 40),
    "GREEN": (50, 220, 50),
}

# Rendering fallback color.
RED = SIGNAL_COLORS["RED"]


# Monotonic flash clock state. Paused time is subtracted so the DBL flash
# resumes from the same phase instead of jumping to wall-clock phase.
paused_freeze_timestamp = 0.0
_dbl_pause_started = None
_dbl_total_paused = 0.0


def _resolve_signal_color(state):
    """Convert a semantic signal state to RGB, failing safely to red.

    RGB tuples/lists remain accepted for compatibility with existing tests and
    reusable callers, but production signal behavior uses semantic strings.
    """
    if isinstance(state, str):
        return SIGNAL_COLORS.get(state.upper(), RED)
    if isinstance(state, (tuple, list)) and len(state) in (3, 4):
        try:
            return tuple(int(component) for component in state)
        except (TypeError, ValueError):
            return RED
    return RED


def _get_dbl_flash_clock(is_paused):
    """Return elapsed monotonic animation time with paused duration removed."""
    global paused_freeze_timestamp, _dbl_pause_started, _dbl_total_paused

    now = time.monotonic()
    if is_paused:
        if _dbl_pause_started is None:
            _dbl_pause_started = now
        effective_time = _dbl_pause_started - _dbl_total_paused
    else:
        if _dbl_pause_started is not None:
            _dbl_total_paused += now - _dbl_pause_started
            _dbl_pause_started = None
        effective_time = now - _dbl_total_paused

    paused_freeze_timestamp = effective_time
    return effective_time


def dashed(surface, start, end, color, width=1):
    """Draw lane markings while keeping intersection conflict boxes clear."""
    dash = 10
    gap = 8
    x1, y1 = start
    x2, y2 = end

    if x1 == x2:
        y = y1
        while y < y2:
            in_intersection = H_Y - ROAD_W // 2 <= y <= H_Y + ROAD_W // 2
            if not in_intersection:
                pygame.draw.line(
                    surface,
                    color,
                    (x1, y),
                    (x1, min(y + dash, y2)),
                    width,
                )
            y += dash + gap
    else:
        x = x1
        while x < x2:
            in_intersection = any(
                cx - ROAD_W // 2 <= x <= cx + ROAD_W // 2 for cx in INT_X
            )
            if not in_intersection:
                pygame.draw.line(
                    surface,
                    color,
                    (x, y1),
                    (min(x + dash, x2), y1),
                    width,
                )
            x += dash + gap


def draw_cleared_medians(surface):
    """Draw center medians only outside the intersection boxes."""
    half = ROAD_W // 2

    pygame.draw.line(surface, YELLOW, (0, H_Y), (INT_X[0] - half, H_Y), 2)
    pygame.draw.line(
        surface,
        YELLOW,
        (INT_X[0] + half, H_Y),
        (INT_X[1] - half, H_Y),
        2,
    )
    pygame.draw.line(
        surface,
        YELLOW,
        (INT_X[1] + half, H_Y),
        (WIDTH, H_Y),
        2,
    )

    for cx in INT_X:
        pygame.draw.line(surface, YELLOW, (cx, 0), (cx, H_Y - half), 2)
        pygame.draw.line(surface, YELLOW, (cx, H_Y + half), (cx, HEIGHT), 2)


def draw_road_labels(screen, font):
    """Draw node and approach labels used to orient the live simulation."""
    half = ROAD_W // 2

    node_a = font.render("NODE A", True, (255, 255, 255))
    node_b = font.render("NODE B", True, (255, 255, 255))
    screen.blit(node_a, (INT_X[0] - 25, H_Y - 8))
    screen.blit(node_b, (INT_X[1] - 25, H_Y - 8))

    eb_label = font.render("EB Corridor -->", True, LABEL_TEXT)
    wb_label = font.render("<-- WB Corridor", True, LABEL_TEXT)
    screen.blit(eb_label, (20, H_Y - half - 20))
    screen.blit(wb_label, (WIDTH - 150, H_Y + half + 8))

    a_sb_label = font.render("A_SB |", True, LABEL_TEXT)
    a_nb_label = font.render("A_NB ^", True, LABEL_TEXT)
    screen.blit(a_sb_label, (INT_X[0] + half + 8, 20))
    screen.blit(a_nb_label, (INT_X[0] - half - 55, HEIGHT - 35))

    b_sb_label = font.render("B_SB |", True, LABEL_TEXT)
    b_nb_label = font.render("B_NB ^", True, LABEL_TEXT)
    screen.blit(b_sb_label, (INT_X[1] + half + 8, 20))
    screen.blit(b_nb_label, (INT_X[1] - half - 55, HEIGHT - 35))


def get_signal_light_center(x, y, facing):
    if facing == "EASTBOUND":
        return x - 6, y
    if facing == "WESTBOUND":
        return x + 6, y
    if facing == "NORTHBOUND":
        return x, y + 6
    if facing == "SOUTHBOUND":
        return x, y - 6
    return x, y


def draw_signal_head(screen, x, y, facing, state="RED"):
    """Draw one primary signal head from a semantic state or RGB value."""
    pygame.draw.rect(screen, POLE, (x - 2, y - 2, 4, 4))
    center_x, center_y = get_signal_light_center(x, y, facing)
    pygame.draw.circle(
        screen,
        _resolve_signal_color(state),
        (center_x, center_y),
        6,
    )


def draw_dbl_signal(screen, x, y, facing, state="INACTIVE", is_paused=False):
    """Draw DBL request state without presenting a pending grant as active."""
    flash_time = _get_dbl_flash_clock(is_paused)

    if isinstance(state, bool):
        state = "ACTIVE" if state else "INACTIVE"
    state = str(state).upper()

    if state == "ACTIVE":
        is_flash_on = int(flash_time * 10) % 2 == 0
        light_color = (0, 255, 120) if is_flash_on else (0, 60, 20)
    elif state == "CLEARING":
        is_flash_on = int(flash_time * 5) % 2 == 0
        light_color = (0, 200, 220) if is_flash_on else (0, 45, 50)
    elif state in ("REQUESTED", "TRANSITIONING"):
        light_color = (245, 158, 11)
    else:
        light_color = (0, 0, 0)

    center_x, center_y = get_signal_light_center(x, y, facing)

    if facing == "EASTBOUND":
        box_x, box_y = center_x, center_y - 16
    elif facing == "WESTBOUND":
        box_x, box_y = center_x, center_y + 16
    elif facing == "NORTHBOUND":
        box_x, box_y = center_x - 16, center_y
    elif facing == "SOUTHBOUND":
        box_x, box_y = center_x + 16, center_y
    else:
        box_x, box_y = center_x, center_y

    pygame.draw.circle(screen, (30, 30, 35), (box_x, box_y), 6)
    pygame.draw.circle(screen, light_color, (box_x, box_y), 4)
    pygame.draw.circle(screen, (200, 200, 200), (box_x, box_y), 6, 1)


def draw_intersection(
    screen,
    center_x,
    signal_states=None,
    dbl_states=None,
    is_paused=False,
):
    """Draw one intersection using safe semantic signal and DBL maps."""
    half = ROAD_W // 2
    states = signal_states if isinstance(signal_states, dict) else {}
    dbls = dbl_states if isinstance(dbl_states, dict) else {}

    pygame.draw.rect(
        screen,
        KEEP_CLEAR,
        (center_x - half, H_Y - half, ROAD_W, ROAD_W),
        2,
    )

    # Stop bars cover the correct approach carriageway half.
    pygame.draw.line(
        screen,
        WHITE,
        (center_x - half - STOP, H_Y - half),
        (center_x - half - STOP, H_Y),
        3,
    )
    pygame.draw.line(
        screen,
        WHITE,
        (center_x + half + STOP, H_Y),
        (center_x + half + STOP, H_Y + half),
        3,
    )
    pygame.draw.line(
        screen,
        WHITE,
        (center_x - half, H_Y + half + STOP),
        (center_x, H_Y + half + STOP),
        3,
    )
    pygame.draw.line(
        screen,
        WHITE,
        (center_x, H_Y - half - STOP),
        (center_x + half, H_Y - half - STOP),
        3,
    )

    eastbound_position = (center_x - half - STOP, H_Y - half - 12)
    westbound_position = (center_x + half + STOP, H_Y + half + 12)
    northbound_position = (center_x - half - 12, H_Y + half + STOP)
    southbound_position = (center_x + half + 12, H_Y - half - STOP)

    draw_signal_head(
        screen,
        eastbound_position[0],
        eastbound_position[1],
        "EASTBOUND",
        states.get("EB", "RED"),
    )
    draw_signal_head(
        screen,
        westbound_position[0],
        westbound_position[1],
        "WESTBOUND",
        states.get("WB", "RED"),
    )
    draw_signal_head(
        screen,
        northbound_position[0],
        northbound_position[1],
        "NORTHBOUND",
        states.get("NB", "RED"),
    )
    draw_signal_head(
        screen,
        southbound_position[0],
        southbound_position[1],
        "SOUTHBOUND",
        states.get("SB", "RED"),
    )

    draw_dbl_signal(
        screen,
        eastbound_position[0],
        eastbound_position[1],
        "EASTBOUND",
        dbls.get("EB", "INACTIVE"),
        is_paused,
    )
    draw_dbl_signal(
        screen,
        westbound_position[0],
        westbound_position[1],
        "WESTBOUND",
        dbls.get("WB", "INACTIVE"),
        is_paused,
    )
    draw_dbl_signal(
        screen,
        northbound_position[0],
        northbound_position[1],
        "NORTHBOUND",
        dbls.get("NB", "INACTIVE"),
        is_paused,
    )
    draw_dbl_signal(
        screen,
        southbound_position[0],
        southbound_position[1],
        "SOUTHBOUND",
        dbls.get("SB", "INACTIVE"),
        is_paused,
    )


def draw_network(
    screen,
    signal_data=None,
    dbl_states=None,
    font=None,
    is_paused=False,
):
    """Draw the complete two-node road network."""
    signal_data = signal_data if isinstance(signal_data, dict) else {}
    dbl_states = dbl_states if isinstance(dbl_states, dict) else {}

    screen.fill(BG)

    pygame.draw.rect(
        screen,
        ROAD,
        (0, H_Y - ROAD_W // 2, WIDTH, ROAD_W),
    )
    for center_x in INT_X:
        pygame.draw.rect(
            screen,
            ROAD,
            (center_x - ROAD_W // 2, 0, ROAD_W, HEIGHT),
        )

    # These offsets are lane boundaries, not vehicle centerlines.
    lane_boundary_offsets = [-2 * LANE, -LANE, LANE, 2 * LANE]

    for offset in lane_boundary_offsets:
        dashed(
            screen,
            (0, H_Y + offset),
            (WIDTH, H_Y + offset),
            LANE_GREY,
            1,
        )

    for center_x in INT_X:
        for offset in lane_boundary_offsets:
            dashed(
                screen,
                (center_x + offset, 0),
                (center_x + offset, HEIGHT),
                LANE_GREY,
                1,
            )

    draw_cleared_medians(screen)

    for center_x in INT_X:
        draw_intersection(
            screen,
            center_x,
            signal_data.get(center_x),
            dbl_states.get(center_x),
            is_paused,
        )

    if font is not None:
        draw_road_labels(screen, font)
```


## 3. `control_panel.py` — Configuration and operator callbacks

Purpose:

Owns the shared global, approach, six-route, and network-discharge configuration dictionaries and builds the Tkinter control panel with normal desktop stacking. Operators can select Auto or one of six discharge corridors, start recovery, request a safe stop, and read the selected/status/reason/recommendation messages. Its per-approach model selector includes Congestion Peak. Callbacks mutate configuration only. The LLM selector is display-only and RUN LLM is intentionally a no-op.

### Full source: `control_panel.py`

```python
# control_panel.py
import tkinter as tk
from tkinter import ttk
import sys

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

FONT_FAMILY = "Segoe UI"      # Clean UI font

SYM_DOT = "\u25cf"            # ●
SYM_PAUSE = "\u23f8"          # ⏸
SYM_PLAY = "\u25b6"           # ▶
SYM_RESET = "\u21ba"          # ↺
SYM_BUS = "\u1f68c"           # 🚌

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

# ==========================================================
# SHARED STATE DICTIONARIES (Accessed by main.py)
# ==========================================================
global_config = {
    "is_paused": False,
    "sim_speed": 1.0,        # 0.5x to 3.0x speed multiplier
    "green_time": 240,       # Signal green phase duration in frames
    "reset_triggered": False,# Flag to wipe canvas vehicles
    "is_running": True,      # Master execution flag
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
}

# ----------------------------------------------------------
# 6 SIMULTANEOUS BUS ROUTES (NODE B CORRECTED TO X=700)
# ----------------------------------------------------------
bus_routes_config = {
    "R1_EB_A_NB": {
        "name": "EB \u2192 Node A (NB)",
        "origin": "EB",
        "destination": "NODE_A_NB",
        "waypoints": {300: "LEFT"},
        "lanes": {300: 2},
        "active": True,
        "headway_sec": 30,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False
    },
    "R2_EB_B_NB": {
        "name": "EB \u2192 Node B (NB)",
        "origin": "EB",
        "destination": "NODE_B_NB",
        "waypoints": {300: "STRAIGHT", 700: "LEFT"},
        "lanes": {300: 1, 700: 2},
        "active": True,
        "headway_sec": 45,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False
    },
    "R3_EB_ONLY": {
        "name": "EB Corridor (Straight)",
        "origin": "EB",
        "destination": "EB_CORRIDOR",
        "waypoints": {300: "STRAIGHT", 700: "STRAIGHT"},
        "lanes": {300: 1, 700: 1},
        "active": False,
        "headway_sec": 30,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False
    },
    "R4_WB_A_SB": {
        "name": "WB \u2192 Node A (SB)",
        "origin": "WB",
        "destination": "NODE_A_SB",
        "waypoints": {700: "STRAIGHT", 300: "LEFT"},
        "lanes": {700: 1, 300: 2},
        "active": True,
        "headway_sec": 30,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False
    },
    "R5_WB_B_SB": {
        "name": "WB \u2192 Node B (SB)",
        "origin": "WB",
        "destination": "NODE_B_SB",
        "waypoints": {700: "LEFT"},
        "lanes": {700: 2},
        "active": True,
        "headway_sec": 45,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False
    },
    "R6_WB_ONLY": {
        "name": "WB Corridor (Straight)",
        "origin": "WB",
        "destination": "WB_CORRIDOR",
        "waypoints": {700: "STRAIGHT", 300: "STRAIGHT"},
        "lanes": {700: 1, 300: 1},
        "active": False,
        "headway_sec": 30,
        "tsp_enabled": False,
        "dbl_enabled": False,
        "manual_dispatch": False
    }
}

approach_configs = {
    "EB":   {"active": True,  "model": "Poisson", "rate": 12, "turn_split": 0.80, "heavy_ratio": 0.10},
    "WB":   {"active": True,  "model": "Poisson", "rate": 12, "turn_split": 0.80, "heavy_ratio": 0.10},
    "A_NB": {"active": True,  "model": "Poisson", "rate": 8,  "turn_split": 0.75, "heavy_ratio": 0.15},
    "A_SB": {"active": True,  "model": "Poisson", "rate": 8,  "turn_split": 0.75, "heavy_ratio": 0.15},
    "B_NB": {"active": True,  "model": "Poisson", "rate": 8,  "turn_split": 0.75, "heavy_ratio": 0.15},
    "B_SB": {"active": True,  "model": "Poisson", "rate": 8,  "turn_split": 0.75, "heavy_ratio": 0.15},
}

APPROACH_NAMES = {
    "EB": "EB Corridor",
    "WB": "WB Corridor",
    "A_NB": "Node A (NB)",
    "A_SB": "Node A (SB)",
    "B_NB": "Node B (NB)",
    "B_SB": "Node B (SB)"
}

def create_dashboard_window():
    root = tk.Tk()
    root.title("Traffic & Transit Control Dashboard")
    # Increased height slightly to accommodate the new AI control section
    root.geometry("820x1020")
    # Use normal desktop stacking. Forced topmost made the Pygame canvas slide
    # underneath this window and also made focus/drag interaction feel sticky.
    root.attributes("-topmost", False)
    root.configure(bg=COLOR_BG)

    def on_close():
        global_config["is_running"] = False
        try:
            root.destroy()
        except Exception:
            pass
        sys.exit()

    root.protocol("WM_DELETE_WINDOW", on_close)

    style = ttk.Style()
    style.theme_use('clam')

    style.configure(
        "Modern.TCombobox",
        fieldbackground=COLOR_BG,
        background=COLOR_CARD_BORDER,
        foreground=COLOR_TEXT_PRIMARY,
        darkcolor=COLOR_CARD_BORDER,
        lightcolor=COLOR_CARD_BORDER,
        bordercolor=COLOR_CARD_BORDER,
        arrowcolor=COLOR_TEXT_SECONDARY,
        padding=3
    )
    style.map("Modern.TCombobox", fieldbackground=[("readonly", COLOR_BG)])

    style.configure(
        "Modern.Horizontal.TScale",
        background=COLOR_CARD_ALT,
        troughcolor="#141822",
        slidercolor=COLOR_ACCENT,
        bordercolor=COLOR_CARD_BORDER,
        lightcolor=COLOR_ACCENT,
        darkcolor=COLOR_ACCENT,
        groovethickness=4,
        sliderthickness=12
    )

    style.configure(
        "Global.Horizontal.TScale",
        background=COLOR_CARD,
        troughcolor="#141822",
        slidercolor=COLOR_ACCENT,
        bordercolor=COLOR_CARD_BORDER,
        lightcolor=COLOR_ACCENT,
        darkcolor=COLOR_ACCENT,
        groovethickness=4,
        sliderthickness=12
    )

    # 1. HEADER & STATUS
    header_frame = tk.Frame(root, bg=COLOR_BG)
    header_frame.pack(fill="x", padx=20, pady=(16, 8))

    title_label = tk.Label(
        header_frame, text="NETWORK CONTROL DASHBOARD",
        font=(FONT_FAMILY, 15, "bold"), bg=COLOR_BG, fg=COLOR_TEXT_PRIMARY
    )
    title_label.pack(side="left")

    status_badge = tk.Frame(header_frame, bg=COLOR_BG)
    status_badge.pack(side="right")

    dot_lbl = tk.Label(status_badge, text=SYM_DOT, font=(FONT_FAMILY, 11), bg=COLOR_BG, fg=COLOR_SUCCESS)
    dot_lbl.pack(side="left", padx=(0, 4))

    status_text = tk.Label(
        status_badge, text="System Normal", font=(FONT_FAMILY, 10, "bold"),
        bg=COLOR_BG, fg=COLOR_SUCCESS
    )
    status_text.pack(side="left")

    # 2. GLOBAL SIMULATION CONTROLS CARD
    global_card = tk.Frame(
        root, bg=COLOR_CARD, highlightbackground=COLOR_CARD_BORDER,
        highlightthickness=1, bd=0
    )
    global_card.pack(fill="x", padx=20, pady=4, ipady=4)

    g_title = tk.Label(
        global_card, text="Global Simulation Controls",
        font=(FONT_FAMILY, 11, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY
    )
    g_title.pack(anchor="w", padx=16, pady=(8, 6))

    controls_row = tk.Frame(global_card, bg=COLOR_CARD)
    controls_row.pack(fill="x", padx=16, pady=(0, 6))

    def toggle_pause():
        global_config["is_paused"] = not global_config["is_paused"]
        if global_config["is_paused"]:
            pause_btn.config(text=f"{SYM_PLAY} RESUME", bg=COLOR_SUCCESS)
            dot_lbl.config(fg=COLOR_WARNING)
            status_text.config(text="Simulation Paused", fg=COLOR_WARNING)
        else:
            pause_btn.config(text=f"{SYM_PAUSE} PAUSE", bg=COLOR_ACCENT)
            dot_lbl.config(fg=COLOR_SUCCESS)
            status_text.config(text="System Normal", fg=COLOR_SUCCESS)

    pause_btn = tk.Button(
        controls_row, text=f"{SYM_PAUSE} PAUSE", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_ACCENT, fg=COLOR_TEXT_PRIMARY, activebackground="#1E70E0",
        activeforeground=COLOR_TEXT_PRIMARY, bd=0, padx=12, pady=4,
        cursor="hand2", relief="flat"
    )
    pause_btn.config(command=toggle_pause)
    pause_btn.pack(side="left", padx=(0, 8))

    def trigger_reset():
        global_config["reset_triggered"] = True

    reset_btn = tk.Button(
        controls_row, text=f"{SYM_RESET} RESET VEHICLES", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_DANGER, fg=COLOR_TEXT_PRIMARY, activebackground="#D32F2F",
        activeforeground=COLOR_TEXT_PRIMARY, bd=0, padx=12, pady=4,
        cursor="hand2", relief="flat"
    )
    reset_btn.config(command=trigger_reset)
    reset_btn.pack(side="left", padx=(0, 16))

    green_group = tk.Frame(controls_row, bg=COLOR_CARD)
    green_group.pack(side="left", padx=(0, 16))

    green_title_lbl = tk.Label(
        green_group, text="Green Time:", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY
    )
    green_title_lbl.pack(side="left", padx=(0, 4))

    green_val_lbl = tk.Label(
        green_group, text=f"{global_config['green_time'] // 60}s", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_CARD, fg=COLOR_ACCENT
    )
    green_val_lbl.pack(side="left", padx=(0, 4))

    def update_green_time(val):
        sec = int(float(val))
        global_config["green_time"] = sec * 60
        green_val_lbl.config(text=f"{sec}s")

    green_slider = ttk.Scale(
        green_group, from_=2, to=10, value=global_config["green_time"] // 60,
        style="Global.Horizontal.TScale", command=update_green_time, length=80
    )
    green_slider.pack(side="left", padx=2)

    speed_group = tk.Frame(controls_row, bg=COLOR_CARD)
    speed_group.pack(side="left")

    speed_title_lbl = tk.Label(
        speed_group, text="Speed:", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY
    )
    speed_title_lbl.pack(side="left", padx=(0, 4))

    speed_val_lbl = tk.Label(
        speed_group, text="1.0x", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_CARD, fg=COLOR_ACCENT
    )
    speed_val_lbl.pack(side="left", padx=(0, 4))

    def update_speed(val):
        v = float(val)
        global_config["sim_speed"] = v
        speed_val_lbl.config(text=f"{v:.1f}x")

    speed_slider = ttk.Scale(
        speed_group, from_=0.5, to=3.0, value=1.0,
        style="Global.Horizontal.TScale", command=update_speed, length=80
    )
    speed_slider.pack(side="left", padx=2)

    # 2.25 NETWORK GRIDLOCK RECOVERY CARD
    recovery_card = tk.Frame(
        root, bg=COLOR_CARD, highlightbackground=COLOR_DANGER,
        highlightthickness=1, bd=0
    )
    recovery_card.pack(fill="x", padx=20, pady=3)

    recovery_controls = tk.Frame(recovery_card, bg=COLOR_CARD)
    recovery_controls.pack(fill="x", padx=16, pady=(2, 2))

    tk.Label(
        recovery_controls, text="GRIDLOCK DISCHARGE:",
        font=(FONT_FAMILY, 8, "bold"), bg=COLOR_CARD, fg=COLOR_DANGER
    ).pack(side="left", padx=(0, 7))

    discharge_mode_box = ttk.Combobox(
        recovery_controls,
        values=list(DISCHARGE_OPTIONS),
        width=23,
        state="readonly",
        style="Modern.TCombobox",
    )
    discharge_mode_box.set(global_config["discharge_selection"])
    discharge_mode_box.pack(side="left", padx=(0, 8))

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

    start_discharge_btn = tk.Button(
        recovery_controls, text="START DISCHARGE",
        font=(FONT_FAMILY, 8, "bold"), bg=COLOR_DANGER,
        fg=COLOR_TEXT_PRIMARY, activebackground="#D32F2F",
        activeforeground=COLOR_TEXT_PRIMARY, bd=0, padx=10, pady=4,
        cursor="hand2", relief="flat", command=start_discharge
    )
    start_discharge_btn.pack(side="left", padx=(0, 8))

    def safe_stop_discharge():
        global_config["discharge_start_requested"] = False
        global_config["discharge_stop_requested"] = True

    stop_discharge_btn = tk.Button(
        recovery_controls, text="SAFE STOP",
        font=(FONT_FAMILY, 8, "bold"), bg=COLOR_CARD_BORDER,
        fg=COLOR_TEXT_PRIMARY, activebackground="#475569",
        activeforeground=COLOR_TEXT_PRIMARY, bd=0, padx=10, pady=4,
        cursor="hand2", relief="flat", command=safe_stop_discharge
    )
    stop_discharge_btn.pack(side="left")

    recovery_status = tk.Frame(recovery_card, bg=COLOR_CARD_ALT)
    recovery_status.pack(fill="x", padx=16, pady=(0, 3))
    discharge_status_lbl = tk.Label(
        recovery_status, text="Selected: Auto (Recommended)  |  Status: IDLE",
        font=(FONT_FAMILY, 8, "bold"), bg=COLOR_CARD_ALT,
        fg=COLOR_TEXT_SECONDARY, anchor="w"
    )
    discharge_status_lbl.pack(fill="x", padx=8, pady=(1, 0))
    discharge_reason_lbl = tk.Label(
        recovery_status, text="Reason: Normal signal control is active",
        font=(FONT_FAMILY, 8), bg=COLOR_CARD_ALT,
        fg=COLOR_TEXT_PRIMARY, anchor="w", justify="left", wraplength=750
    )
    discharge_reason_lbl.pack(fill="x", padx=8)
    discharge_recommendation_lbl = tk.Label(
        recovery_status,
        text="Recommended first action: Select Auto or a corridor, then start discharge",
        font=(FONT_FAMILY, 8), bg=COLOR_CARD_ALT,
        fg=COLOR_WARNING, anchor="w", justify="left", wraplength=750
    )
    discharge_recommendation_lbl.pack(fill="x", padx=8, pady=(0, 5))

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
        root.after(250, refresh_discharge_status)

    root.after(250, refresh_discharge_status)

    # 2.5 AI / LLM CONTROL CARD (NEW)
    ai_card = tk.Frame(
        root, bg=COLOR_CARD, highlightbackground=COLOR_WARNING,
        highlightthickness=1, bd=0
    )
    ai_card.pack(fill="x", padx=20, pady=4, ipady=4)

    ai_title = tk.Label(
        ai_card, text="AI / LLM CONTROL",
        font=(FONT_FAMILY, 11, "bold"), bg=COLOR_CARD, fg=COLOR_WARNING
    )
    ai_title.pack(anchor="w", padx=16, pady=(8, 6))

    # First row: Controls
    ai_row1 = tk.Frame(ai_card, bg=COLOR_CARD)
    ai_row1.pack(fill="x", padx=16, pady=(0, 6))

    llm_lbl = tk.Label(ai_row1, text="LLM Engine", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY)
    llm_lbl.pack(side="left", padx=(0, 8))

    llm_engine_box = ttk.Combobox(
        ai_row1,
        values=["None", "gemma4:12b", "deepseek-r1:32b", "qwen3.6:latest", "llama3:latest", "gemma4:latest"],
        width=22, state="readonly", style="Modern.TCombobox"
    )
    llm_engine_box.set("None")
    llm_engine_box.pack(side="left", padx=(0, 16))

    def on_llm_engine_selected(event):
        # UI-only update
        selected_val_lbl.config(text=llm_engine_box.get())

    llm_engine_box.bind("<<ComboboxSelected>>", on_llm_engine_selected)

    def on_run_llm():
        # Dummy callback - intentionally does nothing
        pass

    run_llm_btn = tk.Button(
        ai_row1, text=f"{SYM_PLAY} RUN LLM", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_WARNING, fg=COLOR_BG, activebackground="#D97706",
        activeforeground=COLOR_BG, bd=0, padx=12, pady=4,
        cursor="hand2", relief="flat", command=on_run_llm
    )
    run_llm_btn.pack(side="left", padx=(0, 16))

    llm_status_box = tk.Frame(ai_row1, bg=COLOR_CARD)
    llm_status_box.pack(side="right")

    llm_dot = tk.Label(llm_status_box, text=SYM_DOT, font=(FONT_FAMILY, 11), bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY)
    llm_dot.pack(side="left", padx=(0, 4))

    llm_status_text = tk.Label(llm_status_box, text="LLM INACTIVE", font=(FONT_FAMILY, 9, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY)
    llm_status_text.pack(side="left")

    # Second row: Selected Model Label
    ai_row2 = tk.Frame(ai_card, bg=COLOR_CARD)
    ai_row2.pack(fill="x", padx=16, pady=(0, 2))

    sel_lbl = tk.Label(ai_row2, text="Selected: ", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY)
    sel_lbl.pack(side="left")
    
    selected_val_lbl = tk.Label(ai_row2, text="None", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_ACCENT)
    selected_val_lbl.pack(side="left")

    # Third row: Control Scope Label
    ai_row3 = tk.Frame(ai_card, bg=COLOR_CARD)
    ai_row3.pack(fill="x", padx=16, pady=(0, 6))

    ctrl_lbl = tk.Label(ai_row3, text="Control: ", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY)
    ctrl_lbl.pack(side="left")
    
    ctrl_val_lbl = tk.Label(ai_row3, text="TSP + DBL", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_ACCENT)
    ctrl_val_lbl.pack(side="left")
    
    ctrl_rest_lbl = tk.Label(ai_row3, text=" for all bus routes", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY)
    ctrl_rest_lbl.pack(side="left")


    # 3. TRANSIT ROUTES CONTROL CARD
    transit_card = tk.Frame(
        root, bg=COLOR_CARD, highlightbackground=COLOR_CARD_BORDER,
        highlightthickness=1, bd=0
    )
    transit_card.pack(fill="x", padx=20, pady=4, ipady=6)

    t_header = tk.Frame(transit_card, bg=COLOR_CARD)
    t_header.pack(fill="x", padx=16, pady=(8, 6))

    t_title = tk.Label(
        t_header, text="Bus Routes Manager (DBL / TSP Control)",
        font=(FONT_FAMILY, 11, "bold"), bg=COLOR_CARD, fg=COLOR_WARNING
    )
    t_title.pack(side="left")

    dispatch_box = tk.Frame(t_header, bg=COLOR_CARD)
    dispatch_box.pack(side="right")

    disp_route_box = ttk.Combobox(
        dispatch_box, values=[r["name"] for r in bus_routes_config.values()],
        width=22, state="readonly", style="Modern.TCombobox"
    )
    disp_route_box.set(bus_routes_config["R1_EB_A_NB"]["name"])
    disp_route_box.pack(side="left", padx=(0, 8))

    def trigger_manual_dispatch():
        selected_name = disp_route_box.get()
        for r_id, r_cfg in bus_routes_config.items():
            if r_cfg["name"] == selected_name:
                if r_cfg["active"]:
                    r_cfg["manual_dispatch"] = True
                break

    manual_btn = tk.Button(
        dispatch_box, text="🚌 DISPATCH NOW", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_WARNING, fg=COLOR_BG, activebackground="#D97706",
        activeforeground=COLOR_BG, bd=0, padx=10, pady=3,
        cursor="hand2", relief="flat"
    )
    manual_btn.config(command=trigger_manual_dispatch)
    manual_btn.pack(side="left")

    TRANSIT_COL_WIDTHS = [200, 70, 190, 110, 110]

    r_headers = tk.Frame(transit_card, bg=COLOR_CARD, height=22)
    r_headers.pack_propagate(False)
    r_headers.pack(fill="x", padx=16, pady=(2, 4))

    r_titles = ["Route Name", "Status", "Headway Frequency", "TSP Signal", "DBL Lane"]
    for i, (title, width) in enumerate(zip(r_titles, TRANSIT_COL_WIDTHS)):
        h_box = tk.Frame(r_headers, bg=COLOR_CARD, width=width, height=22)
        h_box.pack_propagate(False)
        h_box.pack(side="left", padx=(0 if i == 0 else 10, 0))

        lbl = tk.Label(
            h_box, text=title, font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY, anchor="w"
        )
        lbl.pack(fill="both", expand=True)

    for r_id, r_cfg in bus_routes_config.items():
        row_frame = tk.Frame(
            transit_card, bg=COLOR_CARD_ALT, highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1, bd=0, height=34
        )
        row_frame.pack_propagate(False)
        row_frame.pack(fill="x", padx=16, pady=2)

        c1 = tk.Frame(row_frame, bg=COLOR_CARD_ALT, width=TRANSIT_COL_WIDTHS[0], height=34)
        c1.pack_propagate(False)
        c1.pack(side="left", padx=(8, 0))

        lbl_name = tk.Label(
            c1, text=r_cfg["name"], font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD_ALT, fg=COLOR_TEXT_PRIMARY, anchor="w"
        )
        lbl_name.pack(fill="both", expand=True)

        c2 = tk.Frame(row_frame, bg=COLOR_CARD_ALT, width=TRANSIT_COL_WIDTHS[1], height=34)
        c2.pack_propagate(False)
        c2.pack(side="left", padx=(10, 0))

        def make_route_toggle(key, btn):
            def toggle():
                bus_routes_config[key]["active"] = not bus_routes_config[key]["active"]
                act = bus_routes_config[key]["active"]
                btn.config(text="ON" if act else "OFF", fg=COLOR_SUCCESS if act else COLOR_TEXT_SECONDARY)
                if not act:
                    bus_routes_config[key]["manual_dispatch"] = False
            return toggle

        t_btn = tk.Button(
            c2, text="ON" if r_cfg["active"] else "OFF", font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD_ALT, fg=COLOR_SUCCESS if r_cfg["active"] else COLOR_TEXT_SECONDARY,
            bd=0, cursor="hand2", relief="flat", anchor="w"
        )
        t_btn.config(command=make_route_toggle(r_id, t_btn))
        t_btn.pack(fill="both", expand=True)

        c3 = tk.Frame(row_frame, bg=COLOR_CARD_ALT, width=TRANSIT_COL_WIDTHS[2], height=34)
        c3.pack_propagate(False)
        c3.pack(side="left", padx=(10, 0))

        hw_val_lbl = tk.Label(
            c3, text=f"{r_cfg['headway_sec']}s", font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD_ALT, fg=COLOR_ACCENT, width=4, anchor="w"
        )
        hw_val_lbl.pack(side="left")

        def make_hw_slider(key, lbl):
            def update(val):
                sec = int(float(val))
                bus_routes_config[key]["headway_sec"] = sec
                lbl.config(text=f"{sec}s" if sec > 0 else "OFF")
            return update

        hw_slider = ttk.Scale(
            c3, from_=0, to=90, value=r_cfg["headway_sec"],
            style="Modern.Horizontal.TScale", command=make_hw_slider(r_id, hw_val_lbl), length=130
        )
        hw_slider.pack(side="left", padx=(4, 0))

        c4 = tk.Frame(row_frame, bg=COLOR_CARD_ALT, width=TRANSIT_COL_WIDTHS[3], height=34)
        c4.pack_propagate(False)
        c4.pack(side="left", padx=(10, 0))

        def make_tsp_toggle(key, btn):
            def toggle():
                bus_routes_config[key]["tsp_enabled"] = not bus_routes_config[key]["tsp_enabled"]
                act = bus_routes_config[key]["tsp_enabled"]
                btn.config(text="TSP ACTIVE" if act else "TSP OFF", fg=COLOR_SUCCESS if act else COLOR_DANGER)
            return toggle

        tsp_btn = tk.Button(
            c4, text="TSP ACTIVE" if r_cfg["tsp_enabled"] else "TSP OFF", font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD_ALT, fg=COLOR_SUCCESS if r_cfg["tsp_enabled"] else COLOR_DANGER,
            bd=0, cursor="hand2", relief="flat", anchor="w"
        )
        tsp_btn.config(command=make_tsp_toggle(r_id, tsp_btn))
        tsp_btn.pack(fill="both", expand=True)

        c5 = tk.Frame(row_frame, bg=COLOR_CARD_ALT, width=TRANSIT_COL_WIDTHS[4], height=34)
        c5.pack_propagate(False)
        c5.pack(side="left", padx=(10, 0))

        def make_dbl_toggle(key, btn):
            def toggle():
                bus_routes_config[key]["dbl_enabled"] = not bus_routes_config[key]["dbl_enabled"]
                act = bus_routes_config[key]["dbl_enabled"]
                btn.config(text="DBL ACTIVE" if act else "DBL OFF", fg=COLOR_ACCENT if act else COLOR_DANGER)
            return toggle

        dbl_btn = tk.Button(
            c5, text="DBL ACTIVE" if r_cfg["dbl_enabled"] else "DBL OFF", font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD_ALT, fg=COLOR_ACCENT if r_cfg["dbl_enabled"] else COLOR_DANGER,
            bd=0, cursor="hand2", relief="flat", anchor="w"
        )
        dbl_btn.config(command=make_dbl_toggle(r_id, dbl_btn))
        dbl_btn.pack(fill="both", expand=True)

    # 4. PER-APPROACH PARAMETERS SECTION
    approaches_container = tk.Frame(root, bg=COLOR_BG)
    approaches_container.pack(fill="both", expand=True, padx=20, pady=4)

    sec_title = tk.Label(
        approaches_container, text="Per-Approach General Traffic Parameters",
        font=(FONT_FAMILY, 11, "bold"), bg=COLOR_BG, fg=COLOR_TEXT_PRIMARY
    )
    sec_title.pack(anchor="w", pady=(2, 4))

    GEN_COL_WIDTHS = [150, 150, 140, 140, 140]

    headers_frame = tk.Frame(approaches_container, bg=COLOR_BG, height=22)
    headers_frame.pack_propagate(False)
    headers_frame.pack(fill="x", padx=16, pady=(0, 2))

    h_titles = [
        "Source / Status",
        "Generation Model",
        "Inflow Rate (v/m)",
        "Straight %",
        "Trucks %"
    ]

    for i, (title, width) in enumerate(zip(h_titles, GEN_COL_WIDTHS)):
        h_box = tk.Frame(headers_frame, bg=COLOR_BG, width=width, height=22)
        h_box.pack_propagate(False)
        h_box.pack(side="left", padx=(0 if i == 0 else 10, 0))

        lbl = tk.Label(
            h_box, text=title, font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_BG, fg=COLOR_TEXT_SECONDARY, anchor="w"
        )
        lbl.pack(fill="both", expand=True)

    for key, name in APPROACH_NAMES.items():
        card = tk.Frame(
            approaches_container, bg=COLOR_CARD, highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1, bd=0, height=36
        )
        card.pack_propagate(False)
        card.pack(fill="x", padx=16, pady=2)

        col1 = tk.Frame(card, bg=COLOR_CARD, width=GEN_COL_WIDTHS[0], height=36)
        col1.pack_propagate(False)
        col1.pack(side="left", padx=(8, 0))

        c_name = tk.Label(
            col1, text=name, font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY, anchor="w"
        )
        c_name.pack(side="left")

        def make_toggle(k, btn, dot_label):
            def toggle():
                approach_configs[k]["active"] = not approach_configs[k]["active"]
                active = approach_configs[k]["active"]
                if active:
                    btn.config(text="ON", fg=COLOR_SUCCESS)
                    dot_label.config(fg=COLOR_SUCCESS)
                else:
                    btn.config(text="OFF", fg=COLOR_TEXT_SECONDARY)
                    dot_label.config(fg=COLOR_TEXT_SECONDARY)
            return toggle

        status_box = tk.Frame(col1, bg=COLOR_CARD)
        status_box.pack(side="right", padx=(0, 8))

        dot = tk.Label(status_box, text=SYM_DOT, font=(FONT_FAMILY, 8), bg=COLOR_CARD, fg=COLOR_SUCCESS)
        dot.pack(side="left")

        t_btn = tk.Button(
            status_box, text="ON", font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD, fg=COLOR_SUCCESS, bd=0, activebackground=COLOR_CARD,
            cursor="hand2", relief="flat"
        )
        t_btn.config(command=make_toggle(key, t_btn, dot))
        t_btn.pack(side="left", padx=2)

        col2 = tk.Frame(card, bg=COLOR_CARD, width=GEN_COL_WIDTHS[1], height=36)
        col2.pack_propagate(False)
        col2.pack(side="left", padx=(10, 0))

        model_box = ttk.Combobox(
            col2,
            values=[
                "Random",
                "Poisson",
                "Binomial",
                "Neg Binomial",
                "Congestion Peak",
            ],
            width=16,
            state="readonly",
            style="Modern.TCombobox",
        )
        model_box.set(approach_configs[key]["model"])
        model_box.pack(side="left", pady=4)

        def make_model_change(k, box):
            def change(event):
                approach_configs[k]["model"] = box.get()
            return change
        model_box.bind("<<ComboboxSelected>>", make_model_change(key, model_box))

        col3 = tk.Frame(card, bg=COLOR_CARD, width=GEN_COL_WIDTHS[2], height=36)
        col3.pack_propagate(False)
        col3.pack(side="left", padx=(10, 0))

        rate_val = tk.Label(
            col3, text=f"{approach_configs[key]['rate']} v/m",
            font=(FONT_FAMILY, 8, "bold"), bg=COLOR_CARD, fg=COLOR_ACCENT, width=5, anchor="w"
        )
        rate_val.pack(side="left")

        def make_rate_slider(k, lbl):
            def update(val):
                v = int(float(val))
                approach_configs[k]["rate"] = v
                lbl.config(text=f"{v} v/m")
            return update

        rate_slider = ttk.Scale(
            col3, from_=1, to=30, value=approach_configs[key]["rate"],
            style="Modern.Horizontal.TScale", command=make_rate_slider(key, rate_val), length=100
        )
        rate_slider.pack(side="left", padx=(4, 0))

        col4 = tk.Frame(card, bg=COLOR_CARD, width=GEN_COL_WIDTHS[3], height=36)
        col4.pack_propagate(False)
        col4.pack(side="left", padx=(10, 0))

        split_val = tk.Label(
            col4, text=f"{int(approach_configs[key]['turn_split']*100)}%",
            font=(FONT_FAMILY, 8, "bold"), bg=COLOR_CARD, fg=COLOR_ACCENT, width=4, anchor="w"
        )
        split_val.pack(side="left")

        def make_split_slider(k, lbl):
            def update(val):
                v_f = float(val) / 100.0
                approach_configs[k]["turn_split"] = v_f
                lbl.config(text=f"{int(v_f*100)}%")
            return update

        split_slider = ttk.Scale(
            col4, from_=0, to=100, value=int(approach_configs[key]["turn_split"]*100),
            style="Modern.Horizontal.TScale", command=make_split_slider(key, split_val), length=85
        )
        split_slider.pack(side="left", padx=(4, 0))

        col5 = tk.Frame(card, bg=COLOR_CARD, width=GEN_COL_WIDTHS[4], height=36)
        col5.pack_propagate(False)
        col5.pack(side="left", padx=(10, 0))

        heavy_val = tk.Label(
            col5, text=f"{int(approach_configs[key]['heavy_ratio']*100)}%",
            font=(FONT_FAMILY, 8, "bold"), bg=COLOR_CARD, fg=COLOR_ACCENT, width=4, anchor="w"
        )
        heavy_val.pack(side="left")

        def make_heavy_slider(k, lbl):
            def update(val):
                v_f = float(val) / 100.0
                approach_configs[k]["heavy_ratio"] = v_f
                lbl.config(text=f"{int(v_f*100)}%")
            return update

        heavy_slider = ttk.Scale(
            col5, from_=0, to=50, value=int(approach_configs[key]["heavy_ratio"]*100),
            style="Modern.Horizontal.TScale", command=make_heavy_slider(key, heavy_val), length=85
        )
        heavy_slider.pack(side="left", padx=(4, 0))

    return root
```


## 4. `main.py` — Application entry point and simulation ownership

Purpose:

Owns the vehicle list, stochastic arrivals, bounded Congestion Peak demand/backlogs, lane-aware bus dispatch, monotonic fixed-step accumulator with bounded work per Tk callback, source-relative child/dashboard paths, drawing, telemetry calls, reset, pause, and shutdown lifecycle. It freezes arrival admission and bus dispatch whenever network discharge is requested or active.

### Full source: `main.py`

```python
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
```


## 5. `signal_controller.py` — Signals, conflict reservations, DBL, and TSP

Purpose:

Owns independent Node A/Node B normal phases, movement reservations, route-leg priority requests, deterministic queues, stable attempt identity, durable terminal history, yellow and all-red interlocks, exclusive priority greens, geometry-aware same-green flow, rear-clear release, and telemetry-facing priority state. It also owns the gridlock-discharge state machine: downstream-first EB/WB staging, automatic safe-candidate ranking, manual corridor objectives, waiting diagnostics, exclusive greens, and safe restoration through yellow/all-red.

### Full source: `signal_controller.py`

```python
"""Signal timing, intersection reservations, and bus-priority arbitration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import control_panel
from canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from vehicle import Bus

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

DISCHARGE_INACTIVE = "INACTIVE"
DISCHARGE_TRANSITION_YELLOW = "TRANSITION_YELLOW"
DISCHARGE_ALL_RED = "DISCHARGE_ALL_RED"
DISCHARGE_WAITING = "WAITING"
DISCHARGE_ACTIVE = "DISCHARGING"
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
    ):
        self.global_config = global_config
        self.yellow_time = max(1, int(yellow_time))
        self.red_clearance_time = max(1, int(red_clearance_time))
        self.priority_request_timeout = max(1, int(priority_request_timeout))
        self.frame_number = 0
        self.nodes = {node_x: NodeState() for node_x in INT_X}
        self._request_sequence = 0
        self._attempt_counts: dict[tuple[str, int, int], int] = {}
        self.discharge_min_green = max(1, int(discharge_min_green))
        self.discharge_max_green = max(
            self.discharge_min_green, int(discharge_max_green)
        )
        self.discharge_stall_time = max(1, int(discharge_stall_time))
        self.discharge_queue_target = max(0, int(discharge_queue_target))
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
        self._discharge_tracked: dict[tuple[int, int], Any] = {}
        self._discharge_last_served = {
            plan_name: -self.discharge_max_green
            for plan_name in DISCHARGE_PLAN_STAGES
        }
        self._publish_discharge_status()

    # Backward-compatible Node A accessors. Production callers should use
    # get_node_status() because Node A and Node B are intentionally independent.
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
        """Return recovery control to a clean idle state after a full reset."""
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
            if self._matching_stage_vehicles(stage, vehicles):
                return index
        return None

    def _stage_readiness(self, stage, vehicles):
        for node_x, approach in stage.greens:
            if not self.is_intersection_clear(node_x, vehicles):
                node_name = "Node A" if node_x == INT_X[0] else "Node B"
                return False, f"{node_name} intersection is still occupied"

        matching = self._matching_stage_vehicles(stage, vehicles)
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
        self._discharge_tracked.clear()
        self._publish_discharge_status()

    def _activate_current_stage(self, vehicles):
        stage = self.current_discharge_stage
        if stage is None:
            return False
        ready, reason = self._stage_readiness(stage, vehicles)
        if not ready:
            self.discharge_state = DISCHARGE_WAITING
            self.discharge_reason = reason
            self.discharge_recommendation = self._recommend_discharge_plan(
                vehicles,
                exclude=(
                    self.discharge_plan_name
                    if self.discharge_mode != control_panel.DISCHARGE_AUTO
                    else None
                ),
            )
            self._discharge_green_map = {}
            return False

        self.discharge_state = DISCHARGE_ACTIVE
        self.discharge_timer = 0
        self._discharge_green_map = dict(stage.greens)
        self.discharge_reason = reason
        self.discharge_recommendation = (
            "Continue this protected movement until its stage completes"
        )
        self._discharge_last_progress_frame = self.frame_number
        for node_x, _approach in stage.greens:
            for vehicle in self._matching_stage_vehicles(stage, vehicles):
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
                candidate = next(
                    (item for item in ranked if item["ready"]),
                    ranked[0] if ranked else None,
                )
                if candidate is None:
                    self.discharge_state = DISCHARGE_WAITING
                    self.discharge_reason = (
                        "No queued approach currently requires a discharge green"
                    )
                    self.discharge_recommendation = (
                        "Maintain arrival suspension while occupied lanes drain"
                    )
                    return
                self.discharge_plan_name = candidate["plan"]
                self.discharge_stage_index = candidate["stage_index"]
                if not candidate["ready"]:
                    self.discharge_state = DISCHARGE_WAITING
                    self.discharge_reason = candidate["reason"]
                    self.discharge_recommendation = (
                        "Maintain arrival suspension while downstream traffic drains"
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
            if self._matching_stage_vehicles(stage, vehicles):
                self._activate_current_stage(vehicles)
                return
            self.discharge_stage_index += 1

        completed_plan = self.discharge_plan_name
        self._discharge_last_served[completed_plan] = self.frame_number
        self.discharge_cycles += 1
        if self.discharge_mode == control_panel.DISCHARGE_AUTO:
            self.discharge_plan_name = None
            self.discharge_stage_index = 0
            self.discharge_state = DISCHARGE_WAITING
            self.discharge_reason = f"{completed_plan} discharge is complete"
            self.discharge_recommendation = self._recommend_discharge_plan(vehicles)
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
            for vehicle in self._matching_stage_vehicles(stage, vehicles):
                if vehicle.get_next_target_node(INT_X) == node_x:
                    self._discharge_tracked.setdefault(
                        (id(vehicle), node_x), vehicle
                    )
        self._track_discharged_vehicles(vehicles)
        matching = self._matching_stage_vehicles(stage, vehicles)
        if any(vehicle.speed >= 0.25 for vehicle in matching):
            self._discharge_last_progress_frame = self.frame_number

        if self.discharge_timer < self.discharge_min_green:
            return
        if not matching:
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

    def is_bus_dbl_eligible(self, bus, target_node, all_vehicles=None):
        if not isinstance(bus, Bus):
            return False
        live_cfg = self._live_route_config(bus)
        if not live_cfg.get("dbl_enabled", False):
            return False
        leg = bus.get_active_route_leg(INT_X)
        if not leg or leg["node_x"] != target_node:
            return False
        if bus.lane_index != leg["entry_lane"]:
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
            entry_lane=leg["entry_lane"],
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
                node.priority_state = PRIORITY_ACTIVE
                request.state = PRIORITY_ACTIVE
                node.priority_timer = 0
            return
        if node.priority_state == PRIORITY_ACTIVE:
            if not request.bus.is_front_bumper_upstream(
                node_x, H_Y, ROAD_W, STOP
            ):
                node.priority_state = PRIORITY_CLEARING
                request.state = PRIORITY_CLEARING
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
                if request.denial_or_cancel_reason == "REQUEST_TIMEOUT":
                    terminal_state = DENIED
                elif request.denial_or_cancel_reason:
                    terminal_state = CANCELLED
                else:
                    terminal_state = COMPLETED
                self._finalize_request(node, request, terminal_state)
                node.active_request = None

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
```


## 6. `vehicle.py` — Vehicle, bus, route-leg, and movement physics

Purpose:

Implements following, fail-closed signal compliance, spillback checks, intersection-entry reservations, continuous turns, physical-node identity for vertical traffic, rear-clear completion, safe bus lane transitions, upstream merge holding, route-leg identity, and passenger authority.

### Full source: `vehicle.py`

```python
# vehicle.py
import pygame

class Vehicle:
    def __init__(self, x, y, direction, max_speed=1.0, color=(50, 150, 250), is_heavy=False, target_turn="STRAIGHT", lane_index=2, assigned_node_x=None):
        self.x = float(x)
        self.y = float(y)
        self.direction = direction
        self.max_speed = max_speed
        self.speed = max_speed
        self.color = color
        self.is_heavy = is_heavy
        self.lane_index = lane_index
        self.target_turn = target_turn
        self.passed_nodes = set()
        self.length = 28 if is_heavy else 18
        self.width = 12 if is_heavy else 10
        self.passengers = 4
        self.leg_state = "APPROACHING"
        self.intersection_entry_approaches = {}
        self.intersection_entry_movements = {}
        self.must_hold_for_lane = False
        self.merge_hold_distance = 35.0
        # NB/SB traffic belongs to one physical vertical road. Pinning that
        # node prevents it from falsely "completing" the remote intersection,
        # which shares the same horizontal y-coordinate.
        self.assigned_node_x = assigned_node_x

    def get_next_target_node(self, int_x_list):
        sorted_nodes = sorted(int_x_list)
        if self.direction == "EB":
            upcoming = [nx for nx in sorted_nodes if nx not in self.passed_nodes]
            return upcoming[0] if upcoming else sorted_nodes[-1]
        elif self.direction == "WB":
            upcoming = [nx for nx in sorted_nodes if nx not in self.passed_nodes]
            return upcoming[-1] if upcoming else sorted_nodes[0]
        else:
            if self.assigned_node_x not in sorted_nodes:
                self.assigned_node_x = min(sorted_nodes, key=lambda cx: abs(self.x - cx))
            return self.assigned_node_x

    def get_lead_vehicle_distance(self, all_vehicles):
        if not all_vehicles: return float('inf')
        min_dist = float('inf')
        my_half_w = self.width / 2.0
        
        for other in all_vehicles:
            if other is self: continue
            
            if other.direction in ("EB", "WB"):
                o_min_y, o_max_y = other.y - other.width/2.0, other.y + other.width/2.0
                o_min_x, o_max_x = other.x - other.length/2.0, other.x + other.length/2.0
            else:
                o_min_y, o_max_y = other.y - other.length/2.0, other.y + other.length/2.0
                o_min_x, o_max_x = other.x - other.width/2.0, other.x + other.width/2.0
                
            if self.direction in ("EB", "WB"):
                if not (self.y + my_half_w <= o_min_y or self.y - my_half_w >= o_max_y):
                    if self.direction == "EB" and o_min_x > self.x:
                        dist = o_min_x - (self.x + self.length/2.0)
                        if 0 <= dist < min_dist: min_dist = dist
                    elif self.direction == "WB" and o_max_x < self.x:
                        dist = (self.x - self.length/2.0) - o_max_x
                        if 0 <= dist < min_dist: min_dist = dist
            elif self.direction in ("NB", "SB"):
                if not (self.x + my_half_w <= o_min_x or self.x - my_half_w >= o_max_x):
                    if self.direction == "NB" and o_max_y < self.y:
                        dist = (self.y - self.length/2.0) - o_max_y
                        if 0 <= dist < min_dist: min_dist = dist
                    elif self.direction == "SB" and o_min_y > self.y:
                        dist = o_min_y - (self.y + self.length/2.0)
                        if 0 <= dist < min_dist: min_dist = dist
                        
        return min_dist
        
    def is_front_bumper_upstream(self, target_node_x, h_y=300, road_w=132, stop_offset=10):
        half_w = road_w // 2
        fx = self.x + self.length / 2.0 if self.direction == "EB" else self.x - self.length / 2.0
        fy = self.y + self.length / 2.0 if self.direction == "SB" else self.y - self.length / 2.0

        if self.direction == "EB": return fx < (target_node_x - half_w - stop_offset)
        elif self.direction == "WB": return fx > (target_node_x + half_w + stop_offset)
        elif self.direction == "NB": return fy > (h_y + half_w + stop_offset)
        elif self.direction == "SB": return fy < (h_y - half_w - stop_offset)
        return False

    def distance_to_node_stop_bar(self, target_node_x, h_y=300, road_w=132, stop_offset=10):
        half_w = road_w // 2
        if self.direction == "EB": return (target_node_x - half_w - stop_offset) - (self.x + self.length / 2.0)
        elif self.direction == "WB": return (self.x - self.length / 2.0) - (target_node_x + half_w + stop_offset)
        elif self.direction == "NB": return (self.y - self.length / 2.0) - (h_y + half_w + stop_offset)
        elif self.direction == "SB": return (h_y - half_w - stop_offset) - (self.y + self.length / 2.0)
        return 0.0

    def is_spillback_blocked(self, target_node_x, h_y, road_w, all_vehicles):
        half_w = road_w // 2
        SAFE_GAP = 12.0
        
        for other in all_vehicles:
            if other is self: continue
            
            if self.target_turn == "STRAIGHT":
                if other.direction == self.direction:
                    if self.direction in ("EB", "WB") and abs(other.y - self.y) < 8:
                        if self.direction == "EB" and other.x > target_node_x:
                            tail_space = (other.x - other.length/2.0) - (target_node_x + half_w)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5:
                                return True
                        elif self.direction == "WB" and other.x < target_node_x:
                            tail_space = (target_node_x - half_w) - (other.x + other.length/2.0)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5:
                                return True
                    elif self.direction in ("NB", "SB") and abs(other.x - self.x) < 8:
                        if self.direction == "NB" and other.y < h_y:
                            tail_space = (h_y - half_w) - (other.y + other.length/2.0)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5:
                                return True
                        elif self.direction == "SB" and other.y > h_y:
                            tail_space = (other.y - other.length/2.0) - (h_y + half_w)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5:
                                return True
            elif self.target_turn == "LEFT":
                target_dir = {"EB":"NB", "WB":"SB", "NB":"WB", "SB":"EB"}[self.direction]
                if other.direction == target_dir:
                    lane_offset = 2.5 * 22
                    if self.direction == "EB" and abs(other.x - (target_node_x - lane_offset)) < 8:
                        if other.y < h_y:
                            tail_space = (h_y - half_w) - (other.y + other.length/2.0)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5: return True
                    elif self.direction == "WB" and abs(other.x - (target_node_x + lane_offset)) < 8:
                        if other.y > h_y:
                            tail_space = (other.y - other.length/2.0) - (h_y + half_w)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5: return True
                    elif self.direction == "NB" and abs(other.y - (h_y + lane_offset)) < 8:
                        if other.x < target_node_x:
                            tail_space = (target_node_x - half_w) - (other.x + other.length/2.0)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5: return True
                    elif self.direction == "SB" and abs(other.y - (h_y - lane_offset)) < 8:
                        if other.x > target_node_x:
                            tail_space = (other.x - other.length/2.0) - (target_node_x + half_w)
                            if tail_space < self.length + SAFE_GAP and other.speed < 0.5: return True
        return False

    def update(self, signal_data, int_x_list, h_y, road_w=132, stop_offset=10, lane_w=22, all_vehicles=None, signal_controller=None):
        if all_vehicles is None: all_vehicles = []
        target_node_x = self.get_next_target_node(int_x_list)
        node_signals = signal_data.get(target_node_x, {})
        half_w = road_w // 2
        should_stop = False

        upstream = self.is_front_bumper_upstream(target_node_x, h_y, road_w, stop_offset)
        
        if upstream:
            self.leg_state = "APPROACHING"
        elif self.leg_state == "APPROACHING" and target_node_x not in self.passed_nodes:
            self.leg_state = "TURNING" if self.target_turn == "LEFT" else "IN_INTERSECTION"

        # 1. UPSTREAM DBL YIELDING (F-01 Fixed: Car yields only if BEHIND the priority bus)
        if not isinstance(self, Bus) and signal_controller:
            dbl_request = signal_controller.get_active_dbl_request(
                target_node_x, self.direction
            )
            if upstream and dbl_request and self.lane_index == dbl_request["entry_lane"]:
                dist_to_stop = self.distance_to_node_stop_bar(target_node_x, h_y, road_w, stop_offset)
                if 0.0 <= dist_to_stop <= 250.0:
                    # Check if a DBL bus is directly behind us pushing forward
                    is_car_ahead_of_bus = False
                    for other in all_vehicles:
                        if isinstance(other, Bus) and signal_controller.is_bus_dbl_eligible(other, target_node_x):
                            if self.direction == "EB" and other.x < self.x: is_car_ahead_of_bus = True
                            elif self.direction == "WB" and other.x > self.x: is_car_ahead_of_bus = True
                    
                    if not is_car_ahead_of_bus:
                        should_stop = True

        # 2. SIGNAL YIELDING & DOWNSTREAM SPILLBACK
        if target_node_x not in self.passed_nodes and upstream:
            dist_to_stop = self.distance_to_node_stop_bar(target_node_x, h_y, road_w, stop_offset)
            sig_state = node_signals.get(self.direction, "RED")
            if not isinstance(sig_state, str) or sig_state.upper() not in {
                "RED", "YELLOW", "GREEN"
            }:
                sig_state = "RED"
            else:
                sig_state = sig_state.upper()

            if sig_state != "GREEN" and signal_controller:
                signal_controller.cancel_intersection_entry(self, target_node_x)
            
            if sig_state in ("RED", "YELLOW") and dist_to_stop <= 15.0:
                should_stop = True
                
            if sig_state == "GREEN" and 0.0 <= dist_to_stop <= 25.0:
                if self.is_spillback_blocked(target_node_x, h_y, road_w, all_vehicles):
                    should_stop = True
                elif signal_controller and not signal_controller.request_intersection_entry(
                    self, target_node_x, all_vehicles
                ):
                    should_stop = True

            if self.must_hold_for_lane and 0.0 <= dist_to_stop <= self.merge_hold_distance:
                should_stop = True

        # 3. KINEMATICS
        lead_dist = max(0.0, self.get_lead_vehicle_distance(all_vehicles))
        SAFE_GAP = 12.0

        if lead_dist < SAFE_GAP or should_stop:
            should_stop = True
            self.speed = 0.0
        elif lead_dist < SAFE_GAP + 25.0:
            target_speed = min(self.max_speed, (lead_dist / 30.0) * self.max_speed)
            self.speed = max(0.0, self.speed - 0.05) if self.speed > target_speed else self.speed
        else:
            self.speed = min(self.max_speed, self.speed + 0.05)

        if should_stop and upstream and signal_controller:
            signal_controller.cancel_intersection_entry(self, target_node_x)

        # 4. TURN TRIGGERS & CONTINUOUS MOVEMENT
        step_dist = min(self.speed, lead_dist) if lead_dist < float('inf') else self.speed
        turned = False

        if self.target_turn == "LEFT" and target_node_x not in self.passed_nodes:
            lane_offset = 2.5 * lane_w
            
            if isinstance(self, Bus) and self.lane_index != 2 and not upstream:
                self.speed = 0.0
                step_dist = 0.0
            else:
                if self.direction == "EB":
                    pivot_x = target_node_x - lane_offset
                    if self.x <= pivot_x and (self.x + step_dist) >= pivot_x:
                        overshoot = (self.x + step_dist) - pivot_x
                        self.x = pivot_x
                        self.direction = "NB"
                        self.assigned_node_x = target_node_x
                        self.y -= overshoot
                        turned = True
                        step_dist = 0.0
                elif self.direction == "WB":
                    pivot_x = target_node_x + lane_offset
                    if self.x >= pivot_x and (self.x - step_dist) <= pivot_x:
                        overshoot = pivot_x - (self.x - step_dist)
                        self.x = pivot_x
                        self.direction = "SB"
                        self.assigned_node_x = target_node_x
                        self.y += overshoot
                        turned = True
                        step_dist = 0.0
                elif self.direction == "NB":
                    pivot_y = h_y + lane_offset
                    if self.y >= pivot_y and (self.y - step_dist) <= pivot_y:
                        overshoot = pivot_y - (self.y - step_dist)
                        self.y = pivot_y
                        self.direction = "WB"
                        self.assigned_node_x = target_node_x
                        self.x -= overshoot
                        turned = True
                        step_dist = 0.0
                elif self.direction == "SB":
                    pivot_y = h_y - lane_offset
                    if self.y <= pivot_y and (self.y + step_dist) >= pivot_y:
                        overshoot = (self.y + step_dist) - pivot_y
                        self.y = pivot_y
                        self.direction = "EB"
                        self.assigned_node_x = target_node_x
                        self.x += overshoot
                        turned = True
                        step_dist = 0.0

        if step_dist > 0.0:
            if self.direction == "EB": self.x += step_dist
            elif self.direction == "WB": self.x -= step_dist
            elif self.direction == "NB": self.y -= step_dist
            elif self.direction == "SB": self.y += step_dist

        if turned:
            self.leg_state = "DEPARTING"

        rx = self.x - self.length / 2.0 if self.direction == "EB" else self.x + self.length / 2.0
        ry = self.y - self.length / 2.0 if self.direction == "SB" else self.y + self.length / 2.0

        conflict_cleared = False
        if self.direction == "EB" and rx > target_node_x + half_w: conflict_cleared = True
        elif self.direction == "WB" and rx < target_node_x - half_w: conflict_cleared = True
        elif self.direction == "NB" and ry < h_y - half_w: conflict_cleared = True
        elif self.direction == "SB" and ry > h_y + half_w: conflict_cleared = True

        if conflict_cleared and target_node_x not in self.passed_nodes:
            self.passed_nodes.add(target_node_x)
            self.leg_state = "COMPLETE"
            if not getattr(self, "is_heavy", False):
                self.target_turn = "STRAIGHT"

    def draw(self, screen):
        rect = pygame.Rect(int(self.x - self.length / 2.0), int(self.y - self.width / 2.0), self.length, self.width) if self.direction in ("EB", "WB") else pygame.Rect(int(self.x - self.width / 2.0), int(self.y - self.length / 2.0), self.width, self.length)
        pygame.draw.rect(screen, self.color, rect, border_radius=3)


class Bus(Vehicle):
    def __init__(self, x, y, direction, route_info, bus_id="BUS_01"):
        first_node_x = 300 if direction == "EB" else 700
        target_turn = route_info.get("waypoints", {}).get(first_node_x, "STRAIGHT")
        super().__init__(x=x, y=y, direction=direction, max_speed=1.0, color=(245, 158, 11), is_heavy=True, target_turn=target_turn, lane_index=(2 if target_turn == "LEFT" else 1))
        self.bus_id = bus_id
        self.route_info = route_info
        self.route_id = route_info.get("route_id", "")
        self.length, self.width, self.passengers = 42, 14, 45
        self.route_nodes = sorted(
            route_info.get("waypoints", {}).keys(),
            reverse=(direction == "WB"),
        )

    def get_active_route_leg(self, int_x_list):
        """Return canonical metadata for the next unfinished route leg."""
        for route_leg_index, node_x in enumerate(self.route_nodes):
            if node_x in self.passed_nodes:
                continue
            movement = self.route_info.get("waypoints", {}).get(node_x, "STRAIGHT")
            configured_lanes = self.route_info.get("lanes", {})
            entry_lane = int(configured_lanes.get(node_x, 2 if movement == "LEFT" else 1))
            approach = self.direction
            if movement == "LEFT":
                exit_direction = {
                    "EB": "NB", "WB": "SB", "NB": "WB", "SB": "EB"
                }[approach]
            else:
                exit_direction = approach
            return {
                "route_leg_index": route_leg_index,
                "node_x": node_x,
                "approach": approach,
                "movement": movement,
                "entry_lane": entry_lane,
                "exit_direction": exit_direction,
            }
        return None

    def is_target_lane_clear(self, desired_y, all_vehicles):
        corridor_min = min(self.y, desired_y) - self.width / 2.0
        corridor_max = max(self.y, desired_y) + self.width / 2.0
        for other in all_vehicles:
            if other is self: continue
            if other.direction == self.direction:
                other_min = other.y - other.width / 2.0
                other_max = other.y + other.width / 2.0
                lateral_overlap = other_min < corridor_max and other_max > corridor_min
                if lateral_overlap and abs(other.x - self.x) < (self.length + other.length) / 2.0 + 15:
                    return False
        return True

    def update(self, signal_data, int_x_list, h_y, road_w=132, stop_offset=10, lane_w=22, all_vehicles=None, signal_controller=None):
        if all_vehicles is None: all_vehicles = []
        leg = self.get_active_route_leg(int_x_list)
        target_node_x = leg["node_x"] if leg else self.get_next_target_node(int_x_list)
        self.target_turn = leg["movement"] if leg else "STRAIGHT"
        required_lane = leg["entry_lane"] if leg else self.lane_index
        self.must_hold_for_lane = False

        if self.target_turn == "LEFT" and self.lane_index != required_lane:
            dist_to_intersection = abs(self.x - target_node_x) if self.direction in ("EB", "WB") else abs(self.y - h_y)
            if dist_to_intersection < 250.0:
                desired_y = h_y - (2.5 * lane_w) if self.direction == "EB" else h_y + (2.5 * lane_w)
                if self.is_target_lane_clear(desired_y, all_vehicles):
                    if abs(self.y - desired_y) > 1.0: self.y += 0.5 if self.y < desired_y else -0.5
                    else:
                        self.y = desired_y
                        self.lane_index = required_lane
                if self.lane_index != required_lane:
                    self.must_hold_for_lane = True

        super().update(signal_data, int_x_list, h_y, road_w, stop_offset, lane_w, all_vehicles, signal_controller)

    def draw(self, screen):
        super().draw(screen)
        inner_rect = pygame.Rect(int(self.x - self.length / 4.0), int(self.y - self.width / 4.0), self.length / 2.0, self.width / 2.0) if self.direction in ("EB", "WB") else pygame.Rect(int(self.x - self.width / 4.0), int(self.y - self.length / 4.0), self.width / 2.0, self.length / 2.0)
        pygame.draw.rect(screen, (255, 255, 255), inner_rect, border_radius=1)
```


## 7. Telemetry subsystem — Export and dashboard

Purpose:

The exporter builds schema-versioned per-node state, exports the controller's exact normal signal timing, separates pending/active/clearing priority, exports per-source congestion demand and the complete network-discharge status, and atomically replaces a source-relative JSON file. The dashboard validates that schema, distinguishes LIVE/PAUSED/STALE/ERROR, always reschedules polling, displays road/demand queues and active/pending grants separately, renders each node independently, and shows the recovery selection, status, reason, recommendation, stage, and discharge count. Both notebook tabs use responsive two-axis scroll containers, and the dashboard chooses a screen-fitting initial size while remaining freely resizable; the mouse wheel scrolls vertically and Shift+wheel scrolls horizontally. Below the summary cards, a three-band nominal phase-cycle diagram shows east-west, Node A north-south, and Node B north-south timing, outlined all-red intervals, a wrapping time marker, and authoritative live-state dots that expose priority or discharge divergence. Its Session Trends tab samples only advancing LIVE frames into bounded process memory and plots occupancy, queue pressure, and congestion without creating a history file; reset, manual clear, or dashboard close discards the samples.

### Full source: `telemetry_exporter.py`

```python
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


class TelemetryExporter:
    def __init__(self, filename=DEFAULT_TELEMETRY_PATH, export_interval_frames=10):
        self.filename = Path(filename).resolve()
        self.export_interval = max(1, int(export_interval_frames))
        self.frame_counter = 0

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
        self, signal_controller, vehicles, frame_number, demand_state=None
    ):
        queues = self.compute_queue_counts(vehicles)
        demand_state = demand_state or {}
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
        return {
            "schema_version": 2,
            "timestamp": round(time.time(), 3),
            "frame_number": frame_number,
            "simulation_time_seconds": round(frame_number / 60.0, 3),
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
                "pending_demand": pending_demand,
            },
            "demand_generation": demand_state,
            "active_buses": buses,
            "approaching_buses": approaching,
        }

    def export(
        self, signal_controller, vehicles, frame_number, demand_state=None
    ):
        self.frame_counter += 1
        if self.frame_counter % self.export_interval != 0:
            return False

        payload = self.build_payload(
            signal_controller, vehicles, frame_number, demand_state=demand_state
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
```


### Full source: `telemetry_dashboard.py`

```python
"""Read-only live telemetry dashboard with session-only trend history."""

from collections import deque
import json
import math
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk


COLOR_BG = "#1A1E29"
COLOR_CARD = "#222834"
COLOR_CARD_BORDER = "#333D50"
COLOR_ACCENT = "#2D8CFF"
COLOR_SUCCESS = "#2ECC71"
COLOR_WARNING = "#F59E0B"
COLOR_DANGER = "#EF4444"
COLOR_TEXT_PRIMARY = "#FFFFFF"
COLOR_TEXT_SECONDARY = "#94A3B8"
FONT_FAMILY = "Segoe UI"

BASE_DIR = Path(__file__).resolve().parent
TELEMETRY_FILE = BASE_DIR / "traffic_state_telemetry.json"
STALE_AFTER_SECONDS = 2.0
HISTORY_SAMPLE_SECONDS = 1.0
HISTORY_MAX_POINTS = 600
WINDOW_DEFAULT_WIDTH = 900
WINDOW_DEFAULT_HEIGHT = 780
WINDOW_MIN_WIDTH = 480
WINDOW_MIN_HEIGHT = 360
WINDOW_SCREEN_MARGIN_X = 80
WINDOW_SCREEN_MARGIN_Y = 140
DASHBOARD_CONTENT_MIN_WIDTH = 720


class TelemetryDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Live Network Telemetry")
        window_width, window_height = self.initial_window_size(
            self.root.winfo_screenwidth(),
            self.root.winfo_screenheight(),
        )
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.root.resizable(True, True)
        self.root.configure(bg=COLOR_BG)
        self.initialize_history_state()
        self.latest_telemetry = None
        self.last_read_error = None
        self.build_ui()
        self.poll_telemetry()

    @staticmethod
    def initial_window_size(screen_width, screen_height):
        """Fit the initial dashboard inside the screen while allowing resizing."""
        width = min(
            WINDOW_DEFAULT_WIDTH,
            max(WINDOW_MIN_WIDTH, int(screen_width) - WINDOW_SCREEN_MARGIN_X),
        )
        height = min(
            WINDOW_DEFAULT_HEIGHT,
            max(WINDOW_MIN_HEIGHT, int(screen_height) - WINDOW_SCREEN_MARGIN_Y),
        )
        return width, height

    def initialize_history_state(self):
        """Create bounded, process-local history containers.

        Nothing in this state is written to telemetry or another file. Closing
        the dashboard therefore discards the complete chart history.
        """
        self.history = {
            key: deque(maxlen=HISTORY_MAX_POINTS)
            for key in (
                "time",
                "vehicles",
                "buses",
                "road_queue",
                "pending_demand",
                "congestion",
            )
        }
        self.queue_history = deque(maxlen=20)
        self.last_seen_frame = None
        self.last_seen_simulation_time = None
        self.last_sample_time = None

    def build_ui(self):
        header = tk.Frame(self.root, bg=COLOR_BG)
        header.pack(fill="x", padx=20, pady=(15, 10))
        tk.Label(
            header,
            text="LIVE TELEMETRY DASHBOARD",
            font=(FONT_FAMILY, 16, "bold"),
            bg=COLOR_BG,
            fg=COLOR_TEXT_PRIMARY,
        ).pack(side="left")
        self.status_lbl = tk.Label(
            header,
            text="WAITING FOR DATA",
            font=(FONT_FAMILY, 10, "bold"),
            bg=COLOR_BG,
            fg=COLOR_WARNING,
        )
        self.status_lbl.pack(side="right")

        style = ttk.Style(self.root)
        style.configure("Telemetry.TNotebook", background=COLOR_BG, borderwidth=0)
        style.configure(
            "Telemetry.TNotebook.Tab",
            font=(FONT_FAMILY, 9, "bold"),
            padding=(16, 7),
        )
        self.notebook = ttk.Notebook(
            self.root,
            style="Telemetry.TNotebook",
        )
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.scroll_canvases = {}
        (
            self.summary_tab,
            self.summary_content,
            self.summary_scroll_canvas,
        ) = self.create_scrollable_tab()
        (
            self.trends_tab,
            self.trends_content,
            self.trends_scroll_canvas,
        ) = self.create_scrollable_tab()
        self.notebook.add(self.summary_tab, text="SUMMARY")
        self.notebook.add(self.trends_tab, text="SESSION TRENDS")
        self.scroll_canvases[str(self.summary_tab)] = self.summary_scroll_canvas
        self.scroll_canvases[str(self.trends_tab)] = self.trends_scroll_canvas
        self.root.bind("<MouseWheel>", self.on_mousewheel, add="+")
        self.root.bind("<Button-4>", self.on_mousewheel, add="+")
        self.root.bind("<Button-5>", self.on_mousewheel, add="+")

        self.metrics_frame = tk.Frame(self.summary_content, bg=COLOR_BG)
        self.metrics_frame.pack(fill="x", padx=20, pady=5)
        self.vars = {}
        metrics_layout = [
            [("Active Vehicles", "vehicles"), ("Active Buses", "buses"), ("Passenger Vol", "passengers")],
            [("Road/Demand Queue", "queued"), ("Avg Queue (20s)", "delay"), ("Congestion", "congestion")],
            [("TSP Active/Pending", "tsp"), ("DBL Active/Pending", "dbl"), ("Sim Timer", "timer")],
        ]
        for row in metrics_layout:
            row_frame = tk.Frame(self.metrics_frame, bg=COLOR_BG)
            row_frame.pack(fill="x", pady=5)
            for title, key in row:
                card = tk.Frame(
                    row_frame,
                    bg=COLOR_CARD,
                    highlightbackground=COLOR_CARD_BORDER,
                    highlightthickness=1,
                )
                card.pack(side="left", fill="x", expand=True, padx=5)
                tk.Label(
                    card,
                    text=title,
                    font=(FONT_FAMILY, 9, "bold"),
                    bg=COLOR_CARD,
                    fg=COLOR_TEXT_SECONDARY,
                ).pack(anchor="w", padx=10, pady=(10, 0))
                value = tk.Label(
                    card,
                    text="--",
                    font=(FONT_FAMILY, 18, "bold"),
                    bg=COLOR_CARD,
                    fg=COLOR_ACCENT,
                )
                value.pack(anchor="w", padx=10, pady=(0, 10))
                self.vars[key] = value

        recovery_card = tk.Frame(
            self.summary_content,
            bg=COLOR_CARD,
            highlightbackground=COLOR_DANGER,
            highlightthickness=1,
        )
        recovery_card.pack(fill="x", padx=25, pady=(7, 3))
        tk.Label(
            recovery_card,
            text="NETWORK GRIDLOCK RECOVERY",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_DANGER,
        ).pack(anchor="w", padx=10, pady=(6, 1))
        self.discharge_selected_lbl = tk.Label(
            recovery_card,
            text="Selected: Auto (Recommended)",
            font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_ACCENT,
            anchor="w",
        )
        self.discharge_selected_lbl.pack(fill="x", padx=10)
        self.discharge_status_lbl = tk.Label(
            recovery_card,
            text="Status: IDLE",
            font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_SECONDARY,
            anchor="w",
        )
        self.discharge_status_lbl.pack(fill="x", padx=10)
        self.discharge_reason_lbl = tk.Label(
            recovery_card,
            text="Reason: Normal signal control is active",
            font=(FONT_FAMILY, 8),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_PRIMARY,
            anchor="w",
            justify="left",
        )
        self.discharge_reason_lbl.pack(fill="x", padx=10)
        self.discharge_recommendation_lbl = tk.Label(
            recovery_card,
            text="Recommended first action: Select Auto or a corridor, then start discharge",
            font=(FONT_FAMILY, 8),
            bg=COLOR_CARD,
            fg=COLOR_WARNING,
            anchor="w",
            justify="left",
        )
        self.discharge_recommendation_lbl.pack(fill="x", padx=10, pady=(0, 6))

        self.build_phase_cycle_ui()

        tk.Label(
            self.summary_content,
            text="INTERSECTION PHASE STATES",
            font=(FONT_FAMILY, 11, "bold"),
            bg=COLOR_BG,
            fg=COLOR_TEXT_PRIMARY,
        ).pack(anchor="w", padx=20, pady=(15, 5))
        diagram_frame = tk.Frame(self.summary_content, bg=COLOR_BG)
        diagram_frame.pack(fill="both", expand=True, padx=20, pady=5)
        self.node_a_canvas = self.create_node_canvas(diagram_frame, "NODE A (x=300)")
        self.node_a_canvas.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.node_b_canvas = self.create_node_canvas(diagram_frame, "NODE B (x=700)")
        self.node_b_canvas.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self.build_trends_ui()

    def create_scrollable_tab(self):
        """Return a notebook tab with two-axis scrolling and a content frame."""
        tab = tk.Frame(self.notebook, bg=COLOR_BG)
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        viewport = tk.Canvas(
            tab,
            bg=COLOR_BG,
            highlightthickness=0,
            borderwidth=0,
        )
        vertical = ttk.Scrollbar(tab, orient="vertical", command=viewport.yview)
        horizontal = ttk.Scrollbar(tab, orient="horizontal", command=viewport.xview)
        viewport.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        viewport.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        content = tk.Frame(viewport, bg=COLOR_BG)
        window_id = viewport.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event, canvas=viewport: self.update_scroll_region(canvas),
        )
        viewport.bind(
            "<Configure>",
            lambda event, canvas=viewport, item=window_id: self.resize_scroll_content(
                canvas, item, event.width
            ),
        )
        return tab, content, viewport

    @staticmethod
    def update_scroll_region(canvas):
        """Keep both scrollbars synchronized with the complete content area."""
        bounds = canvas.bbox("all")
        if bounds:
            canvas.configure(scrollregion=bounds)

    def resize_scroll_content(self, canvas, window_id, viewport_width):
        """Fill wide viewports, retaining a scrollable minimum on narrow ones."""
        content_width = max(DASHBOARD_CONTENT_MIN_WIDTH, int(viewport_width))
        canvas.itemconfigure(window_id, width=content_width)
        self.update_scroll_region(canvas)

    @staticmethod
    def mousewheel_units(event):
        """Normalize Windows/macOS wheel deltas and Linux wheel buttons."""
        delta = int(getattr(event, "delta", 0) or 0)
        if delta:
            steps = max(1, abs(delta) // 120)
            return -steps if delta > 0 else steps
        button = getattr(event, "num", None)
        if button == 4:
            return -1
        if button == 5:
            return 1
        return 0

    def on_mousewheel(self, event):
        """Scroll the selected tab; Shift+wheel scrolls horizontally."""
        canvas = self.scroll_canvases.get(self.notebook.select())
        units = self.mousewheel_units(event)
        if canvas is None or units == 0:
            return None
        if int(getattr(event, "state", 0) or 0) & 0x0001:
            canvas.xview_scroll(units, "units")
        else:
            canvas.yview_scroll(units, "units")
        return "break"

    def build_phase_cycle_ui(self):
        card = tk.Frame(
            self.summary_content,
            bg=COLOR_CARD,
            highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1,
        )
        card.pack(fill="x", padx=25, pady=(10, 3))
        heading = tk.Frame(card, bg=COLOR_CARD)
        heading.pack(fill="x", padx=10, pady=(7, 0))
        tk.Label(
            heading,
            text="SIGNAL PHASE CYCLE",
            font=(FONT_FAMILY, 10, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_PRIMARY,
        ).pack(side="left")
        self.phase_status_lbl = tk.Label(
            heading,
            text="WAITING",
            width=18,
            anchor="e",
            font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_WARNING,
        )
        self.phase_status_lbl.pack(side="right")
        tk.Label(
            heading,
            text="Nominal plan • marker repeats • dots show live state",
            font=(FONT_FAMILY, 8),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_SECONDARY,
        ).pack(side="right", padx=(10, 12))
        self.phase_cycle_canvas = tk.Canvas(
            card,
            bg=COLOR_CARD,
            highlightthickness=0,
            height=145,
        )
        self.phase_cycle_canvas.pack(fill="x", padx=6, pady=(0, 5))
        self.phase_cycle_canvas.bind(
            "<Configure>",
            lambda _event: self.draw_phase_cycle(self.latest_telemetry),
        )

    def build_trends_ui(self):
        controls = tk.Frame(self.trends_content, bg=COLOR_BG)
        controls.pack(fill="x", padx=20, pady=(12, 6))
        tk.Label(
            controls,
            text=(
                "IN MEMORY ONLY  |  Up to 1 sample per simulated second  |  "
                f"Latest {HISTORY_MAX_POINTS} samples  |  Clears on reset/close"
            ),
            font=(FONT_FAMILY, 9),
            bg=COLOR_BG,
            fg=COLOR_TEXT_SECONDARY,
        ).pack(side="left")
        tk.Button(
            controls,
            text="CLEAR HISTORY",
            command=self.clear_history,
            font=(FONT_FAMILY, 8, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_PRIMARY,
            activebackground=COLOR_CARD_BORDER,
            activeforeground=COLOR_TEXT_PRIMARY,
            relief="flat",
            padx=10,
            pady=4,
        ).pack(side="right")

        charts = tk.Frame(self.trends_content, bg=COLOR_BG)
        charts.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        self.trend_charts = []
        self.create_trend_chart(
            charts,
            "NETWORK OCCUPANCY",
            (
                ("vehicles", "Vehicles", COLOR_ACCENT),
                ("buses", "Buses", COLOR_WARNING),
            ),
        )
        self.create_trend_chart(
            charts,
            "QUEUE PRESSURE",
            (
                ("road_queue", "Stopped road queue", COLOR_DANGER),
                ("pending_demand", "Waiting to enter", COLOR_WARNING),
            ),
        )
        self.create_trend_chart(
            charts,
            "CONGESTION  (stopped road queue / active vehicles)",
            (("congestion", "Congestion %", COLOR_SUCCESS),),
            fixed_max=100.0,
        )

    def create_trend_chart(self, parent, title, series, fixed_max=None):
        card = tk.Frame(
            parent,
            bg=COLOR_CARD,
            highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1,
        )
        card.pack(fill="both", expand=True, pady=5)
        tk.Label(
            card,
            text=title,
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_SECONDARY,
        ).pack(anchor="w", padx=10, pady=(7, 0))
        canvas = tk.Canvas(
            card,
            bg=COLOR_CARD,
            highlightthickness=0,
            height=150,
        )
        canvas.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        chart = {
            "canvas": canvas,
            "series": series,
            "fixed_max": fixed_max,
        }
        self.trend_charts.append(chart)
        canvas.bind("<Configure>", lambda _event: self.draw_trend_charts())
        return canvas

    def create_node_canvas(self, parent, title):
        card = tk.Frame(
            parent,
            bg=COLOR_CARD,
            highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1,
        )
        tk.Label(
            card,
            text=title,
            font=(FONT_FAMILY, 10, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_SECONDARY,
        ).pack(pady=(10, 0))
        canvas = tk.Canvas(card, bg=COLOR_CARD, highlightthickness=0, height=135)
        canvas.pack(fill="both", expand=True)
        return canvas

    @staticmethod
    def build_nominal_phase_segments(timing):
        """Return the normal six-phase plan used by the controller."""
        green = max(1, int(timing.get("green_frames", 240)))
        yellow = max(1, int(timing.get("yellow_frames", 60)))
        all_red = max(1, int(timing.get("all_red_frames", 60)))
        ns_start = green + yellow + all_red
        cycle = 2 * ns_start
        return cycle, {
            "EW": (
                (0, green, "GREEN"),
                (green, green + yellow, "YELLOW"),
                (green + yellow, cycle, "RED"),
            ),
            "NS_A": (
                (0, ns_start, "RED"),
                (ns_start, ns_start + green, "GREEN"),
                (ns_start + green, ns_start + green + yellow, "YELLOW"),
                (ns_start + green + yellow, cycle, "RED"),
            ),
            "NS_B": (
                (0, ns_start, "RED"),
                (ns_start, ns_start + green, "GREEN"),
                (ns_start + green, ns_start + green + yellow, "YELLOW"),
                (ns_start + green + yellow, cycle, "RED"),
            ),
        }

    @staticmethod
    def phase_marker_fraction(frame_number, cycle_frames):
        cycle_frames = max(1, int(cycle_frames))
        return int(frame_number) % cycle_frames / cycle_frames

    @staticmethod
    def aggregate_signal_state(states):
        normalized = {state for state in states if state in ("GREEN", "YELLOW", "RED")}
        if not normalized:
            return "RED"
        if len(normalized) == 1:
            return normalized.pop()
        return "MIXED"

    def draw_phase_cycle(self, data):
        """Draw the repeating nominal plan and authoritative live-state dots."""
        canvas = self.phase_cycle_canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 260 or height < 100:
            return
        if not data:
            if hasattr(self, "phase_status_lbl"):
                self.phase_status_lbl.config(text="WAITING", fg=COLOR_WARNING)
            canvas.create_text(
                width / 2,
                height / 2,
                text="Waiting for signal timing...",
                fill=COLOR_TEXT_SECONDARY,
                font=(FONT_FAMILY, 9),
            )
            return

        signal_state = data.get("signal_state", {})
        timing = signal_state.get("timing", {})
        cycle_frames, segment_map = self.build_nominal_phase_segments(timing)
        frames_per_second = max(1, int(timing.get("frames_per_second", 60)))
        left, right, top, bottom = 142, 14, 25, 25
        plot_width = max(1, width - left - right)
        row_gap = 5
        row_height = max(12, (height - top - bottom - 2 * row_gap) / 3)
        rows = (
            ("EW", "EAST–WEST CORRIDOR"),
            ("NS_A", "NORTH–SOUTH NODE A"),
            ("NS_B", "NORTH–SOUTH NODE B"),
        )
        state_colors = {
            "GREEN": COLOR_SUCCESS,
            "YELLOW": COLOR_WARNING,
            "RED": COLOR_DANGER,
            "MIXED": COLOR_ACCENT,
        }

        def frame_x(frame):
            return left + frame / cycle_frames * plot_width

        row_centers = {}
        for row_index, (row_key, row_label) in enumerate(rows):
            y1 = top + row_index * (row_height + row_gap)
            y2 = y1 + row_height
            row_centers[row_key] = (y1 + y2) / 2
            canvas.create_text(
                left - 10,
                (y1 + y2) / 2,
                text=row_label,
                anchor="e",
                fill=COLOR_TEXT_SECONDARY,
                font=(FONT_FAMILY, 8, "bold"),
            )
            for start, end, state in segment_map[row_key]:
                canvas.create_rectangle(
                    frame_x(start),
                    y1,
                    frame_x(end),
                    y2,
                    fill=state_colors[state],
                    outline=COLOR_CARD,
                    width=1,
                )

        green = max(1, int(timing.get("green_frames", 240)))
        yellow = max(1, int(timing.get("yellow_frames", 60)))
        all_red = max(1, int(timing.get("all_red_frames", 60)))
        all_red_ranges = (
            (green + yellow, green + yellow + all_red),
            (cycle_frames - all_red, cycle_frames),
        )
        chart_bottom = row_centers["NS_B"] + row_height / 2
        for start, end in all_red_ranges:
            canvas.create_rectangle(
                frame_x(start),
                top - 2,
                frame_x(end),
                chart_bottom + 2,
                outline=COLOR_TEXT_PRIMARY,
                dash=(3, 2),
                width=1,
            )
            canvas.create_text(
                (frame_x(start) + frame_x(end)) / 2,
                9,
                text="ALL RED",
                fill=COLOR_TEXT_SECONDARY,
                font=(FONT_FAMILY, 7, "bold"),
            )

        boundaries = sorted(
            {
                0,
                green,
                green + yellow,
                green + yellow + all_red,
                green + yellow + all_red + green,
                cycle_frames - all_red,
                cycle_frames,
            }
        )
        for boundary in boundaries:
            x = frame_x(boundary)
            canvas.create_line(x, chart_bottom + 2, x, chart_bottom + 5, fill=COLOR_TEXT_SECONDARY)
            canvas.create_text(
                x,
                height - 7,
                text=f"{boundary / frames_per_second:g}s",
                anchor="w" if boundary == 0 else ("e" if boundary == cycle_frames else "center"),
                fill=COLOR_TEXT_SECONDARY,
                font=(FONT_FAMILY, 7),
            )

        marker_fraction = self.phase_marker_fraction(
            data.get("frame_number", 0), cycle_frames
        )
        marker_x = left + marker_fraction * plot_width
        canvas.create_line(
            marker_x,
            top - 7,
            marker_x,
            chart_bottom + 4,
            fill=COLOR_TEXT_PRIMARY,
            width=2,
        )
        canvas.create_polygon(
            marker_x - 5,
            top - 8,
            marker_x + 5,
            top - 8,
            marker_x,
            top - 2,
            fill=COLOR_TEXT_PRIMARY,
            outline="",
        )

        nodes = signal_state.get("nodes", {})
        node_a_signals = nodes.get("300", {}).get("signals", {})
        node_b_signals = nodes.get("700", {}).get("signals", {})
        live_states = {
            "EW": self.aggregate_signal_state(
                (
                    node_a_signals.get("EB"),
                    node_a_signals.get("WB"),
                    node_b_signals.get("EB"),
                    node_b_signals.get("WB"),
                )
            ),
            "NS_A": self.aggregate_signal_state(
                (node_a_signals.get("NB"), node_a_signals.get("SB"))
            ),
            "NS_B": self.aggregate_signal_state(
                (node_b_signals.get("NB"), node_b_signals.get("SB"))
            ),
        }
        for row_key, live_state in live_states.items():
            center_y = row_centers[row_key]
            canvas.create_oval(
                marker_x - 5,
                center_y - 5,
                marker_x + 5,
                center_y + 5,
                fill=state_colors[live_state],
                outline=COLOR_TEXT_PRIMARY,
                width=1,
            )

        discharge_active = bool(data.get("network_discharge", {}).get("active"))
        priority_active = any(
            node.get("priority_state", "NORMAL") != "NORMAL"
            for node in nodes.values()
            if isinstance(node, dict)
        )
        all_signals = [
            signal
            for node in nodes.values()
            if isinstance(node, dict)
            for signal in node.get("signals", {}).values()
        ]
        if discharge_active:
            status_text = "NETWORK DISCHARGE"
            status_color = COLOR_WARNING
        elif all_signals and all(signal == "RED" for signal in all_signals):
            status_text = "ALL RED ACTIVE"
            status_color = COLOR_DANGER
        elif priority_active:
            status_text = "PRIORITY OVERRIDE"
            status_color = COLOR_ACCENT
        else:
            status_text = "NORMAL PLAN"
            status_color = COLOR_SUCCESS
        if hasattr(self, "phase_status_lbl"):
            self.phase_status_lbl.config(text=status_text, fg=status_color)

    def draw_intersection(self, canvas, node_key, phase, signals=None):
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 10 or height < 10:
            return
        center_x, center_y = width // 2, height // 2
        road_w = 40
        canvas.create_rectangle(0, center_y - road_w // 2, width, center_y + road_w // 2, fill="#333D50", outline="")
        canvas.create_rectangle(center_x - road_w // 2, 0, center_x + road_w // 2, height, fill="#333D50", outline="")
        ew_color, ns_color = COLOR_DANGER, COLOR_DANGER
        if phase == "EW_GREEN":
            ew_color = COLOR_SUCCESS
        elif phase in ("EW_YELLOW", "CONFLICT_YELLOW"):
            ew_color = COLOR_WARNING
            ns_color = COLOR_WARNING
        elif phase == "NS_GREEN":
            ns_color = COLOR_SUCCESS
        elif phase == "NS_YELLOW":
            ns_color = COLOR_WARNING
        elif phase in ("PRIORITY_ACTIVE", "PRIORITY_CLEARING"):
            pass
        color_by_state = {
            "RED": COLOR_DANGER,
            "YELLOW": COLOR_WARNING,
            "GREEN": COLOR_SUCCESS,
        }
        signals = signals or {}
        eb_color = color_by_state.get(signals.get("EB"), ew_color)
        wb_color = color_by_state.get(signals.get("WB"), ew_color)
        nb_color = color_by_state.get(signals.get("NB"), ns_color)
        sb_color = color_by_state.get(signals.get("SB"), ns_color)
        radius = 8
        canvas.create_oval(center_x - road_w - radius, center_y - radius, center_x - road_w + radius, center_y + radius, fill=eb_color)
        canvas.create_oval(center_x + road_w - radius, center_y - radius, center_x + road_w + radius, center_y + radius, fill=wb_color)
        canvas.create_oval(center_x - radius, center_y - road_w - radius, center_x + radius, center_y - road_w + radius, fill=nb_color)
        canvas.create_oval(center_x - radius, center_y + road_w - radius, center_x + radius, center_y + road_w + radius, fill=sb_color)
        canvas.create_text(center_x, height - 15, text=phase.replace("_", " "), fill=COLOR_TEXT_PRIMARY, font=(FONT_FAMILY, 9, "bold"))

    def safe_read_telemetry(self):
        self.last_read_error = None
        if not TELEMETRY_FILE.exists():
            return None
        try:
            with TELEMETRY_FILE.open("r", encoding="utf-8") as telemetry_file:
                data = json.load(telemetry_file)
            if not isinstance(data, dict):
                raise ValueError("Telemetry root must be an object")
            if not isinstance(data.get("timestamp"), (int, float)):
                raise ValueError("Telemetry timestamp is missing or invalid")
            if not isinstance(data.get("network_summary"), dict):
                raise ValueError("Telemetry network_summary is missing or invalid")
            return data
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.last_read_error = str(exc)
            return None

    @staticmethod
    def classify_status(data, now=None, stale_after=STALE_AFTER_SECONDS):
        if not data:
            return "CONNECTING"
        now = time.time() if now is None else now
        age = max(0.0, now - float(data.get("timestamp", 0.0)))
        if age > stale_after:
            return "STALE"
        if data.get("simulation_paused", False):
            return "PAUSED"
        return "LIVE"

    def poll_telemetry(self):
        try:
            data = self.safe_read_telemetry()
            status = self.classify_status(data)
            if status == "LIVE":
                self.status_lbl.config(text="LIVE", fg=COLOR_SUCCESS)
                self.record_history_sample(data)
                self.update_metrics(data)
            elif status == "PAUSED":
                self.status_lbl.config(text="PAUSED", fg=COLOR_WARNING)
                self.update_metrics(data)
            elif status == "STALE":
                self.status_lbl.config(text="STALE DATA", fg=COLOR_DANGER)
                self.update_metrics(data)
            elif self.last_read_error:
                self.status_lbl.config(text="TELEMETRY ERROR", fg=COLOR_DANGER)
            else:
                self.status_lbl.config(text="CONNECTING...", fg=COLOR_WARNING)
        except Exception as exc:
            self.last_read_error = str(exc)
            self.status_lbl.config(text="TELEMETRY ERROR", fg=COLOR_DANGER)
        finally:
            self.root.after(250, self.poll_telemetry)

    def clear_history(self):
        """Clear this dashboard process's samples without touching telemetry."""
        for values in self.history.values():
            values.clear()
        self.queue_history.clear()
        self.last_seen_frame = None
        self.last_seen_simulation_time = None
        self.last_sample_time = None
        self.draw_trend_charts()

    def record_history_sample(self, data):
        """Record one bounded sample when simulated time advances sufficiently.

        Repeated telemetry reads at the same simulation frame are ignored. A
        backwards frame/time jump is treated as a simulation reset and starts a
        new session history rather than joining unrelated runs on one chart.
        """
        frame = int(data.get("frame_number", 0))
        simulation_time = float(data.get("simulation_time_seconds", frame / 60.0))
        reset_detected = (
            self.last_seen_frame is not None
            and (
                frame < self.last_seen_frame
                or simulation_time < self.last_seen_simulation_time
            )
        )
        if reset_detected:
            self.clear_history()
        elif self.last_seen_frame is not None and frame <= self.last_seen_frame:
            return False

        self.last_seen_frame = frame
        self.last_seen_simulation_time = simulation_time
        if (
            self.last_sample_time is not None
            and simulation_time - self.last_sample_time < HISTORY_SAMPLE_SECONDS
        ):
            return False

        summary = data.get("network_summary", {})
        total_vehicles = int(summary.get("total_vehicles", 0))
        total_buses = int(summary.get("total_buses", 0))
        total_queued = sum(summary.get("queues", {}).values())
        pending_demand = int(summary.get("pending_demand", 0))
        congestion = total_queued / total_vehicles * 100 if total_vehicles else 0.0
        sample = {
            "time": simulation_time,
            "vehicles": total_vehicles,
            "buses": total_buses,
            "road_queue": total_queued,
            "pending_demand": pending_demand,
            "congestion": congestion,
        }
        for key, value in sample.items():
            self.history[key].append(value)
        self.queue_history.append(total_queued)
        self.last_sample_time = simulation_time
        self.draw_trend_charts()
        return True

    @staticmethod
    def _nice_ceiling(value):
        if value <= 1:
            return 1.0
        magnitude = 10 ** math.floor(math.log10(value))
        normalized = value / magnitude
        for step in (1, 2, 5, 10):
            if normalized <= step:
                return float(step * magnitude)
        return float(10 * magnitude)

    def draw_trend_charts(self):
        for chart in getattr(self, "trend_charts", []):
            self.draw_line_chart(
                chart["canvas"],
                chart["series"],
                fixed_max=chart["fixed_max"],
            )

    def draw_line_chart(self, canvas, series, fixed_max=None):
        """Render a compact multi-series time chart on a standard Tk canvas."""
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 80 or height < 70:
            return

        left, right, top, bottom = 48, 14, 25, 24
        plot_width = max(1, width - left - right)
        plot_height = max(1, height - top - bottom)
        times = list(self.history["time"])
        if not times:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Waiting for live samples...",
                fill=COLOR_TEXT_SECONDARY,
                font=(FONT_FAMILY, 9),
            )
            return

        all_values = [
            float(value)
            for key, _label, _color in series
            for value in self.history[key]
        ]
        y_max = (
            float(fixed_max)
            if fixed_max is not None
            else self._nice_ceiling(max(all_values, default=1.0))
        )
        y_max = max(1.0, y_max)
        time_min, time_max = times[0], times[-1]
        time_span = max(HISTORY_SAMPLE_SECONDS, time_max - time_min)

        for index in range(5):
            fraction = index / 4
            y = top + fraction * plot_height
            value = y_max * (1.0 - fraction)
            canvas.create_line(
                left,
                y,
                width - right,
                y,
                fill=COLOR_CARD_BORDER,
                width=1,
            )
            label = f"{value:.0f}" if y_max >= 10 else f"{value:.1f}"
            canvas.create_text(
                left - 6,
                y,
                text=label,
                anchor="e",
                fill=COLOR_TEXT_SECONDARY,
                font=(FONT_FAMILY, 7),
            )

        canvas.create_text(
            left,
            height - 7,
            text=f"{time_min:.1f}s",
            anchor="w",
            fill=COLOR_TEXT_SECONDARY,
            font=(FONT_FAMILY, 7),
        )
        canvas.create_text(
            width - right,
            height - 7,
            text=f"{time_max:.1f}s",
            anchor="e",
            fill=COLOR_TEXT_SECONDARY,
            font=(FONT_FAMILY, 7),
        )

        legend_x = left
        for key, label, color in series:
            values = list(self.history[key])
            canvas.create_line(
                legend_x,
                11,
                legend_x + 12,
                11,
                fill=color,
                width=3,
            )
            legend_text = f"{label}: {values[-1]:.1f}" if values else label
            legend_item = canvas.create_text(
                legend_x + 17,
                11,
                text=legend_text,
                anchor="w",
                fill=COLOR_TEXT_PRIMARY,
                font=(FONT_FAMILY, 8),
            )
            bounds = canvas.bbox(legend_item)
            legend_x = (bounds[2] + 22) if bounds else legend_x + 130

            points = []
            for sample_time, value in zip(times, values):
                x = left + (sample_time - time_min) / time_span * plot_width
                y = top + (1.0 - min(max(float(value), 0.0), y_max) / y_max) * plot_height
                points.extend((x, y))
            if len(points) >= 4:
                canvas.create_line(
                    *points,
                    fill=color,
                    width=2,
                    smooth=True,
                    splinesteps=12,
                )
            elif len(points) == 2:
                x, y = points
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")

    def update_metrics(self, data):
        frame = int(data.get("frame_number", 0))
        summary = data.get("network_summary", {})
        total_vehicles = int(summary.get("total_vehicles", 0))
        total_buses = int(summary.get("total_buses", 0))
        total_queued = sum(summary.get("queues", {}).values())
        pending_demand = int(summary.get("pending_demand", 0))
        passengers = int(summary.get("passenger_volume", 0))
        congestion = total_queued / total_vehicles * 100 if total_vehicles else 0.0
        average_delay = (
            sum(self.queue_history) / len(self.queue_history)
            if self.queue_history
            else 0.0
        )
        buses = data.get("active_buses", data.get("approaching_buses", []))
        active_tsp = sum(bool(bus.get("tsp_active_triggered")) for bus in buses)
        active_dbl = sum(bool(bus.get("dbl_active_triggered")) for bus in buses)
        pending_tsp = sum(bool(bus.get("tsp_priority_pending")) for bus in buses)
        pending_dbl = sum(bool(bus.get("dbl_priority_pending")) for bus in buses)

        self.vars["vehicles"].config(text=str(total_vehicles))
        self.vars["buses"].config(text=str(total_buses))
        self.vars["passengers"].config(text=str(passengers))
        self.vars["queued"].config(text=f"{total_queued}/{pending_demand}")
        self.vars["delay"].config(text=f"{average_delay:.1f} v")
        self.vars["congestion"].config(text=f"{congestion:.1f}%")
        tsp_color = COLOR_SUCCESS if active_tsp else (COLOR_WARNING if pending_tsp else COLOR_ACCENT)
        dbl_color = COLOR_SUCCESS if active_dbl else (COLOR_WARNING if pending_dbl else COLOR_ACCENT)
        self.vars["tsp"].config(text=f"{active_tsp}/{pending_tsp}", fg=tsp_color)
        self.vars["dbl"].config(text=f"{active_dbl}/{pending_dbl}", fg=dbl_color)
        simulation_time = float(data.get("simulation_time_seconds", frame / 60.0))
        self.vars["timer"].config(text=f"{simulation_time:.1f} s")

        if hasattr(self, "discharge_status_lbl"):
            display = self.format_discharge_status(data.get("network_discharge", {}))
            self.discharge_selected_lbl.config(text=display["selected"])
            self.discharge_status_lbl.config(
                text=display["status"], fg=display["status_color"]
            )
            self.discharge_reason_lbl.config(text=display["reason"])
            self.discharge_recommendation_lbl.config(
                text=display["recommendation"]
            )

        nodes = data.get("signal_state", {}).get("nodes", {})
        fallback = data.get("signal_state", {}).get("current_phase", "UNKNOWN")
        phase_a = nodes.get("300", {}).get("phase", fallback)
        phase_b = nodes.get("700", {}).get("phase", fallback)
        self.latest_telemetry = data
        self.root.update_idletasks()
        if hasattr(self, "phase_cycle_canvas"):
            self.draw_phase_cycle(data)
        self.draw_intersection(self.node_a_canvas, "A", phase_a, nodes.get("300", {}).get("signals"))
        self.draw_intersection(self.node_b_canvas, "B", phase_b, nodes.get("700", {}).get("signals"))

    @staticmethod
    def format_discharge_status(status):
        status = status if isinstance(status, dict) else {}
        selected = status.get("selected", "Auto (Recommended)")
        state = status.get("status", "IDLE")
        stage = status.get("stage", "")
        discharged = int(status.get("vehicles_discharged", 0))
        reason = status.get("reason", "Normal signal control is active")
        recommendation = status.get(
            "recommendation", "Select Auto or a corridor, then start discharge"
        )
        status_line = f"Status: {state}"
        if stage:
            status_line += f"  |  Stage: {stage}"
        if discharged:
            status_line += f"  |  Discharged: {discharged}"
        status_color = {
            "DISCHARGING": COLOR_SUCCESS,
            "WAITING": COLOR_WARNING,
            "REQUESTED": COLOR_WARNING,
            "TRANSITIONING": COLOR_WARNING,
            "STOPPING": COLOR_WARNING,
            "COMPLETED": COLOR_SUCCESS,
        }.get(state, COLOR_TEXT_SECONDARY)
        return {
            "selected": f"Selected: {selected}",
            "status": status_line,
            "status_color": status_color,
            "reason": f"Reason: {reason}",
            "recommendation": f"Recommended first action: {recommendation}",
        }


if __name__ == "__main__":
    root = tk.Tk()
    TelemetryDashboard(root)
    root.mainloop()
```
