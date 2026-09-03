import pytest

import control_panel
from canvas_gemini import HEIGHT, H_Y, INT_X, LANE, ROAD_W, STOP, WIDTH
from signal_controller import SignalController
from vehicle import Bus


@pytest.mark.parametrize("route_id", list(control_panel.bus_routes_config))
def test_each_bus_route_completes_unobstructed_with_priority(route_id):
    config = control_panel.bus_routes_config[route_id]
    config["tsp_enabled"] = True
    config["dbl_enabled"] = True
    direction = config["origin"]
    first_node = 300 if direction == "EB" else 700
    lane_index = config["lanes"][first_node]
    y = H_Y - (lane_index + 0.5) * LANE if direction == "EB" else H_Y + (lane_index + 0.5) * LANE
    route_info = {
        "route_id": route_id,
        "origin": direction,
        "destination": config["destination"],
        "waypoints": dict(config["waypoints"]),
        "lanes": dict(config["lanes"]),
    }
    bus = Bus(-40 if direction == "EB" else WIDTH + 40, y, direction, route_info, f"COMPLETE_{route_id}")
    controller = SignalController({"green_time": 30}, yellow_time=3, red_clearance_time=3)
    vehicles = [bus]
    for _ in range(3000):
        controller.update(vehicles)
        signals = controller.get_all_signals(INT_X)
        bus.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
        if bus.x < -60 or bus.x > WIDTH + 60 or bus.y < -60 or bus.y > HEIGHT + 60:
            break
    assert set(config["waypoints"]).issubset(bus.passed_nodes)
    assert bus.passed_nodes == set(config["waypoints"])
    assert bus.x < -60 or bus.x > WIDTH + 60 or bus.y < -60 or bus.y > HEIGHT + 60
