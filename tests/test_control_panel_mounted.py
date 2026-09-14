"""Checkpoint 1, commit 3: the control panel mounts into MainWindow's left
pane instead of owning its own top-level window.
"""
import tkinter as tk

import src.ui.control_panel as control_panel
import src.core.main as main


def test_main_mounts_control_panel_into_the_left_pane_not_its_own_window():
    root, control_pane, _sim, _telemetry = main.build_main_window()
    try:
        returned = control_panel.create_dashboard_window(control_pane)

        assert returned is control_pane
        assert len(control_pane.winfo_children()) > 0
        # The panel's own standalone title never touched the shared root.
        assert root.title() == main.MAIN_WINDOW_TITLE
        assert isinstance(root, tk.Tk)
    finally:
        root.destroy()


def test_control_panel_wheel_scrolling_reaches_every_mounted_widget():
    root, control_pane, _sim, _telemetry = main.build_main_window()
    try:
        control_panel.create_dashboard_window(control_pane)
        main.bind_pane_mousewheel(control_pane, control_pane.scroll_canvas)

        # A representative sample: the top-level content frame itself and at
        # least one deeply nested widget must both be wheel-bound, or
        # scrolling would silently stop working the moment the pointer is
        # over a button/slider instead of bare pane background.
        assert control_pane.bind("<MouseWheel>")

        def find_leaf(widget):
            children = widget.winfo_children()
            if not children:
                return widget
            return find_leaf(children[0])

        leaf = find_leaf(control_pane)
        assert leaf is not control_pane
        assert leaf.bind("<MouseWheel>")
    finally:
        root.destroy()


def test_control_panel_standalone_still_owns_its_own_window():
    """No parent given: back-compat with every existing standalone caller."""
    root = control_panel.create_dashboard_window()
    try:
        assert isinstance(root, tk.Tk)
        assert root.title() == "Traffic & Transit Control Dashboard"
    finally:
        root.destroy()


def test_control_panel_embedded_in_a_bare_frame_touches_no_window_chrome():
    """Not just a MainWindow pane -- any plain frame parent works the same,
    and never touches the host window's own title."""
    host = tk.Tk()
    try:
        pane = tk.Frame(host)
        returned = control_panel.create_dashboard_window(pane)

        assert returned is pane
        assert len(pane.winfo_children()) > 0
        assert host.title() != "Traffic & Transit Control Dashboard"
    finally:
        host.destroy()


def test_write_ai_control_behavior_unchanged_when_mounted(tmp_path, monkeypatch):
    """The agent subprocess depends on ai_control.json; mounting must not
    change what gets written to it."""
    fake_path = tmp_path / "ai_control.json"
    monkeypatch.setattr(control_panel, "AI_CONTROL_PATH", fake_path)

    root, control_pane, _sim, _telemetry = main.build_main_window()
    try:
        control_panel.create_dashboard_window(control_pane)
        assert fake_path.exists()
        import json

        payload = json.loads(fake_path.read_text(encoding="utf-8"))
        assert set(payload) == {"armed", "model", "tick_seconds", "simulation_running"}
    finally:
        root.destroy()
