"""The unified single-window shell: one Tk root, three panes.

Checkpoint 1 merges three OS windows (control panel, telemetry dashboard,
pygame canvas) into one Tk process. This covers only the empty shell itself
-- build_main_window() -- not yet what gets mounted into it.
"""
import tkinter as tk
from tkinter import ttk

import src.core.main as main


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
        screen_width = root.winfo_screenwidth()
        control_width = main.control_pane_width_for(screen_width)
        telemetry_width = main.telemetry_pane_width_for(screen_width)
        # The side panes hold close to their configured width; the center
        # pane is not artificially pinned to a small size.
        assert control_pane.winfo_width() > control_width * 0.8
        assert telemetry_pane.winfo_width() > telemetry_width * 0.8
        assert simulation_pane.winfo_width() > telemetry_width * 0.5
    finally:
        root.destroy()


def test_window_width_fits_side_panes_and_canvas_without_slack():
    """The window is sized to its content, not the screen: two side panes
    plus the canvas and its gutter, so the network never floats in a wide
    empty center pane on a large monitor."""
    import src.ui.canvas_gemini as canvas

    wide_screen = 3840
    control = main.control_pane_width_for(wide_screen)
    telemetry = main.telemetry_pane_width_for(wide_screen)
    assert control == main.MAX_CONTROL_PANE_WIDTH
    assert telemetry == main.MAX_TELEMETRY_PANE_WIDTH
    assert main.main_window_width_for(wide_screen) == (
        control + telemetry + main.CANVAS_DISPLAY_WIDTH + 2 * main.SIMULATION_PANE_GUTTER
    )
    # The control column is a single stack of controls; telemetry keeps
    # roughly 30% more for its KPI grid and side-by-side node diagrams.
    assert main.control_pane_width_for(2560) == 256
    assert main.telemetry_pane_width_for(2560) == 333
    # A screen too narrow for all three still gets a usable window.
    assert main.main_window_width_for(1024) == 964
    # Height frames the canvas with the same gutter top and bottom.
    assert main.main_window_height_for(1440) == (
        canvas.HEIGHT + 2 * main.SIMULATION_PANE_GUTTER
    )
    assert main.main_window_height_for(700) == 600

    root, _control, simulation_pane, _telemetry = main.build_main_window()
    try:
        root.update_idletasks()
        root.update()
        fitted = main.main_window_width_for(root.winfo_screenwidth())
        if root.winfo_screenwidth() - 60 >= fitted:
            # Center pane is the canvas plus its gutter (sashes excepted).
            assert simulation_pane.winfo_width() <= (
                main.CANVAS_DISPLAY_WIDTH + 2 * main.SIMULATION_PANE_GUTTER + 16
            )
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
