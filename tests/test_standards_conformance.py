"""Each behaviour here implements a published standard; the tests pin the
behaviour to the standard so a later change cannot quietly drift from it.

  ITE (2020) change and clearance intervals, bounded by MUTCD (2009) 4D.26
  HCM 7th ed. (2022) Ch. 19 lost time, Ch. 31 permitted-left critical headway
  NCHRP Report 812 common cycle, offsets and transition (coordination)
  TCQSM 3rd ed. (TCRP Report 165) Ch. 6 bus dwell
  FHWA SSAM (FHWA-HRT-08-051) TTC threshold; SUMO vType emergencyDecel
  FHWA Traffic Analysis Toolbox Vol. III: latent demand, GEH, replications
  SUMO plan-then-commit perception (09_INVARIANTS "planning precedes execution")
"""
import math

import pytest

import src.core.main as main
import src.core.vehicle as V
import src.core.webster as webster
from src.core.signal_controller import SignalController
from src.core.vehicle import Vehicle
from src.experiments import headless_run
from src.ui import control_panel
from src.ui.canvas_gemini import H_Y, INT_X, LANE, ROAD_W, STOP
from tests.helpers import NODE_A, NODE_B, make_bus_for_leg


# --- ITE / MUTCD change intervals -------------------------------------------

def test_ite_change_intervals_follow_the_kinematic_formulas():
    v, width = 12.09, 35.5
    yellow, all_red = webster.ite_change_intervals(v, width)
    assert yellow == pytest.approx(3.0)                   # 1 + 12.09 / 6.1 = 2.98 -> 3.0
    assert all_red == pytest.approx(3.5)                  # (35.5 + 6.1) / 12.09 = 3.44 -> 3.5
    # MUTCD bounds: yellow 3-6 s, red clearance <= 6 s.
    assert webster.ite_change_intervals(2.0, width)[0] == 3.0
    assert webster.ite_change_intervals(40.0, width)[0] == 6.0
    assert webster.ite_change_intervals(2.0, 60.0)[1] == 6.0


def test_reset_applies_ite_intervals_at_the_configured_speed(monkeypatch):
    monkeypatch.setattr(main, "calibrate_saturation_flow", lambda *a, **k: 1500.0)
    monkeypatch.setitem(control_panel.global_config, "vehicle_speed_scale", 0.6017)
    signals = SignalController(control_panel.global_config)
    main.perform_full_reset([], signals)
    assert (signals.yellow_time, signals.red_clearance_time) == (180, 210)
    timing = control_panel.global_config["_signal_timing"]
    assert timing["yellow_sec"] == 3.0 and timing["all_red_sec"] == 3.5
    monkeypatch.setitem(control_panel.global_config, "signal_change_intervals", "legacy")
    main.perform_full_reset([], signals)
    assert (signals.yellow_time, signals.red_clearance_time) == (60, 60)


# --- HCM lost time and displayed green ---------------------------------------

def test_hcm_lost_time_keeps_the_old_value_and_adds_start_up_loss():
    assert webster.lost_time_seconds(60, 60) == pytest.approx(4.0)       # l1 = e: unchanged
    assert webster.lost_time_seconds(180, 210) == pytest.approx(13.0)    # 2 x (2 + 6.5 - 2)
    assert webster.lost_time_seconds(180, 210, startup_lost_sec=1.5) == pytest.approx(12.0)


def test_displayed_greens_make_the_real_cycle_equal_webster_cycle():
    """G = g + l1 - e, so displayed greens plus two change intervals are
    exactly the cycle Webster chose."""
    l1, e, change = 1.5, 2.0, 6.5
    lost = webster.lost_time_seconds(180, 210, startup_lost_sec=l1, extension_sec=e)
    split = webster.compute_node_green_splits(
        {"EW": 700.0, "NS": 450.0}, 1600.0, lost_time_sec=lost,
        displayed_green_offset_sec=l1 - e,
    )
    real_cycle = split["EW_green_sec"] + split["NS_green_sec"] + 2 * change
    assert real_cycle == pytest.approx(split["cycle_time_sec"], abs=0.05)


def test_start_up_lost_time_is_measured_on_the_driving_engine():
    V.set_movement_model(V.MOVEMENT_MODEL_IDM)
    measured = main.calibrate_saturation_flow(0.5, 0.1, None, details=True)
    assert measured["startup_lost_source"] == "measured"
    assert 0.5 <= measured["startup_lost_sec"] <= 4.0      # HCM default 2.0 s


