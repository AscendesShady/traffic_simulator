"""Checkpoint 1, commit 4: the telemetry dashboard mounts into MainWindow's
right pane in-process, and the separate dashboard subprocess is gone.
"""
import inspect

import main
from telemetry_dashboard import TelemetryDashboard


def test_main_mounts_telemetry_dashboard_into_the_right_pane():
    root, _control, _sim, telemetry_pane = main.build_main_window()
    try:
        dashboard = TelemetryDashboard(telemetry_pane)

        assert dashboard.root is telemetry_pane
        assert len(telemetry_pane.winfo_children()) > 0
        assert len(dashboard.notebook.tabs()) == 4
        # The dashboard never touched the shared root's own title.
        assert root.title() == main.MAIN_WINDOW_TITLE
    finally:
        root.destroy()


def test_telemetry_wheel_scrolling_reaches_every_mounted_widget():
    root, _control, _sim, telemetry_pane = main.build_main_window()
    try:
        TelemetryDashboard(telemetry_pane)
        main.bind_pane_mousewheel(telemetry_pane, telemetry_pane.scroll_canvas)

        def find_leaf(widget):
            children = widget.winfo_children()
            if not children:
                return widget
            return find_leaf(children[0])

        leaf = find_leaf(telemetry_pane)
        assert leaf is not telemetry_pane
        assert leaf.bind("<MouseWheel>")
    finally:
        root.destroy()


def test_dashboard_subprocess_is_gone():
    """No separate dashboard_proc, no TRAFFIC_TELEMETRY_GEOMETRY plumbing,
    no DASHBOARD_PATH: the dashboard is mounted in-process instead."""
    source = inspect.getsource(main.main)

    assert "dashboard_proc" not in source
    assert "TRAFFIC_TELEMETRY_GEOMETRY" not in source
    assert "TelemetryDashboard(telemetry_pane)" in source
    assert not hasattr(main, "DASHBOARD_PATH")


def test_agent_subprocess_is_still_launched_the_same_way():
    """Only the dashboard subprocess is removed; the agent stays separate."""
    source = inspect.getsource(main.main)

    assert "agent_proc = subprocess.Popen(" in source
    assert "[sys.executable, str(AGENT_PATH)]" in source


def test_cleanup_no_longer_terminates_a_dashboard_process():
    source = inspect.getsource(main.main)
    cleanup_source = source.split("def cleanup():", 1)[1].split(
        "atexit.register", 1
    )[0]

    assert "dashboard_proc" not in cleanup_source
    assert "agent_proc.terminate()" in cleanup_source
    # The combined-export build at process exit is untouched.
    assert "export_session_excel()" in cleanup_source
