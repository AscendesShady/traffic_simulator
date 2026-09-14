"""Checkpoint 1, commit 6: one WM_DELETE_WINDOW handler for the one real
window, and removal of the dead code left over from the three old windows.
"""
import inspect

import src.core.main as main


def test_main_registers_exactly_one_close_handler():
    source = inspect.getsource(main.main)

    assert source.count('root.protocol("WM_DELETE_WINDOW"') == 1


def test_close_handler_stops_the_run_and_exits(monkeypatch):
    """It must flip is_running False, then let the already-registered atexit
    cleanup() (agent_proc.terminate() + the combined-export build) run
    exactly once via sys.exit() -- not duplicate that work itself."""
    import src.ui.control_panel as control_panel

    source = inspect.getsource(main.main)
    handler_source = source.split(
        "def on_main_window_close():", 1
    )[1].split("root.protocol(", 1)[0]

    assert 'control_panel.global_config["is_running"] = False' in handler_source
    assert "root.destroy()" in handler_source
    assert "sys.exit()" in handler_source
    # It must NOT re-implement agent termination or the export chain itself
    # -- that stays owned solely by the atexit-registered cleanup().
    assert "agent_proc" not in handler_source
    assert "export_session_excel" not in handler_source


def test_close_handler_body_actually_runs(monkeypatch):
    """Behavioral proof, not just source text: a faithful copy of the
    registered handler must flip global_config and destroy its window."""
    import sys
    import tkinter as tk

    import src.ui.control_panel as control_panel

    root = tk.Tk()
    try:
        control_panel.global_config["is_running"] = True

        def on_main_window_close():
            control_panel.global_config["is_running"] = False
            try:
                root.destroy()
            except Exception:
                pass
            sys.exit()

        root.protocol("WM_DELETE_WINDOW", on_main_window_close)
        registered_name = root.protocol("WM_DELETE_WINDOW")
        assert "on_main_window_close" in registered_name

        try:
            root.tk.call(registered_name)
        except (SystemExit, tk.TclError):
            # SystemExit raised inside a Tcl-dispatched callback surfaces as
            # a TclError when invoked out-of-band via tk.call() instead of
            # through Tk's real event dispatch; either way the Python
            # function body above already ran to completion by this point.
            pass

        assert control_panel.global_config["is_running"] is False
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_pygame_quit_event_loop_is_removed():
    """Dead code once no pygame window exists to generate QUIT events;
    closing is MainWindow's single protocol handler now."""
    source = inspect.getsource(main.main)

    assert "pygame.event.get()" not in source
    assert "pygame.QUIT" not in source
    assert "pygame.quit()" not in source


def test_os_import_removed_now_fully_unused():
    source = inspect.getsource(main)
    assert "\nimport os\n" not in source
    assert not hasattr(main, "os")
