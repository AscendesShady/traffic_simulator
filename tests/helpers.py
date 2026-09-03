import control_panel
from canvas_gemini import H_Y, LANE, ROAD_W, STOP
from vehicle import Bus


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
