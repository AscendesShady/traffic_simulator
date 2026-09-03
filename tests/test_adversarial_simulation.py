import random

import pytest

import control_panel
import main
from canvas_gemini import HEIGHT, H_Y, INT_X, LANE, ROAD_W, STOP, WIDTH
from signal_controller import SignalController
from tests.helpers import rectangles_overlap
from vehicle import Bus, Vehicle


LANES = {
    "EB": [H_Y - 0.5 * LANE, H_Y - 1.5 * LANE, H_Y - 2.5 * LANE],
    "WB": [H_Y + 0.5 * LANE, H_Y + 1.5 * LANE, H_Y + 2.5 * LANE],
    "NB": {
        node: [node - 0.5 * LANE, node - 1.5 * LANE, node - 2.5 * LANE]
        for node in INT_X
    },
    "SB": {
        node: [node + 0.5 * LANE, node + 1.5 * LANE, node + 2.5 * LANE]
        for node in INT_X
    },
}


def make_car():
    direction = random.choice(("EB", "WB", "NB", "SB"))
    turn = "STRAIGHT" if random.random() < 0.8 else "LEFT"
    lane_index = random.choice((0, 1)) if turn == "STRAIGHT" else 2
    if direction == "EB":
        return Vehicle(-20, LANES[direction][lane_index], direction, target_turn=turn, lane_index=lane_index)
    if direction == "WB":
        return Vehicle(WIDTH + 20, LANES[direction][lane_index], direction, target_turn=turn, lane_index=lane_index)
    node = random.choice(INT_X)
    y = HEIGHT + 20 if direction == "NB" else -20
    return Vehicle(LANES[direction][node][lane_index], y, direction, target_turn=turn, lane_index=lane_index)


def make_bus(route_id, sequence):
    config = control_panel.bus_routes_config[route_id]
    direction = config["origin"]
    first_node = 300 if direction == "EB" else 700
    lane_index = config["lanes"][first_node]
    route_info = {
        "route_id": route_id,
        "origin": direction,
        "destination": config["destination"],
        "waypoints": dict(config["waypoints"]),
        "lanes": dict(config["lanes"]),
    }
    return Bus(
        -40 if direction == "EB" else WIDTH + 40,
        LANES[direction][lane_index],
        direction,
        route_info,
        f"STRESS_BUS_{sequence}",
    )


@pytest.mark.parametrize("seed", [0, 1])
def test_seeded_congestion_has_no_cross_direction_rectangle_overlap(seed):
    random.seed(seed)
    for config in control_panel.bus_routes_config.values():
        config["tsp_enabled"] = True
        config["dbl_enabled"] = True
    controller = SignalController({"green_time": 120}, 20, 20)
    vehicles = []
    dispatch_counters = {route_id: 0 for route_id in control_panel.bus_routes_config}
    sequence = 0
    for tick in range(3000):
        if tick % 45 == 0:
            vehicles.append(make_car())
        for route_id, config in control_panel.bus_routes_config.items():
            if not config["active"]:
                continue
            dispatch_counters[route_id] += 1
            # Accelerated headways retain their relative ordering but exercise
            # every active route inside a short deterministic regression run.
            threshold = max(300, config["headway_sec"] * 12)
            if dispatch_counters[route_id] >= threshold:
                sequence += 1
                vehicles.append(make_bus(route_id, sequence))
                dispatch_counters[route_id] = 0

        controller.update(vehicles)
        signals = controller.get_all_signals(INT_X)
        for vehicle in list(vehicles):
            vehicle.update(signals, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, controller)
        vehicles[:] = [
            vehicle
            for vehicle in vehicles
            if -60 <= vehicle.x <= WIDTH + 60 and -60 <= vehicle.y <= HEIGHT + 60
        ]
        for index, first in enumerate(vehicles):
            for second in vehicles[index + 1 :]:
                if first.direction != second.direction:
                    assert not rectangles_overlap(first, second), (
                        f"seed={seed} tick={tick}; "
                        f"first={type(first).__name__}/{getattr(first, 'route_id', '')}/"
                        f"{first.direction}@({first.x:.1f},{first.y:.1f}) turn={first.target_turn}; "
                        f"second={type(second).__name__}/{getattr(second, 'route_id', '')}/"
                        f"{second.direction}@({second.x:.1f},{second.y:.1f}) turn={second.target_turn}"
                    )


