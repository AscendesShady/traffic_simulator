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
WINDOW_MIN_HEIGHT = 500
WINDOW_SCREEN_MARGIN_X = 80
WINDOW_SCREEN_MARGIN_Y = 140


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
        self._resize_after_id = None
        self.initialize_history_state()
        self.latest_telemetry = None
        self.last_read_error = None
        self.build_ui()
        self.root.bind("<Configure>", self.schedule_responsive_layout, add="+")
        self.root.after_idle(self.apply_responsive_layout)
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
        self.header_title_lbl = tk.Label(
            header,
            text="LIVE TELEMETRY DASHBOARD",
            font=(FONT_FAMILY, 16, "bold"),
            bg=COLOR_BG,
            fg=COLOR_TEXT_PRIMARY,
        )
        self.header_title_lbl.pack(side="left")
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
        self.summary_tab, self.summary_content = self.create_responsive_tab()
        self.trends_tab, self.trends_content = self.create_responsive_tab()
        self.notebook.add(self.summary_tab, text="SUMMARY")
        self.notebook.add(self.trends_tab, text="SESSION TRENDS")

        self.metrics_frame = tk.Frame(self.summary_content, bg=COLOR_BG)
        self.metrics_frame.pack(fill="x", padx=20, pady=5)
        self.vars = {}
        self.metric_cards = []
        self.metric_title_labels = []
        metrics_layout = [
            [("Active Vehicles", "vehicles"), ("Active Buses", "buses"), ("Passenger Vol", "passengers")],
            [("Road/Demand Queue", "queued"), ("Avg Queue (20s)", "delay"), ("Congestion", "congestion")],
            [("TSP Active/Pending", "tsp"), ("DBL Active/Pending", "dbl"), ("Sim Timer", "timer")],
        ]
        for column_index in range(3):
            self.metrics_frame.grid_columnconfigure(
                column_index, weight=1, uniform="summary_metric_columns"
            )
        for row_index, row in enumerate(metrics_layout):
            self.metrics_frame.grid_rowconfigure(
                row_index, weight=1, uniform="summary_metric_rows"
            )
            for column_index, (title, key) in enumerate(row):
                card = tk.Frame(
                    self.metrics_frame,
                    bg=COLOR_CARD,
                    highlightbackground=COLOR_CARD_BORDER,
                    highlightthickness=1,
                )
                card.grid(
                    row=row_index,
                    column=column_index,
                    sticky="nsew",
                    padx=5,
                    pady=5,
                )
                title_label = tk.Label(
                    card,
                    text=title,
                    font=(FONT_FAMILY, 9, "bold"),
                    bg=COLOR_CARD,
                    fg=COLOR_TEXT_SECONDARY,
                    anchor="w",
                )
                title_label.pack(fill="x", padx=10, pady=(8, 0))
                value = tk.Label(
                    card,
                    text="--",
                    font=(FONT_FAMILY, 18, "bold"),
                    bg=COLOR_CARD,
                    fg=COLOR_ACCENT,
                )
                value.pack(fill="x", anchor="w", padx=10, pady=(0, 8))
                self.metric_cards.append(card)
                self.metric_title_labels.append(title_label)
                self.vars[key] = value

        recovery_card = tk.Frame(
            self.summary_content,
            bg=COLOR_CARD,
            highlightbackground=COLOR_DANGER,
            highlightthickness=1,
        )
        recovery_card.pack(fill="x", padx=25, pady=(7, 3))
        self.recovery_title_lbl = tk.Label(
            recovery_card,
            text="NETWORK GRIDLOCK RECOVERY",
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_DANGER,
        )
        self.recovery_title_lbl.pack(anchor="w", padx=10, pady=(6, 1))
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

        self.intersection_title_lbl = tk.Label(
            self.summary_content,
            text="INTERSECTION PHASE STATES",
            font=(FONT_FAMILY, 11, "bold"),
            bg=COLOR_BG,
            fg=COLOR_TEXT_PRIMARY,
        )
        self.intersection_title_lbl.pack(anchor="w", padx=20, pady=(10, 3))
        diagram_frame = tk.Frame(self.summary_content, bg=COLOR_BG)
        diagram_frame.pack(fill="both", expand=True, padx=20, pady=5)
        self.diagram_frame = diagram_frame
        self.diagram_frame.grid_rowconfigure(0, weight=1)
        self.diagram_frame.grid_columnconfigure(
            0, weight=1, uniform="intersection_node_columns"
        )
        self.diagram_frame.grid_columnconfigure(
            1, weight=1, uniform="intersection_node_columns"
        )
        self.node_title_labels = []
        self.node_a_canvas = self.create_node_canvas(diagram_frame, "NODE A (x=300)")
        self.node_a_canvas.master.grid(
            row=0, column=0, sticky="nsew", padx=(0, 5)
        )
        self.node_b_canvas = self.create_node_canvas(diagram_frame, "NODE B (x=700)")
        self.node_b_canvas.master.grid(
            row=0, column=1, sticky="nsew", padx=(5, 0)
        )
        self.node_a_canvas.bind(
            "<Configure>", lambda _event: self.draw_node_intersections()
        )
        self.node_b_canvas.bind(
            "<Configure>", lambda _event: self.draw_node_intersections()
        )
        self.build_trends_ui()

    def create_responsive_tab(self):
        """Return a tab whose content reflows with the available window size."""
        tab = tk.Frame(self.notebook, bg=COLOR_BG)
        content = tk.Frame(tab, bg=COLOR_BG)
        content.pack(fill="both", expand=True)
        return tab, content

    @staticmethod
    def responsive_profile(width, height):
        """Return bounded dimensions and fonts for the current client area."""
        width = max(WINDOW_MIN_WIDTH, int(width))
        height = max(WINDOW_MIN_HEIGHT, int(height))
        scale = max(0.60, min(1.0, min(width / 900.0, height / 780.0)))
        compact = width < 700 or height < 700
        return {
            "compact": compact,
            "header_font": max(11, round(16 * scale)),
            "status_font": max(8, round(10 * scale)),
            "section_font": max(8, round(11 * scale)),
            "metric_title_font": max(6, round(9 * scale)),
            "metric_value_font": max(11, min(16, round(18 * scale))),
            "detail_font": max(6, round(8 * scale)),
            "metric_row_height": max(34, min(58, round(height * 0.07))),
            "phase_height": max(35, min(85, round(height * 0.11))),
            "node_height": max(45, min(100, round(height * 0.13))),
            "chart_height": max(72, min(150, round((height - 165) / 3))),
            "wraplength": max(310, width - 92),
        }

    def schedule_responsive_layout(self, event=None):
        """Debounce resize work so dragging a window edge remains fluid."""
        if event is not None and event.widget is not self.root:
            return
        if self._resize_after_id is not None:
            self.root.after_cancel(self._resize_after_id)
        self._resize_after_id = self.root.after(35, self.apply_responsive_layout)

    def apply_responsive_layout(self, width=None, height=None):
        """Scale dashboard content to the window instead of exposing scrollbars."""
        self._resize_after_id = None
        width = self.root.winfo_width() if width is None else width
        height = self.root.winfo_height() if height is None else height
        profile = self.responsive_profile(width, height)
        compact = profile["compact"]

        self.header_title_lbl.config(
            font=(FONT_FAMILY, profile["header_font"], "bold")
        )
        self.status_lbl.config(
            font=(FONT_FAMILY, profile["status_font"], "bold")
        )
        self.header_title_lbl.master.pack_configure(
            padx=10 if compact else 20,
            pady=(6, 4) if compact else (15, 10),
        )
        self.notebook.pack_configure(
            padx=6 if compact else 14,
            pady=(0, 6 if compact else 12),
        )

        metric_pad = 1 if compact else 3
        self.metrics_frame.pack_configure(
            padx=8 if compact else 20,
            pady=2 if compact else 5,
        )
        for row_index in range(3):
            self.metrics_frame.grid_rowconfigure(
                row_index,
                weight=1,
                uniform="summary_metric_rows",
                minsize=profile["metric_row_height"],
            )
        for card, title_label in zip(self.metric_cards, self.metric_title_labels):
            card.grid_configure(padx=metric_pad, pady=metric_pad)
            title_label.config(
                font=(FONT_FAMILY, profile["metric_title_font"], "bold")
            )
            title_label.pack_configure(
                padx=5 if compact else 10,
                pady=(1 if compact else 4, 0),
            )
        for value_label in self.vars.values():
            value_label.config(
                font=(FONT_FAMILY, profile["metric_value_font"], "bold")
            )
            value_label.pack_configure(
                padx=5 if compact else 10,
                pady=(0, 1 if compact else 4),
            )

        detail_font = (FONT_FAMILY, profile["detail_font"])
        detail_bold_font = (FONT_FAMILY, profile["detail_font"], "bold")
        self.recovery_title_lbl.config(font=detail_bold_font)
        self.discharge_selected_lbl.config(font=detail_bold_font)
        self.discharge_status_lbl.config(font=detail_bold_font)
        self.discharge_reason_lbl.config(
            font=detail_font,
            wraplength=profile["wraplength"],
        )
        self.discharge_recommendation_lbl.config(
            font=detail_font,
            wraplength=profile["wraplength"],
        )
        self.recovery_title_lbl.master.pack_configure(
            padx=10 if compact else 25,
            pady=(3, 2) if compact else (7, 3),
        )
        self.recovery_title_lbl.pack_configure(pady=(3 if compact else 6, 0))
        self.discharge_recommendation_lbl.pack_configure(
            pady=(0, 3 if compact else 6)
        )

        self.phase_title_lbl.config(
            font=(FONT_FAMILY, max(8, profile["section_font"] - 1), "bold")
        )
        self.phase_hint_lbl.config(
            text=(
                "Nominal cycle | live dots"
                if compact
                else "Nominal plan | marker repeats | dots show live state"
            ),
            font=(FONT_FAMILY, profile["detail_font"]),
        )
        self.phase_status_lbl.config(font=detail_bold_font, width=16 if compact else 18)
        self.phase_heading.pack_configure(
            padx=6 if compact else 10,
            pady=(2, 0) if compact else (7, 0),
        )
        self.phase_cycle_canvas.config(height=profile["phase_height"])
        self.phase_cycle_canvas.master.pack_configure(
            padx=10 if compact else 25,
            pady=(3, 2) if compact else (10, 3),
        )

        self.intersection_title_lbl.config(
            font=(FONT_FAMILY, profile["section_font"], "bold")
        )
        self.intersection_title_lbl.pack_configure(
            padx=10 if compact else 20,
            pady=(3, 1) if compact else (10, 3),
        )
        self.diagram_frame.pack_configure(
            padx=10 if compact else 20,
            pady=2 if compact else 5,
        )
        for title_label in self.node_title_labels:
            title_label.config(
                font=(FONT_FAMILY, max(8, profile["section_font"] - 1), "bold")
            )
            title_label.pack_configure(pady=(3 if compact else 10, 0))
        self.node_a_canvas.config(height=profile["node_height"])
        self.node_b_canvas.config(height=profile["node_height"])

        self.trends_info_lbl.config(
            text=(
                "IN MEMORY ONLY  |  Session data clears on reset/close"
                if compact
                else "IN MEMORY ONLY  |  Up to 1 sample per simulated second  |  "
                f"Latest {HISTORY_MAX_POINTS} samples  |  Clears on reset/close"
            ),
            font=(FONT_FAMILY, profile["detail_font"]),
        )
        self.clear_history_btn.config(font=detail_bold_font)
        for chart in self.trend_charts:
            chart["title_label"].config(
                font=(FONT_FAMILY, profile["metric_title_font"], "bold")
            )
            chart["canvas"].config(height=profile["chart_height"])

        self.draw_phase_cycle(self.latest_telemetry)
        self.draw_node_intersections()
        self.draw_trend_charts()

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
        self.phase_heading = heading
        self.phase_title_lbl = tk.Label(
            heading,
            text="SIGNAL PHASE CYCLE",
            font=(FONT_FAMILY, 10, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_PRIMARY,
        )
        self.phase_title_lbl.pack(side="left")
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
        self.phase_hint_lbl = tk.Label(
            heading,
            text="Nominal plan | marker repeats | dots show live state",
            font=(FONT_FAMILY, 8),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_SECONDARY,
        )
        self.phase_hint_lbl.pack(side="right", padx=(10, 12))
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
        self.trends_info_lbl = tk.Label(
            controls,
            text=(
                "IN MEMORY ONLY  |  Up to 1 sample per simulated second  |  "
                f"Latest {HISTORY_MAX_POINTS} samples  |  Clears on reset/close"
            ),
            font=(FONT_FAMILY, 9),
            bg=COLOR_BG,
            fg=COLOR_TEXT_SECONDARY,
        )
        self.trends_info_lbl.pack(side="left")
        self.clear_history_btn = tk.Button(
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
        )
        self.clear_history_btn.pack(side="right")

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
        title_label = tk.Label(
            card,
            text=title,
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_SECONDARY,
        )
        title_label.pack(anchor="w", padx=10, pady=(7, 0))
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
            "title_label": title_label,
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
        title_label = tk.Label(
            card,
            text=title,
            font=(FONT_FAMILY, 10, "bold"),
            bg=COLOR_CARD,
            fg=COLOR_TEXT_SECONDARY,
        )
        title_label.pack(pady=(10, 0))
        self.node_title_labels.append(title_label)
        canvas = tk.Canvas(
            card,
            bg=COLOR_CARD,
            highlightthickness=0,
            width=1,
            height=135,
        )
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
        if width < 240 or height < 45:
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
        compact_chart = width < 650 or height < 90
        left = 82 if compact_chart else 142
        right = 8 if compact_chart else 14
        top = 15 if compact_chart else 25
        bottom = 10 if compact_chart else 25
        plot_width = max(1, width - left - right)
        row_gap = 2 if compact_chart else 5
        row_height = max(7, (height - top - bottom - 2 * row_gap) / 3)
        rows = (
            ("EW", "E-W" if compact_chart else "EAST-WEST CORRIDOR"),
            ("NS_A", "N-S NODE A" if compact_chart else "NORTH-SOUTH NODE A"),
            ("NS_B", "N-S NODE B" if compact_chart else "NORTH-SOUTH NODE B"),
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
                font=(FONT_FAMILY, 6 if compact_chart else 8, "bold"),
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
            if not compact_chart:
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
        displayed_boundaries = (0, cycle_frames) if compact_chart else boundaries
        for boundary in displayed_boundaries:
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

    def draw_node_intersections(self):
        """Redraw both live node diagrams after telemetry or geometry changes."""
        data = self.latest_telemetry or {}
        signal_state = data.get("signal_state", {})
        nodes = signal_state.get("nodes", {})
        fallback = signal_state.get("current_phase", "UNKNOWN")
        node_a = nodes.get("300", {})
        node_b = nodes.get("700", {})
        self.draw_intersection(
            self.node_a_canvas,
            "A",
            node_a.get("phase", fallback),
            node_a.get("signals"),
        )
        self.draw_intersection(
            self.node_b_canvas,
            "B",
            node_b.get("phase", fallback),
            node_b.get("signals"),
        )

    def draw_intersection(self, canvas, node_key, phase, signals=None):
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 10 or height < 10:
            return
        center_x, center_y = width // 2, height // 2
        shortest_side = min(width, height)
        road_w = max(14, min(40, int(shortest_side * 0.38)))
        signal_offset = max(9, min(40, int(shortest_side * 0.32)))
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
        radius = max(4, min(8, int(shortest_side * 0.08)))
        canvas.create_oval(center_x - signal_offset - radius, center_y - radius, center_x - signal_offset + radius, center_y + radius, fill=eb_color)
        canvas.create_oval(center_x + signal_offset - radius, center_y - radius, center_x + signal_offset + radius, center_y + radius, fill=wb_color)
        canvas.create_oval(center_x - radius, center_y - signal_offset - radius, center_x + radius, center_y - signal_offset + radius, fill=nb_color)
        canvas.create_oval(center_x - radius, center_y + signal_offset - radius, center_x + radius, center_y + signal_offset + radius, fill=sb_color)
        if height >= 55:
            canvas.create_text(center_x, height - 12, text=phase.replace("_", " "), fill=COLOR_TEXT_PRIMARY, font=(FONT_FAMILY, 8 if height < 90 else 9, "bold"))

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

        self.latest_telemetry = data
        self.root.update_idletasks()
        if hasattr(self, "phase_cycle_canvas"):
            self.draw_phase_cycle(data)
        self.draw_node_intersections()

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
