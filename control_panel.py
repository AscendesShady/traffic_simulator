# control_panel.py
import json
import os
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import ttk
import tempfile
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
PAGE_GUTTER = SPACE_MD       # column edge -> card edge
CARD_PAD = SPACE_MD          # card edge -> content
ROW_GAP = SPACE_XS           # between rows inside a card
BUTTON_PAD_Y = 7             # action buttons: ~32px tall at 9pt
CHIP_PAD_Y = 4               # toggle chips: ~26px tall
# Shared column grid inside a card: label | value | control. Fixed character
# widths keep every slider starting on the same vertical line.
LABEL_COLUMN_CHARS = 15
VALUE_COLUMN_CHARS = 7

BASE_DIR = Path(__file__).resolve().parent
AI_CONTROL_PATH = BASE_DIR / "ai_control.json"

API_MODEL_REGISTRY = {
    "GEMINI_API_KEY": [
        "gemini-2.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-pro",
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

CONTROL_PANEL_MIN_HEIGHT = 360
CONTROL_PANEL_BOTTOM_MARGIN = 40
# Standalone-window width and the widest a wrapped status line may run.
# Both sized for the portrait column this panel now lays itself out as,
# whether it owns its own window or is mounted into MainWindow's left pane.
CONTROL_PANEL_WIDTH = 520
PORTRAIT_WRAP_LENGTH = 320
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

# Live widget references used by the periodic repaint poller. The data in
# bus_routes_config remains authoritative whether a human or the LLM changed it.
route_flag_buttons = {}
# Collapsible sections by title: {"card", "header", "body", "state", "toggle"}.
# Filled by make_section so callers can expand/collapse without widget access.
control_panel_sections = {}

# ==========================================================
# SHARED STATE DICTIONARIES (Accessed by main.py)
# ==========================================================
global_config = {
    "is_paused": False,
    "sim_speed": 1.0,        # 0.5x to 3.0x speed multiplier
    "green_time": 240,       # Signal green phase duration in frames
    "random_seed": None,     # None = OS entropy; int = reproducible traffic
    "priority_eligibility_px": 500,  # Applied to SignalController on START/reset
    "vehicle_speed_scale": 0.5,      # Pending scale, applied on START/reset
    "_active_vehicle_speed_scale": 0.5,  # Runtime snapshot for this episode
    "reset_triggered": False,# Flag to wipe canvas vehicles
    "start_requested": False,# START requests a fresh run from frame zero
    "is_running": False,     # Sim launches idle; START begins a fresh run
    "run_has_started": False,# Distinguishes launch-idle from a completed STOP
    "test_duration_sim_seconds": None,  # None = free run; int = auto-stop sim-time
    "test_running": False,   # True while a timed benchmark run is active
    "test_model": "None",    # Model captured when the test started
    "test_seed": None,       # Seed captured when the test started
    "test_last_export": "",  # Filename written by the last completed test
    "sim_time_seconds": 0.0, # Simulation clock, published by main.py
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
        "tick_seconds": 5,
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


def set_priority_eligibility_px(value):
    """Store the eligibility distance to apply on the next START/reset."""
    normalized = max(250, min(800, int(round(float(value)))))
    global_config["priority_eligibility_px"] = normalized
    return normalized


def set_vehicle_speed_scale(value):
    """Store the vehicle speed multiplier to apply on the next START/reset."""
    normalized = max(0.25, min(1.0, float(value)))
    global_config["vehicle_speed_scale"] = normalized
    return normalized


def describe_webster_timing():
    """Full per-node Webster diagnostics for the read-only panel status area."""
    if global_config.get("calibrating", False):
        return "Calibrating Webster, please wait...", "CALIBRATING"
    saturation = global_config.get("measured_saturation_flow")
    if not saturation:
        return "Webster timing calibrates on START", "IDLE"
    splits = global_config.get("webster_splits") or {}
    lines = []
    has_oversaturated_node = False
    for node_x, node_name in ((300, "A"), (700, "B")):
        split = splits.get(node_x) or splits.get(str(node_x))
        if not isinstance(split, dict):
            lines.append(f"NODE {node_x} ({node_name}): Webster timing unavailable")
            continue
        cycle = float(split.get("cycle_time_sec") or 0.0)
        total_y = float(split.get("Y") or 0.0)
        y_ew = float(split.get("y_ew") or 0.0)
        y_ns = float(split.get("y_ns") or 0.0)
        ew_green = float(split.get("EW_green_sec") or 0.0)
        ns_green = float(split.get("NS_green_sec") or 0.0)
        if split.get("oversaturated", False):
            has_oversaturated_node = True
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
    return "\n".join(lines), (
        "OVERSATURATED" if has_oversaturated_node else "READY"
    )


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
        button.config(
            text=on_text,
            bg=on_color,
            fg=COLOR_BG if on_color in (COLOR_SUCCESS, COLOR_WARNING) else COLOR_TEXT_PRIMARY,
            activebackground=on_color,
            activeforeground=COLOR_BG if on_color in (COLOR_SUCCESS, COLOR_WARNING) else COLOR_TEXT_PRIMARY,
        )
    else:
        button.config(
            text=off_text,
            bg=COLOR_CARD_BORDER,
            fg=COLOR_TEXT_SECONDARY,
            activebackground="#475569",
            activeforeground=COLOR_TEXT_PRIMARY,
        )
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
    parent, label_text, value_text, from_, to, value, command, step, style
):
    """Label | value | slider on the shared column grid.

    Returns (value_label, scale). The scale is keyboard-adjustable by
    exactly ``step`` per arrow press.
    """
    row = add_labeled_row(parent, label_text)
    value_label = make_label(
        row, value_text, bold=True, color=COLOR_ACCENT, width=VALUE_COLUMN_CHARS
    )
    value_label.pack(side="left")
    scale = ttk.Scale(
        row, from_=from_, to=to, value=value, style=style, command=command
    )
    scale.pack(side="left", fill="x", expand=True, padx=(SPACE_XS, 0))
    enable_scale_keyboard(scale, step=step)
    return value_label, scale


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
    card.pack(fill="x", padx=PAGE_GUTTER, pady=(0, SPACE_SM))
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
        pady=SPACE_SM,
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


def write_ai_control(path=None):
    """Atomically mirror in-process AI controls for the agent subprocess."""
    runtime = global_config["ai_runtime"]
    payload = {
        "armed": bool(runtime.get("armed", False)),
        "model": str(runtime.get("model", "None")),
        "tick_seconds": min(15, max(2, int(runtime.get("tick_seconds", 5)))),
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


def set_active_ai_model(model, other_selector=None, persist=True):
    """Select exactly one local/API model and persist the shared model ID."""
    selected_model = str(model or "None")
    if other_selector is not None:
        other_selector.set("None")
    runtime = global_config["ai_runtime"]
    runtime["model"] = selected_model
    if runtime.get("armed", False):
        runtime["last_status"] = (
            BASELINE_NO_MODEL
            if selected_model == "None"
            else "MODEL_CHANGED_WAITING"
        )
    if persist:
        write_ai_control()
    return selected_model

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

    Layout: one portrait column of collapsible cards, each built by
    ``make_section`` so every header, row, button and toggle shares the same
    type scale and spacing grid. Run Controls, Benchmark and AI/LLM open by
    default; the long tuning and per-route/per-approach editors start
    collapsed.
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
        try:
            root.destroy()
        except Exception:
            pass
        sys.exit()

    if is_toplevel:
        root.protocol("WM_DELETE_WINDOW", on_close)

    style = ttk.Style()
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

    # One slider look for the whole panel; a focused slider swaps its bevel
    # to amber so the operator can see which control the arrow keys drive.
    for style_name, background in (
        ("Modern.Horizontal.TScale", COLOR_CARD_ALT),
        ("Global.Horizontal.TScale", COLOR_CARD),
    ):
        style.configure(
            style_name,
            background=background,
            troughcolor="#141822",
            slidercolor=COLOR_ACCENT,
            bordercolor=COLOR_CARD_BORDER,
            lightcolor=COLOR_ACCENT,
            darkcolor=COLOR_ACCENT,
            groovethickness=4,
            sliderthickness=14,
            sliderlength=22,
        )
        style.map(
            style_name,
            lightcolor=[("focus", COLOR_WARNING)],
            darkcolor=[("focus", COLOR_WARNING)],
            bordercolor=[("focus", COLOR_WARNING)],
        )

    # 1. HEADER & STATUS ---------------------------------------------------
    header_frame = tk.Frame(root, bg=COLOR_BG)
    header_frame.pack(fill="x", padx=PAGE_GUTTER, pady=(SPACE_MD, SPACE_SM))

    make_label(
        header_frame, "Network Control", font=FONT_TITLE,
        color=COLOR_TEXT_PRIMARY,
    ).pack(fill="x")

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

    # 2. RUN CONTROLS ------------------------------------------------------
    run_section = make_section(
        root, "Run Controls", expanded=True, on_toggle=schedule_panel_fit
    )
    run_body = run_section["body"]

    # START/PAUSE share a row, RESET gets its own full-width row below -- a
    # narrow side pane has no room for all three abreast.
    controls_row = tk.Frame(run_body, bg=COLOR_CARD)
    controls_row.pack(fill="x", pady=(0, SPACE_SM))
    controls_row.grid_columnconfigure(0, weight=1, uniform="run_buttons")
    controls_row.grid_columnconfigure(1, weight=1, uniform="run_buttons")

    def toggle_start_stop():
        request_start_stop()
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

    seed_row = add_labeled_row(run_body, "Seed")
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
    seed_entry.pack(side="left", padx=(0, SPACE_XS), ipady=3)

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
    seed_btn.pack(side="left", padx=(0, SPACE_SM))

    # Persistent readout of the seed actually in effect, so the operator can
    # confirm it at a glance instead of relying on the transient status line.
    seed_state_lbl = make_label(seed_row, "", bold=True, color=COLOR_SUCCESS)
    seed_state_lbl.pack(side="left", fill="x", expand=True)

    def refresh_seed_state_label():
        seed = global_config.get("random_seed")
        if seed is None:
            seed_state_lbl.config(text="random", fg=COLOR_TEXT_SECONDARY)
        else:
            seed_state_lbl.config(text=f"set to {seed}", fg=COLOR_SUCCESS)

    refresh_seed_state_label()
    seed_entry.bind("<Return>", apply_seed_from_entry)
    seed_entry.bind("<FocusOut>", apply_seed_from_entry)

    webster_status_lbl = make_label(
        run_body, "Webster timing calibrates on START", bold=True,
        color=COLOR_TEXT_SECONDARY, wraplength=PORTRAIT_WRAP_LENGTH,
    )
    webster_status_lbl.pack(fill="x", pady=(SPACE_XS, 0))

    def refresh_webster_status():
        text, state = describe_webster_timing()
        colour = {
            "CALIBRATING": COLOR_WARNING,
            "READY": COLOR_SUCCESS,
            "OVERSATURATED": COLOR_DANGER,
        }.get(state, COLOR_TEXT_SECONDARY)
        webster_status_lbl.config(text=text, fg=colour)
        root.after(150, refresh_webster_status)

    root.after(150, refresh_webster_status)

    # 3. BENCHMARK TEST ----------------------------------------------------
    test_section = make_section(
        root, "Benchmark Test", accent=COLOR_WARNING, expanded=True,
        on_toggle=schedule_panel_fit,
    )
    test_body = test_section["body"]

    duration_row = add_labeled_row(test_body, "Duration")
    test_duration_box = ttk.Combobox(
        duration_row, values=list(TEST_DURATIONS), width=8,
        state="readonly", style="Modern.TCombobox", font=FONT_BODY,
    )
    test_duration_box.pack(side="left")
    countdown_lbl = make_label(
        duration_row, "--:--", bold=True, color=COLOR_TEXT_SECONDARY,
        # Reserve the longest runtime state ("00:00 PAUSED") up front. Without
        # this width, Tk sizes the benchmark card for "--:--" and clips the
        # countdown when the label grows after a test starts.
        width=BENCHMARK_COUNTDOWN_WIDTH, anchor="e",
    )
    countdown_lbl.pack(side="right")

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
        root.after(200, refresh_test_countdown)

    root.after(200, refresh_test_countdown)

    def on_test_duration_selected(_event=None):
        global_config["test_duration_sim_seconds"] = TEST_DURATIONS.get(
            test_duration_box.get()
        )

    test_duration_box.bind("<<ComboboxSelected>>", on_test_duration_selected)

    def start_test():
        if request_start_test() is None:
            status_text.config(text="Select duration first", fg=COLOR_WARNING)
            return
        write_ai_control()

    start_test_btn = make_button(test_body, "Start test", "warning", start_test)
    start_test_btn.pack(fill="x", pady=(SPACE_XS, 0))

    def refresh_simulation_status():
        running = bool(global_config.get("is_running", False))
        starting = bool(global_config.get("start_requested", False))
        paused = bool(global_config.get("is_paused", False))
        testing = bool(global_config.get("test_running", False))
        if running:
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
            if starting:
                status_text.config(text="Starting fresh run…", fg=COLOR_WARNING)
            elif global_config.get("test_last_export"):
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
        root.after(100, refresh_simulation_status)

    root.after(100, refresh_simulation_status)

    # 4. MOTION / PRIORITY TUNING -----------------------------------------
    tuning_section = make_section(
        root, "Tuning", expanded=False, on_toggle=schedule_panel_fit
    )
    tuning_body = tuning_section["body"]

    def update_priority_eligibility(value):
        pixels = set_priority_eligibility_px(value)
        eligibility_value.config(text=f"{pixels} px")

    eligibility_value, eligibility_slider = add_slider_row(
        tuning_body, "Eligibility zone",
        f"{global_config['priority_eligibility_px']} px",
        250, 800, global_config["priority_eligibility_px"],
        update_priority_eligibility, step=10, style="Global.Horizontal.TScale",
    )

    def update_vehicle_scale(value):
        scale = set_vehicle_speed_scale(value)
        vehicle_scale_value.config(text=f"{scale:.2f}x")

    vehicle_scale_value, vehicle_scale_slider = add_slider_row(
        tuning_body, "Vehicle speed",
        f"{global_config['vehicle_speed_scale']:.2f}x",
        0.25, 1.0, global_config["vehicle_speed_scale"],
        update_vehicle_scale, step=0.05, style="Global.Horizontal.TScale",
    )

    make_label(
        tuning_body, "Applies on next START / RESET", color=COLOR_TEXT_SECONDARY,
    ).pack(fill="x")

    # 5. NETWORK GRIDLOCK RECOVERY ----------------------------------------
    recovery_section = make_section(
        root, "Gridlock Discharge", accent=COLOR_DANGER, expanded=False,
        on_toggle=schedule_panel_fit,
    )
    recovery_body = recovery_section["body"]

    corridor_row = add_labeled_row(recovery_body, "Corridor")
    discharge_mode_box = ttk.Combobox(
        corridor_row,
        values=list(DISCHARGE_OPTIONS),
        state="readonly",
        style="Modern.TCombobox", font=FONT_BODY,
    )
    discharge_mode_box.set(global_config["discharge_selection"])
    discharge_mode_box.pack(side="left", fill="x", expand=True)

    # The two actions take their own row under the corridor selector.
    recovery_actions = tk.Frame(recovery_body, bg=COLOR_CARD)
    recovery_actions.pack(fill="x", pady=(SPACE_XS, SPACE_SM))
    recovery_actions.grid_columnconfigure(0, weight=1, uniform="recovery_buttons")
    recovery_actions.grid_columnconfigure(1, weight=1, uniform="recovery_buttons")

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
    start_discharge_btn.grid(row=0, column=0, sticky="ew", padx=(0, SPACE_XS))

    def safe_stop_discharge():
        global_config["discharge_start_requested"] = False
        global_config["discharge_stop_requested"] = True

    stop_discharge_btn = make_button(
        recovery_actions, "Safe stop", "neutral", safe_stop_discharge
    )
    stop_discharge_btn.grid(row=0, column=1, sticky="ew", padx=(SPACE_XS, 0))

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
        root.after(250, refresh_discharge_status)

    root.after(250, refresh_discharge_status)

    # 6. AI / LLM CONTROL --------------------------------------------------
    ai_section = make_section(
        root, "AI / LLM Control", accent=COLOR_WARNING, expanded=True,
        on_toggle=schedule_panel_fit,
    )
    ai_body = ai_section["body"]

    available_models = get_ollama_models()
    available_api_models = get_api_models()
    selected_model = str(global_config["ai_runtime"].get("model", "None"))
    if selected_model in available_api_models and selected_model != "None":
        selected_local_model = "None"
        selected_api_model = selected_model
    elif selected_model in available_models:
        selected_local_model = selected_model
        selected_api_model = "None"
    else:
        selected_model = "None"
        selected_local_model = "None"
        selected_api_model = "None"
        global_config["ai_runtime"]["model"] = selected_model

    local_row = add_labeled_row(ai_body, "Local model")
    llm_engine_box = ttk.Combobox(
        local_row, values=available_models, state="readonly",
        style="Modern.TCombobox", font=FONT_BODY,
    )
    llm_engine_box.set(selected_local_model)
    llm_engine_box.pack(side="left", fill="x", expand=True)

    def on_llm_engine_selected(event):
        selected_model_name = set_active_ai_model(
            llm_engine_box.get(), api_engine_box, persist=False
        )
        selected_val_lbl.config(text=selected_model_name)
        write_ai_control()

    llm_engine_box.bind("<<ComboboxSelected>>", on_llm_engine_selected)

    api_row = add_labeled_row(ai_body, "API model")
    api_engine_box = ttk.Combobox(
        api_row, values=available_api_models, state="readonly",
        style="Modern.TCombobox", font=FONT_BODY,
    )
    api_engine_box.set(selected_api_model)
    api_engine_box.pack(side="left", fill="x", expand=True)

    def on_api_engine_selected(event):
        selected_model_name = set_active_ai_model(
            api_engine_box.get(), llm_engine_box, persist=False
        )
        selected_val_lbl.config(text=selected_model_name)
        write_ai_control()

    api_engine_box.bind("<<ComboboxSelected>>", on_api_engine_selected)

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
        tick_seconds = min(15, max(2, int(float(value))))
        global_config["ai_runtime"]["tick_seconds"] = tick_seconds
        tick_value_lbl.config(text=f"{tick_seconds}s")
        write_ai_control()

    tick_value_lbl, tick_slider = add_slider_row(
        ai_body, "Decision interval",
        f"{int(tick_runtime.get('tick_seconds', 5))}s",
        2, 15, tick_runtime.get("tick_seconds", 5), on_tick_seconds_changed,
        step=1, style="Global.Horizontal.TScale",
    )

    # Arm button and live status on their own row under the selectors.
    ai_row_run = tk.Frame(ai_body, bg=COLOR_CARD)
    ai_row_run.pack(fill="x", pady=(SPACE_XS, SPACE_SM))

    run_llm_btn = make_button(ai_row_run, f"{SYM_PLAY}  Run LLM", "warning", on_run_llm)
    run_llm_btn.pack(side="left", padx=(0, SPACE_SM))

    llm_dot = make_label(ai_row_run, SYM_DOT, color=COLOR_TEXT_SECONDARY)
    llm_dot.pack(side="left", padx=(0, SPACE_XS))
    llm_status_text = make_label(
        ai_row_run, "LLM INACTIVE", bold=True, color=COLOR_TEXT_SECONDARY,
        wraplength=PORTRAIT_WRAP_LENGTH - 140,
    )
    llm_status_text.pack(side="left", fill="x", expand=True)

    # Selected model and control scope, one line each.
    selected_row = add_labeled_row(ai_body, "Selected")
    selected_val_lbl = make_label(selected_row, selected_model, bold=True, color=COLOR_ACCENT)
    selected_val_lbl.pack(side="left", fill="x", expand=True)

    scope_row = add_labeled_row(ai_body, "Control scope", pady=0)
    ctrl_val_lbl = make_label(scope_row, "TSP + DBL", bold=True, color=COLOR_ACCENT)
    ctrl_val_lbl.pack(side="left")
    ctrl_rest_lbl = make_label(scope_row, " for all bus routes")
    ctrl_rest_lbl.pack(side="left")

    def refresh_llm_status():
        runtime = global_config.get("ai_runtime", {})
        armed = bool(runtime.get("armed", False))
        status = str(runtime.get("last_status", "INACTIVE"))
        turn = int(runtime.get("last_turn", 0))
        if not armed:
            color = COLOR_TEXT_SECONDARY
            status_text_value = "LLM INACTIVE"
            button_text = f"{SYM_PLAY}  Run LLM"
        elif status == BASELINE_NO_MODEL:
            color = COLOR_ACCENT
            status_text_value = "No LLM active - running on baseline"
            button_text = f"{SYM_STOP}  Disarm LLM"
        else:
            color = {
                "OK": COLOR_SUCCESS,
                "HELD_ALL_OFF": COLOR_DANGER,
                "INVALID_DECISION": COLOR_DANGER,
                "CONTROL_WRITE_ERROR": COLOR_DANGER,
            }.get(status, COLOR_WARNING)
            status_text_value = f"LLM {status}"
            if turn:
                status_text_value += f" | TURN {turn}"
            button_text = f"{SYM_STOP}  Disarm LLM"
        llm_dot.config(fg=color)
        llm_status_text.config(text=status_text_value, fg=color)
        run_llm_btn.config(text=button_text)
        root.after(250, refresh_llm_status)

    root.after(250, refresh_llm_status)

    def refresh_route_buttons():
        repaint_route_flag_buttons()
        root.after(250, refresh_route_buttons)

    root.after(250, refresh_route_buttons)

    # 7. BUS ROUTES (TSP / DBL) --------------------------------------------
    transit_section = make_section(
        root, "Bus Routes (TSP / DBL)", accent=COLOR_WARNING, expanded=False,
        on_toggle=schedule_panel_fit,
    )
    transit_body = transit_section["body"]

    dispatch_row = tk.Frame(transit_body, bg=COLOR_CARD)
    dispatch_row.pack(fill="x", pady=(0, SPACE_SM))

    disp_route_box = ttk.Combobox(
        dispatch_row, values=[r["name"] for r in bus_routes_config.values()],
        state="readonly", style="Modern.TCombobox", font=FONT_BODY,
    )
    disp_route_box.set(bus_routes_config["R1_EB_A_NB"]["name"])
    disp_route_box.pack(side="left", fill="x", expand=True, padx=(0, SPACE_SM))

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
    manual_btn.pack(side="left")

    # One stacked card per route. Row 1 = name + ON/OFF chip, row 2 = headway
    # slider, row 3 = the TSP and DBL chips as two equal halves. Every
    # callback and bus_routes_config key is the same as before.
    for r_id, r_cfg in bus_routes_config.items():
        row_frame = tk.Frame(
            transit_body, bg=COLOR_CARD_ALT, highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1, bd=0,
        )
        row_frame.pack(fill="x", pady=(0, SPACE_SM))
        inner = tk.Frame(row_frame, bg=COLOR_CARD_ALT)
        inner.pack(fill="x", padx=SPACE_SM, pady=SPACE_SM)

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
            inner, "Headway", f"{r_cfg['headway_sec']}s", 0, 90,
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

    # 8. PER-APPROACH TRAFFIC ----------------------------------------------
    approaches_section = make_section(
        root, "Approach Traffic", expanded=False, on_toggle=schedule_panel_fit
    )
    approaches_body = approaches_section["body"]

    # One stacked card per approach. Row 1 = name + ON/OFF chip, row 2 =
    # generation model, then one slider row each for inflow, straight % and
    # trucks %. Same callbacks and approach_configs keys as before.
    for key, name in APPROACH_NAMES.items():
        card = tk.Frame(
            approaches_body, bg=COLOR_CARD_ALT, highlightbackground=COLOR_CARD_BORDER,
            highlightthickness=1, bd=0,
        )
        card.pack(fill="x", pady=(0, SPACE_SM))
        inner = tk.Frame(card, bg=COLOR_CARD_ALT)
        inner.pack(fill="x", padx=SPACE_SM, pady=SPACE_SM)

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

        model_row = add_labeled_row(inner, "Model")
        model_box = ttk.Combobox(
            model_row,
            values=[
                "Random",
                "Poisson",
                "Binomial",
                "Neg Binomial",
                "Congestion Peak",
            ],
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

        def make_rate_slider(k, lbl):
            def update(val):
                v = int(float(val))
                approach_configs[k]["rate"] = v
                lbl.config(text=f"{v} v/m")
            return update

        rate_val, rate_slider = add_slider_row(
            inner, "Inflow", f"{approach_configs[key]['rate']} v/m", 1, 30,
            approach_configs[key]["rate"], None, step=1,
            style="Modern.Horizontal.TScale",
        )
        rate_slider.config(command=make_rate_slider(key, rate_val))

        def make_split_slider(k, lbl):
            def update(val):
                v_f = float(val) / 100.0
                approach_configs[k]["turn_split"] = v_f
                lbl.config(text=f"{int(v_f*100)}%")
            return update

        split_val, split_slider = add_slider_row(
            inner, "Straight", f"{int(approach_configs[key]['turn_split']*100)}%",
            0, 100, int(approach_configs[key]["turn_split"] * 100), None, step=1,
            style="Modern.Horizontal.TScale",
        )
        split_slider.config(command=make_split_slider(key, split_val))

        def make_heavy_slider(k, lbl):
            def update(val):
                v_f = float(val) / 100.0
                approach_configs[k]["heavy_ratio"] = v_f
                lbl.config(text=f"{int(v_f*100)}%")
            return update

        heavy_val, heavy_slider = add_slider_row(
            inner, "Trucks", f"{int(approach_configs[key]['heavy_ratio']*100)}%",
            0, 50, int(approach_configs[key]["heavy_ratio"] * 100), None, step=1,
            style="Modern.Horizontal.TScale",
        )
        heavy_slider.config(command=make_heavy_slider(key, heavy_val))

    # Collapsed sections keep every widget alive with its value and callback;
    # only the body frames are unmapped.
    schedule_panel_fit()
    return root
