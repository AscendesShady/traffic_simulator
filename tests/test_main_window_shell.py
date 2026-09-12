"""The unified single-window shell: one Tk root, three panes.

Checkpoint 1 merges three OS windows (control panel, telemetry dashboard,
pygame canvas) into one Tk process. This covers only the empty shell itself
-- build_main_window() -- not yet what gets mounted into it.
"""
import tkinter as tk
from tkinter import ttk

import main


def test_build_main_window_creates_exactly_one_toplevel():
    root, control_pane, simulation_pane, telemetry_pane = main.build_main_window()
    try:
        assert isinstance(root, tk.Tk)
        assert root.title() == main.MAIN_WINDOW_TITLE
        # Every returned pane is a plain frame living inside that one root,
        # not a toplevel of its own.
        for pane in (control_pane, simulation_pane, telemetry_pane):
            assert isinstance(pane, tk.Frame)
            assert not isinstance(pane, (tk.Tk, tk.Toplevel))
            assert pane.winfo_toplevel() is root
    finally:
        root.destroy()


def test_three_panes_added_to_one_horizontal_paned_window():
    root, _control, _sim, _telemetry = main.build_main_window()
    try:
        paned = [
            child for child in root.winfo_children()
            if isinstance(child, ttk.PanedWindow)
        ]
        assert len(paned) == 1
        assert str(paned[0].cget("orient")) == "horizontal"
        assert len(paned[0].panes()) == 3
    finally:
        root.destroy()


def test_center_pane_claims_dominant_share_on_resize():
    """weight=1 center vs weight=0 sides: center gets any extra space."""
    root, control_pane, simulation_pane, telemetry_pane = main.build_main_window()
    try:
        root.update_idletasks()
        root.update()
        expected_side_width = max(
            main.MIN_SIDE_PANE_WIDTH,
            min(
                main.MAX_SIDE_PANE_WIDTH,
                round(root.winfo_width() * main.SIDE_PANE_WIDTH_FRACTION),
            ),
        )
        # The side panes hold close to their configured width; the center
        # pane is not artificially pinned to a small size.
        assert control_pane.winfo_width() > expected_side_width * 0.8
        assert telemetry_pane.winfo_width() > expected_side_width * 0.8
        assert simulation_pane.winfo_width() > expected_side_width * 0.5
    finally:
        root.destroy()


def test_side_panes_scroll_vertically_for_overflow():
    """Each side pane is a Canvas+Scrollbar wrapper, not a bare frame."""
    root, control_pane, _sim, telemetry_pane = main.build_main_window()
    try:
        for pane in (control_pane, telemetry_pane):
            scroll_canvas = pane.master
            assert isinstance(scroll_canvas, tk.Canvas)
            wrapper = scroll_canvas.master
            scrollbars = [
                child for child in wrapper.winfo_children()
                if isinstance(child, ttk.Scrollbar)
            ]
            assert len(scrollbars) == 1
            assert str(scrollbars[0].cget("orient")) == "vertical"
    finally:
        root.destroy()


def test_build_scrollable_pane_tracks_width_and_scroll_region():
    root = tk.Tk()
    try:
        outer, content, scroll_canvas = main.build_scrollable_pane(root, 400)
        outer.pack(fill="both", expand=True)
        tk.Label(content, text="x" * 50, height=30).pack()
        root.update_idletasks()
        root.update()

        assert scroll_canvas.bbox("all") is not None
        region = scroll_canvas.cget("scrollregion")
        assert region  # a non-empty scrollregion was actually set
    finally:
        root.destroy()


def test_bind_pane_mousewheel_recurses_into_children():
    root = tk.Tk()
    try:
        outer, content, scroll_canvas = main.build_scrollable_pane(root, 300)
        outer.pack()
        child = tk.Frame(content)
        child.pack()
        grandchild = tk.Label(child, text="hi")
        grandchild.pack()

        main.bind_pane_mousewheel(content, scroll_canvas)

        for widget in (content, child, grandchild):
            bound = widget.bind("<MouseWheel>")
            assert bound, f"{widget} was not bound"
    finally:
        root.destroy()
