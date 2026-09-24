import src.ui.control_panel as control_panel
from src.ui.canvas_gemini import H_Y, HEIGHT, INT_X, LANE, ROAD_W, STOP, WIDTH
from src.core.vehicle import Bus

# Node x-coordinates by name, so tests never hard-code the geometry.
NODE_A, NODE_B = INT_X[0], INT_X[1]

# Frames for a vehicle at 1 px/frame to cross the network by its longest
# route with signal waits to spare; it scales with the geometry, so a
# resized network does not strand a test's vehicle mid-route.
NETWORK_CROSSING_FRAMES = 2 * (WIDTH + HEIGHT)


def make_bus_for_leg(route_id, node_x, bus_id="TEST_BUS"):
    config = control_panel.bus_routes_config[route_id]
    origin = config["origin"]
    ordered_nodes = sorted(config["waypoints"], reverse=origin == "WB")
    leg_index = ordered_nodes.index(node_x)
    lane_index = config["lanes"][node_x]
    center_y = (
        H_Y - (lane_index + 0.5) * LANE
        if origin == "EB"
        else H_Y + (lane_index + 0.5) * LANE
    )
    bus_half_length = 21.0
    if origin == "EB":
        center_x = node_x - ROAD_W / 2 - STOP - bus_half_length - 5
    else:
        center_x = node_x + ROAD_W / 2 + STOP + bus_half_length + 5
    route_info = {
        "route_id": route_id,
        "origin": origin,
        "destination": config["destination"],
        "waypoints": dict(config["waypoints"]),
        "lanes": dict(config["lanes"]),
    }
    bus = Bus(center_x, center_y, origin, route_info, bus_id)
    bus.lane_index = lane_index
    bus.passed_nodes.update(ordered_nodes[:leg_index])
    return bus


def vehicle_bounds(vehicle):
    if vehicle.direction in ("EB", "WB"):
        return (
            vehicle.x - vehicle.length / 2,
            vehicle.y - vehicle.width / 2,
            vehicle.x + vehicle.length / 2,
            vehicle.y + vehicle.width / 2,
        )
    return (
        vehicle.x - vehicle.width / 2,
        vehicle.y - vehicle.length / 2,
        vehicle.x + vehicle.width / 2,
        vehicle.y + vehicle.length / 2,
    )


def rectangles_overlap(first, second):
    ax1, ay1, ax2, ay2 = vehicle_bounds(first)
    bx1, by1, bx2, by2 = vehicle_bounds(second)
    return ax1 < bx2 and ax2 > bx1 and ay1 < by2 and ay2 > by1


# The demand regime the Webster/units/soak tests are pinned to. The panel's
# live defaults are operator settings and move; a test that needs known
# flows takes these instead of reading control_panel.approach_configs.
REFERENCE_DEMAND = {
    "EB":   {"active": True, "model": "Poisson", "rate": 12, "turn_split": 0.80, "left_far_share": 0.10, "heavy_ratio": 0.10},
    "WB":   {"active": True, "model": "Poisson", "rate": 12, "turn_split": 0.80, "left_far_share": 0.10, "heavy_ratio": 0.10},
    "A_NB": {"active": True, "model": "Poisson", "rate": 8,  "turn_split": 0.75, "left_far_share": 0.00, "heavy_ratio": 0.15},
    "A_SB": {"active": True, "model": "Poisson", "rate": 8,  "turn_split": 0.75, "left_far_share": 0.10, "heavy_ratio": 0.15},
    "B_NB": {"active": True, "model": "Poisson", "rate": 8,  "turn_split": 0.75, "left_far_share": 0.10, "heavy_ratio": 0.15},
    "B_SB": {"active": True, "model": "Poisson", "rate": 8,  "turn_split": 0.75, "left_far_share": 0.00, "heavy_ratio": 0.15},
}


def reference_flows():
    return {key: dict(value) for key, value in REFERENCE_DEMAND.items()}


def apply_reference_demand():
    """Write REFERENCE_DEMAND into the live panel config (conftest restores it)."""
    for key, value in REFERENCE_DEMAND.items():
        control_panel.approach_configs[key].update(value)
