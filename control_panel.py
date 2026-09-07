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

# ==========================================================
# SHARED STATE DICTIONARIES (Accessed by main.py)
# ==========================================================
global_config = {
    "is_paused": False,
    "sim_speed": 1.0,        # 0.5x to 3.0x speed multiplier
    "green_time": 240,       # Signal green phase duration in frames
    "random_seed": None,     # None = OS entropy; int = reproducible traffic
    "reset_triggered": False,# Flag to wipe canvas vehicles
    "start_requested": False,# START requests a fresh run from frame zero
    "is_running": False,     # Sim launches idle; START begins a fresh run
    "run_has_started": False,# Distinguishes launch-idle from a completed STOP
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


def request_start_stop():
    """Request a fresh START, or STOP the active run without clearing data."""
    if global_config.get("is_running", False):
        global_config["is_running"] = False
        global_config["start_requested"] = False
        return "STOPPED"
    global_config["start_requested"] = True
    return "START_REQUESTED"


def request_pause_resume():
    """Toggle an active run's pause without changing its run lifecycle."""
    if not global_config.get("is_running", False):
        return bool(global_config.get("is_paused", False))
    global_config["is_paused"] = not global_config.get("is_paused", False)
    return global_config["is_paused"]

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
    """Repaint TSP/DBL controls from the authoritative route configuration."""
    for route_id, buttons in route_flag_buttons.items():
        config = bus_routes_config.get(route_id, {})
        tsp_on = bool(config.get("tsp_enabled", False))
        dbl_on = bool(config.get("dbl_enabled", False))
        buttons["tsp"].config(
            text="TSP ACTIVE" if tsp_on else "TSP OFF",
            fg=COLOR_SUCCESS if tsp_on else COLOR_DANGER,
        )
        buttons["dbl"].config(
            text="DBL ACTIVE" if dbl_on else "DBL OFF",
            fg=COLOR_ACCENT if dbl_on else COLOR_DANGER,
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


def set_active_ai_model(model, other_selector=None, persist=True):
    """Select exactly one local/API model and persist the shared model ID."""
    selected_model = str(model or "None")
    if other_selector is not None:
        other_selector.set("None")
    runtime = global_config["ai_runtime"]
    runtime["model"] = selected_model
    if runtime.get("armed", False):
        runtime["last_status"] = "MODEL_CHANGED_WAITING"
    if persist:
        write_ai_control()
    return selected_model

def create_dashboard_window():
    route_flag_buttons.clear()
    root = tk.Tk()
    root.title("Traffic & Transit Control Dashboard")
    # Sized to the layout's natural width so no card is clipped. Kept at or
    # below 890 px: main.calculate_startup_window_layout only tiles the canvas
    # beside this window while the remaining desktop width stays >= 1000 px.
    root.geometry("880x1030")
    # Use normal desktop stacking. Forced topmost made the Pygame canvas slide
    # underneath this window and also made focus/drag interaction feel sticky.
    root.attributes("-topmost", False)
    root.configure(bg=COLOR_BG)
    write_ai_control()

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
    header_frame.pack(fill="x", padx=20, pady=(14, 6))

    title_label = tk.Label(
        header_frame, text="NETWORK CONTROL DASHBOARD",
        font=(FONT_FAMILY, 15, "bold"), bg=COLOR_BG, fg=COLOR_TEXT_PRIMARY
    )
    title_label.pack(side="left")

    status_badge = tk.Frame(header_frame, bg=COLOR_BG)
    status_badge.pack(side="right")

    dot_lbl = tk.Label(status_badge, text=SYM_DOT, font=(FONT_FAMILY, 11), bg=COLOR_BG, fg=COLOR_TEXT_SECONDARY)
    dot_lbl.pack(side="left", padx=(0, 4))

    status_text = tk.Label(
        status_badge, text="Idle — press START", font=(FONT_FAMILY, 10, "bold"),
        bg=COLOR_BG, fg=COLOR_TEXT_SECONDARY
    )
    status_text.pack(side="left")

    # 2. GLOBAL SIMULATION CONTROLS CARD
    global_card = tk.Frame(
        root, bg=COLOR_CARD, highlightbackground=COLOR_CARD_BORDER,
        highlightthickness=1, bd=0
    )
    global_card.pack(fill="x", padx=20, pady=5, ipady=4)

    g_title = tk.Label(
        global_card, text="Global Simulation Controls",
        font=(FONT_FAMILY, 11, "bold"), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY
    )
    g_title.pack(anchor="w", padx=16, pady=(7, 5))

    controls_row = tk.Frame(global_card, bg=COLOR_CARD)
    controls_row.pack(fill="x", padx=16, pady=(0, 8))

    def toggle_start_stop():
        request_start_stop()
        write_ai_control()

    start_stop_btn = tk.Button(
        controls_row, text=f"{SYM_PLAY} START", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_SUCCESS, fg=COLOR_TEXT_PRIMARY, activebackground="#24A35A",
        activeforeground=COLOR_TEXT_PRIMARY, bd=0, padx=12, pady=4,
        cursor="hand2", relief="flat", command=toggle_start_stop
    )
    start_stop_btn.pack(side="left", padx=(0, 8))

    def toggle_pause():
        request_pause_resume()

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
    reset_btn.pack(side="left", padx=(0, 10))

    def refresh_simulation_status():
        running = bool(global_config.get("is_running", False))
        starting = bool(global_config.get("start_requested", False))
        paused = bool(global_config.get("is_paused", False))
        if running:
            start_stop_btn.config(text=f"{SYM_STOP} STOP", bg=COLOR_DANGER)
            pause_btn.config(state="normal")
            if paused:
                pause_btn.config(text=f"{SYM_PLAY} RESUME", bg=COLOR_SUCCESS)
                dot_lbl.config(fg=COLOR_WARNING)
                status_text.config(text="Paused", fg=COLOR_WARNING)
            else:
                pause_btn.config(text=f"{SYM_PAUSE} PAUSE", bg=COLOR_ACCENT)
                dot_lbl.config(fg=COLOR_SUCCESS)
                status_text.config(text="Running", fg=COLOR_SUCCESS)
        else:
            start_stop_btn.config(text=f"{SYM_PLAY} START", bg=COLOR_SUCCESS)
            pause_btn.config(
                state="disabled", text=f"{SYM_PAUSE} PAUSE", bg=COLOR_CARD_BORDER
            )
            dot_lbl.config(fg=COLOR_TEXT_SECONDARY)
            if starting:
                status_text.config(text="Starting fresh run…", fg=COLOR_WARNING)
            elif global_config.get("run_has_started", False):
                status_text.config(
                    text="Stopped — export or START new run",
                    fg=COLOR_TEXT_SECONDARY,
                )
            else:
                status_text.config(
                    text="Idle — press START", fg=COLOR_TEXT_SECONDARY
                )
        root.after(100, refresh_simulation_status)

    root.after(100, refresh_simulation_status)

    # Run parameters sit on their own row. Sharing one row with the action
    # buttons required 806 px inside a 748 px card, which clipped the seed
    # control off the right edge.
    params_row = tk.Frame(global_card, bg=COLOR_CARD)
    params_row.pack(fill="x", padx=16, pady=(0, 6))

    green_group = tk.Frame(params_row, bg=COLOR_CARD)
    green_group.pack(side="left", padx=(0, 28))

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

    speed_group = tk.Frame(params_row, bg=COLOR_CARD)
    speed_group.pack(side="left", padx=(0, 28))

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

    seed_group = tk.Frame(params_row, bg=COLOR_CARD)
    seed_group.pack(side="left")

    tk.Label(
        seed_group, text="Seed:", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_CARD, fg=COLOR_TEXT_SECONDARY
    ).pack(side="left", padx=(0, 4))

    seed_entry = tk.Entry(
        seed_group, width=8, font=(FONT_FAMILY, 8),
        bg=COLOR_CARD_ALT, fg=COLOR_TEXT_PRIMARY,
        insertbackground=COLOR_TEXT_PRIMARY, relief="flat"
    )
    configured_seed = global_config.get("random_seed")
    if configured_seed is not None:
        seed_entry.insert(0, str(configured_seed))
    seed_entry.pack(side="left", padx=(0, 4), ipady=2)

    def apply_seed_from_entry(_event=None):
        try:
            seed = set_random_seed(seed_entry.get())
        except (TypeError, ValueError):
            status_text.config(text="Seed must be an integer", fg=COLOR_DANGER)
            return
        if seed is None:
            status_text.config(
                text="Random seed cleared; applies on reset", fg=COLOR_TEXT_SECONDARY
            )
        else:
            status_text.config(
                text=f"Seed {seed} set; applies on reset", fg=COLOR_SUCCESS
            )

    seed_btn = tk.Button(
        seed_group, text="SET", font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_CARD_BORDER, fg=COLOR_TEXT_PRIMARY,
        activebackground="#475569", activeforeground=COLOR_TEXT_PRIMARY,
        bd=0, padx=7, pady=2, cursor="hand2", relief="flat",
        command=apply_seed_from_entry,
    )
    seed_btn.pack(side="left")
    seed_entry.bind("<Return>", apply_seed_from_entry)
    seed_entry.bind("<FocusOut>", apply_seed_from_entry)

    # 2.25 NETWORK GRIDLOCK RECOVERY CARD
    recovery_card = tk.Frame(
        root, bg=COLOR_CARD, highlightbackground=COLOR_DANGER,
        highlightthickness=1, bd=0
    )
    recovery_card.pack(fill="x", padx=20, pady=5)

    recovery_controls = tk.Frame(recovery_card, bg=COLOR_CARD)
    recovery_controls.pack(fill="x", padx=16, pady=(3, 3))

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
    ai_card.pack(fill="x", padx=20, pady=5, ipady=4)

    ai_title = tk.Label(
        ai_card, text="AI / LLM CONTROL",
        font=(FONT_FAMILY, 11, "bold"), bg=COLOR_CARD, fg=COLOR_WARNING
    )
    ai_title.pack(anchor="w", padx=16, pady=(7, 5))

    # First row: Controls
    ai_row1 = tk.Frame(ai_card, bg=COLOR_CARD)
    ai_row1.pack(fill="x", padx=16, pady=(0, 5))

    llm_lbl = tk.Label(ai_row1, text="Local", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY)
    llm_lbl.pack(side="left", padx=(0, 8))

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

    llm_engine_box = ttk.Combobox(
        ai_row1,
        values=available_models,
        width=22, state="readonly", style="Modern.TCombobox"
    )
    llm_engine_box.set(selected_local_model)
    llm_engine_box.pack(side="left", padx=(0, 16))

    def on_llm_engine_selected(event):
        selected_model_name = set_active_ai_model(
            llm_engine_box.get(), api_engine_box, persist=False
        )
        selected_val_lbl.config(text=selected_model_name)
        write_ai_control()

    llm_engine_box.bind("<<ComboboxSelected>>", on_llm_engine_selected)

    api_lbl = tk.Label(
        ai_row1,
        text="API",
        font=(FONT_FAMILY, 9),
        bg=COLOR_CARD,
        fg=COLOR_TEXT_PRIMARY,
    )
    api_lbl.pack(side="left", padx=(0, 8))

    api_engine_box = ttk.Combobox(
        ai_row1,
        values=available_api_models,
        width=22,
        state="readonly",
        style="Modern.TCombobox",
    )
    api_engine_box.set(selected_api_model)
    api_engine_box.pack(side="left", padx=(0, 16))

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
        runtime["last_status"] = (
            "WAITING_FOR_DECISION" if runtime["armed"] else "INACTIVE"
        )
        if runtime["armed"]:
            runtime["last_turn"] = 0
        write_ai_control()

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
    ai_row2.pack(fill="x", padx=16, pady=(0, 6))

    sel_lbl = tk.Label(ai_row2, text="Selected: ", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY)
    sel_lbl.pack(side="left")
    
    selected_val_lbl = tk.Label(ai_row2, text=selected_model, font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_ACCENT)
    selected_val_lbl.pack(side="left")

    tick_runtime = global_config["ai_runtime"]
    tick_value_lbl = tk.Label(
        ai_row2,
        text=f"{int(tick_runtime.get('tick_seconds', 5))}s",
        font=(FONT_FAMILY, 8, "bold"),
        bg=COLOR_CARD,
        fg=COLOR_ACCENT,
        width=4,
    )
    tick_value_lbl.pack(side="right")

    def on_tick_seconds_changed(value):
        tick_seconds = min(15, max(2, int(float(value))))
        global_config["ai_runtime"]["tick_seconds"] = tick_seconds
        tick_value_lbl.config(text=f"{tick_seconds}s")
        write_ai_control()

    tick_slider = ttk.Scale(
        ai_row2,
        from_=2,
        to=15,
        value=tick_runtime.get("tick_seconds", 5),
        style="Global.Horizontal.TScale",
        command=on_tick_seconds_changed,
        length=110,
    )
    tick_slider.pack(side="right", padx=(6, 2))
    tk.Label(
        ai_row2,
        text="Decision interval",
        font=(FONT_FAMILY, 8),
        bg=COLOR_CARD,
        fg=COLOR_TEXT_PRIMARY,
    ).pack(side="right")

    # Control scope shares the "Selected" row. It is static text, and the row
    # it used to own cost the vertical space the run-parameter row now needs.
    ctrl_lbl = tk.Label(ai_row2, text="     Control: ", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY)
    ctrl_lbl.pack(side="left")

    ctrl_val_lbl = tk.Label(ai_row2, text="TSP + DBL", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_ACCENT)
    ctrl_val_lbl.pack(side="left")

    ctrl_rest_lbl = tk.Label(ai_row2, text=" for all bus routes", font=(FONT_FAMILY, 9), bg=COLOR_CARD, fg=COLOR_TEXT_PRIMARY)
    ctrl_rest_lbl.pack(side="left")

    def refresh_llm_status():
        runtime = global_config.get("ai_runtime", {})
        armed = bool(runtime.get("armed", False))
        status = str(runtime.get("last_status", "INACTIVE"))
        turn = int(runtime.get("last_turn", 0))
        if not armed:
            color = COLOR_TEXT_SECONDARY
            status_text_value = "LLM INACTIVE"
            button_text = f"{SYM_PLAY} RUN LLM"
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
            button_text = "DISARM LLM"
        llm_dot.config(fg=color)
        llm_status_text.config(text=status_text_value, fg=color)
        run_llm_btn.config(text=button_text)
        root.after(250, refresh_llm_status)

    root.after(250, refresh_llm_status)

    def refresh_route_buttons():
        repaint_route_flag_buttons()
        root.after(250, refresh_route_buttons)

    root.after(250, refresh_route_buttons)


    # 3. TRANSIT ROUTES CONTROL CARD
    transit_card = tk.Frame(
        root, bg=COLOR_CARD, highlightbackground=COLOR_CARD_BORDER,
        highlightthickness=1, bd=0
    )
    transit_card.pack(fill="x", padx=20, pady=5, ipady=4)

    t_header = tk.Frame(transit_card, bg=COLOR_CARD)
    t_header.pack(fill="x", padx=16, pady=(7, 5))

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
        route_flag_buttons[r_id] = {"tsp": tsp_btn, "dbl": dbl_btn}

    # 4. PER-APPROACH PARAMETERS SECTION
    approaches_container = tk.Frame(root, bg=COLOR_BG)
    approaches_container.pack(fill="both", expand=True, padx=20, pady=5)

    sec_title = tk.Label(
        approaches_container, text="Per-Approach General Traffic Parameters",
        font=(FONT_FAMILY, 11, "bold"), bg=COLOR_BG, fg=COLOR_TEXT_PRIMARY
    )
    sec_title.pack(anchor="w", pady=(2, 3))

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
