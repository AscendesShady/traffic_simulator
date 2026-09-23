"""Regressions for source queues and crawl detection."""
import src.core.main as main
from src.telemetry.bus_event_log import BusEventTracker
from src.telemetry.gridlock_monitor import GRIDLOCK_SUSTAIN_S, GridlockMonitor
from src.ui import control_panel
from src.core.vehicle import Vehicle, dbl_lane_center_y, dbl_lane_queue_ahead
from tests.helpers import make_bus_for_leg, NODE_A
from tests.test_gridlock_monitor import payload, vehicle, wb_y


def test_free_flow_heavy_vehicle_is_not_a_dbl_queue_but_close_crawl_is():
    priority_bus = make_bus_for_leg("R3_EB_ONLY", NODE_A, "priority")
    priority_bus.x = NODE_A - 330
    lane_y = dbl_lane_center_y("EB", 300)
    heavy = Vehicle(priority_bus.x + 85, lane_y, "EB", max_speed=0.5, is_heavy=True)
    heavy.speed = 0.5
    assert dbl_lane_queue_ahead(priority_bus, [priority_bus, heavy], 300, NODE_A) == []
    heavy.speed = 0.14
    assert dbl_lane_queue_ahead(priority_bus, [priority_bus, heavy], 300, NODE_A) == []
    leader = Vehicle(heavy.x + 33, lane_y, "EB", max_speed=0.5)
    leader.speed = 0.0
    assert dbl_lane_queue_ahead(priority_bus, [priority_bus, heavy, leader], 300, NODE_A) == [heavy, leader]


def test_due_bus_trips_accumulate_at_blocked_source_and_depart_in_order():
    main.reset_all_spawner_states()
    main.reset_run_metrics()
    for route in control_panel.bus_routes_config.values():
        route["active"] = False
    route = control_panel.bus_routes_config["R3_EB_ONLY"]
    route.update(active=True, headway_sec=1, manual_dispatch=False)
    lane = main.LANE_OPTIONS["EB"][1]
    blocker = Vehicle(-40, lane, "EB")
    vehicles = [blocker]
    for _ in range(3):
        main.check_and_dispatch_buses(vehicles, main.LANE_OPTIONS, 1.0)
    assert len(main.bus_pending_trips["R3_EB_ONLY"]) == 3
    assert main.run_metrics["bus_trips_scheduled"] == 3
    assert main.run_metrics["bus_trips_missed"] == 3
    vehicles.clear()
    main.check_and_dispatch_buses(vehicles, main.LANE_OPTIONS, 0.0)
    assert len(vehicles) == 1
    dispatch = vehicles[0].route_info
    assert dispatch["scheduled_departure_s"] == 1.0
    assert dispatch["actual_departure_s"] == 3.0
    assert dispatch["source_delay_s"] == 2.0
    assert dispatch["pending_trips_at_departure"] == 2
    record = BusEventTracker()._register(vehicles[0], 180)
    assert record["source_delay_s"] == 2.0
    main.reset_all_spawner_states()


def test_crawling_gridlock_has_sustained_onset_and_clearance():
    monitor = GridlockMonitor()
    crawling = [vehicle(str(i), 1000 + 30 * i, wb_y(i % 3), speed=0.08) for i in range(50)]
    monitor.sample(payload(0, crawling))
    monitor.sample(payload(GRIDLOCK_SUSTAIN_S + 1, crawling))
    assert monitor.state == "GRIDLOCK"
    assert monitor.timeline[-1]["stopped_vehicles"] == 0
    assert monitor.timeline[-1]["crawling_vehicles"] == 50
    moving = [dict(v, speed_px_per_frame=0.5) for v in crawling]
    monitor.sample(payload(GRIDLOCK_SUSTAIN_S + 2, moving))
    assert monitor.state == "FREE"


