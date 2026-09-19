import copy
import os
import sys
import tkinter

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.ui.control_panel as control_panel
from src.ui.canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from src.core.signal_controller import SignalController


@pytest.fixture
def base_geometry():
    return {"INT_X": INT_X, "H_Y": H_Y, "ROAD_W": ROAD_W, "STOP": STOP, "LANE": LANE}


@pytest.fixture
def signal_system():
    return SignalController(
        global_config={"green_time": 20, "is_running": True},
        yellow_time=3,
        red_clearance_time=3,
    )


@pytest.fixture(autouse=True)
def destroy_leftover_tk_root():
    """Tear down a Tk root a test leaves behind.

    A root is process-global state: left alive it keeps its interpreter and
    its pending `after` callbacks around for every later test, so one GUI
    test's window state can reach the next one. Tests that destroy their own
    root are unaffected -- this only sweeps up what they miss.
    """
    yield
    root = getattr(tkinter, "_default_root", None)
    if root is None:
        return
    try:
        root.destroy()
    except Exception:
        # Already destroyed, or its interpreter is gone: nothing left to do
        # but drop the reference so the next test starts clean.
        pass
    tkinter._default_root = None


@pytest.fixture(autouse=True)
def restore_shared_configuration():
    global_snapshot = copy.deepcopy(control_panel.global_config)
    routes_snapshot = copy.deepcopy(control_panel.bus_routes_config)
    approaches_snapshot = copy.deepcopy(control_panel.approach_configs)
    yield
    control_panel.global_config.clear()
    control_panel.global_config.update(global_snapshot)
    control_panel.bus_routes_config.clear()
    control_panel.bus_routes_config.update(routes_snapshot)
    control_panel.approach_configs.clear()
    control_panel.approach_configs.update(approaches_snapshot)
