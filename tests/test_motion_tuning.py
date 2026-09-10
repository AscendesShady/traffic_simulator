import inspect

import pytest

import control_panel
import main
from canvas_gemini import H_Y, LANE
from signal_controller import SignalController
from tests.helpers import make_bus_for_leg
from vehicle import Bus, DBL_LANE_INDEX, Vehicle


def _lane_options():
    return {
        "EB": [
            H_Y - 0.5 * LANE,
            H_Y - 1.5 * LANE,
            H_Y - 2.5 * LANE,
        ],
        "WB": [
            H_Y + 0.5 * LANE,
            H_Y + 1.5 * LANE,
            H_Y + 2.5 * LANE,
        ],
    }


def _put_bus_at_stop_bar_distance(bus, distance):
    current = bus.distance_to_node_stop_bar(300, H_Y, 132, 10)
    delta = float(distance) - current
    bus.x += -delta if bus.direction == "EB" else delta


def test_speed_scale_applied_to_spawned_vehicles(monkeypatch):
    monkeypatch.setitem(control_panel.global_config, "vehicle_speed_scale", 0.5)
    main.reset_traffic_generation()
    monkeypatch.setattr(main, "should_spawn_vehicle", lambda *args: True)
    monkeypatch.setattr(main.random, "random", lambda: 0.5)
    monkeypatch.setattr(main.random, "choice", lambda values: values[0])
    monkeypatch.setattr(main.random, "uniform", lambda low, high: high)

    vehicles = []
    main.try_spawn_vehicle(
        vehicles,
        "EB",
        "EB",
        -20,
        _lane_options()["EB"],
        {"model": "Poisson", "rate": 60, "turn_split": 1.0, "heavy_ratio": 0.0},
        min_gap=0,
    )

    assert len(vehicles) == 1
    assert isinstance(vehicles[0], Vehicle)
    assert vehicles[0].max_speed == pytest.approx(1.4 * 0.5)


def test_speed_scale_applied_to_dispatched_bus(monkeypatch):
    monkeypatch.setitem(control_panel.global_config, "vehicle_speed_scale", 0.5)
    main.reset_traffic_generation()
    for config in control_panel.bus_routes_config.values():
        config["active"] = False
        config["manual_dispatch"] = False
    route = control_panel.bus_routes_config["R1_EB_A_NB"]
    route["active"] = True
    route["manual_dispatch"] = True

    vehicles = []
    main.check_and_dispatch_buses(vehicles, _lane_options(), 1 / 60)

    assert len(vehicles) == 1
    assert isinstance(vehicles[0], Bus)
    assert vehicles[0].max_speed == pytest.approx(0.5)


def test_speed_slider_value_waits_for_reset(monkeypatch):
    monkeypatch.setitem(control_panel.global_config, "vehicle_speed_scale", 0.75)
    monkeypatch.setitem(control_panel.global_config, "_active_vehicle_speed_scale", 0.5)

    assert main.get_active_vehicle_speed_scale() == pytest.approx(0.5)
    main.reset_traffic_generation()
    assert main.get_active_vehicle_speed_scale() == pytest.approx(0.75)


def test_bus_at_400px_uses_configured_eligibility_zone():
    route = control_panel.bus_routes_config["R1_EB_A_NB"]
    route["tsp_enabled"] = True
    route["dbl_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", 300)
    bus.lane_index = DBL_LANE_INDEX
    bus.y = H_Y - (DBL_LANE_INDEX + 0.5) * LANE
    _put_bus_at_stop_bar_distance(bus, 400)

    wide = SignalController({"green_time": 100, "priority_eligibility_px": 500})
    narrow = SignalController({"green_time": 100, "priority_eligibility_px": 250})

    assert wide.is_bus_tsp_eligible(bus, 300)
    assert wide.is_bus_dbl_eligible(bus, 300)
    assert not narrow.is_bus_tsp_eligible(bus, 300)
    assert not narrow.is_bus_dbl_eligible(bus, 300)


def test_eligibility_slider_value_waits_for_controller_reset():
    config = {"green_time": 100, "priority_eligibility_px": 250}
    controller = SignalController(config)
    config["priority_eligibility_px"] = 500

    assert controller.get_priority_eligibility_px() == 250
    controller.reset_all_state()
    assert controller.get_priority_eligibility_px() == 500


def test_priority_distance_checks_are_not_hardcoded_to_250():
    tsp_source = inspect.getsource(SignalController.is_bus_tsp_eligible)
    dbl_source = inspect.getsource(SignalController.is_bus_dbl_eligible)
    vehicle_source = inspect.getsource(Vehicle.update)

    assert "dist <= 250" not in tsp_source
    assert "dist <= 250" not in dbl_source
    assert "dist_to_stop <= 250.0" not in vehicle_source
    assert "get_priority_eligibility_px" in tsp_source
    assert "get_priority_eligibility_px" in dbl_source
    assert "get_priority_eligibility_px" in vehicle_source


def test_default_priority_window_is_about_sixteen_seconds():
    seconds = (
        control_panel.global_config["priority_eligibility_px"]
        / (1.0 * control_panel.global_config["vehicle_speed_scale"] * 60.0)
    )
    assert seconds == pytest.approx(16.6666667)