# --- Coordination (NCHRP Report 812) ------------------------------------------

def _coordinated_controller():
    config = {
        "green_time": 600,
        "webster_splits": {
            NODE_A: {"EW_green_frames": 1800, "NS_green_frames": 1200},
            NODE_B: {"EW_green_frames": 1500, "NS_green_frames": 1500},
        },
    }
    controller = SignalController(config, yellow_time=180, red_clearance_time=210)
    cycle = 1800 + 1200 + 2 * 390
    controller.set_coordination(cycle, {NODE_A: 0, NODE_B: 1100})
    return controller, cycle


def _ew_starts(controller, frames):
    starts = {x: [] for x in INT_X}
    for _ in range(frames):
        before = {x: n.phase for x, n in controller.nodes.items()}
        controller.update([])
        for x, n in controller.nodes.items():
            if n.phase == 0 and n.timer == 0 and before[x] != 0:
                starts[x].append(controller.frame_number)
    return starts


def test_coordinated_nodes_start_every_green_on_their_offset():
    controller, cycle = _coordinated_controller()
    starts = _ew_starts(controller, 5 * cycle)
    offsets = controller.coordination["offsets"]
    for node_x, frames in starts.items():
        assert frames, node_x
        for frame in frames:
            assert (frame - offsets[node_x]) % cycle == 0


