"""TelemetryDashboard must be mountable into a plain frame (as MainWindow
does) while remaining fully backward compatible when constructed with a
real toplevel (the existing standalone entry point).

Only one thing may own the process's real top-level window once the three
windows merge into one, so it may not call title()/geometry()/minsize()/
resizable() on anything that is not itself a toplevel.
"""
import tkinter as tk

from src.ui.telemetry_dashboard import TelemetryDashboard


def test_telemetry_dashboard_mounts_into_a_frame_without_owning_it():
    host = tk.Tk()
    try:
        pane = tk.Frame(host)
        dashboard = TelemetryDashboard(pane)

        assert dashboard.root is pane
        assert len(pane.winfo_children()) > 0
        assert hasattr(dashboard, "notebook")
        assert len(dashboard.notebook.tabs()) == 4
        assert host.title() != "Live Network Telemetry"
    finally:
        host.destroy()


def test_telemetry_dashboard_standalone_still_owns_its_own_window():
    root = tk.Tk()
    try:
        dashboard = TelemetryDashboard(root)
        assert dashboard.root is root
        assert root.title() == "Live Network Telemetry"
    finally:
        root.destroy()