def test_production_congestion_peak_builds_a_queue_without_crashing():
    random.seed(17)
    main.reset_all_spawner_states()
    controller = SignalController({"green_time": 120}, 20, 20)
    vehicles = []
    eb_lanes = [
        H_Y - 0.5 * LANE,
        H_Y - 1.5 * LANE,
        H_Y - 2.5 * LANE,
    ]
    config = {
        "model": main.CONGESTION_MODEL,
        "rate": 30,
        # Concentrating arrivals in the left lane creates an intentionally
        # oversaturated but physically safe demand scenario.
        "turn_split": 0.0,
        "heavy_ratio": 0.2,
    }
    max_road_queue = 0
    requested = admitted = pending = 0

    try:
        for _ in range(main.CONGESTION_PEAK_SECONDS * 60):
            main.try_spawn_vehicle(
                vehicles, "EB", "EB", -20, eb_lanes, config
            )
            controller.update(vehicles)
            signals = controller.get_all_signals(INT_X)
            for vehicle in list(vehicles):
                vehicle.update(
                    signals,
                    INT_X,
                    H_Y,
                    ROAD_W,
                    STOP,
                    LANE,
                    vehicles,
                    controller,
                )
            vehicles[:] = [
                vehicle
                for vehicle in vehicles
                if -60 <= vehicle.x <= WIDTH + 60
                and -60 <= vehicle.y <= HEIGHT + 60
            ]
            max_road_queue = max(
                max_road_queue,
                sum(vehicle.speed < 0.25 for vehicle in vehicles),
            )

        state = main.spawner_states["EB"]
        requested = state["requested_arrivals"]
        admitted = state["admitted_arrivals"]
        pending = state["pending_arrivals"]
    finally:
        main.reset_all_spawner_states()

    assert requested > 0
    assert admitted > 0
    assert max_road_queue >= 5
    assert pending > 0 or requested > admitted


def test_all_sources_congestion_peak_remains_collision_safe_and_bounded():
    random.seed(23)
    main.reset_all_spawner_states()
    controller = SignalController({"green_time": 120}, 20, 20)
    vehicles = []
    sources = {
        "EB": ("EB", -20, LANES["EB"]),
        "WB": ("WB", WIDTH + 20, LANES["WB"]),
        "A_NB": ("NB", HEIGHT + 20, LANES["NB"][300]),
        "A_SB": ("SB", -20, LANES["SB"][300]),
        "B_NB": ("NB", HEIGHT + 20, LANES["NB"][700]),
        "B_SB": ("SB", -20, LANES["SB"][700]),
    }
    config = {
        "model": main.CONGESTION_MODEL,
        "rate": 30,
        "turn_split": 0.8,
        "heavy_ratio": 0.2,
    }
    maximum_live_vehicles = 0

    try:
        for tick in range(1200):
            for approach_key, (direction, spawn_coord, lanes) in sources.items():
                main.try_spawn_vehicle(
                    vehicles,
                    approach_key,
                    direction,
                    spawn_coord,
                    lanes,
                    config,
                )
            controller.update(vehicles)
            signals = controller.get_all_signals(INT_X)
            for vehicle in list(vehicles):
                vehicle.update(
                    signals,
                    INT_X,
                    H_Y,
                    ROAD_W,
                    STOP,
                    LANE,
                    vehicles,
                    controller,
                )
            vehicles[:] = [
                vehicle
                for vehicle in vehicles
                if -60 <= vehicle.x <= WIDTH + 60
                and -60 <= vehicle.y <= HEIGHT + 60
            ]
            maximum_live_vehicles = max(maximum_live_vehicles, len(vehicles))
            for index, first in enumerate(vehicles):
                for second in vehicles[index + 1 :]:
                    if first.direction != second.direction:
                        assert not rectangles_overlap(first, second), (
                            f"tick={tick}; {first.direction}@({first.x:.1f},"
                            f"{first.y:.1f}) overlaps {second.direction}@"
                            f"({second.x:.1f},{second.y:.1f})"
                        )
    finally:
        pending_counts = [
            state["pending_arrivals"] for state in main.spawner_states.values()
        ]
        main.reset_all_spawner_states()

    assert maximum_live_vehicles > 20
    assert all(0 <= count <= main.MAX_PENDING_ARRIVALS for count in pending_counts)