def test_coordination_recovers_after_a_perturbation():
    """A TSP extension, a waiting all-red or a discharge moves a node off its
    offset; the bounded transition brings it back within a few cycles."""
    controller, cycle = _coordinated_controller()
    _ew_starts(controller, cycle)
    controller.nodes[NODE_B].timer = max(0, controller.nodes[NODE_B].timer - 600)  # 10 s late
    starts = _ew_starts(controller, 8 * cycle)[NODE_B]
    offset = controller.coordination["offsets"][NODE_B]
    errors = [((f - offset) % cycle + cycle // 2) % cycle - cycle // 2 for f in starts]
    assert abs(errors[0]) > 0
    assert errors[-1] == 0


# --- Dilemma zone (ITE) ---------------------------------------------------------

def _yellow(direction="EB"):
    node = {d: "RED" for d in ("EB", "WB", "NB", "SB")}
    node[direction] = "YELLOW"
    return {NODE_A: dict(node), NODE_B: dict(node)}


def _car_at(dist_to_bar, speed):
    bar = NODE_A - ROAD_W / 2 - STOP
    car = Vehicle(bar - dist_to_bar - 9, H_Y - 1.5 * LANE, "EB", max_speed=speed, lane_index=1)
    car.speed = speed
    return car


def test_vehicle_inside_its_stopping_distance_at_yellow_onset_goes():
    speed = 0.8
    critical = V.ite_stopping_distance_px(speed)
    controller = SignalController({"green_time": 999})
    near = _car_at(critical * 0.4, speed)
    # Its rear must clear the whole 33 m box, not just reach the bar.
    for _ in range(600):
        near.update(_yellow(), INT_X, H_Y, ROAD_W, STOP, LANE, [near], controller)
        if NODE_A in near.passed_nodes:
            break
    assert near.yellow_decision.get(NODE_A) is True
    assert NODE_A in near.passed_nodes


def test_vehicle_beyond_its_stopping_distance_at_yellow_onset_stops():
    speed = 0.8
    critical = V.ite_stopping_distance_px(speed)
    controller = SignalController({"green_time": 999})
    far = _car_at(critical * 1.3, speed)
    for _ in range(600):
        far.update(_yellow(), INT_X, H_Y, ROAD_W, STOP, LANE, [far], controller)
    assert far.yellow_decision.get(NODE_A) is False
    assert far.speed == 0.0
    assert far.distance_to_node_stop_bar(NODE_A, H_Y, ROAD_W, STOP) >= 0


# --- Permitted left gap acceptance (HCM Ch. 31) ---------------------------------

def test_near_side_turn_does_not_yield_to_opposing_through_traffic():
    """Left-hand traffic: the near-side turn never crosses the opposing
    stream, so an oncoming car about to reach its bar does not hold it."""
    controller = SignalController({"green_time": 999})
    controller.nodes[NODE_A].phase = 0        # EW green
    bar_eb = NODE_A - ROAD_W / 2 - STOP
    turner = Vehicle(bar_eb - 20, H_Y - 2.5 * LANE, "EB", target_turn="LEFT", lane_index=2)
    bar_wb = NODE_A + ROAD_W / 2 + STOP
    oncoming = Vehicle(bar_wb + 40, H_Y + 1.5 * LANE, "WB", max_speed=0.8, lane_index=1)
    oncoming.speed = 0.8
    assert controller.entry_would_be_granted(turner, NODE_A, [turner, oncoming])


def test_only_a_turner_longer_than_the_lane_sweeps_the_adjacent_through_lane():
    """At the square-corner pivot a vehicle swings half its length either
    side of its lane centre: a car (4.5 m) stays in its 5.5 m lane, a truck
    (7 m) and a bus do not."""
    through = Vehicle(NODE_A - 2.5 * LANE, H_Y - 1.5 * LANE, "EB", target_turn="STRAIGHT", lane_index=1)
    car = Vehicle(NODE_A - 80, H_Y - 2.5 * LANE, "EB", target_turn="LEFT", lane_index=2)
    truck = Vehicle(NODE_A - 80, H_Y - 2.5 * LANE, "EB", target_turn="LEFT", lane_index=2, is_heavy=True)
    for turner, blocked in ((car, False), (truck, True)):
        controller = SignalController({"green_time": 999})
        assert controller.request_intersection_entry(through, NODE_A, [through, turner])
        granted = controller.request_intersection_entry(turner, NODE_A, [through, turner])
        assert granted is (not blocked)


# --- Braking bound and surrogate safety -----------------------------------------

def test_idm_never_brakes_harder_than_the_emergency_bound_and_records_what_it_cannot_resolve():
    V.set_movement_model(V.MOVEMENT_MODEL_IDM)
    V.reset_safety_counters()
    red = {NODE_A: {d: "RED" for d in ("EB", "WB", "NB", "SB")}, NODE_B: {d: "RED" for d in ("EB", "WB", "NB", "SB")}}
    car = _car_at(5.0, 1.0)                   # cannot physically stop in 5 px
    bound = V.emergency_decel_px(car) + 1e-9
    previous = car.speed
    for _ in range(200):
        car.update(red, INT_X, H_Y, ROAD_W, STOP, LANE, [car], None)
        assert car.distance_to_node_stop_bar(NODE_A, H_Y, ROAD_W, STOP) >= 0
        drop = previous - car.speed
        # Only the recovery clamp (counted) may take more than the bound.
        if drop > bound:
            assert V.SAFETY_COUNTERS["stop_line_clamp_frames"] > 0
        previous = car.speed
    assert car.speed == 0.0
    assert V.SAFETY_COUNTERS["emergency_decel_events"] >= 1


def test_ttc_conflict_is_counted_below_the_ssam_threshold():
    V.reset_safety_counters()
    follower = Vehicle(100, H_Y - 1.5 * LANE, "EB", max_speed=1.0, lane_index=1)
    follower.speed = 1.0
    leader = Vehicle(100 + 18 + 30, H_Y - 1.5 * LANE, "EB", max_speed=0.0, lane_index=1)
    leader.speed = 0.0
    follower._record_ttc(30.0, leader)        # 30 frames = 0.5 s < 1.5 s
    assert V.SAFETY_COUNTERS["ttc_conflicts"] == 1


def test_a_vehicle_enters_the_network_at_a_safe_speed_for_the_gap_ahead():
    V.set_movement_model(V.MOVEMENT_MODEL_IDM)
    entering = Vehicle(0, H_Y - 1.5 * LANE, "EB", max_speed=1.0, lane_index=1)
    standing = Vehicle(60, H_Y - 1.5 * LANE, "EB", max_speed=0.0, lane_index=1)
    standing.speed = 0.0
    speed = V.safe_insertion_speed(entering, 40.0, standing)
    b = V.idm_params_px(entering)[2]
    assert speed == pytest.approx(math.sqrt(2 * b * (40.0 - V.IDM_S0_PX)))
    assert speed < entering.max_speed
    assert V.safe_insertion_speed(entering, float("inf"), None) == entering.max_speed


# --- Plan-then-commit perception ------------------------------------------------

def test_car_following_does_not_depend_on_update_order():
    """Same frame, same start state, opposite update order: identical result,
    because perception reads the frame-start snapshot (SUMO planMove before
    executeMove)."""
    V.set_movement_model(V.MOVEMENT_MODEL_IDM)
    green = {NODE_A: {d: "GREEN" for d in ("EB", "WB", "NB", "SB")},
             NODE_B: {d: "GREEN" for d in ("EB", "WB", "NB", "SB")}}

    def pair():
        leader = Vehicle(300, H_Y - 1.5 * LANE, "EB", max_speed=0.5, lane_index=1)
        leader.speed = 0.5
        follower = Vehicle(250, H_Y - 1.5 * LANE, "EB", max_speed=1.0, lane_index=1)
        follower.speed = 0.9
        return leader, follower

    results = []
    for order in ((0, 1), (1, 0)):
        vehicles = list(pair())
        V.index_frame(vehicles)
        for i in order:
            vehicles[i].update(green, INT_X, H_Y, ROAD_W, STOP, LANE, vehicles, None)
        V.index_frame(None)
        results.append([(round(v.x, 9), round(v.speed, 9)) for v in vehicles])
    assert results[0] == results[1]


# --- Latent demand (FHWA TAT Vol. III) ------------------------------------------

def test_demand_held_at_the_boundary_is_counted_and_charged():
    for key in control_panel.approach_configs:
        control_panel.approach_configs[key].update({"active": True, "rate": 60})
    headless_run.run(5, 1800, tsp=False, dbl=False)
    assert main.network_throughput["car_passenger_entry_wait_frames"] > 0
    columns = main._latent_demand_and_safety_columns()
    assert columns["vehicles_waiting_entry_at_end"] > 0
    assert 0 < columns["latent_demand_share_at_end"] < 1


def test_every_vehicle_that_leaves_writes_one_trip_record():
    headless_run.run(7, 3600, tsp=True, dbl=True)
    records = main._read_jsonl_rows(main.TRIPINFO_LOG_PATH)
    credited = [r for r in records if r["credited"]]
    assert len(credited) == main.network_throughput["vehicles_served_total"]
    assert all(r["duration_s"] is not None and r["duration_s"] >= 0 for r in records)


# --- Bus dwell (TCQSM) ----------------------------------------------------------

def test_dwell_is_drawn_per_trip_and_identical_across_arms():
    control_panel.global_config["random_seed"] = 99
    cfg = control_panel.bus_routes_config["R3_EB_ONLY"]
    main._route_trip_index.clear()
    first_arm = [main.draw_stop_plan("R3_EB_ONLY", cfg) for _ in range(5)]
    main._route_trip_index.clear()
    second_arm = [main.draw_stop_plan("R3_EB_ONLY", cfg) for _ in range(5)]
    assert first_arm == second_arm
    dwells = [plan[0]["dwell_frames"] / 60 for plan in first_arm]
    params = control_panel.global_config["bus_dwell"]
    assert all(d >= params["door_time_sec"] for d in dwells)
    assert len(set(dwells)) > 1               # stochastic, not a constant


def test_a_near_side_stop_withholds_priority_until_the_bus_has_dwelled():
    control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", NODE_A, "NEAR_SIDE")
    controller = SignalController({"green_time": 999})
    bus.stop_plan = [{"node": NODE_A, "side": "near", "dwell_frames": 60, "served": False, "dwelt": 0}]
    bus._update_stop(H_Y, ROAD_W, STOP)
    assert bus.near_side_stop_pending == NODE_A
    assert not controller.is_bus_tsp_eligible(bus, NODE_A)
    bus.stop_plan[0]["served"] = True
    bus._update_stop(H_Y, ROAD_W, STOP)
    assert controller.is_bus_tsp_eligible(bus, NODE_A)


# --- Replications (FHWA TAT Vol. III) -------------------------------------------

def test_required_replications_formula():
    # n = 3, mean 1.0, s = 0.3, e = 0.1: (4.303 * 0.3 / 0.1)^2 = 166.6 -> 167
    assert main.required_replications([0.7, 1.0, 1.3]) == 167
    assert main.required_replications([1.0]) is None
    assert main.required_replications([0.95, 1.0, 1.05]) == 5
